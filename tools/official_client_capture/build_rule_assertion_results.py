#!/usr/bin/env python3
"""编排官方／候选双侧逐规则断言，并汇总成 accept 所需的验收结果文档。

`accept` 要一份 `codex-egress-rule-assertions/v1` 的 results 文档：每条必需规则都要给出
**双侧**证据绑定、机器执行结果、执行命令与正负断言清单。仓库里只有单规则执行器
（`candidate_rule_assertion.py`）和这份 schema，没有把两者接起来的编排／汇总入口——
accept 因此从未走通。

本工具做两件事，都不替代判断：

1. 对每条规则在官方侧与候选侧各执行一次单规则断言，原样保留执行器的退出码与结果文件；
2. 把两侧结果汇总成 results 文档，正负断言清单直接取自执行器报告的 check 结果
   （`passed` 为真进 positive，为假进 negative），不做任何重写。

任一侧断言失败即整体失败：schema 只接受 `status: "pass"`，把失败规则写进文档等于伪造
验收结论。`evidence_level` 固定为 `full`——双侧都提供了完整原始证据绑定才走到这一步。
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

RESULTS_SCHEMA = "codex-egress-rule-assertions/v1"
SINGLE_SCHEMA = "codex-candidate-rule-assertion/v1"


class RuleAssertionError(RuntimeError):
    """双侧断言不足以支撑验收结论。"""


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


def run_side_assertion(
    *,
    executor: Path,
    rule_id: str,
    capture_manifest: Path,
    evidence_root: Path,
    output: Path,
    expected_codex_version: str,
    extra_arguments: list[str] | None = None,
) -> tuple[list[str], dict[str, Any]]:
    """执行一侧的单规则断言，返回命令与结果文档。"""

    command = [
        sys.executable,
        str(executor),
        "--rule-id",
        rule_id,
        "--capture-manifest",
        str(capture_manifest),
        "--evidence-root",
        str(evidence_root),
        "--expected-codex-version",
        expected_codex_version,
        "--output",
        str(output),
    ]
    command.extend(extra_arguments or [])
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
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


def split_assertions(document: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    """按执行器报告的 check 结果切分正负断言，不重写任何判断。"""

    positive: list[str] = []
    negative: list[str] = []
    for check in document.get("checks") or []:
        check_id = check.get("id")
        if not isinstance(check_id, str) or not check_id:
            raise RuleAssertionError("断言结果存在缺少 id 的 check")
        (positive if check.get("passed") else negative).append(check_id)
    if not positive and not negative:
        raise RuleAssertionError("断言结果没有任何 check，不能作为验收依据")
    return positive, negative


def collect_evidence_bindings(
    document: Mapping[str, Any],
    evidence_root: Path,
) -> list[dict[str, str]]:
    """把 check 引用到的原始证据绑定成 path+sha256，去重后排序。"""

    seen: dict[str, dict[str, str]] = {}
    for check in document.get("checks") or []:
        for reference in check.get("evidence_paths") or []:
            if not isinstance(reference, str) or not reference:
                raise RuleAssertionError("check 的 evidence_paths 含空引用")
            path = evidence_root / reference
            if not path.is_file() or path.is_symlink():
                raise RuleAssertionError(f"断言引用的证据不存在：{reference}")
            seen[reference] = _binding(path, evidence_root)
    if not seen:
        raise RuleAssertionError("断言结果未绑定任何原始证据")
    return [seen[key] for key in sorted(seen)]


def build_results_document(
    *,
    candidate_id: str,
    target_version: str,
    profile_id: str,
    profile_digest: str,
    official_package_digest: str,
    candidate_package_digest: str,
    comparison_package_digest: str,
    rules: list[dict[str, Any]],
) -> dict[str, Any]:
    if not rules:
        raise RuleAssertionError("验收结果必须至少覆盖一条规则")
    return {
        "schema_version": RESULTS_SCHEMA,
        "document_kind": "results",
        "candidate_id": candidate_id,
        "target_version": target_version,
        "profile_id": profile_id,
        "profile_digest": profile_digest,
        "official_package_digest": official_package_digest,
        "candidate_package_digest": candidate_package_digest,
        "comparison_package_digest": comparison_package_digest,
        "rules": sorted(rules, key=lambda item: item["rule"]),
    }


def build_rule_result(
    *,
    rule_id: str,
    official: tuple[list[str], dict[str, Any], Path],
    candidate: tuple[list[str], dict[str, Any], Path],
    official_root: Path,
    candidate_root: Path,
    results_root: Path,
    rationale: str,
) -> dict[str, Any]:
    official_command, official_document, official_output = official
    candidate_command, candidate_document, candidate_output = candidate

    official_positive, official_negative = split_assertions(official_document)
    candidate_positive, candidate_negative = split_assertions(candidate_document)
    # 双侧检查的语义集合必须一致，否则"官方通过、候选也通过"比较的不是同一件事。
    if sorted(official_positive) != sorted(candidate_positive) or sorted(
        official_negative
    ) != sorted(candidate_negative):
        raise RuleAssertionError(
            f"{rule_id} 的官方与候选断言集合不一致，无法作为同一规则的双侧证据"
        )

    return {
        "rule": rule_id,
        "status": "pass",
        "official_evidence_refs": collect_evidence_bindings(
            official_document, official_root
        ),
        "candidate_evidence_refs": collect_evidence_bindings(
            candidate_document, candidate_root
        ),
        "official_machine_result": _binding(official_output, results_root),
        "candidate_machine_result": _binding(candidate_output, results_root),
        "official_command": official_command,
        "candidate_command": candidate_command,
        "positive_assertions": sorted(candidate_positive),
        "negative_assertions": sorted(candidate_negative),
        "evidence_level": "full",
        "rationale": rationale,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="编排双侧逐规则断言并汇总为 accept 验收结果"
    )
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--results-dir", type=Path, required=True)
    arguments = parser.parse_args()

    try:
        config = json.loads(arguments.config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"无法读取配置：{error}") from error

    executor = Path(config["executor"]).resolve(strict=True)
    official_root = Path(config["official_evidence_root"]).resolve(strict=True)
    candidate_root = Path(config["candidate_evidence_root"]).resolve(strict=True)
    official_manifest = Path(config["official_capture_manifest"]).resolve(strict=True)
    candidate_manifest = Path(config["candidate_capture_manifest"]).resolve(strict=True)
    target_version = str(config["target_version"])
    rule_ids = list(config["rules"])

    results_dir = arguments.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)

    rule_results: list[dict[str, Any]] = []
    for rule_id in rule_ids:
        official_output = results_dir / f"{rule_id}.official.json"
        candidate_output = results_dir / f"{rule_id}.candidate.json"
        official = run_side_assertion(
            executor=executor,
            rule_id=rule_id,
            capture_manifest=official_manifest,
            evidence_root=official_root,
            output=official_output,
            expected_codex_version=target_version,
            extra_arguments=config.get("official_extra_arguments"),
        )
        candidate = run_side_assertion(
            executor=executor,
            rule_id=rule_id,
            capture_manifest=candidate_manifest,
            evidence_root=candidate_root,
            output=candidate_output,
            expected_codex_version=target_version,
            extra_arguments=config.get("candidate_extra_arguments"),
        )
        rule_results.append(
            build_rule_result(
                rule_id=rule_id,
                official=(*official, official_output),
                candidate=(*candidate, candidate_output),
                official_root=official_root,
                candidate_root=candidate_root,
                results_root=results_dir,
                rationale=(
                    f"{rule_id} 在官方 {target_version} 证据与候选证据上分别由 "
                    "candidate_rule_assertion.py 独立执行并全部通过；"
                    "两侧检查集合一致，结论只来自机器断言。"
                ),
            )
        )
        print(f"{rule_id} 双侧通过", flush=True)

    document = build_results_document(
        candidate_id=str(config["candidate_id"]),
        target_version=target_version,
        profile_id=str(config["profile_id"]),
        profile_digest=str(config["profile_digest"]),
        official_package_digest=str(config["official_package_digest"]),
        candidate_package_digest=str(config["candidate_package_digest"]),
        comparison_package_digest=str(config["comparison_package_digest"]),
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
