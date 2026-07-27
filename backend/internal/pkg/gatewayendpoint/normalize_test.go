package gatewayendpoint

import "testing"

func TestNormalizeInboundEndpointBareChatCompletions(t *testing.T) {
	t.Parallel()
	for _, path := range []string{"/chat/completions", "/chat/completions/", "/chat/completions/debug"} {
		if got := NormalizeInboundEndpoint(path); got != ChatCompletions {
			t.Fatalf("NormalizeInboundEndpoint(%q) = %q，期望 %q", path, got, ChatCompletions)
		}
	}
}

func TestNormalizeInboundEndpointCountTokensBeforeMessages(t *testing.T) {
	t.Parallel()
	for _, path := range []string{"/v1/messages/count_tokens", "/anthropic/v1/messages/count_tokens"} {
		if got := NormalizeInboundEndpoint(path); got != MessagesCountTokens {
			t.Fatalf("NormalizeInboundEndpoint(%q) = %q，期望 %q", path, got, MessagesCountTokens)
		}
	}
}
