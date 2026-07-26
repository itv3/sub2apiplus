# 官方客户端双任务抓包工具

## 1. 目标与边界

本工具固定执行两套相互独立的基准任务：

```text
OAuth：Claude Code / Codex CLI ──OAuth──▶ Anthropic / OpenAI 官方平台
API：  Claude Code / Codex CLI ──Sub2API 访问 Key──▶ Sub2API 公共 HTTPS 地址
```

- OAuth 任务为“OAuth 官方客户端伪装”提供官方直连基准。
- API 任务为“API Key 官方客户端兼容”提供官方 CLI 入站基准。
- `--task all` 只按 OAuth→API 顺序执行，认证状态、运行目录、抓包文件和
  manifest 始终分离。
- 本工具不抓 Sub2API→AnyRouter 等第三方上游，也不同时抓 Sub2API ingress；
  这些属于后续候选 A/B 阶段。

截至 2026-07-26，本变更集只完成工具、离线测试、部署和 dry-run，不执行新的
OAuth/API 真实模型请求，也不执行 AnyRouter A/B。历史实证仍以
`backend/internal/service/testdata/official_egress/README.md` 为准。

## 2. 固定矩阵与隔离粒度

每套任务包含 3 个主体：

| 主体 | 传输 |
|---|---|
| `claude-http` | Claude Code HTTP |
| `codex-http` | Codex CLI HTTP |
| `codex-ws` | Codex CLI WebSocket |

每个主体分别执行 `direct`、`mitm` 两种证据和 S1、S2、S4 三个场景。因此每套
任务是 `3 主体 × 2 证据 × 3 场景 = 18` 个独立抓包单元，`all` 共 36 个。

每个抓包单元都会单独启动和停止 tcpdump 或 mitmdump，并拥有自己的 pcap、JSONL、
CLI 结果和分析文件。S1/S2/S4 不共用同一抓包文件，HTTP/WS、OAuth/API 也不会混合。

场景定义：

- S1：单轮固定回复，不允许工具调用。
- S2：同一会话连续两轮固定回复，不允许工具调用。
- S4：只允许一次固定 `printf`，严格校验工具命令、输出和最终回复。

## 3. 两类证据分别能证明什么

### 3.1 direct

direct 在 `capture-cli` 自身网络命名空间运行 tcpdump，BPF 只允许目标域名在执行前
解析得到的 IP 和实际 HTTPS 端口。pcap 必须包含目标 SNI 的 ClientHello，否则该单元失败。

规范化结果保留以下客户端 offer，并保持顺序：

- cipher suites、extension types、offered ALPN；
- supported groups、EC point formats、signature algorithms；
- supported versions、key share groups、PSK modes；
- GREASE 值统一为占位符，目标 IP、SNI、端口和帧号不参与跨目标画像比较。

direct 不能读取加密后的 Header、Body 或 WS 帧。ClientHello 的 ALPN 是客户端 offer，
不是服务端协商结果，也不能单独证明实际走了 HTTP 还是 WebSocket。原始 pcap 保留完整
TCP 流；连接次数、重试和复用如需作为结论，必须另行分析原始 pcap，当前自动比较只比较
去重后的 ClientHello 结构，并单列原始观察次数。

### 3.2 MITM

MITM 保持同一 HTTPS Base URL，只给当前 CLI 子进程注入 forward proxy 与专用 CA，记录
目标 allowlist 中的 HTTP/WS。规范化结果用于比较 URL、HTTP 版本、Header 和 JSON 结构：

- 授权头、Cookie、动态身份、查询值和正文文本会脱敏；
- HTTP 单元必须出现精确模型路径的 POST，且不得出现目标 WS 帧；
- WS 单元必须出现客户端 WS 帧，且不得发生 Responses POST 回退；
- 原始 JSONL 仍含请求/响应正文，属于私有敏感材料。

direct 与 MITM 使用相同客户端版本、模型和场景，但两者是独立请求，只能互补，不能互相替代。

## 4. 认证与执行安全

1. API Key 只从运行时环境变量读取，不进入参数、dry-run 或规范化证据。
2. API 任务使用运行目录内独立 `CLAUDE_CONFIG_DIR`、`CODEX_HOME`，不读取 OAuth 状态。
3. OAuth 任务使用容器内既有登录状态，并从子进程环境移除 API Key、Base URL、代理和未知变量。
4. Codex 使用最小环境、只读权限 Profile 和可信 PreToolUse hook；S1/S2 拒绝 Bash，
   S4 只允许逐字匹配的固定 `printf`。hook 文件在请求前校验所有者和权限，并把
   SHA-256 记录到 manifest 供部署来源核对。
5. Claude S1/S2 禁用工具；S4 只允许固定 Bash 命令。两个客户端均拒绝额外工具、伪造
   marker、错误输出和秘密进入 stdout/stderr/last-message。
6. `--execute` 必须同时提供 `--acknowledge-live-requests`。
7. 专用运行根为 0700，文件为 0600；任务使用全局锁，拒绝覆盖既有 run。
8. tcpdump、mitmdump 和 CLI 都使用独立进程组；Linux 上附加父进程死亡信号。
   SIGINT/SIGTERM 会先解栈清理，再写入恢复账本和 manifest。
9. Codex OAuth S2 为了跨进程 `resume`，会在专用 capture 容器既有 OAuth
   `CODEX_HOME` 中留下会话/rollout 元数据；它不属于 run inventory 或 Key 精确扫描范围。
   因此 OAuth 抓包必须使用专用容器，工具也不会盲删共享 sessions。

秘密扫描有明确边界：

- API 任务会对编排器已知的访问 Key 做文本产物精确值扫描；发现后立即替换并使任务失败。
- OAuth 凭据从未读入编排器，无法做精确值扫描；manifest 会记录 `performed=false` 和限制说明。
- pcap 等二进制材料不在文本精确值扫描范围内，始终按原始私有证据管理。

## 5. 固定版本与执行前预检

当前默认基线：

| 资产 | 固定值 |
|---|---|
| Claude Code | `2.1.220` |
| Claude SHA-256 | `674f61f20ff306f3100cf9200e4c36c4b70278b5bef2884549819b942a89c863` |
| Codex CLI | `0.145.0` |
| Codex SHA-256 | `a2a05dafaa1acb002a45eaec0a462de5b13694fcfcd7bc43305f14781ce7be14` |
| Profile | `official-cli-2.1.220-codex-0.145.0-baseline-v1` |
| Vircs 镜像声明 | `oauth-egress-capture-capture-cli@sha256:3438c4e0909d7401ff8e076a985258608a8f031629e65262db16c1979ab1771c` |

真实执行会在第一条模型请求前检查：客户端精确版本和哈希、抓包工具、Codex hook、MITM
addon、CA、目录权限、tshark 全部 TLS 字段、目标 DNS/IP、既有 OAuth 登录状态以及历史恢复账本。

`--runtime-image` 只是写入 manifest 的调用方声明。容器内无法独立反查 Docker image digest，
部署者必须在 Vircs 宿主机用 `docker inspect` 复核容器实际 image ID；manifest 会明确记录
`runtime_image_verified=false`，不能把该声明当作已验证证据。

升级客户端时至少同步更新：

- `--expected-claude-version`、`--expected-codex-version`；
- `--expected-claude-sha256`、`--expected-codex-sha256`；
- `--profile-version` 与经宿主机核实的 `--runtime-image` 声明；
- 如果 CLI 参数、hook 事件或传输能力变化，先更新离线契约测试，再执行新基准。

## 6. Vircs 部署与 dry-run

源码部署到宿主机 `/root/oauth-capture/tools/official_client_capture`；该目录在
`capture-cli` 内对应 `/capture/tools/official_client_capture`。必须保留 `tools` 包层级，
否则脚本模式无法导入 `tools.official_client_capture`。

```bash
ssh Vircs 'install -d -m 0700 /root/oauth-capture/tools'

rsync -a --no-owner --no-group \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  tools/official_client_capture/ \
  Vircs:/root/oauth-capture/tools/official_client_capture/

ssh Vircs 'docker exec capture-cli stat -c "%U:%G %a %n" \
  /capture/tools/official_client_capture/hooks/exact_bash_hook.py \
  /capture/tools/official_client_capture/addons/mitm_capture.py'

ssh Vircs 'docker exec capture-cli python3 \
  /capture/tools/official_client_capture/capture.py \
  --task all \
  --dry-run \
  --sub2api-base-url https://sub2api.example.com'
```

dry-run 只校验参数并输出脱敏计划。它不读取 Key、不检查 OAuth 登录或 DNS、不校验客户端
二进制和镜像、不创建目录或锁、不启动进程，也不发送网络或模型请求。因此 dry-run 成功只说明
矩阵和参数可构造，不代表真实执行预检或抓包一定通过。

## 7. 真实执行

真实执行需要用户另行确认。在 Vircs 交互终端临时读取 Sub2API 访问 Key；不要把 Key 发到
聊天、命令参数或脚本：

```bash
read -rsp 'Sub2API access key: ' SUB2API_CAPTURE_API_KEY
export SUB2API_CAPTURE_API_KEY

docker exec -i \
  -e SUB2API_CAPTURE_API_KEY \
  capture-cli \
  python3 /capture/tools/official_client_capture/capture.py \
  --task all \
  --execute \
  --acknowledge-live-requests \
  --sub2api-base-url https://sub2api.example.com

unset SUB2API_CAPTURE_API_KEY
```

如需严格分成两次人工操作，可分别使用 `--task oauth` 和 `--task api`；它们与 `all` 中的
两个顺序任务具有相同隔离边界。

## 8. 产物结构

```text
/capture/runs/official-client/
├── oauth/oauth-<batch-id>/
│   ├── manifest.json
│   ├── recovery.json
│   ├── direct/<subject>/<scenario>/traffic.pcap
│   ├── mitm/<subject>/<scenario>/*.jsonl
│   ├── results/<evidence>/<subject>/<scenario>/
│   └── analysis/<evidence>/<subject>/<scenario>.json
└── api/api-<batch-id>/
    ├── manifest.json
    ├── recovery.json
    ├── state/                 # API 隔离配置，不含 OAuth 凭据
    ├── direct/<subject>/<scenario>/traffic.pcap
    ├── mitm/<subject>/<scenario>/*.jsonl
    ├── results/<evidence>/<subject>/<scenario>/
    └── analysis/<evidence>/<subject>/<scenario>.json
```

`analysis/**/*.json` 和场景 `summary.json` 是脱敏结构证据；pcap、MITM JSONL、CLI 事件、
日志、状态目录及 last-message 均按 `raw_private` 管理，不能提交 Git。

## 9. 中断与人工恢复

`recovery.json` 原子记录当前抓包 PID、PGID、端口、场景和输出目录。清理失败时会保留
`active_resource`，后续运行发现不干净账本即拒绝开始。工具不会根据历史 PID 自动杀进程，
因为宿主重启后 PID 可能已被复用。

人工恢复顺序：

1. 读取失败 run 的 `recovery.json`，记录 PID、PGID、端口和角色。
2. 用 `ps`/`ss` 核实该 PID/PGID/端口仍属于本次 tcpdump 或 mitmdump；身份不一致时禁止杀进程。
3. 若确有残留，按角色发送正常停止信号，确认进程和 MITM 端口均已消失。
4. 保留失败 manifest，把整个旧 run 移到权限 0700、位于运行根之外的恢复归档目录；不要只删
   或伪造 `recovery.json`。确认运行根不再包含该不干净账本后才能重试。

## 10. 后续 AnyRouter A/B

本工具只产生 API 官方 CLI 基准。用户参与外部站点测试后，另行抓取
“非官方客户端→Sub2API→AnyRouter”的候选出站证据，并按相同主体、传输、场景和证据类型比较：

```bash
python3 tools/official_client_capture/compare.py \
  baseline.json candidate.json --output diff.json
```

不能跨 HTTP/WS 或 direct/MITM 比较，也不能把 OAuth 基准当作 API mimic 候选。

## 11. 本地测试

测试只使用标准库和合成数据，不联网、不读取真实 Key，也不启动 tcpdump、mitmdump 或模型请求：

```bash
make test-capture-tools
```
