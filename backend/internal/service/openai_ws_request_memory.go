package service

import (
	"context"
	"errors"
	"math"
	"sort"
	"sync"
	"unsafe"

	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	coderws "github.com/coder/websocket"
	"github.com/tidwall/gjson"
)

// OpenAIWSRequestMemoryError 区分单帧硬上限与进程共享预算不足，供入口选择稳定关闭码。
type OpenAIWSRequestMemoryError struct {
	TooLarge bool
}

func (e *OpenAIWSRequestMemoryError) Error() string {
	if e.TooLarge {
		return "websocket request body exceeds the single request limit"
	}
	return "websocket request memory budget is temporarily exhausted; retry later"
}

func (e *OpenAIWSRequestMemoryError) closeError() *OpenAIWSClientCloseError {
	status := coderws.StatusTryAgainLater
	if e.TooLarge {
		status = coderws.StatusMessageTooBig
	}
	return &OpenAIWSClientCloseError{statusCode: status, reason: e.Error(), err: e}
}

type openAIWSMemoryRange struct{ start, end uintptr }

type openAIWSOwnedBuffer struct {
	body  *pkghttputil.OwnedRequestBody
	spans []openAIWSMemoryRange
}

// OpenAIWSRequestMemory 只在连接内保存当前 owner 快照。共享正文按底层数组去重，
// 处理中正文使用放大权重，跨轮缓存只按实际仍保活的数组容量计费。
// resize 必须原子替换该连接在 HTTP/WS 公共准入器中的权重。
type OpenAIWSRequestMemory struct {
	mu              sync.Mutex
	maxBytes        int64
	fixedBytes      int64
	amplification   float64
	resize          func(int64) bool
	owners          map[string][][]byte
	roots           []openAIWSMemoryRange
	retainedBytes   int64
	frameBytes      int64
	snapshotVersion uint64
	workingBytes    int64
	readingBytes    int64
	joinBytes       int64
	ownedBodies     bool
	ownedBuffers    []openAIWSOwnedBuffer
	readers         sync.WaitGroup
	closed          bool
}

func NewOpenAIWSRequestMemory(maxBytes, fixedBytes int64, amplification float64, resize func(int64) bool) *OpenAIWSRequestMemory {
	return &OpenAIWSRequestMemory{
		maxBytes: maxBytes, fixedBytes: fixedBytes, amplification: math.Max(1, amplification),
		resize: resize, owners: make(map[string][][]byte),
	}
}

// EnableOwnedBodies 仅供已审计正文借用生命周期的入口显式开启。
// 默认仍使用 Go 堆正文，兼容把裸切片持有到连接之外的旧调用方。
func (m *OpenAIWSRequestMemory) EnableOwnedBodies() {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if !m.closed {
		m.ownedBodies = true
	}
}

// DisableOwnedBodies 只影响之后的读取；已有映射仍由原持有点保护，不能就地释放。
func (m *OpenAIWSRequestMemory) DisableOwnedBodies() {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.ownedBodies = false
}

func (m *OpenAIWSRequestMemory) ownedBodiesEnabled() bool {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.ownedBodies && !m.closed
}

// 先在锁内登记读者，再启动 goroutine，避免 Close 的 Wait 与新的 Add 交错。
func (m *OpenAIWSRequestMemory) startReader() error {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.closed {
		return (&OpenAIWSRequestMemoryError{}).closeError()
	}
	m.readers.Add(1)
	return nil
}

func (m *OpenAIWSRequestMemory) finishReader() {
	if m != nil {
		m.readers.Done()
	}
}

type openAIWSRequestMemoryContextKey struct{}

// takeOpenAIWSClientPayload 把入口正文移交给下一层，同时清除调用层的切片引用。
// 正文仍按只读约定共享；此处不复制，也不改变重试或审计各自持有的当前轮原文。
func takeOpenAIWSClientPayload(payload *[]byte) []byte {
	body := *payload
	*payload = nil
	return body
}

func WithOpenAIWSRequestMemory(ctx context.Context, memory *OpenAIWSRequestMemory) context.Context {
	return context.WithValue(ctx, openAIWSRequestMemoryContextKey{}, memory)
}

func openAIWSRequestMemoryFromContext(ctx context.Context) *OpenAIWSRequestMemory {
	if ctx == nil {
		return nil
	}
	memory, _ := ctx.Value(openAIWSRequestMemoryContextKey{}).(*OpenAIWSRequestMemory)
	return memory
}

// Retain 替换一个持有点，不能向旧快照累加历史。切片头本身由快照持有，保证
// 计量使用的地址始终有效；快照淘汰时根范围也同步淘汰，不依赖 GC 或 finalizer。
func (m *OpenAIWSRequestMemory) Retain(owner string, payloads ...[]byte) error {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.applyLocked(map[string][][]byte{owner: payloads}, m.workingBytes, m.readingBytes, m.joinBytes)
}

// BeginAttempt 在换账号时用新的首包接管工作区，原服务调用已退出，其缓存一并撤销。
func (m *OpenAIWSRequestMemory) BeginAttempt(payload []byte) error {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.applyLocked(map[string][][]byte{"frame": {payload}, "service": nil, "passthrough": nil, "working-root": nil}, int64(len(payload)), 0, 0)
}

// FinishTurn 在下一次收帧之前移交仍被重放状态持有的数组，再撤销本轮加工权重。
func (m *OpenAIWSRequestMemory) FinishTurn(owner string, payloads ...[]byte) error {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.applyLocked(map[string][][]byte{owner: payloads, "frame": nil, "working-root": nil}, 0, 0, 0)
}

func (m *OpenAIWSRequestMemory) EnsureWorkingBytes(bytes int64) error {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if bytes <= m.workingBytes {
		return nil
	}
	return m.applyLocked(nil, bytes, m.readingBytes, m.joinBytes)
}

func (m *OpenAIWSRequestMemory) beginRead() error {
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.applyLocked(map[string][][]byte{"frame": nil}, 0, 0, 0); err != nil {
		return err
	}
	// 轮次边界前已登记完 handler、replay、重试和 context 的所有持有点。
	// Retain 本身不释放映射，允许调用方在同步交接期间先撤销旧名称再登记新名称。
	return m.releaseUnusedOwnedLocked(nil)
}

func (m *OpenAIWSRequestMemory) growRead(bytes, joinBytes int64) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.applyLocked(nil, 0, bytes, joinBytes)
}

func (m *OpenAIWSRequestMemory) commitRead(payload []byte) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.applyLocked(map[string][][]byte{"frame": {payload}}, int64(len(payload)), 0, 0)
}

func (m *OpenAIWSRequestMemory) commitOwnedRead(body *pkghttputil.OwnedRequestBody) error {
	m.mu.Lock()
	defer m.mu.Unlock()
	buffer := openAIWSOwnedBuffer{body: body}
	for _, payload := range body.RetainedBuffers() {
		if cap(payload) > 0 {
			start := uintptr(unsafe.Pointer(unsafe.SliceData(payload)))
			buffer.spans = append(buffer.spans, openAIWSMemoryRange{start: start, end: start + uintptr(cap(payload))})
		}
	}
	m.ownedBuffers = append(m.ownedBuffers, buffer)
	if err := m.applyLocked(map[string][][]byte{"frame": {body.Bytes()}}, int64(len(body.Bytes())), 0, 0); err != nil {
		m.ownedBuffers = m.ownedBuffers[:len(m.ownedBuffers)-1]
		return err
	}
	return nil
}

func (m *OpenAIWSRequestMemory) abortRead() {
	if m == nil {
		return
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	_ = m.applyLocked(map[string][][]byte{"frame": nil}, 0, 0, 0)
}

// discardRead 只用于取消方丢弃尚未交付的成功读结果，调用前必须 join 读协程。
// 普通失败的本次租约由读取函数关闭；其余已登记的跨轮映射仍留到下一次 beginRead。
func (m *OpenAIWSRequestMemory) discardRead(body *pkghttputil.OwnedRequestBody) error {
	if m == nil {
		return nil
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if err := m.applyLocked(map[string][][]byte{"frame": nil}, 0, 0, 0); err != nil {
		return err
	}
	if body == nil {
		return nil
	}
	return m.releaseUnusedOwnedLocked(body)
}

// Close 先禁止新增读者，再等待已登记读协程结束，最后同步释放映射。
// 调用方应先关闭连接或取消读取；本方法不替代传输层取消，也不允许尚有正文消费者时关闭。
func (m *OpenAIWSRequestMemory) Close() {
	if m == nil {
		return
	}
	m.mu.Lock()
	m.closed = true
	m.mu.Unlock()
	m.readers.Wait()
	m.mu.Lock()
	defer m.mu.Unlock()
	m.owners = nil
	m.roots = nil
	m.frameBytes = 0
	m.workingBytes, m.readingBytes, m.joinBytes = 0, 0, 0
	if err := m.releaseUnusedOwnedLocked(nil); err != nil {
		// munmap 失败时保留租约和实际计费，后续 Close 允许重试；不能报告已释放。
		logOpenAIWSModeInfo("ingress_ws_owned_body_release_failed cause=%v", err)
	}
}

func openAIWSRangesOverlap(roots []openAIWSMemoryRange, span openAIWSMemoryRange) bool {
	index := sort.Search(len(roots), func(i int) bool { return roots[i].end > span.start })
	return index < len(roots) && roots[index].start < span.end
}

func (m *OpenAIWSRequestMemory) retainedBytesLocked(roots []openAIWSMemoryRange) int64 {
	var retained int64
	for _, span := range roots {
		retained += int64(span.end - span.start)
	}
	for _, buffer := range m.ownedBuffers {
		for _, span := range buffer.spans {
			if !openAIWSRangesOverlap(roots, span) {
				// 已撤销引用但尚未到释放边界的映射同样占系统内存，不能提前归还预算。
				retained += int64(span.end - span.start)
			}
		}
	}
	return retained
}

func (m *OpenAIWSRequestMemory) releaseUnusedOwnedLocked(only *pkghttputil.OwnedRequestBody) error {
	var releaseErr error
	kept := m.ownedBuffers[:0]
	for _, buffer := range m.ownedBuffers {
		retained := only != nil && buffer.body != only
		for _, span := range buffer.spans {
			retained = retained || openAIWSRangesOverlap(m.roots, span)
		}
		if !retained {
			if err := buffer.body.Close(); err != nil {
				releaseErr = errors.Join(releaseErr, err)
				retained = true
			}
		}
		if retained {
			kept = append(kept, buffer)
		}
	}
	clear(m.ownedBuffers[len(kept):])
	m.ownedBuffers = kept
	retained := m.retainedBytesLocked(m.roots)
	if err := m.resizeWeightLocked(retained, m.frameBytes, m.workingBytes, m.readingBytes, m.joinBytes); err != nil {
		return errors.Join(releaseErr, err)
	}
	m.retainedBytes = retained
	return releaseErr
}

func (m *OpenAIWSRequestMemory) applyLocked(changes map[string][][]byte, working, reading, joining int64) error {
	if m.closed {
		return (&OpenAIWSRequestMemoryError{}).closeError()
	}
	// 读取同一帧期间 owner 不变。每个 32 KiB 块只调整数字，避免反复扫描、
	// 排序上一轮的大量 replay 切片；失败也不会改变已有工作区权重。
	if changes == nil {
		if err := m.resizeWeightLocked(m.retainedBytes, m.frameBytes, working, reading, joining); err != nil {
			return err
		}
		m.workingBytes, m.readingBytes, m.joinBytes = working, reading, joining
		return nil
	}
	owners := make(map[string][][]byte, len(m.owners)+len(changes))
	for name, payloads := range m.owners {
		owners[name] = payloads
	}
	for name, payloads := range changes {
		if len(payloads) == 0 {
			delete(owners, name)
		} else {
			owners[name] = append([][]byte(nil), payloads...)
		}
	}
	var ranges []openAIWSMemoryRange
	var frameBytes int64
	for owner, payloads := range owners {
		for _, payload := range payloads {
			if cap(payload) == 0 {
				continue
			}
			start := uintptr(unsafe.Pointer(unsafe.SliceData(payload)))
			span := openAIWSMemoryRange{start: start, end: start + uintptr(cap(payload))}
			// RawMessage 常是源正文的子切片；已知根补回其仍保活的前缀。
			index := sort.Search(len(m.roots), func(i int) bool { return m.roots[i].end > span.start })
			if index < len(m.roots) && m.roots[index].start <= span.start && m.roots[index].end >= span.end {
				span = m.roots[index]
			}
			for _, buffer := range m.ownedBuffers {
				for _, ownedSpan := range buffer.spans {
					if ownedSpan.start <= span.start && ownedSpan.end >= span.end {
						span = ownedSpan
					}
				}
			}
			ranges = append(ranges, span)
			if owner == "frame" {
				frameBytes += int64(span.end - span.start)
			}
		}
	}
	sort.Slice(ranges, func(i, j int) bool { return ranges[i].start < ranges[j].start })
	roots := ranges[:0]
	for _, span := range ranges {
		if len(roots) > 0 && span.start < roots[len(roots)-1].end {
			if span.end > roots[len(roots)-1].end {
				roots[len(roots)-1].end = span.end
			}
		} else {
			roots = append(roots, span)
		}
	}
	retained := m.retainedBytesLocked(roots)
	if err := m.resizeWeightLocked(retained, frameBytes, working, reading, joining); err != nil {
		return err
	}
	m.owners, m.roots = owners, roots
	m.retainedBytes, m.frameBytes = retained, frameBytes
	m.snapshotVersion++
	m.workingBytes, m.readingBytes, m.joinBytes = working, reading, joining
	return nil
}

func (m *OpenAIWSRequestMemory) resizeWeightLocked(retained, frameBytes, working, reading, joining int64) error {
	weight := float64(retained)
	if working > 0 {
		weight += math.Max(float64(working), float64(frameBytes))*m.amplification - float64(frameBytes)
	}
	if reading > 0 {
		weight += math.Max(float64(reading)*m.amplification, float64(reading)+float64(joining))
	}
	if weight > 0 {
		weight += float64(m.fixedBytes)
	}
	if weight >= float64(math.MaxInt64) || (m.resize != nil && !m.resize(int64(math.Ceil(weight)))) {
		return (&OpenAIWSRequestMemoryError{}).closeError()
	}
	return nil
}

func openAIWSRetainedReplayPayloads(payloads [][]byte, states ...openAIWSReplayInputState) [][]byte {
	for _, state := range states {
		for _, item := range state.items {
			payloads = append(payloads, item)
		}
	}
	return payloads
}

// 在重建 input 之前按将要生成的完整正文扩展工作区，不能只按本轮小增量计费。
func openAIWSReplayWorkingBytes(payload []byte, state openAIWSReplayInputState) int64 {
	size := int64(len(payload))
	if !state.exists || state.unavailable {
		return size
	}
	input := gjson.Get(openAIWSPayloadStringView(payload), "input")
	rebuilt := size - int64(len(input.Raw)) + state.rawBytes + int64(len(state.items)) + 2
	if !input.Exists() {
		// 新增字段还需要键名、冒号及可能的分隔逗号；空对象可多预留一个字节。
		rebuilt += int64(len(`,"input":`))
	}
	// json.Marshal([]json.RawMessage) 会转义 HTML 字符与两个 Unicode 分隔符。
	// 原始字节数不是序列化长度上界；这里只扫描补计，不提前生成另一份大正文。
	// 无意义空白可能在 Marshal 时删除，保留它们的计数得到保守上界即可。
	for _, item := range state.items {
		for i := 0; i < len(item); i++ {
			switch item[i] {
			case '<', '>', '&':
				rebuilt += 5
			case 0xe2:
				if i+2 < len(item) && item[i+1] == 0x80 && (item[i+2] == 0xa8 || item[i+2] == 0xa9) {
					rebuilt += 3
					i += 2
				}
			}
		}
	}
	if rebuilt > size {
		return rebuilt
	}
	return size
}
