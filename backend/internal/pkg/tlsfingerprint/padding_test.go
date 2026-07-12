package tlsfingerprint

import (
	"crypto/md5"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"net"
	"strconv"
	"strings"
	"testing"

	utls "github.com/refraction-networking/utls"
)

func claudeCLI21207TestProfile() *Profile {
	return &Profile{
		Name:              "claude_cli_2_1_207_test",
		ALPNProtocols:     []string{"http/1.1"},
		SupportedVersions: []uint16{utls.VersionTLS13, utls.VersionTLS12},
		Extensions:        []uint16{0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43, 21},
	}
}

func TestBuildClientHelloSpecUsesRealPaddingExtension(t *testing.T) {
	spec := buildClientHelloSpecFromProfile(claudeCLI21207TestProfile())
	if len(spec.Extensions) != 14 {
		t.Fatalf("扩展数量 = %d，期望 14", len(spec.Extensions))
	}

	padding, ok := spec.Extensions[len(spec.Extensions)-1].(*utls.UtlsPaddingExtension)
	if !ok {
		t.Fatalf("extension 21 类型 = %T，期望 *utls.UtlsPaddingExtension", spec.Extensions[len(spec.Extensions)-1])
	}
	if padding.GetPaddingLen == nil {
		t.Fatal("padding 扩展必须配置动态长度计算函数")
	}
	foundALPN := false
	for _, extension := range spec.Extensions {
		if generic, ok := extension.(*utls.GenericExtension); ok && generic.Id == 21 {
			t.Fatal("extension 21 不能使用空 GenericExtension")
		}
		if _, ok := extension.(*utls.GREASEEncryptedClientHelloExtension); ok {
			t.Fatal("Claude Node.js 26 profile 不应包含 extension 65037")
		}
		if alpn, ok := extension.(*utls.ALPNExtension); ok {
			foundALPN = true
			if len(alpn.AlpnProtocols) != 1 || alpn.AlpnProtocols[0] != "http/1.1" {
				t.Fatalf("ALPN = %v，期望仅包含 http/1.1", alpn.AlpnProtocols)
			}
		}
	}
	if !foundALPN {
		t.Fatal("ClientHello 缺少 ALPN 扩展")
	}
}

func TestClaudeCLI21207ClientHelloMarshalsToCapturedFingerprint(t *testing.T) {
	spec := buildClientHelloSpecFromProfile(claudeCLI21207TestProfile())
	clientConn, serverConn := net.Pipe()
	defer func() { _ = clientConn.Close() }()
	defer func() { _ = serverConn.Close() }()

	uconn := utls.UClient(clientConn, &utls.Config{ServerName: "anyrouter.top"}, utls.HelloCustom)
	if err := uconn.ApplyPreset(spec); err != nil {
		t.Fatalf("应用 ClientHello profile 失败: %v", err)
	}
	if err := uconn.BuildHandshakeState(); err != nil {
		t.Fatalf("生成 ClientHello 失败: %v", err)
	}

	raw := uconn.HandshakeState.Hello.Raw
	parsed, err := parseClientHelloForJA3(raw)
	if err != nil {
		t.Fatalf("解析 ClientHello 失败: %v", err)
	}

	wantExtensions := []uint16{0, 23, 65281, 10, 11, 35, 16, 5, 13, 18, 51, 45, 43, 21}
	if !equalUint16Slices(parsed.extensions, wantExtensions) {
		t.Fatalf("扩展顺序 = %v，期望 %v", parsed.extensions, wantExtensions)
	}
	if parsed.paddingLength != 235 {
		t.Fatalf("padding 长度 = %d，期望 235", parsed.paddingLength)
	}
	// uTLS Raw 不含 5 字节 TLS record header；线上抓包的完整 record 因此为 517 字节。
	if len(raw) != 512 || len(raw)+5 != 517 {
		t.Fatalf("ClientHello 长度 = %d（含 record header 为 %d），期望 512/517", len(raw), len(raw)+5)
	}
	if parsed.ja3Hash != "d871d02cecbde59abbf8f4806134addf" {
		t.Fatalf("JA3 = %s，期望 d871d02cecbde59abbf8f4806134addf；原文=%s", parsed.ja3Hash, parsed.ja3Raw)
	}
}

type parsedClientHelloJA3 struct {
	extensions    []uint16
	paddingLength int
	ja3Raw        string
	ja3Hash       string
}

func parseClientHelloForJA3(raw []byte) (*parsedClientHelloJA3, error) {
	if len(raw) < 43 || raw[0] != 1 {
		return nil, fmt.Errorf("不是完整的 ClientHello handshake")
	}
	pos := 4
	legacyVersion := binary.BigEndian.Uint16(raw[pos : pos+2])
	pos += 2 + 32
	if pos >= len(raw) {
		return nil, fmt.Errorf("缺少 session ID 长度")
	}
	sessionIDLength := int(raw[pos])
	pos++
	if pos+sessionIDLength+2 > len(raw) {
		return nil, fmt.Errorf("session ID 越界")
	}
	pos += sessionIDLength

	cipherBytesLength := int(binary.BigEndian.Uint16(raw[pos : pos+2]))
	pos += 2
	if cipherBytesLength%2 != 0 || pos+cipherBytesLength+1 > len(raw) {
		return nil, fmt.Errorf("cipher suites 越界")
	}
	cipherSuites := make([]uint16, 0, cipherBytesLength/2)
	for end := pos + cipherBytesLength; pos < end; pos += 2 {
		value := binary.BigEndian.Uint16(raw[pos : pos+2])
		if !isGREASEValue(value) {
			cipherSuites = append(cipherSuites, value)
		}
	}

	compressionMethodsLength := int(raw[pos])
	pos++
	if pos+compressionMethodsLength+2 > len(raw) {
		return nil, fmt.Errorf("compression methods 越界")
	}
	pos += compressionMethodsLength
	extensionsLength := int(binary.BigEndian.Uint16(raw[pos : pos+2]))
	pos += 2
	if pos+extensionsLength != len(raw) {
		return nil, fmt.Errorf("extensions 长度不一致: 声明=%d 实际=%d", extensionsLength, len(raw)-pos)
	}

	result := &parsedClientHelloJA3{}
	var curves []uint16
	var pointFormats []uint16
	for end := pos + extensionsLength; pos < end; {
		if pos+4 > end {
			return nil, fmt.Errorf("extension header 越界")
		}
		extensionID := binary.BigEndian.Uint16(raw[pos : pos+2])
		extensionLength := int(binary.BigEndian.Uint16(raw[pos+2 : pos+4]))
		pos += 4
		if pos+extensionLength > end {
			return nil, fmt.Errorf("extension %d 数据越界", extensionID)
		}
		extensionData := raw[pos : pos+extensionLength]
		pos += extensionLength
		if !isGREASEValue(extensionID) {
			result.extensions = append(result.extensions, extensionID)
		}
		switch extensionID {
		case 10:
			curves = parseUint16Vector(extensionData)
		case 11:
			if len(extensionData) > 0 && int(extensionData[0]) == len(extensionData)-1 {
				for _, value := range extensionData[1:] {
					pointFormats = append(pointFormats, uint16(value))
				}
			}
		case 21:
			result.paddingLength = extensionLength
		}
	}

	result.ja3Raw = strings.Join([]string{
		strconv.Itoa(int(legacyVersion)),
		joinUint16s(cipherSuites),
		joinUint16s(result.extensions),
		joinUint16s(curves),
		joinUint16s(pointFormats),
	}, ",")
	hash := md5.Sum([]byte(result.ja3Raw))
	result.ja3Hash = hex.EncodeToString(hash[:])
	return result, nil
}

func parseUint16Vector(data []byte) []uint16 {
	if len(data) < 2 {
		return nil
	}
	length := int(binary.BigEndian.Uint16(data[:2]))
	if length%2 != 0 || length != len(data)-2 {
		return nil
	}
	values := make([]uint16, 0, length/2)
	for pos := 2; pos < len(data); pos += 2 {
		value := binary.BigEndian.Uint16(data[pos : pos+2])
		if !isGREASEValue(value) {
			values = append(values, value)
		}
	}
	return values
}

func joinUint16s(values []uint16) string {
	parts := make([]string, len(values))
	for index, value := range values {
		parts[index] = strconv.Itoa(int(value))
	}
	return strings.Join(parts, "-")
}

func equalUint16Slices(left, right []uint16) bool {
	if len(left) != len(right) {
		return false
	}
	for index := range left {
		if left[index] != right[index] {
			return false
		}
	}
	return true
}
