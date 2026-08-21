package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const (
	claudeFWGFableDesktopTitleSourceSHA256 = "d03add436e6e970bbaca3947d7fdf9a50a43879f78a3f89e49bcf1534d28bc56"
	claudeFWGFableDesktopTitleTestSHA256   = "089b0f1c13f1e2a34aa982f8e2220c4a8c4149df1746d932ac14daa40babdb61"
)

type claudeFWGFableDesktopTitleSourceReceipt struct {
	SchemaVersion string                              `json:"schema_version"`
	Date          string                              `json:"date"`
	Phase         string                              `json:"phase"`
	BaseCommit    string                              `json:"base_commit"`
	Trigger       string                              `json:"trigger"`
	Prior         []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	Evidence      struct {
		CapturedAtUTC      string   `json:"captured_at_utc"`
		DesktopVersion     string   `json:"desktop_version"`
		ClientPlatform     string   `json:"client_platform"`
		Authentication     string   `json:"authentication"`
		Privacy            string   `json:"privacy"`
		ClientHost         string   `json:"client_host"`
		CaptureHost        string   `json:"capture_host"`
		CaptureMethod      string   `json:"capture_method"`
		DMITAccountID      int64    `json:"dmit_account_id"`
		UserAgent          string   `json:"user_agent"`
		RequestMethod      string   `json:"request_method"`
		RequestTarget      string   `json:"request_target"`
		RequestBodyBytes   int      `json:"request_body_bytes"`
		RequestBodySHA256  string   `json:"request_body_sha256"`
		PcapBytes          int      `json:"pcap_bytes"`
		PcapSHA256         string   `json:"pcap_sha256"`
		TopLevelOrder      []string `json:"top_level_order"`
		SourceModel        string   `json:"source_model"`
		SourceMaxTokens    int      `json:"source_max_tokens"`
		ThinkingPresent    bool     `json:"source_thinking_present"`
		SourceEffort       string   `json:"source_effort"`
		SourceStream       bool     `json:"source_stream"`
		SourceToolCount    int      `json:"source_tool_count"`
		SystemBlockCount   int      `json:"source_system_block_count"`
		BillingSHA256      string   `json:"billing_header_sha256"`
		AgentSDKSHA256     string   `json:"agent_sdk_system_sha256"`
		TitlePromptSHA256  string   `json:"title_prompt_sha256"`
		TitleFormatSHA256  string   `json:"title_format_sha256"`
		RawCaptureRetained bool     `json:"raw_capture_retained"`
	} `json:"official_evidence"`
	PersonaMapping struct {
		SourceSemantics           string `json:"source_semantics"`
		SourceThinkingDisposition string `json:"source_thinking_disposition"`
		TargetVersion             string `json:"target_version"`
		TargetEntrypoint          string `json:"target_entrypoint"`
		TargetScenario            string `json:"target_scenario"`
		TargetModel               string `json:"target_model"`
		TargetMaxTokens           int    `json:"target_max_tokens"`
		TargetThinking            string `json:"target_thinking"`
		TargetTemperature         int    `json:"target_temperature"`
		TargetToolCount           int    `json:"target_tool_count"`
		WireSHA256                string `json:"wire_sha256"`
	} `json:"persona_mapping"`
	Dispositions []struct {
		Finding string `json:"finding"`
		Result  string `json:"result"`
	} `json:"dispositions"`
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	RequiredRules             int                               `json:"required_rule_count"`
	AtomicAssertions          int                               `json:"atomic_assertion_count"`
	ProfileChanged            bool                              `json:"profile_changed"`
	WireChanged               bool                              `json:"wire_changed"`
	ReleaseBundleChanged      bool                              `json:"release_bundle_changed"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	VircsServiceChanged       bool                              `json:"vircs_service_changed"`
	DMITCandidateRebuild      bool                              `json:"dmit_candidate_rebuild_required"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	Result                    string                            `json:"result"`
}

type claudeFWGFableDesktopTitleTestReceipt struct {
	SchemaVersion             string                            `json:"schema_version"`
	Date                      string                            `json:"date"`
	Phase                     string                            `json:"phase"`
	BaseCommit                string                            `json:"base_commit"`
	Source                    claudeFWGCountTokensTransitionRef `json:"source_transition"`
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	TargetedTests             []string                          `json:"targeted_tests"`
	NegativeCases             []string                          `json:"negative_cases"`
	RequiredRules             int                               `json:"required_rule_count"`
	AtomicAssertions          int                               `json:"atomic_assertion_count"`
	WireSHA256                string                            `json:"wire_sha256"`
	ProfileChanged            bool                              `json:"profile_changed"`
	WireChanged               bool                              `json:"wire_changed"`
	ReleaseBundleChanged      bool                              `json:"release_bundle_changed"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	VircsServiceChanged       bool                              `json:"vircs_service_changed"`
	DMITCandidateRebuild      bool                              `json:"dmit_candidate_rebuild_required"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	Result                    string                            `json:"result"`
}

func loadClaudeFWGFableDesktopTitleSourceReceipt() (
	claudeFWGFableDesktopTitleSourceReceipt,
	error,
) {
	var receipt claudeFWGFableDesktopTitleSourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-fable-desktop-title-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGFableDesktopTitleSourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-fable-desktop-title-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "986cb374eefc8122399dbdd26d21950f38183daf" ||
		strings.TrimSpace(receipt.Trigger) == "" || len(receipt.Prior) != 2 ||
		len(receipt.Dispositions) != 4 || len(receipt.Transitions) != 1 ||
		receipt.RequiredRules != 40 || receipt.AtomicAssertions != 110 ||
		receipt.ProfileChanged || receipt.WireChanged || receipt.ReleaseBundleChanged ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G Fable Desktop 标题 source transition 顶层事实非法")
	}
	if err := validateClaudeFWGFableDesktopTitlePriors(receipt.Prior); err != nil {
		return receipt, err
	}
	if err := validateClaudeFWGFableDesktopTitleEvidence(receipt); err != nil {
		return receipt, err
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	transition := receipt.Transitions[0]
	if transition.Path != "backend/internal/officialegress/claude_body.go" ||
		transition.FromSHA256 != "0bbd57f2f656f1fb6a7ebb02031466d99f87a2775c44eb3aaee9af1a9b41cff9" ||
		transition.ToSHA256 != "f5630f1a1907d54eb8b5f5dfff111e55a7a6c07fcb18779856f17844e0939f3a" {
		return receipt, errors.New("Claude FW-G Fable Desktop 标题源码 transition 非法")
	}
	return receipt, nil
}

func validateClaudeFWGFableDesktopTitlePriors(
	priors []claudeFWGCountTokensTransitionRef,
) error {
	expected := map[string]string{
		"docs/egress/maintenance/claude-fw-g-model-capability-source-transition.json": claudeFWGModelCapabilityTransitionSHA256,
		"docs/egress/maintenance/claude-fw-g-three-model-acceptance-attempt.json":     claudeFWGThreeModelAcceptanceAttemptSHA256,
	}
	for _, prior := range priors {
		if expected[prior.Path] != prior.SHA256 {
			return errors.New("Claude FW-G Fable Desktop 标题 prior transition 非法")
		}
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(raw) != prior.SHA256 {
			return errors.New("Claude FW-G Fable Desktop 标题 prior transition 摘要不一致")
		}
		delete(expected, prior.Path)
	}
	if len(expected) != 0 {
		return errors.New("Claude FW-G Fable Desktop 标题 prior transition 缺失")
	}
	return nil
}

func validateClaudeFWGFableDesktopTitleEvidence(
	receipt claudeFWGFableDesktopTitleSourceReceipt,
) error {
	evidence := receipt.Evidence
	if evidence.CapturedAtUTC != "2026-08-21T12:25:06.096471Z" ||
		evidence.DesktopVersion != "2.1.237" || evidence.ClientPlatform != "darwin/arm64" ||
		evidence.Authentication != "claude.ai-oauth" ||
		evidence.Privacy != "essential-traffic-no-telemetry" ||
		evidence.ClientHost != "local-mac" || evidence.CaptureHost != "DMIT" ||
		strings.TrimSpace(evidence.CaptureMethod) == "" || evidence.DMITAccountID != 101 ||
		evidence.UserAgent !=
			"claude-cli/2.1.237 (external, claude-desktop-3p, agent-sdk/0.3.237)" ||
		evidence.RequestMethod != "POST" || evidence.RequestTarget != "/v1/messages?beta=true" ||
		evidence.RequestBodyBytes != 4086 ||
		evidence.RequestBodySHA256 !=
			"d08ba67ede8b45a31f8679a28fe49c89714698488a763641868216c39797eddd" ||
		evidence.PcapBytes != 328002 ||
		evidence.PcapSHA256 !=
			"59eec107c521f472fffdab9535f6117921a5ce6dee2a002e5d373a3818218011" ||
		!slices.Equal(evidence.TopLevelOrder, []string{
			"model", "messages", "system", "tools", "metadata", "max_tokens",
			"output_config", "stream",
		}) || evidence.SourceModel != "claude-fable-5" || evidence.SourceMaxTokens != 64000 ||
		evidence.ThinkingPresent || evidence.SourceEffort != "high" || !evidence.SourceStream ||
		evidence.SourceToolCount != 0 || evidence.SystemBlockCount != 3 ||
		evidence.BillingSHA256 !=
			"983e994d34502cd7b3c9ca01c3bba8cc10d84e7d64ed54a6eb9a207be11a6e6e" ||
		evidence.AgentSDKSHA256 !=
			"0d7062851dd7bd7e66d4be4f12ac4951e3d2f587ec408295333a49963bd3f6b7" ||
		evidence.TitlePromptSHA256 != claudeDesktopTitlePromptSHA256 ||
		evidence.TitleFormatSHA256 !=
			"6a3dca9cf59b8442c703d56464855f8777da4c2e6f5d31c7a82dd5f16494c4f9" ||
		evidence.RawCaptureRetained {
		return errors.New("Claude FW-G Fable Desktop 标题官方证据不完整")
	}
	mapping := receipt.PersonaMapping
	if mapping.SourceSemantics != "generate-title" ||
		mapping.SourceThinkingDisposition != "fable-only-omitted-equivalent-to-disabled" ||
		mapping.TargetVersion != ClaudeFWGVersion || mapping.TargetEntrypoint != "cli" ||
		mapping.TargetScenario != "tui-title" ||
		mapping.TargetModel != "claude-haiku-4-5-20251001" ||
		mapping.TargetMaxTokens != 32000 || mapping.TargetThinking != "disabled" ||
		mapping.TargetTemperature != 1 || mapping.TargetToolCount != 0 ||
		mapping.WireSHA256 != claudeFWGWireDigest {
		return errors.New("Claude FW-G Fable Desktop 标题 Persona 映射非法")
	}
	return nil
}

func loadClaudeFWGFableDesktopTitleTestReceipt(
	source claudeFWGFableDesktopTitleSourceReceipt,
) (claudeFWGFableDesktopTitleTestReceipt, error) {
	var receipt claudeFWGFableDesktopTitleTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-fable-desktop-title-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	wantTests := []string{
		"TestClaudeFWGDesktopTitleNormalizesToFrozenTUITitleWire",
		"TestClaudeFWGDesktopFableTitleOmittedThinkingNormalizesToFrozenTUITitleWire",
		"TestClaudeFWGDesktopTitleExecutesThroughStrictCandidate",
		"TestClaudeFWGDesktopTitleUnknownShapesRemainFailClosed",
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGFableDesktopTitleTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-fable-desktop-title-test-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-fable-desktop-title-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGFableDesktopTitleSourceSHA256 ||
		len(receipt.Transitions) != 2 || !slices.Equal(receipt.TargetedTests, wantTests) ||
		len(receipt.NegativeCases) != 5 || receipt.RequiredRules != 40 ||
		receipt.AtomicAssertions != 110 || receipt.WireSHA256 != claudeFWGWireDigest ||
		receipt.ProfileChanged || receipt.WireChanged || receipt.ReleaseBundleChanged ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G Fable Desktop 标题 test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGFableDesktopTitleTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	source, err := loadClaudeFWGFableDesktopTitleSourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGFableDesktopTitleTestReceipt(source)
	if err != nil {
		return false
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			if transition.Path == path && transition.FromSHA256 == priorDigest &&
				transition.ToSHA256 == currentDigest {
				return true
			}
		}
	}
	return false
}

func TestClaudeFWGFableDesktopTitleTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGFableDesktopTitleSourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGFableDesktopTitleTestReceipt(source)
	if err != nil {
		t.Fatal(err)
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			raw, readErr := os.ReadFile(
				filepath.Join("../../..", filepath.FromSlash(transition.Path)),
			)
			if readErr != nil {
				t.Fatal(readErr)
			}
			if got := claudeFWGCountTokensDigest(raw); got != transition.ToSHA256 {
				t.Fatalf(
					"Claude FW-G Fable Desktop 标题 transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	if len(profile.document.Rules) != 40 || wire.Identity.ProfileDigest != ClaudeFWGProfileDigest ||
		claudeFWGWireDigest != source.PersonaMapping.WireSHA256 {
		t.Fatal("Claude FW-G Fable Desktop 标题迁移错误改变 Profile／Wire 身份")
	}
}
