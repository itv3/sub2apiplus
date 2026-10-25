//go:build integration

// Package tlsfingerprint provides TLS fingerprint simulation for HTTP clients.
//
// Integration tests for verifying TLS fingerprint correctness.
// 这些用例原先向外网 tls.peet.ws 取回 JA3／JA4；现在改为本地 TLS 捕获服务端
// （见 clienthello_local_test.go）：服务端原样记录 ClientHello 并按定义自算 JA3／JA4，
// 期望值不变，不再依赖外网。
//
// Run with: go test -v -tags=integration ./internal/pkg/tlsfingerprint/...
package tlsfingerprint

import (
	"strings"
	"testing"
)

// TestJA3Fingerprint verifies the JA3/JA4 fingerprint matches expected value.
// 指纹由本地捕获服务端按 ClientHello 原始字节自算（原先依赖 tls.peet.ws，期望值相同）。
// Expected JA3 hash: 44f88fca027f27bab4bb08d4af15f23e (Node.js 24.x)
// Expected JA4: t13d1714h1_5b57614c22b0_7baf387fc6ff
func TestJA3Fingerprint(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping integration test in short mode")
	}

	fp := startLocalFingerprintServer(t).fetch(t, &Profile{
		Name:         "Default Profile Test",
		EnableGREASE: false,
	})

	t.Logf("JA3: %s", fp.JA3)
	t.Logf("JA3 Hash: %s", fp.JA3Hash)
	t.Logf("JA4: %s", fp.JA4)

	expectedJA3Hash := "44f88fca027f27bab4bb08d4af15f23e"
	if fp.JA3Hash == expectedJA3Hash {
		t.Logf("✓ JA3 hash matches: %s", expectedJA3Hash)
	} else {
		t.Errorf("✗ JA3 hash mismatch: got %s, expected %s", fp.JA3Hash, expectedJA3Hash)
	}

	expectedJA4CipherHash := "_5b57614c22b0_"
	if strings.Contains(fp.JA4, expectedJA4CipherHash) {
		t.Logf("✓ JA4 cipher hash matches: %s", expectedJA4CipherHash)
	} else {
		t.Errorf("✗ JA4 cipher hash mismatch: got %s, expected containing %s", fp.JA4, expectedJA4CipherHash)
	}

	// 文档注释里记录的完整 JA4；原先只比对中间的密码套件哈希，本地复现后一并比对整串。
	expectedJA4 := "t13d1714h1_5b57614c22b0_7baf387fc6ff"
	if fp.JA4 != expectedJA4 {
		t.Errorf("✗ JA4 mismatch: got %s, expected %s", fp.JA4, expectedJA4)
	}
}

// TestAllProfiles tests multiple TLS fingerprint profiles against the local capture server.
// Run with: go test -v -tags=integration -run TestAllProfiles ./internal/pkg/tlsfingerprint/...
func TestAllProfiles(t *testing.T) {
	if testing.Short() {
		t.Skip("skipping integration test in short mode")
	}

	// Define all profiles to test with their expected fingerprints
	// These profiles are from config.yaml gateway.tls_fingerprint.profiles
	profiles := []TestProfileExpectation{
		{
			// Default profile (Node.js 24.x)
			Profile: &Profile{
				Name:         "default_node_v24",
				EnableGREASE: false,
			},
			JA4CipherHash: "5b57614c22b0",
		},
		{
			// Linux x64 Node.js v22.17.1 (explicit profile with v22 extensions)
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

// fetchFingerprint 向本地捕获服务端发一次请求并返回它按 ClientHello 算出的 TLS 指纹。
func fetchFingerprint(t *testing.T, profile *Profile) *TLSInfo {
	t.Helper()
	return startLocalFingerprintServer(t).fetch(t, profile)
}
