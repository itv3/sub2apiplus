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
repo_root=${REPO_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}
tool_root=${TOOL_ROOT:-"$repo_root/tools/official_client_capture"}

case "$side" in
  official) attempt_dir="$campaign_dir/official/attempts/$attempt_id" ;;
  candidate)
    candidate_id=${CANDIDATE_ID:?候选侧必须提供 CANDIDATE_ID}
    attempt_dir="$campaign_dir/candidates/$candidate_id/attempts/$attempt_id" ;;
  *) echo "未知 SIDE: $side" >&2; exit 2 ;;
esac

attempt_json="$attempt_dir/attempt.json"
[[ -f $attempt_json ]] || { echo "找不到 attempt: $attempt_json" >&2; exit 1; }
campaign_json="$campaign_dir/campaign.json"
[[ -f $campaign_json ]] || { echo "找不到 Campaign: $campaign_json" >&2; exit 1; }

# 声明必须与 Campaign 目标版本逐字绑定。不存在对应版本声明时失败关闭，绝不回退到
# 旧版本；即使调用方显式传入 DECLARATION，编目器也会再次校验 codex_version。
target_version=$(python3 - "$campaign_json" <<'PY'
import json, pathlib, re, sys
campaign = json.loads(pathlib.Path(sys.argv[1]).read_text())
version = campaign.get("target_version")
if not isinstance(version, str) or not re.fullmatch(r"\d+\.\d+\.\d+", version):
    raise SystemExit("Campaign target_version 非法")
print(version)
PY
)
version_key=${target_version//./_}
declaration=${DECLARATION:-"$tool_root/codex_upgrade_evidence_labels_${version_key}.json"}
[[ -f $declaration && ! -L $declaration ]] || {
  echo "找不到目标版本 $target_version 的证据标签声明：$declaration" >&2
  exit 1
}

bundle_dir="$attempt_dir/evidence/assertion-bundle"
[[ -e $bundle_dir ]] && { echo "断言证据包已存在，拒绝覆盖：$bundle_dir" >&2; exit 1; }
work_dir=$(mktemp -d)
trap 'rm -rf "$work_dir"' EXIT

# 1) 由 campaign.json 的 job 定义与 attempt 已绑定的证据根，推出 job→(前缀, 路径)
python3 - "$campaign_dir" "$attempt_dir" "$side" > "$work_dir/jobroots.txt" <<'PY'
import fnmatch, json, sys, pathlib
campaign = json.loads((pathlib.Path(sys.argv[1]) / "campaign.json").read_text())
attempt = json.loads((pathlib.Path(sys.argv[2]) / "attempt.json").read_text())
side = sys.argv[3]
bound = {str(pathlib.Path(r).resolve()): pathlib.Path(r) for r in attempt["evidence_roots"]}
result_by_job = {result["id"]: result for result in attempt["results"]}

# 候选侧的 run 目录名比 campaign.json 里的 job 定义多一段 candidate_id：plan 阶段还不
# 知道会由哪个候选来跑，模板只展开到 {campaign_id}，实际运行时插入的是
# `{campaign_id}-{candidate_id}-…`。官方侧没有这一段，直接等值匹配即可。
# 不做这层归一化，候选侧会一个证据根都匹配不上，脚本以「没有可编目的证据根」退出。
campaign_id = str(campaign["campaign_id"])
candidate_id = attempt.get("candidate_id") or ""


def suffix(name: str, *prefixes: str) -> str:
    for prefix in prefixes:
        head = f"{prefix}-"
        if name.startswith(head):
            name = name[len(head):]
    return name


by_suffix = {}
if side == "candidate" and candidate_id:
    for resolved, path in bound.items():
        by_suffix.setdefault(suffix(path.name, campaign_id, candidate_id), resolved)

for job in campaign["jobs"]:
    if job["phase"] != side:
        continue
    result = result_by_job.get(job["id"])
    if result is None:
        raise SystemExit(f'job {job["id"]} 缺少 attempt 结果，拒绝编目')
    if result.get("status") != "complete":
        # 可选轨失败不构成候选失败，也不能把其不完整目录伪装成证据根。必需轨若未完成，
        # 则 attempt 本身不具备密封资格，必须在这里继续失败关闭。
        if result.get("required", job.get("required", True)):
            raise SystemExit(f'必需 job {job["id"]} 未完成，拒绝编目')
        print(
            f'# skip optional non-complete job: {job["id"]} '
            f'status={result.get("status")}',
            file=sys.stderr,
        )
        continue
    for root in job["evidence_roots"]:
        p = pathlib.Path(root)
        resolved = str(p.resolve())
        if resolved in bound:
            print(f'{job["id"]}={p.name}={root}')
            continue
        # 候选侧的根名比 job 定义多一段 candidate_id；此外 mitm 系列的 job 定义本身就是
        # 带通配符的模式（…-candidate-mitm-core-*-run），一条模式对应多个实际根，
        # 必须按 fnmatch 展开，否则这两个 job 会整个从编目里消失——而且不会报错，
        # 因为「没匹配到根」和「该 job 没有证据」在下游看起来一样。
        wanted = suffix(p.name, campaign_id)
        matched = by_suffix.get(wanted)
        if matched:
            actual = bound[matched]
            print(f'{job["id"]}={actual.name}={matched}')
            continue
        if "*" in wanted or "?" in wanted:
            for cand_suffix, cand_resolved in sorted(by_suffix.items()):
                if not fnmatch.fnmatch(cand_suffix, wanted):
                    continue
                # mitm 系列的 setup 根只承载代理自身的启动日志，没有任何 wire 证据：
                # 登记为 opaque 会被断言器以「无派生引用」拒绝，不登记又会让编目报
                # 「该根没有适用规则」。它不是证据面，显式跳过并打印，不静默丢。
                if cand_suffix.endswith("-setup-run"):
                    print(f'# skip {job["id"]} setup-only root: {cand_suffix}', file=sys.stderr)
                    continue
                actual = bound[cand_resolved]
                print(f'{job["id"]}={actual.name}={cand_resolved}')
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
  --declaration "$declaration" --expected-codex-version "$target_version" \
  --side "$side" \
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
