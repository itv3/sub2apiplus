package officialegress

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"
	"time"
)

const (
	// ClaudeFWHInitialProductionApprovalDigest 固定首次生产迁移批准，历史验收与
	// 收据只能继续引用它，后继退休批准不得覆盖该事实。
	ClaudeFWHInitialProductionApprovalDigest = "73e690d20fdb36688d70247d4c857f6aa31ebfc6dcd755fbbcc8276e37d74f1a"
	// ClaudeFWHLegacyRetirementApprovalDigest 固定历史六入口退休批准。
	// 后继 active Approval 改变时，历史收据仍只能引用该不可变摘要。
	ClaudeFWHLegacyRetirementApprovalDigest = "1d002a2708b198256098999932908d47f0eb90a2c819f11d8393570f33996c2c"
)

// ClaudeFWHProductionApprovalDigest 始终表示当前 production_active selector
// 实际选择的批准摘要；历史收据必须使用各自的固定常量。
var ClaudeFWHProductionApprovalDigest = DefaultClaudeReleaseCatalog().ProductionActive().ApprovalDigest()

func ClaudeFWGWireDigest() string {
	return DefaultClaudeReleaseCatalog().ValidationCandidate().Release().WireDigest()
}

// ResolvedClaudeProductionRelease 是 Claude 自有发布源向共享运行时暴露的只读坐标。
// 首次 FW-H rollback 是遗留部署，因此这里刻意不伪造 rollback Release。
type ResolvedClaudeProductionRelease struct {
	selection      ResolvedClaudeReleaseSelection
	version        string
	profileDigest  string
	wireDigest     string
	releaseDigest  string
	bundleDigest   string
	approvalDigest string
}

func (r ResolvedClaudeProductionRelease) Version() string        { return r.version }
func (r ResolvedClaudeProductionRelease) ProfileDigest() string  { return r.profileDigest }
func (r ResolvedClaudeProductionRelease) WireDigest() string     { return r.wireDigest }
func (r ResolvedClaudeProductionRelease) ReleaseDigest() string  { return r.releaseDigest }
func (r ResolvedClaudeProductionRelease) BundleDigest() string   { return r.bundleDigest }
func (r ResolvedClaudeProductionRelease) ApprovalDigest() string { return r.approvalDigest }
func (r ResolvedClaudeProductionRelease) Changeset() string      { return r.selection.Changeset() }
func (r ResolvedClaudeProductionRelease) releaseArtifact() ResolvedClaudeRelease {
	return r.selection.Release()
}

func ResolveClaudeProductionRelease() (ResolvedClaudeProductionRelease, error) {
	selection := DefaultClaudeReleaseCatalog().ProductionActive()
	release := selection.Release()
	return resolveClaudeProductionReleaseForCoordinate(ResolvedPersonaRelease{
		persona: PersonaClaudeCode, role: ProductionReleaseActive,
		version: release.Version(), releaseDigest: release.ReleaseDigest(),
		profileDigest: release.ProfileDigest(),
	})
}

func resolveClaudeProductionReleaseForCoordinate(
	coordinate ResolvedPersonaRelease,
) (ResolvedClaudeProductionRelease, error) {
	if err := coordinate.validate(); err != nil || coordinate.Persona() != PersonaClaudeCode ||
		coordinate.Role() != ProductionReleaseActive {
		return ResolvedClaudeProductionRelease{}, errors.New("共享 selector 未返回 Claude production active")
	}
	selection := DefaultClaudeReleaseCatalog().ProductionActive()
	release := selection.Release()
	if coordinate.Version() != release.Version() ||
		coordinate.ProfileDigest() != release.ProfileDigest() ||
		coordinate.ReleaseDigest() != release.ReleaseDigest() {
		return ResolvedClaudeProductionRelease{}, errors.New("共享 selector 与 Claude Release Catalog 不一致")
	}
	approval, err := loadClaudeFWHProductionApprovalForSelection(selection)
	if err != nil {
		return ResolvedClaudeProductionRelease{}, err
	}
	return ResolvedClaudeProductionRelease{
		selection: selection,
		version:   approval.Target.Version, profileDigest: approval.Target.ProfileSHA256,
		wireDigest:     approval.Target.WireSHA256,
		releaseDigest:  approval.Target.ReleaseArtifactSHA256,
		bundleDigest:   approval.Target.ReleaseBundleSHA256,
		approvalDigest: selection.ApprovalDigest(),
	}, nil
}

type claudeFWHProductionApproval struct {
	SchemaVersion   string `json:"schema_version"`
	ApprovalID      string `json:"approval_id"`
	IssuedAtUTC     string `json:"issued_at_utc"`
	ApprovalPurpose string `json:"approval_purpose"`
	Status          string `json:"status"`
	Target          struct {
		Product               string   `json:"product"`
		Version               string   `json:"version"`
		Platform              string   `json:"platform"`
		Authentication        string   `json:"authentication"`
		Privacy               string   `json:"privacy"`
		Models                []string `json:"models"`
		RequiredRules         int      `json:"required_rules"`
		ProfileSHA256         string   `json:"profile_sha256"`
		WireSHA256            string   `json:"wire_sha256"`
		ReleaseArtifactSHA256 string   `json:"release_artifact_sha256"`
		ReleaseBundleSHA256   string   `json:"release_bundle_sha256"`
	} `json:"target"`
	Predecessors []struct {
		Kind   string `json:"kind"`
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"predecessors"`
	ActiveSupportEnvelope struct {
		LogicalIngressIDs []string `json:"logical_ingress_ids"`
		StrictEgressIDs   []string `json:"strict_egress_ids"`
	} `json:"active_support_envelope"`
	RollbackOperationalEnvelope struct {
		Kind              string   `json:"kind"`
		WireEvidence      string   `json:"wire_evidence"`
		LogicalIngressIDs []string `json:"logical_ingress_ids"`
	} `json:"rollback_operational_envelope"`
	DeploymentTrafficEnvelope struct {
		LogicalIngressIDs []string `json:"logical_ingress_ids"`
	} `json:"deployment_traffic_envelope"`
	RetainedLegacy        []string `json:"retained_legacy"`
	UnknownOAuthEgress    string   `json:"unknown_oauth_egress"`
	RemovalReceiptAllowed bool     `json:"removal_receipt_allowed"`
}

func loadClaudeFWHProductionApproval() (claudeFWHProductionApproval, error) {
	return loadClaudeFWHProductionApprovalForSelection(
		DefaultClaudeReleaseCatalog().ProductionActive(),
	)
}

func loadClaudeFWHProductionApprovalForSelection(
	selection ResolvedClaudeReleaseSelection,
) (claudeFWHProductionApproval, error) {
	var approval claudeFWHProductionApproval
	if selection.role != claudeSelectorProductionActive ||
		!receiptSHA256(selection.approvalDigest) || len(selection.approvalRaw) == 0 {
		return approval, errors.New("Claude production active selector 缺少已验证 Approval")
	}
	decoder := json.NewDecoder(strings.NewReader(string(selection.approvalRaw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&approval); err != nil {
		return approval, fmt.Errorf("解析 Claude FW-H production ApprovalFact：%w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return approval, errors.New("Claude FW-H production ApprovalFact 尾部存在额外 JSON")
	}
	if err := validateClaudeFWHProductionApprovalForRelease(
		approval, selection.Release(),
	); err != nil {
		return approval, err
	}
	return approval, nil
}

func validateClaudeFWHProductionApproval(approval claudeFWHProductionApproval) error {
	return validateClaudeFWHProductionApprovalForRelease(
		approval, DefaultClaudeReleaseCatalog().ProductionActive().Release(),
	)
}

func validateClaudeFWHProductionApprovalForRelease(
	approval claudeFWHProductionApproval,
	release ResolvedClaudeRelease,
) error {
	if approval.SchemaVersion != "claude-fw-h-production-approval/v2" ||
		strings.TrimSpace(approval.ApprovalID) == "" ||
		!strings.HasPrefix(approval.ApprovalPurpose, "production_replacement") ||
		approval.Status != "approved" {
		return errors.New("Claude FW-H production ApprovalFact 顶层身份非法")
	}
	if _, err := time.Parse(time.RFC3339, approval.IssuedAtUTC); err != nil {
		return errors.New("Claude FW-H production ApprovalFact 签发时间非法")
	}
	target := approval.Target
	if target.Product != "claude-code" || target.Version != release.Version() ||
		target.Platform != release.Platform() || target.Authentication != "claude.ai-oauth" ||
		target.Privacy != "essential-traffic-no-telemetry" ||
		target.RequiredRules != release.RequiredRuleCount() ||
		!slices.Equal(target.Models, release.Models()) ||
		target.ProfileSHA256 != release.ProfileDigest() ||
		target.WireSHA256 != release.WireDigest() ||
		target.ReleaseArtifactSHA256 != release.ReleaseDigest() ||
		target.ReleaseBundleSHA256 != release.BundleDigest() {
		return errors.New("Claude FW-H production ApprovalFact Release 身份非法")
	}
	if len(approval.Predecessors) == 0 {
		return errors.New("Claude FW-H production ApprovalFact 缺少前序引用")
	}
	predecessors := make(map[string]struct{}, len(approval.Predecessors))
	for _, predecessor := range approval.Predecessors {
		key := predecessor.Kind + "\x00" + predecessor.Path
		if strings.TrimSpace(predecessor.Kind) == "" || strings.TrimSpace(predecessor.Path) == "" ||
			!receiptSHA256(predecessor.SHA256) {
			return errors.New("Claude FW-H production ApprovalFact 前序引用非法")
		}
		if _, duplicate := predecessors[key]; duplicate {
			return errors.New("Claude FW-H production ApprovalFact 前序引用重复")
		}
		predecessors[key] = struct{}{}
	}
	activeIngress := approval.ActiveSupportEnvelope.LogicalIngressIDs
	strictEgress := approval.ActiveSupportEnvelope.StrictEgressIDs
	profile, err := loadClaudeProfile(release)
	if err != nil {
		return err
	}
	wantStrictEgress := make([]string, 0, len(profile.document.StrictEndpoints))
	for _, endpoint := range profile.document.StrictEndpoints {
		wantStrictEgress = append(wantStrictEgress, endpoint.EgressID)
	}
	wantIngress := []string{
		"official-count-tokens-oauth",
		"official-messages-oauth",
	}
	if !slices.Equal(activeIngress, wantIngress) ||
		!validClaudeCatalogStringSet(strictEgress) ||
		!slices.Equal(strictEgress, wantStrictEgress) ||
		!slices.Equal(approval.RollbackOperationalEnvelope.LogicalIngressIDs, activeIngress) ||
		!slices.Equal(approval.DeploymentTrafficEnvelope.LogicalIngressIDs, activeIngress) ||
		approval.RollbackOperationalEnvelope.Kind != "frozen-legacy-deployment" ||
		approval.RollbackOperationalEnvelope.WireEvidence != "diagnostic-only" ||
		len(approval.RetainedLegacy) != 0 || approval.UnknownOAuthEgress != "denied" ||
		!approval.RemovalReceiptAllowed {
		return errors.New("Claude FW-H production ApprovalFact Envelope 或遗留处置非法")
	}
	return nil
}
