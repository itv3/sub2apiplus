package service

import (
	"net/http"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/Wei-Shaw/sub2api/internal/pkg/openai"
	"github.com/google/uuid"
)

// codexUpstreamMinVersion 上游 /backend-api/codex 接受的最低 version 头：
// 若请求携带 version 且低于该值，上游直接 404（issue #3901，2026-07 实测）。
const codexUpstreamMinVersion = "0.144.0"

// codexClientVersionMaxLen 官方版本号均为短 ASCII 串，远低于此上限。
const codexClientVersionMaxLen = 64

// codexClientVersionPattern 同时允许稳定版与带预发布后缀的官方形态。
var codexClientVersionPattern = regexp.MustCompile(`^[0-9]+(\.[0-9]+){1,3}(-[0-9A-Za-z.]+)?$`)

// NormalizeCodexClientVersion 校验并归一化 Codex 客户端版本号，非法值返回空串。
// 该值会被拼进出站 User-Agent 与 version 头，必须拒绝任意字节，避免管理员误填或
// 自动同步拿到异常值时把不可控内容透给上游。
func NormalizeCodexClientVersion(version string) string {
	version = strings.TrimSpace(version)
	if version == "" || len(version) > codexClientVersionMaxLen || !codexClientVersionPattern.MatchString(version) {
		return ""
	}
	return version
}

// buildCodexCLIUserAgent 仅重建上游兼容层所需的版本段。
// OS、架构、终端和尾部客户端标识均继承已验收的 active ReleaseBundle，避免另建一套 UA 模板。
func buildCodexCLIUserAgent(version string) string {
	if version = NormalizeCodexClientVersion(version); version == "" {
		return codexCLIUserAgent
	}
	if rebuilt := openai.SetCodexUserAgentVersion(codexCLIUserAgent, version); rebuilt != "" {
		return rebuilt
	}
	return codexCLIUserAgent
}

// codexIdentityEnforcement 控制 enforceCodexIdentityHeaders 是否强制统一出站身份，
// 由 gateway.disable_codex_identity_enforcement 在服务构造时取反发布。
// 默认开启：上游在容量紧张时按客户端身份分优先级降载，被降载的请求会拿到
// HTTP 200 + 流内 server_is_overloaded，本次请求即失败；强制统一出口可确保没有
// 请求带着第三方或陈旧身份出站。关闭后退回「仅按最终 UA 配对 originator」的收口语义。
var codexIdentityEnforcement = func() *atomic.Bool {
	v := &atomic.Bool{}
	v.Store(true)
	return v
}()

// SetCodexIdentityEnforcementEnabled 发布 Codex 出站身份强制统一开关。
// enforceCodexIdentityHeaders 是所有出站路径共用的纯函数收口点，无法在热路径注入配置，
// 故由持有配置的服务在构造时发布进程级快照。
func SetCodexIdentityEnforcementEnabled(enabled bool) {
	codexIdentityEnforcement.Store(enabled)
}

// codexCanonicalUserAgentResolver 返回严格出站使用的已验收 Codex User-Agent。
// GitHub 同步值只是发现结果，不能成为该解析器的数据源。
type codexCanonicalUserAgentResolver func() string

var (
	codexCanonicalUAMu       sync.RWMutex
	codexCanonicalUAResolver codexCanonicalUserAgentResolver
)

// SetCodexCanonicalUserAgentResolver 注入已验收 User-Agent 解析器。
// 未注入或解析结果非法时回退到编译期常量 codexCLIUserAgent。
func SetCodexCanonicalUserAgentResolver(resolver func() string) {
	codexCanonicalUAMu.Lock()
	defer codexCanonicalUAMu.Unlock()
	codexCanonicalUAResolver = resolver
}

// codexCanonicalUserAgent 返回 strict wire 兼容层使用的已验收 User-Agent。
func codexCanonicalUserAgent() string {
	codexCanonicalUAMu.RLock()
	resolver := codexCanonicalUAResolver
	codexCanonicalUAMu.RUnlock()
	if resolver != nil {
		if ua := strings.TrimSpace(resolver()); ua != "" {
			return ua
		}
	}
	return codexCLIUserAgent
}

// codexOutboundIdentity 出站身份三元组，三者必须同源自洽：
// originator 与 User-Agent 首段配套（否则上游 404，issue #3901），
// version 等于 User-Agent 的版本段且不低于上游门槛。
type codexOutboundIdentity struct {
	userAgent  string
	originator string
	version    string
}

// resolveCodexOutboundIdentity 由候选 User-Agent 推导自洽的出站身份。
// candidateUA 为空时使用规范 User-Agent；推导不出官方身份时整体回退为规范 TUI 身份。
//
// 候选 UA（面板 / 账号级配置）只贡献客户端名与 OS / 架构 / 终端指纹；其版本段
// 一律按已验收 active ReleaseBundle 重建。GitHub 发现的新版本必须先完成证据采集、
// 42 项回归和显式激活，才允许改变 strict wire 身份。
func resolveCodexOutboundIdentity(candidateUA string) codexOutboundIdentity {
	canonical := codexCanonicalUserAgent()
	ua := strings.TrimSpace(candidateUA)
	if ua == "" {
		ua = canonical
	}
	originator, pairedUA, ok := openai.PairCodexClientIdentity(ua)
	if !ok {
		if originator, pairedUA, ok = openai.PairCodexClientIdentity(canonical); !ok {
			if originator, pairedUA, ok = openai.PairCodexClientIdentity(codexCLIUserAgent); !ok {
				originator, pairedUA = openai.CodexCLIOriginator, codexCLIUserAgent
			}
		}
	}
	// 生效版本只来自已验收规范身份，UA 与 version 头由此同源派生。
	version := codexClientVersionFromUA(canonical)
	if rebuilt := openai.SetCodexUserAgentVersion(pairedUA, version); rebuilt != "" {
		pairedUA = rebuilt
	}
	return codexOutboundIdentity{userAgent: pairedUA, originator: originator, version: version}
}

// codexClientVersionFromUA 取 UA 的版本段作为生效版本；
// 非法或低于上游门槛（低于则上游 404，issue #3901）时回退编译期常量。
func codexClientVersionFromUA(ua string) string {
	version := NormalizeCodexClientVersion(openai.CodexUserAgentVersion(ua))
	if version == "" || CompareVersions(version, codexUpstreamMinVersion) < 0 {
		return codexCLIVersion
	}
	return version
}

// applyOpenAICodexAuxiliaryHeaders 为 models、usage、search、PAT 等辅助端点写入
// active ReleaseBundle 的候选身份。端点专属 Header 由端点自身添加；最终 wire 若属于
// 严格路径，仍由 CodexEgressExecutor 重建并校验。
func applyOpenAICodexAuxiliaryHeaders(h http.Header) {
	if h == nil {
		return
	}
	identity := resolveCodexOutboundIdentity("")
	h.Set("User-Agent", identity.userAgent)
	h.Set("originator", identity.originator)
	h.Set("version", identity.version)
	h.Del("OpenAI-Beta")
}

// ensureCodexIdentityHeaders 补齐 OAuth（ChatGPT 内部接口）出站请求所需的 Codex 身份头。
// 已有 User-Agent 与 version 保持不变，交给紧随其后的 enforceCodexIdentityHeaders 收口。
func ensureCodexIdentityHeaders(h http.Header) {
	if h == nil {
		return
	}
	identity := resolveCodexOutboundIdentity("")
	if strings.TrimSpace(h.Get("user-agent")) == "" {
		h.Set("user-agent", identity.userAgent)
	}
	if strings.TrimSpace(h.Get("originator")) == "" {
		h.Set("originator", identity.originator)
	}
	if strings.TrimSpace(h.Get("version")) == "" {
		h.Set("version", identity.version)
	}
	h.Del("OpenAI-Beta")
}

// applyOpenAICodexProbeHeaders 为合成探测请求补齐 Codex 身份和引擎指纹。
func applyOpenAICodexProbeHeaders(h http.Header) {
	if h == nil {
		return
	}
	ensureCodexIdentityHeaders(h)
	h.Set("X-Codex-Window-ID", uuid.NewString())
}

// enforceCodexIdentityHeaders 收口 OAuth（ChatGPT 内部接口）出站请求的客户端身份头。
// 见 enforceCodexIdentityHeadersWithUA；无账号级自定义 User-Agent 时使用本函数。
func enforceCodexIdentityHeaders(h http.Header) {
	enforceCodexIdentityHeadersWithUA(h, "")
}

// enforceCodexIdentityHeadersWithUA 强制统一 OAuth 出站身份：User-Agent / originator / version
// 一律改写为网关的规范身份，客户端自报身份不参与构造。上游在容量紧张时按客户端身份分优先级
// 降载，被降载的请求会拿到 HTTP 200 + 流内 server_is_overloaded；统一出口可确保没有请求带着
// 第三方或陈旧身份出站，也天然满足 originator 与 UA 首段配套的上游校验（issue #3901）。
//
// overrideUA 是账号级自定义 User-Agent：管理员的显式配置仍然生效，但只贡献客户端名与
// OS / 架构 / 终端指纹——版本段与 originator 都由规范身份重建，不允许出现自相矛盾或陈旧的身份。
//
// 强制统一被 gateway.disable_codex_identity_enforcement 关闭时，退回「按最终 User-Agent 配对
// originator + version 门槛校正」的收口语义，供上游策略变动时回滚。
//
// 仅对携带 originator 的请求生效：compat 桥接等非 ChatGPT 内部接口路径会显式删除 originator，
// 不应被补回。需要从缺失身份头恢复的调用方应先调用 ensureCodexIdentityHeaders。
// 必须在所有 User-Agent 改写之后调用。
func enforceCodexIdentityHeadersWithUA(h http.Header, overrideUA string) {
	if h == nil || h.Get("originator") == "" {
		return
	}
	if !codexIdentityEnforcement.Load() {
		pairCodexIdentityHeaders(h)
		return
	}
	identity := resolveCodexOutboundIdentity(overrideUA)
	h.Set("user-agent", identity.userAgent)
	h.Set("originator", identity.originator)
	h.Set("version", identity.version)
}

// pairCodexIdentityHeaders 是关闭强制统一后的兜底收口：保留客户端真实身份，
// 仅保证 originator 与最终 User-Agent 首段配套、version 不低于上游门槛（issue #3901）。
func pairCodexIdentityHeaders(h http.Header) {
	originator, pairedUA, ok := openai.PairCodexClientIdentity(h.Get("user-agent"))
	if !ok {
		identity := resolveCodexOutboundIdentity("")
		originator, pairedUA = identity.originator, identity.userAgent
		h.Set("version", identity.version)
	}
	h.Set("user-agent", pairedUA)
	h.Set("originator", originator)
	if v := strings.TrimSpace(h.Get("version")); v != "" && CompareVersions(v, codexUpstreamMinVersion) < 0 {
		h.Set("version", codexCLIVersion)
	}
}
