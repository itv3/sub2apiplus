# ARM64 抓包驱动链：每轮 Campaign 的参数文件模板（复制为 $RUNROOT/env.sh 后按本轮填写；各脚本以
# ARM64_VC_ENV=<该文件> 读取）。只允许出现变量赋值，不得执行命令；注释以 # 开头。
# 命名约定：ROUND 是本轮标签（如 v14r5），STAMP 是 stage1 开始时的 UTC 时间戳（%Y%m%dt%H%M%Sz）。
ROUND=v14r5
STAMP=20260922t000000z
# 采集主机数据根（受管工具树、Campaign、总账、候选实物都在其下）
D=/root/docker/capture-cli/data
# 本轮日志／状态根：所有脚本的 .out／状态文件、本机上传的门禁日志都落在这里，不再散落 /root
RUNROOT=/root/vc-rounds/$ROUND
# Campaign／输入目录／账本标识（与 ROUND、STAMP 绑定）
NEW=c0154-formal-vc5-$ROUND-$STAMP
IN=c0154-vc5-$ROUND-inputs-$STAMP
UP=codex-0151-to-0154-vc5-$ROUND-$STAMP
# 候选实物目录（candidate_id 与目录同名）
CAND=c0154-candidate-$ROUND
B=$D/candidates/$CAND
# 前序候选目录：vendor（go.mod/go.sum 逐字相同才复用）与 source 对象从这里取
PREV_CANDIDATE=$D/candidates/c0154-candidate-v14r4
# 完整历史的测试树来源（上游合并／历史漂移冻结测试要读基准提交）
HISTORY_TEST_TREE=$D/candidates/c0154-candidate-v14/test-tree
# 候选提交链：C 只改冻结路径的第二段提交，DC 是描述 A→C 的承接收据所在提交，RECEIPT 是收据相对路径
C=0000000000000000000000000000000000000000
DC=0000000000000000000000000000000000000000
RECEIPT=docs/egress/maintenance/upstream-codex-0154-candidate-$ROUND-YYYYMMDD-freeze-successor.json
# 本机推送的 git bundle 与分支
BUNDLE=$D/staging/$ROUND.bundle
BUNDLE_BRANCH=codex/vc5-framework-closure
# 官方证据 Campaign（reuse-official-evidence 的前序）与其停线账本／收据
OFFICIAL_CAMPAIGN=$D/evidence/campaigns/c0154-formal-vc1-recapture-20260915t230327z
OFFICIAL_STOP_LEDGER=$D/control/codex-0151-to-0154-bwg-recapture-vc0-20260915t230327z-timing-ledger
OFFICIAL_STOP_RECEIPT=receipts/stop-checkpoint-20260916t005113z.json
# VC-2 输入（规则迁移草案与目标快照输入；由老板批准的版本）
INPUT_RULE_MIGRATION=/root/v9-rule-migration.json
INPUT_TARGET_SNAPSHOT=/root/v9-target-snapshot-input.json
# 项目总账绝对截止（总预算按它设上限，留 5 分钟余量）与各阶段预算（分钟）
PROJECT_DEADLINE_UTC=2026-09-28T15:59:00Z
STAGE_BUDGETS="VC-0=45 VC-1=10 VC-2=30 VC-3=15 VC-4=90 VC-5=600 VC-6=60"
# 派发前磁盘检查的操作员阈值（GiB，固定最低 40，只能设更高）
MIN_FREE_GIB=40
# 前端 builder 偏差批准（record-candidate-build 严格合同要求的批准人／理由摘要）
FRONTEND_DEVIATION_APPROVED_BY="老板（YYYY-MM-DD 指令：…）"
# 目标画像（VC-3 stage-profile 的画像 id 与摘要，来自 classify 批准的 profile.json）
PROFILE_ID=codex-0.154.0-official-r154-v2
PROFILE_DIGEST=31d8654f6892d37129a2639f1bb48e87b7b8648d67ce754f4ae9379a671b99e3
# Kilo 不可变副本（0500，root）：路径／版本／sha256 三项 fail-fast
KILO_BIN=$D/private-tools/kilo-7.7.501-cdadeea1/bin/kilo
KILO_VERSION=7.7.501
KILO_SHA256=cdadeea18400a3a603753f2a15b7b6c34de3161a7a5a1a8255bc5f2da977a612
# 网关 compose 与切换前备份（固定 IP 版）
COMPOSE_DIR=/root/docker/sub2apiplus/app
COMPOSE_BACKUP=/root/backups/docker-compose.yml.pre-c0154-20260916
PRODUCTION_IMAGE=ghcr.io/itv3/sub2apiplus:0.2.4-3
