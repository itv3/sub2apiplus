package service

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
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

// syntheticPromptCacheKeyLegacyMutation 把 Responses 两个端点的 session-id 来源改回会话身份
// （旧画像形态），是 syntheticPromptCacheKeySessionMutation 的逆操作。
func syntheticPromptCacheKeyLegacyMutation(t *testing.T) func(*profilecontract.SnapshotDoc) {
	return func(doc *profilecontract.SnapshotDoc) {
		for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesWS} {
			syntheticServiceSetHeaderSource(t, doc, endpointID, "session-id", profilecontract.SourceSession)
		}
	}
}

// syntheticServiceRemoveHeaderSlot 删除端点上指定名字的全部 header 槽位，并同步从
// HeaderMapInsertionOrder 中去掉该名字；端点本来没有该槽位时为空操作。
func syntheticServiceRemoveHeaderSlot(
	t *testing.T,
	doc *profilecontract.SnapshotDoc,
	endpointID string,
	headerName string,
) {
	t.Helper()
	endpoint := syntheticServiceSnapshotEndpoint(t, doc, endpointID)
	kept := make([]profilecontract.SnapshotHeaderSlot, 0, len(endpoint.Headers))
	for _, slot := range endpoint.Headers {
		if !strings.EqualFold(slot.Name, headerName) {
			kept = append(kept, slot)
		}
	}
	endpoint.Headers = kept
	if len(endpoint.HeaderMapInsertionOrder) > 0 {
		order := make([]string, 0, len(endpoint.HeaderMapInsertionOrder))
		for _, name := range endpoint.HeaderMapInsertionOrder {
			if !strings.EqualFold(name, headerName) {
				order = append(order, name)
			}
		}
		endpoint.HeaderMapInsertionOrder = order
	}
}

// ============================================================================
// 按结构事实选择发布槽位（VC-6 晋升前后同一用例都成立）
//
// 候选期目标画像在 previous 槽位、Active 是旧画像；VC-6 晋升后两者对调。凡是写死
// “Active 是旧画像”的用例，都改为按画像声明的结构事实来选择对照组或槽位：
//   - 旧画像对照组：withOfficialCodexLegacySyntheticProfile 以 Active 为底稿去掉被测结构；
//   - 正式目录逐槽位断言：officialCodexFormalExecutableProfile 直接读正式目录推导期望；
//   - 只在旧画像中存在的端点（legacy compact）：officialCodexLegacyCompactProfileMode
//     选出仍声明该端点的槽位，再用 withOfficialCodexLegacyCompactRelease 把网关服务的
//     画像配置与 Executor runtime 一起指向它。
// ============================================================================

// officialCodexFormalModes 是正式 ReleaseCatalog 的两个发布槽位（Active 在前）。
var officialCodexFormalModes = []string{officialClientProfileModeActive, officialClientProfileModePrevious}

// officialCodexFormalExecutableProfile 直接读正式 ReleaseCatalog 中 mode 槽位的可执行画像，
// 不经过 service 的包级画像入口，因此不受 withOfficialCodexSyntheticProfile 替换的影响。
// 用于从画像事实推导期望，被测函数仍走生产入口。
func officialCodexFormalExecutableProfile(t *testing.T, mode string) profilecontract.ExecutableProfile {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
	require.NoError(t, err, "解析 %s 槽位的正式发布失败", mode)
	return release.ExecutableProfile()
}

// withOfficialCodexLegacySyntheticProfile 为“画像未声明某结构时行为不变”一类断言提供旧画像
// 对照组：以正式目录 Active 画像为底稿，用 strip 去掉被测结构，替换包级画像入口，并要求
// 合成结果确实不再声明该结构（declares 按结构事实判定）。
//
// 这类断言改动前直接读 Active 画像：候选期 Active 就是旧画像，strip 对它是空操作，结果与
// 改动前等价；VC-6 晋升后 Active 换成已声明被测结构的目标画像，strip 把它还原为未声明的
// 形态，断言验证的仍是“画像未声明该结构时行为回到旧逻辑”这一原意。底稿固定取 Active，
// 是为了让合成画像与 service 其余按 Active 读取的身份（版本、UA）保持一致。
func withOfficialCodexLegacySyntheticProfile(
	t *testing.T,
	feature string,
	declares func(profilecontract.ExecutableProfile) bool,
	strip func(*profilecontract.SnapshotDoc),
) profilecontract.ExecutableProfile {
	t.Helper()
	legacy := withOfficialCodexSyntheticProfile(t, strip)
	require.False(t, declares(legacy),
		"以 Active 为底稿去掉「%s」后合成画像仍声明该结构：strip 与判定口径不一致，旧画像对照组无效", feature)
	return legacy
}

// officialCodexLegacyCompactProfileMode 按结构事实返回正式目录中仍声明 legacy compact 端点
// （responses_compact）的发布槽位，Active 优先。
//
// 目标画像删除了 legacy compact 端点，VC-6 晋升后 Active 下的显式 compact 请求按批准语义
// 在端点解析处失败关闭（由晋升后门禁覆盖）。既有 legacy compact 用例验证的是“画像声明
// compact 端点时”的转发、探针与改写行为，改为在仍声明该端点的槽位上运行：候选期是
// Active（与改动前相同），晋升后是 Previous 中的旧画像（回滚目标）。两个槽位都不再声明时
// 不能静默跳过：只有 officialegress 发布退役清单正式登记了该 route（见
// officialLegacyCompactReleaseRetired），这些成功路径用例才随退役跳过，失败关闭由发布判定与
// 路由退役用例覆盖；没有退役登记时直接失败。
func officialCodexLegacyCompactProfileMode(t *testing.T) string {
	t.Helper()
	for _, mode := range officialCodexFormalModes {
		for _, endpoint := range officialCodexFormalExecutableProfile(t, mode).Endpoints() {
			if endpoint.ID == officialCodexEndpointResponsesCompact {
				return mode
			}
		}
	}
	if officialLegacyCompactReleaseRetired(t) {
		t.Skip("legacy compact 已按 officialegress 发布退役清单退役：正式目录的 Active/Previous 都不再声明该端点，" +
			"官方出站 compact 成功路径不可达；入站失败关闭由发布判定用例覆盖，路由退役由 officialegress 用例覆盖")
	}
	t.Fatal("正式目录的 Active/Previous 都不再声明 legacy compact 端点：legacy compact 相关用例应随该端点的代码路径一并退役")
	return ""
}

// officialLegacyCompactReleaseRetired 判断 legacy compact 是否已正式发布退役：officialegress 的
// catalogdata/release-route-retirements.json 登记了 POST chatgpt.com /backend-api/codex/responses/compact
// （端点 responses_compact），且正式目录的 Active/Previous 都不再声明该端点。测试侧独立读取清单，不调用被测包。
func officialLegacyCompactReleaseRetired(t *testing.T) bool {
	t.Helper()
	for _, mode := range officialCodexFormalModes {
		for _, endpoint := range officialCodexFormalExecutableProfile(t, mode).Endpoints() {
			if endpoint.ID == officialCodexEndpointResponsesCompact {
				return false
			}
		}
	}
	raw, err := os.ReadFile(filepath.Join("..", "officialegress", "catalogdata", "release-route-retirements.json"))
	require.NoError(t, err)
	var manifest struct {
		Routes []struct {
			Method     string `json:"method"`
			Host       string `json:"host"`
			Path       string `json:"path"`
			Protocol   string `json:"protocol"`
			EndpointID string `json:"endpoint_id"`
		} `json:"routes"`
	}
	require.NoError(t, json.Unmarshal(raw, &manifest))
	for _, route := range manifest.Routes {
		if route.Method == http.MethodPost && route.Host == "chatgpt.com" &&
			route.Path == "/backend-api/codex/responses/compact" && route.Protocol == "http" &&
			route.EndpointID == officialCodexEndpointResponsesCompact {
			return true
		}
	}
	return false
}

// newOfficialEgressTestRuntimeForMode 与测试 runtime 工厂的构造相同（正式 Guard 配置、生产
// Compiler/Executor），只是 Executor 冻结在 mode 槽位。
func newOfficialEgressTestRuntimeForMode(
	t *testing.T,
	httpUpstream HTTPUpstream,
	mode string,
) *OfficialEgressTransitionRuntime {
	t.Helper()
	base := officialegress.DefaultGuard()
	guard, err := officialegress.NewGuard(
		base.Config(), officialegress.DefaultSinkCatalog(),
		officialegress.DefaultOfficialRouteCatalog(), base.Recorder(),
	)
	require.NoError(t, err)
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard, httpUpstream, officialCodexExecutorID, officialegress.ReleaseMode(mode),
	)
	require.NoError(t, err, "构造 %s 槽位的官方出站 runtime 失败", mode)
	return runtimeState
}

// withOfficialCodexLegacyCompactProfileConfig 只把网关服务的 service 画像配置指向仍声明
// legacy compact 端点的槽位，返回该槽位名。用于只构造上游请求、不经过 Executor 的用例。
func withOfficialCodexLegacyCompactProfileConfig(t *testing.T, svc *OpenAIGatewayService) string {
	t.Helper()
	mode := officialCodexLegacyCompactProfileMode(t)
	if svc.cfg == nil {
		svc.cfg = &config.Config{}
	}
	svc.cfg.Gateway.OfficialClientProfiles.Mode = mode
	return mode
}

// withOfficialCodexLegacyCompactRelease 把网关服务的发布槽位——service 画像配置（生产上
// 由 officialEgressReleaseModeFromConfig 与 Executor 同源）与 Executor runtime——一起指向
// 仍声明 legacy compact 端点的槽位，返回该槽位名。两者必须指向同一槽位，否则 service 层
// 会按另一份画像派生身份事实。
func withOfficialCodexLegacyCompactRelease(t *testing.T, svc *OpenAIGatewayService) string {
	t.Helper()
	mode := withOfficialCodexLegacyCompactProfileConfig(t, svc)
	svc.officialEgress = newOfficialEgressTestRuntimeForMode(t, svc.httpUpstream, mode)
	return mode
}

// withOfficialCodexLegacyCompactAccountTestRelease 把管理端账号测试服务的 Executor runtime
// 指向仍声明 legacy compact 端点的槽位，返回该槽位名。管理端 compact 探针的 OAuth 分支只经
// Executor 出站（Bundle 由 runtime 的发布槽位决定），不读 service 画像配置。
func withOfficialCodexLegacyCompactAccountTestRelease(t *testing.T, svc *AccountTestService) string {
	t.Helper()
	mode := officialCodexLegacyCompactProfileMode(t)
	svc.officialEgress = newOfficialEgressTestRuntimeForMode(t, svc.httpUpstream, mode)
	return mode
}

// officialCodexBuildIdentityForMode 返回 mode 槽位 OpenAI OAuth HTTP 发布节点的客户端版本与
// User-Agent（与 codexCLIVersion/codexCLIUserAgent 对 Active 的派生方式相同）。在非 Active
// 槽位运行的用例用它替代只代表 Active 的 codexCLIVersion/codexCLIUserAgent。
func officialCodexBuildIdentityForMode(t *testing.T, mode string) (version string, userAgent string) {
	t.Helper()
	release, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseMode(mode))
	require.NoError(t, err)
	node, ok := release.Node(officialegress.RegistryPurposeOpenAIOAuthHTTP)
	require.True(t, ok, "%s 槽位缺少 OpenAI OAuth HTTP 发布节点", mode)
	return NormalizeCodexClientVersion(node.Build.Version), strings.TrimSpace(node.Build.UserAgent)
}
