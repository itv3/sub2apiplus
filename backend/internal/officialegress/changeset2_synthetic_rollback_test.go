package officialegress

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

func syntheticChangeset2ReleaseCatalog(t *testing.T) ReleaseCatalog {
	t.Helper()
	base := DefaultReleaseCatalog()
	graphDoc := base.graph.ToDoc()
	activeNode, ok := base.graph.Resolve(
		RegistryPurposeOpenAIOAuthHTTP,
		releasecontract.ReleaseModeActive,
	)
	if !ok {
		t.Fatal("正式 active HTTP release 缺失")
	}
	var sourceEntry profilecontract.SnapshotCatalogEntry
	for _, entry := range base.snapshots.ToDoc().Snapshots {
		if entry.Version == activeNode.Snapshot.Version && entry.Digest == activeNode.Snapshot.Digest {
			sourceEntry = entry
			break
		}
	}
	if sourceEntry.File == "" {
		t.Fatal("正式 active snapshot entry 缺失")
	}
	activeRaw, err := releaseCatalogFS.ReadFile("catalogdata/runtime/" + sourceEntry.File)
	if err != nil {
		t.Fatal(err)
	}
	previousDoc, err := profilecontract.ParseSnapshot(activeRaw)
	if err != nil {
		t.Fatal(err)
	}
	previousDoc.FeatureDefaults.RemoteCompactionV2 = !previousDoc.FeatureDefaults.RemoteCompactionV2
	previousDoc.FeatureDefaults.RequestCompressionLevel++
	for transportIndex := range previousDoc.Transports {
		transport := &previousDoc.Transports[transportIndex]
		if len(transport.CipherSuites) > 1 {
			transport.CipherSuites[0], transport.CipherSuites[1] =
				transport.CipherSuites[1], transport.CipherSuites[0]
		}
		// 生命周期与 WS 压缩组合已由变更集 4 编译器封闭；合成回滚只改变
		// 仍属于支持闭集的 TLS、feature、header 与 body 发布事实。
	}
	longLivedTransportID := ""
	for _, endpoint := range previousDoc.Endpoints {
		if endpoint.ClientLifecycle == string(profilecontract.LifecycleBackendClientLongLived) {
			longLivedTransportID = endpoint.TransportID
			break
		}
	}
	if longLivedTransportID == "" {
		t.Fatal("正式画像缺少长期 HTTP transport")
	}
	for endpointIndex := range previousDoc.Endpoints {
		endpoint := &previousDoc.Endpoints[endpointIndex]
		for left, right := 0, len(endpoint.Body.Fields)-1; left < right; left, right = left+1, right-1 {
			endpoint.Body.Fields[left], endpoint.Body.Fields[right] =
				endpoint.Body.Fields[right], endpoint.Body.Fields[left]
		}
		endpoint.Headers = append(endpoint.Headers, profilecontract.SnapshotHeaderSlot{
			Slot: 999, Sequence: 0, Name: "x-synthetic-release",
			WireName: "x-synthetic-release", Value: "previous",
			Source: "constant", Condition: "always",
		})
		if endpoint.ID == "alpha_search" || endpoint.ID == "oauth_refresh" {
			endpoint.ClientLifecycle = string(profilecontract.LifecycleBackendClientLongLived)
			endpoint.TransportID = longLivedTransportID
		}
	}
	previousDoc.Digest = ""
	digestInput, err := json.Marshal(previousDoc)
	if err != nil {
		t.Fatal(err)
	}
	digestSum := sha256.Sum256(digestInput)
	previousDoc.Digest = hex.EncodeToString(digestSum[:])
	previousRaw, err := json.Marshal(previousDoc)
	if err != nil {
		t.Fatal(err)
	}
	previousFile := fmt.Sprintf(
		"snapshots/%s/%s.json",
		previousDoc.Version,
		previousDoc.Digest,
	)
	snapshotDoc := profilecontract.SnapshotCatalogDoc{
		SchemaVersion: profilecontract.SnapshotCatalogSchemaVersion,
		Snapshots: []profilecontract.SnapshotCatalogEntry{
			sourceEntry,
			{Version: previousDoc.Version, Digest: previousDoc.Digest, File: previousFile},
		},
	}
	snapshots, err := profilecontract.NewSnapshotCatalog(
		snapshotDoc,
		func(path string) ([]byte, error) {
			switch path {
			case sourceEntry.File:
				return append([]byte(nil), activeRaw...), nil
			case previousFile:
				return append([]byte(nil), previousRaw...), nil
			default:
				return nil, fmt.Errorf("未知合成 snapshot: %s", path)
			}
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	for nodeIndex := range graphDoc.Nodes {
		if graphDoc.Nodes[nodeIndex].Mode == releasecontract.ReleaseModePrevious {
			graphDoc.Nodes[nodeIndex].Snapshot = releasecontract.SnapshotReferenceDoc{
				Version: previousDoc.Version,
				Digest:  previousDoc.Digest,
			}
		}
	}
	graph, err := releasecontract.NewReleaseGraph(graphDoc)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := newReleaseCatalog(graph, snapshots, "synthetic-changeset2", "")
	if err != nil {
		t.Fatal(err)
	}
	return catalog
}

func compileSyntheticChangeset2Endpoint(
	t *testing.T,
	bundle ReleaseBundle,
	sinkID SinkID,
	endpointID string,
	method string,
	rawURL string,
	protocol WireProtocol,
	body string,
) CompiledExecution {
	t.Helper()
	target, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := DefaultSinkCatalog().Resolve(sinkID)
	if !ok {
		t.Fatalf("SinkBinding 缺失: %s", sinkID)
	}
	authenticationInput := AttemptAuthenticationInput{BearerToken: "synthetic-rollback-token"}
	semanticBody := body
	if endpointID == "oauth_refresh" {
		authenticationInput.RefreshToken = "synthetic-refresh-token"
		semanticBody = `{"client_id":"c","grant_type":"refresh_token"}`
	}
	authentication, err := NewAttemptAuthentication(authenticationInput)
	if err != nil {
		t.Fatal(err)
	}
	identityFacts := executorInvocationIdentityFacts(t)
	identityFacts.Conditions.BetaFeaturesPresent = true
	execution, err := NewCompiler().Compile(context.Background(), bundle, CodexEgressPlan{
		SinkID: sinkID, Purpose: binding.Purpose(), EndpointID: endpointID,
		InvocationID: "changeset2-synthetic-" + string(bundle.Mode()) + "-" + endpointID,
		Mode:         bundle.Mode(), Protocol: protocol, Method: method, URL: target,
		Headers: make(http.Header), IdentityMode: IdentityCodexOAuthStrict,
		IdentityFacts:  identityFacts,
		Authentication: authentication,
		HeaderPolicy:   HeaderPolicy{ID: "synthetic.rollback.headers", Source: "test"},
		BodyPolicy:     BodyPolicy{ID: "synthetic.rollback.body", Source: "test"},
		BehaviorPolicy: bundle.Behavior(), Body: NewReplayableRequestBody([]byte(semanticBody)),
		DeclaredPersona: PersonaCodexCLI,
	}, EndpointDynamicInputs{})
	if err != nil {
		t.Fatalf("编译 %s/%s：%v", bundle.Mode(), endpointID, err)
	}
	return execution
}

func TestChangeset2SyntheticProfileRollbackMatrixHasNoMixedFacts(t *testing.T) {
	catalog := syntheticChangeset2ReleaseCatalog(t)
	resolver, err := NewBundleResolver(catalog, DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	resolve := func(mode ReleaseMode, sinkID SinkID) ReleaseBundle {
		request := changeset2BundleRequest(sinkID)
		request.Mode = mode
		bundle, resolveErr := resolver.Resolve(request)
		if resolveErr != nil {
			t.Fatal(resolveErr)
		}
		return bundle
	}
	active := resolve(ReleaseModeActive, SinkCodexResponsesForward)
	previous := resolve(ReleaseModePrevious, SinkCodexResponsesForward)
	if active.ProfileDigest() == previous.ProfileDigest() ||
		active.ReleaseDigest() == previous.ReleaseDigest() {
		t.Fatal("合成 active/previous Profile 或 Release 被折叠")
	}
	profileOrder := func(bundle ReleaseBundle, endpointID string) []string {
		for _, endpoint := range bundle.Release().Profile().Endpoints() {
			if endpoint.ID == endpointID {
				out := make([]string, 0, len(endpoint.Body.Fields))
				for _, field := range endpoint.Body.Fields {
					out = append(out, field.Name)
				}
				return out
			}
		}
		return nil
	}
	if fmt.Sprint(profileOrder(active, "responses_http")) ==
		fmt.Sprint(profileOrder(previous, "responses_http")) {
		t.Fatalf(
			"合成 ProfileSpec Body 顺序未形成差异：active=%v previous=%v",
			profileOrder(active, "responses_http"),
			profileOrder(previous, "responses_http"),
		)
	}

	type endpointCase struct {
		sink                     SinkID
		id, method, target, body string
		protocol                 WireProtocol
	}
	cases := []endpointCase{
		{SinkCodexResponsesForward, "responses_http", http.MethodPost,
			"https://chatgpt.com/backend-api/codex/responses",
			`{"model":"m","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`,
			WireProtocolHTTP},
		{SinkCodexResponsesWS, "responses_ws", http.MethodGet,
			"wss://chatgpt.com/backend-api/codex/responses",
			`{"type":"response.create","model":"m","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`,
			WireProtocolWebSocket},
		{SinkCodexAlphaSearchDirect, "alpha_search", http.MethodPost,
			"https://chatgpt.com/backend-api/codex/alpha/search",
			`{"id":"i","model":"m","input":[],"commands":[],"settings":{},"max_output_tokens":1}`,
			WireProtocolHTTP},
		{SinkCodexOAuthRefresh, "oauth_refresh", http.MethodPost,
			"https://auth.openai.com/oauth/token",
			`{"client_id":"c","grant_type":"refresh_token","refresh_token":"r"}`,
			WireProtocolHTTP},
	}
	for _, tc := range cases {
		active = resolve(ReleaseModeActive, tc.sink)
		previous = resolve(ReleaseModePrevious, tc.sink)
		target, parseErr := url.Parse(tc.target)
		if parseErr != nil {
			t.Fatal(parseErr)
		}
		activePlan, planErr := active.ResolveEndpointPlan(tc.sink, tc.method, target, tc.protocol)
		if planErr != nil {
			t.Fatal(planErr)
		}
		previousPlan, planErr := previous.ResolveEndpointPlan(tc.sink, tc.method, target, tc.protocol)
		if planErr != nil {
			t.Fatal(planErr)
		}
		if fmt.Sprint(activePlan.template.endpoint.Body.Fields) ==
			fmt.Sprint(previousPlan.template.endpoint.Body.Fields) {
			t.Fatalf("%s EndpointPlan 未冻结不同 Body 契约", tc.id)
		}
		activeOrdered, orderErr := orderJSONBody(
			[]byte(tc.body), activePlan.template.endpoint.Body,
		)
		if orderErr != nil {
			t.Fatal(orderErr)
		}
		previousOrdered, orderErr := orderJSONBody(
			[]byte(tc.body), previousPlan.template.endpoint.Body,
		)
		if orderErr != nil {
			t.Fatal(orderErr)
		}
		if string(activeOrdered) == string(previousOrdered) {
			t.Fatalf(
				"%s BodyContract 未产生不同字节：active=%v previous=%v",
				tc.id,
				activePlan.template.endpoint.Body.Fields,
				previousPlan.template.endpoint.Body.Fields,
			)
		}
		activeExecution := compileSyntheticChangeset2Endpoint(
			t, active, tc.sink, tc.id, tc.method, tc.target, tc.protocol, tc.body,
		)
		previousExecution := compileSyntheticChangeset2Endpoint(
			t, previous, tc.sink, tc.id, tc.method, tc.target, tc.protocol, tc.body,
		)
		if tc.id == "responses_http" || tc.id == "responses_ws" {
			if activeExecution.request.Headers().Get("x-codex-beta-features") != "remote_compaction_v2" ||
				previousExecution.request.Headers().Get("x-codex-beta-features") != "" {
				t.Fatalf("%s 使用了 Bundle 外 feature 或丢失 active feature", tc.id)
			}
		}
		activeBody, _ := activeExecution.request.Body().ReplayableBytes()
		previousBody, _ := previousExecution.request.Body().ReplayableBytes()
		if string(activeBody) == string(previousBody) {
			t.Fatalf(
				"%s 未体现不同 Body 字段序：active=%s previous=%s",
				tc.id, activeBody, previousBody,
			)
		}
		if previousExecution.request.Headers().Get("x-synthetic-release") != "previous" ||
			activeExecution.request.Headers().Get("x-synthetic-release") != "" {
			t.Fatalf("%s Header 发布事实混搭", tc.id)
		}
		if activeExecution.transport.ProfileDigest == previousExecution.transport.ProfileDigest ||
			activeExecution.transport.ConnectionPoolDigest == previousExecution.transport.ConnectionPoolDigest ||
			activeExecution.transport.TLS.CipherSuites[0] == previousExecution.transport.TLS.CipherSuites[0] {
			t.Fatalf("%s TransportSpec/TLS/PoolDigest 未随 Profile 回滚", tc.id)
		}
		if (tc.id == "alpha_search" || tc.id == "oauth_refresh") &&
			activeExecution.transport.ResourceLifecycle == previousExecution.transport.ResourceLifecycle {
			t.Fatalf("%s 连接生命周期未随 Profile 回滚", tc.id)
		}
	}
	if active.Release().Profile().Features() == previous.Release().Profile().Features() {
		t.Fatal("合成 feature defaults 未形成真实差异")
	}
}
