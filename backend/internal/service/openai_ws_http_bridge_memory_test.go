package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"math/rand"
	"net/http"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
	"github.com/tidwall/sjson"
)

func TestOpenAIWSHTTPBridgeRawFieldMatchesLegacy(t *testing.T) {
	for _, input := range []string{
		`{"input":[{"text":"正文"}],"tools":[ {"type":"function", "name":"f"} ]}`,
		`{"tools":[1],"tools":null}`, `{"tools":null,"to\u006fls":["last"]}`,
		`{"tools":"\u0061<>&"}`, `{"input":[]}`, `null`, `[]`, `{"tools":`,
	} {
		var fields map[string]json.RawMessage
		err := json.Unmarshal([]byte(input), &fields)
		want, present := fields["tools"]
		if err != nil {
			want, present = nil, false
		}
		got, exists := openAIWSHTTPBridgeRawField([]byte(input), "tools")
		require.Equal(t, present, exists, input)
		require.Equal(t, []byte(want), []byte(got), input)
	}
	body := []byte(`{"tools":["value"]}`)
	got, _ := openAIWSHTTPBridgeRawField(body, "tools")
	got[2] = 'X'
	require.Equal(t, `{"tools":["value"]}`, string(body), "跨 turn 保留的工具定义必须独立持有")
}

func TestPrepareOpenAIWSHTTPBridgeSharedDecodeMatchesLegacy(t *testing.T) {
	fixtures := [][]byte{
		[]byte(`{"type":"response.create","generate":false,"previous_response_id":"old","input":[{"z":1,"a":"<>&\u0061\n","big":9007199254740993,"decimal":1e3}],"stream":false}`),
		[]byte(`{"input":[{"type":"message","content":"old","content":"last"}],"model":"old","model":"new","reasoning":{"effort":"none"}}`),
		[]byte(`{"type":"response.create"}{"trailing":true}`), []byte(`[]`), []byte(`null`), []byte(`{"input":`),
		buildOfficialEgressMemoryProfileBody(t, 128<<10),
	}
	account := newOfficialOpenAIHTTPTestAccount(94)
	for i, fixture := range fixtures {
		t.Run(fmt.Sprintf("fixture-%d", i), func(t *testing.T) {
			want, wantErr := prepareOpenAIWSHTTPBridgeBody(account, fixture)
			_, workspace := newOfficialForwardHTTPBody(t.Context(), fixture)
			got, gotErr := prepareOpenAIWSHTTPBridgeBodyWithWorkspace(account, fixture, workspace)
			if gotErr == nil && workspace.hasUnmaterializedWSHTTPBridgeBody(got) {
				require.True(t, officialForwardSameBody(got, fixture), "普通官方桥接准备阶段保留入口正文")
				got, gotErr = workspace.materializeWSHTTPBridgeBody(got)
			}
			require.Equal(t, fmt.Sprint(wantErr), fmt.Sprint(gotErr))
			require.Equal(t, want, got, "桥接准备必须保持 json.Marshal 原有键序、HTML 转义与数字字面值")
		})
	}
}

func TestOfficialEgressWSHTTPBridgeMatchesLegacyWire(t *testing.T) {
	for _, fixture := range []struct {
		name     string
		explicit bool
		retry    bool
		mutate   func(*testing.T, []byte) []byte
	}{
		{name: "native"},
		{name: "explicit", explicit: true},
		{name: "rejected_status", retry: true},
		{name: "stream_false", mutate: func(t *testing.T, body []byte) []byte {
			body, err := sjson.SetBytes(body, "stream", false)
			require.NoError(t, err)
			return body
		}},
		{name: "escaped_values", mutate: func(t *testing.T, body []byte) []byte {
			body, err := sjson.SetRawBytes(body, "input.2.content", []byte(`[{"type":"input_text","text":"<>&\u0061\n\u2028"}]`))
			require.NoError(t, err)
			return body
		}},
		{name: "nested_duplicate", mutate: func(t *testing.T, body []byte) []byte {
			body, err := sjson.SetRawBytes(body, "input.2", []byte(`{"type":"message","role":"assistant","role":"user","content":"first","content":"last"}`))
			require.NoError(t, err)
			return body
		}},
		{name: "invalid_utf8", mutate: func(t *testing.T, body []byte) []byte {
			body, err := sjson.SetRawBytes(body, "input.2.content", []byte("\"\xff\""))
			require.NoError(t, err)
			return body
		}},
		{name: "namespace", mutate: func(t *testing.T, body []byte) []byte {
			body, err := sjson.SetRawBytes(body, "tools", []byte(`[{"type":"namespace","name":"n","tools":[{"type":"function","name":"f","parameters":{"type":"object"}}]}]`))
			require.NoError(t, err)
			return body
		}},
		{name: "non_lite", mutate: func(t *testing.T, body []byte) []byte {
			body, err := sjson.SetBytes(body, "model", "gpt-5.4")
			require.NoError(t, err)
			return body
		}},
	} {
		t.Run(fixture.name, func(t *testing.T) {
			source := newOfficialOpenAIHTTPTestBody(t, true, fixture.explicit, true)
			if fixture.mutate != nil {
				source = fixture.mutate(t, source)
			}
			source = officialEgressWSHTTPBridgePayload(t, source)
			run := func(disabled bool) (officialForwardBodyOutcome, [][]byte) {
				body := append([]byte(nil), source...)
				c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
				c.Set(officialEgressInvocationGinKey, "bridge-memory-"+fixture.name)
				recorder := &officialForwardBodyRecorder{fallback: officialForwardBodySSE("resp_bridge_memory")}
				if fixture.retry {
					recorder.responses = []func() *http.Response{officialForwardBodyJSON(http.StatusBadRequest,
						`{"error":{"code":"unsupported_parameter","message":"Unsupported parameter: input[3].status","param":"input[3].status","type":"invalid_request_error"}}`)}
				}
				service := officialEgressWSHTTPBridgeTestService(recorder)
				service.openaiModelCapabilities.replaceFromManifest(94, []byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true},{"slug":"gpt-5.4","use_responses_lite":false}]}`))
				ctx := context.Background()
				if disabled {
					ctx = withOfficialForwardHTTPBodyDisabled(ctx)
				}
				var writes [][]byte
				result, err := service.proxyOpenAIWSHTTPBridgeTurn(ctx, c, newOfficialOpenAIHTTPTestAccount(94), "oauth-token", body, len(body), "gpt-5.6-luna", "", "", "", "", 1, func(message []byte) error {
					writes = append(writes, append([]byte(nil), message...))
					return nil
				})
				return officialForwardBodyOutcome{calls: recorder.calls, result: result, err: err}, writes
			}
			legacy, legacyMessages := run(true)
			current, currentMessages := run(false)
			require.NoError(t, legacy.err)
			require.NoError(t, current.err)
			require.Equal(t, legacyMessages, currentMessages, "转发给 WS 客户端的事件保持一致")
			require.NotEmpty(t, current.calls)
			require.Len(t, current.calls, len(legacy.calls))
			if fixture.retry {
				require.Len(t, current.calls, 2)
			}
			for i, want := range legacy.calls {
				got := current.calls[i]
				require.Equal(t, want.method, got.method)
				require.Equal(t, want.url, got.url)
				require.Equal(t, want.host, got.host)
				require.Equal(t, want.header, got.header)
				require.Equal(t, want.contentLength, got.contentLength)
				require.Equal(t, want.decoded, got.decoded)
				require.Equal(t, want.wire, got.wire)
				require.Equal(t, want.tlsProfile, got.tlsProfile)
			}
			require.NotNil(t, current.result)
			legacy.result.Duration, current.result.Duration = 0, 0
			legacy.result.FirstTokenMs, current.result.FirstTokenMs = nil, nil
			require.Equal(t, legacy.result, current.result)
		})
	}
}

func TestPrepareOpenAIWSHTTPBridgePreservesBodyReaderSemantics(t *testing.T) {
	account := newOfficialOpenAIHTTPTestAccount(94)
	for _, source := range [][]byte{
		[]byte(`{"input":[{"type":"message","role":"assistant","role":"user","content":"first","content":"last"}]}`),
		[]byte("{\"input\":[{\"type\":\"message\",\"role\":\"user\",\"content\":\"\xff\"}]}"),
	} {
		_, workspace := newOfficialForwardHTTPBody(t.Context(), source)
		got, err := prepareOpenAIWSHTTPBridgeBodyWithWorkspace(account, source, workspace)
		require.NoError(t, err)
		require.Nil(t, workspace.bridge, "原文与规范化后的只读字段不等价时须立即物化")
		want, err := prepareOpenAIWSHTTPBridgeBody(account, source)
		require.NoError(t, err)
		require.Equal(t, want, got)
	}
}

func TestPrepareOpenAIWSHTTPBridgeMaterializationKeepsRetryVersion(t *testing.T) {
	source := newOfficialOpenAIHTTPTestBody(t, true, false, true)
	_, workspace := newOfficialForwardHTTPBody(t.Context(), source)
	account := newOfficialOpenAIHTTPTestAccount(94)
	body, err := prepareOpenAIWSHTTPBridgeBodyWithWorkspace(account, source, workspace)
	require.NoError(t, err)
	require.True(t, workspace.hasUnmaterializedWSHTTPBridgeBody(body))
	prepared, err := workspace.materializeWSHTTPBridgeBody(body)
	require.NoError(t, err)
	retry, err := sjson.DeleteBytes(prepared, "input.3.status")
	require.NoError(t, err)
	got, err := workspace.materializeWSHTTPBridgeBody(retry)
	require.NoError(t, err)
	require.Equal(t, retry, got, "按需物化的缓存不能覆盖后续错误重试已删除的字段")
}

func TestOpenAIWSHTTPBridgeCanonicalSegmentsMatchJSONMarshal(t *testing.T) {
	fixtures := [][]byte{
		[]byte(`{"z":"\u0061\/\n<>&\u2028\u2029","a":9007199254740993,"b":1e3,"c":-0,"d":null}`),
		[]byte(`{"k":"first","k":"last","o":{"z":false,"a":[true,null,{"y":2,"a":1}]}}`),
		[]byte(`{"broken_unicode":"\ud800","escaped":"\u0061","name\u0061":4}`),
		[]byte("{\"invalid_utf8\":\"\xff\"}"),
		[]byte(`{"type":"message","content":"` + strings.Repeat("共享长字符串", 4096) + `"}`),
	}
	rng := rand.New(rand.NewSource(20261006))
	for i := 0; i < 100; i++ {
		encoded, err := json.Marshal(spliceRandomValue(rng, 3))
		require.NoError(t, err)
		fixtures = append(fixtures, encoded)
	}
	for i, fixture := range fixtures {
		index, err := buildOfficialJSONRawIndexForDecode(fixture)
		require.NoError(t, err)
		var decoded any
		require.NoError(t, decodeOpenAIJSONUseNumber(fixture, &decoded))
		want, err := json.Marshal(decoded)
		require.NoError(t, err)
		writer := openAIWSHTTPBridgeJSONSegments{}
		require.NoError(t, writer.appendCanonicalNode(index, index.root))
		writer.flush()
		require.Equal(t, want, bytes.Join(writer.segments, nil), "fixture=%d", i)
		if i == 4 {
			shared := 0
			for _, segment := range writer.segments {
				if officialForwardOffsetWithin(segment, fixture) >= 0 {
					shared += len(segment)
				}
			}
			require.Greater(t, shared, len(fixture)-64, "长字符串 token 应共享原帧，不能重新复制")
		}
	}
}
