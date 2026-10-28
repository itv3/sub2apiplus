package service

import (
	"bytes"
	"context"
	"encoding/json"
	"math/rand"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/coder/websocket/wsjson"
	"github.com/stretchr/testify/require"
)

// 差分覆盖 RawMessage 的格式、转义、重复键和错误；普通对象仍走原编码器。
func TestOpenAIWSReadonlyJSONMatchesMarshal(t *testing.T) {
	values := []any{
		json.RawMessage(nil), json.RawMessage{}, json.RawMessage(`null`),
		json.RawMessage(` { "a":"\u0061\/\u003C","b":1.20e+04,"a":2 } `),
		json.RawMessage("{\"v\":\"<>&\u2028\u2029\"}"),
		json.RawMessage("{\"v\":\"\xff\"}"),
		json.RawMessage(`{"v":"\ud800","n":9007199254740993}`),
		json.RawMessage(`{"v":"\\\" \t\n <"}`),
		json.RawMessage(`{"broken":`), json.RawMessage(`{} {}`),
		map[string]any{"text": "<你好>", "n": json.Number("1.200")},
		[]byte("普通字节切片仍应编码为 base64"),
	}
	rng := rand.New(rand.NewSource(20261006))
	for i := 0; i < 400; i++ {
		raw := []byte(decodeRandomJSON(rng, 0))
		values = append(values, json.RawMessage(raw))
	}
	for _, value := range values {
		want, wantErr := json.Marshal(value)
		got, gotErr := marshalOpenAIWSReadonlyJSON(value)
		if wantErr != nil {
			require.EqualError(t, gotErr, wantErr.Error())
		} else {
			require.NoError(t, gotErr)
			require.Equal(t, string(want), string(got))
		}
	}
	raw := json.RawMessage(`{"input":"` + strings.Repeat("x", 1<<20) + `"}`)
	got, err := marshalOpenAIWSReadonlyJSON(raw)
	require.NoError(t, err)
	require.True(t, officialForwardSameBody(raw, got), "已定型的大帧应直接借用，不再复制")
}

// 用真实连接比较两种发送方式收到的完整消息，包括分段与压缩。预备帧发送与 wsjson.Write 的差别只有一处：
// 不追加末尾换行（官方客户端发送 serde_json::to_string 的结果，没有换行）。
func TestOpenAIWSPreparedJSONWireParity(t *testing.T) {
	for _, compression := range []coderws.CompressionMode{coderws.CompressionDisabled, coderws.CompressionNoContextTakeover} {
		t.Run(strconv.Itoa(int(compression)), func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			serverErrors := make(chan error, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: compression})
				if err != nil {
					serverErrors <- err
					return
				}
				defer func() { _ = conn.CloseNow() }()
				conn.SetReadLimit(8 << 20)
				for range 2 {
					kind, payload, readErr := conn.Read(ctx)
					if readErr != nil {
						serverErrors <- readErr
						return
					}
					if err := conn.Write(ctx, kind, payload); err != nil {
						serverErrors <- err
						return
					}
				}
				serverErrors <- nil
			}))
			defer server.Close()
			conn, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), &coderws.DialOptions{CompressionMode: compression})
			require.NoError(t, err)
			defer func() { _ = conn.CloseNow() }()
			conn.SetReadLimit(8 << 20)
			raw := []byte(` {"input":"` + strings.Repeat("abcdef", 64<<10) + `<>&","extra": {"b":2,"a":1}} `)
			original := bytes.Clone(raw)
			require.NoError(t, wsjson.Write(ctx, conn, json.RawMessage(raw)))
			_, want, err := conn.Read(ctx)
			require.NoError(t, err)
			client := &coderOpenAIWSClientConn{conn: conn}
			require.NoError(t, client.WritePreparedJSON(ctx, raw))
			_, got, err := conn.Read(ctx)
			require.NoError(t, err)
			require.Equal(t, strings.TrimSuffix(string(want), "\n"), string(got))
			require.NotEqual(t, byte('\n'), got[len(got)-1], "官方帧没有末尾换行")
			require.Equal(t, original, raw, "发送不能改写原文，否则重试和审计会看到被污染的内容")
			require.NoError(t, <-serverErrors)
		})
	}
}

// 官方帧的真实样本：官方客户端实采的 WS 预热帧（official-ws-default 的第一条 response.create，1505 字节，
// 压缩前的原文）。官方消息不以换行结尾。
const officialWSWarmupFrameSample = `{"type":"response.create","model":"gpt-6.1-sol","stream":true,"input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{"effort":"low","context":"all_turns"},"store":false,"include":["reasoning.encrypted_content"],"prompt_cache_key":"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95","text":{"verbosity":"low"},"generate":false,"client_metadata":{"x-codex-turn-metadata":"{\"installation_id\":\"aa8a2570-8c5c-4b1e-843d-34e98d058538\",\"session_id\":\"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95\",\"thread_id\":\"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95\",\"agent_name\":\"/root\",\"turn_id\":\"\",\"window_id\":\"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95:0\",\"window_number\":0,\"context_window_id\":\"01a17daa-cd79-7d21-8e6c-8e34d3bf67c6\",\"request_kind\":\"prewarm\",\"history_initialization\":\"new\",\"thread_source\":\"user\",\"sandbox\":\"none\",\"sandbox_mode\":\"danger-full-access\",\"auto_review_enabled\":false,\"node_repl_auto_review_required\":true,\"node_repl_disabled\":false,\"analytics_enabled\":true,\"model\":\"gpt-6.1-sol\",\"reasoning_effort\":\"low\"}","mcp_attribution":"{\"status\":\"none\"}","session_id":"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95","ws_request_header_x_openai_internal_codex_responses_lite":"true","turn_id":"","guardian_credits_requested":"true","thread_id":"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95","x-codex-window-id":"01a17daa-cd77-76c1-ab1e-58fd8f1e9d95:0","x-codex-installation-id":"aa8a2570-8c5c-4b1e-843d-34e98d058538","x-codex-ws-stream-request-start-ms":"1793109709805"}}`

// 预备帧发出的字节必须与官方样本逐字节相同：不追加换行（压缩开关、门槛各种组合下，对端解压后都一样）。
func TestOpenAIWSPreparedJSONSendsOfficialBytesWithoutTrailingNewline(t *testing.T) {
	for _, compression := range []coderws.CompressionMode{coderws.CompressionDisabled, coderws.CompressionContextTakeover} {
		t.Run(strconv.Itoa(int(compression)), func(t *testing.T) {
			ctx, cancel := context.WithTimeout(t.Context(), 10*time.Second)
			defer cancel()
			received := make(chan []byte, 1)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: compression})
				if err != nil {
					return
				}
				defer func() { _ = conn.CloseNow() }()
				_, payload, readErr := conn.Read(ctx)
				if readErr == nil {
					received <- payload
				}
			}))
			defer server.Close()
			conn, _, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(server.URL, "http"), &coderws.DialOptions{
				CompressionMode: compression, CompressionThreshold: officialOpenAIWSCompressionThresholdBytes,
			})
			require.NoError(t, err)
			defer func() { _ = conn.CloseNow() }()
			client := &coderOpenAIWSClientConn{conn: conn}
			require.NoError(t, client.WritePreparedJSON(ctx, []byte(officialWSWarmupFrameSample)))
			select {
			case got := <-received:
				require.Equal(t, officialWSWarmupFrameSample, string(got))
			case <-ctx.Done():
				t.Fatal("服务端没有收到消息")
			}
		})
	}
}
