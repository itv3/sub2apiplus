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
	"io/fs"
	"path"
	"regexp"
	"slices"
	"strings"
	"sync"
)

// Claude 的 Profile、Wire、Approval 和 selector 统一从这个只读文件系统加载。
// 目录中的历史制品可以继续保留，但只有 release-catalog.json 可授予运行时选择权。
//
//go:embed catalogdata/claude
var claudeReleaseCatalogFS embed.FS

const claudeReleaseCatalogManifestPath = "catalogdata/claude/release-catalog.json"

var claudeCommitPattern = regexp.MustCompile(`^[0-9a-f]{40}$`)

type claudeCatalogBlobReference struct {
	Path   string `json:"path"`
	SHA256 string `json:"sha256"`
}

type claudeReleaseCatalogDocument struct {
	SchemaVersion string `json:"schema_version"`
	Persona       string `json:"persona"`
	Source        string `json:"source"`
	Releases      []struct {
		Version                     string                     `json:"version"`
		Platform                    string                     `json:"platform"`
		Profile                     claudeCatalogBlobReference `json:"profile"`
		Wire                        claudeCatalogBlobReference `json:"wire"`
		RequiredRulesManifestSHA256 string                     `json:"required_rules_manifest_sha256"`
		ReleaseSHA256               string                     `json:"release_sha256"`
		BundleSHA256                string                     `json:"bundle_sha256"`
		RequiredRuleCount           int                        `json:"required_rule_count"`
		ProfileAtomicAssertionCount int                        `json:"profile_atomic_assertion_count"`
		LocalScenarioAssertionCount int                        `json:"local_scenario_assertion_count"`
		StrictEndpointCount         int                        `json:"strict_endpoint_count"`
		Models                      []string                   `json:"models"`
		ModelEvidence               struct {
			SuccessfulAttempts       int `json:"successful_attempts"`
			HistoricalFailedAttempts int `json:"historical_failed_attempts"`
		} `json:"model_evidence"`
	} `json:"releases"`
	Selectors struct {
		ValidationCandidate claudeReleaseSelectorDocument `json:"validation_candidate"`
		ProductionActive    claudeReleaseSelectorDocument `json:"production_active"`
		ProductionRollback  claudeReleaseSelectorDocument `json:"production_rollback"`
	} `json:"selectors"`
}

type claudeReleaseSelectorDocument struct {
	Kind          string                      `json:"kind"`
	ReleaseSHA256 string                      `json:"release_sha256,omitempty"`
	Changeset     string                      `json:"changeset,omitempty"`
	Approval      *claudeCatalogBlobReference `json:"approval,omitempty"`
	Deployment    *struct {
		Commit      string                     `json:"commit"`
		ImageDigest string                     `json:"image_digest"`
		Receipt     claudeCatalogBlobReference `json:"receipt"`
	} `json:"deployment,omitempty"`
}

// ResolvedClaudeRelease 是 Claude Persona 私有的不可变发布制品。
// 原文字节只供本包解析，不向共享控制合同暴露 Persona 字段。
type ResolvedClaudeRelease struct {
	version                     string
	platform                    string
	profilePath                 string
	profileDigest               string
	wirePath                    string
	wireDigest                  string
	requiredRulesManifestDigest string
	releaseDigest               string
	bundleDigest                string
	requiredRuleCount           int
	profileAtomicAssertionCount int
	localScenarioAssertionCount int
	strictEndpointCount         int
	models                      []string
	modelSuccessfulAttempts     int
	modelHistoricalFailures     int
	profileRaw                  []byte
	wireRaw                     []byte
}

func (r ResolvedClaudeRelease) Version() string       { return r.version }
func (r ResolvedClaudeRelease) Platform() string      { return r.platform }
func (r ResolvedClaudeRelease) ProfileDigest() string { return r.profileDigest }
func (r ResolvedClaudeRelease) WireDigest() string    { return r.wireDigest }
func (r ResolvedClaudeRelease) ReleaseDigest() string { return r.releaseDigest }
func (r ResolvedClaudeRelease) BundleDigest() string  { return r.bundleDigest }
func (r ResolvedClaudeRelease) RequiredRuleCount() int {
	return r.requiredRuleCount
}
func (r ResolvedClaudeRelease) ProfileAtomicAssertionCount() int {
	return r.profileAtomicAssertionCount
}
func (r ResolvedClaudeRelease) LocalScenarioAssertionCount() int {
	return r.localScenarioAssertionCount
}
func (r ResolvedClaudeRelease) StrictEndpointCount() int { return r.strictEndpointCount }
func (r ResolvedClaudeRelease) Models() []string {
	return append([]string(nil), r.models...)
}

func (r ResolvedClaudeRelease) validate() error {
	if strings.TrimSpace(r.version) == "" || strings.TrimSpace(r.platform) == "" ||
		!receiptSHA256(r.profileDigest) || !receiptSHA256(r.wireDigest) ||
		!receiptSHA256(r.requiredRulesManifestDigest) ||
		!receiptSHA256(r.releaseDigest) || !receiptSHA256(r.bundleDigest) ||
		r.requiredRuleCount <= 0 || r.profileAtomicAssertionCount <= 0 ||
		r.localScenarioAssertionCount <= 0 || r.strictEndpointCount <= 0 ||
		r.modelSuccessfulAttempts <= 0 || r.modelHistoricalFailures < 0 ||
		len(r.models) == 0 || len(r.profileRaw) == 0 || len(r.wireRaw) == 0 {
		return errors.New("Claude Release 坐标不完整")
	}
	return nil
}

type claudeReleaseSelectorRole string

const (
	claudeSelectorValidationCandidate claudeReleaseSelectorRole = "validation_candidate"
	claudeSelectorProductionActive    claudeReleaseSelectorRole = "production_active"
)

// ResolvedClaudeReleaseSelection 把可变 selector 与不可变 Release 分开保存。
type ResolvedClaudeReleaseSelection struct {
	role           claudeReleaseSelectorRole
	changeset      string
	release        ResolvedClaudeRelease
	approvalPath   string
	approvalDigest string
	approvalRaw    []byte
}

func (s ResolvedClaudeReleaseSelection) Changeset() string { return s.changeset }
func (s ResolvedClaudeReleaseSelection) Release() ResolvedClaudeRelease {
	return s.release
}
func (s ResolvedClaudeReleaseSelection) ApprovalDigest() string {
	return s.approvalDigest
}

// ResolvedClaudeOperationalRollback 明确表示没有对应 strict Release 的操作回退。
// 它不能被转换为 Profile 或用于生成 final wire。
type ResolvedClaudeOperationalRollback struct {
	commit        string
	imageDigest   string
	receiptPath   string
	receiptDigest string
}

func (r ResolvedClaudeOperationalRollback) Commit() string        { return r.commit }
func (r ResolvedClaudeOperationalRollback) ImageDigest() string   { return r.imageDigest }
func (r ResolvedClaudeOperationalRollback) ReceiptPath() string   { return r.receiptPath }
func (r ResolvedClaudeOperationalRollback) ReceiptDigest() string { return r.receiptDigest }

// ClaudeReleaseCatalog 是 Claude Persona 的进程级不可变制品和 selector 目录。
type ClaudeReleaseCatalog struct {
	releases            map[string]ResolvedClaudeRelease
	validationCandidate ResolvedClaudeReleaseSelection
	productionActive    ResolvedClaudeReleaseSelection
	productionRollback  ResolvedClaudeOperationalRollback
	digest              string
	source              string
}

func LoadEmbeddedClaudeReleaseCatalog() (ClaudeReleaseCatalog, error) {
	return loadClaudeReleaseCatalogFromFS(claudeReleaseCatalogFS)
}

func loadClaudeReleaseCatalogFromFS(catalogFS fs.FS) (ClaudeReleaseCatalog, error) {
	if catalogFS == nil {
		return ClaudeReleaseCatalog{}, errors.New("Claude ReleaseCatalog 文件系统为空")
	}
	manifestRaw, err := fs.ReadFile(catalogFS, claudeReleaseCatalogManifestPath)
	if err != nil {
		return ClaudeReleaseCatalog{}, err
	}
	var document claudeReleaseCatalogDocument
	decoder := json.NewDecoder(bytes.NewReader(manifestRaw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&document); err != nil {
		return ClaudeReleaseCatalog{}, fmt.Errorf("解析 Claude ReleaseCatalog：%w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return ClaudeReleaseCatalog{}, errors.New("Claude ReleaseCatalog 尾部存在额外 JSON")
	}
	if document.SchemaVersion != "official-egress-claude-release-catalog/v1" ||
		document.Persona != string(PersonaClaudeCode) || strings.TrimSpace(document.Source) == "" ||
		len(document.Releases) == 0 {
		return ClaudeReleaseCatalog{}, errors.New("Claude ReleaseCatalog 顶层身份非法")
	}
	catalog := ClaudeReleaseCatalog{
		releases: make(map[string]ResolvedClaudeRelease, len(document.Releases)),
		source:   document.Source,
	}
	for _, entry := range document.Releases {
		release, loadErr := loadClaudeCatalogRelease(catalogFS, entry)
		if loadErr != nil {
			return ClaudeReleaseCatalog{}, loadErr
		}
		if _, duplicate := catalog.releases[release.releaseDigest]; duplicate {
			return ClaudeReleaseCatalog{}, errors.New("Claude ReleaseCatalog Release 摘要重复")
		}
		catalog.releases[release.releaseDigest] = release
	}
	catalog.validationCandidate, err = resolveClaudeCatalogReleaseSelector(
		catalogFS, catalog.releases, claudeSelectorValidationCandidate,
		document.Selectors.ValidationCandidate, false,
	)
	if err != nil {
		return ClaudeReleaseCatalog{}, err
	}
	catalog.productionActive, err = resolveClaudeCatalogReleaseSelector(
		catalogFS, catalog.releases, claudeSelectorProductionActive,
		document.Selectors.ProductionActive, true,
	)
	if err != nil {
		return ClaudeReleaseCatalog{}, err
	}
	if catalog.validationCandidate.Changeset() == catalog.productionActive.Changeset() {
		return ClaudeReleaseCatalog{}, errors.New("Claude candidate 与 production active changeset 被折叠")
	}
	catalog.productionRollback, err = resolveClaudeOperationalRollback(
		document.Selectors.ProductionRollback,
	)
	if err != nil {
		return ClaudeReleaseCatalog{}, err
	}
	sum := sha256.Sum256(manifestRaw)
	catalog.digest = hex.EncodeToString(sum[:])
	return catalog, nil
}

func loadClaudeCatalogRelease(
	catalogFS fs.FS,
	entry struct {
		Version                     string                     `json:"version"`
		Platform                    string                     `json:"platform"`
		Profile                     claudeCatalogBlobReference `json:"profile"`
		Wire                        claudeCatalogBlobReference `json:"wire"`
		RequiredRulesManifestSHA256 string                     `json:"required_rules_manifest_sha256"`
		ReleaseSHA256               string                     `json:"release_sha256"`
		BundleSHA256                string                     `json:"bundle_sha256"`
		RequiredRuleCount           int                        `json:"required_rule_count"`
		ProfileAtomicAssertionCount int                        `json:"profile_atomic_assertion_count"`
		LocalScenarioAssertionCount int                        `json:"local_scenario_assertion_count"`
		StrictEndpointCount         int                        `json:"strict_endpoint_count"`
		Models                      []string                   `json:"models"`
		ModelEvidence               struct {
			SuccessfulAttempts       int `json:"successful_attempts"`
			HistoricalFailedAttempts int `json:"historical_failed_attempts"`
		} `json:"model_evidence"`
	},
) (ResolvedClaudeRelease, error) {
	version := strings.TrimSpace(entry.Version)
	profileRaw, err := readClaudeCatalogBlob(
		catalogFS, entry.Profile,
		path.Join("catalogdata/claude/profiles", version),
	)
	if err != nil {
		return ResolvedClaudeRelease{}, fmt.Errorf("读取 Claude Profile：%w", err)
	}
	wireRaw, err := readClaudeCatalogBlob(
		catalogFS, entry.Wire,
		path.Join("catalogdata/claude/wire", version),
	)
	if err != nil {
		return ResolvedClaudeRelease{}, fmt.Errorf("读取 Claude Wire：%w", err)
	}
	models := append([]string(nil), entry.Models...)
	if !validClaudeCatalogStringSet(models) {
		return ResolvedClaudeRelease{}, errors.New("Claude Release 模型闭集非法")
	}
	release := ResolvedClaudeRelease{
		version: version, platform: strings.TrimSpace(entry.Platform),
		profilePath: entry.Profile.Path, profileDigest: entry.Profile.SHA256,
		wirePath: entry.Wire.Path, wireDigest: entry.Wire.SHA256,
		requiredRulesManifestDigest: entry.RequiredRulesManifestSHA256,
		releaseDigest:               entry.ReleaseSHA256, bundleDigest: entry.BundleSHA256,
		requiredRuleCount:           entry.RequiredRuleCount,
		profileAtomicAssertionCount: entry.ProfileAtomicAssertionCount,
		localScenarioAssertionCount: entry.LocalScenarioAssertionCount,
		strictEndpointCount:         entry.StrictEndpointCount, models: models,
		modelSuccessfulAttempts: entry.ModelEvidence.SuccessfulAttempts,
		modelHistoricalFailures: entry.ModelEvidence.HistoricalFailedAttempts,
		profileRaw:              profileRaw, wireRaw: wireRaw,
	}
	if err := release.validate(); err != nil {
		return ResolvedClaudeRelease{}, err
	}
	if err := validateClaudeCatalogArtifactIdentities(release); err != nil {
		return ResolvedClaudeRelease{}, err
	}
	return release, nil
}

func readClaudeCatalogBlob(
	catalogFS fs.FS,
	reference claudeCatalogBlobReference,
	directory string,
) ([]byte, error) {
	if !receiptSHA256(reference.SHA256) || path.Clean(reference.Path) != reference.Path ||
		reference.Path != path.Join(directory, reference.SHA256+".json") {
		return nil, fmt.Errorf("Claude Catalog blob 不是规范内容寻址路径：%s", reference.Path)
	}
	raw, err := fs.ReadFile(catalogFS, reference.Path)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != reference.SHA256 {
		return nil, fmt.Errorf("Claude Catalog blob 摘要不一致：%s", reference.Path)
	}
	return raw, nil
}

func readClaudeCatalogApproval(
	catalogFS fs.FS,
	reference claudeCatalogBlobReference,
) ([]byte, error) {
	if !receiptSHA256(reference.SHA256) || path.Clean(reference.Path) != reference.Path ||
		!strings.HasPrefix(reference.Path, "catalogdata/claude/production/") ||
		!strings.HasSuffix(reference.Path, ".json") {
		return nil, errors.New("Claude production selector Approval 引用非法")
	}
	raw, err := fs.ReadFile(catalogFS, reference.Path)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != reference.SHA256 {
		return nil, errors.New("Claude production selector Approval 摘要不一致")
	}
	return raw, nil
}

func resolveClaudeCatalogReleaseSelector(
	catalogFS fs.FS,
	releases map[string]ResolvedClaudeRelease,
	role claudeReleaseSelectorRole,
	document claudeReleaseSelectorDocument,
	requireApproval bool,
) (ResolvedClaudeReleaseSelection, error) {
	if document.Kind != "release" || !receiptSHA256(document.ReleaseSHA256) ||
		strings.TrimSpace(document.Changeset) == "" || document.Deployment != nil {
		return ResolvedClaudeReleaseSelection{}, fmt.Errorf("Claude %s selector 非法", role)
	}
	release, ok := releases[document.ReleaseSHA256]
	if !ok {
		return ResolvedClaudeReleaseSelection{}, fmt.Errorf("Claude %s 引用了未知 Release", role)
	}
	selection := ResolvedClaudeReleaseSelection{
		role: role, changeset: strings.TrimSpace(document.Changeset), release: release,
	}
	if requireApproval {
		if document.Approval == nil {
			return ResolvedClaudeReleaseSelection{}, errors.New("Claude production active 缺少 Approval")
		}
		raw, err := readClaudeCatalogApproval(catalogFS, *document.Approval)
		if err != nil {
			return ResolvedClaudeReleaseSelection{}, err
		}
		selection.approvalPath = document.Approval.Path
		selection.approvalDigest = document.Approval.SHA256
		selection.approvalRaw = raw
	} else if document.Approval != nil {
		return ResolvedClaudeReleaseSelection{}, errors.New("Claude validation candidate 不得携带生产批准")
	}
	return selection, nil
}

func resolveClaudeOperationalRollback(
	document claudeReleaseSelectorDocument,
) (ResolvedClaudeOperationalRollback, error) {
	if document.Kind != "operational-deployment" || document.ReleaseSHA256 != "" ||
		document.Changeset != "" || document.Approval != nil || document.Deployment == nil {
		return ResolvedClaudeOperationalRollback{}, errors.New("Claude production rollback 必须是明确操作部署")
	}
	deployment := document.Deployment
	if !claudeCommitPattern.MatchString(deployment.Commit) ||
		!strings.HasPrefix(deployment.ImageDigest, "sha256:") ||
		!receiptSHA256(strings.TrimPrefix(deployment.ImageDigest, "sha256:")) ||
		path.Clean(deployment.Receipt.Path) != deployment.Receipt.Path ||
		!strings.HasPrefix(deployment.Receipt.Path, "docs/egress/maintenance/") ||
		!receiptSHA256(deployment.Receipt.SHA256) {
		return ResolvedClaudeOperationalRollback{}, errors.New("Claude operational rollback 坐标不完整")
	}
	return ResolvedClaudeOperationalRollback{
		commit: deployment.Commit, imageDigest: deployment.ImageDigest,
		receiptPath: deployment.Receipt.Path, receiptDigest: deployment.Receipt.SHA256,
	}, nil
}

func validateClaudeCatalogArtifactIdentities(release ResolvedClaudeRelease) error {
	var profile struct {
		Identity struct {
			Version                      string   `json:"version"`
			Platform                     string   `json:"platform"`
			SupportedModels              []string `json:"supported_models"`
			ModelCapabilityCatalogSHA256 string   `json:"model_capability_catalog_sha256"`
		} `json:"identity"`
		Rules           []json.RawMessage `json:"rules"`
		StrictEndpoints []json.RawMessage `json:"strict_endpoints"`
	}
	if err := json.Unmarshal(release.profileRaw, &profile); err != nil ||
		profile.Identity.Version != release.version || profile.Identity.Platform != release.platform ||
		!slices.Equal(profile.Identity.SupportedModels, release.models) ||
		len(profile.Rules) != release.requiredRuleCount ||
		len(profile.StrictEndpoints) != release.strictEndpointCount {
		return errors.New("Claude Catalog 与 Profile 身份不一致")
	}
	var wire struct {
		Identity struct {
			Version                      string `json:"version"`
			Platform                     string `json:"platform"`
			ProfileDigest                string `json:"profile_digest"`
			ModelCapabilityCatalogDigest string `json:"model_capability_catalog_digest"`
		} `json:"identity"`
		ImplementationPolicy struct {
			ModelCatalog struct {
				RequiredRuleCount int `json:"required_rule_count"`
				Models            []struct {
					CanonicalModel string `json:"canonical_model"`
				} `json:"models"`
			} `json:"model_catalog"`
		} `json:"implementation_policy"`
	}
	if err := json.Unmarshal(release.wireRaw, &wire); err != nil ||
		wire.Identity.Version != release.version || wire.Identity.Platform != release.platform ||
		wire.Identity.ProfileDigest != release.profileDigest ||
		wire.Identity.ModelCapabilityCatalogDigest !=
			profile.Identity.ModelCapabilityCatalogSHA256 ||
		!receiptSHA256(profile.Identity.ModelCapabilityCatalogSHA256) ||
		wire.ImplementationPolicy.ModelCatalog.RequiredRuleCount != release.requiredRuleCount {
		return errors.New("Claude Catalog 与 Wire 身份不一致")
	}
	wireModels := make([]string, 0, len(wire.ImplementationPolicy.ModelCatalog.Models))
	for _, model := range wire.ImplementationPolicy.ModelCatalog.Models {
		wireModels = append(wireModels, model.CanonicalModel)
	}
	if !slices.Equal(wireModels, release.models) {
		return errors.New("Claude Catalog 与 Wire 模型闭集不一致")
	}
	releaseDigest, err := claudeCatalogCanonicalDigest(map[string]any{
		"schema_version":                  "claude-code-fw-g-model-capability-release/v1",
		"version":                         release.version,
		"platform":                        release.platform,
		"profile_sha256":                  release.profileDigest,
		"wire_sha256":                     release.wireDigest,
		"model_capability_catalog_sha256": profile.Identity.ModelCapabilityCatalogSHA256,
		"required_rules_manifest_sha256":  release.requiredRulesManifestDigest,
		"required_rule_count":             release.requiredRuleCount,
	})
	if err != nil || releaseDigest != release.releaseDigest {
		return errors.New("Claude Catalog ReleaseArtifact 摘要不一致")
	}
	bundleDigest, err := claudeCatalogCanonicalDigest(map[string]any{
		"schema_version": "claude-code-fw-g-model-capability-bundle/v1",
		"persona":        "claude-code-oauth",
		"release_sha256": release.releaseDigest,
		"profile_sha256": release.profileDigest,
		"wire_sha256":    release.wireDigest,
	})
	if err != nil || bundleDigest != release.bundleDigest {
		return errors.New("Claude Catalog ReleaseBundle 摘要不一致")
	}
	return nil
}

func claudeCatalogCanonicalDigest(document map[string]any) (string, error) {
	raw, err := json.Marshal(document)
	if err != nil {
		return "", err
	}
	raw = append(raw, '\n')
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func validClaudeCatalogStringSet(values []string) bool {
	seen := make(map[string]struct{}, len(values))
	for _, value := range values {
		if strings.TrimSpace(value) != value || value == "" {
			return false
		}
		if _, duplicate := seen[value]; duplicate {
			return false
		}
		seen[value] = struct{}{}
	}
	return true
}

func (c ClaudeReleaseCatalog) ValidationCandidate() ResolvedClaudeReleaseSelection {
	return c.validationCandidate
}

func (c ClaudeReleaseCatalog) ProductionActive() ResolvedClaudeReleaseSelection {
	return c.productionActive
}

func (c ClaudeReleaseCatalog) ProductionRollback() ResolvedClaudeOperationalRollback {
	return c.productionRollback
}

func (c ClaudeReleaseCatalog) Digest() string { return c.digest }
func (c ClaudeReleaseCatalog) Source() string { return c.source }

var loadDefaultClaudeReleaseCatalog = sync.OnceValues(LoadEmbeddedClaudeReleaseCatalog)

func DefaultClaudeReleaseCatalog() ClaudeReleaseCatalog {
	catalog, err := loadDefaultClaudeReleaseCatalog()
	if err != nil {
		panic(err)
	}
	return catalog
}
