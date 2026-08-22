package service

import (
	"bufio"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/pem"
	"errors"
	"math/big"
	"net"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

type claudeFWGClientHelloCapture struct {
	serverName        string
	cipherSuites      []uint16
	supportedCurves   []tls.CurveID
	supportedVersions []uint16
	supportedProtos   []string
}

type claudeFWGTLSCaptureResult struct {
	hello claudeFWGClientHelloCapture
	wire  string
	err   error
}

type claudeFWGCapturingUpstream struct {
	address string
	roots   *x509.CertPool
}

func (u *claudeFWGCapturingUpstream) Do(
	*http.Request,
	string,
	int64,
	int,
) (*http.Response, error) {
	return nil, errors.New("测试上游要求 Claude 候选提供 TLS 画像")
}

func (u *claudeFWGCapturingUpstream) DoWithTLS(
	request *http.Request,
	_ string,
	_ int64,
	_ int,
	profile *tlsfingerprint.Profile,
) (*http.Response, error) {
	if request == nil || profile == nil {
		return nil, errors.New("测试上游缺少请求或 TLS 画像")
	}
	cloned := *profile
	cloned.RootCAs = u.roots
	dialer := tlsfingerprint.NewDialer(
		&cloned,
		func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, u.address)
		},
	)
	transport := &http.Transport{
		DialTLSContext:     dialer.DialTLSContext,
		DisableCompression: cloned.Transport.DisableCompression,
		ForceAttemptHTTP2:  false,
	}
	defer transport.CloseIdleConnections()
	return transport.RoundTrip(request)
}

func TestClaudeFWGCandidateCapturesFrozenTLSAndH1Wire(t *testing.T) {
	listener, roots, result := startClaudeFWGTLSCapture(t)
	upstream := &claudeFWGCapturingUpstream{
		address: listener.Addr().String(),
		roots:   roots,
	}
	runtimeState, _ := newClaudeFWGServiceRuntime(t, upstream)
	trusted := officialegress.ClaudeTrustedFacts{
		Account: officialegress.ClaudeTrustedAccountFacts{
			AccountScope: "anthropic-oauth-account:91",
			AccountUUID:  claudeFWGServiceAccountUUID,
		},
		Session: officialegress.ClaudeTrustedSessionFacts{
			SessionID: "33333333-3333-4333-8333-333333333333",
			Source:    officialegress.ClaudeSessionSourcePlannerDerived,
		},
		Entrypoint: officialegress.ClaudeTrustedEntrypointFacts{
			Entrypoint:       officialegress.ClaudeEntrypointSDKCLI,
			IngressProtocol:  "managed-internal",
			IngressBindingID: "test:91",
		},
		Features: officialegress.ClaudeTrustedFeatureFacts{
			SystemMode: officialegress.ClaudeSystemDefault,
		},
	}
	ctx := withClaudeCandidateHTTPTransport(context.Background(), "", 91, 4)
	response, err := runtimeState.ClaudeCandidate.ExecuteEndpoint(
		ctx,
		officialegress.ClaudeEndpointExecution{
			EndpointKind: "lifecycle-hello",
			TrustedFacts: trusted,
			InvocationID: "55555555-5555-4555-8555-555555555555",
		},
	)
	require.NoError(t, err)
	require.NoError(t, response.Body.Close())

	captured := <-result
	require.NoError(t, captured.err)
	require.Equal(t, "api.anthropic.com", captured.hello.serverName)
	require.Equal(t, []string{"http/1.1"}, captured.hello.supportedProtos)
	require.Equal(t, []uint16{
		0x1301, 0x1302, 0x1303,
		0xc02b, 0xc02f, 0xc02c, 0xc030,
		0xcca9, 0xcca8,
		0xc009, 0xc013, 0xc00a, 0xc014,
		0x009c, 0x009d, 0x002f, 0x0035,
	}, captured.hello.cipherSuites)
	require.Equal(t, []tls.CurveID{tls.X25519, tls.CurveP256, tls.CurveP384}, captured.hello.supportedCurves)
	require.Equal(t, []uint16{tls.VersionTLS13, tls.VersionTLS12}, captured.hello.supportedVersions)

	lines := strings.Split(strings.TrimSuffix(captured.wire, "\r\n\r\n"), "\r\n")
	require.Equal(t, "HEAD /api/hello HTTP/1.1", lines[0])
	headerNames := make([]string, 0, len(lines)-1)
	for _, line := range lines[1:] {
		name, _, ok := strings.Cut(line, ":")
		require.True(t, ok, "非法 HTTP/1.1 Header 行：%q", line)
		headerNames = append(headerNames, name)
	}
	require.Equal(t, []string{
		"Connection",
		"User-Agent",
		"Accept",
		"Host",
		"Accept-Encoding",
	}, headerNames)
}

func startClaudeFWGTLSCapture(
	t *testing.T,
) (net.Listener, *x509.CertPool, <-chan claudeFWGTLSCaptureResult) {
	t.Helper()
	certificate, roots := newClaudeFWGTestCertificate(t)
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	require.NoError(t, err)
	t.Cleanup(func() { _ = listener.Close() })
	result := make(chan claudeFWGTLSCaptureResult, 1)
	go func() {
		connection, acceptErr := listener.Accept()
		if acceptErr != nil {
			result <- claudeFWGTLSCaptureResult{err: acceptErr}
			return
		}
		defer func() { _ = connection.Close() }()
		var hello claudeFWGClientHelloCapture
		server := tls.Server(connection, &tls.Config{
			Certificates: []tls.Certificate{certificate},
			MinVersion:   tls.VersionTLS12,
			MaxVersion:   tls.VersionTLS13,
			NextProtos:   []string{"http/1.1"},
			GetConfigForClient: func(info *tls.ClientHelloInfo) (*tls.Config, error) {
				hello = claudeFWGClientHelloCapture{
					serverName:        info.ServerName,
					cipherSuites:      append([]uint16(nil), info.CipherSuites...),
					supportedCurves:   append([]tls.CurveID(nil), info.SupportedCurves...),
					supportedVersions: append([]uint16(nil), info.SupportedVersions...),
					supportedProtos:   append([]string(nil), info.SupportedProtos...),
				}
				return nil, nil
			},
		})
		if handshakeErr := server.Handshake(); handshakeErr != nil {
			result <- claudeFWGTLSCaptureResult{hello: hello, err: handshakeErr}
			return
		}
		reader := bufio.NewReader(server)
		var wire strings.Builder
		for {
			line, readErr := reader.ReadString('\n')
			_, _ = wire.WriteString(line)
			if readErr != nil {
				result <- claudeFWGTLSCaptureResult{hello: hello, wire: wire.String(), err: readErr}
				return
			}
			if line == "\r\n" {
				break
			}
		}
		_, writeErr := server.Write([]byte("HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"))
		result <- claudeFWGTLSCaptureResult{hello: hello, wire: wire.String(), err: writeErr}
	}()
	return listener, roots, result
}

func newClaudeFWGTestCertificate(t *testing.T) (tls.Certificate, *x509.CertPool) {
	t.Helper()
	now := time.Now()
	rootKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	rootTemplate := &x509.Certificate{
		SerialNumber:          big.NewInt(1),
		Subject:               pkix.Name{CommonName: "Claude FW-G 测试根证书"},
		NotBefore:             now.Add(-time.Hour),
		NotAfter:              now.Add(time.Hour),
		IsCA:                  true,
		BasicConstraintsValid: true,
		KeyUsage:              x509.KeyUsageCertSign | x509.KeyUsageDigitalSignature,
	}
	rootDER, err := x509.CreateCertificate(rand.Reader, rootTemplate, rootTemplate, &rootKey.PublicKey, rootKey)
	require.NoError(t, err)
	rootCertificate, err := x509.ParseCertificate(rootDER)
	require.NoError(t, err)

	leafKey, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	require.NoError(t, err)
	leafTemplate := &x509.Certificate{
		SerialNumber: big.NewInt(2),
		Subject:      pkix.Name{CommonName: "api.anthropic.com"},
		DNSNames:     []string{"api.anthropic.com"},
		NotBefore:    now.Add(-time.Hour),
		NotAfter:     now.Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	leafDER, err := x509.CreateCertificate(rand.Reader, leafTemplate, rootCertificate, &leafKey.PublicKey, rootKey)
	require.NoError(t, err)
	leafKeyDER, err := x509.MarshalPKCS8PrivateKey(leafKey)
	require.NoError(t, err)
	certificate, err := tls.X509KeyPair(
		pem.EncodeToMemory(&pem.Block{Type: "CERTIFICATE", Bytes: leafDER}),
		pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: leafKeyDER}),
	)
	require.NoError(t, err)
	roots := x509.NewCertPool()
	roots.AddCert(rootCertificate)
	return certificate, roots
}
