package service

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/pkg/logger"
	"github.com/gin-gonic/gin"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const (
	// 恢复只覆盖请求开始后的短窗口，不为清洗重试延长已有首输出超时。
	// 流事件继续使用首输出暂存的 8 MiB 上限，不新增预读或等待。
	openAIEncryptedContentRecoveryWindow   = 30 * time.Second
	openAIEncryptedContentRecoveryHint     = "旧会话的加密状态无法验证，无法安全恢复。请新建会话并补充必要的文字背景。"
	openAIEncryptedContentErrorJSON        = `{"code":"invalid_encrypted_content","type":"invalid_request_error","message":"旧会话的加密状态无法验证，无法安全恢复。请新建会话并补充必要的文字背景。"}`
	openAIEncryptedContentRecoveryTriedKey = "openai_encrypted_content_recovery_tried"
	openAIEncryptedContentRecoveryScopeKey = "openai_encrypted_content_recovery_scope"
)

// openAIEncryptedContentRecoverySignal 仅在响应处理器尚未提交事件时返回给
// Forward。它只携带恢复决策和响应编号，不保存上游正文、聊天内容或密文。
type openAIEncryptedContentRecoverySignal struct {
	responseID  string
	safeToRetry bool
	reason      string
}

func (e *openAIEncryptedContentRecoverySignal) Error() string {
	return openAIEncryptedContentRecoveryHint
}

func asOpenAIEncryptedContentRecoverySignal(err error) (*openAIEncryptedContentRecoverySignal, bool) {
	var signal *openAIEncryptedContentRecoverySignal
	if !errors.As(err, &signal) || signal == nil {
		return nil, false
	}
	return signal, true
}

// 同时识别 HTTP 错误信封、Responses 失败终态和顶层 error 事件。包装在
// error.message 中的 JSON 沿用已有精确解析；不靠消息中的关键词猜测错误码。
func isOpenAIInvalidEncryptedContentError(payload []byte) bool {
	if extractUpstreamErrorCode(payload) == openAIWSFallbackReasonInvalidEncryptedContent {
		return true
	}
	if response := gjson.GetBytes(payload, "response"); response.IsObject() &&
		extractUpstreamErrorCode([]byte(response.Raw)) == openAIWSFallbackReasonInvalidEncryptedContent {
		return true
	}
	return gjson.GetBytes(payload, "type").String() == "error" &&
		gjson.GetBytes(payload, "code").String() == openAIWSFallbackReasonInvalidEncryptedContent
}

// 压缩项可能是早期上下文的唯一副本。本次没有可靠的历史重建来源，因此任何
// 加密压缩项都会关闭 HTTP 恢复与缓存预清洗，不能套用通用清洗函数整项删除。
func openAIHTTPEncryptedRecoveryHasCompaction(reqBody map[string]any) bool {
	hasCompaction := func(item any) bool {
		inputItem, ok := item.(map[string]any)
		if !ok {
			return false
		}
		typeName := strings.TrimSpace(firstNonEmptyString(inputItem["type"]))
		_, encrypted := inputItem["encrypted_content"]
		return encrypted && (typeName == "compaction" || typeName == "compaction_summary")
	}
	switch input := reqBody["input"].(type) {
	case []any:
		for _, item := range input {
			if hasCompaction(item) {
				return true
			}
		}
	case []map[string]any:
		for _, item := range input {
			if hasCompaction(item) {
				return true
			}
		}
	case map[string]any:
		return hasCompaction(input)
	}
	return false
}

func trimOpenAIHTTPEncryptedReasoningItems(reqBody map[string]any, invalid map[openAIInvalidEncryptedDigest]struct{}) bool {
	if openAIHTTPEncryptedRecoveryHasCompaction(reqBody) {
		return false
	}
	return trimOpenAIEncryptedReasoningItemsIf(reqBody, func(item any) bool {
		inputItem, ok := item.(map[string]any)
		if !ok || strings.TrimSpace(firstNonEmptyString(inputItem["type"])) != "reasoning" {
			return false
		}
		digest, encrypted := openAIEncryptedReasoningItemDigest(item)
		if !encrypted {
			return false
		}
		if invalid == nil {
			return true
		}
		_, hit := invalid[digest]
		return hit
	})
}

// 工具调用即使尚无参数，也不应因暂存尚未刷新而被当作可重放的空响应。
// 失败终态自身携带的 output 同样需要检查，不能只看此前的 delta。
func openAIEncryptedContentEventBlocksRecovery(payload []byte, eventType string) bool {
	output := gjson.GetBytes(payload, "response.output")
	if output.Exists() && output.Type != gjson.Null && (!output.IsArray() || len(output.Array()) > 0) {
		return true
	}
	if eventType == "error" || eventType == "response.failed" {
		return false
	}
	if eventType == "response.output_item.added" {
		switch gjson.GetBytes(payload, "item.type").String() {
		case "function_call", "custom_tool_call":
			return true
		}
	}
	return openAIStreamDataStartsClientOutput(string(payload), eventType)
}

func openAIEncryptedContentSSEBlocksRecovery(body string) bool {
	blocked := false
	// 与终态解析共用帧解析器，兼容事件名只出现在 event 字段及多行 data 的上游。
	forEachOpenAISSEFrame(body, func(eventType string, payload []byte) {
		if !blocked && openAIEncryptedContentEventBlocksRecovery(payload, eventType) {
			blocked = true
		}
	})
	return blocked
}

// 只替换错误对象，保留当前响应编号、序列号和已输出内容；错误消息使用固定提示，
// 避免上游在 message 或扩展字段里回显密文、令牌或聊天内容。
func withOpenAIEncryptedContentRecoveryHint(payload []byte) []byte {
	path := "error"
	if gjson.GetBytes(payload, "response").IsObject() {
		path = "response.error"
	} else if !gjson.GetBytes(payload, "error").IsObject() {
		// 顶层 error 的扩展字段也可能回显密文，只保留协议要求的安全字段。
		return []byte(fmt.Sprintf(`{"type":"error","sequence_number":%d,"code":"invalid_encrypted_content","message":"%s","param":null}`,
			gjson.GetBytes(payload, "sequence_number").Int(), openAIEncryptedContentRecoveryHint))
	}
	if next, err := sjson.SetRawBytes(payload, path, []byte(openAIEncryptedContentErrorJSON)); err == nil {
		return next
	}
	return []byte(`{"type":"error","error":` + openAIEncryptedContentErrorJSON + `}`)
}

func (s *OpenAIGatewayService) recordOpenAIEncryptedContentRecovery(
	c *gin.Context, account *Account, headers http.Header, model, chainKey, source, outcome, reason string, retryCount int, outputCommitted bool,
) {
	if chainKey == "" && c != nil {
		chainKey = c.GetString(openAIEncryptedContentRecoveryScopeKey)
	}
	if retryCount == 0 && c != nil && c.GetBool(openAIEncryptedContentRecoveryTriedKey) {
		retryCount = 1
	}
	requestID := headers.Get("x-request-id")
	detail := fmt.Sprintf("model=%q chain=%q source=%s outcome=%s reason=%s retry_count=%d output_committed=%t",
		model, chainKey, source, outcome, reason, retryCount, outputCommitted)
	logger.LegacyPrintf("service.openai_gateway", "invalid_encrypted_content_recovery account_id=%d request_id=%q %s", account.ID, requestID, detail)
	if outcome == "recovered" {
		return
	}
	appendOpsUpstreamError(c, OpsUpstreamErrorEvent{
		ProxyID: opsUpstreamProxyID(account), ProxyName: opsUpstreamProxyName(account),
		Platform: account.Platform, AccountID: account.ID, AccountName: account.Name,
		UpstreamStatusCode: http.StatusBadRequest, UpstreamRequestID: requestID,
		Kind: "encrypted_content_recovery", Stage: source, Reason: openAIWSFallbackReasonInvalidEncryptedContent,
		Message: openAIEncryptedContentRecoveryHint, Detail: detail,
	})
	if outcome == "final_failure" {
		setOpsUpstreamError(c, http.StatusBadRequest, openAIEncryptedContentRecoveryHint, detail)
	}
}

// 尚未写出任何事件时可直接返回 HTTP 400；流处理器已经接受的流式响应，或仅
// 心跳已提交的响应，用唯一 response.failed 结束，防止通用错误处理再追加错误帧。
func writeOpenAIEncryptedContentFailure(c *gin.Context, resp *http.Response, stream bool, model, responseID string) error {
	MarkResponseCommitted(c)
	// 先停拍再选择 JSON/SSE，避免首拍恰好发生在 Written 检查与实际写回之间。
	keepaliveCommitted := StopOpenAICompactSSEKeepaliveCommitted(c)
	if !c.Writer.Written() {
		c.Header("X-Request-Id", resp.Header.Get("x-request-id"))
	}
	if stream || keepaliveCommitted || c.Writer.Written() {
		c.Header("Content-Type", "text/event-stream")
		c.Header("Cache-Control", "no-cache")
		c.Header("X-Accel-Buffering", "no")
		payload := []byte(`{"error":` + openAIEncryptedContentErrorJSON + `}`)
		if _, err := io.WriteString(c.Writer, buildOpenAIResponseFailedSSE(responseID, model, payload, openAIEncryptedContentRecoveryHint)); err != nil {
			return fmt.Errorf("write encrypted content failure: %w", err)
		}
		c.Writer.Flush()
	} else {
		// 前一次 SSE attempt 可能只设置了响应头，尚未写出；终止 HTTP 错误须覆盖它。
		c.Header("Content-Type", "application/json; charset=utf-8")
		c.Data(http.StatusBadRequest, "application/json; charset=utf-8", []byte(`{"error":`+openAIEncryptedContentErrorJSON+`}`))
	}
	return &openAIEncryptedContentRecoverySignal{reason: "recovery_stopped"}
}
