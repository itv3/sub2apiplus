package service

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"reflect"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openaiidentity"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
	"github.com/klauspost/compress/zstd"
	"github.com/tidwall/gjson"
)

const (
	officialOpenAIHTTPOriginator    = openaiidentity.CodexOriginator
	officialOpenAIHTTPBetaFeatures  = "remote_compaction_v2"
	officialOpenAIHTTPResponsesLite = "true"
	officialOpenAIHTTPTurnStartKey  = "official_openai_http_turn_started_at_unix_ms"
	officialOpenAIHTTPTurnStartTTL  = 2 * time.Hour
	officialOpenAIHTTPTurnStartMax  = 8192
)

// HTTP 与其他 Codex 出站路径共享 Active ReleaseCatalog 派生的默认身份。
var officialOpenAIHTTPUserAgent = codexCLIUserAgent

type officialOpenAIHTTPTurnStartEntry struct {
	startedAt int64
	expiresAt time.Time
	order     uint64
}

var officialOpenAIHTTPTurnStarts = struct {
	sync.Mutex
	entries   map[string]officialOpenAIHTTPTurnStartEntry
	nextOrder uint64
}{}

type officialOpenAIHTTPCallID struct {
	ItemType string
	Value    string
}

// officialOpenAIHTTPBodyContract 保存入口明确提供且 T5 禁止改写的字段。
// 值只在请求内存中流转，不进入普通日志或审计。
type officialOpenAIHTTPBodyContract struct {
	instructionsPresent bool
	instructions        any
	clientMetadataSet   bool
	clientMetadata      map[string]any
	promptCacheKeySet   bool
	promptCacheKey      string
	additionalTools     []any
	callIDs             []officialOpenAIHTTPCallID
	includePresent      bool
	include             any
	parallelPresent     bool
	parallelToolCalls   any
}

type officialOpenAIHTTPIdentity struct {
	installationID string
	sessionID      string
	threadID       string
	windowID       string
	turnID         string
	turnMetadata   string
	clientRequest  string
	promptCacheKey string
	parentThreadID string
	subagent       string
	memoryGenerate bool
	source         OfficialEgressFieldSource
	// session/turn 来源必须分别冻结；非空值不能替代来源证明。
	sessionProvenance officialOpenAIIdentityProvenance
	turnProvenance    officialOpenAIIdentityProvenance
}

type officialOpenAIIdentityKind string

const (
	officialOpenAIIdentityKindRoot                officialOpenAIIdentityKind = "root"
	officialOpenAIIdentityKindSubagent            officialOpenAIIdentityKind = "subagent"
	officialOpenAIIdentityKindGuardian            officialOpenAIIdentityKind = "guardian"
	officialOpenAIIdentityKindMemoryConsolidation officialOpenAIIdentityKind = "memory_consolidation"
)

type officialOpenAIIngressIdentityValues struct {
	sessionID      string
	threadID       string
	promptCacheKey string
}

// validateOfficialOpenAIIngressIdentityKind 按当前 Codex Release 的身份来源画像验证
// prompt cache 锚点。根线程使用 session UUID；普通子代理与内部记忆合并仍沿用
// session UUID；guardian 是唯一源码明确覆写为 guardian:<parent_thread_id> 的分支。
func validateOfficialOpenAIIngressIdentityKind(
	scope string,
	c *gin.Context,
	clientMetadata map[string]any,
	turnMetadata map[string]any,
	identity officialOpenAIIngressIdentityValues,
	isCompact bool,
) (officialOpenAIIdentityKind, error) {
	if c == nil {
		return "", fmt.Errorf("%s ingress context is unavailable", scope)
	}
	subagent := strings.TrimSpace(c.GetHeader("x-openai-subagent"))
	memoryGeneration := strings.TrimSpace(c.GetHeader("x-openai-memgen-request"))
	parentThreadID := strings.TrimSpace(c.GetHeader("x-codex-parent-thread-id"))
	turnSource := officialOpenAIString(turnMetadata, "thread_source")
	turnSubagentKind := officialOpenAIString(turnMetadata, "subagent_kind")
	turnParentThreadID := officialOpenAIString(turnMetadata, "parent_thread_id")

	for _, headerName := range []string{"x-openai-subagent", "x-codex-parent-thread-id"} {
		rawMetadataValue, exists := clientMetadata[headerName]
		if !exists {
			continue
		}
		metadataValue, valid := rawMetadataValue.(string)
		if !valid || strings.TrimSpace(metadataValue) != strings.TrimSpace(c.GetHeader(headerName)) {
			return "", fmt.Errorf(
				"%s client_metadata %s conflicts with ingress header",
				scope,
				headerName,
			)
		}
	}

	if parentThreadID != "" {
		if _, err := uuid.Parse(parentThreadID); err != nil {
			return "", fmt.Errorf("%s parent thread must be UUID", scope)
		}
		if turnParentThreadID != parentThreadID {
			return "", fmt.Errorf("%s parent thread conflicts with turn metadata", scope)
		}
	}
	// legacy compact 的 wire schema 没有 client_metadata，因此只能依靠入口
	// 条件头与 x-codex-turn-metadata 交叉验证；普通 responses 仍要求正文副本存在。
	if !isCompact && subagent != "" {
		metadataSubagent, exists := clientMetadata["x-openai-subagent"].(string)
		if !exists || strings.TrimSpace(metadataSubagent) != subagent {
			return "", fmt.Errorf("%s subagent is missing from client_metadata", scope)
		}
	}
	if !isCompact && parentThreadID != "" {
		metadataParent, exists := clientMetadata["x-codex-parent-thread-id"].(string)
		if !exists || strings.TrimSpace(metadataParent) != parentThreadID {
			return "", fmt.Errorf("%s parent thread is missing from client_metadata", scope)
		}
	}

	kind := officialOpenAIIdentityKindRoot
	expectedPromptCacheKey := identity.sessionID
	switch {
	case memoryGeneration != "":
		if memoryGeneration != "true" || subagent != "memory_consolidation" || parentThreadID != "" ||
			turnSource != "memory_consolidation" || turnSubagentKind != "" || turnParentThreadID != "" {
			return "", fmt.Errorf("%s memory consolidation identity conflicts", scope)
		}
		kind = officialOpenAIIdentityKindMemoryConsolidation
	case subagent == "guardian":
		if parentThreadID == "" || turnSource != "subagent" ||
			turnSubagentKind != "guardian" || turnParentThreadID != parentThreadID {
			return "", fmt.Errorf("%s guardian identity conflicts", scope)
		}
		kind = officialOpenAIIdentityKindGuardian
		expectedPromptCacheKey = "guardian:" + parentThreadID
	case subagent != "":
		if turnSource != "subagent" || turnSubagentKind == "" {
			return "", fmt.Errorf("%s subagent identity conflicts", scope)
		}
		kind = officialOpenAIIdentityKindSubagent
	case parentThreadID != "" || turnSource == "subagent" ||
		turnSource == "memory_consolidation" || turnSubagentKind != "" || turnParentThreadID != "":
		return "", fmt.Errorf("%s root identity contains subagent metadata", scope)
	}

	if kind == officialOpenAIIdentityKindRoot {
		if identity.sessionID != identity.threadID {
			return "", fmt.Errorf("%s root session/thread identity conflicts", scope)
		}
	} else if identity.sessionID == identity.threadID {
		return "", fmt.Errorf("%s child session/thread identity conflicts", scope)
	}
	if identity.promptCacheKey != expectedPromptCacheKey {
		return "", fmt.Errorf("%s %s prompt_cache_key conflicts with identity", scope, kind)
	}
	return kind, nil
}

// officialOpenAICompactionMetadata 按官方 TurnMetadata 的声明顺序序列化。
// 这里使用结构体而非 map，避免 encoding/json 把嵌套字段改为字典序。
type officialOpenAICompactionMetadata struct {
	Trigger        string `json:"trigger"`
	Reason         string `json:"reason"`
	Implementation string `json:"implementation"`
	Phase          string `json:"phase"`
	Strategy       string `json:"strategy"`
}

var officialOpenAICompactionReasons = map[string]struct{}{
	"user_requested":    {},
	"context_limit":     {},
	"model_downshift":   {},
	"comp_hash_changed": {},
}

// captureOfficialOpenAIHTTPBodyContract 在既有 OAuth 转换前记录入口契约。
// 标准第三方请求可以没有 Codex 身份字段；真正的官方 Codex 请求仍由 Finalizer
// 按入口 Header 严格校验完整身份。
func captureOfficialOpenAIHTTPBodyContract(body []byte) (*officialOpenAIHTTPBodyContract, error) {
	payload, err := decodeOfficialJSONObjectUseNumber(body)
	if err != nil {
		return nil, fmt.Errorf("OpenAI official egress requires valid JSON body: %w", err)
	}
	contract := &officialOpenAIHTTPBodyContract{}
	contract.instructions, contract.instructionsPresent = payload["instructions"]
	contract.include, contract.includePresent = payload["include"]
	contract.parallelToolCalls, contract.parallelPresent = payload["parallel_tool_calls"]

	if promptCacheKey, ok := payload["prompt_cache_key"].(string); ok &&
		strings.TrimSpace(promptCacheKey) != "" {
		contract.promptCacheKeySet = true
		contract.promptCacheKey = strings.TrimSpace(promptCacheKey)
	}

	if clientMetadata, ok := payload["client_metadata"].(map[string]any); ok {
		contract.clientMetadataSet = true
		contract.clientMetadata = cloneOfficialOpenAIMap(clientMetadata)
	}
	contract.additionalTools = collectOfficialOpenAIAdditionalTools(payload)
	contract.callIDs = collectOfficialOpenAICallIDs(payload)
	return contract, nil
}

// captureOfficialOpenAIHTTPBodyContractForRequest 在正文契约之上恢复 Handler
// 已显式保存的 legacy compact 会话锚点。compact normalizer 会按独立 schema
// 删除 prompt_cache_key，因此这里只接受同一请求上下文中预先存在的原始 seed；
// 不从 Header、正文其他字段或随机值生成、猜测身份。
func captureOfficialOpenAIHTTPBodyContractForRequest(
	c *gin.Context,
	body []byte,
) (*officialOpenAIHTTPBodyContract, error) {
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	if err != nil {
		return nil, err
	}
	if c == nil || !isOpenAIResponsesCompactPath(c) {
		return contract, nil
	}
	rawSeed, exists := c.Get(openAICompactSessionSeedKey)
	if !exists {
		return contract, nil
	}
	seed, ok := rawSeed.(string)
	if !ok || strings.TrimSpace(seed) == "" {
		return contract, nil
	}
	seed = strings.TrimSpace(seed)
	if contract.promptCacheKeySet && contract.promptCacheKey != seed {
		return nil, errors.New("OpenAI official egress compact prompt_cache_key conflicts with captured seed")
	}
	contract.promptCacheKeySet = true
	contract.promptCacheKey = seed
	return contract, nil
}

// bindGeneratedOfficialOpenAIHTTPBodyContract 绑定 Chat Completions/Messages
// 转换后的 Responses 语义字段。身份字段仍标记为非入口显式值，后续必须重新派生。
func bindGeneratedOfficialOpenAIHTTPBodyContract(
	contract *officialOpenAIHTTPBodyContract,
	body []byte,
) error {
	if contract == nil {
		return errors.New("OpenAI official egress body contract is nil")
	}
	payload, err := decodeOfficialJSONObjectUseNumber(body)
	if err != nil {
		return fmt.Errorf("decode generated OpenAI official egress body: %w", err)
	}
	contract.additionalTools = collectOfficialOpenAIAdditionalTools(payload)
	contract.callIDs = collectOfficialOpenAICallIDs(payload)
	if contract.promptCacheKey == "" {
		if value, ok := payload["prompt_cache_key"].(string); ok {
			contract.promptCacheKey = strings.TrimSpace(value)
		}
	}
	return nil
}

// captureGeneratedOfficialOpenAIHTTPBodyContract 为未显式传递契约的兼容桥建立
// 最终语义快照。旧链路生成的 instructions 没有入口所有权，必须由 Finalizer 删除。
func captureGeneratedOfficialOpenAIHTTPBodyContract(
	body []byte,
) (*officialOpenAIHTTPBodyContract, error) {
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	if err != nil {
		return nil, err
	}
	contract.instructionsPresent = false
	contract.instructions = nil
	contract.clientMetadataSet = false
	contract.promptCacheKeySet = false
	return contract, nil
}

func cloneOfficialOpenAIMap(source map[string]any) map[string]any {
	cloned := make(map[string]any, len(source))
	for key, value := range source {
		cloned[key] = value
	}
	return cloned
}

func collectOfficialOpenAIAdditionalTools(payload map[string]any) []any {
	input, _ := payload["input"].([]any)
	items := make([]any, 0, 1)
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		if itemType, _ := item["type"].(string); itemType == "additional_tools" {
			items = append(items, item)
		}
	}
	return items
}

func collectOfficialOpenAICallIDs(payload map[string]any) []officialOpenAIHTTPCallID {
	input, _ := payload["input"].([]any)
	callIDs := make([]officialOpenAIHTTPCallID, 0)
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		itemType, _ := item["type"].(string)
		if !isCodexToolCallItemType(itemType) {
			continue
		}
		callID, _ := item["call_id"].(string)
		if strings.TrimSpace(callID) == "" {
			continue
		}
		callIDs = append(callIDs, officialOpenAIHTTPCallID{
			ItemType: itemType,
			Value:    callID,
		})
	}
	return callIDs
}

// prepareOpenAIOfficialEgressSemanticHTTPRequest 只完成 service 所有的业务语义
// 派生与身份来源校验。Header 闭集、画像字段注入、Body 线序、压缩和长度均由
// Executor/compiler 独占，避免在签名前形成第二套 wire 定型权威。
func prepareOpenAIOfficialEgressSemanticHTTPRequest(
	req *http.Request,
	c *gin.Context,
	account *Account,
	body []byte,
	plan openAIUpstreamRequestPlan,
) (*http.Request, OfficialEgressFinalizationResult, error) {
	result := OfficialEgressFinalizationResult{}
	var err error
	if req == nil {
		return nil, result, errors.New("OpenAI official egress requires request")
	}
	egressContext, enabled := OfficialEgressContextFromContext(req.Context())
	if !enabled {
		return req, result, nil
	}
	if c == nil || c.Request == nil {
		return nil, result, errors.New("OpenAI official egress requires ingress request")
	}
	if plan.OfficialEgressBodyContract == nil {
		plan.OfficialEgressBodyContract, err = captureGeneratedOfficialOpenAIHTTPBodyContract(body)
		if err != nil {
			return nil, result, err
		}
	}

	profile, err := defaultOfficialEgressProfileResolver.ResolveHTTPProfile(
		egressContext,
		account,
		egressContext.InboundEndpoint(),
	)
	if err != nil {
		return nil, result, err
	}
	if profile.TargetPlatform != PlatformOpenAI {
		return nil, result, errors.New("OpenAI finalizer received non-OpenAI profile")
	}
	identity, err := resolveOfficialOpenAIHTTPIdentity(
		c,
		account,
		body,
		plan.OfficialEgressBodyContract,
		egressContext.ProfileMode(),
	)
	if err != nil {
		return nil, result, err
	}
	normalizeOfficialCodexConditionalIdentity(
		egressContext,
		identity.subagent,
		identity.parentThreadID,
		identity.memoryGenerate,
	)
	toolPresentationModified := false
	if !plan.IsCompact {
		payload, decodeErr := decodeOfficialJSONObjectUseNumber(body)
		if decodeErr != nil {
			return nil, result, fmt.Errorf("解析 Codex 派生工具呈现：%w", decodeErr)
		}
		toolPresentationModified, err = officialCodexNormalizeDerivedToolPresentation(
			egressContext.ProfileVersion(),
			codexEndpointID(egressContext.CodexEndpointProfileID()),
			payload,
		)
		if err != nil {
			return nil, result, err
		}
		if toolPresentationModified {
			body, err = marshalOfficialJSONObjectPreservingOrderAndRaw(payload, body)
			if err != nil {
				return nil, result, fmt.Errorf("编码 Codex 派生工具呈现：%w", err)
			}
		}
	}
	finalBody, bodyModified, err := finalizeOfficialOpenAIHTTPBody(
		body,
		plan.OfficialEgressBodyContract,
		identity,
		officialOpenAIReasoningDefaultsFromContext(egressContext),
		officialOpenAIHTTPBodyOptions{
			IsCompact:             plan.IsCompact,
			UseResponsesLite:      egressContext.responsesLite,
			SupportsParallelTools: egressContext.parallelTools,
			UserAgent:             officialOpenAIInboundUserAgent(c),
			ProfileMode:           egressContext.ProfileMode(),
		},
	)
	if err != nil {
		return nil, result, err
	}
	bodyModified = bodyModified || toolPresentationModified
	if bodyModified {
		result.Modifications = append(result.Modifications,
			OfficialEgressModification{Kind: "body", Field: "instructions"},
		)
	}

	if err := registerOfficialOpenAIHTTPIdentity(egressContext, identity, plan.IsCompact); err != nil {
		return nil, result, err
	}
	if turnState := strings.TrimSpace(plan.OfficialEgressTurnState); turnState != "" {
		if err := egressContext.RegisterField(
			OfficialEgressFieldTurnState,
			turnState,
			OfficialEgressFieldSourceRedisSession,
			OfficialEgressFieldLifecycleTurn,
		); err != nil {
			return nil, result, err
		}
	}
	if err := ValidateOfficialEgressFinalState(egressContext, profile); err != nil {
		return nil, result, err
	}
	logOfficialEgressProfileResolved(egressContext, profile)
	resetOfficialEgressRequestBody(req, finalBody)
	return req, result, nil
}

// compressOfficialOpenAIHTTPRequest 在所有 JSON 终态修正完成后执行官方 Codex
// 请求压缩。level 由不可变版本画像传入；reset helper 同步 Body、ContentLength
// 与 GetBody，确保重定向、重试和抓包读取到同一份压缩字节。
func compressOfficialOpenAIHTTPRequest(req *http.Request, body []byte, level int) error {
	encoder, err := zstd.NewWriter(nil, zstd.WithEncoderLevel(zstd.EncoderLevelFromZstd(level)))
	if err != nil {
		return fmt.Errorf("create OpenAI official zstd encoder: %w", err)
	}
	compressed := encoder.EncodeAll(body, nil)
	if err := encoder.Close(); err != nil {
		return fmt.Errorf("close OpenAI official zstd encoder: %w", err)
	}
	resetOfficialEgressRequestBody(req, compressed)
	req.Header.Set("Content-Encoding", "zstd")
	return nil
}

// officialOpenAIHTTPBodyOptions 收拢 body 定型开关。入口客户端类型不在这里出现：
// 官方客户端与第三方客户端都必须先归一化，再由同一 active 画像编译 wire。
type officialOpenAIHTTPBodyOptions struct {
	IsCompact             bool
	UseResponsesLite      bool
	SupportsParallelTools bool
	// UserAgent 只用于投影告警定位来源，不参与任何定型决策。
	UserAgent   string
	ProfileMode string
}

func finalizeOfficialOpenAIHTTPBody(
	body []byte,
	contract *officialOpenAIHTTPBodyContract,
	identity officialOpenAIHTTPIdentity,
	reasoningDefaults officialOpenAIReasoningDefaults,
	options officialOpenAIHTTPBodyOptions,
) ([]byte, bool, error) {
	if contract == nil {
		return nil, false, errors.New("OpenAI official egress body contract is nil")
	}
	// 解构一次，保持下方定型逻辑的可读性与原实现一致。
	isCompact := options.IsCompact
	useResponsesLite := options.UseResponsesLite
	supportsParallelTools := options.SupportsParallelTools
	payload, err := decodeOfficialJSONObjectUseNumber(body)
	if err != nil {
		return nil, false, fmt.Errorf("decode OpenAI official egress body: %w", err)
	}

	modified := false
	endpointID := officialCodexEndpointResponsesHTTP
	if isCompact {
		endpointID = officialCodexEndpointResponsesCompact
	}
	allowedTopLevel, err := officialOpenAITopLevelAllowSetForMode(
		options.ProfileMode,
		endpointID,
	)
	if err != nil {
		return nil, false, err
	}
	if isCompact {
		if removed := stripNonOfficialOpenAITopLevelFields(payload, allowedTopLevel); len(removed) > 0 {
			modified = true
			warnOfficialOpenAIProjectedTopLevelFields(removed, options.UserAgent, false)
		}
	}
	currentInstructions, currentInstructionsPresent := payload["instructions"]
	if currentInstructionsPresent && useResponsesLite {
		instructionsModified, err := moveOfficialOpenAIHTTPInstructionsToInput(payload, currentInstructions)
		if err != nil {
			return nil, false, err
		}
		if instructionsModified {
			modified = true
		}
	}

	// 当前 Codex Release 的 Responses Lite Header 与 reasoning.context 是绑定契约：
	// Header=true 时上游强制要求 context=all_turns。第三方入口通常不会提供该
	// 字段，因此必须在同一个最终修正器中补齐，避免只伪装 Header 却发送非法 Body。
	if !isCompact && useResponsesLite {
		reasoningModified, err := ensureOpenAIResponsesLiteReasoningContext(payload)
		if err != nil {
			return nil, false, err
		}
		if reasoningModified {
			modified = true
		}
	}
	if !isCompact {
		profileModified, err := normalizeDerivedOfficialOpenAIHTTPBody(
			payload,
			useResponsesLite,
			supportsParallelTools,
			reasoningDefaults,
			allowedTopLevel,
			options.UserAgent,
		)
		if err != nil {
			return nil, false, err
		}
		if profileModified {
			modified = true
		}
	}

	if isCompact {
		// compact 的官方 Rust 请求结构体始终序列化该字段；所有入口统一补成画像值。
		if current, ok := payload["parallel_tool_calls"].(bool); !ok || current {
			payload["parallel_tool_calls"] = false
			modified = true
		}
	}
	// prompt_cache_key 与 client_metadata 是 compiler-owned wire 字段。service
	// 只把入口值作为会话语义锚点，再登记统一派生身份；提交给 Executor 的语义
	// Body 必须移除二者，避免形成第二套身份/Body 定型权威。
	for _, name := range []string{"prompt_cache_key", "client_metadata"} {
		if _, exists := payload[name]; exists {
			delete(payload, name)
			modified = true
		}
	}
	if !reflect.DeepEqual(contract.callIDs, collectOfficialOpenAICallIDs(payload)) {
		return nil, false, errors.New("OpenAI official egress call_id was modified")
	}

	if !modified {
		return body, false, nil
	}
	finalBody, err := marshalOfficialOpenAIHTTPJSONPreservingRaw(
		options.ProfileMode, payload, isCompact, body,
	)
	if err != nil {
		return nil, false, fmt.Errorf("serialize OpenAI official egress body: %w", err)
	}
	return finalBody, true, nil
}

// normalizeDerivedOfficialOpenAIHTTPBody 把第三方 Responses 请求归一化为
// 当前 Codex Release 的固定外层契约。只有 instructions、tools、reasoning.context
// 和 parallel_tool_calls 按模型能力分叉，其余固定字段不区分 Lite。
// 官方 ResponsesApiRequest / ResponseCreateWsRequest 都是固定 Rust 结构体，顶层不会
// 出现清单外的键。第三方入站的 truncation / top_logprobs / background / max_tool_calls
// 乃至任意自定义字段若不剔除，会被保序序列化器当作未知字段追加到 body 末尾送达上游。
// HTTP 与 WS 字段集不同（WS 多 type / previous_response_id / generate），故各用一份。
func officialOpenAITopLevelAllowSetForMode(
	mode string,
	endpointID string,
) (map[string]struct{}, error) {
	fields, err := officialCodexBodyFieldOrderForMode(
		mode, endpointID,
	)
	if err != nil {
		return nil, err
	}
	return newOfficialOpenAITopLevelAllowSet(fields), nil
}

// officialOpenAIReasoningDefaults 是 `/models` 清单在请求上下文中的冻结投影。
// 它不是全局兜底：不同模型、不同账号清单可以给出不同默认值。
type officialOpenAIReasoningDefaults struct {
	Effort          string
	Summary         string
	SupportsSummary bool
	Known           bool
}

func officialOpenAIReasoningDefaultsFromContext(
	egressContext *OfficialEgressContext,
) officialOpenAIReasoningDefaults {
	if egressContext == nil {
		return officialOpenAIReasoningDefaults{}
	}
	return officialOpenAIReasoningDefaults{
		Effort:          egressContext.defaultReasoningLevel,
		Summary:         egressContext.defaultReasoningSummary,
		SupportsSummary: egressContext.supportsReasoningSummary,
		Known:           egressContext.reasoningDefaultsKnown,
	}
}

func newOfficialOpenAITopLevelAllowSet(fields []string) map[string]struct{} {
	allowed := make(map[string]struct{}, len(fields))
	for _, field := range fields {
		allowed[field] = struct{}{}
	}
	return allowed
}

// stripNonOfficialOpenAITopLevelFields 按白名单剔除顶层字段，返回被剔除的字段名。
//
// 返回字段名而不只是布尔：被投影掉的字段是「官方已经升级、画像还没跟上」在运行时
// 的唯一信号（§3.1），只报告"有改动"无法定位到底是哪个新字段。map 遍历序不确定，
// 因此排序后返回，让同一组字段产生稳定的告警文本，去重才有意义。
func stripNonOfficialOpenAITopLevelFields(
	payload map[string]any,
	allowed map[string]struct{},
) []string {
	var removed []string
	for key := range payload {
		if _, ok := allowed[key]; ok {
			continue
		}
		delete(payload, key)
		removed = append(removed, key)
	}
	sort.Strings(removed)
	return removed
}

// officialOpenAIInboundUserAgent 取入站声明的 UA，只用于投影告警定位来源。
//
// 这里必须是入站 UA 而不是画像生成的出站 UA：后者对所有官方请求都相同，无法回答
// "是哪个客户端发来了画像不认识的字段"这个唯一有诊断价值的问题。
func officialOpenAIInboundUserAgent(c *gin.Context) string {
	if c == nil {
		return ""
	}
	return strings.TrimSpace(c.GetHeader("User-Agent"))
}

// officialOpenAIProjectedFieldsSeen 记录已经告警过的字段组合。
//
// 投影发生在每个请求上，逐请求打印会淹没日志；而这里真正有价值的信息是"出现了一组
// 画像未覆盖的字段"，同一组字段重复出现并不增加信息量。因此按入口类型加字段组合
// 去重，每种组合在进程生命周期内只告警一次。
//
// 去重表必须封顶：key 直接来自入站字段名，第三方客户端可以每次发送随机顶层字段名
// 把表撑大，否则这里就成了一个由外部输入驱动的内存增长向量。真实场景中画像外字段
// 的种类有限，触顶本身即说明遇到的是构造流量而不是新版本客户端。
const officialOpenAIProjectedFieldsSeenLimit = 256

// 用互斥锁而不是 sync.Map + 原子计数：后者的“查计数、插入、加计数”三步不原子，
// 并发请求携带各不相同的未知字段时可以同时看到计数未达上限并全部插入，表的实际
// 规模会越过封顶值。这条路径每种字段组合只走一次热路径，锁竞争可以忽略。
var (
	officialOpenAIProjectedFieldsMu   sync.Mutex
	officialOpenAIProjectedFieldsSeen = map[string]struct{}{}
)

// warnOfficialOpenAIProjectedTopLevelFields 报告被投影丢弃的顶层字段。
//
// §3.1 把这条信号定为运行时唯一能提示"该升级画像了"的依据。出现在官方入口上尤其
// 重要：它说明官方客户端已经在发送当前 active 画像还不认识的字段，画像落后于真实
// 客户端；出现在派生入口上则通常只是第三方客户端携带了非官方字段。
func warnOfficialOpenAIProjectedTopLevelFields(
	fields []string,
	userAgent string,
	strictIngress bool,
) {
	if len(fields) == 0 {
		return
	}
	ingress := "派生入口"
	if strictIngress {
		ingress = "官方入口"
	}
	key := ingress + "|" + strings.Join(fields, ",")
	officialOpenAIProjectedFieldsMu.Lock()
	if _, seen := officialOpenAIProjectedFieldsSeen[key]; seen {
		officialOpenAIProjectedFieldsMu.Unlock()
		return
	}
	if len(officialOpenAIProjectedFieldsSeen) >= officialOpenAIProjectedFieldsSeenLimit {
		officialOpenAIProjectedFieldsMu.Unlock()
		return
	}
	officialOpenAIProjectedFieldsSeen[key] = struct{}{}
	reachedLimit := len(officialOpenAIProjectedFieldsSeen) == officialOpenAIProjectedFieldsSeenLimit
	officialOpenAIProjectedFieldsMu.Unlock()

	if reachedLimit {
		logger.LegacyPrintf(
			"service.official_egress_openai",
			"[Codex] 投影告警去重表已达上限 %d，后续投影不再单独告警；"+
				"字段种类如此之多通常意味着构造流量而非画像落后",
			officialOpenAIProjectedFieldsSeenLimit,
		)
	}
	logger.LegacyPrintf(
		"service.official_egress_openai",
		"[Codex] %s 出现画像闭集外的顶层字段，已按 active 画像投影丢弃：fields=%v user-agent=%q",
		ingress,
		fields,
		userAgent,
	)
}

// validateOfficialOpenAITopLevelContract 校验一份候选正文是否仍在画像闭集内。
//
// 现在只服务于 WS 派生帧的内部一致性检查：那里的输入是网关自己生成或改写过的帧，
// 越界字段意味着 adapter 绕过了画像构建函数，属于本仓代码缺陷，必须失败关闭，
// 不能用投影掩盖——投影只对来自外部、无法控制的输入才有意义。
//
// 入站正文不再走这条路径。入口出现画像外字段时统一按 §3.1 投影并告警。
// 官方 Rust 结构体不会序列化画像外字段这一事实仍然成立，它现在表达为
// “投影后出站形态自洽”，而不是“拒绝服务”。
func validateOfficialOpenAITopLevelContract(
	payload map[string]any,
	allowed map[string]struct{},
	requireToolChoice bool,
) error {
	for key := range payload {
		if _, ok := allowed[key]; !ok {
			return fmt.Errorf("OpenAI official egress body contains unknown top-level field: %s", key)
		}
	}
	toolChoice, exists := payload["tool_choice"]
	if !exists {
		if requireToolChoice {
			return errors.New("OpenAI official egress tool_choice is missing")
		}
		return nil
	}
	value, ok := toolChoice.(string)
	if !ok || value != "auto" {
		return errors.New("OpenAI official egress tool_choice must be the string auto")
	}
	return nil
}

func normalizeDerivedOfficialOpenAIHTTPBody(
	payload map[string]any,
	useResponsesLite bool,
	supportsParallelTools bool,
	reasoningDefaults officialOpenAIReasoningDefaults,
	allowedTopLevel map[string]struct{},
	// userAgent 只用于投影告警定位来源，不参与定型决策。
	userAgent string,
) (bool, error) {
	modified, err := normalizeDerivedOfficialOpenAIInput(payload)
	if err != nil {
		return false, err
	}
	if removed := stripNonOfficialOpenAITopLevelFields(payload, allowedTopLevel); len(removed) > 0 {
		modified = true
		warnOfficialOpenAIProjectedTopLevelFields(removed, userAgent, false)
	}
	if useResponsesLite {
		liteModified, err := normalizeOpenAIResponsesLiteTools(payload)
		if err != nil {
			return false, err
		}
		if liteModified {
			modified = true
		}
		toolsModified, err := moveOfficialOpenAIHTTPToolsToInput(payload)
		if err != nil {
			return false, err
		}
		if toolsModified {
			modified = true
		}
	} else {
		// 非 Lite 保留第三方显式 instructions；Anthropic bridge 为保持字段类型
		// 曾补出的空字符串不属于有效指令，删除后避免形成官方不会产生的空壳字段。
		if instructions, exists := payload["instructions"].(string); exists && strings.TrimSpace(instructions) == "" {
			delete(payload, "instructions")
			modified = true
		}
		if reasoningModified, err := removeOfficialOpenAIReasoningContext(payload); err != nil {
			return false, err
		} else if reasoningModified {
			modified = true
		}
	}
	reasoningModified, err := normalizeDerivedOfficialOpenAIReasoning(
		payload,
		reasoningDefaults,
	)
	if err != nil {
		return false, err
	}
	if reasoningModified {
		modified = true
	}

	// Kilo/OpenAI OAuth 与 Codex CLI 均不会向 Codex 官方上游发送
	// max_output_tokens。第三方 Responses 客户端可能携带该标准 API 字段，
	// 但 ChatGPT Codex HTTP/WS 上游不接受，必须在最终画像层统一删除。
	if _, exists := payload["max_output_tokens"]; exists {
		delete(payload, "max_output_tokens")
		modified = true
	}

	for key, expected := range map[string]bool{
		"parallel_tool_calls": supportsParallelTools && !useResponsesLite,
		"store":               false,
		"stream":              true,
	} {
		if current, ok := payload[key].(bool); !ok || current != expected {
			payload[key] = expected
			modified = true
		}
	}
	// 当前 Codex Release 的 Responses Lite 请求结构体把 tool_choice 固定为 auto，
	// 不读取第三方入口值。若允许 required/none 透传，会形成官方客户端不会发送的
	// 外层形态，并可能与 additional_tools 归一化冲突。
	if current, ok := payload["tool_choice"].(string); !ok || current != "auto" {
		payload["tool_choice"] = "auto"
		modified = true
	}

	expectedInclude := []any{"reasoning.encrypted_content"}
	if !reflect.DeepEqual(payload["include"], expectedInclude) {
		payload["include"] = expectedInclude
		modified = true
	}

	if useResponsesLite {
		rawText, exists := payload["text"]
		text, ok := rawText.(map[string]any)
		if !exists || rawText == nil {
			text = map[string]any{}
			payload["text"] = text
			modified = true
		} else if !ok {
			return false, errors.New("OpenAI official egress text must be an object")
		}
		if verbosity, _ := text["verbosity"].(string); verbosity != "low" {
			text["verbosity"] = "low"
			modified = true
		}
	}
	return modified, nil
}

// normalizeDerivedOfficialOpenAIReasoning 复刻 Codex build_reasoning：effort
// 缺省时取模型清单默认值，summary 缺省时取模型默认值；不支持 summary 或显式
// `none` 时按 serde Option 语义省略。reasoning 外层结构体本身始终存在。
func normalizeDerivedOfficialOpenAIReasoning(
	payload map[string]any,
	defaults officialOpenAIReasoningDefaults,
) (bool, error) {
	rawReasoning, exists := payload["reasoning"]
	reasoning, ok := rawReasoning.(map[string]any)
	modified := false
	if !exists || rawReasoning == nil {
		reasoning = make(map[string]any)
		payload["reasoning"] = reasoning
		modified = true
	} else if !ok {
		return false, errors.New("OpenAI official egress reasoning must be an object")
	}

	if rawEffort, exists := reasoning["effort"]; exists {
		effort, ok := rawEffort.(string)
		if !ok || strings.TrimSpace(effort) == "" {
			return false, errors.New("OpenAI official egress reasoning.effort must be a non-empty string")
		}
		// 当前 Release 的 Ultra 只用于本地配置，对 Responses wire 映射为 Max。
		if strings.EqualFold(strings.TrimSpace(effort), "ultra") && effort != "max" {
			reasoning["effort"] = "max"
			modified = true
		}
	} else if effort := strings.TrimSpace(defaults.Effort); effort != "" {
		if strings.EqualFold(effort, "ultra") {
			effort = "max"
		}
		reasoning["effort"] = strings.ToLower(effort)
		modified = true
	}

	if rawSummary, exists := reasoning["summary"]; exists {
		summary, ok := rawSummary.(string)
		if !ok {
			return false, errors.New("OpenAI official egress reasoning.summary must be a string")
		}
		summary = strings.ToLower(strings.TrimSpace(summary))
		switch summary {
		case "none":
			delete(reasoning, "summary")
			modified = true
		case "auto", "concise", "detailed":
			if defaults.Known && !defaults.SupportsSummary {
				delete(reasoning, "summary")
				modified = true
			} else if rawSummary != summary {
				reasoning["summary"] = summary
				modified = true
			}
		default:
			return false, errors.New("OpenAI official egress reasoning.summary is invalid")
		}
	} else if summary := strings.ToLower(strings.TrimSpace(defaults.Summary)); defaults.Known && defaults.SupportsSummary && summary != "" && summary != "none" {
		switch summary {
		case "auto", "concise", "detailed":
			reasoning["summary"] = summary
			modified = true
		default:
			return false, errors.New("OpenAI official egress model reasoning summary is invalid")
		}
	}
	return modified, nil
}

// removeOfficialOpenAIReasoningContext 对齐非 Lite 请求：官方结构体仍发送
// reasoning.effort/summary，但 context 为 None，序列化时必须缺席。
func removeOfficialOpenAIReasoningContext(payload map[string]any) (bool, error) {
	rawReasoning, exists := payload["reasoning"]
	if !exists || rawReasoning == nil {
		return false, nil
	}
	reasoning, ok := rawReasoning.(map[string]any)
	if !ok {
		return false, errors.New("OpenAI official egress reasoning must be an object")
	}
	if _, exists := reasoning["context"]; !exists {
		return false, nil
	}
	delete(reasoning, "context")
	return true, nil
}

// normalizeDerivedOfficialOpenAIInput 把第三方 Responses 的简写 input
// 归一化为 Codex 使用的显式 message 结构。文本和消息顺序保持不变。
func normalizeDerivedOfficialOpenAIInput(payload map[string]any) (bool, error) {
	rawInput, exists := payload["input"]
	if !exists || rawInput == nil {
		payload["input"] = []any{}
		return true, nil
	}
	if text, ok := rawInput.(string); ok {
		payload["input"] = []any{map[string]any{
			"type":    "message",
			"role":    "user",
			"content": []any{map[string]any{"type": "input_text", "text": text}},
		}}
		return true, nil
	}
	input, ok := rawInput.([]any)
	if !ok {
		return false, errors.New("OpenAI official egress input must be a string or array")
	}
	modified := false
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok {
			continue
		}
		if strings.TrimSpace(officialOpenAIString(item, "type")) == "" &&
			strings.TrimSpace(officialOpenAIString(item, "role")) != "" {
			item["type"] = "message"
			modified = true
		}
	}
	return modified, nil
}

// moveOfficialOpenAIHTTPToolsToInput 将第三方标准 Responses 顶层工具无损搬到
// 当前 Codex Release 使用的 input.additional_tools 载体。工具对象本身不改名、不改
// schema；已有 additional_tools 会按类型和名称去重合并。
func moveOfficialOpenAIHTTPToolsToInput(payload map[string]any) (bool, error) {
	rawTools, exists := payload["tools"]
	if !exists || rawTools == nil {
		return moveOfficialOpenAIAdditionalToolsFirst(payload), nil
	}
	tools, ok := rawTools.([]any)
	if !ok {
		return false, errors.New("OpenAI official egress tools must be an array")
	}
	if len(tools) == 0 {
		delete(payload, "tools")
		moveOfficialOpenAIAdditionalToolsFirst(payload)
		return true, nil
	}

	input, err := appendOpenAIResponsesLiteAdditionalTools(payload["input"], tools)
	if err != nil {
		return false, err
	}
	payload["input"] = input
	moveOfficialOpenAIAdditionalToolsFirst(payload)
	delete(payload, "tools")
	return true, nil
}

// moveOfficialOpenAIAdditionalToolsFirst 把所有 additional_tools 稳定移动到
// input 首位，工具项和其余消息各自的相对顺序均保持不变。纯 namespace 请求在
// Lite 规范化后已经没有顶层 tools，因此排序不能依赖顶层工具是否仍然存在。
func moveOfficialOpenAIAdditionalToolsFirst(payload map[string]any) bool {
	input, ok := payload["input"].([]any)
	if !ok || len(input) < 2 {
		return false
	}
	additional := make([]any, 0, 1)
	remaining := make([]any, 0, len(input))
	firstAdditionalIndex := -1
	for _, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if ok && officialOpenAIString(item, "type") == "additional_tools" {
			if firstAdditionalIndex < 0 {
				firstAdditionalIndex = len(additional) + len(remaining)
			}
			additional = append(additional, rawItem)
			continue
		}
		remaining = append(remaining, rawItem)
	}
	if len(additional) == 0 || firstAdditionalIndex == 0 {
		return false
	}
	payload["input"] = append(additional, remaining...)
	return true
}

// moveOfficialOpenAIHTTPInstructionsToInput 将第三方入口的顶层 instructions
// 无损搬到 developer message。当前官方 Codex Release 不发送顶层 instructions，
// 但不能因此丢弃 Kilo 等客户端提供的系统指令。
func moveOfficialOpenAIHTTPInstructionsToInput(payload map[string]any, rawInstructions any) (bool, error) {
	instructions, ok := rawInstructions.(string)
	if !ok {
		return false, errors.New("OpenAI official egress third-party instructions must be a string")
	}
	delete(payload, "instructions")
	if strings.TrimSpace(instructions) == "" {
		return true, nil
	}

	var input []any
	switch value := payload["input"].(type) {
	case nil:
		input = []any{}
	case string:
		input = []any{map[string]any{
			"type":    "message",
			"role":    "user",
			"content": []any{map[string]any{"type": "input_text", "text": value}},
		}}
	case []any:
		input = value
	default:
		return false, errors.New("OpenAI official egress input must be a string or array")
	}

	developerMessage := map[string]any{
		"type": "message",
		"role": "developer",
		"content": []any{map[string]any{
			"type": "input_text",
			"text": instructions,
		}},
	}
	insertAt := 0
	for insertAt < len(input) {
		item, ok := input[insertAt].(map[string]any)
		if !ok || officialOpenAIString(item, "type") != "additional_tools" {
			break
		}
		insertAt++
	}
	if insertAt < len(input) {
		if item, ok := input[insertAt].(map[string]any); ok &&
			officialOpenAIString(item, "role") == "developer" &&
			officialOpenAIHTTPMessageContentText(item["content"]) == strings.TrimSpace(instructions) {
			// 兼容层可能已把同一份 system 语义放入 developer input。
			// Finalizer 只移除顶层 instructions，避免重复提示词和额外 token。
			return true, nil
		}
	}
	input = append(input, nil)
	copy(input[insertAt+1:], input[insertAt:])
	input[insertAt] = developerMessage
	payload["input"] = input
	return true, nil
}

func resolveOfficialOpenAIHTTPIdentity(
	c *gin.Context,
	account *Account,
	body []byte,
	contract *officialOpenAIHTTPBodyContract,
	profileMode string,
) (officialOpenAIHTTPIdentity, error) {
	if contract == nil {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress body contract is nil")
	}
	return deriveOfficialOpenAIHTTPIdentity(c, account, body, contract, profileMode)
}

// resolveExplicitOfficialOpenAIHTTPIdentity 只用于离线 fixture 与诊断，验证某份
// 入站样本内部是否自洽。生产出站不得调用它：入站身份无论来自官方客户端还是
// 第三方客户端，都只作为语义输入，由 resolveOfficialOpenAIHTTPIdentity 重新派生。
func resolveExplicitOfficialOpenAIHTTPIdentity(
	c *gin.Context,
	contract *officialOpenAIHTTPBodyContract,
	isCompact bool,
) (officialOpenAIHTTPIdentity, error) {
	if !contract.promptCacheKeySet || (!isCompact && !contract.clientMetadataSet) {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress requires complete identity from official ingress")
	}
	identity := officialOpenAIHTTPIdentity{
		clientRequest:  strings.TrimSpace(c.GetHeader("x-client-request-id")),
		promptCacheKey: contract.promptCacheKey,
		source:         OfficialEgressFieldSourceIngressExplicit,
		// 官方入口的整份身份都由入站显式携带，会话归属不存在歧义。
		sessionProvenance: officialOpenAIProvenanceExplicitIngress,
		turnProvenance:    officialOpenAIProvenanceExplicitIngress,
	}
	if isCompact {
		// 官方 compact schema 不包含 client_metadata；身份来自同一请求的
		// 标准 Codex Header，installation-id 则来自 compact 专用 Header。
		identity.installationID = strings.TrimSpace(c.GetHeader("X-Codex-Installation-ID"))
		identity.sessionID = strings.TrimSpace(c.GetHeader("session-id"))
		identity.threadID = strings.TrimSpace(c.GetHeader("thread-id"))
		identity.windowID = strings.TrimSpace(c.GetHeader("x-codex-window-id"))
		identity.turnMetadata = strings.TrimSpace(c.GetHeader("x-codex-turn-metadata"))
		identity.turnID = strings.TrimSpace(gjson.Get(identity.turnMetadata, "turn_id").String())
	} else {
		metadata := contract.clientMetadata
		identity.installationID = officialOpenAIString(metadata, "x-codex-installation-id")
		identity.sessionID = officialOpenAIString(metadata, "session_id")
		identity.threadID = officialOpenAIString(metadata, "thread_id")
		identity.windowID = officialOpenAIString(metadata, "x-codex-window-id")
		identity.turnID = officialOpenAIString(metadata, "turn_id")
		identity.turnMetadata = officialOpenAIString(metadata, "x-codex-turn-metadata")
	}
	requiredIdentity := map[string]string{
		"installation_id":  identity.installationID,
		"session_id":       identity.sessionID,
		"thread_id":        identity.threadID,
		"window_id":        identity.windowID,
		"turn_id":          identity.turnID,
		"turn_metadata":    identity.turnMetadata,
		"prompt_cache_key": identity.promptCacheKey,
	}
	if !isCompact {
		requiredIdentity["client_request"] = identity.clientRequest
	}
	for name, value := range requiredIdentity {
		if value == "" {
			return officialOpenAIHTTPIdentity{}, fmt.Errorf("OpenAI official egress requires %s from ingress", name)
		}
	}
	uuidIdentity := map[string]string{
		"installation_id": identity.installationID,
		"session_id":      identity.sessionID,
		"thread_id":       identity.threadID,
		"turn_id":         identity.turnID,
	}
	if !isCompact {
		uuidIdentity["client_request"] = identity.clientRequest
	}
	for name, value := range uuidIdentity {
		if _, err := uuid.Parse(value); err != nil {
			return officialOpenAIHTTPIdentity{}, fmt.Errorf("OpenAI official egress %s must be UUID", name)
		}
	}
	if !isCompact && identity.threadID != identity.clientRequest {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress thread/request identity conflicts")
	}
	windowParts := strings.Split(identity.windowID, ":")
	if len(windowParts) != 2 || windowParts[0] != identity.threadID {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress window_id conflicts with thread")
	}
	windowIndex, err := strconv.Atoi(windowParts[1])
	if err != nil || windowIndex < 0 {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress window_id has invalid index")
	}

	if strings.TrimSpace(c.GetHeader("session-id")) != identity.sessionID ||
		strings.TrimSpace(c.GetHeader("thread-id")) != identity.threadID ||
		strings.TrimSpace(c.GetHeader("x-codex-window-id")) != identity.windowID ||
		strings.TrimSpace(c.GetHeader("x-codex-turn-metadata")) != identity.turnMetadata {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress ingress headers conflict with body identity")
	}
	var turnMetadata map[string]any
	if err := json.Unmarshal([]byte(identity.turnMetadata), &turnMetadata); err != nil {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress turn metadata must be valid JSON")
	}
	if officialOpenAIString(turnMetadata, "installation_id") != identity.installationID ||
		officialOpenAIString(turnMetadata, "session_id") != identity.sessionID ||
		officialOpenAIString(turnMetadata, "thread_id") != identity.threadID ||
		officialOpenAIString(turnMetadata, "turn_id") != identity.turnID ||
		officialOpenAIString(turnMetadata, "window_id") != identity.windowID {
		return officialOpenAIHTTPIdentity{}, errors.New("OpenAI official egress turn metadata conflicts with identity")
	}
	if _, err := validateOfficialOpenAIIngressIdentityKind(
		"OpenAI official egress",
		c,
		contract.clientMetadata,
		turnMetadata,
		officialOpenAIIngressIdentityValues{
			sessionID:      identity.sessionID,
			threadID:       identity.threadID,
			promptCacheKey: identity.promptCacheKey,
		},
		isCompact,
	); err != nil {
		return officialOpenAIHTTPIdentity{}, err
	}
	return identity, nil
}

// deriveOfficialOpenAIHTTPIdentity 为普通第三方客户端生成当前 Codex Release
// 身份。安装 ID 按客户端稳定，会话 ID 按对话锚点稳定，Turn ID 按最后一条
// 用户消息稳定，因此工具结果续轮会复用同一个 Turn。
func deriveOfficialOpenAIHTTPIdentity(
	c *gin.Context,
	account *Account,
	body []byte,
	contract *officialOpenAIHTTPBodyContract,
	profileMode string,
) (officialOpenAIHTTPIdentity, error) {
	accountID := int64(0)
	if account != nil {
		accountID = account.ID
	}
	userAgent := ""
	if c != nil {
		userAgent = c.GetHeader("User-Agent")
	}
	clientScope := fmt.Sprintf(
		"account=%d|api_key=%d|ua=%s",
		accountID,
		getAPIKeyIDFromContext(c),
		NormalizeSessionUserAgent(userAgent),
	)
	installationID := generateSessionUUID(
		"openai-official-egress-installation|" + clientScope,
	)
	identityKind, subagent, ingressParentThreadID, memoryGenerate :=
		resolveDerivedOfficialOpenAIConditionalKind(c, contract)

	firstUserAnchor, lastUserAnchor := officialOpenAIHTTPUserAnchors(body)
	sessionAnchor := officialOpenAIHTTPSessionAnchor(c)
	// 分别记录 session/turn 的来源：兜底锚点只保证“同内容得到同 ID”，
	// 不保证“同 ID 属于同一个会话”。
	sessionAnchorExplicit := sessionAnchor != ""
	if sessionAnchor == "" && contract != nil {
		sessionAnchor = strings.TrimSpace(contract.promptCacheKey)
		if identityKind == officialOpenAIIdentityKindGuardian {
			sessionAnchor = ingressParentThreadID
		}
		// 必须看 promptCacheKeySet 而不是值是否非空：Chat Completions 兼容链会按
		// 模型、工具、system 与首条用户消息自动生成一个 prompt_cache_key，
		// bindGeneratedOfficialOpenAIHTTPBodyContract 明确不设该标志。那种 key 与
		// 首条消息兜底一样只保证“同内容同 ID”，若当成显式锚点，turn-state 会在
		// 内容相同的独立会话间串用，P7 的隔离等于失效。
		sessionAnchorExplicit = contract.promptCacheKeySet && sessionAnchor != ""
	}
	if sessionAnchor == "" {
		sessionAnchor = "first_user=" + firstUserAnchor
	}
	sessionID := generateOfficialStableUUIDV7(
		"openai-official-egress-session|" + clientScope + "|" + sessionAnchor,
	)
	if lastUserAnchor == "" {
		lastUserAnchor = firstUserAnchor
	}
	threadID := sessionID
	if identityKind != officialOpenAIIdentityKindRoot {
		childAnchor := strings.TrimSpace(c.GetHeader("thread-id"))
		if childAnchor == "" && contract != nil {
			childAnchor = officialOpenAIString(contract.clientMetadata, "thread_id")
		}
		threadID = generateOfficialStableUUIDV7(
			"openai-official-egress-child|" + sessionID + "|" + string(identityKind) + "|" + childAnchor,
		)
	}
	turnID := generateOfficialStableUUIDV7(
		"openai-official-egress-turn|" + threadID + "|" + lastUserAnchor,
	)
	sessionProvenance := officialOpenAIProvenanceContentFallback
	turnProvenance := officialOpenAIProvenanceContentFallback
	if sessionAnchorExplicit {
		sessionProvenance = officialOpenAIProvenanceExplicitIngress
		turnProvenance = officialOpenAIProvenanceExplicitSessionDerivedTurn
	}
	windowID := threadID + ":0"

	turnStartedAtUnixMS := resolveOfficialOpenAIHTTPTurnStart(
		c,
		fmt.Sprintf("%d|%s|%s", accountID, sessionID, turnID),
	)
	turnMetadataValues := map[string]any{
		"installation_id":         installationID,
		"session_id":              sessionID,
		"thread_id":               threadID,
		"turn_id":                 turnID,
		"window_id":               windowID,
		"request_kind":            "turn",
		"thread_source":           "user",
		"sandbox":                 "seccomp",
		"turn_started_at_unix_ms": turnStartedAtUnixMS,
	}
	parentThreadID := ""
	if identityKind == officialOpenAIIdentityKindGuardian ||
		(identityKind == officialOpenAIIdentityKindSubagent && ingressParentThreadID != "") {
		parentThreadID = sessionID
		turnMetadataValues["parent_thread_id"] = parentThreadID
	}
	if identityKind == officialOpenAIIdentityKindGuardian || identityKind == officialOpenAIIdentityKindSubagent {
		turnMetadataValues["thread_source"] = "subagent"
		turnMetadataValues["subagent_kind"] = subagent
	}
	if identityKind == officialOpenAIIdentityKindMemoryConsolidation {
		turnMetadataValues["thread_source"] = "memory_consolidation"
	}
	if compaction, ok := resolveDerivedOfficialOpenAICompactionMetadata(c, body, profileMode); ok {
		turnMetadataValues["request_kind"] = "compaction"
		turnMetadataValues["compaction"] = compaction
	}
	// 该结果直接成为 x-codex-turn-metadata 的值。marshal 失败不能退化成空串：
	// 那会让 header 携带一个官方客户端不可能产生的值出站，且沿途无人察觉。
	turnMetadataBytes, err := marshalOfficialOpenAITurnMetadata(turnMetadataValues)
	if err != nil {
		return officialOpenAIHTTPIdentity{}, fmt.Errorf("构造 turn metadata：%w", err)
	}
	promptCacheKey := sessionID
	if identityKind == officialOpenAIIdentityKindGuardian {
		promptCacheKey = "guardian:" + parentThreadID
	}
	return officialOpenAIHTTPIdentity{
		installationID:    installationID,
		sessionID:         sessionID,
		threadID:          threadID,
		windowID:          windowID,
		turnID:            turnID,
		turnMetadata:      string(turnMetadataBytes),
		clientRequest:     threadID,
		promptCacheKey:    promptCacheKey,
		parentThreadID:    parentThreadID,
		subagent:          subagent,
		memoryGenerate:    memoryGenerate,
		source:            OfficialEgressFieldSourceDerived,
		sessionProvenance: sessionProvenance,
		turnProvenance:    turnProvenance,
	}, nil
}

// resolveDerivedOfficialOpenAIConditionalKind 只识别完整、自洽的子代理业务事实。
// 识别失败会降级为根线程语义，不会让入站身份冲突变成 502，也不会把不完整条件
// 带入 wire。动态 UUID 随后仍由统一派生器重建。
func resolveDerivedOfficialOpenAIConditionalKind(
	c *gin.Context,
	contract *officialOpenAIHTTPBodyContract,
) (officialOpenAIIdentityKind, string, string, bool) {
	if c == nil {
		return officialOpenAIIdentityKindRoot, "", "", false
	}
	subagent := strings.TrimSpace(c.GetHeader("x-openai-subagent"))
	memoryGenerate := strings.EqualFold(strings.TrimSpace(c.GetHeader("x-openai-memgen-request")), "true")
	parentThreadID := strings.TrimSpace(c.GetHeader("x-codex-parent-thread-id"))
	if subagent == "" && !memoryGenerate && parentThreadID == "" {
		return officialOpenAIIdentityKindRoot, "", "", false
	}
	clientMetadata := map[string]any{}
	if contract != nil && contract.clientMetadata != nil {
		clientMetadata = contract.clientMetadata
	}
	turnMetadataRaw := officialOpenAIString(clientMetadata, "x-codex-turn-metadata")
	if turnMetadataRaw == "" {
		turnMetadataRaw = strings.TrimSpace(c.GetHeader("x-codex-turn-metadata"))
	}
	var turnMetadata map[string]any
	if err := json.Unmarshal([]byte(turnMetadataRaw), &turnMetadata); err != nil {
		return officialOpenAIIdentityKindRoot, "", "", false
	}
	rawSessionID := officialOpenAIString(clientMetadata, "session_id")
	if rawSessionID == "" {
		rawSessionID = strings.TrimSpace(c.GetHeader("session-id"))
	}
	rawThreadID := officialOpenAIString(clientMetadata, "thread_id")
	if rawThreadID == "" {
		rawThreadID = strings.TrimSpace(c.GetHeader("thread-id"))
	}
	rawPromptCacheKey := ""
	if contract != nil {
		rawPromptCacheKey = strings.TrimSpace(contract.promptCacheKey)
	}
	kind, err := validateOfficialOpenAIIngressIdentityKind(
		"OpenAI official egress conditional semantics",
		c,
		clientMetadata,
		turnMetadata,
		officialOpenAIIngressIdentityValues{
			sessionID: rawSessionID, threadID: rawThreadID,
			promptCacheKey: rawPromptCacheKey,
		},
		isOpenAIResponsesCompactPath(c),
	)
	if err != nil {
		return officialOpenAIIdentityKindRoot, "", "", false
	}
	return kind, subagent, parentThreadID, memoryGenerate
}

func normalizeOfficialCodexConditionalIdentity(
	egressContext *OfficialEgressContext,
	subagent string,
	parentThreadID string,
	memoryGenerate bool,
) {
	if egressContext == nil {
		return
	}
	if egressContext.codexRuntimeState.ConditionalHeaders == nil {
		egressContext.codexRuntimeState.ConditionalHeaders = make(map[string]string)
	}
	for _, name := range []string{
		"x-openai-subagent",
		"x-openai-memgen-request",
		"x-codex-parent-thread-id",
	} {
		delete(egressContext.codexRuntimeState.ConditionalHeaders, name)
	}
	if strings.TrimSpace(subagent) != "" {
		egressContext.codexRuntimeState.ConditionalHeaders["x-openai-subagent"] = strings.TrimSpace(subagent)
	}
	if memoryGenerate {
		egressContext.codexRuntimeState.ConditionalHeaders["x-openai-memgen-request"] = "true"
	}
	if strings.TrimSpace(parentThreadID) != "" {
		egressContext.codexRuntimeState.ConditionalHeaders["x-codex-parent-thread-id"] = strings.TrimSpace(parentThreadID)
	}
}

// resolveDerivedOfficialOpenAICompactionMetadata 把第三方请求能表达的压缩语义收敛为
// 当前 Codex Release 的四种 reason。已有 trigger 不会再次追加；本函数只生成元数据。
// 三种自动 reason 可由入站 x-codex-turn-metadata 原样携带，非法值不会伪造第五种。
func resolveDerivedOfficialOpenAICompactionMetadata(
	c *gin.Context,
	body []byte,
	profileMode string,
) (officialOpenAICompactionMetadata, bool) {
	legacy := isOpenAIResponsesCompactPath(c)
	remoteV2 := HasCompactionTriggerInInput(body) &&
		OfficialCodexRemoteCompactionV2Default(profileMode)
	if !legacy && !remoteV2 {
		return officialOpenAICompactionMetadata{}, false
	}

	reason := "user_requested"
	if c != nil {
		candidate := strings.TrimSpace(gjson.Get(c.GetHeader("x-codex-turn-metadata"), "compaction.reason").String())
		if _, ok := officialOpenAICompactionReasons[candidate]; ok {
			reason = candidate
		}
	}
	metadata := officialOpenAICompactionMetadata{
		Trigger:        "manual",
		Reason:         reason,
		Implementation: "responses_compaction_v2",
		Phase:          "standalone_turn",
		Strategy:       "memento",
	}
	if legacy {
		metadata.Implementation = "responses_compact"
	}
	if reason != "user_requested" {
		metadata.Trigger = "auto"
		metadata.Phase = "mid_turn"
		if reason == "model_downshift" || reason == "comp_hash_changed" {
			metadata.Phase = "pre_turn"
		}
	}
	return metadata, true
}

// resolveOfficialOpenAIHTTPTurnStart 在同一入站请求重试和同一 turn 的独立续轮中
// 复用首次真实开始时间。缓存按账号、会话和 turn 隔离，并受 TTL 与容量双重约束。
func resolveOfficialOpenAIHTTPTurnStart(c *gin.Context, turnKey string) int64 {
	if c != nil {
		if value, exists := c.Get(officialOpenAIHTTPTurnStartKey); exists {
			if startedAt, ok := value.(int64); ok && startedAt > 0 {
				return startedAt
			}
		}
	}
	now := time.Now()
	if turnKey != "" {
		officialOpenAIHTTPTurnStarts.Lock()
		if entry, exists := officialOpenAIHTTPTurnStarts.entries[turnKey]; exists && now.Before(entry.expiresAt) {
			officialOpenAIHTTPTurnStarts.Unlock()
			if c != nil {
				c.Set(officialOpenAIHTTPTurnStartKey, entry.startedAt)
			}
			return entry.startedAt
		}
		officialOpenAIHTTPTurnStarts.Unlock()
	}
	startedAt := now.UnixMilli()
	// 登记与取值必须是同一次加锁的结果：并发首发请求会各自算出毫秒级不同的
	// startedAt，只有采用实际生效的那一份，同一个 turn 才能得到稳定的
	// turn_started_at_unix_ms。
	if turnKey != "" {
		startedAt = rememberOfficialOpenAIHTTPTurnStart(turnKey, startedAt, now)
	}
	if c != nil {
		c.Set(officialOpenAIHTTPTurnStartKey, startedAt)
	}
	return startedAt
}

func rememberOfficialOpenAIHTTPTurnStart(turnKey string, startedAt int64, now time.Time) int64 {
	officialOpenAIHTTPTurnStarts.Lock()
	defer officialOpenAIHTTPTurnStarts.Unlock()
	if officialOpenAIHTTPTurnStarts.entries == nil {
		officialOpenAIHTTPTurnStarts.entries = make(map[string]officialOpenAIHTTPTurnStartEntry)
	}
	if entry, exists := officialOpenAIHTTPTurnStarts.entries[turnKey]; exists && now.Before(entry.expiresAt) {
		return entry.startedAt
	}
	if len(officialOpenAIHTTPTurnStarts.entries) >= officialOpenAIHTTPTurnStartMax {
		oldestKey := ""
		var oldestOrder uint64
		for key, entry := range officialOpenAIHTTPTurnStarts.entries {
			if !now.Before(entry.expiresAt) {
				delete(officialOpenAIHTTPTurnStarts.entries, key)
				continue
			}
			if oldestKey == "" || entry.order < oldestOrder {
				oldestKey = key
				oldestOrder = entry.order
			}
		}
		if len(officialOpenAIHTTPTurnStarts.entries) >= officialOpenAIHTTPTurnStartMax && oldestKey != "" {
			delete(officialOpenAIHTTPTurnStarts.entries, oldestKey)
		}
	}
	officialOpenAIHTTPTurnStarts.nextOrder++
	officialOpenAIHTTPTurnStarts.entries[turnKey] = officialOpenAIHTTPTurnStartEntry{
		startedAt: startedAt,
		expiresAt: now.Add(officialOpenAIHTTPTurnStartTTL),
		order:     officialOpenAIHTTPTurnStarts.nextOrder,
	}
	return startedAt
}

func officialOpenAIHTTPSessionAnchor(c *gin.Context) string {
	if c == nil {
		return ""
	}
	for _, name := range []string{
		"X-Session-Affinity",
		"session_id",
		"conversation_id",
		"X-Session-Id",
		"X-OpenCode-Session",
	} {
		if value := strings.TrimSpace(c.GetHeader(name)); value != "" {
			return strings.ToLower(name) + "=" + value
		}
	}
	return ""
}

func officialOpenAIHTTPUserAnchors(body []byte) (string, string) {
	var payload map[string]any
	if err := json.Unmarshal(body, &payload); err != nil {
		return "", ""
	}
	input, ok := payload["input"].([]any)
	if !ok {
		if text, ok := payload["input"].(string); ok {
			text = strings.TrimSpace(text)
			return text, "0:" + text
		}
		return "", ""
	}
	first := ""
	last := ""
	for index, rawItem := range input {
		item, ok := rawItem.(map[string]any)
		if !ok || officialOpenAIString(item, "type") != "message" ||
			officialOpenAIString(item, "role") != "user" {
			continue
		}
		text := officialOpenAIHTTPMessageContentText(item["content"])
		if text == "" {
			continue
		}
		if first == "" {
			first = text
		}
		last = strconv.Itoa(index) + ":" + text
	}
	return first, last
}

func officialOpenAIHTTPMessageContentText(content any) string {
	switch value := content.(type) {
	case string:
		return strings.TrimSpace(value)
	case []any:
		parts := make([]string, 0, len(value))
		for _, rawPart := range value {
			part, ok := rawPart.(map[string]any)
			if !ok {
				continue
			}
			text := officialOpenAIString(part, "text")
			if text == "" {
				text = officialOpenAIString(part, "input_text")
			}
			if text != "" {
				parts = append(parts, text)
			}
		}
		return strings.TrimSpace(strings.Join(parts, "\n"))
	default:
		return ""
	}
}

func officialOpenAIString(values map[string]any, key string) string {
	value, _ := values[key].(string)
	return strings.TrimSpace(value)
}

func registerOfficialOpenAIHTTPIdentity(
	egressContext *OfficialEgressContext,
	identity officialOpenAIHTTPIdentity,
	isCompact bool,
) error {
	fields := []struct {
		name      OfficialEgressFieldName
		value     string
		lifecycle OfficialEgressFieldLifecycle
	}{
		{name: OfficialEgressFieldDeviceID, value: identity.installationID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldSessionID, value: identity.sessionID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldThreadID, value: identity.threadID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldWindowID, value: identity.windowID, lifecycle: OfficialEgressFieldLifecycleSession},
		{name: OfficialEgressFieldTurnMetadata, value: identity.turnMetadata, lifecycle: OfficialEgressFieldLifecycleTurn},
		{name: OfficialEgressFieldPromptCacheKey, value: identity.promptCacheKey, lifecycle: OfficialEgressFieldLifecycleSession},
	}
	if !isCompact {
		fields = append(fields, struct {
			name      OfficialEgressFieldName
			value     string
			lifecycle OfficialEgressFieldLifecycle
		}{
			name: OfficialEgressFieldClientRequestID, value: identity.clientRequest,
			lifecycle: OfficialEgressFieldLifecycleSession,
		})
	}
	for _, field := range fields {
		if err := egressContext.RegisterField(
			field.name,
			field.value,
			identity.source,
			field.lifecycle,
		); err != nil {
			return err
		}
	}
	return nil
}

func finalizeOfficialOpenAIHTTPHeaders(
	header http.Header,
	// version 来自运行上下文的 active 画像版本，不再是编译期常量：辅助端点与 WS
	// 握手的 version 头由画像槽位生成，主链若继续写死常量，升级后两者就会分叉。
	version string,
	userAgent string,
	originator string,
	identity officialOpenAIHTTPIdentity,
	isCompact bool,
	useResponsesLite bool,
	turnState string,
) {
	for _, name := range []string{
		"conversation_id",
		"session_id",
		"OpenAI-Beta",
		"X-Codex-Installation-ID",
		"x-codex-beta-features",
		"x-codex-turn-state",
		"x-codex-window-id",
		"x-codex-turn-metadata",
		"x-client-request-id",
		"session-id",
		"thread-id",
		"accept",
		"content-type",
		"user-agent",
		"originator",
		"version",
		responsesLiteHeader,
	} {
		deleteHeaderAllForms(header, name)
	}
	stripOfficialEgressInboundHostHeaders(header)

	// 这里保持 Go 的 canonical 写法，wire 上的全小写形态由官方画像 Transport 统一收口
	// （见 newOfficialEgressLowercaseHeaderRoundTripper）：官方 Codex 走 Rust hyper，
	// h1 线上 header 名一律小写，而 Go 的 Header.Set 会改写成 Session-Id / Originator。
	// 把小写化放在传输层而非此处，是为了让语义定型与 wire 形态分层，且不影响上层断言。
	if isCompact {
		// 官方 compact 走 execute（端点层不设 accept），由 reqwest 补默认 */*。
		// Go 不会自动补，因此必须显式设置——此前 Del 会导致出站彻底缺该头。
		header.Set("Accept", "*/*")
	} else {
		header.Set("Accept", "text/event-stream")
	}
	header.Set("Content-Type", "application/json")
	header.Set("User-Agent", userAgent)
	header.Set("originator", originator)
	header.Set("session-id", identity.sessionID)
	header.Set("thread-id", identity.threadID)
	if !isCompact {
		header.Set("x-client-request-id", identity.clientRequest)
	}
	header.Set("x-codex-turn-metadata", identity.turnMetadata)
	header.Set("x-codex-window-id", identity.windowID)
	header.Set("version", version)
	if !isCompact {
		header.Set("x-codex-beta-features", officialOpenAIHTTPBetaFeatures)
	}
	header.Del(responsesLiteHeader)
	if useResponsesLite {
		header.Set(responsesLiteHeader, officialOpenAIHTTPResponsesLite)
	}
	if isCompact {
		header.Set("X-Codex-Installation-ID", identity.installationID)
	}
	if turnState = strings.TrimSpace(turnState); turnState != "" {
		header.Set("x-codex-turn-state", turnState)
	}
}
