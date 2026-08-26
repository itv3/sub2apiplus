package officialegress

import (
	"bytes"
	"encoding/json"
	"io/fs"
	"path"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func TestRuntimeCatalogFilesDeterministicallyRebuildEmbeddedTree(t *testing.T) {
	catalog := DefaultReleaseCatalog()
	files, err := catalog.RuntimeCatalogFiles()
	if err != nil {
		t.Fatal(err)
	}
	wantFileCount := len(catalog.snapshots.ToDoc().Snapshots) + 3
	if len(files) != wantFileCount {
		t.Fatalf("正式版本数据文件数=%d，期望 %d", len(files), wantFileCount)
	}
	seen := make(map[string]bool, len(files))
	for _, file := range files {
		if seen[file.Path] || path.Clean(file.Path) != file.Path || strings.Contains(file.Path, "testdata") {
			t.Fatalf("正式版本数据导出路径非法：%s", file.Path)
		}
		seen[file.Path] = true
		embedded, readErr := releaseCatalogFS.ReadFile(path.Join("catalogdata/runtime", file.Path))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if !bytes.Equal(file.Data, embedded) {
			t.Fatalf("正式版本数据无法确定性重建：%s", file.Path)
		}
	}
	if !seen["release-catalog.json"] {
		t.Fatal("正式版本数据缺少 release-catalog.json selector")
	}
}

func TestRuntimeCatalogArchiveFilesPreserveHistoricalAggregates(t *testing.T) {
	catalog := DefaultReleaseCatalog()
	current, err := catalog.RuntimeCatalogFiles()
	if err != nil {
		t.Fatal(err)
	}
	archive, err := catalog.RuntimeCatalogArchiveFiles()
	if err != nil {
		t.Fatal(err)
	}
	currentPaths := make(map[string]bool, len(current))
	for _, file := range current {
		currentPaths[file.Path] = true
	}
	archiveFiles := make(map[string][]byte, len(archive))
	for _, file := range archive {
		if _, exists := archiveFiles[file.Path]; exists {
			t.Fatalf("正式版本归档重复路径：%s", file.Path)
		}
		archiveFiles[file.Path] = file.Data
	}
	for _, historicalPath := range []string{
		"release-graphs/d8a7634c9af52159c912b539df74b9fa3b92664c0b90cc0defd6789cd07c846d.json",
		"snapshot-catalogs/0e66972cc5f27fe1a1fe710f2ba1565e220ec7ca0cf4e6e6d6acf29ac63fcc88.json",
	} {
		if currentPaths[historicalPath] {
			t.Fatalf("历史聚合重新进入当前 selector 闭包：%s", historicalPath)
		}
		embedded, readErr := releaseCatalogFS.ReadFile(path.Join("catalogdata/runtime", historicalPath))
		if readErr != nil {
			t.Fatal(readErr)
		}
		if !bytes.Equal(archiveFiles[historicalPath], embedded) {
			t.Fatalf("历史聚合未逐字节追加保留：%s", historicalPath)
		}
	}
}

func TestReleaseCatalogFSContainsNoHistoricalTestdata(t *testing.T) {
	err := fs.WalkDir(releaseCatalogFS, ".", func(pathValue string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		if strings.Contains(pathValue, "testdata") {
			t.Fatalf("releaseCatalogFS 意外嵌入历史 testdata：%s", pathValue)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
}

func TestRuntimeProfileBlobRejectsSamePathDifferentBytes(t *testing.T) {
	catalog := DefaultReleaseCatalog()
	doc := catalog.snapshots.ToDoc()
	if len(doc.Snapshots) == 0 {
		t.Fatal("正式 SnapshotCatalog 为空")
	}
	mutatedPath := doc.Snapshots[0].File
	_, err := profilecontract.NewSnapshotCatalog(doc, func(relativePath string) ([]byte, error) {
		raw, readErr := releaseCatalogFS.ReadFile(path.Join("catalogdata/runtime", relativePath))
		if readErr != nil {
			return nil, readErr
		}
		if relativePath == mutatedPath {
			// 只增加合法 JSON 尾部空白，语义画像 digest 不变；blob SHA-256 仍必须拒绝。
			raw = append(raw, ' ')
		}
		return raw, nil
	})
	if err == nil || !strings.Contains(err.Error(), "blob SHA-256") {
		t.Fatalf("同内容寻址路径的不同原文字节未被拒绝：%v", err)
	}
}

func TestRuntimeReleaseCatalogManifestUsesDigestAddressedAggregates(t *testing.T) {
	raw, err := releaseCatalogFS.ReadFile(runtimeReleaseCatalogManifestPath)
	if err != nil {
		t.Fatal(err)
	}
	var manifest releaseCatalogManifest
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&manifest); err != nil {
		t.Fatal(err)
	}
	for _, item := range []struct {
		name      string
		pathValue string
		digest    string
		directory string
	}{
		{name: "ReleaseGraph", pathValue: manifest.ReleaseGraph.Path, digest: manifest.ReleaseGraph.SHA256, directory: "catalogdata/runtime/release-graphs"},
		{name: "SnapshotCatalog", pathValue: manifest.SnapshotCatalog.Path, digest: manifest.SnapshotCatalog.SHA256, directory: "catalogdata/runtime/snapshot-catalogs"},
	} {
		want := path.Join(item.directory, item.digest+".json")
		if item.pathValue != want || !receiptSHA256(item.digest) {
			t.Fatalf("%s 未按 digest 寻址：got=%s want=%s", item.name, item.pathValue, want)
		}
	}
}
