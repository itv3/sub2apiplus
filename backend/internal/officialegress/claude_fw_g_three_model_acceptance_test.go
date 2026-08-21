package officialegress

import (
	"errors"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const claudeFWGThreeModelAcceptanceSHA256 = "16dc60bc46eede747cb0535e53367c134a28466f12399f06a485693e142b15a9"

type claudeFWGThreeModelMessageResult struct {
	HTTPStatus            int    `json:"http_status"`
	ExpectedMarkerMatched bool   `json:"expected_marker_matched"`
	FallbackDisposition   string `json:"fallback_disposition"`
	FallbackTarget        string `json:"fallback_target"`
	Result                string `json:"result"`
}

type claudeFWGThreeModelTokenResult struct {
	HTTPStatus  int    `json:"http_status"`
	InputTokens int    `json:"input_tokens"`
	Result      string `json:"result"`
}

type claudeFWGThreeModelDesktopRequest struct {
	RequestID     string `json:"request_id"`
	BodyBytes     int    `json:"body_bytes"`
	Stream        bool   `json:"stream"`
	DMITAccountID int64  `json:"dmit_account_id"`
	HTTPStatus    int    `json:"http_status"`
}

type claudeFWGThreeModelAcceptanceReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Phase         string `json:"phase"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	Target        struct {
		Product                      string   `json:"product"`
		Version                      string   `json:"version"`
		Platform                     string   `json:"platform"`
		Authentication               string   `json:"authentication"`
		Privacy                      string   `json:"privacy"`
		Models                       []string `json:"models"`
		ProfileSHA256                string   `json:"profile_sha256"`
		WireSHA256                   string   `json:"wire_sha256"`
		ReleaseArtifactSHA256        string   `json:"release_artifact_sha256"`
		ReleaseBundleSHA256          string   `json:"release_bundle_sha256"`
		ModelCapabilityCatalogSHA256 string   `json:"model_capability_catalog_sha256"`
	} `json:"target"`
	Candidate struct {
		CandidateID     string `json:"candidate_id"`
		Commit          string `json:"commit"`
		Tree            string `json:"tree"`
		Architecture    string `json:"architecture"`
		Version         string `json:"version"`
		BinarySHA256    string `json:"binary_sha256"`
		Image           string `json:"image"`
		ImageID         string `json:"image_id"`
		DeployedHost    string `json:"deployed_host"`
		ContainerID     string `json:"container_id"`
		ContainerHealth string `json:"container_health"`
	} `json:"candidate"`
	Build struct {
		CompileHost                 string   `json:"compile_host"`
		GoVersion                   string   `json:"go_version"`
		GOOS                        string   `json:"goos"`
		GOARCH                      string   `json:"goarch"`
		CGOEnabled                  bool     `json:"cgo_enabled"`
		BuildTags                   []string `json:"build_tags"`
		Trimpath                    bool     `json:"trimpath"`
		PackagingHost               string   `json:"packaging_host"`
		PackagingHostCompiledSource bool     `json:"packaging_host_compiled_source"`
	} `json:"build"`
	Prior       []claudeFWGCountTokensTransitionRef `json:"prior_receipts"`
	Transitions []changeset4SourceTransitionEntry   `json:"transitions"`
	Coverage    struct {
		RequiredRules                   int    `json:"required_rules"`
		ProfileAtomicAssertions         int    `json:"profile_atomic_assertions"`
		ScenarioOnlyAssertions          int    `json:"scenario_only_assertions"`
		AtomicAssertions                int    `json:"atomic_assertions"`
		RequiredRulesDuplicatedPerModel bool   `json:"required_rules_duplicated_per_model"`
		TelemetryAndBehaviorTraffic     string `json:"telemetry_and_behavior_traffic"`
	} `json:"coverage"`
	Validation struct {
		SourceAndLocalGates string                                      `json:"source_and_local_gates"`
		APIMessages         map[string]claudeFWGThreeModelMessageResult `json:"api_messages"`
		APICountTokens      map[string]claudeFWGThreeModelTokenResult   `json:"api_count_tokens"`
		ClaudeDesktopFable  struct {
			Client              string                            `json:"client"`
			SourceModel         string                            `json:"source_model"`
			Effort              string                            `json:"effort"`
			ExpectedResponse    string                            `json:"expected_response"`
			ActualResponse      string                            `json:"actual_response"`
			GeneratedTitle      string                            `json:"generated_title"`
			TitleRequest        claudeFWGThreeModelDesktopRequest `json:"title_request"`
			MainRequest         claudeFWGThreeModelDesktopRequest `json:"main_request"`
			FollowupCountTokens string                            `json:"followup_count_tokens"`
			Result              string                            `json:"result"`
		} `json:"claude_desktop_fable"`
		NegativeBoundaries struct {
			UnknownModel                 string `json:"unknown_model"`
			UnregisteredAlias            string `json:"unregistered_alias"`
			ThirdPartyFallbacks          string `json:"third_party_fallbacks"`
			UnapprovedDynamicToolCatalog string `json:"unapproved_dynamic_tool_catalog"`
		} `json:"negative_boundaries"`
		RollbackAndRestoration struct {
			RollbackImage                     string `json:"rollback_image"`
			RollbackImageID                   string `json:"rollback_image_id"`
			RollbackHealth                    string `json:"rollback_health"`
			RestoredImage                     string `json:"restored_image"`
			RestoredImageID                   string `json:"restored_image_id"`
			RestorationHealth                 string `json:"restoration_health"`
			ThreeXUIContainerIDBeforeAndAfter string `json:"three_x_ui_container_id_before_and_after"`
			PostgresContainerIDBeforeAndAfter string `json:"postgres_container_id_before_and_after"`
			RedisContainerIDBeforeAndAfter    string `json:"redis_container_id_before_and_after"`
			UnrelatedContainersRecreated      bool   `json:"unrelated_containers_recreated"`
			Result                            string `json:"result"`
		} `json:"rollback_and_restoration"`
	} `json:"validation"`
	PriorBlockerDisposition struct {
		PendingReceiptPreserved bool   `json:"pending_receipt_preserved"`
		Blocker                 string `json:"blocker"`
		Resolution              string `json:"resolution"`
		DMITAccountID           int64  `json:"dmit_account_id"`
		Result                  string `json:"result"`
	} `json:"prior_blocker_disposition"`
	Safety struct {
		ProductionSelectorModified bool   `json:"production_selector_modified"`
		DeploymentFactIssued       bool   `json:"deployment_fact_issued"`
		ActivationReceiptIssued    bool   `json:"activation_receipt_issued"`
		FWHStarted                 bool   `json:"fw_h_started"`
		VircsConnectedForMutation  bool   `json:"vircs_connected_for_mutation"`
		VircsServiceChanged        bool   `json:"vircs_service_changed"`
		LegacyChainRetained        bool   `json:"legacy_chain_retained"`
		ValidationHost             string `json:"validation_host"`
	} `json:"safety"`
	SecretScan struct {
		MatchedPatterns int    `json:"matched_patterns"`
		Result          string `json:"result"`
	} `json:"secret_scan"`
	AcceptanceFactIssued bool   `json:"acceptance_fact_issued"`
	CandidateState       string `json:"candidate_state"`
	ProductionState      string `json:"production_state"`
	Status               string `json:"status"`
	Result               string `json:"result"`
}

func loadClaudeFWGThreeModelAcceptance() (
	claudeFWGThreeModelAcceptanceReceipt,
	error,
) {
	var receipt claudeFWGThreeModelAcceptanceReceipt
	raw, err := readClaudeFWGCountTokensReceipt(
		"docs/egress/maintenance/claude-fw-g-three-model-acceptance.json",
		&receipt,
	)
	if err != nil {
		return receipt, err
	}
	if claudeFWGCountTokensDigest(raw) != claudeFWGThreeModelAcceptanceSHA256 ||
		receipt.SchemaVersion != "claude-code-fw-g-three-model-acceptance/v1" ||
		receipt.Phase != "FW-G" || receipt.IssuedAtUTC != "2026-08-21T13:10:49Z" ||
		!receipt.AcceptanceFactIssued || receipt.CandidateState != "ready" ||
		receipt.ProductionState != "not_activated" ||
		receipt.Status != "ready/not_activated" || receipt.Result != "accepted" {
		return receipt, errors.New("Claude 三模型最终验收顶层事实非法")
	}
	for _, validate := range []func(claudeFWGThreeModelAcceptanceReceipt) error{
		validateClaudeFWGThreeModelFinalAcceptanceIdentity,
		validateClaudeFWGThreeModelAcceptanceHistory,
		validateClaudeFWGThreeModelAcceptanceCoverage,
		validateClaudeFWGThreeModelAcceptanceRuntime,
		validateClaudeFWGThreeModelAcceptanceSafety,
	} {
		if err := validate(receipt); err != nil {
			return receipt, err
		}
	}
	return receipt, nil
}

func validateClaudeFWGThreeModelFinalAcceptanceIdentity(
	receipt claudeFWGThreeModelAcceptanceReceipt,
) error {
	target := receipt.Target
	if target.Product != "claude-code" || target.Version != ClaudeFWGVersion ||
		target.Platform != "linux/amd64" || target.Authentication != "claude.ai-oauth" ||
		target.Privacy != "essential-traffic-no-telemetry" ||
		!slices.Equal(target.Models, []string{
			"claude-sonnet-5", "claude-opus-5", "claude-fable-5",
		}) || target.ProfileSHA256 != ClaudeFWGProfileDigest ||
		target.WireSHA256 != claudeFWGWireDigest ||
		target.ReleaseArtifactSHA256 != ClaudeFWGReleaseDigest ||
		target.ReleaseBundleSHA256 != ClaudeFWGBundleDigest ||
		target.ModelCapabilityCatalogSHA256 != claudeFWGModelCapabilityCatalogSHA256 {
		return errors.New("Claude 三模型最终验收目标身份非法")
	}
	candidate := receipt.Candidate
	if candidate.CandidateID != "claude-code-2_1_226-fw-g-three-model-2f224831f" ||
		candidate.Commit != "2f224831fcbad72e03782d109f9f94458457e2ff" ||
		candidate.Tree != "3cd964dc0f4133bb615fa15a9f2f18a5825ac057" ||
		candidate.Architecture != "linux/amd64" ||
		candidate.Version != "0.1.177-4-fw-g-2f224831f" ||
		candidate.BinarySHA256 !=
			"4d42c7e6e819b1d27aa5cbfd85303946275ab93a384ed24fbede76515a3c86e8" ||
		candidate.Image != "sub2apiplus:fw-g-2f224831f" ||
		candidate.ImageID !=
			"sha256:ea2814364a06df65d6c8037fdb455311e6f87754f18c2850f00d3bced9ed1172" ||
		candidate.DeployedHost != "DMIT" ||
		candidate.ContainerID !=
			"56fdf7a243a8fa1a6a4a5807cbb85c57dcb512deffd6ec8b249d5bf1b2acbb68" ||
		candidate.ContainerHealth != "healthy" {
		return errors.New("Claude 三模型最终验收 Candidate 身份非法")
	}
	build := receipt.Build
	if build.CompileHost != "local-mac" || build.GoVersion != "1.26.6" ||
		build.GOOS != "linux" || build.GOARCH != "amd64" || build.CGOEnabled ||
		!slices.Equal(build.BuildTags, []string{"embed"}) || !build.Trimpath ||
		build.PackagingHost != "ARM64" || build.PackagingHostCompiledSource {
		return errors.New("Claude 三模型最终验收构建身份非法")
	}
	return nil
}

func validateClaudeFWGThreeModelAcceptanceHistory(
	receipt claudeFWGThreeModelAcceptanceReceipt,
) error {
	wantPrior := map[string]string{
		"docs/egress/maintenance/claude-fw-g-fable-desktop-title-source-transition.json": claudeFWGFableDesktopTitleSourceSHA256,
		"docs/egress/maintenance/claude-fw-g-fable-desktop-title-test-transition.json":   claudeFWGFableDesktopTitleTestSHA256,
		"docs/egress/maintenance/claude-fw-g-three-model-acceptance-attempt.json":        claudeFWGThreeModelAcceptanceAttemptSHA256,
	}
	for _, prior := range receipt.Prior {
		if wantPrior[prior.Path] != prior.SHA256 {
			return errors.New("Claude 三模型最终验收前序收据非法")
		}
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(prior.Path)))
		if err != nil || claudeFWGCountTokensDigest(raw) != prior.SHA256 {
			return errors.New("Claude 三模型最终验收前序收据摘要不一致")
		}
		delete(wantPrior, prior.Path)
	}
	if len(wantPrior) != 0 {
		return errors.New("Claude 三模型最终验收前序收据不闭合")
	}
	wantTransitions := map[string][2]string{
		"backend/internal/officialegress/claude_fw_g_fable_desktop_title_transition_test.go": {
			"f3be18f80ae5062c54d3240aa04ac1401a4ad61a0beef6188bb8528485b05b34",
			"b285b2a9b2c595fb1b8f25bd070f0691f8c5bad83fb08de46150c3cd7a3fb64f",
		},
		"backend/internal/officialegress/claude_fw_g_model_capability_transition_test.go": {
			"9173c5b7ed29798ceb5728679a7a795b0b5edeb2b65ce3186e1fc0d5aa05f969",
			"21cf6f30608d2347dff283a115ab13d18b0d97a919b1eaf8d5c454fd73afd639",
		},
		"backend/internal/officialegress/claude_fw_g_three_model_acceptance_attempt_test.go": {
			"01214b1308aef7fc3f815a9d6900f9c393f3ccb022446eda838ef3e8576e8baa",
			"a472c11cb291aea970574ff7d999b46ff999d3f51fec0c76416223eaa4e84ed0",
		},
		"docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md": {
			"f764d050426d02b0ca8404747c698c21a42b545f224b93332a8a5dbad4a99bfe",
			"25b8688b5ef288826bf225ae3046250680f331906c1e0c09484320677f54e39d",
		},
	}
	paths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		want, exists := wantTransitions[transition.Path]
		if !exists || transition.FromSHA256 != want[0] ||
			transition.ToSHA256 != want[1] || strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude 三模型最终验收 transition 非法")
		}
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		currentDigest := claudeFWGCountTokensDigest(raw)
		if err != nil || currentDigest != transition.ToSHA256 &&
			!claudeFWHProductionAcceptanceSupersedes(
				transition.Path, transition.ToSHA256, currentDigest,
			) {
			return errors.New("Claude 三模型最终验收 transition 当前摘要不一致")
		}
		paths = append(paths, transition.Path)
		delete(wantTransitions, transition.Path)
	}
	if len(wantTransitions) != 0 || !slices.IsSorted(paths) ||
		len(paths) != len(slices.Compact(append([]string(nil), paths...))) {
		return errors.New("Claude 三模型最终验收 transition 集合不闭合")
	}
	return nil
}

func validateClaudeFWGThreeModelAcceptanceCoverage(
	receipt claudeFWGThreeModelAcceptanceReceipt,
) error {
	coverage := receipt.Coverage
	if coverage.RequiredRules != 40 || coverage.ProfileAtomicAssertions != 106 ||
		coverage.ScenarioOnlyAssertions != 4 || coverage.AtomicAssertions != 110 ||
		coverage.RequiredRulesDuplicatedPerModel ||
		coverage.TelemetryAndBehaviorTraffic != "excluded" {
		return errors.New("Claude 三模型最终验收覆盖统计非法")
	}
	return nil
}

func validateClaudeFWGThreeModelAcceptanceRuntime(
	receipt claudeFWGThreeModelAcceptanceReceipt,
) error {
	validation := receipt.Validation
	if validation.SourceAndLocalGates != "passed" || len(validation.APIMessages) != 3 ||
		len(validation.APICountTokens) != 3 {
		return errors.New("Claude 三模型最终验收本地或 API 矩阵不完整")
	}
	for _, model := range []string{"claude-sonnet-5", "claude-opus-5", "claude-fable-5"} {
		message, exists := validation.APIMessages[model]
		if !exists || message.HTTPStatus != 200 || !message.ExpectedMarkerMatched ||
			message.Result != "passed" {
			return errors.New("Claude 三模型最终验收 Messages 正例非法")
		}
		if model == "claude-fable-5" {
			if message.FallbackDisposition != "approved_server_fallback" ||
				message.FallbackTarget != "claude-opus-4-8" {
				return errors.New("Claude 三模型最终验收 Fable fallback 非法")
			}
		} else if message.FallbackDisposition != "" || message.FallbackTarget != "" {
			return errors.New("Claude 三模型最终验收意外记录非 Fable fallback")
		}
		tokens, exists := validation.APICountTokens[model]
		if !exists || tokens.HTTPStatus != 200 || tokens.InputTokens != 12 ||
			tokens.Result != "passed" {
			return errors.New("Claude 三模型最终验收 count_tokens 正例非法")
		}
	}
	desktop := validation.ClaudeDesktopFable
	if desktop.Client != "claude-desktop" || desktop.SourceModel != "claude-fable-5" ||
		desktop.Effort != "high" ||
		desktop.ExpectedResponse != "DESKTOP_FABLE_2F224831F_OK" ||
		desktop.ActualResponse != desktop.ExpectedResponse ||
		desktop.GeneratedTitle != "DESKTOP_FABLE_2F224831F_OK reply" ||
		desktop.FollowupCountTokens != "passed" || desktop.Result != "passed" {
		return errors.New("Claude 三模型最终验收 Desktop Fable 结果非法")
	}
	if desktop.TitleRequest.RequestID != "73f7c98c-16d8-4821-9f33-004374f6f6d3" ||
		desktop.TitleRequest.BodyBytes != 4081 || !desktop.TitleRequest.Stream ||
		desktop.TitleRequest.DMITAccountID != 101 || desktop.TitleRequest.HTTPStatus != 200 ||
		desktop.MainRequest.RequestID != "262aad4c-dcff-4c44-90b8-41585d0f29e1" ||
		desktop.MainRequest.BodyBytes != 137743 || !desktop.MainRequest.Stream ||
		desktop.MainRequest.DMITAccountID != 101 || desktop.MainRequest.HTTPStatus != 200 {
		return errors.New("Claude 三模型最终验收 Desktop 请求证据非法")
	}
	negative := validation.NegativeBoundaries
	if negative.UnknownModel != "fail-close-passed" ||
		negative.UnregisteredAlias != "fail-close-passed" ||
		negative.ThirdPartyFallbacks != "fail-close-passed" ||
		negative.UnapprovedDynamicToolCatalog != "fail-close-passed" {
		return errors.New("Claude 三模型最终验收负边界非法")
	}
	rollback := validation.RollbackAndRestoration
	if rollback.RollbackImage != "sub2apiplus:fw-g-290516f55" ||
		rollback.RollbackImageID !=
			"sha256:336b9142b95e6f3f64f4ff16a15bb4650a65e6716a7ebb65bfd164c9b8cb6095" ||
		rollback.RollbackHealth != "passed" ||
		rollback.RestoredImage != "sub2apiplus:fw-g-2f224831f" ||
		rollback.RestoredImageID != receipt.Candidate.ImageID ||
		rollback.RestorationHealth != "passed" ||
		rollback.ThreeXUIContainerIDBeforeAndAfter !=
			"3a5bef805f0be14fd4b6667aac6ce45c4f243f1df6f083d6040df32df84048f9" ||
		rollback.PostgresContainerIDBeforeAndAfter !=
			"b9b6915c5336f1857d279e953e9c3f179c1082af99addffdaacd3ace0d6caf0f" ||
		rollback.RedisContainerIDBeforeAndAfter !=
			"b1135fe353ccd78302b1ddc7b306e9fa66038856909bcd315b61ee920a9039bd" ||
		rollback.UnrelatedContainersRecreated || rollback.Result != "passed" {
		return errors.New("Claude 三模型最终验收回滚或恢复事实非法")
	}
	return nil
}

func validateClaudeFWGThreeModelAcceptanceSafety(
	receipt claudeFWGThreeModelAcceptanceReceipt,
) error {
	blocker := receipt.PriorBlockerDisposition
	if !blocker.PendingReceiptPreserved || blocker.Blocker != "fable-messages-usage-credits" ||
		blocker.Resolution != "resolved_by_separate_dmit_oauth_account_with_fable_entitlement" ||
		blocker.DMITAccountID != 101 || blocker.Result != "resolved" {
		return errors.New("Claude 三模型最终验收历史阻断处置非法")
	}
	safety := receipt.Safety
	if safety.ProductionSelectorModified || safety.DeploymentFactIssued ||
		safety.ActivationReceiptIssued || safety.FWHStarted ||
		safety.VircsConnectedForMutation || safety.VircsServiceChanged ||
		!safety.LegacyChainRetained || safety.ValidationHost != "DMIT" {
		return errors.New("Claude 三模型最终验收生产隔离事实非法")
	}
	if receipt.SecretScan.MatchedPatterns != 0 || receipt.SecretScan.Result != "passed" {
		return errors.New("Claude 三模型最终验收秘密扫描事实非法")
	}
	return nil
}

func claudeFWGThreeModelAcceptanceSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHProductionAcceptanceSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWHSourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadClaudeFWGThreeModelAcceptance()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			(transition.ToSHA256 == currentDigest ||
				claudeFWHProductionAcceptanceSupersedes(
					path, transition.ToSHA256, currentDigest,
				) || claudeFWHSourceTransitionSupersedes(
				path, transition.ToSHA256, currentDigest,
			)) {
			return true
		}
	}
	return false
}

func TestClaudeFWGThreeModelAcceptanceIsFrozen(t *testing.T) {
	receipt, err := loadClaudeFWGThreeModelAcceptance()
	if err != nil {
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
	if len(profile.document.Rules) != receipt.Coverage.RequiredRules ||
		wire.Identity.ProfileDigest != receipt.Target.ProfileSHA256 ||
		wire.Identity.ModelCapabilityCatalogDigest !=
			receipt.Target.ModelCapabilityCatalogSHA256 {
		t.Fatal("Claude 三模型最终验收未绑定当前内容寻址制品")
	}
}
