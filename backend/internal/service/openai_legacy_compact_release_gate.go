package service

import (
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
)

// ErrOpenAILegacyCompactRemovedByRelease 表示本次调用冻结的 Codex 发布画像已不再声明
// legacy compact 端点（responses_compact），显式的 /responses/compact 入站在任何出站之前
// 被本地拒绝。
//
// 它是请求级拒绝，不是账号故障：Forward 已向客户端写出 404 本地错误并把 ops 错误日志
// 标为本地特性门控；调用方据此不换号、不补写兜底错误，调度结果上报也不把它计入账号失败。
var ErrOpenAILegacyCompactRemovedByRelease = errors.New(
	"legacy /responses/compact is not declared by the frozen Codex release profile",
)

// openAILegacyCompactRemovedMessage 是返回给客户端的可读原因：提示改用 remote compaction v2
// 或升级客户端。不写具体版本号，避免把发布坐标暴露给下游，也不触发版本泄漏门禁。
const openAILegacyCompactRemovedMessage = "Legacy /responses/compact is not supported by the current Codex release. " +
	"Use remote compaction v2 instead (send a compaction_trigger input item to /responses), or upgrade your client."

// rejectOpenAILegacyCompactRemovedByRelease 处理“目标画像已删除 responses_compact”时的
// legacy compact 入站（SPEC-EP-007／014／020 删除语义的入站侧）：
//   - 只作用于走官方 Codex 画像出站的 OAuth 账号；API Key 等账号的 compact 不受 Codex
//     画像约束，保持原行为；
//   - 发布模式取调用级 runtime 冻结的值，与 Forward 后续构造出站请求时同源，不另读配置；
//   - 画像仍声明该端点时返回 nil，行为与改动前完全一致；
//   - 画像未声明时写出 404 not_found_error（与上游下线 legacy compact 后的状态码一致）和
//     可读原因，并把 ops 错误日志标为本地特性门控（业务受限，不计入上游错误与 SLA），
//     返回 ErrOpenAILegacyCompactRemovedByRelease；
//   - runtime 或画像解析失败时不在此处拦截，交还 Forward 后续原有路径按原语义报错。
//
// 调用点位于 Forward 入口的官方子路径校验之后、模型能力刷新等任何出站之前。
func (s *OpenAIGatewayService) rejectOpenAILegacyCompactRemovedByRelease(c *gin.Context, account *Account) error {
	if account == nil || !account.IsOpenAIOAuth() || !isOpenAIResponsesCompactPath(c) {
		return nil
	}
	runtimeState, err := resolveOfficialEgressRuntime(s.officialEgress, s.httpUpstream)
	if err != nil {
		return nil
	}
	profile, err := resolveCodexVersionProfileForMode(string(runtimeState.CodexReleaseMode))
	if err != nil {
		return nil
	}
	if _, err := profile.ResolveEndpoint(officialCodexEndpointResponsesCompact); err == nil {
		return nil
	}
	MarkOpsClientBusinessLimited(c, OpsClientBusinessLimitedReasonLocalFeatureGate)
	writeOpenAIForwardLocalError(c, http.StatusNotFound, "not_found_error", openAILegacyCompactRemovedMessage, "")
	return ErrOpenAILegacyCompactRemovedByRelease
}
