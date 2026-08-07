# Codex CLI 0.145.0 → 0.147.0 P0 预检报告

> 状态：P0 与全部最小修复已完成；允许在 Vircs 环境创建正式 Campaign
> 执行日期：2026-08-07
> 执行基线：commit `136cef5c5035aeb9b9fdc75bccb2081e17d62cdf`
> 实施依据：[`CODEX_CLI_0145_TO_0147_UPGRADE_PLAN.md`](CODEX_CLI_0145_TO_0147_UPGRADE_PLAN.md) §5

## 1. 结论

P0 未发送真实请求、未创建正式 Campaign、未修改生产画像。健康基线全部通过；使用真实
0.147 源码和官方 Linux CLI 身份完成的预检确认了 12 项阻断，现已按第 7 节逐项修复、
独立复核并提交。P0 总复验通过，可以进入实施计划 §6。

原先怀疑的三处 baseline `0.145.0` 默认值不是 `plan` 静默回退。原始 `plan` 的首个真实
失败是场景清单绑定的 SPEC 第二章摘要过期；仅在临时副本修正该摘要后，42 条规则和 25
个任务全部映射，临时 Campaign 成功到达 `planned`。

## 2. 执行边界与环境

- 仓库在 P0 开始时干净，HEAD 为 `136cef5c…`，tree 为 `2a951a36…`。
- 主机为 macOS 26.6.1 / arm64，Go 1.26.4，Python 3.14.5。
- 所有预检产物位于 `/private/tmp/codex-0147-p0.TIX7d2`，均为 `preflight-only`。
- 未使用 `--acknowledge-live-requests`，未访问 OAuth/API Key，未切换 Active/Previous。
- 正常 Go 门禁统一使用临时可写 `GOCACHE`；缓存 mutation 仅用于判据自测。

## 3. 冻结的预检输入

| 输入 | 身份 |
|---|---|
| 0.145 源码 | tag `rust-v0.145.0`；commit `25af12f7…`；Cargo.lock `e0843448…` |
| 0.147 源码 | tag `rust-v0.147.0`；commit `be6e8eac…`；git tree `3828c818…`；Cargo.lock `eeab4e9d…` |
| 0.147 源码扫描 | tree digest `1909e288…`；TLS dependency digest `8b5b1f09…` |
| 官方 asset | `codex-x86_64-unknown-linux-musl.tar.gz`；archive SHA-256 `0246e2e7…` |
| 解压后 CLI | x86_64 static PIE ELF；SHA-256 `cb0a1556…` |
| Sigstore bundle | SHA-256 `8ea31ab7…`；Rekor subject hash 与解压后 CLI 一致 |
| baseline evidence | 无插件官方目录，共 117 个文件；inventory digest `26f393d3…` |
| baseline manifest | SHA-256 `9bafb96d…` |
| 预检 runtime image | 历史 Vircs 引用 `oauth-egress-capture-capture-cli@sha256:3438c4e0…` |
| 工具身份 | 80 个受管文件；集合摘要 `766d31df…` |

限制：本机没有 Docker，不能运行 Linux ELF，也不能复核 Vircs 镜像的 RepoDigest。正式
`plan` 和官方取证必须在 Ubuntu 24.04 / x86_64 的 Vircs 抓包环境完成：先执行实际
`codex --version`、二进制 SHA-256 和 `docker inspect`。在本机创建正式 Campaign 会把
`darwin/arm64` 错误冻结为官方运行平台，禁止这样做。

## 4. P0-A：基线与 GOCACHE mutation

| 场景 | 结果 |
|---|---|
| 可读写隔离 GOCACHE | `make test-capture-tools`：326 passed、3 skipped；`make check-egress-spec` 全绿 |
| 完全不可写 GOCACHE | `go list` 和扫描器均退出 1，明确报告 build cache permission denied |
| 单个已存在 cache index 不可读 | `go list -e` 退出 0，产生 `./...` 错误包：`p.Errors=1`、GoFiles=0、CompiledGoFiles=0；扫描器却退出 0 并报告 bootstrap 通过 |

阻断 `P0-A-ENV01`：局部缓存失败会产生假绿。`collectTypecheckFallback` 面对无 GoFiles 的
错误包没有可登记文件，顶层扫描又未拒绝该 package error。必须让这种加载不完整明确
fail-close，并补健康、顶层失败和局部失败三组回归测试。

## 5. P0-B：plan 加载预检

原始命令使用 `baseline=0.145.0`、`target=0.147.0`、真实源码、解压后 CLI 摘要、
official-only baseline evidence 和历史不可变镜像引用，退出 1：

```text
升级审计失败：场景清单规格第二章摘要不一致。
```

当前 SPEC 第二章摘要为 `07dafb94…`，场景清单仍冻结 `81f0a20f…`。只在临时场景副本将
该值改为 `07dafb94…` 后，`plan` 退出 0：42 条规则、25 个任务，官方和候选均无未映射
规则；工具身份仍为 80 项 `766d31df…`。

阻断 `P0-B01`：更新 baseline scenario 的规格摘要，并新增摘要扇出完整性测试。正式
Campaign 前还必须在 Vircs 冻结实际平台、模型、账号非秘密 ID、镜像和恢复坐标。

## 6. P0-C/D：异版本与候选工具结果

### 6.1 异版本画像

机器扫描 active Snapshot 得到 34 个 `0.145.0` 行为坐标。完整替换后得到 34 个
`0.147.0`、0 个旧坐标；临时 Profile digest 为 `5b209c24…`，blob SHA-256 为
`26009b10…`。该资产只用于 mutation，不是目标画像。

完整异版本目录首先在 SinkCatalog 初始化失败：变更集 4 transport 过渡收据要求 Active
和 Previous 的 executable 都等于同一个 `0.145` transport ID。为继续探测下游，另建了
明确标记的临时旁路画像，仅保留 19 个历史 transport ID；它不具备正式证据资格。

下游结果：

| ID | 实证结果 |
|---|---|
| `P0-C01` | runtime dump 可复算；profile dump 与真实 0.147 临时快照相同，但 Makefile 固定比较 0.145 文件而失败 |
| `P0-C02` | `TestChangeset2ActivePreviousAndDigestSemanticsStayDistinct` 因版本不同而失败 |
| `P0-C03` | RuntimeCatalog 导出 6 个文件，测试固定期望 5 而失败 |
| `P0-C04` | transport 过渡收据以单一 CurrentTransportID 同时约束 Active/Previous，无法表达异版本 transport identity |
| `P0-C05` | Compiler 合法 query 用例固定 `client_version=0.145.0`，Active=0.147 时失败 |

release graph、binding 和 enums dump 在临时旁路画像上均可复算。真实换版仍须把 Compiler
正例集合改为新旧 ReleaseCatalog endpoint ID 并集；不能保留固定 endpoint 数量。

### 6.2 Candidate 与 classify

| ID | 实证结果 |
|---|---|
| `P0-D01` | core direct/MITM 与 frozen core/aux 四类任务没有注入 target version；两个 matrix 脚本空值回落 0.145、错误值原样接受；core/aux、WS gateway、relay 直接硬编码 0.145 |
| `P0-D02` | classify 草案把顶层改成 0.147，但 assertion profile 仍有 11 个 0.145 行为值；现有批准校验器仍返回 pass |
| `P0-D03` | 仓库只有从当前 embedded Catalog 反向导出的 `egressruntimedump`，没有 approved Profile → 内容寻址 RuntimeCatalog 的安全导入入口 |
| `P0-D04` | candidate evidence guard 和三份 Schema 只接受 0.145；错误、空值和正确 0.147 均不能形成目标版本收据 |
| `P0-D06` | test trace 的 mapping 摘要正确，但冻结 assertion profile 摘要仍为 `0732af76…`，实际文件为 `b52c11ea…`，生成器直接 fail-close |

`candidate_test_trace.py` 还把版本固定为 `0.145.0` 且无显式目标版本输入。上述工具都必须
在正式 `plan` 前完成修复，否则 Campaign 会冻结错误的工具身份。

## 7. 最小变更集完成记录

已严格按以下顺序一次完成一个变更集；每项均在提交前完成定向测试、完整门禁和自复核：

1. `e887085fb`：`P0-A-ENV01`，package load error 失败关闭；
2. `1ccb981d3`：`P0-B01/D06`，SPEC/画像摘要扇出对齐；
3. `4f187e5c0`：`P0-D01/D04`，candidate target-version 单一权威；
4. `21c2ab2b9`：`P0-D02`，classify 全行为坐标一致性门禁；
5. `15f23be87`：`P0-C01～C05`，异版本 Catalog/transport/Compiler 门禁；
6. `1bde77d62`：`P0-D03`，approved Profile → 内容寻址 RuntimeCatalog 离线生成链。

总复验结果：`make test-capture-tools` 340 项通过、3 项跳过；`make check-egress-spec`
全绿；完整异版本 mutation 的 Active/Previous 三坐标、runtime/profile dump 和 Compiler
并集门禁通过；最终 P0 `plan` 加载 42 条规则和 25 个任务，工具身份为 80 项、摘要
`fa8fc9fa…`。该 P0 Campaign 位于临时目录，只证明工具可用，不能转为正式 Campaign。

## 8. 复核摘要

- 未修改任何 0.145 历史画像、抓包或收据；
- 未把临时 0.147 合成资产写入仓库；
- 未触发真实网络请求或生产部署；
- `plan` 的 baseline 规则/场景默认值已证明语义正确；
- 当前结论：`p0_complete_formal_campaign_allowed_on_vircs`。
