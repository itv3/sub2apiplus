"""恢复账本的失败关闭测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture.capturelib.model import build_campaign_plan
from tools.official_client_capture.capturelib.recovery import (
    RecoveryJournal,
    find_unclean_journals,
)


class RecoveryJournalTest(unittest.TestCase):
    def _case(self):
        plan = build_campaign_plan(
            task="oauth",
            batch_id="recovery-test",
            scenarios=("s1",),
            evidence_modes=("direct",),
            sub2api_base_url=None,
            api_key_env="SUB2API_CAPTURE_API_KEY",
        )
        return plan.cases[0]

    def test_active_or_failed_cleanup_blocks_next_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            run_dir = run_root / "oauth" / "oauth-recovery-test"
            run_dir.mkdir(parents=True)
            journal = RecoveryJournal(run_dir)
            journal.activate(
                case=self._case(),
                scenario="s1",
                role="tcpdump",
                pid=123,
                pgid=123,
                output_dir=run_dir / "direct" / "claude-http" / "s1",
                port=None,
            )
            journal.deactivate(cleanup_successful=False)

            self.assertEqual(
                find_unclean_journals(run_root),
                ["oauth/oauth-recovery-test/recovery.json"],
            )
            self.assertIsNotNone(journal.data["active_resource"])

    def test_clean_journal_does_not_block_next_campaign(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            run_root = Path(directory)
            run_dir = run_root / "oauth" / "oauth-recovery-test"
            run_dir.mkdir(parents=True)
            journal = RecoveryJournal(run_dir)
            journal.finalize(status="complete", cleanup_successful=True)
            self.assertEqual(find_unclean_journals(run_root), [])


if __name__ == "__main__":
    unittest.main()
