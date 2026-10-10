"""工程磁盘策略只服务私有 fixture_only staging 根；正式判据与最低余量保持原合同。"""

from __future__ import annotations

import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from tools.ci import entry_housekeeping as hk
from tools.official_client_capture import codex_upgrade_project_ledger as ledger


class EngineeringRootDiskPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.data = self.base / "data"
        self.root = self.data / "staging" / "engineering-run"
        self.root.mkdir(mode=0o700, parents=True)
        for directory in (self.root / "evidence", self.root / "evidence" / "campaigns"):
            directory.mkdir(mode=0o700)
        self.values = {"D": str(self.data), "ENTRY_ROOT": str(self.root), "ENGINEERING_ROOT_MAX_USED_PERCENT": "75"}

    def _ledger(self, client: str = "codex") -> Path:
        self.values["CLIENT"] = client
        return ledger.create_fixture_ledger(self.root / "evidence" / "campaigns", client=client)

    def _usage(self, used: float, free: float) -> object:
        total = 200 * 2 ** 30
        return mock.patch.object(hk.shutil, "disk_usage", return_value=types.SimpleNamespace(
            total=total, used=int(total * used / 100), free=int(free * 2 ** 30)))

    def _change_plan(self, root: Path, **changes: object) -> None:
        path = root / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan.update(changes)
        plan["plan_sha256"] = ledger._digest({key: value for key, value in plan.items() if key != "plan_sha256"})
        path.write_text(json.dumps(plan), encoding="utf-8")

    def test_default_keeps_69_percent_and_does_not_read_a_ledger(self) -> None:
        with self._usage(70, 50), mock.patch.object(hk, "_parse_env", side_effect=AssertionError("缺省不得读总账")):
            disk = hk.disk_status(environment={"D": str(self.data), "ENTRY_ROOT": str(self.root)})
        self.assertEqual((disk["max_used_percent"], disk["min_free_gib"], disk["ok"]), (69.0, 40.0, False))
        self.assertNotIn("engineering_policy", disk)

    def test_both_clients_use_complete_existing_fixture_plans(self) -> None:
        for client in ("codex", "claude"):
            with self.subTest(client=client):
                self._ledger(client)
                with self._usage(72, 50):
                    disk = hk.disk_status(environment=self.values)
                self.assertEqual((disk["max_used_percent"], disk["ok"]), (75.0, True))
                policy = disk["engineering_policy"]
                self.assertEqual((policy["client"], policy["entry_root"]), (client, str(self.root)))
                self.assertEqual(policy["plan_sha256"], ledger._load_plan(Path(policy["ledger_dir"]))[0]["plan_sha256"])

    def test_40_gib_floor_and_higher_operator_floor_remain_enforced(self) -> None:
        self._ledger()
        for free, minimum, expected in ((39, 10, False), (41, 10, True), (49, 50, False), (51, 50, True)):
            with self.subTest(free=free, minimum=minimum), self._usage(72, free):
                disk = hk.disk_status(environment=self.values, min_free_gib=minimum)
                self.assertEqual((disk["min_free_gib"], disk["ok"]), (max(40, minimum), expected))

    def test_missing_plan_fails_before_any_cleanup(self) -> None:
        with mock.patch.object(hk, "trim_go_cache", side_effect=AssertionError("不得先清理")):
            with self.assertRaisesRegex(hk.HousekeepingError, "工程磁盘策略拒绝"):
                hk.run(data_root=self.data, runroot=self.base / "run", environment=self.values)

    def test_formal_plan_with_valid_digest_is_rejected(self) -> None:
        root = self._ledger()
        self._change_plan(root, fixture_only=False)
        with self.assertRaisesRegex(hk.HousekeepingError, "fixture_only"):
            hk.disk_policy(self.values)

    def test_invalid_self_digest_is_rejected(self) -> None:
        root = self._ledger()
        path = root / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["fixture_only"] = False
        path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaisesRegex(hk.HousekeepingError, "自摘要"):
            hk.disk_policy(self.values)

    def test_complete_existing_plan_contract_is_required(self) -> None:
        root = self._ledger()
        self._change_plan(root, fixture_only="true")
        with self.assertRaisesRegex(hk.HousekeepingError, "必须是布尔"):
            hk.disk_policy(self.values)

    def test_missing_and_formal_entry_roots_are_rejected(self) -> None:
        for root in (None, self.data, self.data / "staging", self.base / "elsewhere", self.data / "staging" / "missing"):
            values = dict(self.values)
            if root is None:
                values.pop("ENTRY_ROOT")
            else:
                values["ENTRY_ROOT"] = str(root)
            with self.subTest(root=root), self.assertRaises(hk.HousekeepingError):
                hk.disk_policy(values)

    def test_engineering_root_must_be_private(self) -> None:
        self._ledger()
        self.root.chmod(0o755)
        with self.assertRaisesRegex(hk.HousekeepingError, "不可访问"):
            hk.disk_policy(self.values)

    def test_root_and_ledger_symlinks_are_rejected(self) -> None:
        self._ledger()
        alias = self.data / "staging" / "alias"
        alias.symlink_to(self.root, target_is_directory=True)
        with self.assertRaisesRegex(hk.HousekeepingError, "符号链接"):
            hk.disk_policy({**self.values, "ENTRY_ROOT": str(alias)})
        actual = self.root / "evidence"
        moved = self.root / "evidence-actual"
        actual.rename(moved)
        actual.symlink_to(moved, target_is_directory=True)
        with self.assertRaisesRegex(hk.HousekeepingError, "符号链接"):
            hk.disk_policy(self.values)

    def test_foreign_client_plan_is_rejected(self) -> None:
        root = self._ledger("codex")
        root.rename(root.with_name(ledger.ledger_dir_name("claude")))
        with self.assertRaisesRegex(hk.HousekeepingError, "与 claude 不一致"):
            hk.disk_policy({**self.values, "CLIENT": "claude"})

    def test_invalid_percent_values_are_rejected(self) -> None:
        for value in ("", "69", "74", "76", "NaN", "inf", "-75", "7.5e1"):
            with self.subTest(value=value), self.assertRaises(hk.HousekeepingError):
                hk.disk_policy({**self.values, "ENGINEERING_ROOT_MAX_USED_PERCENT": value})


if __name__ == "__main__":
    unittest.main()
