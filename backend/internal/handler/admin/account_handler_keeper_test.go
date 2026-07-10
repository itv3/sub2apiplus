package admin

import (
	"bytes"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
)

func TestIsKeeperKeepaliveCandidateRequiresSchedulableAPIKeyAccount(t *testing.T) {
	now := time.Now()
	future := now.Add(time.Hour)
	past := now.Add(-time.Hour)

	openAIAccount := service.Account{
		Platform:    service.PlatformOpenAI,
		Type:        service.AccountTypeAPIKey,
		Status:      service.StatusActive,
		Schedulable: true,
		Credentials: map[string]any{"api_key": "sk-openai"},
	}
	anthropicAccount := service.Account{
		Platform:    service.PlatformAnthropic,
		Type:        service.AccountTypeAPIKey,
		Status:      service.StatusActive,
		Schedulable: true,
		Credentials: map[string]any{"api_key": "sk-anthropic"},
	}

	tests := []struct {
		name    string
		account service.Account
		want    bool
	}{
		{name: "OpenAI API Key 可调度账号", account: openAIAccount, want: true},
		{name: "Anthropic API Key 可调度账号", account: anthropicAccount, want: true},
		{name: "停用账号", account: func() service.Account { account := openAIAccount; account.Status = "disabled"; return account }(), want: false},
		{name: "手动不可调度账号", account: func() service.Account { account := openAIAccount; account.Schedulable = false; return account }(), want: false},
		{name: "临时不可调度账号", account: func() service.Account {
			account := openAIAccount
			account.TempUnschedulableUntil = &future
			return account
		}(), want: false},
		{name: "临时不可调度已恢复", account: func() service.Account {
			account := openAIAccount
			account.TempUnschedulableUntil = &past
			return account
		}(), want: true},
		{name: "缺少凭据", account: func() service.Account { account := openAIAccount; account.Credentials = nil; return account }(), want: false},
		{name: "OAuth 账号", account: func() service.Account {
			account := openAIAccount
			account.Type = service.AccountTypeOAuth
			return account
		}(), want: false},
		{name: "其他平台", account: func() service.Account {
			account := openAIAccount
			account.Platform = service.PlatformGemini
			return account
		}(), want: false},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			if got := isKeeperKeepaliveCandidate(tt.account); got != tt.want {
				t.Fatalf("isKeeperKeepaliveCandidate() = %v, want %v", got, tt.want)
			}
		})
	}
}

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

func TestRecordKeeperKeepaliveRejectsInvalidStatus(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := &AccountHandler{}
	router := gin.New()
	router.POST("/accounts/:id/keepalive", h.RecordKeeperKeepalive)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/accounts/1/keepalive", bytes.NewReader([]byte(`{"status":"done"}`)))
	req.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d, body=%s", rec.Code, http.StatusBadRequest, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, "invalid status") {
		t.Fatalf("body = %s, want invalid status error", body)
	}
}

func TestRecordKeeperKeepaliveRejectsNegativeUsage(t *testing.T) {
	gin.SetMode(gin.TestMode)
	h := &AccountHandler{}
	router := gin.New()
	router.POST("/accounts/:id/keepalive", h.RecordKeeperKeepalive)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/accounts/1/keepalive", bytes.NewReader([]byte(`{"status":"success","input_tokens":-1}`)))
	req.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(rec, req)

	if rec.Code != http.StatusBadRequest {
		t.Fatalf("status = %d, want %d, body=%s", rec.Code, http.StatusBadRequest, rec.Body.String())
	}
	if body := rec.Body.String(); !strings.Contains(body, "token usage must be non-negative") {
		t.Fatalf("body = %s, want token validation error", body)
	}
}

func TestRecordKeeperKeepaliveSanitizesAndTruncatesFields(t *testing.T) {
	gin.SetMode(gin.TestMode)
	adminSvc := newStubAdminService()
	h := &AccountHandler{adminService: adminSvc}
	router := gin.New()
	router.POST("/accounts/:id/keepalive", h.RecordKeeperKeepalive)

	summary := strings.Repeat("s", keeperKeepaliveSummaryMaxRunes+25)
	detail := strings.Repeat("e", keeperKeepaliveErrorMaxRunes+25)
	body := map[string]any{
		"status":        " SUCCESS ",
		"session_id":    "  session-1  ",
		"model":         "  gpt-5  ",
		"summary":       summary,
		"error":         detail,
		"input_tokens":  1,
		"output_tokens": 2,
		"total_tokens":  3,
		"total_cost":    0.12,
	}
	payload, err := json.Marshal(body)
	if err != nil {
		t.Fatalf("marshal payload: %v", err)
	}

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodPost, "/accounts/9/keepalive", bytes.NewReader(payload))
	req.Header.Set("Content-Type", "application/json")
	router.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d, body=%s", rec.Code, http.StatusOK, rec.Body.String())
	}
	if adminSvc.lastUpdateAccountExtra.calls != 1 {
		t.Fatalf("UpdateAccountExtra calls = %d, want 1", adminSvc.lastUpdateAccountExtra.calls)
	}
	if got := adminSvc.lastUpdateAccountExtra.accountID; got != 9 {
		t.Fatalf("accountID = %d, want 9", got)
	}
	if got := adminSvc.lastUpdateAccountExtra.updates["keeper_last_status"]; got != "success" {
		t.Fatalf("keeper_last_status = %#v, want success", got)
	}
	if got := adminSvc.lastUpdateAccountExtra.updates["keeper_last_session_id"]; got != "session-1" {
		t.Fatalf("keeper_last_session_id = %#v, want session-1", got)
	}
	if got := adminSvc.lastUpdateAccountExtra.updates["keeper_last_summary"].(string); len([]rune(got)) != keeperKeepaliveSummaryMaxRunes {
		t.Fatalf("summary length = %d, want %d", len([]rune(got)), keeperKeepaliveSummaryMaxRunes)
	}
	if got := adminSvc.lastUpdateAccountExtra.updates["keeper_last_error"].(string); len([]rune(got)) != keeperKeepaliveErrorMaxRunes {
		t.Fatalf("error length = %d, want %d", len([]rune(got)), keeperKeepaliveErrorMaxRunes)
	}
}

func TestListKeeperProjectsFiltersInvalidEntries(t *testing.T) {
	t.Setenv("SUB2APIPLUS_KEEPER_PROJECTS", "alpha, /root/workspace, beta/gamma, .., beta, alpha, windows\\\\path, ., foo..bar")

	gin.SetMode(gin.TestMode)
	h := &AccountHandler{}
	router := gin.New()
	router.GET("/projects", h.ListKeeperProjects)

	rec := httptest.NewRecorder()
	req := httptest.NewRequest(http.MethodGet, "/projects", nil)
	router.ServeHTTP(rec, req)

	if rec.Code != http.StatusOK {
		t.Fatalf("status = %d, want %d, body=%s", rec.Code, http.StatusOK, rec.Body.String())
	}

	var resp struct {
		Data KeeperProjectsResponse `json:"data"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatalf("unmarshal response: %v", err)
	}
	if got, want := resp.Data.Projects, []string{"alpha", "beta"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("projects = %v, want %v", got, want)
	}
}
