"""Codex 升级开工检查补的三项（优化第 14 项）：模型可用性探测、main 上 Security Scan 先核绿（``tools/ci/opening_checks.py``）、
入口判定 atomic-double“沿用”时核对时效窗口（``tools/ci/entry_steps.py``）。

用“模型已下线”和“atomic 记录过时效”两种情形复现，开工检查当场拦下：

* 模型探测用本机 http.server 当中转：列表里有模型时通过，模型被下线（列表里没有）、列表拉不到、最小请求被拒都不通过；密钥文件权限不对拒绝读取；
* Security Scan：``gh`` 注入替身，红／没跑完／过期／对应的不是 main 当前头／查不到都不通过，绿且新鲜才通过；
  本机没装 gh、gh 起不来、调用超时同样不通过，原因是看得懂的中文（真实起子进程复现：ARM64 现场没装 gh 时原来是一屏 Python 回溯），退出码仍是失败关闭的 1；
* atomic-double：步骤记录通过且输入没变时，窗口内沿用，超窗口改为重做并写明原因；窗口取自 VC-1 阶段预算减去签发余量，测试核对与计时账本的预算表一致；
* 入口便宜检查脚本 ``entry-preflight.sh`` 接入这两项检查，没配置也算未通过（要跳过必须显式写明）。
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from tools.ci import entry_steps as es
from tools.ci import opening_checks as oc
from tools.official_client_capture.tests.test_ci_entry_steps import Fixture, _step

REPO_ROOT = Path(__file__).resolve().parents[3]
HEAD = "a" * 40


class FakeRelay(BaseHTTPRequestHandler):
    models: list[str] = []
    respond_status = 200
    seen: list[tuple[str, str, str | None]] = []

    def log_message(self, *args) -> None:   # 静默
        pass

    def do_GET(self) -> None:
        self.seen.append(("GET", self.path, self.headers.get("Authorization")))
        if self.path.endswith("/models") and not self.path.startswith("/nothing"):
            body = json.dumps({"data": [{"id": name} for name in self.models]}).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        self.seen.append(("POST", self.path, payload.get("model")))
        self.send_response(self.respond_status)
        self.end_headers()
        self.wfile.write(b'{"error":"model_not_found"}' if self.respond_status != 200 else b"{}")


class ModelProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeRelay.models, FakeRelay.respond_status, FakeRelay.seen = ["gpt-main", "gpt-lite"], 200, []
        self.server = HTTPServer(("127.0.0.1", 0), FakeRelay)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v1"
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.key_file = Path(directory.name) / "key"
        self.key_file.write_text("sk-test-key\n", encoding="utf-8")
        self.key_file.chmod(0o600)

    def test_models_present_pass_and_the_key_is_sent_as_bearer(self) -> None:
        self.assertEqual(oc.check_models(self.base, oc.read_key(self.key_file), ["gpt-main", "gpt-lite"]), [])
        self.assertEqual(FakeRelay.seen[0], ("GET", "/v1/models", "Bearer sk-test-key"))

    def test_a_model_that_was_taken_offline_blocks_the_opening(self) -> None:
        FakeRelay.models = ["gpt-lite"]
        problems = oc.check_models(self.base, "k", ["gpt-main", "gpt-lite"])
        self.assertEqual(len(problems), 1)
        self.assertIn("gpt-main", problems[0])
        self.assertIn("已下线", problems[0])

    def test_codex_catalog_style_lists_are_understood(self) -> None:
        self.assertEqual(oc.listed_models({"models": [{"slug": "gpt-main"}, {"slug": "gpt-lite"}]}), {"gpt-main", "gpt-lite"})
        self.assertEqual(oc.listed_models({"data": [{"id": "a"}], "models": [{"name": "b"}]}), {"a", "b"})
        self.assertEqual(oc.listed_models("不是对象"), set())

    def test_unreachable_relay_is_a_failure_not_a_pass(self) -> None:
        problems = oc.check_models("http://127.0.0.1:9/v1", "k", ["gpt-main"], timeout=1)
        self.assertEqual(len(problems), 1)
        self.assertIn("拉不到", problems[0])

    def test_non_200_listing_and_empty_listing_fail(self) -> None:
        self.assertIn("HTTP 404", oc.check_models(self.base.replace("/v1", "/nothing"), "k", ["gpt-main"])[0])
        FakeRelay.models = []
        self.assertTrue(any("空的" in problem for problem in oc.check_models(self.base, "k", ["gpt-main"])))

    def test_minimal_request_confirms_the_model_really_answers(self) -> None:
        self.assertEqual(oc.check_models(self.base, "k", ["gpt-main"], minimal=True), [])
        self.assertIn(("POST", "/v1/responses", "gpt-main"), FakeRelay.seen)
        FakeRelay.respond_status = 404
        problems = oc.check_models(self.base, "k", ["gpt-main"], minimal=True)
        self.assertEqual(len(problems), 1)
        self.assertIn("最小请求被拒绝", problems[0])

    def test_key_file_must_be_private_regular_and_non_empty(self) -> None:
        self.key_file.chmod(0o644)
        with self.assertRaisesRegex(oc.CheckError, "0600"):
            oc.read_key(self.key_file)
        self.key_file.chmod(0o600)
        link = self.key_file.with_name("link")
        link.symlink_to(self.key_file)
        with self.assertRaises(oc.CheckError):
            oc.read_key(link)
        self.key_file.write_text("  \n", encoding="utf-8")
        with self.assertRaisesRegex(oc.CheckError, "空"):
            oc.read_key(self.key_file)

    def test_cli_exit_codes_distinguish_failure_from_missing_configuration(self) -> None:
        arguments = ["models", "--base-url", self.base, "--key-file", str(self.key_file), "--models", "gpt-main,gpt-lite"]
        self.assertEqual(oc.main(arguments), oc.EXIT_OK)
        FakeRelay.models = ["gpt-lite"]
        self.assertEqual(oc.main(arguments), oc.EXIT_FAILED)
        self.assertEqual(oc.main(["models", "--base-url", "", "--models", "gpt-main"]), oc.EXIT_UNCONFIGURED)


def gh_replacement(runs, *, head=HEAD, run_code=0, head_code=0):
    def gh(argv):
        if argv[0] == "run":
            return run_code, json.dumps(runs) if run_code == 0 else "boom"
        return head_code, head + "\n"
    return gh


def scan_run(**overrides):
    return {"status": "completed", "conclusion": "success", "headSha": HEAD, "createdAt": "2026-10-25T08:00:00Z", "databaseId": 1, "url": "u", **overrides}


class SecurityScanTests(unittest.TestCase):
    NOW = time.mktime(time.strptime("2026-10-25 10:00:00", "%Y-%m-%d %H:%M:%S")) - time.timezone

    def check(self, runs, **kw):
        return oc.check_security_scan("itv3/sub2apiplus", gh=gh_replacement(runs, **kw), now=self.NOW)

    def test_green_and_fresh_on_the_current_head_passes(self) -> None:
        self.assertEqual(self.check([scan_run()]), [])

    def test_red_scan_blocks_the_opening(self) -> None:
        problems = self.check([scan_run(conclusion="failure")])
        self.assertIn("是红的", problems[0])
        self.assertIn("先修好再开工", problems[0])

    def test_unfinished_stale_or_other_commit_scans_do_not_count(self) -> None:
        self.assertIn("还没跑完", self.check([scan_run(status="in_progress", conclusion=None)])[0])
        self.assertIn("超过时效", self.check([scan_run(createdAt="2026-10-20T08:00:00Z")])[0])
        self.assertIn("没有对应的 Security Scan 运行", self.check([scan_run(headSha="b" * 40)])[0])
        self.assertIn("没有 Security Scan 运行记录", self.check([])[0])

    def test_gh_failures_are_failures(self) -> None:
        self.assertIn("查不到", self.check([scan_run()], run_code=1)[0])
        self.assertIn("头提交", self.check([scan_run()], head_code=1)[0])
        self.assertIn("头提交", self.check([scan_run()], head="短")[0])

    def test_the_repository_must_be_given_explicitly(self) -> None:
        self.assertEqual(oc.main(["security-scan", "--repo", ""]), oc.EXIT_UNCONFIGURED)


class GhUnavailableTests(unittest.TestCase):
    """gh 没装／起不来／超时（ARM64 现场验收发现：没装 gh 时开工检查是一屏 Python 回溯、退出码 1，失败关闭但看不懂）。

    三种都是“检查不通过”：原因是中文、写明怎么办（装好并登录 gh，或显式跳过并写理由），退出码仍是失败关闭的 1。全部真实起子进程，PATH 指向临时目录里的假环境。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()

    @staticmethod
    def path(value: str):
        return mock.patch.dict(os.environ, {"PATH": value})

    def test_a_missing_gh_is_a_readable_not_passed_with_the_way_out(self) -> None:
        with self.path(str(self.bin)):   # PATH 里只有一个空目录：找不到 gh
            problems = oc.check_security_scan("itv3/sub2apiplus")
        self.assertEqual(len(problems), 1, problems)
        for needle in ("本机没有 gh", "Security Scan 一项无法核对", "装好并登录 gh", "OPENING_CHECKS_SKIP=main-security-scan",
                       "OPENING_CHECKS_SKIP_REASON", "写明在别处核对过"):
            self.assertIn(needle, problems[0])

    def test_the_command_line_prints_the_reason_and_keeps_the_failure_exit_code(self) -> None:
        out = io.StringIO()
        with self.path(str(self.bin)), contextlib.redirect_stdout(out):
            code = oc.main(["security-scan", "--repo", "itv3/sub2apiplus"])
        self.assertEqual(code, oc.EXIT_FAILED, "未通过是 1，不是“没配置”的 3；仍然失败关闭")
        self.assertIn("本机没有 gh", out.getvalue())

    def test_the_field_reproduction_prints_no_traceback(self) -> None:
        """ARM64 现场的原样复现：PATH 里没有 gh 时直接运行脚本——退出码 1，标准输出是中文原因，标准错误里没有回溯。"""

        completed = subprocess.run([sys.executable, "-B", str(REPO_ROOT / "tools" / "ci" / "opening_checks.py"), "security-scan", "--repo", "itv3/sub2apiplus"],
                                   env={"PATH": str(self.bin), "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(completed.returncode, 1, completed.stderr)
        self.assertIn("本机没有 gh", completed.stdout)
        self.assertEqual(completed.stderr, "", "标准错误里没有回溯")

    def test_a_gh_that_hangs_is_reported_with_the_timeout_seconds(self) -> None:
        (self.bin / "gh").write_text("#!/bin/sh\nexec sleep 30\n", encoding="utf-8")
        (self.bin / "gh").chmod(0o755)
        started = time.monotonic()
        with self.path(f"{self.bin}:/bin:/usr/bin"), mock.patch.object(oc, "GH_TIMEOUT_SECONDS", 0.5):   # 假 gh 用 exec sleep 卡住，PATH 里要找得到 sleep
            problems = oc.check_security_scan("itv3/sub2apiplus")
        self.assertLess(time.monotonic() - started, 20, "超时后进程被杀，不会等满 30 秒")
        self.assertEqual(len(problems), 1, problems)
        for needle in ("gh run list", "超时", "0.5 秒", "OPENING_CHECKS_SKIP=main-security-scan", "OPENING_CHECKS_SKIP_REASON"):
            self.assertIn(needle, problems[0])

    def test_a_gh_that_cannot_be_executed_is_reported_too(self) -> None:
        (self.bin / "gh").write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
        (self.bin / "gh").chmod(0o644)   # 在 PATH 里但没有执行权限
        with self.path(str(self.bin)):
            problems = oc.check_security_scan("itv3/sub2apiplus")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("gh 运行不起来", problems[0])
        self.assertIn("OPENING_CHECKS_SKIP=main-security-scan", problems[0])

    def test_an_unavailable_gh_is_a_problem_on_either_call(self) -> None:
        """两次 gh 调用（运行列表、当前头提交）哪一次不可用都转成“没过”，不往外抛。"""

        def first_call(argv):
            raise oc.GhUnavailable("第一次调用不可用")

        def second_call(argv):
            if argv[0] == "run":
                return 0, json.dumps([scan_run()])
            raise oc.GhUnavailable("第二次调用超时")

        self.assertEqual(oc.check_security_scan("itv3/sub2apiplus", gh=first_call), ["第一次调用不可用"])
        self.assertEqual(oc.check_security_scan("itv3/sub2apiplus", gh=second_call, now=SecurityScanTests.NOW), ["第二次调用超时"])


class AtomicReuseWindowTests(unittest.TestCase):
    def test_the_window_table_matches_the_ledger_stage_budgets(self) -> None:
        from tools.official_client_capture import codex_upgrade_timing_ledger as ledger

        for client, budgets in ledger.DEFAULT_STAGE_BUDGETS_BY_CLIENT.items():
            self.assertEqual(es.ATOMIC_STAGE_BUDGET_MINUTES[client], budgets["VC-1"], client)
        self.assertEqual(es.reuse_window_minutes("atomic-double", "codex"), 75 - es.ATOMIC_ISSUE_MARGIN_MINUTES)
        self.assertEqual(es.reuse_window_minutes("atomic-double", "claude"), 120 - es.ATOMIC_ISSUE_MARGIN_MINUTES)
        self.assertIsNone(es.reuse_window_minutes("ledger", "codex"), "只有 atomic-double 有时效窗口")

    def test_age_beyond_the_window_forces_a_redo_with_the_reason(self) -> None:
        now = 1_800_000_000.0
        for age_minutes, stale in ((10, False), (44, False), (46, True), (500, True)):
            with self.subTest(age_minutes=age_minutes):
                problems = es.reuse_window_problems("atomic-double", "codex", completed_at_utc=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - age_minutes * 60)), now=now)
                self.assertEqual(bool(problems), stale, problems)
                if stale:
                    self.assertIn("时效窗口", problems[0])
                    self.assertIn("发布认证", problems[0])

    def test_unreadable_or_future_completion_times_do_not_pass(self) -> None:
        now = 1_800_000_000.0
        self.assertTrue(es.reuse_window_problems("atomic-double", "codex", completed_at_utc="昨天", now=now))
        self.assertTrue(es.reuse_window_problems("atomic-double", "codex", completed_at_utc=None, now=now))
        future = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 3600))
        self.assertTrue(es.reuse_window_problems("atomic-double", "codex", completed_at_utc=future, now=now))

    def test_other_steps_have_no_window(self) -> None:
        self.assertEqual(es.reuse_window_problems("ledger", "codex", completed_at_utc="2020-01-01T00:00:00Z", now=1_800_000_000.0), [])


class AtomicReuseWindowEndToEndTests(unittest.TestCase):
    """验收修正建议 13：``evaluate_step`` 到“重做”的端到端——步骤记录通过、输入没变，窗口内判“沿用”，记录超窗口判“重做”并写明原因，下游随之重做。
    用入口步骤的真实夹具（真实写入、真实封存的步骤记录），只把 atomic-double 记录的完成时间改老并重新封存。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.fx = Fixture(Path(directory.name).resolve())
        self.fx.record_all()

    def age_atomic_record(self, minutes: float) -> None:
        path = es.record_path(self.fx.steps_dir, "atomic-double")
        record = json.loads(path.read_text(encoding="utf-8"))
        record = {key: value for key, value in record.items() if key != "record_sha256"}
        record["completed_at_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - minutes * 60))
        es._write_json_atomic(path, es.seal(record))

    def test_inside_the_window_the_step_is_reused(self) -> None:
        self.age_atomic_record(10)
        result = self.fx.evaluate()
        self.assertEqual(_step(result, "atomic-double")["decision"], "reuse", _step(result, "atomic-double")["reasons"])

    def test_outside_the_window_the_step_is_redone_with_the_reason_and_the_downstream_follows(self) -> None:
        window = es.reuse_window_minutes("atomic-double", "codex")
        self.age_atomic_record(window + 15)
        result = self.fx.evaluate()
        atomic = _step(result, "atomic-double")
        self.assertEqual(atomic["decision"], "run", "超窗口：重做")
        self.assertTrue(any("超过" in reason and "分钟" in reason for reason in atomic["reasons"]), atomic["reasons"])
        self.assertEqual(atomic["changed_inputs"], [], "输入没变——重做完全是因为记录太老")
        self.assertIn("atomic-double", _step(result, "release-certification")["upstream_rerun"], "依赖它的发布认证随之重做")
        self.assertEqual(es.exit_code(result), 1)

    def test_the_boundary_minute_decides_and_the_other_steps_have_no_window(self) -> None:
        window = es.reuse_window_minutes("atomic-double", "codex")
        self.age_atomic_record(window - 5)
        self.assertEqual(_step(self.fx.evaluate(), "atomic-double")["decision"], "reuse")
        self.age_atomic_record(window + 5)
        self.assertEqual(_step(self.fx.evaluate(), "atomic-double")["decision"], "run")
        # 其它步骤的记录再老也不因时效重做（它们没有窗口）。
        self.assertEqual(_step(self.fx.evaluate(), "preflight-plan")["decision"], "reuse")

    def test_the_printed_evaluation_names_the_redo_and_its_reason(self) -> None:
        self.age_atomic_record(es.reuse_window_minutes("atomic-double", "codex") + 30)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            es.print_evaluation(self.fx.evaluate())
        line = next(line for line in buffer.getvalue().splitlines() if "atomic-double" in line and "（atomic-double 收据）" in line)
        self.assertIn("重做", line)


class CaptureReceiptTests(unittest.TestCase):
    """起跑前的本机完整测试收据（优化第 4 项）：收据的形状、封口与核对规则（后台验证与入口便宜检查共用）。"""

    COMMIT = "a" * 40

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name).resolve() / "receipts"

    def write(self, **changes) -> Path:
        arguments = dict(commit=self.COMMIT, exit_code=0, started_at_utc="2026-10-29T01:00:00Z", finished_at_utc="2026-10-29T01:48:00Z", tree_clean=True)
        arguments.update(changes)
        return oc.write_capture_receipt(self.dir, **arguments)

    def test_a_complete_green_run_passes_and_the_file_is_private_and_sealed(self) -> None:
        path = self.write(host="mac", log_sha256="f" * 64, log_tail="OK")
        self.assertEqual(oc.check_capture_receipt(self.dir, self.COMMIT), [])
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((value["command"], value["exit_code"], value["duration_seconds"]), (["make", "test-capture-tools"], 0, 2880))
        self.assertTrue(oc.receipt_sealed(value))

    def test_every_rule_refuses_with_a_reason_that_says_what_to_do(self) -> None:
        self.assertIn("没有提交", oc.check_capture_receipt(self.dir, self.COMMIT)[0])
        self.assertIn("40 位", oc.check_capture_receipt(self.dir, "abc")[0])
        for label, change, expected in (("红", dict(exit_code=1, log_tail="FAIL: x\nFAILED (failures=1)"), "没有通过（退出码 1）"),
                                        ("定向命令", dict(command=["make", "test-capture-tools-x"]), "不是完整目标"),
                                        ("脏树", dict(tree_clean=False), "工作树不干净"),
                                        ("太短", dict(finished_at_utc="2026-10-29T01:01:00Z"), "低于 120 秒")):
            with self.subTest(label):
                self.write(**change)
                problems = oc.check_capture_receipt(self.dir, self.COMMIT)
                self.assertTrue(any(expected in problem for problem in problems), problems)
        self.write()
        path = oc.capture_receipt_path(self.dir, self.COMMIT)
        value = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**value, "host": "被改过"}), encoding="utf-8")
        self.assertTrue(any("封口" in problem for problem in oc.check_capture_receipt(self.dir, self.COMMIT)))
        path.unlink()
        path.symlink_to(self.dir / "elsewhere.json")
        self.assertIn("符号链接", oc.check_capture_receipt(self.dir, self.COMMIT)[0])

    def test_writer_rejects_bad_commit_and_times(self) -> None:
        with self.assertRaises(oc.CheckError):
            self.write(commit="HEAD")
        with self.assertRaises(oc.CheckError):
            self.write(finished_at_utc="2026-10-29T00:00:00Z")
        with self.assertRaises(oc.CheckError):
            self.write(started_at_utc="昨天")

    def test_command_line_round_trip_and_exit_codes(self) -> None:
        log = self.dir.parent / "run.log"
        log.write_text("a\nb\nOK\n", encoding="utf-8")
        base = [sys.executable, "-B", str(REPO_ROOT / "tools" / "ci" / "opening_checks.py")]
        wrote = subprocess.run([*base, "capture-receipt-write", "--dir", str(self.dir), "--commit", self.COMMIT, "--exit-code", "0", "--started", "2026-10-29T01:00:00Z",
                                "--finished", "2026-10-29T01:48:00Z", "--tree-clean", "true", "--log", str(log)], capture_output=True, text=True)
        self.assertEqual(wrote.returncode, 0, wrote.stderr)
        value = json.loads(oc.capture_receipt_path(self.dir, self.COMMIT).read_text(encoding="utf-8"))
        self.assertEqual(value["log_sha256"], hashlib.sha256(log.read_bytes()).hexdigest())
        self.assertEqual(value["log_tail"], "a\nb\nOK")
        self.assertEqual(oc.summarize_log("x\nFAIL: test_a (m.C.test_a)\n" + "y\n" * 20 + "ERROR: test_b\nRan 9 tests\n\nFAILED (failures=1)"),
                         "FAIL: test_a (m.C.test_a)\nERROR: test_b\nRan 9 tests\n\nFAILED (failures=1)", "红的用例名与汇总行都进摘要")
        ok = subprocess.run([*base, "capture-receipt-check", "--dir", str(self.dir), "--commit", self.COMMIT], capture_output=True, text=True)
        self.assertEqual((ok.returncode, ok.stdout.strip()), (0, "通过"))
        bad = subprocess.run([*base, "capture-receipt-check", "--dir", str(self.dir), "--commit", "b" * 40], capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("没有提交", bad.stdout)


class TuiReceiptTests(unittest.TestCase):
    """新版二进制 TUI 零请求启动预探收据（优化第 5 项）。"""

    VERSION = "9.8.7"
    SHA = "b" * 64

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.dir = Path(directory.name).resolve() / "receipts"

    def write(self, rows=None, **changes) -> Path:
        rows = [{"name": name, "status": "passed"} for name in oc.REQUIRED_TUI_SCENARIOS] if rows is None else rows
        arguments = dict(client_version=self.VERSION, binary_path="/opt/c/bin", binary_sha256=self.SHA, scenarios=rows,
                         started_at_utc="2026-10-29T01:00:00Z", finished_at_utc="2026-10-29T01:05:00Z", host="arm64")
        arguments.update(changes)
        return oc.write_tui_receipt(self.dir, **arguments)

    def test_required_scenarios_are_real_scenarios_of_the_launch_probe(self) -> None:
        spec = importlib.util.spec_from_file_location("launch_probe_for_receipt_test", REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "client_launch_probe.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        self.addCleanup(sys.modules.pop, spec.name, None)
        spec.loader.exec_module(module)
        self.assertEqual(oc.REQUIRED_TUI_SCENARIOS, ("compact-tui", "ws-idle-close-tui", "daemon-tui", "guardian-tui"))
        for name in oc.REQUIRED_TUI_SCENARIOS:
            self.assertIn(name, module.TUI_SCENARIOS)

    def test_all_required_scenarios_passed_with_zero_requests_passes(self) -> None:
        self.write()
        self.assertEqual(oc.check_tui_receipt(self.dir, target_version=self.VERSION, binary_sha256=self.SHA), [])
        self.write(rows=[{"name": name, "status": "passed"} for name in (*oc.REQUIRED_TUI_SCENARIOS, "review-tui")])
        self.assertEqual(oc.check_tui_receipt(self.dir), [], "可选场景登记了也通过")

    def test_missing_failed_optional_failed_and_unbound_receipts_are_refused(self) -> None:
        self.assertIn("没有新版二进制", oc.check_tui_receipt(self.dir)[0])
        rows = [{"name": name, "status": "passed"} for name in oc.REQUIRED_TUI_SCENARIOS]
        self.write(rows=rows[:3])
        self.assertIn("缺少必需场景：guardian-tui", "\n".join(oc.check_tui_receipt(self.dir)))
        self.write(rows=[{**rows[0], "status": "failed", "exit_code": 1, "tail": "line1\nCannot use the shared background server"}, *rows[1:]])
        text = "\n".join(oc.check_tui_receipt(self.dir))
        self.assertIn("场景 compact-tui 预探没通过", text)
        self.assertIn("Cannot use the shared background server", text)
        self.write(rows=[*rows, {"name": "review-tui", "status": "failed", "exit_code": 2}])
        self.assertIn("场景 review-tui 预探没通过", "\n".join(oc.check_tui_receipt(self.dir)))
        self.write()
        self.assertIn("不是本轮目标版本", "\n".join(oc.check_tui_receipt(self.dir, target_version="1.1.1")))
        self.assertIn("二进制摘要", "\n".join(oc.check_tui_receipt(self.dir, binary_sha256="c" * 64)))
        self.write(live_request_count=1)
        self.assertIn("真实请求数是 1", "\n".join(oc.check_tui_receipt(self.dir)))
        path = oc.tui_receipt_path(self.dir)
        self.write()
        value = json.loads(path.read_text(encoding="utf-8"))
        path.write_text(json.dumps({**value, "host": "被改过"}), encoding="utf-8")
        self.assertTrue(any("封口" in problem for problem in oc.check_tui_receipt(self.dir)))

    def test_scenario_spec_parser(self) -> None:
        self.assertEqual(oc.parse_scenario_spec("compact-tui=passed"), {"name": "compact-tui", "status": "passed"})
        self.assertEqual(oc.parse_scenario_spec("compact-tui=failed:1:Cannot: a:b"),
                         {"name": "compact-tui", "status": "failed", "exit_code": 1, "tail": "Cannot: a:b"})
        for bad in ("compact-tui", "=passed", "x=maybe", "x=failed:abc"):
            with self.subTest(bad):
                with self.assertRaises(oc.CheckError):
                    oc.parse_scenario_spec(bad)
        with self.assertRaises(oc.CheckError):
            oc.write_tui_receipt(self.dir, client_version=self.VERSION, binary_path="/b", binary_sha256=self.SHA,
                                 scenarios=[{"name": "a", "status": "passed"}, {"name": "a", "status": "passed"}],
                                 started_at_utc="2026-10-29T01:00:00Z", finished_at_utc="2026-10-29T01:05:00Z")


class LocalCaptureReceiptScriptTests(unittest.TestCase):
    """``local/local-capture-tools-receipt.sh``：在真实临时 git 仓库里用假 make 跑，红绿都如实写收据。"""

    SCRIPT = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "local" / "local-capture-tools-receipt.sh"

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.git("init", "-q")
        (self.repo / "a.txt").write_text("a", encoding="utf-8")
        self.git("add", "-A")
        self.git("-c", "user.name=t", "-c", "user.email=t@t.invalid", "commit", "-q", "-m", "x")
        self.out = self.root / "receipts"

    def git(self, *args: str) -> str:
        return subprocess.run(["git", *args], cwd=self.repo, capture_output=True, text=True, check=True).stdout.strip()

    def fake_make(self, body: str) -> None:
        (self.bin / "make").write_text("#!/bin/bash\n" + body, encoding="utf-8")
        (self.bin / "make").chmod(0o755)

    def run_script(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.SCRIPT), "--out-dir", str(self.out), "--repo", str(self.repo), *extra], capture_output=True, text=True, timeout=120,
                              env={**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "PYTHONDONTWRITEBYTECODE": "1"})

    def test_a_green_run_writes_a_receipt_that_passes_except_for_the_duration_floor(self) -> None:
        self.fake_make('echo "ran $*"\nexit 0\n')
        completed = self.run_script()
        self.assertEqual(completed.returncode, 0, completed.stderr)
        commit = self.git("rev-parse", "HEAD")
        value = json.loads(oc.capture_receipt_path(self.out, commit).read_text(encoding="utf-8"))
        self.assertEqual((value["commit"], value["exit_code"], value["tree_clean"], value["command"]), (commit, 0, True, ["make", "test-capture-tools"]))
        self.assertIn("ran test-capture-tools", (self.out / f"capture-tools-{commit}.log").read_text(encoding="utf-8"))
        self.assertEqual(oc.check_capture_receipt(self.out, commit, min_seconds=0), [], "假 make 瞬间结束：去掉耗时下限后收据本身合法")
        self.assertTrue(any("低于" in problem for problem in oc.check_capture_receipt(self.out, commit)), "真实起跑检查的耗时下限拦得住瞬间结束的假收据")

    def test_a_red_run_is_recorded_as_red_and_the_script_exits_with_make_code(self) -> None:
        self.fake_make('echo "FAIL: test_driver_has_no_version_literals"\nexit 2\n')
        completed = self.run_script()
        self.assertEqual(completed.returncode, 2)
        commit = self.git("rev-parse", "HEAD")
        problems = oc.check_capture_receipt(self.out, commit, min_seconds=0)
        self.assertTrue(any("没有通过（退出码 2）" in problem and "test_driver_has_no_version_literals" in problem for problem in problems), problems)

    def test_a_dirty_tree_before_or_after_the_run_is_recorded(self) -> None:
        self.fake_make("exit 0\n")
        (self.repo / "untracked.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.run_script().returncode, 0)
        commit = self.git("rev-parse", "HEAD")
        self.assertTrue(any("工作树不干净" in problem for problem in oc.check_capture_receipt(self.out, commit, min_seconds=0)))
        (self.repo / "untracked.txt").unlink()
        self.fake_make('echo dirt > "$PWD/created-by-tests.txt"\nexit 0\n')
        self.assertEqual(self.run_script().returncode, 0)
        self.assertTrue(any("工作树不干净" in problem for problem in oc.check_capture_receipt(self.out, commit, min_seconds=0)), "测试过程中弄脏了树也记")

    def test_usage_errors(self) -> None:
        completed = subprocess.run(["bash", str(self.SCRIPT)], capture_output=True, text=True, env={**os.environ, "LOCAL_RECEIPT_DIR": ""})
        self.assertEqual(completed.returncode, 2)
        self.assertIn("收据目录", completed.stderr)


class TuiReceiptScriptTests(unittest.TestCase):
    """``tui-prelaunch-receipt.sh``（ARM64 侧接口）：核对宿主二进制摘要与版本后写收据，verify 用起跑检查同一规则。"""

    SCRIPT = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "tui-prelaunch-receipt.sh"

    def setUp(self) -> None:
        from tools.official_client_capture.tests.test_arm64_capture_driver import _DriverFixture, _stub_phase_context
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.root.chmod(0o700)
        self.fixture = _DriverFixture(self.root)
        self.binary = self.root / "client"
        self.binary.write_text("#!/bin/bash\necho 'client 9.8.7'\n", encoding="utf-8")
        self.binary.chmod(0o755)
        self.sha = hashlib.sha256(self.binary.read_bytes()).hexdigest()
        text = self.fixture.env_file.read_text(encoding="utf-8")
        text = re.sub(r'(?m)^CODEX_BIN=.*$', f'CODEX_BIN="{self.binary}"', text)
        text = re.sub(r'(?m)^CODEX_BIN_SHA256=.*$', f'CODEX_BIN_SHA256="{self.sha}"', text)
        text = re.sub(r'(?m)^TARGET_VERSION=.*$', 'TARGET_VERSION="9.8.7"', text)
        self.fixture.env_file.write_text(text, encoding="utf-8")
        self.drv = self.root / "drv"
        self.drv.mkdir(mode=0o700)
        for name in ("lib.sh", "parse_env.py", "client_table.py", "opening_checks.py", "tui-prelaunch-receipt.sh"):
            (self.drv / name).write_bytes((self.SCRIPT.parent / name).read_bytes())
        _stub_phase_context(self.drv)

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(["bash", str(self.drv / "tui-prelaunch-receipt.sh"), *args], capture_output=True, text=True, timeout=120, cwd=self.root,
                              env={**os.environ, **self.fixture.env})

    def passing(self) -> list[str]:
        return [item for name in oc.REQUIRED_TUI_SCENARIOS for item in ("--scenario", f"{name}=passed")]

    def test_scenarios_lists_the_required_set(self) -> None:
        completed = self.run_script("scenarios")
        self.assertEqual(completed.stdout.split(), list(oc.REQUIRED_TUI_SCENARIOS), completed.stderr)

    def test_write_then_verify_and_a_failed_scenario_is_recorded_but_refused(self) -> None:
        wrote = self.run_script("write", *self.passing(), "--started", "2026-10-29T01:00:00Z", "--finished", "2026-10-29T01:05:00Z")
        self.assertEqual(wrote.returncode, 0, wrote.stdout + wrote.stderr)
        self.assertEqual(self.run_script("verify").returncode, 0)
        path = self.fixture.runroot / "local-receipts" / oc.TUI_RECEIPT_NAME
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        value = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((value["client_version"], value["binary_sha256"], value["live_request_count"]), ("9.8.7", self.sha, 0))
        failed = [item for name in oc.REQUIRED_TUI_SCENARIOS[2:] for item in ("--scenario", f"{name}=passed")]
        self.assertEqual(self.run_script("write", "--scenario", "compact-tui=failed:1:Cannot use the shared background server", "--scenario", "ws-idle-close-tui=failed:1:x",
                                         *failed, "--started", "2026-10-29T01:00:00Z", "--finished", "2026-10-29T01:05:00Z").returncode, 0)
        verified = self.run_script("verify")
        self.assertEqual(verified.returncode, 1)
        self.assertIn("场景 compact-tui 预探没通过", verified.stdout)

    def test_a_binary_that_is_not_the_registered_one_or_a_wrong_version_is_refused_before_writing(self) -> None:
        self.binary.write_text("#!/bin/bash\necho 'client 9.8.7'\n# 被换过\n", encoding="utf-8")
        wrote = self.run_script("write", *self.passing(), "--started", "2026-10-29T01:00:00Z", "--finished", "2026-10-29T01:05:00Z")
        self.assertEqual(wrote.returncode, 1)
        self.assertIn("不等于登记的", wrote.stderr)
        self.assertFalse((self.fixture.runroot / "local-receipts" / oc.TUI_RECEIPT_NAME).exists())


class PreflightScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.text = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-preflight.sh").read_text(encoding="utf-8")

    def test_the_two_new_checks_are_registered_and_use_the_helper(self) -> None:
        self.assertRegex(self.text, r"(?m)^check model-availability model_availability$")
        self.assertRegex(self.text, r"(?m)^check main-security-scan main_security_scan$")
        self.assertIn('opening_checks.py" models', self.text)
        self.assertIn('opening_checks.py" security-scan', self.text)

    def test_unconfigured_checks_fail_unless_explicitly_skipped(self) -> None:
        self.assertIn("OPENING_CHECKS_SKIP", self.text)
        self.assertIn("未配置", self.text)

    def test_the_gh_unavailable_hint_names_the_switches_the_script_really_honours(self) -> None:
        """gh 不可用时告诉操作员的跳过办法，必须是本脚本里真实生效的开关与项目名（改了一边没改另一边就红）。"""

        self.assertIn("opening_skipped main-security-scan", self.text)
        self.assertIn("OPENING_CHECKS_SKIP_REASON", self.text)
        with tempfile.TemporaryDirectory() as empty, mock.patch.dict(os.environ, {"PATH": empty}):
            problems = oc.check_security_scan("itv3/sub2apiplus")
        self.assertEqual(len(problems), 1, problems)
        for switch in ("OPENING_CHECKS_SKIP=main-security-scan", "OPENING_CHECKS_SKIP_REASON"):
            self.assertIn(switch, problems[0])

    def test_driver_carries_identical_copies(self) -> None:
        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
        for name in ("opening_checks.py", "entry_steps.py"):
            with self.subTest(name):
                self.assertEqual((driver / name).read_bytes(), (REPO_ROOT / "tools" / "ci" / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
