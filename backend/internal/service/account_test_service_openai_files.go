package service

import (
	"bytes"
	"fmt"

	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

// officialFilesProbePayload 返回管理员显式触发 Files 探针时唯一允许上传的内容。
// 版本来自 Active ReleaseCatalog；探针不读取 prompt、文件路径或任意外部内容。
func officialFilesProbePayload(version string) string {
	return fmt.Sprintf("sub2apiplus Codex %s Files probe\n", version)
}

// testOpenAIOfficialFilesProbe 通过选定的 OpenAI OAuth 账号执行官方 Files
// create、blob PUT、uploaded 三段式流程。完成结果只用于判定成功，任何服务端
// 返回的上传地址、下载地址和文件标识都不会写入管理接口响应。
func (s *AccountTestService) testOpenAIOfficialFilesProbe(c *gin.Context, account *Account) error {
	if account == nil || !account.IsOpenAIOAuth() {
		return s.sendErrorAndEnd(c, "Codex Files 出站探针只支持 OpenAI OAuth 账号")
	}
	if s.openAIGatewayService == nil {
		return s.sendErrorAndEnd(c, "Codex Files 出站探针服务未配置")
	}

	c.Writer.Header().Set("Content-Type", "text/event-stream")
	c.Writer.Header().Set("Cache-Control", "no-cache")
	c.Writer.Header().Set("Connection", "keep-alive")
	c.Writer.Header().Set("X-Accel-Buffering", "no")
	c.Writer.Flush()

	s.sendEvent(c, TestEvent{Type: "test_start"})
	version := resolveVerifiedCodexClientVersion()
	s.sendEvent(c, TestEvent{Type: "status", Text: fmt.Sprintf("正在验证 Codex %s Files 三段式出站", version)})

	payload := []byte(officialFilesProbePayload(version))
	fileName := fmt.Sprintf("sub2apiplus-codex-%s-files-probe-%s.txt", version, uuid.NewString())
	uploaded, err := s.openAIGatewayService.UploadOfficialCodexFile(
		c.Request.Context(),
		account,
		OfficialCodexFileUploadInput{
			FileName:      fileName,
			FileSizeBytes: uint64(len(payload)),
			Contents:      bytes.NewReader(payload),
		},
	)
	if err != nil {
		// 上游错误可能携带签名地址，因此管理接口只返回固定错误文本。
		return s.sendErrorAndEnd(c, "Codex Files 出站探针失败")
	}
	if uploaded == nil {
		return s.sendErrorAndEnd(c, "Codex Files 出站探针未返回完成状态")
	}

	s.sendEvent(c, TestEvent{Type: "status", Text: fmt.Sprintf("已通过 Codex %s Files 三段式出站验证", version)})
	s.sendEvent(c, TestEvent{Type: "test_complete", Success: true})
	return nil
}
