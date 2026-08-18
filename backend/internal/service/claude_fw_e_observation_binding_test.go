package service

import (
	"bytes"
	"context"
	"io"
	"net/http"
	"reflect"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

func TestClaudeFWEObservationBindingChangesOnlyRequestContext(t *testing.T) {
	body := []byte(`{"model":"claude-sonnet-5","messages":[]}`)
	req, err := http.NewRequestWithContext(
		context.Background(),
		http.MethodPost,
		"https://api.anthropic.com/v1/messages?beta=true",
		bytes.NewReader(body),
	)
	require.NoError(t, err)
	req.Header.Set("Anthropic-Version", "2023-06-01")
	req.Header.Set("Authorization", "Bearer test-token")

	beforeURL := req.URL.String()
	beforeHeader := req.Header.Clone()
	beforeLength := req.ContentLength

	bound, err := bindClaudeFWELegacyObservationRequest(
		req,
		officialegress.SinkClaudeLegacyMessagesInference,
		http.MethodPost,
		"api.anthropic.com",
		"/v1/messages",
	)
	require.NoError(t, err)
	identity, ok := officialegress.AttemptIdentityFromContext(bound.Context())
	require.True(t, ok)
	require.Equal(t, officialegress.SinkClaudeLegacyMessagesInference, identity.SinkID)
	require.Equal(t, officialegress.PersonaUnclassified, identity.DeclaredPersona)
	require.Equal(t, beforeURL, bound.URL.String())
	require.True(t, reflect.DeepEqual(beforeHeader, bound.Header))
	require.Equal(t, beforeLength, bound.ContentLength)
	actualBody, err := io.ReadAll(bound.Body)
	require.NoError(t, err)
	require.Equal(t, body, actualBody)
}

func TestClaudeFWEObservationBindingDoesNotClaimOtherProductTraffic(t *testing.T) {
	testCases := []struct {
		name   string
		method string
		target string
	}{
		{name: "API Key 自定义主机", method: http.MethodPost, target: "https://anthropic.example.com/v1/messages"},
		{name: "非 TLS", method: http.MethodPost, target: "http://api.anthropic.com/v1/messages"},
		{name: "非标准端口", method: http.MethodPost, target: "https://api.anthropic.com:8443/v1/messages"},
		{name: "其他路径", method: http.MethodPost, target: "https://api.anthropic.com/v1/complete"},
		{name: "其他方法", method: http.MethodGet, target: "https://api.anthropic.com/v1/messages"},
	}
	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			req, err := http.NewRequestWithContext(
				context.Background(), testCase.method, testCase.target, nil,
			)
			require.NoError(t, err)
			bound, err := bindClaudeFWELegacyObservationRequest(
				req,
				officialegress.SinkClaudeLegacyMessagesInference,
				http.MethodPost,
				"api.anthropic.com",
				"/v1/messages",
			)
			require.NoError(t, err)
			_, ok := officialegress.AttemptIdentityFromContext(bound.Context())
			require.False(t, ok)
		})
	}
}

func TestClaudeOAuthRequestBuildersBindExpectedFWEObservationSinks(t *testing.T) {
	account := officialEgressT4AnthropicAccount(
		"11111111-1111-4111-8111-111111111111",
	)
	body := officialEgressT4AnthropicBody(
		"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
		"22222222-2222-4222-8222-222222222222",
	)
	ctx := officialEgressT4GinContext("22222222-2222-4222-8222-222222222222")
	gateway := &GatewayService{}

	messages, _, err := gateway.buildUpstreamRequest(
		context.Background(), ctx, account, body, "oauth-token", "oauth",
		"claude-sonnet-5", true, false,
	)
	require.NoError(t, err)
	assertClaudeFWEObservationIdentity(
		t, messages, officialegress.SinkClaudeLegacyMessagesInference,
	)

	countTokens, _, err := gateway.buildCountTokensRequest(
		context.Background(), ctx, account, body, "oauth-token", "oauth",
		"claude-sonnet-5", false,
	)
	require.NoError(t, err)
	assertClaudeFWEObservationIdentity(
		t, countTokens, officialegress.SinkClaudeLegacyTokenCount,
	)

	account.Credentials = map[string]any{"access_token": "oauth-token"}
	models, err := (&AccountTestService{
		cfg: upstreamModelSyncTestConfig(),
	}).buildAnthropicUpstreamModelsRequest(
		context.Background(), account,
	)
	require.NoError(t, err)
	assertClaudeFWEObservationIdentity(
		t, models, officialegress.SinkClaudeLegacyUpstreamModels,
	)
}

func assertClaudeFWEObservationIdentity(
	t *testing.T,
	req *http.Request,
	want officialegress.SinkID,
) {
	t.Helper()
	identity, ok := officialegress.AttemptIdentityFromContext(req.Context())
	require.True(t, ok)
	require.Equal(t, want, identity.SinkID)
	require.Equal(t, officialegress.PersonaUnclassified, identity.DeclaredPersona)
}
