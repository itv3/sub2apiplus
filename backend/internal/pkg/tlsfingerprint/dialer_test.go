//go:build unit

// Package tlsfingerprint provides TLS fingerprint simulation for HTTP clients.
//
// Unit tests for TLS fingerprint dialer.
// Integration tests that require external network are in dialer_integration_test.go
// and require the 'integration' build tag.
//
// Run unit tests: go test -v ./internal/pkg/tlsfingerprint/...
// Run integration tests: go test -v -tags=integration ./internal/pkg/tlsfingerprint/...
package tlsfingerprint

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"net/http"
	"net/url"
	"os"
	"strings"
	"testing"
	"time"

	utls "github.com/refraction-networking/utls"
)

// TestDialerBasicConnection tests that the dialer can establish TLS connections.
func TestDialerBasicConnection(t *testing.T) {
	skipNetworkTest(t)

	// Create a dialer with default profile
	profile := &Profile{
		Name:         "Test Profile",
		EnableGREASE: false,
	}
	dialer := NewDialer(profile, nil)

	// Create HTTP client with custom TLS dialer
	client := &http.Client{
		Transport: &http.Transport{
			DialTLSContext: dialer.DialTLSContext,
		},
		Timeout: 30 * time.Second,
	}

	// Make a request to a known HTTPS endpoint
	resp, err := client.Get("https://www.google.com")
	if err != nil {
		t.Fatalf("failed to connect: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()

	if resp.StatusCode != http.StatusOK {
		t.Errorf("expected status 200, got %d", resp.StatusCode)
	}
}

// TestJA3Fingerprint verifies the JA3/JA4 fingerprint matches expected value.
// This test uses tls.peet.ws to verify the fingerprint.
// Expected JA3 hash: 44f88fca027f27bab4bb08d4af15f23e (Node.js 24.x)
// Expected JA4: t13d1714h1_5b57614c22b0_7baf387fc6ff
func TestJA3Fingerprint(t *testing.T) {
	skipNetworkTest(t)

	profile := &Profile{
		Name:         "Default Profile Test",
		EnableGREASE: false,
	}
	dialer := NewDialer(profile, nil)

	client := &http.Client{
		Transport: &http.Transport{
			DialTLSContext: dialer.DialTLSContext,
		},
		Timeout: 30 * time.Second,
	}

	// Use tls.peet.ws fingerprint detection API
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", "https://tls.peet.ws/api/all", nil)
	if err != nil {
		t.Fatalf("failed to create request: %v", err)
	}
	req.Header.Set("User-Agent", "Claude Code/2.0.0 Node.js/24.3.0")

	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("failed to get fingerprint: %v", err)
	}
	defer func() { _ = resp.Body.Close() }()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("failed to read response: %v", err)
	}

	var fpResp FingerprintResponse
	if err := json.Unmarshal(body, &fpResp); err != nil {
		t.Logf("Response body: %s", string(body))
		t.Fatalf("failed to parse fingerprint response: %v", err)
	}

	// Log all fingerprint information
	t.Logf("JA3: %s", fpResp.TLS.JA3)
	t.Logf("JA3 Hash: %s", fpResp.TLS.JA3Hash)
	t.Logf("JA4: %s", fpResp.TLS.JA4)
	t.Logf("PeetPrint: %s", fpResp.TLS.PeetPrint)
	t.Logf("PeetPrint Hash: %s", fpResp.TLS.PeetPrintHash)

	// Verify JA3 hash matches expected value (Node.js 24.x default)
	expectedJA3Hash := "44f88fca027f27bab4bb08d4af15f23e"
	if fpResp.TLS.JA3Hash == expectedJA3Hash {
		t.Logf("✓ JA3 hash matches expected value: %s", expectedJA3Hash)
	} else {
		t.Errorf("✗ JA3 hash mismatch: got %s, expected %s", fpResp.TLS.JA3Hash, expectedJA3Hash)
	}

	// Verify JA4 cipher hash (stable middle part)
	expectedJA4CipherHash := "_5b57614c22b0_"
	if strings.Contains(fpResp.TLS.JA4, expectedJA4CipherHash) {
		t.Logf("✓ JA4 cipher hash matches: %s", expectedJA4CipherHash)
	} else {
		t.Errorf("✗ JA4 cipher hash mismatch: got %s, expected containing %s", fpResp.TLS.JA4, expectedJA4CipherHash)
	}

	// Verify JA4 prefix (t13d1714h1 or t13i1714h1)
	expectedJA4Prefix := "t13d1714h1"
	if strings.HasPrefix(fpResp.TLS.JA4, expectedJA4Prefix) {
		t.Logf("✓ JA4 prefix matches: %s (t13=TLS1.3, d=domain, 17=ciphers, 14=extensions, h1=HTTP/1.1)", expectedJA4Prefix)
	} else {
		altPrefix := "t13i1714h1"
		if strings.HasPrefix(fpResp.TLS.JA4, altPrefix) {
			t.Logf("✓ JA4 prefix matches (IP variant): %s", altPrefix)
		} else {
			t.Errorf("✗ JA4 prefix mismatch: got %s, expected %s or %s", fpResp.TLS.JA4, expectedJA4Prefix, altPrefix)
		}
	}

	// Verify JA3 contains expected TLS 1.3 cipher suites
	if strings.Contains(fpResp.TLS.JA3, "4865-4866-4867") {
		t.Logf("✓ JA3 contains expected TLS 1.3 cipher suites")
	} else {
		t.Logf("Warning: JA3 does not contain expected TLS 1.3 cipher suites")
	}

	// Verify extension list (14 extensions, Node.js 24.x order)
	expectedExtensions := "0-65037-23-65281-10-11-35-16-5-13-18-51-45-43"
	if strings.Contains(fpResp.TLS.JA3, expectedExtensions) {
		t.Logf("✓ JA3 contains expected extension list: %s", expectedExtensions)
	} else {
		t.Logf("Warning: JA3 extension list may differ")
	}
}

func skipNetworkTest(t *testing.T) {
	if testing.Short() {
		t.Skip("跳过网络测试（short 模式）")
	}
	if os.Getenv("TLSFINGERPRINT_NETWORK_TESTS") != "1" {
		t.Skip("跳过网络测试（需要设置 TLSFINGERPRINT_NETWORK_TESTS=1）")
	}
}

// TestDialerWithProfile tests that different profiles produce different fingerprints.
func TestDialerWithProfile(t *testing.T) {
	// Create two dialers with different profiles
	profile1 := &Profile{
		Name:         "Profile 1 - No GREASE",
		EnableGREASE: false,
	}
	profile2 := &Profile{
		Name:         "Profile 2 - With GREASE",
		EnableGREASE: true,
	}

	dialer1 := NewDialer(profile1, nil)
	dialer2 := NewDialer(profile2, nil)

	// Build specs and compare
	// Note: We can't directly compare JA3 without making network requests
	// but we can verify the specs are different
	spec1 := buildClientHelloSpecFromProfile(dialer1.profile)
	spec2 := buildClientHelloSpecFromProfile(dialer2.profile)

	// Profile with GREASE should have more extensions
	if len(spec2.Extensions) <= len(spec1.Extensions) {
		t.Error("expected GREASE profile to have more extensions")
	}
}

// TestHTTPProxyDialerBasic tests HTTP proxy dialer creation.
// Note: This is a unit test - actual proxy testing requires a proxy server.
func TestHTTPProxyDialerBasic(t *testing.T) {
	profile := &Profile{
		Name:         "Test Profile",
		EnableGREASE: false,
	}

	// Test that dialer is created without panic
	proxyURL := mustParseURL("http://proxy.example.com:8080")
	dialer := NewHTTPProxyDialer(profile, proxyURL)

	if dialer == nil {
		t.Fatal("expected dialer to be created")
	}
	if dialer.profile != profile {
		t.Error("expected profile to be set")
	}
	if dialer.proxyURL != proxyURL {
		t.Error("expected proxyURL to be set")
	}
}

// TestSOCKS5ProxyDialerBasic tests SOCKS5 proxy dialer creation.
// Note: This is a unit test - actual proxy testing requires a proxy server.
func TestSOCKS5ProxyDialerBasic(t *testing.T) {
	profile := &Profile{
		Name:         "Test Profile",
		EnableGREASE: false,
	}

	// Test that dialer is created without panic
	proxyURL := mustParseURL("socks5://proxy.example.com:1080")
	dialer := NewSOCKS5ProxyDialer(profile, proxyURL)

	if dialer == nil {
		t.Fatal("expected dialer to be created")
	}
	if dialer.profile != profile {
		t.Error("expected profile to be set")
	}
	if dialer.proxyURL != proxyURL {
		t.Error("expected proxyURL to be set")
	}
}

// TestBuildClientHelloSpec tests ClientHello spec construction.
func TestBuildClientHelloSpec(t *testing.T) {
	// Test with nil profile (should use defaults)
	spec := buildClientHelloSpecFromProfile(nil)

	if len(spec.CipherSuites) == 0 {
		t.Error("expected cipher suites to be set")
	}
	if len(spec.Extensions) == 0 {
		t.Error("expected extensions to be set")
	}

	// Verify default cipher suites are used
	if len(spec.CipherSuites) != len(defaultCipherSuites) {
		t.Errorf("expected %d cipher suites, got %d", len(defaultCipherSuites), len(spec.CipherSuites))
	}

	// Test with custom profile
	customProfile := &Profile{
		Name:         "Custom",
		EnableGREASE: false,
		CipherSuites: []uint16{0x1301, 0x1302},
	}
	spec = buildClientHelloSpecFromProfile(customProfile)

	if len(spec.CipherSuites) != 2 {
		t.Errorf("expected 2 cipher suites, got %d", len(spec.CipherSuites))
	}
}

func TestBuildClientHelloSpecAllowsTLS12WithoutALPNOrSupportedVersions(t *testing.T) {
	profile := &Profile{
		Name:          "codex_desktop_captured",
		CipherSuites:  []uint16{0x00ff, 0xc02c, 0xc02b},
		Curves:        []uint16{0x0017, 0x0018, 0x0019},
		PointFormats:  []uint16{0},
		Extensions:    []uint16{0, 10, 11, 13, 5, 18, 23},
		TLSVersMin:    0x0301,
		TLSVersMax:    0x0303,
		ALPNProtocols: nil,
	}

	spec := buildClientHelloSpecFromProfile(profile)
	if spec.TLSVersMin != 0x0301 {
		t.Fatalf("TLSVersMin: got 0x%04x, want 0x0301", spec.TLSVersMin)
	}
	if spec.TLSVersMax != 0x0303 {
		t.Fatalf("TLSVersMax: got 0x%04x, want 0x0303", spec.TLSVersMax)
	}
	for _, ext := range spec.Extensions {
		switch ext.(type) {
		case *utls.ALPNExtension:
			t.Fatal("captured Codex Desktop profile must not send ALPN")
		case *utls.SupportedVersionsExtension:
			t.Fatal("captured Codex Desktop profile must not send supported_versions")
		case *utls.KeyShareExtension:
			t.Fatal("captured Codex Desktop profile must not send key_share")
		}
	}
}

// TestToUTLSCurves tests curve ID conversion.
func TestToUTLSCurves(t *testing.T) {
	input := []uint16{0x001d, 0x0017, 0x0018}
	result := toUTLSCurves(input)

	if len(result) != len(input) {
		t.Errorf("expected %d curves, got %d", len(input), len(result))
	}

	for i, curve := range result {
		if uint16(curve) != input[i] {
			t.Errorf("curve %d: expected 0x%04x, got 0x%04x", i, input[i], uint16(curve))
		}
	}
}

// Helper function to parse URL without error handling.
func mustParseURL(rawURL string) *url.URL {
	u, err := url.Parse(rawURL)
	if err != nil {
		panic(err)
	}
	return u
}

// TestAllProfiles tests multiple TLS fingerprint profiles against tls.peet.ws.
// Run with: go test -v -run TestAllProfiles ./internal/pkg/tlsfingerprint/...
func TestAllProfiles(t *testing.T) {
	skipNetworkTest(t)

	profiles := []TestProfileExpectation{
		{
			// Default profile (Node.js 24.x)
			// JA3 Hash: 44f88fca027f27bab4bb08d4af15f23e
			// JA4: t13d1714h1_5b57614c22b0_7baf387fc6ff
			Profile: &Profile{
				Name:         "default_node_v24",
				EnableGREASE: false,
			},
			JA4CipherHash: "5b57614c22b0",
		},
		{
			// Linux x64 Node.js v22.17.1 (explicit profile)
			Profile: &Profile{
				Name:         "linux_x64_node_v22171",
				EnableGREASE: false,
				CipherSuites: []uint16{4866, 4867, 4865, 49199, 49195, 49200, 49196, 158, 49191, 103, 49192, 107, 163, 159, 52393, 52392, 52394, 49327, 49325, 49315, 49311, 49245, 49249, 49239, 49235, 162, 49326, 49324, 49314, 49310, 49244, 49248, 49238, 49234, 49188, 106, 49187, 64, 49162, 49172, 57, 56, 49161, 49171, 51, 50, 157, 49313, 49309, 49233, 156, 49312, 49308, 49232, 61, 60, 53, 47, 255},
				Curves:       []uint16{29, 23, 30, 25, 24, 256, 257, 258, 259, 260},
				PointFormats: []uint16{0, 1, 2},
				Extensions:   []uint16{0, 11, 10, 35, 16, 22, 23, 13, 43, 45, 51},
			},
			JA4CipherHash: "a33745022dd6",
		},
	}

	for _, tc := range profiles {
		tc := tc // capture range variable
		t.Run(tc.Profile.Name, func(t *testing.T) {
			fp := fetchFingerprint(t, tc.Profile)
			if fp == nil {
				return // fetchFingerprint already called t.Fatal
			}

			t.Logf("Profile: %s", tc.Profile.Name)
			t.Logf("  JA3:           %s", fp.JA3)
			t.Logf("  JA3 Hash:      %s", fp.JA3Hash)
			t.Logf("  JA4:           %s", fp.JA4)
			t.Logf("  PeetPrint:     %s", fp.PeetPrint)
			t.Logf("  PeetPrintHash: %s", fp.PeetPrintHash)

			// Verify expectations
			if tc.ExpectedJA3 != "" {
				if fp.JA3Hash == tc.ExpectedJA3 {
					t.Logf("  ✓ JA3 hash matches: %s", tc.ExpectedJA3)
				} else {
					t.Errorf("  ✗ JA3 hash mismatch: got %s, expected %s", fp.JA3Hash, tc.ExpectedJA3)
				}
			}

			if tc.ExpectedJA4 != "" {
				if fp.JA4 == tc.ExpectedJA4 {
					t.Logf("  ✓ JA4 matches: %s", tc.ExpectedJA4)
				} else {
					t.Errorf("  ✗ JA4 mismatch: got %s, expected %s", fp.JA4, tc.ExpectedJA4)
				}
			}

			// Check JA4 cipher hash (stable middle part)
			// JA4 format: prefix_cipherHash_extHash
			if tc.JA4CipherHash != "" {
				if strings.Contains(fp.JA4, "_"+tc.JA4CipherHash+"_") {
					t.Logf("  ✓ JA4 cipher hash matches: %s", tc.JA4CipherHash)
				} else {
					t.Errorf("  ✗ JA4 cipher hash mismatch: got %s, expected cipher hash %s", fp.JA4, tc.JA4CipherHash)
				}
			}
		})
	}
}

// fetchFingerprint makes a request to tls.peet.ws and returns the TLS fingerprint info.
func fetchFingerprint(t *testing.T, profile *Profile) *TLSInfo {
	t.Helper()

	dialer := NewDialer(profile, nil)
	client := &http.Client{
		Transport: &http.Transport{
			DialTLSContext: dialer.DialTLSContext,
		},
		Timeout: 30 * time.Second,
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(ctx, "GET", "https://tls.peet.ws/api/all", nil)
	if err != nil {
		t.Fatalf("failed to create request: %v", err)
		return nil
	}
	req.Header.Set("User-Agent", "Claude Code/2.0.0 Node.js/20.0.0")

	resp, err := client.Do(req)
	if err != nil {
		t.Fatalf("failed to get fingerprint: %v", err)
		return nil
	}
	defer func() { _ = resp.Body.Close() }()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		t.Fatalf("failed to read response: %v", err)
		return nil
	}

	var fpResp FingerprintResponse
	if err := json.Unmarshal(body, &fpResp); err != nil {
		t.Logf("Response body: %s", string(body))
		t.Fatalf("failed to parse fingerprint response: %v", err)
		return nil
	}

	return &fpResp.TLS
}

// codexExec0144TestProfile 复刻 service 层的 codex_exec/0.144.1 抓包规格，
// 用于在 tlsfingerprint 包内离线验证 MLKEM key_share 能被 utls 正确 marshal。
func codexExec0144TestProfile() *Profile {
	return &Profile{
		Name:                "codex_exec_0144_test",
		CipherSuites:        []uint16{0x1302, 0x1301, 0x1303, 0xc02c, 0xc02b, 0xcca9, 0xc030, 0xc02f, 0xcca8, 0x00ff},
		Curves:              []uint16{0x11ec, 0x001d, 0x0017, 0x0018},
		PointFormats:        []uint16{0},
		SignatureAlgorithms: []uint16{0x0503, 0x0403, 0x0603, 0x0807, 0x0806, 0x0805, 0x0804, 0x0601, 0x0501, 0x0401},
		ALPNProtocols:       []string{"h2", "http/1.1"},
		SupportedVersions:   []uint16{utls.VersionTLS13, utls.VersionTLS12},
		KeyShareGroups:      []uint16{0x11ec, 0x001d},
		PSKModes:            []uint16{1},
		Extensions:          []uint16{11, 0, 5, 43, 13, 51, 16, 23, 35, 45, 10},
		TLSVersMin:          uint16(utls.VersionTLS12),
		TLSVersMax:          uint16(utls.VersionTLS13),
	}
}

// TestCodexExec0144ClientHelloMarshals 离线验证 codex_exec/0.144.1 规格能编成合法
// ClientHello：重点是 X25519MLKEM768(0x11ec) 后量子混合组的 key_share 必须能被 utls
// 真正生成密钥并 marshal，而不是 panic 或空扩展。同时确认 ALPN 声明了 h2。
func TestCodexExec0144ClientHelloMarshals(t *testing.T) {
	spec := buildClientHelloSpecFromProfile(codexExec0144TestProfile())

	// 用一对内存管道，避免任何真实网络；只需要 BuildHandshakeState 生成 ClientHello 字节。
	clientConn, serverConn := net.Pipe()
	defer func() { _ = clientConn.Close() }()
	defer func() { _ = serverConn.Close() }()

	uconn := utls.UClient(clientConn, &utls.Config{ServerName: "anyrouter.top"}, utls.HelloCustom)
	if err := uconn.ApplyPreset(spec); err != nil {
		t.Fatalf("ApplyPreset failed: %v", err)
	}
	if err := uconn.BuildHandshakeState(); err != nil {
		t.Fatalf("BuildHandshakeState failed (MLKEM key_share 可能无法生成): %v", err)
	}

	raw := uconn.HandshakeState.Hello.Raw
	if len(raw) == 0 {
		t.Fatal("ClientHello Raw 为空，marshal 失败")
	}
	// 官方样本 ClientHello ≈1482-1976 B；MLKEM key_share(~1216B) 使其远超普通 ~600B。
	if len(raw) < 1000 {
		t.Fatalf("ClientHello 过短 (%d B)，MLKEM key_share 可能未真正写入", len(raw))
	}

	foundH2 := false
	for _, ext := range spec.Extensions {
		if alpn, ok := ext.(*utls.ALPNExtension); ok {
			for _, p := range alpn.AlpnProtocols {
				if p == "h2" {
					foundH2 = true
				}
			}
		}
	}
	if !foundH2 {
		t.Fatal("codex profile 必须在 ALPN 中声明 h2")
	}
}
