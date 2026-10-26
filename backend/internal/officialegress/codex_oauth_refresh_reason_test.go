package officialegress

import (
	"context"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// 官方 0.164 起，ChatGPT OAuth 令牌刷新请求体多一个 refresh_reason 字段（四值闭集）。
// 本用例在合成画像上验证最终编译字节：封闭 Body 声明了该字段时，键序为官方 BTreeMap 的字典序
// client_id, grant_type, refresh_reason, refresh_token（refresh_token 仍由 compiler 追加）；
// 没声明（旧画像）时多发字段会被封闭 Body 拒绝，旧语义 Body 的最终字节保持不变。
// 语义 Body 字面量与 service 包 openai_oauth_refresh_reason_test.go 的字面量一致。

// 官方 0.164.0 实采（非正式，ARM64，informal-capture 的 s1／s3／s5／s6 场景）的刷新请求体原文：
// 四个 refresh_reason 取值都实采到，键序恒为 client_id, grant_type, refresh_reason, refresh_token。
// 用例的期望字节直接取自实采，不是推导值。
const (
	oauthRefreshCapturedClientID     = "app_EMoamEEZ73f0CkXaXp7hrann"
	oauthRefreshCapturedRefreshToken = "rt_informal_seed"

	oauthRefreshLegacySemanticBody = `{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","grant_type":"refresh_token"}`
	// 0.163 画像（无 refresh_reason）的最终字节，同一套实采基线对照（b163-s1）。
	oauthRefreshLegacyWireBody = `{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","grant_type":"refresh_token","refresh_token":"rt_informal_seed"}`
)

// oauthRefreshCapturedBodies：reason → 实采到的完整请求体。
var oauthRefreshCapturedBodies = map[string]string{
	"proactive_expiry":      `{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","grant_type":"refresh_token","refresh_reason":"proactive_expiry","refresh_token":"rt_informal_seed"}`,
	"scheduled":             `{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","grant_type":"refresh_token","refresh_reason":"scheduled","refresh_token":"rt_informal_seed"}`,
	"unauthorized_recovery": `{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","grant_type":"refresh_token","refresh_reason":"unauthorized_recovery","refresh_token":"rt_informal_seed"}`,
	"forced":                `{"client_id":"app_EMoamEEZ73f0CkXaXp7hrann","grant_type":"refresh_token","refresh_reason":"forced","refresh_token":"rt_informal_seed"}`,
}

// oauthRefreshBundleWithReasonField 以 Active 画像为底稿，把 oauth_refresh 封闭 Body 改写成
// “声明 / 不声明 refresh_reason”两种形态（refresh_reason 紧挨 refresh_token 之前）。
func oauthRefreshBundleWithReasonField(t *testing.T, declare bool) ReleaseBundle {
	t.Helper()
	base, _ := staticClosurePlanForEndpoint(t, ReleaseModeActive, "oauth_refresh")
	return syntheticCodexBundle(t, base, func(doc *profilecontract.SnapshotDoc) {
		endpoint := syntheticSnapshotEndpoint(t, doc, "oauth_refresh")
		fields := make([]profilecontract.SnapshotBodyField, 0, len(endpoint.Body.Fields)+1)
		for _, field := range endpoint.Body.Fields {
			if field.Name == "refresh_reason" {
				continue
			}
			if field.Name == "refresh_token" && declare {
				fields = append(fields, profilecontract.SnapshotBodyField{Name: "refresh_reason", Required: true})
			}
			fields = append(fields, field)
		}
		endpoint.Body.Fields = fields
	})
}

func compileOAuthRefreshTestBody(t *testing.T, bundle ReleaseBundle, semanticBody string) (string, error) {
	t.Helper()
	plan := syntheticPlanForEndpoint(t, bundle, "oauth_refresh")
	target := staticClosureLegalTarget(plan.template)
	egressPlan := staticClosureEgressPlan(t, bundle, plan, target, "oauth-refresh-reason")
	egressPlan.Body = NewReplayableRequestBody([]byte(semanticBody))
	authentication, err := NewAttemptAuthentication(AttemptAuthenticationInput{
		BearerToken: "static-closure-token", RefreshToken: oauthRefreshCapturedRefreshToken,
	})
	if err != nil {
		t.Fatal(err)
	}
	egressPlan.Authentication = authentication
	execution, err := NewCompiler().Compile(context.Background(), bundle, egressPlan, EndpointDynamicInputs{})
	if err != nil {
		return "", err
	}
	body, ok := execution.request.body.replayableView()
	if !ok {
		t.Fatal("编译产物缺少可重放 Body")
	}
	return string(body), nil
}

func TestCompilerOAuthRefreshReasonKeyOrder(t *testing.T) {
	declared := oauthRefreshBundleWithReasonField(t, true)
	for reason, captured := range oauthRefreshCapturedBodies {
		wire, err := compileOAuthRefreshTestBody(t, declared,
			`{"client_id":"`+oauthRefreshCapturedClientID+`","grant_type":"refresh_token","refresh_reason":"`+reason+`"}`)
		if err != nil {
			t.Fatalf("声明 refresh_reason 的画像应能编译（%s）：%v", reason, err)
		}
		if wire != captured {
			t.Fatalf("refresh_reason=%s 最终字节与官方实采不符\n got: %s\nwant: %s", reason, wire, captured)
		}
	}
	// 语义 Body 里字段顺序不同，最终仍由画像字段序决定。
	wire, err := compileOAuthRefreshTestBody(t, declared,
		`{"refresh_reason":"forced","grant_type":"refresh_token","client_id":"`+oauthRefreshCapturedClientID+`"}`)
	if err != nil {
		t.Fatal(err)
	}
	if wire != oauthRefreshCapturedBodies["forced"] {
		t.Fatalf("字段序必须由画像决定\n got: %s\nwant: %s", wire, oauthRefreshCapturedBodies["forced"])
	}
	// 画像声明字段为必填：旧语义 Body（缺 refresh_reason）必须被拒绝，不能静默少发。
	if _, err := compileOAuthRefreshTestBody(t, declared, oauthRefreshLegacySemanticBody); err == nil {
		t.Fatal("画像声明 refresh_reason 为必填时，缺该字段的语义 Body 必须被拒绝")
	}
}

func TestCompilerOAuthRefreshLegacyProfileRejectsReasonAndKeepsLegacyBytes(t *testing.T) {
	legacy := oauthRefreshBundleWithReasonField(t, false)
	wire, err := compileOAuthRefreshTestBody(t, legacy, oauthRefreshLegacySemanticBody)
	if err != nil {
		t.Fatalf("旧画像应能编译旧语义 Body：%v", err)
	}
	if wire != oauthRefreshLegacyWireBody {
		t.Fatalf("旧画像最终字节必须不变\n got: %s\nwant: %s", wire, oauthRefreshLegacyWireBody)
	}
	if _, err := compileOAuthRefreshTestBody(t, legacy,
		`{"client_id":"`+oauthRefreshCapturedClientID+`","grant_type":"refresh_token","refresh_reason":"forced"}`); err == nil {
		t.Fatal("旧画像封闭 Body 未声明 refresh_reason，多发该字段必须被拒绝")
	}
}
