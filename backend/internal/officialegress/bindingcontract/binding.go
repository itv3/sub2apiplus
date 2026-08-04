// Package bindingcontract 承载变更集 0C 的发送面绑定证据。
//
// 本包只把 sink-baseline.json 已经记录的事实转换成不可变 ReleaseBinding：
// SinkID、purpose、persona、route、目标 backend、门禁状态与源码候选。它不会从
// 画像或函数名推导 RetryPolicy、TransportID、selector、CONNECT 画像或执行行为。
package bindingcontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
)

const BindingCatalogSchemaVersion = 1

var ErrTrailingData = errors.New("JSON 尾部存在额外数据")

// SinkBaselineDoc 精确映射 egressscan 生成的基线。严格解析用于在扫描器新增字段时
// 主动失败，防止绑定生成器悄悄忽略新证据。
type SinkBaselineDoc struct {
	BootstrapCommit string                     `json:"bootstrap_commit"`
	BuildContexts   []string                   `json:"build_contexts"`
	PackagesLoaded  int                        `json:"packages_loaded"`
	ScanPattern     string                     `json:"scan_pattern"`
	Sinks           []SinkBaselineCandidateDoc `json:"sinks"`
}

type SinkBaselineCandidateDoc struct {
	ScanCandidateID  string   `json:"scan_candidate_id"`
	File             string   `json:"file"`
	Func             string   `json:"func"`
	Package          string   `json:"package"`
	Callee           string   `json:"callee"`
	Receiver         string   `json:"receiver"`
	SinkKind         string   `json:"sink_kind"`
	Protocol         string   `json:"protocol"`
	SinkType         string   `json:"sink_type"`
	ASTFingerprint   string   `json:"ast_fingerprint"`
	TargetResolution string   `json:"target_resolution"`
	BuildContexts    []string `json:"build_contexts"`
	ResolvedHosts    []string `json:"resolved_hosts"`
	ResolvedMethods  []string `json:"resolved_methods"`
	ResolvedPaths    []string `json:"resolved_paths"`
	ResolvedTargets  []string `json:"resolved_targets"`
	OfficialHost     bool     `json:"official_host"`
	RuntimeSinkID    string   `json:"runtime_sink_id"`
	Purpose          string   `json:"purpose"`
	Persona          string   `json:"persona"`
	EndpointEvidence string   `json:"endpoint_evidence"`
	Routes           []string `json:"routes"`
	IsFacade         bool     `json:"is_facade"`
	Backend          string   `json:"backend"`
	TargetBackend    string   `json:"target_backend"`
	EnforcementState string   `json:"enforcement_state"`
	Owner            string   `json:"owner"`
	MigrationSet     string   `json:"migration_changeset"`
	ExpiryCondition  string   `json:"expiry_condition"`
	Rationale        string   `json:"rationale"`
	LineHint         int      `json:"line_hint"`
}

type BindingCatalogDoc struct {
	SchemaVersion int                 `json:"schema_version"`
	Source        BindingSourceDoc    `json:"source"`
	Bindings      []ReleaseBindingDoc `json:"bindings"`
}

type BindingSourceDoc struct {
	BaselineSHA256  string   `json:"baseline_sha256"`
	BootstrapCommit string   `json:"bootstrap_commit"`
	BuildContexts   []string `json:"build_contexts"`
	PackagesLoaded  int      `json:"packages_loaded"`
	ScanPattern     string   `json:"scan_pattern"`
	TotalCandidates int      `json:"total_candidates"`
	BoundCandidates int      `json:"bound_candidates"`
}

// ReleaseBindingDoc 是一个运行时业务 Sink 的静态证据。一个 Sink 可由多个 factory、
// facade 或 terminal 候选共同实现，也可对应多个 route。
type ReleaseBindingDoc struct {
	SinkID             string                `json:"sink_id"`
	Purpose            string                `json:"purpose"`
	Persona            string                `json:"persona"`
	EndpointEvidence   string                `json:"endpoint_evidence"`
	Routes             []RouteEvidenceDoc    `json:"routes"`
	TargetBackend      string                `json:"target_backend"`
	EnforcementState   string                `json:"enforcement_state"`
	Owner              string                `json:"owner"`
	MigrationChangeset string                `json:"migration_changeset"`
	ExpiryCondition    string                `json:"expiry_condition"`
	Candidates         []BindingCandidateDoc `json:"candidates"`
}

// RouteEvidenceDoc 同时保留扫描基线的原文和无损拆分值。拆分只解释固定语法，
// 不负责把业务 purpose 映射成发布图 purpose。
type RouteEvidenceDoc struct {
	Raw       string `json:"raw"`
	Method    string `json:"method"`
	Host      string `json:"host"`
	Path      string `json:"path"`
	Transport string `json:"transport"`
}

type BindingCandidateDoc struct {
	ScanCandidateID  string   `json:"scan_candidate_id"`
	ASTFingerprint   string   `json:"ast_fingerprint"`
	File             string   `json:"file"`
	Func             string   `json:"func"`
	Callee           string   `json:"callee"`
	SinkKind         string   `json:"sink_kind"`
	SinkType         string   `json:"sink_type"`
	Protocol         string   `json:"protocol"`
	EndpointEvidence string   `json:"endpoint_evidence"`
	ActualBackend    string   `json:"actual_backend"`
	OfficialHost     bool     `json:"official_host"`
	TargetResolution string   `json:"target_resolution"`
	BuildContexts    []string `json:"build_contexts"`
	ResolvedHosts    []string `json:"resolved_hosts"`
	ResolvedMethods  []string `json:"resolved_methods"`
	ResolvedPaths    []string `json:"resolved_paths"`
	ResolvedTargets  []string `json:"resolved_targets"`
	Rationale        string   `json:"rationale"`
}

func ParseSinkBaseline(raw []byte) (SinkBaselineDoc, error) {
	var doc SinkBaselineDoc
	if err := decodeStrict(raw, &doc); err != nil {
		return SinkBaselineDoc{}, fmt.Errorf("解析发送面基线: %w", err)
	}
	return doc, nil
}

func ParseBindingCatalog(raw []byte) (BindingCatalogDoc, error) {
	var doc BindingCatalogDoc
	if err := decodeStrict(raw, &doc); err != nil {
		return BindingCatalogDoc{}, fmt.Errorf("解析绑定目录: %w", err)
	}
	return doc, nil
}

func decodeStrict(raw []byte, out any) error {
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(out); err != nil {
		return err
	}
	if err := dec.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		if err == nil {
			return ErrTrailingData
		}
		return fmt.Errorf("%w: %v", ErrTrailingData, err)
	}
	return nil
}

// BuildBindingCatalogDoc 是从扫描基线生成绑定证据的唯一入口。
func BuildBindingCatalogDoc(raw []byte) (BindingCatalogDoc, error) {
	baseline, err := ParseSinkBaseline(raw)
	if err != nil {
		return BindingCatalogDoc{}, err
	}
	if baseline.BootstrapCommit == "" || len(baseline.BuildContexts) == 0 ||
		baseline.PackagesLoaded <= 0 || baseline.ScanPattern == "" {
		return BindingCatalogDoc{}, errors.New("发送面基线元数据不完整")
	}
	if len(baseline.Sinks) == 0 {
		return BindingCatalogDoc{}, errors.New("发送面基线没有候选")
	}

	groups := make(map[string][]SinkBaselineCandidateDoc)
	seenCandidates := make(map[string]struct{}, len(baseline.Sinks))
	boundCount := 0
	for _, candidate := range baseline.Sinks {
		if candidate.ScanCandidateID == "" {
			return BindingCatalogDoc{}, errors.New("存在空 scan_candidate_id")
		}
		if _, exists := seenCandidates[candidate.ScanCandidateID]; exists {
			return BindingCatalogDoc{}, fmt.Errorf("scan_candidate_id 重复: %s", candidate.ScanCandidateID)
		}
		seenCandidates[candidate.ScanCandidateID] = struct{}{}

		inBindingScope := isBindingPersona(candidate.Persona)
		if inBindingScope && candidate.RuntimeSinkID == "" {
			return BindingCatalogDoc{}, fmt.Errorf("候选 %s 的 persona=%s 但缺少 runtime_sink_id", candidate.ScanCandidateID, candidate.Persona)
		}
		if !inBindingScope && candidate.RuntimeSinkID != "" {
			return BindingCatalogDoc{}, fmt.Errorf("候选 %s 的 persona=%s 不应声明 runtime_sink_id", candidate.ScanCandidateID, candidate.Persona)
		}
		if candidate.RuntimeSinkID == "" {
			continue
		}
		if err := validateBoundCandidate(candidate); err != nil {
			return BindingCatalogDoc{}, err
		}
		groups[candidate.RuntimeSinkID] = append(groups[candidate.RuntimeSinkID], candidate)
		boundCount++
	}

	ids := make([]string, 0, len(groups))
	for id := range groups {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	bindings := make([]ReleaseBindingDoc, 0, len(ids))
	for _, id := range ids {
		binding, err := aggregateBinding(id, groups[id])
		if err != nil {
			return BindingCatalogDoc{}, err
		}
		bindings = append(bindings, binding)
	}

	sum := sha256.Sum256(raw)
	return BindingCatalogDoc{
		SchemaVersion: BindingCatalogSchemaVersion,
		Source: BindingSourceDoc{
			BaselineSHA256:  hex.EncodeToString(sum[:]),
			BootstrapCommit: baseline.BootstrapCommit,
			BuildContexts:   cloneStrings(baseline.BuildContexts),
			PackagesLoaded:  baseline.PackagesLoaded,
			ScanPattern:     baseline.ScanPattern,
			TotalCandidates: len(baseline.Sinks),
			BoundCandidates: boundCount,
		},
		Bindings: bindings,
	}, nil
}

func isBindingPersona(persona string) bool {
	switch persona {
	case "codex-cli", "chatgpt-web", "unclassified", "dead-code":
		return true
	default:
		return false
	}
}

func validateBoundCandidate(candidate SinkBaselineCandidateDoc) error {
	required := map[string]string{
		"purpose":             candidate.Purpose,
		"persona":             candidate.Persona,
		"endpoint_evidence":   candidate.EndpointEvidence,
		"target_backend":      candidate.TargetBackend,
		"enforcement_state":   candidate.EnforcementState,
		"owner":               candidate.Owner,
		"migration_changeset": candidate.MigrationSet,
		"expiry_condition":    candidate.ExpiryCondition,
		"ast_fingerprint":     candidate.ASTFingerprint,
		"file":                candidate.File,
		"func":                candidate.Func,
		"callee":              candidate.Callee,
		"sink_kind":           candidate.SinkKind,
		"sink_type":           candidate.SinkType,
		"protocol":            candidate.Protocol,
		"backend":             candidate.Backend,
		"target_resolution":   candidate.TargetResolution,
	}
	for name, value := range required {
		if value == "" {
			return fmt.Errorf("候选 %s 缺少 %s", candidate.ScanCandidateID, name)
		}
	}
	if len(candidate.BuildContexts) == 0 {
		return fmt.Errorf("候选 %s 缺少 build_contexts", candidate.ScanCandidateID)
	}
	if err := validateEndpointEvidence(candidate.Persona, candidate.Purpose, candidate.EndpointEvidence); err != nil {
		return fmt.Errorf("候选 %s: %w", candidate.ScanCandidateID, err)
	}
	if candidate.Persona == "unclassified" && candidate.EnforcementState != "legacy_observe" {
		return fmt.Errorf("未分类 persona %s 不得进入 %s", candidate.RuntimeSinkID, candidate.EnforcementState)
	}
	return nil
}

func validateEndpointEvidence(persona, purpose, evidence string) error {
	switch persona {
	case "codex-cli":
		if purpose == "facade" {
			if evidence != "not_applicable" {
				return errors.New("Codex facade 的 endpoint_evidence 必须是 not_applicable")
			}
			return nil
		}
		if evidence != "codex_profile" && evidence != "transport_only" {
			return fmt.Errorf("Codex 业务 Sink 的 endpoint_evidence 非法: %s", evidence)
		}
	case "chatgpt-web":
		if evidence != "external_persona" {
			return errors.New("chatgpt-web 的 endpoint_evidence 必须是 external_persona")
		}
	case "unclassified":
		if evidence != "missing" {
			return errors.New("unclassified 的 endpoint_evidence 必须是 missing")
		}
	case "dead-code":
		if evidence != "not_applicable" {
			return errors.New("dead-code 的 endpoint_evidence 必须是 not_applicable")
		}
	default:
		return fmt.Errorf("未知 persona: %s", persona)
	}
	return nil
}

func aggregateBinding(id string, candidates []SinkBaselineCandidateDoc) (ReleaseBindingDoc, error) {
	if len(candidates) == 0 {
		return ReleaseBindingDoc{}, fmt.Errorf("Sink %s 没有候选", id)
	}
	first := candidates[0]
	binding := ReleaseBindingDoc{
		SinkID:             id,
		Purpose:            first.Purpose,
		Persona:            first.Persona,
		EndpointEvidence:   first.EndpointEvidence,
		TargetBackend:      first.TargetBackend,
		EnforcementState:   first.EnforcementState,
		Owner:              first.Owner,
		MigrationChangeset: first.MigrationSet,
		ExpiryCondition:    first.ExpiryCondition,
		Routes:             []RouteEvidenceDoc{},
		Candidates:         make([]BindingCandidateDoc, 0, len(candidates)),
	}

	routes := make(map[string]RouteEvidenceDoc)
	for _, candidate := range candidates {
		if candidate.Purpose != binding.Purpose ||
			candidate.Persona != binding.Persona ||
			candidate.EndpointEvidence != binding.EndpointEvidence ||
			candidate.TargetBackend != binding.TargetBackend ||
			candidate.EnforcementState != binding.EnforcementState ||
			candidate.Owner != binding.Owner ||
			candidate.MigrationSet != binding.MigrationChangeset ||
			candidate.ExpiryCondition != binding.ExpiryCondition {
			return ReleaseBindingDoc{}, fmt.Errorf("Sink %s 的候选分类字段不一致", id)
		}
		for _, rawRoute := range candidate.Routes {
			route, err := parseRouteEvidence(rawRoute)
			if err != nil {
				return ReleaseBindingDoc{}, fmt.Errorf("Sink %s: %w", id, err)
			}
			routes[rawRoute] = route
		}
		binding.Candidates = append(binding.Candidates, BindingCandidateDoc{
			ScanCandidateID:  candidate.ScanCandidateID,
			ASTFingerprint:   candidate.ASTFingerprint,
			File:             candidate.File,
			Func:             candidate.Func,
			Callee:           candidate.Callee,
			SinkKind:         candidate.SinkKind,
			SinkType:         candidate.SinkType,
			Protocol:         candidate.Protocol,
			EndpointEvidence: candidate.EndpointEvidence,
			ActualBackend:    candidate.Backend,
			OfficialHost:     candidate.OfficialHost,
			TargetResolution: candidate.TargetResolution,
			BuildContexts:    cloneStrings(candidate.BuildContexts),
			ResolvedHosts:    cloneStrings(candidate.ResolvedHosts),
			ResolvedMethods:  cloneStrings(candidate.ResolvedMethods),
			ResolvedPaths:    cloneStrings(candidate.ResolvedPaths),
			ResolvedTargets:  cloneStrings(candidate.ResolvedTargets),
			Rationale:        candidate.Rationale,
		})
	}

	for _, route := range routes {
		binding.Routes = append(binding.Routes, route)
	}
	sort.Slice(binding.Routes, func(i, j int) bool { return binding.Routes[i].Raw < binding.Routes[j].Raw })
	sort.Slice(binding.Candidates, func(i, j int) bool {
		return binding.Candidates[i].ScanCandidateID < binding.Candidates[j].ScanCandidateID
	})
	return binding, nil
}

func parseRouteEvidence(raw string) (RouteEvidenceDoc, error) {
	parts := strings.Fields(raw)
	if len(parts) < 2 || len(parts) > 3 {
		return RouteEvidenceDoc{}, fmt.Errorf("route 语法非法: %q", raw)
	}
	transport := "http"
	if len(parts) == 3 {
		if parts[2] != "(WebSocket)" {
			return RouteEvidenceDoc{}, fmt.Errorf("route 传输标记非法: %q", raw)
		}
		transport = "websocket"
	}
	target := parts[1]
	pathIndex := strings.IndexByte(target, '/')
	if pathIndex <= 0 || pathIndex == len(target)-1 {
		return RouteEvidenceDoc{}, fmt.Errorf("route 目标非法: %q", raw)
	}
	method := strings.ToUpper(parts[0])
	if method != parts[0] {
		return RouteEvidenceDoc{}, fmt.Errorf("route method 必须大写: %q", raw)
	}
	return RouteEvidenceDoc{
		Raw:       raw,
		Method:    method,
		Host:      target[:pathIndex],
		Path:      target[pathIndex:],
		Transport: transport,
	}, nil
}

// BindingCatalog 是深拷贝后的不可变绑定目录。
type BindingCatalog struct {
	schemaVersion int
	source        BindingSourceDoc
	bindings      map[string]ReleaseBindingDoc
}

func NewBindingCatalog(doc BindingCatalogDoc) (BindingCatalog, error) {
	if doc.SchemaVersion != BindingCatalogSchemaVersion {
		return BindingCatalog{}, fmt.Errorf("不支持的绑定目录 schema_version: %d", doc.SchemaVersion)
	}
	if len(doc.Source.BaselineSHA256) != sha256.Size*2 {
		return BindingCatalog{}, errors.New("baseline_sha256 长度非法")
	}
	if _, err := hex.DecodeString(doc.Source.BaselineSHA256); err != nil {
		return BindingCatalog{}, fmt.Errorf("baseline_sha256 非十六进制: %w", err)
	}
	if doc.Source.BootstrapCommit == "" || doc.Source.PackagesLoaded <= 0 ||
		len(doc.Source.BuildContexts) == 0 || doc.Source.ScanPattern == "" || doc.Source.TotalCandidates <= 0 ||
		doc.Source.BoundCandidates <= 0 || doc.Source.BoundCandidates > doc.Source.TotalCandidates {
		return BindingCatalog{}, errors.New("绑定目录来源元数据非法")
	}
	if len(doc.Bindings) == 0 {
		return BindingCatalog{}, errors.New("绑定目录为空")
	}

	source := doc.Source
	source.BuildContexts = cloneStrings(doc.Source.BuildContexts)
	catalog := BindingCatalog{
		schemaVersion: doc.SchemaVersion,
		source:        source,
		bindings:      make(map[string]ReleaseBindingDoc, len(doc.Bindings)),
	}
	candidateCount := 0
	seenCandidates := make(map[string]struct{})
	for _, binding := range doc.Bindings {
		if err := validateBindingDoc(binding); err != nil {
			return BindingCatalog{}, err
		}
		if _, exists := catalog.bindings[binding.SinkID]; exists {
			return BindingCatalog{}, fmt.Errorf("sink_id 重复: %s", binding.SinkID)
		}
		for _, candidate := range binding.Candidates {
			if _, exists := seenCandidates[candidate.ScanCandidateID]; exists {
				return BindingCatalog{}, fmt.Errorf("scan_candidate_id 跨 binding 重复: %s", candidate.ScanCandidateID)
			}
			seenCandidates[candidate.ScanCandidateID] = struct{}{}
			candidateCount++
		}
		catalog.bindings[binding.SinkID] = cloneBinding(binding)
	}
	if candidateCount != doc.Source.BoundCandidates {
		return BindingCatalog{}, fmt.Errorf("bound_candidates=%d，但绑定实际承载 %d 条候选", doc.Source.BoundCandidates, candidateCount)
	}
	return catalog, nil
}

func validateBindingDoc(binding ReleaseBindingDoc) error {
	if binding.SinkID == "" || binding.Purpose == "" || binding.Persona == "" || binding.EndpointEvidence == "" ||
		binding.TargetBackend == "" || binding.EnforcementState == "" ||
		binding.Owner == "" || binding.MigrationChangeset == "" || binding.ExpiryCondition == "" {
		return fmt.Errorf("ReleaseBinding 字段不完整: sink_id=%q", binding.SinkID)
	}
	if !isBindingPersona(binding.Persona) {
		return fmt.Errorf("Sink %s 的 persona 非法: %s", binding.SinkID, binding.Persona)
	}
	if err := validateEndpointEvidence(binding.Persona, binding.Purpose, binding.EndpointEvidence); err != nil {
		return fmt.Errorf("Sink %s: %w", binding.SinkID, err)
	}
	if binding.Persona == "unclassified" && binding.EnforcementState != "legacy_observe" {
		return fmt.Errorf("未分类 persona %s 不得进入 %s", binding.SinkID, binding.EnforcementState)
	}
	if len(binding.Candidates) == 0 {
		return fmt.Errorf("Sink %s 没有源码候选", binding.SinkID)
	}
	for _, route := range binding.Routes {
		parsed, err := parseRouteEvidence(route.Raw)
		if err != nil {
			return fmt.Errorf("Sink %s: %w", binding.SinkID, err)
		}
		if parsed != route {
			return fmt.Errorf("Sink %s 的 route 拆分值与原文不一致: %s", binding.SinkID, route.Raw)
		}
	}
	for _, candidate := range binding.Candidates {
		if candidate.ScanCandidateID == "" || candidate.ASTFingerprint == "" ||
			candidate.File == "" || candidate.Func == "" || candidate.Callee == "" ||
			candidate.SinkKind == "" || candidate.SinkType == "" || candidate.Protocol == "" ||
			candidate.EndpointEvidence == "" || candidate.EndpointEvidence != binding.EndpointEvidence ||
			candidate.ActualBackend == "" || candidate.TargetResolution == "" ||
			len(candidate.BuildContexts) == 0 {
			return fmt.Errorf("Sink %s 存在不完整源码候选", binding.SinkID)
		}
	}
	return nil
}

func (c BindingCatalog) Resolve(sinkID string) (ReleaseBindingDoc, bool) {
	binding, ok := c.bindings[sinkID]
	if !ok {
		return ReleaseBindingDoc{}, false
	}
	return cloneBinding(binding), true
}

func (c BindingCatalog) Bindings() []ReleaseBindingDoc {
	ids := make([]string, 0, len(c.bindings))
	for id := range c.bindings {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	out := make([]ReleaseBindingDoc, 0, len(ids))
	for _, id := range ids {
		out = append(out, cloneBinding(c.bindings[id]))
	}
	return out
}

func (c BindingCatalog) Source() BindingSourceDoc {
	out := c.source
	out.BuildContexts = cloneStrings(c.source.BuildContexts)
	return out
}

func (c BindingCatalog) ToDoc() BindingCatalogDoc {
	return BindingCatalogDoc{SchemaVersion: c.schemaVersion, Source: c.Source(), Bindings: c.Bindings()}
}

func (c BindingCatalog) Digest() (string, error) {
	raw, err := CanonicalJSON(c.ToDoc())
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

func CanonicalJSON(value any) ([]byte, error) { return json.Marshal(value) }

func cloneBinding(in ReleaseBindingDoc) ReleaseBindingDoc {
	out := in
	out.Routes = append([]RouteEvidenceDoc(nil), in.Routes...)
	out.Candidates = make([]BindingCandidateDoc, len(in.Candidates))
	for i, candidate := range in.Candidates {
		out.Candidates[i] = candidate
		out.Candidates[i].BuildContexts = cloneStrings(candidate.BuildContexts)
		out.Candidates[i].ResolvedHosts = cloneStrings(candidate.ResolvedHosts)
		out.Candidates[i].ResolvedMethods = cloneStrings(candidate.ResolvedMethods)
		out.Candidates[i].ResolvedPaths = cloneStrings(candidate.ResolvedPaths)
		out.Candidates[i].ResolvedTargets = cloneStrings(candidate.ResolvedTargets)
	}
	return out
}

func cloneStrings(in []string) []string {
	if in == nil {
		return nil
	}
	return append([]string{}, in...)
}
