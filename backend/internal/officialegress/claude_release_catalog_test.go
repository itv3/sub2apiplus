package officialegress

import (
	"strings"
	"testing"
)

func TestClaudeReleaseCatalogResolvesContentAddressedSelectors(t *testing.T) {
	catalog, err := LoadEmbeddedClaudeReleaseCatalog()
	if err != nil {
		t.Fatal(err)
	}
	candidate := catalog.ValidationCandidate()
	active := catalog.ProductionActive()
	if catalog.Digest() == "" || catalog.Source() == "" ||
		candidate.role != claudeSelectorValidationCandidate ||
		active.role != claudeSelectorProductionActive ||
		candidate.Changeset() == active.Changeset() || active.ApprovalDigest() == "" {
		t.Fatal("Claude Release Catalog selector 身份不完整")
	}
	for _, selection := range []ResolvedClaudeReleaseSelection{candidate, active} {
		release := selection.Release()
		if err := release.validate(); err != nil {
			t.Fatal(err)
		}
		profile, profileErr := loadClaudeProfile(release)
		wire, wireErr := loadClaudeWire(release)
		if profileErr != nil || wireErr != nil ||
			profile.document.Identity.Version != release.Version() ||
			wire.Identity.ProfileDigest != release.ProfileDigest() {
			t.Fatalf("Claude selector 未加载对应内容寻址制品：profile=%v wire=%v", profileErr, wireErr)
		}
	}
	rollback := catalog.ProductionRollback()
	if rollback.Commit() == "" || !strings.HasPrefix(rollback.ImageDigest(), "sha256:") ||
		rollback.ReceiptPath() == "" || rollback.ReceiptDigest() == "" {
		t.Fatal("Claude Catalog 未冻结真实操作回退")
	}
}

func TestClaudeProductionUsesSharedPersonaSelectorWithoutFakeRollbackRelease(t *testing.T) {
	sinks, err := ClaudeProductionSinkCatalog(DefaultSinkCatalog())
	if err != nil {
		t.Fatal(err)
	}
	catalog, err := NewProductionPersonaReleaseCatalog(sinks)
	if err != nil {
		t.Fatal(err)
	}
	active, err := catalog.ResolveSelection(PersonaClaudeCode, ProductionReleaseActive)
	if err != nil {
		t.Fatal(err)
	}
	release, ok := active.Release()
	if !ok || release.ReleaseDigest() != DefaultClaudeReleaseCatalog().
		ProductionActive().Release().ReleaseDigest() {
		t.Fatal("Claude production active 未接入共享 Persona selector")
	}
	rollback, err := catalog.ResolveSelection(PersonaClaudeCode, ProductionReleaseRollback)
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := rollback.Release(); ok {
		t.Fatal("Claude 操作回退被伪造成 strict Release")
	}
	if _, ok := rollback.OperationalDeployment(); !ok {
		t.Fatal("Claude shared selector 缺少操作回退载荷")
	}
	if _, err := catalog.Resolve(PersonaClaudeCode, ProductionReleaseRollback); err == nil {
		t.Fatal("Claude 操作回退错误取得了 Profile／Release 坐标")
	}
}

func TestClaudeExecutorPolicyIdentityDerivesFromSelectedRelease(t *testing.T) {
	selection := DefaultClaudeReleaseCatalog().ValidationCandidate()
	release := selection.Release()
	profile, err := loadClaudeProfile(release)
	if err != nil {
		t.Fatal(err)
	}
	endpoint, err := profile.endpoint("messages-inference")
	if err != nil {
		t.Fatal(err)
	}
	bundle := newClaudeReleaseBundle(release, endpoint, 3).executorControl()
	want := string(PersonaClaudeCode) + "." + release.ReleaseDigest() + "." + endpoint.kind
	if bundle.attempt.policyID != want || bundle.profileDigest != release.ProfileDigest() ||
		bundle.releaseDigest != release.ReleaseDigest() || bundle.bundleDigest != release.BundleDigest() {
		t.Fatal("Claude Executor Bundle 未从选中 Release 派生")
	}
}

func TestClaudeReleaseCatalogRecomputesReleaseAndBundleIdentities(t *testing.T) {
	release := DefaultClaudeReleaseCatalog().ProductionActive().Release()
	tamperedRelease := release
	tamperedRelease.releaseDigest = strings.Repeat("0", 64)
	if err := validateClaudeCatalogArtifactIdentities(tamperedRelease); err == nil ||
		!strings.Contains(err.Error(), "ReleaseArtifact") {
		t.Fatalf("伪造 ReleaseArtifact 摘要未 fail-close：%v", err)
	}
	tamperedBundle := release
	tamperedBundle.bundleDigest = strings.Repeat("0", 64)
	if err := validateClaudeCatalogArtifactIdentities(tamperedBundle); err == nil ||
		!strings.Contains(err.Error(), "ReleaseBundle") {
		t.Fatalf("伪造 ReleaseBundle 摘要未 fail-close：%v", err)
	}
}

func TestClaudeOfficialIngressVersionFollowsSelectedRelease(t *testing.T) {
	release := DefaultClaudeReleaseCatalog().ProductionActive().Release()
	userAgent := "claude-cli/" + release.Version() + " (external, sdk-cli)"
	entrypoint, features, err := parseClaudeOfficialUserAgent(userAgent, release.Version())
	if err != nil || entrypoint != ClaudeEntrypointSDKCLI || len(features) != 0 {
		t.Fatalf("所选 Release 的官方 User-Agent 未通过：%v", err)
	}
	if _, _, err := parseClaudeOfficialUserAgent(userAgent, "9.9.9"); err == nil {
		t.Fatal("其他 Release 版本错误继承官方入口身份")
	}
}

func TestClaudeProductionRejectsSharedSelectorCoordinateDrift(t *testing.T) {
	selection := ResolvedPersonaRelease{
		persona: PersonaClaudeCode, role: ProductionReleaseActive,
		version: ClaudeFWGVersion, releaseDigest: ClaudeFWGReleaseDigest,
		profileDigest: strings.Repeat("0", 64),
	}
	if _, err := resolveClaudeProductionReleaseForCoordinate(selection); err == nil ||
		!strings.Contains(err.Error(), "不一致") {
		t.Fatalf("共享 selector 漂移未 fail-close：%v", err)
	}
}
