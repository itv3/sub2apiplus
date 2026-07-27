package openaiidentity

// Codex 画像常量来自同一轮真实抓包，供模型请求与 OAuth 令牌请求共同复用，
// 避免两条路径再次出现版本、User-Agent 或 originator 漂移。
//
// UA 的构造对应官方 login/src/auth/default_client.rs:161 get_codex_user_agent()：
//
//	prefix = "{originator}/{version} ({os_type} {os_version}; {arch}) {terminal}"
//	suffix = 仅当 USER_AGENT_SUFFIX 非空时追加，格式 " ({name}; {version})"
//
// terminal 段取 unknown 是**正确的**：官方在完全无终端时同样输出 unknown
// （terminal-detection/src/lib.rs:204 TerminalName::Unknown => "unknown"），
// 而本服务端本就没有终端。
//
// suffix 段必须缺省：USER_AGENT_SUFFIX 全仓只有 mcp-server 与 app-server 两个
// 设置点，值为**连接进来的第三方客户端**的 name 与 version。普通 codex exec 不设，
// 实测官方 UA 确无 suffix。此前带的 "(codex_exec; 0.145.0)" 是官方永不产生的组合
// ——app-server 场景下 name 等于 originator 自身既不合语义，还会被
// NON_ORIGINATING_CLIENT_NAMES 过滤。详见规格表 SPEC-HDR-005。
const (
	CodexVersion    = "0.145.0"
	CodexUserAgent  = "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown"
	CodexOriginator = "codex_exec"
)
