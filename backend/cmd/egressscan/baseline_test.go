package main

import (
	"bytes"
	"encoding/json"
	"os"
	"testing"
)

func loadCheckedInBaseline(t *testing.T) []byte {
	t.Helper()
	raw, err := os.ReadFile("../../../docs/egress/foundation/sink-baseline.json")
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestBaselineStrictValidationRejectsFalseGreenInputs(t *testing.T) {
	raw := loadCheckedInBaseline(t)
	baseline, err := decodeBaseline(raw)
	if err != nil {
		t.Fatalf("当前基线无法严格解析：%v", err)
	}

	t.Run("未知字段", func(t *testing.T) {
		mutated := bytes.Replace(raw,
			[]byte(`"scan_pattern":`),
			[]byte(`"unknown_field":true,"scan_pattern":`), 1)
		if _, err := decodeBaseline(mutated); err == nil {
			t.Fatal("未知字段未被拒绝")
		}
	})

	t.Run("尾随 JSON", func(t *testing.T) {
		if _, err := decodeBaseline(append(append([]byte(nil), raw...), []byte(`{}`)...)); err == nil {
			t.Fatal("尾随 JSON 未被拒绝")
		}
	})

	t.Run("重复候选", func(t *testing.T) {
		mutated := baseline
		mutated.Sinks = append(mutated.Sinks, mutated.Sinks[0])
		encoded, err := json.Marshal(mutated)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := decodeBaseline(encoded); err == nil {
			t.Fatal("重复 scan_candidate_id 未被拒绝")
		}
	})

	t.Run("请求事实自相矛盾", func(t *testing.T) {
		mutated := baseline
		mutated.Sinks = append([]SinkRecord(nil), baseline.Sinks...)
		for index := range mutated.Sinks {
			if len(mutated.Sinks[index].ResolvedTargets) == 0 {
				continue
			}
			mutated.Sinks[index].ResolvedPaths = []string{"/tampered"}
			break
		}
		encoded, err := json.Marshal(mutated)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := decodeBaseline(encoded); err == nil {
			t.Fatal("与 target 不一致的 host/path 未被拒绝")
		}
	})

	t.Run("构建矩阵缺失", func(t *testing.T) {
		mutated := baseline
		mutated.BuildContexts = nil
		encoded, err := json.Marshal(mutated)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := decodeBaseline(encoded); err == nil {
			t.Fatal("空 build_contexts 未被拒绝")
		}
	})
}

func TestCheckedInBaselineStatsAreFresh(t *testing.T) {
	baselineRaw := loadCheckedInBaseline(t)
	baseline, err := decodeBaseline(baselineRaw)
	if err != nil {
		t.Fatalf("当前基线无法严格解析：%v", err)
	}
	checkedIn, err := os.ReadFile("../../../docs/egress/foundation/sink-stats.md")
	if err != nil {
		t.Fatal(err)
	}
	want := renderBaselineStats(baseline)
	if !bytes.Equal(want, checkedIn) {
		t.Fatal("sink-stats.md 已与 sink-baseline.json 漂移，请重新生成")
	}
}

func TestCheckedInBootstrapCommitIsPinned(t *testing.T) {
	baseline, err := decodeBaseline(loadCheckedInBaseline(t))
	if err != nil {
		t.Fatalf("当前基线无法严格解析：%v", err)
	}
	if baseline.BootstrapCommit != legacyBootstrapCommit {
		t.Fatalf("bootstrap commit 漂移：实际 %s，固定锚点 %s",
			baseline.BootstrapCommit, legacyBootstrapCommit)
	}
}

func TestApplyClassificationEmptyInput(t *testing.T) {
	classified, unclassified := applyClassification(nil)
	if len(classified) != 0 || len(unclassified) != 0 {
		t.Fatalf("空发送面分类结果非法：classified=%v unclassified=%v", classified, unclassified)
	}
}
