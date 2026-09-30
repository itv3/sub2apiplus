//go:build unit

package service

import (
	"net/http"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestStructuredOutputsBetaOAuthMimic(t *testing.T) {
	const beta = "structured-outputs-2025-11-13"
	for _, tc := range []struct {
		name   string
		header string
		drop   map[string]struct{}
		want   bool
	}{
		{"explicit", beta, nil, true},
		{"mixed and duplicate", "custom-beta, " + beta + "," + beta, nil, true},
		{"absent", "custom-beta", nil, false},
		{"similar token", beta + "-other", nil, false},
		{"filtered", beta, map[string]struct{}{beta: {}}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := newTestGatewayServiceForBeta(false)
			header := http.Header{}
			header.Set("anthropic-beta", tc.header)
			body := []byte(`{"output_format":{"type":"json_schema","schema":{"type":"object"}}}`)
			got, set := s.computeFinalAnthropicBeta("oauth", true, "claude-sonnet-5", header, body, tc.drop)
			require.True(t, set)
			require.Equal(t, tc.want, containsBetaToken(got, beta))
			require.False(t, containsBetaToken(got, "custom-beta"))
			require.True(t, containsBetaToken(got, "oauth-2025-04-20"))
			if tc.want {
				require.Equal(t, 1, strings.Count(got, beta))
			}
		})
	}
}
