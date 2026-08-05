package main

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

const changeset3RemovalReceiptRebuildOutputEnv = "CHANGESET3_REMOVAL_RECEIPT_REBUILD_OUTPUT"

const maintenanceRetirementReceiptSHA256 = "d60fb470a83f4a98f5de231265d2f695f3963536ec45290b36341c248a56ee36"

type changeset3FacadeAudit struct {
	SchemaVersion string `json:"schema_version"`
	ReviewedBy    string `json:"reviewed_by"`
	ReviewRef     string `json:"review_ref"`
	Facades       []struct {
		SinkID              string   `json:"sink_id"`
		CurrentCandidateIDs []string `json:"current_candidate_ids"`
		Disposition         string   `json:"disposition"`
		RemovalReceipt      bool     `json:"removal_receipt"`
		Rationale           string   `json:"rationale"`
	} `json:"facades"`
	ProductionAdapters []struct {
		AdapterID   string `json:"adapter_id"`
		CandidateID string `json:"candidate_id"`
	} `json:"production_adapters"`
}

type changeset5AdapterSourceTransition struct {
	SchemaVersion              string `json:"schema_version"`
	Changeset                  string `json:"changeset"`
	ReferenceFacadeAudit       string `json:"reference_facade_audit"`
	ReferenceFacadeAuditSHA256 string `json:"reference_facade_audit_sha256"`
	Transitions                []struct {
		AdapterID                      string `json:"adapter_id"`
		FromCandidateID                string `json:"from_candidate_id"`
		ToCandidateID                  string `json:"to_candidate_id"`
		FinalClassification            string `json:"final_classification"`
		RemovalReceipt                 string `json:"removal_receipt,omitempty"`
		RemovalReceiptSHA256           string `json:"removal_receipt_sha256,omitempty"`
		EnforcedMigrationReceiptDigest string `json:"enforced_migration_receipt_digest,omitempty"`
		Reason                         string `json:"reason"`
	} `json:"transitions"`
}

// TestGenerateChangeset3RemovalReceiptDigestReferences 只替换因 enforced
// MigrationReceipt 重建而变化的引用摘要，其他冻结字段保持原值。
func TestGenerateChangeset3RemovalReceiptDigestReferences(t *testing.T) {
	output := strings.TrimSpace(os.Getenv(changeset3RemovalReceiptRebuildOutputEnv))
	if output == "" {
		t.Skip("仅在显式指定临时输出文件时重建 RemovalReceipt 引用")
	}
	if !filepath.IsAbs(output) {
		t.Fatal("RemovalReceipt 重建输出必须是绝对路径")
	}
	migrations, err := loadMigrationReceiptIndex(
		"../../../docs/egress/lifecycle/migration-receipts.json,../../../docs/egress/migration/migration-receipts.json",
	)
	if err != nil {
		t.Fatal(err)
	}
	enforcedBySink := make(map[string]string)
	for digest, document := range migrations {
		if document.ApprovedState == "enforced" {
			enforcedBySink[document.SinkID] = digest
		}
	}
	raw, err := os.ReadFile("../../../docs/egress/migration/removal-receipts.json")
	if err != nil {
		t.Fatal(err)
	}
	var manifest removalManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		t.Fatal(err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		t.Fatal("RemovalReceipt 原文尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 3 || manifest.BootstrapCommit != legacyBootstrapCommit ||
		len(manifest.Receipts) != 15 {
		t.Fatalf("RemovalReceipt 冻结范围非法：%+v", manifest)
	}
	for index := range manifest.Receipts {
		receipt := &manifest.Receipts[index]
		digest := enforcedBySink[receipt.ReplacementSinkID]
		if digest == "" {
			t.Fatalf("缺少 replacement Sink 的 enforced 收据：%s", receipt.ReplacementSinkID)
		}
		receipt.MigrationReceiptDigest = digest
	}
	updated, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	updated = append(updated, '\n')
	if err := os.WriteFile(output, updated, 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestChangeset3RemovalReceiptsUpgradeOnlyDisappearedDelegations(t *testing.T) {
	migrations, err := loadMigrationReceiptIndex(
		"../../../docs/egress/lifecycle/migration-receipts.json,../../../docs/egress/migration/migration-receipts.json",
	)
	if err != nil {
		t.Fatal(err)
	}
	changeset3, err := loadRemovalManifest("../../../docs/egress/migration/removal-receipts.json", migrations)
	if err != nil {
		t.Fatal(err)
	}
	if len(changeset3.Receipts) != 15 {
		t.Fatalf("变更集 3 RemovalReceipt 数量=%d，期望 15", len(changeset3.Receipts))
	}
	current, err := scanWithCodexRouteEvidence(migrations.codexRoutes())
	if err != nil {
		t.Fatal(err)
	}
	currentCandidates := make(map[string]bool, len(current.Sinks))
	for _, candidate := range current.Sinks {
		currentCandidates[candidate.ScanCandidateID] = true
	}
	for _, receipt := range changeset3.Receipts {
		if receipt.Kind != "migrated" || currentCandidates[receipt.Candidate.ScanCandidateID] {
			t.Fatalf("RemovalReceipt 不是已真实消失的 migrated 候选: %s", receipt.Candidate.ScanCandidateID)
		}
		migration := migrations[receipt.MigrationReceiptDigest]
		if migration.SinkID != receipt.ReplacementSinkID || migration.ApprovedState != "enforced" {
			t.Fatalf("RemovalReceipt 没有连接 enforced MigrationReceipt: %s", receipt.Candidate.ScanCandidateID)
		}
	}
	combined, err := loadRemovalManifest(
		"../../../docs/egress/release/removal-receipts.json,../../../docs/egress/migration/removal-receipts.json",
		migrations,
	)
	if err != nil {
		t.Fatal(err)
	}
	combinedByID := combined.byID()
	for _, receipt := range changeset3.Receipts {
		if combinedByID[receipt.Candidate.ScanCandidateID].Kind != "migrated" {
			t.Fatalf("后续 migrated 收据未单调替代 legacy_delegated: %s", receipt.Candidate.ScanCandidateID)
		}
	}
}

func TestChangeset3FacadeAuditMatchesCurrentSourceAndAdapters(t *testing.T) {
	raw, err := os.ReadFile("../../../docs/egress/migration/facade-audit.json")
	if err != nil {
		t.Fatal(err)
	}
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	var audit changeset3FacadeAudit
	if err := decoder.Decode(&audit); err != nil {
		t.Fatal(err)
	}
	if audit.SchemaVersion != "changeset3-facade-audit/v1" || len(audit.Facades) != 3 ||
		len(audit.ProductionAdapters) != 3 {
		t.Fatalf("facade 审计元数据不完整: %+v", audit)
	}
	auditSum := sha256.Sum256(raw)
	if hex.EncodeToString(auditSum[:]) != "cc2eec638bee28f95f35e8f0e8629c2f2d1f43e64a8447a9eae942987726bbd4" {
		t.Fatal("变更集 3 facade audit 历史原文漂移")
	}
	retiredFacadeCandidate, retiredFacadeSinkID := loadMaintenanceFacadeRetirement(
		t, hex.EncodeToString(auditSum[:]),
	)
	transitionRaw, err := os.ReadFile("../../../docs/egress/consolidation/adapter-source-transition.json")
	if err != nil {
		t.Fatal(err)
	}
	transitionSum := sha256.Sum256(transitionRaw)
	if hex.EncodeToString(transitionSum[:]) != "038ae2e40bc42bf4b497bb406795a1ed4019215bf45332ffdb597f62b2176e2e" {
		t.Fatal("变更集 5 adapter source transition 摘要漂移")
	}
	var transition changeset5AdapterSourceTransition
	transitionDecoder := json.NewDecoder(bytes.NewReader(transitionRaw))
	transitionDecoder.DisallowUnknownFields()
	if err := transitionDecoder.Decode(&transition); err != nil {
		t.Fatal(err)
	}
	if transition.SchemaVersion != "changeset5-adapter-source-transition/v1" ||
		transition.ReferenceFacadeAuditSHA256 != hex.EncodeToString(auditSum[:]) ||
		len(transition.Transitions) != 2 {
		t.Fatalf("变更集 5 adapter source transition 元数据非法：%+v", transition)
	}
	migrations, err := loadMigrationReceiptIndex(
		"../../../docs/egress/lifecycle/migration-receipts.json,../../../docs/egress/migration/migration-receipts.json",
	)
	if err != nil {
		t.Fatal(err)
	}
	current, err := scanWithCodexRouteEvidence(migrations.codexRoutes())
	if err != nil {
		t.Fatal(err)
	}
	currentByID := make(map[string]SinkRecord, len(current.Sinks))
	for _, candidate := range current.Sinks {
		currentByID[candidate.ScanCandidateID] = candidate
	}
	var auditedFacadeIDs []string
	retiredFacadeSeen := false
	for _, facade := range audit.Facades {
		auditedFacadeIDs = append(auditedFacadeIDs, facade.SinkID)
		if facade.Disposition != "retained_transport_infrastructure" || facade.RemovalReceipt ||
			len(facade.CurrentCandidateIDs) == 0 || facade.Rationale == "" {
			t.Fatalf("facade 处置非法: %+v", facade)
		}
		for _, candidateID := range facade.CurrentCandidateIDs {
			candidate, exists := currentByID[candidateID]
			if candidateID == retiredFacadeCandidate {
				if exists || facade.SinkID != retiredFacadeSinkID {
					t.Fatalf("已退休 facade candidate 重新出现或 SinkID 漂移：%s", candidateID)
				}
				retiredFacadeSeen = true
				continue
			}
			if !exists || candidate.RuntimeSinkID != facade.SinkID || !candidate.IsFacade {
				t.Fatalf("facade 审计引用的候选不存在或身份不符: %s", candidateID)
			}
		}
	}
	if !retiredFacadeSeen {
		t.Fatal("维护退休收据没有精确承接历史 facade candidate")
	}
	sort.Strings(auditedFacadeIDs)
	wantFacades := []string{"codex.facade.oauth_client", "codex.facade.upstream", "codex.facade.ws_pool"}
	if !equalStringSlices(auditedFacadeIDs, wantFacades) {
		t.Fatalf("facade 审计范围不完整: %v", auditedFacadeIDs)
	}
	transitionByAdapter := make(map[string]struct{ from, to string }, len(transition.Transitions))
	for _, item := range transition.Transitions {
		transitionByAdapter[item.AdapterID] = struct{ from, to string }{item.FromCandidateID, item.ToCandidateID}
		if _, exists := currentByID[item.FromCandidateID]; exists {
			t.Fatalf("变更集 5 迁移前 adapter candidate 仍存在：%s", item.FromCandidateID)
		}
	}
	for _, adapter := range audit.ProductionAdapters {
		candidateID := adapter.CandidateID
		if item, ok := transitionByAdapter[adapter.AdapterID]; ok {
			if item.from != candidateID {
				t.Fatalf("adapter source transition 未精确承接历史 candidate：%s", adapter.AdapterID)
			}
			candidateID = item.to
		}
		candidate, exists := currentByID[candidateID]
		if !exists || candidate.Persona != "infrastructure" || candidate.EnforcementState != "not_applicable" {
			t.Fatalf("生产 adapter 未进入 source gate infrastructure 闭集: %+v", adapter)
		}
	}
	wsTransition := transitionByAdapter["officialegress.websocket_round_tripper"]
	wsCandidate, exists := currentByID[wsTransition.to]
	if !exists || wsCandidate.Persona != "infrastructure" ||
		wsCandidate.EnforcementState != "not_applicable" || wsCandidate.RuntimeSinkID != "" {
		t.Fatal("变更集 5 WebSocket RoundTripper 新位置未进入物理基础设施闭集")
	}
}

func loadMaintenanceFacadeRetirement(t *testing.T, referenceAuditSHA256 string) (string, string) {
	t.Helper()
	raw, err := os.ReadFile("../../../docs/egress/maintenance/official-egress-consolidation-retirement.json")
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != maintenanceRetirementReceiptSHA256 {
		t.Fatal("维护退休收据摘要漂移")
	}
	var receipt struct {
		SchemaVersion          string `json:"schema_version"`
		RetiredFacadeCandidate struct {
			ReferenceAuditSHA256 string `json:"reference_audit_sha256"`
			CandidateID          string `json:"candidate_id"`
			SinkID               string `json:"sink_id"`
			Replacement          string `json:"replacement"`
			Reason               string `json:"reason"`
		} `json:"retired_facade_candidate"`
	}
	if err := json.Unmarshal(raw, &receipt); err != nil {
		t.Fatal(err)
	}
	retired := receipt.RetiredFacadeCandidate
	if receipt.SchemaVersion != "official-egress-consolidation-retirement/v1" ||
		retired.ReferenceAuditSHA256 != referenceAuditSHA256 ||
		strings.TrimSpace(retired.CandidateID) == "" || strings.TrimSpace(retired.SinkID) == "" ||
		strings.TrimSpace(retired.Replacement) == "" || strings.TrimSpace(retired.Reason) == "" {
		t.Fatalf("维护 facade 退休事实非法：%+v", retired)
	}
	return retired.CandidateID, retired.SinkID
}
