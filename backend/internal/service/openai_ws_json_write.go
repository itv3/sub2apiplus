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

// marshalOpenAIWSReadonlyJSON 与 json.Marshal 的字节及错误语义一致。正式帧已经
// 是 JSON，常见的紧凑且无 HTML 字符形态只需校验，不再交给通用编码器重建整帧。
// 返回值可能引用输入，调用方必须保持正文只读，且不能将它存入跨轮可变缓存。
func marshalOpenAIWSReadonlyJSON(value any) ([]byte, error) {
	raw, ok := value.(json.RawMessage)
	if !ok || len(raw) == 0 || !json.Valid(raw) {
		return json.Marshal(value)
	}
	compact, escape := false, false
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
			if c == '<' || c == '>' || c == '&' ||
				(c == 0xe2 && i+2 < len(raw) && raw[i+1] == 0x80 && raw[i+2]&^1 == 0xa8) {
				escape = true
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
			return json.Marshal(value)
		}
		result = buffer.Bytes()
	}
	if escape {
		var buffer bytes.Buffer
		json.HTMLEscape(&buffer, result)
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

// WritePreparedJSON 保留 wsjson.Write 的紧凑 JSON、HTML 转义及末尾换行行为。
// Writer 分段发送同一条文本消息，避免为了补一个换行再复制整份正文。
func (c *coderOpenAIWSClientConn) WritePreparedJSON(ctx context.Context, payload []byte) (err error) {
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
	writer, err := c.conn.Writer(ctx, coderws.MessageText)
	if err != nil {
		return err
	}
	defer func() {
		closeErr := writer.Close()
		if err == nil {
			err = closeErr
		}
	}()
	if _, err = writer.Write(raw); err != nil {
		return err
	}
	_, err = writer.Write([]byte{'\n'})
	return err
}
