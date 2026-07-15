package servertiming

import (
	"context"
	"fmt"
	"sort"
	"strconv"
	"strings"
	"sync"
	"time"
)

const (
	HeaderName       = "Server-Timing"
	AdminUIHeader    = "X-Admin-UI-Request"
	UserUIHeader     = "X-User-UI-Request"
	MetricDatabase   = "db"
	MetricRedis      = "redis"
	dependencyPrefix = "dep_"

	maxMetricNameLength = 48
	maxIntervals        = 2048
	maxHeaderLength     = 4096
)

type contextKey struct{}

type interval struct {
	start time.Time
	end   time.Time
}

type metric struct {
	count     int64
	intervals []interval
}

// Collector 保存请求范围内的耗时样本，并支持并发使用。
type Collector struct {
	startedAt time.Time

	mu          sync.Mutex
	metrics     map[string]*metric
	cacheStatus string
}

// New 创建总耗时从 startedAt 开始计算的收集器。
func New(startedAt time.Time) *Collector {
	if startedAt.IsZero() {
		startedAt = time.Now()
	}
	return &Collector{
		startedAt: startedAt,
		metrics:   make(map[string]*metric),
	}
}

// WithCollector 将收集器附加到上下文。
func WithCollector(ctx context.Context, collector *Collector) context.Context {
	if ctx == nil {
		ctx = context.Background()
	}
	if collector == nil {
		return ctx
	}
	return context.WithValue(ctx, contextKey{}, collector)
}

// FromContext 在请求耗时收集启用时返回对应收集器。
func FromContext(ctx context.Context) (*Collector, bool) {
	if ctx == nil {
		return nil, false
	}
	collector, ok := ctx.Value(contextKey{}).(*Collector)
	return collector, ok && collector != nil
}

// Active 判断当前请求是否启用了耗时收集。
func Active(ctx context.Context) bool {
	_, ok := FromContext(ctx)
	return ok
}

// Record 将已完成的时间区间和操作次数加入指标。
func Record(ctx context.Context, name string, startedAt, endedAt time.Time, count int) {
	collector, ok := FromContext(ctx)
	if !ok {
		return
	}
	collector.Record(name, startedAt, endedAt, count)
}

// RecordInterval 增加耗时但不增加操作次数，适用于一次逻辑操作包含多次阻塞驱动调用的情况。
func RecordInterval(ctx context.Context, name string, startedAt, endedAt time.Time) {
	collector, ok := FromContext(ctx)
	if !ok {
		return
	}
	collector.record(name, startedAt, endedAt, 0)
}

// Record 将已完成的时间区间直接加入收集器。
func (c *Collector) Record(name string, startedAt, endedAt time.Time, count int) {
	if count <= 0 {
		count = 1
	}
	c.record(name, startedAt, endedAt, count)
}

func (c *Collector) record(name string, startedAt, endedAt time.Time, count int) {
	name = normalizeMetricName(name)
	if c == nil || name == "" || startedAt.IsZero() || endedAt.Before(startedAt) {
		return
	}
	if count < 0 {
		count = 0
	}

	c.mu.Lock()
	m := c.metrics[name]
	if m == nil {
		m = &metric{}
		c.metrics[name] = m
	}
	m.count += int64(count)
	if len(m.intervals) < maxIntervals {
		m.intervals = append(m.intervals, interval{start: startedAt, end: endedAt})
	}
	c.mu.Unlock()
}

// Observe 启动指标时间段，并返回可幂等调用的完成函数。
func Observe(ctx context.Context, name string) func() {
	collector, ok := FromContext(ctx)
	name = normalizeMetricName(name)
	if !ok || name == "" {
		return func() {}
	}
	startedAt := time.Now()
	var once sync.Once
	return func() {
		once.Do(func() {
			collector.Record(name, startedAt, time.Now(), 1)
		})
	}
}

// ObserveDependency 启动具名外部依赖时间段。
func ObserveDependency(ctx context.Context, module string) func() {
	return Observe(ctx, dependencyMetricName(module))
}

// RecordDependency 记录已完成的外部依赖时间区间。
func RecordDependency(ctx context.Context, module string, startedAt, endedAt time.Time) {
	Record(ctx, dependencyMetricName(module), startedAt, endedAt, 1)
}

// SetCacheStatus 记录当前请求的响应缓存结果。
func SetCacheStatus(ctx context.Context, status string) {
	collector, ok := FromContext(ctx)
	if !ok {
		return
	}
	status = normalizeCacheStatus(status)
	if status == "" {
		return
	}
	collector.mu.Lock()
	collector.cacheStatus = status
	collector.mu.Unlock()
}

// HeaderValue 生成长度受限且结果确定的 Server-Timing 响应头。
func HeaderValue(ctx context.Context, endedAt time.Time, cacheStatus string) string {
	collector, ok := FromContext(ctx)
	if !ok {
		return ""
	}
	return collector.HeaderValue(endedAt, cacheStatus)
}

// HeaderValue 生成长度受限且结果确定的 Server-Timing 响应头。
func (c *Collector) HeaderValue(endedAt time.Time, cacheStatus string) string {
	if c == nil {
		return ""
	}
	if endedAt.IsZero() {
		endedAt = time.Now()
	}
	if endedAt.Before(c.startedAt) {
		endedAt = c.startedAt
	}

	c.mu.Lock()
	metrics := make(map[string]metric, len(c.metrics))
	allIntervals := make([]interval, 0)
	dependencyIntervals := make([]interval, 0)
	var dependencyCount int64
	for name, source := range c.metrics {
		copied := metric{count: source.count, intervals: append([]interval(nil), source.intervals...)}
		metrics[name] = copied
		allIntervals = append(allIntervals, copied.intervals...)
		if strings.HasPrefix(name, dependencyPrefix) {
			dependencyIntervals = append(dependencyIntervals, copied.intervals...)
			dependencyCount += copied.count
		}
	}
	storedCacheStatus := c.cacheStatus
	c.mu.Unlock()

	total := endedAt.Sub(c.startedAt)
	blocked := unionDuration(allIntervals, c.startedAt, endedAt)
	app := total - blocked
	if app < 0 {
		app = 0
	}

	cacheStatus = normalizeCacheStatus(cacheStatus)
	if cacheStatus == "" {
		cacheStatus = normalizeCacheStatus(storedCacheStatus)
	}
	if cacheStatus == "" {
		cacheStatus = "bypass"
	}

	database := metrics[MetricDatabase]
	redisMetric := metrics[MetricRedis]
	parts := []string{
		"total;dur=" + formatDuration(total),
		"app;dur=" + formatDuration(app),
		fmt.Sprintf("db;dur=%s;desc=\"queries=%d\"", formatDuration(unionDuration(database.intervals, c.startedAt, endedAt)), database.count),
		fmt.Sprintf("redis;dur=%s;desc=\"commands=%d\"", formatDuration(unionDuration(redisMetric.intervals, c.startedAt, endedAt)), redisMetric.count),
		"cache;desc=\"" + cacheStatus + "\"",
		fmt.Sprintf("deps;dur=%s;desc=\"calls=%d\"", formatDuration(unionDuration(dependencyIntervals, c.startedAt, endedAt)), dependencyCount),
	}

	dependencyNames := make([]string, 0)
	for name := range metrics {
		if strings.HasPrefix(name, dependencyPrefix) {
			dependencyNames = append(dependencyNames, name)
		}
	}
	sort.Strings(dependencyNames)
	for _, name := range dependencyNames {
		m := metrics[name]
		part := fmt.Sprintf("%s;dur=%s;desc=\"calls=%d\"", name, formatDuration(unionDuration(m.intervals, c.startedAt, endedAt)), m.count)
		candidate := strings.Join(append(parts, part), ", ")
		if len(candidate) > maxHeaderLength {
			break
		}
		parts = append(parts, part)
	}

	return strings.Join(parts, ", ")
}

func dependencyMetricName(module string) string {
	module = normalizeMetricName(module)
	module = strings.TrimPrefix(module, dependencyPrefix)
	if module == "" {
		module = "http"
	}
	return dependencyPrefix + module
}

func normalizeMetricName(name string) string {
	name = strings.ToLower(strings.TrimSpace(name))
	if name == "" {
		return ""
	}
	var b strings.Builder
	b.Grow(min(len(name), maxMetricNameLength))
	for _, r := range name {
		if b.Len() >= maxMetricNameLength {
			break
		}
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			_, _ = b.WriteRune(r)
		case r == '_' || r == '-':
			_ = b.WriteByte('_')
		}
	}
	return strings.Trim(b.String(), "_")
}

func normalizeCacheStatus(status string) string {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "hit":
		return "hit"
	case "miss":
		return "miss"
	case "bypass":
		return "bypass"
	default:
		return ""
	}
}

func unionDuration(intervals []interval, lowerBound, upperBound time.Time) time.Duration {
	if len(intervals) == 0 || !upperBound.After(lowerBound) {
		return 0
	}
	normalized := make([]interval, 0, len(intervals))
	for _, item := range intervals {
		start := item.start
		end := item.end
		if start.Before(lowerBound) {
			start = lowerBound
		}
		if end.After(upperBound) {
			end = upperBound
		}
		if end.After(start) {
			normalized = append(normalized, interval{start: start, end: end})
		}
	}
	if len(normalized) == 0 {
		return 0
	}
	sort.Slice(normalized, func(i, j int) bool {
		return normalized[i].start.Before(normalized[j].start)
	})

	currentStart := normalized[0].start
	currentEnd := normalized[0].end
	var total time.Duration
	for _, item := range normalized[1:] {
		if !item.start.After(currentEnd) {
			if item.end.After(currentEnd) {
				currentEnd = item.end
			}
			continue
		}
		total += currentEnd.Sub(currentStart)
		currentStart = item.start
		currentEnd = item.end
	}
	total += currentEnd.Sub(currentStart)
	return total
}

func formatDuration(value time.Duration) string {
	if value < 0 {
		value = 0
	}
	return strconv.FormatFloat(float64(value)/float64(time.Millisecond), 'f', 1, 64)
}
