package service

// codex_third_party_composite_base.go 负责 Composite 分组的在线目录基础目录：
//
//   - 只从可调度的官方 ChatGPT 账号拉取（判定与目录预算一致）。中转 API Key 账号的 /models 往往是
//     几千个各家模型的普通列表，不可能在客户端 5 秒预算内转换完，一律不拿来拉目录；
//   - 没有可调度的官方账号，或官方账号在预算内拉取失败，就按分组内 OpenAI 账号的模型映射在本地生成，
//     复用纯 OpenAI 分组的现成辅助函数，使用内置官方元数据，不发任何上游请求；
//   - 本地也生成不出条目时得到空目录，再由现有第三方策略叠加已开启的条目（等价于「空基础加第三方」）。
//
// 拉取到的和本地生成的基础目录之后都走同一套第三方策略（原生判定、移除未开启的第三方、叠加已开启的条目）。

import (
	"context"
	"fmt"
	"log/slog"
	"sort"
)

// codexCompositeMaxOfficialAttempts 是一次请求最多尝试的官方账号数（总耗时仍受基础目录预算约束）。
const codexCompositeMaxOfficialAttempts = 3

// CodexCompositeBaseFetcher 用一个官方账号拉取并整理基础目录，步骤与调度分支的成功路径一致。
// 由 handler 注入，因为它需要 OpenAI 网关服务和请求上下文。
type CodexCompositeBaseFetcher func(ctx context.Context, account *Account) (*OpenAIModelsResponse, error)

// BuildCompositeBaseManifest 返回 Composite 分组的基础目录和实际使用的官方账号（本地生成时为 nil）。
// reqCtx 是请求上下文，用于数据库查询与本地生成；fetchCtx 带基础目录获取的整次请求时限，只用于官方账号拉取。
// 返回错误只会在数据库查询失败时出现；官方账号拉取失败一律改为本地生成。
func (s *CodexThirdPartyCatalogService) BuildCompositeBaseManifest(
	reqCtx, fetchCtx context.Context,
	group *Group,
	fetch CodexCompositeBaseFetcher,
) (*OpenAIModelsResponse, *Account, error) {
	if s == nil || group == nil || s.accountRepo == nil {
		return nil, nil, fmt.Errorf("codex third party: composite base is unavailable")
	}
	officials, err := s.schedulableOfficialAccounts(reqCtx, group)
	if err != nil {
		// 账号列表读不出来：不能确认有没有官方账号，退回本地生成，让目录继续可用。
		slog.Warn("codex_third_party_composite_official_list_failed", "group_id", group.ID, "reason", err.Error())
	}
	if fetch != nil {
		for i := range officials {
			if i >= codexCompositeMaxOfficialAttempts || fetchCtx.Err() != nil {
				break
			}
			account := officials[i]
			manifest, fetchErr := fetch(fetchCtx, &account)
			if fetchErr == nil && manifest != nil && len(manifest.Body) > 0 {
				return manifest, &account, nil
			}
			reason := "empty manifest"
			if fetchErr != nil {
				reason = fetchErr.Error()
			}
			slog.Warn("codex_third_party_composite_official_base_failed",
				"group_id", group.ID, "account_id", account.ID, "reason", reason)
		}
	}
	if err := reqCtx.Err(); err != nil {
		return nil, nil, err
	}
	body, err := s.buildCompositeLocalBase(reqCtx, group)
	if err != nil {
		return nil, nil, err
	}
	return &OpenAIModelsResponse{Body: body, ETag: codexModelsManifestBodyETag(body)}, nil, nil
}

// schedulableOfficialAccounts 返回分组内当前可调度的官方 OpenAI 账号，按优先级和 ID 排序。
// 可调度判断完全交给账号仓库的调度查询（停用、暂停调度、临时不可调度的账号都不会出现），
// 这里不改变任何账号状态，也不会绕过它去使用暂停调度的账号。
func (s *CodexThirdPartyCatalogService) schedulableOfficialAccounts(ctx context.Context, group *Group) ([]Account, error) {
	accounts, err := s.accountRepo.ListSchedulableByGroupID(ctx, group.ID)
	if err != nil {
		return nil, err
	}
	officials := make([]Account, 0, len(accounts))
	for i := range accounts {
		if accounts[i].Platform == PlatformOpenAI && isCodexThirdPartyOfficialSourceAccount(&accounts[i]) {
			officials = append(officials, accounts[i])
		}
	}
	sort.SliceStable(officials, func(i, j int) bool {
		if officials[i].Priority != officials[j].Priority {
			return officials[i].Priority < officials[j].Priority
		}
		return officials[i].ID < officials[j].ID
	})
	return officials, nil
}

// buildCompositeLocalBase 按分组内 OpenAI 账号的模型映射本地生成基础目录（只查数据库）。
// 与纯 OpenAI 分组的本地生成（BuildGroupConfiguredCodexModelsManifest）使用同一组辅助函数：
// 展示名单遵守「隐藏已映射模型」，条目取内置官方元数据，最后按分组白名单过滤；
// codex-auto-* 只有在白名单里显式列出时才出现。其它平台的账号不参与，第三方条目稍后由第三方策略处理。
func (s *CodexThirdPartyCatalogService) buildCompositeLocalBase(ctx context.Context, group *Group) ([]byte, error) {
	visible, catalog, err := loadCodexGroupCatalogAccounts(ctx, s.accountRepo, group.ID)
	if err != nil {
		return nil, fmt.Errorf("load composite catalog accounts: %w", err)
	}
	visible = openAIPlatformAccounts(visible)
	catalog = openAIPlatformAccounts(catalog)
	models := openAIConfiguredCodexModelIDsForGroup(visible, group)
	if len(models) == 0 {
		return []byte(`{"models":[]}`), nil
	}
	body, err := buildCodexModelsManifestForAccounts(PlatformOpenAI, models, catalog, group, nil, true)
	if err != nil {
		return nil, fmt.Errorf("build composite local codex catalog: %w", err)
	}
	body, _, err = mergeConfiguredCodexModelsManifest(body, nil, group.ModelAllowlist.Models, group.ModelAllowlistEnabled())
	if err != nil {
		return nil, fmt.Errorf("filter composite local codex catalog: %w", err)
	}
	return body, nil
}

func openAIPlatformAccounts(accounts []Account) []Account {
	out := make([]Account, 0, len(accounts))
	for i := range accounts {
		if accounts[i].Platform == PlatformOpenAI {
			out = append(out, accounts[i])
		}
	}
	return out
}
