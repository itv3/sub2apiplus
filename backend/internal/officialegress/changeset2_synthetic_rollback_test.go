package officialegress

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/url"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/releasecontract"
)

// 合成异版本目录用的目标坐标。它必须与正式 previous 的 0.147.0 不同，
// 否则「异版本」会退化成同版本，三坐标里的 version 一维不再分离。
const (
	syntheticHigherVersion           = "0.149.0"
	syntheticHigherVersionUnderscore = "0_149_0"
)

// syntheticChangeset2RollbackSource 按结构事实为合成回滚矩阵选择底稿画像：按 Active、Previous 的顺序取第一个
// 声明了回滚矩阵各 Sink 全部 route 的正式发布；两者都不完整声明时，退回按冻结合同原地保留的 0.154.0 画像。
//
// 合成目录的 active 与 previous 都派生自同一份底稿画像，版本 route 的“两槽位并集覆盖”在合成目录里不存在；
// 底稿缺少任一 route（例如晋升后 Active 的目标画像删掉了 codex.responses.forward 仍登记的 legacy compact
// route），BundleResolver 就会以“route 在 Active/Previous 画像中均无 EndpointBinding”拒绝构造。候选期 Active
// 即满足条件，选择结果与改动前固定取 Active 完全相同；0.157.0 晋升后取 Previous 中的 0.154.0；legacy compact
// 发布退役之后（0.154.0 离开 Active／Previous）两个正式发布都不声明它，本矩阵冻结的正是 legacy compact 仍在时的
// 发布事实，底稿取运行画像目录里保留的同一份 0.154.0 画像（不经快照目录，VC-6 退休裁剪快照目录后照样可读）。
func syntheticChangeset2RollbackSource(t *testing.T, bindings []SinkBinding) profilecontract.SnapshotCatalogEntry {
	t.Helper()
	physical, err := NewPhysicalRouteCatalog(DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	declaresAll := func(profile profilecontract.ExecutableProfile) bool {
		for _, binding := range bindings {
			for _, route := range binding.Routes() {
				if !staticClosureProfileDeclaresRoute(t, profile, physical, binding.ID(), route) {
					return false
				}
			}
		}
		return true
	}
	base := DefaultReleaseCatalog()
	for _, mode := range []ReleaseMode{ReleaseModeActive, ReleaseModePrevious} {
		release, resolveErr := base.Resolve(mode)
		if resolveErr != nil {
			t.Fatal(resolveErr)
		}
		if !declaresAll(release.ExecutableProfile()) {
			continue
		}
		for _, entry := range base.snapshots.ToDoc().Snapshots {
			if entry.Version == release.Version() && entry.Digest == release.ProfileDigest() {
				return entry
			}
		}
		t.Fatalf("正式 %s snapshot entry 缺失", mode)
	}
	retained, err := loadReleaseRouteRetirementProfile(releaseCatalogFS, releaseRouteRetirementProfile{
		Version: legacyCompactProfileVersion, Digest: legacyCompactProfileDigest,
	})
	if err != nil {
		t.Fatal(err)
	}
	if !declaresAll(retained) {
		t.Fatal("正式 Active/Previous 与保留的 0.154.0 画像都未完整声明回滚矩阵各 Sink 的 route：合成回滚矩阵缺少底稿")
	}
	return profilecontract.SnapshotCatalogEntry{
		Version: legacyCompactProfileVersion,
		Digest:  legacyCompactProfileDigest,
		File:    "profiles/" + legacyCompactProfileVersion + "/" + legacyCompactProfileDigest + ".json",
	}
}

// syntheticChangeset2ReleaseCatalog 以 sourceEntry 指向的真实画像为底稿构造同版本合成回滚目录：
// active 节点引用底稿原样，previous 节点引用底稿的变体（字段序、TLS、feature 与 header 事实
// 均被改动），两个节点的发布坐标都对齐到底稿版本。sourceEntry 由
// syntheticChangeset2RollbackSource 按结构事实给出。
func syntheticChangeset2ReleaseCatalog(t *testing.T, sourceEntry profilecontract.SnapshotCatalogEntry) ReleaseCatalog {
	t.Helper()
	base := DefaultReleaseCatalog()
	graphDoc := base.graph.ToDoc()
	activeRaw, err := releaseCatalogFS.ReadFile("catalogdata/runtime/" + sourceEntry.File)
	if err != nil {
		t.Fatal(err)
	}
	previousDoc, err := profilecontract.ParseSnapshot(activeRaw)
	if err != nil {
		t.Fatal(err)
	}
	previousDoc.FeatureDefaults.RemoteCompactionV2 = !previousDoc.FeatureDefaults.RemoteCompactionV2
	previousDoc.FeatureDefaults.RequestCompressionLevel++
	for transportIndex := range previousDoc.Transports {
		transport := &previousDoc.Transports[transportIndex]
		if len(transport.CipherSuites) > 1 {
			transport.CipherSuites[0], transport.CipherSuites[1] =
				transport.CipherSuites[1], transport.CipherSuites[0]
		}
		// 生命周期与 WS 压缩组合已由变更集 4 编译器封闭；合成回滚只改变
		// 仍属于支持闭集的 TLS、feature、header 与 body 发布事实。
	}
	longLivedTransportID := ""
	for _, endpoint := range previousDoc.Endpoints {
		if endpoint.ClientLifecycle == string(profilecontract.LifecycleBackendClientLongLived) {
			longLivedTransportID = endpoint.TransportID
			break
		}
	}
	if longLivedTransportID == "" {
		t.Fatal("正式画像缺少长期 HTTP transport")
	}
	for endpointIndex := range previousDoc.Endpoints {
		endpoint := &previousDoc.Endpoints[endpointIndex]
		for left, right := 0, len(endpoint.Body.Fields)-1; left < right; left, right = left+1, right-1 {
			endpoint.Body.Fields[left], endpoint.Body.Fields[right] =
				endpoint.Body.Fields[right], endpoint.Body.Fields[left]
		}
		endpoint.Headers = append(endpoint.Headers, profilecontract.SnapshotHeaderSlot{
			Slot: 999, Sequence: 0, Name: "x-synthetic-release",
			WireName: "x-synthetic-release", Value: "previous",
			Source: "constant", Condition: "always",
		})
		if endpoint.ID == "alpha_search" || endpoint.ID == "oauth_refresh" {
			endpoint.ClientLifecycle = string(profilecontract.LifecycleBackendClientLongLived)
			endpoint.TransportID = longLivedTransportID
		}
	}
	previousDoc.Digest = ""
	digestInput, err := json.Marshal(previousDoc)
	if err != nil {
		t.Fatal(err)
	}
	digestSum := sha256.Sum256(digestInput)
	previousDoc.Digest = hex.EncodeToString(digestSum[:])
	previousRaw, err := json.Marshal(previousDoc)
	if err != nil {
		t.Fatal(err)
	}
	previousFile := fmt.Sprintf(
		"snapshots/%s/%s.json",
		previousDoc.Version,
		previousDoc.Digest,
	)
	snapshotDoc := profilecontract.SnapshotCatalogDoc{
		SchemaVersion: profilecontract.SnapshotCatalogSchemaVersion,
		Snapshots: []profilecontract.SnapshotCatalogEntry{
			sourceEntry,
			{Version: previousDoc.Version, Digest: previousDoc.Digest, File: previousFile},
		},
	}
	snapshots, err := profilecontract.NewSnapshotCatalog(
		snapshotDoc,
		func(path string) ([]byte, error) {
			switch path {
			case sourceEntry.File:
				return append([]byte(nil), activeRaw...), nil
			case previousFile:
				return append([]byte(nil), previousRaw...), nil
			default:
				return nil, fmt.Errorf("未知合成 snapshot: %s", path)
			}
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	// 合成的 previous 画像是底稿的变体，版本与底稿相同——本函数要验证的是「同版本回滚时
	// active/previous 的事实不串」。升级期间正式 previous 的 Build 已指向目标版本，若只替换
	// Snapshot 而不动 Build，节点内部就会出现「快照版本与 Build 版本不一致」，故把 previous
	// 节点的发布坐标一并降回底稿版本。晋升后底稿取自 Previous，Active 节点的 Build 指向目标
	// 版本，按同一规则让 active 节点引用底稿原样并降回底稿版本；候选期底稿即 Active，active
	// 节点的快照与 Build 版本本就等于底稿，下面的循环对它不做任何改动，与改动前一致。
	downgrade := strings.NewReplacer(
		syntheticHigherVersion, previousDoc.Version,
		syntheticHigherVersionUnderscore, strings.ReplaceAll(previousDoc.Version, ".", "_"),
	)
	for nodeIndex := range graphDoc.Nodes {
		node := &graphDoc.Nodes[nodeIndex]
		switch node.Mode {
		case releasecontract.ReleaseModePrevious:
			node.Snapshot = releasecontract.SnapshotReferenceDoc{
				Version: previousDoc.Version,
				Digest:  previousDoc.Digest,
			}
		case releasecontract.ReleaseModeActive:
			node.Snapshot = releasecontract.SnapshotReferenceDoc{
				Version: sourceEntry.Version,
				Digest:  sourceEntry.Digest,
			}
		default:
			continue
		}
		if node.Build.Version == previousDoc.Version {
			continue
		}
		stale := strings.NewReplacer(
			node.Build.Version, previousDoc.Version,
			strings.ReplaceAll(node.Build.Version, ".", "_"),
			strings.ReplaceAll(previousDoc.Version, ".", "_"),
		)
		node.Build.ID = stale.Replace(downgrade.Replace(node.Build.ID))
		node.Build.UserAgent = stale.Replace(downgrade.Replace(node.Build.UserAgent))
		node.Build.Source = "synthetic:rollback-same-version"
		node.Build.Version = previousDoc.Version
		node.Wire.ID = stale.Replace(downgrade.Replace(node.Wire.ID))
		node.Wire.TransportProfileID = stale.Replace(
			downgrade.Replace(node.Wire.TransportProfileID),
		)
		node.Wire.BuildID = node.Build.ID
		node.Wire.Source = "synthetic:rollback-same-version"
		node.Wire.Digest = syntheticChangeset2RegistryDigest(t, node.Build, node.Wire)
	}
	graph, err := releasecontract.NewReleaseGraph(graphDoc)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := newReleaseCatalog(graph, snapshots, "synthetic-changeset2", "")
	if err != nil {
		t.Fatal(err)
	}
	return catalog
}

// syntheticChangeset2MixedVersionReleaseCatalog 构造只用于门禁自测的异版本目录。
// 它完整替换画像中的行为版本坐标，让 active 指向合成版本、previous 保留
// 正式发布图的取值。合成版本刻意避开当前 previous 的 0.147.0，否则三坐标里的
// version 一维不再分离，本测试要验证的「异版本三坐标全部分离」会失去意义。
// 该目录不写入 runtime，也不得作为正式画像或取证结果。
func syntheticChangeset2MixedVersionReleaseCatalog(t *testing.T) ReleaseCatalog {
	t.Helper()
	base := DefaultReleaseCatalog()
	graphDoc := base.graph.ToDoc()
	baselineNode, ok := base.graph.Resolve(
		RegistryPurposeOpenAIOAuthHTTP,
		releasecontract.ReleaseModePrevious,
	)
	if !ok {
		t.Fatal("正式 previous HTTP release 缺失")
	}
	baselineVersion := baselineNode.Snapshot.Version
	if baselineVersion == syntheticHigherVersion {
		t.Fatal("正式 previous 与合成版本相同")
	}
	var sourceEntry profilecontract.SnapshotCatalogEntry
	for _, entry := range base.snapshots.ToDoc().Snapshots {
		if entry.Version == baselineNode.Snapshot.Version &&
			entry.Digest == baselineNode.Snapshot.Digest {
			sourceEntry = entry
			break
		}
	}
	if sourceEntry.File == "" {
		t.Fatal("正式 active snapshot entry 缺失")
	}
	baselineRaw, err := releaseCatalogFS.ReadFile("catalogdata/runtime/" + sourceEntry.File)
	if err != nil {
		t.Fatal(err)
	}
	targetRaw := []byte(strings.ReplaceAll(string(baselineRaw), baselineVersion, syntheticHigherVersion))
	if strings.Contains(string(targetRaw), baselineVersion) {
		t.Fatalf("异版本合成画像仍残留 %s 行为坐标", baselineVersion)
	}
	var targetDoc profilecontract.SnapshotDoc
	if err := json.Unmarshal(targetRaw, &targetDoc); err != nil {
		t.Fatal(err)
	}
	targetDoc.Digest = ""
	digestInput, err := json.Marshal(targetDoc)
	if err != nil {
		t.Fatal(err)
	}
	targetSum := sha256.Sum256(digestInput)
	targetDoc.Digest = hex.EncodeToString(targetSum[:])
	targetRaw, err = json.Marshal(targetDoc)
	if err != nil {
		t.Fatal(err)
	}
	targetFile := fmt.Sprintf(
		"snapshots/%s/%s.json",
		targetDoc.Version,
		targetDoc.Digest,
	)
	// 本合成 catalog 只改写 Active 节点，Previous 节点原样保留正式发布图的取值。
	// 升级期间正式 previous 已指向真实 0.147 画像（§7.1 的候选阶段状态：active=0.145、
	// previous=目标版本），若 snapshots 里只放 baseline 与合成画像，Previous 引用的那份
	// 就查不到，newReleaseCatalog 会以「release mode previous 引用了未知 Snapshot」失败。
	// 故把发布图里所有非 Active 节点引用的真实 snapshot 一并纳入，保持合成 catalog 自洽。
	carried := map[profilecontract.SnapshotKey][]byte{}
	baseSnapshotDoc := base.snapshots.ToDoc()
	for _, node := range graphDoc.Nodes {
		if node.Mode == releasecontract.ReleaseModeActive {
			continue
		}
		key := profilecontract.SnapshotKey{
			Version: node.Snapshot.Version, Digest: node.Snapshot.Digest,
		}
		if key.Version == sourceEntry.Version && key.Digest == sourceEntry.Digest {
			continue
		}
		if _, ok := carried[key]; ok {
			continue
		}
		var entry profilecontract.SnapshotCatalogEntry
		for _, candidate := range baseSnapshotDoc.Snapshots {
			if candidate.Version == key.Version && candidate.Digest == key.Digest {
				entry = candidate
				break
			}
		}
		if entry.File == "" {
			t.Fatalf("正式 previous snapshot entry 缺失：%s/%s", key.Version, key.Digest)
		}
		raw, err := releaseCatalogFS.ReadFile("catalogdata/runtime/" + entry.File)
		if err != nil {
			t.Fatal(err)
		}
		carried[key] = raw
	}

	snapshotEntries := []profilecontract.SnapshotCatalogEntry{
		sourceEntry,
		{Version: targetDoc.Version, Digest: targetDoc.Digest, File: targetFile},
	}
	carriedFiles := map[string][]byte{}
	for key, raw := range carried {
		for _, candidate := range baseSnapshotDoc.Snapshots {
			if candidate.Version == key.Version && candidate.Digest == key.Digest {
				snapshotEntries = append(snapshotEntries, candidate)
				carriedFiles[candidate.File] = raw
				break
			}
		}
	}
	snapshotDoc := profilecontract.SnapshotCatalogDoc{
		SchemaVersion: profilecontract.SnapshotCatalogSchemaVersion,
		Snapshots:     snapshotEntries,
	}
	snapshots, err := profilecontract.NewSnapshotCatalog(
		snapshotDoc,
		func(path string) ([]byte, error) {
			switch path {
			case sourceEntry.File:
				return append([]byte(nil), baselineRaw...), nil
			case targetFile:
				return append([]byte(nil), targetRaw...), nil
			default:
				if raw, ok := carriedFiles[path]; ok {
					return append([]byte(nil), raw...), nil
				}
				return nil, fmt.Errorf("未知异版本合成 snapshot: %s", path)
			}
		},
	)
	if err != nil {
		t.Fatal(err)
	}
	for index := range graphDoc.Nodes {
		node := &graphDoc.Nodes[index]
		if node.Mode != releasecontract.ReleaseModeActive {
			continue
		}
		nodeVersion := node.Build.Version
		replacer := strings.NewReplacer(
			nodeVersion, syntheticHigherVersion,
			strings.ReplaceAll(nodeVersion, ".", "_"), syntheticHigherVersionUnderscore,
		)
		node.Build.ID = replacer.Replace(node.Build.ID)
		node.Build.Version = targetDoc.Version
		node.Build.UserAgent = replacer.Replace(node.Build.UserAgent)
		node.Build.Source = "synthetic:" + nodeVersion + "-to-" + syntheticHigherVersion
		node.Wire.ID = replacer.Replace(node.Wire.ID)
		node.Wire.BuildID = node.Build.ID
		node.Wire.TransportProfileID = replacer.Replace(node.Wire.TransportProfileID)
		node.Wire.Source = "synthetic:" + nodeVersion + "-to-" + syntheticHigherVersion
		node.Snapshot = releasecontract.SnapshotReferenceDoc{
			Version: targetDoc.Version,
			Digest:  targetDoc.Digest,
		}
		node.Wire.Digest = syntheticChangeset2RegistryDigest(t, node.Build, node.Wire)
	}
	graph, err := releasecontract.NewReleaseGraph(graphDoc)
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := newReleaseCatalog(graph, snapshots, "synthetic-mixed-version", "")
	if err != nil {
		t.Fatal(err)
	}
	return catalog
}

func syntheticChangeset2RegistryDigest(
	t *testing.T,
	build releasecontract.ReleaseBuildDoc,
	wire releasecontract.ReleaseWireDoc,
) string {
	t.Helper()
	type registryHeader struct {
		Name  string `json:"name"`
		Value string `json:"value"`
	}
	type registryBuild struct {
		ID             string           `json:"id"`
		Provider       string           `json:"provider"`
		Product        string           `json:"product"`
		Surface        string           `json:"surface"`
		Version        string           `json:"version"`
		UserAgent      string           `json:"user_agent"`
		Originator     string           `json:"originator,omitempty"`
		RuntimeHeaders []registryHeader `json:"runtime_headers,omitempty"`
		Source         string           `json:"source"`
	}
	type registryWire struct {
		ID                 string           `json:"id"`
		Purpose            string           `json:"purpose"`
		BuildID            string           `json:"build_id"`
		AuthMode           string           `json:"auth_mode"`
		Endpoint           string           `json:"endpoint"`
		Transport          string           `json:"transport"`
		NetworkVariant     string           `json:"network_variant"`
		StaticHeaders      []registryHeader `json:"static_headers,omitempty"`
		BetaHeader         string           `json:"beta_header,omitempty"`
		TransportProfileID string           `json:"transport_profile_id"`
		Source             string           `json:"source"`
		Digest             string           `json:"digest"`
	}
	toHeaders := func(values []releasecontract.HeaderValueDoc) []registryHeader {
		result := make([]registryHeader, len(values))
		for index, value := range values {
			result[index] = registryHeader(value)
		}
		return result
	}
	payload := struct {
		Build   registryBuild `json:"build"`
		Profile registryWire  `json:"profile"`
	}{
		Build: registryBuild{
			ID: build.ID, Provider: build.Provider, Product: build.Product,
			Surface: build.Surface, Version: build.Version, UserAgent: build.UserAgent,
			Originator: build.Originator, RuntimeHeaders: toHeaders(build.RuntimeHeaders),
			Source: build.Source,
		},
		Profile: registryWire{
			ID: wire.ID, Purpose: wire.Purpose, BuildID: wire.BuildID,
			AuthMode: wire.AuthMode, Endpoint: wire.Endpoint, Transport: wire.Transport,
			NetworkVariant: wire.NetworkVariant, StaticHeaders: toHeaders(wire.StaticHeaders),
			BetaHeader: wire.BetaHeader, TransportProfileID: wire.TransportProfileID,
			Source: wire.Source, Digest: "",
		},
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}

func compileSyntheticChangeset2Endpoint(
	t *testing.T,
	bundle ReleaseBundle,
	sinkID SinkID,
	endpointID string,
	method string,
	rawURL string,
	protocol WireProtocol,
	body string,
) CompiledExecution {
	t.Helper()
	target, err := url.Parse(rawURL)
	if err != nil {
		t.Fatal(err)
	}
	binding, ok := DefaultSinkCatalog().Resolve(sinkID)
	if !ok {
		t.Fatalf("SinkBinding 缺失: %s", sinkID)
	}
	authenticationInput := AttemptAuthenticationInput{BearerToken: "synthetic-rollback-token"}
	semanticBody := body
	if endpointID == "oauth_refresh" {
		authenticationInput.RefreshToken = "synthetic-refresh-token"
		semanticBody = `{"client_id":"c","grant_type":"refresh_token"}`
	}
	authentication, err := NewAttemptAuthentication(authenticationInput)
	if err != nil {
		t.Fatal(err)
	}
	identityFacts := executorInvocationIdentityFacts(t)
	identityFacts.Conditions.BetaFeaturesPresent = true
	routingHint := CodexRoutingHintFacts{}
	if officialCodexRoutingHintEndpoint(endpointID) {
		routingHint, err = ParseOfficialCodexRoutingHintFacts(endpointID, []byte(semanticBody))
		if err != nil {
			t.Fatal(err)
		}
	}
	execution, err := NewCompiler().Compile(context.Background(), bundle, CodexEgressPlan{
		SinkID: sinkID, Purpose: binding.Purpose(), EndpointID: endpointID,
		InvocationID: "changeset2-synthetic-" + string(bundle.Mode()) + "-" + endpointID,
		Mode:         bundle.Mode(), Protocol: protocol, Method: method, URL: target,
		Headers: make(http.Header), IdentityMode: IdentityCodexOAuthStrict,
		IdentityFacts:  identityFacts,
		Authentication: authentication,
		HeaderPolicy:   HeaderPolicy{ID: "synthetic.rollback.headers", Source: "test"},
		BodyPolicy:     BodyPolicy{ID: "synthetic.rollback.body", Source: "test"},
		RoutingHint:    routingHint,
		BehaviorPolicy: bundle.Behavior(), Body: NewReplayableRequestBody([]byte(semanticBody)),
		DeclaredPersona: PersonaCodexCLI,
	}, EndpointDynamicInputs{})
	if err != nil {
		t.Fatalf("编译 %s/%s：%v", bundle.Mode(), endpointID, err)
	}
	return execution
}

func TestChangeset2SyntheticProfileRollbackMatrixHasNoMixedFacts(t *testing.T) {
	// 本测试冻结的是版本 route 出现前的 0.145 合成回滚矩阵，只保留本矩阵
	// 实际覆盖的四个 Sink，避免把 0.147 独有端点伪造进历史画像。
	var rollbackInputs []SinkBindingInput
	var rollbackBindings []SinkBinding
	for _, sinkID := range []SinkID{
		SinkCodexResponsesForward, SinkCodexResponsesWS,
		SinkCodexAlphaSearchDirect, SinkCodexOAuthRefresh,
	} {
		binding, ok := DefaultSinkCatalog().Resolve(sinkID)
		if !ok {
			t.Fatalf("合成回滚矩阵缺少 Sink：%s", sinkID)
		}
		input := sinkBindingInputForVersionRoute(binding)
		input.migrationReceipt = binding.migrationReceipt
		rollbackInputs = append(rollbackInputs, input)
		rollbackBindings = append(rollbackBindings, binding)
	}
	// 同理，底稿画像也不伪造、不删减任何 route：按结构事实选出完整声明这四个 Sink 全部
	// route 的真实画像作为底稿（改动前固定取 Active；晋升后 Active 的目标画像删掉了
	// codex.responses.forward 仍登记的 legacy compact route，底稿改取 Previous 中的旧画像；
	// legacy compact 发布退役后两个发布都不声明它，底稿取运行画像目录里保留的 0.154.0 画像）。
	catalog := syntheticChangeset2ReleaseCatalog(t, syntheticChangeset2RollbackSource(t, rollbackBindings))
	rollbackSinks, err := NewSinkCatalog(rollbackInputs)
	if err != nil {
		t.Fatal(err)
	}
	resolver, err := NewBundleResolver(catalog, rollbackSinks)
	if err != nil {
		t.Fatal(err)
	}
	resolve := func(mode ReleaseMode, sinkID SinkID) ReleaseBundle {
		request := changeset2BundleRequest(sinkID)
		request.Mode = mode
		bundle, resolveErr := resolver.Resolve(request)
		if resolveErr != nil {
			t.Fatal(resolveErr)
		}
		return bundle
	}
	active := resolve(ReleaseModeActive, SinkCodexResponsesForward)
	previous := resolve(ReleaseModePrevious, SinkCodexResponsesForward)
	if active.ProfileDigest() == previous.ProfileDigest() ||
		active.ReleaseDigest() == previous.ReleaseDigest() {
		t.Fatal("合成 active/previous Profile 或 Release 被折叠")
	}
	profileOrder := func(bundle ReleaseBundle, endpointID string) []string {
		for _, endpoint := range bundle.Release().Profile().Endpoints() {
			if endpoint.ID == endpointID {
				out := make([]string, 0, len(endpoint.Body.Fields))
				for _, field := range endpoint.Body.Fields {
					out = append(out, field.Name)
				}
				return out
			}
		}
		return nil
	}
	if fmt.Sprint(profileOrder(active, "responses_http")) ==
		fmt.Sprint(profileOrder(previous, "responses_http")) {
		t.Fatalf(
			"合成 ProfileSpec Body 顺序未形成差异：active=%v previous=%v",
			profileOrder(active, "responses_http"),
			profileOrder(previous, "responses_http"),
		)
	}

	type endpointCase struct {
		sink                     SinkID
		id, method, target, body string
		protocol                 WireProtocol
	}
	cases := []endpointCase{
		{SinkCodexResponsesForward, "responses_http", http.MethodPost,
			"https://chatgpt.com/backend-api/codex/responses",
			`{"model":"m","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`,
			WireProtocolHTTP},
		{SinkCodexResponsesWS, "responses_ws", http.MethodGet,
			"wss://chatgpt.com/backend-api/codex/responses",
			`{"type":"response.create","model":"m","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`,
			WireProtocolWebSocket},
		{SinkCodexAlphaSearchDirect, "alpha_search", http.MethodPost,
			"https://chatgpt.com/backend-api/codex/alpha/search",
			`{"id":"i","model":"m","input":[],"commands":[],"settings":{},"max_output_tokens":1}`,
			WireProtocolHTTP},
		{SinkCodexOAuthRefresh, "oauth_refresh", http.MethodPost,
			"https://auth.openai.com/oauth/token",
			`{"client_id":"c","grant_type":"refresh_token","refresh_token":"r"}`,
			WireProtocolHTTP},
	}
	for _, tc := range cases {
		active = resolve(ReleaseModeActive, tc.sink)
		previous = resolve(ReleaseModePrevious, tc.sink)
		target, parseErr := url.Parse(tc.target)
		if parseErr != nil {
			t.Fatal(parseErr)
		}
		activePlan, planErr := active.ResolveEndpointPlan(tc.sink, tc.method, target, tc.protocol)
		if planErr != nil {
			t.Fatal(planErr)
		}
		previousPlan, planErr := previous.ResolveEndpointPlan(tc.sink, tc.method, target, tc.protocol)
		if planErr != nil {
			t.Fatal(planErr)
		}
		if fmt.Sprint(activePlan.template.endpoint.Body.Fields) ==
			fmt.Sprint(previousPlan.template.endpoint.Body.Fields) {
			t.Fatalf("%s EndpointPlan 未冻结不同 Body 契约", tc.id)
		}
		activeOrdered, orderErr := orderJSONBody(
			[]byte(tc.body), activePlan.template.endpoint.Body,
		)
		if orderErr != nil {
			t.Fatal(orderErr)
		}
		previousOrdered, orderErr := orderJSONBody(
			[]byte(tc.body), previousPlan.template.endpoint.Body,
		)
		if orderErr != nil {
			t.Fatal(orderErr)
		}
		if string(activeOrdered) == string(previousOrdered) {
			t.Fatalf(
				"%s BodyContract 未产生不同字节：active=%v previous=%v",
				tc.id,
				activePlan.template.endpoint.Body.Fields,
				previousPlan.template.endpoint.Body.Fields,
			)
		}
		activeExecution := compileSyntheticChangeset2Endpoint(
			t, active, tc.sink, tc.id, tc.method, tc.target, tc.protocol, tc.body,
		)
		previousExecution := compileSyntheticChangeset2Endpoint(
			t, previous, tc.sink, tc.id, tc.method, tc.target, tc.protocol, tc.body,
		)
		if tc.id == "responses_http" || tc.id == "responses_ws" {
			if activeExecution.request.Headers().Get("x-codex-beta-features") != "remote_compaction_v2" ||
				previousExecution.request.Headers().Get("x-codex-beta-features") != "" {
				t.Fatalf("%s 使用了 Bundle 外 feature 或丢失 active feature", tc.id)
			}
		}
		activeBody, _ := activeExecution.request.Body().ReplayableBytes()
		previousBody, _ := previousExecution.request.Body().ReplayableBytes()
		if string(activeBody) == string(previousBody) {
			t.Fatalf(
				"%s 未体现不同 Body 字段序：active=%s previous=%s",
				tc.id, activeBody, previousBody,
			)
		}
		if previousExecution.request.Headers().Get("x-synthetic-release") != "previous" ||
			activeExecution.request.Headers().Get("x-synthetic-release") != "" {
			t.Fatalf("%s Header 发布事实混搭", tc.id)
		}
		if activeExecution.transport.ProfileDigest == previousExecution.transport.ProfileDigest ||
			activeExecution.transport.ConnectionPoolDigest == previousExecution.transport.ConnectionPoolDigest ||
			activeExecution.transport.TLS.CipherSuites[0] == previousExecution.transport.TLS.CipherSuites[0] {
			t.Fatalf("%s TransportSpec/TLS/PoolDigest 未随 Profile 回滚", tc.id)
		}
		if (tc.id == "alpha_search" || tc.id == "oauth_refresh") &&
			activeExecution.transport.ResourceLifecycle == previousExecution.transport.ResourceLifecycle {
			t.Fatalf("%s 连接生命周期未随 Profile 回滚", tc.id)
		}
	}
	if active.Release().Profile().Features() == previous.Release().Profile().Features() {
		t.Fatal("合成 feature defaults 未形成真实差异")
	}
}
