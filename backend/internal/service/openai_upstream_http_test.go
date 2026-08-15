package service

import (
	"context"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/model"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	utls "github.com/refraction-networking/utls"
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

// previousOfficialClientProfileConfig 返回把服务级画像指针整体切到 previous 的配置，
// 用于复现紧急回退模式下的出站决策。
func previousOfficialClientProfileConfig() *config.Config {
	return &config.Config{Gateway: config.GatewayConfig{
		OfficialClientProfiles: config.GatewayOfficialClientProfilesConfig{
			Mode: officialClientProfileModePrevious,
		},
	}}
}

func newOpenAIAPIKeyMimicTLSAccount(id int64) *Account {
	return &Account{
		ID:       id,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli": true,
			"enable_tls_fingerprint":        true,
		},
	}
}

func TestResolveOpenAIAPIKeyCodexTLSProfileUsesCurrentCLIDefault(t *testing.T) {
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli": true,
			"enable_tls_fingerprint":        true,
		},
	}
	got := resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{}, nil)
	require.NotNil(t, got)
	require.Contains(t, got.Name, "Codex CLI "+activeOpenAICodexVersionForTest())
	require.Len(t, got.CipherSuites, 30)
	require.Empty(t, got.ALPNProtocols)
	require.Equal(t, []uint16{0x11ec, 0x001d, 0x0017, 0x001e, 0x0018, 0x0019, 0x0100, 0x0101}, got.Curves)
	require.Equal(t, uint16(0x0303), got.TLSVersMin)
	require.Equal(t, uint16(0x0304), got.TLSVersMax)
	require.True(t, got.Transport.DisableCompression)
	require.False(t, got.Transport.StrictH1Wire)
	require.Empty(t, got.Transport.H1HeaderOrders)

	account.Extra["tls_fingerprint_profile_id"] = int64(42)
	got = resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{}, nil)
	require.NotNil(t, got)
	require.Contains(t, got.Name, "Codex CLI "+activeOpenAICodexVersionForTest())

	svc := &TLSFingerprintProfileService{
		localCache: map[int64]*model.TLSFingerprintProfile{
			42: {
				ID:            42,
				Name:          "codex-cli-captured",
				ALPNProtocols: []string{"h2", "http/1.1"},
			},
		},
	}
	got = resolveOpenAIAPIKeyCodexTLSProfile(account, svc, nil)
	require.NotNil(t, got)
	require.Equal(t, "codex-cli-captured", got.Name)
	require.Equal(t, []string{"h2", "http/1.1"}, got.ALPNProtocols)
	require.False(t, got.Transport.DisableCompression)
}

func TestResolveOpenAIAPIKeyCodexTLSProfileIgnoresDormantAccountProfile(t *testing.T) {
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli":     true,
			"openai_apikey_mimic_codex_profile": "cli_rs_0_125",
			"enable_tls_fingerprint":            true,
		},
	}

	got := resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{}, nil)
	require.NotNil(t, got)
	require.Contains(t, got.Name, "Codex CLI "+activeOpenAICodexVersionForTest())
	require.Len(t, got.CipherSuites, 30)
	require.NotEmpty(t, got.Extensions)
	require.True(t, got.Transport.DisableCompression)
}

// TestResolveOpenAIAPIKeyCodexTLSProfilePreviousModeFallsBackToStandardTransport 固定
// previous 回退画像的 TLS 语义：Desktop 画像不套 mimic 指纹，走标准 Transport。
// 漏传 cfg 时曾静默回落到 active，使 previous 的 Desktop header 与 active 的 CLI
// TLS 画像混用，这正是 README §1.2.4 禁止的按字段混用。
func TestResolveOpenAIAPIKeyCodexTLSProfilePreviousModeFallsBackToStandardTransport(t *testing.T) {
	account := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli": true,
			"enable_tls_fingerprint":        true,
		},
	}

	require.Nil(t, resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{}, previousOfficialClientProfileConfig()))

	// 同一账号在 active 模式下仍使用当前 CLI 实抓画像，证明差异只来自服务级指针。
	got := resolveOpenAIAPIKeyCodexTLSProfile(account, &TLSFingerprintProfileService{}, nil)
	require.NotNil(t, got)
	require.Contains(t, got.Name, "Codex CLI "+activeOpenAICodexVersionForTest())
}

func TestDoOpenAIHTTPUpstreamUsesCurrentCLITLSProfileByDefault(t *testing.T) {
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
	resp, err := doOpenAIHTTPUpstreamWithProfile(recorder, req, "", account, &TLSFingerprintProfileService{}, resolveOpenAIAPIKeyCodexMimicProfile(account, 0, nil))
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.False(t, recorder.doCalled)
	require.True(t, recorder.doWithTLSCalled)
	require.NotNil(t, recorder.lastTLSProfile)
	require.Contains(t, recorder.lastTLSProfile.Name, "Codex CLI "+activeOpenAICodexVersionForTest())
	require.Empty(t, recorder.lastTLSProfile.ALPNProtocols)
	require.Equal(t, []uint16{utls.VersionTLS13, utls.VersionTLS12}, recorder.lastTLSProfile.SupportedVersions)
	require.Equal(t, uint16(0x0304), recorder.lastTLSProfile.TLSVersMax)

	account.Extra["tls_fingerprint_profile_id"] = int64(42)
	tlsSvc := &TLSFingerprintProfileService{
		localCache: map[int64]*model.TLSFingerprintProfile{
			42: {ID: 42, Name: "captured-codex"},
		},
	}
	recorder = &openAIHTTPUpstreamChoiceRecorder{}
	resp, err = doOpenAIHTTPUpstreamWithProfile(recorder, req, "", account, tlsSvc, resolveOpenAIAPIKeyCodexMimicProfile(account, 0, nil))
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.False(t, recorder.doCalled)
	require.True(t, recorder.doWithTLSCalled)
	require.NotNil(t, recorder.lastTLSProfile)
	require.Equal(t, "captured-codex", recorder.lastTLSProfile.Name)
}

func TestDoOpenAIHTTPUpstreamIgnoresDormantAccountProfile(t *testing.T) {
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
	resp, err := doOpenAIHTTPUpstreamWithProfile(recorder, req, "", account, &TLSFingerprintProfileService{}, resolveOpenAIAPIKeyCodexMimicProfile(account, 0, nil))
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.False(t, recorder.doCalled)
	require.True(t, recorder.doWithTLSCalled)
	require.NotNil(t, recorder.lastTLSProfile)
	require.Contains(t, recorder.lastTLSProfile.Name, "Codex CLI "+activeOpenAICodexVersionForTest())
}

func TestDoOpenAIHTTPUpstreamSkipsMimicTLSWhenRequestProfileDisabled(t *testing.T) {
	account := &Account{
		ID:       3,
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"openai_apikey_mimic_codex_cli": true,
			"enable_tls_fingerprint":        true,
		},
	}
	req, err := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/responses", strings.NewReader("{}"))
	require.NoError(t, err)

	// 零值画像即「本次请求不做 mimic」：passthrough / WS-HTTP 桥接就是这样调用的。
	recorder := &openAIHTTPUpstreamChoiceRecorder{}
	resp, err := doOpenAIHTTPUpstreamWithProfile(recorder, req, "", account, &TLSFingerprintProfileService{}, openAIAPIKeyCodexMimicProfile{})
	require.NoError(t, err)
	require.Equal(t, http.StatusOK, resp.StatusCode)
	require.True(t, recorder.doCalled)
	require.False(t, recorder.doWithTLSCalled)
	require.Nil(t, recorder.lastTLSProfile)
}

func TestDoOpenAIHTTPUpstreamRejectsCodexOfficialContextOutsideExecutor(t *testing.T) {
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	ingress := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	account := newOfficialOpenAIHTTPTestAccount(991)
	request := httptest.NewRequest(
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/responses",
		strings.NewReader(string(body)),
	)
	request, err := attachOfficialEgressHTTPContext(
		request, ingress, account, PlatformOpenAI,
	)
	require.NoError(t, err)

	recorder := &openAIHTTPUpstreamChoiceRecorder{}
	response, err := doOpenAIHTTPUpstreamWithProfile(
		recorder,
		request,
		"",
		account,
		&TLSFingerprintProfileService{},
		openAIAPIKeyCodexMimicProfile{},
	)
	require.Nil(t, response)
	require.ErrorContains(t, err, "必须通过 CodexEgressExecutor")
	require.False(t, recorder.doCalled)
	require.False(t, recorder.doWithTLSCalled)
}

// TestOpenAIMimicTLSDecisionMatchesBetweenGatewayAndAccountTest 固定验收点：账号测试
// 与正式 Gateway 必须对同一账号做出相同的 TLS 决策，包括 mode=previous。
//
// 修复前账号测试走的 doOpenAIHTTPUpstream 不接收请求级画像，会在函数内部用不带 cfg 的
// resolveOpenAIAPIKeyCodexMimicClientProfile 重解析，previous 下静默得到 active 的
// CLI 画像并套上 TLS 指纹，而 Gateway 的 Desktop 画像走标准 Transport，两者相反。
func TestOpenAIMimicTLSDecisionMatchesBetweenGatewayAndAccountTest(t *testing.T) {
	newUpstreamRequest := func(t *testing.T) *http.Request {
		t.Helper()
		req, err := http.NewRequest(http.MethodPost, "https://api.openai.com/v1/responses", strings.NewReader("{}"))
		require.NoError(t, err)
		return req
	}

	for _, testCase := range []struct {
		name            string
		cfg             *config.Config
		wantTLS         bool
		wantProfileName string
	}{
		{
			name:            "active 画像套用当前 CLI 指纹",
			cfg:             nil,
			wantTLS:         true,
			wantProfileName: "Codex CLI " + activeOpenAICodexVersionForTest(),
		},
		{
			name:    "previous 画像两侧都走标准 Transport",
			cfg:     previousOfficialClientProfileConfig(),
			wantTLS: false,
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			account := newOpenAIAPIKeyMimicTLSAccount(4)

			gatewayRecorder := &openAIHTTPUpstreamChoiceRecorder{}
			gateway := &OpenAIGatewayService{
				httpUpstream:        gatewayRecorder,
				tlsFPProfileService: &TLSFingerprintProfileService{},
				cfg:                 testCase.cfg,
			}
			gatewayResp, err := gateway.doOpenAIHTTPUpstreamForRequest(
				newUpstreamRequest(t),
				"",
				account,
				// c 为 nil 表示没有入站上下文，与账号测试一样按非官方客户端处理。
				resolveOpenAIAPIKeyCodexMimicProfileForRequest(account, 0, gateway.cfg, nil),
			)
			require.NoError(t, err)
			require.NoError(t, gatewayResp.Body.Close())

			accountTestRecorder := &openAIHTTPUpstreamChoiceRecorder{}
			accountTest := &AccountTestService{
				httpUpstream:        accountTestRecorder,
				tlsFPProfileService: &TLSFingerprintProfileService{},
				cfg:                 testCase.cfg,
			}
			accountTestResp, err := doOpenAIHTTPUpstreamWithProfile(
				accountTest.httpUpstream,
				newUpstreamRequest(t),
				"",
				account,
				accountTest.tlsFPProfileService,
				resolveOpenAIAPIKeyCodexMimicProfile(account, 0, accountTest.cfg),
			)
			require.NoError(t, err)
			require.NoError(t, accountTestResp.Body.Close())

			require.Equal(t, testCase.wantTLS, gatewayRecorder.doWithTLSCalled)
			require.Equal(t, gatewayRecorder.doWithTLSCalled, accountTestRecorder.doWithTLSCalled)
			require.Equal(t, gatewayRecorder.doCalled, accountTestRecorder.doCalled)
			if !testCase.wantTLS {
				require.Nil(t, gatewayRecorder.lastTLSProfile)
				require.Nil(t, accountTestRecorder.lastTLSProfile)
				return
			}
			require.NotNil(t, accountTestRecorder.lastTLSProfile)
			require.Contains(t, gatewayRecorder.lastTLSProfile.Name, testCase.wantProfileName)
			require.Equal(t, gatewayRecorder.lastTLSProfile.Name, accountTestRecorder.lastTLSProfile.Name)
			require.Equal(t, gatewayRecorder.lastTLSProfile.CipherSuites, accountTestRecorder.lastTLSProfile.CipherSuites)
		})
	}
}

func TestOfficialCodexExecutorHTTPSelectsDirectAndProxyProfiles(t *testing.T) {
	for _, testCase := range []struct {
		name            string
		proxyURL        string
		wantCipherCount int
		wantALPN        []string
		wantRandomized  bool
	}{
		{
			name:            "直连",
			wantCipherCount: 30,
		},
		{
			name:            "HTTP CONNECT 代理",
			proxyURL:        "http://capture.example:18080",
			wantCipherCount: 30,
		},
	} {
		t.Run(testCase.name, func(t *testing.T) {
			account := &Account{
				ID: 94, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
				Credentials: map[string]any{
					"access_token":       "executor-profile-test",
					"chatgpt_account_id": "acct-executor-profile-test",
				},
			}
			body := newOfficialOpenAIHTTPTestBody(t, false, false, true)
			req, err := http.NewRequest(
				http.MethodPost,
				"https://chatgpt.com/backend-api/codex/responses",
				strings.NewReader(string(body)),
			)
			require.NoError(t, err)
			req.Header.Set("Authorization", "Bearer executor-profile-test")
			req.Header.Set("Content-Type", "application/json")
			req.Header.Set("Accept", "text/event-stream")
			setOpenAIChatGPTAccountHeaders(req.Header, account)

			recorder := &openAIHTTPUpstreamChoiceRecorder{}
			runtimeState, err := newOfficialEgressTestRuntime(recorder)
			require.NoError(t, err)
			resp, err := runtimeState.ExecuteCodexHTTP(context.Background(), OfficialCodexHTTPExecution{
				SinkID: officialEgressSinkResponsesForward, EndpointID: officialCodexEndpointResponsesHTTP,
				Account: account, ProxyURL: testCase.proxyURL, Request: req,
				PolicyID: "changeset3.http-profile-test", PolicySource: "test",
				ConcurrencyLimit: 1,
			})
			require.NoError(t, err)
			require.NoError(t, resp.Body.Close())
			require.True(t, recorder.doWithTLSCalled)
			require.Len(t, recorder.lastTLSProfile.CipherSuites, testCase.wantCipherCount)
			require.Equal(t, testCase.wantALPN, recorder.lastTLSProfile.ALPNProtocols)
			require.Equal(t, testCase.wantRandomized, recorder.lastTLSProfile.RandomizeExtensions)
		})
	}
}
