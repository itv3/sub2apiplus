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

const bootstrapTree = "a8c3dee18a01a6138bfcea60860bb5ad11548c3a"

//go:embed catalogdata/pre-bootstrap-supplements.json
var preBootstrapSupplementFS embed.FS

type preBootstrapSupplementManifest struct {
	SchemaVersion   int                      `json:"schema_version"`
	BootstrapCommit string                   `json:"bootstrap_commit"`
	BootstrapTree   string                   `json:"bootstrap_tree"`
	Lifecycle       string                   `json:"lifecycle"`
	Supplements     []preBootstrapSupplement `json:"supplements"`
}

type preBootstrapSupplement struct {
	Candidate        bindingcontract.SinkBaselineCandidateDoc `json:"candidate"`
	SourceBlobSHA256 string                                   `json:"source_blob_sha256"`
	ReviewedBy       string                                   `json:"reviewed_by"`
	ReviewRef        string                                   `json:"review_ref"`
	Rationale        string                                   `json:"rationale"`
}

func loadPreBootstrapSupplementManifest() (preBootstrapSupplementManifest, error) {
	raw, err := preBootstrapSupplementFS.ReadFile("catalogdata/pre-bootstrap-supplements.json")
	if err != nil {
		return preBootstrapSupplementManifest{}, err
	}
	var manifest preBootstrapSupplementManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return preBootstrapSupplementManifest{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return preBootstrapSupplementManifest{}, errors.New("pre-bootstrap 补录清单尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 2 || manifest.BootstrapCommit != BootstrapCommit ||
		manifest.BootstrapTree != bootstrapTree ||
		(manifest.Lifecycle != "provisional" && manifest.Lifecycle != "sealed") {
		return preBootstrapSupplementManifest{}, errors.New("pre-bootstrap 补录清单元数据非法")
	}
	previous := ""
	for _, item := range manifest.Supplements {
		if previous >= item.Candidate.ScanCandidateID {
			return preBootstrapSupplementManifest{}, errors.New("pre-bootstrap 补录必须按 candidate ID 严格排序")
		}
		previous = item.Candidate.ScanCandidateID
		if strings.TrimSpace(item.ReviewedBy) == "" || strings.TrimSpace(item.ReviewRef) == "" ||
			strings.TrimSpace(item.Rationale) == "" || !isSHA256(item.SourceBlobSHA256) {
			return preBootstrapSupplementManifest{}, fmt.Errorf("pre-bootstrap 补录审核字段非法: %s", item.Candidate.ScanCandidateID)
		}
		if item.Candidate.RuntimeSinkID == "" || item.Candidate.IsFacade ||
			item.Candidate.EnforcementState != string(SinkStateLegacyObserve) ||
			item.Candidate.Persona == "out-of-scope" || item.Candidate.Persona == "infrastructure" ||
			item.Candidate.Persona == "dead-code" || len(item.Candidate.Routes) == 0 {
			return preBootstrapSupplementManifest{}, fmt.Errorf("pre-bootstrap 补录不是可运行 legacy sink: %s", item.Candidate.ScanCandidateID)
		}
	}
	return manifest, nil
}

func isSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func applyPreBootstrapSupplements(
	evidence bindingcontract.BindingCatalog,
	inputs []SinkBindingInput,
) ([]SinkBindingInput, map[string]bindingcontract.ReleaseBindingDoc, error) {
	manifest, err := loadPreBootstrapSupplementManifest()
	if err != nil {
		return nil, nil, err
	}
	evidenceBySink := make(map[string]bindingcontract.ReleaseBindingDoc)
	knownCandidates := make(map[string]struct{})
	for _, binding := range evidence.Bindings() {
		evidenceBySink[binding.SinkID] = binding
		for _, candidate := range binding.Candidates {
			knownCandidates[candidate.ScanCandidateID] = struct{}{}
		}
	}
	indexBySink := make(map[string]int, len(inputs))
	for index := range inputs {
		indexBySink[string(inputs[index].ID)] = index
	}
	for _, supplement := range manifest.Supplements {
		candidateID := supplement.Candidate.ScanCandidateID
		if _, exists := knownCandidates[candidateID]; exists {
			return nil, nil, fmt.Errorf("pre-bootstrap 补录重复已有候选: %s", candidateID)
		}
		bindingDoc, err := bindingFromSupplement(supplement.Candidate)
		if err != nil {
			return nil, nil, fmt.Errorf("pre-bootstrap 补录 %s: %w", candidateID, err)
		}
		input, err := sinkBindingInputFromEvidence(bindingDoc)
		if err != nil {
			return nil, nil, err
		}
		if index, exists := indexBySink[bindingDoc.SinkID]; exists {
			if err := mergeSupplementInput(&inputs[index], input); err != nil {
				return nil, nil, err
			}
			evidenceBySink[bindingDoc.SinkID] = mergeSupplementBindingEvidence(
				evidenceBySink[bindingDoc.SinkID], bindingDoc,
			)
		} else {
			inputs = append(inputs, input)
			indexBySink[bindingDoc.SinkID] = len(inputs) - 1
			evidenceBySink[bindingDoc.SinkID] = bindingDoc
		}
		knownCandidates[candidateID] = struct{}{}
	}
	sort.Slice(inputs, func(i, j int) bool { return inputs[i].ID < inputs[j].ID })
	return inputs, evidenceBySink, nil
}

func bindingFromSupplement(candidate bindingcontract.SinkBaselineCandidateDoc) (bindingcontract.ReleaseBindingDoc, error) {
	doc := bindingcontract.SinkBaselineDoc{
		BootstrapCommit: BootstrapCommit,
		BuildContexts:   append([]string(nil), candidate.BuildContexts...),
		PackagesLoaded:  1,
		ScanPattern:     "github.com/Wei-Shaw/sub2api/...",
		Sinks:           []bindingcontract.SinkBaselineCandidateDoc{candidate},
	}
	raw, err := json.Marshal(doc)
	if err != nil {
		return bindingcontract.ReleaseBindingDoc{}, err
	}
	built, err := bindingcontract.BuildBindingCatalogDoc(raw)
	if err != nil {
		return bindingcontract.ReleaseBindingDoc{}, err
	}
	catalog, err := bindingcontract.NewBindingCatalog(built)
	if err != nil {
		return bindingcontract.ReleaseBindingDoc{}, err
	}
	bindings := catalog.Bindings()
	if len(bindings) != 1 {
		return bindingcontract.ReleaseBindingDoc{}, errors.New("补录没有确定性生成单个 ReleaseBinding")
	}
	return bindings[0], nil
}

func mergeSupplementInput(target *SinkBindingInput, addition SinkBindingInput) error {
	if target.ID != addition.ID || target.Purpose != addition.Purpose || target.Persona != addition.Persona ||
		target.EndpointEvidence != addition.EndpointEvidence || target.TargetBackend != addition.TargetBackend ||
		target.EnforcementState != addition.EnforcementState || target.Owner != addition.Owner ||
		target.MigrationChangeset != addition.MigrationChangeset || target.ExpiryCondition != addition.ExpiryCondition ||
		target.RuntimeBindable != addition.RuntimeBindable {
		return fmt.Errorf("pre-bootstrap 补录与既有 Sink 身份冲突: %s", addition.ID)
	}
	for _, route := range addition.Routes {
		found := false
		for _, existing := range target.Routes {
			found = found || existing == route
		}
		if !found {
			target.Routes = append(target.Routes, route)
		}
	}
	for _, backend := range addition.LegacyBackends {
		if !supplementContainsBackend(target.LegacyBackends, backend) {
			target.LegacyBackends = append(target.LegacyBackends, backend)
		}
	}
	sort.Slice(target.Routes, func(i, j int) bool {
		return catalogRouteIdentity(target.Routes[i]) < catalogRouteIdentity(target.Routes[j])
	})
	sort.Slice(target.LegacyBackends, func(i, j int) bool { return target.LegacyBackends[i] < target.LegacyBackends[j] })
	return nil
}

func supplementContainsBackend(backends []BackendKind, candidate BackendKind) bool {
	for _, backend := range backends {
		if backend == candidate {
			return true
		}
	}
	return false
}

func mergeSupplementBindingEvidence(
	target bindingcontract.ReleaseBindingDoc,
	addition bindingcontract.ReleaseBindingDoc,
) bindingcontract.ReleaseBindingDoc {
	target.Candidates = append(target.Candidates, addition.Candidates...)
	sort.Slice(target.Candidates, func(i, j int) bool {
		return target.Candidates[i].ScanCandidateID < target.Candidates[j].ScanCandidateID
	})
	for _, route := range addition.Routes {
		found := false
		for _, existing := range target.Routes {
			found = found || existing == route
		}
		if !found {
			target.Routes = append(target.Routes, route)
		}
	}
	sort.Slice(target.Routes, func(i, j int) bool { return target.Routes[i].Raw < target.Routes[j].Raw })
	return target
}
