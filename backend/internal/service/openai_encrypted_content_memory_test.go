package service

import (
	"crypto/sha256"
	"math"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// minAllocsPerRun 多轮 testing.AllocsPerRun 取最小值，用于「不得分配」类断言。
//
// AllocsPerRun 读的是进程级 runtime.MemStats.Mallocs：测量窗口里包内其它 goroutine（前面用例留下的日志刷写、ticker、
// 连接清理等）的分配也会算进来。负载高时这类突发更容易落进窗口——ARM64 入口空跑里本用例的被测函数实测记成 10 次分配，
// 同一提交的后台验证里是 0（m9）。突发噪声不会每一轮都撞上，取最小值即可滤掉；被测函数若真的分配，每一轮都至少 1 次，
// 最小值仍非零，检测力不变。不在测量前调 runtime.GC：它会让 finalizer 排队，finalizer goroutine 随后在测量窗口里运行，
// 反而引入分配。
func minAllocsPerRun(rounds, runs int, f func()) float64 {
	best := math.Inf(1)
	for round := 0; round < rounds && best > 0; round++ {
		best = math.Min(best, testing.AllocsPerRun(runs, f))
	}
	return best
}

func TestOpenAIEncryptedContentSHA256DoesNotCopyLargeString(t *testing.T) {
	for _, value := range []string{"", "中文与转义\\内容", strings.Repeat("gAAAAABrandomEncryptedContent", 64<<10)} {
		expected := sha256.Sum256([]byte(value))
		var digest [sha256.Size]byte
		allocations := minAllocsPerRun(5, 5, func() { digest = openAIEncryptedContentSHA256(value) })
		require.Equal(t, expected, digest)
		require.Zero(t, allocations, "哈希不得为长密文复制临时字节切片")
	}
}
