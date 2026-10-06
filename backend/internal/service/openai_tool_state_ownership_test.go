package service

import (
	"bytes"
	"fmt"
	"net/http/httptest"
	"runtime"
	"strings"
	"testing"
	"unsafe"

	"github.com/Wei-Shaw/sub2api/internal/pkg/apicompat"
	pkghttputil "github.com/Wei-Shaw/sub2api/internal/pkg/httputil"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
	"github.com/tidwall/gjson"
)

func toolStateStringBorrowsBody(body []byte, value string) bool {
	if len(body) == 0 || value == "" {
		return false
	}
	start := uintptr(unsafe.Pointer(unsafe.SliceData(body)))
	pointer := uintptr(unsafe.Pointer(unsafe.StringData(value)))
	return pointer >= start && pointer < start+uintptr(len(body))
}

func requireToolStateStringsOwned(t *testing.T, body []byte, values ...string) {
	t.Helper()
	for _, value := range values {
		require.False(t, toolStateStringBorrowsBody(body, value), "跨轮次保存的工具名不能继续引用入口正文")
	}
}

func requireClientToolMappingOwned(t *testing.T, body []byte, mapping apicompat.ResponsesClientToolMapping) {
	t.Helper()
	for name := range mapping.CustomTools {
		requireToolStateStringsOwned(t, body, name)
	}
	for flat, name := range mapping.NamespaceTools {
		requireToolStateStringsOwned(t, body, flat, name.Namespace, name.Name)
	}
}

// 共享解码后去空白得到的短名字仍可能指向长正文；不能按最终字符串长度决定是否复制。
func TestCodexToolNameReverseOwnsSharedDecodedName(t *testing.T) {
	tests := []struct {
		name string
		key  string
		set  func(*gin.Context, map[string]string)
	}{
		{name: "设置映射", key: codexToolNameReverseKey, set: setCodexToolNameReverse},
		{name: "合并映射", key: codexToolNameReverseKey, set: mergeCodexToolNameReverse},
		{name: "会话更新", key: codexToolNameSessionKey, set: func(c *gin.Context, reverse map[string]string) {
			updateCodexToolNameReverseForWSFrame(c, []byte(`{"type":"session.update","session":{"tools":[]}}`), reverse)
		}},
		{name: "当前轮次", key: codexToolNameReverseKey, set: func(c *gin.Context, reverse map[string]string) {
			updateCodexToolNameReverseForWSFrame(c, []byte(`{"type":"response.create","tools":[]}`), reverse)
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			body := []byte(fmt.Sprintf(`{"tools":[{"type":"function","name":%q}]}`,
				strings.Repeat(" ", officialJSONSharedStringMinBytes)+"PyThOn"))
			payload, _, err := decodeOfficialJSONObjectSharingBody(body)
			require.NoError(t, err)
			reverse, changed, err := aliasOpenAIOAuthReservedToolNames(payload)
			require.NoError(t, err)
			require.True(t, changed)
			require.True(t, toolStateStringBorrowsBody(body, reverse[codexPythonToolAlias]), "用例必须经过真实的共享解码和去空白路径")
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			test.set(c, reverse)
			stored := codexToolNameReverseForKey(c, test.key)
			requireToolStateStringsOwned(t, body, stored[codexPythonToolAlias])

			clear(body)
			restored := restoreCodexToolNamesInJSON([]byte(`{"output":[{"type":"function_call","name":"python__sub2api"}]}`), stored)
			require.Equal(t, "PyThOn", gjson.GetBytes(restored, "output.0.name").String())
		})
	}
}

// 使用正式租约读取入口，释放映射后再消费工具状态，验证其生命期独立于原始请求正文。
// 先检查字符串地址，缺少复制时以普通断言失败，避免把 use-after-unmap 变成进程崩溃。
func TestResponsesToolStateSurvivesOwnedBodyRelease(t *testing.T) {
	if runtime.GOOS != "linux" && runtime.GOOS != "darwin" {
		t.Skip("此用例核对匿名映射释放后的工具状态")
	}
	customName := "custom_" + strings.Repeat("c", 2048)
	namespace := "namespace_" + strings.Repeat("n", 2048)
	functionName := "function_" + strings.Repeat("f", 2048)
	fixture := []byte(fmt.Sprintf(`{"input":%q,"tools":[{"type":"function","name":%q},{"type":"custom","name":%q},{"type":"namespace","name":%q,"tools":[{"type":"function","name":%q,"parameters":{"type":"object"}}]}]}`,
		strings.Repeat("x", 1<<20), strings.Repeat(" ", 2048)+"PyThOn", customName, namespace, functionName))
	customResponse := []byte(fmt.Sprintf(`{"output":[{"type":"function_call","name":%q,"arguments":"{\"input\":\"pwd\"}"}]}`, customName))
	tests := []struct {
		name  string
		cache func(*testing.T, *gin.Context, []byte) func()
	}{
		{name: "保留工具名", cache: func(t *testing.T, c *gin.Context, body []byte) func() {
			payload, _, err := decodeOfficialJSONObjectSharingBody(body)
			require.NoError(t, err)
			reverse, changed, err := aliasOpenAIOAuthReservedToolNames(payload)
			require.NoError(t, err)
			require.True(t, changed)
			setCodexToolNameReverse(c, reverse)
			requireToolStateStringsOwned(t, body, codexToolNameReverseFromContext(c)[codexPythonToolAlias])
			return func() {
				restored := restoreCodexToolNamesFromContext(c, []byte(`{"output":[{"type":"function_call","name":"python__sub2api"}]}`))
				require.Equal(t, "PyThOn", gjson.GetBytes(restored, "output.0.name").String())
			}
		}},
		{name: "命名空间", cache: func(t *testing.T, c *gin.Context, body []byte) func() {
			_, err := flattenOpenAIResponsesNamespaces(c, body)
			require.NoError(t, err)
			names := openAIResponsesNamespaceNames(c)
			require.Len(t, names, 1)
			var response []byte
			for flat, name := range names {
				requireToolStateStringsOwned(t, body, flat, name.Namespace, name.Name)
				response = []byte(fmt.Sprintf(`{"output":[{"type":"function_call","name":%q}]}`, flat))
			}
			return func() {
				restored, err := restoreOpenAIResponsesNamespacePayload(c, response)
				require.NoError(t, err)
				require.Equal(t, namespace, gjson.GetBytes(restored, "output.0.namespace").String())
				require.Equal(t, functionName, gjson.GetBytes(restored, "output.0.name").String())
			}
		}},
		{name: "客户端工具", cache: func(t *testing.T, c *gin.Context, body []byte) func() {
			_, mapping, err := adaptOpenAIResponsesClientTools(body)
			require.NoError(t, err)
			setOpenAIResponsesClientToolMapping(c, mapping)
			stored, ok := openAIResponsesClientToolMapping(c)
			require.True(t, ok)
			requireClientToolMappingOwned(t, body, stored)
			return func() {
				restored, err := restoreOpenAIResponsesClientToolPayload(c, customResponse)
				require.NoError(t, err)
				require.Equal(t, "custom_tool_call", gjson.GetBytes(restored, "output.0.type").String())
				require.Equal(t, customName, gjson.GetBytes(restored, "output.0.name").String())
				require.Equal(t, "pwd", gjson.GetBytes(restored, "output.0.input").String())
			}
		}},
		{name: "WS桥接继承", cache: func(t *testing.T, c *gin.Context, body []byte) func() {
			adapted, mapping, err := adaptResponsesClientToolsForFunctionUpstreamWithMapping(body, "测试上游", apicompat.ResponsesClientToolMapping{})
			require.NoError(t, err)
			lowered, present := openAIWSHTTPBridgeRawField(adapted, "tools")
			require.True(t, present)
			setOpenAIWSHTTPBridgeToolState(c, openAIWSHTTPBridgeToolState{ClientMapping: mapping, LoweredTools: lowered})
			stored, ok := openAIWSHTTPBridgeToolStateFromContext(c)
			require.True(t, ok)
			requireClientToolMappingOwned(t, body, stored.ClientMapping)
			require.Equal(t, -1, officialForwardOffsetWithin(stored.LoweredTools, body))
			return func() {
				state, ok := openAIWSHTTPBridgeToolStateFromContext(c)
				require.True(t, ok)
				next := []byte(fmt.Sprintf(`{"input":[{"type":"custom_tool_call","call_id":"call_1","name":%q,"input":"pwd"}]}`, customName))
				adapted, inherited, err := adaptResponsesClientToolsForFunctionUpstreamWithMapping(next, "测试上游", state.ClientMapping, decodeOpenAIWSHTTPBridgeLoweredTools(state.LoweredTools))
				require.NoError(t, err)
				require.True(t, inherited.CustomTools[customName])
				require.Len(t, inherited.NamespaceTools, 1)
				require.Equal(t, "function_call", gjson.GetBytes(adapted, "input.0.type").String())
				require.Equal(t, customName, gjson.GetBytes(adapted, "input.0.name").String())
			}
		}},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			request := httptest.NewRequest("POST", "/v1/responses", bytes.NewReader(fixture))
			owner, err := pkghttputil.ReadOwnedAdmittedLenientJSONRequestBodyWithReservation(request, int64(len(fixture)), nil)
			require.NoError(t, err)
			t.Cleanup(func() { require.NoError(t, owner.Close()) })
			require.True(t, owner.IsMapped())
			c, _ := gin.CreateTestContext(httptest.NewRecorder())
			consume := test.cache(t, c, owner.Bytes())
			require.NoError(t, owner.Close())
			require.Zero(t, owner.MappedBytes(), "必须先释放映射，再消费跨轮次状态")
			consume()
		})
	}
}
