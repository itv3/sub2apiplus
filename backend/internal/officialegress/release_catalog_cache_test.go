package officialegress

import (
	"reflect"
	"testing"
)

func TestReleaseCatalogResolveCacheIsImmutable(t *testing.T) {
	catalog := DefaultReleaseCatalog()
	before, err := catalog.Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}

	node, ok := before.Node(RegistryPurposeOpenAIOAuthHTTP)
	if !ok {
		t.Fatal("active HTTP node 缺失")
	}
	expectedNode, ok := before.Node(RegistryPurposeOpenAIOAuthHTTP)
	if !ok {
		t.Fatal("active HTTP node 缺失")
	}
	if len(node.Build.RuntimeHeaders) > 0 {
		node.Build.RuntimeHeaders[0].Value = "mutated"
	}
	if len(node.Wire.StaticHeaders) > 0 {
		node.Wire.StaticHeaders[0].Value = "mutated"
	}

	endpoints := before.Profile().Endpoints()
	if len(endpoints) == 0 {
		t.Fatal("Profile 缺少 endpoint")
	}
	endpoints[0].ID = "mutated"
	if len(endpoints[0].Headers) > 0 {
		endpoints[0].Headers[0].Value = "mutated"
	}

	transports := before.ExecutableProfile().Transports()
	if len(transports) == 0 {
		t.Fatal("ExecutableProfile 缺少 transport")
	}
	transports[0].ID = "mutated"
	if len(transports[0].CipherSuites) > 0 {
		transports[0].CipherSuites[0] = 0
	}

	after, err := catalog.Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	afterNode, ok := after.Node(RegistryPurposeOpenAIOAuthHTTP)
	if !ok {
		t.Fatal("二次 Resolve 后 active HTTP node 缺失")
	}
	if !reflect.DeepEqual(afterNode, expectedNode) {
		t.Fatal("修改 Node() 返回值污染了预编译缓存")
	}
	if reflect.DeepEqual(endpoints, after.Profile().Endpoints()) {
		t.Fatal("测试 mutation 未与缓存结果形成差异")
	}
	if reflect.DeepEqual(transports, after.ExecutableProfile().Transports()) {
		t.Fatal("测试 transport mutation 未与缓存结果形成差异")
	}
	if before.ReleaseDigest() != after.ReleaseDigest() ||
		before.ProfileDigest() != after.ProfileDigest() ||
		before.ExecutableProfileDigest() != after.ExecutableProfileDigest() {
		t.Fatal("修改只读 getter 返回值污染了预编译摘要")
	}
}
