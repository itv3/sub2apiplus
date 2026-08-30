package service

import (
	"context"
	"net/http"
	"sync"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

func TestChangeset6ConcurrentPrepareUsesIndependentAttemptDocuments(t *testing.T) {
	body := []byte(`{"client_metadata":{"session_id":"session-1","thread_id":"thread-1","turn_id":"turn-1"},"prompt_cache_key":"caller","model":"gpt-5.6","input":[],"tool_choice":"auto","parallel_tool_calls":false,"reasoning":{},"store":false,"stream":true,"include":[]}`)
	testCase := newChangeset6ResponsesBenchmarkCase(t, body)
	request := &http.Request{
		Method: testCase.method,
		URL:    testCase.target,
		Header: http.Header{"Authorization": []string{"Bearer concurrent-token"}},
	}
	account := officialCodexIdentityAccountProjection{ID: 6006, ChatGPTAccountID: "concurrent-account"}
	compiler := officialegress.NewCompiler()
	var wait sync.WaitGroup
	var digestMu sync.Mutex
	wantDigest := ""
	for range 8 {
		wait.Add(1)
		go func() {
			defer wait.Done()
			for range 8 {
				semantic, err := prepareOfficialCodexSemanticAttempt(
					request, body, testCase.endpointID, "concurrent-invocation", account,
				)
				if err != nil {
					t.Errorf("并发 prepare 失败：%v", err)
					return
				}
				compiled, err := compiler.Compile(context.Background(), testCase.bundle, officialegress.CodexEgressPlan{
					SinkID: testCase.sinkID, Purpose: testCase.purpose,
					EndpointID: testCase.endpointID, Mode: officialegress.ReleaseModeActive,
					Protocol: testCase.protocol, Method: testCase.method, URL: testCase.target,
					Headers: semantic.Headers, IdentityMode: officialegress.IdentityCodexOAuthStrict,
					IdentityFacts: semantic.IdentityFacts, Authentication: semantic.Authentication,
					HeaderPolicy: officialegress.HeaderPolicy{ID: "changeset6.concurrent.headers", Source: "test"},
					BodyPolicy: officialegress.BodyPolicy{
						ID: "changeset6.concurrent.body", Source: "test", Conditions: semantic.BodyConditions,
					},
					RoutingHint:    semantic.RoutingHint,
					BehaviorPolicy: testCase.bundle.Behavior(), Body: semantic.Body,
					InvocationID: "concurrent-invocation", DeclaredPersona: officialegress.PersonaCodexCLI,
				}, officialegress.EndpointDynamicInputs{})
				if err != nil {
					t.Errorf("并发 Compile 失败：%v", err)
					return
				}
				digestMu.Lock()
				if wantDigest == "" {
					wantDigest = compiled.CompiledDigest()
				} else if compiled.CompiledDigest() != wantDigest {
					t.Errorf("并发 attempt document 产生不一致摘要")
				}
				digestMu.Unlock()
			}
		}()
	}
	wait.Wait()
}
