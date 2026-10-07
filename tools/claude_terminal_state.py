#!/usr/bin/env python3
"""Claude Code 升级终态收据合同（Claude 升级控制面设计 B5b-06）：生成端的规范身份、只读校验与当前生产终态门禁。

为什么单独成文件：Codex 的终态合同（``official-client-codex-<ver>-terminal-state/v1``）与校验函数在冻结的
``tools/check_ledger_completeness.py`` 里，按 Codex Runtime Catalog（ReleaseGraph ``mode == active``）判定当前版本；Claude 的
生产选择在另一份目录——``backend/internal/officialegress/catalogdata/claude/release-catalog.json`` 的 ``production_active``
selector——合同与判据都不同。本文件只用标准库、所有入口都以仓库根为参数：收尾（``tools/codex_closeout.py --client claude``）
可以像 Codex 那样从候选仓库直接加载它，在发布前对候选仓库重验；两个门禁消费者（``tools/check_runtime_catalog_projection.py``
与 ``tools/evidence_index.py``）按客户端分派到这里。

一、合同
========
* 合同名 ``official-client-claude-<ver>-terminal-state/v1``，文件 ``docs/egress/maintenance/CLAUDE_CODE_<from>_TO_<to>_TERMINAL_STATE_RECEIPT.json``；
* 自摘要：去掉 ``identity_sha256`` 后的规范 JSON（``sort_keys``、紧凑分隔、``ensure_ascii=False``）的 SHA-256，与 Codex 同一算法
  （历史上带尾换行的变体同样接受）；
* 必填字段：``target``（``version``、``previous_version``，与 Codex 终态收据同名；其余描述性键允许）、``result``（passed）、``completed_at_utc``、``release_catalog``（目录文件、
  ``production_active`` selector 与它选中的 Release：Profile、Wire、release／bundle 摘要）、``campaign_chain``（至少一个官方证据来源，
  末级为最终 Campaign）、四份阶段收据（``STAGE_RECEIPT_FIELDS``：B4-08 生产分支的目录转换、DeploymentFact、ProductionAcceptance，
  加 B4-07 的候选交付收据；都绑定 ``docs/egress/maintenance/`` 下的文件与摘要）、``evidence_archive``（私有证据归档坐标）。
  B4-08 尚未实施，阶段收据先只核路径与摘要，字段集合集中在 ``STAGE_RECEIPT_FIELDS``，B4-08 落地时在这里收紧；
* 说明性字段只允许 ``identity_algorithm`` 与以 ``_note`` 结尾的键，其余未知字段失败关闭。

二、Claude Release 目录的 production_active（照 Go 读侧 ``claude_release_catalog.go``）
=====================================================================================
``release_catalog_state``：目录合同名与 persona；每个 Release 的 Profile／Wire 是内容寻址路径且摘要一致，Profile、Wire 与目录的
版本、平台、模型闭集一致，release 与 bundle 摘要按 Go 同一算法复算；``production_active`` 是 ``release`` 选择器、引用已知
Release、changeset 非空、Approval 位于 ``catalogdata/claude/production/`` 且摘要一致；``validation_candidate`` 不得携带 Approval、
changeset 不得与 production_active 相同；rollback 必须是操作部署。另照 ``claude_production_release.go`` 的
``validateClaudeFWHProductionApprovalForRelease`` 核 Approval 与它选中的 Release 一致：合同名、批准编号、状态 approved，
target 的产品、版本、平台、模型、规则数与 Profile／Wire／release／bundle 摘要（Envelope 与遗留处置仍只由 Go 读侧负责）。

三、当前生产终态门禁（``validate_claude_terminal_state_receipts``）
================================================================
每份 ``CLAUDE_CODE_*_TERMINAL_STATE_RECEIPT.json`` 都按合同校验（历史收据的目录类绑定可按收据首次入库提交回读，与 Codex
``allow_historical`` 同一口径；阶段收据必须逐字在库）；同一版本不得有两份；当前 production_active 必须恰好有一份，且它登记的
selector（Release 摘要、changeset、Approval）与 Release 坐标与当前目录逐项相等。目录文件本身的摘要不要求等于当前——下一轮
升级的候选阶段会改 ``validation_candidate``、生产回退会改 rollback，而 production_active 不变；这与 Codex 当前 Active 收据的
目录绑定按收据入库提交回读同一口径。

唯一豁免（``PRE_CONTRACT_BASELINE``，照 Codex 豁免 0.149.1 的先例）：production_active 精确等于 2.1.226 基线——版本、release 摘要、
Approval 摘要三项全等——时不要求终态收据。理由：这个 Release 在本合同生效之前，由 FW-G 隔离验收与 FW-H 生产批准放行，没有新
控制面的 Campaign 链与阶段收据可以绑定；补签一份等于凭空编造。三项任一不同，就不是这个基线，必须有终态收据。

四、自测夹具
============
``write_fixture_catalog``／``write_fixture_stage_receipts``／``fixture_terminal_receipt`` 在任意目录写一份自洽的合成 Claude Release
目录、阶段收据与终态收据（合成版本号与模型，不碰真实目录）；``self_test`` 用它们跑正负例，供
``check_runtime_catalog_projection.py --self-test``（check-egress-spec 的 ``egress-spec-runtime-catalog-self-test``）与单元测试复用。
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_RELATIVE = "docs/egress/maintenance"
CATALOG_RELATIVE = "backend/internal/officialegress/catalogdata/claude/release-catalog.json"
OFFICIALEGRESS_RELATIVE = "backend/internal/officialegress"
CATALOG_SCHEMA = "official-egress-claude-release-catalog/v1"
CATALOG_PERSONA = "claude-code"
APPROVAL_PREFIX = "catalogdata/claude/production/"
RECEIPT_PREFIX = "CLAUDE_CODE_"
RECEIPT_SUFFIX = "_TERMINAL_STATE_RECEIPT.json"
RECEIPT_GLOB = RECEIPT_PREFIX + "*" + RECEIPT_SUFFIX
SCHEMA_RE = re.compile(r"^official-client-claude-(\d+\.\d+\.\d+)-terminal-state/v1$")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
APPROVAL_SCHEMA = "claude-fw-h-production-approval/v2"
RELEASE_FIELDS = frozenset(
    {
        "version",
        "platform",
        "profile",
        "wire",
        "required_rules_manifest_sha256",
        "release_sha256",
        "bundle_sha256",
        "required_rule_count",
        "profile_atomic_assertion_count",
        "local_scenario_assertion_count",
        "strict_endpoint_count",
        "models",
        "model_evidence",
    }
)
COUNT_FIELDS = ("required_rule_count", "profile_atomic_assertion_count", "local_scenario_assertion_count", "strict_endpoint_count")
RELEASE_SCHEMA = "claude-code-fw-g-model-capability-release/v1"
BUNDLE_SCHEMA = "claude-code-fw-g-model-capability-bundle/v1"
BUNDLE_PERSONA = "claude-code-oauth"
# B4-08 生产分支（目录转换、DeploymentFact、ProductionAcceptance）与 B4-07 候选交付的收据：绑定 maintenance 下的文件与摘要。
STAGE_RECEIPT_FIELDS = ("catalog_transition", "deployment_fact", "production_acceptance", "candidate_delivery")
CAMPAIGN_OFFICIAL_ROLE = "official_evidence_source"
REQUIRED_FIELDS = frozenset(
    {
        "schema_version",
        "target",
        "result",
        "completed_at_utc",
        "identity_sha256",
        "release_catalog",
        "campaign_chain",
        "evidence_archive",
        *STAGE_RECEIPT_FIELDS,
    }
)
NOTE_FIELDS = frozenset({"identity_algorithm"})
# 合同生效前的唯一基线（见模块说明第三节）：2.1.226 由 FW-G 隔离验收与 FW-H 生产批准放行，没有新控制面 Campaign 链。
PRE_CONTRACT_BASELINE: Mapping[str, str] = {
    "version": "2.1.226",
    "release_sha256": "48586f47e0abcde9ea357c08c1465253eaa296862153a89c20e4cf0f1e6f52f8",
    "approval_sha256": "736cf9599c6dcda8aa5f13fd5fa9bf4fe8273e6cd21952892e0fd35eae96f230",
    "reason": "合同生效前、由 FW-G 隔离验收与 FW-H 生产批准放行、没有新控制面 Campaign 链与阶段收据；不补签终态收据。",
}


class TerminalStateError(RuntimeError):
    """Claude 终态收据或 Release 目录不满足合同：一律失败关闭。"""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise TerminalStateError(f"JSON 对象键重复：{key}")
        value[key] = item
    return value


def _read_json(path: Path, label: str) -> Any:
    if path.is_symlink() or not path.is_file():
        raise TerminalStateError(f"{label}不是可信普通文件：{path}")
    try:
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_object)
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TerminalStateError(f"{label}无法读取：{path}：{error}") from error


def claude_terminal_state_identity(document: Mapping[str, Any], *, trailing_newline: bool = False) -> str:
    """终态收据去掉 ``identity_sha256`` 后的规范身份（与 Codex 通用终态收据同一算法）。"""

    payload = {key: value for key, value in document.items() if key != "identity_sha256"}
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if trailing_newline:
        canonical += b"\n"
    return _sha256(canonical)


def _canonical_catalog_digest(document: Mapping[str, Any]) -> str:
    """与 Go ``claudeCatalogCanonicalDigest`` 同一算法：``json.Marshal``（键序排序、紧凑、UTF-8 原样、HTML 与行分隔符转义）加尾换行。"""

    raw = json.dumps(dict(document), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    for character, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"), ("\u2028", "\\u2028"), ("\u2029", "\\u2029")):
        raw = raw.replace(character, escaped)
    return _sha256((raw + "\n").encode("utf-8"))


def _list_length(value: Any) -> int:
    """JSON 数组的长度；不是数组记 -1（与任何合法计数都不相等，失败关闭而不是抛类型错误）。"""

    return len(value) if isinstance(value, list) else -1


def _mapping_path(value: Any, *keys: str) -> Mapping[str, Any]:
    """沿对象键逐级取值；任一级不是对象即得空对象（Go 端按结构体解码时缺失字段同为零值）。"""

    for key in keys:
        value = value.get(key) if isinstance(value, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _blob(root: Path, reference: Any, directory: str, label: str) -> tuple[dict[str, str], dict[str, Any]]:
    """内容寻址 blob：路径必须是 ``<directory>/<sha256>.json``，字节摘要一致；返回 (引用, 内容)。"""

    if not isinstance(reference, Mapping) or set(reference) != {"path", "sha256"}:
        raise TerminalStateError(f"{label}引用字段不闭合")
    path_text, digest = str(reference["path"]), str(reference["sha256"])
    if not SHA256_RE.fullmatch(digest) or PurePosixPath(path_text).as_posix() != path_text or path_text != f"{directory}/{digest}.json":
        raise TerminalStateError(f"{label}不是规范内容寻址路径：{path_text}")
    path = root / OFFICIALEGRESS_RELATIVE / path_text
    if path.is_symlink() or not path.is_file() or _sha256(path.read_bytes()) != digest:
        raise TerminalStateError(f"{label}缺失或摘要不一致：{path_text}")
    return {"path": path_text, "sha256": digest}, _read_json(path, label)


def _release(root: Path, entry: Any) -> dict[str, Any]:
    if not isinstance(entry, Mapping) or not set(entry) <= RELEASE_FIELDS:
        raise TerminalStateError("Claude Release 条目不是对象或含未知字段")
    evidence = entry.get("model_evidence")
    if (
        any(isinstance(entry.get(key), bool) or not isinstance(entry.get(key), int) or entry[key] <= 0 for key in COUNT_FIELDS)
        or not SHA256_RE.fullmatch(str(entry.get("required_rules_manifest_sha256", "")))
        or not isinstance(evidence, Mapping)
        or not set(evidence) <= {"successful_attempts", "historical_failed_attempts"}
        or isinstance(evidence.get("successful_attempts"), bool)
        or not isinstance(evidence.get("successful_attempts"), int)
        or evidence["successful_attempts"] <= 0
        or isinstance(evidence.get("historical_failed_attempts", 0), bool)
        or not isinstance(evidence.get("historical_failed_attempts", 0), int)
        or evidence.get("historical_failed_attempts", 0) < 0
    ):
        raise TerminalStateError("Claude Release 坐标不完整")
    version = str(entry.get("version", "")).strip()
    platform = str(entry.get("platform", "")).strip()
    if not VERSION_RE.fullmatch(version) or not platform:
        raise TerminalStateError(f"Claude Release 版本或平台非法：{version!r}")
    profile_ref, profile = _blob(root, entry.get("profile"), f"catalogdata/claude/profiles/{version}", f"{version} Profile ")
    wire_ref, wire = _blob(root, entry.get("wire"), f"catalogdata/claude/wire/{version}", f"{version} Wire ")
    models = entry.get("models")
    if not isinstance(models, list) or not models or any(not isinstance(item, str) or item.strip() != item or not item for item in models) or len(set(models)) != len(models):
        raise TerminalStateError(f"{version} Release 模型闭集非法")
    identity = profile.get("identity") if isinstance(profile, Mapping) else None
    wire_identity = wire.get("identity") if isinstance(wire, Mapping) else None
    if (
        not isinstance(identity, Mapping)
        or identity.get("version") != version
        or identity.get("platform") != platform
        or identity.get("supported_models") != models
        or _list_length(profile.get("rules")) != entry.get("required_rule_count")
        or _list_length(profile.get("strict_endpoints")) != entry.get("strict_endpoint_count")
    ):
        raise TerminalStateError(f"{version} Catalog 与 Profile 身份不一致")
    capability = str(identity.get("model_capability_catalog_sha256", ""))
    model_catalog = _mapping_path(wire, "implementation_policy", "model_catalog")
    if (
        not isinstance(wire_identity, Mapping)
        or wire_identity.get("version") != version
        or wire_identity.get("platform") != platform
        or wire_identity.get("profile_digest") != profile_ref["sha256"]
        or wire_identity.get("model_capability_catalog_digest") != capability
        or not SHA256_RE.fullmatch(capability)
        or model_catalog.get("required_rule_count") != entry.get("required_rule_count")
        or not isinstance(model_catalog.get("models"), list)
        or [_mapping_path(item).get("canonical_model") for item in model_catalog["models"]] != models
    ):
        raise TerminalStateError(f"{version} Catalog 与 Wire 身份不一致")
    release_digest = _canonical_catalog_digest(
        {
            "schema_version": RELEASE_SCHEMA,
            "version": version,
            "platform": platform,
            "profile_sha256": profile_ref["sha256"],
            "wire_sha256": wire_ref["sha256"],
            "model_capability_catalog_sha256": capability,
            "required_rules_manifest_sha256": entry.get("required_rules_manifest_sha256"),
            "required_rule_count": entry.get("required_rule_count"),
        }
    )
    if release_digest != entry.get("release_sha256"):
        raise TerminalStateError(f"{version} ReleaseArtifact 摘要不一致")
    bundle_digest = _canonical_catalog_digest(
        {
            "schema_version": BUNDLE_SCHEMA,
            "persona": BUNDLE_PERSONA,
            "release_sha256": release_digest,
            "profile_sha256": profile_ref["sha256"],
            "wire_sha256": wire_ref["sha256"],
        }
    )
    if bundle_digest != entry.get("bundle_sha256"):
        raise TerminalStateError(f"{version} ReleaseBundle 摘要不一致")
    return {
        "version": version,
        "platform": platform,
        "release_sha256": release_digest,
        "bundle_sha256": bundle_digest,
        "profile": profile_ref,
        "wire": wire_ref,
        "_models": list(models),
        "_required_rule_count": entry["required_rule_count"],
    }


def _public_release(release: Mapping[str, Any]) -> dict[str, Any]:
    """终态收据登记的 Release 坐标（去掉只供本模块核对 Approval 的内部字段）。"""

    return {key: value for key, value in release.items() if not key.startswith("_")}


def release_catalog_state(root: Path = ROOT) -> dict[str, Any]:
    """读取并按 Go 读侧校验 Claude Release 目录，返回 production_active 的状态（见模块说明第二节）。"""

    root = Path(root)
    path = root / CATALOG_RELATIVE
    raw = path.read_bytes() if path.is_file() and not path.is_symlink() else None
    if raw is None:
        raise TerminalStateError(f"Claude Release 目录不是可信普通文件：{path}")
    document = _read_json(path, "Claude Release 目录")
    if (
        not isinstance(document, Mapping)
        or set(document) != {"schema_version", "persona", "source", "releases", "selectors"}
        or document.get("schema_version") != CATALOG_SCHEMA
        or document.get("persona") != CATALOG_PERSONA
        or not str(document.get("source", "")).strip()
        or not isinstance(document.get("releases"), list)
        or not document["releases"]
    ):
        raise TerminalStateError("Claude Release 目录顶层身份非法")
    releases: dict[str, dict[str, Any]] = {}
    for entry in document["releases"]:
        release = _release(root, entry)
        if release["release_sha256"] in releases:
            raise TerminalStateError("Claude Release 目录 Release 摘要重复")
        releases[release["release_sha256"]] = release
    selectors = document.get("selectors")
    if not isinstance(selectors, Mapping) or set(selectors) != {"validation_candidate", "production_active", "production_rollback"}:
        raise TerminalStateError("Claude Release 目录 selector 不闭合")

    def release_selector(role: str, *, require_approval: bool) -> dict[str, Any]:
        selector = selectors[role]
        if (
            not isinstance(selector, Mapping)
            or selector.get("kind") != "release"
            or not SHA256_RE.fullmatch(str(selector.get("release_sha256", "")))
            or not str(selector.get("changeset", "")).strip()
            or "deployment" in selector
        ):
            raise TerminalStateError(f"Claude {role} selector 非法")
        if selector["release_sha256"] not in releases:
            raise TerminalStateError(f"Claude {role} 引用了未知 Release")
        approval = selector.get("approval")
        if not require_approval:
            if approval is not None:
                raise TerminalStateError("Claude validation candidate 不得携带生产批准")
            return {"changeset": str(selector["changeset"]).strip(), "release": releases[selector["release_sha256"]]}
        if not isinstance(approval, Mapping) or set(approval) != {"path", "sha256"}:
            raise TerminalStateError("Claude production active 缺少 Approval")
        approval_path, approval_digest = str(approval["path"]), str(approval["sha256"])
        if (
            not SHA256_RE.fullmatch(approval_digest)
            or PurePosixPath(approval_path).as_posix() != approval_path
            or not approval_path.startswith(APPROVAL_PREFIX)
            or not approval_path.endswith(".json")
            or ".." in PurePosixPath(approval_path).parts
        ):
            raise TerminalStateError("Claude production selector Approval 引用非法")
        target = root / OFFICIALEGRESS_RELATIVE / approval_path
        if target.is_symlink() or not target.is_file() or _sha256(target.read_bytes()) != approval_digest:
            raise TerminalStateError("Claude production selector Approval 摘要不一致")
        return {
            "changeset": str(selector["changeset"]).strip(),
            "release": releases[selector["release_sha256"]],
            "approval": {"path": approval_path, "sha256": approval_digest},
            "approval_document": _read_json(target, "Claude 生产批准"),
        }

    candidate = release_selector("validation_candidate", require_approval=False)
    active = release_selector("production_active", require_approval=True)
    if candidate["changeset"] == active["changeset"]:
        raise TerminalStateError("Claude candidate 与 production active changeset 被折叠")
    rollback = selectors["production_rollback"]
    deployment = rollback.get("deployment") if isinstance(rollback, Mapping) else None
    receipt = deployment.get("receipt") if isinstance(deployment, Mapping) else None
    if (
        not isinstance(rollback, Mapping)
        or rollback.get("kind") != "operational-deployment"
        or rollback.get("release_sha256")
        or rollback.get("changeset")
        or rollback.get("approval") is not None
        or not isinstance(deployment, Mapping)
        or not COMMIT_RE.fullmatch(str(deployment.get("commit", "")))
        or not str(deployment.get("image_digest", "")).startswith("sha256:")
        or not SHA256_RE.fullmatch(str(deployment.get("image_digest", ""))[len("sha256:"):])
        or not isinstance(receipt, Mapping)
        or not str(receipt.get("path", "")).startswith(MAINTENANCE_RELATIVE + "/")
        or not SHA256_RE.fullmatch(str(receipt.get("sha256", "")))
    ):
        raise TerminalStateError("Claude production rollback 必须是明确操作部署")
    approval_document = active["approval_document"]
    target = approval_document.get("target") if isinstance(approval_document, Mapping) else None
    approval_id = str(approval_document.get("approval_id", "")).strip() if isinstance(approval_document, Mapping) else ""
    release = active["release"]
    if (
        not approval_id
        or approval_document.get("schema_version") != APPROVAL_SCHEMA
        or approval_document.get("status") != "approved"
        or not isinstance(target, Mapping)
        or target.get("product") != CATALOG_PERSONA
        or target.get("version") != release["version"]
        or target.get("platform") != release["platform"]
        or target.get("models") != release["_models"]
        or target.get("required_rules") != release["_required_rule_count"]
        or target.get("profile_sha256") != release["profile"]["sha256"]
        or target.get("wire_sha256") != release["wire"]["sha256"]
        or target.get("release_artifact_sha256") != release["release_sha256"]
        or target.get("release_bundle_sha256") != release["bundle_sha256"]
    ):
        raise TerminalStateError("Claude 生产批准与 production_active 选中的 Release 身份不一致")
    return {
        "catalog": {"path": CATALOG_RELATIVE, "sha256": _sha256(raw)},
        "source": str(document["source"]),
        "version": release["version"],
        "production_active": {"release_sha256": release["release_sha256"], "changeset": active["changeset"], "approval": active["approval"]},
        "release": _public_release(release),
        "approval_id": approval_id,
    }


def is_pre_contract_baseline(state: Mapping[str, Any], baseline: Mapping[str, str] = PRE_CONTRACT_BASELINE) -> bool:
    """production_active 精确等于合同生效前的基线（版本、release 摘要、Approval 摘要三项全等）。

    ``baseline`` 只给自测与单元测试换成合成基线；门禁一律用 ``PRE_CONTRACT_BASELINE``。
    """

    return (
        state["version"] == baseline["version"]
        and state["production_active"]["release_sha256"] == baseline["release_sha256"]
        and state["production_active"]["approval"]["sha256"] == baseline["approval_sha256"]
    )


def _archived_repo_bytes(root: Path, receipt_relative: str, expected_path: str) -> bytes | None:
    """从终态收据首次入库的提交读取会被后继升级换掉的目录类制品；找不到唯一提交时返回 None。"""

    try:
        history = subprocess.run(
            ["git", "log", "--diff-filter=A", "--format=%H", "--", receipt_relative],
            cwd=root, check=True, capture_output=True, text=True,
        ).stdout.splitlines()
    except (OSError, subprocess.CalledProcessError):
        return None
    if len(history) != 1 or not COMMIT_RE.fullmatch(history[0]):
        return None
    # ``./`` 让路径相对于 ``root``（仓库根不是 Git 顶层时也不会错位）。
    archived = subprocess.run(["git", "show", f"{history[0]}:./{expected_path}"], cwd=root, check=False, capture_output=True)
    return archived.stdout if archived.returncode == 0 else None


def _repo_binding(
    root: Path,
    binding: Any,
    label: str,
    *,
    receipt_relative: str,
    allow_historical: bool,
    required_prefix: str | None = None,
) -> str:
    """终态收据里的仓库制品坐标：相对路径、摘要一致（目录类绑定允许按收据首次入库提交回读）；返回相对路径。"""

    if not isinstance(binding, Mapping) or not isinstance(binding.get("path"), str) or not SHA256_RE.fullmatch(str(binding.get("sha256", ""))):
        raise TerminalStateError(f"{label} 坐标字段非法")
    relative = str(binding["path"])
    if relative.startswith("/") or ".." in PurePosixPath(relative).parts:
        raise TerminalStateError(f"{label} 坐标不是仓库相对路径：{relative}")
    if required_prefix and not relative.startswith(required_prefix):
        raise TerminalStateError(f"{label} 必须位于 {required_prefix}：{relative}")
    current = root / relative
    if current.is_file() and not current.is_symlink() and _sha256(current.read_bytes()) == binding["sha256"]:
        return relative
    if allow_historical:
        archived = _archived_repo_bytes(root, receipt_relative, relative)
        if archived is not None and _sha256(archived) == binding["sha256"]:
            return relative
    raise TerminalStateError(f"{label} 摘要漂移：{relative}")


def validate_claude_terminal_state_document(
    receipt: Any,
    receipt_relative: str,
    *,
    root: Path = ROOT,
    catalog_state: Mapping[str, Any] | None = None,
) -> str:
    """只读校验一份 Claude 终态收据（待发布候选或已入库的收据）；返回目标版本。不签发、不写文件。

    当它就是当前 production_active 的版本时，另核它登记的 selector 与 Release 与当前目录逐项相等。
    """

    root = Path(root)
    state = dict(catalog_state) if catalog_state is not None else release_catalog_state(root)
    if not isinstance(receipt, Mapping):
        raise TerminalStateError(f"终态收据不是对象：{receipt_relative}")
    matched = SCHEMA_RE.fullmatch(str(receipt.get("schema_version", "")))
    if matched is None:
        raise TerminalStateError(f"终态收据 schema 非法：{receipt_relative}")
    version = matched.group(1)
    missing = sorted(REQUIRED_FIELDS - set(receipt))
    unknown = sorted(key for key in set(receipt) - REQUIRED_FIELDS if key not in NOTE_FIELDS and not key.endswith("_note"))
    if missing or unknown:
        raise TerminalStateError(f"{version} 终态收据字段不闭合：缺 {missing or '无'}，多 {unknown or '无'}")
    target = receipt["target"]
    if (
        not isinstance(target, Mapping)
        or target.get("version") != version
        or not VERSION_RE.fullmatch(str(target.get("previous_version", "")))
        or target.get("previous_version") == version
    ):
        raise TerminalStateError(f"{version} 终态收据 target 与 schema 不一致：{receipt_relative}")
    if receipt["result"] != "passed" or not str(receipt["completed_at_utc"]).strip():
        raise TerminalStateError(f"{version} 终态收据结果或完成时间非法")
    identity = str(receipt["identity_sha256"])
    if identity not in {
        claude_terminal_state_identity(receipt, trailing_newline=False),
        claude_terminal_state_identity(receipt, trailing_newline=True),
    }:
        raise TerminalStateError(f"{version} 终态收据自摘要不一致：{receipt_relative}")
    catalog = receipt["release_catalog"]
    if not isinstance(catalog, Mapping) or set(catalog) != {"catalog", "production_active", "release"}:
        raise TerminalStateError(f"{version} 终态收据 release_catalog 字段不闭合")
    _repo_binding(root, catalog["catalog"], f"{version} 终态 Claude Release 目录", receipt_relative=receipt_relative, allow_historical=True)
    active = catalog["production_active"]
    release = catalog["release"]
    if (
        not isinstance(active, Mapping)
        or set(active) != {"release_sha256", "changeset", "approval"}
        or not isinstance(release, Mapping)
        or set(release) != {"version", "platform", "release_sha256", "bundle_sha256", "profile", "wire"}
        or release.get("version") != version
        or active.get("release_sha256") != release.get("release_sha256")
    ):
        raise TerminalStateError(f"{version} 终态收据登记的 production_active 与 Release 不一致")
    for key, binding in (("Approval", active.get("approval")), ("Profile", release.get("profile")), ("Wire", release.get("wire"))):
        if not isinstance(binding, Mapping) or set(binding) != {"path", "sha256"}:
            raise TerminalStateError(f"{version} 终态收据 {key} 绑定字段不闭合")
        _repo_binding(
            root,
            {"path": f"{OFFICIALEGRESS_RELATIVE}/{binding['path']}", "sha256": binding["sha256"]},
            f"{version} 终态 {key}",
            receipt_relative=receipt_relative,
            allow_historical=True,
        )
    for key in STAGE_RECEIPT_FIELDS:
        _repo_binding(
            root, receipt[key], f"{version} 终态 {key} 收据",
            receipt_relative=receipt_relative, allow_historical=False, required_prefix=MAINTENANCE_RELATIVE + "/",
        )
    archive = receipt["evidence_archive"]
    if (
        not isinstance(archive, Mapping)
        or set(archive) != {"path", "sha256", "bytes"}
        or not str(archive.get("path", "")).strip()
        or not SHA256_RE.fullmatch(str(archive.get("sha256", "")))
        or isinstance(archive.get("bytes"), bool)
        or not isinstance(archive.get("bytes"), int)
        or archive["bytes"] <= 0
    ):
        raise TerminalStateError(f"{version} 终态收据缺少私有证据归档坐标")
    chain = receipt["campaign_chain"]
    if (
        not isinstance(chain, list)
        or not chain
        or any(not isinstance(link, Mapping) or not str(link.get("campaign_id", "")).strip() or not str(link.get("role", "")).strip() for link in chain)
        or not any(link.get("role") == CAMPAIGN_OFFICIAL_ROLE for link in chain)
    ):
        raise TerminalStateError(f"{version} 终态收据缺少 Campaign 承接链或官方证据来源")
    if version == state["version"]:
        expected_active = {
            "release_sha256": state["production_active"]["release_sha256"],
            "changeset": state["production_active"]["changeset"],
            "approval": dict(state["production_active"]["approval"]),
        }
        if dict(active) != expected_active or {key: release[key] for key in release} != dict(state["release"]):
            raise TerminalStateError(f"{version} 终态收据登记的 production_active 或 Release 与当前 Claude Release 目录不一致")
    return version


def terminal_receipts(root: Path = ROOT) -> list[tuple[Path, dict[str, Any]]]:
    """``docs/egress/maintenance`` 下全部 Claude 终态收据（按文件名排序）。"""

    maintenance = Path(root) / MAINTENANCE_RELATIVE
    if not maintenance.is_dir():
        return []
    return [(path, _read_json(path, "Claude 终态收据")) for path in sorted(maintenance.glob(RECEIPT_GLOB))]


def current_terminal_receipt(
    root: Path = ROOT,
    *,
    baseline: Mapping[str, str] = PRE_CONTRACT_BASELINE,
) -> tuple[dict[str, Any], tuple[Path, dict[str, Any]] | None]:
    """当前 production_active 的状态与它的终态收据（豁免基线时为 None）；全部收据逐份校验，当前版本必须恰好一份。"""

    root = Path(root)
    state = release_catalog_state(root)
    by_version: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path, receipt in terminal_receipts(root):
        relative = path.relative_to(root).as_posix()
        version = validate_claude_terminal_state_document(receipt, relative, root=root, catalog_state=state)
        if version in by_version:
            raise TerminalStateError(f"Claude {version} 有多份终态收据：{by_version[version][0].name}、{path.name}")
        by_version[version] = (path, receipt)
    current = by_version.get(state["version"])
    if current is None:
        if is_pre_contract_baseline(state, baseline):
            return state, None
        raise TerminalStateError(f"当前 Claude production_active {state['version']} 缺少终态收据")
    return state, current


def validate_claude_terminal_state_receipts(
    root: Path = ROOT,
    *,
    baseline: Mapping[str, str] = PRE_CONTRACT_BASELINE,
) -> dict[str, Any]:
    """当前生产终态门禁（见模块说明第三节）：返回门禁结论；任何不满足都抛 ``TerminalStateError``。"""

    root = Path(root)
    state, current = current_terminal_receipt(root, baseline=baseline)
    return {
        "client": "claude",
        "production_active_version": state["version"],
        "status": "pre_contract_baseline" if current is None else "validated",
        "receipt": None if current is None else current[0].relative_to(root).as_posix(),
        "validated_receipts": [path.relative_to(root).as_posix() for path, _receipt in terminal_receipts(root)],
    }


# ---------------------------------------------------------------------------
# 四、自测夹具：合成 Claude Release 目录、阶段收据与终态收据（不碰真实目录）
# ---------------------------------------------------------------------------

FIXTURE_PLATFORM = "linux/arm64"
FIXTURE_MODELS = ("fixture-model-a", "fixture-model-b")
FIXTURE_ROLLBACK_RECEIPT = f"{MAINTENANCE_RELATIVE}/claude-fixture-rollback-acceptance.json"


def _fixture_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _write_fixture(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def fixture_receipt_name(previous_version: str, version: str) -> str:
    """终态收据的规范文件名：``CLAUDE_CODE_<前一版本去点>_TO_<目标版本去点>_TERMINAL_STATE_RECEIPT.json``。"""

    return f"{RECEIPT_PREFIX}{previous_version.replace('.', '')}_TO_{version.replace('.', '')}{RECEIPT_SUFFIX}"


def write_fixture_catalog(root: Path, version: str, *, approval_tag: str = "v1") -> dict[str, Any]:
    """在 ``root`` 下写一份自洽的 Claude Release 目录：production_active 与 validation_candidate 都选 ``version`` 的合成 Release；
    返回 ``release_catalog_state``。已存在的其它版本 blob 与批准文件原样保留（模拟换版后历史制品留在目录里）。"""

    root = Path(root)
    base = root / OFFICIALEGRESS_RELATIVE
    capability = _sha256(f"fixture-capability:{version}".encode())
    profile = {
        "identity": {
            "version": version,
            "platform": FIXTURE_PLATFORM,
            "supported_models": list(FIXTURE_MODELS),
            "model_capability_catalog_sha256": capability,
        },
        "rules": [{"rule_id": "SPEC-FIXTURE-001"}],
        "strict_endpoints": [{"egress_id": "fixture-egress"}],
    }
    profile_bytes = _fixture_bytes(profile)
    profile_ref = {"path": f"catalogdata/claude/profiles/{version}/{_sha256(profile_bytes)}.json", "sha256": _sha256(profile_bytes)}
    _write_fixture(base / profile_ref["path"], profile_bytes)
    wire = {
        "identity": {
            "version": version,
            "platform": FIXTURE_PLATFORM,
            "profile_digest": profile_ref["sha256"],
            "model_capability_catalog_digest": capability,
        },
        "implementation_policy": {
            "model_catalog": {"required_rule_count": 1, "models": [{"canonical_model": model} for model in FIXTURE_MODELS]}
        },
    }
    wire_bytes = _fixture_bytes(wire)
    wire_ref = {"path": f"catalogdata/claude/wire/{version}/{_sha256(wire_bytes)}.json", "sha256": _sha256(wire_bytes)}
    _write_fixture(base / wire_ref["path"], wire_bytes)
    rules_manifest = _sha256(f"fixture-rules:{version}".encode())
    release_digest = _canonical_catalog_digest(
        {
            "schema_version": RELEASE_SCHEMA,
            "version": version,
            "platform": FIXTURE_PLATFORM,
            "profile_sha256": profile_ref["sha256"],
            "wire_sha256": wire_ref["sha256"],
            "model_capability_catalog_sha256": capability,
            "required_rules_manifest_sha256": rules_manifest,
            "required_rule_count": 1,
        }
    )
    bundle_digest = _canonical_catalog_digest(
        {
            "schema_version": BUNDLE_SCHEMA,
            "persona": BUNDLE_PERSONA,
            "release_sha256": release_digest,
            "profile_sha256": profile_ref["sha256"],
            "wire_sha256": wire_ref["sha256"],
        }
    )
    approval_id = f"claude-fixture-{version}-{approval_tag}"
    approval = {
        "schema_version": APPROVAL_SCHEMA,
        "approval_id": approval_id,
        "status": "approved",
        "target": {
            "product": CATALOG_PERSONA,
            "version": version,
            "platform": FIXTURE_PLATFORM,
            "models": list(FIXTURE_MODELS),
            "required_rules": 1,
            "profile_sha256": profile_ref["sha256"],
            "wire_sha256": wire_ref["sha256"],
            "release_artifact_sha256": release_digest,
            "release_bundle_sha256": bundle_digest,
        },
    }
    approval_bytes = _fixture_bytes(approval)
    approval_ref = {"path": f"{APPROVAL_PREFIX}claude-fixture-{version}-{approval_tag}-approval.json", "sha256": _sha256(approval_bytes)}
    _write_fixture(base / approval_ref["path"], approval_bytes)
    rollback = root / FIXTURE_ROLLBACK_RECEIPT
    if not rollback.is_file():
        _write_fixture(rollback, _fixture_bytes({"fixture": "rollback acceptance"}))
    catalog = {
        "schema_version": CATALOG_SCHEMA,
        "persona": CATALOG_PERSONA,
        "source": f"approval:{approval_id}",
        "releases": [
            {
                "version": version,
                "platform": FIXTURE_PLATFORM,
                "profile": profile_ref,
                "wire": wire_ref,
                "required_rules_manifest_sha256": rules_manifest,
                "release_sha256": release_digest,
                "bundle_sha256": bundle_digest,
                "required_rule_count": 1,
                "profile_atomic_assertion_count": 1,
                "local_scenario_assertion_count": 1,
                "strict_endpoint_count": 1,
                "models": list(FIXTURE_MODELS),
                "model_evidence": {"successful_attempts": 1, "historical_failed_attempts": 0},
            }
        ],
        "selectors": {
            "validation_candidate": {"kind": "release", "release_sha256": release_digest, "changeset": f"claude-fixture-{version}-candidate"},
            "production_active": {
                "kind": "release",
                "release_sha256": release_digest,
                "changeset": f"claude-fixture-{version}-production",
                "approval": approval_ref,
            },
            "production_rollback": {
                "kind": "operational-deployment",
                "deployment": {
                    "commit": "0" * 40,
                    "image_digest": "sha256:" + "1" * 64,
                    "receipt": {"path": FIXTURE_ROLLBACK_RECEIPT, "sha256": _sha256(rollback.read_bytes())},
                },
            },
        },
    }
    _write_fixture(root / CATALOG_RELATIVE, _fixture_bytes(catalog))
    return release_catalog_state(root)


def write_fixture_stage_receipts(root: Path, version: str) -> dict[str, dict[str, str]]:
    """写四份合成阶段收据（``STAGE_RECEIPT_FIELDS``），返回终态收据要登记的绑定。"""

    bindings: dict[str, dict[str, str]] = {}
    for field in STAGE_RECEIPT_FIELDS:
        relative = f"{MAINTENANCE_RELATIVE}/claude-fixture-{version}-{field.replace('_', '-')}.json"
        data = _fixture_bytes({"fixture": field, "version": version})
        _write_fixture(Path(root) / relative, data)
        bindings[field] = {"path": relative, "sha256": _sha256(data)}
    return bindings


def fixture_terminal_receipt(
    root: Path,
    state: Mapping[str, Any],
    *,
    previous_version: str,
    campaign_id: str = "c-claude-fixture-final",
    official_campaign_id: str = "c-claude-fixture-official",
) -> dict[str, Any]:
    """按当前目录状态与合成阶段收据生成一份带自摘要的终态收据（不写文件）。"""

    version = str(state["version"])
    document: dict[str, Any] = {
        "schema_version": f"official-client-claude-{version}-terminal-state/v1",
        "target": {"version": version, "previous_version": previous_version},
        "result": "passed",
        "completed_at_utc": "2026-10-08T00:00:00Z",
        "release_catalog": {
            "catalog": dict(state["catalog"]),
            "production_active": json.loads(json.dumps(state["production_active"])),
            "release": json.loads(json.dumps(state["release"])),
        },
        "campaign_chain": [
            {"campaign_id": official_campaign_id, "role": CAMPAIGN_OFFICIAL_ROLE, "order": 1},
            {"campaign_id": campaign_id, "role": "production_replacement", "order": 2},
        ],
        "evidence_archive": {"path": f"/fixture/private/{campaign_id}.tar", "sha256": _sha256(campaign_id.encode()), "bytes": 1},
        **write_fixture_stage_receipts(root, version),
        "identity_algorithm": "sha256(json.dumps(payload_without_identity_sha256, ensure_ascii=False, sort_keys=True, separators=(',', ':')))",
    }
    document["identity_sha256"] = claude_terminal_state_identity(document)
    return document


def write_fixture_receipt(root: Path, receipt: Mapping[str, Any]) -> Path:
    """把终态收据写到规范坐标，返回路径。"""

    target = receipt["target"]
    path = Path(root) / MAINTENANCE_RELATIVE / fixture_receipt_name(str(target["previous_version"]), str(target["version"]))
    _write_fixture(path, _fixture_bytes(dict(receipt)))
    return path


def fixture_baseline(state: Mapping[str, Any]) -> dict[str, str]:
    """把合成目录的 production_active 当作基线（只给自测与单元测试验证豁免判据用）。"""

    return {
        "version": str(state["version"]),
        "release_sha256": str(state["production_active"]["release_sha256"]),
        "approval_sha256": str(state["production_active"]["approval"]["sha256"]),
        "reason": "自测合成基线",
    }


def _reseal(path: Path, mutate: Any) -> None:
    receipt = json.loads(path.read_text(encoding="utf-8"))
    mutate(receipt)
    receipt["identity_sha256"] = claude_terminal_state_identity(receipt)
    path.write_bytes(_fixture_bytes(receipt))


def self_test() -> tuple[int, int]:
    """合成夹具上的正负例；任一例不符抛 ``AssertionError``。返回 (正例数, 负例数)。"""

    import tempfile

    positives = negatives = 0

    def expect_failure(label: str, root: Path, **kwargs: Any) -> None:
        nonlocal negatives
        try:
            validate_claude_terminal_state_receipts(root, **kwargs)
        except TerminalStateError:
            negatives += 1
            return
        raise AssertionError(f"Claude 终态门禁自测负例未失败关闭：{label}")

    def fresh(base: Path, name: str) -> tuple[Path, dict[str, Any], Path]:
        root = base / name
        state = write_fixture_catalog(root, "9.9.9")
        path = write_fixture_receipt(root, fixture_terminal_receipt(root, state, previous_version="9.9.8"))
        return root, state, path

    with tempfile.TemporaryDirectory() as directory:
        base = Path(directory)

        # 正例 1：非基线 production_active 恰有一份合规终态收据。
        root, _state, path = fresh(base, "ok")
        report = validate_claude_terminal_state_receipts(root)
        if report["status"] != "validated" or report["receipt"] != path.relative_to(root).as_posix():
            raise AssertionError(f"Claude 终态门禁自测正例结果异常：{report}")
        positives += 1

        # 正例 2：production_active 精确等于（合成）基线时不要求终态收据。
        root = base / "baseline"
        state = write_fixture_catalog(root, "9.9.9")
        report = validate_claude_terminal_state_receipts(root, baseline=fixture_baseline(state))
        if report["status"] != "pre_contract_baseline" or report["receipt"] is not None:
            raise AssertionError(f"Claude 终态门禁自测正例（基线豁免）结果异常：{report}")
        positives += 1

        # 正例 3：生产不变、目录文件因候选 selector 换了 changeset 而变化，当前收据仍有效。
        root, _state, _path = fresh(base, "candidate-selector")
        catalog_path = root / CATALOG_RELATIVE
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["selectors"]["validation_candidate"]["changeset"] = "claude-fixture-next-candidate"
        catalog_path.write_bytes(_fixture_bytes(catalog))
        # 不在 Git 仓库里，目录绑定无法按入库提交回读：收据登记的目录摘要换成当前值后重封（只验证 selector 判据本身）。
        _reseal(_path, lambda receipt: receipt["release_catalog"]["catalog"].update(sha256=_sha256(catalog_path.read_bytes())))
        if validate_claude_terminal_state_receipts(root)["status"] != "validated":
            raise AssertionError("Claude 终态门禁自测正例（候选 selector 变化）结果异常")
        positives += 1

        # 负例 1：当前 production_active 缺少终态收据。
        root, _state, path = fresh(base, "missing")
        path.unlink()
        expect_failure("缺少当前终态收据", root)

        # 负例 2：自摘要被伪造。
        root, _state, path = fresh(base, "identity")
        forged = json.loads(path.read_text(encoding="utf-8"))
        forged["identity_sha256"] = "0" * 64
        path.write_bytes(_fixture_bytes(forged))
        expect_failure("伪造自摘要", root)

        # 负例 3：收据不是合法 JSON（损坏）。
        root, _state, path = fresh(base, "corrupt")
        path.write_text("{", encoding="utf-8")
        expect_failure("收据损坏", root)

        # 负例 4：登记的 production_active changeset 与当前目录不一致（重封后自摘要仍然成立）。
        root, _state, path = fresh(base, "selector")
        _reseal(path, lambda receipt: receipt["release_catalog"]["production_active"].update(changeset="other-changeset"))
        expect_failure("production_active 不一致", root)

        # 负例 5：阶段收据漂移。
        root, _state, path = fresh(base, "stage-drift")
        stage = root / json.loads(path.read_text(encoding="utf-8"))["deployment_fact"]["path"]
        stage.write_bytes(stage.read_bytes() + b" ")
        expect_failure("阶段收据漂移", root)

        # 负例 6：同一版本两份终态收据。
        root, _state, path = fresh(base, "duplicate")
        duplicate = path.with_name(fixture_receipt_name("9.9.7", "9.9.9"))
        _write_fixture(duplicate, path.read_bytes())
        _reseal(duplicate, lambda receipt: receipt["target"].update(previous_version="9.9.7"))
        expect_failure("同一版本两份终态收据", root)

        # 负例 7：基线三项只差 Approval 摘要，不豁免。
        root = base / "baseline-approval"
        state = write_fixture_catalog(root, "9.9.9")
        near = {**fixture_baseline(state), "approval_sha256": "f" * 64}
        expect_failure("基线 Approval 摘要不同", root, baseline=near)

        # 负例 8：Release 目录被改坏（release 摘要不再可复算）。
        root, _state, _path = fresh(base, "catalog")
        catalog_path = root / CATALOG_RELATIVE
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
        catalog["releases"][0]["required_rules_manifest_sha256"] = "e" * 64
        catalog_path.write_bytes(_fixture_bytes(catalog))
        expect_failure("Release 目录摘要不可复算", root)

        # 负例 9：未知字段与缺少官方证据来源。
        root, _state, path = fresh(base, "unknown")
        _reseal(path, lambda receipt: receipt.update(unexpected="x"))
        expect_failure("收据含未知字段", root)
        root, _state, path = fresh(base, "official")
        _reseal(path, lambda receipt: receipt["campaign_chain"][0].update(role="production_replacement"))
        expect_failure("缺少官方证据来源", root)

    return positives, negatives


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Claude 终态收据门禁（B5b-06）")
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根；缺省按本文件位置推断")
    parser.add_argument("--self-test", action="store_true", help="只运行合成夹具上的正负例")
    arguments = parser.parse_args(argv)
    try:
        if arguments.self_test:
            positives, negatives = self_test()
            print(f"Claude 终态门禁自测通过：正例 {positives} 项、负例 {negatives} 项")
            return 0
        print(json.dumps(validate_claude_terminal_state_receipts(arguments.root.resolve()), ensure_ascii=False))
        return 0
    except (TerminalStateError, AssertionError) as error:
        print(f"🔴 Claude 终态门禁失败：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
