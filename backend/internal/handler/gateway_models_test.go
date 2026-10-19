package handler

import (
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/pkg/claude"
	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	middleware2 "github.com/Wei-Shaw/sub2api/internal/server/middleware"
	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

type gatewayModelsAccountRepoStub struct {
	service.AccountRepository

	byGroup map[int64][]service.Account
}

type gatewayModelsRouteRepoStub struct {
	service.CompositeModelRouteRepository
	routes []service.CompositeModelRoute
	err    error
}

func (s *gatewayModelsRouteRepoStub) ListByGroup(_ context.Context, _ int64, _ bool) ([]service.CompositeModelRoute, error) {
	return s.routes, s.err
}

type gatewayModelsResponseForTest struct {
	Object string                    `json:"object"`
	Data   []gatewayModelItemForTest `json:"data"`
}

type codexModelsResponseForTest struct {
	Models []struct {
		Slug                     string                       `json:"slug"`
		SupportedReasoningLevels []codexReasoningLevelForTest `json:"supported_reasoning_levels"`
		InputModalities          []string                     `json:"input_modalities"`
		ModelMessages            map[string]json.RawMessage   `json:"model_messages"`
		TruncationPolicy         map[string]json.RawMessage   `json:"truncation_policy"`
		AvailabilityNUX          json.RawMessage              `json:"availability_nux"`
		Upgrade                  json.RawMessage              `json:"upgrade"`
	} `json:"models"`
}

type codexReasoningLevelForTest struct {
	Effort string `json:"effort"`
}

type gatewayModelItemForTest struct {
	ID                      string                                `json:"id"`
	Object                  string                                `json:"object"`
	Created                 int64                                 `json:"created"`
	OwnedBy                 string                                `json:"owned_by"`
	CreatedAt               string                                `json:"created_at"`
	SupportsReasoningEffort bool                                  `json:"supportsReasoningEffort"`
	ReasoningEffort         string                                `json:"reasoningEffort"`
	ReasoningEfforts        []gatewayReasoningEffortOptionForTest `json:"reasoningEfforts"`
}

type gatewayReasoningEffortOptionForTest struct {
	Value   string `json:"value"`
	Label   string `json:"label"`
	Default bool   `json:"default"`
}

func (s *gatewayModelsAccountRepoStub) ListSchedulableByGroupID(ctx context.Context, groupID int64) ([]service.Account, error) {
	accounts, ok := s.byGroup[groupID]
	if !ok {
		return nil, nil
	}
	out := make([]service.Account, len(accounts))
	copy(out, accounts)
	return out, nil
}

func (s *gatewayModelsAccountRepoStub) ListByGroup(ctx context.Context, groupID int64) ([]service.Account, error) {
	return s.ListSchedulableByGroupID(ctx, groupID)
}

func (s *gatewayModelsAccountRepoStub) ListModelAvailabilityCandidates(ctx context.Context, groupID *int64, _ []string, _ bool) ([]service.Account, error) {
	if groupID == nil {
		return nil, nil
	}
	return s.ListSchedulableByGroupID(ctx, *groupID)
}

func newGatewayModelsHandlerForTest(repo service.AccountRepository, routeRepo ...service.CompositeModelRouteRepository) *GatewayHandler {
	var resolver *service.CompositeRouteResolver
	if len(routeRepo) > 0 {
		resolver = service.NewCompositeRouteResolver(routeRepo[0])
	}
	return &GatewayHandler{
		gatewayService: service.NewGatewayService(
			repo,
			nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil,
			nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, nil, resolver, nil, nil,
		),
	}
}

func TestGatewayCompositeRouteModelsAppearInBothCatalogs(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 7784
	accounts := &gatewayModelsAccountRepoStub{byGroup: map[int64][]service.Account{
		groupID: {{ID: 1, Platform: service.PlatformOpenAI, Credentials: map[string]any{
			"model_mapping": map[string]any{"mapped-model": "upstream"},
		}}},
	}}
	routes := &gatewayModelsRouteRepoStub{routes: []service.CompositeModelRoute{
		{PublicModel: "route-only", MatchType: service.CompositeRouteMatchExact, Enabled: true},
		{PublicModel: "route-only", MatchType: service.CompositeRouteMatchExact, Enabled: true},
		{PublicModel: "mapped-model", MatchType: service.CompositeRouteMatchExact, Enabled: true},
		{PublicModel: "image-only", MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointImages, Enabled: true},
		{PublicModel: "prefix-", MatchType: service.CompositeRouteMatchPrefix, Enabled: true},
		{PublicModel: "disabled", MatchType: service.CompositeRouteMatchExact, Enabled: false},
	}}
	for _, tc := range []struct {
		name      string
		repo      service.CompositeModelRouteRepository
		allowlist []string
		want      []string
	}{
		{"routes", routes, nil, []string{"mapped-model", "route-only", "image-only"}},
		{"allowlist", routes, []string{"route-only", "disabled", "unknown"}, []string{"route-only"}},
		{"lookup failure", &gatewayModelsRouteRepoStub{err: errors.New("unavailable")}, nil, []string{"mapped-model"}},
		{"allowlisted lookup failure", &gatewayModelsRouteRepoStub{err: errors.New("unavailable")}, []string{"mapped-model", "unknown"}, []string{"mapped-model"}},
		{"no route repo", nil, nil, []string{"mapped-model"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := newGatewayModelsHandlerForTest(accounts, tc.repo)
			group := &service.Group{ID: groupID, Platform: service.PlatformComposite}
			if tc.allowlist != nil {
				group.ModelAllowlist = service.GroupModelAllowlist{Enabled: true, Models: tc.allowlist}
			}
			for _, endpoint := range []string{"models", "codex"} {
				rec := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(rec)
				c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
				c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{Group: group})
				if endpoint == "models" {
					h.Models(c)
					var got gatewayModelsResponseForTest
					require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
					require.Equal(t, tc.want, modelIDsForTest(got.Data))
				} else {
					h.CodexModels(c)
					var got codexModelsResponseForTest
					require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
					want := tc.want
					if tc.name == "routes" {
						want = []string{"mapped-model", "route-only"}
					}
					require.ElementsMatch(t, want, codexModelSlugsForTest(got.Models))
				}
				require.Equal(t, http.StatusOK, rec.Code)
			}
		})
	}
}

func TestDefaultModelIDsForCompositeIncludesAntigravityDefaults(t *testing.T) {
	antigravityIDs := defaultModelIDsForPlatform(service.PlatformAntigravity)
	require.NotEmpty(t, antigravityIDs)

	compositeIDs := defaultModelIDsForPlatform(service.PlatformComposite)
	require.Contains(t, compositeIDs, antigravityIDs[0])
}

// Scenario: Anthropic defaults contain only Claude while Antigravity keeps its own Gemini models.
func TestDefaultModelIDsForAnthropicExcludeAntigravityGemini(t *testing.T) {
	anthropicIDs := defaultModelIDsForPlatform(service.PlatformAnthropic)
	require.Contains(t, anthropicIDs, "claude-opus-4-6")
	require.NotContains(t, anthropicIDs, "gemini-2.5-flash")

	antigravityIDs := defaultModelIDsForPlatform(service.PlatformAntigravity)
	require.Contains(t, antigravityIDs, "gemini-3-flash-agent")
}

// Scenario: non-OpenAI groups return a Codex manifest instead of a standard model list.
func TestGatewayCodexModels_NonOpenAIGroupsUseMappedModels(t *testing.T) {
	tests := []struct {
		name       string
		platform   string
		model      string
		efforts    []string
		modalities []string
	}{
		{
			name:       "Grok",
			platform:   service.PlatformGrok,
			model:      "grok-4.6",
			efforts:    []string{"low", "medium", "high", "xhigh"},
			modalities: []string{"text", "image"},
		},
		{
			name:       "DeepSeek",
			platform:   service.PlatformDeepseek,
			model:      "deepseek-v4-pro",
			efforts:    []string{"low", "high", "max"},
			modalities: []string{"text"},
		},
		{
			name:       "provider-qualified Claude",
			platform:   service.PlatformAnthropic,
			model:      "anthropic/claude-sonnet-4-6",
			efforts:    []string{"low", "medium", "high", "max"},
			modalities: []string{"text"},
		},
	}

	for index, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			gin.SetMode(gin.TestMode)
			groupID := int64(100 + index)
			h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
				byGroup: map[int64][]service.Account{
					groupID: {
						{
							ID:       1,
							Platform: tt.platform,
							Credentials: map[string]any{
								"model_mapping": map[string]any{tt.model: tt.model},
							},
						},
					},
				},
			})

			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.147.0", nil)
			c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
				Group: &service.Group{ID: groupID, Platform: tt.platform},
			})

			h.CodexModels(c)

			require.Equal(t, http.StatusOK, rec.Code)
			var got codexModelsResponseForTest
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
			require.Len(t, got.Models, 1)
			require.Equal(t, tt.model, got.Models[0].Slug)
			require.NotEmpty(t, got.Models[0].ModelMessages)
			require.NotEmpty(t, got.Models[0].TruncationPolicy)
			require.NotNil(t, got.Models[0].AvailabilityNUX)
			require.NotNil(t, got.Models[0].Upgrade)
			require.Equal(t, tt.efforts, codexReasoningEffortsForTest(got.Models[0].SupportedReasoningLevels))
			require.Equal(t, tt.modalities, got.Models[0].InputModalities)
		})
	}
}

// Composite manifests include defaults from unmapped accounts and explicit mappings.
func TestGatewayCodexModels_CompositeUsesCompleteEffectiveModelList(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 120
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {
				{
					ID:          3,
					Platform:    service.PlatformOpenAI,
					Status:      service.StatusActive,
					Schedulable: true,
					Credentials: map[string]any{},
				},
				{
					ID:       1,
					Platform: service.PlatformOpenAI,
					Credentials: map[string]any{
						"model_mapping": map[string]any{"gpt-5.5": "gpt-5.5"},
					},
				},
				{
					ID:       2,
					Platform: service.PlatformGrok,
					Credentials: map[string]any{
						"model_mapping": map[string]any{"grok-4.6": "grok-4.6"},
					},
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.147.0", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformComposite},
	})

	h.CodexModels(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got codexModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	want := service.FilterCodexModelIDsForGroup(openai.DefaultModelIDs(), nil)
	want = append(want, "grok-4.6")
	require.ElementsMatch(t, want, codexModelSlugsForTest(got.Models))
}

func TestGatewayModels_UnmappedOpenAIAccountsSupplementMappedModels(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 28
	const sparkModel = "gpt-5.3-codex-spark"
	const alias = "team-coder"
	parentID := int64(1)
	accounts := []service.Account{
		{ID: parentID, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth},
		{
			ID: 2, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth,
			ParentAccountID: &parentID, QuotaDimension: "spark",
			Credentials: map[string]any{"model_mapping": map[string]any{sparkModel: sparkModel}},
		},
		{
			ID: 3, Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey,
			Credentials: map[string]any{"model_mapping": map[string]any{alias: "gpt-5.6-sol"}},
		},
	}
	subscriptionDefaults := make([]string, 0)
	for _, model := range openai.DefaultModelIDs() {
		if model != "gpt-5.5" {
			subscriptionDefaults = append(subscriptionDefaults, model)
		}
	}
	tests := []struct {
		name     string
		accounts []service.Account
		config   service.GroupModelAllowlist
		want     []string
	}{
		{
			name:     "unmapped parent and Spark shadow retain defaults and aliases",
			accounts: accounts,
			want:     append(append([]string{}, subscriptionDefaults...), alias),
		},
		{
			name:     "unmapped API key account also contributes defaults",
			accounts: append([]service.Account{{ID: 4, Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey}}, accounts[1:]...),
			want:     append(openai.DefaultModelIDs(), alias),
		},
		{
			name:     "unmapped accounts alone retain default response shape",
			accounts: accounts[:1],
			want:     subscriptionDefaults,
		},
		{
			name:     "custom list can select defaults and aliases",
			accounts: accounts,
			config:   service.GroupModelAllowlist{Enabled: true, Models: []string{alias, "gpt-5.6-sol", sparkModel, "unknown-model"}},
			want:     []string{alias, "gpt-5.6-sol", sparkModel},
		},
		{
			name:     "unavailable custom selection remains empty",
			accounts: accounts,
			config:   service.GroupModelAllowlist{Enabled: true, Models: []string{"unknown-model"}},
			want:     []string{},
		},
		{
			name:     "mapped accounts alone do not gain defaults",
			accounts: accounts[1:],
			want:     []string{sparkModel, alias},
		},
		{
			// A passthrough account with a stale mapping behaves like an unmapped
			// one: it adds the defaults but never its own mapping keys, and it no
			// longer hides the aliases declared on ordinary accounts.
			name: "passthrough account contributes defaults without hiding mapped aliases",
			accounts: append([]service.Account{{
				ID: 5, Platform: service.PlatformOpenAI, Type: service.AccountTypeOAuth,
				Credentials: map[string]any{"model_mapping": map[string]any{"stale-model": "stale-model"}},
				Extra:       map[string]any{"openai_passthrough": true},
			}}, accounts[1:]...),
			want: append(append([]string{}, subscriptionDefaults...), alias),
		},
		{
			name:     "unmapped accounts from another platform do not add defaults",
			accounts: append([]service.Account{{ID: 4, Platform: service.PlatformAnthropic}}, accounts[1:]...),
			want:     []string{sparkModel, alias},
		},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
				byGroup: map[int64][]service.Account{groupID: tt.accounts},
			})
			for range 2 {
				rec := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(rec)
				c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
				c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
					Group: &service.Group{ID: groupID, Platform: service.PlatformOpenAI, ModelAllowlist: tt.config},
				})
				h.Models(c)
				require.Equal(t, http.StatusOK, rec.Code)
				var got gatewayModelsResponseForTest
				require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
				require.Equal(t, "list", got.Object)
				require.ElementsMatch(t, tt.want, modelIDsForTest(got.Data))
				for _, model := range got.Data {
					require.Equal(t, "model", model.Object, model.ID)
					require.Positive(t, model.Created, model.ID)
					require.Equal(t, "openai", model.OwnedBy, model.ID)
					require.Empty(t, model.CreatedAt, model.ID)
				}
				if tt.config.Enabled {
					require.Equal(t, tt.want, modelIDsForTest(got.Data))
				}
			}
		})
	}
	require.Empty(t, accounts[0].GetModelMapping())
	require.True(t, accounts[0].IsModelSupported("gpt-future-model"))
	require.False(t, accounts[1].IsModelSupported("gpt-5.6-sol"))
}

func TestGatewayCodexModels_GeneratedManifestUsesFinalBodyETag(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 122
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {{
				ID:       1,
				Platform: service.PlatformDeepseek,
				Credentials: map[string]any{
					"model_mapping": map[string]any{"deepseek-v4-pro": "deepseek-v4-pro"},
				},
			}},
		},
	})
	group := &service.Group{ID: groupID, Platform: service.PlatformDeepseek}

	first := httptest.NewRecorder()
	firstContext, _ := gin.CreateTestContext(first)
	firstContext.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.147.0", nil)
	firstContext.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{Group: group})
	h.CodexModels(firstContext)

	require.Equal(t, http.StatusOK, first.Code)
	etag := first.Header().Get("ETag")
	require.NotEmpty(t, etag)
	require.Equal(t, service.CodexModelsManifestETag(first.Body.Bytes()), etag)

	second := httptest.NewRecorder()
	secondContext, _ := gin.CreateTestContext(second)
	secondContext.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.147.0", nil)
	secondContext.Request.Header.Set("If-None-Match", "W/"+etag)
	secondContext.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{Group: group})
	h.CodexModels(secondContext)

	require.Equal(t, http.StatusNotModified, second.Code)
	require.Empty(t, second.Body.Bytes())
	require.Equal(t, etag, second.Header().Get("ETag"))
}

// Scenario: group model_allowlist limits the generated Codex manifest.
func TestGatewayCodexModels_CustomModelsListFiltersCompositeManifest(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 121
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {
				{
					ID:       1,
					Platform: service.PlatformOpenAI,
					Credentials: map[string]any{
						"model_mapping": map[string]any{"gpt-5.5": "gpt-5.5"},
					},
				},
				{
					ID:       2,
					Platform: service.PlatformGrok,
					Credentials: map[string]any{
						"model_mapping": map[string]any{"grok-4.6": "grok-4.6"},
					},
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.147.0", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformComposite,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"grok-4.6"},
			},
		},
	})

	h.CodexModels(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got codexModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"grok-4.6"}, codexModelSlugsForTest(got.Models))
}

func codexModelSlugsForTest(models []struct {
	Slug                     string                       `json:"slug"`
	SupportedReasoningLevels []codexReasoningLevelForTest `json:"supported_reasoning_levels"`
	InputModalities          []string                     `json:"input_modalities"`
	ModelMessages            map[string]json.RawMessage   `json:"model_messages"`
	TruncationPolicy         map[string]json.RawMessage   `json:"truncation_policy"`
	AvailabilityNUX          json.RawMessage              `json:"availability_nux"`
	Upgrade                  json.RawMessage              `json:"upgrade"`
}) []string {
	slugs := make([]string, 0, len(models))
	for _, model := range models {
		slugs = append(slugs, model.Slug)
	}
	return slugs
}

func codexReasoningEffortsForTest(levels []codexReasoningLevelForTest) []string {
	efforts := make([]string, 0, len(levels))
	for _, level := range levels {
		efforts = append(efforts, level.Effort)
	}
	return efforts
}

func TestGatewayModels_GeminiGroupFallsBackToGeminiModels(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(20)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{ID: 1, Platform: service.PlatformGemini},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformGemini},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, "list", got.Object)
	require.Contains(t, modelIDsForTest(got.Data), "gemini-2.5-flash")
	require.NotContains(t, modelIDsForTest(got.Data), "claude-sonnet-4-6")
}

func TestGatewayModels_Grok45AdvertisesReasoningEffortForGrokBuild(t *testing.T) {
	assertGrokGatewayReasoningEfforts(t, 4409, "grok-4.5", []gatewayReasoningEffortOptionForTest{
		{Value: "low", Label: "Low"},
		{Value: "medium", Label: "Medium"},
		{Value: "high", Label: "High", Default: true},
	})
}

func TestGatewayModels_Grok46AdvertisesXHighReasoningEffortForGrokBuild(t *testing.T) {
	xhighEfforts := []gatewayReasoningEffortOptionForTest{
		{Value: "low", Label: "Low"},
		{Value: "medium", Label: "Medium"},
		{Value: "high", Label: "High", Default: true},
		{Value: "xhigh", Label: "xHigh"},
	}
	tests := []struct {
		groupID int64
		model   string
	}{
		{groupID: 4410, model: "grok-4.6"},
		{groupID: 4411, model: "grok-4.6-latest"},
	}
	for _, tt := range tests {
		t.Run(tt.model, func(t *testing.T) {
			assertGrokGatewayReasoningEfforts(t, tt.groupID, tt.model, xhighEfforts)
		})
	}
}

func assertGrokGatewayReasoningEfforts(t *testing.T, groupID int64, modelID string, want []gatewayReasoningEffortOptionForTest) {
	t.Helper()
	gin.SetMode(gin.TestMode)

	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformGrok,
						Credentials: map[string]any{
							"model_mapping": map[string]any{modelID: modelID},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformGrok},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Len(t, got.Data, 1)
	model := got.Data[0]
	require.Equal(t, modelID, model.ID)
	require.True(t, model.SupportsReasoningEffort)
	require.Equal(t, "high", model.ReasoningEffort)
	require.Equal(t, want, model.ReasoningEfforts)
}

func TestGatewayModels_GeminiGroupFiltersMappedModelsByPlatform(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(21)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"claude-sonnet-4-6": "claude-sonnet-4-6",
							},
						},
					},
					{
						ID:       2,
						Platform: service.PlatformGemini,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gemini-2.5-flash": "gemini-2.5-flash",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformGemini},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"gemini-2.5-flash"}, modelIDsForTest(got.Data))
}

// Scenario: a Composite Codex catalog must not fall back across the OpenAI protocol boundary.
func TestGatewayCodexModels_CompositeAnthropicDoesNotAdvertiseAntigravityDefaults(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(64)
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {{ID: 1, Platform: service.PlatformAnthropic}},
		},
	})

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.147.0", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformComposite},
	})

	h.CodexModels(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got codexModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	slugs := codexModelSlugsForTest(got.Models)
	require.Empty(t, slugs)
	require.NotContains(t, slugs, "gemini-2.5-flash")
}

// Scenario: Antigravity is not an OpenAI-protocol source for Composite /v1/models.
func TestGatewayModels_CompositeAntigravityAdvertisesAntigravityDefaults(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(65)
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {{ID: 1, Platform: service.PlatformAntigravity}},
		},
	})

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformComposite},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	ids := modelIDsForTest(got.Data)
	require.Empty(t, ids)
}

func TestGatewayModels_CustomModelsListDisabledKeepsOriginalModels(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(22)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformOpenAI,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gpt-5.5": "gpt-5.5",
								"gpt-5.4": "gpt-5.4",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformOpenAI,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: false,
				Models:  []string{"gpt-5.5"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"gpt-5.4", "gpt-5.5"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_CustomModelsListFiltersAndOrdersMappedModels(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(23)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformOpenAI,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gpt-5.4":         "gpt-5.4",
								"gpt-5.5":         "gpt-5.5",
								"legacy-gpt-2024": "legacy-gpt-2024",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformOpenAI,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"gpt-5.5", "missing-model", "gpt-5.4"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"gpt-5.5", "gpt-5.4"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_CompositeCustomModelsListFiltersAcrossOpenAIProtocolAccounts(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(33)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformOpenAI,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gpt-5.4": "gpt-5.4",
								"gpt-5.5": "gpt-5.5",
							},
						},
					},
					{
						ID:       2,
						Platform: service.PlatformGemini,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gemini-2.5-flash": "gemini-2.5-flash",
							},
						},
					},
					{
						ID:       3,
						Platform: service.PlatformAntigravity,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"ag-custom-model": "ag-custom-model",
							},
						},
					},
					{
						ID:       4,
						Platform: service.PlatformKimi,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"kimi-custom": "kimi-upstream"},
						},
					},
					{
						ID:       5,
						Platform: service.PlatformZhipu,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"glm-custom": "glm-upstream"},
						},
					},
					{
						ID:       6,
						Platform: service.PlatformDeepseek,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"deepseek-custom": "deepseek-upstream"},
						},
					},
					{
						ID:       7,
						Platform: service.PlatformAnthropic,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"claude-custom": "claude-upstream"},
						},
					},
					{
						ID:       8,
						Platform: service.PlatformMiniMax,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"minimax-custom": "MiniMax-M3"},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformComposite,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"gemini-2.5-flash", "missing-model", "ag-custom-model", "claude-custom", "gpt-5.5", "kimi-custom", "glm-custom", "deepseek-custom", "minimax-custom"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"gpt-5.5", "kimi-custom", "glm-custom", "deepseek-custom", "minimax-custom"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_CompositeFiltersModelsByActualAccountProtocol(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(3301)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"claude-native": "claude-native"},
						},
					},
					{
						ID:       2,
						Platform: service.PlatformOpenAI,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"gpt-native": "gpt-native"},
						},
					},
					{
						ID:       3,
						Platform: service.PlatformZhipu,
						Credentials: map[string]any{
							"api_protocol":  service.APIProtocolAnthropic,
							"model_mapping": map[string]any{"glm-anthropic": "glm-anthropic"},
						},
					},
					{
						ID:       4,
						Platform: service.PlatformZhipu,
						Credentials: map[string]any{
							"api_protocol":  service.APIProtocolChatCompletions,
							"model_mapping": map[string]any{"glm-openai": "glm-openai"},
						},
					},
					{
						ID:       5,
						Platform: service.PlatformDeepseek,
						Credentials: map[string]any{
							"api_protocol":  service.APIProtocolResponses,
							"model_mapping": map[string]any{"deepseek-responses": "deepseek-responses"},
						},
					},
					{
						ID:       6,
						Platform: service.PlatformKimi,
						Credentials: map[string]any{
							"api_protocol":  service.APIProtocolAdaptive,
							"model_mapping": map[string]any{"kimi-adaptive": "kimi-adaptive"},
						},
					},
					{
						ID:       7,
						Platform: service.PlatformGemini,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"gemini-native": "gemini-native"},
						},
					},
				},
			},
		},
	)

	requestModels := func(t *testing.T, anthropic bool) []string {
		t.Helper()
		rec := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(rec)
		c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
		if anthropic {
			c.Request.Header.Set("Anthropic-Version", "2023-06-01")
		}
		c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
			Group: &service.Group{ID: groupID, Platform: service.PlatformComposite},
		})

		h.Models(c)

		require.Equal(t, http.StatusOK, rec.Code)
		var got gatewayModelsResponseForTest
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
		return modelIDsForTest(got.Data)
	}

	t.Run("OpenAI 目录", func(t *testing.T) {
		require.Equal(t, []string{"gpt-native", "kimi-adaptive", "glm-openai", "deepseek-responses"}, requestModels(t, false))
	})

	t.Run("Anthropic 目录", func(t *testing.T) {
		require.Equal(t, []string{"claude-native", "kimi-adaptive", "glm-anthropic"}, requestModels(t, true))
	})
}

func TestGatewayModels_CompositeDoesNotFallbackAcrossProtocols(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(3302)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Credentials: map[string]any{
							"model_mapping": map[string]any{"claude-only": "claude-only"},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformComposite,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"claude-only"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Empty(t, modelIDsForTest(got.Data))
}

func TestCompositeModelListProtocolForRequest(t *testing.T) {
	gin.SetMode(gin.TestMode)

	testCases := []struct {
		name     string
		headers  map[string]string
		protocol string
	}{
		{name: "普通请求使用 OpenAI 目录", protocol: service.CompositeModelListProtocolOpenAI},
		{
			name:     "Anthropic-Version 使用 Anthropic 目录",
			headers:  map[string]string{"Anthropic-Version": "2023-06-01"},
			protocol: service.CompositeModelListProtocolAnthropic,
		},
		{
			name:     "Claude CLI 使用 Anthropic 目录",
			headers:  map[string]string{"User-Agent": "claude-cli/1.0.83 (external, cli)"},
			protocol: service.CompositeModelListProtocolAnthropic,
		},
	}

	for _, testCase := range testCases {
		t.Run(testCase.name, func(t *testing.T) {
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
			for name, value := range testCase.headers {
				c.Request.Header.Set(name, value)
			}
			require.Equal(t, testCase.protocol, compositeModelListProtocolForRequest(c))
		})
	}
}

func TestGatewayModels_CompositeUnmappedAccountsFallbackToLinkedPlatformsOnly(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(34)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{ID: 1, Platform: service.PlatformOpenAI},
					{ID: 2, Platform: service.PlatformGrok},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformComposite},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))

	ids := modelIDsForTest(got.Data)
	require.Contains(t, ids, "gpt-5.5")
	require.Contains(t, ids, "grok-4.3")
	require.NotContains(t, ids, "claude-sonnet-4-6")
	require.NotContains(t, ids, "gemini-2.5-flash")
}

// CN 供应商没有静态默认模型列表：composite 下无映射的可调度 CN 账号不得把
// defaultModelIDsForPlatform default 分支的 Claude 列表挂到 CN 平台名下。
func TestGatewayModels_CompositeUnmappedCNAccountsContributeNoDefaults(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(35)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{ID: 1, Platform: service.PlatformOpenAI},
					{ID: 2, Platform: service.PlatformKimi},
					{ID: 3, Platform: service.PlatformZhipu},
					{ID: 4, Platform: service.PlatformDeepseek},
					{ID: 5, Platform: service.PlatformMiniMax},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformComposite},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))

	ids := modelIDsForTest(got.Data)
	require.Contains(t, ids, "gpt-5.5")
	require.NotContains(t, ids, "claude-sonnet-4-6")
}

// 独立 CN 分组沿用 default 分支的 Claude 默认列表（Claude Code 客户端请求的
// 就是这些模型名并经账号 model_mapping 转换），composite 支持不得改变该回退。
func TestDefaultModelIDsForPlatform_CNProvidersKeepClaudeDefaults(t *testing.T) {
	want := make([]string, 0, len(claude.DefaultModels))
	for _, model := range claude.DefaultModels {
		want = append(want, model.ID)
	}
	for _, platform := range []string{service.PlatformKimi, service.PlatformZhipu, service.PlatformDeepseek, service.PlatformMiniMax} {
		require.Equal(t, want, defaultModelIDsForPlatform(platform), "platform=%s", platform)
	}
}

func TestDefaultCodexModelIDsForPlatform_DeepSeekUsesDeepSeekModels(t *testing.T) {
	require.Equal(t, []string{"deepseek-v4-pro", "deepseek-v4-flash", "deepseek-flash"}, defaultCodexModelIDsForPlatform(service.PlatformDeepseek))
	require.Equal(t, []string{"MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"}, defaultCodexModelIDsForPlatform(service.PlatformMiniMax))
	require.Equal(t, defaultModelIDsForPlatform(service.PlatformAnthropic), defaultCodexModelIDsForPlatform(service.PlatformAnthropic))
}

func TestDefaultModelIDsForPlatform_TypeSafeUsesJev(t *testing.T) {
	require.Equal(t, []string{"jev-latest"}, defaultModelIDsForPlatform(service.PlatformTypeSafe))
}

func TestGatewayCodexModels_DeepSeekWithoutMappingUsesDeepSeekDefaults(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 130
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {
				{
					ID:          1,
					Platform:    service.PlatformDeepseek,
					Status:      service.StatusActive,
					Schedulable: true,
					Credentials: map[string]any{},
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.150.0", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformDeepseek},
	})

	h.CodexModels(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got codexModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	slugs := make([]string, 0, len(got.Models))
	for _, model := range got.Models {
		slugs = append(slugs, model.Slug)
	}
	require.Contains(t, slugs, "deepseek-v4-pro")
	require.Contains(t, slugs, "deepseek-v4-flash")
	require.NotContains(t, slugs, "claude-sonnet-4-6")
	require.NotContains(t, slugs, "claude-opus-4-6")
}

func TestGatewayCodexModels_OmitsWildcardMappingKeys(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 131
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {
				{
					ID:       1,
					Platform: service.PlatformDeepseek,
					Credentials: map[string]any{
						"model_mapping": map[string]any{
							"foo-*":           "deepseek-v4-pro",
							"deepseek-v4-pro": "deepseek-v4-pro",
						},
					},
				},
			},
		},
	})

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.150.0", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformDeepseek},
	})

	h.CodexModels(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got codexModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	slugs := make([]string, 0, len(got.Models))
	for _, model := range got.Models {
		slugs = append(slugs, model.Slug)
	}
	require.Equal(t, []string{"deepseek-v4-pro"}, slugs)
}

func TestGatewayModels_CustomModelsListKeepsConcreteModelAllowedByWildcardMapping(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(26)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"claude-*": "claude-sonnet-4-6",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformAnthropic,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"claude-sonnet-4-6"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"claude-sonnet-4-6"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_AnthropicCustomModelsListIncludesOAuthClaudeAndMappedDeepSeek(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(28)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Type:     service.AccountTypeOAuth,
					},
					{
						ID:       2,
						Platform: service.PlatformAnthropic,
						Type:     service.AccountTypeAPIKey,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"deepseek-v4-pro": "deepseek-v4-pro",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformAnthropic,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"claude-fable-5", "claude-opus-4-8", "deepseek-v4-pro"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"claude-fable-5", "claude-opus-4-8", "deepseek-v4-pro"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_AnthropicCustomModelsListDisabledKeepsMappedModelList(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(29)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Type:     service.AccountTypeOAuth,
					},
					{
						ID:       2,
						Platform: service.PlatformAnthropic,
						Type:     service.AccountTypeAPIKey,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"deepseek-v4-pro": "deepseek-v4-pro",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformAnthropic,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: false,
				Models:  []string{"claude-fable-5", "deepseek-v4-pro"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"deepseek-v4-pro"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_AnthropicCustomModelsListIncludesOAuthClaudeWithoutMappings(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(30)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAnthropic,
						Type:     service.AccountTypeOAuth,
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformAnthropic,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"claude-opus-4-6-thinking", "claude-sonnet-4-5"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"claude-opus-4-6-thinking", "claude-sonnet-4-5"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_CustomModelsListCanReturnEmptyWhenSelectionsUnavailable(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(24)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformOpenAI,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gpt-5.4": "gpt-5.4",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformOpenAI,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"gpt-5.5"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Empty(t, modelIDsForTest(got.Data))
}

func TestGatewayModels_CustomModelsListFiltersDefaultFallbackModels(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(25)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{ID: 1, Platform: service.PlatformOpenAI},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformOpenAI,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"gpt-5.5", "legacy-gpt-2024", "gpt-5.4"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"gpt-5.5", "gpt-5.4"}, modelIDsForTest(got.Data))
}

func TestGatewayModels_OpenAICustomModelsListKeepsOpenAIResponseShapeForDefaultFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(27)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{ID: 1, Platform: service.PlatformOpenAI},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{
			ID:       groupID,
			Platform: service.PlatformOpenAI,
			ModelAllowlist: service.GroupModelAllowlist{
				Enabled: true,
				Models:  []string{"gpt-5.5", "gpt-5.4"},
			},
		},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	require.Equal(t, []string{"gpt-5.5", "gpt-5.4"}, modelIDsForTest(got.Data))
	require.Equal(t, "model", got.Data[0].Object)
	require.NotZero(t, got.Data[0].Created)
	require.Equal(t, "openai", got.Data[0].OwnedBy)
	require.Empty(t, got.Data[0].CreatedAt)
}

func modelIDsForTest(models []gatewayModelItemForTest) []string {
	ids := make([]string, 0, len(models))
	for _, model := range models {
		ids = append(ids, model.ID)
	}
	return ids
}

func TestGatewayModels_AntigravityExplicitMappingAppearsInBothCatalogs(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 93
	mapping := map[string]any{
		"gemini-3.8-flash":         "gemini-3.8-flash-high",
		"gemini-3.8-flash-high":    "gemini-3.8-flash-high",
		"claude-opus-4-6-thinking": "claude-opus-4-6-thinking",
		"gemini-pro-agent":         "gemini-pro-agent",
	}
	for _, platform := range []string{service.PlatformAntigravity, service.PlatformComposite, service.PlatformGemini} {
		t.Run(platform, func(t *testing.T) {
			h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
				byGroup: map[int64][]service.Account{groupID: {{
					ID: 25, Platform: service.PlatformAntigravity,
					Extra:       map[string]any{"mixed_scheduling": true},
					Credentials: map[string]any{"model_mapping": mapping},
				}}},
			})
			// 本分支口径：显式映射只公布自映射条目；仅作请求别名的 gemini-3.8-flash 可路由但不出现在目录中。
			want := []string{"gemini-3.8-flash-high", "gemini-pro-agent", "claude-opus-4-6-thinking"}
			if platform == service.PlatformGemini {
				want = want[:2]
			}
			for _, codex := range []bool{false, true} {
				rec := httptest.NewRecorder()
				c, _ := gin.CreateTestContext(rec)
				c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
				c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
					Group: &service.Group{ID: groupID, Platform: platform},
				})
				var ids []string
				if codex {
					h.CodexModels(c)
					var got codexModelsResponseForTest
					require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
					for _, model := range got.Models {
						ids = append(ids, model.Slug)
					}
				} else {
					h.Models(c)
					var got gatewayModelsResponseForTest
					require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
					ids = modelIDsForTest(got.Data)
				}
				require.Equal(t, http.StatusOK, rec.Code)
				if platform == service.PlatformComposite {
					// 本分支：Antigravity 不是 Composite /v1/models 与 Codex 目录的协议来源
					// （见 TestGatewayModels_CompositeAntigravityAdvertisesAntigravityDefaults），不要求其显式条目出现。
					require.NotContains(t, ids, "gemini-pro-agent", "codex=%t", codex)
					continue
				}
				// 显式条目必须同时出现在两种目录；默认透传补齐的条目不锁定。
				require.Subset(t, ids, want, "codex=%t", codex)
				require.NotContains(t, ids, "gemini-3.8-flash", "codex=%t", codex)
				if platform == service.PlatformGemini {
					require.NotContains(t, ids, "claude-opus-4-6-thinking", "codex=%t", codex)
				}
			}
		})
	}
}

// gemini 分组混合调度：组内 antigravity 账号的 gemini-* 映射也应出现在 /v1/models，
// 与路由层（listSchedulableAccountsOnce 的 useMixedScheduling）同源；claude-* 不带入，
// 非混合调度的异平台账号（anthropic）仍被过滤。
func TestGatewayModels_GeminiGroupIncludesAntigravityGeminiMappings(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(22)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAntigravity,
						Extra:    map[string]any{"mixed_scheduling": true},
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gemini-3.8-flash-high": "gemini-3.8-flash-high",
								"gemini-synced-custom":  "gemini-3.8-flash-high",
								"gemini-3.8-flash-low":  "gemini-3.8-flash-low",
								"claude-sonnet-4-6":     "claude-sonnet-4-6",
							},
						},
					},
					{ID: 3, Platform: service.PlatformAntigravity,
						Extra:       map[string]any{"mixed_scheduling": false},
						Credentials: map[string]any{"model_mapping": map[string]any{"gemini-disabled-only": "gemini-3.8-flash-high"}}},
					{ID: 4, Platform: service.PlatformAntigravity,
						Credentials: map[string]any{"model_mapping": map[string]any{"gemini-unset-only": "gemini-3.8-flash-high"}}},
					{ID: 5, Platform: service.PlatformGemini,
						Credentials: map[string]any{"model_mapping": map[string]any{"gemini-native-only": "gemini-2.5-pro"}}},
					{
						ID:       2,
						Platform: service.PlatformAnthropic,
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"claude-opus-4-6": "claude-opus-4-6",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformGemini},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	ids := modelIDsForTest(got.Data)
	// 账号显式配置的 gemini-* 映射必须出现（resolveModelMapping 还会补一组 antigravity
	// 默认透传别名，这里只断言显式条目与排除项，不锁定补齐后的完整集合）。
	require.Contains(t, ids, "gemini-3.8-flash-high")
	// 本分支对显式映射只宣称映射到自身的条目，别名不进入公开列表（见 antigravityAllowedModelSet）。
	require.NotContains(t, ids, "gemini-synced-custom")
	require.Contains(t, ids, "gemini-3.8-flash-low")
	require.Contains(t, ids, "gemini-native-only")
	require.NotContains(t, ids, "gemini-disabled-only")
	require.NotContains(t, ids, "gemini-unset-only")
	// antigravity 账号的 claude-* 映射与 anthropic 账号的映射都不应出现在 gemini 分组。
	require.NotContains(t, ids, "claude-sonnet-4-6")
	require.NotContains(t, ids, "claude-opus-4-6")
	for _, id := range ids {
		require.True(t, strings.HasPrefix(id, "gemini-"), "unexpected non-gemini model on gemini group: %s", id)
	}
	// 没有回落到 geminicli 静态表。
	require.NotContains(t, ids, "gemini-2.0-flash")
}

// antigravity 账号未配置 model_mapping 时使用 DefaultAntigravityModelMapping：
// gemini 分组应列出其中的 gemini-* 条目，而不是回落到 geminicli 静态表；claude-* 仍不出现。
func TestGatewayModels_GeminiGroupUsesAntigravityDefaultMappingWhenUnset(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(23)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{ID: 1, Platform: service.PlatformAntigravity, Extra: map[string]any{"mixed_scheduling": true}, Credentials: map[string]any{}},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformGemini},
	})

	h.Models(c)

	require.Equal(t, http.StatusOK, rec.Code)

	var got gatewayModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	ids := modelIDsForTest(got.Data)
	// 未配置映射时按本分支官方目录宣称官方 Gemini 模型，不使用上游默认映射表。
	require.Contains(t, ids, "gemini-3.5-flash-low")
	for _, id := range ids {
		require.True(t, strings.HasPrefix(id, "gemini-"), "unexpected non-gemini model on gemini group: %s", id)
	}
	// geminicli 静态表独有条目不应出现（说明没有回落到默认列表）。
	require.NotContains(t, ids, "gemini-2.0-flash")
}

// Codex 通过 /models?client_version= 走 CodexModels，同样应看到混合调度账号的 gemini-* 映射。
func TestGatewayModels_CodexGeminiGroupListsAntigravityGeminiMappings(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(24)
	h := newGatewayModelsHandlerForTest(
		&gatewayModelsAccountRepoStub{
			byGroup: map[int64][]service.Account{
				groupID: {
					{
						ID:       1,
						Platform: service.PlatformAntigravity,
						Extra:    map[string]any{"mixed_scheduling": true},
						Credentials: map[string]any{
							"model_mapping": map[string]any{
								"gemini-3.8-flash-high": "gemini-3.8-flash-high",
								"gemini-synced-custom":  "gemini-3.8-flash-high",
								"claude-sonnet-4-6":     "claude-sonnet-4-6",
							},
						},
					},
				},
			},
		},
	)

	rec := httptest.NewRecorder()
	c, _ := gin.CreateTestContext(rec)
	c.Request = httptest.NewRequest(http.MethodGet, "/models?client_version=0.147.0", nil)
	c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
		Group: &service.Group{ID: groupID, Platform: service.PlatformGemini},
	})

	h.CodexModels(c)

	require.Equal(t, http.StatusOK, rec.Code)
	var got codexModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
	slugs := codexModelSlugsForTest(got.Models)
	require.Contains(t, slugs, "gemini-3.8-flash-high")
	// 本分支对显式映射只宣称映射到自身的条目，别名不进入公开列表（见 antigravityAllowedModelSet）。
	require.NotContains(t, slugs, "gemini-synced-custom")
	require.NotContains(t, slugs, "claude-sonnet-4-6")
	require.NotContains(t, slugs, "gemini-2.0-flash")
	for _, slug := range slugs {
		require.True(t, strings.HasPrefix(slug, "gemini-"), "unexpected non-gemini model on gemini group: %s", slug)
	}
}

func TestGatewayModels_GPT6SolLunaDiscoveryRespectsGroupAndAccountRestrictions(t *testing.T) {
	for _, tc := range []struct {
		name       string
		selected   []string
		restricted bool
		want       []string
	}{
		{"selected and ordered", []string{"gpt-6.1-sol", "gpt-6-luna", "gpt-6-sol"}, false, []string{"gpt-6.1-sol", "gpt-6-luna", "gpt-6-sol"}},
		{"group excludes new models", []string{"gpt-5.6-sol"}, false, []string{"gpt-5.6-sol"}},
		{"account restricts new models", []string{"gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna", "gpt-5.6-sol"}, true, []string{"gpt-5.6-sol"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			groupID := int64(25)
			account := service.Account{ID: 1, Platform: service.PlatformOpenAI, Type: service.AccountTypeAPIKey}
			if tc.restricted {
				account.Credentials = map[string]any{"model_mapping": map[string]any{"gpt-5.6-sol": "gpt-5.6-sol"}}
			}
			h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{byGroup: map[int64][]service.Account{groupID: {account}}})
			rec := httptest.NewRecorder()
			c, _ := gin.CreateTestContext(rec)
			c.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
			c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{Group: &service.Group{ID: groupID, Platform: service.PlatformOpenAI, ModelAllowlist: service.GroupModelAllowlist{Enabled: true, Models: tc.selected}}})
			h.Models(c)
			require.Equal(t, http.StatusOK, rec.Code)
			var got gatewayModelsResponseForTest
			require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &got))
			require.Equal(t, tc.want, modelIDsForTest(got.Data))
		})
	}
}

// 场景：jev-latest 只能经 /v1/systemone 使用。我方组合分组按账号实际协议分目录列模型
// （见 TestGatewayModels_CompositeFiltersModelsByActualAccountProtocol）：通用（OpenAI 兼容）目录
// 在有可调度 TypeSafe 账号时按上游语义列出 jev-latest；Anthropic 协议目录（Claude 客户端）与
// Codex 清单都不列；Anthropic 账号只进入 Anthropic 目录。
func TestGatewayModels_CompositeTypeSafeListingScope(t *testing.T) {
	gin.SetMode(gin.TestMode)

	groupID := int64(66)
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
		byGroup: map[int64][]service.Account{
			groupID: {
				{ID: 1, Platform: service.PlatformAnthropic},
				{ID: 2, Platform: service.PlatformTypeSafe, Type: service.AccountTypeAPIKey},
				{
					ID:          3,
					Platform:    service.PlatformOpenAI,
					Type:        service.AccountTypeAPIKey,
					Credentials: map[string]any{"model_mapping": map[string]any{"gpt-native": "gpt-native"}},
				},
			},
		},
	})
	newContext := func(path string, anthropic bool) (*gin.Context, *httptest.ResponseRecorder) {
		rec := httptest.NewRecorder()
		c, _ := gin.CreateTestContext(rec)
		c.Request = httptest.NewRequest(http.MethodGet, path, nil)
		if anthropic {
			c.Request.Header.Set("Anthropic-Version", "2023-06-01")
		}
		c.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{
			Group: &service.Group{ID: groupID, Platform: service.PlatformComposite},
		})
		return c, rec
	}
	listModels := func(anthropic bool) []string {
		c, rec := newContext("/v1/models", anthropic)
		h.Models(c)
		require.Equal(t, http.StatusOK, rec.Code)
		var models gatewayModelsResponseForTest
		require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &models))
		return modelIDsForTest(models.Data)
	}

	openAIModels := listModels(false)
	require.Contains(t, openAIModels, "jev-latest")
	require.Contains(t, openAIModels, "gpt-native")
	require.NotContains(t, openAIModels, "claude-opus-4-6")

	anthropicModels := listModels(true)
	require.Contains(t, anthropicModels, "claude-opus-4-6")
	require.NotContains(t, anthropicModels, "jev-latest")
	require.NotContains(t, anthropicModels, "gpt-native")

	c, rec := newContext("/models?client_version=0.147.0", false)
	h.CodexModels(c)
	require.Equal(t, http.StatusOK, rec.Code)
	var manifest codexModelsResponseForTest
	require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &manifest))
	slugs := codexModelSlugsForTest(manifest.Models)
	require.Contains(t, slugs, "gpt-native")
	require.NotContains(t, slugs, "jev-latest")
	require.NotContains(t, slugs, "claude-opus-4-6")
}

func TestDefaultModelIDsForPlatform_CompositeFallbackExcludesTypeSafe(t *testing.T) {
	require.NotContains(t, defaultModelIDsForPlatform(service.PlatformComposite), "jev-latest")
	require.NotContains(t, defaultCodexModelIDsForPlatform(service.PlatformComposite), "jev-latest")
}

func TestGatewayModels_CompositeExactRoutesRespectTypeSafeIsolation(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 7784
	routes := &gatewayModelsRouteRepoStub{routes: []service.CompositeModelRoute{
		{PublicModel: "systemone-any-alias", TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointAny, Enabled: true},
		{PublicModel: "systemone-responses-alias", TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointResponses, Enabled: true},
		{PublicModel: "systemone-legacy-alias", TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchExact, Enabled: true},
		{PublicModel: "jev-latest", TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchExact, Enabled: true},
		{PublicModel: "responses-alias", TargetPlatform: service.PlatformOpenAI, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointResponses, Enabled: true},
		{PublicModel: "image-alias", TargetPlatform: service.PlatformOpenAI, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointImages, Enabled: true},
	}}
	h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{}, routes)
	wantModels := make([]string, 0, len(routes.routes))
	for _, route := range routes.routes {
		wantModels = append(wantModels, route.PublicModel)
	}
	for _, allowlisted := range []bool{false, true} {
		name := "unrestricted"
		if allowlisted {
			name = "allowlisted"
		}
		t.Run(name, func(t *testing.T) {
			group := &service.Group{ID: groupID, Platform: service.PlatformComposite}
			if allowlisted {
				group.ModelAllowlist = service.GroupModelAllowlist{Enabled: true, Models: []string{
					"systemone-any-alias", "systemone-responses-alias", "systemone-legacy-alias", "jev-latest", "responses-alias", "image-alias",
				}}
			}
			for _, codex := range []bool{false, true} {
				rec := httptest.NewRecorder()
				ctx, _ := gin.CreateTestContext(rec)
				ctx.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
				ctx.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{Group: group})
				if codex {
					h.CodexModels(ctx)
					var manifest codexModelsResponseForTest
					require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &manifest))
					require.Equal(t, []string{"responses-alias"}, codexModelSlugsForTest(manifest.Models))
				} else {
					h.Models(ctx)
					var models gatewayModelsResponseForTest
					require.NoError(t, json.Unmarshal(rec.Body.Bytes(), &models))
					require.ElementsMatch(t, wantModels, modelIDsForTest(models.Data))
				}
				require.Equal(t, http.StatusOK, rec.Code)
			}
		})
	}
}

func TestGatewayModels_CompositeCodexFiltersEffectiveResponsesRoutes(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 7793
	defaultModel := defaultModelIDsForPlatform(service.PlatformOpenAI)[0]
	for _, scenario := range []struct {
		name     string
		model    string
		accounts []service.Account
		routes   []service.CompositeModelRoute
	}{
		{
			name:  "colliding exact routes",
			model: "shared-alias",
			routes: []service.CompositeModelRoute{
				{PublicModel: "shared-alias", TargetPlatform: service.PlatformOpenAI, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointAny, Priority: 1, Enabled: true},
				{PublicModel: "shared-alias", TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointResponses, Priority: 100, Enabled: true},
			},
		},
		{
			name:  "account alias exact override",
			model: "account-alias",
			accounts: []service.Account{{ID: 1, Platform: service.PlatformOpenAI, Credentials: map[string]any{
				"model_mapping": map[string]any{"account-alias": "gpt-5"},
			}}},
			routes: []service.CompositeModelRoute{
				{PublicModel: "account-alias", TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointResponses, Enabled: true},
			},
		},
		{
			name:  "account alias prefix override",
			model: "account-alias",
			accounts: []service.Account{{ID: 1, Platform: service.PlatformOpenAI, Credentials: map[string]any{
				"model_mapping": map[string]any{"account-alias": "gpt-5"},
			}}},
			routes: []service.CompositeModelRoute{
				{PublicModel: "account-", TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchPrefix, Endpoint: service.CompositeRouteEndpointResponses, Enabled: true},
			},
		},
		{
			name:     "account default override",
			model:    defaultModel,
			accounts: []service.Account{{ID: 1, Platform: service.PlatformOpenAI}},
			routes: []service.CompositeModelRoute{
				{PublicModel: defaultModel, TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointResponses, Enabled: true},
			},
		},
		{
			name:  "fallback default override",
			model: defaultModel,
			routes: []service.CompositeModelRoute{
				{PublicModel: defaultModel, TargetPlatform: service.PlatformTypeSafe, MatchType: service.CompositeRouteMatchPrefix, Endpoint: service.CompositeRouteEndpointResponses, Enabled: true},
			},
		},
	} {
		t.Run(scenario.name, func(t *testing.T) {
			for _, allowlisted := range []bool{false, true} {
				name := "unrestricted"
				if allowlisted {
					name = "allowlisted"
				}
				t.Run(name, func(t *testing.T) {
					routes := append([]service.CompositeModelRoute(nil), scenario.routes...)
					if scenario.name != "fallback default override" {
						routes = append(routes, service.CompositeModelRoute{
							PublicModel: "responses-control", TargetPlatform: service.PlatformOpenAI, MatchType: service.CompositeRouteMatchExact, Endpoint: service.CompositeRouteEndpointResponses, Enabled: true,
						})
					}
					h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
						byGroup: map[int64][]service.Account{groupID: scenario.accounts},
					}, &gatewayModelsRouteRepoStub{routes: routes})
					group := &service.Group{ID: groupID, Platform: service.PlatformComposite}
					if allowlisted {
						group.ModelAllowlist = service.GroupModelAllowlist{Enabled: true, Models: []string{scenario.model, "responses-control"}}
					}
					for _, codex := range []bool{false, true} {
						recorder := httptest.NewRecorder()
						ctx, _ := gin.CreateTestContext(recorder)
						ctx.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
						ctx.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{Group: group})
						if codex {
							h.CodexModels(ctx)
							var manifest codexModelsResponseForTest
							require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &manifest))
							slugs := codexModelSlugsForTest(manifest.Models)
							require.NotContains(t, slugs, scenario.model)
							if scenario.name != "fallback default override" {
								require.Contains(t, slugs, "responses-control")
							} else if allowlisted {
								require.Empty(t, slugs)
							}
						} else {
							h.Models(ctx)
							var models gatewayModelsResponseForTest
							require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &models))
							// 无账号时本分支的 Composite 目录为空（不回落全协议默认清单），只对有账号／路由的场景断言包含。
							if scenario.name != "fallback default override" {
								require.Contains(t, modelIDsForTest(models.Data), scenario.model)
							}
						}
						require.Equal(t, http.StatusOK, recorder.Code)
					}
				})
			}
		})
	}
}

func TestGatewayModels_CompositeCodexRouteLookupErrorKeepsFallback(t *testing.T) {
	gin.SetMode(gin.TestMode)
	const groupID int64 = 7793
	// 本分支的 Composite Codex 目录属于 OpenAI 协议，没有 OpenAI 协议账号时不回落全协议默认清单；
	// 因此「路由查询失败仍保留目录」用一个未配置映射的 OpenAI 账号（默认模型来自账号）来验证。
	model := defaultModelIDsForPlatform(service.PlatformOpenAI)[0]
	for _, allowlisted := range []bool{false, true} {
		name := "unrestricted"
		if allowlisted {
			name = "allowlisted"
		}
		t.Run(name, func(t *testing.T) {
			h := newGatewayModelsHandlerForTest(&gatewayModelsAccountRepoStub{
				byGroup: map[int64][]service.Account{groupID: {{ID: 1, Platform: service.PlatformOpenAI}}},
			}, &gatewayModelsRouteRepoStub{err: errors.New("unavailable")})
			group := &service.Group{ID: groupID, Platform: service.PlatformComposite}
			if allowlisted {
				group.ModelAllowlist = service.GroupModelAllowlist{Enabled: true, Models: []string{model}}
			}
			recorder := httptest.NewRecorder()
			ctx, _ := gin.CreateTestContext(recorder)
			ctx.Request = httptest.NewRequest(http.MethodGet, "/v1/models", nil)
			ctx.Set(string(middleware2.ContextKeyAPIKey), &service.APIKey{Group: group})
			h.CodexModels(ctx)
			require.Equal(t, http.StatusOK, recorder.Code)
			var manifest codexModelsResponseForTest
			require.NoError(t, json.Unmarshal(recorder.Body.Bytes(), &manifest))
			require.Contains(t, codexModelSlugsForTest(manifest.Models), model)
			if allowlisted {
				require.Len(t, manifest.Models, 1)
			}
		})
	}
}
