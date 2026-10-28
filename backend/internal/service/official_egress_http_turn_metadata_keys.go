package service

import (
	"strings"

	"github.com/gin-gonic/gin"
)

// HTTP 回合请求 turn metadata 的窗口与根线程键（画像 TurnMetadata 节 WindowTracking／RootThreadKeys）。
//
// 官方 0.165 的 HTTP /responses 请求（含 HTTP 压缩请求）的 turn metadata 与 WS 帧一样带：
//   - window_number、context_window_id：窗口号就是 x-codex-window-id 的 `<线程>:<窗口号>` 后缀，上下文压缩后加一，
//     上下文窗口 id 每个窗口一个、窗口内各请求相同（实采 HTTP 压缩续聊会话：压缩请求仍在窗口 0，压缩后的请求窗口 1 且换新 id）；
//   - 根线程另带 agent_name（`/root`）与 root_turn_id（等于 turn_id，压缩请求同样）。
//
// 网关的 HTTP 身份由网关自己派生，此前窗口号恒为 0。画像声明 WindowTracking 后，窗口号跟随下游客户端 x-codex-window-id 头的
// 数字后缀（HTTP 无连接状态，只能无状态读取；只采数字，线程部分仍是网关派生的线程标识）；上下文窗口 id 与 WS 同一个派生函数，
// 由（会话、窗口号）稳定生成，同一窗口内各请求与重试相同。子代理、guardian 与记忆合并线程没有官方样本，不写 agent_name／root_turn_id。
// 画像没有声明时本文件的入口都是恒等操作，窗口号与 turn metadata 保持旧行为。

// officialCodexHTTPClientWindowNumber 返回 HTTP 请求应使用的窗口号：画像声明 WindowTracking 且下游带可解析的
// x-codex-window-id 头时取其数字后缀，否则为 0。
func officialCodexHTTPClientWindowNumber(c *gin.Context, profileMode string) int {
	if c == nil || !officialCodexTurnMetadataWindowTracking(profileMode) {
		return 0
	}
	number, ok := officialCodexWindowNumberFromID(c.GetHeader("x-codex-window-id"))
	if !ok {
		return 0
	}
	return number
}

// fillOfficialCodexHTTPTurnMetadataIdentityKeys 把 HTTP 身份里的窗口号、上下文窗口 id 与根线程键放进 turn metadata 扩展输入。
// 是否真正写入由画像节的开关决定（applyOfficialCodexTurnMetadataSection）；这里只提供有可信来源的取值。
func fillOfficialCodexHTTPTurnMetadataIdentityKeys(
	extension *officialCodexTurnMetadataExtension,
	identity officialOpenAIHTTPIdentity,
) {
	if number, ok := officialCodexWindowNumberFromID(identity.windowID); ok {
		extension.WindowNumber = &number
		extension.ContextWindowID = officialCodexContextWindowID(identity.sessionID, number)
	}
	if strings.TrimSpace(identity.subagent) == "" && strings.TrimSpace(identity.parentThreadID) == "" && !identity.memoryGenerate {
		extension.AgentName = officialCodexRootAgentName
		extension.RootTurnID = identity.turnID
	}
}
