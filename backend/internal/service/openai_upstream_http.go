package service

import (
	"fmt"
	"net/http"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	utls "github.com/refraction-networking/utls"
)

// codexDesktopCapturedTLSProfile 复刻 2026-06-25 在 api.3ab.in 抓到的
// Codex Desktop/0.142.0 ClientHello。样本特征：
// JA4=t12d220700_0d4ca5d4ec72_3304d8368043，JA3 hash=e4d448cdfe06dc1243c1eb026c74ac9a，
// 无 ALPN / supported_versions / key_share，nginx 侧协议为 HTTP/1.1。
func codexDesktopCapturedTLSProfile() *tlsfingerprint.Profile {
	return &tlsfingerprint.Profile{
		Name: "Built-in Codex Desktop 0.142.0 (captured api.3ab.in 2026-06-25)",
		CipherSuites: []uint16{
			0x00ff,
			0xc02c, 0xc02b, 0xc024, 0xc023,
			0xc00a, 0xc009, 0xc008,
			0xc030, 0xc02f, 0xc028, 0xc027,
			0xc014, 0xc013, 0xc012,
			0x009d, 0x009c, 0x003d, 0x003c, 0x0035, 0x002f, 0x000a,
		},
		Curves:              []uint16{0x0017, 0x0018, 0x0019},
		PointFormats:        []uint16{0},
		SignatureAlgorithms: []uint16{0x0401, 0x0201, 0x0501, 0x0601, 0x0403, 0x0203, 0x0503, 0x0603},
		Extensions:          []uint16{0, 10, 11, 13, 5, 18, 23},
		TLSVersMin:          uint16(utls.VersionTLS10),
		TLSVersMax:          uint16(utls.VersionTLS12),
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
	return codexDesktopCapturedTLSProfile()
}

func (s *OpenAIGatewayService) resolveOpenAIAPIKeyCodexTLSProfile(account *Account) *tlsfingerprint.Profile {
	if s == nil {
		return nil
	}
	return resolveOpenAIAPIKeyCodexTLSProfile(account, s.tlsFPProfileService)
}

func doOpenAIHTTPUpstream(httpUpstream HTTPUpstream, req *http.Request, proxyURL string, account *Account, tlsFPProfileService *TLSFingerprintProfileService) (*http.Response, error) {
	if httpUpstream == nil {
		return nil, fmt.Errorf("http upstream unavailable")
	}
	if account != nil && account.ShouldUseOpenAITLSFingerprint() {
		if tlsProfile := resolveOpenAIAPIKeyCodexTLSProfile(account, tlsFPProfileService); tlsProfile != nil {
			return httpUpstream.DoWithTLS(req, proxyURL, account.ID, account.Concurrency, tlsProfile)
		}
	}
	if account == nil {
		return httpUpstream.Do(req, proxyURL, 0, 0)
	}
	return httpUpstream.Do(req, proxyURL, account.ID, account.Concurrency)
}

func (s *OpenAIGatewayService) doOpenAIHTTPUpstream(req *http.Request, proxyURL string, account *Account) (*http.Response, error) {
	if s == nil {
		return nil, fmt.Errorf("http upstream unavailable")
	}
	return doOpenAIHTTPUpstream(s.httpUpstream, req, proxyURL, account, s.tlsFPProfileService)
}
