package service

import (
	"context"
	"maps"
	"sort"
	"strings"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
)

// 自定义多协议平台（custom）的凭据校验与归一，覆盖创建、复制、单个更新、批量更新与导入
// 四条管理路径。平台判定与其它小钩子见 custom_platform.go。

// NormalizeCustomPlatformCredentials 校验并就地归一 custom 平台账号的「完整凭据」：
//   - 账号类型必须是 API Key；
//   - api_protocol 必须是 adaptive；
//   - api_base_urls.chat_completions 必须非空（缺地址时 GetOpenAIBaseURL 会回退到
//     OpenAI 官方地址，请求会发错上游，所以必须在写库前拒绝）；
//   - 把 base_url 归一为 api_base_urls.chat_completions（Ollama 用量识别只读旧字段
//     base_url，两个字段必须一致）。
//
// credentials 必须是「该账号合并后的完整凭据」，不能只传本次提交的增量。非 custom
// 平台直接返回 nil，不做任何改动。
func NormalizeCustomPlatformCredentials(platform, accountType string, credentials map[string]any) error {
	if platform != PlatformCustom {
		return nil
	}
	if accountType != AccountTypeAPIKey {
		return infraerrors.BadRequest(
			"CUSTOM_PLATFORM_ACCOUNT_TYPE_INVALID",
			"custom platform accounts only support apikey type",
		)
	}
	if protocol, _ := credentials[credentialKeyAPIProtocol].(string); strings.TrimSpace(protocol) != APIProtocolAdaptive {
		return infraerrors.BadRequest(
			"CUSTOM_PLATFORM_API_PROTOCOL_INVALID",
			"custom platform accounts require credentials.api_protocol to be adaptive",
		)
	}
	chatURL := credentialAPIBaseURL(credentials, APIProtocolChatCompletions)
	if chatURL == "" {
		return infraerrors.BadRequest(
			"CUSTOM_PLATFORM_CHAT_COMPLETIONS_URL_REQUIRED",
			"custom platform accounts require credentials.api_base_urls.chat_completions",
		)
	}
	credentials[credentialKeyBaseURL] = chatURL
	return nil
}

// mergeCredentialsTopLevel 模拟批量更新写库时的 JSONB 顶层键合并（existing || patch）：
// patch 里出现的键整体覆盖 existing 的同名键，不做深合并。返回新 map，不改入参。
func mergeCredentialsTopLevel(existing, patch map[string]any) map[string]any {
	merged := make(map[string]any, len(existing)+len(patch))
	for key, value := range existing {
		merged[key] = value
	}
	for key, value := range patch {
		merged[key] = value
	}
	return merged
}

// planCustomBulkBaseURLs 为批量更新里的 custom 账号规划 base_url 归一：按账号现有凭据与
// 本次增量合并出完整凭据，逐个校验并取归一后的 base_url。返回「账号 ID → 归一后 base_url」，
// 只包含 custom 账号；其它平台账号不出现、行为不变。任一 custom 账号校验失败即返回错误，
// 调用方应在任何写操作之前整体拒绝。patch 为空（本次不改凭据）时无需校验，返回空 map。
func planCustomBulkBaseURLs(targets []*Account, accountIDs []int64, patch map[string]any) (map[int64]string, error) {
	plan := make(map[int64]string)
	if len(patch) == 0 {
		return plan, nil
	}
	selected := make(map[int64]struct{}, len(accountIDs))
	for _, id := range accountIDs {
		selected[id] = struct{}{}
	}
	for _, account := range targets {
		if account == nil || !account.IsCustom() {
			continue
		}
		if _, ok := selected[account.ID]; !ok {
			continue
		}
		merged := mergeCredentialsTopLevel(account.Credentials, patch)
		if err := NormalizeCustomPlatformCredentials(account.Platform, account.Type, merged); err != nil {
			return nil, err
		}
		baseURL, _ := merged[credentialKeyBaseURL].(string)
		plan[account.ID] = baseURL
	}
	return plan, nil
}

// splitBulkAccountIDsByCustomBaseURL 把批量更新的目标账号拆成互不相交的几组：
// 非 custom 账号一组（沿用原增量，行为不变），custom 账号按归一后的 base_url 各成一组
// （增量里追加 base_url）。按 base_url 排序保证调用顺序稳定。
func splitBulkAccountIDsByCustomBaseURL(accountIDs []int64, plan map[int64]string) (others []int64, customGroups []customBulkGroup) {
	byURL := make(map[string][]int64)
	for _, id := range accountIDs {
		baseURL, ok := plan[id]
		if !ok {
			others = append(others, id)
			continue
		}
		byURL[baseURL] = append(byURL[baseURL], id)
	}
	urls := make([]string, 0, len(byURL))
	for baseURL := range byURL {
		urls = append(urls, baseURL)
	}
	sort.Strings(urls)
	for _, baseURL := range urls {
		customGroups = append(customGroups, customBulkGroup{baseURL: baseURL, ids: byURL[baseURL]})
	}
	return others, customGroups
}

// customBulkGroup 是批量更新里 base_url 归一结果相同的一组 custom 账号。
type customBulkGroup struct {
	baseURL string
	ids     []int64
}

// bulkUpdateAccountColumns 执行批量更新的列与 JSONB 写入。没有 custom 账号需要归一时
// 与原来完全一致（一次调用）；否则非 custom 账号沿用原增量一次写入，custom 账号按归一后的
// base_url 分组，每组的凭据增量追加 base_url（并按身份变化重置上游计费探测快照）。
func (s *adminServiceImpl) bulkUpdateAccountColumns(ctx context.Context, accountIDs []int64, updates AccountBulkUpdate, customBaseURLs map[int64]string) error {
	if len(customBaseURLs) == 0 {
		_, err := s.accountRepo.BulkUpdate(ctx, accountIDs, updates)
		return err
	}
	others, customGroups := splitBulkAccountIDsByCustomBaseURL(accountIDs, customBaseURLs)
	if len(others) > 0 {
		if _, err := s.accountRepo.BulkUpdate(ctx, others, updates); err != nil {
			return err
		}
	}
	for _, group := range customGroups {
		groupUpdates := updates
		groupUpdates.Credentials = maps.Clone(updates.Credentials)
		groupUpdates.Credentials[credentialKeyBaseURL] = group.baseURL
		// base_url 属于上游身份：本组归一后的 base_url 即使不在原增量里，也要让旧的
		// 上游计费探测快照失效，与原增量直接带 base_url 时的处理一致。
		groupUpdates.Extra = maps.Clone(updates.Extra)
		if groupUpdates.Extra == nil {
			groupUpdates.Extra = make(map[string]any, 1)
		}
		groupUpdates.Extra[UpstreamBillingProbeExtraKey] = nil
		if _, err := s.accountRepo.BulkUpdate(ctx, group.ids, groupUpdates); err != nil {
			return err
		}
	}
	return nil
}
