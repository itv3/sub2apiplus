package service

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"runtime"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// OfficialCodexLegacyHTTPDispatch 是变更集 2 为尚未迁入 Executor 的 HTTP sink
// 提供的数据链入口。它不签发 Token，只允许 legacy_observe，并把请求与传输事实
// 作为同一个 capability 交给可信发送边界。
type OfficialCodexLegacyHTTPDispatch struct {
	Bundle          *officialegress.ReleaseBundle
	SinkID          officialegress.SinkID
	EndpointID      string
	Account         *Account
	Request         *http.Request
	ProxyURL        string
	PolicyID        string
	PolicySource    string
	InvocationID    string
	BehaviorKind    officialegress.BehaviorKind
	FallbackSinkIDs []officialegress.SinkID
	DynamicInputs   officialegress.EndpointDynamicInputs
	SingleUseBody   bool
}

// officialCodexLegacyHTTPResource 是可信的 HTTPUpstream 终端资源。裸请求与
// TransportSpec 只在 LegacyCompiledRequest.Dispatch 的同步回调期间出现，并在此处
// 立即转换为 TLS 画像后发送，不会回交业务调用方。
type officialCodexLegacyHTTPResource struct {
	httpUpstream     HTTPUpstream
	proxyURL         string
	accountID        int64
	concurrencyLimit int
}

func (r *officialCodexLegacyHTTPResource) DispatchLegacy(
	_ context.Context,
	request *http.Request,
	transport officialegress.TransportSpec,
	_ string,
) (any, error) {
	if r == nil || r.httpUpstream == nil || request == nil {
		return nil, errors.New("legacy HTTP terminal resource 未初始化")
	}
	if transport.Backend != officialegress.BackendHTTPUpstream ||
		transport.Protocol != officialegress.WireProtocolHTTP {
		return nil, errors.New("legacy HTTP terminal transport 身份不符")
	}
	request = request.WithContext(WithHTTPUpstreamRedirectsDisabled(
		WithHTTPUpstreamProfile(request.Context(), HTTPUpstreamProfileOpenAI),
	))
	tlsProfile, err := tlsFingerprintProfileFromTransportSpec(transport)
	if err != nil {
		return nil, err
	}
	return r.httpUpstream.DoWithTLS(
		request,
		r.proxyURL,
		r.accountID,
		r.concurrencyLimit,
		tlsProfile,
	)
}

func (r *OfficialEgressTransitionRuntime) DispatchCodexLegacyHTTP(
	ctx context.Context,
	httpUpstream HTTPUpstream,
	input OfficialCodexLegacyHTTPDispatch,
) (*http.Response, officialegress.ReleaseBundle, error) {
	if r == nil || r.BundleResolver == nil || r.LegacyDispatcher == nil {
		return nil, officialegress.ReleaseBundle{}, errors.New("legacy official egress runtime 未初始化")
	}
	if httpUpstream == nil || input.Account == nil || input.Request == nil || input.Request.URL == nil {
		return nil, officialegress.ReleaseBundle{}, errors.New("legacy official egress HTTP 输入不完整")
	}
	binding, ok := r.ProcessSinks.Resolve(input.SinkID)
	if !ok || !binding.RuntimeBindable() || binding.Persona() != officialegress.PersonaCodexCLI ||
		binding.EnforcementState() != officialegress.SinkStateLegacyObserve {
		return nil, officialegress.ReleaseBundle{}, errors.New("legacy dispatcher 只接受 legacy_observe Codex sink")
	}
	if strings.TrimSpace(input.PolicyID) == "" || strings.TrimSpace(input.PolicySource) == "" {
		return nil, officialegress.ReleaseBundle{}, errors.New("legacy dispatcher 缺少冻结策略来源")
	}
	requestContext := input.Request.Context()
	if requestContext == nil {
		requestContext = ctx
	}
	if requestContext == nil {
		requestContext = context.Background()
	}
	requestContext, err := r.ProcessSinks.PreserveAttemptContext(
		requestContext, input.SinkID,
	)
	if err != nil {
		return nil, officialegress.ReleaseBundle{}, err
	}
	if strings.TrimSpace(input.InvocationID) != "" {
		requestContext, err = officialegress.WithAttemptMetadata(
			requestContext,
			officialegress.AttemptMetadataInput{
				SinkID: input.SinkID, Purpose: binding.Purpose(),
				DeclaredPersona: binding.Persona(), InvocationID: input.InvocationID,
			},
		)
		if err != nil {
			return nil, officialegress.ReleaseBundle{}, err
		}
	}

	concurrencyLimit := input.Account.EffectiveLoadFactor()
	if concurrencyLimit < 1 {
		concurrencyLimit = 1
	}
	execution := officialegress.ExecutionPolicy{
		ID: input.PolicyID + ".execution", Source: input.PolicySource,
		MaxAttempts: 1, Replayable: !input.SingleUseBody, ConcurrencyLimit: concurrencyLimit,
	}
	deployment := officialegress.DeploymentSupportPolicy{
		ID: input.PolicyID + ".deployment", Source: input.PolicySource,
		Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: "direct",
		ProxyIdentityDigest: officialEgressProxyStateKey(input.ProxyURL),
		SupportedBackends: []officialegress.BackendKind{
			officialegress.BackendHTTPUpstream,
			officialegress.BackendWebSocket,
		},
	}
	if strings.TrimSpace(input.ProxyURL) != "" {
		deployment.ProxyMode = "configured_proxy"
	}
	behaviorKind := input.BehaviorKind
	if behaviorKind == "" {
		behaviorKind = officialegress.BehaviorUserRequest
	}
	behavior := officialegress.BehaviorPolicy{
		ID: input.PolicyID + ".behavior", Source: input.PolicySource,
		Kind: behaviorKind, FallbackSinkIDs: append([]officialegress.SinkID(nil), input.FallbackSinkIDs...),
		AttemptBudget: 1,
	}
	bundle := officialegress.ReleaseBundle{}
	if input.Bundle != nil {
		bundle = *input.Bundle
	} else {
		bundle, err = r.BundleResolver.Resolve(officialegress.BundleResolveRequest{
			SinkID: input.SinkID, Mode: r.CodexReleaseMode,
			Execution: execution, Deployment: deployment, Behavior: behavior,
		})
		if err != nil {
			return nil, officialegress.ReleaseBundle{}, fmt.Errorf("解析 legacy ReleaseBundle：%w", err)
		}
	}

	request := input.Request.Clone(requestContext)
	request.Header = input.Request.Header.Clone()
	var body officialegress.RequestBody
	if input.SingleUseBody {
		body, err = officialegress.NewSingleUseRequestBody(input.Request.Body, input.Request.ContentLength)
		if err != nil {
			return nil, officialegress.ReleaseBundle{}, err
		}
		input.Request.Body = http.NoBody
	} else {
		bodyBytes, readErr := readReplayableHTTPRequestBody(input.Request)
		if readErr != nil {
			return nil, officialegress.ReleaseBundle{}, readErr
		}
		body = officialegress.NewReplayableRequestBody(bodyBytes)
	}
	identity, _ := officialegress.AttemptIdentityFromContext(requestContext)
	compiled, err := r.LegacyDispatcher.Compile(
		requestContext,
		bundle,
		officialegress.CodexEgressPlan{
			SinkID: input.SinkID, Purpose: binding.Purpose(), EndpointID: input.EndpointID,
			Mode: bundle.Mode(), Protocol: officialegress.WireProtocolHTTP,
			Method: request.Method, URL: request.URL, Headers: request.Header,
			IdentityMode: officialegress.IdentityCodexOAuthStrict,
			HeaderPolicy: officialegress.HeaderPolicy{
				ID: input.PolicyID + ".headers", Source: input.PolicySource,
			},
			BehaviorPolicy: behavior, Body: body, InvocationID: identity.InvocationID,
			DeclaredPersona: officialegress.PersonaCodexCLI,
		},
		input.DynamicInputs,
	)
	if err != nil {
		return nil, officialegress.ReleaseBundle{}, fmt.Errorf("编译 legacy official egress：%w", err)
	}
	result, err := compiled.Dispatch(requestContext, &officialCodexLegacyHTTPResource{
		httpUpstream: httpUpstream, proxyURL: input.ProxyURL,
		accountID: input.Account.ID, concurrencyLimit: concurrencyLimit,
	})
	if err != nil {
		return nil, bundle, err
	}
	response, ok := result.(*http.Response)
	if !ok || response == nil {
		return nil, bundle, errors.New("legacy HTTP terminal resource 返回类型非法")
	}
	return response, bundle, nil
}
