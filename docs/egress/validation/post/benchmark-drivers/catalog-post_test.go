package officialegress

import "testing"

var changeset6ReleaseCatalogBenchmarkSink string

// BenchmarkReleaseCatalogResolve 守护默认 Catalog 启动期预编译后的常数级查表契约。
func BenchmarkReleaseCatalogResolve(b *testing.B) {
	catalog := DefaultReleaseCatalog()
	b.ReportAllocs()
	for range b.N {
		release, err := catalog.Resolve(ReleaseModeActive)
		if err != nil {
			b.Fatal(err)
		}
		changeset6ReleaseCatalogBenchmarkSink = release.ReleaseDigest()
	}
}
