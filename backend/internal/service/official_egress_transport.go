package service

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	utls "github.com/refraction-networking/utls"
)

// tlsFingerprintProfileFromTransportSpec 只做中立 TransportSpec 到底层资源 DTO
// 的机械转换，不重新读取 service 画像或 active 发布事实。
func tlsFingerprintProfileFromTransportSpec(
	transport officialegress.TransportSpec,
) (*tlsfingerprint.Profile, error) {
	tls := transport.TLS
	if tls.Stack == "" || len(tls.CipherSuites) == 0 {
		return nil, errors.New("CompiledExecution 缺少 TLS 事实")
	}
	rules := make([]tlsfingerprint.H1HeaderOrderRule, len(tls.H1HeaderOrders))
	for index, rule := range tls.H1HeaderOrders {
		mode := tlsfingerprint.H1HeaderOrderModeStatic
		if rule.Mode == "swap_remove" {
			mode = tlsfingerprint.H1HeaderOrderModeSwapRemove
		}
		rules[index] = tlsfingerprint.H1HeaderOrderRule{
			Method: rule.Method, Path: rule.Path,
			RequiredHeaders:  append([]string(nil), rule.RequiredHeaders...),
			ForbiddenHeaders: append([]string(nil), rule.ForbiddenHeaders...),
			Order:            append([]string(nil), rule.Order...), Mode: mode,
			PrefixHeaders:  append([]string(nil), rule.PrefixHeaders...),
			RemoveHeaders:  append([]string(nil), rule.RemoveHeaders...),
			AppendHeaders:  append([]string(nil), rule.AppendHeaders...),
			RejectUnlisted: rule.RejectUnlisted,
		}
	}
	return &tlsfingerprint.Profile{
		Name: "Official Codex compiled " + transport.ConnectionPoolDigest,
		Transport: tlsfingerprint.TransportOptions{
			DisableCompression: true, LowercaseHeaders: tls.LowercaseHeaders,
			PreserveHeaderCase: append([]string(nil), tls.PreserveHeaderCase...),
			H1HeaderOrders:     rules, StrictH1Wire: tls.StrictH1Wire,
		},
		CipherSuites:        append([]uint16(nil), tls.CipherSuites...),
		Curves:              append([]uint16(nil), tls.SupportedGroups...),
		SignatureAlgorithms: append([]uint16(nil), tls.SignatureAlgorithms...),
		ALPNProtocols:       append([]string(nil), tls.ALPN...),
		SupportedVersions:   append([]uint16(nil), tls.SupportedVersions...),
		KeyShareGroups:      append([]uint16(nil), tls.KeyShareGroups...),
		PSKModes:            append([]uint16(nil), tls.PSKModes...),
		Extensions:          append([]uint16(nil), tls.Extensions...),
		RandomizeExtensions: tls.RandomizeExtensions,
		TLSVersMin:          tls.MinVersion, TLSVersMax: tls.MaxVersion,
	}, nil
}

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
	tlsProfile, err := resolveOfficialCodexTransportTLSProfileByID(
		egressContext.ProfileVersion(),
		egressContext.transportProfileID,
	)
	if err != nil {
		return OfficialEgressTransportSelection{}, fmt.Errorf("编译 Codex HTTP 传输画像：%w", err)
	}
	return OfficialEgressTransportSelection{
		ProfileID:  egressContext.transportProfileID,
		TLSProfile: tlsProfile,
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
	tlsProfile, err := resolveOfficialCodexTransportTLSProfileByID(
		egressContext.ProfileVersion(),
		egressContext.transportProfileID,
	)
	if err != nil {
		return OfficialEgressTransportSelection{}, fmt.Errorf("编译 Codex WS 传输画像：%w", err)
	}
	return OfficialEgressTransportSelection{
		ProfileID:  egressContext.transportProfileID,
		TLSProfile: tlsProfile,
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

// newOpenAIOfficialEgressHTTPTLSProfile 是旧调用点的兼容入口。TLS、H1 端点矩阵
// 与 strict 策略全部由不可变 Release 画像编译，不再在传输文件复制参数。
func newOpenAIOfficialEgressHTTPTLSProfile() *tlsfingerprint.Profile {
	return mustResolveOfficialCodexDefaultTLSProfile(officialCodexTransportProtocolHTTP1)
}

// OpenAIOfficialEgressHTTPTLSProfile 为尚在迁移的辅助端点提供默认 HTTP 画像。
// 路由是否经过代理不能改变 ClientHello；当前 Codex Release 只有配置有效自定义 CA
// 才切换到 rustls/h2，而本产品未开放该条件分支。参数仅为旧调用点兼容保留。
func OpenAIOfficialEgressHTTPTLSProfile(_ bool) *tlsfingerprint.Profile {
	return newOpenAIOfficialEgressHTTPTLSProfile()
}

// newOpenAIOfficialEgressHTTPProxyTLSProfile 复现当前 Codex CLI Release
// 通过 HTTP CONNECT 代理发送 Responses HTTP 时的 rustls ClientHello。
// 阶段 0 的 client_to_mitm 抓包证明该路径使用 10 个 cipher、
// ALPN h2/http1.1 和随机扩展顺序，并通过 HTTP/2 发送请求；它不能与
// 直连时的 30-cipher、空 ALPN 画像共用同一个 Transport。
func newOpenAIOfficialEgressHTTPProxyTLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name: "Official Codex CLI HTTP Proxy (phase0-2026-07-24)",
		Transport: tlsfingerprint.TransportOptions{
			DisableCompression: true,
			LowercaseHeaders:   true,
			// 官方 h2 实测为 16KB，Go 默认 10MB。
			H2MaxHeaderListSize: 16384,
		},
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

// 包初始化期预检两个内置传输画像。
//
// 它们的输入全是编译期常量（固定版本 + 固定传输 ID），编译失败属于构建错误而
// 不是运行时状态。此前失败只会在请求路径上 panic，而 gin 的 Recovery 会把它降级
// 成单请求 500——既达不到下方注释期望的"立即停止"，又让同一个构建错误以最难定位
// 的形式在 6 个调用点上反复出现。这里在启动时先走一遍同样的构造路径把失败提前；
// HTTP 兼容调用点仍需复用历史构造器；WS 已无兼容消费者，直接调用同一严格解析器，
// 不再为启动预检保留一个可被业务代码重新调用的旧包装入口。
var _ = precompileOfficialCodexBuiltinTLSProfiles()

func precompileOfficialCodexBuiltinTLSProfiles() struct{} {
	newOpenAIOfficialEgressHTTPTLSProfile()
	mustResolveOfficialCodexDefaultTLSProfile(officialCodexTransportProtocolWS)
	return struct{}{}
}

// resolveOfficialCodexDefaultTLSProfile 按版本与协议解析传输画像。
//
// 版本来自运行上下文或 registry 的 release 指针，传输 ID 由画像按协议给出，因此
// 执行层不再持有任何版本相关的常量：升级只需登记新快照并切换 release 指针。
func resolveOfficialCodexDefaultTLSProfile(
	version string,
	protocol string,
) (*tlsfingerprint.Profile, error) {
	profile, err := resolveCodexVersionProfile(version)
	if err != nil {
		return nil, err
	}
	transportID, err := profile.ResolveDefaultTransportID(protocol)
	if err != nil {
		return nil, err
	}
	return resolveOfficialCodexTransportTLSProfileByID(version, transportID)
}

// mustResolveOfficialCodexDefaultTLSProfile 仅供无法修改签名的历史构造器使用。
// 画像若在进程内失效，必须立即停止，不能返回 nil 让调用点悄悄降级为 Go 默认 TLS；
// 带动态版本/端点的执行路径一律使用返回 error 的严格解析 API。
//
// 同样的输入已在包初始化期预检通过，因此这里的 panic 只是理论兜底。它也不能改为
// 返回共享单例：API-key mimic 的 TLS-only 派生构造器会就地改写返回值（清掉
// H1HeaderOrders 与 StrictH1Wire），共享指针会让这些改写污染全部官方出站。
func mustResolveOfficialCodexDefaultTLSProfile(protocol string) *tlsfingerprint.Profile {
	version, err := officialCodexVersionForMode(officialClientProfileModeActive)
	if err != nil {
		panic(fmt.Sprintf("解析正式 Codex 版本失败：%v", err))
	}
	profile, err := resolveOfficialCodexDefaultTLSProfile(version, protocol)
	if err != nil {
		panic(fmt.Sprintf("编译 Codex %s 传输画像失败：%v", version, err))
	}
	return profile
}

func doAnthropicHTTPUpstreamWithOfficialEgress(
	httpUpstream HTTPUpstream,
	req *http.Request,
	proxyURL string,
	account *Account,
	fallbackProfile *tlsfingerprint.Profile,
) (*http.Response, error) {
	if httpUpstream == nil || account == nil {
		return nil, errors.New("anthropic HTTP upstream is unavailable")
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
	rawProxyURL = strings.TrimSpace(rawProxyURL)
	if rawProxyURL == "" {
		return "direct"
	}
	normalized := rawProxyURL
	if parsed, err := url.Parse(rawProxyURL); err == nil && parsed.Hostname() != "" {
		parsed.Scheme = strings.ToLower(parsed.Scheme)
		port := parsed.Port()
		if port == "" {
			switch parsed.Scheme {
			case "http":
				port = "80"
			case "https":
				port = "443"
			case "socks5", "socks5h":
				port = "1080"
			}
		}
		if port != "" {
			parsed.Host = net.JoinHostPort(strings.ToLower(parsed.Hostname()), port)
		}
		normalized = parsed.String()
	}
	sum := sha256.Sum256([]byte("sub2api.proxy-identity.v1\x00" + normalized))
	return hex.EncodeToString(sum[:])
}
