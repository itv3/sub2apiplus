package main

// sink 身份分类规则。
//
// 方案 §9 第 5 点：「候选项必须经人工补齐 persona、route、backend、状态和迁移责任后，
// 才能进入 SinkCatalog 或 legacy 基线；扫描命中本身不等于身份分类已经正确。」
//
// ## 为什么必须按函数精确匹配，不能按文件通配
//
// 前一版规则用文件路径做主判据，实测产生 8 条跨平台错误绑定：
// account_test_service.go 一个文件里同时有 Claude、Vertex、Bedrock、Grok、Gemini、
// Antigravity 和 OpenAI 七个平台的测试函数，文件级通配把它们统统登记成了
// Codex `/responses`。keeper 文件同样混放 Anthropic 与 OpenAI。
//
// 这类错误比不分类更危险：SinkCatalog 看起来完整，实际给 Guard 喂了错误的 RouteKey。
// 真按它 enforce，Claude 账号测试会因为「route 不匹配 codex-cli」被拒发。
//
// 因此 in-scope 的每一条都必须用 funcExact 精确到函数；filePrefix 只允许用于整包
// out-of-scope 的批量排除。
//
// ## ScanCandidateID 与 RuntimeSinkID 的区别
//
// 扫描器发现的是**调用表达式**（ScanCandidateID）：一次用量探测会同时命中
// httpclient.GetClient（factory）与 client.Do（terminal）两个表达式。
// Guard 需要的是**业务身份**（RuntimeSinkID）：这两个表达式属于同一个业务出口。
// 规则负责把多个 candidate 映射到同一个 RuntimeSinkID。

import (
	"fmt"
	"sort"
	"strings"
)

type classifyRule struct {
	// candidateExact 只用于“同一函数机械迁址后语义发生变化”的受审 source transition。
	// 它以完整 ScanCandidateID 区分历史位置与新位置，优先级高于 funcExact。
	candidateExact string
	// funcExact 是常规 in-scope 规则的主判据。
	funcExact  string // 精确匹配 SinkRecord.Func
	filePrefix string // 文件路径子串；仅用于 out-of-scope 批量排除
	sinkKind   string // 可选：区分同一函数内的不同 sink 种类

	runtimeSinkID string
	purpose       string
	persona       string
	// endpointEvidence 区分“具备端点级画像”与“只有传输/身份证据”。
	// 留空时由 persona 与 facade 状态取安全默认值；特殊项必须显式覆盖。
	endpointEvidence string
	routes           []string // 精确 route 集合；facade 留空并置 isFacade
	isFacade         bool
	backend          string // 当前实际后端
	targetBackend    string // 迁移后应当使用的后端；留空表示与 backend 相同
	state            string
	owner            string
	changeset        string
	expiry           string
	rationale        string
}

// ownerEgress 是这些 sink 的迁移责任人。
//
// 方案 §9 要求机器基线记录责任人：全部 legacy_observe/pending_removal 都带到期条件，
// 责任人回答的是「到期未迁移时找谁」。没有责任人的豁免实际上是永久豁免。
const ownerEgress = "czs"

// ownerPlaceholders 是被拒绝的占位符值。
//
// 单列出来是因为占位符比空值更危险：空值一眼能看出没填，而 "TBD" 或
// "egress-owner" 会让 validateClassification 通过，基线看起来合规，
// 实际上那个「责任人」并不存在。
var ownerPlaceholders = map[string]bool{
	"": true, "-": true, "TBD": true, "tbd": true,
	"egress-owner": true, "owner": true, "unassigned": true, "todo": true,
}

// oos 构造 out-of-scope 规则，避免几十行重复字面量淹没真正需要审阅的 in-scope 规则。
func oos(filePrefix, rationale string) classifyRule {
	return classifyRule{
		filePrefix: filePrefix, persona: "out-of-scope",
		backend: "-", state: "not_applicable",
		owner: "-", changeset: "-", rationale: rationale,
	}
}

func oosFunc(funcExact, rationale string) classifyRule {
	return classifyRule{
		funcExact: funcExact, persona: "out-of-scope",
		backend: "-", state: "not_applicable",
		owner: "-", changeset: "-", rationale: rationale,
	}
}

// reviewedPostBootstrapInfrastructure 是 bootstrap 后经方案审核允许新增的发送栈
// delegation 精确闭集。runCheck 会同时校验候选 ID 与 infrastructure 分类；新增裸
// sink 不能只靠改 persona 绕过门禁。
var reviewedPostBootstrapInfrastructure = map[string]string{
	"github.com/Wei-Shaw/sub2api/internal/officialegress.*guardedRoundTripper.RoundTrip@backend/internal/officialegress/guard.go#roundtripper_roundtrip#1":                                             "变更集 1A Guard 的 terminal delegation；业务 SinkID 必须来自调用上下文",
	"github.com/Wei-Shaw/sub2api/internal/service.*officialCodexHTTPUpstreamPort.SendHTTPUpstream@backend/internal/service/official_egress_1b_executor.go#facade_http_upstream_do_tls#1":               "变更集 1B Executor 的 HTTPUpstream terminal delegation；业务 SinkID 与定型凭证必须来自 Executor Plan",
	"github.com/Wei-Shaw/sub2api/internal/repository.openAIOAuthReqProfileTransport.Do@backend/internal/repository/official_egress_guard.go#net_http_client_do#1":                                      "变更集 5 将 OAuth req-profile 物理资源机械迁入独立官方出站 adapter 文件；只发送 Executor 编译结果",
	"github.com/Wei-Shaw/sub2api/internal/service.officialCodexWebSocketAcquireRouter.AcquireOfficialCodexWebSocket@backend/internal/service/official_egress_transport_adapters.go#facade_ws_dialer#1": "变更集 3 WebSocket adapter 的中心 Acquire delegation；业务 SinkID、FinalizationToken 与连接池身份必须来自 Executor",
	"github.com/Wei-Shaw/sub2api/internal/service.*officialEgressWebSocketRoundTripper.RoundTrip@backend/internal/service/official_egress_transport_adapters.go#roundtripper_roundtrip#1":              "变更集 5 将 WebSocket RoundTripper 机械迁入独立 adapter；只展开已由 Executor 签发的物理握手，不承载业务 SinkID",
	"github.com/Wei-Shaw/sub2api/internal/service.doOpenAIAPIKeyHTTPTransport@backend/internal/service/openai_upstream_http.go#facade_http_upstream_do#1":                                              "变更集 2 将非 Codex persona 的 API-key/custom provider 发送与官方画像链物理分离",
	"github.com/Wei-Shaw/sub2api/internal/service.doOpenAIAPIKeyHTTPTransport@backend/internal/service/openai_upstream_http.go#facade_http_upstream_do_tls#1":                                          "变更集 2 将非 Codex persona 的 API-key/custom provider TLS 发送与官方画像链物理分离",
	"github.com/Wei-Shaw/sub2api/internal/service.dialOpenAIWSV2UnwiredTest@backend/internal/service/openai_ws_v2_passthrough_adapter.go#facade_ws_dialer#1":                                           "变更集 2 保留 API Key／自定义上游 WebSocket 产品路径；OAuth Codex persona 不可达",
	"github.com/Wei-Shaw/sub2api/internal/service.*ContentModerationService.moderationHTTPClient@backend/internal/service/content_moderation.go#factory_httpclient_pool#1":                             "本次上游同步为范围外内容审核增加可配置代理客户端工厂，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*EmailService.connectSMTP@backend/internal/service/email_service.go#raw_tls_dial_dialer#1":                                                           "本次上游同步将范围外 SMTP TLS 拨号收口到共享连接函数，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*EmailService.connectSMTPStartTLS@backend/internal/service/email_service.go#raw_dialer_dial#1":                                                       "本次上游同步将范围外 SMTP STARTTLS 拨号收口到共享连接函数，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.emitGrokVideoResult@backend/internal/service/account_test_service.go#facade_http_upstream_do#1":                                  "本次上游同步新增的 Grok 视频结果轮询，属于 xAI 管理端测试，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokImageGeneration@backend/internal/service/account_test_service.go#facade_http_upstream_do#1":                              "本次上游同步新增的 Grok 图片管理端测试，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokRealtime@backend/internal/service/account_test_service.go#facade_ws_dialer#1":                                            "本次上游同步新增的 Grok 实时语音管理端测试，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokResponsesConnection@backend/internal/service/account_test_service.go#facade_http_upstream_do#1":                          "本次上游同步新增的 Grok Responses 管理端测试，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokSTT@backend/internal/service/account_test_service.go#facade_http_upstream_do#1":                                          "本次上游同步新增的 Grok 语音识别管理端测试，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokTTS@backend/internal/service/account_test_service.go#facade_http_upstream_do#1":                                          "本次上游同步新增的 Grok 语音合成管理端测试，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokVideoGeneration@backend/internal/service/account_test_service.go#facade_http_upstream_do#1":                              "本次上游同步新增的 Grok 视频创建管理端测试，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokVideoGeneration@backend/internal/service/account_test_service.go#facade_http_upstream_do#2":                              "本次上游同步新增的 Grok 视频状态查询，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*AccountTestService.testGrokWebSearch@backend/internal/service/account_test_service.go#facade_http_upstream_do#1":                                    "本次上游同步新增的 Grok 搜索管理端测试，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/repository.createGrokPasswordSession@backend/internal/repository/grok_oauth_client.go#net_http_client_do#1":                                                  "本次上游同步新增的 Grok 密码登录会话请求，属于 xAI OAuth，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/repository.extractGrokSSOToken@backend/internal/repository/grok_oauth_client.go#net_http_client_do#1":                                                        "本次上游同步新增的 Grok SSO token 提取请求，属于 xAI OAuth，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/repository.solveTurnstile@backend/internal/repository/grok_oauth_client.go#net_http_client_do#1":                                                             "本次上游同步新增的 Grok Turnstile 挑战请求，属于 xAI OAuth，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/repository.solveTurnstile@backend/internal/repository/grok_oauth_client.go#net_http_client_do#2":                                                             "本次上游同步新增的 Grok Turnstile 校验请求，属于 xAI OAuth，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*GatewayService.DoGrokNativeResponsesJSON@backend/internal/service/gateway_service.go#facade_http_upstream_do#1":                                     "本次上游同步新增的 Grok 原生 Responses 发送点，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*GrokQuotaService.syncGrokObservedModels@backend/internal/service/grok_observed_models.go#facade_http_upstream_do#1":                                 "本次上游同步新增的 Grok 模型观测同步，属于 xAI 平台，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*OpenAIGatewayService.ForwardGrokVoice@backend/internal/service/grok_audio.go#facade_http_upstream_do#1":                                             "本次上游同步新增的 Grok 语音发送点，属于 xAI 桥接，不承载 Codex persona",
	"github.com/Wei-Shaw/sub2api/internal/service.*OpenAIGatewayService.ProxyGrokRealtime@backend/internal/service/grok_audio.go#facade_ws_dialer#1":                                                   "本次上游同步新增的 Grok 实时语音拨号点，属于 xAI 桥接，不承载 Codex persona",
}

var classifyRules = []classifyRule{
	{
		funcExact: "doOpenAIAPIKeyHTTPTransport", persona: "out-of-scope",
		backend: "-", state: "not_applicable", owner: "-", changeset: "-",
		rationale: "仅承载 API-key/custom provider，不消费 Codex ReleaseBundle",
	},
	{
		funcExact: "dialOpenAIWSV2UnwiredTest", persona: "out-of-scope",
		backend: "-", state: "not_applicable", owner: "-", changeset: "-",
		rationale: "承载 API Key／自定义上游 WebSocket，不属于 OAuth Codex persona",
	},
	{
		funcExact: "openAIOAuthReqProfileTransport.Do", persona: "infrastructure",
		backend: "req_profile", state: "not_applicable", owner: "-", changeset: "2",
		rationale: "OAuth compiler 结果到 req-profile resource 的中心 delegation",
	},
	{
		funcExact: "officialCodexWebSocketAcquireRouter.AcquireOfficialCodexWebSocket",
		sinkKind:  "facade_ws_dialer", persona: "infrastructure",
		backend: "websocket", state: "not_applicable", owner: "-", changeset: "3",
		rationale: "统一 WebSocket adapter 在 Acquire 边界验证当前 FinalizationToken 后委托物理拨号",
	},
	// =================================================================
	// codex-cli：P0 旁路（无 uTLS 直打官方 host）
	// =================================================================
	{
		funcExact:     "*AccountUsageService.probeOpenAICodexSnapshot",
		runtimeSinkID: "codex.usage.probe", purpose: "usage_probe",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "plain_net_http", targetBackend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B",
		expiry:    "1B 改 WHAM-first 后本 sink 消失",
		rationale: "P0：httpclient.GetClient 无 uTLS 直打官方推理端点",
	},
	{
		funcExact:     "registerAgentIdentityTask",
		runtimeSinkID: "unclassified.agent.task_register", purpose: "agent_task_register",
		persona: "unclassified", routes: []string{"POST auth.openai.com/api/accounts/v1/agent/{runtime}/task/register"},
		backend: "plain_net_http", targetBackend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B",
		expiry:    "1B 完成官方行为举证并接入 Executor",
		rationale: "P0：无 uTLS 且无 Codex 身份头；persona 归属待举证",
	},
	{
		funcExact:     "*OpenAIOAuthService.ValidateCodexPersonalAccessToken",
		runtimeSinkID: "unclassified.pat.whoami", purpose: "pat_whoami",
		persona: "unclassified", routes: []string{"GET auth.openai.com/api/accounts/v1/user-auth-credential/whoami"},
		backend: "plain_net_http", targetBackend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B",
		expiry:    "1B 完成官方行为举证并接入 Executor",
		rationale: "P0：Codex 身份头 + Go 标准库 TLS，身份自相矛盾",
	},

	// =================================================================
	// codex-cli：OAuth（auth.openai.com）
	// =================================================================
	{
		funcExact:     "*openaiOAuthService.ExchangeCode",
		runtimeSinkID: "codex.oauth.exchange", purpose: "oauth_code_exchange",
		persona: "codex-cli", routes: []string{"POST auth.openai.com/oauth/token"},
		endpointEvidence: "transport_only",
		backend:          "req_profile", state: "legacy_observe",
		owner: ownerEgress, changeset: "2",
		expiry:    "变更集 2 补齐授权码换 token 的端点证据后消费 ReleaseBundle",
		rationale: "已有 Codex TLS/User-Agent/Originator 证据，但 0.145 画像只有 refresh body，不能按同 route 冒充同端点契约",
	},
	{
		funcExact:     "createOpenAIReqClientWithProfile",
		runtimeSinkID: "codex.facade.oauth_client", purpose: "facade",
		persona: "codex-cli", isFacade: true,
		backend: "req_profile", state: "legacy_observe",
		owner: ownerEgress, changeset: "3",
		expiry:    "变更集 3 由 Executor backend wiring 吸收",
		rationale: "refresh 与 exchange 共用的客户端工厂；不是任一业务请求的 RuntimeSinkID",
	},

	// =================================================================
	// codex-cli：主链与业务入口
	// =================================================================
	{
		funcExact:     "*OpenAIGatewayService.Forward",
		runtimeSinkID: "codex.responses.forward", purpose: "user_request.responses",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3",
		expiry:    "变更集 3 迁入 Executor 并收敛 Forward",
		rationale: "主链；forward.go:242 是向 passthrough 的分派，不是独立发送点",
	},
	{
		funcExact:     "*OpenAIGatewayService.forwardOpenAIPassthrough",
		runtimeSinkID: "codex.responses.passthrough", purpose: "user_request.passthrough",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "透传主链",
	},
	{
		funcExact:     "*OpenAIGatewayService.ForwardAsChatCompletions",
		runtimeSinkID: "codex.responses.chat_completions", purpose: "user_request.chat_completions",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "chat completions 形态入站 → Codex 出站",
	},
	{
		funcExact:     "*OpenAIGatewayService.ForwardAsAnthropic",
		runtimeSinkID: "codex.responses.anthropic_compat", purpose: "user_request.anthropic_compat",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "Anthropic 形态入站、Codex 形态出站；入站形态不改变出站 persona",
	},
	{
		funcExact:     "doOpenAIHTTPUpstreamWithProfile",
		runtimeSinkID: "codex.facade.upstream", purpose: "facade",
		persona: "codex-cli", isFacade: true,
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3",
		expiry:    "变更集 3 后由 Executor 取代",
		rationale: "共享 facade；11 个业务调用点经此发送，自身不承载业务身份",
	},
	{
		funcExact:     "*OpenAIGatewayService.doOpenAIHTTPUpstreamForRequest",
		runtimeSinkID: "codex.facade.upstream", purpose: "facade",
		persona: "codex-cli", isFacade: true,
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 后由 Executor 取代",
		rationale: "二级 facade",
	},

	// =================================================================
	// codex-cli：WebSocket 与 realtime
	//
	// 三条 route 各不相同，前一版按文件通配曾把它们错误合并：
	//   responses WS   = GET  chatgpt.com/backend-api/codex/responses      (WS)
	//   realtime calls = POST chatgpt.com/backend-api/codex/realtime/calls (HTTP)
	//   sideband       = GET  api.openai.com/v1/realtime                   (WS)
	// =================================================================
	{
		funcExact:     "*coderOpenAIWSClientDialer.Dial",
		runtimeSinkID: "codex.responses.ws", purpose: "user_request.responses_ws",
		persona: "codex-cli", routes: []string{"GET chatgpt.com/backend-api/codex/responses (WebSocket)"},
		backend: "websocket", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor.Dial",
		rationale: "Responses WS 拨号；画像 method 为 GET + Upgrade，不是 POST",
	},
	{
		funcExact:     "*coderOpenAIWSClientDialer.DialLiveSideband",
		runtimeSinkID: "codex.realtime.sideband", purpose: "user_request.realtime_sideband",
		persona: "codex-cli", routes: []string{"GET api.openai.com/v1/realtime (WebSocket)"},
		backend: "websocket", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor.Dial",
		rationale: "realtime sideband：唯一在 api.openai.com 上的 codex-cli route",
	},
	{
		funcExact:     "*OpenAIGatewayService.createUpstreamLiveCall",
		runtimeSinkID: "codex.realtime.calls", purpose: "user_request.realtime_calls",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/realtime/calls"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "画像 TransportID=HTTPDefault，是 HTTP 端点而非 WebSocket",
	},
	{
		funcExact:     "*OpenAIGatewayService.proxyOpenAIWSHTTPBridgeTurn",
		runtimeSinkID: "codex.responses.ws_http_bridge", purpose: "user_request.ws_http_fallback",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "入站 WS、出站 HTTP 的桥；backend 是 http_upstream 而非 websocket",
	},
	{
		funcExact:     "*officialEgressWebSocketRoundTripper.RoundTrip",
		runtimeSinkID: "codex.responses.ws", purpose: "user_request.responses_ws",
		persona: "codex-cli", routes: []string{"GET chatgpt.com/backend-api/codex/responses (WebSocket)"},
		backend: "websocket", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor.Dial",
		rationale: "WS 握手的 RoundTripper 层，与 Dial 同属一个 RuntimeSinkID",
	},

	{
		funcExact:     "*OpenAIGatewayService.dialLiveSideband",
		runtimeSinkID: "codex.realtime.sideband", purpose: "user_request.realtime_sideband",
		persona: "codex-cli", routes: []string{"GET api.openai.com/v1/realtime (WebSocket)"},
		backend: "websocket", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor.Dial",
		rationale: "经 openAIWSClientDialer 接口拨号；与 DialLiveSideband 同一 RuntimeSinkID",
	},
	{
		funcExact:     "*OpenAIGatewayService.proxyResponsesWebSocketV2Passthrough",
		runtimeSinkID: "codex.responses.ws_v2_passthrough", purpose: "user_request.responses_ws_v2",
		persona: "codex-cli", routes: []string{"GET chatgpt.com/backend-api/codex/responses (WebSocket)"},
		backend: "websocket", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor.Dial",
		rationale: "WSv2 透传；经接口拨号，前一版扫描器完全看不见",
	},
	{
		funcExact:     "*openAIWSConnPool.dialConn",
		runtimeSinkID: "codex.facade.ws_pool", purpose: "facade",
		persona: "codex-cli", isFacade: true,
		backend: "websocket", state: "legacy_observe",
		owner: ownerEgress, changeset: "3",
		expiry:    "变更集 3 后由 Executor 统一管理连接生命周期",
		rationale: "WS 连接池拨号 facade；自身不承载业务身份",
	},

	// =================================================================
	// codex-cli：辅助端点
	// =================================================================
	{
		funcExact:     "*OpenAIQuotaService.doCodexQuotaRequest",
		runtimeSinkID: "codex.quota.wham", purpose: "quota_query",
		persona: "codex-cli", routes: []string{
			"GET chatgpt.com/backend-api/wham/usage",
			"GET chatgpt.com/backend-api/wham/rate-limit-reset-credits",
			"POST chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
		},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "WHAM 用量与重置额度；端点已在画像登记",
	},
	{
		funcExact:     "*OpenAIGatewayService.fetchCodexModelsManifestUpstream",
		runtimeSinkID: "codex.models.list", purpose: "models_manifest",
		persona: "codex-cli", routes: []string{"GET chatgpt.com/backend-api/codex/models"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B",
		expiry:    "1B 移除其中的 httpclient.GetClient 分支",
		rationale: "三分支实现，其中一支用无 uTLS 客户端",
	},
	{
		funcExact:     "*OpenAIGatewayService.ForwardAlphaSearch",
		runtimeSinkID: "codex.alpha_search.direct", purpose: "user_request.alpha_search",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/alpha/search"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "主路径已 fail-close：缺 egress context 直接报错",
	},
	{
		funcExact:     "*OpenAIGatewayService.forwardAlphaSearchViaResponsesWebSearch",
		runtimeSinkID: "codex.alpha_search.pat_fallback", purpose: "user_request.alpha_search_pat",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B", expiry: "1B 补 finalize",
		rationale: "半旁路：TLS 有画像，Header/Body 手工构造且无 finalize",
	},
	{
		funcExact:     "*OpenAIGatewayService.forwardOpenAIImagesOAuth",
		runtimeSinkID: "codex.images.responses", purpose: "user_request.images",
		persona: "codex-cli", routes: []string{
			"POST chatgpt.com/backend-api/codex/images/generations",
			"POST chatgpt.com/backend-api/codex/images/edits",
		},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "图片生成/编辑 OAuth 路径",
	},
	{
		funcExact:     "*officialCodexFileUploadCall.executeJSON",
		runtimeSinkID: "codex.files.register", purpose: "user_request.file_register",
		persona: "codex-cli", routes: []string{
			"POST chatgpt.com/backend-api/files",
			"POST chatgpt.com/backend-api/files/{file_id}/uploaded",
		},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3", expiry: "变更集 3 迁入 Executor",
		rationale: "文件上传登记与确认（JSON 段）",
	},
	{
		funcExact:     "*officialCodexFileUploadCall.uploadBlob",
		runtimeSinkID: "codex.files.blob_upload", purpose: "user_request.file_blob",
		persona: "codex-cli", routes: []string{"PUT *.oaiusercontent.com/{server_returned_path}"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "3",
		expiry:    "变更集 3 迁入 Executor 并补动态 host 的运行时 Guard",
		rationale: "HostFromResponse=true：host 与 path 均由上游响应给出，静态不可解析",
	},

	// =================================================================
	// codex-cli：管理端探针
	//
	// 必须逐函数登记：同文件内还有 6 个其他平台的测试函数。
	// =================================================================
	{
		funcExact:     "*AccountTestService.testOpenAIAccountConnection",
		runtimeSinkID: "codex.admin_test.responses", purpose: "admin_test.responses",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B", expiry: "1B 迁入 Executor",
		rationale: "管理端账号测试；Header 手工构造，缺 finalize",
	},
	oosFunc("*AccountTestService.testOpenAIChatCompletionsConnection",
		"仅处理通用 API-Key /v1/chat/completions 与自定义 base URL，不属于 OAuth Codex persona"),
	{
		funcExact:     "*AccountTestService.testOpenAICompactConnection",
		runtimeSinkID: "codex.admin_test.compact", purpose: "admin_test.compact",
		persona: "codex-cli", routes: []string{"POST chatgpt.com/backend-api/codex/responses/compact"},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B", expiry: "1B 迁入 Executor",
		rationale: "compact 端点探测",
	},
	// 调用者对 mimic 账号在进入此函数后立即返回；真正可达的发送分支只处理
	// 非 mimic API Key，并请求账号自定义 baseURL 的 /v1/responses。它不属于
	// Codex OAuth 官方端点闭集，不能登记成 chatgpt.com 的 Codex route。
	oosFunc("*AccountTestService.ProbeOpenAIAPIKeyResponsesSupport",
		"非 mimic API Key 能力探测；mimic 分支提前返回，目标为自定义 baseURL/v1/responses"),
	oosFunc("*AccountTestService.ProxyKeeperOpenAIAccount",
		"仅接受 API-Key 账号并转发账号 base URL，不属于 OAuth Codex persona"),
	{
		funcExact:     "*AccountTestService.testOpenAIImageOAuth",
		runtimeSinkID: "codex.images.oauth_test", purpose: "admin_test.images_oauth",
		persona: "codex-cli", routes: []string{
			"POST chatgpt.com/backend-api/codex/images/generations",
			"POST chatgpt.com/backend-api/codex/images/edits",
		},
		backend: "http_upstream", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B", expiry: "1B 迁入 Executor",
		rationale: "OAuth 图片测试打官方端点",
	},

	// =================================================================
	// 非 OpenAI 平台：与 codex 共文件，必须逐函数排除
	// =================================================================
	oosFunc("*AccountTestService.testClaudeAccountConnection", "Anthropic 账号测试；Anthropic persona 化另行规划"),
	oosFunc("*AccountTestService.testClaudeVertexServiceAccountConnection", "Vertex 承载的 Claude"),
	oosFunc("*AccountTestService.testBedrockAccountConnection", "AWS Bedrock"),
	oosFunc("*AccountTestService.testGrokAccountConnection", "Grok 平台"),
	oosFunc("*AccountTestService.testGrokResponsesConnection", "Grok Responses 管理端测试，属于 xAI 平台"),
	oosFunc("*AccountTestService.testGrokImageGeneration", "Grok 图片管理端测试，属于 xAI 平台"),
	oosFunc("*AccountTestService.testGrokVideoGeneration", "Grok 视频创建与状态查询，属于 xAI 平台"),
	oosFunc("*AccountTestService.emitGrokVideoResult", "Grok 视频结果轮询，属于 xAI 平台"),
	oosFunc("*AccountTestService.testGrokWebSearch", "Grok 搜索管理端测试，属于 xAI 平台"),
	oosFunc("*AccountTestService.testGrokTTS", "Grok 语音合成管理端测试，属于 xAI 平台"),
	oosFunc("*AccountTestService.testGrokSTT", "Grok 语音识别管理端测试，属于 xAI 平台"),
	oosFunc("*AccountTestService.testGrokRealtime", "Grok 实时语音管理端测试，属于 xAI 平台"),
	oosFunc("*AccountTestService.testGeminiAccountConnection", "Gemini 平台"),
	oosFunc("*AccountTestService.testAntigravityAccountConnection", "Antigravity 平台"),
	oosFunc("*AccountTestService.ProxyKeeperAnthropicAccount", "Anthropic keeper 代发"),
	oosFunc("*AccountTestService.testOpenAIImageAPIKey", "API Key 图片测试走第三方兼容上游，非官方 Codex 端点"),

	// =================================================================
	// chatgpt-web persona
	// =================================================================
	{
		funcExact:     "disableOpenAITraining",
		runtimeSinkID: "web.privacy.disable_training", purpose: "privacy_setting",
		persona: "chatgpt-web", routes: []string{"PATCH chatgpt.com/backend-api/settings/account_user_setting"},
		backend: "req_profile", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B",
		expiry:    "1B 修复 wire 缺陷（Chrome 120→133、导航 Header→XHR）后进 enforced",
		rationale: "Chrome impersonate；wire 实测含导航专属 Header 且指纹版本过时",
	},
	{
		funcExact:     "fetchChatGPTAccountInfo",
		runtimeSinkID: "web.privacy.account_info", purpose: "account_info",
		persona: "chatgpt-web", routes: []string{"GET chatgpt.com/backend-api/accounts/check/v4-2023-04-27"},
		backend: "req_profile", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B",
		expiry:    "1B 修复 wire 缺陷后进 enforced",
		rationale: "同 web.privacy.disable_training",
	},
	{
		funcExact:     "fetchChatGPTSubscriptionExpiresAt",
		runtimeSinkID: "web.privacy.subscription", purpose: "subscription_info",
		persona: "chatgpt-web", routes: []string{"GET chatgpt.com/backend-api/subscriptions"},
		backend: "req_profile", state: "legacy_observe",
		owner: ownerEgress, changeset: "1B",
		expiry:    "1B 修复 wire 缺陷后进 enforced",
		rationale: "同 web.privacy.disable_training",
	},

	// =================================================================
	// 共享传输基础设施：不承载业务身份
	// =================================================================
	{
		filePrefix: "repository/http_upstream.go",
		persona:    "infrastructure", backend: "-", state: "not_applicable",
		owner: ownerEgress, changeset: "-",
		rationale: "HTTPUpstream 实现层；业务身份由调用方 RuntimeSinkID 决定",
	},
	{
		filePrefix: "pkg/tlsfingerprint/",
		persona:    "infrastructure", backend: "-", state: "not_applicable",
		owner: ownerEgress, changeset: "-",
		rationale: "TLS 指纹与 header casing 属 transport adapter wire 层；后续保持该分层",
	},
	{
		filePrefix: "pkg/httpclient/",
		persona:    "infrastructure", backend: "-", state: "not_applicable",
		owner: ownerEgress, changeset: "1B",
		rationale: "共享客户端池实现层；1B 起对官方 host 拒绝构建普通客户端",
	},
	{
		filePrefix: "pkg/servertiming/",
		persona:    "infrastructure", backend: "-", state: "not_applicable",
		owner: ownerEgress, changeset: "-",
		rationale: "server timing 中间件，转发 RoundTrip",
	},
	{
		filePrefix: "repository/req_client_pool.go",
		persona:    "infrastructure", backend: "-", state: "not_applicable",
		owner: ownerEgress, changeset: "1B",
		rationale: "req/v3 池；同时承载 codex 与 chrome 两种 persona 的 TLS profile",
	},
	{
		funcExact: "*guardedRoundTripper.RoundTrip",
		persona:   "infrastructure", backend: "guard", state: "not_applicable",
		owner: ownerEgress, changeset: "1A",
		rationale: "Guard 判定后的 terminal delegation；禁止生成业务 SinkID",
	},
	{
		funcExact: "*officialCodexHTTPUpstreamPort.SendHTTPUpstream",
		persona:   "infrastructure", backend: "http_upstream", state: "not_applicable",
		owner: ownerEgress, changeset: "1B",
		rationale: "Executor adapter 的 terminal delegation；业务 SinkID 与 FinalizationToken 均由上游 Executor 冻结",
	},
	{
		candidateExact: "github.com/Wei-Shaw/sub2api/internal/service.*officialEgressWebSocketRoundTripper.RoundTrip@backend/internal/service/official_egress_transport_adapters.go#roundtripper_roundtrip#1",
		persona:        "infrastructure", backend: "websocket", state: "not_applicable",
		owner: "-", changeset: "5",
		rationale: "变更集 5 source transition 后的物理 RoundTripper；业务 SinkID 由 Executor Acquire 上下文提供",
	},

	// =================================================================
	// out-of-scope：整包排除，每条附理由
	// =================================================================
	oos("service/official_egress_transport.go", "Anthropic official egress facade；Anthropic persona 化另行规划"),
	oos("service/email_service.go", "SMTP 邮件发送"),
	oos("service/channel_monitor", "渠道监控探测管理员配置的第三方 endpoint"),
	oos("service/crs_sync_service.go", "CRS 账号同步"),
	oos("service/antigravity", "Antigravity 平台"),
	oos("service/gemini", "Gemini 兼容层"),
	oos("service/batch_image_provider", "批量图片 provider"),
	oos("service/grok", "Grok 平台"),
	oos("service/openai_gateway_grok", "Grok 桥接"),
	oos("repository/claude_", "Anthropic 体系"),
	oos("repository/gemini_", "Gemini OAuth"),
	oos("repository/grok_", "Grok OAuth"),
	oos("repository/geminicli_", "Gemini CLI code assist"),
	oos("repository/github_release_service.go", "版本检查，目标 github.com"),
	oos("repository/pricing_service.go", "价格表拉取"),
	oos("repository/proxy_probe_service.go", "代理连通性探测"),
	oos("repository/turnstile_service.go", "Cloudflare Turnstile 校验"),
	oos("handler/auth_", "第三方登录"),
	oos("handler/admin/account_handler_keeper.go", "keeper 代理只接受 API Key 账号"),
	oos("payment/provider/", "支付网关"),
	oos("pkg/geminicli/", "Gemini CLI Drive"),
	oos("pkg/websearch/", "Brave/Tavily 搜索"),
	oos("securityaudit/", "内容安全审计"),
	oos("service/content_moderation.go", "内容审核"),
	oos("service/image_storage.go", "图片下载与对象存储"),
	oos("service/ollama_cloud_usage.go", "Ollama Cloud"),
	oos("service/admin_proxy.go", "代理质量检测"),
	oos("service/vertex_service_account.go", "Vertex token 交换"),
	oos("service/setting_oauth.go", "OIDC metadata 发现"),
	oos("service/upstream_", "上游计费与模型探测，目标为账号 base_url"),
	oos("service/openai_embeddings.go", "embeddings 走第三方兼容上游"),
	oos("service/openai_gateway_cc_pipeline.go", "chat completions 兼容管线"),
	oos("service/openai_gateway_count_tokens.go", "count_tokens 兼容路径"),
	oos("service/gateway_", "Anthropic 网关"),
	oos("service/openai_images.go", "图片兼容层其余部分走第三方上游"),
}

// classify 匹配规则。受审 candidateExact 优先，其次是 funcExact，最后才是
// filePrefix：多平台共用文件里，先按文件匹配会把邻居的 persona 传染给它。
func classify(rec SinkRecord) (classifyRule, bool) {
	for _, rule := range classifyRules {
		if rule.candidateExact != "" && rec.ScanCandidateID == rule.candidateExact {
			return rule, true
		}
	}
	for _, rule := range classifyRules {
		if rule.funcExact == "" || rec.Func != rule.funcExact {
			continue
		}
		if rule.sinkKind != "" && rec.SinkKind != rule.sinkKind {
			continue
		}
		return rule, true
	}
	for _, rule := range classifyRules {
		if rule.filePrefix == "" || !strings.Contains(rec.File, rule.filePrefix) {
			continue
		}
		if rule.sinkKind != "" && rec.SinkKind != rule.sinkKind {
			continue
		}
		return rule, true
	}
	return classifyRule{}, false
}

func applyClassification(records []SinkRecord) ([]SinkRecord, []string) {
	return applyClassificationWithHistory(records, nil)
}

// applyClassificationWithHistory 仅供 bootstrap 历史源码回放使用。当前分类规则已经
// 随生产兼容代码退休时，允许由追加式 RemovalReceipt 为完全相同的 candidate ID
// 提供冻结分类；未被收据覆盖的新发送点仍按未分类硬失败。
func applyClassificationWithHistory(
	records []SinkRecord,
	historical map[string]SinkRecord,
) ([]SinkRecord, []string) {
	var unclassified []string
	out := make([]SinkRecord, 0, len(records))
	for _, rec := range records {
		rule, ok := classify(rec)
		if !ok {
			if frozen, reviewed := historical[rec.ScanCandidateID]; reviewed {
				rec.RuntimeSinkID = frozen.RuntimeSinkID
				rec.Purpose = frozen.Purpose
				rec.Persona = frozen.Persona
				rec.EndpointEvidence = frozen.EndpointEvidence
				rec.Routes = append([]string(nil), frozen.Routes...)
				rec.IsFacade = frozen.IsFacade
				rec.Backend = frozen.Backend
				rec.TargetBackend = frozen.TargetBackend
				rec.EnforcementState = frozen.EnforcementState
				rec.Owner = frozen.Owner
				rec.MigrationChangeset = frozen.MigrationChangeset
				rec.ExpiryCondition = frozen.ExpiryCondition
				rec.Rationale = frozen.Rationale
				out = append(out, rec)
				continue
			}
			unclassified = append(unclassified,
				fmt.Sprintf("%s  (%s @ %s:%d，函数 %s)",
					rec.ScanCandidateID, rec.Callee, rec.File, rec.Line, rec.Func))
			out = append(out, rec)
			continue
		}
		rec.RuntimeSinkID = rule.runtimeSinkID
		rec.Purpose = rule.purpose
		rec.Persona = rule.persona
		rec.EndpointEvidence = rule.endpointEvidence
		if rec.EndpointEvidence == "" {
			switch {
			case rec.Persona == "codex-cli" && !rule.isFacade:
				rec.EndpointEvidence = "codex_profile"
			case rec.Persona == "chatgpt-web":
				rec.EndpointEvidence = "external_persona"
			case rec.Persona == "unclassified":
				rec.EndpointEvidence = "missing"
			default:
				rec.EndpointEvidence = "not_applicable"
			}
		}
		rec.Routes = append([]string(nil), rule.routes...)
		sort.Strings(rec.Routes)
		rec.IsFacade = rule.isFacade
		rec.Backend = rule.backend
		rec.TargetBackend = rule.targetBackend
		if rec.TargetBackend == "" {
			rec.TargetBackend = rule.backend
		}
		rec.EnforcementState = rule.state
		rec.Owner = rule.owner
		rec.MigrationChangeset = rule.changeset
		rec.ExpiryCondition = rule.expiry
		rec.Rationale = rule.rationale
		out = append(out, rec)
	}
	return out, unclassified
}

// validateClassification 检查分类结果自身的完整性。
//
// 「填了值」不等于「填对了」。这些约束把审核时靠肉眼发现的问题变成机器断言：
// legacy_observe 没有到期条件就是永久豁免；in-scope 缺 purpose 或 owner 就生成不出
// 完整的 RouteKey 与 SinkBinding，Guard 无法按业务语义 enforce。
// codexAuthoritativeRoutes 是 codex-cli persona 的权威 route 闭集，逐条对应
// 版本画像文件中的「必需端点 ID 清单」函数。
//
// 存在的理由：上一版 persona 表按「images/*」通配合并，把 generations 与 edits
// 记成一条，16 条端点写成了 14 条。通配符既掩盖了遗漏，又让 Guard 无法按
// RouteKey 四元组精确匹配。这里做双向断言——少一条或多一条都失败。
var codexAuthoritativeRoutes = []string{
	"GET chatgpt.com/backend-api/codex/models",
	"POST chatgpt.com/backend-api/codex/responses",
	"GET chatgpt.com/backend-api/codex/responses (WebSocket)",
	"POST chatgpt.com/backend-api/codex/responses/compact",
	"POST chatgpt.com/backend-api/codex/alpha/search",
	"POST chatgpt.com/backend-api/codex/images/generations",
	"POST chatgpt.com/backend-api/codex/images/edits",
	"POST chatgpt.com/backend-api/codex/realtime/calls",
	"GET api.openai.com/v1/realtime (WebSocket)",
	"GET chatgpt.com/backend-api/wham/usage",
	"GET chatgpt.com/backend-api/wham/rate-limit-reset-credits",
	"POST chatgpt.com/backend-api/wham/rate-limit-reset-credits/consume",
	"POST auth.openai.com/oauth/token",
	"POST chatgpt.com/backend-api/files",
	"PUT *.oaiusercontent.com/{server_returned_path}",
	"POST chatgpt.com/backend-api/files/{file_id}/uploaded",
}

// validateCodexRouteClosure 双向校验 codex-cli 的 route 与权威闭集一致。
func validateCodexRouteClosure(records []SinkRecord, reviewedRoutes []string) []string {
	authoritative := map[string]bool{}
	for _, r := range codexAuthoritativeRoutes {
		authoritative[r] = false
	}
	var problems []string
	for _, rec := range records {
		if rec.Persona != "codex-cli" || rec.IsFacade {
			continue
		}
		for _, r := range rec.Routes {
			seen, ok := authoritative[r]
			if !ok {
				problems = append(problems, fmt.Sprintf(
					"%s：route %q 不在 codex-cli 权威闭集内", rec.ScanCandidateID, r))
				continue
			}
			_ = seen
			authoritative[r] = true
		}
	}
	for _, route := range reviewedRoutes {
		if _, ok := authoritative[route]; ok {
			authoritative[route] = true
		}
	}
	for _, r := range codexAuthoritativeRoutes {
		if !authoritative[r] {
			problems = append(problems, fmt.Sprintf(
				"权威 route %q 没有任何 sink 覆盖——要么漏登记了发送点，要么该端点已废弃", r))
		}
	}
	return problems
}

func validateClassification(records []SinkRecord, reviewedRoutes []string) []string {
	problems := validateCodexRouteClosure(records, reviewedRoutes)
	inScope := func(p string) bool {
		switch p {
		case "codex-cli", "chatgpt-web", "unclassified", "dead-code":
			return true
		}
		return false
	}
	for _, r := range records {
		switch r.EndpointEvidence {
		case "codex_profile", "transport_only", "external_persona", "missing", "not_applicable":
		default:
			problems = append(problems, fmt.Sprintf(
				"%s：endpoint_evidence 非法: %q", r.ScanCandidateID, r.EndpointEvidence))
		}
		switch r.Persona {
		case "codex-cli":
			if r.IsFacade && r.EndpointEvidence != "not_applicable" {
				problems = append(problems, fmt.Sprintf(
					"%s：Codex facade 不应声明端点画像证据", r.ScanCandidateID))
			}
			if !r.IsFacade && r.EndpointEvidence != "codex_profile" && r.EndpointEvidence != "transport_only" {
				problems = append(problems, fmt.Sprintf(
					"%s：Codex 业务 Sink 缺少合法端点证据状态", r.ScanCandidateID))
			}
		case "chatgpt-web":
			if r.EndpointEvidence != "external_persona" {
				problems = append(problems, fmt.Sprintf(
					"%s：chatgpt-web 必须使用 external_persona 证据状态", r.ScanCandidateID))
			}
		case "unclassified":
			if r.EndpointEvidence != "missing" {
				problems = append(problems, fmt.Sprintf(
					"%s：未分类身份必须标记 missing 证据", r.ScanCandidateID))
			}
		}
		if !inScope(r.Persona) {
			continue
		}
		id := r.ScanCandidateID
		if r.RuntimeSinkID == "" {
			problems = append(problems, fmt.Sprintf("%s：in-scope 但缺 runtime_sink_id", id))
		}
		// dead-code 的终点是删除而非 enforce，不要求登记 route——
		// 给一条待删链路补 RouteKey 会让它看起来像是被纳入了体系。
		if len(r.Routes) == 0 && !r.IsFacade && r.Persona != "dead-code" {
			problems = append(problems, fmt.Sprintf(
				"%s：in-scope 非 facade 却没有 route，Guard 无法构成 RouteKey", id))
		}
		if r.Purpose == "" {
			problems = append(problems, fmt.Sprintf("%s：in-scope 但缺 purpose，无法构成 RouteKey", id))
		}
		if ownerPlaceholders[r.Owner] {
			problems = append(problems, fmt.Sprintf(
				"%s：owner %q 是占位符——责任人必须是真实的人，否则到期无人清理", id, r.Owner))
		}
		if r.EnforcementState == "legacy_observe" && r.ExpiryCondition == "" {
			problems = append(problems,
				fmt.Sprintf("%s：legacy_observe 缺到期条件，等于永久豁免", id))
		}
		// unclassified 身份未举证，enforce 它等于把不知道是什么的东西宣布为合规。
		if r.Persona == "unclassified" {
			switch r.EnforcementState {
			case "canary_enforce", "enforced":
				problems = append(problems, fmt.Sprintf(
					"%s：persona 为 unclassified 却处于 %s——身份未举证不得 enforce", id, r.EnforcementState))
			}
		}
		if r.MigrationChangeset == "" || r.MigrationChangeset == "-" {
			switch r.EnforcementState {
			case "legacy_observe", "pending_removal":
				problems = append(problems,
					fmt.Sprintf("%s：状态为 %s 但未指定迁移变更集", id, r.EnforcementState))
			}
		}
	}
	return problems
}

// unclassifiedError 让未分类与分类不完整都成为硬失败。
type unclassifiedError struct {
	sinks    []string
	problems []string
}

func (e *unclassifiedError) Error() string {
	var b strings.Builder
	if len(e.sinks) > 0 {
		fmt.Fprintf(&b, "%d 条 sink 未匹配任何分类规则：\n", len(e.sinks))
		for _, s := range e.sinks {
			fmt.Fprintf(&b, "  - %s\n", s)
		}
		_, _ = b.WriteString("\n新增发送点必须在 cmd/egressscan/classify.go 显式登记。\n")
		_, _ = b.WriteString("in-scope 必须用 funcExact 精确到函数：多平台共用文件里，文件级通配会产生错误绑定。\n")
	}
	if len(e.problems) > 0 {
		fmt.Fprintf(&b, "\n%d 条分类结果不完整：\n", len(e.problems))
		for _, p := range e.problems {
			fmt.Fprintf(&b, "  - %s\n", p)
		}
	}
	return b.String()
}
