#!/usr/bin/env bash
set -Eeuo pipefail
set +x
umask 077

# 采集正式验收前后都可重算的生产归一化状态。时间戳单独保存，绝不进入
# 状态 JSON；因此恢复完整时 before.json 与 after.json 必须字节一致。

mode=${MODE:?必须提供 MODE=before 或 MODE=after}
assessment_id=${ASSESSMENT_ID:?必须提供 ASSESSMENT_ID}
evidence_root=${EVIDENCE_ROOT:?必须提供 EVIDENCE_ROOT}
[[ $mode == before || $mode == after ]]
[[ $assessment_id =~ ^[A-Za-z0-9._-]+$ ]]
[[ $evidence_root == /root/oauth-capture/evidence/$assessment_id ]]

source_root=/root/Docker/sub2apiplus/builds/codex0145-20260730T195700Z-r11/source
deployment_root=/root/Docker/sub2apiplus/deployments/codex0145-20260730T195700Z-r11
runtime_root=/root/oauth-capture/runtime/formal-config-$assessment_id
restoration_root=$evidence_root/restoration
state_path=$restoration_root/$mode.json
captured_path=$restoration_root/$mode-captured-at.txt
identity_path=$runtime_root/candidate-identity.json

service=sub2apiplus
postgres=sub2apiplus-postgres
redis=sub2apiplus-redis
keeper=sub2apiplus-keeper
expected_tree=39e579acb066d0daaf036dfd23ae2c61c5cc99137bc9a0eb9af0bcf073363a5b
expected_ref=sub2apiplus:codex0145-20260730T195700Z-39e579acb066-r11
expected_image=sha256:a0228995f1879f345a59cc9836770d3a276618b69ed51089f1b6a707f4d5ee14
expected_version=0.1.165-codex0145-20260730T195700Z-r11

if [[ $mode == before ]]; then
  [[ ! -e $evidence_root && ! -e $runtime_root ]]
  install -d -m 0700 "$evidence_root" "$restoration_root" "$runtime_root"
else
  [[ -d $evidence_root && -d $restoration_root && -d $runtime_root ]]
  [[ -s $restoration_root/before.json && -s $restoration_root/before-captured-at.txt ]]
  [[ -d $evidence_root/assertions ]]
fi
[[ ! -e $state_path && ! -e $captured_path ]]

db_user=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres" |
  sed -n 's/^POSTGRES_USER=//p')
db_name=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$postgres" |
  sed -n 's/^POSTGRES_DB=//p')
[[ -n $db_user && -n $db_name ]]
db_query() {
  docker exec "$postgres" psql -v ON_ERROR_STOP=1 -U "$db_user" -d "$db_name" -qAtc "$1"
}
mount_hash() {
  docker inspect -f '{{json .Mounts}}' "$1" | sha256sum | awk '{print $1}'
}

app_image=$(docker inspect -f '{{.Image}}' "$service")
app_ref=$(docker inspect -f '{{.Config.Image}}' "$service")
app_health=$(docker inspect -f \
  '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$service")
attestation_count=$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$service" |
  sed -n '/^SUB2API_LIVE_ATTESTATION_CAPTURE_/p' | wc -l | tr -d ' ')
custom_ca_absent=false
if docker exec "$service" test ! -e \
    /usr/local/share/ca-certificates/candidate-core-capture.crt &&
  docker exec "$service" test ! -e \
    /usr/local/share/ca-certificates/candidate-aux-capture.crt; then
  custom_ca_absent=true
fi
chatgpt_hosts_override_absent=false
if ! docker exec "$service" grep -Eq \
  '(^|[[:space:]])chatgpt\.com([[:space:]]|$)' /etc/hosts; then
  chatgpt_hosts_override_absent=true
fi

git_commit=$(tr -d '\r\n' <"$source_root/GIT_COMMIT")
source_tree=$(tr -d '\r\n' <"$source_root/SOURCE_TREE_SHA256")
deployed_version=$(tr -d '\r\n' <"$deployment_root/version.txt")
postgres_id=$(docker inspect -f '{{.Id}}' "$postgres")
redis_id=$(docker inspect -f '{{.Id}}' "$redis")
keeper_id=$(docker inspect -f '{{.Id}}' "$keeper")
postgres_mounts=$(mount_hash "$postgres")
redis_mounts=$(mount_hash "$redis")
keeper_mounts=$(mount_hash "$keeper")
entity_counts=$(db_query "
select (select count(*) from users) || '|' ||
       (select count(*) from accounts) || '|' ||
       (select count(*) from groups) || '|' ||
       (select count(*) from api_keys) || '|' ||
       (select count(*) from account_groups) || '|' ||
       (select count(*) from proxies)")
group_state=$(db_query "
select platform || '|' || require_oauth_only::text || '|' || allow_live::text || '|' ||
       allow_image_generation::text from groups where id=9")
account50_state=$(db_query "
select platform || '|' || type || '|' || status || '|' || schedulable::text || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id=50")
account99_state=$(db_query "
select platform || '|' || type || '|' || status || '|' || schedulable::text || '|' ||
       coalesce(parent_account_id::text,'NULL') || '|' ||
       coalesce(proxy_id::text,'NULL') || '|' ||
       coalesce(proxy_fallback_origin_id::text,'NULL') from accounts where id=99")
account99_extra_sha=$(db_query "
select encode(sha256(convert_to(extra::text,'UTF8')),'hex') from accounts where id=99")
model_mapping_sha=$(db_query "
select case when credentials ? 'model_mapping'
  then encode(sha256(convert_to((credentials->'model_mapping')::text,'UTF8')),'hex')
  else 'absent' end from accounts where id=99")
api_key_state=$(db_query "
select id::text || '|' || group_id::text || '|' || status from api_keys
where id=15 and deleted_at is null")
auth_digest=$(db_query "
select encode(sha256(convert_to(key,'UTF8')),'hex') from api_keys
where id=15 and group_id=9 and status='active' and deleted_at is null")
[[ $auth_digest =~ ^[0-9a-f]{64}$ ]]
auth_cache_exists=$(printf 'apikey:auth:%s' "$auth_digest" |
  docker exec -i "$redis" redis-cli --raw -x EXISTS | tr -d '\r')
redis_ping=$(docker exec "$redis" redis-cli PING | tr -d '\r')
keeper_running=$(docker inspect -f '{{.State.Running}}' "$keeper")
port_bindings_sha=$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$service" |
  sha256sum | awk '{print $1}')

[[ $git_commit =~ ^[0-9a-f]{40}$ ]]
[[ $source_tree == "$expected_tree" && $app_image == "$expected_image" ]]
[[ $app_ref == "$expected_ref" && $deployed_version == "$expected_version" ]]
[[ $app_health == healthy && $attestation_count == 0 ]]
[[ $custom_ca_absent == true && $chatgpt_hosts_override_absent == true ]]
[[ $group_state == 'composite|false|true|false' ]]
[[ $account50_state == 'anthropic|oauth|error|false|NULL|NULL' ]]
[[ $account99_state == 'openai|oauth|active|true|NULL|NULL|NULL' ]]
[[ $api_key_state == '15|9|active' && $auth_cache_exists == 0 ]]
[[ $redis_ping == PONG && $keeper_running == true ]]
(
  cd "$source_root"
  sha256sum -c SOURCE_FILE_SHA256SUMS >/dev/null
)
(
  cd "$deployment_root"
  sha256sum -c deployment-evidence.sha256 >/dev/null
)

state_tmp=$(mktemp "$restoration_root/.${mode}.json.XXXXXX")
python3 - "$state_tmp" "$git_commit" "$source_tree" "$app_ref" "$app_image" \
  "$deployed_version" "$postgres_id" "$postgres_mounts" "$redis_id" \
  "$redis_mounts" "$keeper_id" "$keeper_mounts" "$entity_counts" \
  "$group_state" "$account50_state" "$account99_state" "$account99_extra_sha" \
  "$model_mapping_sha" "$api_key_state" "$app_health" "$custom_ca_absent" \
  "$chatgpt_hosts_override_absent" "$redis_ping" "$keeper_running" \
  "$port_bindings_sha" <<'PY'
import json
import os
import sys
from pathlib import Path

(
    path, git_commit, source_tree, image_reference, image_digest,
    deployed_version, postgres_id, postgres_mounts, redis_id, redis_mounts,
    keeper_id, keeper_mounts, entity_counts, group_state, account50_state,
    account99_state, account99_extra_sha, model_mapping_sha, api_key_state,
    app_health, custom_ca_absent, hosts_absent, redis_ping, keeper_running,
    port_bindings_sha,
) = sys.argv[1:]
payload = {
    "schema_version": "codex-candidate-normalized-state/v1",
    "candidate": {
        "git_commit": git_commit,
        "source_tree_sha256": source_tree,
        "image_reference": image_reference,
        "image_digest": image_digest,
        "deployed_version": deployed_version,
    },
    "runtime": {
        "health": app_health,
        "attestation_environment_count": 0,
        "custom_capture_ca_absent": custom_ca_absent == "true",
        "chatgpt_hosts_override_absent": hosts_absent == "true",
        "port_bindings_sha256": port_bindings_sha,
    },
    "persistent_services": {
        "postgres": {"container_id": postgres_id, "mounts_sha256": postgres_mounts},
        "redis": {
            "container_id": redis_id,
            "mounts_sha256": redis_mounts,
            "ping": redis_ping,
        },
        "keeper": {
            "container_id": keeper_id,
            "mounts_sha256": keeper_mounts,
            "running": keeper_running == "true",
        },
    },
    "database": {
        "entity_counts": entity_counts,
        "group_9": group_state,
        "account_50": account50_state,
        "account_99": account99_state,
        "account_99_extra_sha256": account99_extra_sha,
        "account_99_model_mapping_sha256": model_mapping_sha,
        "api_key_15": api_key_state,
        "api_key_15_auth_cache_absent": True,
    },
}
Path(path).write_text(
    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
os.chmod(path, 0o600)
PY
mv -- "$state_tmp" "$state_path"
captured_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf '%s\n' "$captured_at" >"$captured_path"
chmod 0600 "$captured_path"

if [[ $mode == before ]]; then
  identity_tmp=$(mktemp "$runtime_root/.candidate-identity.json.XXXXXX")
  python3 - "$identity_tmp" "$git_commit" "$source_tree" "$app_ref" \
    "$app_image" "$deployed_version" <<'PY'
import json
import os
import sys
from pathlib import Path

path, git_commit, source_tree, image_reference, image_digest, deployed_version = sys.argv[1:]
payload = {
    "git_commit": git_commit,
    "source_tree_sha256": source_tree,
    "image_reference": image_reference,
    "image_digest": image_digest,
    "deployed_version": deployed_version,
}
Path(path).write_text(
    json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n",
    encoding="utf-8",
)
os.chmod(path, 0o600)
PY
  mv -- "$identity_tmp" "$identity_path"
else
  cmp -s "$restoration_root/before.json" "$state_path"
fi

printf 'mode=%s assessment=%s captured_at=%s state_sha256=%s\n' \
  "$mode" "$assessment_id" "$captured_at" "$(sha256sum "$state_path" | awk '{print $1}')"
