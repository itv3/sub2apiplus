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
// suffix 段**必须保留**：当前发布值来自 0.147.0 已验收画像；其设置机制与
// 0.145.0 历史画像一致，不能只替换主版本而遗漏 suffix 中的版本。
//
// 机制：USER_AGENT_SUFFIX 是 login/src/auth/default_client.rs:41 的全局
// LazyLock<Mutex<Option<String>>>。codex exec 内部走 app-server 协议，
// app-server 收到 initialize 时由 app-server/src/request_processors/
// initialize_processor.rs:90-91 构造 suffix、:133 写入该 static，
// 此后**该进程的所有出站请求**都带上这段 suffix。
//
// ⚠ 与 POST /backend-api/ps/mcp 无关。曾据"exec 启动时会与 MCP 端点完成
// initialize"来解释，该因果已被实测推翻：用 --disable apps 关掉 Codex Apps 后
// ps/mcp 一次都没发，而 suffix 照常出现（clean-* 四个洁净样本）。
// 两处 USER_AGENT_SUFFIX 写入点都是**接收侧**（codex 作为 MCP server /
// app-server 接收 initialize），不是 codex 去访问 ps/mcp 那条出站链。
//
// models 端点带不带 suffix **不确定**：它在启动早期发出，与 initialize 写入
// 全局 static **并发**，谁先谁后不由代码路径决定。实测四个洁净样本 2 带 2 不带，
// 且同为 codex_exec 入口的两次结果相反，可排除"按入口"或"按端点"的解释。
// responses 一律在其后，故 100% 带 suffix。
//
// ⚠ 曾一度删除本段，理由是"USER_AGENT_SUFFIX 只有 mcp-server 与 app-server
// 两个设置点，普通 codex exec 不设"。该判断有两处错：全仓实为**三个**写入点
// （另有 cloud-tasks/src/util.rs:10），且更关键的是——设置点在不在 exec 的
// 直接调用链上根本不重要，它是**进程级全局状态**，被谁设置都会影响后续全部出站。
// 当时"实测无 suffix"的依据只取了 models 端点的样本，恰好落在设置之前。
// 详见规格表 SPEC-HDR-005。
const (
	CodexVersion    = "0.147.0"
	CodexUserAgent  = "codex_exec/0.147.0 (Ubuntu 24.4.0; x86_64) unknown (codex_exec; 0.147.0)"
	CodexOriginator = "codex_exec"
)
