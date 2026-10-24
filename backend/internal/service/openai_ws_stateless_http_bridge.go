package service

// 客户端 Responses WebSocket 经 HTTP 桥接服务「上游不提供 Responses WebSocket」的平台。
//
// 背景：Codex 等客户端优先用 WebSocket 连 /v1/responses。Composite 分组的候选池可能选中
// command_code、custom、deepseek 这类 OpenAI 网关族的非 openai 平台账号，它们的上游只有
// HTTP（Responses 或 Chat Completions），不可能再开上游 WebSocket。已有的「客户端 WS → 上游
// HTTP」桥接原本只服务 Grok，这里把它扩展到全部这类平台，并补齐三件事：
//
//  1. 谓词集中：OpenAIPlatformUsesWSHTTPBridge 是「必须走桥接」的唯一判据，
//     OpenAIPlatformIsStatelessWSHTTPBridge 再把 Grok 排除出去，得到「无状态桥接平台」。
//     Grok 的桥接行为保持原样，不进入本文件里的任何新增分支。
//  2. 续链完整：Codex 在同一条 WS 连接的后续轮次只发增量 input 加 previous_response_id，
//     而上游无状态，桥接又会剥离 previous_response_id。桥接循环因此要自己保存整段对话：
//     每轮上游的全部输出项（助手消息、推理项、工具调用）都并入重放历史，下一轮用「历史 + 增量」
//     还原完整 input。既有的重放历史只收工具调用项，助手文本与推理项会丢，对只做文字对话的
//     无状态上游是上下文缺口，所以无状态桥接平台改收全部输出项。
//  3. 预热帧本地应答：Codex 在会话开始时先发一帧 generate=false 的预热请求，真实 OpenAI 对它
//     只建立基线、不生成内容。HTTP 桥接没有「不生成」的语义，原样转发会白白调用一次模型，
//     输出还会污染对话历史，所以由网关本地合成 response.created 与 response.completed。
//
// 无法在本连接内续链时（previous_response_id 不是本连接上一轮产生的 ID，或重放历史已超限），
// 桥接回复 previous_response_not_found 错误事件。Codex 把它当作可重试错误，重试时改发完整 input，
// 对话上下文由客户端自己补齐，网关永远不会把只有增量的请求发给无状态上游。

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/domain"
	"github.com/google/uuid"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// OpenAIPlatformUsesWSHTTPBridge 报告平台的上游是否不提供 Responses WebSocket，
// 因而客户端的 Responses WebSocket 必须经「客户端 WS → 上游 HTTP」桥接服务。
//
// 范围是 OpenAI 网关族里除 openai 之外的全部平台（grok、kimi、zhipu、deepseek、minimax、
// opencode_go、command_code、cline、custom）。openai 平台（官方 OAuth 与 API Key，含指向
// 兼容中转站的 API Key 账号）保留既有的上游 WebSocket 行为，不在此列。
// Anthropic 网关族（anthropic、gemini、antigravity、typesafe）不走 OpenAI 网关，也不在此列。
func OpenAIPlatformUsesWSHTTPBridge(platform string) bool {
	return platform != PlatformOpenAI && domain.UsesOpenAIGateway(platform)
}

// OpenAIPlatformIsStatelessWSHTTPBridge 报告平台是否属于「无状态桥接平台」：
// 走 WS→HTTP 桥接，且不是 Grok。Grok 的桥接另有专属的缓存身份、工具提升等逻辑并保持原样，
// 本文件新增的续链、预热与协议校验只对无状态桥接平台生效。
func OpenAIPlatformIsStatelessWSHTTPBridge(platform string) bool {
	return platform != PlatformGrok && OpenAIPlatformUsesWSHTTPBridge(platform)
}

func isOpenAIWSStatelessBridgeAccount(account *Account) bool {
	return account != nil && OpenAIPlatformIsStatelessWSHTTPBridge(account.Platform)
}

// openAIWSHTTPBridgeLogHost 返回桥接日志里的「上游主机」标签：无状态桥接平台用通用标签，
// 其余（Grok 与插件接管的 OpenAI OAuth）沿用历史标签，日志检索口径不变。
func openAIWSHTTPBridgeLogHost(account *Account) string {
	if isOpenAIWSStatelessBridgeAccount(account) {
		return "http-bridge"
	}
	return "xai-http-bridge"
}

const (
	// openAIWSBridgePreviousResponseNotFoundCode 是 Responses WebSocket 协议约定的错误码：
	// 服务端找不到 previous_response_id 对应的响应。Codex 把它映射成可重试错误并在重试时改发完整 input。
	openAIWSBridgePreviousResponseNotFoundCode = "previous_response_not_found"
	// openAIWSBridgeWarmupResponseIDPrefix 是本地合成预热应答的响应 ID 前缀。
	// 保留 resp_ 前缀，客户端后续把它填进 previous_response_id 时能通过 ID 类型校验。
	openAIWSBridgeWarmupResponseIDPrefix = "resp_bridge_warmup_"
)

// openAIWSHTTPBridgeAdaptsClientTools 报告桥接是否要把客户端的自由格式工具（custom、tool_search）
// 适配成上游认得的 function 工具，并在响应里还原成客户端的形态。判定与 HTTP 入口同源：
//   - openai 的 API Key 账号与 Grok：既有行为；
//   - DeepSeek 原生 Responses 的 API Key 账号：HTTP 入口在 Forward 里对它做同样的适配，
//     否则 Codex 的 apply_patch 等自由格式工具会被上游拒绝。桥接补上这一步，WS 与 HTTP 的体验才一致。
//
// 其它平台（command_code、custom、kimi 等）在 HTTP 入口也不做这一步，桥接保持一致。
func openAIWSHTTPBridgeAdaptsClientTools(account *Account) bool {
	if account == nil {
		return false
	}
	switch {
	case account.Platform == PlatformOpenAI && account.Type == AccountTypeAPIKey:
		return true
	case account.Platform == PlatformGrok:
		return true
	case account.Platform == PlatformDeepseek && account.Type == AccountTypeAPIKey && account.UsesNativeCNResponses():
		return true
	default:
		return false
	}
}

// openAIWSStatelessBridgeUnsupportedUpstreamMessage 是桥接无法服务账号时给客户端的关闭原因。
const openAIWSStatelessBridgeUnsupportedUpstreamMessage = "Responses WebSocket only supports upstreams with native Responses support for this model; use the HTTP Responses endpoint instead"

// ErrOpenAIWSBridgeUnsupportedUpstream 标记「该账号的上游需要协议转换，桥接无法服务」。
// 它是请求级的能力拒绝，与账号健康无关：处理器据此不把这次关闭上报为账号失败，
// 否则每次 Codex 尝试 WebSocket 都会拉低这个本来健康的账号的调度评分。
var ErrOpenAIWSBridgeUnsupportedUpstream = errors.New("openai ws http bridge: upstream requires protocol conversion")

// openAIWSStatelessBridgeContinuationBroken 判断无状态桥接平台上一帧的续链是否已经断裂。
//
// 帧里没有 previous_response_id 时是完整请求，没有续链可言。带了 previous_response_id 时，
// 只有它恰好是本连接上一轮产生的响应 ID、且重放历史仍可用，桥接才能用「历史 + 增量」还原
// 完整 input；其余情况（新连接的首帧、指向更早的响应、历史超限）都无法保证上游看到完整上下文。
func openAIWSStatelessBridgeContinuationBroken(previousResponseID, lastResponseID string, state openAIWSReplayInputState) bool {
	previousResponseID = strings.TrimSpace(previousResponseID)
	if previousResponseID == "" {
		return false
	}
	return previousResponseID != strings.TrimSpace(lastResponseID) || state.unavailable
}

// openAIWSStatelessBridgeNativeResponses 判断无状态桥接平台账号对该模型的上游是否原生 Responses。
//
// 桥接按 Responses 形态把请求原样发往上游的 Responses 端点，只能服务原生 Responses 上游；
// 上游是 Chat Completions 或 Anthropic 协议的账号需要协议转换，转换链路只存在于 HTTP 入口，
// 桥接无法服务。按模型分流的账号（command_code、opencode_go）与 adaptive 账号（custom、
// 国产厂商）要带上账号映射后的路由模型名与上游模型目录判定，规则与候选池的零转换判定同源。
func (s *OpenAIGatewayService) openAIWSStatelessBridgeNativeResponses(ctx context.Context, account *Account, clientModel string) bool {
	if s == nil || account == nil {
		return false
	}
	routingBody, err := sjson.SetBytes([]byte(`{}`), "model", strings.TrimSpace(clientModel))
	if err != nil {
		return false
	}
	routingModel := upstreamRoutingModel(account, routingBody, "")
	return s.resolveUpstreamProtocolFor(ctx, account, APIProtocolResponses, routingModel) == APIProtocolResponses
}

// isOpenAIWSGenerateFalseFrame 报告 response.create 帧是否显式带 generate=false（预热帧）。
func isOpenAIWSGenerateFalseFrame(payload []byte) bool {
	generate := gjson.GetBytes(payload, "generate")
	return generate.Exists() && generate.Type == gjson.False
}

// buildOpenAIWSBridgePreviousResponseNotFoundEvent 构造 previous_response_not_found 错误事件。
// status 取 400：它是客户端可修复的请求问题（改发完整 input），不是上游故障。
func buildOpenAIWSBridgePreviousResponseNotFoundEvent(previousResponseID string) []byte {
	message := "Previous response not found; resend the full input without previous_response_id."
	if id := strings.TrimSpace(previousResponseID); id != "" {
		message = fmt.Sprintf("Previous response with id '%s' not found; resend the full input without previous_response_id.", id)
	}
	body, err := json.Marshal(map[string]any{
		"type":            "error",
		"sequence_number": 0,
		"status":          400,
		"error": map[string]any{
			"type":    "invalid_request_error",
			"code":    openAIWSBridgePreviousResponseNotFoundCode,
			"message": message,
			"param":   "previous_response_id",
		},
	})
	if err != nil {
		return []byte(`{"type":"error","status":400,"error":{"type":"invalid_request_error","code":"previous_response_not_found","message":"Previous response not found"}}`)
	}
	return body
}

// buildOpenAIWSBridgeWarmupEvents 合成预热帧的应答事件序列：response.created、response.completed。
// 与真实 OpenAI 对 generate=false 的应答一致：output 为空、用量为零；model 取客户端请求的模型名。
func buildOpenAIWSBridgeWarmupEvents(responseID, model string, createdAt int64) ([][]byte, error) {
	response := func(status string) map[string]any {
		item := map[string]any{
			"id":         responseID,
			"object":     "response",
			"created_at": createdAt,
			"status":     status,
			"output":     []any{},
		}
		if model = strings.TrimSpace(model); model != "" {
			item["model"] = model
		}
		return item
	}
	completed := response("completed")
	completed["usage"] = map[string]any{"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
	created, err := json.Marshal(map[string]any{"type": "response.created", "sequence_number": 0, "response": response("in_progress")})
	if err != nil {
		return nil, err
	}
	done, err := json.Marshal(map[string]any{"type": "response.completed", "sequence_number": 1, "response": completed})
	if err != nil {
		return nil, err
	}
	return [][]byte{created, done}, nil
}

// answerOpenAIWSBridgeWarmupTurn 在本地应答一个预热帧，不访问上游。
// 返回的结果用量为零，交给调用方走与普通轮次相同的收尾（用量记录、重放历史、读取下一帧）。
func answerOpenAIWSBridgeWarmupTurn(
	originalModel string,
	upstreamModel string,
	turnStart time.Time,
	writeClientMessage func([]byte) error,
) (*OpenAIForwardResult, error) {
	responseID := openAIWSBridgeWarmupResponseIDPrefix + strings.ReplaceAll(uuid.NewString(), "-", "")
	events, err := buildOpenAIWSBridgeWarmupEvents(responseID, originalModel, turnStart.Unix())
	if err != nil {
		return nil, fmt.Errorf("build websocket http bridge warmup events: %w", err)
	}
	for _, event := range events {
		if err := writeClientMessage(event); err != nil {
			return nil, wrapOpenAIWSIngressTurnError("write_client", fmt.Errorf("write websocket http bridge warmup event: %w", err), false)
		}
	}
	return &OpenAIForwardResult{
		RequestID:             responseID,
		Model:                 originalModel,
		UpstreamModel:         upstreamModel,
		Stream:                true,
		OpenAIWSMode:          true,
		UpstreamTerminalEvent: "response.completed",
		Duration:              time.Since(turnStart),
	}, nil
}
