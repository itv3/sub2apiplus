package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func stagedCatalogTestInput(t *testing.T) CatalogStageInput {
	t.Helper()
	active, err := DefaultReleaseCatalog().Resolve(ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := active.Profile().ToSnapshot()
	raw, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	raw = []byte(strings.ReplaceAll(string(raw), "0.145.0", "0.147.0"))
	if err := json.Unmarshal(raw, &snapshot); err != nil {
		t.Fatal(err)
	}
	snapshot.Digest = ""
	digestInput, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(digestInput)
	snapshot.Digest = hex.EncodeToString(sum[:])
	payload, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	return CatalogStageInput{
		TargetVersion: "0.147.0", ProfileID: "codex-0.147.0-approved",
		ProfileDigest: snapshot.Digest, ProfilePayload: payload,
		CampaignID: "codex-0-147-test", ClassificationSHA: strings.Repeat("a", 64),
	}
}

func TestBuildStagedReleaseCatalogKeepsActiveAndPlacesTargetInPrevious(t *testing.T) {
	base := DefaultReleaseCatalog()
	input := stagedCatalogTestInput(t)
	staged, err := BuildStagedReleaseCatalog(base, input)
	if err != nil {
		t.Fatal(err)
	}
	baseActive, _ := base.Resolve(ReleaseModeActive)
	active, _ := staged.catalog.Resolve(ReleaseModeActive)
	previous, _ := staged.catalog.Resolve(ReleaseModePrevious)
	if active.Version() != baseActive.Version() ||
		active.ProfileDigest() != baseActive.ProfileDigest() ||
		active.ReleaseDigest() != baseActive.ReleaseDigest() {
		t.Fatal("候选导入修改了 Active")
	}
	if previous.Version() != input.TargetVersion ||
		previous.ProfileDigest() != input.ProfileDigest ||
		previous.ReleaseDigest() == active.ReleaseDigest() {
		t.Fatal("目标画像未进入独立 Previous 候选坐标")
	}
	files, err := staged.RuntimeCatalogFiles()
	if err != nil {
		t.Fatal(err)
	}
	wantFiles := len(base.snapshots.ToDoc().Snapshots) + 1 + 3
	if len(files) != wantFiles {
		t.Fatalf("候选 RuntimeCatalog 文件数=%d，期望 %d", len(files), wantFiles)
	}
	contractRaw, err := staged.ContractSnapshotCatalogJSON()
	if err != nil {
		t.Fatal(err)
	}
	contractDoc, err := profilecontract.ParseSnapshotCatalog(contractRaw)
	if err != nil {
		t.Fatal(err)
	}
	found := false
	for _, entry := range contractDoc.Snapshots {
		if entry.Version == input.TargetVersion && entry.Digest == input.ProfileDigest {
			found = entry.BlobSHA256 == "" && strings.HasPrefix(entry.File, "snapshots/")
		}
	}
	if !found {
		t.Fatal("契约镜像缺少目标内容寻址 Snapshot")
	}
	receipt := staged.CatalogStageReceiptCore()
	if receipt["active_unchanged"] != true ||
		receipt["production_selector_changed"] != false ||
		receipt["candidate_release_mode"] != string(ReleaseModePrevious) {
		t.Fatalf("候选收据越权声明生产切换：%v", receipt)
	}
}

func TestBuildStagedReleaseCatalogRejectsUnapprovedCoordinates(t *testing.T) {
	input := stagedCatalogTestInput(t)
	input.ProfileDigest = strings.Repeat("f", 64)
	if _, err := BuildStagedReleaseCatalog(DefaultReleaseCatalog(), input); err == nil {
		t.Fatal("画像自报 digest 与批准坐标不一致时未失败关闭")
	}
	input = stagedCatalogTestInput(t)
	input.TargetVersion = "0.145.0"
	if _, err := BuildStagedReleaseCatalog(DefaultReleaseCatalog(), input); err == nil {
		t.Fatal("Active 版本被作为候选重复导入时未失败关闭")
	}
}
