package finalwirecontract

import "testing"

func TestCompareRequiresExactApprovedDelta(t *testing.T) {
	before := map[string]any{"body": map[string]any{"digest": "before"}}
	after := map[string]any{"body": map[string]any{"digest": "after"}}
	result, err := Compare("capture", before, after, nil)
	if err != nil {
		t.Fatal(err)
	}
	if result.OK() || len(result.Unexpected) != 1 || result.Unexpected[0].Path != "/body/digest" {
		t.Fatalf("严格比较器未报告差异：%+v", result)
	}
	difference := result.Unexpected[0]
	result, err = Compare("capture", before, after, []ApprovedDelta{{
		CaptureKey: "capture", Path: difference.Path,
		BeforeSHA256: difference.BeforeSHA256, AfterSHA256: difference.AfterSHA256,
		Reason: "测试精确批准",
	}})
	if err != nil {
		t.Fatal(err)
	}
	if !result.OK() || len(result.Applied) != 1 {
		t.Fatalf("精确 approved delta 未生效：%+v", result)
	}
}

func TestCompareRejectsUnusedOrWrongApprovedDelta(t *testing.T) {
	approval := ApprovedDelta{
		CaptureKey: "capture", Path: "/body/digest",
		BeforeSHA256: missingFingerprint, AfterSHA256: missingFingerprint,
		Reason: "错误批准",
	}
	result, err := Compare(
		"capture",
		map[string]any{"body": map[string]any{"digest": "before"}},
		map[string]any{"body": map[string]any{"digest": "after"}},
		[]ApprovedDelta{approval},
	)
	if err != nil {
		t.Fatal(err)
	}
	if result.OK() || len(result.Unexpected) != 1 || len(result.Unused) != 1 {
		t.Fatalf("错误 approved delta 意外放行：%+v", result)
	}
}
