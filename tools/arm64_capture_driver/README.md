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
* 开发侧改动脚本后必须 `python3 tools/arm64_capture_driver/install.py build-manifest --root tools/arm64_capture_driver`
  刷新清单；`tests/test_arm64_capture_driver.py` 以 `--check` 门禁清单与目录一致。
* 顶层精确闭合：根目录只允许 `install.py`、`README.md`、`manifest.json`、`driver/`，多任何一项（含 `__pycache__`）
  清单生成与复验都失败；安装态 `manifest.json` 自身 0600／root 也在复验范围内。
* 参数文件不 `source`：`lib.sh` 经 `driver/parse_env.py` 解析（精确键集合、值只允许引用已定义键、任何命令形态拒绝），
  只 `eval` 引号化后的赋值。

## 每轮流程（参数全部来自 `$ARM64_VC_ENV`，模板 `driver/env.example.sh`）

1. 本机：`cp driver/env.example.sh` → 填写 ROUND／STAMP／C／DC／RECEIPT 等 → 传到采集主机 `$RUNROOT/env.sh`。
2. 采集主机：先跑入口门禁 `ARM64_VC_ENV=$RUNROOT/env.sh setsid -f bash driver/entry-gates.sh <bundle> <分支> <40 位提交>`
   （E2-04，见下文“入口门禁一次运行”：全部门禁、pre-A3 认证、P0 证据与 VC-0 预跑记录都取自这一次运行；只签 pre-A3 认证时
   仍可单独用 `pre-a3.sh`，工具身份（五摘要）与策略未变时复用最近一次认证——重新部署也不重跑 pre-A3，跨部署复用登记复用
   收据），再 `ARM64_VC_ENV=$RUNROOT/env.sh bash driver/stage1.sh` 完成预检与演练——stage1 建账本前核验本轮认证，缺失即拒绝
   （账本一建 VC-0 即开始计时，长检查一律放在建账本之前）。新目标首次取证按指南
   `codex_upgrade_vc0_closeout` 完成 Formal VC-0／VC-1 后进入 `vc23.sh`；同目标恢复才用 `pre-all.sh`（stage2 + vc23）。
   closeout 编排必须先执行 `client_launch_probe.py verify --output-dir "$PROBE" --campaign-dir "$PRE"`（坐标取自
   `stage1.env`），通过后才能调用 `codex_upgrade_vc0_closeout`；`stage2.sh` 已内置同一复核。
   VC-0 预跑记录取自入口门禁的那次运行（主体目录下的 `preflight.json`），建账本之后不再单独预跑；入口门禁之后改了源码时，
   按下文“VC-0 预跑目标平台门禁”单独补跑一次，通过才建 Formal Campaign。

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
* 没通过的认证写旁路文件 `<正式文件名>.failed-<时间>.json`，正式路径保持不存在：修好后用同一 STAMP 直接重跑。
* E2-03 的旧路径（`plan --staging-root` → `run-commands` → `issue --executor-summary`，签 v1）与串行入口 `run` 保留作对照与
  回退，旧形式的场景单元在入口门禁里仍标不可承接。
* 耗时（ARM64 4 核、机器上无其他任务，10-01 实测，全部执行时）：整份认证 478 秒，几乎全是最长的
  `vc-chain.vc1-recovery-chain`（477 秒）；其余 43 个场景在它运行期间由其余 3 核跑完。

## 入口门禁一次运行（E2-04，`driver/entry-gates.sh`）

* 一次运行跑完入口要的全部门禁：采集工具测试（测试组，按模块拆单元）、`check-egress-spec` 的全部子检查（Makefile 的
  `EGRESS_SPEC_CHECKS`，每项一个单元）、后端 go test 三组（不带标签、`-tags=unit`、`-tags=integration`，都带 `-count=1`，
  integration 带 `CI=true`，没有 Docker 时失败而不是静默跳过）、golangci-lint 三组、前端三项、CI 里的部署脚本测试（每条
  一个单元）与 pre-A3 的 44 个场景。全部交给驱动随附的统一调度执行器（`unit_executor.py run-gates`）在整机额度内并行，
  一项失败其余照跑，全部跑完再按门禁项汇总；`test-official-client-control` 单列一个门禁项，和 check-egress-spec 的同名
  子检查共用一个单元，只执行一次（Makefile 的 test 目标也不再单列它：子检查跑在执行器另起的 make 进程里，不和先决去重）。
* 组合 `--profile`：`entry`（默认，全部门禁项＋pre-A3）、`full-gates`（不含 pre-A3，`arm64-full-gates.sh` 用）、`preflight`
  （只含 make test 的组成，`vc0-gate-target.sh` 用）、`pre-a3`（只含 pre-A3 场景，单独签 pre-A3 用，E3-02）。`entry` 与
  `pre-a3` 要求数据根已部署本提交（最新部署收据的整树摘要等于测试树的受管树，受管整树、两份指南与部署脚本副本逐项一致，
  否则退出 3）；本轮 pre-A3 认证已有且有效、或有可复用认证时沿用，pre-A3 场景不纳入本次运行。
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
  * `pre-a3.sh`、`stage1.sh`、`stage1-finish.sh`、`stage2.sh` 保留，供单独补跑某一段或对照旧轮次；入口一律用 `entry.sh`。
  * 入口门禁 `entry-gates.sh` 新增 `--policy-activation`、`--pre-a3-certification`、`--pre-a3-mode auto|present|run`，由编排器替它定 pre-A3 沿用还是新跑。
  * 便宜检查结束时写 `summary.json`（逐项状态）。

## P0 收据与 VC-0 收口（E2-07，`entry.sh` 的最后两步）

* P0 收据（原来每轮手写）：两份离线门禁证据取自入口门禁那一次运行（`p0/test-capture-tools.json`、`p0/check-egress-spec.json`），
  加发布认证与回退依据 `P0_ROLLBACK_EVIDENCE`（参数文件新键，上一版本可回退点的收据，例如前序 Campaign 的画像目录晋升收据）；
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
  * `full-set-pass`（全集通过，默认）：先在记录库里给每个单元找可承接的记录，只执行找不到的。可承接＝正式执行、按原始字段
    重新判定为通过、单元规格与输入摘要没变、调度策略版本与环境指纹没变、执行器没变、7 天以内、所在运行的清单把它列为该单元
    的正式执行、日志在库且摘要相符。诊断执行的记录永不承接。
  * `re-execute`（重新执行全集）：一个都不承接。升级开工的入口空跑、一致性验收、收尾合入前用；编排器带 `--reexecute-gates`。
* 输入：采集工具测试单元＝受管工具树（不含测试目录）、本模块静态依赖闭包里的测试文件、夹具目录、docs、仓库其余部分，整目录
  读取测试的另加整个测试目录、闭包里有真实链的另加全部辅助模块与真实链目录、读真实仓库 git 的另加 HEAD；命令单元（子检查、
  后端、lint、前端、部署脚本）按 E2-05 从宽：整个仓库＋HEAD，子检查另加历史源码树。pre-A3 场景按场景声明输入、可以承接
  （E3-02，见上文「pre-A3 路径认证」）；只决定缓存位置的环境变量（字节码前缀、身份记忆目录）不算进单元规格。
  承接模式要求测试树干净，否则本次一律不承接。
* 环境指纹：门禁清单生成与执行器都在白名单环境里运行（`env -i` 只放行 PATH、HOME、语言与时区、TMPDIR、代理与证书、Go／
  Docker／Node／pnpm 的变量），执行器把这份环境整份算进指纹（字节码前缀除外），再加系统、内核、Python 与已装包、dpkg 软件包、
  有效用户、主机名、核数，以及门禁清单给的工具链版本与前端依赖摘要。任何一项变了全部单元不承接。
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

## 读集审计（E3-04，`driver/read_audit.py`）

* 用途：承接（E3-01）与步骤失效判定（E2-05）都靠声明的输入范围，漏声明会把该重跑的判成可以承接。审计让每个单元真跑一遍，
  核对实际读取都在声明里。只在重新执行全集时带：`entry-gates.sh --mode re-execute --audit-reads`，或编排器
  `--reexecute-gates --audit-reads`（升级开工的入口空跑、E4-03 验收各一次）。要求采集主机有 strace（ARM64 是 6.8）。
* 做法：执行器把每个正式执行的单元包在 `strace -f -qq --seccomp-bpf -y -e verbose=none -e signal=none
  -e trace=%file,clone,clone3,fork,vfork` 下（诊断执行不包），输出先经 `grep -E` 预筛（只留测试树、数据根下的路径与
  派生、execve、chdir 行），再交给 `read_audit.py filter`，写 `executor/audit/<单元>.trace.json`；单元结束后按它本次的
  输入明细核对。声明了整个仓库的单元（后端、前端、lint、egress 子检查、部署脚本）不包 strace：它们对仓库不可能有
  未声明读取。不改单元规格、不改执行记录：审计跑出来的记录照常可承接。
* 核对口径：只判读内容、执行、写和探测不存在的路径；stat 成功、打开或列举目录只算元数据、不判（工具身份计算一类遍历会
  stat 树里每个文件再按名字排除）；导入系统试探的 `.so` 扩展模块变体与字节码缓存不看。覆盖＝范围项、闭包文件（含
  `unit_records.EXTRA_TEST_READS` 补登的）、声明的单个文件、HEAD（读 `.git`）、已算明细里的绝对路径（冻结台账目录、
  录制数据全部根目录、项目总账的探测位置）；只读 HEAD 提交号的模块（`HEAD_ID_ONLY_READERS`）读 `.git` 只许 HEAD、refs、
  配置一类。测试树单元读到数据根一律报出；pre-A3 场景在数据根运行，与仓库同布局的部分按仓库相对路径核对，其余按
  豁免表 `DATA_ROOT_EXEMPT`／`DATA_ROOT_EXEMPT_PATTERNS`（场景临时根、git 仓库发现、命名空间包标记、编译器与 unittest
  的试探，每条写明原因）。
* 结论：有未声明读取的单元写进汇总与 `executor/audit/read-audit.json`（仓库或数据根相对路径、读取方式、样例系统调用、
  补声明的建议），整次运行判失败；门禁项结论不变。已知局限：只看元数据、不读内容的依赖（只列目录或只 stat）看不到。

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

## VC-0 预跑目标平台门禁（`driver/vc0-gate-target.sh`）

* 用途：目标平台门禁（采集主机上对候选测试树隔离执行 `make test`，40～70 分钟）原本只在 VC-5 accept 之前跑，门禁自身的
  问题（修好接着跑第 67 项）要到那时才暴露。VC-0 先用当时的候选源码（本轮受管工具部署所在的提交）把同一门禁跑一遍，
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

3. 本机：候选提交链（A／C／D 三段，见 `driver/local/local-candidate-chain.sh`）→ `git bundle` 推到 `$BUNDLE`。本机只做
   提交与打包，不跑测试。
4. 采集主机：先 `setsid -f bash driver/arm64-vc4-gates.sh > $RUNROOT/arm64-vc4-gates.out 2>&1 < /dev/null`（ARM64 版本地门禁，
   见下文“ARM64 全量门禁与 ARM64 版 VC-4 门禁”），末行 `ARM64_VC4_GATES_DONE` 后再
   `setsid -f bash driver/vc4-all.sh > $RUNROOT/vc4-all.out 2>&1 < /dev/null`：READY 已在，上传等待立即通过，两者不并行。
   `driver/local/local-vc4.sh` 是改为 ARM64 门禁之前的本机做法，只为解释旧轮次记录保留，新轮次不用。
5. 采集主机：`setsid -f bash driver/vc5-all.sh > $RUNROOT/vc5-all.out 2>&1 < /dev/null`。

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
* `parse_env.py` 集中派生规则／场景／补丁路径、Campaign 前缀、lifecycle 目录和候选镜像仓库；Python 与 shell
  使用同一解析结果。来源目录、官方包、基线画像可按模板可选键覆盖；不可从路径推导制品摘要或账号。
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

## 修好接着跑一条命令（第 35 项，`driver/fix-and-continue.sh`）

* 用途：候选采集（VC-5）续跑的一轮"修复 → 部署 → 登记 → 对账 → 批准 → 重派"由一条命令编排，取代按 sed 复制改写的
  upload-rN／repair-rN 轮次脚本。每轮只换一份轮次参数文件（模板 `driver/fix-and-continue.example.params`，经
  `parse_env.py` 同一词法层安全解析、与 `$ARM64_VC_ENV` 交叉核对）＋本机生成的期望摘要 JSON（EXPECT：部署收据的整树／
  五摘要／监督器、数据根 wire 闭包、守护基线与差异）与入口断言 JSON（ENTRY_GREPS）。
* 部署前提：本机全量门禁通过即可部署续跑，推送后 CI 与续跑并行、不等待（脚本里没有等 CI 的步骤）；CI 失败时暂停续跑，
  按该轮部署收据的 `rollback_backup` 回滚已部署工具（同样走受监督部署），修好后再走一轮；发版前仍须 CI 全绿（见指南
  “修好接着跑”一节）。
* 用法（采集主机 root）：`setsid -f bash /root/arm64-capture-driver/driver/fix-and-continue.sh <参数文件> > <日志> 2>&1 < /dev/null`；
  `--from <步骤>` 续跑（前序步骤在本轮必须有 passed／skipped 记录），`--list` 查看本轮各步骤记录。
* 步骤：deploy → postdeploy → item-tests → evolution → pre-extend → reconcile-runs → reconcile-attempt → repair → approve →
  authorize → extend → accepted → recover；每步幂等（同 HEAD 且收据一致则不重部署、实测已过不重跑、无漂移不登记、
  链尾父 run 已对账不重复、同一修复提交已登记不重复、本轮已启动 vc5-recover 不重复派发），输出写
  `$RUNROOT/fix-and-continue/<轮次>/<步骤>.json`，受管命令原始输出在同目录 `raw/`。
* 停下即退出并打印"下一步"与 `--from` 续跑命令：失败 1，需要人工 4（账务暂停、环境污染、永久停线、需审核、请求预算、
  需要人给出估计上界或证据文件、wire 闭包变化、守护代码变化），驱动被本轮重装更新 5（用新驱动 `--from item-tests`）。
  本脚本从不调用 accounting-resolve／environment-isolate／campaign-resume／request-budget-extend，从不传 `--force`，不重装守护。
* 阶段延期在对账前（pre-extend）与授权后（extend）各判一次：对账前阶段截止已过时计时账本是 deadline_paused、对账判预算
  暂停且拒绝批准；延期写入又会推进 Campaign 账本 head，放在批准与授权之间会让授权拒绝"账本 head 已推进"。
* 根因修复登记（第 59 项）：reconcile-runs 与 reconcile-attempt 对账遇"项目总账根因达上限"暂停，走同一条登记路径——
  shell 函数 `root_cause_repair`（同一判定 repair-plan、同一命令 record-root-cause-repair、同一核对 repair-verdict）。
  reconcile-attempt 照旧记 passed（needs_repair）交 repair 步骤登记、approve 重新对账；reconcile-runs 在 repair 之前，
  参数给了材料就在本步骤内登记、对该对象重新对账一次（仍暂停即停下），没给材料停下时提示 `--from reconcile-runs`。
  受管对账器先写对账收据、入总账再判定，暂停对象的收据按监督器判据核验是通过的：停下记录带 `revisit`，续跑扫描时
  重新对账这些对象，不当作已对账跳过。Campaign 账本 stop_required 不走此路径，只停下等人工 campaign-resume。
* 续跑重新对账（第 62 项）：`revisit` 覆盖 reconcile-runs 里任何暂停种类（deadline、accounting、environment、request_budget、
  root_cause_repair 与受管将来新增的种类），重新对账判可恢复即记 done、仍暂停按原提示停下。暂停提示末尾恰好一个
  `--from`，与停下记录的续跑步骤相同：含 deadline 时是 `pre-extend`（在参数文件填 EXTEND_DEADLINE／EXTEND_REASON，
  pre-extend 先于对账执行阶段延期），其余种类按受管提示处理后从停下的步骤续跑。reconcile-attempt／approve 每次都对
  目标 attempt 重新对账，不存在"暂停后被跳过"；它们的 deadline 暂停同样从 pre-extend 续跑。永久停线、需审核与命令
  失败不进 revisit，行为不变。
