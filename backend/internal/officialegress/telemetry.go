package officialegress

import (
	"context"
	"log/slog"
	"sort"
	"strings"
	"sync"
)

type GuardReason string

const (
	ReasonOutOfScopePassthrough            GuardReason = "out_of_scope_passthrough"
	ReasonUnknownRoute                     GuardReason = "unknown_route"
	ReasonRoutePersonaMismatch             GuardReason = "route_persona_mismatch"
	ReasonMissingSinkID                    GuardReason = "missing_sink_id"
	ReasonUnregisteredSink                 GuardReason = "unregistered_sink"
	ReasonSinkBindingMismatch              GuardReason = "sink_binding_mismatch"
	ReasonMissingFinalizationToken         GuardReason = "missing_finalization_token"
	ReasonReleaseDigestMismatch            GuardReason = "release_digest_mismatch"
	ReasonRequestModifiedAfterFinalize     GuardReason = "request_modified_after_finalize"
	ReasonWrongExecutor                    GuardReason = "wrong_executor"
	ReasonWrongBackend                     GuardReason = "wrong_backend"
	ReasonConnectionPoolMismatch           GuardReason = "conn_pool_identity_mismatch"
	ReasonLegacyObservePassthrough         GuardReason = "legacy_observe_passthrough"
	ReasonUnregisteredSinkObserved         GuardReason = "unregistered_sink_observed"
	ReasonUnknownRouteObserved             GuardReason = "unknown_route_observed"
	ReasonUnregisteredSinkOverrideObserved GuardReason = "unregistered_sink_override_observed"
	ReasonUnknownRouteOverrideObserved     GuardReason = "unknown_route_override_observed"
	ReasonCanaryObservePassthrough         GuardReason = "canary_observe_passthrough"
	ReasonSinkOverrideObserved             GuardReason = "sink_override_observed"
	ReasonUnclassifiedPersona              GuardReason = "unclassified_persona"
	ReasonManagedPolicyMismatch            GuardReason = "managed_policy_mismatch"
)

// GuardEvent 是唯一允许进入日志和指标的结构。它有意不包含 URL、query、Header、Body、
// Token、Cookie、账号 ID 或真实资源路径。
type GuardEvent struct {
	Reason           GuardReason
	Scope            EgressScope
	SinkID           SinkID
	Method           string
	HostTemplate     string
	PathTemplate     string
	DeclaredPersona  Persona
	ResolvedPersona  Persona
	Backend          BackendKind
	Protocol         WireProtocol
	ProfileDigest    string
	EnforcementState SinkEnforcementState
}

func (e GuardEvent) dedupKey() string {
	return strings.Join([]string{
		string(e.Reason), string(e.Scope), string(e.SinkID), e.Method,
		e.HostTemplate, e.PathTemplate, string(e.DeclaredPersona),
		string(e.ResolvedPersona), string(e.Backend), string(e.Protocol),
		e.ProfileDigest, string(e.EnforcementState),
	}, "\x00")
}

type GuardRecorder interface {
	RecordOfficialEgressEvent(event GuardEvent) error
}

type GuardMetric struct {
	Reason           GuardReason
	SinkID           SinkID
	Persona          Persona
	Backend          BackendKind
	EnforcementState SinkEnforcementState
	Total            uint64
}

// BoundedGuardRecorder 对日志样本做容量有界的精确去重，同时始终累计总量指标。
type BoundedGuardRecorder struct {
	mu         sync.Mutex
	logger     *slog.Logger
	maxSamples int
	samples    map[string]struct{}
	totals     map[string]GuardMetric
}

func NewBoundedGuardRecorder(maxSamples int, logger *slog.Logger) *BoundedGuardRecorder {
	if maxSamples <= 0 {
		maxSamples = 512
	}
	if logger == nil {
		logger = slog.Default()
	}
	return &BoundedGuardRecorder{
		logger: logger, maxSamples: maxSamples,
		samples: make(map[string]struct{}, maxSamples),
		totals:  make(map[string]GuardMetric),
	}
}

func (r *BoundedGuardRecorder) RecordOfficialEgressEvent(event GuardEvent) error {
	if r == nil {
		return nil
	}
	metricKey := strings.Join([]string{
		string(event.Reason), string(event.SinkID), string(event.ResolvedPersona),
		string(event.Backend), string(event.EnforcementState),
	}, "\x00")
	sampleKey := event.dedupKey()

	r.mu.Lock()
	metric := r.totals[metricKey]
	metric.Reason = event.Reason
	metric.SinkID = event.SinkID
	metric.Persona = event.ResolvedPersona
	metric.Backend = event.Backend
	metric.EnforcementState = event.EnforcementState
	metric.Total++
	r.totals[metricKey] = metric
	_, alreadyLogged := r.samples[sampleKey]
	shouldLog := !alreadyLogged && len(r.samples) < r.maxSamples
	if shouldLog {
		r.samples[sampleKey] = struct{}{}
	}
	r.mu.Unlock()

	if !shouldLog {
		return nil
	}
	level := slog.LevelWarn
	switch event.Reason {
	case ReasonOutOfScopePassthrough, ReasonLegacyObservePassthrough:
		level = slog.LevelDebug
	case ReasonCanaryObservePassthrough, ReasonSinkOverrideObserved,
		ReasonUnknownRouteOverrideObserved, ReasonUnregisteredSinkOverrideObserved:
		// 回滚与限时覆盖必须形成生产可见的审计记录；有界去重避免正常灰度产生日志风暴。
		level = slog.LevelInfo
	}
	r.logger.LogAttrs(
		context.Background(),
		level,
		"official_egress_guard",
		slog.String("reason_code", string(event.Reason)),
		slog.String("scope", string(event.Scope)),
		slog.String("sink_id", string(event.SinkID)),
		slog.String("method", event.Method),
		slog.String("host_template", event.HostTemplate),
		slog.String("path_template", event.PathTemplate),
		slog.String("declared_persona", string(event.DeclaredPersona)),
		slog.String("resolved_persona", string(event.ResolvedPersona)),
		slog.String("backend", string(event.Backend)),
		slog.String("protocol", string(event.Protocol)),
		slog.String("profile_digest", event.ProfileDigest),
		slog.String("enforcement_state", string(event.EnforcementState)),
	)
	return nil
}

func (r *BoundedGuardRecorder) Snapshot() []GuardMetric {
	if r == nil {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]GuardMetric, 0, len(r.totals))
	for _, metric := range r.totals {
		out = append(out, metric)
	}
	sort.Slice(out, func(i, j int) bool {
		left := string(out[i].Reason) + "\x00" + string(out[i].SinkID) + "\x00" + string(out[i].Persona) + "\x00" + string(out[i].Backend)
		right := string(out[j].Reason) + "\x00" + string(out[j].SinkID) + "\x00" + string(out[j].Persona) + "\x00" + string(out[j].Backend)
		return left < right
	})
	return out
}

func (r *BoundedGuardRecorder) UniqueSampleCount() int {
	if r == nil {
		return 0
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	return len(r.samples)
}
