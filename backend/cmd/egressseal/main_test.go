package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress/sealcontract"
)

func TestRunRequiresProtectedBaseForSealedLifecycle(t *testing.T) {
	provisional := commandTestAssets(t, false)
	if err := run(
		provisional.receipt, provisional.ceiling, provisional.supplements, provisional.baseline, "",
	); err != nil {
		t.Fatalf("provisional 本地检查失败: %v", err)
	}

	sealed := commandTestAssets(t, true)
	err := run(sealed.receipt, sealed.ceiling, sealed.supplements, sealed.baseline, "")
	if err == nil || !strings.Contains(err.Error(), "EGRESS_SEAL_BASE_REF") {
		t.Fatalf("sealed 缺少受保护基准未 fail-close: %v", err)
	}
}

func TestResolveCommitAcceptsActualRepositoryHEAD(t *testing.T) {
	commit, err := resolveCommit("HEAD")
	if err != nil {
		t.Fatal(err)
	}
	if len(commit) != 40 || !sealcontract.ValidGitObjectID(commit) {
		t.Fatalf("当前仓库 HEAD 应解析为真实 40 位 SHA-1 object ID: %s", commit)
	}
	provisionalPaths := commandTestAssetsForBase(t, false, commit)
	sealedPaths := commandTestAssetsForBase(t, true, commit)
	provisional, err := readCurrentAssets(
		provisionalPaths.receipt, provisionalPaths.ceiling, provisionalPaths.supplements,
		provisionalPaths.baseline,
	)
	if err != nil {
		t.Fatal(err)
	}
	sealed, err := readCurrentAssets(
		sealedPaths.receipt, sealedPaths.ceiling, sealedPaths.supplements, sealedPaths.baseline,
	)
	if err != nil {
		t.Fatal(err)
	}
	if err := sealcontract.VerifyProtectedBase(sealed, provisional, commit); err != nil {
		t.Fatalf("真实 40 位 HEAD 未能完成首次 provisional→sealed 校验: %v", err)
	}
}

type commandAssetPaths struct {
	receipt     string
	ceiling     string
	supplements string
	baseline    string
}

func commandTestAssets(t *testing.T, sealed bool) commandAssetPaths {
	t.Helper()
	return commandTestAssetsForBase(t, sealed, sealcontract.BootstrapCommit)
}

func commandTestAssetsForBase(t *testing.T, sealed bool, protectedBaseCommit string) commandAssetPaths {
	t.Helper()
	directory := t.TempDir()
	lifecycle := sealcontract.LifecycleProvisional
	var sealedAt *string
	if sealed {
		lifecycle = sealcontract.LifecycleSealed
		value := "2026-08-03T00:00:00+08:00"
		sealedAt = &value
	}
	ceilingRaw := commandMarshalJSON(t, map[string]any{
		"schema_version": 1, "bootstrap_commit": sealcontract.BootstrapCommit,
		"lifecycle": lifecycle, "sealed_at": sealedAt, "sink_ids": []string{"sink.a"},
	})
	supplementRaw := commandMarshalJSON(t, map[string]any{
		"schema_version": 2, "bootstrap_commit": sealcontract.BootstrapCommit,
		"bootstrap_tree": sealcontract.BootstrapTree, "lifecycle": lifecycle,
		"supplements": []any{},
	})
	baselineRaw := commandMarshalJSON(t, map[string]any{
		"schema_version": 1, "bootstrap_commit": sealcontract.BootstrapCommit,
		"lifecycle": lifecycle, "observation_started_at": "2026-08-02",
		"sealed_at": sealedAt, "sink_ids": []string{"sink.a"},
	})
	receipt := sealcontract.Receipt{
		SchemaVersion: sealcontract.SchemaVersion, BootstrapCommit: sealcontract.BootstrapCommit,
		BootstrapTree: sealcontract.BootstrapTree, Lifecycle: lifecycle,
	}
	if sealed {
		ceilingDigest := commandDigest(ceilingRaw)
		supplementDigest := commandDigest(supplementRaw)
		reviewedBy := "reviewer"
		reviewRef := "review/ref"
		rationale := "正式封存"
		receipt.SealedAt = sealedAt
		receipt.ProtectedBaseCommit = &protectedBaseCommit
		receipt.LegacyCeilingSHA256 = &ceilingDigest
		receipt.PreBootstrapSupplementsSHA256 = &supplementDigest
		receipt.ReviewedBy = &reviewedBy
		receipt.ReviewRef = &reviewRef
		receipt.Rationale = &rationale
	}
	receiptRaw := commandMarshalJSON(t, receipt)
	paths := commandAssetPaths{
		receipt: filepath.Join(directory, "receipt.json"), ceiling: filepath.Join(directory, "ceiling.json"),
		supplements: filepath.Join(directory, "supplements.json"),
		baseline:    filepath.Join(directory, "baseline.json"),
	}
	for path, raw := range map[string][]byte{
		paths.receipt: receiptRaw, paths.ceiling: ceilingRaw, paths.supplements: supplementRaw,
		paths.baseline: baselineRaw,
	} {
		if err := os.WriteFile(path, raw, 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return paths
}

func commandMarshalJSON(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func commandDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
