"""官方客户端升级控制面的客户端适配接口（Claude 升级控制面设计 D4、B1-01、附录 A）。

共享控制面（监督器、对账器、项目总账、计时账本、认证与入口模块，以及迁入 ``upgrade_control_plane`` 的共享流程）
对编排器的全部依赖收进本模块定义的协议：

* ``ORCHESTRATOR_SURFACE``：共享代码可以经 ``adapter.orchestrator.<原名>`` 访问的编排器符号与签名文本（类记
  ``class``，常量与导入符号记 None）。清单由 ``tests/orchestrator_surface_inventory.py`` 从基线源码提取后冻结：
  161 个函数、42 个常量、3 个导入符号。
* ``ADAPTER_BINDINGS``：每个客户端的服务面成员与适配器成员（值、模块、可调用对象）各自落到哪个模块的哪个符号
  （符号为 None 表示模块本身）。适配器由这张表构造；v3 身份引擎按它解析 ``adapter.orchestrator.<名字>`` 与
  ``adapter.<成员>`` 的间接调用，所以运行时取到的实现与身份引擎解析到的实现同源。
* 注册表 ``adapter_for(client)``、Campaign 归属 ``client_of(campaign_dir)`` 与身份根之间的 ``client`` 一致性核对。

访问规则：共享代码一律写字面形式 ``adapter.orchestrator.<原名>``、``adapter.<成员>``；不得把 ``orchestrator``
对象赋给变量、作为参数传递或用 ``getattr`` 取成员，否则身份引擎无法解析，``plan`` 与认证失败。两客户端共用的
代码里不写 ``if client ==`` 分支，客户端差异全部由绑定表表达；按客户端分派只发生在本模块的注册表里。

解析时机：适配器成员每次访问都按绑定表现取模块属性，不在构造时缓存对象。Codex 的 ``orchestrator`` 就是按包路径
导入的 ``codex_upgrade`` 模块本身（脚本运行时它已把 ``__main__`` 登记为包路径别名，见其文件头），因此经适配器
取到的永远是模块的当前属性，现有测试对 ``codex_upgrade`` 或对账器符号的 ``mock.patch`` 照常生效，Codex 行为
与改造前逐字相同。Claude 的 ``orchestrator`` 是按服务面绑定现取 ``claude_upgrade`` 及其子模块同名符号的门面，
只暴露服务面内的名字。

模块实例：适配器一律按包路径解析模块。监督器以脚本方式运行时自己的兄弟模块是扁平名实例，与包路径实例
各带一份异常类；所以共享代码要捕获的异常一律经模块型成员取（``adapter.identity_policy``、``adapter.host_layout``、
``adapter.provenance``），被调函数与被捕获的异常类来自同一份模块；只取数据的成员（如根因码表路径）由调用方
用自己那份模块加载（``root_cause.load_codes(adapter.root_cause_codes_path)``），异常类与改造前完全相同。
"""

from __future__ import annotations

import importlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping

PACKAGE = "tools.official_client_capture"
CLIENTS = ("codex", "claude")
CAMPAIGN_MANIFEST_FILENAME = "campaign.json"
# 历史 Codex 总计划没有 client 字段：只有这一族 schema 隐含 codex，其余缺字段一律拒绝。
LEGACY_CODEX_CAMPAIGN_SCHEMA_PREFIX = "codex-upgrade-campaign/v"


class ClientAdapterError(ValueError):
    """客户端未知、绑定缺失，或身份根之间的 client 缺失、不一致。"""


@dataclass(frozen=True)
class CandidateCoordinates:
    """候选运行坐标：对应编排器 ``_campaign_jobs``／``_campaign_arguments`` 的关键字参数。

    恢复时取失败源 attempt／reservation 记录的值，不取当前候选的默认值。
    """

    candidate_id: str | None = None
    runtime_image: str | None = None
    profile_id: str | None = None
    profile_digest: str | None = None
    build_id: str | None = None
    deployed_version: str | None = None
    candidate_image_id: str | None = None
    source_tree_sha256: str | None = None
    candidate_purpose: str | None = None


@dataclass(frozen=True)
class RecoveryContext:
    """恢复接口的统一入参：失败源身份全部显式给出。"""

    campaign_dir: Path
    manifest: Mapping[str, Any]
    phase: str
    attempt_root: Path
    source_attempt_id: str
    candidate: CandidateCoordinates
    output_dir: Path | None = None  # recovery_preview_continuity 的探针输出目录


# 编排器服务面（B1-01 冻结）：共享代码经 adapter.orchestrator.<原名> 可访问的编排器符号 → 签名文本。
# 由 tests/orchestrator_surface_inventory.py 从基线提交 17c87351a 提取：161 个函数（113 个在 Codex 身份闭包内）、
# 42 个常量（35 个在闭包内）、3 个导入符号；Claude 编排器以同名同签名实现全部函数成员（B1-09 合同测试）。
ORCHESTRATOR_SURFACE: Mapping[str, str | None] = MappingProxyType({
    'APPROVAL_REVISION_FILENAME': None,
    'ATTEMPT_RECOVERY_DIRNAME': None,
    'ATTEMPT_RECOVERY_RESERVATION_FILENAME': None,
    'ATTEMPT_RECOVERY_RESERVATION_SCHEMA': None,
    'ATTEMPT_RECOVERY_SUCCESS_STATUS': None,
    'ATTEMPT_RECOVERY_SUMMARY_FILENAME': None,
    'ATTEMPT_RECOVERY_SUMMARY_SCHEMA': None,
    'CAMPAIGN_LEASE_FILENAME': None,
    'CANDIDATE_INVALIDATION_FILENAME': None,
    'CANDIDATE_VC_PHASES': None,
    'CANONICAL_DIRECTORY': None,
    'CANONICAL_IMPORT_RECEIPT_FILENAME': None,
    'CONTINUITY_PROBE_KINDS': None,
    'CampaignLease': 'class',
    'ConfigurationError': None,
    'ENVIRONMENT_ISOLATION_DIR': None,
    'ENVIRONMENT_ISOLATION_SCHEMA': None,
    'EVALUATION_BASELINE_ABANDON_FILENAME': None,
    'EVALUATION_BASELINE_AUTHORIZATION_FILENAME': None,
    'EVALUATION_BASELINE_COMMIT_FILENAME': None,
    'EVALUATION_BASELINE_PREPARED_FILENAME': None,
    'EVALUATION_BASELINE_RECOVERY_FILENAME': None,
    'EVALUATION_CHECKPOINTS_DIRNAME': None,
    'EVALUATION_RUN_FILENAME': None,
    'EVIDENCE_CONFLICT_QUARANTINE_DIR': None,
    'EVIDENCE_CONFLICT_QUARANTINE_SCHEMA': None,
    'EVIDENCE_ROOT_SUPERSESSION_DIR': None,
    'EVIDENCE_ROOT_SUPERSESSION_SCHEMA': None,
    'FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS': None,
    'FAILED_JOB_EVIDENCE_HOST_RUN_ROOT': None,
    'FORMAL_FAILED_JOB_RECOVERY_COMMAND': None,
    'FORMAL_POST_RUN_SEAL_RECOVERY_COMMAND': None,
    'JOB_RETRY_DELAY_SECONDS': None,
    'JOB_RETRY_LIMIT': None,
    'Job': 'class',
    'OFFICIAL_COMPACTION_LITE_RECOVERY_FILES': None,
    'OFFICIAL_COMPACTION_LITE_RECOVERY_JOB_IDS': None,
    'RECOVERY_CONTINUITY_DRIFT_RECEIPT_SCHEMA': None,
    'RECOVERY_CONTINUITY_DRIFT_STATUSES': None,
    'RUNTIME_SUCCESSOR_CHANGED_TOOL_PATH_JOB_IDS': None,
    'SAFE_ID_RE': None,
    'SHA256_RE': None,
    'STAGING_PREPARED_FILENAME': None,
    'StagingDeadlinePaused': 'class',
    'StagingStopTheLine': 'class',
    'TimingLedgerGateError': 'class',
    'ToolEvolutionRequired': 'class',
    '_PHASE_EVALUATION_HYBRID_FILES': None,
    '_VC_SEQUENCE_FILE_RE': None,
    '__file__': None,
    '_acceptance_validation_modes': '(campaign_dir: Path, classification: dict[str, Any], rules: tuple[str, ...]) -> dict[str, str]',
    '_accounting_resolve_command': '(arguments: argparse.Namespace) -> dict[str, Any]',
    '_active_unsealed_attempts': '(campaign_dir: Path, phase: str, *, _manifest: Mapping[str, Any] | None=None, _baseline: int | None=None) -> list[str]',
    '_apply_candidate_runtime_override': '(campaign_dir: Path, manifest: dict[str, Any], candidate_id: str | None) -> dict[str, Any]',
    '_approval_revision_baselines': '(campaign_dir: Path, candidate_id: str) -> list[int]',
    '_approval_revision_delta': '(current_path: Path, revised_path: Path) -> dict[str, Any]',
    '_approved_rules': '(campaign_dir: Path, manifest: dict[str, Any], *, require_approved: bool) -> tuple[str, ...]',
    '_assertions_root': '(campaign_dir: Path, candidate_id: str) -> Path',
    '_assertions_root_for_baseline': '(campaign_dir: Path, candidate_id: str, baseline: int) -> Path',
    '_attempt_conflict_quarantine_receipt': '(campaign_dir: Path, phase: str, candidate_id: str | None, attempt_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None',
    '_attempt_continuity_drift_receipt': '(campaign_dir: Path, attempt_id: str) -> dict[str, Any] | None',
    '_attempt_continuity_drifted': '(attempt: Mapping[str, Any]) -> bool',
    '_attempt_deadline': '(arguments: argparse.Namespace, phase: str) -> incremental_recovery.WallClockDeadline',
    '_attempt_evolution_impact': '(campaign_dir: Path, manifest: Mapping[str, Any], attempt_root: Path, *, phase: str, candidate_id: str | None) -> dict[str, Any]',
    '_attempt_evolution_invalidated_job_ids': '(campaign_dir: Path, manifest: Mapping[str, Any], attempt_root: Path, attempt: Mapping[str, Any], *, phase: str, candidate_id: str | None) -> list[str]',
    '_attempt_failure_facts': '(payload: Mapping[str, Any]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]',
    '_attempt_recovery_segment_root': '(attempt_root: Path, recovery_revision: str) -> Path',
    '_attempt_recovery_segment_terminal_failed': '(campaign_dir: Path, *, candidate_id: str, attempt_id: str, segment_root: Path) -> bool',
    '_authoritative_recovery_execute_jobs': '(campaign_dir: Path, candidate_id: str, reservation: Mapping[str, Any]) -> tuple[int, dict[str, Any], dict[str, Any]]',
    '_authorize_phase_recovery_production_paths': '(campaign_dir: Path, manifest: Mapping[str, Any], *, phase: str, candidate_id: str | None, attempt_root: Path, attempt: Mapping[str, Any], current_tool: Mapping[str, Any], recovery_scope: Mapping[str, Any]) -> set[str]',
    '_authorized_evaluator_digests': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, baseline: int) -> dict[str, str]',
    '_b0_evaluator_authorized_digest_history': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> list[dict[str, str]]',
    '_build_parser': '() -> argparse.ArgumentParser',
    '_campaign_attempt_roots': '(campaign_dir: Path) -> list[tuple[str, str | None, Path]]',
    '_campaign_cleanup_reserve_seconds': '() -> float',
    '_campaign_contamination_facts': '(campaign_dir: Path, *, _manifest: Mapping[str, Any] | None=None) -> list[dict[str, Any]]',
    '_campaign_contamination_records': '(campaign_dir: Path, *, _manifest: Mapping[str, Any] | None=None) -> list[str]',
    '_campaign_effective_tool_identity': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]',
    '_campaign_environment_decision': '(campaign_dir: Path, *, _manifest: Mapping[str, Any] | None=None) -> dict[str, Any]',
    '_campaign_file': '(campaign_dir: Path, relative: str) -> Path',
    '_campaign_jobs': '(campaign_dir: Path, manifest: dict[str, Any], phase: str, *, candidate_id: str | None=None, runtime_image: str | None=None, profile_id: str | None=None, profile_digest: str | None=None, build_id: str | None=None, deployed_version: str | None=None, candidate_image_id: str | None=None, source_tree_sha256: str | None=None, candidate_purpose: str | None=None, use_approved_scenario: bool | None=None) -> list[Job]',
    '_campaign_lease_pid_alive': '(pid: Any) -> bool',
    '_campaign_lease_staleness': '(payload: Mapping[str, Any], *, now: datetime | None=None) -> tuple[bool, str, bool, bool]',
    '_campaign_lock': '(campaign_dir: Path, *, deadline: incremental_recovery.WallClockDeadline | None=None) -> Iterable[None]',
    '_campaign_operation_lease': '(campaign_dir: Path, *, phase: str, candidate_id: str | None, command: str, deadline: incremental_recovery.WallClockDeadline, allow_stale_recovery: bool=False, campaign_id: str | None=None) -> Iterable[CampaignLease | None]',
    '_campaign_plan_deadline': '(campaign_dir: Path) -> str | None',
    '_campaign_resume_command': '(arguments: argparse.Namespace) -> dict[str, Any]',
    '_campaign_stage_replay_facts': '(campaign_dir: Path, run_manifest: Mapping[str, Any]) -> dict[str, Any]',
    '_campaign_timing_ledger_dir': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> Path',
    '_campaign_tool_evolutions': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]',
    '_candidate_build_receipt_path': '(campaign_dir: Path, candidate_id: str) -> Path',
    '_candidate_failed_run_for_review': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, review_event: Mapping[str, Any]) -> dict[str, Any]',
    '_candidate_revision_dir': '(campaign_dir: Path, revision: int) -> Path',
    '_candidate_revisions_root': '(campaign_dir: Path) -> Path',
    '_capture_attempt_relative': '(phase: str, candidate_id: str | None) -> Path',
    '_capture_stage_attempt_context': '(campaign_dir: Path, stage: Mapping[str, Any], *, phase: str, candidate_id: str | None) -> tuple[Path, dict[str, Any]]',
    '_cheap_capture_tool_impact': '(manifest: Mapping[str, Any], jobs: Iterable[Job], tool_identity: Mapping[str, Any], *, campaign_dir: Path | None=None) -> dict[str, Any]',
    '_classification_official_authority': '(classification: dict[str, Any]) -> dict[str, str]',
    '_committed_vc_sequences': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[int]',
    '_compile_and_run_vc_batch_legacy': '(arguments: argparse.Namespace, *, governance: dict[str, Any], _compiler: Any | None) -> tuple[dict[str, Any], int]',
    '_conflict_quarantined_attempts': '(campaign_dir: Path) -> set[tuple[str, str | None, str]]',
    '_current_candidate_revision': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> int | None',
    '_current_candidate_revision_record': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[int | None, dict[str, Any] | None]',
    '_current_evaluation_baseline': '(campaign_dir: Path, candidate_id: str) -> tuple[int, dict[str, Any] | None]',
    '_directory_identity': '(path: Path, *, ignore_device: bool=False) -> tuple[int, int] | None',
    '_directory_tree_digest': '(root: Path) -> str',
    '_effective_assertion_profile': '(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any] | None',
    '_effective_classification_view': '(campaign_dir: Path, candidate_id: str, classification: dict[str, Any]) -> dict[str, Any]',
    '_environment_isolations': '(campaign_dir: Path) -> list[dict[str, Any]]',
    '_evaluation_baseline_dir': '(campaign_dir: Path, candidate_id: str, baseline: int) -> Path',
    '_evaluation_baseline_has_outputs': '(campaign_dir: Path, candidate_id: str, baseline: int) -> list[str]',
    '_evaluation_baseline_states': '(campaign_dir: Path, candidate_id: str) -> dict[int, dict[str, bool]]',
    '_evidence_conflict_quarantines': '(campaign_dir: Path) -> list[dict[str, Any]]',
    '_evidence_root_relocations': '(campaign_dir: Path, *, phase: str, candidate_id: str | None, attempt_id: str) -> dict[str, str]',
    '_evolution_reuse_context': '(campaign_dir: Path, manifest: Mapping[str, Any], *, phase: str, candidate_id: str | None, attempt_root: Path, current_tool: Mapping[str, Any]) -> dict[str, Any] | None',
    '_existing_candidate_revisions': '(campaign_dir: Path) -> list[int]',
    '_failed_job_evidence_host_route': '(value: Any) -> tuple[Path, PurePosixPath]',
    '_failed_job_ids': '(results: Any) -> list[str]',
    '_fingerprint': '(payload: dict[str, Any]) -> str',
    '_git_commit': '(root: Path) -> str | None',
    '_in_isolated_seal_rehearsal': '(arguments: argparse.Namespace, command: str) -> bool',
    '_interrupted_recovery_transition_path': '(attempt_root: Path) -> Path',
    '_is_c0154_vc5_failed_job_control_epoch_campaign': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> bool',
    '_is_policy_v2_identity': '(identity: Mapping[str, Any]) -> bool',
    '_isolation_invalidated_attempts': '(campaign_dir: Path) -> set[tuple[str, str | None, str]]',
    '_job_execution_sha256': '(job: Job) -> str',
    '_job_rehearsal_contract_from_manifest': '(campaign_dir: Path, manifest: Mapping[str, Any], *, target_scenario_override: Mapping[str, Any] | None=None, tool_files_sha256_override: str | None=None, recovery_rehearsal_receipt: Mapping[str, Any] | None=None, control_receipts_override: Mapping[str, Any] | None=None, _allow_historical_tool_identity: bool=False, _allow_bound_evidence_label_digest: bool=False, _require_incremental_noop_preflight_active: bool=True, _allow_stopped_metadata_only: bool=False) -> dict[str, Any]',
    '_job_tool_dependency_files': '(job: Job, tool_identity: Mapping[str, Any]) -> dict[str, str]',
    '_latest_failed_attempt_for_identity': '(campaign_dir: Path, *, phase: str, candidate_id: str | None, identity: Mapping[str, Any], manifest: Mapping[str, Any] | None=None) -> tuple[Path, dict[str, Any]] | None',
    '_ledger_last_stage_revision': '(ledger_dir: Path) -> dict[str, Any] | None',
    '_legacy_stage_path': '(campaign_dir: Path, stage: str, candidate_id: str | None=None) -> tuple[str, Path]',
    '_load_attempt_recovery_reservation': '(campaign_dir: Path, segment_root: Path, *, candidate_id: str, attempt_id: str, recovery_revision: str) -> dict[str, Any]',
    '_load_attempt_recovery_segment': '(campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str) -> tuple[Path, dict[str, Any], dict[str, Any]]',
    '_load_capture_attempt': '(campaign_dir: Path, phase: str, candidate_id: str | None, attempt_id: str, *, _historical_manifest_controls: bool=False, _verified_campaign_manifest: Mapping[str, Any] | None=None, _replay_evidence_permissions: bool=True) -> tuple[Path, dict[str, Any]]',
    '_load_capture_reservation': '(campaign_dir: Path, attempt_root: Path, *, phase: str, candidate_id: str | None, _manifest: Mapping[str, Any] | None=None, _historical_manifest_controls: bool=False) -> dict[str, Any]',
    '_load_evaluation_baseline_recovery': '(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any]',
    '_load_recovery_execution_handoff': '(campaign_dir: Path, manifest: Mapping[str, Any], *, source_root: Path, source_attempt: Mapping[str, Any], planned_jobs: Sequence[Job], execute_job_ids: Iterable[str], reuse_job_ids: Iterable[str], recovery_scope: Mapping[str, Any], tool_identity: Mapping[str, Any]) -> dict[str, Any] | None',
    '_load_stage_result': '(campaign_dir: Path, stage: str, candidate_id: str | None=None, *, _import_chain: frozenset[Path] | None=None, _replay_machine_receipts: bool=True, _shallow: bool=False, _ignore_checkpoint: bool=False, _import_cache: dict[tuple[str, str, str | None], dict[str, Any]] | None=None, _skip_evidence_scan: bool=False, _historical_manifest_controls: bool=False, _verified_campaign_manifest: Mapping[str, Any] | None=None, _baseline: int | None=None) -> dict[str, Any]',
    '_normalized_json_sha256': '(payload: dict[str, Any]) -> str',
    '_official_seal_chain_stage_replay_actions': '(campaign_dir: Path, run_manifest: Mapping[str, Any]) -> list[dict[str, Any]] | None',
    '_optional_campaign_timing_ledger_dir': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> Path | None',
    '_ordered_capture_attempts': '(campaign_dir: Path, phase: str, candidate_id: str | None, *, _historical_manifest_controls: bool=False, _manifest: Mapping[str, Any] | None=None) -> list[tuple[Path, dict[str, Any]]]',
    '_path_in_candidate_source': '(path: Path, source_root: Path, label: str) -> Path',
    '_phase_evaluation_recovery_scope': '(campaign_dir: Path, manifest: Mapping[str, Any], *, phase: str, candidate_id: str | None, attempt_root: Path, attempt: Mapping[str, Any], allow_empty: bool=False, allow_awaiting_failures: bool=False, allow_evolution_invalidated: bool=False) -> dict[str, Any]',
    '_phase_recovery_exact_affected_job_ids': '(production_paths: Iterable[str], *, planned_job_ids: Iterable[str], recovery_scope: Mapping[str, Any], explicit_affected_job_ids: Iterable[str] | None=None) -> set[str]',
    '_plan_evaluator_entry_digests': '(manifest: Mapping[str, Any]) -> dict[str, str]',
    '_prior_complete_results': "(campaign_dir: Path, relative: Path, jobs: list[Job], *, phase: str, candidate_id: str | None, identity: dict[str, Any], tool_identity: Mapping[str, Any] | None=None, affected_job_ids: Iterable[str]=(), expected_reuse_job_ids: Iterable[str] | None=None, source_attempt_id: str | None=None, source_campaign_dir: Path | None=None, source_candidate_id: str | None=None, source_receipt_binding: Mapping[str, Any] | None=None, allowed_high_risk_path_changes: Iterable[str]=(), validated_current_production_sha256: str | None=None, allowed_source_statuses: Iterable[str]=('failed',), allow_unbound_cross_campaign_preview: bool=False, source_tool_identity: Mapping[str, Any] | None=None, evolution_context: Mapping[str, Any] | None=None) -> list[dict[str, Any]]",
    '_probe_capture_environment': '(manifest: dict[str, Any], output_dir: Path, phase: str, *, deadline: incremental_recovery.WallClockDeadline | None=None, heartbeat: Any | None=None) -> dict[str, Any]',
    '_probe_snapshot_digests': '(manifest: Mapping[str, Any]) -> dict[str, str]',
    '_read_campaign_lease': '(campaign_dir: Path) -> dict[str, Any] | None',
    '_read_candidate_revision_record': '(campaign_dir: Path, revision: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]',
    '_read_json': '(path: Path, label: str) -> dict[str, Any]',
    '_read_stop_reason': '(run_dir: Path) -> str | None',
    '_recovery_continuity_receipt_path': '(campaign_dir: Path, attempt_id: str) -> Path',
    '_reject_contaminated_campaign': '(campaign_dir: Path, *, allow_metadata_only_repair: bool=False, attempt_root: Path | None=None, attempt: Mapping[str, Any] | None=None) -> None',
    '_replay_attempt_evidence_permissions': '(attempt_root: Path, payload: Mapping[str, Any], *, root_relocations: Mapping[str, str] | None=None) -> dict[str, Any] | None',
    '_replay_attempt_recovery_evidence_permissions': '(segment_root: Path, payload: Mapping[str, Any]) -> dict[str, Any] | None',
    '_replay_vc_checkpoint': '(campaign_dir: Path, plan: Mapping[str, Any], phase: str, *, revision: int | None=None) -> tuple[Path, dict[str, Any]]',
    '_require_candidate_revision': '(campaign_dir: Path, manifest: Mapping[str, Any], *, action: str) -> tuple[int, dict[str, Any] | None]',
    '_require_file_binding': '(value: Any, label: str) -> None',
    '_require_formal_campaign': '(campaign_dir: Path, manifest: dict[str, Any] | None=None) -> dict[str, Any]',
    '_require_recovery_preview_scope_equals_frozen_jobs': '(preview: Mapping[str, Any], execute_jobs: Sequence[str], *, label: str) -> None',
    '_requires_complete_vc_artifacts': '(value: Mapping[str, Any] | str) -> bool',
    '_rerun_all_invalidated_attempts': '(campaign_dir: Path) -> set[tuple[str, str | None, str]]',
    '_rfc3339_datetime': '(value: Any, label: str) -> datetime',
    '_run_job_with_retry': '(job: Job, log_root: Path, scenario_context: ScenarioReceiptContext | None=None, *, identity: Mapping[str, Any] | None=None, tool_identity: Mapping[str, Any] | None=None, deadline: incremental_recovery.WallClockDeadline | None=None, heartbeat: Any | None=None) -> dict[str, Any]',
    '_secure_copy_file_once': '(source: Path, destination: Path) -> dict[str, Any]',
    '_secure_write_json_once': '(path: Path, payload: dict[str, Any]) -> None',
    '_stage_path': "(campaign_dir: Path, stage: str, candidate_id: str | None=None, *, mode: str='read') -> tuple[str, Path]",
    '_stage_read_source': '(campaign_dir: Path, candidate_id: str, baseline: int, stage: str) -> dict[str, Any]',
    '_stage_replay_input_directory': '(path: Path, label: str) -> str',
    '_stage_replay_input_file': '(path: Path, label: str) -> str',
    '_stage_write_target': '(campaign_dir: Path, candidate_id: str, baseline: int, stage: str) -> Path',
    '_staging_attempt_sha256s': '(attempt_dir: Path) -> tuple[str | None, str | None]',
    '_staging_error_summary': '(error: BaseException) -> str',
    '_staging_run_arguments': '(arguments: argparse.Namespace) -> argparse.Namespace',
    '_timing_event_phase': '(ledger_dir: Path, sequence: int) -> str',
    '_tool_entry_digest_map': '(identity: Mapping[str, Any]) -> dict[str, str]',
    '_tool_evolution_quiescence_problems': '(campaign_dir: Path) -> list[str]',
    '_tool_identity': '(*, include_git: bool=True) -> dict[str, Any]',
    '_tool_identity_drift': '(current: Mapping[str, Any], expected: Mapping[str, Any]) -> dict[str, list[str]]',
    '_tool_tree_entries': '(tool_root: Path) -> list[dict[str, Any]]',
    '_utc_now': '() -> str',
    '_validate_interrupted_recovery_transition': '(campaign_dir: Path, manifest: Mapping[str, Any], *, source_root: Path, source_attempt: Mapping[str, Any], current_tool: Mapping[str, Any]) -> dict[str, Any]',
    '_validate_recovery_scope_plan': '(campaign_dir: Path, *, phase: str, candidate_id: str | None, source_root: Path, scope: Mapping[str, Any], planned_jobs: Iterable[Job], allow_empty: bool=False) -> tuple[set[str], set[str]]',
    '_vc_campaign_plan': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]',
    '_vc_checkpoint_path': '(campaign_dir: Path, phase: str, *, revision: int | None=None) -> Path',
    '_vc_checkpoint_reference': '(campaign_dir: Path, path: Path, checkpoint: Mapping[str, Any]) -> dict[str, str]',
    '_vc_run_manifest_from_batch': "(batch: Mapping[str, Any], *, batch_model: str='legacy') -> dict[str, Any]",
    '_verify_candidate_attempt_identity': '(manifest: dict[str, Any], identity: dict[str, Any]) -> None',
    '_verify_catalog_stage_output': '(output: Path, receipt: dict[str, Any]) -> None',
    '_verify_classification_draft': '(root: Path) -> dict[str, Any]',
    '_verify_evaluator_digests_authorized': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, baseline: int, digests: Mapping[str, Any], *, label: str) -> None',
    '_verify_execution_tree': '(capture_root: Path | None) -> None',
    '_verify_official_binaries': '(manifest: dict[str, Any], *, deadline: incremental_recovery.WallClockDeadline | None=None, heartbeat: Any | None=None) -> dict[str, Any]',
    '_wait_for_monitor_finalization': '(run_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]',
    '_write_or_verify_json': '(path: Path, payload: dict[str, Any]) -> None',
    '_write_or_verify_json_ignoring': '(path: Path, payload: Mapping[str, Any], *, volatile: Iterable[str]) -> None',
    '_write_staging_batch_artifacts': '(campaign_dir: Path, attempt_dir: Path, *, plan: Mapping[str, Any], batch: Mapping[str, Any], run_manifest: Mapping[str, Any], owner_nonce: str, prepared_at_utc: str) -> dict[str, Any]',
    'campaign_status': '(campaign_dir: Path, candidate_id: str | None=None) -> dict[str, Any]',
    'compile_and_run_vc_batch': '(arguments: argparse.Namespace, *, _compiler: Any | None=None) -> tuple[dict[str, Any], int]',
    'create_campaign': '(arguments: argparse.Namespace) -> dict[str, Any]',
    'file_sha256': None,
    'load_campaign_manifest': '(path: Path, *, _control_epoch_bootstrap: bool=False, _skip_control_validation: bool=False) -> dict[str, Any]',
    'recovery_preview_continuity': '(campaign_dir: Path, *, manifest: Mapping[str, Any], phase: str, candidate_id: str | None, source_attempt_id: str, output_dir: Path) -> dict[str, Any]',
    'recovery_reuse_check': '(campaign_dir: Path, *, phase: str, candidate_id: str | None, source_attempt_id: str, reuse_job_ids: Iterable[str], execute_job_ids: Iterable[str]) -> dict[str, Any]',
})

# 服务面之外的适配器成员 → 种类。value：常量值；module：模块对象（后接属性时解析到该模块的符号，异常类型也
# 由它取）；callable：可调用对象（适配器方法）。每个客户端都必须为下列成员登记绑定。只收共享代码里确有客户端
# 差异的名字：子命令名、阶段名与动作 ID 两客户端相同（Claude 编排器子命令与 Codex 同名），仍按字面书写。
ADAPTER_MEMBERS: Mapping[str, str] = MappingProxyType({
    "orchestrator": "module",
    "orchestrator_basenames": "value",  # 编排器 CLI 名：监督器据此识别预声明动作是否调用编排器
    "forbidden_subcommands": "value",  # 预声明动作里拒绝的编排器子命令（基础集合）
    "evaluation_builder_basename": "value",  # 评估动作的断言结果 builder 文件名
    "target_version_field": "value",  # 断言包取证清单里记录目标版本的字段名
    "root_cause_codes_path": "value",  # 根因码表路径：调用方用自己那份根因模块的 load_codes 加载
    "deploy_receipt_glob": "value",  # 受管部署收据文件名模式
    "host_layout": "module",  # 宿主机数据根与容器证据根映射（VC-0 收口模块的三个路径函数与其异常）
    "provenance": "module",  # 请求账务计量（collect_campaign_provenance 与 ProvenanceError）
    "identity_policy": "module",  # 工具身份引擎（evaluator_dependency_digests 与 ToolIdentityPolicyError）
    "reject_volatile_dimensions": "callable",  # 调用根因 API 前拒绝客户端专属的易变维度值
    "recovery_segment_summary": "callable",  # 恢复段摘要（签名同对账器原函数）
    "segment_reuse_proofs": "callable",  # 恢复段复用证明（签名同对账器原函数）
    "requires_project_ledger": "callable",  # (campaign_mode, target_version) → 是否必须在项目总账内
})


def _surface_bindings(module: str) -> dict[str, tuple[str, str]]:
    return {name: (module, name) for name in ORCHESTRATOR_SURFACE}


# 客户端 → 成员名（服务面符号与适配器成员）→ (受管包内模块名, 符号；None 表示模块本身)。
# Codex 的绑定逐字指向改造前写死或导入的原符号；Claude 的绑定注明落实它的工作项。
ADAPTER_BINDINGS: Mapping[str, Mapping[str, tuple[str, str | None]]] = MappingProxyType({
    "codex": MappingProxyType({
        **_surface_bindings("codex_upgrade"),
        "orchestrator": ("codex_upgrade", None),
        "orchestrator_basenames": ("codex_upgrade_vc_artifacts", "UPGRADE_CLI_BASENAMES"),
        "forbidden_subcommands": ("codex_upgrade_client_adapter", "FORBIDDEN_SUBCOMMANDS"),
        "evaluation_builder_basename": ("codex_upgrade_supervisor", "EVALUATION_ACTION_BUILDER_BASENAME"),
        "target_version_field": ("codex_upgrade_client_adapter", "TARGET_VERSION_FIELD"),
        "root_cause_codes_path": ("codex_upgrade_root_cause", "DEFAULT_CODES_PATH"),
        "deploy_receipt_glob": ("codex_upgrade_wire_transition", "DEPLOY_RECEIPT_GLOB"),
        "host_layout": ("codex_upgrade_vc0_closeout", None),
        "provenance": ("codex_upgrade_live_request_provenance", None),
        "identity_policy": ("codex_upgrade_tool_identity_policy", None),
        "reject_volatile_dimensions": ("codex_upgrade_client_adapter", "reject_volatile_dimensions"),
        "recovery_segment_summary": ("codex_upgrade_reconciler", "recovery_segment_summary"),
        "segment_reuse_proofs": ("codex_upgrade_reconciler", "segment_reuse_proofs"),
        "requires_project_ledger": ("codex_upgrade_project_ledger", "requires_project_ledger"),
    }),
    "claude": MappingProxyType({
        **_surface_bindings("claude_upgrade"),  # B1-09
        "orchestrator": ("claude_upgrade_client_adapter", "ORCHESTRATOR"),
        "orchestrator_basenames": ("claude_upgrade_client_adapter", "ORCHESTRATOR_BASENAMES"),
        "forbidden_subcommands": ("claude_upgrade_client_adapter", "FORBIDDEN_SUBCOMMANDS"),
        "evaluation_builder_basename": ("claude_upgrade_client_adapter", "EVALUATION_BUILDER_BASENAME"),
        "target_version_field": ("claude_upgrade_client_adapter", "TARGET_VERSION_FIELD"),
        "root_cause_codes_path": ("claude_upgrade_client_adapter", "ROOT_CAUSE_CODES_PATH"),
        "deploy_receipt_glob": ("claude_upgrade_client_adapter", "DEPLOY_RECEIPT_GLOB"),
        # 设计 D3：两客户端共用 ARM64 数据根与 Campaign 布局，宿主机路径函数只调用、不复制。
        "host_layout": ("codex_upgrade_vc0_closeout", None),
        "provenance": ("claude_upgrade_live_request_provenance", None),  # B1-11
        "identity_policy": ("claude_tool_identity_policy", None),  # B1-07
        "reject_volatile_dimensions": ("claude_upgrade_client_adapter", "reject_volatile_dimensions"),
        "recovery_segment_summary": ("claude_upgrade", "recovery_segment_summary"),  # B1-09
        "segment_reuse_proofs": ("claude_upgrade", "segment_reuse_proofs"),  # B1-09
        "requires_project_ledger": ("claude_upgrade_client_adapter", "requires_project_ledger"),
    }),
})


def _import_managed(module_name: str) -> Any:
    """按包路径导入受管模块；脚本方式运行且仓库根不在 sys.path（没有 tools 包）时退回扁平模块名。

    只在缺的正是 ``tools`` 包本身时退回；受管树里缺某个子模块（例如尚未实现的 Claude 模块）照常抛错。
    """

    try:
        return importlib.import_module(f"{PACKAGE}.{module_name}")
    except ModuleNotFoundError as error:
        if error.name not in {"tools", PACKAGE}:
            raise
    return importlib.import_module(module_name)


def _resolve(binding: tuple[str, str | None]) -> Any:
    module_name, symbol = binding
    module = _import_managed(module_name)
    return module if symbol is None else getattr(module, symbol)


class OrchestratorFacade:
    """按服务面绑定现取符号的编排器门面：只暴露 ``ORCHESTRATOR_SURFACE`` 里的名字。"""

    __slots__ = ("_client",)

    def __init__(self, client: str) -> None:
        if client not in ADAPTER_BINDINGS:
            raise ClientAdapterError(f"未知客户端：{client!r}")
        object.__setattr__(self, "_client", client)

    def __getattr__(self, name: str) -> Any:
        if name not in ORCHESTRATOR_SURFACE:
            raise AttributeError(f"{name} 不在编排器服务面（ORCHESTRATOR_SURFACE）")
        return _resolve(ADAPTER_BINDINGS[self._client][name])

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("编排器门面只读")


class ClientAdapter:
    """一个客户端的适配器：成员按 ``ADAPTER_BINDINGS`` 每次现取；恢复接口统一接收 ``RecoveryContext``。"""

    __slots__ = ("client",)

    def __init__(self, client: str) -> None:
        bindings = ADAPTER_BINDINGS.get(client)
        if bindings is None:
            raise ClientAdapterError(f"未知客户端：{client!r}")
        missing = sorted(set(ADAPTER_MEMBERS) - set(bindings))
        if missing:
            raise ClientAdapterError(f"客户端 {client} 缺少适配器成员绑定：{missing}")
        object.__setattr__(self, "client", client)

    def __getattr__(self, member: str) -> Any:
        if member not in ADAPTER_MEMBERS:
            raise AttributeError(f"{member} 不是适配器成员；编排器符号经 adapter.orchestrator.<原名> 访问")
        return _resolve(ADAPTER_BINDINGS[self.client][member])

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("客户端适配器只读")

    def __repr__(self) -> str:
        return f"ClientAdapter({self.client!r})"

    # 恢复接口：失败源身份一律来自 RecoveryContext，不取当前默认值。
    def recovery_reuse_check(
        self,
        context: RecoveryContext,
        *,
        reuse_job_ids: Iterable[str],
        execute_job_ids: Iterable[str],
    ) -> dict[str, Any]:
        return self.orchestrator.recovery_reuse_check(
            context.campaign_dir,
            phase=context.phase,
            candidate_id=context.candidate.candidate_id,
            source_attempt_id=context.source_attempt_id,
            reuse_job_ids=reuse_job_ids,
            execute_job_ids=execute_job_ids,
        )

    def recovery_preview_continuity(self, context: RecoveryContext) -> dict[str, Any]:
        if context.output_dir is None:
            raise ClientAdapterError("recovery_preview_continuity 需要 RecoveryContext.output_dir（探针输出目录）")
        return self.orchestrator.recovery_preview_continuity(
            context.campaign_dir,
            manifest=context.manifest,
            phase=context.phase,
            candidate_id=context.candidate.candidate_id,
            source_attempt_id=context.source_attempt_id,
            output_dir=context.output_dir,
        )

    def attempt_evolution_impact(self, context: RecoveryContext) -> dict[str, Any]:
        return self.orchestrator._attempt_evolution_impact(
            context.campaign_dir,
            context.manifest,
            context.attempt_root,
            phase=context.phase,
            candidate_id=context.candidate.candidate_id,
        )


_ADAPTERS: dict[str, ClientAdapter] = {}
_ADAPTERS_LOCK = threading.Lock()


def adapter_for(client: str) -> ClientAdapter:
    """按 client 取适配器；未知客户端即抛错，不默认成 codex。"""

    if not isinstance(client, str) or client not in ADAPTER_BINDINGS:
        raise ClientAdapterError(f"未知客户端：{client!r}")
    with _ADAPTERS_LOCK:
        adapter = _ADAPTERS.get(client)
        if adapter is None:
            adapter = _ADAPTERS[client] = ClientAdapter(client)
    return adapter


def client_of_manifest(manifest: Mapping[str, Any]) -> str:
    """Campaign 总计划的归属客户端：读顶层 ``client``；只有历史 Codex 总计划允许缺失并隐含 codex。"""

    if not isinstance(manifest, Mapping):
        raise ClientAdapterError("Campaign 总计划必须是 JSON 对象")
    client = manifest.get("client")
    if client is None:
        schema = manifest.get("schema_version")
        if isinstance(schema, str) and schema.startswith(LEGACY_CODEX_CAMPAIGN_SCHEMA_PREFIX):
            return "codex"
        raise ClientAdapterError(f"Campaign 总计划缺少 client，且 schema 不是历史 Codex 总计划：{schema!r}")
    if not isinstance(client, str) or client not in ADAPTER_BINDINGS:
        raise ClientAdapterError(f"Campaign 总计划的 client 非法：{client!r}")
    return client


def client_of(campaign_dir: Path) -> str:
    """读 ``<campaign_dir>/campaign.json`` 判定 Campaign 归属的客户端。"""

    path = Path(campaign_dir) / CAMPAIGN_MANIFEST_FILENAME
    if path.is_symlink() or not path.is_file():
        raise ClientAdapterError(f"Campaign 总计划不存在或不可信：{path}")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ClientAdapterError(f"Campaign 总计划不可读：{path}") from error
    return client_of_manifest(manifest)


def root_cause_dimensions(
    adapter: ClientAdapter,
    codes: Mapping[str, Any],
    stable_error_code: str,
    dimensions: Mapping[str, str],
) -> dict[str, str]:
    """按码表登记补齐 client 维度，并做客户端专属的易变值检查。

    码表要求 ``client`` 维度时填适配器的客户端名（Claude 码表每个码都有）；Codex 码表不含 ``client``，原样返回，
    根因 ID 与改造前逐字相同。``codes`` 是调用方用自己那份根因模块 ``load_codes`` 加载的码表；码未登记时原样返回，
    由根因 API 照常拒绝。
    """

    result = dict(dimensions)
    entry = codes.get("codes", {}).get(stable_error_code) if isinstance(codes, Mapping) else None
    if isinstance(entry, Mapping) and "client" in entry.get("stable_dimensions", ()):
        result["client"] = adapter.client
    adapter.reject_volatile_dimensions(result)
    return result


def require_consistent_client(expected: str, sources: Mapping[str, Mapping[str, Any] | None]) -> None:
    """身份根之间的 client 必须一致（Campaign 总计划、Store Campaign、项目总账 plan、计时账本 plan）。

    ``sources`` 是「身份根名称 → 该对象」；任一对象缺失、缺 ``client`` 或与 ``expected`` 不等即拒绝，
    不默认成 codex（历史 Codex 制品的隐含归属只由 ``client_of_manifest`` 对总计划判定）。
    """

    if expected not in ADAPTER_BINDINGS:
        raise ClientAdapterError(f"未知客户端：{expected!r}")
    for name, payload in sources.items():
        if not isinstance(payload, Mapping):
            raise ClientAdapterError(f"{name} 缺失，无法核对客户端身份")
        actual = payload.get("client")
        if actual != expected:
            raise ClientAdapterError(f"{name} 的 client 为 {actual!r}，与 {expected!r} 不一致")
