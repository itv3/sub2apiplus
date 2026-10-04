"""ChangeDecision 机械决定（UM-22）：只有 Git 事实成立的“上游单侧”“两侧自动合并未改”才能机械决定。"""

from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.upstream_merge import workflow
from tools.upstream_merge.gitops import changed_paths, rev_parse


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True).stdout.strip()


def write(root: Path, relative: str, text: str) -> None:
    (root / relative).write_text(text, encoding="utf-8")


class MechanicalMergeFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        git(root, "init", "-q", "-b", "main")
        git(root, "config", "user.name", "Mechanical Test")
        git(root, "config", "user.email", "mechanical@example.invalid")
        lines = "".join(f"line {index}\n" for index in range(1, 9))
        for name in ("a", "b", "c", "d", "g", "h", "e"):
            write(root, f"{name}.txt", f"{name}\n{lines}")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "base")
        base = rev_parse(root, "HEAD^{commit}")

        git(root, "checkout", "-q", "-b", "upstream")
        write(root, "a.txt", f"a upstream\n{lines}")  # 上游单侧
        write(root, "b.txt", f"b upstream\n{lines}")  # 两侧改不同行，自动合并
        write(root, "c.txt", f"c upstream\n{lines}")  # 与我方改同一行，冲突
        write(root, "d.txt", f"d upstream\n{lines}")  # 上游改，合并后又被适配
        write(root, "g.txt", f"g upstream\n{lines}")  # 两侧自动合并，合并后又被适配
        write(root, "n.txt", "new upstream file\n")  # 上游新增
        git(root, "add", "-A")
        git(root, "mv", "e.txt", "e2.txt")  # 上游改名
        git(root, "commit", "-q", "-m", "upstream")
        upstream = rev_parse(root, "HEAD^{commit}")

        git(root, "checkout", "-q", "main")
        write(root, "b.txt", f"b\n{lines}fork tail\n")
        write(root, "c.txt", f"c fork\n{lines}")
        write(root, "g.txt", f"g\n{lines}fork tail\n")
        git(root, "commit", "-q", "-am", "fork")
        fork = rev_parse(root, "HEAD^{commit}")

        merged = subprocess.run(["git", "merge", "--no-ff", "--no-commit", upstream], cwd=root, capture_output=True, text=True)
        self.assertNotEqual(merged.returncode, 0, "c.txt 应当冲突")
        write(root, "c.txt", f"c fork and upstream\n{lines}")
        git(root, "add", "-A")
        git(root, "commit", "-q", "-m", "merge")
        write(root, "d.txt", f"d upstream adapted\n{lines}")
        write(root, "g.txt", f"g upstream\n{lines}fork tail adapted\n")
        write(root, "h.txt", f"h adapted\n{lines}")  # 只有我方适配
        git(root, "commit", "-q", "-am", "adapt")
        self.source = rev_parse(root, "HEAD^{commit}")
        self.root = root
        self.start = {
            "fork_head": fork,
            "upstream_commit": upstream,
            "merge_base": base,
            "conflict_paths": ["c.txt"],
        }

    def facts(self) -> dict[str, str]:
        plan = SimpleNamespace(repository_root=self.root)
        changes = changed_paths(self.root, self.start["fork_head"], self.source)
        with mock.patch.object(workflow, "_load_merge_start", return_value=self.start):
            return workflow.mechanical_merge_facts(plan, changes, self.source)

    def test_only_git_facts_qualify_for_mechanical_decision(self) -> None:
        changes = {item["path"]: item for item in changed_paths(self.root, self.start["fork_head"], self.source)}
        # 七类变化都在 fork→候选 的变化清单里（改名带旧路径）。
        self.assertEqual(sorted(changes), ["a.txt", "b.txt", "c.txt", "d.txt", "e2.txt", "g.txt", "h.txt", "n.txt"])
        self.assertEqual(changes["e2.txt"].get("old_path"), "e.txt")
        self.assertEqual(
            self.facts(),
            {
                "a.txt": "upstream_only",
                "b.txt": "both_auto_merged",
                "e2.txt": "upstream_only",
                "n.txt": "upstream_only",
            },
        )

    def test_mechanical_item_keeps_categories_and_explains_fact(self) -> None:
        entry = {"path": "a.txt", "component_ownership": {"component_ids": ["service"]}}
        item = {
            "path": "a.txt",
            "categories": ["protocol_adapter"],
            "rationale": "待人工审查",
            "required_actions": ["人工确认组件所有权与直接依赖"],
            "official_client_identity_changed": False,
            "evidence_semantics_changed": False,
            "decision_source": "manual_required",
        }
        decided = workflow._mechanical_decision(item, entry, "upstream_only")
        self.assertEqual(decided["decision_source"], "mechanical")
        self.assertEqual(decided["categories"], ["protocol_adapter"])
        self.assertTrue(decided["rationale"].startswith("上游单侧改动，自动合入未做修改"))
        self.assertIn("所属组件：service", decided["rationale"])
        self.assertEqual(decided["required_actions"], sorted(workflow.MECHANICAL_DECISION_ACTIONS))


class SuggestionCountTest(unittest.TestCase):
    def test_mechanical_items_are_neither_auto_nor_unresolved(self) -> None:
        # 草稿计数必须与封存时的逐条重算一致：机械决定既不算自动放行，也不算待人工。
        entries = [
            {"path": name, "component_ownership": {"component_ids": ["service"]}}
            for name in ("auto.md", "mech.go", "manual.go")
        ]
        sources = {"auto.md": "auto", "mech.go": "manual_required", "manual.go": "manual_required"}

        def suggested(entry: dict) -> dict:
            return {"path": entry["path"], "categories": ["protocol_adapter"], "rationale": "x" * 20,
                    "required_actions": ["a"], "official_client_identity_changed": False,
                    "evidence_semantics_changed": False, "decision_source": sources[entry["path"]]}

        plan = SimpleNamespace()
        with mock.patch.object(workflow, "_load_impact_matrix", return_value={"file_changes": entries, "surface_deltas": [{"delta_id": "d"}]}), \
                mock.patch.object(workflow, "_load_source_candidate", return_value={"source_commit": "c", "source_tree": "t"}), \
                mock.patch.object(workflow, "mechanical_merge_facts", return_value={"mech.go": "upstream_only", "auto.md": "upstream_only"}), \
                mock.patch.object(workflow, "_suggested_change_decision_item", side_effect=suggested), \
                mock.patch.object(workflow, "latest_stage_path", return_value=Path("/dev/null")), \
                mock.patch.object(workflow, "sha256_file", return_value="0" * 64), \
                mock.patch.object(workflow, "_stage_document", side_effect=lambda _plan, _schema, body: body):
            document = workflow.generate_change_decision_suggestion(plan, None)
        by_path = {item["path"]: item["decision_source"] for item in document["files"]}
        # 自动分类不被机械决定覆盖；只有待人工的条目才可能改成机械决定。
        self.assertEqual(by_path, {"auto.md": "auto", "mech.go": "mechanical", "manual.go": "manual_required"})
        self.assertEqual(document["auto_accepted_count"], 1)
        self.assertEqual(document["unresolved_paths"], ["manual.go"])
        self.assertEqual(document["manual_required_count"], 2)
        self.assertEqual(document["result"], "ready_for_review")


if __name__ == "__main__":
    unittest.main()
