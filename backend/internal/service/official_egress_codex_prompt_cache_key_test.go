package service

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// prompt_cache_key 来源与临时 fork（VC-4 第 2 项）：
//   - 旧画像不识别 fork：fork 入口与改动前一样按请求体 prompt_cache_key 锚定会话，
//     prompt cache 键等于 session ID；
//   - 画像声明 prompt_cache_key 来源时，根会话 fork 的本会话身份改由
//     client_metadata.session_id 锚定，prompt cache 键仍由源会话锚点派生，恰好等于
//     源会话派生出的 session ID；
//   - 入站身份校验只在画像声明该来源时放开根会话 fork 的 prompt cache 判定；
//   - 语义 attempt 把已登记的 prompt cache 键作为身份事实交给 compiler。

const (
	promptCacheTestSourceSession = "019f9577-d69f-7892-809e-8a3a4198c6a1"
	promptCacheTestForkSession   = "019f9577-d69f-7892-809e-8a3a4198c6a2"
)

func promptCacheTestContext(t *testing.T, body []byte) *gin.Context {
	t.Helper()
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", bytes.NewReader(body))
	c.Request.Header.Set("Content-Type", "application/json")
	c.Request.Header.Set("User-Agent", officialOpenAIHTTPUserAgent)
	return c
}

func promptCacheTestBody(t *testing.T, promptCacheKey string, clientSession string) []byte {
	t.Helper()
	payload := map[string]any{
		"model": "gpt-5.6-luna",
		"input": []any{map[string]any{
			"type": "message", "role": "user",
			"content": []any{map[string]any{"type": "input_text", "text": "fork 场景"}},
		}},
		"prompt_cache_key": promptCacheKey,
	}
	if clientSession != "" {
		payload["client_metadata"] = map[string]any{"session_id": clientSession, "thread_id": clientSession}
	}
	raw, err := json.Marshal(payload)
	require.NoError(t, err)
	return raw
}

func derivePromptCacheTestIdentity(t *testing.T, promptCacheKey string, clientSession string) officialOpenAIHTTPIdentity {
	t.Helper()
	body := promptCacheTestBody(t, promptCacheKey, clientSession)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	identity, err := deriveOfficialOpenAIHTTPIdentity(
		promptCacheTestContext(t, body), newOfficialOpenAIHTTPTestAccount(157), body, contract,
		officialClientProfileModeActive,
	)
	require.NoError(t, err)
	return identity
}

func TestDeriveOfficialOpenAIHTTPIdentityIgnoresForkUnderLegacyProfile(t *testing.T) {
	source := derivePromptCacheTestIdentity(t, promptCacheTestSourceSession, promptCacheTestSourceSession)
	fork := derivePromptCacheTestIdentity(t, promptCacheTestSourceSession, promptCacheTestForkSession)
	require.Equal(t, fork.sessionID, fork.promptCacheKey, "旧画像的 prompt cache 键恒等于 session ID")
	require.Equal(t, source.sessionID, fork.sessionID, "旧画像按请求体 prompt_cache_key 锚定会话，与改动前一致")
	require.Equal(t, source.threadID, fork.threadID)
}

func TestDeriveOfficialOpenAIHTTPIdentityForkUsesSourceCacheKeyWhenProfileDeclares(t *testing.T) {
	withOfficialCodexSyntheticProfile(t, syntheticPromptCacheKeySessionMutation(t))

	source := derivePromptCacheTestIdentity(t, promptCacheTestSourceSession, promptCacheTestSourceSession)
	require.Equal(t, source.sessionID, source.promptCacheKey, "非 fork 根会话不受影响")

	ownAnchored := derivePromptCacheTestIdentity(t, promptCacheTestForkSession, promptCacheTestForkSession)
	fork := derivePromptCacheTestIdentity(t, promptCacheTestSourceSession, promptCacheTestForkSession)
	require.Equal(t, ownAnchored.sessionID, fork.sessionID, "fork 的本会话身份由 client_metadata.session_id 锚定")
	require.Equal(t, fork.sessionID, fork.threadID, "fork 仍是根会话")
	require.Equal(t, source.sessionID, fork.promptCacheKey, "fork 的 prompt cache 键等于源会话派生出的 session ID")
	require.NotEqual(t, fork.sessionID, fork.promptCacheKey)

	// 非 UUID 的任意字符串不当成 fork，保持按请求体 prompt_cache_key 锚定。
	notFork := derivePromptCacheTestIdentity(t, "third-party-cache-key", promptCacheTestForkSession)
	require.Equal(t, notFork.sessionID, notFork.promptCacheKey)
}

func TestValidateOfficialOpenAIIngressIdentityKindForkRelaxation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	root := officialOpenAIIngressIdentityValues{
		sessionID: promptCacheTestForkSession, threadID: promptCacheTestForkSession,
		promptCacheKey: promptCacheTestSourceSession,
	}
	turnMetadata := map[string]any{"thread_source": "user"}

	_, err := validateOfficialOpenAIIngressIdentityKind("test", c, map[string]any{}, turnMetadata, root, false)
	require.ErrorContains(t, err, "prompt_cache_key conflicts", "未声明来源时严格按旧规则拒绝")

	allowed := root
	allowed.forkPromptCacheKeyAllowed = true
	kind, err := validateOfficialOpenAIIngressIdentityKind("test", c, map[string]any{}, turnMetadata, allowed, false)
	require.NoError(t, err)
	require.Equal(t, officialOpenAIIdentityKindRoot, kind)

	notUUID := allowed
	notUUID.promptCacheKey = "not-a-session"
	_, err = validateOfficialOpenAIIngressIdentityKind("test", c, map[string]any{}, turnMetadata, notUUID, false)
	require.ErrorContains(t, err, "prompt_cache_key conflicts")

	// 子代理不受 fork 缓存键影响。
	subagent, _ := gin.CreateTestContext(httptest.NewRecorder())
	subagent.Request = httptest.NewRequest(http.MethodPost, "/v1/responses", nil)
	subagent.Request.Header.Set("x-openai-subagent", "review")
	child := officialOpenAIIngressIdentityValues{
		sessionID: promptCacheTestSourceSession, threadID: promptCacheTestForkSession,
		promptCacheKey: promptCacheTestForkSession, forkPromptCacheKeyAllowed: true,
	}
	_, err = validateOfficialOpenAIIngressIdentityKind(
		"test", subagent, map[string]any{"x-openai-subagent": "review"},
		map[string]any{"thread_source": "subagent", "subagent_kind": "review"}, child, false,
	)
	require.ErrorContains(t, err, "prompt_cache_key conflicts")
}

func TestOfficialOpenAIIngressSessionHeaderMatchesFork(t *testing.T) {
	require.True(t, officialOpenAIIngressSessionHeaderMatches("a", "a", "b", false))
	require.False(t, officialOpenAIIngressSessionHeaderMatches(
		promptCacheTestSourceSession, promptCacheTestForkSession, promptCacheTestSourceSession, false,
	))
	require.True(t, officialOpenAIIngressSessionHeaderMatches(
		promptCacheTestSourceSession, promptCacheTestForkSession, promptCacheTestSourceSession, true,
	))
	require.False(t, officialOpenAIIngressSessionHeaderMatches(
		"guardian:"+promptCacheTestSourceSession, promptCacheTestForkSession, "guardian:"+promptCacheTestSourceSession, true,
	), "只接受 UUID 形态的源会话键")
}

func TestOfficialCodexSemanticAttemptCarriesRegisteredPromptCacheKey(t *testing.T) {
	for _, tc := range []struct {
		name       string
		endpointID string
		want       string
	}{
		{name: "Responses 端点携带", endpointID: officialCodexEndpointResponsesHTTP, want: promptCacheTestSourceSession},
		{name: "非 Responses 端点不携带", endpointID: officialCodexEndpointAlphaSearch},
	} {
		t.Run(tc.name, func(t *testing.T) {
			state := defaultOfficialCodexRuntimeState()
			state.ProfileMode = officialClientProfileModeActive
			egressContext := NewOfficialEgressContext(OfficialEgressContextInput{
				AccountID: 157, TargetPlatform: PlatformOpenAI,
				ProfileVersion: officialCodexVersion0145, ProfileMode: officialClientProfileModeActive,
				Transport: OfficialEgressTransportHTTP, UpstreamHost: "chatgpt.com",
				CodexRuntimeState: state,
			})
			require.NoError(t, egressContext.RegisterField(
				OfficialEgressFieldPromptCacheKey, promptCacheTestSourceSession,
				OfficialEgressFieldSourceDerived, OfficialEgressFieldLifecycleSession,
			))
			body := `{"model":"gpt-5.6-luna","input":[]}`
			req := httptest.NewRequest(
				http.MethodPost, "https://chatgpt.com/backend-api/codex/responses", strings.NewReader(body),
			)
			req = req.WithContext(WithOfficialEgressContext(req.Context(), egressContext))
			attempt, err := prepareOfficialCodexSemanticAttempt(
				req, []byte(body), tc.endpointID, "prompt-cache-key-fact-test",
				projectOfficialCodexIdentityAccount(officialEgressTestAccount(157, PlatformOpenAI)),
			)
			require.NoError(t, err)
			require.Equal(t, tc.want, attempt.IdentityFacts.PromptCacheKey.Value)
		})
	}
}
