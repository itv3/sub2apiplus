package service

import (
	"strings"

	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 本分支 OAuth 生图固定走 Codex 原生 /images/generations 并接入官方出站（见
// forwardOpenAIImagesOAuth），本次上游合并新增的 direct 与 responses 双模式实现未采纳；
// 本文件只保留仍被共用路径使用的三个函数：两条错误处理路径的主控模型错误判断，
// 以及 API Key 生图流式处理用到的图片 URL 回填与用量解析。

// isOpenAIImagesMainModelError 判断上游错误是否只是 responses 主控模型不可用（套餐门控）。
// 这种错误不代表图片模型配额耗尽，调用方应直接透传，避免误冷却整个图片账号池。
func isOpenAIImagesMainModelError(status int, body []byte) bool {
	if !isOpenAICodexPlanGatedModelError(status, body) {
		return false
	}
	message := extractUpstreamErrorMessage(body)
	model := openAIImagesResponsesMainModelValue()
	return strings.Contains(message, "'"+model+"'") ||
		strings.Contains(message, `"`+model+`"`)
}

func codexDirectImageURL(body []byte, path, outputFormat string) []byte {
	if result := gjson.GetBytes(body, path+"b64_json").String(); result != "" {
		body, _ = sjson.SetBytes(body, path+"url", "data:"+openAIImageOutputMIMEType(outputFormat)+";base64,"+result)
		body, _ = sjson.DeleteBytes(body, path+"b64_json")
	}
	return body
}

// Images 端点只输出图片；未提供输出分类时，output_tokens 全部是图片 token。
// 缓存图片数量只采信明确明细，不根据总缓存量猜测图文占比。
func codexDirectImagesUsage(body []byte) (OpenAIUsage, bool) {
	value := gjson.GetBytes(body, "usage")
	usage, ok := openAIUsageFromGJSON(value)
	if !ok {
		return usage, false
	}
	if !value.Get("output_tokens_details.image_tokens").Exists() {
		usage.ImageOutputTokens = usage.OutputTokens
	}
	cached := value.Get("input_tokens_details.cached_tokens_details")
	if !value.Get("input_tokens_details.cached_tokens").Exists() && cached.IsObject() {
		imageTokens, _ := boundedJSONNonNegativeInt(cached.Get("image_tokens"))
		textTokens, _ := boundedJSONNonNegativeInt(cached.Get("text_tokens"))
		usage.CacheReadInputTokens = min(imageTokens, max(usage.InputTokens, 0))
		usage.CacheReadInputTokens += min(textTokens, max(usage.InputTokens-usage.CacheReadInputTokens, 0))
	}
	imageCached, _ := boundedJSONNonNegativeInt(cached.Get("image_tokens"))
	usage.ImageCacheReadTokens = min(imageCached, max(usage.ImageInputTokens, 0), max(usage.CacheReadInputTokens, 0))
	return usage, true
}
