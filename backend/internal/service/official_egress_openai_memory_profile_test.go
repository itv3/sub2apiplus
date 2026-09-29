package service

// 官方出站 HTTP 转发链的大正文内存测量用例（Codex 重连修复方案问题四第一步：压测校准）。
//
// 用途：用 Codex 形态的大请求体（默认 16.8MiB）走完整的 OpenAIGatewayService.Forward 官方出站链路，
// 测量单个请求与并发请求的内存峰值，作为请求准入放大倍数（GATEWAY_REQUEST_MEMORY_AMPLIFICATION）的依据；
// 减少正文副本的每个里程碑（M1、M2）完成后用同一用例重测。
//
// 默认跳过，只有设置 SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE=1 才运行，不进入日常门禁与 CI。
// 可选环境变量：
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_BODY_MIB：单个请求体大小（MiB，默认 16.8）；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_CONCURRENCY：同时转发的请求数（默认 1）；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_ROUNDS：重复轮数（默认 1）。
//
// 测量口径：
//   - 堆峰值：转发期间每 2 毫秒 runtime.ReadMemStats 采样 HeapInuse 取最大值，减去转发前（请求体已在内存、
//     已 GC）的 HeapInuse，得到“原文之外的额外驻留”；含原文倍数 = 1 + 额外驻留 / 请求体总字节；
//   - 累计分配：TotalAlloc 差值；
//   - cgroup：读取 /sys/fs/cgroup/memory.current 与 memory.peak（cgroup v2）。memory.peak 从容器创建起单调
//     递增，所以每次只测一种并发、一轮，并放在全新容器里运行，才能把峰值归因到本次转发；
//   - GOMEMLIMIT 与 GOGC 取自运行时实际生效值，一并输出。
//
// 上游用本文件的丢弃桩：业务请求体只按流读掉并计数，不留存、不解压，避免把测试桩自身的副本
// 计入测量（现有 httpUpstreamRecorder 会整段读入、解压再复制，约 3 倍正文）。
//
// 每次测量输出一行以 MEMPROFILE_RESULT 开头的 JSON，便于在 ARM64 上收集。

import (
	"crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

const (
	officialEgressMemoryProfileEnv            = "SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE"
	officialEgressMemoryProfileBodyMiBEnv     = "SUB2API_OFFICIAL_EGRESS_MEMORY_BODY_MIB"
	officialEgressMemoryProfileConcurrencyEnv = "SUB2API_OFFICIAL_EGRESS_MEMORY_CONCURRENCY"
	officialEgressMemoryProfileRoundsEnv      = "SUB2API_OFFICIAL_EGRESS_MEMORY_ROUNDS"
	officialEgressMemoryProfileHistoryMarker  = "__MEMORY_PROFILE_HISTORY__"
)

// officialEgressDiscardUpstream 是只丢弃业务请求体的上游桩；模型清单请求照常应答。
type officialEgressDiscardUpstream struct {
	mu               sync.Mutex
	businessRequests int
	wireBytes        int64
	encodings        []string
}

func (u *officialEgressDiscardUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	if req != nil && req.URL != nil && strings.Contains(req.URL.Path, "/codex/models") {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(codexModelsRecorderManifest)),
		}, nil
	}
	var written int64
	encoding := ""
	if req != nil {
		encoding = req.Header.Get("Content-Encoding")
		if req.Body != nil {
			written, _ = io.Copy(io.Discard, req.Body)
			_ = req.Body.Close()
		}
	}
	u.mu.Lock()
	u.businessRequests++
	u.wireBytes += written
	u.encodings = append(u.encodings, encoding)
	u.mu.Unlock()
	return newOfficialOpenAIHTTPSSECompletedResponse("resp_memory_profile"), nil
}

func (u *officialEgressDiscardUpstream) DoWithTLS(req *http.Request, proxyURL string, accountID int64, accountConcurrency int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return u.Do(req, proxyURL, accountID, accountConcurrency)
}

// officialEgressMemoryProfileText 生成不含引号、反斜杠与控制字符的正文文本，可直接放进 JSON 字符串。
func officialEgressMemoryProfileText(length int, salt int) string {
	const alphabet = "abcdefghijklmnopqrstuvwxyz0123456789 执行测试检查输出结果并继续下一步 "
	runes := []rune(alphabet)
	var builder strings.Builder
	builder.Grow(length + 8)
	for i := 0; builder.Len() < length; i++ {
		_, _ = builder.WriteRune(runes[(i*7+salt)%len(runes)])
	}
	return builder.String()
}

// officialEgressMemoryProfileEncrypted 生成与真实 encrypted_content 同类的随机 base64 密文（压缩不动）。
func officialEgressMemoryProfileEncrypted(t *testing.T, rawBytes int) string {
	t.Helper()
	raw := make([]byte, rawBytes)
	_, err := rand.Read(raw)
	require.NoError(t, err)
	return "gAAAAAB" + base64.StdEncoding.EncodeToString(raw)
}

// buildOfficialEgressMemoryProfileBody 在现有官方出站测试正文模板上注入 Codex 形态的历史轮次，
// 直到请求体达到 targetBytes。每轮依次是：用户消息、带加密推理内容的 reasoning、exec 工具调用、
// 工具输出、助手回复；随机 base64 加密内容约占正文 87%，zstd 压缩后约为原文九成，接近 docs/bug.md
// 记录的真实正文（大头是压不动的加密推理内容，压缩输出与原文相当），避免低估压缩输出这一份驻留。
func buildOfficialEgressMemoryProfileBody(t *testing.T, targetBytes int) []byte {
	t.Helper()
	template := newOfficialOpenAIHTTPTestBody(t, true, false, true)
	// 模板由 map 序列化，键按字母序排列；先定位最后一轮用户消息的文本，再回退到该对象的起始花括号
	// （该对象没有嵌套对象，最近的“{”就是它的起点），历史轮次插在它前面。
	textAt := strings.Index(string(template), `"执行测试"`)
	require.Positive(t, textAt, "模板正文缺少最后一轮用户消息，无法注入历史")
	insertAt := strings.LastIndex(string(template[:textAt]), "{")
	require.Positive(t, insertAt, "模板正文最后一轮用户消息的起点定位失败")

	var history strings.Builder
	history.Grow(targetBytes + 64*1024)
	for turn := 0; len(template)+history.Len() < targetBytes; turn++ {
		callID := "call_memory_profile_" + strconv.Itoa(turn)
		items := []string{
			`{"type":"message","role":"user","content":[{"type":"input_text","text":"` + officialEgressMemoryProfileText(300, turn) + `"}]}`,
			`{"type":"reasoning","summary":[{"type":"summary_text","text":"` + officialEgressMemoryProfileText(100, turn+1) + `"}],"encrypted_content":"` + officialEgressMemoryProfileEncrypted(t, 10000) + `"}`,
			`{"type":"custom_tool_call","name":"exec","call_id":"` + callID + `","input":"` + officialEgressMemoryProfileText(200, turn+2) + `","status":"completed"}`,
			`{"type":"custom_tool_call_output","call_id":"` + callID + `","output":"` + officialEgressMemoryProfileText(1000, turn+3) + `"}`,
			`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"` + officialEgressMemoryProfileText(400, turn+4) + `"}]}`,
		}
		for _, item := range items {
			_, _ = history.WriteString(item)
			_ = history.WriteByte(',')
		}
	}
	body := make([]byte, 0, len(template)+history.Len())
	body = append(body, template[:insertAt]...)
	body = append(body, history.String()...)
	body = append(body, template[insertAt:]...)
	require.True(t, json.Valid(body), "生成的测量正文不是合法 JSON")
	return body
}

func officialEgressMemoryProfileEnvFloat(name string, fallback float64) float64 {
	if value, err := strconv.ParseFloat(strings.TrimSpace(os.Getenv(name)), 64); err == nil && value > 0 {
		return value
	}
	return fallback
}

func officialEgressMemoryProfileEnvInt(name string, fallback int) int {
	if value, err := strconv.Atoi(strings.TrimSpace(os.Getenv(name))); err == nil && value > 0 {
		return value
	}
	return fallback
}

// readOfficialEgressMemoryProfileCgroup 读取 cgroup v2 的内存计数（字节），文件不存在时返回 -1。
func readOfficialEgressMemoryProfileCgroup(name string) int64 {
	raw, err := os.ReadFile("/sys/fs/cgroup/" + name)
	if err != nil {
		return -1
	}
	value, err := strconv.ParseInt(strings.TrimSpace(string(raw)), 10, 64)
	if err != nil {
		return -1
	}
	return value
}

func officialEgressMemoryProfileMiB(value float64) float64 {
	return math.Round(value/(1<<20)*10) / 10
}

// officialEgressMemoryProfileCgroupMiB 把 cgroup 计数换成 MiB；读不到（非 Linux 或非 cgroup v2）时返回 nil。
func officialEgressMemoryProfileCgroupMiB(value int64) any {
	if value < 0 {
		return nil
	}
	return officialEgressMemoryProfileMiB(float64(value))
}

// warmOfficialEgressMemoryProfile 用小正文先完整转发一次，把 zstd 编码器、画像编译等进程级一次性初始化
// 排除在测量之外（生产进程常驻，大请求到达时这些都已就绪）。
func warmOfficialEgressMemoryProfile(t *testing.T, service *OpenAIGatewayService, account *Account) {
	t.Helper()
	body := buildOfficialEgressMemoryProfileBody(t, 256<<10)
	result, err := service.Forward(t.Context(), newOfficialOpenAIHTTPTestContext(body, "/v1/responses"), account, body)
	require.NoError(t, err, "预热转发失败")
	require.NotNil(t, result, "预热转发返回空结果")
}

func TestOfficialEgressHTTPForwardMemoryProfile(t *testing.T) {
	if os.Getenv(officialEgressMemoryProfileEnv) != "1" {
		t.Skipf("内存测量用例默认跳过；设置 %s=1 才运行", officialEgressMemoryProfileEnv)
	}
	gin.SetMode(gin.TestMode)
	bodyMiB := officialEgressMemoryProfileEnvFloat(officialEgressMemoryProfileBodyMiBEnv, 16.8)
	concurrency := officialEgressMemoryProfileEnvInt(officialEgressMemoryProfileConcurrencyEnv, 1)
	rounds := officialEgressMemoryProfileEnvInt(officialEgressMemoryProfileRoundsEnv, 1)
	targetBytes := int(bodyMiB * (1 << 20))

	gcPercent := debug.SetGCPercent(-1)
	debug.SetGCPercent(gcPercent)
	memoryLimit := debug.SetMemoryLimit(-1)

	for round := 1; round <= rounds; round++ {
		upstream := &officialEgressDiscardUpstream{}
		service := &OpenAIGatewayService{
			cfg: &config.Config{Security: config.SecurityConfig{
				URLAllowlist: config.URLAllowlistConfig{Enabled: false},
			}},
			httpUpstream: upstream,
		}
		service.openaiModelCapabilities.replaceFromManifest(
			94,
			[]byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`),
		)

		account := newOfficialOpenAIHTTPTestAccount(94)
		warmOfficialEgressMemoryProfile(t, service, account)
		upstream.mu.Lock()
		upstream.businessRequests, upstream.wireBytes, upstream.encodings = 0, 0, nil
		upstream.mu.Unlock()

		bodies := make([][]byte, concurrency)
		contexts := make([]*gin.Context, concurrency)
		totalBodyBytes := 0
		for i := range bodies {
			bodies[i] = buildOfficialEgressMemoryProfileBody(t, targetBytes)
			contexts[i] = newOfficialOpenAIHTTPTestContext(bodies[i], "/v1/responses")
			totalBodyBytes += len(bodies[i])
		}

		runtime.GC()
		debug.FreeOSMemory()
		var before runtime.MemStats
		runtime.ReadMemStats(&before)
		cgroupBefore := readOfficialEgressMemoryProfileCgroup("memory.current")

		var peakHeapInuse atomic.Uint64
		peakHeapInuse.Store(before.HeapInuse)
		stop := make(chan struct{})
		samplerDone := make(chan struct{})
		go func() {
			defer close(samplerDone)
			ticker := time.NewTicker(2 * time.Millisecond)
			defer ticker.Stop()
			var stats runtime.MemStats
			for {
				select {
				case <-stop:
					return
				case <-ticker.C:
					runtime.ReadMemStats(&stats)
					for {
						current := peakHeapInuse.Load()
						if stats.HeapInuse <= current || peakHeapInuse.CompareAndSwap(current, stats.HeapInuse) {
							break
						}
					}
				}
			}
		}()

		started := time.Now()
		errs := make([]error, concurrency)
		var wg sync.WaitGroup
		for i := 0; i < concurrency; i++ {
			wg.Add(1)
			go func(index int) {
				defer wg.Done()
				result, err := service.Forward(t.Context(), contexts[index], account, bodies[index])
				if err == nil && result == nil {
					err = fmt.Errorf("Forward 返回空结果")
				}
				errs[index] = err
			}(i)
		}
		wg.Wait()
		elapsed := time.Since(started)
		close(stop)
		<-samplerDone

		var after runtime.MemStats
		runtime.ReadMemStats(&after)
		cgroupPeak := readOfficialEgressMemoryProfileCgroup("memory.peak")
		for i, err := range errs {
			require.NoError(t, err, "第 %d 个请求转发失败", i+1)
		}
		upstream.mu.Lock()
		businessRequests := upstream.businessRequests
		wireBytes := upstream.wireBytes
		encodings := append([]string(nil), upstream.encodings...)
		upstream.mu.Unlock()
		require.Equal(t, concurrency, businessRequests, "上游收到的业务请求数与并发数不一致")

		extraHeap := float64(peakHeapInuse.Load()) - float64(before.HeapInuse)
		result := map[string]any{
			"round":                       round,
			"concurrency":                 concurrency,
			"body_mib_each":               officialEgressMemoryProfileMiB(float64(totalBodyBytes) / float64(concurrency)),
			"body_mib_total":              officialEgressMemoryProfileMiB(float64(totalBodyBytes)),
			"wire_mib_total":              officialEgressMemoryProfileMiB(float64(wireBytes)),
			"wire_content_encodings":      encodings,
			"heap_inuse_before_mib":       officialEgressMemoryProfileMiB(float64(before.HeapInuse)),
			"heap_inuse_peak_mib":         officialEgressMemoryProfileMiB(float64(peakHeapInuse.Load())),
			"heap_extra_peak_mib":         officialEgressMemoryProfileMiB(extraHeap),
			"extra_multiple":              math.Round(extraHeap/float64(totalBodyBytes)*100) / 100,
			"resident_multiple_with_body": math.Round((1+extraHeap/float64(totalBodyBytes))*100) / 100,
			"total_alloc_mib":             officialEgressMemoryProfileMiB(float64(after.TotalAlloc - before.TotalAlloc)),
			"gc_cycles":                   after.NumGC - before.NumGC,
			"elapsed_ms":                  elapsed.Milliseconds(),
			"cgroup_current_before_mib":   officialEgressMemoryProfileCgroupMiB(cgroupBefore),
			"cgroup_peak_mib":             officialEgressMemoryProfileCgroupMiB(cgroupPeak),
			"gogc":                        gcPercent,
			"gomemlimit_mib":              officialEgressMemoryProfileMiB(float64(memoryLimit)),
			"go_version":                  runtime.Version(),
			"goarch":                      runtime.GOARCH,
		}
		encoded, err := json.Marshal(result)
		require.NoError(t, err)
		fmt.Printf("MEMPROFILE_RESULT %s\n", encoded)

		for i := range bodies {
			bodies[i] = nil
			contexts[i] = nil
		}
	}
}
