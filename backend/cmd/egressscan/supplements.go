package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
)

const legacyBootstrapTree = "a8c3dee18a01a6138bfcea60860bb5ad11548c3a"

// reviewedSupplement 冻结一次 pre-bootstrap 遗漏的完整历史事实和分类结论。
// 后续扫描器与分类规则只能验证这份收据，不能重新推导并静默改写它。
type reviewedSupplement struct {
	Candidate        SinkRecord `json:"candidate"`
	SourceBlobSHA256 string     `json:"source_blob_sha256"`
	ReviewedBy       string     `json:"reviewed_by"`
	ReviewRef        string     `json:"review_ref"`
	Rationale        string     `json:"rationale"`
}

type supplementManifest struct {
	SchemaVersion   int                  `json:"schema_version"`
	BootstrapCommit string               `json:"bootstrap_commit"`
	BootstrapTree   string               `json:"bootstrap_tree"`
	Lifecycle       string               `json:"lifecycle"`
	Supplements     []reviewedSupplement `json:"supplements"`
}

func loadSupplementManifest(path string) (supplementManifest, error) {
	if strings.TrimSpace(path) == "" {
		return supplementManifest{}, errors.New("必须提供 -supplements")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return supplementManifest{}, err
	}
	var manifest supplementManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return supplementManifest{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return supplementManifest{}, errors.New("补录清单尾部存在额外 JSON")
		}
		return supplementManifest{}, fmt.Errorf("补录清单尾部非法: %w", err)
	}
	if manifest.SchemaVersion != 2 || manifest.BootstrapCommit != legacyBootstrapCommit ||
		manifest.BootstrapTree != legacyBootstrapTree {
		return supplementManifest{}, errors.New("补录清单 schema_version、bootstrap_commit 或 bootstrap_tree 非法")
	}
	if manifest.Lifecycle != "provisional" && manifest.Lifecycle != "sealed" {
		return supplementManifest{}, fmt.Errorf("补录清单 lifecycle 非法: %q", manifest.Lifecycle)
	}
	previousID := ""
	for _, item := range manifest.Supplements {
		if err := validateReviewedSupplement(item); err != nil {
			return supplementManifest{}, err
		}
		if previousID >= item.Candidate.ScanCandidateID {
			return supplementManifest{}, errors.New("补录项必须按 scan_candidate_id 严格排序且不得重复")
		}
		previousID = item.Candidate.ScanCandidateID
	}
	return manifest, nil
}

func validateReviewedSupplement(item reviewedSupplement) error {
	candidate := item.Candidate
	if strings.TrimSpace(candidate.ScanCandidateID) == "" || strings.TrimSpace(item.ReviewedBy) == "" ||
		strings.TrimSpace(item.ReviewRef) == "" || strings.TrimSpace(item.Rationale) == "" {
		return errors.New("补录项必须包含完整候选、审核人、审核引用和理由")
	}
	if !validDigest(item.SourceBlobSHA256) {
		return fmt.Errorf("补录项 %s 的历史源码 blob 摘要非法", candidate.ScanCandidateID)
	}
	if candidate.RuntimeSinkID == "" || candidate.IsFacade || candidate.EnforcementState != "legacy_observe" ||
		candidate.Persona == "out-of-scope" || candidate.Persona == "infrastructure" ||
		candidate.Persona == "dead-code" || len(candidate.Routes) == 0 {
		return fmt.Errorf("补录项 %s 必须是可运行时绑定的 legacy sink", candidate.ScanCandidateID)
	}
	// 复用 ReleaseBinding 的通用证据校验，证明 persona、route、backend、owner、
	// endpoint evidence 等冻结字段能够确定性生成生产 Catalog。
	baseline := bindingcontract.SinkBaselineDoc{
		BootstrapCommit: legacyBootstrapCommit,
		BuildContexts:   append([]string(nil), candidate.BuildContexts...),
		PackagesLoaded:  1,
		ScanPattern:     scanPattern,
		Sinks:           []bindingcontract.SinkBaselineCandidateDoc{toBindingCandidate(candidate)},
	}
	raw, err := json.Marshal(baseline)
	if err != nil {
		return err
	}
	if _, err := bindingcontract.BuildBindingCatalogDoc(raw); err != nil {
		return fmt.Errorf("补录项 %s 无法生成 ReleaseBinding: %w", candidate.ScanCandidateID, err)
	}
	return nil
}

func toBindingCandidate(candidate SinkRecord) bindingcontract.SinkBaselineCandidateDoc {
	return bindingcontract.SinkBaselineCandidateDoc{
		ScanCandidateID: candidate.ScanCandidateID, File: candidate.File, Func: candidate.Func,
		Package: candidate.Package, Callee: candidate.Callee, Receiver: candidate.Receiver,
		SinkKind: candidate.SinkKind, Protocol: candidate.Protocol, SinkType: candidate.SinkType,
		ASTFingerprint: candidate.ASTFingerprint, TargetResolution: string(candidate.Resolution),
		BuildContexts:   append([]string(nil), candidate.BuildContexts...),
		ResolvedHosts:   append([]string(nil), candidate.ResolvedHosts...),
		ResolvedMethods: append([]string(nil), candidate.ResolvedMethods...),
		ResolvedPaths:   append([]string(nil), candidate.ResolvedPaths...),
		ResolvedTargets: append([]string(nil), candidate.ResolvedTargets...),
		OfficialHost:    candidate.OfficialHost, RuntimeSinkID: candidate.RuntimeSinkID,
		Purpose: candidate.Purpose, Persona: candidate.Persona,
		EndpointEvidence: candidate.EndpointEvidence, Routes: append([]string(nil), candidate.Routes...),
		IsFacade: candidate.IsFacade, Backend: candidate.Backend, TargetBackend: candidate.TargetBackend,
		EnforcementState: candidate.EnforcementState, Owner: candidate.Owner,
		MigrationSet: candidate.MigrationChangeset, ExpiryCondition: candidate.ExpiryCondition,
		Rationale: candidate.Rationale, LineHint: candidate.Line,
	}
}

func validDigest(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func (m supplementManifest) byID() map[string]reviewedSupplement {
	items := make(map[string]reviewedSupplement, len(m.Supplements))
	for _, item := range m.Supplements {
		items[item.Candidate.ScanCandidateID] = item
	}
	return items
}

func compareSinkRecords(previous, current SinkRecord) []string {
	previous.Line = 0
	current.Line = 0
	previousJSON, _ := json.Marshal(previous)
	currentJSON, _ := json.Marshal(current)
	if bytes.Equal(previousJSON, currentJSON) {
		return nil
	}
	return []string{fmt.Sprintf("%s  候选语义发生变化", previous.ScanCandidateID)}
}

// compareSupplementStructure 只比较扫描器能从历史源码观察到的结构事实。persona、
// route、owner 等审核结论以收据为准，不能由未来的 classify 规则重新解释。
func compareSupplementStructure(frozen, scanned SinkRecord) bool {
	clearClassification := func(record *SinkRecord) {
		record.RuntimeSinkID = ""
		record.Purpose = ""
		record.Persona = ""
		record.EndpointEvidence = ""
		record.Routes = nil
		record.IsFacade = false
		record.Backend = ""
		record.TargetBackend = ""
		record.EnforcementState = ""
		record.Owner = ""
		record.MigrationChangeset = ""
		record.ExpiryCondition = ""
		record.Rationale = ""
		record.Line = 0
	}
	clearClassification(&frozen)
	clearClassification(&scanned)
	// 扫描器对“没有目标事实”可能生成 nil 或空 slice；两者 JSON 语义相同，不能
	// 因内存表示差异误判受审结构漂移。实际字段仍全部进入规范 JSON 比较。
	frozenJSON, _ := json.Marshal(frozen)
	scannedJSON, _ := json.Marshal(scanned)
	return bytes.Equal(frozenJSON, scannedJSON)
}

func verifySupplementSourceBlob(item reviewedSupplement) error {
	path := filepath.FromSlash(item.Candidate.File)
	if strings.HasPrefix(path, "backend"+string(filepath.Separator)) {
		path = strings.TrimPrefix(path, "backend"+string(filepath.Separator))
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != item.SourceBlobSHA256 {
		return errors.New("历史源码 blob 摘要不匹配")
	}
	return nil
}

func runBootstrapReplay(
	baselinePath, supplementsPath, removalsPath, migrationReceiptsPath,
	catalogAmendmentsPath, inventoryLockPath, scannerSourceRoot string,
) int {
	if strings.TrimSpace(baselinePath) == "" {
		fmt.Fprintln(os.Stderr, "replay 模式需要 -baseline")
		return 2
	}
	raw, err := os.ReadFile(baselinePath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取 bootstrap 基线失败：%v\n", err)
		return 1
	}
	baseline, err := decodeBaseline(raw)
	if err != nil {
		fmt.Fprintf(os.Stderr, "解析 bootstrap 基线失败：%v\n", err)
		return 1
	}
	if err := verifyBootstrapInventoryLock(inventoryLockPath, raw, scannerSourceRoot); err != nil {
		fmt.Fprintf(os.Stderr, "验证 bootstrap inventory lock 失败：%v\n", err)
		return 1
	}
	supplements, err := loadSupplementManifest(supplementsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取受审补录清单失败：%v\n", err)
		return 1
	}
	migrations, err := loadMigrationReceiptIndex(migrationReceiptsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取 MigrationReceipt 清单失败：%v\n", err)
		return 1
	}
	removals, err := loadRemovalManifest(removalsPath, migrations)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取受审移除收据失败：%v\n", err)
		return 1
	}
	amendments, err := loadScannerCatalogAmendments(catalogAmendmentsPath)
	if err != nil {
		fmt.Fprintf(os.Stderr, "读取 Catalog amendment 清单失败：%v\n", err)
		return 1
	}
	removalByID := removals.byID()
	historicalClassifications := make(map[string]SinkRecord, len(removalByID))
	for id, receipt := range removalByID {
		historicalClassifications[id] = receipt.Candidate
	}
	replayed, err := scanWithReviewedHistory(nil, historicalClassifications)
	if err != nil {
		fmt.Fprintf(os.Stderr, "bootstrap 源码回放失败：%v\n", err)
		return 1
	}

	expected := make(map[string]SinkRecord, len(baseline.Sinks)+len(supplements.Supplements))
	for _, sink := range baseline.Sinks {
		expected[sink.ScanCandidateID] = sink
	}
	supplementByID := supplements.byID()
	amendmentByCandidateID := amendments.byCandidateID()
	actual := make(map[string]SinkRecord, len(replayed.Sinks))
	for _, sink := range replayed.Sinks {
		actual[sink.ScanCandidateID] = sink
	}

	var problems []string
	for id, item := range supplementByID {
		if _, exists := expected[id]; exists {
			problems = append(problems, fmt.Sprintf("%s 已在 bootstrap 基线中，不得重复补录", id))
			continue
		}
		sink, exists := actual[id]
		if !exists {
			problems = append(problems, fmt.Sprintf("%s 无法证明存在于 bootstrap commit", id))
			continue
		}
		if !compareSupplementStructure(item.Candidate, sink) {
			problems = append(problems, fmt.Sprintf("%s 的冻结结构证据与 bootstrap 源码不匹配", id))
			continue
		}
		if err := verifySupplementSourceBlob(item); err != nil {
			problems = append(problems, fmt.Sprintf("%s 的历史源码证据非法：%v", id, err))
			continue
		}
		expected[id] = item.Candidate
	}
	for id, want := range expected {
		got, exists := actual[id]
		if !exists {
			problems = append(problems, fmt.Sprintf("%s 在 bootstrap 回放中消失", id))
			continue
		}
		if receipt, removed := removalByID[id]; removed {
			// 当前 scanner 可以随已退休实现删除旧分类规则，但不能失去对历史
			// 调用表达式的结构识别。分类、路由和责任字段由 RemovalReceipt 冻结；
			// 文件、函数、callee、AST 指纹和目标解析等结构事实仍从 bootstrap
			// 源码重新扫描并逐字段比较。
			if !compareSupplementStructure(receipt.Candidate, got) {
				problems = append(problems, fmt.Sprintf("%s 的退休候选结构证据发生变化", id))
			}
		} else if item, supplemented := supplementByID[id]; supplemented {
			if !compareSupplementStructure(item.Candidate, got) {
				problems = append(problems, fmt.Sprintf("%s 的补录结构证据发生变化", id))
			}
		} else if amendment, amended := amendmentByCandidateID[id]; amended &&
			amendment.Kind == scannerAmendmentRuntimeScopeExclusion {
			if err := validateScopeCorrectedCandidate(amendment, want, got); err != nil {
				problems = append(problems, fmt.Sprintf("%s 的 scope correction 非法：%v", id, err))
			}
		} else {
			problems = append(problems, compareSinkRecords(want, got)...)
		}
	}
	for id := range actual {
		if _, exists := expected[id]; !exists {
			problems = append(problems, fmt.Sprintf("%s 是未受审的 pre-bootstrap 遗漏", id))
		}
	}
	for id, receipt := range removals.byID() {
		frozen, exists := expected[id]
		if !exists {
			problems = append(problems, fmt.Sprintf("%s 的移除收据引用未知历史 inventory", id))
			continue
		}
		if !sameFrozenCandidate(receipt.Candidate, frozen) {
			problems = append(problems, fmt.Sprintf("%s 的移除收据未冻结原始历史候选", id))
			continue
		}
		item := reviewedSupplement{Candidate: receipt.Candidate, SourceBlobSHA256: receipt.SourceBlobSHA256}
		if err := verifySupplementSourceBlob(item); err != nil {
			problems = append(problems, fmt.Sprintf("%s 的移除源码证据非法：%v", id, err))
		}
	}
	for candidateID, amendment := range amendmentByCandidateID {
		frozen, exists := expected[candidateID]
		if !exists {
			problems = append(problems, fmt.Sprintf("%s 的 Catalog amendment 引用未知历史 inventory", candidateID))
			continue
		}
		if err := verifyAmendmentSourceBlob(amendment, frozen); err != nil {
			problems = append(problems, fmt.Sprintf("%s 的 Catalog amendment 历史源码证据非法：%v", candidateID, err))
		}
	}
	sort.Strings(problems)
	if len(problems) != 0 {
		fmt.Fprintln(os.Stderr, "❌ bootstrap 回放未通过：")
		for _, problem := range problems {
			fmt.Fprintf(os.Stderr, "  - %s\n", problem)
		}
		return 1
	}
	fmt.Printf("✅ bootstrap 回放通过：基线=%d，受审补录=%d，受审移除=%d\n",
		len(baseline.Sinks), len(supplements.Supplements), len(removals.Receipts))
	return 0
}
