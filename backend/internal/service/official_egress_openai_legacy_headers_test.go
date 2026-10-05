package service

import (
	"net/http"
	"strings"
)

// 旧 Header 定型器只供历史画像对照及候选事实夹具使用，生产源码没有调用。
// 当前正式出站的 Header 由 Executor/compiler 根据冻结画像生成；历史 compact
// 夹具仍需保留原有输出，因此仅将该实现和专用常量移入测试，不改变其行为。
const (
	officialOpenAIHTTPBetaFeatures  = "remote_compaction_v2"
	officialOpenAIHTTPResponsesLite = "true"
)

func finalizeOfficialOpenAIHTTPHeaders(
	header http.Header,
	// version 来自运行上下文的 active 画像版本，不再是编译期常量：辅助端点与 WS
	// 握手的 version 头由画像槽位生成，主链若继续写死常量，升级后两者就会分叉。
	version string,
	userAgent string,
	originator string,
	identity officialOpenAIHTTPIdentity,
	isCompact bool,
	useResponsesLite bool,
	turnState string,
) {
	for _, name := range []string{
		"conversation_id",
		"session_id",
		"OpenAI-Beta",
		"X-Codex-Installation-ID",
		"x-codex-beta-features",
		"x-codex-turn-state",
		"x-codex-window-id",
		"x-codex-turn-metadata",
		"x-client-request-id",
		"session-id",
		"thread-id",
		"accept",
		"content-type",
		"user-agent",
		"originator",
		"version",
		responsesLiteHeader,
	} {
		deleteHeaderAllForms(header, name)
	}
	stripOfficialEgressInboundHostHeaders(header)

	// 这里保持 Go 的 canonical 写法，wire 上的全小写形态由官方画像 Transport 统一收口
	// （见 newOfficialEgressLowercaseHeaderRoundTripper）：官方 Codex 走 Rust hyper，
	// h1 线上 header 名一律小写，而 Go 的 Header.Set 会改写成 Session-Id / Originator。
	// 把小写化放在传输层而非此处，是为了让语义定型与 wire 形态分层，且不影响上层断言。
	if isCompact {
		// 官方 compact 走 execute（端点层不设 accept），由 reqwest 补默认 */*。
		// Go 不会自动补，因此必须显式设置——此前 Del 会导致出站彻底缺该头。
		header.Set("Accept", "*/*")
	} else {
		header.Set("Accept", "text/event-stream")
	}
	header.Set("Content-Type", "application/json")
	header.Set("User-Agent", userAgent)
	header.Set("originator", originator)
	header.Set("session-id", identity.sessionID)
	header.Set("thread-id", identity.threadID)
	if !isCompact {
		header.Set("x-client-request-id", identity.clientRequest)
	}
	header.Set("x-codex-turn-metadata", identity.turnMetadata)
	header.Set("x-codex-window-id", identity.windowID)
	header.Set("version", version)
	if !isCompact {
		header.Set("x-codex-beta-features", officialOpenAIHTTPBetaFeatures)
	}
	header.Del(responsesLiteHeader)
	if useResponsesLite {
		header.Set(responsesLiteHeader, officialOpenAIHTTPResponsesLite)
	}
	if isCompact {
		header.Set("X-Codex-Installation-ID", identity.installationID)
	}
	if turnState = strings.TrimSpace(turnState); turnState != "" {
		header.Set("x-codex-turn-state", turnState)
	}
}
