package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
	"github.com/Wei-Shaw/sub2api/internal/officialegress/receiptcontract"
	"github.com/klauspost/compress/zstd"
	"github.com/stretchr/testify/require"
)

type changeset1BReceiptArtifactCase struct {
	sinkID     officialegress.SinkID
	endpointID string
	path       string
}

type changeset1BWireFixture struct {
	SchemaVersion        int                           `json:"schema_version"`
	CaptureKind          string                        `json:"capture_kind"`
	SinkID               string                        `json:"sink_id"`
	Route                receiptcontract.RouteIdentity `json:"route"`
	AuthorityID          string                        `json:"authority_id"`
	HasFinalizationToken bool                          `json:"has_finalization_token"`
	HeaderNames          []string                      `json:"header_names"`
	BodyFields           []string                      `json:"body_fields"`
	BodyShapeSHA256      string                        `json:"body_shape_sha256"`
	Redaction            string                        `json:"redaction"`
}

// TestChangeset1BReceiptArtifactsReplayProductionExecutor 使用生产 Executor、
// 固定 Authority 与 100% canary Guard 重放四条已迁移路径。受审产物
// 只保存最终请求的字段形状和头名，凭据、动态 ID 与正文值均不落盘。
func TestChangeset1BReceiptArtifactsReplayProductionExecutor(t *testing.T) {
	previousGuard := officialegress.DefaultGuard()
	guard, err := officialegress.ConfigureDefaultGuard(officialegress.GuardConfig{
		UnknownRoutePolicy:     officialegress.UnknownRoutePolicy(officialegress.PolicyObserve),
		UnregisteredSinkPolicy: officialegress.UnregisteredSinkPolicy(officialegress.PolicyObserve),
		CanaryPercent:          100,
	}, nil)
	require.NoError(t, err)
	t.Cleanup(func() {
		_, restoreErr := officialegress.ConfigureDefaultGuard(previousGuard.Config(), previousGuard.Recorder())
		require.NoError(t, restoreErr)
	})

	upstream := &changeset1BExecutorUpstream{}
	runtimeState, err := newOfficialEgressTransitionRuntimeWithExecutor(
		guard,
		upstream,
		officialCodexExecutorID,
		officialegress.ReleaseModeActive,
	)
	require.NoError(t, err)

	testCases := []changeset1BReceiptArtifactCase{
		{officialEgressSinkAdminTestCompact, officialCodexEndpointResponsesCompact, "/backend-api/codex/responses/compact"},
		{officialEgressSinkAdminTestResponses, officialCodexEndpointResponsesHTTP, "/backend-api/codex/responses"},
		{officialEgressSinkAlphaSearchPATFallback, officialCodexEndpointResponsesHTTP, "/backend-api/codex/responses"},
		{officialEgressSinkUsageProbe, officialCodexEndpointResponsesHTTP, "/backend-api/codex/responses"},
	}
	for _, testCase := range testCases {
		t.Run(string(testCase.sinkID), func(t *testing.T) {
			wireRaw, verificationRaw := replayChangeset1BReceiptArtifact(
				t,
				runtimeState,
				upstream,
				testCase,
			)
			artifactDirectory := filepath.Join(
				"..", "officialegress", "catalogdata", "migration-artifacts", "changeset1b",
				strings.ReplaceAll(string(testCase.sinkID), ".", "_"),
			)
			assertChangeset1BActive01491WireSuccessor(
				t, filepath.Join(artifactDirectory, "wire.json"), wireRaw,
			)
			assertChangeset1BActive01491VerificationSuccessor(
				t,
				filepath.Join(artifactDirectory, "execution-verification.json"),
				verificationRaw,
				wireRaw,
			)
		})
	}
}

func replayChangeset1BReceiptArtifact(
	t *testing.T,
	runtimeState *OfficialEgressTransitionRuntime,
	upstream *changeset1BExecutorUpstream,
	testCase changeset1BReceiptArtifactCase,
) ([]byte, []byte) {
	t.Helper()
	binding, ok := officialegress.DefaultSinkCatalog().Resolve(testCase.sinkID)
	require.True(t, ok)
	requestContext, err := officialegress.WithAttemptMetadata(context.Background(), officialegress.AttemptMetadataInput{
		SinkID: testCase.sinkID, Purpose: binding.Purpose(), DeclaredPersona: binding.Persona(),
		InvocationID: "changeset1b-receipt-" + strings.ReplaceAll(string(testCase.sinkID), ".", "-"),
	})
	require.NoError(t, err)
	request, err := http.NewRequestWithContext(
		requestContext,
		http.MethodPost,
		"https://chatgpt.com"+testCase.path,
		bytes.NewBufferString(`{"model":"gpt-5.6-sol","input":[],"tool_choice":"auto","parallel_tool_calls":true,"reasoning":{"effort":"medium","summary":"auto"},"store":false,"stream":true,"include":[]}`),
	)
	require.NoError(t, err)
	request.Header.Set("Authorization", "Bearer receipt-secret-never-persisted")
	request.Header.Set("Content-Type", "application/json")
	request.Header.Set("Accept", "text/event-stream")

	account := &Account{
		ID: 991, Platform: PlatformOpenAI, Type: AccountTypeOAuth, Concurrency: 1,
		Credentials: map[string]any{
			"access_token":       "receipt-secret-never-persisted",
			"chatgpt_account_id": "receipt-account-never-persisted",
		},
	}
	setOpenAIChatGPTAccountHeaders(request.Header, account)
	response, err := runtimeState.ExecuteCodexHTTP(requestContext, OfficialCodexHTTPExecution{
		SinkID: testCase.sinkID, EndpointID: testCase.endpointID,
		Account: account, Request: request,
		PolicyID: "changeset1b.migration-receipt.v1", PolicySource: "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md#policy-changeset-1b",
		ConcurrencyLimit: 1, HasBillingSideEffect: true,
	})
	require.NoError(t, err)
	require.NotNil(t, response)
	_ = response.Body.Close()
	require.NotNil(t, upstream.request)

	finalRequest := upstream.request
	identity, ok := officialegress.AttemptIdentityFromContext(finalRequest.Context())
	require.True(t, ok)
	require.Equal(t, testCase.sinkID, identity.SinkID)
	require.Equal(t, officialCodexExecutorID, identity.ExecutorID)
	require.True(t, identity.HasFinalizationToken)

	bodyRaw, err := io.ReadAll(finalRequest.Body)
	require.NoError(t, err)
	finalRequest.Body = io.NopCloser(bytes.NewReader(bodyRaw))
	bodyDocument := bodyRaw
	if strings.EqualFold(finalRequest.Header.Get("Content-Encoding"), "zstd") {
		decoder, decodeErr := zstd.NewReader(nil)
		require.NoError(t, decodeErr)
		defer decoder.Close()
		bodyDocument, decodeErr = decoder.DecodeAll(bodyRaw, nil)
		require.NoError(t, decodeErr)
	}
	bodyFields, bodyShapeSHA256 := changeset1BBodyShape(t, bodyDocument)
	headerNames := make([]string, 0, len(finalRequest.Header))
	for name := range finalRequest.Header {
		headerNames = append(headerNames, strings.ToLower(name))
	}
	sort.Strings(headerNames)
	route := receiptcontract.RouteIdentity{
		Method: finalRequest.Method, Host: finalRequest.URL.Hostname(), Path: finalRequest.URL.Path,
		Purpose: string(binding.Purpose()), Protocol: string(officialegress.WireProtocolHTTP),
	}
	fixture := changeset1BWireFixture{
		SchemaVersion: 1, CaptureKind: "sanitized_final_request", SinkID: string(testCase.sinkID),
		Route: route, AuthorityID: string(identity.ExecutorID),
		HasFinalizationToken: identity.HasFinalizationToken,
		HeaderNames:          headerNames, BodyFields: bodyFields, BodyShapeSHA256: bodyShapeSHA256,
		Redaction: "仅保存头名、正文字段名与类型形状；所有凭据、动态 ID 和字段值已省略。",
	}
	wireRaw := mustMarshalChangeset1BArtifact(t, fixture)
	verification := receiptcontract.ExecutionVerification{
		SchemaVersion: 1, Result: "passed", SinkID: string(testCase.sinkID), Route: route,
		AuthorityKind: receiptcontract.AuthorityCodexExecutor,
		AuthorityID:   string(identity.ExecutorID), TokenIssuerID: string(identity.ExecutorID),
		EvidenceKind: "codex_endpoint", EvidenceID: testCase.endpointID,
		Backend:     string(officialegress.BackendHTTPUpstream),
		AdapterID:   string(officialegress.AdapterHTTPUpstream),
		TransportID: officialCodexTransportHTTPDefault,
		WireSHA256:  changeset1BSHA256(wireRaw),
	}
	return wireRaw, mustMarshalChangeset1BArtifact(t, verification)
}

func changeset1BBodyShape(t *testing.T, raw []byte) ([]string, string) {
	t.Helper()
	var document map[string]any
	require.NoError(t, json.Unmarshal(raw, &document))
	fields := make([]string, 0, len(document))
	shape := make(map[string]string, len(document))
	for key, value := range document {
		fields = append(fields, key)
		shape[key] = fmt.Sprintf("%T", value)
	}
	sort.Strings(fields)
	shapeRaw, err := json.Marshal(shape)
	require.NoError(t, err)
	return fields, changeset1BSHA256(shapeRaw)
}

func mustMarshalChangeset1BArtifact(t *testing.T, value any) []byte {
	t.Helper()
	raw, err := json.MarshalIndent(value, "", "  ")
	require.NoError(t, err)
	return append(raw, '\n')
}

func changeset1BSHA256(raw []byte) string {
	digest := sha256.Sum256(raw)
	return hex.EncodeToString(digest[:])
}

func assertChangeset1BActive01491WireSuccessor(
	t *testing.T,
	path string,
	want []byte,
) {
	t.Helper()
	historicalRaw, err := os.ReadFile(path)
	require.NoError(t, err)
	var historical changeset1BWireFixture
	require.NoError(t, json.Unmarshal(historicalRaw, &historical))
	require.NotContains(
		t,
		historical.HeaderNames,
		"x-codex-routing-hint",
		"changeset1b 冻结制品必须保持 0.147 原文",
	)
	successor := historical
	successor.HeaderNames = append([]string(nil), historical.HeaderNames...)
	successor.HeaderNames = append(successor.HeaderNames, "x-codex-routing-hint")
	sort.Strings(successor.HeaderNames)
	require.Equal(t, string(mustMarshalChangeset1BArtifact(t, successor)), string(want))
}

func assertChangeset1BActive01491VerificationSuccessor(
	t *testing.T,
	path string,
	want []byte,
	activeWire []byte,
) {
	t.Helper()
	historicalRaw, err := os.ReadFile(path)
	require.NoError(t, err)
	var historical receiptcontract.ExecutionVerification
	require.NoError(t, json.Unmarshal(historicalRaw, &historical))
	historical.WireSHA256 = changeset1BSHA256(activeWire)
	require.Equal(t, string(mustMarshalChangeset1BArtifact(t, historical)), string(want))
}
