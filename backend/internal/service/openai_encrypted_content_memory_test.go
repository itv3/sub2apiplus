package service

import (
	"crypto/sha256"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

func TestOpenAIEncryptedContentSHA256DoesNotCopyLargeString(t *testing.T) {
	for _, value := range []string{"", "中文与转义\\内容", strings.Repeat("gAAAAABrandomEncryptedContent", 64<<10)} {
		expected := sha256.Sum256([]byte(value))
		var digest [sha256.Size]byte
		allocations := testing.AllocsPerRun(5, func() { digest = openAIEncryptedContentSHA256(value) })
		require.Equal(t, expected, digest)
		require.Zero(t, allocations, "哈希不得为长密文复制临时字节切片")
	}
}
