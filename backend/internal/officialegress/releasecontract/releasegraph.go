// Package releasecontract 承载变更集 0B 的 Codex OAuth 发布图。
//
// 发布节点以 purpose + mode 为坐标。版本号不是节点身份：active 与 previous 即使
// 版本相同，也可能使用不同 Build 与 wire profile。
package releasecontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strings"
)

const ReleaseGraphSchemaVersion = 1

type ReleaseMode string

const (
	ReleaseModeActive   ReleaseMode = "active"
	ReleaseModePrevious ReleaseMode = "previous"
)

func (m ReleaseMode) Valid() bool {
	return m == ReleaseModeActive || m == ReleaseModePrevious
}

type ReleaseCoordinate struct {
	Purpose string
	Mode    ReleaseMode
}

// ReleaseGraphDoc 与导出文件逐字段对应，不使用 omitempty。
type ReleaseGraphDoc struct {
	SchemaVersion int              `json:"schema_version"`
	Nodes         []ReleaseNodeDoc `json:"nodes"`
}

type ReleaseNodeDoc struct {
	Purpose  string               `json:"purpose"`
	Mode     ReleaseMode          `json:"mode"`
	Build    ReleaseBuildDoc      `json:"build"`
	Wire     ReleaseWireDoc       `json:"wire"`
	Snapshot SnapshotReferenceDoc `json:"snapshot"`
}

type HeaderValueDoc struct {
	Name  string `json:"name"`
	Value string `json:"value"`
}

type ReleaseBuildDoc struct {
	ID             string           `json:"id"`
	Provider       string           `json:"provider"`
	Product        string           `json:"product"`
	Surface        string           `json:"surface"`
	Version        string           `json:"version"`
	UserAgent      string           `json:"user_agent"`
	Originator     string           `json:"originator"`
	RuntimeHeaders []HeaderValueDoc `json:"runtime_headers"`
	Source         string           `json:"source"`
}

type ReleaseWireDoc struct {
	ID                 string           `json:"id"`
	Purpose            string           `json:"purpose"`
	BuildID            string           `json:"build_id"`
	AuthMode           string           `json:"auth_mode"`
	Endpoint           string           `json:"endpoint"`
	Transport          string           `json:"transport"`
	NetworkVariant     string           `json:"network_variant"`
	StaticHeaders      []HeaderValueDoc `json:"static_headers"`
	BetaHeader         string           `json:"beta_header"`
	TransportProfileID string           `json:"transport_profile_id"`
	Source             string           `json:"source"`
	Digest             string           `json:"digest"`
}

// SnapshotReferenceDoc 只保存不可变画像坐标，不按版本号覆盖旧文件。
type SnapshotReferenceDoc struct {
	Version string `json:"version"`
	Digest  string `json:"digest"`
}

var ErrTrailingData = errors.New("发布图后存在多余数据")

func ParseReleaseGraph(raw []byte) (ReleaseGraphDoc, error) {
	var doc ReleaseGraphDoc
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&doc); err != nil {
		return ReleaseGraphDoc{}, fmt.Errorf("解析发布图: %w", err)
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return ReleaseGraphDoc{}, ErrTrailingData
	}
	return doc, nil
}

// ReleaseGraph 是深拷贝后的不可变发布图。
type ReleaseGraph struct {
	schemaVersion int
	ordered       []ReleaseNodeDoc
	byCoordinate  map[ReleaseCoordinate]ReleaseNodeDoc
}

func NewReleaseGraph(doc ReleaseGraphDoc) (ReleaseGraph, error) {
	if doc.SchemaVersion != ReleaseGraphSchemaVersion {
		return ReleaseGraph{}, fmt.Errorf("不支持的发布图 schema_version: %d", doc.SchemaVersion)
	}
	if len(doc.Nodes) == 0 {
		return ReleaseGraph{}, errors.New("发布图没有节点")
	}

	graph := ReleaseGraph{
		schemaVersion: doc.SchemaVersion,
		ordered:       make([]ReleaseNodeDoc, 0, len(doc.Nodes)),
		byCoordinate:  make(map[ReleaseCoordinate]ReleaseNodeDoc, len(doc.Nodes)),
	}
	modesByPurpose := make(map[string]map[ReleaseMode]bool)
	for _, input := range doc.Nodes {
		node := cloneReleaseNode(input)
		if err := validateReleaseNode(node); err != nil {
			return ReleaseGraph{}, err
		}
		coordinate := ReleaseCoordinate{Purpose: node.Purpose, Mode: node.Mode}
		if _, exists := graph.byCoordinate[coordinate]; exists {
			return ReleaseGraph{}, fmt.Errorf("发布坐标重复: purpose=%s mode=%s", node.Purpose, node.Mode)
		}
		graph.ordered = append(graph.ordered, node)
		graph.byCoordinate[coordinate] = cloneReleaseNode(node)
		if modesByPurpose[node.Purpose] == nil {
			modesByPurpose[node.Purpose] = make(map[ReleaseMode]bool)
		}
		modesByPurpose[node.Purpose][node.Mode] = true
	}
	for purpose, modes := range modesByPurpose {
		if !modes[ReleaseModeActive] || !modes[ReleaseModePrevious] || len(modes) != 2 {
			return ReleaseGraph{}, fmt.Errorf("purpose %s 必须同时包含 active 与 previous", purpose)
		}
	}
	return graph, nil
}

func validateReleaseNode(node ReleaseNodeDoc) error {
	if strings.TrimSpace(node.Purpose) == "" || !node.Mode.Valid() {
		return errors.New("发布节点缺少合法 purpose/mode")
	}
	if node.Wire.Purpose != node.Purpose {
		return fmt.Errorf("purpose %s 与 wire purpose %s 不一致", node.Purpose, node.Wire.Purpose)
	}
	if node.Build.ID == "" || node.Build.ID != node.Wire.BuildID {
		return fmt.Errorf("purpose %s mode %s 的 BuildID 不一致", node.Purpose, node.Mode)
	}
	if node.Build.Provider != "openai" || node.Build.Product != "codex" || node.Wire.AuthMode != "oauth" {
		return fmt.Errorf("purpose %s mode %s 不是 Codex OAuth 发布", node.Purpose, node.Mode)
	}
	if node.Build.Version == "" || node.Build.UserAgent == "" || node.Wire.ID == "" ||
		node.Wire.Transport == "" || node.Wire.TransportProfileID == "" {
		return fmt.Errorf("purpose %s mode %s 的发布信息不完整", node.Purpose, node.Mode)
	}
	if node.Snapshot.Version != node.Build.Version {
		return fmt.Errorf("purpose %s mode %s 的快照版本与 Build 版本不一致", node.Purpose, node.Mode)
	}
	if !isSHA256Hex(node.Snapshot.Digest) {
		return fmt.Errorf("purpose %s mode %s 的快照 digest 非法", node.Purpose, node.Mode)
	}
	if !isSHA256Hex(node.Wire.Digest) {
		return fmt.Errorf("purpose %s mode %s 的 wire digest 非法", node.Purpose, node.Mode)
	}
	expected, err := digestRegistryProfile(node.Build, node.Wire)
	if err != nil {
		return err
	}
	if expected != node.Wire.Digest {
		return fmt.Errorf(
			"purpose %s mode %s 的 wire digest 与 Build/Profile 内容不一致",
			node.Purpose,
			node.Mode,
		)
	}
	return nil
}

func isSHA256Hex(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func (g ReleaseGraph) SchemaVersion() int { return g.schemaVersion }

func (g ReleaseGraph) Resolve(purpose string, mode ReleaseMode) (ReleaseNodeDoc, bool) {
	node, ok := g.byCoordinate[ReleaseCoordinate{Purpose: purpose, Mode: mode}]
	if !ok {
		return ReleaseNodeDoc{}, false
	}
	return cloneReleaseNode(node), true
}

func (g ReleaseGraph) Nodes() []ReleaseNodeDoc {
	out := make([]ReleaseNodeDoc, len(g.ordered))
	for i, node := range g.ordered {
		out[i] = cloneReleaseNode(node)
	}
	return out
}

func (g ReleaseGraph) ToDoc() ReleaseGraphDoc {
	return ReleaseGraphDoc{SchemaVersion: g.schemaVersion, Nodes: g.Nodes()}
}

func (g ReleaseGraph) Digest() (string, error) {
	canonical, err := CanonicalJSON(g.ToDoc())
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

func cloneReleaseNode(node ReleaseNodeDoc) ReleaseNodeDoc {
	out := node
	out.Build.RuntimeHeaders = cloneHeaderValues(node.Build.RuntimeHeaders)
	out.Wire.StaticHeaders = cloneHeaderValues(node.Wire.StaticHeaders)
	return out
}

// cloneHeaderValues 保留 nil 与非 nil 空切片的区别，避免 canonical 往返把 [] 改成 null。
func cloneHeaderValues(values []HeaderValueDoc) []HeaderValueDoc {
	if values == nil {
		return nil
	}
	out := make([]HeaderValueDoc, len(values))
	copy(out, values)
	return out
}

func CanonicalJSON(value any) ([]byte, error) {
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var generic any
	if err := json.Unmarshal(raw, &generic); err != nil {
		return nil, err
	}
	return json.Marshal(generic)
}

// digestRegistryProfile 复刻 official-client registry 的摘要输入格式。
//
// 这不是创建第二个发布事实源，而是验证导出图中的 digest 确实对应同一节点内容。
func digestRegistryProfile(build ReleaseBuildDoc, wire ReleaseWireDoc) (string, error) {
	type registryHeader struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	}
	type registryBuild struct {
		ID             string           `json:"id"`
		Provider       string           `json:"provider"`
		Product        string           `json:"product"`
		Surface        string           `json:"surface"`
		Version        string           `json:"version"`
		UserAgent      string           `json:"user_agent"`
		Originator     string           `json:"originator,omitempty"`
		RuntimeHeaders []registryHeader `json:"runtime_headers,omitempty"`
		Source         string           `json:"source"`
	}
	type registryWire struct {
		ID                 string           `json:"id"`
		Purpose            string           `json:"purpose"`
		BuildID            string           `json:"build_id"`
		AuthMode           string           `json:"auth_mode"`
		Endpoint           string           `json:"endpoint"`
		Transport          string           `json:"transport"`
		NetworkVariant     string           `json:"network_variant"`
		StaticHeaders      []registryHeader `json:"static_headers,omitempty"`
		BetaHeader         string           `json:"beta_header,omitempty"`
		TransportProfileID string           `json:"transport_profile_id"`
		Source             string           `json:"source"`
		Digest             string           `json:"digest"`
	}
	toHeaders := func(values []HeaderValueDoc) []registryHeader {
		out := make([]registryHeader, len(values))
		for i, value := range values {
			out[i] = registryHeader(value)
		}
		return out
	}
	payload := struct {
		Build   registryBuild `json:"build"`
		Profile registryWire  `json:"profile"`
	}{
		Build: registryBuild{
			ID: build.ID, Provider: build.Provider, Product: build.Product,
			Surface: build.Surface, Version: build.Version, UserAgent: build.UserAgent,
			Originator: build.Originator, RuntimeHeaders: toHeaders(build.RuntimeHeaders),
			Source: build.Source,
		},
		Profile: registryWire{
			ID: wire.ID, Purpose: wire.Purpose, BuildID: wire.BuildID,
			AuthMode: wire.AuthMode, Endpoint: wire.Endpoint, Transport: wire.Transport,
			NetworkVariant: wire.NetworkVariant, StaticHeaders: toHeaders(wire.StaticHeaders),
			BetaHeader: wire.BetaHeader, TransportProfileID: wire.TransportProfileID,
			Source: wire.Source, Digest: "",
		},
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return "", fmt.Errorf("计算 registry profile digest: %w", err)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:]), nil
}

// SortedCoordinates 供门禁输出稳定的发布坐标清单。
func (g ReleaseGraph) SortedCoordinates() []ReleaseCoordinate {
	out := make([]ReleaseCoordinate, 0, len(g.byCoordinate))
	for coordinate := range g.byCoordinate {
		out = append(out, coordinate)
	}
	sort.Slice(out, func(i, j int) bool {
		if out[i].Purpose != out[j].Purpose {
			return out[i].Purpose < out[j].Purpose
		}
		return out[i].Mode < out[j].Mode
	})
	return out
}
