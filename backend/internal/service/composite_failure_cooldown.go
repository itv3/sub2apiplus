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
// 已知范围：只处理经 HandleUpstreamError 上报的"带 HTTP 状态码"的上游失败。没有 HTTP 响应的
// 传输层超时（连接超时、响应头超时）不经过该入口，不在本冷却范围内。

import (
	"context"
	"encoding/json"
	"log/slog"
	"net/http"
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
	if s == nil || s.accountRepo == nil || account == nil || account.ID <= 0 {
		return false
	}
	kind := compositeFailureCooldownKind(statusCode)
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
