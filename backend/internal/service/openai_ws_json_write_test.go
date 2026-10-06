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

// 用真实连接比较两种发送方式收到的完整消息，包括分段、压缩和末尾换行。
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
			require.Equal(t, string(want), string(got))
			require.Equal(t, byte('\n'), got[len(got)-1])
			require.Equal(t, original, raw, "发送不能改写原文，否则重试和审计会看到被污染的内容")
			require.NoError(t, <-serverErrors)
		})
	}
}
