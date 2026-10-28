package service

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"

	coderws "github.com/coder/websocket"
)

// openAIWSPreparedJSONWriter 在同步写入期间借用不可变的 JSON 字节。调用返回后
// 不再持有正文；不支持此能力的兼容连接继续使用原来的 WriteJSON 接口。
type openAIWSPreparedJSONWriter interface {
	WritePreparedJSON(context.Context, []byte) error
}

// marshalOpenAIWSReadonlyJSON 与 SetEscapeHTML(false) 的 json 编码器的字节及错误语义一致。正式帧已经
// 是 JSON，常见的紧凑形态只需校验，不再交给通用编码器重建整帧。
//
// 不做 HTML 转义：官方客户端用 serde_json 序列化，字符串里的 `<`、`>`、`&` 和 U+2028／U+2029 都按字面量写出
// （实采的官方消息里有大量字面量 `<`，例如 `<environment_context>` 之类标签）；这里以前用 json.Marshal
// 的语义把它们改成 \u003c 等，官方帧里不会有。本函数只用于官方出站的帧编译输入和最终写入，
// 不参与响应解析、用量或计费读取。
// 返回值可能引用输入，调用方必须保持正文只读，且不能将它存入跨轮可变缓存。
func marshalOpenAIWSReadonlyJSON(value any) ([]byte, error) {
	raw, ok := value.(json.RawMessage)
	if !ok || len(raw) == 0 || !json.Valid(raw) {
		return marshalOpenAIUpstreamJSON(value)
	}
	compact := false
	inString := false
	for i := 0; i < len(raw); i++ {
		c := raw[i]
		if inString {
			if c == '\\' {
				i++
				continue
			}
			if c == '"' {
				inString = false
			}
		} else if c == '"' {
			inString = true
		} else if c == ' ' || c == '\n' || c == '\r' || c == '\t' {
			compact = true
		}
	}
	result := []byte(raw)
	if compact {
		var buffer bytes.Buffer
		if err := json.Compact(&buffer, raw); err != nil {
			return marshalOpenAIUpstreamJSON(value)
		}
		result = buffer.Bytes()
	}
	return result[:len(result):len(result)], nil
}

// writeOpenAIPreparedJSON 只缩短受信传输层的编码路径，不绕过 Executor 的帧编译、
// 身份校验和一次性写能力。兼容实现获得独立副本，延续旧接口允许其保留正文的约定。
func writeOpenAIPreparedJSON(ctx context.Context, connection openAIWSClientConn, payload []byte) error {
	if writer, ok := connection.(openAIWSPreparedJSONWriter); ok {
		return writer.WritePreparedJSON(ctx, payload)
	}
	return connection.WriteJSON(ctx, json.RawMessage(append([]byte(nil), payload...)))
}

// WritePreparedJSON 把已定型的帧正文原样作为一条文本消息发出，不追加任何字节。
// 官方客户端对序列化结果直接发送（serde_json::to_string，没有末尾换行）；以前这里沿用 wsjson.Write 的
// 末尾换行，使每条业务消息比官方多一个 0x0a。
func (c *coderOpenAIWSClientConn) WritePreparedJSON(ctx context.Context, payload []byte) error {
	if c == nil || c.conn == nil {
		return errOpenAIWSConnClosed
	}
	if ctx == nil {
		ctx = context.Background()
	}
	raw, err := marshalOpenAIWSReadonlyJSON(json.RawMessage(payload))
	if err != nil {
		return fmt.Errorf("failed to write JSON message: failed to marshal JSON: %w", err)
	}
	return c.conn.Write(ctx, coderws.MessageText, raw)
}
