package officialegress

import (
	"crypto/sha256"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"
)

const (
	// ClaudeFWHInitialProductionApprovalDigest 固定首次生产迁移批准，历史验收与
	// 收据只能继续引用它，后继退休批准不得覆盖该事实。
	ClaudeFWHInitialProductionApprovalDigest = "73e690d20fdb36688d70247d4c857f6aa31ebfc6dcd755fbbcc8276e37d74f1a"
	// ClaudeFWHProductionApprovalDigest 是当前正式运行时唯一接受的后继批准。
	ClaudeFWHProductionApprovalDigest = "1d002a2708b198256098999932908d47f0eb90a2c819f11d8393570f33996c2c"
)

func ClaudeFWGWireDigest() string { return claudeFWGWireDigest }

//go:embed catalogdata/claude/production/claude-code-2.1.226-fw-h-legacy-retirement-approval.json
var claudeFWHProductionApprovalRaw []byte

// ResolvedClaudeProductionRelease 是 Claude 自有发布源向共享运行时暴露的只读坐标。
// 首次 FW-H rollback 是遗留部署，因此这里刻意不伪造 rollback Release。
type ResolvedClaudeProductionRelease struct {
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

func ResolveClaudeProductionRelease() (ResolvedClaudeProductionRelease, error) {
	approval, err := loadClaudeFWHProductionApproval()
	if err != nil {
		return ResolvedClaudeProductionRelease{}, err
	}
	return ResolvedClaudeProductionRelease{
		version: approval.Target.Version, profileDigest: approval.Target.ProfileSHA256,
		wireDigest:     approval.Target.WireSHA256,
		releaseDigest:  approval.Target.ReleaseArtifactSHA256,
		bundleDigest:   approval.Target.ReleaseBundleSHA256,
		approvalDigest: ClaudeFWHProductionApprovalDigest,
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
	var approval claudeFWHProductionApproval
	sum := sha256.Sum256(claudeFWHProductionApprovalRaw)
	if hex.EncodeToString(sum[:]) != ClaudeFWHProductionApprovalDigest {
		return approval, errors.New("Claude FW-H production ApprovalFact 原文字节摘要不一致")
	}
	decoder := json.NewDecoder(strings.NewReader(string(claudeFWHProductionApprovalRaw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&approval); err != nil {
		return approval, fmt.Errorf("解析 Claude FW-H production ApprovalFact：%w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return approval, errors.New("Claude FW-H production ApprovalFact 尾部存在额外 JSON")
	}
	if err := validateClaudeFWHProductionApproval(approval); err != nil {
		return approval, err
	}
	return approval, nil
}

func validateClaudeFWHProductionApproval(approval claudeFWHProductionApproval) error {
	if approval.SchemaVersion != "claude-fw-h-production-approval/v2" ||
		approval.ApprovalID != "claude-code-2.1.226-fw-h-legacy-retirement-v2" ||
		approval.IssuedAtUTC != "2026-08-22T02:30:04Z" ||
		approval.ApprovalPurpose != "production_replacement_legacy_retirement" ||
		approval.Status != "approved" {
		return errors.New("Claude FW-H production ApprovalFact 顶层身份非法")
	}
	target := approval.Target
	if target.Product != "claude-code" || target.Version != ClaudeFWGVersion ||
		target.Platform != "linux/amd64" || target.Authentication != "claude.ai-oauth" ||
		target.Privacy != "essential-traffic-no-telemetry" || target.RequiredRules != 40 ||
		!slices.Equal(target.Models, []string{
			"claude-sonnet-5", "claude-opus-5", "claude-fable-5",
		}) || target.ProfileSHA256 != ClaudeFWGProfileDigest ||
		target.WireSHA256 != claudeFWGWireDigest ||
		target.ReleaseArtifactSHA256 != ClaudeFWGReleaseDigest ||
		target.ReleaseBundleSHA256 != ClaudeFWGBundleDigest {
		return errors.New("Claude FW-H production ApprovalFact Release 身份非法")
	}
	wantPredecessors := map[string]string{
		"prior_production_approval\x00backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-approval.json": ClaudeFWHInitialProductionApprovalDigest,
		"production_acceptance\x00docs/egress/maintenance/claude-fw-h-production-acceptance-package.json":                                   "45b21fd3e74e6b2a3968c04bf18b416a0a43a157ba7cb7451f32a5d3c711cabd",
		"candidate_approval\x00docs/egress/maintenance/claude-fw-h-third-party-strict-candidate-approval.json":                              "7763d9337f1529dba2bdb8bdf07a98d78cb57749f75ba38a7f80fa82bd80e297",
		"dmit_acceptance\x00docs/egress/maintenance/claude-fw-h-third-party-strict-dmit-acceptance.json":                                    "1a77cc26952355e13d486cdf7979f1bf9ed8a40d413cf55999cb5e8093c8fcfc",
	}
	for _, predecessor := range approval.Predecessors {
		key := predecessor.Kind + "\x00" + predecessor.Path
		if wantPredecessors[key] != predecessor.SHA256 {
			return errors.New("Claude FW-H production ApprovalFact 前序引用非法")
		}
		delete(wantPredecessors, key)
	}
	if len(wantPredecessors) != 0 {
		return errors.New("Claude FW-H production ApprovalFact 前序引用不闭合")
	}
	wantIngress := []string{
		"official-count-tokens-oauth",
		"official-messages-oauth",
		"third-party-count-tokens-oauth",
		"third-party-messages-oauth",
		"chat-completions-oauth",
		"responses-oauth",
	}
	wantEgress := []string{
		"egress-claude-count-tokens",
		"egress-claude-lifecycle-hello",
		"egress-claude-mcp-servers",
		"egress-claude-messages-inference",
		"egress-claude-oauth-profile",
		"egress-claude-oauth-token-refresh",
		"egress-claude-policy-limits",
		"egress-claude-settings",
	}
	if !slices.Equal(approval.ActiveSupportEnvelope.LogicalIngressIDs, wantIngress) ||
		!slices.Equal(approval.ActiveSupportEnvelope.StrictEgressIDs, wantEgress) ||
		approval.RollbackOperationalEnvelope.Kind != "frozen-legacy-deployment" ||
		approval.RollbackOperationalEnvelope.WireEvidence != "diagnostic-only" ||
		!slices.Equal(approval.RollbackOperationalEnvelope.LogicalIngressIDs, wantIngress) ||
		!slices.Equal(approval.DeploymentTrafficEnvelope.LogicalIngressIDs, wantIngress) ||
		len(approval.RetainedLegacy) != 0 || approval.UnknownOAuthEgress != "denied" ||
		!approval.RemovalReceiptAllowed {
		return errors.New("Claude FW-H production ApprovalFact Envelope 或遗留处置非法")
	}
	return nil
}
