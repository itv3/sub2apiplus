package officialegress

import (
	"context"
	"net/http"
	"testing"
	"time"
)

func TestGuardPolicyOverridesAreInstanceScopedAndKeepViolationTelemetry(t *testing.T) {
	expiresAt := time.Date(2026, 8, 3, 12, 0, 0, 0, time.UTC)
	unknown, err := http.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api/codex/temporary", nil)
	if err != nil {
		t.Fatal(err)
	}
	overrides := []GuardPolicyOverride{
		{
			Policy: GuardPolicyOverrideUnknownRoute, InstanceID: "DMIT",
			Route: GuardPolicyRouteScope{
				Method: http.MethodGet, Host: "chatgpt.com", Path: "/backend-api/codex/{managed_path}",
				PathSHA256: exactRoutePathSHA256(unknown.URL), Protocol: WireProtocolHTTP,
			},
			ObserveUntil: expiresAt, Owner: "oncall", ReasonCode: "unknown_route_exercise",
		},
		{
			Policy: GuardPolicyOverrideUnregisteredSink, InstanceID: "DMIT",
			SinkID: MissingSinkIDOverrideTarget,
			Route: GuardPolicyRouteScope{
				Method: http.MethodPost, Host: "chatgpt.com", Path: "/backend-api/codex/responses",
				Protocol: WireProtocolHTTP,
			},
			ObserveUntil: expiresAt, Owner: "oncall", ReasonCode: "missing_sink_exercise",
		},
	}
	recorder := &captureGuardRecorder{}
	guard, err := NewGuard(GuardConfig{
		UnknownRoutePolicy:     UnknownRoutePolicy(PolicyEnforce),
		UnregisteredSinkPolicy: UnregisteredSinkPolicy(PolicyEnforce),
		InstanceID:             "DMIT", PolicyOverrides: overrides,
	}, DefaultSinkCatalog(), DefaultOfficialRouteCatalog(), recorder)
	if err != nil {
		t.Fatal(err)
	}
	configCopy := guard.Config()
	configCopy.PolicyOverrides[0].Owner = "mutated"
	if guard.Config().PolicyOverrides[0].Owner != "oncall" {
		t.Fatal("调用方通过 Guard.Config 修改了运行时策略覆盖")
	}
	guard.now = func() time.Time { return expiresAt.Add(-time.Minute) }

	unknownDecision := guard.Evaluate(unknown, BackendHTTPUpstream, WireProtocolHTTP)
	if !unknownDecision.Allow || !containsGuardReason(unknownDecision.Reasons, ReasonUnknownRoute) ||
		!containsGuardReason(unknownDecision.Reasons, ReasonUnknownRouteOverrideObserved) {
		t.Fatalf("未知 route 的范围化覆盖未放行或未保留原始违规：%+v", unknownDecision)
	}
	sibling, err := http.NewRequest(
		http.MethodGet, "https://chatgpt.com/backend-api/codex/unexpected-new-bypass", nil,
	)
	if err != nil {
		t.Fatal(err)
	}
	siblingDecision := guard.Evaluate(sibling, BackendHTTPUpstream, WireProtocolHTTP)
	if siblingDecision.Allow || siblingDecision.RejectionReason != ReasonUnknownRoute ||
		containsGuardReason(siblingDecision.Reasons, ReasonUnknownRouteOverrideObserved) {
		t.Fatalf("同命名空间兄弟未知路径被目标 override 越权放行：%+v", siblingDecision)
	}

	missingContext, err := WithAttemptMetadata(context.Background(), AttemptMetadataInput{
		Purpose: "user_request.responses", DeclaredPersona: PersonaCodexCLI,
	})
	if err != nil {
		t.Fatal(err)
	}
	missingDecision := guard.Evaluate(
		testResponsesRequest(t, missingContext), BackendHTTPUpstream, WireProtocolHTTP,
	)
	if !missingDecision.Allow || !containsGuardReason(missingDecision.Reasons, ReasonMissingSinkID) ||
		!containsGuardReason(missingDecision.Reasons, ReasonUnregisteredSinkOverrideObserved) {
		t.Fatalf("缺失 SinkID 的范围化覆盖未放行或未保留原始违规：%+v", missingDecision)
	}

	guard.now = func() time.Time { return expiresAt.Add(time.Minute) }
	expiredDecision := guard.Evaluate(unknown, BackendHTTPUpstream, WireProtocolHTTP)
	if expiredDecision.Allow || expiredDecision.RejectionReason != ReasonUnknownRoute {
		t.Fatalf("范围化覆盖到期后未自动恢复 fail-close：%+v", expiredDecision)
	}

	reasons := make(map[GuardReason]bool)
	for _, event := range recorder.events {
		reasons[event.Reason] = true
	}
	for _, want := range []GuardReason{
		ReasonUnknownRoute, ReasonUnknownRouteOverrideObserved,
		ReasonMissingSinkID, ReasonUnregisteredSinkOverrideObserved,
	} {
		if !reasons[want] {
			t.Fatalf("范围化覆盖期间缺少 Guard 记账：%s，events=%+v", want, recorder.events)
		}
	}
}

func TestGuardPolicyOverridesRejectBroadOrMalformedControls(t *testing.T) {
	invalid := []string{
		`{"schema_version":1,"overrides":[{"policy":"unknown_route","instance_id":"DMIT","route":{"method":"GET","host":"chatgpt.com","path":"/backend-api/codex/{managed_path}","protocol":"http"},"observe_until":"2026-08-03T12:00:00Z","owner":"oncall","reason_code":"old_broad_schema"}]}`,
		`{"schema_version":2,"overrides":[{"policy":"unknown_route","instance_id":"DMIT","route":{"method":"GET","host":"chatgpt.com","path":"/backend-api/codex/{managed_path}","protocol":"http"},"observe_until":"2026-08-03T12:00:00Z","owner":"oncall","reason_code":"missing_path_digest"}]}`,
		`{"schema_version":2,"overrides":[{"policy":"unknown_route","instance_id":"DMIT","route":{"method":"GET","host":"chatgpt.com","path":"/backend-api/codex/{managed_path}","path_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","protocol":"http"},"observe_until":"2026-08-03T12:00:00Z","owner":"oncall","reason_code":"ok","unknown":true}]}`,
		`{"schema_version":2,"overrides":[{"policy":"unregistered_sink","instance_id":"DMIT","route":{"method":"POST","host":"chatgpt.com","path":"/backend-api/codex/responses","protocol":"http"},"observe_until":"2026-08-03T12:00:00Z","owner":"oncall","reason_code":"missing_sink_scope"}]}`,
		`{"schema_version":2,"overrides":[{"policy":"unknown_route","instance_id":"","route":{"method":"GET","host":"chatgpt.com","path":"/backend-api/codex/{managed_path}","path_sha256":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","protocol":"http"},"observe_until":"2026-08-03T12:00:00Z","owner":"oncall","reason_code":"missing_instance"}]}`,
	}
	for _, raw := range invalid {
		if _, err := ParseGuardPolicyOverrides(raw); err == nil {
			t.Fatalf("宽泛或非法策略覆盖未被拒绝：%s", raw)
		}
	}
}

func TestGuardPolicyOverrideMustMatchCurrentInstance(t *testing.T) {
	target, err := http.NewRequest(http.MethodGet, "https://chatgpt.com/backend-api/codex/temporary", nil)
	if err != nil {
		t.Fatal(err)
	}
	_, err = NewGuard(GuardConfig{
		InstanceID: "other-instance",
		PolicyOverrides: []GuardPolicyOverride{{
			Policy: GuardPolicyOverrideUnknownRoute, InstanceID: "DMIT",
			Route: GuardPolicyRouteScope{
				Method: http.MethodGet, Host: "chatgpt.com", Path: "/backend-api/codex/{managed_path}",
				PathSHA256: exactRoutePathSHA256(target.URL), Protocol: WireProtocolHTTP,
			},
			ObserveUntil: time.Date(2026, 8, 3, 12, 0, 0, 0, time.UTC),
			Owner:        "oncall", ReasonCode: "wrong_instance",
		}},
	}, DefaultSinkCatalog(), DefaultOfficialRouteCatalog(), nil)
	if err == nil {
		t.Fatal("策略覆盖绑定到其他实例时仍被接受")
	}
}
