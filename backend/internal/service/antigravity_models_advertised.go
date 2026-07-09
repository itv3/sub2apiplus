package service

import "github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"

func defaultAntigravityModelIDs() []string {
	return antigravity.OfficialModelIDs()
}

// AdvertisedModelMappingForAccount 返回模型列表接口应暴露给用户的模型。
// Antigravity 默认广告口径收敛到官方模型，同时暴露管理员在账号白名单中手动加入的自映射模型。
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
		for from, to := range stringMappingFromRaw(account.Credentials["model_mapping"]) {
			from = normalizeAntigravityModelID(from)
			to = normalizeAntigravityModelID(to)
			if from == "" || from != to {
				continue
			}
			mapping[from] = to
		}
		return mapping
	}
	return account.GetModelMapping()
}
