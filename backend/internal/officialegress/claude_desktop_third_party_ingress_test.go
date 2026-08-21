package officialegress

import (
	"net/http"
	"testing"
)

func TestClaudeFWGDesktopThirdPartyDoesNotClaimTargetReleaseIdentity(t *testing.T) {
	trusted := claudeTestTrustedFacts()
	for _, userAgent := range []string{
		"claude-cli/2.1.234 (external, claude-desktop-3p)",
		"claude-cli/2.1.234 (external, claude-desktop-3p, agent-sdk/0.3.234)",
	} {
		desktop := ClaudeIngressSnapshot{Captured: true, Headers: http.Header{
			"User-Agent": []string{userAgent},
		}}
		resolved, state, official, err := resolveClaudeOfficialIngressBase(
			nil, desktop, trusted, claudeFWGProfile{}, claudeWireArtifact{},
		)
		if err != nil || official || state != (claudeOfficialIngressState{}) ||
			resolved.Session.SessionID != trusted.Session.SessionID {
			t.Fatalf("Claude Desktop 第三方入口错误继承目标 Release 身份：ua=%s official=%t err=%v", userAgent, official, err)
		}
		countResolved, err := resolveClaudeOfficialCountTokensIngress(
			desktop, trusted, claudeFWGProfile{},
		)
		if err != nil || countResolved.Session.SessionID != trusted.Session.SessionID {
			t.Fatalf("Claude Desktop count_tokens 第三方入口错误继承官方会话：ua=%s err=%v", userAgent, err)
		}
	}

	unsupportedOfficial := ClaudeIngressSnapshot{Captured: true, Headers: http.Header{
		"User-Agent": []string{"claude-cli/2.1.234 (external, cli)"},
	}}
	if _, _, _, err := resolveClaudeOfficialIngressBase(
		nil, unsupportedOfficial, trusted, claudeFWGProfile{}, claudeWireArtifact{},
	); err == nil {
		t.Fatal("未批准版本的官方身份声明没有 fail-close")
	}
}
