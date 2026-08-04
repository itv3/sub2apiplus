package repository

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/config"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/stretchr/testify/require"
)

type candidateConnectionTraceContextKey struct{}

type candidateConnectionTraceFact struct {
	SchemaVersion string         `json:"schema_version"`
	FactID        string         `json:"fact_id"`
	ScenarioID    string         `json:"scenario_id"`
	RecordType    string         `json:"record_type"`
	Data          map[string]any `json:"data"`
}

func candidateConnectionTraceHash(value string) string {
	digest := sha256.Sum256([]byte(value))
	return hex.EncodeToString(digest[:])
}

func candidateConnectionTraceLog(t *testing.T, data map[string]any) {
	t.Helper()
	encoded, err := json.Marshal(candidateConnectionTraceFact{
		SchemaVersion: "codex-candidate-test-fact/v1",
		FactID:        "a08.connection-lifecycle",
		ScenarioID:    "A08",
		RecordType:    "connection_lifecycle",
		Data:          data,
	})
	require.NoError(t, err)
	t.Log("CANDIDATE_TRACE_FACT " + string(encoded))
}

func TestCandidateTraceCodex0145HTTPConnectionLifecycle(t *testing.T) {
	var connectionSequence atomic.Int64
	disconnectedConnection := make(chan string, 1)
	// 编号只在 net/http Server
	// 接受真实 net.Conn 时分配，并通过请求 Context 回送给 handler。
	server := httptest.NewUnstartedServer(http.HandlerFunc(func(writer http.ResponseWriter, request *http.Request) {
		connectionID, _ := request.Context().Value(candidateConnectionTraceContextKey{}).(string)
		if connectionID == "" {
			http.Error(writer, "missing connection id", http.StatusInternalServerError)
			return
		}
		if request.URL.Path == "/controlled-disconnect" {
			disconnectedConnection <- connectionID
			hijacker, ok := writer.(http.Hijacker)
			if !ok {
				t.Error("测试 HTTP writer 不支持 hijack")
				return
			}
			connection, _, err := hijacker.Hijack()
			if err != nil {
				t.Errorf("hijack 受控断连连接失败：%v", err)
				return
			}
			_ = connection.Close()
			return
		}
		writer.Header().Set("X-Candidate-Connection-ID", connectionID)
		_, _ = io.WriteString(writer, "ok")
	}))
	server.Config.ConnContext = func(ctx context.Context, connection net.Conn) context.Context {
		connectionID := fmt.Sprintf("tcp-%d-%s", connectionSequence.Add(1), connection.RemoteAddr().String())
		return context.WithValue(ctx, candidateConnectionTraceContextKey{}, connectionID)
	}
	server.Start()
	t.Cleanup(server.Close)

	cfg := &config.Config{}
	cfg.Security.URLAllowlist.Enabled = false
	cfg.Gateway.ConnectionPoolIsolation = config.ConnectionPoolIsolationProxy
	cfg.Gateway.OpenAIHTTP2.Enabled = false
	upstream, ok := NewHTTPUpstream(cfg).(*httpUpstreamService)
	if !ok {
		t.Fatal("NewHTTPUpstream 未返回 *httpUpstreamService")
	}
	t.Cleanup(func() {
		upstream.mu.Lock()
		defer upstream.mu.Unlock()
		for _, entry := range upstream.clients {
			entry.client.CloseIdleConnections()
		}
	})

	entryFor := func(poolID string) *upstreamClientEntry {
		entry, err := upstream.getClientEntry(
			"",
			14508,
			1,
			service.HTTPUpstreamProfileOpenAI,
			false,
			false,
			poolID,
		)
		require.NoError(t, err)
		return entry
	}
	do := func(entry *upstreamClientEntry, method string, path string) (string, error) {
		request, err := http.NewRequest(method, server.URL+path, nil)
		if err != nil {
			return "", err
		}
		response, err := entry.client.Do(request)
		if err != nil {
			return "", err
		}
		_, readErr := io.ReadAll(response.Body)
		closeErr := response.Body.Close()
		if readErr != nil {
			return "", readErr
		}
		if closeErr != nil {
			return "", closeErr
		}
		return response.Header.Get("X-Candidate-Connection-ID"), nil
	}

	crossFirst, err := do(entryFor("candidate-invocation-cross-a"), http.MethodGet, "/ok")
	require.NoError(t, err)
	crossSecond, err := do(entryFor("candidate-invocation-cross-b"), http.MethodGet, "/ok")
	require.NoError(t, err)
	require.NotEmpty(t, crossFirst)
	require.NotEmpty(t, crossSecond)
	require.NotEqual(t, crossFirst, crossSecond)

	keepaliveEntry := entryFor("candidate-invocation-keepalive")
	keepaliveFirst, err := do(keepaliveEntry, http.MethodGet, "/retry-500-first")
	require.NoError(t, err)
	keepaliveSecond, err := do(keepaliveEntry, http.MethodGet, "/retry-500-second")
	require.NoError(t, err)
	require.NotEmpty(t, keepaliveFirst)
	require.Equal(t, keepaliveFirst, keepaliveSecond)

	disconnectEntry := entryFor("candidate-invocation-disconnect")
	_, err = do(disconnectEntry, http.MethodPost, "/controlled-disconnect")
	require.Error(t, err)
	disconnectFirst := <-disconnectedConnection
	disconnectSecond, err := do(disconnectEntry, http.MethodPost, "/after-controlled-disconnect")
	require.NoError(t, err)
	require.NotEmpty(t, disconnectFirst)
	require.NotEmpty(t, disconnectSecond)
	require.NotEqual(t, disconnectFirst, disconnectSecond)

	candidateConnectionTraceLog(t, map[string]any{
		"cross_call_connection_ids": []string{
			candidateConnectionTraceHash(crossFirst),
			candidateConnectionTraceHash(crossSecond),
		},
		"disconnect_retry_connection_ids": []string{
			candidateConnectionTraceHash(disconnectFirst),
			candidateConnectionTraceHash(disconnectSecond),
		},
		"keepalive_retry_connection_ids": []string{
			candidateConnectionTraceHash(keepaliveFirst),
			candidateConnectionTraceHash(keepaliveSecond),
		},
	})
}
