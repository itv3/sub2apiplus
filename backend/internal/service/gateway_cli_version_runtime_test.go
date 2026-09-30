package service

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/stretchr/testify/require"
)

// withCLIVersionResolverForTest 注入解析器并在用例结束时还原（置 nil），避免用例间污染。
func withCLIVersionResolverForTest(t *testing.T, resolver func() string) {
	t.Helper()
	claude.SetCLIVersionResolver(resolver)
	t.Cleanup(func() { claude.SetCLIVersionResolver(nil) })
}

// e. 注入更高版本后，floorClaudeCLIUserAgentVersion 把存量低版本指纹抬升到新版本；
//
//	等于或高于新版本的指纹保持不动（只升不降）。
func TestFloorClaudeCLIUserAgentVersion_UsesRuntimeVersion(t *testing.T) {
	const upgraded = "9.9.9"
	withCLIVersionResolverForTest(t, func() string { return upgraded })

	floored, changed := floorClaudeCLIUserAgentVersion("claude-cli/2.1.100 (external, cli)")
	require.True(t, changed)
	require.Equal(t, "claude-cli/"+upgraded+" (external, cli)", floored)

	// 等于运行期版本：不动。
	same, changed := floorClaudeCLIUserAgentVersion("claude-cli/" + upgraded + " (external, cli)")
	require.False(t, changed)
	require.Equal(t, "claude-cli/"+upgraded+" (external, cli)", same)

	// 高于运行期版本：不动。
	greater, changed := floorClaudeCLIUserAgentVersion("claude-cli/99.0.0 (external, cli)")
	require.False(t, changed)
	require.Equal(t, "claude-cli/99.0.0 (external, cli)", greater)

	// resolver 返回非法值时回退内置基线，floor 语义仍然成立。
	withCLIVersionResolverForTest(t, func() string { return "abc" })
	baselineFloored, changed := floorClaudeCLIUserAgentVersion("claude-cli/1.0.0 (external, cli)")
	require.True(t, changed)
	require.Equal(t, "claude-cli/"+claude.CLIVersion()+" (external, cli)", baselineFloored)
}

// 注入更高版本后，isAcceptableFingerprintUserAgent 以运行期版本为基准，允许客户端
// 上报的新版本。
func TestIsAcceptableFingerprintUserAgent_UsesRuntimeVersion(t *testing.T) {
	const upgraded = "9.9.9"
	withCLIVersionResolverForTest(t, func() string { return upgraded })
	require.True(t, isAcceptableFingerprintUserAgent("claude-cli/"+upgraded+" (external, cli)"))
	// 允许的最大主版本超前量是 +2（9.9.9 → 11.x）。
	require.True(t, isAcceptableFingerprintUserAgent("claude-cli/11.0.0 (external, cli)"))
	// 超前 +3 会被拒（与静态基线下的既有语义一致，只是基准换成运行期版本）。
	require.False(t, isAcceptableFingerprintUserAgent("claude-cli/12.0.0 (external, cli)"))
}

// defaultFingerprint 现取运行期版本，不再在 init 固化。
func TestDefaultFingerprintUsesRuntimeVersion(t *testing.T) {
	const upgraded = "9.9.9"
	withCLIVersionResolverForTest(t, func() string { return upgraded })
	require.Equal(t, "claude-cli/"+upgraded+" (external, cli)", defaultFingerprint().UserAgent)
}
