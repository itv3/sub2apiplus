# Codex 官方 0.165.0 真实 WS 帧（非正式实采）

来源：`/Users/czs/Developer/sub2apiplus-evidence/codex-cli/0.165.0/informal/runs/<场景>/raw-masked.jsonl.gz`（X 路在 ARM64 上用官方 0.165.0 TUI 连假上游实采，
凭据全是假的，授权类请求头已打码）。这里只摘主线程那条连接（第一条握手 conn=1）的握手头与客户端发出的 response.create 文本帧，
文本是解压后的原始字节。每个文件是 gzip 的 JSONL：第一行 `{"kind":"handshake","headers":[[名,值],…]}`，其后每行 `{"kind":"frame","frame_no":n,"text":"…"}`。

- `m1_ws_model_switch`：gpt-6.1-sol → gpt-6-astra → gpt-6.1-sol。帧 1 预热，帧 2 首个业务帧，帧 3 换模型整段重放，帧 4 换回整段重放。
- `c1_ws_compact_continue`：帧 1 预热，帧 2 首个业务帧，帧 3 增量，帧 4 压缩帧（compaction_trigger 增量），帧 5 压缩后新窗口整段重放（窗口号 :0 变 :1），帧 6 增量。
- `e6_ws_effort_unsupported_model`：目录里不支持 reasoning effort update 的 gpt-5.6-sol 改 effort：帧 1 预热，帧 2 业务帧，帧 3 整段重放（reasoning_changed）。

- `t1_ws_title_thread`：每个 TUI 会话启动后另开的 gpt-5.6-luna 标题线程（独立 WS 连接）：帧 1 预热，帧 2 业务帧（预热之后在同一连接上发的全量帧，无 previous_response_id、无 generate，因为工具目录与预热不同）。

换模型、压缩、改 effort 引起的整段重放都在原连接上，握手只有一次，握手头里的 routing hint 与 window id 保持旧值。

- `c3_http_compact_continue`（HTTP，非 WS 帧）：官方 HTTP 压缩续聊会话里 7 个 `/backend-api/codex/responses` 请求的 `client_metadata`（含压缩请求）。只留身份与回合元数据，
  请求正文已略去；原始记录里已打码的安装 id 换成固定的合成 UUID（turn metadata 内的 installation_id 同步替换）。每行 `{"kind":"http_request","index":n,"request_kind":…,"input_types":[…],"client_metadata":{…}}`。
  官方事实：9 个键（含 `root_turn_id`、`guardian_credits_requested`、`mcp_attribution`），根线程上 `root_turn_id` 等于 `turn_id`。生成脚本在 local-analysis/opt-0165/scratch/W3/make_http_fixture.py。
- `h2_http_probe_effort_compaction`（HTTP）：同样格式，来自“名叫 OpenAI、不支持 WS 预热”的自定义 provider 的 HTTP 会话（第 5 个请求是压缩请求，第 6 个是压缩后窗口 1 的回合请求）。该 provider 的回合请求体不带 access_programs，
  所以 access_programs 的有无只用 c3 对照；压缩请求的 turn metadata 形态两份实采一致。
- 两份 HTTP 夹具另有 `top_level_keys`（请求体顶层键序）与 `input_types`（input 末三项类型）两个字段，供压缩请求判据使用；生成脚本同上，参数是场景目录名与输出路径。
