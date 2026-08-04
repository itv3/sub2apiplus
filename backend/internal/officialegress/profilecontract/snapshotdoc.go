// Package profilecontract 是 0A 的交付：官方画像快照的严格 DTO 与 ProfileSpec。
//
// 边界刻意收窄——本包**只**承载画像里实际存在的数据，不含 purpose、backend、
// RetryPolicy、selector、FactsDomain。那些属于 ReleaseBinding / ExecutionPolicy /
// DeploymentSupportPolicy，由 0B–0D 处理，与本包分属不同 Go 包，物理上碰不到。
//
// 本包的测试全部属于 0A，不混入 Executor 或发送实现用例。
package profilecontract

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
)

// 快照 DTO：字段名、类型与顺序全部对应真实源结构体。
//
// **不使用 omitempty**。源快照本身是完整序列化的——实测 16 个端点的键集完全一致，
// `Upgrade` 存在且为空串。加了 omitempty 会让往返产出与源不同构：源里显式的零值
// 在往返后消失，而比较双方又都经过同一套 omitempty，于是一起"变绿"。
// 上一版的往返测试正是这样假绿的。
type SnapshotDoc struct {
	Version          string              `json:"Version"`
	RequiredRules    json.RawMessage     `json:"RequiredRules"`
	Surfaces         json.RawMessage     `json:"Surfaces"`
	FeatureDefaults  SnapshotFeatures    `json:"FeatureDefaults"`
	ToolPresentation json.RawMessage     `json:"ToolPresentation"`
	Subagents        json.RawMessage     `json:"Subagents"`
	Files            json.RawMessage     `json:"Files"`
	Transports       []SnapshotTransport `json:"Transports"`
	Endpoints        []SnapshotEndpoint  `json:"Endpoints"`
	Digest           string              `json:"Digest"`
}

type SnapshotEndpoint struct {
	ID                      string               `json:"ID"`
	Method                  string               `json:"Method"`
	Upgrade                 string               `json:"Upgrade"`
	TransportID             string               `json:"TransportID"`
	Host                    string               `json:"Host"`
	HostFromResponse        bool                 `json:"HostFromResponse"`
	Path                    string               `json:"Path"`
	Query                   []SnapshotQueryField `json:"Query"`
	Accept                  string               `json:"Accept"`
	ContentType             string               `json:"ContentType"`
	Compression             string               `json:"Compression"`
	ClientLifecycle         string               `json:"ClientLifecycle"`
	HeaderOrderMode         string               `json:"HeaderOrderMode"`
	Headers                 []SnapshotHeaderSlot `json:"Headers"`
	HeaderMapInsertionOrder []string             `json:"HeaderMapInsertionOrder"`
	PostRemoveHeaders       []string             `json:"PostRemoveHeaders"`
	Body                    SnapshotBodyContract `json:"Body"`
}

type SnapshotQueryField struct {
	Name     string `json:"Name"`
	Value    string `json:"Value"`
	Source   string `json:"Source"`
	Required bool   `json:"Required"`
}

type SnapshotHeaderSlot struct {
	Slot           int    `json:"Slot"`
	Sequence       int    `json:"Sequence"`
	Name           string `json:"Name"`
	WireName       string `json:"WireName"`
	Value          string `json:"Value"`
	Source         string `json:"Source"`
	Condition      string `json:"Condition"`
	AlternateGroup string `json:"AlternateGroup"`
}

type SnapshotBodyContract struct {
	Encoding      string              `json:"Encoding"`
	Closed        bool                `json:"Closed"`
	Discriminator string              `json:"Discriminator"`
	Fields        []SnapshotBodyField `json:"Fields"`
}

// SnapshotBodyField 逐字段对应真实的 officialCodexBodyField：
// 只有 Name / Required / OmitWhen / Condition **四个**字段。
//
// 上一版给它加了 Source——真实结构体里没有这个字段。那是凭空增加信息，
// 与给端点安上 MaxAttempts=3 是同一类错误。
type SnapshotBodyField struct {
	Name      string `json:"Name"`
	Required  bool   `json:"Required"`
	OmitWhen  string `json:"OmitWhen"`
	Condition string `json:"Condition"`
}

type SnapshotTransport struct {
	ID                       string               `json:"ID"`
	Protocol                 string               `json:"Protocol"`
	PlatformCondition        string               `json:"PlatformCondition"`
	TLSStack                 string               `json:"TLSStack"`
	CipherSuites             []uint16             `json:"CipherSuites"`
	SupportedGroups          []uint16             `json:"SupportedGroups"`
	SignatureAlgorithms      []uint16             `json:"SignatureAlgorithms"`
	ALPN                     []string             `json:"ALPN"`
	Extensions               []uint16             `json:"Extensions"`
	RandomizeExtensions      bool                 `json:"RandomizeExtensions"`
	SupportedVersions        []uint16             `json:"SupportedVersions"`
	KeyShareGroups           []uint16             `json:"KeyShareGroups"`
	PSKModes                 []uint16             `json:"PSKModes"` // 真实源码是 uint16，不是 uint8
	TLSMinVersion            uint16               `json:"TLSMinVersion"`
	TLSMaxVersion            uint16               `json:"TLSMaxVersion"`
	LowercaseHTTPHeaders     bool                 `json:"LowercaseHTTPHeaders"`
	CrossCallConnectionReuse bool                 `json:"CrossCallConnectionReuse"`
	RetryReusesClient        bool                 `json:"RetryReusesClient"`
	WebSocket                *SnapshotWSTransport `json:"WebSocket"`
}

type SnapshotWSTransport struct {
	FixedHandshakePrefix []string `json:"FixedHandshakePrefix"`
	RemainingHeaderMode  string   `json:"RemainingHeaderMode"`
	CompressionOffer     string   `json:"CompressionOffer"`
	CompressedTextRSV1   bool     `json:"CompressedTextRSV1"`
	RawDeflatePayload    bool     `json:"RawDeflatePayload"`
	ContextTakeover      bool     `json:"ContextTakeover"`
}

type SnapshotFeatures struct {
	SupportsWebSockets             bool `json:"SupportsWebSockets"`
	RemoteCompactionV2             bool `json:"RemoteCompactionV2"`
	EnableRequestCompression       bool `json:"EnableRequestCompression"`
	RequestCompressionLevel        int  `json:"RequestCompressionLevel"`
	RuntimeMetrics                 bool `json:"RuntimeMetrics"`
	ForceHTTPFallback              bool `json:"ForceHTTPFallback"`
	ResponsesLiteFromModelManifest bool `json:"ResponsesLiteFromModelManifest"`
	ParallelToolsFromModelManifest bool `json:"ParallelToolsFromModelManifest"`
}

var ErrTrailingData = errors.New("快照后存在多余数据")

// ParseSnapshot 严格解析：未知字段失败，且**必须**在一个 JSON 值后到达 EOF。
//
// 少了 EOF 检查，`{...}{...}` 这种尾随第二个 JSON 会被静默接受——只解析第一个。
func ParseSnapshot(raw []byte) (SnapshotDoc, error) {
	var doc SnapshotDoc
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&doc); err != nil {
		return SnapshotDoc{}, fmt.Errorf("解析画像快照: %w", err)
	}
	if _, err := dec.Token(); !errors.Is(err, io.EOF) {
		return SnapshotDoc{}, ErrTrailingData
	}
	return doc, nil
}
