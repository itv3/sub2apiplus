package service

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"strings"
	"time"
	"unsafe"
)

const (
	openAIInvalidEncryptedDigestMaxCount  = 128
	openAIInvalidEncryptedAccountMaxCount = 1024
	// 一个账号允许保留少量相互隔离的链路作用域。作用域数量和每个作用域
	// 的摘要数量都固定上限，避免把上游返回的任意 ID 变成无界进程内存。
	openAIInvalidEncryptedScopeMaxCount            = 4096
	openAIInvalidEncryptedScopesPerAccountMaxCount = 16
)

type openAIInvalidEncryptedDigest [sha256.Size]byte

// 哈希只同步读取字符串，返回的固定长度摘要不引用原文。直接借用只读视图，
// 避免在坏密文重试时为整段 encrypted_content 再复制一份字节切片。
func openAIEncryptedContentSHA256(value string) [sha256.Size]byte {
	return sha256.Sum256(unsafe.Slice(unsafe.StringData(value), len(value)))
}

type openAIInvalidEncryptedAccountBinding struct {
	digests   map[openAIInvalidEncryptedDigest]struct{}
	expiresAt time.Time
}

// openAIInvalidEncryptedScope 将坏摘要绑定到一次可重放链路，而不是只绑定账号。
// 同一账号切换模型、协议、端点或响应链后，不能因为旧链路收到过错误就预先删除
// 新链路中可能完全合法的 reasoning.encrypted_content。
type openAIInvalidEncryptedScope struct {
	Model    string
	Protocol string
	Endpoint string
	ChainKey string
}

type openAIInvalidEncryptedScopeKey struct {
	AccountID int64
	Scope     openAIInvalidEncryptedScope
}

func (scope openAIInvalidEncryptedScope) valid() bool {
	return strings.TrimSpace(scope.Model) != "" &&
		strings.TrimSpace(scope.Protocol) != "" &&
		strings.TrimSpace(scope.Endpoint) != "" &&
		strings.TrimSpace(scope.ChainKey) != ""
}

// hashOpenAIInvalidEncryptedChainKey 只在内存键中保存固定长度摘要，避免把用户
// 可控的 previous_response_id/prompt_cache_key 原文长期留在进程堆和调试转储中。
func hashOpenAIInvalidEncryptedChainKey(kind, value string) string {
	value = strings.TrimSpace(value)
	if value == "" {
		return ""
	}
	digest := sha256.Sum256([]byte(value))
	return kind + ":" + hex.EncodeToString(digest[:])
}

// buildOpenAIInvalidEncryptedScope 根据最终上游请求构造稳定作用域。
// 没有 previous_response_id 或 prompt_cache_key 时返回 false：此时没有可靠的
// 跨请求链路标识，调用方只能保留“收到错误后清洗并重试一次”的反应式恢复，
// 不得把摘要用于后续请求的发送前预清理。
func buildOpenAIInvalidEncryptedScope(
	account *Account,
	model string,
	transport OpenAIUpstreamTransport,
	compact bool,
	passthrough bool,
	reqBody map[string]any,
	fallbackPromptCacheKey string,
) (openAIInvalidEncryptedScope, bool) {
	if account == nil {
		return openAIInvalidEncryptedScope{}, false
	}
	model = strings.TrimSpace(model)
	if model == "" {
		model = firstNonEmptyString(reqBody["model"])
	}
	if model == "" {
		return openAIInvalidEncryptedScope{}, false
	}

	// 把账号平台/类型/API 协议和实际传输一起纳入协议维度。这样同一账号
	// 在官方 WS、官方 HTTP、API-key HTTP 或国产兼容协议之间不会共享坏摘要。
	protocol := strings.Join([]string{
		strings.TrimSpace(account.Platform),
		strings.TrimSpace(account.Type),
		strings.TrimSpace(account.GetAPIProtocol()),
		strings.TrimSpace(string(transport)),
	}, "/")
	if protocol == "///" {
		return openAIInvalidEncryptedScope{}, false
	}

	endpoint := "responses_http"
	if transport == OpenAIUpstreamTransportResponsesWebsocketV2 {
		endpoint = "responses_ws_v2"
	}
	if compact {
		endpoint = "responses_compact"
	}
	if account.IsOpenAIOAuthLike() {
		endpoint = "official/" + endpoint
	} else {
		endpoint = "compatible/" + endpoint
	}
	if passthrough {
		endpoint += "/passthrough"
	}

	chainValue := firstNonEmptyString(reqBody["previous_response_id"])
	chainKey := hashOpenAIInvalidEncryptedChainKey("previous", chainValue)
	if chainKey == "" {
		chainValue = firstNonEmptyString(reqBody["prompt_cache_key"], fallbackPromptCacheKey)
		chainKey = hashOpenAIInvalidEncryptedChainKey("prompt", chainValue)
	}
	if chainKey == "" {
		return openAIInvalidEncryptedScope{}, false
	}
	return openAIInvalidEncryptedScope{
		// 作用域会进入跨请求缓存，不能保留共享解码借用的入站正文。
		Model:    strings.Clone(model),
		Protocol: protocol,
		Endpoint: endpoint,
		ChainKey: chainKey,
	}, true
}

func openAIEncryptedReasoningItemDigest(item any) (openAIInvalidEncryptedDigest, bool) {
	inputItem, ok := item.(map[string]any)
	if !ok {
		return openAIInvalidEncryptedDigest{}, false
	}
	itemType := strings.TrimSpace(firstNonEmptyString(inputItem["type"]))
	if itemType != "reasoning" && itemType != "compaction" && itemType != "compaction_summary" {
		return openAIInvalidEncryptedDigest{}, false
	}
	encryptedContent, exists := inputItem["encrypted_content"]
	if !exists {
		return openAIInvalidEncryptedDigest{}, false
	}
	if encryptedString, ok := encryptedContent.(string); ok {
		return openAIEncryptedContentSHA256(encryptedString), true
	}
	encoded, err := json.Marshal(encryptedContent)
	if err != nil {
		return openAIInvalidEncryptedDigest{}, false
	}
	return sha256.Sum256(encoded), true
}

func collectOpenAIEncryptedReasoningDigests(reqBody map[string]any) map[openAIInvalidEncryptedDigest]struct{} {
	if len(reqBody) == 0 {
		return nil
	}
	input, exists := reqBody["input"]
	if !exists {
		return nil
	}
	digests := make(map[openAIInvalidEncryptedDigest]struct{})
	collect := func(item any) {
		if len(digests) >= openAIInvalidEncryptedDigestMaxCount {
			return
		}
		if digest, ok := openAIEncryptedReasoningItemDigest(item); ok {
			digests[digest] = struct{}{}
		}
	}
	switch items := input.(type) {
	case []any:
		for _, item := range items {
			collect(item)
		}
	case []map[string]any:
		for _, item := range items {
			collect(item)
		}
	case map[string]any:
		collect(items)
	}
	if len(digests) == 0 {
		return nil
	}
	return digests
}

func mergeOpenAIInvalidEncryptedDigests(
	base map[openAIInvalidEncryptedDigest]struct{},
	extra map[openAIInvalidEncryptedDigest]struct{},
) map[openAIInvalidEncryptedDigest]struct{} {
	if len(base) == 0 && len(extra) == 0 {
		return nil
	}
	merged := make(map[openAIInvalidEncryptedDigest]struct{}, min(
		len(base)+len(extra),
		openAIInvalidEncryptedDigestMaxCount,
	))
	// 新确认的坏摘要优先进入有界集合；达到上限后再用旧摘要补齐。
	for digest := range extra {
		if len(merged) >= openAIInvalidEncryptedDigestMaxCount {
			break
		}
		merged[digest] = struct{}{}
	}
	for digest := range base {
		if len(merged) >= openAIInvalidEncryptedDigestMaxCount {
			break
		}
		merged[digest] = struct{}{}
	}
	return merged
}

func trimOpenAIInvalidEncryptedReasoningItems(
	reqBody map[string]any,
	digests map[openAIInvalidEncryptedDigest]struct{},
) bool {
	if len(digests) == 0 {
		return false
	}
	return trimOpenAIEncryptedReasoningItemsIf(reqBody, func(item any) bool {
		digest, ok := openAIEncryptedReasoningItemDigest(item)
		if !ok {
			return false
		}
		_, invalid := digests[digest]
		return invalid
	})
}

func (s *OpenAIGatewayService) openAIInvalidEncryptedAccountDigests(
	accountID int64,
) map[openAIInvalidEncryptedDigest]struct{} {
	if s == nil || accountID <= 0 {
		return nil
	}
	now := time.Now()
	s.openaiInvalidEncryptedAccountsMu.Lock()
	defer s.openaiInvalidEncryptedAccountsMu.Unlock()
	binding, exists := s.openaiInvalidEncryptedAccounts[accountID]
	if !exists {
		return nil
	}
	if !binding.expiresAt.IsZero() && now.After(binding.expiresAt) {
		delete(s.openaiInvalidEncryptedAccounts, accountID)
		return nil
	}
	// binding 中的摘要 map 写入后保持不可变；后续合并会创建新 map，
	// 因此可在解锁后安全地只读使用，且无需为每个请求复制摘要正文。
	return binding.digests
}

func (s *OpenAIGatewayService) bindOpenAIInvalidEncryptedAccount(
	accountID int64,
	digests map[openAIInvalidEncryptedDigest]struct{},
) {
	if s == nil || accountID <= 0 || len(digests) == 0 {
		return
	}
	now := time.Now()
	s.openaiInvalidEncryptedAccountsMu.Lock()
	defer s.openaiInvalidEncryptedAccountsMu.Unlock()
	if s.openaiInvalidEncryptedAccounts == nil {
		s.openaiInvalidEncryptedAccounts = make(
			map[int64]openAIInvalidEncryptedAccountBinding,
			min(openAIInvalidEncryptedAccountMaxCount, 16),
		)
	}
	for cachedAccountID, binding := range s.openaiInvalidEncryptedAccounts {
		if !binding.expiresAt.IsZero() && now.After(binding.expiresAt) {
			delete(s.openaiInvalidEncryptedAccounts, cachedAccountID)
		}
	}
	if existing, exists := s.openaiInvalidEncryptedAccounts[accountID]; exists {
		digests = mergeOpenAIInvalidEncryptedDigests(existing.digests, digests)
	} else {
		digests = mergeOpenAIInvalidEncryptedDigests(nil, digests)
		if len(s.openaiInvalidEncryptedAccounts) >= openAIInvalidEncryptedAccountMaxCount {
			s.evictOldestOpenAIInvalidEncryptedAccountLocked()
		}
	}
	s.openaiInvalidEncryptedAccounts[accountID] = openAIInvalidEncryptedAccountBinding{
		digests:   digests,
		expiresAt: now.Add(s.openAIWSResponseStickyTTL()),
	}
}

// openAIInvalidEncryptedScopeDigests 读取生产路径使用的精确作用域缓存。
// 作用域无效时返回 nil，明确表示本请求只能走反应式恢复。
func (s *OpenAIGatewayService) openAIInvalidEncryptedScopeDigests(
	accountID int64,
	scope openAIInvalidEncryptedScope,
) map[openAIInvalidEncryptedDigest]struct{} {
	if s == nil || accountID <= 0 || !scope.valid() {
		return nil
	}
	now := time.Now()
	s.openaiInvalidEncryptedAccountsMu.Lock()
	defer s.openaiInvalidEncryptedAccountsMu.Unlock()
	if len(s.openaiInvalidEncryptedScopes) == 0 {
		return nil
	}
	key := openAIInvalidEncryptedScopeKey{AccountID: accountID, Scope: scope}
	binding, exists := s.openaiInvalidEncryptedScopes[key]
	if !exists {
		return nil
	}
	if !binding.expiresAt.IsZero() && now.After(binding.expiresAt) {
		delete(s.openaiInvalidEncryptedScopes, key)
		return nil
	}
	return binding.digests
}

// bindOpenAIInvalidEncryptedScope 绑定一次反应式恢复确认的坏摘要。
// 没有稳定链路键时直接忽略，防止账号级“污染”重新出现。
func (s *OpenAIGatewayService) bindOpenAIInvalidEncryptedScope(
	accountID int64,
	scope openAIInvalidEncryptedScope,
	digests map[openAIInvalidEncryptedDigest]struct{},
) {
	if s == nil || accountID <= 0 || !scope.valid() || len(digests) == 0 {
		return
	}
	now := time.Now()
	s.openaiInvalidEncryptedAccountsMu.Lock()
	defer s.openaiInvalidEncryptedAccountsMu.Unlock()
	if s.openaiInvalidEncryptedScopes == nil {
		s.openaiInvalidEncryptedScopes = make(
			map[openAIInvalidEncryptedScopeKey]openAIInvalidEncryptedAccountBinding,
			min(openAIInvalidEncryptedScopeMaxCount, 64),
		)
	}
	for key, binding := range s.openaiInvalidEncryptedScopes {
		if !binding.expiresAt.IsZero() && now.After(binding.expiresAt) {
			delete(s.openaiInvalidEncryptedScopes, key)
		}
	}
	key := openAIInvalidEncryptedScopeKey{AccountID: accountID, Scope: scope}
	if existing, exists := s.openaiInvalidEncryptedScopes[key]; exists {
		digests = mergeOpenAIInvalidEncryptedDigests(existing.digests, digests)
	} else {
		// 先限制单账号作用域数量，再限制全局作用域数量；两层上限都只
		// 淘汰最早到期项，不影响其他账号的恢复状态。
		if s.countOpenAIInvalidEncryptedScopesForAccountLocked(accountID) >= openAIInvalidEncryptedScopesPerAccountMaxCount {
			s.evictOldestOpenAIInvalidEncryptedScopeLocked(accountID)
		}
		if len(s.openaiInvalidEncryptedScopes) >= openAIInvalidEncryptedScopeMaxCount {
			s.evictOldestOpenAIInvalidEncryptedScopeLocked(0)
		}
		digests = mergeOpenAIInvalidEncryptedDigests(nil, digests)
	}
	s.openaiInvalidEncryptedScopes[key] = openAIInvalidEncryptedAccountBinding{
		digests:   digests,
		expiresAt: now.Add(s.openAIWSResponseStickyTTL()),
	}
}

// bindOpenAIInvalidEncryptedScopeWithResponseID 同时绑定恢复前的链路键和本次
// 成功响应生成的新 previous_response_id。响应 ID 会在下一轮成为新的链路键；
// 这样仍然只影响同一模型/协议/端点作用域，不会退化成账号级全局清洗。
func (s *OpenAIGatewayService) bindOpenAIInvalidEncryptedScopeWithResponseID(
	accountID int64,
	scope openAIInvalidEncryptedScope,
	responseID string,
	digests map[openAIInvalidEncryptedDigest]struct{},
) {
	s.bindOpenAIInvalidEncryptedScope(accountID, scope, digests)
	responseID = strings.TrimSpace(responseID)
	if responseID == "" || !scope.valid() {
		return
	}
	nextScope := scope
	nextScope.ChainKey = hashOpenAIInvalidEncryptedChainKey("previous", responseID)
	s.bindOpenAIInvalidEncryptedScope(accountID, nextScope, digests)
}

func (s *OpenAIGatewayService) countOpenAIInvalidEncryptedScopesForAccountLocked(accountID int64) int {
	count := 0
	for key := range s.openaiInvalidEncryptedScopes {
		if key.AccountID == accountID {
			count++
		}
	}
	return count
}

// evictOldestOpenAIInvalidEncryptedScopeLocked accountID 为 0 时淘汰全局最早项。
// 调用方必须持有 openaiInvalidEncryptedAccountsMu。
func (s *OpenAIGatewayService) evictOldestOpenAIInvalidEncryptedScopeLocked(accountID int64) {
	var oldestKey openAIInvalidEncryptedScopeKey
	var oldestExpiry time.Time
	for key, binding := range s.openaiInvalidEncryptedScopes {
		if accountID != 0 && key.AccountID != accountID {
			continue
		}
		if oldestKey.AccountID == 0 || binding.expiresAt.Before(oldestExpiry) {
			oldestKey = key
			oldestExpiry = binding.expiresAt
		}
	}
	if oldestKey.AccountID != 0 {
		delete(s.openaiInvalidEncryptedScopes, oldestKey)
	}
}

// evictOldestOpenAIInvalidEncryptedAccountLocked 在容量已满时淘汰最早到期项。
// 调用方必须持有 openaiInvalidEncryptedAccountsMu。
func (s *OpenAIGatewayService) evictOldestOpenAIInvalidEncryptedAccountLocked() {
	var oldestAccountID int64
	var oldestExpiry time.Time
	for accountID, binding := range s.openaiInvalidEncryptedAccounts {
		if oldestAccountID == 0 || binding.expiresAt.Before(oldestExpiry) {
			oldestAccountID = accountID
			oldestExpiry = binding.expiresAt
		}
	}
	if oldestAccountID != 0 {
		delete(s.openaiInvalidEncryptedAccounts, oldestAccountID)
	}
}
