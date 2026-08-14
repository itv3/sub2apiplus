package releasecontract_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	p "github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	r "github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

func loadReleaseGraphRawFrom(t *testing.T, path string) []byte {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("读取发布图: %v", err)
	}
	return raw
}

func loadReleaseGraphRaw(t *testing.T) []byte {
	t.Helper()
	return loadReleaseGraphRawFrom(t, "testdata/release-graph.json")
}

func mustReleaseGraphFrom(t *testing.T, path string) r.ReleaseGraph {
	t.Helper()
	doc, err := r.ParseReleaseGraph(loadReleaseGraphRawFrom(t, path))
	if err != nil {
		t.Fatalf("解析发布图: %v", err)
	}
	graph, err := r.NewReleaseGraph(doc)
	if err != nil {
		t.Fatalf("构造发布图: %v", err)
	}
	return graph
}

func mustReleaseGraph(t *testing.T) r.ReleaseGraph {
	t.Helper()
	return mustReleaseGraphFrom(t, "testdata/release-graph.json")
}

func TestReleaseGraphCanonicalRoundTrip(t *testing.T) {
	raw := loadReleaseGraphRaw(t)
	var source any
	if err := json.Unmarshal(raw, &source); err != nil {
		t.Fatal(err)
	}
	sourceCanonical, err := json.Marshal(source)
	if err != nil {
		t.Fatal(err)
	}
	backCanonical, err := r.CanonicalJSON(mustReleaseGraph(t).ToDoc())
	if err != nil {
		t.Fatal(err)
	}
	if string(sourceCanonical) != string(backCanonical) {
		t.Fatal("发布图 canonical 往返不相等")
	}
}

func TestReleaseGraphKeepsPurposeAndModeIdentity(t *testing.T) {
	// 该性质必须排除“版本号天然不同”带来的假通过，因此使用升级前冻结的
	// 同版本历史夹具；运行时共享夹具继续表达当前 Active/Previous 混版本状态。
	graph := mustReleaseGraphFrom(t, "testdata/release-graph-same-version.json")
	for _, purpose := range []string{
		"openai_oauth_responses_http",
		"openai_oauth_responses_ws",
	} {
		active, ok := graph.Resolve(purpose, r.ReleaseModeActive)
		if !ok {
			t.Fatalf("缺少 %s active", purpose)
		}
		previous, ok := graph.Resolve(purpose, r.ReleaseModePrevious)
		if !ok {
			t.Fatalf("缺少 %s previous", purpose)
		}
		if active.Build.Version != previous.Build.Version || active.Snapshot != previous.Snapshot {
			t.Fatalf("当前夹具应证明同版本、同画像快照可以对应不同发布: %s", purpose)
		}
		if active.Build.ID == previous.Build.ID || active.Build.UserAgent == previous.Build.UserAgent ||
			active.Wire.ID == previous.Wire.ID || active.Wire.Digest == previous.Wire.Digest {
			t.Fatalf("%s active/previous 被错误折叠", purpose)
		}
	}
}

func TestEveryReleaseNodeReferencesImmutableSnapshot(t *testing.T) {
	manifestRaw, err := os.ReadFile("../profilecontract/testdata/snapshot-catalog.json")
	if err != nil {
		t.Fatal(err)
	}
	manifest, err := p.ParseSnapshotCatalog(manifestRaw)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := p.NewSnapshotCatalog(manifest, func(relativePath string) ([]byte, error) {
		return os.ReadFile(filepath.Join(
			"../profilecontract/testdata",
			filepath.FromSlash(relativePath),
		))
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, node := range mustReleaseGraph(t).Nodes() {
		key := p.SnapshotKey{Version: node.Snapshot.Version, Digest: node.Snapshot.Digest}
		if _, ok := catalog.Resolve(key); !ok {
			t.Fatalf("发布节点 %s/%s 引用了未归档快照 %+v", node.Purpose, node.Mode, key)
		}
	}
}

func TestReleaseGraphIsDeeplyImmutable(t *testing.T) {
	graph := mustReleaseGraph(t)
	before, err := graph.Digest()
	if err != nil {
		t.Fatal(err)
	}
	targets := []any{graph.Nodes(), graph.ToDoc()}
	if node, ok := graph.Resolve("openai_oauth_responses_http", r.ReleaseModeActive); ok {
		targets = append(targets, &node)
	}
	mutated := 0
	for _, target := range targets {
		mutated += corruptReleaseLeaves(reflect.ValueOf(target))
	}
	if mutated == 0 {
		t.Fatal("反射未改写任何发布图叶子")
	}
	after, err := graph.Digest()
	if err != nil {
		t.Fatal(err)
	}
	if before != after {
		t.Fatal("修改返回对象污染了不可变 ReleaseGraph")
	}
}

func TestReleaseGraphRejectsInvalidEvidence(t *testing.T) {
	raw := loadReleaseGraphRaw(t)
	doc, err := r.ParseReleaseGraph(raw)
	if err != nil {
		t.Fatal(err)
	}

	t.Run("重复坐标", func(t *testing.T) {
		copyDoc := doc
		copyDoc.Nodes = append(append([]r.ReleaseNodeDoc(nil), doc.Nodes...), doc.Nodes[0])
		if _, err := r.NewReleaseGraph(copyDoc); err == nil {
			t.Fatal("重复 purpose+mode 必须失败")
		}
	})

	t.Run("内容与wire摘要不一致", func(t *testing.T) {
		copyDoc := doc
		copyDoc.Nodes = append([]r.ReleaseNodeDoc(nil), doc.Nodes...)
		copyDoc.Nodes[0].Build.UserAgent += "_MUT"
		if _, err := r.NewReleaseGraph(copyDoc); err == nil {
			t.Fatal("修改 Build 后沿用旧 wire digest 必须失败")
		}
	})

	t.Run("快照版本不一致", func(t *testing.T) {
		copyDoc := doc
		copyDoc.Nodes = append([]r.ReleaseNodeDoc(nil), doc.Nodes...)
		copyDoc.Nodes[0].Snapshot.Version = "0.999.0"
		if _, err := r.NewReleaseGraph(copyDoc); err == nil {
			t.Fatal("Snapshot.Version 与 Build.Version 不一致必须失败")
		}
	})

	t.Run("未知字段和尾随数据", func(t *testing.T) {
		unknown := append([]byte(`{"unknown":true,`), raw[1:]...)
		if _, err := r.ParseReleaseGraph(unknown); err == nil {
			t.Fatal("未知字段必须失败")
		}
		trailing := append(append([]byte(nil), raw...), []byte(`{"second":1}`)...)
		if _, err := r.ParseReleaseGraph(trailing); err == nil {
			t.Fatal("尾随 JSON 必须失败")
		}
	})
}

func corruptReleaseLeaves(value reflect.Value) int {
	switch value.Kind() {
	case reflect.Ptr, reflect.Interface:
		if value.IsNil() {
			return 0
		}
		return corruptReleaseLeaves(value.Elem())
	case reflect.Slice, reflect.Array:
		count := 0
		for i := 0; i < value.Len(); i++ {
			count += corruptReleaseLeaves(value.Index(i))
		}
		return count
	case reflect.Struct:
		count := 0
		for i := 0; i < value.NumField(); i++ {
			count += corruptReleaseLeaves(value.Field(i))
		}
		return count
	case reflect.String:
		if value.CanSet() {
			value.SetString(value.String() + "_MUT")
			return 1
		}
	case reflect.Int, reflect.Int8, reflect.Int16, reflect.Int32, reflect.Int64:
		if value.CanSet() {
			value.SetInt(value.Int() + 1)
			return 1
		}
	}
	return 0
}
