"""U-1 冲突决策由理由文件生成（UM-22）：人只写理由，处置类型按 index 事实推断，签名后照常封存。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.upstream_merge.conflict_rationale import (
    CONFLICT_DECISIONS_NAME,
    conflict_input_from_rationale,
    draft_conflict_rationale,
)
from tools.upstream_merge.errors import UpstreamMergeError
from tools.upstream_merge.plan_inputs import AWAITING_MANUAL_INPUT
from tools.upstream_merge.tests.test_workflow import SyntheticRepository, run
from tools.upstream_merge.workflow import seal_merge, start_merge


class ConflictRationaleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.temp_root = Path(self.temporary.name)
        self.fixture: SyntheticRepository | None = None

    def tearDown(self) -> None:
        if self.fixture is not None:
            self.fixture.cleanup_worktree()
        self.temporary.cleanup()

    def test_draft_then_seal_from_rationale_with_inferred_resolution(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=True)
        plan = self.fixture.plan
        start_merge(plan)
        # 有冲突、没给决策：写理由草稿并以“待人工输入”停下；冲突未解决时不推断处置类型。
        draft = draft_conflict_rationale(plan)
        self.assertEqual(draft["result"], AWAITING_MANUAL_INPUT)
        self.assertEqual(draft["unresolved_paths"], ["conflict.txt"])
        self.assertEqual(json.loads(Path(draft["draft"]).read_text(encoding="utf-8")), {"conflict.txt": ""})
        run(self.fixture.worktree, "git", "checkout", "--ours", "conflict.txt")
        run(self.fixture.worktree, "git", "add", "conflict.txt")
        again = draft_conflict_rationale(plan)
        self.assertEqual(again["inferred_resolutions"], {"conflict.txt": "fork"})
        self.assertEqual(again["draft"], draft["draft"])

        rationale = self.temp_root / "rationale.json"
        rationale.write_text(json.dumps({"conflict.txt": "太短"}, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "至少 12 个字符"):
            conflict_input_from_rationale(plan, rationale, ["conflict.txt"], remaining=False)
        rationale.write_text(json.dumps({"other.txt": "保留我方控制边界的完整说明"}, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(UpstreamMergeError, "逐个覆盖全部冲突路径"):
            conflict_input_from_rationale(plan, rationale, ["conflict.txt"], remaining=False)

        rationale.write_text(
            json.dumps({"conflict.txt": "保留我方控制边界，上游此处改动不适用于本分支"}, ensure_ascii=False),
            encoding="utf-8",
        )
        decisions = conflict_input_from_rationale(plan, rationale, ["conflict.txt"], remaining=False)
        self.assertEqual(decisions.name, CONFLICT_DECISIONS_NAME)
        document = json.loads(decisions.read_text(encoding="utf-8"))
        self.assertEqual(document["resolutions"][0]["resolution"], "fork")
        self.assertIn("identity_sha256", document)
        candidate = seal_merge(plan, decisions)
        self.assertEqual(candidate["parents"], [self.fixture.fork, self.fixture.upstream])

    def test_clean_merge_needs_no_draft(self) -> None:
        self.fixture = SyntheticRepository(self.temp_root, conflict=False)
        start_merge(self.fixture.plan)
        self.assertIsNone(draft_conflict_rationale(self.fixture.plan))


if __name__ == "__main__":
    unittest.main()
