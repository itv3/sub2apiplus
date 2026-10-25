//go:build integration

package tlsfingerprint

import (
	"bytes"
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/md5" //nolint:gosec // JA3 的定义就是 MD5，不是安全用途
	"crypto/rand"
	"crypto/sha256"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"net"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
)

// 本地 TLS 指纹捕获服务端。
//
// 原先 TestJA3Fingerprint／TestAllProfiles 把 ClientHello 发给外网的 tls.peet.ws，由它回报 JA3／JA4。
// 这里换成进程内的 TLS 服务端：它原样记录客户端发来的第一条 TLS 记录（ClientHello 原始字节），
// 按 JA3、JA4 的公开定义自己计算指纹，并用与 tls.peet.ws 相同的 JSON 形状（FingerprintResponse）应答。
// 因此原来的期望值（JA3 哈希 44f88fca…、JA4 密码套件哈希 5b57614c22b0、a33745022dd6）可以在本地逐字复现，
// 断言强度不变，测试不再依赖外网、DNS 与对方服务的可用性。

type capturedConn struct {
	net.Conn
	mu       sync.Mutex
	recorded bytes.Buffer
	done     bool
}

// Read 在读到第一条完整 TLS 记录之前，把读到的字节原样记下来。
func (c *capturedConn) Read(p []byte) (int, error) {
	n, err := c.Conn.Read(p)
	c.mu.Lock()
	if !c.done && n > 0 {
		c.recorded.Write(p[:n])
		if c.recorded.Len() >= 5 && c.recorded.Len() >= 5+int(binary.BigEndian.Uint16(c.recorded.Bytes()[3:5])) {
			c.done = true
		}
	}
	c.mu.Unlock()
	return n, err
}

func (c *capturedConn) clientHello() []byte {
	c.mu.Lock()
	defer c.mu.Unlock()
	return append([]byte(nil), c.recorded.Bytes()...)
}

type captureListener struct{ net.Listener }

func (l captureListener) Accept() (net.Conn, error) {
	conn, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	return &capturedConn{Conn: conn}, nil
}

type captureContextKey struct{}

// localFingerprintServer 描述本地捕获服务端：dial 把任何目标地址都接到本地监听器，rootCAs 用来信任它的自签证书。
type localFingerprintServer struct {
	dial    func(ctx context.Context, network, addr string) (net.Conn, error)
	rootCAs *x509.CertPool
	host    string
}

// startLocalFingerprintServer 起一个只服务 /api/all 的本地 HTTPS 服务端，返回的主机名固定为 tls.test。
func startLocalFingerprintServer(t *testing.T) *localFingerprintServer {
	t.Helper()
	const host = "tls.test"
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		t.Fatalf("生成密钥失败：%v", err)
	}
	template := &x509.Certificate{
		SerialNumber: big.NewInt(1),
		Subject:      pkix.Name{CommonName: host},
		DNSNames:     []string{host},
		NotBefore:    time.Now().Add(-time.Hour),
		NotAfter:     time.Now().Add(time.Hour),
		KeyUsage:     x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
	}
	der, err := x509.CreateCertificate(rand.Reader, template, template, &key.PublicKey, key)
	if err != nil {
		t.Fatalf("生成自签证书失败：%v", err)
	}
	parsed, err := x509.ParseCertificate(der)
	if err != nil {
		t.Fatalf("解析自签证书失败：%v", err)
	}
	pool := x509.NewCertPool()
	pool.AddCert(parsed)

	raw, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("监听失败：%v", err)
	}
	tlsListener := tls.NewListener(captureListener{raw}, &tls.Config{
		Certificates: []tls.Certificate{{Certificate: [][]byte{der}, PrivateKey: key}},
		NextProtos:   []string{"http/1.1"},
		MinVersion:   tls.VersionTLS12,
	})
	mux := http.NewServeMux()
	mux.HandleFunc("/api/all", func(w http.ResponseWriter, r *http.Request) {
		captured, _ := r.Context().Value(captureContextKey{}).(*capturedConn)
		if captured == nil {
			http.Error(w, "没有捕获到 ClientHello", http.StatusInternalServerError)
			return
		}
		info, err := fingerprintFromClientHello(captured.clientHello())
		if err != nil {
			http.Error(w, err.Error(), http.StatusInternalServerError)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(FingerprintResponse{TLS: *info})
	})
	server := &http.Server{
		Handler:           mux,
		ReadHeaderTimeout: 10 * time.Second,
		ConnContext: func(ctx context.Context, conn net.Conn) context.Context {
			if tlsConn, ok := conn.(*tls.Conn); ok {
				if captured, ok := tlsConn.NetConn().(*capturedConn); ok {
					return context.WithValue(ctx, captureContextKey{}, captured)
				}
			}
			return ctx
		},
	}
	go func() { _ = server.Serve(tlsListener) }()
	t.Cleanup(func() { _ = server.Close() })

	address := raw.Addr().String()
	return &localFingerprintServer{
		host:    host,
		rootCAs: pool,
		dial: func(ctx context.Context, network, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, network, address)
		},
	}
}

// fetch 用 profile 的 Dialer 向本地服务端请求 /api/all，返回它按 ClientHello 算出的指纹。
// profile 的 RootCAs 会被换成本地自签证书；该字段只影响证书校验，不进入 ClientHello，因而不改变指纹。
func (s *localFingerprintServer) fetch(t *testing.T, profile *Profile) *TLSInfo {
	t.Helper()
	local := *profile
	local.RootCAs = s.rootCAs
	dialer := NewDialer(&local, s.dial)
	client := &http.Client{
		Transport: &http.Transport{DialTLSContext: dialer.DialTLSContext},
		Timeout:   30 * time.Second,
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, "https://"+s.host+"/api/all", nil)
	if err != nil {
		t.Fatalf("创建请求失败：%v", err)
	}
	response, err := client.Do(req)
	if err != nil {
		t.Fatalf("向本地指纹服务端请求失败：%v", err)
	}
	defer func() { _ = response.Body.Close() }()
	if response.StatusCode != http.StatusOK {
		t.Fatalf("本地指纹服务端返回 %d", response.StatusCode)
	}
	var parsed FingerprintResponse
	if err := json.NewDecoder(response.Body).Decode(&parsed); err != nil {
		t.Fatalf("解析本地指纹响应失败：%v", err)
	}
	return &parsed.TLS
}

// ---------------------------------------------------------------------------
// ClientHello 解析与 JA3／JA4 计算
// ---------------------------------------------------------------------------

type parsedClientHello struct {
	version           uint16
	ciphers           []uint16
	extensions        []uint16 // 按出现顺序
	curves            []uint16
	pointFormats      []uint8
	signatureAlgs     []uint16
	supportedVersions []uint16
	alpn              []string
	hasSNI            bool
}

func isGREASE(value uint16) bool { return value&0x0f0f == 0x0a0a && value>>8 == value&0xff }

func parseClientHello(record []byte) (*parsedClientHello, error) {
	if len(record) < 9 || record[0] != 0x16 {
		return nil, errors.New("不是 TLS 握手记录")
	}
	recordLength := int(binary.BigEndian.Uint16(record[3:5]))
	if len(record) < 5+recordLength {
		return nil, errors.New("ClientHello 记录不完整")
	}
	body := record[5 : 5+recordLength]
	if body[0] != 0x01 || len(body) < 4 {
		return nil, errors.New("不是 ClientHello")
	}
	cursor := body[4:]
	read := func(n int) ([]byte, error) {
		if len(cursor) < n {
			return nil, errors.New("ClientHello 被截断")
		}
		out := cursor[:n]
		cursor = cursor[n:]
		return out, nil
	}
	header, err := read(2 + 32)
	if err != nil {
		return nil, err
	}
	hello := &parsedClientHello{version: binary.BigEndian.Uint16(header[:2])}
	sessionLength, err := read(1)
	if err != nil {
		return nil, err
	}
	if _, err := read(int(sessionLength[0])); err != nil {
		return nil, err
	}
	cipherLength, err := read(2)
	if err != nil {
		return nil, err
	}
	ciphers, err := read(int(binary.BigEndian.Uint16(cipherLength)))
	if err != nil {
		return nil, err
	}
	for i := 0; i+1 < len(ciphers); i += 2 {
		hello.ciphers = append(hello.ciphers, binary.BigEndian.Uint16(ciphers[i:]))
	}
	compressionLength, err := read(1)
	if err != nil {
		return nil, err
	}
	if _, err := read(int(compressionLength[0])); err != nil {
		return nil, err
	}
	extensionsLength, err := read(2)
	if err != nil {
		return nil, err
	}
	extensions, err := read(int(binary.BigEndian.Uint16(extensionsLength)))
	if err != nil {
		return nil, err
	}
	for len(extensions) >= 4 {
		kind := binary.BigEndian.Uint16(extensions[:2])
		size := int(binary.BigEndian.Uint16(extensions[2:4]))
		if len(extensions) < 4+size {
			return nil, errors.New("扩展被截断")
		}
		data := extensions[4 : 4+size]
		extensions = extensions[4+size:]
		hello.extensions = append(hello.extensions, kind)
		switch kind {
		case 0:
			hello.hasSNI = true
		case 10: // supported_groups
			for i := 2; i+1 < len(data); i += 2 {
				hello.curves = append(hello.curves, binary.BigEndian.Uint16(data[i:]))
			}
		case 11: // ec_point_formats
			if len(data) > 1 {
				hello.pointFormats = append(hello.pointFormats, data[1:]...)
			}
		case 13: // signature_algorithms
			for i := 2; i+1 < len(data); i += 2 {
				hello.signatureAlgs = append(hello.signatureAlgs, binary.BigEndian.Uint16(data[i:]))
			}
		case 16: // ALPN
			for i := 2; i < len(data); {
				length := int(data[i])
				if i+1+length > len(data) {
					break
				}
				hello.alpn = append(hello.alpn, string(data[i+1:i+1+length]))
				i += 1 + length
			}
		case 43: // supported_versions
			for i := 1; i+1 < len(data); i += 2 {
				hello.supportedVersions = append(hello.supportedVersions, binary.BigEndian.Uint16(data[i:]))
			}
		}
	}
	return hello, nil
}

func joinUint16(values []uint16, separator string, format func(uint16) string, skipGREASE bool) string {
	parts := make([]string, 0, len(values))
	for _, value := range values {
		if skipGREASE && isGREASE(value) {
			continue
		}
		parts = append(parts, format(value))
	}
	return strings.Join(parts, separator)
}

func decimal(value uint16) string { return strconv.Itoa(int(value)) }
func hex4(value uint16) string    { return fmt.Sprintf("%04x", value) }

// ja3String 按 JA3 定义拼接：版本,密码套件,扩展,曲线,点格式；全部去掉 GREASE，数字用十进制、用 "-" 连接。
func (h *parsedClientHello) ja3String() string {
	points := make([]uint16, len(h.pointFormats))
	for i, value := range h.pointFormats {
		points[i] = uint16(value)
	}
	return strings.Join([]string{
		decimal(h.version),
		joinUint16(h.ciphers, "-", decimal, true),
		joinUint16(h.extensions, "-", decimal, true),
		joinUint16(h.curves, "-", decimal, true),
		joinUint16(points, "-", decimal, false),
	}, ",")
}

func truncatedSHA256(text string) string {
	sum := sha256.Sum256([]byte(text))
	return hex.EncodeToString(sum[:])[:12]
}

// ja4 按 JA4 定义：a 段（协议、版本、SNI、套件数、扩展数、ALPN 首尾字符），b 段（排序后的密码套件哈希），
// c 段（排序后的扩展哈希，不含 SNI 与 ALPN，再接按出现顺序的签名算法）。
func (h *parsedClientHello) ja4() string {
	version := h.version
	for _, candidate := range h.supportedVersions {
		if !isGREASE(candidate) && candidate > version {
			version = candidate
		}
	}
	versionText := map[uint16]string{0x0304: "13", 0x0303: "12", 0x0302: "11", 0x0301: "10"}[version]
	if versionText == "" {
		versionText = "00"
	}
	sni := "i"
	if h.hasSNI {
		sni = "d"
	}
	ciphers := make([]uint16, 0, len(h.ciphers))
	for _, value := range h.ciphers {
		if !isGREASE(value) {
			ciphers = append(ciphers, value)
		}
	}
	extensions := make([]uint16, 0, len(h.extensions))
	for _, value := range h.extensions {
		if !isGREASE(value) {
			extensions = append(extensions, value)
		}
	}
	alpn := "00"
	if len(h.alpn) > 0 && h.alpn[0] != "" {
		first := h.alpn[0]
		alpn = string(first[0]) + string(first[len(first)-1])
	}
	prefix := fmt.Sprintf("t%s%s%02d%02d%s", versionText, sni, len(ciphers), len(extensions), alpn)

	sortedCiphers := append([]uint16(nil), ciphers...)
	sort.Slice(sortedCiphers, func(i, j int) bool { return sortedCiphers[i] < sortedCiphers[j] })
	cipherHash := truncatedSHA256(joinUint16(sortedCiphers, ",", hex4, false))

	hashed := make([]uint16, 0, len(extensions))
	for _, value := range extensions {
		if value != 0 && value != 16 {
			hashed = append(hashed, value)
		}
	}
	sort.Slice(hashed, func(i, j int) bool { return hashed[i] < hashed[j] })
	extensionText := joinUint16(hashed, ",", hex4, false)
	if len(h.signatureAlgs) > 0 {
		extensionText += "_" + joinUint16(h.signatureAlgs, ",", hex4, false)
	}
	return prefix + "_" + cipherHash + "_" + truncatedSHA256(extensionText)
}

func fingerprintFromClientHello(record []byte) (*TLSInfo, error) {
	hello, err := parseClientHello(record)
	if err != nil {
		return nil, err
	}
	ja3 := hello.ja3String()
	sum := md5.Sum([]byte(ja3)) //nolint:gosec // JA3 定义使用 MD5
	return &TLSInfo{JA3: ja3, JA3Hash: hex.EncodeToString(sum[:]), JA4: hello.ja4()}, nil
}

// TestClientHelloFingerprintAlgorithms 用手工构造的 ClientHello 固定 JA3／JA4 计算本身的行为（含 GREASE 剔除与排序规则），
// 避免本地捕获服务端的算法错了却恰好“自洽”。
func TestClientHelloFingerprintAlgorithms(t *testing.T) {
	hello := &parsedClientHello{
		version:           0x0303,
		ciphers:           []uint16{0x0a0a, 0x1302, 0x1301, 0xc02b},
		extensions:        []uint16{0x0a0a, 0, 16, 13, 43, 10},
		curves:            []uint16{0x0a0a, 29, 23},
		pointFormats:      []uint8{0},
		signatureAlgs:     []uint16{0x0403, 0x0804},
		supportedVersions: []uint16{0x0a0a, 0x0304, 0x0303},
		alpn:              []string{"h2", "http/1.1"},
		hasSNI:            true,
	}
	if got, want := hello.ja3String(), "771,4866-4865-49195,0-16-13-43-10,29-23,0"; got != want {
		t.Fatalf("JA3 字符串 = %q，期望 %q", got, want)
	}
	cipherHash := truncatedSHA256("1301,1302,c02b")
	extensionHash := truncatedSHA256("000a,000d,002b_0403,0804")
	if got, want := hello.ja4(), "t13d0305h2_"+cipherHash+"_"+extensionHash; got != want {
		t.Fatalf("JA4 = %q，期望 %q", got, want)
	}
}
