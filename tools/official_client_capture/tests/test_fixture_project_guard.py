"""guard 的私有 fixture 总账接点：真实总账重放、锁内复核和既有业务停线，均无需账号。"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from tools.ci import entry_housekeeping as housekeeping
from tools.official_client_capture import codex_upgrade_project_ledger as ledger

REPOSITORY = Path(__file__).resolve().parents[3]
DRIVER = REPOSITORY / "tools/arm64_capture_driver/driver"
GUARD = DRIVER / "guard.sh"
LEDGER_MODULE = "tools.official_client_capture.codex_upgrade_project_ledger"
SOURCE = GUARD.read_text(encoding="utf-8")
PROJECT_START = SOURCE.index("guard_project_status() {")
PROJECT_FRAGMENT = SOURCE[PROJECT_START:SOURCE.index('\nif [ "$MODE" = pre-vc4 ]', PROJECT_START)]
PROJECT_PYTHON = re.search(r'python3 -B - "\$DRV" "\$CT_PROJECT_LEDGER_MODULE" <<\'PY\'\n(.*?)\nPY',
                           PROJECT_FRAGMENT, re.DOTALL).group(1)
CONSUMER_PYTHON = PROJECT_FRAGMENT.split('guard_project_status | python3 -c "\n', 1)[1].split('\n" "${TARGET_VERSION:-}"', 1)[0]


class FixtureProjectGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.data = self.root / "data"
        self.entry = self.data / "staging/engineering-run"
        self.entry.mkdir(parents=True, mode=0o700)
        for directory in (self.entry / "evidence", self.entry / "evidence/campaigns"):
            directory.mkdir(mode=0o700)

    def _fixture(self, client: str = "claude", entry: Path | None = None) -> Path:
        entry = self.entry if entry is None else entry
        for directory in (entry, entry / "evidence", entry / "evidence/campaigns"):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return ledger.create_fixture_ledger(entry / "evidence/campaigns", client=client)

    def _default(self, client: str) -> Path:
        for directory in (self.data / "evidence", self.data / "evidence/campaigns"):
            directory.mkdir(mode=0o700, exist_ok=True)
        root = self.data / "evidence/campaigns" / ledger.ledger_dir_name(client)
        ledger.create_project_ledger(
            root, project_id="isolated-guard-test",
            absolute_deadline_utc=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            deadline_approved_by="isolated-test", estimation_policy="upper_bound_from_sibling_or_turn_ratio",
            estimation_policy_approved_by="isolated-test", fixture_only=False, client=client,
        )
        return root

    def _environment(self, client: str = "claude", *, engineering: bool = True, **changes: str) -> dict[str, str]:
        values = {"PATH": os.environ["PATH"], "PYTHONPATH": str(REPOSITORY), "PYTHONDONTWRITEBYTECODE": "1",
                  "D": str(self.data), "ENTRY_ROOT": str(self.entry), "DRV": str(DRIVER), "CLIENT": client,
                  "CT_PROJECT_LEDGER_MODULE": LEDGER_MODULE, "CT_PROJECT_LEDGER_DIR_NAME": ledger.ledger_dir_name(client),
                  "TARGET_VERSION": "2.1.226" if client == "claude" else "0.160.0"}
        if engineering:
            values["ENGINEERING_ROOT_MAX_USED_PERCENT"] = "75"
        values.update(changes)
        return values

    def _run(self, client: str = "claude", *, engineering: bool = True, **changes: str) -> subprocess.CompletedProcess[str]:
        # 只运行真实脚本的项目接点；安装、出口与磁盘探测由原入口负责，本测试不触发网络或现场维护。
        return subprocess.run(["bash", "-c", "set -Eeuo pipefail\n" + PROJECT_FRAGMENT], cwd=REPOSITORY,
                              env=self._environment(client, engineering=engineering, **changes),
                              capture_output=True, text=True, timeout=30)

    def _python(self, client: str = "claude") -> tuple[str, str]:
        output, errors = io.StringIO(), io.StringIO()
        original_umask = os.umask(0o077)
        try:
            with mock.patch.object(sys, "argv", ["guard-project", str(DRIVER), LEDGER_MODULE]), \
                    mock.patch.object(sys, "path", sys.path.copy()), \
                    mock.patch.dict(sys.modules, {"entry_housekeeping": housekeeping}), \
                    mock.patch.dict(os.environ, self._environment(client), clear=True), \
                    contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                exec(compile(PROJECT_PYTHON, str(GUARD), "exec"), {})
        finally:
            os.umask(original_umask)
        return output.getvalue(), errors.getvalue()

    def _change_plan(self, root: Path, **changes: object) -> None:
        path = root / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan.update(changes)
        plan["plan_sha256"] = ledger._digest({key: value for key, value in plan.items() if key != "plan_sha256"})
        path.write_text(json.dumps(plan), encoding="utf-8")

    def test_default_uses_original_path_for_both_clients_even_with_entry_root(self) -> None:
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                default = self._default(client)
                private = self._fixture(client)
                (private / "plan.json").write_text("{}\n", encoding="utf-8")
                before = (default / "head.json").read_bytes()
                result = self._run(client, engineering=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("guard/project:", result.stdout)
                self.assertEqual((default / "head.json").read_bytes(), before)

    def test_default_missing_ledger_fails_without_creating_or_using_fixture(self) -> None:
        self._fixture()
        result = self._run(engineering=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("锁文件无法打开", result.stderr)
        self.assertFalse((self.data / "evidence").exists())

    def test_engineering_uses_complete_private_ledger_for_both_clients(self) -> None:
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                root = self._fixture(client)
                before = (root / "head.json").read_bytes()
                result = self._run(client)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("guard/project:", result.stdout)
                self.assertEqual((root / "head.json").read_bytes(), before)
                self.assertFalse((self.data / "evidence").exists())

    def test_invalid_fixture_qualification_is_rejected_without_formal_writes(self) -> None:
        cases = (("missing", "plan"), ("formal", "fixture_only"), ("foreign", "与 claude 不一致"),
                 ("digest", "自摘要"), ("mode", "不可访问"), ("root-link", "符号链接"), ("ledger-link", "符号链接"))
        for name, message in cases:
            with self.subTest(case=name):
                entry = self.data / "staging" / name
                root = self._fixture("codex" if name == "foreign" else "claude", entry)
                selected = entry
                if name == "missing":
                    (root / "plan.json").unlink()
                elif name == "formal":
                    self._change_plan(root, fixture_only=False)
                elif name == "foreign":
                    root.rename(root.with_name(ledger.ledger_dir_name("claude")))
                elif name == "digest":
                    plan = json.loads((root / "plan.json").read_text())
                    plan["plan_sha256"] = "0" * 64
                    (root / "plan.json").write_text(json.dumps(plan))
                elif name == "mode":
                    entry.chmod(0o755)
                elif name == "root-link":
                    selected = entry.with_name("root-alias")
                    selected.symlink_to(entry, target_is_directory=True)
                elif name == "ledger-link":
                    actual = root.with_name("ledger-actual")
                    root.rename(actual)
                    root.symlink_to(actual, target_is_directory=True)
                result = self._run(ENTRY_ROOT=str(selected))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse((self.data / "evidence").exists())

    def test_policy_recheck_and_replay_share_the_project_lock(self) -> None:
        self._fixture()
        lock_depths = []
        original_policy, original_replay = housekeeping.disk_policy, ledger.replay_head

        def policy(environment):
            lock_depths.append(ledger._PROJECT_LOCK_DEPTH)
            return original_policy(environment)

        def replay(root):
            self.assertEqual(ledger._PROJECT_LOCK_DEPTH, 1)
            return original_replay(root)

        with mock.patch.object(housekeeping, "disk_policy", side_effect=policy), \
                mock.patch.object(ledger, "replay_head", side_effect=replay):
            output, errors = self._python()
        self.assertEqual(lock_depths, [0, 1])
        self.assertEqual(ledger._PROJECT_LOCK_DEPTH, 0)
        self.assertEqual(json.loads(output)["sequence"], 0)
        self.assertEqual(errors, "")

    def test_plan_digest_or_fixture_qualification_change_after_lock_is_rejected_before_replay(self) -> None:
        root = self._fixture()
        original_plan = (root / "plan.json").read_bytes()
        original_lock = ledger.project_lock
        for changes in ({"project_id": "valid-but-drifted"}, {"fixture_only": False}):
            @contextlib.contextmanager
            def drifted_lock(locked_root):
                with original_lock(locked_root):
                    self._change_plan(locked_root, **changes)
                    yield

            with self.subTest(changes=changes), mock.patch.object(ledger, "project_lock", drifted_lock), \
                    mock.patch.object(ledger, "replay_head", side_effect=AssertionError("漂移后不得重放")), \
                    self.assertRaises(SystemExit) as caught:
                self._python()
            self.assertEqual(caught.exception.code, 2)
            self.assertEqual(ledger._PROJECT_LOCK_DEPTH, 0)
            (root / "plan.json").write_bytes(original_plan)

    def test_original_business_stops_reject_successful_fixture_heads(self) -> None:
        self._fixture()
        output, _ = self._python()
        head = json.loads(output)
        cases = (({"blocked": True}, "总账 blocked"),
                 ({"root_causes_at_limit_base": ["rc1-guard"]}, "同根因重试上限"),
                 ({"remaining_live_requests": 0}, "剩余请求预算"),
                 ({"remaining_live_requests": True}, "剩余请求预算"),
                 ({"remaining_live_requests": "1"}, "剩余请求预算"))
        for changes, message in cases:
            with self.subTest(changes=changes), \
                    mock.patch.object(sys, "argv", ["guard-consumer", "2.1.226", LEDGER_MODULE]), \
                    mock.patch.object(sys, "stdin", io.StringIO(json.dumps({**head, **changes}))), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as caught:
                exec(compile(CONSUMER_PYTHON, str(GUARD), "exec"), {})
            self.assertIn(message, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
