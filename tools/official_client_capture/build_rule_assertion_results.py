#!/usr/bin/env python3
"""按验收契约编排逐规则断言，汇总为 accept 所需的 v2 验收结果文档。

主手册 §4.5.2 验收模型规定：每条规则的 ``validation_mode`` 由冻结验收契约
（``acceptance_contract.py``）从批准断言画像机器推导，禁止手写——

- ``dual_wire``（25 条 wire 规则）：官方／候选两侧各执行一次单规则断言，
  双侧 check 集合必须与契约 check 全集逐项一致；
- ``candidate_profile``（17 条内部规则）：只在候选侧执行机器断言；官方权威
  是批准画像链，行内逐字绑定批准断言画像 SHA-256、classification package
  digest 与联合 ``review_sha256``，不再伪造官方侧机器结果。

v1 的人工 ``positive_assertions``／``negative_assertions`` 已废除：accept 从
批准画像复算应有 check ID 并离线重放，正负语义由画像判据本身表达。

evidence refs 以 ``<evidence_prefix>/<相对路径>`` 的 inventory 逻辑路径写入，
按主手册 §4.4.3 统一路径空间；accept 端只做精确路径＋摘要匹配。任一侧断言
失败即整体失败：schema 只接受 ``status: "pass"``，把失败规则写进文档等于
伪造验收结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.candidate_rule_assertion import (  # noqa: E402
    build_assertion_command,
    canonical_projection_bytes,
    command_sha256,
    project_capture_manifest,
)
from tools.official_client_capture.candidate_rule_assertion import (  # noqa: E402
    _load_json as _load_checker_json,
)
from tools.official_client_capture.candidate_rule_assertion import (  # noqa: E402
    load_profile as load_checker_profile,
)
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts  # noqa: E402
from tools.official_client_capture.acceptance_contract import (  # noqa: E402
    AcceptanceContractError,
    MODE_CANDIDATE_PROFILE,
    MODE_DUAL_WIRE,
    RESULTS_SCHEMA_V2,
    build_contract_payload,
    contract_sha256,
    expected_check_ids_for_side,
    load_profile,
)

SINGLE_SCHEMA = "codex-candidate-rule-assertion/v1"
AUTHORITY_FIELDS = (
    "assertion_profile_sha256",
    "classification_package_digest",
    "review_sha256",
)


class RuleAssertionError(RuntimeError):
    """断言编排不足以支撑验收结论。"""


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _binding(path: Path, root: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": _file_sha256(path),
    }


def assertions_root_for_baseline(campaign_root: Path, candidate_id: str, baseline: int) -> Path:
    """改造 5：b0 为 ``assertions/<cid>/``，b≥1 为 ``assertions/<cid>/revisions/b<K>/``。"""

    root = campaign_root / "assertions" / candidate_id
    if baseline > 0:
        root = root / "revisions" / f"b{baseline}"
    return root


def resolve_machine_layout(
    config: Mapping[str, Any], results_dir: Path, evaluation_baseline: int = 0
) -> tuple[Path, Path, Path]:
    """解析机器结果落位与 Campaign 逻辑路径根。

    未声明 ``campaign_dir`` 时保留旧的平铺布局，供独立离线编排使用。正式
    Campaign 必须声明该字段，机器结果随即严格落在 compare 已冻结的
    ``assertions/<candidate-id>[/revisions/b<K>]/machine/{official,candidate}/``，且收据路径
    相对 Campaign 根绑定，确保 builder 输出可被 accept 逐字重放。
    """

    campaign_value = config.get("campaign_dir")
    if campaign_value is None:
        return results_dir, results_dir, results_dir
    if not isinstance(campaign_value, str) or not Path(campaign_value).is_absolute():
        raise RuleAssertionError("campaign_dir 必须是绝对路径")
    campaign_root = Path(campaign_value).resolve(strict=True)
    candidate_id = config.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id:
        raise RuleAssertionError("正式 Campaign 布局缺少 candidate_id")
    expected_root = assertions_root_for_baseline(campaign_root, candidate_id, evaluation_baseline) / "machine"
    if results_dir.resolve() != expected_root.resolve():
        raise RuleAssertionError(
            "results-dir 必须等于 Campaign 的 assertions/<candidate-id>[/revisions/b<K>]/machine"
        )
    official_dir = results_dir / "official"
    candidate_dir = results_dir / "candidate"
    official_dir.mkdir(parents=True, exist_ok=True)
    candidate_dir.mkdir(parents=True, exist_ok=True)
    return campaign_root, official_dir, candidate_dir


def run_side_assertion(
    *,
    rule_id: str,
    capture_manifest: Path,
    evidence_root: Path,
    output: Path,
    profile: Path,
    rule_manifest: Path,
    expected_codex_version: str,
    expected_profile_sha256: str,
    side: str,
    capture_manifest_projection: Path | None = None,
    allow_fail: bool = False,
) -> tuple[list[str], dict[str, Any]]:
    """执行单规则断言并返回**与 accept 期望逐字一致**的命令。

    命令必须由 `candidate_rule_assertion.build_assertion_command` 这一权威
    构造器产出：accept 用同一构造器复算期望命令并逐元素比对，编排器另造一套
    参数形态（解释器路径、executor 绝对路径、缺 profile／rule-manifest）会让
    结果文档永远无法通过 accept——这是 builder → accept 此前未集成的表现之一。

    改造 5：``capture_manifest_projection`` 给出时以投影模式执行；``allow_fail`` 为真时
    ``status=fail`` 不抛错而是把文档原样返回（由 checkpoint 记录 fail，批次结束再非零退出）。
    """

    command = build_assertion_command(
        rule_id=rule_id,
        capture_manifest=str(capture_manifest),
        evidence_root=str(evidence_root),
        profile=str(profile),
        rule_manifest=str(rule_manifest),
        expected_codex_version=expected_codex_version,
        expected_profile_sha256=expected_profile_sha256,
        side=side,
        capture_manifest_projection=(
            str(capture_manifest_projection) if capture_manifest_projection is not None else None
        ),
        output=str(output),
    )
    # 命令里的 checker 是仓库相对路径，执行时在仓库根解析，产出的命令保持原样。
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        cwd=Path(__file__).resolve().parents[2],
    )
    if completed.returncode != 0 and not (allow_fail and completed.returncode == 1 and output.is_file()):
        raise RuleAssertionError(
            f"{rule_id} 的断言执行失败（退出码 {completed.returncode}）"
        )
    if not output.is_file():
        raise RuleAssertionError(f"{rule_id} 的断言未产出结果文件")
    document = json.loads(output.read_text(encoding="utf-8"))
    if document.get("schema_version") != SINGLE_SCHEMA:
        raise RuleAssertionError(f"{rule_id} 的断言结果 schema 不受支持")
    if document.get("rule_id") != rule_id:
        raise RuleAssertionError(f"{rule_id} 的断言结果规则标识不一致")
    if document.get("status") != "pass" and not allow_fail:
        raise RuleAssertionError(
            f"{rule_id} 的断言未通过：{document.get('status')}"
        )
    if document.get("status") not in {"pass", "fail"}:
        raise RuleAssertionError(f"{rule_id} 的断言结果 status 非法：{document.get('status')}")
    return command, document


def verify_check_closure(
    document: Mapping[str, Any],
    expected_check_ids: list[str],
    *,
    rule_id: str,
    label: str,
) -> None:
    """机器结果的 check ID 必须与契约复算的全集逐项一致且全部通过。"""

    checks = document.get("checks") or []
    seen: list[str] = []
    for check in checks:
        check_id = check.get("id")
        if not isinstance(check_id, str) or not check_id:
            raise RuleAssertionError(f"{rule_id} {label}存在缺少 id 的 check")
        if check.get("passed") is not True:
            raise RuleAssertionError(
                f"{rule_id} {label}存在未通过 check：{check_id}"
            )
        seen.append(check_id)
    if sorted(seen) != sorted(expected_check_ids) or len(seen) != len(set(seen)):
        raise RuleAssertionError(
            f"{rule_id} {label}check 集合与验收契约不一致："
            f"实际 {sorted(seen)}，应有 {sorted(expected_check_ids)}"
        )


def collect_evidence_bindings(
    document: Mapping[str, Any],
    evidence_root: Path,
    evidence_prefix: str,
) -> list[dict[str, str]]:
    """把 check 引用的证据绑定为 inventory 逻辑路径＋sha256，去重排序。"""

    if not isinstance(evidence_prefix, str) or not evidence_prefix.strip():
        raise RuleAssertionError("evidence_prefix 不能为空")
    seen: dict[str, dict[str, str]] = {}
    for check in document.get("checks") or []:
        for reference in check.get("evidence_paths") or []:
            if not isinstance(reference, str) or not reference:
                raise RuleAssertionError("check 的 evidence_paths 含空引用")
            path = evidence_root / reference
            if not path.is_file() or path.is_symlink():
                raise RuleAssertionError(f"断言引用的证据不存在：{reference}")
            logical = f"{evidence_prefix}/{reference}"
            seen[logical] = {
                "path": logical,
                "sha256": _file_sha256(path),
            }
    if not seen:
        raise RuleAssertionError("断言结果未绑定任何原始证据")
    return [seen[key] for key in sorted(seen)]


def validate_official_authority(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(AUTHORITY_FIELDS):
        raise RuleAssertionError(
            "official_authority 必须且只含批准画像链三摘要"
        )
    authority: dict[str, str] = {}
    for field in AUTHORITY_FIELDS:
        digest = value.get(field)
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(ch not in "0123456789abcdef" for ch in digest)
        ):
            raise RuleAssertionError(f"official_authority.{field} 必须是 SHA-256")
        authority[field] = digest
    return authority


def build_results_document(
    *,
    candidate_id: str,
    target_version: str,
    profile_id: str,
    profile_digest: str,
    official_package_digest: str,
    candidate_package_digest: str,
    comparison_package_digest: str,
    acceptance_contract_sha256_value: str,
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    if not rules:
        raise RuleAssertionError("验收结果必须至少覆盖一条规则")
    return {
        "schema_version": RESULTS_SCHEMA_V2,
        "document_kind": "results",
        "candidate_id": candidate_id,
        "target_version": target_version,
        "profile_id": profile_id,
        "profile_digest": profile_digest,
        "official_package_digest": official_package_digest,
        "candidate_package_digest": candidate_package_digest,
        "comparison_package_digest": comparison_package_digest,
        "acceptance_contract_sha256": acceptance_contract_sha256_value,
        "rules": sorted(rules, key=lambda item: item["rule"]),
    }


def build_dual_wire_result(
    *,
    rule_id: str,
    official_expected_check_ids: list[str],
    candidate_expected_check_ids: list[str],
    official: tuple[list[str], dict[str, Any], Path],
    candidate: tuple[list[str], dict[str, Any], Path],
    official_root: Path,
    candidate_root: Path,
    official_prefix: str,
    candidate_prefix: str,
    results_root: Path,
    rationale: str,
) -> dict[str, Any]:
    official_command, official_document, official_output = official
    candidate_command, candidate_document, candidate_output = candidate
    verify_check_closure(
        official_document, official_expected_check_ids, rule_id=rule_id, label="官方"
    )
    verify_check_closure(
        candidate_document, candidate_expected_check_ids, rule_id=rule_id, label="候选"
    )
    return {
        "rule": rule_id,
        "validation_mode": MODE_DUAL_WIRE,
        "status": "pass",
        "official_evidence_refs": collect_evidence_bindings(
            official_document, official_root, official_prefix
        ),
        "candidate_evidence_refs": collect_evidence_bindings(
            candidate_document, candidate_root, candidate_prefix
        ),
        "official_machine_result": _binding(official_output, results_root),
        "candidate_machine_result": _binding(candidate_output, results_root),
        "official_command": official_command,
        "candidate_command": candidate_command,
        "evidence_level": "full",
        "rationale": rationale,
    }


def build_candidate_profile_result(
    *,
    rule_id: str,
    expected_check_ids: list[str],
    candidate: tuple[list[str], dict[str, Any], Path],
    candidate_root: Path,
    candidate_prefix: str,
    official_authority: dict[str, str],
    results_root: Path,
    rationale: str,
) -> dict[str, Any]:
    candidate_command, candidate_document, candidate_output = candidate
    verify_check_closure(
        candidate_document, expected_check_ids, rule_id=rule_id, label="候选"
    )
    return {
        "rule": rule_id,
        "validation_mode": MODE_CANDIDATE_PROFILE,
        "status": "pass",
        "official_authority": dict(official_authority),
        "candidate_evidence_refs": collect_evidence_bindings(
            candidate_document, candidate_root, candidate_prefix
        ),
        "candidate_machine_result": _binding(candidate_output, results_root),
        "candidate_command": candidate_command,
        "evidence_level": "full",
        "rationale": rationale,
    }


# ---------------------------------------------------------------------------
# 改造 5：逐规则逐侧 write-once checkpoint、投影输入、evaluation-run 索引与引用式复用
# ---------------------------------------------------------------------------

RUN_DIR_ENV = "CODEX_UPGRADE_CAMPAIGN_RUN_DIR"
OWNER_NONCE_ENV = "CODEX_UPGRADE_CAMPAIGN_OWNER_NONCE"
CHECKPOINTS_DIRNAME = "checkpoints"
EVALUATION_RUN_FILENAME = "evaluation-run.json"
INPUT_SUFFIX = "-input.json"
EVALUATOR_DIGEST_FIELDS = (
    "checker_sha256",
    "builder_sha256",
    "compare_reader_sha256",
    "accept_reader_sha256",
)


def _canonical_sha256(value: Any) -> str:
    """与全部 VC 控制制品同一 canonical JSON 摘要（vc_artifacts.digest：含唯一末尾换行）。"""

    return vc_artifacts.digest(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_once_bytes(path: Path, payload: bytes, label: str) -> None:
    """write-once：已存在时字节必须逐字相等，否则失败关闭；不覆盖。"""

    if path.is_symlink():
        raise RuleAssertionError(f"{label} 不得是符号链接：{path}")
    if path.exists():
        if path.read_bytes() != payload:
            raise RuleAssertionError(f"{label} 已存在且内容不同，禁止覆盖：{path}")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _write_once_json(path: Path, payload: Mapping[str, Any], label: str) -> None:
    _write_once_bytes(path, vc_artifacts.canonical_bytes(payload), label)


def parent_run_binding() -> dict[str, str]:
    """父监督器身份：checkpoint 只能由 campaign-run 动作内的受管 builder 写。"""

    run_dir_value = os.environ.get(RUN_DIR_ENV)
    owner_nonce = os.environ.get(OWNER_NONCE_ENV)
    if not run_dir_value or not owner_nonce:
        raise RuleAssertionError(
            "正式 Campaign 布局的断言 builder 必须由 campaign-run 父监督器派发"
            f"（缺少 {RUN_DIR_ENV}／{OWNER_NONCE_ENV}）"
        )
    run_dir = Path(run_dir_value)
    record_path = run_dir / "campaign-run-manifest.json"
    if not run_dir.is_absolute() or record_path.is_symlink() or not record_path.is_file():
        raise RuleAssertionError("父监督器 run 目录缺少 campaign-run-manifest.json")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    manifest_sha256 = record.get("manifest_sha256")
    inner = record.get("manifest")
    if (
        not isinstance(manifest_sha256, str)
        or len(manifest_sha256) != 64
        or not isinstance(inner, Mapping)
        or _canonical_sha256(inner) != manifest_sha256
    ):
        raise RuleAssertionError("父监督器 run 清单摘要不一致")
    return {
        "run_dir": str(run_dir),
        "owner_nonce": owner_nonce,
        "run_manifest_sha256": manifest_sha256,
        "inner_manifest": inner,
    }


def evaluator_digests_for_run(inner_manifest: Mapping[str, Any]) -> dict[str, str]:
    """evaluator 四项摘要：批次冻结值与当前受管树互校，任一项不等即失败关闭。

    冻结值（batch v3 ``evaluator_digests``）是逐规则依赖摘要与复用判据的唯一 checker／builder
    口径，但 builder 自己必须重算当前树（``evaluator_dependency_digests()``：checker／builder 整文件
    摘要与 compare／accept 读侧闭包）并逐项核对——父监督器在动作执行前与 COMMIT 前的核对
    不能代替 builder 的自证，否则冻结值与实际执行的 checker 可以脱钩。无冻结值（非 v3 批次）
    时以当前受管树为口径。
    """

    from tools.official_client_capture import codex_upgrade_tool_identity_policy as policy_module

    current = {field: str(value) for field, value in policy_module.evaluator_dependency_digests().items()}
    frozen = inner_manifest.get("evaluator_digests")
    if not isinstance(frozen, Mapping) or set(frozen) != set(EVALUATOR_DIGEST_FIELDS):
        return current
    drift = sorted(field for field in EVALUATOR_DIGEST_FIELDS if str(frozen[field]) != current[field])
    if drift:
        raise RuleAssertionError(
            "批次冻结的 evaluator 摘要与当前受管树不一致，builder 拒绝执行："
            + "、".join(f"{field} 冻结 {str(frozen[field])[:12]} 当前 {current[field][:12]}" for field in drift)
        )
    return {field: str(frozen[field]) for field in EVALUATOR_DIGEST_FIELDS}


class EvaluationRun:
    """一次评估运行（正式 Campaign 布局）的 checkpoint 链、投影输入与复用判定。"""

    def __init__(
        self,
        *,
        campaign_root: Path,
        candidate_id: str,
        baseline: int,
        assertions_root: Path,
        profile: Mapping[str, Any],
        expected_profile_sha256: str,
        rule_manifest_path: Path,
        reuse_from: Path | None,
        reuse_authority: str,
    ) -> None:
        self.campaign_root = campaign_root
        self.candidate_id = candidate_id
        self.baseline = baseline
        self.assertions_root = assertions_root
        self.checkpoints_dir = assertions_root / CHECKPOINTS_DIRNAME
        self.profile = profile
        self.expected_profile_sha256 = expected_profile_sha256
        self.rule_manifest_sha256 = _file_sha256(rule_manifest_path)
        binding = parent_run_binding()
        inner = binding.pop("inner_manifest")
        self.parent = binding
        self.inner_manifest = inner
        self.campaign_id = str(inner.get("campaign_id", ""))
        self.candidate_revision = inner.get("candidate_revision")
        if inner.get("candidate_id") != candidate_id:
            raise RuleAssertionError("父监督器清单绑定的候选与 builder 配置不一致")
        frozen_baseline = inner.get("evaluation_baseline")
        if (frozen_baseline or 0) != baseline:
            raise RuleAssertionError(
                f"父监督器清单冻结的评估基线 b{frozen_baseline or 0} 与 --evaluation-baseline b{baseline} 不一致"
            )
        self.evaluator = evaluator_digests_for_run(inner)
        self.checker_sha256 = self.evaluator["checker_sha256"]
        self.builder_sha256 = self.evaluator["builder_sha256"]
        self.reuse_authority = reuse_authority
        self.reuse_index: dict[str, dict[str, Any]] = {}
        self.reuse_run: dict[str, Any] | None = None
        if reuse_from is not None:
            self.reuse_run = self._load_reuse_run(reuse_from)
            self.reuse_index = {str(row["rule"]): dict(row) for row in self.reuse_run["rules"]}
        self.checkpoints_dir.mkdir(parents=True, exist_ok=True)
        self.chain = self._load_chain(self.checkpoints_dir)

    # ---- 链与索引 ---------------------------------------------------------

    @staticmethod
    def _load_chain(directory: Path) -> list[dict[str, Any]]:
        """按文件名顺序重放 checkpoints 目录的链（自摘要、previous 链接、序号连续）。"""

        if directory.is_symlink():
            raise RuleAssertionError("checkpoints 目录不得是符号链接")
        if not directory.is_dir():
            return []
        files = sorted(
            path for path in directory.iterdir()
            if path.suffix == ".json" and not path.name.endswith(INPUT_SUFFIX) and path.is_file() and not path.is_symlink()
        )
        chain: list[dict[str, Any]] = []
        previous: str | None = None
        for index, path in enumerate(files, 1):
            try:
                checkpoint = vc_artifacts.validate_evaluation_checkpoint(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, vc_artifacts.VCArtifactError) as error:
                raise RuleAssertionError(f"checkpoint 无法校验：{path.name}：{error}") from error
            if checkpoint["sequence"] != index or checkpoint["previous_checkpoint_sha256"] != previous:
                raise RuleAssertionError(f"checkpoint 链断裂：{path.name}")
            expected_name = f"{index:04d}-{checkpoint['rule']}-{checkpoint['side']}.json"
            if path.name != expected_name:
                raise RuleAssertionError(f"checkpoint 文件名与内容不一致：{path.name}")
            previous = str(checkpoint["checkpoint_sha256"])
            chain.append(checkpoint)
        return chain

    def _load_reuse_run(self, path: Path) -> dict[str, Any]:
        if path.is_symlink() or not path.is_file():
            raise RuleAssertionError(f"--reuse-from 不是普通文件：{path}")
        try:
            run = vc_artifacts.validate_evaluation_run(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError, vc_artifacts.VCArtifactError) as error:
            raise RuleAssertionError(f"--reuse-from 无法校验：{error}") from error
        if (
            run["campaign_id"] != self.campaign_id
            or run["candidate_id"] != self.candidate_id
            or run["candidate_revision"] != self.candidate_revision
            or run["evaluation_baseline"] >= self.baseline
        ):
            raise RuleAssertionError("--reuse-from 不属于同候选、同 revision 的更小编号基线")
        if run["derived"]:
            raise RuleAssertionError("b0 派生索引不参与复用")
        previous_root = assertions_root_for_baseline(self.campaign_root, self.candidate_id, int(run["evaluation_baseline"]))
        if path.resolve() != (previous_root / EVALUATION_RUN_FILENAME).resolve():
            raise RuleAssertionError("--reuse-from 不是前序基线的规范 evaluation-run.json 路径")
        previous_chain = self._load_chain(previous_root / CHECKPOINTS_DIRNAME)
        head = previous_chain[-1]["checkpoint_sha256"] if previous_chain else None
        if run["checkpoint_head_sha256"] != head:
            raise RuleAssertionError("--reuse-from 的 checkpoint_head_sha256 与前序链 head 不一致")
        self.reuse_chain = {(c["rule"], c["side"]): c for c in previous_chain}
        return run

    def find_checkpoint(self, rule_id: str, side: str) -> dict[str, Any] | None:
        for checkpoint in reversed(self.chain):
            if checkpoint["rule"] == rule_id and checkpoint["side"] == side:
                return checkpoint
        return None

    def next_sequence(self) -> int:
        return len(self.chain) + 1

    def head_sha256(self) -> str | None:
        return str(self.chain[-1]["checkpoint_sha256"]) if self.chain else None

    def _relative(self, path: Path) -> str:
        return path.resolve().relative_to(self.campaign_root).as_posix()

    # ---- 投影与依赖摘要 ----------------------------------------------------

    def compute_projection(self, rule_id: str, side: str, manifest_path: Path, evidence_root: Path, expected_codex_version: str) -> tuple[bytes, str]:
        """构造 per-rule 投影（与 checker 同源函数），返回规范字节与其摘要；不落盘。"""

        try:
            manifest = _load_checker_json(manifest_path, "capture manifest")
            projection = project_capture_manifest(self.profile, rule_id, manifest, evidence_root, expected_codex_version)
        except Exception as error:  # noqa: BLE001 - checker 侧任何配置错误都失败关闭
            raise RuleAssertionError(f"{rule_id} {side} 投影构造失败：{error}") from error
        payload = canonical_projection_bytes(projection)
        return payload, hashlib.sha256(payload).hexdigest()

    def write_projection(self, rule_id: str, side: str, payload: bytes) -> Path:
        """以将要写出的 checkpoint 序号 write-once 落投影输入文件 ``<NNNN>-<rule>-<side>-input.json``。"""

        path = self.checkpoints_dir / f"{self.next_sequence():04d}-{rule_id}-{side}{INPUT_SUFFIX}"
        _write_once_bytes(path, payload, "投影输入")
        return path

    def rule_contract_sha256(self, rule_id: str) -> str:
        entry = next(
            (rule for rule in self.profile["rules"] if isinstance(rule, dict) and rule.get("rule_id") == rule_id),
            None,
        )
        if entry is None:
            raise RuleAssertionError(f"画像不含规则 {rule_id}")
        return _canonical_sha256({"rule": entry, "profile_sha256": self.expected_profile_sha256})

    def dependency_projection_sha256(
        self,
        rule_id: str,
        mode: str,
        candidate_projection_sha256: str,
        official_projection_sha256: str | None,
        official_authority: Mapping[str, str] | None,
    ) -> str:
        return _canonical_sha256(
            {
                "rule": rule_id,
                "validation_mode": mode,
                "candidate_projection_sha256": candidate_projection_sha256,
                "official_projection_sha256": official_projection_sha256,
                "official_authority": dict(official_authority) if official_authority is not None else None,
                "rule_contract_sha256": self.rule_contract_sha256(rule_id),
                "checker_sha256": self.checker_sha256,
                "builder_sha256": self.builder_sha256,
            }
        )

    # ---- checkpoint 写出 ---------------------------------------------------

    def write_checkpoint(
        self,
        *,
        rule_id: str,
        side: str,
        status: str,
        document_path: Path,
        input_projection: Path,
        projection_sha256: str,
        command_sha256_value: str,
        checker_sha256: str,
        context: Mapping[str, Any],
        dependency_projection_sha256: str,
        reused_from: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        sequence = self.next_sequence()
        payload = {
            "schema_version": vc_artifacts.EVALUATION_CHECKPOINT_SCHEMA,
            "sequence": sequence,
            "rule": rule_id,
            "side": side,
            "status": status,
            "document": {"path": self._relative(document_path), "sha256": _file_sha256(document_path)},
            "input_projection": {"path": self._relative(input_projection), "sha256": projection_sha256},
            "projection_sha256": projection_sha256,
            "checker_sha256": checker_sha256,
            "command_sha256": command_sha256_value,
            "context": dict(context),
            "dependency_projection_sha256": dependency_projection_sha256,
            "executed_by": {
                "builder_sha256": self.builder_sha256,
                "run_dir": self.parent["run_dir"],
                "owner_nonce": self.parent["owner_nonce"],
                "run_manifest_sha256": self.parent["run_manifest_sha256"],
            },
            "reused_from": dict(reused_from) if reused_from is not None else None,
            "recorded_at_utc": _utc_now(),
            "previous_checkpoint_sha256": self.head_sha256(),
        }
        payload["checkpoint_sha256"] = _canonical_sha256(payload)
        try:
            checkpoint = vc_artifacts.validate_evaluation_checkpoint(payload)
        except vc_artifacts.VCArtifactError as error:
            raise RuleAssertionError(f"checkpoint 构造非法：{error}") from error
        path = self.checkpoints_dir / f"{sequence:04d}-{rule_id}-{side}.json"
        _write_once_json(path, checkpoint, "checkpoint")
        self.chain.append(checkpoint)
        return checkpoint

    def checkpoint_binding(self, checkpoint: Mapping[str, Any]) -> dict[str, str]:
        path = self.checkpoints_dir / f"{int(checkpoint['sequence']):04d}-{checkpoint['rule']}-{checkpoint['side']}.json"
        return {"path": self._relative(path), "sha256": _file_sha256(path)}

    # ---- 复用判定 ----------------------------------------------------------

    def reusable(self, rule_id: str, mode: str, dependency_projection_sha256: str) -> dict[str, dict[str, Any]] | None:
        """六稿复用判据：前序 pass、依赖摘要相等、checker 相等、文档 sha 未变、``anchored`` 授权。"""

        if self.reuse_run is None or self.reuse_authority != "anchored":
            return None
        row = self.reuse_index.get(rule_id)
        if row is None or row["status"] != "pass" or row["dependency_projection_sha256"] != dependency_projection_sha256:
            return None
        sides = ["candidate"] + (["official"] if mode == MODE_DUAL_WIRE else [])
        historical: dict[str, dict[str, Any]] = {}
        for side in sides:
            checkpoint = self.reuse_chain.get((rule_id, side))
            binding = row.get(f"{side}_checkpoint")
            if checkpoint is None or binding is None or checkpoint["status"] != "pass" or checkpoint["checker_sha256"] != self.checker_sha256:
                return None
            previous_root = assertions_root_for_baseline(self.campaign_root, self.candidate_id, int(self.reuse_run["evaluation_baseline"]))
            checkpoint_path = previous_root / CHECKPOINTS_DIRNAME / f"{int(checkpoint['sequence']):04d}-{rule_id}-{side}.json"
            if _file_sha256(checkpoint_path) != binding["sha256"]:
                return None
            document_path = self.campaign_root / checkpoint["document"]["path"]
            if not document_path.is_file() or _file_sha256(document_path) != checkpoint["document"]["sha256"]:
                return None
            projection_path = self.campaign_root / checkpoint["input_projection"]["path"]
            if not projection_path.is_file() or _file_sha256(projection_path) != checkpoint["input_projection"]["sha256"]:
                return None
            historical[side] = checkpoint
        return historical


def evaluation_run_preflight(assertions_root: Path, output: Path, chain: list[dict[str, Any]]) -> dict[str, Any] | None:
    """同基线重入：已有 evaluation-run.json 即视为完成——校验各行 checkpoint 与链 head 后按其状态退出，零 checker 调用。"""

    index_path = assertions_root / EVALUATION_RUN_FILENAME
    if not index_path.exists():
        return None
    try:
        run = vc_artifacts.validate_evaluation_run(json.loads(index_path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, vc_artifacts.VCArtifactError) as error:
        raise RuleAssertionError(f"既有 evaluation-run.json 无法校验：{error}") from error
    head = chain[-1]["checkpoint_sha256"] if chain else None
    if run["checkpoint_head_sha256"] != head:
        raise RuleAssertionError("既有 evaluation-run.json 的 checkpoint head 与 checkpoints 目录不一致")
    campaign_root = assertions_root
    while campaign_root.name != "assertions" and campaign_root.parent != campaign_root:
        campaign_root = campaign_root.parent
    campaign_root = campaign_root.parent
    for row in run["rules"]:
        for side in ("candidate", "official"):
            binding = row.get(f"{side}_checkpoint")
            if binding is None:
                continue
            path = campaign_root / binding["path"]
            if not path.is_file() or _file_sha256(path) != binding["sha256"]:
                raise RuleAssertionError(f"既有 evaluation-run.json 引用的 checkpoint 漂移：{binding['path']}")
    return run


def _rebuild_command_from_checkpoint(
    checkpoint: Mapping[str, Any],
    *,
    campaign_root: Path,
    profile_path: Path,
    rule_manifest_path: Path,
    target_version: str,
    expected_profile_sha256: str,
) -> list[str]:
    """以历史 checkpoint 的 context＋投影＋文档路径重建命令，并核对命令摘要。"""

    context = checkpoint["context"]
    command = build_assertion_command(
        rule_id=str(checkpoint["rule"]),
        capture_manifest=str(context["capture_manifest"]["path"]),
        evidence_root=str(context["evidence_root"]),
        profile=str(profile_path),
        rule_manifest=str(rule_manifest_path),
        expected_codex_version=target_version,
        expected_profile_sha256=expected_profile_sha256,
        side=str(checkpoint["side"]),
        capture_manifest_projection=str(campaign_root / checkpoint["input_projection"]["path"]),
        output=str(campaign_root / checkpoint["document"]["path"]),
    )
    if command_sha256(command) != checkpoint["command_sha256"]:
        raise RuleAssertionError(f"{checkpoint['rule']} {checkpoint['side']} 历史命令无法逐字重建")
    return command


def evaluate_rule_with_checkpoints(
    evaluation: EvaluationRun,
    *,
    rule_id: str,
    mode: str,
    candidate_expected_check_ids: list[str],
    official_expected_check_ids: list[str],
    candidate_manifest: Path,
    official_manifest: Path,
    candidate_root: Path,
    official_root: Path,
    candidate_prefix: str,
    official_prefix: str,
    candidate_results_dir: Path,
    official_results_dir: Path,
    results_root: Path,
    profile_path: Path,
    rule_manifest_path: Path,
    target_version: str,
    expected_profile_sha256: str,
    official_authority: dict[str, str],
    reuse_candidate_prefix: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """一条规则的完整评估：两侧投影 → 依赖摘要 → 复用或执行 → 逐侧 checkpoint → 索引行（+ v2 行）。"""

    sides = ["candidate"] + (["official"] if mode == MODE_DUAL_WIRE else [])
    manifests = {"candidate": candidate_manifest, "official": official_manifest}
    roots = {"candidate": candidate_root, "official": official_root}
    prefixes = {"candidate": candidate_prefix, "official": official_prefix}
    outputs = {
        "candidate": candidate_results_dir / f"{rule_id}.json",
        "official": official_results_dir / f"{rule_id}.json",
    }
    # 两侧投影先算出（不落盘），规则级依赖摘要由两侧投影摘要共同决定；投影文件在该侧真正执行前
    # 以其 checkpoint 序号 write-once 写出，命中既有 checkpoint 或复用时不再写新投影文件。
    projections: dict[str, tuple[bytes, str]] = {}
    for side in sides:
        projections[side] = evaluation.compute_projection(rule_id, side, manifests[side], roots[side], target_version)
    dependency = evaluation.dependency_projection_sha256(
        rule_id,
        mode,
        projections["candidate"][1],
        projections["official"][1] if mode == MODE_DUAL_WIRE else None,
        None if mode == MODE_DUAL_WIRE else official_authority,
    )
    historical = evaluation.reusable(rule_id, mode, dependency)
    checkpoints: dict[str, dict[str, Any]] = {}
    documents: dict[str, dict[str, Any]] = {}
    commands: dict[str, list[str]] = {}
    if historical is not None:
        # 复用：不执行 checker，只写引用历史文档的 reused checkpoint；v2 行由历史事实重建。
        for side in sides:
            past = historical[side]
            existing = evaluation.find_checkpoint(rule_id, side)
            if existing is None:
                existing = evaluation.write_checkpoint(
                    rule_id=rule_id,
                    side=side,
                    status="pass",
                    document_path=evaluation.campaign_root / past["document"]["path"],
                    input_projection=evaluation.campaign_root / past["input_projection"]["path"],
                    projection_sha256=str(past["projection_sha256"]),
                    command_sha256_value=str(past["command_sha256"]),
                    checker_sha256=str(past["checker_sha256"]),
                    context=past["context"],
                    dependency_projection_sha256=dependency,
                    reused_from={
                        "baseline": int(evaluation.reuse_run["evaluation_baseline"]),  # type: ignore[index]
                        "checkpoint_sha256": str(past["checkpoint_sha256"]),
                        "document_sha256": str(past["document"]["sha256"]),
                    },
                )
            elif existing["reused_from"] is None or existing["reused_from"]["checkpoint_sha256"] != past["checkpoint_sha256"]:
                raise RuleAssertionError(f"{rule_id} {side} 已有 checkpoint 与本次复用判定冲突")
            checkpoints[side] = existing
            documents[side] = json.loads((evaluation.campaign_root / past["document"]["path"]).read_text(encoding="utf-8"))
            commands[side] = _rebuild_command_from_checkpoint(
                past,
                campaign_root=evaluation.campaign_root,
                profile_path=profile_path,
                rule_manifest_path=rule_manifest_path,
                target_version=target_version,
                expected_profile_sha256=expected_profile_sha256,
            )
    else:
        for side in sides:
            projection_payload, projection_digest = projections[side]
            context = {
                "capture_manifest": {"path": str(manifests[side]), "sha256": _file_sha256(manifests[side])},
                "evidence_root": str(roots[side]),
                "profile_sha256": expected_profile_sha256,
                "rule_manifest_sha256": evaluation.rule_manifest_sha256,
            }
            existing = evaluation.find_checkpoint(rule_id, side)
            output = outputs[side]
            if existing is not None:
                document_path = evaluation.campaign_root / existing["document"]["path"]
                if (
                    existing["reused_from"] is None
                    and existing["checker_sha256"] == evaluation.checker_sha256
                    and document_path.is_file()
                    and _file_sha256(document_path) == existing["document"]["sha256"]
                    and existing["projection_sha256"] == projection_digest
                    and existing["dependency_projection_sha256"] == dependency
                ):
                    checkpoints[side] = existing
                    documents[side] = json.loads(document_path.read_text(encoding="utf-8"))
                    commands[side] = _rebuild_command_from_checkpoint(
                        existing,
                        campaign_root=evaluation.campaign_root,
                        profile_path=profile_path,
                        rule_manifest_path=rule_manifest_path,
                        target_version=target_version,
                        expected_profile_sha256=expected_profile_sha256,
                    )
                    continue
                raise RuleAssertionError(f"{rule_id} {side} 已有 checkpoint 但文档、投影或依赖漂移，禁止同基线原地重跑")
            if output.exists() or output.is_symlink():
                orphan = output.with_name(f"{output.name}.orphan-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}")
                os.replace(output, orphan)
            projection_path = evaluation.write_projection(rule_id, side, projection_payload)
            command, document = run_side_assertion(
                rule_id=rule_id,
                capture_manifest=manifests[side],
                evidence_root=roots[side],
                output=output,
                profile=profile_path,
                rule_manifest=rule_manifest_path,
                expected_codex_version=target_version,
                expected_profile_sha256=expected_profile_sha256,
                side=side,
                capture_manifest_projection=projection_path,
                allow_fail=True,
            )
            if document.get("projection_sha256") != projection_digest:
                raise RuleAssertionError(f"{rule_id} {side} 单规则文档未绑定本次投影摘要")
            # 三者一致：checker 自记的实际文件摘要（单规则文档）＝批次冻结值（checkpoint／index 口径）。
            if document.get("checker_sha256") != evaluation.checker_sha256:
                raise RuleAssertionError(
                    f"{rule_id} {side} 单规则文档记录的 checker 摘要与批次冻结值不一致，拒绝写 checkpoint"
                )
            checkpoints[side] = evaluation.write_checkpoint(
                rule_id=rule_id,
                side=side,
                status=str(document["status"]),
                document_path=output,
                input_projection=projection_path,
                projection_sha256=projection_digest,
                command_sha256_value=str(document["command_sha256"]),
                checker_sha256=evaluation.checker_sha256,
                context=context,
                dependency_projection_sha256=dependency,
                reused_from=None,
            )
            documents[side] = document
            commands[side] = command
    status = "pass" if all(checkpoints[side]["status"] == "pass" for side in sides) else "fail"
    index_row = {
        "rule": rule_id,
        "validation_mode": mode,
        "status": status,
        "candidate_checkpoint": evaluation.checkpoint_binding(checkpoints["candidate"]),
        "official_checkpoint": evaluation.checkpoint_binding(checkpoints["official"]) if mode == MODE_DUAL_WIRE else None,
        "dependency_projection_sha256": dependency,
        "reused_from": dict(checkpoints["candidate"]["reused_from"]) if checkpoints["candidate"]["reused_from"] else None,
    }
    if status != "pass":
        return index_row, None
    document_paths = {side: evaluation.campaign_root / checkpoints[side]["document"]["path"] for side in sides}
    evidence_roots = {side: Path(checkpoints[side]["context"]["evidence_root"]) for side in sides}
    # 复用行由历史事实重建：候选证据引用的逻辑前缀属于被复用基线的候选阶段（attempt-recovery 基线的候选
    # 证据前缀与被复用基线不同；accept 按被复用基线 inventory 核对），由配置的 reuse_candidate_evidence_prefix
    # 给出；未给出时沿用当前前缀（同一候选阶段的复用，与既有行为一致）。
    row_prefixes = dict(prefixes)
    if historical is not None and reuse_candidate_prefix:
        row_prefixes["candidate"] = reuse_candidate_prefix
    if mode == MODE_DUAL_WIRE:
        result_row = build_dual_wire_result(
            rule_id=rule_id,
            official_expected_check_ids=official_expected_check_ids,
            candidate_expected_check_ids=candidate_expected_check_ids,
            official=(commands["official"], documents["official"], document_paths["official"]),
            candidate=(commands["candidate"], documents["candidate"], document_paths["candidate"]),
            official_root=evidence_roots["official"],
            candidate_root=evidence_roots["candidate"],
            official_prefix=row_prefixes["official"],
            candidate_prefix=row_prefixes["candidate"],
            results_root=results_root,
            rationale=(
                f"{rule_id} 在官方 {target_version} 证据与候选证据上分别由 "
                "candidate_rule_assertion.py 独立执行并全部通过；"
                "check 集合与批准画像逐项一致，结论只来自机器断言。"
            ),
        )
    else:
        result_row = build_candidate_profile_result(
            rule_id=rule_id,
            expected_check_ids=candidate_expected_check_ids,
            candidate=(commands["candidate"], documents["candidate"], document_paths["candidate"]),
            candidate_root=evidence_roots["candidate"],
            candidate_prefix=row_prefixes["candidate"],
            official_authority=official_authority,
            results_root=results_root,
            rationale=(
                f"{rule_id} 描述 Sub2API 内部实现事实，由候选侧机器断言"
                "通过；官方权威为批准断言画像链，行内逐字绑定其摘要。"
            ),
        )
    return index_row, result_row


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按验收契约编排逐规则断言并汇总为 v2 验收结果"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    parser.add_argument(
        "--evaluation-baseline",
        type=int,
        default=0,
        help="改造 5：评估基线编号 b<K>（0 为原路径）；正式 Campaign 布局按其解析 assertions 目录",
    )
    parser.add_argument(
        "--reuse-from",
        type=Path,
        help="改造 5：前序基线的 evaluation-run.json；按复用判据引用其 pass checkpoint，不复制",
    )
    parser.add_argument(
        "--reuse-authority",
        choices=("anchored", "none"),
        default="none",
        help="改造 5：recovery.json 冻结的复用授权；none 时拒写任何 reused checkpoint",
    )
    arguments = parser.parse_args()
    if arguments.evaluation_baseline < 0:
        raise SystemExit("--evaluation-baseline 必须是非负整数")

    try:
        config = json.loads(arguments.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"无法读取配置：{error}") from error

    # 契约权威是本 Campaign 批准的断言画像，不是仓库冻结基线：目标规则集允许
    # 相对基线增删，验收模型必须随批准画像走（与 accept 侧同源，见 ACC-04）。
    profile_path = Path(config["assertion_profile"]).resolve(strict=True)
    try:
        contract = build_contract_payload(load_profile(profile_path))
    except AcceptanceContractError as error:
        raise SystemExit(f"验收契约不可用：{error}") from error
    validation_modes = contract["validation_modes"]
    expected_by_rule = contract["expected_check_ids"]

    rule_manifest_path = Path(config["rule_manifest"]).resolve(strict=True)
    expected_profile_sha256 = str(config["expected_profile_sha256"])
    official_root = Path(config["official_evidence_root"]).resolve(strict=True)
    candidate_root = Path(config["candidate_evidence_root"]).resolve(strict=True)
    official_manifest = Path(config["official_capture_manifest"]).resolve(strict=True)
    candidate_manifest = Path(config["candidate_capture_manifest"]).resolve(strict=True)
    official_prefix = str(config["official_evidence_prefix"])
    candidate_prefix = str(config["candidate_evidence_prefix"])
    # 改造 5 M2：--reuse-from 引用的被复用基线若是另一份候选阶段结果（attempt-recovery 基线之前的基线），
    # 复用行的候选证据引用要用该基线的逻辑路径前缀；同一候选阶段的复用可不给（沿用当前前缀）。
    reuse_candidate_prefix_raw = config.get("reuse_candidate_evidence_prefix")
    if reuse_candidate_prefix_raw is not None and (not isinstance(reuse_candidate_prefix_raw, str) or not reuse_candidate_prefix_raw.strip()):
        raise SystemExit("配置非法：reuse_candidate_evidence_prefix 必须是非空字符串")
    reuse_candidate_prefix = reuse_candidate_prefix_raw
    target_version = str(config["target_version"])
    rule_ids = list(config["rules"])
    try:
        official_authority = validate_official_authority(
            config.get("official_authority")
        )
    except RuleAssertionError as error:
        raise SystemExit(f"配置非法：{error}") from error
    unknown_rules = sorted(set(rule_ids) - set(validation_modes))
    if unknown_rules:
        raise SystemExit(f"配置引用契约外规则：{unknown_rules}")

    results_dir = arguments.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    try:
        results_root, official_results_dir, candidate_results_dir = (
            resolve_machine_layout(config, results_dir, arguments.evaluation_baseline)
        )
    except RuleAssertionError as error:
        raise SystemExit(f"配置非法：{error}") from error
    formal_campaign_layout = results_root != results_dir
    evaluation: EvaluationRun | None = None
    if formal_campaign_layout:
        assertions_root = results_dir.resolve().parent
        try:
            evaluation = EvaluationRun(
                campaign_root=results_root,
                candidate_id=str(config["candidate_id"]),
                baseline=arguments.evaluation_baseline,
                assertions_root=assertions_root,
                profile=load_checker_profile(
                    profile_path,
                    rule_manifest_path,
                    verify_frozen_digest=False,
                    expected_codex_version=target_version,
                    expected_profile_sha256=expected_profile_sha256,
                ),
                expected_profile_sha256=expected_profile_sha256,
                rule_manifest_path=rule_manifest_path,
                reuse_from=arguments.reuse_from,
                reuse_authority=arguments.reuse_authority,
            )
            existing_run = evaluation_run_preflight(assertions_root, arguments.output, evaluation.chain)
        except (RuleAssertionError, Exception) as error:  # noqa: BLE001 - 画像加载错误同样失败关闭
            raise SystemExit(f"评估运行不可用：{error}") from error
        if existing_run is not None:
            # 同基线幂等重入：有效 checkpoint／index 一律视为完成，不重跑、不覆盖，按既有状态退出。
            failed = sorted(row["rule"] for row in existing_run["rules"] if row["status"] != "pass")
            print(
                json.dumps(
                    {"evaluation_run": str(assertions_root / EVALUATION_RUN_FILENAME), "reentry": True, "failed_rules": failed},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            if failed:
                print(f"评估索引已存在且含未通过规则（零 checker 重入）：{failed}", file=sys.stderr)
                return 1
            if not arguments.output.is_file():
                raise SystemExit("评估索引全部通过但 results.json 缺失，禁止重写")
            return 0
    elif arguments.evaluation_baseline or arguments.reuse_from is not None or arguments.reuse_authority != "none":
        raise SystemExit("平铺布局不支持 --evaluation-baseline／--reuse-from／--reuse-authority")

    rule_results: list[dict[str, Any]] = []
    index_rows: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        mode = validation_modes[rule_id]
        if evaluation is not None:
            index_row, result_row = evaluate_rule_with_checkpoints(
                evaluation,
                rule_id=rule_id,
                mode=mode,
                candidate_expected_check_ids=expected_check_ids_for_side(contract, rule_id, "candidate"),
                official_expected_check_ids=expected_check_ids_for_side(contract, rule_id, "official"),
                candidate_manifest=candidate_manifest,
                official_manifest=official_manifest,
                candidate_root=candidate_root,
                official_root=official_root,
                candidate_prefix=candidate_prefix,
                official_prefix=official_prefix,
                candidate_results_dir=candidate_results_dir,
                official_results_dir=official_results_dir,
                results_root=results_root,
                profile_path=profile_path,
                rule_manifest_path=rule_manifest_path,
                target_version=target_version,
                expected_profile_sha256=expected_profile_sha256,
                official_authority=official_authority,
                reuse_candidate_prefix=reuse_candidate_prefix,
            )
            index_rows.append(index_row)
            if result_row is not None:
                rule_results.append(result_row)
            print(f"{rule_id} {mode} {index_row['status']}" + ("（复用）" if index_row["reused_from"] else ""), flush=True)
            continue
        # 侧别限定 check 不在本侧执行，期望集合必须按侧复算，否则闭合校验会
        # 拿全集去比对一份合法缺项的结果文档。
        candidate_expected_check_ids = expected_check_ids_for_side(
            contract, rule_id, "candidate"
        )
        official_expected_check_ids = expected_check_ids_for_side(
            contract, rule_id, "official"
        )
        candidate_output = candidate_results_dir / (
            f"{rule_id}.json"
            if formal_campaign_layout
            else f"{rule_id}.candidate.json"
        )
        candidate = run_side_assertion(
            rule_id=rule_id,
            capture_manifest=candidate_manifest,
            evidence_root=candidate_root,
            output=candidate_output,
            profile=profile_path,
            rule_manifest=rule_manifest_path,
            expected_codex_version=target_version,
            expected_profile_sha256=expected_profile_sha256,
            side="candidate",
        )
        if mode == MODE_DUAL_WIRE:
            official_output = official_results_dir / (
                f"{rule_id}.json"
                if formal_campaign_layout
                else f"{rule_id}.official.json"
            )
            official = run_side_assertion(
                rule_id=rule_id,
                capture_manifest=official_manifest,
                evidence_root=official_root,
                output=official_output,
                profile=profile_path,
                rule_manifest=rule_manifest_path,
                expected_codex_version=target_version,
                expected_profile_sha256=expected_profile_sha256,
                side="official",
            )
            rule_results.append(
                build_dual_wire_result(
                    rule_id=rule_id,
                    official_expected_check_ids=official_expected_check_ids,
                    candidate_expected_check_ids=candidate_expected_check_ids,
                    official=(*official, official_output),
                    candidate=(*candidate, candidate_output),
                    official_root=official_root,
                    candidate_root=candidate_root,
                    official_prefix=official_prefix,
                    candidate_prefix=candidate_prefix,
                    results_root=results_root,
                    rationale=(
                        f"{rule_id} 在官方 {target_version} 证据与候选证据上分别由 "
                        "candidate_rule_assertion.py 独立执行并全部通过；"
                        "check 集合与批准画像逐项一致，结论只来自机器断言。"
                    ),
                )
            )
            print(f"{rule_id} dual_wire 双侧通过", flush=True)
        else:
            rule_results.append(
                build_candidate_profile_result(
                    rule_id=rule_id,
                    expected_check_ids=candidate_expected_check_ids,
                    candidate=(*candidate, candidate_output),
                    candidate_root=candidate_root,
                    candidate_prefix=candidate_prefix,
                    official_authority=official_authority,
                    results_root=results_root,
                    rationale=(
                        f"{rule_id} 描述 Sub2API 内部实现事实，由候选侧机器断言"
                        "通过；官方权威为批准断言画像链，行内逐字绑定其摘要。"
                    ),
                )
            )
            print(f"{rule_id} candidate_profile 候选通过", flush=True)

    if evaluation is not None:
        # 改造 5：批次结束先 write-once 落 evaluation-run.json（只汇总 checkpoint），有 fail 再非零退出；
        # results.json（v2 闭集不变）只在全 pass 时生成。
        index = {
            "schema_version": vc_artifacts.EVALUATION_RUN_SCHEMA,
            "campaign_id": evaluation.campaign_id,
            "candidate_id": evaluation.candidate_id,
            "candidate_revision": evaluation.candidate_revision,
            "evaluation_baseline": evaluation.baseline,
            "derived": False,
            "evaluator": dict(evaluation.evaluator),
            "rules": sorted(index_rows, key=lambda row: row["rule"]),
            "checkpoint_head_sha256": evaluation.head_sha256(),
            "recorded_at_utc": _utc_now(),
        }
        index["run_sha256"] = _canonical_sha256(index)
        try:
            index = vc_artifacts.validate_evaluation_run(index)
            _write_once_json(evaluation.assertions_root / EVALUATION_RUN_FILENAME, index, "evaluation-run.json")
        except (vc_artifacts.VCArtifactError, RuleAssertionError) as error:
            raise SystemExit(f"evaluation-run.json 写出失败：{error}") from error
        failed = sorted(row["rule"] for row in index_rows if row["status"] != "pass")
        if failed:
            print(
                json.dumps(
                    {"evaluation_run": str(evaluation.assertions_root / EVALUATION_RUN_FILENAME), "failed_rules": failed},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            print(f"存在未通过规则，results.json 不生成：{failed}", file=sys.stderr)
            return 1

    document = build_results_document(
        candidate_id=str(config["candidate_id"]),
        target_version=target_version,
        profile_id=str(config["profile_id"]),
        profile_digest=str(config["profile_digest"]),
        official_package_digest=str(config["official_package_digest"]),
        candidate_package_digest=str(config["candidate_package_digest"]),
        comparison_package_digest=str(config["comparison_package_digest"]),
        acceptance_contract_sha256_value=contract_sha256(contract),
        rules=rule_results,
    )
    rendered = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    if evaluation is not None:
        try:
            _write_once_bytes(arguments.output, rendered.encode("utf-8"), "results.json")
        except RuleAssertionError as error:
            raise SystemExit(str(error)) from error
    else:
        arguments.output.write_text(rendered, encoding="utf-8")
    arguments.output.chmod(0o600)
    print(
        json.dumps(
            {"output": str(arguments.output), "rule_count": len(rule_results)},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
