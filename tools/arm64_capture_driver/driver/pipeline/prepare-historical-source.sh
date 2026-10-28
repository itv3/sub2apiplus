#!/bin/bash
# 给门禁准备“目标版本历史门禁源码基线”（采集主机侧，<驱动目录>/pipeline/prepare-historical-source.sh）。
# 背景：集成分支的 Makefile 把 check-egress-spec-local-source 的源码版本换成目标版本（check_spec_refs 读 codex-rs/Cargo.toml 的 version 比对），
# 而数据根默认的 official/historical-gate-source 是旧版本，门禁会失败。做法：从官方目标版本源码树复制一份（去掉 .git），
# 放到 official/historical-gate-source-<TARGET_VERSION>，参数文件里 HISTORICAL_SOURCE_ROOT 指向它；旧目录原样保留（旧记录引用它）。
# 目标版本取本轮参数文件（TARGET_VERSION）；官方源码树位置缺省 $D/official/codex-<版本>/source-rust-v<版本>。
# 幂等：目标已存在则只核对。不删除任何文件。
set -Eeuo pipefail
source "$(dirname "${BASH_SOURCE[0]}")/pipeline-lib.sh"
pl_load_round_env || { echo "本轮参数文件解析失败：${ARM64_VC_ENV:-未设置}"; exit 2; }
SRC=${PIPE_OFFICIAL_SOURCE:-${D}/official/codex-${TARGET_VERSION}/source-rust-v${TARGET_VERSION}}
DST=${D}/official/historical-gate-source-${TARGET_VERSION}
if [ ! -e "${DST}" ]; then
  umask 022
  mkdir "${DST}"
  ( cd "${SRC}" && tar --exclude=./.git -cf - . ) | tar -xf - --no-same-owner -C "${DST}"
  chown -R root:root "${DST}"; chmod -R go-w "${DST}"
  echo "已复制到 ${DST}"
fi
VER=$(sed -n 's/^version *= *"\(.*\)"/\1/p' "${DST}/codex-rs/Cargo.toml" | head -1)
echo "源码树版本=${VER}（应为 ${TARGET_VERSION}）"; [ "${VER}" = "${TARGET_VERSION}" ]
diff -rq --exclude=.git "${SRC}" "${DST}" && echo "与官方 ${TARGET_VERSION} 源码树逐文件一致（除 .git）"
echo "不安全项=$(find "${DST}" ! -type l \( ! -user root -o -perm /022 \) | wc -l | tr -d ' ')（应 0）  .git 存在=$(ls -d "${DST}/.git" 2>/dev/null | wc -l | tr -d ' ')（应 0）  大小=$(du -sh "${DST}" | cut -f1)"
echo HISTORICAL_SOURCE_OK
