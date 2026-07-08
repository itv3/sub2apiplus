package admin

import (
	"bytes"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

func TestBuildKeeperAccountConfigUsesLaterActivityTime(t *testing.T) {
	now := time.Date(2026, 7, 8, 12, 0, 0, 0, time.UTC)
	lastUsed := now.Add(-30 * time.Minute)
	lastKeepalive := now.Add(-5 * time.Minute)
	account := service.Account{
		ID:         1,
		Name:       "keeper-openai",
		Platform:   service.PlatformOpenAI,
		Type:       service.AccountTypeAPIKey,
		LastUsedAt: &lastUsed,
		Extra: map[string]any{
			"keeper_keepalive_enabled":          true,
			"keeper_keepalive_interval_minutes": 8,
			"keeper_last_keepalive_at":          lastKeepalive.Format(time.RFC3339),
		},
	}

	got := buildKeeperAccountConfig(account, now)

	if got.NextKeepaliveAt == nil {
		t.Fatal("NextKeepaliveAt is nil")
	}
	want := lastKeepalive.Add(8 * time.Minute)
	if !got.NextKeepaliveAt.Equal(want) {
		t.Fatalf("NextKeepaliveAt = %s, want %s", got.NextKeepaliveAt, want)
	}
	if got.Due {
		t.Fatal("Due = true, want false before later keepalive interval")
	}
}

func TestBuildKeeperAccountConfigDueWithoutActivity(t *testing.T) {
	account := service.Account{
		ID:       2,
		Name:     "keeper-anthropic",
		Platform: service.PlatformAnthropic,
		Type:     service.AccountTypeAPIKey,
		Extra: map[string]any{
			"keeper_keepalive_enabled": true,
		},
	}

	got := buildKeeperAccountConfig(account, time.Now().UTC())

	if got.NextKeepaliveAt != nil {
		t.Fatalf("NextKeepaliveAt = %s, want nil", got.NextKeepaliveAt)
	}
	if !got.Due {
		t.Fatal("Due = false, want true for enabled account without activity")
	}
}

func TestCopyKeeperProxyResponseHeadersUsesAllowlist(t *testing.T) {
	dst := http.Header{}
	src := http.Header{
		"Content-Type":         []string{"application/json"},
		"X-Request-Id":         []string{"req-1"},
		"Set-Cookie":           []string{"sid=upstream"},
		"Authorization":        []string{"Bearer upstream"},
		"Transfer-Encoding":    []string{"chunked"},
		"Anthropic-Request-Id": []string{"anthropic-1"},
	}

	copyKeeperProxyResponseHeaders(dst, src)

	if got := dst.Get("Content-Type"); got != "application/json" {
		t.Fatalf("Content-Type = %q, want application/json", got)
	}
	if got := dst.Get("X-Request-Id"); got != "req-1" {
		t.Fatalf("X-Request-Id = %q, want req-1", got)
	}
	if got := dst.Get("Anthropic-Request-Id"); got != "anthropic-1" {
		t.Fatalf("Anthropic-Request-Id = %q, want anthropic-1", got)
	}
	if got := dst.Get("Set-Cookie"); got != "" {
		t.Fatalf("Set-Cookie = %q, want empty", got)
	}
	if got := dst.Get("Authorization"); got != "" {
		t.Fatalf("Authorization = %q, want empty", got)
	}
	if got := dst.Get("Transfer-Encoding"); got != "" {
		t.Fatalf("Transfer-Encoding = %q, want empty", got)
	}
}

func TestRecordKeeperKeepaliveRejectsPromptExecution(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := &AccountHandler{}
	router := gin.New()
	router.POST("/accounts/:id/keepalive", h.RecordKeeperKeepalive)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/accounts/1/keepalive", bytes.NewReader([]byte(`{"prompt":"hello","status":"success"}`)))
	req.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d, body=%s", rec.Code, http.StatusBadRequest, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, "keeper keepalive execution must run in sidecar") {
		t.Fatalf("body = %s, want sidecar-only error", body)
	}
}
