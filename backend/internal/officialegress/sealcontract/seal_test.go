package sealcontract

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"testing"
)

func TestVerifyCurrentProvisionalAndSealed(t *testing.T) {
	provisional := provisionalAssets(t)
	receipt, err := VerifyCurrent(provisional)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Lifecycle != LifecycleProvisional {
		t.Fatalf("provisional lifecycle=%s", receipt.Lifecycle)
	}

	sealed := sealedAssets(t, []string{"sink.a"}, []string{"sink.a"}, []string{})
	receipt, err = VerifyCurrent(sealed)
	if err != nil {
		t.Fatal(err)
	}
	if receipt.Lifecycle != LifecycleSealed {
		t.Fatalf("sealed lifecycle=%s", receipt.Lifecycle)
	}

	tampered := sealed
	tampered.CeilingRaw = append(append([]byte(nil), sealed.CeilingRaw...), '\n')
	if _, err := VerifyCurrent(tampered); err == nil {
		t.Fatal("修改 ceiling 原文后未被 receipt 摘要拒绝")
	}
}

func TestVerifyProtectedBaseRejectsCoordinatedSealedRewrite(t *testing.T) {
	base := sealedAssets(t, []string{"sink.a"}, []string{"sink.a"}, []string{})
	// 同时修改 ceiling、supplements 和当前 receipt，使当前工作区内部完全自洽。
	// 只有受保护基准比较能够识别这种 coordinated rewrite。
	current := sealedAssets(
		t, []string{"sink.a", "sink.new"}, []string{"sink.a", "sink.new"},
		[]string{`{"candidate":"new"}`},
	)
	if _, err := VerifyCurrent(current); err != nil {
		t.Fatalf("反例必须先证明当前工作区内部自洽: %v", err)
	}
	if err := VerifyProtectedBase(current, base, BootstrapCommit); err == nil {
		t.Fatal("同时改写 receipt/ceiling/supplements 未被受保护基准拒绝")
	}
}

func TestVerifyProtectedBaseAllowsInitialSealWithActualSHA1AndRejectsDowngrade(t *testing.T) {
	provisional := provisionalAssets(t)
	sealed := sealedAssets(t, []string{"sink.a"}, []string{"sink.a"}, []string{})
	if len(BootstrapCommit) != 40 || !ValidGitObjectID(BootstrapCommit) {
		t.Fatalf("测试锚必须是仓库真实使用的 40 位 SHA-1 object ID: %s", BootstrapCommit)
	}
	if err := VerifyProtectedBase(sealed, provisional, BootstrapCommit); err != nil {
		t.Fatalf("首次受审封存应允许建立信任锚: %v", err)
	}
	if err := VerifyProtectedBase(sealed, provisional, strings.Repeat("2", 40)); err == nil {
		t.Fatal("首次 sealed receipt 未绑定实际 base SHA 时仍被接受")
	}
	if err := VerifyProtectedBase(provisional, sealed, BootstrapCommit); err == nil {
		t.Fatal("受保护基准 sealed 后仍允许降级")
	}
}

func TestVerifyProtectedBaseRejectsLegacyBaselineReaddition(t *testing.T) {
	base := sealedAssets(
		t, []string{"sink.a", "sink.b"}, []string{"sink.b"}, []string{},
	)
	current := sealedAssets(
		t, []string{"sink.a", "sink.b"}, []string{"sink.a", "sink.b"}, []string{},
	)
	if !bytesEqualThreeImmutableAssets(current, base) {
		t.Fatal("反例必须保持 receipt/ceiling/supplements 完全不变")
	}
	if _, err := VerifyCurrent(current); err != nil {
		t.Fatalf("重新加入后的当前工作区必须先保持内部自洽: %v", err)
	}
	if err := VerifyProtectedBase(current, base, BootstrapCommit); err == nil {
		t.Fatal("受保护 baseline 已删除 sink.a 后仍允许重新加入")
	}
}

func TestVerifyProtectedBaseAllowsLegacyBaselineReduction(t *testing.T) {
	base := sealedAssets(
		t, []string{"sink.a", "sink.b"}, []string{"sink.a", "sink.b"}, []string{},
	)
	current := sealedAssets(
		t, []string{"sink.a", "sink.b"}, []string{"sink.b"}, []string{},
	)
	if err := VerifyProtectedBase(current, base, BootstrapCommit); err != nil {
		t.Fatalf("sealed baseline 合法减少被拒绝: %v", err)
	}
}

func TestValidGitObjectIDSupportsSHA1AndSHA256(t *testing.T) {
	if !ValidGitObjectID(BootstrapCommit) {
		t.Fatal("40 位 SHA-1 object ID 被拒绝")
	}
	if !ValidGitObjectID(strings.Repeat("a", sha256.Size*2)) {
		t.Fatal("64 位 SHA-256 object ID 被拒绝")
	}
	if ValidGitObjectID(strings.ToUpper(BootstrapCommit)) || ValidGitObjectID("abc") {
		t.Fatal("非规范 Git object ID 被接受")
	}
}

func provisionalAssets(t *testing.T) Assets {
	t.Helper()
	ceiling := marshalTestJSON(t, map[string]any{
		"schema_version": 1, "bootstrap_commit": BootstrapCommit,
		"lifecycle": string(LifecycleProvisional), "sealed_at": nil,
		"sink_ids": []string{"sink.a"},
	})
	supplements := marshalTestJSON(t, map[string]any{
		"schema_version": 2, "bootstrap_commit": BootstrapCommit, "bootstrap_tree": BootstrapTree,
		"lifecycle": string(LifecycleProvisional), "supplements": []any{},
	})
	receipt := marshalTestJSON(t, Receipt{
		SchemaVersion: SchemaVersion, BootstrapCommit: BootstrapCommit,
		BootstrapTree: BootstrapTree, Lifecycle: LifecycleProvisional,
	})
	baseline := legacyBaselineJSON(t, LifecycleProvisional, nil, []string{"sink.a"})
	return Assets{
		ReceiptRaw: receipt, CeilingRaw: ceiling, SupplementsRaw: supplements, BaselineRaw: baseline,
	}
}

func sealedAssets(
	t *testing.T,
	ceilingSinkIDs []string,
	baselineSinkIDs []string,
	supplementRaw []string,
) Assets {
	t.Helper()
	sealedAt := "2026-08-03T00:00:00+08:00"
	ceiling := marshalTestJSON(t, map[string]any{
		"schema_version": 1, "bootstrap_commit": BootstrapCommit,
		"lifecycle": string(LifecycleSealed), "sealed_at": sealedAt, "sink_ids": ceilingSinkIDs,
	})
	supplements := make([]json.RawMessage, 0, len(supplementRaw))
	for _, raw := range supplementRaw {
		supplements = append(supplements, json.RawMessage(raw))
	}
	supplementDocument := marshalTestJSON(t, map[string]any{
		"schema_version": 2, "bootstrap_commit": BootstrapCommit, "bootstrap_tree": BootstrapTree,
		"lifecycle": string(LifecycleSealed), "supplements": supplements,
	})
	ceilingDigest := testDigest(ceiling)
	supplementDigest := testDigest(supplementDocument)
	protectedBaseCommit := BootstrapCommit
	reviewedBy := "reviewer"
	reviewRef := "review/ref"
	rationale := "观察期结束，正式封存。"
	receipt := marshalTestJSON(t, Receipt{
		SchemaVersion: SchemaVersion, BootstrapCommit: BootstrapCommit, BootstrapTree: BootstrapTree,
		Lifecycle: LifecycleSealed, SealedAt: &sealedAt, ProtectedBaseCommit: &protectedBaseCommit,
		LegacyCeilingSHA256:           &ceilingDigest,
		PreBootstrapSupplementsSHA256: &supplementDigest, ReviewedBy: &reviewedBy,
		ReviewRef: &reviewRef, Rationale: &rationale,
	})
	baseline := legacyBaselineJSON(t, LifecycleSealed, &sealedAt, baselineSinkIDs)
	return Assets{
		ReceiptRaw: receipt, CeilingRaw: ceiling, SupplementsRaw: supplementDocument,
		BaselineRaw: baseline,
	}
}

func legacyBaselineJSON(
	t *testing.T,
	lifecycle Lifecycle,
	sealedAt *string,
	sinkIDs []string,
) []byte {
	t.Helper()
	return marshalTestJSON(t, map[string]any{
		"schema_version": 1, "bootstrap_commit": BootstrapCommit,
		"lifecycle": lifecycle, "observation_started_at": "2026-08-02",
		"sealed_at": sealedAt, "sink_ids": sinkIDs,
	})
}

func bytesEqualThreeImmutableAssets(left, right Assets) bool {
	return string(left.ReceiptRaw) == string(right.ReceiptRaw) &&
		string(left.CeilingRaw) == string(right.CeilingRaw) &&
		string(left.SupplementsRaw) == string(right.SupplementsRaw)
}

func marshalTestJSON(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.MarshalIndent(value, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	return append(raw, '\n')
}

func testDigest(raw []byte) string {
	sum := sha256.Sum256(raw)
	return strings.ToLower(hex.EncodeToString(sum[:]))
}
