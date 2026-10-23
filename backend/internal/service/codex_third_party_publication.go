package service

// codex_third_party_publication.go 把一个第三方路由结果变成「能否发布」的结论：
// 分组白名单 → 能力档案匹配 → 协议契约 → 指令模板校验 → 生成完整条目与指纹。
// 没有档案也没有可靠能力快照的型号状态为「待补能力」，绝不套用通用构建器的 272K、none、unified_exec 默认值。

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
)

// codexTPPublication 是第三方模型的静态发布结论（不含管理员开关）。
type codexTPPublication struct {
	Route   *codexTPRoute
	Profile *codexThirdPartyProfile
	// Status 是静态状态：needs_capability、configurable、conflict、unavailable。
	Status      CodexThirdPartyModelStatus
	Reasons     []string
	CanPublish  bool
	Entry       []byte
	Fingerprint string
	Summary     CodexThirdPartyCapabilitySummary
}

// publicationFor 计算第三方路由的静态发布结论。route 必须是非原生路由。
func (s *CodexThirdPartyCatalogService) publicationFor(group *Group, route *codexTPRoute) *codexTPPublication {
	pub := &codexTPPublication{Route: route, Reasons: append([]string(nil), route.Reasons...)}
	switch {
	case route.Conflict:
		pub.Status = CodexModelStatusConflict
		return pub
	case route.Unavailable:
		pub.Status = CodexModelStatusUnavailable
		return pub
	}
	if isCodexDedicatedMediaModel(route.Slug) || isCodexNonChatModelName(route.Slug) {
		pub.Status = CodexModelStatusUnavailable
		pub.Reasons = append(pub.Reasons, "专用图片、视频或向量模型不进入 Codex 目录")
		return pub
	}
	if group != nil && group.ModelAllowlistEnabled() && !group.ModelAllowlist.Allows(route.Slug) {
		pub.Status = CodexModelStatusUnavailable
		pub.Reasons = append(pub.Reasons, "分组模型白名单未包含该模型")
		return pub
	}
	if s.profiles == nil {
		pub.Status = CodexModelStatusNeedsCapability
		pub.Reasons = append(pub.Reasons, "能力档案加载失败，无法发布")
		return pub
	}
	if len(route.UpstreamModels) == 0 || len(route.Protocols) == 0 {
		pub.Status = CodexModelStatusNeedsCapability
		return pub
	}
	profile, reason := s.profiles.matchAll(route.UpstreamModels)
	if profile == nil {
		pub.Status = CodexModelStatusNeedsCapability
		pub.Reasons = append(pub.Reasons, reason)
		return pub
	}
	pub.Profile = profile
	if !profile.supportsProtocols(route.Protocols) {
		pub.Status = CodexModelStatusConflict
		pub.Reasons = append(pub.Reasons, fmt.Sprintf("能力档案 %s 不覆盖当前协议（%s）", profile.ID, strings.Join(route.Protocols, "、")))
		return pub
	}
	removeImage := codexTPSnapshotDeniesImage(route)
	entry, _, err := profile.buildEntry(route.Slug, removeImage)
	if err != nil {
		pub.Status = CodexModelStatusNeedsCapability
		pub.Reasons = append(pub.Reasons, err.Error())
		return pub
	}
	pub.Entry = entry
	pub.Summary = summarizeCodexThirdPartyEntry(entry)
	pub.Fingerprint = s.entryFingerprint(route, profile, removeImage)
	pub.Status = CodexModelStatusConfigurable
	pub.CanPublish = true
	if removeImage {
		pub.Reasons = append(pub.Reasons, "承接账号的能力快照明确不含图片输入，已从该条目移除图片能力")
	}
	return pub
}

// codexTPSnapshotDeniesImage 判断承接账号的能力快照是否明确声明了不含图片的输入模态。
// 站点能力限制优先于共享档案，且只可能让能力变少，不会放宽。
func codexTPSnapshotDeniesImage(route *codexTPRoute) bool {
	for _, item := range route.Accounts {
		metadata, ok := item.Account.GetUpstreamModelMetadata(item.Final)
		if !ok {
			continue
		}
		modalities := normalizeCodexInputModalities(metadata.InputModalities)
		if len(modalities) == 0 {
			continue
		}
		if !stringSliceContains(modalities, "image") {
			return true
		}
	}
	return false
}

// entryFingerprint 绑定客户端契约、公开名、目标平台、上游型号、协议、能力档案摘要与承接端点身份。
// 换站点（端点身份变化）、换档案或换协议，指纹必然变化。
func (s *CodexThirdPartyCatalogService) entryFingerprint(route *codexTPRoute, profile *codexThirdPartyProfile, removeImage bool) string {
	input := struct {
		Contract       string   `json:"contract"`
		Slug           string   `json:"slug"`
		TargetPlatform string   `json:"target_platform"`
		UpstreamModel  string   `json:"upstream_model"`
		Protocols      []string `json:"protocols"`
		ProfileID      string   `json:"profile_id"`
		ProfileDigest  string   `json:"profile_digest"`
		EndpointDigest string   `json:"endpoint_digest"`
		RemoveImage    bool     `json:"remove_image"`
	}{
		Contract:       s.profiles.ClientContract,
		Slug:           route.Slug,
		TargetPlatform: route.TargetPlatform,
		UpstreamModel:  route.UpstreamModel,
		Protocols:      route.Protocols,
		ProfileID:      profile.ID,
		ProfileDigest:  profile.Digest,
		EndpointDigest: route.EndpointDigest,
		RemoveImage:    removeImage,
	}
	raw, _ := json.Marshal(input)
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

// isCodexNonChatModelName 识别向量、重排、语音转写、审核等明显不是对话模型的公开名。
// 现有 isCodexDedicatedMediaModel 只覆盖图片与视频生成，这里补充其余专用模型。
func isCodexNonChatModelName(modelID string) bool {
	name := strings.ToLower(codexProviderQualifiedModelID(modelID))
	for _, token := range []string{"embedding", "rerank", "moderation", "whisper", "transcribe", "-tts", "tts-", "video"} {
		if strings.Contains(name, token) {
			return true
		}
	}
	return false
}
