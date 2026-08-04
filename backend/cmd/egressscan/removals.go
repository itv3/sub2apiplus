package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

type migrationReceiptIndex map[string]receiptcontract.Document

func loadMigrationReceiptIndex(path string) (migrationReceiptIndex, error) {
	if strings.TrimSpace(path) == "" {
		return nil, errors.New("必须提供 -migration-receipts")
	}
	index := make(migrationReceiptIndex)
	for _, manifestPath := range reviewedManifestPaths(path) {
		raw, err := os.ReadFile(manifestPath)
		if err != nil {
			return nil, err
		}
		manifest, err := receiptcontract.ParseManifest(raw)
		if err != nil {
			return nil, err
		}
		if manifest.BootstrapCommit != legacyBootstrapCommit {
			return nil, errors.New("MigrationReceipt bootstrap_commit 非法")
		}
		for _, document := range manifest.Receipts {
			digest, digestErr := receiptcontract.DigestDocument(document)
			if digestErr != nil {
				return nil, digestErr
			}
			if _, duplicate := index[digest]; duplicate {
				return nil, fmt.Errorf("MigrationReceipt 摘要重复: %s", digest)
			}
			index[digest] = document
		}
	}
	return index, nil
}

func (m migrationReceiptIndex) codexRoutes() []string {
	seen := make(map[string]bool)
	for _, document := range m {
		if document.AuthorityKind != receiptcontract.AuthorityCodexExecutor {
			continue
		}
		for _, proof := range document.Routes {
			route := strings.ToUpper(strings.TrimSpace(proof.Route.Method)) + " " +
				strings.ToLower(strings.TrimSpace(proof.Route.Host)) + strings.TrimSpace(proof.Route.Path)
			if proof.Route.Protocol == "websocket" {
				route += " (WebSocket)"
			}
			seen[route] = true
		}
	}
	routes := make([]string, 0, len(seen))
	for route := range seen {
		routes = append(routes, route)
	}
	sort.Strings(routes)
	return routes
}

func reviewedManifestPaths(value string) []string {
	var paths []string
	for _, item := range strings.Split(value, ",") {
		if path := strings.TrimSpace(item); path != "" {
			paths = append(paths, path)
		}
	}
	return paths
}

type removalReceipt struct {
	Candidate              SinkRecord `json:"candidate"`
	Kind                   string     `json:"kind"`
	ReplacementSinkID      string     `json:"replacement_sink_id,omitempty"`
	MigrationReceiptDigest string     `json:"migration_receipt_digest,omitempty"`
	DelegationCandidateID  string     `json:"delegation_candidate_id,omitempty"`
	SourceBlobSHA256       string     `json:"source_blob_sha256"`
	ReviewedBy             string     `json:"reviewed_by"`
	ReviewRef              string     `json:"review_ref"`
	Rationale              string     `json:"rationale"`
}

type removalManifest struct {
	SchemaVersion   int              `json:"schema_version"`
	BootstrapCommit string           `json:"bootstrap_commit"`
	Receipts        []removalReceipt `json:"receipts"`
}

func loadRemovalManifest(path string, migrations migrationReceiptIndex) (removalManifest, error) {
	if strings.TrimSpace(path) == "" {
		return removalManifest{}, errors.New("必须提供 -removals")
	}
	manifest := removalManifest{SchemaVersion: 3, BootstrapCommit: legacyBootstrapCommit}
	byCandidate := make(map[string]removalReceipt)
	for _, manifestPath := range reviewedManifestPaths(path) {
		raw, err := os.ReadFile(manifestPath)
		if err != nil {
			return removalManifest{}, err
		}
		var current removalManifest
		decoder := json.NewDecoder(bytes.NewReader(raw))
		decoder.DisallowUnknownFields()
		if err := decoder.Decode(&current); err != nil {
			return removalManifest{}, err
		}
		if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
			return removalManifest{}, errors.New("移除收据尾部存在额外 JSON")
		}
		if (current.SchemaVersion != 2 && current.SchemaVersion != 3) ||
			current.BootstrapCommit != legacyBootstrapCommit {
			return removalManifest{}, errors.New("移除收据 schema_version 或 bootstrap_commit 非法")
		}
		for _, receipt := range current.Receipts {
			if err := validateRemovalReceipt(receipt, migrations); err != nil {
				return removalManifest{}, err
			}
			candidateID := receipt.Candidate.ScanCandidateID
			if previous, duplicate := byCandidate[candidateID]; duplicate {
				if previous.Kind != "legacy_delegated" || receipt.Kind != "migrated" ||
					!sameFrozenCandidate(previous.Candidate, receipt.Candidate) {
					return removalManifest{}, fmt.Errorf("移除收据重复或非法状态提升: %s", candidateID)
				}
			}
			byCandidate[candidateID] = receipt
		}
	}
	for _, receipt := range byCandidate {
		manifest.Receipts = append(manifest.Receipts, receipt)
	}
	sort.Slice(manifest.Receipts, func(i, j int) bool {
		return manifest.Receipts[i].Candidate.ScanCandidateID <
			manifest.Receipts[j].Candidate.ScanCandidateID
	})
	previous := ""
	for _, receipt := range manifest.Receipts {
		if previous >= receipt.Candidate.ScanCandidateID {
			return removalManifest{}, errors.New("移除收据必须按 scan_candidate_id 严格排序且不得重复")
		}
		previous = receipt.Candidate.ScanCandidateID
	}
	return manifest, nil
}

func validateRemovalReceipt(receipt removalReceipt, migrations migrationReceiptIndex) error {
	candidate := receipt.Candidate
	if candidate.ScanCandidateID == "" || !validDigest(receipt.SourceBlobSHA256) ||
		strings.TrimSpace(receipt.ReviewedBy) == "" || strings.TrimSpace(receipt.ReviewRef) == "" ||
		strings.TrimSpace(receipt.Rationale) == "" {
		return fmt.Errorf("移除收据字段不完整: %s", candidate.ScanCandidateID)
	}
	switch receipt.Kind {
	case "dead_code_removed":
		if candidate.Persona != "dead-code" && candidate.EnforcementState != "pending_removal" {
			return fmt.Errorf("非 dead-code 候选不得使用 dead_code_removed: %s", candidate.ScanCandidateID)
		}
		if receipt.ReplacementSinkID != "" || receipt.MigrationReceiptDigest != "" || receipt.DelegationCandidateID != "" {
			return fmt.Errorf("dead-code 移除不得声明迁移目标: %s", candidate.ScanCandidateID)
		}
	case "migrated":
		if candidate.RuntimeSinkID == "" || strings.TrimSpace(receipt.ReplacementSinkID) == "" ||
			!validDigest(receipt.MigrationReceiptDigest) {
			return fmt.Errorf("迁移移除缺少 replacement SinkID 或 MigrationReceipt: %s", candidate.ScanCandidateID)
		}
		migration, exists := migrations[receipt.MigrationReceiptDigest]
		if !exists || migration.SinkID != receipt.ReplacementSinkID {
			return fmt.Errorf("迁移移除引用的 MigrationReceipt 不存在或 replacement Sink 不匹配: %s", candidate.ScanCandidateID)
		}
		covered := false
		for _, candidateID := range receiptcontract.CandidateIDs(migration) {
			covered = covered || candidateID == candidate.ScanCandidateID
		}
		if !covered {
			return fmt.Errorf("MigrationReceipt 未覆盖待移除 candidate: %s", candidate.ScanCandidateID)
		}
		if receipt.DelegationCandidateID != "" {
			return fmt.Errorf("migrated 移除不得声明 legacy delegation: %s", candidate.ScanCandidateID)
		}
	case "legacy_delegated":
		if candidate.RuntimeSinkID == "" || candidate.EnforcementState != "legacy_observe" ||
			receipt.ReplacementSinkID != candidate.RuntimeSinkID ||
			strings.TrimSpace(receipt.DelegationCandidateID) == "" || receipt.MigrationReceiptDigest != "" {
			return fmt.Errorf("legacy delegation 字段或冻结状态非法: %s", candidate.ScanCandidateID)
		}
	default:
		return fmt.Errorf("移除收据 kind 非法: %s", receipt.Kind)
	}
	return nil
}

func (m removalManifest) delegationByTargetID() map[string][]removalReceipt {
	items := make(map[string][]removalReceipt)
	for _, item := range m.Receipts {
		if item.Kind == "legacy_delegated" {
			items[item.DelegationCandidateID] = append(items[item.DelegationCandidateID], item)
		}
	}
	return items
}

func validateLegacyDelegationTarget(receipt removalReceipt, current SinkRecord) error {
	frozen := receipt.Candidate
	if current.ScanCandidateID != receipt.DelegationCandidateID ||
		current.RuntimeSinkID != frozen.RuntimeSinkID || current.Purpose != frozen.Purpose ||
		current.Persona != frozen.Persona || current.EndpointEvidence != frozen.EndpointEvidence ||
		current.Backend != frozen.Backend || current.TargetBackend != frozen.TargetBackend ||
		current.EnforcementState != "legacy_observe" || current.IsFacade != frozen.IsFacade ||
		!equalStringSlices(current.Routes, frozen.Routes) {
		return fmt.Errorf("delegation 未保持 RuntimeSinkID/purpose/persona/route/backend/enforcement")
	}
	if current.SinkKind != "facade_legacy_compiled_http" &&
		current.SinkKind != "facade_legacy_compiled_ws" &&
		current.SinkKind != "facade_legacy_compiled_req_profile" {
		return fmt.Errorf("delegation target 未进入受审 LegacyCompiledDispatcher")
	}
	return nil
}

func equalStringSlices(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func (m removalManifest) byID() map[string]removalReceipt {
	items := make(map[string]removalReceipt, len(m.Receipts))
	for _, item := range m.Receipts {
		items[item.Candidate.ScanCandidateID] = item
	}
	return items
}

func sameFrozenCandidate(left, right SinkRecord) bool {
	left.Line = 0
	right.Line = 0
	leftJSON, _ := json.Marshal(left)
	rightJSON, _ := json.Marshal(right)
	return bytes.Equal(leftJSON, rightJSON)
}
