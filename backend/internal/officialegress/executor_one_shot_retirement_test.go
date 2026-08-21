package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const (
	executorOneShotRetirementSHA256 = "ecfb18939dd513156349ab03b3004b875d9879c8a04b92469961b60211e9efb6"
	executorOneShotSourceSHA256     = "73b79e4061a8e3becabb5a361aa341c855ddd0296a94fd2057296d798b6048ec"
)

type executorOneShotRetirementReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	Date                  string `json:"date"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	ConsumerClosureBefore struct {
		PrepareConsumers int    `json:"executor_prepare_production_consumers"`
		ExecuteConsumers int    `json:"executor_execute_production_consumers"`
		ExecuteConsumer  string `json:"executor_execute_consumer"`
	} `json:"consumer_closure_before"`
	MigratedConsumer       string   `json:"migrated_consumer"`
	CatalogItemRemovals    []string `json:"catalog_item_removals"`
	RemovalReceiptRequired bool     `json:"removal_receipt_required"`
	RetiredSymbols         []string `json:"retired_symbols"`
	RetiredBehaviors       []string `json:"retired_behaviors"`
	RetainedSemantics      []string `json:"retained_product_semantics"`
	SourceTransition       struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"source_transition_receipt"`
	SourceTransitions []changeset4SourceTransitionEntry `json:"source_transitions"`
	AllowedWireDeltas []string                          `json:"allowed_wire_deltas"`
	Verification      []string                          `json:"verification"`
}

func TestExecutorOneShotCompatibilityRetirementReceiptAndSourceExtinction(t *testing.T) {
	receiptPath := "../../../docs/egress/maintenance/executor-one-shot-compat-retirement.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := executorRetirementSHA256(raw); got != executorOneShotRetirementSHA256 {
		t.Fatalf("Executor 一次性兼容退休收据漂移：got=%s want=%s", got, executorOneShotRetirementSHA256)
	}
	var receipt executorOneShotRetirementReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		t.Fatal("Executor 一次性兼容退休收据尾部存在额外 JSON")
	}
	if receipt.SchemaVersion != "official-egress.executor-one-shot-compat-retirement/v1" ||
		receipt.Date != "2026-08-16" || receipt.BaseCommit == "" || receipt.Scope == "" ||
		receipt.ConsumerClosureBefore.PrepareConsumers != 0 ||
		receipt.ConsumerClosureBefore.ExecuteConsumers != 1 ||
		!strings.Contains(receipt.ConsumerClosureBefore.ExecuteConsumer, "ExecuteCodexHTTP") ||
		receipt.MigratedConsumer == "" || receipt.RemovalReceiptRequired ||
		len(receipt.CatalogItemRemovals) != 0 || len(receipt.RetiredSymbols) != 2 ||
		len(receipt.RetiredBehaviors) != 3 || len(receipt.RetainedSemantics) != 4 ||
		len(receipt.SourceTransitions) != 2 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) != 5 {
		t.Fatalf("Executor 一次性兼容退休收据事实不完整：%+v", receipt)
	}
	if receipt.SourceTransition.Path != "docs/egress/maintenance/executor-one-shot-source-transition.json" ||
		receipt.SourceTransition.SHA256 != executorOneShotSourceSHA256 {
		t.Fatalf("Executor source-transition 引用非法：%+v", receipt.SourceTransition)
	}

	repoRoot := filepath.Clean("../../..")
	multiPersonaTransition := loadMultiPersonaControlSourceTransition(t)
	multiPersonaV2Transition := loadMultiPersonaControlSourceTransitionV2(t)
	claudeFWGTransition := loadClaudeFWGSourceTransition(t)
	claudeFWHTransition := loadClaudeFWHSourceTransition(t)
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == "" || transition.FromSHA256 == "" ||
			transition.ToSHA256 == "" || transition.Reason == "" {
			t.Fatalf("Executor 一次性兼容源码 transition 不完整：%+v", transition)
		}
		source, readErr := os.ReadFile(filepath.Join(repoRoot, transition.Path))
		if readErr != nil {
			t.Fatal(readErr)
		}
		expected := transition.ToSHA256
		if approved, ok := multiPersonaTransition[transition.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("多 Persona 控制层未承接 Executor 一次性兼容摘要：%s", transition.Path)
			}
			expected = approved.ToSHA256
		}
		if approved, ok := multiPersonaV2Transition[transition.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("多 Persona 控制层 v2 未承接上一层摘要：%s", transition.Path)
			}
			expected = approved.ToSHA256
		}
		if approved, ok := claudeFWGTransition[transition.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Claude FW-G source transition 未承接上一层摘要：%s", transition.Path)
			}
			expected = approved.ToSHA256
		}
		if approved, ok := claudeFWHTransition[transition.Path]; ok {
			if approved.FromSHA256 != expected || strings.TrimSpace(approved.Reason) == "" {
				t.Fatalf("Claude FW-H source transition 未承接上一层摘要：%s", transition.Path)
			}
			expected = approved.ToSHA256
		}
		if got := executorRetirementSHA256(source); got != expected {
			t.Fatalf("Executor 一次性兼容源码摘要漂移：path=%s got=%s want=%s", transition.Path, got, expected)
		}
	}

	executorSource, err := os.ReadFile("executor.go")
	if err != nil {
		t.Fatal(err)
	}
	serviceSource, err := os.ReadFile("../service/official_egress_1b_executor.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, symbol := range receipt.RetiredSymbols {
		if bytes.Contains(executorSource, []byte(symbol)) {
			t.Fatalf("已退休 Executor 一次性兼容符号重新进入生产源码：%s", symbol)
		}
	}
	if bytes.Contains(serviceSource, []byte("r.CodexExecutor.Execute(")) ||
		!bytes.Contains(serviceSource, []byte("r.CodexExecutor.BeginInvocation(")) ||
		!bytes.Contains(serviceSource, []byte("invocation.ExecuteAttempt(")) ||
		!bytes.Contains(serviceSource, []byte("ExpectedAttemptOrdinal: 1")) ||
		!bytes.Contains(executorSource, []byte("ExpectedAttemptOrdinal 必须显式设置")) {
		t.Fatal("Executor 一次性兼容退休或显式 ordinal 门禁未保持闭环")
	}
}

func executorRetirementSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
