package service

import (
	"encoding/json"
	"fmt"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// openAIOAuthRefreshReason 是 ChatGPT OAuth 令牌刷新请求体里的 refresh_reason 取值。
//
// 官方客户端自 0.164 起，在 POST https://auth.openai.com/oauth/token 的 JSON 请求体里多发一个
// refresh_reason 字段（取值闭集见下），官方注释写明它只是“调用方自报的诊断信息，不作为令牌有效
// 性证据”。字段是否出站由当前 Release 画像的 oauth_refresh 封闭 Body 是否声明它决定：
// 旧画像（0.163 及以前）没有该字段，行为与引入本文件之前逐字节相同。
type openAIOAuthRefreshReason string

const (
	// openAIOAuthRefreshReasonProactiveExpiry 官方触发条件：访问令牌 JWT 的 exp 可解析，且距现在
	// 不足 5 分钟，在发起请求之前主动刷新。我方“后台刷新服务发现令牌进入过期窗口”与“请求路径上
	// 取令牌时发现临近过期”两条路径都对应它。
	openAIOAuthRefreshReasonProactiveExpiry openAIOAuthRefreshReason = "proactive_expiry"
	// openAIOAuthRefreshReasonScheduled 官方触发条件：exp 不可解析、且上次刷新早于 8 天。注意它不是
	// “后台定时器”：官方客户端没有任何后台刷新定时器，刷新只发生在取认证信息的请求路径上。
	// 我方访问令牌恒为带 exp 的 JWT，也不保存 last_refresh，所以当前没有调用点产生该取值，只保留
	// 在闭集里。
	openAIOAuthRefreshReasonScheduled openAIOAuthRefreshReason = "scheduled"
	// openAIOAuthRefreshReasonUnauthorizedRecovery 官方触发条件：上游返回 401 后恢复流程里的“刷新
	// 令牌”一步。我方没有“401 后刷新再重试”的路径，所以当前没有调用点产生该取值。
	openAIOAuthRefreshReasonUnauthorizedRecovery openAIOAuthRefreshReason = "unauthorized_recovery"
	// openAIOAuthRefreshReasonForced 官方触发条件：调用方显式要求刷新（app-server 的
	// getAuthStatus(refreshToken:true) 等公共入口）。我方管理端手动刷新、CRS 同步、粘贴
	// refresh_token 校验都对应它；它也是没有显式说明原因的调用的默认取值。
	openAIOAuthRefreshReasonForced openAIOAuthRefreshReason = "forced"
)

// openAIOAuthRefreshReasonFieldName 是画像 oauth_refresh 封闭 Body 里该字段的名字。
const openAIOAuthRefreshReasonFieldName = "refresh_reason"

// valid 判断取值是否落在官方四值闭集内。画像只校验字段名，不校验取值，所以闭集校验在这里做。
func (r openAIOAuthRefreshReason) valid() bool {
	switch r {
	case openAIOAuthRefreshReasonProactiveExpiry,
		openAIOAuthRefreshReasonScheduled,
		openAIOAuthRefreshReasonUnauthorizedRecovery,
		openAIOAuthRefreshReasonForced:
		return true
	default:
		return false
	}
}

// openAIOAuthRefreshProfileDeclaresReason 判断当前 Release 画像的 oauth_refresh 端点是否在封闭 Body
// 里声明了 refresh_reason。声明了才在语义 Body 里带上它，没声明（旧画像）一个字节都不多发：
// 封闭 Body 会拒绝未声明字段，这里与最终 compiler 读的是同一份画像，判断必然一致。
func openAIOAuthRefreshProfileDeclaresReason(profile profilecontract.ExecutableProfile) bool {
	endpoint, found := profile.Endpoint(officialCodexEndpointOAuthRefresh)
	if !found {
		return false
	}
	for _, field := range endpoint.Body.Fields {
		if field.Name == openAIOAuthRefreshReasonFieldName {
			return true
		}
	}
	return false
}

// buildOpenAIOAuthRefreshSemanticBody 构造 OAuth 刷新的语义 Body（不含 refresh_token，
// refresh_token 由 compiler 作为一次性认证材料追加）。
//
// includeReason 为 false 时输出与引入 refresh_reason 之前完全相同的字节
// {"client_id":…,"grant_type":"refresh_token"}；为 true 时在 grant_type 之后加 refresh_reason。
// 最终线上键序由画像字段序决定，官方用 BTreeMap 编码，恒为字典序
// client_id, grant_type, refresh_reason, refresh_token。
func buildOpenAIOAuthRefreshSemanticBody(
	clientID string,
	reason openAIOAuthRefreshReason,
	includeReason bool,
) ([]byte, error) {
	if !includeReason {
		return json.Marshal(struct {
			ClientID  string `json:"client_id"`
			GrantType string `json:"grant_type"`
		}{
			ClientID: clientID, GrantType: "refresh_token",
		})
	}
	// 画像要求带该字段，取值必须显式给出且落在闭集内；缺失或非法一律失败关闭，
	// 不静默补默认值，避免调用点映射遗漏被掩盖。
	if !reason.valid() {
		return nil, fmt.Errorf("OAuth refresh 画像要求 refresh_reason，但取值缺失或不在闭集内：%q", string(reason))
	}
	return json.Marshal(struct {
		ClientID      string `json:"client_id"`
		GrantType     string `json:"grant_type"`
		RefreshReason string `json:"refresh_reason"`
	}{
		ClientID: clientID, GrantType: "refresh_token", RefreshReason: string(reason),
	})
}

// openAIOAuthProactiveRefreshReason 是“主动刷新”调用点的统一取值：我方后台刷新服务发现令牌进入
// 过期窗口、请求路径取令牌时发现临近过期，都对应官方“请求前发现 exp 距现在不足 5 分钟”。
//
// 之所以不按访问令牌 JWT 能否解析 exp 去区分 scheduled：我方 ChatGPT OAuth 访问令牌恒为带 exp 的
// JWT，官方只有“exp 不可解析且 8 天未刷新”才会得到 scheduled，在我方不会出现；凭空造出官方
// 默认链路上不会有的取值反而是偏差。
func openAIOAuthProactiveRefreshReason() openAIOAuthRefreshReason {
	return openAIOAuthRefreshReasonProactiveExpiry
}

// openAIOAuthRefreshReasonObserver 仅供测试：每次真正进入 OAuth 刷新出站前被调用一次，
// 让测试在旧画像（线上字节里没有 refresh_reason）下也能核对各调用点的原因映射。
// 生产代码不得设置；替换它的测试不得并行执行。
var openAIOAuthRefreshReasonObserver func(reason openAIOAuthRefreshReason)
