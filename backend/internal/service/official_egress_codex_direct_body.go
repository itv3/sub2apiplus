package service

import (
	"github.com/tidwall/gjson"
	"github.com/tidwall/sjson"
)

// 直接交给 Executor 的 Responses 请求体。
//
// 用量探针（probeOpenAICodexSnapshot）与管理员「测试连接」的 OAuth 分支不经过 Forward 的定型层：它们自己构造完整业务
// 正文，由 Executor 的 Compiler 负责字段线序、画像元数据与压缩，并对闭集外字段失败关闭。较新画像的 responses_http
// 闭集不再声明顶层 instructions（基础指令改走 input 里的载体消息），而这类内部请求只为引出限额响应头或验证连通性，
// 不需要基础指令。所以端点画像闭集不声明 instructions 时直接去掉这个字段，不装载体消息：直连正文拿不到 Compiler
// 之后才派生的线程身份，也就算不出与之一致的消息 id。
//
// 整理作用在最终字节上，必须排在所有改写正文的步骤之后：API-Key mimic 的 RewriteBody 会补回默认 instructions，
// 影子账号借父账号的 OAuth 凭据走官方出口时两者同时生效，先整理再 mimic 改写就会把字段补回来。
//
// 取画像的方式与配额链路一致：用 Executor runtime 实际冻结的发布槽位（CodexReleaseMode）解析端点画像，不读入站的
// ProfileMode。闭集仍声明 instructions（旧画像）、runtime 缺失或画像解析失败时原样放行：出站字节与不经过本文件时
// 逐字节相同，失败仍由 Compiler 按原语义关闭。共享代码不比较版本号，是否声明只看画像数据。

// officialCodexEndpointDeclaresBodyField 判断 mode 对应发布的端点画像闭集是否声明了 field。
func officialCodexEndpointDeclaresBodyField(mode string, endpointID string, field string) (bool, error) {
	endpoint, err := resolveCodexEndpointForMode(mode, endpointID)
	if err != nil {
		return false, err
	}
	for _, declared := range endpoint.Body.Fields {
		if declared.Name == field {
			return true, nil
		}
	}
	return false, nil
}

// shapeOfficialCodexDirectResponsesBody 按 Executor runtime 冻结的发布槽位整理直连 Responses 请求体：
// responses_http 闭集不声明顶层 instructions 时删掉它，返回整理后的字节与是否被改动。删除按字节拼接，
// 其余成员的原始字节与相对顺序保持不变。runtime 为空、正文本来没有顶层 instructions、闭集仍声明它、
// 画像解析失败、正文不是合法 JSON 时都原样返回同一份字节。
func shapeOfficialCodexDirectResponsesBody(
	body []byte,
	runtimeState *OfficialEgressTransitionRuntime,
) ([]byte, bool) {
	if runtimeState == nil || !gjson.ValidBytes(body) || !gjson.GetBytes(body, "instructions").Exists() {
		return body, false
	}
	declared, err := officialCodexEndpointDeclaresBodyField(
		string(runtimeState.CodexReleaseMode), officialCodexEndpointResponsesHTTP, "instructions",
	)
	if err != nil || declared {
		return body, false
	}
	shaped, err := sjson.DeleteBytes(body, "instructions")
	if err != nil {
		return body, false
	}
	return shaped, true
}
