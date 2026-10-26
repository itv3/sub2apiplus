package service

// Composite 自动路由的上游失败短冷却。
//
// 背景：上游普通 5xx（500、502、503 以及 Cloudflare 的 520～524）和明确的上游超时（504、522、524）
// 默认不会让账号离开调度池——只有管理员给账号配了"临时不可调度"规则才会冷却。结果是上游刚出故障
// 的那几十秒里，后续请求仍然排着队选到同一个坏账号。本文件在现有错误反馈入口
// RateLimitService.HandleUpstreamError 的 5xx 分支补一个固定 30 秒的短冷却：
//
//   - 复用现有状态存储：写账号级临时不可调度（SetTempUnschedulable）、临时不可调度缓存，
//     并通知 OpenAI 网关的进程内运行时封禁，调度器与候选池资格过滤都按原有路径避开该账号；
//   - 到期后无需任何清理动作，账号自动重新允许选择；
//   - 账号级冷却沿用全局状态：共用该账号的其它分组也会避开它（与现有临时不可调度一致）。
//
// 触发条件（全部满足才冷却）：
//  1. 开关开启（SUB2API_COMPOSITE_FAILURE_COOLDOWN，默认开启）；
//  2. 本请求走的是 Composite 自动路由——请求上下文里的路由来源是候选池写入的 account_model，
//     显式路由、前缀猜测和非 Composite 分组的请求都不触发；
//  3. 上游状态码属于 compositeFailureCooldownStatus 列出的普通 5xx 或超时类状态码。
//
// 不触发的情形：429、额度、鉴权（401、402、403）、4xx 参数错误、客户端取消、本地满载都不会
// 走到 5xx 分支，仍按原规则处理；529 过载沿用原有的过载冷却；池模式、自定义错误码账号在
// HandleUpstreamError 更早的位置已经分流，不会到达这里。
//
// 不缩短已有冷却：账号已有更长的临时不可调度、限流或过载窗口时直接跳过，数据库写入本身也只会
// 把截止时间往后推（SetTempUnschedulable 的 SQL 带 "原值为空或更早" 条件）。
//
// 传输层超时（没有收到 HTTP 响应）：连接超时、TLS 握手超时、等待响应头超时、读超时不经过
// HandleUpstreamError，由 OpenAI 族转发的传输层错误统一入口 handleOpenAIUpstreamTransportError
// 以及首输出超时入口 newOpenAIFirstOutputTimeoutError 调用本文件的
// ApplyCompositeTransportTimeoutCooldown／ApplyCompositeFirstOutputTimeoutCooldown，
// 边界与 5xx 路径完全一致（同一开关、同一路由来源判断、同一"不缩短已有冷却"）。
// 计入与不计入的判定见 compositeTransportTimeoutKind。

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"net"
	"net/http"
	"strings"
	"time"
)

// compositeFailureCooldownEnv 是上游失败短冷却的开关。
// 取值 0、false、off、no、disabled 时关闭，未设置或其它取值一律开启。
const compositeFailureCooldownEnv = "SUB2API_COMPOSITE_FAILURE_COOLDOWN"

// compositeFailureCooldownDuration 是短冷却时长（固定 30 秒）。
const compositeFailureCooldownDuration = 30 * time.Second

// compositeFailureCooldownMarker 写进临时不可调度状态的 matched_keyword，便于在管理端识别来源。
const compositeFailureCooldownMarker = "composite_failure_cooldown"

// CompositeFailureCooldownEnabled 报告上游失败短冷却是否开启（默认开启）。
func CompositeFailureCooldownEnabled() bool {
	return compositeEnvSwitchEnabled(compositeFailureCooldownEnv)
}

// compositeFailureCooldownKind 返回状态码的失败类别："timeout"（明确的上游超时）、
// "server_error"（普通 5xx）；其它状态码返回空串，表示不冷却。
func compositeFailureCooldownKind(statusCode int) string {
	switch statusCode {
	case http.StatusGatewayTimeout, 522, 524:
		return "timeout"
	case http.StatusInternalServerError, http.StatusBadGateway, http.StatusServiceUnavailable, 520, 521, 523:
		return "server_error"
	default:
		// 501、505 等描述的是请求本身不被支持，529 有自己的过载冷却，都不在此列。
		return ""
	}
}

// isCompositeAutoRouted 判断本请求是否走的 Composite 自动路由（候选池按账号声明选出的目标）。
func isCompositeAutoRouted(ctx context.Context) bool {
	source, ok := CompositeRouteSourceFromContext(ctx)
	return ok && source == CompositeRouteSourceAccount
}

// applyCompositeFailureCooldown 在 5xx 分支为账号补 30 秒短冷却，返回是否写入了冷却。
// 调用方是 HandleUpstreamError，不改变它的返回值。
func (s *RateLimitService) applyCompositeFailureCooldown(ctx context.Context, account *Account, statusCode int, responseBody []byte) bool {
	return s.writeCompositeFailureCooldown(ctx, account, statusCode, compositeFailureCooldownKind(statusCode), responseBody)
}

// writeCompositeFailureCooldown 是 5xx 与传输层超时共用的冷却写入：kind 为空表示不冷却；
// statusCode 为 0 表示没有 HTTP 响应（传输层超时）；detail 是写进状态的错误说明（会被截断）。
// 开关、路由来源、"不缩短已有冷却"的判断都在这里，保证各入口边界一致。
func (s *RateLimitService) writeCompositeFailureCooldown(ctx context.Context, account *Account, statusCode int, kind string, responseBody []byte) bool {
	if s == nil || s.accountRepo == nil || account == nil || account.ID <= 0 {
		return false
	}
	if kind == "" || !CompositeFailureCooldownEnabled() || !isCompositeAutoRouted(ctx) {
		return false
	}

	now := time.Now()
	until := now.Add(compositeFailureCooldownDuration)
	// 先看调用方手里的账号快照，再以库里最新的账号复核一次（快照可能早于别的请求写下的冷却）。
	if compositeAccountBlockedUntilAfter(account, until) {
		return false
	}
	if fresh, err := s.accountRepo.GetByID(ctx, account.ID); err == nil && fresh != nil && compositeAccountBlockedUntilAfter(fresh, until) {
		return false
	}

	state := &TempUnschedState{
		UntilUnix:       until.Unix(),
		TriggeredAtUnix: now.Unix(),
		StatusCode:      statusCode,
		MatchedKeyword:  compositeFailureCooldownMarker,
		RuleIndex:       -1,
		ErrorMessage:    truncateTempUnschedMessage(responseBody, tempUnschedMessageMaxBytes),
		Scope:           TempUnschedScopeAccount,
	}
	reason := ""
	if raw, err := json.Marshal(state); err == nil {
		reason = string(raw)
	}
	if reason == "" {
		reason = compositeFailureCooldownMarker + ":" + kind
	}

	s.notifyAccountSchedulingBlocked(account, until, compositeFailureCooldownMarker)
	if err := s.accountRepo.SetTempUnschedulable(ctx, account.ID, until, reason); err != nil {
		slog.Warn("composite_failure_cooldown_set_failed", "account_id", account.ID, "status_code", statusCode, "error", err)
		return false
	}
	if s.tempUnschedCache != nil {
		if err := s.tempUnschedCache.SetTempUnsched(ctx, account.ID, state); err != nil {
			slog.Warn("composite_failure_cooldown_cache_set_failed", "account_id", account.ID, "error", err)
		}
	}
	slog.Info("composite_failure_cooldown_applied",
		"account_id", account.ID,
		"platform", account.Platform,
		"status_code", statusCode,
		"kind", kind,
		"until", until,
	)
	return true
}

// compositeAccountBlockedUntilAfter 判断账号已有的临时不可调度、限流或过载窗口是否不早于 until：
// 是则说明账号本来就被挡得更久，短冷却既没有意义，也不能缩短它。
func compositeAccountBlockedUntilAfter(account *Account, until time.Time) bool {
	if account == nil {
		return false
	}
	for _, existing := range []*time.Time{account.TempUnschedulableUntil, account.RateLimitResetAt, account.OverloadUntil} {
		if existing != nil && !existing.Before(until) {
			return true
		}
	}
	return false
}

// compositeTransportTimeoutKind 判断一次没有 HTTP 响应的传输层错误是否属于"明确的上游超时"，
// 是则返回 "transport_timeout"，否则返回空串（不冷却）。判定从严：
//
// 计入：
//   - net.Error 且 Timeout() 为真：连接超时、TLS 握手超时、等待响应头超时（ResponseHeaderTimeout）、读超时；
//   - context.DeadlineExceeded，但前提是请求自己的上下文没有结束——说明截止时间来自传输层的
//     上游超时设置（如 Client.Timeout），而不是客户端或网关入口给整条请求设的期限；
//   - 错误文本明确写着"timeout awaiting response headers"、"tls handshake timeout"、
//     "Client.Timeout exceeded while awaiting headers"（没有保留类型的包装错误）。
//
// 不计入：
//   - 客户端取消（context.Canceled，或请求上下文已被取消／已超过期限，即下游断开或整条请求超时）；
//   - DNS 解析失败（含解析超时）：问题在解析环节，不是上游没响应；账号层面已有更长的"永久性
//     传输故障"处理；
//   - 连接被拒、无路由、网络不可达及代理凭据被拒（classifyUpstreamTransportError 判为永久性）：
//     上游或代理不可达，不是"超时"，而且永久性故障本身已写 10 分钟临时不可调度，更长，不能被缩短；
//   - 本机暂存故障（IsRequestBodyStorageError）：与上游无关；
//   - 其它没有超时语义的错误（TLS 证书错误、连接重置、EOF 等）。
func compositeTransportTimeoutKind(ctx context.Context, err error) string {
	if err == nil || IsRequestBodyStorageError(err) {
		return ""
	}
	// 客户端取消、或请求自己的上下文已经结束：与上游无关。
	if errors.Is(err, context.Canceled) {
		return ""
	}
	if ctx != nil && ctx.Err() != nil {
		return ""
	}
	// DNS 解析失败（含解析超时）不是"上游超时"。
	var dnsErr *net.DNSError
	if errors.As(err, &dnsErr) {
		return ""
	}
	// 连接被拒、无路由、代理凭据被拒等永久性故障另有 10 分钟处理。
	if classifyUpstreamTransportError(err).Persistent {
		return ""
	}
	if errors.Is(err, context.DeadlineExceeded) {
		return "transport_timeout"
	}
	var netErr net.Error
	if errors.As(err, &netErr) && netErr.Timeout() {
		return "transport_timeout"
	}
	msg := strings.ToLower(err.Error())
	for _, marker := range []string{
		"timeout awaiting response headers",
		"tls handshake timeout",
		"client.timeout exceeded while awaiting headers",
	} {
		if strings.Contains(msg, marker) {
			return "transport_timeout"
		}
	}
	return ""
}

// ApplyCompositeTransportTimeoutCooldown 在 OpenAI 族转发遇到传输层错误时调用：若该错误是
// 明确的上游超时（见 compositeTransportTimeoutKind），且请求走的是 Composite 自动路由，
// 为账号补 30 秒短冷却，返回是否写入。其它情况（含开关关闭、非 Composite 请求、已有更长冷却）
// 一律不动。调用方不应依赖返回值改变自己的流程。
func (s *RateLimitService) ApplyCompositeTransportTimeoutCooldown(ctx context.Context, account *Account, err error) bool {
	if s == nil || account == nil {
		return false
	}
	kind := compositeTransportTimeoutKind(ctx, err)
	if kind == "" {
		return false
	}
	// 与账号状态更新一致：脱离请求的取消信号再写库，但保留上下文里的路由信息。
	stateCtx, cancel := openAIAccountStateContext(ctx)
	defer cancel()
	return s.writeCompositeFailureCooldown(stateCtx, account, 0, kind, []byte(sanitizeUpstreamErrorMessage(err.Error())))
}

// ApplyCompositeFirstOutputTimeoutCooldown 在"首输出超时"（等待响应头超时或上游迟迟没有语义输出，
// 管理员配置的上游超时设置）触发换号时调用，规则同 ApplyCompositeTransportTimeoutCooldown。
// 该超时在转发层已按 504 归类，所以冷却状态里的状态码记 504，类别记 timeout。
func (s *RateLimitService) ApplyCompositeFirstOutputTimeoutCooldown(ctx context.Context, account *Account, phase string) bool {
	if s == nil || account == nil {
		return false
	}
	stateCtx, cancel := openAIAccountStateContext(ctx)
	defer cancel()
	return s.writeCompositeFailureCooldown(stateCtx, account, http.StatusGatewayTimeout, "timeout", []byte("first output timeout: phase="+phase))
}
