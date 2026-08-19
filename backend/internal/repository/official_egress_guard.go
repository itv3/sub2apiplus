package repository

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/servertiming"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/imroc/req/v3"
)

// ProvideOfficialEgressGuard 从应用配置构造同一个进程级 Guard，并让共享客户端池
// 在请求期读取该实例。1C 默认 enforce，配置非法时启动失败。
func ProvideOfficialEgressGuard(cfg *config.Config) (*officialegress.Guard, error) {
	guardConfig := officialegress.GuardConfig{
		UnknownRoutePolicy:     officialegress.UnknownRoutePolicy(officialegress.PolicyEnforce),
		UnregisteredSinkPolicy: officialegress.UnregisteredSinkPolicy(officialegress.PolicyEnforce),
		MaxUniqueLogSamples:    512,
	}
	if cfg != nil {
		configured := cfg.Gateway.OfficialEgressGuard
		guardConfig.UnknownRoutePolicy = officialegress.UnknownRoutePolicy(
			strings.ToLower(strings.TrimSpace(configured.UnknownRoutePolicy)),
		)
		guardConfig.UnregisteredSinkPolicy = officialegress.UnregisteredSinkPolicy(
			strings.ToLower(strings.TrimSpace(configured.UnregisteredSinkPolicy)),
		)
		if guardConfig.UnknownRoutePolicy != officialegress.UnknownRoutePolicy(officialegress.PolicyEnforce) ||
			guardConfig.UnregisteredSinkPolicy != officialegress.UnregisteredSinkPolicy(officialegress.PolicyEnforce) {
			return nil, fmt.Errorf("变更集 1C 禁止 unknown/unregistered policy 永久全局 observe")
		}
		if configured.CanaryPercent < 0 || configured.CanaryPercent > 100 {
			return nil, fmt.Errorf("official egress canary_percent 超出 0..100: %d", configured.CanaryPercent)
		}
		guardConfig.CanaryPercent = uint8(configured.CanaryPercent)
		guardConfig.MaxUniqueLogSamples = configured.MaxUniqueLogSamples
		guardConfig.InstanceID = strings.TrimSpace(configured.InstanceID)
	}
	controlsRaw := ""
	if cfg != nil {
		controlsRaw = cfg.Gateway.OfficialEgressGuard.SinkControlsJSON
	}
	controls, err := officialegress.ParseSinkRuntimeControls(controlsRaw)
	if err != nil {
		return nil, err
	}
	if guardConfig.CanaryPercent == 0 {
		for _, control := range controls {
			if control.State == officialegress.SinkStateCanaryEnforce {
				return nil, fmt.Errorf("变更集 1C 禁止 canary_enforce 控制使用 0%% canary；全量 observe 必须使用限时覆盖")
			}
		}
	}
	sinkCatalog := officialegress.DefaultSinkCatalog()
	if cfg != nil && cfg.Gateway.ClaudeFWGCandidateEnabled {
		sinkCatalog, err = officialegress.ClaudeFWGCandidateSinkCatalog(sinkCatalog)
		if err != nil {
			return nil, fmt.Errorf("安装 Claude FW-G candidate SinkCatalog：%w", err)
		}
	}
	sinkCatalog, err = sinkCatalog.WithRuntimeControls(controls)
	if err != nil {
		return nil, err
	}
	policyOverridesRaw := ""
	if cfg != nil {
		policyOverridesRaw = cfg.Gateway.OfficialEgressGuard.PolicyOverridesJSON
	}
	policyOverrides, err := officialegress.ParseGuardPolicyOverrides(policyOverridesRaw)
	if err != nil {
		return nil, err
	}
	guardConfig.PolicyOverrides = policyOverrides
	return officialegress.ConfigureDefaultGuardWithSinkCatalog(guardConfig, sinkCatalog, nil)
}

// ProvideHTTPUpstream 把 Guard 显式注入共享 HTTPUpstream；保留 NewHTTPUpstream
// 供现有单元测试和非 Wire 构造使用。
func ProvideHTTPUpstream(
	cfg *config.Config,
	guard *officialegress.Guard,
) service.HTTPUpstream {
	return newHTTPUpstream(cfg, guard)
}

// instrumentReqClientWithGuard 让包内 before/after wire 测试注入独立 Guard，避免修改
// 进程默认配置；生产始终由 instrumentReqClient 使用 wiring 后的默认 Guard。
func instrumentReqClientWithGuard(
	client *req.Client,
	guard *officialegress.Guard,
	profile *tlsfingerprint.Profile,
) *req.Client {
	if client == nil {
		return nil
	}
	lowercaseWire := profile != nil && profile.Transport.LowercaseHeaders
	preserveCase := []string(nil)
	if profile != nil {
		preserveCase = append(preserveCase, profile.Transport.PreserveHeaderCase...)
	}
	client.GetTransport().WrapRoundTripFunc(func(rt http.RoundTripper) req.HttpRoundTripFunc {
		guarded := officialegress.NewGuardedRoundTripper(
			rt,
			guard,
			officialegress.BackendReqProfile,
			officialegress.WireProtocolHTTP,
		)
		timed := servertiming.WrapRoundTripper(guarded)
		if !lowercaseWire {
			return timed.RoundTrip
		}
		// Guard 校验的是最终 wire 形态，因此小写化必须发生在 Guard 之外——与
		// http_upstream 链的 lowercase(Guard(base)) 顺序保持一致。顺序反了，Guard 看到的
		// 仍是 Go 规范化后的名字（Accept），会把画像声明的 lowercase 计划误判成
		// request_modified_after_finalize。
		lowered := tlsfingerprint.NewLowercaseHeaderRoundTripper(timed, preserveCase)
		return lowered.RoundTrip
	})
	return client
}

// ProvideOfficialCodexReqProfileTransportResource 把 req/v3 连接池作为受信物理资源
// 注入 Codex Executor；exchange 继续使用独立 transport-only 工厂。
func ProvideOfficialCodexReqProfileTransportResource() service.OfficialCodexReqProfileTransportResource {
	return openAIOAuthReqProfileTransport{}
}

func (openAIOAuthReqProfileTransport) SendOfficialCodexReqProfile(
	ctx context.Context,
	request *http.Request,
	transport officialegress.TransportSpec,
	proxyURL string,
) (*http.Response, error) {
	return openAIOAuthReqProfileTransport{}.Do(ctx, request, transport, proxyURL)
}

func (openAIOAuthReqProfileTransport) Do(
	_ context.Context,
	request *http.Request,
	transport officialegress.TransportSpec,
	proxyURL string,
) (*http.Response, error) {
	tlsProfile, err := tlsProfileFromOfficialTransport(transport)
	if err != nil {
		return nil, err
	}
	client, err := createOpenAIReqClientWithProfile(proxyURL, tlsProfile)
	if err != nil {
		return nil, err
	}
	return client.GetClient().Do(request)
}

func resolveOpenAIExchangeTLSProfile(mode officialegress.ReleaseMode) (*tlsfingerprint.Profile, error) {
	release, err := officialegress.DefaultReleaseCatalog().Resolve(mode)
	if err != nil {
		return nil, err
	}
	for _, transport := range release.Profile().Transports() {
		if transport.Protocol != "http/1.1" {
			continue
		}
		return &tlsfingerprint.Profile{
			Name: "Official Codex OAuth exchange transport-only " + release.ReleaseDigest(),
			Transport: tlsfingerprint.TransportOptions{
				DisableCompression: true, LowercaseHeaders: transport.LowercaseHTTPHeaders,
				StrictH1Wire: false,
			},
			CipherSuites:        append([]uint16(nil), transport.CipherSuites...),
			Curves:              append([]uint16(nil), transport.SupportedGroups...),
			SignatureAlgorithms: append([]uint16(nil), transport.SignatureAlgorithms...),
			ALPNProtocols:       append([]string(nil), transport.ALPN...),
			SupportedVersions:   append([]uint16(nil), transport.SupportedVersions...),
			KeyShareGroups:      append([]uint16(nil), transport.KeyShareGroups...),
			PSKModes:            append([]uint16(nil), transport.PSKModes...),
			Extensions:          append([]uint16(nil), transport.Extensions...),
			RandomizeExtensions: transport.RandomizeExtensions,
			TLSVersMin:          transport.TLSMinVersion,
			TLSVersMax:          transport.TLSMaxVersion,
		}, nil
	}
	return nil, errors.New("正式 ProfileSpec 缺少 OAuth exchange HTTP transport")
}

func tlsProfileFromOfficialTransport(
	transport officialegress.TransportSpec,
) (*tlsfingerprint.Profile, error) {
	tls := transport.TLS
	if tls.Stack == "" || len(tls.CipherSuites) == 0 {
		return nil, errors.New("CompiledExecution 缺少 TLS 事实")
	}
	rules := make([]tlsfingerprint.H1HeaderOrderRule, len(tls.H1HeaderOrders))
	for i, rule := range tls.H1HeaderOrders {
		mode := tlsfingerprint.H1HeaderOrderModeStatic
		if rule.Mode == "swap_remove" {
			mode = tlsfingerprint.H1HeaderOrderModeSwapRemove
		}
		rules[i] = tlsfingerprint.H1HeaderOrderRule{
			Method: rule.Method, Path: rule.Path,
			RequiredHeaders:  append([]string(nil), rule.RequiredHeaders...),
			ForbiddenHeaders: append([]string(nil), rule.ForbiddenHeaders...),
			Order:            append([]string(nil), rule.Order...), Mode: mode,
			PrefixHeaders:  append([]string(nil), rule.PrefixHeaders...),
			RemoveHeaders:  append([]string(nil), rule.RemoveHeaders...),
			AppendHeaders:  append([]string(nil), rule.AppendHeaders...),
			RejectUnlisted: rule.RejectUnlisted,
		}
	}
	return &tlsfingerprint.Profile{
		Name: "Official Codex compiled " + transport.ConnectionPoolDigest,
		Transport: tlsfingerprint.TransportOptions{
			DisableCompression: true, LowercaseHeaders: tls.LowercaseHeaders,
			PreserveHeaderCase: append([]string(nil), tls.PreserveHeaderCase...),
			H1HeaderOrders:     rules, StrictH1Wire: tls.StrictH1Wire,
		},
		CipherSuites:        append([]uint16(nil), tls.CipherSuites...),
		Curves:              append([]uint16(nil), tls.SupportedGroups...),
		SignatureAlgorithms: append([]uint16(nil), tls.SignatureAlgorithms...),
		ALPNProtocols:       append([]string(nil), tls.ALPN...),
		SupportedVersions:   append([]uint16(nil), tls.SupportedVersions...),
		KeyShareGroups:      append([]uint16(nil), tls.KeyShareGroups...),
		PSKModes:            append([]uint16(nil), tls.PSKModes...),
		Extensions:          append([]uint16(nil), tls.Extensions...),
		RandomizeExtensions: tls.RandomizeExtensions,
		TLSVersMin:          tls.MinVersion,
		TLSVersMax:          tls.MaxVersion,
	}, nil
}
