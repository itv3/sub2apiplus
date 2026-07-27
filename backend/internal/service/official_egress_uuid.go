package service

import (
	"sync"
	"time"

	"github.com/google/uuid"
)

const (
	officialUUIDV7CacheTTL        = 24 * time.Hour
	officialUUIDV7CacheMaxEntries = 65536
)

type officialUUIDV7Binding struct {
	value    string
	lastUsed time.Time
}

var officialUUIDV7Cache = struct {
	sync.Mutex
	values map[string]officialUUIDV7Binding
}{values: make(map[string]officialUUIDV7Binding)}

// generateOfficialStableUUIDV7 为官方 Codex 的 session/turn 字段生成真实时间有序
// UUIDv7，并在进程内按会话种子稳定复用。缓存有 TTL 和硬上限，避免使用把哈希位
// 伪装成 v7 时间戳的“改版本位”方案，也避免无界保存用户会话标识。
func generateOfficialStableUUIDV7(seed string) string {
	now := time.Now()
	officialUUIDV7Cache.Lock()
	defer officialUUIDV7Cache.Unlock()
	if binding, exists := officialUUIDV7Cache.values[seed]; exists &&
		now.Sub(binding.lastUsed) <= officialUUIDV7CacheTTL {
		binding.lastUsed = now
		officialUUIDV7Cache.values[seed] = binding
		return binding.value
	}
	if len(officialUUIDV7Cache.values) >= officialUUIDV7CacheMaxEntries {
		for key, binding := range officialUUIDV7Cache.values {
			if now.Sub(binding.lastUsed) > officialUUIDV7CacheTTL {
				delete(officialUUIDV7Cache.values, key)
			}
		}
		for key := range officialUUIDV7Cache.values {
			if len(officialUUIDV7Cache.values) < officialUUIDV7CacheMaxEntries {
				break
			}
			delete(officialUUIDV7Cache.values, key)
		}
	}
	value, err := uuid.NewV7()
	if err != nil {
		value = uuid.New()
	}
	result := value.String()
	officialUUIDV7Cache.values[seed] = officialUUIDV7Binding{value: result, lastUsed: now}
	return result
}
