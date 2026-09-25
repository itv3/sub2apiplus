package officialegress

import (
	"encoding/json"
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
