package service

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/json"
	"errors"
	"strconv"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
)

// 增量窗口前缀（画像 BaseInstructions 节 IDScheme=uuidv7，且本次请求走 Lite）。
//
// 较新版本的官方客户端对 Lite 模型默认走“增量工具窗口”：工具目录（additional_tools）和基础指令消息在窗口开头
// 作为历史项记录一次，id 是记录时随机生成的 前缀_UUIDv7，之后每个请求都按同一个 id 原样重放；从旧版本续聊的
// 旧窗口仍然是“每请求现场拼装、id 由线程与内容算出的 UUIDv5”。同一个客户端因此会在网关面前同时出现两种前缀形态，
// 网关不能按版本号二选一，而要按每条入站项的实际形态处理：
//
//   - 入站已是官方形态的项（Codex 客户端自带，不论是 UUIDv7 还是 UUIDv5 的 id）：除基础指令消息自己的 passthrough.turn_id
//     外逐字节保留，不重算 id、不改键序和其余各键。turn_id 例外：它属于客户端自己的轮次命名空间，官方首轮请求里它与其余项、
//     client_metadata.turn_id 完全相同，所以统一改写成网关的"窗口第一段"turn_id（见下面的 turn_id 说明）；
//   - 入站没有官方形态的项（第三方客户端）：才合成。合成 id 是 前缀_UUIDv7，由 (线程, 内容) 的摘要为种子在进程内按种子
//     稳定复用（generateOfficialStableUUIDV7）：时间戳位是第一次合成的时刻，同一线程同一内容在之后各请求上 id 相同。
//     不能每次现生成随机值——id 在 input 最前面，随机 id 会让上游的前缀缓存逐请求失效。
//
// 非 Lite 模型不走增量窗口：官方客户端对它们仍每请求现场拼装 UUIDv5 的基础指令消息，网关沿用旧逻辑。
// 画像不声明 uuidv7 方案时，本文件的任何函数都不会被调用，出站字节与引入本方案之前逐字节相同。

const (
	// officialUUIDV7DomainBaseInstructions／officialUUIDV7DomainAdditionalTools 是合成 id 的种子用途域，
	// 与会话、轮次身份的用途域互不碰撞（见 official_egress_uuid.go）。
	officialUUIDV7DomainBaseInstructions     = "openai-official-egress-base-instructions"
	officialUUIDV7DomainBaseInstructionsTurn = "openai-official-egress-base-instructions-turn"
	officialUUIDV7DomainAdditionalTools      = "openai-official-egress-additional-tools"

	// officialCodexAdditionalToolsIDPrefix 是官方 AdditionalTools 项 id 的前缀（官方客户端按项类型固定，不随画像变化）。
	officialCodexAdditionalToolsIDPrefix = "at"

	// officialCodexBaseInstructionsContentItemMetadata 是官方客户端给基础指令消息写的 content_item_metadata：
	// 基础指令由 harness 自己生成（不是 hook），按 ContentItemMetadata::harness() 序列化。
	officialCodexBaseInstructionsContentItemMetadata = `[{"provenance":{"type":"harness","hook":false}}]`

	// officialCodexInboundBaseInstructionsRawLimit 限制为“原样保留”而复制的入站基础指令消息大小。官方基础指令只有
	// 几十 KB；超过上限的带标记项不会被当成官方形态，按第三方入站合成，避免为异常入站复制大块正文。
	officialCodexInboundBaseInstructionsRawLimit = 1 << 20
)

// officialCodexIncrementalPrefix 判断本次请求（HTTP）或本条连接（WS）是否按增量窗口形态承载前缀。
// section 为 nil（画像没有该节或没有列出该端点）时恒为 false。
func officialCodexIncrementalPrefix(section *profilecontract.BaseInstructionsSection, lite bool) bool {
	return lite && section.UsesUUIDv7IDs()
}

// officialCodexStableItemID 生成合成项的 id：前缀_UUIDv7。种子由用途域、线程 id 和内容摘要构成，
// 同一线程同一内容在进程内（滑动 24 小时）得到同一个 UUIDv7。
func officialCodexStableItemID(prefix, domain, threadID string, digest officialContentDigest) string {
	key := newOfficialUUIDV7Seed(domain).WriteString(threadID).WriteDigest(digest).Key()
	return prefix + "_" + generateOfficialStableUUIDV7(key)
}

// renderOfficialCodexIncrementalBaseInstructionsItem 为第三方入站合成官方 0.164 增量窗口形态的基础指令消息（预编码 JSON），
// turn_id 取按 (线程, 正文) 稳定的 UUIDv7。没有"窗口第一段"turn_id 可用的场景（例如 WS 预热帧）才走这个入口，
// 其余场景见 renderOfficialCodexIncrementalBaseInstructionsItemWithTurnID。
func renderOfficialCodexIncrementalBaseInstructionsItem(
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	text string,
) (json.RawMessage, error) {
	return renderOfficialCodexIncrementalBaseInstructionsItemWithTurnID(section, threadID, text, "")
}

// renderOfficialCodexIncrementalBaseInstructionsItemWithTurnID 为第三方入站合成官方 0.164 增量窗口形态的基础指令消息（预编码 JSON）：
//
//	{"type":"message","id":"msg_<UUIDv7>","role":"developer","content":[{"type":"input_text","text":"<正文>"}],
//	 "internal_chat_message_metadata_passthrough":{"turn_id":"<UUIDv7>","create_time":<秒>,
//	 "content_item_kinds":["model.base_instructions"],"content_item_metadata":[{"provenance":{"type":"harness","hook":false}}]}}
//
// 官方客户端把这条消息作为历史项记录一次，之后各请求原样重放，所以 id、create_time 在整个窗口内都不变。
// 网关同样要求它们按 (线程, 正文) 稳定，否则 input 最前面的几十个字节逐请求变化，上游前缀缓存会整体失效：
//   - id：前缀加稳定 UUIDv7（时间戳位是第一次合成的时刻）；
//   - create_time：取 id 里的毫秒时间戳，再按正文摘要补出毫秒以内的小数（官方是纳秒精度的 f64 秒数，与 id 同一时刻生成）；
//   - turn_id：官方里它是"窗口第一个回合"的 id，首轮请求中与其余项、client_metadata.turn_id 完全相同，多轮时等于第一段
//     历史的 id。turnID 非空时直接用调用方给的"窗口第一段"网关 turn_id（HTTP 与 WS 各自按第一段的派生规则算出，
//     见 officialOpenAIHTTPFirstSegmentTurnID、officialOpenAIWSFirstSegmentTurnID）；为空时退回按 (线程, 正文) 稳定的
//     UUIDv7（调用方拿不到回合身份的场景，例如 WS 预热帧）。
//
// 线程缺失时失败关闭。
func renderOfficialCodexIncrementalBaseInstructionsItemWithTurnID(
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	text string,
	turnID string,
) (json.RawMessage, error) {
	if strings.TrimSpace(threadID) == "" {
		return nil, errors.New("OpenAI official egress base instructions require a thread id")
	}
	digest := officialContentDigest(sha256.Sum256([]byte(text)))
	id := officialCodexStableItemID(section.IDPrefix, officialUUIDV7DomainBaseInstructions, threadID, digest)
	if strings.TrimSpace(turnID) == "" {
		turnID = generateOfficialStableUUIDV7(
			newOfficialUUIDV7Seed(officialUUIDV7DomainBaseInstructionsTurn).WriteString(threadID).WriteDigest(digest).Key(),
		)
	}

	passthrough := append([]byte(`{"turn_id":`), appendOfficialCodexJSONString(nil, turnID)...)
	passthrough = append(passthrough, `,"create_time":`...)
	passthrough = officialCodexAppendCreateTimeFromID(passthrough, section.IDPrefix, id, digest)
	passthrough = append(passthrough, `,"content_item_kinds":[`...)
	passthrough = appendOfficialCodexJSONString(passthrough, section.ContentItemKind)
	passthrough = append(passthrough, `],"content_item_metadata":`...)
	passthrough = append(passthrough, officialCodexBaseInstructionsContentItemMetadata...)
	passthrough = append(passthrough, '}')
	return renderOfficialCodexBaseInstructionsItemWithPassthrough(id, text, passthrough), nil
}

// rewriteOfficialCodexBaseInstructionsRawTurnID 把入站官方形态基础指令消息原文里的 passthrough.turn_id 换成 turnID，
// 其余字节（id、键序、其余各键、空白）逐字节不动：只替换这一个字符串值的区间。
//
// 为什么要改：turn_id 属于客户端自己的轮次命名空间；官方首轮请求里基础指令消息的 turn_id 与其余项、
// client_metadata.turn_id 完全相同，网关出站的其余项用的是网关派生的 turn_id，只有这一项还是客户端的值，
// 既造成"同一请求里 turn_id 不一致"这一可识别特征，也把客户端命名空间的 id 泄给上游。
//
// 没有 turn_id 键（旧窗口的基础指令消息 passthrough 只有 content_item_kinds 与 content_item_metadata，官方实采如此）、
// turnID 为空、turn_id 不是字符串时原样返回，不补键。返回值总是新切片，不引用入参。
func rewriteOfficialCodexBaseInstructionsRawTurnID(raw json.RawMessage, turnID string) json.RawMessage {
	if strings.TrimSpace(turnID) == "" {
		return raw
	}
	value := gjson.GetBytes(raw, "internal_chat_message_metadata_passthrough.turn_id")
	if !value.Exists() || value.Type != gjson.String || value.Index <= 0 {
		return raw
	}
	encoded := appendOfficialCodexJSONString(nil, turnID)
	out := make([]byte, 0, len(raw)+len(encoded))
	out = append(out, raw[:value.Index]...)
	out = append(out, encoded...)
	out = append(out, raw[value.Index+len(value.Raw):]...)
	return out
}

// rewriteOfficialCodexBaseInstructionsMapTurnIDs 把 input 里仍以对象形态存在的基础指令消息（没有被安装步骤重建的残留项，
// 例如入站消息的正文与实际使用的指令不同、不是官方形态因而没有被替换）的 passthrough.turn_id 也改写成 turnID，
// 避免客户端的 turn_id 漏到上游。只改已有的 turn_id 键，不补键。返回是否有改动。
func rewriteOfficialCodexBaseInstructionsMapTurnIDs(
	payload map[string]any,
	section *profilecontract.BaseInstructionsSection,
	turnID string,
) bool {
	if section == nil || strings.TrimSpace(turnID) == "" {
		return false
	}
	input, ok := payload["input"].([]any)
	if !ok {
		return false
	}
	modified := false
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || !isOfficialCodexBaseInstructionsItem(section, item) {
			continue
		}
		passthrough, ok := item[officialOpenAIWSItemTurnMetadata].(map[string]any)
		if !ok {
			continue
		}
		if current, exists := passthrough["turn_id"]; exists && current != turnID {
			passthrough["turn_id"] = turnID
			modified = true
		}
	}
	return modified
}

// officialCodexAppendCreateTimeFromID 追加 create_time（Unix 秒的小数）：整数毫秒取自 前缀_UUIDv7 的 48 位时间戳，毫秒以内的
// 小数部分由摘要决定（同一正文每次相同）。数字用最短往返表示、不带指数，与官方 f64 序列化同形（如 1793034902.6807268）。
func officialCodexAppendCreateTimeFromID(dst []byte, prefix, id string, digest officialContentDigest) []byte {
	var milliseconds uint64
	if parsed, err := uuid.Parse(strings.TrimPrefix(id, prefix+"_")); err == nil {
		for _, b := range parsed[:6] {
			milliseconds = milliseconds<<8 | uint64(b)
		}
	}
	subMillisecond := float64(binary.BigEndian.Uint32(digest[:4])%1_000_000) / 1_000_000
	return strconv.AppendFloat(dst, (float64(milliseconds)+subMillisecond)/1000, 'f', -1, 64)
}

// officialCodexBaseInstructionsItemKeyAllowed 判断键是否可以出现在官方基础指令消息上：type 为 message 的 Message 项里，
// 基础指令消息不会有 status、phase、encrypted_content 等其他成员。
func officialCodexBaseInstructionsItemKeyAllowed(key string) bool {
	switch key {
	case "type", "id", "role", "content", "internal_chat_message_metadata_passthrough":
		return true
	}
	return false
}

// officialCodexBaseInstructionsPassthroughKeyAllowed 判断键是否可以出现在基础指令消息的 passthrough 里：turn_id、
// create_time、content_item_kinds、content_item_metadata。cell_id、executed_tool_calls、tool_calls_complete 属于工具调用项，
// 出现在基础指令消息上说明不是官方形态。
func officialCodexBaseInstructionsPassthroughKeyAllowed(key string) bool {
	switch key {
	case "turn_id", "create_time", "content_item_kinds", "content_item_metadata":
		return true
	}
	return false
}

// officialCodexBaseInstructionsContentPartKeyAllowed 判断键是否可以出现在基础指令消息的 input_text 片段上。
func officialCodexBaseInstructionsContentPartKeyAllowed(key string) bool {
	return key == "type" || key == "text"
}

// objectKeysWithin 判断 JSON 对象的全部键都满足 allowed。
func objectKeysWithin(object gjson.Result, allowed func(key string) bool) bool {
	within := true
	object.ForEach(func(key, _ gjson.Result) bool {
		if !allowed(key.String()) {
			within = false
		}
		return within
	})
	return within
}

// isOfficialCodexOfficialBaseInstructionsForm 判断一条入站 input 项是否是官方客户端发出的基础指令消息：
// developer 消息，键集合与官方一致，id 是 前缀_UUID，content 恰好一个与 text 逐字相同的 input_text 片段，
// passthrough 的 content_item_kinds 恰好是节声明的标记。不满足任一条件都当作非官方形态，由调用方合成。
//
// id 只校验形状（前缀加规范写法的 UUID），不区分 UUIDv5 与 UUIDv7：旧窗口、非 Lite 请求里官方客户端发的是 UUIDv5，
// 同样是官方形态。
func isOfficialCodexOfficialBaseInstructionsForm(
	section *profilecontract.BaseInstructionsSection,
	raw []byte,
	text string,
) bool {
	item := gjson.ParseBytes(raw)
	if !item.IsObject() || !objectKeysWithin(item, officialCodexBaseInstructionsItemKeyAllowed) {
		return false
	}
	if kind := item.Get("type"); kind.Type != gjson.String || kind.String() != "message" {
		return false
	}
	if role := item.Get("role"); role.Type != gjson.String || role.String() != "developer" {
		return false
	}
	id := item.Get("id")
	prefix := section.IDPrefix + "_"
	if id.Type != gjson.String || !strings.HasPrefix(id.String(), prefix) {
		return false
	}
	if suffix := id.String()[len(prefix):]; len(suffix) != 36 {
		return false
	} else if _, err := uuid.Parse(suffix); err != nil {
		return false
	}
	content := item.Get("content")
	if !content.IsArray() {
		return false
	}
	parts := content.Array()
	if len(parts) != 1 || !parts[0].IsObject() {
		return false
	}
	part := parts[0]
	if !objectKeysWithin(part, officialCodexBaseInstructionsContentPartKeyAllowed) ||
		part.Get("type").String() != "input_text" || part.Get("text").Type != gjson.String ||
		part.Get("text").String() != text {
		return false
	}
	passthrough := item.Get("internal_chat_message_metadata_passthrough")
	if !passthrough.IsObject() || !objectKeysWithin(passthrough, officialCodexBaseInstructionsPassthroughKeyAllowed) {
		return false
	}
	if value := passthrough.Get("turn_id"); value.Exists() && value.Type != gjson.String {
		return false
	}
	if value := passthrough.Get("create_time"); value.Exists() && value.Type != gjson.Number {
		return false
	}
	if value := passthrough.Get("content_item_metadata"); value.Exists() && !value.IsArray() {
		return false
	}
	kinds := passthrough.Get("content_item_kinds").Array()
	return len(kinds) == 1 && kinds[0].Type == gjson.String && kinds[0].String() == section.ContentItemKind
}

// officialCodexInboundOfficialBaseInstructionsFromFrame 在入站帧（或请求体）原文的 input 前几项里找官方形态的基础指令消息，
// 返回它的原文（复制，不引用入参）。text 必须是本次实际使用的基础指令正文：入站官方消息的正文与它不同时（顶层 instructions
// 优先于消息）不返回，让调用方按正文合成。找不到返回 nil。
func officialCodexInboundOfficialBaseInstructionsFromFrame(
	original []byte,
	section *profilecontract.BaseInstructionsSection,
	text string,
) json.RawMessage {
	if len(original) == 0 || text == "" {
		return nil
	}
	for position := 0; position < officialCodexInboundPrefixScanLimit; position++ {
		item := gjson.GetBytes(original, "input."+strconv.Itoa(position))
		if !item.Exists() {
			return nil
		}
		if len(item.Raw) > officialCodexInboundBaseInstructionsRawLimit {
			continue
		}
		if isOfficialCodexOfficialBaseInstructionsForm(section, []byte(item.Raw), text) {
			return json.RawMessage(bytes.Clone([]byte(item.Raw)))
		}
	}
	return nil
}

// inboundOfficialBaseInstructionsItem 返回契约在入站 input 前缀里记录的、正文与 text 相同的官方形态基础指令消息原文。
func (c *officialOpenAIHTTPBodyContract) inboundOfficialBaseInstructionsItem(
	section *profilecontract.BaseInstructionsSection,
	text string,
) json.RawMessage {
	for _, message := range c.inboundTaggedMessages {
		if len(message.raw) == 0 || message.text != text {
			continue
		}
		if isOfficialCodexOfficialBaseInstructionsForm(section, message.raw, text) {
			return message.raw
		}
	}
	return nil
}

// installOfficialCodexIncrementalBaseInstructions 把基础指令消息安装到 payload["input"]：official 非空时就是入站官方形态消息
// 的原文，除 passthrough.turn_id 外逐字节放进去；否则按第三方入站合成（稳定 UUIDv7 id）。两种情况下 turn_id 都是 turnID
// （窗口第一段的网关 turn_id；为空表示调用方拿不到回合身份，官方原文不改写、合成退回稳定值）。
// 去重与安装位置同 installOfficialCodexBaseInstructions。空白指令且没有官方消息时不动 input。
func installOfficialCodexIncrementalBaseInstructions(
	payload map[string]any,
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	instructions string,
	official json.RawMessage,
	turnID string,
) error {
	trimmed := strings.TrimSpace(instructions)
	if trimmed == "" {
		return nil
	}
	var rendered json.RawMessage
	if len(official) > 0 {
		rendered = rewriteOfficialCodexBaseInstructionsRawTurnID(official, turnID)
	} else {
		var err error
		if rendered, err = renderOfficialCodexIncrementalBaseInstructionsItemWithTurnID(
			section, threadID, instructions, turnID,
		); err != nil {
			return err
		}
	}
	input, err := officialCodexBaseInstructionsInputItems(payload)
	if err != nil {
		return err
	}
	payload["input"] = rebuildOfficialCodexBaseInstructionsInput(input, section, trimmed, rendered)
	return nil
}

// applyOfficialCodexIncrementalBaseInstructionsCarrier 是 HTTP 定型阶段在增量窗口形态下对 instructions 的处理，
// 来源判定与旧方案相同（applyOfficialCodexBaseInstructionsCarrier），区别只在安装的消息：入站官方形态除 turn_id 外逐字节保留，
// 否则合成稳定 UUIDv7 id。turnID 是窗口第一段的网关 turn_id（可为空，含义见 install 函数）。返回 payload 是否被改动。
func applyOfficialCodexIncrementalBaseInstructionsCarrier(
	payload map[string]any,
	contract *officialOpenAIHTTPBodyContract,
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	turnID string,
) (bool, error) {
	text, sourced, err := officialCodexBaseInstructionsSource(payload, contract, section)
	if err != nil {
		return false, err
	}
	_, hadTopLevel := payload["instructions"]
	delete(payload, "instructions")
	if !sourced || strings.TrimSpace(text) == "" {
		return hadTopLevel, nil
	}
	official := contract.inboundOfficialBaseInstructionsItem(section, text)
	if err := installOfficialCodexIncrementalBaseInstructions(payload, section, threadID, text, official, turnID); err != nil {
		return false, err
	}
	return true, nil
}

// applyOfficialCodexWSIncrementalBaseInstructions 是 WS 派生帧在增量窗口形态下对基础指令的处理，决策结构同
// applyOfficialCodexWSBaseInstructions（按“链”决定安装、丢弃或不动），三处不同：
//
//  1. 预热帧（generate=false）不回退到模型默认指令：增量窗口里官方预热帧的 input 就是空数组，前缀由首个业务帧携带。
//     回退会把一条“默认 Codex 指令”塞进客户端自己的空预热帧，随后首个业务帧里真实的基础指令又被当成“换了一段正文”
//     再追加一次，会话里出现两套指令。预热帧上有明确来源（旧窗口的预热帧自带前缀）时照常安装。
//  2. 安装的消息：official 非空（入站官方形态消息原文）时除 turn_id 外逐字节放进去，否则合成稳定 UUIDv7 id；
//     两种情况下 turn_id 都是 turnID（本帧窗口第一段的网关 turn_id，拿不到回合身份时为空）。
//  3. 其余（新链业务帧缺来源时回退默认指令、续链帧按摘要丢弃或追加）与旧方案一致。
//
// 必须在逐项 turn_id 补写之后调用（安装的是预编码字节）。
func applyOfficialCodexWSIncrementalBaseInstructions(
	payload map[string]any,
	state *officialOpenAIWSDerivedState,
	section *profilecontract.BaseInstructionsSection,
	threadID string,
	text string,
	sourced bool,
	official json.RawMessage,
	turnID string,
) error {
	trimmed := strings.TrimSpace(text)
	sourced = sourced && trimmed != ""
	chained := strings.TrimSpace(officialOpenAIString(payload, "previous_response_id")) != ""
	generate, generatePresent := payload["generate"].(bool)
	prewarm := generatePresent && !generate
	if !sourced && !chained && !prewarm {
		// 与旧方案相同：新链业务帧既没有顶层 instructions 也没有可用的带标记消息时，回退到模型默认指令。
		// 回退正文不是入站消息，更不会有可原样保留的官方原文。
		text = defaultCodexSynthInstructions(officialOpenAIString(payload, "model"))
		trimmed = strings.TrimSpace(text)
		sourced = trimmed != ""
		official = nil
	}
	var digest officialCodexBaseInstructionsDigest
	if sourced {
		digest = sha256.Sum256([]byte(text))
	}
	action, replaced := state.planBaseInstructions(chained, sourced, digest)
	switch action {
	case officialCodexWSBaseInstructionsInstall:
		if replaced {
			warnOfficialCodexBaseInstructionsChanged()
		}
		return installOfficialCodexIncrementalBaseInstructions(payload, section, threadID, text, official, turnID)
	case officialCodexWSBaseInstructionsDrop:
		input, err := officialCodexBaseInstructionsInputItems(payload)
		if err != nil {
			return err
		}
		payload["input"] = rebuildOfficialCodexBaseInstructionsInput(input, section, trimmed, nil)
	}
	return nil
}

// ensureOfficialCodexAdditionalToolsIDs 给 input 里还没有 id 的 additional_tools 项补 at_ 加稳定 UUIDv7（第三方入站的
// 工具目录由网关合成，没有 id）。已经带非空 id 的项（Codex 客户端自带）和没有任何工具的空壳都不动：空壳不是官方客户端会
// 发出的形态，补 id 只会让它更像真的。种子是线程与工具定义的摘要：同一线程同一份工具定义在各请求上 id 相同，
// 工具集变了 id 跟着变（与官方“新窗口新 id”一致）。返回 input 是否被改动。
func ensureOfficialCodexAdditionalToolsIDs(payload map[string]any, threadID string) (bool, error) {
	input, ok := payload["input"].([]any)
	if !ok {
		return false, nil
	}
	modified := false
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || officialOpenAIString(item, "type") != officialOpenAIAdditionalToolsType {
			continue
		}
		if id, _ := item["id"].(string); strings.TrimSpace(id) != "" {
			continue
		}
		tools, _ := item["tools"].([]any)
		if len(tools) == 0 {
			continue
		}
		if strings.TrimSpace(threadID) == "" {
			return false, errors.New("OpenAI official egress additional_tools require a thread id")
		}
		digest, err := digestOfficialJSONValue(tools)
		if err != nil {
			return false, err
		}
		item["id"] = officialCodexStableItemID(
			officialCodexAdditionalToolsIDPrefix, officialUUIDV7DomainAdditionalTools, threadID, digest,
		)
		modified = true
	}
	return modified, nil
}
