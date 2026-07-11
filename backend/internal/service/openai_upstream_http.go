package service

import (
	"fmt"
	"net/http"

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
		Name: "Built-in Codex 0.144.1 (captured anyrouter.top 2026-07-11)",
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

func resolveOpenAIAPIKeyCodexTLSProfile(account *Account, tlsFPProfileService *TLSFingerprintProfileService) *tlsfingerprint.Profile {
	if account == nil || !account.ShouldUseOpenAITLSFingerprint() {
		return nil
	}
	profileID := account.GetTLSFingerprintProfileID()
	if profileID > 0 && tlsFPProfileService != nil {
		if profile := tlsFPProfileService.GetProfileByID(profileID); profile != nil {
			return profile
		}
	}
	if resolveOpenAIAPIKeyCodexMimicClientProfile(account).ID == openAIAPIKeyCodexMimicClientCLIRS0125 {
		return codexCLIRS0125TLSProfile()
	}
	return codexExec0144TLSProfile()
}

func (s *OpenAIGatewayService) resolveOpenAIAPIKeyCodexTLSProfile(account *Account) *tlsfingerprint.Profile {
	if s == nil {
		return nil
	}
	return resolveOpenAIAPIKeyCodexTLSProfile(account, s.tlsFPProfileService)
}

func doOpenAIHTTPUpstreamWithMimicTLS(httpUpstream HTTPUpstream, req *http.Request, proxyURL string, account *Account, tlsFPProfileService *TLSFingerprintProfileService, useAPIKeyMimicTLS bool) (*http.Response, error) {
	if httpUpstream == nil {
		return nil, fmt.Errorf("http upstream unavailable")
	}
	if useAPIKeyMimicTLS && account != nil && account.ShouldUseOpenAITLSFingerprint() {
		if tlsProfile := resolveOpenAIAPIKeyCodexTLSProfile(account, tlsFPProfileService); tlsProfile != nil {
			return httpUpstream.DoWithTLS(req, proxyURL, account.ID, account.Concurrency, tlsProfile)
		}
	}
	if account == nil {
		return httpUpstream.Do(req, proxyURL, 0, 0)
	}
	return httpUpstream.Do(req, proxyURL, account.ID, account.Concurrency)
}

func doOpenAIHTTPUpstream(httpUpstream HTTPUpstream, req *http.Request, proxyURL string, account *Account, tlsFPProfileService *TLSFingerprintProfileService) (*http.Response, error) {
	useAPIKeyMimicTLS := account != nil && account.ShouldUseOpenAITLSFingerprint()
	return doOpenAIHTTPUpstreamWithMimicTLS(httpUpstream, req, proxyURL, account, tlsFPProfileService, useAPIKeyMimicTLS)
}

func (s *OpenAIGatewayService) doOpenAIHTTPUpstream(req *http.Request, proxyURL string, account *Account) (*http.Response, error) {
	if s == nil {
		return nil, fmt.Errorf("http upstream unavailable")
	}
	return doOpenAIHTTPUpstream(s.httpUpstream, req, proxyURL, account, s.tlsFPProfileService)
}

func (s *OpenAIGatewayService) doOpenAIHTTPUpstreamForRequest(req *http.Request, proxyURL string, account *Account, mimicProfile openAIAPIKeyCodexMimicProfile) (*http.Response, error) {
	if s == nil {
		return nil, fmt.Errorf("http upstream unavailable")
	}
	return doOpenAIHTTPUpstreamWithMimicTLS(s.httpUpstream, req, proxyURL, account, s.tlsFPProfileService, mimicProfile.ShouldUseTLSFingerprint(account))
}
