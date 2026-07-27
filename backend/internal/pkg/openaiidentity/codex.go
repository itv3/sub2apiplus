package openaiidentity

// Codex 画像常量来自同一轮真实抓包，供模型请求与 OAuth 令牌请求共同复用，
// 避免两条路径再次出现版本、User-Agent 或 originator 漂移。
const (
	CodexVersion    = "0.145.0"
	CodexUserAgent  = "codex_exec/0.145.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.145.0)"
	CodexOriginator = "codex_exec"
)
