package service

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/stretchr/testify/require"
)

// 本文件是版本升级的安全网。
//
// §3.2 与 §4.5.13 承诺新增 Codex 版本只需新建快照、登记注册表并调整 registry 的
// release 指针，§3.5.2 的共享接入点无需改动。该承诺依赖一个前提：出站形态的每一
// 处版本来源都收敛到同一个 active 版本。下面的测试逐条验证这个前提。
//
// 它们当前全部为绿——因为 active 版本恰好等于编译期常量，两个来源暂时重合。一旦
// release 指针指向新版本而某条路径仍绑定常量，对应测试立即变红并指出分叉位置。
// 这正是升级时最需要、而单版本测试无法提供的信号。

// synthesizeOfficialCodexNextVersionProfile 基于现役画像合成一份「下一个版本」的
// 自洽快照。
//
// 合成画像只改版本标识，不改任何 wire 语义：它要回答的是「版本号本身能否换掉」，
// 而不是「新版本长什么样」。真实升级的画像内容必须来自第四部分的抓包 Campaign，
// 不能由本函数推断。
func synthesizeOfficialCodexNextVersionProfile(
	t *testing.T,
	nextVersion string,
) officialCodexVersionProfile {
	t.Helper()
	active, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	synthetic, err := cloneOfficialCodexVersionProfile(active)
	require.NoError(t, err)

	synthetic.Version = nextVersion
	for index := range synthetic.Surfaces {
		synthetic.Surfaces[index].Version = nextVersion
	}
	digest, err := digestOfficialCodexVersionProfile(synthetic)
	require.NoError(t, err)
	synthetic.Digest = digest
	return synthetic
}

// registerSyntheticCodexRelease 按生产流程注册一个合成版本：编码成 JSON 快照，经
// compileOfficialCodexVersionSnapshot 完成解码、结构校验与摘要核对，再登记进编译缓存。
//
// 直接把构造好的 Go 对象塞进 officialCodexCompiledProfiles 会跳过这三道关口，测试就
// 回答不了"新版本快照本身能否通过生产的编译流程"——而那正是升级时第一个会失败的环节。
func registerSyntheticCodexRelease(
	t *testing.T,
	version string,
	mutate func(*officialCodexVersionProfile),
) {
	t.Helper()
	synthetic := synthesizeOfficialCodexNextVersionProfile(t, version)
	if mutate != nil {
		mutate(&synthetic)
		digest, err := digestOfficialCodexVersionProfile(synthetic)
		require.NoError(t, err)
		synthetic.Digest = digest
	}

	officialCodexVersionValidators[version] = func(profile officialCodexVersionProfile) error {
		if profile.Version != version {
			return fmt.Errorf("版本必须精确为 %s", version)
		}
		return nil
	}

	encoded, err := json.Marshal(synthetic)
	require.NoError(t, err)
	compiled, err := compileOfficialCodexVersionSnapshot(
		version,
		officialCodexProfileSnapshot{JSON: string(encoded), Digest: synthetic.Digest},
	)
	// 说明覆盖边界：合成 validator 只断言版本号，因此这里验证的是"快照能否通过生产
	// 的解码、版本校验器分派与摘要核对"，不等于对画像结构做了完整断言——结构断言由
	// 各版本自己的专属校验器负责（现役版本见 validateCodex0145Profile）。
	require.NoError(t, err, "新版本快照必须能通过生产的解码、校验器分派与摘要核对")
	require.Equal(t, version, compiled.Version)
	officialCodexCompiledProfiles[version] = compiled

	t.Cleanup(func() {
		delete(officialCodexVersionValidators, version)
		delete(officialCodexCompiledProfiles, version)
	})
}

// TestOfficialCodexUpgradeAcceptsRegisteredVersionValidator 验证「升级只需登记快照」
// 在代码侧已经成立：通用解析路径不再要求版本等于编译期常量。
//
// 改造前这里写着 `profile.Version != officialCodexVersion0145 → 报错`，注册表按版本
// 查表的能力被这一行整个抵消——登记一份完全自洽的 0.146.0 快照，解析阶段照样失败。
// 现在通用入口只要求该版本登记了专属校验器，而校验器随新版本快照文件一起提供，
// 因此不触碰通用引擎与 §3.5.2 的共享接入点。
func TestOfficialCodexUpgradeAcceptsRegisteredVersionValidator(t *testing.T) {
	const nextVersion = "0.146.0"
	synthetic := synthesizeOfficialCodexNextVersionProfile(t, nextVersion)

	// 先证明合成快照除版本号外完全自洽：把版本改回现役值即可通过全部断言。
	sameVersion := synthetic
	sameVersion.Version = officialCodexVersion0145
	for index := range sameVersion.Surfaces {
		sameVersion.Surfaces[index].Version = officialCodexVersion0145
	}
	require.NoError(
		t,
		validateOfficialCodexVersionProfile(sameVersion),
		"合成快照本身应当自洽，否则下面的断言无法归因到版本注册",
	)

	// 未登记校验器的版本必须明确失败，且不回退到任何既有版本。
	err := validateOfficialCodexVersionProfile(synthetic)
	require.ErrorContains(t, err, nextVersion)
	require.ErrorContains(t, err, "未登记")

	// 登记专属校验器后即可通过——这正是新增版本快照文件所要做的事。
	officialCodexVersionValidators[nextVersion] = func(profile officialCodexVersionProfile) error {
		if profile.Version != nextVersion {
			return fmt.Errorf("版本必须精确为 %s", nextVersion)
		}
		return nil
	}
	t.Cleanup(func() { delete(officialCodexVersionValidators, nextVersion) })

	require.NoError(
		t,
		validateOfficialCodexVersionProfile(synthetic),
		"登记专属校验器后，通用路径必须接受新版本画像",
	)

	// 现役版本仍走自己的严格断言，不因通用入口放开而降低要求。
	active, err := resolveCodex0145VersionProfile(officialCodexVersion0145)
	require.NoError(t, err)
	require.NoError(t, validateOfficialCodexVersionProfile(*active))
}

// TestOfficialCodexVersionHeaderFollowsSuppliedRelease 锁定主 Responses HTTP 链的
// version header 跟随传入的版本，而不是编译期常量。
//
// 改造前这里写死 officialCodexVersion0145：release 指针切到新版本后，辅助端点与 WS
// 会按新画像出站而主链仍发旧版本号——同一账号同一 IP 上出现两种版本形态，正是 §3.1
// 列为最强识别特征的那类不一致。生产调用点传入的是 egressContext.ProfileVersion()。
func TestOfficialCodexVersionHeaderFollowsSuppliedRelease(t *testing.T) {
	const nextVersion = "0.146.0"
	header := http.Header{}
	finalizeOfficialOpenAIHTTPHeaders(
		header,
		nextVersion,
		"codex_exec/0.146.0 (Ubuntu 24.4.0; x86_64) unknown",
		"codex_exec",
		officialOpenAIHTTPIdentity{},
		false,
		false,
		"",
	)

	require.Equal(
		t,
		nextVersion,
		header.Get("version"),
		"主链 version header 没有跟随传入版本，仍绑定编译期常量",
	)
}

// TestOfficialCodexVersionHeaderSlotMatchesProfileVersion 锁定画像自洽：每个端点
// version 槽位声明的值必须等于画像自身的版本号。
//
// 主链的 version 取自运行上下文，辅助端点与 WS 取自画像槽位；两条来源一致的前提是
// 快照内部自洽。新版本快照若在槽位里漏改版本号，出站就会出现 header 与画像版本不符
// 的组合，而这类错误在只看单一版本的测试里根本暴露不出来。
func TestOfficialCodexVersionHeaderSlotMatchesProfileVersion(t *testing.T) {
	activeVersion, err := activeOfficialCodexVersion()
	require.NoError(t, err)
	profile, err := resolveCodex0145VersionProfile(activeVersion)
	require.NoError(t, err)

	checked := 0
	for _, endpoint := range profile.Endpoints {
		for _, slot := range endpoint.OrderedHeaders() {
			if !strings.EqualFold(slot.Name, "version") || slot.Value == "" {
				continue
			}
			require.Equalf(
				t,
				profile.Version,
				slot.Value,
				"端点 %s 的 version 槽位声明与画像自身版本不符",
				endpoint.ID,
			)
			checked++
		}
	}
	require.NotZero(t, checked, "画像里应当至少存在一个 version 槽位")
}

// TestOfficialCodexEndpointsResolveUnderActiveRelease 遍历画像内全部端点，断言它们
// 都能在 registry 的 active 版本下解析。
//
// release 指针切到尚未登记快照的版本时，这里会整体失败，把「升级只切了指针、忘了
// 登记快照」这一类错误挡在启动前，而不是留到某个辅助端点的首个真实请求。
func TestOfficialCodexEndpointsResolveUnderActiveRelease(t *testing.T) {
	activeVersion, err := activeOfficialCodexVersion()
	require.NoError(t, err)

	profile, err := resolveCodex0145VersionProfile(activeVersion)
	require.NoErrorf(t, err, "active 版本 %s 未登记版本快照", activeVersion)
	require.NotEmpty(t, profile.Endpoints)

	for _, endpoint := range profile.Endpoints {
		resolved, err := resolveCodex0145Endpoint(
			activeVersion,
			codex0145EndpointID(endpoint.ID),
		)
		require.NoErrorf(t, err, "端点 %s 无法在 active 版本下解析", endpoint.ID)
		require.Equal(t, endpoint.ID, resolved.ID)
		require.NotEmpty(t, resolved.TransportID)
	}
}

// TestOfficialCodexTransportProfilesFollowActiveRelease 锁定第三个阻塞点：HTTP 与
// WS 的传输画像在生产路径上绑定编译期版本常量。
//
// TLS 参数与 ClientHello 由传输画像决定。若它不随 release 指针切换，升级后网关会
// 用旧版本的 ClientHello 承载新版本的 header 与 body，形态自相矛盾。
func TestOfficialCodexTransportProfilesFollowActiveRelease(t *testing.T) {
	activeVersion, err := activeOfficialCodexVersion()
	require.NoError(t, err)

	for name, profile := range map[string]string{
		"HTTP":      newOpenAIOfficialEgressHTTPTLSProfile().Name,
		"WebSocket": newOpenAIOfficialEgressWebSocketTLSProfile().Name,
	} {
		require.Containsf(
			t,
			profile,
			activeVersion,
			"%s 传输画像绑定的是编译期版本而非 active 版本，升级后 TLS 层不会跟随切换",
			name,
		)
	}
}

// TestOfficialCodexJSONFieldOrderFollowsActiveRelease 锁定第四个阻塞点：body 的
// 字段顺序在进程启动时从固定版本读取。
//
// 顺序属于 wire 形态的一部分，必须与 header、TLS 出自同一版本，否则升级后会出现
// 新版本 header 搭配旧版本字段序的组合。
func TestOfficialCodexJSONFieldOrderFollowsActiveRelease(t *testing.T) {
	activeVersion, err := activeOfficialCodexVersion()
	require.NoError(t, err)

	endpoint, err := resolveCodex0145Endpoint(
		activeVersion,
		codex0145EndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err)
	require.NotEmpty(t, endpoint.Body.Fields)

	expected := make([]string, 0, len(endpoint.Body.Fields))
	for _, field := range endpoint.Body.Fields {
		expected = append(expected, field.Name)
	}
	require.Equal(
		t,
		expected,
		officialOpenAIHTTPFieldOrder,
		"进程内的 body 字段顺序与 active 版本画像不一致，升级后字段序不会跟随切换",
	)
}

// TestOfficialCodexUpgradeRegisteredSnapshotDrivesWholeChain 直接验证「升级只改数据」：
// 登记一份新版本快照与它的专属校验器之后，解析、端点与传输画像全部跟随新版本，
// 无需改动通用执行引擎或 §3.5.2 的共享接入点。
//
// 这里模拟的正是新增版本快照文件所做的三件事：登记专属校验器、把快照编译进解析表、
// 让调用方用新版本号解析。真实升级还需要在 registry 补 build/wire profile 并切换
// release 指针，那部分由 TestOfficialCodexEndpointsResolveUnderActiveRelease 覆盖。
func TestOfficialCodexUpgradeRegisteredSnapshotDrivesWholeChain(t *testing.T) {
	const nextVersion = "0.146.0"
	registerSyntheticCodexRelease(t, nextVersion, nil)

	profile, err := resolveCodex0145VersionProfile(nextVersion)
	require.NoError(t, err, "登记快照后新版本必须可解析")
	require.Equal(t, nextVersion, profile.Version)

	endpoint, err := resolveCodex0145Endpoint(
		nextVersion,
		codex0145EndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err, "新版本下端点解析必须可用")
	require.Equal(t, officialCodexEndpointResponsesHTTP, endpoint.ID)

	// 传输画像按协议解析，不依赖带版本号的传输 ID 常量；画像名带上新版本，
	// 说明 TLS 层确实跟随了 release 而不是停在编译期常量上。
	for _, protocol := range []string{
		officialCodexTransportProtocolHTTP1,
		officialCodexTransportProtocolWS,
	} {
		tlsProfile, err := resolveOfficialCodexDefaultTLSProfile(nextVersion, protocol)
		require.NoErrorf(t, err, "协议 %s 在新版本下无法解析传输画像", protocol)
		require.Containsf(
			t,
			tlsProfile.Name,
			nextVersion,
			"协议 %s 的传输画像没有跟随新版本",
			protocol,
		)
	}
}

// TestOfficialCodexUpgradeFollowsSwitchedReleasePointer 真正切换 registry 的 release
// 指针，验证业务入口跟随到新版本。
//
// 它与上面的 …RegisteredSnapshotDrivesWholeChain 不重复，区别恰恰是本文件最需要覆盖
// 的那一层：那个测试把 nextVersion 当参数直接传给解析函数，证明的是「底层解析支持
// 任意版本」；本测试不传版本，改的是 release 指针本身，证明的是「业务路径确实按
// active 版本解析」。少了后者，某处把版本源换回编译期常量时，前者依然全绿。
//
// 本测试写包级注册表与 registry，不能加 t.Parallel()；全部改动在 t.Cleanup 还原。
func TestOfficialCodexUpgradeFollowsSwitchedReleasePointer(t *testing.T) {
	const nextVersion = "0.146.0"
	registerSyntheticCodexRelease(t, nextVersion, nil)

	activeProfile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	buildID := activeProfile.Build.ID
	originalBuild := defaultOfficialClientProfileRegistry.builds[buildID]
	require.Equal(t, officialCodexVersion0145, originalBuild.Version, "前置条件：当前 active 应为现役版本")

	// 切 release 指针：registry 对外只暴露 Build.Version，改它等价于指向新发布。
	switchedBuild := originalBuild
	switchedBuild.Version = nextVersion
	defaultOfficialClientProfileRegistry.builds[buildID] = switchedBuild

	t.Cleanup(func() {
		defaultOfficialClientProfileRegistry.builds[buildID] = originalBuild
	})

	active, err := activeOfficialCodexVersion()
	require.NoError(t, err)
	require.Equal(t, nextVersion, active, "release 指针切换未生效，后续断言无意义")

	profile, err := resolveActiveCodexVersionProfile()
	require.NoError(t, err)
	require.Equal(t, nextVersion, profile.Version, "画像解析没有跟随 release 指针")

	endpoint, err := resolveActiveCodexEndpoint(
		codex0145EndpointID(officialCodexEndpointResponsesHTTP),
	)
	require.NoError(t, err, "端点解析没有跟随 release 指针")
	require.Equal(t, officialCodexEndpointResponsesHTTP, endpoint.ID)

	// 历史构造器同样必须跟随：它们没有运行上下文，只能从 registry 取版本，
	// 一旦退回编译期常量，TLS 层就会用旧版本 ClientHello 承载新版本 header。
	for name, tlsProfile := range map[string]*tlsfingerprint.Profile{
		"HTTP":      newOpenAIOfficialEgressHTTPTLSProfile(),
		"WebSocket": newOpenAIOfficialEgressWebSocketTLSProfile(),
	} {
		require.Containsf(
			t,
			tlsProfile.Name,
			nextVersion,
			"%s 传输画像没有跟随 release 指针，仍绑定编译期版本",
			name,
		)
	}
}

// TestOfficialCodexFeatureDefaultsFollowSwitchedRelease 锁定 feature 默认值跟随 release
// 指针。
//
// OfficialCodexRemoteCompactionV2Default 决定 handler 的压缩分派与 HTTP turn metadata。
// 它此前直接解析编译期常量版本：切换 active release 后，header、TLS、端点已按新画像
// 出站，而压缩路由仍读旧画像默认值，形成跨层版本分叉——这类分叉比单点落后更难定位，
// 因为出站形态本身看不出矛盾。
func TestOfficialCodexFeatureDefaultsFollowSwitchedRelease(t *testing.T) {
	const nextVersion = "0.146.0"
	require.True(
		t,
		OfficialCodexRemoteCompactionV2Default(),
		"前置条件：现役画像的 RemoteCompactionV2 应为 true",
	)

	// 合成画像刻意翻转该 feature：只有真正读取 active 画像才会观察到变化。
	registerSyntheticCodexRelease(t, nextVersion, func(profile *officialCodexVersionProfile) {
		profile.FeatureDefaults.RemoteCompactionV2 = false
	})

	activeProfile, err := resolveOfficialClientProfile(
		officialClientPurposeOpenAIOAuthResponsesHTTP,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	buildID := activeProfile.Build.ID
	originalBuild := defaultOfficialClientProfileRegistry.builds[buildID]

	switchedBuild := originalBuild
	switchedBuild.Version = nextVersion
	defaultOfficialClientProfileRegistry.builds[buildID] = switchedBuild

	t.Cleanup(func() {
		defaultOfficialClientProfileRegistry.builds[buildID] = originalBuild
	})

	require.False(
		t,
		OfficialCodexRemoteCompactionV2Default(),
		"feature 默认值没有跟随 release 指针，压缩分派仍绑定编译期版本",
	)
}
