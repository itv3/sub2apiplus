"""R16：逐作业显式工具依赖的完整性门禁。

场景清单里的 ``tool_dependencies`` 是审核过的权威，运行时不再递归扫描。本文件保证：

* 分析器剔除注释的规则正确（shell 词首 ``#``、heredoc 中的 Python、Python 注释与文档字符串）；
* 任何声明了依赖的场景清单，每个作业的声明都与分析器结果逐项相等（多登记、漏登记都失败）；
* 一份清单要么全部作业声明，要么全部不声明，避免半迁移后又走回旧算法。
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture.tests import job_dependency_analyzer as analyzer

TOOL_ROOT = Path(codex_upgrade.__file__).resolve().parent


class CommentStrippingTest(unittest.TestCase):
    def test_shell_comments_are_removed_outside_quotes_only(self) -> None:
        text = (
            "#!/usr/bin/env bash\n"
            "# 见 run_candidate_aux_capture.sh\n"
            "bash run_real.sh  # 旁注 run_note.sh\n"
            "echo '#not_comment.sh' \"# also.sh\"\n"
            "echo ${#items[@]} $# x#y.sh\n"
        )
        cleaned = analyzer.strip_shell_comments(text)
        self.assertNotIn("run_candidate_aux_capture.sh", cleaned)
        self.assertNotIn("run_note.sh", cleaned)
        for kept in ("run_real.sh", "#not_comment.sh", "# also.sh", "${#items[@]}", "$#", "x#y.sh"):
            self.assertIn(kept, cleaned)

    def test_python_heredoc_in_shell_is_stripped_as_python(self) -> None:
        text = (
            "python3 - \"$x\" <<'PY'\n"
            "# it's a comment naming ghost.py\n"
            "import tools.official_client_capture.real_module\n"
            "PY\n"
            "echo after.sh # trailing.sh\n"
        )
        cleaned = analyzer.strip_shell_comments(text)
        self.assertNotIn("ghost.py", cleaned)
        self.assertIn("tools.official_client_capture.real_module", cleaned)
        self.assertIn("after.sh", cleaned)
        self.assertNotIn("trailing.sh", cleaned)

    def test_python_comments_and_docstrings_are_removed(self) -> None:
        text = (
            '"""模块说明提到 doc_only.sh。"""\n'
            "import os  # 注释提到 comment_only.py\n"
            "def f():\n"
            '    """函数说明提到 fn_doc.sh。"""\n'
            '    return "literal_kept.sh"\n'
        )
        cleaned = analyzer.strip_python_comments(text)
        for removed in ("doc_only.sh", "comment_only.py", "fn_doc.sh"):
            self.assertNotIn(removed, cleaned)
        self.assertIn("literal_kept.sh", cleaned)


class AnalyzeStepsTest(unittest.TestCase):
    def test_transitive_dependencies_exclude_comment_and_docstring_references(self) -> None:
        files = {
            "a.sh": "# 注释提到 b.sh\nbash c.sh\n",
            "b.sh": "echo b\n",
            "c.sh": "python3 -m tools.official_client_capture.d\n",
            "d.py": '"""文档提到 e.py"""\nimport f\n# 注释提到 g.py\nX = "h.sh"\n',
            "e.py": "",
            "f.py": "",
            "g.py": "",
            "h.sh": "",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            index = {}
            for name, content in files.items():
                (root / name).write_text(content, encoding="utf-8")
                index[name] = hashlib.sha256(content.encode()).hexdigest()
            dependencies = analyzer.analyze_steps(
                [{"argv": ["bash", "{tool_root}/a.sh"], "environment": {}}],
                index,
                tool_root=root,
            )
        self.assertEqual(dependencies, ["a.sh", "c.sh", "d.py", "f.py", "h.sh"])

    def test_relay_job_no_longer_pulls_orchestrator_through_comments(self) -> None:
        """0.156.1 relay 作业：注释引用链（→ 辅助采集脚本 → 监督器 → 编排器）不再计入依赖。"""

        payload = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_156_1.json").read_text(encoding="utf-8")
        )
        job = next(
            item
            for item in payload["capture_jobs"]
            if item["id"] == "official-relay-ws-default"
        )
        dependencies = analyzer.analyze_steps(job["steps"], analyzer.current_tool_index())
        self.assertIn("run_official_relay_scenario.sh", dependencies)
        for excluded in (
            "run_candidate_aux_capture.sh",
            "codex_upgrade_supervisor.py",
            "codex_upgrade.py",
        ):
            self.assertNotIn(excluded, dependencies)


class DeclaredDependencyGateTest(unittest.TestCase):
    def test_declared_scenario_manifests_match_analysis(self) -> None:
        """声明了依赖的场景清单：全部作业都声明，且逐项等于分析结果。"""

        index = analyzer.current_tool_index()
        for manifest in sorted(TOOL_ROOT.glob("codex_upgrade_scenarios_*.json")):
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            jobs = payload.get("capture_jobs", [])
            declared = [job for job in jobs if "tool_dependencies" in job]
            if not declared:
                continue
            with self.subTest(manifest=manifest.name):
                self.assertEqual(len(declared), len(jobs), "声明必须覆盖全部作业")
                for job in jobs:
                    self.assertEqual(
                        job["tool_dependencies"],
                        analyzer.analyze_steps(job["steps"], index),
                        f"{manifest.name} 的 {job['id']} 声明与实际依赖不一致",
                    )

    def test_manifest_must_declare_all_jobs_or_none(self) -> None:
        payload = json.loads(
            (TOOL_ROOT / "codex_upgrade_scenarios_0_156_1.json").read_text(encoding="utf-8")
        )
        payload["capture_jobs"][0]["tool_dependencies"] = ["run_official_relay_scenario.sh"]
        with self.assertRaisesRegex(codex_upgrade.ConfigurationError, "全部作业"):
            codex_upgrade._validate_scenario_manifest_shape(payload)
        for job in payload["capture_jobs"]:
            job["tool_dependencies"] = ["run_official_relay_scenario.sh"]
        codex_upgrade._validate_scenario_manifest_shape(payload)


if __name__ == "__main__":
    unittest.main()
