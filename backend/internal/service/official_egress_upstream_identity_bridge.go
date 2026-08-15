package service

import (
	"fmt"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// mustResolveActiveCodexIdentity 在进程启动时从正式发布目录读取唯一默认身份。
// 默认身份被多个兼容路径复用，因此目录异常必须 fail-close，不能静默退回硬编码值。
func mustResolveActiveCodexIdentity() (string, string) {
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	if err != nil {
		panic(fmt.Errorf("解析 Active Codex ReleaseCatalog: %w", err))
	}
	node, ok := release.Node(officialegress.RegistryPurposeOpenAIOAuthHTTP)
	if !ok {
		panic("Active Codex ReleaseCatalog 缺少 OpenAI OAuth HTTP 节点")
	}
	userAgent := strings.TrimSpace(node.Build.UserAgent)
	version := NormalizeCodexClientVersion(node.Build.Version)
	if userAgent == "" || version == "" {
		panic("Active Codex ReleaseCatalog 的客户端身份不完整")
	}
	return userAgent, version
}

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
