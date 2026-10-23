package main

import (
	"os"
	"strings"
	"testing"
)

func TestScopeExcludedRemovalRequiresHistoricalAmendment(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/egress/foundation/sink-baseline.json")
	if err != nil {
		t.Fatal(err)
	}
	baseline, err := decodeBaseline(raw)
	if err != nil {
		t.Fatal(err)
	}
	amendments, err := loadScannerCatalogAmendments("../../../docs/egress/lifecycle/catalog-amendments.json")
	if err != nil {
		t.Fatal(err)
	}
	var candidate SinkRecord
	for _, frozen := range baseline.Sinks {
		if frozen.RuntimeSinkID == "codex.admin_test.keeper" {
			candidate = frozen
			break
		}
	}
	if candidate.ScanCandidateID == "" {
		t.Fatal("历史基线缺少账号保活调用")
	}
	receipt := removalReceipt{
		Candidate: candidate, Kind: "scope_excluded_removed", ScopeExclusionSinkID: candidate.RuntimeSinkID,
		SourceBlobSHA256: strings.Repeat("a", 64), ReviewedBy: "下线审核", ReviewRef: "审核记录", Rationale: "完整删除账号保活",
	}
	if err := validateRemovalReceipt(receipt, nil); err != nil {
		t.Fatal(err)
	}
	if err := validateScopeExcludedRemoval(receipt, amendments.byCandidateID()); err != nil {
		t.Fatal(err)
	}
	if err := validateScopeExcludedRemoval(receipt, nil); err == nil {
		t.Fatal("缺少历史排除证据的删除未被拒绝")
	}
	for _, field := range []string{"kind", "sink", "ast", "function"} {
		t.Run(field, func(t *testing.T) {
			index := amendments.byCandidateID()
			amendment := index[candidate.ScanCandidateID]
			switch field {
			case "kind":
				amendment.Kind = scannerAmendmentRouteEvidence
			case "sink":
				amendment.SinkID = "other.sink"
			case "ast":
				amendment.Source.ASTFingerprint = "unreviewed"
			case "function":
				amendment.Source.Function = "otherFunction"
			}
			index[candidate.ScanCandidateID] = amendment
			if err := validateScopeExcludedRemoval(receipt, index); err == nil {
				t.Fatal("不匹配的历史排除证据未被拒绝")
			}
		})
	}
	invalid := receipt
	invalid.ReplacementSinkID = "forbidden"
	if err := validateRemovalReceipt(invalid, nil); err == nil {
		t.Fatal("声明替代发送路径的范围排除删除未被拒绝")
	}
	invalid = receipt
	invalid.Kind = "out_of_scope_refactored"
	if err := validateRemovalReceipt(invalid, nil); err == nil {
		t.Fatal("原始官方分类被伪装成范围外分类时未被拒绝")
	}
}
