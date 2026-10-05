#!/usr/bin/env python3
"""CI 四分片执行入口：沿用原分片，显式选择执行器，保存身份、覆盖、结果与耗时。"""

from __future__ import annotations

import argparse
from collections import Counter
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.ci import capture_test_shards as shards
from tools.ci import unit_executor as executor

SCHEMA = "capture-ci-shard/v1"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def file_digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_once(path, value):
    with path.open("x", encoding="utf-8") as stream:
        os.chmod(path, 0o600)
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def source_identity(args):
    """只记录公开 CI 坐标和内容摘要，不导出环境变量或认证凭据。"""
    def git(*argv):
        return subprocess.check_output(["git", "-C", str(ROOT), *argv], text=True).strip()
    commit = git("rev-parse", "HEAD")
    status = git("status", "--porcelain", "--untracked-files=all")
    declared = os.environ.get("GITHUB_SHA")
    if os.environ.get("GITHUB_ACTIONS") == "true" and (status or declared != commit):
        raise shards.ShardError("CI 提交身份不匹配或源码工作树不干净")
    paths = [Path(__file__), ROOT / "tools/ci/capture_test_shards.py", ROOT / "tools/ci/unit_executor.py",
             ROOT / "tools/ci/unit_records.py", ROOT / "tools/arm64_capture_driver/driver/bytecode_cache.py",
             ROOT / "tools/official_client_capture/codex_upgrade_tool_identity_policy.py",
             args.weights, args.config, args.durations]
    paths += sorted(args.start.rglob("*.py"))
    parser = os.environ.get("CLAUDE_AST_TYPESCRIPT_MODULE")
    if parser:
        paths.append(Path(parser))
    inputs = {str(path.resolve()): file_digest(path) for path in paths if path.is_file()}
    return {"commit": commit, "tree": git("rev-parse", "HEAD^{tree}"), "working_tree_clean": not status,
            "working_diff_sha256": digest([status, git("diff", "--no-ext-diff", "HEAD")]), "inputs": inputs,
            "python": platform.python_version(), "platform": platform.platform(), "architecture": platform.machine(),
            "ci": {name: os.environ.get(name) for name in ("GITHUB_REPOSITORY", "GITHUB_SHA", "GITHUB_RUN_ID",
                                                           "GITHUB_RUN_ATTEMPT", "GITHUB_JOB")}}


def prepare(args):
    """保持旧分片器的模块归组；别名导入的完整 ID 也保留在其原分片。"""
    grouped = shards.discover_cases(args.start, args.pattern)
    weights = shards.load_weights(args.weights)
    partitions = shards.check(grouped, weights, args.count)
    if not 1 <= args.index <= args.count:
        raise shards.ShardError("分片编号必须在 1..count 之间")
    all_ids = sorted(case.id() for cases in grouped.values() for case in cases)
    if len(all_ids) != len(set(all_ids)):
        raise shards.ShardError("discover 全集中出现重复测试 ID")
    modules = partitions[args.index - 1]
    selected = [case for module in modules for case in grouped[module]]
    if not selected:
        raise shards.ShardError("空分片不能作为通过凭证")
    ids = sorted(case.id() for case in selected)
    config = executor.load_config(args.config)
    for prefix, _reason in config.exclusive:
        if not any(executor._matches(test_id, prefix) for test_id in all_ids):
            raise shards.ShardError(f"独占登记没有命中任何测试：{prefix}")
    exclusive_ids = [test_id for test_id in ids
                     if any(executor._matches(test_id, prefix) for prefix, _reason in config.exclusive)]
    return selected, {"schema_version": SCHEMA, "source": source_identity(args),
                      "shard_count": args.count, "shard_index": args.index, "modules": modules,
                      "partition_sha256": digest(partitions), "full_test_ids_sha256": digest(all_ids),
                      "full_test_count": len(all_ids), "test_ids": ids, "selected_test_ids_sha256": digest(ids),
                      "exclusive_test_ids": exclusive_ids}


def verify_dispatch(plan, execution_plan, events, summary):
    """用真实启动／退出事件闭合规划；独占约束覆盖正式执行和诊断，不以诊断替代正式结果。"""
    units = {}
    planned_tests = []
    exclusive = set(plan["exclusive_test_ids"])
    for unit in execution_plan["units"]:
        unit_id, tests = unit["unit_id"], unit["tests"]
        if unit_id in units or not tests:
            raise shards.ShardError("派发规划含重复或空单元")
        required = bool(set(tests) & exclusive)
        if type(unit["exclusive"]) is not bool or unit["exclusive"] != required or (required and not set(tests) <= exclusive):
            raise shards.ShardError("派发规划的独占标志与登记不符")
        units[unit_id] = unit
        planned_tests.extend(tests)
    if sorted(planned_tests) != plan["test_ids"] or len(set(planned_tests)) != len(planned_tests):
        raise shards.ShardError("派发规划未闭合本片测试全集")
    formal = summary["units"]
    if sorted(row["unit_id"] for row in formal) != sorted(units):
        raise shards.ShardError("正式单元结果缺失或重复")
    for row in formal:
        if row["kind"] != "formal" or row["exclusive"] != units[row["unit_id"]]["exclusive"]:
            raise shards.ShardError("正式单元独占身份不匹配")
    active, started, exited = {}, Counter(), Counter()
    for event in events:
        if event["event"] not in {"start", "exit"}:
            continue
        unit_id, kind, pid = event["unit"], event["kind"], event["pid"]
        if unit_id not in units or kind not in {"formal", "diagnostic"} or type(pid) is not int or pid <= 0:
            raise shards.ShardError("派发事件身份非法")
        identity = (kind, unit_id)
        if event["event"] == "start":
            if pid in active or started[identity]:
                raise shards.ShardError("单元重复派发")
            if active and (units[unit_id]["exclusive"] or any(units[item[1]]["exclusive"] for item in active.values())):
                raise shards.ShardError("独占单元与其他单元重叠")
            active[pid] = identity
            started[identity] += 1
        else:
            if active.pop(pid, None) != identity:
                raise shards.ShardError("派发启动与退出未配对")
            exited[identity] += 1
        if event["running"] != sorted(item[1] for item in active.values()):
            raise shards.ShardError("派发事件的在跑集合不一致")
    expected = Counter({("formal", unit_id): 1 for unit_id in units})
    expected.update(("diagnostic", row["unit_id"]) for row in summary.get("diagnostic", []))
    if active or started != expected or exited != expected:
        raise shards.ShardError("派发事件缺报或仍有未退出单元")
    return {"status": "passed", "formal_units": len(units), "exclusive_tests": len(exclusive),
            "exclusive_units": sum(unit["exclusive"] for unit in units.values()),
            "diagnostic_units": len(summary.get("diagnostic", [])), "start_exit_pairs": sum(exited.values())}


class ObservedResult(unittest.TextTestResult):
    """旧 unittest 执行流程只增加观测；正式失败不会被后续成功或重试覆盖。"""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records, self.started, self.seen = {}, {}, []

    def startTest(self, test):
        self.seen.append(test.id())
        self.started[test.id()] = time.monotonic()
        super().startTest(test)

    def record(self, test, outcome):
        if self.records.get(test.id(), {}).get("outcome") not in {"failed", "error"}:
            self.records[test.id()] = {"outcome": outcome}

    def stopTest(self, test):
        self.records.setdefault(test.id(), {"outcome": "passed"})["seconds"] = time.monotonic() - self.started[test.id()]
        super().stopTest(test)

    def addFailure(self, test, err):
        self.record(test, "failed")
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, "error")
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.record(test, "skipped")
        super().addSkip(test, reason)

    def addExpectedFailure(self, test, err):
        self.record(test, "expected_failure")
        super().addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test):
        self.record(test, "unexpected_success")
        super().addUnexpectedSuccess(test)

    def addSubTest(self, test, subtest, err):
        if err is not None:
            self.record(test, "failed" if issubclass(err[0], test.failureException) else "error")
        super().addSubTest(test, subtest, err)


def run_legacy(selected, out):
    """保留原单进程 TestSuite／TextTestRunner，可显式回退；不调用新调度器。"""
    # 回退路径明确关闭身份记忆化，不能继承外层或前一轮配置而与收据矛盾。
    previous_memo = os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
    try:
        with (out / "execution.log").open("x") as log, contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            result = unittest.TextTestRunner(stream=log, verbosity=1, resultclass=ObservedResult).run(unittest.TestSuite(selected))
    finally:
        if previous_memo is None:
            os.environ.pop("CODEX_UPGRADE_IDENTITY_MEMO", None)
        else:
            os.environ["CODEX_UPGRADE_IDENTITY_MEMO"] = previous_memo
    write_once(out / "legacy-results.json", {"tests": result.records, "executed_test_ids": result.seen,
                                             "tests_run": result.testsRun, "successful": result.wasSuccessful()})
    return 0 if result.wasSuccessful() else 1, result.records, result.seen, None


def run_unified(args, plan, out):
    """先核对全集与分片；每片在独立目录准备字节码与身份缓存，两个开关分别回退。"""
    selection = out / "selection.json"
    write_once(selection, {"schema_version": "unit-executor-selection/v1",
                           "full_test_ids_sha256": plan["full_test_ids_sha256"], "test_ids": plan["test_ids"]})
    selection_sha = file_digest(selection)
    work = out / "executor"
    argv = [sys.executable, "-B", str(ROOT / "tools/ci/unit_executor.py"), "run", "--start", str(args.start),
            "--pattern", args.pattern, "--weights", str(args.weights), "--config", str(args.config),
            "--durations", str(args.durations), "--selection-file", str(selection), "--parallel", str(args.parallel),
            "--cores", str(args.cores), "--state-dir", str(out / "state"), "--out-dir", str(work),
            "--shared-caches", "bytecode" if args.bytecode_cache == "auto" else "off",
            "--identity-memo", args.identity_memo,
            "--bytecode-source", str(ROOT / "tools")]
    if not args.start.resolve().is_relative_to((ROOT / "tools").resolve()):
        argv.extend(["--bytecode-source", str(args.start.resolve())])
    environment = {key: value for key, value in os.environ.items()
                   if key not in {"CODEX_UPGRADE_IDENTITY_MEMO", "PYTHONPYCACHEPREFIX"}}
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    with (out / "execution.log").open("x") as log:
        process = subprocess.Popen(argv, cwd=ROOT, env=environment, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            code = process.wait()
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            raise
    summary_path = work / "summary.json"
    if not summary_path.is_file():
        raise shards.ShardError("执行器未留下完整结果，失败日志已保留")
    summary = json.loads(summary_path.read_text())
    binding = summary.get("selection") or {}
    if (file_digest(selection) != selection_sha or binding.get("sha256") != selection_sha
            or binding.get("full_test_ids_sha256") != plan["full_test_ids_sha256"]
            or binding.get("selected_test_ids_sha256") != plan["selected_test_ids_sha256"]):
        raise shards.ShardError("执行器的分片身份或选择件发生漂移")
    execution_plan_path, events_path = work / "plan.json", work / "events.jsonl"
    dispatch = verify_dispatch(plan, json.loads(execution_plan_path.read_text()),
                               [json.loads(line) for line in events_path.read_text().splitlines()], summary)
    dispatch.update(plan_sha256=file_digest(execution_plan_path), events_sha256=file_digest(events_path))
    records, seen = {}, []
    for unit in summary["units"]:
        if unit.get("kind") != "formal":
            raise shards.ShardError("诊断结果不能替代正式结果")
        safe = unit["unit_id"].replace("#", "-").replace("!", "-")
        path = work / "units" / (safe + ".result.json")
        if not path.is_file():
            continue
        result = json.loads(path.read_text())
        if result["unit_id"] != unit["unit_id"]:
            raise shards.ShardError("单元结果身份不匹配")
        seen.extend(result["tests"])
        records.update(result["tests"])
    if (summary.get("status") != "passed" or summary.get("expected_tests") != len(plan["test_ids"])
            or any(summary.get("full_set", {}).values()) or summary.get("failed_units")
            or any(not unit["passed"] or unit.get("orphans") or unit.get("timed_out") for unit in summary["units"])):
        code = code or 1
    return code, records, seen, {"command": argv, "summary_sha256": file_digest(summary_path),
                                "policy_sha256": summary["policy_sha256"], "diagnostic": summary.get("diagnostic", []),
                                "bytecode_cache": summary["bytecode_cache"], "identity_memo": summary["identity_memo"],
                                "dispatch_verification": dispatch}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--executor", choices=("unified", "legacy"), default="unified")
    parser.add_argument("--bytecode-cache", choices=("auto", "off"), default="auto",
                        help="统一执行器的作业内只读字节码共享；off 显式关闭，旧执行器不预编译")
    parser.add_argument("--identity-memo", choices=("auto", "off"), default="auto",
                        help="统一执行器的作业内身份记忆化；off 独立关闭，旧执行器不启用")
    parser.add_argument("--start", type=Path, default=ROOT / shards.DEFAULT_START)
    parser.add_argument("--pattern", default=shards.DEFAULT_PATTERN)
    parser.add_argument("--weights", type=Path, default=ROOT / shards.DEFAULT_WEIGHTS)
    parser.add_argument("--config", type=Path, default=ROOT / "tools/ci/unit_executor.json")
    parser.add_argument("--durations", type=Path, default=ROOT / "tools/ci/capture_test_durations.json")
    parser.add_argument("--parallel", type=int, default=0)
    parser.add_argument("--cores", type=int, default=0)
    parser.add_argument("--out-dir", type=Path)
    args = parser.parse_args(argv)
    sys.dont_write_bytecode = True
    began, started = time.monotonic(), datetime.now(timezone.utc).isoformat()
    out = args.out_dir.absolute() if args.out_dir else Path(tempfile.mkdtemp(prefix="capture-ci-")).resolve()
    try:
        if any(path.is_symlink() for path in (out, *out.parents)):
            raise shards.ShardError("CI 证据目录不得含符号链接")
        if args.out_dir:
            out.mkdir(mode=0o700, parents=True, exist_ok=False)
    except (OSError, shards.ShardError) as error:
        print(f"CI 入口错误：{error}", file=sys.stderr)
        return 2
    receipt = {"schema_version": SCHEMA, "executor": args.executor, "started_at_utc": started,
               "bytecode_cache_mode": args.bytecode_cache if args.executor == "unified" else "off",
               "identity_memo_mode": args.identity_memo if args.executor == "unified" else "off",
               "shard_count": args.count, "shard_index": args.index, "automatic_fallback": False}
    code = 2
    interrupted_by = None

    def terminate(signum, _frame):
        nonlocal interrupted_by
        interrupted_by = signum
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        raise KeyboardInterrupt

    previous_handler = signal.signal(signal.SIGTERM, terminate)
    try:
        selected, plan = prepare(args)
        write_once(out / "plan.json", plan)
        receipt["plan_sha256"] = file_digest(out / "plan.json")
        receipt["source"] = plan["source"]
        code, records, seen, detail = run_unified(args, plan, out) if args.executor == "unified" else run_legacy(selected, out)
        closed = sorted(seen) == plan["test_ids"] and len(seen) == len(set(seen)) and set(records) == set(plan["test_ids"])
        source_after = source_identity(args)
        source_unchanged = source_after == plan["source"]
        if not source_unchanged:
            receipt["source_drift"] = {key: {"before": plan["source"].get(key), "after": source_after.get(key)}
                                       for key in set(plan["source"]) | set(source_after)
                                       if plan["source"].get(key) != source_after.get(key)}
        outcomes = {test_id: row["outcome"] for test_id, row in sorted(records.items())}
        failed = any(value in {"failed", "error", "unexpected_success"} for value in outcomes.values())
        code = code or (1 if not closed or not source_unchanged or failed else 0)
        receipt.update(status="passed" if code == 0 else "failed", coverage_closed=closed,
                       source_unchanged=source_unchanged, expected_tests=len(plan["test_ids"]), reported_tests=len(seen),
                       counts=dict(Counter(outcomes.values())), outcomes_sha256=digest(outcomes), detail=detail)
        write_once(out / "results.json", records)
    except KeyboardInterrupt:
        receipt.update(status="aborted", reason="CI 执行被中断，原日志保留")
        code = 143 if interrupted_by == signal.SIGTERM else 130
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, shards.ShardError, executor.ExecutorError) as error:
        receipt.update(status="failed", reason=str(error))
        code = 2
    finally:
        signal.signal(signal.SIGTERM, previous_handler)
    receipt.update(exit_code=code, completed_at_utc=datetime.now(timezone.utc).isoformat(), elapsed_seconds=time.monotonic() - began)
    receipt["files"] = {str(path.relative_to(out)): file_digest(path) for path in sorted(out.rglob("*"))
                        if path.is_file() and not path.is_relative_to(out / "state")}
    write_once(out / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "executor": args.executor, "receipt": str(out / "receipt.json"),
                      "sha256": file_digest(out / "receipt.json"), "elapsed_seconds": receipt["elapsed_seconds"]}, ensure_ascii=False))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
