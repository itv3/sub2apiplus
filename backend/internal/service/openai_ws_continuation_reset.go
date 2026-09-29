package service

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"slices"
	"strings"
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

const officialCodexContinuationResetAuthRevision = "auth_revision"

// officialCodexWebSocketContinuationResetsOn 判断 mode 对应画像是否声明指定续接失效条件。
func officialCodexWebSocketContinuationResetsOn(mode string, kind string) bool {
	if strings.TrimSpace(mode) == "" {
		return false
	}
	section := officialCodexOptionalSectionsForMode(mode).WebSocketContinuation
	return section != nil && slices.Contains(section.ResetOn, kind)
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
