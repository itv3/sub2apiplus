package service

import (
	"bytes"
	"net/http"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/stretchr/testify/require"
)

// 本文件锁定问题四 M1 在 service 侧的等价性与分配：Forward HTTP attempt 的语义准备直接
// 共享终态正文字节，结果与复制入口逐项一致，且不再按正文体积复制。

// officialBodySharingLargeRequestBody 在官方 HTTP 夹具的 input 中插入大段历史，保持契约
// 字段（client_metadata、prompt_cache_key、工具续接）完整，可直接走 buildUpstreamRequest。
func officialBodySharingLargeRequestBody(t *testing.T, items int, textBytes int) []byte {
	t.Helper()
	base := newOfficialOpenAIHTTPTestBody(t, true, false, true)
	payload, err := decodeOfficialJSONObjectUseNumber(base)
	require.NoError(t, err)
	input, ok := payload["input"].([]any)
	require.True(t, ok)
	filler := strings.Repeat("QUJDREVGR0g=", textBytes/12+1)[:textBytes]
	history := make([]any, 0, items)
	for i := 0; i < items; i++ {
		history = append(history,
			map[string]any{"type": "reasoning", "summary": []any{}, "encrypted_content": filler + strconv.Itoa(i)},
		)
	}
	merged := make([]any, 0, len(input)+len(history))
	merged = append(merged, input[:2]...)
	merged = append(merged, history...)
	merged = append(merged, input[2:]...)
	payload["input"] = merged
	body, err := marshalOpenAIUpstreamJSON(payload)
	require.NoError(t, err)
	return body
}

// officialBodySharingFinalRequest 走真实 buildUpstreamRequest 得到终态修正后的语义请求与正文。
func officialBodySharingFinalRequest(t *testing.T, body []byte) (*http.Request, []byte) {
	t.Helper()
	contract, err := captureOfficialOpenAIHTTPBodyContract(body)
	require.NoError(t, err)
	c := newOfficialOpenAIHTTPTestContext(body, "/v1/responses")
	req, err := (&OpenAIGatewayService{}).buildUpstreamRequest(
		c.Request.Context(), c, newOfficialOpenAIHTTPTestAccount(94), body, "oauth-token",
		openAIUpstreamRequestPlan{
			IsStream: true, PromptCacheKey: testOfficialOpenAISessionID,
			IsCodexCLI: true, OfficialEgressBodyContract: contract,
		},
	)
	require.NoError(t, err)
	final, err := readOfficialEgressRequestBodyBytes(req)
	require.NoError(t, err)
	return req, final
}

func officialBodySharingCloneRequest(req *http.Request) *http.Request {
	cloned := req.Clone(req.Context())
	cloned.Header = req.Header.Clone()
	return cloned
}

func TestOfficialCodexSemanticAttemptSharedBodyMatchesCopy(t *testing.T) {
	account := projectOfficialCodexIdentityAccount(newOfficialOpenAIHTTPTestAccount(94))
	bodies := map[string][]byte{"large": officialBodySharingLargeRequestBody(t, 16, 4096)}
	for _, stream := range []bool{false, true} {
		for _, explicit := range []bool{false, true} {
			for _, tool := range []bool{false, true} {
				name := "stream=" + strconv.FormatBool(stream) + " explicit=" + strconv.FormatBool(explicit) +
					" tool=" + strconv.FormatBool(tool)
				bodies[name] = newOfficialOpenAIHTTPTestBody(t, stream, explicit, tool)
			}
		}
	}
	for name, body := range bodies {
		req, final := officialBodySharingFinalRequest(t, body)
		for _, endpointID := range []string{officialCodexEndpointResponsesHTTP, officialCodexEndpointResponsesCompact} {
			legacyRequest := officialBodySharingCloneRequest(req)
			currentRequest := officialBodySharingCloneRequest(req)
			legacy, legacyErr := prepareOfficialCodexSemanticAttempt(
				legacyRequest, final, endpointID, "body-sharing-seed", account,
			)
			current, currentErr := prepareOfficialCodexSemanticAttempt(
				currentRequest, final, endpointID, "body-sharing-seed", account,
				officialCodexSemanticAttemptSharedBody,
			)
			label := name + "@" + endpointID
			if decodeSharingErrorParity(t, label, legacyErr, currentErr) {
				require.Equal(t, legacyErr.Error(), currentErr.Error(), label)
				continue
			}
			require.Equal(t, legacyRequest.Header, currentRequest.Header, "%s：请求 Header 副作用必须一致", label)
			require.Equal(t, legacy.Headers, current.Headers, label)
			require.True(t, reflect.DeepEqual(legacy.BodyConditions, current.BodyConditions), label)
			require.True(t, reflect.DeepEqual(legacy.RoutingHint, current.RoutingHint), label)
			require.True(t, reflect.DeepEqual(legacy.IdentityFacts, current.IdentityFacts), label)
			require.True(t, reflect.DeepEqual(legacy.Authentication, current.Authentication), label)
			legacyBytes, legacyOK := legacy.Body.ReplayableBytes()
			currentBytes, currentOK := current.Body.ReplayableBytes()
			require.Equal(t, legacyOK, currentOK, label)
			require.True(t, bytes.Equal(legacyBytes, currentBytes), "%s：语义 Body 必须逐字节一致", label)
			require.Equal(t, legacy.Body.ContentLength(), current.Body.ContentLength(), label)
		}
	}
}

func TestOfficialCodexSemanticAttemptSharedBodyDoesNotCopyLargeBody(t *testing.T) {
	account := projectOfficialCodexIdentityAccount(newOfficialOpenAIHTTPTestAccount(94))
	req, final := officialBodySharingFinalRequest(t, officialBodySharingLargeRequestBody(t, 64, 128*1024))
	require.Greater(t, len(final), 8*1024*1024)
	shared := testMeasureAllocatedBytes(t, func() {
		_, err := prepareOfficialCodexSemanticAttempt(
			officialBodySharingCloneRequest(req), final, officialCodexEndpointResponsesHTTP, "seed", account,
			officialCodexSemanticAttemptSharedBody,
		)
		require.NoError(t, err)
	})
	copied := testMeasureAllocatedBytes(t, func() {
		_, err := prepareOfficialCodexSemanticAttempt(
			officialBodySharingCloneRequest(req), final, officialCodexEndpointResponsesHTTP, "seed", account,
		)
		require.NoError(t, err)
	})
	t.Logf("终态正文 %.1f MiB：共享语义准备 %.1f KiB，复制语义准备 %.1f MiB",
		float64(len(final))/(1<<20), float64(shared)/1024, float64(copied)/(1<<20))
	require.Less(t, shared, uint64(len(final))/64,
		"共享语义准备分配 %d 字节，不得再按正文体积（%d 字节）复制", shared, len(final))
	require.GreaterOrEqual(t, copied, uint64(len(final)), "对照组的复制入口应至少分配一份正文")
}
