"""新评估基线的 compare 来源合同回归；执行产品源码分支，不复制它的判定实现。

共享恢复函数只截取 COMMIT 创建／重放边界，替换外部阶段来源，真实适配值与 COMMIT 校验、构造、落盘仍执行产品代码。
另执行 Claude compare／accept 的基线字段与 builder 路径表达式，核当前基线形成闭环，旧基线收据确实被消费者拒绝。
这些是局部合同回归；不代替完整 Campaign、ARM64 真机或认证链验收。
"""

from __future__ import annotations

import ast
import copy
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_acceptance as acceptance
from tools.official_client_capture import claude_upgrade_baseline as claude_baseline
from tools.official_client_capture import claude_upgrade_compare as compare
from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import upgrade_client_adapter as adapters
from tools.official_client_capture import upgrade_control_plane as control

TOOL_ROOT = Path(__file__).resolve().parents[1]
CANDIDATE = "claude-candidate-r1"


def _function(name: str, path: Path) -> ast.FunctionDef:
    """每次从待验产品源码读取函数体；函数消失或重构到别处时测试明确失败。"""

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _compile(nodes: list[ast.stmt], path: Path) -> Any:
    return compile(ast.fix_missing_locations(ast.Module(body=copy.deepcopy(nodes), type_ignores=[])), str(path), "exec")


def _assignment(function: ast.FunctionDef, name: str) -> ast.Assign | ast.AnnAssign:
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(ast.unparse(target) == name for target in node.targets):
            return node
        if isinstance(node, ast.AnnAssign) and ast.unparse(node.target) == name:
            return node
    raise AssertionError(f"产品函数中未找到 {name} 赋值")


def _selected_fields(node: ast.Assign | ast.AnnAssign, fields: set[str]) -> ast.stmt:
    """保留待核合同的原始值表达式；省去无关文件绑定，避免镜像实现或打桩成功判据。"""

    selected = copy.deepcopy(node)
    assert isinstance(selected.value, ast.Dict)
    pairs = [(key, value) for key, value in zip(selected.value.keys, selected.value.values)
             if isinstance(key, ast.Constant) and key.value in fields]
    if {key.value for key, _value in pairs} != fields:
        raise AssertionError("产品合同字段与定向回归不一致")
    selected.value = ast.Dict(keys=[key for key, _value in pairs], values=[value for _key, value in pairs])
    return selected


class ComparisonBaselineContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        control_path = Path(control.__file__)
        function = _function("_evaluation_recover_locked", control_path)
        commits = [node for node in ast.walk(function)
                   if isinstance(node, ast.If) and ast.unparse(node.test) == "commit_path.is_file()"]
        if len(commits) != 1:
            raise AssertionError("共享恢复函数 COMMIT 边界不是唯一分支")
        cls.commit_code = _compile([_assignment(function, "commit_path"), commits[0]], control_path)
        compare_path = Path(compare.__file__)
        compare_function = _function("_compare", compare_path)
        cls.report_code = _compile([_selected_fields(_assignment(compare_function, "report"), {
            "candidate_revision", "evaluation_baseline", "baseline_commit_sha256", "rules", "identity",
        })], compare_path)
        cls.builder_code = _compile([_selected_fields(_assignment(compare_function, "report['builder']"), {
            "script", "output", "results_dir", "evaluation_baseline",
        })], compare_path)
        accept_path = Path(acceptance.__file__)
        accept_function = _function("_accept", accept_path)
        refusals = [node for node in accept_function.body if isinstance(node, ast.If) and ast.unparse(node.test) == "drift"]
        cls.accept_code = _compile([_assignment(accept_function, "expectations"),
            _assignment(accept_function, "drift"), *refusals], accept_path)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.campaign = Path(temporary.name) / "campaign"
        self.directory = self.campaign / "candidates" / CANDIDATE / "revisions" / "b1"
        self.directory.mkdir(parents=True, mode=0o700)

    def execute_commit(self, client: str, *, changed: list[str] | None = None,
                       status: str = "complete", transient: bool = False, refuse_sources: bool = False) -> dict[str, Any]:
        engine = codex_upgrade if client == "codex" else claude_upgrade
        adapter = adapters.adapter_for(client)
        sources = {
            "capture-candidate": {"status": "complete", "baseline_of_record": 0,
                "path": self.campaign / "candidates" / CANDIDATE / "result.json", "sha256": "a" * 64},
            "compare": {"status": status, "baseline_of_record": 0,
                "path": self.campaign / "comparisons" / CANDIDATE / "result.json", "sha256": "b" * 64},
        }

        def read_source(_campaign: Path, _candidate: str, baseline: int, stage: str) -> dict[str, Any]:
            self.assertFalse(refuse_sources, "已有 COMMIT 不得重读新来源后追改合同")
            self.assertEqual((_campaign, _candidate, baseline), (self.campaign, CANDIDATE, 0))
            return sources[stage]

        context = {
            "adapter": adapter, "baseline_dir": self.directory, "campaign_dir": self.campaign,
            "candidate_id": CANDIDATE, "current_baseline": 0, "number": 1, "revision": 1,
            "manifest": {"campaign_id": "comparison-baseline-fixture"},
            "transient": {} if transient else None, "defect": {"changed_items": changed or ["checker_sha256"]},
            "baseline_kind": "attempt-recovery" if transient else "evaluator-only",
            "recovery": {"recovery_sha256": "c" * 64}, "authorization": {"authorization_sha256": "d" * 64},
            "codex_upgrade_vc_artifacts": artifacts, "Path": Path, "Any": Any,
        }
        with mock.patch.object(engine, "_stage_read_source", side_effect=read_source) as read:
            exec(self.commit_code, context)
        self.assertEqual(read.call_count, 0 if refuse_sources else 2)
        return artifacts.validate_evaluation_baseline_commit(context["commit"])

    def test_real_adapter_values_are_client_specific(self) -> None:
        self.assertIs(adapters.adapter_for("claude").comparison_is_baseline_bound, True)
        self.assertIs(adapters.adapter_for("codex").comparison_is_baseline_bound, False)

    def test_claude_checker_only_new_commit_recomputes_compare(self) -> None:
        commit = self.execute_commit("claude")
        self.assertEqual(commit["stage_sources"]["compare"], {
            "source": "local", "target": f"comparisons/{CANDIDATE}/revisions/b1/result.json",
        })
        self.assertEqual(commit["stage_sources"]["capture-candidate"]["source"], "reused")

    def test_codex_checker_only_preserves_completed_comparison_reuse(self) -> None:
        commit = self.execute_commit("codex")
        self.assertEqual(commit["stage_sources"]["compare"], {
            "source": "reused", "baseline": 0, "path": f"comparisons/{CANDIDATE}/result.json", "sha256": "b" * 64,
        })

    def test_codex_original_local_conditions_are_unchanged(self) -> None:
        for label, options in (("读侧变化", {"changed": ["compare_reader_sha256"]}),
                               ("比较未完成", {"status": "pending"}), ("临时环境", {"transient": True})):
            with self.subTest(condition=label):
                (self.directory / "COMMIT").unlink(missing_ok=True)
                commit = self.execute_commit("codex", **options)
                self.assertEqual(commit["stage_sources"]["compare"]["source"], "local")

    def test_existing_commit_is_replayed_without_rewriting_or_reselecting(self) -> None:
        original = self.execute_commit("codex")
        path = self.directory / "COMMIT"
        before = (path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns)
        for client in ("claude", "codex"):
            with self.subTest(client=client):
                replay = self.execute_commit(client, changed=["compare_reader_sha256"], refuse_sources=True)
                self.assertEqual(replay, original)
                self.assertEqual((path.read_bytes(), path.stat().st_ino, path.stat().st_mtime_ns), before)

    def test_new_claude_comparison_builder_and_accept_share_current_baseline(self) -> None:
        commit = self.execute_commit("claude")
        with mock.patch.object(claude_baseline, "read_evaluation_baseline_commit", return_value=commit):
            compare_target = claude_upgrade._stage_write_target(self.campaign, CANDIDATE, 1, "compare")
            assertions_dir = claude_upgrade._stage_write_target(self.campaign, CANDIDATE, 1, "assertions")
        self.assertEqual(compare_target, self.campaign / commit["stage_sources"]["compare"]["target"])
        context = {
            "current": {"revision": 1}, "baseline": 1, "commit": commit, "Mapping": Mapping,
            "rules": ["SPEC-HDR-001"], "identity": {"candidate_id": CANDIDATE},
            "campaign_dir": self.campaign, "assertions_dir": assertions_dir,
            "BUILDER_RELATIVE": compare.BUILDER_RELATIVE, "ConfigurationError": acceptance.ConfigurationError,
        }
        exec(self.report_code, context)
        exec(self.builder_code, context)
        self.assertEqual(context["report"]["builder"], {
            "script": compare.BUILDER_RELATIVE, "output": f"assertions/{CANDIDATE}/revisions/b1/results.json",
            "results_dir": f"assertions/{CANDIDATE}/revisions/b1/machine", "evaluation_baseline": 1,
        })
        context["comparison"] = context["report"]
        exec(self.accept_code, context)
        self.assertEqual(context["drift"], [])
        old = copy.deepcopy(context["report"])
        old.update(evaluation_baseline=0, baseline_commit_sha256=None)
        context["comparison"] = old
        with self.assertRaisesRegex(acceptance.ConfigurationError, "先在当前评估基线重新 compare"):
            exec(self.accept_code, context)


if __name__ == "__main__":
    unittest.main()
