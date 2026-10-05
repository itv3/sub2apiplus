#!/usr/bin/env python3
"""B-09 全集承接合同：单次正式全集签发，逐项重放，失效后全量重跑。

收据按原执行结束时间起算，重复签发不能续期。调用方必须提供当前身份、
校时凭证、即时撤销查询与两类批准；本模块不签批准、不发请求、不补造证据。
所有外部文件均按绝对路径和完整摘要绑定，原执行与旧收据只读保留。
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import math
import os
import platform
import re
import stat
import subprocess
import sys
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "full-set-receipt/v1"
CONTRACT = "full-set-inheritance/v1"
MAX_VALIDITY_SECONDS = 86400
CLOCK_SKEW_SECONDS = 60
CLOCK_MAX_AGE_SECONDS = 300
REVOCATION_MAX_AGE_SECONDS = 60
DIGEST_FIELDS = ("deployment_receipt_digest", "tool_package_digest", "target_profile_digest",
                 "external_dependencies_digest", "data_snapshot_digest", "environment_fingerprint")
TEXT_FIELDS = ("campaign", "candidate", "architecture", "os_version", "kernel_version", "gate_contract_version")
BINDING_FIELDS = frozenset((*DIGEST_FIELDS, *TEXT_FIELDS, "commit", "runtime_image_digest"))
APPROVAL_ROLES = frozenset(("third_party_reviewer", "change_approver"))
FULL_GATE_IDS = frozenset(("backend-go-test", "backend-lint", "frontend-lint", "frontend-typecheck", "frontend-critical",
                          "test-capture-tools", "test-official-client-control", "check-egress-spec", "backend-unit",
                          "backend-integration", "lint-unit", "lint-integration", "deploy-scripts"))


class ContractError(ValueError):
    """证据缺失、错绑或过期，调用方须回到全量重跑。"""


def records_module():
    name = "full_set_unit_records"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name("unit_records.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def file_bytes(path: Path | str) -> bytes:
    """同一次打开读取证据并核对文件未在读取期间变化，解析与摘要消费同一批字节。"""
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ContractError("证据路径须为无符号链接的规范绝对路径")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        before = os.fstat(handle.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ContractError("证据必须是普通文件")
        raw = handle.read()
        after = os.fstat(handle.fileno())
        if any(getattr(before, k) != getattr(after, k) for k in ("st_size", "st_mtime_ns", "st_ctime_ns")):
            raise ContractError("证据在读取期间发生变化")
        return raw


def file_digest(path: Path) -> str:
    return hashlib.sha256(file_bytes(path)).hexdigest()


def read(path: Path | str) -> dict[str, Any]:
    value = json.loads(file_bytes(path))
    if not isinstance(value, dict):
        raise ContractError("证据必须是 JSON 对象")
    return value


def reference(path: Path | str) -> dict[str, str]:
    path = Path(path)
    raw = file_bytes(path)
    if not isinstance(json.loads(raw), dict):
        raise ContractError("证据必须是 JSON 对象")
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def replay_ref(ref: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise ContractError("证据索引须含路径和完整摘要")
    raw = file_bytes(ref["path"])
    if hashlib.sha256(raw).hexdigest() != ref["sha256"]:
        raise ContractError("证据文件摘要发生变化")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ContractError("证据必须是 JSON 对象")
    return value


def utc(value: str) -> float:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value):
        raise ContractError("时间必须使用明确的 UTC")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def binding_check(binding: Mapping[str, Any]) -> None:
    if not isinstance(binding, dict) or set(binding) != BINDING_FIELDS:
        raise ContractError("全集承接身份字段不完整或含未知字段")
    for name in DIGEST_FIELDS:
        if not re.fullmatch(r"[0-9a-f]{64}", str(binding[name])):
            raise ContractError(f"身份字段 {name} 不是完整 SHA-256")
    if not re.fullmatch(r"[0-9a-f]{40}", str(binding["commit"])):
        raise ContractError("提交必须是完整 SHA-1")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(binding["runtime_image_digest"])):
        raise ContractError("运行镜像必须使用完整内容摘要")
    if any(not isinstance(binding[k], str) or not binding[k].strip() for k in TEXT_FIELDS):
        raise ContractError("身份文本字段缺失")
    if binding["gate_contract_version"] != CONTRACT:
        raise ContractError("门禁合同版本不匹配")


def context_check(context: Mapping[str, Any]) -> dict[str, Any]:
    """当前上下文来自明确登记的快照，逐份核对实物和绑定断言。"""
    if context.get("schema_version") != "full-set-context/v1":
        raise ContractError("当前上下文格式错误")
    binding = context.get("binding")
    binding_check(binding)
    evidence = context.get("evidence")
    # 外部事实由原生收据提供；不得用只有摘要、没有实物的参数代替。
    if not isinstance(evidence, dict) or set(evidence) != {"deployment", "profile", "runtime", "dependencies", "data"}:
        raise ContractError("上下文缺少部署、画像、镜像、依赖或数据快照证据")
    for name, field in (("deployment", "deployment_receipt_digest"), ("dependencies", "external_dependencies_digest"),
                        ("data", "data_snapshot_digest")):
        replay_ref(evidence[name])
        if evidence[name]["sha256"] != binding[field]:
            raise ContractError(f"{name} 实物与身份摘要不同")
    profile, runtime = replay_ref(evidence["profile"]), replay_ref(evidence["runtime"])
    if profile.get("target_profile_digest") != binding["target_profile_digest"]:
        raise ContractError("画像权威收据与目标内容摘要不同")
    if runtime.get("runtime_image_digest") != binding["runtime_image_digest"]:
        raise ContractError("运行镜像收据与身份不同")
    if replay_ref(evidence["deployment"]).get("status") != "passed":
        raise ContractError("部署收据未通过")
    return dict(binding)


def platform_fields() -> dict[str, str]:
    return {"architecture": platform.machine(), "os_version": platform.platform(), "kernel_version": platform.release()}


def live_check(request: Mapping[str, Any], *, deployment: Path | None, store: Path | None, tree: Path) -> None:
    """消费时查询实际源码、部署、主机与运行容器，旧上下文文件不能替代现场事实。"""
    context = replay_ref(request["context"])
    binding = context_check(context)
    source = replay_ref(request["receipt"])["source"]
    if deployment is None or store is None or source["store"] != str(store):
        raise ContractError("当前部署或记录库缺失／错绑")
    read(deployment)
    candidates = sorted(deployment.parent.glob("codex-*-supervisor-enable-*.json"),
                        key=lambda p: p.name.split("-supervisor-enable-", 1)[1].lower())
    if not candidates or candidates[-1] != deployment or file_digest(deployment) != binding["deployment_receipt_digest"]:
        raise ContractError("当前最新部署与全集收据不同")
    actual_commit = subprocess.run(["git", "-C", str(tree), "rev-parse", "HEAD"], check=True, capture_output=True,
                                   text=True, stdin=subprocess.DEVNULL, timeout=30).stdout.strip()
    if actual_commit != binding["commit"] or any(binding[k] != v for k, v in platform_fields().items()):
        raise ContractError("实际提交或平台与全集绑定不同")
    runtime = replay_ref(context["evidence"]["runtime"])
    container = runtime.get("container_name")
    if not isinstance(container, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", container):
        raise ContractError("运行镜像证据缺少受核对的容器名称")
    image_id = subprocess.run(["docker", "inspect", "--format", "{{.Image}}", container], check=True,
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30).stdout.strip()
    if image_id != binding["runtime_image_digest"]:
        raise ContractError("实际运行镜像已改变")


def clock_check(clock: Mapping[str, Any], now: float, *, reference_time: float | None = None) -> None:
    sample = replay_ref(clock)
    offset = sample.get("offset_seconds")
    if (sample.get("status") != "synchronized" or not sample.get("source") or isinstance(offset, bool)
            or not isinstance(offset, (float, int)) or not math.isfinite(offset) or abs(offset) > CLOCK_SKEW_SECONDS):
        raise ContractError("时钟偏差不可确认或超过 60 秒")
    age = (now if reference_time is None else reference_time) - utc(sample["sampled_at_utc"])
    if not -CLOCK_SKEW_SECONDS <= age <= CLOCK_MAX_AGE_SECONDS:
        raise ContractError("校时凭证不在本次判定的有效窗口内")


def normalized_result(summary: Mapping[str, Any]) -> dict[str, Any]:
    """仅剥离计时、资源用量与 run ID；测试结论和门禁全集仍逐项绑定。"""
    return {"gates": sorted(({k: row.get(k) for k in ("gate_id", "status", "units", "test_groups", "not_executed")}
                              for row in summary["gates"]), key=lambda row: row["gate_id"]),
            "units": sorted(({k: row.get(k) for k in ("unit_id", "passed", "exit_code", "signal", "timed_out")}
                              for row in summary["units"]), key=lambda row: row["unit_id"]),
            "test_groups": {key: {k: value.get(k) for k in ("counts", "expected_tests", "reported_tests", "full_set", "skipped")}
                            for key, value in summary.get("test_groups", {}).items()}}


def source_check(source: Mapping[str, Any], binding: Mapping[str, Any]) -> tuple[dict, dict, Any]:
    records = records_module()
    entry, summary, manifest = (replay_ref(source[k]) for k in ("entry", "summary", "manifest"))
    store = records.RecordStore(Path(source["store"]))
    if (entry.get("status") != "passed" or entry.get("profile") != "full-gates"
            or entry.get("source", {}).get("commit") != binding["commit"]
            or not entry.get("source", {}).get("deploy_receipt")):
        raise ContractError("来源必须是同提交、已核对部署的完整门禁")
    if file_digest(Path(entry["source"]["deploy_receipt"])) != binding["deployment_receipt_digest"]:
        raise ContractError("来源部署收据与绑定不同")
    if (entry.get("executor_summary") != source["summary"]["path"]
            or entry.get("unit_manifest", {}).get("path") != source["manifest"]["path"]
            or summary.get("run_id") != manifest.get("run_id")
            or summary.get("status") != "passed" or summary.get("mode") != records.RE_EXECUTE
            or manifest.get("mode") != records.RE_EXECUTE):
        raise ContractError("来源并非同一次重新执行全集的正式记录")
    if (manifest.get("environment_sha256") != binding["environment_fingerprint"]
            or manifest.get("executor", {}).get("sha256") != binding["tool_package_digest"]):
        raise ContractError("来源环境或执行工具摘要与绑定不同")
    problems = records.verify_manifest(manifest, store=store)
    published = store.manifest(manifest["run_id"])
    ids = manifest.get("planned_units", [])
    if (problems or not ids or published != manifest or summary.get("failed_units") or summary.get("units_not_run")
            or sorted(row["gate_id"] for row in summary["gates"]) != sorted(FULL_GATE_IDS)
            or sorted(row["unit_id"] for row in summary["units"]) != sorted(ids)
            or any(row.get("passed") is not True for row in summary["units"])
            or any(row.get("status") != "passed" for row in summary["gates"])):
        raise ContractError("来源运行清单、全集或原始结果无法重放")
    if summary.get("read_audit", {}).get("coverage_complete") is not True:
        raise ContractError("全集读集覆盖未闭合")
    for row in manifest["units"]:
        record = read(row["record_path"])
        current = records.Current(row["unit_id"], record["unit_type"], record["spec"], row["spec_sha256"],
                                  record["inputs"], row["inputs_sha256"], True)
        facts = records.RunFacts(manifest["policy_sha256"], manifest["environment"], manifest["environment_sha256"],
                                 manifest["executor"], 24, utc(record["completed_at_utc"]), require_read_audit=True)
        reasons = records.check_record(store, Path(row["record_path"]), record, current, facts)
        if row.get("disposition") != "executed" or reasons:
            raise ContractError(f"来源单元 {row['unit_id']} 非正式完整读集记录：{'；'.join(reasons)}")
    return manifest, summary, store


def issue(source: Mapping[str, Any], context: Mapping[str, Any], clock: Mapping[str, Any], *,
          validity_seconds: int = MAX_VALIDITY_SECONDS, predecessor: Mapping[str, Any] | None = None) -> dict[str, Any]:
    binding = context_check(context)
    if isinstance(validity_seconds, bool) or not isinstance(validity_seconds, int) or not 0 < validity_seconds <= MAX_VALIDITY_SECONDS:
        raise ContractError("有效时长必须为 1～86400 秒")
    manifest, summary, _store = source_check(source, binding)
    issued = max(utc(row["completed_at_utc"]) for row in summary["units"])
    clock_check(clock, issued, reference_time=issued)
    if predecessor is not None and replay_ref(predecessor).get("run_id") == manifest["run_id"]:
        raise ContractError("失效后的替代收据必须关联新一次全集运行，禁止刷新旧收据有效期")
    body = {"schema_version": SCHEMA, "binding": binding, "source": dict(source), "context": dict(context),
            "issuer_clock": dict(clock), "issued_at_utc": timestamp(issued), "expires_at_utc": timestamp(issued + validity_seconds),
            "run_id": manifest["run_id"], "result_digest": digest(normalized_result(summary)),
            "predecessor": predecessor, "statement": "同一次重新执行全集的结论，实际承接仍须消费时重新核对"}
    return {**body, "receipt_sha256": digest(body)}


def authorize(request: Mapping[str, Any], *, now: float, request_schema: str = "full-set-request/v1",
              approval_scope: str = CONTRACT) -> tuple[dict, dict, dict, dict, Any]:
    """B-09／B-10 共用来源、身份、时钟、撤销与批准校验；不替调用方选择目标单元。"""
    if request.get("schema_version") != request_schema or type(request.get("reuse_enabled")) is not bool:
        raise ContractError("全集承接请求格式错误")
    receipt = replay_ref(request["receipt"])
    if receipt.get("schema_version") != SCHEMA or receipt.get("receipt_sha256") != digest({k: v for k, v in receipt.items() if k != "receipt_sha256"}):
        raise ContractError("全集收据自摘要不符")
    binding = context_check(replay_ref(request["context"]))
    if binding != receipt["binding"]:
        raise ContractError("提交、部署、画像、平台或外部输入发生变化")
    issued, expires = utc(receipt["issued_at_utc"]), utc(receipt["expires_at_utc"])
    if not 0 < expires - issued <= MAX_VALIDITY_SECONDS or now < issued - CLOCK_SKEW_SECONDS or now >= expires:
        raise ContractError("签发／失效时间不合法或已过期")
    clock_check(receipt["issuer_clock"], issued, reference_time=issued)
    clock_check(request["consumer_clock"], now)
    revocation = replay_ref(request["revocation"])
    if (revocation.get("schema_version") != "full-set-revocation-query/v1" or revocation.get("status") != "ok"
            or revocation.get("receipt_sha256") != receipt["receipt_sha256"] or revocation.get("revoked") is not False
            or revocation.get("campaign_status") != "active"
            or not 0 <= now - utc(revocation["queried_at_utc"]) <= REVOCATION_MAX_AGE_SECONDS):
        raise ContractError("撤销查询失败、过旧、已撤销或 Campaign 不再有效")
    approvals = [replay_ref(ref) for ref in request["approvals"]]
    if {a.get("role") for a in approvals} != APPROVAL_ROLES or len(approvals) != len(APPROVAL_ROLES):
        raise ContractError("缺少三方审核及变更批准")
    for approval in approvals:
        if (approval.get("status") != "approved" or not approval.get("account")
                or approval.get("scope") != approval_scope or approval.get("binding_sha256") != digest(binding)
                or not utc(approval["approved_at_utc"]) <= now < utc(approval["expires_at_utc"])):
            raise ContractError("专项批准缺失、错绑、已撤回或过期")
    manifest, summary, store = source_check(receipt["source"], binding)
    if (receipt["run_id"] != manifest["run_id"] or receipt["result_digest"] != digest(normalized_result(summary))
            or issued != max(utc(row["completed_at_utc"]) for row in summary["units"])):
        raise ContractError("来源时间或语义结果不同")
    return receipt, binding, manifest, summary, store


def assess(request: Mapping[str, Any], currents: Mapping[str, Any], facts: Any, *, gate_ids: list[str], now: float) -> tuple[dict, dict]:
    """返回判定收据与来源决策。任何失败均全拒，不混用其它运行的成功单元。"""
    records = records_module()
    verdict = {"schema_version": "full-set-decision/v1", "decided_at_utc": timestamp(now), "eligible": False,
               "reuse_enabled": request.get("reuse_enabled") is True, "action": "reexecute-all", "reasons": [],
               "actor": {"effective_uid": os.geteuid(), "host": platform.node()},
               "source_receipt": request.get("receipt"), "source_run_id": None, "expires_at_utc": None}
    decisions: dict[str, Any] = {}
    try:
        receipt, binding, manifest, summary, store = authorize(request, now=now)
        verdict.update(source_run_id=receipt["run_id"], expires_at_utc=receipt["expires_at_utc"])
        if (sorted(currents) != sorted(manifest["planned_units"])
                or sorted(gate_ids) != sorted(row["gate_id"] for row in summary["gates"])):
            raise ContractError("当前门禁全集不同")
        facts = replace(facts, require_read_audit=True, max_age_hours=24, now=now)
        for row in manifest["units"]:
            unit_id = row["unit_id"]
            record = read(row["record_path"])
            reasons = records.check_record(store, Path(row["record_path"]), record, currents[unit_id], facts)
            if not currents[unit_id].inheritable or reasons:
                raise ContractError(f"当前单元 {unit_id} 不能承接：{'；'.join(reasons)}")
            decisions[unit_id] = records.Decision(record, Path(row["record_path"]), [])
        verdict["eligible"] = True
        if not verdict["reuse_enabled"]:
            raise ContractError("真实承接开关关闭，仅记录可承接判定")
        verdict["action"] = "inherit-full-set"
        return verdict, decisions
    except (ContractError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        verdict["reasons"].append(str(error))
        return verdict, {unit_id: records.Decision(reasons=["B-09 全集重跑：" + str(error)]) for unit_id in currents}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", required=True, type=Path, help="来源入口、执行汇总、运行清单和记录库的索引")
    parser.add_argument("--context", required=True, type=Path)
    parser.add_argument("--clock", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--validity-seconds", type=int, default=MAX_VALIDITY_SECONDS)
    parser.add_argument("--predecessor", type=Path)
    args = parser.parse_args(argv)
    try:
        receipt = issue(read(args.source), read(args.context), reference(args.clock), validity_seconds=args.validity_seconds,
                        predecessor=reference(args.predecessor) if args.predecessor else None)
        # 只新增；旧收据过期后也不能覆盖。
        with args.output.open("x", encoding="utf-8") as handle:
            os.chmod(args.output, 0o600)
            handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        print(json.dumps({"status": "issued", "receipt": str(args.output), "receipt_sha256": receipt["receipt_sha256"]}))
        return 0
    except (ContractError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.exit(main())
