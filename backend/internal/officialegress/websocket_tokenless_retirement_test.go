package officialegress

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

const websocketTokenlessRetirementSHA256 = "d05b87932cfe15798e8f1b39a50f8f8efe6aaaef5a0b5037168d0084a87714e2"

type websocketTokenlessRetirementReceipt struct {
	SchemaVersion          string   `json:"schema_version"`
	Date                   string   `json:"date"`
	BaseCommit             string   `json:"base_commit"`
	Scope                  string   `json:"scope"`
	CatalogItemRemovals    []string `json:"catalog_item_removals"`
	RemovalReceiptRequired bool     `json:"removal_receipt_required"`
	RetiredBehaviors       []string `json:"retired_behaviors"`
	RetiredSymbols         []string `json:"retired_symbols"`
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

func TestWebSocketTokenlessCompatibilityRetirementReceiptAndSourceExtinction(t *testing.T) {
	receiptPath := "../../../docs/egress/maintenance/websocket-tokenless-compat-retirement.json"
	raw, err := os.ReadFile(receiptPath)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if got := hex.EncodeToString(sum[:]); got != websocketTokenlessRetirementSHA256 {
		t.Fatalf("WS 无 token 兼容退休收据漂移：got=%s want=%s", got, websocketTokenlessRetirementSHA256)
	}
	var receipt websocketTokenlessRetirementReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); err != io.EOF {
		t.Fatal("WS 无 token 兼容退休收据尾部存在额外 JSON")
	}
	if receipt.SchemaVersion != "official-egress.websocket-tokenless-compat-retirement/v1" ||
		receipt.Date != "2026-08-16" || receipt.BaseCommit == "" || receipt.Scope == "" ||
		receipt.RemovalReceiptRequired || len(receipt.CatalogItemRemovals) != 0 ||
		len(receipt.RetiredBehaviors) != 2 || len(receipt.RetiredSymbols) != 4 ||
		len(receipt.RetainedSemantics) != 4 || len(receipt.SourceTransitions) != 3 ||
		len(receipt.AllowedWireDeltas) != 0 || len(receipt.Verification) != 6 {
		t.Fatalf("WS 无 token 兼容退休收据事实不完整：%+v", receipt)
	}

	repoRoot := filepath.Clean("../../..")
	productionSources := make([]byte, 0)
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == "" || transition.FromSHA256 == "" || transition.ToSHA256 == "" || transition.Reason == "" {
			t.Fatalf("WS 无 token 兼容源码 transition 不完整：%+v", transition)
		}
		source, readErr := os.ReadFile(filepath.Join(repoRoot, transition.Path))
		if readErr != nil {
			t.Fatal(readErr)
		}
		sourceSum := sha256.Sum256(source)
		if got := hex.EncodeToString(sourceSum[:]); got != transition.ToSHA256 &&
			!websocketRetirementTransitionSuperseded(t, transition.Path, transition.ToSHA256, got) {
			t.Fatalf("WS 无 token 兼容源码摘要漂移：path=%s got=%s want=%s", transition.Path, got, transition.ToSHA256)
		}
		productionSources = append(productionSources, source...)
	}
	for _, symbol := range receipt.RetiredSymbols {
		if bytes.Contains(productionSources, []byte(symbol)) {
			t.Fatalf("已退休 WS 无 token 兼容符号重新进入生产源码：%s", symbol)
		}
	}
	if !bytes.Contains(productionSources, []byte("metadata.Token == nil")) ||
		!bytes.Contains(productionSources, []byte(`if req.SinkID != ""`)) ||
		!bytes.Contains(productionSources, []byte("TokenBoundAcquire")) {
		t.Fatal("WS 无 token 兼容退休误删 fail-close 或必须保留的产品语义")
	}
}

// websocketRetirementTransitionSuperseded 只接受由后续已冻结退休收据连续承接的源码变化。
// 旧收据保持不可变；后续变更必须从旧 to 摘要出发，并精确落到当前源码摘要。
func websocketRetirementTransitionSuperseded(t *testing.T, path, priorDigest, currentDigest string) bool {
	t.Helper()
	raw, err := os.ReadFile("../../../docs/egress/maintenance/catalog-generic-bind-compat-retirement.json")
	if err != nil || catalogGenericBindRetirementDigest(raw) != catalogGenericBindRetirementSHA256 {
		return false
	}
	var receipt catalogGenericBindRetirementReceipt
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		return false
	}
	for _, transition := range receipt.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
	}
	return false
}
