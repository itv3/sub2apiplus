package officialegress

import (
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func TestCompilerBodyAuthorityRejectsDuplicateJSONFields(t *testing.T) {
	contract := profilecontract.BodyContractProfile{
		Encoding: profilecontract.BodyJson,
		Closed:   true,
		Fields: []profilecontract.BodyFieldProfile{
			{Name: "model", Required: true},
		},
	}
	_, err := orderJSONBodyWithPolicy(
		[]byte(`{"model":"first","model":"second"}`), contract,
		profilecontract.FeatureDefaults{}, CodexRequestConditions{},
		BodyRuntimeConditions{}, AttemptAuthenticationInput{},
	)
	if err == nil || !strings.Contains(err.Error(), "字段重复") {
		t.Fatalf("重复 JSON 字段未 fail-close：%v", err)
	}
}

func TestCompilerBodyAuthorityAppliesConditionsOmitAndClosedSet(t *testing.T) {
	t.Run("instructions_empty_string", func(t *testing.T) {
		contract := profilecontract.BodyContractProfile{
			Encoding: profilecontract.BodyJson, Closed: true,
			Fields: []profilecontract.BodyFieldProfile{
				{Name: "model", Required: true},
				{Name: "instructions", OmitWhen: profilecontract.OmitEmptyString},
			},
		}
		got := compileBodyAuthorityTestJSON(t, []byte(`{"instructions":"","model":"gpt"}`), contract, BodyRuntimeConditions{})
		if string(got) != `{"model":"gpt"}` {
			t.Fatalf("empty_string 未按画像省略：%s", got)
		}
	})

	t.Run("credit_id_condition_disabled", func(t *testing.T) {
		contract := profilecontract.BodyContractProfile{
			Encoding: profilecontract.BodyJson, Closed: true,
			Fields: []profilecontract.BodyFieldProfile{
				{Name: "redeem_request_id", Required: true},
				{Name: "credit_id", Condition: profilecontract.ConditionCreditIdPresent, OmitWhen: profilecontract.OmitNone},
			},
		}
		_, err := orderJSONBodyWithPolicy(
			[]byte(`{"redeem_request_id":"redeem","credit_id":"credit"}`), contract,
			profilecontract.FeatureDefaults{}, CodexRequestConditions{},
			BodyRuntimeConditions{}, AttemptAuthenticationInput{},
		)
		if err == nil || !strings.Contains(err.Error(), "条件字段未启用") {
			t.Fatalf("未启用 credit_id 未拒绝：%v", err)
		}
	})

	t.Run("previous_response_unreusable", func(t *testing.T) {
		contract := profilecontract.BodyContractProfile{
			Encoding: profilecontract.BodyWebsocketJson, Closed: true,
			Fields: []profilecontract.BodyFieldProfile{
				{Name: "type", Required: true},
				{Name: "previous_response_id", OmitWhen: profilecontract.OmitNoneOrUnreusablePrefix},
				{Name: "input", Required: true},
			},
		}
		got := compileBodyAuthorityTestJSON(
			t, []byte(`{"previous_response_id":"resp_old","input":[],"type":"response.create"}`),
			contract, BodyRuntimeConditions{PreviousResponseIDReusable: false},
		)
		if string(got) != `{"type":"response.create","input":[]}` {
			t.Fatalf("不可复用 previous_response_id 未省略：%s", got)
		}
	})

	t.Run("unknown_closed_field", func(t *testing.T) {
		contract := profilecontract.BodyContractProfile{
			Encoding: profilecontract.BodyJson, Closed: true,
			Fields: []profilecontract.BodyFieldProfile{{Name: "model", Required: true}},
		}
		_, err := orderJSONBodyWithPolicy(
			[]byte(`{"model":"gpt","unknown":true}`), contract,
			profilecontract.FeatureDefaults{}, CodexRequestConditions{},
			BodyRuntimeConditions{}, AttemptAuthenticationInput{},
		)
		if err == nil || !strings.Contains(err.Error(), "闭集外字段") {
			t.Fatalf("闭集外字段未拒绝：%v", err)
		}
	})
}

func TestCompilerBodyAuthorityPreservesOpenWebSocketUnknownFieldOrder(t *testing.T) {
	contract := profilecontract.BodyContractProfile{
		Encoding: profilecontract.BodyWebsocketDiscriminatedEvents,
		Closed:   false,
		Fields:   []profilecontract.BodyFieldProfile{{Name: "type", Required: true}},
	}
	got := compileBodyAuthorityTestJSON(
		t,
		[]byte(`{"zeta":1,"type":"session.update","alpha":{"b":2},"middle":true}`),
		contract,
		BodyRuntimeConditions{},
	)
	var ordered []string
	fields, err := compilerDecodeOrderedJSONObject(got)
	if err != nil {
		t.Fatal(err)
	}
	for _, field := range fields {
		ordered = append(ordered, field.name)
	}
	if want := []string{"type", "zeta", "alpha", "middle"}; !reflect.DeepEqual(ordered, want) {
		t.Fatalf("开放 WS event 未保持未知字段输入顺序：got=%v want=%v body=%s", ordered, want, got)
	}
	var decoded map[string]any
	if err := json.Unmarshal(got, &decoded); err != nil {
		t.Fatal(err)
	}
}

func compileBodyAuthorityTestJSON(
	t *testing.T,
	raw []byte,
	contract profilecontract.BodyContractProfile,
	conditions BodyRuntimeConditions,
) []byte {
	t.Helper()
	got, err := orderJSONBodyWithPolicy(
		raw, contract, profilecontract.FeatureDefaults{}, CodexRequestConditions{},
		conditions, AttemptAuthenticationInput{},
	)
	if err != nil {
		t.Fatal(err)
	}
	return got
}
