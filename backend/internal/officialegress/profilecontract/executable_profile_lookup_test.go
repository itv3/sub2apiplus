package profilecontract

import (
	"reflect"
	"testing"
)

func TestExecutableProfileLookupMatchesIndependentCopies(t *testing.T) {
	_, profile, err := compileExecutableDoc(t, loadExecutableProfileDoc(t))
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range profile.Endpoints() {
		got, found := profile.Endpoint(want.ID)
		if !found || !reflect.DeepEqual(want, got) {
			t.Fatalf("单端点查询与全表查询不同：%s", want.ID)
		}
		if len(got.Headers) > 0 {
			got.Headers[0].Name = "changed"
		}
		if len(got.Body.Fields) > 0 {
			got.Body.Fields[0].Name = "changed"
		}
		again, _ := profile.Endpoint(want.ID)
		if !reflect.DeepEqual(want, again) {
			t.Fatalf("调用方修改污染了冻结画像：%s", want.ID)
		}
	}
	for _, want := range profile.Transports() {
		got, found := profile.Transport(want.ID)
		if !found || !reflect.DeepEqual(want, got) {
			t.Fatalf("单传输查询与全表查询不同：%s", want.ID)
		}
		if len(got.CipherSuites) > 0 {
			got.CipherSuites[0] = 0
		}
		again, _ := profile.Transport(want.ID)
		if !reflect.DeepEqual(want, again) {
			t.Fatalf("调用方修改污染了传输画像：%s", want.ID)
		}
	}
	if _, found := profile.Endpoint("missing"); found {
		t.Fatal("未知端点不应命中")
	}
	if _, found := profile.Transport("missing"); found {
		t.Fatal("未知传输不应命中")
	}
}
