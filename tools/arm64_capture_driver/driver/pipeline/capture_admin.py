#!/usr/bin/env python3
"""采集账号开关，以及采集专用测试用户／API Key 的建立（采集主机，随驱动安装在 pipeline/capture_admin.py）。

背景：升级用的采集账号平时关闭、要用的时候打开；采集专用的网关 Key 挂在配置的分组，属于一个专用测试用户，不用真实用户的 Key。
账号号、名称、分组、用户邮箱全部来自环境（pipeline.env 或本轮参数文件），脚本里不写死：
  CAPTURE_ACCOUNT_ID    必填（缺省取本轮参数文件的 CLIENT_ACCOUNT_ID／CODEX_ACCOUNT_ID，由 capture-account-on／off.sh 补）
  CAPTURE_ACCOUNT_NAME  可选；给了就要求账号名称相等（防止号对但换了账号）
  CAPTURE_GROUP_ID      provision 时 Key 挂的分组（缺省 3）
  CAPTURE_USER_EMAIL    provision 的专用测试用户邮箱
  CAPTURE_KEY_NAME      provision 的专用 Key 名称
  PREP                  本轮流水线工作目录（日志、锁、身份文件放这里）

命令：
  account on          把采集账号设为 status=active、schedulable=true（幂等）
  account off         把采集账号恢复成 status=inactive、schedulable=false（幂等）
  provision           建专用测试用户与只挂指定分组的 API Key（幂等：已有就沿用），把用户 ID／Key ID（不含密钥）写进 capture-identity.txt

安全约束（写死在代码里，不靠调用者自觉）：
  * 开关只改该账号的 status 与 schedulable 两个字段：走管理接口（PUT /accounts/<id> 只带 status；POST /accounts/<id>/schedulable），
    这样调度器快照经 outbox 同步刷新，不会出现“库里改了、调度器还不知道”；动手前核对账号 ID／平台／类型（和可选的名称），对不上就拒绝；
    前后各算一次“该账号除 status／schedulable／updated_at／last_used_at／extra（网关运行态）之外的整行摘要（含凭据）”与“其他账号 status+schedulable 摘要”，不一致写进日志告警。
  * 打开前核对访问令牌离过期还有 60 分钟以上：刷新服务只处理 active 账号、只在过期前 30 分钟刷新，打开一个快过期的账号会触发
    刷新并轮换刷新令牌，让别处同一登录的那份令牌失效；不够 60 分钟拒绝打开（CAPTURE_FORCE_ON=1 可强制，不建议）。
  * 管理接口要 x-api-key：settings 里没有 admin_api_key 时临时写一个，用完（含异常）立刻删除；已有的沿用且绝不删。
  * 每次执行都先把“当前状态、将要改什么、时间”打印并写进 ${PREP}/logs/capture-account.log，再动手，动完再写结果。
  * 不碰别的账号，不改该账号的任何别的字段，不打印任何密钥。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone

def _required_int(name: str) -> int:
    value = os.environ.get(name, "")
    if not value.isdigit() or int(value) <= 0:
        raise SystemExit(f"缺少或非法的环境变量 {name}（要正整数）；账号号不写死在脚本里，请在 pipeline.env 或本轮参数文件里配置")
    return int(value)


PREP = os.environ.get("PREP") or ""
if not PREP:
    raise SystemExit("缺少环境变量 PREP（本轮流水线工作目录）；请经 capture-account-on.sh／off.sh 调用")
ENV_FILE = os.environ.get("PIPE_COMPOSE_ENV", "/root/docker/sub2apiplus/app/.env")
GATEWAY = os.environ.get("PIPE_ADMIN_GATEWAY", "http://127.0.0.1:28080")
LOG = PREP + "/logs/capture-account.log"
LOCK = PREP + "/capture-account.lock"
IDENTITY = PREP + "/capture-identity.txt"

ACCOUNT_ID = _required_int("CAPTURE_ACCOUNT_ID") if len(sys.argv) > 1 and sys.argv[1] in ("account", "provision") else 0
ACCOUNT_NAME = os.environ.get("CAPTURE_ACCOUNT_NAME", "")   # 可选；给了就要求名称相等
GROUP_ID = int(os.environ.get("CAPTURE_GROUP_ID", "3"))     # 缺省 3（openai-default）
USER_EMAIL = os.environ.get("CAPTURE_USER_EMAIL", "capture-user@entry.invalid")
KEY_NAME = os.environ.get("CAPTURE_KEY_NAME", "capture-key")
MIN_TOKEN_MINUTES = 60                         # 打开前访问令牌至少还剩这么久（刷新服务在过期前 30 分钟才会动它）
SETTLE_SECONDS = 3                             # 等调度器快照（outbox 约 1 秒）


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def say(message: str) -> None:
    """打印并追加到日志。"""
    line = f"{now_utc()} {message}"
    print(line, flush=True)
    os.makedirs(os.path.dirname(LOG), exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    os.chmod(LOG, 0o600)


def read_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw or raw.startswith("#") or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def sql(statement: str) -> str:
    """经 docker exec 对 sub2apiplus 库执行一条 SQL（语句走标准输入，不进进程命令行）。返回标准输出去掉首尾空白。"""
    env = read_env_file(ENV_FILE)
    result = subprocess.run(
        ["docker", "exec", "-i", "sub2apiplus-postgres", "psql", "-U", env["POSTGRES_USER"], "-d", env["POSTGRES_DB"],
         "-qAt", "-v", "ON_ERROR_STOP=1"],
        input=statement, capture_output=True, text=True, timeout=60, check=False)
    if result.returncode != 0:
        raise RuntimeError("SQL 失败：" + result.stderr.strip()[:300])
    return result.stdout.strip()


def literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


@contextlib.contextmanager
def admin_key():
    """管理接口用的 x-api-key：库里已有就沿用（绝不删），没有就临时写一个，用完（含异常）立刻删。"""
    current = sql("select value from settings where key = 'admin_api_key'")
    if current:
        yield current
        return
    key = "admin-" + secrets.token_hex(32)
    sql("insert into settings(key, value, updated_at) values ('admin_api_key', %s, now())" % literal(key))
    say("已临时写入 admin_api_key（用完立即删除）")
    try:
        yield key
    finally:
        sql("delete from settings where key = 'admin_api_key' and value = %s" % literal(key))
        say("已删除临时 admin_api_key")


def admin_call(key: str, method: str, path: str, body: dict | None = None) -> tuple[int, dict | None, str]:
    headers = {"x-api-key": key, "Content-Type": "application/json"}
    if method in ("POST", "PUT", "PATCH", "DELETE"):
        headers["Idempotency-Key"] = str(uuid.uuid4())
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(GATEWAY + "/api/v1/admin" + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            status, text = response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as error:
        status, text = error.code, error.read().decode("utf-8", "replace")
    try:
        parsed = json.loads(text)
    except ValueError:
        parsed = None
    return status, parsed, text


@contextlib.contextmanager
def exclusive_lock():
    os.makedirs(PREP, exist_ok=True)
    handle = open(LOCK, "w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        print("另一个实例正在执行（持有 " + LOCK + "），退出")
        raise SystemExit(3)
    try:
        yield
    finally:
        fcntl.flock(handle, fcntl.LOCK_UN)
        handle.close()


# ------------------------------------------------------------------------------------------------
# 采集账号开关
# ------------------------------------------------------------------------------------------------
def account_state() -> dict:
    row = sql("select row_to_json(t) from (select id, name, platform, type, status, schedulable, (deleted_at is not null) as deleted, "
              "credentials->>'expires_at' as token_expires_at, (credentials ? 'refresh_token') as has_refresh "
              "from accounts where id = %d) t" % ACCOUNT_ID)
    return json.loads(row) if row else {}


def fingerprints() -> tuple[str, str]:
    """（该账号除 status／schedulable／updated_at 与网关运行态（last_used_at、extra 里的用量快照）之外的整行摘要——含凭据，
    所以令牌被动过一定会告警；其他账号 id+status+schedulable+deleted 摘要）。运行态字段在请求结束后异步落库，
    10-27 实测不排除它们会让“关回去”紧跟在请求之后时误报。"""
    own = sql("select md5((to_jsonb(a) - 'status' - 'schedulable' - 'updated_at' - 'last_used_at' - 'extra')::text) from accounts a where id = %d" % ACCOUNT_ID)
    others = sql("select md5(coalesce(string_agg(id || ':' || status || ':' || schedulable::text || ':' || (deleted_at is not null)::text, ',' "
                 "order by id), '')) from accounts where id <> %d" % ACCOUNT_ID)
    return own, others


def minutes_to_expiry(state: dict) -> float | None:
    text = state.get("token_expires_at")
    if not text:
        return None
    try:
        return (datetime.fromisoformat(text) - datetime.now(timezone.utc)).total_seconds() / 60
    except ValueError:
        return None


def account_switch(action: str) -> int:
    want_active = action == "on"
    target = {"status": "active" if want_active else "inactive", "schedulable": want_active}
    before = account_state()
    # 先核对身份：账号 ID／名称／平台／类型对不上就拒绝，绝不动别的账号
    if (not before or before.get("id") != ACCOUNT_ID or (ACCOUNT_NAME and before.get("name") != ACCOUNT_NAME) or before.get("platform") != "openai"
            or before.get("type") != "oauth" or before.get("deleted")):
        say(f"account {action}：拒绝——账号 {ACCOUNT_ID} 不是预期的账号（{json.dumps(before, ensure_ascii=False)}）")
        return 2
    left = minutes_to_expiry(before)
    say(f"account {action}：当前状态 status={before['status']} schedulable={before['schedulable']}；"
        f"目标 status={target['status']} schedulable={target['schedulable']}；访问令牌到期 {before.get('token_expires_at')}"
        f"（剩 {int(left) if left is not None else '?'} 分钟），有刷新令牌={before.get('has_refresh')}")
    plan = []
    if before["status"] != target["status"]:
        plan.append(f"status {before['status']} -> {target['status']}")
    if before["schedulable"] != target["schedulable"]:
        plan.append(f"schedulable {before['schedulable']} -> {target['schedulable']}")
    if not plan:
        say(f"account {action}：已经是目标状态，无需修改（幂等）")
        return 0
    if want_active and (left is None or left < MIN_TOKEN_MINUTES) and os.environ.get("CAPTURE_FORCE_ON") != "1":
        say(f"account on：拒绝——访问令牌剩余不足 {MIN_TOKEN_MINUTES} 分钟或读不出到期时间，打开后刷新服务会刷新并轮换刷新令牌，"
            f"让别处同一登录的令牌失效；确需打开请设 CAPTURE_FORCE_ON=1（不建议）")
        return 4
    say(f"account {action}：将修改 " + "；".join(plan) + "（只改这两个字段）")
    own_before, others_before = fingerprints()
    with admin_key() as key:
        # 打开：先 status 再 schedulable；关闭：先 schedulable 再 status，保证中间态不会“可调度却已停用”之外的怪状态
        steps = ([("status", "PUT", "/accounts/%d" % ACCOUNT_ID, {"status": target["status"]}),
                  ("schedulable", "POST", "/accounts/%d/schedulable" % ACCOUNT_ID, {"schedulable": target["schedulable"]})]
                 if want_active else
                 [("schedulable", "POST", "/accounts/%d/schedulable" % ACCOUNT_ID, {"schedulable": target["schedulable"]}),
                  ("status", "PUT", "/accounts/%d" % ACCOUNT_ID, {"status": target["status"]})])
        for field, method, path, body in steps:
            if field == "status" and before["status"] == target["status"]:
                continue
            if field == "schedulable" and before["schedulable"] == target["schedulable"]:
                continue
            code, _parsed, text = admin_call(key, method, path, body)
            if not 200 <= code < 300:
                say(f"account {action}：管理接口 {method} {path} 失败 http={code} {text[:200]}")
                return 5
            say(f"account {action}：已调用 {method} {path} {json.dumps(body)} -> http {code}")
    time.sleep(SETTLE_SECONDS)
    after = account_state()
    own_after, others_after = fingerprints()
    if own_before != own_after:
        say("account 告警：该账号除 status／schedulable／updated_at／last_used_at／extra 之外的字段（含凭据）在这次操作期间有变化，请核对")
    if others_before != others_after:
        say("account 告警：其他账号的 status／schedulable 在这次操作期间有变化（不是本脚本改的，请核对）")
    ok = after.get("status") == target["status"] and after.get("schedulable") == target["schedulable"]
    say(f"account {action}：完成后状态 status={after.get('status')} schedulable={after.get('schedulable')} -> {'成功' if ok else '失败'}")
    return 0 if ok else 6


# ------------------------------------------------------------------------------------------------
# 采集专用测试用户与 Key
# ------------------------------------------------------------------------------------------------
def provision() -> int:
    user = sql("select id from users where email = %s and deleted_at is null" % literal(USER_EMAIL))
    if user:
        say(f"provision：专用测试用户已存在 id={user}，沿用")
    else:
        password = secrets.token_urlsafe(24)      # 随机密码，不记录；这个用户只通过测试 Key 使用，不登录
        with admin_key() as key:
            code, parsed, text = admin_call(key, "POST", "/users", {
                "email": USER_EMAIL, "password": password, "username": "capture-user",
                "notes": "升级采集专用测试用户（升级结束后可删）", "balance": 1000, "concurrency": 20, "rpm_limit": 100000})
        if not 200 <= code < 300:
            say(f"provision：管理接口建用户失败 http={code} {text[:200]}")
            return 5
        user = str(int((parsed.get("data") or {}).get("id")))
        say(f"provision：已建专用测试用户 id={user}（经管理接口）")
    # 兜底：余额／并发／RPM 上限确为期望值，只改自己建的这个用户
    sql("update users set balance = 1000, concurrency = 20, rpm_limit = 100000 where id = %s and email = %s" % (user, literal(USER_EMAIL)))
    key_id = sql("select id from api_keys where user_id = %s and name = %s and deleted_at is null" % (user, literal(KEY_NAME)))
    if key_id:
        say(f"provision：采集专用 Key 已存在 id={key_id}，沿用")
    else:
        secret = "sk-" + secrets.token_hex(24)     # 与现有格式一致：sk- + 48 位
        key_id = sql("insert into api_keys(user_id, key, name, group_id, status) values (%s, %s, %s, %d, 'active') returning id"
                     % (user, literal(secret), literal(KEY_NAME), GROUP_ID))
        say(f"provision：已建采集专用 Key id={key_id}（只挂分组 {GROUP_ID}，密钥不打印）")
    group = sql("select group_id from api_keys where id = %s" % key_id)
    if group != str(GROUP_ID):
        say(f"provision：Key {key_id} 的分组是 {group}，不是 {GROUP_ID}，异常")
        return 6
    with open(IDENTITY, "w", encoding="utf-8") as handle:
        handle.write(f"# 采集专用用户与 Key（不含密钥）；{now_utc()}\nuser_id={user}\nuser_email={USER_EMAIL}\napi_key_id={key_id}\n"
                     f"api_key_name={KEY_NAME}\ngroup_id={GROUP_ID}\naccount_id={ACCOUNT_ID}\n")
    os.chmod(IDENTITY, 0o600)
    say(f"provision：完成 user_id={user} api_key_id={key_id} group_id={GROUP_ID}")
    return 0


def main(argv: list[str]) -> int:
    sys.dont_write_bytecode = True
    if len(argv) == 3 and argv[1] == "account" and argv[2] in ("on", "off"):
        with exclusive_lock():
            return account_switch(argv[2])
    if len(argv) == 2 and argv[1] == "provision":
        with exclusive_lock():
            return provision()
    print(__doc__.split("\n\n")[0])
    print("用法：capture_admin.py account on|off | provision")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
