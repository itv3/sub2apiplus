"""升级前基线验收收据的生成与校验。

基线收据把功能门禁、证据门禁和已知历史漂移绑定到同一个不可变的
commit/tree/tool-bundle 身份。它不替代实际测试，也不会把功能失败标记为
“已知漂移”；它只允许明确登记的历史证据漂移进入后续差异比较。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .canonical import (
    bind_identity,
    expect_exact_fields,
    expect_git_object,
    expect_object,
    expect_safe_id,
    expect_sha256,
    expect_string,
    load_json,
    safe_relative_path,
    sha256_file,
    validate_identity,
    write_json_once,
)
from .errors import UpstreamMergeError
from .gitops import assert_clean, assert_git_repository, assert_private_path, commit_tree, rev_parse, tool_bundle


BASELINE_INPUT_SCHEMA = "official-egress-upstream-baseline-acceptance-input/v1"
BASELINE_ACCEPTANCE_SCHEMA = "official-egress-upstream-baseline-acceptance/v1"

_CHECK_STATUSES = {"passed", "known_drift"}


def _validate_check(value: Any, label: str, *, allow_known_drift: bool) -> dict[str, Any]:
    check = expect_object(value, label)
    expected = {"id", "status", "command", "observed_at_utc", "failure_ids"}
    expect_exact_fields(check, expected, label)
    expect_safe_id(check.get("id"), f"{label}.id")
    status = expect_string(check.get("status"), f"{label}.status")
    allowed = _CHECK_STATUSES if allow_known_drift else {"passed"}
    if status not in allowed:
        raise UpstreamMergeError(
            f"{label}.status 非法：{status}，允许={sorted(allowed)}"
        )
    expect_string(check.get("command"), f"{label}.command")
    expect_string(check.get("observed_at_utc"), f"{label}.observed_at_utc")
    failure_ids = check.get("failure_ids")
    if not isinstance(failure_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in failure_ids
    ):
        raise UpstreamMergeError(f"{label}.failure_ids 必须是字符串数组")
    if failure_ids != sorted(set(failure_ids)):
        raise UpstreamMergeError(f"{label}.failure_ids 必须排序且不得重复")
    if status == "passed" and failure_ids:
        raise UpstreamMergeError(f"{label}.通过检查不得包含 failure_ids")
    if status == "known_drift" and not failure_ids:
        raise UpstreamMergeError(f"{label}.known_drift 必须包含 failure_ids")
    return check


def _validate_drift(value: Any, label: str) -> dict[str, Any]:
    drift = expect_object(value, label)
    expect_exact_fields(
        drift,
        {
            "id",
            "path",
            "prior_sha256",
            "current_sha256",
            "source_receipts",
            "reason",
        },
        label,
    )
    expect_safe_id(drift.get("id"), f"{label}.id")
    safe_relative_path(drift.get("path"), f"{label}.path")
    prior = expect_sha256(drift.get("prior_sha256"), f"{label}.prior_sha256")
    current = expect_sha256(drift.get("current_sha256"), f"{label}.current_sha256")
    if prior == current:
        raise UpstreamMergeError(f"{label} 的前后摘要不得相同")
    receipts = drift.get("source_receipts")
    if not isinstance(receipts, list) or not receipts or any(
        not isinstance(item, str) or not item.strip() for item in receipts
    ):
        raise UpstreamMergeError(f"{label}.source_receipts 必须是非空字符串数组")
    if receipts != sorted(set(receipts)):
        raise UpstreamMergeError(f"{label}.source_receipts 必须排序且不得重复")
    expect_string(drift.get("reason"), f"{label}.reason")
    return drift


def _validate_draft(value: Any, label: str = "BaselineAcceptanceInput") -> dict[str, Any]:
    draft = expect_object(value, label)
    expect_exact_fields(
        draft,
        {"schema_version", "functional_checks", "evidence_checks", "known_drift"},
        label,
    )
    if draft.get("schema_version") != BASELINE_INPUT_SCHEMA:
        raise UpstreamMergeError(f"{label}.schema_version 非法")
    functional = draft.get("functional_checks")
    evidence = draft.get("evidence_checks")
    drift = draft.get("known_drift")
    if not isinstance(functional, list) or not functional:
        raise UpstreamMergeError(f"{label}.functional_checks 必须是非空数组")
    if not isinstance(evidence, list) or not evidence:
        raise UpstreamMergeError(f"{label}.evidence_checks 必须是非空数组")
    if not isinstance(drift, list):
        raise UpstreamMergeError(f"{label}.known_drift 必须是数组")
    functional_checks = [
        _validate_check(item, f"{label}.functional_checks[{index}]", allow_known_drift=False)
        for index, item in enumerate(functional)
    ]
    evidence_checks = [
        _validate_check(item, f"{label}.evidence_checks[{index}]", allow_known_drift=True)
        for index, item in enumerate(evidence)
    ]
    drift_entries = [
        _validate_drift(item, f"{label}.known_drift[{index}]")
        for index, item in enumerate(drift)
    ]
    check_ids = [item["id"] for item in functional_checks + evidence_checks]
    if check_ids != sorted(set(check_ids)):
        raise UpstreamMergeError(f"{label} 检查 id 必须全局排序且不得重复")
    drift_ids = [item["id"] for item in drift_entries]
    if drift_ids != sorted(set(drift_ids)):
        raise UpstreamMergeError(f"{label}.known_drift id 必须排序且不得重复")
    known_ids = set(drift_ids)
    referenced_ids = {
        failure_id
        for check in evidence_checks
        if check["status"] == "known_drift"
        for failure_id in check["failure_ids"]
    }
    if not referenced_ids.issubset(known_ids):
        missing = sorted(referenced_ids - known_ids)
        raise UpstreamMergeError(f"证据检查引用了未登记的 known_drift：{missing}")
    if drift_ids and not referenced_ids:
        raise UpstreamMergeError("known_drift 不得脱离 evidence_checks 单独存在")
    return {
        "functional_checks": functional_checks,
        "evidence_checks": evidence_checks,
        "known_drift": drift_entries,
    }


def seal_baseline_acceptance(
    repository: Path,
    input_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    """将已执行的基线检查草稿绑定到当前干净提交并写入不可变收据。"""

    root = assert_git_repository(repository)
    assert_clean(root, "基线验收")
    if not input_path.is_absolute():
        raise UpstreamMergeError("基线验收输入必须是绝对路径")
    if not input_path.is_file() or input_path.is_symlink():
        raise UpstreamMergeError(f"基线验收输入不是可信普通文件：{input_path}")
    if not output_path.is_absolute():
        raise UpstreamMergeError("基线验收输出必须是绝对路径")
    # 私有目录不得进入版本控制，避免污染已验收工作树或形成收据自引用。
    assert_private_path(root, output_path, "基线验收收据")
    if output_path.exists() or output_path.is_symlink():
        raise UpstreamMergeError(f"基线验收输出已存在，禁止覆盖：{output_path}")

    normalized = _validate_draft(load_json(input_path, "BaselineAcceptanceInput"))
    commit = rev_parse(root, "HEAD^{commit}")
    tree = commit_tree(root, commit)
    bundle = tool_bundle(root)
    evidence_result = "accepted_with_known_drift" if normalized["known_drift"] else "passed"
    document = bind_identity(
        {
            "schema_version": BASELINE_ACCEPTANCE_SCHEMA,
            "issued_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "repository": {"commit": commit, "tree": tree},
            "tool_bundle_sha256": bundle["bundle_sha256"],
            "functional": {"result": "passed", "checks": normalized["functional_checks"]},
            "evidence": {
                "result": evidence_result,
                "checks": normalized["evidence_checks"],
                "known_drift": normalized["known_drift"],
            },
            "result": "accepted",
        }
    )
    write_json_once(output_path, document)
    validate_baseline_acceptance(root, output_path)
    return document


def validate_baseline_acceptance(
    repository: Path,
    path: Path,
    *,
    require_current: bool = True,
) -> dict[str, Any]:
    """校验基线收据身份、门禁分类及其当前仓库绑定。"""

    root = assert_git_repository(repository)
    if not path.is_absolute():
        raise UpstreamMergeError("基线验收收据路径必须是绝对路径")
    document = expect_object(load_json(path, "BaselineAcceptance"), "BaselineAcceptance")
    expect_exact_fields(
        document,
        {
            "schema_version",
            "issued_at_utc",
            "repository",
            "tool_bundle_sha256",
            "functional",
            "evidence",
            "result",
            "identity_sha256",
        },
        "BaselineAcceptance",
    )
    if document.get("schema_version") != BASELINE_ACCEPTANCE_SCHEMA:
        raise UpstreamMergeError("BaselineAcceptance schema_version 非法")
    expect_string(document.get("issued_at_utc"), "BaselineAcceptance.issued_at_utc")
    validate_identity(document, "BaselineAcceptance")
    repository_document = expect_object(
        document.get("repository"), "BaselineAcceptance.repository"
    )
    expect_exact_fields(
        repository_document, {"commit", "tree"}, "BaselineAcceptance.repository"
    )
    commit = expect_git_object(
        repository_document.get("commit"), "BaselineAcceptance.repository.commit"
    )
    tree = expect_git_object(
        repository_document.get("tree"), "BaselineAcceptance.repository.tree"
    )
    if commit_tree(root, commit) != tree:
        raise UpstreamMergeError("BaselineAcceptance repository.tree 与 commit 不一致")
    if require_current:
        assert_clean(root, "基线验收当前仓库")
        current_commit = rev_parse(root, "HEAD^{commit}")
        current_tree = commit_tree(root, current_commit)
        if commit != current_commit or tree != current_tree:
            raise UpstreamMergeError(
                "BaselineAcceptance 未绑定当前 HEAD/tree："
                f"expected={current_commit}/{current_tree} actual={commit}/{tree}"
            )
    bundle_digest = expect_sha256(
        document.get("tool_bundle_sha256"), "BaselineAcceptance.tool_bundle_sha256"
    )
    if tool_bundle(root)["bundle_sha256"] != bundle_digest:
        raise UpstreamMergeError("BaselineAcceptance tool_bundle 摘要漂移")

    functional = expect_object(
        document.get("functional"), "BaselineAcceptance.functional"
    )
    expect_exact_fields(functional, {"result", "checks"}, "BaselineAcceptance.functional")
    if functional.get("result") != "passed":
        raise UpstreamMergeError("BaselineAcceptance.functional 必须为 passed")
    functional_checks = functional.get("checks")
    if not isinstance(functional_checks, list) or not functional_checks:
        raise UpstreamMergeError("BaselineAcceptance.functional.checks 必须是非空数组")
    for index, check in enumerate(functional_checks):
        _validate_check(
            check,
            f"BaselineAcceptance.functional.checks[{index}]",
            allow_known_drift=False,
        )

    evidence = expect_object(document.get("evidence"), "BaselineAcceptance.evidence")
    expect_exact_fields(
        evidence, {"result", "checks", "known_drift"}, "BaselineAcceptance.evidence"
    )
    evidence_result = expect_string(
        evidence.get("result"), "BaselineAcceptance.evidence.result"
    )
    if evidence_result not in {"passed", "accepted_with_known_drift"}:
        raise UpstreamMergeError("BaselineAcceptance.evidence.result 非法")
    evidence_checks = evidence.get("checks")
    known_drift = evidence.get("known_drift")
    if not isinstance(evidence_checks, list) or not evidence_checks:
        raise UpstreamMergeError("BaselineAcceptance.evidence.checks 必须是非空数组")
    if not isinstance(known_drift, list):
        raise UpstreamMergeError("BaselineAcceptance.evidence.known_drift 必须是数组")
    for index, check in enumerate(evidence_checks):
        _validate_check(
            check,
            f"BaselineAcceptance.evidence.checks[{index}]",
            allow_known_drift=True,
        )
    for index, drift in enumerate(known_drift):
        _validate_drift(drift, f"BaselineAcceptance.evidence.known_drift[{index}]")
    draft = _validate_draft(
        {
            "schema_version": BASELINE_INPUT_SCHEMA,
            "functional_checks": functional_checks,
            "evidence_checks": evidence_checks,
            "known_drift": known_drift,
        }
    )
    expected_result = "accepted_with_known_drift" if draft["known_drift"] else "passed"
    if evidence_result != expected_result:
        raise UpstreamMergeError("BaselineAcceptance.evidence.result 与 known_drift 不一致")
    if document.get("result") != "accepted":
        raise UpstreamMergeError("BaselineAcceptance.result 必须为 accepted")
    return document


def baseline_binding(path: Path) -> dict[str, Any]:
    """生成计划可用的外部基线收据绑定。"""

    if path.is_symlink() or not path.is_file():
        raise UpstreamMergeError(f"基线验收收据不是可信普通文件：{path}")
    return {
        "path": str(path.resolve(strict=True)),
        "sha256": sha256_file(path),
        "bytes": path.stat().st_size,
    }
