package service

import (
	"context"
	"strconv"
	"strings"
	"sync"

	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// WS 回合元数据的上下文窗口跟踪（画像 TurnMetadata 节 WindowTracking）。
//
// 官方客户端用“窗口号”和“上下文窗口 id”标识上下文窗口：窗口号是 x-codex-window-id 的 `<线程>:<窗口号>` 后缀，
// 也写在 turn metadata 的 window_id 与 window_number 里；上下文窗口 id 是 turn metadata 的 context_window_id，
// 每个窗口一个 UUIDv7。上下文压缩后进入新窗口：窗口号加一、上下文窗口 id 换新，之后的请求与新建连接的握手都用新值。
// 握手头只在建连时发出，复用的连接上握手里仍是旧窗口号，新窗口号只出现在每帧 client_metadata 里。
//
// 网关的窗口身份是连接级冻结字段（`<线程>:0`），不会自己推进。画像声明 WindowTracking 后：
//   - 窗口号跟随下游客户端自己的窗口号：入站帧 client_metadata 里 x-codex-window-id 的数字后缀比已记录的大，
//     就推进到该值（只记数字，线程部分仍是网关派生的线程标识，不采纳客户端的标识）；
//   - 上下文窗口 id 由（会话、窗口号）稳定派生，同一窗口内各帧与重试相同，换窗口换新；
//   - 帧里的窗口头、turn metadata 的 window_id／window_number／context_window_id，以及推进之后新建连接握手里的窗口头与
//     turn metadata，都用推进后的值。
// 画像没有该字段时本文件的入口都是恒等操作，窗口身份保持旧行为。

// officialUUIDV7DomainContextWindow 是上下文窗口 id 的种子用途域，与会话、轮次身份的用途域互不碰撞。
const officialUUIDV7DomainContextWindow = "openai-official-egress-context-window"

// officialCodexWSWindowTracker 记录当前下游 WS 会话已推进到的窗口号。零值可用。
type officialCodexWSWindowTracker struct {
	mu     sync.Mutex
	number int
}

// observe 读取入站帧自带的窗口号：比已记录的大就推进，返回当前窗口号。无法解析时不改变记录。
func (t *officialCodexWSWindowTracker) observe(clientWindowID string) int {
	t.mu.Lock()
	defer t.mu.Unlock()
	if number, ok := officialCodexWindowNumberFromID(clientWindowID); ok && number > t.number {
		t.number = number
	}
	return t.number
}

func (t *officialCodexWSWindowTracker) current() int {
	t.mu.Lock()
	defer t.mu.Unlock()
	return t.number
}

// officialCodexWindowNumberFromID 取 `<线程>:<窗口号>` 的窗口号。
func officialCodexWindowNumberFromID(id string) (int, bool) {
	id = strings.TrimSpace(id)
	index := strings.LastIndex(id, ":")
	if index <= 0 || index == len(id)-1 {
		return 0, false
	}
	number, err := strconv.Atoi(id[index+1:])
	if err != nil || number < 0 {
		return 0, false
	}
	return number, true
}

// officialCodexWindowIDWithNumber 把 base（`<线程>:<窗口号>`）的窗口号换成 number；base 不是该形态时原样返回。
func officialCodexWindowIDWithNumber(base string, number int) string {
	index := strings.LastIndex(base, ":")
	if index <= 0 {
		return base
	}
	if _, ok := officialCodexWindowNumberFromID(base); !ok {
		return base
	}
	return base[:index] + ":" + strconv.Itoa(number)
}

// officialCodexContextWindowID 派生上下文窗口 id：由（会话、窗口号）稳定生成的 UUIDv7。
func officialCodexContextWindowID(sessionID string, number int) string {
	return generateOfficialStableUUIDV7(
		newOfficialUUIDV7Seed(officialUUIDV7DomainContextWindow).WriteString(sessionID).WriteInt(number).Key(),
	)
}

// officialCodexTurnMetadataWindowTracking 判断 mode 对应画像是否声明跟踪上下文窗口。
func officialCodexTurnMetadataWindowTracking(mode string) bool {
	if strings.TrimSpace(mode) == "" {
		return false
	}
	section := officialCodexOptionalSectionsForMode(mode).TurnMetadata
	return section != nil && section.WindowTracking
}

// observeOfficialCodexWSClientWindow 读取下游客户端原帧自带的窗口号并推进记录。必须读客户端原帧：进入帧定型的候选帧里
// 客户端的窗口头已被账号身份隔离改写成不透明的 UUID，数字后缀已经丢失。画像未声明或会话没有派生状态时不做任何事。
func observeOfficialCodexWSClientWindow(ctx context.Context, originalFrame []byte) {
	egressContext, ok := OfficialEgressContextFromContext(ctx)
	if !ok || egressContext == nil || egressContext.openAIWSDerived == nil ||
		!officialCodexTurnMetadataWindowTracking(egressContext.ProfileMode()) {
		return
	}
	egressContext.openAIWSDerived.windows.observe(gjson.GetBytes(originalFrame, "client_metadata.x-codex-window-id").String())
}

// officialCodexWSFrameWindow 返回本帧使用的窗口身份。画像未声明、或会话没有派生状态时 tracked 为 false，
// 窗口号原样返回；否则按已记录的窗口号返回窗口头、窗口号与上下文窗口 id。
func officialCodexWSFrameWindow(
	egressContext *OfficialEgressContext,
	baseWindowID string,
	sessionID string,
) (windowID string, number int, contextWindowID string, tracked bool) {
	if egressContext == nil || egressContext.openAIWSDerived == nil ||
		!officialCodexTurnMetadataWindowTracking(egressContext.ProfileMode()) {
		return baseWindowID, 0, "", false
	}
	number = egressContext.openAIWSDerived.windows.current()
	return officialCodexWindowIDWithNumber(baseWindowID, number), number,
		officialCodexContextWindowID(sessionID, number), true
}

// officialCodexWSAdvancedRegisteredField 把连接级登记字段换成窗口推进之后的值，供新建连接的握手使用：
// 窗口头取推进后的窗口号；握手的 turn metadata 里的 window_id、window_number、context_window_id 同步换成推进后的值。
// 画像未声明、会话没有派生状态或窗口号还没推进时原样返回。
func officialCodexWSAdvancedRegisteredField(
	egressContext *OfficialEgressContext,
	name OfficialEgressFieldName,
	value string,
) string {
	if egressContext == nil || egressContext.openAIWSDerived == nil ||
		!officialCodexTurnMetadataWindowTracking(egressContext.ProfileMode()) {
		return value
	}
	number := egressContext.openAIWSDerived.windows.current()
	if number <= 0 {
		return value
	}
	switch name {
	case OfficialEgressFieldWindowID:
		return officialCodexWindowIDWithNumber(value, number)
	case OfficialEgressFieldTurnMetadata:
		raw := []byte(value)
		if !gjson.ValidBytes(raw) {
			return value
		}
		if current := gjson.GetBytes(raw, "window_id"); current.Exists() {
			raw, _ = sjson.SetBytes(raw, "window_id", officialCodexWindowIDWithNumber(current.String(), number))
		}
		if gjson.GetBytes(raw, "window_number").Exists() {
			raw, _ = sjson.SetBytes(raw, "window_number", number)
		}
		if gjson.GetBytes(raw, "context_window_id").Exists() {
			session := gjson.GetBytes(raw, "session_id").String()
			raw, _ = sjson.SetBytes(raw, "context_window_id", officialCodexContextWindowID(session, number))
		}
		return string(raw)
	}
	return value
}

// officialCodexWSHandshakeTurnMetadataExtension 给连接握手的 turn metadata 扩展补上窗口身份：窗口号 0 与对应的上下文窗口 id。
// 画像未声明跟踪上下文窗口时原样返回。
func officialCodexWSHandshakeTurnMetadataExtension(
	egressContext *OfficialEgressContext,
	sessionID string,
	extension officialCodexTurnMetadataExtension,
) officialCodexTurnMetadataExtension {
	if egressContext == nil {
		return extension
	}
	if officialCodexRootThreadDeclared(egressContext) {
		extension.AgentName = officialCodexRootAgentName
	}
	if !officialCodexTurnMetadataWindowTracking(egressContext.ProfileMode()) {
		return extension
	}
	number := 0
	extension.WindowNumber = &number
	extension.ContextWindowID = officialCodexContextWindowID(sessionID, number)
	return extension
}

// officialCodexRootAgentName 是根线程在 turn metadata 里的 agent_name（官方 0.165 实采：TUI 与 exec 的主线程都是 /root）。
const officialCodexRootAgentName = "/root"

// officialCodexWSRootThread 判断会话是不是根线程：画像声明 RootThreadKeys，且没有子代理（含 guardian）与记忆合并标记。
// 子代理、guardian 审阅与记忆合并线程的 agent_name／root_turn_id 没有官方样本，网关不写。
func officialCodexWSRootThread(egressContext *OfficialEgressContext) bool {
	if egressContext == nil || egressContext.openAIWSDerived == nil {
		return false
	}
	return officialCodexRootThreadDeclared(egressContext)
}

// officialCodexRootThreadDeclared 与 officialCodexWSRootThread 的区别是不要求会话已建好派生状态：连接握手的 turn metadata
// 在登记会话身份的过程中生成，此时派生状态还没有挂上。
func officialCodexRootThreadDeclared(egressContext *OfficialEgressContext) bool {
	if egressContext == nil {
		return false
	}
	section := officialCodexOptionalSectionsForMode(egressContext.ProfileMode()).TurnMetadata
	if section == nil || !section.RootThreadKeys {
		return false
	}
	conditional := egressContext.codexRuntimeState.ConditionalHeaders
	return strings.TrimSpace(conditional["x-openai-subagent"]) == "" &&
		!strings.EqualFold(strings.TrimSpace(conditional["x-openai-memgen-request"]), "true")
}
