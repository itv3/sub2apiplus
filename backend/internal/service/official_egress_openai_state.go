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
	digest := sha256.Sum256([]byte(sessionID + "\x00" + turnID))
	return "http-turn:" + hex.EncodeToString(digest[:16])
}
