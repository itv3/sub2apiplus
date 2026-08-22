package officialegress

import (
	"net/http"
	"testing"
)

func TestClaudeUnregisteredIngressFailsClosed(t *testing.T) {
	trusted := claudeTestTrustedFacts()
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	for _, userAgent := range []string{
		"claude-cli/2.1.234 (external, claude-desktop-3p)",
		"claude-cli/2.1.234 (external, claude-desktop-3p, agent-sdk/0.3.234)",
		"claude-cli/2.1.235 (external, claude-desktop-3p, agent-sdk/0.3.235)",
		"claude-cli/2.1.235 (external, local-agent, agent-sdk/0.3.235)",
		"claude-cli/2.1.234 (external, cli)",
		"claude-cli/2.1.226 (external, sdk-cli)",
		"Kilo-Code/4.95.0",
	} {
		desktop := ClaudeIngressSnapshot{Captured: true, Headers: http.Header{
			"User-Agent": []string{userAgent},
		}}
		resolved, state, official, err := resolveClaudeOfficialIngressBase(
			nil, desktop, trusted, claudeFWGProfile{}, wire,
		)
		if err == nil || official || state.CatalogMatch.entry.clientID != "" ||
			resolved.Account.AccountScope != "" {
			t.Fatalf("Claude 未登记入口没有 fail-close：ua=%s official=%t err=%v", userAgent, official, err)
		}
		countResolved, err := resolveClaudeOfficialCountTokensIngress(
			desktop, trusted, claudeFWGProfile{}, wire,
		)
		if err == nil || countResolved.Account.AccountScope != "" {
			t.Fatalf("Claude 未登记 count_tokens 入口没有 fail-close：ua=%s err=%v", userAgent, err)
		}
	}

}
