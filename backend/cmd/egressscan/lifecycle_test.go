package main

import (
	"crypto/sha256"
	"encoding/hex"
	"os"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

func TestLifecycleReceiptsFreezeCompleteCandidates(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/egress/foundation/sink-baseline.json")
	if err != nil {
		t.Fatal(err)
	}
	baseline, err := decodeBaseline(raw)
	if err != nil {
		t.Fatal(err)
	}
	var runtimeCandidate, deadCandidate, outOfScopeCandidate SinkRecord
	for _, candidate := range baseline.Sinks {
		if runtimeCandidate.ScanCandidateID == "" && candidate.RuntimeSinkID != "" &&
			!candidate.IsFacade && candidate.EnforcementState == "legacy_observe" {
			runtimeCandidate = candidate
		}
		if deadCandidate.ScanCandidateID == "" &&
			(candidate.Persona == "dead-code" || candidate.EnforcementState == "pending_removal") {
			deadCandidate = candidate
		}
		if outOfScopeCandidate.ScanCandidateID == "" && candidate.Persona == "out-of-scope" {
			outOfScopeCandidate = candidate
		}
	}
	if runtimeCandidate.ScanCandidateID == "" || deadCandidate.ScanCandidateID == "" ||
		outOfScopeCandidate.ScanCandidateID == "" {
		t.Fatal("测试基线缺少 runtime/dead/out-of-scope 候选")
	}
	digest := strings.Repeat("a", 64)
	supplement := reviewedSupplement{
		Candidate: runtimeCandidate, SourceBlobSHA256: digest,
		ReviewedBy: "reviewer", ReviewRef: "review/ref", Rationale: "历史遗漏补录",
	}
	if err := validateReviewedSupplement(supplement); err != nil {
		t.Fatalf("完整补录收据被拒绝：%v", err)
	}
	missingEvidence := supplement
	missingEvidence.Candidate.ASTFingerprint = ""
	if err := validateReviewedSupplement(missingEvidence); err == nil {
		t.Fatal("缺少 AST 指纹的补录收据未被拒绝")
	}
	deadRemoval := removalReceipt{
		Candidate: deadCandidate, Kind: "dead_code_removed", SourceBlobSHA256: digest,
		ReviewedBy: "reviewer", ReviewRef: "review/ref", Rationale: "删除已确认死代码",
	}
	if err := validateRemovalReceipt(deadRemoval, nil); err != nil {
		t.Fatalf("dead-code 移除收据被拒绝：%v", err)
	}
	migration := removalReceipt{
		Candidate: runtimeCandidate, Kind: "migrated", SourceBlobSHA256: digest,
		ReplacementSinkID: runtimeCandidate.RuntimeSinkID, MigrationReceiptDigest: strings.Repeat("b", 64),
		ReviewedBy: "reviewer", ReviewRef: "review/ref", Rationale: "旧裸调用已迁移",
	}
	migrations := migrationReceiptIndex{
		migration.MigrationReceiptDigest: {
			SinkID: migration.ReplacementSinkID,
			Candidates: []receiptcontract.CandidateEvidence{{
				ScanCandidateID: runtimeCandidate.ScanCandidateID,
				ASTFingerprint:  runtimeCandidate.ASTFingerprint,
			}},
		},
	}
	if err := validateRemovalReceipt(migration, migrations); err != nil {
		t.Fatalf("迁移移除收据被拒绝：%v", err)
	}
	if err := validateRemovalReceipt(migration, nil); err == nil {
		t.Fatal("引用不存在 MigrationReceipt 的迁移移除未被拒绝")
	}
	delegated := removalReceipt{
		Candidate: runtimeCandidate, Kind: "legacy_delegated", SourceBlobSHA256: digest,
		ReplacementSinkID:     runtimeCandidate.RuntimeSinkID,
		DelegationCandidateID: "reviewed-dispatcher-candidate",
		ReviewedBy:            "reviewer", ReviewRef: "review/ref", Rationale: "旧调用改接受审 Dispatcher",
	}
	if err := validateRemovalReceipt(delegated, nil); err != nil {
		t.Fatalf("legacy delegation 收据被拒绝：%v", err)
	}
	retired := delegated
	retired.Candidate.IsFacade = true
	retired.Kind = "legacy_retired"
	retired.ReplacementSinkID = ""
	retired.DelegationCandidateID = ""
	retired.Rationale = "旧 facade 已由 fail-close 与 Executor 绝迹门禁替代"
	if err := validateRemovalReceipt(retired, nil); err != nil {
		t.Fatalf("legacy facade 退休收据被拒绝：%v", err)
	}
	outOfScopeRefactor := removalReceipt{
		Candidate: outOfScopeCandidate, Kind: "out_of_scope_refactored", SourceBlobSHA256: digest,
		ReviewedBy: "reviewer", ReviewRef: "review/ref", Rationale: "范围外发送点由上游重构收口",
	}
	if err := validateRemovalReceipt(outOfScopeRefactor, nil); err != nil {
		t.Fatalf("范围外重构收据被拒绝：%v", err)
	}
	outOfScopeRefactor.ReplacementSinkID = "forbidden"
	if err := validateRemovalReceipt(outOfScopeRefactor, nil); err == nil {
		t.Fatal("范围外重构收据声明迁移目标时未被拒绝")
	}
}

func TestHistoricalRetirementClassificationRequiresExactReceipt(t *testing.T) {
	reviewedID := "reviewed-retired-candidate"
	records := []SinkRecord{
		{ScanCandidateID: reviewedID, Func: "retiredFunction", File: "retired.go", Line: 10},
		{ScanCandidateID: "unreviewed-candidate", Func: "newFunction", File: "new.go", Line: 20},
	}
	frozen := SinkRecord{
		ScanCandidateID:    reviewedID,
		RuntimeSinkID:      "codex.oauth.refresh",
		Purpose:            "oauth_refresh",
		Persona:            "codex-cli",
		EndpointEvidence:   "codex_profile",
		Routes:             []string{"POST auth.openai.com/oauth/token"},
		Backend:            "req_profile",
		TargetBackend:      "req_profile",
		EnforcementState:   "legacy_observe",
		Owner:              "reviewer",
		MigrationChangeset: "2",
		ExpiryCondition:    "已由 RemovalReceipt 证明退休",
		Rationale:          "历史分类由追加式收据冻结",
	}

	classified, unclassified := applyClassificationWithHistory(
		records,
		map[string]SinkRecord{reviewedID: frozen},
	)
	if len(unclassified) != 1 || !strings.Contains(unclassified[0], "unreviewed-candidate") {
		t.Fatalf("未受审候选没有保持硬失败：%v", unclassified)
	}
	if classified[0].RuntimeSinkID != frozen.RuntimeSinkID ||
		classified[0].Persona != frozen.Persona ||
		classified[0].File != records[0].File || classified[0].Line != records[0].Line {
		t.Fatalf("历史收据分类没有只覆盖分类字段：%+v", classified[0])
	}
}

func TestBootstrapInventoryLockMatchesCurrentReviewedScanner(t *testing.T) {
	historicalLockRaw, err := os.ReadFile("../../../docs/egress/consolidation/bootstrap-inventory-lock.json")
	if err != nil {
		t.Fatal(err)
	}
	historicalLockSum := sha256.Sum256(historicalLockRaw)
	if hex.EncodeToString(historicalLockSum[:]) != "7c1f89db30fc4164c5b6a186a338242433416d97695885ee62c2215e1d61c877" {
		t.Fatal("变更集 5 bootstrap inventory lock 摘要漂移")
	}
	currentLockRaw, err := os.ReadFile("../../../docs/egress/maintenance/bootstrap-inventory-lock.json")
	if err != nil {
		t.Fatal(err)
	}
	currentLockSum := sha256.Sum256(currentLockRaw)
	if hex.EncodeToString(currentLockSum[:]) != "88d027eefa197d564d1bc3a902581be412768239a06902facf448d51e5f7d24c" {
		t.Fatal("当前 bootstrap inventory lock 摘要漂移")
	}
	baseline, err := os.ReadFile("../../../docs/egress/foundation/sink-baseline.json")
	if err != nil {
		t.Fatal(err)
	}
	if err := verifyBootstrapInventoryLock(
		"../../../docs/egress/maintenance/bootstrap-inventory-lock.json",
		baseline,
		".",
	); err != nil {
		t.Fatal(err)
	}
}
