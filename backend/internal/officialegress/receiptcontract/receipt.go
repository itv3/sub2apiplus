// Package receiptcontract 定义 official egress 迁移收据的共享机器契约。
// 运行时 Catalog 与静态扫描门禁共同使用本包，避免两边各自实现摘要算法后发生分叉。
package receiptcontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"path"
	"sort"
	"strings"
)

const SchemaVersion = 2

type AuthorityKind string

const (
	AuthorityCodexExecutor    AuthorityKind = "codex_executor"
	AuthorityClaudeExecutor   AuthorityKind = "claude_persona_executor"
	AuthorityChatGPTWebClient AuthorityKind = "chatgpt_web_client"
)

func (k AuthorityKind) Valid() bool {
	return k == AuthorityCodexExecutor || k == AuthorityClaudeExecutor ||
		k == AuthorityChatGPTWebClient
}

type Manifest struct {
	SchemaVersion   int        `json:"schema_version"`
	BootstrapCommit string     `json:"bootstrap_commit"`
	Receipts        []Document `json:"receipts"`
}

type Document struct {
	SinkID                   string              `json:"sink_id"`
	ApprovedState            string              `json:"approved_state"`
	BindingDigest            string              `json:"binding_digest"`
	AuthorityKind            AuthorityKind       `json:"authority_kind"`
	AuthorityID              string              `json:"authority_id"`
	TokenIssuerID            string              `json:"token_issuer_id"`
	Routes                   []RouteProof        `json:"routes"`
	Candidates               []CandidateEvidence `json:"candidates"`
	PriorCanaryReceiptDigest string              `json:"prior_canary_receipt_digest,omitempty"`
	CanaryAcceptance         *ArtifactRef        `json:"canary_acceptance,omitempty"`
	ReviewedBy               string              `json:"reviewed_by"`
	ReviewRef                string              `json:"review_ref"`
	Rationale                string              `json:"rationale"`
}

type RouteProof struct {
	Route                 RouteIdentity `json:"route"`
	EvidenceKind          string        `json:"evidence_kind"`
	EvidenceID            string        `json:"evidence_id"`
	Backend               string        `json:"backend"`
	AdapterID             string        `json:"adapter_id"`
	TransportID           string        `json:"transport_id"`
	WireFixture           ArtifactRef   `json:"wire_fixture"`
	ExecutionVerification ArtifactRef   `json:"execution_verification"`
}

type RouteIdentity struct {
	Method   string `json:"method"`
	Host     string `json:"host"`
	Path     string `json:"path"`
	Purpose  string `json:"purpose"`
	Protocol string `json:"protocol"`
}

func (r RouteIdentity) Identity() string {
	return strings.Join([]string{
		strings.ToUpper(strings.TrimSpace(r.Method)),
		strings.ToLower(strings.TrimSpace(r.Host)),
		strings.TrimSpace(r.Path), strings.TrimSpace(r.Purpose),
		strings.TrimSpace(r.Protocol),
	}, "\x00")
}

type ArtifactRef struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type CandidateEvidence struct {
	ScanCandidateID string `json:"scan_candidate_id"`
	ASTFingerprint  string `json:"ast_fingerprint"`
}

// ExecutionVerification 是由迁移验证工具生成并被 CI 重新关联的执行产物。
// 它不保存凭据或请求正文，只冻结已验证的运行身份与 wire fixture 摘要。
type ExecutionVerification struct {
	SchemaVersion           int           `json:"schema_version"`
	Result                  string        `json:"result"`
	SinkID                  string        `json:"sink_id"`
	Route                   RouteIdentity `json:"route"`
	AuthorityKind           AuthorityKind `json:"authority_kind"`
	AuthorityID             string        `json:"authority_id"`
	TokenIssuerID           string        `json:"token_issuer_id"`
	EvidenceKind            string        `json:"evidence_kind"`
	EvidenceID              string        `json:"evidence_id"`
	Backend                 string        `json:"backend"`
	AdapterID               string        `json:"adapter_id"`
	TransportID             string        `json:"transport_id"`
	WireSHA256              string        `json:"wire_fixture_sha256"`
	FinalWireManifestSHA256 string        `json:"final_wire_manifest_sha256,omitempty"`
	ActiveCaptureSHA256     string        `json:"active_capture_sha256,omitempty"`
	PreviousCaptureSHA256   string        `json:"previous_capture_sha256,omitempty"`
	TerminalGuardAllow      bool          `json:"terminal_guard_allow,omitempty"`
	ComparedReleaseModes    []string      `json:"compared_release_modes,omitempty"`
}

type CanaryAcceptance struct {
	SchemaVersion       int    `json:"schema_version"`
	Result              string `json:"result"`
	SinkID              string `json:"sink_id"`
	CanaryReceiptDigest string `json:"canary_receipt_digest"`
	ObservationDigest   string `json:"observation_digest"`
	ReviewedBy          string `json:"reviewed_by"`
	ReviewRef           string `json:"review_ref"`
}

func ParseManifest(raw []byte) (Manifest, error) {
	var manifest Manifest
	if err := decodeStrict(raw, &manifest); err != nil {
		return Manifest{}, err
	}
	if err := ValidateManifest(manifest); err != nil {
		return Manifest{}, err
	}
	return manifest, nil
}

func ValidateManifest(manifest Manifest) error {
	if manifest.SchemaVersion != SchemaVersion || strings.TrimSpace(manifest.BootstrapCommit) == "" {
		return errors.New("MigrationReceipt schema/bootstrap 非法")
	}
	previousKey := ""
	canaryBySink := make(map[string]string)
	for index, document := range manifest.Receipts {
		if err := validateDocument(document); err != nil {
			return fmt.Errorf("MigrationReceipt[%d] %s: %w", index, document.SinkID, err)
		}
		stateRank := "0"
		if document.ApprovedState == "enforced" {
			stateRank = "1"
		}
		key := document.SinkID + "\x00" + stateRank
		if previousKey >= key {
			return errors.New("MigrationReceipt 必须按 SinkID 与 canary→enforced 严格排序")
		}
		previousKey = key
		digest, err := DigestDocument(document)
		if err != nil {
			return err
		}
		switch document.ApprovedState {
		case "canary_enforce":
			if document.PriorCanaryReceiptDigest != "" || document.CanaryAcceptance != nil {
				return fmt.Errorf("canary 收据不得声明 enforced 前序: %s", document.SinkID)
			}
			canaryBySink[document.SinkID] = digest
		case "enforced":
			canaryDigest := canaryBySink[document.SinkID]
			if canaryDigest == "" || document.PriorCanaryReceiptDigest != canaryDigest ||
				document.CanaryAcceptance == nil {
				return fmt.Errorf("enforced 收据缺少同 Sink canary 前序或验收产物: %s", document.SinkID)
			}
		}
	}
	return nil
}

func validateDocument(document Document) error {
	if strings.TrimSpace(document.SinkID) == "" ||
		(document.ApprovedState != "canary_enforce" && document.ApprovedState != "enforced") ||
		!ValidSHA256(document.BindingDigest) || !document.AuthorityKind.Valid() ||
		strings.TrimSpace(document.AuthorityID) == "" || strings.TrimSpace(document.TokenIssuerID) == "" ||
		len(document.Routes) == 0 || len(document.Candidates) == 0 ||
		strings.TrimSpace(document.ReviewedBy) == "" || strings.TrimSpace(document.ReviewRef) == "" ||
		strings.TrimSpace(document.Rationale) == "" {
		return errors.New("字段不完整")
	}
	previousRoute := ""
	for _, route := range document.Routes {
		if err := validateRouteProof(route); err != nil {
			return err
		}
		identity := route.Route.Identity()
		if previousRoute >= identity {
			return errors.New("route proof 必须严格排序且不得重复")
		}
		previousRoute = identity
	}
	previousCandidate := ""
	for _, candidate := range document.Candidates {
		if strings.TrimSpace(candidate.ScanCandidateID) == "" ||
			strings.TrimSpace(candidate.ASTFingerprint) == "" || previousCandidate >= candidate.ScanCandidateID {
			return errors.New("源码候选必须完整、排序且不得重复")
		}
		previousCandidate = candidate.ScanCandidateID
	}
	if document.PriorCanaryReceiptDigest != "" && !ValidSHA256(document.PriorCanaryReceiptDigest) {
		return errors.New("prior canary receipt 摘要非法")
	}
	if document.CanaryAcceptance != nil {
		if err := validateArtifactRef(*document.CanaryAcceptance); err != nil {
			return fmt.Errorf("canary acceptance: %w", err)
		}
	}
	return nil
}

func validateRouteProof(proof RouteProof) error {
	route := proof.Route
	if strings.ToUpper(strings.TrimSpace(route.Method)) != route.Method || route.Method == "" ||
		strings.TrimSpace(route.Host) == "" || !strings.HasPrefix(route.Path, "/") ||
		strings.TrimSpace(route.Purpose) == "" || strings.TrimSpace(route.Protocol) == "" ||
		strings.TrimSpace(proof.EvidenceKind) == "" || strings.TrimSpace(proof.EvidenceID) == "" ||
		strings.TrimSpace(proof.Backend) == "" || strings.TrimSpace(proof.AdapterID) == "" ||
		strings.TrimSpace(proof.TransportID) == "" {
		return errors.New("route proof 字段不完整")
	}
	if err := validateArtifactRef(proof.WireFixture); err != nil {
		return fmt.Errorf("wire fixture: %w", err)
	}
	if err := validateArtifactRef(proof.ExecutionVerification); err != nil {
		return fmt.Errorf("execution verification: %w", err)
	}
	return nil
}

func validateArtifactRef(ref ArtifactRef) error {
	cleaned := path.Clean(strings.TrimSpace(ref.Path))
	if cleaned == "." || cleaned != ref.Path || strings.HasPrefix(cleaned, "/") ||
		cleaned == ".." || strings.HasPrefix(cleaned, "../") || !ValidSHA256(ref.SHA256) {
		return errors.New("产物路径或摘要非法")
	}
	if !strings.HasPrefix(cleaned, "catalogdata/migration-artifacts/") {
		return errors.New("产物必须位于受控 migration-artifacts 目录")
	}
	return nil
}

func DigestDocument(document Document) (string, error) {
	raw, err := json.Marshal(document)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func VerifyArtifacts(files fs.FS, document Document) error {
	if files == nil {
		return errors.New("MigrationReceipt 缺少产物文件系统")
	}
	for _, proof := range document.Routes {
		wireRaw, err := verifyArtifact(files, proof.WireFixture)
		if err != nil {
			return fmt.Errorf("route %s wire fixture: %w", proof.Route.Identity(), err)
		}
		verificationRaw, err := verifyArtifact(files, proof.ExecutionVerification)
		if err != nil {
			return fmt.Errorf("route %s execution verification: %w", proof.Route.Identity(), err)
		}
		var verification ExecutionVerification
		if err := decodeStrict(verificationRaw, &verification); err != nil {
			return fmt.Errorf("解析 execution verification: %w", err)
		}
		wireSum := sha256.Sum256(wireRaw)
		if (verification.SchemaVersion != 1 && verification.SchemaVersion != 2) ||
			verification.Result != "passed" ||
			verification.SinkID != document.SinkID || verification.Route != proof.Route ||
			verification.AuthorityKind != document.AuthorityKind ||
			verification.AuthorityID != document.AuthorityID ||
			verification.TokenIssuerID != document.TokenIssuerID ||
			verification.EvidenceKind != proof.EvidenceKind || verification.EvidenceID != proof.EvidenceID ||
			verification.Backend != proof.Backend || verification.AdapterID != proof.AdapterID ||
			verification.TransportID != proof.TransportID ||
			verification.WireSHA256 != hex.EncodeToString(wireSum[:]) {
			return fmt.Errorf("execution verification 与收据 route claim 不一致: %s", proof.Route.Identity())
		}
		if verification.SchemaVersion == 2 &&
			(!ValidSHA256(verification.FinalWireManifestSHA256) ||
				!ValidSHA256(verification.ActiveCaptureSHA256) ||
				!ValidSHA256(verification.PreviousCaptureSHA256) ||
				!verification.TerminalGuardAllow || len(verification.ComparedReleaseModes) != 2 ||
				verification.ComparedReleaseModes[0] != "active" ||
				verification.ComparedReleaseModes[1] != "previous") {
			return fmt.Errorf("schema v2 execution verification 缺少双 release final-wire/Guard 证据: %s", proof.Route.Identity())
		}
	}
	if document.ApprovedState == "enforced" {
		raw, err := verifyArtifact(files, *document.CanaryAcceptance)
		if err != nil {
			return err
		}
		var acceptance CanaryAcceptance
		if err := decodeStrict(raw, &acceptance); err != nil {
			return err
		}
		if (acceptance.SchemaVersion != 1 && acceptance.SchemaVersion != 2) ||
			acceptance.Result != "accepted" ||
			acceptance.SinkID != document.SinkID ||
			acceptance.CanaryReceiptDigest != document.PriorCanaryReceiptDigest ||
			!ValidSHA256(acceptance.ObservationDigest) || strings.TrimSpace(acceptance.ReviewedBy) == "" ||
			strings.TrimSpace(acceptance.ReviewRef) == "" {
			return errors.New("canary acceptance 与 enforced 收据不一致")
		}
	}
	return nil
}

func verifyArtifact(files fs.FS, ref ArtifactRef) ([]byte, error) {
	raw, err := fs.ReadFile(files, ref.Path)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != strings.ToLower(ref.SHA256) {
		return nil, errors.New("产物摘要不匹配")
	}
	return raw, nil
}

func ValidSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil && value == strings.ToLower(value)
}

func CandidateIDs(document Document) []string {
	ids := make([]string, 0, len(document.Candidates))
	for _, candidate := range document.Candidates {
		ids = append(ids, candidate.ScanCandidateID)
	}
	sort.Strings(ids)
	return ids
}

func decodeStrict(raw []byte, value any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(value); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("JSON 尾部存在额外数据")
		}
		return err
	}
	return nil
}
