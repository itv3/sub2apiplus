package service

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"slices"
	"strings"

	"github.com/tidwall/gjson"
)

// WS 增量续接的失效条件（画像 WebSocketContinuation 节 ResetOn）。
//
// 官方客户端把 base_url、account routing override、认证代次（含 token 刷新）与账号 owner
// 作为 WS 连接键，任一变化即重建连接并全量发送（previous_response_id 失效）；已发送项的
// 迟到 tool-result metadata 变化时也不发增量。网关侧对应关系：
//   - base_url：官方出站连接池键（connectionPoolID）已含目标 Host；
//   - account_owner：连接池键与连接身份已含本地账号，换账号必然新建连接；
//   - account_routing_override：首期非默认路由失败关闭，不会出现 override；
//   - late_tool_result_metadata：网关不自行拼增量，派生帧只把预热响应挂到同一轮首帧，
//     官方下游客户端的增量由其自身按该条件判定；
//   - auth_revision：连接池键原先不含认证代次，token 刷新后会复用旧连接——画像声明该条件时
//     把握手 Authorization 的摘要计入连接池键，刷新后的请求改用新连接。
// 画像没有该节时连接池键与改动前逐字节相同。
//
// 与 ResetOn 互补的是节里的 ReplayOnSameConnection（整段重放不重连）：官方客户端在请求属性变化
// （换模型、工具目录、推理档位、service_tier 等）、输入前缀不再可复用（上下文压缩后新窗口、input_mismatch、
// input_shortened）或相邻 configuration_update 合并时，并不新建连接，而是在原连接上发一帧不带
// previous_response_id、不带 generate 的全量 response.create，也不再补预热。画像声明该字段后，网关对
// “自己管理续链的客户端”（本会话内自己发过 generate=false 预热帧）的这类全量帧同样复用原上游连接并原样转发，
// 不新建连接、不补预热帧、不把预热响应挂成 previous_response_id（见 shouldReplayOnSameOpenAIWSConnection）。
// 第三方客户端每个用户轮次都发整段历史，网关仍把它归一成“新连接加预热加链”的官方形态，行为不变。

const officialCodexContinuationResetAuthRevision = "auth_revision"

// officialCodexWebSocketContinuationResetsOn 判断 mode 对应画像是否声明指定续接失效条件。
func officialCodexWebSocketContinuationResetsOn(mode string, kind string) bool {
	if strings.TrimSpace(mode) == "" {
		return false
	}
	section := officialCodexOptionalSectionsForMode(mode).WebSocketContinuation
	return section != nil && slices.Contains(section.ResetOn, kind)
}

// officialCodexWebSocketReplayOnSameConnection 判断 mode 对应画像是否声明“整段重放不重连”
// （WebSocketContinuation 节 ReplayOnSameConnection）。画像没有该节或没有该字段时恒为 false，保持旧行为。
func officialCodexWebSocketReplayOnSameConnection(mode string) bool {
	if strings.TrimSpace(mode) == "" {
		return false
	}
	section := officialCodexOptionalSectionsForMode(mode).WebSocketContinuation
	return section != nil && section.ReplayOnSameConnection
}

// openAIWSPayloadIsClientPrewarm 判断下游 response.create 是否是客户端自己发的预热帧（generate=false）。
// 只有官方 Codex 客户端会在会话开头发这种帧，第三方客户端从不发，所以它是“该客户端自己管理续链”的证据。
func openAIWSPayloadIsClientPrewarm(payload []byte) bool {
	generate := gjson.GetBytes(payload, "generate")
	return generate.Exists() && generate.Type == gjson.False
}

// shouldReplayOnSameOpenAIWSConnection 判断一帧下游 response.create 是否是“自管续链客户端的整段重放帧”：
// 此时网关应复用原上游连接并原样转发，不新建连接、不补预热、不链 previous_response_id。
// 全部条件同时成立才返回 true：
//   - enabled：画像声明了 ReplayOnSameConnection，且本会话走派生官方出站；
//   - clientOwnsChain：本会话内下游客户端已经自己发过 generate=false 预热帧；
//   - 帧不带 previous_response_id、不带 generate（自己发的预热帧不算，它按原逻辑处理）；
//   - 帧不是工具结果续轮（工具续轮有自己的续链收敛，不在此列）。
func shouldReplayOnSameOpenAIWSConnection(
	enabled bool,
	clientOwnsChain bool,
	payload []byte,
	currentPreviousResponseID string,
	hasFunctionCallOutput bool,
) bool {
	if !enabled || !clientOwnsChain || hasFunctionCallOutput {
		return false
	}
	if strings.TrimSpace(currentPreviousResponseID) != "" {
		return false
	}
	return !gjson.GetBytes(payload, "generate").Exists()
}

// officialCodexWebSocketContinuationAuthRevision 在画像声明 auth_revision 失效条件时返回握手
// Bearer 凭据的摘要，作为连接池键中的认证代次；其他情况返回空串。Agent Identity 断言每次
// 签发都不同，不代表认证代次变化，不计入。
func officialCodexWebSocketContinuationAuthRevision(mode string, headers http.Header) string {
	if !officialCodexWebSocketContinuationResetsOn(mode, officialCodexContinuationResetAuthRevision) {
		return ""
	}
	authorization := strings.TrimSpace(headers.Get("Authorization"))
	if !strings.HasPrefix(authorization, "Bearer ") {
		return ""
	}
	sum := sha256.Sum256([]byte(authorization))
	return hex.EncodeToString(sum[:12])
}

// officialEgressWebSocketPoolTransportKey 组合官方出站 WS 连接池的硬兼容键：连接池 ID、
// 代理状态、会话身份，以及画像声明时的认证代次。
func officialEgressWebSocketPoolTransportKey(
	egressContext *OfficialEgressContext,
	proxyURL string,
	headers http.Header,
) string {
	key := egressContext.connectionPoolID +
		"|proxy_state=" + officialEgressProxyStateKey(stringsTrim(proxyURL))
	if identityKey := officialEgressWebSocketIdentityKey(egressContext); identityKey != "" {
		key += "|identity=" + identityKey
	}
	if revision := officialCodexWebSocketContinuationAuthRevision(egressContext.ProfileMode(), headers); revision != "" {
		key += "|auth_revision=" + revision
	}
	return key
}
