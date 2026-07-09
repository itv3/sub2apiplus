package antigravity

import "strings"

// OfficialModelDescriptor 描述 Antigravity 官方客户端抓包确认的发包模型。
// 抓包基线：Antigravity Hub 2.2.1，2026-07。
// 注意：MODEL_PLACEHOLDER_M* 是 fetchAvailableModels 抓包原样返回的 model enum，
// 不是本仓库为了脱敏临时造出的占位符；只有重新抓包确认变化后才应替换。
type OfficialModelDescriptor struct {
	ID             string `json:"id"`
	DisplayName    string `json:"display_name"`
	ModelEnum      string `json:"model_enum"`
	ThinkingBudget int    `json:"thinking_budget"`
	CreatedAt      string `json:"created_at"`
	IsReasoning    bool   `json:"is_reasoning"`
}

var officialModelDescriptors = []OfficialModelDescriptor{
	{
		ID:             "gemini-3.5-flash-extra-low",
		DisplayName:    "Gemini 3.5 Flash Low",
		ModelEnum:      "MODEL_PLACEHOLDER_M187",
		ThinkingBudget: 1000,
		CreatedAt:      "2026-06-29T00:00:00Z",
		IsReasoning:    true,
	},
	{
		ID:             "gemini-3.5-flash-low",
		DisplayName:    "Gemini 3.5 Flash Medium",
		ModelEnum:      "MODEL_PLACEHOLDER_M20",
		ThinkingBudget: 4000,
		CreatedAt:      "2026-06-29T00:00:00Z",
		IsReasoning:    true,
	},
	{
		ID:             "gemini-3-flash-agent",
		DisplayName:    "Gemini 3.5 Flash High",
		ModelEnum:      "MODEL_PLACEHOLDER_M132",
		ThinkingBudget: 10000,
		CreatedAt:      "2026-06-29T00:00:00Z",
		IsReasoning:    true,
	},
	{
		ID:             "gemini-3.1-pro-low",
		DisplayName:    "Gemini 3.1 Pro Low",
		ModelEnum:      "MODEL_PLACEHOLDER_M36",
		ThinkingBudget: 1001,
		CreatedAt:      "2026-02-19T00:00:00Z",
		IsReasoning:    true,
	},
	{
		ID:             "gemini-pro-agent",
		DisplayName:    "Gemini 3.1 Pro High",
		ModelEnum:      "MODEL_PLACEHOLDER_M16",
		ThinkingBudget: 10001,
		CreatedAt:      "2026-02-19T00:00:00Z",
		IsReasoning:    true,
	},
	{
		ID:             "claude-sonnet-4-6",
		DisplayName:    "Claude Sonnet 4.6",
		ModelEnum:      "MODEL_PLACEHOLDER_M35",
		ThinkingBudget: 1024,
		CreatedAt:      "2026-02-17T00:00:00Z",
		IsReasoning:    true,
	},
	{
		ID:             "claude-opus-4-6-thinking",
		DisplayName:    "Claude Opus 4.6 Thinking",
		ModelEnum:      "MODEL_PLACEHOLDER_M26",
		ThinkingBudget: 1024,
		CreatedAt:      "2026-02-05T00:00:00Z",
		IsReasoning:    true,
	},
	{
		ID:             "gpt-oss-120b-medium",
		DisplayName:    "GPT-OSS 120B Medium",
		ModelEnum:      "MODEL_OPENAI_GPT_OSS_120B_MEDIUM",
		ThinkingBudget: 8192,
		CreatedAt:      "2026-06-29T00:00:00Z",
		IsReasoning:    true,
	},
}

// OfficialWebSearchFallbackModel 是 web_search 请求的官方兜底模型，必须保持在 officialModelDescriptors 内。
const OfficialWebSearchFallbackModel = "gemini-3.5-flash-low"

func officialModelDescriptor(modelID string) (OfficialModelDescriptor, bool) {
	modelID = strings.ToLower(strings.TrimSpace(modelID))
	for _, descriptor := range officialModelDescriptors {
		if descriptor.ID == modelID {
			return descriptor, true
		}
	}
	return OfficialModelDescriptor{}, false
}

// OfficialModelDescriptors 返回官方模型描述表的副本。
func OfficialModelDescriptors() []OfficialModelDescriptor {
	out := make([]OfficialModelDescriptor, len(officialModelDescriptors))
	copy(out, officialModelDescriptors)
	return out
}

// OfficialModelIDs 返回 Antigravity 官方发包使用的模型 ID。
func OfficialModelIDs() []string {
	models := make([]string, 0, len(officialModelDescriptors))
	for _, descriptor := range officialModelDescriptors {
		models = append(models, descriptor.ID)
	}
	return models
}

// OfficialModelMapping 返回默认展示/白名单使用的官方模型映射。
func OfficialModelMapping() map[string]string {
	mapping := make(map[string]string, len(officialModelDescriptors))
	for _, descriptor := range officialModelDescriptors {
		mapping[descriptor.ID] = descriptor.ID
	}
	return mapping
}

func OfficialGeminiModels() []GeminiModel {
	result := make([]GeminiModel, len(officialModelDescriptors))
	for i, descriptor := range officialModelDescriptors {
		result[i] = GeminiModel{
			Name:                       "models/" + descriptor.ID,
			DisplayName:                descriptor.DisplayName,
			SupportedGenerationMethods: defaultGeminiMethods,
		}
	}
	return result
}

func OfficialGeminiModelsList() GeminiModelsListResponse {
	return GeminiModelsListResponse{Models: OfficialGeminiModels()}
}

func OfficialGeminiModel(model string) (GeminiModel, bool) {
	trimmed := strings.TrimSpace(model)
	trimmed = strings.TrimPrefix(trimmed, "models/")
	descriptor, ok := officialModelDescriptor(trimmed)
	if !ok {
		return GeminiModel{}, false
	}
	return GeminiModel{
		Name:                       "models/" + descriptor.ID,
		DisplayName:                descriptor.DisplayName,
		SupportedGenerationMethods: defaultGeminiMethods,
	}, true
}

// IsOfficialModelID 判断模型 ID 是否属于 Antigravity 官方发包模型。
func IsOfficialModelID(modelID string) bool {
	_, ok := officialModelDescriptor(modelID)
	return ok
}

func OfficialModelEnum(modelID string) string {
	descriptor, ok := officialModelDescriptor(modelID)
	if !ok {
		return ""
	}
	return descriptor.ModelEnum
}

func DefaultThinkingBudget(modelID string) (int, bool) {
	descriptor, ok := officialModelDescriptor(modelID)
	if !ok {
		return 0, false
	}
	return descriptor.ThinkingBudget, true
}
