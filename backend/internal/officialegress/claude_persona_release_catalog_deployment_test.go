package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

const (
	claudePersonaCatalogDeploymentFactPath = "docs/egress/maintenance/claude-persona-release-catalog-deployment-fact.json"
	claudePersonaCatalogDeploymentFactSHA  = "766974e7e10d917c4b8691964c50ac4a44b436a2eff072737b6f7d8435b714d2"
	claudePersonaCatalogAcceptancePath     = "docs/egress/maintenance/claude-persona-release-catalog-production-acceptance.json"
	claudePersonaCatalogAcceptanceSHA      = "ff81b254423202c5f1795c7c1f7978a46671fe9720fc454728e73c27f86fd965"
)

var claudePersonaCatalogCloseoutLedgerFiles = map[string]string{
	"docs/egress/maintenance/claude-fw-h-response-request-id-source-transition.json":    "b4febb67d2690fe2a95c47258b66b6e5d7dea8b551f3635a281073709ae76b76",
	"docs/egress/maintenance/claude-fw-h-response-request-id-test-transition.json":      "cb540e6053aa693a9de3b210094f5a517bff2c430b5c9c49ade01458ba1cdeab",
	"docs/egress/maintenance/claude-fw-h-state-durability-source-transition.json":       "4d529a58d06a8a597da17b477c17fb5ee99dc6e2b6728e8adeaf3afb98f0fa60",
	"docs/egress/maintenance/claude-fw-h-state-durability-test-transition.json":         "5ab18f3424aa6b2300ae865999019c586814896a0d805c6fed6a48eaa7647d40",
	"docs/egress/maintenance/claude-persona-release-catalog-production-acceptance.json": claudePersonaCatalogAcceptanceSHA,
	"docs/egress/maintenance/claude-persona-release-catalog-transition.json":            claudePersonaReleaseCatalogTransitionSHA256,
}

type claudePersonaCatalogDeploymentFact struct {
	SchemaVersion string `json:"schema_version"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	EventID       string `json:"event_id"`
	Phase         string `json:"phase"`
	Scope         string `json:"scope"`
	Stage         string `json:"stage"`
	Release       struct {
		Version        string `json:"version"`
		CatalogSHA256  string `json:"catalog_sha256"`
		ProfileSHA256  string `json:"profile_sha256"`
		WireSHA256     string `json:"wire_sha256"`
		ReleaseSHA256  string `json:"release_sha256"`
		BundleSHA256   string `json:"bundle_sha256"`
		ApprovalSHA256 string `json:"approval_sha256"`
	} `json:"release"`
	Build struct {
		Commit           string `json:"commit"`
		Tree             string `json:"tree"`
		SourceTreeSHA256 string `json:"source_tree_sha256"`
		BinarySHA256     string `json:"binary_sha256"`
		BuildID          string `json:"build_id"`
		Version          string `json:"version"`
		ImageID          string `json:"image_id"`
		ImageReference   string `json:"image_reference"`
		Platform         string `json:"platform"`
	} `json:"build"`
	Deployment struct {
		Host                           string   `json:"host"`
		Selector                       string   `json:"selector"`
		ContainerID                    string   `json:"container_id"`
		Health                         string   `json:"health"`
		RestartCount                   int      `json:"restart_count"`
		ComposeFiles                   []string `json:"compose_files"`
		ComposeOverrideSHA256          string   `json:"compose_override_sha256"`
		ActivationFactSHA256           string   `json:"activation_fact_sha256"`
		ActivationEventID              string   `json:"activation_event_id"`
		DependencyIdentityBeforeSHA256 string   `json:"dependency_identity_before_sha256"`
		DependencyIdentityAfterSHA256  string   `json:"dependency_identity_after_sha256"`
		DependenciesUnchanged          bool     `json:"dependencies_unchanged"`
		OnlyApplicationRecreated       bool     `json:"only_application_container_recreated"`
	} `json:"deployment"`
	Validation struct {
		CurrentLiveMatrix struct {
			Models []string `json:"models"`
			Passed int      `json:"passed"`
			Failed int      `json:"failed"`
		} `json:"current_live_matrix"`
		FableSameReleaseEvidence struct {
			CurrentLiveProbePerformed bool   `json:"current_live_probe_performed"`
			ReleaseBundleChanged      bool   `json:"release_bundle_changed"`
			SupportEnvelopeChanged    bool   `json:"support_envelope_changed"`
			AcceptanceSHA256          string `json:"acceptance_sha256"`
			PriorActiveMatrixSHA256   string `json:"prior_active_matrix_sha256"`
		} `json:"fable_same_release_evidence"`
		StabilityObservation struct {
			StableSeconds     int `json:"stable_seconds"`
			FatalCount        int `json:"fatal_count"`
			PanicCount        int `json:"panic_count"`
			GuardFailureCount int `json:"guard_failure_count"`
			RestartCount      int `json:"restart_count"`
		} `json:"stability_observation"`
		Result string `json:"result"`
	} `json:"validation"`
	Rollback struct {
		ImageID                    string `json:"image_id"`
		RetainedOnDMIT             bool   `json:"retained_on_dmit"`
		PreviousFullMatrixPassed   int    `json:"previous_full_matrix_passed"`
		NewRollbackSwitchPerformed bool   `json:"new_rollback_switch_performed"`
	} `json:"rollback"`
	Safety struct {
		VircsAccessed                        bool `json:"vircs_accessed"`
		VircsChanged                         bool `json:"vircs_changed"`
		AccountStateChanged                  bool `json:"account_state_changed"`
		ComposeDownUsed                      bool `json:"compose_down_used"`
		UnscopedPruneUsed                    bool `json:"unscoped_prune_used"`
		DatabaseOrCacheRecreated             bool `json:"database_or_cache_recreated"`
		DependencyContainersChanged          bool `json:"dependency_containers_changed"`
		ProductionLogicChangedDuringCloseout bool `json:"production_logic_changed_during_closeout"`
	} `json:"safety"`
	ProductionState string `json:"production_state"`
	Result          string `json:"result"`
}

type claudePersonaCatalogProductionAcceptance struct {
	SchemaVersion  string                                     `json:"schema_version"`
	IssuedAtUTC    string                                     `json:"issued_at_utc"`
	Phase          string                                     `json:"phase"`
	AcceptanceID   string                                     `json:"acceptance_id"`
	Scope          string                                     `json:"scope"`
	Predecessors   []claudePersonaReleaseCatalogTransitionRef `json:"predecessors"`
	DeploymentFact struct {
		Path       string `json:"path"`
		SHA256     string `json:"sha256"`
		RemotePath string `json:"remote_path"`
		RemoteMode string `json:"remote_mode"`
		EventID    string `json:"event_id"`
		Stage      string `json:"stage"`
	} `json:"deployment_fact"`
	Release struct {
		Version                string `json:"version"`
		CatalogSHA256          string `json:"catalog_sha256"`
		ProfileSHA256          string `json:"profile_sha256"`
		WireSHA256             string `json:"wire_sha256"`
		ReleaseSHA256          string `json:"release_sha256"`
		BundleSHA256           string `json:"bundle_sha256"`
		ApprovalSHA256         string `json:"approval_sha256"`
		FinalWireChanged       bool   `json:"final_wire_changed"`
		SupportEnvelopeChanged bool   `json:"support_envelope_changed"`
	} `json:"release"`
	Production struct {
		Host                           string   `json:"host"`
		Commit                         string   `json:"commit"`
		Tree                           string   `json:"tree"`
		ImageID                        string   `json:"image_id"`
		ImageReference                 string   `json:"image_reference"`
		Platform                       string   `json:"platform"`
		ContainerID                    string   `json:"container_id"`
		Health                         string   `json:"health"`
		RestartCount                   int      `json:"restart_count"`
		ComposeFiles                   []string `json:"compose_files"`
		ComposeOverrideSHA256          string   `json:"compose_override_sha256"`
		ActivationFactSHA256           string   `json:"activation_fact_sha256"`
		ActivationEventID              string   `json:"activation_event_id"`
		DependencyIdentityBeforeSHA256 string   `json:"dependency_identity_before_sha256"`
		DependencyIdentityAfterSHA256  string   `json:"dependency_identity_after_sha256"`
		DependenciesUnchanged          bool     `json:"dependencies_unchanged"`
	} `json:"production"`
	Verification struct {
		LocalGates           []string `json:"local_gates"`
		DiagnosticCorrection struct {
			RuntimeLogicChanged      bool   `json:"runtime_logic_changed"`
			OldRunnerSHA256          string `json:"old_runner_sha256"`
			NewRunnerSHA256          string `json:"new_runner_sha256"`
			AccountOrDatabaseChanged bool   `json:"account_or_database_changed"`
		} `json:"diagnostic_correction"`
		CurrentLiveMatrix struct {
			Models []string `json:"models"`
			Passed int      `json:"passed"`
			Failed int      `json:"failed"`
		} `json:"current_live_matrix"`
		Fable struct {
			CurrentLiveProbePerformed bool   `json:"current_live_probe_performed"`
			SameReleaseAcceptanceSHA  string `json:"same_release_acceptance_sha256"`
			SameReleasePriorMatrixSHA string `json:"same_release_prior_full_matrix_sha256"`
		} `json:"fable"`
		Rollback struct {
			ImageID               string `json:"image_id"`
			RetainedOnDMIT        bool   `json:"retained_on_dmit"`
			PriorFullMatrixPassed int    `json:"prior_full_matrix_passed"`
			NewSwitchPerformed    bool   `json:"new_switch_performed"`
		} `json:"rollback"`
		Stability struct {
			StableSeconds     int `json:"stable_seconds"`
			FatalCount        int `json:"fatal_count"`
			PanicCount        int `json:"panic_count"`
			GuardFailureCount int `json:"guard_failure_count"`
			RestartCount      int `json:"restart_count"`
		} `json:"stability"`
		Result string `json:"result"`
	} `json:"verification"`
	Transitions []claudePersonaReleaseCatalogTransitionEntry `json:"transitions"`
	Safety      struct {
		VircsAccessed                        bool `json:"vircs_accessed"`
		VircsChanged                         bool `json:"vircs_changed"`
		AccountStateChanged                  bool `json:"account_state_changed"`
		ComposeDownUsed                      bool `json:"compose_down_used"`
		UnscopedPruneUsed                    bool `json:"unscoped_prune_used"`
		DatabaseOrCacheRecreated             bool `json:"database_or_cache_recreated"`
		DependencyContainersChanged          bool `json:"dependency_containers_changed"`
		OnlyDMITApplicationRecreated         bool `json:"only_dmit_application_recreated"`
		ProductionLogicChangedDuringCloseout bool `json:"production_logic_changed_during_closeout"`
		SecretScan                           struct {
			MatchedPatterns int    `json:"matched_patterns"`
			Result          string `json:"result"`
		} `json:"secret_scan"`
	} `json:"safety"`
	ProductionState string `json:"production_state"`
	Result          string `json:"result"`
}

func readFrozenClaudePersonaCatalogJSON(path string, wantSHA string, out any) error {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != wantSHA {
		return errors.New("Claude Persona Catalog 生产收据摘要不一致")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	if err := decoder.Decode(out); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return errors.New("Claude Persona Catalog 生产收据尾部存在额外 JSON")
	}
	return nil
}

func validateClaudePersonaCatalogDeploymentFact(fact claudePersonaCatalogDeploymentFact) error {
	if fact.SchemaVersion != "official-egress-claude-persona-release-catalog-deployment-fact/v1" ||
		fact.IssuedAtUTC != "2026-08-22T10:41:02Z" || fact.Phase != "FW-H" ||
		fact.Scope != "claude-persona-release-catalog-production-activation" ||
		fact.Stage != "active" || fact.Result != "accepted" ||
		fact.ProductionState != "active_persona_release_catalog" || !receiptSHA256(fact.EventID) {
		return errors.New("Claude Persona Catalog DeploymentFact 顶层事实非法")
	}
	if fact.Release.Version != ClaudeFWGVersion ||
		fact.Release.CatalogSHA256 != "3f47ee0d89a13478bcce121b956c8e3554685cbffeb47530080684654a1e0fa7" ||
		fact.Release.ProfileSHA256 != ClaudeFWGProfileDigest ||
		fact.Release.WireSHA256 != claudeFWGWireDigest ||
		fact.Release.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		fact.Release.BundleSHA256 != ClaudeFWGBundleDigest ||
		fact.Release.ApprovalSHA256 != ClaudeFWHLegacyRetirementApprovalDigest {
		return errors.New("Claude Persona Catalog DeploymentFact Release 非法")
	}
	if fact.Build.Commit != "4ea8f73e5b36ce294d71751d795669c3778327cd" ||
		fact.Build.Tree != "649542d7d388156f52919aab2c940cba245c84a7" ||
		fact.Build.ImageID != "sha256:5c48094d4007bf402f71a9b5d4509b2db5e29378f3758c386d6cb2e2ed61e565" ||
		fact.Build.Platform != "linux/amd64" || !receiptSHA256(fact.Build.SourceTreeSHA256) ||
		!receiptSHA256(fact.Build.BinarySHA256) {
		return errors.New("Claude Persona Catalog DeploymentFact 构建身份非法")
	}
	if fact.Deployment.Host != "DMIT" || fact.Deployment.Selector != "active" ||
		fact.Deployment.Health != "healthy" || fact.Deployment.RestartCount != 0 ||
		len(fact.Deployment.ComposeFiles) != 2 ||
		fact.Deployment.ComposeOverrideSHA256 != "9e1d1965e8a287c3a599c0d30aceb61ff7eaaf717f7ae36594a664c2d74ca57e" ||
		fact.Deployment.ActivationFactSHA256 != "31bcd0576240e78f0a6f005d875b8748b8835a08bc94a8a4ffed3a08e74f77d2" ||
		fact.Deployment.DependencyIdentityBeforeSHA256 != fact.Deployment.DependencyIdentityAfterSHA256 ||
		!fact.Deployment.DependenciesUnchanged || !fact.Deployment.OnlyApplicationRecreated {
		return errors.New("Claude Persona Catalog DeploymentFact 部署事实非法")
	}
	wantModels := []string{"claude-sonnet-5", "claude-opus-5"}
	if !slices.Equal(fact.Validation.CurrentLiveMatrix.Models, wantModels) ||
		fact.Validation.CurrentLiveMatrix.Passed != 37 || fact.Validation.CurrentLiveMatrix.Failed != 0 ||
		fact.Validation.FableSameReleaseEvidence.CurrentLiveProbePerformed ||
		fact.Validation.FableSameReleaseEvidence.ReleaseBundleChanged ||
		fact.Validation.FableSameReleaseEvidence.SupportEnvelopeChanged ||
		fact.Validation.FableSameReleaseEvidence.AcceptanceSHA256 != "16dc60bc46eede747cb0535e53367c134a28466f12399f06a485693e142b15a9" ||
		fact.Validation.FableSameReleaseEvidence.PriorActiveMatrixSHA256 != "722df263723bbf64ebc74f9470d4c9725b9e92710feda4a3b55bfa8eb4ad8668" ||
		fact.Validation.StabilityObservation.StableSeconds < 600 ||
		fact.Validation.StabilityObservation.FatalCount != 0 ||
		fact.Validation.StabilityObservation.PanicCount != 0 ||
		fact.Validation.StabilityObservation.GuardFailureCount != 0 ||
		fact.Validation.StabilityObservation.RestartCount != 0 || fact.Validation.Result != "passed" {
		return errors.New("Claude Persona Catalog DeploymentFact 验收事实非法")
	}
	if fact.Rollback.ImageID != "sha256:356384ce0429a2dc30d484648371d1921644184fc978195ae583c23e968dc3d6" ||
		!fact.Rollback.RetainedOnDMIT || fact.Rollback.PreviousFullMatrixPassed != 46 ||
		fact.Rollback.NewRollbackSwitchPerformed {
		return errors.New("Claude Persona Catalog DeploymentFact 回退事实非法")
	}
	if fact.Safety.VircsAccessed || fact.Safety.VircsChanged || fact.Safety.AccountStateChanged ||
		fact.Safety.ComposeDownUsed || fact.Safety.UnscopedPruneUsed ||
		fact.Safety.DatabaseOrCacheRecreated || fact.Safety.DependencyContainersChanged ||
		fact.Safety.ProductionLogicChangedDuringCloseout {
		return errors.New("Claude Persona Catalog DeploymentFact 安全边界非法")
	}
	return nil
}

func loadClaudePersonaCatalogProductionAcceptance() (claudePersonaCatalogProductionAcceptance, error) {
	var receipt claudePersonaCatalogProductionAcceptance
	if err := readFrozenClaudePersonaCatalogJSON(
		claudePersonaCatalogAcceptancePath, claudePersonaCatalogAcceptanceSHA, &receipt,
	); err != nil {
		return receipt, err
	}
	return receipt, validateClaudePersonaCatalogProductionAcceptance(receipt)
}

func validateClaudePersonaCatalogProductionAcceptance(
	receipt claudePersonaCatalogProductionAcceptance,
) error {
	if receipt.SchemaVersion != "official-egress-claude-persona-release-catalog-production-acceptance/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T10:44:48Z" || receipt.Phase != "FW-H" ||
		receipt.AcceptanceID != "claude-code-2.1.226-persona-release-catalog-4ea8f73e5-dmit" ||
		receipt.Scope != "claude-persona-release-catalog-production-documentation-closeout" ||
		receipt.ProductionState != "active_persona_release_catalog" || receipt.Result != "accepted" {
		return errors.New("Claude Persona Catalog 生产验收顶层事实非法")
	}
	wantPredecessors := map[string]string{
		"persona_release_catalog_transition\x00docs/egress/maintenance/claude-persona-release-catalog-transition.json":                                  "16be5939a3c14dd9bc5a7e717583173939ac4d1df4b217e2d15f229d54b21288",
		"fw_h_response_request_id_acceptance\x00docs/egress/maintenance/claude-fw-h-response-request-id-acceptance.json":                                "722df263723bbf64ebc74f9470d4c9725b9e92710feda4a3b55bfa8eb4ad8668",
		"three_model_acceptance\x00docs/egress/maintenance/claude-fw-g-three-model-acceptance.json":                                                     "16dc60bc46eede747cb0535e53367c134a28466f12399f06a485693e142b15a9",
		"production_approval\x00backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-legacy-retirement-approval.json": ClaudeFWHLegacyRetirementApprovalDigest,
	}
	for _, predecessor := range receipt.Predecessors {
		key := predecessor.Kind + "\x00" + predecessor.Path
		if wantPredecessors[key] != predecessor.SHA256 {
			return errors.New("Claude Persona Catalog 生产验收前序非法")
		}
		delete(wantPredecessors, key)
	}
	if len(wantPredecessors) != 0 {
		return errors.New("Claude Persona Catalog 生产验收前序不闭合")
	}
	if receipt.DeploymentFact.Path != claudePersonaCatalogDeploymentFactPath ||
		receipt.DeploymentFact.SHA256 != claudePersonaCatalogDeploymentFactSHA ||
		receipt.DeploymentFact.RemoteMode != "0400" ||
		receipt.DeploymentFact.Stage != "active" {
		return errors.New("Claude Persona Catalog 生产验收 DeploymentFact 非法")
	}
	var fact claudePersonaCatalogDeploymentFact
	if err := readFrozenClaudePersonaCatalogJSON(
		claudePersonaCatalogDeploymentFactPath, claudePersonaCatalogDeploymentFactSHA, &fact,
	); err != nil {
		return err
	}
	if err := validateClaudePersonaCatalogDeploymentFact(fact); err != nil ||
		receipt.DeploymentFact.EventID != fact.EventID {
		return errors.New("Claude Persona Catalog 生产验收未绑定合法 DeploymentFact")
	}
	if receipt.Release.Version != ClaudeFWGVersion ||
		receipt.Release.CatalogSHA256 != fact.Release.CatalogSHA256 ||
		receipt.Release.ProfileSHA256 != fact.Release.ProfileSHA256 ||
		receipt.Release.WireSHA256 != fact.Release.WireSHA256 ||
		receipt.Release.ReleaseSHA256 != fact.Release.ReleaseSHA256 ||
		receipt.Release.BundleSHA256 != fact.Release.BundleSHA256 ||
		receipt.Release.ApprovalSHA256 != fact.Release.ApprovalSHA256 ||
		receipt.Release.FinalWireChanged || receipt.Release.SupportEnvelopeChanged {
		return errors.New("Claude Persona Catalog 生产验收 Release 非法")
	}
	if receipt.Production.Host != fact.Deployment.Host || receipt.Production.Commit != fact.Build.Commit ||
		receipt.Production.Tree != fact.Build.Tree || receipt.Production.ImageID != fact.Build.ImageID ||
		receipt.Production.Platform != fact.Build.Platform || receipt.Production.ContainerID != fact.Deployment.ContainerID ||
		receipt.Production.Health != "healthy" || receipt.Production.RestartCount != 0 ||
		receipt.Production.ComposeOverrideSHA256 != fact.Deployment.ComposeOverrideSHA256 ||
		receipt.Production.ActivationFactSHA256 != fact.Deployment.ActivationFactSHA256 ||
		receipt.Production.DependencyIdentityBeforeSHA256 != receipt.Production.DependencyIdentityAfterSHA256 ||
		!receipt.Production.DependenciesUnchanged {
		return errors.New("Claude Persona Catalog 生产验收运行身份非法")
	}
	wantModels := []string{"claude-sonnet-5", "claude-opus-5"}
	if len(receipt.Verification.LocalGates) != 5 ||
		receipt.Verification.DiagnosticCorrection.RuntimeLogicChanged ||
		receipt.Verification.DiagnosticCorrection.AccountOrDatabaseChanged ||
		!receiptSHA256(receipt.Verification.DiagnosticCorrection.OldRunnerSHA256) ||
		!receiptSHA256(receipt.Verification.DiagnosticCorrection.NewRunnerSHA256) ||
		!slices.Equal(receipt.Verification.CurrentLiveMatrix.Models, wantModels) ||
		receipt.Verification.CurrentLiveMatrix.Passed != 37 ||
		receipt.Verification.CurrentLiveMatrix.Failed != 0 ||
		receipt.Verification.Fable.CurrentLiveProbePerformed ||
		receipt.Verification.Fable.SameReleaseAcceptanceSHA != "16dc60bc46eede747cb0535e53367c134a28466f12399f06a485693e142b15a9" ||
		receipt.Verification.Fable.SameReleasePriorMatrixSHA != "722df263723bbf64ebc74f9470d4c9725b9e92710feda4a3b55bfa8eb4ad8668" ||
		!receipt.Verification.Rollback.RetainedOnDMIT ||
		receipt.Verification.Rollback.PriorFullMatrixPassed != 46 ||
		receipt.Verification.Rollback.NewSwitchPerformed ||
		receipt.Verification.Stability.StableSeconds < 600 ||
		receipt.Verification.Stability.FatalCount != 0 ||
		receipt.Verification.Stability.PanicCount != 0 ||
		receipt.Verification.Stability.GuardFailureCount != 0 ||
		receipt.Verification.Stability.RestartCount != 0 || receipt.Verification.Result != "passed" {
		return errors.New("Claude Persona Catalog 生产验收矩阵非法")
	}
	if len(receipt.Transitions) != 2 {
		return errors.New("Claude Persona Catalog 生产验收文档迁移不完整")
	}
	transitionPaths := make([]string, 0, len(receipt.Transitions))
	for _, transition := range receipt.Transitions {
		if !receiptSHA256(transition.FromSHA256) || !receiptSHA256(transition.ToSHA256) ||
			transition.FromSHA256 == transition.ToSHA256 ||
			!claudePersonaCatalogDocumentMatches(transition.Path, transition.ToSHA256) {
			return errors.New("Claude Persona Catalog 生产验收文档迁移非法")
		}
		transitionPaths = append(transitionPaths, transition.Path)
	}
	if !slices.IsSorted(transitionPaths) {
		return errors.New("Claude Persona Catalog 生产验收文档迁移未排序")
	}
	if receipt.Safety.VircsAccessed || receipt.Safety.VircsChanged ||
		receipt.Safety.AccountStateChanged || receipt.Safety.ComposeDownUsed ||
		receipt.Safety.UnscopedPruneUsed || receipt.Safety.DatabaseOrCacheRecreated ||
		receipt.Safety.DependencyContainersChanged || !receipt.Safety.OnlyDMITApplicationRecreated ||
		receipt.Safety.ProductionLogicChangedDuringCloseout ||
		receipt.Safety.SecretScan.MatchedPatterns != 0 || receipt.Safety.SecretScan.Result != "passed" {
		return errors.New("Claude Persona Catalog 生产验收安全边界非法")
	}
	return nil
}

func claudePersonaCatalogDocumentMatches(path string, wantSHA string) bool {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	current := hex.EncodeToString(sum[:])
	return current == wantSHA ||
		claudeOfficialClientOnlyTransitionSupersedes(path, wantSHA, current)
}

func claudePersonaReleaseCatalogProductionAcceptanceSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if !receiptSHA256(priorDigest) || !receiptSHA256(currentDigest) {
		return false
	}
	if claudeOfficialClientOnlyTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadClaudePersonaCatalogProductionAcceptance()
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

func claudePersonaCatalogCloseoutTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if !receiptSHA256(priorDigest) || !receiptSHA256(currentDigest) {
		return false
	}
	if claudeFWHImmutableTransitionLedgerSupersedesBeforeOfficialClientOnly(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	graph := make(map[string][]string)
	for receiptPath, wantSHA := range claudePersonaCatalogCloseoutLedgerFiles {
		raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(receiptPath)))
		if err != nil {
			return false
		}
		sum := sha256.Sum256(raw)
		if hex.EncodeToString(sum[:]) != wantSHA {
			return false
		}
		var receipt struct {
			Transitions []claudePersonaReleaseCatalogTransitionEntry `json:"transitions"`
		}
		if json.Unmarshal(raw, &receipt) != nil {
			return false
		}
		for _, transition := range receipt.Transitions {
			if transition.Path == path && receiptSHA256(transition.FromSHA256) &&
				receiptSHA256(transition.ToSHA256) && transition.FromSHA256 != transition.ToSHA256 {
				graph[transition.FromSHA256] = append(graph[transition.FromSHA256], transition.ToSHA256)
			}
		}
	}
	queue := []string{priorDigest}
	visited := map[string]struct{}{priorDigest: {}}
	for len(queue) != 0 {
		from := queue[0]
		queue = queue[1:]
		if from == currentDigest ||
			claudeFWHImmutableTransitionLedgerSupersedesBeforeOfficialClientOnly(
				path, from, currentDigest,
			) {
			return true
		}
		for edgeFrom, targets := range graph {
			if from != edgeFrom &&
				!claudeFWHImmutableTransitionLedgerSupersedesBeforeOfficialClientOnly(
					path, from, edgeFrom,
				) {
				continue
			}
			for _, target := range targets {
				if _, exists := visited[target]; exists {
					continue
				}
				visited[target] = struct{}{}
				queue = append(queue, target)
			}
		}
	}
	return false
}

func TestClaudePersonaReleaseCatalogProductionAcceptanceIsFrozen(t *testing.T) {
	if _, err := loadClaudePersonaCatalogProductionAcceptance(); err != nil {
		t.Fatal(err)
	}
}
