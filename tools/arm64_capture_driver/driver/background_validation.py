#!/usr/bin/env python3
"""修复轮的后台验证（E4-01，指南「修好接着跑」）：修复提交部署之后，由同一个统一调度执行器在后台跑全量门禁，结论按
修复提交与当时的部署收据落盘；批次驱动在每个批次边界读这份结论。

为什么：修复轮原先以全量门禁通过为部署前提（串行约 95 分钟），而工具本来只认修复提交、定向回归与部署收据。现在改为
定向回归（入口门禁 ``regression`` 组合）通过即部署接着跑，全量门禁挪到后台：入口门禁 ``full-gates`` 组合、重新执行
全集模式（B-09 来源必须是同一次真实完整执行）、``nice`` 降优先级、``--require-deployed``（门禁前核对数据根
部署的就是这个提交）。采集批次开始前向调度器申请整机（``unit_executor.py acquire``）：调度器停派、等在跑单元结束才
批准，批次结束归还后接着跑，不和采集抢核。

结论文件 ``<RUNROOT>/background-validation/<提交前 12 位>-<部署收据摘要前 12 位>.json``（``background-validation/v1``）：
提交、分支、bundle、绑定的部署收据（文件名、sha256、工具身份）、组合、状态、后台进程号、起止时间、入口门禁输出目录与
日志、没通过的门禁项、原因。状态：

* ``running``：在跑（后台进程活着才算；进程已经不在而没写结论的，读出来是 ``vanished``）；
* ``passed``／``failed``：入口门禁的结论（门禁项全部通过／有门禁项没通过）；
* ``aborted``：没有门禁结论（被信号终止、准备失败、部署与提交不一致、部署在验证途中换了）；
* ``superseded``：被更新的修复轮停下（上一轮的提交已作废）。注意：被停下的运行没有发布运行清单，它执行过的单元记录
  不能被承接（E3-01 承接要追到原运行清单），下一次验证会重新执行它们。

同一提交＋同一部署只有一份当前状态；重开前保存旧终态。A-04 起工作树与全部 Go 缓存位于
本轮 background-validation/work 的独立目录，输入合同变化不能继续复用旧结论，启动以文件锁去重。显式 ``--work`` 也可放在
数据根外的 ``entry-gates-source-work/<完整提交>-<部署绑定摘要>/``，与后台状态和输出分开；来源绑定前须一次准备好目录与缓存。

B-09 可提供当前上下文与结束时校时凭证，覆盖完整才签发全集收据；到期自动重跑，撤销后可用 ``--rerun-full-set``。

已有后台失败后，显式 ``start --unit-inherit-request <当前请求>`` 可在同一 WORK 续作：保留旧终态，只通过现有逐单元合同
补跑不能承接的项。请求必须绑定本客户端的 full-gates／pre-ledger、当前提交、部署集合及记录库；与 B-09 签发参数互斥。
新后台通过前实际消费已发布的决策收据；批次边界按该次消费凭证重放，不能只信 passed 标签。默认路径仍重新执行全集。

两客户端都已纳管时（B5a-10；数据根 ``control`` 里两客户端都有部署收据）：仓库全量门禁两客户端只跑一次（测试集合已含 Claude
测试），结论按「提交＋部署收据集合」绑定——集合是每个已纳管客户端最新的那一份（交换共享根的一次部署即该事务签出的每一份，
B5a-01 的双收据），写进结论的 ``deployments``，文件名的部署摘要换成集合摘要；入口门禁带 ``--require-deployed-all``，本轮
客户端之外每个已纳管客户端的部署也核对是本提交，门禁结束时集合必须没变，否则判中止。两客户端的轮次各有自己的 RUNROOT，
所以另在数据根 ``control/background-validation/`` 登记「提交＋集合 → 结论文件」：另一客户端（另一 RUNROOT）的 start 查到
同一提交与集合已有在跑或已有结论就不再起，只把自己的入口空跑登记上（``.peer-<客户端>.json``），门禁通过后两客户端的入口
空跑都接着起（已经通过的当场接上）；起新的一次时，登记里其它提交或集合的在跑验证一并停下（部署已经换了，它们不再有用）。
搭上的轮次在自己的运行根 ``background-validation/attached/`` 留一个指向那份结论的指针（``background-validation-attached/v1``），
``stop`` 与 ``status`` 也覆盖它：哪个客户端的修复轮跑定向回归前 ``stop``，都停得下本轮正在用的那一次验证。
``check-boundary``／``require-passed`` 按 ``--client`` 取该客户端当前部署收据所绑定的结论：先找本 RUNROOT，再找数据根的登记。
只有一个客户端纳管时结论、文件名与命令行与改造前逐字相同（不写 ``deployments``，不登记）。

子命令：

* ``start --runroot --data-root --bundle --branch --commit [--profile] [--record-store] [--work] [--vc-env] [--then-dryrun]``：
  绑定数据根最新部署收据；同一提交＋同一部署已有在跑或已有 passed／failed 结论就不重复起，打印现有结论；在跑的其它后台
  验证先停下、标 superseded；然后在新会话里起 ``run``。``--then-dryrun``（E4-02 日常化）：通过之后接着起入口空跑
  （``entry_dryrun.py start --to pre-a3``，同一份记录库；门禁结论仍须满足 B-09 完整合同，缺失时重新执行；
  有采集在跑就让路），结果记在本结论的 ``dryrun`` 字段。
  本次带 ``--audit-policy`` 时记为 skipped：策略来源需要当前逐单元批准，操作员应显式调用
  ``entry-dryrun.sh start ... --unit-inherit-request <请求> --audit-policy <策略>``，不自动补批准或重新执行全集。
* ``run``（内部）：跑入口门禁，按结论写状态。收到 SIGTERM 时把它转给入口门禁（执行器会终止全部单元会话）。
* ``stop --runroot [--reason]``：停下在跑的后台验证并标 superseded。修复轮跑定向回归之前用：同一台机器同一时间只能有
  一个调度器，上一轮的验证也已作废。两客户端都纳管时连本运行根搭上的（别的运行根发起的）那一次一起停。
* ``check-boundary --runroot --data-root [--client]``：批次边界——取该客户端（缺省 codex）最新部署收据绑定的结论：failed／
  aborted 退出 3（拒绝派发下一批，打印没通过的门禁与日志位置）；passed、running 退出 0；没有结论（没起、被停下或进程已经
  不在）退出 0 并提示先 start。
* ``require-passed --runroot --data-root [--client]``：VC-5 验收前——该客户端最新部署绑定的结论是 passed 才退出 0，否则退出 3。
* ``status --runroot``：打印全部结论（JSON）。

退出码：0 通过或放行；2 用法或环境错误；3 拒绝（有失败结论、要求通过而没有通过结论）。
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "background-validation/v1"
DIRECTORY = "background-validation"
DEFAULT_PROFILE = "full-gates"
HERE = Path(__file__).resolve().parent


def _client_table() -> Any:
    """同目录的客户端表 client_table.py（B5a-06）：按路径加载，与驱动其它模块共用一个模块对象（client_table_sibling）。"""

    name = "client_table_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "client_table.py")
        if spec is None or spec.loader is None:
            raise RuntimeError(f"找不到同目录的 client_table.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


_CT = _client_table()
# 与驱动 install.py 的 latest_deploy_receipt 同一规则（按文件名里的时间戳取最新）。
DEPLOY_RECEIPT_GLOB = _CT.spec("codex")["deploy_receipt_glob"]
# B5a-01：按客户端（--client，缺省 codex）选部署收据，规则与驱动 install.py 相同；Claude 收据的工具身份在 identity
# 对象里。Codex 的结论文件逐字不变（不写 client 字段），Claude 的结论写 client，run 与边界判定据此取同一客户端的收据。
# B5a-06 起取值来自客户端表，这几个名字保留作只读视图。
DEPLOY_RECEIPT_GLOBS = {client: _CT.spec(client)["deploy_receipt_glob"] for client in _CT.CLIENTS}
DEPLOY_RECEIPT_STAMP_SEPARATORS = {client: _CT.spec(client)["deploy_receipt_stamp_separator"] for client in _CT.CLIENTS}
DEPLOY_RECEIPT_IDENTITY_KEYS = {client: _CT.spec(client)["deploy_receipt_identity_key"] for client in _CT.CLIENTS}
DEPLOY_CLIENTS = tuple(_CT.CLIENTS)
IDENTITY_FIELDS = ("policy_version", "policy_sha256", "tool_files_sha256", "wire_producer_sha256", "evidence_semantics_sha256",
                   "control_sha256")
NICENESS = 10
# 停下后台验证时给入口门禁与执行器的清理时间（执行器收到 SIGTERM 终止全部单元会话），超时再 SIGKILL 整组。
STOP_GRACE_SECONDS = 60.0
# 同一后台工作目录的既有进程尚在收尾时，入口锁最多等这么久。
WAIT_LOCK_SECONDS = 7200
# 两客户端都纳管时（B5a-10）：数据根 control 下的跨轮次登记目录与登记文件合同。
REGISTRY_DIRECTORY = "background-validation"
REGISTRY_SCHEMA = "background-validation-registry/v1"
PEER_SCHEMA = "background-validation-peer/v1"
# 另一客户端搭同一次验证时只比这几项输入合同：参数文件、运行目录与是否接空跑本来就随客户端与轮次不同。
CLIENT_SPECIFIC_INPUTS = frozenset({"vc_env_sha256", "work", "then_dryrun"})
# 搭上的轮次在自己运行根留的指针（子目录，不混进本运行根的结论列表）。
ATTACHED_DIRECTORY = "attached"
ATTACHED_SCHEMA = "background-validation-attached/v1"


class ValidationError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _read(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema_version") == SCHEMA else None


def _pid_alive(pid: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _group_alive(pgid: Any) -> bool:
    """进程组里还有进程（含组长已退出、组员还在清理的情形）。"""

    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 0:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def latest_deployment(data_root: Path, client: str = "codex") -> dict[str, Any]:
    """数据根该客户端最新的受管工具部署收据：文件名、sha256、工具身份。"""

    if client not in DEPLOY_RECEIPT_GLOBS:
        raise ValidationError(f"不认识的客户端：{client!r}（可选：{list(DEPLOY_CLIENTS)}）")
    control = Path(data_root) / "control"
    separator = DEPLOY_RECEIPT_STAMP_SEPARATORS[client]
    candidates = sorted((path for path in control.glob(DEPLOY_RECEIPT_GLOBS[client]) if path.is_file() and not path.is_symlink()),
                        key=lambda path: path.name.split(separator, 1)[1].lower())
    if not candidates:
        raise ValidationError(f"数据根没有受管工具部署收据：{control}")
    path = candidates[-1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValidationError(f"部署收据不是合法 JSON：{path}：{error}") from error
    if not isinstance(payload, dict) or payload.get("status") != "passed":
        raise ValidationError(f"最新部署收据不是 passed：{path}")
    key = DEPLOY_RECEIPT_IDENTITY_KEYS[client]
    identity = payload if key is None else payload.get(key)
    if not isinstance(identity, dict):
        raise ValidationError(f"部署收据缺少工具身份：{path}")
    return {"receipt": path.name, "sha256": _sha256_file(path), "identity": {field: identity.get(field) for field in IDENTITY_FIELDS}}


def managed_clients(data_root: Path) -> list[str]:
    """已纳管的客户端：数据根 control 里已有它的部署收据（与驱动 install.py、受管部署器同一判据），按客户端表顺序。"""

    control = Path(data_root) / "control"
    return [client for client in DEPLOY_CLIENTS
            if any(path.is_file() and not path.is_symlink() for path in control.glob(DEPLOY_RECEIPT_GLOBS[client]))]


def deployment_set(data_root: Path) -> dict[str, dict[str, Any]] | None:
    """两个及以上客户端已纳管时，每个已纳管客户端最新的部署收据（B5a-10，见模块说明）；只有一个客户端纳管时返回 None。"""

    clients = managed_clients(data_root)
    if len(clients) < 2:
        return None
    return {client: latest_deployment(data_root, client) for client in clients}


def set_sha256(deployments: Mapping[str, Mapping[str, Any]]) -> str:
    """部署收据集合的摘要：客户端 → 收据摘要，按客户端排序后的规范 JSON。"""

    canonical = json.dumps({client: str(item["sha256"]) for client, item in sorted(deployments.items())}, sort_keys=True,
                           separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def binding_key(deployment: Mapping[str, Any], deployments: Mapping[str, Mapping[str, Any]] | None = None) -> str:
    """结论文件名与后台工作目录用的部署摘要：只有一个客户端纳管时是那份部署收据的摘要（与改造前相同），否则是集合摘要。"""

    return set_sha256(deployments) if deployments else str(deployment["sha256"])


def result_path(runroot: Path, commit: str, deployment: Mapping[str, Any],
                deployments: Mapping[str, Mapping[str, Any]] | None = None) -> Path:
    return Path(runroot) / DIRECTORY / f"{commit[:12]}-{binding_key(deployment, deployments)[:12]}.json"


def registry_dir(data_root: Path) -> Path:
    return Path(data_root) / "control" / REGISTRY_DIRECTORY


def registry_path(data_root: Path, commit: str, deployments: Mapping[str, Mapping[str, Any]]) -> Path:
    return registry_dir(data_root) / f"{commit[:12]}-{set_sha256(deployments)[:12]}.json"


def _read_kind(path: Path, schema: str) -> dict[str, Any] | None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) and payload.get("schema_version") == schema else None


def registrations(data_root: Path) -> list[tuple[Path, dict[str, Any]]]:
    """数据根登记的全部「提交＋集合 → 结论文件」。"""

    found = []
    directory = registry_dir(data_root)
    if directory.is_dir() and not directory.is_symlink():
        for path in sorted(directory.glob("*.json")):
            if ".peer-" in path.name:
                continue
            payload = _read_kind(path, REGISTRY_SCHEMA)
            if payload is not None:
                found.append((path, payload))
    return found


def peers(registration: Path) -> list[dict[str, Any]]:
    """搭这次验证的其它客户端的入口空跑登记（``<登记名>.peer-<客户端>.json``）。"""

    found = []
    for path in sorted(Path(registration).parent.glob(f"{Path(registration).stem}.peer-*.json")):
        payload = _read_kind(path, PEER_SCHEMA)
        if payload is not None:
            found.append(payload)
    return found


def attached(runroot: Path) -> list[tuple[Path, dict[str, Any]]]:
    """本运行根搭上的、别的运行根发起的后台验证结论（指针在 ``attached/`` 下；只有两客户端都纳管时才会有）。"""

    found = []
    directory = Path(runroot) / DIRECTORY / ATTACHED_DIRECTORY
    if directory.is_dir() and not directory.is_symlink():
        for pointer in sorted(directory.glob("*.json")):
            entry = _read_kind(pointer, ATTACHED_SCHEMA)
            target = Path(str((entry or {}).get("result") or ""))
            payload = _read(target) if entry is not None and target.is_absolute() else None
            if payload is not None:
                found.append((target, payload))
    return found


def results(runroot: Path) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for path in sorted((Path(runroot) / DIRECTORY).glob("*.json")):
        payload = _read(path)
        if payload is not None:
            found.append((path, payload))
    return found


def effective_status(payload: Mapping[str, Any]) -> str:
    """running 只有后台进程还活着才算；进程已经不在而没写结论的是 ``vanished``。"""

    status = str(payload.get("status"))
    if status == "running" and not _pid_alive(payload.get("pid")):
        return "vanished"
    return status


def _binds(payload: Mapping[str, Any], deployment: Mapping[str, Any], client: str | None) -> bool:
    """结论是否绑定了这份部署收据：本身发起时绑定的那份，或两客户端都纳管时集合里该客户端的那份（B5a-10）。"""

    if (payload.get("deployment") or {}).get("sha256") == deployment["sha256"]:
        return True
    bound = (payload.get("deployments") or {}).get(client) if client else None
    return isinstance(bound, Mapping) and bound.get("sha256") == deployment["sha256"]


def for_deployment(runroot: Path, deployment: Mapping[str, Any], *, client: str | None = None,
                   data_root: Path | None = None) -> tuple[Path, dict[str, Any]] | None:
    """绑定这次部署的结论（同一部署验证过多个提交时取最后开始的一份）。给了 ``client`` 时另认集合里该客户端的那份；本 RUNROOT
    找不到而给了 ``data_root`` 时再按数据根的登记找（另一客户端的轮次发起的同一次验证，B5a-10）。"""

    bound = [(path, payload) for path, payload in results(runroot) if _binds(payload, deployment, client)]
    if not bound and data_root is not None and client is not None:
        for _registration, entry in registrations(data_root):
            target = Path(str(entry.get("result") or ""))
            payload = _read(target) if target.is_absolute() else None
            if payload is not None and _binds(payload, deployment, client):
                bound.append((target, payload))
    return max(bound, key=lambda item: (str(item[1].get("started_at_utc")), item[0].name)) if bound else None


# ---------------------------------------------------------------- 启动、停止与后台运行

def _terminate(payload: Mapping[str, Any]) -> None:
    """终止后台运行的整个进程组：先 SIGTERM（执行器收到后终止全部单元会话），等整组退出、超过宽限再 SIGKILL。

    要等整组而不是只等组长：组长（``run`` 进程）收到 SIGTERM 立即退出，原来据此马上对整组发 SIGKILL，执行器正在逐个
    终止单元会话就被杀掉，排在后面的单元（各自成会话、不在本组里）留在后台继续跑（E4-02 验收实测）。"""

    pid = payload.get("pid")
    if not _group_alive(pid):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while _group_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(int(pid), signal.SIGKILL)


def stop(runroot: Path, reason: str, *, keep: Path | None = None, with_dryruns: bool = True) -> list[str]:
    """停下在跑的后台验证（``keep`` 除外），标 superseded；``with_dryruns`` 时连后台验证接上的入口空跑一起停（E4-02：空跑与
    前台入口门禁共用调度器和入口门禁工作目录，不停会挡住修复轮的定向回归）。两客户端都纳管时，本运行根搭上的（别的运行根
    发起的）那一次也停（B5a-10）。"""

    stopped = []
    if with_dryruns and (HERE / "entry_dryrun.py").is_file():
        spec = importlib.util.spec_from_file_location("entry_dryrun_sibling", HERE / "entry_dryrun.py")
        if spec is not None and spec.loader is not None:
            module = sys.modules.get("entry_dryrun_sibling") or importlib.util.module_from_spec(spec)
            if "entry_dryrun_sibling" not in sys.modules:
                sys.modules["entry_dryrun_sibling"] = module
                spec.loader.exec_module(module)
            stopped += [f"entry-dryrun/{name}" for name in module.stop(runroot, reason)]
    for path, _payload in [*results(runroot), *attached(runroot)]:
        current = _read(path)   # 紧挨着写之前再读一次：刚写完结论的不再改
        if path == keep or current is None or current.get("status") != "running":
            continue
        # 先标 superseded 再终止：后台 ``run`` 收尾时看到状态已不是 running 就不再写（否则会写成 aborted）。
        _write(path, {**current, "status": "superseded", "completed_at_utc": _utc_now(), "reason": reason})
        _terminate(current)
        stopped.append(path.name)
    return stopped


def start(args: argparse.Namespace) -> dict[str, Any]:
    """同一运行根串行登记启动，避免两个入口同时读到空结论而重复派发。两客户端都纳管时另取数据根登记目录的锁（先运行根、
    后登记目录，顺序固定）：两个客户端的轮次各在自己的运行根里 start，也只起一次。"""
    root = Path(args.runroot) / DIRECTORY
    if any(path.is_symlink() for path in (root, *root.parents)):
        raise ValidationError("后台验证运行根不得含符号链接")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(root / ".start.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        deployments = deployment_set(args.data_root)
        if deployments is None:
            return _start_locked(args)
        registry = registry_dir(args.data_root)
        if any(path.is_symlink() for path in (registry, *registry.parents)):
            raise ValidationError("后台验证登记目录不得含符号链接")
        registry.mkdir(mode=0o700, parents=True, exist_ok=True)
        shared = os.open(registry / ".start.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(shared, fcntl.LOCK_EX)
            return _start_locked(args, deployments=deployments)
        finally:
            os.close(shared)
    finally:
        os.close(fd)


def _shared_contract(inputs: Any) -> dict[str, Any]:
    return {key: value for key, value in (inputs or {}).items() if key not in CLIENT_SPECIFIC_INPUTS}


def _register_peer(args: argparse.Namespace, registration: Path, client: str) -> dict[str, Any] | None:
    """另一客户端搭这次验证：登记它的入口空跑（参数文件与运行根），门禁通过后据此接着空跑；不接空跑时不登记。"""

    if not (args.vc_env and args.then_dryrun):
        return None
    peer = {"schema_version": PEER_SCHEMA, "client": client, "vc_env": str(args.vc_env), "runroot": str(args.runroot),
            "data_root": str(args.data_root), "registered_at_utc": _utc_now()}
    _write(registration.with_name(f"{registration.stem}.peer-{client}.json"), peer)
    return peer


def _attach(args: argparse.Namespace, registration: Path, target: Path, existing: Mapping[str, Any], client: str) -> dict[str, Any]:
    """同一提交与集合已有另一客户端发起的在跑验证或结论：不再起，只登记本客户端的入口空跑；已经通过的当场接上。"""

    peer = _register_peer(args, registration, client)
    own = Path(args.runroot) / DIRECTORY
    if target.parent != own:
        # 本运行根留指针：本轮的修复轮跑定向回归前 stop 要停得下它，status 也列得出它。
        _write(own / ATTACHED_DIRECTORY / f"{target.stem}.json",
               {"schema_version": ATTACHED_SCHEMA, "result": str(target), "registration": str(registration), "client": client,
                "attached_at_utc": _utc_now()})
    # 先登记、再重读状态：后台 run 写下 passed 之后才读登记，这里登记之后才判状态，两边总有一边接上空跑（同一份空跑
    # 已有就不重复起，两边都接也无妨）。
    current = _read(target) or existing
    status = effective_status(current)
    outcome: dict[str, Any] = {"action": "exists", "result": str(target), "status": status, "attached": client}
    if peer is not None and status == "passed":
        outcome["dryrun"] = _chain_dryrun({**current, "vc_env": peer["vc_env"], "runroot": peer["runroot"],
                                           "data_root": peer["data_root"]})
    return outcome


def stop_registered(data_root: Path, reason: str, *, keep: Path) -> list[str]:
    """两客户端都纳管时：数据根登记里其它提交或集合的在跑验证（在别的运行根）一并停下，标 superseded。"""

    stopped = []
    for _registration, entry in registrations(data_root):
        target = Path(str(entry.get("result") or ""))
        if not target.is_absolute() or target == keep:
            continue
        current = _read(target)
        if current is None or current.get("status") != "running":
            continue
        _write(target, {**current, "status": "superseded", "completed_at_utc": _utc_now(), "reason": reason})
        _terminate(current)
        stopped.append(str(target))
    return stopped


def _bound_work_roots(runroot: Path, data_root: Path, commit: str, deployment: Mapping[str, Any],
                      deployments: Mapping[str, Mapping[str, Any]] | None = None) -> tuple[Path, Path]:
    """旧默认坐标保留；显式来源工作区按同一提交与部署绑定，避免后台发布变动它的精确目录输入。"""

    binding = f"{commit}-{binding_key(deployment, deployments)}"
    return (Path(runroot) / DIRECTORY / "work" / binding,
            Path(data_root).parent / "entry-gates-source-work" / binding)


def _unit_inherit() -> Any:
    """加载原逐单元合同，后台不另写批准、来源或决策验证规则。"""
    name = "background_unit_inherit_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "unit_inherit.py")
        if spec is None or spec.loader is None:
            raise ValidationError("后台逐单元承接验证器缺失")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def _inherit_reference(request: Path, scope: Mapping[str, Any], *, check_approval: bool = True) -> dict[str, str]:
    """启动与派发前只核固定授权对象；实时读集与三次复核仍由原入口执行器完成。"""
    module = _unit_inherit()
    if not request.is_absolute() or any(path.is_symlink() for path in (request, *request.parents)):
        raise ValidationError("后台逐单元请求须为无符号链接的绝对路径")
    reference, payload = module._read_request(request)
    context = module.context_check(module.fs.replay_ref(payload["context"]))
    sources = module.sources_check(payload["sources"])
    expected = {"client": scope.get("client") or "codex", "stage": "pre-ledger", "commit": scope["commit"],
                "profile": DEFAULT_PROFILE, "record_store": str(Path(str(scope["record_store"])).resolve()),
                "deployment_receipts": sorted(module.deployment_set(Path(str(scope["data_root"])) / "control"),
                                              key=lambda item: item["client"])}
    if not payload["reuse_enabled"] or any(context.get(key) != value for key, value in expected.items()):
        raise ValidationError("后台逐单元请求未开启承接，或客户端、提交、部署集合、阶段、组合、记录库错绑")
    if "input_contract" in scope:
        contract = scope["input_contract"]
        if (_sha256_file(Path(str(scope["audit_policy"]))) != contract["full_set_files"]["audit_policy"]
                or _sha256_file(Path(str(scope["entry_gates"]))) != contract["entry_gates_sha256"]):
            raise ValidationError("后台逐单元策略或门禁入口在登记后变化")
    approvals = [module.fs.replay_ref(ref) for ref in payload["approvals"]]
    if check_approval:
        module.approvals_check(approvals, module.binding_sha256(context, sources), module.current_time())
    return reference


def _inherit_reexecuted(module: Any, manifest: Mapping[str, Any], store: Any, decision: Mapping[str, Any], fixed: Any,
                        consumption: Mapping[str, Any] | None) -> dict[str, Any]:
    """只有已通过的复核点把承接集合缩为空，才接受原执行器的全执行回退；授权失败回退仍拒绝。"""
    records = module.records()
    checkpoints = decision.get("checkpoints") or []
    if (decision.get("schema_version") != records.UNIT_INHERIT_DECISION_SCHEMA or decision.get("contract") != module.CONTRACT
            or not records.seal_ok(decision, "decision_sha256") or decision.get("action") != "reexecute-all"
            or decision.get("run_id") != manifest["run_id"] or decision.get("record_store") != str(store.root)
            or tuple(item.get("checkpoint") for item in checkpoints) not in (("queue",), ("queue", "lock"))
            or checkpoints[-1].get("inherited") != {} or not fixed.reuse_enabled
            or set(decision.get("units") or {}) != {row["unit_id"] for row in manifest["units"]}
            or any(row.get("disposition") != "executed" for row in manifest["units"])
            or any(row.get("disposition") != "executed" or row.get("source") is not None for row in decision["units"].values())
            or store.manifest(manifest["run_id"]) != manifest or records.verify_manifest(manifest, store=store, historical=True)
            or manifest.get("environment_sha256") != fixed.context["environment_sha256"]
            or manifest.get("policy_sha256") != fixed.context["policy_sha256"]
            or (manifest.get("executor") or {}).get("sha256") != fixed.context["executor_sha256"]):
        raise ValidationError("后台全执行回退缺少完整原件，或不是已授权的空承接集合")
    for checkpoint in checkpoints:
        module._checkpoint_proof(checkpoint, fixed, set())
    if consumption is None:
        moment = module.current_time()
        clock = module.put_evidence(store.root, module.sample_clock(fixed.clock_source, moment))
        revoked = module.put_evidence(store.root, module.query_revocation(store.root, sorted(fixed.sources["runs"]),
                                      fixed.context["stage"], moment, fixed.campaign_status_source))
        body = records.seal({"schema_version": module.CONSUMPTION_SCHEMA, "run_id": manifest["run_id"],
                             "decision_sha256": decision["decision_sha256"], "purpose": "background-validation",
                             "consumed_at_utc": module.fs.timestamp(moment), "consumer_clock": clock, "revocation": revoked,
                             "verifier": module.verifier_files()}, "consumption_sha256")
        module._consumption_proof(body, decision, fixed, set())
        consumption = module.put_evidence(store.root, body)
    else:
        module._consumption_proof(module.fs.replay_ref(dict(consumption)), decision, fixed, set())
    return {"run_id": manifest["run_id"], "decision_sha256": decision["decision_sha256"], "inherited": 0,
            "executed": len(manifest["units"]), "consumption": dict(consumption), "verifier": module.verifier_files()}


def _inherit_completion(payload: Mapping[str, Any], *, historical: bool = False) -> dict[str, Any]:
    """新结论实际消费原决策；边界像认证核验一样重放形成期及签发时消费证明，不重签或改写旧结论。"""
    module = _unit_inherit()
    request = payload["unit_inherit_request"]
    if (_inherit_reference(Path(request["path"]), payload, check_approval=not historical) != request
            or payload["input_contract"].get("unit_inherit_request") != request):
        raise ValidationError("后台逐单元请求在登记后变化")
    out = Path(str(payload["out"]))
    summary_path = out / "entry-gates.json"
    manifest_path = out / "executor" / "unit-manifest.json"
    plan_path = out / "gates-manifest.json"
    summary = module.fs.read(summary_path)
    manifest = module.fs.read(manifest_path)
    plan = module.fs.read(plan_path)
    reference = summary.get("unit_manifest") or {}
    if (summary.get("status") != "passed" or summary.get("mode") != manifest.get("mode")
            or manifest.get("mode") not in (module.MODE, "re-execute")
            or summary.get("profile") != DEFAULT_PROFILE or plan.get("profile") != DEFAULT_PROFILE
            or summary.get("record_store") != payload["record_store"]
            or summary.get("test_tree") != str(Path(payload["work"]) / "test-tree")
            or (summary.get("source") or {}).get("commit") != payload["commit"]
            or reference.get("path") != str(manifest_path) or reference.get("self_check") != "passed"
            or reference.get("manifest_sha256") != manifest.get("manifest_sha256")
            or manifest.get("out_dir") != str(out / "executor")
            or not manifest.get("units") or any(row.get("passed") is not True for row in manifest["units"])):
        raise ValidationError("后台通过结论未绑定本次完整逐单元清单")
    store = module.records().RecordStore(Path(str(payload["record_store"])))
    decision_path = (store.decision_path(manifest["run_id"]) if manifest["mode"] == module.MODE
                     else out / "executor" / "unit-inherit-decision.json")
    decision = module.fs.read(decision_path)
    fixed = module.replay_fixed(decision)
    if (fixed.request_ref != request or fixed.context["profile"] != DEFAULT_PROFILE
            or fixed.context["gates"] != sorted(row["gate_id"] for row in plan["gates"])
            or fixed.context["gates"] != sorted(row["gate_id"] for row in summary["gates"])
            or any(row.get("status") != "passed" for row in summary["gates"])):
        raise ValidationError("后台门禁全集、当前请求与决策收据不一致")
    references = {"summary": module.fs.reference(summary_path), "manifest": module.fs.reference(manifest_path),
                  "plan": module.fs.reference(plan_path), "decision": module.fs.reference(decision_path)}
    previous = payload.get("unit_inherit_consumption") if historical else None
    if historical and (not isinstance(previous, dict) or previous.get("references") != references):
        raise ValidationError("后台逐单元消费凭证缺失，或签发后的原件发生变化")
    consumption = previous["proof"]["consumption"] if historical else None
    if historical and (not isinstance(consumption, dict) or set(consumption) != {"path", "sha256"}):
        raise ValidationError("后台签发时的实际消费凭证缺失")
    proof = (module.verify_published(manifest, store.root, purpose="background-validation", historical=historical,
                                     consumption=consumption) if manifest["mode"] == module.MODE
             else _inherit_reexecuted(module, manifest, store, decision, fixed, consumption))
    if historical and any(proof[key] != previous["proof"].get(key) for key in ("run_id", "decision_sha256", "inherited", "executed")):
        raise ValidationError("后台逐单元消费凭证与实际决策不一致")
    return {"references": references, "proof": proof}


def _start_locked(args: argparse.Namespace, *, deployments: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    client = getattr(args, "client", None) or "codex"
    parallel = getattr(args, "parallel", None)
    if parallel is not None and (type(parallel) is not int or parallel <= 0):
        raise ValidationError("--parallel 必须是正整数")
    deployment = latest_deployment(args.data_root, client)
    if deployments is not None and deployments.get(client) != deployment:
        raise ValidationError(f"部署收据集合里没有 {client} 当前的部署收据")
    path = result_path(args.runroot, args.commit, deployment, deployments)
    # 后台编译缓存不再依赖操作者的 GOCACHE，也不与前台准入清理共用工作目录。
    bound_roots = _bound_work_roots(args.runroot, args.data_root, args.commit, deployment, deployments)
    work = args.work or bound_roots[0]
    work = Path(work)
    if (not work.is_absolute() or ".." in work.parts or not any(work.is_relative_to(root) for root in bound_roots)
            or any(item.is_symlink() for item in (work, *work.parents))):
        raise ValidationError("后台工作目录必须绑定本轮完整提交和部署摘要，不得共用其它轮次目录")
    if Path(args.bundle).is_symlink() or not Path(args.bundle).is_file():
        raise ValidationError("后台验证 bundle 必须是普通文件")
    inputs = {"execution_contract": "b09-reexecute-full/v1", "bundle_sha256": _sha256_file(args.bundle), "branch": args.branch, "profile": args.profile,
              "work": str(work), "entry_gates_sha256": _sha256_file(args.entry_gates),
              "record_store": str(args.record_store) if args.record_store else None,
              "vc_env_sha256": _sha256_file(args.vc_env) if args.vc_env else None, "then_dryrun": bool(args.then_dryrun)}
    # 只有显式指定才登记；缺省仍由既有执行器配置决定，不改变旧输入合同的字段形状。
    if parallel is not None:
        inputs["parallel"] = parallel
    full_set_paths = {key: str(getattr(args, key)) if getattr(args, key, None) else None
                      for key in ("full_set_context", "full_set_clock", "full_set_predecessor", "audit_policy")}
    if bool(full_set_paths["full_set_context"]) != bool(full_set_paths["full_set_clock"]):
        raise ValidationError("B-09 签发须同时提供当前上下文和校时凭证")
    inputs["full_set_files"] = {key: _sha256_file(Path(value)) if value else None for key, value in full_set_paths.items()}
    inherit_request = getattr(args, "unit_inherit_request", None)
    inherit_reference = None
    if inherit_request is not None:
        if (args.profile != DEFAULT_PROFILE or not args.record_store or not args.work or not args.audit_policy
                or getattr(args, "rerun_full_set", False)
                or any(full_set_paths[key] for key in ("full_set_context", "full_set_clock", "full_set_predecessor"))):
            raise ValidationError("后台逐单元续作须显式提供原 WORK、记录库及策略，仅限 full-gates，不能与 B-09 参数同用")
        inherit_reference = _inherit_reference(Path(inherit_request), vars(args))
        inputs.update(execution_contract="unit-inherit/v1", unit_inherit_request=inherit_reference)
    existing = _read(path)
    registration = registry_path(args.data_root, args.commit, deployments) if deployments is not None else None
    if registration is not None and (existing is None or effective_status(existing) not in ("running", "passed", "failed")):
        # 本运行根没有这次验证的结论：看另一客户端的轮次（另一运行根）是不是已经起了同一提交与集合的那一次。
        entry = _read_kind(registration, REGISTRY_SCHEMA)
        target = Path(str((entry or {}).get("result") or ""))
        other = _read(target) if entry is not None and target.is_absolute() and target != path else None
        if other is not None and other.get("commit") == args.commit and other.get("deployments") == deployments:
            path, existing = target, other
    rerun = bool(getattr(args, "rerun_full_set", False))
    if inherit_reference is not None:
        if existing is None:
            raise ValidationError("后台逐单元续作缺少同提交、同部署的原后台结论")
        unchanged = ("bundle_sha256", "branch", "profile", "work", "entry_gates_sha256", "record_store", "parallel")
        if ((existing.get("client") or "codex") != client or existing.get("commit") != args.commit
                or existing.get("deployment") != deployment or existing.get("deployments") != (dict(deployments) if deployments else None)
                or any((existing.get("input_contract") or {}).get(key) != inputs.get(key) for key in unchanged)):
            raise ValidationError("后台逐单元续作必须保留原客户端、提交、部署、WORK、记录库、门禁及调度参数")
        if effective_status(existing) not in ("running", "passed"):
            if _group_alive(existing.get("pid")):
                raise ValidationError("原后台进程组尚未退出，拒绝在同 WORK 启动后继")
            rerun = True
    if inherit_reference is not None and existing is not None and existing.get("full_set_receipt"):
        raise ValidationError("已有 B-09 全集收据不能转换成后台逐单元结论")
    if existing is not None and existing.get("full_set_receipt", {}).get("status") == "issued":
        # 到期后必须新执行，不能被“同部署已有 passed”去重挡住；坏来源也先重跑。
        try:
            old_ref = existing["full_set_receipt"]["receipt"]
            old_receipt = json.loads(Path(old_ref["path"]).read_text())
            expires = datetime.fromisoformat(old_receipt["expires_at_utc"].replace("Z", "+00:00")).timestamp()
            rerun = rerun or _sha256_file(Path(old_ref["path"])) != old_ref["sha256"] or time.time() >= expires
            if rerun and not full_set_paths["full_set_predecessor"]:
                full_set_paths["full_set_predecessor"] = old_ref["path"]
                inputs["full_set_files"]["full_set_predecessor"] = _sha256_file(Path(old_ref["path"]))
        except (OSError, ValueError, TypeError, KeyError):
            rerun = True
    if existing is not None and effective_status(existing) == "running" and rerun:
        raise ValidationError("当前全集仍在运行，禁止另起重复执行")
    if (registration is not None and existing is not None and not rerun and (existing.get("client") or "codex") != client
            and effective_status(existing) in ("running", "passed", "failed")):
        # B5a-10：另一客户端发起的同一次验证（同一提交、同一组部署收据）。只比与客户端无关的输入合同，相同即搭上它。
        if _shared_contract(existing.get("input_contract")) != _shared_contract(inputs) or existing.get("commit") != args.commit:
            raise ValidationError(f"同一提交与部署已有另一客户端发起的后台验证（{path}），输入合同不同：须先显式停止并重新验证")
        return _attach(args, registration, path, existing, client)
    if existing is not None and not rerun and effective_status(existing) in ("running", "passed", "failed"):
        if existing.get("input_contract") != inputs or existing.get("commit") != args.commit or existing.get("deployment") != deployment:
            raise ValidationError("后台验证已有记录的输入或缓存隔离合同不同，须先显式停止并重新验证")
        return {"action": "exists", "result": str(path), "status": effective_status(existing)}
    if registration is not None and path.parent != Path(args.runroot) / DIRECTORY:
        # 登记里同一次验证在另一运行根、但没法搭（要重跑全集）：本运行根新起一次，登记改指向新的这一份。
        path = result_path(args.runroot, args.commit, deployment, deployments)
        existing = _read(path)
    superseded = stop(args.runroot, f"被提交 {args.commit[:12]} 在部署 {deployment['receipt']} 上的后台验证取代", keep=path)
    if registration is not None:
        superseded += stop_registered(args.data_root, f"被提交 {args.commit[:12]} 在部署集合 {registration.stem} 上的后台验证取代",
                                      keep=path)
    out = path.parent / "runs" / f"{path.stem}-{time.time_ns()}"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA, "commit": args.commit, "branch": args.branch, "bundle": str(args.bundle),
        "deployment": deployment, "profile": args.profile, "mode": "re-execute", "status": "running", "pid": None,
        "started_at_utc": _utc_now(), "completed_at_utc": None, "out": str(out), "log": f"{out}.log",
        "record_store": str(args.record_store) if args.record_store else None, "work": str(work),
        "input_contract": inputs, "cache_roots": {"GOCACHE": str(work / "go-build-cache"),
            "GOMODCACHE": str(work / "go-mod-cache"), "GOTMPDIR": str(work / "go-tmp")},
        "vc_env": str(args.vc_env) if args.vc_env else None, "entry_gates": str(args.entry_gates),
        "data_root": str(args.data_root), "runroot": str(args.runroot), "then_dryrun": bool(args.then_dryrun),
        "failed_gates": [], "reason": None, "returncode": None, "dryrun": None,
        **({"parallel": parallel} if parallel is not None else {}),
        **full_set_paths,
        # B5a-01：只有非 Codex 的结论记录客户端，Codex 结论的字段逐字不变（读取时缺省按 codex）。
        **({"client": client} if client != "codex" else {}),
        # B5a-10：两客户端都纳管时绑定部署收据集合（只有一个客户端纳管时不写，结论与改造前同形）。
        **({"deployments": dict(deployments)} if deployments is not None else {}),
    }
    if inherit_reference is not None:
        payload.update(mode="unit-inherit", unit_inherit_request=inherit_reference)
    if existing is not None:
        # 当前状态文件可更新；每次重新开始前保留上一执行的完整终态，便于复核失败恢复。
        saved = json.dumps(existing, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        history = path.parent / "history" / (hashlib.sha256(saved.encode()).hexdigest() + ".json")
        if not history.exists():
            _write(history, existing)
        if inherit_reference is not None:
            payload["predecessor"] = {"path": str(history), "sha256": _sha256_file(history), "status": existing["status"]}
    _write(path, payload)
    if registration is not None:
        _write(registration, {"schema_version": REGISTRY_SCHEMA, "commit": args.commit, "deployments": dict(deployments),
                              "result": str(path), "client": client, "runroot": str(args.runroot), "registered_at_utc": _utc_now()})
    argv = ["nice", "-n", str(NICENESS), sys.executable, "-B", str(Path(__file__).resolve()), "run", "--result", str(path)]
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **({"ARM64_VC_ENV": str(args.vc_env)} if args.vc_env else {})}
    log = Path(payload["log"])
    log.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with log.open("ab") as handle:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, env=environment,
                                   start_new_session=True)
    current = _read(path) or payload
    if current.get("status") == "running" and current.get("pid") is None:
        _write(path, {**current, "pid": process.pid})
    return {"action": "started", "result": str(path), "pid": process.pid, "out": str(out), "superseded": superseded}


def _entry_summary(out: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads((Path(out) / "entry-gates.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def run(result: Path) -> int:
    path = Path(result)
    payload = _read(path)
    if payload is None:
        raise ValidationError(f"后台验证结论文件不可读：{path}")
    # 工作树和全部 Go 缓存使用本次后台验证的独立坐标；承接判断仍交给输入合同，不强行沿用前台记录。两客户端都纳管时
    # （B5a-10）入口门禁核对每个已纳管客户端的部署（--require-deployed-all），只有一个客户端纳管时命令行与改造前相同。
    require = "--require-deployed-all" if payload.get("deployments") else "--require-deployed"
    mode = "unit-inherit" if payload.get("unit_inherit_request") else "re-execute"
    argv = ["bash", payload["entry_gates"], "--profile", payload["profile"], "--mode", mode, require,
            "--wait-lock", str(WAIT_LOCK_SECONDS), "--out", payload["out"]]
    for flag, key in (("--record-store", "record_store"), ("--work", "work")):
        if payload.get(key):
            argv += [flag, payload[key]]
    if "parallel" in payload:
        argv += ["--parallel", str(payload["parallel"])]
    if payload.get("audit_policy"):
        argv += ["--audit-reads", "--audit-strict", "--audit-policy", payload["audit_policy"]]
    argv += [payload["bundle"], payload["branch"], payload["commit"]]
    received: list[int] = []

    def forward(signum: int, _frame: Any) -> None:
        received.append(signum)

    signal.signal(signal.SIGTERM, forward)
    environment = dict(os.environ)
    work = Path(payload["work"])
    expected_caches = {"GOCACHE": str(work / "go-build-cache"), "GOMODCACHE": str(work / "go-mod-cache"), "GOTMPDIR": str(work / "go-tmp")}
    try:
        if (payload.get("mode") == "unit-inherit" or payload.get("unit_inherit_request")
                or payload.get("input_contract", {}).get("execution_contract") == "unit-inherit/v1"):
            if (payload.get("mode") != "unit-inherit" or payload["input_contract"].get("execution_contract") != "unit-inherit/v1"
                    or payload["input_contract"].get("unit_inherit_request") != payload.get("unit_inherit_request")
                    or _inherit_reference(Path(payload["unit_inherit_request"]["path"]), payload) != payload["unit_inherit_request"]):
                raise ValidationError("后台逐单元运行的模式、授权与输入合同不一致")
            argv[2:2] = ["--unit-inherit-request", payload["unit_inherit_request"]["path"], "--unit-inherit-stage", "pre-ledger"]
        # 运行期再核对已登记的资源值；修改结果或输入合同不能悄悄改变门禁的调度策略。
        parallel = payload.get("parallel")
        contract = payload.get("input_contract", {})
        if "parallel" in payload or "parallel" in contract:
            if (type(parallel) is not int or parallel <= 0 or type(contract.get("parallel")) is not int
                    or contract["parallel"] != parallel):
                raise ValidationError("后台并发参数缺失或发生漂移")
        bound_roots = _bound_work_roots(Path(payload["runroot"]), Path(payload["data_root"]), payload["commit"],
                                       payload["deployment"], payload.get("deployments"))
        if (payload.get("cache_roots") != expected_caches or payload.get("input_contract", {}).get("work") != str(work)
                or not work.is_absolute() or not any(work.is_relative_to(root) for root in bound_roots) or ".." in work.parts):
            raise ValidationError("后台缓存合同缺失或发生漂移")
        for value in expected_caches.values():
            directory = Path(value)
            if any(item.is_symlink() for item in (directory, *directory.parents)):
                raise ValidationError("后台缓存目录不得含符号链接")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
        _write(path, {**payload, "status": "aborted", "completed_at_utc": _utc_now(), "reason": str(error), "returncode": 3})
        return 3
    environment.update(expected_caches)
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, env=environment)
    while True:
        try:
            returncode = process.wait()
            break
        except InterruptedError:   # pragma: no cover - PEP 475 之后 wait 自动重试，这里只是保险
            continue
    current = _read(path) or payload
    if current.get("status") != "running":
        return 0   # 已被 stop 标为 superseded
    summary = _entry_summary(Path(payload["out"]))
    bound = str(((payload.get("deployment") or {}).get("receipt")) or "")
    checked = Path(str(((summary or {}).get("source") or {}).get("deploy_receipt") or "")).name
    failed = [str(gate.get("gate_id")) for gate in (summary or {}).get("gates") or [] if gate.get("status") != "passed"]
    try:
        deployment_matches = latest_deployment(Path(payload["data_root"]), payload.get("client") or "codex") == payload["deployment"]
        if payload.get("deployments"):
            deployment_matches = deployment_matches and deployment_set(Path(payload["data_root"])) == payload["deployments"]
    except (ValidationError, OSError, ValueError):
        deployment_matches = False
    others_problem = _others_checked(payload, summary) if payload.get("deployments") and summary is not None else None
    if received:
        status, reason = "aborted", f"收到信号 {received[0]}"
    elif summary is None:
        status, reason = "aborted", f"入口门禁没有结论（退出码 {returncode}），日志 {payload.get('log')}"
    elif checked != bound or not deployment_matches:
        status, reason = "aborted", f"入口门禁核对的部署收据（{checked or '没有核对'}）不是开始时绑定的 {bound}：部署在验证途中换了"
    elif others_problem:
        status, reason = "aborted", others_problem
    elif returncode == 0 and not failed and summary.get("status") == "passed":
        status, reason = "passed", None
    else:
        status, reason = "failed", f"入口门禁退出码 {returncode}"
    if status == "passed" and current.get("unit_inherit_request"):
        try:
            current = {**current, "unit_inherit_consumption": _inherit_completion(current)}
        except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
            status, reason = "aborted", f"后台逐单元消费不通过：{error}"
    _write(path, {**current, "status": status, "completed_at_utc": _utc_now(), "reason": reason, "returncode": returncode,
                  "failed_gates": failed})
    if status == "passed" and current.get("full_set_context"):
        # 功能通过与可承接签发分开；覆盖不完整时保留通过结论，明确拒签，不生成假绿收据。
        issued = _issue_full_set(current)
        _write(path, {**(_read(path) or current), "full_set_receipt": issued})
    if status == "passed" and current.get("then_dryrun"):
        dryrun = _chain_dryrun(current)
        _write(path, {**(_read(path) or current), "dryrun": dryrun})
    if status == "passed" and current.get("deployments"):
        # B5a-10：搭这次验证的其它客户端的入口空跑一并接上（各用自己的参数文件与运行根；同一份空跑已有就不重复）。
        chained = {}
        for peer in peers(registry_path(Path(current["data_root"]), current["commit"], current["deployments"])):
            if peer.get("client") != (current.get("client") or "codex"):
                chained[str(peer.get("client"))] = _chain_dryrun({**current, "vc_env": peer.get("vc_env"), "runroot": peer.get("runroot"),
                                                                  "data_root": peer.get("data_root")})
        if chained:
            _write(path, {**(_read(path) or current), "peer_dryruns": chained})
    return 0


def _others_checked(payload: Mapping[str, Any], summary: Mapping[str, Any]) -> str | None:
    """两客户端都纳管时，入口门禁必须逐一核对过本轮客户端之外每个绑定的部署收据（总摘要来源 deploy_receipts_others）。"""

    own = payload.get("client") or "codex"
    expected = {client: str(item.get("receipt")) for client, item in (payload.get("deployments") or {}).items() if client != own}
    raw = str(((summary.get("source") or {}).get("deploy_receipts_others")) or "")
    checked = dict(item.split("=", 1) for item in raw.split() if "=" in item)
    if checked != expected:
        return f"入口门禁没有逐一核对其余客户端绑定的部署收据（核对了 {checked or '没有'}，绑定的是 {expected}）"
    return None


def _issue_full_set(payload: Mapping[str, Any]) -> dict[str, Any]:
    spec = importlib.util.spec_from_file_location("background_full_set", HERE / "full_set_receipt.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        for key in ("full_set_context", "full_set_predecessor", "audit_policy"):
            if payload.get(key) and _sha256_file(Path(payload[key])) != payload["input_contract"]["full_set_files"][key]:
                raise ValidationError("B-09 签发的静态上下文在后台运行期间变化")
        out = Path(payload["out"])
        summary = _entry_summary(out)
        source = {"entry": module.reference(out / "entry-gates.json"), "summary": module.reference(summary["executor_summary"]),
                  "manifest": module.reference(summary["unit_manifest"]["path"]), "store": summary["record_store"]}
        context = module.read(payload["full_set_context"])
        receipt = module.issue(source, context, module.reference(payload["full_set_clock"]),
            predecessor=module.reference(payload["full_set_predecessor"]) if payload.get("full_set_predecessor") else None)
        output = out / "full-set-receipt.json"
        with output.open("x", encoding="utf-8") as handle:
            os.chmod(output, 0o600)
            handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return {"status": "issued", "receipt": module.reference(output), "reuse_enabled": False}
    except (OSError, ValueError, TypeError, KeyError, AttributeError, ValidationError) as error:
        return {"status": "refused", "reason": str(error), "reuse_enabled": False}


# 通过之后接着空跑入口（E4-02）只到 pre-A3：便宜检查、策略认证、入口门禁与 pre-A3 场景都受统一调度（采集预约会让它停派）；
# 零请求 smoke、atomic-double 与建账本之后的步骤要用采集容器，只在明确的空跑（entry-dryrun.sh）与升级开工的空跑里跑。
CHAINED_DRYRUN_TO = "pre-a3"


def _chain_dryrun(payload: Mapping[str, Any]) -> dict[str, Any]:
    if payload.get("audit_policy"):
        # 后台的同次全集来源不是当前逐单元批准，不能暗中补造批准或丢掉策略再自动执行一遍全集。
        return {"action": "skipped", "reason": "后台已绑定读集策略；请显式 entry-dryrun.sh start ... "
                "--unit-inherit-request <当前批准请求> --audit-policy <原策略>，不自动接空跑"}
    if not payload.get("vc_env") or not payload.get("data_root") or not payload.get("runroot"):
        return {"action": "skipped", "reason": "缺少驱动参数文件、数据根或 RUNROOT，没有接着空跑"}
    argv = [sys.executable, "-B", str(HERE / "entry_dryrun.py"), "start", "--runroot", str(payload["runroot"]),
            "--data-root", str(payload["data_root"]), "--vc-env", str(payload["vc_env"]), "--bundle", str(payload["bundle"]),
            "--branch", str(payload["branch"]), "--commit", str(payload["commit"]), "--to", CHAINED_DRYRUN_TO]
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, timeout=600, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    except (OSError, subprocess.SubprocessError) as error:
        return {"action": "error", "reason": str(error)[:300]}
    lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
    with contextlib.suppress(ValueError, IndexError):
        return {**json.loads(lines[-1]), "returncode": completed.returncode}
    return {"action": "error", "returncode": completed.returncode, "reason": (completed.stderr or completed.stdout)[-300:]}


# ---------------------------------------------------------------- 批次边界与 VC-5 前核对

def check(runroot: Path, data_root: Path, *, require_passed: bool, client: str = "codex") -> tuple[int, str]:
    """批次边界（``require_passed`` 为假）或 VC-5 验收前（为真）的判定：（退出码，说明）。"""

    deployment = latest_deployment(data_root, client)
    found = for_deployment(runroot, deployment, client=client, data_root=data_root)
    if found is None:
        message = f"当前部署（{deployment['receipt']}）还没有后台验证：先 background-validate.sh start <bundle> <分支> <提交>"
        return (3, message) if require_passed else (0, "提示：" + message)
    path, payload = found
    status = effective_status(payload)
    where = f"提交 {str(payload.get('commit'))[:12]}，结论 {path}，日志 {payload.get('log')}"
    if status == "passed":
        if (payload.get("mode") == "unit-inherit" or payload.get("unit_inherit_request")
                or payload.get("input_contract", {}).get("execution_contract") == "unit-inherit/v1"):
            try:
                if payload.get("mode") != "unit-inherit" or not payload.get("unit_inherit_request"):
                    raise ValidationError("后台逐单元结论缺少模式或请求")
                _inherit_completion(payload, historical=True)
            except (OSError, ValueError, RuntimeError, KeyError, TypeError) as error:
                return 3, f"后台逐单元消费证明不能重放（{where}）：{error}"
        return 0, f"后台验证通过（{where}）"
    if status == "failed":
        return 3, f"后台验证失败（没通过的门禁项 {payload.get('failed_gates')}；{where}）：拒绝派发下一批，修好、定向回归、部署后重新 start"
    if status == "aborted":
        return 3, f"后台验证中止、没有结论（{payload.get('reason')}；{where}）：拒绝派发下一批，排查后重新 start"
    if status == "running":
        if require_passed:
            return 3, f"后台验证还在跑（{where}）：VC-5 验收要等它通过"
        return 0, f"后台验证还在跑（{where}），允许继续"
    message = f"当前部署的后台验证{'被停下' if status == 'superseded' else '进程已经不在'}、没有结论（{where}）：重新 background-validate.sh start"
    return (3, message) if require_passed else (0, "提示：" + message)


def _positive_parallel(value: str) -> int:
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise argparse.ArgumentTypeError("--parallel 必须是正整数")
    return int(value)


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", help="绑定最新部署，后台起全量门禁的全集通过")
    p_start.add_argument("--runroot", type=Path, required=True)
    p_start.add_argument("--data-root", type=Path, required=True)
    p_start.add_argument("--bundle", type=Path, required=True)
    p_start.add_argument("--branch", required=True)
    p_start.add_argument("--commit", required=True)
    p_start.add_argument("--profile", default=DEFAULT_PROFILE)
    p_start.add_argument("--record-store", type=Path, default=None)
    p_start.add_argument("--work", type=Path, default=None, help="只允许本轮完整提交及部署摘要目录或其子目录；缺省自动派生")
    p_start.add_argument("--parallel", type=_positive_parallel, default=None, help="门禁单元并发数；缺省沿用执行器配置")
    p_start.add_argument("--vc-env", type=Path, default=None, help="驱动参数文件（ARM64_VC_ENV），后台运行的入口门禁要用")
    p_start.add_argument("--entry-gates", type=Path, default=HERE / "entry-gates.sh")
    p_start.add_argument("--then-dryrun", action="store_true", help="通过之后接着空跑入口到 pre-A3（E4-02）")
    p_start.add_argument("--full-set-context", type=Path, help="B-09 全集绑定及外部证据上下文")
    p_start.add_argument("--full-set-clock", type=Path, help="全集结束时仍有效的校时凭证；不自动补造")
    p_start.add_argument("--full-set-predecessor", type=Path, help="失效的旧收据，只读关联")
    p_start.add_argument("--audit-policy", type=Path, help="B-11 已登记的严格读集策略")
    p_start.add_argument("--rerun-full-set", action="store_true", help="撤销或错绑后显式重跑全集，保留旧终态并关联旧收据")
    p_start.add_argument("--unit-inherit-request", type=Path, help="原后台结束后在同 WORK 按当前批准请求续作，保留旧失败并重验决策")
    p_start.add_argument("--client", choices=DEPLOY_CLIENTS, default="codex", help="绑定哪个客户端的部署收据（缺省 codex）")
    p_run = sub.add_parser("run", help="（内部）跑入口门禁并写结论")
    p_run.add_argument("--result", type=Path, required=True)
    p_stop = sub.add_parser("stop", help="停下在跑的后台验证，标 superseded")
    p_stop.add_argument("--runroot", type=Path, required=True)
    p_stop.add_argument("--reason", default="修复轮跑定向回归前停下（上一轮提交的验证已作废）")
    for name in ("check-boundary", "require-passed"):
        p = sub.add_parser(name)
        p.add_argument("--runroot", type=Path, required=True)
        p.add_argument("--data-root", type=Path, required=True)
        p.add_argument("--client", choices=DEPLOY_CLIENTS, default="codex", help="取哪个客户端的部署收据（缺省 codex）")
    p_status = sub.add_parser("status")
    p_status.add_argument("--runroot", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "start":
            if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
                raise ValidationError("--commit 要完整的 40 位小写提交号")
            for name in ("runroot", "data_root", "bundle"):
                if not getattr(args, name).is_absolute():
                    raise ValidationError(f"--{name.replace('_', '-')} 必须是绝对路径")
            print(json.dumps(start(args), ensure_ascii=False))
            return 0
        if args.command == "run":
            return run(args.result)
        if args.command == "stop":
            print(json.dumps({"stopped": stop(args.runroot, args.reason)}, ensure_ascii=False))
            return 0
        if args.command in ("check-boundary", "require-passed"):
            code, message = check(args.runroot, args.data_root, require_passed=args.command == "require-passed",
                                  client=args.client)
            print(message, flush=True)
            return code
        listed = [{**payload, "effective_status": effective_status(payload), "path": str(path)} for path, payload in results(args.runroot)]
        # 两客户端都纳管时另列本运行根搭上的那一次（标 attached；只有一个客户端纳管时没有，输出与改造前相同）。
        listed += [{**payload, "effective_status": effective_status(payload), "path": str(path), "attached": True}
                   for path, payload in attached(args.runroot)]
        print(json.dumps(listed, ensure_ascii=False, indent=2))
        return 0
    except ValidationError as error:
        print(f"后台验证：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
