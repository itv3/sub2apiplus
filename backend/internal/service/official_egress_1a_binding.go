package service

import (
	"context"

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

const (
	officialEgressSinkAdminTestCompact         = officialegress.SinkCodexAdminTestCompact
	officialEgressSinkAdminTestResponses       = officialegress.SinkCodexAdminTestResponses
	officialEgressSinkAlphaSearchDirect        = officialegress.SinkCodexAlphaSearchDirect
	officialEgressSinkAlphaSearchPATFallback   = officialegress.SinkCodexAlphaSearchPATFallback
	officialEgressSinkFilesBlobUpload          = officialegress.SinkCodexFilesBlobUpload
	officialEgressSinkFilesRegister            = officialegress.SinkCodexFilesRegister
	officialEgressSinkImagesOAuthTest          = officialegress.SinkCodexImagesOAuthTest
	officialEgressSinkImagesResponses          = officialegress.SinkCodexImagesResponses
	officialEgressSinkModelsList               = officialegress.SinkCodexModelsList
	officialEgressSinkQuotaWHAM                = officialegress.SinkCodexQuotaWHAM
	officialEgressSinkRealtimeCalls            = officialegress.SinkCodexRealtimeCalls
	officialEgressSinkRealtimeSideband         = officialegress.SinkCodexRealtimeSideband
	officialEgressSinkResponsesAnthropicCompat = officialegress.SinkCodexResponsesAnthropicCompat
	officialEgressSinkResponsesChatCompletions = officialegress.SinkCodexResponsesChatCompletions
	officialEgressSinkResponsesForward         = officialegress.SinkCodexResponsesForward
	officialEgressSinkResponsesPassthrough     = officialegress.SinkCodexResponsesPassthrough
	officialEgressSinkResponsesWS              = officialegress.SinkCodexResponsesWS
	officialEgressSinkResponsesWSHTTPBridge    = officialegress.SinkCodexResponsesWSHTTPBridge
	officialEgressSinkResponsesWSV2Passthrough = officialegress.SinkCodexResponsesWSV2Passthrough
	officialEgressSinkUsageProbe               = officialegress.SinkCodexUsageProbe
	officialEgressSinkAgentTaskRegister        = officialegress.SinkUnclassifiedAgentTaskRegister
	officialEgressSinkPATWhoAmI                = officialegress.SinkUnclassifiedPATWhoAmI
	officialEgressSinkPrivacyAccountInfo       = officialegress.SinkWebPrivacyAccountInfo
	officialEgressSinkPrivacyDisableTraining   = officialegress.SinkWebPrivacyDisableTraining
	officialEgressSinkPrivacySubscription      = officialegress.SinkWebPrivacySubscription
)
