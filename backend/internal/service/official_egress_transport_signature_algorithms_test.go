package service

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/stretchr/testify/require"
)

// TransportSpec 到底层 TLS 资源的机械转换必须逐项、按序保留画像签名算法（含目标版本
// WS 传输追加的 ML-DSA-44/65/87），不得截断、去重或重排（VC-4 第 7 项回归断言）。
func TestTLSFingerprintProfileFromTransportSpecKeepsSignatureAlgorithms(t *testing.T) {
	algorithms := []uint16{1283, 1027, 1539, 2055, 2054, 2053, 2052, 1537, 1281, 1025, 2308, 2309, 2310}
	profile, err := tlsFingerprintProfileFromTransportSpec(officialegress.TransportSpec{
		ConnectionPoolDigest: "sigalgs-test",
		TLS: officialegress.TLSProfileSpec{
			Stack: "rustls", CipherSuites: []uint16{4865}, SignatureAlgorithms: algorithms,
			Extensions: []uint16{0, 5, 10, 11, 13, 23, 35, 43, 45, 51},
		},
	})
	require.NoError(t, err)
	require.Equal(t, algorithms, profile.SignatureAlgorithms)
	algorithms[len(algorithms)-1] = 0
	require.Equal(t, uint16(2310), profile.SignatureAlgorithms[len(profile.SignatureAlgorithms)-1], "转换结果不得与输入共享底层数组")
}
