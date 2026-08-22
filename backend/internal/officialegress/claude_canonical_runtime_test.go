package officialegress

import (
	"bytes"
	"context"
	"fmt"
	"net/http"
	"testing"
)

func TestClaudeCanonicalIngressesCompileIdenticalFinalWire(t *testing.T) {
	models := []string{"claude-sonnet-5", "claude-opus-5", "claude-fable-5"}
	for _, model := range models {
		for _, stream := range []bool{false, true} {
			t.Run(fmt.Sprintf("%s-stream-%t", model, stream), func(t *testing.T) {
				messagesBody := []byte(fmt.Sprintf(`{
					"model":%q,
					"messages":[{"role":"user","content":[{"type":"text","text":"pair probe"}]}],
					"system":[{"type":"text","text":"custom rules"}],
					"tools":[],"max_tokens":32000,
					"output_config":{"effort":"low"},"stream":%t
				}`, model, stream))
				chatBody := []byte(fmt.Sprintf(`{
					"model":%q,
					"messages":[
						{"role":"system","content":"custom rules"},
						{"role":"user","content":[{"type":"text","text":"pair probe"}]}
					],
					"tools":[],"max_completion_tokens":32000,
					"reasoning_effort":"low","stream":%t
				}`, model, stream))
				responsesBody := []byte(fmt.Sprintf(`{
					"model":%q,"instructions":"custom rules",
					"input":[{"type":"message","role":"user","content":[{"type":"input_text","text":"pair probe"}]}],
					"tools":[],"max_output_tokens":32000,
					"reasoning":{"effort":"low"},"stream":%t
				}`, model, stream))

				messagesWire, messagesHeaders := executeClaudeMessagesWireForPair(t, messagesBody)
				chatWire, chatHeaders := executeClaudeCanonicalWireForPair(
					t, IngressProtocolOpenAIChatCompletions, chatBody,
				)
				responsesWire, responsesHeaders := executeClaudeCanonicalWireForPair(
					t, IngressProtocolOpenAIResponses, responsesBody,
				)
				if !bytes.Equal(messagesWire, chatWire) || !bytes.Equal(messagesWire, responsesWire) {
					t.Fatalf("三种入站没有生成同一 final wire：\nmessages=%s\nchat=%s\nresponses=%s",
						messagesWire, chatWire, responsesWire)
				}
				for _, name := range []string{
					"Accept", "Authorization", "Content-Type", "User-Agent",
					"X-Claude-Code-Session-Id", "X-Stainless-Package-Version",
					"anthropic-beta", "anthropic-version", "x-app", "x-client-request-id",
				} {
					want := claudeTestHeaderValue(messagesHeaders, name)
					if claudeTestHeaderValue(chatHeaders, name) != want ||
						claudeTestHeaderValue(responsesHeaders, name) != want {
						t.Fatalf("三种入站 Header %s 不一致", name)
					}
				}
			})
		}
	}
}

func executeClaudeMessagesWireForPair(
	t *testing.T,
	body []byte,
) ([]byte, http.Header) {
	t.Helper()
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	result, err := runtime.ExecuteMessages(context.Background(), ClaudeMessagesExecution{
		Body: body, AccessToken: "pair-token", TrustedFacts: claudeTestTrustedFacts(),
		InvocationID: claudeTestRequestID,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = result.Response.Body.Close() }()
	if err := result.FinalizeSession(false); err != nil {
		t.Fatal(err)
	}
	request := findClaudeCapturedRequest(t, port.snapshot(), http.MethodPost, "/v1/messages?beta=true")
	return decodeClaudeTestBody(t, request), request.Header
}

func executeClaudeCanonicalWireForPair(
	t *testing.T,
	protocol string,
	body []byte,
) ([]byte, http.Header) {
	t.Helper()
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	canonical, report, err := AdaptIngressProtocol(protocol, body)
	if err != nil {
		t.Fatal(err)
	}
	trusted := claudeTestTrustedFacts()
	trusted.Entrypoint.IngressProtocol = protocol
	result, err := runtime.ExecuteCanonical(context.Background(), ClaudeCanonicalExecution{
		Canonical: canonical, Translation: report, AccessToken: "pair-token",
		TrustedFacts: trusted, InvocationID: claudeTestRequestID,
	})
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = result.Response.Body.Close() }()
	if err := result.FinalizeSession(false); err != nil {
		t.Fatal(err)
	}
	request := findClaudeCapturedRequest(t, port.snapshot(), http.MethodPost, "/v1/messages?beta=true")
	return decodeClaudeTestBody(t, request), request.Header
}

func TestClaudeCanonicalValidationFailsBeforeAnyUpstreamCall(t *testing.T) {
	port := &claudeCapturePort{}
	runtime, _, _ := newClaudeTestRuntime(t, port)
	canonical, report, err := AdaptIngressProtocol(
		IngressProtocolOpenAIResponses,
		[]byte(`{"model":"claude-unknown","input":"hello"}`),
	)
	if err != nil {
		t.Fatal(err)
	}
	trusted := claudeTestTrustedFacts()
	trusted.Entrypoint.IngressProtocol = IngressProtocolOpenAIResponses
	_, err = runtime.ExecuteCanonical(context.Background(), ClaudeCanonicalExecution{
		Canonical: canonical, Translation: report, AccessToken: "must-not-be-used",
		TrustedFacts: trusted, InvocationID: claudeTestRequestID,
	})
	if err == nil || !IsClaudeSupportEnvelopeRejection(err) {
		t.Fatalf("未知模型没有在 SupportEnvelope 门禁拒绝：%v", err)
	}
	if len(port.snapshot()) != 0 {
		t.Fatalf("本地拒绝前发生了上游调用：%+v", port.snapshot())
	}

	canonical.Model = "claude-sonnet-5"
	report.Lossless = false
	_, err = runtime.ExecuteCanonical(context.Background(), ClaudeCanonicalExecution{
		Canonical: canonical, Translation: report, AccessToken: "must-not-be-used",
		TrustedFacts: trusted, InvocationID: claudeTestRequestID,
	})
	if err == nil || !IsClaudeSupportEnvelopeRejection(err) || len(port.snapshot()) != 0 {
		t.Fatalf("非 lossless TranslationReport 没有在上游前拒绝：err=%v calls=%d",
			err, len(port.snapshot()))
	}
}
