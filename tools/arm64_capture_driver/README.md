# ARM64 抓包驱动链（逐轮参数化的 Codex 升级 VC-4／VC-5）

采集主机（ARM64）上驱动一轮真实 Campaign 的脚本集，2026-09-22 起入库并有闭合清单。它们**不是**受管工具
（不在 `tools/official_client_capture/` 内，不改工具身份），只是调用受管工具 CLI 的操作编排；但每个脚本都由
`manifest.json` 登记逐文件 sha256 与安装模式，安装时写组合安装收据绑定当前受管工具部署收据，
`guard.sh` 在每次派发前先复验（见 `install.py` 模块说明）。

## 安装目标与权限

* 安装目标：`/root/arm64-capture-driver/`（root:root，目录 0700；`.sh`／`.py` 0700，其余 0600）。
* 安装／复验：在采集主机以 root 执行
  `python3 <仓库副本>/tools/arm64_capture_driver/install.py install --source <仓库副本>/tools/arm64_capture_driver --target /root/arm64-capture-driver --data-root /root/docker/capture-cli/data`；
  安装收据落 `<data-root>/control/arm64-capture-driver-install-<stamp>.json`。受管工具每次重新部署后必须重新
  `install`（安装收据绑定的部署收据不再是最新时 `verify` 失败、guard 停止）。
* 按客户端（Claude 升级 B5a-06）：`install` 绑定全部已纳管客户端（`control/` 里已有其部署收据）各自最新的部署收据——只有
  一个客户端纳管时照旧写 v1，两客户端都已纳管时写 `arm64-capture-driver-install/v2`（`deployment_receipts`），交换共享根的
  一次部署之后重装一次即可；`install --client <客户端>` 只绑定该客户端。`verify --client`（缺省 codex）取该客户端当前
  最新的部署收据判定，guard 按本轮 `CLIENT` 传它。
* 开发侧改动脚本后必须 `python3 tools/arm64_capture_driver/install.py build-manifest --root tools/arm64_capture_driver`
  刷新清单；`tests/test_arm64_capture_driver.py` 以 `--check` 门禁清单与目录一致。
* 顶层精确闭合：根目录只允许 `install.py`、`README.md`、`manifest.json`、`driver/`，多任何一项（含 `__pycache__`）
  清单生成与复验都失败；安装态 `manifest.json` 自身 0600／root 也在复验范围内。
* 参数文件不 `source`：`lib.sh` 经 `driver/parse_env.py` 解析（精确键集合、值只允许引用已定义键、任何命令形态拒绝），
  只 `eval` 引号化后的赋值。
* `B` 自动派生为 `D/candidates/CAND`；旧参数保留时只作相等断言。公共前导与 Python 驱动共用根解析，
  旧环境中的 `NEWDIR`、候选实物根和收据根均被本轮派生值覆盖；路径跳转或符号链接在建运行目录前拒绝。
* `python3 -B driver/round_context.py --env "$ARM64_VC_ENV" --mode candidate` 只读重放正式 Campaign、
  有效账本、激活的 revision 与收据根，输出绑定摘要。`--mode campaign` 允许尚未激活候选；
  `--mode init` 只允许尚未创建的 Campaign，返回初始化坐标，不能代替正式准入。VC-5 准入已消费严格候选解析。
  `CAND` 是预期断言，实际候选来自账本；不按最新目录或最大 revision 选择。正式收据的重放、批准和派发合同保持独立。

* `phase_context.py --env "$ARM64_VC_ENV" --mode paths|build|attempt` 只读解析 VC-5／VC-6 的当前评估基线、
  阶段读来源与可写目标。`--mode attempt [--attempt-id ...]` 核对唯一有效 attempt 及完成收据；显式 ATT 只作断言。
  重开后的 checkpoint 使用原生 reopen 路径，reused 阶段不生成写计划；断言配置和门禁目录按候选／基线隔离。
  `build` 必须完整重放构建实物；已封存结果的只读查询可核对历史构建收据绑定，报告明确为 `sealed_receipt_binding`，
  不能用作新采集准入。`vc-batch.sh` 在预约前重放直接前序与计划参数，VC-6 另核对步骤收据与当前 acceptance。

* `cleanup_context.py --env "$ARM64_VC_ENV"` 只读输出当前收尾清理参数及 VC-5／VC-6、canonical 收据绑定。
  `tools/codex_closeout.py` 的 execute 清理配置可在参数、目标和凭证路径中引用 `${B}`、`${VC5_RECEIPT}`、
  `${VC6_RECEIPT}` 等报告字段；未知引用和旧阶段参数拒绝。恢复演练凭证须含 `status=passed`、展开后的
  `targets`、`backup` 文件绑定及 `cleanup_context_sha256`（报告规范化 JSON 的 SHA-256）。准备及发布时
  重放当前参数，变化后必须重新审核。执行脚本从 `CODEX_CLOSEOUT_CLEANUP_CONTEXT` 读取已批准报告，
  从 `CODEX_CLOSEOUT_CLEANUP_APPROVAL` 读取专项批准；结果须回填 `approval` 文件绑定和上下文摘要，
  删除核验也须绑定同一摘要。首次执行先持久化占位，中断后只对账、不重派；完成后只读复核。
  本入口不自动停止容器、删除资源或修改历史脚本，实际动作仍由审核绑定的脚本负责。

## 每轮流程（参数全部来自 `$ARM64_VC_ENV`，模板 `driver/env.example.sh`）

阶段合同与完成条件见[指南第四部分](../../docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md#codex-driver-entries)。
目标是“哪里坏修哪里，修好接着跑、整体约 6 小时”；耗时口径与未开放的承接边界以指南为准。
本机材料统一放 `/Users/czs/Developer/sub2apiplus-evidence/codex-cli/`，服务器活动数据保持冻结数据根。

1. 填写本轮参数，准备干净工具提交与 bundle，受监督部署工具／文档并安装、复验驱动。
   先 `entry-dryrun.sh start <bundle> <分支> <完整提交> --opening` 在独立演练根空跑；不写正式总账、不发真实请求。
2. **新目标**：`entry.sh` 完成预检、P0 和 VC-0 原子收口，最后派发 VC-1 首批；失败重跑同一入口，账本不重建。
   **同目标官方证据复用**：`pre-a3.sh → stage1.sh → vc0-gate-target.sh → stage2.sh`，只收 `EVIDENCE_DECISION=reuse`；
   已建账本的 stage1 收尾失败仅跑 `stage1-finish.sh`。`pre-all.sh` 可替代 stage2 并继续 VC-2／VC-3。
3. VC-1 封存和 checkpoint 重放通过后，先完成人工分类审核，再用 `vc23.sh`；批准前必须通过下节离线判据门。
4. 本机 `driver/local/local-candidate-chain.sh` 形成 A／C／D 提交链及 bundle，上传到参数中的 `$BUNDLE`。
   ARM64 先 `arm64-vc4-gates.sh`，确认 `ARM64_VC4_GATES_DONE` 和收据通过后再 `vc4-all.sh`，两者不并行。
   `driver/local/local-vc4.sh` 只解释旧轮次的本机门禁，新轮次不使用。Claude 的顺序见下文「Claude 的 VC-4 候选链」。
5. VC-4 收据通过后，`vc5-precheck.sh --dry-run` → 按批准 `--apply` → `vc5-all.sh`。
   现成 VC-4／VC-5 整链冻结生产替换用途；只读交付用途须生成对应的受管批次，不直接套用生产驱动计划。
6. VC-6 按指南经受管批次执行；收尾材料使用下节 `codex_closeout.py`，人工批准后才正式发布。

采集主机上的长任务使用 `ARM64_VC_ENV=<本轮参数> setsid -f bash <驱动脚本> > <本轮日志> 2>&1 < /dev/null`。
不要用 `nohup`；完成以正式收据为准。脚本路径相对安装根，`$RUNROOT`、bundle 和日志目录须预先从本轮参数确定。

## VC-2 批准前离线判据门

`vc23.sh` 调用 `vc2-approve-and-stage.sh <联合摘要>`，顺序固定为：批准预览批次 →
`vc2_assertion_preflight.py record --joint <摘要>` → `verify --joint <摘要> --report <报告>` →
批准批次 → 再次 verify → VC-3 stage-profile。全部由正式冻结清单派发；不是绕过监督器的低层命令串。

预检在封存官方证据上枚举全部官方适用判据，不发请求；候选内部判据仍在 VC-5 验证。
报告绑定联合摘要、正式预览及读侧输入，任一漂移必须重新生成。人工先审定五清单，驱动预检通过不能代替人工审核。
草案、预览或预检报告存在不代表 VC-2 完成，必须重放正式 classification 和 checkpoint。

## VC-5 统一准入（`driver/vc5-precheck.sh`）

每次调用先设置本轮 `ARM64_VC_ENV`，不要手填推导得到的 candidate／attempt／画像参数：

```bash
bash driver/vc5-precheck.sh --dry-run
bash driver/vc5-precheck.sh --apply --approval <本轮批准文件>
bash driver/vc5-precheck.sh --consume
```

* `--dry-run` 在公共前导前执行，零写入、零续签、零请求；输出 `blocked/needs_apply/ready`，前两者退出 3。
  `blocked` 先修阻塞；`needs_apply` 列出精确补齐动作。即使 ready，尚无合法准入收据仍须批准封存。
* 批准文件使用 `codex-vc5-admission-approval/v1`，绑定报告的 `admission_key`、完整 `actions`、
  `seal_admission=true`、`approval_id`、`approved_by`、`approved_at_utc` 与 `expires_at_utc`。
  使用当前执行账号所有、0600 的普通文件，可由 `VC5_ADMISSION_APPROVAL` 指定；工具不代签。
* apply 持派发锁重新检查，只写批准的画像／token／有效参数和准入收据；已完成则只读 consume。
  失败记录补偿结果；已有失败尝试不能盲目重复使用同一批准。consume 重新核对事实、凭据与 apply 事件。
* 画像内容 Digest 对照 VC-3，文件 SHA-256 对照 inventory，两者不能混用；宿主／容器副本必须一致。
  管理 JWT 按实际签发合同检查结构、签名、身份和时效，不额外要求 `iss/sub/aud/jti`。
  文件当前账号所有、无符号链接；0400 旧输入经批准规范化为 0600。最小剩余有效期为
  `max(21900, VC5_ADMIN_TOKEN_MIN_SECONDS)` 秒，参数默认 43200；日志不输出 token 或密钥。
* 完成产物是 `$RUNROOT/vc5-admission.json`、有效参数及 `vc5-admission-events/`；`vc5-all.sh` 与派发入口自动消费。
  准入不创建采集预约、不切网关、不发送模型请求；派发仍执行正式 Campaign／构建／环境门禁。

## 收尾编排（仓库 `tools/codex_closeout.py`）

该入口在可重放 Campaign 的环境中运行，使用已部署的读侧合同；不替代 VC-6 切换、回滚或目标恢复。
draft 可提前生成指南材料；prepare／gates／publish 在 VC-6 收口后执行。execute 清理的上下文解析要求
VC-6 已完成，因此先以延期清理决定封存 VC-6，再按新批准实际清理；不能让清理反过来阻塞其必需的完成收据。
在仓库根执行，所有参数路径使用本轮绝对路径：

```bash
python3 -B tools/codex_closeout.py draft --campaign <Campaign目录> --candidate <候选ID> --guide <指南路径> --baseline-profile <基线断言画像> --output <隔离草稿目录>
python3 -B tools/codex_closeout.py prepare --config <收尾配置> --dry-run
python3 -B tools/codex_closeout.py prepare --config <收尾配置>
python3 -B tools/codex_closeout.py gates --plan <工作根/plan.json>
python3 -B tools/codex_closeout.py publish --plan <工作根/plan.json> --guide-approval <指南批准> --publication-approval <发布批准>
```

`draft` 重放正式材料后输出 `material.json` 与 `guide.md`，人工核对受影响规则后用于 prepare。
prepare 要求权威仓库已提交且干净，工作根与仓库互不包含；dry-run 不写入，正式 prepare 只生成隔离候选，
不改权威分支、不推送。第二部分未变时不更新其派生摘要。配置 schema 为 `codex-upgrade-closeout/v1`：

| 字段 | 输入约束 |
|---|---|
| `repo/work_root/guide/material/terminal_facts/terminal_relative/tag` | 权威仓库、隔离根、已审核草稿及正式材料、终态事实、仓库内终态相对路径、本轮标识；不重签已存在终态 |
| `gate` | 精确提供 `driver`（entry-gates.sh）、`vc_env`、`node_modules`；gates 必须 full-gates／re-execute |
| `deployment_check` | 只读核验脚本、字符串数组 `arguments`、凭证 `inputs`、工作根内 `result` |
| `cleanup` | defer：`mode/reason/inputs`；execute：脚本、参数、输入、结果、精确 `targets`、`backup` 和 `restore_check` |
| `push` | 固定 `remote/ref/expected_tip`，ref 为完整分支引用、expected_tip 为完整提交；仅快进 |

execute 清理须用当前 `cleanup_context.py` 解析并绑定 VC-5／VC-6／canonical，发布另加
`--cleanup-approval <专项批准>`；无清理条件登记 defer。备份、恢复演练和删除核验绑定同一上下文摘要。
指南、发布和清理批准均绑定 plan 的 `review_sha256`、scope、批准人、UTC 有效期与 proof；文件为当前账号所有、0600。
指南批准 schema 为 `guide-review-approval/v1`，其余为 `codex-closeout-operation-approval/v1`。
只有正式门禁和批准都通过，publish 才快进权威分支、执行已批准收尾并推送；中断只对账既有动作，不自动重派删除／推送。
完整配置校验与可重入边界见[工具实现](../codex_closeout.py)，机器夹具见[现有测试](../official_client_capture/tests/test_codex_closeout.py)。

## 入口便宜检查（E1-02）

`pre-a3.sh` 与 `stage1.sh` 的第一步都是 `entry-preflight.sh`：在 pre-A3（ARM64 4 核约 8 分钟）与建计时账本之前，把几分钟内
就能查出的错误一次查全，未通过即停。也可以单独运行：`ARM64_VC_ENV=$RUNROOT/env.sh bash driver/entry-preflight.sh`。

* 七项互相独立，一项失败其余照查，最后汇总（`ENTRY_PREFLIGHT_PASSED`／`ENTRY_PREFLIGHT_FAILED <未通过项>`）；
  逐项日志在 `$RUNROOT/entry-preflight/<时间>/`。
* `required-parameters`：后续阶段要用、参数文件里可缺省的身份参数已填写；`guard-pre-plan`：同 `guard.sh pre-plan`；
  `deployment-identity`：最新部署收据的策略与五摘要等于当前受管树；`target-client`：宿主机与采集容器内的目标
  客户端摘要、版本等于登记值；`official-package`：官方包摘要与两份源码树；`plan-audit`：`codex_upgrade plan
  --audit-only`（参数与官方包、源码、规则与场景清单、作业 covers 与覆盖计划、基线证据、执行副本、工具身份；
  不读账本、不写文件）；`environment-probe`：ARM64 环境收据在临时目录试采并封存，结束即删除。
* 零请求：不建账本、不建 Campaign、不签收据。计时账本、checkpoint、环境收据与 VC 控制制品在建账本之后才有，
  由 stage1 建预检 Campaign 时的 plan 照旧校验。
* Claude（B5a-09，`CLIENT=claude`，取值见客户端表）：`required-parameters` 另要 `UPSTREAM_IP`（场景清单的必需 Campaign
  变量 `upstream_ip`）；`target-client` 另核断网合成容器 `capture-cli-synthetic` 内同一路径的摘要；`official-package`
  没有源码树，按 B2-01 冻结合同只读复核目标版本的冻结目录（`claude_upgrade_freeze verify`：收据合同、目标版本、两渠道
  等价、二进制摘要等于 `CLIENT_BIN_SHA256`、目录内每个登记文件的摘要）；`plan-audit` 调 `claude_upgrade plan --audit-only`
  （参数、官方包、规则／场景清单／证据标签与 Job 展开、工具身份并与 Store 现算身份按值比对），参数形状取客户端表的模板。

## pre-A3 路径认证：按场景并行（E2-03），从单元执行记录签发（E3-02）

* 场景作为入口门禁的单元执行（`entry-gates.sh` 的 `entry` 或 `pre-a3` 组合）：
  1. `codex_upgrade_pre_a3_certification plan --staging-parent $D/staging/pre-a3-scenarios`：44 个场景各一条
     `run-scenario --name <场景> --staging-parent <父目录>`，命令只带稳定内容，另给每个场景用到的测试模块文件；
  2. `entry_gates.py plan` 按场景声明输入：受管树（不含测试）、夹具、文档、部署脚本副本、场景测试模块的静态依赖闭包，
     加 `--pre-a3-data-root` 在数据根算好的冻结台账、录制数据与 alpine 镜像；场景单元可以承接；
  3. 驱动随附的统一调度执行器 `run-gates` 跑场景，执行记录入库（记录库 `$(dirname $D)/unit-records`）；已通过、且输入、
     规格、环境与调度策略都没变的场景承接，不再执行；
  4. `issue --unit-manifest <本次运行清单> --record-store <记录库>` 从记录组装 `pre-a3-path-certification/v2`：44 个场景在
     清单里各恰好一项，每项一条通过的正式执行记录（本次执行或承接；承接项另追到原运行的正式执行、没超期限），日志里
     恰好一行结果、网络计数为 0；清单须已发布进记录库（执行器自检通过才发布）。任何一条不满足都不签发。
* 每个场景是一个独立子进程，各自装网络拦截；本次的临时根在父目录下新建（`<场景>-<随机>`），通过后删除、失败保留供
  排查。结论是标准输出里的一行 `PRE_A3_SCENARIO_RESULT <JSON>`（带自摘要），执行器把日志按内容摘要存进记录库。执行器
  对失败单元的诊断重跑结果标 `kind=diagnostic`，不参与签发、不改结论。
* v2 保留 v1 的全部字段，另带每个场景的执行记录引用（`unit_record`）与运行清单引用（`unit_manifest`、`record_store`）。
  核验（`verify`、复用查找、发布认证）逐条重验记录，记录库须在原位；v1 历史认证照旧只读重放。`reuse-official-evidence`
  的 `--path-certification` 两种形状都认。
* 单独签（`pre-a3.sh` 新跑、`stage2.sh` 兜底，`lib.sh` 的 `issue_pre_a3_certification`）调 `entry-gates.sh --profile pre-a3`，
  测试树取自参数文件的 `ENTRY_BUNDLE`、`ENTRY_BRANCH`、`ENTRY_COMMIT`，缺一个即停（没有测试树算不出输入）；入口编排器
  入口门禁沿用、只重做 pre-A3 时同样走它。
* Claude 的工程历史来源必须在本轮安全参数文件显式填写三个只读目录：`CLAUDE_2_1_226_RELAY_FIXTURE`、
  `CLAUDE_2_1_226_FREEZE_FIXTURE`、`CLAUDE_2_1_226_STATIC_FIXTURE`。驱动仅将这三个固定键传给正式清单生产器，
  不继承外层 `CLAUDE_*`；缺目录、空目录或路径经过符号链接时在单元派发前失败。路径和内容都参与输入摘要，
  relay 还包含同级补充取证目录，因而内容摘要覆盖共同父根；任一来源变化会使声明它的 Claude 场景失效，Codex 输入不变。
  历史 2.1.226 工程录制只供零请求回归，不代表当前目标版本的 ARM64 原生双渠道资格或 OAuth 认证。
* Claude 每个正式场景另获独占的持久记录坐标 `$D/staging/pre-a3-scenarios/claude-chain-records/<单元摘要>/`。
  录制链与候选链原件、stdout/stderr 和副本保存在这里，成功场景的临时根清理不会删掉这些材料；可变产物内容不并入来源
  输入摘要。来源缺失与真实链失败保持失败，不转写通过；全部问题由同一轮执行器汇总后统一修复。
* 没通过的认证写旁路文件 `<正式文件名>.failed-<时间>.json`，正式路径保持不存在：修好后用同一 STAMP 直接重跑。
* E2-03 的旧路径（`plan --staging-root` → `run-commands` → `issue --executor-summary`，签 v1）与串行入口 `run` 保留作对照与
  回退，旧形式的场景单元在入口门禁里仍标不可承接。串行 `run` 不经执行器：真实链按闭包复制受管树要用 `unit_records.py`，
  数据根里没有 `tools/ci`，要给 `--records-module $DRV/unit_records.py`（驱动安装目录的同一份）；没给又找不到时在跑任何
  场景之前就拒绝（E4-03 实测：不给时 6 条真实链逐个失败、全部跑完才报）。
* 耗时（ARM64 4 核、机器上无其他任务，10-01 实测，全部执行时）：整份认证 478 秒，几乎全是最长的
  `vc-chain.vc1-recovery-chain`（477 秒）；其余 43 个场景在它运行期间由其余 3 核跑完。

## 入口门禁一次运行（E2-04，`driver/entry-gates.sh`）

* 一次运行跑完入口要的全部门禁：采集工具测试（测试组，按模块拆单元）、`check-egress-spec` 的全部子检查（Makefile 的
  `EGRESS_SPEC_CHECKS`，每项一个单元）、后端 go test 三组（不带标签、`-tags=unit`、`-tags=integration`，都带 `-count=1`，
  integration 带 `CI=true`，没有 Docker 时失败而不是静默跳过）、golangci-lint 三组、前端三项、CI 里的部署脚本测试（每条
  一个单元）与 pre-A3 的 44 个场景。全部交给驱动随附的统一调度执行器（`unit_executor.py run-gates`）在整机额度内并行，
  一项失败其余照跑，全部跑完再按门禁项汇总；`test-official-client-control` 单列一个门禁项，和 check-egress-spec 的同名
  子检查共用一个单元，只执行一次（Makefile 的 test 目标也不再单列它：子检查跑在执行器另起的 make 进程里，不和先决去重）。
* 组合 `--profile`：`entry`（默认，全部门禁项＋pre-A3）、`full-gates`（不含 pre-A3，`arm64-full-gates.sh` 与后台验证用）、
  `preflight`（只含 make test 的组成，`vc0-gate-target.sh` 用）、`pre-a3`（只含 pre-A3 场景，单独签 pre-A3 用，E3-02）、
  `regression`（定向回归，E4-01：只含采集工具测试组；全集通过下只改了测试或夹具时只执行静态依赖闭包里有改动文件的测试单元，
  改了受管工具或仓库其余部分时全部测试单元都要重跑——测试单元都声明了这两段范围）。`--with-gates
  <门禁项,…>` 给任一组合人工补门禁项（pre-A3 除外；定向回归选不出时用，比如改了后端、前端、文档或出站规格）。`entry` 与
  `pre-a3` 要求数据根已部署本提交（最新部署收据的整树摘要等于测试树的受管树，受管整树、两份指南与部署脚本副本逐项一致，
  否则退出 3），其余组合带 `--require-deployed` 时同样核对（后台验证用它把结论绑定到这次部署）；本轮 pre-A3 认证已有且
  有效、或有可复用认证时沿用，pre-A3 场景不纳入本次运行。
* 隔离：测试树单元在私有挂载命名空间里遮住 `/root/oauth-capture`（与 `isolated_run` 同一做法），树外只读字节码缓存；pre-A3
  场景在数据根的生产布局里运行（受管树经生产别名访问的分支也要覆盖到），用生产字节码共享层与身份记忆化。Linux 上不执行
  macOS 专用的 Apple container 部署脚本测试（BSD `stat`，写进门禁记录的 `not_executed`，CI 在 macos-15 上照常执行）。
* 测试树与字节码缓存默认放在数据根之外、跨轮次固定的 `$(dirname $D)/entry-gates-work`（Go 不加 `-trimpath` 时按包所在目录
  做构建缓存的键，路径每轮都变的话后端测试与 lint 每轮第一次都要冷编译整个 backend），通过即删；同一台机器同一时间只跑
  一次入口门禁（该目录下的 `.entry-gates.lock`）。
* 产物（主体目录 `--out`，默认 `$RUNROOT/entry-gates/entry-gates-<UTC 时间戳>`）：
  * `entry-gates.json`：总摘要（各门禁项结论、复合记录、P0 证据与 pre-A3 子汇总的位置）；
  * `logs/<门禁项>.gate.json`：与 `write_gate_json` 同一组字段，另带成员单元、失败单元与不在本平台执行的项；
    `logs/full-regression.gate.json` 是 make test 的组成全部通过与否；
  * `p0/check-egress-spec.json`、`p0/test-capture-tools.json`：P0 证据，check-egress-spec 另列逐个子检查，test-capture-tools
    另列逐条跳过与原因。两项门禁都没有承接单元时是 `codex-p0-offline-gate-evidence/v1`（与原手写 P0 脚本同一形状，收口前
    照原样组装 P0 收据的 facts）；任一项承接了单元时两份都是 v2（E3-03，见下文「单元执行记录与承接」的 P0 一条）；
  * `preflight.json`：VC-0 预跑记录；`full-gates-summary.json`：部署前全量门禁记录；`pre-a3-executor-summary.json`：
    pre-A3 场景的子汇总（E2-03 旧签发路径用；E3-02 起签发读 `executor/unit-manifest.json` 与记录库）；`executor/`、
    `executor.log`：执行器记录与逐单元日志。
* 退出码：0 全部门禁通过（`entry` 还要 pre-A3 认证签发或沿用成功）；1 有门禁未通过（测试树保留供排查）；2 用法错误；
  3 准备或执行失败（没有门禁结论）。`make check-egress-spec` 在测试树、本机与 CI 上也按同一份子检查清单并行执行。

## 入口步骤的输入摘要与失效判定（E2-05，`driver/entry_steps.py`）

* 入口的 16 个步骤（便宜检查、策略兼容与激活认证、入口门禁、pre-A3、零请求 smoke、atomic-double、建账本、环境收据、
  checkpoint、预检 plan、Job 演练、启动探测、发布认证、P0 收据、VC-0 收口）各登记一份输入清单，分七类：参数（本步骤
  实际读的键）、受管工具、测试与夹具、文档、命令与驱动、上游产物、环境。步骤表是 `entry_steps.py` 的 `STEPS`；入口门禁的
  门禁项另有 `GATE_INPUTS`，导出时写进每份门禁记录（`inputs`、`inputs_sha256`）。
* 步骤记录在 `$RUNROOT/entry-steps/`：`<步骤>.json` 是当前记录，`history/` 留每一次的不可变副本，记录带自摘要。执行前
  `begin`，执行后 `finish --status passed|failed [--product 名称=路径]`（两步合一用 `record`）；`evaluate` 逐步重算输入，
  给出沿用／重做／冻结／阻塞／实时和全部原因，`--json` 另存机读结果。参数取自 `source lib.sh` 后的环境变量，入口门禁的
  源码提交取参数 `ENTRY_COMMIT`。
* 判定规则：实时类（便宜检查、启动探测）每次执行；Formal Campaign 建成后创建链冻结；没有记录、记录被改、上次没通过、
  输入声明变了、产物缺失或被改、Job 演练收据不是通过、任一输入变了、上游要重做都判重做；账本已建就绝不重建，输入变了
  判阻塞。快照记录（`snapshot`，只供验收建立基准）默认不接受，要加 `--accept-snapshot`。
* 有意的取舍：兼容收据只看两份策略文件；激活认证随部署收据换新；pre-A3 不看部署收据的文件摘要（工具五摘要与策略不变时
  跨部署复用），但看受管整树（含测试）、部署脚本副本、两份指南、冻结台账目录与录制数据；账本只看决定「这本账属于哪次
  升级」的参数（截止时间的延期记在账本事件里）。
* 入口门禁 `entry` 组合在跑之前用 `entry_steps.py deploy-consistency` 核对数据根部署的就是测试树这一份：受管整树（含
  测试，不计字节码）、两份指南、部署脚本副本逐项比内容。整树身份不含测试目录，只比它会放过「测试改了、还没重新部署」；
  不一致退出 3。

## 入口编排器（E2-06，`driver/entry.sh`）

* 一条命令：`ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash driver/entry.sh [--plan] [--from <步骤>] [--to <步骤>] > $RUNROOT/entry.out 2>&1 < /dev/null`。
  * 建账本之前一次报全：便宜检查、策略兼容与激活认证、入口门禁与 pre-A3、零请求 smoke、atomic-double。依赖失败步骤的标为被阻塞，
    便宜检查的部署绑定一项没过时，激活认证、入口门禁与 pre-A3 都被阻塞。全部通过才建账本。
  * 建账本之后按顺序执行，失败即停：建账本、环境收据、checkpoint、预检 plan、Job 演练、启动探测、发布认证、P0 收据、VC-0 收口
    （后两步见下一节 E2-07）。
* 续跑就是重新执行同一条命令：
  * 每一步按 E2-05 的步骤记录判定沿用还是重做，没有记录的旧产物一律不沿用；
  * 只写一次的坐标被占用时换 `-r2`、`-r3`……，实际坐标写进步骤记录，下游从记录取；
  * 账本已建就沿用，绝不重建；
  * Job 演练上一次失败且留下了收据时，新的一次带上它，只重跑失败的作业。
* 参数：
  * `ENTRY_BUNDLE`、`ENTRY_BRANCH`、`ENTRY_COMMIT`：入口门禁的源码坐标，即部署到数据根的那一份工具提交。
  * `ENTRY_ROOT`：产物根，缺省是数据根。建账本之后的产物（计时账本、项目总账、环境收据、预检 Campaign、Job 演练、启动探测）都在它下面。
    验收演练设成数据根 `staging` 下的独立目录，配一本演练总账（项目总账模块的 fixture_only），这样生产项目总账与正式坐标都不写。
* 日志与记录：每次运行的日志与汇总在 `$RUNROOT/entry-runs/<UTC>/`（`run.json`），步骤记录在 `$RUNROOT/entry-steps/`，运行锁在 `$RUNROOT/.entry.lock`。
  `--plan` 只判定不执行；`--inject-mask <步骤>=<目录>` 只供验收（执行这一步时用只读空 tmpfs 遮住目录，制造一次真实的失败）。
* 相关脚本的变化：
  * `pre-a3.sh`、`stage1.sh`、`stage1-finish.sh`、`stage2.sh` 保留，供同目标官方证据复用及补跑；新目标入口使用 `entry.sh`。
  * 入口门禁 `entry-gates.sh` 新增 `--policy-activation`、`--pre-a3-certification`、`--pre-a3-mode auto|present|run`，由编排器替它定 pre-A3 沿用还是新跑。
  * 便宜检查结束时写 `summary.json`（逐项状态）。

## P0 收据与 VC-0 收口（E2-07，`entry.sh` 的最后两步）

* P0 收据（原来每轮手写）：两份离线门禁证据取自入口门禁那一次运行（`p0/test-capture-tools.json`、`p0/check-egress-spec.json`），
  加发布认证与回退依据 `P0_ROLLBACK_EVIDENCE`（参数文件新键，上一版本可回退点的收据，例如前序 Campaign 的画像目录晋升收据；
  入口便宜检查的必填参数一项就核对它是已有的普通文件，缺了在第一步报出）；
  subject 取计时账本计划；证据根 `$ENTRY_ROOT/control/<前缀>-p0-gate-<轮次>-<STAMP>`，签发后立即重放。
* VC-0 收口调用 `codex_upgrade_vc0_closeout`，参数全部来自参数文件与前序步骤记录：Formal `$ENTRY_ROOT/evidence/campaigns/$NEW`、
  监督器状态目录 `$ENTRY_ROOT/control/$NEW-supervisor`（控制根下一层，VC-1 对账才找得到首批父 run）、审计目录
  `$ENTRY_ROOT/audit/<前缀>-vc0-closeout-<轮次>-<STAMP>[-r<n>]`。Formal 未建时先复核启动探测报告；收口模块自己做发布认证、P0、
  Job 演练、部署收据与「当前工具身份＝预检冻结身份」的只读预检，任何一项不过都在写账本之前拒绝。
* 收口可重入：同一 Formal ID、同一账本，重新执行 `entry.sh` 即续作。收口模块每次先做只读现场判定：
  * Formal 未建：收据副本 → 建 Formal → 控制产物副本包在账本的收口 attempt 里；失败记 attempt 失败与根因，VC-0 保持打开；
    被硬杀留下的进行中 attempt 记为「收口进程中断」失败后开新 attempt；半成品 Formal 改名归档到账本的收口命名空间（不删）后重建。
  * Formal 已建、未派发：补控制产物副本、总账注册推送、「VC-0 完成」「VC-1 开始」，再派发首批。建成之后修工具不重签：受监督部署
    并在 Formal 上登记工具演进（`codex_upgrade tool-evolution`）后再执行 `entry.sh`。
  * 已派发：不再派发、不重跑收口；首批（或其恢复）已跑完就补写收口收据，否则按指南对账，给出恢复预览摘要。
  * Formal 建成之后，创建链各步冻结，`entry.sh` 只剩收口这一步；收口完成后再执行就全部沿用。
* 需要批准时收口这一步标「阻塞」（退出码 3），打印摘要与下一条命令；批准后带
  `--approve-sha256 <review_sha256> --approved-by <批准人>` 重新执行 `entry.sh`：
  * 同一根因连败两次（账本拒绝第三次）：修复并受监督部署后先不带批准执行一次得到预览（Formal 未建走收口模块的上限后恢复，
    已建走 `campaign-resume`；修复提交取 `ENTRY_COMMIT`、离线回归收据取入口门禁汇总，另须 `--reason <失败原因已如何消除>`）；
  * 首批已派发、对账可恢复：批准恢复预览后，只补跑没完成的作业（零请求预览批次 → 按预览补跑批次）。

## 单元执行记录与承接（E3-01，`driver/unit_records.py`）

* 统一调度执行器给每个单元的每次执行（正式、诊断）落一条不可变记录（`unit-execution-record/v1`）：单元 ID、执行类别、
  执行器版本、调度策略版本、环境指纹、单元规格、输入明细与摘要、测试 ID 与逐个结论、退出状态、用量、日志摘要、起止时间、
  自摘要。入口门禁把记录与日志存进记录库 `--record-store`（默认数据根之外跨轮次固定的 `$(dirname $D)/unit-records`）。
* 两种模式（`entry-gates.sh --mode`，方案 D12）：
  * `full-set-pass`（底层逐单元模式，入口由 B-09 全集准入保护）：先在记录库里给每个单元找可承接的记录，只执行找不到的。可承接＝正式执行、按原始字段
    重新判定为通过、单元规格与输入摘要没变、调度策略版本与环境指纹没变、执行器没变、7 天以内、所在运行的清单把它列为该单元
    的正式执行、日志在库且摘要相符。诊断执行的记录永不承接。
  * `re-execute`（重新执行全集）：一个都不承接。升级开工的入口空跑、一致性验收、收尾合入前用；编排器带 `--reexecute-gates`。
* 输入：采集工具测试单元＝受管工具树（不含测试目录）、本模块静态依赖闭包里的测试文件、夹具目录、docs、仓库其余部分，整目录
  读取测试的另加整个测试目录、闭包里有真实链的另加全部辅助模块与真实链目录、读真实仓库 git 的另加 HEAD；命令单元（子检查、
  后端、lint、前端、部署脚本）按 E2-05 从宽：整个仓库＋HEAD，子检查另加历史源码树。pre-A3 场景按场景声明输入、可以承接
  （E3-02，见上文「pre-A3 路径认证」）；只决定缓存位置的环境变量（字节码前缀、身份记忆目录）不算进单元规格。
  承接模式要求测试树干净，否则本次一律不承接。
* 环境指纹：门禁清单生成与执行器都在白名单环境里运行（`env -i`：PATH 与语言用固定值，另放行 HOME、时区、TMPDIR、代理与
  证书、Go／Docker／Node／pnpm 的变量；USER、LOGNAME、SHELL 不放行——E4-01 起与调用方无关，操作员 shell、修好接着跑、后台验证、
  入口空跑跑出的记录可以互相承接），执行器把这份环境整份算进指纹（字节码前缀除外），再加系统、内核、Python 与已装包、dpkg
  软件包、有效用户、主机名、核数，以及门禁清单给的工具链版本与前端依赖摘要。任何一项变了全部单元不承接。单元规格与环境里有
  测试树路径，测试树目录也要相同（B-09 不强行让隔离后台缓存与前台共用；坐标不同且输入不能重放时全量重跑）。
* 清单：每次运行写 `executor/unit-manifest.json`（逐单元本次执行还是承接、承接的原运行与记录摘要、执行的不承接原因）并
  自检——执行＋承接＝全集、重新执行全集不得有承接项、每条承接都追到原始的正式执行记录并逐项重验、测试组记录的测试 ID 并集
  等于全集。自检不通过，门禁结论判失败；自检通过的清单才存进记录库（`runs/`），之后的运行才能承接这次的记录。单独重验：
  `python3 driver/unit_records.py verify --manifest <清单>`。
* P0（E3-03）：门禁项里有承接的单元时，入口门禁把两份 P0 证据写成 `codex-p0-offline-gate-evidence/v2`——v1 字段之外
  登记本次运行清单（run_id、自摘要、模式）、记录库、门禁项的命令单元与测试组、本次执行／承接的单元数，以及测试树的
  工具五摘要（导出时在测试树里起子进程算，与发布认证同一个函数）。P0 收据的形状不变（两条字面 make 命令、四个证据
  角色）；签发（`codex_upgrade_vc_receipt finalize`）与 VC-0 收口按记录库逐条重验：清单已发布、两份证据引用同一份清单；
  check-egress-spec 的单元等于清单规划的全部子检查，test-capture-tools 的测试 ID 并集等于清单冻结的全集、没有缺报和
  重复；每条记录在库、自摘要相符、是正式执行、规格与输入摘要等于清单、调度策略／环境指纹／执行器与清单相同、判通过、
  日志在库，承接的追到原运行清单且在承接期限内；计数按记录重算等于证据与收据断言；工具五摘要等于发布认证登记的身份。
  重放不读记录库。记录库里没有已发布的清单、五摘要算不出来时两份都不写，总摘要 `p0_evidence_withheld` 写明原因，
  编排器的 P0 收据步骤照此报失败（出路：修好后重跑，或 `--reexecute-gates` 重新执行全集得到 v1）。v2 收据的收口要求
  记录库还在原位。
* 清理（E4-02，`driver/entry_housekeeping.py`，入口空跑开跑前自动做，也可 `entry-dryrun.sh housekeeping` 单独做）：保留期
  （默认 30 天，不得短于承接期限另加 1 天）内写入的运行清单、记录与日志都留着；数据根 `control`、`evidence`、`staging` 里
  登记了本记录库的认证与 P0 v2 证据（Codex 的 pre-A3 v2、Claude 的 pre-A3 v3 与两客户端的 P0 v2 同一形状；含复用复制的认证、
  P0 收据证据根里的副本、演练根里的）引用的运行，连同清单列出的全部记录、记录的日志与承接项的原运行清单都留着；逐单元承接
  目录（`unit-inherit/`，B5a-10 纳入）跟着运行走：保留下来的运行的决策收据、撤销登记与消费登记，以及它们逐层引用的证据快照
  都留着；其余早于保留期的删掉，目录不删。同样的日志与证据快照内容寻址到同一个文件，发布方遇到已有的同名文件会刷新修改
  时间，清理先移走再复查修改时间，期间被刷新的放回原处。

## 读集审计（B-11，`driver/read_audit.py`）

* 旧 `--audit-reads` 仍核对测试树和数据根，明确标为 `coverage_complete=false`，不能作为完整宿主覆盖凭证。
* `entry-gates.sh --mode re-execute --audit-reads --audit-strict` 记录全根路径，包括宿主依赖、目录元数据、缺失探测和字节码；整仓声明的单元也接受审计。无法解析、轨迹缺失、未绑定路径或输入漂移均失败，不发布可承接的通过记录。仅审计失败不自动重复执行命令。
* `--audit-host-inputs <JSON>` 以 `unit-host-inputs/v1` 的 `units` 对象登记逐单元精确绝对路径，运行前计算快照。文件绑定内容、元数据、链接及祖先权限；目录只绑定直接目录项，不能代表子文件。虚拟设备、写入和不能稳定绑定的读取不能承接；未闭合单元维持重新执行。
* 宿主快照 `read-audit-host-snapshot/v2` 保留链接指向、身份、权限、修改时间等属性，排除正常路径解析可能刷新的链接访问时间；普通目标文件和目录的访问时间仍绑定。成功的 `lstat`、带 `AT_SYMLINK_NOFOLLOW` 的元数据读取及 `O_PATH|O_NOFOLLOW` 打开继续拒绝承接，避免遗漏可观察的链接元数据。旧快照须重新取证，不能直接当作 v2 凭证。
* `read_audit.py scope --record <记录库正式记录> --trace <库内轨迹> --output <新提案>` 重放来源运行清单、日志和完整读集。`entry_gates.py plan --input-scope <提案>` 或入口同名选项只替换该单元的输入声明，保留门禁全集；测试提案进入 `test_input_scopes`，按本轮实际发现结果复核完整测试规格，命令提案仍按原命令复核。资源、环境及执行器摘要均须匹配，并重新执行严格审计。提案 `reuse_enabled=false`，不直接承接宽范围来源记录。
* 严格审计保留原测试树和字节码缓存。带 `--input-scope` 再验收时，只读核对原树提交、干净状态、bundle 分支及缓存，禁止重建测试树、复制依赖或重新预编译来绕过快照漂移；原实物缺失则重新建立读集。
* 原始轨迹按内容摘要入库，审计结论先于执行记录落盘；承接时重新读取轨迹并核对当前快照。正式结果与诊断结果分别登记；覆盖报告逐单元列出缺口，汇总缺少任一单元不能判完整通过。B 类真实承接仍须独立批准。
* `--audit-policy <JSON>` 登记 `unit-audit-policy/v1`，固定 `default=reexecute-only`；`units` 按本轮实际命令或测试单元 ID 列出精确 `host_paths`、`require_read_audit=true` 与 `runtime_contract`，未知 ID 拒绝。登记单元必须覆盖闭合；未登记单元执行自身，并明确记录 `inheritable=false`、`reexecute_required`、`coverage_complete=false`。未覆盖项不能成为以后承接的来源；功能与承接边界通过不代表全集读集覆盖。缺单元、错绑或登记项审计失败仍判失败。此策略不授予承接权限，真实承接仍须原 B5a-08 请求与批准。
* `entry.sh --audit-reads --audit-strict --audit-policy <JSON>` 将严格审计与策略文件原样传给入口门禁及 pre-A3，并绑定两步输入；环境中的同名键不能隐式启用。策略登记必须匹配实际步骤的单元集合，不能把含测试 ID 的完整门禁策略直接用于只有场景命令的 pre-A3。
* 控制终端单独绑定 `/dev/tty` 标准字符设备身份和无控制终端状态；只接受轨迹明确为 `ENXIO` 的打开失败或合同内元数据检查。成功访问、其它错误、事件缺失均拒绝，不能把所有设备读取一概豁免。
* `/bin/bash -n` 语法检查显式设置 `SHELL=/bin/bash` 并绑定到单元规格，避免精简环境下额外查询用户数据库。其它脚本的环境保持原合同；取证和完整门禁必须使用相同的入口环境，不能用 SSH 会话环境替代。
* 单元输入可登记 `runtime_contract`（`unit-runtime-contract/v1`）：指定专用空临时挂载点、既有生产别名遮挡和明确的动态环境输入。`read_audit_runtime.py` 创建私有挂载命名空间，分别留存实际挂载轨迹与被测轨迹，在实际环境中复核外部输入前后快照。测试单元另绑定完整 `runtime-test-spec`，沿原 `run-unit` 运输测试载荷，在私有 tmpfs 生成固定结果和首次已观察失败旁路；结果回收只接受本轮创建且身份、测试集合与摘要匹配的两类产物，消费端重放同一证明。输出坐标只绑定本轮请求，不进入稳定输入；每轮重建的宿主测试清单不能充当稳定输入。旧文件、先读后写、链接逃逸、来源不明、环境漂移或结束凭证缺失均不可承接，不放开通用移动操作或宿主写入。
* 动态路径只支持逐项登记 `/proc/filesystems` 和 `/proc/sys/crypto/fips_enabled` 的实际值；`/dev/null` 单独绑定标准设备身份。其它动态内核输入、命名空间变更和未建模通信继续失败关闭，不允许整个 `/tmp`、`/proc` 或 `/dev` 前缀豁免。子进程早于 clone 返回的相对路径按父子关系缓存重放，缺少父子关系仍拒绝；轨迹文件名包含完整单元 ID 摘要，避免不同单元串用。

## 前阶段 1 的收尾段与客户端启动探测（R19）

* `stage1.sh` 建好账本与预检 Campaign 后写 `$RUNROOT/stage1.partial.env`，再调用 `stage1-finish.sh`：
  Job 演练收据 → 客户端启动探测 → atomic-double 收据 → 写 `stage1.env`（新增 `PROBE` 坐标）。两个脚本开头都把
  旧的 `stage1.env` 改名留档，任何一步失败都不会留下可被误用的旧坐标。
* 收尾段任一步失败：修复环境后单独执行 `ARM64_VC_ENV=$RUNROOT/env.sh bash driver/stage1-finish.sh` 续跑，不要重跑
  `stage1.sh`（会新建账本）。已有收据的 Job 演练与 atomic-double 直接沿用；半途中断的目录原样保留，换带时间后缀的
  新目录重做；启动探测每次都重跑，反映修复后的环境。
* 启动探测（`client_launch_probe.py`）从预检 Campaign 展开全部作业（与 Job 演练同一入口），挑出经
  `run_official_relay_scenario.sh` 运行 TUI 场景的步骤，按调用点的展开规则算出受管 `drive_codex_tui.py` 的参数，
  逐组合在采集容器的私有命名空间（`unshare --net --mount --pid`）里启动目标客户端：只有回环网络，本地替身终结
  全部请求（零真实请求）；CODEX_HOME、/tmp、/var/tmp、/work 与证书目录全部叠加 tmpfs 覆盖层（零副作用）。
  提示词换成只含大写字母的口令，替身收到正文含口令的首个 turn 请求才算通过；信任目录、模型迁移、登录、hooks
  审查等交互屏拦住首帧即失败。失败组合在新命名空间里以 48×160 窗口复跑一次，只用于识别是哪类交互屏。
  探测前后在命名空间外比对容器状态指纹并检查残留进程。报告在 `$PROBE/report.json`（每次运行另存
  `attempts/<时间>/`），`verify` 拒绝未通过、自摘要不符、Campaign 不符或带验收参数的报告。
* 替身只做 TUI 引导必需的最小应答：`accounts/check` 回显请求头里的账号（与真实上游同构）、`/models` 用
  CODEX_HOME 中客户端自己缓存的最近一次真实目录（新版本首次探测时目录来自上一版本的缓存，迁移屏判断以此为准，
  报告 `models_catalog` 记录来源）、WebSocket 升级回 426（客户端立即回退 HTTP）、`POST …/responses` 回 400、
  其余 404。报告只记录请求的主机、方法、路径与头部名称，不记录任何头部取值。
* 验收参数：`--test-mutation untrust:<目录>`／`unack_migration:<模型>` 只改覆盖层里的 config.toml 副本，
  `--only-scenario` 只探测指定场景；带这两类参数的报告一律不能作为放行依据。
* Claude（`CLIENT=claude`，B2-10）：`stage1-finish.sh` 与入口编排给 Job 演练收据模块和启动探测都追加 `--client claude`
  （取客户端表；Codex 不追加，命令行不变）。Job 演练由共享收据模块经客户端适配器取 Claude 画像，签
  `official-client-job-rehearsal-receipt/v1`：核对抓包容器与断网容器 `capture-cli-synthetic` 两份执行副本、命令依赖与
  证据 runs 根，目标客户端三处摘要，逐步核对真实上游步骤只进抓包容器、合成步骤只进断网容器，受管中继与目录文件在
  执行容器里可用；不读凭据，ARM64 还没做 Claude OAuth 登录时也能通过。启动探测的组合来自中继步骤
  `--fw-f-v5-probe` 指向的 TUI probe（按启动身份去重），运行器 `claude_launch_probe_runner.py` 经受管驱动入口
  `claude_drive_tui_cli.py` 启动 TUI（入口与中继 TUI 场景用同一份 Claude 侧驱动 `capturelib/claude_fw_f_v3.py`：
  2.1.285 以 auto 为缺省启动权限模式，页脚不再显示「? for shortcuts」，空闲时是「⏵⏵ auto mode on (shift+tab to cycle) ·
  ← for agents」，驱动把它也认作可以输入；报告的 `host_drive_sha256` 与逐组合 `drive_sha256` 覆盖入口、共用状态机与这份
  驱动三个文件）：工作目录与夹具用中继同一函数建，私有 HOME 在覆盖层 `/work/probe-home`
  （合成凭据、隐私设置、全局状态 `/root/.claude.json` 的规范化副本；文件不存在时从空对象开始；与中继 TUI 临时 HOME
  同法预置工作目录信任 `projects[<工作目录>].hasTrustDialogAccepted`），`/dev/shm` 换成私有 tmpfs，本地替身终结
  `api.anthropic.com` 与 `platform.claude.com`，替身收到正文含口令的 `POST /v1/messages` 才算通过（其余回 404，2.1.285
  启动期的 hello／profile／settings／policy_limits 都把 404 当作「没有」放行）。会话文本里识别信任目录、登录、onboarding、
  主题、模型迁移等交互屏，拦住首帧的屏判失败并以 48×160 窗口复跑诊断。信任目录屏只在 `untrust:` 验收变更下是预期的
  （驱动入口按选项文字把焦点移到「Yes, I trust this folder」再确认，2.1.285 起默认焦点在「No, exit」）；正常路径出现它判
  `trust_dialog_unexpected`（预置信任失效，启动流程与设计不符，先查预置再放行）。前后指纹另覆盖 `/root/.claude`（凭据文件只记
  元数据，不读内容）与 `/root/.claude.json`。验收参数 `--test-mutation untrust:<目录>`（目录须是探测工作目录或其上级）
  撤销工作目录信任链（工作目录本身与它的全部上级）上的信任确认，`--only-scenario` 取 TUI probe 编号；报告合同 `official-client-launch-probe/v1`，
  `verify` 要带 `--client claude`。atomic-double 按客户端表取收据模块、`--client` 与证据根前缀（B5a-09 续：Claude 的证据根是
  `$D/staging/claude-atomic-vc0-vc1-<轮次>-<时间戳>`，收据 `official-client-atomic-vc0-vc1-rehearsal/v1`）。

## VC-0 预跑目标平台门禁（`driver/vc0-gate-target.sh`）

* 用途：目标平台门禁（采集主机上对候选测试树隔离执行 `make test`，40～70 分钟）原本只在 VC-5 accept 之前跑，门禁自身的
  问题（字节码缓存前缀导致计时用例失败）要到那时才暴露。VC-0 先用当时的候选源码（本轮受管工具部署所在的提交）把同一门禁跑一遍，
  问题在 VC-0 就修掉。结果只作预检，**不是** accept 的门禁收据；VC-5 accept 前 `vc5-accept.sh` 仍在候选门禁目录执行正式门禁。
* 时机：E2-04 起预跑记录取自建账本之前的入口门禁（同一次运行的 `preflight.json`），本命令只在入口门禁之后又改了源码、需要
  单独补跑时使用；单独运行时不与 stage1 各步或 VC-1 取证并行（与 accept 前正式门禁同一安静条件，资源争用会把计时用例拖红）。
* 源码：本机 `git bundle create <文件> <BASE>..<分支>`（与候选提交链末尾同一打法；BASE 必须在 `$HISTORY_TEST_TREE` 的历史里，
  例如前序候选的 DC；部署受管工具时上传的 bundle 满足这一条件也可直接用），传到采集主机 `$RUNROOT/vc0-preflight/` 下。
* 命令（输出重定向到 `$RUNROOT/vc0-gate-target.out`；make test 里有挂断检测用例，必须 `setsid -f`，不能 `nohup`）：
  `ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash driver/vc0-gate-target.sh <bundle 绝对路径> <分支> <40 位提交> [<前端依赖目录>] > $RUNROOT/vc0-gate-target.out 2>&1 < /dev/null`。
* 做法：测试树用 `lib.sh` 的 `clone_test_tree`（与 VC-5 的 `gates.sh prepare` 同一函数：从完整历史测试树克隆、从 bundle 取分支、
  检出、断言提交数 >10000 且不含 vendor），放在 `$RUNROOT/vc0-preflight/test-tree`；前端 `node_modules` 默认取
  `$HISTORY_TEST_TREE/frontend`，其 `pnpm-lock.yaml` 必须与本树逐字相同（本轮 lockfile 有变化时按 `frontend.sh` 同一方式在独立
  目录装好依赖，作为第 4 个参数传入）。门禁是入口门禁的一次运行（`entry-gates.sh --profile preflight`：make test 的组成全部
  并行，前后采集 gate_before／gate_after 环境收据）：主体标识 `vc0-preflight-<UTC 时间戳>`，门禁根
  `$RUNROOT/vc0-preflight/<主体标识>/`（环境收据、`logs/target-platform.gate.json` 与各门禁项记录、预检摘要 `preflight.json`、
  执行器日志），字节码缓存放在 `$RUNROOT/vc0-preflight/pycache-target-platform`，与 VC-5 不共用任何目录。
* 边界：不写候选门禁目录与候选目录，不写时间账本与 Campaign，零模型请求；同一轮只允许一个预跑（`$RUNROOT/vc0-preflight/.lock`）。
* 退出码：0 通过（删掉测试树与缓存，末行 `VC0_GATE_TARGET_DONE`）；1 门禁未通过（打印记录位置、保留测试树，末行
  `VC0_GATE_TARGET_FAILED`）；2 用法错误；3 准备或执行失败（`VC0_GATE_TARGET_ABORTED`，没有门禁结论）。未通过按普通 VC-0
  失败处理：修门禁、驱动、环境或源码后重跑同一命令（新主体标识，旧结果留档）。

## ARM64 全量门禁与 ARM64 版 VC-4 门禁（测试一律在采集主机执行）

* 两个入口都用 `lib.sh` 的 `clone_test_tree` 建测试树（完整历史、不含 vendor，前端依赖取 lockfile 相同的一份），隔离方式与
  `vc5-gate-target.sh` 的目标平台门禁相同（私有挂载命名空间里只读 tmpfs 遮住 `/root/oauth-capture` 别名，树外只读字节码
  缓存，不用生产的身份记忆化目录）。make test 里有挂断检测用例，一律 `setsid -f` 启动；不与目标平台门禁、VC-1／VC-5 采集并行（采集主机只有 4 核，
  资源争用会把计时用例拖红）。
* `driver/arm64-full-gates.sh <bundle> <分支> <40 位提交> [<前端依赖目录>]`：受管工具每轮修复的部署前提、版本登记变更集与
  升级收尾的验证。E2-04 起是入口门禁的一次运行（`entry-gates.sh --profile full-gates`），与 CI 逐项对齐、全部单元并行，
  全部跑完再下结论：make test 的组成、`go test -tags=unit／integration ./... -count=1`（采集主机有 Docker，集成测试真实执行）、
  `golangci-lint run --timeout=30m --build-tags=unit`／`integration`、CI 里的部署脚本测试（从测试树的 `backend-ci.yml` 逐行
  取出，每条单独执行，macOS 专用的那条在 Linux 上记为不执行）。结论只写 `$RUNROOT/full-gates/<主体标识>/`（summary.json、
  entry-gates.json、各门禁项 gate.json 与执行器记录），不是 Campaign 收据。退出码：0 全部通过；1 有门禁未通过（其余照跑，
  测试树保留）；2 用法错误；3 准备失败（没有门禁结论）。
* `arm64-vc4-gates.sh` 仍按 VC-4 本地门禁合同逐项经 `isolated_run` 执行 `make check-egress-spec`、`make test` 与
  `make check-egress-spec-ci`（其中 check-egress-spec 的子检查在 make 目标内部并行）。
* `driver/arm64-vc4-gates.sh [<前端依赖目录>]`：替代本机 `local-vc4.sh`，C、DC、RECEIPT、BUNDLE、BUNDLE_BRANCH 取自本轮参数
  文件。门禁分工沿用本机合同：DC 上 `make check-egress-spec` 与 `make test` 必须通过；C 上只跑 `make check-egress-spec-ci`
  交叉核对，只允许缺冻结承接收据导致的预期失败（由操作员按日志确认）。DC 必须恰好是 C 加承接收据一个文件，否则不交付。
  产物直接写入 `$RUNROOT/local-gates`（六件套）与 `$RUNROOT/impl-logs`（旧目录先整体归档），文件名、gate.json 字段与日志
  格式都与本机上传逐字段相同，随后生成上传清单并写 READY——`vc4-all.sh`、`vc5-all.sh`、`build_gate_facts.py` 都不用改（门禁
  收据只要求 target-platform 一项的架构等于候选架构）。门禁结论（rc 非零）照常交付并写 READY、退出 1；准备失败不交付、
  不写 READY、退出 3。

## 每轮身份与批准集合

* 在原有参数上必填：`BASELINE_VERSION`、`TARGET_VERSION`、`TARGET_PROFILE_ID`、`CODEX_BIN`、
  `CODEX_BIN_SHA256`、`OFFICIAL_ASSET_SHA256`、`MAIN_MODEL`、`LITE_MODEL`、`CODEX_ACCOUNT_ID`、`API_KEY_ID`、
  `PREDECESSOR_CAMPAIGN`、`POLICY_COMPAT_RECEIPT`、`POLICY_ACTIVATION`、`RELEASE_CERTIFICATION`。
  任一缺失都拒绝；模板中的 `REPLACE_*` 是待填写占位符，不能直接执行。`PROFILE_ID` 必须与目标画像一致。
* 按客户端（Claude 升级 B5a-06）：参数 `CLIENT`（缺省 codex，可省略）选择 `driver/client_table.py` 的一行——编排器与各收据
  模块名、二进制与账号参数键、总账目录、部署收据规则、指南、追加给共享模块的 `--client` 等。目标客户端的二进制、摘要与账号
  通用化为 `CLIENT_BIN`／`CLIENT_BIN_SHA256`／`CLIENT_ACCOUNT_ID`：Claude 只认通用键、不要求 Kilo 三项与 `CODEX_*`（写了即
  拒绝）；Codex 的旧键照旧有效，通用键是它们的别名、解析后按旧键导出。`lib.sh` 解析参数后以不导出的 `CT_` 变量给各脚本
  用客户端表；Claude 尚未接入的入口步骤、便宜检查项与驱动工具登记在表的 `pending`，执行到即失败关闭并写明工作项。
* `parse_env.py` 集中派生规则／场景／补丁路径、Campaign 前缀、lifecycle 目录和候选镜像仓库（随客户端的部分取客户端表的
  模板）；Python 与 shell 使用同一解析结果。来源目录、官方包、基线画像可按模板可选键覆盖；不可从路径推导制品摘要或账号。
  `stage1.sh` 还要求 `TARGET_CODE_MODE_HOST_SHA256` 与 `CAPTURE_RUNTIME_IMAGE`，由正式 plan 校验制品和镜像。
* `stage2.sh` 仅接受 `EVIDENCE_DECISION=reuse`，且前序 Campaign 必须与本轮目标版本相同；首次升级用 `recapture`。
  认证输入按显式路径使用；需要新签兼容收据时必须提供 `PREVIOUS_POLICY`。最新部署按跨版本收据时间戳判定，
  相同时间多份收据拒绝。任何门禁仍由受管工具验证，文件存在不代表认证生效。
* `local-candidate-chain.sh` 要求本轮 `ARM64_VC_ENV` 与 `GATE_MAPPING_INPUT`。Catalog 先逐项验收 inventory，
  已有 blob 只能逐字复用，所有新 blob 和测试快照索引进 A 段，两份冻结指针进 C 段。映射必须已绑定本轮 VC-3
  完整需求；不从旧版本映射自动猜测。bundle 分支由 `BUNDLE_BRANCH` 提供，并核对当前分支。
* 实现门禁与 facts 从批准 gate-plan／mapping 和 VC-3 需求读取；VC-5 与 canonical 的候选 Job 集合由正式
  Campaign 场景解析器读取，不固定门禁数、规则名或 Job 列表。缺少批准输入、命令／集合／摘要漂移均拒绝。
  canonical 必须提供 `RETIRE_VERSION`；本机历史门禁须显式设置本机路径 `HISTORICAL_SOURCE_ROOT`，ARM64
  可在本轮参数文件中覆盖其远端路径。保留历史测试接口变量名，不固定其来源目录。

## Claude 的 VC-1 封存批次（Claude 升级 B2-11 第二部分）

`CLIENT=claude` 的轮次：`entry.sh` 的 VC-0 收口只派发 VC-1 首批（恰一个 `capture-official run`，共享监督器的 VC-1 普通恢复链
要求首批恰有一个动作）。首批完成、官方 attempt 处于 awaiting_receipts 之后，在采集主机运行 `vc1-claude.sh`（客户端表
`vc1_dispatch_script`，Codex 没有这一项）：

1. 不带参数：预览批次——静态分析 `vc1-01-analyze-target`（seal 要的目标 SinkInventory 与 sink containment）在前，seal 预览
   `vc1-02-seal-preview` 在后；预览停在等批准（批次内的合法暂停点）。只读复用前序的 Campaign 在这一批里直接封存完成。
2. 审核 `claude_vc1.py summary` 打印的 seal 预览（闭合状态与 review_sha256）后，`vc1-claude.sh <review_sha256>` 派发批准批次
   `vc1-03-seal-approve`，写 VC-1 checkpoint。发现项要人工处置时用环境变量 `DISCOVERY_DISPOSITIONS` 给出 Campaign 内的处置
   文件，预览与批准给同一份。

动作取受管 `claude_upgrade_static.vc1_seal_actions`（受管 Python 直接调 `claude_upgrade.py`），`claude_vc1.py plan` 按共享合同
校验后写进本轮输入目录 W，经 `vc-batch.sh <NEW> <IN> VC-1 <序号> VC-0` 派发；动作名不用 Codex 的 `seal-official-*`（那两个
名字会触发 Codex 的断言证据包门禁）。

## Claude 的 VC-4 候选链（Claude 升级 B3-04a）

`CLIENT=claude` 的轮次按客户端表 `vc4_requirements_source=arm64-preview`、`vc4_dispatch_script=vc4-claude.sh` 走下面的顺序（Codex
仍是 `local-candidate-chain.sh` → `vc4-all.sh` → `vc4.sh`，流程与命令行不变）：

1. 采集主机 `vc4-requirements.sh`：`guard.sh pre-plan` → `vc4_resume.py open-revision`（经 `claude_upgrade revision-open` 写编排器与
   Store 两份登记，同一参数重跑幂等）→ `vc4_client.py requirements`：编排器的门禁需求预览（`plan-candidate-gates` 只给 Campaign
   与候选，零写入）与按 VC-2／VC-3 阶段结果只读推出的需求逐字核对后写 `$RUNROOT/vc4-gate-requirements.json`（0600，同一轮重跑
   收敛）。本步在候选提交之前，参数文件里的 C／DC／RECEIPT／BUNDLE 可先沿用上一轮，`vc4-all.sh` 之前改成本轮的提交。
2. 本机 `local/local-claude-candidate-chain.sh`（参数与 Codex 链同形）：一条只读 ssh tar 取回 Campaign 总计划、`profile/` 下的
   VC-3 产物与需求预览，`claude_catalog_chain.py` 按摘要链核验后装配三段提交：
   * A：暂存 Release 的画像与 Wire（`catalogdata/claude/…`，内容寻址、已有只能逐字复用）、投影策略
     `tools/official_client_capture/claude_upgrade_wire_projection_<版本下划线>.json`、lifecycle 目录的 `catalog-stage/`（暂存收据与
     Release 条目，先删再写、恰好两个）、`gate-mapping.json`（`GATE_MAPPING_INPUT` 原字节）、`gate-plan.json`；
   * C：只有 Claude 的 `release-catalog.json`（VC-3 冻结的 Active 目录、末尾追加暂存 Release、validation_candidate 指向它）。写完先
     用候选源码的 Go 加载器（`claude_catalog_check.go`，在 backend 内的临时目录 `go run`）核对，再以 `verify_candidate_catalog`
     验收，任一项不过即还原、脚本停下；
   * D：`freeze-successor-generate --before A --after C`，要求结论通过、未登记与人工动作为 0、摘要边恰好只有 release-catalog.json，
     并按 Claude 的 source transition 校验（与 record-candidate-build 同一函数）验收。
   仓库当前提交的 Claude 目录必须就是 VC-3 冻结的 Active 目录：不同说明主线在 VC-3 之后改过目录，候选链停下；r≥2 的候选也从
   不含候选目录改动的提交开始。投影策略进了受管树，部署含它的候选树之前先登记一次 control 层工具演进。
3. 采集主机 `arm64-vc4-gates.sh` 与 `vc4-all.sh` 同 Codex。续跑凭证与实现门禁经 `vc4_client.py` 取 Claude 的绑定：门禁需求与
   record-candidate-build 的入口门同一推法，前序构建收据认客户端中立合同；Claude 不复用实现测试（`early-test-mode` 恒为 full，
   重跑计划不读前序构建收据，r≥2 一律全量重跑，登记为后续项）。最后一步派发 `vc4-claude.sh`：
   * 实现测试收据沿用 `vc4-facts.sh`；
   * `claude_vc4.py plan-gates` 用 `claude_upgrade_batch` 的受管动作生成 plan-candidate-gates 计划，并按 VC-4 阶段幂等重派合同
     自检；`plan-rehearsal` 在批次外以同一组旗标跑一次生成模式（输出换成 `gate-plan-rehearsal.json`），与提交内 `gate-plan.json`
     逐字节比对后删除；随后经 `vc-batch.sh` 派发；
   * `record-plan` 生成十九个旗标的 record-candidate-build 计划（用途取 Campaign 冻结的 `campaign_purpose`）；`record-preview`
     加 `--preview` 在批次外跑零写入预览，前后比对 Campaign 写域（`candidates`、`control/vc`、`control-store`）；随后派发；
   * `summary` 打印 VC-4 checkpoint、候选构建收据（`official-client-upgrade-candidate-build-receipt/v1`）与计时账本状态。
* 第一条真实链的前提：B3-04b（Go 侧 Release 钉住解耦）合入、Go 加载器不再写死 Active 版本的模型角色——在那之前候选链会在
  C 段的 Go 核对处停下。

## Claude 的 VC-5 驱动三段（Claude 升级 B5b-01b）

`vc5-all.sh` 按客户端表的四个派发脚本表项调用（Codex：`vc5-start.sh`／`vc5-seal.sh`／`vc5-accept.sh`／`vc5-canonical2.sh`，流程与
命令行不变；Claude：`vc5-claude-start.sh`／`vc5-claude-seal.sh`／`vc5-claude-accept.sh`，没有 canonical 交接）。每个 VC-5 脚本在
`source lib.sh` 前写 `CLIENT_STEP_REQUIRED=(<表变量> <脚本名>)`，本轮客户端的这一步不由它负责即在解析阶段之前退出 3；Codex 的
`gen_vc5_plans.py` 遇到别的派发脚本同样拒绝。动作计划一律由 `claude_vc5.py` 经受管 `claude_upgrade_batch` 生成，阶段项与 Codex
同名（`candidate-run`、`candidate-seal`、`compare`、`assert-rules`、`acceptance`）。

1. run：`vc5-claude-start.sh` 沿用 Codex 的锁、准入、`guard.sh pre-vc5` 与后台批次写法；`claude_vc5.py run-plan` 生成受管
   `capture-candidate run` 计划；`vc5-claude-switch.sh candidate <标签>` 从 compose 备份改写 compose：镜像换候选标签，环境变量块写入
   `GATEWAY_CLAUDE_FW_G_CANDIDATE_ENABLED=true`、登记的隔离实例 `GATEWAY_OFFICIAL_EGRESS_GUARD_INSTANCE_ID=ARM64`（Go 只在登记的
   隔离候选实例 ARM64 放行候选开关，生产实例 DMIT、BWG 均拒绝）与激活事实那组 `GATEWAY_CLAUDE_EGRESS_ACTIVATION_*`（身份取 VC-4 构建收据，不写批准
   摘要）；备份里已有这些变量即拒绝。起服务后 `activation-check` 核对激活事实是候选 Runtime 且与构建收据逐项一致，`precheck`
   只读调候选 run 的正式入口门。`restore` 交给 `vc5-switch.sh`（拷回备份，以上变量一并撤回）。
2. seal 链：`vc5-claude-seal.sh <attempt>` 每步先由 `claude_vc5.py seal-state` 判定下一步：两项边界核对
   （`capture-candidate boundary-check`，入口进主线前登记为待接入 `tool:vc5-boundary-check`，走到这里退出 3）→ Desktop／VS Code
   客户端收据没就位即暂停 → 当前输入下没有 seal 预览就派发预览（带 `--client-evidence` 与 `--gate-execution-overrides`）→ 闭合门禁
   blocked 即 `SEAL_ABORT` → 等审核人批准。驱动不替审核人批准：没给 `--approve-seal-sha256 <review_sha256> --approved-by <审核人>`
   即暂停；给了且与当前预览一致就派发「批准 + compare」一个批次。已封存而当前基线没有比较收据时只派发 compare。
3. accept 链：`vc5-claude-accept.sh <attempt>` 要求后台验证已通过；assert 批次的 builder 配置、输出、结果目录与评估基线取比较
   收据登记的那一份（`--reuse-authority none`），批次计划由 `phase_context` 逐项核对；目标平台门禁与 Codex 同一套；门禁 facts 的
   subject 取 `claude_vc5.py gate-subject`（`build_gate_facts.py --subject`，Campaign 与候选必须与本轮一致）；accept 批次写 Store
   验收链、accept 阶段结果与 VC-5 checkpoint（`phase_require --require-completion VC-5`）。外部门禁收据有失败门禁时 accept 只把
   这一轮定稿进 Store、批次失败，下一步是 `capture-candidate seal --revalidate`。

* 暂停：写 `$RUNROOT/vc5-pause.json`（`claude_vc5.py pause`），`vc5-all.sh` 读到后打印 `VC5_ALL_PAUSED reason=… next=…` 并退出 0；
  两处暂停是 `client_receipts_missing` 与 `seal_approval_required`，处置后重起 `vc5-all.sh` 续跑。
* 门禁执行主机覆盖（`claude_vc5.py gate-overrides`）：B4-02 封存时冻结的门禁执行计划与门禁收据逐字比对主机与架构。两条本机
  门禁取 `$GATE_ROOT/local` 注入记录的 `host`／`architecture`，目标平台门禁按 `write_gate_json` 同一算法现算。覆盖并入 seal 代号，
  预览与批准必须是同一份；`--revalidate` 沿用第 1 轮冻结请求，不接受覆盖。
* 待接入：`tool:vc5-boundary-check`（B4-01 f 段）、`tool:vc5-recover`（B4-05b 与 B4-01c）。

## Claude Mac v2 与真实交付：本机合同和现场验收分层

本节对应获批的真实交付合同 A、Mac v2 合同 B 及六补项。当前状态必须分别读取：

| 范围 | 本机状态 | 现场与最终认证 |
|---|---|---|
| Mac v2 来源／导入 | CLI／批次、门禁适配、预约发现、provenance 及两 wrapper 的读侧已接；内部 fixture 与正式产品工厂的实现边界见 Claude 指南 §3.6 与共享实现文档 §12.1 | 工具实现与发布认证不授予签名安装、原生权限、真实产品或三官方客户端真账号现场资格；正式来源仅在当前资格逐项通过后受控登记，缺资格仍在启动前拒绝 |
| 严格候选交付 | 已提交 `551a01c17`，89 项本机通过；typed 执行证明、Store／阶段结果／checkpoint 消费已接 | 固定 arm64-isolated 工厂及 guard 来源已接受管安装，58 项安装接线局部回归通过；空注册表保留测试接缝。ARM64 实际安装、场地、可信回退、专用凭据、四阶段／异常恢复待现场验收 |
| 共享 D4／r2 与发布 | D4／r2 与五项恢复后继修复已合入；当前 policy_version=15，schema 仍 v3、文件名仍 _v2.json；本机组合专项保留形成期效力 | ARM64 九条必需链、pre-A3 与当前发布认证按 Claude 指南 §4.0.5 的实际原件核验；VC-0 收口还须核本轮输入与准入，真账号首派及现场升级结果另验 |

固定守卫安装入口为 `python3 -B /root/arm64-capture-driver/driver/delivery_guard.py`。先运行
`prepare --instance <实例>`，真实采样 systemd 自动重启与旧进程组退出；此时不执行交付动作。取得安装
绑定后才冻结配置 v2／execution context，再运行 `activate --instance <实例> --execution-context <绝对路径>`，
必须核完整注册链、注册头与启动 readiness。配置一旦进入启用，损坏只能修复后重入，不能退回自检。
此安装原件不代替 Docker 回退、账号资格或四阶段交付证据；完整操作合同见
[Claude 指南 §4.6.2](../../docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#462-arm64-隔离候选环境四阶段交付)。

Mac 产品物料冻结及只读计划已补实读安装树入口，73 项定向测试与两套真实安装树检查通过；
正式模板、来源与产品启动资格仍关闭，不能据此完成 B4-01。

通用 Store 的父域读集补正与 v2 重放结果已接入，合同和严格扫描边界见
[共享实现 §3.2](../../docs/OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md#32-合同版本)。实现存在不能证明
旧大图已经完整重放，也不授予当前写入、执行或发布权限。

fixture 薄记录器已接九方法协议、真实 StateJournal、首次 partial 与追加恢复诊断；记录器、
状态、预算、配方与双客户端身份专项在 `059d87e52` 上 165 项通过、零跳过。准确准入与原件边界见
[共享实现 §12.1](../../docs/OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md#121-候选采集来源复用与封存)。
当前 formal、完整 v2 成功与正式来源仍拒绝，真实产品后端及现场采集未就绪。

内部 owned-v2 原生进程管线已完成有限源码、私有编译与离线接线；合入源码的原生／身份组合86项专项通过，生成物未执行。旧v1启动拒绝不变，五能力旗标恒false；签名及运行authority、真实窗口、产品模块、transport-map和正式来源仍待，不能据此核销B4-01整项。

Mac 离线 `client-evidence prepare／verify／import` 不启动产品；live 协议要复用原 Candidate 准入、
reservation／lease 和调度前预算／身份复核。原 attempt 的 `client-evidence/reservations/<producer_run_id>.json`
是请求前登记分母，live 原件固定在 `client-evidence/<client>/runs/<task_nonce>/`。本地缺捕获、尚未同步或
未导入均保持 unresolved；进入 dispatch-intent 后重入只同步／导入，不再启动产品。真实产品、进程实例、
加载模块、socket 和独立连接／ordinal／代理映射要全部闭合；统一当前成功 reader 强制完整 v2。
`client-run` 已进入主 CLI／批次与现有门禁，但正式产品观测、来源工厂与完整 v2 联合来源未闭合时
必须在产品启动前拒绝，不能把本机替身通过当成正式来源登记。

已发请求仍按原 producer、`claude_mac_candidate` 与 capture／connection／ordinal 键入账，失败、拒绝、
超额和非法 payload 不删除、不截断。导入继续使用 accounting-intent → COMMIT → outbox → ledger；
历史 v1 已有原位置只读账务发现，准确 Store／封存 authority 的可信形成期入口仍待补；现有发现
不授予形成期成功审计或当前 seal／accept 权限，也不转换、重签历史材料。不得用当前安装的新产品
为旧 Catalog 的 embedded CLI 项签成功，须先核来源并按正式链更新 Candidate revision。

候选真实交付使用 Plan v2、StageFact v3（Fact v2 外壳）、typed operational v2、Package v2、Receipt v4
和阶段结果 v2。执行配置明确到 ARM64 主机、应用服务、根目录、固定镜像、可信回退、recipe、private-ref
及 guard 部署；允许集合仅为 ARM64，生产 selector／目录不进入。零模型探针为鉴权与无凭据拒绝的
GET /v1/models，核业务分组／模型及只读数据事实，health 只占一项；独立记录重算模型 POST 为零。

切换前 journal 已 fsync 且独立 guard armed；物理 intent → guard execution_committed → 封存执行证据
→ 独立 publication_intent → 原 Store／账本发布。发布断点保留原 producer、时间、formed_under 与
幂等键；首次执行证明固定，后续恢复原件单独追加。物理提交后中断只补后处理，不能重复切换；未提交的
异常、强杀和失联由 guard 恢复固定回退并留下真实复核原件。guard／Docker 不可用期间保持恢复未决，
重新可用后先恢复所有操作再放行。全部成功、失败、恢复和未落 StageFact 的操作均按 typed 闭集归档。

直接 Store 发布、reused、finalizer、当前 status、阶段结果与 VC-6 checkpoint 都须重验新交付语义与版本
下限；历史幂等引用不能包装成当前完成。validation_only 完成候选交付，production_replacement 仍等待
B4-08 的独立生产链。详细字段、断点和后继边界见
[Claude 指南第四部分](../../docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md#claude-vc-5) 与
[Framework §5.3.4](../../docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md#534-失败恢复与身份边界)。

## 幂等与证据边界（2026-09-22 v14r4 批次 15 事故后的硬规则）

* 已封存的官方证据由 `reuse-official-evidence` 自动登记 VC-0／VC-1 完成；无需另运行账本对齐脚本。
  目录发布后中断，使用相同命令、目录与输入继续，只补物化、总账注册和缺失事件。账号、控制收据或其他
  输入漂移仍拒绝。未封存的官方 attempt 保持 awaiting_receipts，完成 seal 后可重跑原导入命令补齐阶段事件。
* VC-4 的前端和门禁等待均监视实际子进程；任一死亡或超过阶段／项目时限，打印相关日志尾 200 行并退出 3。
  上传方每 30 秒更新 `impl-logs/HEARTBEAT`；等待方在 5 分钟无心跳或总时限到期时退出 3。心跳只判断存活，
  完成必须有 `READY`，并逐文件核验上传清单及日志中的候选／承接提交。
  `lib.sh` 的 `wait_for_marker` 第 4 个参数是可选 PID：不以 `--` 开头时才按 PID 取走（空串表示不绑定），
  省略 PID 直接跟 `--log` 等选项时选项原样交给等待器；非空 PID 不是正整数时直接退出 3。
* 上传中断后执行 `bash vc4-all.sh --resume-from upload-wait`。工具先核验 `E.txt` 指向的 `upload-wait.json`：
  四树 HEAD、实际源码摘要、架构、affected 闭集、go.sum／vendor、Go／Node、基础镜像 digest、构建参数、本轮批准门禁的成功
  日志与镜像／二进制／dist／context 产物。完整实现测试收据尚未生成时不要求它存在；上传后生成并正式 replay。
  已有完整收据时仍按同一输入合同检查；普通重起只有完整收据及这些绑定全部通过才跳过四树、门禁和构建。
  旧记录缺少中间凭证时重新执行，不自动追认旧日志。驱动输入或产物变化时必须重做受影响阶段。
* 只换镜像时先按正式流程作废旧候选。`vc4-all.sh` 按当前账本自动选择首次登记或 `--supersedes`，
  构建后用 `retest-plan.json` 冻结真实输入差异：完全一致时实现测试执行 0、复用全部；只变构建参数时
  重跑目标平台批准门禁并复用本机 `check-egress-spec`；依赖、工具链或源码变化时完整执行。
  承接收据保留 `reused_from` 与原始来源，`vc4.sh` 逐级 replay 后正式 record。即使全部复用，镜像、
  context 和 dist 仍实际装配复验；仅换镜像不会要求重新上传本机测试日志。旧记录缺完整输入证明时不复用。
  同源码重建保留原二进制 ldflags，实际本轮构建时间另记在 `built-at-utc.txt`，不会仅因日期变化重跑测试。
  新收据写入后，旧工具不认识 `build.inputs`／显式复用字段，不能直接回退；必须先用实际回退副本做只读回放，
  不兼容时保持暂停并以前进修复恢复。旧收据在新工具下继续按原合同读取。
* VC-5 后台脚本首先写 `vc5-run-batch.pid`，等待方同时检查该真实 run PID 和本次父 run 的新鲜 heartbeat，
  不监视已经退出的 `setsid -f` 启动器。正式 CLI 非零时包装脚本不写 `RUN_BATCH_DONE`。上述等待失败不写 Campaign
  状态或账本事件，仍按原恢复协议处理；seal 与 switch 的既有有限重试保持不变。

* `vc5-all.sh` 按阶段产物续跑：compare 结果存在不再进 seal 链；acceptance 结果存在不再进 accept；目标平台
  门禁失败在 `vc5-accept.sh` 内归档后重跑，绝不回到 seal 链。
* 权限收口只在 `vc5-permission-closeout.sh`：manifest（`evidence-manifest.json`）不存在时只对不合规条目
  chmod／chown（重复执行零调用）；manifest 存在后只核对、绝不修改——即使模式不变，chmod 也会让 ctime 漂移，
  读侧判 `evidence-integrity` 永久停线。
* `vc5-seal.sh` 在 manifest 存在时不再派发 Kilo／seal checkpoint／assertion bundle／seal 预览任何写动作；四项前置
  产物缺一即失败关闭（退出 3，需人工裁定），只允许读侧复核与批准 + compare。
* attempt 根外或未纳入 manifest 的控制制品（门禁目录、断言目录）仍按各自合同 write-once，不因此禁止写入。

## 修复轮的后台验证与批次边界（E4-01，`driver/background-validate.sh`）

* 规则（指南“修好接着跑”一节）：定向回归通过即部署接着跑；全量门禁挪到后台，由同一个统一调度执行器跑，结论按修复提交与
  部署收据绑定，每个批次边界检查；采集批次开始前向调度器申请整机。
* 起：`bash background-validate.sh start <bundle> <分支> <40 位提交>`（fix-and-continue 的 background-validate 步骤自动执行）。
  绑定数据根最新部署收据，`nice` 降优先级跑 `entry-gates.sh --profile full-gates --mode re-execute --require-deployed`
  （门禁前核对数据根部署的就是这个提交；承接记录库里输入没变的单元，只执行受修复影响的），测试树与缓存和前台入口门禁同一个
  目录（单元规格与环境指纹里有测试树路径，目录不同记录就互相承接不了），前台在跑时 `--wait-lock` 排队等锁。同一提交＋同一部署
  已有在跑或已有结论就不重复起；在跑的其它后台验证先停下（superseded）。
* 结论：`$RUNROOT/background-validation/<提交前 12 位>-<部署收据摘要前 12 位>.json`（`background-validation/v1`）：running
  （后台进程活着才算）、passed／failed（入口门禁的结论，带没通过的门禁项）、aborted（没有门禁结论：被信号终止、准备失败、
  部署与提交不一致、部署在验证途中换了）、superseded（被新一轮停下）。`status` 列出全部。
* 停：`bash background-validate.sh stop [--reason …]`。修复轮跑定向回归之前用（fix-and-continue 的 regression 步骤自动执行）：
  同一台机器同一时间只能有一个调度器，上一轮的提交也已作废。执行器收到 SIGTERM 会终止全部在跑单元的会话、不留后台进程。
  注意：被停下的运行没有发布运行清单，它执行过的单元记录不能被承接（E3-01 承接要追到原运行清单），下一次验证会重新执行它们。
* 批次边界：`vc-batch.sh`（VC-2～VC-6 的全部批次都经它）派发前执行 `check-boundary`：当前部署绑定的结论是 failed／aborted
  就拒绝派发、退出 3；passed、running 照常；没有结论（没起、被停下或进程已不在）照常派发并提示先 start。随后
  `unit_executor.py acquire --owner vc-batch-<Campaign>-<序号> --owner-pid $$`：后台验证立即停派；隔离测试默认宽限 30 秒，超时先 TERM、5 秒仍存活再 KILL，确认原会话清空才批准。
  命令须显式声明 `reservation_restartable: true`；pre-A3 与一次性预热保持排空等待。申请上限仍为
  `VC_ACQUIRE_TIMEOUT` 秒（默认 3600）。批次结束（含失败退出）`release`，后台核验输入、环境和工具身份后重派被中止单元。VC-0 收口同步派发 VC-1 首批，编排器在
  调收口模块前后同样申请与归还整机。
* VC-5 验收：`vc5-accept.sh` 开头执行 `require-passed`，当前部署的后台验证必须已是 passed，否则退出 3。
* 收尾合入前重新执行全集：`entry-gates.sh --profile full-gates --mode re-execute`（D12：专项审计、一致性验收、升级开工的入口
  空跑与收尾合入前用重新执行全集）。
* 通过之后默认接着空跑入口到 pre-A3（见下一节；`start --no-dryrun` 不接），结果记在后台验证结论的 `dryrun` 字段。
* 两客户端都纳管时（Claude 升级 B5a-10；数据根 `control` 里两客户端都有部署收据）：
  * 仓库全量门禁两客户端只跑一次（测试集合已含 Claude 测试）。结论绑定部署收据集合（每个已纳管客户端最新的一份，交换共享根
    的一次部署即该事务签出的两份），写进结论的 `deployments`，文件名的部署摘要换成集合摘要；入口门禁带
    `--require-deployed-all`，本轮客户端之外每个已纳管客户端的部署也核对是本提交（`entry_steps.py deploy-check`：整树摘要按
    该客户端策略在测试树里现算、部署一致性各部分逐项比内容），门禁结束时集合必须没变，否则 aborted。
  * 两客户端的轮次各有 RUNROOT，所以另在数据根 `control/background-validation/` 登记「提交＋集合 → 结论文件」：另一客户端
    的轮次 `start` 查到同一提交与集合已有在跑或已有结论就不再起，只登记自己的入口空跑；门禁通过后两客户端的入口空跑都接上
    （验证进行中搭上的由后台 run 接，已通过时搭上的当场接）。起新的一次时，登记里旧集合的在跑验证（别的 RUNROOT）一并停下。
    搭上的轮次在自己 RUNROOT 的 `background-validation/attached/` 留指针：它的 `stop`（修复轮跑定向回归前）也停得下这一次，
    `status` 也列出它（标 `attached`）。
  * `start`、`check-boundary`、`require-passed` 按本轮客户端（参数 `CLIENT`）取它的部署收据：Claude 的批次边界与 VC-5 前核对
    取到的是绑定了 Claude 收据的那份结论（本 RUNROOT 找不到再找数据根登记）。只有一个客户端纳管时结论、文件名与命令行与
    改造前逐字相同。

## 入口空跑（E4-02，`driver/entry-dryrun.sh`）

* 用途：入口平时不跑（CI 不跑真实链），遗留缺陷会攒到下次升级过入口时才一个一个冒出来。每次受管工具部署后空跑一次、一次报全；
  升级开工的第一步固定是空跑。
* 起：`bash entry-dryrun.sh start <bundle> <分支> <40 位提交> [--opening] [--to <步骤>]`，后台执行、`nice` 降优先级。在数据根
  `staging/entry-dryrun-<UTC>/` 演练根里（入口编排器的 `ENTRY_ROOT`，配一本 fixture_only 演练总账）用 `entry.sh` 跑一遍入口，零请求，
  生产项目总账、正式计时账本与正式认证坐标都不写；演练参数文件按本轮参数文件只改产物坐标（RUNROOT、STAMP、项目截止、三份认证
  与 pre-A3 认证、ENTRY_*），编排器在 `env -i` 干净环境里启动。默认到 `atomic-double`（建账本之前那一段）；`--to p0-receipt`
  连建账本之后一起（VC-0 收口会发正式请求，演练根里一律不做）。`--opening`：升级开工的空跑，重新执行全集并带读集审计
  （`--reexecute-gates --audit-reads`），默认到 `p0-receipt`；`--reexecute`：只重新执行全集、不带审计（冷跑计时用，审计约多
  两倍开销）。
* 每次部署后：后台验证（上一节）通过之后自动接着空跑到 `pre-a3`——便宜检查、策略认证、入口门禁与 pre-A3 场景都受统一调度，
  同一份记录库里刚验过的门禁单元全部承接，真正执行的只有 pre-A3 场景（输入变了的）与便宜检查；零请求 smoke、atomic-double 与
  建账本之后的步骤要用采集容器，只在明确执行的空跑里跑。
* 让路：有采集在跑（执行器整机预约的申请方还活着）就不起，结论记 `yielded`、退出 4；空跑不申请整机，不挡采集。
* 日常维护（空跑和升级开工前先查磁盘余量）：每次空跑开跑前先清 Go 编译缓存里 6 小时以上没用过的条目
  （`go env GOCACHE`，Go 用到一个条目会把它的修改时间刷新到一小时以内）、清理单元执行记录库（见上文「单元执行记录与承接」）、
  只留最近 5 次空跑的演练根与空跑产物（在跑的不动）、部署留下的旧暂存树（数据根 `staging/<名>-managed-tools.superseded-<UTC>`，
  没有收据或账本引用）每组只留最近 2 份（E4-03），再查根盘：越过停线（与 `guard.sh` 同一条：已用超过 69%，或可用少于
  40 GiB 与 `MIN_FREE_GIB` 中较大的那个）就不跑，结论 failed、没通过的一步记 `disk`。报告摘要在结论的 `housekeeping`，全文在
  结论旁的 `<结论名>.housekeeping.json`。单独做一遍：`bash entry-dryrun.sh housekeeping [--dry-run]`（退出码 5＝越过停线）。
* 停：`bash entry-dryrun.sh stop`。空跑与前台入口门禁共用调度器和入口门禁工作目录，修复轮跑定向回归之前要停下——
  `background-validate.sh stop`（修好接着跑的 regression 步骤会执行）连空跑一起停。
* 结论：`$RUNROOT/entry-dryrun/<提交前 12 位>-<部署收据摘要前 12 位>[-opening|-reexecute][-to-<终点>].json`（`entry-dryrun/v1`；终点不是
  这种空跑的默认终点时才带，后台验证接着的那次是 `-to-pre-a3`），状态 running／passed／
  failed／yielded，带编排器每一步的动作、结论与原因（没通过的几步一次列全）、演练根与日志位置；同一提交＋同一部署（同一种空跑）
  已有在跑或已有结论就不重复跑。`status` 列出全部。演练根与空跑产物只留最近 5 次（日常维护自动清）。
* 按客户端（Claude 升级 B5a-10）：本轮客户端取参数文件的 `CLIENT`，空跑绑定该客户端最新的部署收据，结论文件名带客户端表的
  前缀（Codex 为空，与改造前同名同形；Claude 是 `claude-`），同一提交＋同一部署两客户端各一份；演练总账按该客户端建。Claude 的
  结论另带没通过各步的分类 `failure_classes`：客户端表登记的待接入步骤记 `registered_pending`（B5a-09 续落实 atomic-double 之后
  Claude 已没有待接入的入口步骤）；pre-A3 一步按
  演练根里的 pre-A3 认证（没通过时是旁路文件）登记的记录库与运行清单逐场景读日志里的场景结果，链体停在登记占位上（指标
  `pending`，如「等 B2-08」）、错误信息就是占位、零请求零网络、隔离没有降级的记 `scenario_pending`，其余记 `real`；被阻塞的
  记 `blocked`。`real_failed_steps` 为空＝只差登记过的待接入与占位，没有真实失败。
* 逐单元承接（B5a-08 合同）：`start … --unit-inherit-request <请求>` 把请求原样交给入口编排器，入口门禁与 pre-A3 只执行依赖变化
  或缺少有效记录的单元；结论文件名另带 `-unit-inherit-<请求摘要前 12 位>`；与 `--opening`／`--reexecute` 互斥。
  同时提供 `--unit-inherit-full-gates-request <配套请求>` 时，结论还绑定配套文件摘要，命名另加 `-full-gates-<摘要前 12 位>`；
  排队结束、执行前核对两份字节仍与结论一致，任一变化即失败，不借旧结论执行新批准对象。

## 修好接着跑一条命令（`driver/fix-and-continue.sh`）

* 用途：候选采集（VC-5）续跑的一轮"修复 → 部署 → 登记 → 对账 → 批准 → 重派"由一条命令编排，取代按 sed 复制改写的
  upload-rN／repair-rN 轮次脚本。每轮只换一份轮次参数文件（模板 `driver/fix-and-continue.example.params`，经
  `parse_env.py` 同一词法层安全解析、与 `$ARM64_VC_ENV` 交叉核对）＋本机生成的期望摘要 JSON（EXPECT：部署收据的整树／
  五摘要／监督器、数据根 wire 闭包、守护基线与差异）与入口断言 JSON（ENTRY_GREPS）。
* 修复轮规则（E4-01，见指南“修好接着跑”一节与下文「修复轮的后台验证与批次边界」）：部署之后先跑定向回归（regression 步骤），
  通过即接着走；全量门禁由后台验证跑（background-validate 步骤起），批次边界读它的结论。推送后 CI 与续跑并行、不等待（脚本
  里没有等 CI 的步骤）；后台验证或 CI 失败时暂停续跑，按该轮部署收据的 `rollback_backup` 回滚已部署工具（同样走受监督部署），
  修好后再走一轮；发版前仍须 CI 全绿。
* 用法（采集主机 root）：`setsid -f bash /root/arm64-capture-driver/driver/fix-and-continue.sh <参数文件> > <日志> 2>&1 < /dev/null`；
  `--dry-run` 只读检查打包和动作计划；`--from <步骤>` 续跑（前序步骤在本轮必须有 passed／skipped 记录），`--list` 查看本轮各步骤记录。
* deploy、evolution、deadline-extension、recovery-approve、recovery-authorize 消费 `fix_safety.py` 定义的独立批准，
  精确绑定动作、部署包／恢复摘要和有效期；缺失只写 approval intent 并停止，不能由参数中的 APPROVER 代签。
  `APPROVALS_DIR` 指定凭证目录；失败恢复先核对步骤记录与副作用结果，不能靠重复执行补造批准。
* 步骤：deploy → postdeploy → regression → item-tests → background-validate → evolution → pre-extend → reconcile-runs →
  reconcile-attempt → repair → approve → authorize → extend → accepted → recover。regression 先停下上一轮还在跑的后台验证
  （同一时间只能有一个调度器），再跑入口门禁 regression 组合（参数 REGRESSION_GATES 人工补门禁项）；item-tests 是人工指定的
  实测，给了 ITEM_TESTS 才跑（根因修复的回归收据由草稿生成时要它）；background-validate 起后台验证，本提交在当前部署上已验证
  失败即停。每步幂等（同 HEAD 且收据一致则不重部署、定向回归与实测已过不重跑、后台验证已起不重起、无漂移不登记、
  链尾父 run 已对账不重复、同一修复提交已登记不重复、本轮已启动 vc5-recover 不重复派发），输出写
  `$RUNROOT/fix-and-continue/<轮次>/<步骤>.json`，受管命令原始输出在同目录 `raw/`。
* 停下即退出并打印"下一步"与 `--from` 续跑命令：失败 1，需要人工 4（账务暂停、环境污染、永久停线、需审核、请求预算、
  需要人给出估计上界或证据文件、wire 闭包变化、守护代码变化），驱动被本轮重装更新 5（用新驱动 `--from regression`）。
  本脚本从不调用 accounting-resolve／environment-isolate／campaign-resume／request-budget-extend，从不传 `--force`，不重装守护。
* 阶段延期在对账前（pre-extend）与授权后（extend）各判一次：对账前阶段截止已过时计时账本是 deadline_paused、对账判预算
  暂停且拒绝批准；延期写入又会推进 Campaign 账本 head，放在批准与授权之间会让授权拒绝"账本 head 已推进"。
* 根因修复登记：reconcile-runs 与 reconcile-attempt 对账遇"项目总账根因达上限"暂停，走同一条登记路径——
  shell 函数 `root_cause_repair`（同一判定 repair-plan、同一命令 record-root-cause-repair、同一核对 repair-verdict）。
  reconcile-attempt 照旧记 passed（needs_repair）交 repair 步骤登记、approve 重新对账；reconcile-runs 在 repair 之前，
  参数给了材料就在本步骤内登记、对该对象重新对账一次（仍暂停即停下），没给材料停下时提示 `--from reconcile-runs`。
  受管对账器先写对账收据、入总账再判定，暂停对象的收据按监督器判据核验是通过的：停下记录带 `revisit`，续跑扫描时
  重新对账这些对象，不当作已对账跳过。Campaign 账本 stop_required 不走此路径，只停下等人工 campaign-resume。
* 续跑重新对账：`revisit` 覆盖 reconcile-runs 里任何暂停种类（deadline、accounting、environment、request_budget、
  root_cause_repair 与受管将来新增的种类），重新对账判可恢复即记 done、仍暂停按原提示停下。暂停提示末尾恰好一个
  `--from`，与停下记录的续跑步骤相同：含 deadline 时是 `pre-extend`（在参数文件填 EXTEND_DEADLINE／EXTEND_REASON，
  pre-extend 先于对账执行阶段延期），其余种类按受管提示处理后从停下的步骤续跑。reconcile-attempt／approve 每次都对
  目标 attempt 重新对账，不存在"暂停后被跳过"；它们的 deadline 暂停同样从 pre-extend 续跑。永久停线、需审核与命令
  失败不进 revisit，行为不变。

## B-09 全集收据与开工准入

后台验证使用 `re-execute`。可附带 `--full-set-context <JSON> --full-set-clock <JSON>` 与
`--audit-policy <JSON>`；通过后只对同一次执行且所有单元完整读集覆盖的结果签发 `full-set-receipt.json`。
覆盖未闭合保留功能通过结果，另外记录拒签，不能把 B-11 的部分覆盖当作全集可承接。

`full_set_receipt.py --source <索引> --context <上下文> --clock <凭证> --output <新文件>` 可独立签发；
输出不可覆盖。签发时刻取原全集最后单元完成 UTC，期限至多 86400 秒；`--predecessor` 关联失效旧收据，
同一旧运行不得作为替代重跑，重复签发不续期。

开工 `entry.sh --full-set-request <JSON> --full-set-work <来源后台的 work 目录>` 或独立 `entry-gates.sh --full-set-request <JSON>` 消费请求。
缺少请求、任一条件失效或开关关闭均重新执行全集。请求使用 `full-set-request/v1`，含 `reuse_enabled`、
`receipt`、`context`、`consumer_clock`、`revocation`、`approvals`；文件索引均为规范绝对路径和完整 SHA-256。
两类批准分别登记三方审核与变更批准的角色、账号、UTC、有效期、合同与绑定摘要，工具不代签。

上下文使用 `full-set-context/v1`，`binding` 字段由模块常量列出，`evidence` 绑定部署、画像权威收据、
运行镜像、外部依赖锁及数据快照。镜像收据须登记 `container_name` 与实际 Docker Image ID；消费时重新查询
容器镜像、Git HEAD、最新部署和主机平台，不能只相信旧 JSON。工具包摘要取源运行执行器包摘要，
平台字段用模块 `platform_fields()`；画像使用内容 Digest，不与收据文件 SHA-256 混用。

校时凭证含 `status=synchronized`、`source`、`offset_seconds`、`sampled_at_utc`；偏差绝对值 ≤60 秒，
采样年龄 ≤300 秒。撤销查询 `full-set-revocation-query/v1` 必须 `status=ok`、`revoked=false`、
`campaign_status=active`，绑定收据自摘要且查询年龄 ≤60 秒。边界 `now >= expires_at` 立即失效。
任何审批撤回、证据更正或身份变化应登记撤销事件并重新执行，不修改旧收据。

入口在排队前和获得调度锁后复核，只接受同一来源的完整集合；`full-set-decision.json` 与运行清单同时记录
实际承接／全量重跑原因、来源 run ID 和原失效时间。开工前每次重新判断，旧步骤缓存不能绕过时效门。
真实复用当前保持关闭；专项批准及全集覆盖闭合后才可提交开启请求。隔离夹具的批准不具有生产效力。

B-09 后台结论到期时自动新建执行；收到撤销或错绑后用 `background-validate.sh start … --rerun-full-set`，
旧终态按内容摘要归档并关联旧收据，仍在运行时拒绝重复派发。校时采样允许由已登记的校时来源更新；
部署、画像与数据等静态上下文在运行期间改变则拒签，不能把新上下文套到旧执行上。

来源与消费工作区及缓存必须保持一致；`--full-set-work` 显式传递已登记的后台独立目录，
工作区重建或路径不同会使严格输入失效并重跑，不能为了承接而忽略路径差异。

## B-10 目标平台单元承接

VC-5 缺省仍执行原目标平台 `make test`。在本轮参数中显式设置 `VC5_TARGET_REQUEST` 才进入
`target_platform_gate.py run`：从固定八类门禁生成完整计划，以同一个 B-09 全集来源选择匹配单元，
差异与新增单元完整执行。读集未闭合、共享身份变化、审批或来源失效时全部重跑。
可设置 `VC5_TARGET_TREE`、`VC5_TARGET_PYCACHE` 和 `VC5_TARGET_RECORD_STORE` 指向来源已登记的
工作树、缓存和记录库；工作树必须与候选测试树同提交且干净，现有缓存只读使用，宿主输入逐次重算。
路径不同导致不匹配时仍重跑，不自动迁移或改写原记录。

请求为 `target-platform-request/v1`，含 B-09 的 `receipt/context/consumer_clock/revocation/reuse_enabled`，
两类 `approvals` 的 scope 改为 `target-platform-inheritance/v1`，另需 `platform_compatibility` 签字凭证。
签字凭证含 `status=approved`、账号、scope、`binding_sha256`、批准与失效 UTC。
`target` 精确登记 Campaign、Candidate、画像 Digest、目标架构、候选源码摘要和候选 Image ID；
字段名见 `SUBJECT_FIELDS`，两类批准及平台签字还必须绑定 `target_sha256`。
执行环境的运行镜像与被验收候选镜像分别绑定，不能假设相等。工具不签批准，演练凭证不用于生产。

判定在排队前、取锁后和差异执行结束时复核；结束时失效不得发布通过清单。
`target-units/evidence.json` 绑定计划、执行汇总、逐单元清单、封存的动态查询及原失效时间。
正式外部门禁 v4 的 `unit_execution` 字段引用此证据，实际命令明确记录为单元调度入口。
受管生成器按钉住的五个验证器摘要加载仓库源码或安装驱动，拒绝缺件和漂移；修改这些文件须同步
更新生成器的 `TARGET_VERIFIER_SHA256S`、旧 producer 只读登记及必要冻结承接。

`vc5-accept.sh` 在消费旧通过文件及派发 accept 前重新核验。失效或失败的门禁、环境及派生事实
归档到 `superseded/`，索引保留原路径、归档路径和逐文件完整摘要；本机门禁与采集封存包不动。
已正式 accept 的结果只作历史重放，后续过期不改写已完成的历史阶段。
工程代码和驱动安装不自动部署新受管收据合同，也不启用真实承接；受管合同的监督部署、
完整读集覆盖和专项批准仍需闭合后才允许真实消费。

### C-02：三组后端测试共用一次 Go 预编译

`entry-gates.sh` 的 `entry`／`full-gates` 重新执行路径默认先运行一次 `go build ./...`，只编译生产包和依赖，不运行测试或 `TestMain`。三组正式 `go test` 命令、标签和覆盖保持原样；`--go-warmup off` 关闭预热。已有 B-09 承接请求时跳过预热，不改动来源缓存。

预热通过同一统一执行器的独占准备单元运行，遵守整机预约、进程回收与日志规则；任何编译失败都在正式门禁派发前停止，诊断不会重复编译或覆盖首份收据。`GOCACHE`、`GOMODCACHE`、`GOTMPDIR` 默认取本次 `--work` 根下的独立目录；显式缓存必须在该工作根内、测试树外且互不包含，外部共享目录或符号链接拒绝。后台验证沿用 A-04 已绑定的缓存目录。

`go-compile-warmup.json` 记录提交、后端源码摘要、Go 二进制与环境摘要、三个缓存根、编译日志、退出码和耗时；对应准备执行器的目录为 `go-compile-warmup-executor/`。这是编译收据，不是测试通过凭证，不能承接测试结论。性能比较必须把预热和三组测试全部耗时相加，不只比较预热后的热跑部分。

## B5a-08 逐单元承接（`driver/unit_inherit.py`）

两客户端共用的受控承接入口：来源记录可来自多次运行、多个提交，逐单元判定，不能承接的单元只重跑它自己。缺省仍是
`re-execute`；`full-set-pass` 与 B-09 保留给需要全集同源的场合。

* 入口：`entry.sh --unit-inherit-request <JSON>`（与 `--full-set-request`、`--reexecute-gates` 互斥）或独立
  `entry-gates.sh --mode unit-inherit --unit-inherit-request <JSON> [--unit-inherit-stage pre-ledger|post-ledger|formal:<id>]`。
  没给请求时回退重新执行全集。与 B-09 一样核对部署、沿用能重放的原工作区、不做 Go 预热、通过后保留测试树；可带
  `--audit-reads [--audit-strict]` 与读集合同，宿主输入清单与收窄提案只许 `re-execute`。编排器给了请求时，入口门禁与
  pre-A3 两步另把请求文件与 `unit_inherit.py` 列入输入，请求一换两步都重做。
  同一入口续跑可从首轮固定带 `--unit-inherit-full-gates-request <JSON>`：主请求绑定实际 `entry` 组合，配套请求单独批准实际
  `full-gates` 组合；pre-A3 真跑时用主请求，已有有效步骤记录而沿用认证时仍实时跑门禁，并用配套请求。两份文件始终一起进入
  这两步的输入，重跑同一条命令不临时换请求。只承接所选请求原批准来源中的有效记录；首轮新记录不会自动成为第二轮来源，
  未批准或失效的单元仍执行。未提供配套请求时不改写主请求、批准或门禁全集，保持原合同的拒绝与回退。
* 当前上下文 `unit-inherit-context/v1`：客户端、入口阶段、测试树提交、`control` 里每个已纳管客户端最新的部署收据、环境
  指纹、调度策略版本、执行器版本、门禁全集与组合、记录库。执行器在每个复核点现算，与请求登记的逐字段相同才承接。
* 请求 `unit-inherit-request/v1`：`reuse_enabled`、`context`（文件索引）、`sources`（`unit-inherit-sources/v1`：承接单元 →
  来源运行与记录摘要，来源运行 → 已发布清单摘要）、`consumer_clock`、`revocation`、两类 `approvals`（scope `unit-inherit/v1`，
  `binding_sha256` 绑定上下文摘要与来源集合摘要）；可选 `max_age_hours`（只能调小）、`clock_source`（`chronyc` 缺省，或
  `file:<绝对路径>`）、`campaign_status_source`（Formal 阶段必需，`file:<绝对路径>`）。缺任一项或任一项失效即整次重新执行。
  工具不签批准。
* 可承接＝`check_record` 全部通过且要求读集闭合、来源清单已发布且摘要与来源集合相符、在期限内、来源运行未撤销；来源记录
  登记的宿主快照按 B-10 同法沿用并重新快照（测试树重建、文件元数据变化即判输入变了重跑）。
* 复核点：排队前、取得调度锁后、发布清单前各一次，每次重新取校时样本与撤销查询（以新文件追加进记录库
  `unit-inherit/evidence/`）、重读请求核对固定部分与批准原文件、重做每条承接记录的核对；锁后只缩不扩，发布前失败即不发布
  清单（已执行单元的记录保留）。决策收据 `unit-inherit-decision/v1` 随清单发布到 `unit-inherit/decisions/<run_id>.json`。
* 消费端：P0 收据签发与 VC-0 收口（`codex_upgrade_vc_receipt`）、pre-A3 签发（认证模块 `issue`）按决策收据重验形成期
  证明并取消费时的校时、撤销与批准（写 `unit-inherit/consumptions/<run_id>/`）；认证核验按形成期快照与签发时刻历史重放。
  验证器从仓库 `tools/ci` 或本目录 `driver/` 加载。请求与批准原文件在消费完成前必须留在原处；撤回批准＝改动或删除批准文件。
* 工具：`entry-gates.sh <与正式运行相同的组合与坐标> --unit-inherit-propose <新目录>` 在同一执行环境里让执行器只判定、写出
  提案（`context.json`、`sources.json`、`binding.json`）后退出 0，不执行、不导出、不签发（上下文含环境指纹，提案必须与
  正式运行同一环境）；`unit_inherit.py sample-clock`、`query-revocation`、`revoke`（登记撤销一次来源运行，只增）、
  `assemble-request`、`verify [--historical]`。
* B5a-10 起：记录库清理按所属运行处理 `unit-inherit/` 目录（见上文「单元执行记录与承接」的清理）；入口空跑可带
  `--unit-inherit-request` 按本模式承接。后台验证仍是重新执行全集（它本身就是 B-09 全集收据与本模式的来源运行），后台验证
  接着的空跑不带请求。尚未覆盖：Formal 阶段的 Campaign 状态文件由谁维护留给建账本之后的工作项。

## 整机预约中止与重派（C-03）

预约批准绑定完整请求摘要，同名 owner 的下一次请求不能复用旧批准。`reservation_grace_seconds` 默认 30、`reservation_stop_seconds` 默认 5；将前者设为 `null` 恢复等待自然结束。并行与独占段都在等待时观察预约；撤回的预约停止后续中止操作。

中止尝试写为 `reservation-aborted`，不可承接，不计测试失败或诊断。每次重派独立命名日志、结果和收据，最终正式记录以 `resumed_from` 关联中止记录；汇总的 `reservation_interruptions` 单独列账。部分审计轨迹只作中止证据保留。Python 断言、错误和意外成功先即时留档再打印；已观察失败即使随后被预约中止、诊断重跑通过，仍保留正式失败。实际非零测试失败继续计失败，无法清空会话或重派前身份漂移均拒绝继续。
