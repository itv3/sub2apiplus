package officialegress

import (
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// 本文件提供“合成画像 Bundle”测试夹具：以正式目录里真实 release 的 ProfileSpec 为底稿，
// 应用调用方给出的快照改写（追加可选节、改条件、加槽位等），重新编译 ExecutableProfile，
// 再按端点 ID 重建每个 plan 的端点、传输与 H1 线序规则。
//
// 用途：在目标版本画像尚未进入正式目录前，验证“画像声明新节／新条件时”执行层的行为；
// 正式目录与原 Bundle 均不被修改，旧画像回归仍由原 Bundle 直接验证。

// syntheticCodexBundle 返回替换了可执行画像的 Bundle 副本。
func syntheticCodexBundle(
	t *testing.T,
	bundle ReleaseBundle,
	mutate func(*profilecontract.SnapshotDoc),
) ReleaseBundle {
	t.Helper()
	doc := bundle.release.profile.ToSnapshot()
	if mutate != nil {
		mutate(&doc)
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	if err != nil {
		t.Fatalf("合成画像 ProfileSpec 构造失败：%v", err)
	}
	executable, err := profilecontract.CompileExecutableProfile(spec)
	if err != nil {
		t.Fatalf("合成画像编译失败：%v", err)
	}
	out := bundle
	out.release.profile = spec
	out.release.executable = executable
	out.plans = make(map[string]ResolvedEndpointPlan, len(bundle.plans))
	for key, plan := range bundle.plans {
		endpoint, transport, resolveErr := resolveBundleProfileFacts(executable, plan.template.endpoint.ID)
		if resolveErr != nil {
			t.Fatalf("合成画像缺少端点 %s：%v", plan.template.endpoint.ID, resolveErr)
		}
		tls, tlsErr := compileTLSProfileSpec(executable, transport, endpoint.ID, endpoint.Path)
		if tlsErr != nil {
			t.Fatalf("合成画像 TLS 规则编译失败 %s：%v", endpoint.ID, tlsErr)
		}
		plan.template.endpoint = endpoint
		plan.template.transport = transport
		plan.template.tls = tls
		out.plans[key] = plan
	}
	return out
}

// syntheticPlanForEndpoint 在 Bundle 中找到承载指定端点的 plan。
func syntheticPlanForEndpoint(t *testing.T, bundle ReleaseBundle, endpointID string) ResolvedEndpointPlan {
	t.Helper()
	for _, plan := range bundle.EndpointPlans() {
		if plan.EndpointID() == endpointID {
			return plan
		}
	}
	t.Fatalf("Bundle 中没有端点 %s", endpointID)
	return ResolvedEndpointPlan{}
}

// syntheticSnapshotEndpoint 返回快照中指定端点的可写指针。
func syntheticSnapshotEndpoint(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
) *profilecontract.SnapshotEndpoint {
	t.Helper()
	for index := range doc.Endpoints {
		if doc.Endpoints[index].ID == endpointID {
			return &doc.Endpoints[index]
		}
	}
	t.Fatalf("快照中没有端点 %s", endpointID)
	return nil
}

// syntheticSetHeaderCondition 改写端点上指定 header 槽位的条件。
func syntheticSetHeaderCondition(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
	headerName string,
	condition profilecontract.ConditionKind,
) {
	t.Helper()
	endpoint := syntheticSnapshotEndpoint(t, doc, endpointID)
	for index := range endpoint.Headers {
		if endpoint.Headers[index].Name == headerName {
			endpoint.Headers[index].Condition = string(condition)
			return
		}
	}
	t.Fatalf("端点 %s 没有 header 槽位 %s", endpointID, headerName)
}

// syntheticSetHeaderSource 改写端点上指定 header 槽位的取值来源。
func syntheticSetHeaderSource(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
	headerName string,
	source profilecontract.ValueSource,
) {
	t.Helper()
	endpoint := syntheticSnapshotEndpoint(t, doc, endpointID)
	for index := range endpoint.Headers {
		if endpoint.Headers[index].Name == headerName {
			endpoint.Headers[index].Source = string(source)
			return
		}
	}
	t.Fatalf("端点 %s 没有 header 槽位 %s", endpointID, headerName)
}

// syntheticAddHeaderSlot 在端点上追加一个 header 槽位；WS 端点同时把名字追加到
// HeaderMapInsertionOrder 末尾（插入序以画像为准，这里只为让合成画像自洽）。
func syntheticAddHeaderSlot(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
	slot profilecontract.SnapshotHeaderSlot,
) {
	t.Helper()
	endpoint := syntheticSnapshotEndpoint(t, doc, endpointID)
	endpoint.Headers = append(endpoint.Headers, slot)
	if endpoint.HeaderOrderMode == string(profilecontract.HeaderOrderWsFixedPrefixThenHeaderMapSwapRemove) {
		endpoint.HeaderMapInsertionOrder = append(endpoint.HeaderMapInsertionOrder, slot.Name)
	}
}

// syntheticSetBodyFieldCondition 改写端点 body 字段的条件。
func syntheticSetBodyFieldCondition(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
	fieldName string,
	condition profilecontract.ConditionKind,
) {
	t.Helper()
	endpoint := syntheticSnapshotEndpoint(t, doc, endpointID)
	for index := range endpoint.Body.Fields {
		if endpoint.Body.Fields[index].Name == fieldName {
			endpoint.Body.Fields[index].Condition = string(condition)
			return
		}
	}
	t.Fatalf("端点 %s 没有 body 字段 %s", endpointID, fieldName)
}

// syntheticRawSection 把可选节序列化为快照原文。
func syntheticRawSection(t *testing.T, value any) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

// syntheticRemoveHeaderSlot 删除端点上指定名字的全部 header 槽位，并同步从
// HeaderMapInsertionOrder 中去掉该名字；端点本来没有该槽位时为空操作。用于把画像
// 还原成“未声明某个槽位”的旧形态。
func syntheticRemoveHeaderSlot(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
	headerName string,
) {
	t.Helper()
	endpoint := syntheticSnapshotEndpoint(t, doc, endpointID)
	kept := make([]profilecontract.SnapshotHeaderSlot, 0, len(endpoint.Headers))
	for _, slot := range endpoint.Headers {
		if !strings.EqualFold(slot.Name, headerName) {
			kept = append(kept, slot)
		}
	}
	endpoint.Headers = kept
	if len(endpoint.HeaderMapInsertionOrder) > 0 {
		order := make([]string, 0, len(endpoint.HeaderMapInsertionOrder))
		for _, name := range endpoint.HeaderMapInsertionOrder {
			if !strings.EqualFold(name, headerName) {
				order = append(order, name)
			}
		}
		endpoint.HeaderMapInsertionOrder = order
	}
}

// syntheticLegacyBundleForEndpoint 为“旧画像不受新结构影响、目标画像按新结构出站”一类
// 用例选出旧画像对照组，并返回承载 endpointID 的 Bundle。
//
// 这类用例改动前一律以正式目录的 Active 画像充当“旧画像”：候选期 Active 确实是旧画像，
// 目标画像只在 previous 槽位。VC-6 晋升后 Active 换成目标画像，它本身已经声明了被测的
// 新结构，“旧画像”前提随之失效。这里改为按结构事实选择，不写死槽位名或版本号：
//   - declares 判断画像是否声明了被测结构（可选节、槽位、条件、取值来源、传输参数等）；
//   - 按 Active、Previous 的顺序取正式目录中第一个未声明该结构的发布，原样返回它的真实
//     Bundle。候选期取到的是 Active，与改动前完全相同；晋升后取到的是 Previous 中的同一份
//     旧画像，对照组仍是真实旧画像，判别力不变；
//   - 两个槽位都已声明（旧画像随版本退休后）才以 Active 为底稿，用 strip 去掉该结构合成
//     旧形态，并要求合成结果确实不再声明，防止 strip 与 declares 口径漂移后对照组悄悄失效。
//
// 用例的“目标画像”一侧继续用 syntheticCodexBundle 在本函数返回的旧画像上追加新结构，
// 与改动前以 Active 旧画像为底稿的做法一致。
func syntheticLegacyBundleForEndpoint(
	t *testing.T,
	endpointID string,
	feature string,
	declares func(profilecontract.ExecutableProfile) bool,
	strip func(*profilecontract.SnapshotDoc),
) ReleaseBundle {
	t.Helper()
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, err := DefaultReleaseCatalog().Resolve(mode)
		if err != nil {
			t.Fatal(err)
		}
		if !declares(release.ExecutableProfile()) {
			bundle, _ := staticClosurePlanForEndpoint(t, mode, endpointID)
			return bundle
		}
	}
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, endpointID)
	legacy := syntheticCodexBundle(t, base, strip)
	if declares(legacy.release.executable) {
		t.Fatalf("以 Active 为底稿去掉「%s」后合成画像仍声明该结构：strip 与判定口径不一致，旧画像对照组无效", feature)
	}
	return legacy
}
