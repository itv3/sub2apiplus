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
    """一台假宿主：真实运行包与策略文件，外部副作用调用只记录并按开关模拟成败。出口机、源端各建一个。"""

    def __init__(self, root: Path, *, fresh: bool = False) -> None:
        self.root = root
        self.fresh = fresh
        self.runtime = root / "var" / "sub2api-egress"
        self.etc = root / "etc" / "sub2api-egress"
        self.etc.mkdir(parents=True)
        files = {name: (REPO_ROOT / name).read_bytes() for name in BUNDLE_FILES}
        hashes = {name: digest(data) for name, data in files.items()}
        self.identity = digest(ctl.canonical(hashes))
        # 全新出口机：运行包目录由操作员从源端拷来，放在任意位置（不在 runtime/bundles 下）；已有机器：就在持久运行包目录里。
        self.bundle_dir = (root / "copied-bundle" / self.identity) if fresh else (self.runtime / "bundles" / self.identity)
        for name, data in files.items():
            target = self.bundle_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (self.bundle_dir / "bundle.json").write_text(json.dumps({
            "schema_version": "codex-runtime-egress-bundle/v1", "bundle_sha256": self.identity, "files": hashes}), encoding="utf-8")
        self.script = self.bundle_dir / "tools" / "arm64_supervised_deploy.py"
        self.unit = root / "guard.service"
        self.policy_path = self.etc / "policy.json"
        self.old_policy = json.loads(FIXTURE_POLICY.read_text(encoding="utf-8"))
        self.old_bytes = (json.dumps(self.old_policy, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self.installed_bytes: bytes | None = None
        if not fresh:
            self.unit.write_text(f"[Service]\nExecStart={sys.executable} {self.script} egress-guard --policy x --role origin\n", encoding="utf-8")
            self.policy_path.write_bytes(self.old_bytes)
            self.write_installed(self.old_bytes)
            self.installed_bytes = self.old_bytes
        # 模拟开关与记录
        self.calls: list[str] = []
        self.fail_install_when_new = False
        self.check_never_passes = False
        self.guard_inactive = False

    def write_installed(self, policy_bytes: bytes) -> None:
        sha = digest(json.dumps(json.loads(policy_bytes), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")
        self.runtime.mkdir(parents=True, exist_ok=True)
        (self.runtime / "installed.json").write_text(json.dumps({"policy_sha256": sha}), encoding="utf-8")

    def config(self, out: io.StringIO, err: io.StringIO, *, root: bool = True):
        return ctl.Config(policy_path=self.policy_path, key_path=self.etc / "private.key", runtime_root=self.runtime,
                          guard_unit=self.unit, bundle_dir=self.bundle_dir if self.fresh else None, runner=self.run,
                          sleep=lambda _seconds: None, is_root=lambda: root, out=out, err=err)

    # ---- 外部调用替身
    def run(self, argv, input_bytes=None, timeout=300.0):
        argv = list(argv)
        assert argv[0] != "ssh", "脚本不得连接任何其他机器"
        if "-c" in argv and ctl.VALIDATE_SNIPPET in argv:
            return ctl.default_runner(argv, input_bytes, timeout)        # 真实的运行包校验
        if "egress-install" in argv:
            assert argv[2] == str(self.script), "必须从现行运行包内执行安装"
            role = argv[argv.index("--role") + 1]
            self.calls.append(f"install_{role}")
            if self.fail_install_when_new and self.policy_path.read_bytes() != self.old_bytes:
                return ctl.Result(1, b"", "安装器失败".encode())
            self.installed_bytes = self.policy_path.read_bytes()
            self.write_installed(self.installed_bytes)
            return ctl.Result(0, b"{}")
        if "egress-check" in argv:
            self.calls.append("check")
            ok = (not self.check_never_passes) and self.installed_bytes == self.policy_path.read_bytes()
            return ctl.Result(0 if ok else 1, b"{}" if ok else b"", b"" if ok else "策略摘要与安装记录不一致".encode())
        if argv[:2] == ["systemctl", "is-active"]:
            self.calls.append("guard_active")
            return ctl.Result(1 if self.guard_inactive else 0)
        raise AssertionError(f"未模拟的命令：{argv}")


def new_policy(world: World, *, revision: int | None = None, address: str | None = None, actor: str | None = None) -> dict:
    policy = copy.deepcopy(world.old_policy)
    policy["revision"] = revision if revision is not None else policy["revision"] + 1
    address = address or runtime_egress_fixtures.FIXTURE_ALTERNATE_EXIT_IPV4S[0]
    policy["allowed_public_ipv4"] = [address]
    policy["nodes"]["exit"]["endpoint"]["ipv4"] = address
    policy["authorization"]["actor"] = actor or policy["authorization"]["actor"]
    return policy


def expected_sha(policy: dict) -> str:
    return digest(json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n")


class EgressCtlTestCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.world = World(self.root / "origin-host")
        self.out, self.err = io.StringIO(), io.StringIO()

    def draft(self, policy: dict) -> str:
        path = self.root / "draft.json"
        path.write_text(json.dumps(policy), encoding="utf-8")
        return str(path)

    def run_ctl(self, *argv: str, root: bool = True, world: World | None = None) -> int:
        cfg = (world or self.world).config(self.out, self.err, root=root)
        return ctl.main(list(argv), cfg)

    def change_args(self, policy: dict, role: str, *extra: str) -> list[str]:
        return ["apply", "--draft", self.draft(policy), "--authorized-by", policy["authorization"]["actor"],
                "--reason", "更换指定出口", "--role", role, *extra]

    def audit_entries(self, world: World | None = None) -> list[dict]:
        path = (world or self.world).runtime / "audit.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def exit_record(self) -> str:
        """取出口机 apply 成功后打印的那一行审计记录。"""

        return next(line for line in self.out.getvalue().splitlines() if line.startswith(ctl.RECORD_PREFIX))


class ValidationTests(EgressCtlTestCase):
    def test_validate_uses_the_installed_bundle_validator(self) -> None:
        self.assertEqual(self.run_ctl("validate", "--draft", self.draft(new_policy(self.world))), 0)
        self.assertIn("校验通过", self.out.getvalue())
        broken = new_policy(self.world)
        del broken["allowed_public_ipv4"]
        self.assertEqual(self.run_ctl("validate", "--draft", self.draft(broken)), 1)
        self.assertIn("运行包的策略校验拒绝", self.err.getvalue())

    def test_change_commands_require_authorization_reason_and_role(self) -> None:
        draft = self.draft(new_policy(self.world))
        for label, argv in (("diff 缺授权人", ["diff", "--draft", draft, "--reason", "x"]),
                            ("diff 缺原因", ["diff", "--draft", draft, "--authorized-by", "x"]),
                            ("apply 缺授权人", ["apply", "--draft", draft, "--reason", "x", "--role", "exit"]),
                            ("apply 缺原因", ["apply", "--draft", draft, "--authorized-by", "x", "--role", "exit"]),
                            ("apply 缺角色", ["apply", "--draft", draft, "--authorized-by", "x", "--reason", "x"]),
                            ("apply 角色非法", ["apply", "--draft", draft, "--authorized-by", "x", "--reason", "x", "--role", "both"])):
            with self.subTest(label), contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_ctl(*argv)

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
    def test_dry_run_changes_nothing_for_either_role(self) -> None:
        policy = new_policy(self.world)
        for role in ("exit", "origin"):
            with self.subTest(role):
                before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file() and path.name != "draft.json"}
                self.assertEqual(self.run_ctl(*self.change_args(policy, role, "--dry-run")), 0, self.err.getvalue())
                after = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file() and path.name != "draft.json"}
                self.assertEqual(before, after, "dry-run 不得新增或改动任何文件")
        self.assertEqual(self.audit_entries(), [])
        self.assertFalse((self.world.runtime / "policy-backups").exists())
        self.assertFalse([call for call in self.world.calls if call.startswith("install")], "dry-run 不得安装")
        self.assertIn("--dry-run", self.out.getvalue())

    def test_apply_refuses_without_the_safety_switches(self) -> None:
        policy = new_policy(self.world)
        for label, argv, root in (("非 root", self.change_args(policy, "exit", "--confirm-maintenance-window"), False),
                                  ("缺维护窗口确认", self.change_args(policy, "exit"), True)):
            with self.subTest(label):
                self.assertEqual(self.run_ctl(*argv, root=root), 1)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.audit_entries(), [])
        self.assertEqual(self.world.calls, [])

    def test_no_ssh_anywhere(self) -> None:
        policy = new_policy(self.world)
        self.assertEqual(self.run_ctl(*self.change_args(policy, "exit", "--confirm-maintenance-window")), 0, self.err.getvalue())
        self.assertFalse([call for call in self.world.calls if "ssh" in call])


class ExitStageTests(EgressCtlTestCase):
    """出口机本机执行：校验、diff、备份、审计、安装、核对，并打印交给源端的审计记录。"""

    def setUp(self) -> None:
        super().setUp()
        self.world = World(self.root / "exit-host")

    def apply(self, policy: dict, *extra: str) -> int:
        return self.run_ctl(*self.change_args(policy, "exit", "--confirm-maintenance-window", *extra))

    def test_success_installs_exit_role_checks_and_prints_the_handoff_record(self) -> None:
        policy = new_policy(self.world)
        self.assertEqual(self.apply(policy), 0, self.err.getvalue())
        self.assertEqual(self.world.calls, ["install_exit", "guard_active"])
        self.assertEqual(json.loads(self.world.policy_path.read_text(encoding="utf-8")), policy)
        backups = sorted((self.world.runtime / "policy-backups").glob("policy-*.json"))
        self.assertTrue(any(path.read_bytes() == self.world.old_bytes for path in backups))
        entries = self.audit_entries()
        self.assertEqual([(item["event"], item["role"]) for item in entries], [("begin", "exit"), ("result", "exit")])
        self.assertEqual(entries[1]["status"], "ok")
        self.assertEqual(entries[1]["new_policy_sha256"], expected_sha(policy))
        self.assertTrue(ctl.audit_verify(self.world.runtime / "audit.jsonl")[0])
        record = json.loads(self.exit_record()[len(ctl.RECORD_PREFIX):])
        self.assertEqual(record, entries[1], "打印出来的就是审计里那一条")
        self.assertIn("new_policy_sha256=" + expected_sha(policy), self.out.getvalue())

    def test_install_failure_rolls_back_the_local_exit(self) -> None:
        self.world.fail_install_when_new = True
        self.assertEqual(self.apply(new_policy(self.world)), 2)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.world.installed_bytes, self.world.old_bytes)
        self.assertEqual(self.world.calls.count("install_exit"), 2)
        last = self.audit_entries()[-1]
        self.assertEqual((last["event"], last["status"], last["rollback"]["status"]), ("result", "failed", "ok"))
        self.assertFalse(any(line.startswith(ctl.RECORD_PREFIX) for line in self.out.getvalue().splitlines()), "失败不得打印交接记录")

    def test_guard_not_active_rolls_back_and_unrecoverable_state_returns_three(self) -> None:
        self.world.guard_inactive = True
        self.assertEqual(self.apply(new_policy(self.world), "--check-timeout", "0"), 3)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes, "回滚后旧策略已写回")
        last = self.audit_entries()[-1]
        self.assertEqual((last["status"], last["rollback"]["status"]), ("failed", "failed"))

    def test_fresh_exit_machine_uses_the_copied_bundle_and_has_nothing_to_roll_back(self) -> None:
        self.world = World(self.root / "fresh-exit", fresh=True)
        policy = new_policy(self.world)
        self.assertEqual(self.apply(policy), 0, self.err.getvalue())
        entry = self.audit_entries()[-1]
        self.assertEqual((entry["old_policy_sha256"], entry["status"]), (None, "ok"))
        self.assertIn("本机没有现行策略", self.out.getvalue())
        failing = World(self.root / "fresh-exit-2", fresh=True)
        failing.fail_install_when_new = True
        failing.old_bytes = b"\x00"          # 全新机器没有“旧策略”：任何已写入的策略都算新的
        self.assertEqual(self.run_ctl(*self.change_args(policy, "exit", "--confirm-maintenance-window"), world=failing), 3)
        self.assertIn("无旧策略可回滚", json.dumps(self.audit_entries(failing)[-1], ensure_ascii=False))

    def test_fresh_exit_machine_without_a_bundle_dir_is_refused(self) -> None:
        fresh = World(self.root / "fresh-exit-3", fresh=True)
        cfg = fresh.config(self.out, self.err)
        cfg.bundle_dir = None
        self.assertEqual(ctl.main(["validate", "--draft", self.draft(new_policy(self.world))], cfg), 1)
        self.assertIn("--bundle-dir", self.err.getvalue())


class OriginStageTests(EgressCtlTestCase):
    """源端（ARM64）本机执行：先核对出口端交来的审计记录／摘要与本地草稿一致，再安装。"""

    def exit_stage(self, policy: dict) -> str:
        """在一台独立的出口机上完成出口端 apply，返回它打印的审计记录行。"""

        exit_world = World(self.root / "exit-host")
        cfg = exit_world.config(self.out, self.err)
        argv = ["apply", "--draft", self.draft(policy), "--authorized-by", policy["authorization"]["actor"], "--reason", "更换指定出口",
                "--role", "exit", "--confirm-maintenance-window"]
        self.assertEqual(ctl.main(argv, cfg), 0, self.err.getvalue())
        line = self.exit_record()
        self.out.truncate(0), self.out.seek(0)
        return line

    def origin(self, policy: dict, *extra: str, **kwargs) -> int:
        return self.run_ctl(*self.change_args(policy, "origin", "--confirm-maintenance-window", *extra), **kwargs)

    def assert_untouched(self) -> None:
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.audit_entries(), [])
        self.assertEqual(self.world.calls, [])
        self.assertFalse((self.world.runtime / "policy-backups").exists())

    def test_two_stage_handoff_with_the_audit_record(self) -> None:
        policy = new_policy(self.world)
        record = self.exit_stage(policy)
        self.assertEqual(self.origin(policy, "--exit-audit-record", record), 0, self.err.getvalue())
        self.assertEqual(self.world.calls, ["install_origin", "check"])
        self.assertEqual(json.loads(self.world.policy_path.read_text(encoding="utf-8")), policy)
        entries = self.audit_entries()
        self.assertEqual([(item["event"], item["role"]) for item in entries], [("begin", "origin"), ("result", "origin")])
        self.assertEqual(entries[0]["exit_policy_sha256"], expected_sha(policy))
        self.assertEqual(entries[0]["exit_record_entry_sha256"], json.loads(record[len(ctl.RECORD_PREFIX):])["entry_sha256"])
        self.assertEqual(entries[1]["status"], "ok")

    def test_digest_only_handoff_is_accepted_and_both_must_agree(self) -> None:
        policy = new_policy(self.world)
        record = self.exit_stage(policy)
        self.assertEqual(self.origin(policy, "--exit-policy-sha256", expected_sha(policy)), 0, self.err.getvalue())
        self.assertEqual(self.audit_entries()[0]["exit_record_entry_sha256"], None)
        other = World(self.root / "origin-2")
        self.assertEqual(ctl.main(self.change_args(policy, "origin", "--confirm-maintenance-window", "--exit-audit-record", record,
                                                   "--exit-policy-sha256", "0" * 64), other.config(self.out, self.err)), 1)
        self.assertEqual(other.calls, [])

    def test_missing_handoff_is_refused_before_any_change(self) -> None:
        self.assertEqual(self.origin(new_policy(self.world)), 1)
        self.assertIn("交接凭据", self.err.getvalue())
        self.assert_untouched()

    def test_digest_from_a_different_policy_is_refused_before_any_change(self) -> None:
        policy = new_policy(self.world)
        other = new_policy(self.world, address=runtime_egress_fixtures.FIXTURE_ALTERNATE_EXIT_IPV4S[1])
        record = self.exit_stage(other)          # 出口端装的是另一份
        for label, extra in (("整行记录", ("--exit-audit-record", record)), ("仅摘要", ("--exit-policy-sha256", expected_sha(other)))):
            with self.subTest(label):
                self.err.truncate(0), self.err.seek(0)
                self.assertEqual(self.origin(policy, *extra), 1)
                self.assertIn("不一致", self.err.getvalue())
        self.assert_untouched()

    def test_malformed_or_wrong_kind_of_record_is_refused(self) -> None:
        policy = new_policy(self.world)
        record = self.exit_stage(policy)
        entry = json.loads(record[len(ctl.RECORD_PREFIX):])
        tampered = dict(entry, new_policy_sha256=expected_sha(policy), reason="被改的原因")           # 自摘要不再成立
        wrong_role = dict(entry, role="origin")
        failed = dict(entry, status="failed")
        begin = dict(entry, event="begin")
        for name, value in (("被改动", tampered), ("角色不是出口端", wrong_role), ("状态不是 ok", failed), ("不是 result 记录", begin)):
            if name != "被改动":
                value["entry_sha256"] = ctl._entry_hash(value)       # 自摘要有效，只是种类不对
            with self.subTest(name):
                self.assertEqual(self.origin(policy, "--exit-audit-record", json.dumps(value)), 1)
        for name, text in (("不是 JSON", "EGRESS_CTL_AUDIT_RECORD {oops"), ("摘要不是 64 位十六进制", None)):
            with self.subTest(name):
                argv = ("--exit-audit-record", text) if text else ("--exit-policy-sha256", "abc")
                self.assertEqual(self.origin(policy, *argv), 1)
        self.assert_untouched()

    def test_revision_mismatch_between_record_and_draft_is_refused(self) -> None:
        policy = new_policy(self.world)
        record = self.exit_stage(policy)
        same_content_other_revision = copy.deepcopy(policy)
        same_content_other_revision["revision"] += 1
        self.assertEqual(self.origin(same_content_other_revision, "--exit-audit-record", record), 1)
        self.assert_untouched()

    def test_dry_run_checks_a_given_record_but_does_not_require_one(self) -> None:
        policy = new_policy(self.world)
        self.assertEqual(self.run_ctl(*self.change_args(policy, "origin", "--dry-run")), 0, self.err.getvalue())
        self.assertIn("dry-run 未提供", self.out.getvalue())
        self.assertEqual(self.run_ctl(*self.change_args(policy, "origin", "--dry-run", "--exit-policy-sha256", "1" * 64)), 1)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.audit_entries(), [])
        self.assertFalse([call for call in self.world.calls if call.startswith("install")])

    def test_origin_failure_rolls_back_only_the_origin_and_says_how_to_roll_back_the_exit(self) -> None:
        policy = new_policy(self.world)
        record = self.exit_stage(policy)
        self.world.fail_install_when_new = True
        self.assertEqual(self.origin(policy, "--exit-audit-record", record), 2)
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        self.assertEqual(self.world.installed_bytes, self.world.old_bytes)
        self.assertEqual(self.world.calls, ["install_origin", "install_origin", "check"])
        self.assertIn("rollback 子命令", self.err.getvalue())
        self.assertEqual(self.audit_entries()[-1]["rollback"]["status"], "ok")

    def test_check_never_passing_returns_three(self) -> None:
        policy = new_policy(self.world)
        record = self.exit_stage(policy)
        self.world.check_never_passes = True
        self.assertEqual(self.origin(policy, "--exit-audit-record", record, "--check-timeout", "0"), 3)
        last = self.audit_entries()[-1]
        self.assertEqual((last["status"], last["rollback"]["status"]), ("failed", "failed"))

    def test_rollback_subcommand_restores_a_backup_on_either_role(self) -> None:
        policy = new_policy(self.world)
        record = self.exit_stage(policy)
        self.assertEqual(self.origin(policy, "--exit-audit-record", record), 0)
        backup = next(path for path in (self.world.runtime / "policy-backups").glob("policy-*-origin-*.json") if not path.name.endswith(".installed.json"))
        self.assertEqual(backup.read_bytes(), self.world.old_bytes)
        argv = ["rollback", "--role", "origin", "--backup", str(backup), "--authorized-by", "老板", "--reason", "回退"]
        self.assertEqual(self.run_ctl(*argv), 1, "缺维护窗口确认")
        self.assertEqual(self.run_ctl(*argv, "--confirm-maintenance-window"), 0, self.err.getvalue())
        self.assertEqual(self.world.policy_path.read_bytes(), self.world.old_bytes)
        last = self.audit_entries()[-1]
        self.assertEqual((last["kind"], last["event"], last["status"]), ("rollback", "result", "ok"))


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
        source = SCRIPT.read_text(encoding="utf-8")
        self.assertNotIn('"ssh"', source, "两段式：脚本不连接任何其他机器，ARM64 不需要到出口机的凭据")
        self.assertNotIn("subprocess.Popen", source)

    def test_script_and_runbook_contain_no_concrete_egress_address(self) -> None:
        import ipaddress
        for path in (SCRIPT, SCRIPT.with_name("RUNBOOK.md")):
            for token in re.findall(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])", path.read_text(encoding="utf-8")):
                with self.subTest(path=path.name, token=token):
                    self.assertFalse(ipaddress.IPv4Address(token).is_global)


if __name__ == "__main__":
    unittest.main()
