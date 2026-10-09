"""候选 r2 与评估器修复的私有场景及显式运行入口；没有登记正式认证。

--describe 只列出八场景与来源边界；--execute 必须给新证据目录，才委托
原隔离外壳运行完整链。原六项检查、同 Campaign、时间上限及失败判据
保持不变；成功也仅表示本机夹具范围，不能用于版本或 ARM64 正式认证。
"""

from __future__ import annotations

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import stat
import subprocess
import time
from typing import Any
import unittest

from tools.official_client_capture.tests import claude_revision_evaluator_chain_driver as driver

SOURCE_PATHS = (
    "tools/official_client_capture/tests/claude_revision_evaluator_chain_driver.py",
    "tools/official_client_capture/tests/claude_fault_chain_world.py",
    "tools/official_client_capture/tests/claude_transient_chain_driver.py",
    "tools/official_client_capture/tests/claude_late_stage_chain_driver.py",
    "tools/official_client_capture/tests/real_chains/claude_revision_evaluator_scenarios.py",
    "tools/official_client_capture/tests/real_chains/test_claude_upgrade_fault_chains.py",
    "tools/official_client_capture/claude_rule_assertion.py",
    "tools/official_client_capture/claude_client_receipt_contract.py",
    "tools/official_client_capture/claude_mac_product_recipe.py",
    "tools/official_client_capture/claude_upgrade_delivery.py",
    "tools/official_client_control/delivery_execution.py",
    "tools/official_client_capture/codex_upgrade_pre_a3_certification.py",
    "tools/official_client_capture/certify_release.py",
    "tools/official_client_capture/claude_upgrade_vc0_closeout.py",
)


def describe_entry() -> dict[str, Any]:
    """读取当前私有入口及八场景声明，不执行世界构造、命令或认证签发。"""

    from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as outer

    specification = outer.CHAINS.get("revision-continuous") or outer.PENDING_CONTINUOUS_CHAINS["revision-continuous"]
    return {
        "schema_version": "claude-revision-chain-local-entry/v1",
        "chain_id": specification["scenario"],
        "test": outer.CHAIN_MODULE + ".ClaudeFaultChainTests._run_pending_revision_continuous",
        "scenarios": list(outer.REVISION_CONTINUOUS_SCENARIOS),
        "six_checks": dict(outer.SIX_CHECKS),
        "chain_budget_seconds": outer.CHAIN_BUDGET_SECONDS,
        "chain_timeout_seconds": outer.CHAIN_TIMEOUT_SECONDS,
        "sources": {
            "official": "已有离线评估夹具的可实读清单与原字节，不生产新的官方 Store 包",
            "candidate": "同 Campaign 的真实 run／seal、封存 reader 与两本账；宿主、中继及 Mac 是明示夹具",
            "gate_environment": "完整环境测试事实经原 finalizer／replay 与独立 reader 消费",
            "delivery": "同世界 typed 文件现场与独立守卫，正式来源登记保持关闭",
        },
        "pending_formal_registration": "revision-continuous" in outer.PENDING_CONTINUOUS_CHAINS,
        "formal_certification": False,
        "scope": "仅准备并运行本机夹具链；合链／拆链、ARM64 时限与发布认证由成功实测后另行收口",
    }


def source_snapshot() -> dict[str, Any]:
    """只读记录源文件字节和元数据；Git 工作树存在时另记准确提交与状态。"""

    repository = driver.REPOSITORY_ROOT
    files = {}
    for relative in SOURCE_PATHS:
        path = repository / relative
        raw, metadata = path.read_bytes(), path.stat()
        files[relative] = {"bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(),
                           "mode": format(stat.S_IMODE(metadata.st_mode), "04o"), "mtime_ns": metadata.st_mtime_ns}
    git = {"commit": None, "status_porcelain": None}
    if (repository / ".git").exists():
        for key, arguments in (("commit", ["rev-parse", "HEAD"]),
                               ("status_porcelain", ["status", "--porcelain=v1", "--untracked-files=all"])):
            git[key] = subprocess.check_output(["git", "--no-optional-locks", *arguments],
                                              cwd=repository, text=True, timeout=30)
        git["commit"] = git["commit"].strip()
    from tools.official_client_capture import claude_client_receipt_contract as client
    from tools.official_client_capture import claude_upgrade_delivery as delivery
    from tools.official_client_control import delivery_execution as execution

    registries = {"client_sources": sorted(client.SOURCE_ADAPTERS),
                  "delivery_executors": sorted(delivery.ENVIRONMENT_EXECUTORS),
                  "delivery_sources": sorted(execution.SOURCE_VERIFIERS)}
    return {"repository": str(repository), "git": git, "files": files, "formal_registry_keys": registries}


def write_once(root: Path, name: str, value: Any) -> None:
    """证据只新建一次；不覆盖先前失败或成功结果。"""

    with (root / name).open("x", encoding="utf-8") as stream:
        stream.write(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def execute_private_chain(evidence_root: Path) -> tuple[int, dict[str, Any]]:
    """委托唯一既有完整外壳，保存真实首败与指标；没有轻量结果升格路径。"""

    from tools.official_client_capture.tests.real_chains import test_claude_upgrade_fault_chains as outer

    evidence_root = evidence_root.expanduser().resolve()
    if evidence_root.is_relative_to(driver.REPOSITORY_ROOT):
        raise ValueError("证据目录必须位于执行源码树之外")
    evidence_root.mkdir(mode=0o700, exist_ok=False)
    entry = describe_entry()
    before = source_snapshot()
    write_once(evidence_root, "入口与来源边界.json", entry)
    write_once(evidence_root, "源文件执行前.json", before)
    if before["git"]["status_porcelain"]:
        summary = {"schema_version": "claude-revision-chain-local-run/v1", "result": "refused_dirty_source",
                   "tests_run": 0, "formal_certification": False, "evidence_root": str(evidence_root),
                   "reason": "Git 执行源码须先固定为干净提交；未启动完整链"}
        write_once(evidence_root, "结果.json", summary)
        return 1, summary

    case = outer.ClaudeFaultChainTests("_run_pending_revision_continuous")
    stdout, stderr = io.StringIO(), io.StringIO()
    started = time.monotonic()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = unittest.TextTestRunner(stream=stderr, verbosity=2).run(unittest.TestSuite([case]))
    seconds = round(time.monotonic() - started, 3)
    after = source_snapshot()
    write_once(evidence_root, "stdout.log", stdout.getvalue())
    write_once(evidence_root, "tests.log", stderr.getvalue())
    write_once(evidence_root, "源文件执行后.json", after)
    metrics = dict(getattr(case, "real_chain_metrics", {}))
    records = metrics.get("scenarios") or []
    complete = outer._records_complete(records, list(outer.REVISION_CONTINUOUS_SCENARIOS))
    single_campaign = complete and len({row.get("campaign_id") for row in records}) == 1 \
        and all(row.get("campaign_id") for row in records)
    passed = (result.testsRun == 1 and result.wasSuccessful() and not result.skipped and complete and single_campaign
              and metrics.get("upstream_requests") == 0 and metrics.get("returncode") == 0 and before == after)
    failures = result.failures + result.errors
    if failures:
        write_once(evidence_root, "首败诊断.txt", failures[0][1])
    summary = {"schema_version": "claude-revision-chain-local-run/v1", "chain_id": entry["chain_id"],
               "result": "passed_local_fixture_chain" if passed else "failed_or_incomplete",
               "tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
               "skips": len(result.skipped), "seconds": seconds, "metrics": metrics,
               "eight_scenarios_six_checks_complete": complete, "same_campaign": single_campaign,
               "source_unchanged": before == after, "source_commit": before["git"]["commit"],
               "evidence_root": str(evidence_root), "completed_at_utc": datetime.now(timezone.utc).isoformat(),
               "formal_certification": False, "formal_registry_or_certification_written": False,
               "formal_registry_keys_unchanged": before["formal_registry_keys"] == after["formal_registry_keys"],
               "scope": entry["scope"]}
    write_once(evidence_root, "结果.json", summary)
    return (0 if passed else 1), summary


def main(argv: list[str] | None = None) -> int:
    """只有显式 --execute 才运行；--describe 不产生任何验收结果。"""

    parser = argparse.ArgumentParser(description="Claude r2／evaluator 私有八场景入口，结果不构成正式认证")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--describe", action="store_true", help="仅列出八场景、六项检查与来源边界")
    mode.add_argument("--execute", action="store_true", help="委托原隔离外壳运行完整私有链")
    parser.add_argument("--evidence-dir", type=Path, help="源码树之外的新证据目录；必须不存在")
    arguments = parser.parse_args(argv)
    if arguments.describe:
        print(json.dumps(describe_entry(), ensure_ascii=False, indent=2))
        return 0
    if arguments.evidence_dir is None:
        parser.error("--execute 必须给 --evidence-dir，新目录不能覆盖历史结果")
    code, summary = execute_private_chain(arguments.evidence_dir)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return code


class _RevisionEvaluatorScenarios(driver.ClaudeRevisionEvaluatorChainDriver):
    """必须由隔离外壳启动；普通工作树执行在 setUp 的任何修改之前拒绝。"""

    def _scenario_r1_source_failure_r2_evaluator_repair(self) -> None:
        self.run_revision_evaluator_chain()


if __name__ == "__main__":
    raise SystemExit(main())
