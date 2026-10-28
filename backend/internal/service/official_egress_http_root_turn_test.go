package service

import (
	"bufio"
	"compress/gzip"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/profilecontract"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// HTTP 回合请求 client_metadata 的 root_turn_id（画像 ClientMetadata.DynamicKeys 声明 root_turn_id）。
// 依据官方 0.165 HTTP 压缩续聊会话（c3）的 7 个 /codex/responses 请求：每个请求的 client_metadata 都带 root_turn_id，
// 根线程上等于 turn_id（含压缩请求）；画像不声明时保持旧行为（不产生该事实）。

type replay0165HTTPRequest struct {
	Index          int               `json:"index"`
	RequestKind    string            `json:"request_kind"`
	ClientMetadata map[string]string `json:"client_metadata"`
}

// loadCodex0165HTTPRequests 读取官方 HTTP 实采会话（c3）的请求 client_metadata（正文已略去，只留身份与回合元数据）。
func loadCodex0165HTTPRequests(t *testing.T) []replay0165HTTPRequest {
	t.Helper()
	file, err := os.Open(filepath.Join("testdata", "codex_0165_informal", "c3_http_compact_continue.jsonl.gz"))
	require.NoError(t, err)
	defer func() { _ = file.Close() }()
	reader, err := gzip.NewReader(file)
	require.NoError(t, err)
	var requests []replay0165HTTPRequest
	scanner := bufio.NewScanner(reader)
	scanner.Buffer(make([]byte, 1<<20), 16<<20)
	for scanner.Scan() {
		var request replay0165HTTPRequest
		require.NoError(t, json.Unmarshal(scanner.Bytes(), &request))
		requests = append(requests, request)
	}
	require.NoError(t, scanner.Err())
	require.NotEmpty(t, requests)
	return requests
}

// replayHTTPIdentityFacts 以官方实采的 client_metadata 为入站，走真实的 HTTP 出站构造与语义 attempt 准备，返回身份事实。
// mutate 可在构造前改写 client_metadata（例如去掉入站的 root_turn_id，模拟第三方客户端）。
func replayHTTPIdentityFacts(
	t *testing.T,
	sample replay0165HTTPRequest,
	mutate func(clientMetadata string) string,
) officialegress.CodexIdentityFacts {
	t.Helper()
	cmRaw, err := json.Marshal(sample.ClientMetadata)
	require.NoError(t, err)
	clientMetadata := string(cmRaw)
	if mutate != nil {
		clientMetadata = mutate(clientMetadata)
	}
	sessionID := sample.ClientMetadata["session_id"]
	body := newOfficialOpenAIHTTPTestBody(t, true, false, false)
	body, err = sjson.SetRawBytes(body, "client_metadata", []byte(clientMetadata))
	require.NoError(t, err)
	body, err = sjson.SetBytes(body, "prompt_cache_key", sessionID)
	require.NoError(t, err)
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)

	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	c.Request.Header.Set("session-id", sessionID)
	c.Request.Header.Set("thread-id", sample.ClientMetadata["thread_id"])
	c.Request.Header.Set("x-client-request-id", sample.ClientMetadata["thread_id"])
	c.Request.Header.Set("x-codex-window-id", sample.ClientMetadata["x-codex-window-id"])
	c.Request.Header.Set("x-codex-turn-metadata", gjson.Get(clientMetadata, `x-codex-turn-metadata`).String())
	account := newOfficialOpenAIHTTPTestAccount(94)
	req, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(), c, account, body, "oauth-token",
		openAIUpstreamRequestPlan{
			IsStream: true, PromptCacheKey: sessionID, IsCodexCLI: true, OfficialEgressBodyContract: contract,
		},
	)
	require.NoError(t, err)
	final, err := readOfficialEgressRequestBodyBytes(req)
	require.NoError(t, err)
	semantic, err := prepareOfficialCodexSemanticAttempt(
		officialBodySharingCloneRequest(req), final, officialCodexEndpointResponsesHTTP, "root-turn-seed",
		projectOfficialCodexIdentityAccount(account),
	)
	require.NoError(t, err)
	return semantic.IdentityFacts
}

func TestOfficialCodexHTTPRootTurnIDFactFollowsProfile(t *testing.T) {
	mode := officialClientProfileModeActive
	samples := loadCodex0165HTTPRequests(t)
	require.Len(t, samples, 7)
	var sawCompaction bool

	// 画像不声明：不产生事实（旧画像的身份与摘要不变）。
	for _, sample := range samples {
		require.Empty(t, replayHTTPIdentityFacts(t, sample, nil).RootTurnID.Value,
			"请求 %d：画像没有声明 DynamicKeys 时不产生 HTTP 根回合事实", sample.Index)
	}

	withClientMetadataSectionProfile(t, mode, func(section *profilecontract.ClientMetadataSection) {
		section.DynamicKeys = []string{
			"root_turn_id", "ws_request_header_x_openai_internal_codex_responses_lite", "x-codex-ws-stream-request-start-ms",
		}
	})
	for _, sample := range samples {
		// 官方事实：每个请求的 client_metadata 都带 root_turn_id，根线程上等于 turn_id（含压缩请求）。
		official := sample.ClientMetadata["root_turn_id"]
		require.NotEmpty(t, official, "官方请求 %d 的 client_metadata 带 root_turn_id", sample.Index)
		require.Equal(t, sample.ClientMetadata["turn_id"], official, "官方根线程上 root_turn_id 等于 turn_id（请求 %d）", sample.Index)
		if sample.RequestKind == "compaction" {
			sawCompaction = true
		}
		// 网关出站：身份整体由网关派生，root_turn_id 事实取本请求出站的 turn_id，同样满足“根线程上等于 turn_id”。
		facts := replayHTTPIdentityFacts(t, sample, nil)
		require.NotEmpty(t, facts.TurnID.Value, "请求 %d（%s）", sample.Index, sample.RequestKind)
		require.Equal(t, facts.TurnID.Value, facts.RootTurnID.Value, "请求 %d（%s）：出站根回合 id 等于出站 turn_id", sample.Index, sample.RequestKind)
		require.Regexp(t, replayUUIDPattern, facts.RootTurnID.Value)
		// 入站是否带 root_turn_id 不影响出站值（入站 id 是客户端自己的回合，不透出）。
		withoutInbound := replayHTTPIdentityFacts(t, sample, func(clientMetadata string) string {
			out, err := sjson.Delete(clientMetadata, "root_turn_id")
			require.NoError(t, err)
			return out
		})
		require.Equal(t, withoutInbound.TurnID.Value, withoutInbound.RootTurnID.Value, "入站没带 root_turn_id 时结果相同")
		require.NotEqual(t, official, facts.RootTurnID.Value, "入站的 root_turn_id 不原样透出")
	}
	require.True(t, sawCompaction, "实采会话含 HTTP 压缩请求")
}

func TestOfficialCodexHTTPRootTurnIDOnlyForRootThreads(t *testing.T) {
	mode := officialClientProfileModeActive
	turn := "01a18395-e8ed-72f0-acf3-22d7c131055f"
	parent := "01a18395-610d-79d2-8eb4-de5172330cb9"
	build := func(subagent, parentThread, turnID string) officialegress.CodexIdentityFacts {
		built := officialegress.CodexIdentityFacts{}
		var err error
		if turnID != "" {
			built.TurnID, err = officialegress.NewCodexIdentityValue(turnID, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn)
			require.NoError(t, err)
		}
		if subagent != "" {
			built.Subagent, err = officialegress.NewCodexIdentityValue(subagent, officialegress.IdentitySourceTurn, officialegress.IdentityLifecycleTurn)
			require.NoError(t, err)
		}
		if parentThread != "" {
			built.ParentThreadID, err = officialegress.NewCodexIdentityValue(parentThread, officialegress.IdentitySourceInvocation, officialegress.IdentityLifecycleSession)
			require.NoError(t, err)
		}
		return built
	}
	// 画像不声明：任何线程都不产生。
	require.Empty(t, officialCodexHTTPRootTurnID(mode, build("", "", turn), false))

	withClientMetadataSectionProfile(t, mode, func(section *profilecontract.ClientMetadataSection) {
		section.DynamicKeys = []string{"root_turn_id"}
	})
	require.Equal(t, turn, officialCodexHTTPRootTurnID(mode, build("", "", turn), false), "根线程取 turn_id")
	require.Empty(t, officialCodexHTTPRootTurnID(mode, build("review", parent, turn), false), "子代理线程没有官方样本，不写")
	require.Empty(t, officialCodexHTTPRootTurnID(mode, build("guardian", parent, turn), false))
	require.Empty(t, officialCodexHTTPRootTurnID(mode, build("memory_consolidation", "", turn), true))
	require.Empty(t, officialCodexHTTPRootTurnID(mode, build("", "", "not-a-uuid"), false), "回合 id 不合法不写")
	require.Empty(t, officialCodexHTTPRootTurnID(mode, build("", "", ""), false))
	require.Empty(t, officialCodexHTTPRootTurnID("", build("", "", turn), false), "没有画像模式不写")
}
