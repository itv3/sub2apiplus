# 升级前行为验收套件

## 一、目的

升级（换 Codex 画像版本、换网关镜像）验收历来只验“出站字节像不像官方”，没有验“上游行为”。v0.2.18-3／4／5 三次修同一个
“跨模型旧会话 WebSocket 断流”，每次都漏了一块：

| 漏掉的盲区 | 后果 |
| --- | --- |
| 假上游在 `error` 后带了终态（`response.failed`） | 测不出“真实上游只发 `error`、既不发终态也不断连”这一形态 |
| Go 测试走 service 层，绕过 handler 的内存准入 | 测不出旧连接占着共享内存预算 |
| 没有大历史首帧 | 测不出按首帧体积加权的预算占用 |
| 没有把共享预算压到只够一个连接 | 测不出“重连被准入拒绝” |
| 只有一个失效项 | 测不出“逐项剥离用尽客户端 5 次重连” |

本套件把这些盲区固化成 8 个场景：独立库、假上游冒充 `chatgpt.com`、测试 CA 烘进网关派生镜像、可压预算、可只发 `error`，
经由**真实网关容器的完整 handler 链路**跑完。**每次换网关镜像（含升级、热修）发版前跑一遍**，对新镜像要求全部通过。
对 v0.2.18-4 镜像跑 `--expect accident`，则要求复现事故（`websocket_request_memory_rejected` 多次），用来证明套件本身没有退化成“什么都测不出”。

## 二、前置

- 一台带 docker（含 `docker compose` 插件）的 Linux 主机；本仓库里实际用的是 ARM64 测试机。主机上要有 `python3`（3.8 以上，只用标准库）、`openssl`、`curl`。
- 本机要有被测镜像（标签或 `标签@摘要` 均可）；没有时 `run.sh` 会尝试 `docker pull`，拉不到就停下，不会自己构建镜像。
- 基础镜像 `postgres:18-alpine`、`redis:8-alpine`、`alpine:3.21` 本机已有则直接用，没有会拉取；构建假上游镜像时需要能访问 alpine 软件源（`apk add python3`）。
- 根盘门槛：已用不超过 69%、可用不少于 40 GiB（`BHV_MAX_USE_PCT`、`BHV_MIN_AVAIL_GIB` 可调，后者设 0 关闭）。起跑前后各量一次并打印。套件自身占用很小（PG 用 tmpfs，帧文件与测试 CA 在临时工作目录，跑完删除）。
- 共享主机上先确认没有别的重负载任务（后台验证、全量门禁）在跑；套件峰值约 1 个 postgres + 1 个网关 + 少量 Python，但会反复重建网关。
- 默认占用：端口 `127.0.0.1:28190`、网络 `172.31.78.0/24`、名字前缀 `bhvacc-`。冲突时用 `BHV_PORT`、`BHV_NET_PREFIX`、`BHV_PROJECT`（必须以 `bhv` 开头）改。

## 三、用法

**一条命令**起栈、跑完、拆栈、删派生镜像（在 docker 主机上）：

```
bash run.sh ghcr.io/itv3/sub2apiplus:0.2.18-5                      # 要求全部场景通过
bash run.sh ghcr.io/itv3/sub2apiplus:0.2.18-4 --expect accident    # 对事故版本要求复现事故特征
bash run.sh <镜像> --only S3,S4                                    # 只跑部分场景
```

从开发机远程跑（把套件同步到远端目录再执行；远端默认目录 `/root/behavior-acceptance`）：

```
bash remote_run.sh ARM64 ghcr.io/itv3/sub2apiplus:0.2.18-5
BHV_DETACH=1 bash remote_run.sh ARM64 <镜像> [选项...]   # 后台跑（远端 setsid -f），日志最后一行是 EXIT=<码>
```

选项：`--expect pass|accident`、`--only`、`--out 结果目录`、`--keep`（不拆栈，调试用）、`--skip-log-checks`、`--no-prod-guard`；`bash run.sh --cleanup` 清理上次异常中断的残留；`bash run.sh --help` 看全部说明。

**退出码**（最后一行固定是 `EXIT=<码>`）：0 达到预期；1 有场景未达预期或“现网未动”对比不一致；2 参数或前置不满足；3 栈不健康（冒烟失败，不是被测镜像的判据问题）。

**结果目录**（默认 `./bhv-out-<时间戳>`）：`scenarios-<模式>.log`（场景完整输出）、`results-<模式>.json`（逐断言结果与事故特征证据）、`stack-up.log`、
`gateway-logs/`（每次重建网关前的完整日志，以及每个会话窗口里与套件相关的日志行）、`prod-before.txt`／`prod-after.txt`／`prod-diff.txt`（现网未动对比）。

**每个场景前都会重建网关容器**：清空网关内存里的失效记录、旧版本残留的占预算连接，场景之间互不污染（旧版本的“旧连接永不关闭”会一路堆积，不重建会把后面的场景全部污染）。

## 四、场景表与判据

场景表由 `scenarios.py` 的 `SCENARIOS` 生成（`python3 -B scenarios.py --print-table`），单元测试核对本表与代码逐字一致；改场景时先改代码再重新生成本表。

<!-- scenario-table:begin -->
| 编号 | 场景 | 预算档 | 通过判据 | 事故特征（v0.2.18-4） | 事故必现 |
| --- | --- | --- | --- | --- | --- |
| S1 | 只发 error 不发终态（only-error） | default | error 后服务端以 1000 关闭连接；重连 1 次即 completed；无内存准入拒绝、无 proxy_failed；失效密文被清、有效密文保留 | error 后连接一直不关闭（旧连接占着内存预算） | 是 |
| S2 | 大历史首帧（不小于 3MiB）（big-history） | default | 首帧不小于 3MiB 仍满足 S1 全部判据；大段消息原样转发（出站帧逐项等于预期） | error 后连接一直不关闭，且每个旧连接按大历史加权占预算 | 是 |
| S3 | 预算压到只够一个连接（budget-one-conn） | tight | 预算 64MiB、首帧 3.0/3.5/4.0MiB、2 个失效项：重连 1 次即 completed，websocket_request_memory_rejected 为 0 | websocket_request_memory_rejected 多次，重连用尽 5 次仍无 completed（客户端只见 1013 关闭） | 是 |
| S4 | 两个以上失效项（multi-bad-items） | default | 6 个失效项 + 8 个有效项：一次拒绝记完全部失效项，重连 1 次即 completed，出站帧只缺失效项密文 | 逐项剥离：6 个失效项需要 7 次连接，客户端 5 次用尽仍无 completed | 是 |
| S5 | 压缩项被拒（compaction-rejected） | default | 压缩项密文被拒：每次都给客户端明确的 invalid_encrypted_content 错误，压缩项从不被静默删除，不 1008，error 后服务端 1000 关闭 | error 后连接一直不关闭 | 是 |
| S6 | 已输出内容后再来 error（output-then-error） | default | 先输出文字再只来 error：不做任何清理重放（无 strip/entry），重连仍得到上游错误，不 1008 | 无（行为在各版本一致，属回归守卫；若出现 strip/entry 即对已输出请求静默重放） | 否 |
| S7 | 并发（concurrency） | default | 3 路并发 × 4 个会话（首帧约 1MiB）：12 个会话各自 1 次重连即 completed，互不串扰，无内存准入拒绝 | 12 个会话的首次连接 error 后都不关闭（旧连接堆积占预算） | 是 |
| S8 | 账号状态不变（account-state） | default | 整套运行前后账号行（status、schedulable、错误与限流／过载／临时不可调度字段）与健康熔断相关 Redis 键完全不变 | 无（若 error 被误记为账号故障，会在账号行或熔断键上留痕） | 否 |
<!-- scenario-table:end -->

- **预算档**：`default` 是现网同款准入（预算 256MiB、单请求上限约 37MiB、放大 6.11、固定 25MiB）；`tight` 是预算 64MiB、单请求上限 5MiB，只够一个 3～4MiB 大历史连接（权重＝正文×6.11＋25MiB，约 44～49MiB，两个放不下）。
- **假上游的默认应答就是事故形态**：对失效密文只发 `error`（HTTP 400 形态、`invalid_encrypted_content`、片段格式与线上一致），不发 `response.failed`，也不断连。需要别的形态时才在 `ctl.json` 里加开关（`emit_failed`、`output_first`、`close_after_error`、`drop_next` 等，见 `wsmock.py` 文件头）。
- **客户端按 Codex 0.165 的方式重连**：收到 `error` 或 Close 都立即重连（放弃旧 TCP），连同首次最多 5 次连接；`error` 之后等 3 秒（`BHV_HOLD`）看服务端是否关闭，不关闭记为 `held_after_error`。
- **通过判据**分两类：行为类断言（客户端看到的事件、关闭码、连接次数、假上游收到的出站帧、`websocket_request_memory_rejected`／`websocket_proxy_failed` 计数、账号快照）和日志类断言（带 `[日志]` 标记，依赖网关日志文案，如 `stage=turn_end close_status=1000`、`marked=6`、`reason=compaction_item`）。新版本改了日志文案时，可先用 `--skip-log-checks` 放行日志类，再回来改 `scenarios.py` 里对应的 `log_check` 断言。
- **事故特征**每个场景各有一个判定（`Recorder.set_signature`），用于 `--expect accident`：标“必现”的场景必须看到事故特征才算复现成功，S6、S8 是回归守卫，不要求。
- **出站帧核对**用语义视图比较（忽略网关补的开发者基础指令与内部元数据）：消息正文、角色、工具参数与配对 ID、有效推理项密文、压缩项必须原样；失效项密文被清，清完的空推理壳删除或保留均可（0.2.18-5 保留 `{"type":"reasoning","summary":[]}`，真实 GPT 接受它，见原会话复测）。

## 五、隔离与清理

- **只创建、只删除名字以 `<项目名>-` 开头的东西**：4 个容器（`-pg`、`-redis`、`-mock`、`-gw`）、1 个网络（`-net`）、2 个派生镜像（`-gw:run`、`-mock:1`）。被测镜像本身、基础镜像一律不删。项目名必须以 `bhv` 开头，`run.sh` 和 `stackctl.sh` 都在碰 docker 之前校验，防止误指向现网。
- **不碰现网**：不读、不复制现网容器的环境变量（网关环境变量按 compose 公开项自己写，密码和密钥全是本次随机值）；数据库是 tmpfs 上的一次性 PG；compose 只挂工作目录（`/tmp/<项目名>.work.*`，里面有测试 CA 私钥、`gw.env`、`state.json`，拆栈时整目录删除）。
- **“现网未动”前后对比**（`prod-guard`，默认开，同机没有现网容器时自动跳过）：只用 `docker inspect`（容器 ID、启动时间、重启次数、镜像）和一次**只读事务**的 SQL（`PGOPTIONS=-c default_transaction_read_only=on`）取现网库 users／groups／api_keys／accounts／account_groups／settings 的行数与 accounts 状态校验和，起跑前一次、拆栈后一次，不一致或取数失败都判红。现网容器名用 `BHV_PROD_CONTAINERS`、库容器用 `BHV_PROD_PG` 改。
- **异常中断**（Ctrl-C、SSH 掉线、栈起不来）：`run.sh` 的 `EXIT` 陷阱会拆栈并删工作目录；万一进程被 `kill -9`，执行 `bash run.sh --cleanup` 清残留并释放锁（锁是 `/tmp/<项目名>.lock`）。
- 套件的 Python 一律带 `-B`，不在源码目录留 `__pycache__`。

## 六、何时跑、耗时

- 何时：每次**网关镜像要发版前**（含 Codex 升级、上游合并、热修），对候选镜像跑 `bash run.sh <镜像>`，全过才能发；改了套件本身（场景、假上游）时，再对上一个已知事故镜像跑一次 `--expect accident`，确认仍能复现。
- 耗时（ARM64，实测）：镜像都在本机时，对 v0.2.18-5（全过）整条命令约 1.5 分钟（起栈约 15 秒、场景约 70 秒、拆栈约 5 秒）；对 v0.2.18-4（`--expect accident`）约 3 分钟（旧连接不关闭，每次 error 后要多等 3 秒确认，S4 逐项剥离 5 次连接最慢）。每个场景前重建网关约 10 秒，已计入。首次在一台新主机上还要加上拉取基础镜像和构建假上游镜像的时间。

- **实测基线（2026-10-31，ARM64）**：对 `ghcr.io/itv3/sub2apiplus:0.2.18-5`（revision `2ac8e0c59a`）8 个场景全部通过，网关日志里 `websocket_request_memory_rejected` 为 0；
  对 `ghcr.io/itv3/sub2apiplus:0.2.18-4`（revision `551ddec120`）`--expect accident` 达到预期：S3 三个体积各出现 4 次 `websocket_request_memory_rejected`（合计 12 次）、5 次重连用尽无 `response.completed`，
  S1／S2／S5／S7 的 `error` 后连接不关闭，S4 的 6 个失效项逐项剥离 5 次连接用尽。两次运行前后现网容器 ID／启动时间／重启次数与现网库行数／校验和完全一致，根盘 64%（可用 51.5 GiB）不变，拆栈后无 `bhvacc-*` 残留。

## 七、已知限制

- **假上游不是真 GPT**：证明不了真实 GPT 是否接受清掉密文后剩下的空推理壳，只有原会话复测才知道。
- **一次清完的结构假设**：S1～S4 里“失效项＝带非空 `content` 的别家推理项、有效项不带 `content`”，是对线上现象的建模；官方推理项若也带 `content`，网关会误清，本套件的有效项不带 `content`，测不出。
- **已有 token 输出之后只来 `error` 不来终态**（S6）：网关仍不结束连接，这是原有行为，S6 只守住“不做清理重放”，没有要求它关闭连接。
- **失效记录在进程内存**：网关重启后丢失，该会话首次请求会再重连 1 次；每个场景前重建网关正好利用这一点，不测重启后的自愈（那属于另一个场景，已确认不做）。
- **账号与分组是套件自己造的**：靠 SQL 直接写表（`setup_data.py`），表结构随上游迁移变化时要跟着改；管理 API 需要先确认合规声明，所以不走它。
- **日志类断言依赖日志文案**（见第四节）；行为类断言不依赖。
- **只覆盖 OpenAI OAuth（官方出站链路）与 OpenAI API Key 两类账号的 WebSocket 路径**，不含 Claude、Gemini、HTTP 路径；换版本若引入新的上游错误类型，要加新场景而不是改旧场景。
- 假上游和 PG／Redis 镜像来自本机缓存或公网；ARM64 无法访问外网时，需要先把镜像准备好。

## 八、文件清单

| 文件 | 作用 |
| --- | --- |
| `run.sh` | 一条命令入口：前置检查、现网快照、起栈、跑场景、拆栈、前后对比 |
| `stackctl.sh` | 栈管理：起栈（测试 CA、派生镜像、compose、造数据）、按预算档重建网关、按前缀拆栈 |
| `compose.yml` / `Dockerfile.mock` | 独立栈定义 / 假上游镜像 |
| `wsmock.py` | 假上游（`plan_response` 是纯函数，其余是网络与文件副作用） |
| `wsclient.py` | 模拟 Codex 的 WebSocket 客户端 |
| `lib.py` | 公共库：历史构造、日志解析、会话驱动、判据记录器、账号快照比对 |
| `scenarios.py` | 场景表与 8 个场景实现 |
| `setup_data.py` | 在一次性测试库里造用户、分组、Key、账号 |
| `remote_run.sh` | 同步到远端 docker 主机并运行 |
| `../official_client_capture/tests/test_behavior_acceptance.py` | 离线单元测试（不依赖网络与 docker） |
