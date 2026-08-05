package service

import "strings"

// resolveVerifiedCodexCanonicalUserAgent 是上游 header 身份基础设施与本项目
// strict wire 画像之间的唯一桥接点。它只读取 ReleaseCatalog 的 active 指针；
// GitHub 最新版本发现、管理端手填版本和账号级 UA 都不能在此处激活新版本。
func resolveVerifiedCodexCanonicalUserAgent() string {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	if err != nil || strings.TrimSpace(profile.Build.UserAgent) == "" {
		return codexCLIUserAgent
	}
	return profile.Build.UserAgent
}

// resolveVerifiedCodexClientVersion 返回当前已验收 active ReleaseBundle 的版本号，
// 用于管理端同时展示“已发现版本”和“严格出站版本”。
func resolveVerifiedCodexClientVersion() string {
	profile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	if err != nil || NormalizeCodexClientVersion(profile.Build.Version) == "" {
		return codexCLIVersion
	}
	return profile.Build.Version
}
