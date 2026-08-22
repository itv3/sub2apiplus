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
	"strings"
	"testing"
)

const (
	claudeOfficialClientOnlyTransitionPath = "docs/egress/maintenance/claude-official-client-only-transition.json"
	claudeOfficialClientOnlyTransitionSHA  = "8e7d6cd4750a06cb3233bf5d0c579b51b0271d5c76a9ddf45eec54b5a7e05be9"
)

type claudeOfficialClientOnlyTransitionRef struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeOfficialClientOnlyAddition struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Reason string `json:"reason"`
}

type claudeOfficialClientOnlyIngressDisposition struct {
	LogicalIngressID  string   `json:"logical_ingress_id"`
	PhysicalRoutes    []string `json:"physical_routes"`
	TargetDisposition string   `json:"target_disposition"`
	RuntimeAction     string   `json:"runtime_action"`
	RequiredRulePairs bool     `json:"required_rule_pairs"`
}

type claudeOfficialClientOnlyTransitionReceipt struct {
	SchemaVersion string                                  `json:"schema_version"`
	IssuedAtUTC   string                                  `json:"issued_at_utc"`
	BaseCommit    string                                  `json:"base_commit"`
	Scope         string                                  `json:"scope"`
	Predecessors  []claudeOfficialClientOnlyTransitionRef `json:"predecessors"`
	Target        struct {
		IngressPolicy                string   `json:"ingress_policy"`
		ReleaseSHA256                string   `json:"release_sha256"`
		OfficialIngressCatalogSHA256 string   `json:"official_ingress_catalog_sha256"`
		ApprovalSHA256               string   `json:"approval_sha256"`
		OfficialClientIDs            []string `json:"official_client_ids"`
	} `json:"target"`
	IngressInventory []claudeOfficialClientOnlyIngressDisposition `json:"ingress_inventory"`
	Transitions      []changeset4SourceTransitionEntry            `json:"transitions"`
	Additions        []claudeOfficialClientOnlyAddition           `json:"additions"`
	Verification     []string                                     `json:"verification"`
	Safety           struct {
		DMITDeployed  bool   `json:"dmit_deployed"`
		DMITState     string `json:"dmit_state"`
		VircsAccessed bool   `json:"vircs_accessed"`
		VircsChanged  bool   `json:"vircs_changed"`
		VircsState    string `json:"vircs_state"`
	} `json:"safety"`
	Result string `json:"result"`
}

func loadClaudeOfficialClientOnlyTransition() (
	claudeOfficialClientOnlyTransitionReceipt,
	error,
) {
	var receipt claudeOfficialClientOnlyTransitionReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(claudeOfficialClientOnlyTransitionPath),
	))
	if err != nil {
		return receipt, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != claudeOfficialClientOnlyTransitionSHA {
		return receipt, errors.New("Claude official-client-only transition 摘要不一致")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Claude official-client-only transition 尾部存在额外 JSON")
	}
	if err := validateClaudeOfficialClientOnlyTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateClaudeOfficialClientOnlyTransition(
	receipt claudeOfficialClientOnlyTransitionReceipt,
) error {
	if receipt.SchemaVersion != "official-egress-claude-official-client-only-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T14:00:00Z" ||
		receipt.BaseCommit != "3d5967919" ||
		receipt.Scope != "claude-official-client-only-dmit-candidate" ||
		receipt.Result != "sealed_for_dmit_validation" ||
		len(receipt.Predecessors) != 3 || len(receipt.Transitions) < 15 ||
		len(receipt.Additions) != 4 || len(receipt.Verification) == 0 {
		return errors.New("Claude official-client-only transition 顶层事实非法")
	}
	if receipt.Target.IngressPolicy != "official-client-only" ||
		receipt.Target.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		receipt.Target.OfficialIngressCatalogSHA256 != claudeOfficialIngressCatalogSHA256 ||
		receipt.Target.ApprovalSHA256 != ClaudeFWHProductionApprovalDigest ||
		!slices.Equal(receipt.Target.OfficialClientIDs, []string{
			"claude-code-2.1.226-darwin-arm64",
			"claude-code-vscode-2.1.239-darwin-arm64",
			"claude-desktop-2.1.237-darwin-arm64",
		}) {
		return errors.New("Claude official-client-only target 身份非法")
	}
	wantPredecessors := map[string]string{
		"legacy_production_approval\x00backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-legacy-retirement-approval.json": ClaudeFWHLegacyRetirementApprovalDigest,
		"persona_release_catalog_transition\x00docs/egress/maintenance/claude-persona-release-catalog-transition.json":                                         claudePersonaReleaseCatalogTransitionSHA256,
		"persona_release_catalog_acceptance\x00docs/egress/maintenance/claude-persona-release-catalog-production-acceptance.json":                              claudePersonaCatalogAcceptanceSHA,
	}
	for _, predecessor := range receipt.Predecessors {
		key := predecessor.Kind + "\x00" + predecessor.Path
		if wantPredecessors[key] != predecessor.SHA256 {
			return errors.New("Claude official-client-only predecessor 非法")
		}
		delete(wantPredecessors, key)
	}
	if len(wantPredecessors) != 0 {
		return errors.New("Claude official-client-only predecessor 不闭合")
	}
	if err := validateClaudeOfficialClientOnlyIngressInventory(receipt.IngressInventory); err != nil {
		return err
	}
	transitionPaths := make([]string, 0, len(receipt.Transitions))
	additionPaths := make([]string, 0, len(receipt.Additions))
	seen := make(map[string]struct{}, len(receipt.Transitions)+len(receipt.Additions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || !receiptSHA256(transition.FromSHA256) ||
			!receiptSHA256(transition.ToSHA256) || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			!claudeOfficialClientOnlyTargetMatches(transition.Path, transition.ToSHA256) {
			return errors.New("Claude official-client-only transition 条目非法")
		}
		if _, duplicate := seen[transition.Path]; duplicate {
			return errors.New("Claude official-client-only transition 路径重复")
		}
		seen[transition.Path] = struct{}{}
		transitionPaths = append(transitionPaths, transition.Path)
	}
	for _, addition := range receipt.Additions {
		if strings.TrimSpace(addition.Path) == "" || !receiptSHA256(addition.SHA256) ||
			strings.TrimSpace(addition.Reason) == "" ||
			!claudeOfficialClientOnlyTargetMatches(addition.Path, addition.SHA256) {
			return errors.New("Claude official-client-only addition 条目非法")
		}
		if _, duplicate := seen[addition.Path]; duplicate {
			return errors.New("Claude official-client-only addition 路径重复")
		}
		seen[addition.Path] = struct{}{}
		additionPaths = append(additionPaths, addition.Path)
	}
	if !slices.IsSorted(transitionPaths) || !slices.IsSorted(additionPaths) {
		return errors.New("Claude official-client-only 路径未排序")
	}
	if receipt.Safety.DMITDeployed || receipt.Safety.DMITState != "not_yet_validated" ||
		receipt.Safety.VircsAccessed || receipt.Safety.VircsChanged ||
		receipt.Safety.VircsState != "operator_managed/unverified/not_touched" {
		return errors.New("Claude official-client-only 安全边界非法")
	}
	return nil
}

func validateClaudeOfficialClientOnlyIngressInventory(
	inventory []claudeOfficialClientOnlyIngressDisposition,
) error {
	want := map[string]struct {
		disposition string
		action      string
		paired      bool
	}{
		"chat-completions-oauth":         {"explicitly_retired", "denied_before_oauth", false},
		"official-count-tokens-oauth":    {"migrated_strict", "catalog_required", true},
		"official-messages-oauth":        {"migrated_strict", "catalog_required", true},
		"responses-oauth":                {"explicitly_retired", "denied_before_oauth", false},
		"third-party-count-tokens-oauth": {"explicitly_retired", "denied_before_oauth", false},
		"third-party-messages-oauth":     {"explicitly_retired", "denied_before_oauth", false},
	}
	ids := make([]string, 0, len(inventory))
	for _, entry := range inventory {
		expected, ok := want[entry.LogicalIngressID]
		if !ok || len(entry.PhysicalRoutes) == 0 ||
			entry.TargetDisposition != expected.disposition ||
			entry.RuntimeAction != expected.action ||
			entry.RequiredRulePairs != expected.paired {
			return errors.New("Claude official-client-only ingress inventory 非法")
		}
		ids = append(ids, entry.LogicalIngressID)
		delete(want, entry.LogicalIngressID)
	}
	if len(want) != 0 || !slices.IsSorted(ids) {
		return errors.New("Claude official-client-only ingress inventory 不闭合")
	}
	return nil
}

func claudeOfficialClientOnlyTargetMatches(path string, want string) bool {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]) == want
}

// claudeOfficialClientOnlyTransitionSupersedes 只接受本轮固定收据，并允许
// official-client-only 之前的不可变 Claude 账本连接到本轮精确 from 摘要。
func claudeOfficialClientOnlyTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if !receiptSHA256(priorDigest) || !receiptSHA256(currentDigest) {
		return false
	}
	receipt, err := loadClaudeOfficialClientOnlyTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path || transition.ToSHA256 != currentDigest {
			continue
		}
		if transition.FromSHA256 == priorDigest ||
			claudeFWHImmutableTransitionLedgerSupersedesBeforeOfficialClientOnly(
				path, priorDigest, transition.FromSHA256,
			) || claudePersonaCatalogCloseoutTransitionSupersedes(
			path, priorDigest, transition.FromSHA256,
		) {
			return true
		}
	}
	return false
}

func TestClaudeOfficialClientOnlyTransitionIsFrozen(t *testing.T) {
	if _, err := loadClaudeOfficialClientOnlyTransition(); err != nil {
		t.Fatal(err)
	}
}
