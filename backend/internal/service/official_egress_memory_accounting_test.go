package service

import (
	"sync/atomic"
	"testing"

	"github.com/stretchr/testify/require"
)

// officialEgressMemoryPeak 把同一采样时刻的 Go 堆与显式管理的正文内存合并。
// 系统分配的正文按完整映射容量计入，不能因不属于 Go 堆而从目标中消失。
type officialEgressMemoryPeak struct {
	heap  atomic.Uint64
	owned atomic.Uint64
	total atomic.Uint64
}

func (p *officialEgressMemoryPeak) observe(heap, owned uint64) {
	officialEgressMemoryStorePeak(&p.heap, heap)
	officialEgressMemoryStorePeak(&p.owned, owned)
	officialEgressMemoryStorePeak(&p.total, heap+owned)
}

func officialEgressMemoryStorePeak(peak *atomic.Uint64, value uint64) {
	for old := peak.Load(); value > old; old = peak.Load() {
		if peak.CompareAndSwap(old, value) {
			return
		}
	}
}

func TestOfficialEgressMemoryPeakIncludesOwnedBodyAtSameInstant(t *testing.T) {
	var peak officialEgressMemoryPeak
	peak.observe(10, 30)
	peak.observe(25, 5)
	peak.observe(8, 0)
	require.EqualValues(t, 25, peak.heap.Load())
	require.EqualValues(t, 30, peak.owned.Load())
	require.EqualValues(t, 40, peak.total.Load(), "必须记录实际同时存在的总量，不漏计系统内存，也不累加不同时间的峰值")
}
