"""后台验证起跑前置检查（优化第 4 项，``tools/ci/background_preflight.py``）。

四项检查：冻结收据、基线重封、重部署、准入绑定。用两次真实发生过的“起跑顺序错”复现并确认被拦下：

* 顺序错一（先于冻结收据）：源码改了冻结文件，承接收据还没单独提交就起后台验证——冻结覆盖检查报出未承接的路径；
* 顺序错二（先于重部署）：数据根部署的受管树还是旧的，就起后台验证——部署与测试树不一致；
* 两个都错时一次报全；全都对时放行；``background_validation.py start --preflight on`` 在不通过时拒绝起跑，也不停掉已在跑的验证。

冻结覆盖用真实的 ``tools.upstream_merge``（拷进临时仓库），其余依赖用可注入的替身，不碰真实 ARM64。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.ci import background_preflight as pre
from tools.ci import background_validation as bv

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMIT_PLACEHOLDER = "d" * 40


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=验收", "-c", "user.email=a@example.invalid", *args], cwd=root, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def make_frozen_repo(root: Path, *, receipted: bool) -> str:
    """最小的“有冻结台账”的仓库：frozen/data.txt 是冻结文件；receipted 为假时再改一版内容而不补承接收据。返回提交号。"""

    root.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "tools" / "upstream_merge", root / "tools" / "upstream_merge", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    maintenance = root / "docs" / "egress" / "maintenance"
    maintenance.mkdir(parents=True)
    (root / "frozen").mkdir()
    (root / "frozen" / "data.txt").write_text("v1\n", encoding="utf-8")
    receipt = {"schema_version": "official-egress-upstream-freeze-successor/v1",
               "transitions": [{"path": "frozen/data.txt", "reason": "示例承接", "predecessor_sha256": sha("v0\n"), "to_sha256": sha("v1\n")}]}
    (maintenance / "demo-freeze-successor.json").write_text(json.dumps(receipt, ensure_ascii=False), encoding="utf-8")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "冻结台账基线")
    if not receipted:
        (root / "frozen" / "data.txt").write_text("v2 源码已改，承接收据还没提交\n", encoding="utf-8")
        git(root, "commit", "-q", "-am", "改冻结文件")
    return git(root, "rev-parse", "HEAD")


class FreezeReceiptsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()

    def test_covered_tree_passes(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        result = pre.check_freeze_receipts(self.root / "tree")
        self.assertEqual((result["passed"], result["problems"]), (True, []), result)
        self.assertEqual(result["detail"]["uncovered"], [])

    def test_order_error_one_start_before_the_freeze_receipt_is_blocked(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=False)
        result = pre.check_freeze_receipts(self.root / "tree")
        self.assertFalse(result["passed"])
        self.assertEqual(result["detail"]["uncovered"], ["frozen/data.txt"])
        self.assertIn("没有承接收据登记", result["problems"][0])
        self.assertIn("另行提交", result["problems"][0])

    def test_the_real_repository_head_has_no_uncovered_frozen_paths(self) -> None:
        # 反证：算法在真实仓库（主线头，冻结测试全绿）上不报任何未承接路径，不会把正常树误拦。
        result = pre.check_freeze_receipts(REPO_ROOT)
        self.assertEqual(result["detail"].get("uncovered"), [], result["problems"])

    def test_unreadable_result_is_a_failure_not_a_pass(self) -> None:
        broken = SimpleNamespace(returncode=1, stdout="", stderr="Traceback ...")
        result = pre.check_freeze_receipts(self.root, runner=lambda *a, **k: broken)
        self.assertFalse(result["passed"])


class BaselineResealTests(unittest.TestCase):
    def test_runner_result_decides(self) -> None:
        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
        bad = SimpleNamespace(returncode=1, stdout="", stderr="FAIL: test_snapshot\nAssertionError: Codex 身份敏感摘要变化")
        self.assertTrue(pre.check_baseline_reseal(Path("."), "codex", runner=lambda *a, **k: ok)["passed"])
        failed = pre.check_baseline_reseal(Path("."), "codex", runner=lambda *a, **k: bad)
        self.assertFalse(failed["passed"])
        self.assertIn("重封基线", failed["problems"][0])

    def test_each_client_uses_its_own_gate_test_and_unknown_clients_fail_closed(self) -> None:
        calls: list[list[str]] = []

        def runner(argv, **_):
            calls.append(list(argv))
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        pre.check_baseline_reseal(Path("."), "codex", runner=runner)
        pre.check_baseline_reseal(Path("."), "claude", runner=runner)
        self.assertEqual([call[-1] for call in calls], [pre.BASELINE_TESTS["codex"], pre.BASELINE_TESTS["claude"]])
        self.assertFalse(pre.check_baseline_reseal(Path("."), "未知客户端", runner=runner)["passed"])

    def test_a_sequence_of_clients_checks_every_one_and_names_the_failing_client(self) -> None:
        """验收修正建议 12：两客户端都纳管时逐个核对，不只本轮的 ``client``——另一客户端被改而没重封基线也要拦下。"""

        calls: list[str] = []

        def runner(argv, **_):
            calls.append(argv[-1])
            failing = argv[-1] == pre.BASELINE_TESTS["claude"]
            return SimpleNamespace(returncode=1 if failing else 0, stdout="", stderr="AssertionError: Claude 工具身份变化" if failing else "")

        result = pre.check_baseline_reseal(Path("."), ["claude", "codex"], runner=runner)
        self.assertEqual(sorted(calls), sorted(pre.BASELINE_TESTS.values()), "两个客户端的守恒测试都跑了")
        self.assertFalse(result["passed"])
        self.assertTrue(result["problems"][0].startswith("claude："), result["problems"])
        self.assertEqual(result["detail"]["returncodes"], {"claude": 1, "codex": 0})
        self.assertFalse(pre.check_baseline_reseal(Path("."), ["codex", "未知客户端"], runner=runner)["passed"], "任一客户端没登记就 fail-closed")

    def test_the_named_tests_exist_in_the_repository(self) -> None:
        for module in pre.BASELINE_TESTS.values():
            self.assertTrue((REPO_ROOT / Path(*module.split(".")).with_suffix(".py")).is_file(), module)


class FakeSteps:
    """入口步骤模块的替身：部署收据登记的整树摘要、测试树现算的整树摘要与部署一致性各部分都可控。"""

    class EntryStepsError(RuntimeError):
        pass

    class CT:
        @staticmethod
        def spec(_client):
            return {"deploy_receipt_identity_key": None}

    def __init__(self, *, recorded: str, actual: str, mismatch: list[str] | None = None) -> None:
        self.recorded, self.actual, self.mismatch = recorded, actual, mismatch or []

    def latest_deploy_receipt(self, _control, _client):
        return Path("codex-01630-supervisor-enable-20261025t000000z.json"), {"tool_files_sha256": self.recorded}

    def tree_tool_files_sha256(self, _tree, _client, *, pycache=None):
        return self.actual

    def deploy_consistency(self, _tree, _data, _client):
        return list(self.mismatch)


class RedeployTests(unittest.TestCase):
    def test_deployed_tree_equal_to_the_commit_passes(self) -> None:
        steps = FakeSteps(recorded="a" * 64, actual="a" * 64)
        self.assertTrue(pre.check_redeploy(Path("t"), Path("d"), ["codex"], steps=steps)["passed"])

    def test_order_error_two_start_before_redeploy_is_blocked(self) -> None:
        result = pre.check_redeploy(Path("t"), Path("d"), ["codex"], steps=FakeSteps(recorded="a" * 64, actual="b" * 64))
        self.assertFalse(result["passed"])
        self.assertIn("先受监督部署本提交", result["problems"][0])

    def test_tests_guides_or_script_copies_that_differ_also_block(self) -> None:
        result = pre.check_redeploy(Path("t"), Path("d"), ["codex"],
                                    steps=FakeSteps(recorded="a" * 64, actual="a" * 64, mismatch=["tools/official_client_capture/tests"]))
        self.assertFalse(result["passed"])
        self.assertIn("整树摘要相同也不行", result["problems"][0])

    def test_every_managed_client_is_checked(self) -> None:
        steps = FakeSteps(recorded="a" * 64, actual="a" * 64)
        seen: list[str] = []
        original = steps.tree_tool_files_sha256

        def spy(tree, client, *, pycache=None):
            seen.append(client)
            return "b" * 64 if client == "claude" else original(tree, client, pycache=pycache)

        steps.tree_tool_files_sha256 = spy
        result = pre.check_redeploy(Path("t"), Path("d"), ["codex", "claude"], steps=steps)
        self.assertEqual(seen, ["codex", "claude"])
        self.assertFalse(result["passed"])
        self.assertTrue(result["problems"][0].startswith("claude："))


class AdmissionBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data, self.runroot = self.root / "data", self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        self.write_receipt("20261025t000000z", "f")

    def write_receipt(self, stamp: str, files: str) -> None:
        path = self.data / "control" / f"codex-01630-supervisor-enable-{stamp}.json"
        path.write_text(json.dumps({"status": "passed", "tool_files_sha256": files * 64, "policy_version": 7}), encoding="utf-8")

    def test_current_latest_deployment_binds(self) -> None:
        deployment = bv.latest_deployment(self.data)
        result = pre.check_admission_binding(self.runroot, self.data, COMMIT_PLACEHOLDER, "codex", deployment, None)
        self.assertEqual((result["passed"], result["problems"]), (True, []), result)

    def test_a_deployment_that_changed_during_the_check_is_blocked(self) -> None:
        stale = bv.latest_deployment(self.data)
        self.write_receipt("20261025t010000z", "e")   # 检查期间又部署了一次
        result = pre.check_admission_binding(self.runroot, self.data, COMMIT_PLACEHOLDER, "codex", stale, None)
        self.assertFalse(result["passed"])
        self.assertIn("换了", result["problems"][0])

    def test_deployment_set_must_cover_every_managed_client(self) -> None:
        claude = self.data / "control" / "claude-supervisor-enable-20261025t000000z.json"
        claude.write_text(json.dumps({"status": "passed", "identity": {"tool_files_sha256": "c" * 64}}), encoding="utf-8")
        deployment = bv.latest_deployment(self.data)
        with mock.patch.object(bv, "managed_clients", return_value=["codex", "claude"]):
            alone = pre.check_admission_binding(self.runroot, self.data, COMMIT_PLACEHOLDER, "codex", deployment, None, background=bv)
            partial = pre.check_admission_binding(self.runroot, self.data, COMMIT_PLACEHOLDER, "codex", deployment, {"codex": deployment}, background=bv)
        self.assertFalse(alone["passed"])
        self.assertFalse(partial["passed"])
        self.assertIn("缺少已纳管客户端", partial["problems"][0])


class RunChecksTests(unittest.TestCase):
    """四项合在一起：两种顺序错误各自被拦下，两个都错一次报全，全对放行。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data, self.runroot = self.root / "data", self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        (self.data / "control" / "codex-01630-supervisor-enable-20261025t000000z.json").write_text(
            json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.deployment = bv.latest_deployment(self.data)
        self.ok_runner = lambda argv, **kw: (pre._run(argv, **kw) if "-c" in argv else SimpleNamespace(returncode=0, stdout="OK", stderr=""))

    def run_all(self, tree: Path, steps: FakeSteps) -> dict:
        return pre.run_checks(tree=tree, commit=COMMIT_PLACEHOLDER, data_root=self.data, runroot=self.runroot, client="codex",
                              deployment=self.deployment, deployments=None, runner=self.ok_runner, steps=steps)

    def test_all_good_passes(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        report = self.run_all(self.root / "tree", FakeSteps(recorded="a" * 64, actual="a" * 64))
        self.assertTrue(report["passed"], report)
        self.assertEqual([item["name"] for item in report["checks"]], list(pre.CHECK_NAMES))

    def test_reproduces_and_blocks_the_two_historical_wrong_orders(self) -> None:
        make_frozen_repo(self.root / "late-receipt", receipted=False)
        make_frozen_repo(self.root / "good", receipted=True)
        before_freeze = self.run_all(self.root / "late-receipt", FakeSteps(recorded="a" * 64, actual="a" * 64))
        before_redeploy = self.run_all(self.root / "good", FakeSteps(recorded="a" * 64, actual="b" * 64))
        both = self.run_all(self.root / "late-receipt", FakeSteps(recorded="a" * 64, actual="b" * 64))
        self.assertEqual(before_freeze["failed_checks"], ["freeze-receipts"], "先于冻结收据起跑：被拦下")
        self.assertEqual(before_redeploy["failed_checks"], ["redeploy"], "先于重部署起跑：被拦下")
        self.assertEqual(both["failed_checks"], ["freeze-receipts", "redeploy"], "两个都错，一次报全")
        text = pre.format_failures(both)
        self.assertIn("拒绝起跑后台验证", text)
        self.assertIn("[freeze-receipts]", text)
        self.assertIn("[redeploy]", text)

    def test_with_two_managed_clients_the_baseline_reseal_covers_both(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        seen: list[str] = []

        def runner(argv, **kw):
            if "-c" in argv:
                return pre._run(argv, **kw)
            seen.append(argv[-1])
            bad = argv[-1] == pre.BASELINE_TESTS["claude"]
            return SimpleNamespace(returncode=1 if bad else 0, stdout="", stderr="Claude 基线没重封" if bad else "")

        deployments = {"codex": self.deployment, "claude": {**self.deployment, "receipt": "claude-x.json"}}
        report = pre.run_checks(tree=self.root / "tree", commit=COMMIT_PLACEHOLDER, data_root=self.data, runroot=self.runroot, client="codex",
                                deployment=self.deployment, deployments=deployments, only=["baseline-reseal"], runner=runner)
        self.assertEqual(sorted(seen), sorted(pre.BASELINE_TESTS.values()))
        self.assertEqual(report["failed_checks"], ["baseline-reseal"], "本轮是 codex，也被 claude 的基线没重封拦下")

    def test_only_runs_the_named_checks_and_rejects_unknown_names(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        report = pre.run_checks(tree=self.root / "tree", commit=COMMIT_PLACEHOLDER, data_root=self.data, runroot=self.runroot, client="codex",
                                deployment=self.deployment, deployments=None, only=["freeze-receipts"], runner=self.ok_runner)
        self.assertEqual([item["name"] for item in report["checks"]], ["freeze-receipts"])
        with self.assertRaises(pre.PreflightError):
            pre.run_checks(tree=self.root, commit=COMMIT_PLACEHOLDER, data_root=self.data, runroot=self.runroot, client="codex",
                           deployment=self.deployment, deployments=None, only=["不存在"])


class BundleMaterializationTests(unittest.TestCase):
    def test_a_bundle_is_checked_out_to_the_exact_commit_and_cleaned_up(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            commit = make_frozen_repo(root / "src", receipted=True)
            git(root / "src", "bundle", "create", str(root / "x.bundle"), "main")
            data, runroot = root / "data", root / "run"
            (data / "control").mkdir(parents=True)
            runroot.mkdir()
            (data / "control" / "codex-01630-supervisor-enable-20261025t000000z.json").write_text(
                json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
            deployment = bv.latest_deployment(data)
            report = pre.preflight_with_bundle(bundle=root / "x.bundle", branch="main", commit=commit, data_root=data, runroot=runroot,
                                               client="codex", deployment=deployment, deployments=None, scratch_parent=root / "scratch",
                                               only=["freeze-receipts"])
            self.assertTrue(report["passed"], report)
            self.assertEqual(list((root / "scratch").iterdir()), [], "临时树用完即删")
            with self.assertRaises(pre.PreflightError):
                pre.preflight_with_bundle(bundle=root / "x.bundle", branch="main", commit="0" * 40, data_root=data, runroot=runroot,
                                          client="codex", deployment=deployment, deployments=None, scratch_parent=root / "scratch")
            self.assertEqual(list((root / "scratch").iterdir()), [], "出错时临时树同样清掉")


class StartIntegrationTests(unittest.TestCase):
    """``start --preflight on``：不通过就拒绝起跑（不启动后台、不停掉已在跑的验证）；通过则照常起。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.data, self.runroot = self.root / "data", self.root / "run"
        (self.data / "control").mkdir(parents=True)
        self.runroot.mkdir()
        self.receipt = self.data / "control" / "codex-01630-supervisor-enable-20261025t000000z.json"
        self.receipt.write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        (self.root / "x.bundle").write_bytes(b"isolated-bundle")
        self.gates = self.root / "entry-gates.sh"
        self.gates.write_text("#!/bin/bash\nsleep 30\n", encoding="utf-8")
        self.addCleanup(lambda: bv.stop(self.runroot, "测试收尾"))
        # background_validation 按路径另载一份预检模块；测试让它用被测的这一个模块对象，才能替换其中的检查。
        patcher = mock.patch.object(bv, "_preflight_module", return_value=pre)
        patcher.start()
        self.addCleanup(patcher.stop)

    def args(self, commit: str, preflight: str | None = "off") -> SimpleNamespace:
        """``preflight=None`` 表示命令行里不写 --preflight，吃 Python 的缺省（on）。沿用一律显式关掉：这里测的是前置检查。"""

        return bv._parse(["start", "--runroot", str(self.runroot), "--data-root", str(self.data), "--bundle", str(self.root / "x.bundle"), "--branch", "main",
                          "--commit", commit, "--entry-gates", str(self.gates), "--reuse", "off", *(["--preflight", preflight] if preflight else [])])

    def failing_report(self) -> dict:
        return {"schema_version": pre.SCHEMA, "passed": False, "failed_checks": ["redeploy"], "commit": "x", "client": "codex",
                "checks": [{"name": "redeploy", "passed": False, "problems": ["数据根部署的受管树不是本提交"]}]}

    def test_failed_preflight_refuses_to_start_and_leaves_no_conclusion_or_process(self) -> None:
        with mock.patch.object(pre, "preflight_with_bundle", return_value=self.failing_report()):
            with self.assertRaises(bv.ValidationError) as caught:
                bv.start(self.args("a" * 40, "on"))
        self.assertIn("拒绝起跑", str(caught.exception))
        self.assertEqual(bv.results(self.runroot), [], "没有生成任何结论文件")
        saved = list((self.runroot / bv.DIRECTORY / "preflight").glob("*.json"))
        self.assertEqual(len(saved), 1, "报告留档")
        self.assertFalse(json.loads(saved[0].read_text(encoding="utf-8"))["passed"])

    def test_failed_preflight_does_not_stop_a_validation_already_running(self) -> None:
        started = bv.start(self.args("a" * 40))
        self.assertEqual(started["action"], "started")
        with mock.patch.object(pre, "preflight_with_bundle", return_value=self.failing_report()):
            with self.assertRaises(bv.ValidationError):
                bv.start(self.args("b" * 40, "on"))
        statuses = [bv.effective_status(payload) for _path, payload in bv.results(self.runroot)]
        self.assertEqual(statuses, ["running"], "拒绝起跑不能连累已在跑的那一轮")

    def test_passed_preflight_starts_normally(self) -> None:
        passing = {**self.failing_report(), "passed": True, "failed_checks": [], "checks": []}
        with mock.patch.object(pre, "preflight_with_bundle", return_value=passing) as preflight:
            started = bv.start(self.args("a" * 40, "on"))
        self.assertEqual(started["action"], "started")
        self.assertEqual(preflight.call_args.kwargs["commit"], "a" * 40)

    def test_explicit_off_skips_the_preflight(self) -> None:
        with mock.patch.object(pre, "preflight_with_bundle") as preflight:
            started = bv.start(self.args("a" * 40, "off"))
        self.assertEqual(started["action"], "started")
        preflight.assert_not_called()

    def test_the_python_command_line_defaults_to_on_so_a_direct_call_cannot_silently_skip_it(self) -> None:
        """验收修正建议 12：Python 命令行的 start 与驱动入口同一缺省——前置检查缺省 on，不写 --preflight 也被拦。"""

        self.assertEqual(self.args("a" * 40, None).preflight, "on")
        with mock.patch.object(pre, "preflight_with_bundle", return_value=self.failing_report()) as preflight:
            with self.assertRaises(bv.ValidationError) as caught:
                bv.start(self.args("a" * 40, None))
        self.assertIn("拒绝起跑", str(caught.exception))
        preflight.assert_called_once()
        self.assertEqual(bv.results(self.runroot), [])

    def test_the_wrapper_maps_the_no_flags_to_explicit_off_and_defaults_stay_on(self) -> None:
        text = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "background-validate.sh").read_text(encoding="utf-8")
        start = text.index("  start)\n")
        block = text[start + len("  start)\n"):text.index(";;", start)]

        # 真实执行 start 那一段 shell：取出 shift 3 之后的参数处理与 exec，看实际传给 Python 的词。
        tail = block[block.index("bundle=$1; branch=$2; commit=$3; shift 3"):]

        def run(*extra: str) -> list[str]:
            script = ("RUNROOT=/r; D=/d; ARM64_VC_ENV=/e; BV=(printf '%s\\n'); CT_CLIENT_ARGV=()\n"
                      f"set -- /b main {'a' * 40} " + " ".join(extra) + "\n" + tail)
            completed = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            return completed.stdout.split()

        default = run()
        self.assertEqual(default[default.index("--preflight") + 1], "on")
        self.assertEqual(default[default.index("--reuse") + 1], "auto")
        skipped = run("--no-preflight", "--no-reuse")
        self.assertEqual(skipped[skipped.index("--preflight") + 1], "off", "--no-preflight 要显式传 off：Python 缺省已经是 on")
        self.assertEqual(skipped[skipped.index("--reuse") + 1], "off")
        self.assertNotIn("--no-preflight", skipped)
        self.assertNotIn("--no-reuse", skipped)


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_identical_copies(self) -> None:
        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
        for name in ("background_preflight.py", "background_validation.py"):
            with self.subTest(name):
                self.assertEqual((driver / name).read_bytes(), (REPO_ROOT / "tools" / "ci" / name).read_bytes())


if __name__ == "__main__":
    unittest.main()
