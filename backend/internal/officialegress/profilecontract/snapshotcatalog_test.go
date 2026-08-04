package profilecontract_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	p "github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func loadSnapshotCatalog(t *testing.T) (p.SnapshotCatalogDoc, p.SnapshotCatalog) {
	t.Helper()
	raw, err := os.ReadFile("testdata/snapshot-catalog.json")
	if err != nil {
		t.Fatalf("读取快照目录: %v", err)
	}
	doc, err := p.ParseSnapshotCatalog(raw)
	if err != nil {
		t.Fatalf("解析快照目录: %v", err)
	}
	catalog, err := p.NewSnapshotCatalog(doc, func(relativePath string) ([]byte, error) {
		return os.ReadFile(filepath.Join("testdata", filepath.FromSlash(relativePath)))
	})
	if err != nil {
		t.Fatalf("构造快照目录: %v", err)
	}
	return doc, catalog
}

func TestSnapshotCatalogUsesImmutableVersionDigestKey(t *testing.T) {
	doc, catalog := loadSnapshotCatalog(t)
	if len(doc.Snapshots) < 2 {
		t.Fatalf("同版本修正必须追加新 digest 并保留旧画像，实际 %d", len(doc.Snapshots))
	}
	for _, entry := range doc.Snapshots {
		key := p.SnapshotKey{Version: entry.Version, Digest: entry.Digest}
		profile, ok := catalog.Resolve(key)
		if !ok {
			t.Fatal("version+digest 无法解析画像")
		}
		if profile.Version() != key.Version || profile.OfficialDigest() != key.Digest {
			t.Fatal("解析画像与目录坐标不一致")
		}
		wantFile := filepath.ToSlash(filepath.Join("snapshots", key.Version, key.Digest+".json"))
		if entry.File != wantFile {
			t.Fatalf("快照文件名没有绑定 version+digest: %s", entry.File)
		}
	}
}

func TestSnapshotCatalogRejectsMutableOrMismatchedEntries(t *testing.T) {
	doc, _ := loadSnapshotCatalog(t)
	loader := func(relativePath string) ([]byte, error) {
		return os.ReadFile(filepath.Join("testdata", filepath.FromSlash(relativePath)))
	}

	t.Run("重复坐标", func(t *testing.T) {
		copyDoc := doc
		copyDoc.Snapshots = append(append([]p.SnapshotCatalogEntry(nil), doc.Snapshots...), doc.Snapshots[0])
		if _, err := p.NewSnapshotCatalog(copyDoc, loader); err == nil {
			t.Fatal("重复 version+digest 必须失败")
		}
	})

	t.Run("路径不由坐标派生", func(t *testing.T) {
		copyDoc := doc
		copyDoc.Snapshots = append([]p.SnapshotCatalogEntry(nil), doc.Snapshots...)
		copyDoc.Snapshots[0].File = "snapshots/latest.json"
		if _, err := p.NewSnapshotCatalog(copyDoc, loader); err == nil {
			t.Fatal("可覆盖的 latest 路径必须失败")
		}
	})

	t.Run("目录digest与内容不一致", func(t *testing.T) {
		copyDoc := doc
		copyDoc.Snapshots = append([]p.SnapshotCatalogEntry(nil), doc.Snapshots...)
		copyDoc.Snapshots[0].Digest = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
		copyDoc.Snapshots[0].File = filepath.ToSlash(filepath.Join(
			"snapshots",
			copyDoc.Snapshots[0].Version,
			copyDoc.Snapshots[0].Digest+".json",
		))
		if _, err := p.NewSnapshotCatalog(copyDoc, func(string) ([]byte, error) { return loadRaw(t), nil }); err == nil {
			t.Fatal("目录 digest 与快照内容不一致必须失败")
		}
	})

	t.Run("内容被篡改但保留旧digest", func(t *testing.T) {
		snapshot, err := p.ParseSnapshot(loadRaw(t))
		if err != nil {
			t.Fatal(err)
		}
		snapshot.Endpoints[0].Accept += ";tampered"
		mutated, err := json.Marshal(snapshot)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := p.NewSnapshotCatalog(doc, func(string) ([]byte, error) { return mutated, nil }); err == nil {
			t.Fatal("画像内容被篡改但保留旧 digest 时必须失败")
		}
	})
}
