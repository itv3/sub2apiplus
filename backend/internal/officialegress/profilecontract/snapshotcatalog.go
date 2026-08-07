package profilecontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"path"
	"sort"
	"strings"
)

const SnapshotCatalogSchemaVersion = 1

type SnapshotKey struct {
	Version string
	Digest  string
}

type SnapshotCatalogDoc struct {
	SchemaVersion int                    `json:"schema_version"`
	Snapshots     []SnapshotCatalogEntry `json:"snapshots"`
}

type SnapshotCatalogEntry struct {
	Version    string `json:"version"`
	Digest     string `json:"digest"`
	BlobSHA256 string `json:"blob_sha256,omitempty"`
	File       string `json:"file"`
}

type SnapshotLoader func(relativePath string) ([]byte, error)

var ErrSnapshotCatalogTrailingData = errors.New("快照目录后存在多余数据")

func ParseSnapshotCatalog(raw []byte) (SnapshotCatalogDoc, error) {
	var doc SnapshotCatalogDoc
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&doc); err != nil {
		return SnapshotCatalogDoc{}, fmt.Errorf("解析快照目录: %w", err)
	}
	if _, err := decoder.Token(); !errors.Is(err, io.EOF) {
		return SnapshotCatalogDoc{}, ErrSnapshotCatalogTrailingData
	}
	return doc, nil
}

// SnapshotCatalog 以 version + digest 为不可变主键保存画像。
//
// 同一版本允许存在多个 digest；升级或回滚不得覆盖旧文件。
type SnapshotCatalog struct {
	schemaVersion int
	entries       map[SnapshotKey]SnapshotCatalogEntry
	profiles      map[SnapshotKey]ProfileSpec
	executables   map[SnapshotKey]ExecutableProfile
}

func NewSnapshotCatalog(doc SnapshotCatalogDoc, load SnapshotLoader) (SnapshotCatalog, error) {
	if doc.SchemaVersion != SnapshotCatalogSchemaVersion {
		return SnapshotCatalog{}, fmt.Errorf("不支持的快照目录 schema_version: %d", doc.SchemaVersion)
	}
	if len(doc.Snapshots) == 0 || load == nil {
		return SnapshotCatalog{}, errors.New("快照目录为空或 loader 为空")
	}
	catalog := SnapshotCatalog{
		schemaVersion: doc.SchemaVersion,
		entries:       make(map[SnapshotKey]SnapshotCatalogEntry, len(doc.Snapshots)),
		profiles:      make(map[SnapshotKey]ProfileSpec, len(doc.Snapshots)),
		executables:   make(map[SnapshotKey]ExecutableProfile, len(doc.Snapshots)),
	}
	for _, entry := range doc.Snapshots {
		key := SnapshotKey{Version: entry.Version, Digest: entry.Digest}
		if err := validateSnapshotEntry(entry); err != nil {
			return SnapshotCatalog{}, err
		}
		if _, exists := catalog.entries[key]; exists {
			return SnapshotCatalog{}, fmt.Errorf("快照坐标重复: version=%s digest=%s", key.Version, key.Digest)
		}
		raw, err := load(entry.File)
		if err != nil {
			return SnapshotCatalog{}, fmt.Errorf("读取快照 %s: %w", entry.File, err)
		}
		if entry.BlobSHA256 != "" {
			rawSum := sha256.Sum256(raw)
			if hex.EncodeToString(rawSum[:]) != entry.BlobSHA256 {
				return SnapshotCatalog{}, fmt.Errorf("快照 %s 的 blob SHA-256 不一致", entry.File)
			}
		}
		snapshot, err := ParseSnapshot(raw)
		if err != nil {
			return SnapshotCatalog{}, fmt.Errorf("解析快照 %s: %w", entry.File, err)
		}
		computedDigest, err := OfficialSnapshotDigest(snapshot)
		if err != nil {
			return SnapshotCatalog{}, fmt.Errorf("计算快照 %s 的官方摘要: %w", entry.File, err)
		}
		if computedDigest != snapshot.Digest || computedDigest != key.Digest {
			return SnapshotCatalog{}, fmt.Errorf("快照 %s 的内容摘要、自报摘要与目录坐标不一致", entry.File)
		}
		profile, err := NewProfileSpec(snapshot)
		if err != nil {
			return SnapshotCatalog{}, fmt.Errorf("构造快照 %s: %w", entry.File, err)
		}
		if profile.Version() != key.Version || profile.OfficialDigest() != key.Digest {
			return SnapshotCatalog{}, fmt.Errorf("快照 %s 的内容与 version+digest 坐标不一致", entry.File)
		}
		executable, err := CompileExecutableProfile(profile)
		if err != nil {
			return SnapshotCatalog{}, fmt.Errorf("编译快照 %s 的可执行画像: %w", entry.File, err)
		}
		catalog.entries[key] = entry
		catalog.profiles[key] = profile
		catalog.executables[key] = executable
	}
	return catalog, nil
}

// computeOfficialSnapshotDigest 独立复现生产画像的摘要算法：清空 Digest 后，
// 按官方源码结构体的字段顺序编码并计算完整 SHA-256。
//
// SnapshotDoc 及其嵌套 DTO 的字段顺序刻意与官方结构体一致。若只信任快照里的
// Digest 字段，攻击者或误操作可以修改画像内容后保留旧摘要，目录仍会假绿。
// OfficialSnapshotDigest 按官方 Snapshot 结构字段顺序复算完整画像摘要。
// 离线导入工具必须用它绑定 profile manifest，禁止自行猜测 JSON 字节序。
func OfficialSnapshotDigest(snapshot SnapshotDoc) (string, error) {
	snapshot.Digest = ""
	encoded, err := json.Marshal(snapshot)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(encoded)
	return hex.EncodeToString(sum[:]), nil
}

// CompactSnapshotRawMessages 去除 Snapshot 横切 RawMessage 字段的格式化空白，保留
// 字段顺序，确保批准清单被重新格式化后仍得到同一官方摘要。
func CompactSnapshotRawMessages(snapshot SnapshotDoc) (SnapshotDoc, error) {
	fields := []*json.RawMessage{
		&snapshot.RequiredRules,
		&snapshot.Surfaces,
		&snapshot.ToolPresentation,
		&snapshot.Subagents,
		&snapshot.Files,
	}
	for _, field := range fields {
		var compact bytes.Buffer
		if err := json.Compact(&compact, *field); err != nil {
			return SnapshotDoc{}, err
		}
		*field = append((*field)[:0], compact.Bytes()...)
	}
	return snapshot, nil
}

// PrepareSnapshotForManifest 先规范化 RawMessage 横切字段，再写入按官方算法复算的
// Digest。这样画像嵌入 sort_keys 规范化清单后，复算结果仍稳定一致。
func PrepareSnapshotForManifest(snapshot SnapshotDoc) (SnapshotDoc, error) {
	fields := []*json.RawMessage{
		&snapshot.RequiredRules,
		&snapshot.Surfaces,
		&snapshot.ToolPresentation,
		&snapshot.Subagents,
		&snapshot.Files,
	}
	for _, field := range fields {
		var value any
		if err := json.Unmarshal(*field, &value); err != nil {
			return SnapshotDoc{}, err
		}
		raw, err := json.Marshal(value)
		if err != nil {
			return SnapshotDoc{}, err
		}
		*field = raw
	}
	snapshot.Digest = ""
	digest, err := OfficialSnapshotDigest(snapshot)
	if err != nil {
		return SnapshotDoc{}, err
	}
	snapshot.Digest = digest
	return snapshot, nil
}

func validateSnapshotEntry(entry SnapshotCatalogEntry) error {
	if strings.TrimSpace(entry.Version) == "" || !isSnapshotSHA256(entry.Digest) {
		return errors.New("快照目录项缺少合法 version/digest")
	}
	clean := path.Clean(entry.File)
	if clean != entry.File || strings.HasPrefix(clean, "/") || clean == "." || strings.HasPrefix(clean, "../") {
		return fmt.Errorf("快照路径必须是目录内规范相对路径: %s", entry.File)
	}
	if entry.BlobSHA256 != "" && !isSnapshotSHA256(entry.BlobSHA256) {
		return errors.New("快照目录项 blob_sha256 非法")
	}
	wantEvidence := path.Join("snapshots", entry.Version, entry.Digest+".json")
	wantRuntime := path.Join("profiles", entry.Version, entry.Digest+".json")
	if entry.File != wantEvidence && entry.File != wantRuntime {
		return fmt.Errorf(
			"快照路径必须由 version+digest 派生: 实际 %s，期望 %s 或 %s",
			entry.File,
			wantEvidence,
			wantRuntime,
		)
	}
	return nil
}

func isSnapshotSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func (c SnapshotCatalog) Resolve(key SnapshotKey) (ProfileSpec, bool) {
	profile, ok := c.profiles[key]
	return profile, ok
}

// ResolveExecutable 返回启动期已验证的执行投影。运行时不得从 ProfileSpec.RawJSON
// 重新解释跨端点语义。
func (c SnapshotCatalog) ResolveExecutable(key SnapshotKey) (ExecutableProfile, bool) {
	profile, ok := c.executables[key]
	return profile, ok
}

func (c SnapshotCatalog) Entry(key SnapshotKey) (SnapshotCatalogEntry, bool) {
	entry, ok := c.entries[key]
	return entry, ok
}

func (c SnapshotCatalog) Keys() []SnapshotKey {
	keys := make([]SnapshotKey, 0, len(c.entries))
	for key := range c.entries {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].Version != keys[j].Version {
			return keys[i].Version < keys[j].Version
		}
		return keys[i].Digest < keys[j].Digest
	})
	return keys
}

func (c SnapshotCatalog) ToDoc() SnapshotCatalogDoc {
	entries := make([]SnapshotCatalogEntry, 0, len(c.entries))
	for _, key := range c.Keys() {
		entries = append(entries, c.entries[key])
	}
	return SnapshotCatalogDoc{SchemaVersion: c.schemaVersion, Snapshots: entries}
}

func (c SnapshotCatalog) Digest() (string, error) {
	canonical, err := CanonicalJSON(c.ToDoc())
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}
