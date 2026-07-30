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

	openAIAPIKeyCodexMimicClientCodexExec0145 = "codex_exec_0_145"
	openAIAPIKeyCodexMimicClientDesktop0144   = "desktop_0_144"
	openAIAPIKeyCodexMimicClientCLIRS0125     = "cli_rs_0_125"
)

type openAIAPIKeyCodexMimicProfile struct {
	Enabled bool
	Scope   openAIAPIKeyCodexMimicScope
	Client  openAIAPIKeyCodexMimicClientProfile
}

type openAIUpstreamRequestPlan struct {
	IsStream                   bool
	IsCompact                  bool
	PromptCacheKey             string
	IsCodexCLI                 bool
	APIKeyCodexMimic           openAIAPIKeyCodexMimicProfile
	OfficialEgressBodyContract *officialOpenAIHTTPBodyContract
	// OfficialEgressTurnState 必须在终态 Header Finalizer 内写入；禁止发送层在
	// Finalizer 之后补头，否则条件槽位与最终校验都会被绕过。
	OfficialEgressTurnState string
}

func resolveOpenAIAPIKeyCodexMimicProfile(account *Account, apiKeyID int64, cfg *config.Config) openAIAPIKeyCodexMimicProfile {
	client := resolveOpenAIAPIKeyCodexMimicClientProfile(account, cfg)
	scope := resolveOpenAIAPIKeyCodexMimicScope(account, apiKeyID, cfg)
	scope.ClientProfile = client.ID
	if client.RequiresMetadata {
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
	return p.Enabled && !p.Client.IsDesktop && account != nil && account.ShouldUseOpenAITLSFingerprint()
}

type openAIAPIKeyCodexMimicClientProfile struct {
	ID               string
	UserAgent        string
	Originator       string
	Version          string
	OpenAIBeta       string
	IsDesktop        bool
	RequiresMetadata bool
	StaticHeaders    []officialClientHeaderValue
	Sandbox          string
	WorkspaceKind    string
}

// resolveOpenAIAPIKeyCodexMimicClientProfile 解析当前服务级发布画像。
// cfg 为必填参数（允许显式传 nil 表示按 active 处理）：可选变参会让调用方漏传
// 配置时静默回落到 active，从而在 mode=previous 下把 Desktop header 与
// active CLI 的 TLS 画像混用，因此这里必须由调用方显式给出配置来源。
func resolveOpenAIAPIKeyCodexMimicClientProfile(account *Account, cfg *config.Config) openAIAPIKeyCodexMimicClientProfile {
	// 画像版本只由服务级 active/previous 指针决定。历史账号字段
	// openai_apikey_mimic_codex_profile 作为 dormant 数据保留，不得让单个账号
	// 继续锁定 Desktop 画像或与当前发布画像混用。
	_ = account
	mode := officialClientProfileModeFromConfig(cfg)
	profileID := openAIAPIKeyCodexMimicClientCodexExec0145
	if mode == officialClientProfileModePrevious {
		profileID = openAIAPIKeyCodexMimicClientDesktop0144
	}
	return resolveOpenAIAPIKeyCodexMimicClientProfileByID(profileID)
}

func resolveOpenAIAPIKeyCodexMimicClientProfileFromScope(scope openAIAPIKeyCodexMimicScope) openAIAPIKeyCodexMimicClientProfile {
	return resolveOpenAIAPIKeyCodexMimicClientProfileByID(scope.ClientProfile)
}

func resolveOpenAIAPIKeyCodexMimicClientProfileByID(profileID string) openAIAPIKeyCodexMimicClientProfile {
	switch normalizeOpenAIAPIKeyCodexMimicClientProfileID(profileID) {
	case openAIAPIKeyCodexMimicClientCodexExec0145:
		profile, err := resolveOfficialClientProfile(
			officialClientPurposeOpenAIAPIKeyResponsesHTTP,
			officialClientProfileModeActive,
		)
		if err != nil {
			return openAIAPIKeyCodexMimicClientProfile{}
		}
		return openAIAPIKeyCodexMimicClientProfile{
			ID:               openAIAPIKeyCodexMimicClientCodexExec0145,
			UserAgent:        profile.Build.UserAgent,
			Originator:       profile.Build.Originator,
			Version:          profile.Build.Version,
			RequiresMetadata: true,
			StaticHeaders:    profile.Wire.StaticHeaders,
			Sandbox:          "seccomp",
		}
	case openAIAPIKeyCodexMimicClientCLIRS0125:
		return openAIAPIKeyCodexMimicClientProfile{
			ID:         openAIAPIKeyCodexMimicClientCLIRS0125,
			UserAgent:  codexCLIUserAgent,
			Originator: "codex_cli_rs",
			Version:    codexCLIVersion,
			OpenAIBeta: "responses=experimental",
		}
	default:
		profile, _ := resolveOfficialClientProfile(
			officialClientPurposeOpenAIAPIKeyResponsesHTTP,
			officialClientProfileModePrevious,
		)
		return openAIAPIKeyCodexMimicClientProfile{
			ID:               openAIAPIKeyCodexMimicClientDesktop0144,
			UserAgent:        codexDesktopUserAgent,
			Originator:       codexDesktopOriginator,
			IsDesktop:        true,
			RequiresMetadata: true,
			StaticHeaders:    profile.Wire.StaticHeaders,
			Sandbox:          "none",
			WorkspaceKind:    "project",
		}
	}
}

func normalizeOpenAIAPIKeyCodexMimicClientProfileID(profileID string) string {
	v := strings.ToLower(strings.TrimSpace(profileID))
	switch strings.ReplaceAll(v, "-", "_") {
	case "", "codex_exec", "codex_exec_0_145", "codex_cli_0_145", "cli_0_145":
		return openAIAPIKeyCodexMimicClientCodexExec0145
	case "desktop", "codex_desktop", "desktop_0_142", "codex_desktop_0_142", "desktop_0_144", "codex_desktop_0_144":
		return openAIAPIKeyCodexMimicClientDesktop0144
	case "cli", "codex_cli", "cli_rs", "codex_cli_rs", "cli_rs_0_125", "codex_cli_rs_0_125":
		return openAIAPIKeyCodexMimicClientCLIRS0125
	default:
		return openAIAPIKeyCodexMimicClientCodexExec0145
	}
}
