package profilecontract

// ProfileSpec：官方版本证据的不可变承载。
//
// # 边界
//
// 本文件**只**承载画像里实际存在的数据。它不含、也不允许含：
//
//	purpose        —— 一个 endpoint 对应多个 purpose，画像里没有这个维度
//	backend        —— oauth_refresh 与主链共用 TransportID 但 backend 不同
//	RetryPolicy    —— 画像没有重试证据
//	selector       —— 传输选择依赖部署支持策略，不是画像数据
//	FactsDomain    —— 同上
//
// 上一版的转换器把这些统统"推导"了出来：给所有非 raw 端点安上 MaxAttempts=3
// （覆盖 OAuth refresh、WHAM consume 等非幂等 POST）、令 Purpose = EndpointID、
// 把所有 HTTP transport 硬推成 BackendHTTPUpstream。这不是默认值不当，
// 是让纯数据转换器产出了数据里不存在的策略。
//
// 这些属于 ReleaseBinding / ExecutionPolicy / DeploymentSupportPolicy 层，
// 由 0C/0D 处理。ProfileSpec 碰不到它们，也就编不出来。
//
// # 无损判据
//
// 唯一验收是 canonical 往返：
//
//	canonical(源 snapshot) == canonical(ProfileSpec → snapshot)
//
// 因此 Transport 必须保存**完整**数据。上一版把 TLS/WS 压成 64-bit 哈希，
// 哈希能证明"变了"，不能证明"承载了"——从 ProfileSpec 恢复不出 CipherSuites。

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// ProfileSpec 与 SnapshotDoc 同构——这是刻意的。
//
// 「纯版本证据」的含义就是它与画像一一对应：多一个字段是发明，少一个字段是丢失。
// 相对裸 SnapshotDoc 的增值在于类型化枚举、私有字段的不可变性，以及 canonical 序列化。
type ProfileSpec struct {
	version        string
	officialDigest string
	endpoints      []EndpointProfile
	transports     []TransportProfile
	features       FeatureDefaults
	crossSections  []CrossSection
}

func (p ProfileSpec) Version() string        { return p.version }
func (p ProfileSpec) OfficialDigest() string { return p.officialDigest }

func (p ProfileSpec) Endpoints() []EndpointProfile {
	out := make([]EndpointProfile, len(p.endpoints))
	for i, e := range p.endpoints {
		out[i] = e.clone()
	}
	return out
}

func (p ProfileSpec) Transports() []TransportProfile {
	out := make([]TransportProfile, len(p.transports))
	for i, tr := range p.transports {
		out[i] = tr.clone()
	}
	return out
}

func (p ProfileSpec) Features() FeatureDefaults { return p.features }

// CrossSections 返回深拷贝：RawJSON 是 []byte，浅拷贝切片头仍共享底层数组，
// 调用方改它会污染 ProfileSpec 并改变后续 digest。
func (p ProfileSpec) CrossSections() []CrossSection {
	out := make([]CrossSection, len(p.crossSections))
	for i, s := range p.crossSections {
		out[i] = CrossSection{
			Name:    s.Name,
			RawJSON: append(json.RawMessage(nil), s.RawJSON...),
		}
	}
	return out
}

// EndpointProfile 逐字段对应画像的端点定义。
type EndpointProfile struct {
	ID                      string
	Method                  string
	Upgrade                 string
	TransportID             string
	Host                    string
	HostFromResponse        bool
	Path                    string
	Query                   []QueryFieldProfile
	Accept                  string
	ContentType             string
	Compression             CompressionKind
	ClientLifecycle         LifecycleKind
	HeaderOrderMode         HeaderOrderKind
	Headers                 []HeaderSlotProfile
	HeaderMapInsertionOrder []string
	PostRemoveHeaders       []string
	Body                    BodyContractProfile
}

func (e EndpointProfile) clone() EndpointProfile {
	out := e
	out.Query = append([]QueryFieldProfile(nil), e.Query...)
	out.Headers = append([]HeaderSlotProfile(nil), e.Headers...)
	out.HeaderMapInsertionOrder = append([]string(nil), e.HeaderMapInsertionOrder...)
	out.PostRemoveHeaders = append([]string(nil), e.PostRemoveHeaders...)
	out.Body.Fields = append([]BodyFieldProfile(nil), e.Body.Fields...)
	return out
}

type QueryFieldProfile struct {
	Name     string
	Value    string
	Source   ValueSource
	Required bool
}

type HeaderSlotProfile struct {
	Slot           int
	Sequence       int
	Name           string
	WireName       string
	Value          string
	Source         ValueSource
	Condition      ConditionKind
	AlternateGroup string
}

type BodyContractProfile struct {
	Encoding      BodyEncoding
	Closed        bool
	Discriminator string
	Fields        []BodyFieldProfile
}

// BodyFieldProfile 只有真实源结构体存在的四个字段。
// 上一版多了一个 Source——真实 officialCodexBodyField 里没有它。
type BodyFieldProfile struct {
	Name      string
	Required  bool
	OmitWhen  OmitCondition
	Condition ConditionKind
}

// TransportProfile 保存**完整**传输参数。
//
// 上一版压成一个短哈希，导致 ResolvedRelease 恢复不出 CipherSuites、Extensions、
// WS 协商参数，adapter 也没有 catalog 能按 ref 取回。往返验收因此不可能成立。
type TransportProfile struct {
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
	WebSocket                *WSTransportProfile
}

func (t TransportProfile) clone() TransportProfile {
	out := t
	out.CipherSuites = append([]uint16(nil), t.CipherSuites...)
	out.SupportedGroups = append([]uint16(nil), t.SupportedGroups...)
	out.SignatureAlgorithms = append([]uint16(nil), t.SignatureAlgorithms...)
	out.ALPN = append([]string(nil), t.ALPN...)
	out.Extensions = append([]uint16(nil), t.Extensions...)
	out.SupportedVersions = append([]uint16(nil), t.SupportedVersions...)
	out.KeyShareGroups = append([]uint16(nil), t.KeyShareGroups...)
	out.PSKModes = append([]uint16(nil), t.PSKModes...)
	if t.WebSocket != nil {
		ws := *t.WebSocket
		ws.FixedHandshakePrefix = append([]string(nil), t.WebSocket.FixedHandshakePrefix...)
		out.WebSocket = &ws
	}
	return out
}

type WSTransportProfile struct {
	FixedHandshakePrefix []string
	RemainingHeaderMode  string
	CompressionOffer     string
	ContextTakeover      bool
	CompressedTextRSV1   bool
	RawDeflatePayload    bool
}

type FeatureDefaults struct {
	RemoteCompactionV2             bool
	EnableRequestCompression       bool
	RequestCompressionLevel        int
	ResponsesLiteFromModelManifest bool
	ParallelToolsFromModelManifest bool
	RuntimeMetrics                 bool
	SupportsWebSockets             bool
	ForceHTTPFallback              bool
}

// CrossSection 承载跨端点配置的原文。
//
// 不解析成结构体：这些段属于 body/tool 编排层，语义不在 wire 定型层。
// 但必须保留**原文**，否则往返不可能字节相等。
type CrossSection struct {
	Name    string
	RawJSON json.RawMessage
}

// NewProfileSpec 是唯一构造入口，深拷贝全部引用类型。
//
// 枚举是否被执行引擎支持不属于纯画像证据，由 0B 的
// ObservedEnumValues ⊆ EngineSupportedEnumValues 门禁单独判断。
func NewProfileSpec(doc SnapshotDoc) (ProfileSpec, error) {
	spec := ProfileSpec{
		version:        doc.Version,
		officialDigest: doc.Digest,
		features: FeatureDefaults{
			RemoteCompactionV2:             doc.FeatureDefaults.RemoteCompactionV2,
			EnableRequestCompression:       doc.FeatureDefaults.EnableRequestCompression,
			RequestCompressionLevel:        doc.FeatureDefaults.RequestCompressionLevel,
			ResponsesLiteFromModelManifest: doc.FeatureDefaults.ResponsesLiteFromModelManifest,
			ParallelToolsFromModelManifest: doc.FeatureDefaults.ParallelToolsFromModelManifest,
			RuntimeMetrics:                 doc.FeatureDefaults.RuntimeMetrics,
			SupportsWebSockets:             doc.FeatureDefaults.SupportsWebSockets,
			ForceHTTPFallback:              doc.FeatureDefaults.ForceHTTPFallback,
		},
	}

	for _, ep := range doc.Endpoints {
		e := EndpointProfile{
			ID: ep.ID, Method: ep.Method, Upgrade: ep.Upgrade,
			TransportID: ep.TransportID, Host: ep.Host,
			HostFromResponse: ep.HostFromResponse, Path: ep.Path,
			Accept: ep.Accept, ContentType: ep.ContentType,
			Compression:             CompressionKind(ep.Compression),
			ClientLifecycle:         LifecycleKind(ep.ClientLifecycle),
			HeaderOrderMode:         HeaderOrderKind(ep.HeaderOrderMode),
			HeaderMapInsertionOrder: append([]string(nil), ep.HeaderMapInsertionOrder...),
			PostRemoveHeaders:       append([]string(nil), ep.PostRemoveHeaders...),
			Body: BodyContractProfile{
				Encoding:      BodyEncoding(ep.Body.Encoding),
				Closed:        ep.Body.Closed,
				Discriminator: ep.Body.Discriminator,
			},
		}
		for _, q := range ep.Query {
			qp := QueryFieldProfile{Name: q.Name, Value: q.Value,
				Source: ValueSource(q.Source), Required: q.Required}
			e.Query = append(e.Query, qp)
		}
		for _, h := range ep.Headers {
			hp := HeaderSlotProfile{
				Slot: h.Slot, Sequence: h.Sequence, Name: h.Name, WireName: h.WireName,
				Value: h.Value, Source: ValueSource(h.Source),
				Condition: ConditionKind(h.Condition), AlternateGroup: h.AlternateGroup,
			}
			e.Headers = append(e.Headers, hp)
		}
		for _, f := range ep.Body.Fields {
			fp := BodyFieldProfile{
				Name: f.Name, Required: f.Required,
				OmitWhen: OmitCondition(f.OmitWhen), Condition: ConditionKind(f.Condition),
			}
			e.Body.Fields = append(e.Body.Fields, fp)
		}
		spec.endpoints = append(spec.endpoints, e)
	}

	for _, tr := range doc.Transports {
		t := TransportProfile{
			ID: tr.ID, Protocol: tr.Protocol,
			PlatformCondition: tr.PlatformCondition, TLSStack: tr.TLSStack,
			CipherSuites:        append([]uint16(nil), tr.CipherSuites...),
			SupportedGroups:     append([]uint16(nil), tr.SupportedGroups...),
			SignatureAlgorithms: append([]uint16(nil), tr.SignatureAlgorithms...),
			ALPN:                append([]string(nil), tr.ALPN...),
			Extensions:          append([]uint16(nil), tr.Extensions...),
			RandomizeExtensions: tr.RandomizeExtensions,
			SupportedVersions:   append([]uint16(nil), tr.SupportedVersions...),
			KeyShareGroups:      append([]uint16(nil), tr.KeyShareGroups...),
			PSKModes:            append([]uint16(nil), tr.PSKModes...),
			TLSMinVersion:       tr.TLSMinVersion, TLSMaxVersion: tr.TLSMaxVersion,
			LowercaseHTTPHeaders:     tr.LowercaseHTTPHeaders,
			CrossCallConnectionReuse: tr.CrossCallConnectionReuse,
			RetryReusesClient:        tr.RetryReusesClient,
		}
		if tr.WebSocket != nil {
			t.WebSocket = &WSTransportProfile{
				FixedHandshakePrefix: append([]string(nil), tr.WebSocket.FixedHandshakePrefix...),
				RemainingHeaderMode:  tr.WebSocket.RemainingHeaderMode,
				CompressionOffer:     tr.WebSocket.CompressionOffer,
				ContextTakeover:      tr.WebSocket.ContextTakeover,
				CompressedTextRSV1:   tr.WebSocket.CompressedTextRSV1,
				RawDeflatePayload:    tr.WebSocket.RawDeflatePayload,
			}
		}
		spec.transports = append(spec.transports, t)
	}

	// 跨端点配置按画像的固定顺序保留，不排序——顺序也是数据。
	for _, sec := range []struct {
		name string
		raw  json.RawMessage
	}{
		{"RequiredRules", doc.RequiredRules},
		{"Surfaces", doc.Surfaces},
		{"ToolPresentation", doc.ToolPresentation},
		{"Subagents", doc.Subagents},
		{"Files", doc.Files},
	} {
		spec.crossSections = append(spec.crossSections, CrossSection{
			Name: sec.name, RawJSON: append(json.RawMessage(nil), sec.raw...),
		})
	}
	return spec, nil
}

// ToSnapshot 是 NewProfileSpec 的逆变换。
//
// 它让 canonical 往返成为可判定的验收：
//
//	canonical(源) == canonical(ProfileSpec → 源)
//
// 上一版声称有 CanonicalSnapshot，仓库里没有这个类型——注释写了不存在的东西。
func (p ProfileSpec) ToSnapshot() SnapshotDoc {
	doc := SnapshotDoc{
		Version: p.version,
		Digest:  p.officialDigest,
		FeatureDefaults: SnapshotFeatures{
			RemoteCompactionV2:             p.features.RemoteCompactionV2,
			EnableRequestCompression:       p.features.EnableRequestCompression,
			RequestCompressionLevel:        p.features.RequestCompressionLevel,
			ResponsesLiteFromModelManifest: p.features.ResponsesLiteFromModelManifest,
			ParallelToolsFromModelManifest: p.features.ParallelToolsFromModelManifest,
			RuntimeMetrics:                 p.features.RuntimeMetrics,
			SupportsWebSockets:             p.features.SupportsWebSockets,
			ForceHTTPFallback:              p.features.ForceHTTPFallback,
		},
	}
	for _, e := range p.endpoints {
		ep := SnapshotEndpoint{
			ID: e.ID, Method: e.Method, Upgrade: e.Upgrade,
			TransportID: e.TransportID, Host: e.Host,
			HostFromResponse: e.HostFromResponse, Path: e.Path,
			Accept: e.Accept, ContentType: e.ContentType,
			Compression:             string(e.Compression),
			ClientLifecycle:         string(e.ClientLifecycle),
			HeaderOrderMode:         string(e.HeaderOrderMode),
			HeaderMapInsertionOrder: append([]string(nil), e.HeaderMapInsertionOrder...),
			PostRemoveHeaders:       append([]string(nil), e.PostRemoveHeaders...),
			Body: SnapshotBodyContract{
				Encoding:      string(e.Body.Encoding),
				Closed:        e.Body.Closed,
				Discriminator: e.Body.Discriminator,
			},
		}
		for _, q := range e.Query {
			ep.Query = append(ep.Query, SnapshotQueryField{
				Name: q.Name, Value: q.Value, Source: string(q.Source), Required: q.Required,
			})
		}
		for _, h := range e.Headers {
			ep.Headers = append(ep.Headers, SnapshotHeaderSlot{
				Slot: h.Slot, Sequence: h.Sequence, Name: h.Name, WireName: h.WireName,
				Value: h.Value, Source: string(h.Source),
				Condition: string(h.Condition), AlternateGroup: h.AlternateGroup,
			})
		}
		for _, f := range e.Body.Fields {
			ep.Body.Fields = append(ep.Body.Fields, SnapshotBodyField{
				Name: f.Name, Required: f.Required,
				OmitWhen: string(f.OmitWhen), Condition: string(f.Condition),
			})
		}
		doc.Endpoints = append(doc.Endpoints, ep)
	}
	for _, t := range p.transports {
		tr := SnapshotTransport{
			ID: t.ID, Protocol: t.Protocol,
			PlatformCondition: t.PlatformCondition, TLSStack: t.TLSStack,
			CipherSuites:        append([]uint16(nil), t.CipherSuites...),
			SupportedGroups:     append([]uint16(nil), t.SupportedGroups...),
			SignatureAlgorithms: append([]uint16(nil), t.SignatureAlgorithms...),
			ALPN:                append([]string(nil), t.ALPN...),
			Extensions:          append([]uint16(nil), t.Extensions...),
			RandomizeExtensions: t.RandomizeExtensions,
			SupportedVersions:   append([]uint16(nil), t.SupportedVersions...),
			KeyShareGroups:      append([]uint16(nil), t.KeyShareGroups...),
			PSKModes:            append([]uint16(nil), t.PSKModes...),
			TLSMinVersion:       t.TLSMinVersion, TLSMaxVersion: t.TLSMaxVersion,
			LowercaseHTTPHeaders:     t.LowercaseHTTPHeaders,
			CrossCallConnectionReuse: t.CrossCallConnectionReuse,
			RetryReusesClient:        t.RetryReusesClient,
		}
		if t.WebSocket != nil {
			tr.WebSocket = &SnapshotWSTransport{
				FixedHandshakePrefix: append([]string(nil), t.WebSocket.FixedHandshakePrefix...),
				RemainingHeaderMode:  t.WebSocket.RemainingHeaderMode,
				CompressionOffer:     t.WebSocket.CompressionOffer,
				ContextTakeover:      t.WebSocket.ContextTakeover,
				CompressedTextRSV1:   t.WebSocket.CompressedTextRSV1,
				RawDeflatePayload:    t.WebSocket.RawDeflatePayload,
			}
		}
		doc.Transports = append(doc.Transports, tr)
	}
	for _, sec := range p.crossSections {
		raw := append(json.RawMessage(nil), sec.RawJSON...)
		switch sec.Name {
		case "RequiredRules":
			doc.RequiredRules = raw
		case "Surfaces":
			doc.Surfaces = raw
		case "ToolPresentation":
			doc.ToolPresentation = raw
		case "Subagents":
			doc.Subagents = raw
		case "Files":
			doc.Files = raw
		}
	}
	return doc
}

// ProfileDigest 是**完整 SHA-256**，不截断。
//
// 上一版用 16 位十六进制（64 bit）——对一份需要证明"未被篡改"的权威数据，
// 截断只是省了几十字节的显示宽度，换来的是碰撞空间从 2^256 掉到 2^64。
func (p ProfileSpec) ProfileDigest() (string, error) {
	canon, err := CanonicalJSON(p.ToSnapshot())
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canon)
	return hex.EncodeToString(sum[:]), nil
}

// CanonicalJSON 产出确定性字节序列，供往返比较与 digest。
//
// 走 map[string]any 中转：Go 对 map 键排序，从而消除结构体字段序、
// omitempty 与空切片/nil 的表示差异。
func CanonicalJSON(v any) ([]byte, error) {
	raw, err := json.Marshal(v)
	if err != nil {
		return nil, err
	}
	var generic any
	if err := json.Unmarshal(raw, &generic); err != nil {
		return nil, err
	}
	return json.Marshal(generic)
}
