package service

import "github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"

func defaultAntigravityModelIDs() []string {
	return antigravity.OfficialModelIDs()
}

// AdvertisedModelMappingForAccount 返回模型列表接口应暴露给用户的模型。
// Antigravity 的手动同步结果可保存为真实上游模型，但 /models 广告口径始终收敛到官方模型。
func AdvertisedModelMappingForAccount(account *Account) map[string]string {
	if account == nil {
		return nil
	}
	if account.Platform == PlatformAntigravity {
		defaults := defaultAntigravityModelIDs()
		mapping := make(map[string]string, len(defaults))
		for _, model := range defaults {
			mapping[model] = model
		}
		return mapping
	}
	return account.GetModelMapping()
}
