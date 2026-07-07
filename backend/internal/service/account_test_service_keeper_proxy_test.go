package service

import (
	"net/http"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestValidateKeeperOpenAIProxyPath(t *testing.T) {
	allowed := []string{
		"/v1/responses",
		"/v1/responses/resp_123/input_items",
		"/responses",
		"/chat/completions",
		"/v1/chat/completions",
		"/models",
		"/v1/models",
	}
	for _, path := range allowed {
		got, err := validateKeeperOpenAIProxyPath(path)
		require.NoError(t, err, path)
		require.Equal(t, path, got)
	}

	for _, path := range []string{"/v1/files", "/v1/fine_tuning/jobs", "/v1/../models", "/v1/models?limit=1"} {
		_, err := validateKeeperOpenAIProxyPath(path)
		require.Error(t, err, path)
	}
}

func TestValidateKeeperAnthropicProxyPath(t *testing.T) {
	for _, path := range []string{"/v1/messages", "/v1/messages/count_tokens", "/v1/models"} {
		got, err := validateKeeperAnthropicProxyPath(path)
		require.NoError(t, err, path)
		require.Equal(t, path, got)
	}

	for _, path := range []string{"/v1/files", "/v1/messages/../../models", "/v1/messages?beta=true"} {
		_, err := validateKeeperAnthropicProxyPath(path)
		require.Error(t, err, path)
	}
}

func TestCopyProxyRequestHeadersStripsCredentialsAndHopByHopHeaders(t *testing.T) {
	src := http.Header{
		"Authorization":  []string{"Bearer keeper"},
		"X-Api-Key":      []string{"keeper"},
		"Connection":     []string{"close"},
		"Content-Type":   []string{"application/json"},
		"Anthropic-Beta": []string{"tools-2024-05-16"},
	}
	dst := http.Header{}

	copyProxyRequestHeaders(dst, src)

	require.Empty(t, dst.Get("Authorization"))
	require.Empty(t, dst.Get("X-Api-Key"))
	require.Empty(t, dst.Get("Connection"))
	require.Equal(t, "application/json", dst.Get("Content-Type"))
	require.Equal(t, "tools-2024-05-16", dst.Get("Anthropic-Beta"))
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
}
