package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

const claudeFWHResponseRequestIDAcceptanceSHA256 = "722df263723bbf64ebc74f9470d4c9725b9e92710feda4a3b55bfa8eb4ad8668"

type claudeFWHResponseRequestIDLiveMatrix struct {
	Name          string `json:"name"`
	Passed        int    `json:"passed"`
	Restarts      int    `json:"restarts"`
	StartedAtUTC  string `json:"started_at_utc"`
	FinishedAtUTC string `json:"finished_at_utc"`
	LogSHA256     string `json:"log_sha256"`
}

type claudeFWHResponseRequestIDAcceptance struct {
	SchemaVersion string                          `json:"schema_version"`
	IssuedAtUTC   string                          `json:"issued_at_utc"`
	Phase         string                          `json:"phase"`
	AcceptanceID  string                          `json:"acceptance_id"`
	Scope         string                          `json:"scope"`
	Predecessors  []claudeFWHResponseRequestIDRef `json:"predecessors"`
	Release       struct {
		Version              string `json:"version"`
		ProfileSHA256        string `json:"profile_sha256"`
		WireSHA256           string `json:"wire_sha256"`
		ReleaseSHA256        string `json:"release_sha256"`
		BundleSHA256         string `json:"bundle_sha256"`
		ApprovalSHA256       string `json:"approval_sha256"`
		ProfileChanged       bool   `json:"profile_changed"`
		WireChanged          bool   `json:"wire_changed"`
		ReleaseBundleChanged bool   `json:"release_bundle_changed"`
	} `json:"release"`
	Build struct {
		Commit                 string `json:"commit"`
		Tree                   string `json:"tree"`
		SourceArchiveSHA256    string `json:"source_archive_sha256"`
		BinarySHA256           string `json:"binary_sha256"`
		BuildDateUTC           string `json:"build_date_utc"`
		Version                string `json:"version"`
		BuildID                string `json:"build_id"`
		Image                  string `json:"image"`
		ImageID                string `json:"image_id"`
		Platform               string `json:"platform"`
		TargetOverrideSHA256   string `json:"target_override_sha256"`
		FrontendBuildHost      string `json:"frontend_build_host"`
		BackendBuildHost       string `json:"backend_build_host"`
		ImagePackagingHost     string `json:"image_packaging_host"`
		ARM64SourceCompilation bool   `json:"arm64_source_compilation"`
		DMITSourceCompilation  bool   `json:"dmit_source_compilation"`
	} `json:"build"`
	Deployment struct {
		Host                     string   `json:"host"`
		Selector                 string   `json:"selector"`
		ContainerID              string   `json:"container_id"`
		StartedAtUTC             string   `json:"started_at_utc"`
		Health                   string   `json:"health"`
		ComposeFiles             []string `json:"compose_files"`
		ActivationFactSHA256     string   `json:"activation_fact_sha256"`
		ActivationEventID        string   `json:"activation_event_id"`
		ActivationObservedAtUTC  string   `json:"activation_observed_at_utc"`
		DependencyIdentitySHA256 string   `json:"dependency_identity_sha256"`
		DependencyContainers     []string `json:"dependency_containers"`
		DependenciesUnchanged    bool     `json:"dependencies_unchanged"`
		DiskAvailable            string   `json:"disk_available_after_validation"`
	} `json:"deployment"`
	Verification struct {
		LocalGates       []string `json:"local_gates"`
		ResponseContract struct {
			OfficialHeaderName                 string   `json:"official_header_name"`
			XRequestIDAcceptedAsTestSubstitute bool     `json:"x_request_id_accepted_as_test_substitute"`
			ForwardResultUsesFinalResponse     bool     `json:"forward_result_uses_final_response"`
			UnitPaths                          []string `json:"unit_paths"`
		} `json:"response_contract"`
		LiveMatrices    []claudeFWHResponseRequestIDLiveMatrix `json:"live_matrices"`
		StateDurability struct {
			ScriptSHA256                          string `json:"script_sha256"`
			RequestIDHeaderRequiredByOriginalName bool   `json:"request_id_header_required_by_original_name"`
			TitleStateSurvivesRestart             bool   `json:"title_state_survives_restart"`
			PreviousRequestStateSurvivesRestart   bool   `json:"previous_request_state_survives_restart"`
			LongHistoryBytesMinimum               int    `json:"long_history_bytes_minimum"`
			LongHistoryPassed                     bool   `json:"long_history_passed"`
		} `json:"state_durability"`
		Rollback struct {
			Commit           string `json:"commit"`
			ImageID          string `json:"image_id"`
			FullMatrixPassed int    `json:"full_matrix_passed"`
			RestoredImageID  string `json:"restored_image_id"`
			RestoreResult    string `json:"restore_result"`
		} `json:"rollback"`
		RealClients struct {
			ClaudeDesktop struct {
				Mode                    string `json:"mode"`
				Model                   string `json:"model"`
				Turn1Marker             string `json:"turn_1_marker"`
				Turn2Marker             string `json:"turn_2_marker"`
				Turn1GatewayRequestID   string `json:"turn_1_gateway_request_id"`
				Turn2GatewayRequestID   string `json:"turn_2_gateway_request_id"`
				Turn1Status             int    `json:"turn_1_status"`
				Turn2Status             int    `json:"turn_2_status"`
				SameSessionContinuation bool   `json:"same_session_continuation"`
				Result                  string `json:"result"`
			} `json:"claude_desktop"`
			KiloCodeCodeMode struct {
				Model                            string `json:"model"`
				GatewayRequestID                 string `json:"gateway_request_id"`
				Status                           int    `json:"status"`
				Result                           string `json:"result"`
				Reason                           string `json:"reason"`
				ToolBridgeApproval               string `json:"tool_bridge_approval"`
				MatchesDocumentedSupportEnvelope bool   `json:"matches_documented_support_envelope"`
			} `json:"kilocode_code_mode"`
		} `json:"real_clients"`
		Result string `json:"result"`
	} `json:"verification"`
	Safety struct {
		VircsAccessed               bool `json:"vircs_accessed"`
		VircsChanged                bool `json:"vircs_changed"`
		ComposeDownUsed             bool `json:"compose_down_used"`
		UnscopedPruneUsed           bool `json:"unscoped_prune_used"`
		DatabaseOrCacheRecreated    bool `json:"database_or_cache_recreated"`
		DependencyContainersChanged bool `json:"dependency_containers_changed"`
		OnlyDMITSub2APIRecreated    bool `json:"only_dmit_sub2api_recreated"`
		RollbackAndRestoreCompleted bool `json:"rollback_and_restore_completed"`
	} `json:"safety"`
	ProductionState string `json:"production_state"`
	Result          string `json:"result"`
}

func TestClaudeFWHResponseRequestIDAcceptanceIsFrozen(t *testing.T) {
	path := "docs/egress/maintenance/claude-fw-h-response-request-id-acceptance.json"
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		t.Fatal(err)
	}
	if claudeFWHSourceDigest(raw) != claudeFWHResponseRequestIDAcceptanceSHA256 {
		t.Fatal("Claude request-id 最终验收收据摘要不一致")
	}
	for _, forbidden := range []string{"sk-", "access_token", "refresh_token", "authorization"} {
		if strings.Contains(strings.ToLower(string(raw)), forbidden) {
			t.Fatal("Claude request-id 最终验收收据含秘密材料")
		}
	}

	var receipt claudeFWHResponseRequestIDAcceptance
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("Claude request-id 最终验收收据尾部存在额外 JSON")
	}
	validateClaudeFWHResponseRequestIDAcceptanceIdentity(t, receipt)
	validateClaudeFWHResponseRequestIDAcceptanceRuntime(t, receipt)
	validateClaudeFWHResponseRequestIDAcceptanceVerification(t, receipt)
}

func validateClaudeFWHResponseRequestIDAcceptanceIdentity(
	t *testing.T,
	receipt claudeFWHResponseRequestIDAcceptance,
) {
	t.Helper()
	if receipt.SchemaVersion != "official-egress-claude-fw-h-response-request-id-acceptance/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T08:01:19Z" || receipt.Phase != "FW-H" ||
		receipt.AcceptanceID != "claude-code-2.1.226-fw-h-response-request-id-bd1c09d5f-dmit" ||
		receipt.Scope != "official_response_request_id_continuity_correction" ||
		receipt.ProductionState != "active_response_request_id_correction" ||
		receipt.Result != "accepted" {
		t.Fatal("Claude request-id 最终验收顶层身份非法")
	}
	wantPredecessors := map[string]string{
		"docs/egress/maintenance/claude-fw-h-final-acceptance-package.json":              claudeFWHFinalAcceptanceSHA256,
		"docs/egress/maintenance/claude-fw-h-response-request-id-source-transition.json": claudeFWHResponseRequestIDSourceSHA256,
		"docs/egress/maintenance/claude-fw-h-response-request-id-test-transition.json":   claudeFWHResponseRequestIDTestSHA256,
	}
	if len(receipt.Predecessors) != len(wantPredecessors) {
		t.Fatal("Claude request-id 最终验收前序数量非法")
	}
	paths := make([]string, 0, len(receipt.Predecessors))
	for _, predecessor := range receipt.Predecessors {
		want, ok := wantPredecessors[predecessor.Path]
		if !ok || predecessor.SHA256 != want || strings.TrimSpace(predecessor.Kind) == "" {
			t.Fatal("Claude request-id 最终验收前序引用非法")
		}
		predecessorRaw, err := os.ReadFile(filepath.Join(
			"../../..", filepath.FromSlash(predecessor.Path),
		))
		if err != nil || claudeFWHSourceDigest(predecessorRaw) != predecessor.SHA256 {
			t.Fatal("Claude request-id 最终验收前序摘要不一致")
		}
		paths = append(paths, predecessor.Path)
	}
	if !slices.IsSorted(paths) {
		t.Fatal("Claude request-id 最终验收前序未排序")
	}
	if receipt.Release.Version != ClaudeFWGVersion ||
		receipt.Release.ProfileSHA256 != ClaudeFWGProfileDigest ||
		receipt.Release.WireSHA256 != claudeFWGWireDigest ||
		receipt.Release.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		receipt.Release.BundleSHA256 != ClaudeFWGBundleDigest ||
		receipt.Release.ApprovalSHA256 != ClaudeFWHLegacyRetirementApprovalDigest ||
		receipt.Release.ProfileChanged || receipt.Release.WireChanged ||
		receipt.Release.ReleaseBundleChanged {
		t.Fatal("Claude request-id 最终验收 Release 身份非法")
	}
}

func validateClaudeFWHResponseRequestIDAcceptanceRuntime(
	t *testing.T,
	receipt claudeFWHResponseRequestIDAcceptance,
) {
	t.Helper()
	build := receipt.Build
	if build.Commit != "bd1c09d5f7b531fae1e0e8d22a56135017cb1420" ||
		build.Tree != "8925bc7224646855e0542d7b8d12cf7a2f518036" ||
		build.SourceArchiveSHA256 != "1be021203291849ba1f15c52fa833cd6730be520cb18a2cdbf5d823b37e385e7" ||
		build.BinarySHA256 != "0cea841d579549f7d01f490457144cb92d04973088676098348bef757cec2b14" ||
		build.BuildDateUTC != "2026-08-22T07:43:09Z" ||
		build.Version != "0.1.177-4-fw-h-final-bd1c09d5f" ||
		build.BuildID != "sub2api-v0.1.177-4-fw-h-final-bd1c09d5f" ||
		build.Image != "sub2apiplus:fw-h-final-bd1c09d5f" ||
		build.ImageID != "sha256:8117aa6cf7a8c83d5c7ca6536154fa4e4acd2ad273a0fddb617d80656bd62c85" ||
		build.Platform != "linux/amd64" || len(build.TargetOverrideSHA256) != 64 ||
		build.FrontendBuildHost != "local-mac-native" ||
		build.BackendBuildHost != "local-mac-cross-linux-amd64" ||
		build.ImagePackagingHost != "ARM64" || build.ARM64SourceCompilation ||
		build.DMITSourceCompilation {
		t.Fatal("Claude request-id 最终验收构建身份非法")
	}
	deployment := receipt.Deployment
	if deployment.Host != "DMIT" || deployment.Selector != "active" ||
		len(deployment.ContainerID) != 64 || deployment.Health != "healthy" ||
		len(deployment.ActivationFactSHA256) != 64 || len(deployment.ActivationEventID) != 64 ||
		len(deployment.DependencyIdentitySHA256) != 64 || !deployment.DependenciesUnchanged ||
		deployment.DiskAvailable != "4.8G" || !slices.Equal(deployment.ComposeFiles, []string{
		"/root/Docker/sub2apiplus/app/docker-compose.yml",
		"/root/sub2apiplus-fw-h-final-bd1c09d5f/private/target.override.yml",
	}) || !slices.Equal(deployment.DependencyContainers, []string{
		"3x-ui", "mihomo", "sub2apiplus-keeper", "sub2apiplus-postgres", "sub2apiplus-redis",
	}) {
		t.Fatal("Claude request-id 最终验收 DMIT 运行身份非法")
	}
	if receipt.Safety.VircsAccessed || receipt.Safety.VircsChanged ||
		receipt.Safety.ComposeDownUsed || receipt.Safety.UnscopedPruneUsed ||
		receipt.Safety.DatabaseOrCacheRecreated || receipt.Safety.DependencyContainersChanged ||
		!receipt.Safety.OnlyDMITSub2APIRecreated || !receipt.Safety.RollbackAndRestoreCompleted {
		t.Fatal("Claude request-id 最终验收安全边界非法")
	}
}

func validateClaudeFWHResponseRequestIDAcceptanceVerification(
	t *testing.T,
	receipt claudeFWHResponseRequestIDAcceptance,
) {
	t.Helper()
	verification := receipt.Verification
	if verification.Result != "passed" || len(verification.LocalGates) != 6 ||
		verification.ResponseContract.OfficialHeaderName != "request-id" ||
		verification.ResponseContract.XRequestIDAcceptedAsTestSubstitute ||
		!verification.ResponseContract.ForwardResultUsesFinalResponse ||
		!slices.Equal(verification.ResponseContract.UnitPaths, []string{
			"messages-stream", "messages-non-stream", "messages-stream-fallback", "messages-count-tokens",
		}) {
		t.Fatal("Claude request-id 最终验收本地门禁非法")
	}
	wantMatrices := map[string]struct {
		passed   int
		restarts int
		digest   string
	}{
		"active-before-rollback":                  {46, 0, "fe2d469611e34de8e88fd6a6fff4a78eff23c9263791dc12f3ab0d17a7f71666"},
		"active-state-durability-before-rollback": {4, 2, "1cdee8f1f8b0f0eb61b86442822841a9e9c959355d0747dc36498f3b24170ed3"},
		"rollback-e2c80213a":                      {46, 0, "91a9e06c56c528d8ba229d050deed0fa2ab491244365c86709be36acab5a54a1"},
		"active-after-restore":                    {46, 0, "8f5a0f68d491617c334701e29bbad9bccf8c60b48c1f4043a864b5944ec2b298"},
		"active-state-durability-after-restore":   {4, 2, "2c487430b33f809912bfbfc3e0465b4b4f9d3cdfa66756ef06eca10a4dc63f3e"},
	}
	if len(verification.LiveMatrices) != len(wantMatrices) {
		t.Fatal("Claude request-id 最终验收实时矩阵数量非法")
	}
	for _, matrix := range verification.LiveMatrices {
		want, ok := wantMatrices[matrix.Name]
		if !ok || matrix.Passed != want.passed || matrix.Restarts != want.restarts ||
			matrix.LogSHA256 != want.digest || matrix.StartedAtUTC == "" || matrix.FinishedAtUTC == "" {
			t.Fatal("Claude request-id 最终验收实时矩阵非法")
		}
		delete(wantMatrices, matrix.Name)
	}
	if len(wantMatrices) != 0 {
		t.Fatal("Claude request-id 最终验收实时矩阵未闭合")
	}
	state := verification.StateDurability
	if state.ScriptSHA256 != "94fad77d304eaa07b6752b048fadc485ef825c26eb3f229217a6306f7782a7da" ||
		!state.RequestIDHeaderRequiredByOriginalName || !state.TitleStateSurvivesRestart ||
		!state.PreviousRequestStateSurvivesRestart || state.LongHistoryBytesMinimum != 347000 ||
		!state.LongHistoryPassed {
		t.Fatal("Claude request-id 最终验收状态耐久性非法")
	}
	rollback := verification.Rollback
	if rollback.Commit != "e2c80213ab338cc6f91eee00e28a96bc956f0512" ||
		rollback.ImageID != "sha256:356384ce0429a2dc30d484648371d1921644184fc978195ae583c23e968dc3d6" ||
		rollback.FullMatrixPassed != 46 || rollback.RestoredImageID != receipt.Build.ImageID ||
		rollback.RestoreResult != "passed" {
		t.Fatal("Claude request-id 最终验收回滚恢复非法")
	}
	desktop := verification.RealClients.ClaudeDesktop
	if desktop.Mode != "Code" || desktop.Model != "claude-sonnet-5" ||
		desktop.Turn1Marker != "DESKTOP_REQUEST_ID_TURN1_OK" ||
		desktop.Turn2Marker != "DESKTOP_REQUEST_ID_TURN2_OK" ||
		desktop.Turn1Status != 200 || desktop.Turn2Status != 200 ||
		!desktop.SameSessionContinuation || desktop.Result != "passed" ||
		desktop.Turn1GatewayRequestID == desktop.Turn2GatewayRequestID {
		t.Fatal("Claude request-id 最终验收 Desktop 实测非法")
	}
	kilo := verification.RealClients.KiloCodeCodeMode
	if kilo.Model != "claude-sonnet-5" || kilo.Status != 400 ||
		kilo.Result != "expected_fail_close" ||
		kilo.Reason != "Claude 动态工具目录不接受未实测 tool_choice" ||
		kilo.ToolBridgeApproval != "not_approved" || !kilo.MatchesDocumentedSupportEnvelope {
		t.Fatal("Claude request-id 最终验收 KiloCode 边界事实非法")
	}
}
