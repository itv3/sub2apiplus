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
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_ROUNDS：重复轮数（默认 1）；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_SHAPE：正文形态，支持 native（默认）、explicit_instructions、
//     incompressible；最后一种使用跨多个 zstd 块的长密文和随机文本，测试更难压缩的合法 JSON；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_FIXTURE：固定正文文件；存在时读取，不存在时在测量前生成并保存。
//     对照版本使用同一文件，并保持 SHAPE 一致；指定文件后，BODY_MIB 仅在首次生成时生效；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_STAGE：forward（默认）或 read_forward；后者从文件流读取
//     正文开始计量，覆盖已准入的入口读取／解压到转发，不包含鉴权与账号调度；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_INGRESS_ENCODING：identity（默认）、chunked 或 zstd；仅
//     read_forward 使用，样本编码在采样前完成，入站源文件不在测量窗口内整段驻留 Go 堆；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_WIRE_FIXTURE：可选的已编码入站文件；容器测量压缩入口时
//     在容器外预生成该文件，避免样本编码与临时文件页缓存污染容器历史峰值；
//   - SUB2API_OFFICIAL_EGRESS_MEMORY_REQUESTS_PER_SLOT：每个并发槽在测量窗口内依次转发的次数（默认 1，口径与
//     过去相同）。默认口径测的是单个请求，而测量前的 runtime.GC 与 debug.FreeOSMemory 是两次 GC，sync.Pool
//     里的空闲对象这时已被清空；大于 1 时同一进程里的请求一个接一个地到达，测跨请求复用的对象（例如编译器
//     常驻复用的 zstd 编码器）与请求间留下的垃圾在持续负载下对峰值的影响，累计分配另按请求数平均输出。
//
// 测量口径：
//   - 堆峰值：转发期间每 2 毫秒 runtime.ReadMemStats 采样 HeapInuse，连同转发结束时的 HeapInuse 取最大值，
//     减去转发前（请求体已在内存、已 GC）的 HeapInuse，得到“原文之外的额外驻留”；含原文倍数 = 1 + 额外驻留 /
//     请求体总字节。转发结束时的值也要计入：小正文一次转发只有十毫秒上下、只采到几次，最后一次采样之后的
//     分配（例如压缩时新建的编码器，转发返回后仍占着堆）会被漏掉；
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
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
	"github.com/gin-gonic/gin"
	"github.com/klauspost/compress/zstd"
	"github.com/shirou/gopsutil/v4/process"
	"github.com/stretchr/testify/require"
)

const (
	officialEgressMemoryProfileEnv            = "SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE"
	officialEgressMemoryProfileBodyMiBEnv     = "SUB2API_OFFICIAL_EGRESS_MEMORY_BODY_MIB"
	officialEgressMemoryProfileConcurrencyEnv = "SUB2API_OFFICIAL_EGRESS_MEMORY_CONCURRENCY"
	officialEgressMemoryProfileRoundsEnv      = "SUB2API_OFFICIAL_EGRESS_MEMORY_ROUNDS"
	officialEgressMemoryProfileRequestsEnv    = "SUB2API_OFFICIAL_EGRESS_MEMORY_REQUESTS_PER_SLOT"
	officialEgressMemoryProfileShapeEnv       = "SUB2API_OFFICIAL_EGRESS_MEMORY_SHAPE"
	officialEgressMemoryProfileFixtureEnv     = "SUB2API_OFFICIAL_EGRESS_MEMORY_FIXTURE"
	officialEgressMemoryProfileHistoryMarker  = "__MEMORY_PROFILE_HISTORY__"
	officialEgressMemoryProfileNative         = "native"
	officialEgressMemoryProfileExplicit       = "explicit_instructions"
	officialEgressMemoryProfileIncompressible = "incompressible"
)

// officialEgressDiscardUpstream 是只丢弃业务请求体的上游桩；模型清单请求照常应答。
type officialEgressDiscardUpstream struct {
	mu               sync.Mutex
	businessRequests int
	wireBytes        int64
	encodings        []string
	contentLengths   []int64
}

func (u *officialEgressDiscardUpstream) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	if req != nil && req.URL != nil && strings.Contains(req.URL.Path, "/codex/models") {
		return &http.Response{
			StatusCode: http.StatusOK,
			Header:     http.Header{"Content-Type": []string{"application/json"}},
			Body:       io.NopCloser(strings.NewReader(codexModelsRecorderManifest)),
		}, nil
	}
	if req == nil || req.Body == nil {
		return nil, fmt.Errorf("内存测量上游收到空请求或空正文")
	}
	// io.Discard 用固定大小缓冲消费正文；不要改成 io.ReadAll、解压或保存 req，避免把测试桩的副本计入峰值。
	written, readErr := io.Copy(io.Discard, req.Body)
	closeErr := req.Body.Close()
	if readErr != nil {
		return nil, fmt.Errorf("内存测量上游读取正文失败: %w", readErr)
	}
	if closeErr != nil {
		return nil, fmt.Errorf("内存测量上游关闭正文失败: %w", closeErr)
	}
	if req.ContentLength < 0 || written != req.ContentLength {
		return nil, fmt.Errorf("内存测量上游正文长度不匹配: 已读 %d, ContentLength %d", written, req.ContentLength)
	}
	u.mu.Lock()
	u.businessRequests++
	u.wireBytes += written
	u.encodings = append(u.encodings, req.Header.Get("Content-Encoding"))
	u.contentLengths = append(u.contentLengths, req.ContentLength)
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

// officialEgressMemoryProfileEncrypted 生成与真实 encrypted_content 同类的随机 base64 密文。
// 随机原始字节难以压缩，base64 仍有编码冗余；压缩率以输出 wire 字节实测为准。
func officialEgressMemoryProfileEncrypted(t *testing.T, rawBytes int) string {
	t.Helper()
	raw := make([]byte, rawBytes)
	_, err := rand.Read(raw)
	require.NoError(t, err)
	return "gAAAAAB" + base64.StdEncoding.EncodeToString(raw)
}

// buildOfficialEgressMemoryProfileBody 在现有官方出站测试正文模板上注入 Codex 形态的历史轮次，
// 直到请求体达到 targetBytes。每轮依次是：用户消息、带加密推理内容的 reasoning、exec 工具调用、
// 工具输出、助手回复；随机 base64 加密内容占主要比例。所有随机数据只在采样前生成，
// 以免随机数生成与样本构造的分配进入转发峰值；固定正文文件可保证改造前后的输入逐字节一致。
func buildOfficialEgressMemoryProfileBody(t *testing.T, targetBytes int) []byte {
	return buildOfficialEgressMemoryProfileBodyWithShape(t, targetBytes, officialEgressMemoryProfileNative)
}

func buildOfficialEgressMemoryProfileBodyWithShape(t *testing.T, targetBytes int, shape string) []byte {
	t.Helper()
	template := newOfficialOpenAIHTTPTestBody(t, true, shape == officialEgressMemoryProfileExplicit, true)
	// 模板由 map 序列化，键按字母序排列；先定位最后一轮用户消息的文本，再回退到该对象的起始花括号
	// （该对象没有嵌套对象，最近的“{”就是它的起点），历史轮次插在它前面。
	textAt := bytes.Index(template, []byte(`"执行测试"`))
	require.Positive(t, textAt, "模板正文缺少最后一轮用户消息，无法注入历史")
	insertAt := bytes.LastIndexByte(template[:textAt], '{')
	require.Positive(t, insertAt, "模板正文最后一轮用户消息的起点定位失败")

	var history strings.Builder
	history.Grow(targetBytes + 64*1024)
	text := func(length, salt int) string {
		if shape == officialEgressMemoryProfileIncompressible {
			return officialEgressMemoryProfileRandomText(t, length)
		}
		return officialEgressMemoryProfileText(length, salt)
	}
	encryptedRawBytes := 10000
	if shape == officialEgressMemoryProfileIncompressible {
		// 长密文横跨多个压缩块，减少块内可匹配的字段结构；用于覆盖压缩结果接近原文的压力形态。
		encryptedRawBytes = 256 << 10
	}
	for turn := 0; len(template)+history.Len() < targetBytes; turn++ {
		callID := "call_memory_profile_" + strconv.Itoa(turn)
		items := []string{
			`{"type":"message","role":"user","content":[{"type":"input_text","text":"` + text(300, turn) + `"}]}`,
			`{"type":"reasoning","summary":[{"type":"summary_text","text":"` + text(100, turn+1) + `"}],"encrypted_content":"` + officialEgressMemoryProfileEncrypted(t, encryptedRawBytes) + `"}`,
			`{"type":"custom_tool_call","name":"exec","call_id":"` + callID + `","input":"` + text(200, turn+2) + `","status":"completed"}`,
			`{"type":"custom_tool_call_output","call_id":"` + callID + `","output":"` + text(1000, turn+3) + `"}`,
			`{"type":"message","role":"assistant","content":[{"type":"output_text","text":"` + text(400, turn+4) + `"}]}`,
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

// officialEgressMemoryProfileRandomText 在 JSON 无需转义的 ASCII 字符中抽样。
// 此形态用于压低文本部分的可压缩性，并不声称合法 JSON 可以做到完全不可压缩。
func officialEgressMemoryProfileRandomText(t *testing.T, length int) string {
	t.Helper()
	const alphabet = " !#$%&'()*+,-./0123456789:;<=>?@ABCDEFGHIJKLMNOPQRSTUVWXYZ[]^_`abcdefghijklmnopqrstuvwxyz{|}~"
	raw := make([]byte, length)
	_, err := rand.Read(raw)
	require.NoError(t, err)
	for i := range raw {
		raw[i] = alphabet[int(raw[i])%len(alphabet)]
	}
	return string(raw)
}

// loadOfficialEgressMemoryProfileBody 只在测量窗口外读取或生成正文。
// 每个并发槽拥有独立切片，避免共享一份 fixture 时把原文常驻字节数重复计入分母。
func loadOfficialEgressMemoryProfileBody(t *testing.T, targetBytes int, shape, fixture string) []byte {
	t.Helper()
	if fixture == "" {
		return buildOfficialEgressMemoryProfileBodyWithShape(t, targetBytes, shape)
	}
	body, err := os.ReadFile(fixture)
	if os.IsNotExist(err) {
		body = buildOfficialEgressMemoryProfileBodyWithShape(t, targetBytes, shape)
		// 使用独占创建，防止误覆盖已有对照样本；目录由调用者明确指定并提前创建。
		file, createErr := os.OpenFile(fixture, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		require.NoError(t, createErr, "创建固定测量正文失败")
		_, writeErr := file.Write(body)
		closeErr := file.Close()
		require.NoError(t, writeErr, "保存固定测量正文失败")
		require.NoError(t, closeErr, "关闭固定测量正文失败")
	} else {
		require.NoError(t, err, "读取固定测量正文失败")
	}
	require.True(t, json.Valid(body), "固定测量正文不是合法 JSON")
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

// RSS 只在提供 /proc 的系统采集；其他平台返回未知，不能用 Go 堆代替进程 RSS。
func readOfficialEgressMemoryProfileRSS() int64 {
	raw, err := os.ReadFile("/proc/self/statm")
	if err != nil {
		return -1
	}
	fields := strings.Fields(string(raw))
	if len(fields) < 2 {
		return -1
	}
	pages, err := strconv.ParseInt(fields[1], 10, 64)
	if err != nil {
		return -1
	}
	return pages * int64(os.Getpagesize())
}

func readOfficialEgressMemoryProfileCPU() float64 {
	current, err := process.NewProcess(int32(os.Getpid()))
	if err != nil {
		return -1
	}
	times, err := current.Times()
	if err != nil {
		return -1
	}
	return times.User + times.System
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
	requestsPerSlot := officialEgressMemoryProfileEnvInt(officialEgressMemoryProfileRequestsEnv, 1)
	shape := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileShapeEnv))
	if shape == "" {
		shape = officialEgressMemoryProfileNative
	}
	require.Contains(t, []string{officialEgressMemoryProfileNative, officialEgressMemoryProfileExplicit, officialEgressMemoryProfileIncompressible}, shape,
		"不支持的测量正文形态")
	fixture := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileFixtureEnv))
	stage := strings.TrimSpace(os.Getenv("SUB2API_OFFICIAL_EGRESS_MEMORY_STAGE"))
	if stage == "" {
		stage = "forward"
	}
	require.Contains(t, []string{"forward", "read_forward"}, stage)
	ingressEncoding := strings.TrimSpace(os.Getenv("SUB2API_OFFICIAL_EGRESS_MEMORY_INGRESS_ENCODING"))
	if ingressEncoding == "" {
		ingressEncoding = "identity"
	}
	require.Contains(t, []string{"identity", "chunked", "zstd"}, ingressEncoding)
	wireFixture := strings.TrimSpace(os.Getenv("SUB2API_OFFICIAL_EGRESS_MEMORY_WIRE_FIXTURE"))
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
		upstream.businessRequests, upstream.wireBytes, upstream.encodings, upstream.contentLengths = 0, 0, nil, nil
		upstream.mu.Unlock()

		bodies := make([][]byte, concurrency)
		contexts := make([]*gin.Context, concurrency)
		bodyDigests := make([]string, concurrency)
		bodyLengths := make([]int64, concurrency)
		wireFixtures := make([]string, concurrency)
		wireLengths := make([]int64, concurrency)
		totalBodyBytes := 0
		for i := range bodies {
			if stage == "read_forward" && fixture != "" && (ingressEncoding != "zstd" || wireFixture != "") {
				// 容器验收只流式读取固定样本的摘要与长度，避免测量前整段加载污染 memory.peak。
				file, err := os.Open(fixture)
				require.NoError(t, err)
				// 固定生成器把 client_metadata 放在 input 前；只读取有界前缀构造同一组请求头。
				prefix := make([]byte, 64<<10)
				n, prefixErr := file.Read(prefix)
				require.NoError(t, prefixErr)
				contexts[i] = newOfficialOpenAIHTTPTestContext(prefix[:n], "/v1/responses")
				require.NotEmpty(t, contexts[i].Request.Header.Get("x-codex-turn-metadata"), "固定样本的小字段必须位于有界前缀内")
				contexts[i].Request.Body = http.NoBody
				contexts[i].Request.GetBody = nil
				digest := sha256.New()
				_, err = digest.Write(prefix[:n])
				require.NoError(t, err)
				remaining, readErr := io.Copy(digest, file)
				length := int64(n) + remaining
				closeErr := file.Close()
				require.NoError(t, readErr)
				require.NoError(t, closeErr)
				bodyDigests[i] = fmt.Sprintf("%x", digest.Sum(nil))
				bodyLengths[i] = length
				totalBodyBytes += int(length)
				wireFixtures[i] = fixture
				if wireFixture != "" {
					wireFixtures[i] = wireFixture
				}
				info, err := os.Stat(wireFixtures[i])
				require.NoError(t, err)
				wireLengths[i] = info.Size()
				continue
			}
			bodies[i] = loadOfficialEgressMemoryProfileBody(t, targetBytes, shape, fixture)
			contexts[i] = newOfficialOpenAIHTTPTestContext(bodies[i], "/v1/responses")
			bodyDigests[i] = fmt.Sprintf("%x", sha256.Sum256(bodies[i]))
			bodyLengths[i] = int64(len(bodies[i]))
			totalBodyBytes += len(bodies[i])
			if stage == "read_forward" {
				wireFixtures[i] = wireFixture
				if wireFixtures[i] == "" && ingressEncoding != "zstd" {
					wireFixtures[i] = fixture
				}
				if wireFixtures[i] == "" {
					wireFixtures[i] = filepath.Join(t.TempDir(), "ingress-body")
					file, err := os.Create(wireFixtures[i])
					require.NoError(t, err)
					var writer io.Writer = file
					var encoder *zstd.Encoder
					if ingressEncoding == "zstd" {
						encoder, err = zstd.NewWriter(file, zstd.WithEncoderConcurrency(1), zstd.WithLowerEncoderMem(true))
						require.NoError(t, err)
						writer = encoder
					}
					_, err = writer.Write(bodies[i])
					require.NoError(t, err)
					if encoder != nil {
						require.NoError(t, encoder.Close())
					}
					require.NoError(t, file.Close())
				}
				info, err := os.Stat(wireFixtures[i])
				require.NoError(t, err)
				wireLengths[i] = info.Size()
				// 保留官方测试头，释放样本及 request.Body/GetBody 对原文的引用。
				contexts[i].Request.Body = http.NoBody
				contexts[i].Request.GetBody = nil
				bodies[i] = nil
			}
		}

		runtime.GC()
		debug.FreeOSMemory()
		var before runtime.MemStats
		runtime.ReadMemStats(&before)
		cgroupBefore := readOfficialEgressMemoryProfileCgroup("memory.current")
		cpuBefore := readOfficialEgressMemoryProfileCPU()
		rssBefore := readOfficialEgressMemoryProfileRSS()
		var peakRSS atomic.Int64
		peakRSS.Store(rssBefore)

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
					if rss := readOfficialEgressMemoryProfileRSS(); rss > peakRSS.Load() {
						peakRSS.Store(rss)
					}
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
				// 同一槽依次转发 requestsPerSlot 次（默认 1 次）；正文只读、各次共用，第二次起在窗口内新建上下文。
				for request := 0; request < requestsPerSlot; request++ {
					c := contexts[index]
					if stage == "read_forward" {
						if request > 0 {
							fresh := newOfficialOpenAIHTTPTestContext(nil, "/v1/responses")
							fresh.Request.Header = c.Request.Header.Clone()
							c = fresh
							contexts[index] = fresh
						}
						bodies[index] = nil
						file, err := os.Open(wireFixtures[index])
						if err != nil {
							errs[index] = err
							return
						}
						c.Request.Body = file
						c.Request.ContentLength = wireLengths[index]
						if ingressEncoding == "chunked" {
							c.Request.ContentLength = -1
						} else if ingressEncoding == "zstd" {
							c.Request.Header.Set("Content-Encoding", "zstd")
						}
						readLimit := bodyLengths[index]
						if wireLengths[index] > readLimit {
							readLimit = wireLengths[index]
						}
						body, readErr := pkghttputil.ReadAdmittedLenientJSONRequestBody(c.Request, readLimit)
						closeErr := file.Close()
						if readErr != nil || closeErr != nil {
							errs[index] = fmt.Errorf("入口读取失败: read=%v close=%v", readErr, closeErr)
							return
						}
						bodies[index] = body
						if digest := fmt.Sprintf("%x", sha256.Sum256(body)); digest != bodyDigests[index] {
							errs[index] = fmt.Errorf("入口读取的正文与固定对照样本不一致")
							return
						}
					} else if request > 0 {
						c = newOfficialOpenAIHTTPTestContext(bodies[index], "/v1/responses")
						// 请求结束后入口上下文随请求退役；只保留当前请求，不能把第一轮缓存钉住整场测量。
						contexts[index] = c
					}
					result, err := service.Forward(t.Context(), c, account, bodies[index])
					if err == nil && result == nil {
						err = fmt.Errorf("Forward 返回空结果")
					}
					if err != nil {
						errs[index] = err
						return
					}
				}
			}(i)
		}
		wg.Wait()
		elapsed := time.Since(started)
		close(stop)
		<-samplerDone

		var after runtime.MemStats
		runtime.ReadMemStats(&after)
		cpuAfter := readOfficialEgressMemoryProfileCPU()
		// 即使编译器提前解除引用，也要保留入口原文到最终采样；含原文倍数的基线始终是一整份真实原文。
		runtime.KeepAlive(bodies)
		runtime.KeepAlive(contexts)
		// 采样协程已停止，这里直接把转发结束时的 HeapInuse 并入峰值（见文件头测量口径）。
		if after.HeapInuse > peakHeapInuse.Load() {
			peakHeapInuse.Store(after.HeapInuse)
		}
		cgroupPeak := readOfficialEgressMemoryProfileCgroup("memory.peak")
		if rss := readOfficialEgressMemoryProfileRSS(); rss > peakRSS.Load() {
			peakRSS.Store(rss)
		}
		for i, err := range errs {
			require.NoError(t, err, "第 %d 个请求转发失败", i+1)
		}
		upstream.mu.Lock()
		businessRequests := upstream.businessRequests
		wireBytes := upstream.wireBytes
		encodings := append([]string(nil), upstream.encodings...)
		contentLengths := append([]int64(nil), upstream.contentLengths...)
		upstream.mu.Unlock()
		require.Equal(t, concurrency*requestsPerSlot, businessRequests, "上游收到的业务请求数与并发数乘每槽请求数不一致")

		extraHeap := float64(peakHeapInuse.Load()) - float64(before.HeapInuse)
		requestHeap := extraHeap + float64(totalBodyBytes)
		if stage == "read_forward" {
			// 此阶段基线未持有原文，读取分配已进入采样，不能再加一份正文。
			requestHeap = extraHeap
		}
		totalAlloc := float64(after.TotalAlloc - before.TotalAlloc)
		result := map[string]any{
			"round":                       round,
			"measurement_stage":           stage,
			"ingress_encoding":            ingressEncoding,
			"body_shape":                  shape,
			"body_sha256":                 bodyDigests,
			"fixture_path":                fixture,
			"concurrency":                 concurrency,
			"requests_per_slot":           requestsPerSlot,
			"body_mib_each":               officialEgressMemoryProfileMiB(float64(totalBodyBytes) / float64(concurrency)),
			"body_mib_total":              officialEgressMemoryProfileMiB(float64(totalBodyBytes)),
			"body_bytes_total":            totalBodyBytes,
			"wire_mib_total":              officialEgressMemoryProfileMiB(float64(wireBytes)),
			"wire_content_encodings":      encodings,
			"wire_content_lengths":        contentLengths,
			"heap_inuse_before_mib":       officialEgressMemoryProfileMiB(float64(before.HeapInuse)),
			"heap_inuse_peak_mib":         officialEgressMemoryProfileMiB(float64(peakHeapInuse.Load())),
			"heap_inuse_after_mib":        officialEgressMemoryProfileMiB(float64(after.HeapInuse)),
			"heap_extra_peak_mib":         officialEgressMemoryProfileMiB(extraHeap),
			"heap_request_peak_mib":       officialEgressMemoryProfileMiB(requestHeap),
			"heap_request_peak_bytes":     int64(requestHeap),
			"extra_multiple":              math.Round(extraHeap/float64(totalBodyBytes)*100) / 100,
			"resident_multiple_with_body": math.Round(requestHeap/float64(totalBodyBytes)*100) / 100,
			"total_alloc_mib":             officialEgressMemoryProfileMiB(totalAlloc),
			"total_alloc_mib_per_request": officialEgressMemoryProfileMiB(totalAlloc / float64(concurrency*requestsPerSlot)),
			"gc_cycles":                   after.NumGC - before.NumGC,
			"gc_pause_ms":                 float64(after.PauseTotalNs-before.PauseTotalNs) / float64(time.Millisecond),
			"elapsed_ms":                  elapsed.Milliseconds(),
			"cgroup_current_before_mib":   officialEgressMemoryProfileCgroupMiB(cgroupBefore),
			"cgroup_peak_mib":             officialEgressMemoryProfileCgroupMiB(cgroupPeak),
			"rss_before_mib":              officialEgressMemoryProfileCgroupMiB(rssBefore),
			"rss_peak_mib":                officialEgressMemoryProfileCgroupMiB(peakRSS.Load()),
			"gogc":                        gcPercent,
			"gomemlimit_mib":              officialEgressMemoryProfileMiB(float64(memoryLimit)),
			"go_version":                  runtime.Version(),
			"goarch":                      runtime.GOARCH,
		}
		if cpuBefore >= 0 && cpuAfter >= cpuBefore {
			result["cpu_ms"] = math.Round((cpuAfter - cpuBefore) * 1000)
		}
		if totalBodyBytes/concurrency >= 16<<20 {
			result["large_body_target_met"] = requestHeap <= 2.5*float64(totalBodyBytes)
		}
		for i := range bodies {
			bodies[i] = nil
			contexts[i] = nil
		}
		// 强制回收只用于测量窗口结束后的释放检查，禁止在请求期间用 GC 压低峰值。
		runtime.GC()
		debug.FreeOSMemory()
		var released runtime.MemStats
		runtime.ReadMemStats(&released)
		result["heap_after_release_mib"] = officialEgressMemoryProfileMiB(float64(released.HeapInuse))
		result["rss_after_release_mib"] = officialEgressMemoryProfileCgroupMiB(readOfficialEgressMemoryProfileRSS())
		encoded, err := json.Marshal(result)
		require.NoError(t, err)
		fmt.Printf("MEMPROFILE_RESULT %s\n", encoded)
	}
}
