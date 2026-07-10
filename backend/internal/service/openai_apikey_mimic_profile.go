package service

import (
	"context"
	"net/http"
	"strings"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai_compat"
	"github.com/gin-gonic/gin"
	"github.com/google/uuid"
)

type openAIAPIKeyMimicRequestContextKey struct{}

const (
	openAIAPIKeyCodexMimicProfileExtraKey = "openai_apikey_mimic_codex_profile"

	openAIAPIKeyCodexMimicClientDesktop0142 = "desktop_0_142"
	openAIAPIKeyCodexMimicClientCLIRS0125   = "cli_rs_0_125"
)

type openAIAPIKeyCodexMimicProfile struct {
	Enabled bool
	Scope   openAIAPIKeyCodexMimicScope
	Client  openAIAPIKeyCodexMimicClientProfile
}

type openAIUpstreamRequestPlan struct {
	IsStream         bool
	IsCompact        bool
	PromptCacheKey   string
	IsCodexCLI       bool
	APIKeyCodexMimic openAIAPIKeyCodexMimicProfile
}

func resolveOpenAIAPIKeyCodexMimicProfile(account *Account, apiKeyID int64, cfg *config.Config) openAIAPIKeyCodexMimicProfile {
	client := resolveOpenAIAPIKeyCodexMimicClientProfile(account)
	scope := resolveOpenAIAPIKeyCodexMimicScope(account, apiKeyID, cfg)
	scope.ClientProfile = client.ID
	if client.IsDesktop {
		scope.TurnID = uuid.NewString()
		scope.TurnStartedAtUnixMS = time.Now().UnixMilli()
	}
	return openAIAPIKeyCodexMimicProfile{
		Enabled: account != nil && account.IsOpenAIAPIKeyCodexMimicEnabled(),
		Scope:   scope,
		Client:  client,
	}
}

func resolveOpenAIAPIKeyCodexMimicProfileForRequest(account *Account, apiKeyID int64, cfg *config.Config, c *gin.Context) openAIAPIKeyCodexMimicProfile {
	profile := resolveOpenAIAPIKeyCodexMimicProfile(account, apiKeyID, cfg)
	if profile.Enabled && isInboundOpenAIOfficialClient(c) {
		profile.Enabled = false
	}
	return profile
}

// WithOpenAIAPIKeyMimicRequestContext 记录本次入站请求是否来自 Codex 官方客户端。
// 调度发生在账号选定之前，因此必须把请求身份放入标准 context，确保账号筛选与最终转发使用同一套 mimic/WS 判定。
func WithOpenAIAPIKeyMimicRequestContext(ctx context.Context, c *gin.Context) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	return context.WithValue(ctx, openAIAPIKeyMimicRequestContextKey{}, isInboundOpenAIOfficialClient(c))
}

func isOpenAIOfficialClientRequestContext(ctx context.Context) bool {
	if ctx == nil {
		return false
	}
	official, _ := ctx.Value(openAIAPIKeyMimicRequestContextKey{}).(bool)
	return official
}

func isInboundOpenAIOfficialClient(c *gin.Context) bool {
	if c == nil {
		return false
	}
	return openai.IsCodexOfficialClientRequestStrict(c.GetHeader("User-Agent")) ||
		openai.IsCodexOfficialClientOriginator(c.GetHeader("originator"))
}

func (p openAIAPIKeyCodexMimicProfile) RewriteBody(body []byte) []byte {
	if !p.Enabled {
		return body
	}
	return applyOpenAIAPIKeyCodexMimicryToBody(body, p.Scope)
}

func (p openAIAPIKeyCodexMimicProfile) ApplyHeaders(req *http.Request, isStream bool) {
	if !p.Enabled {
		return
	}
	applyOpenAIAPIKeyCodexMimicHeaders(req, isStream, p.Scope)
}

func (p openAIAPIKeyCodexMimicProfile) ShouldUseResponsesAPI(extra map[string]any) bool {
	return openai_compat.ShouldUseResponsesAPIForProfile(extra, p.Enabled)
}

func (p openAIAPIKeyCodexMimicProfile) ResolveResponsesSupport(extra map[string]any) openai_compat.AccountResponsesSupport {
	return openai_compat.ResolveResponsesSupportForProfile(extra, p.Enabled)
}

func (p openAIAPIKeyCodexMimicProfile) ShouldUseTLSFingerprint(account *Account) bool {
	return p.Enabled && account != nil && account.ShouldUseOpenAITLSFingerprint()
}

type openAIAPIKeyCodexMimicClientProfile struct {
	ID           string
	UserAgent    string
	Originator   string
	Version      string
	OpenAIBeta   string
	IsDesktop    bool
	BetaFeatures string
}

func resolveOpenAIAPIKeyCodexMimicClientProfile(account *Account) openAIAPIKeyCodexMimicClientProfile {
	profileID := ""
	if account != nil {
		profileID = account.GetExtraString(openAIAPIKeyCodexMimicProfileExtraKey)
	}
	return resolveOpenAIAPIKeyCodexMimicClientProfileByID(profileID)
}

func resolveOpenAIAPIKeyCodexMimicClientProfileFromScope(scope openAIAPIKeyCodexMimicScope) openAIAPIKeyCodexMimicClientProfile {
	return resolveOpenAIAPIKeyCodexMimicClientProfileByID(scope.ClientProfile)
}

func resolveOpenAIAPIKeyCodexMimicClientProfileByID(profileID string) openAIAPIKeyCodexMimicClientProfile {
	switch normalizeOpenAIAPIKeyCodexMimicClientProfileID(profileID) {
	case openAIAPIKeyCodexMimicClientCLIRS0125:
		return openAIAPIKeyCodexMimicClientProfile{
			ID:         openAIAPIKeyCodexMimicClientCLIRS0125,
			UserAgent:  codexCLIUserAgent,
			Originator: "codex_cli_rs",
			Version:    codexCLIVersion,
			OpenAIBeta: "responses=experimental",
		}
	default:
		return openAIAPIKeyCodexMimicClientProfile{
			ID:           openAIAPIKeyCodexMimicClientDesktop0142,
			UserAgent:    codexDesktopUserAgent,
			Originator:   codexDesktopOriginator,
			IsDesktop:    true,
			BetaFeatures: codexDesktopBetaFeatures,
		}
	}
}

func normalizeOpenAIAPIKeyCodexMimicClientProfileID(profileID string) string {
	v := strings.ToLower(strings.TrimSpace(profileID))
	switch strings.ReplaceAll(v, "-", "_") {
	case "", "desktop", "codex_desktop", "desktop_0_142", "codex_desktop_0_142":
		return openAIAPIKeyCodexMimicClientDesktop0142
	case "cli", "codex_cli", "cli_rs", "codex_cli_rs", "cli_rs_0_125", "codex_cli_rs_0_125":
		return openAIAPIKeyCodexMimicClientCLIRS0125
	default:
		return openAIAPIKeyCodexMimicClientDesktop0142
	}
}
