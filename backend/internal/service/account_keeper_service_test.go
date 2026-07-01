package service

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/Wei-Shaw/sub2api/internal/pkg/tlsfingerprint"
)

func TestCollectKeeperOpenAIResponsesStream(t *testing.T) {
	body := strings.NewReader(strings.Join([]string{
		`data: {"type":"response.output_text.delta","delta":"可以"}`,
		`data: {"type":"response.output_text.delta","delta":"优化"}`,
		`data: {"type":"response.completed","response":{"usage":{"input_tokens":12,"output_tokens":8,"total_tokens":20,"input_tokens_details":{"cached_tokens":3}}}}`,
		``,
	}, "\n"))

	reply, usage, err := collectKeeperOpenAIResponsesStream(body)
	if err != nil {
		t.Fatalf("collectKeeperOpenAIResponsesStream returned error: %v", err)
	}
	if reply != "可以优化" {
		t.Fatalf("reply = %q, want %q", reply, "可以优化")
	}
	if usage.InputTokens != 12 || usage.OutputTokens != 8 || usage.TotalTokens != 20 || usage.CacheReadTokens != 3 {
		t.Fatalf("usage = %+v", usage)
	}
}

func TestCollectKeeperClaudeStream(t *testing.T) {
	body := strings.NewReader(strings.Join([]string{
		`data: {"type":"message_start","message":{"usage":{"input_tokens":15,"cache_creation_input_tokens":2,"cache_read_input_tokens":4}}}`,
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"继续"}}`,
		`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"分析"}}`,
		`data: {"type":"message_delta","usage":{"output_tokens":9}}`,
		`data: {"type":"message_stop"}`,
		``,
	}, "\n"))

	reply, usage, err := collectKeeperClaudeStream(body)
	if err != nil {
		t.Fatalf("collectKeeperClaudeStream returned error: %v", err)
	}
	if reply != "继续分析" {
		t.Fatalf("reply = %q, want %q", reply, "继续分析")
	}
	if usage.InputTokens != 15 || usage.OutputTokens != 9 || usage.CacheCreationTokens != 2 || usage.CacheReadTokens != 4 || usage.TotalTokens != 30 {
		t.Fatalf("usage = %+v", usage)
	}
}

func TestRunKeeperKeepaliveAnthropicUsesContext1M(t *testing.T) {
	account := &Account{
		ID:          101,
		Name:        "anthropic-apikey",
		Platform:    PlatformAnthropic,
		Type:        AccountTypeAPIKey,
		Status:      StatusActive,
		Schedulable: true,
		Concurrency: 1,
		Credentials: map[string]any{
			"api_key":  "anthropic-key",
			"base_url": "https://api.anthropic.com",
		},
	}
	upstream := &keeperHTTPUpstreamRecorder{resp: &http.Response{
		StatusCode: http.StatusOK,
		Header:     http.Header{"request-id": []string{"rid-keeper"}},
		Body: io.NopCloser(strings.NewReader(strings.Join([]string{
			`data: {"type":"message_start","message":{"usage":{"input_tokens":1}}}`,
			`data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"ok"}}`,
			`data: {"type":"message_delta","usage":{"output_tokens":1}}`,
			`data: {"type":"message_stop"}`,
			``,
		}, "\n"))),
	}}
	svc := &AccountTestService{
		accountRepo:  &keeperAccountRepo{account: account},
		httpUpstream: upstream,
		cfg:          &config.Config{Security: config.SecurityConfig{URLAllowlist: config.URLAllowlistConfig{Enabled: false}}},
	}

	result, err := svc.RunKeeperKeepalive(context.Background(), account.ID, KeeperKeepaliveRequest{
		Model:           "claude-opus-4-8",
		Prompt:          "hi",
		MaxOutputTokens: 64,
	})
	if err != nil {
		t.Fatalf("RunKeeperKeepalive() error = %v", err)
	}
	if result.Model != "claude-opus-4-8[1m]" {
		t.Fatalf("result.Model = %q, want claude-opus-4-8[1m]", result.Model)
	}
	if upstream.request == nil {
		t.Fatal("upstream request is nil")
	}
	var payload struct {
		Model string `json:"model"`
	}
	if err := json.Unmarshal(upstream.body, &payload); err != nil {
		t.Fatalf("json.Unmarshal(upstream.body) error = %v", err)
	}
	if payload.Model != "claude-opus-4-8[1m]" {
		t.Fatalf("payload.Model = %q, want claude-opus-4-8[1m]", payload.Model)
	}
	if got := upstream.request.Header.Get("x-api-key"); got != "anthropic-key" {
		t.Fatalf("x-api-key = %q, want anthropic-key", got)
	}
	if got := upstream.request.Header.Get("anthropic-beta"); !strings.Contains(got, claude.BetaContext1M) {
		t.Fatalf("anthropic-beta = %q, want containing %q", got, claude.BetaContext1M)
	}
}

type keeperAccountRepo struct {
	AccountRepository
	account *Account
}

func (r *keeperAccountRepo) GetByID(_ context.Context, id int64) (*Account, error) {
	if r.account != nil && r.account.ID == id {
		return r.account, nil
	}
	return nil, errors.New("account not found")
}

type keeperHTTPUpstreamRecorder struct {
	resp    *http.Response
	request *http.Request
	body    []byte
}

func (r *keeperHTTPUpstreamRecorder) Do(req *http.Request, _ string, _ int64, _ int) (*http.Response, error) {
	return r.record(req)
}

func (r *keeperHTTPUpstreamRecorder) DoWithTLS(req *http.Request, _ string, _ int64, _ int, _ *tlsfingerprint.Profile) (*http.Response, error) {
	return r.record(req)
}

func (r *keeperHTTPUpstreamRecorder) record(req *http.Request) (*http.Response, error) {
	if req != nil && req.Body != nil {
		body, _ := io.ReadAll(req.Body)
		r.body = append([]byte(nil), body...)
		_ = req.Body.Close()
		req.Body = io.NopCloser(bytes.NewReader(body))
	}
	r.request = req
	if r.resp == nil {
		return nil, errors.New("missing mocked response")
	}
	return r.resp, nil
}
