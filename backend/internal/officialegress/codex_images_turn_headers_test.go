package officialegress

import (
	"context"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// 独立 images 端点的回合头（官方 ext/image-generation 的 image_request_headers）：
//   - x-codex-image-turn-id：取 TurnID 事实（与 turn metadata 里的 turn_id 同源）；
//   - x-codex-turn-metadata：取 TurnMetadata 事实（0.164 新增）。
//
// 两个头只在画像为 images 端点声明了槽位时出站。本用例在合成画像上验证最终编译结果；
// 旧画像（未声明槽位）不出站这两个头，且 TurnID／TurnMetadata 事实存在与否都不改变任何字节。

func imagesBundleWithTurnHeaders(t *testing.T, endpointID string, imageTurnID, turnMetadata bool) ReleaseBundle {
	t.Helper()
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, endpointID)
	return syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		endpoint := syntheticSnapshotEndpoint(t, doc, endpointID)
		kept := make([]profilecontract.SnapshotHeaderSlot, 0, len(endpoint.Headers)+2)
		for _, slot := range endpoint.Headers {
			if slot.Name == "x-codex-image-turn-id" || slot.Name == "x-codex-turn-metadata" {
				continue
			}
			kept = append(kept, slot)
		}
		// 官方实采（informal-capture/images）的请求头顺序：version、x-codex-image-turn-id、
		// x-codex-turn-metadata、authorization……两个新头落在 version（Slot 10）与 authorization（Slot 30）之间。
		next := func(slotNumber int, name string) profilecontract.SnapshotHeaderSlot {
			return profilecontract.SnapshotHeaderSlot{
				Slot: slotNumber, Name: name, WireName: name,
				Source: string(profilecontract.SourceTurn), Condition: string(profilecontract.ConditionAlways),
			}
		}
		if imageTurnID {
			kept = append(kept, next(20, "x-codex-image-turn-id"))
		}
		if turnMetadata {
			kept = append(kept, next(25, "x-codex-turn-metadata"))
		}
		endpoint.Headers = kept
	})
}

func compileImagesTurnHeaderRequest(
	t *testing.T,
	bundle ReleaseBundle,
	endpointID string,
	mutateFacts func(*CodexIdentityFacts),
) (map[string]string, error) {
	t.Helper()
	plan := syntheticPlanForEndpoint(t, bundle, endpointID)
	target := staticClosureLegalTarget(plan.template)
	egressPlan := staticClosureEgressPlan(t, bundle, plan, target, "images-turn-headers")
	if mutateFacts != nil {
		mutateFacts(&egressPlan.IdentityFacts)
	}
	execution, err := NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
	if err != nil {
		return nil, err
	}
	headers := map[string]string{}
	for name := range execution.request.Headers() {
		headers[strings.ToLower(name)] = execution.request.Headers().Get(name)
	}
	return headers, nil
}

func TestCompilerImagesTurnHeadersFollowProfile(t *testing.T) {
	const turnMetadata = `{"session_id":"s","thread_id":"t","turn_id":"synthetic-turn"}`
	for _, endpointID := range []string{"images_generations", "images_edits"} {
		withFacts := func(facts *CodexIdentityFacts) {
			facts.TurnID = promptCacheKeyTestValue(t, "synthetic-turn", IdentitySourceTurn, IdentityLifecycleTurn)
			facts.TurnMetadata = promptCacheKeyTestValue(t, turnMetadata, IdentitySourceTurn, IdentityLifecycleTurn)
		}

		// 旧画像：两个头都不出站，事实有没有都一样。
		legacy := imagesBundleWithTurnHeaders(t, endpointID, false, false)
		plain, err := compileImagesTurnHeaderRequest(t, legacy, endpointID, nil)
		if err != nil {
			t.Fatalf("%s 旧画像编译失败：%v", endpointID, err)
		}
		withLegacyFacts, err := compileImagesTurnHeaderRequest(t, legacy, endpointID, withFacts)
		if err != nil {
			t.Fatalf("%s 旧画像（带回合事实）编译失败：%v", endpointID, err)
		}
		for _, headers := range []map[string]string{plain, withLegacyFacts} {
			if _, present := headers["x-codex-image-turn-id"]; present {
				t.Fatalf("%s 旧画像不得出站 x-codex-image-turn-id", endpointID)
			}
			if _, present := headers["x-codex-turn-metadata"]; present {
				t.Fatalf("%s 旧画像不得出站 x-codex-turn-metadata", endpointID)
			}
		}
		if len(plain) != len(withLegacyFacts) {
			t.Fatalf("%s 旧画像 Header 集合因回合事实变化：%v / %v", endpointID, plain, withLegacyFacts)
		}

		// 两个槽位都声明：取各自的事实。
		both := imagesBundleWithTurnHeaders(t, endpointID, true, true)
		headers, err := compileImagesTurnHeaderRequest(t, both, endpointID, withFacts)
		if err != nil {
			t.Fatalf("%s 目标画像编译失败：%v", endpointID, err)
		}
		if headers["x-codex-image-turn-id"] != "synthetic-turn" {
			t.Fatalf("%s x-codex-image-turn-id 必须取 TurnID 事实：%q", endpointID, headers["x-codex-image-turn-id"])
		}
		if headers["x-codex-turn-metadata"] != turnMetadata {
			t.Fatalf("%s x-codex-turn-metadata 必须取 TurnMetadata 事实：%q", endpointID, headers["x-codex-turn-metadata"])
		}

		// 两个槽位彼此独立，登记时可以只声明其一。
		onlyTurnID := imagesBundleWithTurnHeaders(t, endpointID, true, false)
		headers, err = compileImagesTurnHeaderRequest(t, onlyTurnID, endpointID, withFacts)
		if err != nil {
			t.Fatal(err)
		}
		if headers["x-codex-image-turn-id"] != "synthetic-turn" {
			t.Fatalf("%s 仅声明 image-turn-id 槽位时应出站该头", endpointID)
		}
		if _, present := headers["x-codex-turn-metadata"]; present {
			t.Fatalf("%s 未声明 turn-metadata 槽位时不得出站该头", endpointID)
		}

		// 声明了槽位但缺事实：失败关闭，不能静默少发。
		if _, err := compileImagesTurnHeaderRequest(t, both, endpointID, func(facts *CodexIdentityFacts) {
			facts.TurnID = CodexIdentityValue{}
		}); err == nil {
			t.Fatalf("%s 声明 x-codex-image-turn-id 槽位却缺 TurnID 事实时必须失败关闭", endpointID)
		}
	}
}
