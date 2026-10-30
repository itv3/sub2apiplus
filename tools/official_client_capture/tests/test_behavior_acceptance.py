"""升级前行为验收套件（tools/behavior_acceptance）的离线单元测试。

套件本身要起 docker 栈、连真实网关容器，这些在这里一概不碰。本模块只测不依赖网络和 docker 的部分：

* 脚本语法：Python 脚本逐个编译（不写 pyc）、shell 脚本 ``bash -n``；入口脚本的参数校验与项目名安全检查在碰 docker 之前就退出；
* 隔离静态检查：compose 文件与脚本里没有现网容器名、没有宿主机目录挂载（工作目录除外）、没有 docker.sock、没有 host 网络；
  对现网的唯一接触（快照）只用 ``docker inspect`` 与只读事务 SQL；
* 场景表：编号、字段、README 里的场景表与 ``render_table()`` 逐字一致；
* 假上游：``plan_response`` 的各个模式（只发 error、带终态、先输出、断线、各种文案形态…），以及 socketpair 上跑真实的
  WebSocket 收发（error 之后连接不被主动关闭）；
* 判据函数：日志解析、历史构造、出站帧预期视图、账号快照比对、判据记录器、场景判决、通用恢复判据对“健康”与“事故”两种会话的判定。

测试不在真实工作树里造任何目录或文件；需要落盘的用例一律用系统临时目录。
"""

from __future__ import annotations

import importlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

SUITE = Path(__file__).resolve().parents[2] / "behavior_acceptance"
SUITE_MODULES = ("lib", "wsclient", "wsmock", "scenarios", "setup_data")

# 不让导入套件模块在源码目录里写 __pycache__
sys.dont_write_bytecode = True
_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}


def _load():
    """把套件目录临时放进 sys.path 导入模块，返回 {名字: 模块}。

    放在 setUpModule 里而不是模块顶层：unittest 发现阶段会先导入全部测试模块，顶层改 sys.path 会让套件里的 lib／scenarios 等
    短名字在其它测试模块导入期间遮蔽同名模块；tearDownModule 把路径和 sys.modules 条目清掉。"""
    if str(SUITE) not in sys.path:
        sys.path.insert(0, str(SUITE))
    return {name: importlib.import_module(name) for name in SUITE_MODULES}


lib = wsmock = wsclient = scenarios = setup_data = None   # 由 setUpModule 填充


def setUpModule() -> None:
    mods = _load()
    globals().update(mods)


def tearDownModule() -> None:
    for name in SUITE_MODULES:
        sys.modules.pop(name, None)
    if str(SUITE) in sys.path:
        sys.path.remove(str(SUITE))


def run_script(args, env_extra=None, timeout=60):
    env = dict(_ENV)
    # 干净的项目名环境：测试不继承外部可能设置的 BHV_*
    for k in list(env):
        if k.startswith("BHV_"):
            del env[k]
    env.update(env_extra or {})
    return subprocess.run(["bash", *args], capture_output=True, text=True, timeout=timeout, env=env)


class ScriptSyntaxTests(unittest.TestCase):
    def test_python_scripts_compile(self) -> None:
        files = sorted(SUITE.glob("*.py"))
        self.assertGreaterEqual(len(files), 5)
        for path in files:
            with self.subTest(path.name):
                compile(path.read_text(encoding="utf-8"), str(path), "exec")

    def test_shell_scripts_pass_bash_n(self) -> None:
        for name in ("run.sh", "stackctl.sh", "remote_run.sh"):
            with self.subTest(name):
                r = run_script(["-n", str(SUITE / name)])
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_suite_directory_has_no_secrets_or_caches(self) -> None:
        names = sorted(p.name for p in SUITE.rglob("*") if p.is_file())
        self.assertFalse([n for n in names if n.endswith((".pem", ".key", ".env", ".pyc", ".log"))], names)
        self.assertFalse(list(SUITE.rglob("__pycache__")))
        expected = {"Dockerfile.mock", "README.md", "compose.yml", "lib.py", "remote_run.sh", "run.sh", "scenarios.py",
                    "setup_data.py", "stackctl.sh", "wsclient.py", "wsmock.py"}
        self.assertEqual(set(names), expected)


class EntryScriptGuardTests(unittest.TestCase):
    """入口脚本在碰 docker 之前就校验参数与项目名；最后一行永远是 EXIT=<码>。"""

    def last_line(self, r) -> str:
        return r.stdout.strip().splitlines()[-1]

    def test_help_exits_zero_with_exit_line(self) -> None:
        r = run_script([str(SUITE / "run.sh"), "--help"])
        self.assertEqual(r.returncode, 0)
        self.assertIn("用法", r.stdout)
        self.assertEqual(self.last_line(r), "EXIT=0")

    def test_missing_image_is_usage_error(self) -> None:
        r = run_script([str(SUITE / "run.sh")])
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.last_line(r), "EXIT=2")

    def test_bad_expect_and_unknown_option_rejected(self) -> None:
        for args in (["img", "--expect", "bogus"], ["img", "--nope"]):
            with self.subTest(args):
                r = run_script([str(SUITE / "run.sh"), *args])
                self.assertEqual(r.returncode, 2)
                self.assertEqual(self.last_line(r), "EXIT=2")

    def test_project_name_must_start_with_bhv(self) -> None:
        """项目名决定拆栈时删谁：指向现网名字必须在碰 docker 之前被拒。"""
        for name in ("sub2apiplus", "prod", "bhv-x", "Bhv", ""):
            with self.subTest(name):
                r = run_script([str(SUITE / "run.sh"), "img"], {"BHV_PROJECT": name or "x y"})
                self.assertEqual(r.returncode, 2)
                self.assertIn("不合法", r.stderr)
        r = run_script([str(SUITE / "stackctl.sh"), "down"], {"BHV_PROJECT": "sub2apiplus"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("不合法", r.stderr)

    def test_stackctl_usage_error_without_docker(self) -> None:
        r = run_script([str(SUITE / "stackctl.sh")], {"BHV_PROJECT": "bhvtest"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("用法", r.stderr)


class IsolationStaticTests(unittest.TestCase):
    """静态核对“不碰现网”：这些约束被改坏时立刻红，不必等到 ARM64 上才发现。"""

    def setUp(self) -> None:
        self.compose = (SUITE / "compose.yml").read_text(encoding="utf-8")
        self.run_sh = (SUITE / "run.sh").read_text(encoding="utf-8")
        self.stackctl = (SUITE / "stackctl.sh").read_text(encoding="utf-8")

    def test_compose_names_are_project_scoped(self) -> None:
        names = re.findall(r"container_name:\s*(\S+)", self.compose)
        self.assertEqual(len(names), 4)
        for n in names:
            self.assertTrue(n.startswith("${BHV_PROJECT}-"), n)
        self.assertIn("name: ${BHV_PROJECT}-net", self.compose)
        for image in re.findall(r"image:\s*(\S+)", self.compose):
            self.assertNotIn("sub2apiplus", image)

    def test_compose_mounts_only_the_work_directory(self) -> None:
        mounts = re.findall(r"^\s+-\s+(\S+):/", self.compose, flags=re.M)
        self.assertTrue(mounts)
        for m in mounts:
            if m.startswith("/var/lib/postgresql"):
                continue   # tmpfs，不是宿主机目录
            self.assertTrue(m.startswith("${BHV_WORKDIR}/"), m)
        self.assertNotIn("docker.sock", self.compose)
        for banned in ("network_mode", "privileged", "pid: host", "cap_add"):
            self.assertNotIn(banned, self.compose)
        # 网关端口只绑本机回环
        self.assertIn('"127.0.0.1:${BHV_PORT}:8080"', self.compose)

    def test_stackctl_only_removes_project_prefixed_things(self) -> None:
        body = self.stackctl.split("cmd_down()", 1)[1].split("cmd_status()", 1)[0]
        self.assertIn('--filter "name=^${P}-"', body)
        self.assertIn('docker network rm "${P}-net"', body)
        self.assertIn('docker rmi -f "${P}-gw:run" "${P}-mock:1"', body)
        self.assertNotIn("sub2apiplus", body)
        self.assertNotIn("prune", self.stackctl)
        self.assertNotIn("sub2apiplus", self.stackctl.replace("sub2apiplus-test-ca", ""))

    def test_production_is_only_read_by_the_snapshot(self) -> None:
        """run.sh 里提到现网容器的地方只有快照函数：docker inspect 与只读事务 psql。"""
        lines = [l for l in self.run_sh.splitlines() if "docker exec" in l or "docker restart" in l or "docker stop" in l or "docker kill" in l
                 or "docker rm" in l or "docker cp" in l]
        for l in lines:
            if "docker run --rm" in l:
                continue
            self.assertIn("default_transaction_read_only=on", l, l)
        snap = self.run_sh.split("prod_snapshot() {", 1)[1].split("\n}\n", 1)[0]
        self.assertNotRegex(snap, r"\b(insert|update|delete|drop|truncate|alter)\b", "快照 SQL 只许 select")
        self.assertIn("select 'rows_users'", snap)

    def test_disk_guard_defaults(self) -> None:
        self.assertIn("BHV_MAX_USE_PCT:-69", self.run_sh)
        self.assertIn("BHV_MIN_AVAIL_GIB:-40", self.run_sh)

    def test_run_sh_prints_exit_line_on_every_path(self) -> None:
        self.assertIn('echo "EXIT=${RC}"', self.run_sh)
        self.assertIn("trap on_exit EXIT", self.run_sh)


README = (SUITE / "README.md").read_text(encoding="utf-8") if (SUITE / "README.md").exists() else ""


class ScenarioTableTests(unittest.TestCase):
    def test_ids_and_fields(self) -> None:
        sc = scenarios.SCENARIOS
        self.assertEqual([s.id for s in sc], ["S%d" % i for i in range(1, 9)])
        self.assertEqual(len({s.key for s in sc}), len(sc))
        for s in sc:
            with self.subTest(s.id):
                self.assertIn(s.budget, ("default", "tight"))
                self.assertTrue(s.criteria.strip() and s.accident.strip() and s.title.strip())
                self.assertRegex(s.key, r"^[a-z0-9-]+$")
                self.assertIn(s.id, scenarios.RUNNERS)
                self.assertTrue(callable(scenarios.RUNNERS[s.id]))
        self.assertEqual(set(scenarios.RUNNERS), set(scenarios.SCENARIO_IDS))

    def test_task_checklist_is_covered(self) -> None:
        """任务文档要求的 8 类场景都在表里（按标题关键词核对，防止删场景）。"""
        text = " ".join(s.title + s.criteria for s in scenarios.SCENARIOS)
        for needle in ("只发 error", "3MiB", "预算", "失效项", "压缩项", "已输出内容", "并发", "账号状态"):
            self.assertIn(needle, text)
        by_id = {s.id: s for s in scenarios.SCENARIOS}
        self.assertEqual(by_id["S3"].budget, "tight")
        self.assertTrue(by_id["S3"].required, "预算压紧场景是事故直接复现场景，必须“必现”")
        self.assertIn("websocket_request_memory_rejected", by_id["S3"].accident)

    def test_readme_table_matches_render_table(self) -> None:
        m = re.search(r"<!-- scenario-table:begin -->\n(.*?)\n<!-- scenario-table:end -->", README, flags=re.S)
        self.assertIsNotNone(m, "README 缺少场景表标记")
        self.assertEqual(m.group(1).strip(), scenarios.render_table().strip(),
                         "README 场景表与 scenarios.render_table() 不一致；重新生成：python3 -B scenarios.py --print-table")

    def test_readme_mentions_the_essentials(self) -> None:
        for needle in ("目的", "前置", "用法", "场景", "判据", "清理", "已知限制", "run.sh", "--expect accident",
                       "EXIT=", "websocket_request_memory_rejected", "remote_run.sh"):
            self.assertIn(needle, README)

    def test_print_table_cli(self) -> None:
        r = subprocess.run([sys.executable, "-B", str(SUITE / "scenarios.py"), "--print-table"], capture_output=True, text=True,
                           env=_ENV, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), scenarios.render_table().strip())


def masked_client_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """按 RFC 6455 构造一个带掩码的客户端帧。"""
    mask = bytes([1, 2, 3, 4])
    head = bytearray([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head.append(0x80 | n)
    elif n < 65536:
        head.append(0x80 | 126)
        head += n.to_bytes(2, "big")
    else:
        head.append(0x80 | 127)
        head += n.to_bytes(8, "big")
    return bytes(head) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


class MockPlanTests(unittest.TestCase):
    """假上游 plan_response：纯函数，覆盖各个应答模式。"""

    def setUp(self) -> None:
        self.bad = lib.cipher("bad")
        self.ok = lib.cipher("ok")
        self.items = [lib.msg("user", "q"), lib.reasoning("a", enc=self.ok), lib.foreign("b", enc=self.bad), lib.msg("user", "q2")]
        self.frame = lib.frame("sid", self.items)

    def plan(self, **ctl):
        return wsmock.plan_response(self.frame, ctl, "resp_x")

    def types(self, plan):
        return [e["type"] for e in plan["events"]]

    def test_no_reject_completes(self) -> None:
        p = self.plan()
        self.assertEqual(p["action"], "complete")
        self.assertEqual(self.types(p)[-1], "response.completed")
        self.assertFalse(p["disconnect"])

    def test_unrelated_reject_list_still_completes(self) -> None:
        self.assertEqual(self.plan(reject_ciphers=[lib.cipher("other")])["action"], "complete")

    def test_warmup_frame_completes_without_output(self) -> None:
        fr = dict(self.frame, generate=False)
        p = wsmock.plan_response(fr, {"reject_ciphers": [self.bad]}, "r")
        # 预热帧里带了被拒密文照样被拒（假上游只看密文），但无密文的预热帧只回 created + completed
        self.assertEqual(p["action"], "reject_invalid_encrypted_content")
        fr2 = dict(lib.frame("sid", [lib.msg("user", "q")]), generate=False)
        p2 = wsmock.plan_response(fr2, {}, "r")
        self.assertEqual([e["type"] for e in p2["events"]], ["response.created", "response.completed"])
        self.assertEqual(p2["events"][-1]["response"]["usage"]["total_tokens"], 0)

    def test_default_reject_is_error_only_real_upstream_shape(self) -> None:
        """默认形态＝真实上游：只发 error，没有 response.created、没有 response.failed，也不断开连接。"""
        p = self.plan(reject_ciphers=[self.bad])
        self.assertEqual(p["action"], "reject_invalid_encrypted_content")
        self.assertEqual(self.types(p), ["error"])
        self.assertFalse(p["disconnect"])
        err = p["events"][0]
        self.assertEqual(err["status"], 400)
        self.assertEqual(err["error"]["code"], "invalid_encrypted_content")
        self.assertEqual(err["error"]["type"], "invalid_request_error")
        self.assertEqual(err["error"]["message"], "The encrypted content %s...%s could not be verified. Reason: Encrypted content could not be decrypted or parsed."
                         % (self.bad[:4], self.bad[-4:]))
        self.assertEqual(p["rejected"], wsmock.digest(self.bad))

    def test_emit_failed_adds_terminal_event(self) -> None:
        p = self.plan(reject_ciphers=[self.bad], emit_failed=True)
        self.assertEqual(self.types(p), ["error", "response.failed"])
        self.assertEqual(p["events"][1]["response"]["error"]["code"], "invalid_encrypted_content")

    def test_lifecycle_and_output_first(self) -> None:
        self.assertEqual(self.types(self.plan(reject_ciphers=[self.bad], lifecycle_first=True)), ["response.created", "error"])
        p = self.plan(reject_ciphers=[self.bad], output_first=True)
        self.assertEqual(self.types(p), ["response.created", "response.output_text.delta", "error"])

    def test_toolitem_first(self) -> None:
        p = self.plan(reject_ciphers=[self.bad], toolitem_first=True)
        self.assertEqual(self.types(p), ["response.created", "response.output_item.added", "error"])
        self.assertEqual(p["events"][1]["item"]["type"], "function_call")

    def test_close_after_error_disconnects(self) -> None:
        self.assertTrue(self.plan(reject_ciphers=[self.bad], close_after_error=True)["disconnect"])

    def test_hint_forms(self) -> None:
        def msg(**ctl):
            return self.plan(reject_ciphers=[self.bad], **ctl)["events"][0]["error"]["message"]
        self.assertIn(self.bad, msg(hint="exact"))
        self.assertNotIn(self.bad[:4], msg(hint="none"))
        self.assertIn("%s...%s" % (self.bad[:2], self.bad[-2:]), msg(hint="short"))
        self.assertIn("%s…%s" % (self.bad[:6], self.bad[-6:]), msg(hint="unicode", hint_len=6))
        self.assertIn("%s...%s" % (self.bad[:8], self.bad[-8:]), msg(hint="ellipsis", hint_len=8))
        other = lib.cipher("other")
        self.assertIn(other[:4], msg(hint_override=other))

    def test_first_matching_cipher_in_input_order_is_reported(self) -> None:
        bad2 = lib.cipher("bad2")
        items = [lib.foreign("x", enc=bad2), lib.foreign("y", enc=self.bad)]
        p = wsmock.plan_response(lib.frame("s", items), {"reject_ciphers": [self.bad, bad2]}, "r")
        self.assertEqual(p["rejected"], wsmock.digest(bad2))

    def test_drop_next(self) -> None:
        p = self.plan(drop_next=1)
        self.assertEqual((p["action"], p["disconnect"], p["consume_drop"], p["events"]), ("drop_connection", True, True, []))
        self.assertEqual(self.plan(drop_next=0)["action"], "complete")
        warm = dict(self.frame, generate=False)
        self.assertEqual(wsmock.plan_response(warm, {"drop_next": 1}, "r")["action"], "complete")
        empty = lib.frame("s", [])
        self.assertEqual(wsmock.plan_response(empty, {"drop_next": 1}, "r")["action"], "complete")

    def test_summarize_input_never_contains_cipher_plaintext(self) -> None:
        summary = wsmock.summarize_input(self.items)
        blob = json.dumps(summary)
        self.assertNotIn(self.bad, blob)
        self.assertNotIn(self.ok, blob)
        row = [r for r in summary if r.get("enc") == wsmock.digest(self.bad)][0]
        self.assertEqual(row["enc_len"], len(self.bad))
        self.assertEqual(row["content"], "list1")
        self.assertEqual(wsmock.summarize_input("not a list"), [])

    def test_parse_http_head(self) -> None:
        head = b"GET /v1/responses HTTP/1.1\r\nHost: x\r\nUpgrade: websocket\r\nSec-WebSocket-Key: abc\r\n\r\n"
        method, path, headers = wsmock.parse_http_head(head)
        self.assertEqual((method, path, headers["upgrade"], headers["sec-websocket-key"]), ("GET", "/v1/responses", "websocket", "abc"))


class MockSocketTests(unittest.TestCase):
    """在 socketpair 上跑真实的 serve_ws / handle_response_create（不开网络端口）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self._old = (wsmock.DATA, wsmock.PORT)
        wsmock.configure(self._tmp.name)
        self.addCleanup(lambda: wsmock.configure(self._old[0], self._old[1]))
        self.server, self.client = socket.socketpair()
        self.client.settimeout(3.0)
        self.addCleanup(self.server.close)
        self.addCleanup(self.client.close)

        def serve() -> None:
            try:
                wsmock.serve_ws(self.server, 1, {})
            except (ConnectionError, OSError):
                pass   # 对端关闭时 serve_ws 以 ConnectionError 退出，真实服务里由 handle_conn 兜住
        self.thread = threading.Thread(target=serve, daemon=True)
        self.thread.start()

    def read_log(self):
        with open(wsmock.LOG_PATH, encoding="utf-8") as f:
            return [json.loads(l) for l in f]

    def send_frame(self, frame_obj) -> None:
        self.client.sendall(masked_client_frame(json.dumps(frame_obj, ensure_ascii=False).encode()))

    def read_event(self):
        buf = b""
        # 服务端帧不带掩码
        while len(buf) < 2:
            buf += self.client.recv(2 - len(buf))
        length = buf[1] & 0x7F
        extra = 0
        if length == 126:
            extra = 2
        elif length == 127:
            extra = 8
        ext = b""
        while len(ext) < extra:
            ext += self.client.recv(extra - len(ext))
        if extra:
            length = int.from_bytes(ext, "big")
        payload = b""
        while len(payload) < length:
            payload += self.client.recv(length - len(payload))
        return buf[0] & 0x0F, json.loads(payload.decode()) if (buf[0] & 0x0F) == 1 else payload

    def test_error_only_then_connection_stays_open(self) -> None:
        bad = lib.cipher("sock-bad")
        wsmock.save_ctl({"reject_ciphers": [bad]})
        self.send_frame(lib.frame("sid", [lib.msg("user", "q"), lib.foreign("z", enc=bad)]))
        opcode, ev = self.read_event()
        self.assertEqual((opcode, ev["type"]), (1, "error"))
        # 真实上游形态：error 之后既没有终态也没有关闭帧
        self.client.settimeout(0.4)
        with self.assertRaises(socket.timeout):
            self.client.recv(1)
        self.client.settimeout(3.0)
        # 同一连接再发一个不含被拒密文的帧，仍能正常完成
        self.send_frame(lib.frame("sid", [lib.msg("user", "q2")]))
        types = []
        while True:
            _, ev = self.read_event()
            types.append(ev["type"])
            if ev["type"] == "response.completed":
                break
        self.assertEqual(types[0], "response.created")
        recs = self.read_log()
        frames = [r for r in recs if r["kind"] == "frame"]
        self.assertEqual([f["action"] for f in frames], ["reject_invalid_encrypted_content", "complete"])
        self.assertTrue(Path(frames[0]["frame_file"]).is_file())
        self.assertGreater(frames[0]["frame_bytes"], 100)
        self.assertNotIn(bad, json.dumps(frames))   # 日志里没有密文原文

    def test_drop_next_closes_tcp_without_reply(self) -> None:
        wsmock.save_ctl({"drop_next": 1})
        self.send_frame(lib.frame("sid", [lib.msg("user", "q")]))
        self.assertEqual(self.client.recv(10), b"")   # 对端直接关闭，没有任何应答
        self.assertEqual(wsmock.load_ctl()["drop_next"], 0)

    def test_client_close_is_logged_and_echoed(self) -> None:
        self.client.sendall(masked_client_frame(b"\x03\xe8", opcode=0x8))
        opcode, payload = self.read_event()
        self.assertEqual(opcode, 0x8)
        self.thread.join(timeout=3)
        recs = self.read_log()
        self.assertEqual(recs[-1]["kind"], "ws_close_from_client")
        self.assertEqual(recs[-1]["code"], 1000)

    def test_unmask_matches_bytewise_for_large_payload(self) -> None:
        payload = bytes(range(256)) * 1000 + b"xyz"
        mask = bytes([9, 8, 7, 6])
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.assertEqual(wsmock.unmask(masked, mask), payload)
        self.assertEqual(wsmock.unmask(b"", mask), b"")


class ClientFrameTests(unittest.TestCase):
    """WSClient.send_json 的掩码实现：三种长度档位都能被服务端正确解码。"""

    def roundtrip(self, obj):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        b.settimeout(5.0)
        c = wsclient.WSClient("k", port=1)
        c.sock = a
        t = threading.Thread(target=c.send_json, args=(obj,), daemon=True)
        t.start()
        fin, opcode, payload = wsmock.ws_read_frame(b)
        t.join(timeout=5)
        self.assertTrue(fin)
        self.assertEqual(opcode, 1)
        return json.loads(payload.decode())

    def test_small_medium_large(self) -> None:
        for size in (10, 5000, 300000):
            with self.subTest(size):
                obj = {"type": "response.create", "x": "界" * size}
                self.assertEqual(self.roundtrip(obj), obj)

    def test_read_message_parses_server_frames(self) -> None:
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        c = wsclient.WSClient("k", port=1)
        c.sock = a
        b.sendall(wsmock.ws_encode(1, json.dumps({"type": "error"}).encode()) + wsmock.ws_encode(8, b"\x03\xe8bye"))
        self.assertEqual(c.read_message(), ("text", json.dumps({"type": "error"})))
        self.assertEqual(c.read_message(), ("close", (1000, "bye")))

    def test_default_port_comes_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {"BHV_PORT": "34567"}):
            self.assertEqual(wsclient.WSClient("k").port, 34567)


# 取自 ARM64 实测日志的真实行形态（v0.2.18-4 / v0.2.18-5）
LINE_REJECT = ("2026-10-30T18:46:24.259+0800\tINFO\tservice/openai_ws_forwarder_logutil.go:563\t[OpenAI WS Mode][openai_ws_mode=true] "
               "ingress_ws_encrypted_recovery stage=upstream_reject client_request_id=c54753e5 account_id=1 turn=1 model=gpt-6.1-sol "
               "candidates=14 hint=ellipsis matched=1 marked=2 located=structural result=recorded reason=fragment attempts=1 output_committed=false\t{\"service\": \"sub2api\"}")
LINE_TURN_END = "2026-10-30T18:48:59.469+0800\tINFO\t[OpenAI WS Mode] ingress_ws_encrypted_recovery stage=turn_end account_id=1 turn=1 close_status=1000"
LINE_ENTRY = "x ingress_ws_encrypted_recovery stage=entry client_request_id=fd5ef0c4 account_id=1 known_digests=2"
LINE_STRIP = "x ingress_ws_invalid_encrypted_lineage_strip account_id=1 turn=1 stripped_items=2"
LINE_MARK = "x ingress_ws_invalid_encrypted_lineage_mark account_id=1 turn=1 digests=2"
LINE_MEMREJ = ("2026-10-30T18:46:28.403+0800\tINFO\thandler/openai_gateway_handler.go:2669\topenai.websocket_request_memory_rejected\t"
               "{\"service\": \"sub2api\", \"path\": \"/v1/responses\"}")
LINE_PROXY = "2026-10-30T16:31:01.301+0800 openai.websocket_proxy_failed \t{\"service\": \"sub2api\"}\tinput was modified"
LINE_CLOSED = "2026-10-30T18:48:59.471+0800\tINFO\thandler/openai_gateway_handler.go:3619\topenai.websocket_ingress_closed_normally"


class LogParsingTests(unittest.TestCase):
    def test_kv(self) -> None:
        d = lib.kv(LINE_REJECT)
        self.assertEqual((d["stage"], d["marked"], d["located"], d["result"], d["output_committed"]), ("upstream_reject", "2", "structural", "recorded", "false"))
        self.assertEqual(d["candidates"], "14")

    def test_counters(self) -> None:
        lines = [LINE_REJECT, LINE_MEMREJ, LINE_MEMREJ, LINE_PROXY, LINE_CLOSED, LINE_TURN_END]
        self.assertEqual(lib.count_memory_rejected(lines), 2)
        self.assertEqual(lib.count_proxy_failed(lines), 1)
        self.assertEqual(lib.count_closed_normally(lines), 1)
        self.assertEqual(lib.count_memory_rejected([LINE_REJECT]), 0)

    def test_lineage_classification(self) -> None:
        lg = lib.lineage([LINE_REJECT, LINE_TURN_END, LINE_ENTRY, LINE_STRIP, LINE_MARK, LINE_PROXY, LINE_MEMREJ])
        self.assertEqual(lg["reject"][0]["marked"], "2")
        self.assertEqual(lg["turn_end"][0]["close_status"], "1000")
        self.assertEqual(lg["entry"][0]["known_digests"], "2")
        self.assertEqual(lg["strip"][0]["stripped_items"], "2")
        self.assertEqual(lg["mark"][0]["digests"], "2")
        self.assertEqual(len(lg["proxy_failed"]), 1)
        self.assertNotIn("memory", json.dumps(lg))   # 内存准入拒绝不属于恢复事件，不混进 lineage

    def test_brief_trims_json_tail(self) -> None:
        self.assertNotIn("service", lib.brief(LINE_REJECT))


class HistoryTests(unittest.TestCase):
    def test_ciphers_are_deterministic_and_shaped(self) -> None:
        self.assertEqual(lib.cipher("a"), lib.cipher("a"))
        self.assertNotEqual(lib.cipher("a"), lib.cipher("b"))
        self.assertTrue(lib.cipher("a").startswith("gAAAAB"))
        self.assertEqual(len(lib.short_cipher("a")), 38)
        self.assertEqual(len(lib.cipher("a", 20)), 20)

    def test_build_history_shape(self) -> None:
        h = lib.build_history("t1", bad=6, valid=8)
        bad_items = [i for i in h.items if i.get("encrypted_content") in h.bad]
        valid_items = [i for i in h.items if i.get("encrypted_content") in h.valid]
        self.assertEqual((len(h.bad), len(h.valid), len(bad_items), len(valid_items)), (6, 8, 6, 8))
        self.assertTrue(all(i["content"] and "id" not in i and i["summary"] == [] for i in bad_items), "失效项＝别家空骨架推理项")
        self.assertTrue(all("content" not in i and i["id"].startswith("rs_") and i["summary"] for i in valid_items), "有效项＝OpenAI 自家推理项")
        self.assertEqual(sum(1 for i in h.items if i["type"] == "compaction"), 1)
        self.assertEqual(h.items[-1]["role"], "user")
        calls = [i for i in h.items if i["type"] == "function_call"]
        outs = [i for i in h.items if i["type"] == "function_call_output"]
        self.assertEqual([c["call_id"] for c in calls], [o["call_id"] for o in outs])
        self.assertEqual(len(set(h.bad) | set(h.valid) | {h.cmp_enc}), 6 + 8 + 1, "密文互不相同")

    def test_small_history_still_has_exactly_one_compaction(self) -> None:
        for bad, valid in ((1, 3), (0, 2), (2, 11)):
            with self.subTest((bad, valid)):
                h = lib.build_history("t2", bad=bad, valid=valid)
                self.assertEqual(sum(1 for i in h.items if i["type"] == "compaction"), 1)
        self.assertEqual(sum(1 for i in lib.build_history("t3", bad=0, valid=2, with_compaction=False).items if i["type"] == "compaction"), 0)
        self.assertIsNone(lib.build_history("t3", with_compaction=False).cmp_enc)

    def test_pad_reaches_requested_size(self) -> None:
        for mib in (3.0, 4.0):
            with self.subTest(mib):
                h = lib.build_history("t4", bad=2, valid=11, pad_mib=mib)
                self.assertGreaterEqual(lib.frame_bytes(lib.frame("s", h.items)), mib * lib.MIB)
        small = lib.build_history("t5", bad=2, valid=11)
        self.assertLess(lib.frame_bytes(lib.frame("s", small.items)), 100 * 1024)

    def test_expected_after_recovery_variants(self) -> None:
        h = lib.build_history("t6", bad=2, valid=3)
        deleted = lib.expected_after_recovery(h.items, set(h.bad))
        skeleton = lib.expected_after_recovery(h.items, set(h.bad), keep_skeleton=True)
        self.assertEqual(len(skeleton) - len(deleted), 2, "两个空骨架：删除形态少 2 项，保留形态各留一个空壳")
        self.assertIn(("reasoning", None, "[]", None, None), skeleton)
        for v in h.valid:
            self.assertTrue(any(row[0] == "reasoning" and row[3] == v for row in deleted))
        self.assertFalse(any(row[0] == "reasoning" and row[3] in h.bad for row in deleted + skeleton))
        # 带 id 与摘要的别家项：content 与密文被清，但项保留
        foreign = lib.foreign("keepme", enc=lib.short_cipher("k"))
        out = lib.expected_after_recovery([foreign], {foreign["encrypted_content"]})
        self.assertEqual(len(out), 1)
        self.assertIsNone(out[0][3])
        self.assertIsNone(out[0][4])

    def test_received_view_drops_developer_preamble(self) -> None:
        raw = {"input": [{"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "基础指令"}]}, lib.msg("user", "q")]}
        self.assertEqual(lib.received_view(raw), [("message", "user", "q")])
        raw2 = {"input": [lib.msg("user", "q")]}
        self.assertEqual(lib.received_view(raw2), [("message", "user", "q")])


class SnapshotDiffTests(unittest.TestCase):
    def snap(self, **over):
        row = {"id": 1, "status": "active", "schedulable": True, "error_message": "", "rate_limit_reset_at": None,
               "overload_until": None, "temp_unschedulable_until": None, "updated_at": "t0", "last_used_at": "t0"}
        row.update(over)
        return {"accounts": {"1": row}, "redis": ["sticky:1", "sched:2:openai:forced:v3", "rpm:1"]}

    def test_identical_and_volatile_changes_are_ignored(self) -> None:
        a = self.snap()
        b = self.snap(updated_at="t9", last_used_at="t9")
        b["redis"] = ["sticky:2", "sched:2:openai:single:v9", "rpm:1", "concurrency:3", "usage:x"]
        self.assertEqual(lib.diff_snapshots(a, b), [])

    def test_account_state_changes_are_reported(self) -> None:
        for field, value in (("schedulable", False), ("status", "error"), ("temp_unschedulable_until", "2030-01-01"),
                             ("rate_limit_reset_at", "2030-01-01"), ("overload_until", "2030-01-01"), ("error_message", "boom")):
            with self.subTest(field):
                diffs = lib.diff_snapshots(self.snap(), self.snap(**{field: value}))
                self.assertEqual(len(diffs), 1)
                self.assertIn(field, diffs[0])

    def test_new_health_redis_key_is_reported(self) -> None:
        a = self.snap()
        b = self.snap()
        b["redis"] = a["redis"] + ["apikey_health_breaker:fail:2"]
        diffs = lib.diff_snapshots(a, b)
        self.assertEqual(diffs, ["Redis 新增键: apikey_health_breaker:fail:2"])
        self.assertEqual(lib.diff_snapshots(b, a), ["Redis 消失键: apikey_health_breaker:fail:2"])

    def test_added_or_removed_account_reported(self) -> None:
        a = self.snap()
        b = self.snap()
        b["accounts"]["2"] = dict(b["accounts"]["1"], id=2)
        self.assertTrue(lib.diff_snapshots(a, b))
        self.assertTrue(lib.diff_snapshots(b, a))


class RecorderAndVerdictTests(unittest.TestCase):
    def quiet(self, fn, *a, **k):
        with mock.patch("builtins.print"):
            return fn(*a, **k)

    def test_recorder_counts(self) -> None:
        r = lib.Recorder("X")
        self.quiet(r.check, True, "ok")
        self.quiet(r.check, False, "bad")
        self.quiet(r.check, False, "log bad", "ev", kind="log")
        self.assertEqual((r.fail, r.failed_log_checks()), (2, 1))
        self.assertIsNone(r.signature)
        self.quiet(r.set_signature, 1, "e")
        self.assertIs(r.signature, True)

    def test_log_check_can_be_skipped(self) -> None:
        r = lib.Recorder("X")
        old = scenarios.SKIP_LOG_CHECKS
        self.addCleanup(setattr, scenarios, "SKIP_LOG_CHECKS", old)
        scenarios.SKIP_LOG_CHECKS = True
        self.assertTrue(self.quiet(scenarios.log_check, r, False, "依赖日志", ""))
        self.assertEqual(r.fail, 0)
        scenarios.SKIP_LOG_CHECKS = False
        self.assertFalse(self.quiet(scenarios.log_check, r, False, "依赖日志", ""))
        self.assertEqual((r.fail, r.failed_log_checks()), (1, 1))

    def test_verdict_pass_mode(self) -> None:
        sc = scenarios.SCENARIOS[0]
        r = lib.Recorder("S1")
        self.quiet(r.check, True, "ok")
        self.assertTrue(scenarios.verdict(sc, r, "pass")["ok"])
        self.quiet(r.check, False, "bad")
        v = scenarios.verdict(sc, r, "pass")
        self.assertFalse(v["ok"])
        self.assertEqual(v["assertion_fail"], 1)

    def test_verdict_accident_mode_requires_signature_only_for_required(self) -> None:
        required = [s for s in scenarios.SCENARIOS if s.required][0]
        optional = [s for s in scenarios.SCENARIOS if not s.required][0]
        r = lib.Recorder("X")
        self.assertFalse(scenarios.verdict(required, r, "accident")["ok"], "必现场景没有事故特征＝未复现")
        self.quiet(r.set_signature, True, "memory_rejected=4")
        self.assertTrue(scenarios.verdict(required, r, "accident")["ok"])
        r2 = lib.Recorder("Y")
        self.assertTrue(scenarios.verdict(optional, r2, "accident")["ok"], "参考场景不强求")

    def test_conclusion_helpers(self) -> None:
        good = {"error": {"type": "error", "status": 400, "error": {"code": "invalid_encrypted_content", "message": "The encrypted content a...b could not be verified."}}}
        self.assertTrue(scenarios.first_error_intact(good))
        bad_status = json.loads(json.dumps(good))
        bad_status["error"]["status"] = 500
        self.assertFalse(scenarios.first_error_intact(bad_status))
        self.assertFalse(scenarios.first_error_intact({"error": None}))
        self.assertTrue(scenarios.closed_1000({"closed": (1000, "x")}))
        self.assertFalse(scenarios.closed_1000({"closed": (1013, "x")}))
        self.assertFalse(scenarios.closed_1000({"closed": None}))
        self.assertTrue(scenarios.held({"held_after_error": True, "closed": None}))
        self.assertFalse(scenarios.held({"held_after_error": True, "closed": (1000, "")}))


class RecoveryChecksTests(unittest.TestCase):
    """用合成的会话数据检验“通用恢复判据”能分清健康与事故两种形态（不碰网络、docker）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = Path(self._tmp.name)
        (self.work / "mock-data" / "frames").mkdir(parents=True)
        patcher = mock.patch.dict(os.environ, {"BHV_WORKDIR": str(self.work)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.hist = lib.build_history("rc", bad=2, valid=3)

    def error_attempt(self, closed=(1000, "encrypted content rejected by upstream, please reconnect"), held=False, hs="101"):
        err = {"type": "error", "status": 400, "error": {"type": "invalid_request_error", "code": "invalid_encrypted_content",
                                                          "message": "The encrypted content ab...cd could not be verified. Reason: x"}}
        return {"events": ["error"], "closed": closed, "completed": None, "error": err, "hs": hs, "send_err": None,
                "ms": 10, "held_after_error": held, "client_request_id": "c"}

    def ok_attempt(self):
        return {"events": ["response.created", "response.completed"], "closed": None, "completed": "resp_1", "error": None, "hs": "101",
                "send_err": None, "ms": 10, "held_after_error": False, "client_request_id": "c"}

    def session(self, attempts, gw_lines, final_items=None):
        s = lib.Session("sid", attempts, "t0", 0)
        s.gw_lines = gw_lines
        s.frames = []
        if final_items is not None:
            path = self.work / "mock-data" / "frames" / "1-2.json"
            path.write_text(json.dumps(lib.frame("sid", final_items), ensure_ascii=False), encoding="utf-8")
            s.frames = [{"action": "complete", "frame_file": str(path)}]
        return s

    def final_items(self):
        # 上游收到的最终帧：失效项密文被清（空壳保留），其余原样
        out = []
        for it in self.hist.items:
            if it.get("encrypted_content") in self.hist.bad:
                out.append({"type": "reasoning", "summary": []})
            else:
                out.append(it)
        return out

    def run_checks(self, session, **kw):
        r = lib.Recorder("T")
        with mock.patch("builtins.print"):
            facts = scenarios.recovery_checks(r, session, self.hist, "", **kw)
        return r, facts

    def test_healthy_session_passes_everything(self) -> None:
        s = self.session([self.error_attempt(), self.ok_attempt()], [LINE_TURN_END, LINE_REJECT], self.final_items())
        r, facts = self.run_checks(s, strict=True)
        self.assertEqual(r.fail, 0, [row for row in r.rows if not row[0]])
        self.assertEqual(facts["rejected"], 0)

    def test_accident_session_fails_the_expected_checks(self) -> None:
        """v0.2.18-4 的事故形态：error 后连接不关闭、重连被内存准入拒绝、5 次用尽。"""
        held = self.error_attempt(closed=None, held=True)
        rejected = {"events": [], "closed": (1013, "websocket request memory budget is temporarily exhausted; retry later"), "completed": None,
                    "error": None, "hs": "101", "send_err": None, "ms": 5, "held_after_error": False, "client_request_id": "c"}
        s = self.session([held] + [rejected] * 4, [LINE_MEMREJ] * 4 + [LINE_REJECT])
        r, facts = self.run_checks(s)
        failed = " | ".join(row[1] for row in r.rows if not row[0])
        self.assertIn("关闭码 1000", failed)
        self.assertIn("response.completed", failed)
        self.assertIn("websocket_request_memory_rejected", failed)
        self.assertEqual(facts["rejected"], 4)
        self.assertGreaterEqual(r.fail, 4)
        self.assertTrue(scenarios.held(s.attempts[0]))

    def test_leftover_bad_cipher_in_final_frame_fails(self) -> None:
        s = self.session([self.error_attempt(), self.ok_attempt()], [LINE_TURN_END], self.hist.items)   # 没清
        r, _ = self.run_checks(s)
        self.assertTrue(any("最终帧" in row[1] and not row[0] for row in r.rows))

    def test_modified_valid_history_fails_strict_comparison(self) -> None:
        items = self.final_items()
        items.insert(1, lib.msg("user", "网关凭空多出来的一条消息"))
        s = self.session([self.error_attempt(), self.ok_attempt()], [LINE_TURN_END], items)
        r, _ = self.run_checks(s, strict=True)
        self.assertTrue(any("逐项等于预期" in row[1] and not row[0] for row in r.rows))

    def test_proxy_failed_and_missing_turn_end_log(self) -> None:
        s = self.session([self.error_attempt(), self.ok_attempt()], [LINE_PROXY], self.final_items())
        r, _ = self.run_checks(s)
        failed = [row[1] for row in r.rows if not row[0]]
        self.assertTrue(any("proxy_failed" in x for x in failed))
        self.assertTrue(any("turn_end" in x for x in failed))
        self.assertEqual(r.failed_log_checks(), 1)


class SetupDataTests(unittest.TestCase):
    def test_fake_jwt_is_three_segment_and_carries_account_id(self) -> None:
        import base64
        token = setup_data.fake_jwt("user-x", "acct-1")
        parts = token.split(".")
        self.assertEqual(len(parts), 3)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        self.assertEqual(claims["https://api.openai.com/auth"]["chatgpt_account_id"], "acct-1")

    def test_script_uses_only_suite_named_rows(self) -> None:
        src = (SUITE / "setup_data.py").read_text(encoding="utf-8")
        self.assertIn('NAME_PREFIX = "bhvacc-"', src)
        self.assertNotRegex(src, r"\b(delete|update)\s+(from\s+)?(accounts|users|groups)\b", "只往一次性测试库插入，不改已有行")


if __name__ == "__main__":
    unittest.main()
