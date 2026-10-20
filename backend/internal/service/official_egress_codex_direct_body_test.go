package service

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"slices"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	openaipkg "github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 直接交给 Executor 的 Responses 请求体（用量探针、管理员「测试连接」的 OAuth 分支）。
//
// 这两处不经过 Forward 的定型层，正文由 Compiler 按画像闭集定型并对闭集外字段失败关闭。较新画像的 responses_http
// 闭集不再声明顶层 instructions，所以：
//   - 闭集仍声明 instructions 的槽位（旧画像）：整理是恒等操作，出站字节与改动前逐字节相同；
//   - 闭集不声明的槽位（较新画像）：去掉 instructions 后正文能通过 Compiler 并发往终端，探针取到限额快照。
//
// 用例对两个正式槽位各验证一遍，结论由槽位画像的结构事实决定，不写死 active／previous：晋升前后它们各是其中一种。

const directBodyInstructionsPlaceholder = "<DEFAULT_INSTRUCTIONS>"

// 对照组：改动前的旧代码上实测的黄金摘要。
//
// 正文摘要对象是交给 Executor 之前的 JSON（json.Marshal 按键名排序，字节确定），其中 instructions 的取值换成
// 占位符，使摘要只锁定结构、键集与其余取值，不随默认指令文本的版本演进而漂移。
// 线上摘要对象是旧画像槽位上经正式 Compiler／Executor 与真实 TLS 终端的 wire 正文，去掉每次随机的
// client_metadata 与 prompt_cache_key 后同样把 instructions 换成占位符。
const (
	directBodyGoldenProbe             = "f331b70719b58beac98313550d337a43a5c3bf5523f24c6d8012cfe4f0b4d755"
	directBodyGoldenAdminDefault      = "0b9621c633be1bc98aec44e7329b022bb128641c6452a2bd5a169f9e317dacaa"
	directBodyGoldenAdminPrompt       = "cd1de364c0d400f9223b34f58761071a8bbf384385542ab70b061a1709b9fa4e"
	directBodyGoldenAdminMaxTokens    = "1f851114e032503b78efa3fc66f334b1d2fae75d02f6b666b1921f377e9e6fe1"
	directBodyGoldenAdminAPIKey       = "2f443a4f5496c9e6b84ab0de0442c1be279c078e64ea70d6b28916316b3df1cd"
	directBodyGoldenProbeWire         = "cbc25c482143d4395930492216728dcbbc236cd6ac6a09826a4adff2fe664d86"
	directBodyGoldenAdminPromptOnWire = "c1a3db17d33281efde884ff909844e57f055e1f012d08bba577f7ef3452a3700"
)

// directBodyDigest 把 instructions 的取值换成占位符后取 SHA-256。
func directBodyDigest(t *testing.T, body []byte) string {
	t.Helper()
	normalized := body
	if gjson.GetBytes(body, "instructions").Exists() {
		var err error
		normalized, err = sjson.SetBytes(body, "instructions", directBodyInstructionsPlaceholder)
		require.NoError(t, err)
	}
	sum := sha256.Sum256(normalized)
	return hex.EncodeToString(sum[:])
}

// directBodyWireDigest 先去掉线上正文里每次随机的成员，再按 directBodyDigest 取摘要。
func directBodyWireDigest(t *testing.T, wireBody []byte) string {
	t.Helper()
	body := wireBody
	for _, member := range []string{"client_metadata", "prompt_cache_key"} {
		var err error
		body, err = sjson.DeleteBytes(body, member)
		require.NoError(t, err)
	}
	return directBodyDigest(t, body)
}

// directBodyDeclaresInstructions 返回 mode 槽位正式画像的 responses_http 闭集是否声明顶层 instructions。
func directBodyDeclaresInstructions(t *testing.T, mode string) bool {
	t.Helper()
	endpoint := codexGateMustEndpoint(t, codexGateExecutableProfile(t, mode), officialCodexEndpointResponsesHTTP)
	for _, field := range endpoint.Body.Fields {
		if field.Name == "instructions" {
			return true
		}
	}
	return false
}

// directBodyBothSlots 返回两个正式槽位及各自的结构事实，并要求候选态与晋升后态两种情形至少各有一个槽位：
// 一个声明 instructions（旧画像），一个不声明（较新画像）。
func directBodyBothSlots(t *testing.T) map[string]bool {
	t.Helper()
	slots := map[string]bool{}
	for _, mode := range []string{officialClientProfileModeActive, officialClientProfileModePrevious} {
		slots[mode] = directBodyDeclaresInstructions(t, mode)
	}
	declared, omitted := 0, 0
	for _, declares := range slots {
		if declares {
			declared++
		} else {
			omitted++
		}
	}
	require.Equal(t, 1, declared, "两个槽位里应恰有一个旧画像（闭集声明 instructions）：%v", slots)
	require.Equal(t, 1, omitted, "两个槽位里应恰有一个较新画像（闭集不声明 instructions）：%v", slots)
	return slots
}

// 对照组：交给 Executor 之前的正文字节在旧代码上实测的摘要，整理逻辑不得改变它们。
func TestDirectResponsesPayloadsKeepTheirLegacyGoldens(t *testing.T) {
	marshal := func(payload map[string]any) []byte {
		raw, err := json.Marshal(payload)
		require.NoError(t, err)
		return raw
	}
	require.Equal(t, directBodyGoldenProbe,
		directBodyDigest(t, marshal(buildOpenAICodexProbePayload(openaipkg.CodexUsageProbeModel))), "用量探针正文")
	require.Equal(t, directBodyGoldenAdminDefault,
		directBodyDigest(t, marshal(createOpenAITestPayload(openaipkg.DefaultTestModel, "", true, 0))), "管理员测试连接 OAuth 默认")
	require.Equal(t, directBodyGoldenAdminPrompt,
		directBodyDigest(t, marshal(createOpenAITestPayload("gpt-5.5", "你好 ping", true, 0))), "管理员测试连接 OAuth 自定义提示词")
	require.Equal(t, directBodyGoldenAdminMaxTokens,
		directBodyDigest(t, marshal(createOpenAITestPayload("gpt-5.5", "", true, 64))), "管理员测试连接 OAuth 带输出上限（OAuth 忽略）")
	require.Equal(t, directBodyGoldenAdminAPIKey,
		directBodyDigest(t, marshal(createOpenAITestPayload("gpt-5.5", "你好 ping", false, 64))), "管理员测试连接 API Key（不属于 Codex 画像）")
}

// 整理函数只看 Executor runtime 实际冻结的发布槽位：闭集声明 instructions 时恒等，不声明时只删这一个字段。
func TestShapeOfficialCodexDirectResponsesBodyFollowsTheExecutorReleaseSlot(t *testing.T) {
	probe, err := json.Marshal(buildOpenAICodexProbePayload(openaipkg.CodexUsageProbeModel))
	require.NoError(t, err)
	for mode, declares := range directBodyBothSlots(t) {
		runtimeState := newOfficialEgressTestRuntimeForMode(t, &httpUpstreamRecorder{}, mode)
		before := append([]byte(nil), probe...)

		after, changed := shapeOfficialCodexDirectResponsesBody(probe, runtimeState)
		require.Equal(t, string(before), string(probe), "%s：入参字节不得被原地改写", mode)

		if declares {
			require.False(t, changed, "%s：闭集声明 instructions，不得改动", mode)
			require.Equal(t, string(before), string(after), "%s：旧画像槽位正文必须逐字节不变", mode)
			require.Equal(t, directBodyGoldenProbe, directBodyDigest(t, after), mode)
			continue
		}
		require.True(t, changed, "%s：闭集不声明 instructions，必须去掉", mode)
		require.False(t, gjson.GetBytes(after, "instructions").Exists(), mode)
		withoutInstructions, err := sjson.DeleteBytes(before, "instructions")
		require.NoError(t, err)
		require.Equal(t, string(withoutInstructions), string(after), "%s：只能删 instructions 这一个字段，其余字节原样", mode)
	}

	// 不满足条件时一律原样返回同一份字节。
	runtimeState := newOfficialEgressTestRuntimeForMode(t, &httpUpstreamRecorder{}, officialClientProfileModeActive)
	unchanged, changed := shapeOfficialCodexDirectResponsesBody(probe, nil)
	require.False(t, changed, "runtime 缺失不得改动")
	require.Equal(t, string(probe), string(unchanged))
	noInstructions := []byte(`{"model":"m"}`)
	unchanged, changed = shapeOfficialCodexDirectResponsesBody(noInstructions, runtimeState)
	require.False(t, changed, "本来没有 instructions 不得改动")
	require.Equal(t, string(noInstructions), string(unchanged))
	notJSON := []byte(`{"instructions":`)
	unchanged, changed = shapeOfficialCodexDirectResponsesBody(notJSON, runtimeState)
	require.False(t, changed, "不是合法 JSON 不得改动")
	require.Equal(t, string(notJSON), string(unchanged))
}

// API-Key mimic 的 RewriteBody 会补回默认 instructions；影子账号借父账号的 OAuth 凭据走官方出口时两者同时生效，
// 所以整理必须排在 mimic 改写之后，作用在最终字节上。
func TestShapeOfficialCodexDirectResponsesBodyRunsAfterTheMimicRewrite(t *testing.T) {
	shadow := &Account{
		Platform: PlatformOpenAI,
		Type:     AccountTypeAPIKey,
		Extra:    map[string]any{"openai_apikey_mimic_codex_cli": true},
	}
	mimicProfile := resolveOpenAIAPIKeyCodexMimicProfile(shadow, 0, &config.Config{})
	require.True(t, mimicProfile.Enabled, "前提：影子账号开启了 Codex CLI mimic")

	payload := createOpenAITestPayload("gpt-5.5", "", true, 0)
	delete(payload, "instructions")
	raw, err := json.Marshal(payload)
	require.NoError(t, err)
	rewritten := mimicProfile.RewriteBody(raw)
	require.True(t, gjson.GetBytes(rewritten, "instructions").Exists(), "前提：mimic 改写会补回默认 instructions")

	for mode, declares := range directBodyBothSlots(t) {
		runtimeState := newOfficialEgressTestRuntimeForMode(t, &httpUpstreamRecorder{}, mode)
		shaped, changed := shapeOfficialCodexDirectResponsesBody(rewritten, runtimeState)
		require.Equal(t, !declares, changed, mode)
		require.Equal(t, declares, gjson.GetBytes(shaped, "instructions").Exists(),
			"%s：改写之后再整理，instructions 是否保留只由槽位画像闭集决定", mode)
	}
}

// 线上：两个槽位各经正式 Compiler／Executor 与真实 TLS 终端发出一次用量探针和一次管理员测试连接。
// 较新画像槽位过去会在 Compiler 里以「JSON Body 存在闭集外字段: instructions」失败关闭。
func TestOpenAICodexProbeAndAdminTestPassTheCompilerOnBothReleaseSlots(t *testing.T) {
	for mode, declares := range directBodyBothSlots(t) {
		t.Run("probe/"+mode, func(t *testing.T) {
			server := startCodexGateWireServer(t, func(request codexGateWireRequest) codexGateWireResponse {
				if request.path == codexGateResponsesPath {
					return codexGateSSECompletedResponse("resp_direct_probe", http.Header{
						"X-Codex-Primary-Used-Percent":          []string{"44"},
						"X-Codex-Primary-Window-Minutes":        []string{"300"},
						"X-Codex-Primary-Reset-After-Seconds":   []string{"100"},
						"X-Codex-Secondary-Used-Percent":        []string{"55"},
						"X-Codex-Secondary-Window-Minutes":      []string{"10080"},
						"X-Codex-Secondary-Reset-After-Seconds": []string{"200"},
					})
				}
				return codexGateDefaultResponse(request)
			})
			gate := newCodexGateService(t, mode, server)
			usage := &AccountUsageService{
				officialEgress:     gate.service.officialEgress,
				openAIQuotaService: &OpenAIQuotaService{httpUpstream: gate.upstream},
			}
			updates, err := usage.probeOpenAICodexSnapshot(t.Context(), gate.account)
			require.NoError(t, err, "%s 槽位的用量探针必须通过 Compiler（握手错误 %v，上游错误 %v）",
				mode, server.handshakeErrors(), gate.upstream.failureList())
			require.Equal(t, float64(44), updates["codex_5h_used_percent"], "探针必须取到限额快照")
			require.Equal(t, float64(55), updates["codex_7d_used_percent"])

			requests := server.httpRequestsForPath(codexGateResponsesPath)
			require.Len(t, requests, 1)
			body := requests[0].body
			require.Equal(t, declares, gjson.GetBytes(body, "instructions").Exists(),
				"%s：instructions 是否出现必须由槽位画像闭集决定：%s", mode, body)
			requireDirectWireBodyMatchesLegacyShape(t, body, declares, directBodyGoldenProbeWire)
		})

		t.Run("admin/"+mode, func(t *testing.T) {
			server := startCodexGateWireServer(t, nil)
			gate := newCodexGateService(t, mode, server)
			service := &AccountTestService{
				httpUpstream: gate.upstream, officialEgress: gate.service.officialEgress, cfg: &config.Config{},
			}
			gin.SetMode(gin.TestMode)
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/api/v1/admin/accounts/94/test", nil)
			require.NoError(t, service.testOpenAIAccountConnection(c, gate.account, "gpt-5.5", "你好 ping", ""))
			output := recorder.Body.String()
			require.Contains(t, output, `"type":"test_start"`)
			require.NotContains(t, output, `"type":"error"`, "%s 槽位的测试连接不得以错误事件结束：%s", mode, output)
			require.NotContains(t, output, "compiler.compile", mode)

			requests := server.httpRequestsForPath(codexGateResponsesPath)
			require.Len(t, requests, 1)
			body := requests[0].body
			require.Equal(t, declares, gjson.GetBytes(body, "instructions").Exists(),
				"%s：instructions 是否出现必须由槽位画像闭集决定：%s", mode, body)
			requireDirectWireBodyMatchesLegacyShape(t, body, declares, directBodyGoldenAdminPromptOnWire)
		})
	}
}

// requireDirectWireBodyMatchesLegacyShape 校验线上正文：旧画像槽位的摘要与旧代码实测黄金一致；
// 较新画像槽位的正文恰是旧画像正文去掉 instructions 后的同一组业务字段（字段值逐项相同，线序由各自画像决定）。
func requireDirectWireBodyMatchesLegacyShape(t *testing.T, wireBody []byte, declares bool, legacyGolden string) {
	t.Helper()
	if declares {
		require.Equal(t, legacyGolden, directBodyWireDigest(t, wireBody), "旧画像槽位的线上正文必须与旧代码实测黄金一致：%s", wireBody)
		return
	}
	var members map[string]json.RawMessage
	require.NoError(t, json.Unmarshal(wireBody, &members))
	names := make([]string, 0, len(members))
	for name := range members {
		names = append(names, name)
	}
	slices.Sort(names)
	require.Equal(t, []string{
		"client_metadata", "include", "input", "model", "parallel_tool_calls", "prompt_cache_key",
		"reasoning", "store", "stream", "tool_choice",
	}, names, "较新画像槽位的线上正文只能是业务字段加 Compiler 补出的 client_metadata 与 prompt_cache_key：%s", wireBody)
	for _, name := range []string{"model", "input", "tool_choice", "parallel_tool_calls", "store", "stream", "include"} {
		require.NotEmpty(t, string(members[name]), name)
	}
}

// directBodyShadowRepo 只实现影子账号解析父账号所需的 GetByID。
type directBodyShadowRepo struct {
	AccountRepository
	accounts map[int64]*Account
}

func (r *directBodyShadowRepo) GetByID(_ context.Context, id int64) (*Account, error) {
	account, ok := r.accounts[id]
	if !ok {
		return nil, errors.New("account not found")
	}
	return account, nil
}

// 影子账号（API-Key 形态、开启 Codex CLI mimic、借父账号的 OAuth 凭据）的管理员测试连接走官方出口：mimic 的 RewriteBody
// 会把默认 instructions 补回正文，整理必须排在改写之后。两个槽位各发一次，较新画像槽位必须通过 Compiler，
// 旧画像槽位仍带 instructions。
func TestAdminTestOfShadowMimicAccountPassesTheCompilerOnBothReleaseSlots(t *testing.T) {
	for mode, declares := range directBodyBothSlots(t) {
		t.Run(mode, func(t *testing.T) {
			server := startCodexGateWireServer(t, nil)
			gate := newCodexGateService(t, mode, server)
			parentID := gate.account.ID
			shadow := &Account{
				ID: 302, Platform: PlatformOpenAI, Type: AccountTypeAPIKey, Status: StatusActive,
				ParentAccountID: &parentID, Concurrency: 1,
				Extra: map[string]any{"openai_apikey_mimic_codex_cli": true},
			}
			service := &AccountTestService{
				accountRepo:  &directBodyShadowRepo{accounts: map[int64]*Account{parentID: gate.account, shadow.ID: shadow}},
				httpUpstream: gate.upstream, officialEgress: gate.service.officialEgress, cfg: &config.Config{},
			}
			gin.SetMode(gin.TestMode)
			recorder := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(recorder)
			c.Request = httptest.NewRequest(http.MethodPost, "/api/v1/admin/accounts/302/test", nil)
			require.NoError(t, service.testOpenAIAccountConnection(c, shadow, "gpt-5.4", "", ""))
			require.NotContains(t, recorder.Body.String(), `"type":"error"`, "%s：影子账号的测试连接不得以错误事件结束", mode)

			requests := server.httpRequestsForPath(codexGateResponsesPath)
			require.Len(t, requests, 1, "%s：请求必须通过 Compiler 到达终端（握手错误 %v，上游错误 %v）",
				mode, server.handshakeErrors(), gate.upstream.failureList())
			require.Equal(t, declares, gjson.GetBytes(requests[0].body, "instructions").Exists(),
				"%s：instructions 是否出现必须由槽位画像闭集决定：%s", mode, requests[0].body)
		})
	}
}
