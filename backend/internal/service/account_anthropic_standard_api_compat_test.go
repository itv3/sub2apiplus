package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

const standardAPICompatTestBody = `{"model":"claude-sonnet-4-5","max_tokens":32,"stream":true,"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.304.abc; cc_entrypoint=claude-desktop-3p; cch=00000;"},{"type":"text","text":"You are Claude Code, Anthropic's official CLI for Claude."},{"type":"text","text":"保留项目指令","cache_control":{"type":"ephemeral","ttl":"1h"}}],"messages":[{"role":"user","content":"You are Claude Code, Anthropic's official CLI for Claude."}],"tools":[{"name":"probe","input_schema":{"type":"object","properties":{}}}],"metadata":{"user_id":"test-session","large_id":9007199254740993}}`

func standardAPICompatTestAccount() *Account {
	return &Account{ID: 42, Platform: PlatformAnthropic, Type: AccountTypeAPIKey,
		Status: StatusActive, Schedulable: true, Concurrency: 1,
		Credentials: map[string]any{"api_key": "test-key"},
		Extra:       map[string]any{AnthropicAPIKeyStandardAPICompatExtraKey: true}}
}

func requireStandardAPICompatRequest(t *testing.T, req *http.Request, body []byte) {
	t.Helper()
	require.Equal(t, anthropicStandardAPIUserAgent, getHeaderRaw(req.Header, "User-Agent"))
	require.NotContains(t, strings.ToLower(getHeaderRaw(req.Header, "anthropic-beta")), claude.BetaClaudeCode)
	require.Contains(t, getHeaderRaw(req.Header, "anthropic-beta"), "fine-grained-tool-streaming-2025-05-14")
	require.NotContains(t, gjson.GetBytes(body, "system").Raw, "x-anthropic-billing-header")
	require.NotContains(t, gjson.GetBytes(body, "system").Raw, "official CLI")
	require.Equal(t, "保留项目指令", gjson.GetBytes(body, "system.0.text").String())
	require.Equal(t, "1h", gjson.GetBytes(body, "system.0.cache_control.ttl").String())
	require.Equal(t, "probe", gjson.GetBytes(body, "tools.0.name").String())
	require.Contains(t, gjson.GetBytes(body, "messages.0.content").String(), "official CLI")
	require.Equal(t, "9007199254740993", gjson.GetBytes(body, "metadata.large_id").Raw)
}

func TestAnthropicStandardAPICompatSystem(t *testing.T) {
	for _, test := range []struct{ name, body, system string }{
		{"字符串混合指令", `{"system":"x-anthropic-billing-header: cc_version=2.1.304; cc_entrypoint=cli; 保留同一行业务指令\nYou are Claude Code, Anthropic's official CLI for Claude.\n保留下一行"}`, "保留同一行业务指令\n\n保留下一行"},
		{"块内混合指令", `{"system":[{"type":"text","text":"You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK. 保留项目约束","cache_control":{"type":"ephemeral"},"custom":true}]}`, "保留项目约束"},
		{"只有身份时移除system", `{"system":"You are Claude Code, Anthropic's official CLI for Claude."}`, ""},
		{"不匹配的业务文本原样保留", `{"system":"说明：x-anthropic-billing-header 是一个诊断字段。"}`, "说明：x-anthropic-billing-header 是一个诊断字段。"},
	} {
		t.Run(test.name, func(t *testing.T) {
			body, err := sanitizeAnthropicStandardAPIBody([]byte(test.body))
			require.NoError(t, err)
			system := gjson.GetBytes(body, "system")
			if system.IsArray() {
				require.Equal(t, test.system, system.Get("0.text").String())
				require.True(t, system.Get("0.custom").Bool())
				require.Equal(t, "ephemeral", system.Get("0.cache_control.type").String())
			} else {
				require.Equal(t, test.system, system.String())
			}
			if test.system == "" {
				require.False(t, system.Exists())
			}
			again, err := sanitizeAnthropicStandardAPIBody(body)
			require.NoError(t, err)
			require.Equal(t, body, again)
		})
	}
}

func TestAnthropicStandardAPICompatHeadersAndReplay(t *testing.T) {
	account := standardAPICompatTestAccount()
	body := []byte(standardAPICompatTestBody)
	req, err := http.NewRequest(http.MethodPost, "https://api.anthropic.com/v1/messages", bytes.NewReader(body))
	require.NoError(t, err)
	req.Header = http.Header{
		"USER-AGENT": {"claude-cli/test"}, "user-agent": {"claude-cli/other"},
		"ANTHROPIC-BETA": {"CLAUDE-CODE-20250219,fine-grained-tool-streaming-2025-05-14", "context-1m-2025-08-07"},
		"X-App":          {"cli"}, "X-Claude-Code-Session-Id": {"test-session"},
	}
	next, err := applyAnthropicAPIKeyStandardAPICompat(account, req, body)
	require.NoError(t, err)
	requireStandardAPICompatRequest(t, req, next)
	require.Equal(t, "cli", req.Header.Get("X-App"))
	require.Equal(t, "test-session", req.Header.Get("X-Claude-Code-Session-Id"))
	require.Contains(t, getHeaderRaw(req.Header, "anthropic-beta"), "context-1m-2025-08-07")
	require.Equal(t, int64(len(next)), req.ContentLength)
	read, err := io.ReadAll(req.Body)
	require.NoError(t, err)
	require.Equal(t, next, read)
	replay, err := req.GetBody()
	require.NoError(t, err)
	defer func() { _ = replay.Close() }()
	read, err = io.ReadAll(replay)
	require.NoError(t, err)
	require.Equal(t, next, read)

	req.Header = http.Header{"Anthropic-Beta": {claude.BetaClaudeCode}}
	_, err = applyAnthropicAPIKeyStandardAPICompat(account, req, next)
	require.NoError(t, err)
	require.Empty(t, getHeaderRaw(req.Header, "anthropic-beta"))
}

func TestAnthropicStandardAPICompatDisabledAndOtherPlatforms(t *testing.T) {
	for _, account := range []*Account{nil, {Platform: PlatformAnthropic, Type: AccountTypeAPIKey},
		{Platform: PlatformAnthropic, Type: AccountTypeOAuth, Extra: map[string]any{AnthropicAPIKeyStandardAPICompatExtraKey: true}},
		{Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Extra: map[string]any{AnthropicAPIKeyStandardAPICompatExtraKey: true}}} {
		body := []byte(standardAPICompatTestBody)
		req, err := http.NewRequest(http.MethodPost, "https://example.com", bytes.NewReader(body))
		require.NoError(t, err)
		req.Header.Set("User-Agent", "claude-cli/test")
		req.Header.Set("anthropic-beta", claude.BetaClaudeCode)
		next, err := applyAnthropicAPIKeyStandardAPICompat(account, req, body)
		require.NoError(t, err)
		require.Equal(t, body, next)
		require.Equal(t, "claude-cli/test", req.Header.Get("User-Agent"))
		require.Equal(t, claude.BetaClaudeCode, req.Header.Get("anthropic-beta"))
	}
}

func TestAnthropicStandardAPICompatGatewayBuilders(t *testing.T) {
	gin.SetMode(gin.TestMode)
	svc := &GatewayService{cfg: &config.Config{}}
	for _, path := range []string{"messages", "messages-passthrough", "count_tokens", "count_tokens-passthrough"} {
		t.Run(path, func(t *testing.T) {
			account := standardAPICompatTestAccount()
			account.Credentials["header_override_enabled"] = true
			account.Credentials["header_overrides"] = map[string]any{
				"user-agent": "claude-cli/from-override", "anthropic-beta": claude.BetaClaudeCode + ",fine-grained-tool-streaming-2025-05-14",
			}
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
			c.Request.Header.Set("User-Agent", "claude-cli/2.1.304 (external, claude-desktop-3p)")
			c.Request.Header.Set("anthropic-beta", claude.BetaClaudeCode+",fine-grained-tool-streaming-2025-05-14")
			original := []byte(standardAPICompatTestBody)
			for range 2 {
				var req *http.Request
				var err error
				switch path {
				case "messages":
					req, _, err = svc.buildUpstreamRequest(context.Background(), c, account, original, "test-key", "apikey", "claude-sonnet-4-5", true, true)
				case "messages-passthrough":
					req, _, err = svc.buildUpstreamRequestAnthropicAPIKeyPassthrough(context.Background(), c, account, original, "test-key")
				case "count_tokens":
					req, _, err = svc.buildCountTokensRequest(context.Background(), c, account, original, "test-key", "apikey", "claude-sonnet-4-5", true)
				case "count_tokens-passthrough":
					req, err = svc.buildCountTokensRequestAnthropicAPIKeyPassthrough(context.Background(), c, account, original, "test-key")
				}
				require.NoError(t, err)
				body, err := io.ReadAll(req.Body)
				require.NoError(t, err)
				requireStandardAPICompatRequest(t, req, body)
				require.Contains(t, string(original), "x-anthropic-billing-header")
				require.Contains(t, c.Request.Header.Get("anthropic-beta"), claude.BetaClaudeCode)
			}
		})
	}
}

func TestAnthropicStandardAPICompatKeeperProxy(t *testing.T) {
	for _, path := range []string{"/v1/messages", "/v1/messages/count_tokens", "/v1/models"} {
		t.Run(path, func(t *testing.T) {
			account := standardAPICompatTestAccount()
			account.Extra["keeper_keepalive_enabled"] = true
			upstream := &keeperProxyHTTPUpstreamRecorder{}
			svc := &AccountTestService{accountRepo: &keeperProxyAccountRepoStub{account: account}, httpUpstream: upstream, cfg: &config.Config{}}
			method := http.MethodPost
			var body io.Reader = strings.NewReader(standardAPICompatTestBody)
			if path == "/v1/models" {
				method, body = http.MethodGet, nil
			}
			resp, err := svc.ProxyKeeperAnthropicAccount(context.Background(), account.ID, KeeperOpenAIProxyRequest{
				Method: method, Path: path, Body: body,
				Header: http.Header{"User-Agent": {"claude-cli/test"}, "Anthropic-Beta": {claude.BetaClaudeCode + ",fine-grained-tool-streaming-2025-05-14"}},
			})
			require.NoError(t, err)
			_ = resp.Body.Close()
			if method == http.MethodPost {
				requireStandardAPICompatRequest(t, upstream.lastRequest, upstream.lastBody)
			} else {
				require.Equal(t, anthropicStandardAPIUserAgent, getHeaderRaw(upstream.lastRequest.Header, "User-Agent"))
			}
		})
	}
}

type standardAPICompatAdminRepo struct {
	AccountRepository
	account   *Account
	writes    int
	lastPatch map[string]any
}

func (r *standardAPICompatAdminRepo) GetByID(context.Context, int64) (*Account, error) {
	return r.account, nil
}

func (r *standardAPICompatAdminRepo) GetByIDs(context.Context, []int64) ([]*Account, error) {
	return []*Account{r.account}, nil
}

func (r *standardAPICompatAdminRepo) UpdateExtra(_ context.Context, _ int64, updates map[string]any) error {
	r.writes++
	r.lastPatch = updates
	return nil
}

func TestAnthropicStandardAPICompatSaveValidation(t *testing.T) {
	account := standardAPICompatTestAccount()
	for _, extra := range []map[string]any{
		{AnthropicAPIKeyStandardAPICompatExtraKey: true, anthropicAPIKeyClaudeCodeMimicExtraKey: true},
		{AnthropicAPIKeyStandardAPICompatExtraKey: "true"},
	} {
		require.Error(t, ValidateAnthropicAPIKeyCompatExtra(account.Platform, account.Type, extra))
		_, err := buildAccountForCreate(&CreateAccountInput{Platform: account.Platform, Type: account.Type}, extra)
		require.Error(t, err)
	}
	require.Error(t, ValidateAnthropicAPIKeyCompatExtra(PlatformOpenAI, AccountTypeAPIKey, account.Extra))
	require.Error(t, ValidateAnthropicAPIKeyCompatExtra(PlatformAnthropic, AccountTypeOAuth, account.Extra))
	require.NoError(t, ValidateAnthropicAPIKeyCompatExtraPatch(account, map[string]any{AnthropicAPIKeyStandardAPICompatExtraKey: false, anthropicAPIKeyClaudeCodeMimicExtraKey: true}))
	require.Error(t, ValidateAnthropicAPIKeyCompatExtraPatch(account, map[string]any{anthropicAPIKeyClaudeCodeMimicExtraKey: true}))

	repo := &standardAPICompatAdminRepo{account: account}
	svc := &adminServiceImpl{accountRepo: repo}
	require.Error(t, svc.UpdateAccountExtra(context.Background(), account.ID, map[string]any{anthropicAPIKeyClaudeCodeMimicExtraKey: true}))
	_, err := svc.UpdateAccount(context.Background(), account.ID, &UpdateAccountInput{
		Extra: map[string]any{AnthropicAPIKeyStandardAPICompatExtraKey: true, anthropicAPIKeyClaudeCodeMimicExtraKey: true},
	})
	require.Error(t, err)
	_, err = svc.BulkUpdateAccounts(context.Background(), &BulkUpdateAccountsInput{
		AccountIDs: []int64{account.ID}, Extra: map[string]any{anthropicAPIKeyClaudeCodeMimicExtraKey: true},
	})
	require.Error(t, err)
	require.Zero(t, repo.writes)
	require.NoError(t, svc.UpdateAccountExtra(context.Background(), account.ID, map[string]any{AnthropicAPIKeyStandardAPICompatExtraKey: false}))
	require.Equal(t, 1, repo.writes)
	require.NoError(t, svc.UpdateAccountExtra(context.Background(), account.ID, map[string]any{AnthropicAPIKeyStandardAPICompatExtraKey: true}))
	require.Equal(t, false, repo.lastPatch[anthropicAPIKeyClaudeCodeMimicExtraKey])
}

func TestAnthropicStandardAPICompatLegacyOtherAccounts(t *testing.T) {
	for _, account := range []*Account{
		{Platform: PlatformAnthropic, Type: AccountTypeOAuth},
		{Platform: PlatformOpenAI, Type: AccountTypeAPIKey},
	} {
		for _, legacyValue := range []any{nil, false, true, "false"} {
			account.Extra = map[string]any{anthropicAPIKeyClaudeCodeMimicExtraKey: legacyValue}
			// 未设置新开关的历史账号仍可普通编辑，无关的 Extra 更新也不受影响。
			require.NoError(t, ValidateAnthropicAPIKeyCompatExtra(account.Platform, account.Type, account.Extra))
			require.NoError(t, ValidateAnthropicAPIKeyCompatExtraPatch(account, map[string]any{"remark": "保留旧配置"}))
			// 显式更新兼容开关时仍严格限制账号平台和类型。
			require.Error(t, ValidateAnthropicAPIKeyCompatExtraPatch(account, map[string]any{anthropicAPIKeyClaudeCodeMimicExtraKey: false}))
			account.Extra[AnthropicAPIKeyStandardAPICompatExtraKey] = false
			require.Error(t, ValidateAnthropicAPIKeyCompatExtra(account.Platform, account.Type, account.Extra))
		}
	}
}
