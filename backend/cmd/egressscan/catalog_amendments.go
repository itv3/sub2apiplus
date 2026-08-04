package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
)

const (
	scannerAmendmentRouteEvidence         = "route_evidence"
	scannerAmendmentRuntimeScopeExclusion = "runtime_scope_exclusion"
)

type scannerCatalogAmendmentManifest struct {
	SchemaVersion   int                       `json:"schema_version"`
	BootstrapCommit string                    `json:"bootstrap_commit"`
	Amendments      []scannerCatalogAmendment `json:"amendments"`
}

type scannerCatalogAmendment struct {
	Kind       string                         `json:"kind"`
	SinkID     string                         `json:"sink_id"`
	Source     scannerCatalogAmendmentSource  `json:"source"`
	Routes     []scannerCatalogAmendmentRoute `json:"routes"`
	ReviewedBy string                         `json:"reviewed_by"`
	ReviewRef  string                         `json:"review_ref"`
	Rationale  string                         `json:"rationale"`
}

type scannerCatalogAmendmentSource struct {
	ScanCandidateID  string `json:"scan_candidate_id"`
	ASTFingerprint   string `json:"ast_fingerprint"`
	File             string `json:"file"`
	Function         string `json:"function"`
	Callee           string `json:"callee"`
	SourceBlobSHA256 string `json:"source_blob_sha256"`
}

type scannerCatalogAmendmentRoute struct {
	Method   string `json:"method"`
	Host     string `json:"host"`
	Path     string `json:"path"`
	Protocol string `json:"protocol"`
}

func loadScannerCatalogAmendments(path string) (scannerCatalogAmendmentManifest, error) {
	if strings.TrimSpace(path) == "" {
		return scannerCatalogAmendmentManifest{}, errors.New("必须提供 -catalog-amendments")
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return scannerCatalogAmendmentManifest{}, err
	}
	var manifest scannerCatalogAmendmentManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return scannerCatalogAmendmentManifest{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return scannerCatalogAmendmentManifest{}, errors.New("Catalog amendment 尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 2 || manifest.BootstrapCommit != legacyBootstrapCommit {
		return scannerCatalogAmendmentManifest{}, errors.New("Catalog amendment schema/bootstrap 非法")
	}
	previousSink := ""
	seenCandidate := make(map[string]struct{})
	for _, amendment := range manifest.Amendments {
		if err := validateScannerCatalogAmendment(amendment); err != nil {
			return scannerCatalogAmendmentManifest{}, err
		}
		if previousSink >= amendment.SinkID {
			return scannerCatalogAmendmentManifest{}, errors.New("Catalog amendment 必须按 SinkID 严格排序")
		}
		previousSink = amendment.SinkID
		if _, duplicate := seenCandidate[amendment.Source.ScanCandidateID]; duplicate {
			return scannerCatalogAmendmentManifest{}, errors.New("同一 candidate 不得重复 amendment")
		}
		seenCandidate[amendment.Source.ScanCandidateID] = struct{}{}
	}
	return manifest, nil
}

func validateScannerCatalogAmendment(amendment scannerCatalogAmendment) error {
	source := amendment.Source
	if (amendment.Kind != scannerAmendmentRouteEvidence &&
		amendment.Kind != scannerAmendmentRuntimeScopeExclusion) ||
		strings.TrimSpace(amendment.SinkID) == "" || strings.TrimSpace(source.ScanCandidateID) == "" ||
		strings.TrimSpace(source.ASTFingerprint) == "" || strings.TrimSpace(source.File) == "" ||
		strings.TrimSpace(source.Function) == "" || strings.TrimSpace(source.Callee) == "" ||
		!validDigest(source.SourceBlobSHA256) || strings.TrimSpace(amendment.ReviewedBy) == "" ||
		strings.TrimSpace(amendment.ReviewRef) == "" || strings.TrimSpace(amendment.Rationale) == "" {
		return fmt.Errorf("Catalog amendment 字段不完整: %s", amendment.SinkID)
	}
	if amendment.Kind == scannerAmendmentRouteEvidence && len(amendment.Routes) == 0 {
		return fmt.Errorf("route evidence amendment 缺少 route: %s", amendment.SinkID)
	}
	if amendment.Kind == scannerAmendmentRuntimeScopeExclusion && len(amendment.Routes) != 0 {
		return fmt.Errorf("runtime scope exclusion 不得声明 route: %s", amendment.SinkID)
	}
	return nil
}

func (m scannerCatalogAmendmentManifest) byCandidateID() map[string]scannerCatalogAmendment {
	index := make(map[string]scannerCatalogAmendment, len(m.Amendments))
	for _, amendment := range m.Amendments {
		index[amendment.Source.ScanCandidateID] = amendment
	}
	return index
}

func validateAmendmentFrozenSource(amendment scannerCatalogAmendment, frozen SinkRecord) error {
	source := amendment.Source
	if amendment.SinkID != frozen.RuntimeSinkID || source.ScanCandidateID != frozen.ScanCandidateID ||
		source.ASTFingerprint != frozen.ASTFingerprint || source.File != frozen.File ||
		source.Function != frozen.Func || source.Callee != frozen.Callee {
		return errors.New("amendment 未冻结匹配的 bootstrap candidate")
	}
	return nil
}

func verifyAmendmentSourceBlob(amendment scannerCatalogAmendment, frozen SinkRecord) error {
	if err := validateAmendmentFrozenSource(amendment, frozen); err != nil {
		return err
	}
	return verifySupplementSourceBlob(reviewedSupplement{
		Candidate: frozen, SourceBlobSHA256: amendment.Source.SourceBlobSHA256,
	})
}

func validateScopeCorrectedCandidate(
	amendment scannerCatalogAmendment,
	frozen SinkRecord,
	current SinkRecord,
) error {
	if amendment.Kind != scannerAmendmentRuntimeScopeExclusion {
		return errors.New("不是 runtime scope exclusion")
	}
	if err := validateAmendmentFrozenSource(amendment, frozen); err != nil {
		return err
	}
	if !compareSupplementStructure(frozen, current) {
		return errors.New("scope correction 之外的调用结构发生变化")
	}
	if current.Persona != "out-of-scope" || current.RuntimeSinkID != "" ||
		current.EnforcementState != "not_applicable" {
		return errors.New("scope correction 后当前候选仍可进入运行时 Catalog")
	}
	return nil
}
