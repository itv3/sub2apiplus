package admin

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"

	infraerrors "github.com/Wei-Shaw/sub2api/internal/pkg/errors"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

// ---------------------------------------------------------------------------
// 假服务：记录收到的参数，返回预设结果或错误
// ---------------------------------------------------------------------------

type codexThirdPartyFakeService struct {
	mu sync.Mutex

	lastGroupID int64
	lastPreview service.CodexThirdPartyPreviewRequest
	lastSave    service.CodexThirdPartySaveRequest
	lastVerify  service.CodexThirdPartyVerifyRequest
	lastAlias   [3]string
	calls       []string

	err error
}

func (f *codexThirdPartyFakeService) record(name string, groupID int64) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, name)
	f.lastGroupID = groupID
}

func (f *codexThirdPartyFakeService) GetGroupState(_ context.Context, groupID int64) (*service.CodexThirdPartyGroupState, error) {
	f.record("get", groupID)
	if f.err != nil {
		return nil, f.err
	}
	return &service.CodexThirdPartyGroupState{
		GroupID: groupID, GroupName: "g", GroupPlatform: "composite",
		Config:     service.NewCodexModelVisibilityConfig(),
		Evaluation: service.CodexThirdPartyCatalogEvaluation{GroupID: groupID, InputFingerprint: "fp", Candidates: []service.CodexThirdPartyCandidate{}},
		Sources:    []service.CodexThirdPartySourceStatus{},
	}, nil
}

func (f *codexThirdPartyFakeService) PreviewChanges(_ context.Context, groupID int64, req service.CodexThirdPartyPreviewRequest) (*service.CodexThirdPartyPreviewResult, error) {
	f.record("preview", groupID)
	f.lastPreview = req
	if f.err != nil {
		return nil, f.err
	}
	return &service.CodexThirdPartyPreviewResult{Revision: req.ExpectedRevision, Evaluation: service.CodexThirdPartyCatalogEvaluation{InputFingerprint: "fp-preview"}}, nil
}

func (f *codexThirdPartyFakeService) SaveChanges(_ context.Context, groupID int64, req service.CodexThirdPartySaveRequest) (*service.CodexThirdPartySaveResult, error) {
	f.record("save", groupID)
	f.lastSave = req
	if f.err != nil {
		return nil, f.err
	}
	cfg := service.NewCodexModelVisibilityConfig()
	cfg.Revision = req.ExpectedRevision + 1
	return &service.CodexThirdPartySaveResult{Config: cfg}, nil
}

func (f *codexThirdPartyFakeService) MarkVerified(_ context.Context, groupID int64, req service.CodexThirdPartyVerifyRequest) (*service.CodexThirdPartyVerifyResult, error) {
	f.record("verify", groupID)
	f.lastVerify = req
	if f.err != nil {
		return nil, f.err
	}
	cfg := service.NewCodexModelVisibilityConfig()
	cfg.Revision = req.ExpectedRevision + 1
	return &service.CodexThirdPartyVerifyResult{Config: cfg}, nil
}

func (f *codexThirdPartyFakeService) SyncSources(_ context.Context, groupID int64) (*service.CodexThirdPartySyncResult, error) {
	f.record("sync", groupID)
	if f.err != nil {
		return nil, f.err
	}
	return &service.CodexThirdPartySyncResult{Sources: []service.CodexThirdPartySourceStatus{{AccountID: 1, Status: "missing", Refreshing: true}}}, nil
}

func (f *codexThirdPartyFakeService) RefreshCatalogs(_ context.Context, groupID int64) (*service.CodexThirdPartyRefreshResult, error) {
	f.record("refresh", groupID)
	if f.err != nil {
		return nil, f.err
	}
	state, _ := f.GetGroupState(context.Background(), groupID)
	return &service.CodexThirdPartyRefreshResult{
		Refresh: service.CodexCatalogRefreshReport{
			GroupID: groupID, Total: 2, Failed: 1,
			Items: []service.CodexCatalogRefreshItem{
				{AccountID: 11, AccountName: "CommandCode", Platform: "commandcode", Kind: service.CodexCatalogRefreshKindModelProtocol, OK: true, ModelCount: 40, ElapsedMS: 120},
				{AccountID: 12, AccountName: "官方", Platform: "openai", Kind: service.CodexCatalogRefreshKindOfficialBase, Reason: "拉取超时", ElapsedMS: 3000},
			},
		},
		State: state,
	}, nil
}

func (f *codexThirdPartyFakeService) aliasList() *service.CodexProfileAliasList {
	return &service.CodexProfileAliasList{
		Revision: 2,
		Aliases: []service.CodexProfileAliasView{
			{UpstreamModel: "acme-ds:prod", Key: "acme-ds", ProfileID: "deepseek-flash", Valid: true, UpdatedBy: "user:9"},
		},
		Profiles: []service.CodexThirdPartyProfileSummary{{ID: "deepseek-flash", UpstreamModels: []string{"deepseek-v4.1-flash"}, Protocols: []string{"responses_native"}}},
	}
}

func (f *codexThirdPartyFakeService) ListProfileAliases(_ context.Context) (*service.CodexProfileAliasList, error) {
	f.record("alias-list", 0)
	if f.err != nil {
		return nil, f.err
	}
	return f.aliasList(), nil
}

func (f *codexThirdPartyFakeService) SetProfileAlias(_ context.Context, upstreamModel, profileID, operator string) (*service.CodexProfileAliasList, error) {
	f.record("alias-set", 0)
	f.lastAlias = [3]string{upstreamModel, profileID, operator}
	if f.err != nil {
		return nil, f.err
	}
	return f.aliasList(), nil
}

func (f *codexThirdPartyFakeService) DeleteProfileAlias(_ context.Context, upstreamModel string) (*service.CodexProfileAliasList, error) {
	f.record("alias-delete", 0)
	f.lastAlias = [3]string{upstreamModel, "", ""}
	if f.err != nil {
		return nil, f.err
	}
	return f.aliasList(), nil
}

func newCodexThirdPartyTestRouter(h *CodexThirdPartyModelsHandler) *gin.Engine {
	gin.SetMode(gin.TestMode)
	router := gin.New()
	group := router.Group("/api/v1/admin/codex-third-party-models")
	group.GET("/:group_id", h.Get)
	group.POST("/:group_id/preview", h.Preview)
	group.PUT("/:group_id", h.Save)
	group.POST("/:group_id/verify", h.Verify)
	group.POST("/:group_id/sync", h.Sync)
	group.POST("/:group_id/refresh", h.Refresh)
	aliases := router.Group("/api/v1/admin/codex-third-party-profile-aliases")
	aliases.GET("", h.ListProfileAliases)
	aliases.PUT("", h.SetProfileAlias)
	aliases.DELETE("", h.DeleteProfileAlias)
	return router
}

func codexThirdPartyRequest(router *gin.Engine, method, path, body string) *httptest.ResponseRecorder {
	req := httptest.NewRequest(method, path, bytes.NewBufferString(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	router.ServeHTTP(rec, req)
	return rec
}

type codexThirdPartyEnvelope struct {
	Code     int               `json:"code"`
	Message  string            `json:"message"`
	Reason   string            `json:"reason"`
	Metadata map[string]string `json:"metadata"`
	Data     json.RawMessage   `json:"data"`
}

func decodeCodexThirdPartyEnvelope(t *testing.T, rec *httptest.ResponseRecorder) codexThirdPartyEnvelope {
	t.Helper()
	var envelope codexThirdPartyEnvelope
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &envelope))
	return envelope
}

// ---------------------------------------------------------------------------
// 请求解析与响应包装
// ---------------------------------------------------------------------------

func TestCodexThirdPartyModelsHandlerGetUsesStandardEnvelope(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})

	rec := codexThirdPartyRequest(router, http.MethodGet, "/api/v1/admin/codex-third-party-models/42", "")
	require.Equal(t, http.StatusOK, rec.Code)
	envelope := decodeCodexThirdPartyEnvelope(t, rec)
	require.Equal(t, 0, envelope.Code)
	require.Equal(t, "success", envelope.Message)
	require.EqualValues(t, 42, svc.lastGroupID)

	var data struct {
		GroupID int64 `json:"group_id"`
		Config  struct {
			Revision int64          `json:"revision"`
			Models   map[string]any `json:"models"`
		} `json:"config"`
		Evaluation struct {
			InputFingerprint string `json:"input_fingerprint"`
			Candidates       []any  `json:"candidates"`
		} `json:"evaluation"`
		Sources []any `json:"sources"`
	}
	require.NoError(t, json.Unmarshal(envelope.Data, &data))
	require.EqualValues(t, 42, data.GroupID)
	require.Equal(t, "fp", data.Evaluation.InputFingerprint)
	require.NotNil(t, data.Evaluation.Candidates)
	require.NotNil(t, data.Sources)
	require.NotNil(t, data.Config.Models)
}

func TestCodexThirdPartyModelsHandlerRejectsInvalidGroupID(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	for _, id := range []string{"abc", "0", "-3", "1.5"} {
		for _, call := range []struct{ method, suffix, body string }{
			{http.MethodGet, "", ""},
			{http.MethodPost, "/preview", `{"expected_revision":0,"changes":[{"slug":"a","enabled":true}]}`},
			{http.MethodPut, "", `{"expected_revision":0,"changes":[{"slug":"a","enabled":false}]}`},
			{http.MethodPost, "/verify", `{"expected_revision":0,"slug":"a","fingerprint":"f"}`},
			{http.MethodPost, "/sync", ""},
		} {
			rec := codexThirdPartyRequest(router, call.method, "/api/v1/admin/codex-third-party-models/"+id+call.suffix, call.body)
			require.Equal(t, http.StatusBadRequest, rec.Code, "%s %s%s", call.method, id, call.suffix)
		}
	}
	require.Empty(t, svc.calls, "非法分组 ID 不得进入服务层")
}

func TestCodexThirdPartyModelsHandlerPreviewParsesBody(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})

	rec := codexThirdPartyRequest(router, http.MethodPost, "/api/v1/admin/codex-third-party-models/7/preview",
		`{"expected_revision":3,"changes":[{"slug":"kimi-k3","enabled":true},{"slug":"glm-5.3","enabled":false}]}`)
	require.Equal(t, http.StatusOK, rec.Code)
	require.EqualValues(t, 7, svc.lastGroupID)
	require.EqualValues(t, 3, svc.lastPreview.ExpectedRevision)
	require.Equal(t, []service.CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}, {Slug: "glm-5.3", Enabled: false}}, svc.lastPreview.Changes)

	var data struct {
		Revision   int64 `json:"revision"`
		Evaluation struct {
			InputFingerprint string `json:"input_fingerprint"`
		} `json:"evaluation"`
	}
	require.NoError(t, json.Unmarshal(decodeCodexThirdPartyEnvelope(t, rec).Data, &data))
	require.EqualValues(t, 3, data.Revision)
	require.Equal(t, "fp-preview", data.Evaluation.InputFingerprint)
}

func TestCodexThirdPartyModelsHandlerAcceptsExpectedRevisionZero(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	rec := codexThirdPartyRequest(router, http.MethodPut, "/api/v1/admin/codex-third-party-models/7",
		`{"expected_revision":0,"changes":[{"slug":"kimi-k3","enabled":false}]}`)
	require.Equal(t, http.StatusOK, rec.Code, "revision 0 是合法值（期望配置尚不存在）")
	require.EqualValues(t, 0, svc.lastSave.ExpectedRevision)
}

func TestCodexThirdPartyModelsHandlerSavePassesFingerprint(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	rec := codexThirdPartyRequest(router, http.MethodPut, "/api/v1/admin/codex-third-party-models/7",
		`{"expected_revision":4,"input_fingerprint":"fp-xyz","changes":[{"slug":"kimi-k3","enabled":true}]}`)
	require.Equal(t, http.StatusOK, rec.Code)
	require.EqualValues(t, 4, svc.lastSave.ExpectedRevision)
	require.Equal(t, "fp-xyz", svc.lastSave.InputFingerprint)
	require.Equal(t, []service.CodexThirdPartyChange{{Slug: "kimi-k3", Enabled: true}}, svc.lastSave.Changes)

	var data struct {
		Config struct {
			Revision int64 `json:"revision"`
		} `json:"config"`
		Evaluation any `json:"evaluation"`
	}
	require.NoError(t, json.Unmarshal(decodeCodexThirdPartyEnvelope(t, rec).Data, &data))
	require.EqualValues(t, 5, data.Config.Revision)
	require.Nil(t, data.Evaluation, "纯关闭或未评估时不返回 evaluation 字段")
}

func TestCodexThirdPartyModelsHandlerVerifyPassesFields(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	rec := codexThirdPartyRequest(router, http.MethodPost, "/api/v1/admin/codex-third-party-models/7/verify",
		`{"expected_revision":2,"slug":"kimi-k3","fingerprint":"fp-kimi","note":"桌面验收通过"}`)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, service.CodexThirdPartyVerifyRequest{ExpectedRevision: 2, Slug: "kimi-k3", Fingerprint: "fp-kimi", Note: "桌面验收通过"}, svc.lastVerify)
}

func TestCodexThirdPartyModelsHandlerSyncReturnsAccepted(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	rec := codexThirdPartyRequest(router, http.MethodPost, "/api/v1/admin/codex-third-party-models/7/sync", "")
	require.Equal(t, http.StatusAccepted, rec.Code)
	var data struct {
		Sources []struct {
			AccountID  int64 `json:"account_id"`
			Refreshing bool  `json:"refreshing"`
		} `json:"sources"`
	}
	require.NoError(t, json.Unmarshal(decodeCodexThirdPartyEnvelope(t, rec).Data, &data))
	require.Len(t, data.Sources, 1)
	require.True(t, data.Sources[0].Refreshing)
}

func TestCodexThirdPartyModelsHandlerRefreshReturnsReportAndState(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	rec := codexThirdPartyRequest(router, http.MethodPost, "/api/v1/admin/codex-third-party-models/7/refresh", "")
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, int64(7), svc.lastGroupID)
	var data struct {
		Refresh struct {
			GroupID int64 `json:"group_id"`
			Reused  bool  `json:"reused"`
			Total   int   `json:"total"`
			Failed  int   `json:"failed"`
			Items   []struct {
				AccountID   int64  `json:"account_id"`
				AccountName string `json:"account_name"`
				Kind        string `json:"kind"`
				OK          bool   `json:"ok"`
				Reason      string `json:"reason"`
				ElapsedMS   int64  `json:"elapsed_ms"`
				ModelCount  int    `json:"model_count"`
			} `json:"items"`
		} `json:"refresh"`
		State struct {
			GroupID    int64 `json:"group_id"`
			Evaluation struct {
				InputFingerprint string `json:"input_fingerprint"`
			} `json:"evaluation"`
		} `json:"state"`
	}
	require.NoError(t, json.Unmarshal(decodeCodexThirdPartyEnvelope(t, rec).Data, &data))
	require.Equal(t, 2, data.Refresh.Total)
	require.Equal(t, 1, data.Refresh.Failed)
	require.False(t, data.Refresh.Reused)
	require.Len(t, data.Refresh.Items, 2)
	require.Equal(t, "model_protocol", data.Refresh.Items[0].Kind)
	require.True(t, data.Refresh.Items[0].OK)
	require.Equal(t, 40, data.Refresh.Items[0].ModelCount)
	require.Equal(t, "official_base", data.Refresh.Items[1].Kind)
	require.False(t, data.Refresh.Items[1].OK)
	require.Equal(t, "拉取超时", data.Refresh.Items[1].Reason)
	require.Equal(t, int64(7), data.State.GroupID, "刷新结果附带与读取接口同结构的分组状态")
	require.Equal(t, "fp", data.State.Evaluation.InputFingerprint)

	rec = codexThirdPartyRequest(router, http.MethodPost, "/api/v1/admin/codex-third-party-models/abc/refresh", "")
	require.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCodexThirdPartyModelsHandlerProfileAliases(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	h := &CodexThirdPartyModelsHandler{svc: svc}
	router := newCodexThirdPartyTestRouter(h)
	const path = "/api/v1/admin/codex-third-party-profile-aliases"

	rec := codexThirdPartyRequest(router, http.MethodGet, path, "")
	require.Equal(t, http.StatusOK, rec.Code)
	var data struct {
		Revision int64 `json:"revision"`
		Aliases  []struct {
			UpstreamModel string `json:"upstream_model"`
			Key           string `json:"key"`
			ProfileID     string `json:"profile_id"`
			Valid         bool   `json:"valid"`
			UpdatedBy     string `json:"updated_by"`
		} `json:"aliases"`
		Profiles []struct {
			ID             string   `json:"id"`
			UpstreamModels []string `json:"upstream_models"`
		} `json:"profiles"`
	}
	require.NoError(t, json.Unmarshal(decodeCodexThirdPartyEnvelope(t, rec).Data, &data))
	require.Equal(t, int64(2), data.Revision)
	require.Len(t, data.Aliases, 1)
	require.Equal(t, "acme-ds:prod", data.Aliases[0].UpstreamModel)
	require.True(t, data.Aliases[0].Valid)
	require.Equal(t, "deepseek-flash", data.Profiles[0].ID)
	require.Equal(t, []string{"deepseek-v4.1-flash"}, data.Profiles[0].UpstreamModels)

	rec = codexThirdPartyRequest(router, http.MethodPut, path, `{"upstream_model":"acme-ds:prod","profile_id":"deepseek-flash"}`)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, [3]string{"acme-ds:prod", "deepseek-flash", ""}, svc.lastAlias, "没有认证主体时操作人为空（路由组的认证中间件保证线上一定有）")
	for _, body := range []string{`{}`, `{"upstream_model":"x"}`, `{"profile_id":"deepseek-flash"}`, `not json`} {
		rec = codexThirdPartyRequest(router, http.MethodPut, path, body)
		require.Equal(t, http.StatusBadRequest, rec.Code, body)
	}

	rec = codexThirdPartyRequest(router, http.MethodDelete, path+"?upstream_model=acme-ds%3Aprod", "")
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, "acme-ds:prod", svc.lastAlias[0])
	rec = codexThirdPartyRequest(router, http.MethodDelete, path, "")
	require.Equal(t, http.StatusBadRequest, rec.Code)
}

func TestCodexThirdPartyModelsHandlerProfileAliasRecordsOperator(t *testing.T) {
	gin.SetMode(gin.TestMode)
	svc := &codexThirdPartyFakeService{}
	h := &CodexThirdPartyModelsHandler{svc: svc}
	router := gin.New()
	router.Use(func(c *gin.Context) {
		c.Set(string(middleware2.ContextKeyUser), middleware2.AuthSubject{UserID: 42})
		c.Next()
	})
	router.PUT("/aliases", h.SetProfileAlias)
	rec := codexThirdPartyRequest(router, http.MethodPut, "/aliases", `{"upstream_model":"m","profile_id":"p"}`)
	require.Equal(t, http.StatusOK, rec.Code)
	require.Equal(t, [3]string{"m", "p", "user:42"}, svc.lastAlias)
}

func TestCodexThirdPartyModelsHandlerProfileAliasErrors(t *testing.T) {
	cases := []struct {
		name       string
		err        error
		wantStatus int
		wantReason string
	}{
		{"请求不合法", service.ErrCodexProfileAliasInvalid, http.StatusBadRequest, "CODEX_PROFILE_ALIAS_INVALID"},
		{"档案不存在", service.ErrCodexProfileNotFound, http.StatusBadRequest, "CODEX_PROFILE_NOT_FOUND"},
		{"内置档案已认出", service.ErrCodexProfileAliasBuiltin, http.StatusConflict, "CODEX_PROFILE_ALIAS_BUILTIN"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: &codexThirdPartyFakeService{err: tc.err}})
			rec := codexThirdPartyRequest(router, http.MethodPut, "/api/v1/admin/codex-third-party-profile-aliases", `{"upstream_model":"m","profile_id":"p"}`)
			require.Equal(t, tc.wantStatus, rec.Code)
			require.Equal(t, tc.wantReason, decodeCodexThirdPartyEnvelope(t, rec).Reason)
		})
	}
	// 服务未装配：500。
	router := newCodexThirdPartyTestRouter(NewCodexThirdPartyModelsHandler(nil))
	require.Equal(t, http.StatusInternalServerError, codexThirdPartyRequest(router, http.MethodGet, "/api/v1/admin/codex-third-party-profile-aliases", "").Code)
}

func TestCodexThirdPartyModelsHandlerRejectsMalformedBodies(t *testing.T) {
	svc := &codexThirdPartyFakeService{}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	const base = "/api/v1/admin/codex-third-party-models/7"
	tests := []struct {
		name, method, path, body string
	}{
		{"预览缺 expected_revision", http.MethodPost, base + "/preview", `{"changes":[{"slug":"a","enabled":true}]}`},
		{"预览缺 changes", http.MethodPost, base + "/preview", `{"expected_revision":0}`},
		{"预览变更缺 enabled", http.MethodPost, base + "/preview", `{"expected_revision":0,"changes":[{"slug":"a"}]}`},
		{"预览变更缺 slug", http.MethodPost, base + "/preview", `{"expected_revision":0,"changes":[{"enabled":true}]}`},
		{"预览非 JSON", http.MethodPost, base + "/preview", `not json`},
		{"保存缺 expected_revision", http.MethodPut, base, `{"changes":[{"slug":"a","enabled":true}]}`},
		{"保存变更缺 enabled", http.MethodPut, base, `{"expected_revision":0,"changes":[{"slug":"a"}]}`},
		{"验收缺 slug", http.MethodPost, base + "/verify", `{"expected_revision":0,"fingerprint":"f"}`},
		{"验收缺 fingerprint", http.MethodPost, base + "/verify", `{"expected_revision":0,"slug":"a"}`},
		{"验收缺 expected_revision", http.MethodPost, base + "/verify", `{"slug":"a","fingerprint":"f"}`},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rec := codexThirdPartyRequest(router, tc.method, tc.path, tc.body)
			require.Equal(t, http.StatusBadRequest, rec.Code)
		})
	}
	require.Empty(t, svc.calls, "格式不对的请求不得进入服务层")
}

// ---------------------------------------------------------------------------
// 错误映射
// ---------------------------------------------------------------------------

func TestCodexThirdPartyModelsHandlerMapsServiceErrors(t *testing.T) {
	tests := []struct {
		name       string
		err        error
		wantStatus int
		wantReason string
	}{
		{"分组不存在", service.ErrGroupNotFound, http.StatusNotFound, "GROUP_NOT_FOUND"},
		{"平台不支持", service.ErrCodexThirdPartyGroupPlatformUnsupported, http.StatusBadRequest, "CODEX_THIRD_PARTY_GROUP_PLATFORM_UNSUPPORTED"},
		{"revision 冲突", service.ErrCodexModelVisibilityConflict, http.StatusConflict, "CODEX_MODEL_VISIBILITY_CONFLICT"},
		{"预览过期", service.ErrCodexCatalogPreviewStale, http.StatusConflict, "CODEX_CATALOG_PREVIEW_STALE"},
		{"验收指纹过期", service.ErrCodexVerificationStale, http.StatusConflict, "CODEX_MODEL_VERIFICATION_STALE"},
		{"未开启", service.ErrCodexModelNotEnabled, http.StatusConflict, "CODEX_MODEL_NOT_ENABLED"},
		{"变更不合法", service.ErrCodexThirdPartyChangesInvalid, http.StatusBadRequest, "CODEX_THIRD_PARTY_CHANGES_INVALID"},
		{"配置损坏", service.ErrCodexModelVisibilityCorrupt, http.StatusInternalServerError, "CODEX_MODEL_VISIBILITY_CORRUPT"},
		{"刷新失败", service.ErrCodexCatalogRefreshFailed, http.StatusInternalServerError, "CODEX_CATALOG_REFRESH_FAILED"},
		{"被包装的冲突", errors.Join(errors.New("save"), service.ErrCodexModelVisibilityConflict), http.StatusConflict, "CODEX_MODEL_VISIBILITY_CONFLICT"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			svc := &codexThirdPartyFakeService{err: tc.err}
			router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
			calls := []struct{ method, path, body string }{
				{http.MethodGet, "/api/v1/admin/codex-third-party-models/7", ""},
				{http.MethodPost, "/api/v1/admin/codex-third-party-models/7/preview", `{"expected_revision":0,"changes":[{"slug":"a","enabled":true}]}`},
				{http.MethodPut, "/api/v1/admin/codex-third-party-models/7", `{"expected_revision":0,"changes":[{"slug":"a","enabled":true}]}`},
				{http.MethodPost, "/api/v1/admin/codex-third-party-models/7/verify", `{"expected_revision":0,"slug":"a","fingerprint":"f"}`},
				{http.MethodPost, "/api/v1/admin/codex-third-party-models/7/sync", ""},
				{http.MethodPost, "/api/v1/admin/codex-third-party-models/7/refresh", ""},
			}
			for _, call := range calls {
				rec := codexThirdPartyRequest(router, call.method, call.path, call.body)
				require.Equal(t, tc.wantStatus, rec.Code, "%s %s", call.method, call.path)
				envelope := decodeCodexThirdPartyEnvelope(t, rec)
				require.Equal(t, tc.wantStatus, envelope.Code)
				require.Equal(t, tc.wantReason, envelope.Reason)
			}
		})
	}
}

func TestCodexThirdPartyModelsHandlerReturnsPerItemRejectionReasons(t *testing.T) {
	rejected := service.ErrCodexModelEnableRejected.WithMetadata(map[string]string{
		"model:glm-5.3": "缺少上下文窗口依据",
		"budget":        "目录体积预算检查未完成",
	})
	svc := &codexThirdPartyFakeService{err: rejected}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	rec := codexThirdPartyRequest(router, http.MethodPut, "/api/v1/admin/codex-third-party-models/7",
		`{"expected_revision":0,"input_fingerprint":"f","changes":[{"slug":"glm-5.3","enabled":true}]}`)
	require.Equal(t, http.StatusBadRequest, rec.Code)
	envelope := decodeCodexThirdPartyEnvelope(t, rec)
	require.Equal(t, "CODEX_MODEL_ENABLE_REJECTED", envelope.Reason)
	require.Equal(t, "缺少上下文窗口依据", envelope.Metadata["model:glm-5.3"])
	require.Equal(t, "目录体积预算检查未完成", envelope.Metadata["budget"])
}

func TestCodexThirdPartyModelsHandlerHidesUnexpectedErrorDetails(t *testing.T) {
	svc := &codexThirdPartyFakeService{err: errors.New("pq: password authentication failed for user sub2api, key=sk-secret")}
	router := newCodexThirdPartyTestRouter(&CodexThirdPartyModelsHandler{svc: svc})
	rec := codexThirdPartyRequest(router, http.MethodGet, "/api/v1/admin/codex-third-party-models/7", "")
	require.Equal(t, http.StatusInternalServerError, rec.Code)
	require.NotContains(t, rec.Body.String(), "sk-secret")
	require.NotContains(t, rec.Body.String(), "password authentication")
}

func TestCodexThirdPartyModelsHandlerWithoutServiceReturns500(t *testing.T) {
	router := newCodexThirdPartyTestRouter(NewCodexThirdPartyModelsHandler(nil))
	rec := codexThirdPartyRequest(router, http.MethodGet, "/api/v1/admin/codex-third-party-models/7", "")
	require.Equal(t, http.StatusInternalServerError, rec.Code)
	rec = codexThirdPartyRequest(router, http.MethodPost, "/api/v1/admin/codex-third-party-models/7/sync", "")
	require.Equal(t, http.StatusInternalServerError, rec.Code)
	rec = codexThirdPartyRequest(router, http.MethodPost, "/api/v1/admin/codex-third-party-models/7/refresh", "")
	require.Equal(t, http.StatusInternalServerError, rec.Code)
}

// ---------------------------------------------------------------------------
// 与真实服务联调：确认对外 JSON 结构，并确认响应里没有凭据
// ---------------------------------------------------------------------------

type codexE2ESettingRepo struct {
	mu     sync.Mutex
	values map[string]string
}

func (r *codexE2ESettingRepo) Get(ctx context.Context, key string) (*service.Setting, error) {
	value, err := r.GetValue(ctx, key)
	if err != nil {
		return nil, err
	}
	return &service.Setting{Key: key, Value: value}, nil
}
func (r *codexE2ESettingRepo) GetValue(_ context.Context, key string) (string, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	value, ok := r.values[key]
	if !ok {
		return "", service.ErrSettingNotFound
	}
	return value, nil
}
func (r *codexE2ESettingRepo) Set(_ context.Context, key, value string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.values[key] = value
	return nil
}
func (r *codexE2ESettingRepo) GetMultiple(context.Context, []string) (map[string]string, error) {
	return nil, errors.New("not used")
}
func (r *codexE2ESettingRepo) SetMultiple(context.Context, map[string]string) error {
	return errors.New("not used")
}
func (r *codexE2ESettingRepo) GetAll(context.Context) (map[string]string, error) {
	return nil, errors.New("not used")
}
func (r *codexE2ESettingRepo) Delete(_ context.Context, key string) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	delete(r.values, key)
	return nil
}

type codexE2EStore struct{ repo *codexE2ESettingRepo }

func (s codexE2EStore) CompareAndSwapCodexModelVisibility(ctx context.Context, groupID int64, expected int64, next service.CodexModelVisibilityConfig) (service.CodexModelVisibilityConfig, error) {
	key := service.CodexModelVisibilitySettingKey(groupID)
	raw, err := s.repo.GetValue(ctx, key)
	switch {
	case errors.Is(err, service.ErrSettingNotFound):
		if expected != 0 {
			return service.CodexModelVisibilityConfig{}, service.ErrCodexModelVisibilityConflict
		}
	case err != nil:
		return service.CodexModelVisibilityConfig{}, err
	default:
		current, parseErr := service.ParseCodexModelVisibilityConfig(raw)
		if parseErr != nil {
			return service.CodexModelVisibilityConfig{}, parseErr
		}
		if expected == 0 || current.Revision != expected {
			return service.CodexModelVisibilityConfig{}, service.ErrCodexModelVisibilityConflict
		}
	}
	written := next.Clone()
	written.SchemaVersion = service.CodexModelVisibilitySchemaVersion
	written.Revision = expected + 1
	value, err := written.Marshal()
	if err != nil {
		return service.CodexModelVisibilityConfig{}, err
	}
	_ = s.repo.Set(ctx, key, value)
	return written, nil
}

func (s codexE2EStore) DeleteCodexModelVisibility(ctx context.Context, groupID int64) error {
	return s.repo.Delete(ctx, service.CodexModelVisibilitySettingKey(groupID))
}

type codexE2EGroups struct {
	service.GroupRepository
}

func (codexE2EGroups) GetByIDLite(_ context.Context, id int64) (*service.Group, error) {
	if id != 9 {
		return nil, service.ErrGroupNotFound
	}
	return &service.Group{ID: 9, Name: "三方混合", Platform: service.PlatformComposite}, nil
}

type codexE2EAccounts struct {
	service.AccountRepository
}

func (codexE2EAccounts) ListModelAvailabilityCandidates(context.Context, *int64, []string, bool) ([]service.Account, error) {
	return nil, nil
}

type codexE2EEvaluator struct{}

func (codexE2EEvaluator) EvaluateCodexThirdPartyCatalog(_ context.Context, group *service.Group, cfg service.CodexModelVisibilityConfig) (service.CodexThirdPartyCatalogEvaluation, error) {
	return service.CodexThirdPartyCatalogEvaluation{
		GroupID: group.ID, GroupPlatform: group.Platform, Revision: cfg.Revision, InputFingerprint: "e2e-fp",
		Candidates: []service.CodexThirdPartyCandidate{
			{Slug: "kimi-k3", DisplayName: "kimi-k3", Enabled: cfg.IsEnabled("kimi-k3"), CanEnable: true, Status: service.CodexModelStatusConfigurable, Fingerprint: "fp-kimi"},
		},
		Budget: service.CodexCatalogBudget{LimitBytes: service.CodexCatalogMaxBytes, WorstCaseBytes: 1000, Complete: true},
	}, nil
}

func TestCodexThirdPartyModelsHandlerWithRealServiceEndToEnd(t *testing.T) {
	repo := &codexE2ESettingRepo{values: map[string]string{}}
	svc := service.NewCodexThirdPartyAdminService(
		service.NewSettingService(repo, nil), codexE2EGroups{}, codexE2EAccounts{}, nil,
		codexE2EStore{repo: repo}, codexE2EEvaluator{},
	)
	router := newCodexThirdPartyTestRouter(NewCodexThirdPartyModelsHandler(svc))
	const base = "/api/v1/admin/codex-third-party-models/9"

	// 1. 初始读取：配置缺失，revision 为 0，kimi-k3 可开启。
	rec := codexThirdPartyRequest(router, http.MethodGet, base, "")
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	var state struct {
		GroupName  string                   `json:"group_name"`
		Config     struct{ Revision int64 } `json:"config"`
		Evaluation struct {
			InputFingerprint string `json:"input_fingerprint"`
			Candidates       []struct {
				Slug      string `json:"slug"`
				CanEnable bool   `json:"can_enable"`
			} `json:"candidates"`
			Budget struct {
				Complete bool `json:"complete"`
			} `json:"budget"`
		} `json:"evaluation"`
		Sources []any `json:"sources"`
	}
	require.NoError(t, json.Unmarshal(decodeCodexThirdPartyEnvelope(t, rec).Data, &state))
	require.Equal(t, "三方混合", state.GroupName)
	require.EqualValues(t, 0, state.Config.Revision)
	require.Equal(t, "e2e-fp", state.Evaluation.InputFingerprint)
	require.True(t, state.Evaluation.Candidates[0].CanEnable)
	require.True(t, state.Evaluation.Budget.Complete)
	require.Empty(t, state.Sources)

	// 2. 预览 → 保存开启。
	rec = codexThirdPartyRequest(router, http.MethodPost, base+"/preview", `{"expected_revision":0,"changes":[{"slug":"kimi-k3","enabled":true}]}`)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	rec = codexThirdPartyRequest(router, http.MethodPut, base,
		`{"expected_revision":0,"input_fingerprint":"e2e-fp","changes":[{"slug":"kimi-k3","enabled":true}]}`)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	var saved struct {
		Config struct {
			Revision int64 `json:"revision"`
			Models   map[string]struct {
				Enabled bool `json:"enabled"`
			} `json:"models"`
		} `json:"config"`
	}
	require.NoError(t, json.Unmarshal(decodeCodexThirdPartyEnvelope(t, rec).Data, &saved))
	require.EqualValues(t, 1, saved.Config.Revision)
	require.True(t, saved.Config.Models["kimi-k3"].Enabled)

	// 3. 用旧 revision 再保存：409。
	rec = codexThirdPartyRequest(router, http.MethodPut, base,
		`{"expected_revision":0,"input_fingerprint":"e2e-fp","changes":[{"slug":"kimi-k3","enabled":true}]}`)
	require.Equal(t, http.StatusConflict, rec.Code)

	// 4. 标记已验证，再纯关闭。
	rec = codexThirdPartyRequest(router, http.MethodPost, base+"/verify", `{"expected_revision":1,"slug":"kimi-k3","fingerprint":"fp-kimi","note":"ok"}`)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())
	rec = codexThirdPartyRequest(router, http.MethodPut, base, `{"expected_revision":2,"changes":[{"slug":"kimi-k3","enabled":false}]}`)
	require.Equal(t, http.StatusOK, rec.Code, rec.Body.String())

	// 5. 手动同步：没有账号时返回空数组。
	rec = codexThirdPartyRequest(router, http.MethodPost, base+"/sync", "")
	require.Equal(t, http.StatusAccepted, rec.Code, rec.Body.String())
	require.True(t, strings.Contains(rec.Body.String(), `"sources":[]`), rec.Body.String())

	// 6. 不存在的分组：404。
	rec = codexThirdPartyRequest(router, http.MethodGet, "/api/v1/admin/codex-third-party-models/10", "")
	require.Equal(t, http.StatusNotFound, rec.Code)
	var envelope codexThirdPartyEnvelope
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &envelope))
	require.Equal(t, infraerrors.Reason(service.ErrGroupNotFound), envelope.Reason)
}
