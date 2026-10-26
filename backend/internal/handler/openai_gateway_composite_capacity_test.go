//go:build unit

package handler

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/Wei-Shaw/sub2api/internal/service"
	"github.com/gin-gonic/gin"
	"github.com/stretchr/testify/require"
)

func TestCompositeAcquireSessionHash(t *testing.T) {
	require.Equal(t, "abc", compositeAcquireSessionHash("abc", false))
	require.Equal(t, "", compositeAcquireSessionHash("abc", true), "临时借用不带会话哈希，避免改写粘性绑定")
}

func TestMessagesDispatchMappedModelForPlatform(t *testing.T) {
	group := &service.Group{Platform: service.PlatformComposite, MessagesDispatchModelConfig: service.OpenAIMessagesDispatchModelConfig{OpusMappedModel: "gpt-5.5"}}
	apiKey := &service.APIKey{Group: group}
	// 多协议平台与 grok 不适用分组的 Messages 调度映射。
	require.Equal(t, "", messagesDispatchMappedModelForPlatform(apiKey, service.PlatformGrok, "claude-opus-4"))
	require.Equal(t, "", messagesDispatchMappedModelForPlatform(apiKey, service.PlatformDeepseek, "claude-opus-4"))
	require.Equal(t, "gpt-5.5", messagesDispatchMappedModelForPlatform(apiKey, service.PlatformOpenAI, "claude-opus-4"))
	require.Equal(t, "", messagesDispatchMappedModelForPlatform(nil, service.PlatformOpenAI, "claude-opus-4"))

	// 原函数改走共用实现后行为不变。
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	c.Request = c.Request.WithContext(service.WithResolvedTargetPlatform(c.Request.Context(), service.PlatformDeepseek))
	require.Equal(t, "", resolveOpenAIMessagesDispatchMappedModel(c, apiKey, "claude-opus-4"))
	c.Request = c.Request.WithContext(service.WithResolvedTargetPlatform(c.Request.Context(), service.PlatformOpenAI))
	require.Equal(t, "gpt-5.5", resolveOpenAIMessagesDispatchMappedModel(c, apiKey, "claude-opus-4"))
}

func TestTryCompositeCapacitySwitchGates(t *testing.T) {
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/chat/completions", nil)
	apiKey := &service.APIKey{Group: &service.Group{Platform: service.PlatformComposite}}
	waiting := &service.AccountSelectionResult{
		Account:  &service.Account{ID: 1, Platform: service.PlatformKimi},
		WaitPlan: &service.AccountWaitPlan{AccountID: 1, MaxConcurrency: 1},
	}
	h := &OpenAIGatewayHandler{}

	// 调用方判定不可换号（有账号失败、续链、compact、生图）：直接放行，原有排队流程不变。
	_, switched := h.tryCompositeCapacitySwitch(c, apiKey, nil, waiting, nil, compositeCapacitySwitchParams{Eligible: false})
	require.False(t, switched)
	// 没有网关服务、已抢到槽位、没有排队计划：同样不换号。
	_, switched = h.tryCompositeCapacitySwitch(c, apiKey, nil, waiting, nil, compositeCapacitySwitchParams{Eligible: true})
	require.False(t, switched)
	acquired := &service.AccountSelectionResult{Account: waiting.Account, Acquired: true}
	_, switched = h.tryCompositeCapacitySwitch(c, apiKey, nil, acquired, nil, compositeCapacitySwitchParams{Eligible: true})
	require.False(t, switched)
	_, switched = h.tryCompositeCapacitySwitch(c, apiKey, nil, &service.AccountSelectionResult{Account: waiting.Account}, nil, compositeCapacitySwitchParams{Eligible: true})
	require.False(t, switched)
}
