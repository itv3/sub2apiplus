package service

import "github.com/Wei-Shaw/sub2api/internal/pkg/antigravity"

func defaultAntigravityModelIDs() []string {
	return antigravity.OfficialModelIDs()
}

// AdvertisedModelMappingForAccount 返回模型列表接口应暴露给用户的模型。
// Antigravity 未显式配置白名单时广告官方模型；显式配置后只广告管理员保留的自映射模型。
func AdvertisedModelMappingForAccount(account *Account) map[string]string {
	if account == nil {
		return nil
	}
	if account.Platform == PlatformAntigravity {
		allowed := antigravityAllowedModelSet(account)
		mapping := make(map[string]string, len(allowed))
		for model := range allowed {
			mapping[model] = model
		}
		return mapping
	}
	return account.GetModelMapping()
}
