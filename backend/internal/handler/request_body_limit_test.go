package handler

import (
	"bytes"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestRequestBodyLimitTooLarge(t *testing.T) {
	gin.SetMode(gin.TestMode)

	limit := int64(16)
	router := gin.New()
	router.Use(middleware.RequestBodyLimit(limit))
	router.POST("/test", func(c *gin.Context) {
		_, err := io.ReadAll(c.Request.Body)
		if err != nil {
			if maxErr, ok := extractMaxBytesError(err); ok {
				c.JSON(http.StatusRequestEntityTooLarge, gin.H{
					"error": buildBodyTooLargeMessage(maxErr.Limit),
				})
				return
			}
			c.JSON(http.StatusBadRequest, gin.H{
				"error": "read_failed",
			})
			return
		}
		c.JSON(http.StatusOK, gin.H{"ok": true})
	})

	payload := bytes.Repeat([]byte("a"), int(limit+1))
	req := httptest.NewRequest(http.MethodPost, "/test", bytes.NewReader(payload))
	recorder := httptest.NewRecorder()
	router.ServeHTTP(recorder, req)

	require.Equal(t, http.StatusRequestEntityTooLarge, recorder.Code)
	require.Contains(t, recorder.Body.String(), buildBodyTooLargeMessage(limit))
}

func TestRequestMemoryAdmissionReservesBeforeReadAndReleases(t *testing.T) {
	cfg := &config.Config{}
	cfg.Gateway.RequestMemoryBudgetBytes = 128
	cfg.Gateway.RequestMemoryMaxRequestBytes = 32
	cfg.Gateway.RequestMemoryAmplification = 2
	cfg.Gateway.RequestMemoryFixedBytes = 8
	admission := newRequestMemoryAdmission(cfg)

	req := httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader([]byte("{}")))
	req.ContentLength = 20
	weight, tooLarge := requestMemoryWeight(req, cfg)
	require.False(t, tooLarge)
	require.Equal(t, int64(48), weight)

	first, ok := admission.tryAcquire(weight)
	require.True(t, ok)
	second, ok := admission.tryAcquire(weight)
	require.True(t, ok)
	third, ok := admission.tryAcquire(weight)
	require.False(t, ok)
	require.Nil(t, third)
	first.release()
	third, ok = admission.tryAcquire(weight)
	require.True(t, ok)
	second.release()
	third.release()
	used, capacity, accepted, rejected, _ := admission.stats()
	require.Zero(t, used)
	require.Equal(t, int64(128), capacity)
	require.Equal(t, uint64(3), accepted)
	require.Equal(t, uint64(1), rejected)
}

func TestRequestMemoryAdmissionRejectsOversizedAndCompressedUnknown(t *testing.T) {
	cfg := &config.Config{}
	cfg.Gateway.RequestMemoryBudgetBytes = 1024
	cfg.Gateway.RequestMemoryMaxRequestBytes = 32
	cfg.Gateway.RequestMemoryAmplification = 2
	cfg.Gateway.RequestMemoryFixedBytes = 8

	tooLargeReq := httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	tooLargeReq.ContentLength = 33
	_, tooLarge := requestMemoryWeight(tooLargeReq, cfg)
	require.True(t, tooLarge)

	chunkedReq := httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	chunkedReq.ContentLength = -1
	chunkedReq.Header.Set("Content-Encoding", "gzip")
	weight, tooLarge := requestMemoryWeight(chunkedReq, cfg)
	require.False(t, tooLarge)
	require.Equal(t, int64(72), weight)
}

func TestRequestMemoryReservationResizeSharesHTTPBudgetAndReleasesOnce(t *testing.T) {
	admission := &requestMemoryAdmission{capacityBytes: 1000}
	httpReservation, ok := admission.tryAcquire(308)
	require.True(t, ok)
	wsReservation := &requestMemoryReservation{admission: admission}
	require.True(t, wsReservation.resize(500))
	require.EqualValues(t, 808, admission.usedBytes.Load())
	require.False(t, wsReservation.resize(700))
	require.EqualValues(t, 808, admission.usedBytes.Load())
	_, ok = admission.tryAcquire(300)
	require.False(t, ok, "WS 正文必须与 HTTP 请求竞争同一个容量")
	require.True(t, wsReservation.resize(100))
	second, ok := admission.tryAcquire(300)
	require.True(t, ok)
	wsReservation.release()
	wsReservation.release()
	require.EqualValues(t, 608, admission.usedBytes.Load())
	require.False(t, wsReservation.resize(1), "已释放句柄不能重新占用预算")
	httpReservation.release()
	second.release()
	require.Zero(t, admission.usedBytes.Load())
}

func TestRequestMemoryReservationResizeAndCancellationAreConcurrentSafe(t *testing.T) {
	admission := &requestMemoryAdmission{capacityBytes: 10000}
	reservation := &requestMemoryReservation{admission: admission}
	var wait sync.WaitGroup
	for worker := 0; worker < 8; worker++ {
		wait.Add(1)
		go func(value int64) {
			defer wait.Done()
			for range 100 {
				reservation.resize(value)
			}
		}(int64(worker + 1))
	}
	reservation.release()
	wait.Wait()
	require.Zero(t, admission.usedBytes.Load())
}

func TestResponsesRequestBodyStorageFailureReturnsRetryable503(t *testing.T) {
	gin.SetMode(gin.TestMode)
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	c.Request.ContentLength = 2
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Body = io.NopCloser(requestBodyStorageFailureReader{})
	c.Set(string(middleware.ContextKeyAPIKey), &service.APIKey{ID: 1})
	c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 1, Concurrency: 1})
	cfg := &config.Config{}
	cfg.Gateway.RequestMemoryBudgetBytes = 1024
	cfg.Gateway.RequestMemoryMaxRequestBytes = 64
	cfg.Gateway.RequestMemoryFixedBytes = 8
	cfg.Gateway.RequestMemoryAmplification = 2
	h := &OpenAIGatewayHandler{
		cfg: cfg, gatewayService: &service.OpenAIGatewayService{},
		billingCacheService: &service.BillingCacheService{}, apiKeyService: &service.APIKeyService{},
		concurrencyHelper: &ConcurrencyHelper{concurrencyService: &service.ConcurrencyService{}},
	}
	h.Responses(c)
	require.Equal(t, http.StatusServiceUnavailable, recorder.Code)
	require.Equal(t, "1", recorder.Header().Get("Retry-After"))
	require.Contains(t, recorder.Body.String(), "Request body storage temporarily unavailable")
	require.NotContains(t, recorder.Body.String(), "private-storage-path")
	require.Zero(t, h.responsesRequestMemoryAdmission().usedBytes.Load())
}

type requestBodyStorageFailureReader struct{}

func (requestBodyStorageFailureReader) Read([]byte) (int, error) {
	return 0, &pkghttputil.RequestBodyStorageError{Err: errors.New("private-storage-path: disk full")}
}

func TestResponsesRequestMemoryRejectsNormalizedBodyGrowthAndReleases(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{}
	cfg.Gateway.MaxBodySize = 1024
	cfg.Gateway.RequestMemoryBudgetBytes = 2048
	cfg.Gateway.RequestMemoryMaxRequestBytes = 1024
	cfg.Gateway.RequestMemoryFixedBytes = 8
	cfg.Gateway.RequestMemoryAmplification = 2
	h := &OpenAIGatewayHandler{
		cfg: cfg, gatewayService: &service.OpenAIGatewayService{},
		billingCacheService: &service.BillingCacheService{}, apiKeyService: &service.APIKeyService{},
		concurrencyHelper: &ConcurrencyHelper{concurrencyService: &service.ConcurrencyService{}},
	}
	admission := h.responsesRequestMemoryAdmission()
	other, acquired := admission.tryAcquire(1000)
	require.True(t, acquired)
	defer other.release()
	// 100 个裸换行会转成 600 字节的 Unicode 转义。上传长度可准入，规范化后
	// 单独也放得下，但与已在执行的另一个请求竞争时必须在规范化分配前拒绝。
	body := []byte("{\"input\":\"" + strings.Repeat("\n", 100) + "\"}")
	normalized, err := pkghttputil.NormalizeLenientJSONRequestBody(body, 1024)
	require.NoError(t, err)
	initialWeight, _ := requestMemoryWeight(&http.Request{ContentLength: int64(len(body))}, cfg)
	finalWeight, _ := requestMemoryWeight(&http.Request{ContentLength: int64(len(normalized))}, cfg)
	require.LessOrEqual(t, initialWeight+1000, admission.capacityBytes)
	require.LessOrEqual(t, finalWeight, admission.capacityBytes)
	require.Greater(t, finalWeight+1000, admission.capacityBytes)

	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set(string(middleware.ContextKeyAPIKey), &service.APIKey{ID: 1})
	c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 1, Concurrency: 1})
	h.Responses(c)
	require.Equal(t, http.StatusServiceUnavailable, recorder.Code)
	require.Equal(t, "2", recorder.Header().Get("Retry-After"))
	require.Contains(t, recorder.Body.String(), "Request memory budget is temporarily exhausted")
	require.NotContains(t, recorder.Body.String(), "Failed to read request body", "读取入口不能覆盖准入回调已经写入的响应")
	require.EqualValues(t, 1000, admission.usedBytes.Load(), "补额失败不得占用差额，handler 的 defer 必须释放原预留")
	other.release()
	require.Zero(t, admission.usedBytes.Load())
}

func TestResponsesRequestMemoryReservesBeforeNormalizedBodyIsReturned(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{}
	cfg.Gateway.RequestMemoryBudgetBytes = 4096
	cfg.Gateway.RequestMemoryMaxRequestBytes = 1024
	cfg.Gateway.RequestMemoryFixedBytes = 8
	cfg.Gateway.RequestMemoryAmplification = 2
	h := &OpenAIGatewayHandler{cfg: cfg}
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	body := []byte("{\"input\":\"" + strings.Repeat("\n", 100) + "\"}")
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	release, acquired := h.acquireResponsesRequestMemory(c, nil)
	require.True(t, acquired)
	defer release()
	initialWeight := h.responsesRequestMemoryAdmission().usedBytes.Load()
	var normalizedBytes, calls int
	normalized, err := readAdmittedResponsesJSONRequestBodyWithReservation(c.Request, cfg, func(size int) error {
		calls++
		normalizedBytes = size
		require.Equal(t, initialWeight, h.responsesRequestMemoryAdmission().usedBytes.Load())
		require.True(t, h.ensureResponsesRequestMemoryForBody(c, size, nil))
		require.Greater(t, h.responsesRequestMemoryAdmission().usedBytes.Load(), initialWeight)
		return nil
	})
	require.NoError(t, err)
	require.Equal(t, 1, calls)
	require.Equal(t, normalizedBytes, len(normalized))
	require.Equal(t, len(body)+500, len(normalized))
	release()
	require.Zero(t, h.responsesRequestMemoryAdmission().usedBytes.Load())
}

func TestResponsesRequestMemoryKeepsBOMOnlyBodyEmptyError(t *testing.T) {
	gin.SetMode(gin.TestMode)
	cfg := &config.Config{}
	cfg.Gateway.RequestMemoryBudgetBytes = 256
	cfg.Gateway.RequestMemoryMaxRequestBytes = 1 << 20
	cfg.Gateway.RequestMemoryFixedBytes = 8
	cfg.Gateway.RequestMemoryAmplification = 2
	h := &OpenAIGatewayHandler{
		cfg: cfg, gatewayService: &service.OpenAIGatewayService{},
		billingCacheService: &service.BillingCacheService{}, apiKeyService: &service.APIKeyService{},
		concurrencyHelper: &ConcurrencyHelper{concurrencyService: &service.ConcurrencyService{}},
	}
	recorder := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(recorder)
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader([]byte{0xef, 0xbb, 0xbf}))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Set(string(middleware.ContextKeyAPIKey), &service.APIKey{ID: 1})
	c.Set(string(middleware.ContextKeyUser), middleware.AuthSubject{UserID: 1, Concurrency: 1})
	h.Responses(c)
	require.Equal(t, http.StatusBadRequest, recorder.Code)
	require.Contains(t, recorder.Body.String(), "Request body is empty")
	require.Empty(t, recorder.Header().Get("Retry-After"))
	require.Zero(t, h.responsesRequestMemoryAdmission().usedBytes.Load())
}

func TestResponsesRequestMemoryGrowsOriginalReservationWithoutReducingMaxPreallocation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	for _, encoding := range []string{"identity", "gzip", "unknown"} {
		t.Run(encoding, func(t *testing.T) {
			cfg := &config.Config{}
			cfg.Gateway.RequestMemoryBudgetBytes = 4096
			cfg.Gateway.RequestMemoryMaxRequestBytes = 1024
			cfg.Gateway.RequestMemoryFixedBytes = 8
			cfg.Gateway.RequestMemoryAmplification = 2
			h := &OpenAIGatewayHandler{cfg: cfg}
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", strings.NewReader(`{}`))
			if encoding == "unknown" {
				c.Request.ContentLength = -1
			} else {
				c.Request.Header.Set("Content-Encoding", encoding)
			}
			release, acquired := h.acquireResponsesRequestMemory(c, nil)
			require.True(t, acquired)
			defer release()
			initial := h.responsesRequestMemoryAdmission().usedBytes.Load()
			require.True(t, h.ensureResponsesRequestMemoryForBody(c, 100, nil))
			if encoding == "identity" {
				require.EqualValues(t, 208, h.responsesRequestMemoryAdmission().usedBytes.Load())
			} else {
				require.Equal(t, initial, h.responsesRequestMemoryAdmission().usedBytes.Load(), "按最大长度预留的请求不能因当前正文变小而提前退还预算")
			}
			release()
			require.Zero(t, h.responsesRequestMemoryAdmission().usedBytes.Load(), "原始 release 句柄必须包含补足后的全部权重")
		})
	}
}
