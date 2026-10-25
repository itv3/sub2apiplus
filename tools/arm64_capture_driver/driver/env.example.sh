# 每轮参数模板：复制后填写 ARM64_VC_ENV；parse_env.py 只解析赋值，不执行文件。
# 只允许必填／可选键及对前面已定义键的引用。REPLACE_* 必须替换，模板原样执行会拒绝。
# 下面版本仅演示目录关系，实际版本、模型、账号、摘要、认证与来源由本轮批准输入确定。
# 客户端（Claude 升级 B5a-06）：CLIENT=codex（缺省，可省略）或 claude，决定必填键、派生坐标与驱动调用的模块（driver/client_table.py）。
# 下面是 Codex 的写法；Claude 的写法见文件末尾注释，凡是取值里带 codex 的键都按那里替换。
CLIENT=codex
ROUND=round1
STAMP=YYYYMMDDtHHMMSSz
BASELINE_VERSION=9.0.0
TARGET_VERSION=9.1.0
TARGET_PROFILE_ID=codex-$TARGET_VERSION-official
D=/root/docker/capture-cli/data
RUNROOT=/root/vc-rounds/$ROUND
NEW=codex-$TARGET_VERSION-formal-$ROUND-$STAMP
IN=codex-$TARGET_VERSION-inputs-$ROUND-$STAMP
UP=codex-$BASELINE_VERSION-to-$TARGET_VERSION-$ROUND-$STAMP
CAND=codex-$TARGET_VERSION-candidate-$ROUND
# B 自动派生为 $D/candidates/$CAND。旧文件可保留同值断言，错绑即拒绝。
PREV_CANDIDATE=$D/candidates/REPLACE_PREVIOUS_CANDIDATE
HISTORY_TEST_TREE=$D/candidates/REPLACE_HISTORY_CANDIDATE/test-tree
# C 是候选提交，DC 是其冻结承接提交；不得使用缩写。
C=0000000000000000000000000000000000000000
DC=0000000000000000000000000000000000000000
RECEIPT=docs/egress/maintenance/REPLACE_FREEZE_SUCCESSOR.json
BUNDLE=$D/staging/$ROUND.bundle
BUNDLE_BRANCH=codex/REPLACE_CANDIDATE_BRANCH
# 新目标首次取证必须 recapture。reuse 仅限同一目标的可信官方证据恢复。
EVIDENCE_DECISION=recapture
PREDECESSOR_CAMPAIGN=$D/evidence/campaigns/REPLACE_PREDECESSOR
OFFICIAL_CAMPAIGN=$PREDECESSOR_CAMPAIGN
OFFICIAL_STOP_LEDGER=$D/control/REPLACE_PREDECESSOR-timing-ledger
OFFICIAL_STOP_RECEIPT=receipts/REPLACE_STOP_RECEIPT.json
INPUT_RULE_MIGRATION=$D/control/$IN/rule-migration-approved.json
INPUT_TARGET_SNAPSHOT=$D/control/$IN/target-snapshot-approved.json
PROJECT_DEADLINE_UTC=REPLACE_APPROVED_DEADLINE_UTC
# R20：阶段预算按“顺利路径估算上限×1.5+30 分钟、向上取整到 15 分钟”标定，只经本参数生效（绑定项目总账后各阶段上限是到截止的总分钟数）；按本轮实测再调。
# VC-0 接入目标平台门禁预跑（vc0-gate-target.sh）后，顺利路径上限由 15 分钟调为 90 分钟（其余步骤约 12 分钟＋2026-09-29 首次预跑实测 69 分钟），预算 60→165。
# E2-04 起预跑记录取自建账本之前的入口门禁（entry-gates.sh），建账本之后只剩分钟级步骤；预算仍为 165，留作 VC-0 内多轮修复的余量（方案 D8）。
STAGE_BUDGETS="VC-0=165 VC-1=120 VC-2=210 VC-3=75 VC-4=165 VC-5=210 VC-6=165"
MIN_FREE_GIB=40
FRONTEND_DEVIATION_APPROVED_BY="REPLACE_本轮批准人日期和ARM64隔离候选适用范围"
PROFILE_ID=$TARGET_PROFILE_ID
PROFILE_DIGEST=REPLACE_APPROVED_PROFILE_SHA256
# 目标客户端的二进制、摘要与账号用通用键 CLIENT_BIN／CLIENT_BIN_SHA256／CLIENT_ACCOUNT_ID。Codex 的旧键 CODEX_BIN／
# CODEX_BIN_SHA256／CODEX_ACCOUNT_ID 仍可用（通用键是它们的别名，二选一），解析后按旧键导出；Claude 只认通用键。
CLIENT_BIN=/opt/codex-$TARGET_VERSION/bin/codex
CLIENT_BIN_SHA256=REPLACE_OFFICIAL_CODEX_SHA256
OFFICIAL_ASSET_SHA256=REPLACE_OFFICIAL_PACKAGE_SHA256
MAIN_MODEL=REPLACE_MAIN_MODEL
LITE_MODEL=REPLACE_LITE_MODEL
CLIENT_ACCOUNT_ID=REPLACE_CODEX_ACCOUNT_ID
API_KEY_ID=REPLACE_API_KEY_ID
POLICY_COMPAT_RECEIPT=$D/control/policy-certification/REPLACE_COMPATIBILITY.json
POLICY_ACTIVATION=$D/control/policy-certification/REPLACE_ACTIVATION.json
RELEASE_CERTIFICATION=$D/control/policy-certification/REPLACE_RELEASE.json
# Kilo 三项只有 Codex 需要（Codex 画像的第三方客户端用例），Claude 参数文件里不写。
KILO_BIN=$D/private-tools/REPLACE_KILO/bin/kilo
KILO_VERSION=REPLACE_KILO_VERSION
KILO_SHA256=REPLACE_KILO_SHA256
COMPOSE_DIR=/root/docker/sub2apiplus/app
COMPOSE_BACKUP=/root/backups/REPLACE_COMPOSE_BACKUP.yml
PRODUCTION_IMAGE=REPLACE_VERIFIED_PRODUCTION_IMAGE
# 可选键：来源路径可覆盖默认布局；以下不可推导身份在对应入口使用前必须填好。
# stage1 必须提供官方包中 code-mode-host 的摘要与固定 digest 的采集镜像。
# TARGET_CODE_MODE_HOST_SHA256=REPLACE_CODE_MODE_HOST_SHA256
# CAPTURE_RUNTIME_IMAGE=REPLACE_PINNED_CAPTURE_IMAGE
# BASELINE_SOURCE=$D/official/codex-$BASELINE_VERSION/source-rust-v$BASELINE_VERSION/codex-rs
# TARGET_SOURCE=$D/official/codex-$TARGET_VERSION/source-rust-v$TARGET_VERSION/codex-rs
# TARGET_PACKAGE=$D/official/codex-$TARGET_VERSION/assets/codex-package-aarch64-unknown-linux-musl.tar.gz
# ACTIVE_PROFILE=$D/control/$IN/baseline-profile.json
# PREVIOUS_POLICY=$D/control/policy-certification/REPLACE_PREVIOUS_POLICY.json
# 默认 $D/control/policy-certification/pre-a3-path-certification-$STAMP.json，由 pre-a3.sh 生成或复用（stage1 前）。
# PRE_A3_CERTIFICATION=$D/control/policy-certification/REPLACE_PRE_A3.json
# 本机候选提交链要求本轮完整、已绑定 VC-3 需求的映射；canonical 要求明确退役版本。
# GATE_MAPPING_INPUT=/absolute/local/path/gate-mapping.json
# RETIRE_VERSION=8.0.0
# HISTORICAL_SOURCE_ROOT=/absolute/path/to/historical/gate/source
# 入口编排器 entry.sh（E2-06）：入口门禁的源码坐标（部署到数据根的那一份工具提交）；产物根缺省是数据根，只在验收演练时
# 设成数据根 staging 下的独立目录（配演练总账，生产项目总账与正式坐标不写）。
# ENTRY_BUNDLE=$RUNROOT/bundles/REPLACE_TOOL_BUNDLE.bundle
# ENTRY_BRANCH=codex/REPLACE_TOOL_BRANCH
# ENTRY_COMMIT=REPLACE_TOOL_COMMIT_40_HEX
# ENTRY_ROOT=$D/staging/REPLACE_REHEARSAL_ROOT
# P0 收据（entry.sh 的固定步骤，E2-07）的回退依据：上一版本可回退点的收据文件，例如前序 Campaign 的画像目录晋升收据。
# 走入口（entry.sh、入口空跑）时必填：入口便宜检查的 required-parameters 一项核对它是已有的普通文件。
# P0_ROLLBACK_EVIDENCE=$RUNROOT/REPLACE_ROLLBACK_RECEIPT.json
# JWTGEN_BIN=$D/private-tools/jwtgen
# VC-4 默认使用 Docker 默认网络；R15 阻断时先获取绑定本轮 bundle／提交／主机／驱动的专项批准，再明确选择 host。
# VC4_BUILD_NETWORK=host
# VC4_BUILD_NETWORK_APPROVAL=$RUNROOT/REPLACE_BUILD_NETWORK_APPROVAL.json
# Claude 的写法（CLIENT=claude；设计 D3／B5a-07：目标客户端装在宿主机 /opt/claude-<版本>/claude，采集容器内同路径）：
#   CLIENT=claude
#   TARGET_PROFILE_ID=claude-code-$TARGET_VERSION-official
#   NEW=claude-code-$TARGET_VERSION-formal-$ROUND-$STAMP
#   IN=claude-code-$TARGET_VERSION-inputs-$ROUND-$STAMP
#   UP=claude-code-$BASELINE_VERSION-to-$TARGET_VERSION-$ROUND-$STAMP
#   CAND=claude-code-$TARGET_VERSION-candidate-$ROUND
#   BUNDLE_BRANCH=claude/REPLACE_CANDIDATE_BRANCH
#   CLIENT_BIN=/opt/claude-$TARGET_VERSION/claude
#   CLIENT_BIN_SHA256=REPLACE_OFFICIAL_CLAUDE_SHA256
#   CLIENT_ACCOUNT_ID=REPLACE_CLAUDE_ACCOUNT_ID
#   不写 KILO_BIN、KILO_VERSION、KILO_SHA256 与 TARGET_CODE_MODE_HOST_SHA256（Codex 专属）。
#   官方来源是 B2-01 冻结目录（claude_upgrade_freeze --output-root），缺省布局：
#   BASELINE_SOURCE=$D/official/claude-code-$BASELINE_VERSION
#   TARGET_SOURCE=$D/official/claude-code-$TARGET_VERSION
#   TARGET_PACKAGE=$D/official/claude-code-$TARGET_VERSION/freeze.json（OFFICIAL_ASSET_SHA256 是它的 SHA-256）
#   入口便宜检查的 official-package 一项按 B2-01 冻结合同复核目标版本的冻结目录（B5a-09），基线版本的冻结目录不核对。
#   UPSTREAM_IP=REPLACE_API_ANTHROPIC_COM_IP（api.anthropic.com 的冻结公开上游 IP：场景清单的必需 Campaign 变量 upstream_ip，
#   入口便宜检查的 plan-audit 与之后的 plan 都用它；不写时 required-parameters 一项报缺）
#   预检 plan 建 Campaign 的输入（B2-11 第二部分；不写时 required-parameters 一项报缺，前序复用两项只在证据决定为 reuse 时
#   由预检 plan 一步报缺）。Active 画像沿用上面 Codex 同一个缺省布局；来源提交取 ENTRY_COMMIT（生产工具树不是 git 仓库）：
#   ACTIVE_RELEASE_CATALOG=$D/control/$IN/active-release-catalog.json（production_active 的 Release 目录，冻结进 Campaign）
#   STATIC_BASELINE_ANCHORS=$D/control/$IN/static-baseline-anchors.json（VC-1 静态分析的基线词法锚点）
#   STATIC_BASELINE_REACHABILITY=$D/control/$IN/static-baseline-reachability.json（VC-1 静态分析的基线词法索引）
#   PRODUCTION_INVENTORY_STORE=$D/evidence/campaigns/REPLACE_ACTIVE_CAMPAIGN/control-store（生产现状清单取它最新的画像批准）
#   PRODUCTION_INVENTORY_CAMPAIGN=REPLACE_ACTIVE_CAMPAIGN
#   PREDECESSOR_STORE=$D/evidence/campaigns/REPLACE_PREDECESSOR_CAMPAIGN/control-store（reuse：只读复用同一目标版本前序的官方证据）
#   PREDECESSOR_STORE_CAMPAIGN=REPLACE_PREDECESSOR_CAMPAIGN
#   ENTRYPOINT=REPLACE_STORE_ENTRYPOINT（Store 身份：入口、隐私模式、OAuth 组织与模型目录摘要）
#   PRIVACY_MODE=REPLACE_STORE_PRIVACY_MODE
#   OAUTH_ORG_ID=REPLACE_OAUTH_ORG_ID
#   MODEL_CATALOG_SHA256=REPLACE_MODEL_CATALOG_SHA256
#   ENTRY_BRANCH=claude/REPLACE_TOOL_BRANCH
#   CLAUDE_TARGET_SCENARIOS_JSON=/absolute/private-inputs/current-scenarios.json（B5a-09／B2-11：可选，须为现存普通文件且
#   路径无符号链接。只覆盖 Claude 目标清单；缺省仍用受管版本模板，基线清单不变。plan 仍严格核规则、规格和版本事实，
#   入口便宜检查与预检 plan 绑定该文件全文。回归夹具只供无账号演练，不授正式采纳或现场认证。）
#   CAMPAIGN_PURPOSE=validation_only（B5b-01：Campaign 用途，缺省 production_replacement；入口建计时账本、预检 plan 与之后各阶段
#   都按它，VC-4 构建与 VC-5 准入从冻结的总计划读取。Codex 的入口固定 production_replacement，不得写这一项）
#   API_KEY_ID 与 CLIENT_ACCOUNT_ID 是候选阶段专用的 ingress API Key 与验收账号（B4-01），预检 plan 冻结进 Campaign 配置，
#   VC-0 收口按它恢复 Formal plan 参数。
#   VC-5 边界核对 codex-final-wire-isolation 的轮次参数（B5b-01b 续，Claude 专属可选键；Codex 参数文件不得写）：本轮 Codex
#   专用 OpenAI 账号与它所在单账号分组的网关 API Key（正整数 ID）、Codex 主轨模型，以及当前生产部署的源码提交（40 位，
#   必须在候选源码仓里；由操作员按实际部署填写）。可缺省，VC-5 启动预检与 seal 链派发边界核对之前报缺：
#   BOUNDARY_CODEX_ACCOUNT_ID=REPLACE_CODEX_ACCOUNT_ID
#   BOUNDARY_CODEX_API_KEY_ID=REPLACE_CODEX_API_KEY_ID
#   BOUNDARY_CODEX_MODEL=REPLACE_CODEX_MAIN_MODEL
#   PRODUCTION_COMMIT=REPLACE_PRODUCTION_COMMIT_40_HEX
# 开工检查补的两项外部事实检查（优化第 14 项，entry-preflight.sh 的 model-availability 与 main-security-scan）所用的变量是调用环境变量，
# 不是参数文件的键（parse_env.py 不接受它们当赋值）：在启动入口的 shell 里导出，或写进启动命令前缀。没配置也算未通过（fail-closed）。
# - MODEL_PROBE_URL：中转基地址（含 /v1），向它拉 /models，本轮的 MAIN_MODEL 与 LITE_MODEL 必须都在列表里（模型被下线当场拦下）
# - MODEL_PROBE_KEY_FILE：只含密钥的普通文件，属主自己、权限 0600；密钥不进命令行、不写日志
# - MODEL_PROBE_MINIMAL：可选，非空即开，另发一个最小请求确认模型真的能用
# - SECURITY_SCAN_REPO：owner/name；main 当前头对应的 Security Scan 必须已跑完、success、不超过 36 小时（要有登录好的 gh；本仓库的 gh 默认会查到上游，必须显式给）
# - OPENING_CHECKS_SKIP：确需跳过时列出要跳过的项目名（model-availability、main-security-scan），逗号分隔
# - OPENING_CHECKS_SKIP_REASON：跳过的理由，必填；列了跳过却没写理由，拒绝跳过、这一项按未通过处理
# 被跳过的项在 results.txt 与 summary.json 里记为 skipped（带理由），不是 passed；入口编排器不把 skipped 当失败，但汇总里一眼看得出。
