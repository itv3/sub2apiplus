package service

import (
	"net/http"
	"net/url"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

// 本文件仅把正式 ReleaseCatalog 的显式 mode 投影为过渡期 service DTO。
// mode 由调用开始时冻结，不允许任何下游函数自行选择 active。

func resolveCodexVersionProfileForMode(mode string) (*officialCodexVersionProfile, error) {
	release, err := officialegress.DefaultReleaseCatalog().Resolve(
		officialegress.ReleaseMode(normalizeOfficialClientProfileMode(mode)),
	)
	if err != nil {
		return nil, err
	}
	return resolveOfficialCodexReleaseProfile(release)
}

func resolveCodexEndpointForMode[T ~string](
	mode string,
	endpointID T,
) (officialCodexEndpointProfile, error) {
	profile, err := resolveCodexVersionProfileForMode(mode)
	if err != nil {
		return officialCodexEndpointProfile{}, err
	}
	return profile.ResolveEndpoint(string(endpointID))
}

func buildCodexEndpointURLForMode[T ~string](
	mode string,
	endpointID T,
	input officialCodexEndpointURLInput,
) (*url.URL, error) {
	profile, err := resolveCodexVersionProfileForMode(mode)
	if err != nil {
		return nil, err
	}
	return buildOfficialCodexEndpointURL(profile, string(endpointID), input)
}

func resolveCodexEndpointTLSProfileForMode[T ~string](
	mode string,
	endpointID T,
) (*tlsfingerprint.Profile, error) {
	profile, err := resolveCodexVersionProfileForMode(mode)
	if err != nil {
		return nil, err
	}
	return resolveOfficialCodexEndpointTLSProfile(profile, string(endpointID), nil)
}

func applyCodexHeaderContractForMode[T ~string](
	mode string,
	endpointID T,
	headers http.Header,
	conditions map[string]bool,
) ([]officialCodexHeaderField, error) {
	profile, err := resolveCodexVersionProfileForMode(mode)
	if err != nil {
		return nil, err
	}
	return applyOfficialCodexHeaderContract(profile, string(endpointID), headers, conditions)
}

func projectCodexEndpointJSONBodyForMode[T ~string](
	mode string,
	endpointID T,
	payload map[string]any,
	original []byte,
	conditions map[string]bool,
) ([]byte, error) {
	profile, err := resolveCodexVersionProfileForMode(mode)
	if err != nil {
		return nil, err
	}
	return projectOfficialCodexEndpointJSONBody(
		profile, string(endpointID), payload, original, conditions,
	)
}
