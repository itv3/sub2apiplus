package officialegress

import (
	"context"
	"net/http"
	"strings"
	"testing"
	"time"
)

func TestSinkRuntimeControlDowngradeChangesBehaviorWithoutChangingReceipt(t *testing.T) {
	raw := `{"schema_version":1,"controls":[{` +
		`"sink_id":"codex.responses.forward","state":"canary_enforce"}]}`
	controls, err := ParseSinkRuntimeControls(raw)
	if err != nil {
		t.Fatal(err)
	}
	base, err := NewSinkCatalog([]SinkBindingInput{testSinkBindingInput(SinkStateEnforced)})
	if err != nil {
		t.Fatal(err)
	}
	before, _ := base.Resolve(SinkCodexResponsesForward)
	controlled, err := base.WithRuntimeControls(controls)
	if err != nil {
		t.Fatal(err)
	}
	after, ok := controlled.Resolve(SinkCodexResponsesForward)
	if !ok || after.EnforcementState() != SinkStateCanaryEnforce ||
		after.MigrationReceiptDigest() != before.MigrationReceiptDigest() ||
		after.EnforcementIdentityDigest() == before.EnforcementIdentityDigest() {
		t.Fatalf("运行时降级未保留收据或未隔离 enforcement 身份：before=%+v after=%+v", before, after)
	}

	request := runtimeControlTestRequest(t, controlled)
	enforcedGuard, err := NewGuard(GuardConfig{CanaryPercent: 0}, base, mustTestRouteCatalog(t, base), nil)
	if err != nil {
		t.Fatal(err)
	}
	enforcedDecision := enforcedGuard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if enforcedDecision.Allow || enforcedDecision.RejectionReason != ReasonMissingFinalizationToken {
		t.Fatalf("enforced 基线未 fail-close：%+v", enforcedDecision)
	}

	canaryGuard, err := NewGuard(GuardConfig{CanaryPercent: 1}, controlled, mustTestRouteCatalog(t, controlled), nil)
	if err != nil {
		t.Fatal(err)
	}
	canaryDecision := canaryGuard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if !canaryDecision.Allow || !containsGuardReason(canaryDecision.Reasons, ReasonCanaryObservePassthrough) {
		t.Fatalf("enforced→canary_enforce 未产生可观察的回滚行为：%+v", canaryDecision)
	}
}

func TestGuardRejectsZeroPercentWithCanarySink(t *testing.T) {
	base, err := NewSinkCatalog([]SinkBindingInput{testSinkBindingInput(SinkStateEnforced)})
	if err != nil {
		t.Fatal(err)
	}
	controlled, err := base.WithRuntimeControls([]SinkRuntimeControl{{
		SinkID: SinkCodexResponsesForward,
		State:  SinkStateCanaryEnforce,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := NewGuard(
		GuardConfig{CanaryPercent: 0}, controlled, mustTestRouteCatalog(t, controlled), nil,
	); err == nil {
		t.Fatal("Guard 接受了 canary_enforce + 0% 的无期限全量 observe")
	}
}

func TestSinkRuntimeControlOverrideExpiresAndKeepsViolationTelemetry(t *testing.T) {
	observeUntil := time.Date(2026, 8, 2, 18, 0, 0, 0, time.UTC)
	raw := `{"schema_version":1,"controls":[{` +
		`"sink_id":"codex.responses.forward",` +
		`"observe_until":"` + observeUntil.Format(time.RFC3339) + `",` +
		`"owner":"oncall","reason_code":"exercise_rollback"}]}`
	controls, err := ParseSinkRuntimeControls(raw)
	if err != nil {
		t.Fatal(err)
	}
	base, err := NewSinkCatalog([]SinkBindingInput{testSinkBindingInput(SinkStateEnforced)})
	if err != nil {
		t.Fatal(err)
	}
	before, _ := base.Resolve(SinkCodexResponsesForward)
	controlled, err := base.WithRuntimeControls(controls)
	if err != nil {
		t.Fatal(err)
	}
	after, ok := controlled.Resolve(SinkCodexResponsesForward)
	if !ok || after.EnforcementState() != SinkStateEnforced ||
		after.MigrationReceiptDigest() != before.MigrationReceiptDigest() ||
		after.EnforcementIdentityDigest() == before.EnforcementIdentityDigest() {
		t.Fatalf("限时覆盖未保留收据或未隔离 enforcement 身份：before=%+v after=%+v", before, after)
	}

	recorder := &captureGuardRecorder{}
	guard, err := NewGuard(
		GuardConfig{CanaryPercent: 100}, controlled, mustTestRouteCatalog(t, controlled), recorder,
	)
	if err != nil {
		t.Fatal(err)
	}
	request := runtimeControlTestRequest(t, controlled)
	guard.now = func() time.Time { return observeUntil.Add(-time.Minute) }
	decision := guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if !decision.Allow || !containsGuardReason(decision.Reasons, ReasonSinkOverrideObserved) {
		t.Fatalf("限时 observe 覆盖未在到期前放行：%+v", decision)
	}
	guard.now = func() time.Time { return observeUntil.Add(time.Minute) }
	decision = guard.Evaluate(request, BackendHTTPUpstream, WireProtocolHTTP)
	if decision.Allow || decision.RejectionReason != ReasonMissingFinalizationToken {
		t.Fatalf("限时 observe 覆盖到期后未自动 fail-close：%+v", decision)
	}
	reasons := make(map[GuardReason]bool)
	for _, event := range recorder.events {
		reasons[event.Reason] = true
	}
	if !reasons[ReasonMissingFinalizationToken] || !reasons[ReasonWrongExecutor] ||
		!reasons[ReasonSinkOverrideObserved] {
		t.Fatalf("限时覆盖期间未持续记录原始违规与覆盖原因：%+v", recorder.events)
	}
}

func mustTestRouteCatalog(t *testing.T, catalog SinkCatalog) OfficialRouteCatalog {
	t.Helper()
	routes, err := NewOfficialRouteCatalog(catalog)
	if err != nil {
		t.Fatal(err)
	}
	return routes
}

func runtimeControlTestRequest(t *testing.T, catalog SinkCatalog) *http.Request {
	t.Helper()
	requestContext, err := catalog.StartAttemptContext(context.Background(), SinkCodexResponsesForward)
	if err != nil {
		t.Fatal(err)
	}
	request, err := http.NewRequestWithContext(
		requestContext,
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/responses",
		strings.NewReader(`{"model":"gpt-test"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	return request
}

func TestSinkRuntimeControlsRejectUpgradeLegacyAndMalformedManifest(t *testing.T) {
	invalid := []string{
		`{"schema_version":1,"controls":[{"sink_id":"a","state":"enforced"}]}`,
		`{"schema_version":1,"controls":[{"sink_id":"b","state":"canary_enforce"},{"sink_id":"a","state":"canary_enforce"}]}`,
		`{"schema_version":1,"controls":[{"sink_id":"a","owner":"oncall","reason_code":"missing_time"}]}`,
		`{"schema_version":1,"controls":[{"sink_id":"a","state":"canary_enforce","unknown":true}]}`,
	}
	for _, raw := range invalid {
		if _, err := ParseSinkRuntimeControls(raw); err == nil {
			t.Fatalf("非法运行时控制未被拒绝：%s", raw)
		}
	}
	legacy, err := NewSinkCatalog([]SinkBindingInput{testSinkBindingInput(SinkStateLegacyObserve)})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := legacy.WithRuntimeControls([]SinkRuntimeControl{{
		SinkID: SinkCodexResponsesForward, State: SinkStateCanaryEnforce,
	}}); err == nil {
		t.Fatal("legacy Sink 被运行时控制伪造为 canary")
	}
}

func TestDefaultEnforcementIdentityFollowsControlledGuardCatalog(t *testing.T) {
	t.Cleanup(func() {
		if _, err := ConfigureDefaultGuard(GuardConfig{}, nil); err != nil {
			t.Errorf("恢复默认 Guard 失败：%v", err)
		}
	})
	if _, err := ConfigureDefaultGuard(GuardConfig{}, nil); err != nil {
		t.Fatal(err)
	}
	before, err := DefaultSinkEnforcementIdentity(SinkCodexAdminTestCompact)
	if err != nil {
		t.Fatal(err)
	}
	controlled, err := DefaultSinkCatalog().WithRuntimeControls([]SinkRuntimeControl{{
		SinkID: SinkCodexAdminTestCompact,
		State:  SinkStateCanaryEnforce,
	}})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := ConfigureDefaultGuardWithSinkCatalog(GuardConfig{CanaryPercent: 1}, controlled, nil); err != nil {
		t.Fatal(err)
	}
	after, err := DefaultSinkEnforcementIdentity(SinkCodexAdminTestCompact)
	if err != nil {
		t.Fatal(err)
	}
	if before == after {
		t.Fatal("连接池 enforcement identity 未跟随运行时单 Sink 回滚快照变化")
	}
}

func containsGuardReason(reasons []GuardReason, want GuardReason) bool {
	for _, reason := range reasons {
		if reason == want {
			return true
		}
	}
	return false
}
