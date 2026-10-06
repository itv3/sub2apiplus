package service

import (
	"crypto/sha256"
	"fmt"
	"os"
	"strings"
	"testing"

	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
)

// 独立准备固定样本，供全新限额容器只读挂载。样本生成/压缩不得进入待验收容器，
// 否则其临时堆和写入文件的页缓存会污染从容器创建起累计的 memory.peak。
func TestOfficialEgressMemoryPrepareFixture(t *testing.T) {
	if os.Getenv("SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_FIXTURE") != "1" {
		t.Skip("仅在显式准备容器测量样本时运行")
	}
	fixture := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileFixtureEnv))
	require.NotEmpty(t, fixture, "必须指定固定正文文件")
	shape := strings.TrimSpace(os.Getenv(officialEgressMemoryProfileShapeEnv))
	if shape == "" {
		shape = officialEgressMemoryProfileNative
	}
	require.Contains(t, []string{officialEgressMemoryProfileNative, officialEgressMemoryProfileExplicit, officialEgressMemoryProfileIncompressible}, shape)
	size := int(officialEgressMemoryProfileEnvFloat(officialEgressMemoryProfileBodyMiBEnv, 16.8) * (1 << 20))
	body := loadOfficialEgressMemoryProfileBody(t, size, shape, fixture)
	if path := strings.TrimSpace(os.Getenv("SUB2API_OFFICIAL_EGRESS_MEMORY_WIRE_FIXTURE")); path != "" {
		file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0600)
		require.NoError(t, err, "压缩入站样本必须使用新的文件，不能覆盖既有对照数据")
		defer func() { require.NoError(t, file.Close()) }()
		encoder, err := zstd.NewWriter(file, zstd.WithEncoderConcurrency(1), zstd.WithLowerEncoderMem(true))
		require.NoError(t, err)
		encoder.ResetContentSize(file, int64(len(body)))
		_, err = encoder.Write(body)
		require.NoError(t, err)
		require.NoError(t, encoder.Close())
	}
	fmt.Printf("MEMPROFILE_FIXTURE bytes=%d sha256=%x shape=%s path=%s\n", len(body), sha256.Sum256(body), shape, fixture)
}
