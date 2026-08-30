package service

import (
	"fmt"
	"testing"
	"time"

	"github.com/stretchr/testify/require"
)

func TestOpenAIInvalidEncryptedAccountCacheIsAccountScoped(t *testing.T) {
	svc := &OpenAIGatewayService{}
	digest := openAIInvalidEncryptedDigestForTest("cipher-a")
	svc.bindOpenAIInvalidEncryptedAccount(1, map[openAIInvalidEncryptedDigest]struct{}{digest: {}})

	_, firstAccountHasDigest := svc.openAIInvalidEncryptedAccountDigests(1)[digest]
	require.True(t, firstAccountHasDigest)
	require.Empty(t, svc.openAIInvalidEncryptedAccountDigests(2))
}

func TestOpenAIInvalidEncryptedAccountCacheExpires(t *testing.T) {
	svc := &OpenAIGatewayService{
		openaiInvalidEncryptedAccounts: map[int64]openAIInvalidEncryptedAccountBinding{
			1: {
				digests: map[openAIInvalidEncryptedDigest]struct{}{
					openAIInvalidEncryptedDigestForTest("expired"): {},
				},
				expiresAt: time.Now().Add(-time.Second),
			},
		},
	}

	require.Empty(t, svc.openAIInvalidEncryptedAccountDigests(1))
	require.Empty(t, svc.openaiInvalidEncryptedAccounts)
}

func TestOpenAIInvalidEncryptedAccountCacheBoundsAccountsAndDigests(t *testing.T) {
	svc := &OpenAIGatewayService{}
	for accountID := int64(1); accountID <= openAIInvalidEncryptedAccountMaxCount+1; accountID++ {
		digest := openAIInvalidEncryptedDigestForTest(fmt.Sprintf("account-%d", accountID))
		svc.bindOpenAIInvalidEncryptedAccount(
			accountID,
			map[openAIInvalidEncryptedDigest]struct{}{digest: {}},
		)
	}
	require.Len(t, svc.openaiInvalidEncryptedAccounts, openAIInvalidEncryptedAccountMaxCount)
	require.NotEmpty(t, svc.openAIInvalidEncryptedAccountDigests(openAIInvalidEncryptedAccountMaxCount+1))

	full := make(map[openAIInvalidEncryptedDigest]struct{}, openAIInvalidEncryptedDigestMaxCount)
	for index := 0; index < openAIInvalidEncryptedDigestMaxCount; index++ {
		full[openAIInvalidEncryptedDigestForTest(fmt.Sprintf("old-%d", index))] = struct{}{}
	}
	svc.bindOpenAIInvalidEncryptedAccount(2001, full)
	newDigest := openAIInvalidEncryptedDigestForTest("newest")
	svc.bindOpenAIInvalidEncryptedAccount(
		2001,
		map[openAIInvalidEncryptedDigest]struct{}{newDigest: {}},
	)
	bounded := svc.openAIInvalidEncryptedAccountDigests(2001)
	require.Len(t, bounded, openAIInvalidEncryptedDigestMaxCount)
	_, containsNewest := bounded[newDigest]
	require.True(t, containsNewest, "容量已满时应优先保留新确认的坏摘要")
}

func openAIInvalidEncryptedDigestForTest(value string) openAIInvalidEncryptedDigest {
	digest, ok := openAIEncryptedReasoningItemDigest(map[string]any{
		"type":              "reasoning",
		"encrypted_content": value,
	})
	if !ok {
		panic("测试摘要生成失败")
	}
	return digest
}
