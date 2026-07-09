package service

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func newBetaPolicySettingServiceForTest(t *testing.T, token string) *SettingService {
	t.Helper()
	settings := &BetaPolicySettings{
		Rules: []BetaPolicyRule{
			{
				BetaToken: token,
				Action:    BetaPolicyActionBlock,
				Scope:     BetaPolicyScopeAll,
			},
		},
	}
	raw, err := json.Marshal(settings)
	require.NoError(t, err)
	return NewSettingService(&gatewayTTLSettingRepo{
		data: map[string]string{
			SettingKeyBetaPolicySettings: string(raw),
		},
	}, &config.Config{})
}

func TestGatewayServiceAnthropicAPIKeyMimicBlocksAutoInjectedFinalBeta(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request.Header.Set("User-Agent", "same-client/1.0")
	c.Request.RemoteAddr = "1.2.3.4:1234"

	svc := &GatewayService{
		cfg: &config.Config{
			Gateway: config.GatewayConfig{
				InjectBetaForAPIKey: true,
				MaxLineSize:         defaultMaxLineSize,
			},
		},
		settingService: newBetaPolicySettingServiceForTest(t, claude.BetaContext1M),
	}
	account := &Account{
		ID:       303,
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
		Credentials: map[string]any{
			"api_key": "anthropic-key",
		},
	}

	_, _, err := svc.buildUpstreamRequest(
		context.Background(),
		c,
		account,
		[]byte(`{"model":"claude-opus-4-8","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}`),
		"anthropic-key",
		"apikey",
		"claude-opus-4-8",
		false,
		false,
	)
	require.Error(t, err)
	var blocked *BetaBlockedError
	require.ErrorAs(t, err, &blocked)
	require.Contains(t, blocked.Error(), claude.BetaContext1M)
}

func TestGatewayServiceAnthropicAPIKeyMimicCountTokensBlocksAutoInjectedFinalBeta(t *testing.T) {
	gin.SetMode(gin.TestMode)
	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages/count_tokens", nil)

	svc := &GatewayService{
		cfg: &config.Config{
			Gateway: config.GatewayConfig{
				InjectBetaForAPIKey: true,
			},
		},
		settingService: newBetaPolicySettingServiceForTest(t, claude.BetaTokenCounting),
	}
	account := &Account{
		Platform: PlatformAnthropic,
		Type:     AccountTypeAPIKey,
		Extra: map[string]any{
			"anthropic_apikey_mimic_claude_code": true,
		},
		Credentials: map[string]any{
			"api_key": "anthropic-key",
		},
	}

	_, _, err := svc.buildCountTokensRequest(
		context.Background(),
		c,
		account,
		[]byte(`{"model":"claude-3-5-sonnet-latest","messages":[{"role":"user","content":[{"type":"text","text":"hi"}]}]}`),
		"anthropic-key",
		"apikey",
		"claude-3-5-sonnet-latest",
		false,
	)
	require.Error(t, err)
	var blocked *BetaBlockedError
	require.ErrorAs(t, err, &blocked)
	require.Contains(t, blocked.Error(), claude.BetaTokenCounting)
}

func TestBuildAPIKeyMimicMetadataUserIDUsesAPIKeySessionContext(t *testing.T) {
	account := &Account{
		ID: 12,
		Extra: map[string]any{
			"account_uuid": "11111111-1111-1111-1111-111111111111",
		},
	}
	body := []byte(`{"messages":[{"role":"user","content":[{"type":"text","text":"hello"}]}]}`)
	headers := http.Header{}
	headers.Set("User-Agent", "same-client/1.0")

	first := buildAPIKeyMimicMetadataUserID(account, body, headers, "1.1.1.1", 101)
	second := buildAPIKeyMimicMetadataUserID(account, body, headers, "1.1.1.1", 202)

	require.NotEmpty(t, first)
	require.NotEmpty(t, second)
	require.NotEqual(t, first, second)

	firstParsed := ParseMetadataUserID(first)
	secondParsed := ParseMetadataUserID(second)
	require.NotNil(t, firstParsed)
	require.NotNil(t, secondParsed)
	require.NotEqual(t, firstParsed.SessionID, secondParsed.SessionID)
}
