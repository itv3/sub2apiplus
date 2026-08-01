package service

import (
	"net/http"
	"net/url"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

// 本文件提供“按 registry 的 active 版本解析”的统一入口。
//
// §3.2 要求升级只需登记新快照并调整 release 指针，共享接入点不必改动。这条承诺的
// 前提是业务层不持有版本常量：调用点一旦写死某个版本，新快照登记后它仍按旧版本
// 解析——主链与辅助链就此分叉，同账号同 IP 上出现两种版本形态，正是 §3.1 列为最强
// 识别特征的那类不一致。
//
// 这些包装把版本解析收敛到 activeOfficialCodexVersion() 一处，调用点只表达“用当前
// 发布版本”这个意图。需要显式指定版本的场景（画像自身的测试、跨版本比对）仍直接
// 使用带 version 参数的底层 API。

func resolveActiveCodexVersionProfile() (*officialCodexVersionProfile, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, err
	}
	return resolveCodex0145VersionProfile(version)
}

func resolveActiveCodexEndpoint(
	endpointID codex0145EndpointID,
) (officialCodexEndpointProfile, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return officialCodexEndpointProfile{}, err
	}
	return resolveCodex0145Endpoint(version, endpointID)
}

func buildActiveCodexEndpointURL(
	endpointID codex0145EndpointID,
	input officialCodex0145EndpointURLInput,
) (*url.URL, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, err
	}
	return officialCodex0145BuildEndpointURL(version, endpointID, input)
}

func resolveActiveCodexEndpointTLSProfile(
	endpointID codex0145EndpointID,
) (*tlsfingerprint.Profile, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, err
	}
	return officialCodex0145ResolveEndpointTLSProfile(version, endpointID)
}

func resolveActiveCodexEndpointTLSProfileForURL(
	endpointID codex0145EndpointID,
	target *url.URL,
) (*tlsfingerprint.Profile, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, err
	}
	return officialCodex0145ResolveEndpointTLSProfileForURL(version, endpointID, target)
}

func applyActiveCodexHeaderContract(
	endpointID codex0145EndpointID,
	headers http.Header,
	conditions map[string]bool,
) ([]officialCodex0145HeaderField, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, err
	}
	return officialCodex0145ApplyHeaderContract(version, endpointID, headers, conditions)
}

func projectActiveCodexEndpointJSONBody(
	endpointID codex0145EndpointID,
	payload map[string]any,
	original []byte,
	conditions map[string]bool,
) ([]byte, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, err
	}
	return officialCodex0145ProjectEndpointJSONBody(
		version,
		endpointID,
		payload,
		original,
		conditions,
	)
}

func validateAndOrderActiveCodexFormBody(
	endpointID codex0145EndpointID,
	values url.Values,
) ([]byte, error) {
	version, err := activeOfficialCodexVersion()
	if err != nil {
		return nil, err
	}
	return officialCodex0145ValidateAndOrderFormBody(version, endpointID, values)
}
