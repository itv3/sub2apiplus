package officialegress

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"io/fs"
	"path"
	"slices"
	"strings"
	"testing"
	"testing/fstest"
)

// claudePinTestSyntheticVersion 是只在内存目录里出现的合成候选 Release 版本，用来证明历史锚点不随
// validation_candidate 移动。
const claudePinTestSyntheticVersion = "2.1.999"

// TestClaudeReleaseCatalogReleaseByDigestMatchesSelectors 证明按摘要取回的 Release 与两个 selector 解析的
// 结果逐字段相同，格式非法与未知摘要一律失败关闭。
func TestClaudeReleaseCatalogReleaseByDigestMatchesSelectors(t *testing.T) {
	catalog, err := LoadEmbeddedClaudeReleaseCatalog()
	if err != nil {
		t.Fatal(err)
	}
	for _, selection := range []ResolvedClaudeReleaseSelection{catalog.ValidationCandidate(), catalog.ProductionActive()} {
		want := selection.Release()
		got, err := catalog.ReleaseByDigest(want.ReleaseDigest())
		if err != nil {
			t.Fatal(err)
		}
		assertClaudePinTestReleaseEqual(t, got, want)
	}
	for _, digest := range []string{
		"",
		strings.Repeat("0", 64),
		strings.ToUpper(claudeFWGHistoricalReleaseDigest),
		claudeFWGHistoricalReleaseDigest[:63],
		" " + claudeFWGHistoricalReleaseDigest,
	} {
		if _, err := catalog.ReleaseByDigest(digest); err == nil {
			t.Fatalf("非法或未知 Claude Release 摘要没有失败关闭：%q", digest)
		}
	}
}

// TestClaudeFWGHistoricalAnchorsArePinned 证明全部 ClaudeFWG* 历史名称与两个历史加载函数都从钉住的历史
// Release 派生，路由目录用的加载函数则跟随 validation_candidate。
func TestClaudeFWGHistoricalAnchorsArePinned(t *testing.T) {
	release, err := claudeFWGHistoricalReleaseFrom(DefaultClaudeReleaseCatalog())
	if err != nil {
		t.Fatal(err)
	}
	if release.ReleaseDigest() != claudeFWGHistoricalReleaseDigest ||
		ClaudeFWGVersion != release.Version() || ClaudeFWGProfileDigest != release.ProfileDigest() ||
		ClaudeFWGReleaseDigest != release.ReleaseDigest() || ClaudeFWGBundleDigest != release.BundleDigest() ||
		claudeFWGWireDigest != release.WireDigest() || ClaudeFWGWireDigest() != release.WireDigest() {
		t.Fatal("Claude FW-G 历史锚点没有钉住历史 Release")
	}
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	if profile.document.Identity.Version != release.Version() || wire.Identity.ProfileDigest != release.ProfileDigest() {
		t.Fatal("Claude FW-G 历史加载函数没有加载钉住的 Release")
	}
	candidate, err := loadClaudeValidationCandidateProfile()
	if err != nil {
		t.Fatal(err)
	}
	selected := DefaultClaudeReleaseCatalog().ValidationCandidate().Release()
	if candidate.document.Identity.Version != selected.Version() ||
		len(candidate.endpoints) != selected.StrictEndpointCount() {
		t.Fatal("路由目录用的 Claude Profile 没有跟随 validation_candidate")
	}
}

// TestClaudeFWGHistoricalAnchorsIgnoreCandidateSelector 在内存目录里把 validation_candidate 切到一条合成
// Release：候选 selector 跟着走，钉住的历史 Release 不动；目录里去掉历史 Release 时按摘要取失败关闭。
func TestClaudeFWGHistoricalAnchorsIgnoreCandidateSelector(t *testing.T) {
	catalogFS, synthetic := claudePinTestCatalogFS(t, true)
	catalog, err := loadClaudeReleaseCatalogFromFS(catalogFS)
	if err != nil {
		t.Fatal(err)
	}
	candidate := catalog.ValidationCandidate().Release()
	if candidate.ReleaseDigest() != synthetic || candidate.Version() != claudePinTestSyntheticVersion ||
		catalog.ProductionActive().Release().ReleaseDigest() != claudeFWGHistoricalReleaseDigest {
		t.Fatal("合成目录的 selector 没有按预期解析")
	}
	pinned, err := claudeFWGHistoricalReleaseFrom(catalog)
	if err != nil {
		t.Fatal(err)
	}
	if pinned.ReleaseDigest() != ClaudeFWGReleaseDigest || pinned.Version() != ClaudeFWGVersion ||
		pinned.ProfileDigest() != ClaudeFWGProfileDigest || pinned.WireDigest() != claudeFWGWireDigest ||
		pinned.BundleDigest() != ClaudeFWGBundleDigest {
		t.Fatal("validation_candidate 换版后历史锚点跟着移动")
	}
	profile, err := loadClaudeProfile(candidate)
	if err != nil {
		t.Fatal(err)
	}
	if profile.document.Identity.Version != claudePinTestSyntheticVersion {
		t.Fatal("候选 Profile 没有跟随 validation_candidate 换版")
	}

	withoutPinned, synthetic := claudePinTestCatalogFS(t, false)
	catalog, err = loadClaudeReleaseCatalogFromFS(withoutPinned)
	if err != nil {
		t.Fatal(err)
	}
	if catalog.ValidationCandidate().Release().ReleaseDigest() != synthetic {
		t.Fatal("去掉历史 Release 的合成目录 selector 没有按预期解析")
	}
	if _, err := claudeFWGHistoricalReleaseFrom(catalog); err == nil || !strings.Contains(err.Error(), "历史 Release 不可用") {
		t.Fatalf("目录缺少历史 Release 时没有失败关闭：%v", err)
	}
}

func assertClaudePinTestReleaseEqual(t *testing.T, got, want ResolvedClaudeRelease) {
	t.Helper()
	if got.Version() != want.Version() || got.Platform() != want.Platform() ||
		got.ProfileDigest() != want.ProfileDigest() || got.WireDigest() != want.WireDigest() ||
		got.ReleaseDigest() != want.ReleaseDigest() || got.BundleDigest() != want.BundleDigest() ||
		got.RequiredRuleCount() != want.RequiredRuleCount() ||
		got.ProfileAtomicAssertionCount() != want.ProfileAtomicAssertionCount() ||
		got.LocalScenarioAssertionCount() != want.LocalScenarioAssertionCount() ||
		got.StrictEndpointCount() != want.StrictEndpointCount() ||
		!slices.Equal(got.Models(), want.Models()) ||
		!bytes.Equal(got.profileRaw, want.profileRaw) || !bytes.Equal(got.wireRaw, want.wireRaw) {
		t.Fatalf("按摘要取回的 Claude Release 与 selector 解析结果不一致：%s", want.ReleaseDigest())
	}
}

// claudePinTestCatalogFS 复制内嵌的 Claude 目录，追加一条只换版本号的合成 Release（Profile、Wire 与
// Release、Bundle 摘要全部按加载器同一算法重算），并把 validation_candidate 切过去。keepPinned 为假时去掉
// 钉住的历史 Release，production_active 也改指合成 Release（沿用原批准文件）。返回内存目录与合成 Release 摘要。
func claudePinTestCatalogFS(t *testing.T, keepPinned bool) (fstest.MapFS, string) {
	t.Helper()
	files := fstest.MapFS{}
	if err := fs.WalkDir(claudeReleaseCatalogFS, "catalogdata/claude", func(name string, entry fs.DirEntry, walkErr error) error {
		if walkErr != nil || entry.IsDir() {
			return walkErr
		}
		raw, err := fs.ReadFile(claudeReleaseCatalogFS, name)
		if err != nil {
			return err
		}
		files[name] = &fstest.MapFile{Data: raw}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	document := claudePinTestObject(t, files[claudeReleaseCatalogManifestPath].Data)
	releases := claudePinTestAs[[]any](t, document["releases"])
	var pinned map[string]any
	for _, item := range releases {
		entry := claudePinTestAs[map[string]any](t, item)
		if entry["release_sha256"] == claudeFWGHistoricalReleaseDigest {
			pinned = entry
		}
	}
	if pinned == nil {
		t.Fatal("内嵌 Claude 目录缺少钉住的历史 Release")
	}
	profilePath := claudePinTestAs[string](t, claudePinTestAs[map[string]any](t, pinned["profile"])["path"])
	wirePath := claudePinTestAs[string](t, claudePinTestAs[map[string]any](t, pinned["wire"])["path"])

	profile := claudePinTestObject(t, files[profilePath].Data)
	profileIdentity := claudePinTestAs[map[string]any](t, profile["identity"])
	profileIdentity["version"] = claudePinTestSyntheticVersion
	profileRaw, profileSHA := claudePinTestBlob(t, profile)

	wire := claudePinTestObject(t, files[wirePath].Data)
	wireIdentity := claudePinTestAs[map[string]any](t, wire["identity"])
	wireIdentity["version"] = claudePinTestSyntheticVersion
	wireIdentity["profile_digest"] = profileSHA
	wireRaw, wireSHA := claudePinTestBlob(t, wire)

	platform := claudePinTestAs[string](t, pinned["platform"])
	requiredRules := claudePinTestAs[string](t, pinned["required_rules_manifest_sha256"])
	ruleCount := claudePinTestAs[json.Number](t, pinned["required_rule_count"])
	releaseSHA, err := claudeCatalogCanonicalDigest(map[string]any{
		"schema_version":                  "claude-code-fw-g-model-capability-release/v1",
		"version":                         claudePinTestSyntheticVersion,
		"platform":                        platform,
		"profile_sha256":                  profileSHA,
		"wire_sha256":                     wireSHA,
		"model_capability_catalog_sha256": profileIdentity["model_capability_catalog_sha256"],
		"required_rules_manifest_sha256":  requiredRules,
		"required_rule_count":             ruleCount,
	})
	if err != nil {
		t.Fatal(err)
	}
	bundleSHA, err := claudeCatalogCanonicalDigest(map[string]any{
		"schema_version": "claude-code-fw-g-model-capability-bundle/v1",
		"persona":        "claude-code-oauth",
		"release_sha256": releaseSHA,
		"profile_sha256": profileSHA,
		"wire_sha256":    wireSHA,
	})
	if err != nil {
		t.Fatal(err)
	}
	syntheticProfilePath := path.Join("catalogdata/claude/profiles", claudePinTestSyntheticVersion, profileSHA+".json")
	syntheticWirePath := path.Join("catalogdata/claude/wire", claudePinTestSyntheticVersion, wireSHA+".json")
	files[syntheticProfilePath] = &fstest.MapFile{Data: profileRaw}
	files[syntheticWirePath] = &fstest.MapFile{Data: wireRaw}

	synthetic := make(map[string]any, len(pinned))
	for key, value := range pinned {
		synthetic[key] = value
	}
	synthetic["version"] = claudePinTestSyntheticVersion
	synthetic["profile"] = map[string]any{"path": syntheticProfilePath, "sha256": profileSHA}
	synthetic["wire"] = map[string]any{"path": syntheticWirePath, "sha256": wireSHA}
	synthetic["release_sha256"] = releaseSHA
	synthetic["bundle_sha256"] = bundleSHA
	if keepPinned {
		document["releases"] = append(releases, synthetic)
	} else {
		document["releases"] = []any{synthetic}
	}
	selectors := claudePinTestAs[map[string]any](t, document["selectors"])
	selectors["validation_candidate"] = map[string]any{
		"kind": "release", "release_sha256": releaseSHA, "changeset": "claude-code-synthetic-pin-candidate",
	}
	if !keepPinned {
		claudePinTestAs[map[string]any](t, selectors["production_active"])["release_sha256"] = releaseSHA
	}
	manifest, err := json.Marshal(document)
	if err != nil {
		t.Fatal(err)
	}
	files[claudeReleaseCatalogManifestPath] = &fstest.MapFile{Data: manifest}
	return files, releaseSHA
}

func claudePinTestObject(t *testing.T, raw []byte) map[string]any {
	t.Helper()
	decoder := json.NewDecoder(bytes.NewReader(raw))
	decoder.UseNumber()
	var value map[string]any
	if err := decoder.Decode(&value); err != nil {
		t.Fatal(err)
	}
	return value
}

func claudePinTestBlob(t *testing.T, value map[string]any) ([]byte, string) {
	t.Helper()
	raw, err := json.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	return raw, hex.EncodeToString(sum[:])
}

func claudePinTestAs[T any](t *testing.T, value any) T {
	t.Helper()
	typed, ok := value.(T)
	if !ok {
		t.Fatalf("合成 Claude 目录字段类型不符：%T", value)
	}
	return typed
}
