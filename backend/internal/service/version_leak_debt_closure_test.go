package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"testing"
)

const versionLeakDebtClosureSHA256 = "deaaf580764c1cab5313b377b82f74ad48b820b113da7b565bb6c6a2c5eb0205"

type versionLeakDebtReference struct {
	Path        string `json:"path"`
	Fingerprint string `json:"fingerprint"`
	Count       int    `json:"count"`
	Reason      string `json:"reason"`
}

type versionLeakRetainedHistoricalEvidence struct {
	Path    string `json:"path"`
	Pattern string `json:"pattern"`
	Count   int    `json:"count"`
	Reason  string `json:"reason"`
}

type versionLeakDebtSourceTransition struct {
	Path       string `json:"path"`
	FromSHA256 string `json:"from_sha256"`
	ToSHA256   string `json:"to_sha256"`
	Reason     string `json:"reason"`
}

type versionLeakDebtClosure struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	BaseCommit    string `json:"base_commit"`
	Scope         string `json:"scope"`
	TextDebt      struct {
		PriorFiles           int `json:"prior_files"`
		PriorHits            int `json:"prior_hits"`
		CurrentFiles         int `json:"current_files"`
		CurrentHits          int `json:"current_hits"`
		ApprovedNonLeakFiles int `json:"approved_non_leak_files"`
		ApprovedNonLeakHits  int `json:"approved_non_leak_hits"`
	} `json:"text_debt"`
	ApprovedNonLeakReferences  []versionLeakDebtReference              `json:"approved_non_leak_references"`
	RetainedHistoricalEvidence []versionLeakRetainedHistoricalEvidence `json:"retained_historical_evidence"`
	ASTScan                    struct {
		PriorFiles               int `json:"prior_files"`
		PriorHits                int `json:"prior_hits"`
		CurrentFiles             int `json:"current_files"`
		CurrentHits              int `json:"current_hits"`
		RemainingClassifications []struct {
			Path   string `json:"path"`
			Hits   int    `json:"hits"`
			Reason string `json:"reason"`
		} `json:"remaining_classifications"`
	} `json:"ast_scan"`
	SourceTransitions      []versionLeakDebtSourceTransition `json:"source_transitions"`
	RuntimeBehaviorChanged bool                              `json:"runtime_behavior_changed"`
	AllowedWireDeltas      []string                          `json:"allowed_wire_deltas"`
	ClosureResult          string                            `json:"closure_result"`
	Verification           []string                          `json:"verification"`
}

func TestVersionLeakDebtClosureIsComplete(t *testing.T) {
	repoRoot := filepath.Clean("../../..")
	receipt := readVersionLeakDebtClosure(t, repoRoot)
	if receipt.SchemaVersion != "official-egress.version-leak-debt-closure/v1" ||
		receipt.Date != "2026-08-16" || receipt.BaseCommit == "" || receipt.Scope == "" ||
		receipt.TextDebt.PriorFiles != 19 || receipt.TextDebt.PriorHits != 58 ||
		receipt.TextDebt.CurrentFiles != 0 || receipt.TextDebt.CurrentHits != 0 ||
		receipt.TextDebt.ApprovedNonLeakFiles != 2 || receipt.TextDebt.ApprovedNonLeakHits != 2 ||
		len(receipt.ApprovedNonLeakReferences) != 2 || len(receipt.RetainedHistoricalEvidence) != 1 ||
		receipt.ASTScan.PriorFiles != 26 || receipt.ASTScan.PriorHits != 71 ||
		receipt.ASTScan.CurrentFiles != 8 || receipt.ASTScan.CurrentHits != 10 ||
		len(receipt.ASTScan.RemainingClassifications) != 8 ||
		len(receipt.SourceTransitions) != 8 || receipt.RuntimeBehaviorChanged ||
		len(receipt.AllowedWireDeltas) != 0 || receipt.ClosureResult != "complete" ||
		len(receipt.Verification) != 6 {
		t.Fatalf("版本泄漏债务闭集事实不完整：%+v", receipt)
	}

	for _, transition := range receipt.SourceTransitions {
		if transition.Path == "" || transition.FromSHA256 == "" ||
			transition.ToSHA256 == "" || transition.FromSHA256 == transition.ToSHA256 ||
			transition.Reason == "" {
			t.Fatalf("版本泄漏源码 transition 不完整：%+v", transition)
		}
		raw, err := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(transition.Path)))
		if err != nil {
			t.Fatal(err)
		}
		if got := versionLeakDebtDigest(raw); got != transition.ToSHA256 &&
			!upstreamV0177SourceTransitionSupersedes(transition.Path, transition.ToSHA256, got) &&
			!upstreamMergeFrameworkTransitionSupersedesService(
				transition.Path, transition.ToSHA256, got,
			) {
			t.Fatalf("版本泄漏源码摘要漂移：path=%s got=%s want=%s", transition.Path, got, transition.ToSHA256)
		}
	}
	for _, evidence := range receipt.RetainedHistoricalEvidence {
		if evidence.Path == "" || evidence.Pattern == "" || evidence.Count <= 0 || evidence.Reason == "" {
			t.Fatalf("保留历史证据分类不完整：%+v", evidence)
		}
		raw, err := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(evidence.Path)))
		if err != nil {
			t.Fatal(err)
		}
		if got := bytes.Count(raw, []byte(evidence.Pattern)); got != evidence.Count {
			t.Fatalf("保留历史证据分类漂移：path=%s got=%d want=%d", evidence.Path, got, evidence.Count)
		}
	}

	assertVersionLeakTextPolicyMatchesClosure(t, repoRoot, receipt)
	assertVersionLeakASTPolicyMatchesClosure(t, receipt)
}

func assertVersionLeakTextPolicyMatchesClosure(
	t *testing.T,
	repoRoot string,
	receipt versionLeakDebtClosure,
) {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(repoRoot, "tools/version_leak_baseline.json"))
	if err != nil {
		t.Fatal(err)
	}
	var policy struct {
		SchemaVersion string                    `json:"schema_version"`
		Files         map[string]map[string]int `json:"files"`
		Approved      map[string]map[string]struct {
			Count  int    `json:"count"`
			Reason string `json:"reason"`
		} `json:"approved_non_leak_references"`
	}
	if err := json.Unmarshal(raw, &policy); err != nil {
		t.Fatal(err)
	}
	if policy.SchemaVersion != "codex-oauth-version-leak-policy/v2" || len(policy.Files) != 0 ||
		len(policy.Approved) != receipt.TextDebt.ApprovedNonLeakFiles {
		t.Fatalf("文本版本泄漏策略未保持零债务：%+v", policy)
	}
	for _, reference := range receipt.ApprovedNonLeakReferences {
		entry, ok := policy.Approved[reference.Path][reference.Fingerprint]
		if !ok || entry.Count != reference.Count || entry.Reason != reference.Reason {
			t.Fatalf("受审非泄漏引用与闭集收据不一致：%+v", reference)
		}
	}
}

func assertVersionLeakASTPolicyMatchesClosure(t *testing.T, receipt versionLeakDebtClosure) {
	t.Helper()
	hits, err := scanOfficialEgressVersionLiterals("../..")
	if err != nil {
		t.Fatal(err)
	}
	baseline := readVersionLeakASTBaseline(t)
	if diff := diffVersionLeakBaseline(hits, baseline); diff != "" {
		t.Fatalf("AST 版本命中与收紧基线不一致：%s", diff)
	}
	if len(hits) != receipt.ASTScan.CurrentFiles || totalVersionLeakHits(hits) != receipt.ASTScan.CurrentHits {
		t.Fatalf("AST 版本命中数量漂移：files=%d hits=%d", len(hits), totalVersionLeakHits(hits))
	}
	for _, classification := range receipt.ASTScan.RemainingClassifications {
		if classification.Path == "" || classification.Hits <= 0 || classification.Reason == "" {
			t.Fatalf("AST 剩余命中缺少分类理由：%+v", classification)
		}
		count := 0
		for _, entry := range hits[classification.Path] {
			count += entry.Count
		}
		if count != classification.Hits {
			t.Fatalf("AST 剩余命中分类漂移：path=%s got=%d want=%d", classification.Path, count, classification.Hits)
		}
	}
}

func readVersionLeakDebtClosure(t *testing.T, repoRoot string) versionLeakDebtClosure {
	t.Helper()
	path := filepath.Join(repoRoot, "docs/egress/maintenance/version-leak-debt-closure.json")
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := versionLeakDebtDigest(raw); got != versionLeakDebtClosureSHA256 {
		t.Fatalf("版本泄漏债务闭集收据漂移：got=%s want=%s", got, versionLeakDebtClosureSHA256)
	}
	var receipt versionLeakDebtClosure
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		t.Fatal("版本泄漏债务闭集收据尾部存在额外 JSON")
	}
	return receipt
}

func versionLeakDebtTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	raw, err := os.ReadFile("../../../docs/egress/maintenance/version-leak-debt-closure.json")
	if err != nil || versionLeakDebtDigest(raw) != versionLeakDebtClosureSHA256 {
		return false
	}
	var receipt struct {
		SchemaVersion     string                            `json:"schema_version"`
		SourceTransitions []versionLeakDebtSourceTransition `json:"source_transitions"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil ||
		receipt.SchemaVersion != "official-egress.version-leak-debt-closure/v1" {
		return false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			upstreamV0177SourceTransitionSupersedes(path, transition.ToSHA256, currentDigest) {
			return true
		}
	}
	return upstreamV0177SourceTransitionSupersedes(path, priorDigest, currentDigest)
}

func versionLeakDebtDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
