#!/usr/bin/env bash
# 在 seal 之前把一侧 attempt 的断言证据包准备好：编目 → 收口 → 派生 → 回填 manifest。
#
# ACC-06 的落位约束：bundle 必须是**已绑定证据根内**名为 `assertion-bundle` 的子目录，
# 这里固定放在 attempt 的环境证据根下（`<attempt>/evidence/assertion-bundle/`）。
# seal 的 `_capture_assertion_context` 据此定位，并把 `<根前缀>/assertion-bundle`
# 作为 inventory 逻辑前缀。
#
# 用法：
#   CAMPAIGN_DIR=... ATTEMPT_ID=... SIDE=official|candidate \
#   bash prepare_assertion_bundle.sh
set -euo pipefail

campaign_dir=${CAMPAIGN_DIR:?必须提供 CAMPAIGN_DIR}
attempt_id=${ATTEMPT_ID:?必须提供 ATTEMPT_ID}
side=${SIDE:-official}
# ⚠ 本脚本刻意**不放在** tools/official_client_capture/ 下：那里的 .py/.sh/.json
# 参与 Campaign 的工具身份摘要，新增文件会让已建 Campaign 的 seal 以「工具漂移」
# 拒绝继续。本脚本只编排受管工具、不产生新的证据语义，故置于其外。
repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
tool_root=${TOOL_ROOT:-"$repo_root/tools/official_client_capture"}
declaration=${DECLARATION:-"$tool_root/codex_upgrade_evidence_labels_0_145_0.json"}

case "$side" in
  official) attempt_dir="$campaign_dir/official/attempts/$attempt_id" ;;
  candidate)
    candidate_id=${CANDIDATE_ID:?候选侧必须提供 CANDIDATE_ID}
    attempt_dir="$campaign_dir/candidates/$candidate_id/attempts/$attempt_id" ;;
  *) echo "未知 SIDE: $side" >&2; exit 2 ;;
esac

attempt_json="$attempt_dir/attempt.json"
[[ -f $attempt_json ]] || { echo "找不到 attempt: $attempt_json" >&2; exit 1; }

bundle_dir="$attempt_dir/evidence/assertion-bundle"
[[ -e $bundle_dir ]] && { echo "断言证据包已存在，拒绝覆盖：$bundle_dir" >&2; exit 1; }
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

# 1) 由 campaign.json 的 job 定义与 attempt 已绑定的证据根，推出 job→(前缀, 路径)
python3 - "$campaign_dir" "$attempt_dir" "$side" > "$work_dir/jobroots.txt" <<'PY'
import json, sys, pathlib
campaign = json.loads((pathlib.Path(sys.argv[1]) / "campaign.json").read_text())
attempt = json.loads((pathlib.Path(sys.argv[2]) / "attempt.json").read_text())
side = sys.argv[3]
bound = {str(pathlib.Path(r).resolve()) for r in attempt["evidence_roots"]}
for job in campaign["jobs"]:
    if job["phase"] != side:
        continue
    for root in job["evidence_roots"]:
        p = pathlib.Path(root)
        if str(p.resolve()) in bound:
            print(f'{job["id"]}={p.name}={root}')
PY

catalog_args=()
bundle_args=()
while IFS= read -r line; do
  [[ -z $line ]] && continue
  catalog_args+=(--job-root "$line")
  bundle_args+=(--source-root "${line#*=}")
done < "$work_dir/jobroots.txt"
[[ ${#catalog_args[@]} -gt 0 ]] || { echo "没有可编目的证据根" >&2; exit 1; }

cd "$repo_root"
python3 "$tool_root/build_evidence_catalog.py" \
  --declaration "$declaration" --side "$side" \
  "${catalog_args[@]}" --output-dir "$work_dir/catalog"

python3 "$tool_root/build_assertion_bundle.py" \
  "${bundle_args[@]}" --plan "$work_dir/catalog/bundle-plan.json" \
  --bundle-dir "$bundle_dir"

if [[ -s $work_dir/catalog/derivation-plan.json ]] &&
   python3 -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))['entries'] else 1)" \
     "$work_dir/catalog/derivation-plan.json"; then
  python3 "$tool_root/derive_official_observations.py" \
    --bundle-dir "$bundle_dir" --plan "$work_dir/catalog/derivation-plan.json"
fi

# 2) 回填 sha256，产出可提交的 capture manifest
python3 - "$work_dir/catalog/manifest-draft.json" "$bundle_dir" "$campaign_dir" <<'PY'
import json, pathlib, sys
# 本段以 stdin 执行，无 __file__；cwd 已切到仓库根。
sys.path.insert(0, str(pathlib.Path("tools/official_client_capture").resolve()))
import build_evidence_catalog as catalog
draft = json.loads(pathlib.Path(sys.argv[1]).read_text())
bundle = pathlib.Path(sys.argv[2])
campaign = json.loads((pathlib.Path(sys.argv[3]) / "campaign.json").read_text())
manifest = catalog.finalize_manifest(
    draft, bundle,
    # manifest 的 codex_version 必须等于 seal 传给断言器的 expected 版本，
    # 即本轮目标版本；断言器会逐字校验。
    codex_version=campaign["target_version"],
    capture_id=campaign["campaign_id"],
)
path = bundle / "capture-manifest.json"
path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
path.chmod(0o600)
print(f"capture manifest 已写入：{len(manifest['artifacts'])} 个 artifact")
PY

echo "断言证据包就绪：$bundle_dir"
echo "接下来执行 capture-$side seal（--capture-manifest 可省略，seal 会在证据根内唯一发现）。"
