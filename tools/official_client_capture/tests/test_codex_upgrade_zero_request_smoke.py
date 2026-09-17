"""零请求 smoke：全部步骤通过、网络守卫生效、收据一次性写入。"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade_zero_request_smoke as smoke


class ZeroRequestSmokeTests(unittest.TestCase):
    def test_provenance_import_does_not_reenter_test_fixtures(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "from tools.official_client_capture import "
                    "codex_upgrade_live_request_provenance"
                ),
            ],
            cwd=Path(__file__).resolve().parents[3],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_passes_on_staging_fixture_and_blocks_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve() / "staging" / "smoke"
            receipt = smoke.run_smoke(root)
            self.assertEqual(receipt["status"], "passed", receipt["steps"])
            self.assertEqual(receipt["network_attempts"], 0)
            self.assertEqual(receipt["live_request_count"], 0)
            self.assertTrue(receipt["summary"]["project_ledger"]["admitted"]["seal"])
            self.assertEqual(receipt["summary"]["ledger_close"]["status"], "closed")
            with smoke._NetworkGuard() as guard:
                with socket.socket() as guarded_socket:
                    with self.assertRaises(smoke.SmokeError):
                        guarded_socket.connect(("127.0.0.1", 9))
            self.assertEqual(len(guard.attempts), 1)
            # 守卫退出后 socket 恢复原样
            self.assertIs(socket.socket.connect, guard._original_connect)

    def test_smoke_rejects_non_staging_root_and_writes_receipt_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory).resolve()
            with self.assertRaisesRegex(smoke.SmokeError, "staging"):
                smoke.run_smoke(base / "not-staging")
            output = base / "audit" / "smoke.json"
            self.assertEqual(smoke.main(["--staging-root", str(base / "staging" / "s1"), "--output", str(output)]), 0)
            self.assertEqual(json.loads(output.read_text("utf-8"))["status"], "passed")
            self.assertEqual(smoke.main(["--staging-root", str(base / "staging" / "s1"), "--output", str(output)]), 2)


if __name__ == "__main__":
    unittest.main()
