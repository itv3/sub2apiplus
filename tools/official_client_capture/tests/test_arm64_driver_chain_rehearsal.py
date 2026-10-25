"""第 13 项：驱动入口 ``chain-rehearsal.sh`` 的接线测试（只验证入口把参数交给引擎、汇总落进 RUNROOT、退出码原样透传）。

引擎本身的语义在 ``test_chain_rehearsal*``；这里不重复，只挑一个本机秒级可跑的历史缺陷探针走一遍入口。
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.tests import test_arm64_capture_driver as driver_tests

SCRIPT = driver_tests.SCRIPTS / "chain-rehearsal.sh"
REPO_ROOT = Path(__file__).resolve().parents[3]


class ChainRehearsalDriverTests(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.fixture = driver_tests._DriverFixture(Path(self._directory.name).resolve())

    def run_script(self, *args: str):
        return driver_tests._run(SCRIPT, *args, env=self.fixture.env)

    def test_script_is_executable_and_registered_in_the_manifest_as_0700(self) -> None:
        manifest = json.loads((driver_tests.SCRIPTS.parent / "manifest.json").read_text(encoding="utf-8"))
        rows = {row["path"]: row for row in manifest["files"]}
        self.assertIn("driver/chain-rehearsal.sh", rows)
        self.assertEqual(rows["driver/chain-rehearsal.sh"]["mode"], "0700")
        self.assertTrue(SCRIPT.stat().st_mode & 0o111)

    def test_unknown_command_is_a_usage_error(self) -> None:
        result = self.run_script("frobnicate")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("用法", result.stderr)

    def test_list_shows_probes_chain_and_capabilities(self) -> None:
        result = self.run_script("list", "--tree", str(REPO_ROOT))
        self.assertEqual(result.returncode, 0, result.stderr)
        for expected in ("capture-host-container-id", "kilo-http-usage-order", "chain-vc1-capture", "能力：", "linux_root_unshare"):
            self.assertIn(expected, result.stdout)

    def test_round_runs_one_probe_writes_the_summary_under_runroot_and_returns_its_exit_code(self) -> None:
        result = self.run_script("round", "--probe", "kilo-http-usage-order", "--tree", str(REPO_ROOT))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        summaries = sorted((self.fixture.runroot / "chain-rehearsal").glob("*/round.json"))
        self.assertEqual(len(summaries), 1, result.stdout)
        data = json.loads(summaries[0].read_text(encoding="utf-8"))
        self.assertEqual((data["verdict"], [row["status"] for row in data["rows"]]), ("clean", ["passed"]))
        self.assertTrue((summaries[0].parent / "run.log").is_file())

    def test_usage_error_from_the_engine_is_passed_through(self) -> None:
        result = self.run_script("round", "--probe", "no-such-probe", "--tree", str(REPO_ROOT))
        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)

    def test_the_wrapper_never_leaves_bytecode_in_the_tree(self) -> None:
        self.run_script("list", "--tree", str(REPO_ROOT))
        self.assertEqual([path for path in (REPO_ROOT / "tools" / "official_client_capture").rglob("__pycache__")], [])


if __name__ == "__main__":
    unittest.main()
