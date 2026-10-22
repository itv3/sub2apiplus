package service

import (
	"bytes"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// 本文件锁定 Lite 规范化的零解码预检：对全部形态组合，输出字节、changed 与错误文本都必须
// 与“总是解码”的旧实现一致。

var litePrecheckReasoning = []string{
	"", `"reasoning":null`, `"reasoning":{"effort":"medium"}`, `"reasoning":{"effort":"medium","context":"all_turns"}`,
	`"reasoning":{"context":"none"}`, `"reasoning":{"context":1}`, `"reasoning":"text"`, `"reasoning":[]`,
	`"reasoning":{"context":"all_turns","context":"none"}`, `"reasoning":{"context":"none","context":"all_turns"}`,
	`"reasoning":{"context":"all_turns"},"reasoning":null`, `"reasoning":null,"reasoning":{"context":"all_turns"}`,
	`"reasoning":{"context":"all_turns"}`, `"reasoning":{"context":" all_turns"}`,
}

var litePrecheckTools = []string{
	"", `"tools":null`, `"tools":[]`, `"tools":[{"type":"function","name":"a"}]`, `"tools":[{"type":" function ","name":"a"}]`,
	`"tools":[{"type":"namespace","name":"ns","tools":[{"type":"function","name":"x"}]}]`, `"tools":["shell"]`, `"tools":[" "]`,
	`"tools":[{"type":"web_search"}]`, `"tools":[{"name":"x"}]`, `"tools":[1]`, `"tools":{}`, `"tools":"x"`,
	`"tools":[{"type":"custom","name":"c"},{"type":"tool_search"}]`, `"tools":[{"type":"function","type":"namespace","name":"n"}]`,
	`"tools":[{"type":"namespace","type":"function","name":"n"}]`, `"tools":[{"type":""}]`, `"tools":[{"type":5}]`,
	`"tools":[{"type":"function","name":"a"}],"tools":null`, `"tools":null,"tools":[{"type":"function","name":"a"}]`,
	`"tools":[{"type":"function","name":"a"},null]`, `"tools":[{"type":"function","name":"a"}]`,
}

var litePrecheckParallel = []string{
	"", `"parallel_tool_calls":true`, `"parallel_tool_calls":false`, `"parallel_tool_calls":"false"`, `"parallel_tool_calls":null`,
	`"parallel_tool_calls":0`, `"parallel_tool_calls":true,"parallel_tool_calls":false`, `"parallel_tool_calls":false,"parallel_tool_calls":true`,
}

var litePrecheckInput = []string{
	"", `"input":"hi"`, `"input":[]`, `"input":null`, `"input":5`, `"input":[{"type":"message","role":"user","content":"x"}]`,
	`"input":[{"type":"additional_tools","role":"developer","tools":[{"type":"function","name":"t"}]}]`,
	`"input":[{"type":"additional_tools","tools":[]}]`, `"input":[{"type":"additional_tools","tools":"x"}]`,
	`"input":[{"type":" additional_tools ","tools":[{"type":"function","name":"t"}]}]`, `"input":[{"type":"additional_tools"}]`,
	`"input":[{"type":"message"},{"type":"additional_tools","tools":[{"type":"namespace","name":"ns"}]}]`, `"input":[1,"x"]`,
	`"input":[{"type":"additional_tools","tools":[{"type":"function","name":"t"}],"tools":[]}]`,
	`"input":[{"type":"message","type":"additional_tools","tools":[{"type":"function","name":"t"}]}]`,
	`"input":[{"type":"additional_tools","tools":[{"type":"function","name":"t"}]}],"input":[]`,
}

func litePrecheckCompare(t *testing.T, body []byte) {
	t.Helper()
	gotBody, gotChanged, gotErr := normalizeOpenAIResponsesLiteToolsPayload(body)
	wantBody, wantChanged, wantErr := legacyNormalizeOpenAIResponsesLiteToolsPayload(body)
	if (gotErr == nil) != (wantErr == nil) {
		t.Fatalf("错误有无不一致\nbody=%s\nnew=%v\nlegacy=%v", body, gotErr, wantErr)
	}
	if wantErr != nil {
		require.Equal(t, wantErr.Error(), gotErr.Error(), "错误文本不一致：%s", body)
	}
	require.Equal(t, wantChanged, gotChanged, "changed 不一致：%s", body)
	if !bytes.Equal(gotBody, wantBody) {
		t.Fatalf("输出不一致\nbody=%s\nnew=%s\nlegacy=%s", body, gotBody, wantBody)
	}
}

func TestResponsesLitePrecheckMatchesDecodePathOnAllCombinations(t *testing.T) {
	count := 0
	for _, reasoning := range litePrecheckReasoning {
		for _, tools := range litePrecheckTools {
			for _, parallel := range litePrecheckParallel {
				for _, input := range litePrecheckInput {
					parts := []string{`"model":"gpt-5.6-codex"`, `"stream":true`}
					for _, part := range []string{reasoning, tools, parallel, input} {
						if part != "" {
							parts = append(parts, part)
						}
					}
					body := []byte("{" + strings.Join(parts, ",") + "}")
					litePrecheckCompare(t, body)
					count++
				}
			}
		}
	}
	t.Logf("组合数 %d", count)
	// 非法与非对象正文
	for _, fixture := range []string{``, ` `, `{`, `null`, `[]`, `"x"`, `{"a":1,}`, `{"reasoning":{"context":"all_turns"}} x`} {
		litePrecheckCompare(t, []byte(fixture))
	}
}

func TestResponsesLitePrecheckDoesNotAmplifyNormalizedBody(t *testing.T) {
	var b strings.Builder
	_, _ = b.WriteString(`{"model":"gpt-5.6-codex","instructions":"i","reasoning":{"effort":"medium","summary":"auto","context":"all_turns"},"parallel_tool_calls":false,"tools":[{"type":"function","name":"shell","parameters":{"type":"object"}}],"input":[`)
	for i := 0; i < 64; i++ {
		if i > 0 {
			_, _ = b.WriteString(",")
		}
		_, _ = b.WriteString(`{"type":"message","role":"user","content":[{"type":"input_text","text":"`)
		_, _ = b.WriteString(strings.Repeat("x", 64*1024))
		_, _ = b.WriteString(`"}]}`)
	}
	_, _ = b.WriteString(`]}`)
	body := []byte(b.String())
	var out []byte
	var changed bool
	var err error
	// 满载时其它协程的分配会混进单次窗口，改用 testMeasureAllocatedBytes（窗口内只留一个 P、取多次最小值）。
	allocated := float64(testMeasureAllocatedBytes(t, func() {
		out, changed, err = normalizeOpenAIResponsesLiteToolsPayload(body)
	}))
	require.NoError(t, err)
	require.False(t, changed)
	require.True(t, bytes.Equal(out, body))
	t.Logf("正文 %.1f MiB：预检分配 %.2f MiB", float64(len(body))/1024/1024, allocated/1024/1024)
	require.Less(t, allocated, float64(len(body))/8, "已规范化的 Lite 正文不应再被解码")
}
