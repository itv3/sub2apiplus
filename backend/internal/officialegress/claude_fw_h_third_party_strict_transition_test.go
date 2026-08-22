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

const (
	claudeFWHThirdPartyStrictSourceSHA256   = "92c7a0f1d1150cb12004705347c18cc9d0b411439b8fca865cb8a395569b1f6d"
	claudeFWHThirdPartyStrictTestSHA256     = "646d66a654a2fece0081e08710a687563e6d67750351c48dbdb114c0b3dd234c"
	claudeFWHThirdPartyStrictApprovalSHA256 = "7763d9337f1529dba2bdb8bdf07a98d78cb57749f75ba38a7f80fa82bd80e297"
)

type claudeFWHThirdPartyStrictTransitionReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Phase         string `json:"phase"`
	BaseCommit    string `json:"base_commit"`
	Scope         string `json:"scope"`
	Predecessor   struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"predecessor"`
	SourceTransition *struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"source_transition,omitempty"`
	LogicalIngressIDs []string                          `json:"logical_ingress_ids"`
	Transitions       []changeset4SourceTransitionEntry `json:"transitions"`
	Gates             []string                          `json:"gates,omitempty"`
	Safety            struct {
		CurrentDisposition     string   `json:"current_disposition"`
		RetainedLegacy         []string `json:"retained_legacy"`
		ProductionSelector     string   `json:"production_selector"`
		DeploymentPerformed    bool     `json:"deployment_performed"`
		DMITValidated          bool     `json:"dmit_validated"`
		VircsProductionChanged bool     `json:"vircs_production_changed"`
		RemovalReceiptAllowed  bool     `json:"removal_receipt_allowed"`
	} `json:"safety"`
	Result string `json:"result"`
}

type claudeFWHThirdPartyStrictCandidateApproval struct {
	SchemaVersion string `json:"schema_version"`
	ApprovalID    string `json:"approval_id"`
	IssuedAtUTC   string `json:"issued_at_utc"`
	Phase         string `json:"phase"`
	Status        string `json:"status"`
	Target        struct {
		Product       string   `json:"product"`
		Version       string   `json:"version"`
		Models        []string `json:"models"`
		ProfileSHA256 string   `json:"profile_sha256"`
		WireSHA256    string   `json:"wire_sha256"`
		ReleaseSHA256 string   `json:"release_sha256"`
		BundleSHA256  string   `json:"bundle_sha256"`
	} `json:"target"`
	Predecessors []struct {
		Kind   string `json:"kind"`
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"predecessors"`
	IngressTargets []struct {
		LogicalIngressID   string   `json:"logical_ingress_id"`
		ProtocolClass      string   `json:"protocol_class"`
		PhysicalAliasIDs   []string `json:"physical_alias_ids"`
		CurrentDisposition string   `json:"current_disposition"`
		TargetDisposition  string   `json:"target_disposition"`
	} `json:"ingress_targets"`
	Compatibility struct {
		Lossless []string `json:"lossless"`
		Denied   []string `json:"denied"`
	} `json:"compatibility"`
	Safety struct {
		ProductionSelectorChanged bool     `json:"production_selector_changed"`
		DeploymentPerformed       bool     `json:"deployment_performed"`
		DMITValidated             bool     `json:"dmit_validated"`
		VircsProductionChanged    bool     `json:"vircs_production_changed"`
		RetainedLegacy            []string `json:"retained_legacy"`
		RemovalReceiptAllowed     bool     `json:"removal_receipt_allowed"`
	} `json:"safety"`
	Result string `json:"result"`
}

func readClaudeFWHThirdPartyStrictTransition(
	path string,
) (claudeFWHThirdPartyStrictTransitionReceipt, []byte, error) {
	var receipt claudeFWHThirdPartyStrictTransitionReceipt
	raw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(path)))
	if err != nil {
		return receipt, nil, err
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return receipt, nil, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return receipt, nil, errors.New("Claude FW-H 第三方 strict transition 尾部存在额外 JSON")
	}
	return receipt, raw, nil
}

func validateClaudeFWHThirdPartyStrictSource(
	receipt claudeFWHThirdPartyStrictTransitionReceipt,
	raw []byte,
) error {
	if claudeFWHSourceDigest(raw) != claudeFWHThirdPartyStrictSourceSHA256 ||
		receipt.SchemaVersion != "claude-fw-h-third-party-strict-source-transition/v1" ||
		receipt.Date != "2026-08-22" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "a181859d374821d0c514e00a684ba15a6b8f59c3" ||
		receipt.Scope != "candidate_only" || receipt.SourceTransition != nil ||
		receipt.Predecessor.Path != "docs/egress/maintenance/claude-fw-h-production-acceptance-package.json" ||
		receipt.Predecessor.SHA256 != claudeFWHProductionAcceptanceSHA256 ||
		!slices.Equal(receipt.LogicalIngressIDs, []string{
			"chat-completions-oauth", "responses-oauth",
		}) || len(receipt.Transitions) != 9 || receipt.Result != "passed" {
		return errors.New("Claude FW-H 第三方 strict source transition 顶层事实非法")
	}
	return validateClaudeFWHThirdPartyStrictTransitionEntries(receipt.Transitions)
}

func validateClaudeFWHThirdPartyStrictTest(
	receipt claudeFWHThirdPartyStrictTransitionReceipt,
	raw []byte,
) error {
	if claudeFWHSourceDigest(raw) != claudeFWHThirdPartyStrictTestSHA256 ||
		receipt.SchemaVersion != "claude-fw-h-third-party-strict-test-transition/v1" ||
		receipt.Date != "2026-08-22" || receipt.Phase != "FW-H" ||
		receipt.BaseCommit != "a181859d374821d0c514e00a684ba15a6b8f59c3" ||
		receipt.Scope != "candidate_only" || receipt.SourceTransition == nil ||
		receipt.SourceTransition.Path != "docs/egress/maintenance/claude-fw-h-third-party-strict-source-transition.json" ||
		receipt.SourceTransition.SHA256 != claudeFWHThirdPartyStrictSourceSHA256 ||
		!slices.Equal(receipt.LogicalIngressIDs, []string{
			"chat-completions-oauth", "responses-oauth",
		}) || len(receipt.Transitions) != 4 || len(receipt.Gates) != 5 ||
		receipt.Result != "passed" {
		return errors.New("Claude FW-H 第三方 strict test transition 顶层事实非法")
	}
	return validateClaudeFWHThirdPartyStrictTransitionEntries(receipt.Transitions)
}

func validateClaudeFWHThirdPartyStrictTransitionEntries(
	transitions []changeset4SourceTransitionEntry,
) error {
	paths := make([]string, 0, len(transitions))
	for _, transition := range transitions {
		if strings.TrimSpace(transition.Path) == "" ||
			strings.TrimSpace(transition.FromSHA256) == "" ||
			strings.TrimSpace(transition.ToSHA256) == "" ||
			transition.FromSHA256 == transition.ToSHA256 ||
			strings.TrimSpace(transition.Reason) == "" {
			return errors.New("Claude FW-H 第三方 strict transition 条目不完整")
		}
		source, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(transition.Path)))
		currentDigest := claudeFWHSourceDigest(source)
		if err != nil || currentDigest != transition.ToSHA256 &&
			!claudeFWHBareChatRouteTransitionSupersedes(
				transition.Path, transition.ToSHA256, currentDigest,
			) {
			return errors.New("Claude FW-H 第三方 strict transition 当前摘要不一致")
		}
		paths = append(paths, transition.Path)
	}
	if !slices.IsSorted(paths) || len(paths) != len(slices.Compact(paths)) {
		return errors.New("Claude FW-H 第三方 strict transition 路径未排序或重复")
	}
	return nil
}

func validateClaudeFWHThirdPartyStrictSafety(
	receipt claudeFWHThirdPartyStrictTransitionReceipt,
) error {
	safety := receipt.Safety
	if safety.CurrentDisposition != "retained_legacy_until_dmit_acceptance" ||
		!slices.Equal(safety.RetainedLegacy, []string{
			"chat-completions-oauth", "responses-oauth",
		}) || safety.ProductionSelector != "unchanged" || safety.DeploymentPerformed ||
		safety.DMITValidated || safety.VircsProductionChanged || safety.RemovalReceiptAllowed {
		return errors.New("Claude FW-H 第三方 strict transition 安全边界非法")
	}
	return nil
}

func claudeFWHThirdPartyStrictSourceTransitionSupersedes(
	path string,
	priorDigest string,
	currentDigest string,
) bool {
	if claudeFWHLegacyRetirementTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	receipt, raw, err := readClaudeFWHThirdPartyStrictTransition(
		"docs/egress/maintenance/claude-fw-h-third-party-strict-source-transition.json",
	)
	if err != nil || validateClaudeFWHThirdPartyStrictSource(receipt, raw) != nil ||
		validateClaudeFWHThirdPartyStrictSafety(receipt) != nil {
		return false
	}
	for _, transition := range receipt.Transitions {
		if transition.Path != path ||
			(transition.ToSHA256 != currentDigest &&
				!claudeFWHLegacyRetirementTransitionSupersedes(
					path, transition.ToSHA256, currentDigest,
				)) {
			continue
		}
		if transition.FromSHA256 == priorDigest {
			return true
		}
		// 追加收据的 from 是候选开始时的真实摘要。历史冻结测试可能从更早的
		// 摘要询问可达性，因此只通过既有已冻结迁移链连接到这个精确 from。
		// 递归调用不会重新命中本分支，因为目标已变为 transition.FromSHA256。
		if claudeFWHSourceTransitionSupersedes(
			path, priorDigest, transition.FromSHA256,
		) || claudeFWGModelCapabilityTransitionSupersedes(
			path, priorDigest, transition.FromSHA256,
		) {
			return true
		}
	}
	return false
}

func TestClaudeFWHThirdPartyStrictTransitionsAreFrozen(t *testing.T) {
	source, sourceRaw, err := readClaudeFWHThirdPartyStrictTransition(
		"docs/egress/maintenance/claude-fw-h-third-party-strict-source-transition.json",
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWHThirdPartyStrictSource(source, sourceRaw); err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWHThirdPartyStrictSafety(source); err != nil {
		t.Fatal(err)
	}
	testReceipt, testRaw, err := readClaudeFWHThirdPartyStrictTransition(
		"docs/egress/maintenance/claude-fw-h-third-party-strict-test-transition.json",
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWHThirdPartyStrictTest(testReceipt, testRaw); err != nil {
		t.Fatal(err)
	}
	if err := validateClaudeFWHThirdPartyStrictSafety(testReceipt); err != nil {
		t.Fatal(err)
	}
	validateClaudeFWHThirdPartyStrictCandidateApproval(t)
}

func validateClaudeFWHThirdPartyStrictCandidateApproval(t *testing.T) {
	t.Helper()
	raw, err := os.ReadFile("../../../docs/egress/maintenance/claude-fw-h-third-party-strict-candidate-approval.json")
	if err != nil {
		t.Fatal(err)
	}
	if claudeFWHSourceDigest(raw) != claudeFWHThirdPartyStrictApprovalSHA256 {
		t.Fatal("Claude FW-H 第三方 strict candidate approval 摘要漂移")
	}
	var approval claudeFWHThirdPartyStrictCandidateApproval
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&approval); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("Claude FW-H 第三方 strict candidate approval 尾部存在额外 JSON")
	}
	if approval.SchemaVersion != "claude-fw-h-third-party-strict-candidate-approval/v1" ||
		approval.ApprovalID != "claude-code-2.1.226-fw-h-third-party-strict-v1" ||
		approval.Phase != "FW-H" || approval.Status != "accepted_not_activated" ||
		approval.Result != "approved_for_dmit_validation" {
		t.Fatal("Claude FW-H 第三方 strict candidate approval 顶层事实非法")
	}
	target := approval.Target
	if target.Product != "claude-code" || target.Version != ClaudeFWGVersion ||
		!slices.Equal(target.Models, []string{
			"claude-sonnet-5", "claude-opus-5", "claude-fable-5",
		}) || target.ProfileSHA256 != ClaudeFWGProfileDigest ||
		target.WireSHA256 != claudeFWGWireDigest ||
		target.ReleaseSHA256 != ClaudeFWGReleaseDigest ||
		target.BundleSHA256 != ClaudeFWGBundleDigest {
		t.Fatal("Claude FW-H 第三方 strict candidate Release 身份非法")
	}
	wantPredecessors := map[string]string{
		"production_acceptance\x00docs/egress/maintenance/claude-fw-h-production-acceptance-package.json":    claudeFWHProductionAcceptanceSHA256,
		"source_transition\x00docs/egress/maintenance/claude-fw-h-third-party-strict-source-transition.json": claudeFWHThirdPartyStrictSourceSHA256,
		"test_transition\x00docs/egress/maintenance/claude-fw-h-third-party-strict-test-transition.json":     claudeFWHThirdPartyStrictTestSHA256,
	}
	for _, predecessor := range approval.Predecessors {
		key := predecessor.Kind + "\x00" + predecessor.Path
		if wantPredecessors[key] != predecessor.SHA256 {
			t.Fatal("Claude FW-H 第三方 strict candidate 前序引用非法")
		}
		delete(wantPredecessors, key)
	}
	if len(wantPredecessors) != 0 || len(approval.IngressTargets) != 3 ||
		len(approval.Compatibility.Lossless) != 7 || len(approval.Compatibility.Denied) != 10 {
		t.Fatal("Claude FW-H 第三方 strict candidate 范围不闭合")
	}
	wantIngress := []string{"chat-completions-oauth", "responses-oauth", "codex-direct-rerouted"}
	for index, ingress := range approval.IngressTargets {
		if ingress.LogicalIngressID != wantIngress[index] {
			t.Fatal("Claude FW-H 第三方 strict candidate 入口顺序非法")
		}
		if index < 2 && (ingress.CurrentDisposition != "retained_legacy" ||
			ingress.TargetDisposition != "migrated_strict") {
			t.Fatal("Claude FW-H 第三方 strict candidate 目标处置非法")
		}
		if index == 2 && (ingress.CurrentDisposition != "rerouted" ||
			ingress.TargetDisposition != "rerouted") {
			t.Fatal("Claude FW-H Codex direct 处置被误改")
		}
	}
	safety := approval.Safety
	if safety.ProductionSelectorChanged || safety.DeploymentPerformed || safety.DMITValidated ||
		safety.VircsProductionChanged || safety.RemovalReceiptAllowed ||
		!slices.Equal(safety.RetainedLegacy, []string{
			"chat-completions-oauth", "responses-oauth",
		}) {
		t.Fatal("Claude FW-H 第三方 strict candidate 安全边界非法")
	}
}
