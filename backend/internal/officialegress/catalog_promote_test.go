package officialegress

import (
	"strings"
	"testing"
)

func TestBuildPromotedReleaseCatalogSwapsAcceptedTargetAndRollback(t *testing.T) {
	base := DefaultReleaseCatalog()
	oldActive, err := base.Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	oldPrevious, err := base.Resolve(ReleaseModePrevious)
	if err != nil {
		t.Fatal(err)
	}
	input := CatalogPromotionInput{
		CampaignID:            "codex-0_147_0-20260815T055433Z",
		AcceptanceSHA256:      strings.Repeat("a", 64),
		TargetVersion:         oldPrevious.Version(),
		TargetProfileDigest:   oldPrevious.ProfileDigest(),
		RollbackVersion:       oldActive.Version(),
		RollbackProfileDigest: oldActive.ProfileDigest(),
	}
	promoted, err := BuildPromotedReleaseCatalog(base, input)
	if err != nil {
		t.Fatal(err)
	}
	active, err := promoted.catalog.Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	previous, err := promoted.catalog.Resolve(ReleaseModePrevious)
	if err != nil {
		t.Fatal(err)
	}
	if active.Version() != oldPrevious.Version() ||
		active.ProfileDigest() != oldPrevious.ProfileDigest() {
		t.Fatalf("目标画像未提升为 Active：version=%s digest=%s", active.Version(), active.ProfileDigest())
	}
	if previous.Version() != oldActive.Version() ||
		previous.ProfileDigest() != oldActive.ProfileDigest() {
		t.Fatalf("原 Active 未固化为 Previous：version=%s digest=%s", previous.Version(), previous.ProfileDigest())
	}
	if active.ReleaseDigest() == previous.ReleaseDigest() {
		t.Fatal("生产提升错误折叠了 Active/Previous ReleaseDigest")
	}
	receipt := promoted.CatalogPromotionReceiptCore()
	if receipt["production_selector_changed"] != true ||
		receipt["active_mode"] != "active" || receipt["previous_mode"] != "previous" {
		t.Fatalf("生产提升收据不完整：%v", receipt)
	}
	files, err := promoted.RuntimeCatalogFiles()
	if err != nil {
		t.Fatal(err)
	}
	if len(files) < 5 {
		t.Fatalf("生产提升未导出完整 RuntimeCatalog：%d", len(files))
	}
}

func TestBuildPromotedReleaseCatalogRejectsCoordinateDrift(t *testing.T) {
	base := DefaultReleaseCatalog()
	active, _ := base.Resolve(ReleaseModeActive)
	previous, _ := base.Resolve(ReleaseModePrevious)
	_, err := BuildPromotedReleaseCatalog(base, CatalogPromotionInput{
		CampaignID:            "codex-0_147_0-20260815T055433Z",
		AcceptanceSHA256:      strings.Repeat("b", 64),
		TargetVersion:         previous.Version(),
		TargetProfileDigest:   strings.Repeat("c", 64),
		RollbackVersion:       active.Version(),
		RollbackProfileDigest: active.ProfileDigest(),
	})
	if err == nil {
		t.Fatal("目标画像摘要漂移时未失败关闭")
	}
}
