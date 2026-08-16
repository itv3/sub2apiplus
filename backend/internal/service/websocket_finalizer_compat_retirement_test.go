package service

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

const websocketFinalizerCompatRetirementSHA256 = "6d04e3a5a2d56aab8ecbb181defcd5ce68611fb6d033f6ef1d0045ae220975a7"

const (
	retiredWebSocketFinalizer    = "finalizeOpenAIOfficialEgressWSFrame"
	currentWebSocketFramePrepare = "prepareOpenAIOfficialEgressSemanticWSFrame"
)

type websocketFinalizerCompatRetirementReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	Date                  string `json:"date"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	ConsumerClosureBefore struct {
		ProductionSymbolDefinitions int `json:"production_symbol_definitions"`
		ProductionCallSites         int `json:"production_call_sites"`
		DirectUnitTestCallSites     int `json:"direct_unit_test_call_sites"`
		LiveBenchmarkCallSites      int `json:"live_benchmark_call_sites"`
		FrozenEvidenceCallSites     int `json:"frozen_evidence_call_sites"`
	} `json:"consumer_closure_before"`
	ConsumerClosureAfter struct {
		ProductionSymbolDefinitions int `json:"production_symbol_definitions"`
		ProductionCallSites         int `json:"production_call_sites"`
		CurrentTestOldCallSites     int `json:"current_test_old_call_sites"`
		CurrentReplacementCallSites int `json:"current_replacement_call_sites"`
		FrozenEvidenceCallSites     int `json:"frozen_evidence_call_sites"`
	} `json:"consumer_closure_after"`
	CatalogItemRemovals    []string `json:"catalog_item_removals"`
	RemovalReceiptRequired bool     `json:"removal_receipt_required"`
	RetiredSymbols         []string `json:"retired_symbols"`
	ReplacementSymbol      string   `json:"replacement_symbol"`
	HistoricalBenchmark    struct {
		Immutable            bool     `json:"immutable"`
		SHA256               string   `json:"sha256"`
		FrozenCallSitesEach  int      `json:"frozen_call_sites_each"`
		LiveAdapterCallSites int      `json:"live_adapter_call_sites"`
		Paths                []string `json:"paths"`
		EquivalenceRule      string   `json:"equivalence_rule"`
	} `json:"historical_benchmark_evidence"`
	RetainedSemantics []string `json:"retained_product_semantics"`
	SourceTransitions []struct {
		Path       string `json:"path"`
		FromSHA256 string `json:"from_sha256"`
		ToSHA256   string `json:"to_sha256"`
		Reason     string `json:"reason"`
	} `json:"source_transitions"`
	AllowedWireDeltas []string `json:"allowed_wire_deltas"`
	Verification      []string `json:"verification"`
}

func TestWebSocketFinalizerCompatibilityRetirementReceiptAndSourceExtinction(t *testing.T) {
	repoRoot := filepath.Clean("../../..")
	receiptPath := filepath.Join(
		repoRoot,
		"docs/egress/maintenance/websocket-finalizer-compat-retirement.json",
	)
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := websocketFinalizerCompatRetirementDigest(raw); got != websocketFinalizerCompatRetirementSHA256 {
		t.Fatalf("WebSocket finalizer 兼容代码退休收据漂移：got=%s want=%s", got, websocketFinalizerCompatRetirementSHA256)
	}

	var receipt websocketFinalizerCompatRetirementReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		t.Fatal("WebSocket finalizer 兼容代码退休收据尾部存在额外 JSON")
	}
	before := receipt.ConsumerClosureBefore
	after := receipt.ConsumerClosureAfter
	historical := receipt.HistoricalBenchmark
	if receipt.SchemaVersion != "official-egress.websocket-finalizer-compat-retirement/v1" ||
		receipt.Date != "2026-08-16" || receipt.BaseCommit == "" || receipt.Scope == "" ||
		before.ProductionSymbolDefinitions != 1 || before.ProductionCallSites != 0 ||
		before.DirectUnitTestCallSites != 14 || before.LiveBenchmarkCallSites != 2 ||
		before.FrozenEvidenceCallSites != 4 ||
		after.ProductionSymbolDefinitions != 0 || after.ProductionCallSites != 0 ||
		after.CurrentTestOldCallSites != 0 || after.CurrentReplacementCallSites != 16 ||
		after.FrozenEvidenceCallSites != 4 ||
		receipt.RemovalReceiptRequired || len(receipt.CatalogItemRemovals) != 0 ||
		len(receipt.RetiredSymbols) != 1 || receipt.RetiredSymbols[0] != retiredWebSocketFinalizer ||
		receipt.ReplacementSymbol != currentWebSocketFramePrepare ||
		!historical.Immutable || historical.SHA256 == "" ||
		historical.FrozenCallSitesEach != 2 || historical.LiveAdapterCallSites != 2 ||
		len(historical.Paths) != 2 || historical.EquivalenceRule == "" ||
		len(receipt.RetainedSemantics) != 6 || len(receipt.SourceTransitions) != 4 ||
		len(receipt.AllowedWireDeltas) != 0 || len(receipt.Verification) != 7 {
		t.Fatalf("WebSocket finalizer 兼容代码退休收据事实不完整：%+v", receipt)
	}

	for _, transition := range receipt.SourceTransitions {
		if transition.Path == "" || transition.FromSHA256 == "" ||
			transition.ToSHA256 == "" || transition.Reason == "" {
			t.Fatalf("WebSocket finalizer 源码 transition 不完整：%+v", transition)
		}
		source, readErr := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(transition.Path)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := websocketFinalizerCompatRetirementDigest(source); got != transition.ToSHA256 &&
			!versionLeakDebtTransitionSupersedes(transition.Path, transition.ToSHA256, got) {
			t.Fatalf("WebSocket finalizer 源码摘要漂移：path=%s got=%s want=%s", transition.Path, got, transition.ToSHA256)
		}
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
		if bytes.Contains(source, []byte(retiredWebSocketFinalizer)) {
			t.Fatalf("已退休 WebSocket finalizer 重新进入生产源码：%s", path)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	assertWebSocketFinalizerCurrentTestSource(t, "official_egress_openai_ws_test.go", 14)
	assertWebSocketFinalizerCurrentTestSource(t, "official_egress_benchmark_test.go", 2)

	for _, historicalPath := range historical.Paths {
		historicalSource, readErr := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(historicalPath)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := websocketFinalizerCompatRetirementDigest(historicalSource); got != historical.SHA256 {
			t.Fatalf("冻结 Profile benchmark driver 漂移：path=%s got=%s want=%s", historicalPath, got, historical.SHA256)
		}
		if got := bytes.Count(historicalSource, []byte(retiredWebSocketFinalizer+"(")); got != historical.FrozenCallSitesEach {
			t.Fatalf("冻结 Profile benchmark finalizer 调用点漂移：path=%s got=%d want=%d", historicalPath, got, historical.FrozenCallSitesEach)
		}
	}

	authorityGate, err := os.ReadFile("official_egress_changeset3_authority_ast_test.go")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(authorityGate, []byte("\""+retiredWebSocketFinalizer+"\":       true")) {
		t.Fatal("Runtime Sink 对已退休 WebSocket finalizer 的 AST 禁止规则缺失")
	}

	benchmarkGate, err := os.ReadFile(filepath.Join(repoRoot, "tools/changeset6_benchmark_evidence.py"))
	if err != nil {
		t.Fatal(err)
	}
	for _, proof := range [][]byte{
		[]byte("PROFILE_LIVE_CALLEE_DELTA_COUNT = 2"),
		[]byte("recovered_frozen_profile_driver"),
		[]byte("Profile benchmark driver 非法 mutation 未被退休等价门禁拒绝"),
	} {
		if !bytes.Contains(benchmarkGate, proof) {
			t.Fatalf("Profile benchmark 退休等价门禁缺少证明：%s", proof)
		}
	}
}

func assertWebSocketFinalizerCurrentTestSource(t *testing.T, path string, replacementCalls int) {
	t.Helper()
	source, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Contains(source, []byte(retiredWebSocketFinalizer)) {
		t.Fatalf("现行测试仍调用已退休 WebSocket finalizer：%s", path)
	}
	if got := bytes.Count(source, []byte(currentWebSocketFramePrepare+"(")); got != replacementCalls {
		t.Fatalf("现行测试的 WebSocket 语义帧转换调用闭集漂移：path=%s got=%d want=%d", path, got, replacementCalls)
	}
}

func websocketFinalizerCompatRetirementDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
