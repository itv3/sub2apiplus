package service

import (
	"crypto/sha256"
	"encoding/json"
	"strings"
	"time"
)

const (
	openAIInvalidEncryptedDigestMaxCount  = 128
	openAIInvalidEncryptedAccountMaxCount = 1024
)

type openAIInvalidEncryptedDigest [sha256.Size]byte

type openAIInvalidEncryptedAccountBinding struct {
	digests   map[openAIInvalidEncryptedDigest]struct{}
	expiresAt time.Time
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
		return sha256.Sum256([]byte(encryptedString)), true
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
