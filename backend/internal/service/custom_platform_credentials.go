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

// customBulkAccountPlan 是批量更新里单个 custom 账号的归一结果。
type customBulkAccountPlan struct {
	// baseURL 是归一后的 base_url（即合并后凭据里的 Chat Completions 地址）。
	baseURL string
	// identityChanged 表示归一后的 base_url 与账号当前保存的 base_url 不同，
	// 也就是上游身份确实变了，旧的上游计费探测快照随之失效。
	identityChanged bool
}

// customBulkPlan 是「账号 ID → 该账号的归一结果」，只包含 custom 账号。
type customBulkPlan map[int64]customBulkAccountPlan

// planCustomBulkBaseURLs 为批量更新里的 custom 账号规划 base_url 归一：按账号现有凭据与
// 本次增量合并出完整凭据，逐个校验并取归一后的 base_url，同时记录它与账号当前 base_url
// 是否不同。返回值只包含 custom 账号；其它平台账号不出现、行为不变。任一 custom 账号校验
// 失败即返回错误，调用方应在任何写操作之前整体拒绝。patch 为空（本次不改凭据）时无需校验，
// 返回空规划。
func planCustomBulkBaseURLs(targets []*Account, accountIDs []int64, patch map[string]any) (customBulkPlan, error) {
	plan := make(customBulkPlan)
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
		currentBaseURL, _ := account.Credentials[credentialKeyBaseURL].(string)
		plan[account.ID] = customBulkAccountPlan{
			baseURL:         baseURL,
			identityChanged: strings.TrimSpace(currentBaseURL) != strings.TrimSpace(baseURL),
		}
	}
	return plan, nil
}

// splitBulkAccountIDsByCustomBaseURL 把批量更新的目标账号拆成互不相交的几组：
// 非 custom 账号一组（沿用原增量，行为不变），custom 账号按（归一后的 base_url，上游身份是否
// 变化）各成一组（增量里追加 base_url）。按 base_url 再按「未变化在前」排序保证调用顺序稳定。
func splitBulkAccountIDsByCustomBaseURL(accountIDs []int64, plan customBulkPlan) (others []int64, customGroups []customBulkGroup) {
	type groupKey struct {
		baseURL         string
		identityChanged bool
	}
	byKey := make(map[groupKey][]int64)
	for _, id := range accountIDs {
		accountPlan, ok := plan[id]
		if !ok {
			others = append(others, id)
			continue
		}
		key := groupKey(accountPlan)
		byKey[key] = append(byKey[key], id)
	}
	keys := make([]groupKey, 0, len(byKey))
	for key := range byKey {
		keys = append(keys, key)
	}
	sort.Slice(keys, func(i, j int) bool {
		if keys[i].baseURL != keys[j].baseURL {
			return keys[i].baseURL < keys[j].baseURL
		}
		return !keys[i].identityChanged && keys[j].identityChanged
	})
	for _, key := range keys {
		customGroups = append(customGroups, customBulkGroup{baseURL: key.baseURL, identityChanged: key.identityChanged, ids: byKey[key]})
	}
	return others, customGroups
}

// customBulkGroup 是批量更新里 base_url 归一结果与上游身份变化情况都相同的一组 custom 账号。
type customBulkGroup struct {
	baseURL         string
	identityChanged bool
	ids             []int64
}

// bulkUpdateAccountColumns 执行批量更新的列与 JSONB 写入。没有 custom 账号需要归一时
// 与原来完全一致（一次调用）；否则非 custom 账号沿用原增量一次写入，custom 账号按归一后的
// base_url 与上游身份是否变化分组，每组的凭据增量追加 base_url。
//
// 上游计费探测快照的失效口径与原逻辑一致：增量本身带上游身份字段（api_key、base_url、
// 请求头覆写）或改了代理时，调用方已在增量 Extra 里置空快照，这里原样沿用；增量没带这些字段，
// 只是 api_base_urls 变动时，仅当某组账号归一后的 base_url 确实与当前不同才置空，
// 只补充 Responses、Anthropic 地址这类不影响身份的改动不清快照。
func (s *adminServiceImpl) bulkUpdateAccountColumns(ctx context.Context, accountIDs []int64, updates AccountBulkUpdate, customPlan customBulkPlan) error {
	if len(customPlan) == 0 {
		_, err := s.accountRepo.BulkUpdate(ctx, accountIDs, updates)
		return err
	}
	others, customGroups := splitBulkAccountIDsByCustomBaseURL(accountIDs, customPlan)
	if len(others) > 0 {
		if _, err := s.accountRepo.BulkUpdate(ctx, others, updates); err != nil {
			return err
		}
	}
	for _, group := range customGroups {
		groupUpdates := updates
		groupUpdates.Credentials = maps.Clone(updates.Credentials)
		groupUpdates.Credentials[credentialKeyBaseURL] = group.baseURL
		if group.identityChanged {
			groupUpdates.Extra = maps.Clone(updates.Extra)
			if groupUpdates.Extra == nil {
				groupUpdates.Extra = make(map[string]any, 1)
			}
			groupUpdates.Extra[UpstreamBillingProbeExtraKey] = nil
		}
		if _, err := s.accountRepo.BulkUpdate(ctx, group.ids, groupUpdates); err != nil {
			return err
		}
	}
	return nil
}
