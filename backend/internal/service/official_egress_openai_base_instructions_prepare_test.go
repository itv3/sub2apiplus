package service

import (
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

// 准备阶段（buildUpstreamRequest）把身份派生出的线程 ID 传给定型，基础指令消息的 id 才能由请求自己的线程算出。
// 这里走真实的准备入口：previous 槽位装合成画像、配置选择 previous 槽位，active 槽位对照旧形态。

func buildOfficialOpenAIHTTPRequestForSlot(t *testing.T, mode string, body []byte) (*OfficialEgressContext, []byte) {
	t.Helper()
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	cfg := &config.Config{}
	cfg.Gateway.OfficialClientProfiles.Mode = mode
	req, err := (&OpenAIGatewayService{cfg: cfg}).buildUpstreamRequest(
		c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "oauth-token",
		openAIUpstreamRequestPlan{
			IsStream: true, PromptCacheKey: testOfficialOpenAISessionID,
			IsCodexCLI: true, OfficialEgressBodyContract: contract,
		},
	)
	require.NoError(t, err)
	egressContext, ok := OfficialEgressContextFromContext(req.Context())
	require.True(t, ok)
	final, err := readOfficialEgressRequestBodyBytes(req)
	require.NoError(t, err)
	return egressContext, final
}

func TestBuildUpstreamRequestInstallsBaseInstructionsWithEgressThreadOnPreviousSlot(t *testing.T) {
	synthetic := withOfficialCodexSyntheticPreviousProfile(t, syntheticNewerProfileMutation(t))
	section := synthetic.Optional().BaseInstructions
	require.NotNil(t, section)
	body := newOfficialOpenAIHTTPTestBody(t, true, true, false)
	require.Equal(t, "入口显式指令", gjson.GetBytes(body, "instructions").String())

	egressContext, final := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModePrevious, body)
	require.Equal(t, officialClientProfileModePrevious, egressContext.ProfileMode())
	require.False(t, gjson.GetBytes(final, "instructions").Exists(), "画像声明了载体就不能再发顶层 instructions")
	items := gjson.GetBytes(final, "input").Array()
	require.Equal(t, []string{"additional_tools:developer", "BASE", "message:developer", "message:user"}, describeBaseInstructionsInput(items))
	threadID := mustOfficialEgressField(t, egressContext, OfficialEgressFieldThreadID).Value()
	rendered, err := renderOfficialCodexBaseInstructionsItem(section, threadID, "入口显式指令")
	require.NoError(t, err)
	require.Equal(t, string(rendered), items[1].Raw, "消息 id 必须由请求自己的线程 ID 算出")

	// 同一请求走 active 槽位：保持旧形态。指令文本仍在请求里（Lite 迁入 input、非 Lite 留在顶层，取决于模型能力），
	// 但 input 里没有官方形态的基础指令消息，旧迁入形态也没有 id。
	activeContext, activeFinal := buildOfficialOpenAIHTTPRequestForSlot(t, officialClientProfileModeActive, body)
	require.Equal(t, officialClientProfileModeActive, activeContext.ProfileMode())
	require.Contains(t, string(activeFinal), "入口显式指令")
	for _, item := range gjson.GetBytes(activeFinal, "input").Array() {
		require.False(t, item.Get("internal_chat_message_metadata_passthrough").Exists())
		require.False(t, item.Get("id").Exists())
	}
}
