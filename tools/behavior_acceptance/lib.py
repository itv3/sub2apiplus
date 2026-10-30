# -*- coding: utf-8 -*-
"""行为验收套件公共库（只用标准库，在跑套件的 docker 宿主机上运行）。

分三块：
  1. 环境与外部命令：套件目录、容器名、docker / psql / redis-cli 调用（有副作用，单元测试不碰）；
  2. 历史与帧构造、网关日志解析、会话驱动（纯逻辑为主，单元测试直接测）；
  3. 判据记录器 Recorder 与账号状态快照比对。

导入本模块不做任何 I/O、不读环境变量之外的任何东西。
"""
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone

from wsclient import WSClient

MIB = 1024 * 1024


# ---------------------------------------------------------------------------------------------
# 1. 环境与外部命令
# ---------------------------------------------------------------------------------------------
def project() -> str:
    """compose 项目名，也是所有容器、网络的名字前缀。"""
    return os.environ.get("BHV_PROJECT", "bhvacc")


def workdir() -> str:
    """本次运行的工作目录（run.sh 创建，里面放测试 CA、假上游数据、state.json、gw.env）。"""
    path = os.environ.get("BHV_WORKDIR", "")
    if not path:
        raise RuntimeError("未设置 BHV_WORKDIR：请通过 run.sh 运行，不要直接运行场景脚本")
    return path


def cname(role: str) -> str:
    """套件容器名：gw（网关）/ pg / redis / mock。"""
    return "%s-%s" % (project(), role)


def mock_dir() -> str:
    return os.path.join(workdir(), "mock-data")


def gateway_port() -> int:
    return int(os.environ.get("BHV_PORT", "28190"))


def sh(cmd, check=True, timeout=120):
    r = subprocess.run(cmd, shell=isinstance(cmd, str), capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise RuntimeError("命令失败 rc=%s %s\n%s" % (r.returncode, cmd if isinstance(cmd, str) else " ".join(cmd), r.stderr[:500]))
    return r.stdout


def sql(query: str) -> str:
    return sh(["docker", "exec", "-i", cname("pg"), "psql", "-U", "sub2api", "-d", "sub2api", "-At", "-F", "|", "-c", query]).strip()


def lit(s: str) -> str:
    """SQL 字符串字面量（单引号加倍）。"""
    return "'" + s.replace("'", "''") + "'"


def state_path() -> str:
    return os.path.join(workdir(), "state.json")


def load_state() -> dict:
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st: dict) -> None:
    fd = os.open(state_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(st, f, ensure_ascii=False, indent=1)


def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


# 当前正在跑的场景编号（run_scenario 设置），用来给落盘的网关日志命名
CURRENT = {"scenario": "S0"}
# 落盘的网关日志只留与本套件相关的行，避免把每个请求的访问日志都存下来
LOG_KEYS = ("websocket_request_memory_rejected", "encrypted_recovery", "lineage", "websocket_proxy_failed", "websocket_ingress",
            "ingress_ws_", "stage=")


def gw_logs_since(since_iso: str) -> list:
    """网关容器自某时刻起的全部日志行（stdout 与 stderr 合并）。"""
    out = sh("docker logs --since %s %s 2>&1 || true" % (since_iso, cname("gw")), check=False)
    return out.splitlines()


def save_gw_log(name: str, lines) -> None:
    """把与套件相关的网关日志行存进 BHV_LOGDIR（run.sh 设置，在结果目录里），事后可核对 memory_rejected 等证据。"""
    d = os.environ.get("BHV_LOGDIR", "")
    if not d:
        return
    try:
        os.makedirs(d, exist_ok=True)
        keep = [l for l in lines if any(k in l for k in LOG_KEYS)]
        with open(os.path.join(d, "%s-%s.log" % (CURRENT["scenario"], name)), "w", encoding="utf-8") as f:
            f.write("\n".join(keep) + "\n")
    except OSError:
        pass


def restart_gateway(budget: str) -> None:
    """让 run.sh 的 stackctl 重建网关容器（budget：default | tight）。重建会清空网关内存里的失效记录、残留连接与预算占用。"""
    ctl = os.environ.get("BHV_STACKCTL", "")
    if not ctl:
        raise RuntimeError("未设置 BHV_STACKCTL：请通过 run.sh 运行")
    sh(["bash", ctl, "restart-gw", budget], timeout=240)


# ---------------------------------------------------------------------------------------------
# 2. 历史与帧构造
# ---------------------------------------------------------------------------------------------
def cipher(label: str, length: int = 98) -> str:
    """确定性的 OpenAI 形态密文（gAAAAB 开头）。"""
    h = hashlib.sha256(label.encode()).hexdigest()
    s = "gAAAAB" + h + h[:24] + h[10:14].upper()
    return s[:length] if length < len(s) else s


def short_cipher(label: str) -> str:
    """38 字符的短密文（模拟事故里别家模型阶段留下的 38 字符历史值）。"""
    h = hashlib.sha256(label.encode()).hexdigest()
    return ("ds" + h)[:36] + "=="


def reasoning(label, enc=None, content=False, summary=True):
    """OpenAI 自家格式的推理项：有 id、有摘要、默认不带 content。"""
    it = {"type": "reasoning", "id": "rs_" + label, "encrypted_content": enc if enc is not None else cipher(label)}
    it["summary"] = [{"type": "summary_text", "text": "摘要-" + label}] if summary else []
    if content:
        it["content"] = [{"type": "reasoning_text", "text": "别家模型的可见推理-" + label}]
    return it


def foreign(label, enc=None, summary=True):
    """别家模型留下的推理项：带非空 content 数组（网关“一次清完”的结构特征）。"""
    return reasoning(label, enc=enc, content=True, summary=summary)


def foreign_bare(label, enc):
    """别家模型留下的“空骨架”推理项：无 id、摘要为空、只有 content 与密文。清理后整项被删除。"""
    return {"type": "reasoning", "summary": [], "content": [{"type": "reasoning_text", "text": "别家推理-" + label}], "encrypted_content": enc}


def compaction(label, enc=None):
    return {"type": "compaction", "id": "cmp_" + label, "encrypted_content": enc if enc is not None else cipher("cmp-" + label)}


def msg(role, text):
    return {"type": "message", "role": role,
            "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}]}


def call(n, name="lookup"):
    return [{"type": "function_call", "id": "fc_%s" % n, "call_id": "call_%s" % n, "name": name, "arguments": json.dumps({"q": n})},
            {"type": "function_call_output", "call_id": "call_%s" % n, "output": "ok-%s" % n}]


def frame(session_key, items, extra=None, model="gpt-6.1-sol"):
    f = {"type": "response.create", "model": model, "stream": True, "store": False, "reasoning": {"effort": "high"},
         "instructions": "你是测试助手。", "prompt_cache_key": session_key, "input": items}
    if extra:
        f.update(extra)
    return f


def frame_bytes(fr) -> int:
    """帧序列化后的字节数（客户端实际发出的体积）。"""
    return len(json.dumps(fr, ensure_ascii=False).encode())


class History:
    """一段测试历史：items 是 input 数组，其余是各类密文，供出站帧核对。"""

    def __init__(self, items, bad, valid, cmp_enc):
        self.items = items
        self.bad = bad          # 会被假上游拒绝的密文（别家阶段的短密文，所在项带非空 content）
        self.valid = valid      # 有效 OpenAI 密文（所在项不带 content）
        self.cmp_enc = cmp_enc  # 压缩项密文，没有则 None


def build_history(tag, bad=2, valid=11, with_compaction=True, pad_mib=0.0, pad_pieces=8) -> History:
    """构造一段跨模型旧会话历史。

    bad    失效项个数：别家模型阶段留下的空骨架推理项（带非空 content、无 id、摘要为空、38 字符短密文）；
    valid  有效项个数：OpenAI 自家推理项（带 id 与摘要、不带 content）；
    pad_mib  用大段文本消息把首帧撑到至少这个体积（0 表示不撑）；
    压缩项与配对工具调用各一份，其余是普通消息。"""
    bads = [short_cipher("B%d%s" % (i, tag)) for i in range(bad)]
    valids = [cipher("V%d%s" % (i, tag)) for i in range(valid)]
    cmp_enc = cipher("cmp-C" + tag) if with_compaction else None
    pieces = pad_pieces if pad_mib > 0 else 0
    chunk = int(pad_mib * MIB) // pieces if pieces else 0

    def filler(i):
        # 恰好 chunk 个字符的大段正文（ASCII，字节数＝字符数）
        base = "历史正文-%d-" % i
        return (base + ("0123456789abcdef" * 64) * (chunk // 1024 + 1))[:chunk] if chunk else base

    items = [msg("user", "第一问-" + tag)]
    pad_i = 0
    rounds = max(bad, valid, pieces, 1)
    for i in range(rounds):
        if pad_i < pieces:
            items.append(msg("assistant" if pad_i % 2 else "user", filler(pad_i)))
            pad_i += 1
        if i < bad:
            items.append(foreign_bare("%d%s" % (i, tag), bads[i]))
        if i < valid:
            items.append(reasoning("V%d%s" % (i, tag)))
        if i == 3:
            items.extend(call("%s_%d" % (tag, i)))
        if i == 5 and cmp_enc:
            items.append(compaction("C" + tag, enc=cmp_enc))
    if cmp_enc and rounds <= 5:
        items.append(compaction("C" + tag, enc=cmp_enc))
    items.append(msg("user", "最后一问-" + tag))
    return History(items, bads, valids, cmp_enc)


def norm(item):
    """历史项的语义视图（忽略网关补的 id / 内部元数据）：用来比较客户端原帧与上游收到的出站帧。"""
    t = item.get("type")
    if t == "message":
        txt = "".join(p.get("text", "") for p in (item.get("content") or []) if isinstance(p, dict))
        return ("message", item.get("role"), txt)
    if t == "function_call":
        return ("function_call", item.get("name"), item.get("arguments"), item.get("call_id"))
    if t == "function_call_output":
        return ("function_call_output", item.get("call_id"), item.get("output"))
    if t == "reasoning":
        return ("reasoning", item.get("id"), json.dumps(item.get("summary"), ensure_ascii=False, sort_keys=True),
                item.get("encrypted_content"), json.dumps(item.get("content"), ensure_ascii=False, sort_keys=True) if "content" in item else None)
    if t in ("compaction", "compaction_summary"):
        return (t, item.get("id"), item.get("encrypted_content"))
    return (t, json.dumps(item, ensure_ascii=False, sort_keys=True))


def expected_after_recovery(items, bad, keep_skeleton=False):
    """客户端原历史在“content 清理 + 失效密文恢复”后应有的语义视图。

    bad：被记录为失效的密文集合。reasoning：必删 content；密文命中则删密文；
    清完只剩 type（无 id、无非空 summary）的空壳：keep_skeleton=False 时整项删除，True 时保留空壳
    {"type":"reasoning","summary":[]}（0.2.18-5 的行为：GPT 上游接受这个空壳，见记忆里 10-30 21:10 原会话复测）。"""
    out = []
    for it in items:
        if it.get("type") == "reasoning":
            it2 = {k: v for k, v in it.items() if k != "content"}
            if it2.get("encrypted_content") in bad:
                it2.pop("encrypted_content")
            if set(it2.keys()) <= {"type"} or (set(it2.keys()) <= {"type", "summary"} and not it2.get("summary")):
                if keep_skeleton:
                    out.append(norm({"type": "reasoning", "summary": []}))
                continue
            out.append(norm(it2))
        else:
            out.append(norm(it))
    return out


def received_view(raw_frame):
    """上游收到的业务帧 input 的语义视图（去掉网关补的第 0 项开发者基础指令消息）。"""
    items = raw_frame["input"]
    if items and items[0].get("type") == "message" and items[0].get("role") == "developer":
        items = items[1:]
    return [norm(i) for i in items]


# ---------------------------------------------------------------------------------------------
# 假上游控制与读取
# ---------------------------------------------------------------------------------------------
_ctl_lock = threading.Lock()


def ctl_path() -> str:
    return os.path.join(mock_dir(), "ctl.json")


def log_path() -> str:
    return os.path.join(mock_dir(), "requests.jsonl")


def set_ctl(**kw):
    """整体替换假上游的控制指令（不带参数＝清空，恢复默认应答）。emit_failed 缺省为 False：真实上游形态，只发 error。"""
    with _ctl_lock:
        tmp = ctl_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(kw, f)
        os.replace(tmp, ctl_path())


def add_reject(*ciphers):
    """把密文追加进假上游的拒绝名单（并发场景要读改写，所以加锁）。"""
    with _ctl_lock:
        try:
            with open(ctl_path()) as f:
                cur = json.load(f)
        except Exception:
            cur = {}
        cur.setdefault("reject_ciphers", [])
        cur["reject_ciphers"] = list(cur["reject_ciphers"]) + [c for c in ciphers if c not in cur["reject_ciphers"]]
        tmp = ctl_path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cur, f)
        os.replace(tmp, ctl_path())


def mock_lines():
    try:
        with open(log_path(), encoding="utf-8") as f:
            return [json.loads(l) for l in f]
    except FileNotFoundError:
        return []


def mock_mark():
    return len(mock_lines())


def mock_frames_since(mark, business_only=True):
    out = [r for r in mock_lines()[mark:] if r.get("kind") == "frame"]
    if business_only:
        out = [r for r in out if r.get("generate") and r.get("n_input")]
    return out


def read_frame_file(rec):
    """读假上游落盘的原始帧；超大帧不落盘时返回 None。"""
    p = rec.get("frame_file")
    if not p:
        return None
    p = p.replace("/data/", mock_dir() + "/")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------------------------
# 网关日志解析（纯函数）
# ---------------------------------------------------------------------------------------------
_KV = re.compile(r"(\w+)=([^\s\"{}]+)")


def kv(line):
    """解析日志行里的 k=v 对。"""
    return dict(_KV.findall(line))


def count_memory_rejected(lines) -> int:
    """内存准入拒绝次数：v0.2.18-4 事故的核心特征日志。"""
    return sum(1 for l in lines if "websocket_request_memory_rejected" in l)


def count_proxy_failed(lines) -> int:
    return sum(1 for l in lines if "websocket_proxy_failed" in l)


def count_closed_normally(lines) -> int:
    return sum(1 for l in lines if "websocket_ingress_closed_normally" in l)


def lineage(lines):
    """按事件归类网关日志里与失效密文恢复相关的行（只留 k=v 摘要）。"""
    out = {"reject": [], "entry": [], "strip": [], "mark": [], "validate": [], "proxy_failed": [], "retry": [], "error_event": [], "turn_end": []}
    for l in lines:
        if "stage=upstream_reject" in l:
            out["reject"].append(kv(l))
        elif "stage=turn_end" in l:
            out["turn_end"].append(kv(l))
        elif "stage=entry" in l:
            out["entry"].append(kv(l))
        elif "stage=validate" in l:
            d = kv(l)
            d["cause"] = l.split("cause=", 1)[1][:160] if "cause=" in l else ""
            out["validate"].append(d)
        elif "invalid_encrypted_lineage_strip" in l:
            out["strip"].append(kv(l))
        elif "invalid_encrypted_lineage_mark" in l:
            out["mark"].append(kv(l))
        elif "websocket_proxy_failed" in l:
            out["proxy_failed"].append(l.split("\t")[-2][-260:] if "\t" in l else l[-260:])
        elif "ingress_ws_turn_retry" in l:
            out["retry"].append(kv(l))
        elif "ingress_ws_error_event" in l:
            out["error_event"].append(kv(l))
    return out


def brief(line, limit=300):
    """把一行网关日志压成短摘要（去掉 JSON 尾部上下文）。"""
    body = line.split("\t{", 1)[0]
    return body[-limit:]


# ---------------------------------------------------------------------------------------------
# 会话驱动：模拟 Codex 的重连行为
# ---------------------------------------------------------------------------------------------
def attempt_once(key, sid, fr, hold=3.0, timeout=30):
    """一次连接：握手、发首帧、读到 completed / 服务端关闭 / error 后 hold 秒仍不关闭。

    Codex 0.165 的行为：收到 error 或 Close 帧都立即重连（放弃旧 TCP）。这里 error 之后等 hold 秒看服务端是否关闭，
    超时就判定“服务端不关闭”（held_after_error=True）并放弃旧 TCP。"""
    import socket
    rec = {"events": [], "closed": None, "completed": None, "error": None, "hs": None, "send_err": None,
           "ms": None, "held_after_error": False, "client_request_id": None}
    t0 = time.time()
    try:
        c = WSClient(key, session_id=sid).connect()
    except Exception as exc:
        rec["hs"] = "握手失败: %s" % str(exc)[:160]
        rec["ms"] = int((time.time() - t0) * 1000)
        return rec
    rec["hs"] = "101"
    rec["client_request_id"] = c.client_request_id
    try:
        c.send_json(fr)
    except Exception as exc:
        rec["send_err"] = repr(exc)[:100]
    c.sock.settimeout(2.0)
    deadline = time.time() + timeout
    err_at = None
    while time.time() < deadline:
        try:
            kind, val = c.read_message()
        except socket.timeout:
            if err_at and time.time() - err_at > hold:
                rec["held_after_error"] = True
                break
            continue
        except (EOFError, OSError) as exc:
            rec["closed"] = ("eof", repr(exc)[:60])
            break
        if kind == "close":
            rec["closed"] = val
            break
        try:
            ev = json.loads(val)
        except Exception:
            continue
        rec["events"].append(ev.get("type"))
        if ev.get("type") == "error" and rec["error"] is None:
            rec["error"] = ev
            err_at = time.time()
        if ev.get("type") == "response.completed":
            rec["completed"] = (ev.get("response") or {}).get("id")
            break
    rec["ms"] = int((time.time() - t0) * 1000)
    c.close()
    return rec


class Session:
    """一个会话的全部连接尝试，以及这段时间的网关日志与假上游业务帧。"""

    def __init__(self, sid, attempts, t0, mark):
        self.sid = sid
        self.attempts = attempts
        self.t0 = t0
        self.mark = mark
        self.gw_lines = []
        self.frames = []

    @property
    def completed(self) -> bool:
        return any(a["completed"] for a in self.attempts)

    @property
    def completed_index(self):
        for i, a in enumerate(self.attempts, 1):
            if a["completed"]:
                return i
        return None

    def final_frame(self):
        """假上游最后一次“完成”的业务帧（原始 JSON），没有则 None。"""
        done = [r for r in self.frames if r.get("action") == "complete"]
        return read_frame_file(done[-1]) if done else None

    def error_code(self, index=0):
        a = self.attempts[index]
        return ((a.get("error") or {}).get("error") or {}).get("code")


def drive_session(key, sid, fr, max_attempts=5, hold=3.0, settle=0.6) -> Session:
    """按 Codex 的重连方式驱动一个会话：直到收到 completed 或连接次数用尽。"""
    t0 = now_utc()
    mark = mock_mark()
    attempts = []
    for _ in range(max_attempts):
        a = attempt_once(key, sid, fr, hold=hold)
        attempts.append(a)
        if a["completed"]:
            break
    time.sleep(settle)
    s = Session(sid, attempts, t0, mark)
    s.gw_lines = gw_logs_since(t0)
    s.frames = mock_frames_since(mark)
    save_gw_log(sid, s.gw_lines)
    return s


def attempt_line(i, a, hold):
    err = ((a.get("error") or {}).get("error") or {}).get("code")
    return "尝试 %d: 握手=%s 事件=%s error=%s completed=%s 服务端关闭=%s error后%.0fs仍未关闭=%s 耗时=%dms%s" % (
        i, a["hs"], a["events"], err, a["completed"], a["closed"], hold, a["held_after_error"], a["ms"] or 0,
        (" 发送异常=" + a["send_err"]) if a["send_err"] else "")


def new_session_id(prefix="bhv") -> str:
    return "%s-%s" % (prefix, uuid.uuid4().hex[:12])


# ---------------------------------------------------------------------------------------------
# 3. 判据记录器与账号状态快照
# ---------------------------------------------------------------------------------------------
class Recorder:
    """收集一个场景里的断言与证据。kind="log" 的断言依赖网关日志文案，版本改日志时只需改这类断言。"""

    def __init__(self, name):
        self.name = name
        self.rows = []      # (ok, 描述, 证据, 类别)
        self.fail = 0
        self.signature = None          # None＝本场景不判事故特征；True/False＝事故特征是否出现
        self.signature_evidence = ""

    def check(self, ok, what, evidence="", kind="behavior"):
        ok = bool(ok)
        self.rows.append((ok, what, evidence, kind))
        if not ok:
            self.fail += 1
        tag = "PASS" if ok else "FAIL"
        print("  [%s]%s %s%s" % (tag, "[日志]" if kind == "log" else "", what, (" —— " + str(evidence)) if evidence else ""), flush=True)
        return ok

    def note(self, text):
        print("  · " + text, flush=True)

    def set_signature(self, seen, evidence=""):
        self.signature = bool(seen)
        self.signature_evidence = evidence
        print("  [事故特征 %s] %s" % ("出现" if seen else "未出现", evidence), flush=True)

    def failed_log_checks(self) -> int:
        return sum(1 for ok, _, _, kind in self.rows if not ok and kind == "log")


# 快照里这些 Redis 键属于正常业务痕迹（粘性会话、WS 会话、限流、并发、用量、计费、排队、调度器快照 sched:*），不算账号健康变化
VOLATILE_REDIS = ("sticky", "openai_ws", "session", "rpm", "concurrency", "usage", "billing", "wait", "sched:")
# 账号行里的这些字段本来就会随请求变化
VOLATILE_ACCOUNT_COLUMNS = ("credentials", "updated_at", "last_used_at")


def snapshot_accounts() -> dict:
    """账号快照：accounts 全字段（去掉凭据与时间戳类字段）+ Redis 键名。"""
    rows = {}
    for line in sql("select to_jsonb(a) - 'credentials' - 'updated_at' - 'last_used_at' from accounts a where deleted_at is null order by id").splitlines():
        d = json.loads(line)
        rows[str(d["id"])] = d
    keys = sorted(sh(["docker", "exec", cname("redis"), "redis-cli", "--scan"], check=False).split())
    return {"accounts": rows, "redis": keys}


def diff_snapshots(a: dict, b: dict) -> list:
    """比较两份账号快照，返回人类可读的差异列表（空＝账号状态不变）。纯函数。"""
    diffs = []
    ra_all, rb_all = a.get("accounts", {}), b.get("accounts", {})
    for aid in sorted(set(ra_all) | set(rb_all), key=lambda x: int(x) if str(x).isdigit() else 0):
        ra, rb = ra_all.get(aid), rb_all.get(aid)
        if ra == rb:
            continue
        for k in sorted(set(ra or {}) | set(rb or {})):
            if k in VOLATILE_ACCOUNT_COLUMNS:
                continue
            if (ra or {}).get(k) != (rb or {}).get(k):
                diffs.append("账号 %s 字段 %s: %r -> %r" % (aid, k, (ra or {}).get(k), (rb or {}).get(k)))
    ka, kb = set(a.get("redis", [])), set(b.get("redis", []))
    new_keys = sorted(k for k in kb - ka if not any(x in k for x in VOLATILE_REDIS))
    gone_keys = sorted(k for k in ka - kb if not any(x in k for x in VOLATILE_REDIS))
    for k in new_keys:
        diffs.append("Redis 新增键: %s" % k)
    for k in gone_keys:
        diffs.append("Redis 消失键: %s" % k)
    return diffs
