package profilecontract

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"slices"
	"sort"
	"strings"
)

// 可选跨端点节：由目标版本画像声明的新行为开关（0.156.1 起）。
//
// # 语义
//
// 节不存在表示该版本画像没有这项行为，执行层保持旧逻辑；节存在时必须完整合法，
// 未知字段、显式 null、空列表、未排序或重复的取值一律拒绝，避免拼写错误让开关静默失效。
//
// # 与旧版本画像的关系
//
// 这些节只在存在时进入 ProfileSpec.crossSections 与可执行投影；快照 DTO 对它们使用
// omitempty。因此 0.154.0、0.151.0 等不含这些节的画像，规范化 JSON、ProfileDigest 与
// executable 摘要逐字节不变，回退到旧版本时也不会带出新行为。

const (
	// SectionCookieJar：共享 Cookie jar 的名单与 WS 握手回写（SPEC-H1-004）。
	SectionCookieJar = "CookieJar"
	// SectionWorkspaceRouting：工作区路由发现与非默认路由处置（SPEC-EP-002 及路由 override 头）。
	SectionWorkspaceRouting = "WorkspaceRouting"
	// SectionTurnMetadata：x-codex-turn-metadata 的目标键集与取值（压缩闭集、analytics_enabled 等）。
	SectionTurnMetadata = "TurnMetadata"
	// SectionClientMetadata：请求体 client_metadata 中由客户端固定写入的常量键。
	SectionClientMetadata = "ClientMetadata"
	// SectionTurnState：turn-state 保存与清空条件（SPEC-BODY-004）。
	SectionTurnState = "TurnState"
	// SectionWebSocketRetry：WS 可重试错误与降级预算（SPEC-PROTO-002）。
	SectionWebSocketRetry = "WebSocketRetry"
	// SectionWebSocketContinuation：WS 增量续接（previous_response_id）的失效条件（SPEC-WS-005）。
	SectionWebSocketContinuation = "WebSocketContinuation"
	// SectionReasoningEffort：reasoning.effort 的出站序列化（SPEC-BODY-006）。
	SectionReasoningEffort = "ReasoningEffort"
	// SectionImageGeneration：imagegen 出站的 background 取值与编辑图片引用形态（SPEC-EP-022）。
	SectionImageGeneration = "ImageGeneration"
	// SectionBaseInstructions：基础指令的承载方式（不再发顶层 instructions，改为 input 最前的 developer 消息）。
	SectionBaseInstructions = "BaseInstructions"
	// SectionAccessPrograms：请求体条件字段 access_programs 的出站值域。
	SectionAccessPrograms = "AccessPrograms"
)

// OptionalSectionNames 按快照字段顺序列出全部可选节；顺序也是数据，不排序。
var OptionalSectionNames = []string{
	SectionCookieJar,
	SectionWorkspaceRouting,
	SectionTurnMetadata,
	SectionClientMetadata,
	SectionTurnState,
	SectionWebSocketRetry,
	SectionWebSocketContinuation,
	SectionReasoningEffort,
	SectionImageGeneration,
	SectionBaseInstructions,
	SectionAccessPrograms,
}

// CookieJarSection 声明共享 Cookie jar 接受的 Cookie 名与 WS 握手回写。
type CookieJarSection struct {
	// AllowedNames：允许保存并回送的 Cookie 名（排序去重）。
	AllowedNames []string `json:"AllowedNames"`
	// AllowedPrefixes：按前缀允许的 Cookie 名（排序去重，例如 Cloudflare 挑战 Cookie）。
	AllowedPrefixes []string `json:"AllowedPrefixes"`
	// WebSocketHandshakeWriteBack：WS 握手响应（101 与被拒绝的升级）中的 Set-Cookie 写回同一 jar。
	WebSocketHandshakeWriteBack bool `json:"WebSocketHandshakeWriteBack"`
}

// WorkspaceRoutingSection 声明工作区路由发现请求与路由结果的处置。
type WorkspaceRoutingSection struct {
	// DiscoveryEndpointID：发现请求所用端点，必须存在于同一画像。
	DiscoveryEndpointID string `json:"DiscoveryEndpointID"`
	// DefaultOrigin：默认业务 origin。
	DefaultOrigin string `json:"DefaultOrigin"`
	// AcceptedOriginValues：workspace_backend_origin 中视为默认路由的取值（排序去重）。
	AcceptedOriginValues []string `json:"AcceptedOriginValues"`
	// AcceptedOverrideValues：account_routing_override 中视为无需 override 的取值（排序去重）。
	AcceptedOverrideValues []string `json:"AcceptedOverrideValues"`
	// OverrideHeader：官方客户端在非默认路由时追加的请求头名（记录用，首期不发出）。
	OverrideHeader string `json:"OverrideHeader"`
	// NonDefaultAction：发现非默认路由时的处置，目前只支持 fail_closed。
	NonDefaultAction string `json:"NonDefaultAction"`
	// RoutedEndpointIDs：受路由结果影响的端点（排序去重），必须存在于同一画像。
	RoutedEndpointIDs []string `json:"RoutedEndpointIDs"`
}

// TurnMetadataSection 声明 x-codex-turn-metadata 的目标键集与固定取值。
type TurnMetadataSection struct {
	// Keys：目标版本 turn metadata 的完整键集（排序去重）。
	Keys []string `json:"Keys"`
	// AnalyticsEnabled：analytics_enabled 的出站取值（按官方取证值固定，不透传下游）。
	AnalyticsEnabled bool `json:"AnalyticsEnabled"`
	// CompactionImplementations：compaction.implementation 允许的取值（排序去重）。
	CompactionImplementations []string `json:"CompactionImplementations"`
	// CompactionPhases：compaction.phase 允许的取值（排序去重）。
	CompactionPhases []string `json:"CompactionPhases"`
	// SessionDefaults：官方 CLI 默认路径的会话模型取值（可选）。独立 images 端点的 turn metadata 里，
	// model 是会话模型（不是请求体里的图像模型），reasoning_effort 是该会话的生效推理强度；入站没有同一回合的
	// Responses 回合事实（非 Codex 客户端直接调图像接口）时，网关没有别的可信来源，就取这里登记的默认值。
	// 缺省表示不补这几个键（旧画像行为，字节与摘要不变）。
	SessionDefaults *TurnMetadataSessionDefaults `json:"SessionDefaults,omitempty"`
	// WindowTracking：WS 回合元数据按官方跟踪上下文窗口。
	//
	// 官方客户端的上下文窗口由“窗口号”（x-codex-window-id 的 `<线程>:<窗口号>` 后缀，turn metadata 里的 window_id 与
	// window_number）和“上下文窗口 id”（turn metadata 的 context_window_id，每个窗口一个 UUIDv7）标识；上下文压缩后
	// 进入新窗口，窗口号加一、上下文窗口 id 换新，之后的请求与新建连接的握手都用新值。握手头只在建连时发出，所以复用
	// 的连接上握手里仍是旧窗口号，新窗口号只出现在每帧 client_metadata 里。声明 true 后，网关在 WS 回合元数据里写入
	// window_number 与 context_window_id，并跟随下游客户端自己的窗口号推进（压缩之后客户端帧里的窗口号加一）：帧里的
	// x-codex-window-id、turn metadata 的 window_id／window_number／context_window_id，以及新建连接握手里的窗口头与
	// turn metadata 都用推进后的值。缺省（false）表示旧行为：窗口号固定为 `:0`，不写这两个键。
	// 声明时 Keys 必须包含 window_id、window_number、context_window_id。
	//
	// omitempty：缺省时规范化 JSON 与各类摘要和引入本字段之前逐字节相同；显式写 false 会让同一份语义出现两种表示，
	// 因此解码阶段拒绝（见 DecodeOptionalSection）。
	WindowTracking bool `json:"WindowTracking,omitempty"`
	// WSCompactionRequests：WS 上的压缩请求（`compaction_trigger` 增量帧）按官方写元数据与请求体。
	//
	// 官方压缩请求是一帧 previous_response_id 加单项 `{"type":"compaction_trigger"}` 的增量帧：请求体不带 access_programs；
	// turn metadata 的 request_kind 是 compaction，带 compaction{trigger,reason,implementation,phase,strategy}，不带
	// model、reasoning_effort、turn_trigger（官方 0.165 实采）。声明 true 后网关对这类帧同样这样写；compaction 取值落在本节
	// CompactionImplementations／CompactionPhases 闭集内（声明时 CompactionImplementations 必须含 responses_compaction_v2）。
	// 缺省（false）表示旧行为：按普通业务帧处理（补 access_programs、request_kind=turn、写 model 与 reasoning_effort）。
	//
	// omitempty：缺省时规范化 JSON 与各类摘要逐字节不变；显式写 false 会让同一份语义出现两种表示，因此解码阶段拒绝。
	WSCompactionRequests bool `json:"WSCompactionRequests,omitempty"`
	// HTTPCompactionRequests：HTTP `/responses` 上的压缩请求（input 里带 `compaction_trigger`）按官方写元数据与请求体。
	//
	// 官方 0.165 的 HTTP 压缩请求（实采 HTTP 压缩续聊会话）与 WS 压缩帧一样：请求体不带 access_programs；turn metadata 的
	// request_kind 是 compaction，带 compaction{trigger,reason,implementation,phase,strategy}（排在最后一个键），不带 model、
	// reasoning_effort、turn_trigger。声明 true 后网关对 HTTP 上的压缩请求同样这样写；compaction 取值落在本节
	// CompactionImplementations／CompactionPhases 闭集内（声明时 CompactionImplementations 必须含 responses_compaction_v2）。
	// 缺省（false）表示旧行为：HTTP 压缩请求按普通回合请求处理（补 access_programs、写 model／reasoning_effort／turn_trigger）。
	// 与 WSCompactionRequests 各自独立：WS 与 HTTP 是两条出站路径，画像按路径分别声明。
	//
	// omitempty：缺省时规范化 JSON 与各类摘要逐字节不变；显式写 false 会让同一份语义出现两种表示，因此解码阶段拒绝。
	HTTPCompactionRequests bool `json:"HTTPCompactionRequests,omitempty"`
	// RootThreadKeys：根线程回合写 agent_name 与 root_turn_id。
	//
	// 官方客户端的 turn metadata 里，agent_name 是线程的代理路径（根线程为 `/root`，握手与每帧都带），root_turn_id 是根回合的
	// 回合 id（根线程上等于本回合的 turn_id，预热帧没有回合所以不带；官方 0.165 实采，TUI 与 exec 一致）。子代理、guardian
	// 审阅与记忆合并线程的取值没有样本，网关不写。声明 true 后网关只对根线程写这两个键；声明时 Keys 必须包含 agent_name 与
	// root_turn_id。缺省（false）表示旧行为：不写这两个键。
	//
	// omitempty：缺省时规范化 JSON 与各类摘要逐字节不变；显式写 false 会让同一份语义出现两种表示，因此解码阶段拒绝。
	RootThreadKeys bool `json:"RootThreadKeys,omitempty"`
}

// TurnMetadataSessionDefaults 是目标版本客户端默认会话模型的 turn metadata 取值，登记时取自该版本官方内置
// 模型目录里的默认模型（排第一的 list 模型）。三项都属于同一条目录记录，所以放在一起声明，避免各处分别硬编码。
type TurnMetadataSessionDefaults struct {
	// Model：默认会话模型 slug。
	Model string `json:"Model"`
	// ReasoningEffort：该模型的默认推理强度（目录 default_reasoning_level）；为空表示没有生效强度，
	// 此时 turn metadata 不写 reasoning_effort。
	ReasoningEffort string `json:"ReasoningEffort"`
	// NodeReplAutoReviewRequired：该模型目录条目的 node_repl_auto_review_required。
	NodeReplAutoReviewRequired bool `json:"NodeReplAutoReviewRequired"`
}

// ClientMetadataSection 声明请求体 client_metadata 中客户端固定写入的常量键。
type ClientMetadataSection struct {
	Constants map[string]string `json:"Constants"`
	// Condition：常量的写入条件，取引擎支持的请求条件闭集；恒定写入时为 always。
	// 0.156.1 的 guardian 审阅请求不写 guardian_credits_requested，取 not_guardian_review_request。
	Condition ConditionKind `json:"Condition"`
	// KeyConditions：按键覆盖 Condition 的写入条件（可选）。同一节里的常量写入条件
	// 不同时使用，例如较新版本的 mcp_attribution 对 guardian 审阅请求同样写入（always），
	// 而 guardian_credits_requested 仍取 Condition。键必须出现在 Constants 中，条件取
	// 引擎支持的闭集且不得为空串；存在时不能为空表。omitempty 保证未使用该字段的节
	// 序列化形态与摘要不变。
	KeyConditions map[string]ConditionKind `json:"KeyConditions,omitempty"`
	// DynamicKeys：由客户端逐请求写入的动态键（排序去重，取值见 clientMetadataDynamicKeyKinds）。端点范围：WS 帧三个键都生效；
	// HTTP 回合请求只有 root_turn_id 生效（Lite 在 HTTP 上是请求头，发送时间戳是 WS 专属）。
	//
	// 官方 0.165 的 WS response.create 的 client_metadata 除身份键与常量外还带：root_turn_id（根回合 id，预热帧没有）、
	// ws_request_header_x_openai_internal_codex_responses_lite（本请求的模型走 Lite 时为 "true"）、
	// x-codex-ws-stream-request-start-ms（发送前一刻的 Unix 毫秒时间戳，每个 WS 请求都带，含预热帧）。这些键不是常量也不是
	// 身份事实，编译器按本字段声明写入：root_turn_id 与 Lite 取自调用方提供的帧事实，时间戳由编译器在编译该帧时取当前时间。
	// 缺省表示旧行为：client_metadata 只含身份键与常量。键不得与 Constants 重名。
	//
	// omitempty：缺省时规范化 JSON 与各类摘要逐字节不变；显式写空列表会让同一份语义出现两种表示，因此校验要求列表非空。
	DynamicKeys []string `json:"DynamicKeys,omitempty"`
}

// ConditionFor 返回常量键的写入条件：KeyConditions 覆盖优先，其余键取 Condition。
func (s ClientMetadataSection) ConditionFor(key string) ConditionKind {
	if condition, ok := s.KeyConditions[key]; ok {
		return condition
	}
	return s.Condition
}

// TurnStateSection 声明 turn-state 的清空条件。
type TurnStateSection struct {
	// ResetOnAccountOwnerChange：同一会话内上游账号 owner 变化时丢弃已保存的 turn-state 与 WS 续接状态。
	ResetOnAccountOwnerChange bool `json:"ResetOnAccountOwnerChange"`
	// ResetOnNewTurn：turn-state 只在产生它的那个回合内回送，新用户轮次开始时清空。
	//
	// 官方客户端每个回合新建一个模型客户端会话（核心 turn 流程里的 new_session），会话里保存 turn-state 的
	// 单次写入容器随之换新：同一回合内的后续请求（工具结果续轮、压缩等）回送本回合已收到的值，新回合的第一个请求
	// 不回送上一回合的值。例外：会话启动预热帧的响应里收到的值随“预热过的会话”交给第一个回合，所以紧跟预热帧的
	// 第一个业务帧仍回送。节里声明 true 后，网关对派生出站的新用户轮次（不是工具结果续轮）同样清空已保存的值；
	// 缺省（false）表示旧行为：保存的值在整个会话内回送给每个后续请求。
	//
	// omitempty：缺省时规范化 JSON 与各类摘要和引入本字段之前逐字节相同；显式写 false 会让同一份语义出现两种表示，
	// 因此解码阶段拒绝（见 DecodeOptionalSection）。
	ResetOnNewTurn bool `json:"ResetOnNewTurn,omitempty"`
}

// WebSocketRetrySection 声明 WS 可重试错误与降级预算。
type WebSocketRetrySection struct {
	// RetryableErrorCodes：握手拒绝体或流内 wrapped error 中视为可重试的 error.code（排序去重）。
	RetryableErrorCodes []string `json:"RetryableErrorCodes"`
	// RetryBudget：计入 stream 重试预算的次数上限。
	RetryBudget int `json:"RetryBudget"`
	// FallbackTransport：预算耗尽后的降级传输，目前只支持 http。
	FallbackTransport string `json:"FallbackTransport"`
}

// WebSocketContinuationSection 声明 WS 增量续接的失效条件。
type WebSocketContinuationSection struct {
	// ResetOn：任一条件变化即新建 WS 并全量发送（排序去重，取值见 continuationResetKinds）。
	ResetOn []string `json:"ResetOn"`
	// ReplayOnSameConnection：声明官方客户端在“请求属性变化或输入前缀不再可复用”时的整段重放形态。
	//
	// 官方客户端每次发 WS 请求前先判断能否在上一次请求加其输出之上续链；不能时（换模型、工具目录或推理档位等
	// 请求属性变化，上下文压缩后新窗口，input 不再以上次请求加输出为前缀，相邻 configuration_update 合并等）
	// 在原连接上发一帧不带 previous_response_id、不带 generate 的全量 response.create：不重连、不补预热、
	// permessage-deflate 上下文延续。只有认证代次、账号 owner 等连接键变化（见 ResetOn）或连接已断才会新建连接。
	//
	// 节里声明 true 后，执行层对“自己管理续链”的下游客户端（本会话内自己发过 generate=false 预热帧）发来的这类
	// 全量帧，复用原上游连接并原样转发：不新建连接、不补预热帧、不把预热响应挂成 previous_response_id。
	// 其余下游客户端（第三方，每个用户轮次都是整段历史）仍按旧方式归一成官方形态。
	// 缺省（false）表示旧行为：全量帧一律由新连接承接并补预热。
	//
	// omitempty：缺省时规范化 JSON 与各类摘要和引入本字段之前逐字节相同；显式写 false 会让同一份语义出现两种表示，
	// 因此解码阶段拒绝（见 DecodeOptionalSection）。
	ReplayOnSameConnection bool `json:"ReplayOnSameConnection,omitempty"`
}

// ReasoningEffortSection 声明 reasoning.effort 的出站序列化。
//
// 官方客户端对模型清单之外的自定义档位（ReasoningEffortConfig::Custom）做 u64 解析，解析成功时发 JSON 整数，
// 已知档位与解析不成功的自定义值照旧发字符串；HTTP 与 WS response.create 共用同一结构体。节缺省表示该版本
// 画像一律发字符串（旧版本行为）。
type ReasoningEffortSection struct {
	// CustomNumericSerialization：自定义档位能按 u64 解析时的出站形态，目前只支持 u64_integer。
	CustomNumericSerialization string `json:"CustomNumericSerialization"`
	// ConfigurationUpdate：较新版本官方客户端默认开启 reasoning_effort_override 后，窗口内 effort 的变化以 input 里的
	// configuration_update 项承载（请求级 reasoning.effort 在窗口内保持基线）。本字段声明 HTTP 出站（Responses HTTP，
	// 对应官方不带 WS 预热的 provider）对第三方入站如何呈现这种项；缺省表示不合成（旧版本行为，也是 WS 出站的唯一取值：
	// 有 WS 预热的路径窗口首轮不带该项）。目前只支持 http_first_turn：模型目录声明 supports_reasoning_effort_updates 的
	// 模型，每个窗口在首个用户轮次之后带一条取值为请求 effort 的 update，之后各请求随历史在同一位置重放。
	ConfigurationUpdate string `json:"ConfigurationUpdate,omitempty"`
}

// ReasoningEffortConfigurationUpdateHTTPFirstTurn 是 ReasoningEffortSection.ConfigurationUpdate 目前唯一的非缺省取值。
const ReasoningEffortConfigurationUpdateHTTPFirstTurn = "http_first_turn"

// ImageGenerationSection 声明 imagegen 出站的 background 取值与编辑请求的图片引用形态。
//
// 较新版本的 imagegen 按工具参数 transparent_background 决定 background：要求透明时发 TransparentBackground，
// 否则发 DefaultBackground（不再固定 auto）；编辑请求 images 项除 {"image_url": …} 外，会话里 file-backed 图片以
// {"file_id": …} 发出。节缺省表示旧逻辑：background 原样透传、编辑只收 image_url。
type ImageGenerationSection struct {
	// DefaultBackground：未要求透明背景时发出的 background（取值 auto 或 opaque）。
	DefaultBackground string `json:"DefaultBackground"`
	// TransparentBackground：要求透明背景时发出的 background，目前只支持 transparent。
	TransparentBackground string `json:"TransparentBackground"`
	// EditImageReferences：编辑请求 images 项允许的引用形态（排序去重，取值 file_id、image_url，必须含 image_url）。
	EditImageReferences []string `json:"EditImageReferences"`
}

// BaseInstructionsSection 声明基础指令（base instructions）的承载方式。
//
// 较新版本的非 Lite 请求体不再发送顶层 instructions：基础指令作为 input 最前的 developer 消息承载，
// 消息的 internal_chat_message_metadata_passthrough 以 content_item_kinds 标记它的身份。节存在即表示
// 该版本画像采用这种载体，节缺省表示旧逻辑（顶层 instructions；Lite 请求的迁入形态不变）。
// 声明本节时，EndpointIDs 所列端点的 Body.Fields 不得再含 instructions，且必须含 input
// （交叉校验见 validateBaseInstructionsCrossReferences），避免画像自相矛盾。
type BaseInstructionsSection struct {
	// Carrier：承载形态，目前只支持 input_developer_message。
	Carrier string `json:"Carrier"`
	// ContentItemKind：消息 passthrough 里 content_item_kinds 的唯一取值（点分小写标识）。
	ContentItemKind string `json:"ContentItemKind"`
	// EndpointIDs：适用的 Responses 端点（排序去重，取值 responses_http、responses_ws，必须存在于同一画像）。
	EndpointIDs []string `json:"EndpointIDs"`
	// IDPrefix：消息 id 的前缀。id 的生成规则由 IDScheme 决定：缺省时 id = 前缀_UUIDv5(UUIDv5(NAMESPACE_OID, thread_id), 指令正文)。
	IDPrefix string `json:"IDPrefix"`
	// IDScheme：消息 id 方案与前缀承载形态，缺省表示旧方案；目前只支持 uuidv7。
	//
	// 旧方案（缺省）：每个请求现场拼装前缀，基础指令消息的 id 由线程与正文确定性算出；WS 上前缀随预热帧发送，
	// 业务帧不重复。
	//
	// uuidv7：较新版本的客户端对 Lite 模型默认走“增量工具窗口”——工具目录（additional_tools）与基础指令消息
	// 在窗口开头作为历史项记录一次，id 是记录时随机生成的 前缀_UUIDv7，之后随历史原样重放；WS 预热帧的
	// input 为空，前缀改由首个业务帧携带。声明本方案后：
	//   - 入站已是官方形态的基础指令消息（Codex 客户端自带）原样保留，不重算 id、不改 passthrough；
	//   - 入站没有官方形态消息（第三方客户端）时才合成：id 是按 (线程, 内容) 稳定复用的 UUIDv7，passthrough 按官方
	//     键序带 turn_id、create_time、content_item_kinds、content_item_metadata；同一线程同一内容在各请求上逐字节相同，
	//     上游前缀缓存不会被 id 或时间戳的随机性破坏；
	//   - additional_tools 项同理：入站已带 id 的原样保留，没有 id 的补 at_ 加稳定 UUIDv7，键序固定为 type,id,role,tools；
	//   - WS 上 additional_tools 项不补 passthrough（官方该项没有这个字段）。
	// 非 Lite 模型不走增量窗口，仍按旧方案处理。本字段只改变上述行为，不改变任何请求头、字段闭集或摘要口径。
	IDScheme string `json:"IDScheme,omitempty"`
}

// BaseInstructionsIDSchemeUUIDv7 是 BaseInstructionsSection.IDScheme 目前唯一支持的取值。
const BaseInstructionsIDSchemeUUIDv7 = "uuidv7"

// UsesUUIDv7IDs 报告本节是否声明了 uuidv7 方案（见 IDScheme）。接收者为 nil 时返回 false。
func (s *BaseInstructionsSection) UsesUUIDv7IDs() bool {
	return s != nil && s.IDScheme == BaseInstructionsIDSchemeUUIDv7
}

// AccessProgramsSection 声明请求体条件字段 access_programs 的出站值域。
//
// 较新版本的 Responses 请求体可携带 access_programs（形如 {"cyber":"standard"}）。账号是否有资格由服务端裁决，
// 网关没有账号资格信息，所以画像只列出允许出站的 cyber 程序子集，入站取值落在子集之外时由执行层丢弃，
// 而不是原样透传。本节与两个 Responses 端点 Body.Fields 中的 access_programs 声明必须同时存在
// （交叉校验见 validateAccessProgramsCrossReferences）：声明了字段却没有值域，会让入站值隐性透传。
type AccessProgramsSection struct {
	// AllowedCyberPrograms：允许出站的 cyber 程序（排序去重，取值 daybreak_blue、daybreak_red、standard）。
	AllowedCyberPrograms []string `json:"AllowedCyberPrograms"`
	// DefaultCyberProgram：入站未携带时默认生成的程序。缺省表示不默认生成；存在时必须属于
	// AllowedCyberPrograms，且不得显式写成空串（空串会让规范化 JSON 多出一种与缺省等价的表示）。
	DefaultCyberProgram string `json:"DefaultCyberProgram,omitempty"`
}

var (
	sectionTokenPattern   = regexp.MustCompile(`^[a-z0-9_]+$`)
	cookieNamePattern     = regexp.MustCompile(`^[A-Za-z0-9_-]+$`)
	headerNamePattern     = regexp.MustCompile(`^[a-z0-9-]+$`)
	errOptionalSectionNil = errors.New("可选节不得显式为 null")

	// contentItemKindPattern 约束基础指令消息的 content_item_kinds 取值：至少两段、点分的小写标识。
	contentItemKindPattern = regexp.MustCompile(`^[a-z_]+(\.[a-z_]+)+$`)
	// messageIDPrefixPattern 约束基础指令消息 id 的前缀。
	messageIDPrefixPattern = regexp.MustCompile(`^[a-z]{2,8}$`)

	imageReferenceKinds = map[string]bool{
		"file_id":   true,
		"image_url": true,
	}

	// cyberProgramKinds 是 access_programs.cyber 的取值全集；画像节只能从中挑出允许出站的子集。
	cyberProgramKinds = map[string]bool{
		"daybreak_blue": true,
		"daybreak_red":  true,
		"standard":      true,
	}

	continuationResetKinds = map[string]bool{
		"account_owner":             true,
		"account_routing_override":  true,
		"auth_revision":             true,
		"base_url":                  true,
		"late_tool_result_metadata": true,
	}
)

// sessionModelSlugPattern 约束默认会话模型 slug：小写字母、数字、点、下划线、连字符，首字符为字母或数字。
var sessionModelSlugPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*$`)

// responsesEndpointIDs 是基础指令载体与 access_programs 所约束的两个 Responses 端点（已排序）。
var responsesEndpointIDs = []string{"responses_http", "responses_ws"}

// validateTurnMetadataSessionDefaults 校验会话默认值：模型 slug 与强度的形态，且对应的键必须出现在节 Keys 里，
// 否则执行层会因为键未声明而静默丢弃取值。
func validateTurnMetadataSessionDefaults(section *TurnMetadataSection) error {
	defaults := section.SessionDefaults
	if defaults == nil {
		return nil
	}
	if !sessionModelSlugPattern.MatchString(defaults.Model) {
		return fmt.Errorf("SessionDefaults.Model 非法: %q", defaults.Model)
	}
	if !slices.Contains(section.Keys, "model") {
		return errors.New("SessionDefaults 存在时 Keys 必须包含 model")
	}
	if defaults.ReasoningEffort != "" {
		if !sectionTokenPattern.MatchString(defaults.ReasoningEffort) {
			return fmt.Errorf("SessionDefaults.ReasoningEffort 非法: %q", defaults.ReasoningEffort)
		}
		if !slices.Contains(section.Keys, "reasoning_effort") {
			return errors.New("SessionDefaults.ReasoningEffort 非空时 Keys 必须包含 reasoning_effort")
		}
	}
	return nil
}

// DecodeOptionalSection 严格解码并校验一个可选节；未知节名、未知字段或非法取值均返回错误。
func DecodeOptionalSection(name string, raw json.RawMessage) (any, error) {
	trimmed := bytes.TrimSpace(raw)
	if len(trimmed) == 0 || bytes.Equal(trimmed, []byte("null")) {
		return nil, fmt.Errorf("%s: %w", name, errOptionalSectionNil)
	}
	var target any
	switch name {
	case SectionCookieJar:
		target = &CookieJarSection{}
	case SectionWorkspaceRouting:
		target = &WorkspaceRoutingSection{}
	case SectionTurnMetadata:
		target = &TurnMetadataSection{}
	case SectionClientMetadata:
		target = &ClientMetadataSection{}
	case SectionTurnState:
		target = &TurnStateSection{}
	case SectionWebSocketRetry:
		target = &WebSocketRetrySection{}
	case SectionWebSocketContinuation:
		target = &WebSocketContinuationSection{}
	case SectionReasoningEffort:
		target = &ReasoningEffortSection{}
	case SectionImageGeneration:
		target = &ImageGenerationSection{}
	case SectionBaseInstructions:
		target = &BaseInstructionsSection{}
	case SectionAccessPrograms:
		target = &AccessProgramsSection{}
	default:
		return nil, fmt.Errorf("未知可选节: %s", name)
	}
	// 节内字段同样不得显式为 null：null 会被解码成缺省值，却让规范化 JSON 与摘要多出一种表示。
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(trimmed, &fields); err == nil {
		for key, value := range fields {
			if bytes.Equal(bytes.TrimSpace(value), []byte("null")) {
				return nil, fmt.Errorf("可选节 %s 的字段 %s 不得显式为 null", name, key)
			}
		}
		// omitempty 的字符串字段同理不得显式写成空串：空串解码后与缺省无法区分，
		// 规范化 JSON 却会保留它，同一份语义于是对应两种官方摘要。
		if name == SectionAccessPrograms {
			if value, declared := fields["DefaultCyberProgram"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`""`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 DefaultCyberProgram 存在时不得为空串", name)
			}
		}
		if name == SectionBaseInstructions {
			if value, declared := fields["IDScheme"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`""`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 IDScheme 存在时不得为空串", name)
			}
		}
		// omitempty 的布尔字段同理不得显式写成 false：false 与缺省无法区分，规范化 JSON 却会省略它，
		// 同一份语义于是对应两种官方摘要。声明该能力只能写 true，不声明就省略。
		if name == SectionWebSocketContinuation {
			if value, declared := fields["ReplayOnSameConnection"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`false`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 ReplayOnSameConnection 存在时必须为 true", name)
			}
		}
		if name == SectionTurnMetadata {
			if value, declared := fields["WindowTracking"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`false`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 WindowTracking 存在时必须为 true", name)
			}
			if value, declared := fields["WSCompactionRequests"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`false`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 WSCompactionRequests 存在时必须为 true", name)
			}
			if value, declared := fields["HTTPCompactionRequests"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`false`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 HTTPCompactionRequests 存在时必须为 true", name)
			}
			if value, declared := fields["RootThreadKeys"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`false`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 RootThreadKeys 存在时必须为 true", name)
			}
		}
		if name == SectionTurnState {
			if value, declared := fields["ResetOnNewTurn"]; declared && bytes.Equal(bytes.TrimSpace(value), []byte(`false`)) {
				return nil, fmt.Errorf("可选节 %s 的字段 ResetOnNewTurn 存在时必须为 true", name)
			}
		}
	}
	dec := json.NewDecoder(bytes.NewReader(trimmed))
	dec.DisallowUnknownFields()
	if err := dec.Decode(target); err != nil {
		return nil, fmt.Errorf("解析可选节 %s: %w", name, err)
	}
	if dec.More() {
		return nil, fmt.Errorf("可选节 %s 后存在多余数据", name)
	}
	if err := validateOptionalSection(name, target); err != nil {
		return nil, fmt.Errorf("校验可选节 %s: %w", name, err)
	}
	return target, nil
}

func validateOptionalSection(name string, value any) error {
	switch section := value.(type) {
	case *CookieJarSection:
		if err := requireSortedUnique("AllowedNames", section.AllowedNames, cookieNamePattern); err != nil {
			return err
		}
		return requireSortedUnique("AllowedPrefixes", section.AllowedPrefixes, cookieNamePattern)
	case *WorkspaceRoutingSection:
		if section.DiscoveryEndpointID == "" {
			return errors.New("DiscoveryEndpointID 不能为空")
		}
		if !strings.HasPrefix(section.DefaultOrigin, "https://") || strings.Count(section.DefaultOrigin, "/") != 2 {
			return errors.New("DefaultOrigin 必须是不带路径的 https origin")
		}
		if err := requireSortedUnique("AcceptedOriginValues", section.AcceptedOriginValues, nil); err != nil {
			return err
		}
		if err := requireSortedUnique("AcceptedOverrideValues", section.AcceptedOverrideValues, nil); err != nil {
			return err
		}
		if !headerNamePattern.MatchString(section.OverrideHeader) {
			return errors.New("OverrideHeader 必须是小写请求头名")
		}
		if section.NonDefaultAction != "fail_closed" {
			return fmt.Errorf("NonDefaultAction 只支持 fail_closed，实际 %q", section.NonDefaultAction)
		}
		return requireSortedUnique("RoutedEndpointIDs", section.RoutedEndpointIDs, sectionTokenPattern)
	case *TurnMetadataSection:
		if err := requireSortedUnique("Keys", section.Keys, sectionTokenPattern); err != nil {
			return err
		}
		if err := requireSortedUnique("CompactionImplementations", section.CompactionImplementations, sectionTokenPattern); err != nil {
			return err
		}
		if err := requireSortedUnique("CompactionPhases", section.CompactionPhases, sectionTokenPattern); err != nil {
			return err
		}
		if section.WindowTracking {
			for _, key := range []string{"window_id", "window_number", "context_window_id"} {
				if !slices.Contains(section.Keys, key) {
					return fmt.Errorf("WindowTracking 存在时 Keys 必须包含 %s", key)
				}
			}
		}
		if section.RootThreadKeys {
			for _, key := range []string{"agent_name", "root_turn_id"} {
				if !slices.Contains(section.Keys, key) {
					return fmt.Errorf("RootThreadKeys 存在时 Keys 必须包含 %s", key)
				}
			}
		}
		if section.WSCompactionRequests && !slices.Contains(section.CompactionImplementations, "responses_compaction_v2") {
			return errors.New("WSCompactionRequests 存在时 CompactionImplementations 必须包含 responses_compaction_v2")
		}
		if section.HTTPCompactionRequests && !slices.Contains(section.CompactionImplementations, "responses_compaction_v2") {
			return errors.New("HTTPCompactionRequests 存在时 CompactionImplementations 必须包含 responses_compaction_v2")
		}
		return validateTurnMetadataSessionDefaults(section)
	case *ClientMetadataSection:
		if len(section.Constants) == 0 {
			return errors.New("Constants 不能为空")
		}
		for key, entry := range section.Constants {
			if !sectionTokenPattern.MatchString(key) || entry == "" {
				return fmt.Errorf("Constants 键或值非法: %q", key)
			}
		}
		// 条件必须显式给出：空串在条件闭集里表示“无条件”，这里不允许省略，恒定写入写 always。
		if section.Condition == "" || !EngineSupportedEnumValues().Contains(EnumDomainConditionKind, string(section.Condition)) {
			return fmt.Errorf("Condition 未受支持: %q", section.Condition)
		}
		if section.KeyConditions != nil {
			if len(section.KeyConditions) == 0 {
				return errors.New("KeyConditions 存在时不能为空")
			}
			for key, condition := range section.KeyConditions {
				if _, declared := section.Constants[key]; !declared {
					return fmt.Errorf("KeyConditions 引用了未声明的常量键: %q", key)
				}
				if condition == "" || !EngineSupportedEnumValues().Contains(EnumDomainConditionKind, string(condition)) {
					return fmt.Errorf("KeyConditions 的条件未受支持: %q=%q", key, condition)
				}
			}
		}
		if section.DynamicKeys != nil {
			if err := requireSortedUniqueKnown("DynamicKeys", section.DynamicKeys, clientMetadataDynamicKeyKinds); err != nil {
				return err
			}
			for _, key := range section.DynamicKeys {
				if _, collides := section.Constants[key]; collides {
					return fmt.Errorf("DynamicKeys 与 Constants 重名: %s", key)
				}
			}
		}
		return nil
	case *TurnStateSection:
		if !section.ResetOnAccountOwnerChange {
			return errors.New("TurnState 节存在时 ResetOnAccountOwnerChange 必须为 true")
		}
		return nil
	case *WebSocketRetrySection:
		if err := requireSortedUnique("RetryableErrorCodes", section.RetryableErrorCodes, sectionTokenPattern); err != nil {
			return err
		}
		if section.RetryBudget <= 0 {
			return errors.New("RetryBudget 必须为正")
		}
		if section.FallbackTransport != "http" {
			return fmt.Errorf("FallbackTransport 只支持 http，实际 %q", section.FallbackTransport)
		}
		return nil
	case *WebSocketContinuationSection:
		if err := requireSortedUnique("ResetOn", section.ResetOn, sectionTokenPattern); err != nil {
			return err
		}
		for _, kind := range section.ResetOn {
			if !continuationResetKinds[kind] {
				return fmt.Errorf("ResetOn 含未知条件: %s", kind)
			}
		}
		return nil
	case *ReasoningEffortSection:
		if section.CustomNumericSerialization != "u64_integer" {
			return fmt.Errorf("CustomNumericSerialization 只支持 u64_integer，实际 %q", section.CustomNumericSerialization)
		}
		if section.ConfigurationUpdate != "" && section.ConfigurationUpdate != ReasoningEffortConfigurationUpdateHTTPFirstTurn {
			return fmt.Errorf("ConfigurationUpdate 只支持 %s（缺省表示不合成），实际 %q",
				ReasoningEffortConfigurationUpdateHTTPFirstTurn, section.ConfigurationUpdate)
		}
		return nil
	case *ImageGenerationSection:
		if section.DefaultBackground != "auto" && section.DefaultBackground != "opaque" {
			return fmt.Errorf("DefaultBackground 只支持 auto 或 opaque，实际 %q", section.DefaultBackground)
		}
		if section.TransparentBackground != "transparent" {
			return fmt.Errorf("TransparentBackground 只支持 transparent，实际 %q", section.TransparentBackground)
		}
		if err := requireSortedUnique("EditImageReferences", section.EditImageReferences, sectionTokenPattern); err != nil {
			return err
		}
		for _, kind := range section.EditImageReferences {
			if !imageReferenceKinds[kind] {
				return fmt.Errorf("EditImageReferences 含未知形态: %s", kind)
			}
		}
		if !slices.Contains(section.EditImageReferences, "image_url") {
			return errors.New("EditImageReferences 必须包含 image_url")
		}
		return nil
	case *BaseInstructionsSection:
		if section.Carrier != "input_developer_message" {
			return fmt.Errorf("Carrier 只支持 input_developer_message，实际 %q", section.Carrier)
		}
		if !contentItemKindPattern.MatchString(section.ContentItemKind) {
			return fmt.Errorf("ContentItemKind 必须是点分小写标识，实际 %q", section.ContentItemKind)
		}
		if err := requireSortedUnique("EndpointIDs", section.EndpointIDs, sectionTokenPattern); err != nil {
			return err
		}
		for _, endpointID := range section.EndpointIDs {
			if !slices.Contains(responsesEndpointIDs, endpointID) {
				return fmt.Errorf("EndpointIDs 含不支持的端点: %s", endpointID)
			}
		}
		if !messageIDPrefixPattern.MatchString(section.IDPrefix) {
			return fmt.Errorf("IDPrefix 必须是 2 到 8 位小写字母，实际 %q", section.IDPrefix)
		}
		if section.IDScheme != "" && section.IDScheme != BaseInstructionsIDSchemeUUIDv7 {
			return fmt.Errorf("IDScheme 只支持 %s（缺省表示旧方案），实际 %q", BaseInstructionsIDSchemeUUIDv7, section.IDScheme)
		}
		return nil
	case *AccessProgramsSection:
		if err := requireSortedUnique("AllowedCyberPrograms", section.AllowedCyberPrograms, sectionTokenPattern); err != nil {
			return err
		}
		for _, program := range section.AllowedCyberPrograms {
			if !cyberProgramKinds[program] {
				return fmt.Errorf("AllowedCyberPrograms 含未知程序: %s", program)
			}
		}
		if section.DefaultCyberProgram != "" && !slices.Contains(section.AllowedCyberPrograms, section.DefaultCyberProgram) {
			return fmt.Errorf("DefaultCyberProgram 必须属于 AllowedCyberPrograms，实际 %q", section.DefaultCyberProgram)
		}
		return nil
	default:
		return fmt.Errorf("未知可选节类型: %s", name)
	}
}

// clientMetadataDynamicKeyKinds 是 ClientMetadataSection.DynamicKeys 允许的取值全集。
var clientMetadataDynamicKeyKinds = map[string]bool{
	"root_turn_id": true,
	"ws_request_header_x_openai_internal_codex_responses_lite": true,
	"x-codex-ws-stream-request-start-ms":                       true,
}

// requireSortedUniqueKnown 要求列表非空、严格升序，且每个元素都在 known 里。
func requireSortedUniqueKnown(field string, values []string, known map[string]bool) error {
	if len(values) == 0 {
		return fmt.Errorf("%s 不能为空", field)
	}
	if !sort.StringsAreSorted(values) {
		return fmt.Errorf("%s 必须排序", field)
	}
	for index, value := range values {
		if !known[value] {
			return fmt.Errorf("%s 含未知取值: %q", field, value)
		}
		if index > 0 && values[index-1] == value {
			return fmt.Errorf("%s 含重复取值: %q", field, value)
		}
	}
	return nil
}

// requireSortedUnique 要求列表非空、严格升序（即排序且无重复），元素满足可选的格式约束。
func requireSortedUnique(field string, values []string, pattern *regexp.Regexp) error {
	if len(values) == 0 {
		return fmt.Errorf("%s 不能为空", field)
	}
	if !sort.StringsAreSorted(values) {
		return fmt.Errorf("%s 必须排序", field)
	}
	for index, value := range values {
		if value == "" || (pattern != nil && !pattern.MatchString(value)) {
			return fmt.Errorf("%s 含非法取值: %q", field, value)
		}
		if index > 0 && values[index-1] == value {
			return fmt.Errorf("%s 含重复取值: %q", field, value)
		}
	}
	return nil
}
