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
2. 采集主机：`ARM64_VC_ENV=$RUNROOT/env.sh bash driver/stage1.sh` 完成预检与演练。新目标首次取证按指南
   `codex_upgrade_vc0_closeout` 完成 Formal VC-0／VC-1 后进入 `vc23.sh`；同目标恢复才用 `pre-all.sh`（stage2 + vc23）。
3. 本机：候选提交链（A／C／D 三段，见 `driver/local/`）→ `git bundle` 推到 `$BUNDLE`。
4. 采集主机：`setsid -f bash driver/vc4-all.sh > $RUNROOT/vc4-all.out 2>&1 < /dev/null`；本机同时跑
   `driver/local/local-gate.sh` 与 `local-full-regression.sh`，产物由 `local-upload.sh` 上传到 `$RUNROOT/`。
5. 采集主机：`setsid -f bash driver/vc5-all.sh > $RUNROOT/vc5-all.out 2>&1 < /dev/null`。

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

* VC-4 的前端和门禁等待均监视实际子进程；任一死亡或超过阶段／项目时限，打印相关日志尾 200 行并退出 3。
  上传方每 30 秒更新 `impl-logs/HEARTBEAT`；等待方在 5 分钟无心跳或总时限到期时退出 3。心跳只判断存活，
  完成必须有 `READY`，并逐文件核验上传清单及日志中的候选／承接提交。
* 上传中断后执行 `bash vc4-all.sh --resume-from upload-wait`。工具先核验 `E.txt` 指向的 `upload-wait.json`：
  四树 HEAD、实际源码摘要、架构、affected 闭集、go.sum／vendor、Go／Node、基础镜像 digest、构建参数、本轮批准门禁的成功
  日志与镜像／二进制／dist／context 产物。完整实现测试收据尚未生成时不要求它存在；上传后生成并正式 replay。
  已有完整收据时仍按同一输入合同检查；普通重起只有完整收据及这些绑定全部通过才跳过四树、门禁和构建。
  旧记录缺少中间凭证时重新执行，不自动追认旧日志。驱动输入或产物变化时必须重做受影响阶段。
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
