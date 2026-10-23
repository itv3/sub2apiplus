package service

// codex_third_party_discovery.go 负责候选名称发现与输入指纹。
//
// 名称取并集：账号具体映射与别名、Composite 精确路由、源快照（只用 Matches(account) 为真的快照）、
// 基础目录里已有的条目。通配符只在已知 ID 上展开，本身不作为型号。
// 发现只读：不触发上游同步，不写库。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"sort"
	"strings"
)

const (
	codexSourceMapping        = "mapping"
	codexSourceAlias          = "alias"
	codexSourceCompositeRoute = "composite_route"
	codexSourceUpstreamSync   = "upstream_sync"
	codexSourceBaseCatalog    = "base_catalog"
)

// codexTPSourceNote 记录发现过程中值得进入输入指纹的来源状态（例如某账号快照读取失败或指纹不一致）。
type codexTPSourceNote struct {
	AccountID int64  `json:"account_id"`
	State     string `json:"state"`
}

// discoverNames 返回候选名称到来源列表的映射。
func (sess *codexTPSession) discoverNames(ctx context.Context, baseSlugs map[string]struct{}) (map[string][]string, []codexTPSourceNote, error) {
	found := map[string]map[string]struct{}{}
	add := func(name, source string) {
		name = strings.TrimSpace(name)
		if name == "" || strings.Contains(name, "*") {
			return
		}
		if isCodexDedicatedMediaModel(name) || isCodexNonChatModelName(name) {
			return
		}
		if found[name] == nil {
			found[name] = map[string]struct{}{}
		}
		found[name][source] = struct{}{}
	}

	var notes []codexTPSourceNote
	for i := range sess.accounts {
		account := &sess.accounts[i]
		for public, target := range AdvertisedModelMappingForAccount(account) {
			if strings.TrimSpace(public) == strings.TrimSpace(target) {
				add(public, codexSourceMapping)
			} else {
				add(public, codexSourceAlias)
			}
		}
		snapshot, ok, err := sess.svc.settings.LoadCodexCatalogSource(ctx, account.ID)
		switch {
		case err != nil:
			notes = append(notes, codexTPSourceNote{AccountID: account.ID, State: "error"})
			slog.Warn("codex_third_party_source_snapshot_unreadable", "account_id", account.ID, "stage", "discover", "reason", err.Error())
		case !ok:
			notes = append(notes, codexTPSourceNote{AccountID: account.ID, State: "missing"})
		case !snapshot.Matches(account):
			// 源配置已变化的旧快照不得参与发现。
			notes = append(notes, codexTPSourceNote{AccountID: account.ID, State: "mismatch"})
		default:
			notes = append(notes, codexTPSourceNote{AccountID: account.ID, State: string(snapshot.Status) + "@" + snapshot.SyncedAt.UTC().Format("2006-01-02T15:04:05Z")})
			for _, model := range snapshot.Models {
				add(model, codexSourceUpstreamSync)
			}
		}
	}

	if sess.group.Platform == PlatformComposite && sess.svc.resolver != nil {
		models, err := sess.svc.resolver.ListExactPublicModels(ctx, sess.group.ID, CompositeRouteEndpointResponses, false)
		if err != nil {
			return nil, nil, fmt.Errorf("list composite exact routes: %w", err)
		}
		for _, model := range models {
			add(model, codexSourceCompositeRoute)
		}
	}
	for slug := range baseSlugs {
		// 基础目录里已有的条目一律纳入，包括可能被判为专用媒体的名称由后续过滤处理。
		name := strings.TrimSpace(slug)
		if name == "" {
			continue
		}
		if found[name] == nil {
			found[name] = map[string]struct{}{}
		}
		found[name][codexSourceBaseCatalog] = struct{}{}
	}

	out := make(map[string][]string, len(found))
	for name, set := range found {
		sources := make([]string, 0, len(set))
		for source := range set {
			sources = append(sources, source)
		}
		sort.Strings(sources)
		out[name] = sources
	}
	sort.Slice(notes, func(i, j int) bool { return notes[i].AccountID < notes[j].AccountID })
	return out, notes, nil
}

// inputFingerprint 计算除管理员开关以外全部评估输入的确定性摘要：
// 分组与白名单、承接账号端点身份、各基础目录内容、候选集合与其静态结论、能力档案摘要、源快照状态。
// 不含时间、配置 revision、启用与验证状态，因此相同输入必然得到相同指纹。
func (s *CodexThirdPartyCatalogService) inputFingerprint(group *Group, sess *codexTPSession, bases []codexTPBase, candidates []CodexThirdPartyCandidate, notes []codexTPSourceNote) string {
	type accountInput struct {
		ID       int64  `json:"id"`
		Platform string `json:"platform"`
		Endpoint string `json:"endpoint"`
	}
	type baseInput struct {
		AccountID int64  `json:"account_id"`
		Digest    string `json:"digest"`
		Failed    bool   `json:"failed"`
	}
	type candidateInput struct {
		Slug        string   `json:"slug"`
		Native      bool     `json:"native"`
		StaticState string   `json:"static_state"`
		Sources     []string `json:"sources"`
		Accounts    []int64  `json:"accounts"`
		Fingerprint string   `json:"fingerprint"`
		Reasons     []string `json:"reasons"`
	}
	input := struct {
		Group      int64               `json:"group"`
		Platform   string              `json:"platform"`
		Allowlist  []string            `json:"allowlist"`
		Restricted bool                `json:"restricted"`
		Profiles   string              `json:"profiles"`
		Accounts   []accountInput      `json:"accounts"`
		Bases      []baseInput         `json:"bases"`
		Candidates []candidateInput    `json:"candidates"`
		Sources    []codexTPSourceNote `json:"sources"`
	}{
		Group:      group.ID,
		Platform:   group.Platform,
		Allowlist:  append([]string(nil), group.ModelAllowlist.Models...),
		Restricted: group.ModelAllowlist.Enabled,
		Sources:    notes,
	}
	if s.profiles != nil {
		input.Profiles = s.profiles.Digest
	}
	sort.Strings(input.Allowlist)
	for i := range sess.accounts {
		account := &sess.accounts[i]
		input.Accounts = append(input.Accounts, accountInput{
			ID:       account.ID,
			Platform: account.Platform,
			Endpoint: codexTPEndpointDigest([]codexTPAccountRoute{{Account: account}}),
		})
	}
	sort.Slice(input.Accounts, func(i, j int) bool { return input.Accounts[i].ID < input.Accounts[j].ID })
	for _, base := range bases {
		item := baseInput{AccountID: base.accountID, Failed: base.err != nil}
		if len(base.body) > 0 {
			sum := sha256.Sum256(base.body)
			item.Digest = hex.EncodeToString(sum[:])
		}
		input.Bases = append(input.Bases, item)
	}
	sort.Slice(input.Bases, func(i, j int) bool { return input.Bases[i].AccountID < input.Bases[j].AccountID })
	for _, c := range candidates {
		state := string(c.Status)
		// 启用后才产生的体积结论与验证结论依赖开关，不属于开关以外的输入。
		if c.Status == CodexModelStatusOverBudget || c.Status == CodexModelStatusVerified {
			state = string(CodexModelStatusConfigurable)
		}
		input.Candidates = append(input.Candidates, candidateInput{
			Slug: c.Slug, Native: c.Native, StaticState: state, Sources: c.Sources,
			Accounts: c.AccountIDs, Fingerprint: c.Fingerprint, Reasons: staticCodexTPReasons(c.Reasons),
		})
	}
	sort.Slice(input.Candidates, func(i, j int) bool { return input.Candidates[i].Slug < input.Candidates[j].Slug })
	return codexTPHashJSON(input)
}

// staticCodexTPReasons 去掉只在启用后才追加的体积原因，保证指纹与开关无关。
func staticCodexTPReasons(reasons []string) []string {
	out := make([]string, 0, len(reasons))
	for _, reason := range reasons {
		if strings.Contains(reason, "最坏目录体积") {
			continue
		}
		out = append(out, reason)
	}
	return out
}

// codexTPHashJSON 返回值的 JSON 序列化的 sha256（map 键由标准库排序，输出确定）。
func codexTPHashJSON(value any) string {
	raw, err := json.Marshal(value)
	if err != nil {
		return ""
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
