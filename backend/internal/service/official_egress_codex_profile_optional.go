package service

import (
	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

// 本文件是 service 读取“当前 release 画像”新行为开关的唯一入口。
//
// 总原则：全部新行为只由调用冻结的 release mode 对应画像驱动——画像没有对应的可选节、
// 条件或取值来源时，调用方保持旧逻辑；共享代码不按版本号分支。解析失败同样视为
// “画像未声明”，由调用方退回旧逻辑（正式请求在其他环节会因同一解析失败而失败关闭）。

// officialCodexExecutableProfileForMode 按 release mode 返回正式目录中的可执行画像。
// 它是包级变量，便于测试在目标版本画像入库前注入合成画像；生产代码不得改写。
var officialCodexExecutableProfileForMode = func(mode string) (profilecontract.ExecutableProfile, error) {
	release, err := officialegress.DefaultReleaseCatalog().Resolve(
		officialegress.ReleaseMode(normalizeOfficialClientProfileMode(mode)),
	)
	if err != nil {
		return profilecontract.ExecutableProfile{}, err
	}
	return release.ExecutableProfile(), nil
}

// officialCodexOptionalSectionsForMode 返回 mode 对应画像的可选节投影；
// 画像不可解析时返回零值（全部节缺席，即旧逻辑）。
func officialCodexOptionalSectionsForMode(mode string) profilecontract.OptionalSections {
	profile, err := officialCodexExecutableProfileForMode(mode)
	if err != nil {
		return profilecontract.OptionalSections{}
	}
	return profile.Optional()
}

// officialCodexProfileUsesPromptCacheKeySession 判断 mode 对应画像的 Responses 端点
// 是否以 prompt_cache_key 为 session-id 来源。声明该来源的画像才建模临时 fork 的
// “源会话缓存键”，service 才会为根会话 fork 派生与本会话不同的 prompt cache 键。
func officialCodexProfileUsesPromptCacheKeySession(mode string) bool {
	profile, err := officialCodexExecutableProfileForMode(mode)
	if err != nil {
		return false
	}
	for _, id := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
		endpoint, found := profile.Endpoint(id)
		if !found {
			continue
		}
		for _, slot := range endpoint.Headers {
			if slot.Source == profilecontract.SourcePromptCacheKey {
				return true
			}
		}
	}
	return false
}
