#!/usr/bin/env python3
"""中断运行的封存清单（Codex 升级工具优化第 16 项：中途作废的运行不再白跑）。

问题：承接要求记录出现在“来源运行已发布、自摘要通过的清单”里并被列为正式执行（``unit_records.check_record``），而执行器只在正常收尾时发布清单——
被 kill -9、被后继停下、进程消失的运行没有清单，它已经完成的单元记录再合法也承接不了（0.163 后台验证起了 16 次只有 7 次跑满）。
只放开 aborted／superseded／vanished 的来源状态没用，要有清单。

做法：由**后继**（不能指望原进程退出时处理：强制终止和进程消失都没有机会）生成一份可验证的**部分完成清单**，写进记录库的 ``runs/<run_id>.json``：

* 沿用 ``unit-execution-manifest/v1``，加 ``partial: true``、``sealed_by: "successor"``、``origin_state``（原运行的终态）、``sealed_at_utc``、``origin_run_dir``；
  ``planned_units`` 保持全集，``units`` 只列“记录已入库、自摘要合法、日志摘要匹配、``run.run_id`` 等于该运行、有完成时间”的正式执行；
  未完成、记录不完整的单元不列（它们没有可承接的来源，自然重跑）；
* **进程必须已死才封存**：后继先取调度器的状态目录锁（``scheduler.lock`` 的 flock，被占用说明有调度器在跑，原进程可能仍在写，拒绝）；
  再按事件流核对每个有开始、没结束的单元进程组都已退出，确认不了就不封存；
* **封存冲突**：后继用 ``O_EXCL`` 写 ``runs/<run_id>.json``——原进程若稍后真去发布清单会因文件已存在而失败，已经有清单（正常收尾或已封存）时拒绝再写；
* 承接侧只有逐单元承接路径接受部分清单（``RunFacts.allow_partial_origin``），逐单元仍走原来的三个复核点，一条都不放宽；B-09、B-10、收尾不接受。

``seal`` 子命令：``seal --store <记录库> --run-dir <原运行的执行器输出目录> --origin-state aborted|superseded|vanished [--state-dir <调度器状态目录>]``，
成功时在运行目录写 ``partial-seal.json`` 收据并打印。
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

HERE = Path(__file__).resolve().parent
RECEIPT_SCHEMA = "unit-run-partial-seal/v1"
RECEIPT_NAME = "partial-seal.json"


class SealError(RuntimeError):
    """不能封存：原运行可能仍在写、已有清单、证据不全，或封存后的清单自检不通过。"""


def _records() -> Any:
    name = "run_seal_unit_records"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "unit_records.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _executor() -> Any:
    name = "run_seal_unit_executor"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "unit_executor.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise SealError(f"读不出 {path}：{error}") from error


def _pid_group_alive(pid: int) -> bool:
    """单元各自成会话，进程组号就是它的进程号：组里还有进程（含组长已退出、组员还在）就算活着。"""

    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def unfinished_pids(run_dir: Path) -> list[int]:
    """事件流里有开始、没有结束、进程组还活着的单元进程。事件流读不出来按“确认不了”处理（抛 SealError）。"""

    events_path = Path(run_dir) / "events.jsonl"
    started: dict[int, str] = {}
    try:
        lines = events_path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise SealError(f"读不到事件流 {events_path}，确认不了原运行的进程是否已退出：{error}") from error
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError as error:
            raise SealError(f"事件流有读不懂的行，确认不了原运行的进程是否已退出：{error}") from error
        pid = event.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool):
            continue
        if event.get("event") == "start":
            started[pid] = str(event.get("unit"))
        elif event.get("event") == "exit":
            started.pop(pid, None)
    return sorted(pid for pid in started if _pid_group_alive(pid))


def _collect_records(records: Any, store: Any, run_dir: Path, run_id: str) -> tuple[dict[str, dict[str, Any]], list[dict[str, str]]]:
    """运行目录里这一运行写过的记录，逐条核对后才算数；每个单元取完成时间最晚的一条。返回（单元 → 记录, 被跳过的记录及原因）。"""

    chosen: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, str]] = []
    for path in sorted((Path(run_dir) / "units").glob("*.record.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            skipped.append({"file": path.name, "reason": "记录文件读不出来"})
            continue
        unit_id = str(record.get("unit_id")) if isinstance(record, dict) else ""
        reason = None
        if not isinstance(record, dict) or not records.seal_ok(record, "record_sha256"):
            reason = "记录自摘要不符"
        elif not (record.get("run") or {}).get("run_id") == run_id:
            reason = "记录不属于这个运行"
        elif record.get("kind") != "formal":
            reason = "不是正式执行"
        elif records.parse_utc(record.get("completed_at_utc")) is None:
            reason = "没有完成时间（单元没跑完）"
        else:
            stored = store.record_path(unit_id, str(record["record_sha256"]))
            log = record.get("log") if isinstance(record.get("log"), dict) else {}
            digest = str(log.get("sha256") or "")
            stored_log = store.log_path(digest) if len(digest) == 64 else None
            if not stored.is_file() or records._read_json(stored) != record:
                reason = "记录没有入库，或库里那份与运行目录里的不是同一份"
            elif stored_log is None or not stored_log.is_file() or records.file_sha256(stored_log) != digest:
                reason = "日志不在记录库里或摘要不符"
        if reason is not None:
            skipped.append({"file": path.name, "unit_id": unit_id, "reason": reason})
            continue
        current = chosen.get(unit_id)
        if current is None or (records.parse_utc(record["completed_at_utc"]) or 0) >= (records.parse_utc(current["completed_at_utc"]) or 0):
            chosen[unit_id] = record
    return chosen, skipped


def seal_interrupted_run(*, store_root: Path | str, run_dir: Path | str, origin_state: str, state_dir: Path | str | None = None) -> dict[str, Any]:
    """后继封存一次中断的运行，返回封存收据。不满足条件抛 ``SealError``（原因是中文），不写任何东西。"""

    records = _records()
    if origin_state not in records.PARTIAL_ORIGIN_STATES:
        raise SealError(f"原运行终态只能是 {'／'.join(records.PARTIAL_ORIGIN_STATES)}，收到 {origin_state!r}")
    run_dir, store = Path(run_dir), records.RecordStore(Path(store_root))
    plan = _read_json(run_dir / "plan.json")
    run_id = str(plan.get("run_id") or "")
    if not run_id or plan.get("record_store") != str(store.root):
        raise SealError("运行目录的计划没有运行 ID，或它的记录库不是要封存的这个记录库")
    receipt_path = run_dir / RECEIPT_NAME
    existing = store.manifest(run_id)
    if existing is not None or store.manifest_path(run_id).exists():
        if existing is not None and existing.get("partial") is True and receipt_path.is_file():
            return _read_json(receipt_path)   # 幂等：已经封存过，原样返回收据
        raise SealError(f"运行 {run_id} 在记录库里已有清单（正常收尾发布的，或不是本后继封存的），不再封存")
    state = Path(state_dir) if state_dir is not None else _executor().default_state_dir()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(state / "scheduler.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SealError("本机有调度器持有状态目录锁：原运行可能仍在写，确认不了就不封存（来源仍在运行时照旧拒绝）") from error
        alive = unfinished_pids(run_dir)
        if alive:
            raise SealError(f"原运行还有未结束的单元进程组仍然存活：{alive[:5]}，不封存")
        chosen, skipped = _collect_records(records, store, run_dir, run_id)
        if not chosen:
            raise SealError("原运行没有任何已落库且完整的正式执行记录，没有可封存的成果")
        first = next(iter(chosen.values()))
        for record in chosen.values():
            if any(record.get(key) != first.get(key) for key in ("policy_sha256", "environment_sha256", "executor")):
                raise SealError("原运行的记录之间调度策略、环境或执行器不一致，不封存")
        gates_of: dict[str, list[str]] = {}
        for gate in plan.get("gates") or []:
            for unit_id in gate.get("units") or []:
                gates_of.setdefault(str(unit_id), []).append(str(gate.get("gate_id")))
        entries = []
        for unit_id in sorted(chosen):
            record = chosen[unit_id]
            entries.append({
                "unit_id": unit_id, "unit_type": record["unit_type"], "gates": sorted(gates_of.get(unit_id, [])), "test_group": None,
                "disposition": "executed", "record_sha256": record["record_sha256"],
                "record_path": str(store.record_path(unit_id, str(record["record_sha256"]))), "passed": bool(record.get("passed")),
                "spec_sha256": record["spec_sha256"], "inputs_sha256": record["inputs_sha256"], "inheritable": bool(record.get("inheritable")),
                "reasons": ["原运行被中断，由后继封存：这条记录是它落库的正式执行"]})
        planned = [str(unit["unit_id"]) for unit in plan.get("units") or []]
        if not planned or set(chosen) - set(planned):
            raise SealError("运行目录的计划缺少单元清单，或记录里有计划之外的单元")
        sealed_at = records.utc_now()
        started = min(records.parse_utc(record.get("started_at_utc")) or 0 for record in chosen.values())
        decided = records.datetime.fromtimestamp(started, records.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if started else sealed_at
        manifest = records.build_manifest(
            run_id=run_id, mode=records.RE_EXECUTE, inheritance_max_age_hours=records.MAX_AGE_HOURS, record_store=str(store.root),
            out_dir=str(run_dir), decided_at_utc=decided, completed_at_utc=sealed_at, policy_sha256=first["policy_sha256"],
            environment=first["environment"], environment_sha256=first["environment_sha256"], executor=first["executor"],
            planned_units=planned, units=entries, diagnostic=[], test_groups={},
            counts={"planned": len(planned), "executed": len(entries), "inherited": 0, "inherited_tests": 0},
            partial=True, sealed_by="successor", origin_state=origin_state, sealed_at_utc=sealed_at, origin_run_dir=str(run_dir))
        problems = records.verify_manifest(manifest, store=store)
        if problems:
            raise SealError("封存的部分完成清单自检不通过：" + "；".join(problems[:5]))
        try:
            store.put_manifest(manifest)      # O_EXCL：原进程若稍后真去发布清单，会因文件已存在而失败
        except FileExistsError as error:
            raise SealError("记录库里已有这个运行的清单（原进程抢先发布了），不再封存") from error
        receipt = {"schema_version": RECEIPT_SCHEMA, "run_id": run_id, "manifest_sha256": manifest["manifest_sha256"],
                   "origin_state": origin_state, "sealed_at_utc": sealed_at, "listed_units": len(entries), "planned_units": len(planned),
                   "skipped_records": skipped, "run_dir": str(run_dir), "record_store": str(store.root)}
        with (run_dir / RECEIPT_NAME).open("x", encoding="utf-8") as handle:
            os.chmod(handle.name, 0o600)
            handle.write(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
        return receipt
    finally:
        os.close(descriptor)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    seal = sub.add_parser("seal", help="后继为中断的运行封存部分完成清单")
    seal.add_argument("--store", type=Path, required=True)
    seal.add_argument("--run-dir", type=Path, required=True)
    seal.add_argument("--origin-state", required=True, choices=("aborted", "superseded", "vanished"))
    seal.add_argument("--state-dir", type=Path, default=None)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        receipt = seal_interrupted_run(store_root=args.store, run_dir=args.run_dir, origin_state=args.origin_state, state_dir=args.state_dir)
    except SealError as error:
        print(json.dumps({"status": "refused", "reason": str(error)}, ensure_ascii=False))
        return 3
    print(json.dumps({"status": "sealed", "receipt": receipt}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
