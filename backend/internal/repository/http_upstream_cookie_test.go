package repository

import (
	"io"
	"net/http"
	"net/http/cookiejar"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

type cookieRoundTripper func(*http.Request) (*http.Response, error)

func (f cookieRoundTripper) RoundTrip(req *http.Request) (*http.Response, error) {
	return f(req)
}

func TestHTTPClientWithCookieJarConsumesAndReplaysSetCookie(t *testing.T) {
	requestCount := 0
	secondCookie := ""
	base := &http.Client{Transport: cookieRoundTripper(func(req *http.Request) (*http.Response, error) {
		requestCount++
		response := &http.Response{
			StatusCode: http.StatusOK,
			Header:     make(http.Header),
			Body:       io.NopCloser(strings.NewReader("{}")),
			Request:    req,
		}
		if requestCount == 1 {
			response.Header.Add("Set-Cookie", "_cfuvid=abc; Path=/; Secure; HttpOnly")
		} else {
			secondCookie = req.Header.Get("Cookie")
		}
		return response, nil
	})}
	jar, err := cookiejar.New(nil)
	require.NoError(t, err)
	client := httpClientWithCookieJar(base, jar)

	first, err := client.Get("https://chatgpt.com/backend-api/codex/models")
	require.NoError(t, err)
	require.NoError(t, first.Body.Close())
	second, err := client.Get("https://chatgpt.com/backend-api/codex/responses")
	require.NoError(t, err)
	require.NoError(t, second.Body.Close())
	require.Contains(t, secondCookie, "_cfuvid=abc")
}
