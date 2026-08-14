package officialegress

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
)

//go:embed catalogdata/catalog-amendments.json
var catalogAmendmentFS embed.FS

type catalogAmendmentManifest struct {
	SchemaVersion   int                      `json:"schema_version"`
	BootstrapCommit string                   `json:"bootstrap_commit"`
	Amendments      []routeEvidenceAmendment `json:"amendments"`
}

type routeEvidenceAmendment struct {
	Kind       string                   `json:"kind"`
	SinkID     string                   `json:"sink_id"`
	Source     amendmentSourceEvidence  `json:"source"`
	Routes     []amendmentRouteEvidence `json:"routes"`
	ReviewedBy string                   `json:"reviewed_by"`
	ReviewRef  string                   `json:"review_ref"`
	Rationale  string                   `json:"rationale"`
}

const (
	catalogAmendmentRouteEvidence         = "route_evidence"
	catalogAmendmentRuntimeScopeExclusion = "runtime_scope_exclusion"
)

type amendmentSourceEvidence struct {
	ScanCandidateID  string `json:"scan_candidate_id"`
	ASTFingerprint   string `json:"ast_fingerprint"`
	File             string `json:"file"`
	Function         string `json:"function"`
	Callee           string `json:"callee"`
	SourceBlobSHA256 string `json:"source_blob_sha256"`
}

type amendmentRouteEvidence struct {
	Method   string `json:"method"`
	Host     string `json:"host"`
	Path     string `json:"path"`
	Protocol string `json:"protocol"`
}

func loadCatalogAmendments() (catalogAmendmentManifest, error) {
	raw, err := catalogAmendmentFS.ReadFile("catalogdata/catalog-amendments.json")
	if err != nil {
		return catalogAmendmentManifest{}, err
	}
	var manifest catalogAmendmentManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return catalogAmendmentManifest{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return catalogAmendmentManifest{}, errors.New("Catalog 补充证据尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 2 || manifest.BootstrapCommit != BootstrapCommit {
		return catalogAmendmentManifest{}, errors.New("Catalog 补充证据 schema/bootstrap 非法")
	}
	previous := ""
	for _, amendment := range manifest.Amendments {
		if (amendment.Kind != catalogAmendmentRouteEvidence &&
			amendment.Kind != catalogAmendmentRuntimeScopeExclusion) ||
			strings.TrimSpace(amendment.SinkID) == "" ||
			strings.TrimSpace(amendment.ReviewedBy) == "" || strings.TrimSpace(amendment.ReviewRef) == "" ||
			strings.TrimSpace(amendment.Rationale) == "" {
			return catalogAmendmentManifest{}, fmt.Errorf("Catalog route 补充证据字段不完整: %s", amendment.SinkID)
		}
		if amendment.Kind == catalogAmendmentRouteEvidence && len(amendment.Routes) == 0 {
			return catalogAmendmentManifest{}, fmt.Errorf("Catalog route 补充缺少 route: %s", amendment.SinkID)
		}
		if amendment.Kind == catalogAmendmentRuntimeScopeExclusion && len(amendment.Routes) != 0 {
			return catalogAmendmentManifest{}, fmt.Errorf("运行时 scope 排除不得附带 route: %s", amendment.SinkID)
		}
		if previous >= amendment.SinkID {
			return catalogAmendmentManifest{}, errors.New("Catalog 补充证据必须按 SinkID 严格排序")
		}
		previous = amendment.SinkID
		if err := validateAmendmentSource(amendment.Source); err != nil {
			return catalogAmendmentManifest{}, fmt.Errorf("Sink %s: %w", amendment.SinkID, err)
		}
	}
	return manifest, nil
}

func validateAmendmentSource(source amendmentSourceEvidence) error {
	if strings.TrimSpace(source.ScanCandidateID) == "" || strings.TrimSpace(source.ASTFingerprint) == "" ||
		strings.TrimSpace(source.File) == "" || strings.TrimSpace(source.Function) == "" ||
		strings.TrimSpace(source.Callee) == "" || len(source.SourceBlobSHA256) != sha256.Size*2 {
		return errors.New("历史源码证据字段不完整")
	}
	if _, err := hex.DecodeString(source.SourceBlobSHA256); err != nil {
		return errors.New("历史源码 blob 摘要非法")
	}
	return nil
}

func applyCatalogAmendments(
	evidence bindingcontract.BindingCatalog,
	inputs []SinkBindingInput,
) ([]SinkBindingInput, error) {
	manifest, err := loadCatalogAmendments()
	if err != nil {
		return nil, err
	}
	indexBySink := make(map[string]int, len(inputs))
	for index := range inputs {
		indexBySink[string(inputs[index].ID)] = index
	}
	for _, amendment := range manifest.Amendments {
		index, ok := indexBySink[amendment.SinkID]
		if !ok {
			return nil, fmt.Errorf("Catalog route 补充引用未知 SinkID: %s", amendment.SinkID)
		}
		bindingDoc, ok := evidence.Resolve(amendment.SinkID)
		if !ok || !amendmentSourceMatchesBinding(amendment.Source, bindingDoc) {
			return nil, fmt.Errorf("Catalog route 补充的历史候选不匹配: %s", amendment.SinkID)
		}
		input := &inputs[index]
		if amendment.Kind == catalogAmendmentRuntimeScopeExclusion {
			if !input.RuntimeBindable || input.EnforcementState != SinkStateLegacyObserve {
				return nil, fmt.Errorf("运行时 scope 排除目标状态非法: %s", amendment.SinkID)
			}
			// 历史 Evidence Catalog 保留 bootstrap 时的完整分类，运行时 Catalog 则
			// 明确禁止该 ID 成为 binding key。这样不篡改不可变 inventory，也不会
			// 再用非空 OAuth SinkID 污染通用 API-Key 发送。
			input.RuntimeBindable = false
			continue
		}
		if input.EndpointEvidence != EndpointEvidenceCodexProfile {
			return nil, fmt.Errorf("无 codex_profile 证据的 Sink 禁止补充发布 route: %s", amendment.SinkID)
		}
		for _, routeDoc := range amendment.Routes {
			protocol := WireProtocol(routeDoc.Protocol)
			route := CatalogRoute{Key: RouteKey{
				Method: strings.ToUpper(strings.TrimSpace(routeDoc.Method)),
				Host:   strings.ToLower(strings.TrimSpace(routeDoc.Host)), Path: strings.TrimSpace(routeDoc.Path),
				Purpose: input.Purpose,
			}, Protocol: protocol}
			if err := route.Validate(); err != nil {
				return nil, fmt.Errorf("Catalog route 补充非法: %s", amendment.SinkID)
			}
			matched := false
			for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
				release, releaseErr := DefaultReleaseCatalog().Resolve(mode)
				if releaseErr != nil {
					return nil, releaseErr
				}
				if _, ok := uniqueProfileEndpointForPhysical(
					release.ExecutableProfile(), physicalRouteFromCatalogRoute(route),
				); ok {
					matched = true
				}
			}
			if !matched {
				return nil, fmt.Errorf("Catalog route 补充在 Active/Previous 中均没有 ProfileSpec endpoint: %s", amendment.SinkID)
			}
			duplicate := false
			for _, existing := range input.Routes {
				duplicate = duplicate || (existing.Protocol == route.Protocol && existing.Key == route.Key)
			}
			if duplicate {
				return nil, fmt.Errorf("Catalog route 补充与现有 route 重复: %s", amendment.SinkID)
			}
			input.Routes = append(input.Routes, route)
		}
		sort.Slice(input.Routes, func(i, j int) bool {
			return catalogRouteIdentity(input.Routes[i]) < catalogRouteIdentity(input.Routes[j])
		})
	}
	return inputs, nil
}

func amendmentSourceMatchesBinding(
	source amendmentSourceEvidence,
	binding bindingcontract.ReleaseBindingDoc,
) bool {
	for _, candidate := range binding.Candidates {
		if candidate.ScanCandidateID == source.ScanCandidateID &&
			candidate.ASTFingerprint == source.ASTFingerprint && candidate.File == source.File &&
			candidate.Func == source.Function && candidate.Callee == source.Callee {
			return true
		}
	}
	return false
}
