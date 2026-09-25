package officialegress

import (
	"context"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// TLS 签名算法由画像驱动（VC-4 第 7 项回归断言）：目标版本 WS 传输在原列表之后追加
// ML-DSA-44/65/87（2308、2309、2310），Bundle 模板与编译产物的 TLS 事实逐项、按序
// 携带；旧画像的 WS 传输列表不变，HTTP 传输（已含 2308～2310）不受影响。
var mlDSASignatureAlgorithms = []uint16{2308, 2309, 2310}

func TestWebSocketTransportSignatureAlgorithmsFollowProfile(t *testing.T) {
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "responses_ws")
	legacyPlan := syntheticPlanForEndpoint(t, base, "responses_ws")
	legacy := append([]uint16(nil), legacyPlan.template.transport.SignatureAlgorithms...)
	if len(legacy) == 0 {
		t.Fatal("旧画像 WS 传输缺少签名算法")
	}
	for _, value := range mlDSASignatureAlgorithms {
		if slices.Contains(legacy, value) {
			t.Fatalf("旧画像 WS 传输不应包含 %d", value)
		}
	}
	if !slices.Equal(legacyPlan.template.tls.SignatureAlgorithms, legacy) {
		t.Fatal("旧画像 Bundle TLS 事实必须逐项等于传输画像")
	}

	synthetic := syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		for index := range doc.Transports {
			if doc.Transports[index].Protocol == "websocket" {
				doc.Transports[index].SignatureAlgorithms = append(
					append([]uint16(nil), doc.Transports[index].SignatureAlgorithms...), mlDSASignatureAlgorithms...,
				)
			}
		}
	})
	want := append(append([]uint16(nil), legacy...), mlDSASignatureAlgorithms...)
	plan := syntheticPlanForEndpoint(t, synthetic, "responses_ws")
	if !slices.Equal(plan.template.tls.SignatureAlgorithms, want) {
		t.Fatalf("目标画像 Bundle TLS 事实未按序追加 ML-DSA：%v", plan.template.tls.SignatureAlgorithms)
	}
	egressPlan := staticClosureEgressPlan(t, synthetic, plan, staticClosureLegalTarget(plan.template), "sigalgs-ws")
	egressPlan.Body = NewReplayableRequestBody(nil)
	execution, err := NewCompiler().Compile(context.Background(), synthetic, egressPlan, EndpointDynamicInputs{})
	if err != nil {
		t.Fatalf("目标画像 WS 握手编译失败：%v", err)
	}
	if !slices.Equal(execution.transport.TLS.SignatureAlgorithms, want) {
		t.Fatalf("编译产物 TLS 签名算法与画像不一致：%v", execution.transport.TLS.SignatureAlgorithms)
	}
	for _, candidate := range synthetic.EndpointPlans() {
		if candidate.template.transport.Protocol == "http/1.1" &&
			!slices.Equal(candidate.template.transport.SignatureAlgorithms, syntheticPlanForEndpoint(t, base, candidate.EndpointID()).template.transport.SignatureAlgorithms) {
			t.Fatalf("HTTP 传输 %s 的签名算法不应受 WS 画像改动影响", candidate.EndpointID())
		}
	}
}
