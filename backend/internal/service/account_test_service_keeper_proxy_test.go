package service

import (
	"context"
	"io"
	"net/http"
	"strings"
	"testing"

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
