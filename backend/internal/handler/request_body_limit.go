package handler

import (
	"errors"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	appconfig "github.com/Wei-Shaw/sub2api/internal/config"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/gin-gonic/gin"
	"go.uber.org/zap"
)

const (
	defaultRequestMemoryAmplification    = 5.0
	defaultRequestMemoryFixedBytes       = 8 << 20
	defaultRequestMemoryRetryAfter       = 2
	responsesRequestMemoryReservationKey = "openai_responses_request_memory_reservation"
)

// 准入回调已经写入 413/503 响应时，用独立错误通知读取入口立即返回。
var errResponsesRequestMemoryRejected = errors.New("responses request memory reservation rejected")

// requestMemoryAdmission 是 Responses 正文处理的进程级加权准入器。
// 它只保存计数和固定大小的统计，不保存请求正文；正文读取前先占用预算，
// 请求生命周期结束后释放，避免并发排队请求在获得账号槽位前堆积大对象。
type requestMemoryAdmission struct {
	capacityBytes int64
	usedBytes     atomic.Int64
	accepted      atomic.Uint64
	rejected      atomic.Uint64
	tooLarge      atomic.Uint64
}

type requestMemoryReservation struct {
	mu        sync.Mutex
	admission *requestMemoryAdmission
	bytes     int64
	released  atomic.Bool
}

func newRequestMemoryAdmission(cfg *appconfig.Config) *requestMemoryAdmission {
	if cfg == nil || cfg.Gateway.RequestMemoryBudgetBytes <= 0 {
		return nil
	}
	return &requestMemoryAdmission{capacityBytes: cfg.Gateway.RequestMemoryBudgetBytes}
}

func (a *requestMemoryAdmission) tryAcquire(bytes int64) (*requestMemoryReservation, bool) {
	if a == nil || a.capacityBytes <= 0 || bytes <= 0 {
		return &requestMemoryReservation{}, true
	}
	for {
		used := a.usedBytes.Load()
		if used > a.capacityBytes-bytes {
			a.rejected.Add(1)
			return nil, false
		}
		if a.usedBytes.CompareAndSwap(used, used+bytes) {
			a.accepted.Add(1)
			return &requestMemoryReservation{admission: a, bytes: bytes}, true
		}
	}
}

func (r *requestMemoryReservation) release() {
	if r == nil {
		return
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.released.Swap(true) {
		return
	}
	if r.admission != nil && r.admission.capacityBytes > 0 {
		r.admission.usedBytes.Add(-r.bytes)
	}
	r.bytes = 0
}

// resize 原子替换连接当前权重。WS 按到达的正文增量占用同一个 HTTP 预算，
// 跨轮缓存缩小时直接归还差额；失败不改变原有预留。
func (r *requestMemoryReservation) resize(bytes int64) bool {
	if r == nil || bytes < 0 {
		return false
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.released.Load() {
		return false
	}
	a := r.admission
	if a == nil || a.capacityBytes <= 0 {
		r.bytes = bytes
		return true
	}
	delta := bytes - r.bytes
	if delta <= 0 {
		a.usedBytes.Add(delta)
		r.bytes = bytes
		return true
	}
	for {
		used := a.usedBytes.Load()
		if used > a.capacityBytes-delta {
			a.rejected.Add(1)
			return false
		}
		if a.usedBytes.CompareAndSwap(used, used+delta) {
			if r.bytes == 0 {
				a.accepted.Add(1)
			}
			r.bytes = bytes
			return true
		}
	}
}

func (a *requestMemoryAdmission) stats() (used, capacity int64, accepted, rejected, tooLarge uint64) {
	if a == nil {
		return 0, 0, 0, 0, 0
	}
	return a.usedBytes.Load(), a.capacityBytes, a.accepted.Load(), a.rejected.Load(), a.tooLarge.Load()
}

// requestMemoryAdmissionConfig 从配置计算单请求硬上限和加权预算。
func requestMemoryAdmissionConfig(cfg *appconfig.Config) (maxRequest, fixed int64, amplification float64) {
	if cfg == nil {
		return 0, defaultRequestMemoryFixedBytes, defaultRequestMemoryAmplification
	}
	maxRequest = cfg.Gateway.RequestMemoryMaxRequestBytes
	if maxRequest <= 0 {
		maxRequest = cfg.Gateway.TextMaxBodySize
	}
	if maxRequest <= 0 {
		maxRequest = cfg.Gateway.MaxBodySize
	}
	fixed = cfg.Gateway.RequestMemoryFixedBytes
	if fixed < 0 {
		fixed = 0
	}
	if fixed == 0 {
		fixed = defaultRequestMemoryFixedBytes
	}
	amplification = cfg.Gateway.RequestMemoryAmplification
	if amplification < 1 {
		amplification = defaultRequestMemoryAmplification
	}
	return maxRequest, fixed, amplification
}

func requestMemoryWeight(req *http.Request, cfg *appconfig.Config) (weight int64, tooLarge bool) {
	maxRequest, fixed, amplification := requestMemoryAdmissionConfig(cfg)
	if req == nil {
		return fixed, false
	}
	contentLength := req.ContentLength
	encoding := strings.ToLower(strings.TrimSpace(req.Header.Get("Content-Encoding")))
	// 压缩正文的 Content-Length 只代表压缩后的大小，chunked 请求没有长度；
	// 两者都按单请求上限预留，不能让解压后的正文绕过准入。
	if contentLength >= 0 && (encoding == "" || encoding == "identity") {
		if maxRequest > 0 && contentLength > maxRequest {
			return 0, true
		}
	} else {
		contentLength = maxRequest
	}
	if contentLength <= 0 {
		contentLength = 1 << 20
	}
	if maxRequest > 0 && contentLength > maxRequest {
		return 0, true
	}
	calculated := float64(contentLength)*amplification + float64(fixed)
	if calculated >= float64(math.MaxInt64) {
		return math.MaxInt64, false
	}
	return int64(math.Ceil(calculated)), false
}

// ensureResponsesRequestMemoryForBody 在兼容 JSON 规范化分配前按计算出的正文大小补足原预留。
// 裸控制字符转义可能扩大正文；压缩或未知长度已按最大值预留，不因当前正文变小提前降额。
// 仍使用 acquire 注册的同一句柄，因此无论转发成功还是补额失败，原有 defer 都能完整释放。
func (h *OpenAIGatewayHandler) ensureResponsesRequestMemoryForBody(c *gin.Context, bodyBytes int, reqLog *zap.Logger) bool {
	// 仅含 BOM 的正文规范化后为空，后续仍应返回空正文错误，不按未知长度补额。
	if bodyBytes <= 0 || h == nil || h.cfg == nil || c == nil || h.cfg.Gateway.RequestMemoryBudgetBytes <= 0 {
		return true
	}
	value, exists := c.Get(responsesRequestMemoryReservationKey)
	reservation, ok := value.(*requestMemoryReservation)
	if !exists || !ok || reservation == nil {
		return true
	}
	weight, tooLarge := requestMemoryWeight(&http.Request{ContentLength: int64(bodyBytes)}, h.cfg)
	if tooLarge {
		if reservation.admission != nil {
			reservation.admission.tooLarge.Add(1)
		}
		maxRequest, _, _ := requestMemoryAdmissionConfig(h.cfg)
		h.errorResponse(c, http.StatusRequestEntityTooLarge, "invalid_request_error", buildBodyTooLargeMessage(maxRequest))
		return false
	}
	reservation.mu.Lock()
	currentWeight := reservation.bytes
	reservation.mu.Unlock()
	if weight <= currentWeight || reservation.resize(weight) {
		return true
	}
	c.Header("Retry-After", strconv.Itoa(defaultRequestMemoryRetryAfter))
	if reqLog != nil {
		used, capacity, _, rejected, _ := reservation.admission.stats()
		reqLog.Warn("openai.request_memory_rejected",
			zap.String("reason", "budget_exhausted"),
			zap.String("phase", "normalized_body"),
			zap.Int("body_bytes", bodyBytes),
			zap.Int64("requested_weight_bytes", weight),
			zap.Int64("used_bytes", used),
			zap.Int64("capacity_bytes", capacity),
			zap.Uint64("rejected_total", rejected),
		)
	}
	h.errorResponse(c, http.StatusServiceUnavailable, "rate_limit_error", "Request memory budget is temporarily exhausted; retry later")
	return false
}

func extractMaxBytesError(err error) (*http.MaxBytesError, bool) {
	var maxErr *http.MaxBytesError
	if errors.As(err, &maxErr) {
		return maxErr, true
	}
	return nil, false
}

func formatBodyLimit(limit int64) string {
	const mb = 1024 * 1024
	if limit >= mb {
		return fmt.Sprintf("%dMB", limit/mb)
	}
	return fmt.Sprintf("%dB", limit)
}

func buildBodyTooLargeMessage(limit int64) string {
	return fmt.Sprintf("Request body too large, limit is %s", formatBodyLimit(limit))
}

func readLenientJSONRequestBodyWithPrealloc(req *http.Request, cfg *appconfig.Config) ([]byte, error) {
	return pkghttputil.ReadLenientJSONRequestBodyWithPrealloc(req, gatewayMaxBodySize(cfg))
}

func readResponsesJSONRequestBodyWithPrealloc(req *http.Request, cfg *appconfig.Config) ([]byte, error) {
	limit := gatewayMaxBodySize(cfg)
	if maxRequest, _, _ := requestMemoryAdmissionConfig(cfg); maxRequest > 0 &&
		(limit <= 0 || maxRequest < limit) {
		limit = maxRequest
	}
	return pkghttputil.ReadLenientJSONRequestBodyWithPrealloc(req, limit)
}

// 读取正文后先计算规范化长度并申请差额，只有准入成功才能分配扩展后的正文。
func readAdmittedResponsesJSONRequestBodyWithReservation(req *http.Request, cfg *appconfig.Config, beforeNormalize func(int) error) ([]byte, error) {
	if cfg == nil || cfg.Gateway.RequestMemoryBudgetBytes <= 0 {
		return readResponsesJSONRequestBodyWithPrealloc(req, cfg)
	}
	limit := gatewayMaxBodySize(cfg)
	if maxRequest, _, _ := requestMemoryAdmissionConfig(cfg); maxRequest > 0 && (limit <= 0 || maxRequest < limit) {
		limit = maxRequest
	}
	return pkghttputil.ReadAdmittedLenientJSONRequestBodyWithReservation(req, limit, beforeNormalize)
}

// 显式所有权入口只在已启用准入时使用；未准入和旧调用者保持原有堆正文语义。
// 调用方必须把 owner 保留到全部请求处理与账号重试结束，之后同步 Close。
func readOwnedAdmittedResponsesJSONRequestBody(req *http.Request, cfg *appconfig.Config, beforeNormalize func(int) error) ([]byte, *pkghttputil.OwnedRequestBody, error) {
	if cfg == nil || cfg.Gateway.RequestMemoryBudgetBytes <= 0 {
		body, err := readResponsesJSONRequestBodyWithPrealloc(req, cfg)
		return body, nil, err
	}
	limit := gatewayMaxBodySize(cfg)
	if maxRequest, _, _ := requestMemoryAdmissionConfig(cfg); maxRequest > 0 && (limit <= 0 || maxRequest < limit) {
		limit = maxRequest
	}
	owner, err := pkghttputil.ReadOwnedAdmittedLenientJSONRequestBodyWithReservation(req, limit, beforeNormalize)
	if err != nil {
		return nil, nil, err
	}
	return owner.Bytes(), owner, nil
}

func gatewayMaxBodySize(cfg *appconfig.Config) int64 {
	if cfg == nil {
		return 0
	}
	return cfg.Gateway.MaxBodySize
}
