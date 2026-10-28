"""后台验证起跑前置检查（优化第 4 项，``tools/ci/background_preflight.py``）。

七项检查：冻结收据、基线重封、重部署、准入绑定、主机工具链（host-toolchain）、本机完整测试收据（local-capture-receipt）、TUI 预探收据（tui-prelaunch-receipt）。
用三次真实发生过的“起跑条件不对”复现并确认被拦下，另有第六、七项针对上一轮入口门禁两次白跑的复现（见 ``ReceiptChecksTests``）：

* 顺序错一（先于冻结收据）：源码改了冻结文件，承接收据还没单独提交就起后台验证——冻结覆盖检查报出未承接的路径；
* 顺序错二（先于重部署）：数据根部署的受管树还是旧的，就起后台验证——部署与测试树不一致；
* 环境不匹配（后台验证 #1）：采集主机 Go 是 1.27.2，被验证提交的 backend/go.mod 已要求 1.27.3，``GOTOOLCHAIN=local`` 的出站单元退出 2、整轮白跑——主机工具链检查拦下；
* 几个都错时一次报全；全都对时放行；``background_validation.py start --preflight on`` 在不通过时拒绝起跑，也不停掉已在跑的验证。

冻结覆盖用真实的 ``tools.upstream_merge``（拷进临时仓库），其余依赖用可注入的替身，不碰真实 ARM64。主机工具链检查用临时目录里的假 go 可执行脚本充当执行器的 PATH，
不碰真实 Go、不联网。
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.ci import background_preflight as pre
from tools.ci import background_validation as bv
from tools.ci import entry_gates as eg
from tools.ci import opening_checks as oc

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMIT_PLACEHOLDER = "d" * 40
TARGET = "9.8.7"   # 测试用的假目标版本（驱动与测试夹具里不写真实版本字面量）
BINARY_SHA = "b" * 64


def seed_receipts(runroot: Path, commit: str = COMMIT_PLACEHOLDER, *, capture: bool = True, tui: bool = True, capture_exit: int = 0,
                  tui_failed: tuple[str, ...] = (), skip_scenarios: tuple[str, ...] = ()) -> Path:
    """往运行根的收据目录放一对合法的收据（可按需做坏）：本机完整测试收据与 TUI 预探收据。"""

    directory = runroot / oc.RECEIPT_DIR_NAME
    if capture:
        oc.write_capture_receipt(directory, commit=commit, exit_code=capture_exit, started_at_utc="2026-10-29T01:00:00Z",
                                 finished_at_utc="2026-10-29T01:48:00Z", host="mac", tree_clean=True, log_tail="OK")
    if tui:
        scenarios = [{"name": name, "status": "failed" if name in tui_failed else "passed", **({"exit_code": 1, "tail": "Cannot use the shared background server"}
                                                                                             if name in tui_failed else {})}
                     for name in oc.REQUIRED_TUI_SCENARIOS if name not in skip_scenarios]
        oc.write_tui_receipt(directory, client_version=TARGET, binary_path="/opt/client/bin", binary_sha256=BINARY_SHA, scenarios=scenarios,
                             started_at_utc="2026-10-29T01:00:00Z", finished_at_utc="2026-10-29T01:05:00Z", host="arm64")
    return directory
# 假 go 探测时记下的环境变量名（见 write_fake_go）：用来核对探测环境里放行了什么、强制了什么、挡掉了什么。
FAKE_GO_LOGGED_NAMES = ("PATH", "LANG", "HOME", "GOTOOLCHAIN", "GOPROXY", "GOFLAGS", "GOCACHE", "CGO_ENABLED", "STRAY", "PYTHONPATH")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=验收", "-c", "user.email=a@example.invalid", *args], cwd=root, check=True,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL).stdout.strip()


def make_frozen_repo(root: Path, *, receipted: bool) -> str:
    """最小的“有冻结台账”的仓库：frozen/data.txt 是冻结文件；receipted 为假时再改一版内容而不补承接收据。返回提交号。

    树里另带一份 ``backend/go.mod``（要求 go 1.27.3），让它同时是第五项主机工具链检查能读的“被验证提交”；冻结覆盖检查只看台账里登记的路径，不受它影响。"""

    root.mkdir(parents=True)
    shutil.copytree(REPO_ROOT / "tools" / "upstream_merge", root / "tools" / "upstream_merge", ignore=shutil.ignore_patterns("__pycache__", "tests"))
    (root / "tools" / "__init__.py").write_text("", encoding="utf-8")
    (root / "backend").mkdir()
    (root / "backend" / "go.mod").write_text("module example.invalid/backend\n\ngo 1.27.3\n", encoding="utf-8")
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


def write_fake_go(directory: Path, stdout: str = "go version go1.27.3 linux/arm64", *, exit_code: int = 0, stderr: str = "",
                  log: Path | None = None, delay: float | None = None) -> Path:
    """在 ``directory`` 里写一个假的 go 可执行脚本，返回它的路径。

    脚本是 POSIX sh，只用 shell 内建命令（echo、printf、pwd、exit），所以执行器 PATH 里只放这个目录也能跑。``log`` 不为空时，每次运行把参数、当前目录和
    ``FAKE_GO_LOGGED_NAMES`` 里各变量的值（没设置记为 ``<unset>``）追加进去，用来核对探测环境；``delay`` 不为空时先 ``exec sleep``（模拟 go 卡住，
    调用方的 PATH 里要找得到 sleep）。"""

    lines = ["#!/bin/sh"]
    if log is not None:
        lines.append("{")
        lines.append('  echo "args=$*"')
        lines.append('  echo "pwd=$(pwd)"')
        lines.extend(f'  echo "{name}=${{{name}-<unset>}}"' for name in FAKE_GO_LOGGED_NAMES)
        lines.append(f"}} >> {shlex.quote(str(log))}")
    if delay is not None:
        lines.append(f"exec sleep {delay}")
    if stdout:
        lines.append(f"printf '%s\\n' {shlex.quote(stdout)}")
    if stderr:
        lines.append(f"printf '%s\\n' {shlex.quote(stderr)} >&2")
    lines.append(f"exit {exit_code}")
    path = Path(directory) / "go"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def read_fake_go_log(log: Path) -> dict[str, str]:
    """假 go 记下的最后一次探测：变量名（以及 args、pwd）→ 值。"""

    values: dict[str, str] = {}
    for line in log.read_text(encoding="utf-8").splitlines():
        key, _, value = line.partition("=")
        values[key] = value
    return values


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


class HostToolchainTests(unittest.TestCase):
    """第五项 ``host-toolchain``：执行器单元用的 go 不得低于被验证提交 ``backend/go.mod`` 的 ``go`` 指令，判不了一律失败关闭。

    背景（后台验证 #1）：采集主机 ``/usr/local/go`` 是 1.27.2，上游合并把 go.mod 抬到 1.27.3，``GOTOOLCHAIN=local`` 的两个 make 出站单元退出 2，整轮白跑。
    这里用临时目录里的假 go 脚本充当“执行器 PATH”复现各种主机；不碰真实 Go，也不联网。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.bin = self.root / "go-bin"
        self.bin.mkdir()
        self.tree = self.root / "tree"
        (self.tree / "backend").mkdir(parents=True)
        self.log = self.root / "go-probe.log"
        self.write_go_mod("module example.invalid/backend\n\ngo 1.27.3\n")

    def write_go_mod(self, text: str) -> None:
        (self.tree / "backend" / "go.mod").write_text(text, encoding="utf-8")

    def install_go(self, version: str | None = "1.27.3", **kwargs) -> Path:
        """装一个报告 ``go version go<version> linux/arm64`` 的假 go；``version`` 给 None 时输出由关键字 ``stdout`` 指定。每次探测都记进 ``self.log``。"""

        if version is not None:
            kwargs.setdefault("stdout", f"go version go{version} linux/arm64")
        return write_fake_go(self.bin, log=self.log, **kwargs)

    def check(self, **kwargs) -> dict:
        kwargs.setdefault("exec_path", str(self.bin))
        kwargs.setdefault("environ", {})
        return pre.check_host_toolchain(self.tree, **kwargs)

    def test_the_incident_host_older_than_go_mod_is_blocked_with_a_full_explanation(self) -> None:
        go = self.install_go("1.27.2")
        result = self.check()
        self.assertEqual((result["name"], result["passed"]), ("host-toolchain", False))
        self.assertEqual(len(result["problems"]), 1)
        text = result["problems"][0]
        for needle in (str(go), "go1.27.2", "backend/go.mod", "go 1.27.3",                          # 主机 go 的路径与版本、go.mod 要求的版本
                       "GOTOOLCHAIN=local", "退出 2", "整轮后台验证白跑",                                  # 后果
                       "egress-spec:egress-scanner-check", "egress-spec:egress-spec-egressscan-self-test",
                       "toolchain@v0.0.1-go1.27.3.linux-arm64", "升级必须在没有后台验证运行时进行"):          # 怎么办
            self.assertIn(needle, text)
        self.assertEqual({key: result["detail"][key] for key in ("required_go", "host_go", "go_path")},
                         {"required_go": "1.27.3", "host_go": "1.27.2", "go_path": str(go)})

    def test_equal_or_newer_hosts_pass_and_versions_compare_numerically(self) -> None:
        for host in ("1.27.3", "1.27.4", "1.27.10", "1.28.0", "1.28", "1.100.0"):   # 1.27.10 与 1.100.0：按字符串比会误判成“更旧”
            with self.subTest(host=host):
                self.install_go(host)
                result = self.check()
                self.assertEqual((result["passed"], result["problems"]), (True, []), result)
                self.assertEqual(result["detail"]["host_go"], host)
        self.install_go(None, stdout="go version go1.27.3 X:nocoverageredesign linux/arm64")
        self.assertTrue(self.check()["passed"], "版本号后面的实验标记不影响解析")

    def test_older_hosts_fail_even_where_a_string_comparison_would_let_them_through(self) -> None:
        for host in ("1.27.2", "1.27.0", "1.26.9", "1.9.9", "1.2.99", "0.99.0"):   # 1.9.9：按字符串比比 1.27.3 “更大”
            with self.subTest(host=host):
                self.install_go(host)
                result = self.check()
                self.assertFalse(result["passed"], result)
                self.assertEqual(result["detail"]["host_go"], host)

    def test_a_missing_patch_defaults_to_zero_on_both_sides(self) -> None:
        self.write_go_mod("module example.invalid/backend\n\ngo 1.27\n")
        for host, expected in (("1.27", True), ("1.27.0", True), ("1.27.2", True), ("1.28", True), ("1.26.9", False), ("1.26", False)):
            with self.subTest(go_mod="go 1.27", host=host):
                self.install_go(host)
                self.assertEqual(self.check()["passed"], expected)
        self.install_go("1.26.9")
        self.assertIn("toolchain@v0.0.1-go1.27.0.linux-arm64", self.check()["problems"][0], "go.mod 写 go 1.27（没有补丁段）时，建议拷出的工具链目录名补成 1.27.0")
        self.write_go_mod("module example.invalid/backend\n\ngo 1.27.0\n")
        self.install_go("1.27")
        self.assertTrue(self.check()["passed"], "go.mod 写 1.27.0、主机是老式写法 go1.27（没有补丁段）：两边都按 1.27.0 比")
        self.write_go_mod("module example.invalid/backend\n\ngo 1.27.1\n")
        self.assertFalse(self.check()["passed"], "同一台 go1.27（按 1.27.0）低于 go.mod 的 1.27.1")

    def test_go_missing_from_the_executor_path_fails_closed(self) -> None:
        empty, not_executable = self.root / "empty-bin", self.root / "not-executable-bin"
        empty.mkdir()
        not_executable.mkdir()
        (not_executable / "go").write_text("#!/bin/sh\necho 'go version go1.27.3 linux/arm64'\n", encoding="utf-8")   # 没有执行权限
        for label, path in (("空目录", str(empty)), ("go 没有执行权限", str(not_executable)), ("PATH 为空", ""), ("目录不存在", str(self.root / "nope"))):
            with self.subTest(label):
                result = self.check(exec_path=path)
                self.assertFalse(result["passed"], result)
                self.assertIn("找不到 go", result["problems"][0])
                self.assertIsNone(result["detail"]["go_path"])
        self.assertIn(str(empty), self.check(exec_path=str(empty))["problems"][0], "失败信息写明查找的 PATH")

    def test_unparseable_go_version_output_fails_closed(self) -> None:
        for label, output in (("乱码", "hello world"), ("空输出", ""), ("没有 go version 前缀", "go1.27.3 linux/arm64"),
                              ("开发版", "go version devel go1.28-abcdef0 Mon Jan 1 00:00:00 2026 +0000 linux/arm64"),
                              ("候选版", "go version go1.27rc1 linux/arm64"), ("带厂商后缀", "go version go1.27.3-bigcorp linux/arm64")):
            with self.subTest(label):
                self.install_go(None, stdout=output)
                result = self.check()
                self.assertFalse(result["passed"], result)
                self.assertIn("解析不了", result["problems"][0])
                self.assertIsNone(result["detail"]["host_go"])
                self.assertEqual(result["detail"]["go_version_output"], output)

    def test_a_go_that_fails_or_cannot_run_fails_closed(self) -> None:
        self.install_go(None, stdout="go version go1.27.3 linux/arm64", exit_code=1, stderr="go: boom")
        result = self.check()
        self.assertFalse(result["passed"], "退出码非零：即使输出看着像合格的版本也不信")
        self.assertIn("退出码 1", result["problems"][0])
        self.assertIn("go: boom", result["problems"][0])
        (self.bin / "go").write_text("这不是一个可执行程序\n", encoding="utf-8")   # 有执行权限，但既不是二进制也没有 #! 行：exec 失败
        (self.bin / "go").chmod(0o755)
        result = self.check()
        self.assertFalse(result["passed"], result)
        self.assertIn("运行不起来", result["problems"][0])

    def test_a_go_that_hangs_is_killed_and_fails_closed(self) -> None:
        self.install_go(None, stdout="", delay=30)
        started = time.monotonic()
        with mock.patch.object(pre, "HOST_GO_PROBE_TIMEOUT_SECONDS", 0.5):
            result = self.check(exec_path=f"{self.bin}:/bin:/usr/bin")   # 假 go 用 exec sleep 模拟卡住，PATH 里要找得到 sleep
        self.assertFalse(result["passed"], result)
        self.assertIn("没有返回", result["problems"][0])
        self.assertLess(time.monotonic() - started, 20, "超时后进程被杀，不会等满 30 秒")

    def test_go_mod_without_exactly_one_parseable_go_directive_fails_closed(self) -> None:
        self.install_go("1.27.3")
        cases = {
            "没有 go 指令（require 块里有 go.uber.org 开头的模块行，不能误认）":
                ("module example.invalid/backend\n\nrequire (\n\tgo.uber.org/zap v1.27.0\n)\n", "实际有 0 个"),
            "两个 go 指令": ("module example.invalid/backend\n\ngo 1.27.3\ngo 1.28.0\n", "实际有 2 个"),
            "被注释掉": ("module example.invalid/backend\n\n// go 1.27.3\n", "实际有 0 个"),
            "候选版写法": ("module example.invalid/backend\n\ngo 1.27rc1\n", "不是 X.Y 或 X.Y.Z 形式"),
            "只有主版本": ("module example.invalid/backend\n\ngo 1\n", "不是 X.Y 或 X.Y.Z 形式"),
        }
        for label, (text, expected) in cases.items():
            with self.subTest(label):
                self.write_go_mod(text)
                result = self.check()
                self.assertFalse(result["passed"], result)
                self.assertIn(expected, result["problems"][0])
        (self.tree / "backend" / "go.mod").unlink()
        result = self.check()
        self.assertFalse(result["passed"], result)
        self.assertIn("读不到", result["problems"][0])
        self.assertFalse(self.log.exists(), "go.mod 判不了的时候根本没有去跑 go")

    def test_go_directive_spellings_that_go_itself_accepts_are_all_read(self) -> None:
        self.install_go("1.27.3")
        for label, text in (("行尾注释", "module m\n\ngo 1.27.3 // 注释\n"), ("Windows 换行", "module m\r\n\r\ngo 1.27.3\r\n"),
                            ("行首缩进", "module m\n\n  go 1.27.3\n"), ("Tab 分隔", "module m\n\ngo\t1.27.3\n")):
            with self.subTest(label):
                self.write_go_mod(text)
                result = self.check()
                self.assertEqual((result["passed"], result["detail"]["required_go"]), (True, "1.27.3"), result)

    def test_go_mod_parser_reads_only_real_directives(self) -> None:
        text = ("module example.invalid/m\n\ngo 1.27.3 // 注释\n\ntoolchain go1.27.5\n\nrequire (\n\tgo.uber.org/zap v1.27.0\n"
                "\tgolang.org/x/net v0.56.0 // indirect\n)\n\n// go 1.99.0\n godebug default=go1.21\n")
        self.assertEqual(pre.parse_go_mod(text), (["1.27.3"], ["go1.27.5"]))
        self.assertEqual(pre.parse_go_mod("go 1.27.3\r\ntoolchain default\r\n"), (["1.27.3"], ["default"]))
        self.assertEqual(pre.parse_go_mod(""), ([], []))

    def test_the_toolchain_line_is_a_hint_not_a_gate(self) -> None:
        self.write_go_mod("module example.invalid/backend\n\ngo 1.27.3\n\ntoolchain go1.27.5\n")
        self.install_go("1.27.3")
        below = self.check()
        self.assertTrue(below["passed"], "主机满足 go 指令、低于 toolchain 行：GOTOOLCHAIN=local 的单元不据 toolchain 行做决定，放行")
        self.assertEqual(below["detail"]["toolchain_line"], "go1.27.5")
        self.assertIn("go1.27.5", below["detail"]["toolchain_note"])
        for host in ("1.27.5", "1.28.0"):
            self.install_go(host)
            met = self.check()
            self.assertTrue(met["passed"], met)
            self.assertNotIn("toolchain_note", met["detail"], "主机不低于 toolchain 行：不需要提示")
        self.install_go("1.27.2")
        self.assertFalse(self.check()["passed"], "硬门槛仍是 go 指令：低于它照样拦，哪怕 toolchain 行也在")
        self.write_go_mod("module example.invalid/backend\n\ngo 1.27.3\n\ntoolchain default\n")
        self.install_go("1.27.3")
        self.assertNotIn("toolchain_note", self.check()["detail"], "toolchain default 没有版本可比，不提示")

    def test_the_probe_runs_in_the_offline_units_environment_and_not_in_the_tree(self) -> None:
        self.install_go("1.27.3")
        caller = {"PATH": "/operator/path", "LANG": "zh_CN.UTF-8", "HOME": "/home/operator", "GOTOOLCHAIN": "auto",
                  "GOPROXY": "https://proxy.example.invalid", "GOFLAGS": "-mod=mod", "GOCACHE": "/work/go-build-cache", "CGO_ENABLED": "0",
                  "STRAY": "不应放行", "PYTHONPATH": "/leak"}
        self.assertTrue(self.check(environ=caller)["passed"])
        seen = read_fake_go_log(self.log)
        self.assertEqual(seen["args"], "version")
        self.assertEqual(seen["pwd"], "/", "站在根目录：与被验证的树没有任何关系")
        self.assertEqual((seen["GOTOOLCHAIN"], seen["GOPROXY"], seen["GOFLAGS"]), ("local", "off", "-mod=readonly"),
                         "三个变量强制成离线单元的值，盖掉调用方给的")
        self.assertEqual((seen["GOCACHE"], seen["CGO_ENABLED"], seen["HOME"]), ("/work/go-build-cache", "0", "/home/operator"),
                         "其余 GO*／CGO_* 与 HOME 照调用方放行")
        self.assertEqual((seen["PATH"], seen["LANG"]), (str(self.bin), "C.UTF-8"), "PATH 与语言固定，不用调用方的")
        self.assertEqual((seen["STRAY"], seen["PYTHONPATH"]), ("<unset>", "<unset>"), "白名单之外的变量一个都不带")

    def test_the_operators_path_is_ignored_and_the_first_go_on_the_executor_path_wins(self) -> None:
        operator_bin = self.root / "operator-bin"
        operator_bin.mkdir()
        write_fake_go(operator_bin, "go version go1.20.0 linux/arm64")   # 操作员 shell 的 PATH 上有个旧 go：执行器根本看不见它
        self.install_go("1.27.3")
        result = self.check(environ={"PATH": str(operator_bin)})
        self.assertTrue(result["passed"], result)
        self.assertEqual(result["detail"]["go_path"], str(self.bin / "go"))
        # 执行器 PATH 上有两个 go 时取第一个（/usr/local/go/bin 排在 /usr/local/bin 前面）。
        second = self.root / "second-bin"
        second.mkdir()
        write_fake_go(second, "go version go1.28.0 linux/arm64")
        self.install_go("1.27.2")
        result = self.check(exec_path=f"{self.bin}:{second}")
        self.assertFalse(result["passed"], "排在前面的旧 go 才是单元用的，后面的新 go 救不了")
        self.assertEqual(result["detail"]["go_path"], str(self.bin / "go"))

    def test_the_constants_agree_with_the_executor_and_the_offline_units(self) -> None:
        script = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "entry-gates.sh").read_text(encoding="utf-8")
        self.assertEqual(re.findall(r"^EXEC_PATH=(\S+)$", script, re.M), [pre.EXEC_PATH],
                         "执行器固定 PATH 在 entry-gates.sh 里只定义一次，且与本模块的 EXEC_PATH 逐字相同")
        self.assertEqual(pre.HOST_GO_PROBE_ENV, eg.DECLARED_OFFLINE_ENV, "探测强制的三个变量就是声明级单元运行期的离线环境")
        self.assertTrue(eg.DECLARED_REQUIRE_OFFLINE, "声明级单元的离线要求仍打开：它关了的话 GOTOOLCHAIN=local 的前提不成立，第五项的判定理由要重新评估")
        self.assertTrue((REPO_ROOT / pre.GO_MOD_RELATIVE).is_file())

    def test_the_repositorys_own_go_mod_is_readable_and_gates_exactly_at_its_version(self) -> None:
        go_values, _toolchain = pre.parse_go_mod((REPO_ROOT / pre.GO_MOD_RELATIVE).read_text(encoding="utf-8"))
        self.assertEqual(len(go_values), 1, go_values)
        required = go_values[0]
        major, minor = required.split(".")[:2]
        self.install_go(required)
        at_version = pre.check_host_toolchain(REPO_ROOT, exec_path=str(self.bin), environ={})
        self.assertEqual((at_version["passed"], at_version["detail"]["required_go"]), (True, required), at_version)
        self.install_go(f"{major}.{int(minor) - 1}.99")
        self.assertFalse(pre.check_host_toolchain(REPO_ROOT, exec_path=str(self.bin), environ={})["passed"], "低一个次版本的主机被拦")


class RunChecksTests(unittest.TestCase):
    """五项合在一起：三种起跑条件错误各自被拦下，几个都错一次报全，全对放行。"""

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
        # 主机工具链检查（第五项）：假 go 充当执行器 PATH，缺省报 1.27.3，与 make_frozen_repo 树里 backend/go.mod 要求的版本相同，所以缺省放行。
        self.go_bin = self.root / "go-bin"
        self.go_bin.mkdir()
        write_fake_go(self.go_bin, "go version go1.27.3 linux/arm64")
        seed_receipts(self.runroot)   # 第六、七项缺省放行：运行根里有合法的本机完整测试收据与 TUI 预探收据

    def run_all(self, tree: Path, steps: FakeSteps, **extra) -> dict:
        return pre.run_checks(tree=tree, commit=COMMIT_PLACEHOLDER, data_root=self.data, runroot=self.runroot, client="codex",
                              deployment=self.deployment, deployments=None, runner=self.ok_runner, steps=steps,
                              exec_path=str(self.go_bin), environ={}, **extra)

    def test_all_good_passes(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        report = self.run_all(self.root / "tree", FakeSteps(recorded="a" * 64, actual="a" * 64))
        self.assertTrue(report["passed"], report)
        self.assertEqual([item["name"] for item in report["checks"]], list(pre.CHECK_NAMES))
        self.assertEqual(list(pre.CHECK_NAMES), ["freeze-receipts", "baseline-reseal", "redeploy", "admission-binding", "host-toolchain",
                                                 "local-capture-receipt", "tui-prelaunch-receipt"],
                         "新增检查追加在末尾，前五项的名字与顺序不变")

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

    def test_reproduces_and_blocks_the_wrong_host_go_environment(self) -> None:
        """第三次真实发生的白跑（后台验证 #1）：被验证提交的 backend/go.mod 要求 go 1.27.3，采集主机 Go 是 1.27.2——环境与被验证提交不匹配，起跑前就要拦下。"""

        make_frozen_repo(self.root / "late-receipt", receipted=False)
        make_frozen_repo(self.root / "good", receipted=True)
        write_fake_go(self.go_bin, "go version go1.27.2 linux/arm64")
        old_host = self.run_all(self.root / "good", FakeSteps(recorded="a" * 64, actual="a" * 64))
        every_wrong = self.run_all(self.root / "late-receipt", FakeSteps(recorded="a" * 64, actual="b" * 64))
        self.assertEqual(old_host["failed_checks"], ["host-toolchain"], "主机 Go 低于 go.mod：被拦下，其余四项照常通过")
        self.assertEqual(every_wrong["failed_checks"], ["freeze-receipts", "redeploy", "host-toolchain"], "三种都错，一次报全")
        text = pre.format_failures(every_wrong)
        self.assertIn("拒绝起跑后台验证", text)
        self.assertIn("[host-toolchain]", text)
        self.assertIn("go1.27.2", text)
        # 换上够新的 Go 之后，同一棵树放行：被拦的就是主机 Go，不是别的。
        write_fake_go(self.go_bin, "go version go1.27.3 linux/arm64")
        self.assertTrue(self.run_all(self.root / "good", FakeSteps(recorded="a" * 64, actual="a" * 64))["passed"])

    def test_the_fifth_check_reports_in_the_same_shape_as_the_first_four(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        report = self.run_all(self.root / "tree", FakeSteps(recorded="a" * 64, actual="a" * 64))
        self.assertEqual(sorted(report), ["checked_at_unix", "checks", "client", "commit", "failed_checks", "passed", "schema_version"])
        for item in report["checks"]:
            self.assertEqual(sorted(item), ["detail", "name", "passed", "problems", "seconds"], item["name"])
            self.assertIsInstance(item["problems"], list, item["name"])
            self.assertIsInstance(item["detail"], dict, item["name"])
        json.dumps(report, ensure_ascii=False)   # 整份报告能序列化：background_validation 把它留档成 JSON
        fifth = report["checks"][4]
        self.assertEqual((fifth["name"], fifth["passed"], fifth["problems"]), ("host-toolchain", True, []))
        self.assertEqual((fifth["detail"]["required_go"], fifth["detail"]["host_go"]), ("1.27.3", "1.27.3"))

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

    def test_only_can_select_the_toolchain_check_and_nothing_else_runs(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        spawned: list[object] = []
        report = pre.run_checks(tree=self.root / "tree", commit=COMMIT_PLACEHOLDER, data_root=self.data, runroot=self.runroot, client="codex",
                                deployment=self.deployment, deployments=None, only=["host-toolchain"],
                                runner=lambda *args, **kwargs: spawned.append(args), exec_path=str(self.go_bin), environ={})
        self.assertEqual([item["name"] for item in report["checks"]], ["host-toolchain"])
        self.assertTrue(report["passed"], report)
        self.assertEqual(spawned, [], "只选第五项时，冻结覆盖与基线重封（经 runner 起子进程的两项）一个都没有执行")

    def command_line(self, tree: Path, *extra: str) -> tuple[int, str, str]:
        """跑一遍 ``background_preflight.py check`` 命令行（执行器 PATH 指向假 go 目录），返回（退出码, 标准输出, 标准错误）。"""

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(pre, "EXEC_PATH", str(self.go_bin)), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = pre.main(["check", "--runroot", str(self.runroot), "--data-root", str(self.data), "--commit", COMMIT_PLACEHOLDER,
                             "--tree", str(tree), *extra])
        return code, out.getvalue(), err.getvalue()

    def test_command_line_only_host_toolchain_passes_and_blocks_with_the_documented_exit_codes(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        code, stdout, stderr = self.command_line(self.root / "tree", "--only", "host-toolchain")
        report = json.loads(stdout)
        self.assertEqual((code, report["passed"], [item["name"] for item in report["checks"]]), (0, True, ["host-toolchain"]), stderr)
        write_fake_go(self.go_bin, "go version go1.27.2 linux/arm64")
        code, stdout, stderr = self.command_line(self.root / "tree", "--only", "host-toolchain")
        self.assertEqual((code, json.loads(stdout)["failed_checks"]), (3, ["host-toolchain"]), "没过：退出码 3，JSON 报告照常打印")
        self.assertIn("[host-toolchain]", stderr)
        self.assertIn("拒绝起跑后台验证", stderr)
        code, _stdout, stderr = self.command_line(self.root / "tree", "--only", "不存在")
        self.assertEqual(code, 2, f"不认识的检查项是用法错误，退出码 2：{stderr}")


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


class ReceiptChecksTests(unittest.TestCase):
    """第六、七项（Codex 升级工具优化第 4、5 项）：用上一轮入口门禁两次白跑的失败复现，确认都在起跑前被拦下。

    * 失败一（第三轮入口门禁红在 ``test_driver_client_table``）：探测修复提交在注释里写了版本号，``make test-capture-tools`` 完整跑会红，但修复提交只跑了定向测试就部署起跑，
      白跑 1.8 小时并多部署一次。对应：没有本机完整测试收据，或收据如实记录了红，或收据是别的提交的，都拒绝起跑；
    * 失败二（入口第 13 步客户端启动探测 5 个组合 3 个失败）：新版客户端取消 daemon 功能白名单，带 ``--disable`` 的 TUI 报 ``--no-daemon`` 退出，在两小时门禁之后才暴露。
      对应：没有新版二进制的 TUI 零请求预探收据，或收据里有场景启动即退出，都拒绝起跑。"""

    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.runroot = self.root / "run"
        self.runroot.mkdir()
        self.env = self.root / "env.sh"
        self.env.write_text(f'ROUND="r1"\nTARGET_VERSION="{TARGET}"\nexport CODEX_BIN_SHA256="{BINARY_SHA}"\n', encoding="utf-8")

    def capture(self, commit: str = COMMIT_PLACEHOLDER, client: str = "codex") -> dict:
        return pre.check_local_capture_receipt(self.runroot, commit, client)

    def tui(self, client: str = "codex", env=True) -> dict:
        return pre.check_tui_prelaunch_receipt(self.runroot, client, self.env if env else None)

    def test_failure_one_a_fix_commit_without_a_full_local_run_is_stopped_before_the_gate(self) -> None:
        result = self.capture()
        self.assertFalse(result["passed"])
        self.assertIn("没有提交", result["problems"][0])
        self.assertIn("local-capture-tools-receipt.sh", result["problems"][0], "写明怎么生成收据")
        self.assertIn("test_driver_client_table", result["problems"][0], "点出上一轮的教训")

    def test_failure_one_a_recorded_red_run_is_stopped_and_shows_where_it_failed(self) -> None:
        directory = self.runroot / oc.RECEIPT_DIR_NAME
        oc.write_capture_receipt(directory, commit=COMMIT_PLACEHOLDER, exit_code=2, started_at_utc="2026-10-29T01:00:00Z", finished_at_utc="2026-10-29T01:50:00Z",
                                 tree_clean=True, log_tail="FAIL: test_driver_has_no_version_literals (client_launch_probe.py 含版本字面量)")
        result = self.capture()
        self.assertFalse(result["passed"])
        self.assertIn("没有通过（退出码 2）", result["problems"][0])
        self.assertIn("test_driver_has_no_version_literals", result["problems"][0], "红在哪个用例带进提示")

    def test_a_receipt_for_another_commit_a_targeted_command_a_dirty_tree_or_a_tiny_duration_is_refused(self) -> None:
        directory = seed_receipts(self.runroot, tui=False)
        self.assertTrue(self.capture()["passed"])
        other = "e" * 40
        self.assertIn("没有提交", self.capture(other)["problems"][0], "收据按完整提交号命名：别的提交没有收据")
        # 收据文件被复制成另一个提交的名字：内容里的提交号对不上
        (directory / f"capture-tools-{other}.json").write_bytes((directory / f"capture-tools-{COMMIT_PLACEHOLDER}.json").read_bytes())
        self.assertTrue(any("不是要起跑的" in problem for problem in self.capture(other)["problems"]))
        cases = {
            "定向命令": dict(command=["make", "test-capture-tools-targeted"]),
            "工作树不干净": dict(tree_clean=False),
            "耗时太短": dict(finished_at_utc="2026-10-29T01:00:30Z"),
        }
        for label, change in cases.items():
            with self.subTest(label):
                arguments = dict(commit=COMMIT_PLACEHOLDER, exit_code=0, started_at_utc="2026-10-29T01:00:00Z", finished_at_utc="2026-10-29T01:48:00Z",
                                 tree_clean=True)
                arguments.update(change)
                oc.write_capture_receipt(directory, **arguments)
                self.assertFalse(self.capture()["passed"], label)
        seed_receipts(self.runroot, tui=False)
        self.assertTrue(self.capture()["passed"])

    def test_a_tampered_receipt_fails_its_seal(self) -> None:
        seed_receipts(self.runroot, tui=False)
        path = self.runroot / oc.RECEIPT_DIR_NAME / f"capture-tools-{COMMIT_PLACEHOLDER}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["exit_code"] = 0
        value["duration_seconds"] = 99999
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        self.assertFalse(self.capture()["passed"])
        self.assertTrue(any("封口" in problem for problem in self.capture()["problems"]))

    def test_failure_two_without_the_new_binary_prelaunch_receipt_the_run_is_stopped(self) -> None:
        result = self.tui()
        self.assertFalse(result["passed"])
        self.assertIn("没有新版二进制 TUI 零请求启动预探收据", result["problems"][0])
        self.assertIn("tui-prelaunch-receipt.sh write", result["problems"][0])

    def test_failure_two_a_scenario_that_exits_at_launch_is_stopped_with_its_exit_code_and_screen_tail(self) -> None:
        """复现：compact、ws-idle-close 带 --disable 的 TUI 启动即退出（Cannot use the shared background server），daemon、guardian 通过。"""

        seed_receipts(self.runroot, capture=False, tui_failed=("compact-tui", "ws-idle-close-tui"))
        result = self.tui()
        self.assertFalse(result["passed"])
        text = "\n".join(result["problems"])
        for name in ("compact-tui", "ws-idle-close-tui"):
            self.assertIn(f"场景 {name} 预探没通过", text)
        self.assertNotIn("场景 daemon-tui", text)
        self.assertNotIn("场景 guardian-tui", text)
        self.assertIn("Cannot use the shared background server", text, "屏幕尾部带进提示")
        self.assertIn("退出码 1", text)

    def test_the_receipt_must_cover_every_required_scenario_and_bind_the_target_version_and_binary(self) -> None:
        seed_receipts(self.runroot, capture=False, skip_scenarios=("guardian-tui",))
        self.assertIn("缺少必需场景：guardian-tui", "\n".join(self.tui()["problems"]))
        seed_receipts(self.runroot, capture=False)
        self.assertTrue(self.tui()["passed"])
        self.env.write_text(f'TARGET_VERSION="{TARGET}1"\nCODEX_BIN_SHA256="{BINARY_SHA}"\n', encoding="utf-8")
        self.assertIn("不是本轮目标版本", "\n".join(self.tui()["problems"]))
        self.env.write_text(f'TARGET_VERSION="{TARGET}"\nCODEX_BIN_SHA256="{"c" * 64}"\n', encoding="utf-8")
        self.assertIn("二进制摘要与本轮登记的客户端二进制摘要不一致", "\n".join(self.tui()["problems"]))
        self.assertTrue(self.tui(env=False)["passed"], "拿不到参数文件时只核收据自身")

    def test_a_probe_that_sent_real_requests_is_not_a_zero_request_probe(self) -> None:
        directory = self.runroot / oc.RECEIPT_DIR_NAME
        rows = [{"name": name, "status": "passed"} for name in oc.REQUIRED_TUI_SCENARIOS]
        oc.write_tui_receipt(directory, client_version=TARGET, binary_path="/b", binary_sha256=BINARY_SHA, scenarios=rows, started_at_utc="2026-10-29T01:00:00Z",
                             finished_at_utc="2026-10-29T01:05:00Z", live_request_count=3)
        self.assertIn("真实请求数是 3", "\n".join(self.tui()["problems"]))

    def test_claude_rounds_are_not_subject_to_the_two_codex_flows(self) -> None:
        for result in (self.capture(client="claude"), self.tui(client="claude")):
            self.assertTrue(result["passed"], result)
            self.assertIn("not_applicable", result["detail"])

    def test_the_whole_preflight_reports_both_reproductions_at_once(self) -> None:
        make_frozen_repo(self.root / "tree", receipted=True)
        data = self.root / "data"
        (data / "control").mkdir(parents=True)
        (data / "control" / "codex-01630-supervisor-enable-20261025t000000z.json").write_text(json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        go_bin = self.root / "go-bin"
        go_bin.mkdir()
        write_fake_go(go_bin, "go version go1.27.3 linux/arm64")
        runner = lambda argv, **kw: (pre._run(argv, **kw) if "-c" in argv else SimpleNamespace(returncode=0, stdout="OK", stderr=""))  # noqa: E731
        report = pre.run_checks(tree=self.root / "tree", commit=COMMIT_PLACEHOLDER, data_root=data, runroot=self.runroot, client="codex",
                                deployment=bv.latest_deployment(data), deployments=None, runner=runner, steps=FakeSteps(recorded="a" * 64, actual="a" * 64),
                                exec_path=str(go_bin), environ={}, vc_env=self.env)
        self.assertEqual(report["failed_checks"], ["local-capture-receipt", "tui-prelaunch-receipt"], "前五项照常通过，新增两项一次报全")
        text = pre.format_failures(report)
        self.assertIn("[local-capture-receipt]", text)
        self.assertIn("[tui-prelaunch-receipt]", text)
        seed_receipts(self.runroot)
        report = pre.run_checks(tree=self.root / "tree", commit=COMMIT_PLACEHOLDER, data_root=data, runroot=self.runroot, client="codex",
                                deployment=bv.latest_deployment(data), deployments=None, runner=runner, steps=FakeSteps(recorded="a" * 64, actual="a" * 64),
                                exec_path=str(go_bin), environ={}, vc_env=self.env)
        self.assertTrue(report["passed"], report)

    def test_env_reader_only_reads_literal_assignments(self) -> None:
        self.env.write_text('A=1\nexport TARGET_VERSION="1.2.3"\nCODEX_BIN_SHA256=\'abc\'\n# TARGET_VERSION="no"\nB=$(rm -rf /)\nPLAIN=value # c\n', encoding="utf-8")
        self.assertEqual(pre._env_value(self.env, "TARGET_VERSION"), "1.2.3")
        self.assertEqual(pre._env_value(self.env, "CODEX_BIN_SHA256"), "abc")
        self.assertIsNone(pre._env_value(self.env, "MISSING"))
        self.assertIsNone(pre._env_value(None, "A"))
        self.assertIsNone(pre._env_value(self.root / "absent", "A"))


class DriverCopyTests(unittest.TestCase):
    def test_driver_carries_identical_copies(self) -> None:
        driver = REPO_ROOT / "tools" / "arm64_capture_driver" / "driver"
        for name in ("background_preflight.py", "background_validation.py"):
            with self.subTest(name):
                self.assertEqual((driver / name).read_bytes(), (REPO_ROOT / "tools" / "ci" / name).read_bytes())

    def test_usage_texts_name_every_preflight_check(self) -> None:
        """用法说明跟着检查项走：``preflight --only`` 的帮助和驱动入口脚本的说明都得列全 ``CHECK_NAMES``（新增检查项忘了改文字就红）。"""

        text = (REPO_ROOT / "tools" / "ci" / "background_validation.py").read_text(encoding="utf-8")
        only_help = next(line for line in text.splitlines() if 'p_pre.add_argument("--only"' in line)
        wrapper = (REPO_ROOT / "tools" / "arm64_capture_driver" / "driver" / "background-validate.sh").read_text(encoding="utf-8")
        wrapper_usage = wrapper[wrapper.index("#   preflight <bundle"):wrapper.index("#   stop [--reason")]
        for name in pre.CHECK_NAMES:
            with self.subTest(name):
                self.assertIn(name, only_help)
        self.assertIn("host-toolchain", wrapper_usage)
        self.assertIn("七项", wrapper_usage)
        self.assertIn("local-capture-receipt", wrapper_usage)
        self.assertIn("tui-prelaunch-receipt", wrapper_usage)


if __name__ == "__main__":
    unittest.main()
