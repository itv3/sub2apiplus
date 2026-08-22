package officialegress

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
)

type imagesDeadCodeRetirementReceipt struct {
	SchemaVersion string `json:"schema_version"`
	Date          string `json:"date"`
	Scope         string `json:"scope"`
	Historical    struct {
		Path                string `json:"path"`
		SHA256BeforeRemoval string `json:"sha256_before_removal"`
		SHA256AfterRemoval  string `json:"sha256_after_removal"`
	} `json:"historical_source"`
	ConsumerClosure struct {
		ProductionRootCount     int      `json:"production_root_count"`
		TestOnlyRootCount       int      `json:"test_only_root_count"`
		RetiredSinkIDs          []string `json:"retired_sink_ids"`
		RemovedSymbols          []string `json:"removed_symbols"`
		RemovedTests            []string `json:"removed_tests"`
		RetainedProductSemantic []string `json:"retained_product_semantics"`
	} `json:"consumer_closure"`
	RemovalReceipt struct {
		Path         string `json:"path"`
		SHA256       string `json:"sha256"`
		Kind         string `json:"kind"`
		ReceiptCount int    `json:"receipt_count"`
	} `json:"removal_receipt"`
	CatalogTransition struct {
		HistoricalReleaseBindingCount int  `json:"historical_release_binding_count"`
		CurrentRuntimeCatalogCount    int  `json:"current_runtime_catalog_count"`
		PendingRemovalBefore          int  `json:"pending_removal_before"`
		PendingRemovalAfter           int  `json:"pending_removal_after"`
		HistoricalBindingPreserved    bool `json:"historical_release_binding_preserved"`
		RetirementOverlayRequired     bool `json:"retirement_overlay_required"`
	} `json:"catalog_transition"`
	WireVerification struct {
		AllowedDeltas []string `json:"allowed_deltas"`
		Result        string   `json:"result"`
		Reason        string   `json:"reason"`
	} `json:"wire_verification"`
	ScannerLockTransition struct {
		PriorLockSHA256        string `json:"prior_lock_sha256"`
		CurrentLockPath        string `json:"current_lock_path"`
		CurrentLockSHA256      string `json:"current_lock_sha256"`
		PriorAlgorithmSHA256   string `json:"prior_algorithm_sha256"`
		CurrentAlgorithmSHA256 string `json:"current_algorithm_sha256"`
		Reason                 string `json:"reason"`
	} `json:"scanner_lock_transition"`
	RequiredVerification []string `json:"required_verification"`
}

func TestCatalogRetirementsRemoveOnlyFrozenDeadCode(t *testing.T) {
	manifest, err := loadCatalogRetirementManifest()
	if err != nil {
		t.Fatal(err)
	}
	if len(manifest.Receipts) != 2 {
		t.Fatalf("Images dead-code RemovalReceipt 数量=%d，期望 2", len(manifest.Receipts))
	}

	historicalDoc, err := bindingcontract.ParseBindingCatalog(embeddedReleaseBindings)
	if err != nil {
		t.Fatal(err)
	}
	historical, err := bindingcontract.NewBindingCatalog(historicalDoc)
	if err != nil {
		t.Fatal(err)
	}
	for _, sinkID := range []string{"dead.images.download_blob", "dead.images.fetch_url"} {
		if _, ok := historical.Resolve(sinkID); !ok {
			t.Fatalf("冻结 ReleaseBinding 丢失退休历史证据: %s", sinkID)
		}
		if _, ok := DefaultSinkCatalog().Resolve(SinkID(sinkID)); ok {
			t.Fatalf("已签发 RemovalReceipt 的 dead-code 仍在当前 Runtime Catalog: %s", sinkID)
		}
	}

	gotIDs := make([]string, 0, len(manifest.Receipts))
	for _, receipt := range manifest.Receipts {
		var candidate catalogRetirementCandidate
		if err := decodeCatalogRetirementCandidate(receipt.Candidate, &candidate); err != nil {
			t.Fatal(err)
		}
		gotIDs = append(gotIDs, candidate.RuntimeSinkID)
	}
	slices.Sort(gotIDs)
	wantIDs := []string{"dead.images.download_blob", "dead.images.fetch_url"}
	if !slices.Equal(gotIDs, wantIDs) {
		t.Fatalf("Catalog 退休闭集漂移: got=%v want=%v", gotIDs, wantIDs)
	}
}

func TestImagesDeadCodeRetirementReceiptIsComplete(t *testing.T) {
	receiptRaw, err := os.ReadFile("../../../docs/egress/maintenance/openai-images-dead-code-retirement.json")
	if err != nil {
		t.Fatal(err)
	}
	const receiptSHA256 = "14ad4d91e6168c74b618bfd01324f6cf2f953f5a392d6321c5e0cdd258e44c94"
	if got := sha256Hex(receiptRaw); got != receiptSHA256 {
		t.Fatalf("Images dead-code 退休收据漂移: got=%s want=%s", got, receiptSHA256)
	}
	var receipt imagesDeadCodeRetirementReceipt
	decoder := json.NewDecoder(bytes.NewReader(receiptRaw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&receipt); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("Images dead-code 退休收据尾部存在额外 JSON")
	}
	if receipt.SchemaVersion != "official-egress-images-dead-code-retirement/v1" ||
		receipt.Date == "" || receipt.Scope == "" ||
		receipt.Historical.Path != "backend/internal/service/openai_images.go" ||
		receipt.Historical.SHA256BeforeRemoval != "43f6e940277a060a743566cef4fb4f6e19d2e9ef78761a2b47dcfd8b0fe73769" ||
		receipt.ConsumerClosure.ProductionRootCount != 0 ||
		receipt.ConsumerClosure.TestOnlyRootCount != 2 || len(receipt.ConsumerClosure.RemovedSymbols) != 20 ||
		len(receipt.ConsumerClosure.RemovedTests) != 2 || len(receipt.ConsumerClosure.RetainedProductSemantic) != 3 {
		t.Fatalf("Images dead-code 退休事实不完整: %+v", receipt)
	}
	if !slices.Equal(receipt.ConsumerClosure.RetiredSinkIDs,
		[]string{"dead.images.download_blob", "dead.images.fetch_url"}) ||
		receipt.RemovalReceipt.Path != "backend/internal/officialegress/catalogdata/maintenance-removal-receipts.json" ||
		receipt.RemovalReceipt.Kind != "dead_code_removed" || receipt.RemovalReceipt.ReceiptCount != 2 ||
		receipt.CatalogTransition.HistoricalReleaseBindingCount != 34 ||
		receipt.CatalogTransition.CurrentRuntimeCatalogCount != 32 ||
		receipt.CatalogTransition.PendingRemovalBefore != 2 ||
		receipt.CatalogTransition.PendingRemovalAfter != 0 ||
		!receipt.CatalogTransition.HistoricalBindingPreserved ||
		!receipt.CatalogTransition.RetirementOverlayRequired ||
		len(receipt.WireVerification.AllowedDeltas) != 0 ||
		receipt.WireVerification.Result != "no_runtime_wire_change" ||
		len(receipt.RequiredVerification) != 6 {
		t.Fatalf("Images dead-code 退休闭环非法: %+v", receipt)
	}
	for path, want := range map[string]string{
		receipt.Historical.Path:                       receipt.Historical.SHA256AfterRemoval,
		receipt.RemovalReceipt.Path:                   receipt.RemovalReceipt.SHA256,
		receipt.ScannerLockTransition.CurrentLockPath: receipt.ScannerLockTransition.CurrentLockSHA256,
	} {
		raw, readErr := os.ReadFile("../../../" + path)
		if readErr != nil {
			t.Fatal(readErr)
		}
		if got := sha256Hex(raw); got != want &&
			!imagesRetirementReferenceWasSuperseded(path, want, got) {
			t.Fatalf("Images dead-code 退休引用摘要漂移: path=%s got=%s want=%s", path, got, want)
		}
	}
}

// imagesRetirementReferenceWasSuperseded 只允许后续已冻结 transition 连续承接历史摘要。
// Images 历史收据保持不可变，其他引用仍必须与其当时摘要逐字一致。
func imagesRetirementReferenceWasSuperseded(path, priorDigest, currentDigest string) bool {
	if upstreamV0177SourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if path != "docs/egress/maintenance/bootstrap-inventory-lock.json" {
		return false
	}
	raw, err := os.ReadFile("../../../docs/egress/maintenance/codex-models-unwired-transport-retirement.json")
	if err != nil || sha256Hex(raw) != "44961fa04fbc4e3d24d94e9f75a202f5624fd62050068c4dd43819be00ed21c0" {
		return false
	}
	var receipt struct {
		ScannerLockTransition struct {
			PriorLockSHA256   string `json:"prior_lock_sha256"`
			CurrentLockSHA256 string `json:"current_lock_sha256"`
		} `json:"scanner_lock_transition"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil {
		return false
	}
	if receipt.ScannerLockTransition.PriorLockSHA256 != priorDigest {
		return false
	}
	intermediateDigest := receipt.ScannerLockTransition.CurrentLockSHA256
	return intermediateDigest == currentDigest ||
		compatibilityCodeRetirementTransitionSupersedes(path, intermediateDigest, currentDigest)
}

// compatibilityCodeRetirementTransitionSupersedes 验证历史退休收据的目标摘要是否由
// 当前 5.2 闭集收据或其后的版本泄漏债务闭集连续承接。历史收据保持不可变，只有
// 精确的 path/from/to 链可以解释后续合法维护；两份闭集收据自身另由 service 门禁
// 固定原文摘要。
func compatibilityCodeRetirementTransitionSupersedes(path, priorDigest, currentDigest string) bool {
	if claudeOfficialClientOnlyTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if retirementPrior, ok := claudeFWHLegacyRetirementTransitionPrior(path, currentDigest); ok &&
		upstreamV0177SourceTransitionSupersedes(path, priorDigest, retirementPrior) {
		return true
	}
	if claudeFWGTestTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if claudeFWGSourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	if upstreamV0177SourceTransitionSupersedes(path, priorDigest, currentDigest) {
		return true
	}
	raw, err := os.ReadFile("../../../docs/egress/maintenance/compatibility-code-retirement-closure.json")
	if err != nil {
		return false
	}
	var closure struct {
		SchemaVersion     string `json:"schema_version"`
		SourceTransitions []struct {
			Path       string `json:"path"`
			FromSHA256 string `json:"from_sha256"`
			ToSHA256   string `json:"to_sha256"`
		} `json:"source_transitions"`
	}
	if err := json.Unmarshal(raw, &closure); err != nil ||
		closure.SchemaVersion != "official-egress.compatibility-code-retirement-closure/v1" {
		return false
	}
	for _, transition := range closure.SourceTransitions {
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			transition.ToSHA256 == currentDigest {
			return true
		}
		if transition.Path == path && transition.FromSHA256 == priorDigest &&
			(versionLeakDebtTransitionSupersedesOfficialEgress(path, transition.ToSHA256, currentDigest) ||
				upstreamV0177SourceTransitionSupersedes(path, transition.ToSHA256, currentDigest)) {
			return true
		}
	}
	return versionLeakDebtTransitionSupersedesOfficialEgress(path, priorDigest, currentDigest) ||
		upstreamV0177SourceTransitionSupersedes(path, priorDigest, currentDigest)
}

func versionLeakDebtTransitionSupersedesOfficialEgress(path, priorDigest, currentDigest string) bool {
	raw, err := os.ReadFile("../../../docs/egress/maintenance/version-leak-debt-closure.json")
	if err != nil || sha256Hex(raw) != "deaaf580764c1cab5313b377b82f74ad48b820b113da7b565bb6c6a2c5eb0205" {
		return false
	}
	var receipt struct {
		SchemaVersion     string `json:"schema_version"`
		SourceTransitions []struct {
			Path       string `json:"path"`
			FromSHA256 string `json:"from_sha256"`
			ToSHA256   string `json:"to_sha256"`
		} `json:"source_transitions"`
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

func TestCatalogRetirementValidationRejectsLiveTarget(t *testing.T) {
	inputs, evidenceBySink := catalogRetirementHistoricalInputs(t)
	manifest, err := loadCatalogRetirementManifest()
	if err != nil {
		t.Fatal(err)
	}
	for index := range inputs {
		if inputs[index].ID == "dead.images.download_blob" {
			inputs[index].RuntimeBindable = true
		}
	}
	if _, err := validateCatalogRetirements(manifest, evidenceBySink, inputs); err == nil {
		t.Fatal("可达 Sink 被 dead-code RemovalReceipt 静默删除")
	}
}

func TestImagesRetiredSourceSymbolsAreExtinct(t *testing.T) {
	source, err := os.ReadFile("../service/openai_images.go")
	if err != nil {
		t.Fatal(err)
	}
	classifier, err := os.ReadFile("../../cmd/egressscan/classify.go")
	if err != nil {
		t.Fatal(err)
	}
	for _, symbol := range []string{
		"openAIChatGPTStartURL",
		"openAIChatGPTFilesURL",
		"openAIImageBackendUserAgent",
		"openAIImageMaxDownloadBytes",
		"openAIImagePointerInfo",
		"collectOpenAIImagePointers",
		"openAIImagePointerMatches",
		"mergeOpenAIImagePointerInfos",
		"mergeOpenAIImagePointerInfo",
		"identityKey",
		"resolveOpenAIImageBytes",
		"normalizeOpenAIImageBase64",
		"collectOpenAIImageInlineAssets",
		"walkOpenAIImageInlineAssets",
		"isLikelyOpenAIImageDownloadURL",
		"fetchOpenAIImageDownloadURL",
		"downloadOpenAIImageBytes",
		"isOpenAIImageTransientConversationNotFoundError",
		"cloneHTTPHeader",
		"headerToMap",
	} {
		if bytes.Contains(source, []byte(symbol)) {
			t.Fatalf("已退休 Images 兼容符号重新进入生产源码: %s", symbol)
		}
		if bytes.Contains(classifier, []byte(symbol)) {
			t.Fatalf("已退休 Images 兼容符号重新进入 scanner 当前分类: %s", symbol)
		}
	}
	if !bytes.Contains(source, []byte("func firstNonEmptyString")) ||
		!bytes.Contains(source, []byte("func newOpenAIImageStatusError")) {
		t.Fatal("Images 退休误删仍有生产消费者的共享产品语义")
	}
}

func catalogRetirementHistoricalInputs(
	t *testing.T,
) ([]SinkBindingInput, map[string]bindingcontract.ReleaseBindingDoc) {
	t.Helper()
	doc, err := bindingcontract.ParseBindingCatalog(embeddedReleaseBindings)
	if err != nil {
		t.Fatal(err)
	}
	evidence, err := bindingcontract.NewBindingCatalog(doc)
	if err != nil {
		t.Fatal(err)
	}
	inputs := make([]SinkBindingInput, 0, len(evidence.Bindings()))
	for _, item := range evidence.Bindings() {
		input, convertErr := sinkBindingInputFromEvidence(item)
		if convertErr != nil {
			t.Fatal(convertErr)
		}
		inputs = append(inputs, input)
	}
	inputs, err = applyCatalogAmendments(evidence, inputs)
	if err != nil {
		t.Fatal(err)
	}
	inputs, evidenceBySink, err := applyPreBootstrapSupplements(evidence, inputs)
	if err != nil {
		t.Fatal(err)
	}
	return inputs, evidenceBySink
}

func decodeCatalogRetirementCandidate(raw []byte, target *catalogRetirementCandidate) error {
	// 完整 RemovalReceipt 结构由 egressscan 严格解码；Runtime Catalog 只读取并
	// 交叉验证与冻结 ReleaseBinding 有关的最小字段。
	return json.Unmarshal(raw, target)
}
