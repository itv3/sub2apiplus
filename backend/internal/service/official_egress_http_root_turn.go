package service

import (
	"slices"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/google/uuid"
)

// HTTP 回合请求 client_metadata 的 root_turn_id（画像 ClientMetadata.DynamicKeys 声明 root_turn_id 时）。
//
// 官方 0.165 的 HTTP /responses 回合请求（含 HTTP 压缩请求）的 client_metadata 与 WS 帧一样带 root_turn_id：根线程上等于 turn_id
// （实采 t1h／c3／h1／h2／e2／x2 全部如此）。网关出站时本会话的身份（会话、线程、回合 id）整体由网关自己派生，入站客户端的 id 不会
// 原样透出，所以入站带的 root_turn_id 不能采用（它指向客户端自己的回合，与出站 turn_id 对不上）。这里只按出站身份取值：
// 根线程（无子代理、无父线程、非记忆合并）的根回合 id 就是本请求出站的 turn_id；子代理、guardian 与记忆合并线程的根回合是另一条线程
// 的回合，网关没有可信来源也没有官方样本，不写。
//
// 画像没有声明该键时本文件的入口返回空，事实不进入身份，旧画像的出站字节与摘要不变。

// officialCodexClientMetadataDeclaresDynamicKey 判断 mode 对应画像的 ClientMetadata 节是否声明了动态键 key。
func officialCodexClientMetadataDeclaresDynamicKey(mode, key string) bool {
	if strings.TrimSpace(mode) == "" {
		return false
	}
	section := officialCodexOptionalSectionsForMode(mode).ClientMetadata
	return section != nil && slices.Contains(section.DynamicKeys, key)
}

// officialCodexHTTPRootTurnID 取 HTTP 请求的根回合 id；画像未声明、不是根线程或回合 id 不合法时返回空串。
func officialCodexHTTPRootTurnID(
	profileMode string,
	facts officialegress.CodexIdentityFacts,
	memoryGeneration bool,
) string {
	if !officialCodexClientMetadataDeclaresDynamicKey(profileMode, "root_turn_id") {
		return ""
	}
	if facts.Subagent.Value != "" || facts.ParentThreadID.Value != "" || memoryGeneration {
		return ""
	}
	turnID := strings.TrimSpace(facts.TurnID.Value)
	if _, err := uuid.Parse(turnID); err != nil {
		return ""
	}
	return turnID
}
