package service

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// 工作区路由发现（画像 WorkspaceRouting 节，首期）。
//
// 官方客户端在启动与 token 刷新后用 backend client 发出 GET /wham/accounts/check，按当前
// ChatGPT 工作区条目的 workspace_backend_origin 与 account_routing_override 决定 Responses
// 的业务 origin 与 override 头。网关首期只做三件事：
//  1. 经 WHAM 配额同一出口（Sink codex.quota.wham）发出画像声明的发现请求；
//  2. 记录返回值（日志）并缓存判定；
//  3. 判定为非默认路由时，画像 RoutedEndpointIDs 中的端点按 NonDefaultAction（fail_closed）
//     失败关闭，不把请求发往默认 origin。
//
// 画像没有 WorkspaceRouting 节时，发现请求不发出、闸门恒放行，旧版本画像行为不变。

// officialCodexWorkspaceRoutingResult 是一次发现的判定结果。
type officialCodexWorkspaceRoutingResult struct {
	ChatGPTAccountID string
	BackendOrigin    string
	RoutingOverride  string
	// Default 为 true 表示 origin 与 override 都落在画像接受的默认取值内。
	Default bool
	// Reason 在非默认或响应不合法时说明原因，只用于日志与错误信息。
	Reason       string
	DiscoveredAt time.Time
}

// ErrOfficialCodexWorkspaceRoutingNonDefault 表示工作区路由判定为非默认，受路由端点失败关闭。
var ErrOfficialCodexWorkspaceRoutingNonDefault = errors.New("Codex 工作区路由为非默认结果，按画像失败关闭")

type officialCodexAccountsCheckEntry struct {
	ID                     string  `json:"id"`
	WorkspaceBackendOrigin *string `json:"workspace_backend_origin"`
	AccountRoutingOverride *string `json:"account_routing_override"`
}

// decodeOfficialCodexAccountsCheck 解析 accounts/check 响应的条目。官方响应有两种形态：
// 列表（Codex backend，携带路由字段）与按 ID 索引的映射（ChatGPT 形态，不携带路由字段）。
func decodeOfficialCodexAccountsCheck(body []byte) ([]officialCodexAccountsCheckEntry, error) {
	var envelope struct {
		Accounts json.RawMessage `json:"accounts"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		return nil, fmt.Errorf("accounts/check 响应不是合法 JSON：%w", err)
	}
	raw := strings.TrimSpace(string(envelope.Accounts))
	switch {
	case raw == "" || raw == "null":
		return nil, nil
	case strings.HasPrefix(raw, "["):
		var entries []officialCodexAccountsCheckEntry
		if err := json.Unmarshal(envelope.Accounts, &entries); err != nil {
			return nil, fmt.Errorf("accounts/check 列表条目非法：%w", err)
		}
		return entries, nil
	case strings.HasPrefix(raw, "{"):
		var indexed map[string]struct {
			Account struct {
				AccountID string `json:"account_id"`
			} `json:"account"`
		}
		if err := json.Unmarshal(envelope.Accounts, &indexed); err != nil {
			return nil, fmt.Errorf("accounts/check 映射条目非法：%w", err)
		}
		entries := make([]officialCodexAccountsCheckEntry, 0, len(indexed))
		for _, item := range indexed {
			entries = append(entries, officialCodexAccountsCheckEntry{ID: item.Account.AccountID})
		}
		return entries, nil
	default:
		return nil, errors.New("accounts/check 的 accounts 形态非法")
	}
}

// decideOfficialCodexWorkspaceRouting 按画像节判定当前工作区的路由。
//
// 与官方客户端一致：找不到或重复出现当前工作区条目、缺少 workspace_backend_origin 或
// account_routing_override 都视为发现失败，判定为非默认（失败关闭）；两个字段都落在
// 画像接受的取值内才是默认路由。
func decideOfficialCodexWorkspaceRouting(
	section *profilecontract.WorkspaceRoutingSection,
	chatGPTAccountID string,
	body []byte,
) officialCodexWorkspaceRoutingResult {
	result := officialCodexWorkspaceRoutingResult{
		ChatGPTAccountID: strings.TrimSpace(chatGPTAccountID),
		DiscoveredAt:     time.Now(),
	}
	if section == nil {
		result.Default = true
		return result
	}
	entries, err := decodeOfficialCodexAccountsCheck(body)
	if err != nil {
		result.Reason = err.Error()
		return result
	}
	var matched []officialCodexAccountsCheckEntry
	for _, entry := range entries {
		if strings.TrimSpace(entry.ID) == result.ChatGPTAccountID {
			matched = append(matched, entry)
		}
	}
	switch {
	case result.ChatGPTAccountID == "":
		result.Reason = "缺少当前 chatgpt-account-id"
		return result
	case len(matched) == 0:
		result.Reason = "accounts/check 不含当前工作区"
		return result
	case len(matched) > 1:
		result.Reason = "accounts/check 重复出现当前工作区"
		return result
	}
	entry := matched[0]
	if entry.WorkspaceBackendOrigin == nil {
		result.Reason = "缺少 workspace_backend_origin"
		return result
	}
	if entry.AccountRoutingOverride == nil {
		result.Reason = "缺少 account_routing_override"
		return result
	}
	result.BackendOrigin = *entry.WorkspaceBackendOrigin
	result.RoutingOverride = *entry.AccountRoutingOverride
	originAccepted := slices.Contains(section.AcceptedOriginValues, result.BackendOrigin)
	overrideAccepted := slices.Contains(section.AcceptedOverrideValues, result.RoutingOverride)
	result.Default = originAccepted && overrideAccepted
	if !result.Default {
		result.Reason = "非默认工作区路由"
	}
	return result
}

// officialCodexWorkspaceRoutingResults 按 chatgpt-account-id 缓存最近一次发现判定。
// 路由属于 ChatGPT 工作区而不是本地账号；新的发现结果覆盖旧结果。
var officialCodexWorkspaceRoutingResults sync.Map

func recordOfficialCodexWorkspaceRouting(result officialCodexWorkspaceRoutingResult) {
	if strings.TrimSpace(result.ChatGPTAccountID) == "" {
		return
	}
	officialCodexWorkspaceRoutingResults.Store(result.ChatGPTAccountID, result)
}

func lookupOfficialCodexWorkspaceRouting(chatGPTAccountID string) (officialCodexWorkspaceRoutingResult, bool) {
	value, ok := officialCodexWorkspaceRoutingResults.Load(strings.TrimSpace(chatGPTAccountID))
	if !ok {
		return officialCodexWorkspaceRoutingResult{}, false
	}
	result, ok := value.(officialCodexWorkspaceRoutingResult)
	return result, ok
}

// officialCodexWorkspaceRoutingAccountKey 与配额链路解析 chatgpt-account-id 的规则一致：
// 优先 chatgpt_account_id，旧账号回退 organization_id。
func officialCodexWorkspaceRoutingAccountKey(account *Account) string {
	if account == nil {
		return ""
	}
	if key := strings.TrimSpace(account.GetCredential("chatgpt_account_id")); key != "" {
		return key
	}
	return strings.TrimSpace(account.GetCredential("organization_id"))
}

// officialCodexWorkspaceRoutingGate 是受路由端点的出站闸门：画像声明 WorkspaceRouting 节、
// 端点属于 RoutedEndpointIDs、且该工作区最近一次发现判定为非默认时失败关闭。尚未发现
// 的工作区放行（首期不在请求热路径上发起发现）。
func officialCodexWorkspaceRoutingGate(mode string, account *Account, endpointID string) error {
	if account == nil {
		return nil
	}
	section := officialCodexOptionalSectionsForMode(mode).WorkspaceRouting
	if section == nil || !slices.Contains(section.RoutedEndpointIDs, strings.TrimSpace(endpointID)) {
		return nil
	}
	result, found := lookupOfficialCodexWorkspaceRouting(officialCodexWorkspaceRoutingAccountKey(account))
	if !found || result.Default {
		return nil
	}
	if section.NonDefaultAction != "fail_closed" {
		return fmt.Errorf("Codex 工作区路由处置未受支持：%s", section.NonDefaultAction)
	}
	return fmt.Errorf("%w：account_id=%d endpoint=%s reason=%s",
		ErrOfficialCodexWorkspaceRoutingNonDefault, account.ID, endpointID, result.Reason)
}

// doOfficialCodexWorkspaceRoutingDiscovery 是发现请求的出口，默认复用 WHAM 配额统一出口
// （同一 Sink、同一 backend client 画像）。包级变量只为测试注入。
var doOfficialCodexWorkspaceRoutingDiscovery = func(
	s *OpenAIQuotaService,
	ctx context.Context,
	accountID int64,
	proxyURL string,
	endpointID codexEndpointID,
	headers http.Header,
) (int, []byte, error) {
	return s.doCodexQuotaRequest(ctx, accountID, proxyURL, endpointID, headers, nil)
}

// discoverOfficialCodexWorkspaceRouting 在画像声明 WorkspaceRouting 节时发出发现请求、
// 记录返回值并缓存判定。返回值 performed 表示是否发出了请求。传输失败或非 2xx 时不缓存
// 判定（下次发现重试），只记录日志。
func (s *OpenAIQuotaService) discoverOfficialCodexWorkspaceRouting(
	ctx context.Context,
	mode string,
	accountID int64,
	chatGPTAccountID string,
	proxyURL string,
	headers http.Header,
) (officialCodexWorkspaceRoutingResult, bool, error) {
	section := officialCodexOptionalSectionsForMode(mode).WorkspaceRouting
	if section == nil {
		return officialCodexWorkspaceRoutingResult{}, false, nil
	}
	status, body, err := doOfficialCodexWorkspaceRoutingDiscovery(
		s, ctx, accountID, proxyURL, codexEndpointID(section.DiscoveryEndpointID), headers,
	)
	if err != nil {
		slog.Warn("openai_workspace_routing_discovery_failed", "account_id", accountID, "error", err)
		return officialCodexWorkspaceRoutingResult{}, true, err
	}
	if status < http.StatusOK || status >= http.StatusMultipleChoices {
		slog.Warn("openai_workspace_routing_discovery_failed", "account_id", accountID, "status", status)
		return officialCodexWorkspaceRoutingResult{}, true, fmt.Errorf("accounts/check 返回 %d", status)
	}
	result := decideOfficialCodexWorkspaceRouting(section, chatGPTAccountID, body)
	recordOfficialCodexWorkspaceRouting(result)
	// 首期目的之一是观察生产账号的实际返回值：origin 与 override 都是路由枚举，不含凭据。
	slog.Info("openai_workspace_routing_discovered",
		"account_id", accountID,
		"workspace_backend_origin", result.BackendOrigin,
		"account_routing_override", result.RoutingOverride,
		"default", result.Default,
		"reason", result.Reason,
	)
	return result, true, nil
}
