"""运维脚本 ``deploy/egress-ops/egress-ctl`` 的单测（更换指定出口：校验、差异、审计、按序安装、失败回滚）。

脚本本身不属于任何受管根，也不 import 受管模块；这里按文件路径加载它，用临时目录搭一个“假宿主”：
真实的运行包目录（取仓库里的三个运行包文件，所以策略校验是**运行包里真实的 validate_egress_policy**，不是替身）、
守护单元、策略文件；``systemctl``、``egress-install``、``ssh`` 等会改系统的外部调用由记录型替身承担。
"""

from __future__ import annotations

import ast
import contextlib
import copy
import importlib.util
import io
import json
import re
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import runtime_egress_fixtures

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "deploy" / "egress-ops" / "egress-ctl"
FIXTURE_POLICY = REPO_ROOT / "tools" / "official_client_capture" / "tests" / "fixtures" / "runtime_egress_policy.json"
BUNDLE_FILES = ("tools/arm64_supervised_deploy.py",
                "tools/official_client_capture/codex_upgrade_arm64_environment_receipt.py",
                "tools/official_client_capture/incremental_recovery.py")


def load_ctl():
    spec = importlib.util.spec_from_loader("egress_ctl_under_test", importlib.machinery.SourceFileLoader("egress_ctl_under_test", str(SCRIPT)))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module      # dataclass 解析字符串注解需要模块已登记
    spec.loader.exec_module(module)
    return module


ctl = load_ctl()


def digest(data: bytes) -> str:
    return ctl.sha256_hex(data)


class World:
    """假宿主：真实运行包与策略文件，外部副作用调用只记录并按开关模拟成败。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.runtime = root / "var" / "sub2api-egress"
        self.etc = root / "etc" / "sub2api-egress"
        self.etc.mkdir(parents=True)
        files = {name: (REPO_ROOT / name).read_bytes() for name in BUNDLE_FILES}
        hashes = {name: digest(data) for name, data in files.items()}
        self.identity = digest(ctl.canonical(hashes))
        self.bundle_dir = self.runtime / "bundles" / self.identity
        for name, data in files.items():
            target = self.bundle_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.bundle_dir / "bundle.json").write_text(json.dumps({
            "schema_version": "codex-runtime-egress-bundle/v1", "bundle_sha256": self.identity, "files": hashes}), encoding="utf-8")
        self.script = self.bundle_dir / "tools" / "arm64_supervised_deploy.py"
        self.unit = root / "guard.service"
        self.unit.write_text(f"[Service]\nExecStart={sys.executable} {self.script} egress-guard --policy x --role origin\n", encoding="utf-8")
        self.policy_path = self.etc / "policy.json"
        self.old_policy = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
        self.old_bytes = (json.dumps(self.old_policy, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.policy_path.write_bytes(self.old_bytes)
        (self.runtime / "installed.json").write_text("{}", encoding="utf-8")
        # 模拟开关与记录
        self.calls: list[str] = []
        self.origin_installed_bytes = self.old_bytes
        self.exit_policy: bytes | None = self.old_bytes
        self.exit_has_bundle = True
        self.exit_installed_bytes: bytes | None = self.old_bytes
        self.fail_exit_install_when_new = False
        self.fail_origin_install_when_new = False
        self.check_never_passes = False
        self.exit_commands: list[str] = []
        self.pushed_tar_members: list[str] = []

    def config(self, out: io.StringIO, err: io.StringIO, *, root: bool = True):
        return ctl.Config(policy_path=self.policy_path, key_path=self.etc / "private.key", runtime_root=self.runtime,
                          guard_unit=self.unit, runner=self.run, sleep=lambda _seconds: None, is_root=lambda: root, out=out, err=err)

    # ---- 外部调用替身
    def run(self, argv, input_bytes=None, timeout=300.0):
        argv = list(argv)
        if "-c" in argv and ctl.VALIDATE_SNIPPET in argv:
            return ctl.default_runner(argv, input_bytes, timeout)        # 真实的运行包校验
        if argv[0] == "ssh":
            return self.ssh(argv[-1], input_bytes)
        if "egress-install" in argv:
            assert argv[2] == str(self.script), "必须从现行运行包内执行安装"
            self.calls.append("origin_install")
            if self.fail_origin_install_when_new and self.policy_path.read_bytes() != self.old_bytes:
                return ctl.Result(1, b"", "源端安装器失败".encode())
            self.origin_installed_bytes = self.policy_path.read_bytes()
            return ctl.Result(0, b"{}")
        if "egress-check" in argv:
            self.calls.append("check")
            ok = (not self.check_never_passes) and self.origin_installed_bytes == self.policy_path.read_bytes()
            return ctl.Result(0 if ok else 1, b"{}" if ok else b"", b"" if ok else "策略摘要与安装记录不一致".encode())
        raise AssertionError(f"未模拟的命令：{argv}")

    def ssh(self, command: str, data: bytes | None):
        self.exit_commands.append(command)
        if "sha256sum -c" in command:
            self.calls.append("exit_bundle_check")
            return ctl.Result(0 if self.exit_has_bundle else 1)
        if "tar -x" in command:
            self.calls.append("exit_push_bundle")
            import io as _io, tarfile
            with tarfile.open(fileobj=_io.BytesIO(data)) as archive:
                self.pushed_tar_members = archive.getnames()
            self.exit_has_bundle = True
            return ctl.Result(0)
        if "HAD_POLICY" in command:
            self.calls.append("exit_backup")
            return ctl.Result(0, b"HAD_POLICY\n" if self.exit_policy is not None else b"NO_POLICY\n")
        if ".policy-new" in command:
            self.calls.append("exit_policy_push")
            self.exit_policy = data
            return ctl.Result(0)
        if "egress-install" in command:
            assert str(self.script) in command and "--role exit" in command
            self.calls.append("exit_install")
            if self.fail_exit_install_when_new and self.exit_policy != self.old_bytes:
                return ctl.Result(1, b"", "出口端安装器失败".encode())
            self.exit_installed_bytes = self.exit_policy
            return ctl.Result(0)
        if "is-active" in command:
            self.calls.append("exit_guard_active")
            return ctl.Result(0)
        raise AssertionError(f"未模拟的远端命令：{command}")


def new_policy(world: World, *, revision: int | None = None, address: str | None = None, actor: str | None = None) -> dict:
    policy = copy.deepcopy(world.old_policy)
    policy["revision"] = revision if revision is not None else policy["revision"] + 1
    address = address or runtime_egress_fixtures.FIXTURE_ALTERNATE_EXIT_IPV4S[0]
    policy["allowed_public_ipv4"] = [address]
    policy["nodes"]["exit"]["endpoint"]["ipv4"] = address
    policy["authorization"]["actor"] = actor or policy["authorization"]["actor"]
    return policy


class EgressCtlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.world = World(self.root)
        self.out, self.err = io.StringIO(), io.StringIO()

    def draft(self, policy: dict) -> str:
        path = self.root / "draft.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return str(path)

    def run_ctl(self, *argv: str, root: bool = True) -> int:
        cfg = self.world.config(self.out, self.err, root=root)
        return ctl.main(list(argv), cfg)

    def change_args(self, policy: dict, *extra: str) -> list[str]:
        return ["apply", "--draft", self.draft(policy), "--authorized-by", policy["authorization"]["actor"],
                "--reason", "更换指定出口", *extra]

    def audit_entries(self) -> list[dict]:
        path = self.world.runtime / "audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


class ValidationTests(EgressCtlTestCase):
    def test_validate_uses_the_installed_bundle_validator(self) -> None:
        self.assertEqual(self.run_ctl("validate", "--draft", self.draft(new_policy(self.world))), 0)
        self.assertIn("校验通过", self.out.getvalue())
        broken = new_policy(self.world)
        del broken["allowed_public_ipv4"]
        self.assertEqual(self.run_ctl("validate", "--draft", self.draft(broken)), 1)
        self.assertIn("运行包的策略校验拒绝", self.err.getvalue())

    def test_change_commands_require_authorization_and_reason(self) -> None:
        for command in ("diff", "apply"):
            with self.subTest(command=command), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_ctl(command, "--draft", self.draft(new_policy(self.world)), "--reason", "没写授权人")
            with self.subTest(command=command, missing="reason"), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_ctl(command, "--draft", self.draft(new_policy(self.world)), "--authorized-by", "x")

    def test_rules_beyond_the_validator(self) -> None:
        old_address = self.world.old_policy["allowed_public_ipv4"][0]
        cases = {
            "revision 不增": new_policy(self.world, revision=self.world.old_policy["revision"]),
            "授权人与草稿不一致": new_policy(self.world, actor="另一个人"),
        }
        both = new_policy(self.world)
        both["allowed_public_ipv4"] = [old_address, both["allowed_public_ipv4"][0]]
        cases["新旧出口同时放行"] = both
        for name, policy in cases.items():
            with self.subTest(name):
                self.err.truncate(0), self.err.seek(0)
                argv = ["diff", "--draft", self.draft(policy), "--authorized-by", self.world.old_policy["authorization"]["actor"],
                        "--reason", "测试"]
                self.assertEqual(self.run_ctl(*argv), 1, self.err.getvalue())
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)

    def test_diff_shows_summary_and_unified_diff(self) -> None:
        policy = new_policy(self.world)
        self.assertEqual(self.run_ctl("diff", "--draft", self.draft(policy), "--authorized-by", policy["authorization"]["actor"],
                                      "--reason", "测试"), 0)
        text = self.out.getvalue()
        self.assertIn("revision", text)
        self.assertIn(policy["allowed_public_ipv4"][0], text)
        self.assertIn("--- 现行 policy.json", text)
        self.assertEqual(self.audit_entries(), [])


class DryRunTests(EgressCtlTestCase):
    def test_dry_run_changes_nothing(self) -> None:
        policy = new_policy(self.world)
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.run_ctl(*self.change_args(policy, "--dry-run")), 0, self.err.getvalue())
        after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file() and path.name != "draft.json"}
        self.assertEqual({k: v for k, v in before.items() if k.name != "draft.json"}, after, "dry-run 不得新增或改动任何文件")
        self.assertEqual(self.audit_entries(), [])
        self.assertFalse((self.world.runtime / "policy-backups").exists())
        self.assertEqual([call for call in self.world.calls if call != "check"], [], "dry-run 只允许只读的 egress-check")
        self.assertIn("--dry-run", self.out.getvalue())

    def test_apply_refuses_without_the_safety_switches(self) -> None:
        policy = new_policy(self.world)
        for label, argv, root in (("非 root", self.change_args(policy, "--exit-ssh", "root@exit", "--confirm-maintenance-window"), False),
                                  ("缺 --exit-ssh", self.change_args(policy, "--confirm-maintenance-window"), True),
                                  ("缺维护窗口确认", self.change_args(policy, "--exit-ssh", "root@exit"), True)):
            with self.subTest(label):
                self.assertEqual(self.run_ctl(*argv, root=root), 1)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.audit_entries(), [])
        self.assertEqual(self.world.calls, [])


class ApplyTests(EgressCtlTestCase):
    def apply(self, policy: dict, *extra: str) -> int:
        return self.run_ctl(*self.change_args(policy, "--exit-ssh", "root@exit", "--confirm-maintenance-window", *extra))

    def test_success_orders_exit_before_origin_and_checks_last(self) -> None:
        policy = new_policy(self.world)
        self.assertEqual(self.apply(policy), 0, self.err.getvalue())
        calls = self.world.calls
        self.assertEqual(calls, ["exit_bundle_check", "exit_backup", "exit_policy_push", "exit_install",
                                 "origin_install", "check", "exit_guard_active"])
        self.assertEqual(json.loads(self.world.policy_path.read_text(encoding="utf-8")), policy)
        self.assertEqual(json.loads(self.world.exit_policy.decode("utf-8")), policy)
        # 旧策略原字节备份
        backups = sorted((self.world.runtime / "policy-backups").glob("policy-*.json"))
        self.assertTrue(any(path.read_bytes() == self.world.old_bytes for path in backups))
        self.assertTrue(any(path.name.endswith(".installed.json") for path in backups))
        # 审计：begin + result，哈希链完整，记新旧摘要、授权与原因
        entries = self.audit_entries()
        self.assertEqual([item["event"] for item in entries], ["begin", "result"])
        self.assertEqual(entries[1]["status"], "ok")
        self.assertEqual(entries[0]["operation_id"], entries[1]["operation_id"])
        self.assertNotEqual(entries[0]["old_policy_sha256"], entries[0]["new_policy_sha256"])
        self.assertEqual((entries[0]["authorized_by"], entries[0]["reason"]), (policy["authorization"]["actor"], "更换指定出口"))
        self.assertEqual(entries[1]["results"]["exit_install"]["status"], "ok")
        self.assertEqual(entries[1]["results"]["origin_install"]["status"], "ok")
        self.assertTrue(ctl.audit_verify(self.world.runtime / "audit.jsonl")[0])

    def test_audit_digests_come_from_the_bundle_algorithm(self) -> None:
        policy = new_policy(self.world)
        self.assertEqual(self.apply(policy), 0)
        entry = self.audit_entries()[0]
        # 运行包的摘要算法：canonical JSON（排序键、紧凑分隔、末尾换行）的 sha256。
        expected = digest(json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
        self.assertEqual(entry["new_policy_sha256"], expected)

    def test_bundle_is_pushed_to_a_fresh_exit_machine_only_when_absent(self) -> None:
        self.world.exit_has_bundle = False
        self.world.exit_policy = None
        self.assertEqual(self.apply(new_policy(self.world)), 0, self.err.getvalue())
        self.assertIn("exit_push_bundle", self.world.calls)
        self.assertIn("bundle.json", self.world.pushed_tar_members)
        self.assertEqual(set(BUNDLE_FILES) <= set(self.world.pushed_tar_members), True)
        self.assertEqual(self.audit_entries()[-1]["results"]["exit_install"]["had_previous_policy"], False)

    def test_exit_install_failure_rolls_back_without_touching_origin(self) -> None:
        self.world.fail_exit_install_when_new = True
        self.assertEqual(self.apply(new_policy(self.world)), 2)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.world.exit_policy, self.world.old_bytes)
        self.assertEqual(self.world.exit_installed_bytes, self.world.old_bytes, "回滚后出口端重装了旧策略")
        self.assertEqual(self.world.calls.count("origin_install"), 0)
        last = self.audit_entries()[-1]
        self.assertEqual((last["event"], last["status"], last["rollback"]["status"]), ("result", "failed", "ok"))
        self.assertIn("出口端 egress-install 失败", last["results"]["failure"])

    def test_origin_install_failure_rolls_back_both_ends_in_order(self) -> None:
        self.world.fail_origin_install_when_new = True
        self.assertEqual(self.apply(new_policy(self.world)), 2)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.world.origin_installed_bytes, self.world.old_bytes)
        self.assertEqual(self.world.exit_installed_bytes, self.world.old_bytes)
        order = [call for call in self.world.calls if call.endswith("install")]
        self.assertEqual(order, ["exit_install", "origin_install", "exit_install", "origin_install"], "回滚同样出口端先、源端后")
        self.assertEqual(self.audit_entries()[-1]["rollback"]["status"], "ok")

    def test_failed_check_rolls_back_and_unrecoverable_state_returns_three(self) -> None:
        self.world.check_never_passes = True
        self.assertEqual(self.apply(new_policy(self.world), "--check-timeout", "0"), 3)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes, "即使回滚后核对仍失败，旧策略也已写回")
        last = self.audit_entries()[-1]
        self.assertEqual((last["status"], last["rollback"]["status"]), ("failed", "failed"))
        self.assertIn("egress-check 未通过", last["results"]["failure"])

    def test_fresh_exit_machine_rollback_keeps_it_as_is_and_says_so(self) -> None:
        self.world.exit_policy = None
        self.world.fail_origin_install_when_new = True
        self.assertEqual(self.apply(new_policy(self.world)), 2)
        self.assertIn("原本没有策略", self.audit_entries()[-1]["rollback"]["steps"]["exit"])


class AuditTests(EgressCtlTestCase):
    def test_chain_detects_tamper_and_truncation(self) -> None:
        cfg = self.world.config(self.out, self.err)
        for number in range(3):
            ctl.audit_append(cfg, {"event": "x", "n": number})
        path = self.world.runtime / "audit.jsonl"
        self.assertEqual(ctl.audit_verify(path), (True, "审计链完整，共 3 条"))
        lines = path.read_text(encoding="utf-8").splitlines()
        path.write_text("\n".join([lines[0], lines[1].replace('"n":1', '"n":9'), lines[2]]) + "\n", encoding="utf-8")
        self.assertFalse(ctl.audit_verify(path)[0])
        path.write_text("\n".join(lines[1:]) + "\n", encoding="utf-8")
        self.assertFalse(ctl.audit_verify(path)[0], "删掉开头几条也要发现")
        path.write_text("\n".join(lines[:2]) + "\n", encoding="utf-8")
        self.assertTrue(ctl.audit_verify(path)[0], "只丢掉结尾无法由链本身发现，运行手册要求对照 begin/result 配对")

    def test_append_refuses_after_a_corrupt_last_line(self) -> None:
        cfg = self.world.config(self.out, self.err)
        ctl.audit_append(cfg, {"event": "x"})
        path = self.world.runtime / "audit.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace('"event":"x"', '"event":"y"'), encoding="utf-8")
        with self.assertRaises(ctl.CtlError):
            ctl.audit_append(cfg, {"event": "z"})

    def test_audit_verify_command_exit_codes(self) -> None:
        cfg = self.world.config(self.out, self.err)
        ctl.audit_append(cfg, {"event": "x"})
        self.assertEqual(self.run_ctl("audit-verify"), 0)
        path = self.world.runtime / "audit.jsonl"
        path.write_text(path.read_text(encoding="utf-8").replace("x", "q", 1), encoding="utf-8")
        self.assertEqual(self.run_ctl("audit-verify"), 1)


class BundleTests(EgressCtlTestCase):
    def test_modified_bundle_file_is_refused(self) -> None:
        target = self.world.bundle_dir / BUNDLE_FILES[2]
        target.chmod(0o600)
        target.write_bytes(target.read_bytes() + "# 被改\n".encode("utf-8"))
        self.assertEqual(self.run_ctl("validate", "--draft", self.draft(new_policy(self.world))), 1)
        self.assertIn("摘要与登记不符", self.err.getvalue())

    def test_guard_must_run_inside_the_persistent_bundle(self) -> None:
        self.world.unit.write_text(f"[Service]\nExecStart={sys.executable} /tmp/arm64_supervised_deploy.py egress-guard\n", encoding="utf-8")
        self.assertEqual(self.run_ctl("validate", "--draft", self.draft(new_policy(self.world))), 1)
        self.assertIn("没有运行在持久运行包内", self.err.getvalue())


class ScriptBoundaryTests(unittest.TestCase):
    def test_script_stays_outside_every_managed_root_and_imports_nothing_managed(self) -> None:
        relative = SCRIPT.relative_to(REPO_ROOT).as_posix()
        self.assertTrue(relative.startswith("deploy/"), "运维脚本不得放进 tools/ 等受管根或 docs/egress")
        tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.add((node.module or "").split(".")[0])
        self.assertFalse(imported & {"tools", "arm64_supervised_deploy", "codex_upgrade_arm64_environment_receipt", "backend"})
        self.assertIn("-I", SCRIPT.read_text(encoding="utf-8"), "运行包函数只经隔离模式的子进程使用")

    def test_script_and_runbook_contain_no_concrete_egress_address(self) -> None:
        import ipaddress
        for path in (SCRIPT, SCRIPT.with_name("RUNBOOK.md")):
            for token in re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", path.read_text(encoding="utf-8")):
                with self.subTest(path=path.name, token=token):
                    self.assertFalse(ipaddress.IPv4Address(token).is_global)


if __name__ == "__main__":
    unittest.main()
