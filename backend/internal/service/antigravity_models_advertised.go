package service

// AdvertisedModelMappingForAccount 返回模型列表接口应暴露给用户的模型。
func AdvertisedModelMappingForAccount(account *Account) map[string]string {
	if account == nil {
		return nil
	}
	mapping := account.GetModelMapping()
	// Antigravity 模型目录沿用上游默认规则，保留账号映射与默认透传项。
	if account.Platform == PlatformAntigravity {
		return mapping
	}
	// 平台内置别名用于兼容默认请求名；仅管理员显式配置映射时隐藏原名。
	if account.IsOpenAIPassthroughEnabled() || len(stringMappingFromRaw(account.Credentials["model_mapping"])) == 0 {
		return mapping
	}
	return modelMappingForListing(mapping)
}
