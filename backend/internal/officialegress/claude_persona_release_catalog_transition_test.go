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
	claudePersonaReleaseCatalogTransitionPath   = "docs/egress/maintenance/claude-persona-release-catalog-transition.json"
	claudePersonaReleaseCatalogTransitionSHA256 = "16be5939a3c14dd9bc5a7e717583173939ac4d1df4b217e2d15f229d54b21288"
)

type claudePersonaReleaseCatalogTransitionRef struct {
	Kind   string `json:"kind"`
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudePersonaReleaseCatalogTransitionEntry struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

type claudePersonaReleaseCatalogAddition struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
	Reason string `json:"reason"`
}

type claudePersonaReleaseCatalogTransitionReceipt struct {
	SchemaVersion string                                     `json:"schema_version"`
	IssuedAtUTC   string                                     `json:"issued_at_utc"`
	Scope         string                                     `json:"scope"`
	BaseCommit    string                                     `json:"base_commit"`
	Predecessors  []claudePersonaReleaseCatalogTransitionRef `json:"predecessors"`
	Release       struct {
		Version       string `json:"version"`
		ProfileSHA256 string `json:"profile_sha256"`
		WireSHA256    string `json:"wire_sha256"`
		ReleaseSHA256 string `json:"release_sha256"`
		BundleSHA256  string `json:"bundle_sha256"`
	} `json:"release"`
	Invariants struct {
		FinalWireChanged              bool `json:"final_wire_changed"`
		SupportEnvelopeChanged        bool `json:"support_envelope_changed"`
		DefaultPersonaRegistryChanged bool `json:"default_persona_registry_changed"`
		RollbackReleaseFabricated     bool `json:"rollback_release_fabricated"`
	} `json:"invariants"`
	Transitions  []claudePersonaReleaseCatalogTransitionEntry `json:"transitions"`
	Additions    []claudePersonaReleaseCatalogAddition        `json:"additions"`
	Verification []string                                     `json:"verification"`
	Safety       struct {
		DeploymentPerformed bool `json:"deployment_performed"`
		DMITChanged         bool `json:"dmit_changed"`
		VircsChanged        bool `json:"vircs_changed"`
	} `json:"safety"`
	Result string `json:"result"`
}

func loadClaudePersonaReleaseCatalogTransition() (
	claudePersonaReleaseCatalogTransitionReceipt,
	error,
) {
	var receipt claudePersonaReleaseCatalogTransitionReceipt
	raw, err := os.ReadFile(filepath.Join(
		"../../..", filepath.FromSlash(claudePersonaReleaseCatalogTransitionPath),
	))
	if err != nil {
		return receipt, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != claudePersonaReleaseCatalogTransitionSHA256 {
		return receipt, errors.New("Claude Persona Release Catalog transition 摘要不一致")
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, errors.New("Claude Persona Release Catalog transition 尾部存在额外 JSON")
	}
	if err := validateClaudePersonaReleaseCatalogTransition(receipt); err != nil {
		return receipt, err
	}
	return receipt, nil
}

func validateClaudePersonaReleaseCatalogTransition(
	receipt claudePersonaReleaseCatalogTransitionReceipt,
) error {
	if receipt.SchemaVersion != "official-egress-claude-persona-release-catalog-transition/v1" ||
		receipt.IssuedAtUTC != "2026-08-22T09:00:00Z" ||
		receipt.Scope != "post-fw-h-release-catalog-runtime-refactor" ||
		receipt.BaseCommit != "f7fd07c2a19fded0d33a807167fca11fca32ff6e" ||
		receipt.Result != "passed" || len(receipt.Predecessors) == 0 ||
		len(receipt.Transitions) == 0 || len(receipt.Additions) == 0 ||
		len(receipt.Verification) == 0 {
		return errors.New("Claude Persona Release Catalog transition 顶层事实非法")
	}
	if receipt.Release.Version != ClaudeFWGVersion ||
		receipt.Release.ProfileSHA256 != ClaudeFWGProfileDigest ||
		receipt.Release.WireSHA256 != claudeFWGWireDigest ||
		receipt.Release.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		receipt.Release.BundleSHA256 != ClaudeFWGBundleDigest ||
		receipt.Invariants.FinalWireChanged || receipt.Invariants.SupportEnvelopeChanged ||
		receipt.Invariants.DefaultPersonaRegistryChanged ||
		receipt.Invariants.RollbackReleaseFabricated || receipt.Safety.DeploymentPerformed ||
		receipt.Safety.DMITChanged || receipt.Safety.VircsChanged {
		return errors.New("Claude Persona Release Catalog transition 不变量非法")
	}
	wantPredecessors := map[string]string{
		"production_acceptance\x00docs/egress/maintenance/claude-fw-h-response-request-id-acceptance.json":                                              "722df263723bbf64ebc74f9470d4c9725b9e92710feda4a3b55bfa8eb4ad8668",
		"production_approval\x00backend/internal/officialegress/catalogdata/claude/production/claude-code-2.1.226-fw-h-legacy-retirement-approval.json": ClaudeFWHLegacyRetirementApprovalDigest,
	}
	for _, predecessor := range receipt.Predecessors {
		key := predecessor.Kind + "\x00" + predecessor.Path
		if wantPredecessors[key] != predecessor.SHA256 {
			return errors.New("Claude Persona Release Catalog transition 前序非法")
		}
		delete(wantPredecessors, key)
	}
	if len(wantPredecessors) != 0 {
		return errors.New("Claude Persona Release Catalog transition 前序不闭合")
	}
	transitionPaths := make([]string, 0, len(receipt.Transitions))
	additionPaths := make([]string, 0, len(receipt.Additions))
	seenPaths := make(map[string]struct{}, len(receipt.Transitions)+len(receipt.Additions))
	for _, transition := range receipt.Transitions {
		if strings.TrimSpace(transition.Path) == "" || !receiptSHA256(transition.FromSHA256) ||
			!receiptSHA256(transition.ToSHA256) || transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" ||
			!claudePersonaTransitionTargetMatches(transition.Path, transition.ToSHA256) {
			return errors.New("Claude Persona Release Catalog transition 条目非法")
		}
		if _, duplicate := seenPaths[transition.Path]; duplicate {
			return errors.New("Claude Persona Release Catalog transition 路径重复")
		}
		seenPaths[transition.Path] = struct{}{}
		transitionPaths = append(transitionPaths, transition.Path)
	}
	for _, addition := range receipt.Additions {
		if strings.TrimSpace(addition.Path) == "" || !receiptSHA256(addition.SHA256) ||
			strings.TrimSpace(addition.Reason) == "" ||
			!claudePersonaTransitionTargetMatches(addition.Path, addition.SHA256) {
			return errors.New("Claude Persona Release Catalog addition 条目非法")
		}
		if _, duplicate := seenPaths[addition.Path]; duplicate {
			return errors.New("Claude Persona Release Catalog addition 路径重复")
		}
		seenPaths[addition.Path] = struct{}{}
		additionPaths = append(additionPaths, addition.Path)
	}
	if !slices.IsSorted(transitionPaths) || !slices.IsSorted(additionPaths) {
		return errors.New("Claude Persona Release Catalog transition 路径未排序")
	}
	return nil
}

func claudePersonaTransitionTargetMatches(path string, want string) bool {
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return false
	}
	sum := sha256.Sum256(raw)
	current := hex.EncodeToString(sum[:])
	return current == want ||
		claudeOfflineCIStabilizationTransitionSupersedes(path, want, current) ||
		claudeOfficialClientOnlyTransitionSupersedes(path, want, current) ||
		claudePersonaReleaseCatalogProductionAcceptanceSupersedes(path, want, current)
}

// 历史门禁已经独立验证各自 receipt；本函数只证明该路径从基线提交继续演进到
// 本轮明确 transition 的最终摘要，避免原位改写旧收据。
func claudePersonaReleaseCatalogTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if !receiptSHA256(priorDigest) || !receiptSHA256(currentDigest) {
		return false
	}
	if claudeOfflineCIStabilizationTransitionSupersedes(
		path, priorDigest, currentDigest,
	) {
		return true
	}
	if claudeOfficialClientOnlyTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudePersonaCatalogCloseoutTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, err := loadClaudePersonaReleaseCatalogTransition()
	if err != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path == path && transition.ToSHA256 == currentDigest &&
			(transition.FromSHA256 == priorDigest ||
				claudeFWHImmutableTransitionLedgerSupersedes(
					path, priorDigest, transition.FromSHA256,
				)) {
			return true
		}
	}
	return false
}

func TestClaudePersonaReleaseCatalogTransitionIsFrozen(t *testing.T) {
	if _, err := loadClaudePersonaReleaseCatalogTransition(); err != nil {
		t.Fatal(err)
	}
}
