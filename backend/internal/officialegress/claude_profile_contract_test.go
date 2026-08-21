package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"slices"
	"testing"
)

const claudeFWGRequiredRulesManifestSHA256 = "c09fdc9158d4d3aee7e5d28cc4a794e259a3c56899e00d558afab4c537389045"

// TestClaudeFWGProfileMatchesRequiredRulesManifest 固定 FW-F 批准的 40 条
// RequiredRules 与 106 条画像原子断言，防止只维持数量却替换集合成员。
func TestClaudeFWGProfileMatchesRequiredRulesManifest(t *testing.T) {
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile("../../../tools/official_client_capture/claude_required_rules_2_1_226.json")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if got := hex.EncodeToString(sum[:]); got != claudeFWGRequiredRulesManifestSHA256 {
		t.Fatalf("Claude RequiredRules manifest 摘要漂移：got=%s want=%s", got, claudeFWGRequiredRulesManifestSHA256)
	}
	var manifest struct {
		TargetVersion               string `json:"target_version"`
		RequiredRuleCount           int    `json:"required_rule_count"`
		ProfileAtomicAssertionCount int    `json:"profile_atomic_assertion_count"`
		RequiredRules               []struct {
			SpecID             string   `json:"spec_id"`
			AtomicAssertionIDs []string `json:"atomic_assertion_ids"`
		} `json:"required_rules"`
	}
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatal(err)
	}
	if manifest.TargetVersion != ClaudeFWGVersion || manifest.RequiredRuleCount != 40 ||
		manifest.ProfileAtomicAssertionCount != 106 || len(manifest.RequiredRules) != 40 {
		t.Fatalf("Claude RequiredRules manifest 顶层事实非法：%+v", manifest)
	}
	manifestRules := make(map[string][]string, len(manifest.RequiredRules))
	for _, rule := range manifest.RequiredRules {
		if _, duplicate := manifestRules[rule.SpecID]; duplicate {
			t.Fatalf("Claude RequiredRules manifest 规则重复：%s", rule.SpecID)
		}
		manifestRules[rule.SpecID] = append([]string(nil), rule.AtomicAssertionIDs...)
	}
	if len(manifestRules) != len(profile.rules) {
		t.Fatalf("Claude RequiredRules 集合数量不一致：manifest=%d profile=%d", len(manifestRules), len(profile.rules))
	}
	for specID, rule := range profile.rules {
		atomicIDs, ok := manifestRules[specID]
		if !ok || !slices.Equal(atomicIDs, rule.AtomicAssertionIDs) {
			t.Fatalf("Claude RequiredRule 或原子映射不一致：%s manifest=%v profile=%v", specID, atomicIDs, rule.AtomicAssertionIDs)
		}
	}
}
