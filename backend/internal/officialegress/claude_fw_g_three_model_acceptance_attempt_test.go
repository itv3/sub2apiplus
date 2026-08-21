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

const claudeFWGThreeModelAcceptanceAttemptSHA256 = "7f6599b3ac521cf669bcccef8170984702b9d62feb5812d5bb2cb80c25c08319"

type claudeFWGThreeModelAcceptanceAttemptReceipt struct {
	SchemaVersion        string                              `json:"schema_version"`
	Phase                string                              `json:"phase"`
	RecordedAtUTC        string                              `json:"recorded_at_utc"`
	Target               json.RawMessage                     `json:"target"`
	Candidate            json.RawMessage                     `json:"candidate"`
	Prior                []claudeFWGCountTokensTransitionRef `json:"prior_transitions"`
	Transitions          []changeset4SourceTransitionEntry   `json:"transitions"`
	Coverage             json.RawMessage                     `json:"coverage"`
	Validation           json.RawMessage                     `json:"validation"`
	Safety               json.RawMessage                     `json:"safety"`
	BlockingConditions   []json.RawMessage                   `json:"blocking_conditions"`
	AcceptanceFactIssued bool                                `json:"acceptance_fact_issued"`
	ProductionState      string                              `json:"production_state"`
	Result               string                              `json:"result"`
}

func loadClaudeFWGThreeModelAcceptanceAttempt() (
	claudeFWGThreeModelAcceptanceAttemptReceipt,
	error,
) {
	var receipt claudeFWGThreeModelAcceptanceAttemptReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-three-model-acceptance-attempt.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGThreeModelAcceptanceAttemptSHA256 ||
		receipt.SchemaVersion != "claude-code-fw-g-three-model-acceptance-attempt/v1" ||
		receipt.Phase != "FW-G" || receipt.RecordedAtUTC != "2026-08-21T11:02:23Z" ||
		receipt.AcceptanceFactIssued || receipt.ProductionState != "not_activated" ||
		receipt.Result != "acceptance_pending" || len(receipt.Prior) != 1 ||
		len(receipt.Transitions) != 1 || len(receipt.BlockingConditions) != 1 {
		return receipt, errors.New("Claude 三模型验收尝试顶层事实非法")
	}
	if err := validateClaudeFWGThreeModelAcceptanceIdentity(receipt); err != nil {
		return receipt, err
	}
	if err := validateClaudeFWGThreeModelAcceptanceResult(receipt); err != nil {
		return receipt, err
	}
	if err := validateClaudeFWGThreeModelAcceptanceTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateClaudeFWGThreeModelAcceptanceIdentity(
	receipt claudeFWGThreeModelAcceptanceAttemptReceipt,
) error {
	var target struct {
		Product                      string   `json:"product"`
		Version                      string   `json:"version"`
		Platform                     string   `json:"platform"`
		Models                       []string `json:"models"`
		ProfileSHA256                string   `json:"profile_sha256"`
		WireSHA256                   string   `json:"wire_sha256"`
		ReleaseArtifactSHA256        string   `json:"release_artifact_sha256"`
		ReleaseBundleSHA256          string   `json:"release_bundle_sha256"`
		ModelCapabilityCatalogSHA256 string   `json:"model_capability_catalog_sha256"`
	}
	var candidate struct {
		CandidateID  string `json:"candidate_id"`
		Commit       string `json:"commit"`
		Tree         string `json:"tree"`
		Architecture string `json:"architecture"`
		Image        string `json:"image"`
		ImageDigest  string `json:"image_digest"`
		DeployedHost string `json:"deployed_host"`
		Version      string `json:"version"`
	}
	if json.Unmarshal(receipt.Target, &target) != nil ||
		json.Unmarshal(receipt.Candidate, &candidate) != nil {
		return errors.New("Claude 三模型验收尝试身份无法解析")
	}
	if target.Product != "claude-code" || target.Version != ClaudeFWGVersion ||
		target.Platform != "linux/amd64" || !slices.Equal(target.Models, []string{
		"claude-sonnet-5", "claude-opus-5", "claude-fable-5",
	}) || target.ProfileSHA256 != ClaudeFWGProfileDigest ||
		target.WireSHA256 != claudeFWGWireDigest ||
		target.ReleaseArtifactSHA256 != ClaudeFWGReleaseDigest ||
		target.ReleaseBundleSHA256 != ClaudeFWGBundleDigest ||
		target.ModelCapabilityCatalogSHA256 != claudeFWGModelCapabilityCatalogSHA256 {
		return errors.New("Claude 三模型验收尝试目标身份非法")
	}
	if candidate.CandidateID != "claude-code-2_1_226-fw-g-three-model-290516f55" ||
		candidate.Commit != "290516f55c0aaa615ff0eb7ea53dd8fa13078ba9" ||
		candidate.Tree != "a742c21e279945d4e1f2a40424ad27baf546568d" ||
		candidate.Architecture != "linux/amd64" ||
		candidate.Image != "sub2apiplus:fw-g-290516f55" ||
		candidate.ImageDigest !=
			"sha256:336b9142b95e6f3f64f4ff16a15bb4650a65e6716a7ebb65bfd164c9b8cb6095" ||
		candidate.DeployedHost != "DMIT" ||
		candidate.Version != "0.1.177-4-fw-g-290516f55" {
		return errors.New("Claude 三模型验收尝试 Candidate 身份非法")
	}
	return nil
}

func validateClaudeFWGThreeModelAcceptanceResult(
	receipt claudeFWGThreeModelAcceptanceAttemptReceipt,
) error {
	var coverage struct {
		RequiredRules                   int    `json:"required_rules"`
		ProfileAtomicAssertions         int    `json:"profile_atomic_assertions"`
		ScenarioOnlyAssertions          int    `json:"scenario_only_assertions"`
		AtomicAssertions                int    `json:"atomic_assertions"`
		RequiredRulesDuplicatedPerModel bool   `json:"required_rules_duplicated_per_model"`
		TelemetryAndBehaviorTraffic     string `json:"telemetry_and_behavior_traffic"`
	}
	var validation struct {
		SourceAndLocalGates string `json:"source_and_local_gates"`
		APIMessages         map[string]struct {
			Result string `json:"result"`
		} `json:"api_messages"`
		APICountTokens map[string]struct {
			Result      string `json:"result"`
			InputTokens int    `json:"input_tokens"`
		} `json:"api_count_tokens"`
		ClaudeDesktop struct {
			Sonnet string `json:"claude-sonnet-5"`
			Opus   string `json:"claude-opus-5"`
			Fable  string `json:"claude-fable-5"`
			Result string `json:"result"`
		} `json:"claude_desktop"`
		ZLFCode struct {
			UpstreamRequestSent bool   `json:"upstream_request_sent"`
			ExpectedDisposition string `json:"expected_disposition"`
			Result              string `json:"result"`
		} `json:"zlfcode"`
		RollbackAndRestoration struct {
			Result string `json:"result"`
		} `json:"rollback_and_restoration"`
	}
	var safety struct {
		ProductionSelectorModified bool   `json:"production_selector_modified"`
		FWHStarted                 bool   `json:"fw_h_started"`
		VircsConnectedForMutation  bool   `json:"vircs_connected_for_mutation"`
		VircsServiceChanged        bool   `json:"vircs_service_changed"`
		VircsHealth                string `json:"vircs_health"`
	}
	var blocking struct {
		ID             string `json:"id"`
		Scope          string `json:"scope"`
		Reason         string `json:"reason"`
		RequiredAction string `json:"required_action"`
	}
	if json.Unmarshal(receipt.Coverage, &coverage) != nil ||
		json.Unmarshal(receipt.Validation, &validation) != nil ||
		json.Unmarshal(receipt.Safety, &safety) != nil ||
		json.Unmarshal(receipt.BlockingConditions[0], &blocking) != nil {
		return errors.New("Claude 三模型验收尝试结果无法解析")
	}
	if coverage.RequiredRules != 40 || coverage.ProfileAtomicAssertions != 106 ||
		coverage.ScenarioOnlyAssertions != 4 || coverage.AtomicAssertions != 110 ||
		coverage.RequiredRulesDuplicatedPerModel ||
		coverage.TelemetryAndBehaviorTraffic != "excluded" {
		return errors.New("Claude 三模型验收尝试覆盖统计非法")
	}
	if validation.SourceAndLocalGates != "passed" ||
		validation.APIMessages["claude-sonnet-5"].Result != "passed" ||
		validation.APIMessages["claude-opus-5"].Result != "passed" ||
		validation.APIMessages["claude-fable-5"].Result !=
			"blocked_by_upstream_entitlement" ||
		validation.APICountTokens["claude-sonnet-5"].Result != "passed" ||
		validation.APICountTokens["claude-opus-5"].Result != "passed" ||
		validation.APICountTokens["claude-fable-5"].Result != "passed" ||
		validation.APICountTokens["claude-sonnet-5"].InputTokens != 12 ||
		validation.APICountTokens["claude-opus-5"].InputTokens != 12 ||
		validation.APICountTokens["claude-fable-5"].InputTokens != 12 ||
		validation.ClaudeDesktop.Sonnet != "DESKTOP_SONNET_OK" ||
		validation.ClaudeDesktop.Opus != "DESKTOP_OPUS_OK" ||
		validation.ClaudeDesktop.Fable != "blocked_by_upstream_entitlement" ||
		validation.ClaudeDesktop.Result != "partial" ||
		validation.ZLFCode.UpstreamRequestSent ||
		validation.ZLFCode.ExpectedDisposition != "denied" ||
		validation.ZLFCode.Result != "fail-close-passed" ||
		validation.RollbackAndRestoration.Result != "passed" {
		return errors.New("Claude 三模型验收尝试矩阵事实非法")
	}
	if safety.ProductionSelectorModified || safety.FWHStarted ||
		safety.VircsConnectedForMutation || safety.VircsServiceChanged ||
		safety.VircsHealth != "healthy" {
		return errors.New("Claude 三模型验收尝试生产隔离事实非法")
	}
	if blocking.ID != "fable-messages-usage-credits" ||
		blocking.Scope != "claude-fable-5 POST /v1/messages" ||
		strings.TrimSpace(blocking.Reason) == "" ||
		strings.TrimSpace(blocking.RequiredAction) == "" {
		return errors.New("Claude 三模型验收尝试阻断条件非法")
	}
	return nil
}

func validateClaudeFWGThreeModelAcceptanceTransition(
	receipt claudeFWGThreeModelAcceptanceAttemptReceipt,
) error {
	prior := receipt.Prior[0]
	if prior.Path !=
		"docs/egress/maintenance/claude-fw-g-model-capability-source-transition.json" ||
		prior.SHA256 != claudeFWGModelCapabilityTransitionSHA256 {
		return errors.New("Claude 三模型验收尝试前序 transition 非法")
	}
	priorRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
	if err != nil || claudeFWGCountTokensDigest(priorRaw) != prior.SHA256 {
		return errors.New("Claude 三模型验收尝试前序 transition 摘要不一致")
	}
	transition := receipt.Transitions[0]
	if transition.Path != "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md" ||
		transition.FromSHA256 !=
			"1a882aec8fcff55693126d5d2d8cadb6e8460cf59762c27de76e24a39a0e4a81" ||
		transition.ToSHA256 !=
			"f764d050426d02b0ca8404747c698c21a42b545f224b93332a8a5dbad4a99bfe" ||
		strings.TrimSpace(transition.Reason) == "" {
		return errors.New("Claude 三模型验收尝试文档 transition 非法")
	}
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
	if err != nil || claudeFWGCountTokensDigest(raw) != transition.ToSHA256 {
		return errors.New("Claude 三模型验收尝试文档 transition 当前摘要不一致")
	}
	return nil
}

func claudeFWGThreeModelAcceptanceAttemptSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	receipt, err := loadClaudeFWGThreeModelAcceptanceAttempt()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}

func TestClaudeFWGThreeModelAcceptanceAttemptIsFrozen(t *testing.T) {
	if _, err := loadClaudeFWGThreeModelAcceptanceAttempt(); err != nil {
		t.Fatal(err)
	}
}
