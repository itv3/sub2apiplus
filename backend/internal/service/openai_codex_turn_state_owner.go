package service

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// turn-state 按账号 owner 隔离（画像 TurnState 节）。
//
// 官方客户端在认证 owner 变化时丢弃已缓存的 turn-state 与 WS 会话，旧值不再回送。
// 网关里“owner”就是本次出站选定的上游账号：HTTP 持久化 turn-state 的存储键已含本地账号，
// 原生 WS 的 turn-state 只来自当前连接的握手与事件流，二者天然按账号隔离；剩下需要按画像
// 收紧的是 WS 入口两处可能跨账号带出的值——客户端回带的 turn-state（账号 failover 后仍可能
// 是旧账号铸造的）与无法归属账号的会话级缓存值。画像没有该节时行为与改动前一致。

// officialCodexTurnStateOwnerIsolation 判断 mode 对应画像是否声明 turn-state 按账号 owner 隔离。
func officialCodexTurnStateOwnerIsolation(mode string) bool {
	section := officialCodexOptionalSectionsForMode(mode).TurnState
	return section != nil && section.ResetOnAccountOwnerChange
}

// isolateOfficialCodexIngressTurnState 丢弃已知由其他账号铸造的 turn-state；同账号铸造或
// 没有溯源记录时原样返回。溯源表与透传路径的出站守卫共用（按下游会话记录最近铸造账号）。
func (s *OpenAIGatewayService) isolateOfficialCodexIngressTurnState(
	c *gin.Context,
	account *Account,
	turnState string,
) string {
	turnState = strings.TrimSpace(turnState)
	if turnState == "" {
		return ""
	}
	headers := http.Header{}
	headers.Set(openAICodexTurnStateHeader, turnState)
	s.guardOpenAICodexTurnStateEcho(c, account, headers)
	return strings.TrimSpace(headers.Get(openAICodexTurnStateHeader))
}
