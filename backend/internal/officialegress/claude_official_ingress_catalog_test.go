package officialegress

import (
	"slices"
	"strings"
	"testing"
)

func TestClaudeOfficialIngressCatalogIdentityIsFrozen(t *testing.T) {
	catalog := defaultClaudeOfficialIngressCatalog()
	if catalog.digest != claudeOfficialIngressCatalogSHA256 ||
		catalog.targetReleaseDigest != DefaultClaudeReleaseCatalog().ProductionActive().Release().ReleaseDigest() ||
		catalog.evidencePolicy == "" || len(catalog.entries) != 3 {
		t.Fatal("Claude 官方入口 Catalog 顶层身份未冻结")
	}
	expected := []struct {
		clientID     string
		product      string
		version      string
		binaryDigest string
		userAgent    string
		packageVer   string
		toolDigests  []string
	}{
		{
			clientID: "claude-code-2.1.226-darwin-arm64", product: "claude-code",
			version:      "2.1.226",
			binaryDigest: "013a1cf17df5ff1dcc189d5d6fd3fdd5f097ddc3cd41aa9992e99805574febbe",
			userAgent:    "claude-cli/2.1.226 (external, sdk-cli)", packageVer: "0.94.0",
			toolDigests: []string{
				"4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
				"317c7be389c076d3cdf5aac4d80081d4403aed5067c0583a18dcb7dc3864f79b",
				"f5821e94ca6869e82b88a03ae9a5911bcded637a3aeb5d4188b02e3c5f04c4b6",
			},
		},
		{
			clientID: "claude-desktop-2.1.237-darwin-arm64", product: "claude-desktop",
			version:      "2.1.237",
			binaryDigest: "e969ac52969a8c35b258b9318ad65bc9544d848b2220c4af21e840ab1a137b23",
			userAgent:    "claude-cli/2.1.237 (external, claude-desktop-3p)", packageVer: "0.112.1",
			toolDigests: []string{
				"4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
				"d0f659710616e13301c92cda1b67c776471d48fd479b0b793fc11d982ca855dc",
				"e138f9428b36401f366b12ff783e3ab359c8ff06902d73b7a0118e073674ede1",
			},
		},
		{
			clientID: "claude-code-vscode-2.1.239-darwin-arm64", product: "claude-code-vscode",
			version:      "2.1.239",
			binaryDigest: "2b4f7aafdaa65bcc2335f56a4b276317837203f2c5587b1f2a17ca78ad14e36f",
			userAgent:    "claude-cli/2.1.239 (external, claude-vscode)", packageVer: "0.112.1",
			toolDigests: []string{
				"4f53cda18c2baa0c0354bb5f9a3ecbe5ed12ab4d8e11ba873c2f11161202b945",
				"d0f659710616e13301c92cda1b67c776471d48fd479b0b793fc11d982ca855dc",
				"e138f9428b36401f366b12ff783e3ab359c8ff06902d73b7a0118e073674ede1",
			},
		},
	}
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	for index, want := range expected {
		entry := catalog.entries[index]
		if entry.clientID != want.clientID || entry.product != want.product ||
			entry.version != want.version || entry.platform != "darwin/arm64" ||
			entry.binaryDigest != want.binaryDigest ||
			!slices.Contains(entry.userAgents, want.userAgent) ||
			entry.fixedHeaders["x-stainless-package-version"] != want.packageVer ||
			entry.systemAnchorDigest != "0d7062851dd7bd7e66d4be4f12ac4951e3d2f587ec408295333a49963bd3f6b7" {
			t.Fatalf("Claude 官方入口 Catalog 条目身份漂移：%s", want.clientID)
		}
		for _, digest := range want.toolDigests {
			if _, ok := entry.toolCatalogDigests[digest]; !ok {
				t.Fatalf("Claude 官方入口工具摘要缺失：%s/%s", want.clientID, digest)
			}
		}
		if entry.product == "claude-desktop" && !claudeCatalogHasExactUserAgent(
			entry, "claude-cli/2.1.237 (external, claude-desktop-3p, agent-sdk/0.3.237)",
		) {
			t.Fatal("Claude Desktop 标题请求的实测 agent-sdk UA 未登记")
		}
		headers := claudeOfficialCatalogHeaders(
			t, want.clientID, wire.Messages.DefaultBeta,
			"81818181-8181-4818-8818-818181818181",
		)
		matched, _, _, matchErr := catalog.matchHeaders(headers, profile, wire)
		if matchErr != nil || matched.clientID != want.clientID {
			t.Fatalf("Claude 官方入口 Header 不能命中 Catalog：%s/%v", want.clientID, matchErr)
		}
	}
}

func TestClaudeOfficialIngressCatalogDesktopNormalizationBoundary(t *testing.T) {
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	body := claudeDesktopTitleRequestBody(t, wire, nil)
	headers := claudeDesktopOfficialHeaders(
		t, wire.ImplementationPolicy.Scenarios.TUITitle.AnthropicBeta,
	)
	match, err := defaultClaudeOfficialIngressCatalog().matchMessages(
		body, headers, profile, wire,
	)
	if err != nil || match.entry.clientID != "claude-desktop-2.1.237-darwin-arm64" ||
		match.nativeTargetScenario {
		t.Fatalf("Claude Desktop 官方入口未命中转换目录：%+v/%v", match, err)
	}
	trusted := claudeTestTrustedFacts()
	targetAccountUUID := trusted.Account.AccountUUID
	resolved, state, official, err := resolveClaudeOfficialIngressBase(
		body, ClaudeIngressSnapshot{Captured: true, Headers: headers},
		trusted, profile, wire,
	)
	if err != nil || !official || state.CatalogMatch.entry.clientID != match.entry.clientID ||
		resolved.Account.AccountUUID != targetAccountUUID ||
		resolved.Session.SessionID != claudeDesktopTitleSessionID ||
		resolved.Session.Source != ClaudeSessionSourceOfficialConsistent {
		t.Fatalf("Claude 官方来源身份越过 Persona 调度边界：%+v/%v", resolved, err)
	}

	conflicting := headers.Clone()
	conflicting.Set("X-Claude-Code-Session-Id", "82828282-8282-4828-8828-828282828282")
	if _, _, _, err := resolveClaudeOfficialIngressBase(
		body, ClaudeIngressSnapshot{Captured: true, Headers: conflicting},
		trusted, profile, wire,
	); err == nil || !strings.Contains(err.Error(), "Session Header") {
		t.Fatalf("Claude metadata 与 Session Header 冲突未拒绝：%v", err)
	}

	unknownVersion := headers.Clone()
	unknownVersion.Set("User-Agent", "claude-cli/2.1.238 (external, claude-desktop-3p)")
	if _, err := defaultClaudeOfficialIngressCatalog().matchMessages(
		body, unknownVersion, profile, wire,
	); err == nil {
		t.Fatal("Claude 未登记 Desktop 版本没有 fail-close")
	}

	withRequestID := headers.Clone()
	withRequestID.Set("x-client-request-id", "83838383-8383-4838-8838-838383838383")
	if _, err := defaultClaudeOfficialIngressCatalog().matchMessages(
		body, withRequestID, profile, wire,
	); err == nil {
		t.Fatal("Claude Desktop 未登记的 x-client-request-id 没有 fail-close")
	}
}

func TestClaudeOfficialIngressCatalogRejectsUnregisteredDynamicTools(t *testing.T) {
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	body := claudeDesktopTitleRequestBody(t, wire, func(document map[string]any) {
		document["tools"] = []any{map[string]any{
			"name":         "mcp__unregistered__tool",
			"description":  "未登记动态 MCP 工具",
			"input_schema": map[string]any{"type": "object"},
		}}
	})
	headers := claudeDesktopOfficialHeaders(
		t, wire.ImplementationPolicy.Scenarios.TUITitle.AnthropicBeta,
	)
	if _, err := defaultClaudeOfficialIngressCatalog().matchMessages(
		body, headers, profile, wire,
	); err == nil || !strings.Contains(err.Error(), "工具目录摘要未登记") {
		t.Fatalf("Claude 未登记动态工具目录没有 fail-close：%v", err)
	}
}

func TestClaudeOfficialIngressCatalogHeadersDoNotRequireClientRequestID(t *testing.T) {
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	headers := claudeOfficialCatalogHeaders(
		t, "claude-code-vscode-2.1.239-darwin-arm64",
		wire.Messages.DefaultBeta, "84848484-8484-4848-8848-848484848484",
	)
	if headers.Get("x-client-request-id") != "" {
		t.Fatal("Claude 官方入口测试夹具意外携带 x-client-request-id")
	}
	entry, _, _, err := defaultClaudeOfficialIngressCatalog().matchHeaders(
		headers, profile, wire,
	)
	if err != nil || entry.product != "claude-code-vscode" {
		t.Fatalf("Claude VS Code 无 request-id 的官方 Header 被拒绝：%v", err)
	}
}

func TestClaudeOfficialIngressCatalogTargetReleaseKeepsNativeScenario(t *testing.T) {
	profile, err := loadClaudeFWGProfile()
	if err != nil {
		t.Fatal(err)
	}
	wire, err := loadClaudeFWGWire()
	if err != nil {
		t.Fatal(err)
	}
	headers := claudeOfficialCatalogHeaders(
		t, "claude-code-2.1.226-darwin-arm64",
		wire.Messages.DefaultBeta, "85858585-8585-4858-8858-858585858585",
	)
	headers.Set("x-client-request-id", "86868686-8686-4868-8868-868686868686")
	entry, entrypoint, native, err := defaultClaudeOfficialIngressCatalog().matchHeaders(
		headers, profile, wire,
	)
	if err != nil || entry.product != "claude-code" ||
		entrypoint != ClaudeEntrypointSDKCLI || !native {
		t.Fatalf("Claude 目标 Release 官方入口未保留原生场景：%s/%s/%t/%v",
			entry.product, entrypoint, native, err)
	}
}
