package service

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

type keeperProxyAccountRepoStub struct {
	AccountRepository
	account *Account
}

func (r *keeperProxyAccountRepoStub) GetByID(_ context.Context, _ int64) (*Account, error) {
	return r.account, nil
}

type keeperProxyHTTPUpstreamRecorder struct {
	standardCalls int
	tlsCalls      int
	tlsProfile    *tlsfingerprint.Profile
	lastRequest   *http.Request
	lastBody      []byte
}

func (r *keeperProxyHTTPUpstreamRecorder) record(req *http.Request) {
	r.lastRequest = req
	if req == nil || req.Body == nil {
		return
	}
	r.lastBody, _ = io.ReadAll(req.Body)
}

func (r *keeperProxyHTTPUpstreamRecorder) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	r.standardCalls++
	r.record(req)
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(`{"type":"message"}`)),
	}, nil
}

func (r *keeperProxyHTTPUpstreamRecorder) DoWithTLS(req *http.Request, _ string, _ int64, _ int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	r.tlsCalls++
	r.tlsProfile = profile
	r.record(req)
	return &http.Response{
		StatusCode: http.StatusOK,
		Header:     make(http.Header),
		Body:       io.NopCloser(strings.NewReader(`{"type":"message"}`)),
	}, nil
}

func TestProxyKeeperOpenAIAccountRejectsNonAPIKeyAccount(t *testing.T) {
	svc := &AccountTestService{
		accountRepo: &keeperProxyAccountRepoStub{
			account: &Account{
				ID:       42,
				Platform: PlatformOpenAI,
				Type:     AccountTypeOAuth,
			},
		},
	}

	_, err := svc.ProxyKeeperOpenAIAccount(context.Background(), 42, KeeperOpenAIProxyRequest{
		Method: http.MethodGet,
		Path:   "/v1/models",
	})
	require.Error(t, err)
	require.Contains(t, err.Error(), "OpenAI API key account")
}

func TestProxyKeeperAnthropicAccountMimicTakesPriorityAndUsesCurrentCLITLS(t *testing.T) {
	account := &Account{
		ID:          42,
		Platform:    PlatformAnthropic,
		Type:        AccountTypeAPIKey,
		Status:      StatusActive,
		Schedulable: true,
		Concurrency: 1,
		Credentials: map[string]any{
			"api_key": "anthropic-key",
		},
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
			"anthropic_passthrough":              true,
			"enable_tls_fingerprint":             true,
			"keeper_keepalive_enabled":           true,
		},
	}
	upstream := &keeperProxyHTTPUpstreamRecorder{}
	svc := &AccountTestService{
		accountRepo:         &keeperProxyAccountRepoStub{account: account},
		httpUpstream:        upstream,
		cfg:                 &config.Config{Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}},
		tlsFPProfileService: &TLSFingerprintProfileService{},
		gatewayService: &GatewayService{cfg: &config.Config{
			Gateway:  config.GatewayConfig{MaxLineSize: defaultMaxLineSize},
			Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}},
		}},
	}

	resp, err := svc.ProxyKeeperAnthropicAccount(context.Background(), account.ID, KeeperOpenAIProxyRequest{
		Method: http.MethodPost,
		Path:   "/v1/messages",
		Header: http.Header{
			"User-Agent":     []string{"claude-cli/2.1.210 (external, sdk-cli)"},
			"Anthropic-Beta": []string{"advisor-tool-2026-03-01,context-1m-2025-08-07"},
		},
		Body: strings.NewReader(`{"model":"claude-fable-5","max_tokens":64000,"stream":true,"system":[{"type":"text","text":"x-anthropic-billing-header: cc_version=2.1.210.abc; cc_entrypoint=sdk-cli;"},{"type":"text","text":"You are a Claude agent, built on Anthropic's Claude Agent SDK.","cache_control":{"type":"ephemeral"}},{"type":"text","text":"CWD: /workspace/projects/ai-keeper\nDate: 2026-07-15","cache_control":{"type":"ephemeral"}}],"tools":[{"name":"Read","description":"Read files from the mounted workspace.","input_schema":{"type":"object","properties":{"file_path":{"type":"string"}}}},{"name":"CustomTool","description":"Must be removed.","input_schema":{"type":"object","properties":{}}}],"messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}],"metadata":{"user_id":"{\"device_id\":\"device\",\"account_uuid\":\"\",\"session_id\":\"11111111-2222-4333-8444-555555555555\"}"}}`),
	})
	require.NoError(t, err)
	require.NotNil(t, resp)
	require.Zero(t, upstream.standardCalls)
	require.Equal(t, 1, upstream.tlsCalls)
	require.NotNil(t, upstream.tlsProfile)
	require.Contains(t, upstream.tlsProfile.Name, "Claude Code 2.1.220")
	require.NotNil(t, upstream.lastRequest)
	require.Equal(t, "claude-cli/2.1.220 (external, sdk-cli)", getHeaderRaw(upstream.lastRequest.Header, "User-Agent"))
	require.Equal(t, defaultAPIKeyMimicBetaHeader(upstream.lastBody), getHeaderRaw(upstream.lastRequest.Header, "anthropic-beta"))
	require.Equal(t, int64(512), gjson.GetBytes(upstream.lastBody, "max_tokens").Int())
	require.Len(t, gjson.GetBytes(upstream.lastBody, "system").Array(), 3)
	require.Contains(t, gjson.GetBytes(upstream.lastBody, "system.0.text").String(), "cc_entrypoint=sdk-cli")
	require.Contains(t, gjson.GetBytes(upstream.lastBody, "system.0.text").String(), "cc_version=2.1.220.")
	require.Equal(t, claudeSDKCLIIdentityPrompt, gjson.GetBytes(upstream.lastBody, "system.1.text").String())
	require.Equal(t, claudeCodeSystemPromptExpansion, gjson.GetBytes(upstream.lastBody, "system.2.text").String())
	require.Contains(t, gjson.GetBytes(upstream.lastBody, "messages.0.content.0.text").String(), "CWD: /workspace/projects/ai-keeper")
	require.NotContains(t, string(upstream.lastBody), "cc_entrypoint=claude-desktop-3p")
	tools := gjson.GetBytes(upstream.lastBody, "tools").Array()
	require.Len(t, tools, len(anthropicAPIKeyMimicTestToolNames))
	for i, name := range anthropicAPIKeyMimicTestToolNames {
		require.Equal(t, name, tools[i].Get("name").String())
	}
	require.Equal(t, "Read files from the mounted workspace.", gjson.GetBytes(upstream.lastBody, "tools.12.description").String())
	require.Equal(t, "string", gjson.GetBytes(upstream.lastBody, "tools.12.input_schema.properties.file_path.type").String())
	require.Equal(t, keeperAnthropicUnavailableToolDescription, gjson.GetBytes(upstream.lastBody, "tools.1.description").String())
	require.False(t, gjson.GetBytes(upstream.lastBody, "tools.#(name==\"CustomTool\")").Exists())
}

// TestProxyKeeperOpenAIAccountTLSFollowsOfficialClientProfileMode 固定 keeper OpenAI
// 内部代理的 TLS 决策来源：该路径按 header allowlist 透传真实官方 Codex CLI 形态、
// 不应用 mimic header，但 TLS 仍须跟随同一个服务级 active/previous 指针。
// 否则紧急整体回退期间会出现「官方 CLI header + 另一 mode 的 TLS 画像」的跨画像混用。
func TestProxyKeeperOpenAIAccountTLSFollowsOfficialClientProfileMode(t *testing.T) {
	newKeeperOpenAIAccount := func() *Account {
		return &Account{
			ID:          43,
			Platform:    PlatformOpenAI,
			Type:        AccountTypeAPIKey,
			Status:      StatusActive,
			Schedulable: true,
			Concurrency: 1,
			Credentials: map[string]any{
				"api_key":  "sk-keeper",
				"base_url": "https://compat-upstream.example",
			},
			Extra: map[string]any{
				"openai_apikey_mimic_codex_cli": true,
				"enable_tls_fingerprint":        true,
				"keeper_keepalive_enabled":      true,
			},
		}
	}
	newProxyRequest := func() KeeperOpenAIProxyRequest {
		return KeeperOpenAIProxyRequest{
			Method: http.MethodPost,
			Path:   "/v1/responses",
			Header: http.Header{"User-Agent": []string{"codex_exec/0.145.0"}},
			Body:   strings.NewReader(`{"model":"gpt-5.5","input":[{"role":"user","content":"hi"}]}`),
		}
	}

	t.Run("active 模式套用当前 CLI 指纹", func(t *testing.T) {
		account := newKeeperOpenAIAccount()
		upstream := &keeperProxyHTTPUpstreamRecorder{}
		svc := &AccountTestService{
			accountRepo:         &keeperProxyAccountRepoStub{account: account},
			httpUpstream:        upstream,
			cfg:                 &config.Config{Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}},
			tlsFPProfileService: &TLSFingerprintProfileService{},
		}

		resp, err := svc.ProxyKeeperOpenAIAccount(context.Background(), account.ID, newProxyRequest())
		require.NoError(t, err)
		require.NoError(t, resp.Body.Close())
		require.Zero(t, upstream.standardCalls)
		require.Equal(t, 1, upstream.tlsCalls)
		require.NotNil(t, upstream.tlsProfile)
		require.Contains(t, upstream.tlsProfile.Name, "Codex CLI 0.145.0")
		// keeper 不应用 mimic header：客户端原样透传的官方 UA 必须保留。
		require.Equal(t, "codex_exec/0.145.0", upstream.lastRequest.Header.Get("User-Agent"))
	})

	t.Run("previous 模式走标准 Transport", func(t *testing.T) {
		account := newKeeperOpenAIAccount()
		upstream := &keeperProxyHTTPUpstreamRecorder{}
		cfg := previousOfficialClientProfileConfig()
		cfg.Security = config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}
		svc := &AccountTestService{
			accountRepo:         &keeperProxyAccountRepoStub{account: account},
			httpUpstream:        upstream,
			cfg:                 cfg,
			tlsFPProfileService: &TLSFingerprintProfileService{},
		}

		resp, err := svc.ProxyKeeperOpenAIAccount(context.Background(), account.ID, newProxyRequest())
		require.NoError(t, err)
		require.NoError(t, resp.Body.Close())
		require.Equal(t, 1, upstream.standardCalls)
		require.Zero(t, upstream.tlsCalls)
		require.Nil(t, upstream.tlsProfile)
		require.Equal(t, "codex_exec/0.145.0", upstream.lastRequest.Header.Get("User-Agent"))
	})
}

func TestValidateKeeperOpenAIProxyPath(t *testing.T) {
	allowed := []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/v1/responses"},
		{http.MethodGet, "/v1/responses/resp_123/input_items"},
		{http.MethodPost, "/responses"},
		{http.MethodPost, "/chat/completions"},
		{http.MethodPost, "/v1/chat/completions"},
		{http.MethodGet, "/models"},
		{http.MethodGet, "/v1/models"},
	}
	for _, tt := range allowed {
		got, err := validateKeeperOpenAIProxyPath(tt.method, tt.path)
		require.NoError(t, err, tt.path)
		require.Equal(t, tt.path, got)
	}

	for _, tt := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/v1/files"},
		{http.MethodPost, "/v1/fine_tuning/jobs"},
		{http.MethodPost, "/v1/../models"},
		{http.MethodGet, "/v1/models?limit=1"},
		{http.MethodDelete, "/v1/responses/resp_123"},
		{http.MethodGet, "/v1/chat/completions"},
	} {
		_, err := validateKeeperOpenAIProxyPath(tt.method, tt.path)
		require.Error(t, err, tt.path)
	}
}

func TestValidateKeeperAnthropicProxyPath(t *testing.T) {
	for _, tt := range []struct {
		method string
		path   string
	}{
		{http.MethodPost, "/v1/messages"},
		{http.MethodPost, "/v1/messages/count_tokens"},
		{http.MethodGet, "/v1/models"},
	} {
		got, err := validateKeeperAnthropicProxyPath(tt.method, tt.path)
		require.NoError(t, err, tt.path)
		require.Equal(t, tt.path, got)
	}

	for _, tt := range []struct {
		method string
		path   string
	}{
		{http.MethodGet, "/v1/files"},
		{http.MethodPost, "/v1/messages/../../models"},
		{http.MethodPost, "/v1/messages?beta=true"},
		{http.MethodGet, "/v1/messages"},
	} {
		_, err := validateKeeperAnthropicProxyPath(tt.method, tt.path)
		require.Error(t, err, tt.path)
	}
}

func TestValidateKeeperAnthropicProxyQuery(t *testing.T) {
	for _, path := range []string{"/v1/messages", "/v1/messages/count_tokens"} {
		got, err := validateKeeperAnthropicProxyQuery(http.MethodPost, path, "beta=true")
		require.NoError(t, err, path)
		require.Equal(t, "beta=true", got)

		got, err = validateKeeperAnthropicProxyQuery(http.MethodPost, path, "")
		require.NoError(t, err, path)
		require.Empty(t, got)
	}

	for _, tt := range []struct {
		method   string
		path     string
		rawQuery string
	}{
		{http.MethodPost, "/v1/messages", "beta=false"},
		{http.MethodPost, "/v1/messages", "beta=true&extra=1"},
		{http.MethodPost, "/v1/messages", "beta=true&beta=true"},
		{http.MethodPost, "/v1/messages", "beta=%zz"},
		{http.MethodGet, "/v1/models", "beta=true"},
	} {
		_, err := validateKeeperAnthropicProxyQuery(tt.method, tt.path, tt.rawQuery)
		require.Error(t, err, tt)
	}
}

func TestCopyProxyRequestHeadersStripsCredentialsAndHopByHopHeaders(t *testing.T) {
	src := http.Header{
		"Authorization":  []string{"Bearer keeper"},
		"X-Api-Key":      []string{"keeper"},
		"Cookie":         []string{"sid=admin"},
		"Connection":     []string{"close"},
		"Content-Type":   []string{"application/json"},
		"Anthropic-Beta": []string{"tools-2024-05-16"},
		"Set-Cookie":     []string{"upstream=bad"},
	}
	dst := http.Header{}

	copyProxyRequestHeaders(dst, src)

	require.Empty(t, dst.Get("Authorization"))
	require.Empty(t, dst.Get("X-Api-Key"))
	require.Empty(t, dst.Get("Cookie"))
	require.Empty(t, dst.Get("Connection"))
	require.Empty(t, dst.Get("Set-Cookie"))
	require.Equal(t, "application/json", dst.Get("Content-Type"))
	require.Equal(t, "tools-2024-05-16", dst.Get("Anthropic-Beta"))
}

func TestKeeperProxyOfficialClientOverrideProtectsIdentityHeaders(t *testing.T) {
	src := http.Header{
		"Authorization":            []string{"Bearer keeper"},
		"Cookie":                   []string{"sid=admin"},
		"Thread-Id":                []string{"thread-1"},
		"X-App":                    []string{"claude-code"},
		"X-Claude-Code-Session-Id": []string{"claude-session"},
		"X-Client-Request-Id":      []string{"client-request"},
		"X-Codex-Beta-Features":    []string{"beta-a"},
		"X-Codex-Window-Id":        []string{"window-1"},
		"X-Stainless-Lang":         []string{"client-lang"},
		"X-Stainless-Retry-Count":  []string{"0"},
		"X-Stainless-Timeout":      []string{"600"},
	}
	dst := http.Header{}
	copyProxyRequestHeaders(dst, src)

	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Credentials: map[string]any{
			credKeyHeaderOverrideEnabled: true,
			credKeyHeaderOverrides: map[string]any{
				"x-stainless-lang": "go",
				"authorization":    "Bearer blocked",
				"cookie":           "sid=blocked",
			},
		},
	}
	applyAccountTestHeaderOverridesForOfficialClientProxy(account, dst)

	require.Equal(t, "thread-1", dst.Get("Thread-Id"))
	require.Equal(t, "claude-code", dst.Get("X-App"))
	require.Equal(t, "claude-session", dst.Get("X-Claude-Code-Session-Id"))
	require.Equal(t, "client-request", dst.Get("X-Client-Request-Id"))
	require.Equal(t, "beta-a", dst.Get("X-Codex-Beta-Features"))
	require.Equal(t, "window-1", dst.Get("X-Codex-Window-Id"))
	require.Equal(t, "client-lang", dst.Get("X-Stainless-Lang"))
	require.Equal(t, "0", dst.Get("X-Stainless-Retry-Count"))
	require.Equal(t, "600", dst.Get("X-Stainless-Timeout"))
	require.Empty(t, dst.Get("Authorization"))
	require.Empty(t, dst.Get("Cookie"))
}

func TestBuildKeeperOpenAIProxyURLAvoidsDoubleV1(t *testing.T) {
	require.Equal(t,
		"https://upstream.example/v1/responses",
		buildKeeperOpenAIProxyURL("https://upstream.example/v1", "/v1/responses"),
	)
	require.Equal(t,
		"https://upstream.example/v1/messages",
		buildKeeperOpenAIProxyURL("https://upstream.example", "/v1/messages"),
	)
	require.Equal(t,
		"https://upstream.example/v1/messages?beta=true",
		buildKeeperProxyURL("https://upstream.example", "/v1/messages", "beta=true"),
	)
}

func TestKeeperMaxOutputTokensUsesDefaultAndExtra(t *testing.T) {
	require.Equal(t, DefaultKeeperMaxOutputTokens, KeeperMaxOutputTokens(&Account{}))
	require.Equal(t, 256, KeeperMaxOutputTokens(&Account{Extra: map[string]any{
		"keeper_keepalive_max_output_tokens": "256",
	}}))
	require.Equal(t, DefaultKeeperMaxOutputTokens, KeeperMaxOutputTokens(&Account{Extra: map[string]any{
		"keeper_keepalive_max_output_tokens": 0,
	}}))
	require.Equal(t, KeeperProxyMaxOutputTokensHardCap, KeeperMaxOutputTokens(&Account{Extra: map[string]any{
		"keeper_keepalive_max_output_tokens": KeeperProxyMaxOutputTokensHardCap + 1,
	}}))
}

func TestClampKeeperProxyJSONMaxTokensAddsAndClamps(t *testing.T) {
	reader, err := clampKeeperProxyJSONMaxTokens(strings.NewReader(`{"model":"gpt-5.5"}`), 512, "max_output_tokens", "max_output_tokens")
	require.NoError(t, err)
	raw, err := io.ReadAll(reader)
	require.NoError(t, err)
	require.EqualValues(t, 512, gjson.GetBytes(raw, "max_output_tokens").Int())

	reader, err = clampKeeperProxyJSONMaxTokens(strings.NewReader(`{"model":"gpt-5.5","max_output_tokens":2048}`), 512, "max_output_tokens", "max_output_tokens")
	require.NoError(t, err)
	raw, err = io.ReadAll(reader)
	require.NoError(t, err)
	require.EqualValues(t, 512, gjson.GetBytes(raw, "max_output_tokens").Int())

	reader, err = clampKeeperProxyJSONMaxTokens(strings.NewReader(`{"model":"gpt-5.5","max_output_tokens":128}`), 512, "max_output_tokens", "max_output_tokens")
	require.NoError(t, err)
	raw, err = io.ReadAll(reader)
	require.NoError(t, err)
	require.EqualValues(t, 128, gjson.GetBytes(raw, "max_output_tokens").Int())
}

func TestClampKeeperProxyJSONMaxTokensSupportsAnthropicField(t *testing.T) {
	reader, err := clampKeeperProxyJSONMaxTokens(strings.NewReader(`{"model":"claude-opus-4-8","max_tokens":4096}`), 512, "max_tokens", "max_tokens")
	require.NoError(t, err)
	raw, err := io.ReadAll(reader)
	require.NoError(t, err)
	require.EqualValues(t, 512, gjson.GetBytes(raw, "max_tokens").Int())
}
