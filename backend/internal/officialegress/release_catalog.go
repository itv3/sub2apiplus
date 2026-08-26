package officialegress

import (
	"bytes"
	"crypto/sha256"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"path"
	"sort"
	"strings"
	"sync"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

// 正式版本数据位于 catalogdata/runtime。ReleaseGraph 与 SnapshotCatalog 按原文字节
// SHA-256 寻址，Profile 按 version + 官方画像 digest 寻址并由独立 blob SHA-256
// 冻结原文字节。历史 testdata 不进入 releaseCatalogFS，也不再是运行时事实源。
//
//go:embed catalogdata/runtime/release-catalog.json catalogdata/runtime/release-graphs/*.json catalogdata/runtime/snapshot-catalogs/*.json catalogdata/runtime/profiles/*/*.json
var releaseCatalogFS embed.FS

const runtimeReleaseCatalogManifestPath = "catalogdata/runtime/release-catalog.json"

type releaseCatalogManifest struct {
	SchemaVersion int `json:"schema_version"`
	ReleaseGraph  struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"release_graph"`
	SnapshotCatalog struct {
		Path   string `json:"path"`
		SHA256 string `json:"sha256"`
	} `json:"snapshot_catalog"`
	Source string `json:"source"`
}

// ReleaseCatalog 是正式发布事实的进程级不可变目录。
type ReleaseCatalog struct {
	graph            releasecontract.ReleaseGraph
	snapshots        profilecontract.SnapshotCatalog
	digest           string
	source           string
	resolvedActive   ResolvedCodexRelease
	resolvedPrevious ResolvedCodexRelease
}

func LoadEmbeddedReleaseCatalog() (ReleaseCatalog, error) {
	return loadReleaseCatalogFromFS(releaseCatalogFS)
}

// loadReleaseCatalogFromFS 是正式 Catalog 的可注入真实加载入口。所有 manifest、blob、
// digest、交叉校验和预编译错误都在此边界取得稳定分类；底层错误链保持不变。
func loadReleaseCatalogFromFS(catalogFS fs.FS) (ReleaseCatalog, error) {
	catalog, err := loadReleaseCatalogFromFSUnwrapped(catalogFS)
	if err != nil {
		return ReleaseCatalog{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogLoadFailed, "catalog.load", err,
		)
	}
	return catalog, nil
}

func loadReleaseCatalogFromFSUnwrapped(catalogFS fs.FS) (ReleaseCatalog, error) {
	if catalogFS == nil {
		return ReleaseCatalog{}, errors.New("正式 ReleaseCatalog 文件系统为空")
	}
	manifestRaw, err := fs.ReadFile(catalogFS, runtimeReleaseCatalogManifestPath)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	var manifest releaseCatalogManifest
	decoder := json.NewDecoder(bytes.NewReader(manifestRaw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		return ReleaseCatalog{}, fmt.Errorf("解析正式 ReleaseCatalog 清单: %w", err)
	}
	if err := decoder.Decode(&struct{}{}); !errors.Is(err, io.EOF) {
		return ReleaseCatalog{}, errors.New("正式 ReleaseCatalog 清单尾部存在额外数据")
	}
	if manifest.SchemaVersion != 1 || strings.TrimSpace(manifest.Source) == "" {
		return ReleaseCatalog{}, errors.New("正式 ReleaseCatalog 清单元数据非法")
	}
	graphRaw, err := readReleaseCatalogBlob(
		catalogFS,
		manifest.ReleaseGraph.Path,
		manifest.ReleaseGraph.SHA256,
		"catalogdata/runtime/release-graphs",
	)
	if err != nil {
		return ReleaseCatalog{}, fmt.Errorf("读取正式 ReleaseGraph: %w", err)
	}
	graphDoc, err := releasecontract.ParseReleaseGraph(graphRaw)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	graph, err := releasecontract.NewReleaseGraph(graphDoc)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	snapshotRaw, err := readReleaseCatalogBlob(
		catalogFS,
		manifest.SnapshotCatalog.Path,
		manifest.SnapshotCatalog.SHA256,
		"catalogdata/runtime/snapshot-catalogs",
	)
	if err != nil {
		return ReleaseCatalog{}, fmt.Errorf("读取正式 SnapshotCatalog: %w", err)
	}
	snapshotDoc, err := profilecontract.ParseSnapshotCatalog(snapshotRaw)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	for _, entry := range snapshotDoc.Snapshots {
		if !strings.HasPrefix(entry.File, "profiles/") || !receiptSHA256(entry.BlobSHA256) {
			return ReleaseCatalog{}, fmt.Errorf(
				"正式 SnapshotCatalog 只能引用带 blob SHA-256 的 runtime Profile: %s",
				entry.File,
			)
		}
	}
	snapshotCatalog, err := profilecontract.NewSnapshotCatalog(
		snapshotDoc,
		func(relativePath string) ([]byte, error) {
			return fs.ReadFile(catalogFS, path.Join("catalogdata/runtime", relativePath))
		},
	)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	digestRaw, err := json.Marshal(struct {
		Manifest []byte
		Graph    string
		Snapshot string
	}{Manifest: manifestRaw, Graph: manifest.ReleaseGraph.SHA256, Snapshot: manifest.SnapshotCatalog.SHA256})
	if err != nil {
		return ReleaseCatalog{}, err
	}
	sum := sha256.Sum256(digestRaw)
	return newReleaseCatalog(
		graph,
		snapshotCatalog,
		hex.EncodeToString(sum[:]),
		manifest.Source,
	)
}

// newReleaseCatalog 在目录加载阶段完成全部交叉校验与 active/previous 预编译。
// 普通测试 Catalog 也必须经过这里构造，使 Resolve 始终只是固定槽位查表。
func newReleaseCatalog(
	graph releasecontract.ReleaseGraph,
	snapshots profilecontract.SnapshotCatalog,
	digest string,
	source string,
) (ReleaseCatalog, error) {
	if err := validateReleaseSiblingConsistency(graph, snapshots); err != nil {
		return ReleaseCatalog{}, err
	}
	catalog := ReleaseCatalog{
		graph: graph, snapshots: snapshots, digest: digest, source: source,
	}
	active, err := catalog.resolveUncached(ReleaseModeActive)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	previous, err := catalog.resolveUncached(ReleaseModePrevious)
	if err != nil {
		return ReleaseCatalog{}, err
	}
	catalog.resolvedActive = active
	catalog.resolvedPrevious = previous
	return catalog, nil
}

func readReleaseCatalogBlob(
	catalogFS fs.FS,
	pathValue,
	wantSHA256,
	directory string,
) ([]byte, error) {
	if strings.TrimSpace(pathValue) == "" || !receiptSHA256(wantSHA256) {
		return nil, errors.New("ReleaseCatalog blob 引用非法")
	}
	wantPath := path.Join(directory, wantSHA256+".json")
	if path.Clean(pathValue) != pathValue || pathValue != wantPath {
		return nil, fmt.Errorf("ReleaseCatalog blob 不是规范内容寻址路径: %s", pathValue)
	}
	raw, err := fs.ReadFile(catalogFS, pathValue)
	if err != nil {
		return nil, err
	}
	sum := sha256.Sum256(raw)
	if hex.EncodeToString(sum[:]) != wantSHA256 {
		return nil, fmt.Errorf("ReleaseCatalog blob 摘要不一致: %s", pathValue)
	}
	return raw, nil
}

func receiptSHA256(value string) bool {
	if len(value) != sha256.Size*2 {
		return false
	}
	_, err := hex.DecodeString(value)
	return err == nil
}

func validateReleaseSiblingConsistency(
	graph releasecontract.ReleaseGraph,
	snapshots profilecontract.SnapshotCatalog,
) error {
	for _, mode := range []releasecontract.ReleaseMode{
		releasecontract.ReleaseModeActive,
		releasecontract.ReleaseModePrevious,
	} {
		httpNode, httpOK := graph.Resolve(RegistryPurposeOpenAIOAuthHTTP, mode)
		wsNode, wsOK := graph.Resolve(RegistryPurposeOpenAIOAuthWS, mode)
		if !httpOK || !wsOK {
			return fmt.Errorf("release mode %s 缺少 HTTP/WS sibling", mode)
		}
		if httpNode.Build.ID != wsNode.Build.ID || httpNode.Build.Version != wsNode.Build.Version ||
			httpNode.Snapshot != wsNode.Snapshot {
			return fmt.Errorf("release mode %s 的 HTTP/WS sibling 发布身份不一致", mode)
		}
		key := profilecontract.SnapshotKey{
			Version: httpNode.Snapshot.Version, Digest: httpNode.Snapshot.Digest,
		}
		if _, ok := snapshots.Resolve(key); !ok {
			return fmt.Errorf("release mode %s 引用了未知 Snapshot", mode)
		}
	}
	active, _ := graph.Resolve(RegistryPurposeOpenAIOAuthHTTP, releasecontract.ReleaseModeActive)
	previous, _ := graph.Resolve(RegistryPurposeOpenAIOAuthHTTP, releasecontract.ReleaseModePrevious)
	if active.Build.ID == previous.Build.ID || active.Wire.Digest == previous.Wire.Digest {
		return errors.New("active/previous 被错误折叠")
	}
	return nil
}

func (c ReleaseCatalog) Digest() string { return c.digest }

func (c ReleaseCatalog) CanonicalGraphJSON() ([]byte, error) {
	return releasecontract.CanonicalJSON(c.graph.ToDoc())
}

// GraphJSON 按 ReleaseGraphDoc 的审计字段顺序导出正式发布图。文件级清单摘要
// 覆盖该稳定表示；CanonicalGraphJSON 仅用于与字段顺序无关的内容摘要。
func (c ReleaseCatalog) GraphJSON() ([]byte, error) {
	raw, err := json.Marshal(c.graph.ToDoc())
	if err != nil {
		return nil, err
	}
	return append(raw, '\n'), nil
}

// ResolvedCodexRelease 是纯发布事实：同 mode 的 HTTP/WS sibling 与共同 Snapshot。
type ResolvedCodexRelease struct {
	mode          ReleaseMode
	releaseDigest string
	profileDigest string
	nodes         map[string]releasecontract.ReleaseNodeDoc
	profile       profilecontract.ProfileSpec
	executable    profilecontract.ExecutableProfile
}

// ResolvedCodexSnapshot 是只读历史画像坐标，不包含 ReleaseGraph 节点、selector、
// ReleaseDigest 或可执行 Bundle。它只供冻结证据复算和离线兼容性门禁使用，不能
// 被生产请求当作 active/previous 发布选择。
type ResolvedCodexSnapshot struct {
	profileDigest string
	profile       profilecontract.ProfileSpec
	executable    profilecontract.ExecutableProfile
}

func (r ResolvedCodexSnapshot) Version() string                      { return r.profile.Version() }
func (r ResolvedCodexSnapshot) ProfileDigest() string                { return r.profileDigest }
func (r ResolvedCodexSnapshot) Profile() profilecontract.ProfileSpec { return r.profile }
func (r ResolvedCodexSnapshot) ExecutableProfile() profilecontract.ExecutableProfile {
	return r.executable
}
func (r ResolvedCodexSnapshot) ExecutableProfileDigest() string { return r.executable.Digest() }

func (r ResolvedCodexRelease) Mode() ReleaseMode                    { return r.mode }
func (r ResolvedCodexRelease) Persona() Persona                     { return PersonaCodexCLI }
func (r ResolvedCodexRelease) ReleaseDigest() string                { return r.releaseDigest }
func (r ResolvedCodexRelease) ProfileDigest() string                { return r.profileDigest }
func (r ResolvedCodexRelease) Version() string                      { return r.profile.Version() }
func (r ResolvedCodexRelease) Profile() profilecontract.ProfileSpec { return r.profile }
func (r ResolvedCodexRelease) ExecutableProfile() profilecontract.ExecutableProfile {
	return r.executable
}
func (r ResolvedCodexRelease) ExecutableProfileDigest() string { return r.executable.Digest() }

func (r ResolvedCodexRelease) Node(purpose string) (releasecontract.ReleaseNodeDoc, bool) {
	node, ok := r.nodes[purpose]
	if !ok {
		return releasecontract.ReleaseNodeDoc{}, false
	}
	node.Build.RuntimeHeaders = append(
		[]releasecontract.HeaderValueDoc(nil),
		node.Build.RuntimeHeaders...,
	)
	node.Wire.StaticHeaders = append(
		[]releasecontract.HeaderValueDoc(nil),
		node.Wire.StaticHeaders...,
	)
	return node, true
}

func (c ReleaseCatalog) Resolve(mode ReleaseMode) (ResolvedCodexRelease, error) {
	switch mode {
	case ReleaseModeActive:
		if c.resolvedActive.releaseDigest == "" {
			return ResolvedCodexRelease{}, WrapRuntimeError(
				RuntimeErrorCodeCatalogResolveFailed,
				"catalog.resolve",
				errors.New("ReleaseCatalog 尚未预编译 active"),
			)
		}
		return c.resolvedActive, nil
	case ReleaseModePrevious:
		if c.resolvedPrevious.releaseDigest == "" {
			return ResolvedCodexRelease{}, WrapRuntimeError(
				RuntimeErrorCodeCatalogResolveFailed,
				"catalog.resolve",
				errors.New("ReleaseCatalog 尚未预编译 previous"),
			)
		}
		return c.resolvedPrevious, nil
	default:
		return ResolvedCodexRelease{}, WrapRuntimeError(
			RuntimeErrorCodeCatalogResolveFailed,
			"catalog.resolve",
			errors.New("ReleaseMode 非法"),
		)
	}
}

// ResolveSnapshotExact 按不可变 version+digest 坐标读取历史画像。该入口不解析
// ReleaseMode，也不生成发布身份；线上请求仍只能通过 Resolve(active|previous)。
func (c ReleaseCatalog) ResolveSnapshotExact(
	version string,
	digest string,
) (ResolvedCodexSnapshot, error) {
	key := profilecontract.SnapshotKey{Version: version, Digest: digest}
	profile, ok := c.snapshots.Resolve(key)
	if !ok {
		return ResolvedCodexSnapshot{}, fmt.Errorf(
			"ReleaseCatalog 缺少精确历史 Snapshot: version=%s digest=%s", version, digest,
		)
	}
	executable, ok := c.snapshots.ResolveExecutable(key)
	if !ok {
		return ResolvedCodexSnapshot{}, fmt.Errorf(
			"ReleaseCatalog 历史 Snapshot 未预编译: version=%s digest=%s", version, digest,
		)
	}
	return ResolvedCodexSnapshot{
		profileDigest: profile.OfficialDigest(),
		profile:       profile,
		executable:    executable,
	}, nil
}

// resolveUncached 只允许在 newReleaseCatalog 的启动期编译过程中调用。
func (c ReleaseCatalog) resolveUncached(mode ReleaseMode) (ResolvedCodexRelease, error) {
	if !mode.Valid() {
		return ResolvedCodexRelease{}, errors.New("ReleaseMode 非法")
	}
	contractMode := releasecontract.ReleaseMode(mode)
	nodes := make(map[string]releasecontract.ReleaseNodeDoc, 2)
	for _, purpose := range []string{RegistryPurposeOpenAIOAuthHTTP, RegistryPurposeOpenAIOAuthWS} {
		node, ok := c.graph.Resolve(purpose, contractMode)
		if !ok {
			return ResolvedCodexRelease{}, fmt.Errorf("缺少 release node: %s/%s", purpose, mode)
		}
		nodes[purpose] = node
	}
	httpNode := nodes[RegistryPurposeOpenAIOAuthHTTP]
	profileKey := profilecontract.SnapshotKey{
		Version: httpNode.Snapshot.Version, Digest: httpNode.Snapshot.Digest,
	}
	profile, ok := c.snapshots.Resolve(profileKey)
	if !ok {
		return ResolvedCodexRelease{}, errors.New("Release 引用了未知 ProfileSpec")
	}
	executable, ok := c.snapshots.ResolveExecutable(profileKey)
	if !ok {
		return ResolvedCodexRelease{}, errors.New("Release 引用了未编译的 ExecutableProfile")
	}
	purposes := make([]string, 0, len(nodes))
	for purpose := range nodes {
		purposes = append(purposes, purpose)
	}
	sort.Strings(purposes)
	payload := struct {
		Mode    ReleaseMode
		Profile string
		Nodes   []releasecontract.ReleaseNodeDoc
	}{Mode: mode, Profile: profile.OfficialDigest()}
	for _, purpose := range purposes {
		payload.Nodes = append(payload.Nodes, nodes[purpose])
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return ResolvedCodexRelease{}, err
	}
	sum := sha256.Sum256(raw)
	return ResolvedCodexRelease{
		mode: mode, releaseDigest: hex.EncodeToString(sum[:]),
		profileDigest: profile.OfficialDigest(), nodes: nodes, profile: profile,
		executable: executable,
	}, nil
}

var loadDefaultReleaseCatalog = sync.OnceValues(LoadEmbeddedReleaseCatalog)

func DefaultReleaseCatalog() ReleaseCatalog {
	catalog, err := loadDefaultReleaseCatalog()
	if err != nil {
		panic(err)
	}
	return catalog
}
