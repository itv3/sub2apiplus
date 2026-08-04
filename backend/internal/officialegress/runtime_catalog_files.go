package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"path"
	"sort"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// RuntimeCatalogFile 是正式版本数据导出工具使用的只读文件快照。
// Data 每次由导出函数重新构造，调用方修改它不会污染进程内 Catalog。
type RuntimeCatalogFile struct {
	Path string
	Data []byte
}

// RuntimeCatalogFiles 从已验证的内存 Catalog 确定性重建内容寻址目录。
// 聚合文档按原文字节 SHA-256 命名，Profile 按 version + 官方画像 digest 命名，
// SnapshotCatalog 另记录 Profile 原文字节 SHA-256，防止同路径原地覆盖。
func (c ReleaseCatalog) RuntimeCatalogFiles() ([]RuntimeCatalogFile, error) {
	if c.digest == "" || c.source == "" {
		return nil, errors.New("ReleaseCatalog 缺少可导出的正式来源")
	}
	graphRaw, err := c.GraphJSON()
	if err != nil {
		return nil, err
	}
	graphSHA := runtimeCatalogSHA256(graphRaw)
	graphRelativePath := path.Join("release-graphs", graphSHA+".json")

	snapshotDoc := c.snapshots.ToDoc()
	files := make([]RuntimeCatalogFile, 0, len(snapshotDoc.Snapshots)+3)
	files = append(files, RuntimeCatalogFile{Path: graphRelativePath, Data: graphRaw})
	for index := range snapshotDoc.Snapshots {
		entry := &snapshotDoc.Snapshots[index]
		key := profilecontract.SnapshotKey{Version: entry.Version, Digest: entry.Digest}
		profile, ok := c.snapshots.Resolve(key)
		if !ok {
			return nil, errors.New("SnapshotCatalog 引用了无法导出的 Profile")
		}
		profileRaw, marshalErr := json.Marshal(profile.ToSnapshot())
		if marshalErr != nil {
			return nil, marshalErr
		}
		profileRaw = append(profileRaw, '\n')
		entry.File = path.Join("profiles", entry.Version, entry.Digest+".json")
		entry.BlobSHA256 = runtimeCatalogSHA256(profileRaw)
		files = append(files, RuntimeCatalogFile{
			Path: entry.File,
			Data: profileRaw,
		})
	}
	snapshotRaw, err := json.Marshal(snapshotDoc)
	if err != nil {
		return nil, err
	}
	snapshotRaw = append(snapshotRaw, '\n')
	snapshotSHA := runtimeCatalogSHA256(snapshotRaw)
	snapshotRelativePath := path.Join("snapshot-catalogs", snapshotSHA+".json")
	files = append(files, RuntimeCatalogFile{Path: snapshotRelativePath, Data: snapshotRaw})

	var manifest releaseCatalogManifest
	manifest.SchemaVersion = 1
	manifest.Source = c.source
	manifest.ReleaseGraph.Path = path.Join("catalogdata/runtime", graphRelativePath)
	manifest.ReleaseGraph.SHA256 = graphSHA
	manifest.SnapshotCatalog.Path = path.Join("catalogdata/runtime", snapshotRelativePath)
	manifest.SnapshotCatalog.SHA256 = snapshotSHA
	manifestRaw, err := json.Marshal(manifest)
	if err != nil {
		return nil, err
	}
	manifestRaw = append(manifestRaw, '\n')
	files = append(files, RuntimeCatalogFile{Path: "release-catalog.json", Data: manifestRaw})

	sort.Slice(files, func(i, j int) bool { return files[i].Path < files[j].Path })
	return files, nil
}

func runtimeCatalogSHA256(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
