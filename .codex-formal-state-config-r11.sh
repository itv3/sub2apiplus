#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# 采集正式验收前后的确定性生产状态。状态 JSON 不含时间、运行批次或证据路径；
# 环境恢复完整时 restoration/before.json 与 restoration/after.json 必须字节相等。
# 时间与断言绑定只写入独立 receipt，任何正式文件均采用排他创建，拒绝覆盖。

die() {
  printf 'formal-state-config-r11: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || die "缺少必需命令：$1"
}

require_regular_file() {
  [[ -f $1 && ! -L $1 ]] || die "缺少非符号链接普通文件：$1"
}

require_directory() {
  [[ -d $1 && ! -L $1 ]] || die "缺少非符号链接目录：$1"
}

require_absent() {
  [[ ! -e $1 && ! -L $1 ]] || die "目标已存在，拒绝覆盖：$1"
}

link_exclusive() {
  local source=$1
  local target=$2
  require_regular_file "$source"
  require_absent "$target"
  ln -- "$source" "$target" || die "无法排他创建：$target"
  chmod 0600 "$target"
}

mode=${MODE:?必须提供 MODE=before 或 MODE=after}
assessment_id=${ASSESSMENT_ID:?必须提供 ASSESSMENT_ID}
evidence_root=${EVIDENCE_ROOT:?必须提供 EVIDENCE_ROOT}

[[ $mode == before || $mode == after ]] || die "MODE 只能是 before 或 after"
[[ $assessment_id =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] ||
  die "ASSESSMENT_ID 格式非法"
[[ $evidence_root == "/root/oauth-capture/evidence/$assessment_id" ]] ||
  die "EVIDENCE_ROOT 必须精确对应 ASSESSMENT_ID"

source_root=/root/Docker/sub2apiplus/builds/codex0145-20260730T195700Z-r11/source
deployment_root=/root/Docker/sub2apiplus/deployments/codex0145-20260730T195700Z-r11
runtime_root=/root/oauth-capture/runtime/formal-config-$assessment_id
restoration_root=$evidence_root/restoration
state_path=$restoration_root/$mode.json
receipt_path=$restoration_root/$mode.receipt.json
identity_path=$runtime_root/candidate-identity.json
assertion_index_path=$evidence_root/assertions/candidate-42/assertion-index.json

service_container=sub2apiplus
postgres_container=sub2apiplus-postgres
redis_container=sub2apiplus-redis
keeper_container=sub2apiplus-keeper

# R11 冻结画像。这里既验证运行镜像，也验证其对应的不可变源码与部署清单。
expected_git_commit=ecbfa13993d2f4cbbe274aa4693b338c94a41132
expected_source_tree=39e579acb066d0daaf036dfd23ae2c61c5cc99137bc9a0eb9af0bcf073363a5b
expected_image_reference=sub2apiplus:codex0145-20260730T195700Z-39e579acb066-r11
expected_image_digest=sha256:a0228995f1879f345a59cc9836770d3a276618b69ed51089f1b6a707f4d5ee14
expected_deployed_version=0.1.165-codex0145-20260730T195700Z-r11
expected_postgres_id=1beb049f386eee8c619b4c49434528acf1fcff4e8510f2f64367041455a6b3ea
expected_redis_id=1c37487fe738deffea8d6fdacd4cd4a734d617ecbc3879d50679c8a988c37fbf
expected_keeper_id=12d28415de597e49ed54d835698fcf96ad22203fdeea4bade794f3d544508776

for command_name in docker jq python3 sha256sum sed awk grep find date cmp ln mktemp \
  tr mkdir chmod rm; do
  require_command "$command_name"
done
require_directory /root/oauth-capture/evidence
require_directory /root/oauth-capture/runtime
require_directory "$source_root"
require_directory "$deployment_root"

temporary_files=()
cleanup_temporary_files() {
  local path
  set +e
  unset auth_digest auth_cache_key
  for path in "${temporary_files[@]}"; do
    if [[ -f $path && ! -L $path ]]; then
      rm -f -- "$path"
    fi
  done
}
trap cleanup_temporary_files EXIT

if [[ $mode == before ]]; then
  require_absent "$evidence_root"
  require_absent "$runtime_root"
  mkdir -m 0700 -- "$evidence_root" || die "无法排他创建新 evidence root"
  [[ -z $(find "$evidence_root" -mindepth 1 -maxdepth 1 -print -quit) ]] ||
    die "新 evidence root 并非空目录"
  mkdir -m 0700 -- "$restoration_root" || die "无法创建 restoration 目录"
  mkdir -m 0700 -- "$runtime_root" || die "无法创建外置 identity 目录"
else
  require_directory "$evidence_root"
  require_directory "$restoration_root"
  require_directory "$runtime_root"
  require_regular_file "$restoration_root/before.json"
  require_regular_file "$restoration_root/before.receipt.json"
  require_regular_file "$identity_path"
fi
require_absent "$state_path"
require_absent "$receipt_path"

require_regular_file "$source_root/GIT_COMMIT"
require_regular_file "$source_root/SOURCE_FILE_SHA256SUMS"
require_regular_file "$source_root/SOURCE_TREE_SHA256"
require_regular_file "$deployment_root/deployment-evidence.sha256"
require_regular_file "$deployment_root/image.override.yml"
require_regular_file "$deployment_root/source-tree-sha256.txt"
require_regular_file "$deployment_root/version.txt"

# 源码树与部署元数据必须先完成离线完整性校验，之后才读取候选身份。
(
  cd "$source_root"
  sha256sum -c SOURCE_FILE_SHA256SUMS >/dev/null
) || die "冻结源码清单校验失败"
(
  cd "$deployment_root"
  sha256sum -c deployment-evidence.sha256 >/dev/null
) || die "冻结部署清单校验失败"

git_commit=$(tr -d '\r\n' <"$source_root/GIT_COMMIT")
source_tree_sha256=$(tr -d '\r\n' <"$source_root/SOURCE_TREE_SHA256")
source_manifest_sha256=$(sha256sum "$source_root/SOURCE_FILE_SHA256SUMS" | awk '{print $1}')
deployment_source_tree=$(tr -d '\r\n' <"$deployment_root/source-tree-sha256.txt")
deployed_version=$(tr -d '\r\n' <"$deployment_root/version.txt")
image_reference=$(docker inspect -f '{{.Config.Image}}' "$service_container")
image_digest=$(docker inspect -f '{{.Image}}' "$service_container")
image_lookup_digest=$(docker image inspect -f '{{.Id}}' "$image_reference")
service_health=$(docker inspect -f \
  '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
  "$service_container")

[[ $git_commit == "$expected_git_commit" ]] || die "GIT_COMMIT 不符合冻结画像"
[[ $source_tree_sha256 == "$expected_source_tree" ]] || die "源码树摘要不符合冻结画像"
[[ $source_manifest_sha256 == "$source_tree_sha256" ]] || die "源码树摘要未绑定文件清单"
[[ $deployment_source_tree == "$source_tree_sha256" ]] || die "部署元数据未绑定源码树"
[[ $deployed_version == "$expected_deployed_version" ]] || die "部署版本不符合冻结画像"
[[ $image_reference == "$expected_image_reference" ]] || die "运行镜像引用不符合冻结画像"
[[ $image_digest == "$expected_image_digest" ]] || die "运行镜像摘要不符合冻结画像"
[[ $image_lookup_digest == "$image_digest" ]] || die "镜像引用与运行摘要不一致"
[[ $service_health == healthy ]] || die "sub2api 未处于 healthy 状态"

attestation_env_count=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    awk '/^SUB2API_LIVE_ATTESTATION_CAPTURE_/ {count++} END {print count+0}'
)
[[ $attestation_env_count == 0 ]] || die "normal 容器仍带 live attestation 环境"

temporary_ca_absent=false
if docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-core-capture.crt &&
  docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-aux-capture.crt &&
  docker exec "$service_container" test ! -e \
    /usr/local/share/ca-certificates/candidate-capture.crt; then
  temporary_ca_absent=true
fi
[[ $temporary_ca_absent == true ]] || die "normal 容器仍有临时抓包 CA"

temporary_hosts_absent=false
if ! docker exec "$service_container" grep -Eqi \
  '(^|[[:space:]])(([[:alnum:]_-]+\.)*chatgpt\.com|api\.openai\.com|auth\.openai\.com|([[:alnum:]_-]+\.)*oaiusercontent\.com)([[:space:]]|$)' \
  /etc/hosts; then
  temporary_hosts_absent=true
fi
[[ $temporary_hosts_absent == true ]] || die "normal 容器仍有临时 ChatGPT hosts 映射"

db_user=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_USER=//p'
)
db_name=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_DB=//p'
)
[[ $db_user =~ ^[A-Za-z0-9_.-]+$ && $db_name =~ ^[A-Za-z0-9_.-]+$ ]] ||
  die "无法读取 PostgreSQL 非敏感连接元数据"

db_query() {
  docker exec "$postgres_container" \
    psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" -qAtc "$1"
}

mount_fingerprint() {
  docker inspect -f '{{json .Mounts}}' "$1" |
    python3 -c '
import hashlib
import json
import sys

mounts = json.load(sys.stdin)
payload = json.dumps(mounts, sort_keys=True, separators=(",", ":")).encode("utf-8")
print(f"{len(mounts)}|{hashlib.sha256(payload).hexdigest()}")
'
}

postgres_id=$(docker inspect -f '{{.Id}}' "$postgres_container")
redis_id=$(docker inspect -f '{{.Id}}' "$redis_container")
keeper_id=$(docker inspect -f '{{.Id}}' "$keeper_container")
postgres_running=$(docker inspect -f '{{.State.Running}}' "$postgres_container")
redis_running=$(docker inspect -f '{{.State.Running}}' "$redis_container")
keeper_running=$(docker inspect -f '{{.State.Running}}' "$keeper_container")
IFS='|' read -r postgres_mount_count postgres_mount_sha256 <<<"$(mount_fingerprint "$postgres_container")"
IFS='|' read -r redis_mount_count redis_mount_sha256 <<<"$(mount_fingerprint "$redis_container")"
IFS='|' read -r keeper_mount_count keeper_mount_sha256 <<<"$(mount_fingerprint "$keeper_container")"

[[ $postgres_id == "$expected_postgres_id" ]] || die "PostgreSQL 容器 ID 偏离冻结基线"
[[ $redis_id == "$expected_redis_id" ]] || die "Redis 容器 ID 偏离冻结基线"
[[ $keeper_id == "$expected_keeper_id" ]] || die "Keeper 容器 ID 偏离冻结基线"
[[ $postgres_running == true && $redis_running == true && $keeper_running == true ]] ||
  die "数据容器或 Keeper 未运行"
for fingerprint in "$postgres_mount_sha256" "$redis_mount_sha256" "$keeper_mount_sha256"; do
  [[ $fingerprint =~ ^[0-9a-f]{64}$ ]] || die "数据容器挂载摘要非法"
done
for mount_count in "$postgres_mount_count" "$redis_mount_count" "$keeper_mount_count"; do
  [[ $mount_count =~ ^[0-9]+$ ]] || die "数据容器挂载数量非法"
done

entity_counts=$(db_query "
select (select count(*) from users)::text || '|' ||
       (select count(*) from accounts)::text || '|' ||
       (select count(*) from groups)::text || '|' ||
       (select count(*) from api_keys)::text || '|' ||
       (select count(*) from account_groups)::text || '|' ||
       (select count(*) from proxies)::text")
group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id=9 and deleted_at is null")
account50_state=$(db_query "
select platform || '|' || type || '|' || status || '|' || schedulable::text || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id=50 and deleted_at is null")
account99_state=$(db_query "
select platform || '|' || type || '|' || status || '|' || schedulable::text || '|' ||
       coalesce(parent_account_id::text,'NULL') || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id=99 and deleted_at is null")
account99_extra_sha256=$(db_query "
select encode(sha256(convert_to(extra::text,'UTF8')),'hex')
from accounts where id=99 and deleted_at is null")
account99_model_mapping=$(db_query "
select case when credentials ? 'model_mapping'
  then 'present|' || encode(
    sha256(convert_to((credentials->'model_mapping')::text,'UTF8')),'hex')
  else 'absent|NULL' end
from accounts where id=99 and deleted_at is null")
api_key15_state=$(db_query "
select id::text || '|' || group_id::text || '|' || status
from api_keys where id=15 and deleted_at is null")

[[ $entity_counts =~ ^[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+\|[0-9]+$ ]] ||
  die "保护实体计数读取失败"
[[ $group_state == 'composite|false|true|false' ]] || die "group #9 偏离最终配置"
[[ $account50_state == 'anthropic|oauth|error|false|NULL|NULL' ]] ||
  die "account #50 的调度或代理状态偏离最终配置"
[[ $account99_state == 'openai|oauth|active|true|NULL|NULL|NULL' ]] ||
  die "account #99 的调度或代理状态偏离最终配置"
[[ $account99_extra_sha256 =~ ^[0-9a-f]{64}$ ]] || die "account #99 extra 摘要非法"
[[ $account99_model_mapping =~ ^(present\|[0-9a-f]{64}|absent\|NULL)$ ]] ||
  die "account #99 model_mapping 状态非法"
[[ $api_key15_state == '15|9|active' ]] || die "API Key #15 非敏感状态偏离最终配置"

# API Key 明文不离开 PostgreSQL。外层只取得 SHA-256，并通过 redis-cli -x
# 从标准输入检查精确缓存键；摘要和缓存键均不写入任何输出。
auth_digest=$(db_query "
select encode(sha256(convert_to(key,'UTF8')),'hex')
from api_keys
where id=15 and group_id=9 and status='active' and deleted_at is null")
[[ $auth_digest =~ ^[0-9a-f]{64}$ ]] || die "API Key #15 摘要读取失败"
auth_cache_key=apikey:auth:$auth_digest
auth_cache_exists=$(
  printf '%s' "$auth_cache_key" |
    docker exec -i "$redis_container" redis-cli --raw -x EXISTS |
    tr -d '\r'
)
unset auth_digest auth_cache_key
[[ $auth_cache_exists == 0 ]] || die "API Key #15 认证缓存未清空"
redis_ping=$(docker exec "$redis_container" redis-cli PING | tr -d '\r')
redis_loading=$(
  docker exec "$redis_container" redis-cli INFO persistence |
    sed -n 's/^loading://p' |
    tr -d '\r'
)
[[ $redis_ping == PONG && $redis_loading == 0 ]] || die "Redis 不可读或仍在加载"

assertion_generated_at=
assertion_latest_finished_at=
assertion_capture_manifest=
assertion_index_sha256=
if [[ $mode == after ]]; then
  require_directory "$evidence_root/assertions"
  require_directory "$evidence_root/assertions/candidate-42"
  require_regular_file "$assertion_index_path"
  assertion_metadata=$(
    python3 - "$source_root" "$evidence_root" "$assertion_index_path" <<'PY'
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

source_root = Path(sys.argv[1]).resolve(strict=True)
evidence_root = Path(sys.argv[2]).resolve(strict=True)
index_path = Path(sys.argv[3])
sha_pattern = re.compile(r"^[0-9a-f]{64}$")


def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"JSON 含重复键：{key}")
        value[key] = item
    return value


def load_json(path):
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle, object_pairs_hook=reject_duplicates)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file(root, relative_text):
    if not isinstance(relative_text, str) or "\\" in relative_text:
        raise ValueError("证据相对路径非法")
    relative = PurePosixPath(relative_text)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("证据相对路径逃逸")
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("证据路径包含符号链接")
    resolved = current.resolve(strict=True)
    resolved.relative_to(root)
    if not resolved.is_file():
        raise ValueError("证据路径不是普通文件")
    return resolved


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("断言时间不是字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("断言时间缺少时区")
    return parsed.astimezone(timezone.utc)


if index_path.is_symlink() or not index_path.is_file():
    raise ValueError("断言索引不是非符号链接普通文件")
index_path.resolve(strict=True).relative_to(evidence_root)
index = load_json(index_path)
required_index_keys = {
    "schema_version", "codex_version", "generated_at", "capture_manifest",
    "profile_sha256", "rule_manifest_sha256", "checker", "results",
}
if not isinstance(index, dict) or set(index) != required_index_keys:
    raise ValueError("断言索引字段不闭合")
if index["schema_version"] != "codex-candidate-assertion-index/v1":
    raise ValueError("断言索引 schema_version 不匹配")
if index["codex_version"] != "0.145.0":
    raise ValueError("断言索引 Codex 版本不匹配")

profile_path = source_root / "tools/official_client_capture/candidate_rule_expectations_0_145_0.json"
manifest_path = source_root / "tools/official_client_capture/codex_upgrade_rules_0_145_0.json"
checker_path = source_root / "tools/official_client_capture/candidate_rule_assertion.py"
for path in (profile_path, manifest_path, checker_path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("冻结断言输入不是普通文件")
if index["profile_sha256"] != digest(profile_path):
    raise ValueError("断言索引画像摘要不匹配")
if index["rule_manifest_sha256"] != digest(manifest_path):
    raise ValueError("断言索引规则清单摘要不匹配")
expected_checker = {
    "path": "tools/official_client_capture/candidate_rule_assertion.py",
    "sha256": digest(checker_path),
}
if index["checker"] != expected_checker:
    raise ValueError("断言索引 checker 绑定不匹配")

rule_manifest = load_json(manifest_path)
expected_rules = rule_manifest.get("required_rules")
if not isinstance(expected_rules, list) or len(expected_rules) != 42:
    raise ValueError("冻结规则清单不是精确 42 条")
if len(set(expected_rules)) != 42:
    raise ValueError("冻结规则清单存在重复项")

capture_ref = index["capture_manifest"]
if not isinstance(capture_ref, dict) or set(capture_ref) != {"path", "sha256"}:
    raise ValueError("断言索引 capture manifest 绑定非法")
capture_path = safe_file(evidence_root, capture_ref["path"])
if not sha_pattern.fullmatch(str(capture_ref["sha256"])) or digest(capture_path) != capture_ref["sha256"]:
    raise ValueError("断言索引 capture manifest 摘要不匹配")

results = index["results"]
if not isinstance(results, list) or len(results) != 42:
    raise ValueError("断言索引未精确包含 42 条结果")
entries = {}
latest_finished = None
latest_finished_text = ""
now = datetime.now(timezone.utc)
for entry in results:
    required_entry_keys = {"rule_id", "path", "sha256", "status", "exit_code"}
    if not isinstance(entry, dict) or set(entry) != required_entry_keys:
        raise ValueError("断言索引结果字段不闭合")
    rule_id = entry["rule_id"]
    if rule_id in entries or rule_id not in expected_rules:
        raise ValueError("断言索引含非法或重复 rule_id")
    if entry["status"] != "pass" or entry["exit_code"] != 0:
        raise ValueError(f"断言未通过：{rule_id}")
    if not isinstance(entry["sha256"], str) or not sha_pattern.fullmatch(entry["sha256"]):
        raise ValueError("断言结果摘要非法")
    result_path = safe_file(evidence_root, entry["path"])
    if digest(result_path) != entry["sha256"]:
        raise ValueError("断言结果摘要不匹配")
    result = load_json(result_path)
    required_result_keys = {
        "schema_version", "rule_id", "status", "started_at", "finished_at",
        "exit_code", "checker_sha256", "command_sha256", "checks",
    }
    if not isinstance(result, dict) or set(result) != required_result_keys:
        raise ValueError("断言结果字段不闭合")
    if result["schema_version"] != "codex-candidate-rule-assertion/v1":
        raise ValueError("断言结果 schema_version 不匹配")
    if result["rule_id"] != rule_id or result["status"] != "pass" or result["exit_code"] != 0:
        raise ValueError("断言结果状态与索引不一致")
    if result["checker_sha256"] != expected_checker["sha256"]:
        raise ValueError("断言结果 checker 摘要不匹配")
    if not isinstance(result["command_sha256"], str) or not sha_pattern.fullmatch(result["command_sha256"]):
        raise ValueError("断言结果命令摘要非法")
    checks = result["checks"]
    if not isinstance(checks, list) or not checks or any(
        not isinstance(check, dict) or check.get("passed") is not True for check in checks
    ):
        raise ValueError(f"断言检查未全部通过：{rule_id}")
    started = parse_time(result["started_at"])
    finished = parse_time(result["finished_at"])
    if started > finished or finished > now:
        raise ValueError("断言结果时间顺序非法")
    if latest_finished is None or finished > latest_finished:
        latest_finished = finished
        latest_finished_text = result["finished_at"]
    entries[rule_id] = entry

if set(entries) != set(expected_rules):
    raise ValueError("断言结果未精确覆盖冻结 42 条规则")
generated = parse_time(index["generated_at"])
if latest_finished is None or generated < latest_finished or generated > now:
    raise ValueError("断言索引时间不在 42 条结果之后")
print(f"{index['generated_at']}\t{latest_finished_text}\t{capture_ref['path']}")
PY
  ) || die "42 条断言索引校验失败"
  IFS=$'\t' read -r assertion_generated_at assertion_latest_finished_at \
    assertion_capture_manifest <<<"$assertion_metadata"
  [[ -n $assertion_generated_at && -n $assertion_latest_finished_at &&
    -n $assertion_capture_manifest ]] || die "断言索引元数据读取失败"
  assertion_index_sha256=$(sha256sum "$assertion_index_path" | awk '{print $1}')
fi

identity_tmp=$(mktemp "$runtime_root/.candidate-identity.XXXXXX")
temporary_files+=("$identity_tmp")
chmod 0600 "$identity_tmp"
jq -cnS \
  --arg git_commit "$git_commit" \
  --arg source_tree_sha256 "$source_tree_sha256" \
  --arg image_reference "$image_reference" \
  --arg image_digest "$image_digest" \
  --arg deployed_version "$deployed_version" \
  '{
    git_commit: $git_commit,
    source_tree_sha256: $source_tree_sha256,
    image_reference: $image_reference,
    image_digest: $image_digest,
    deployed_version: $deployed_version
  }' >"$identity_tmp"

state_tmp=$(mktemp "$restoration_root/.${mode}.state.XXXXXX")
temporary_files+=("$state_tmp")
chmod 0600 "$state_tmp"
python3 - "$state_tmp" "$identity_tmp" \
  "$postgres_id" "$postgres_mount_count" "$postgres_mount_sha256" \
  "$redis_id" "$redis_mount_count" "$redis_mount_sha256" \
  "$keeper_id" "$keeper_mount_count" "$keeper_mount_sha256" \
  "$entity_counts" "$group_state" "$account50_state" "$account99_state" \
  "$account99_extra_sha256" "$account99_model_mapping" "$api_key15_state" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    output_path, identity_path,
    postgres_id, postgres_mount_count, postgres_mount_sha,
    redis_id, redis_mount_count, redis_mount_sha,
    keeper_id, keeper_mount_count, keeper_mount_sha,
    entity_counts, group_state, account50_state, account99_state,
    account99_extra_sha, account99_model_mapping, api_key15_state,
) = sys.argv[1:]


def nullable(value):
    return None if value == "NULL" else int(value)


identity = json.loads(Path(identity_path).read_text(encoding="utf-8"))
users, accounts, groups, api_keys, account_groups, proxies = map(int, entity_counts.split("|"))
group_platform, require_oauth_only, allow_live, allow_image_generation = group_state.split("|")
(
    account50_platform, account50_type, account50_status, account50_schedulable,
    account50_proxy, account50_fallback,
) = account50_state.split("|")
(
    account99_platform, account99_type, account99_status, account99_schedulable,
    account99_parent, account99_proxy, account99_fallback,
) = account99_state.split("|")
mapping_presence, mapping_sha = account99_model_mapping.split("|", 1)
api_key_id, api_key_group, api_key_status = api_key15_state.split("|")

payload = {
    "schema_version": "codex-formal-restoration-state/v1",
    "candidate_identity": identity,
    "runtime": {
        "service_healthy": True,
        "attestation_environment_absent": True,
        "temporary_capture_ca_absent": True,
        "temporary_chatgpt_hosts_absent": True,
    },
    "data_containers": {
        "postgres": {
            "container_id": postgres_id,
            "mount_count": int(postgres_mount_count),
            "mounts_sha256": postgres_mount_sha,
            "running": True,
        },
        "redis": {
            "container_id": redis_id,
            "mount_count": int(redis_mount_count),
            "mounts_sha256": redis_mount_sha,
            "running": True,
            "readable": True,
        },
        "keeper": {
            "container_id": keeper_id,
            "mount_count": int(keeper_mount_count),
            "mounts_sha256": keeper_mount_sha,
            "running": True,
        },
    },
    "database": {
        "entity_counts": {
            "users": users,
            "accounts": accounts,
            "groups": groups,
            "api_keys": api_keys,
            "account_groups": account_groups,
            "proxies": proxies,
        },
        "group_9": {
            "platform": group_platform,
            "require_oauth_only": require_oauth_only == "true",
            "allow_live": allow_live == "true",
            "allow_image_generation": allow_image_generation == "true",
        },
        "account_50": {
            "platform": account50_platform,
            "type": account50_type,
            "status": account50_status,
            "schedulable": account50_schedulable == "true",
            "proxy_id": nullable(account50_proxy),
            "proxy_fallback_origin_id": nullable(account50_fallback),
        },
        "account_99": {
            "platform": account99_platform,
            "type": account99_type,
            "status": account99_status,
            "schedulable": account99_schedulable == "true",
            "parent_account_id": nullable(account99_parent),
            "proxy_id": nullable(account99_proxy),
            "proxy_fallback_origin_id": nullable(account99_fallback),
            "extra_sha256": account99_extra_sha,
            "model_mapping": {
                "present": mapping_presence == "present",
                "sha256": mapping_sha if mapping_presence == "present" else None,
            },
        },
        "api_key_15": {
            "id": int(api_key_id),
            "group_id": int(api_key_group),
            "status": api_key_status,
            "deleted_at_absent": True,
            "auth_cache_absent": True,
        },
    },
}
Path(output_path).write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
os.chmod(output_path, 0o600)
PY

if [[ $mode == after ]]; then
  cmp -s "$identity_path" "$identity_tmp" || die "after 候选身份与 before 不一致"
  cmp -s "$restoration_root/before.json" "$state_tmp" ||
    die "after 状态与 before 不满足字节相等"
fi

state_sha256=$(sha256sum "$state_tmp" | awk '{print $1}')
identity_sha256=$(sha256sum "$identity_tmp" | awk '{print $1}')
captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
receipt_tmp=$(mktemp "$restoration_root/.${mode}.receipt.XXXXXX")
temporary_files+=("$receipt_tmp")
chmod 0600 "$receipt_tmp"
python3 - "$receipt_tmp" "$mode" "$assessment_id" "$captured_at" \
  "$state_sha256" "$identity_sha256" "$identity_path" \
  "$assertion_index_sha256" "$assertion_generated_at" \
  "$assertion_latest_finished_at" "$assertion_capture_manifest" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    output_path, mode, assessment_id, captured_at, state_sha256,
    identity_sha256, identity_path, assertion_index_sha256,
    assertion_generated_at, assertion_latest_finished_at,
    assertion_capture_manifest,
) = sys.argv[1:]

assertions = None
if mode == "after":
    assertions = {
        "index_path": "assertions/candidate-42/assertion-index.json",
        "index_sha256": assertion_index_sha256,
        "generated_at": assertion_generated_at,
        "latest_result_finished_at": assertion_latest_finished_at,
        "capture_manifest_path": assertion_capture_manifest,
        "passed_rule_count": 42,
    }
payload = {
    "schema_version": "codex-formal-restoration-receipt/v1",
    "assessment_id": assessment_id,
    "mode": mode,
    "captured_at": captured_at,
    "status": "pass",
    "state": {
        "path": f"restoration/{mode}.json",
        "sha256": state_sha256,
        "contains_time": False,
    },
    "candidate_identity": {
        "path_outside_evidence": identity_path,
        "sha256": identity_sha256,
    },
    "assertions": assertions,
}
Path(output_path).write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
os.chmod(output_path, 0o600)
PY

if [[ $mode == before ]]; then
  link_exclusive "$identity_tmp" "$identity_path"
fi
link_exclusive "$state_tmp" "$state_path"
link_exclusive "$receipt_tmp" "$receipt_path"

printf 'formal-state-config-r11: mode=%s assessment=%s state_sha256=%s identity=%s\n' \
  "$mode" "$assessment_id" "$state_sha256" "$identity_path"
