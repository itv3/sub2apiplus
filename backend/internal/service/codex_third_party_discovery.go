package service

// codex_third_party_discovery.go 负责候选名称发现与输入指纹。
//
// 候选名称 = 所选分组「默认启用的模型」，即该分组的 Key 走 OpenAI 协议调用普通模型列表时返回的名单
// （只含可调度账号、遵守分组白名单、Composite 按协议过滤，并遵守「隐藏已映射模型」规则）。
// 名单由网关 handler 层注入（见 SetGroupModelLister），评估器与运行时目录共用同一份，
// 这里不再自己对账号映射、Composite 路由、源快照和基础目录求并集：
// 中转站点同步来的上千个上游型号不属于分组默认启用的模型，不能进入候选。
// 发现只读：不触发上游同步，不写库。

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"
)

const (
	codexSourceMapping     = "mapping"
	codexSourceAlias       = "alias"
	codexSourceGroupModels = "group_models"
	codexSourceBaseCatalog = "base_catalog"
)

// CodexThirdPartyGroupModelLister 返回分组默认启用的模型名单，等价于该分组 Key 调用普通模型列表接口的结果。
// 实现必须与普通模型列表使用同一套生成逻辑，不得另写一份。
type CodexThirdPartyGroupModelLister func(ctx context.Context, group *Group) ([]string, error)

// ErrCodexThirdPartyGroupModelsUnavailable 表示分组默认模型名单不可得（未注入或生成失败）。
var ErrCodexThirdPartyGroupModelsUnavailable = errors.New("codex third party: group default model list unavailable")

// SetGroupModelLister 注入分组默认模型名单生成函数，由网关 handler 装配调用。
func (s *CodexThirdPartyCatalogService) SetGroupModelLister(lister CodexThirdPartyGroupModelLister) {
	if s != nil {
		s.groupModels = lister
	}
}

// groupModelNames 返回会话内缓存的分组默认模型名单（去空白、去重、保持原顺序）。
// 失败时返回错误，调用方据此降级或让评估失败，不得当作「名单为空」继续。
func (sess *codexTPSession) groupModelNames(ctx context.Context) ([]string, map[string]struct{}, error) {
	if sess.listingLoaded {
		return sess.listingNames, sess.listingSet, sess.listingErr
	}
	sess.listingLoaded = true
	lister := sess.svc.groupModels
	if lister == nil {
		sess.listingErr = fmt.Errorf("%w: lister is not configured", ErrCodexThirdPartyGroupModelsUnavailable)
		return nil, nil, sess.listingErr
	}
	raw, err := lister(ctx, sess.group)
	if err != nil {
		sess.listingErr = fmt.Errorf("%w: %v", ErrCodexThirdPartyGroupModelsUnavailable, err)
		return nil, nil, sess.listingErr
	}
	sess.listingSet = make(map[string]struct{}, len(raw))
	for _, name := range raw {
		name = strings.TrimSpace(name)
		if name == "" {
			continue
		}
		if _, dup := sess.listingSet[name]; dup {
			continue
		}
		sess.listingSet[name] = struct{}{}
		sess.listingNames = append(sess.listingNames, name)
	}
	return sess.listingNames, sess.listingSet, nil
}

// discoverNames 返回候选名称到来源列表的映射：名称取自分组默认模型名单，
// 来源只用于向管理员说明名称出处（分组名单一定有；账号映射、别名、基础目录为附加说明）。
// 通配符不作为型号；专用图片、视频与非对话模型不进入候选。
func (sess *codexTPSession) discoverNames(ctx context.Context, baseSlugs map[string]struct{}) (map[string][]string, error) {
	names, _, err := sess.groupModelNames(ctx)
	if err != nil {
		return nil, err
	}
	// 附加出处：账号的具体映射（别名或同名映射）。仅用于说明，不扩大候选范围。
	mapped := map[string]string{}
	for i := range sess.accounts {
		for public, target := range AdvertisedModelMappingForAccount(&sess.accounts[i]) {
			public = strings.TrimSpace(public)
			if public == strings.TrimSpace(target) {
				if _, exists := mapped[public]; !exists {
					mapped[public] = codexSourceMapping
				}
			} else {
				mapped[public] = codexSourceAlias
			}
		}
	}
	out := make(map[string][]string, len(names))
	for _, name := range names {
		if strings.Contains(name, "*") || isCodexDedicatedMediaModel(name) || isCodexNonChatModelName(name) {
			continue
		}
		sources := []string{codexSourceGroupModels}
		if source, ok := mapped[name]; ok {
			sources = append(sources, source)
		}
		if _, ok := baseSlugs[name]; ok {
			sources = append(sources, codexSourceBaseCatalog)
		}
		sort.Strings(sources)
		out[name] = sources
	}
	return out, nil
}

// inputFingerprint 计算除管理员开关以外全部评估输入的确定性摘要：
// 分组与白名单、承接账号端点身份、各基础目录内容、候选集合（即分组默认模型名单）与其静态结论、能力档案摘要。
// 不含时间、配置 revision、启用与验证状态，因此相同输入必然得到相同指纹。
func (s *CodexThirdPartyCatalogService) inputFingerprint(group *Group, sess *codexTPSession, bases []codexTPBase, candidates []CodexThirdPartyCandidate) string {
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
		Group      int64            `json:"group"`
		Platform   string           `json:"platform"`
		Allowlist  []string         `json:"allowlist"`
		Restricted bool             `json:"restricted"`
		Profiles   string           `json:"profiles"`
		Accounts   []accountInput   `json:"accounts"`
		Bases      []baseInput      `json:"bases"`
		Candidates []candidateInput `json:"candidates"`
	}{
		Group:      group.ID,
		Platform:   group.Platform,
		Allowlist:  append([]string(nil), group.ModelAllowlist.Models...),
		Restricted: group.ModelAllowlist.Enabled,
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
