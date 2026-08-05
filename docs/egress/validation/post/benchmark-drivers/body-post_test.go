package service

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/url"
	"runtime"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

const changeset6BodyBenchmarkFixtureSHA256 = "7698cddeadace650567e46e7be9b66286212e26e983edb29e78da423ac713e08"

type changeset6BodyBenchmarkFixtures struct {
	largeUnchanged []byte
	largeDirty     []byte
	largeOpenWS    []byte
}

type changeset6BodyBenchmarkCase struct {
	sinkID     officialegress.SinkID
	endpointID string
	protocol   officialegress.WireProtocol
	method     string
	target     *url.URL
	body       []byte
	bundle     officialegress.ReleaseBundle
	purpose    officialegress.Purpose
}

var changeset6BenchmarkDigestSink string

// BenchmarkBodyCompileLargeUnchanged 覆盖已经按画像排序、最终语义不变的大 Responses Body。
func BenchmarkBodyCompileLargeUnchanged(b *testing.B) {
	fixtures := newChangeset6BodyBenchmarkFixtures()
	benchmarkChangeset6BodyCompile(b, newChangeset6ResponsesBenchmarkCase(b, fixtures.largeUnchanged), 1)
}

// BenchmarkBodyCompileLargeDirty 覆盖 compiler-owned 字段抽取、omit、注入和重排。
func BenchmarkBodyCompileLargeDirty(b *testing.B) {
	fixtures := newChangeset6BodyBenchmarkFixtures()
	benchmarkChangeset6BodyCompile(b, newChangeset6ResponsesBenchmarkCase(b, fixtures.largeDirty), 1)
}

// BenchmarkBodyCompileRetry 模拟同一语义 Body 的两个独立 attempt，禁止复用 dirty overlay。
func BenchmarkBodyCompileRetry(b *testing.B) {
	fixtures := newChangeset6BodyBenchmarkFixtures()
	benchmarkChangeset6BodyCompile(b, newChangeset6ResponsesBenchmarkCase(b, fixtures.largeDirty), 2)
}

// BenchmarkBodyCompileOpenWS 覆盖开放 WebSocket event 对未知字段相对顺序的保持。
func BenchmarkBodyCompileOpenWS(b *testing.B) {
	fixtures := newChangeset6BodyBenchmarkFixtures()
	benchmarkChangeset6BodyCompile(b, newChangeset6OpenWSBenchmarkCase(b, fixtures.largeOpenWS), 1)
}

func benchmarkChangeset6BodyCompile(
	b *testing.B,
	testCase changeset6BodyBenchmarkCase,
	attemptsPerIteration int,
) {
	b.Helper()
	b.ReportAllocs()
	b.SetBytes(int64(len(testCase.body) * attemptsPerIteration))
	request := &http.Request{
		Method: testCase.method,
		URL:    testCase.target,
		Header: http.Header{"Authorization": []string{"Bearer changeset6-benchmark-token"}},
	}
	request = request.WithContext(context.Background())
	compiler := officialegress.NewCompiler()
	account := officialCodexIdentityAccountProjection{
		ID: 6006, ChatGPTAccountID: "changeset6-benchmark-account",
	}
	b.ResetTimer()
	for range b.N {
		for attempt := 0; attempt < attemptsPerIteration; attempt++ {
			semantic, err := prepareOfficialCodexSemanticAttempt(
				request,
				testCase.body,
				testCase.endpointID,
				"changeset6-benchmark-invocation",
				account,
			)
			if err != nil {
				b.Fatal(err)
			}
			compiled, err := compiler.Compile(context.Background(), testCase.bundle, officialegress.CodexEgressPlan{
				SinkID: testCase.sinkID, Purpose: testCase.purpose,
				EndpointID: testCase.endpointID, Mode: officialegress.ReleaseModeActive,
				Protocol: testCase.protocol, Method: testCase.method, URL: testCase.target,
				Headers: semantic.Headers, IdentityMode: officialegress.IdentityCodexOAuthStrict,
				IdentityFacts: semantic.IdentityFacts, Authentication: semantic.Authentication,
				HeaderPolicy: officialegress.HeaderPolicy{
					ID: "changeset6.benchmark.headers", Source: "changeset6 benchmark",
				},
				BodyPolicy: officialegress.BodyPolicy{
					ID: "changeset6.benchmark.body", Source: "changeset6 benchmark",
					Conditions: semantic.BodyConditions,
				},
				BehaviorPolicy:  testCase.bundle.Behavior(),
				Body:            semantic.Body,
				InvocationID:    "changeset6-benchmark-invocation",
				DeclaredPersona: officialegress.PersonaCodexCLI,
			}, officialegress.EndpointDynamicInputs{})
			if err != nil {
				b.Fatal(err)
			}
			changeset6BenchmarkDigestSink = compiled.CompiledDigest()
		}
	}
}

func newChangeset6ResponsesBenchmarkCase(
	testingTB testing.TB,
	body []byte,
) changeset6BodyBenchmarkCase {
	testingTB.Helper()
	return newChangeset6BodyBenchmarkCase(
		testingTB,
		officialegress.SinkCodexResponsesForward,
		officialCodexEndpointResponsesHTTP,
		officialegress.WireProtocolHTTP,
		http.MethodPost,
		"https://chatgpt.com/backend-api/codex/responses",
		body,
	)
}

func newChangeset6OpenWSBenchmarkCase(
	testingTB testing.TB,
	body []byte,
) changeset6BodyBenchmarkCase {
	testingTB.Helper()
	return newChangeset6BodyBenchmarkCase(
		testingTB,
		officialegress.SinkCodexRealtimeSideband,
		officialCodexEndpointRealtimeSideband,
		officialegress.WireProtocolWebSocket,
		http.MethodGet,
		"wss://api.openai.com/v1/realtime",
		body,
	)
}

func newChangeset6BodyBenchmarkCase(
	testingTB testing.TB,
	sinkID officialegress.SinkID,
	endpointID string,
	protocol officialegress.WireProtocol,
	method string,
	rawURL string,
	body []byte,
) changeset6BodyBenchmarkCase {
	testingTB.Helper()
	binding, ok := officialegress.DefaultSinkCatalog().Resolve(sinkID)
	if !ok {
		testingTB.Fatalf("变更集 6 benchmark 缺少 SinkBinding：%s", sinkID)
	}
	resolver, err := officialegress.NewBundleResolver(
		officialegress.DefaultReleaseCatalog(), officialegress.DefaultSinkCatalog(),
	)
	if err != nil {
		testingTB.Fatal(err)
	}
	bundle, err := resolver.Resolve(officialegress.BundleResolveRequest{
		SinkID: sinkID, Mode: officialegress.ReleaseModeActive,
		Execution: officialegress.ExecutionPolicy{
			ID: "changeset6.benchmark.execution", Source: "changeset6 benchmark",
			MaxAttempts: 2, Replayable: true, ConcurrencyLimit: 1,
		},
		Deployment: officialegress.DeploymentSupportPolicy{
			ID: "changeset6.benchmark.deployment", Source: "changeset6 benchmark",
			Platform: runtime.GOOS + "/" + runtime.GOARCH, ProxyMode: "direct",
			SupportedBackends: []officialegress.BackendKind{
				officialegress.BackendHTTPUpstream,
				officialegress.BackendWebSocket,
				officialegress.BackendReqProfile,
			},
		},
		Behavior: officialegress.BehaviorPolicy{
			ID: "changeset6.benchmark.behavior", Source: "changeset6 benchmark",
			Kind: officialegress.BehaviorUserRequest, AttemptBudget: 2,
		},
	})
	if err != nil {
		testingTB.Fatal(err)
	}
	target, err := url.Parse(rawURL)
	if err != nil {
		testingTB.Fatal(err)
	}
	return changeset6BodyBenchmarkCase{
		sinkID: sinkID, endpointID: endpointID, protocol: protocol,
		method: method, target: target, body: body, bundle: bundle,
		purpose: binding.Purpose(),
	}
}

func TestChangeset6BodyBenchmarkFixtureIsFrozen(t *testing.T) {
	fixtures := newChangeset6BodyBenchmarkFixtures()
	actual := changeset6BodyBenchmarkFixtureDigest(fixtures)
	if actual != changeset6BodyBenchmarkFixtureSHA256 {
		t.Fatalf("变更集 6 benchmark fixture 摘要漂移：got=%s want=%s", actual, changeset6BodyBenchmarkFixtureSHA256)
	}
}

func newChangeset6BodyBenchmarkFixtures() changeset6BodyBenchmarkFixtures {
	largeText := strings.Repeat("官方出站大正文-0123456789abcdef", 32768)
	largeTextJSON, err := json.Marshal(largeText)
	if err != nil {
		panic(err)
	}
	metadata := `{"session_id":"019fcf00-0000-7000-8000-000000000001","turn_id":"019fcf00-0000-7000-8000-000000000002","thread_id":"019fcf00-0000-7000-8000-000000000003","x-codex-window-id":"019fcf00-0000-7000-8000-000000000004","x-codex-installation-id":"019fcf00-0000-7000-8000-000000000005"}`
	input := append([]byte(`[{"type":"message","role":"user","content":[{"type":"input_text","text":`), largeTextJSON...)
	input = append(input, []byte(`}]}]`)...)
	unchanged := bytes.NewBuffer(make([]byte, 0, len(input)+1024))
	unchanged.WriteString(`{"model":"gpt-5.6","instructions":"遵循用户要求","input":`)
	unchanged.Write(input)
	unchanged.WriteString(`,"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{"effort":"high","summary":"auto"},"store":false,"stream":true,"include":["reasoning.encrypted_content"],"prompt_cache_key":"019fcf00-0000-7000-8000-000000000001","client_metadata":`)
	unchanged.WriteString(metadata)
	unchanged.WriteByte('}')
	dirty := bytes.NewBuffer(make([]byte, 0, len(input)+1024))
	dirty.WriteString(`{"client_metadata":`)
	dirty.WriteString(metadata)
	dirty.WriteString(`,"include":["reasoning.encrypted_content"],"stream":true,"store":false,"reasoning":{"summary":"auto","effort":"high"},"parallel_tool_calls":false,"tool_choice":"auto","input":`)
	dirty.Write(input)
	dirty.WriteString(`,"instructions":"","model":"gpt-5.6","prompt_cache_key":"019fcf00-0000-7000-8000-000000000001"}`)
	openWS := bytes.NewBuffer(make([]byte, 0, len(largeTextJSON)+256))
	openWS.WriteString(`{"zeta":`)
	openWS.Write(largeTextJSON)
	openWS.WriteString(`,"type":"session.update","alpha":{"b":2},"middle":true}`)
	return changeset6BodyBenchmarkFixtures{
		largeUnchanged: unchanged.Bytes(),
		largeDirty:     dirty.Bytes(),
		largeOpenWS:    openWS.Bytes(),
	}
}

func changeset6BodyBenchmarkFixtureDigest(fixtures changeset6BodyBenchmarkFixtures) string {
	hash := sha256.New()
	for _, fixture := range []struct {
		name string
		body []byte
	}{
		{name: "large_unchanged", body: fixtures.largeUnchanged},
		{name: "large_dirty", body: fixtures.largeDirty},
		{name: "large_open_ws", body: fixtures.largeOpenWS},
	} {
		hash.Write([]byte(fixture.name))
		hash.Write([]byte{0})
		hash.Write(fixture.body)
		hash.Write([]byte{0})
	}
	return hex.EncodeToString(hash.Sum(nil))
}
