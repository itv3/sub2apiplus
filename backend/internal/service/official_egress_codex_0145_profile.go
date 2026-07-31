package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"regexp"
	"sort"
	"strings"
)

const (
	officialCodexVersion0145 = "0.145.0"

	officialCodexSurfaceExec = "exec"
	officialCodexSurfaceTUI  = "tui"

	officialCodexTransportHTTPDefault = "codex-0.145.0-http-ubuntu24-native"
	officialCodexTransportWS          = "codex-0.145.0-ws-rustls"

	officialCodexEndpointModels                 = "models"
	officialCodexEndpointResponsesHTTP          = "responses_http"
	officialCodexEndpointResponsesWS            = "responses_ws"
	officialCodexEndpointResponsesCompact       = "responses_compact"
	officialCodexEndpointAlphaSearch            = "alpha_search"
	officialCodexEndpointImagesGenerations      = "images_generations"
	officialCodexEndpointImagesEdits            = "images_edits"
	officialCodexEndpointRealtimeCalls          = "realtime_calls"
	officialCodexEndpointRealtimeSideband       = "realtime_sideband"
	officialCodexEndpointWhamUsage              = "wham_usage"
	officialCodexEndpointWhamResetCredits       = "wham_rate_limit_reset_credits"
	officialCodexEndpointWhamConsumeResetCredit = "wham_rate_limit_reset_credits_consume"
	officialCodexEndpointOAuthRefresh           = "oauth_refresh"
	officialCodexEndpointFilesCreate            = "files_create"
	officialCodexEndpointFilesBlobUpload        = "files_blob_upload"
	officialCodexEndpointFilesUploaded          = "files_uploaded"

	officialCodexCompressionNone              = "none"
	officialCodexCompressionZstdFeature       = "zstd_when_feature_enabled"
	officialCodexCompressionPerMessageDeflate = "permessage_deflate_context_takeover"

	officialCodexClientPerUpperCall      = "per_upper_api_call"
	officialCodexClientWebSocket         = "websocket_connection"
	officialCodexClientBackendLongLived  = "backend_client_long_lived"
	officialCodexClientReturnedUploadURL = "returned_upload_url_call"

	officialCodexHeaderOrderH1HeaderMap  = "h1_header_map_final_order"
	officialCodexHeaderOrderWSSwapRemove = "ws_fixed_prefix_then_header_map_swap_remove"
	officialCodexHeaderOrderExplicit     = "explicit_order"

	officialCodexConditionAlways             = "always"
	officialCodexConditionAuto               = "auto"
	officialCodexConditionCookie             = "cookie_present"
	officialCodexConditionRemoteCompactionV2 = "remote_compaction_v2"
	officialCodexConditionBetaFeatures       = "beta_features_present"
	officialCodexConditionResponsesLite      = "responses_lite"
	officialCodexConditionRequestCompression = "request_compression_enabled"
	officialCodexConditionTurnState          = "turn_state_present"
	officialCodexConditionResidency          = "managed_residency_present"
	officialCodexConditionSubagent           = "subagent_present"
	officialCodexConditionMemoryGeneration   = "memory_generation"
	officialCodexConditionParentThread       = "parent_thread_present"
	officialCodexConditionRuntimeMetrics     = "runtime_metrics"
	officialCodexConditionSessionID          = "session_id_present"
	officialCodexConditionCreditID           = "credit_id_present"
	officialCodexConditionAttestation        = "attestation_present"
	officialCodexConditionFedRAMP            = "fedramp_account"

	officialCodexSourceConstant       = "constant"
	officialCodexSourceAuthentication = "authentication"
	officialCodexSourceAccount        = "account"
	officialCodexSourceManagedConfig  = "managed_config"
	officialCodexSourceModelManifest  = "model_manifest"
	officialCodexSourceTurn           = "turn"
	officialCodexSourceSession        = "session"
	officialCodexSourceProcess        = "process"
	officialCodexSourceGenerated      = "generated"
	officialCodexSourceServerResponse = "server_response"
	officialCodexSourceRequestBody    = "request_body"
	officialCodexSourceFeature        = "feature"
)

// codex0145EndpointID 是 0.145.0 画像允许的端点标识类型。
// 动态字符串必须显式转换后才能进入解析器，避免调用方误把路径或别名当作端点 ID。
type codex0145EndpointID string

// officialCodexVersionProfile 是一个完整、不可变快照的内存表示。
//
// 规范数据只在构造阶段出现一次，随后被编码成不可变字符串。任何解析调用都会重新
// 解码，因此调用方永远拿不到内部规范快照中的切片或映射引用。
type officialCodexVersionProfile struct {
	Version          string
	RequiredRules    []string
	Surfaces         []officialCodexSurfaceProfile
	FeatureDefaults  officialCodexFeatureDefaults
	ToolPresentation officialCodexToolPresentationProfile
	Subagents        officialCodexSubagentProfile
	Files            officialCodexFilesProfile
	Transports       []officialCodexTransportProfile
	Endpoints        []officialCodexEndpointProfile
	Digest           string
}

// officialCodexSurfaceProfile 描述同一版本在不同入口上的用户代理身份。
type officialCodexSurfaceProfile struct {
	ID                      string
	Product                 string
	Version                 string
	PlatformPrefix          string
	DefaultTerminalToken    string
	TerminalTokenPattern    string
	SuffixName              string
	SuffixVersion           string
	SuffixOptional          bool
	InitialModelsMayOmit    bool
	Originator              string
	InitialModelsOriginator string
}

// officialCodexFeatureDefaults 保存影响可见出站的版本默认开关。
type officialCodexFeatureDefaults struct {
	SupportsWebSockets             bool
	RemoteCompactionV2             bool
	EnableRequestCompression       bool
	RequestCompressionLevel        int
	RuntimeMetrics                 bool
	ForceHTTPFallback              bool
	ResponsesLiteFromModelManifest bool
	ParallelToolsFromModelManifest bool
}

// officialCodexToolPresentationProfile 固定模型内生图能力在 Responses 中的形态。
// 它与独立 images 端点画像配套：模型阶段只允许 namespace/imagegen，不得把
// OpenAI 公共 API 的 hosted image_generation 工具伪装成 Codex CLI 出站。
type officialCodexToolPresentationProfile struct {
	EndpointIDs                  []string
	HostedImageGenerationAllowed bool
	HostedImageGenerationType    string
	NamespaceType                string
	NamespaceName                string
	FunctionType                 string
	FunctionName                 string
	LiteCarrierItemType          string
	NamespaceRequiredFields      []string
	FunctionRequiredFields       []string
}

// officialCodexSubagentProfile 固定条件 header 与 turn metadata 的非对称映射。
// ThreadSpawn 和 internal memory consolidation 的 wire 值并不等于 metadata kind，
// 因此不能用通用字符串等式或封闭的业务分支替代这份版本画像。
type officialCodexSubagentProfile struct {
	Mappings              []officialCodexSubagentMapping
	OtherLabelAllowed     bool
	OtherThreadSource     string
	OtherHeaderEqualsKind bool
}

type officialCodexSubagentMapping struct {
	ID                   string
	HeaderValue          string
	MetadataKind         string
	ThreadSource         string
	MemoryGeneration     bool
	ParentThreadRequired bool
}

// officialCodexFilesProfile 保存文件上传跨端点流程的版本级定型数据。
//
// 三个端点自身仍分别声明 URL、header 与 body；这里仅描述它们的顺序关系和
// codex-api/src/files.rs 中跨请求生效的限制，执行器不得另建一套平行常量。
type officialCodexFilesProfile struct {
	CreateEndpointID         string
	BlobUploadEndpointID     string
	UploadedEndpointID       string
	UploadLimitBytes         uint64
	RequestTimeoutMillis     int64
	FinalizeTimeoutMillis    int64
	FinalizeRetryDelayMillis int64
	UseCase                  string
	URIPrefix                string
	FinalizeSuccessStatus    string
	FinalizeRetryStatus      string
}

// officialCodexTransportProfile 描述 HTTP 或 WS 的传输和连接边界。
type officialCodexTransportProfile struct {
	ID                       string
	Protocol                 string
	PlatformCondition        string
	TLSStack                 string
	CipherSuites             []uint16
	SupportedGroups          []uint16
	SignatureAlgorithms      []uint16
	ALPN                     []string
	Extensions               []uint16
	RandomizeExtensions      bool
	SupportedVersions        []uint16
	KeyShareGroups           []uint16
	PSKModes                 []uint16
	TLSMinVersion            uint16
	TLSMaxVersion            uint16
	LowercaseHTTPHeaders     bool
	CrossCallConnectionReuse bool
	RetryReusesClient        bool
	WebSocket                *officialCodexWebSocketTransportProfile
}

// officialCodexWebSocketTransportProfile 保存 WS 特有的握手和帧参数。
type officialCodexWebSocketTransportProfile struct {
	FixedHandshakePrefix []string
	RemainingHeaderMode  string
	CompressionOffer     string
	CompressedTextRSV1   bool
	RawDeflatePayload    bool
	ContextTakeover      bool
}

// officialCodexEndpointProfile 是端点级最终定型数据，不允许执行器自行推断缺失项。
type officialCodexEndpointProfile struct {
	ID                      string
	Method                  string
	Upgrade                 string
	TransportID             string
	Host                    string
	HostFromResponse        bool
	Path                    string
	Query                   []officialCodexQueryField
	Accept                  string
	ContentType             string
	Compression             string
	ClientLifecycle         string
	HeaderOrderMode         string
	Headers                 []officialCodexHeaderSlot
	HeaderMapInsertionOrder []string
	PostRemoveHeaders       []string
	Body                    officialCodexBodyContract
}

// officialCodexQueryField 描述固定 query 或从上下文读取的动态 query。
type officialCodexQueryField struct {
	Name     string
	Value    string
	Source   string
	Required bool
}

// officialCodexHeaderSlot 用槽位而非条件分支拼接 header。
// 同一 Slot 可包含多个条件项，Sequence 决定它们同时出现时的确定顺序。
type officialCodexHeaderSlot struct {
	Slot           int
	Sequence       int
	Name           string
	WireName       string
	Value          string
	Source         string
	Condition      string
	AlternateGroup string
}

// officialCodexBodyContract 描述顶层序列化结构体的字段和省略条件。
type officialCodexBodyContract struct {
	Encoding      string
	Closed        bool
	Discriminator string
	Fields        []officialCodexBodyField
}

// officialCodexBodyField 的切片顺序就是线上 JSON 字段顺序。
type officialCodexBodyField struct {
	Name      string
	Required  bool
	OmitWhen  string
	Condition string
}

type officialCodexProfileSnapshot struct {
	JSON   string
	Digest string
}

var defaultOfficialCodex0145Snapshot = mustBuildOfficialCodex0145Snapshot()

// officialCodexVersionSnapshots 是版本快照注册表。升级 Codex 版本时只在此登记新
// 快照并调整 registry 的 release 指针，不修改稳定执行引擎，也不修改 §3.5.2 的
// 共享接入点——这正是 §4.5.13 承诺的落地方式。
var officialCodexVersionSnapshots = map[string]officialCodexProfileSnapshot{
	officialCodexVersion0145: defaultOfficialCodex0145Snapshot,
}

// resolveCodex0145VersionProfile 只接受精确三段版本，不做 trim、别名或回退；
// 未登记的版本按未知处理，不回退到任何既有快照。
func resolveCodex0145VersionProfile(version string) (*officialCodexVersionProfile, error) {
	snapshot, exists := officialCodexVersionSnapshots[version]
	if !exists {
		return nil, fmt.Errorf("未知 Codex 官方出站版本画像：%q", version)
	}
	var profile officialCodexVersionProfile
	if err := json.Unmarshal([]byte(snapshot.JSON), &profile); err != nil {
		return nil, fmt.Errorf("解码 Codex %s 版本画像：%w", version, err)
	}
	if err := validateOfficialCodexVersionProfile(profile); err != nil {
		return nil, fmt.Errorf("校验 Codex %s 版本画像：%w", version, err)
	}
	digest, err := digestOfficialCodexVersionProfile(profile)
	if err != nil {
		return nil, err
	}
	if digest != snapshot.Digest || profile.Digest != digest {
		return nil, fmt.Errorf("Codex %s 版本画像摘要不一致", version)
	}
	return &profile, nil
}

// resolveCodex0145Endpoint 同时执行精确版本和精确端点解析，并返回端点深拷贝。
func resolveCodex0145Endpoint(version string, endpointID codex0145EndpointID) (officialCodexEndpointProfile, error) {
	profile, err := resolveCodex0145VersionProfile(version)
	if err != nil {
		return officialCodexEndpointProfile{}, err
	}
	return profile.ResolveEndpoint(string(endpointID))
}

// resolveOfficialCodexVersionProfile 保留旧接入名，统一委托给严格版本解析器。
func resolveOfficialCodexVersionProfile(version string) (*officialCodexVersionProfile, error) {
	return resolveCodex0145VersionProfile(version)
}

// resolveOfficialCodexEndpointProfile 保留旧接入名，统一委托给严格端点解析器。
func resolveOfficialCodexEndpointProfile(version, endpointID string) (officialCodexEndpointProfile, error) {
	return resolveCodex0145Endpoint(version, codex0145EndpointID(endpointID))
}

// OfficialCodexRemoteCompactionV2Default 把 handler 的压缩分派决策绑定到版本画像，
// 避免再次把“header 是否出现”误当成 feature 默认值。显式 legacy 请求仍通过
// /responses/compact 选择；普通 /responses 中已有 compaction_trigger 时保持 V2。
func OfficialCodexRemoteCompactionV2Default() bool {
	profile, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	if err != nil {
		panic(err)
	}
	return profile.FeatureDefaults.RemoteCompactionV2
}

// ResolveEndpoint 返回端点画像的深拷贝，未知端点或带空白的近似 ID 均失败。
func (p *officialCodexVersionProfile) ResolveEndpoint(endpointID string) (officialCodexEndpointProfile, error) {
	if p == nil {
		return officialCodexEndpointProfile{}, errors.New("Codex 版本画像为空")
	}
	for _, endpoint := range p.Endpoints {
		if endpoint.ID != endpointID {
			continue
		}
		encoded, err := json.Marshal(endpoint)
		if err != nil {
			return officialCodexEndpointProfile{}, fmt.Errorf("复制端点画像 %s：%w", endpointID, err)
		}
		var cloned officialCodexEndpointProfile
		if err := json.Unmarshal(encoded, &cloned); err != nil {
			return officialCodexEndpointProfile{}, fmt.Errorf("复制端点画像 %s：%w", endpointID, err)
		}
		return cloned, nil
	}
	return officialCodexEndpointProfile{}, fmt.Errorf("Codex %s 不支持端点画像：%q", p.Version, endpointID)
}

// OrderedHeaders 返回按槽位和槽内序号排列的 header 深拷贝。
// 执行器只能消费此结果，不能把画像 JSON 中的切片存储顺序解释为 wire 线序。
func (p officialCodexEndpointProfile) OrderedHeaders() []officialCodexHeaderSlot {
	headers := append([]officialCodexHeaderSlot(nil), p.Headers...)
	sort.SliceStable(headers, func(i, j int) bool {
		if headers[i].Slot != headers[j].Slot {
			return headers[i].Slot < headers[j].Slot
		}
		return headers[i].Sequence < headers[j].Sequence
	})
	return headers
}

// RenderUserAgent 按入口和进程状态生成 UA，suffix 是显式条件而不是固定常量。
func (p *officialCodexVersionProfile) RenderUserAgent(surfaceID string, includeSuffix bool) (string, error) {
	if p == nil {
		return "", errors.New("Codex 版本画像为空")
	}
	for _, surface := range p.Surfaces {
		if surface.ID == surfaceID {
			return p.RenderUserAgentWithTerminal(surfaceID, surface.DefaultTerminalToken, includeSuffix)
		}
	}
	return "", fmt.Errorf("Codex %s 不支持入口画像：%q", p.Version, surfaceID)
}

// RenderUserAgentWithTerminal 按可信入口冻结的终端 token 渲染 UA。动态 token 的
// 语法约束仍来自版本画像；普通第三方派生始终使用画像默认 unknown。
func (p *officialCodexVersionProfile) RenderUserAgentWithTerminal(
	surfaceID string,
	terminalToken string,
	includeSuffix bool,
) (string, error) {
	if p == nil {
		return "", errors.New("Codex 版本画像为空")
	}
	for _, surface := range p.Surfaces {
		if surface.ID != surfaceID {
			continue
		}
		if matched, err := regexp.MatchString(surface.TerminalTokenPattern, terminalToken); err != nil || !matched {
			return "", fmt.Errorf("Codex %s 入口 %s 的终端 token 无效", p.Version, surfaceID)
		}
		base := strings.TrimSpace(surface.Product + "/" + surface.Version + " " + surface.PlatformPrefix + " " + terminalToken)
		if !includeSuffix {
			return base, nil
		}
		if !surface.SuffixOptional || surface.SuffixName == "" || surface.SuffixVersion == "" {
			return "", fmt.Errorf("Codex %s 入口 %s 不允许 UA suffix", p.Version, surfaceID)
		}
		return fmt.Sprintf("%s (%s; %s)", base, surface.SuffixName, surface.SuffixVersion), nil
	}
	return "", fmt.Errorf("Codex %s 不支持入口画像：%q", p.Version, surfaceID)
}

func mustBuildOfficialCodex0145Snapshot() officialCodexProfileSnapshot {
	profile, err := newOfficialCodex0145VersionProfile()
	if err != nil {
		panic(err)
	}
	digest, err := digestOfficialCodexVersionProfile(profile)
	if err != nil {
		panic(err)
	}
	profile.Digest = digest
	encoded, err := json.Marshal(profile)
	if err != nil {
		panic(err)
	}
	return officialCodexProfileSnapshot{JSON: string(encoded), Digest: digest}
}

func newOfficialCodex0145VersionProfile() (officialCodexVersionProfile, error) {
	profile := officialCodexVersionProfile{
		Version:          officialCodexVersion0145,
		RequiredRules:    officialCodex0145RequiredRules(),
		Surfaces:         officialCodex0145Surfaces(),
		FeatureDefaults:  officialCodex0145FeatureDefaults(),
		ToolPresentation: officialCodex0145ToolPresentation(),
		Subagents:        officialCodex0145Subagents(),
		Files:            officialCodex0145FilesProfile(),
		Transports:       officialCodex0145Transports(),
		Endpoints:        officialCodex0145Endpoints(),
	}
	if err := validateOfficialCodexVersionProfile(profile); err != nil {
		return officialCodexVersionProfile{}, err
	}
	return profile, nil
}

func officialCodex0145RequiredRules() []string {
	return []string{
		"SPEC-TLS-001", "SPEC-TLS-003", "SPEC-PROTO-001", "SPEC-PROTO-002",
		"SPEC-CONN-001", "SPEC-H1-001", "SPEC-H1-002", "SPEC-H1-003",
		"SPEC-H1-004", "SPEC-WS-001", "SPEC-WS-002", "SPEC-WS-004",
		"SPEC-WS-005", "SPEC-HDR-001", "SPEC-HDR-002", "SPEC-HDR-004",
		"SPEC-HDR-005", "SPEC-HDR-006", "SPEC-HDR-007", "SPEC-HDR-008",
		"SPEC-BODY-001", "SPEC-BODY-002", "SPEC-BODY-003", "SPEC-BODY-004",
		"SPEC-BODY-005", "SPEC-BODY-006", "SPEC-EP-001", "SPEC-EP-002",
		"SPEC-EP-005", "SPEC-EP-006", "SPEC-EP-007", "SPEC-EP-008",
		"SPEC-EP-009", "SPEC-EP-012", "SPEC-EP-013", "SPEC-EP-014",
		"SPEC-EP-015", "SPEC-EP-019", "SPEC-EP-020", "SPEC-EP-021",
		"SPEC-EP-022", "SPEC-EP-023",
	}
}

func officialCodex0145Surfaces() []officialCodexSurfaceProfile {
	return []officialCodexSurfaceProfile{
		{
			ID: officialCodexSurfaceExec, Product: "codex_exec", Version: officialCodexVersion0145,
			PlatformPrefix: "(Ubuntu 24.4.0; x86_64)", DefaultTerminalToken: "unknown", TerminalTokenPattern: `^[A-Za-z0-9._/-]+$`, SuffixName: "codex_exec",
			SuffixVersion: officialCodexVersion0145, SuffixOptional: true,
			InitialModelsMayOmit: true, Originator: "codex_exec", InitialModelsOriginator: "codex_cli_rs",
		},
		{
			ID: officialCodexSurfaceTUI, Product: "codex-tui", Version: officialCodexVersion0145,
			PlatformPrefix: "(Ubuntu 24.4.0; x86_64)", DefaultTerminalToken: "unknown", TerminalTokenPattern: `^[A-Za-z0-9._/-]+$`, SuffixName: "codex-tui",
			SuffixVersion: officialCodexVersion0145, SuffixOptional: true,
			InitialModelsMayOmit: true, Originator: "codex-tui", InitialModelsOriginator: "codex_cli_rs",
		},
	}
}

func officialCodex0145FeatureDefaults() officialCodexFeatureDefaults {
	return officialCodexFeatureDefaults{
		SupportsWebSockets: true, RemoteCompactionV2: true,
		EnableRequestCompression: true, RequestCompressionLevel: 3,
		RuntimeMetrics: false, ForceHTTPFallback: false,
		ResponsesLiteFromModelManifest: true, ParallelToolsFromModelManifest: true,
	}
}

func officialCodex0145ToolPresentation() officialCodexToolPresentationProfile {
	return officialCodexToolPresentationProfile{
		EndpointIDs: []string{
			officialCodexEndpointResponsesHTTP,
			officialCodexEndpointResponsesWS,
		},
		HostedImageGenerationAllowed: false,
		HostedImageGenerationType:    "image_generation",
		NamespaceType:                "namespace",
		NamespaceName:                "image_gen",
		FunctionType:                 "function",
		FunctionName:                 "imagegen",
		LiteCarrierItemType:          "additional_tools",
		NamespaceRequiredFields:      []string{"type", "name", "description", "tools"},
		FunctionRequiredFields:       []string{"type", "name", "description", "strict", "parameters"},
	}
}

func officialCodex0145Subagents() officialCodexSubagentProfile {
	return officialCodexSubagentProfile{
		Mappings: []officialCodexSubagentMapping{
			{ID: "review", HeaderValue: "review", MetadataKind: "review", ThreadSource: "subagent"},
			{ID: "compact", HeaderValue: "compact", MetadataKind: "compact", ThreadSource: "subagent"},
			{ID: "thread_spawn", HeaderValue: "collab_spawn", MetadataKind: "thread_spawn", ThreadSource: "subagent", ParentThreadRequired: true},
			{ID: "memory_subagent", HeaderValue: "memory_consolidation", MetadataKind: "memory_consolidation", ThreadSource: "subagent"},
			{ID: "memory_internal", HeaderValue: "memory_consolidation", ThreadSource: "memory_consolidation", MemoryGeneration: true},
		},
		OtherLabelAllowed:     true,
		OtherThreadSource:     "subagent",
		OtherHeaderEqualsKind: true,
	}
}

func officialCodex0145FilesProfile() officialCodexFilesProfile {
	return officialCodexFilesProfile{
		CreateEndpointID:         officialCodexEndpointFilesCreate,
		BlobUploadEndpointID:     officialCodexEndpointFilesBlobUpload,
		UploadedEndpointID:       officialCodexEndpointFilesUploaded,
		UploadLimitBytes:         512 * 1024 * 1024,
		RequestTimeoutMillis:     60 * 1000,
		FinalizeTimeoutMillis:    30 * 1000,
		FinalizeRetryDelayMillis: 250,
		UseCase:                  "codex",
		URIPrefix:                "sediment://",
		FinalizeSuccessStatus:    "success",
		FinalizeRetryStatus:      "retry",
	}
}

func officialCodex0145Transports() []officialCodexTransportProfile {
	return []officialCodexTransportProfile{
		{
			ID: officialCodexTransportHTTPDefault, Protocol: "http/1.1",
			PlatformCondition: "ubuntu_24_04_without_valid_custom_ca", TLSStack: "native_tls_openssl",
			CipherSuites: []uint16{
				0x1302, 0x1303, 0x1301, 0xc02c, 0xc030, 0x009f,
				0xcca9, 0xcca8, 0xccaa, 0xc02b, 0xc02f, 0x009e,
				0xc024, 0xc028, 0x006b, 0xc023, 0xc027, 0x0067,
				0xc00a, 0xc014, 0x0039, 0xc009, 0xc013, 0x0033,
				0x009d, 0x009c, 0x003d, 0x003c, 0x0035, 0x002f,
			},
			SupportedGroups: []uint16{0x11ec, 0x001d, 0x0017, 0x001e, 0x0018, 0x0019, 0x0100, 0x0101},
			SignatureAlgorithms: []uint16{
				0x0905, 0x0906, 0x0904, 0x0403, 0x0503, 0x0603,
				0x0807, 0x0808, 0x081a, 0x081b, 0x081c, 0x0809,
				0x080a, 0x080b, 0x0804, 0x0805, 0x0806, 0x0401,
				0x0501, 0x0601, 0x0303, 0x0301, 0x0302, 0x0402,
				0x0502, 0x0602,
			},
			Extensions:        []uint16{65281, 0, 11, 10, 35, 22, 23, 13, 43, 45, 51},
			SupportedVersions: []uint16{0x0304, 0x0303}, KeyShareGroups: []uint16{0x11ec, 0x001d},
			PSKModes: []uint16{1}, TLSMinVersion: 0x0303, TLSMaxVersion: 0x0304,
			LowercaseHTTPHeaders: true, CrossCallConnectionReuse: false, RetryReusesClient: true,
		},
		{
			ID: officialCodexTransportWS, Protocol: "websocket",
			PlatformCondition: "all_supported_platforms", TLSStack: "rustls",
			CipherSuites: []uint16{
				0x1302, 0x1301, 0x1303, 0xc02c, 0xc02b,
				0xcca9, 0xc030, 0xc02f, 0xcca8, 0x00ff,
			},
			SupportedGroups:     []uint16{0x11ec, 0x001d, 0x0017, 0x0018},
			SignatureAlgorithms: []uint16{0x0503, 0x0403, 0x0603, 0x0807, 0x0806, 0x0805, 0x0804, 0x0601, 0x0501, 0x0401},
			Extensions:          []uint16{0, 5, 10, 11, 13, 23, 35, 43, 45, 51}, RandomizeExtensions: true,
			SupportedVersions: []uint16{0x0304, 0x0303}, KeyShareGroups: []uint16{0x11ec, 0x001d},
			PSKModes: []uint16{1}, TLSMinVersion: 0x0303, TLSMaxVersion: 0x0304,
			CrossCallConnectionReuse: false, RetryReusesClient: true,
			WebSocket: &officialCodexWebSocketTransportProfile{
				FixedHandshakePrefix: []string{"Host", "Connection", "Upgrade", "Sec-WebSocket-Version", "Sec-WebSocket-Key"},
				RemainingHeaderMode:  officialCodexHeaderOrderWSSwapRemove,
				CompressionOffer:     "permessage-deflate; client_max_window_bits",
				CompressedTextRSV1:   true, RawDeflatePayload: true, ContextTakeover: true,
			},
		},
	}
}

func officialCodex0145Endpoints() []officialCodexEndpointProfile {
	return []officialCodexEndpointProfile{
		codex0145ModelsEndpoint(),
		codex0145ResponsesHTTPEndpoint(),
		codex0145ResponsesWSEndpoint(),
		codex0145CompactEndpoint(),
		codex0145SearchEndpoint(),
		codex0145ImagesEndpoint(false),
		codex0145ImagesEndpoint(true),
		codex0145RealtimeCallsEndpoint(),
		codex0145RealtimeSidebandEndpoint(),
		codex0145WhamEndpoint(officialCodexEndpointWhamUsage, http.MethodGet, "/backend-api/wham/usage", false),
		codex0145WhamEndpoint(officialCodexEndpointWhamResetCredits, http.MethodGet, "/backend-api/wham/rate-limit-reset-credits", false),
		codex0145WhamEndpoint(officialCodexEndpointWhamConsumeResetCredit, http.MethodPost, "/backend-api/wham/rate-limit-reset-credits/consume", true),
		codex0145OAuthRefreshEndpoint(),
		codex0145FilesCreateEndpoint(),
		codex0145FilesBlobUploadEndpoint(),
		codex0145FilesUploadedEndpoint(),
	}
}

func codex0145ModelsEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointModels, Method: http.MethodGet,
		TransportID: officialCodexTransportHTTPDefault, Host: "chatgpt.com",
		Path:   "/backend-api/codex/models",
		Query:  []officialCodexQueryField{{Name: "client_version", Value: officialCodexVersion0145, Source: officialCodexSourceConstant, Required: true}},
		Accept: "*/*", Compression: officialCodexCompressionNone,
		ClientLifecycle: officialCodexClientPerUpperCall, HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap,
		Headers: []officialCodexHeaderSlot{
			h(10, "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
			h(20, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
			h(30, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
			h(35, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
			h(40, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
			h(60, "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(70, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(75, "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
			h(80, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		},
		Body: bodyNone(),
	}
}

func codex0145ResponsesHTTPEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointResponsesHTTP, Method: http.MethodPost,
		TransportID: officialCodexTransportHTTPDefault, Host: "chatgpt.com",
		Path: "/backend-api/codex/responses", Accept: "text/event-stream",
		ContentType: "application/json", Compression: officialCodexCompressionZstdFeature,
		ClientLifecycle: officialCodexClientPerUpperCall, HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap,
		Headers: []officialCodexHeaderSlot{
			h(10, "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
			h(20, "x-codex-beta-features", "remote_compaction_v2", officialCodexSourceFeature, officialCodexConditionRemoteCompactionV2),
			h(25, "x-codex-turn-state", "", officialCodexSourceTurn, officialCodexConditionTurnState),
			h(30, "x-codex-window-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			h(40, "x-codex-turn-metadata", "", officialCodexSourceTurn, officialCodexConditionAlways),
			h(50, "x-openai-subagent", "", officialCodexSourceTurn, officialCodexConditionSubagent),
			h(51, "x-openai-memgen-request", "true", officialCodexSourceTurn, officialCodexConditionMemoryGeneration),
			h(52, "x-codex-parent-thread-id", "", officialCodexSourceSession, officialCodexConditionParentThread),
			h(60, "x-openai-internal-codex-responses-lite", "true", officialCodexSourceModelManifest, officialCodexConditionResponsesLite),
			h(70, "x-client-request-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			h(80, "session-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			h(90, "thread-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			h(100, "accept", "text/event-stream", officialCodexSourceConstant, officialCodexConditionAlways),
			h(110, "content-encoding", "zstd", officialCodexSourceFeature, officialCodexConditionRequestCompression),
			h(120, "content-type", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
			h(130, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
			h(140, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
			h(142, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
			h(150, "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(160, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(165, "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
			h(170, "cookie", "", officialCodexSourceSession, officialCodexConditionCookie),
			h(180, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
			h(190, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		},
		Body: bodyJSON(true,
			bf("model", true, ""), bf("instructions", false, "empty_string"), bf("input", true, ""),
			bf("tools", false, "none"), bf("tool_choice", true, ""), bf("parallel_tool_calls", true, ""),
			bf("reasoning", true, ""), bf("store", true, ""), bf("stream", true, ""),
			bf("stream_options", false, "none"), bf("include", true, ""), bf("service_tier", false, "none"),
			bf("prompt_cache_key", false, "none"), bf("text", false, "none"), bf("client_metadata", false, "none"),
		),
	}
}

func codex0145ResponsesWSEndpoint() officialCodexEndpointProfile {
	headers := []officialCodexHeaderSlot{
		wh(10, "host", "Host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		wh(20, "connection", "Connection", "Upgrade", officialCodexSourceGenerated, officialCodexConditionAlways),
		wh(30, "upgrade", "Upgrade", "websocket", officialCodexSourceGenerated, officialCodexConditionAlways),
		wh(40, "sec-websocket-version", "Sec-WebSocket-Version", "13", officialCodexSourceGenerated, officialCodexConditionAlways),
		wh(50, "sec-websocket-key", "Sec-WebSocket-Key", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		wh(60, "chatgpt-account-id", "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
		wh(65, "x-openai-fedramp", "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
		wh(70, "authorization", "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
		wh(80, "user-agent", "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
		wh(90, "originator", "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
		wh(95, "x-openai-internal-codex-residency", "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
		wh(100, "openai-beta", "openai-beta", "responses_websockets=2026-02-06", officialCodexSourceConstant, officialCodexConditionAlways),
		wh(110, "version", "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
		wh(120, "x-codex-beta-features", "x-codex-beta-features", "remote_compaction_v2", officialCodexSourceFeature, officialCodexConditionRemoteCompactionV2),
		wh(130, "x-client-request-id", "x-client-request-id", "", officialCodexSourceSession, officialCodexConditionAlways),
		wh(140, "session-id", "session-id", "", officialCodexSourceSession, officialCodexConditionAlways),
		wh(150, "thread-id", "thread-id", "", officialCodexSourceSession, officialCodexConditionAlways),
		wh(160, "x-codex-window-id", "x-codex-window-id", "", officialCodexSourceSession, officialCodexConditionAlways),
		wh(170, "x-codex-turn-metadata", "x-codex-turn-metadata", "", officialCodexSourceTurn, officialCodexConditionAlways),
		wh(180, "x-openai-subagent", "x-openai-subagent", "", officialCodexSourceTurn, officialCodexConditionSubagent),
		wh(181, "x-openai-memgen-request", "x-openai-memgen-request", "true", officialCodexSourceTurn, officialCodexConditionMemoryGeneration),
		wh(182, "x-codex-parent-thread-id", "x-codex-parent-thread-id", "", officialCodexSourceSession, officialCodexConditionParentThread),
		wh(183, "x-responsesapi-include-timing-metrics", "x-responsesapi-include-timing-metrics", "true", officialCodexSourceFeature, officialCodexConditionRuntimeMetrics),
		wh(190, "sec-websocket-extensions", "sec-websocket-extensions", "permessage-deflate; client_max_window_bits", officialCodexSourceGenerated, officialCodexConditionAlways),
	}
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointResponsesWS, Method: http.MethodGet, Upgrade: "websocket",
		TransportID: officialCodexTransportWS, Host: "chatgpt.com", Path: "/backend-api/codex/responses",
		Compression:     officialCodexCompressionPerMessageDeflate,
		ClientLifecycle: officialCodexClientWebSocket, HeaderOrderMode: officialCodexHeaderOrderWSSwapRemove,
		Headers: headers,
		// 这是 Codex 0.145.0 在 tungstenite 删除五个固定握手头之前的
		// HeaderMap.entries 顺序，不是某次完整抓包的最终顺序。条件头先按本序
		// 缺席，再执行 swap_remove，才能复刻 residency/runtime 等条件引起的
		// 整体扰动。扩展头由 tungstenite 在删除完成后生成，单列在下方。
		HeaderMapInsertionOrder: []string{
			"host", "connection", "upgrade", "sec-websocket-version", "sec-websocket-key",
			"version", "x-codex-beta-features", "x-client-request-id", "session-id", "thread-id",
			"x-codex-window-id", "x-codex-turn-metadata", "x-codex-parent-thread-id",
			"x-openai-subagent", "x-openai-memgen-request", "openai-beta",
			"x-responsesapi-include-timing-metrics", "originator", "user-agent",
			"x-openai-internal-codex-residency", "authorization", "chatgpt-account-id",
			"x-openai-fedramp",
		},
		PostRemoveHeaders: []string{"sec-websocket-extensions"},
		Body: officialCodexBodyContract{
			Encoding: "websocket_json", Closed: true, Discriminator: "type=response.create",
			Fields: []officialCodexBodyField{
				bf("type", true, ""), bf("model", true, ""), bf("instructions", false, "empty_string"),
				bf("previous_response_id", false, "none_or_unreusable_prefix"), bf("input", true, ""),
				bf("tools", false, "none"), bf("tool_choice", true, ""), bf("parallel_tool_calls", true, ""),
				bf("reasoning", true, ""), bf("store", true, ""), bf("stream", true, ""),
				bf("stream_options", false, "none"), bf("include", true, ""), bf("service_tier", false, "none"),
				bf("prompt_cache_key", false, "none"), bf("text", false, "none"),
				bf("generate", false, "none"), bf("client_metadata", false, "none"),
			},
		},
	}
}

func codex0145CompactEndpoint() officialCodexEndpointProfile {
	beta := h(30, "x-codex-beta-features", "", officialCodexSourceFeature, officialCodexConditionBetaFeatures)
	beta.Sequence = 0
	beta.AlternateGroup = "compact-third-slot"
	turnState := h(30, "x-codex-turn-state", "", officialCodexSourceTurn, officialCodexConditionTurnState)
	turnState.Sequence = 1
	turnState.AlternateGroup = "compact-third-slot"
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointResponsesCompact, Method: http.MethodPost,
		TransportID: officialCodexTransportHTTPDefault, Host: "chatgpt.com",
		Path: "/backend-api/codex/responses/compact", Accept: "*/*", ContentType: "application/json",
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientPerUpperCall,
		HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap,
		Headers: []officialCodexHeaderSlot{
			h(10, "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
			h(20, "x-codex-installation-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			beta, turnState,
			h(40, "x-codex-window-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			h(50, "x-codex-turn-metadata", "", officialCodexSourceTurn, officialCodexConditionAlways),
			h(51, "x-openai-subagent", "", officialCodexSourceTurn, officialCodexConditionSubagent),
			h(52, "x-openai-memgen-request", "true", officialCodexSourceTurn, officialCodexConditionMemoryGeneration),
			h(53, "x-codex-parent-thread-id", "", officialCodexSourceSession, officialCodexConditionParentThread),
			h(60, "session-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			h(70, "thread-id", "", officialCodexSourceSession, officialCodexConditionAlways),
			h(80, "x-openai-internal-codex-responses-lite", "true", officialCodexSourceModelManifest, officialCodexConditionResponsesLite),
			h(90, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
			h(100, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
			h(105, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
			h(110, "content-type", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
			h(120, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
			h(130, "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(140, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(145, "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
			h(150, "cookie", "", officialCodexSourceSession, officialCodexConditionCookie),
			h(160, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
			h(170, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		},
		Body: bodyJSON(true,
			bf("model", true, ""), bf("input", true, ""), bf("instructions", false, "empty_string"),
			bf("tools", false, "none"), bf("parallel_tool_calls", true, ""), bf("reasoning", false, "none"),
			bf("service_tier", false, "none"), bf("prompt_cache_key", false, "none"), bf("text", false, "none"),
		),
	}
}

func codex0145SearchEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointAlphaSearch, Method: http.MethodPost,
		TransportID: officialCodexTransportHTTPDefault, Host: "chatgpt.com",
		Path: "/backend-api/codex/alpha/search", Accept: "*/*", ContentType: "application/json",
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientPerUpperCall,
		HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap,
		Headers: []officialCodexHeaderSlot{
			h(10, "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
			h(20, "x-codex-turn-metadata", "", officialCodexSourceTurn, officialCodexConditionAlways),
			h(30, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
			h(40, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
			h(45, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
			h(50, "content-type", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
			h(60, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
			h(70, "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(80, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(85, "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
			h(90, "cookie", "", officialCodexSourceSession, officialCodexConditionCookie),
			h(100, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
			h(110, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		},
		Body: bodyJSON(true,
			bf("id", true, ""), bf("model", true, ""), bf("input", true, ""),
			bf("commands", true, ""), bf("settings", true, ""), bf("max_output_tokens", true, ""),
		),
	}
}

func codex0145ImagesEndpoint(edit bool) officialCodexEndpointProfile {
	id := officialCodexEndpointImagesGenerations
	path := "/backend-api/codex/images/generations"
	fields := []officialCodexBodyField{
		bf("prompt", true, ""), bf("background", false, "none"), bf("model", true, ""),
		bf("quality", false, "none"), bf("size", false, "none"),
	}
	if edit {
		id = officialCodexEndpointImagesEdits
		path = "/backend-api/codex/images/edits"
		fields = append([]officialCodexBodyField{bf("images", true, "")}, fields...)
	}
	return officialCodexEndpointProfile{
		ID: id, Method: http.MethodPost, TransportID: officialCodexTransportHTTPDefault,
		Host: "chatgpt.com", Path: path, Accept: "*/*", ContentType: "application/json",
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientPerUpperCall,
		HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap,
		Headers:         standardCodexJSONHeaders(false), Body: bodyJSON(true, fields...),
	}
}

func codex0145RealtimeCallsEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointRealtimeCalls, Method: http.MethodPost,
		TransportID: officialCodexTransportHTTPDefault, Host: "chatgpt.com",
		Path: "/backend-api/codex/realtime/calls",
		Query: []officialCodexQueryField{
			{Name: "intent", Value: "quicksilver", Source: officialCodexSourceConstant, Required: true},
			{Name: "architecture", Value: "avas", Source: officialCodexSourceConstant, Required: true},
		},
		Accept: "*/*", ContentType: "application/json", Compression: officialCodexCompressionNone,
		ClientLifecycle: officialCodexClientPerUpperCall, HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap,
		Headers: []officialCodexHeaderSlot{
			h(10, "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
			h(20, "openai-alpha", "quicksilver=v1", officialCodexSourceConstant, officialCodexConditionAlways),
			h(30, "x-session-id", "", officialCodexSourceSession, officialCodexConditionSessionID),
			h(35, "x-oai-attestation", "", officialCodexSourceAuthentication, officialCodexConditionAttestation),
			h(40, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
			h(50, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
			h(55, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
			h(60, "content-type", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
			h(70, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
			h(80, "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(90, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(95, "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
			h(100, "cookie", "", officialCodexSourceSession, officialCodexConditionCookie),
			h(110, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
			h(120, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		},
		Body: bodyJSON(true, bf("sdp", true, ""), bf("session", true, "")),
	}
}

func codex0145RealtimeSidebandEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointRealtimeSideband, Method: http.MethodGet, Upgrade: "websocket",
		TransportID: officialCodexTransportWS, Host: "api.openai.com", Path: "/v1/realtime",
		Query: []officialCodexQueryField{
			{Name: "intent", Value: "quicksilver", Source: officialCodexSourceConstant, Required: true},
			{Name: "call_id", Source: officialCodexSourceServerResponse, Required: true},
		},
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientWebSocket,
		HeaderOrderMode: officialCodexHeaderOrderWSSwapRemove,
		Headers: []officialCodexHeaderSlot{
			wh(10, "host", "Host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
			wh(20, "connection", "Connection", "Upgrade", officialCodexSourceGenerated, officialCodexConditionAlways),
			wh(30, "upgrade", "Upgrade", "websocket", officialCodexSourceGenerated, officialCodexConditionAlways),
			wh(40, "sec-websocket-version", "Sec-WebSocket-Version", "13", officialCodexSourceGenerated, officialCodexConditionAlways),
			wh(50, "sec-websocket-key", "Sec-WebSocket-Key", "", officialCodexSourceGenerated, officialCodexConditionAuto),
			wh(60, "user-agent", "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
			wh(70, "originator", "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
			wh(75, "x-openai-internal-codex-residency", "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
			wh(80, "chatgpt-account-id", "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
			wh(85, "x-openai-fedramp", "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
			wh(90, "authorization", "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
			wh(100, "x-session-id", "x-session-id", "", officialCodexSourceSession, officialCodexConditionSessionID),
			wh(105, "x-oai-attestation", "x-oai-attestation", "", officialCodexSourceAuthentication, officialCodexConditionAttestation),
			wh(110, "version", "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
			wh(120, "openai-alpha", "openai-alpha", "quicksilver=v1", officialCodexSourceConstant, officialCodexConditionAlways),
		},
		// sideband 使用同一 tungstenite 删除算法，但默认不协商压缩，因此没有
		// 删除后追加项。顺序来自 realtime provider → extra/auth → default headers。
		HeaderMapInsertionOrder: []string{
			"host", "connection", "upgrade", "sec-websocket-version", "sec-websocket-key",
			"version", "openai-alpha", "x-session-id", "x-oai-attestation", "authorization",
			"chatgpt-account-id", "x-openai-fedramp", "originator", "user-agent",
			"x-openai-internal-codex-residency",
		},
		Body: officialCodexBodyContract{
			Encoding: "websocket_discriminated_events", Closed: false, Discriminator: "type",
			Fields: []officialCodexBodyField{bf("type", true, "")},
		},
	}
}

func codex0145WhamEndpoint(id, method, path string, consume bool) officialCodexEndpointProfile {
	headers := []officialCodexHeaderSlot{
		h(10, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
		h(20, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
		h(30, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
		h(32, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
		h(40, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
		h(50, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
	}
	body := bodyNone()
	contentType := ""
	if consume {
		contentType = "application/json"
		headers = append(headers,
			h(35, "content-type", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
			h(60, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		)
		body = bodyJSON(true,
			bf("redeem_request_id", true, ""),
			bfCond("credit_id", false, "none", officialCodexConditionCreditID),
		)
	}
	return officialCodexEndpointProfile{
		ID: id, Method: method, TransportID: officialCodexTransportHTTPDefault,
		Host: "chatgpt.com", Path: path, Accept: "*/*", ContentType: contentType,
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientBackendLongLived,
		HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap, Headers: headers, Body: body,
	}
}

func codex0145OAuthRefreshEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointOAuthRefresh, Method: http.MethodPost,
		TransportID: officialCodexTransportHTTPDefault, Host: "auth.openai.com", Path: "/oauth/token",
		Accept: "application/json", ContentType: "application/x-www-form-urlencoded",
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientPerUpperCall,
		HeaderOrderMode: officialCodexHeaderOrderExplicit,
		Headers: []officialCodexHeaderSlot{
			h(10, "content-type", "application/x-www-form-urlencoded", officialCodexSourceConstant, officialCodexConditionAlways),
			h(20, "accept", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
			h(30, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
			h(40, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
			h(50, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		},
		Body: officialCodexBodyContract{
			Encoding: "form_urlencoded", Closed: true,
			Fields: []officialCodexBodyField{
				bf("client_id", true, ""), bf("grant_type", true, ""),
				bf("refresh_token", true, ""), bf("scope", true, ""),
			},
		},
	}
}

func codex0145FilesCreateEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointFilesCreate, Method: http.MethodPost,
		TransportID: officialCodexTransportHTTPDefault, Host: "chatgpt.com", Path: "/backend-api/files",
		Accept: "*/*", ContentType: "application/json", Compression: officialCodexCompressionNone,
		ClientLifecycle: officialCodexClientPerUpperCall, HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap,
		Headers: codex0145FilesAuthorizedJSONHeaders(),
		Body:    bodyJSON(true, bf("file_name", true, ""), bf("file_size", true, ""), bf("use_case", true, "")),
	}
}

func codex0145FilesBlobUploadEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointFilesBlobUpload, Method: http.MethodPut,
		TransportID: officialCodexTransportHTTPDefault, Host: "*.oaiusercontent.com", HostFromResponse: true,
		Path:        "{server_returned_path}",
		Query:       []officialCodexQueryField{{Name: "*", Source: officialCodexSourceServerResponse, Required: true}},
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientReturnedUploadURL,
		HeaderOrderMode: officialCodexHeaderOrderExplicit,
		Accept:          "*/*",
		Headers: []officialCodexHeaderSlot{
			h(10, "x-ms-blob-type", "BlockBlob", officialCodexSourceConstant, officialCodexConditionAlways),
			h(20, "x-ms-client-request-id", "", officialCodexSourceGenerated, officialCodexConditionAlways),
			h(30, "content-length", "", officialCodexSourceRequestBody, officialCodexConditionAlways),
			h(40, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
			h(50, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		},
		Body: officialCodexBodyContract{Encoding: "raw_bytes", Closed: true},
	}
}

func codex0145FilesUploadedEndpoint() officialCodexEndpointProfile {
	return officialCodexEndpointProfile{
		ID: officialCodexEndpointFilesUploaded, Method: http.MethodPost,
		TransportID: officialCodexTransportHTTPDefault, Host: "chatgpt.com",
		Path: "/backend-api/files/{file_id}/uploaded", Accept: "*/*", ContentType: "application/json",
		Compression: officialCodexCompressionNone, ClientLifecycle: officialCodexClientPerUpperCall,
		HeaderOrderMode: officialCodexHeaderOrderH1HeaderMap, Headers: codex0145FilesAuthorizedJSONHeaders(),
		Body: bodyJSON(true),
	}
}

// codex0145FilesAuthorizedJSONHeaders 对应 FileClient 的 authorized_request +
// reqwest JSON 构造器。文件控制面不会经过通用 Codex API client 的默认头注入，
// 因而没有 version、originator、user-agent、cookie 或 residency。
func codex0145FilesAuthorizedJSONHeaders() []officialCodexHeaderSlot {
	return []officialCodexHeaderSlot{
		h(10, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
		h(20, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
		h(25, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
		h(30, "content-type", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
		h(40, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
		h(50, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		h(60, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
	}
}

func standardCodexJSONHeaders(includeTurnMetadata bool) []officialCodexHeaderSlot {
	headers := []officialCodexHeaderSlot{
		h(10, "version", officialCodexVersion0145, officialCodexSourceConstant, officialCodexConditionAlways),
	}
	if includeTurnMetadata {
		headers = append(headers, h(20, "x-codex-turn-metadata", "", officialCodexSourceTurn, officialCodexConditionAlways))
	}
	base := 30
	headers = append(headers,
		h(base, "authorization", "", officialCodexSourceAuthentication, officialCodexConditionAlways),
		h(base+10, "chatgpt-account-id", "", officialCodexSourceAccount, officialCodexConditionAlways),
		h(base+15, "x-openai-fedramp", "true", officialCodexSourceAccount, officialCodexConditionFedRAMP),
		h(base+20, "content-type", "application/json", officialCodexSourceConstant, officialCodexConditionAlways),
		h(base+30, "accept", "*/*", officialCodexSourceConstant, officialCodexConditionAlways),
		h(base+40, "originator", "", officialCodexSourceProcess, officialCodexConditionAlways),
		h(base+50, "user-agent", "", officialCodexSourceProcess, officialCodexConditionAlways),
		h(base+55, "x-openai-internal-codex-residency", "", officialCodexSourceManagedConfig, officialCodexConditionResidency),
		h(base+60, "cookie", "", officialCodexSourceSession, officialCodexConditionCookie),
		h(base+70, "host", "", officialCodexSourceGenerated, officialCodexConditionAuto),
		h(base+80, "content-length", "", officialCodexSourceGenerated, officialCodexConditionAuto),
	)
	return headers
}

func h(slot int, name, value, source, condition string) officialCodexHeaderSlot {
	return officialCodexHeaderSlot{
		Slot: slot, Name: name, WireName: strings.ToLower(name), Value: value,
		Source: source, Condition: condition,
	}
}

func wh(slot int, name, wireName, value, source, condition string) officialCodexHeaderSlot {
	return officialCodexHeaderSlot{
		Slot: slot, Name: name, WireName: wireName, Value: value,
		Source: source, Condition: condition,
	}
}

func bf(name string, required bool, omitWhen string) officialCodexBodyField {
	return officialCodexBodyField{Name: name, Required: required, OmitWhen: omitWhen}
}

func bfCond(name string, required bool, omitWhen, condition string) officialCodexBodyField {
	return officialCodexBodyField{Name: name, Required: required, OmitWhen: omitWhen, Condition: condition}
}

func bodyJSON(closed bool, fields ...officialCodexBodyField) officialCodexBodyContract {
	return officialCodexBodyContract{Encoding: "json", Closed: closed, Fields: fields}
}

func bodyNone() officialCodexBodyContract {
	return officialCodexBodyContract{Encoding: "none", Closed: true}
}

func digestOfficialCodexVersionProfile(profile officialCodexVersionProfile) (string, error) {
	profile.Digest = ""
	encoded, err := json.Marshal(profile)
	if err != nil {
		return "", fmt.Errorf("编码 Codex 版本画像摘要：%w", err)
	}
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:]), nil
}

func validateOfficialCodexVersionProfile(profile officialCodexVersionProfile) error {
	if profile.Version != officialCodexVersion0145 {
		return fmt.Errorf("版本必须精确为 %s", officialCodexVersion0145)
	}
	if err := validateCodex0145RequiredRules(profile.RequiredRules); err != nil {
		return err
	}
	if err := validateCodex0145Surfaces(profile.Surfaces); err != nil {
		return err
	}
	if err := validateCodex0145FeatureDefaults(profile.FeatureDefaults); err != nil {
		return err
	}
	if err := validateCodex0145ToolPresentation(profile.ToolPresentation); err != nil {
		return err
	}
	if err := validateCodex0145Subagents(profile.Subagents); err != nil {
		return err
	}
	if err := validateCodex0145FilesProfile(profile.Files); err != nil {
		return err
	}
	transportIDs, err := validateCodex0145Transports(profile.Transports)
	if err != nil {
		return err
	}
	if err := validateCodex0145Endpoints(profile.Endpoints, transportIDs); err != nil {
		return err
	}
	return nil
}

func validateCodex0145ToolPresentation(tools officialCodexToolPresentationProfile) error {
	expected := officialCodex0145ToolPresentation()
	endpointIDsMatch := officialCodex0145StringSliceEqual(tools.EndpointIDs, expected.EndpointIDs)
	namespaceFieldsMatch := officialCodex0145StringSliceEqual(
		tools.NamespaceRequiredFields,
		expected.NamespaceRequiredFields,
	)
	functionFieldsMatch := officialCodex0145StringSliceEqual(
		tools.FunctionRequiredFields,
		expected.FunctionRequiredFields,
	)
	if tools.HostedImageGenerationAllowed != expected.HostedImageGenerationAllowed ||
		tools.HostedImageGenerationType != expected.HostedImageGenerationType ||
		tools.NamespaceType != expected.NamespaceType ||
		tools.NamespaceName != expected.NamespaceName ||
		tools.FunctionType != expected.FunctionType ||
		tools.FunctionName != expected.FunctionName ||
		tools.LiteCarrierItemType != expected.LiteCarrierItemType || !endpointIDsMatch ||
		!namespaceFieldsMatch || !functionFieldsMatch {
		return fmt.Errorf("Codex 0.145.0 工具呈现画像不符合版本规格：%+v", tools)
	}
	return nil
}

func officialCodex0145StringSliceEqual(left, right []string) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range right {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}

func validateCodex0145Subagents(subagents officialCodexSubagentProfile) error {
	expected := officialCodex0145Subagents()
	mappingsMatch := len(subagents.Mappings) == len(expected.Mappings)
	if mappingsMatch {
		for index := range expected.Mappings {
			if subagents.Mappings[index] != expected.Mappings[index] {
				mappingsMatch = false
				break
			}
		}
	}
	if !mappingsMatch || subagents.OtherLabelAllowed != expected.OtherLabelAllowed ||
		subagents.OtherThreadSource != expected.OtherThreadSource ||
		subagents.OtherHeaderEqualsKind != expected.OtherHeaderEqualsKind {
		return fmt.Errorf("Codex 0.145.0 subagent 画像不符合版本规格：%+v", subagents)
	}
	return nil
}

func validateCodex0145FilesProfile(files officialCodexFilesProfile) error {
	expected := officialCodex0145FilesProfile()
	if files != expected {
		return fmt.Errorf("Codex 0.145.0 Files 流程画像不符合版本规格：%+v", files)
	}
	return nil
}

func validateCodex0145RequiredRules(rules []string) error {
	expected := stringSet(officialCodex0145RequiredRules())
	if len(rules) != 42 {
		return fmt.Errorf("Codex 0.145.0 必须包含 42 条规则，实际为 %d", len(rules))
	}
	seen := make(map[string]struct{}, len(rules))
	for _, rule := range rules {
		if _, duplicate := seen[rule]; duplicate {
			return fmt.Errorf("Codex 0.145.0 规则重复：%s", rule)
		}
		if _, ok := expected[rule]; !ok {
			return fmt.Errorf("Codex 0.145.0 包含未知规则：%s", rule)
		}
		seen[rule] = struct{}{}
	}
	for rule := range expected {
		if _, ok := seen[rule]; !ok {
			return fmt.Errorf("Codex 0.145.0 缺少规则：%s", rule)
		}
	}
	return nil
}

func validateCodex0145Surfaces(surfaces []officialCodexSurfaceProfile) error {
	expected := stringSet([]string{officialCodexSurfaceExec, officialCodexSurfaceTUI})
	if len(surfaces) != len(expected) {
		return fmt.Errorf("Codex 0.145.0 入口画像数量错误：%d", len(surfaces))
	}
	seen := make(map[string]struct{}, len(surfaces))
	for _, surface := range surfaces {
		if _, ok := expected[surface.ID]; !ok {
			return fmt.Errorf("未知 Codex 入口画像：%q", surface.ID)
		}
		if _, duplicate := seen[surface.ID]; duplicate {
			return fmt.Errorf("Codex 入口画像重复：%s", surface.ID)
		}
		seen[surface.ID] = struct{}{}
		if surface.Product == "" || surface.Version != officialCodexVersion0145 || surface.PlatformPrefix == "" ||
			surface.DefaultTerminalToken != "unknown" || surface.TerminalTokenPattern != `^[A-Za-z0-9._/-]+$` ||
			surface.SuffixName == "" || surface.SuffixVersion != officialCodexVersion0145 || !surface.SuffixOptional ||
			!surface.InitialModelsMayOmit || surface.Originator == "" || surface.InitialModelsOriginator != "codex_cli_rs" {
			return fmt.Errorf("Codex 入口画像不完整：%s", surface.ID)
		}
	}
	return nil
}

func validateCodex0145FeatureDefaults(features officialCodexFeatureDefaults) error {
	if !features.SupportsWebSockets || !features.RemoteCompactionV2 || !features.EnableRequestCompression ||
		features.RequestCompressionLevel != 3 || features.RuntimeMetrics || features.ForceHTTPFallback ||
		!features.ResponsesLiteFromModelManifest || !features.ParallelToolsFromModelManifest {
		return errors.New("Codex 0.145.0 feature 默认值不符合版本规格")
	}
	return nil
}

func validateCodex0145Transports(transports []officialCodexTransportProfile) (map[string]struct{}, error) {
	expected := stringSet([]string{officialCodexTransportHTTPDefault, officialCodexTransportWS})
	if len(transports) != len(expected) {
		return nil, fmt.Errorf("Codex 0.145.0 传输画像数量错误：%d", len(transports))
	}
	seen := make(map[string]struct{}, len(transports))
	for _, transport := range transports {
		if _, ok := expected[transport.ID]; !ok {
			return nil, fmt.Errorf("未知 Codex 传输画像：%q", transport.ID)
		}
		if _, duplicate := seen[transport.ID]; duplicate {
			return nil, fmt.Errorf("Codex 传输画像重复：%s", transport.ID)
		}
		seen[transport.ID] = struct{}{}
		if transport.Protocol == "" || transport.PlatformCondition == "" || transport.TLSStack == "" ||
			len(transport.CipherSuites) == 0 || len(transport.Extensions) == 0 ||
			len(transport.SupportedVersions) == 0 || transport.TLSMinVersion == 0 || transport.TLSMaxVersion == 0 {
			return nil, fmt.Errorf("Codex 传输画像不完整：%s", transport.ID)
		}
		if hasDuplicateUint16(transport.CipherSuites) || hasDuplicateUint16(transport.Extensions) ||
			hasDuplicateUint16(transport.SupportedGroups) {
			return nil, fmt.Errorf("Codex 传输画像包含重复 TLS 项：%s", transport.ID)
		}
		if transport.ID == officialCodexTransportHTTPDefault {
			if len(transport.CipherSuites) != 30 || len(transport.ALPN) != 0 || !transport.LowercaseHTTPHeaders ||
				transport.CrossCallConnectionReuse || !transport.RetryReusesClient || transport.WebSocket != nil {
				return nil, errors.New("Codex 0.145.0 默认 HTTP 传输参数不完整")
			}
		}
		if transport.ID == officialCodexTransportWS {
			if len(transport.CipherSuites) != 10 || !transport.RandomizeExtensions || transport.WebSocket == nil ||
				len(transport.WebSocket.FixedHandshakePrefix) != 5 || !transport.WebSocket.CompressedTextRSV1 ||
				!transport.WebSocket.RawDeflatePayload || !transport.WebSocket.ContextTakeover {
				return nil, errors.New("Codex 0.145.0 WS 传输参数不完整")
			}
		}
	}
	return seen, nil
}

func validateCodex0145Endpoints(endpoints []officialCodexEndpointProfile, transportIDs map[string]struct{}) error {
	expected := stringSet(officialCodex0145RequiredEndpointIDs())
	if len(endpoints) != len(expected) {
		return fmt.Errorf("Codex 0.145.0 端点画像数量错误：%d", len(endpoints))
	}
	seen := make(map[string]struct{}, len(endpoints))
	for _, endpoint := range endpoints {
		if _, ok := expected[endpoint.ID]; !ok {
			return fmt.Errorf("未知 Codex 端点画像：%q", endpoint.ID)
		}
		if _, duplicate := seen[endpoint.ID]; duplicate {
			return fmt.Errorf("Codex 端点画像重复：%s", endpoint.ID)
		}
		seen[endpoint.ID] = struct{}{}
		if endpoint.Method == "" || endpoint.Host == "" || endpoint.Path == "" || endpoint.TransportID == "" ||
			endpoint.ClientLifecycle == "" || endpoint.HeaderOrderMode == "" || endpoint.Compression == "" {
			return fmt.Errorf("Codex 端点画像不完整：%s", endpoint.ID)
		}
		if _, ok := transportIDs[endpoint.TransportID]; !ok {
			return fmt.Errorf("Codex 端点 %s 引用未知传输 %s", endpoint.ID, endpoint.TransportID)
		}
		if err := validateCodex0145Query(endpoint); err != nil {
			return err
		}
		if err := validateCodex0145Headers(endpoint); err != nil {
			return err
		}
		if err := validateCodex0145Body(endpoint); err != nil {
			return err
		}
	}
	for endpointID := range expected {
		if _, ok := seen[endpointID]; !ok {
			return fmt.Errorf("Codex 0.145.0 缺少端点画像：%s", endpointID)
		}
	}
	return nil
}

func validateCodex0145Query(endpoint officialCodexEndpointProfile) error {
	seen := make(map[string]struct{}, len(endpoint.Query))
	for _, field := range endpoint.Query {
		if field.Name == "" || field.Source == "" {
			return fmt.Errorf("Codex 端点 %s 包含不完整 query", endpoint.ID)
		}
		if _, duplicate := seen[field.Name]; duplicate {
			return fmt.Errorf("Codex 端点 %s 的 query 重复：%s", endpoint.ID, field.Name)
		}
		seen[field.Name] = struct{}{}
	}
	return nil
}

func validateCodex0145Headers(endpoint officialCodexEndpointProfile) error {
	if len(endpoint.Headers) == 0 {
		return fmt.Errorf("Codex 端点 %s 缺少 header 画像", endpoint.ID)
	}
	seenNames := make(map[string]struct{}, len(endpoint.Headers))
	seenPositions := make(map[string]struct{}, len(endpoint.Headers))
	for _, header := range endpoint.Headers {
		if header.Slot <= 0 || header.Name == "" || header.WireName == "" || header.Source == "" || header.Condition == "" {
			return fmt.Errorf("Codex 端点 %s 包含不完整 header 槽位", endpoint.ID)
		}
		name := strings.ToLower(header.Name)
		if _, duplicate := seenNames[name]; duplicate {
			return fmt.Errorf("Codex 端点 %s 的 header 重复：%s", endpoint.ID, header.Name)
		}
		seenNames[name] = struct{}{}
		position := fmt.Sprintf("%d/%d", header.Slot, header.Sequence)
		if _, duplicate := seenPositions[position]; duplicate {
			return fmt.Errorf("Codex 端点 %s 的 header 槽位重复：%s", endpoint.ID, position)
		}
		seenPositions[position] = struct{}{}
		if endpoint.Upgrade == "" && header.WireName != strings.ToLower(header.WireName) {
			return fmt.Errorf("普通 HTTP 端点 %s 的 header 必须小写：%s", endpoint.ID, header.WireName)
		}
	}
	if endpoint.Upgrade == "" {
		if len(endpoint.HeaderMapInsertionOrder) != 0 || len(endpoint.PostRemoveHeaders) != 0 {
			return fmt.Errorf("普通 HTTP 端点 %s 不得声明 WS HeaderMap 构造序", endpoint.ID)
		}
		return nil
	}
	if endpoint.HeaderOrderMode != officialCodexHeaderOrderWSSwapRemove {
		return fmt.Errorf("Codex WS 端点 %s 未使用 swap_remove 画像", endpoint.ID)
	}
	desired := make([]string, 0, len(endpoint.Headers))
	for _, header := range endpoint.Headers {
		desired = append(desired, strings.ToLower(header.Name))
	}
	prefix := []string{"host", "connection", "upgrade", "sec-websocket-version", "sec-websocket-key"}
	if _, _, err := officialCodex0145CompileWSHeaderConstruction(endpoint, desired, prefix); err != nil {
		return err
	}
	return nil
}

func validateCodex0145Body(endpoint officialCodexEndpointProfile) error {
	body := endpoint.Body
	if body.Encoding == "" {
		return fmt.Errorf("Codex 端点 %s 缺少 body 编码", endpoint.ID)
	}
	seen := make(map[string]struct{}, len(body.Fields))
	for _, field := range body.Fields {
		if field.Name == "" {
			return fmt.Errorf("Codex 端点 %s 包含空 body 字段", endpoint.ID)
		}
		if _, duplicate := seen[field.Name]; duplicate {
			return fmt.Errorf("Codex 端点 %s 的 body 字段重复：%s", endpoint.ID, field.Name)
		}
		seen[field.Name] = struct{}{}
	}
	if body.Encoding == "none" && len(body.Fields) != 0 {
		return fmt.Errorf("Codex 端点 %s 的无 body 契约包含字段", endpoint.ID)
	}
	return nil
}

func officialCodex0145RequiredEndpointIDs() []string {
	return []string{
		officialCodexEndpointModels,
		officialCodexEndpointResponsesHTTP,
		officialCodexEndpointResponsesWS,
		officialCodexEndpointResponsesCompact,
		officialCodexEndpointAlphaSearch,
		officialCodexEndpointImagesGenerations,
		officialCodexEndpointImagesEdits,
		officialCodexEndpointRealtimeCalls,
		officialCodexEndpointRealtimeSideband,
		officialCodexEndpointWhamUsage,
		officialCodexEndpointWhamResetCredits,
		officialCodexEndpointWhamConsumeResetCredit,
		officialCodexEndpointOAuthRefresh,
		officialCodexEndpointFilesCreate,
		officialCodexEndpointFilesBlobUpload,
		officialCodexEndpointFilesUploaded,
	}
}

func stringSet(values []string) map[string]struct{} {
	result := make(map[string]struct{}, len(values))
	for _, value := range values {
		result[value] = struct{}{}
	}
	return result
}

func hasDuplicateUint16(values []uint16) bool {
	seen := make(map[uint16]struct{}, len(values))
	for _, value := range values {
		if _, duplicate := seen[value]; duplicate {
			return true
		}
		seen[value] = struct{}{}
	}
	return false
}
