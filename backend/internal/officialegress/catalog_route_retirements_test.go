package officialegress

import (
	"encoding/json"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

const (
	legacyCompactPath           = "/backend-api/codex/responses/compact"
	legacyCompactProfileVersion = "0.154.0"
	legacyCompactProfileDigest  = "31d8654f6892d37129a2639f1bb48e87b7b8648d67ce754f4ae9379a671b99e3"
	activeProfile0157Version    = "0.157.0"
	activeProfile0157Digest     = "3edd1c7bd487021469a932ff63599e581000402dd8633dfe02101a2ec4e3ae9d"
)

func legacyCompactPhysicalRoute() PhysicalRouteKey {
	return PhysicalRouteKey{Method: http.MethodPost, Host: "chatgpt.com", Path: legacyCompactPath, Protocol: WireProtocolHTTP}
}

func mustRetiredRouteProfile(t *testing.T, version, digest string) profilecontract.ExecutableProfile {
	t.Helper()
	executable, err := loadReleaseRouteRetirementProfile(
		releaseCatalogFS, releaseRouteRetirementProfile{Version: version, Digest: digest},
	)
	if err != nil {
		t.Fatal(err)
	}
	return executable
}

// 内嵌清单的唯一一条是 legacy compact：最后声明它的 0.154.0 画像按冻结合同原地保留，解析后恰有一个
// responses_compact 端点对应该物理 route。
func TestReleaseRouteRetirementManifestIsProvenByRetainedProfile(t *testing.T) {
	declared, err := parseReleaseRouteRetirements(embeddedReleaseRouteRetirements, releaseCatalogFS)
	if err != nil {
		t.Fatal(err)
	}
	if len(declared) != 1 || declared[0].physical != legacyCompactPhysicalRoute() ||
		declared[0].endpointID != officialCodexEndpointResponsesCompactForTest {
		t.Fatalf("发布退役清单与 legacy compact 登记不一致：%+v", declared)
	}
}

// 退役只在所有给定画像都不声明该端点时生效：0.157.0 单独不声明 legacy compact，加上 0.154.0 即仍声明。
func TestReleaseRouteRetirementOnlyAppliesWhenNoReleaseDeclaresRoute(t *testing.T) {
	declared, err := parseReleaseRouteRetirements(embeddedReleaseRouteRetirements, releaseCatalogFS)
	if err != nil {
		t.Fatal(err)
	}
	profile0157 := mustRetiredRouteProfile(t, activeProfile0157Version, activeProfile0157Digest)
	profile0154 := mustRetiredRouteProfile(t, legacyCompactProfileVersion, legacyCompactProfileDigest)
	compact := CatalogRoute{
		Key:      RouteKey{Method: http.MethodPost, Host: "chatgpt.com", Path: legacyCompactPath, Purpose: "admin_test.compact"},
		Protocol: WireProtocolHTTP,
	}
	responses := CatalogRoute{
		Key:      RouteKey{Method: http.MethodPost, Host: "chatgpt.com", Path: "/backend-api/codex/responses", Purpose: "user_request.responses"},
		Protocol: WireProtocolHTTP,
	}
	active := activeReleaseRouteRetirements(declared, []profilecontract.ExecutableProfile{profile0157})
	if !active.retires(compact) || active.retires(responses) {
		t.Fatal("只有 0.157.0 时 legacy compact 必须退役，普通 Responses 不得退役")
	}
	if endpointID, ok := active.retiredEndpointID(compact); !ok || endpointID != officialCodexEndpointResponsesCompactForTest {
		t.Fatalf("退役 route 登记端点不一致：%q", endpointID)
	}
	rollback := activeReleaseRouteRetirements(declared, []profilecontract.ExecutableProfile{profile0157, profile0154})
	if rollback.retires(compact) {
		t.Fatal("仍有发布声明 legacy compact 时退役不得生效")
	}
}

// 当前内嵌目录：Active/Previous 都不声明 legacy compact 时它必须退役、不进入路由目录且不可解析；任一声明时照常解析。
func TestDefaultCatalogLegacyCompactFollowsReleaseRetirement(t *testing.T) {
	declaredByRelease := false
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		declaredByRelease = declaredByRelease || profileDeclaresPhysicalRoute(release.ExecutableProfile(), legacyCompactPhysicalRoute())
	}
	retirements, err := defaultReleaseRouteRetirements()
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := DefaultSinkCatalog().Resolve(SinkCodexResponsesForward)
	if !ok {
		t.Fatal("缺少 Responses forward SinkBinding")
	}
	compact := CatalogRoute{
		Key:      RouteKey{Method: http.MethodPost, Host: "chatgpt.com", Path: legacyCompactPath, Purpose: binding.Purpose()},
		Protocol: WireProtocolHTTP,
	}
	staticRoute := false
	for _, route := range binding.Routes() {
		staticRoute = staticRoute || route == compact
	}
	if !staticRoute {
		t.Fatal("legacy compact 必须继续留在 Responses forward 的静态 binding 里供历史收据复核")
	}
	target := &url.URL{Scheme: "https", Host: "chatgpt.com", Path: legacyCompactPath}
	_, resolvable := DefaultOfficialRouteCatalog().ResolveBinding(http.MethodPost, target, WireProtocolHTTP, binding)
	if declaredByRelease {
		if retirements.retires(compact) || !resolvable {
			t.Fatal("仍有发布声明 legacy compact 时必须照常发布")
		}
		return
	}
	if !retirements.retires(compact) || resolvable {
		t.Fatal("Active/Previous 都不声明 legacy compact 时必须发布退役且不可解析")
	}
}

func TestReleaseRouteRetirementManifestRejectsInvalidDocuments(t *testing.T) {
	valid := func() map[string]any {
		return map[string]any{
			"schema_version":   1,
			"bootstrap_commit": BootstrapCommit,
			"routes": []any{map[string]any{
				"method": "POST", "host": "chatgpt.com", "path": legacyCompactPath, "protocol": "http",
				"endpoint_id":  officialCodexEndpointResponsesCompactForTest,
				"last_profile": map[string]any{"version": legacyCompactProfileVersion, "digest": legacyCompactProfileDigest},
				"reviewed_by":  "reviewer", "review_ref": "review", "rationale": "reason",
			}},
		}
	}
	route := func(document map[string]any) map[string]any {
		return document["routes"].([]any)[0].(map[string]any)
	}
	encode := func(t *testing.T, document map[string]any) []byte {
		t.Helper()
		raw, err := json.Marshal(document)
		if err != nil {
			t.Fatal(err)
		}
		return raw
	}
	if _, err := parseReleaseRouteRetirements(encode(t, valid()), releaseCatalogFS); err != nil {
		t.Fatalf("合法清单被拒绝：%v", err)
	}
	cases := []struct {
		name   string
		mutate func(map[string]any)
		want   string
	}{
		{"未知字段", func(d map[string]any) { route(d)["extra"] = true }, "解析失败"},
		{"schema 非法", func(d map[string]any) { d["schema_version"] = 2 }, "schema/bootstrap"},
		{"bootstrap 非法", func(d map[string]any) { d["bootstrap_commit"] = strings.Repeat("0", 40) }, "schema/bootstrap"},
		{"重复登记", func(d map[string]any) {
			d["routes"] = []any{route(d), route(valid())}
		}, "严格排序"},
		{"缺审核信息", func(d map[string]any) { route(d)["review_ref"] = " " }, "字段不完整"},
		{"摘要非法", func(d map[string]any) {
			route(d)["last_profile"] = map[string]any{"version": "0.154.0", "digest": "abc"}
		}, "字段不完整"},
		{"主机未规范化", func(d map[string]any) { route(d)["host"] = "ChatGPT.com" }, "未规范化"},
		{"方法未大写", func(d map[string]any) { route(d)["method"] = "post" }, "未规范化"},
		{"画像不存在", func(d map[string]any) {
			route(d)["last_profile"] = map[string]any{"version": "0.154.0", "digest": strings.Repeat("a", 64)}
		}, "读取最后画像"},
		{"画像版本错配", func(d map[string]any) {
			route(d)["last_profile"] = map[string]any{"version": "../0.154.0", "digest": legacyCompactProfileDigest}
		}, "版本非法"},
		{"端点 ID 不符", func(d map[string]any) { route(d)["endpoint_id"] = "responses" }, "没有唯一端点"},
		{"画像不声明该 route", func(d map[string]any) { route(d)["path"] = "/backend-api/codex/responses/unknown" }, "没有唯一端点"},
	}
	for _, testCase := range cases {
		t.Run(testCase.name, func(t *testing.T) {
			document := valid()
			testCase.mutate(document)
			_, err := parseReleaseRouteRetirements(encode(t, document), releaseCatalogFS)
			if err == nil || !strings.Contains(err.Error(), testCase.want) {
				t.Fatalf("期望错误含 %q，实际 %v", testCase.want, err)
			}
		})
	}
	if _, err := parseReleaseRouteRetirements(append(encode(t, valid()), []byte(" {}")...), releaseCatalogFS); err == nil ||
		!strings.Contains(err.Error(), "尾部") {
		t.Fatalf("尾部额外 JSON 未被拒绝：%v", err)
	}
}

func TestValidateReleaseRouteRetirementsUsedRejectsDanglingRoute(t *testing.T) {
	dangling := PhysicalRouteKey{Method: http.MethodPost, Host: "chatgpt.com", Path: "/backend-api/codex/retired-nowhere", Protocol: WireProtocolHTTP}
	retirements := releaseRouteRetirements{active: map[string]releaseRouteRetirement{
		dangling.identity(): {physical: dangling, endpointID: "retired_nowhere"},
	}}
	inputs := []SinkBindingInput{{
		ID: SinkCodexResponsesForward, Persona: PersonaCodexCLI, EndpointEvidence: EndpointEvidenceCodexProfile,
		Routes: []CatalogRoute{{
			Key:      RouteKey{Method: http.MethodPost, Host: "chatgpt.com", Path: "/backend-api/codex/responses", Purpose: "user_request.responses"},
			Protocol: WireProtocolHTTP,
		}},
	}}
	if err := validateReleaseRouteRetirementsUsed(retirements, inputs); err == nil ||
		!strings.Contains(err.Error(), "没有被任何 Codex binding 引用") {
		t.Fatalf("悬空的发布退役 route 未被拒绝：%v", err)
	}
	inputs[0].Routes = append(inputs[0].Routes, CatalogRoute{
		Key:      RouteKey{Method: dangling.Method, Host: dangling.Host, Path: dangling.Path, Purpose: "user_request.responses"},
		Protocol: dangling.Protocol,
	})
	if err := validateReleaseRouteRetirementsUsed(retirements, inputs); err != nil {
		t.Fatalf("被引用的发布退役 route 被误拒：%v", err)
	}
}

// officialCodexEndpointResponsesCompactForTest 与 legacy compact 历史收据、0.154.0 画像中的端点 ID 一致。
const officialCodexEndpointResponsesCompactForTest = "responses_compact"
