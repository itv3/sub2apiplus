package service

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"regexp"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const (
	AnthropicAPIKeyStandardAPICompatExtraKey = "anthropic_apikey_standard_api_compat"
	anthropicAPIKeyClaudeCodeMimicExtraKey   = "anthropic_apikey_mimic_claude_code"
	anthropicStandardAPIUserAgent            = "sub2apiplus/standard-api"
)

// IsAnthropicAPIKeyStandardAPICompatEnabled 返回账号是否启用普通 API 兼容。
// 此模式只处理 Anthropic API Key；即使历史数据误存了开关，也不会影响其它平台或 OAuth。
func (a *Account) IsAnthropicAPIKeyStandardAPICompatEnabled() bool {
	return a != nil && a.Platform == PlatformAnthropic && a.Type == AccountTypeAPIKey &&
		a.getExtraBool(AnthropicAPIKeyStandardAPICompatExtraKey)
}

func hasAnthropicAPIKeyCompatExtra(extra map[string]any) bool {
	_, standard := extra[AnthropicAPIKeyStandardAPICompatExtraKey]
	_, mimic := extra[anthropicAPIKeyClaudeCodeMimicExtraKey]
	return standard || mimic
}

// completeAnthropicAPIKeyCompatPatch 在校验通过后补齐互斥值。
// JSONB 一次原子合并两项，避免两个并发开启请求都读取旧状态后保存出双开配置。
func completeAnthropicAPIKeyCompatPatch(updates map[string]any) map[string]any {
	standard, _ := updates[AnthropicAPIKeyStandardAPICompatExtraKey].(bool)
	mimic, _ := updates[anthropicAPIKeyClaudeCodeMimicExtraKey].(bool)
	if standard {
		updates[anthropicAPIKeyClaudeCodeMimicExtraKey] = false
	} else if mimic {
		updates[AnthropicAPIKeyStandardAPICompatExtraKey] = false
	}
	return updates
}

// ValidateAnthropicAPIKeyCompatExtra 校验最终配置，供创建、编辑、批量更新共同使用。
// 两种兼容模式方向相反，不能同时启用；null 表示清除配置，等同关闭。
func ValidateAnthropicAPIKeyCompatExtra(platform, accountType string, extra map[string]any) error {
	if platform != PlatformAnthropic || accountType != AccountTypeAPIKey {
		if _, exists := extra[AnthropicAPIKeyStandardAPICompatExtraKey]; exists {
			return infraerrors.BadRequest("INVALID_ANTHROPIC_APIKEY_COMPAT", "客户端兼容仅适用于 Anthropic API Key 账号")
		}
		// 其它账号历史上可能保存过旧 mimic 字段；普通编辑保持原有行为。
		return nil
	}
	for _, key := range []string{AnthropicAPIKeyStandardAPICompatExtraKey, anthropicAPIKeyClaudeCodeMimicExtraKey} {
		value, exists := extra[key]
		if !exists {
			continue
		}
		if value != nil {
			if _, ok := value.(bool); !ok {
				return infraerrors.BadRequest("INVALID_ANTHROPIC_APIKEY_COMPAT", fmt.Sprintf("extra.%s 必须为布尔值", key))
			}
		}
	}
	standard, _ := extra[AnthropicAPIKeyStandardAPICompatExtraKey].(bool)
	mimic, _ := extra[anthropicAPIKeyClaudeCodeMimicExtraKey].(bool)
	if standard && mimic {
		return infraerrors.BadRequest("ANTHROPIC_APIKEY_COMPAT_CONFLICT", "普通 API 兼容与 API Key 官方客户端兼容不能同时开启，请先关闭另一项")
	}
	return nil
}

// ValidateAnthropicAPIKeyCompatExtraPatch 按 JSONB 顶层合并语义检查增量，避免漏掉已开启的另一模式。
func ValidateAnthropicAPIKeyCompatExtraPatch(account *Account, updates map[string]any) error {
	if !hasAnthropicAPIKeyCompatExtra(updates) {
		return nil
	}
	if account == nil {
		return ErrAccountNotFound
	}
	if account.Platform != PlatformAnthropic || account.Type != AccountTypeAPIKey {
		return infraerrors.BadRequest("INVALID_ANTHROPIC_APIKEY_COMPAT", "客户端兼容仅适用于 Anthropic API Key 账号")
	}
	merged := make(map[string]any, len(account.Extra)+len(updates))
	for key, value := range account.Extra {
		merged[key] = value
	}
	for key, value := range updates {
		merged[key] = value
	}
	return ValidateAnthropicAPIKeyCompatExtra(account.Platform, account.Type, merged)
}

var (
	// 只移除计费标识中的键值项；同一行后续的业务指令仍然保留。
	anthropicStandardAPIBillingPattern = regexp.MustCompile(`(?m)^[\t ]*x-anthropic-billing-header:[\t ]*(?:[A-Za-z_][A-Za-z0-9_-]*=[^;\r\n]*;[\t ]*)+`)
	// 桌面端和 sdk-cli 可能使用独立的 Agent SDK 身份句，不能只覆盖传统 CLI 介绍。
	// 两项均为精确固定文本；业务指令、工具定义和用户消息不参与身份句过滤。
	anthropicStandardAPIIdentityPattern = regexp.MustCompile(`(?:You are Claude Code, Anthropic's official CLI for Claude(?:, running within the Claude Agent SDK)?|You are a Claude agent, built on Anthropic's Claude Agent SDK)\.`)
)

func sanitizeAnthropicStandardAPISystemText(text string) string {
	next := anthropicStandardAPIBillingPattern.ReplaceAllString(text, "")
	next = anthropicStandardAPIIdentityPattern.ReplaceAllString(next, "")
	if next == text {
		return text
	}
	return strings.TrimSpace(next)
}

// sanitizeAnthropicStandardAPIBody 只修改 system 中的固定身份标识。
// 保留原始 messages、tools、metadata 和未修改块的属性，避免数值精度或缓存配置被重编码改变。
func sanitizeAnthropicStandardAPIBody(body []byte) ([]byte, error) {
	system := gjson.GetBytes(body, "system")
	var replacement any
	changed := false
	if system.Type == gjson.String {
		text := sanitizeAnthropicStandardAPISystemText(system.String())
		changed = text != system.String()
		if text != "" {
			replacement = text
		}
	} else if system.IsArray() {
		blocks := make([]json.RawMessage, 0, len(system.Array()))
		for _, block := range system.Array() {
			raw := []byte(block.Raw)
			if block.Get("type").String() == "text" && block.Get("text").Type == gjson.String {
				text := sanitizeAnthropicStandardAPISystemText(block.Get("text").String())
				if text != block.Get("text").String() {
					changed = true
					if text == "" {
						continue
					}
					var err error
					raw, err = sjson.SetBytes(raw, "text", text)
					if err != nil {
						return nil, fmt.Errorf("适配 system 文本：%w", err)
					}
				}
			}
			blocks = append(blocks, json.RawMessage(raw))
		}
		if len(blocks) > 0 {
			replacement = blocks
		}
	}
	if !changed {
		return body, nil
	}
	if replacement == nil {
		return sjson.DeleteBytes(body, "system")
	}
	return sjson.SetBytes(body, "system", replacement)
}

// applyAnthropicAPIKeyStandardAPICompat 必须在正文构造和所有请求头覆写之后调用。
// 每次重试都从原始正文重新构造，处理函数幂等，且同步 GetBody 供传输层重放使用。
func applyAnthropicAPIKeyStandardAPICompat(account *Account, req *http.Request, body []byte) ([]byte, error) {
	if !account.IsAnthropicAPIKeyStandardAPICompatEnabled() {
		return body, nil
	}
	next, err := sanitizeAnthropicStandardAPIBody(body)
	if err != nil {
		return nil, err
	}
	if !bytes.Equal(next, body) {
		req.Body = io.NopCloser(bytes.NewReader(next))
		req.ContentLength = int64(len(next))
		req.GetBody = func() (io.ReadCloser, error) { return io.NopCloser(bytes.NewReader(next)), nil }
	}
	betas := make([]string, 0)
	for key, values := range req.Header {
		switch {
		case strings.EqualFold(key, "user-agent"):
			delete(req.Header, key)
		case strings.EqualFold(key, "anthropic-beta"):
			for _, value := range values {
				for _, token := range strings.Split(value, ",") {
					token = strings.TrimSpace(token)
					if token != "" && !strings.EqualFold(token, claude.BetaClaudeCode) {
						betas = append(betas, token)
					}
				}
			}
			delete(req.Header, key)
		}
	}
	setHeaderRaw(req.Header, "User-Agent", anthropicStandardAPIUserAgent)
	if len(betas) > 0 {
		setHeaderRaw(req.Header, "anthropic-beta", strings.Join(betas, ","))
	}
	return next, nil
}
