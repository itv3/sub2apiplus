#!/usr/bin/env python3
"""按验收契约编排逐规则断言，汇总为 accept 所需的 v2 验收结果文档。

§10.8.10 验收模型：每条规则的 ``validation_mode`` 由冻结验收契约
（``acceptance_contract.py``）从批准断言画像机器推导，禁止手写——

- ``dual_wire``（25 条 wire 规则）：官方／候选两侧各执行一次单规则断言，
  双侧 check 集合必须与契约 check 全集逐项一致；
- ``candidate_profile``（17 条内部规则）：只在候选侧执行机器断言；官方权威
  是批准画像链，行内逐字绑定批准断言画像 SHA-256、classification package
  digest 与联合 ``review_sha256``，不再伪造官方侧机器结果。

v1 的人工 ``positive_assertions``／``negative_assertions`` 已废除：accept 从
批准画像复算应有 check ID 并离线重放，正负语义由画像判据本身表达。

evidence refs 以 ``<evidence_prefix>/<相对路径>`` 的 inventory 逻辑路径写入
（§10.8.5 的路径空间统一），accept 端只做精确路径＋摘要匹配。任一侧断言
失败即整体失败：schema 只接受 ``status: "pass"``，把失败规则写进文档等于
伪造验收结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.official_client_capture.candidate_rule_assertion import (  # noqa: E402
    build_assertion_command,
)
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


def resolve_machine_layout(
    config: Mapping[str, Any], results_dir: Path
) -> tuple[Path, Path, Path]:
    """解析机器结果落位与 Campaign 逻辑路径根。

    未声明 ``campaign_dir`` 时保留旧的平铺布局，供独立离线编排使用。正式
    Campaign 必须声明该字段，机器结果随即严格落在 compare 已冻结的
    ``assertions/<candidate-id>/machine/{official,candidate}/``，且收据路径相对
    Campaign 根绑定，确保 builder 输出可被 accept 逐字重放。
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
    expected_root = campaign_root / "assertions" / candidate_id / "machine"
    if results_dir.resolve() != expected_root.resolve():
        raise RuleAssertionError(
            "results-dir 必须等于 Campaign 的 assertions/<candidate-id>/machine"
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
) -> tuple[list[str], dict[str, Any]]:
    """执行单规则断言并返回**与 accept 期望逐字一致**的命令。

    命令必须由 `candidate_rule_assertion.build_assertion_command` 这一权威
    构造器产出：accept 用同一构造器复算期望命令并逐元素比对，编排器另造一套
    参数形态（解释器路径、executor 绝对路径、缺 profile／rule-manifest）会让
    结果文档永远无法通过 accept——这是 builder → accept 此前未集成的表现之一。
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
    if completed.returncode != 0:
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
    if document.get("status") != "pass":
        raise RuleAssertionError(
            f"{rule_id} 的断言未通过：{document.get('status')}"
        )
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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="按验收契约编排逐规则断言并汇总为 v2 验收结果"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    arguments = parser.parse_args()

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
            resolve_machine_layout(config, results_dir)
        )
    except RuleAssertionError as error:
        raise SystemExit(f"配置非法：{error}") from error
    formal_campaign_layout = results_root != results_dir

    rule_results: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        mode = validation_modes[rule_id]
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
    arguments.output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
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
