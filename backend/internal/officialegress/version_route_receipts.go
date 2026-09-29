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
	"slices"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
)

// versionRouteReceiptFS 保存“旧画像没有、新画像新增”的 route 迁移收据。
// 历史 MigrationReceipt 保持逐字节不变；本清单只以 prior_receipt_digest 追加授权，
// 不回写既有 canary/enforced 收据，也不声称旧画像具备不存在的 endpoint。
//
// 清单顺序即追加顺序：不同 Sink 按 SinkID 升序分组；同一 Sink 的多条收据按追加先后
// 排列，后一条的 prior_receipt_digest 必须接上前一条应用后的收据摘要。
//
//go:embed catalogdata/version-route-migration-receipts.json catalogdata/version-route-migration-artifacts
var versionRouteReceiptFS embed.FS

const versionRouteFrozenProfileDirectory = "catalogdata/version-route-migration-artifacts/frozen-profiles"

type versionRouteReceiptManifest struct {
	SchemaVersion   int                      `json:"schema_version"`
	BootstrapCommit string                   `json:"bootstrap_commit"`
	Receipts        []versionRouteReceiptDoc `json:"receipts"`
}

type versionRouteReceiptDoc struct {
	SinkID             string                     `json:"sink_id"`
	PriorReceiptDigest string                     `json:"prior_receipt_digest"`
	BindingDigest      string                     `json:"binding_digest"`
	Route              receiptcontract.RouteProof `json:"route"`
	ProfileDigests     []string                   `json:"profile_digests"`
	Source             amendmentSourceEvidence    `json:"source"`
	// CanaryAcceptance 是生产 canary 实测验收产物的引用。候选期收据只准备 wire fixture 与
	// execution verification，本字段缺省（候选态）；生产 canary 必须逐项绑定本收据的
	// sink_id、route、profile_digests、wire_fixture.sha256 与 execution_verification.sha256
	// 实测后才能补齐，禁止用合成占位代替实测验收。字段存在时照旧严格校验。
	CanaryAcceptance *receiptcontract.ArtifactRef `json:"canary_acceptance,omitempty"`
	ReviewedBy       string                       `json:"reviewed_by"`
	ReviewRef        string                       `json:"review_ref"`
	Rationale        string                       `json:"rationale"`
}

type versionRouteExecutionVerification struct {
	SchemaVersion      int                           `json:"schema_version"`
	Result             string                        `json:"result"`
	SinkID             string                        `json:"sink_id"`
	Route              receiptcontract.RouteIdentity `json:"route"`
	AuthorityKind      receiptcontract.AuthorityKind `json:"authority_kind"`
	AuthorityID        string                        `json:"authority_id"`
	TokenIssuerID      string                        `json:"token_issuer_id"`
	EvidenceKind       string                        `json:"evidence_kind"`
	EvidenceID         string                        `json:"evidence_id"`
	Backend            string                        `json:"backend"`
	AdapterID          string                        `json:"adapter_id"`
	TransportID        string                        `json:"transport_id"`
	WireSHA256         string                        `json:"wire_fixture_sha256"`
	ProfileDigests     []string                      `json:"profile_digests"`
	TerminalGuardAllow bool                          `json:"terminal_guard_allow"`
	ExternalTraffic    bool                          `json:"external_traffic"`
}

type versionRouteCanaryAcceptance struct {
	SchemaVersion               int                           `json:"schema_version"`
	Result                      string                        `json:"result"`
	SinkID                      string                        `json:"sink_id"`
	Route                       receiptcontract.RouteIdentity `json:"route"`
	ProfileDigests              []string                      `json:"profile_digests"`
	WireFixtureSHA256           string                        `json:"wire_fixture_sha256"`
	ExecutionVerificationSHA256 string                        `json:"execution_verification_sha256"`
	ObservationDigest           string                        `json:"observation_digest"`
	ExternalTraffic             bool                          `json:"external_traffic"`
	ReviewedBy                  string                        `json:"reviewed_by"`
	ReviewRef                   string                        `json:"review_ref"`
}

func loadVersionRouteReceiptManifest() (versionRouteReceiptManifest, error) {
	raw, err := versionRouteReceiptFS.ReadFile("catalogdata/version-route-migration-receipts.json")
	if err != nil {
		return versionRouteReceiptManifest{}, err
	}
	var manifest versionRouteReceiptManifest
	if err := decodeVersionRouteStrict(raw, &manifest); err != nil {
		return versionRouteReceiptManifest{}, err
	}
	if manifest.SchemaVersion != 1 || manifest.BootstrapCommit != BootstrapCommit {
		return versionRouteReceiptManifest{}, errors.New("版本 route 收据 schema/bootstrap 非法")
	}
	if err := validateVersionRouteReceiptOrder(manifest.Receipts); err != nil {
		return versionRouteReceiptManifest{}, err
	}
	for _, document := range manifest.Receipts {
		if err := validateVersionRouteReceiptShape(document); err != nil {
			return versionRouteReceiptManifest{}, err
		}
	}
	return manifest, nil
}

// validateVersionRouteReceiptOrder 只校验清单的分组与唯一性：SinkID 必须非递减（同一 Sink
// 的收据连续出现），同一 Sink 不得重复登记同一 route。同一 Sink 内的先后就是追加顺序，
// 不按 route 字典序排列——后追加的 route 可能字典序更小，其 prior_receipt_digest 链由
// applyVersionRouteReceipts 逐条校验，顺序颠倒会在那里失败关闭。
func validateVersionRouteReceiptOrder(receipts []versionRouteReceiptDoc) error {
	previousSink := ""
	seen := make(map[string]bool, len(receipts))
	for _, document := range receipts {
		if document.SinkID < previousSink {
			return errors.New("版本 route 收据必须按 SinkID 分组升序排列")
		}
		previousSink = document.SinkID
		identity := document.SinkID + "\x00" + document.Route.Route.Identity()
		if seen[identity] {
			return fmt.Errorf("版本 route 收据重复登记同一 route: %s", document.SinkID)
		}
		seen[identity] = true
	}
	return nil
}

func validateVersionRouteReceiptShape(document versionRouteReceiptDoc) error {
	if strings.TrimSpace(document.SinkID) == "" || !receiptcontract.ValidSHA256(document.PriorReceiptDigest) ||
		!receiptcontract.ValidSHA256(document.BindingDigest) || len(document.ProfileDigests) == 0 ||
		strings.TrimSpace(document.ReviewedBy) == "" || strings.TrimSpace(document.ReviewRef) == "" ||
		strings.TrimSpace(document.Rationale) == "" {
		return fmt.Errorf("版本 route 收据字段不完整: %s", document.SinkID)
	}
	if err := validateAmendmentSource(document.Source); err != nil {
		return fmt.Errorf("版本 route 收据 source 非法: %s: %w", document.SinkID, err)
	}
	route := document.Route.Route
	if strings.ToUpper(strings.TrimSpace(route.Method)) != route.Method || route.Method == "" ||
		strings.TrimSpace(route.Host) == "" || !strings.HasPrefix(route.Path, "/") ||
		strings.TrimSpace(route.Purpose) == "" || strings.TrimSpace(route.Protocol) == "" ||
		strings.TrimSpace(document.Route.EvidenceKind) == "" || strings.TrimSpace(document.Route.EvidenceID) == "" ||
		strings.TrimSpace(document.Route.Backend) == "" || strings.TrimSpace(document.Route.AdapterID) == "" ||
		strings.TrimSpace(document.Route.TransportID) == "" {
		return fmt.Errorf("版本 route 收据 proof 字段不完整: %s", document.SinkID)
	}
	if err := validateVersionRouteArtifactRef(document.Route.WireFixture); err != nil {
		return err
	}
	if err := validateVersionRouteArtifactRef(document.Route.ExecutionVerification); err != nil {
		return err
	}
	if document.CanaryAcceptance != nil {
		if err := validateVersionRouteArtifactRef(*document.CanaryAcceptance); err != nil {
			return err
		}
	}
	if !sortedUniqueSHA256(document.ProfileDigests) {
		return fmt.Errorf("版本 route 收据 profile_digests 非法: %s", document.SinkID)
	}
	return nil
}

func validateVersionRouteArtifactRef(ref receiptcontract.ArtifactRef) error {
	cleaned := path.Clean(strings.TrimSpace(ref.Path))
	if cleaned == "." || cleaned != ref.Path || strings.HasPrefix(cleaned, "/") ||
		cleaned == ".." || strings.HasPrefix(cleaned, "../") ||
		!receiptcontract.ValidSHA256(ref.SHA256) ||
		!strings.HasPrefix(cleaned, "catalogdata/version-route-migration-artifacts/") {
		return errors.New("版本 route 收据产物路径或摘要非法")
	}
	return nil
}

func sortedUniqueSHA256(values []string) bool {
	previous := ""
	for _, value := range values {
		if !receiptcontract.ValidSHA256(value) || previous >= value {
			return false
		}
		previous = value
	}
	return true
}

func prepareVersionRouteReceiptInputs(
	manifest versionRouteReceiptManifest,
	evidenceBySink map[string]bindingcontract.ReleaseBindingDoc,
	inputs []SinkBindingInput,
) ([]SinkBindingInput, error) {
	indexBySink := make(map[string]int, len(inputs))
	for index := range inputs {
		indexBySink[string(inputs[index].ID)] = index
	}
	for _, document := range manifest.Receipts {
		index, ok := indexBySink[document.SinkID]
		if !ok {
			return nil, fmt.Errorf("版本 route 收据引用未知 SinkID: %s", document.SinkID)
		}
		bindingEvidence, ok := evidenceBySink[document.SinkID]
		if !ok || !amendmentSourceMatchesBinding(document.Source, bindingEvidence) {
			return nil, fmt.Errorf("版本 route 收据源码证据不匹配: %s", document.SinkID)
		}
		route := versionRouteCatalogRoute(document.Route.Route)
		if err := route.Validate(); err != nil || route.Key.Purpose != inputs[index].Purpose {
			return nil, fmt.Errorf("版本 route 收据 route 非法: %s", document.SinkID)
		}
		matches := 0
		for _, existing := range inputs[index].Routes {
			if catalogRouteIdentity(existing) == catalogRouteIdentity(route) {
				matches++
			}
		}
		if matches != 0 {
			return nil, fmt.Errorf(
				"版本 route 收据只能追加历史 binding 中不存在的 route: %s", document.SinkID,
			)
		}
	}
	return inputs, nil
}

func applyVersionRouteReceipts(
	manifest versionRouteReceiptManifest,
	evidenceBySink map[string]bindingcontract.ReleaseBindingDoc,
	inputs []SinkBindingInput,
) ([]SinkBindingInput, error) {
	indexBySink := make(map[string]int, len(inputs))
	for index := range inputs {
		indexBySink[string(inputs[index].ID)] = index
	}
	for _, document := range manifest.Receipts {
		index, ok := indexBySink[document.SinkID]
		if !ok {
			return nil, fmt.Errorf("版本 route 收据引用未知 SinkID: %s", document.SinkID)
		}
		bindingEvidence, ok := evidenceBySink[document.SinkID]
		if !ok || !amendmentSourceMatchesBinding(document.Source, bindingEvidence) {
			return nil, fmt.Errorf("版本 route 收据源码证据不匹配: %s", document.SinkID)
		}
		input := &inputs[index]
		if input.EnforcementState != SinkStateEnforced || input.migrationReceipt == nil ||
			input.migrationReceipt.digest != document.PriorReceiptDigest {
			return nil, fmt.Errorf("版本 route 收据未连接既有 enforced 收据: %s", document.SinkID)
		}
		route := versionRouteCatalogRoute(document.Route.Route)
		if err := route.Validate(); err != nil || route.Key.Purpose != input.Purpose {
			return nil, fmt.Errorf("版本 route 收据 route 非法: %s", document.SinkID)
		}
		for _, existing := range input.Routes {
			if catalogRouteIdentity(existing) == catalogRouteIdentity(route) {
				return nil, fmt.Errorf("版本 route 收据重复登记 route: %s", document.SinkID)
			}
		}
		// 历史版本 route 收据只复核其签发时冻结的画像集合。当前 Active/Previous
		// 是否仍能覆盖该 route，由 NewBundleResolver 的跨画像门禁独立校验；不能因
		// 候选版本进入 Previous 就反向要求覆盖或改写既有只读收据。
		binding, profileDigests, err := resolveVersionRouteBindingForProfiles(
			*input, route, document.ProfileDigests,
		)
		if err != nil {
			return nil, err
		}
		if !slices.Equal(profileDigests, document.ProfileDigests) ||
			document.Route.EvidenceKind != "codex_endpoint" ||
			document.Route.EvidenceID != binding.EndpointID() ||
			document.Route.Backend != string(input.TargetBackend) ||
			document.Route.AdapterID != string(adapterForBackend(input.TargetBackend)) {
			return nil, fmt.Errorf("版本 route 收据未绑定真实 EndpointBinding: %s", document.SinkID)
		}
		if document.CanaryAcceptance == nil {
			if err := requireVersionRouteCandidateProfiles(document); err != nil {
				return nil, err
			}
		}
		if err := verifyVersionRouteReceiptArtifacts(
			versionRouteReceiptFS, document,
			input.migrationReceipt.authorityKind,
			string(input.migrationReceipt.authorityID),
			string(input.migrationReceipt.tokenIssuerID),
		); err != nil {
			return nil, err
		}
		input.Routes = append(input.Routes, route)
		sort.Slice(input.Routes, func(i, j int) bool {
			return catalogRouteIdentity(input.Routes[i]) < catalogRouteIdentity(input.Routes[j])
		})
		bindingDigest, err := sinkBindingIdentityDigest(*input)
		if err != nil || bindingDigest != document.BindingDigest {
			return nil, fmt.Errorf("版本 route 收据 binding digest 不匹配: %s", document.SinkID)
		}
		receipt := *input.migrationReceipt
		receipt.bindingDigest = bindingDigest
		receipt.routeClaims = append(receipt.routeClaims, migrationRouteClaim{
			route: route, evidenceKind: document.Route.EvidenceKind,
			evidenceID: document.Route.EvidenceID, backend: BackendKind(document.Route.Backend),
			adapterID: AdapterID(document.Route.AdapterID), transportID: document.Route.TransportID,
		})
		sort.Slice(receipt.routeClaims, func(i, j int) bool {
			return catalogRouteIdentity(receipt.routeClaims[i].route) <
				catalogRouteIdentity(receipt.routeClaims[j].route)
		})
		raw, err := json.Marshal(document)
		if err != nil {
			return nil, err
		}
		documentDigest := sha256.Sum256(raw)
		combined := sha256.Sum256([]byte(receipt.digest + "\x00" + hex.EncodeToString(documentDigest[:])))
		receipt.digest = hex.EncodeToString(combined[:])
		input.migrationReceipt = &receipt
		if !receipt.validFor(*input) {
			return nil, fmt.Errorf("版本 route 收据扩展后未形成完整 enforced binding: %s", document.SinkID)
		}
	}
	return inputs, nil
}

func versionRouteCatalogRoute(identity receiptcontract.RouteIdentity) CatalogRoute {
	return CatalogRoute{Key: RouteKey{
		Method: identity.Method, Host: identity.Host,
		Path: identity.Path, Purpose: Purpose(identity.Purpose),
	}, Protocol: WireProtocol(identity.Protocol)}
}

// requireVersionRouteCandidateProfiles 限定候选态收据（尚无生产 canary acceptance）只能
// 绑定当前 Active／Previous 发布的画像。冻结历史画像只为已验收收据在旧画像退休后仍可
// 自校验而保留；目标画像退出运行目录时若仍未补齐 canary acceptance 即失败关闭，避免
// 候选态收据长期充当已验收收据。
func requireVersionRouteCandidateProfiles(document versionRouteReceiptDoc) error {
	runtimeProfiles := make(map[string]bool, 2)
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			return err
		}
		runtimeProfiles[release.ProfileDigest()] = true
	}
	for _, profileDigest := range document.ProfileDigests {
		if !runtimeProfiles[profileDigest] {
			return fmt.Errorf(
				"候选态版本 route 收据只能绑定当前 Active/Previous 画像: %s: %s",
				document.SinkID, profileDigest,
			)
		}
	}
	return nil
}

// resolveVersionRouteBinding 供收据生成器使用：只收集实际包含该 route 的 Active／Previous
// 画像。版本新增 route 允许在不含该端点的单个 Release 中零匹配（候选期目标画像只在
// previous），但两侧都不含时失败关闭。
func resolveVersionRouteBinding(
	input SinkBindingInput,
	route CatalogRoute,
) (EndpointBinding, []string, error) {
	sinks, physical, sink, err := versionRouteTemporaryBinding(input, route)
	if err != nil {
		return EndpointBinding{}, nil, err
	}
	profiles := make([]string, 0, 2)
	seenProfiles := make(map[string]bool)
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			return EndpointBinding{}, nil, err
		}
		if seenProfiles[release.ProfileDigest()] {
			continue
		}
		seenProfiles[release.ProfileDigest()] = true
		bindings, err := NewEndpointBindingCatalog(sinks, physical, release.ExecutableProfile())
		if err != nil {
			return EndpointBinding{}, nil, err
		}
		if _, present := bindings.ResolveBindingRoute(sink, route, physical); !present {
			continue
		}
		profiles = append(profiles, release.ProfileDigest())
	}
	if len(profiles) == 0 {
		return EndpointBinding{}, nil, fmt.Errorf("版本 route 在 Active/Previous 中均无 EndpointBinding: %s", input.ID)
	}
	sort.Strings(profiles)
	return resolveVersionRouteBindingForProfiles(input, route, profiles)
}

// versionRouteTemporaryBinding 构造只含目标 Sink 且已并入待追加 route 的临时
// legacy_observe 视图。收据校验发生在状态提升之前，用临时视图解析 EndpointBinding，
// 避免 EndpointBinding 与收据形成构造循环。
func versionRouteTemporaryBinding(
	input SinkBindingInput,
	route CatalogRoute,
) (SinkCatalog, PhysicalRouteCatalog, SinkBinding, error) {
	temporary := input
	temporary.Routes = append(append([]CatalogRoute(nil), input.Routes...), route)
	temporary.EnforcementState = SinkStateLegacyObserve
	temporary.migrationReceipt = nil
	sinks, err := NewSinkCatalog([]SinkBindingInput{temporary})
	if err != nil {
		return SinkCatalog{}, PhysicalRouteCatalog{}, SinkBinding{}, err
	}
	physical, err := NewPhysicalRouteCatalog(sinks)
	if err != nil {
		return SinkCatalog{}, PhysicalRouteCatalog{}, SinkBinding{}, err
	}
	sink, ok := sinks.Resolve(input.ID)
	if !ok {
		return SinkCatalog{}, PhysicalRouteCatalog{}, SinkBinding{}, fmt.Errorf(
			"版本 route 临时 SinkCatalog 缺少 %s", input.ID,
		)
	}
	return sinks, physical, sink, nil
}

func resolveVersionRouteBindingForProfiles(
	input SinkBindingInput,
	route CatalogRoute,
	profileDigests []string,
) (EndpointBinding, []string, error) {
	sinks, physical, sink, err := versionRouteTemporaryBinding(input, route)
	if err != nil {
		return EndpointBinding{}, nil, err
	}
	var resolved EndpointBinding
	resolvedProfiles := make([]string, 0, len(profileDigests))
	releaseCatalog := DefaultReleaseCatalog()
	for _, profileDigest := range profileDigests {
		matched := false
		for _, key := range releaseCatalog.snapshots.Keys() {
			if key.Digest != profileDigest {
				continue
			}
			if matched {
				return EndpointBinding{}, nil, fmt.Errorf("版本 route 画像摘要不唯一: %s", profileDigest)
			}
			executable, ok := releaseCatalog.snapshots.ResolveExecutable(key)
			if !ok {
				return EndpointBinding{}, nil, fmt.Errorf("版本 route 缺少可执行画像: %s", profileDigest)
			}
			bindings, bindingErr := NewEndpointBindingCatalog(sinks, physical, executable)
			if bindingErr != nil {
				return EndpointBinding{}, nil, bindingErr
			}
			candidate, present := bindings.ResolveBindingRoute(sink, route, physical)
			if !present {
				return EndpointBinding{}, nil, fmt.Errorf(
					"版本 route 在冻结画像中无 EndpointBinding: %s: %s", input.ID, profileDigest,
				)
			}
			if len(resolvedProfiles) > 0 && (candidate.EndpointID() != resolved.EndpointID() ||
				candidate.ReleasePurpose() != resolved.ReleasePurpose()) {
				return EndpointBinding{}, nil, fmt.Errorf("版本 route 在不同画像中连接到不同 EndpointBinding: %s", input.ID)
			}
			resolved = candidate
			matched = true
		}
		if !matched {
			executable, archived, archiveErr := loadVersionRouteFrozenProfile(profileDigest)
			if archiveErr != nil {
				return EndpointBinding{}, nil, archiveErr
			}
			if archived {
				bindings, bindingErr := NewEndpointBindingCatalog(sinks, physical, executable)
				if bindingErr != nil {
					return EndpointBinding{}, nil, bindingErr
				}
				candidate, present := bindings.ResolveBindingRoute(sink, route, physical)
				if !present {
					return EndpointBinding{}, nil, fmt.Errorf(
						"版本 route 在冻结历史画像中无 EndpointBinding: %s: %s", input.ID, profileDigest,
					)
				}
				if len(resolvedProfiles) > 0 && (candidate.EndpointID() != resolved.EndpointID() ||
					candidate.ReleasePurpose() != resolved.ReleasePurpose()) {
					return EndpointBinding{}, nil, fmt.Errorf("版本 route 在不同画像中连接到不同 EndpointBinding: %s", input.ID)
				}
				resolved = candidate
				matched = true
			}
		}
		if !matched {
			return EndpointBinding{}, nil, fmt.Errorf("版本 route 引用未知画像摘要: %s", profileDigest)
		}
		resolvedProfiles = append(resolvedProfiles, profileDigest)
	}
	sort.Strings(resolvedProfiles)
	if len(resolvedProfiles) == 0 {
		return EndpointBinding{}, nil, fmt.Errorf("版本 route 未指定冻结画像: %s", input.ID)
	}
	return resolved, resolvedProfiles, nil
}

// loadVersionRouteFrozenProfile 只加载历史 version-route 收据冻结的证明画像。
// 文件不属于 releaseCatalogFS，因而不能进入 Active／Previous 或被生产 selector
// 选中；其唯一用途是让不可改写的历史收据在旧运行画像退休后仍可自校验。
func loadVersionRouteFrozenProfile(
	profileDigest string,
) (profilecontract.ExecutableProfile, bool, error) {
	if !receiptcontract.ValidSHA256(profileDigest) {
		return profilecontract.ExecutableProfile{}, false, errors.New("版本 route 冻结画像摘要非法")
	}
	profilePath := path.Join(versionRouteFrozenProfileDirectory, profileDigest+".json")
	raw, err := versionRouteReceiptFS.ReadFile(profilePath)
	if errors.Is(err, fs.ErrNotExist) {
		return profilecontract.ExecutableProfile{}, false, nil
	}
	if err != nil {
		return profilecontract.ExecutableProfile{}, false, err
	}
	snapshot, err := profilecontract.ParseSnapshot(raw)
	if err != nil {
		return profilecontract.ExecutableProfile{}, false, fmt.Errorf("解析版本 route 冻结画像: %w", err)
	}
	computedDigest, err := profilecontract.OfficialSnapshotDigest(snapshot)
	if err != nil {
		return profilecontract.ExecutableProfile{}, false, fmt.Errorf("计算版本 route 冻结画像摘要: %w", err)
	}
	if computedDigest != profileDigest || snapshot.Digest != profileDigest {
		return profilecontract.ExecutableProfile{}, false, errors.New("版本 route 冻结画像内容摘要不一致")
	}
	profile, err := profilecontract.NewProfileSpec(snapshot)
	if err != nil {
		return profilecontract.ExecutableProfile{}, false, fmt.Errorf("构造版本 route 冻结画像: %w", err)
	}
	executable, err := profilecontract.CompileExecutableProfile(profile)
	if err != nil {
		return profilecontract.ExecutableProfile{}, false, fmt.Errorf("编译版本 route 冻结画像: %w", err)
	}
	return executable, true, nil
}

func verifyVersionRouteReceiptArtifacts(
	files fs.FS,
	document versionRouteReceiptDoc,
	authorityKind receiptcontract.AuthorityKind,
	authorityID string,
	tokenIssuerID string,
) error {
	wireRaw, err := readVersionRouteArtifact(files, document.Route.WireFixture)
	if err != nil {
		return err
	}
	executionRaw, err := readVersionRouteArtifact(files, document.Route.ExecutionVerification)
	if err != nil {
		return err
	}
	var execution versionRouteExecutionVerification
	if err := decodeVersionRouteStrict(executionRaw, &execution); err != nil {
		return err
	}
	wireDigest := sha256.Sum256(wireRaw)
	if execution.SchemaVersion != 1 || execution.Result != "passed" ||
		execution.SinkID != document.SinkID || execution.Route != document.Route.Route ||
		execution.AuthorityKind != receiptcontract.AuthorityCodexExecutor ||
		execution.AuthorityKind != authorityKind || execution.AuthorityID != authorityID ||
		execution.TokenIssuerID != tokenIssuerID ||
		execution.EvidenceKind != document.Route.EvidenceKind || execution.EvidenceID != document.Route.EvidenceID ||
		execution.Backend != document.Route.Backend || execution.AdapterID != document.Route.AdapterID ||
		execution.TransportID != document.Route.TransportID ||
		execution.WireSHA256 != hex.EncodeToString(wireDigest[:]) ||
		!slices.Equal(execution.ProfileDigests, document.ProfileDigests) ||
		!execution.TerminalGuardAllow || execution.ExternalTraffic {
		return fmt.Errorf("版本 route execution verification 与收据不一致: %s", document.SinkID)
	}
	if document.CanaryAcceptance == nil {
		// 候选态收据：生产 canary acceptance 尚未实测，只核验 wire fixture 与执行产物。
		return nil
	}
	acceptanceRaw, err := readVersionRouteArtifact(files, *document.CanaryAcceptance)
	if err != nil {
		return err
	}
	var acceptance versionRouteCanaryAcceptance
	if err := decodeVersionRouteStrict(acceptanceRaw, &acceptance); err != nil {
		return err
	}
	if acceptance.SchemaVersion != 1 || acceptance.Result != "accepted" ||
		acceptance.SinkID != document.SinkID || acceptance.Route != document.Route.Route ||
		!slices.Equal(acceptance.ProfileDigests, document.ProfileDigests) ||
		acceptance.WireFixtureSHA256 != document.Route.WireFixture.SHA256 ||
		acceptance.ExecutionVerificationSHA256 != document.Route.ExecutionVerification.SHA256 ||
		!receiptcontract.ValidSHA256(acceptance.ObservationDigest) || acceptance.ExternalTraffic ||
		strings.TrimSpace(acceptance.ReviewedBy) == "" || strings.TrimSpace(acceptance.ReviewRef) == "" {
		return fmt.Errorf("版本 route canary acceptance 与收据不一致: %s", document.SinkID)
	}
	return nil
}

func readVersionRouteArtifact(files fs.FS, ref receiptcontract.ArtifactRef) ([]byte, error) {
	raw, err := fs.ReadFile(files, ref.Path)
	if err != nil {
		return nil, err
	}
	digest := sha256.Sum256(raw)
	if hex.EncodeToString(digest[:]) != ref.SHA256 {
		return nil, fmt.Errorf("版本 route 收据产物摘要不匹配: %s", ref.Path)
	}
	return raw, nil
}

func decodeVersionRouteStrict(raw []byte, target any) error {
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(target); err != nil {
		return err
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return errors.New("版本 route 收据 JSON 尾部存在额外对象")
		}
		return err
	}
	return nil
}
