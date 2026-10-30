#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""在套件自己的一次性测试库里直接用 SQL 造测试数据：1 个测试用户、2 个 openai 分组、2 把 Key、
1 个 OpenAI OAuth 账号（官方出站链路，ctx_pool WebSocket）+ 1 个 OpenAI API Key 账号（base_url 指向假上游）。

为什么不走管理 API：新库的管理 API 需要先确认“管理员合规声明”（法务条款确认），这不是测试脚本该替人点的步骤，
所以绕开管理 API，只往套件自己的一次性测试库放数据。账号显式写 account_groups 绑定，建完核对只绑在预期的测试分组。
写完后需要重启网关让调度缓存重建（由 run.sh 做）。

API Key 账号（k1）额外设为池模式并打开健康熔断（阈值 1 次失败即熔断），这样任何被误判为“账号故障”的上报都会立刻
在账号行或 Redis 里留下痕迹，供场景 S8 发现。凭据全是本次随机生成的假值，只存在于工作目录的 state.json（0600），拆栈时删除。"""
import base64
import json
import secrets
import sys
import uuid

import lib

NAME_PREFIX = "bhvacc-"
MODEL = "gpt-6.1-sol"


def fake_jwt(sub: str, account_id: str) -> str:
    def b(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()
    claims = {"iss": "https://auth.openai.com", "sub": sub, "exp": 1893456000, "aud": ["https://api.openai.com/v1"],
              "https://api.openai.com/auth": {"chatgpt_account_id": account_id, "chatgpt_plan_type": "pro", "chatgpt_user_id": sub}}
    return b({"alg": "none", "typ": "JWT"}) + "." + b(claims) + "." + "sig"


def main():
    sql, lit = lib.sql, lib.lit
    st = {"run": secrets.token_hex(3), "groups": {}, "keys": {}, "accounts": {}}
    email = "bhvacc@acc-pool.invalid"
    uid = int(sql("insert into users(email, password_hash, role, balance, concurrency, status, username, notes, rpm_limit) "
                  "values (%s, '!bhvacc-no-login', 'user', 1000, 50, 'active', 'bhvacc', '升级前行为验收套件专用，拆栈时随库销毁', 100000) returning id" % lit(email)).split()[0])
    st["user"] = {"id": uid, "email": email}
    for g in ("g1", "g3"):
        gid = int(sql("insert into groups(name, description, platform, rate_multiplier, subscription_type, is_exclusive, status) "
                      "values (%s, '行为验收', 'openai', 1, 'standard', false, 'active') returning id" % lit(NAME_PREFIX + g)).split()[0])
        st["groups"][g] = gid
        k = "sk-" + secrets.token_hex(24)
        rid = int(sql("insert into api_keys(user_id, key, name, group_id, status) values (%d, %s, %s, %d, 'active') returning id"
                      % (uid, lit(k), lit(NAME_PREFIX + "key-" + g), gid)).split()[0])
        st["keys"][g] = {"id": rid, "key": k}
    lib.save_state(st)
    specs = [("o1", "g1", "oauth"), ("k1", "g3", "apikey")]
    for key, grp, typ in specs:
        if typ == "oauth":
            acct_id = str(uuid.uuid4())
            cred = {"access_token": fake_jwt("user-" + key, acct_id), "expires_at": "2030-01-01T00:00:00Z", "chatgpt_account_id": acct_id,
                    "chatgpt_user_id": "user-" + key, "organization_id": "org-" + key, "plan_type": "pro", "email": key + "@wsrec.invalid",
                    "model_mapping": {MODEL: MODEL}}
            extra = {"openai_oauth_responses_websockets_v2_mode": "ctx_pool", "openai_oauth_responses_websockets_v2_enabled": True}
        else:
            acct_id = None
            cred = {"api_key": "sk-bhvacc-fake-" + secrets.token_hex(8), "base_url": "https://api.wsrec.test",
                    "model_mapping": {MODEL: MODEL}, "pool_mode": True}
            extra = {"openai_apikey_responses_websockets_v2_mode": "ctx_pool", "openai_apikey_responses_websockets_v2_enabled": True}
        # 账号并发给足（20），避免场景 S7 的并发会话被账号并发槽位而不是被内存准入卡住
        aid = int(sql("insert into accounts(name, platform, type, credentials, extra, concurrency, priority, status, schedulable) "
                      "values (%s, 'openai', %s, %s::jsonb, %s::jsonb, 20, 1, 'active', true) returning id"
                      % (lit(NAME_PREFIX + key), lit(typ), lit(json.dumps(cred)), lit(json.dumps(extra)))).split()[0])
        sql("insert into account_groups(account_id, group_id) values (%d, %d)" % (aid, st["groups"][grp]))
        bound = [int(x) for x in sql("select coalesce(string_agg(group_id::text, ','), '') from account_groups where account_id=%d" % aid).split(",") if x]
        if bound != [st["groups"][grp]]:
            sys.exit("账号 %s 分组绑定=%s 与预期 %s 不符" % (key, bound, st["groups"][grp]))
        st["accounts"][key] = {"id": aid, "group": grp, "type": typ, "chatgpt_account_id": acct_id}
        print("账号 %s id=%d type=%s group=%s 绑定=%s" % (key, aid, typ, grp, bound))
    # API Key 账号的健康熔断：阈值 1 次失败即熔断、窗口 10 分钟
    breaker = json.dumps({"enabled": True, "window_minutes": 10, "failure_threshold": 1, "cooldown_minutes": 30})
    sql("insert into settings(key, value, updated_at) values ('openai_apikey_health_breaker_settings', %s, now()) "
        "on conflict (key) do update set value=excluded.value" % lit(breaker))
    lib.save_state(st)
    print("用户 id=%d 分组=%s 健康熔断=%s" % (uid, st["groups"], sql("select value from settings where key='openai_apikey_health_breaker_settings'")))
    print(sql("select id,name,platform,type,status,schedulable from accounts where deleted_at is null order by id"))


if __name__ == "__main__":
    main()
