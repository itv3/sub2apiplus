//go:build unit

package tlsfingerprint

import (
	"reflect"
	"testing"

	"github.com/stretchr/testify/require"
)

// requireType 以两值形式断言 v 的动态类型：断言失败时立即终止用例，并报告期望与实际类型。
// 带构建标签的测试原先直接写单值断言 v.(T)，失败时 panic，也不满足 errcheck 的
// check-type-assertions；统一改走这里，保留"类型不符即失败"的语义，同时让 lint 能覆盖这些文件。
func requireType[T any](t testing.TB, v any) T {
	t.Helper()
	got, ok := v.(T)
	require.Truef(t, ok, "类型断言失败：期望 %v，实际 %T", reflect.TypeFor[T](), v)
	return got
}
