"""客户端适配接口（Claude 升级控制面设计 B1-01）：注册表、归属判定、绑定同源与恢复接口转调。

覆盖：
* 注册：已知客户端返回同一适配器实例；未知、空值、非字符串一律拒绝，不默认成 codex。
* 归属：``client_of`` 读总计划顶层 ``client``；只有历史 Codex 总计划（``codex-upgrade-campaign/v*``）允许缺失并
  隐含 codex；身份根之间的 ``client`` 缺失或不一致即拒绝。
* 绑定同源：Codex 的 ``orchestrator`` 就是按包路径导入的 ``codex_upgrade`` 模块；服务面每个成员在编排器上存在、
  函数签名与冻结清单逐字相同；适配器成员与按 ``ADAPTER_BINDINGS`` 现取的对象是同一个对象；对编排器或原模块的
  ``mock.patch`` 经适配器照常生效（现有 Codex 测试因此零改动）。
* Claude 绑定表覆盖全部服务面与适配器成员；门面只暴露服务面名字。
* 恢复接口：用旧候选坐标构造 ``RecoveryContext`` 调三个恢复接口，结果与直接调原函数逐字节相同。
* 共享代码对 ``orchestrator`` 只做字面访问 ``<表达式>.orchestrator.<服务面名>``。
"""

from __future__ import annotations

import ast
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.official_client_capture import codex_upgrade
from tools.official_client_capture import upgrade_client_adapter as uca
from tools.official_client_capture.tests.orchestrator_surface_inventory import signature_text

TOOL_ROOT = Path(__file__).resolve().parents[1]
OLD_CANDIDATE = uca.CandidateCoordinates(
    candidate_id="c0154-candidate-v7",
    runtime_image="sub2api:candidate-v7",
    profile_id="codex-0.154.0",
    profile_digest="d" * 64,
    build_id="build-7",
    deployed_version="0.2.4-2",
    candidate_image_id="sha256:" + "e" * 64,
    source_tree_sha256="f" * 64,
    candidate_purpose="production_replacement",
)


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _write_campaign(root: Path, payload: object) -> Path:
    campaign_dir = root / "campaign"
    campaign_dir.mkdir(parents=True, exist_ok=True)
    (campaign_dir / uca.CAMPAIGN_MANIFEST_FILENAME).write_text(json.dumps(payload), encoding="utf-8")
    return campaign_dir


class RegistryTests(unittest.TestCase):
    def test_known_clients_return_cached_adapters(self) -> None:
        for client in uca.CLIENTS:
            with self.subTest(client=client):
                adapter = uca.adapter_for(client)
                self.assertIsInstance(adapter, uca.ClientAdapter)
                self.assertEqual(adapter.client, client)
                self.assertIs(uca.adapter_for(client), adapter)

    def test_unknown_clients_are_rejected(self) -> None:
        for client in ("", "gemini", "Codex", None, 1):
            with self.subTest(client=client), self.assertRaises(uca.ClientAdapterError):
                uca.adapter_for(client)  # type: ignore[arg-type]

    def test_adapter_is_read_only_and_routes_orchestrator_symbols(self) -> None:
        adapter = uca.adapter_for("codex")
        with self.assertRaises(AttributeError):
            adapter.client = "claude"  # type: ignore[misc]
        with self.assertRaisesRegex(AttributeError, "adapter.orchestrator"):
            adapter._failed_job_ids  # noqa: B018  服务面符号不能绕过 orchestrator 直接取


class ClientOfTests(unittest.TestCase):
    def test_explicit_client_and_legacy_codex_manifests(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = {
                "codex-upgrade-campaign/v3": "codex",
                "codex-upgrade-campaign/v2": "codex",
                "codex-upgrade-campaign/v1": "codex",
            }
            for schema, expected in cases.items():
                with self.subTest(schema=schema):
                    campaign_dir = _write_campaign(root / schema.replace("/", "_"), {"schema_version": schema})
                    self.assertEqual(uca.client_of(campaign_dir), expected)
            claude = _write_campaign(root / "claude", {"schema_version": "official-client-upgrade-campaign/v1", "client": "claude"})
            self.assertEqual(uca.client_of(claude), "claude")
            codex = _write_campaign(root / "codex-explicit", {"schema_version": "codex-upgrade-campaign/v3", "client": "codex"})
            self.assertEqual(uca.client_of(codex), "codex")

    def test_missing_or_invalid_client_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, payload in {
                "新合同缺 client": {"schema_version": "official-client-upgrade-campaign/v1"},
                "无 schema 缺 client": {"campaign_id": "x"},
                "未知 client": {"schema_version": "codex-upgrade-campaign/v3", "client": "gemini"},
                "client 非字符串": {"schema_version": "official-client-upgrade-campaign/v1", "client": ["claude"]},
                "非对象": ["codex"],
            }.items():
                with self.subTest(label=label), self.assertRaises(uca.ClientAdapterError):
                    uca.client_of(_write_campaign(root / label, payload))
            with self.assertRaises(uca.ClientAdapterError):
                uca.client_of(root / "absent")
            target = _write_campaign(root / "real", {"schema_version": "codex-upgrade-campaign/v3"})
            linked = root / "linked"
            linked.mkdir()
            (linked / uca.CAMPAIGN_MANIFEST_FILENAME).symlink_to(target / uca.CAMPAIGN_MANIFEST_FILENAME)
            with self.assertRaises(uca.ClientAdapterError):
                uca.client_of(linked)

    def test_identity_roots_must_agree(self) -> None:
        roots = {
            "Campaign 总计划": {"client": "claude"},
            "Store Campaign": {"client": "claude"},
            "项目总账 plan": {"client": "claude"},
            "计时账本 plan": {"client": "claude"},
        }
        uca.require_consistent_client("claude", roots)
        for name in roots:
            for label, value in (("缺失", None), ("缺字段", {}), ("不一致", {"client": "codex"})):
                with self.subTest(root=name, case=label), self.assertRaises(uca.ClientAdapterError):
                    uca.require_consistent_client("claude", {**roots, name: value})
        with self.assertRaises(uca.ClientAdapterError):
            uca.require_consistent_client("gemini", roots)


class CodexBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.adapter = uca.adapter_for("codex")

    def test_orchestrator_is_the_package_module(self) -> None:
        self.assertIs(self.adapter.orchestrator, codex_upgrade)

    def test_surface_members_exist_with_frozen_signatures(self) -> None:
        tree = ast.parse((TOOL_ROOT / "codex_upgrade.py").read_text(encoding="utf-8"))
        definitions = {
            node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        for name, signature in uca.ORCHESTRATOR_SURFACE.items():
            with self.subTest(member=name):
                self.assertTrue(hasattr(codex_upgrade, name), f"编排器缺少服务面成员：{name}")
                if signature is not None:
                    self.assertIn(name, definitions, f"服务面函数不是编排器顶层定义：{name}")
                    self.assertEqual(signature_text(definitions[name]), signature, f"服务面函数签名漂移：{name}")

    def test_surface_counts_match_design(self) -> None:
        signatures = list(uca.ORCHESTRATOR_SURFACE.values())
        self.assertEqual(len(signatures), 254)
        self.assertEqual(sum(1 for value in signatures if value is not None), 209)
        self.assertEqual(
            sorted(name for name in uca.ORCHESTRATOR_SURFACE if not hasattr(codex_upgrade, name) or name.startswith("__")),
            ["__file__"],
        )

    def test_every_binding_resolves_to_the_same_object(self) -> None:
        bindings = uca.ADAPTER_BINDINGS["codex"]
        self.assertEqual(set(bindings), set(uca.ORCHESTRATOR_SURFACE) | set(uca.ADAPTER_MEMBERS))
        for name in uca.ORCHESTRATOR_SURFACE:
            with self.subTest(member=name):
                self.assertEqual(bindings[name], ("codex_upgrade", name))
                self.assertIs(getattr(self.adapter.orchestrator, name), getattr(codex_upgrade, name))
        for member in uca.ADAPTER_MEMBERS:
            with self.subTest(member=member):
                self.assertIs(getattr(self.adapter, member), uca._resolve(bindings[member]))

    def test_patches_on_original_modules_are_visible_through_adapter(self) -> None:
        sentinel = object()
        with mock.patch.object(codex_upgrade, "_failed_job_ids", sentinel):
            self.assertIs(self.adapter.orchestrator._failed_job_ids, sentinel)
        self.assertIsNot(self.adapter.orchestrator._failed_job_ids, sentinel)


class CodexMemberValueTests(unittest.TestCase):
    """Codex 适配器成员取值逐字等于改造前共享模块写死或导入的值。"""

    def setUp(self) -> None:
        self.adapter = uca.adapter_for("codex")

    def test_values_equal_former_literals(self) -> None:
        from tools.official_client_capture import codex_upgrade_project_ledger, codex_upgrade_reconciler
        from tools.official_client_capture import codex_upgrade_root_cause, codex_upgrade_tool_identity_policy

        self.assertEqual(self.adapter.orchestrator_basenames, frozenset({"codex_upgrade.py", "codex-upgrade"}))
        self.assertEqual(self.adapter.forbidden_subcommands, frozenset({
            "successor", "recover-candidate-failed-jobs", "control-epoch", "evaluation-transition",
            "terminal-transition-preflight", "plan", "reuse-official-evidence", "harden-evidence-permissions",
            "reconcile-supervisor-run", "reconcile-attempt", "compile-vc-batch", "compile-vc-interrupted-recovery-batch",
            "compile-vc-interrupted-recovery-continuation", "continue-vc1-interruption", "finalize-vc1-deadline-orphan",
        }))
        self.assertEqual(self.adapter.evaluation_builder_basename, "build_rule_assertion_results.py")
        self.assertEqual(self.adapter.target_version_field, "codex_version")
        self.assertEqual(self.adapter.deploy_receipt_glob, "codex-*-supervisor-enable-*.json")
        self.assertEqual(self.adapter.root_cause_codes_path, codex_upgrade_root_cause.DEFAULT_CODES_PATH)
        self.assertEqual(codex_upgrade_root_cause.load_codes(self.adapter.root_cause_codes_path), codex_upgrade_root_cause.load_codes())
        self.assertIs(self.adapter.identity_policy, codex_upgrade_tool_identity_policy)
        self.assertIs(self.adapter.recovery_segment_summary, codex_upgrade_reconciler.recovery_segment_summary)
        self.assertIs(self.adapter.segment_reuse_proofs, codex_upgrade_reconciler.segment_reuse_proofs)
        self.assertIs(self.adapter.requires_project_ledger, codex_upgrade_project_ledger.requires_project_ledger)
        self.assertIsNone(self.adapter.reject_volatile_dimensions({"phase": "VC-1", "scenario": "claude-code-2.1.286"}))
        # B1-04 项目总账：目录名、新建总账的 plan 合同与候选就绪探针计量类别逐字等于改造前写死的值。
        self.assertEqual(self.adapter.project_ledger_dir_name, "upgrade-project-ledger")
        self.assertEqual(self.adapter.project_ledger_plan_schema, "upgrade-project-ledger-plan/v1")
        self.assertEqual(self.adapter.candidate_probe_accounting_category, "candidate_readiness_models_probe/v1")

    def test_member_patches_are_visible_through_adapter(self) -> None:
        from tools.official_client_capture import codex_upgrade_reconciler

        sentinel = object()
        with mock.patch.object(codex_upgrade_reconciler, "segment_reuse_proofs", sentinel):
            self.assertIs(self.adapter.segment_reuse_proofs, sentinel)


class ClaudeBindingTests(unittest.TestCase):
    def test_bindings_cover_surface_and_members(self) -> None:
        bindings = uca.ADAPTER_BINDINGS["claude"]
        self.assertEqual(set(bindings), set(uca.ORCHESTRATOR_SURFACE) | set(uca.ADAPTER_MEMBERS))
        for name, (module, symbol) in bindings.items():
            with self.subTest(member=name):
                self.assertNotEqual(module, "codex_upgrade", "Claude 绑定不得落到 Codex 编排器")
                if name in uca.ORCHESTRATOR_SURFACE:
                    self.assertTrue(module.startswith("claude_"), f"服务面成员必须由 Claude 编排器实现：{module}")
                    self.assertEqual(symbol, name)

    def test_claude_member_values(self) -> None:
        from tools.official_client_capture import claude_upgrade_client_adapter as claude

        adapter = uca.adapter_for("claude")
        self.assertEqual(adapter.orchestrator_basenames, frozenset({"claude_upgrade.py", "claude-upgrade"}))
        self.assertLess(uca.adapter_for("codex").forbidden_subcommands, adapter.forbidden_subcommands)
        self.assertEqual(adapter.root_cause_codes_path, claude.ROOT_CAUSE_CODES_PATH)
        self.assertTrue(adapter.requires_project_ledger("formal", "2.1.286"))
        self.assertFalse(adapter.requires_project_ledger("preflight_only", "2.1.286"))

    def test_facade_exposes_only_surface_names(self) -> None:
        facade = uca.OrchestratorFacade("claude")
        with self.assertRaisesRegex(AttributeError, "服务面"):
            facade.not_in_surface  # noqa: B018
        with self.assertRaises(AttributeError):
            facade.x = 1  # type: ignore[attr-defined]
        with self.assertRaises(uca.ClientAdapterError):
            uca.OrchestratorFacade("gemini")


class RecoveryInterfaceTests(unittest.TestCase):
    """旧候选坐标构造的 RecoveryContext 经适配器调用，与直接调原函数逐字节相同。"""

    def setUp(self) -> None:
        self.adapter = uca.adapter_for("codex")

    @staticmethod
    def _echo(name: str):
        def run(*args, **kwargs):
            return {"function": name, "args": [str(item) for item in args], "kwargs": {key: str(value) for key, value in kwargs.items()}}
        return run

    def test_reuse_check_and_evolution_impact_forward_source_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            campaign_dir = Path(directory) / "campaign"
            manifest = {"campaign_id": "c-old", "configuration": {}}
            context = uca.RecoveryContext(
                campaign_dir=campaign_dir, manifest=manifest, phase="candidate",
                attempt_root=campaign_dir / "candidates" / OLD_CANDIDATE.candidate_id / "attempts" / "a7",
                source_attempt_id="a7", candidate=OLD_CANDIDATE,
            )
            with mock.patch.object(codex_upgrade, "recovery_reuse_check", side_effect=self._echo("reuse")):
                direct = codex_upgrade.recovery_reuse_check(
                    campaign_dir, phase="candidate", candidate_id=OLD_CANDIDATE.candidate_id,
                    source_attempt_id="a7", reuse_job_ids=["j1"], execute_job_ids=["j2"],
                )
                adapted = self.adapter.recovery_reuse_check(context, reuse_job_ids=["j1"], execute_job_ids=["j2"])
            self.assertEqual(_canonical(adapted), _canonical(direct))
            with mock.patch.object(codex_upgrade, "_attempt_evolution_impact", side_effect=self._echo("impact")):
                direct = codex_upgrade._attempt_evolution_impact(
                    campaign_dir, manifest, context.attempt_root, phase="candidate", candidate_id=OLD_CANDIDATE.candidate_id,
                )
                adapted = self.adapter.attempt_evolution_impact(context)
            self.assertEqual(_canonical(adapted), _canonical(direct))

    def test_preview_continuity_matches_direct_call(self) -> None:
        kinds = codex_upgrade.CONTINUITY_PROBE_KINDS
        snapshot = {kind: kind * 4 for kind in kinds}

        def probe_manifest(digests: dict[str, str]) -> dict:
            return {"phase": "after", "snapshots": [{"kind": kind, "sha256": sha} for kind, sha in digests.items()]}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            campaign_dir = root / "campaign"
            after = campaign_dir / "official" / "attempts" / "a1" / "evidence" / "environment" / "after" / "probe-manifest.json"
            after.parent.mkdir(parents=True)
            after.write_text(json.dumps(probe_manifest(snapshot)), encoding="utf-8")
            manifest = {"campaign_id": "c", "configuration": {}}

            def run(_manifest, output_dir: Path, phase: str) -> dict:
                payload = probe_manifest(dict(snapshot, containers="x" * 4))
                (output_dir / "probe-manifest.json").write_text(json.dumps(payload), encoding="utf-8")
                return payload

            # 两次调用用同一个探针输出目录（每次先清空），结果里的路径与摘要因此可以逐字节比较。
            output = root / "probes" / "p"
            results = []
            for call in (
                lambda: codex_upgrade.recovery_preview_continuity(
                    campaign_dir, manifest=manifest, phase="official", candidate_id=None,
                    source_attempt_id="a1", output_dir=output,
                ),
                lambda: self.adapter.recovery_preview_continuity(uca.RecoveryContext(
                    campaign_dir=campaign_dir, manifest=manifest, phase="official",
                    attempt_root=campaign_dir / "official" / "attempts" / "a1", source_attempt_id="a1",
                    candidate=uca.CandidateCoordinates(), output_dir=output,
                )),
            ):
                shutil.rmtree(output, ignore_errors=True)
                with mock.patch.object(codex_upgrade, "_probe_capture_environment", side_effect=run):
                    results.append(_canonical(call()))
            self.assertEqual(results[0], results[1])
            self.assertIn('"drifted"', results[0])

    def test_preview_continuity_requires_output_dir(self) -> None:
        context = uca.RecoveryContext(
            campaign_dir=Path("/nonexistent"), manifest={}, phase="official", attempt_root=Path("/nonexistent"),
            source_attempt_id="a1", candidate=uca.CandidateCoordinates(),
        )
        with self.assertRaisesRegex(uca.ClientAdapterError, "output_dir"):
            self.adapter.recovery_preview_continuity(context)


class LiteralOrchestratorAccessTests(unittest.TestCase):
    """共享代码对 orchestrator 只做字面访问：``<表达式>.orchestrator.<服务面名>``，不赋值、不传递、不 getattr。"""

    def test_managed_tree_uses_orchestrator_literally(self) -> None:
        violations: list[str] = []
        for path in sorted(TOOL_ROOT.rglob("*.py")):
            relative = path.relative_to(TOOL_ROOT)
            if "tests" in relative.parts or "__pycache__" in relative.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            parents = {id(child): node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Attribute) and node.attr == "orchestrator"):
                    continue
                parent = parents.get(id(node))
                if not (isinstance(parent, ast.Attribute) and parent.value is node):
                    violations.append(f"{relative}:{node.lineno} orchestrator 被整体使用")
                elif parent.attr not in uca.ORCHESTRATOR_SURFACE:
                    violations.append(f"{relative}:{node.lineno} {parent.attr} 不在编排器服务面")
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
