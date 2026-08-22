package officialegress

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const (
	claudeFWGModelCapabilityTransitionSHA256 = "f65639b6805a9bc43344bfaa7c912d6a7522df8db15510e6f35ef4aa786afb8f"
	claudeFWGModelCapabilityCatalogSHA256    = "d34ca049ec851b220f06a4701c951a8be270d4a498b8012229c7f62cb8183df1"
	claudeFWGModelCapabilityCampaignSHA256   = "28639c21b70fbd0a7a0b6f8db804d0b529f4aa7ae91ef6c0615c03251e76a0a0"
	claudeFWGModelCapabilityProductionSHA256 = "fd2abe74748c41a4667bb8761d92d8f30c63d8322a6982162643c7ac7733a7c6"
)

type claudeFWGModelCapabilityAttemptRef struct {
	AttemptID            string `json:"attempt_id"`
	Status               string `json:"status"`
	ManifestSHA256       string `json:"manifest_sha256"`
	RuntimeReceiptSHA256 string `json:"runtime_receipt_sha256"`
}

type claudeFWGModelCapabilityTransitionReceipt struct {
	SchemaVersion string                              `json:"schema_version"`
	Date          string                              `json:"date"`
	Phase         string                              `json:"phase"`
	BaseCommit    string                              `json:"base_commit"`
	Prior         []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	Target        struct {
		Product  string   `json:"product"`
		Version  string   `json:"version"`
		Platform string   `json:"platform"`
		Models   []string `json:"models"`
	} `json:"target"`
	Invariants struct {
		RequiredRulesBefore    int    `json:"required_rule_count_before"`
		RequiredRulesAfter     int    `json:"required_rule_count_after"`
		RulesDuplicatedByModel bool   `json:"required_rules_duplicated_per_model"`
		UnknownModelPolicy     string `json:"unknown_model_policy"`
		AliasPolicy            string `json:"alias_policy"`
		TelemetryDisposition   string `json:"telemetry_and_behavior_traffic"`
	} `json:"invariants"`
	Evidence struct {
		CampaignRoot             string                               `json:"campaign_root"`
		SuccessfulAttempts       int                                  `json:"successful_attempts"`
		HistoricalFailedAttempts int                                  `json:"historical_failed_attempts"`
		Attempts                 []claudeFWGModelCapabilityAttemptRef `json:"attempts"`
		ProductionDiff           struct {
			Path      string `json:"path"`
			SHA256    string `json:"sha256"`
			Identical bool   `json:"identical"`
		} `json:"production_diff"`
		BehaviorTrafficDisposition string `json:"behavior_traffic_disposition"`
		CampaignSHA256             string `json:"campaign_sha256"`
	} `json:"evidence"`
	ModelCapabilityCatalogSHA256 string `json:"model_capability_catalog_sha256"`
	Artifacts                    struct {
		Before struct {
			ProfileSHA256 string `json:"profile_sha256"`
			WireSHA256    string `json:"wire_sha256"`
			ReleaseSHA256 string `json:"release_sha256"`
			BundleSHA256  string `json:"bundle_sha256"`
		} `json:"before"`
		After struct {
			Profile       string `json:"profile"`
			ProfileSHA256 string `json:"profile_sha256"`
			Wire          string `json:"wire"`
			WireSHA256    string `json:"wire_sha256"`
			ReleaseSHA256 string `json:"release_sha256"`
			BundleSHA256  string `json:"bundle_sha256"`
		} `json:"after"`
	} `json:"artifacts"`
	Transitions []changeset4SourceTransitionEntry `json:"transitions"`
	Safety      struct {
		VircsProductionChanged    bool `json:"vircs_production_changed"`
		ProductionSelectorChanged bool `json:"production_selector_changed"`
		DMITDeployed              bool `json:"dmit_deployed"`
	} `json:"safety"`
	Result string `json:"result"`
}

func loadClaudeFWGModelCapabilityTransition() (
	claudeFWGModelCapabilityTransitionReceipt,
	error,
) {
	var receipt claudeFWGModelCapabilityTransitionReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-model-capability-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGModelCapabilityTransitionSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-model-capability-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "c662b59a3e558f7aa352cf3a424a603aa0c254eb" ||
		receipt.Result != "passed" || len(receipt.Prior) != 2 ||
		len(receipt.Transitions) != 16 || receipt.Safety.VircsProductionChanged ||
		receipt.Safety.ProductionSelectorChanged || receipt.Safety.DMITDeployed {
		return receipt, errors.New("Claude 模型能力 transition 顶层事实非法")
	}
	if err := validateClaudeFWGModelCapabilityIdentity(receipt); err != nil {
		return receipt, err
	}
	if err := validateClaudeFWGModelCapabilityEvidence(receipt); err != nil {
		return receipt, err
	}
	if err := validateClaudeFWGModelCapabilityTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateClaudeFWGModelCapabilityIdentity(
	receipt claudeFWGModelCapabilityTransitionReceipt,
) error {
	wantPrior := map[string]string{
		"docs/egress/maintenance/claude-fw-g-desktop-title-source-transition.json": claudeFWGDesktopTitleSourceSHA256,
		"docs/egress/maintenance/claude-fw-g-desktop-title-test-transition.json":   claudeFWGDesktopTitleTestSHA256,
	}
	for _, prior := range receipt.Prior {
		if wantPrior[prior.Path] != prior.SHA256 {
			return errors.New("Claude 模型能力 prior transition 引用非法")
		}
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(raw) != prior.SHA256 {
			return errors.New("Claude 模型能力 prior transition 摘要不一致")
		}
		delete(wantPrior, prior.Path)
	}
	if len(wantPrior) != 0 || receipt.Target.Product != "claude-code" ||
		receipt.Target.Version != ClaudeFWGVersion || receipt.Target.Platform != "linux/amd64" ||
		!slices.Equal(receipt.Target.Models, []string{
			"claude-sonnet-5", "claude-opus-5", "claude-fable-5",
		}) {
		return errors.New("Claude 模型能力目标身份非法")
	}
	invariants := receipt.Invariants
	if invariants.RequiredRulesBefore != 40 || invariants.RequiredRulesAfter != 40 ||
		invariants.RulesDuplicatedByModel || invariants.UnknownModelPolicy != "deny" ||
		invariants.AliasPolicy != "explicit-only" ||
		invariants.TelemetryDisposition != "excluded" ||
		receipt.ModelCapabilityCatalogSHA256 != claudeFWGModelCapabilityCatalogSHA256 {
		return errors.New("Claude 模型能力不变量非法")
	}
	before, after := receipt.Artifacts.Before, receipt.Artifacts.After
	if before.ProfileSHA256 != claudeFWGInitialProfileDigest ||
		before.WireSHA256 != claudeFWGThinkingDisplayWireSHA256 ||
		before.ReleaseSHA256 != claudeFWGInitialReleaseDigest ||
		before.BundleSHA256 != claudeFWGInitialBundleDigest ||
		after.ProfileSHA256 != ClaudeFWGProfileDigest ||
		after.WireSHA256 != claudeFWGWireDigest ||
		after.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		after.BundleSHA256 != ClaudeFWGBundleDigest {
		return errors.New("Claude 模型能力制品迁移身份非法")
	}
	for path, digest := range map[string]string{
		after.Profile: after.ProfileSHA256,
		after.Wire:    after.WireSHA256,
	} {
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
		if err != nil || claudeFWGCountTokensDigest(raw) != digest {
			return errors.New("Claude 模型能力内容寻址制品摘要不一致")
		}
	}
	return nil
}

func validateClaudeFWGModelCapabilityEvidence(
	receipt claudeFWGModelCapabilityTransitionReceipt,
) error {
	evidence := receipt.Evidence
	if evidence.CampaignRoot !=
		"local-analysis/fw-f/claude-code-2.1.226/model-capability-v1-20260821" ||
		evidence.SuccessfulAttempts != 42 || evidence.HistoricalFailedAttempts != 3 ||
		len(evidence.Attempts) != 45 ||
		evidence.BehaviorTrafficDisposition !=
			"excluded-from-rules-and-difference-judgement" ||
		evidence.CampaignSHA256 != claudeFWGModelCapabilityCampaignSHA256 ||
		!evidence.ProductionDiff.Identical ||
		evidence.ProductionDiff.SHA256 != claudeFWGModelCapabilityProductionSHA256 {
		return errors.New("Claude 模型能力 Campaign 事实非法")
	}
	attemptIDs := make([]string, 0, len(evidence.Attempts))
	failed := map[string]struct{}{
		"claude-opus-5-v4-replay-baseline/attempt-001": {},
		"claude-opus-5-v4-replay-tui/attempt-001":      {},
		"claude-opus-5-v4-replay-tui/attempt-002":      {},
	}
	completeCount := 0
	failedCount := 0
	for _, attempt := range evidence.Attempts {
		scenario, ordinal, ok := strings.Cut(attempt.AttemptID, "/")
		if !ok || scenario == "" || ordinal == "" {
			return errors.New("Claude 模型能力 attempt 身份非法")
		}
		attemptIDs = append(attemptIDs, attempt.AttemptID)
		manifestPath := filepath.Join(
			"../../..", filepath.FromSlash(evidence.CampaignRoot),
			"attempts", scenario, ordinal, "relay-manifest.json",
		)
		manifest, err := os.ReadFile(manifestPath)
		if err != nil || claudeFWGCountTokensDigest(manifest) != attempt.ManifestSHA256 {
			return errors.New("Claude 模型能力 attempt manifest 摘要不一致")
		}
		switch attempt.Status {
		case "complete":
			if _, historicalFailure := failed[attempt.AttemptID]; historicalFailure ||
				attempt.RuntimeReceiptSHA256 == "" {
				return errors.New("Claude 模型能力成功 attempt 状态非法")
			}
			runtimePath := filepath.Join(
				"../../..", filepath.FromSlash(evidence.CampaignRoot), "runtime-receipts",
				scenario+"-"+ordinal+".json",
			)
			runtimeReceipt, readErr := os.ReadFile(runtimePath)
			if readErr != nil ||
				claudeFWGCountTokensDigest(runtimeReceipt) != attempt.RuntimeReceiptSHA256 {
				return errors.New("Claude 模型能力 runtime receipt 摘要不一致")
			}
			completeCount++
		case "historical_failed":
			if _, expected := failed[attempt.AttemptID]; !expected ||
				attempt.RuntimeReceiptSHA256 != "" {
				return errors.New("Claude 模型能力历史失败 attempt 状态非法")
			}
			delete(failed, attempt.AttemptID)
			failedCount++
		default:
			return errors.New("Claude 模型能力 attempt 含未知状态")
		}
	}
	if len(attemptIDs) != len(slices.Compact(attemptIDs)) ||
		completeCount != 42 || failedCount != 3 || len(failed) != 0 {
		return errors.New("Claude 模型能力 attempt 集合不闭合")
	}
	productionRaw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(evidence.ProductionDiff.Path),
	))
	if err != nil || claudeFWGCountTokensDigest(productionRaw) !=
		evidence.ProductionDiff.SHA256 {
		return errors.New("Claude 模型能力生产零差异证明摘要不一致")
	}
	var production struct {
		Identical     bool              `json:"identical"`
		ChangedFields []json.RawMessage `json:"changed_fields"`
	}
	if json.Unmarshal(productionRaw, &production) != nil || !production.Identical ||
		len(production.ChangedFields) != 0 {
		return errors.New("Claude 模型能力 Campaign 改变了 Vircs 生产状态")
	}
	return nil
}

func validateClaudeFWGModelCapabilityTransitions(
	transitions []changeset4SourceTransitionEntry,
) error {
	wantBefore := map[string]string{
		"backend/internal/officialegress/catalogdata/claude/profiles/2.1.226/e02a3af6fa56cf09b6525d884c9de3f7b76ffe84eb000d92606681b0085b9ab5.json": EMPTYFileSHA256ForTest,
		"backend/internal/officialegress/catalogdata/claude/wire/2.1.226/a7d2c91fc5c4b43bd49f93b60d0d681e487db0e1cdb25d3096e703cb85587c4d.json":     EMPTYFileSHA256ForTest,
		"backend/internal/officialegress/claude_body.go":                                  "ba45723ed345a30cbd358ea9d0c23fe065df410a921fc3c69aa3eb6d5dd846b7",
		"backend/internal/officialegress/claude_fw_g_desktop_title_transition_test.go":    "4916d31469ac694b3b6c957551b474adb2206da2db361a3b74bb6dc91931afdb",
		"backend/internal/officialegress/claude_fw_g_source_transition_test.go":           "e943503961ebaf1e890410fcd61a8745f205b09668b9b807d5d7ec5404961fae",
		"backend/internal/officialegress/claude_fw_g_thinking_display_transition_test.go": "e3abae6d0a1ff445adb531618653b8f0c84e53de7c07705531ee8cfaf5719bd6",
		"backend/internal/officialegress/claude_ingress.go":                               "0920335717e3d646bc4d54bda997ea8c81a23bd3fd056d6258b442824abd0204",
		"backend/internal/officialegress/claude_profile.go":                               "6c347093c3b722b6b9339fc9f497b279f5d9839d6dc99212313933fe0b5756b1",
		"backend/internal/officialegress/claude_runtime.go":                               "5b24eec2cedd2cdd44e9045933261b62dfac2f5d9cc64d6c057ff0024af9167c",
		"backend/internal/officialegress/claude_runtime_test.go":                          "eb8945aec040a5c7107b86138bac48aac467d19fdf453a6105770e46132d8e48",
		"backend/internal/officialegress/claude_tools.go":                                 "e49b12302ff469ec5d446f9d184466a25c65c8b7b3e1db8cf793b1bfc63d7b79",
		"backend/internal/officialegress/claude_wire.go":                                  "9558669b8f9c546116d1942424405d18e9a665b1fd6967e77c35ba4cb6fa14d8",
		"backend/internal/service/claude_fw_g_source_transition_test.go":                  "9ef2cc4a97c023f6ad4921e2664d823ffcfc2435442b18a9673529d136e93893",
		"backend/internal/service/gateway_claude_fw_g.go":                                 "750ecc54194ba13a01bdf42a0a962fc6b76195cca3c00179cf369017407d0e6d",
		"docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md":                                      "4cc9b2acbbc50203cab78eeeedaf0d4f46aaa4500d082e27fdf5a8e9b937b31b",
		"tools/generate_claude_fw_g_profile.py":                                           "871e74ac3e0466d26977e9bb44256ec2e4d913b7e87895c7a307c4c5926420e4",
	}
	paths := make([]string, 0, len(transitions))
	for _, transition := range transitions {
		before, exists := wantBefore[transition.Path]
		if !exists || transition.FromSHA256 != before || transition.ToSHA256 == before ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude 模型能力源码 transition 条目非法")
		}
		raw, err := os.ReadFile(
			filepath.Join("../../..", filepath.FromSlash(transition.Path)),
		)
		currentDigest := claudeFWGCountTokensDigest(raw)
		if err != nil || currentDigest != transition.ToSHA256 &&
			!claudeFWHStateDurabilityTransitionSupersedes(
				transition.Path, transition.ToSHA256, currentDigest,
			) &&
			!claudeFWGThreeModelAcceptanceAttemptSupersedes(
				transition.Path, transition.ToSHA256, currentDigest,
			) && !claudeFWGFableDesktopTitleTransitionSupersedes(
			transition.Path, transition.ToSHA256, currentDigest,
		) && !claudeFWGThreeModelAcceptanceSupersedes(
			transition.Path, transition.ToSHA256, currentDigest,
		) && !claudeFWHFableDeclaredFallbackTransitionSupersedes(
			transition.Path, transition.ToSHA256, currentDigest,
		) {
			return errors.New("Claude 模型能力源码 transition 当前摘要不一致")
		}
		paths = append(paths, transition.Path)
		delete(wantBefore, transition.Path)
	}
	if len(wantBefore) != 0 || !slices.IsSorted(paths) ||
		len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude 模型能力源码 transition 集合不闭合")
	}
	return nil
}

const EMPTYFileSHA256ForTest = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

func claudeFWGModelCapabilityTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHStateDurabilityTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHFableDeclaredFallbackTransitionSupersedes(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if claudeFWHLegacyRetirementTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHProductionAcceptanceSupersedes(
		path, priorDigest, currentDigest,
	) || claudeFWHSourceTransitionSupersedes(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if claudeFWGFableDesktopTitleTransitionSupersedes(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	receipt, err := loadClaudeFWGModelCapabilityTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		currentReached := transition.ToSHA256 == currentDigest ||
			claudeFWHFableDeclaredFallbackTransitionSupersedes(
				path, transition.ToSHA256, currentDigest,
			) ||
			claudeFWHLegacyRetirementTransitionSupersedes(
				path, transition.ToSHA256, currentDigest,
			) ||
			claudeFWGThreeModelAcceptanceAttemptSupersedes(
				path, transition.ToSHA256, currentDigest,
			) || claudeFWGFableDesktopTitleTransitionSupersedes(
			path, transition.ToSHA256, currentDigest,
		) || claudeFWGThreeModelAcceptanceSupersedes(
			path, transition.ToSHA256, currentDigest,
		)
		if transition.Path == path && currentReached &&
			(transition.FromSHA256 == priorDigest ||
				claudeFWGDesktopTitleTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) ||
				claudeFWGThinkingDisplayTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				) ||
				claudeFWGDesktopCompatibilityTransitionSupersedes(
					path, priorDigest, transition.FromSHA256,
				)) {
			return true
		}
	}
	return false
}

func TestClaudeFWGModelCapabilityTransitionIsFrozen(t *testing.T) {
	if _, err := loadClaudeFWGModelCapabilityTransition(); err != nil {
		t.Fatal(err)
	}
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	if len(profile.document.Rules) != 40 ||
		profile.document.Identity.ModelCapabilityCatalogSHA256 !=
			wire.Identity.ModelCapabilityCatalogDigest ||
		wire.Identity.ModelCapabilityCatalogDigest !=
			claudeFWGModelCapabilityCatalogSHA256 {
		t.Fatal("Claude 公共规则与模型能力目录未形成独立内容寻址绑定")
	}
}
