package service

// usage_logs.request_id 的列宽治理。
//
// 列定义是 varchar(64)，同时它与 api_key_id 组成幂等唯一键，也是计费去重的键。HTTP 入口的
// request_id 取自网关自己生成的客户端请求标识（"client:" 或 "local:" 加 UUID），天然在 64 个字符内；
// 上游自己的请求标识另存 upstream_request_id 列（128 个字符，超长截断）。
//
// WebSocket 轮次不同：同一条连接的所有轮次共用一个客户端请求标识，不能区分轮次，所以 WS 轮次的
// request_id 取上游响应 ID，保证「一轮一个、可重放不重复」。官方 OpenAI 的响应 ID 在 64 个字符内，
// 但第三方上游（Command Code 等）的响应 ID 可以更长，原样写入会触发
// "value too long for type character varying(64)"，整条用量记录丢失（计费去重表的列更宽，
// 扣费已经发生，却没有用量行）。
//
// 因此在写用量的唯一出口把 request_id 收敛到列宽以内：超长时保留前缀并追加完整 ID 的摘要。
// 规则是确定性的（同一个响应 ID 永远得到同一个 request_id，幂等与计费去重不受影响），
// 摘要使不同的超长 ID 不会撞键；完整的响应 ID 另存 upstream_request_id，可直接按原 ID 追溯。

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"unicode/utf8"
)

const (
	// maxUsageLogRequestIDLen 是 usage_logs.request_id 的列宽（ent schema MaxLen(64)）。
	maxUsageLogRequestIDLen = 64
	// usageLogRequestIDDigestLen 是超长时附加的摘要十六进制字符数（64 位，同一连接内撞键概率可忽略）。
	usageLogRequestIDDigestLen = 16
)

// capUsageLogRequestID 把 request_id 收敛到列宽以内。未超长时原样返回；
// 超长时返回「前缀 + ~ + 完整 ID 的 SHA-256 前 16 位十六进制」，总长不超过列宽，且不会截断多字节字符。
func capUsageLogRequestID(requestID string) string {
	if len(requestID) <= maxUsageLogRequestIDLen {
		return requestID
	}
	sum := sha256.Sum256([]byte(requestID))
	digest := hex.EncodeToString(sum[:])[:usageLogRequestIDDigestLen]
	prefixLen := maxUsageLogRequestIDLen - usageLogRequestIDDigestLen - 1
	prefix := requestID[:prefixLen]
	for len(prefix) > 0 && !utf8.ValidString(prefix) {
		prefix = prefix[:len(prefix)-1]
	}
	return prefix + "~" + digest
}

// usageUpstreamRequestIDForCappedRequestID 在 request_id 因超长被收敛时，返回要写进
// usage_logs.upstream_request_id 的完整原始 ID（按该列宽截断），让用户和运维仍能按原始响应 ID 找到这条记录。
// request_id 没有被收敛时返回 nil，不改变既有行为。
func usageUpstreamRequestIDForCappedRequestID(original, capped string) *string {
	if original == capped {
		return nil
	}
	id := strings.TrimSpace(original)
	if len(id) > maxUsageUpstreamRequestIDLen {
		id = id[:maxUsageUpstreamRequestIDLen]
		for len(id) > 0 && !utf8.ValidString(id) {
			id = id[:len(id)-1]
		}
	}
	if id == "" {
		return nil
	}
	return &id
}
