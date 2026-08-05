package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const documentationCleanupRetirementSHA256 = "d7e2730f6fce3aa58596cb372724fadeb22ddaee650c4e1a03117e0f071b4c4c"

type documentationCleanupMachineProof struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type documentationCleanupRemovedFile struct {
	Path                     string                             `json:"path"`
	SHA256BeforeRemoval      string                             `json:"sha256_before_removal"`
	ReplacementMachineProofs []documentationCleanupMachineProof `json:"replacement_machine_proofs"`
}

func TestDocumentationCleanupRetirementIsFrozenAndComplete(t *testing.T) {
	receiptPath := "../../../docs/egress/maintenance/documentation-cleanup-retirement.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(raw); got != documentationCleanupRetirementSHA256 {
		t.Fatalf("文档清理退休收据漂移：got=%s want=%s", got, documentationCleanupRetirementSHA256)
	}
	var receipt struct {
		SchemaVersion           string                            `json:"schema_version"`
		Date                    string                            `json:"date"`
		PriorTransition         string                            `json:"prior_transition"`
		PriorTransitionSHA256   string                            `json:"prior_transition_sha256"`
		RemovedNarrativeFiles   []documentationCleanupRemovedFile `json:"removed_narrative_files"`
		RewrittenCurrentCatalog struct {
			Path       string `json:"path"`
			FromSHA256 string `json:"from_sha256"`
			ToSHA256   string `json:"to_sha256"`
			Result     string `json:"result"`
		} `json:"rewritten_current_catalog"`
		RuntimeBehaviorChanged bool   `json:"runtime_behavior_changed"`
		Result                 string `json:"result"`
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if receipt.SchemaVersion != "official-egress-documentation-cleanup-retirement/v1" ||
		receipt.Date != "2026-08-05" ||
		receipt.PriorTransition != "docs/egress/maintenance/evidence-directory-consolidation-source-transition.json" ||
		receipt.PriorTransitionSHA256 != evidenceDirectoryTransitionSHA256 ||
		receipt.RuntimeBehaviorChanged || receipt.Result != "passed" ||
		len(receipt.RemovedNarrativeFiles) != 3 {
		t.Fatalf("文档清理退休收据顶层事实非法：%+v", receipt)
	}

	expectedRemoved := map[string]string{
		"docs/egress/source-freeze/acceptance-report.md":   "24089b010460095e2a5cbffa2fadd18eb5f6979760d9ef8d9257d6ab67c901a0",
		"docs/egress/consolidation/acceptance-report.md":   "f6eedea5df1abaf89bc699b04c84ffef92632671622d28a2e6b435813d04a353",
		"docs/egress/validation/post/acceptance-report.md": "b42e027def9a0abe4356b54a46995cde8a56021ba52cbcc1b1ccd474af46f603",
	}
	for _, removed := range receipt.RemovedNarrativeFiles {
		expectedSHA, ok := expectedRemoved[removed.Path]
		if !ok || removed.SHA256BeforeRemoval != expectedSHA || len(removed.ReplacementMachineProofs) == 0 {
			t.Fatalf("文档清理退休条目非法：%+v", removed)
		}
		delete(expectedRemoved, removed.Path)
		if _, statErr := os.Stat(filepath.Join("../../..", filepath.FromSlash(removed.Path))); !errors.Is(statErr, os.ErrNotExist) {
			t.Fatalf("已退休叙事文件仍存在或状态异常：path=%s err=%v", removed.Path, statErr)
		}
		for _, proof := range removed.ReplacementMachineProofs {
			proofRaw, readErr := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(proof.Path)))
			if readErr != nil {
				t.Fatalf("读取替代机器证据 %s：%v", proof.Path, readErr)
			}
			if got := changeset3ReferenceSHA256(proofRaw); got != proof.SHA256 {
				t.Fatalf("替代机器证据漂移：path=%s got=%s want=%s", proof.Path, got, proof.SHA256)
			}
		}
	}
	if len(expectedRemoved) != 0 {
		t.Fatalf("文档清理退休收据缺少路径：%v", expectedRemoved)
	}

	catalog := receipt.RewrittenCurrentCatalog
	if catalog.Path != "docs/egress/foundation/persona-catalog.md" ||
		catalog.FromSHA256 != "5d8d24bce5966a6f4e7d698830940597eedfb545e115567d5e4a4ba4d797fbb3" ||
		catalog.ToSHA256 != "bced4154e6c7a9ea7a317ffcf6ddc7e07ea67accfa6b2d1dd5747420368f9ef5" ||
		catalog.Result != "current_rules_only" {
		t.Fatalf("persona 目录改写收据非法：%+v", catalog)
	}
	catalogRaw, err := os.ReadFile(filepath.Join("../../..", filepath.FromSlash(catalog.Path)))
	if err != nil {
		t.Fatal(err)
	}
	if got := changeset3ReferenceSHA256(catalogRaw); got != catalog.ToSHA256 {
		t.Fatalf("persona 目录漂移：got=%s want=%s", got, catalog.ToSHA256)
	}
	for _, stale := range []string{"sink-inventory.md", "前一版本", "建议删除"} {
		if strings.Contains(string(catalogRaw), stale) {
			t.Fatalf("persona 目录仍包含历史过程内容：%s", stale)
		}
	}
}
