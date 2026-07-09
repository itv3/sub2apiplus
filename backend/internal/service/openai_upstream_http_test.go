package service

import (
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/model"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

type openAIHTTPUpstreamChoiceRecorder struct {
	doCalled        bool
	doWithTLSCalled bool
	lastTLSProfile  *tlsfingerprint.Profile
}

func (r *openAIHTTPUpstreamChoiceRecorder) Do(_ *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	r.doCalled = true
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("{}"))}, nil
}

func (r *openAIHTTPUpstreamChoiceRecorder) DoWithTLS(_ *http.Request, _ string, _ int64, _ int, profile *tlsfingerprint.Profile) (*http.Response, error) {
	r.doWithTLSCalled = true
	r.lastTLSProfile = profile
	return &http.Response{StatusCode: http.StatusOK, Body: io.NopCloser(strings.NewReader("{}"))}, nil
}

func TestResolveOpenAIAPIKeyCodexTLSProfileUsesCapturedDesktopDefault(t *testing.T) {
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli": true,
			"enable_tls_fingerprint":        true,
		},
	}
	got := resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{})
	require.NotNil(t, got)
	require.Contains(t, got.Name, "Codex Desktop 0.142.0")
	require.Equal(t, []uint16{0, 10, 11, 13, 5, 18, 23}, got.Extensions)
	require.Empty(t, got.ALPNProtocols)
	require.Empty(t, got.SupportedVersions)
	require.Equal(t, uint16(0x0301), got.TLSVersMin)
	require.Equal(t, uint16(0x0303), got.TLSVersMax)

	account.Extra["tls_fingerprint_profile_id"] = int64(42)
	got = resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{})
	require.NotNil(t, got)
	require.Contains(t, got.Name, "Codex Desktop 0.142.0")

	svc := &TLSFingerprintProfileService{
		localCache: map[int64]*model.TLSFingerprintProfile{
			42: {
				ID:            42,
				Name:          "codex-cli-captured",
				ALPNProtocols: []string{"h2", "http/1.1"},
			},
		},
	}
	got = resolveOpenAIAPIKeyCodexTLSProfile(account, svc)
	require.NotNil(t, got)
	require.Equal(t, "codex-cli-captured", got.Name)
	require.Equal(t, []string{"h2", "http/1.1"}, got.ALPNProtocols)
}

func TestResolveOpenAIAPIKeyCodexTLSProfileUsesCLIDefaultWhenRequested(t *testing.T) {
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli":     true,
			"openai_apikey_mimic_codex_profile": "cli_rs_0_125",
			"enable_tls_fingerprint":            true,
		},
	}

	got := resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{})
	require.NotNil(t, got)
	require.Equal(t, "Built-in Default (Node.js 24.x)", got.Name)
	require.Empty(t, got.CipherSuites)
	require.Empty(t, got.Extensions)
}

func TestDoOpenAIHTTPUpstreamUsesCapturedDesktopTLSProfileByDefault(t *testing.T) {
	account := &Account{
		ID:       1,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli": true,
			"enable_tls_fingerprint":        true,
		},
	}
	req, err := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/responses", strings.NewReader("{}"))
	require.NoError(t, err)

	recorder := &openAIHTTPUpstreamChoiceRecorder{}
	resp, err := doOpenAIHTTPUpstream(recorder, req, "", account, &TLSFingerprintProfileService{})
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.False(t, recorder.doCalled)
	require.True(t, recorder.doWithTLSCalled)
	require.NotNil(t, recorder.lastTLSProfile)
	require.Contains(t, recorder.lastTLSProfile.Name, "Codex Desktop 0.142.0")
	require.Empty(t, recorder.lastTLSProfile.ALPNProtocols)
	require.Empty(t, recorder.lastTLSProfile.SupportedVersions)
	require.Equal(t, uint16(0x0303), recorder.lastTLSProfile.TLSVersMax)

	account.Extra["tls_fingerprint_profile_id"] = int64(42)
	tlsSvc := &TLSFingerprintProfileService{
		localCache: map[int64]*model.TLSFingerprintProfile{
			42: {ID: 42, Name: "captured-codex"},
		},
	}
	recorder = &openAIHTTPUpstreamChoiceRecorder{}
	resp, err = doOpenAIHTTPUpstream(recorder, req, "", account, tlsSvc)
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.False(t, recorder.doCalled)
	require.True(t, recorder.doWithTLSCalled)
	require.NotNil(t, recorder.lastTLSProfile)
	require.Equal(t, "captured-codex", recorder.lastTLSProfile.Name)
}

func TestDoOpenAIHTTPUpstreamUsesCLITLSProfileWhenRequested(t *testing.T) {
	account := &Account{
		ID:       2,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli":     true,
			"openai_apikey_mimic_codex_profile": "cli_rs_0_125",
			"enable_tls_fingerprint":            true,
		},
	}
	req, err := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/responses", strings.NewReader("{}"))
	require.NoError(t, err)

	recorder := &openAIHTTPUpstreamChoiceRecorder{}
	resp, err := doOpenAIHTTPUpstream(recorder, req, "", account, &TLSFingerprintProfileService{})
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.False(t, recorder.doCalled)
	require.True(t, recorder.doWithTLSCalled)
	require.NotNil(t, recorder.lastTLSProfile)
	require.Equal(t, "Built-in Default (Node.js 24.x)", recorder.lastTLSProfile.Name)
}
