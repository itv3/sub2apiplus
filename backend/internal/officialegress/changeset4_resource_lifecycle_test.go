package officialegress

import (
	"context"
	"net/url"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func TestChangeset4RetryReusePolicyChangesAttemptResourceScope(t *testing.T) {
	reuse := profilecontract.ResourceLifecyclePolicy{
		Lifecycle:         profilecontract.LifecycleBackendClientLongLived,
		Scope:             profilecontract.ResourceScopeAccountTransport,
		RetryReusesClient: true,
	}
	first, err := resourceLifecycleScopeIdentity(reuse, "invocation-a", 1, "account-a", "https://chatgpt.com:443")
	if err != nil {
		t.Fatal(err)
	}
	second, err := resourceLifecycleScopeIdentity(reuse, "invocation-a", 2, "account-a", "https://chatgpt.com:443")
	if err != nil {
		t.Fatal(err)
	}
	if first != second {
		t.Fatalf("RetryReusesClient=true 时同一资源作用域发生漂移: %s != %s", first, second)
	}

	noReuse := reuse
	noReuse.RetryReusesClient = false
	first, err = resourceLifecycleScopeIdentity(noReuse, "invocation-a", 1, "account-a", "https://chatgpt.com:443")
	if err != nil {
		t.Fatal(err)
	}
	second, err = resourceLifecycleScopeIdentity(noReuse, "invocation-a", 2, "account-a", "https://chatgpt.com:443")
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatal("RetryReusesClient=false 时不同 attempt 仍取得同一客户端资源作用域")
	}
}

func TestChangeset4ResourceLifecyclePolicyRejectsContradictoryTruthTable(t *testing.T) {
	invalid := []profilecontract.ResourceLifecyclePolicy{
		{
			Lifecycle: profilecontract.LifecyclePerUpperApiCall,
			Scope:     profilecontract.ResourceScopeAccountTransport,
		},
		{
			Lifecycle:         profilecontract.LifecyclePerUpperApiCall,
			Scope:             profilecontract.ResourceScopeInvocationAttempt,
			RetryReusesClient: true,
		},
		{
			Lifecycle: profilecontract.LifecycleWebsocketConnection,
			Scope:     profilecontract.ResourceScopeInvocation,
		},
		{
			Lifecycle: profilecontract.LifecycleReturnedUploadUrlCall,
			Scope:     profilecontract.ResourceScopeAccountTransport,
		},
	}
	for _, policy := range invalid {
		if err := policy.Validate(); err == nil {
			t.Fatalf("矛盾资源生命周期策略未被拒绝: %+v", policy)
		}
	}
}

func TestChangeset4ConnectionPoolDigestBindsAccountAuthorityAndRelease(t *testing.T) {
	resolver, err := NewBundleResolver(DefaultReleaseCatalog(), DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	request := changeset2BundleRequest(SinkCodexResponsesForward)
	active, err := resolver.Resolve(request)
	if err != nil {
		t.Fatal(err)
	}
	request.Mode = ReleaseModePrevious
	previous, err := resolver.Resolve(request)
	if err != nil {
		t.Fatal(err)
	}
	target, err := url.Parse("https://chatgpt.com/backend-api/codex/responses")
	if err != nil {
		t.Fatal(err)
	}
	plan, err := active.ResolveEndpointPlan(
		SinkCodexResponsesForward, "POST", target, WireProtocolHTTP,
	)
	if err != nil {
		t.Fatal(err)
	}
	previousPlan, err := previous.ResolveEndpointPlan(
		SinkCodexResponsesForward, "POST", target, WireProtocolHTTP,
	)
	if err != nil {
		t.Fatal(err)
	}
	facts := executorInvocationIdentityFacts(t)
	ctx := context.WithValue(context.Background(), attemptMetadataContextKey{}, attemptMetadata{AttemptOrdinal: 1})
	_, base, err := compileConnectionIdentity(ctx, active, plan, target, facts, "invocation-a")
	if err != nil {
		t.Fatal(err)
	}

	otherAccount := facts
	otherAccount.AccountIdentityProjection.Value = "another-local-account"
	_, accountDigest, err := compileConnectionIdentity(ctx, active, plan, target, otherAccount, "invocation-a")
	if err != nil {
		t.Fatal(err)
	}
	otherAuthority, _ := url.Parse("https://chatgpt.com:8443/backend-api/codex/responses")
	_, authorityDigest, err := compileConnectionIdentity(ctx, active, plan, otherAuthority, facts, "invocation-a")
	if err != nil {
		t.Fatal(err)
	}
	_, releaseDigest, err := compileConnectionIdentity(ctx, previous, previousPlan, target, facts, "invocation-a")
	if err != nil {
		t.Fatal(err)
	}
	proxyRequest := changeset2BundleRequest(SinkCodexResponsesForward)
	proxyRequest.Deployment.ProxyMode = "configured_proxy"
	proxyRequest.Deployment.ProxyIdentityDigest = "proxy-identity-a"
	proxyBundle, err := resolver.Resolve(proxyRequest)
	if err != nil {
		t.Fatal(err)
	}
	proxyPlan, err := proxyBundle.ResolveEndpointPlan(
		SinkCodexResponsesForward, "POST", target, WireProtocolHTTP,
	)
	if err != nil {
		t.Fatal(err)
	}
	_, proxyDigest, err := compileConnectionIdentity(ctx, proxyBundle, proxyPlan, target, facts, "invocation-a")
	if err != nil {
		t.Fatal(err)
	}
	caRequest := changeset2BundleRequest(SinkCodexResponsesForward)
	caRequest.Deployment.CustomCARoots = true
	caRequest.Deployment.CustomCAContentDigest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
	caBundle, err := resolver.Resolve(caRequest)
	if err != nil {
		t.Fatal(err)
	}
	caPlan, err := caBundle.ResolveEndpointPlan(
		SinkCodexResponsesForward, "POST", target, WireProtocolHTTP,
	)
	if err != nil {
		t.Fatal(err)
	}
	_, caDigest, err := compileConnectionIdentity(ctx, caBundle, caPlan, target, facts, "invocation-a")
	if err != nil {
		t.Fatal(err)
	}
	for name, digest := range map[string]string{
		"account":   accountDigest,
		"authority": authorityDigest,
		"release":   releaseDigest,
		"proxy":     proxyDigest,
		"custom_ca": caDigest,
	} {
		if base == digest {
			t.Fatalf("连接池摘要未隔离 %s", name)
		}
	}
}
