package service

import (
	"strings"
	"testing"
	"time"
)

// 契约单测：锁定 settings 键、配置解析与源指纹的基本语义，两路实现都依赖这些行为。

func TestCodexThirdPartyContractSettingKeys(t *testing.T) {
	if got := CodexModelVisibilitySettingKey(8); got != "plus_codex_model_visibility:8" {
		t.Fatalf("visibility key = %q", got)
	}
	if got := CodexCatalogSourceSettingKey(106); got != "plus_codex_catalog_source:106" {
		t.Fatalf("source key = %q", got)
	}
	// settings 键最长 100 字符，int64 最大值也不能超限。
	if n := len(CodexModelVisibilitySettingKey(9223372036854775807)); n > 100 {
		t.Fatalf("visibility key too long: %d", n)
	}
}

func TestCodexThirdPartyContractPlatformEligible(t *testing.T) {
	for _, platform := range []string{PlatformComposite, PlatformOpenAI, PlatformKimi, PlatformZhipu, PlatformDeepseek} {
		if !CodexCatalogPlatformEligible(platform) {
			t.Fatalf("platform %q should be eligible", platform)
		}
	}
	for _, platform := range []string{PlatformAnthropic, PlatformGemini, PlatformAntigravity, ""} {
		if CodexCatalogPlatformEligible(platform) {
			t.Fatalf("platform %q should not be eligible", platform)
		}
	}
}

func TestCodexModelVisibilityConfigRoundTrip(t *testing.T) {
	cfg := NewCodexModelVisibilityConfig()
	cfg.Revision = 3
	cfg.Models["kimi-k3"] = CodexModelVisibilityEntry{Enabled: true}
	cfg.Models["glm-5.3"] = CodexModelVisibilityEntry{
		Enabled:      true,
		Verification: &CodexModelVerification{Fingerprint: "abc", VerifiedAt: time.Unix(0, 0).UTC()},
	}
	cfg.Models["deepseek-v4-pro"] = CodexModelVisibilityEntry{Enabled: false}
	raw, err := cfg.Marshal()
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	parsed, err := ParseCodexModelVisibilityConfig(raw)
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if parsed.Revision != 3 || !parsed.IsEnabled("kimi-k3") || !parsed.IsEnabled("glm-5.3") {
		t.Fatalf("unexpected parsed config: %+v", parsed)
	}
	if parsed.IsEnabled("deepseek-v4-pro") || parsed.IsEnabled("deepseek-flash") {
		t.Fatalf("disabled or unlisted model must be off")
	}
	clone := parsed.Clone()
	clone.Models["glm-5.3"].Verification.Fingerprint = "changed"
	if parsed.Models["glm-5.3"].Verification.Fingerprint != "abc" {
		t.Fatalf("clone must deep copy verification")
	}
}

func TestCodexModelVisibilityConfigRejectsInvalid(t *testing.T) {
	cases := map[string]string{
		"empty":          "",
		"bad json":       "{",
		"unknown schema": `{"schema_version":2,"revision":1,"models":{}}`,
		"wildcard slug":  `{"schema_version":1,"revision":1,"models":{"kimi-*":{"enabled":true}}}`,
		"blank slug":     `{"schema_version":1,"revision":1,"models":{" ":{"enabled":true}}}`,
		"padded slug":    `{"schema_version":1,"revision":1,"models":{" kimi-k3":{"enabled":true}}}`,
		"empty verify":   `{"schema_version":1,"revision":1,"models":{"kimi-k3":{"enabled":true,"verification":{"fingerprint":""}}}}`,
	}
	for name, raw := range cases {
		if _, err := ParseCodexModelVisibilityConfig(raw); err == nil {
			t.Fatalf("%s: expected error", name)
		}
	}
	long := NewCodexModelVisibilityConfig()
	long.Models[strings.Repeat("m", codexModelSlugMaxLength+1)] = CodexModelVisibilityEntry{Enabled: true}
	if err := long.Validate(); err == nil {
		t.Fatalf("over-long slug must be rejected")
	}
}

func TestCodexCatalogSourceFingerprint(t *testing.T) {
	account := &Account{
		ID:       106,
		Platform: PlatformDeepseek,
		Type:     "apikey",
		Credentials: map[string]any{
			"base_url":      "https://example.invalid/v1",
			"api_key":       "sk-secret",
			"model_mapping": map[string]any{"deepseek-flash": "deepseek-flash"},
		},
	}
	first := CodexCatalogSourceFingerprint(account)
	if first == "" || strings.Contains(first, "sk-secret") {
		t.Fatalf("fingerprint must be non-empty and must not leak credentials: %q", first)
	}
	if again := CodexCatalogSourceFingerprint(account); again != first {
		t.Fatalf("fingerprint must be stable")
	}
	account.Credentials["access_token"] = "rotating"
	if got := CodexCatalogSourceFingerprint(account); got != first {
		t.Fatalf("rotating tokens must not change fingerprint")
	}
	account.Credentials["base_url"] = "https://other.invalid/v1"
	if got := CodexCatalogSourceFingerprint(account); got == first {
		t.Fatalf("base_url change must change fingerprint")
	}
	snap := CodexCatalogSourceSnapshot{AccountID: 106, SourceFingerprint: first}
	if snap.Matches(account) {
		t.Fatalf("snapshot must not match after source change")
	}
}

func TestCodexCatalogSourceSnapshotStaleAndNormalize(t *testing.T) {
	now := time.Date(2026, 10, 23, 12, 0, 0, 0, time.UTC)
	fresh := CodexCatalogSourceSnapshot{Status: CodexCatalogSourceComplete, SyncedAt: now.Add(-time.Hour)}
	if fresh.Stale(now) {
		t.Fatalf("one hour old snapshot should be fresh")
	}
	old := CodexCatalogSourceSnapshot{Status: CodexCatalogSourceComplete, SyncedAt: now.Add(-7 * time.Hour)}
	failed := CodexCatalogSourceSnapshot{Status: CodexCatalogSourceFailed, SyncedAt: now}
	if !old.Stale(now) || !failed.Stale(now) || !(CodexCatalogSourceSnapshot{}).Stale(now) {
		t.Fatalf("old, failed and empty snapshots must be stale")
	}
	got := NormalizeCodexCatalogSourceModels([]string{" kimi-k3 ", "glm-5.3", "kimi-k3", "", "gpt-*"})
	if strings.Join(got, ",") != "glm-5.3,kimi-k3" {
		t.Fatalf("normalize = %v", got)
	}
}
