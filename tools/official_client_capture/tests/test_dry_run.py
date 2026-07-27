"""dry-run 必须零副作用、零秘密。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DryRunTest(unittest.TestCase):
    def test_documented_capture_tools_layout_is_importable(self) -> None:
        project_root = Path(__file__).resolve().parents[3]
        source = project_root / "tools" / "official_client_capture"
        with tempfile.TemporaryDirectory() as directory:
            capture_root = Path(directory) / "capture"
            deployed = capture_root / "tools" / "official_client_capture"
            shutil.copytree(
                source,
                deployed,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            completed = subprocess.run(
                [sys.executable, str(deployed / "capture.py"), "--help"],
                cwd=capture_root,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("--task", completed.stdout)

    def test_all_dry_run_does_not_create_run_root_or_read_secret(self) -> None:
        project_root = Path(__file__).resolve().parents[3]
        script = project_root / "tools" / "official_client_capture" / "capture.py"
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "must-not-exist"
            environment = dict(os.environ)
            environment["SUB2API_CAPTURE_API_KEY"] = "CANARY-SECRET"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--task",
                    "all",
                    "--dry-run",
                    "--batch-id",
                    "dry-run-test",
                    "--sub2api-base-url",
                    "https://gateway.example.com",
                    "--run-root",
                    str(run_root),
                ],
                cwd=project_root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(run_root.exists())
            self.assertNotIn("CANARY-SECRET", completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(payload["execution_order"], ["oauth", "api"])
            self.assertFalse(payload["external_ab_executed"])

    def test_oauth_token_override_dry_run_only_exposes_env_name(self) -> None:
        project_root = Path(__file__).resolve().parents[3]
        script = project_root / "tools" / "official_client_capture" / "capture.py"
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory) / "must-not-exist"
            environment = dict(os.environ)
            environment["CLAUDE_CAPTURE_OAUTH_TOKEN"] = "CANARY-SECRET"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(script),
                    "--task",
                    "oauth",
                    "--dry-run",
                    "--batch-id",
                    "oauth-override-dry-run",
                    "--subjects",
                    "claude-http",
                    "--claude-oauth-token-env",
                    "CLAUDE_CAPTURE_OAUTH_TOKEN",
                    "--run-root",
                    str(run_root),
                ],
                cwd=project_root,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(run_root.exists())
            self.assertNotIn("CANARY-SECRET", completed.stdout + completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertEqual(
                payload["plans"][0]["credential"]["claude_token_source_env"],
                "CLAUDE_CAPTURE_OAUTH_TOKEN",
            )


if __name__ == "__main__":
    unittest.main()
