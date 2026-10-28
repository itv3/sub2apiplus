//go:build unit

package handler

import (
	"context"
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

func TestMessagesDispatchMappedModelInCtx(t *testing.T) {
	group := &service.Group{Platform: service.PlatformComposite, MessagesDispatchModelConfig: service.OpenAIMessagesDispatchModelConfig{OpusMappedModel: "gpt-5.5"}}
	apiKey := &service.APIKey{Group: group}
	c, _ := gin.CreateTestContext(httptest.NewRecorder())
	c.Request = httptest.NewRequest(http.MethodPost, "/v1/messages", nil)
	inPlatform := func(platform string) context.Context {
		return service.WithResolvedTargetPlatform(context.Background(), platform)
	}
	// 多协议平台与 grok 不适用分组的 Messages 调度映射。
	require.Equal(t, "", messagesDispatchMappedModelInCtx(c, inPlatform(service.PlatformGrok), apiKey, "claude-opus-4"))
	require.Equal(t, "", messagesDispatchMappedModelInCtx(c, inPlatform(service.PlatformDeepseek), apiKey, "claude-opus-4"))
	require.Equal(t, "gpt-5.5", messagesDispatchMappedModelInCtx(c, inPlatform(service.PlatformOpenAI), apiKey, "claude-opus-4"))
	require.Equal(t, "", messagesDispatchMappedModelInCtx(c, inPlatform(service.PlatformOpenAI), nil, "claude-opus-4"))

	// 与上游函数在对应上下文下的结果一致（全平台、两种分组的逐项对照见 openai_gateway_composite_entry_test.go）。
	c.Request = c.Request.WithContext(inPlatform(service.PlatformDeepseek))
	require.Equal(t, "", resolveOpenAIMessagesDispatchMappedModel(c, apiKey, "claude-opus-4"))
	c.Request = c.Request.WithContext(inPlatform(service.PlatformOpenAI))
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
