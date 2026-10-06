package service

import (
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os"
	"runtime"
	"runtime/debug"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 本测量仅覆盖已读取的 WS response.create → 单轮 HTTP bridge → 官方 Executor → 丢弃上游。
// 不覆盖 WS 帧读取、会话历史重放、连接池或请求准入，不能据此声明整条 WS 路径达标。
// 默认跳过，环境开关、形态、大小、固定 fixture、并发、轮数和每槽请求数与 HTTP 测量共用。
// 固定 fixture 可复用 HTTP 的原始正文：在采样前补 WS type/generate/Lite 标记，并同时输出原文件与 WS 帧摘要。
// 容器应设置 SUB2API_OFFICIAL_EGRESS_MEMORY_WS_FIXTURE，直接读取容器外准备好的 WS 帧，避免造帧复制污染
// 容器生命周期 memory.peak；原 fixture 只流式计算摘要，可用 MEMORY_SOURCE_SHA256 指定预期摘要。
// 请求内不强制 GC；峰值口径为 HeapInuse 采样增量加一份仍存活的 WS 原文。
func TestOfficialEgressWSHTTPBridgeMemoryProfile(t *testing.T) {
	if os.Getenv(officialEgressMemoryProfileEnv) != "1" {
		t.Skipf("bridge turn 内存测量默认跳过；设置 %s=1 才运行", officialEgressMemoryProfileEnv)
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
	require.Contains(t, []string{officialEgressMemoryProfileNative, officialEgressMemoryProfileExplicit, officialEgressMemoryProfileIncompressible}, shape)
	fixture := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileFixtureEnv))
	wsFixture := strings.TrimSpace(os.Getenv("SUB2API_OFFICIAL_EGRESS_MEMORY_WS_FIXTURE"))
	expectedSourceDigest := strings.TrimSpace(os.Getenv("SUB2API_OFFICIAL_EGRESS_MEMORY_SOURCE_SHA256"))
	gcPercent := debug.SetGCPercent(-1)
	debug.SetGCPercent(gcPercent)
	memoryLimit := debug.SetMemoryLimit(-1)

	for round := 1; round <= rounds; round++ {
		upstream := &officialEgressDiscardUpstream{}
		service := officialEgressWSHTTPBridgeTestService(upstream)
		account := newOfficialOpenAIHTTPTestAccount(94)
		warm := officialEgressWSHTTPBridgePayload(t, buildOfficialEgressMemoryProfileBody(t, 128<<10))
		_, err := service.proxyOpenAIWSHTTPBridgeTurn(t.Context(), newOfficialOpenAIHTTPTestContext(warm, "/v1/responses"), account, "oauth-token", warm, len(warm), "gpt-5.6-luna", "", "", "", "", 1, func([]byte) error { return nil })
		require.NoError(t, err)
		upstream.mu.Lock()
		upstream.businessRequests, upstream.wireBytes, upstream.encodings, upstream.contentLengths = 0, 0, nil, nil
		upstream.mu.Unlock()

		bodies := make([][]byte, concurrency)
		contexts := make([]*gin.Context, concurrency)
		bodyDigests := make([]string, concurrency)
		sourceDigests := make([]string, concurrency)
		totalBodyBytes := 0
		for i := range bodies {
			if wsFixture != "" {
				require.NotEmpty(t, fixture, "直接读取 WS 帧时必须提供对应原正文 fixture，用于流式核对摘要")
				sourceDigests[i] = officialEgressWSHTTPBridgeFixtureDigest(t, fixture)
				var readErr error
				bodies[i], readErr = os.ReadFile(wsFixture)
				require.NoError(t, readErr)
				require.True(t, json.Valid(bodies[i]))
				require.Equal(t, "response.create", openAIBodyGet(bodies[i], "type").String())
				require.Equal(t, gjson.True, openAIBodyGet(bodies[i], "generate").Type)
				require.True(t, isOpenAIResponsesLiteWebSocketPayload(bodies[i]))
			} else {
				source := loadOfficialEgressMemoryProfileBody(t, int(bodyMiB*(1<<20)), shape, fixture)
				sourceDigests[i] = fmt.Sprintf("%x", sha256.Sum256(source))
				bodies[i] = officialEgressWSHTTPBridgePayload(t, source)
			}
			if expectedSourceDigest != "" {
				require.Equal(t, expectedSourceDigest, sourceDigests[i], "WS 测量的原始 fixture 摘要不符")
			}
			bodyDigests[i] = fmt.Sprintf("%x", sha256.Sum256(bodies[i]))
			contexts[i] = newOfficialOpenAIHTTPTestContext(bodies[i], "/v1/responses")
			totalBodyBytes += len(bodies[i])
		}
		runtime.GC()
		debug.FreeOSMemory()
		var before runtime.MemStats
		runtime.ReadMemStats(&before)
		cgroupBefore := readOfficialEgressMemoryProfileCgroup("memory.current")
		rssBefore := readOfficialEgressMemoryProfileRSS()
		var peakRSS atomic.Int64
		peakRSS.Store(rssBefore)
		var peakHeapInuse atomic.Uint64
		peakHeapInuse.Store(before.HeapInuse)
		stop, samplerDone := make(chan struct{}), make(chan struct{})
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
					if stats.HeapInuse > peakHeapInuse.Load() {
						peakHeapInuse.Store(stats.HeapInuse)
					}
					if rss := readOfficialEgressMemoryProfileRSS(); rss > peakRSS.Load() {
						peakRSS.Store(rss)
					}
				}
			}
		}()

		cpuBefore := readOfficialEgressMemoryProfileCPU()
		started := time.Now()
		errs := make([]error, concurrency)
		var wg sync.WaitGroup
		for i := range bodies {
			wg.Add(1)
			go func(index int) {
				defer wg.Done()
				for request := 0; request < requestsPerSlot; request++ {
					c := contexts[index]
					if request > 0 {
						c = newOfficialOpenAIHTTPTestContext(bodies[index], "/v1/responses")
						contexts[index] = c
					}
					result, err := service.proxyOpenAIWSHTTPBridgeTurn(t.Context(), c, account, "oauth-token", bodies[index], len(bodies[index]), "gpt-5.6-luna", "", "", "", "", 1, func([]byte) error { return nil })
					if err == nil && result == nil {
						err = fmt.Errorf("bridge turn 返回空结果")
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
		runtime.KeepAlive(bodies)
		runtime.KeepAlive(contexts)
		if after.HeapInuse > peakHeapInuse.Load() {
			peakHeapInuse.Store(after.HeapInuse)
		}
		if rss := readOfficialEgressMemoryProfileRSS(); rss > peakRSS.Load() {
			peakRSS.Store(rss)
		}
		for i, err := range errs {
			require.NoError(t, err, "第 %d 个 bridge turn 失败", i+1)
		}
		upstream.mu.Lock()
		businessRequests, wireBytes := upstream.businessRequests, upstream.wireBytes
		encodings := append([]string(nil), upstream.encodings...)
		lengths := append([]int64(nil), upstream.contentLengths...)
		upstream.mu.Unlock()
		require.Equal(t, concurrency*requestsPerSlot, businessRequests)
		extraHeap := float64(peakHeapInuse.Load()) - float64(before.HeapInuse)
		requestHeap := extraHeap + float64(totalBodyBytes)
		totalAlloc := float64(after.TotalAlloc - before.TotalAlloc)
		result := map[string]any{
			"round": round, "measurement_stage": "ws_http_bridge_turn", "ingress_encoding": "already_read_ws_frame",
			"body_shape": shape, "fixture_path": fixture, "ws_fixture_path": wsFixture, "source_body_sha256": sourceDigests, "body_sha256": bodyDigests,
			"concurrency": concurrency, "requests_per_slot": requestsPerSlot,
			"body_mib_each": officialEgressMemoryProfileMiB(float64(totalBodyBytes) / float64(concurrency)), "body_mib_total": officialEgressMemoryProfileMiB(float64(totalBodyBytes)), "body_bytes_total": totalBodyBytes,
			"wire_mib_total": officialEgressMemoryProfileMiB(float64(wireBytes)), "wire_content_encodings": encodings, "wire_content_lengths": lengths,
			"heap_inuse_before_mib": officialEgressMemoryProfileMiB(float64(before.HeapInuse)), "heap_inuse_peak_mib": officialEgressMemoryProfileMiB(float64(peakHeapInuse.Load())), "heap_inuse_after_mib": officialEgressMemoryProfileMiB(float64(after.HeapInuse)),
			"heap_extra_peak_mib": officialEgressMemoryProfileMiB(extraHeap), "heap_request_peak_mib": officialEgressMemoryProfileMiB(requestHeap), "heap_request_peak_bytes": int64(requestHeap),
			"extra_multiple": math.Round(extraHeap/float64(totalBodyBytes)*100) / 100, "resident_multiple_with_body": math.Round(requestHeap/float64(totalBodyBytes)*100) / 100,
			"total_alloc_mib": officialEgressMemoryProfileMiB(totalAlloc), "total_alloc_mib_per_request": officialEgressMemoryProfileMiB(totalAlloc / float64(concurrency*requestsPerSlot)),
			"gc_cycles": after.NumGC - before.NumGC, "gc_pause_ms": float64(after.PauseTotalNs-before.PauseTotalNs) / float64(time.Millisecond), "elapsed_ms": elapsed.Milliseconds(),
			"cgroup_current_before_mib": officialEgressMemoryProfileCgroupMiB(cgroupBefore), "cgroup_peak_mib": officialEgressMemoryProfileCgroupMiB(readOfficialEgressMemoryProfileCgroup("memory.peak")),
			"rss_before_mib": officialEgressMemoryProfileCgroupMiB(rssBefore), "rss_peak_mib": officialEgressMemoryProfileCgroupMiB(peakRSS.Load()),
			"gogc": gcPercent, "gomemlimit_mib": officialEgressMemoryProfileMiB(float64(memoryLimit)), "go_version": runtime.Version(), "goarch": runtime.GOARCH,
		}
		if cpuBefore >= 0 && cpuAfter >= cpuBefore {
			result["cpu_ms"] = math.Round((cpuAfter - cpuBefore) * 1000)
		}
		if totalBodyBytes/concurrency >= 16<<20 {
			result["large_body_target_met"] = requestHeap <= 2.5*float64(totalBodyBytes)
		}
		bodies, contexts = nil, nil
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

func officialEgressWSHTTPBridgeFixtureDigest(t *testing.T, path string) string {
	t.Helper()
	file, err := os.Open(path)
	require.NoError(t, err)
	digest := sha256.New()
	_, copyErr := io.Copy(digest, file)
	closeErr := file.Close()
	require.NoError(t, copyErr)
	require.NoError(t, closeErr)
	return fmt.Sprintf("%x", digest.Sum(nil))
}

func officialEgressWSHTTPBridgeTestService(upstream HTTPUpstream) *OpenAIGatewayService {
	service := &OpenAIGatewayService{cfg: &config.Config{Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}}, httpUpstream: upstream}
	service.openaiModelCapabilities.replaceFromManifest(94, []byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`))
	return service
}

func officialEgressWSHTTPBridgePayload(t *testing.T, source []byte) []byte {
	t.Helper()
	body, err := sjson.SetBytes(source, "type", "response.create")
	require.NoError(t, err)
	body, err = sjson.SetBytes(body, "generate", true)
	require.NoError(t, err)
	body, err = sjson.SetBytes(body, "client_metadata."+responsesLiteWSMetadataKey, "true")
	require.NoError(t, err)
	return body
}
