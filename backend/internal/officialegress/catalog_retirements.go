package officialegress

import (
	"bytes"
	_ "embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
)

//go:embed catalogdata/maintenance-removal-receipts.json
var embeddedMaintenanceRemovalReceipts []byte

// catalogRetirementManifest 复用 egressscan 的 RemovalReceipt 清单作为当前
// Runtime Catalog 的追加式退休覆盖层。bootstrap ReleaseBinding 保持不可变，
// 只有经该清单完整证明的 pending_removal dead-code Sink 才能从当前 Catalog 消失。
type catalogRetirementManifest struct {
	SchemaVersion   int                        `json:"schema_version"`
	BootstrapCommit string                     `json:"bootstrap_commit"`
	Receipts        []catalogRetirementReceipt `json:"receipts"`
}

type catalogRetirementReceipt struct {
	Candidate              json.RawMessage `json:"candidate"`
	Kind                   string          `json:"kind"`
	ReplacementSinkID      string          `json:"replacement_sink_id,omitempty"`
	MigrationReceiptDigest string          `json:"migration_receipt_digest,omitempty"`
	DelegationCandidateID  string          `json:"delegation_candidate_id,omitempty"`
	SourceBlobSHA256       string          `json:"source_blob_sha256"`
	ReviewedBy             string          `json:"reviewed_by"`
	ReviewRef              string          `json:"review_ref"`
	Rationale              string          `json:"rationale"`
}

type catalogRetirementCandidate struct {
	ScanCandidateID  string `json:"scan_candidate_id"`
	File             string `json:"file"`
	Function         string `json:"func"`
	Callee           string `json:"callee"`
	ASTFingerprint   string `json:"ast_fingerprint"`
	RuntimeSinkID    string `json:"runtime_sink_id"`
	Persona          string `json:"persona"`
	EnforcementState string `json:"enforcement_state"`
}

type approvedCatalogRetirement struct {
	candidate catalogRetirementCandidate
}

func loadCatalogRetirementManifest() (catalogRetirementManifest, error) {
	var manifest catalogRetirementManifest
	decoder := json.NewDecoder(bytes.NewReader(embeddedMaintenanceRemovalReceipts))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return catalogRetirementManifest{}, err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return catalogRetirementManifest{}, errors.New("Catalog 退休清单尾部存在额外 JSON")
	}
	if manifest.SchemaVersion != 3 || manifest.BootstrapCommit != BootstrapCommit {
		return catalogRetirementManifest{}, errors.New("Catalog 退休清单 schema/bootstrap 非法")
	}
	return manifest, nil
}

func applyCatalogRetirements(
	evidenceBySink map[string]bindingcontract.ReleaseBindingDoc,
	inputs []SinkBindingInput,
) ([]SinkBindingInput, map[string]bindingcontract.ReleaseBindingDoc, error) {
	manifest, err := loadCatalogRetirementManifest()
	if err != nil {
		return nil, nil, err
	}
	approved, err := validateCatalogRetirements(manifest, evidenceBySink, inputs)
	if err != nil {
		return nil, nil, err
	}

	retiredSinkIDs := make(map[SinkID]struct{}, len(approved))
	for _, item := range approved {
		retiredSinkIDs[SinkID(item.candidate.RuntimeSinkID)] = struct{}{}
	}
	currentInputs := make([]SinkBindingInput, 0, len(inputs)-len(retiredSinkIDs))
	for _, input := range inputs {
		if _, retired := retiredSinkIDs[input.ID]; retired {
			continue
		}
		currentInputs = append(currentInputs, input)
	}
	currentEvidence := make(map[string]bindingcontract.ReleaseBindingDoc, len(evidenceBySink)-len(retiredSinkIDs))
	for sinkID, evidence := range evidenceBySink {
		if _, retired := retiredSinkIDs[SinkID(sinkID)]; retired {
			continue
		}
		currentEvidence[sinkID] = evidence
	}
	return currentInputs, currentEvidence, nil
}

func validateCatalogRetirements(
	manifest catalogRetirementManifest,
	evidenceBySink map[string]bindingcontract.ReleaseBindingDoc,
	inputs []SinkBindingInput,
) ([]approvedCatalogRetirement, error) {
	indexBySink := make(map[string]SinkBindingInput, len(inputs))
	for _, input := range inputs {
		indexBySink[string(input.ID)] = input
	}

	approved := make([]approvedCatalogRetirement, 0, len(manifest.Receipts))
	seenCandidates := make(map[string]struct{}, len(manifest.Receipts))
	seenSinks := make(map[string]struct{}, len(manifest.Receipts))
	previousCandidateID := ""
	for _, receipt := range manifest.Receipts {
		var candidate catalogRetirementCandidate
		if err := json.Unmarshal(receipt.Candidate, &candidate); err != nil {
			return nil, fmt.Errorf("解析 Catalog 退休候选失败: %w", err)
		}
		if candidate.ScanCandidateID == "" || candidate.RuntimeSinkID == "" ||
			candidate.File == "" || candidate.Function == "" || candidate.Callee == "" ||
			candidate.ASTFingerprint == "" || receipt.Kind != "dead_code_removed" ||
			candidate.Persona != string(PersonaDeadCode) ||
			candidate.EnforcementState != string(SinkStatePendingRemoval) ||
			!isSHA256(receipt.SourceBlobSHA256) || strings.TrimSpace(receipt.ReviewedBy) == "" ||
			strings.TrimSpace(receipt.ReviewRef) == "" || strings.TrimSpace(receipt.Rationale) == "" {
			return nil, fmt.Errorf("Catalog 退休收据字段非法: %s", candidate.ScanCandidateID)
		}
		if receipt.ReplacementSinkID != "" || receipt.MigrationReceiptDigest != "" ||
			receipt.DelegationCandidateID != "" {
			return nil, fmt.Errorf("dead-code 退休不得声明替代路径: %s", candidate.ScanCandidateID)
		}
		if previousCandidateID >= candidate.ScanCandidateID {
			return nil, errors.New("Catalog 退休收据必须按 scan_candidate_id 严格排序")
		}
		previousCandidateID = candidate.ScanCandidateID
		if _, duplicate := seenCandidates[candidate.ScanCandidateID]; duplicate {
			return nil, fmt.Errorf("Catalog 退休候选重复: %s", candidate.ScanCandidateID)
		}
		seenCandidates[candidate.ScanCandidateID] = struct{}{}
		if _, duplicate := seenSinks[candidate.RuntimeSinkID]; duplicate {
			return nil, fmt.Errorf("Catalog 退休 SinkID 重复: %s", candidate.RuntimeSinkID)
		}
		seenSinks[candidate.RuntimeSinkID] = struct{}{}

		input, ok := indexBySink[candidate.RuntimeSinkID]
		if !ok || input.Persona != PersonaDeadCode ||
			input.EnforcementState != SinkStatePendingRemoval || input.RuntimeBindable {
			return nil, fmt.Errorf("Catalog 退休目标不是不可达 dead-code: %s", candidate.RuntimeSinkID)
		}
		binding, ok := evidenceBySink[candidate.RuntimeSinkID]
		if !ok || len(binding.Candidates) != 1 || !catalogRetirementCandidateMatches(
			candidate, binding.Candidates[0],
		) {
			return nil, fmt.Errorf("Catalog 退休候选与冻结 ReleaseBinding 不匹配: %s", candidate.RuntimeSinkID)
		}
		approved = append(approved, approvedCatalogRetirement{candidate: candidate})
	}

	sort.Slice(approved, func(i, j int) bool {
		return approved[i].candidate.RuntimeSinkID < approved[j].candidate.RuntimeSinkID
	})
	return approved, nil
}

func catalogRetirementCandidateMatches(
	candidate catalogRetirementCandidate,
	frozen bindingcontract.BindingCandidateDoc,
) bool {
	return candidate.ScanCandidateID == frozen.ScanCandidateID &&
		candidate.File == frozen.File && candidate.Function == frozen.Func &&
		candidate.Callee == frozen.Callee && candidate.ASTFingerprint == frozen.ASTFingerprint
}
