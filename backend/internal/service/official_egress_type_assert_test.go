package service

import (
	"fmt"
	"testing"
)

// typeAs 以两值形式断言 v 的动态类型为 T：不符时立即终止用例并报告实际类型。
//
// 与 require_type_test.go 里的 requireType 语义相同。区别只在构建标签：requireType 只在 unit 标签下编译，
// 而 0.164 登记新增的一批出站测试文件没有构建标签，默认编译（不带 -tags 的 golangci-lint 与 go vet）也会看到它们；
// 它们里面的单值断言 v.(T) 会被 errcheck（check-type-assertions）报告，所以用这个不带标签的同名语义函数。
func typeAs[T any](t testing.TB, v any) T {
	t.Helper()
	typed, ok := v.(T)
	if !ok {
		var want T
		t.Fatalf("类型断言失败：期望 %T，实际 %s", want, fmt.Sprintf("%T", v))
	}
	return typed
}
