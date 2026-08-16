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

const codexModelsUnwiredTransportRetirementSHA256 = "44961fa04fbc4e3d24d94e9f75a202f5624fd62050068c4dd43819be00ed21c0"

type codexModelsUnwiredTransportRetirementReceipt struct {
	SchemaVersion         string `json:"schema_version"`
	Date                  string `json:"date"`
	BaseCommit            string `json:"base_commit"`
	Scope                 string `json:"scope"`
	ConsumerClosureBefore struct {
		ProductionWiringConsumers int    `json:"production_wiring_consumers"`
		ProductionSourceCallSites int    `json:"production_source_call_sites"`
		DirectTestCallSites       int    `json:"direct_test_call_sites"`
		FullServiceTestCoverage   string `json:"full_service_test_coverage"`
	} `json:"consumer_closure_before"`
	ConsumerClosureAfter struct {
		ProductionSourceCallSites int `json:"production_source_call_sites"`
		DirectTestCallSites       int `json:"direct_test_call_sites"`
	} `json:"consumer_closure_after"`
	CatalogItemRemovals    []string `json:"catalog_item_removals"`
	RemovalReceiptRequired bool     `json:"removal_receipt_required"`
	RetiredSymbols         []string `json:"retired_symbols"`
	RetiredBehaviors       []string `json:"retired_behaviors"`
	ReplacementBehavior    string   `json:"replacement_behavior"`
	RetainedSemantics      []string `json:"retained_product_semantics"`
	SourceTransitions      []struct {
		Path       string `json:"path"`
		FromSHA256 string `json:"from_sha256"`
		ToSHA256   string `json:"to_sha256"`
		Reason     string `json:"reason"`
	} `json:"source_transitions"`
	ScannerLockTransition struct {
		PriorLockSHA256        string `json:"prior_lock_sha256"`
		CurrentLockSHA256      string `json:"current_lock_sha256"`
		PriorAlgorithmSHA256   string `json:"prior_algorithm_sha256"`
		CurrentAlgorithmSHA256 string `json:"current_algorithm_sha256"`
	} `json:"scanner_lock_transition"`
	AllowedWireDeltas []string `json:"allowed_wire_deltas"`
	Verification      []string `json:"verification"`
}

func TestCodexModelsUnwiredTransportRetirementReceiptAndSourceExtinction(t *testing.T) {
	receiptPath := "../../../docs/egress/maintenance/codex-models-unwired-transport-retirement.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	if got := codexModelsUnwiredTransportRetirementDigest(raw); got != codexModelsUnwiredTransportRetirementSHA256 {
		t.Fatalf("Codex Models unwired transport 退休收据漂移：got=%s want=%s", got, codexModelsUnwiredTransportRetirementSHA256)
	}

	var receipt codexModelsUnwiredTransportRetirementReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		t.Fatal("Codex Models unwired transport 退休收据尾部存在额外 JSON")
	}
	if receipt.SchemaVersion != "official-egress.codex-models-unwired-transport-retirement/v1" ||
		receipt.Date != "2026-08-16" || receipt.BaseCommit == "" || receipt.Scope == "" ||
		receipt.ConsumerClosureBefore.ProductionWiringConsumers != 0 ||
		receipt.ConsumerClosureBefore.ProductionSourceCallSites != 1 ||
		receipt.ConsumerClosureBefore.DirectTestCallSites != 0 ||
		receipt.ConsumerClosureBefore.FullServiceTestCoverage != "0.0%" ||
		receipt.ConsumerClosureAfter.ProductionSourceCallSites != 0 ||
		receipt.ConsumerClosureAfter.DirectTestCallSites != 0 ||
		receipt.RemovalReceiptRequired || len(receipt.CatalogItemRemovals) != 0 ||
		len(receipt.RetiredSymbols) != 1 || len(receipt.RetiredBehaviors) != 2 ||
		receipt.ReplacementBehavior == "" || len(receipt.RetainedSemantics) != 4 ||
		len(receipt.SourceTransitions) != 5 ||
		receipt.ScannerLockTransition.PriorLockSHA256 == "" ||
		receipt.ScannerLockTransition.CurrentLockSHA256 == "" ||
		receipt.ScannerLockTransition.PriorAlgorithmSHA256 == "" ||
		receipt.ScannerLockTransition.CurrentAlgorithmSHA256 == "" ||
		len(receipt.AllowedWireDeltas) != 0 ||
		len(receipt.Verification) != 6 {
		t.Fatalf("Codex Models unwired transport 退休收据事实不完整：%+v", receipt)
	}

	repoRoot := filepath.Clean("../../..")
	closureRaw, err := os.ReadFile(filepath.Join(
		repoRoot,
		"docs/egress/maintenance/compatibility-code-retirement-closure.json",
	))
	if err != nil {
		t.Fatal(err)
	}
	var closure compatibilityCodeRetirementClosure
	if err := json.Unmarshal(closureRaw, &closure); err != nil {
		t.Fatal(err)
	}
	laterTransitions := make(map[string]compatibilityClosureSourceTransition)
	for _, transition := range closure.SourceTransitions {
		laterTransitions[transition.Path] = transition
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == "" || transition.FromSHA256 == "" ||
			transition.ToSHA256 == "" || transition.Reason == "" {
			t.Fatalf("Codex Models unwired transport 源码 transition 不完整：%+v", transition)
		}
		source, readErr := os.ReadFile(filepath.Join(repoRoot, filepath.FromSlash(transition.Path)))
		if readErr != nil {
			t.Fatal(readErr)
		}
		got := codexModelsUnwiredTransportRetirementDigest(source)
		if got == transition.ToSHA256 {
			continue
		}
		later, superseded := laterTransitions[transition.Path]
		if !superseded || later.FromSHA256 != transition.ToSHA256 || later.ToSHA256 != got {
			t.Fatalf("Codex Models unwired transport 源码摘要没有形成追加式 transition：path=%s got=%s prior=%s later=%+v", transition.Path, got, transition.ToSHA256, later)
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
		if bytes.Contains(source, []byte("doCodexModelsUnwiredTestTransport")) {
			t.Fatalf("已退休 Codex Models unwired transport 重新进入生产源码：%s", path)
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}

	productionSource, err := os.ReadFile("openai_codex_models_service.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, retained := range [][]byte{
		[]byte("OPENAI_CODEX_MODELS_UPSTREAM_NOT_CONFIGURED"),
		[]byte("newOfficialCodexHTTPInvocation("),
		[]byte("doOpenAIAPIKeyHTTPTransport("),
	} {
		if !bytes.Contains(productionSource, retained) {
			t.Fatalf("Codex Models unwired transport 退休误删必须保留的语义：%s", retained)
		}
	}
	if bytes.Contains(productionSource, []byte("httpclient.GetClient(")) {
		t.Fatal("Codex Models 生产源码仍可自行创建绕过统一 transport 的 httpclient")
	}

	testSource, err := os.ReadFile("openai_codex_models_service_test.go")
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Contains(testSource, []byte("TestFetchCodexModelsManifestMissingHTTPUpstreamFailsClosedWithoutNetwork")) ||
		!bytes.Contains(testSource, []byte("networkRequests.Load()")) {
		t.Fatal("Codex Models 缺失 HTTPUpstream 的零网络请求负例未保持闭环")
	}
}

func codexModelsUnwiredTransportRetirementDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
