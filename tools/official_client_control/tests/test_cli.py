"""类型化 CLI 不暴露绕过状态门禁的裸事实写入口。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.official_client_control.cli import FACT_COMMANDS, build_parser, execute
from tools.official_client_control.store import ControlStore


class CLITests(unittest.TestCase):
    def test_cli_has_typed_transitions_and_no_raw_append(self) -> None:
        parser = build_parser()
        help_text = parser.format_help()
        self.assertNotIn("fact-append", help_text)
        self.assertIn("evidence-approve", FACT_COMMANDS)
        self.assertIn("profile-approve", FACT_COMMANDS)
        self.assertIn("candidate-freeze", FACT_COMMANDS)
        self.assertIn("selector-activate", FACT_COMMANDS)

    def test_cli_initializes_private_store(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "store"
            arguments = build_parser().parse_args(
                [
                    "init-store",
                    "--root",
                    str(root),
                    "--created-at",
                    "2026-08-18T00:00:00Z",
                ]
            )
            result = execute(arguments)
            self.assertEqual(result["result"], "initialized")
            ControlStore(root)


if __name__ == "__main__":
    unittest.main()
