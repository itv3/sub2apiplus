package officialegress

import (
	"context"
	"net/http"
	"net/url"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

func changeset2BundleRequest(sinkID SinkID, fallback ...SinkID) BundleResolveRequest {
	return BundleResolveRequest{
		SinkID: sinkID,
		Mode:   ReleaseModeActive,
		Execution: ExecutionPolicy{
			ID: "changeset2-acceptance", Source: "test", MaxAttempts: 3,
			Replayable: true, ConcurrencyLimit: 1,
		},
		Deployment: DeploymentSupportPolicy{
			ID: "changeset2-acceptance", Source: "test", Platform: "test",
			ProxyMode: "direct", SupportedBackends: []BackendKind{
				BackendHTTPUpstream, BackendWebSocket, BackendReqProfile,
			},
		},
		Behavior: BehaviorPolicy{
			ID: "changeset2-acceptance", Source: "test", Kind: BehaviorUserRequest,
			FallbackSinkIDs: fallback, AttemptBudget: 3,
		},
	}
}

func TestChangeset2ResolvedReleaseNodeIsDeepImmutable(t *testing.T) {
	release := ResolvedCodexRelease{nodes: map[string]releasecontract.ReleaseNodeDoc{
		"http": {
			Build: releasecontract.ReleaseBuildDoc{RuntimeHeaders: []releasecontract.HeaderValueDoc{{
				Name: "x-runtime", Value: "original",
			}}},
			Wire: releasecontract.ReleaseWireDoc{StaticHeaders: []releasecontract.HeaderValueDoc{{
				Name: "x-static", Value: "original",
			}}},
		},
	}}
	first, ok := release.Node("http")
	if !ok {
		t.Fatal("合成 release node 缺失")
	}
	first.Build.RuntimeHeaders[0].Value = "mutated"
	first.Wire.StaticHeaders[0].Value = "mutated"
	second, _ := release.Node("http")
	if second.Build.RuntimeHeaders[0].Value != "original" ||
		second.Wire.StaticHeaders[0].Value != "original" {
		t.Fatal("Node getter 的 Header slice 污染了冻结 Release")
	}
}

func TestChangeset2BundleResolvesOnceAcrossRetryAndFallback(t *testing.T) {
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := resolver.Resolve(changeset2BundleRequest(
		SinkCodexResponsesWS,
		SinkCodexResponsesWSHTTPBridge,
	))
	if err != nil {
		t.Fatal(err)
	}
	if resolver.ResolveCount() != 1 {
		t.Fatalf("调用开始解析次数=%d，期望 1", resolver.ResolveCount())
	}
	wsTarget, _ := url.Parse("wss://chatgpt.com/backend-api/codex/responses")
	if _, err := bundle.ResolveEndpointPlan(
		SinkCodexResponsesWS, http.MethodGet, wsTarget, WireProtocolWebSocket,
	); err != nil {
		t.Fatal(err)
	}
	// 同调用 retry 只读取冻结 Bundle，不得触碰 Resolver。
	if _, err := bundle.ResolveEndpointPlan(
		SinkCodexResponsesWS, http.MethodGet, wsTarget, WireProtocolWebSocket,
	); err != nil {
		t.Fatal(err)
	}
	fallback := bundle.FallbackNodes()
	if len(fallback) != 1 || fallback[0].SinkID != SinkCodexResponsesWSHTTPBridge {
		t.Fatalf("fallback closure 错误：%+v", fallback)
	}
	ctx, err := DefaultSinkCatalog().StartAttemptContext(context.Background(), SinkCodexResponsesWS)
	if err != nil {
		t.Fatal(err)
	}
	binding, _ := DefaultSinkCatalog().Resolve(SinkCodexResponsesWS)
	ctx, err = WithAttemptMetadata(ctx, AttemptMetadataInput{
		SinkID: SinkCodexResponsesWS, Purpose: binding.Purpose(), DeclaredPersona: PersonaCodexCLI,
		InvocationID: "changeset2-invocation", ReleaseMode: bundle.Mode(),
		ReleaseDigest: bundle.ReleaseDigest(), BundleDigest: bundle.BundleDigest(),
		ProfileDigest: bundle.ProfileDigest(),
	})
	if err != nil {
		t.Fatal(err)
	}
	ctx = withFinalizationToken(ctx, FinalizationToken{payload: tokenPayload{
		AuthorityID: "old-ws-executor", ReleaseDigest: bundle.ReleaseDigest(),
		ConnectionPoolDigest: "old-ws-pool", InvocationID: "changeset2-invocation",
	}})
	transitioned, err := TransitionFallbackAttempt(ctx, bundle, fallback[0])
	if err != nil {
		t.Fatal(err)
	}
	metadata, ok := attemptMetadataFromContext(transitioned)
	if !ok || metadata.InvocationID != "changeset2-invocation" ||
		metadata.SinkID != SinkCodexResponsesWSHTTPBridge || metadata.Token != nil ||
		metadata.ExecutorID != "" || metadata.ConnectionPoolDigest != "" {
		t.Fatalf("fallback metadata 未原子转换：%+v", metadata)
	}
	if resolver.ResolveCount() != 1 {
		t.Fatalf("retry/fallback 触发二次解析：%d", resolver.ResolveCount())
	}
}

func TestChangeset2ActivePreviousAndDigestSemanticsStayDistinct(t *testing.T) {
	catalog := DefaultReleaseCatalog()
	active, err := catalog.Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	previous, err := catalog.Resolve(ReleaseModePrevious)
	if err != nil {
		t.Fatal(err)
	}
	if active.ReleaseDigest() == previous.ReleaseDigest() {
		t.Fatal("active/previous ReleaseDigest 被版本号折叠")
	}
	if active.ProfileDigest() == active.ReleaseDigest() {
		t.Fatal("ProfileDigest 与 ReleaseDigest 语义仍被混用")
	}
	if active.Version() != previous.Version() {
		t.Fatal("本合成回滚门禁要求同版本不同 Build/Wire identity")
	}
}

func TestChangeset2DynamicTargetFreezesOnlyAtCompile(t *testing.T) {
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	bundle, err := resolver.Resolve(changeset2BundleRequest(SinkCodexFilesBlobUpload))
	if err != nil {
		t.Fatal(err)
	}
	binding, _ := DefaultSinkCatalog().Resolve(SinkCodexFilesBlobUpload)
	compile := func(raw string) (CompiledExecution, error) {
		target, parseErr := url.Parse(raw)
		if parseErr != nil {
			return CompiledExecution{}, parseErr
		}
		return NewCompiler().Compile(context.Background(), bundle, CodexEgressPlan{
			SinkID: SinkCodexFilesBlobUpload, Purpose: binding.Purpose(),
			InvocationID: "changeset2-dynamic-target",
			EndpointID:   "files_blob_upload", Mode: bundle.Mode(), Protocol: WireProtocolHTTP,
			Method: http.MethodPut, URL: target, Headers: make(http.Header),
			Body:           NewReplayableRequestBody([]byte("blob")),
			IdentityMode:   IdentityCodexOAuthStrict,
			IdentityFacts:  executorInvocationIdentityFacts(t),
			HeaderPolicy:   HeaderPolicy{ID: "changeset2-dynamic", Source: "test"},
			BodyPolicy:     BodyPolicy{ID: "changeset2-dynamic-body", Source: "test"},
			BehaviorPolicy: bundle.Behavior(), DeclaredPersona: PersonaCodexCLI,
		}, EndpointDynamicInputs{ReturnedURL: target})
	}
	first, err := compile("https://a.oaiusercontent.com/blob/one?sig=redacted")
	if err != nil {
		t.Fatal(err)
	}
	second, err := compile("https://b.oaiusercontent.com/blob/two?sig=redacted")
	if err != nil {
		t.Fatal(err)
	}
	if first.PoolDigest() == "" || first.ConnectionIdentity() == "" ||
		first.PoolDigest() == second.PoolDigest() {
		t.Fatal("动态 ReturnedURL 未在 compiler 阶段冻结独立连接身份")
	}
	target, _ := url.Parse("https://a.oaiusercontent.com/blob/one?sig=redacted")
	other, _ := url.Parse("https://sibling.oaiusercontent.com/blob/untrusted")
	_, err = NewCompiler().Compile(context.Background(), bundle, CodexEgressPlan{
		SinkID: SinkCodexFilesBlobUpload, Purpose: binding.Purpose(),
		InvocationID: "changeset2-dynamic-target-mismatch",
		EndpointID:   "files_blob_upload", Mode: bundle.Mode(), Protocol: WireProtocolHTTP,
		Method: http.MethodPut, URL: target, Headers: make(http.Header),
		Body: NewReplayableRequestBody([]byte("blob")), IdentityMode: IdentityCodexOAuthStrict,
		IdentityFacts:  executorInvocationIdentityFacts(t),
		HeaderPolicy:   HeaderPolicy{ID: "changeset2-dynamic", Source: "test"},
		BodyPolicy:     BodyPolicy{ID: "changeset2-dynamic-body", Source: "test"},
		BehaviorPolicy: bundle.Behavior(), DeclaredPersona: PersonaCodexCLI,
	}, EndpointDynamicInputs{ReturnedURL: other})
	if err == nil {
		t.Fatal("与 Plan 不一致的 sibling ReturnedURL 未被拒绝")
	}
}
