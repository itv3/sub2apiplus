package service

import (
	"context"
	"crypto/sha256"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"runtime/debug"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	coderws "github.com/coder/websocket"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

const (
	officialEgressMemoryWSSessionFixtureDirEnv = "SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR"
	officialEgressMemoryWSPrepareFixtureEnv    = "SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_WS_SESSION_FIXTURE"
)

// 本测量覆盖真实入站 WS 读帧、官方正文转换、原生 WS 或自动 HTTP bridge，及同连接多轮状态。
// 沿用 HTTP 测量的环境开关、fixture、大小、并发、轮数；REQUESTS_PER_SLOT 在此代表会话轮数。
// 客户端从采样前生成的文件流发送，入站源正文不在基线堆中，故不再给峰值增量加回一份正文。
// 分母为各并发连接最大单帧字节数之和；多轮发送的累计字节不进入分母，避免稀释历史保活开销。
// 原生 WS 上游是真实 coder 连接，由固定缓冲读完即丢弃；不缓存请求体。测量包括本地两端 WS
// 编解码的固定开销，不包括公网、TLS、完整鉴权/调度 handler，也不使用线上共享容量拒绝样本。
// 保持生产的 15 MiB 自动 bridge 阈值和 64 MiB 单帧上限；50 MiB 会测 bridge，而非冒充原生 WS。
// 容器验收先在容器外设置 PREPARE_WS_SESSION_FIXTURE=1 运行独立准备用例，再只读挂载
// WS_SESSION_FIXTURE_DIR；测量进程仅流式校验摘要，避免样本生成污染容器生命周期峰值。
func TestOfficialEgressWSSessionReadMemoryProfile(t *testing.T) {
	if os.Getenv(officialEgressMemoryProfileEnv) != "1" {
		t.Skipf("WS 会话内存测量默认跳过；设置 %s=1 才运行", officialEgressMemoryProfileEnv)
	}
	bodyMiB := officialEgressMemoryProfileEnvFloat(officialEgressMemoryProfileBodyMiBEnv, 16.8)
	concurrency := officialEgressMemoryProfileEnvInt(officialEgressMemoryProfileConcurrencyEnv, 1)
	rounds := officialEgressMemoryProfileEnvInt(officialEgressMemoryProfileRoundsEnv, 1)
	turns := officialEgressMemoryProfileEnvInt(officialEgressMemoryProfileRequestsEnv, 2)
	shape := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileShapeEnv))
	if shape == "" {
		shape = officialEgressMemoryProfileNative
	}
	require.Contains(t, []string{officialEgressMemoryProfileNative, officialEgressMemoryProfileExplicit, officialEgressMemoryProfileIncompressible}, shape)
	fixture := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileFixtureEnv))
	fixtureDir := strings.TrimSpace(os.Getenv(officialEgressMemoryWSSessionFixtureDirEnv))
	var paths, frameDigests []string
	var frameBytes []int64
	var sourceDigest string
	if fixtureDir == "" {
		paths, frameBytes, frameDigests, sourceDigest = prepareOfficialEgressWSSessionFiles(t, int(bodyMiB*(1<<20)), shape, fixture, turns, t.TempDir())
	} else {
		paths, frameBytes, frameDigests, sourceDigest = loadOfficialEgressWSSessionFiles(t, fixtureDir, shape, turns)
	}
	maxFrameBytes := int64(0)
	for _, size := range frameBytes {
		if size > maxFrameBytes {
			maxFrameBytes = size
		}
	}
	require.LessOrEqual(t, maxFrameBytes, int64(64<<20), "放行测量保持 64 MiB 上限；超限拒绝应使用准入回归用例")
	gcPercent := debug.SetGCPercent(-1)
	debug.SetGCPercent(gcPercent)
	memoryLimit := debug.SetMemoryLimit(-1)

	for round := 1; round <= rounds; round++ {
		func() {
			upstream := &officialEgressDiscardUpstream{}
			wsUpstream := newOfficialEgressWSDiscardProfileServer()
			defer wsUpstream.server.Close()
			errs := make(chan error, concurrency)
			clients := make([]*coderws.Conn, concurrency)
			var reserved, peakReserved atomic.Int64
			var verifiedFrames atomic.Int64
			for slot := range concurrency {
				service := officialEgressWSHTTPBridgeTestService(upstream)
				cfg := newOpenAIWSExecutionScopeTestConfig()
				cfg.Gateway.OpenAIWS.HTTPBridgeEnabled = true
				cfg.Gateway.OpenAIWS.HTTPBridgeThresholdBytes = 15 << 20
				cfg.Gateway.OpenAIWS.ClientReadLimitBytes = 64 << 20
				cfg.Gateway.OpenAIWS.ReadTimeoutSeconds = 120
				cfg.Gateway.OpenAIWS.WriteTimeoutSeconds = 120
				service.cfg = cfg
				service.cache = &stubGatewayCache{}
				service.openaiWSResolver = NewOpenAIWSProtocolResolver(cfg)
				service.toolCorrector = NewCodexToolCorrector()
				service.openaiWSPool = newOpenAIWSConnPool(cfg)
				service.openaiWSPool.setClientDialerForTest(&officialEgressWSLocalProfileDialer{url: wsUpstream.server.URL})
				defer service.openaiWSPool.Close()
				account := newOfficialOpenAIHTTPTestAccount(int64(94 + slot))
				account.Extra["responses_websockets_v2_enabled"] = true
				service.openaiModelCapabilities.replaceFromManifest(account.ID, []byte(`{"models":[{"slug":"gpt-5.6-luna","use_responses_lite":true}]}`))
				c := newOfficialOpenAIHTTPTestContext(nil, "/v1/responses")
				server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
					conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionDisabled})
					if err != nil {
						errs <- err
						return
					}
					defer conn.CloseNow()
					conn.SetReadLimit(64 << 20)
					lastWeight := int64(0)
					memory := NewOpenAIWSRequestMemory(64<<20, 25<<20, 6.11, func(weight int64) bool {
						current := reserved.Add(weight - lastWeight)
						lastWeight = weight
						for old := peakReserved.Load(); current > old; old = peakReserved.Load() {
							if peakReserved.CompareAndSwap(old, current) {
								break
							}
						}
						return true
					})
					ctx := WithOpenAIWSRequestMemory(r.Context(), memory)
					_, first, err := ReadOpenAIWSClientMessage(ctx, conn, 120*time.Second, coderws.StatusPolicyViolation, "missing first frame")
					if err == nil {
						err = verifyOfficialEgressWSProfileFrame(first, frameDigests[0])
					}
					if err == nil {
						verifiedFrames.Add(1)
						// 与 handler 的首包 failover 持有点一致，直到连接结束才撤销引用。
						err = memory.Retain("handler", first)
					}
					if err == nil {
						c.Request = c.Request.WithContext(ctx)
						applyOfficialOpenAIWSIngressHeadersForTest(c, first)
						hooks := &OpenAIWSIngressHooks{BeforeRequest: func(turn int, body []byte, _ string) error {
							if turn < 1 || turn > len(frameDigests) {
								return fmt.Errorf("非预期会话轮次：%d", turn)
							}
							if err := verifyOfficialEgressWSProfileFrame(body, frameDigests[turn-1]); err != nil {
								return err
							}
							verifiedFrames.Add(1)
							return nil
						}}
						err = service.ProxyResponsesWebSocketFromClient(ctx, c, conn, account, "oauth-token", first, hooks)
					}
					memory.Close()
					errs <- err
				}))
				defer server.Close()
				client, _, err := coderws.Dial(t.Context(), "ws"+strings.TrimPrefix(server.URL, "http"), nil)
				require.NoError(t, err)
				clients[slot] = client
				defer client.CloseNow()
			}

			runtime.GC()
			debug.FreeOSMemory()
			var before runtime.MemStats
			runtime.ReadMemStats(&before)
			cgroupBefore := readOfficialEgressMemoryProfileCgroup("memory.current")
			rssBefore := readOfficialEgressMemoryProfileRSS()
			var peakRSS atomic.Int64
			peakRSS.Store(rssBefore)
			var peakHeap atomic.Uint64
			peakHeap.Store(before.HeapInuse)
			stop, samplerDone := make(chan struct{}), make(chan struct{})
			go func() {
				defer close(samplerDone)
				ticker := time.NewTicker(2 * time.Millisecond)
				defer ticker.Stop()
				for {
					select {
					case <-stop:
						return
					case <-ticker.C:
						var stats runtime.MemStats
						runtime.ReadMemStats(&stats)
						if stats.HeapInuse > peakHeap.Load() {
							peakHeap.Store(stats.HeapInuse)
						}
						if rss := readOfficialEgressMemoryProfileRSS(); rss > peakRSS.Load() {
							peakRSS.Store(rss)
						}
					}
				}
			}()
			cpuBefore := readOfficialEgressMemoryProfileCPU()
			started := time.Now()
			clientErrs := make([]error, concurrency)
			var wait sync.WaitGroup
			for slot, client := range clients {
				wait.Add(1)
				go func(slot int, client *coderws.Conn) {
					defer wait.Done()
					ctx, cancel := context.WithTimeout(t.Context(), 5*time.Minute)
					defer cancel()
					clientErrs[slot] = streamOfficialEgressWSProfileFiles(ctx, client, paths)
					_ = client.Close(coderws.StatusNormalClosure, "done")
				}(slot, client)
			}
			wait.Wait()
			serverErrs := make([]error, concurrency)
			for slot := range serverErrs {
				select {
				case serverErrs[slot] = <-errs:
				case <-time.After(5 * time.Second):
					serverErrs[slot] = fmt.Errorf("WS 服务会话关闭超时")
				}
			}
			elapsed := time.Since(started)
			cpuAfter := readOfficialEgressMemoryProfileCPU()
			close(stop)
			<-samplerDone
			var after runtime.MemStats
			runtime.ReadMemStats(&after)
			if after.HeapInuse > peakHeap.Load() {
				peakHeap.Store(after.HeapInuse)
			}
			if rss := readOfficialEgressMemoryProfileRSS(); rss > peakRSS.Load() {
				peakRSS.Store(rss)
			}
			for slot := range concurrency {
				require.NoError(t, clientErrs[slot], "客户端 %d", slot)
				require.NoError(t, serverErrs[slot], "服务端 %d", slot)
			}
			require.Zero(t, reserved.Load())
			require.EqualValues(t, concurrency*turns, verifiedFrames.Load())
			heapRequest := float64(peakHeap.Load()) - float64(before.HeapInuse)
			totalBodyBytes := float64(maxFrameBytes * int64(concurrency))
			upstream.mu.Lock()
			httpRequests, httpWireBytes := upstream.businessRequests, upstream.wireBytes
			upstream.mu.Unlock()
			transport := "native_websocket"
			if httpRequests > 0 {
				transport = "http_bridge"
			}
			result := map[string]any{
				"round": round, "measurement_stage": "ws_read_service_session", "ingress_encoding": "websocket_identity",
				"body_shape": shape, "fixture_path": fixture, "source_body_sha256": sourceDigest, "frame_sha256": frameDigests, "frame_bytes": frameBytes,
				"ws_session_fixture_dir": fixtureDir, "fixture_prepared_externally": fixtureDir != "", "cold_session": true,
				"concurrency": concurrency, "requests_per_slot": turns, "actual_transport": transport,
				"body_mib_each": officialEgressMemoryProfileMiB(float64(maxFrameBytes)), "body_mib_total": officialEgressMemoryProfileMiB(totalBodyBytes),
				"body_bytes_total": maxFrameBytes * int64(concurrency), "heap_request_peak_bytes": int64(heapRequest),
				"heap_inuse_before_mib": officialEgressMemoryProfileMiB(float64(before.HeapInuse)), "heap_inuse_peak_mib": officialEgressMemoryProfileMiB(float64(peakHeap.Load())), "heap_inuse_after_mib": officialEgressMemoryProfileMiB(float64(after.HeapInuse)),
				"heap_request_peak_mib": officialEgressMemoryProfileMiB(heapRequest), "resident_multiple_with_body": math.Round(heapRequest/totalBodyBytes*100) / 100,
				"total_alloc_mib": officialEgressMemoryProfileMiB(float64(after.TotalAlloc - before.TotalAlloc)),
				"gc_cycles":       after.NumGC - before.NumGC, "gc_pause_ms": float64(after.PauseTotalNs-before.PauseTotalNs) / float64(time.Millisecond), "elapsed_ms": elapsed.Milliseconds(),
				"cgroup_current_before_mib": officialEgressMemoryProfileCgroupMiB(cgroupBefore), "cgroup_peak_mib": officialEgressMemoryProfileCgroupMiB(readOfficialEgressMemoryProfileCgroup("memory.peak")),
				"rss_before_mib": officialEgressMemoryProfileCgroupMiB(rssBefore), "rss_peak_mib": officialEgressMemoryProfileCgroupMiB(peakRSS.Load()),
				"http_upstream_requests": httpRequests, "http_upstream_wire_bytes": httpWireBytes, "ws_upstream_frames": wsUpstream.frames.Load(), "ws_upstream_decoded_bytes": wsUpstream.decodedBytes.Load(),
				"budget_enforced": false, "reservation_peak_mib": officialEgressMemoryProfileMiB(float64(peakReserved.Load())),
				"gogc": gcPercent, "gomemlimit_mib": officialEgressMemoryProfileMiB(float64(memoryLimit)), "go_version": runtime.Version(), "goarch": runtime.GOARCH,
			}
			if cpuBefore >= 0 && cpuAfter >= cpuBefore {
				result["cpu_ms"] = math.Round((cpuAfter - cpuBefore) * 1000)
			}
			if maxFrameBytes >= 16<<20 {
				result["large_body_target_met"] = heapRequest <= 2.5*totalBodyBytes
			}
			runtime.GC()
			debug.FreeOSMemory()
			var released runtime.MemStats
			runtime.ReadMemStats(&released)
			result["heap_after_release_mib"] = officialEgressMemoryProfileMiB(float64(released.HeapInuse))
			result["rss_after_release_mib"] = officialEgressMemoryProfileCgroupMiB(readOfficialEgressMemoryProfileRSS())
			encoded, err := json.Marshal(result)
			require.NoError(t, err)
			fmt.Printf("MEMPROFILE_RESULT %s\n", encoded)
		}()
	}
}

type officialEgressWSSessionFixtureManifest struct {
	Shape        string   `json:"body_shape"`
	SourceDigest string   `json:"source_body_sha256"`
	FrameBytes   []int64  `json:"frame_bytes"`
	FrameDigests []string `json:"frame_sha256"`
}

func TestOfficialEgressWSMemoryPrepareFixture(t *testing.T) {
	if os.Getenv(officialEgressMemoryWSPrepareFixtureEnv) != "1" {
		t.Skip("仅在容器外显式准备 WS 会话固定样本时运行")
	}
	directory := strings.TrimSpace(os.Getenv(officialEgressMemoryWSSessionFixtureDirEnv))
	require.NotEmpty(t, directory, "必须指定 WS 会话样本目录")
	shape := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileShapeEnv))
	if shape == "" {
		shape = officialEgressMemoryProfileNative
	}
	require.Contains(t, []string{officialEgressMemoryProfileNative, officialEgressMemoryProfileExplicit, officialEgressMemoryProfileIncompressible}, shape)
	size := int(officialEgressMemoryProfileEnvFloat(officialEgressMemoryProfileBodyMiBEnv, 16.8) * (1 << 20))
	turns := officialEgressMemoryProfileEnvInt(officialEgressMemoryProfileRequestsEnv, 2)
	_, sizes, digests, sourceDigest := prepareOfficialEgressWSSessionFiles(t, size, shape, strings.TrimSpace(os.Getenv(officialEgressMemoryProfileFixtureEnv)), turns, directory)
	fmt.Printf("MEMPROFILE_WS_FIXTURE directory=%s source_sha256=%s frame_bytes=%v frame_sha256=%v\n", directory, sourceDigest, sizes, digests)
}

func prepareOfficialEgressWSSessionFiles(t *testing.T, targetBytes int, shape, fixture string, turns int, directory string) ([]string, []int64, []string, string) {
	t.Helper()
	require.NoError(t, os.MkdirAll(directory, 0700))
	source := loadOfficialEgressMemoryProfileBody(t, targetBytes, shape, fixture)
	sourceDigest := fmt.Sprintf("%x", sha256.Sum256(source))
	body := officialEgressWSHTTPBridgePayload(t, source)
	paths, sizes, digests := make([]string, turns), make([]int64, turns), make([]string, turns)
	for turn := range turns {
		if turn > 0 {
			var err error
			body, err = sjson.SetBytes(body, "input.-1", map[string]any{"type": "message", "role": "user", "content": []any{map[string]any{"type": "input_text", "text": fmt.Sprintf("继续内存测量第 %d 轮", turn+1)}}})
			require.NoError(t, err)
		}
		paths[turn] = filepath.Join(directory, fmt.Sprintf("turn-%d.json", turn+1))
		sizes[turn] = int64(len(body))
		digests[turn] = fmt.Sprintf("%x", sha256.Sum256(body))
		writeOfficialEgressWSFixtureFile(t, paths[turn], body)
	}
	manifest, err := json.Marshal(officialEgressWSSessionFixtureManifest{Shape: shape, SourceDigest: sourceDigest, FrameBytes: sizes, FrameDigests: digests})
	require.NoError(t, err)
	writeOfficialEgressWSFixtureFile(t, filepath.Join(directory, "manifest.json"), manifest)
	return paths, sizes, digests, sourceDigest
}

func writeOfficialEgressWSFixtureFile(t *testing.T, path string, body []byte) {
	t.Helper()
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
	require.NoError(t, err, "固定样本不得覆盖已有文件")
	_, writeErr := file.Write(body)
	closeErr := file.Close()
	require.NoError(t, writeErr)
	require.NoError(t, closeErr)
}

func loadOfficialEgressWSSessionFiles(t *testing.T, directory, shape string, turns int) ([]string, []int64, []string, string) {
	t.Helper()
	manifestFile, err := os.Open(filepath.Join(directory, "manifest.json"))
	require.NoError(t, err, "指定目录必须已在容器外完成准备")
	var manifest officialEgressWSSessionFixtureManifest
	decodeErr := json.NewDecoder(io.LimitReader(manifestFile, 64<<10)).Decode(&manifest)
	closeErr := manifestFile.Close()
	require.NoError(t, decodeErr)
	require.NoError(t, closeErr)
	require.Equal(t, shape, manifest.Shape)
	require.GreaterOrEqual(t, len(manifest.FrameDigests), turns)
	require.Equal(t, len(manifest.FrameDigests), len(manifest.FrameBytes))
	paths := make([]string, turns)
	for turn := range turns {
		paths[turn] = filepath.Join(directory, fmt.Sprintf("turn-%d.json", turn+1))
		file, err := os.Open(paths[turn])
		require.NoError(t, err)
		info, statErr := file.Stat()
		hash := sha256.New()
		count, readErr := io.Copy(hash, file)
		closeErr := file.Close()
		require.NoError(t, statErr)
		require.NoError(t, readErr)
		require.NoError(t, closeErr)
		require.Equal(t, manifest.FrameBytes[turn], info.Size())
		require.Equal(t, info.Size(), count)
		require.Equal(t, manifest.FrameDigests[turn], fmt.Sprintf("%x", hash.Sum(nil)))
	}
	return paths, manifest.FrameBytes[:turns], manifest.FrameDigests[:turns], manifest.SourceDigest
}

func verifyOfficialEgressWSProfileFrame(body []byte, expected string) error {
	if actual := fmt.Sprintf("%x", sha256.Sum256(body)); actual != expected {
		return fmt.Errorf("WS 帧摘要不匹配：got=%s want=%s", actual, expected)
	}
	return nil
}

func streamOfficialEgressWSProfileFiles(ctx context.Context, conn *coderws.Conn, paths []string) error {
	buffer := make([]byte, 32<<10)
	for _, path := range paths {
		file, err := os.Open(path)
		if err != nil {
			return err
		}
		writer, err := conn.Writer(ctx, coderws.MessageText)
		if err != nil {
			_ = file.Close()
			return err
		}
		_, copyErr := io.CopyBuffer(writer, file, buffer)
		fileErr, writeErr := file.Close(), writer.Close()
		for _, err := range []error{copyErr, fileErr, writeErr} {
			if err != nil {
				return err
			}
		}
		for {
			_, event, err := conn.Read(ctx)
			if err != nil {
				return err
			}
			eventType := gjson.GetBytes(event, "type").String()
			if eventType == "response.completed" || eventType == "response.done" {
				break
			}
			if eventType == "error" || eventType == "response.failed" {
				return fmt.Errorf("WS 会话收到错误事件：%s", event)
			}
		}
	}
	return nil
}

type officialEgressWSDiscardProfileServer struct {
	server       *httptest.Server
	frames       atomic.Int64
	decodedBytes atomic.Int64
}

func newOfficialEgressWSDiscardProfileServer() *officialEgressWSDiscardProfileServer {
	u := &officialEgressWSDiscardProfileServer{}
	u.server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		conn, err := coderws.Accept(w, r, &coderws.AcceptOptions{CompressionMode: coderws.CompressionContextTakeover})
		if err != nil {
			return
		}
		defer conn.CloseNow()
		conn.SetReadLimit(128 << 20)
		for {
			_, reader, err := conn.Reader(r.Context())
			if err != nil {
				return
			}
			// 不保存帧正文，保留真实上游 WS 写入、压缩与控制帧的行为。
			count, err := io.Copy(io.Discard, reader)
			if err != nil {
				return
			}
			u.decodedBytes.Add(count)
			frame := u.frames.Add(1)
			event := fmt.Sprintf(`{"type":"response.completed","response":{"id":"resp_ws_profile_%d","model":"gpt-5.6-luna","status":"completed","output":[],"usage":{"input_tokens":1,"output_tokens":1}}}`, frame)
			if conn.Write(r.Context(), coderws.MessageText, []byte(event)) != nil {
				return
			}
		}
	}))
	return u
}

type officialEgressWSLocalProfileDialer struct{ url string }

func (d *officialEgressWSLocalProfileDialer) Dial(ctx context.Context, _ string, _ http.Header, _ string) (openAIWSClientConn, int, http.Header, error) {
	conn, response, err := coderws.Dial(ctx, "ws"+strings.TrimPrefix(d.url, "http"), &coderws.DialOptions{CompressionMode: coderws.CompressionContextTakeover})
	if err != nil {
		return nil, 0, nil, err
	}
	conn.SetReadLimit(1 << 20)
	return &coderOpenAIWSClientConn{conn: conn}, response.StatusCode, response.Header, nil
}
