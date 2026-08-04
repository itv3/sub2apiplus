package officialegress

import (
	"os"
	"strings"
	"testing"
)

// TestChangeset2CurrentSourceHasNoActiveSynonyms 扫描当前生产源码，而不是只核对
// 基线清单中的旧符号。新增 active 包装器或重新拆解 legacy capability 会立即阻塞门禁。
func TestChangeset2CurrentSourceHasNoActiveSynonyms(t *testing.T) {
	files := []string{
		"../handler/openai_gateway_handler.go",
		"../service/official_egress_codex_0145_profile.go",
		"../service/official_egress_codex_integration.go",
		"../service/official_egress_openai_http.go",
		"../service/official_egress_openai_json.go",
		"../service/official_egress_openai_ws.go",
		"../service/openai_alpha_search.go",
		"../service/openai_quota_service.go",
		"../service/openai_gateway_forward.go",
		"../service/openai_images_responses.go",
		"../service/openai_codex_models_service.go",
		"../repository/openai_oauth_service.go",
	}
	forbidden := []string{
		"officialClientProfileModeActive",
		"Resolve(officialegress.ReleaseModeActive)",
		"Resolve(ReleaseModeActive)",
	}
	for _, file := range files {
		raw, err := os.ReadFile(file)
		if err != nil {
			t.Fatalf("读取当前生产源码 %s：%v", file, err)
		}
		text := string(raw)
		for _, token := range forbidden {
			if strings.Contains(text, token) {
				t.Fatalf("当前生产源码 %s 重新引入被禁止事实源 %q", file, token)
			}
		}
	}
	requiredModeFlow := map[string][]string{
		"../handler/openai_gateway_handler.go": {
			"service.OfficialCodexProcessProfileMode()",
			"service.OfficialCodexRemoteCompactionV2Default(profileMode)",
		},
		"../service/official_egress_codex_0145_profile.go": {
			"func OfficialCodexRemoteCompactionV2Default(mode string) bool",
			"Codex compaction feature 缺少冻结 release mode",
		},
		"../service/official_egress_openai_http.go": {
			"OfficialCodexRemoteCompactionV2Default(profileMode)",
		},
	}
	for file, tokens := range requiredModeFlow {
		raw, err := os.ReadFile(file)
		if err != nil {
			t.Fatalf("读取 compaction mode 生产源码 %s：%v", file, err)
		}
		for _, token := range tokens {
			if !strings.Contains(string(raw), token) {
				t.Fatalf("当前生产源码 %s 丢失 compaction mode 闭环 %q", file, token)
			}
		}
	}
	legacyFiles := []string{
		"../service/official_egress_legacy_dispatch.go",
		"../repository/openai_oauth_service.go",
	}
	for _, file := range legacyFiles {
		raw, err := os.ReadFile(file)
		if err != nil {
			t.Fatalf("读取 legacy 生产源码 %s：%v", file, err)
		}
		for _, token := range []string{"TakeHTTPRequest(", ".Transport()", ".PoolDigest()"} {
			if strings.Contains(string(raw), token) {
				t.Fatalf("legacy 生产源码 %s 重新拆解 capability：%q", file, token)
			}
		}
	}
}
