#!/bin/bash
# VC-6 公共前导（被 vc6-*.sh 用 source 引入，不可直接执行；随驱动安装在 <驱动目录>/pipeline/vc6/vc6-env.sh）。
#
# 为什么有这个文件：早期的 VC-6 手工脚本把 Campaign／候选／提交号／画像摘要／回滚镜像号等全部写成字面量，
# 换一轮就要重改一遍，漏一处就是事故。现在一律从本轮参数文件或命令现取，不写死任何提交号、摘要、镜像号。取值来源：
#   * 本轮 env.sh 经驱动自带的 parse_env.py 安全解析（只接受精确键集合，不执行任何命令；与 lib.sh 同一解析器）；
#   * 回滚侧摘要取 P0_ROLLBACK_EVIDENCE（上一轮 VC-6 晋升收据副本）里的 target_profile_digest，并断言其 target_version
#     等于 BASELINE_VERSION；
#   * 回滚镜像取 PRODUCTION_IMAGE（VC-5 前现网 compose 的整行，含摘要）。
# 注意：parse_env 导出的 W 是“输入目录”（control/<IN>），本文件不改它；本套脚本里叫 W 的“VC-6 工作目录”在这里叫 V6，
# 需要沿用 W=$V6 写法的脚本在 source 之后自己赋值。
if [ -z "${BASH_SOURCE[1]:-}" ]; then echo "vc6-env.sh 只能被 source"; exit 2; fi
: "${ARM64_VC_ENV:?需要 ARM64_VC_ENV（本轮参数文件）}"
export ARM64_VC_ENV
PIPE_DRIVER_DIR=${PIPE_DRIVER_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}
export PYTHONDONTWRITEBYTECODE=1
VC6_DRV=${PIPE_DRIVER_DIR}
VC6_EXPORTS=$(python3 -B "${VC6_DRV}/parse_env.py" "${ARM64_VC_ENV}") || { echo "参数文件拒绝加载：${ARM64_VC_ENV}"; exit 2; }
eval "${VC6_EXPORTS}"; unset VC6_EXPORTS

# ---- 本轮坐标（全部来自 env.sh）
export V6=${RUNROOT}/vc6                      # VC-6 工作目录（状态文件、增量 bundle、阶段输出）；旧写法里叫 W
export TARGET_DIGEST=${PROFILE_DIGEST}        # 目标画像摘要：VC-3 后由流水线写进 env.sh 的 PROFILE_DIGEST
export ROLLBACK_VERSION=${BASELINE_VERSION}   # 回滚版本＝本轮基线（上一轮的目标）

# ---- 起跑断言：占位值一律拒绝（VC-3 之后、VC-4 之后才是真值）
case "${TARGET_DIGEST}" in
  *[!0-9a-f]*|"") echo "vc6-env: PROFILE_DIGEST 不是十六进制摘要：${TARGET_DIGEST}"; exit 2 ;;
esac
[ "${#TARGET_DIGEST}" -eq 64 ] || { echo "vc6-env: PROFILE_DIGEST 长度不是 64"; exit 2; }
[ "${TARGET_DIGEST}" != "$(printf '%064d' 0)" ] \
  || { echo "vc6-env: PROFILE_DIGEST 仍是占位全 0（VC-3 之后才有真值）"; exit 2; }
[ "${C}" != "${ENTRY_COMMIT}" ] && [ "${C}" != "0000000000000000000000000000000000000000" ] \
  || { echo "vc6-env: env 的 C 仍是占位（应为 VC-4 候选链的 C）"; exit 2; }
[ -n "${RETIRE_VERSION:-}" ] || { echo "vc6-env: env 缺 RETIRE_VERSION"; exit 2; }

# ---- 回滚侧摘要：取回退依据收据（上一轮 VC-6 晋升收据副本）的目标画像摘要，并断言它确实是本轮基线版本
ROLLBACK_DIGEST=$(python3 -B -c "
import json, sys
r = json.load(open(sys.argv[1]))
assert r['target_version'] == sys.argv[2], ('回退依据收据的目标版本与 BASELINE_VERSION 不一致', r['target_version'], sys.argv[2])
d = r['target_profile_digest']
assert len(d) == 64 and all(c in '0123456789abcdef' for c in d), d
print(d)" "${P0_ROLLBACK_EVIDENCE}" "${BASELINE_VERSION}") || { echo "vc6-env: 读不出回滚画像摘要（${P0_ROLLBACK_EVIDENCE}）"; exit 2; }
export ROLLBACK_DIGEST

# ---- 回滚镜像：VC-5 前现网 compose 的生产镜像整行（PRODUCTION_IMAGE，含 @sha256），例如 ghcr.io/<组织>/<镜像>:<标签>@sha256:…
# 现网容器的 .Image 就等于这个摘要（ARM64 实测 .Image=sha256:<该摘要>），所以回滚镜像 ID 直接取摘要部分。
case "${PRODUCTION_IMAGE}" in *@sha256:*) ;; *) echo "vc6-env: PRODUCTION_IMAGE 不含 @sha256 摘要：${PRODUCTION_IMAGE}"; exit 2 ;; esac
export ROLLBACK_IMAGE_ID=sha256:${PRODUCTION_IMAGE##*@sha256:}
VC6_REPO_TAG=${PRODUCTION_IMAGE%%@*}                                   # ghcr.io/<组织>/<镜像>:<标签>
export ROLLBACK_REF=${VC6_REPO_TAG%:*}@${ROLLBACK_IMAGE_ID}            # ghcr.io/itv3/sub2apiplus@sha256:…（不带标签）
export ROLLBACK_BUILD_ID=$(printf '%s' "${VC6_REPO_TAG}" | sed -E 's#^ghcr\.io/#ghcr-#; s#[/:]#-#g')   # ghcr-<组织>-<镜像>-<标签>
unset VC6_REPO_TAG

# ---- 切换镜像与激活事实的命名（按 CAMPAIGN_PREFIX 区分轮次）
export PROD_IMAGE_REPO=sub2apiplus-${CAMPAIGN_PREFIX}-production      # 本机构建的切换镜像仓库名
export ACT_FACT_NAME=${CAMPAIGN_PREFIX}-production-activation-fact.json
# 业务验证模型：缺省取 env 的 LITE_MODEL（gpt-6-astra，经 /v1/chat/completions 实证最多），可用环境变量 BUSINESS_MODEL 覆盖
export BUSINESS_MODEL=${BUSINESS_MODEL:-${LITE_MODEL}}
