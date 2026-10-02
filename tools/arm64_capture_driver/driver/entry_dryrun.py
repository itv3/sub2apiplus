#!/usr/bin/env python3
"""入口空跑（E4-02 日常化）：在演练根里用入口编排器把入口跑一遍（零请求、不写生产总账与正式坐标），结论按提交与部署
收据落盘。遗留缺陷不再攒到下次升级才一个一个冒出来：每次受管工具部署后空跑一次，一次报全。

为什么：入口平时不跑（CI 不跑真实链），上一轮升级之后留下的三个缺陷（真实链夹具没跟上工具改动、场景清单 covers 没同步、
历史测试逐字比 covers）直到下一次升级过入口时才先后暴露，每暴露一个就要重新部署、整套重跑。

* 演练根：数据根 ``staging/entry-dryrun-<UTC>/``（入口编排器的 ``ENTRY_ROOT``；容器经 ``/capture`` 只看得到数据根），里面一本
  fixture_only 演练总账（项目总账模块 ``create-project-ledger --fixture-only``），建账本之后的产物（计时账本、预检 Campaign、
  Job 演练、启动探测、发布认证、P0 收据）都在它下面；生产项目总账、正式计时账本与正式认证坐标都不写。演练参数文件按本轮
  参数文件改写产物坐标（RUNROOT、STAMP、项目截止、三份认证与 pre-A3 认证坐标、ENTRY_*）。编排器在演练根里拒绝 VC-0 收口
  （首批是真实官方取证），空跑最远到 ``p0-receipt``。
* 跑：``env -i`` 干净环境里 ``nice`` 执行 ``entry.sh --to <步骤>``（默认 ``atomic-double``：建账本之前那一段；``p0-receipt`` 连
  建账本之后一起）；升级开工的空跑 ``--opening``：重新执行全集并带读集审计（``--reexecute-gates --audit-reads``），默认到
  ``p0-receipt``。入口门禁与 pre-A3 用同一份单元执行记录库：刚由后台验证（E4-01）验过的单元输入没变就承接，不重复劳动。
* 让路：有采集在跑（统一调度执行器的整机预约有存活的申请方）就不起，记 ``yielded``；空跑不申请整机，不能挡采集——门禁单元
  本来就会被采集预约停派。
* 结论 ``entry-dryrun/v1``：``<RUNROOT>/entry-dryrun/<提交前 12 位>-<部署收据摘要前 12 位>[-opening].json``，状态 running／passed／
  failed／yielded，带编排器每一步的动作、结论与原因（没通过的几步一次列全）、演练根与日志位置。同一提交＋同一部署（同一种
  空跑）已有在跑或已有结论就不重复跑。

子命令：

* ``start --runroot --data-root --vc-env --bundle --branch --commit [--opening] [--to <步骤>] [--entry <entry.sh>]``：起一次空跑
  （新会话、后台）；退出码 0 已起或已有结论，4 让路，2 用法或环境错误。
* ``run --result``（内部）：建演练根与演练总账、写演练参数文件、跑编排器、写结论。
* ``stop --runroot [--reason]``：停下在跑的空跑（先标 superseded 再整组终止，执行器收到 SIGTERM 会终止全部单元会话）。空跑与
  前台入口门禁共用调度器和入口门禁工作目录，修复轮跑定向回归之前要停下（后台验证的 ``stop`` 连空跑一起停）。
* ``status --runroot``：打印全部结论。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

SCHEMA = "entry-dryrun/v1"
DIRECTORY = "entry-dryrun"
HERE = Path(__file__).resolve().parent
NICENESS = 10
DEFAULT_TO = "atomic-double"
OPENING_TO = "p0-receipt"
# 演练根里编排器能走到的最远一步（VC-0 收口会发正式请求，演练根里一律拒绝）。
ALLOWED_TO = ("entry-preflight", "policy-compatibility", "policy-activation", "entry-gates", "pre-a3", "zero-request-smoke",
              "atomic-double", "ledger", "environment-p0", "ledger-checkpoint", "preflight-plan", "job-rehearsal",
              "client-launch-probe", "release-certification", "p0-receipt")
# 演练参数文件里改写的坐标（其余键原样沿用本轮参数文件）。
LEDGER_MODULE = "tools.official_client_capture.codex_upgrade_project_ledger"
APPROVED_BY = "入口空跑（演练总账）"
CLEAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class DryRunError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sibling(name: str) -> Any:
    """同目录的辅助模块（驱动目录与 tools/ci 都是平铺的一组文件，按路径加载）。"""

    module_name = f"{name}_sibling"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, HERE / f"{name}.py")
    if spec is None or spec.loader is None:
        raise DryRunError(f"找不到同目录的 {name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module   # dataclass 等按模块名回查，先登记再执行
    spec.loader.exec_module(module)
    return module


def _bv() -> Any:
    return _sibling("background_validation")


def _parse_env() -> Any:
    """驱动的参数文件解析（parse_env.py 只在驱动目录里有；tools/ci 原件跑测试时取仓库里的驱动那一份）。"""

    for path in (HERE / "parse_env.py", HERE.parent / "arm64_capture_driver" / "driver" / "parse_env.py"):
        if path.is_file():
            spec = importlib.util.spec_from_file_location("parse_env_sibling", path)
            if spec is not None and spec.loader is not None:
                module = importlib.util.module_from_spec(spec)
                sys.modules["parse_env_sibling"] = module
                spec.loader.exec_module(module)
                return module
    raise DryRunError("找不到驱动的 parse_env.py")


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


def capture_busy(state_dir: Path | None = None) -> str | None:
    """有采集在跑时返回说明：统一调度执行器的整机预约有存活的申请方（vc-batch.sh 与 VC-0 收口都在批次期间持有）。"""

    executor = _sibling("unit_executor")
    reservation = executor.Reservation(state_dir or executor.default_state_dir()).current()
    if reservation is None:
        return None
    return f"有采集在跑：整机预约属于 {reservation.get('owner')}（进程 {reservation.get('owner_pid')}）"


def result_path(runroot: Path, commit: str, deployment: Mapping[str, Any], *, opening: bool) -> Path:
    suffix = "-opening" if opening else ""
    return Path(runroot) / DIRECTORY / f"{commit[:12]}-{str(deployment['sha256'])[:12]}{suffix}.json"


def results(runroot: Path) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for path in sorted((Path(runroot) / DIRECTORY).glob("*.json")):
        payload = _read(path)
        if payload is not None:
            found.append((path, payload))
    return found


def effective_status(payload: Mapping[str, Any]) -> str:
    status = str(payload.get("status"))
    if status == "running" and not _bv()._pid_alive(payload.get("pid")):
        return "vanished"
    return status


def stop(runroot: Path, reason: str) -> list[str]:
    """停下在跑的空跑，标 superseded（与后台验证同一做法：先标再终止，后台 ``run`` 收尾时看到不是 running 就不再写）。"""

    bv = _bv()
    stopped = []
    for path, _payload in results(runroot):
        current = _read(path)
        if current is None or current.get("status") != "running":
            continue
        _write(path, {**current, "status": "superseded", "completed_at_utc": _utc_now(), "reason": reason})
        bv._terminate(current)
        stopped.append(path.name)
    return stopped


def rehearsal_env(values: Mapping[str, str], *, root: Path, runroot: Path, stamp: str, deadline: str, bundle: str, branch: str,
                  commit: str) -> dict[str, str]:
    """演练参数：本轮参数原样沿用，只把产物坐标改到演练根与空跑自己的 RUNROOT（正式坐标一律不写）。"""

    certification = root / "control" / "policy-certification"
    changed = {
        "RUNROOT": str(runroot), "STAMP": stamp, "PROJECT_DEADLINE_UTC": deadline,
        "POLICY_COMPAT_RECEIPT": str(certification / "policy-compatibility.json"),
        "POLICY_ACTIVATION": str(certification / "policy-activation.json"),
        "RELEASE_CERTIFICATION": str(certification / "release-certification.json"),
        "PRE_A3_CERTIFICATION": str(certification / "pre-a3-path-certification.json"),
        "ENTRY_BUNDLE": bundle, "ENTRY_BRANCH": branch, "ENTRY_COMMIT": commit, "ENTRY_ROOT": str(root),
    }
    return {**dict(values), **changed}


def _env_text(values: Mapping[str, str]) -> str:
    for key, value in values.items():
        if re.search(r'["`$\\\n]', value):
            raise DryRunError(f"参数 {key} 的值含引号、反引号、$、反斜杠或换行，不能写进演练参数文件")
    return "".join(f'{key}="{value}"\n' for key, value in values.items())


# ---------------------------------------------------------------- 启动与运行

def start(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    bv = _bv()
    deployment = bv.latest_deployment(args.data_root)
    path = result_path(args.runroot, args.commit, deployment, opening=args.opening)
    existing = _read(path)
    if existing is not None and effective_status(existing) in ("running", "passed", "failed"):
        return 0, {"action": "exists", "result": str(path), "status": effective_status(existing)}
    to = args.to or (OPENING_TO if args.opening else DEFAULT_TO)
    if to not in ALLOWED_TO:
        raise DryRunError(f"--to 只能到 {ALLOWED_TO[-1]}（VC-0 收口会发正式请求，演练根里不做）：{to}")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA, "commit": args.commit, "branch": args.branch, "bundle": str(args.bundle),
        "deployment": deployment, "opening": bool(args.opening), "to": to, "vc_env": str(args.vc_env),
        "data_root": str(args.data_root), "runroot": str(args.runroot), "entry": str(args.entry),
        "status": "running", "pid": None, "started_at_utc": _utc_now(), "completed_at_utc": None,
        "rehearsal_root": None, "log": None, "exit_code": None, "steps": [], "failed_steps": [], "reason": None,
    }
    busy = capture_busy()
    if busy:
        _write(path, {**payload, "status": "yielded", "completed_at_utc": _utc_now(), "reason": busy})
        return 4, {"action": "yielded", "result": str(path), "reason": busy}
    _write(path, payload)
    log = path.with_suffix(".log")
    with log.open("ab") as handle:
        process = subprocess.Popen(["nice", "-n", str(NICENESS), sys.executable, "-B", str(Path(__file__).resolve()), "run",
                                    "--result", str(path)],
                                   stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
                                   env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    current = _read(path) or payload
    if current.get("status") == "running" and current.get("pid") is None:
        _write(path, {**current, "pid": process.pid, "log": str(log)})
    return 0, {"action": "started", "result": str(path), "pid": process.pid, "to": to}


def _prepare(payload: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    """建演练根、fixture_only 演练总账与演练参数文件，返回（演练根，空跑 RUNROOT，演练参数文件）。"""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    data_root = Path(payload["data_root"])
    root = data_root / "staging" / f"entry-dryrun-{stamp}"
    runroot = Path(payload["runroot"]) / DIRECTORY / f"run-{stamp}"
    for directory in (root, root / "evidence" / "campaigns", root / "control" / "policy-certification", runroot):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=30)).strftime("%Y-%m-%dT%H:00:00Z")
    completed = subprocess.run(
        [sys.executable, "-B", "-m", LEDGER_MODULE, "create-project-ledger",
         "--ledger-dir", str(root / "evidence" / "campaigns" / "upgrade-project-ledger"), "--project-id", f"entry-dryrun-{stamp}",
         "--absolute-deadline-utc", deadline, "--deadline-approved-by", APPROVED_BY, "--estimation-policy", "none",
         "--estimation-policy-approved-by", APPROVED_BY, "--fixture-only"],
        cwd=str(data_root), env={**os.environ, "PYTHONPATH": str(data_root), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=300)
    if completed.returncode != 0:
        raise DryRunError(f"演练总账没建成：{(completed.stderr or completed.stdout)[-400:]}")
    values = _parse_env().parse(Path(payload["vc_env"]).read_text(encoding="utf-8"))
    env_values = rehearsal_env(values, root=root, runroot=runroot, stamp=stamp, deadline=deadline, bundle=payload["bundle"],
                               branch=payload["branch"], commit=payload["commit"])
    env_file = runroot / "env.sh"
    env_file.write_text(_env_text(env_values), encoding="utf-8")
    env_file.chmod(0o600)
    return root, runroot, env_file


def _orchestrator_run(runroot: Path) -> dict[str, Any] | None:
    runs = sorted((runroot / "entry-runs").glob("*/run.json"))
    if not runs:
        return None
    with contextlib.suppress(OSError, ValueError):
        return json.loads(runs[-1].read_text(encoding="utf-8"))
    return None


def run(result: Path) -> int:
    path = Path(result)
    payload = _read(path)
    if payload is None:
        raise DryRunError(f"空跑结论文件不可读：{path}")
    try:
        root, runroot, env_file = _prepare(payload)
    except (DryRunError, OSError, subprocess.SubprocessError) as error:
        _write(path, {**payload, "status": "failed", "completed_at_utc": _utc_now(), "reason": f"演练根没准备好：{error}"})
        return 0
    payload = {**payload, "rehearsal_root": str(root), "dryrun_runroot": str(runroot), "env_file": str(env_file)}
    _write(path, payload)
    argv = ["env", "-i", "HOME=/root", "LANG=C.UTF-8", f"PATH={CLEAN_PATH}", f"ARM64_VC_ENV={env_file}",
            "bash", str(payload["entry"]), "--to", payload["to"]]
    if payload.get("opening"):
        argv += ["--reexecute-gates", "--audit-reads"]
    log = runroot / "entry.out"
    with log.open("ab") as handle:
        returncode = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT).returncode
    summary = _orchestrator_run(runroot) or {}
    steps = [{"step_id": step.get("step_id"), "action": step.get("action"), "status": step.get("status"),
              "reasons": list(step.get("reasons") or [])[:4]} for step in summary.get("steps") or []]
    failed = [step for step in steps if step["status"] == "failed" or step["action"] in ("阻塞", "被阻塞")]
    status = "passed" if returncode == 0 and not failed and summary else "failed"
    reason = None if status == "passed" else (summary.get("stopped_because") or f"入口编排器退出码 {returncode}，日志 {log}")
    current = _read(path) or payload
    if current.get("status") != "running":
        return 0   # 已被 stop 标为 superseded
    _write(path, {**current, "status": status, "completed_at_utc": _utc_now(), "exit_code": returncode, "log": str(log),
                  "steps": steps, "failed_steps": [step["step_id"] for step in failed], "reason": reason})
    return 0


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_start = sub.add_parser("start", help="起一次入口空跑（后台）")
    p_start.add_argument("--runroot", type=Path, required=True)
    p_start.add_argument("--data-root", type=Path, required=True)
    p_start.add_argument("--vc-env", type=Path, required=True, help="本轮驱动参数文件（ARM64_VC_ENV），演练参数由它改写")
    p_start.add_argument("--bundle", type=Path, required=True)
    p_start.add_argument("--branch", required=True)
    p_start.add_argument("--commit", required=True)
    p_start.add_argument("--opening", action="store_true", help="升级开工的空跑：重新执行全集并带读集审计，默认到 p0-receipt")
    p_start.add_argument("--to", default=None, help=f"编排器跑到哪一步（默认 {DEFAULT_TO}；--opening 默认 {OPENING_TO}）")
    p_start.add_argument("--entry", type=Path, default=HERE / "entry.sh")
    p_run = sub.add_parser("run", help="（内部）建演练根、跑编排器、写结论")
    p_run.add_argument("--result", type=Path, required=True)
    p_stop = sub.add_parser("stop", help="停下在跑的空跑，标 superseded")
    p_stop.add_argument("--runroot", type=Path, required=True)
    p_stop.add_argument("--reason", default="修复轮跑定向回归前停下（空跑与前台入口门禁共用调度器）")
    p_status = sub.add_parser("status")
    p_status.add_argument("--runroot", type=Path, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "start":
            if not re.fullmatch(r"[0-9a-f]{40}", args.commit):
                raise DryRunError("--commit 要完整的 40 位小写提交号")
            for name in ("runroot", "data_root", "vc_env", "bundle"):
                if not getattr(args, name).is_absolute():
                    raise DryRunError(f"--{name.replace('_', '-')} 必须是绝对路径")
            code, outcome = start(args)
            print(json.dumps(outcome, ensure_ascii=False))
            return code
        if args.command == "run":
            return run(args.result)
        if args.command == "stop":
            print(json.dumps({"stopped": stop(args.runroot, args.reason)}, ensure_ascii=False))
            return 0
        print(json.dumps([{**payload, "effective_status": effective_status(payload), "path": str(path)}
                          for path, payload in results(args.runroot)], ensure_ascii=False, indent=2))
        return 0
    except DryRunError as error:
        print(f"入口空跑：{error}", file=sys.stderr)
        return 2
    except Exception as error:   # 后台验证模块的 ValidationError 等（部署收据缺失）
        if type(error).__name__ == "ValidationError":
            print(f"入口空跑：{error}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
