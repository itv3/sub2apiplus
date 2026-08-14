package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"regexp"
	"sort"
	"strings"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

const officialCodexProfileVersion = officialCodexVersion0145

const (
	officialCodexVersion0145 = "0.145.0"

	officialCodexSurfaceExec = "exec"
	officialCodexSurfaceTUI  = "tui"

	officialCodexTransportHTTPDefault   = "codex-0.145.0-http-ubuntu24-native"
	officialCodexTransportHTTPLongLived = "codex-0.145.0-http-ubuntu24-native-long-lived"
	officialCodexTransportWS            = "codex-0.145.0-ws-rustls"

	// 协议标识不含版本号，是跨版本稳定的语义键；执行层按它查传输画像，
	// 而不是写死带版本号的传输 ID。
	officialCodexTransportProtocolHTTP1 = "http/1.1"
	officialCodexTransportProtocolWS    = "websocket"

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
	officialCodexEndpointWhamSettingsUser       = "wham_settings_user"
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

// codexEndpointID 是 0.145.0 画像允许的端点标识类型。
// 动态字符串必须显式转换后才能进入解析器，避免调用方误把路径或别名当作端点 ID。
type codexEndpointID string

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
	ID                   string
	Protocol             string
	PlatformCondition    string
	TLSStack             string
	CipherSuites         []uint16
	SupportedGroups      []uint16
	SignatureAlgorithms  []uint16
	ALPN                 []string
	Extensions           []uint16
	RandomizeExtensions  bool
	SupportedVersions    []uint16
	KeyShareGroups       []uint16
	PSKModes             []uint16
	TLSMinVersion        uint16
	TLSMaxVersion        uint16
	LowercaseHTTPHeaders bool
	WebSocket            *officialCodexWebSocketTransportProfile
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

var officialCodexFormalProfileCache sync.Map

// resolveCodexVersionProfile 只接受精确三段版本，不做 trim、别名或回退；
// 未登记的版本按未知处理，不回退到任何既有快照。
//
// 返回的是进程内只读单例，调用方不得修改其任何字段。
func resolveCodexVersionProfile(version string) (*officialCodexVersionProfile, error) {
	var selected officialegress.ResolvedCodexRelease
	found := false
	for _, mode := range []officialegress.ReleaseMode{
		officialegress.ReleaseModeActive,
		officialegress.ReleaseModePrevious,
	} {
		release, err := officialegress.DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			return nil, err
		}
		if release.Version() != version {
			continue
		}
		if found && selected.ProfileDigest() != release.ProfileDigest() {
			return nil, fmt.Errorf("版本 %q 对应多个不可互换的 ProfileDigest", version)
		}
		selected = release
		found = true
	}
	if !found {
		return nil, fmt.Errorf("未知 Codex 官方出站版本画像：%q", version)
	}
	return resolveOfficialCodexReleaseProfile(selected)
}

func resolveOfficialCodexReleaseProfile(
	release officialegress.ResolvedCodexRelease,
) (*officialCodexVersionProfile, error) {
	executable := release.ExecutableProfile()
	if cached, ok := officialCodexFormalProfileCache.Load(executable.Digest()); ok {
		profile, valid := cached.(*officialCodexVersionProfile)
		if !valid {
			return nil, errors.New("service 正式版本画像缓存类型非法")
		}
		return profile, nil
	}
	profile := projectExecutableCodexProfile(executable)
	if profile.Digest != release.ExecutableProfileDigest() {
		return nil, errors.New("service 投影与正式 ExecutableProfileDigest 不一致")
	}
	actual, _ := officialCodexFormalProfileCache.LoadOrStore(executable.Digest(), &profile)
	cachedProfile, valid := actual.(*officialCodexVersionProfile)
	if !valid {
		return nil, errors.New("service 正式版本画像缓存类型非法")
	}
	return cachedProfile, nil
}

// projectExecutableCodexProfile 是旧 service 数据形状的兼容投影。它只从启动期已验证的
// ExecutableProfile 构造，不再序列化 ProfileSpec/RawJSON；RequiredRules 因而保持为空。
func projectExecutableCodexProfile(
	executable profilecontract.ExecutableProfile,
) officialCodexVersionProfile {
	features := executable.Features()
	tool := executable.ToolPresentation()
	subagents := executable.Subagents()
	files := executable.Files()
	profile := officialCodexVersionProfile{
		Version: executable.Version(), Digest: executable.Digest(),
		FeatureDefaults: officialCodexFeatureDefaults{
			SupportsWebSockets:             features.SupportsWebSockets,
			RemoteCompactionV2:             features.RemoteCompactionV2,
			EnableRequestCompression:       features.EnableRequestCompression,
			RequestCompressionLevel:        features.RequestCompressionLevel,
			RuntimeMetrics:                 features.RuntimeMetrics,
			ForceHTTPFallback:              features.ForceHTTPFallback,
			ResponsesLiteFromModelManifest: features.ResponsesLiteFromModelManifest,
			ParallelToolsFromModelManifest: features.ParallelToolsFromModelManifest,
		},
		ToolPresentation: officialCodexToolPresentationProfile{
			EndpointIDs:                  append([]string(nil), tool.EndpointIDs...),
			HostedImageGenerationAllowed: tool.HostedImageGenerationAllowed,
			HostedImageGenerationType:    tool.HostedImageGenerationType,
			NamespaceType:                tool.NamespaceType, NamespaceName: tool.NamespaceName,
			FunctionType: tool.FunctionType, FunctionName: tool.FunctionName,
			LiteCarrierItemType:     tool.LiteCarrierItemType,
			NamespaceRequiredFields: append([]string(nil), tool.NamespaceRequiredFields...),
			FunctionRequiredFields:  append([]string(nil), tool.FunctionRequiredFields...),
		},
		Subagents: officialCodexSubagentProfile{
			OtherLabelAllowed:     subagents.OtherLabelAllowed,
			OtherThreadSource:     subagents.OtherThreadSource,
			OtherHeaderEqualsKind: subagents.OtherHeaderEqualsKind,
		},
		Files: officialCodexFilesProfile{
			CreateEndpointID:         files.CreateEndpointID,
			BlobUploadEndpointID:     files.BlobUploadEndpointID,
			UploadedEndpointID:       files.UploadedEndpointID,
			UploadLimitBytes:         files.UploadLimitBytes,
			RequestTimeoutMillis:     files.RequestTimeoutMillis,
			FinalizeTimeoutMillis:    files.FinalizeTimeoutMillis,
			FinalizeRetryDelayMillis: files.FinalizeRetryDelayMillis,
			UseCase:                  files.UseCase, URIPrefix: files.URIPrefix,
			FinalizeSuccessStatus: files.FinalizeSuccessStatus,
			FinalizeRetryStatus:   files.FinalizeRetryStatus,
		},
	}
	for _, surface := range executable.Surfaces() {
		profile.Surfaces = append(profile.Surfaces, officialCodexSurfaceProfile{
			ID: surface.ID, Product: surface.Product, Version: surface.Version,
			PlatformPrefix:       surface.PlatformPrefix,
			DefaultTerminalToken: surface.DefaultTerminalToken,
			TerminalTokenPattern: surface.TerminalTokenPattern,
			SuffixName:           surface.SuffixName, SuffixVersion: surface.SuffixVersion,
			SuffixOptional:          surface.SuffixOptional,
			InitialModelsMayOmit:    surface.InitialModelsMayOmit,
			Originator:              surface.Originator,
			InitialModelsOriginator: surface.InitialModelsOriginator,
		})
	}
	for _, mapping := range subagents.Mappings {
		profile.Subagents.Mappings = append(profile.Subagents.Mappings, officialCodexSubagentMapping{
			ID: mapping.ID, HeaderValue: mapping.HeaderValue, MetadataKind: mapping.MetadataKind,
			ThreadSource: mapping.ThreadSource, MemoryGeneration: mapping.MemoryGeneration,
			ParentThreadRequired: mapping.ParentThreadRequired,
		})
	}
	for _, endpoint := range executable.Endpoints() {
		projected := officialCodexEndpointProfile{
			ID: endpoint.ID, Method: endpoint.Method, Upgrade: endpoint.Upgrade,
			TransportID: endpoint.TransportID, Host: endpoint.Host,
			HostFromResponse: endpoint.HostFromResponse, Path: endpoint.Path,
			Accept: endpoint.Accept, ContentType: endpoint.ContentType,
			Compression:             string(endpoint.Compression),
			ClientLifecycle:         string(endpoint.ResourceLifecycle.Lifecycle),
			HeaderOrderMode:         string(endpoint.HeaderOrderMode),
			HeaderMapInsertionOrder: append([]string(nil), endpoint.HeaderMapInsertionOrder...),
			PostRemoveHeaders:       append([]string(nil), endpoint.PostRemoveHeaders...),
			Body: officialCodexBodyContract{
				Encoding: string(endpoint.Body.Encoding), Closed: endpoint.Body.Closed,
				Discriminator: endpoint.Body.Discriminator,
			},
		}
		for _, query := range endpoint.Query {
			projected.Query = append(projected.Query, officialCodexQueryField{
				Name: query.Name, Value: query.Value, Source: string(query.Source), Required: query.Required,
			})
		}
		for _, header := range endpoint.Headers {
			projected.Headers = append(projected.Headers, officialCodexHeaderSlot{
				Slot: header.Slot, Sequence: header.Sequence, Name: header.Name,
				WireName: header.WireName, Value: header.Value, Source: string(header.Source),
				Condition: string(header.Condition), AlternateGroup: header.AlternateGroup,
			})
		}
		for _, field := range endpoint.Body.Fields {
			projected.Body.Fields = append(projected.Body.Fields, officialCodexBodyField{
				Name: field.Name, Required: field.Required,
				OmitWhen: string(field.OmitWhen), Condition: string(field.Condition),
			})
		}
		profile.Endpoints = append(profile.Endpoints, projected)
	}
	for _, transport := range executable.Transports() {
		projected := officialCodexTransportProfile{
			ID: transport.ID, Protocol: transport.Protocol,
			PlatformCondition: transport.PlatformCondition, TLSStack: transport.TLSStack,
			CipherSuites:        append([]uint16(nil), transport.CipherSuites...),
			SupportedGroups:     append([]uint16(nil), transport.SupportedGroups...),
			SignatureAlgorithms: append([]uint16(nil), transport.SignatureAlgorithms...),
			ALPN:                append([]string(nil), transport.ALPN...),
			Extensions:          append([]uint16(nil), transport.Extensions...),
			RandomizeExtensions: transport.RandomizeExtensions,
			SupportedVersions:   append([]uint16(nil), transport.SupportedVersions...),
			KeyShareGroups:      append([]uint16(nil), transport.KeyShareGroups...),
			PSKModes:            append([]uint16(nil), transport.PSKModes...),
			TLSMinVersion:       transport.TLSMinVersion, TLSMaxVersion: transport.TLSMaxVersion,
			LowercaseHTTPHeaders: transport.LowercaseHTTPHeaders,
		}
		if transport.WebSocket != nil {
			projected.WebSocket = &officialCodexWebSocketTransportProfile{
				FixedHandshakePrefix: append([]string(nil), transport.WebSocket.FixedHandshakePrefix...),
				RemainingHeaderMode:  transport.WebSocket.RemainingHeaderMode,
				CompressionOffer:     transport.WebSocket.CompressionOffer,
				CompressedTextRSV1:   transport.WebSocket.CompressedTextRSV1,
				RawDeflatePayload:    transport.WebSocket.RawDeflatePayload,
				ContextTakeover:      transport.WebSocket.ContextTakeover,
			}
		}
		profile.Transports = append(profile.Transports, projected)
	}
	return profile
}

// cloneOfficialCodexVersionProfile 返回整份版本画像的深拷贝。解析路径交付的是
// 只读单例，因此任何需要改写画像的场景都必须先经过这里，避免污染全局快照。
func cloneOfficialCodexVersionProfile(
	profile *officialCodexVersionProfile,
) (officialCodexVersionProfile, error) {
	if profile == nil {
		return officialCodexVersionProfile{}, errors.New("Codex 版本画像为空")
	}
	encoded, err := json.Marshal(profile)
	if err != nil {
		return officialCodexVersionProfile{}, fmt.Errorf("复制 Codex %s 版本画像：%w", profile.Version, err)
	}
	var cloned officialCodexVersionProfile
	if err := json.Unmarshal(encoded, &cloned); err != nil {
		return officialCodexVersionProfile{}, fmt.Errorf("复制 Codex %s 版本画像：%w", profile.Version, err)
	}
	return cloned, nil
}

// resolveCodexEndpoint 同时执行精确版本和精确端点解析，并返回端点深拷贝。
func resolveCodexEndpoint(version string, endpointID codexEndpointID) (officialCodexEndpointProfile, error) {
	profile, err := resolveCodexVersionProfile(version)
	if err != nil {
		return officialCodexEndpointProfile{}, err
	}
	return profile.ResolveEndpoint(string(endpointID))
}

// resolveOfficialCodexVersionProfile 保留旧接入名，统一委托给严格版本解析器。
func resolveOfficialCodexVersionProfile(version string) (*officialCodexVersionProfile, error) {
	return resolveCodexVersionProfile(version)
}

// resolveOfficialCodexTransportTLSProfileByID 是传输层使用的版本中立接缝。
// 具体版本解析只保留在画像实现内，调用方提交启动期已编译的精确 transport ID。
func resolveOfficialCodexTransportTLSProfileByID(
	version string,
	transportID string,
) (*tlsfingerprint.Profile, error) {
	return officialCodexResolveTLSProfile(version, transportID)
}

// OfficialCodexRemoteCompactionV2Default 把 handler 的压缩分派决策绑定到版本画像，
// 避免再次把“header 是否出现”误当成 feature 默认值。显式 legacy 请求仍通过
// /responses/compact 选择；普通 /responses 中已有 compaction_trigger 时保持 V2。
//
// feature 默认值必须与 header、TLS、端点出自同一发布模式：它决定 handler 的压缩分派与
// HTTP turn metadata，若自行选择 active，切换 previous 后就会出现跨发布混搭。
// mode 必须来自进程运行时或调用级 OfficialEgressContext；空 mode 不允许退化为 active。
func OfficialCodexRemoteCompactionV2Default(mode string) bool {
	enabled, err := resolveOfficialCodexRemoteCompactionV2Default(
		mode,
		resolveCodexVersionProfileForMode,
	)
	if err != nil {
		panic(err)
	}
	return enabled
}

type officialCodexModeProfileResolver func(string) (*officialCodexVersionProfile, error)

func resolveOfficialCodexRemoteCompactionV2Default(
	mode string,
	resolver officialCodexModeProfileResolver,
) (bool, error) {
	mode = strings.TrimSpace(mode)
	if mode == "" {
		return false, errors.New("Codex compaction feature 缺少冻结 release mode")
	}
	if resolver == nil {
		return false, errors.New("Codex compaction feature 缺少版本画像解析器")
	}
	profile, err := resolver(mode)
	if err != nil {
		return false, err
	}
	return profile.FeatureDefaults.RemoteCompactionV2, nil
}

// ResolveEndpoint 返回端点画像的深拷贝，未知端点或带空白的近似 ID 均失败。
// 版本画像本身是只读单例，端点则是执行器会就地改写的数据，因此这里必须拷贝。
// 拷贝按结构逐字段进行，不再经过 JSON 编解码。
func (p *officialCodexVersionProfile) ResolveEndpoint(endpointID string) (officialCodexEndpointProfile, error) {
	if p == nil {
		return officialCodexEndpointProfile{}, errors.New("Codex 版本画像为空")
	}
	for i := range p.Endpoints {
		if p.Endpoints[i].ID != endpointID {
			continue
		}
		return cloneOfficialCodexEndpointProfile(p.Endpoints[i]), nil
	}
	return officialCodexEndpointProfile{}, fmt.Errorf("Codex %s 不支持端点画像：%q", p.Version, endpointID)
}

// ResolveDefaultTransportID 按协议返回该版本的请求级默认传输画像 ID。
//
// 传输 ID 自身携带版本号（如 codex-0.145.0-http-ubuntu24-native），调用方因此不能
// 写死传输 ID 常量：升级后新快照会定义自己的传输 ID，写死的常量在新画像里根本查不到。
// HTTP 长期 client 与请求级 client 可以具有相同 TLS 参数，但资源语义不同；默认入口
// 只从 per_upper_api_call 端点使用的 transport 中选取，不能因协议相同而混用。
func (p *officialCodexVersionProfile) ResolveDefaultTransportID(protocol string) (string, error) {
	if p == nil {
		return "", errors.New("Codex 版本画像为空")
	}
	transportProtocols := make(map[string]string, len(p.Transports))
	for _, transport := range p.Transports {
		transportProtocols[transport.ID] = transport.Protocol
	}
	wantLifecycle := officialCodexClientPerUpperCall
	if protocol == officialCodexTransportProtocolWS {
		wantLifecycle = officialCodexClientWebSocket
	}
	resolved := ""
	for _, endpoint := range p.Endpoints {
		if endpoint.ClientLifecycle != wantLifecycle || transportProtocols[endpoint.TransportID] != protocol {
			continue
		}
		if resolved != "" && resolved != endpoint.TransportID {
			return "", fmt.Errorf(
				"Codex %s 的协议 %q 存在多个请求级默认传输画像",
				p.Version,
				protocol,
			)
		}
		resolved = endpoint.TransportID
	}
	if resolved == "" {
		return "", fmt.Errorf("Codex %s 没有协议为 %q 的请求级默认传输画像", p.Version, protocol)
	}
	return resolved, nil
}

// cloneOfficialCodexEndpointProfile 复制端点画像及其全部切片。端点内只有值类型
// 元素的切片，没有嵌套指针，因此逐切片复制即构成完整深拷贝。
func cloneOfficialCodexEndpointProfile(
	endpoint officialCodexEndpointProfile,
) officialCodexEndpointProfile {
	cloned := endpoint
	cloned.Query = cloneOfficialCodexSlice(endpoint.Query)
	cloned.Headers = cloneOfficialCodexSlice(endpoint.Headers)
	cloned.HeaderMapInsertionOrder = cloneOfficialCodexSlice(endpoint.HeaderMapInsertionOrder)
	cloned.PostRemoveHeaders = cloneOfficialCodexSlice(endpoint.PostRemoveHeaders)
	cloned.Body.Fields = cloneOfficialCodexSlice(endpoint.Body.Fields)
	return cloned
}

// cloneOfficialCodexSlice 保持 nil 与空切片的区别，使结构化拷贝与原先的 JSON
// 往返拷贝结果完全一致。
func cloneOfficialCodexSlice[T any](src []T) []T {
	if src == nil {
		return nil
	}
	dst := make([]T, len(src))
	copy(dst, src)
	return dst
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

func digestOfficialCodexVersionProfile(profile officialCodexVersionProfile) (string, error) {
	profile.Digest = ""
	encoded, err := json.Marshal(profile)
	if err != nil {
		return "", fmt.Errorf("编码 Codex 版本画像摘要：%w", err)
	}
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:]), nil
}
