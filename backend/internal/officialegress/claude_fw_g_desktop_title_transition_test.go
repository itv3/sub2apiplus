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
	claudeFWGDesktopTitleSourceSHA256 = "3d176f9b24549388936fd918e3eb2a160263eac372160611b42ab3dd62da6884"
	claudeFWGDesktopTitleTestSHA256   = "eecadfe40a4bf5f6e1e55ac2493d74557cccb0568e61d33ef0291c30ab5056d3"
)

type claudeFWGDesktopTitleSourceReceipt struct {
	SchemaVersion    string                              `json:"schema_version"`
	Date             string                              `json:"date"`
	Phase            string                              `json:"phase"`
	BaseCommit       string                              `json:"base_commit"`
	Trigger          string                              `json:"trigger"`
	Prior            []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	OfficialEvidence struct {
		DesktopVersion       string `json:"desktop_version"`
		ClientPlatform       string `json:"client_platform"`
		Authentication       string `json:"authentication"`
		Privacy              string `json:"privacy"`
		ClientHost           string `json:"client_host"`
		CaptureHost          string `json:"capture_host"`
		CaptureMethod        string `json:"capture_method"`
		UserAgent            string `json:"user_agent"`
		RequestMethod        string `json:"request_method"`
		RequestTarget        string `json:"request_target"`
		RequestBodyBytes     int    `json:"request_body_bytes"`
		RequestBodySHA256    string `json:"request_body_sha256"`
		PcapSHA256           string `json:"pcap_sha256"`
		SourceModel          string `json:"source_model"`
		SourceMaxTokens      int    `json:"source_max_tokens"`
		SourceThinking       string `json:"source_thinking"`
		SourceEffort         string `json:"source_effort"`
		SourceStream         bool   `json:"source_stream"`
		SourceToolCount      int    `json:"source_tool_count"`
		AgentSDKSystemSHA256 string `json:"agent_sdk_system_sha256"`
		TitlePromptSHA256    string `json:"title_prompt_sha256"`
		TitleFormatSHA256    string `json:"title_format_sha256"`
		RawCaptureRetained   bool   `json:"raw_capture_retained"`
	} `json:"official_evidence"`
	PersonaMapping struct {
		TargetVersion            string `json:"target_version"`
		TargetEntrypoint         string `json:"target_entrypoint"`
		TargetScenario           string `json:"target_scenario"`
		TargetModel              string `json:"target_model"`
		TargetMaxTokens          int    `json:"target_max_tokens"`
		TargetThinking           string `json:"target_thinking"`
		TargetTemperature        int    `json:"target_temperature"`
		TargetToolCount          int    `json:"target_tool_count"`
		TargetOutputConfigSHA256 string `json:"target_output_config_sha256"`
		WireSHA256               string `json:"wire_sha256"`
	} `json:"persona_mapping"`
	Dispositions []struct {
		Finding string `json:"finding"`
		Result  string `json:"result"`
	} `json:"dispositions"`
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	VircsServiceChanged       bool                              `json:"vircs_service_changed"`
	DMITCandidateRebuild      bool                              `json:"dmit_candidate_rebuild_required"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	Result                    string                            `json:"result"`
}

type claudeFWGDesktopTitleTestReceipt struct {
	SchemaVersion             string                            `json:"schema_version"`
	Date                      string                            `json:"date"`
	Phase                     string                            `json:"phase"`
	BaseCommit                string                            `json:"base_commit"`
	Source                    claudeFWGCountTokensTransitionRef `json:"source_transition"`
	Transitions               []changeset4SourceTransitionEntry `json:"transitions"`
	TargetedTests             []string                          `json:"targeted_tests"`
	RequiredRules             int                               `json:"required_rule_count"`
	AtomicRules               int                               `json:"atomic_assertion_count"`
	WireSHA256                string                            `json:"wire_sha256"`
	ProductionSelectorChanged bool                              `json:"production_selector_changed"`
	VircsServiceChanged       bool                              `json:"vircs_service_changed"`
	CodexFinalWire            string                            `json:"codex_final_wire"`
	Result                    string                            `json:"result"`
}

func loadClaudeFWGDesktopTitleSourceReceipt() (
	claudeFWGDesktopTitleSourceReceipt,
	error,
) {
	var receipt claudeFWGDesktopTitleSourceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-desktop-title-source-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGDesktopTitleSourceSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-desktop-title-source-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != "29fbcd3dd6b021c09e91f6b812f4a31b1c233d5f" ||
		strings.TrimSpace(receipt.Trigger) == "" || len(receipt.Prior) != 2 ||
		len(receipt.Dispositions) != 4 || len(receipt.Transitions) != 4 ||
		receipt.ProductionSelectorChanged || receipt.VircsServiceChanged ||
		!receipt.DMITCandidateRebuild || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G Desktop title source transition 顶层事实非法")
	}
	expectedPrior := map[string]string{
		"docs/egress/maintenance/claude-fw-g-thinking-display-source-transition.json": claudeFWGThinkingDisplaySourceSHA256,
		"docs/egress/maintenance/claude-fw-g-thinking-display-test-transition.json":   claudeFWGThinkingDisplayTestSHA256,
	}
	for _, prior := range receipt.Prior {
		if expectedPrior[prior.Path] != prior.SHA256 {
			return receipt, errors.New("Claude FW-G Desktop title prior transition 引用非法")
		}
		priorRaw, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if readErr != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
			return receipt, errors.New("Claude FW-G Desktop title prior transition 摘要不一致")
		}
		delete(expectedPrior, prior.Path)
	}
	if len(expectedPrior) != 0 {
		return receipt, errors.New("Claude FW-G Desktop title prior transition 缺失")
	}
	evidence := receipt.OfficialEvidence
	if evidence.DesktopVersion != "2.1.237" || evidence.ClientPlatform != "darwin/arm64" ||
		evidence.Authentication != "claude.ai-oauth" ||
		evidence.Privacy != "essential-traffic-no-telemetry" ||
		evidence.ClientHost != "local-mac" || evidence.CaptureHost != "DMIT" ||
		strings.TrimSpace(evidence.CaptureMethod) == "" ||
		evidence.UserAgent !=
			"claude-cli/2.1.237 (external, claude-desktop-3p, agent-sdk/0.3.237)" ||
		evidence.RequestMethod != "POST" || evidence.RequestTarget != "/v1/messages?beta=true" ||
		evidence.RequestBodyBytes != 4146 ||
		evidence.RequestBodySHA256 !=
			"c2a1a36b97373229f9f101cfaebc2d7d7c3c36647ca8ebcee9479eb4ae5a5f4d" ||
		evidence.PcapSHA256 !=
			"6d44f6094e3eef5a9a211044efb6b8e70f61e985a1e49a1885cad15fe8720255" ||
		evidence.SourceModel != "claude-sonnet-5" || evidence.SourceMaxTokens != 64000 ||
		evidence.SourceThinking != "disabled" || evidence.SourceEffort != "high" ||
		!evidence.SourceStream || evidence.SourceToolCount != 0 ||
		evidence.AgentSDKSystemSHA256 !=
			"0d7062851dd7bd7e66d4be4f12ac4951e3d2f587ec408295333a49963bd3f6b7" ||
		evidence.TitlePromptSHA256 != claudeDesktopTitlePromptSHA256 ||
		evidence.TitleFormatSHA256 !=
			"6a3dca9cf59b8442c703d56464855f8777da4c2e6f5d31c7a82dd5f16494c4f9" ||
		evidence.RawCaptureRetained {
		return receipt, errors.New("Claude FW-G Desktop title 官方证据不完整")
	}
	mapping := receipt.PersonaMapping
	if mapping.TargetVersion != ClaudeFWGVersion || mapping.TargetEntrypoint != "cli" ||
		mapping.TargetScenario != "tui-title" ||
		mapping.TargetModel != "claude-haiku-4-5-20251001" ||
		mapping.TargetMaxTokens != 32000 || mapping.TargetThinking != "disabled" ||
		mapping.TargetTemperature != 1 || mapping.TargetToolCount != 0 ||
		mapping.TargetOutputConfigSHA256 !=
			"bcd793e2583a3d26907643c170d741880269a90fee52ce40f3c5b64dcdfcf08c" ||
		mapping.WireSHA256 != claudeFWGWireDigest {
		return receipt, errors.New("Claude FW-G Desktop title Persona 映射非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func loadClaudeFWGDesktopTitleTestReceipt(
	source claudeFWGDesktopTitleSourceReceipt,
) (claudeFWGDesktopTitleTestReceipt, error) {
	var receipt claudeFWGDesktopTitleTestReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-desktop-title-test-transition.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	wantTests := []string{
		"TestClaudeFWGDesktopTitleNormalizesToFrozenTUITitleWire",
		"TestClaudeFWGDesktopTitleExecutesThroughStrictCandidate",
		"TestClaudeFWGDesktopTitleUnknownShapesRemainFailClosed",
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGDesktopTitleTestSHA256 ||
		receipt.SchemaVersion !=
			"official-egress-claude-fw-g-desktop-title-test-transition/v1" ||
		receipt.Date != "2026-08-21" || receipt.Phase != "FW-G" ||
		receipt.BaseCommit != source.BaseCommit ||
		receipt.Source.Path !=
			"docs/egress/maintenance/claude-fw-g-desktop-title-source-transition.json" ||
		receipt.Source.SHA256 != claudeFWGDesktopTitleSourceSHA256 ||
		len(receipt.Transitions) != 5 || !slices.Equal(receipt.TargetedTests, wantTests) ||
		receipt.RequiredRules != 40 || receipt.AtomicRules != 110 ||
		receipt.WireSHA256 != claudeFWGWireDigest || receipt.ProductionSelectorChanged ||
		receipt.VircsServiceChanged || receipt.CodexFinalWire != "zero_difference_required" ||
		receipt.Result != "passed" {
		return receipt, errors.New("Claude FW-G Desktop title test transition 顶层事实非法")
	}
	if err := validateClaudeFWGCountTokensTransitions(receipt.Transitions); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func claudeFWGDesktopTitleTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	source, err := loadClaudeFWGDesktopTitleSourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGDesktopTitleTestReceipt(source)
	if err != nil {
		return false
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			if transition.Path == path && transition.ToSHA256 == currentDigest &&
				claudeFWGDesktopTitlePriorReaches(
					path, priorDigest, transition.FromSHA256,
				) {
				return true
			}
		}
	}
	return false
}

func claudeFWGDesktopTitlePriorReaches(path, priorDigest, targetDigest string) bool {
	return priorDigest == targetDigest ||
		claudeFWGDesktopTitleThinkingPriorReaches(path, priorDigest, targetDigest) ||
		claudeFWGIngressAuthorityPriorReaches(path, priorDigest, targetDigest) ||
		claudeFWGDesktopCompatibilityPriorReaches(path, priorDigest, targetDigest)
}

func claudeFWGDesktopTitleThinkingPriorReaches(
	path string,
	priorDigest string,
	targetDigest string,
) bool {
	source, err := loadClaudeFWGThinkingDisplaySourceReceipt()
	if err != nil {
		return false
	}
	testReceipt, err := loadClaudeFWGThinkingDisplayTestReceipt(source)
	if err != nil {
		return false
	}
	for _, transitions := range [][]changeset4SourceTransitionEntry{
		source.Transitions,
		testReceipt.Transitions,
	} {
		for _, transition := range transitions {
			if transition.Path == path && transition.ToSHA256 == targetDigest &&
				claudeFWGThinkingDisplayPriorReaches(
					path, priorDigest, transition.FromSHA256,
				) {
				return true
			}
		}
	}
	return false
}

func TestClaudeFWGDesktopTitleTransitionsAreFrozen(t *testing.T) {
	source, err := loadClaudeFWGDesktopTitleSourceReceipt()
	if err != nil {
		t.Fatal(err)
	}
	testReceipt, err := loadClaudeFWGDesktopTitleTestReceipt(source)
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
					"Claude FW-G Desktop title transition 漂移：path=%s got=%s want=%s",
					transition.Path, got, transition.ToSHA256,
				)
			}
		}
	}
	if _, err := loadClaudeFWGWire(); err != nil {
		t.Fatal(err)
	}
}
