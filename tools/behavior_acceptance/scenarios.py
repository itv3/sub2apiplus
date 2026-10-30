#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""升级前行为验收套件：场景表与场景实现。

背景：v0.2.18-3／4／5 三次修同一个“跨模型旧会话 WebSocket 断流”，每次验收都漏了一块：假上游在 error 后带了终态、
测试绕过 handler 的内存准入、没有大历史、没压共享预算、真实上游只发 error 不发终态也不断连。本套件把这些盲区
固化成 8 个场景，经由真实网关容器 + 假上游（冒充 chatgpt.com）跑完整条 handler 链路。

用法（通常由 run.sh 调用，环境变量 BHV_* 由它设置）：
    python3 scenarios.py [--expect pass|accident] [--only S1,S3] [--out DIR] [--skip-log-checks]
    python3 scenarios.py --print-table      # 打印场景表（README 里的表由它生成，单元测试核对二者一致）

两种模式：
  --expect pass      默认。镜像应当通过所有场景（目标镜像 v0.2.18-5 及以后）。
  --expect accident  对事故版本（v0.2.18-4）复现：要求标记为“必现”的场景里出现事故特征。

退出码：0 达到预期；1 有场景未达预期；3 栈或冒烟不健康（不是被测镜像的问题）。
"""
import argparse
import json
import os
import sys
import threading
import time
from collections import namedtuple

import lib
from lib import (MIB, Recorder, add_reject, build_history, count_memory_rejected, count_proxy_failed, drive_session,
                 expected_after_recovery, frame, frame_bytes, lineage, load_state, new_session_id, received_view, set_ctl)

# 场景表。字段：
#   id        S1..S8
#   key       短名（日志与结果文件用）
#   title     场景名
#   budget    网关启动时的请求内存预算档：default（现网同款 256MiB）| tight（64MiB，只够一个大历史连接）
#   criteria  通过判据（一句话，README 场景表原文）
#   accident  事故特征：对 v0.2.18-4 应当看到什么
#   required  事故必现：--expect accident 时必须看到事故特征的场景
Scenario = namedtuple("Scenario", "id key title budget criteria accident required")

SCENARIOS = [
    Scenario("S1", "only-error", "只发 error 不发终态", "default",
             "error 后服务端以 1000 关闭连接；重连 1 次即 completed；无内存准入拒绝、无 proxy_failed；失效密文被清、有效密文保留",
             "error 后连接一直不关闭（旧连接占着内存预算）", True),
    Scenario("S2", "big-history", "大历史首帧（不小于 3MiB）", "default",
             "首帧不小于 3MiB 仍满足 S1 全部判据；大段消息原样转发（出站帧逐项等于预期）",
             "error 后连接一直不关闭，且每个旧连接按大历史加权占预算", True),
    Scenario("S3", "budget-one-conn", "预算压到只够一个连接", "tight",
             "预算 64MiB、首帧 3.0/3.5/4.0MiB、2 个失效项：重连 1 次即 completed，websocket_request_memory_rejected 为 0",
             "websocket_request_memory_rejected 多次，重连用尽 5 次仍无 completed（客户端只见 1013 关闭）", True),
    Scenario("S4", "multi-bad-items", "两个以上失效项", "default",
             "6 个失效项 + 8 个有效项：一次拒绝记完全部失效项，重连 1 次即 completed，出站帧只缺失效项密文",
             "逐项剥离：6 个失效项需要 7 次连接，客户端 5 次用尽仍无 completed", True),
    Scenario("S5", "compaction-rejected", "压缩项被拒", "default",
             "压缩项密文被拒：每次都给客户端明确的 invalid_encrypted_content 错误，压缩项从不被静默删除，不 1008，error 后服务端 1000 关闭",
             "error 后连接一直不关闭", True),
    Scenario("S6", "output-then-error", "已输出内容后再来 error", "default",
             "先输出文字再只来 error：不做任何清理重放（无 strip/entry），重连仍得到上游错误，不 1008",
             "无（行为在各版本一致，属回归守卫；若出现 strip/entry 即对已输出请求静默重放）", False),
    Scenario("S7", "concurrency", "并发", "default",
             "3 路并发 × 4 个会话（首帧约 1MiB）：12 个会话各自 1 次重连即 completed，互不串扰，无内存准入拒绝",
             "12 个会话的首次连接 error 后都不关闭（旧连接堆积占预算）", True),
    Scenario("S8", "account-state", "账号状态不变", "default",
             "整套运行前后账号行（status、schedulable、错误与限流／过载／临时不可调度字段）与健康熔断相关 Redis 键完全不变",
             "无（若 error 被误记为账号故障，会在账号行或熔断键上留痕）", False),
]

SCENARIO_IDS = [s.id for s in SCENARIOS]

TABLE_HEADER = "| 编号 | 场景 | 预算档 | 通过判据 | 事故特征（v0.2.18-4） | 事故必现 |"
TABLE_RULE = "| --- | --- | --- | --- | --- | --- |"


def render_table() -> str:
    """场景表的 Markdown 文本（README 里的场景表就是它，单元测试核对二者逐字一致）。"""
    rows = [TABLE_HEADER, TABLE_RULE]
    for s in SCENARIOS:
        rows.append("| %s | %s（%s） | %s | %s | %s | %s |" % (s.id, s.title, s.key, s.budget, s.criteria, s.accident, "是" if s.required else "否"))
    return "\n".join(rows)


# ---------------------------------------------------------------------------------------------
# 公共判据
# ---------------------------------------------------------------------------------------------
HOLD = float(os.environ.get("BHV_HOLD", "3.0"))   # error 之后等服务端关闭的最长秒数；超时视为“服务端不关闭”
MAX_ATTEMPTS = 5                                  # Codex 0.165 的重连上限：连同首次共 5 次连接
SKIP_LOG_CHECKS = False                           # --skip-log-checks：不核对依赖日志文案的断言


def key_g1() -> str:
    return load_state()["keys"]["g1"]["key"]


def key_g3() -> str:
    return load_state()["keys"]["g3"]["key"]


def first_error_intact(a) -> bool:
    """首次连接收到的 error 事件内容不变：400 invalid_request_error / invalid_encrypted_content，文案原样。"""
    e = a.get("error") or {}
    inner = e.get("error") or {}
    msg = inner.get("message") or ""
    return (inner.get("code") == "invalid_encrypted_content" and e.get("status") == 400
            and "The encrypted content" in msg and "could not be verified" in msg)


def closed_1000(a) -> bool:
    return bool(a.get("closed")) and a["closed"][0] == 1000


def held(a) -> bool:
    """error 之后服务端既不发终态也不关闭（超过 HOLD 秒）。"""
    return bool(a.get("held_after_error")) and a.get("closed") is None


def frame_blobs(s):
    """假上游这段时间收到的全部业务帧原文（序列化成字符串）；超大帧不落盘时跳过。"""
    out = []
    for r in s.frames:
        raw = lib.read_frame_file(r)
        if raw is not None:
            out.append(json.dumps(raw, ensure_ascii=False))
    return out


def log_check(R, ok, what, evidence=""):
    """依赖网关日志文案的断言；--skip-log-checks 时只记不判。"""
    if SKIP_LOG_CHECKS:
        R.note("[已跳过日志核对] %s —— %s" % (what, evidence))
        return True
    return R.check(ok, what, evidence, kind="log")


def recovery_checks(R, s, hist, label, max_conn=2, strict=False):
    """“只发 error 的失效密文”恢复路径的通用判据（S1／S2／S3／S4 共用）。返回关键事实字典，供事故特征判断。"""
    a0 = s.attempts[0]
    rej = count_memory_rejected(s.gw_lines)
    pf = count_proxy_failed(s.gw_lines)
    for i, a in enumerate(s.attempts, 1):
        R.note(lib.attempt_line(i, a, HOLD))
    R.note("网关日志: memory_rejected=%d proxy_failed=%d" % (rej, pf))
    R.check(first_error_intact(a0), "%s首次：客户端收到的 error 事件内容不变（400 / invalid_encrypted_content，文案原样）" % label,
            json.dumps(a0["error"], ensure_ascii=False)[:200] if a0["error"] else "无 error 事件")
    R.check(closed_1000(a0) and a0["events"] == ["error"],
            "%s首次：error 之后服务端主动以关闭码 1000 结束连接（不等终态，释放内存预算）" % label,
            "events=%s closed=%s held_after_error=%s" % (a0["events"], a0["closed"], a0["held_after_error"]))
    R.check(s.completed and s.completed_index <= max_conn, "%s最终收到 response.completed，连接次数 ≤ %d（首次 + 重连 %d 次）" % (label, max_conn, max_conn - 1),
            "completed_index=%s attempts=%d" % (s.completed_index, len(s.attempts)))
    R.check(rej == 0, "%s网关日志无 websocket_request_memory_rejected" % label, "n=%d" % rej)
    R.check(all(a["hs"] == "101" and not a["send_err"] for a in s.attempts), "%s每次连接都通过准入、握手 101 且首帧发送无异常" % label,
            str([(a["hs"], a["send_err"]) for a in s.attempts]))
    R.check(pf == 0, "%s网关日志无 openai.websocket_proxy_failed（没有被 1008 断开）" % label, "n=%d" % pf)
    final = s.final_frame()
    if final is not None and hist is not None:
        blob = json.dumps(final, ensure_ascii=False)
        kept = sum(1 for v in hist.valid if v in blob)
        gone = all(b not in blob for b in hist.bad)
        cmp_ok = (hist.cmp_enc is None) or (hist.cmp_enc in blob)
        R.check(gone and kept == len(hist.valid) and cmp_ok,
                "%s上游收到的最终帧：%d 个失效项密文被清、%d 个有效推理项密文与压缩项原样保留" % (label, len(hist.bad), len(hist.valid)),
                "失效项已清=%s 有效项保留=%d/%d 压缩项保留=%s" % (gone, kept, len(hist.valid), cmp_ok))
        if strict:
            view = received_view(final)
            exp_del = expected_after_recovery(hist.items, set(hist.bad))
            exp_skel = expected_after_recovery(hist.items, set(hist.bad), keep_skeleton=True)
            form = "删除空壳" if view == exp_del else ("保留空壳" if view == exp_skel else None)
            R.check(form is not None, "%s出站帧逐项等于预期（仅作 content 清理与清失效项密文，空壳删除或保留均可；消息／工具配对／顺序无任何差异）" % label,
                    "上游 %d 项，预期 %d（删空壳）或 %d（留空壳）项；形态=%s" % (len(view), len(exp_del), len(exp_skel), form))
    elif hist is not None:
        R.check(False, "%s读不到上游最后一次完成的业务帧原文" % label, "frames=%d" % len(s.frames))
    lg = lineage(s.gw_lines)
    te = lg["turn_end"]
    log_check(R, bool(te) and te[0].get("close_status") == "1000", "%s日志含 stage=turn_end close_status=1000（error 后本轮被主动结束）" % label,
              str(te[:1]))
    return {"rejected": rej, "proxy_failed": pf, "lineage": lg}


# ---------------------------------------------------------------------------------------------
# 场景
# ---------------------------------------------------------------------------------------------
def s1(R):
    """S1 只发 error 不发终态：真实上游对失效密文的应答形态。"""
    tag = new_session_id("s1")[-6:]
    hist = build_history(tag, bad=1, valid=3)
    sid = new_session_id()
    set_ctl(reject_ciphers=hist.bad, hint="ellipsis", hint_len=4)
    s = drive_session(key_g1(), sid, frame(sid, hist.items), max_attempts=MAX_ATTEMPTS, hold=HOLD)
    recovery_checks(R, s, hist, "", max_conn=2)
    R.set_signature(held(s.attempts[0]), "首次连接 error 后 %.0f 秒内服务端 %s" % (HOLD, "没有关闭连接" if held(s.attempts[0]) else "已关闭：%s" % (s.attempts[0]["closed"],)))


def s2(R):
    """S2 大历史首帧（不小于 3MiB），默认预算。"""
    tag = new_session_id("s2")[-6:]
    hist = build_history(tag, bad=1, valid=11, pad_mib=3.2)
    sid = new_session_id()
    fr = frame(sid, hist.items)
    size = frame_bytes(fr)
    R.check(size >= 3 * MIB, "前置：首帧体积不小于 3MiB", "%.2f MiB，input %d 项" % (size / MIB, len(hist.items)))
    set_ctl(reject_ciphers=hist.bad, hint="ellipsis", hint_len=4)
    s = drive_session(key_g1(), sid, fr, max_attempts=MAX_ATTEMPTS, hold=HOLD)
    recovery_checks(R, s, hist, "", max_conn=2, strict=True)
    final = s.final_frame()
    if final is not None:
        msgs = [i for i in final["input"] if i.get("type") == "message"]
        total = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs)
        R.check(total > 3 * MIB, "大段历史消息原样转发（上游收到 message %d 条，合计 %.2f MiB）" % (len(msgs), total / MIB), "")
    R.set_signature(held(s.attempts[0]), "首次连接 error 后 %.0f 秒内服务端 %s" % (HOLD, "没有关闭连接" if held(s.attempts[0]) else "已关闭：%s" % (s.attempts[0]["closed"],)))


def s3(R):
    """S3 预算压到只够一个连接：事故的直接复现场景。"""
    seen = []
    for mib in (3.0, 3.5, 4.0):
        lib.restart_gateway("tight")   # 每个体积前重建网关：旧版本残留的占预算连接不会污染下一轮
        tag = new_session_id("s3")[-6:]
        hist = build_history(tag, bad=2, valid=11, pad_mib=mib)
        sid = new_session_id()
        fr = frame(sid, hist.items)
        size = frame_bytes(fr)
        label = "[%.1fMiB] " % mib
        R.check(size >= mib * MIB, "%s前置：首帧体积不小于 %.1fMiB" % (label, mib), "%.2f MiB" % (size / MIB))
        set_ctl(reject_ciphers=hist.bad, hint="ellipsis", hint_len=4)
        s = drive_session(key_g1(), sid, fr, max_attempts=MAX_ATTEMPTS, hold=HOLD)
        facts = recovery_checks(R, s, hist, label, max_conn=2, strict=False)
        closes = [a["closed"] for a in s.attempts if a["closed"]]
        seen.append((mib, facts["rejected"], s.completed, closes[-1:] if closes else None))
    reproduced = any(rej >= 2 and not done for _, rej, done, _ in seen)
    R.set_signature(reproduced, "; ".join("%.1fMiB: memory_rejected=%d completed=%s 末次关闭=%s" % (m, r, d, c) for m, r, d, c in seen))


def s4(R):
    """S4 两个以上失效项：一次拒绝应记完全部失效项。"""
    tag = new_session_id("s4")[-6:]
    hist = build_history(tag, bad=6, valid=8)
    sid = new_session_id()
    set_ctl(reject_ciphers=hist.bad, hint="ellipsis", hint_len=4)
    s = drive_session(key_g1(), sid, frame(sid, hist.items), max_attempts=MAX_ATTEMPTS, hold=HOLD)
    recovery_checks(R, s, hist, "", max_conn=2, strict=True)
    lg = lineage(s.gw_lines)
    rj = lg["reject"][0] if lg["reject"] else {}
    log_check(R, rj.get("marked") == str(len(hist.bad)) and rj.get("result") == "recorded",
              "日志：首次上游拒绝一次记完 %d 个失效项（marked=%d result=recorded）" % (len(hist.bad), len(hist.bad)), str(rj))
    stuck = (not s.completed) or s.completed_index > 2
    R.set_signature(stuck, "completed_index=%s，连接 %d 次（%d 个失效项逐项剥离需要 %d 次连接，客户端上限 %d 次）" % (
        s.completed_index, len(s.attempts), len(hist.bad), len(hist.bad) + 1, MAX_ATTEMPTS))


def s5(R):
    """S5 压缩项被拒：恢复不了，但不能静默删压缩项，也不能裸关闭。"""
    tag = new_session_id("s5")[-6:]
    hist = build_history(tag, bad=0, valid=3, with_compaction=True)
    sid = new_session_id()
    fr = frame(sid, hist.items)
    set_ctl(reject_ciphers=[hist.cmp_enc], hint="ellipsis", hint_len=4)
    s = drive_session(key_g1(), sid, fr, max_attempts=3, hold=HOLD)
    for i, a in enumerate(s.attempts, 1):
        R.note(lib.attempt_line(i, a, HOLD))
    codes = [s.error_code(i) for i in range(len(s.attempts))]
    R.check(len(s.attempts) == 3 and all(c == "invalid_encrypted_content" for c in codes) and not s.completed,
            "每次连接都收到明确的 invalid_encrypted_content 错误，从不 completed（不假装成功）", str(codes))
    R.check(all(not (a["closed"] and a["closed"][0] == 1008) for a in s.attempts), "没有任何一次被 1008 关闭",
            str([a["closed"] for a in s.attempts]))
    R.check(closed_1000(s.attempts[0]), "首次：error 之后服务端以关闭码 1000 结束连接", "closed=%s held=%s" % (s.attempts[0]["closed"], s.attempts[0]["held_after_error"]))
    blobs = frame_blobs(s)
    R.check(len(blobs) == 3 and all(hist.cmp_enc in b for b in blobs), "压缩项密文在每次发往上游的帧里都原样保留（从不被静默删除）", "上游帧 %d 个" % len(blobs))
    rej = count_memory_rejected(s.gw_lines)
    R.check(rej == 0 and count_proxy_failed(s.gw_lines) == 0, "网关日志无内存准入拒绝、无 proxy_failed", "memory_rejected=%d" % rej)
    lg = lineage(s.gw_lines)
    r0 = lg["reject"][0] if lg["reject"] else {}
    log_check(R, r0.get("result") == "unrecoverable" and r0.get("reason") == "compaction_item" and r0.get("marked") == "0",
              "日志：判不可恢复（result=unrecoverable reason=compaction_item marked=0）", str(r0))
    log_check(R, not lg["strip"], "日志：没有任何清理（无 lineage_strip）", str(lg["strip"][:1]))
    R.set_signature(held(s.attempts[0]), "首次连接 error 后 %.0f 秒内服务端 %s" % (HOLD, "没有关闭连接" if held(s.attempts[0]) else "已关闭：%s" % (s.attempts[0]["closed"],)))


def s6(R):
    """S6 已输出内容后再来 error：不推断、不记录、不静默重放。"""
    tag = new_session_id("s6")[-6:]
    hist = build_history(tag, bad=1, valid=2)
    sid = new_session_id()
    fr = frame(sid, hist.items)
    set_ctl(reject_ciphers=hist.bad, hint="ellipsis", hint_len=4, output_first=True)
    s = drive_session(key_g1(), sid, fr, max_attempts=2, hold=HOLD)
    for i, a in enumerate(s.attempts, 1):
        R.note(lib.attempt_line(i, a, HOLD))
    a0 = s.attempts[0]
    ev = a0["events"]
    R.check("response.output_text.delta" in ev and "error" in ev and ev.index("response.output_text.delta") < ev.index("error"),
            "首次：客户端先收到文字增量、再收到 error（已有输出）", str(ev))
    R.check(len(s.attempts) == 2 and not s.completed and all(s.error_code(i) == "invalid_encrypted_content" for i in range(2)),
            "重连后仍得到上游错误：没有对已输出的请求清理重放", str([s.error_code(i) for i in range(len(s.attempts))]))
    R.check(all(not (a["closed"] and a["closed"][0] == 1008) for a in s.attempts), "没有 1008", str([a["closed"] for a in s.attempts]))
    blobs = frame_blobs(s)
    R.check(len(blobs) == 2 and all(hist.bad[0] in b for b in blobs), "两次发往上游的帧都仍含失效密文（网关没有改写已输出请求的历史）", "上游帧 %d 个" % len(blobs))
    R.check(count_proxy_failed(s.gw_lines) == 0, "网关日志无 proxy_failed", "")
    lg = lineage(s.gw_lines)
    log_check(R, not lg["strip"] and not lg["entry"], "日志：重连没有进场清理（无 stage=entry、无 lineage_strip）", "entry=%s strip=%s" % (lg["entry"][:1], lg["strip"][:1]))
    r0 = lg["reject"][0] if lg["reject"] else {}
    log_check(R, r0.get("result") == "unrecoverable" and r0.get("reason") == "output_committed" and r0.get("marked") == "0",
              "日志：判不可恢复（result=unrecoverable reason=output_committed marked=0）", str(r0))
    R.signature = None   # 回归守卫：各版本行为一致，不判事故特征


def s7(R):
    """S7 并发：多路会话同时走恢复路径，互不串扰，不堆积占预算的旧连接。"""
    workers, rounds = 3, 4
    key = key_g1()
    mark = lib.mock_mark()
    t0 = lib.now_utc()
    results, errors = [], []
    rlock = threading.Lock()

    def worker(i):
        try:
            for rnd in range(rounds):
                sid = "bhv-c%d-%d-%s" % (i, rnd, new_session_id("")[-6:])
                hist = build_history("c%d%d%s" % (i, rnd, sid[-4:]), bad=1, valid=2, pad_mib=1.0)
                add_reject(*hist.bad)
                fr = frame(sid, hist.items)
                attempts = []
                for _ in range(MAX_ATTEMPTS):
                    a = lib.attempt_once(key, sid, fr, hold=HOLD)
                    attempts.append(a)
                    if a["completed"]:
                        break
                with rlock:
                    results.append({"sid": sid, "hist": hist, "attempts": attempts})
        except Exception as exc:   # 记下异常，由主线程判红
            with rlock:
                errors.append(repr(exc))

    set_ctl(hint="ellipsis", hint_len=4)
    ths = [threading.Thread(target=worker, args=(i,)) for i in range(workers)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    time.sleep(0.8)
    lines = lib.gw_logs_since(t0)
    rej = count_memory_rejected(lines)
    pf = count_proxy_failed(lines)
    frames = lib.mock_frames_since(mark)
    total = workers * rounds
    n_done = sum(1 for r in results if any(a["completed"] for a in r["attempts"]))
    over = [r["sid"] for r in results if len(r["attempts"]) > 2]
    not_closed = sum(1 for r in results if not closed_1000(r["attempts"][0]))
    held_n = sum(1 for r in results if held(r["attempts"][0]))
    mock_rej = [f for f in frames if (f.get("action") or "").startswith("reject")]
    mock_done = [f for f in frames if f.get("action") == "complete"]
    R.note("会话 %d 个：completed=%d 超过 2 次连接=%d 首次未被服务端 1000 关闭=%d（其中不关闭=%d）；memory_rejected=%d proxy_failed=%d；假上游 拒绝 %d 次 完成 %d 次" % (
        len(results), n_done, len(over), not_closed, held_n, rej, pf, len(mock_rej), len(mock_done)))
    R.check(not errors and len(results) == total, "%d 个会话全部跑完、无客户端异常" % total, str(errors[:2]))
    R.check(n_done == total and not over, "每个会话 1 次重连内 completed", "completed=%d/%d 超限会话=%d" % (n_done, total, len(over)))
    R.check(not_closed == 0, "每个会话首次连接都在 error 后被服务端以 1000 关闭", "未关闭=%d" % not_closed)
    R.check(rej == 0, "网关日志无 websocket_request_memory_rejected", "n=%d" % rej)
    R.check(pf == 0, "网关日志无 openai.websocket_proxy_failed", "n=%d" % pf)
    # 串扰核对：网关会改写 prompt_cache_key，所以按有效密文把完成帧归到会话——每个会话必须恰有自己的完成帧，
    # 帧里有自己的全部有效密文，且没有任何会话的失效密文、没有别的会话的有效密文
    blobs_done = []
    for f in mock_done:
        raw = lib.read_frame_file(f)
        if raw is not None:
            blobs_done.append(json.dumps(raw, ensure_ascii=False))
    crossed = 0
    for r in results:
        mine = [b for b in blobs_done if r["hist"].valid[0] in b]
        if len(mine) != 1:
            crossed += 1
            continue
        blob = mine[0]
        others_valid = [v for o in results if o is not r for v in o["hist"].valid]
        others_bad = [b for o in results for b in o["hist"].bad]
        if not all(v in blob for v in r["hist"].valid) or any(b in blob for b in others_bad) or any(v in blob for v in others_valid):
            crossed += 1
    R.check(crossed == 0, "互不串扰：每个会话恰有一个完成帧，只含自己的有效密文、不含任何会话的失效密文", "错乱或缺帧=%d/%d" % (crossed, total))
    bad = rej > 0 or held_n > 0 or n_done < total
    R.set_signature(bad, "首次连接不关闭 %d/%d；memory_rejected=%d；completed=%d/%d" % (held_n, len(results), rej, n_done, total))


def s8(R):
    """S8 账号状态不变：整套场景前后账号行与健康熔断键不变；本场景再补一轮 OAuth 与 API Key 账号的失效密文流量。"""
    ctx = CONTEXT
    base = ctx.get("snapshot_start")
    R.check(base is not None, "前置：运行开始时已取账号基线快照", "")
    for label, key in (("OAuth 账号 o1", key_g1()), ("API Key 账号 k1（池模式，健康熔断阈值 1）", key_g3())):
        tag = new_session_id("s8")[-6:]
        hist = build_history(tag, bad=1, valid=2)
        sid = new_session_id()
        set_ctl(reject_ciphers=hist.bad, hint="ellipsis", hint_len=4)
        s = drive_session(key, sid, frame(sid, hist.items), max_attempts=MAX_ATTEMPTS, hold=HOLD)
        R.check(s.completed and s.completed_index <= 2, "%s：失效密文流量跑完（重连 1 次内 completed）" % label,
                "completed_index=%s closed0=%s" % (s.completed_index, s.attempts[0]["closed"]))
    # 再来一轮“已输出后 error”和压缩项被拒这类不可恢复流量，确认它们也不拖累账号
    tag = new_session_id("s8")[-6:]
    hist = build_history(tag, bad=0, valid=2, with_compaction=True)
    sid = new_session_id()
    set_ctl(reject_ciphers=[hist.cmp_enc], hint="ellipsis", hint_len=4)
    drive_session(key_g1(), sid, frame(sid, hist.items), max_attempts=2, hold=HOLD)
    set_ctl()
    time.sleep(1.0)
    end = lib.snapshot_accounts()
    diffs = lib.diff_snapshots(base or {}, end) if base is not None else ["缺少基线快照"]
    for d in diffs[:10]:
        R.note(d)
    rows = end["accounts"]
    R.check(not diffs, "整套运行前后账号状态不变（accounts 全字段扣除凭据／时间戳，加 Redis 熔断类键）", "%d 处差异" % len(diffs) if diffs else "账号 %d 个，差异 0" % len(rows))
    R.check(all(r.get("status") == "active" and r.get("schedulable") is True for r in rows.values()) and len(rows) >= 2,
            "账号仍全部 active 且 schedulable", str({k: (v.get("status"), v.get("schedulable")) for k, v in rows.items()}))
    R.set_signature(bool(diffs), "; ".join(diffs[:3]) if diffs else "账号状态无差异")


RUNNERS = {"S1": s1, "S2": s2, "S3": s3, "S4": s4, "S5": s5, "S6": s6, "S7": s7, "S8": s8}
CONTEXT = {}


# ---------------------------------------------------------------------------------------------
# 冒烟与主流程
# ---------------------------------------------------------------------------------------------
def smoke() -> bool:
    """冒烟：网关健康，普通会话（假上游不拒绝任何密文）经 OAuth 与 API Key 两条链路都能完成。"""
    set_ctl()
    ok = True
    for label, key in (("OAuth 账号", key_g1()), ("API Key 账号", key_g3())):
        sid = new_session_id("smoke")
        hist = build_history(sid[-6:], bad=0, valid=2, with_compaction=False)
        s = drive_session(key, sid, frame(sid, hist.items), max_attempts=1, hold=HOLD)
        good = s.completed
        print("  冒烟 %s: %s —— %s" % (label, "通过" if good else "失败", lib.attempt_line(1, s.attempts[0], HOLD)), flush=True)
        ok = ok and good
    return ok


def run_scenario(sc: Scenario):
    print("\n===== %s %s（%s，预算档 %s） =====" % (sc.id, sc.title, sc.key, sc.budget), flush=True)
    lib.CURRENT["scenario"] = sc.id
    R = Recorder(sc.id)
    try:
        if sc.id != "S3":
            lib.restart_gateway(sc.budget)   # 每个场景前重建网关：清空内存里的失效记录、残留连接与预算占用，场景互不污染
        RUNNERS[sc.id](R)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        R.check(False, "场景异常终止: %r" % exc)
    finally:
        try:
            set_ctl()
        except Exception:
            pass
    return R


def verdict(sc: Scenario, R: Recorder, expect: str) -> dict:
    """单个场景在给定预期下是否达标。"""
    if expect == "pass":
        ok = R.fail == 0
        note = "通过" if ok else "未通过（%d 项断言失败）" % R.fail
    else:
        if not sc.required:
            ok, note = True, "参考（事故特征%s）" % ("出现" if R.signature else ("未出现" if R.signature is False else "不适用"))
        else:
            ok = R.signature is True
            note = "事故特征出现" if ok else "事故特征未出现（未复现）"
    return {"id": sc.id, "key": sc.key, "title": sc.title, "ok": ok, "note": note, "assertion_fail": R.fail,
            "log_check_fail": R.failed_log_checks(), "signature": R.signature, "signature_evidence": R.signature_evidence,
            "required": sc.required,
            "checks": [{"ok": ok_, "what": w, "evidence": str(e)[:300], "kind": k} for ok_, w, e, k in R.rows]}


def main(argv=None) -> int:
    global SKIP_LOG_CHECKS
    ap = argparse.ArgumentParser(description="升级前行为验收套件：场景驱动")
    ap.add_argument("--expect", choices=("pass", "accident"), default="pass")
    ap.add_argument("--only", default="", help="只跑指定场景，逗号分隔，如 S1,S3")
    ap.add_argument("--out", default="", help="结果 JSON 输出目录")
    ap.add_argument("--image-label", default="", help="被测镜像标识，写进结果文件")
    ap.add_argument("--skip-log-checks", action="store_true", help="不核对依赖网关日志文案的断言（日志改名的新版本临时用）")
    ap.add_argument("--print-table", action="store_true", help="打印场景表后退出")
    args = ap.parse_args(argv)
    if args.print_table:
        print(render_table())
        return 0
    SKIP_LOG_CHECKS = args.skip_log_checks
    only = [x.strip() for x in args.only.split(",") if x.strip()]
    unknown = [x for x in only if x not in SCENARIO_IDS]
    if unknown:
        print("未知场景: %s（可选 %s）" % (",".join(unknown), ",".join(SCENARIO_IDS)), file=sys.stderr)
        return 2
    chosen = [s for s in SCENARIOS if not only or s.id in only]
    print("== 冒烟 ==", flush=True)
    if not smoke():
        print("冒烟失败：栈不健康，先排查栈（不是被测镜像的场景判据问题）", file=sys.stderr)
        return 3
    CONTEXT["snapshot_start"] = lib.snapshot_accounts()
    print("账号基线快照: 账号 %d 个，Redis 键 %d 个" % (len(CONTEXT["snapshot_start"]["accounts"]), len(CONTEXT["snapshot_start"]["redis"])), flush=True)
    t_start = time.time()
    verdicts = []
    for sc in chosen:
        R = run_scenario(sc)
        verdicts.append(verdict(sc, R, args.expect))
    print("\n== 汇总（预期：%s；镜像：%s；用时 %d 秒） ==" % ("通过" if args.expect == "pass" else "复现事故", args.image_label or "未标注", time.time() - t_start))
    for v in verdicts:
        sig = {True: "事故特征出现", False: "事故特征未出现", None: "不适用"}[v["signature"]]
        print("  %-3s %-22s %s  断言失败=%d(日志类%d)  %s" % (v["id"], v["key"], "OK " if v["ok"] else "NOK", v["assertion_fail"], v["log_check_fail"], sig))
    all_ok = all(v["ok"] for v in verdicts)
    if args.out:
        os.makedirs(args.out, exist_ok=True)
        with open(os.path.join(args.out, "results-%s.json" % args.expect), "w", encoding="utf-8") as f:
            json.dump({"image": args.image_label, "expect": args.expect, "all_ok": all_ok, "scenarios": verdicts}, f, ensure_ascii=False, indent=1)
    print("结论: %s" % ("达到预期" if all_ok else "未达到预期"))
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
