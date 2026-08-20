package web

import "testing"

func TestClaudeFWGBareCountTokensAliasBypassesEmbeddedFrontend(t *testing.T) {
	for _, path := range []string{
		"/messages/count_tokens",
		"  /messages/count_tokens  ",
	} {
		if !shouldBypassEmbeddedFrontend(path) {
			t.Fatalf("Claude FW-G count_tokens 裸路径别名被前端中间件吞掉：%q", path)
		}
	}
	for _, path := range []string{
		"/messages",
		"/messages/count_tokens/unknown",
	} {
		if shouldBypassEmbeddedFrontend(path) {
			t.Fatalf("未批准的 Claude 裸路径不应绕过前端中间件：%q", path)
		}
	}
}
