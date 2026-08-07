package main

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
)

func approvedProfileManifestForStageTest(t *testing.T) []byte {
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
	raw = []byte(strings.ReplaceAll(string(raw), "0.145.0", "0.147.0"))
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
		CodexVersion:         "0.147.0",
		ProfileID:            "codex-0.147.0-stage-test",
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
		output,
	); err == nil {
		t.Fatal("重复输出覆盖候选目录时未失败关闭")
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
	raw = []byte(strings.ReplaceAll(string(raw), "0.145.0", "0.147.0"))
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
		"codex-0.147.0-prepared",
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
