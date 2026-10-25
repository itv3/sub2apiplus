"""Codex 升级开工检查补的三项（优化第 14 项）：模型可用性探测、main 上 Security Scan 先核绿（``tools/ci/opening_checks.py``）、
入口判定 atomic-double“沿用”时核对时效窗口（``tools/ci/entry_steps.py``）。

用“模型已下线”和“atomic 记录过时效”两种情形复现，开工检查当场拦下：

* 模型探测用本机 http.server 当中转：列表里有模型时通过，模型被下线（列表里没有）、列表拉不到、最小请求被拒都不通过；密钥文件权限不对拒绝读取；
* Security Scan：``gh`` 注入替身，红／没跑完／过期／对应的不是 main 当前头／查不到都不通过，绿且新鲜才通过；
* atomic-double：步骤记录通过且输入没变时，窗口内沿用，超窗口改为重做并写明原因；窗口取自 VC-1 阶段预算减去签发余量，测试核对与计时账本的预算表一致；
* 入口便宜检查脚本 ``entry-preflight.sh`` 接入这两项检查，没配置也算未通过（要跳过必须显式写明）。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

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

    def test_driver_carries_identical_copies(self) -> None:
        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
        for name in ("opening_checks.py", "entry_steps.py"):
            with self.subTest(name):
                self.assertEqual((driver / name).read_bytes(), (REPO_ROOT / "tools" / "ci" / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
