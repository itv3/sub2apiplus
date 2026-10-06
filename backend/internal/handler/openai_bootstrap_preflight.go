package handler

import (
	"unsafe"

	"github.com/tidwall/gjson"
)

// codexBootstrapMayNeedNormalization 只判断是否存在自动化或委派注入的候选项。
// 长会话绝大多数没有这种项，不应为了发现无需修改而两次解码整段历史。
// 只读字符串视图不离开本函数；这里不读取 output，也不复制 input 或历史密文。
// 找到候选后仍执行原来的完整语法、重复键、关联调用和信封检查。
func codexBootstrapMayNeedNormalization(body []byte) bool {
	view := unsafe.String(unsafe.SliceData(body), len(body))
	input := gjson.Get(view, "input")
	if !input.IsArray() {
		return false
	}
	found := false
	input.ForEach(func(_, item gjson.Result) bool {
		if item.Get("type").Str != "function_call_output" {
			return true
		}
		namespace, name := item.Get("namespace").Str, item.Get("name").Str
		found = isCodexDelegationTool(namespace, name) || namespace == "codex_app" && name == "automation_update"
		return !found
	})
	return found
}
