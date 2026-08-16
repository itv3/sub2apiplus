package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

const catalogGenericBindRetirementSHA256 = "2d9cb043dfba2ef27403cfec86d10ab8cc7cb26f6592e21c99e751d947e82bef"

type catalogGenericBindRetirementReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	Date                  string `json:"date"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	ConsumerClosureBefore struct {
		BindContextProductionConsumers     int `json:"bind_context_production_consumers"`
		BindDefaultSinkProductionConsumers int `json:"bind_default_sink_production_consumers"`
		TestOnlyCallSites                  int `json:"test_only_call_sites"`
	} `json:"consumer_closure_before"`
	ConsumerClosureAfter struct {
		BindContextCallSites     int `json:"bind_context_call_sites"`
		BindDefaultSinkCallSites int `json:"bind_default_sink_call_sites"`
	} `json:"consumer_closure_after"`
	MigratedTestConsumers  string   `json:"migrated_test_consumers"`
	CatalogItemRemovals    []string `json:"catalog_item_removals"`
	RemovalReceiptRequired bool     `json:"removal_receipt_required"`
	RetiredSymbols         []string `json:"retired_symbols"`
	RetiredBehaviors       []string `json:"retired_behaviors"`
	RetainedSemantics      []string `json:"retained_product_semantics"`
	SourceTransitions      []struct {
		Path       string `json:"path"`
		FromSHA256 string `json:"from_sha256"`
		ToSHA256   string `json:"to_sha256"`
		Reason     string `json:"reason"`
	} `json:"source_transitions"`
	AllowedWireDeltas []string `json:"allowed_wire_deltas"`
	Verification      []string `json:"verification"`
}

func TestCatalogGenericBindCompatibilityRetirementReceiptAndSourceExtinction(t *testing.T) {
	receiptPath := "../../../docs/egress/maintenance/catalog-generic-bind-compat-retirement.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := catalogGenericBindRetirementDigest(raw); got != catalogGenericBindRetirementSHA256 {
		t.Fatalf("Catalog 通用 binding 退休收据漂移：got=%s want=%s", got, catalogGenericBindRetirementSHA256)
	}

	var receipt catalogGenericBindRetirementReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		t.Fatal("Catalog 通用 binding 退休收据尾部存在额外 JSON")
	}
	if receipt.SchemaVersion != "official-egress.catalog-generic-bind-compat-retirement/v1" ||
		receipt.Date != "2026-08-16" || receipt.BaseCommit == "" || receipt.Scope == "" ||
		receipt.ConsumerClosureBefore.BindContextProductionConsumers != 0 ||
		receipt.ConsumerClosureBefore.BindDefaultSinkProductionConsumers != 0 ||
		receipt.ConsumerClosureBefore.TestOnlyCallSites != 6 ||
		receipt.ConsumerClosureAfter.BindContextCallSites != 0 ||
		receipt.ConsumerClosureAfter.BindDefaultSinkCallSites != 0 ||
		receipt.MigratedTestConsumers == "" || receipt.RemovalReceiptRequired ||
		len(receipt.CatalogItemRemovals) != 0 || len(receipt.RetiredSymbols) != 2 ||
		len(receipt.RetiredBehaviors) != 3 || len(receipt.RetainedSemantics) != 4 ||
		len(receipt.SourceTransitions) != 5 || len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) != 5 {
		t.Fatalf("Catalog 通用 binding 退休收据事实不完整：%+v", receipt)
	}

	repoRoot := filepath.Clean("../../..")
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == "" || transition.FromSHA256 == "" ||
			transition.ToSHA256 == "" || transition.Reason == "" {
			t.Fatalf("Catalog 通用 binding 源码 transition 不完整：%+v", transition)
		}
		source, readErr := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(transition.Path)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := catalogGenericBindRetirementDigest(source); got != transition.ToSHA256 &&
			!compatibilityCodeRetirementTransitionSupersedes(transition.Path, transition.ToSHA256, got) {
			t.Fatalf("Catalog 通用 binding 源码摘要漂移：path=%s got=%s want=%s", transition.Path, got, transition.ToSHA256)
		}
	}

	retiredPatterns := [][]byte{
		[]byte(".BindContext("),
		[]byte("BindDefaultSink("),
	}
	backendRoot := filepath.Join(repoRoot, "backend")
	if err := filepath.WalkDir(backendRoot, func(path string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if entry.IsDir() || filepath.Ext(path) != ".go" || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		source, readErr := os.ReadFile(path)
		if readErr != nil {
			return readErr
		}
		for _, pattern := range retiredPatterns {
			if bytes.Contains(source, pattern) {
				t.Fatalf("已退休 Catalog 通用 binding 入口重新进入生产源码：path=%s pattern=%s", path, pattern)
			}
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	catalogSource, err := os.ReadFile("catalog.go")
	if err != nil {
		t.Fatal(err)
	}
	executorSource, err := os.ReadFile("executor.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, retained := range [][]byte{
		[]byte("func (c SinkCatalog) StartAttemptContext("),
		[]byte("func (c SinkCatalog) PreserveAttemptContext("),
		[]byte("func StartDefaultSinkAttempt("),
		[]byte("func PreserveDefaultSinkAttempt("),
	} {
		if !bytes.Contains(catalogSource, retained) {
			t.Fatalf("Catalog 通用 binding 退休误删必须保留的 attempt 入口：%s", retained)
		}
	}
	if !bytes.Contains(executorSource, []byte("WithAttemptMetadata(")) {
		t.Fatal("Catalog 通用 binding 退休误删 Executor 的终态身份写入路径")
	}
}

func catalogGenericBindRetirementDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
