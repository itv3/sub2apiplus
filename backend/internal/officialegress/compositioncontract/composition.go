// Package compositioncontract 承载变更集 0D 的证据组合边界。
//
// 本包把 0A ProfileSpec、0B ReleaseGraph 与 0C ReleaseBinding 做一致性连接，
// 但不生成可执行请求。真正的 ResolvedRelease 还需要 ExecutionPolicy 与
// DeploymentSupportPolicy（retry、代理/CA 选择、CONNECT 画像、连接生命周期等）；
// 这些数据未出现在三份证据中，本包禁止推导或填默认值。
package compositioncontract

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/bindingcontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

const (
	CodexOAuthHTTPReleasePurpose = "openai_oauth_responses_http"
	CodexOAuthWSReleasePurpose   = "openai_oauth_responses_ws"
)

// CompositionRequest 要求调用方显式给出官方客户端发布 purpose。
//
// 业务 purpose（如 user_request.responses）与发布 purpose（如
// openai_oauth_responses_http）不是同一命名空间；从 backend 自动猜发布 purpose 会
// 再次制造不存在于源数据中的绑定，因此此处不提供自动映射。
type CompositionRequest struct {
	SinkID         string
	ReleasePurpose string
	Mode           releasecontract.ReleaseMode
}

type EndpointMatch struct {
	RouteRaw   string `json:"route_raw"`
	EndpointID string `json:"endpoint_id"`
}

// EvidenceBundle 是三份不可变证据的组合视图，不是可发送的 ResolvedRelease。
type EvidenceBundle struct {
	binding         bindingcontract.ReleaseBindingDoc
	release         releasecontract.ReleaseNodeDoc
	profile         profilecontract.ProfileSpec
	endpointMatches []EndpointMatch
}

func (b EvidenceBundle) Binding() bindingcontract.ReleaseBindingDoc {
	return cloneBinding(b.binding)
}

func (b EvidenceBundle) Release() releasecontract.ReleaseNodeDoc {
	return cloneRelease(b.release)
}

func (b EvidenceBundle) Profile() profilecontract.ProfileSpec { return b.profile }

func (b EvidenceBundle) EndpointMatches() []EndpointMatch {
	return append([]EndpointMatch(nil), b.endpointMatches...)
}

func (b EvidenceBundle) Digest() (string, error) {
	payload := struct {
		Binding         bindingcontract.ReleaseBindingDoc `json:"binding"`
		Release         releasecontract.ReleaseNodeDoc    `json:"release"`
		Profile         profilecontract.SnapshotDoc       `json:"profile"`
		EndpointMatches []EndpointMatch                   `json:"endpoint_matches"`
	}{
		Binding:         b.Binding(),
		Release:         b.Release(),
		Profile:         b.profile.ToSnapshot(),
		EndpointMatches: b.EndpointMatches(),
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

type Composer struct {
	bindings  bindingcontract.BindingCatalog
	releases  releasecontract.ReleaseGraph
	snapshots profilecontract.SnapshotCatalog
}

func NewComposer(
	bindings bindingcontract.BindingCatalog,
	releases releasecontract.ReleaseGraph,
	snapshots profilecontract.SnapshotCatalog,
) Composer {
	return Composer{bindings: bindings, releases: releases, snapshots: snapshots}
}

func (c Composer) Compose(request CompositionRequest) (EvidenceBundle, error) {
	if strings.TrimSpace(request.SinkID) == "" || strings.TrimSpace(request.ReleasePurpose) == "" || !request.Mode.Valid() {
		return EvidenceBundle{}, errors.New("组合请求缺少合法 sink_id/release_purpose/mode")
	}
	binding, ok := c.bindings.Resolve(request.SinkID)
	if !ok {
		return EvidenceBundle{}, fmt.Errorf("ReleaseBinding 不存在: %s", request.SinkID)
	}
	if binding.Persona != "codex-cli" {
		return EvidenceBundle{}, fmt.Errorf("Sink %s 的 persona=%s，不能组合 Codex OAuth 发布", binding.SinkID, binding.Persona)
	}
	if binding.Purpose == "facade" || len(binding.Routes) == 0 {
		return EvidenceBundle{}, fmt.Errorf("Sink %s 是共享 facade 或没有业务 route，必须在上游业务调用点组合", binding.SinkID)
	}
	if binding.EndpointEvidence != "codex_profile" {
		return EvidenceBundle{}, fmt.Errorf(
			"Sink %s 的端点证据状态为 %s，不能冒充画像端点完成 EvidenceBundle",
			binding.SinkID,
			binding.EndpointEvidence,
		)
	}

	release, ok := c.releases.Resolve(request.ReleasePurpose, request.Mode)
	if !ok {
		return EvidenceBundle{}, fmt.Errorf("发布坐标不存在: purpose=%s mode=%s", request.ReleasePurpose, request.Mode)
	}
	if err := validateTransportBinding(binding, release); err != nil {
		return EvidenceBundle{}, err
	}

	key := profilecontract.SnapshotKey{Version: release.Snapshot.Version, Digest: release.Snapshot.Digest}
	profile, ok := c.snapshots.Resolve(key)
	if !ok {
		return EvidenceBundle{}, fmt.Errorf("发布节点引用的不可变画像不存在: version=%s digest=%s", key.Version, key.Digest)
	}

	matches, err := matchBindingEndpoints(binding, profile)
	if err != nil {
		return EvidenceBundle{}, err
	}
	return EvidenceBundle{
		binding:         cloneBinding(binding),
		release:         cloneRelease(release),
		profile:         profile,
		endpointMatches: append([]EndpointMatch(nil), matches...),
	}, nil
}

func validateTransportBinding(
	binding bindingcontract.ReleaseBindingDoc,
	release releasecontract.ReleaseNodeDoc,
) error {
	wantTransport := ""
	switch binding.TargetBackend {
	case "http_upstream", "req_profile":
		wantTransport = "http"
	case "websocket":
		wantTransport = "websocket"
	default:
		return fmt.Errorf("Sink %s 的目标 backend %q 不能映射到 Codex OAuth wire transport", binding.SinkID, binding.TargetBackend)
	}
	if release.Wire.Transport != wantTransport {
		return fmt.Errorf(
			"Sink %s 需要 %s，但发布 purpose %s 是 %s",
			binding.SinkID,
			wantTransport,
			release.Purpose,
			release.Wire.Transport,
		)
	}
	for _, route := range binding.Routes {
		if route.Transport != wantTransport {
			return fmt.Errorf("Sink %s 的 route %s 与目标 backend %s 不一致", binding.SinkID, route.Raw, binding.TargetBackend)
		}
	}
	return nil
}

func matchBindingEndpoints(
	binding bindingcontract.ReleaseBindingDoc,
	profile profilecontract.ProfileSpec,
) ([]EndpointMatch, error) {
	endpoints := profile.Endpoints()
	matches := make([]EndpointMatch, 0, len(binding.Routes))
	for _, route := range binding.Routes {
		ids := make([]string, 0, 1)
		for _, endpoint := range endpoints {
			if endpointMatchesRoute(endpoint, route) {
				ids = append(ids, endpoint.ID)
			}
		}
		if len(ids) != 1 {
			return nil, fmt.Errorf(
				"Sink %s 的 route %s 在画像 %s 中匹配 %d 个端点",
				binding.SinkID,
				route.Raw,
				profile.Version(),
				len(ids),
			)
		}
		matches = append(matches, EndpointMatch{RouteRaw: route.Raw, EndpointID: ids[0]})
	}
	sort.Slice(matches, func(i, j int) bool { return matches[i].RouteRaw < matches[j].RouteRaw })
	return matches, nil
}

func endpointMatchesRoute(endpoint profilecontract.EndpointProfile, route bindingcontract.RouteEvidenceDoc) bool {
	if endpoint.Method != route.Method || endpoint.Host != route.Host {
		return false
	}
	endpointTransport := "http"
	if endpoint.Upgrade == "websocket" {
		endpointTransport = "websocket"
	}
	if endpointTransport != route.Transport {
		return false
	}
	return normalizeEvidencePath(endpoint.Path) == normalizeEvidencePath(route.Path)
}

// server_returned_path 在画像中是“完整服务端返回路径”标记，基线为了保持 route
// 的 host/path 分隔写成 /{server_returned_path}；这里只规范这一个证据表达差异。
func normalizeEvidencePath(value string) string {
	if value == "{server_returned_path}" {
		return "/{server_returned_path}"
	}
	return value
}

func cloneBinding(in bindingcontract.ReleaseBindingDoc) bindingcontract.ReleaseBindingDoc {
	out := in
	out.Routes = cloneSlice(in.Routes)
	out.Candidates = make([]bindingcontract.BindingCandidateDoc, len(in.Candidates))
	for i, candidate := range in.Candidates {
		out.Candidates[i] = candidate
		out.Candidates[i].BuildContexts = cloneSlice(candidate.BuildContexts)
		out.Candidates[i].ResolvedHosts = cloneSlice(candidate.ResolvedHosts)
		out.Candidates[i].ResolvedMethods = cloneSlice(candidate.ResolvedMethods)
		out.Candidates[i].ResolvedPaths = cloneSlice(candidate.ResolvedPaths)
		out.Candidates[i].ResolvedTargets = cloneSlice(candidate.ResolvedTargets)
	}
	return out
}

func cloneRelease(in releasecontract.ReleaseNodeDoc) releasecontract.ReleaseNodeDoc {
	out := in
	out.Build.RuntimeHeaders = cloneSlice(in.Build.RuntimeHeaders)
	out.Wire.StaticHeaders = cloneSlice(in.Wire.StaticHeaders)
	return out
}

// cloneSlice 保留 nil 与非 nil 空切片的区别；发布图原文中的 [] 不能往返成 null。
func cloneSlice[T any](in []T) []T {
	if in == nil {
		return nil
	}
	out := make([]T, len(in))
	copy(out, in)
	return out
}
