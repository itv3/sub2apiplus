package service

import (
	"fmt"
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	utls "github.com/refraction-networking/utls"
)

// codexExec0144TLSProfile 复刻 2026-07-11 在 anyrouter.top 抓到的
// codex_exec/0.144.1 ClientHello（官方 install.sh latest，Debian 12 / aarch64）。
// 样本特征：ALPN=[h2, http/1.1]，10 个 cipher（含 0x00ff SCSV），
// curves/key_share 含 0x11ec(X25519MLKEM768) 后量子混合组，无 GREASE，
// 11 个 extension 顺序固定，TLS 1.3/1.2。典型 Rustls 指纹。
// ClientHello ≈1482-1976 B（MLKEM key_share 占主要字节）。
func codexExec0144TLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name:      "Built-in Codex 0.144.1 (captured anyrouter.top 2026-07-11)",
		Transport: tlsfingerprint.TransportOptions{DisableCompression: true},
		CipherSuites: []uint16{
			0x1302, 0x1301, 0x1303,
			0xc02c, 0xc02b, 0xcca9,
			0xc030, 0xc02f, 0xcca8,
			0x00ff, // TLS_EMPTY_RENEGOTIATION_INFO_SCSV
		},
		Curves:              []uint16{0x11ec, 0x001d, 0x0017, 0x0018},
		PointFormats:        []uint16{0},
		SignatureAlgorithms: []uint16{0x0503, 0x0403, 0x0603, 0x0807, 0x0806, 0x0805, 0x0804, 0x0601, 0x0501, 0x0401},
		ALPNProtocols:       []string{"h2", "http/1.1"},
		SupportedVersions:   []uint16{utls.VersionTLS13, utls.VersionTLS12},
		KeyShareGroups:      []uint16{0x11ec, 0x001d},
		PSKModes:            []uint16{1}, // psk_dhe_ke
		Extensions:          []uint16{11, 0, 5, 43, 13, 51, 16, 23, 35, 45, 10},
		TLSVersMin:          uint16(utls.VersionTLS12),
		TLSVersMax:          uint16(utls.VersionTLS13),
	}
}

func codexCLIRS0125TLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name: "Built-in Default (Node.js 24.x)",
	}
}

// resolveOpenAIAPIKeyCodexTLSProfile 解析账号在直连（proxyURL 为空）下的 mimic TLS 画像。
// 判定链必须与实际出站路径 doOpenAIHTTPUpstreamWithProfile 完全一致：由服务级
// active/previous 指针解析出画像，再统一经 ShouldUseTLSFingerprint 决定是否套指纹。
// previous 的 Desktop 画像没有可复用的实抓 TLS，因此返回 nil 表示走标准 Transport。
func resolveOpenAIAPIKeyCodexTLSProfile(account *Account, tlsFPProfileService *TLSFingerprintProfileService, cfg *config.Config) *tlsfingerprint.Profile {
	mimicProfile := resolveOpenAIAPIKeyCodexMimicTLSDecision(account, cfg)
	if !mimicProfile.ShouldUseTLSFingerprint(account) {
		return nil
	}
	return resolveOpenAIAPIKeyCodexTLSProfileForClient(account, tlsFPProfileService, mimicProfile.Client, "")
}

// resolveOpenAIAPIKeyCodexMimicTLSDecision 只构造 TLS 决策所需的字段，不生成 mimic 的
// session/turn 身份。供不应用 mimic header/body、但仍须与服务级画像指针保持同源的路径使用。
func resolveOpenAIAPIKeyCodexMimicTLSDecision(account *Account, cfg *config.Config) openAIAPIKeyCodexMimicProfile {
	return openAIAPIKeyCodexMimicProfile{
		Enabled: account.IsOpenAIAPIKeyCodexMimicEnabled(),
		Client:  resolveOpenAIAPIKeyCodexMimicClientProfile(account, cfg),
	}
}

func resolveOpenAIAPIKeyCodexTLSProfileForClient(
	account *Account,
	tlsFPProfileService *TLSFingerprintProfileService,
	client openAIAPIKeyCodexMimicClientProfile,
	proxyURL string,
) *tlsfingerprint.Profile {
	if account == nil || !account.ShouldUseOpenAITLSFingerprint() {
		return nil
	}
	profileID := account.GetTLSFingerprintProfileID()
	if profileID > 0 && tlsFPProfileService != nil {
		if profile := tlsFPProfileService.GetProfileByID(profileID); profile != nil {
			return profile
		}
	}
	switch client.ID {
	case openAIAPIKeyCodexMimicClientCodexExec0145:
		if proxyURL != "" {
			return newOpenAIOfficialEgressHTTPProxyTLSProfile()
		}
		return newOpenAIOfficialEgressHTTPTLSProfile()
	case openAIAPIKeyCodexMimicClientCLIRS0125:
		return codexCLIRS0125TLSProfile()
	default:
		return codexExec0144TLSProfile()
	}
}

// doOpenAIHTTPUpstreamWithProfile 是所有 OpenAI HTTP 出站路径的唯一入口：
// TLS 决策只能来自调用方给定的 mimicProfile，不在函数内部重新解析画像。
//
// 这样 header/body 画像与 TLS 画像必然出自同一个服务级 active/previous 指针，
// 消除 mode=previous 下「Desktop header + active CLI TLS」的跨画像混用。
// 不做 mimic 的路径（passthrough、WS-HTTP 桥接）传零值画像即表示不套 mimic TLS。
func doOpenAIHTTPUpstreamWithProfile(httpUpstream HTTPUpstream, req *http.Request, proxyURL string, account *Account, tlsFPProfileService *TLSFingerprintProfileService, mimicProfile openAIAPIKeyCodexMimicProfile) (*http.Response, error) {
	if httpUpstream == nil {
		return nil, fmt.Errorf("http upstream unavailable")
	}
	officialTLSProfile, officialEnabled, err := resolveOfficialEgressHTTPTransportProfile(req, nil)
	if err != nil {
		return nil, fmt.Errorf("resolve official egress HTTP transport: %w", err)
	}
	if officialEnabled {
		if account == nil {
			return nil, fmt.Errorf("official egress HTTP account is unavailable")
		}
		// 官方 Codex 在直连和 HTTP CONNECT 代理下使用两套已实证的 TLS/ALPN
		// 画像。代理路径必须在建立连接前切换到 h2 画像，且由 TLS Profile
		// 摘要和代理键共同隔离连接池，不能复用直连的空 ALPN 连接。
		if proxyURL != "" {
			officialTLSProfile = newOpenAIOfficialEgressHTTPProxyTLSProfile()
		}
		return httpUpstream.DoWithTLS(
			req,
			proxyURL,
			account.ID,
			account.Concurrency,
			officialTLSProfile,
		)
	}
	if mimicProfile.ShouldUseTLSFingerprint(account) {
		if tlsProfile := resolveOpenAIAPIKeyCodexTLSProfileForClient(account, tlsFPProfileService, mimicProfile.Client, proxyURL); tlsProfile != nil {
			return httpUpstream.DoWithTLS(req, proxyURL, account.ID, account.Concurrency, tlsProfile)
		}
	}
	if account == nil {
		return httpUpstream.Do(req, proxyURL, 0, 0)
	}
	return httpUpstream.Do(req, proxyURL, account.ID, account.Concurrency)
}

func (s *OpenAIGatewayService) doOpenAIHTTPUpstreamForRequest(req *http.Request, proxyURL string, account *Account, mimicProfile openAIAPIKeyCodexMimicProfile) (*http.Response, error) {
	if s == nil {
		return nil, fmt.Errorf("http upstream unavailable")
	}
	return doOpenAIHTTPUpstreamWithProfile(s.httpUpstream, req, proxyURL, account, s.tlsFPProfileService, mimicProfile)
}
