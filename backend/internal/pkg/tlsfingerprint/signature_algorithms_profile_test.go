package tlsfingerprint

import (
	"slices"
	"testing"

	utls "github.com/refraction-networking/utls"
)

// 画像驱动的签名算法（VC-4 第 7 项回归断言）：目标版本 WS 传输在原 10 项之后追加
// ML-DSA-44/65/87（0x0904～0x0906）。ClientHello 的 signature_algorithms（扩展 13）必须
// 逐项、按序携带画像列表，扩展 50（signature_algorithms_cert）出现时复用同一列表；
// utls 自定义 Hello 可发送任意 SignatureScheme，不依赖本地签名能力。
func TestBuildClientHelloSpecCarriesProfileSignatureAlgorithmsInOrder(t *testing.T) {
	profileAlgorithms := []uint16{1283, 1027, 1539, 2055, 2054, 2053, 2052, 1537, 1281, 1025, 2308, 2309, 2310}
	for _, extensions := range [][]uint16{
		{0, 5, 10, 11, 13, 23, 35, 43, 45, 51},
		{0, 5, 10, 11, 13, 23, 35, 43, 45, 50, 51},
	} {
		spec := buildClientHelloSpecFromProfile(&Profile{
			SignatureAlgorithms: profileAlgorithms,
			Extensions:          extensions,
		})
		want := make([]utls.SignatureScheme, len(profileAlgorithms))
		for index, value := range profileAlgorithms {
			want[index] = utls.SignatureScheme(value)
		}
		sawSignatureAlgorithms := false
		sawCertificateAlgorithms := false
		for _, extension := range spec.Extensions {
			switch typed := extension.(type) {
			case *utls.SignatureAlgorithmsExtension:
				sawSignatureAlgorithms = true
				if !slices.Equal(typed.SupportedSignatureAlgorithms, want) {
					t.Fatalf("signature_algorithms 未按画像逐项携带：%v", typed.SupportedSignatureAlgorithms)
				}
			case *utls.SignatureAlgorithmsCertExtension:
				sawCertificateAlgorithms = true
				if !slices.Equal(typed.SupportedSignatureAlgorithms, want) {
					t.Fatalf("signature_algorithms_cert 必须复用同一列表：%v", typed.SupportedSignatureAlgorithms)
				}
			}
		}
		if !sawSignatureAlgorithms {
			t.Fatal("ClientHello 缺少 signature_algorithms 扩展")
		}
		if sawCertificateAlgorithms != slices.Contains(extensions, 50) {
			t.Fatalf("扩展 50 是否出现必须由画像扩展序决定：%v", extensions)
		}
	}
}
