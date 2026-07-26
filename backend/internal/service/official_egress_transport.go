package service

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	utls "github.com/refraction-networking/utls"
)

// OfficialEgressTransportSelection 是路径 Provider 输出的确定性传输选择。
type OfficialEgressTransportSelection struct {
	ProfileID  string
	TLSProfile *tlsfingerprint.Profile
}

type officialEgressTransportProvider interface {
	Resolve(*OfficialEgressContext) (OfficialEgressTransportSelection, error)
}

type anthropicOfficialEgressHTTPTransportProvider struct{}
type openAIOfficialEgressHTTPTransportProvider struct{}
type openAIOfficialEgressWebSocketTransportProvider struct{}

func (anthropicOfficialEgressHTTPTransportProvider) Resolve(
	egressContext *OfficialEgressContext,
) (OfficialEgressTransportSelection, error) {
	if err := validateOfficialEgressTransportContext(
		egressContext,
		OfficialEgressTransportHTTP,
		false,
	); err != nil {
		return OfficialEgressTransportSelection{}, err
	}
	return OfficialEgressTransportSelection{
		ProfileID:  egressContext.transportProfileID,
		TLSProfile: newAnthropicOfficialEgressTLSProfile(),
	}, nil
}

func (openAIOfficialEgressHTTPTransportProvider) Resolve(
	egressContext *OfficialEgressContext,
) (OfficialEgressTransportSelection, error) {
	if err := validateOfficialEgressTransportContext(
		egressContext,
		OfficialEgressTransportHTTP,
		false,
	); err != nil {
		return OfficialEgressTransportSelection{}, err
	}
	return OfficialEgressTransportSelection{
		ProfileID:  egressContext.transportProfileID,
		TLSProfile: newOpenAIOfficialEgressHTTPTLSProfile(),
	}, nil
}

func (openAIOfficialEgressWebSocketTransportProvider) Resolve(
	egressContext *OfficialEgressContext,
) (OfficialEgressTransportSelection, error) {
	if err := validateOfficialEgressTransportContext(
		egressContext,
		OfficialEgressTransportWebSocket,
		true,
	); err != nil {
		return OfficialEgressTransportSelection{}, err
	}
	return OfficialEgressTransportSelection{
		ProfileID:  egressContext.transportProfileID,
		TLSProfile: newOpenAIOfficialEgressWebSocketTLSProfile(),
	}, nil
}

func resolveOfficialEgressTransportProvider(
	egressContext *OfficialEgressContext,
) (officialEgressTransportProvider, error) {
	if egressContext == nil {
		return nil, errors.New("official egress transport context is nil")
	}
	switch {
	case egressContext.targetPlatform == PlatformAnthropic && egressContext.transport == OfficialEgressTransportHTTP:
		return anthropicOfficialEgressHTTPTransportProvider{}, nil
	case egressContext.targetPlatform == PlatformOpenAI && egressContext.transport == OfficialEgressTransportHTTP:
		return openAIOfficialEgressHTTPTransportProvider{}, nil
	case egressContext.targetPlatform == PlatformOpenAI && egressContext.transport == OfficialEgressTransportWebSocket:
		return openAIOfficialEgressWebSocketTransportProvider{}, nil
	default:
		return nil, fmt.Errorf(
			"unsupported official egress transport profile: %s",
			egressContext.transportProfileID,
		)
	}
}

func resolveOfficialEgressTransportSelection(
	egressContext *OfficialEgressContext,
) (OfficialEgressTransportSelection, error) {
	provider, err := resolveOfficialEgressTransportProvider(egressContext)
	if err != nil {
		return OfficialEgressTransportSelection{}, err
	}
	selection, err := provider.Resolve(egressContext)
	if err != nil {
		return OfficialEgressTransportSelection{}, err
	}
	if selection.TLSProfile == nil || selection.ProfileID == "" {
		return OfficialEgressTransportSelection{}, errors.New(
			"official egress transport provider returned incomplete selection",
		)
	}
	return selection, nil
}

func resolveOfficialEgressHTTPTransportProfile(
	req *http.Request,
	fallback *tlsfingerprint.Profile,
) (*tlsfingerprint.Profile, bool, error) {
	if req == nil {
		return nil, false, errors.New("official egress requires HTTP request")
	}
	egressContext, enabled := OfficialEgressContextFromContext(req.Context())
	if !enabled {
		return fallback, false, nil
	}
	if egressContext.transport != OfficialEgressTransportHTTP {
		return nil, true, errors.New("official egress HTTP request contains non-HTTP context")
	}
	if err := validateOfficialEgressHTTPRequest(req, egressContext); err != nil {
		return nil, true, err
	}
	selection, err := resolveOfficialEgressTransportSelection(egressContext)
	if err != nil {
		return nil, true, err
	}
	return selection.TLSProfile, true, nil
}

func resolveOfficialEgressWebSocketTransportProfile(
	ctx *OfficialEgressContext,
) (*tlsfingerprint.Profile, error) {
	if ctx == nil {
		return nil, errors.New("official egress WebSocket context is nil")
	}
	if ctx.transport != OfficialEgressTransportWebSocket {
		return nil, errors.New("official egress WebSocket request contains non-WebSocket context")
	}
	selection, err := resolveOfficialEgressTransportSelection(ctx)
	if err != nil {
		return nil, err
	}
	return selection.TLSProfile, nil
}

func validateOfficialEgressTransportContext(
	egressContext *OfficialEgressContext,
	transport OfficialEgressTransport,
	requireFrozen bool,
) error {
	if egressContext == nil {
		return errors.New("official egress transport context is nil")
	}
	if strings.TrimSpace(egressContext.transportProfileID) == "" ||
		strings.TrimSpace(egressContext.clientProfileID) == "" ||
		strings.TrimSpace(egressContext.clientProfileDigest) == "" {
		return errors.New("official egress transport profile is incomplete")
	}
	if egressContext.transport != transport {
		return errors.New("official egress transport type conflicts with resolved context")
	}
	if egressContext.connectionPoolID == "" {
		return errors.New("official egress connection pool identity is missing")
	}
	if requireFrozen && !egressContext.frozen {
		return errors.New("official egress WebSocket transport requires frozen context")
	}
	return nil
}

// newAnthropicOfficialEgressTLSProfile 复现 Claude Code 2.1.220
// 目标请求的 Node.js ClientHello：17 个 cipher、HTTP/1.1 ALPN。
func newAnthropicOfficialEgressTLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name: "Official Claude Code 2.1.220 HTTP (capture-2026-07-26)",
		CipherSuites: []uint16{
			0x1301, 0x1302, 0x1303,
			0xc02b, 0xc02f, 0xc02c, 0xc030,
			0xcca9, 0xcca8,
			0xc009, 0xc013, 0xc00a, 0xc014,
			0x009c, 0x009d, 0x002f, 0x0035,
		},
		Curves:              []uint16{0x001d, 0x0017, 0x0018},
		PointFormats:        []uint16{0},
		SignatureAlgorithms: []uint16{0x0403, 0x0804, 0x0401, 0x0503, 0x0805, 0x0501, 0x0806, 0x0601, 0x0201},
		ALPNProtocols:       []string{"http/1.1"},
		SupportedVersions:   []uint16{utls.VersionTLS13, utls.VersionTLS12},
		KeyShareGroups:      []uint16{0x001d},
		PSKModes:            []uint16{1},
		Extensions:          []uint16{0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43, 21},
		TLSVersMin:          uint16(utls.VersionTLS12),
		TLSVersMax:          uint16(utls.VersionTLS13),
	}
}

// newOpenAIOfficialEgressHTTPTLSProfile 复现 Codex CLI 0.145.0
// Responses HTTP 的 30-cipher、空 ALPN ClientHello。
func newOpenAIOfficialEgressHTTPTLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name:      "Official Codex CLI 0.145.0 HTTP (capture-2026-07-26)",
		Transport: tlsfingerprint.TransportOptions{DisableCompression: true},
		CipherSuites: []uint16{
			0x1302, 0x1303, 0x1301,
			0xc02c, 0xc030, 0x009f,
			0xcca9, 0xcca8, 0xccaa,
			0xc02b, 0xc02f, 0x009e,
			0xc024, 0xc028, 0x006b,
			0xc023, 0xc027, 0x0067,
			0xc00a, 0xc014, 0x0039,
			0xc009, 0xc013, 0x0033,
			0x009d, 0x009c, 0x003d,
			0x003c, 0x0035, 0x002f,
		},
		Curves: []uint16{
			0x11ec, 0x001d, 0x0017, 0x001e,
			0x0018, 0x0019, 0x0100, 0x0101,
		},
		PointFormats: []uint16{0},
		SignatureAlgorithms: []uint16{
			0x0905, 0x0906, 0x0904,
			0x0403, 0x0503, 0x0603,
			0x0807, 0x0808, 0x081a, 0x081b, 0x081c,
			0x0809, 0x080a, 0x080b,
			0x0804, 0x0805, 0x0806,
			0x0401, 0x0501, 0x0601,
			0x0303, 0x0301, 0x0302,
			0x0402, 0x0502, 0x0602,
		},
		SupportedVersions: []uint16{utls.VersionTLS13, utls.VersionTLS12},
		KeyShareGroups:    []uint16{0x11ec, 0x001d},
		PSKModes:          []uint16{1},
		Extensions:        []uint16{65281, 0, 11, 10, 35, 22, 23, 13, 43, 45, 51},
		TLSVersMin:        uint16(utls.VersionTLS12),
		TLSVersMax:        uint16(utls.VersionTLS13),
	}
}

// newOpenAIOfficialEgressHTTPProxyTLSProfile 复现 Codex CLI 0.145.0
// 通过 HTTP CONNECT 代理发送 Responses HTTP 时的 rustls ClientHello。
// 阶段 0 的 client_to_mitm 抓包证明该路径使用 10 个 cipher、
// ALPN h2/http1.1 和随机扩展顺序，并通过 HTTP/2 发送请求；它不能与
// 直连时的 30-cipher、空 ALPN 画像共用同一个 Transport。
func newOpenAIOfficialEgressHTTPProxyTLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name:      "Official Codex CLI 0.145.0 HTTP Proxy (phase0-2026-07-24)",
		Transport: tlsfingerprint.TransportOptions{DisableCompression: true},
		CipherSuites: []uint16{
			0x1302, 0x1301, 0x1303,
			0xc02c, 0xc02b, 0xcca9,
			0xc030, 0xc02f, 0xcca8,
			0x00ff,
		},
		Curves:              []uint16{0x11ec, 0x001d, 0x0017, 0x0018},
		PointFormats:        []uint16{0},
		SignatureAlgorithms: []uint16{0x0503, 0x0403, 0x0603, 0x0807, 0x0806, 0x0805, 0x0804, 0x0601, 0x0501, 0x0401},
		ALPNProtocols:       []string{"h2", "http/1.1"},
		SupportedVersions:   []uint16{utls.VersionTLS13, utls.VersionTLS12},
		KeyShareGroups:      []uint16{0x11ec, 0x001d},
		PSKModes:            []uint16{1},
		Extensions:          []uint16{0, 5, 10, 11, 13, 16, 23, 35, 43, 45, 51},
		RandomizeExtensions: true,
		TLSVersMin:          uint16(utls.VersionTLS12),
		TLSVersMax:          uint16(utls.VersionTLS13),
	}
}

// newOpenAIOfficialEgressWebSocketTLSProfile 复现 Codex CLI 0.145.0
// Responses WS 的 rustls ClientHello。扩展集合固定，但每次握手随机排序，
// 对应阶段 0 四个目标握手的不同 JA3，禁止固定单一样本。
func newOpenAIOfficialEgressWebSocketTLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name:      "Official Codex CLI 0.145.0 WebSocket (phase0-2026-07-24)",
		Transport: tlsfingerprint.TransportOptions{DisableCompression: true},
		CipherSuites: []uint16{
			0x1302, 0x1301, 0x1303,
			0xc02c, 0xc02b, 0xcca9,
			0xc030, 0xc02f, 0xcca8,
			0x00ff,
		},
		Curves:              []uint16{0x11ec, 0x001d, 0x0017, 0x0018},
		PointFormats:        []uint16{0},
		SignatureAlgorithms: []uint16{0x0503, 0x0403, 0x0603, 0x0807, 0x0806, 0x0805, 0x0804, 0x0601, 0x0501, 0x0401},
		SupportedVersions:   []uint16{utls.VersionTLS13, utls.VersionTLS12},
		KeyShareGroups:      []uint16{0x11ec, 0x001d},
		PSKModes:            []uint16{1},
		Extensions:          []uint16{0, 5, 10, 11, 13, 23, 35, 43, 45, 51},
		RandomizeExtensions: true,
		TLSVersMin:          uint16(utls.VersionTLS12),
		TLSVersMax:          uint16(utls.VersionTLS13),
	}
}

func doAnthropicHTTPUpstreamWithOfficialEgress(
	httpUpstream HTTPUpstream,
	req *http.Request,
	proxyURL string,
	account *Account,
	fallbackProfile *tlsfingerprint.Profile,
) (*http.Response, error) {
	if httpUpstream == nil || account == nil {
		return nil, errors.New("Anthropic HTTP upstream is unavailable")
	}
	tlsProfile, _, err := resolveOfficialEgressHTTPTransportProfile(req, fallbackProfile)
	if err != nil {
		return nil, fmt.Errorf("resolve official egress HTTP transport: %w", err)
	}
	return httpUpstream.DoWithTLS(
		req,
		proxyURL,
		account.ID,
		account.Concurrency,
		tlsProfile,
	)
}

func officialEgressProxyStateKey(rawProxyURL string) string {
	if rawProxyURL == "" {
		return "direct"
	}
	sum := sha256.Sum256([]byte(rawProxyURL))
	return hex.EncodeToString(sum[:8])
}
