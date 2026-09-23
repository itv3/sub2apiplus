#!/bin/bash
# Kilo 双入口：在候选 attempt 完成后、seal 第一步前发送恰好两条真实请求，并从服务端记录组装 kilo-facts.json。
# 用法：bash vc5-kilo.sh <attempt_id>
set -Eeuo pipefail; umask 077
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
ATT="$1"
A="$NEWDIR/candidates/$CAND/attempts/$ATT"; EV="$A/evidence"; RAW="$EV/client/raw"; LOG="$D/control/$NEW-kilo-logs/run$(date -u +%Y%m%dt%H%M%Sz)"
# 已封存 attempt 不得再进入 Kilo（manifest 绑定 evidence/**）；kilo-facts 已存在也不得重发请求
if [ -e "$A/evidence-manifest.json" ]; then echo "KILO_ABORT: evidence-manifest.json 已存在，禁止再写 evidence/client"; exit 3; fi
if [ -f "$RAW/kilo-facts.json" ]; then echo "KILO_SKIP: kilo-facts.json 已存在"; exit 0; fi
# 老板 2026-09-22 拍板：Kilo 二进制取不可变审计副本（0500，root），账号请求发生前三项 fail-fast：路径存在、--version、SHA256 均须等于冻结值
K=$KILO_BIN
test -f "$K" || { echo "KILO_FAILFAST: 二进制不存在 $K"; exit 1; }
ACTUAL_SHA=$(sha256sum "$K" | cut -c1-64); [ "$ACTUAL_SHA" = "$KILO_SHA256" ] || { echo "KILO_FAILFAST: sha256 不等于冻结值 ($ACTUAL_SHA)"; exit 1; }
ACTUAL_VER=$(timeout 20 "$K" --version 2>/dev/null | tail -n 1 | tr -d "[:space:]"); [ "$ACTUAL_VER" = "$KILO_VERSION" ] || { echo "KILO_FAILFAST: --version 不等于冻结值 ($ACTUAL_VER)"; exit 1; }
echo "KILO_FAILFAST_PASS path=$K version=$ACTUAL_VER sha256=${ACTUAL_SHA:0:16}"
mkdir -p "$RAW" "$LOG"; chmod 700 "$LOG"
bash "$DRV/vc5-permission-closeout.sh" "$A" > /dev/null
eval "$(python3 - "$A/attempt.json" <<'PY'
import json, sys, shlex
d = json.load(open(sys.argv[1])); i = d["identity"]
for k, v in {"CID": d["campaign_id"], "RUN_NONCE": d["run_nonce"], "IMAGE_ID": i["image_id"], "TREE": i["source_tree_sha256"], "BUILD_ID": i["build_id"], "DEPLOYED": i["deployed_version"], "PROFILE_ID": i["profile_id"], "PROFILE_DIGEST": i["profile_digest"]}.items():
    print(f"{k}={shlex.quote(str(v))}")
PY
)"
cd "$COMPOSE_DIR" && set -a && . ./.env && set +a; cd /tmp
dbq() { docker exec sub2apiplus-postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qAtc "$1"; }
API_KEY=$(dbq "select key from api_keys where id = $API_KEY_ID and status = 'active' and deleted_at is null"); test -n "$API_KEY"
PRE_ID=$(dbq "select coalesce(max(id),0) from usage_logs"); PRE_AT=$(utc_now)
python3 -c "import json,sys; json.dump({'schema_version':'kilo-usage-checkpoint/v1','campaign_id':sys.argv[1],'attempt_id':sys.argv[2],'run_nonce':sys.argv[3],'usage_logs_max_id_before_kilo':int(sys.argv[4]),'observed_at_utc':sys.argv[5]}, open(sys.argv[6],'w'), ensure_ascii=False, indent=2)" "$CID" "$ATT" "$RUN_NONCE" "$PRE_ID" "$PRE_AT" "$RAW/pre-kilo-usage-checkpoint.json"; chmod 600 "$RAW/pre-kilo-usage-checkpoint.json"
cp /root/.config/kilo/kilo.jsonc /root/.config/kilo/kilo.jsonc.pre-kilo-run; trap 'cp /root/.config/kilo/kilo.jsonc.pre-kilo-run /root/.config/kilo/kilo.jsonc' EXIT
cat > /root/.config/kilo/kilo.jsonc <<EOF2
{
  "\$schema": "https://app.kilo.ai/config.json",
  "disabled_providers": [],
  "provider": {
    "sub2api-compat": {"npm": "@ai-sdk/openai-compatible", "name": "sub2api candidate compat", "options": {"baseURL": "http://127.0.0.1:28080/v1", "apiKey": "$API_KEY"}, "models": {"$LITE_MODEL": {"name": "$LITE_MODEL"}}},
    "sub2api-responses": {"npm": "@ai-sdk/openai", "name": "sub2api candidate responses", "options": {"baseURL": "http://127.0.0.1:28080/v1", "apiKey": "$API_KEY", "websocket": true}, "models": {"$LITE_MODEL": {"name": "$LITE_MODEL"}}}
  },
  "permission": {"bash": "allow"},
  "agent": {"orchestrator": {"disable": true, "hidden": true}}
}
EOF2
chmod 600 /root/.config/kilo/kilo.jsonc
echo "--- Kilo compat 请求"; set +e; timeout 240 "$K" run --pure --format json -m sub2api-compat/$LITE_MODEL "Reply with exactly one word: pong" > "$LOG/kilo-compatible.out" 2>&1; echo "rc=$?"; tail -c 300 "$LOG/kilo-compatible.out"; echo
echo "--- Kilo responses(WS) 请求"; timeout 240 "$K" run --pure --format json -m sub2api-responses/$LITE_MODEL "Reply with exactly one word: pong" > "$LOG/kilo-responses.out" 2>&1; echo "rc=$?"; tail -c 300 "$LOG/kilo-responses.out"; echo; set -e
cp /root/.config/kilo/kilo.jsonc.pre-kilo-run /root/.config/kilo/kilo.jsonc; trap - EXIT
echo "--- 服务端 usage_logs（api_key ${API_KEY_ID}，id > ${PRE_ID}）"; dbq "select id, request_id, model, account_id, created_at, duration_ms, coalesce(user_agent,''), coalesce(inbound_endpoint,''), coalesce(upstream_endpoint,''), openai_ws_mode from usage_logs where api_key_id = $API_KEY_ID and id > $PRE_ID order by id" > "$LOG/usage_rows.txt"; cat "$LOG/usage_rows.txt"
docker logs sub2apiplus --since "$PRE_AT" 2>&1 | grep '"component": "http.access"' | grep -E '"path": "/v1/(chat/completions|responses)"' > "$LOG/access_rows.txt" || true; wc -l < "$LOG/access_rows.txt"
KILO_VERSION="$KILO_VERSION" python3 - "$LOG/usage_rows.txt" "$LOG/access_rows.txt" "$RAW/kilo-facts.json" "$K" "$CID" "$ATT" "$RUN_NONCE" "$CAND" "$PROFILE_ID" "$PROFILE_DIGEST" "$IMAGE_ID" "$TREE" "$BUILD_ID" "$DEPLOYED" <<'PY'
import json, sys, hashlib, re, datetime, os
usage_path, access_path, out, kilo, cid, att, nonce, cand, pid, pdig, image, tree, build, deployed = sys.argv[1:]
rows = [l.split("|") for l in open(usage_path).read().splitlines() if l.strip()]
assert len(rows) == 2, f"期望恰好两条 usage_logs，实际 {len(rows)}"
access = {}
for line in open(access_path):
    j = json.loads(line[line.index("{"):])
    access[j.get("request_id")] = j
    # 候选 v8 网关对带客户端请求 ID 的 chat/completions 把 usage_logs.request_id 记为
    # client:<client_request_id>；按该键也建立索引，response_id 仍取服务端 request_id。
    if j.get("client_request_id"):
        access.setdefault("client:" + str(j["client_request_id"]), j)
def iso(ts):
    ts = ts.strip()
    if " " in ts and "T" not in ts: ts = ts.replace(" ", "T")
    if re.search(r"[+-]\d\d$", ts): ts += ":00"
    return ts
obs = {}
for r in rows:
    uid, rid, model, acct, created, dur, ua, inbound, upstream, ws = r
    a = access.get(rid)
    if a is None and ws == "t":
        # WebSocket 入口：usage_logs.request_id 记的是上游响应 ID（resp_…），与 http.access 的
        # 请求 ID 无直接关联；按同窗口内唯一一条 101 GET /v1/responses 关联。
        ws_rows = [j for j in access.values() if j.get("path") == "/v1/responses" and int(j.get("status_code", 0)) == 101]
        ws_rows = list({j["request_id"]: j for j in ws_rows}.values())
        assert len(ws_rows) == 1, f"WS 入口 101 记录数 {len(ws_rows)} != 1"
        a = ws_rows[0]
    assert a is not None, f"http.access 缺少 request_id={rid}"
    completed = a["completed_at"]; lat = int(a.get("latency_ms", 0))
    cdt = datetime.datetime.fromisoformat(iso(completed)); received = (cdt - datetime.timedelta(milliseconds=lat)).isoformat()
    client = "kilo-responses" if ws == "t" else "kilo-compatible"
    obs[client] = {
        "entrypoint": a["path"], "user_agent": ua, "model": model, "request_id": a.get("client_request_id") or rid, "response_id": (a.get("request_id") if rid.startswith("client:") else rid),
        "http_status": int(a["status_code"]), "received_at_utc": received, "completed_at_utc": cdt.isoformat(), "usage_id": uid,
        "oauth_account_id": int(acct), "recorded_at_utc": iso(created), "upstream_endpoint": upstream, "transport": "websocket" if ws == "t" else "http",
    }
assert set(obs) == {"kilo-compatible", "kilo-responses"}, list(obs)
assert obs["kilo-compatible"]["http_status"] == 200 and obs["kilo-responses"]["http_status"] == 101, {k: v["http_status"] for k, v in obs.items()}
sha = hashlib.sha256(open(kilo, "rb").read()).hexdigest()
facts = {
    "identity": {"campaign_id": cid, "attempt_id": att, "run_nonce": nonce, "candidate_id": cand, "target_version": os.environ["TARGET_VERSION"], "profile_id": pid, "profile_digest": pdig, "candidate_image_id": image, "source_tree_sha256": tree, "build_id": build, "deployed_version": deployed},
    # installation 观察时间取请求前的 usage checkpoint 时刻：finalizer 要求 installed_at <= ingress_at。
    "installation": {"executable_path": kilo, "executable_sha256": sha, "client_version": os.environ["KILO_VERSION"], "display_name": "Kilo Code", "observed_at_utc": json.load(open(out.replace("kilo-facts.json", "pre-kilo-usage-checkpoint.json")))["observed_at_utc"]},
    "observations": obs,
}
json.dump(facts, open(out, "w"), ensure_ascii=False, indent=2); print("kilo-facts.json 已写：", {k: (v["http_status"], v["transport"], v["user_agent"][:40]) for k, v in obs.items()})
PY
chmod 600 "$RAW/kilo-facts.json"; echo "KILO_DONE"
