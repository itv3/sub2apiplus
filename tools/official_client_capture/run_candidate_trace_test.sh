#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 候选侧零请求 Job：在 Candidate 同源源码树上执行冻结的 go test -json，产出
# candidate-go-test.jsonl（SPEC-PROTO-002 等判据所需的 transport_fallback／
# connection_lifecycle 等内部状态记录只能来自这份日志，抓包面不存在）。
#
# 2026-09-18 起由 VC-5 的 candidate-trace-test Job 承担。此前该日志从未由任何
# 脚本产出（0.151 那份是采集后手工放入证据根的），Framework／指南要求
# "内部状态记录只能来自同源候选树的冻结测试日志"，因此：
#   1. 源码树只认 Campaign 内冻结的 build-receipt.json 的 source.root，不接受
#      脚本默认值或外部同名环境变量；
#   2. 测试集合只认该源码树内 candidate_test_fact_map_<version>.json 的 tests；
#   3. -count=1 禁用缓存，GOPROXY=off／GOTOOLCHAIN=local 保证零网络；
#   4. 任何测试失败或 go 非零退出即整个 Job 失败，不产出部分日志。

codex_version=${CODEX_VERSION:?必须由 Campaign 提供 CODEX_VERSION}
if [[ ! $codex_version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "CODEX_VERSION 必须是完整的 x.y.z 版本。" >&2
  exit 2
fi
version_key=${codex_version//./_}
campaign_dir=${CAMPAIGN_DIR:?必须由 Campaign 提供 CAMPAIGN_DIR}
candidate_id=${CANDIDATE_ID:?必须由 Campaign 提供 CANDIDATE_ID}
capture_root=${CAPTURE_ROOT:-/root/oauth-capture}
run_id=${RUN_ID:?必须提供 RUN_ID}
go_bin=${GO_BIN:-go}

if [[ $campaign_dir != /* || -L $campaign_dir || ! -d $campaign_dir ]]; then
  echo "CAMPAIGN_DIR 必须是绝对、非符号链接的目录：$campaign_dir" >&2
  exit 2
fi
if [[ ! $candidate_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ || ! $run_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  echo "CANDIDATE_ID 与 RUN_ID 必须是安全标识。" >&2
  exit 2
fi
build_receipt="$campaign_dir/candidates/$candidate_id/build-receipt.json"
if [[ -L $build_receipt || ! -f $build_receipt ]]; then
  echo "候选构建收据不存在或不可信：$build_receipt" >&2
  exit 2
fi

# 从冻结的构建收据读源码根与 commit；收据身份必须与本 Job 的 Campaign／
# Candidate 一致，避免用错树。
if ! source_binding=$(python3 - "$build_receipt" "$candidate_id" <<'PY'
import json, sys
from pathlib import Path

receipt = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if receipt.get("candidate_id") != sys.argv[2]:
    raise SystemExit(f"构建收据 candidate_id 不一致：{receipt.get('candidate_id')!r}")
source = receipt.get("source")
if not isinstance(source, dict):
    raise SystemExit("构建收据缺少 source 绑定")
root = str(source.get("root", ""))
commit = str(source.get("git_commit", ""))
if not root.startswith("/") or len(commit) != 40:
    raise SystemExit("构建收据 source.root／git_commit 非法")
print(root, commit)
PY
); then
  echo "候选构建收据无法解析源码根。" >&2
  exit 2
fi
read -r source_root source_commit <<<"$source_binding"
if [[ -L $source_root || ! -d $source_root/backend || ! -f $source_root/backend/go.mod ]]; then
  echo "候选源码树不存在、不可信或缺少 backend/go.mod：$source_root" >&2
  exit 2
fi
mapping="$source_root/tools/official_client_capture/candidate_test_fact_map_${version_key}.json"
if [[ -L $mapping || ! -f $mapping ]]; then
  echo "候选源码树缺少目标版本的测试事实映射：$mapping" >&2
  exit 2
fi

# 从映射读取包与测试名；映射自身声明的 codex_version 必须等于 Campaign 目标。
if ! mapping_output=$(python3 - "$mapping" "$codex_version" <<'PY'
import json, re, sys
from pathlib import Path

mapping = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if mapping.get("codex_version") != sys.argv[2]:
    raise SystemExit(f"测试事实映射版本 {mapping.get('codex_version')!r} 与 Campaign 目标不一致")
tests = mapping.get("tests")
if not isinstance(tests, list) or not tests:
    raise SystemExit("测试事实映射没有 tests")
module = "github.com/Wei-Shaw/sub2api"
packages: list[str] = []
names: list[str] = []
for test in tests:
    package = str(test.get("package", ""))
    name = str(test.get("name", ""))
    if not package.startswith(module + "/") or not re.fullmatch(r"Test[A-Za-z0-9_]+", name):
        raise SystemExit(f"映射条目非法：{package!r} {name!r}")
    relative = "./" + package[len(module) + 1 :]
    if relative not in packages:
        packages.append(relative)
    if name not in names:
        names.append(name)
print(" ".join(packages))
print("|".join(names))
PY
); then
  echo "测试事实映射无法解析。" >&2
  exit 2
fi
mapfile -t mapping_lines <<<"$mapping_output"
packages_line=${mapping_lines[0]:?映射未给出测试包}
names_regex=${mapping_lines[1]:?映射未给出测试名}
read -r -a packages <<<"$packages_line"

evidence_root="$capture_root/runs/$run_id"
if [[ -e $evidence_root || -L $evidence_root ]]; then
  echo "证据根已存在，拒绝覆盖：$evidence_root" >&2
  exit 2
fi
mkdir -p "$evidence_root"
chmod 700 "$evidence_root"
log_path="$evidence_root/candidate-go-test.jsonl"
summary_path="$evidence_root/run-summary.json"

started_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)
go_version=$("$go_bin" version 2>/dev/null || true)
if [[ -z $go_version ]]; then
  echo "go 工具链不可用：$go_bin" >&2
  exit 2
fi
mod_flag=-mod=mod
if [[ -f $source_root/backend/vendor/modules.txt ]]; then
  mod_flag=-mod=vendor
fi

# 零网络：模块只能来自 vendor 或本地模块缓存；工具链禁止自动下载。
export GOPROXY=off GOTOOLCHAIN=local GOFLAGS="$mod_flag"
export GOMODCACHE=${GOMODCACHE:-/root/go/pkg/mod}
export GOCACHE=${GOCACHE:-$evidence_root/.gocache}
command=("$go_bin" test -json -count=1 -run "^(${names_regex})\$" "${packages[@]}")
set +e
(
  cd "$source_root/backend"
  "${command[@]}"
) >"$log_path" 2>"$evidence_root/go-test.stderr"
exit_code=$?
set -e
rm -rf "$evidence_root/.gocache"
finished_at_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)

# 逐条核对：每个映射测试恰好 run 一次且 pass；缺失、失败或多余都视为失败。
verdict=$(python3 - "$log_path" "$names_regex" <<'PY' || true
import json, sys
from collections import Counter
from pathlib import Path

expected = set(sys.argv[2].split("|"))
runs: Counter[str] = Counter()
passes: Counter[str] = Counter()
fails: set[str] = set()
for line in Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    if not line.strip():
        continue
    event = json.loads(line)
    name = event.get("Test")
    if not name:
        continue
    action = event.get("Action")
    if action == "run":
        runs[name] += 1
    elif action == "pass":
        passes[name] += 1
    elif action == "fail":
        fails.add(name)
missing = sorted(expected - set(passes))
extra = sorted(set(runs) - expected)
duplicated = sorted(name for name, count in runs.items() if count != 1)
if missing or extra or duplicated or fails:
    print(f"fail missing={missing} extra={extra} duplicated={duplicated} failed={sorted(fails)}")
else:
    print("pass")
PY
)

python3 - "$summary_path" <<PY
import hashlib, json
from pathlib import Path

log = Path("$log_path")
digest = hashlib.sha256(log.read_bytes()).hexdigest()
summary = {
    "schema_version": "candidate-trace-test/v1",
    "run_id": "$run_id",
    "codex_version": "$codex_version",
    "campaign_dir": "$campaign_dir",
    "candidate_id": "$candidate_id",
    "build_receipt_sha256": hashlib.sha256(Path("$build_receipt").read_bytes()).hexdigest(),
    "source_root": "$source_root",
    "source_git_commit": "$source_commit",
    "mapping_path": "$mapping",
    "mapping_sha256": hashlib.sha256(Path("$mapping").read_bytes()).hexdigest(),
    "go_version": "$go_version",
    "go_flags": "$GOFLAGS",
    "command": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${command[@]}"),
    "packages": $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1:]))' "${packages[@]}"),
    "exit_code": $exit_code,
    "verdict": "$verdict",
    "log_path": str(log),
    "log_sha256": digest,
    "log_bytes": log.stat().st_size,
    "started_at_utc": "$started_at_utc",
    "finished_at_utc": "$finished_at_utc",
}
Path("$summary_path").write_text(json.dumps(summary, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
PY
chmod 600 "$log_path" "$summary_path" "$evidence_root/go-test.stderr"

if [[ $exit_code -ne 0 || $verdict != pass ]]; then
  echo "candidate-trace-test 失败：exit=$exit_code verdict=$verdict" >&2
  tail -n 20 "$evidence_root/go-test.stderr" >&2 || true
  exit 1
fi
echo "candidate-trace-test 完成：$log_path"
