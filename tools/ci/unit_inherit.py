#!/usr/bin/env python3
"""B5a-08 受控的逐单元承接合同（``unit-inherit/v1``）：来源记录可来自多次运行、多个提交，逐单元判定承接或重跑。

为什么要有：入口门禁原有两种模式——``re-execute`` 全部重跑；``full-set-pass`` 只有 B-09 全集请求通过时才承接，而 B-09
要求当前全集与唯一一次来源运行完全相同，任一单元不能承接就整次重跑。B5a-09／B5a-10 的局部重跑与跨客户端承接需要「只
重跑受影响的单元」的合法入口，这里定义它。两客户端共用，执行器（``unit_executor.py run-gates --mode unit-inherit``）与消费端
（P0 v2 证据与收据重验、pre-A3 v2／v3 签发）都按本模块判定。

* **当前上下文**（``unit-inherit-context/v1``）：当前提交、当前部署收据集合（每个已纳管客户端最新的一份，B5a-01 的双收据）、
  环境指纹、调度策略版本、执行器版本、门禁全集与组合、客户端、入口阶段（``pre-ledger``／``post-ledger``／
  ``formal:<campaign_id>``）、记录库。执行器在每个复核点现算，必须与请求登记的上下文逐字段相同，否则整次重新执行。
* **来源集合**（``unit-inherit-sources/v1``，内嵌在请求里）：每个承接单元指向一条来源记录（来源运行 ID＋记录摘要），并列出
  全部来源运行及其已发布清单的摘要。撤销以来源运行为对象：任一来源运行被撤销，只有指向它的单元重跑。
* **批准**（请求 ``unit-inherit-request/v1``）：``reuse_enabled``、``context``、``sources``、``consumer_clock``、``revocation``、两类
  ``approvals``（第三方审核与变更批准，scope ``unit-inherit/v1``，``binding_sha256`` 绑定上下文摘要与来源集合摘要）。缺任一项、
  格式不对、当前上下文与请求不同、校时或撤销查询失败、任一批准失效：整次重新执行。凭证规则复用 B-09
  （``full_set_receipt.clock_check``、批准的角色、形状与有效期、撤销查询 60 秒时效），授权对象换成上下文与来源集合。
* **可承接**＝``unit_records.check_record`` 全部通过且要求读集覆盖闭合（轨迹在库、按当前输入能重放）、来源运行清单已发布并
  把该记录列为正式执行（``check_record`` 内核对，另核对清单摘要等于来源集合登记的）、完成时间在承接期限内（默认 7 天，只能
  调小）、来源运行未被撤销。不可承接的单元只重跑它自己，不因一个单元失效拒绝整次运行；执行＋承接＝全集（清单自检）。
  来源记录登记的宿主路径（``host:``）与隔离合同按 B-10 同法沿用：每次重新快照，不缩小当前仓库声明。
* **复核点**：排队前（``queue``）、取得调度锁后（``lock``）、发布清单前（``publish``）各一次。每次都重新获取校时与撤销查询快照，
  以新文件、新摘要追加留证进记录库（不覆盖旧凭证），重做授权与每条承接记录的 ``check_record``；上下文、来源集合与批准绑定
  保持不变（每次重读请求文件核对），批准不自动续期。锁后复核只能把承接改为执行；发布前复核失败即拒绝发布，已执行的单元
  记录照常入库保留。请求自带的 ``consumer_clock``／``revocation`` 是组装请求时的凭证：内容必须合法、采样时间落在批准有效期内，
  它标出的撤销同样生效；各复核点的时效由刷新的快照判定（排队、取锁可能远超 60 秒，初始凭证撑不到发布前）。
* **决策收据**（``unit-inherit-decision/v1``）：登记上下文与来源集合摘要、封存的请求与批准、每个复核点的快照引用与判定时间、
  逐单元处置；随清单发布进记录库（``unit-inherit/decisions/<run_id>.json``，只增不改），清单自检核对两者一致
  （``unit_records.verify_manifest``）。
* **消费端**（``verify_published``）：决策收据在库且自摘要相符、每个复核点按形成期快照与原判定时间成立、每条承接记录按决策
  收据登记的上下文重做 ``check_record``；签发时（``historical=False``）另取消费时的校时与撤销快照（追加留证并写一份消费登记），
  批准必须在此刻仍有效；历史重放（``historical=True``）不刷新，按形成期快照与原判定时间验证，给了消费登记时另按签发时的快照
  与签发时间验证。

记录库里的布局（``<记录库>/unit-inherit/``）：``evidence/<sha256>.json`` 内容寻址的证据快照（请求、上下文、批准、各复核点与消费
时的校时与撤销快照）；``decisions/<run_id>.json`` 决策收据；``revocations/<run_id>.json`` 撤销登记（只增，撤销不可撤回）；
``consumptions/<run_id>/<用途>-<UTC>-<随机>.json`` 消费登记。清理规则（``entry_housekeeping``）尚未覆盖本目录，留给 B5a-10。

校时样本的来源由请求的 ``clock_source`` 指定：``chronyc``（缺省，执行 ``chronyc -c tracking``）或 ``file:<绝对路径>``（外部维护的
样本文件，原样封存后按同一规则判定）。撤销查询读本记录库的撤销登记；``formal:`` 阶段另要 Campaign 状态（请求的
``campaign_status_source``：``file:<绝对路径>``），缺失即失败关闭。本模块不签批准、不替调用方补造凭证。

子命令：``sample-clock``（取一份校时样本）、``query-revocation``（撤销查询快照）、``revoke``（登记撤销一次来源运行）、
``assemble-request``（把执行器 ``--decide-only --unit-inherit-propose`` 写出的提案、凭证与两份批准组装成请求）、``verify``（消费端
重验：缺省按当前时刻消费并留证，``--historical`` 只按形成期证明重放）。
"""

from __future__ import annotations

import argparse
import fnmatch
import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import time
from contextvars import ContextVar
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent


def _sibling(filename: str, key: str) -> Any:
    """同目录模块按路径加载（仓库 tools/ci 与驱动副本都是平铺的一组文件），同一进程共用一个模块对象。"""

    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / filename)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"找不到同目录的 {filename}：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(key, None)
            raise
    return sys.modules[key]


fs = _sibling("full_set_receipt.py", "unit_inherit_full_set_receipt")
ContractError = fs.ContractError


_LEGACY_RECORDS = fs.records_module
_CONTROL_READ_AUDIT: ContextVar[Any] = ContextVar("unit_inherit_control_read_audit", default=None)


def records() -> Any:
    """记录与清单模块（同目录 unit_records.py）：与 B-09 共用 ``full_set_receipt.records_module`` 加载的那一份。"""

    adapter = _CONTROL_READ_AUDIT.get()
    return adapter._records_module() if adapter is not None else _LEGACY_RECORDS()


# 这是当前unit-inherit自己的固定fs实例，不修改历史target gate的模块或共享缓存。
fs.records_module = records


@contextmanager
def _control_reader_scope(executor: Mapping[str, Any], client: str, unit_ids: Sequence[str]):
    """只有当前Claude四control及执行器实物绑定同时成立才启用同一消费adapter。"""
    adapter = None
    files = executor.get("files") or {}
    names = {"pre-a3:claude:" + name for name in (
        "vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause", "vc-chain.claude-tool-evolution")}
    if client == "claude" and names & set(unit_ids) and {"control_read_audit.py", "control_read_audit_runtime.py"} & set(files):
        adapter = _sibling("control_read_audit.py", "unit_inherit_control_read_audit_" + str(HERE).replace("/", "_"))
        if not adapter.frozen_executor(executor):
            raise ContractError("当前四control消费缺本次执行器的准确sidecar绑定")
    token = _CONTROL_READ_AUDIT.set(adapter)
    try:
        yield
    finally:
        _CONTROL_READ_AUDIT.reset(token)


def current_time() -> float:
    """消费端与命令行取「此刻」的唯一入口（执行器的复核点由调用方传入时刻）：验收用替身时钟替换它，不改系统时间。"""

    return time.time()


def _moment(value: float) -> float:
    """判定时刻按凭证里的写法（微秒精度的 UTC 文本）往返一次：刷新的快照就写这个时刻，读回来与判定时刻严格相等，不会因为
    浮点舍入比判定时刻晚一微秒而被 B-09 的「撤销查询不得来自未来」挡掉；决策收据登记的判定时间也是它。"""

    return fs.utc(fs.timestamp(value))


MODE = "unit-inherit"
CONTRACT = "unit-inherit/v1"
REQUEST_SCHEMA = "unit-inherit-request/v1"
CONTEXT_SCHEMA = "unit-inherit-context/v1"
SOURCES_SCHEMA = "unit-inherit-sources/v1"
REVOCATION_SCHEMA = "unit-inherit-revocation/v1"
REVOCATION_QUERY_SCHEMA = "unit-inherit-revocation-query/v1"
CONSUMPTION_SCHEMA = "unit-inherit-consumption/v1"
PROPOSAL_SCHEMA = "unit-inherit-proposal/v1"
CAMPAIGN_STATUS_SCHEMA = "unit-inherit-campaign-status/v1"
CHECKPOINTS = ("queue", "lock", "publish")
DEFAULT_CLOCK_SOURCE = "chronyc"
ERRORS = (ContractError, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError)
REQUEST_FIELDS = frozenset({"schema_version", "reuse_enabled", "context", "sources", "consumer_clock", "revocation", "approvals"})
REQUEST_OPTIONAL_FIELDS = frozenset({"max_age_hours", "clock_source", "campaign_status_source"})
CONTEXT_FIELDS = frozenset({"schema_version", "client", "stage", "commit", "deployment_receipts", "environment_sha256",
                            "policy_sha256", "executor_sha256", "gates", "profile", "record_store"})
# 消费端加载的验证器文件（本模块与它按路径加载的依赖）：摘要随消费登记留证。
VERIFIER_FILES = ("unit_inherit.py", "full_set_receipt.py", "unit_records.py", "read_audit.py", "read_audit_runtime.py")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_RUN_ID = re.compile(r"[0-9A-Za-z._-]{1,128}")
_CLIENT = re.compile(r"[a-z][a-z0-9-]{0,31}")
_STAGE = re.compile(r"pre-ledger|post-ledger|formal:[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


# ---------------------------------------------------------------------------
# 记录库里的承接目录
# ---------------------------------------------------------------------------


def inherit_root(store_root: Path | str) -> Path:
    return Path(store_root) / records().UNIT_INHERIT_DIR


def evidence_path(store_root: Path | str, digest: str) -> Path:
    return inherit_root(store_root) / "evidence" / f"{digest}.json"


def revocation_path(store_root: Path | str, run_id: str) -> Path:
    return inherit_root(store_root) / "revocations" / f"{run_id}.json"


def _dump(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")


def put_evidence(store_root: Path | str, payload: Mapping[str, Any]) -> dict[str, str]:
    """把一份证据快照按内容寻址存进记录库（只增不改），返回 ``{path, sha256}`` 引用。"""

    data = _dump(payload)
    digest = records().sha256_bytes(data)
    path = records()._publish(inherit_root(store_root) / "evidence", f"{digest}.json", data)
    reference = fs.reference(path)
    if reference["sha256"] != digest:
        raise ContractError("证据快照落盘后摘要与内容不符")
    return reference


def _write_once(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_dump(payload))
    return path


def seal_evidence(store_root: Path | str, reference: Mapping[str, Any]) -> dict[str, str]:
    """把调用方给的一份外部证据（``{path, sha256}``）按原样内容封存进记录库：历史重放不依赖外部文件仍在原处。"""

    return put_evidence(store_root, fs.replay_ref(dict(reference)))


# ---------------------------------------------------------------------------
# 上下文与来源集合
# ---------------------------------------------------------------------------


def deployment_set(control_root: Path | str) -> list[dict[str, str]]:
    """当前部署收据集合：每个已纳管客户端（control 目录里有它的部署收据）最新的一份，规则与驱动 install.py 相同。"""

    table = _sibling("client_table.py", "client_table_sibling")
    control = Path(control_root)
    entries: list[dict[str, str]] = []
    for client in table.CLIENTS:
        spec = table.spec(client)
        separator = spec["deploy_receipt_stamp_separator"]
        candidates = sorted((path for path in control.glob(spec["deploy_receipt_glob"]) if path.is_file() and not path.is_symlink()),
                            key=lambda path: path.name.split(separator, 1)[1].lower())
        if not candidates:
            continue
        if len(candidates) > 1 and (candidates[-1].name.split(separator, 1)[1].lower()
                                    == candidates[-2].name.split(separator, 1)[1].lower()):
            raise ContractError(f"客户端 {client} 同一时刻存在两份部署收据，无法唯一确定最新部署")
        path = candidates[-1].resolve()
        entries.append({"client": client, "path": str(path), "sha256": fs.file_digest(path)})
    if not entries:
        raise ContractError(f"control 目录没有任何客户端的部署收据：{control}")
    return entries


def tree_commit(tree: Path | str) -> str:
    completed = subprocess.run(["git", "--no-optional-locks", "-C", str(tree), "rev-parse", "HEAD"], capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=60)
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not _COMMIT.fullmatch(commit):
        raise ContractError(f"算不出测试树的当前提交：{completed.stderr.strip()[-200:]}")
    return commit


def current_context(*, client: str, stage: str, commit: str, deployments: Sequence[Mapping[str, str]], environment_sha256: str,
                    policy_sha256: str, executor_sha256: str, gate_ids: Sequence[str], profile: Any,
                    record_store: Path | str) -> dict[str, Any]:
    """执行器在复核点现算的当前上下文（字段见模块说明）。"""

    return context_check({
        "schema_version": CONTEXT_SCHEMA, "client": client, "stage": stage, "commit": commit,
        "deployment_receipts": sorted((dict(item) for item in deployments), key=lambda item: item["client"]),
        "environment_sha256": environment_sha256, "policy_sha256": policy_sha256, "executor_sha256": executor_sha256,
        "gates": sorted(gate_ids), "profile": profile, "record_store": str(Path(record_store).resolve()),
    })


def context_check(context: Any) -> dict[str, Any]:
    if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS or context.get("schema_version") != CONTEXT_SCHEMA:
        raise ContractError("承接上下文格式错误或字段不闭合")
    if not isinstance(context["client"], str) or not _CLIENT.fullmatch(context["client"]):
        raise ContractError("承接上下文的客户端非法")
    if not isinstance(context["stage"], str) or not _STAGE.fullmatch(context["stage"]):
        raise ContractError("入口阶段只能是 pre-ledger、post-ledger 或 formal:<campaign_id>")
    if not isinstance(context["commit"], str) or not _COMMIT.fullmatch(context["commit"]):
        raise ContractError("承接上下文的提交必须是完整 SHA-1")
    for name in ("environment_sha256", "policy_sha256", "executor_sha256"):
        if not isinstance(context[name], str) or not _SHA256.fullmatch(context[name]):
            raise ContractError(f"承接上下文的 {name} 不是完整 SHA-256")
    deployments = context["deployment_receipts"]
    if (not isinstance(deployments, list) or not deployments
            or any(not isinstance(item, dict) or set(item) != {"client", "path", "sha256"} for item in deployments)):
        raise ContractError("承接上下文的部署收据集合非法")
    clients = [item["client"] for item in deployments]
    if (clients != sorted(clients) or len(clients) != len(set(clients))
            or any(not str(item["path"]).startswith("/") or not _SHA256.fullmatch(str(item["sha256"])) for item in deployments)):
        raise ContractError("部署收据集合须每个客户端一份、按客户端排序、绝对路径与完整摘要")
    gates = context["gates"]
    if (not isinstance(gates, list) or not gates or gates != sorted(gates) or len(gates) != len(set(gates))
            or not all(isinstance(item, str) and item for item in gates)):
        raise ContractError("承接上下文的门禁全集非法")
    if not isinstance(context["profile"], str) or not context["profile"]:
        raise ContractError("承接上下文缺少门禁组合")
    if not isinstance(context["record_store"], str) or not context["record_store"].startswith("/"):
        raise ContractError("承接上下文的记录库必须是绝对路径")
    return context


def sources_check(sources: Any) -> dict[str, Any]:
    """来源集合：``runs``（来源运行 → 已发布清单的摘要）与 ``units``（承接单元 → 来源运行与记录摘要），两边闭合。"""

    if not isinstance(sources, dict) or set(sources) != {"schema_version", "runs", "units"} or sources["schema_version"] != SOURCES_SCHEMA:
        raise ContractError("来源集合格式错误")
    runs, units = sources["runs"], sources["units"]
    if not isinstance(runs, dict) or not runs or not isinstance(units, dict) or not units:
        raise ContractError("来源集合必须列出来源运行与承接单元")
    for run_id, digest in runs.items():
        if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise ContractError("来源运行须为合法运行 ID 与完整清单摘要")
    for unit_id, source in units.items():
        if (not isinstance(unit_id, str) or not unit_id or not isinstance(source, dict) or set(source) != {"run_id", "record_sha256"}
                or source["run_id"] not in runs or not isinstance(source["record_sha256"], str)
                or not _SHA256.fullmatch(source["record_sha256"])):
            raise ContractError(f"承接单元 {unit_id!r} 的来源非法或指向未列出的来源运行")
    if {source["run_id"] for source in units.values()} != set(runs):
        raise ContractError("来源运行与承接单元不闭合：列出的每个来源运行都要被至少一个单元引用")
    return sources


def binding(context: Mapping[str, Any], sources: Mapping[str, Any]) -> dict[str, str]:
    return {"contract": CONTRACT, "context_sha256": fs.digest(context), "sources_sha256": fs.digest(sources)}


def binding_sha256(context: Mapping[str, Any], sources: Mapping[str, Any]) -> str:
    """两类批准签的对象：上下文摘要与来源集合摘要（审批人对执行器提案里 ``binding.json`` 的这个值签字）。"""

    return fs.digest(binding(context, sources))


# ---------------------------------------------------------------------------
# 动态凭证：校时样本、撤销查询与批准
# ---------------------------------------------------------------------------


def _source_file(source: str, label: str) -> Path:
    path = Path(source[len("file:"):])
    if not source.startswith("file:") or not path.is_absolute() or ".." in path.parts:
        raise ContractError(f"{label}只能是 file:<规范绝对路径>：{source!r}")
    return path


def sample_clock(source: str, now: float) -> dict[str, Any]:
    """取一份校时样本（只采样不判定，判定由 ``full_set_receipt.clock_check`` 做）。

    ``chronyc``：执行 ``chronyc -c tracking``，第 5 个字段是系统时钟与 NTP 的偏差（秒），最后一个字段是闰秒状态，``Normal``
    且层级有效才算同步；命令不在、失败或输出认不出时样本标 ``unavailable``，判定随之失败（失败关闭）。``file:<路径>``：外部
    维护的样本文件原样取用（必须自带 ``sampled_at_utc``），另记来源与文件摘要。"""

    if source == DEFAULT_CLOCK_SOURCE:
        stamp = fs.timestamp(now)
        try:
            completed = subprocess.run(["chronyc", "-c", "tracking"], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError) as error:
            return {"status": "unavailable", "source": "chronyc", "reason": str(error)[:300], "sampled_at_utc": stamp}
        lines = completed.stdout.strip().splitlines()
        fields = lines[-1].split(",") if lines else []
        if completed.returncode != 0 or len(fields) < 14:
            return {"status": "unavailable", "source": "chronyc", "sampled_at_utc": stamp,
                    "reason": (completed.stderr.strip() or completed.stdout.strip())[-300:] or "chronyc 输出认不出"}
        try:
            offset, stratum = float(fields[4]), int(fields[2])
        except ValueError:
            return {"status": "unavailable", "source": "chronyc", "sampled_at_utc": stamp, "reason": lines[-1][:300]}
        leap = fields[13].strip()
        synchronized = leap == "Normal" and 0 < stratum < 16
        return {"status": "synchronized" if synchronized else "unsynchronized", "source": f"chronyc:{fields[1]}",
                "offset_seconds": offset, "stratum": stratum, "leap_status": leap, "sampled_at_utc": stamp}
    path = _source_file(str(source), "校时来源")
    sample = fs.read(path)
    return {**sample, "provider": source, "sample_file_sha256": fs.file_digest(path)}


def query_revocation(store_root: Path | str, run_ids: Sequence[str], stage: str, now: float,
                     campaign_status_source: str | None = None) -> dict[str, Any]:
    """撤销查询快照：本记录库里登记撤销的来源运行；``formal:`` 阶段另带 Campaign 状态（来源缺失或不可读即 ``unavailable``）。

    撤销登记文件只要存在就算撤销（内容读不出也按撤销处理，失败关闭）。"""

    root = Path(store_root).resolve()
    ordered = sorted(set(run_ids))
    snapshot: dict[str, Any] = {
        "schema_version": REVOCATION_QUERY_SCHEMA, "status": "ok", "queried_at_utc": fs.timestamp(now), "record_store": str(root),
        "stage": stage, "run_ids": ordered,
        "revoked_run_ids": [run_id for run_id in ordered if revocation_path(root, run_id).exists() or revocation_path(root, run_id).is_symlink()],
    }
    if stage.startswith("formal:"):
        try:
            if campaign_status_source is None:
                raise ContractError("Formal 阶段的撤销查询要 Campaign 状态，请求没有给出 campaign_status_source")
            status = fs.read(_source_file(campaign_status_source, "Campaign 状态来源"))
            if (status.get("schema_version") != CAMPAIGN_STATUS_SCHEMA or status.get("campaign_id") != stage.split(":", 1)[1]
                    or not isinstance(status.get("status"), str)):
                raise ContractError("Campaign 状态文件格式错误或不是本阶段的 Campaign")
            if not 0 <= now - fs.utc(status["observed_at_utc"]) <= fs.REVOCATION_MAX_AGE_SECONDS:
                raise ContractError("Campaign 状态过旧")
            snapshot["campaign_status"] = status["status"]
        except ERRORS as error:
            snapshot.update(status="unavailable", reason=str(error)[:300])
    return snapshot


def revocation_check(snapshot: Mapping[str, Any], *, context: Mapping[str, Any], sources: Mapping[str, Any], now: float,
                     check_age: bool = True) -> set[str]:
    """撤销查询快照必须成功、覆盖全部来源运行、绑定同一记录库与入口阶段、不超过 60 秒；Formal 阶段另要 Campaign active。
    返回被撤销的来源运行（只让指向它们的单元重跑）。"""

    if snapshot.get("schema_version") != REVOCATION_QUERY_SCHEMA or snapshot.get("status") != "ok":
        raise ContractError(f"撤销查询失败：{snapshot.get('reason') or snapshot.get('status')}")
    if snapshot.get("record_store") != context["record_store"] or snapshot.get("stage") != context["stage"]:
        raise ContractError("撤销查询的记录库或入口阶段与承接上下文不同")
    if snapshot.get("run_ids") != sorted(sources["runs"]):
        raise ContractError("撤销查询没有覆盖全部来源运行")
    revoked = snapshot.get("revoked_run_ids")
    if (not isinstance(revoked, list) or revoked != sorted(set(revoked)) or not set(revoked) <= set(sources["runs"])):
        raise ContractError("撤销查询的撤销集合非法")
    queried = fs.utc(snapshot["queried_at_utc"])
    if check_age and not 0 <= now - queried <= fs.REVOCATION_MAX_AGE_SECONDS:
        raise ContractError("撤销查询过旧（超过 60 秒）或来自未来")
    if str(context["stage"]).startswith("formal:") and snapshot.get("campaign_status") != "active":
        raise ContractError("Formal 阶段所属 Campaign 不再 active")
    return set(revoked)


def approvals_check(approvals: Sequence[Mapping[str, Any]], expected_binding: str, now: float) -> None:
    """两类批准（角色、形状与有效期同 B-09）：都要已批准、有账号、scope ``unit-inherit/v1``、绑定本上下文与来源集合、此刻有效。"""

    if {item.get("role") for item in approvals} != fs.APPROVAL_ROLES or len(approvals) != len(fs.APPROVAL_ROLES):
        raise ContractError("缺少三方审核及变更批准")
    for approval in approvals:
        if (approval.get("status") != "approved" or not approval.get("account") or approval.get("scope") != CONTRACT
                or approval.get("binding_sha256") != expected_binding
                or not fs.utc(approval["approved_at_utc"]) <= now < fs.utc(approval["expires_at_utc"])):
            raise ContractError("专项批准缺失、错绑、已撤回或过期")


# ---------------------------------------------------------------------------
# 请求与固定的授权对象
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixed:
    """一次运行固定不变的授权对象：首个复核点读出并封存，之后每个复核点重读请求核对未变。"""

    request: dict[str, Any]          # 请求原文里固定的部分（不含两项动态凭证）
    request_ref: dict[str, str]      # 请求文件的原始引用
    sealed_request: dict[str, str]   # 封存进记录库的请求（引用改写成封存副本）
    context: dict[str, Any]
    context_ref: dict[str, str]      # 封存副本
    sources: dict[str, Any]
    approvals: tuple[dict[str, Any], ...]
    approval_refs: tuple[dict[str, str], ...]   # 封存副本
    request_clock: dict[str, str]                # 请求自带校时凭证的封存副本
    request_revocation: dict[str, str]           # 请求自带撤销快照的封存副本
    binding_sha256: str
    reuse_enabled: bool
    max_age_hours: float
    clock_source: str
    campaign_status_source: str | None


def _request_shape(request: Any) -> dict[str, Any]:
    if not isinstance(request, dict) or request.get("schema_version") != REQUEST_SCHEMA:
        raise ContractError("逐单元承接请求格式错误")
    missing = sorted(REQUEST_FIELDS - set(request))
    extra = sorted(set(request) - REQUEST_FIELDS - REQUEST_OPTIONAL_FIELDS)
    if missing or extra:
        raise ContractError(f"逐单元承接请求缺少 {missing} 或含未知字段 {extra}")
    if type(request["reuse_enabled"]) is not bool:
        raise ContractError("reuse_enabled 必须是布尔值")
    if not isinstance(request["approvals"], list):
        raise ContractError("approvals 必须是批准引用列表")
    max_age = request.get("max_age_hours", records().MAX_AGE_HOURS)
    if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or not 0 < float(max_age) <= records().MAX_AGE_HOURS:
        raise ContractError(f"承接期限只能在 0～{records().MAX_AGE_HOURS:g} 小时之间（只能调小）")
    clock_source = request.get("clock_source", DEFAULT_CLOCK_SOURCE)
    if clock_source != DEFAULT_CLOCK_SOURCE:
        _source_file(str(clock_source), "校时来源")
    if request.get("campaign_status_source") is not None:
        _source_file(str(request["campaign_status_source"]), "Campaign 状态来源")
    return request


def _fixed_part(request: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in request.items() if key not in ("consumer_clock", "revocation")}


def _replay_originals(request: Mapping[str, Any]) -> None:
    """请求引用的上下文与两份批准的原文件仍在原处、内容没变：审批人撤回批准（改动或删除批准文件）即不再承接、不再签发。"""

    fs.replay_ref(request["context"])
    for reference in request["approvals"]:
        fs.replay_ref(reference)


def _read_request(request_path: Path | str) -> tuple[dict[str, str], dict[str, Any]]:
    """同一次读取得到请求文件的引用与内容（解析与摘要消费同一批字节）。"""

    path = Path(request_path)
    raw = fs.file_bytes(path)
    request = json.loads(raw)
    return {"path": str(path), "sha256": records().sha256_bytes(raw)}, _request_shape(request)


def seal_request(request_path: Path | str, store_root: Path | str, *, now: float) -> Fixed:
    """首个复核点：读请求、校验固定部分与两项初始凭证，并把请求、上下文、批准与初始凭证封存进 ``store_root`` 的证据目录
    （正式运行是记录库；只判定时是运行目录，不进记录库）。"""

    request_ref, request = _read_request(request_path)
    context = context_check(fs.replay_ref(request["context"]))
    sources = sources_check(request["sources"])
    approvals = tuple(fs.replay_ref(ref) for ref in request["approvals"])
    expected = binding_sha256(context, sources)
    approvals_check(approvals, expected, now)
    # 请求自带的两项凭证是组装请求时的证据：内容合法、采样时间落在每份批准的有效期内且不在未来；时效由各复核点刷新的快照判。
    clock = fs.replay_ref(request["consumer_clock"])
    sampled = fs.utc(clock["sampled_at_utc"])
    fs.clock_check(request["consumer_clock"], sampled, reference_time=sampled)
    revocation = fs.replay_ref(request["revocation"])
    queried = fs.utc(revocation["queried_at_utc"])
    revocation_check(revocation, context=context, sources=sources, now=queried, check_age=False)
    for moment, label in ((sampled, "校时凭证"), (queried, "撤销查询")):
        if moment > now + fs.CLOCK_SKEW_SECONDS:
            raise ContractError(f"请求自带的{label}来自未来")
        if any(not fs.utc(item["approved_at_utc"]) <= moment < fs.utc(item["expires_at_utc"]) for item in approvals):
            raise ContractError(f"请求自带的{label}不在批准有效期内组装")
    sealed_context = seal_evidence(store_root, request["context"])
    sealed_approvals = tuple(seal_evidence(store_root, ref) for ref in request["approvals"])
    sealed_clock = seal_evidence(store_root, request["consumer_clock"])
    sealed_revocation = seal_evidence(store_root, request["revocation"])
    sealed = put_evidence(store_root, {**request, "context": sealed_context, "approvals": list(sealed_approvals),
                                       "consumer_clock": sealed_clock, "revocation": sealed_revocation})
    return Fixed(request=_fixed_part(request), request_ref=request_ref, sealed_request=sealed, context=context,
                 context_ref=sealed_context, sources=sources, approvals=approvals, approval_refs=sealed_approvals,
                 request_clock=sealed_clock, request_revocation=sealed_revocation, binding_sha256=expected,
                 reuse_enabled=request["reuse_enabled"], max_age_hours=float(request.get("max_age_hours", records().MAX_AGE_HOURS)),
                 clock_source=str(request.get("clock_source", DEFAULT_CLOCK_SOURCE)),
                 campaign_status_source=request.get("campaign_status_source"))


def replay_fixed(decision: Mapping[str, Any]) -> Fixed:
    """消费端：从决策收据登记的封存副本还原固定的授权对象，逐份核对摘要与决策收据登记的一致。"""

    sealed = fs.replay_ref(decision["sealed_request"])
    request = _request_shape(sealed)
    context = context_check(fs.replay_ref(request["context"]))
    sources = sources_check(request["sources"])
    if request["context"] != decision["context"] or [dict(ref) for ref in request["approvals"]] != list(decision["approvals"]):
        raise ContractError("决策收据登记的封存上下文或批准与封存请求不一致")
    approvals = tuple(fs.replay_ref(ref) for ref in request["approvals"])
    expected = binding_sha256(context, sources)
    if (fs.digest(context) != decision["context_sha256"] or fs.digest(sources) != decision["sources_sha256"]
            or expected != decision["binding_sha256"] or sorted(sources["runs"]) != decision["source_run_ids"]):
        raise ContractError("决策收据登记的上下文、来源集合或批准绑定与封存副本不符")
    return Fixed(request=_fixed_part(request), request_ref=dict(decision["request"]), sealed_request=dict(decision["sealed_request"]),
                 context=context, context_ref=dict(request["context"]), sources=sources, approvals=approvals,
                 approval_refs=tuple(dict(ref) for ref in request["approvals"]), request_clock=dict(request["consumer_clock"]),
                 request_revocation=dict(request["revocation"]), binding_sha256=expected, reuse_enabled=request["reuse_enabled"],
                 max_age_hours=float(request.get("max_age_hours", records().MAX_AGE_HOURS)),
                 clock_source=str(request.get("clock_source", DEFAULT_CLOCK_SOURCE)),
                 campaign_status_source=request.get("campaign_status_source"))


# ---------------------------------------------------------------------------
# 逐单元判定
# ---------------------------------------------------------------------------


def _carry_host_inputs(current: Any, record: Mapping[str, Any]) -> None:
    """沿用来源记录登记的宿主路径与隔离合同（B-10 同法）：每次重新快照，只补当前声明里没有的项，不缩小当前仓库声明，也不
    拿来源摘要冒充现场事实——快照与来源不同时 ``check_record`` 判输入变了。"""

    R = records()
    names = {entry["name"] for entry in current.inputs or []}
    additions = []
    for entry in record.get("inputs") or []:
        name = entry.get("name") if isinstance(entry, Mapping) else None
        if not isinstance(name, str) or name in names:
            continue
        if name.startswith("host:"):
            # 按来源条目的合同版本（v2／v3）和元数据依赖声明重新快照：v2 条目与过去的 host_snapshot(name[5:]) 逐字相同，
            # v3 条目取 v3，摘要才能与来源记录可比。
            additions.append(R._audit_module().host_snapshot_like(entry))
        elif name == "runtime-contract":
            additions.append(R._audit_module()._runtime_module().contract_entry(entry["detail"]["contract"]))
        elif name == "require-read-audit":
            additions.append(R.value_entry("policy", name, "all-file-paths/v1"))
    if additions:
        current.inputs = R._unique([*(current.inputs or []), *additions])
        current.inputs_sha256 = R.entries_sha256(current.inputs)


def assess(fixed: Fixed, currents: Mapping[str, Any], facts: Any, store_root: Path | str, *, now: float,
           revoked: set[str]) -> dict[str, Any]:
    """形成期与消费端绑定同一固定四control adapter，普通来源仍调用原完整判断。"""
    with _control_reader_scope(facts.executor, fixed.context["client"], list(currents)):
        return _assess_current(fixed, currents, facts, store_root, now=now, revoked=revoked)


def _assess_current(fixed: Fixed, currents: Mapping[str, Any], facts: Any, store_root: Path | str, *, now: float,
           revoked: set[str]) -> dict[str, Any]:
    """逐单元判定：来源集合登记了、来源运行没撤销、来源清单已发布且摘要相符、``check_record``（要求读集闭合）全部通过才承接，
    其余单元各自执行。"""

    R = records()
    store = R.RecordStore(Path(store_root))
    facts = replace(facts, require_read_audit=True, max_age_hours=min(float(facts.max_age_hours), fixed.max_age_hours), now=now,
                    historical_read_audit=False, allow_partial_origin=True)   # 第 16 项：逐单元承接接受后继封存的部分完成清单
    decisions: dict[str, Any] = {}
    for unit_id, current in currents.items():
        source = fixed.sources["units"].get(unit_id)
        if source is None:
            decisions[unit_id] = R.Decision(reasons=["来源集合没有登记这个单元：执行"])
            continue
        run_id, digest = source["run_id"], source["record_sha256"]
        if run_id in revoked:
            decisions[unit_id] = R.Decision(reasons=[f"来源运行 {run_id} 已撤销：只重跑指向它的单元"])
            continue
        if not current.inheritable:
            decisions[unit_id] = R.Decision(reasons=[current.reason or "本单元不可承接"])
            continue
        origin = store.manifest(run_id)
        if origin is None or origin.get("manifest_sha256") != fixed.sources["runs"][run_id]:
            decisions[unit_id] = R.Decision(reasons=[f"来源运行 {run_id} 的清单没有发布进记录库，或与来源集合登记的摘要不同"])
            continue
        path = store.record_path(unit_id, digest)
        record = R._read_json(path)
        try:
            if record is not None:
                _carry_host_inputs(current, record)
        except (OSError, ValueError, RuntimeError) as error:
            decisions[unit_id] = R.Decision(reasons=[f"来源登记的宿主输入快照失败：{error}"])
            continue
        reasons = R.check_record(store, path, record, current, facts)
        if record is not None and (record.get("run") or {}).get("run_id") != run_id:
            reasons.append("记录不属于来源集合登记的运行")
        decisions[unit_id] = (R.Decision(record, path, []) if not reasons
                              else R.Decision(reasons=[f"来源记录不能承接：{'；'.join(reasons)}"]))
    return decisions


def propose(store_root: Path | str, currents: Mapping[str, Any], facts: Any, *, max_age_hours: float | None = None,
            client: str = "") -> dict[str, Any]:
    """提案沿用已计算的上下文客户端及执行器绑定，和形成、消费共用固定四control合同。"""
    with _control_reader_scope(facts.executor, client, list(currents)):
        return _propose_current(store_root, currents, facts, max_age_hours=max_age_hours)


def _propose_current(store_root: Path | str, currents: Mapping[str, Any], facts: Any, *, max_age_hours: float | None = None) -> dict[str, Any]:
    """为当前每个可承接的单元在记录库里找完成时间最新、要求读集闭合时仍能承接的正式记录，组成来源集合草案（执行器
    ``--decide-only --unit-inherit-propose`` 调用）。找不到的单元不列入，运行时执行。``currents`` 不被改动。"""

    R = records()
    store = R.RecordStore(Path(store_root))
    limit = min(float(facts.max_age_hours), float(max_age_hours or R.MAX_AGE_HOURS))
    facts = replace(facts, require_read_audit=True, max_age_hours=limit, historical_read_audit=False, allow_partial_origin=True)
    units: dict[str, dict[str, str]] = {}
    runs: dict[str, str] = {}

    def completed(item: tuple[Path, dict[str, Any] | None]) -> tuple[float, str]:
        return (R.parse_utc((item[1] or {}).get("completed_at_utc")) or 0.0, item[0].name)

    for unit_id, current in sorted(currents.items()):
        if not current.inheritable:
            continue
        for path, record in sorted(store.candidates(unit_id), key=completed, reverse=True):
            if record is None or record.get("kind") != "formal":
                continue
            trial = replace(current, inputs=list(current.inputs or []))
            try:
                _carry_host_inputs(trial, record)
            except (OSError, ValueError, RuntimeError):
                continue
            if R.check_record(store, path, record, trial, facts, thorough=False) or R.check_record(store, path, record, trial, facts):
                continue
            run_id = str(record["run"]["run_id"])
            origin = store.manifest(run_id)
            if origin is None:
                continue
            units[unit_id] = {"run_id": run_id, "record_sha256": str(record["record_sha256"])}
            runs[run_id] = str(origin["manifest_sha256"])
            break
    return {"schema_version": SOURCES_SCHEMA, "runs": runs, "units": units}


def write_proposal(directory: Path | str, *, context: Mapping[str, Any], sources: Mapping[str, Any]) -> dict[str, Any]:
    """提案目录（新建，不覆盖）：上下文、来源集合、批准要签的绑定与请求草案。来源集合为空时没有可承接的单元，不写草案。"""

    root = Path(directory)
    root.mkdir(parents=True, mode=0o700)
    context_path = _write_once(root / "context.json", context)
    sources_path = _write_once(root / "sources.json", sources)
    summary = {"schema_version": PROPOSAL_SCHEMA, **binding(context, sources), "binding_sha256": binding_sha256(context, sources),
               "units": len(sources["units"]), "runs": len(sources["runs"]), "context": fs.reference(context_path),
               "sources": fs.reference(sources_path),
               "statement": "两类批准（third_party_reviewer、change_approver）对 binding_sha256 签字，scope unit-inherit/v1；"
                            "批准与校时、撤销查询凭证由调用方提供，执行器只核对"}
    _write_once(root / "binding.json", summary)
    return summary


def assemble_request(proposal: Path | str, *, clock: Path | str, revocation: Path | str, approvals: Sequence[Path | str],
                     reuse_enabled: bool, output: Path | str, max_age_hours: float | None = None, clock_source: str | None = None,
                     campaign_status_source: str | None = None) -> dict[str, Any]:
    root = Path(proposal)
    summary = fs.read(root / "binding.json")
    sources = sources_check(fs.read(root / "sources.json"))
    if summary.get("schema_version") != PROPOSAL_SCHEMA or fs.reference(root / "sources.json") != summary["sources"]:
        raise ContractError("提案目录被改过或不是执行器写出的提案")
    request: dict[str, Any] = {
        "schema_version": REQUEST_SCHEMA, "reuse_enabled": reuse_enabled, "context": fs.reference(root / "context.json"),
        "sources": sources, "consumer_clock": fs.reference(Path(clock)), "revocation": fs.reference(Path(revocation)),
        "approvals": [fs.reference(Path(item)) for item in approvals],
    }
    if request["context"] != summary["context"]:
        raise ContractError("提案目录的上下文被改过")
    if max_age_hours is not None:
        request["max_age_hours"] = max_age_hours
    if clock_source is not None:
        request["clock_source"] = clock_source
    if campaign_status_source is not None:
        request["campaign_status_source"] = campaign_status_source
    _request_shape(request)
    _write_once(Path(output), request)
    return request


# ---------------------------------------------------------------------------
# 一次运行的承接会话：三个复核点与决策收据
# ---------------------------------------------------------------------------


class Session:
    """执行器一次 ``run-gates --mode unit-inherit`` 的承接会话。复核点任一授权失败即标记拒绝：排队前、锁后的拒绝让整次运行
    重新执行，发布前的拒绝让清单不发布；拒绝之后不再承接。"""

    def __init__(self, request_path: Path | str, *, store_root: Path | str, run_id: str,
                 evidence_root: Path | str | None = None) -> None:
        self.request_path = Path(request_path)
        self.store_root = Path(store_root).resolve()
        # 证据快照（封存的请求、各复核点刷新的凭证）写在哪里：缺省记录库；执行器只判定时给运行目录，不写记录库。
        self.evidence_root = Path(evidence_root).resolve() if evidence_root is not None else self.store_root
        self.run_id = run_id
        self.fixed: Fixed | None = None
        self.checkpoints: list[dict[str, Any]] = []
        self.decisions: dict[str, Any] = {}
        self.refused = False
        self.publish_refused = False
        self.reasons: list[str] = []   # 会话级的回退原因（例如复核通过但没有可承接的单元）

    def fallback(self, reason: str) -> None:
        """执行器决定整次重新执行（复核通过但没有可承接的单元）：之后不再承接。"""

        self.reasons.append(reason)
        self.refused = True

    @property
    def authorized(self) -> bool:
        return not self.refused and bool(self.checkpoints) and self.checkpoints[-1]["authorized"] is True

    @property
    def max_age_hours(self) -> float:
        return self.fixed.max_age_hours if self.fixed is not None else float(records().MAX_AGE_HOURS)

    def _refresh(self, name: str, now: float) -> tuple[dict[str, str], dict[str, str]]:
        """重新获取校时与撤销查询快照，以新文件、新摘要追加留证（内容寻址，不覆盖旧凭证）。"""

        assert self.fixed is not None
        clock = sample_clock(self.fixed.clock_source, now)
        clock_ref = put_evidence(self.evidence_root, {**clock, "collected_for": {"run_id": self.run_id, "checkpoint": name}})
        revocation = query_revocation(self.store_root, sorted(self.fixed.sources["runs"]), self.fixed.context["stage"], now,
                                      self.fixed.campaign_status_source)
        revocation_ref = put_evidence(self.evidence_root, {**revocation, "collected_for": {"run_id": self.run_id, "checkpoint": name}})
        return clock_ref, revocation_ref

    def checkpoint(self, name: str, currents: Mapping[str, Any], facts: Any, context: Mapping[str, Any] | None, *,
                   now: float) -> dict[str, Any]:
        """一个复核点：返回逐单元判定。``context`` 是执行器现算的当前上下文（算不出时给 None，按拒绝处理）。"""

        R = records()
        now = _moment(now)
        entry: dict[str, Any] = {"checkpoint": name, "decided_at_utc": fs.timestamp(now), "authorized": False, "reasons": [],
                                 "consumer_clock": None, "revocation": None, "revoked_run_ids": [], "inherited": {}}
        self.checkpoints.append(entry)
        label = {"queue": "排队前复核", "lock": "锁后复核", "publish": "发布前复核"}.get(name, name)
        try:
            order = tuple(item["checkpoint"] for item in self.checkpoints)
            if order != CHECKPOINTS[:len(order)]:
                raise ContractError(f"复核点顺序非法：{list(order)}（只能依次是排队前、锁后、发布前）")
            if self.refused:
                raise ContractError("此前已拒绝承接，不再承接：" + "；".join(self.reasons or ["此前的复核点拒绝"]))
            if self.fixed is None:
                self.fixed = seal_request(self.request_path, self.evidence_root, now=now)
            else:
                _ref, again = _read_request(self.request_path)
                if _fixed_part(again) != self.fixed.request:
                    raise ContractError("请求文件的上下文、来源集合、批准或开关在运行期间被改动")
                _replay_originals(again)
            fixed = self.fixed
            if context is None:
                raise ContractError("算不出当前上下文")
            if dict(context) != fixed.context:
                changed = sorted(key for key in CONTEXT_FIELDS if context.get(key) != fixed.context.get(key))
                raise ContractError(f"当前上下文与请求登记的不同：{'、'.join(changed)}")
            clock_ref, revocation_ref = self._refresh(name, now)
            entry.update(consumer_clock=clock_ref, revocation=revocation_ref)
            fs.clock_check(clock_ref, now)
            revoked = revocation_check(fs.replay_ref(revocation_ref), context=fixed.context, sources=fixed.sources, now=now)
            # 请求自带撤销快照里标出的撤销同样生效（撤销不可撤回，刷新快照理应包含）。
            revoked |= set(fs.replay_ref(fixed.request_revocation).get("revoked_run_ids") or [])
            entry["revoked_run_ids"] = sorted(revoked)
            approvals_check(fixed.approvals, fixed.binding_sha256, now)
            decisions = assess(fixed, currents, facts, self.store_root, now=now, revoked=revoked)
            if name != "queue":
                # 锁后与发布前只能缩小承接集合：此前复核点没承接、或承接的不是同一条记录的单元一律执行。
                for unit_id, decision in decisions.items():
                    before = self.decisions.get(unit_id)
                    if decision.inherit and not (before is not None and before.inherit
                                                 and before.record["record_sha256"] == decision.record["record_sha256"]):
                        decisions[unit_id] = R.Decision(reasons=[f"{label}：此前的复核点没有承接它，不扩大承接集合"])
            entry["inherited"] = {unit_id: decision.record["record_sha256"] for unit_id, decision in sorted(decisions.items())
                                  if decision.inherit}
            if not fixed.reuse_enabled:
                raise ContractError("真实承接开关关闭（reuse_enabled=false），只记录可承接判定")
            entry["authorized"] = True
            if name != "publish":
                self.decisions = decisions
            return decisions
        except ERRORS as error:
            entry["reasons"].append(str(error))
            self.refused = True
            if name == "publish":
                self.publish_refused = True
            return {unit_id: R.Decision(reasons=[f"逐单元承接{label}拒绝：{error}"]) for unit_id in currents}

    def publish_problems(self, published: Mapping[str, Any]) -> list[str]:
        """发布前复核的结论：复核被拒，或锁后承接的任一单元此刻不再可承接（或改了来源记录）——清单不能发布。"""

        last = self.checkpoints[-1] if self.checkpoints else {}
        if last.get("checkpoint") != "publish":
            return ["逐单元承接缺少发布前复核"]
        if last.get("authorized") is not True:
            return [f"逐单元承接发布前复核拒绝：{'；'.join(last.get('reasons') or [])}"]
        problems = []
        for unit_id, decision in sorted(self.decisions.items()):
            if not decision.inherit:
                continue
            now = published.get(unit_id)
            if now is None or not now.inherit or now.record["record_sha256"] != decision.record["record_sha256"]:
                problems.append(f"{unit_id}：发布前复核不再可承接：{'；'.join(getattr(now, 'reasons', None) or ['单元缺失'])}")
        if problems:
            # 授权本身通过、但承接项此刻已失效（例如执行期间来源被撤销）：同样拒绝发布。
            last["reasons"].extend(problems[:20])
            self.refused = self.publish_refused = True
        return problems

    def action(self) -> str:
        """``inherit-matching-units``（三个复核点都通过）、``publish-refused``（发布前复核拒绝，清单不发布）、``reexecute-all``
        （排队前或锁后拒绝、或没有可承接的单元，整次重新执行）、``pending``（还没走完复核点）。"""

        if self.publish_refused:
            return "publish-refused"
        if self.refused or not self.checkpoints or self.fixed is None:
            return "reexecute-all"
        return "inherit-matching-units" if tuple(item["checkpoint"] for item in self.checkpoints) == CHECKPOINTS else "pending"

    def verdict(self) -> dict[str, Any]:
        """写进执行器汇总与运行目录的概要（不含逐单元明细）。"""

        return {"schema_version": records().UNIT_INHERIT_DECISION_SCHEMA, "contract": CONTRACT, "run_id": self.run_id,
                "action": self.action(), "request": str(self.request_path), "reasons": list(self.reasons),
                "checkpoints": [{key: item[key] for key in ("checkpoint", "decided_at_utc", "authorized", "reasons")}
                                | {"inherited": len(item["inherited"])} for item in self.checkpoints],
                "reuse_enabled": self.fixed.reuse_enabled if self.fixed is not None else None}

    def build_decision(self, decisions: Mapping[str, Any]) -> dict[str, Any]:
        """决策收据（``unit-inherit-decision/v1``）：``decisions`` 是本次运行实际的执行／承接划分。"""

        R = records()
        fixed = self.fixed
        units: dict[str, Any] = {}
        for unit_id in sorted(decisions):
            decision = decisions[unit_id]
            if decision.inherit:
                units[unit_id] = {"disposition": "inherited", "reasons": [],
                                  "source": {"run_id": decision.record["run"]["run_id"], "record_sha256": decision.record["record_sha256"]}}
            else:
                units[unit_id] = {"disposition": "executed", "reasons": list(decision.reasons), "source": None}
        body = {
            "schema_version": R.UNIT_INHERIT_DECISION_SCHEMA, "contract": CONTRACT, "run_id": self.run_id,
            "record_store": str(self.store_root), "action": self.action(), "reasons": list(self.reasons),
            "decided_at_utc": self.checkpoints[-1]["decided_at_utc"] if self.checkpoints else None,
            "request": fixed.request_ref if fixed else {"path": str(self.request_path), "sha256": None},
            "sealed_request": fixed.sealed_request if fixed else None, "context": fixed.context_ref if fixed else None,
            "context_sha256": fs.digest(fixed.context) if fixed else None, "sources_sha256": fs.digest(fixed.sources) if fixed else None,
            "binding_sha256": fixed.binding_sha256 if fixed else None,
            "source_run_ids": sorted(fixed.sources["runs"]) if fixed else [],
            "approvals": list(fixed.approval_refs) if fixed else [], "reuse_enabled": fixed.reuse_enabled if fixed else None,
            "max_age_hours": self.max_age_hours, "clock_source": fixed.clock_source if fixed else None,
            "campaign_status_source": fixed.campaign_status_source if fixed else None,
            "checkpoints": [dict(item) for item in self.checkpoints], "units": units,
        }
        return R.seal(body, "decision_sha256")

    def publish_decision(self, decision: Mapping[str, Any]) -> dict[str, str]:
        """决策收据发布进记录库（只增不改），返回写进运行清单的引用。"""

        R = records()
        if decision.get("action") != "inherit-matching-units" or not R.seal_ok(decision, "decision_sha256"):
            raise ContractError("只有三个复核点都通过的决策收据才能发布")
        path = _write_once(R.RecordStore(self.store_root).decision_path(self.run_id), decision)
        return {"path": str(path), "decision_sha256": str(decision["decision_sha256"]),
                "context_sha256": str(decision["context_sha256"]), "sources_sha256": str(decision["sources_sha256"])}


# ---------------------------------------------------------------------------
# 消费端：形成期证明重验与消费登记
# ---------------------------------------------------------------------------


def verifier_files() -> dict[str, str]:
    files = {name: fs.file_digest(HERE / name) for name in VERIFIER_FILES}
    adapter = _CONTROL_READ_AUDIT.get()
    if adapter is not None:
        files.update({path.name: fs.file_digest(path) for path in adapter.sidecar_paths()})
    return files


def _checkpoint_proof(item: Mapping[str, Any], fixed: Fixed, inherited_runs: set[str]) -> float:
    """一个形成期复核点按它自己的快照与判定时间成立：校时有效、撤销查询有效且没撤销承接项指向的运行、批准此刻有效。"""

    moment = fs.utc(item["decided_at_utc"])
    if item.get("authorized") is not True:
        raise ContractError(f"决策收据的复核点 {item.get('checkpoint')} 不是通过")
    fs.clock_check(item["consumer_clock"], moment)
    revoked = revocation_check(fs.replay_ref(item["revocation"]), context=fixed.context, sources=fixed.sources, now=moment)
    # 复核点登记的撤销集合＝刷新快照的撤销∪请求自带快照的撤销（执行器同一算法）。
    revoked |= set(fs.replay_ref(fixed.request_revocation).get("revoked_run_ids") or [])
    if revoked != set(item.get("revoked_run_ids") or []):
        raise ContractError(f"复核点 {item.get('checkpoint')} 登记的撤销集合与快照不符")
    if revoked & inherited_runs:
        raise ContractError(f"复核点 {item.get('checkpoint')} 时承接项指向的来源运行已撤销：{sorted(revoked & inherited_runs)}")
    approvals_check(fixed.approvals, fixed.binding_sha256, moment)
    return moment


def _consumption_proof(record: Mapping[str, Any], decision: Mapping[str, Any], fixed: Fixed, inherited_runs: set[str]) -> float:
    R = records()
    if (record.get("schema_version") != CONSUMPTION_SCHEMA or not R.seal_ok(record, "consumption_sha256")
            or record.get("run_id") != decision["run_id"] or record.get("decision_sha256") != decision["decision_sha256"]):
        raise ContractError("消费登记格式错误、自摘要不符或不属于这份决策收据")
    moment = fs.utc(record["consumed_at_utc"])
    fs.clock_check(record["consumer_clock"], moment)
    revoked = revocation_check(fs.replay_ref(record["revocation"]), context=fixed.context, sources=fixed.sources, now=moment)
    if revoked & inherited_runs:
        raise ContractError(f"消费时承接项指向的来源运行已撤销：{sorted(revoked & inherited_runs)}")
    approvals_check(fixed.approvals, fixed.binding_sha256, moment)
    return moment


def verify_published(manifest: Mapping[str, Any], store_root: Path | str, *, purpose: str, now: float | None = None,
                     historical: bool = False, consumption: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """从同库正式decision的客户端与本清单executor选择当前消费合同，不信任自填开关。"""
    client = ""
    if {"control_read_audit.py", "control_read_audit_runtime.py"} & set((manifest.get("executor") or {}).get("files") or {}):
        store = _LEGACY_RECORDS().RecordStore(Path(store_root).resolve())
        decision = _LEGACY_RECORDS()._read_json(store.decision_path(str(manifest.get("run_id") or "")))
        # context是封存引用，不能当内嵌对象取client；先按原合同重验引用与固定绑定。
        client = replay_fixed(decision).context["client"] if decision is not None else ""
    with _control_reader_scope(manifest.get("executor") or {}, client,
                               [item["unit_id"] for item in manifest.get("units") or []]):
        return _verify_published_current(manifest, store_root, purpose=purpose, now=now,
                                          historical=historical, consumption=consumption)


def _verify_published_current(manifest: Mapping[str, Any], store_root: Path | str, *, purpose: str, now: float | None = None,
                     historical: bool = False, consumption: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """消费端重验一次逐单元承接运行的形成期证明（规则见模块说明）。任一不满足抛 ``ContractError``（调用方拒签）。

    返回 ``{run_id, decision_sha256, inherited, executed, consumption, verifier}``；``consumption`` 是本次写的消费登记引用（历史
    重放时是传入的那一份或 None）。"""

    R = records()
    if not isinstance(purpose, str) or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", purpose):
        raise ContractError("消费用途非法")
    root = Path(store_root).resolve()
    store = R.RecordStore(root)
    manifest = dict(manifest)
    run_id = str(manifest.get("run_id") or "")
    if manifest.get("mode") != MODE or not _RUN_ID.fullmatch(run_id):
        raise ContractError("不是逐单元承接（unit-inherit）的运行清单")
    published = store.manifest(run_id)
    if published is None or published != manifest:
        raise ContractError("运行清单没有发布进记录库，或与发布的那一份不同")
    problems = R.verify_manifest(manifest, store=store, historical=True)
    if problems:
        raise ContractError(f"运行清单自检不通过：{'；'.join(problems[:5])}")
    decision = R._read_json(store.decision_path(run_id))
    if decision is None:   # verify_manifest 已核对决策收据在库且与清单一致；这里只防竞态删除
        raise ContractError("决策收据不在记录库里")
    fixed = replay_fixed(decision)
    if fixed.context["record_store"] != str(root):
        raise ContractError("决策收据绑定的不是这个记录库")
    if (manifest.get("environment_sha256") != fixed.context["environment_sha256"]
            or (manifest.get("executor") or {}).get("sha256") != fixed.context["executor_sha256"]
            or manifest.get("policy_sha256") != fixed.context["policy_sha256"]):
        raise ContractError("运行清单的环境指纹、执行器或调度策略与决策收据登记的上下文不同")
    if not fixed.reuse_enabled:
        raise ContractError("决策收据登记的承接开关是关闭的")
    inherited = [entry for entry in manifest["units"] if entry.get("disposition") == "inherited"]
    inherited_runs = {str((entry.get("basis") or {}).get("run_id")) for entry in inherited}
    checkpoints = decision["checkpoints"]
    if tuple(item.get("checkpoint") for item in checkpoints) != CHECKPOINTS:
        raise ContractError("决策收据的复核点不完整（要依次有排队前、锁后、发布前三次复核）")
    publish_at = 0.0
    for item in checkpoints:
        publish_at = _checkpoint_proof(item, fixed, inherited_runs)
        for entry in inherited:
            if item["inherited"].get(entry["unit_id"]) != entry["record_sha256"]:
                raise ContractError(f"{entry['unit_id']}：复核点 {item['checkpoint']} 没有承接这条记录")
    # 按决策收据登记的上下文重做 check_record：形成期事实（环境、执行器、策略、期限）、发布前复核时刻、封存的宿主快照。
    facts = R.RunFacts(policy_sha256=fixed.context["policy_sha256"], environment=list(manifest.get("environment") or []),
                       environment_sha256=fixed.context["environment_sha256"], executor=dict(manifest.get("executor") or {}),
                       max_age_hours=float(decision["max_age_hours"]), now=publish_at, require_read_audit=True,
                       historical_read_audit=True, allow_partial_origin=True)
    for entry in inherited:
        unit_id = str(entry["unit_id"])
        source = fixed.sources["units"].get(unit_id)
        if source is None or source != decision["units"][unit_id]["source"]:
            raise ContractError(f"{unit_id}：承接项不在请求的来源集合里，或与决策收据登记的来源不同")
        origin = store.manifest(source["run_id"])
        if origin is None or origin.get("manifest_sha256") != fixed.sources["runs"][source["run_id"]]:
            raise ContractError(f"{unit_id}：来源运行清单缺失或与来源集合登记的摘要不同")
        path = store.record_path(unit_id, source["record_sha256"])
        record = R._read_json(path)
        if record is None:
            raise ContractError(f"{unit_id}：来源记录不在记录库里")
        current = R.Current(unit_id=unit_id, unit_type=str(record.get("unit_type")), spec=record.get("spec") or {},
                            spec_sha256=str(entry.get("spec_sha256")), inputs=list(record.get("inputs") or []),
                            inputs_sha256=str(entry.get("inputs_sha256")), inheritable=True)
        if R._narrowed_source(record):
            # 窄化承接项（第 10a 项）：消费端不能重新快照，当前侧视图摘要与测试树坐标只能取自清单承接项的 inherit_check
            # （执行器写清单时现算；与 verify_manifest 同口径）。缺失或格式不对即拒绝（失败关闭）。
            shape = R._inherit_check_problem(entry, record)
            if shape is not None:
                raise ContractError(f"{unit_id}：{shape}")
            current.inherit_repo_root = entry["inherit_check"]["repo_root"]
            current.inherit_inputs_sha256 = entry["inherit_check"]["inherit_inputs_sha256"]
            current.inherit_data_root = entry["inherit_check"]["data_root"]
        reasons = R.check_record(store, path, record, current, facts)
        if reasons:
            raise ContractError(f"{unit_id}：按决策收据登记的上下文重做承接核对不通过：{'；'.join(reasons)}")
    result = {"run_id": run_id, "decision_sha256": decision["decision_sha256"], "inherited": len(inherited),
              "executed": sum(1 for entry in manifest["units"] if entry.get("disposition") == "executed"),
              "consumption": None, "verifier": verifier_files()}
    if historical:
        if consumption is not None:
            _consumption_proof(fs.replay_ref(dict(consumption)), decision, fixed, inherited_runs)
            result["consumption"] = dict(consumption)
        return result
    moment = _moment(current_time() if now is None else float(now))
    clock = sample_clock(fixed.clock_source, moment)
    clock_ref = put_evidence(root, {**clock, "collected_for": {"run_id": run_id, "checkpoint": f"consume:{purpose}"}})
    fs.clock_check(clock_ref, moment)
    revocation = query_revocation(root, sorted(fixed.sources["runs"]), fixed.context["stage"], moment, fixed.campaign_status_source)
    revocation_ref = put_evidence(root, {**revocation, "collected_for": {"run_id": run_id, "checkpoint": f"consume:{purpose}"}})
    revoked = revocation_check(fs.replay_ref(revocation_ref), context=fixed.context, sources=fixed.sources, now=moment)
    if revoked & inherited_runs:
        raise ContractError(f"消费时承接项指向的来源运行已撤销：{sorted(revoked & inherited_runs)}")
    # 消费时批准仍有效：请求与两份批准的原文件仍在原处、没被改动（撤回即改动或删除批准文件），且此刻在有效期内。
    _replay_originals(_request_shape(fs.replay_ref(dict(decision["request"]))))
    approvals_check(fixed.approvals, fixed.binding_sha256, moment)
    body = {"schema_version": CONSUMPTION_SCHEMA, "run_id": run_id, "decision_sha256": decision["decision_sha256"],
            "purpose": purpose, "consumed_at_utc": fs.timestamp(moment), "consumer_clock": clock_ref, "revocation": revocation_ref,
            "verifier": result["verifier"]}
    sealed = R.seal(body, "consumption_sha256")
    stamp = time.strftime("%Y%m%dt%H%M%Sz", time.gmtime(moment))
    path = _write_once(inherit_root(root) / "consumptions" / run_id / f"{purpose}-{stamp}-{secrets.token_hex(4)}.json", sealed)
    result["consumption"] = fs.reference(path)
    return result


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def revoke(store_root: Path | str, run_id: str, *, reason: str, actor: str, now: float | None = None) -> Path:
    """登记撤销一次来源运行（只增；撤销不可撤回，已登记即报错）。"""

    if not _RUN_ID.fullmatch(run_id) or not reason.strip() or not actor.strip():
        raise ContractError("撤销须给合法运行 ID、原因与操作人")
    moment = current_time() if now is None else now
    body = {"schema_version": REVOCATION_SCHEMA, "run_id": run_id, "reason": reason, "actor": actor,
            "revoked_at_utc": fs.timestamp(moment)}
    return _write_once(revocation_path(Path(store_root).resolve(), run_id), records().seal(body, "revocation_sha256"))


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    clock = sub.add_parser("sample-clock", help="取一份校时样本写到新文件")
    clock.add_argument("--source", default=DEFAULT_CLOCK_SOURCE, help="chronyc（缺省）或 file:<绝对路径>")
    clock.add_argument("--output", type=Path, required=True)
    query = sub.add_parser("query-revocation", help="对提案或请求的来源运行做一次撤销查询")
    query.add_argument("--store", type=Path, required=True)
    query.add_argument("--sources", type=Path, required=True, help="提案的 sources.json 或请求文件")
    query.add_argument("--stage", required=True)
    query.add_argument("--campaign-status-source", default=None)
    query.add_argument("--output", type=Path, required=True)
    revoked = sub.add_parser("revoke", help="登记撤销一次来源运行")
    revoked.add_argument("--store", type=Path, required=True)
    revoked.add_argument("--run-id", required=True)
    revoked.add_argument("--reason", required=True)
    revoked.add_argument("--actor", required=True)
    assemble = sub.add_parser("assemble-request", help="把提案、凭证与两份批准组装成逐单元承接请求")
    assemble.add_argument("--proposal", type=Path, required=True)
    assemble.add_argument("--clock", type=Path, required=True)
    assemble.add_argument("--revocation", type=Path, required=True)
    assemble.add_argument("--approval", type=Path, action="append", required=True)
    assemble.add_argument("--reuse-enabled", action="store_true")
    assemble.add_argument("--max-age-hours", type=float, default=None)
    assemble.add_argument("--clock-source", default=None)
    assemble.add_argument("--campaign-status-source", default=None)
    assemble.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify", help="消费端重验一次逐单元承接运行（缺省按当前时刻消费并留证）")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--store", type=Path, required=True)
    verify.add_argument("--purpose", default="manual")
    verify.add_argument("--historical", action="store_true", help="只按形成期证明重放，不刷新凭证、不写消费登记")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "sample-clock":
            _write_once(args.output, sample_clock(args.source, current_time()))
            result: Any = fs.reference(args.output)
        elif args.command == "query-revocation":
            payload = fs.read(args.sources)
            sources = sources_check(payload.get("sources") if payload.get("schema_version") == REQUEST_SCHEMA else payload)
            _write_once(args.output, query_revocation(args.store, sorted(sources["runs"]), args.stage, current_time(),
                                                      args.campaign_status_source))
            result = fs.reference(args.output)
        elif args.command == "revoke":
            result = {"revocation": str(revoke(args.store, args.run_id, reason=args.reason, actor=args.actor))}
        elif args.command == "assemble-request":
            assemble_request(args.proposal, clock=args.clock, revocation=args.revocation, approvals=args.approval,
                             reuse_enabled=args.reuse_enabled, output=args.output, max_age_hours=args.max_age_hours,
                             clock_source=args.clock_source, campaign_status_source=args.campaign_status_source)
            result = fs.reference(args.output)
        else:
            result = verify_published(fs.read(args.manifest), args.store, purpose=args.purpose, historical=args.historical)
        print(json.dumps({"status": "ok", "result": result}, ensure_ascii=False, sort_keys=True))
        return 0
    except ERRORS as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, ensure_ascii=False))
        return 3


if __name__ == "__main__":
    sys.exit(main())
