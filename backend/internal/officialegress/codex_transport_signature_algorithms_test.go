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

// codexWebSocketTransportDeclaresMLDSA 判断画像的 WS 传输是否已在签名算法中声明任一 ML-DSA。
func codexWebSocketTransportDeclaresMLDSA(profile profilecontract.ExecutableProfile) bool {
	for _, transport := range profile.Transports() {
		if transport.Protocol != "websocket" {
			continue
		}
		for _, value := range mlDSASignatureAlgorithms {
			if slices.Contains(transport.SignatureAlgorithms, value) {
				return true
			}
		}
	}
	return false
}

// stripWebSocketMLDSA 从画像的 WS 传输签名算法中去掉 ML-DSA，其余算法与顺序不变。
func stripWebSocketMLDSA(doc *profilecontract.SnapshotDoc) {
	for index := range doc.Transports {
		if doc.Transports[index].Protocol != "websocket" {
			continue
		}
		kept := make([]uint16, 0, len(doc.Transports[index].SignatureAlgorithms))
		for _, value := range doc.Transports[index].SignatureAlgorithms {
			if !slices.Contains(mlDSASignatureAlgorithms, value) {
				kept = append(kept, value)
			}
		}
		doc.Transports[index].SignatureAlgorithms = kept
	}
}

// TestWebSocketTransportSignatureAlgorithmsFollowProfile 的“旧画像”一侧改动前直接取 Active
// 画像；晋升后 Active 是 WS 传输已含 ML-DSA 的目标画像，旧画像前提失效。现按结构事实选出
// WS 传输未声明 ML-DSA 的真实发布作为旧画像（候选期是 Active，晋升后是 Previous 中同一份
// 旧画像）；都已声明时去掉 ML-DSA 合成旧形态。目标一侧仍在旧画像上追加，断言原样保留，
// 末尾“HTTP 传输不受影响”的对照同样以该旧画像为基准。
func TestWebSocketTransportSignatureAlgorithmsFollowProfile(t *testing.T) {
	base := syntheticLegacyBundleForEndpoint(t, "responses_ws", "WS 传输的 ML-DSA 签名算法",
		codexWebSocketTransportDeclaresMLDSA, stripWebSocketMLDSA)
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
