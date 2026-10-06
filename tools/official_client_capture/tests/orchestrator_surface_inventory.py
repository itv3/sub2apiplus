"""编排器服务面清单生成器（Claude 升级控制面设计 B1-01）。

从 B1-01 开工前的基线提交提取「共享模块与可迁入共享流程」对 ``codex_upgrade`` 的引用，生成
``upgrade_client_adapter.ORCHESTRATOR_SURFACE``：符号名 → 签名文本（类为 ``class``，常量与导入符号为 None）。

口径（与设计第 2 节 B1-01、附录 E 一致）：
* 可迁入共享流程：附录 E 第 3 节的 119 个函数（``MIGRATABLE_FUNCTIONS``）。它们引用的、不在可迁集合内的编排器
  函数全部进入服务面；引用的常量若还被其它编排器函数或常量使用，或落在任一身份闭包内，则是共用常量、进入服务面，
  其余随迁（不进服务面）。
* 共享模块：引用 ``codex_upgrade`` 的 control 层模块（``SHARED_MODULES``）。对账器只计评估读侧闭包之外的函数
  （闭包内的 7 个函数原位不动、继续直接引用 ``codex_upgrade``）。
* 导入符号只取编排器里非标准库、非受管模块的三个：``ConfigurationError``、``file_sha256``、``__file__``；
  其余导入名（标准库与受管模块）由共享模块自己导入。

迁出（B1-09）会改写编排器源码，所以生成器固定读取基线提交的工具树（``git archive``），任何时候都能复算。
用法：``python3 -m tools.official_client_capture.tests.orchestrator_surface_inventory [--commit SHA] [--check]``。
"""

from __future__ import annotations

import argparse
import ast
import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any

from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TOOL_RELATIVE = "tools/official_client_capture"
BASELINE_COMMIT = "17c87351a1f30dd5946986017553382619226833"
ORCHESTRATOR_MODULE = "codex_upgrade"
IMPORTED_SURFACE = ("ConfigurationError", "__file__", "file_sha256")
SHARED_MODULES = (
    "certify_release",
    "codex_upgrade_campaign_run_rehearsal_receipt",
    "codex_upgrade_job_rehearsal_receipt",
    "codex_upgrade_policy_certification",
    "codex_upgrade_pre_a3_certification",
    "codex_upgrade_project_ledger",
    "codex_upgrade_reconciler",
    "codex_upgrade_supervisor",
    "codex_upgrade_vc0_closeout",
    "codex_upgrade_wire_transition",
)
ENTRY_MODULES = (
    "codex_upgrade_campaign_run_rehearsal_receipt",
    "codex_upgrade_job_rehearsal_receipt",
    "codex_upgrade_vc0_closeout",
)
# 附录 E 第 3 节：不在任何身份闭包内、可迁入 upgrade_control_plane.py 的 119 个函数。
MIGRATABLE_FUNCTIONS = (
    "_mutable_command_coordinates", "_main_command_lease", "_require_capture_budget_before_data_action",
    "_wire_closure_sha256", "_tool_path_job_map", "_implicit_r1_candidate_id", "_campaign_batch_model",
    "_campaign_uses_staging_model", "_staging_sequence_dir", "_staging_commit_path", "_staging_attempt_dirs",
    "_next_staging_attempt", "_read_staging_commit", "_committed_vc_sequences", "_evaluation_action_kind",
    "_freeze_evaluation_output_bindings", "_require_stage_replay_proof_for_checkpoint",
    "_candidate_stage_replay_action_facts", "_campaign_stage_replay_facts", "_campaign_vc_plan", "compile_vc_batch",
    "_write_staging_batch_artifacts", "_timing_ledger_batch_events", "_append_timing_ledger_batch_events",
    "_complete_timing_ledger_phase_after_batch", "_bootstrap_noop_first_batch", "_prepare_atomic_batch_governance",
    "_empty_batch_governance", "compile_and_run_vc_batch", "_load_prepared_staging_attempt", "_publish_staging_file",
    "_write_staging_abort", "_staging_abort_accounted", "_reconcile_staging_abort_receipt",
    "_abort_staging_attempt_without_parent", "_supervisor_run_reconciled", "_archive_uncommitted_formal_artifacts",
    "_formal_artifact_attempt", "_reconcile_prepared_parent_run", "_reconcile_staging_orphans",
    "_compile_and_run_vc_batch_staging", "_assert_revision_admission", "_ledger_last_event_of_type",
    "_write_candidate_revision_pending", "open_candidate_revision", "_candidate_identity_snapshot",
    "_candidate_accounting_checks", "_invalidation_phase", "invalidate_candidate", "_locate_evaluation_failed_run",
    "_derive_b0_evaluation_run", "_evaluation_failure_scope", "_evaluation_failure_diagnosis_facts",
    "_verify_current_deployment_receipt", "_evaluation_defect_admission", "_next_recovery_revision",
    "_transient_environment_admission", "_evaluation_epoch_for_recovery", "_verify_failed_run_reconciled",
    "evaluation_recover", "_authorize_and_commit_non_failure_baseline", "_evaluation_approval_revision_locked",
    "_evaluation_reevaluate_locked", "_evaluation_recover_locked", "_publish_ledger_receipt_copies",
    "_project_ledger_required", "_campaign_plan_deadline", "_deadline_extend_command",
    "_request_budget_extend_command", "_recovery_preview_retry_root_cause_ids", "_assert_project_ledger_consumer",
    "_b0_evaluator_authorized_digest_history", "_evidence_root_registrants", "_supersede_occupied_evidence_roots",
    "_evidence_root_conflicts", "_incremental_noop_job_ids", "_evidence_conflict_quarantine_preview",
    "_evidence_conflict_quarantine_command", "recovery_reuse_check", "recovery_preview_continuity",
    "_recovery_root_suffix", "_job_recovery_root_keys", "_relocate_job_for_recovery", "_reserve_attempt_recovery",
    "_backfill_original_attempt_ledger_events", "_append_attempt_recovery_ledger_event",
    "_write_attempt_recovery_summary", "_seal_candidate_revision", "_reject_canonical_legacy_write",
    "_reject_campaign_run_legacy_write", "_reject_unparented_formal_write", "_tool_evolution_unregistered_drift",
    "_refuse_seal_chain_on_evolution_invalidated_attempt", "_refuse_vc1_seal_batch_against_bundle_state",
    "_require_tool_evolution_registered", "_tool_evolution_candidate_jobs", "_candidate_b0_evaluation_outputs",
    "_tool_evolution_quiescence_problems", "_policy_only_change_paths", "_policy_evolution_transition",
    "_tool_evolution_preview", "_evaluator_entry_digests", "_tool_evolution_command", "_tool_evolution_status_command",
    "_campaign_resume_preview", "_campaign_resume_command", "_apply_campaign_resume", "_accounting_resolve_preview",
    "_accounting_resolve_command", "_apply_accounting_resolution", "_optional_json_object",
    "_environment_isolate_preview", "_environment_isolate_command", "_evaluation_epoch_command",
    "_reconcile_supervisor_run_command", "_rehearse_candidate_seal_command", "_reconcile_attempt_command",
    "_account_sealed_official_command", "_account_sealed_candidate_command",
)


def _names(node: ast.AST) -> set[str]:
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)}


def signature_text(node: ast.AST) -> str:
    """函数签名文本（参数表与返回注解原文）；类记为 ``class``。"""

    if isinstance(node, ast.ClassDef):
        return "class"
    assert isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    text = f"({ast.unparse(node.args)})"
    if node.returns is not None:
        text += f" -> {ast.unparse(node.returns)}"
    return ("async " + text) if isinstance(node, ast.AsyncFunctionDef) else text


def _orchestrator_aliases(scope: ast.AST) -> tuple[set[str], set[str]]:
    """作用域内导入 ``codex_upgrade`` 的别名，以及 ``from …codex_upgrade import x`` 直接导入的符号。"""

    aliases: set[str] = set()
    direct: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.rsplit(".", 1)[-1] == ORCHESTRATOR_MODULE:
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == ORCHESTRATOR_MODULE and (node.level or module in ("", "tools.official_client_capture")):
                    aliases.add(alias.asname or alias.name)
                elif module.rsplit(".", 1)[-1] == ORCHESTRATOR_MODULE:
                    direct.add(alias.name)
    return aliases, direct


def shared_module_references(tool_root: Path, module: str) -> dict[str, set[str]]:
    """共享模块里逐函数引用的编排器符号（``别名.属性`` 与直接导入）。"""

    tree = ast.parse((tool_root / f"{module}.py").read_text(encoding="utf-8"))
    functions, _constants, _imports = tip._module_symbols(tree)
    _unused, top_direct = _orchestrator_aliases(ast.Module(body=tip._top_level_import_nodes(tree), type_ignores=[]))
    # 别名按整个模块收集：项目总账的 ``_deadline_context`` 在函数内延迟导入编排器、再把模块对象返回给调用方，
    # 调用方函数里没有导入语句，却以同一别名访问编排器符号（B1-04 把这种传递改成字面的 adapter.orchestrator）。
    top_aliases, _unused_direct = _orchestrator_aliases(tree)
    found: dict[str, set[str]] = {}
    for name, node in functions.items():
        aliases, direct = _orchestrator_aliases(node)
        aliases |= top_aliases
        symbols = set(direct) | {name_ for name_ in _names(node) if name_ in top_direct}
        for child in ast.walk(node):
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id in aliases:
                symbols.add(child.attr)
        if symbols:
            found[name] = symbols
    return found


def compute_surface(tool_root: Path) -> dict[str, Any]:
    policy = tip.load_policy(tool_root / tip.POLICY_FILENAME)
    digests = tip._managed_tree_digests(tool_root)
    tree = ast.parse((tool_root / f"{ORCHESTRATOR_MODULE}.py").read_text(encoding="utf-8"))
    functions, constants, _imports = tip._module_symbols(tree)
    migratable = set(MIGRATABLE_FUNCTIONS)
    missing = sorted(migratable - set(functions))
    if missing or len(migratable) != len(MIGRATABLE_FUNCTIONS):
        raise SystemExit(f"可迁入函数名单与编排器不符：{missing}")
    wire = tip.orchestrator_closure(policy, tool_root, list(policy["orchestrator"]["wire_roots"]), digests, layer="wire_producer")
    evidence = tip.orchestrator_closure(policy, tool_root, list(policy["orchestrator"]["evidence_roots"]), digests, layer="evidence_semantics")
    reader: dict[str, set[str]] = {}
    for roots in (tip.EVALUATOR_COMPARE_READER_ROOTS, tip.EVALUATOR_ACCEPT_READER_ROOTS):
        for item in tip.evaluator_reader_closure(policy, tool_root, list(roots), digests)["symbols"]:
            reader.setdefault(item["module"], set()).add(item["name"])
    closure = {item["name"] for item in wire["functions"] + wire["constants"] + evidence["functions"] + evidence["constants"]}
    closure |= reader.get(ORCHESTRATOR_MODULE, set())

    migrated_functions: set[str] = set()
    migrated_constants: set[str] = set()
    for name in MIGRATABLE_FUNCTIONS:
        for referenced in _names(functions[name]):
            if referenced in functions and referenced not in migratable:
                migrated_functions.add(referenced)
            elif referenced in constants:
                migrated_constants.add(referenced)
    users: dict[str, set[str]] = {}
    for owner, node in [*functions.items(), *constants.items()]:
        for referenced in _names(node):
            if referenced in constants and referenced != owner:
                users.setdefault(referenced, set()).add(owner)
    shared_constants = {
        name for name in migrated_constants if name in closure or users.get(name, set()) - migratable
    }
    moving_constants = migrated_constants - shared_constants

    by_module: dict[str, set[str]] = {}
    for module in SHARED_MODULES:
        references = shared_module_references(tool_root, module)
        if module == "codex_upgrade_reconciler":
            references = {name: refs for name, refs in references.items() if name not in reader.get(module, set())}
        by_module[module] = set().union(*references.values()) if references else set()

    referenced = migrated_functions | shared_constants | set().union(*by_module.values())
    surface: dict[str, str | None] = {}
    for name in sorted(referenced | set(IMPORTED_SURFACE)):
        if name in functions:
            surface[name] = signature_text(functions[name])
        elif name in constants or name in IMPORTED_SURFACE:
            surface[name] = None
    non_entry = migrated_functions | shared_constants
    for module in SHARED_MODULES:
        if module not in ENTRY_MODULES:
            non_entry |= by_module[module]
    return {
        "surface": surface,
        "functions": sorted(name for name in surface if name in functions),
        "constants": sorted(name for name in surface if name in constants),
        "imported": sorted(name for name in surface if name not in functions and name not in constants),
        "entry_only": sorted(set(surface) - non_entry - set(IMPORTED_SURFACE)),
        "closure_functions": sorted(name for name in surface if name in functions and name in closure),
        "closure_constants": sorted(name for name in surface if name in constants and name in closure),
        "moving_constants": sorted(moving_constants),
        "migrated_dependencies": {"functions": len(migrated_functions), "shared_constants": len(shared_constants)},
    }


def baseline_tool_tree(commit: str, destination: Path) -> Path:
    """把基线提交的受管工具树解到临时目录（只取 ``tools/official_client_capture``）。"""

    archive = subprocess.run(
        ["git", "archive", "--format=tar", commit, TOOL_RELATIVE],
        cwd=REPOSITORY_ROOT, check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(destination, filter="data")
    return destination / TOOL_RELATIVE


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--commit", default=BASELINE_COMMIT, help="基线提交（默认 B1-01 开工前的 main）")
    parser.add_argument("--check", action="store_true", help="与 upgrade_client_adapter.ORCHESTRATOR_SURFACE 比对")
    args = parser.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="orchestrator-surface-") as scratch:
        result = compute_surface(baseline_tool_tree(args.commit, Path(scratch)))
    summary = {key: (len(value) if isinstance(value, (list, dict)) and key != "migrated_dependencies" else value)
               for key, value in result.items() if key != "surface"}
    print(summary, file=sys.stderr)
    if args.check:
        from tools.official_client_capture import upgrade_client_adapter

        if dict(upgrade_client_adapter.ORCHESTRATOR_SURFACE) != result["surface"]:
            print("ORCHESTRATOR_SURFACE 与基线源码复算不一致", file=sys.stderr)
            return 1
        return 0
    for name, signature in result["surface"].items():
        print(f"    {name!r}: {signature!r},")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
