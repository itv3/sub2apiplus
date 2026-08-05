package officialegress

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"os"
	"sort"
	"strings"
	"testing"
)

type changeset5ConflictUnit struct {
	Path                    string `json:"path"`
	UnitType                string `json:"unit_type"`
	Receiver                string `json:"receiver"`
	Name                    string `json:"name"`
	Signature               string `json:"signature"`
	LocalASTSHA256          string `json:"pre_refactor_local_ast_sha256"`
	LocalTokenSHA256        string `json:"pre_refactor_local_token_sha256"`
	NormalizedDiffSHA256    string `json:"normalized_diff_sha256"`
	HunkSHA256              string `json:"hunk_sha256"`
	OfficialEgressOwnership string `json:"official_egress_ownership"`
	AllowedFinalState       string `json:"allowed_final_state"`
	Generated               bool   `json:"generated"`
}

type changeset5ConflictClassificationAmendment struct {
	UnitKey               string `json:"unit_key"`
	Path                  string `json:"path"`
	UnitType              string `json:"unit_type"`
	Receiver              string `json:"receiver"`
	Name                  string `json:"name"`
	Signature             string `json:"signature"`
	OriginalOwnership     string `json:"original_ownership"`
	EffectiveOwnership    string `json:"effective_ownership"`
	PreOriginalASTSHA256  string `json:"pre_original_ast_sha256"`
	PostOriginalASTSHA256 string `json:"post_original_ast_sha256"`
	Reason                string `json:"reason"`
}

type changeset5ConflictClassificationOverlay struct {
	SchemaVersion                    string                                      `json:"schema_version"`
	Changeset                        string                                      `json:"changeset"`
	ClassificationUpstreamBase       string                                      `json:"classification_upstream_base"`
	PreFullInventorySHA256           string                                      `json:"pre_full_inventory_sha256"`
	PreRawGovernableInventorySHA256  string                                      `json:"pre_raw_governable_inventory_sha256"`
	PostFullInventorySHA256          string                                      `json:"post_full_inventory_sha256"`
	PostRawGovernableInventorySHA256 string                                      `json:"post_raw_governable_inventory_sha256"`
	Amendments                       []changeset5ConflictClassificationAmendment `json:"amendments"`
	EffectivePreGovernableCount      int                                         `json:"effective_pre_governable_count"`
	EffectivePostGovernableCount     int                                         `json:"effective_post_governable_count"`
}

type changeset5ConflictInventory struct {
	SchemaVersion              string                   `json:"schema_version"`
	InventoryKind              string                   `json:"inventory_kind"`
	ClassificationUpstreamBase string                   `json:"classification_upstream_base"`
	ConflictFileCount          int                      `json:"conflict_file_count"`
	UnitCount                  int                      `json:"unit_count"`
	Units                      []changeset5ConflictUnit `json:"units"`
}

func TestChangeset5ConflictUnitsOnlyShrinkAndPreserveUnrelatedFork(t *testing.T) {
	const (
		preFullSHA        = "6fee65be361be40bd8abb1d16bc88b10f9877dc475899549e15afbae0870a470"
		preGovernableSHA  = "bc1ab9379486d510f40fb8d94fa660cd069af88d6d8fe5a3a99317fa19f93fcf"
		preReceiptSHA     = "8c2a8c81f7452d137f35408df8cdcb97b81357aa4735900d090d23357ded1785"
		postFullSHA       = "6a3c0a45339da65a01ae2e5d591cb682046f33fe35464583c66155dfe318c816"
		postGovernableSHA = "03c2d35555febf086caffdd7ab03625353be5f1146064c19aebfc50b44543118"
		postReceiptSHA    = "e263d3b6199f6c411c4c48c4a59c19be773c48e8a61df3d0301fe9b112bcc9b4"
		migrationSHA      = "f81b127ba6198ff72be55b6d320cf2acf70bb398a55c598f3e5b139b7eb9e93a"
		amendmentSHA      = "53fbdbad39153065aba599fe9b73fa576a1aefbabf89bf6fc870b391c65ff9a3"
	)
	paths := map[string]string{
		"../../../docs/egress/consolidation/conflict-inventory/full.json":                     preFullSHA,
		"../../../docs/egress/consolidation/conflict-inventory/governable.json":               preGovernableSHA,
		"../../../docs/egress/consolidation/conflict-inventory/receipt.json":                  preReceiptSHA,
		"../../../docs/egress/consolidation/post-refactor-conflict-inventory/full.json":       postFullSHA,
		"../../../docs/egress/consolidation/post-refactor-conflict-inventory/governable.json": postGovernableSHA,
		"../../../docs/egress/consolidation/post-refactor-conflict-inventory/receipt.json":    postReceiptSHA,
		"../../../docs/egress/consolidation/conflict-migration-receipt.json":                  migrationSHA,
		"../../../docs/egress/consolidation/conflict-classification-amendments.json":          amendmentSHA,
	}
	for path, expected := range paths {
		if actual := changeset5FileSHA256(t, path); actual != expected {
			t.Fatalf("变更集 5 冲突证据摘要漂移：path=%s actual=%s", path, actual)
		}
	}

	pre := changeset5ReadConflictInventory(
		t, "../../../docs/egress/consolidation/conflict-inventory/full.json",
	)
	preGovernable := changeset5ReadConflictInventory(
		t, "../../../docs/egress/consolidation/conflict-inventory/governable.json",
	)
	post := changeset5ReadConflictInventory(
		t, "../../../docs/egress/consolidation/post-refactor-conflict-inventory/full.json",
	)
	postGovernable := changeset5ReadConflictInventory(
		t, "../../../docs/egress/consolidation/post-refactor-conflict-inventory/governable.json",
	)
	overlay := changeset5ReadConflictClassificationOverlay(
		t, "../../../docs/egress/consolidation/conflict-classification-amendments.json",
	)
	if overlay.SchemaVersion != "changeset5-conflict-classification-amendments/v1" ||
		overlay.Changeset != "5" ||
		overlay.ClassificationUpstreamBase != "26d894ef4f50645a4bf1030e378ac892f17d0223" ||
		overlay.PreFullInventorySHA256 != preFullSHA ||
		overlay.PreRawGovernableInventorySHA256 != preGovernableSHA ||
		overlay.PostFullInventorySHA256 != postFullSHA ||
		overlay.PostRawGovernableInventorySHA256 != postGovernableSHA {
		t.Fatalf("冲突分类 overlay 未绑定原始 inventory：%+v", overlay)
	}
	if pre.UnitCount != 260 || len(pre.Units) != 260 || preGovernable.UnitCount != 103 ||
		post.UnitCount != 247 || len(post.Units) != 247 || postGovernable.UnitCount != 90 ||
		pre.ConflictFileCount != 36 || post.ConflictFileCount != 36 {
		t.Fatalf("冲突单元冻结计数非法：pre=%d/%d post=%d/%d", pre.UnitCount,
			preGovernable.UnitCount, post.UnitCount, postGovernable.UnitCount)
	}
	changeset5RequireStrictSubset(t, preGovernable.Units, pre.Units)
	changeset5RequireStrictSubset(t, postGovernable.Units, post.Units)
	effectivePre := changeset5EffectiveGovernableUnits(t, pre, preGovernable, overlay, true)
	effectivePost := changeset5EffectiveGovernableUnits(t, post, postGovernable, overlay, false)
	if len(effectivePre) != 104 || len(effectivePost) != 91 {
		t.Fatalf("effective governable 计数漂移：pre=%d post=%d", len(effectivePre), len(effectivePost))
	}
	changeset5RequireStrictSubset(t, effectivePre, pre.Units)
	changeset5RequireStrictSubset(t, effectivePost, post.Units)

	violations := changeset5ConflictViolations(pre.Units, post.Units, changeset5EffectiveOwnership(overlay))
	if len(violations) != 0 {
		t.Fatalf("冲突单元收缩门禁失败：%v", violations)
	}

	preByKey := changeset5ConflictUnitMap(pre.Units)
	postByKey := changeset5ConflictUnitMap(post.Units)
	var changed, removed []string
	for key, before := range preByKey {
		after, exists := postByKey[key]
		if !exists {
			removed = append(removed, key)
			continue
		}
		if before.LocalASTSHA256 != after.LocalASTSHA256 {
			changed = append(changed, key)
		}
	}
	sort.Strings(changed)
	sort.Strings(removed)
	if len(changed) != 28 || changeset5StringFingerprint(changed) !=
		"e17e3a68a0b6c326d1b7b8ad42b9a44c3733337b21f87bb29d49991a9615f047" {
		t.Fatalf("retained 单元变更闭集漂移：count=%d fingerprint=%s", len(changed), changeset5StringFingerprint(changed))
	}
	if len(removed) != 13 || changeset5StringFingerprint(removed) !=
		"d6d6b8da80acb350003102b19de97909d3b8bee263fdb30f2b67711de8448331" {
		t.Fatalf("迁出单元闭集漂移：count=%d fingerprint=%s", len(removed), changeset5StringFingerprint(removed))
	}

	preModified := changeset5ModifiedDeclarationKeys(pre.Units)
	postModified := changeset5ModifiedDeclarationKeys(post.Units)
	const modifiedFingerprint = "e6071ec3247da5eb7bd570ac651dc8230e7333ff220dd74d7ac5d92fd2cd633b"
	if len(preModified) != 136 || len(postModified) != 136 ||
		changeset5StringFingerprint(preModified) != modifiedFingerprint ||
		changeset5StringFingerprint(postModified) != modifiedFingerprint {
		t.Fatal("既有上游 declaration 修改集合发生新增、删除或重命名")
	}

	for key, before := range preByKey {
		after, exists := postByKey[key]
		if before.Generated && (!exists || before.LocalASTSHA256 != after.LocalASTSHA256) {
			t.Fatalf("生成文件单元不是确定性原样保留：%s", key)
		}
		if before.Path == "Dockerfile" && (!exists || before.HunkSHA256 != after.HunkSHA256 ||
			before.NormalizedDiffSHA256 != after.NormalizedDiffSHA256) {
			t.Fatal("Dockerfile hunk 指纹或行区间所有权漂移")
		}
	}
}

func TestChangeset5ConflictGateRejectsNewUpstreamModificationAndUnrelatedForkDrift(t *testing.T) {
	baseline := []changeset5ConflictUnit{
		{Path: "shared.go", UnitType: "declaration_modified", Name: "Shared", LocalASTSHA256: "before", LocalTokenSHA256: "token", OfficialEgressOwnership: "non_official"},
	}
	post := append([]changeset5ConflictUnit(nil), baseline...)
	post[0].LocalASTSHA256 = "mutated"
	post = append(post, changeset5ConflictUnit{
		Path: "shared.go", UnitType: "declaration_modified", Name: "NewUpstream", LocalASTSHA256: "new", OfficialEgressOwnership: "mixed",
	})
	violations := changeset5ConflictViolations(baseline, post, nil)
	joined := strings.Join(violations, "\n")
	if !strings.Contains(joined, "新增被修改的上游 declaration") ||
		!strings.Contains(joined, "非官方 fork 单元漂移") {
		t.Fatalf("冲突门禁 mutation 负例未被拒绝：%v", violations)
	}
}

func TestChangeset5ConflictOverlayDoesNotHideOtherNonOfficialDrift(t *testing.T) {
	baseline := []changeset5ConflictUnit{
		{Path: "shared.go", UnitType: "declaration_added", Name: "Amended", LocalASTSHA256: "before-amended", LocalTokenSHA256: "token-amended", OfficialEgressOwnership: "non_official"},
		{Path: "shared.go", UnitType: "declaration_added", Name: "Unrelated", LocalASTSHA256: "before-unrelated", LocalTokenSHA256: "token-unrelated", OfficialEgressOwnership: "non_official"},
	}
	post := append([]changeset5ConflictUnit(nil), baseline...)
	post[0].LocalASTSHA256 = "after-amended"
	post[1].LocalASTSHA256 = "after-unrelated"
	effective := map[string]string{
		changeset5ConflictUnitKey(baseline[0]): "official_egress_exclusive",
	}
	violations := changeset5ConflictViolations(baseline, post, effective)
	joined := strings.Join(violations, "\n")
	if strings.Contains(joined, "Amended") || !strings.Contains(joined, "Unrelated") {
		t.Fatalf("classification overlay 隐藏了其他 non_official 漂移：%v", violations)
	}
}

func changeset5ConflictViolations(
	pre []changeset5ConflictUnit,
	post []changeset5ConflictUnit,
	effectiveOwnership map[string]string,
) []string {
	preByKey := changeset5ConflictUnitMap(pre)
	postByKey := changeset5ConflictUnitMap(post)
	preModified := make(map[string]bool)
	for _, unit := range pre {
		if unit.UnitType == "declaration_modified" {
			preModified[changeset5DeclarationKey(unit)] = true
		}
	}
	var violations []string
	for _, unit := range post {
		if unit.UnitType == "declaration_modified" && !preModified[changeset5DeclarationKey(unit)] {
			violations = append(violations, "新增被修改的上游 declaration: "+changeset5DeclarationKey(unit))
		}
		if _, existed := preByKey[changeset5ConflictUnitKey(unit)]; !existed {
			violations = append(violations, "冲突面出现新增单元: "+changeset5ConflictUnitKey(unit))
		}
	}
	for key, before := range preByKey {
		after, exists := postByKey[key]
		ownership := before.OfficialEgressOwnership
		if amended, ok := effectiveOwnership[key]; ok {
			ownership = amended
		}
		if ownership == "non_official" &&
			(!exists || before.LocalASTSHA256 != after.LocalASTSHA256 ||
				before.LocalTokenSHA256 != after.LocalTokenSHA256) {
			violations = append(violations, "非官方 fork 单元漂移: "+key)
		}
	}
	sort.Strings(violations)
	return violations
}

func changeset5ReadConflictInventory(t *testing.T, path string) changeset5ConflictInventory {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var result changeset5ConflictInventory
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatal(err)
	}
	if result.SchemaVersion != "changeset5-conflict-unit-inventory/v1" ||
		result.ClassificationUpstreamBase != "26d894ef4f50645a4bf1030e378ac892f17d0223" {
		t.Fatalf("冲突清单元数据非法：%+v", result)
	}
	return result
}

func changeset5ReadConflictClassificationOverlay(
	t *testing.T,
	path string,
) changeset5ConflictClassificationOverlay {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var result changeset5ConflictClassificationOverlay
	if err := json.Unmarshal(raw, &result); err != nil {
		t.Fatal(err)
	}
	return result
}

func changeset5EffectiveGovernableUnits(
	t *testing.T,
	full changeset5ConflictInventory,
	raw changeset5ConflictInventory,
	overlay changeset5ConflictClassificationOverlay,
	pre bool,
) []changeset5ConflictUnit {
	t.Helper()
	fullByKey := changeset5ConflictUnitMap(full.Units)
	effectiveByKey := changeset5ConflictUnitMap(raw.Units)
	seenAmendment := make(map[string]bool, len(overlay.Amendments))
	for _, amendment := range overlay.Amendments {
		key := strings.Join([]string{
			amendment.Path, amendment.UnitType, amendment.Receiver, amendment.Name,
		}, "\x00")
		if amendment.UnitKey != key || seenAmendment[key] {
			t.Fatalf("冲突分类 amendment key 非法或重复：%q", amendment.UnitKey)
		}
		seenAmendment[key] = true
		unit, exists := fullByKey[key]
		if !exists || unit.Signature != amendment.Signature ||
			unit.OfficialEgressOwnership != amendment.OriginalOwnership ||
			amendment.OriginalOwnership != "non_official" ||
			(amendment.EffectiveOwnership != "official_egress_exclusive" && amendment.EffectiveOwnership != "mixed") ||
			strings.TrimSpace(amendment.Reason) == "" {
			t.Fatalf("冲突分类 amendment 未精确绑定原始单元：%+v", amendment)
		}
		expectedAST := amendment.PostOriginalASTSHA256
		if pre {
			expectedAST = amendment.PreOriginalASTSHA256
		}
		if unit.LocalASTSHA256 != expectedAST {
			t.Fatalf("冲突分类 amendment AST 锚点漂移：key=%q", key)
		}
		if _, exists := effectiveByKey[key]; exists {
			t.Fatalf("冲突分类 amendment 重复扩入 raw governable：%q", key)
		}
		unit.OfficialEgressOwnership = amendment.EffectiveOwnership
		effectiveByKey[key] = unit
	}
	result := make([]changeset5ConflictUnit, 0, len(effectiveByKey))
	for _, unit := range effectiveByKey {
		result = append(result, unit)
	}
	sort.Slice(result, func(i, j int) bool {
		return changeset5ConflictUnitKey(result[i]) < changeset5ConflictUnitKey(result[j])
	})
	expectedCount := overlay.EffectivePostGovernableCount
	if pre {
		expectedCount = overlay.EffectivePreGovernableCount
	}
	if len(result) != expectedCount {
		t.Fatalf("effective governable overlay 计数不一致：actual=%d expected=%d", len(result), expectedCount)
	}
	return result
}

func changeset5EffectiveOwnership(
	overlay changeset5ConflictClassificationOverlay,
) map[string]string {
	result := make(map[string]string, len(overlay.Amendments))
	for _, amendment := range overlay.Amendments {
		result[amendment.UnitKey] = amendment.EffectiveOwnership
	}
	return result
}

func changeset5RequireStrictSubset(
	t *testing.T,
	subset []changeset5ConflictUnit,
	full []changeset5ConflictUnit,
) {
	t.Helper()
	fullSet := changeset5ConflictUnitMap(full)
	if len(subset) == 0 || len(subset) >= len(full) {
		t.Fatal("可治理清单不是严格非空子集")
	}
	for _, unit := range subset {
		if _, ok := fullSet[changeset5ConflictUnitKey(unit)]; !ok {
			t.Fatalf("可治理单元不在全量冲突清单中：%s", changeset5ConflictUnitKey(unit))
		}
	}
}

func changeset5ConflictUnitMap(units []changeset5ConflictUnit) map[string]changeset5ConflictUnit {
	result := make(map[string]changeset5ConflictUnit, len(units))
	for _, unit := range units {
		result[changeset5ConflictUnitKey(unit)] = unit
	}
	return result
}

func changeset5ConflictUnitKey(unit changeset5ConflictUnit) string {
	return strings.Join([]string{unit.Path, unit.UnitType, unit.Receiver, unit.Name}, "\x00")
}

func changeset5DeclarationKey(unit changeset5ConflictUnit) string {
	return strings.Join([]string{unit.Path, unit.Receiver, unit.Name}, "\x00")
}

func changeset5ModifiedDeclarationKeys(units []changeset5ConflictUnit) []string {
	var result []string
	for _, unit := range units {
		if unit.UnitType == "declaration_modified" {
			result = append(result, changeset5DeclarationKey(unit))
		}
	}
	sort.Strings(result)
	return result
}

func changeset5StringFingerprint(values []string) string {
	sum := sha256.Sum256([]byte(strings.Join(values, "\n")))
	return hex.EncodeToString(sum[:])
}

func changeset5FileSHA256(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	sum := sha256.Sum256(raw)
	return hex.EncodeToString(sum[:])
}
