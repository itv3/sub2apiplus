#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# 在 Vircs 上冻结最终 R11 源码画像、镜像别名与部署元数据。
# 只允许重建 sub2api；PostgreSQL、Redis、Keeper 及其挂载一律不得重建。

source_root=/root/Docker/sub2apiplus/builds/codex0145-20260730T195700Z-r11/source
deployment_root=/root/Docker/sub2apiplus/deployments/codex0145-20260730T195700Z-r11
compose_file=/root/Docker/sub2apiplus/app/docker-compose.yml
old_override="$deployment_root/image.override.yml"
old_normal_ref=sub2apiplus:codex0145-20260730T195700Z-f9bd704c2e75-r11
normal_image_id=sha256:a0228995f1879f345a59cc9836770d3a276618b69ed51089f1b6a707f4d5ee14
capture_image_id=sha256:54aee6e64177d2db210fd183f829aa90cfdb4ec7ed9cf3fdbfecb50c82473b64

service_container=sub2apiplus
postgres_container=sub2apiplus-postgres
redis_container=sub2apiplus-redis
keeper_container=sub2apiplus-keeper
expected_postgres_id=1beb049f386eee8c619b4c49434528acf1fcff4e8510f2f64367041455a6b3ea
expected_redis_id=1c37487fe738deffea8d6fdacd4cd4a734d617ecbc3879d50679c8a988c37fbf
expected_keeper_id=12d28415de597e49ed54d835698fcf96ad22203fdeea4bade794f3d544508776

freeze_id=${FREEZE_ID:-$(date -u +%Y%m%dT%H%M%SZ)}
if [[ ! $freeze_id =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "FREEZE_ID 格式非法。" >&2
  exit 2
fi
runtime_dir=/root/oauth-capture/runtime/freeze-r11-$freeze_id
backup_dir="$runtime_dir/backups"
if [[ -e $runtime_dir ]]; then
  echo "冻结运行目录已存在，拒绝覆盖：$runtime_dir" >&2
  exit 2
fi
install -d -m 0700 "$runtime_dir" "$backup_dir"

db_user=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_USER=//p'
)
db_name=$(
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres_container" |
    sed -n 's/^POSTGRES_DB=//p'
)
if [[ -z $db_user || -z $db_name ]]; then
  echo "无法读取 PostgreSQL 连接元数据。" >&2
  exit 1
fi

db_query() {
  docker exec "$postgres_container" psql -U "$db_user" -d "$db_name" -qAtc "$1"
}

wait_healthy() {
  local health
  for _ in $(seq 1 90); do
    health=$(docker inspect -f \
      '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' \
      "$service_container" 2>/dev/null || true)
    [[ $health == healthy || $health == running ]] && return 0
    sleep 1
  done
  return 1
}

attestation_env_count() {
  docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service_container" |
    sed -n '/^SUB2API_LIVE_ATTESTATION_CAPTURE_/p' |
    wc -l |
    tr -d ' '
}

mount_hash() {
  docker inspect -f '{{json .Mounts}}' "$1" | sha256sum | awk '{print $1}'
}

postgres_counts() {
  db_query "
select (select count(*) from users) || '|' ||
       (select count(*) from accounts) || '|' ||
       (select count(*) from groups) || '|' ||
       (select count(*) from api_keys) || '|' ||
       (select count(*) from account_groups) || '|' ||
       (select count(*) from proxies)"
}

redis_count() {
  docker exec "$redis_container" redis-cli DBSIZE | tr -d '\r'
}

keeper_count() {
  local root=/root/Docker/sub2apiplus/keeper/app/data
  printf '%s|%s' \
    "$(find "$root" -type f | wc -l | tr -d ' ')" \
    "$(find "$root" -type d | wc -l | tr -d ' ')"
}

compose_shape() {
  docker compose -f "$compose_file" -f "$1" config --format json |
    python3 -c '
import json
import sys

service = json.load(sys.stdin)["services"]["sub2api"]
environment = service.get("environment") or {}
names = ({item.split("=", 1)[0] for item in environment}
         if isinstance(environment, list) else set(environment))
count = sum(name.startswith("SUB2API_LIVE_ATTESTATION_CAPTURE_") for name in names)
print("{}|{}".format(service.get("image", ""), count))
'
}

stage_file() {
  local source=$1
  local target=$2
  local mode=${3:-0600}
  local staged
  staged=$(mktemp "$(dirname "$target")/.freeze-r11.$(basename "$target").XXXXXX")
  install -m "$mode" "$source" "$staged"
  sync -f "$staged"
  printf '%s' "$staged"
}

atomic_restore() {
  local source=$1
  local target=$2
  local staged
  staged=$(stage_file "$source" "$target" 0600) || return 1
  mv -f -- "$staged" "$target"
}

current_state_matches() {
  local group_state account_shape account_extra
  state_mismatch() {
    printf '%s\n' "$1" >"$runtime_dir/state-mismatch.txt"
    chmod 0600 "$runtime_dir/state-mismatch.txt"
    return 1
  }
  group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id = 9" 2>/dev/null) || return 1
  account_shape=$(db_query "
select platform || '|' || type || '|' || status || '|' || schedulable::text || '|' ||
       coalesce(parent_account_id::text,'NULL') || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id = 99" 2>/dev/null) || return 1
  account_extra=$(db_query "
select encode(convert_to(extra::text,'UTF8'),'hex') from accounts where id = 99" \
    2>/dev/null) || return 1

  [[ $group_state == "$initial_group_state" ]] || state_mismatch group
  [[ $account_shape == "$initial_account_shape" ]] || state_mismatch account-shape
  [[ $account_extra == "$initial_account_extra" ]] || state_mismatch account-extra
  [[ $(docker inspect -f '{{.Id}}' "$postgres_container" 2>/dev/null) == "$initial_postgres_id" ]] || state_mismatch postgres-id
  [[ $(docker inspect -f '{{.Id}}' "$redis_container" 2>/dev/null) == "$initial_redis_id" ]] || state_mismatch redis-id
  [[ $(docker inspect -f '{{.Id}}' "$keeper_container" 2>/dev/null) == "$initial_keeper_id" ]] || state_mismatch keeper-id
  [[ $(docker inspect -f '{{.State.Running}}' "$postgres_container" 2>/dev/null) == true ]] || state_mismatch postgres-running
  [[ $(docker inspect -f '{{.State.Running}}' "$redis_container" 2>/dev/null) == true ]] || state_mismatch redis-running
  [[ $(docker inspect -f '{{.State.Running}}' "$keeper_container" 2>/dev/null) == true ]] || state_mismatch keeper-running
  [[ $(mount_hash "$postgres_container" 2>/dev/null) == "$initial_postgres_mounts" ]] || state_mismatch postgres-mounts
  [[ $(mount_hash "$redis_container" 2>/dev/null) == "$initial_redis_mounts" ]] || state_mismatch redis-mounts
  [[ $(mount_hash "$keeper_container" 2>/dev/null) == "$initial_keeper_mounts" ]] || state_mismatch keeper-mounts
  [[ $(postgres_counts 2>/dev/null) == "$initial_postgres_counts" ]] || state_mismatch postgres-counts
  # Redis 键会因正常 TTL 到期或应用重启后的认证缓存预热而增减，Keeper 也会
  # 自主整理运行文件；二者不能用瞬时条目数做“数据未丢”的等值门禁。这里以
  # 容器 ID、持久化挂载摘要、运行态和服务可读性证明没有重建或清空。
  [[ $(docker exec "$redis_container" redis-cli PING 2>/dev/null | tr -d '\r') == PONG ]] || state_mismatch redis-ping
  [[ $(docker exec "$redis_container" redis-cli INFO persistence 2>/dev/null |
      sed -n 's/^loading://p' | tr -d '\r') == 0 ]] || state_mismatch redis-loading
  [[ -d /root/Docker/sub2apiplus/keeper/app/data ]] || state_mismatch keeper-data-dir
  [[ $(find /root/Docker/sub2apiplus/keeper/app/data -type f 2>/dev/null |
      sed -n '1p') != '' ]] || state_mismatch keeper-data-files
  return 0
}

source_mutated=0
deployment_mutated=0
normal_alias_created=0
capture_alias_created=0
app_recreated=0
freeze_complete=0
restore_failed=0
normal_alias=""
capture_alias=""
freeze_step=initial

record_failure_step() {
  local status=$?
  printf '%s\n' "$freeze_step" >"$runtime_dir/failure-step.txt"
  chmod 0600 "$runtime_dir/failure-step.txt"
  return "$status"
}

restore_on_failure() {
  local original_exit_code=$?
  trap - EXIT ERR INT TERM
  set +e

  if [[ $freeze_complete == 1 && $original_exit_code == 0 ]]; then
    echo "R11 冻结完成；运行记录位于 $runtime_dir。"
    exit 0
  fi

  if [[ $deployment_mutated == 1 ]]; then
    atomic_restore "$backup_dir/image.override.yml" "$deployment_root/image.override.yml" || restore_failed=1
    atomic_restore "$backup_dir/source-tree-sha256.txt" "$deployment_root/source-tree-sha256.txt" || restore_failed=1
    atomic_restore "$backup_dir/deployment-evidence.sha256" "$deployment_root/deployment-evidence.sha256" || restore_failed=1
  fi
  if [[ $source_mutated == 1 ]]; then
    atomic_restore "$backup_dir/SOURCE_FILE_SHA256SUMS" "$source_root/SOURCE_FILE_SHA256SUMS" || restore_failed=1
    atomic_restore "$backup_dir/SOURCE_TREE_SHA256" "$source_root/SOURCE_TREE_SHA256" || restore_failed=1
  fi

  if [[ $app_recreated == 1 || $deployment_mutated == 1 ]]; then
    docker compose -f "$compose_file" -f "$deployment_root/image.override.yml" \
      up -d --no-deps --force-recreate sub2api \
      >"$runtime_dir/rollback-compose.log" 2>&1 || restore_failed=1
    wait_healthy || restore_failed=1
  fi

  if [[ $normal_alias_created == 1 && -n $normal_alias ]]; then
    docker image rm "$normal_alias" >/dev/null 2>&1 || restore_failed=1
  fi
  if [[ $capture_alias_created == 1 && -n $capture_alias ]]; then
    docker image rm "$capture_alias" >/dev/null 2>&1 || restore_failed=1
  fi

  [[ $(docker inspect -f '{{.Image}}' "$service_container" 2>/dev/null) == "$normal_image_id" ]] || restore_failed=1
  [[ $(attestation_env_count 2>/dev/null) == 0 ]] || restore_failed=1
  current_state_matches || restore_failed=1

  if [[ $restore_failed != 0 ]]; then
    echo "R11 冻结失败且自动回退未完全闭合；保留 $runtime_dir。" >&2
    exit 97
  fi
  echo "R11 冻结未完成，旧源码元数据、部署元数据与 normal 容器已恢复。" >&2
  exit "$original_exit_code"
}

# 最终预检：必须从无抓包环境的 R11 normal 基线开始。
initial_postgres_id=$(docker inspect -f '{{.Id}}' "$postgres_container")
initial_redis_id=$(docker inspect -f '{{.Id}}' "$redis_container")
initial_keeper_id=$(docker inspect -f '{{.Id}}' "$keeper_container")
if [[ $initial_postgres_id != "$expected_postgres_id" ||
  $initial_redis_id != "$expected_redis_id" ||
  $initial_keeper_id != "$expected_keeper_id" ]]; then
  echo "数据容器 ID 不符合冻结基线。" >&2
  exit 1
fi
if [[ $(docker inspect -f '{{.Image}}' "$service_container") != "$normal_image_id" ||
  $(docker inspect -f '{{.State.Running}}' "$keeper_container") != true ||
  $(attestation_env_count) != 0 ]]; then
  echo "sub2api/keeper/attestation 最终基线不成立。" >&2
  exit 1
fi
wait_healthy || { echo "sub2api 不健康。" >&2; exit 1; }
docker image inspect "$normal_image_id" "$capture_image_id" >/dev/null
if [[ $(compose_shape "$old_override") != "$old_normal_ref|0" ]]; then
  echo "现有部署 override 不是无 attestation 的 R11 normal 基线。" >&2
  exit 1
fi

initial_group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' ||
       allow_live::text || '|' || allow_image_generation::text
from groups where id = 9")
initial_account_shape=$(db_query "
select platform || '|' || type || '|' || status || '|' || schedulable::text || '|' ||
       coalesce(parent_account_id::text,'NULL') || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL')
from accounts where id = 99")
initial_account_extra=$(db_query "
select encode(convert_to(extra::text,'UTF8'),'hex') from accounts where id = 99")
if [[ $initial_group_state != 'composite|false|true|false' ||
  $initial_account_shape != 'openai|oauth|active|true|NULL|NULL|NULL' ||
  ! $initial_account_extra =~ ^[0-9a-f]+$ ]]; then
  echo "group9/account99 最终基线不成立。" >&2
  exit 1
fi

initial_postgres_mounts=$(mount_hash "$postgres_container")
initial_redis_mounts=$(mount_hash "$redis_container")
initial_keeper_mounts=$(mount_hash "$keeper_container")
initial_postgres_counts=$(postgres_counts)
initial_redis_count=$(redis_count)
initial_keeper_count=$(keeper_count)

docker exec "$service_container" test ! -e \
  /usr/local/share/ca-certificates/candidate-core-capture.crt
docker exec "$service_container" test ! -e \
  /usr/local/share/ca-certificates/candidate-aux-capture.crt
(
  cd "$deployment_root"
  sha256sum -c deployment-evidence.sha256 >/dev/null
)

for name in SOURCE_FILE_SHA256SUMS SOURCE_TREE_SHA256; do
  install -m 0600 "$source_root/$name" "$backup_dir/$name"
done
for name in image.override.yml source-tree-sha256.txt deployment-evidence.sha256; do
  install -m 0600 "$deployment_root/$name" "$backup_dir/$name"
done

trap restore_on_failure EXIT
trap record_failure_step ERR
trap 'exit 130' INT
trap 'exit 143' TERM

# 生成冻结源码文件清单；元数据自身不参与，pycache/字节码/AppleDouble 直接拒绝。
freeze_step=source-manifest
manifest_candidate="$runtime_dir/SOURCE_FILE_SHA256SUMS.new"
tree_candidate="$runtime_dir/SOURCE_TREE_SHA256.new"
python3 - "$source_root" "$manifest_candidate" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

root = Path(sys.argv[1]).resolve()
output = Path(sys.argv[2])
excluded = {"GIT_COMMIT", "SOURCE_FILE_SHA256SUMS", "SOURCE_TREE_SHA256"}
entries = []
for path in root.rglob("*"):
    relative = path.relative_to(root)
    parts = relative.parts
    if (
        "__pycache__" in parts
        or "__MACOSX" in parts
        or ".AppleDouble" in parts
        or path.name.startswith("._")
        or path.name.startswith(".freeze-r11.")
        or path.suffix in {".pyc", ".pyo"}
    ):
        raise SystemExit(f"拒绝冻结缓存或 AppleDouble：{relative}")
    if path.is_symlink():
        raise SystemExit(f"拒绝冻结符号链接：{relative}")
    if not path.is_file() or str(relative) in excluded:
        continue
    relative_text = relative.as_posix()
    if any(char in relative_text for char in ("\n", "\r", "\\")):
        raise SystemExit(f"拒绝不可移植路径：{relative_text!r}")
    before = path.stat()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    after = path.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise SystemExit(f"源码在计算哈希期间发生变化：{relative_text}")
    entries.append((relative_text, digest))

entries.sort(key=lambda item: item[0].encode("utf-8"))
if not entries:
    raise SystemExit("冻结源码清单为空")
output.write_text(
    "".join(f"{digest}  {name}\n" for name, digest in entries),
    encoding="utf-8",
)
os.chmod(output, 0o600)
print(len(entries))
PY
(
  cd "$source_root"
  sha256sum -c "$manifest_candidate" >/dev/null
)
final_tree=$(sha256sum "$manifest_candidate" | awk '{print $1}')
[[ $final_tree =~ ^[0-9a-f]{64}$ ]]
printf '%s\n' "$final_tree" >"$tree_candidate"
chmod 0600 "$tree_candidate"

manifest_staged=$(stage_file "$manifest_candidate" "$source_root/SOURCE_FILE_SHA256SUMS")
tree_staged=$(stage_file "$tree_candidate" "$source_root/SOURCE_TREE_SHA256")
freeze_step=source-install
source_mutated=1
mv -f -- "$manifest_staged" "$source_root/SOURCE_FILE_SHA256SUMS"
mv -f -- "$tree_staged" "$source_root/SOURCE_TREE_SHA256"
(
  cd "$source_root"
  sha256sum -c SOURCE_FILE_SHA256SUMS >/dev/null
)
[[ $(tr -d '\r\n' <"$source_root/SOURCE_TREE_SHA256") == "$final_tree" ]]

tree_prefix=${final_tree:0:12}
normal_alias="sub2apiplus:codex0145-20260730T195700Z-${tree_prefix}-r11"
capture_alias="${normal_alias}-capture"
freeze_step=image-aliases

current_alias_id=$(docker image inspect -f '{{.Id}}' "$normal_alias" 2>/dev/null || true)
if [[ -n $current_alias_id && $current_alias_id != "$normal_image_id" ]]; then
  echo "normal 冻结别名已被其他镜像占用。" >&2
  exit 1
fi
if [[ -z $current_alias_id ]]; then
  docker image tag "$normal_image_id" "$normal_alias"
  normal_alias_created=1
fi
current_alias_id=$(docker image inspect -f '{{.Id}}' "$capture_alias" 2>/dev/null || true)
if [[ -n $current_alias_id && $current_alias_id != "$capture_image_id" ]]; then
  echo "capture 冻结别名已被其他镜像占用。" >&2
  exit 1
fi
if [[ -z $current_alias_id ]]; then
  docker image tag "$capture_image_id" "$capture_alias"
  capture_alias_created=1
fi
[[ $(docker image inspect -f '{{.Id}}' "$normal_alias") == "$normal_image_id" ]]
[[ $(docker image inspect -f '{{.Id}}' "$capture_alias") == "$capture_image_id" ]]

# 三份部署元数据先完整预生成，再在同目录逐个原子替换；失败由 EXIT 钩子回退。
freeze_step=deployment-metadata
override_candidate="$runtime_dir/image.override.yml.new"
deploy_tree_candidate="$runtime_dir/source-tree-sha256.txt.new"
evidence_candidate="$runtime_dir/deployment-evidence.sha256.new"
python3 - "$override_candidate" "$normal_alias" <<'PY'
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.write_text(
    "services:\n  sub2api:\n    image: {}\n".format(sys.argv[2]),
    encoding="utf-8",
)
os.chmod(path, 0o600)
PY
printf '%s\n' "$final_tree" >"$deploy_tree_candidate"
chmod 0600 "$deploy_tree_candidate"
python3 - "$deployment_root" "$override_candidate" "$deploy_tree_candidate" \
  "$evidence_candidate" <<'PY'
import hashlib
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
replacement = {
    "image.override.yml": Path(sys.argv[2]),
    "source-tree-sha256.txt": Path(sys.argv[3]),
}
output = Path(sys.argv[4])
names = sorted(
    path.name
    for path in root.iterdir()
    if path.is_file()
    and path.name != "deployment-evidence.sha256"
    and not path.name.startswith(".freeze-r11.")
)
lines = []
for name in names:
    source = replacement.get(name, root / name)
    lines.append(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  {name}\n")
output.write_text("".join(lines), encoding="utf-8")
os.chmod(output, 0o600)
PY

override_staged=$(stage_file "$override_candidate" "$deployment_root/image.override.yml")
deploy_tree_staged=$(stage_file "$deploy_tree_candidate" "$deployment_root/source-tree-sha256.txt")
evidence_staged=$(stage_file "$evidence_candidate" "$deployment_root/deployment-evidence.sha256")
deployment_mutated=1
mv -f -- "$override_staged" "$deployment_root/image.override.yml"
mv -f -- "$deploy_tree_staged" "$deployment_root/source-tree-sha256.txt"
mv -f -- "$evidence_staged" "$deployment_root/deployment-evidence.sha256"
(
  cd "$deployment_root"
  sha256sum -c deployment-evidence.sha256 >/dev/null
)
[[ $(tr -d '\r\n' <"$deployment_root/source-tree-sha256.txt") == "$final_tree" ]]
[[ $(compose_shape "$deployment_root/image.override.yml") == "$normal_alias|0" ]]

freeze_step=app-recreate
app_recreated=1
docker compose -f "$compose_file" -f "$deployment_root/image.override.yml" \
  up -d --no-deps --force-recreate sub2api \
  >"$runtime_dir/freeze-compose.log" 2>&1
wait_healthy

freeze_step=post-image
[[ $(docker inspect -f '{{.Image}}' "$service_container") == "$normal_image_id" ]]
[[ $(docker inspect -f '{{.Config.Image}}' "$service_container") == "$normal_alias" ]]
[[ $(attestation_env_count) == 0 ]]
docker exec "$service_container" test ! -e \
  /usr/local/share/ca-certificates/candidate-core-capture.crt
docker exec "$service_container" test ! -e \
  /usr/local/share/ca-certificates/candidate-aux-capture.crt
freeze_step=post-state
current_state_matches
freeze_step=post-source-manifest
(
  cd "$source_root"
  sha256sum -c SOURCE_FILE_SHA256SUMS >/dev/null
)
freeze_step=post-deployment-evidence
(
  cd "$deployment_root"
  sha256sum -c deployment-evidence.sha256 >/dev/null
)

freeze_step=write-result
python3 - "$runtime_dir/freeze-result.json" "$final_tree" "$normal_alias" \
  "$capture_alias" "$normal_image_id" "$capture_image_id" <<'PY'
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
payload = {
    "status": "complete",
    "source_tree_sha256": sys.argv[2],
    "normal_alias": sys.argv[3],
    "capture_alias": sys.argv[4],
    "normal_image_id": sys.argv[5],
    "capture_image_id": sys.argv[6],
    "attestation_env_absent": True,
    "data_containers_unchanged": True,
    "group9_account99_unchanged": True,
}
path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
os.chmod(path, 0o600)
PY

freeze_complete=1
