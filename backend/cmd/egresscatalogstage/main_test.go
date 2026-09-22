package main

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func approvedProfileManifestForStageTest(t *testing.T) []byte {
	return approvedProfileManifestForStageVersionTest(t, "0.148.0")
}

// activeStageVersionForTest 返回当前 Active 版本，供“禁止重复导入 Active”的负例使用。
func activeStageVersionForTest(t *testing.T) string {
	t.Helper()
	active, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	return active.Version()
}

// stageTargetVersionAfterActiveForTest 从当前 Active 动态推导一个合法的候选目标版本，
// 不硬编码具体版本号：次版本号 +1 同时满足两个条件——既不等于 Active（否则按设计被
// BuildStagedReleaseCatalog 拒绝重复导入），又落在 requiresCompleteVCArtifacts 为真的
// 区间（0.154.0 起），从而继续覆盖“0.154 起必须绑定 VC-3 两项摘要”的合同。
// 0.154 晋升为 Active 之后，写死 0.154.0 的旧夹具会被正确地拒绝，本函数消除该耦合。
func stageTargetVersionAfterActiveForTest(t *testing.T) string {
	t.Helper()
	version := activeStageVersionForTest(t)
	var major, minor, patch int
	if _, err := fmt.Sscanf(version, "%d.%d.%d", &major, &minor, &patch); err != nil {
		t.Fatalf("无法解析 Active 版本 %q：%v", version, err)
	}
	if major == 0 && minor < 154 {
		minor = 154
	}
	return fmt.Sprintf("%d.%d.0", major, minor+1)
}

func approvedProfileManifestForStageVersionTest(t *testing.T, targetVersion string) []byte {
	t.Helper()
	active, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := active.Profile().ToSnapshot()
	raw, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	raw = []byte(strings.ReplaceAll(string(raw), active.Version(), targetVersion))
	if err := json.Unmarshal(raw, &snapshot); err != nil {
		t.Fatal(err)
	}
	snapshot, err = profilecontract.PrepareSnapshotForManifest(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	payload, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	var canonicalPayload any
	if err := json.Unmarshal(payload, &canonicalPayload); err != nil {
		t.Fatal(err)
	}
	manifest := approvedProfileManifest{
		SchemaVersion:        "codex-egress-profile/v1",
		CodexVersion:         targetVersion,
		ProfileID:            "codex-" + targetVersion + "-stage-test",
		ProfileDigest:        snapshot.Digest,
		ProfilePayload:       payload,
		ProfilePayloadSHA256: canonicalSHA256(canonicalPayload),
		Status:               "approved",
	}
	manifestRaw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	return append(manifestRaw, '\n')
}

func TestStageApprovedProfileWritesCompleteImmutableCandidateDirectory(t *testing.T) {
	root := t.TempDir()
	manifestPath := filepath.Join(root, "profile.json")
	if err := os.WriteFile(manifestPath, approvedProfileManifestForStageTest(t), 0o600); err != nil {
		t.Fatal(err)
	}
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	output := filepath.Join(resolvedRoot, "catalog-stage")
	receipt, err := stageApprovedProfile(
		manifestPath,
		"codex-0-147-stage-test",
		strings.Repeat("a", 64),
		"",
		"",
		output,
	)
	if err != nil {
		t.Fatal(err)
	}
	if receipt["active_unchanged"] != true ||
		receipt["production_selector_changed"] != false ||
		receipt["candidate_release_mode"] != "previous" {
		t.Fatalf("候选目录收据越权：%v", receipt)
	}
	for _, relativePath := range []string{
		"catalog-stage-receipt.json",
		"catalogdata/runtime/release-catalog.json",
		"profilecontract/testdata/snapshot-catalog.json",
		"releasecontract/testdata/release-graph.json",
	} {
		if info, statErr := os.Stat(filepath.Join(output, relativePath)); statErr != nil || !info.Mode().IsRegular() {
			t.Fatalf("候选目录缺少文件 %s：%v", relativePath, statErr)
		}
	}
	if _, err := stageApprovedProfile(
		manifestPath,
		"codex-0-147-stage-test",
		strings.Repeat("a", 64),
		"",
		"",
		output,
	); err == nil {
		t.Fatal("重复输出覆盖候选目录时未失败关闭")
	}
}

// TestStageApprovedProfileBindsVC3ArtifactsSince0154 覆盖“0.154 起的目标版本必须绑定
// VC-3 画像派生与 post-promotion 门禁需求两项摘要”的合同；目标版本从当前 Active 推导。
func TestStageApprovedProfileBindsVC3ArtifactsSince0154(t *testing.T) {
	root := t.TempDir()
	manifestPath := filepath.Join(root, "profile.json")
	manifestRaw := approvedProfileManifestForStageVersionTest(t, stageTargetVersionAfterActiveForTest(t))
	if err := os.WriteFile(manifestPath, manifestRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	derivationSHA := strings.Repeat("b", 64)
	requirementsSHA := strings.Repeat("c", 64)
	receipt, err := stageApprovedProfile(
		manifestPath,
		"codex-stage-vc3-test",
		strings.Repeat("a", 64),
		derivationSHA,
		requirementsSHA,
		filepath.Join(resolvedRoot, "catalog-stage"),
	)
	if err != nil {
		t.Fatal(err)
	}
	if receipt["profile_derivation_sha256"] != derivationSHA ||
		receipt["post_promotion_gate_requirements_sha256"] != requirementsSHA {
		t.Fatalf("候选目录收据未绑定 VC-3 制品：%v", receipt)
	}
}

func TestStageApprovedProfileRejectsMissingVC3ArtifactsSince0154(t *testing.T) {
	root := t.TempDir()
	manifestPath := filepath.Join(root, "profile.json")
	targetVersion := stageTargetVersionAfterActiveForTest(t)
	manifestRaw := approvedProfileManifestForStageVersionTest(t, targetVersion)
	if err := os.WriteFile(manifestPath, manifestRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stageApprovedProfile(
		manifestPath,
		"codex-stage-vc3-test",
		strings.Repeat("a", 64),
		"",
		"",
		filepath.Join(resolvedRoot, "catalog-stage"),
	); err == nil {
		t.Fatalf("%s 缺少 VC-3 摘要时未失败关闭", targetVersion)
	}
}

// TestStageApprovedProfileRejectsActiveVersionReimport 固化产品行为：已经是 Active 的版本
// 不得再作为候选导入。晋升后旧夹具正是撞上这条规则，这里把它变成显式负例。
func TestStageApprovedProfileRejectsActiveVersionReimport(t *testing.T) {
	root := t.TempDir()
	manifestPath := filepath.Join(root, "profile.json")
	activeVersion := activeStageVersionForTest(t)
	manifestRaw := approvedProfileManifestForStageVersionTest(t, activeVersion)
	if err := os.WriteFile(manifestPath, manifestRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	_, stageErr := stageApprovedProfile(
		manifestPath,
		"codex-stage-active-reimport-test",
		strings.Repeat("a", 64),
		strings.Repeat("b", 64),
		strings.Repeat("c", 64),
		filepath.Join(resolvedRoot, "catalog-stage"),
	)
	if stageErr == nil {
		t.Fatalf("Active 版本 %s 被错误地接受为候选", activeVersion)
	}
	if !strings.Contains(stageErr.Error(), "目标版本已经是 Active") {
		t.Fatalf("拒绝原因不是重复导入 Active：%v", stageErr)
	}
}

func TestReadApprovedProfileManifestRejectsDraft(t *testing.T) {
	var manifest map[string]any
	if err := json.Unmarshal(approvedProfileManifestForStageTest(t), &manifest); err != nil {
		t.Fatal(err)
	}
	manifest["status"] = "draft"
	raw, err := json.Marshal(manifest)
	if err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(t.TempDir(), "profile.json")
	if err := os.WriteFile(path, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := readApprovedProfileManifest(path); err == nil {
		t.Fatal("draft 画像进入候选目录时未失败关闭")
	}
}

func TestPrepareProfileManifestSurvivesSortedApprovalCopy(t *testing.T) {
	active, err := officialegress.DefaultReleaseCatalog().Resolve(officialegress.ReleaseModeActive)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := active.Profile().ToSnapshot()
	raw, err := json.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	raw = []byte(strings.ReplaceAll(string(raw), active.Version(), "0.148.0"))
	root := t.TempDir()
	resolvedRoot, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	snapshotPath := filepath.Join(resolvedRoot, "snapshot.json")
	if err := os.WriteFile(snapshotPath, raw, 0o600); err != nil {
		t.Fatal(err)
	}
	draftPath := filepath.Join(resolvedRoot, "profile-draft.json")
	draft, err := prepareProfileManifest(
		snapshotPath,
		"codex-0.148.0-prepared",
		draftPath,
	)
	if err != nil {
		t.Fatal(err)
	}
	if draft.Status != "draft" || draft.ProfileDigest == "" {
		t.Fatalf("画像草案身份不完整：%+v", draft)
	}
	var approved map[string]any
	if err := json.Unmarshal(mustReadStageTestFile(t, draftPath), &approved); err != nil {
		t.Fatal(err)
	}
	approved["status"] = "approved"
	approvedRaw, err := json.Marshal(approved)
	if err != nil {
		t.Fatal(err)
	}
	approvedPath := filepath.Join(resolvedRoot, "profile-approved.json")
	if err := os.WriteFile(approvedPath, approvedRaw, 0o600); err != nil {
		t.Fatal(err)
	}
	manifest, err := readApprovedProfileManifest(approvedPath)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := officialegress.BuildStagedReleaseCatalog(
		officialegress.DefaultReleaseCatalog(),
		officialegress.CatalogStageInput{
			TargetVersion:     manifest.CodexVersion,
			ProfileID:         manifest.ProfileID,
			ProfileDigest:     manifest.ProfileDigest,
			ProfilePayload:    manifest.ProfilePayload,
			CampaignID:        "codex-0-147-prepare-test",
			ClassificationSHA: strings.Repeat("b", 64),
		},
	); err != nil {
		t.Fatalf("sort_keys 批准副本破坏画像摘要：%v", err)
	}
}

func mustReadStageTestFile(t *testing.T, path string) []byte {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
