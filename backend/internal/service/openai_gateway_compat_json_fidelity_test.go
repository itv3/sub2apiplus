package service

import (
	"context"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// 9007199254740993 是 2^53+1，float64 无法精确表示；一旦走 map 往返就会变成
// 9007199254740992 或科学计数法。z_first/a_second 用来检出按字典序重排的嵌套键。
const (
	fidelityUnsafeInteger = "9007199254740993"
	fidelityFirstKey      = "z_first"
	fidelitySecondKey     = "a_second"
)

func requireCompatJSONFidelity(t *testing.T, outbound []byte) {
	t.Helper()
	payload := string(outbound)
	require.Contains(t, payload, fidelityUnsafeInteger,
		"大整数必须逐字节保留，不得因 map 往返降级为 float64")

	first := strings.Index(payload, fidelityFirstKey)
	second := strings.Index(payload, fidelitySecondKey)
	require.GreaterOrEqual(t, first, 0, "工具 schema 中的 %s 丢失", fidelityFirstKey)
	require.GreaterOrEqual(t, second, 0, "工具 schema 中的 %s 丢失", fidelitySecondKey)
	require.Less(t, first, second,
		"嵌套对象的键顺序必须保留原样，不得被 Go map 序列化按字典序重排")
}

// Chat Completions 入口：入站结构体用 json.RawMessage 保住了工具 schema 的原始字节，
// 转换阶段必须同样保真，否则第三方传入的数值与键顺序会在出站时被改写。
func TestOfficialEgressChatCompletionsPreservesJSONFidelity(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.6-luna",
		"stream":true,
		"messages":[{"role":"user","content":"FIDELITY_CC_OK"}],
		"tools":[{
			"type":"function",
			"function":{
				"name":"read_file",
				"description":"读取文件",
				"parameters":{
					"type":"object",
					"z_first":9007199254740993,
					"a_second":1,
					"properties":{"path":{"type":"string"}}
				}
			}
		}]
	}`)
	c := newOfficialOpenAIHTTPKiloContext(body, "fidelity-cc-session")
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_fidelity_cc"),
	}

	_, err := newOfficialOpenAIHTTPTestService(upstream).ForwardAsChatCompletions(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"",
		"gpt-5.6-luna",
	)

	require.NoError(t, err)
	require.NotNil(t, upstream.lastBody)
	requireCompatJSONFidelity(t, upstream.lastBody)
}

// Anthropic Messages 入口经同一条 codex transform，保真要求相同。
func TestOfficialEgressAnthropicMessagesPreservesJSONFidelity(t *testing.T) {
	body := []byte(`{
		"model":"gpt-5.6-luna",
		"stream":true,
		"max_tokens":128,
		"messages":[{"role":"user","content":[{"type":"text","text":"FIDELITY_MSG_OK"}]}],
		"tools":[{
			"name":"read_file",
			"description":"读取文件",
			"input_schema":{
				"type":"object",
				"z_first":9007199254740993,
				"a_second":1,
				"properties":{"path":{"type":"string"}}
			}
		}]
	}`)
	c := newOfficialOpenAIHTTPKiloContext(body, "fidelity-messages-session")
	c.Request.URL.Path = "/v1/messages"
	upstream := &httpUpstreamRecorder{
		resp: newOfficialOpenAIHTTPSSECompletedResponse("resp_fidelity_messages"),
	}

	_, err := newOfficialOpenAIHTTPTestService(upstream).ForwardAsAnthropic(
		context.Background(),
		c,
		newOfficialOpenAIHTTPTestAccount(94),
		body,
		"",
		"gpt-5.6-luna",
	)

	require.NoError(t, err)
	require.NotNil(t, upstream.lastBody)
	requireCompatJSONFidelity(t, upstream.lastBody)
}
