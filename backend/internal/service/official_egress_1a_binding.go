package service

import (
	"context"
	"errors"
	"net/http"
	"strings"

	"github.com/Wei-Shaw/sub2api/internal/officialegress"
)

// 变更集 1A 只在官方出站业务调用点绑定 SinkID。共享 facade 与发送栈不得生成或覆盖它。
func bindOfficialEgressSink(ctx context.Context, sinkID officialegress.SinkID) (context.Context, error) {
	return officialegress.StartDefaultSinkAttempt(ctx, sinkID)
}

// preserveOfficialEgressSinkAttempt 用于共享连接池：只保留已有且携带 Token 的
// Executor attempt；共享层不得补造业务身份或覆盖其它 Sink。
func preserveOfficialEgressSinkAttempt(ctx context.Context, sinkID officialegress.SinkID) (context.Context, error) {
	return officialegress.PreserveDefaultSinkAttempt(ctx, sinkID)
}

// bindClaudeManagedEndpointRequest 只在请求精确指向登记的 Claude 物理端点时
// 绑定受管 Sink。自定义 base URL、API Key 与其他产品路径不得因复用同一 HTTP
// 客户端而被错误归入 Claude 管理域。
func bindClaudeManagedEndpointRequest(
	req *http.Request,
	sinkID officialegress.SinkID,
	method string,
	host string,
	path string,
) (*http.Request, error) {
	if req == nil || req.URL == nil {
		return nil, errors.New("Claude 受管请求为空")
	}
	port := req.URL.Port()
	if !strings.EqualFold(req.Method, method) ||
		!strings.EqualFold(req.URL.Scheme, "https") ||
		!strings.EqualFold(req.URL.Hostname(), host) ||
		(port != "" && port != "443") || req.URL.EscapedPath() != path {
		return req, nil
	}
	bound, err := bindOfficialEgressSink(req.Context(), sinkID)
	if err != nil {
		return nil, err
	}
	return req.WithContext(bound), nil
}

const (
	officialEgressSinkAdminTestCompact           = officialegress.SinkCodexAdminTestCompact
	officialEgressSinkAdminTestResponses         = officialegress.SinkCodexAdminTestResponses
	officialEgressSinkAlphaSearchDirect          = officialegress.SinkCodexAlphaSearchDirect
	officialEgressSinkAlphaSearchPATFallback     = officialegress.SinkCodexAlphaSearchPATFallback
	officialEgressSinkFilesBlobUpload            = officialegress.SinkCodexFilesBlobUpload
	officialEgressSinkFilesRegister              = officialegress.SinkCodexFilesRegister
	officialEgressSinkImagesOAuthTest            = officialegress.SinkCodexImagesOAuthTest
	officialEgressSinkImagesResponses            = officialegress.SinkCodexImagesResponses
	officialEgressSinkModelsList                 = officialegress.SinkCodexModelsList
	officialEgressSinkQuotaWHAM                  = officialegress.SinkCodexQuotaWHAM
	officialEgressSinkRealtimeCalls              = officialegress.SinkCodexRealtimeCalls
	officialEgressSinkRealtimeSideband           = officialegress.SinkCodexRealtimeSideband
	officialEgressSinkResponsesAnthropicCompat   = officialegress.SinkCodexResponsesAnthropicCompat
	officialEgressSinkResponsesChatCompletions   = officialegress.SinkCodexResponsesChatCompletions
	officialEgressSinkResponsesForward           = officialegress.SinkCodexResponsesForward
	officialEgressSinkResponsesPassthrough       = officialegress.SinkCodexResponsesPassthrough
	officialEgressSinkResponsesWS                = officialegress.SinkCodexResponsesWS
	officialEgressSinkResponsesWSHTTPBridge      = officialegress.SinkCodexResponsesWSHTTPBridge
	officialEgressSinkResponsesWSV2Passthrough   = officialegress.SinkCodexResponsesWSV2Passthrough
	officialEgressSinkUsageProbe                 = officialegress.SinkCodexUsageProbe
	officialEgressSinkAgentTaskRegister          = officialegress.SinkUnclassifiedAgentTaskRegister
	officialEgressSinkPATWhoAmI                  = officialegress.SinkUnclassifiedPATWhoAmI
	officialEgressSinkPrivacyAccountInfo         = officialegress.SinkWebPrivacyAccountInfo
	officialEgressSinkPrivacyDisableTraining     = officialegress.SinkWebPrivacyDisableTraining
	officialEgressSinkPrivacySubscription        = officialegress.SinkWebPrivacySubscription
	officialEgressSinkClaudeLegacyAccountTest    = officialegress.SinkClaudeLegacyAccountTest
	officialEgressSinkClaudeSetupTokenMessages   = officialegress.SinkClaudeSetupTokenMessagesInference
	officialEgressSinkClaudeSetupTokenTokenCount = officialegress.SinkClaudeSetupTokenTokenCount
	officialEgressSinkClaudeLegacyModels         = officialegress.SinkClaudeLegacyUpstreamModels
)
