#!/bin/bash
# Claude 候选网关切换（Claude 升级 B5b-01b）：从 compose 备份改写 docker-compose.yml——生产镜像行换成候选标签，环境变量块写入
#   Claude 候选开关 GATEWAY_CLAUDE_FW_G_CANDIDATE_ENABLED、登记的隔离实例 GATEWAY_OFFICIAL_EGRESS_GUARD_INSTANCE_ID=ARM64（Go 只在
#   登记的隔离候选实例放行候选开关）与激活事实那组变量（声明身份取 VC-4 构建收据，不声明批准摘要）；起服务后核对激活事实是
#   候选 Runtime 且与构建收据逐项一致。恢复生产镜像两客户端通用：restore 交给 vc5-switch.sh（拷回 compose 备份，以上变量随之撤回）。
# 用法：bash vc5-claude-switch.sh candidate <image_tag> | bash vc5-claude-switch.sh restore
set -Eeuo pipefail
if [ "${1:-}" = restore ]; then exec bash "$(dirname "${BASH_SOURCE[0]}")/vc5-switch.sh" restore; fi
PHASE_CONTEXT_ARGS=(--mode build)
CLIENT_STEP_REQUIRED=(CT_VC5_START_SCRIPT vc5-claude-start.sh)
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
if [ "${1:-}" != candidate ] || [ -z "${2:-}" ]; then echo "用法：bash vc5-claude-switch.sh candidate <image_tag> | restore"; exit 2; fi
TAG="$2"
vc5_require_admission
cd "$COMPOSE_DIR"
python3 "$DRV/claude_vc5.py" switch-compose --tag "$TAG"
docker compose config --quiet
python3 "$TOOLS/codex_upgrade_supervisor.py" egress-transition --container sub2apiplus --compose-service sub2api \
  -- docker compose -f "$COMPOSE_DIR/docker-compose.yml" up -d sub2api
st=unknown; for i in $(seq 1 36); do st=$(docker inspect sub2apiplus --format "{{.State.Health.Status}}"); [ "$st" = healthy ] && break; sleep 5; done
echo "health=$st image=$(docker inspect sub2apiplus --format '{{.Config.Image}}') image_id=$(docker inspect sub2apiplus --format '{{.Image}}')"
[ "$st" = healthy ]
python3 "$DRV/claude_vc5.py" activation-check --fact "$COMPOSE_DIR/data/$CAND-claude-activation-fact.json"
