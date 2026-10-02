#!/usr/bin/env python3
"""修复轮的后台验证（E4-01，指南「修好接着跑」）：修复提交部署之后，由同一个统一调度执行器在后台跑全量门禁，结论按
修复提交与当时的部署收据落盘；批次驱动在每个批次边界读这份结论。

为什么：修复轮原先以全量门禁通过为部署前提（串行约 95 分钟），而工具本来只认修复提交、定向回归与部署收据。现在改为
定向回归（入口门禁 ``regression`` 组合）通过即部署接着跑，全量门禁挪到后台：入口门禁 ``full-gates`` 组合、全集通过
模式（承接记录库里输入没变的单元，只执行受修复影响的）、``nice`` 降优先级、``--require-deployed``（门禁前核对数据根
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

同一提交＋同一部署只有一份；状态变化原子替换写。

子命令：

* ``start --runroot --data-root --bundle --branch --commit [--profile] [--record-store] [--work] [--vc-env]``：绑定数据根
  最新部署收据；同一提交＋同一部署已有在跑或已有 passed／failed 结论就不重复起，打印现有结论；在跑的其它后台验证先
  停下、标 superseded；然后在新会话里起 ``run``。
* ``run``（内部）：跑入口门禁，按结论写状态。收到 SIGTERM 时把它转给入口门禁（执行器会终止全部单元会话）。
* ``stop --runroot [--reason]``：停下在跑的后台验证并标 superseded。修复轮跑定向回归之前用：同一台机器同一时间只能有
  一个调度器，上一轮的验证也已作废。
* ``check-boundary --runroot --data-root``：批次边界——取最新部署收据绑定的结论：failed／aborted 退出 3（拒绝派发下一批，
  打印没通过的门禁与日志位置）；passed、running 退出 0；没有结论（没起、被停下或进程已经不在）退出 0 并提示先 start。
* ``require-passed --runroot --data-root``：VC-5 验收前——最新部署绑定的结论是 passed 才退出 0，否则退出 3。
* ``status --runroot``：打印全部结论（JSON）。

退出码：0 通过或放行；2 用法或环境错误；3 拒绝（有失败结论、要求通过而没有通过结论）。
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
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
# 与驱动 install.py 的 latest_deploy_receipt 同一规则（按文件名里的时间戳取最新）。
DEPLOY_RECEIPT_GLOB = "codex-*-supervisor-enable-*.json"
IDENTITY_FIELDS = ("policy_version", "policy_sha256", "tool_files_sha256", "wire_producer_sha256", "evidence_semantics_sha256",
                   "control_sha256")
HERE = Path(__file__).resolve().parent
NICENESS = 10
# 停下后台验证时给入口门禁与执行器的清理时间（执行器收到 SIGTERM 终止全部单元会话），超时再 SIGKILL 整组。
STOP_GRACE_SECONDS = 60.0


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


def latest_deployment(data_root: Path) -> dict[str, Any]:
    """数据根最新的受管工具部署收据：文件名、sha256、工具身份。"""

    control = Path(data_root) / "control"
    candidates = sorted((path for path in control.glob(DEPLOY_RECEIPT_GLOB) if path.is_file() and not path.is_symlink()),
                        key=lambda path: path.name.split("-supervisor-enable-", 1)[1].lower())
    if not candidates:
        raise ValidationError(f"数据根没有受管工具部署收据：{control}")
    path = candidates[-1]
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValidationError(f"部署收据不是合法 JSON：{path}：{error}") from error
    if not isinstance(payload, dict) or payload.get("status") != "passed":
        raise ValidationError(f"最新部署收据不是 passed：{path}")
    return {"receipt": path.name, "sha256": _sha256_file(path), "identity": {field: payload.get(field) for field in IDENTITY_FIELDS}}


def result_path(runroot: Path, commit: str, deployment: Mapping[str, Any]) -> Path:
    return Path(runroot) / DIRECTORY / f"{commit[:12]}-{str(deployment['sha256'])[:12]}.json"


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


def for_deployment(runroot: Path, deployment: Mapping[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    """绑定这次部署的结论（同一部署验证过多个提交时取最后开始的一份）。"""

    bound = [(path, payload) for path, payload in results(runroot)
             if (payload.get("deployment") or {}).get("sha256") == deployment["sha256"]]
    return max(bound, key=lambda item: (str(item[1].get("started_at_utc")), item[0].name)) if bound else None


# ---------------------------------------------------------------- 启动、停止与后台运行

def _terminate(payload: Mapping[str, Any]) -> None:
    """终止后台运行的整个进程组：先 SIGTERM（``run`` 转给入口门禁，执行器终止全部单元会话），宽限后 SIGKILL。"""

    pid = payload.get("pid")
    if not _pid_alive(pid):
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(int(pid), signal.SIGTERM)
    deadline = time.monotonic() + STOP_GRACE_SECONDS
    while _pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(int(pid), signal.SIGKILL)


def stop(runroot: Path, reason: str, *, keep: Path | None = None) -> list[str]:
    """停下在跑的后台验证（``keep`` 除外），标 superseded。"""

    stopped = []
    for path, _payload in results(runroot):
        current = _read(path)   # 紧挨着写之前再读一次：刚写完结论的不再改
        if path == keep or current is None or current.get("status") != "running":
            continue
        # 先标 superseded 再终止：后台 ``run`` 收尾时看到状态已不是 running 就不再写（否则会写成 aborted）。
        _write(path, {**current, "status": "superseded", "completed_at_utc": _utc_now(), "reason": reason})
        _terminate(current)
        stopped.append(path.name)
    return stopped


def start(args: argparse.Namespace) -> dict[str, Any]:
    deployment = latest_deployment(args.data_root)
    path = result_path(args.runroot, args.commit, deployment)
    existing = _read(path)
    if existing is not None and effective_status(existing) in ("running", "passed", "failed"):
        return {"action": "exists", "result": str(path), "status": effective_status(existing)}
    superseded = stop(args.runroot, f"被提交 {args.commit[:12]} 在部署 {deployment['receipt']} 上的后台验证取代", keep=path)
    out = path.parent / "runs" / f"{path.stem}-{datetime.now(timezone.utc).strftime('%Y%m%dt%H%M%Sz')}"
    payload: dict[str, Any] = {
        "schema_version": SCHEMA, "commit": args.commit, "branch": args.branch, "bundle": str(args.bundle),
        "deployment": deployment, "profile": args.profile, "mode": "full-set-pass", "status": "running", "pid": None,
        "started_at_utc": _utc_now(), "completed_at_utc": None, "out": str(out), "log": f"{out}.log",
        "record_store": str(args.record_store) if args.record_store else None, "work": str(args.work) if args.work else None,
        "vc_env": str(args.vc_env) if args.vc_env else None, "entry_gates": str(args.entry_gates),
        "failed_gates": [], "reason": None, "returncode": None,
    }
    _write(path, payload)
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
    argv = ["bash", payload["entry_gates"], "--profile", payload["profile"], "--mode", "full-set-pass", "--require-deployed",
            "--out", payload["out"]]
    for flag, key in (("--record-store", "record_store"), ("--work", "work")):
        if payload.get(key):
            argv += [flag, payload[key]]
    argv += [payload["bundle"], payload["branch"], payload["commit"]]
    received: list[int] = []

    def forward(signum: int, _frame: Any) -> None:
        received.append(signum)

    signal.signal(signal.SIGTERM, forward)
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
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
    if received:
        status, reason = "aborted", f"收到信号 {received[0]}"
    elif summary is None:
        status, reason = "aborted", f"入口门禁没有结论（退出码 {returncode}），日志 {payload.get('log')}"
    elif checked != bound:
        status, reason = "aborted", f"入口门禁核对的部署收据（{checked or '没有核对'}）不是开始时绑定的 {bound}：部署在验证途中换了"
    elif returncode == 0 and not failed and summary.get("status") == "passed":
        status, reason = "passed", None
    else:
        status, reason = "failed", f"入口门禁退出码 {returncode}"
    _write(path, {**current, "status": status, "completed_at_utc": _utc_now(), "reason": reason, "returncode": returncode,
                  "failed_gates": failed})
    return 0


# ---------------------------------------------------------------- 批次边界与 VC-5 前核对

def check(runroot: Path, data_root: Path, *, require_passed: bool) -> tuple[int, str]:
    """批次边界（``require_passed`` 为假）或 VC-5 验收前（为真）的判定：（退出码，说明）。"""

    deployment = latest_deployment(data_root)
    found = for_deployment(runroot, deployment)
    if found is None:
        message = f"当前部署（{deployment['receipt']}）还没有后台验证：先 background-validate.sh start <bundle> <分支> <提交>"
        return (3, message) if require_passed else (0, "提示：" + message)
    path, payload = found
    status = effective_status(payload)
    where = f"提交 {str(payload.get('commit'))[:12]}，结论 {path}，日志 {payload.get('log')}"
    if status == "passed":
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
    p_start.add_argument("--work", type=Path, default=None, help="入口门禁的测试树与缓存目录（与前台入口门禁分开，避免并发锁）")
    p_start.add_argument("--vc-env", type=Path, default=None, help="驱动参数文件（ARM64_VC_ENV），后台运行的入口门禁要用")
    p_start.add_argument("--entry-gates", type=Path, default=HERE / "entry-gates.sh")
    p_run = sub.add_parser("run", help="（内部）跑入口门禁并写结论")
    p_run.add_argument("--result", type=Path, required=True)
    p_stop = sub.add_parser("stop", help="停下在跑的后台验证，标 superseded")
    p_stop.add_argument("--runroot", type=Path, required=True)
    p_stop.add_argument("--reason", default="修复轮跑定向回归前停下（上一轮提交的验证已作废）")
    for name in ("check-boundary", "require-passed"):
        p = sub.add_parser(name)
        p.add_argument("--runroot", type=Path, required=True)
        p.add_argument("--data-root", type=Path, required=True)
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
            code, message = check(args.runroot, args.data_root, require_passed=args.command == "require-passed")
            print(message, flush=True)
            return code
        print(json.dumps([{**payload, "effective_status": effective_status(payload), "path": str(path)}
                          for path, payload in results(args.runroot)], ensure_ascii=False, indent=2))
        return 0
    except ValidationError as error:
        print(f"后台验证：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
