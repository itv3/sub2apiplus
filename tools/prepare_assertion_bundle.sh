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
# 改造 5 M2（attempt-recovery 基线的增量封存）：候选侧再给 BASELINE=b<K>（K≥1）时，
# 证据根改读 `candidates/<cid>/revisions/b<K>/effective-results.json`（每 Job 恰一条，
# reused 引用前序结果、recovered 指向恢复段结果），bundle 落在
# `candidates/<cid>/revisions/b<K>/baseline-evidence/assertion-bundle/`（本基线私有根；不叫 evidence，避免与恢复段证据根同名）。
set -euo pipefail
umask 077

campaign_dir=${CAMPAIGN_DIR:?必须提供 CAMPAIGN_DIR}
attempt_id=${ATTEMPT_ID:?必须提供 ATTEMPT_ID}
side=${SIDE:-official}
# ⚠ 本脚本刻意**不放在** tools/official_client_capture/ 下：那里的 .py/.sh/.json
# 参与 Campaign 的工具身份摘要，新增文件会让已建 Campaign 的 seal 以「工具漂移」
# 拒绝继续。本脚本只编排受管工具、不产生新的证据语义，故置于其外。
repo_root=${REPO_ROOT:-$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)}
tool_root=${TOOL_ROOT:-"$repo_root/tools/official_client_capture"}

baseline=${BASELINE:-}
case "$side" in
  official) attempt_dir="$campaign_dir/official/attempts/$attempt_id" ;;
  candidate)
    candidate_id=${CANDIDATE_ID:?候选侧必须提供 CANDIDATE_ID}
    attempt_dir="$campaign_dir/candidates/$candidate_id/attempts/$attempt_id" ;;
  *) echo "未知 SIDE: $side" >&2; exit 2 ;;
esac
if [[ -n $baseline ]]; then
  [[ $side == candidate ]] || { echo "BASELINE 只用于候选侧" >&2; exit 2; }
  [[ $baseline =~ ^b[1-9][0-9]*$ ]] || { echo "BASELINE 必须是 b<K>（K≥1）：$baseline" >&2; exit 2; }
  baseline_dir="$campaign_dir/candidates/$candidate_id/revisions/$baseline"
  results_json="$baseline_dir/effective-results.json"
  [[ -f $results_json && ! -L $results_json ]] || { echo "找不到 effective-results: $results_json" >&2; exit 1; }
  evidence_dir="$baseline_dir/baseline-evidence"
  if [[ ! -e $evidence_dir ]]; then mkdir -m 0700 "$evidence_dir"; fi
else
  results_json="$attempt_dir/attempt.json"
  evidence_dir="$attempt_dir/evidence"
fi

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

bundle_dir="$evidence_dir/assertion-bundle"
[[ -e $bundle_dir ]] && { echo "断言证据包已存在，拒绝覆盖：$bundle_dir" >&2; exit 1; }
[[ -d $evidence_dir && ! -L $evidence_dir ]] || {
  echo "attempt 证据目录不存在或不可信：$evidence_dir" >&2
  exit 1
}
# 暂存目录必须与最终 bundle 位于同一文件系统；全部步骤通过后再原子发布，
# 失败时不能留下一个看似可用的半成品 assertion-bundle。
work_dir=$(mktemp -d "$evidence_dir/.assertion-work.XXXXXX")
chmod 700 "$work_dir"
trap 'rm -rf "$work_dir"' EXIT
staged_bundle="$work_dir/assertion-bundle"

# 1) 唯一从逐 Job 结果读取权威证据根；顶层 evidence_roots 和 Campaign 名称
# 不能覆盖跨 Campaign 的复用根。BASELINE 模式下结果来自 effective-results（entries）。
python3 - "$campaign_dir" "$results_json" "$side" > "$work_dir/jobroots.txt" <<'PY'
import json, re, sys, pathlib
campaign = json.loads((pathlib.Path(sys.argv[1]) / "campaign.json").read_text())
source = json.loads(pathlib.Path(sys.argv[2]).read_text())
side = sys.argv[3]
if source.get("schema_version") == "codex-upgrade-effective-results/v1":
    results = [
        {"id": entry.get("job_id"), "status": entry.get("status"), "required": True, "evidence_roots": entry.get("evidence_roots")}
        for entry in source.get("entries", [])
        if isinstance(entry, dict)
    ]
else:
    results = source.get("results")
if not isinstance(results, list):
    raise SystemExit("attempt results 必须是数组")
result_by_job = {}
for result in results:
    if not isinstance(result, dict) or not isinstance(result.get("id"), str):
        raise SystemExit("attempt result 身份非法")
    if result["id"] in result_by_job:
        raise SystemExit(f'attempt result 重复：{result["id"]}')
    result_by_job[result["id"]] = result

root_name_re = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
seen_roots = {}
complete_jobs = set()

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
    roots = result.get("evidence_roots")
    if not isinstance(roots, list) or not roots:
        raise SystemExit(f'complete job {job["id"]} 缺少权威 evidence_roots')
    complete_jobs.add(job["id"])
    for raw_root in roots:
        if not isinstance(raw_root, str) or not raw_root.startswith("/"):
            raise SystemExit(f'job {job["id"]} 的 evidence_root 不是绝对路径')
        path = pathlib.Path(raw_root)
        if path.is_symlink() or not path.is_dir():
            raise SystemExit(f'job {job["id"]} 的 evidence_root 不存在或不可信：{raw_root}')
        resolved = str(path.resolve(strict=True))
        if resolved in seen_roots:
            raise SystemExit(
                f'权威 evidence_root 被多个结果重复声明：{resolved} '
                f'({seen_roots[resolved]}、{job["id"]})'
            )
        if not root_name_re.fullmatch(path.name):
            raise SystemExit(f'job {job["id"]} 的 evidence_root 名称非法：{path.name}')
        seen_roots[resolved] = job["id"]
        print(f'{job["id"]}={path.name}={resolved}')

if not complete_jobs:
    raise SystemExit("没有已完成 Job 可供编目")
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
  --bundle-dir "$staged_bundle"

if [[ -s $work_dir/catalog/derivation-plan.json ]] &&
   python3 -c "import json,sys; sys.exit(0 if json.load(open(sys.argv[1]))['entries'] else 1)" \
     "$work_dir/catalog/derivation-plan.json"; then
  python3 "$tool_root/derive_official_observations.py" \
    --bundle-dir "$staged_bundle" --plan "$work_dir/catalog/derivation-plan.json"
fi

# 2) 回填 sha256，产出可提交的 capture manifest
python3 - "$work_dir/catalog/manifest-draft.json" "$staged_bundle" "$campaign_dir" <<'PY'
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

# 候选侧的内部状态事实必须由冻结源码快照的 go test 原始日志派生。该步骤过去
# 依赖人工命令，容易出现“抓包 Job complete、但 bundle 缺 candidate trace”；现在
# 与 bundle 一起在暂存目录内完成，任一步失败都不会发布半成品。
if [[ $side == candidate ]]; then
  go_test_artifact=$(python3 - "$staged_bundle/capture-manifest.json" <<'PY'
import json, pathlib, sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
matches = [
    artifact["path"]
    for artifact in manifest.get("artifacts", [])
    if isinstance(artifact, dict)
    and artifact.get("kind") == "stdout_log"
    and pathlib.PurePosixPath(str(artifact.get("path", ""))).name
    == "candidate-go-test.jsonl"
]
if len(matches) > 1:
    raise SystemExit(
        f"候选 bundle 最多包含一份 candidate-go-test.jsonl，实际 {len(matches)} 份"
    )
print(matches[0] if matches else "")
PY
  )
  # 2026-09-18 起该日志由 VC-5 的 candidate-trace-test Job 在同源源码树上产出并
  # 以 required 规则登记；只要目标版本的证据标签声明了这份日志，bundle 里没有
  # 它就是 Job 闭集不完整，必须失败关闭，不能再静默跳过结构化 trace。判据取
  # 自标签声明而不是版本号，未声明该日志的历史夹具保持原语义。
  go_test_declared=$(python3 - "$declaration" <<'PY'
import json, pathlib, sys

declaration = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
declared = any(
    entry.get("side") == "candidate"
    and any(rule.get("glob") == "candidate-go-test.jsonl" for rule in entry.get("rules", []))
    for entry in declaration.get("entries", [])
)
print("yes" if declared else "no")
PY
  )
  if [[ $go_test_declared == yes && -z $go_test_artifact ]]; then
    echo "候选 bundle 缺少 candidate-go-test.jsonl：candidate-trace-test Job 未产出或未被证据目录纳入" >&2
    exit 1
  fi
  if [[ -n $go_test_artifact ]]; then
    candidate_source_root=${CANDIDATE_SOURCE_ROOT:?含 candidate-go-test 的候选侧必须提供 CANDIDATE_SOURCE_ROOT}
    [[ $candidate_source_root == /* && -d $candidate_source_root && ! -L $candidate_source_root ]] || {
      echo "候选源码快照不存在、不可信或不是绝对路径：$candidate_source_root" >&2
      exit 1
    }
    mapping="$candidate_source_root/tools/official_client_capture/candidate_test_fact_map_${version_key}.json"
    profile="$candidate_source_root/tools/official_client_capture/candidate_rule_expectations_${version_key}.json"
    [[ -f $mapping && ! -L $mapping && -f $profile && ! -L $profile ]] || {
      echo "候选源码快照缺少目标版本的测试事实映射或断言画像" >&2
      exit 1
    }
    mapping_sha256=$(sha256sum "$mapping" | awk '{print $1}')
    profile_sha256=$(sha256sum "$profile" | awk '{print $1}')
    mv "$staged_bundle/capture-manifest.json" \
      "$staged_bundle/capture-manifest.json.base"
    python3 "$tool_root/candidate_test_trace.py" \
      --source-root "$candidate_source_root" \
      --evidence-root "$staged_bundle" \
      --capture-manifest "$staged_bundle/capture-manifest.json.base" \
      --go-test-artifact "$go_test_artifact" \
      --mapping "$mapping" --profile "$profile" \
      --expected-codex-version "$target_version" \
      --expected-mapping-sha256 "$mapping_sha256" \
      --expected-profile-sha256 "$profile_sha256" \
      --trace-dir candidate-trace \
      --output-manifest capture-manifest.json \
      --output-receipt candidate-trace/trace-receipt.json >/dev/null
    echo "候选测试 trace 已生成并绑定冻结源码快照"
  fi
fi

# 3) 发布前证明权威根集合与 provenance 实际来源严格相等，并立即重放复制摘要。
python3 - "$work_dir/jobroots.txt" "$staged_bundle/provenance.json" <<'PY'
import json, pathlib, sys

expected = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    job_id, root_name, root_path = line.split("=", 2)
    expected[root_name] = {"job_id": job_id, "path": root_path}
provenance = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
actual = {entry["source_root"] for entry in provenance["entries"]}
if actual != set(expected):
    missing = sorted(set(expected) - actual)
    extra = sorted(actual - set(expected))
    raise SystemExit(f"assertion bundle 根集合不闭合：missing={missing} extra={extra}")
print(f"assertion bundle 权威根闭合：{len(expected)} 个")
PY

python3 "$tool_root/build_assertion_bundle.py" \
  "${bundle_args[@]}" --bundle-dir "$staged_bundle" --verify \
  --allow-extra derived/ --allow-extra candidate-trace/ \
  --allow-extra capture-manifest.json

python3 - "$staged_bundle" "$bundle_dir" <<'PY'
import os, pathlib, sys

source = pathlib.Path(sys.argv[1])
target = pathlib.Path(sys.argv[2])
if target.exists() or target.is_symlink():
    raise SystemExit(f"断言证据包发布目标已存在：{target}")
os.rename(source, target)
PY

echo "断言证据包就绪：$bundle_dir"
echo "接下来执行 capture-$side seal（--capture-manifest 可省略，seal 会在证据根内唯一发现）。"
