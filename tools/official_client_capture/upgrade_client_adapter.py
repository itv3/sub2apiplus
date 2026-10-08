"""官方客户端升级控制面的客户端适配接口（Claude 升级控制面设计 D4、B1-01、附录 A）。

共享控制面（监督器、对账器、项目总账、计时账本、认证与入口模块，以及迁入 ``upgrade_control_plane`` 的共享流程）
对编排器的全部依赖收进本模块定义的协议：

* ``ORCHESTRATOR_SURFACE``：共享代码可以经 ``adapter.orchestrator.<原名>`` 访问的编排器符号与签名文本（类记
  ``class``，常量与导入符号记 None）。清单由 ``tests/orchestrator_surface_inventory.py`` 从基线源码提取后冻结：
  209 个可调用成员（含 6 个类）、42 个常量、3 个导入符号。
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


class Vc5AdmissionFactError(ClientAdapterError):
    """VC-5 准入的受管事实不成立（B5b-01，适配器成员 ``catalog_stage_profile``／``vc5_official_campaign``）。

    消息只是静态错误码（与驱动 ``vc5_admission.AdmissionError`` 同一组码），驱动原样记为准入报告里 blocked 的原因；
    不带路径、凭据或收据内容。"""


def vc5_plain_path(path: Path) -> None:
    """VC-5 准入事实读取的路径核对：拒绝非绝对路径、父目录跳转与整条路径上的符号链接（与驱动 ``plain_path`` 同一判据、
    同一错误码，两客户端的实现共用）。"""

    if not path.is_absolute() or ".." in path.parts:
        raise Vc5AdmissionFactError("path_not_absolute_or_contains_parent")
    for item in (path, *path.parents):
        if item.is_symlink():
            raise Vc5AdmissionFactError("path_contains_symlink")


def vc5_read_json(path: Path) -> dict[str, Any]:
    """VC-5 准入事实读取的 JSON 对象（与驱动 ``read_json`` 非私有读取同一判据与错误码）。"""

    vc5_plain_path(path)
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise Vc5AdmissionFactError("json_not_object")
    return value


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
    """恢复接口的统一入参：失败源身份全部显式给出。

    ``manifest``／``attempt_root`` 只有工具演进影响与连续性预览要用（缺失时这两个接口报错）；复用核对只用
    Campaign、阶段、候选坐标与源 attempt，调用方不必为它另读总计划。
    """

    campaign_dir: Path
    phase: str
    source_attempt_id: str
    candidate: CandidateCoordinates
    manifest: Mapping[str, Any] | None = None
    attempt_root: Path | None = None
    output_dir: Path | None = None  # recovery_preview_continuity 的探针输出目录


# 编排器服务面（B1-01 冻结）：共享代码经 adapter.orchestrator.<原名> 可访问的编排器符号 → 签名文本。
# 由 tests/orchestrator_surface_inventory.py 从基线提交 17c87351a 提取：209 个函数、42 个常量、3 个导入符号；
# Claude 编排器以同名同签名实现全部函数成员（B1-09 合同测试）。B1-09 按实际迁入集合重算：留原位的 11 个可迁函数的依赖
# 不再进服务面；被迁入函数调用的留原位函数，以及被其它迁入函数调用的迁入函数（经编排器互调，保证 Codex 测试对编排器
# 符号的替身照常生效）进入服务面（规则见生成器 RETAINED_FUNCTIONS、routed_migrated_functions）。
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
    'TOOL_EVOLUTION_FIX_COMMIT_RE': None,
    'TimingLedgerGateError': 'class',
    'ToolEvolutionRequired': 'class',
    '_VC_SEQUENCE_FILE_RE': None,
    '__file__': None,
    '_abort_staging_attempt_without_parent': '(campaign_dir: Path, attempt_dir: Path, *, plan: Mapping[str, Any], sequence: int, phase: str, attempt: int, stage: str, failure_kind: str, error_type: str, error_message: str | None=None, error_signature: str | None=None) -> dict[str, Any] | None',
    '_accounting_resolve_command': '(arguments: argparse.Namespace) -> dict[str, Any]',
    '_accounting_resolve_preview': '(campaign_dir: Path, manifest: Mapping[str, Any], *, operation_id: str, estimated_count: int | None, evidence: Path | None, reason: str) -> dict[str, Any]',
    '_active_unsealed_attempts': '(campaign_dir: Path, phase: str, *, _manifest: Mapping[str, Any] | None=None, _baseline: int | None=None) -> list[str]',
    '_append_timing_ledger_batch_events': '(ledger_dir: Path, events: list[tuple[str, str]], *, phase: str, sequence: int, expected_head: int) -> list[dict[str, Any]]',
    '_apply_accounting_resolution': '(campaign_dir: Path, manifest: Mapping[str, Any], preview: Mapping[str, Any], *, approved_by: str) -> dict[str, Any]',
    '_apply_campaign_resume': '(campaign_dir: Path, manifest: Mapping[str, Any], preview: Mapping[str, Any], *, approved_by: str, next_steps: str) -> dict[str, Any]',
    '_apply_candidate_runtime_override': '(campaign_dir: Path, manifest: dict[str, Any], candidate_id: str | None) -> dict[str, Any]',
    '_approval_revision_baselines': '(campaign_dir: Path, candidate_id: str) -> list[int]',
    '_approval_revision_delta': '(current_path: Path, revised_path: Path) -> dict[str, Any]',
    '_approved_rules': '(campaign_dir: Path, manifest: dict[str, Any], *, require_approved: bool) -> tuple[str, ...]',
    '_archive_uncommitted_formal_artifacts': '(campaign_dir: Path, *, sequence: int, phase: str, attempt: int) -> list[str]',
    '_assert_revision_admission': '(campaign_dir: Path, manifest: Mapping[str, Any], *, command: str) -> dict[str, Any] | None',
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
    '_authorize_and_commit_non_failure_baseline': '(campaign_dir: Path, manifest: Mapping[str, Any], *, candidate_id: str, revision: int, number: int, current_baseline: int, kind: str, recovery: Mapping[str, Any], batch: Mapping[str, Any], head_after: Mapping[str, Any], decision: Mapping[str, Any], root_cause_id: str, operation_id: str, ledger_dir: Path, ledger_event_type: str, next_action: str, result: dict[str, Any]) -> dict[str, Any]',
    '_authorized_evaluator_digests': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, baseline: int) -> dict[str, str]',
    '_b0_evaluator_authorized_digest_history': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str) -> list[dict[str, str]]',
    '_bootstrap_noop_first_batch': '(campaign_dir: Path, manifest: Mapping[str, Any], *, sequence: int, state_dir: Path, run_arguments: argparse.Namespace) -> dict[str, Any] | None',
    '_build_parser': '() -> argparse.ArgumentParser',
    '_campaign_attempt_roots': '(campaign_dir: Path) -> list[tuple[str, str | None, Path]]',
    '_campaign_batch_model': '(campaign_dir: Path) -> str',
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
    '_campaign_resume_preview': '(campaign_dir: Path, manifest: Mapping[str, Any], *, fix_commit: str, regression_receipt: Path, reason: str, control_root: Path | None) -> dict[str, Any]',
    '_campaign_stage_replay_facts': '(campaign_dir: Path, run_manifest: Mapping[str, Any]) -> dict[str, Any]',
    '_campaign_timing_ledger_dir': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> Path',
    '_campaign_tool_evolutions': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[dict[str, Any]]',
    '_campaign_uses_staging_model': '(campaign_dir: Path, plan: Mapping[str, Any]) -> bool',
    '_campaign_vc_plan': '(campaign_dir: Path) -> dict[str, Any]',
    '_candidate_accounting_checks': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, *, head: Mapping[str, Any], ledger_dir: Path) -> dict[str, Any]',
    '_candidate_build_receipt_path': '(campaign_dir: Path, candidate_id: str) -> Path',
    '_candidate_failed_run_for_review': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, review_event: Mapping[str, Any]) -> dict[str, Any]',
    '_candidate_identity_snapshot': '(campaign_dir: Path, candidate_id: str, candidate_source: Path | None) -> dict[str, Any]',
    '_candidate_revision_dir': '(campaign_dir: Path, revision: int) -> Path',
    '_candidate_revisions_root': '(campaign_dir: Path) -> Path',
    '_capture_attempt_relative': '(phase: str, candidate_id: str | None) -> Path',
    '_capture_stage_attempt_context': '(campaign_dir: Path, stage: Mapping[str, Any], *, phase: str, candidate_id: str | None) -> tuple[Path, dict[str, Any]]',
    '_classification_official_authority': '(classification: dict[str, Any]) -> dict[str, str]',
    '_committed_vc_sequences': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> list[int]',
    '_compile_and_run_vc_batch_legacy': '(arguments: argparse.Namespace, *, governance: dict[str, Any], _compiler: Any | None) -> tuple[dict[str, Any], int]',
    '_compile_and_run_vc_batch_staging': '(arguments: argparse.Namespace, *, governance: dict[str, Any], _compiler: Any | None) -> tuple[dict[str, Any], int]',
    '_complete_timing_ledger_phase_after_batch': '(campaign_dir: Path, plan: Mapping[str, Any], *, phase: str, sequence: int, ledger_dir: Path) -> dict[str, Any] | None',
    '_conflict_quarantined_attempts': '(campaign_dir: Path) -> set[tuple[str, str | None, str]]',
    '_current_candidate_revision': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> int | None',
    '_current_candidate_revision_record': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[int | None, dict[str, Any] | None]',
    '_current_evaluation_baseline': '(campaign_dir: Path, candidate_id: str) -> tuple[int, dict[str, Any] | None]',
    '_derive_b0_evaluation_run': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, revision: int, classification: Mapping[str, Any], rules: Sequence[str]) -> dict[str, Any]',
    '_directory_identity': '(path: Path, *, ignore_device: bool=False) -> tuple[int, int] | None',
    '_effective_assertion_profile': '(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any] | None',
    '_effective_classification_view': '(campaign_dir: Path, candidate_id: str, classification: dict[str, Any]) -> dict[str, Any]',
    '_empty_batch_governance': '() -> dict[str, Any]',
    '_environment_isolate_preview': '(campaign_dir: Path, manifest: Mapping[str, Any], *, repair_receipt: Path, clean_receipt: Path, reason: str) -> dict[str, Any]',
    '_environment_isolations': '(campaign_dir: Path) -> list[dict[str, Any]]',
    '_evaluation_action_kind': '(command: Sequence[str]) -> str | None',
    '_evaluation_approval_revision_locked': '(arguments: argparse.Namespace, campaign_dir: Path, campaign_root: Path, manifest: dict[str, Any], candidate_id: str, reviewer: str, approve_sha256: Any, admission: dict[str, Any] | None, ledger_dir: Path, reconciler: Any) -> dict[str, Any]',
    '_evaluation_baseline_dir': '(campaign_dir: Path, candidate_id: str, baseline: int) -> Path',
    '_evaluation_baseline_has_outputs': '(campaign_dir: Path, candidate_id: str, baseline: int) -> list[str]',
    '_evaluation_baseline_states': '(campaign_dir: Path, candidate_id: str) -> dict[int, dict[str, bool]]',
    '_evaluation_defect_admission': '(arguments: argparse.Namespace, campaign_dir: Path, manifest: Mapping[str, Any], facts: Mapping[str, Any], *, attempt_root: Path) -> dict[str, Any]',
    '_evaluation_epoch_for_recovery': '(campaign_dir: Path, manifest: Mapping[str, Any], attempt_root: Path, *, current_evidence: str) -> dict[str, Any] | None',
    '_evaluation_failure_diagnosis_facts': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, revision: int, baseline: int, *, candidate: Mapping[str, Any], attempt: Mapping[str, Any], classification: Mapping[str, Any], rules: Sequence[str]) -> dict[str, Any]',
    '_evaluation_failure_scope': '(campaign_dir: Path, candidate: Mapping[str, Any], attempt: Mapping[str, Any], evaluation_run: Mapping[str, Any], assertions_root: Path) -> tuple[dict[str, Any], str]',
    '_evaluation_recover_locked': '(arguments: argparse.Namespace, action: str, campaign_dir: Path, campaign_root: Path, manifest: dict[str, Any], candidate_id: str, reviewer: str, approve_sha256: Any, root_cause_class: Any, admission: dict[str, Any] | None, ledger_dir: Path, reconciler: Any) -> dict[str, Any]',
    '_evaluation_reevaluate_locked': '(arguments: argparse.Namespace, campaign_dir: Path, campaign_root: Path, manifest: dict[str, Any], candidate_id: str, reviewer: str, approve_sha256: Any, admission: dict[str, Any] | None, ledger_dir: Path, reconciler: Any) -> dict[str, Any]',
    '_evidence_conflict_quarantine_preview': '(campaign_dir: Path, manifest: Mapping[str, Any], *, phase: str, candidate_id: str | None, reason: str) -> dict[str, Any]',
    '_evidence_conflict_quarantines': '(campaign_dir: Path) -> list[dict[str, Any]]',
    '_evidence_root_conflicts': '(campaign_dir: Path, manifest: Mapping[str, Any], *, phase: str, candidate_id: str | None) -> list[dict[str, Any]]',
    '_evidence_root_registrants': '(campaign_dir: Path, manifest: Mapping[str, Any], *, phase: str, candidate_id: str | None, exclude_attempt_id: str) -> dict[str, list[dict[str, Any]]]',
    '_evidence_root_relocations': '(campaign_dir: Path, *, phase: str, candidate_id: str | None, attempt_id: str) -> dict[str, str]',
    '_existing_candidate_revisions': '(campaign_dir: Path) -> list[int]',
    '_failed_job_evidence_host_route': '(value: Any) -> tuple[Path, PurePosixPath]',
    '_failed_job_ids': '(results: Any) -> list[str]',
    '_fingerprint': '(payload: dict[str, Any]) -> str',
    '_formal_artifact_attempt': '(campaign_dir: Path, *, sequence: int, phase: str) -> int | None',
    '_freeze_evaluation_output_bindings': '(campaign_dir: Path, candidate_id: str, baseline: int, actions: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]',
    '_implicit_r1_candidate_id': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> str',
    '_incremental_noop_job_ids': '(attempt_root: Path, payload: Mapping[str, Any]) -> list[str]',
    '_invalidation_phase': '(summary: Mapping[str, Any], ledger_dir: Path) -> str',
    '_is_policy_v2_identity': '(identity: Mapping[str, Any]) -> bool',
    '_isolation_invalidated_attempts': '(campaign_dir: Path) -> set[tuple[str, str | None, str]]',
    '_job_execution_sha256': '(job: Job) -> str',
    '_job_recovery_root_keys': '(job: Job) -> list[str]',
    '_job_rehearsal_contract_from_manifest': '(campaign_dir: Path, manifest: Mapping[str, Any], *, target_scenario_override: Mapping[str, Any] | None=None, tool_files_sha256_override: str | None=None, recovery_rehearsal_receipt: Mapping[str, Any] | None=None, control_receipts_override: Mapping[str, Any] | None=None, _allow_historical_tool_identity: bool=False, _allow_bound_evidence_label_digest: bool=False, _require_incremental_noop_preflight_active: bool=True, _allow_stopped_metadata_only: bool=False) -> dict[str, Any]',
    '_job_tool_dependency_files': '(job: Job, tool_identity: Mapping[str, Any]) -> dict[str, str]',
    '_ledger_last_event_of_type': '(ledger_dir: Path, event_type: str) -> tuple[dict[str, Any], str] | None',
    '_ledger_last_stage_revision': '(ledger_dir: Path) -> dict[str, Any] | None',
    '_legacy_stage_path': '(campaign_dir: Path, stage: str, candidate_id: str | None=None) -> tuple[str, Path]',
    '_load_attempt_recovery_reservation': '(campaign_dir: Path, segment_root: Path, *, candidate_id: str, attempt_id: str, recovery_revision: str) -> dict[str, Any]',
    '_load_attempt_recovery_segment': '(campaign_dir: Path, candidate_id: str, attempt_id: str, recovery_revision: str) -> tuple[Path, dict[str, Any], dict[str, Any]]',
    '_load_capture_attempt': '(campaign_dir: Path, phase: str, candidate_id: str | None, attempt_id: str, *, _historical_manifest_controls: bool=False, _verified_campaign_manifest: Mapping[str, Any] | None=None, _replay_evidence_permissions: bool=True) -> tuple[Path, dict[str, Any]]',
    '_load_capture_reservation': '(campaign_dir: Path, attempt_root: Path, *, phase: str, candidate_id: str | None, _manifest: Mapping[str, Any] | None=None, _historical_manifest_controls: bool=False) -> dict[str, Any]',
    '_load_evaluation_baseline_recovery': '(campaign_dir: Path, candidate_id: str, baseline: int) -> dict[str, Any]',
    '_load_prepared_staging_attempt': '(campaign_dir: Path, attempt_dir: Path, *, owner_nonce: str | None, sequence: int, phase: str) -> dict[str, Any]',
    '_load_stage_result': '(campaign_dir: Path, stage: str, candidate_id: str | None=None, *, _import_chain: frozenset[Path] | None=None, _replay_machine_receipts: bool=True, _shallow: bool=False, _ignore_checkpoint: bool=False, _import_cache: dict[tuple[str, str, str | None], dict[str, Any]] | None=None, _skip_evidence_scan: bool=False, _historical_manifest_controls: bool=False, _verified_campaign_manifest: Mapping[str, Any] | None=None, _baseline: int | None=None) -> dict[str, Any]',
    '_locate_evaluation_failed_run': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, revision: int, baseline: int) -> dict[str, Any]',
    '_mutable_command_coordinates': '(arguments: argparse.Namespace) -> tuple[str, str, str | None, bool] | None',
    '_next_recovery_revision': '(attempt_root: Path) -> str',
    '_next_staging_attempt': '(sequence_dir: Path) -> int',
    '_optional_json_object': '(path: Path) -> dict[str, Any] | None',
    '_ordered_capture_attempts': '(campaign_dir: Path, phase: str, candidate_id: str | None, *, _historical_manifest_controls: bool=False, _manifest: Mapping[str, Any] | None=None) -> list[tuple[Path, dict[str, Any]]]',
    '_path_in_candidate_source': '(path: Path, source_root: Path, label: str) -> Path',
    '_plan_evaluator_entry_digests': '(manifest: Mapping[str, Any]) -> dict[str, str]',
    '_prepare_atomic_batch_governance': '(arguments: argparse.Namespace) -> dict[str, Any]',
    '_probe_capture_environment': '(manifest: dict[str, Any], output_dir: Path, phase: str, *, deadline: incremental_recovery.WallClockDeadline | None=None, heartbeat: Any | None=None) -> dict[str, Any]',
    '_probe_snapshot_digests': '(manifest: Mapping[str, Any]) -> dict[str, str]',
    '_project_ledger_required': '(campaign_mode: Any, target_version: Any) -> bool',
    '_publish_ledger_receipt_copies': '(ledger_dir: Path, subject: str, sources: Mapping[str, Path]) -> list[dict[str, str]]',
    '_publish_staging_file': '(source: Path, target: Path, campaign_dir: Path) -> None',
    '_read_campaign_lease': '(campaign_dir: Path) -> dict[str, Any] | None',
    '_read_candidate_revision_record': '(campaign_dir: Path, revision: int) -> tuple[dict[str, Any] | None, dict[str, Any] | None]',
    '_read_json': '(path: Path, label: str) -> dict[str, Any]',
    '_read_staging_commit': '(commit_path: Path) -> dict[str, Any]',
    '_read_stop_reason': '(run_dir: Path) -> str | None',
    '_reconcile_prepared_parent_run': '(campaign_dir: Path, run_dir: Path, state: Mapping[str, Any], *, plan: Mapping[str, Any]) -> dict[str, Any]',
    '_reconcile_staging_abort_receipt': '(campaign_dir: Path, attempt_dir: Path, *, sequence: int, phase: str, attempt: int) -> dict[str, Any]',
    '_reconcile_staging_orphans': '(campaign_dir: Path, state_dir: Path, *, plan: Mapping[str, Any]) -> dict[str, Any]',
    '_recovery_continuity_receipt_path': '(campaign_dir: Path, attempt_id: str) -> Path',
    '_recovery_preview_retry_root_cause_ids': '(preview_path: Any) -> list[str] | None',
    '_recovery_root_suffix': '(recovery_revision: str) -> str',
    '_refuse_seal_chain_on_evolution_invalidated_attempt': '(campaign_dir: Path, manifest: Mapping[str, Any], *, phase: str, action_plan: Any) -> None',
    '_refuse_vc1_seal_batch_against_bundle_state': '(manifest: Mapping[str, Any], *, phase: str, action_plan: Any) -> None',
    '_reject_contaminated_campaign': '(campaign_dir: Path, *, allow_metadata_only_repair: bool=False, attempt_root: Path | None=None, attempt: Mapping[str, Any] | None=None) -> None',
    '_replay_attempt_evidence_permissions': '(attempt_root: Path, payload: Mapping[str, Any], *, root_relocations: Mapping[str, str] | None=None) -> dict[str, Any] | None',
    '_replay_attempt_recovery_evidence_permissions': '(segment_root: Path, payload: Mapping[str, Any]) -> dict[str, Any] | None',
    '_replay_vc_checkpoint': '(campaign_dir: Path, plan: Mapping[str, Any], phase: str, *, revision: int | None=None) -> tuple[Path, dict[str, Any]]',
    '_require_candidate_revision': '(campaign_dir: Path, manifest: Mapping[str, Any], *, action: str) -> tuple[int, dict[str, Any] | None]',
    '_require_file_binding': '(value: Any, label: str) -> None',
    '_require_formal_campaign': '(campaign_dir: Path, manifest: dict[str, Any] | None=None) -> dict[str, Any]',
    '_require_recovery_preview_scope_equals_frozen_jobs': '(preview: Mapping[str, Any], execute_jobs: Sequence[str], *, label: str) -> None',
    '_require_stage_replay_proof_for_checkpoint': '(ledger_dir: Path, phase: str, checkpoint_path: Path) -> dict[str, Any]',
    '_require_tool_evolution_registered': '(campaign_dir: Path, manifest: Mapping[str, Any], *, current: Mapping[str, Any] | None=None, action: str) -> dict[str, Any] | None',
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
    '_staging_abort_accounted': '(campaign_dir: Path, sequence: int, attempt: int) -> bool',
    '_staging_attempt_dirs': '(sequence_dir: Path) -> list[tuple[int, Path]]',
    '_staging_attempt_sha256s': '(attempt_dir: Path) -> tuple[str | None, str | None]',
    '_staging_commit_path': '(campaign_dir: Path, sequence: int, phase: str) -> Path',
    '_staging_error_summary': '(error: BaseException) -> str',
    '_staging_run_arguments': '(arguments: argparse.Namespace) -> argparse.Namespace',
    '_staging_sequence_dir': '(campaign_dir: Path, sequence: int, phase: str) -> Path',
    '_supervisor_run_reconciled': '(campaign_dir: Path, run_dir: Path) -> bool',
    '_timing_event_phase': '(ledger_dir: Path, sequence: int) -> str',
    '_timing_ledger_batch_events': '(campaign_dir: Path, manifest: Mapping[str, Any], plan: Mapping[str, Any], *, phase: str, ledger_dir: Path) -> tuple[int, list[tuple[str, str]]]',
    '_tool_evolution_preview': '(campaign_dir: Path, manifest: dict[str, Any], *, fix_commit: str, reason: str, control_root: Path | None, compatibility_receipt: Path | None=None, activation_certification: Path | None=None) -> dict[str, Any]',
    '_tool_evolution_quiescence_problems': '(campaign_dir: Path) -> list[str]',
    '_tool_evolution_unregistered_drift': '(effective_identity: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]',
    '_tool_identity': '(*, include_git: bool=True) -> dict[str, Any]',
    '_tool_tree_entries': '(tool_root: Path) -> list[dict[str, Any]]',
    '_transient_environment_admission': '(campaign_dir: Path, manifest: Mapping[str, Any], facts: Mapping[str, Any], *, attempt_root: Path, attempt: Mapping[str, Any]) -> dict[str, Any]',
    '_utc_now': '() -> str',
    '_vc_campaign_plan': '(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]',
    '_vc_checkpoint_path': '(campaign_dir: Path, phase: str, *, revision: int | None=None) -> Path',
    '_vc_checkpoint_reference': '(campaign_dir: Path, path: Path, checkpoint: Mapping[str, Any]) -> dict[str, str]',
    '_vc_run_manifest_from_batch': "(batch: Mapping[str, Any], *, batch_model: str='legacy') -> dict[str, Any]",
    '_verify_candidate_attempt_identity': '(manifest: dict[str, Any], identity: dict[str, Any]) -> None',
    '_verify_current_deployment_receipt': '(campaign_dir: Path, manifest: Mapping[str, Any], receipt_path: Path) -> dict[str, Any]',
    '_verify_evaluator_digests_authorized': '(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, baseline: int, digests: Mapping[str, Any], *, label: str) -> None',
    '_verify_execution_tree': '(capture_root: Path | None) -> None',
    '_verify_failed_run_reconciled': '(campaign_dir: Path, manifest: Mapping[str, Any], ledger_dir: Path, failed: Mapping[str, Any], *, head: Mapping[str, Any]) -> dict[str, Any]',
    '_verify_official_binaries': '(manifest: dict[str, Any], *, deadline: incremental_recovery.WallClockDeadline | None=None, heartbeat: Any | None=None) -> dict[str, Any]',
    '_wait_for_monitor_finalization': '(run_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]',
    '_write_candidate_revision_pending': '(campaign_dir: Path, manifest: Mapping[str, Any], *, revision: int, candidate_id: str, supersedes: Mapping[str, Any] | None) -> tuple[dict[str, Any], dict[str, Any]]',
    '_write_or_verify_json_ignoring': '(path: Path, payload: Mapping[str, Any], *, volatile: Iterable[str]) -> None',
    '_write_staging_abort': '(attempt_dir: Path, *, campaign_id: str, campaign_plan_sha256: str, phase: str, sequence: int, attempt: int, stage: str, failure_kind: str, error_type: str, root_cause_id: str, batch_sha256: str | None, manifest_sha256: str | None, parent_run_dir: str | None, parent_run_state: str | None, reconciliation_receipt: Mapping[str, Any] | None, error_message: str | None=None, error_signature: str | None=None) -> dict[str, Any]',
    '_write_staging_batch_artifacts': '(campaign_dir: Path, attempt_dir: Path, *, plan: Mapping[str, Any], batch: Mapping[str, Any], run_manifest: Mapping[str, Any], owner_nonce: str, prepared_at_utc: str) -> dict[str, Any]',
    'campaign_status': '(campaign_dir: Path, candidate_id: str | None=None) -> dict[str, Any]',
    'compile_and_run_vc_batch': '(arguments: argparse.Namespace, *, _compiler: Any | None=None) -> tuple[dict[str, Any], int]',
    'compile_vc_batch': '(arguments: argparse.Namespace, *, staging_attempt_dir: Path | None=None, owner_nonce: str | None=None) -> dict[str, Any]',
    'create_campaign': '(arguments: argparse.Namespace) -> dict[str, Any]',
    'file_sha256': None,
    'load_campaign_manifest': '(path: Path, *, _control_epoch_bootstrap: bool=False, _skip_control_validation: bool=False) -> dict[str, Any]',
    'recovery_preview_continuity': '(campaign_dir: Path, *, manifest: Mapping[str, Any], phase: str, candidate_id: str | None, source_attempt_id: str, output_dir: Path) -> dict[str, Any]',
    'recovery_reuse_check': '(campaign_dir: Path, *, phase: str, candidate_id: str | None, source_attempt_id: str, reuse_job_ids: Iterable[str], execute_job_ids: Iterable[str]) -> dict[str, Any]',
})

# 默认后继协议顺序保留监督器原 tuple 对象；只有客户端明确登记完整名字顺序时才在已核归属后调整。
DEFAULT_SUCCESSOR_PROTOCOL_ORDER = None

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
    # 对账器判定 attempt 环境已恢复的口径（B2-08）：两客户端的恢复证据不同——Codex 是 after 探针加恢复收据，Claude 是
    # after 探针在位且前后探针无漂移（中继逐步恢复 hosts 并停止，没有单独的恢复收据）。
    "attempt_environment_restored": "callable",
    # 部署收据里工具五摘要所在的对象（B2-08）：Codex 部署收据在收据顶层，Claude 受管部署收据（official-client-managed-deploy/v1）
    # 在 ``identity`` 子键里；对账器按它比对当前工具身份（原写死为读收据顶层）。
    "deploy_receipt_identity": "callable",
    # Campaign 当前有效 wire 身份（B2-08）：Codex 由 wire transition 与工具演进两条链推出（共享原函数逐字不变）；Claude 只有
    # 工具演进链，链上策略演进的策略文件按 Claude 受管条目键核对（共享链校验在 Codex 评估读侧闭包内，不改）。
    "effective_wire_identity": "callable",
    # 项目总账（B1-04）：两客户端共用数据根、各有一本总账（设计 D3），新建总账用的 plan 合同不同（附录 B），
    # 候选就绪探针的请求计量类别不同。
    "project_ledger_dir_name": "value",  # 数据根下的项目总账目录名
    "project_ledger_plan_schema": "value",  # 新建项目总账写出的 plan 合同（Codex v1 不带 client，Claude v2 带 client）
    "candidate_probe_accounting_category": "value",  # 候选就绪探针在项目总账里的请求计量类别
    # 完整 Job 离线演练（B2-10）：共享流程（增量计划、checkpoint、逐 Job 执行与复用、收据封存与重放）在
    # codex_upgrade_job_rehearsal_receipt；执行合同的构造、要核对的二进制表与合同重放是完整接口（Claude 总计划没有 Codex
    # 的官方包与 code-mode-host 字段，只换名称过不了原构造器），其余客户端差异（合同名组、容器与依赖集合、执行副本坐标、
    # 运行时探针、步骤启动器、Formal 必需的探针摘要）是画像模块。
    "job_rehearsal_profile": "module",
    "build_job_contract": "callable",  # (manifest, target_scenario, extra_jobs) → 完整执行合同
    "binary_facts": "callable",  # (contract) → 宿主与容器里要核对的二进制标签与期望摘要表
    "job_contract_replay": "callable",  # (contract, facts) → 按执行合同核对 facts 里的二进制事实
    # VC-0 收口（B2-11）：共享流程在 codex_upgrade_vc0_closeout（现场判定、收口 attempt、收据副本、建 Formal、首批派发与
    # 续作）；客户端差异收进收口画像模块——合同名（收口收据、失败诊断、现场、上限后恢复预览、首批交接）、四份输入的校验
    # ``validate_inputs``、从预检恢复 Formal plan 参数的 ``recover_formal_plan_arguments`` 与命令提示里的编排器模块名。
    "closeout_profile": "module",
    # 原子双跑（B5a-09 续）：共享流程在 codex_upgrade_campaign_run_rehearsal_receipt（两个独立新根里经原子入口完成 VC-0→VC-1、
    # 权限收口、失败父 run 的账本收口与负例，收据封存与重放）；客户端差异收进画像模块——合同名（收据、动作事实、VC-0 阶段
    # 事实）、合成 Campaign 的版本与归属字段、父 run 队列清单合同、动作命令的客户端参数、工具身份额外绑定的文件、失败父 run 的
    # 清单形状。
    "atomic_rehearsal_profile": "module",
    # VC-1 官方零请求后处理（修好接着跑：VC-1 续跑路径）：监督器 post-run-tooling 判据与 seal 链续派协议认的阶段项闭集
    # （Codex 是断言包、seal 预览与 seal 批准三项，Claude 是封存批次的静态分析、seal 预览与 seal 批准三项），以及
    # (action, campaign_dir) → 该 VC-1 动作是否不绑定 attempt 的零请求后处理动作（Codex 没有，恒为假；Claude 只有命令
    # 形态精确闭合的静态分析）。
    "official_post_run_tooling_item_ids": "value",
    "official_attempt_free_post_run_action": "callable",
    # 候选级（VC-5／VC-6）零请求后处理的额外动作（B4-07 收尾）：(run_manifest, action) → 该动作是否是本客户端额外认定的
    # 候选级零请求后处理动作。监督器 ``candidate_post_run_recovery_action`` 在共享闭集（candidate-seal、compare、acceptance、
    # assert-*、canonical 各步）之外按它放行，失败后进 recovery_required、对账后逐字重派。Codex 没有，恒为假（判定逐字不变）；
    # Claude 只认 VC-6 两个交付动作（deliver-candidate run／finalize），按动作 ID、项名与命令形态精确闭合。
    "candidate_post_run_extra_action": "callable",
    # VC-5 准入（B5b-01）：驱动 ``vc5_admission`` 的两项受管事实，参数一律按关键字给出。
    # ``catalog_stage_profile(campaign_dir, *, campaign_id, candidate_id, target_version, profile_id, candidate_source,
    # lifecycle_dir, candidate_commit)``：VC-3 暂存画像与 VC-4 候选构建收据的重放，返回两客户端同一组字段（profile_bytes、
    # profile_sha256、profile_digest、profile_id、vc3_receipt_sha256、build_receipt_sha256、build_id、image_id；Claude 另带
    # extra_bindings 并入准入绑定）。Codex 是驱动原 ``ManagedFacts.catalog`` 的函数体（候选用途照旧写死），Claude 按
    # catalogdata/claude 的画像合同、候选用途取冻结的总计划。
    # ``vc5_official_campaign(data_root, *, campaign_id, target_version, evidence_decision, predecessor_campaign)``：官方
    # 证据所在的 Campaign 与阻断原因（证据决定为 reuse 时核对总计划冻结的前序）。
    # 事实不成立一律抛 ``Vc5AdmissionFactError``（静态错误码）。
    "catalog_stage_profile": "callable",
    "vc5_official_campaign": "callable",
    # VC-5 候选采集续跑（B4-05b-1）：监督器续跑后继协议与驱动生成续跑命令时，候选续跑命令逐字携带的身份参数
    # （``codex_upgrade_supervisor.candidate_recovery_identity_flags``）。Codex 是监督器原常量（十个身份旗标，原位不动）；
    # Claude 的候选身份只来自 VC-4 构建收据，只有 ``--candidate-id``。
    "candidate_recovery_identity_flags": "value",
    # 后继协议顺序：None 沿用监督器原 tuple；显式值必须是全部既有协议名的无重复 tuple，不能由清单或 CLI 注入。
    "successor_protocol_order": "value",
    # 跨基线恢复段承接：调用方必须已持有 Campaign 锁，且旧段未通过 terminal_failed。
    # (campaign_dir, *, manifest, candidate_id, attempt_id, segment_root, baseline, baseline_commit_sha256,
    # recovery) → 旧成功段是否属于当前 COMMIT 前序链，且原计划／执行、完整 seal 与实际入账均可信。
    # Codex 恒为假，保留原成功段拒绝语义；Claude 只允许更早基线的成功段，不允许当前基线重开。
    "sealed_predecessor_segment": "callable",
    # compare 收据是否绑定写出时的评估基线：Claude 收据与 builder 路径都绑定当前基线，开新基线必须重新 compare。
    # Codex 沿用原判据，仅 transient、compare-reader 变化或比较尚未完成时才将 compare 标成 local。
    "comparison_is_baseline_bound": "value",
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
        "attempt_environment_restored": ("codex_upgrade_client_adapter", "attempt_environment_restored"),
        "deploy_receipt_identity": ("codex_upgrade_client_adapter", "deploy_receipt_identity"),
        "effective_wire_identity": ("codex_upgrade_wire_transition", "effective_wire_identity"),
        "project_ledger_dir_name": ("codex_upgrade_project_ledger", "LEDGER_DIR_NAME"),
        "project_ledger_plan_schema": ("codex_upgrade_project_ledger", "PLAN_SCHEMA"),
        "candidate_probe_accounting_category": ("codex_upgrade_client_adapter", "CANDIDATE_PROBE_ACCOUNTING_CATEGORY"),
        # B2-10：Codex 画像就是共享模块本身；三个合同接口的实现是改造前写在采集与校验流程里的原函数体。
        "job_rehearsal_profile": ("codex_upgrade_job_rehearsal_receipt", None),
        "build_job_contract": ("codex_upgrade_job_rehearsal_receipt", "build_codex_job_contract"),
        "binary_facts": ("codex_upgrade_job_rehearsal_receipt", "codex_binary_facts"),
        "job_contract_replay": ("codex_upgrade_job_rehearsal_receipt", "codex_job_contract_replay"),
        # B2-11：Codex 收口画像就是收口模块本身（合同名与两个函数是改造前的原值与原函数）。
        "closeout_profile": ("codex_upgrade_vc0_closeout", None),
        # B5a-09 续：Codex 原子双跑画像就是共享模块本身（合同名与取值是改造前写死的原值）。
        "atomic_rehearsal_profile": ("codex_upgrade_campaign_run_rehearsal_receipt", None),
        # VC-1 续跑路径：Codex 的阶段项闭集就是监督器原常量（原位不动），不绑定 attempt 的动作判定恒为假。
        "official_post_run_tooling_item_ids": ("codex_upgrade_supervisor", "OFFICIAL_POST_RUN_TOOLING_ITEM_IDS"),
        "official_attempt_free_post_run_action": ("codex_upgrade_client_adapter", "official_attempt_free_post_run_action"),
        # B4-07 收尾：Codex 没有共享闭集之外的候选级零请求后处理动作，恒为假。
        "candidate_post_run_extra_action": ("codex_upgrade_client_adapter", "candidate_post_run_extra_action"),
        # B5b-01：VC-5 准入两项事实，函数体是驱动原写在 vc5_admission 里的 Codex 逻辑，原样搬入。
        "catalog_stage_profile": ("codex_upgrade_client_adapter", "catalog_stage_profile"),
        "vc5_official_campaign": ("codex_upgrade_client_adapter", "vc5_official_campaign"),
        # B4-05b-1：Codex 的候选续跑身份参数就是监督器原常量。
        "sealed_predecessor_segment": ("codex_upgrade_client_adapter", "sealed_predecessor_segment"),
        "candidate_recovery_identity_flags": ("codex_upgrade_supervisor", "CANDIDATE_RECOVERY_IDENTITY_FLAGS"),
        "successor_protocol_order": ("upgrade_client_adapter", "DEFAULT_SUCCESSOR_PROTOCOL_ORDER"),
        "comparison_is_baseline_bound": ("codex_upgrade_client_adapter", "COMPARISON_IS_BASELINE_BOUND"),
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
        "attempt_environment_restored": ("claude_upgrade_client_adapter", "attempt_environment_restored"),  # B2-08
        "deploy_receipt_identity": ("claude_upgrade_client_adapter", "deploy_receipt_identity"),  # B2-08
        "effective_wire_identity": ("claude_upgrade_evolution", "effective_wire_identity"),  # B2-08
        "project_ledger_dir_name": ("claude_upgrade_client_adapter", "PROJECT_LEDGER_DIR_NAME"),
        # v2 合同由项目总账定义与校验，Claude 只是选用它（共享 control 模块，不落到 Codex 编排器）。
        "project_ledger_plan_schema": ("codex_upgrade_project_ledger", "PLAN_SCHEMA_V2"),
        "candidate_probe_accounting_category": ("claude_upgrade_client_adapter", "CANDIDATE_PROBE_ACCOUNTING_CATEGORY"),
        "job_rehearsal_profile": ("claude_upgrade_job_rehearsal", None),  # B2-10
        "build_job_contract": ("claude_upgrade_job_rehearsal", "build_job_contract"),  # B2-10
        "binary_facts": ("claude_upgrade_job_rehearsal", "binary_facts"),  # B2-10
        "job_contract_replay": ("claude_upgrade_job_rehearsal", "job_contract_replay"),  # B2-10
        "closeout_profile": ("claude_upgrade_vc0_closeout", None),  # B2-11
        "atomic_rehearsal_profile": ("claude_upgrade_atomic_rehearsal", None),  # B5a-09 续
        "official_post_run_tooling_item_ids": ("claude_upgrade_client_adapter", "OFFICIAL_POST_RUN_TOOLING_ITEM_IDS"),  # VC-1 续跑路径
        "official_attempt_free_post_run_action": ("claude_upgrade_client_adapter", "official_attempt_free_post_run_action"),  # VC-1 续跑路径
        "candidate_post_run_extra_action": ("claude_upgrade_client_adapter", "candidate_post_run_extra_action"),  # B4-07 收尾
        "catalog_stage_profile": ("claude_upgrade_vc5_admission", "catalog_stage_profile"),  # B5b-01
        "vc5_official_campaign": ("claude_upgrade_vc5_admission", "vc5_official_campaign"),  # B5b-01
        "sealed_predecessor_segment": ("claude_upgrade_recovery_history", "sealed_predecessor_segment"),
        "candidate_recovery_identity_flags": ("claude_upgrade_client_adapter", "CANDIDATE_RECOVERY_IDENTITY_FLAGS"),  # B4-05b-1
        "successor_protocol_order": ("claude_upgrade_client_adapter", "SUCCESSOR_PROTOCOL_ORDER"),
        "comparison_is_baseline_bound": ("claude_upgrade_client_adapter", "COMPARISON_IS_BASELINE_BOUND"),
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
        if context.manifest is None or context.output_dir is None:
            raise ClientAdapterError("recovery_preview_continuity 需要 RecoveryContext.manifest 与 output_dir（探针输出目录）")
        return self.orchestrator.recovery_preview_continuity(
            context.campaign_dir,
            manifest=context.manifest,
            phase=context.phase,
            candidate_id=context.candidate.candidate_id,
            source_attempt_id=context.source_attempt_id,
            output_dir=context.output_dir,
        )

    def attempt_evolution_impact(self, context: RecoveryContext) -> dict[str, Any]:
        if context.manifest is None or context.attempt_root is None:
            raise ClientAdapterError("attempt_evolution_impact 需要 RecoveryContext.manifest 与 attempt_root")
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
