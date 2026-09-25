package service

import (
	"encoding/json"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
)

// 合成画像夹具：以正式目录 Active release 的 ProfileSpec 为底稿，应用快照改写后重新
// 编译可执行画像，并临时替换 officialCodexExecutableProfileForMode，使 service 读到
// “声明了新可选节／新条件／新来源”的画像。正式目录不被修改；用例结束自动恢复。
//
// 注意：替换的是包级入口，使用本夹具的用例不得并行执行。

func withOfficialCodexSyntheticProfile(
	t *testing.T,
	mutate func(*profilecontract.SnapshotDoc),
) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	require.NoError(t, err)
	doc := release.Profile().ToSnapshot()
	if mutate != nil {
		mutate(&doc)
	}
	spec, err := profilecontract.NewProfileSpec(doc)
	require.NoError(t, err)
	executable, err := profilecontract.CompileExecutableProfile(spec)
	require.NoError(t, err)
	previous := officialCodexExecutableProfileForMode
	officialCodexExecutableProfileForMode = func(string) (profilecontract.ExecutableProfile, error) {
		return executable, nil
	}
	t.Cleanup(func() { officialCodexExecutableProfileForMode = previous })
	return executable
}

func syntheticServiceSnapshotEndpoint(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
) *profilecontract.SnapshotEndpoint {
	t.Helper()
	for index := range doc.Endpoints {
		if doc.Endpoints[index].ID == endpointID {
			return &doc.Endpoints[index]
		}
	}
	t.Fatalf("快照中没有端点 %s", endpointID)
	return nil
}

func syntheticServiceSetHeaderSource(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
	headerName string,
	source profilecontract.ValueSource,
) {
	t.Helper()
	endpoint := syntheticServiceSnapshotEndpoint(t, doc, endpointID)
	for index := range endpoint.Headers {
		if endpoint.Headers[index].Name == headerName {
			endpoint.Headers[index].Source = string(source)
			return
		}
	}
	t.Fatalf("端点 %s 没有 header 槽位 %s", endpointID, headerName)
}

func syntheticServiceRawSection(t *testing.T, value any) json.RawMessage {
	t.Helper()
	raw, err := json.Marshal(value)
	require.NoError(t, err)
	return raw
}

// syntheticPromptCacheKeySessionMutation 把 Responses 两个端点的 session-id 来源改为
// prompt_cache_key（目标画像 SPEC-HDR-007 的形态）。
func syntheticPromptCacheKeySessionMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
			syntheticServiceSetHeaderSource(t, doc, endpointID, "session-id", profilecontract.SourcePromptCacheKey)
		}
	}
}
