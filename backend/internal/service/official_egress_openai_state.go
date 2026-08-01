package service

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

// officialOpenAIHTTPTurnStateStoreKey 把会话与 turn 身份转换为不可逆的本地键。
// turn-state 只允许在同一 turn 的 responses、retry 与 legacy compact 间复用；
// 不能只按账号或 session 保存，否则下一轮用户输入会误带上一轮状态。
func officialOpenAIHTTPTurnStateStoreKey(identity officialOpenAIHTTPIdentity) string {
	sessionID := strings.TrimSpace(identity.sessionID)
	turnID := strings.TrimSpace(identity.turnID)
	if sessionID == "" || turnID == "" {
		return ""
	}
	// 会话身份若是从消息内容兜底推导出来的，它只保证“同内容得到同 ID”，并不保证
	// “同 ID 属于同一个会话”：同账号、同 API Key、同 UA 下两段以相同开头且当前末条
	// 消息也相同的独立对话，会算出同一个键，从而共享 turn-state。turn-state 是上游
	// 的会话状态句柄，串用比不用更糟。
	//
	// 放弃复用的代价很小：缺少 turn-state 时按 §3.1 不发该条件头，而这本就是官方
	// 最常见的合法形态。带显式会话锚点（入站会话头或 prompt_cache_key）的请求不受
	// 影响，工具结果续轮仍然复用同一个 turn。
	if !identity.sessionAnchorExplicit {
		return ""
	}
	digest := sha256.Sum256([]byte(sessionID + "\x00" + turnID))
	return "http-turn:" + hex.EncodeToString(digest[:16])
}
