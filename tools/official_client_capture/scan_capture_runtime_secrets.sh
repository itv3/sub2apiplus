#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

# 用 Vircs 当前真实凭据逐值扫描抓包目录，不把凭据写入日志或命令参数。
if [[ $# -lt 1 ]]; then
  echo "用法：$0 <抓包目录> [...]" >&2
  exit 2
fi

postgres_container=${POSTGRES_CONTAINER:-sub2apiplus-postgres}
secret_file=$(mktemp)
match_file=$(mktemp)
chmod 0600 "$secret_file" "$match_file"

cleanup() {
  rm -f "$secret_file" "$match_file"
}
trap cleanup EXIT INT TERM

docker exec "$postgres_container" psql -U sub2api -d sub2api -qAtc "
select key from api_keys where id = 1 and length(key) >= 16
union all
select credentials->>field_name
from accounts
cross join lateral (
  values ('access_token'), ('refresh_token'), ('id_token')
) as fields(field_name)
where id in (50, 90)
  and length(coalesce(credentials->>field_name, '')) >= 16
" >"$secret_file"

if [[ ! -s $secret_file ]]; then
  echo "没有取得可扫描的运行时凭据，拒绝给出通过结论。" >&2
  exit 3
fi

if grep -RIlF -f "$secret_file" -- "$@" >"$match_file"; then
  echo "运行时凭据值扫描失败，命中文件如下：" >&2
  sed -n '1,100p' "$match_file" >&2
  exit 1
fi

echo "运行时 API Key、access_token、refresh_token、id_token 精确值扫描通过。"
