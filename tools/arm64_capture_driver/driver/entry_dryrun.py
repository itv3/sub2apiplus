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
  ``p0-receipt``；``--reexecute``：只重新执行全集、不带审计（冷跑计时用，审计会多约两倍开销）。入口门禁与 pre-A3 用同一份单元执行记录库。入口门禁的范围（第 9 项）：缺省（不带 ``--opening``／``--reexecute``）走入口编排器的缺省 ``--gate-scope auto``——Codex
  只重新执行 Codex 计划的采集工具测试，基点与回退规则见 ``entry_orchestrator.py``（找不到发版 tag 等一律回退全集并写明原因）；``--opening``／``--reexecute``（编排器的
  ``--reexecute-gates``）显式重新执行**全集**，Claude 也保持全集；只有带 ``--unit-inherit-request`` 时才按 B5a-08 合同逐单元承接（也是全集口径）。
* 让路：有采集在跑（统一调度执行器的整机预约有存活的申请方）就不起，记 ``yielded``；空跑不申请整机，不能挡采集——门禁单元
  本来就会被采集预约停派。
* 排队（10-07 B5a-10 ARM64 验收发现）：入口门禁工作目录（``entry-gates.sh`` 缺省的数据根上一级 ``entry-gates-work``）同一时刻
  只许一次入口门禁，编排器调用它时不排队，并发的第二次以退出码 3 失败；两客户端都纳管时，后台验证通过后两客户端的空跑几乎
  同时接上，必然撞上。所以空跑调用编排器之前，先在该工作目录里取一把空跑串行锁（``.entry-dryrun.lock``，flock），持有到编排器
  结束：后到的空跑排队等前一份跑完（上限 3 小时，超时结论 failed）；排队期间结论被 stop 标为 superseded 就放弃、不跑编排器。
  真的排过队（至少 1 秒）才在结论里写 ``serial_lock_waited_seconds``，只有一个客户端纳管时不会排队，结论与改造前同形。前台
  入口与后台验证的入口门禁不经这把锁，行为不变。
* 开跑前的日常维护（``entry_housekeeping.py``）：清 Go 编译缓存里 6 小时以上没用过的条目、清理单元执行记录库（保留期内与仍被
  v2 认证、P0 v2 证据引用的都留着）、只留最近 5 次空跑的演练根与产物、部署留下的旧暂存树每组只留最近 2 份（E4-03），再查
  根盘余量；越过停线（与派发前守卫同一条）就不跑，
  结论 failed、没通过的一步记 ``disk``。维护报告摘要写进结论的 ``housekeeping``，全文在结论旁的 ``<结论名>.housekeeping.json``。
  环境变量 ``ENTRY_DRYRUN_HOUSEKEEPING=off`` 跳过维护（只给测试的替身环境用：不能碰开发机的 Go 缓存、不能看开发机的磁盘；
  驱动不设）。
* 结论 ``entry-dryrun/v1``：``<RUNROOT>/entry-dryrun/<客户端前缀><提交前 12 位>-<部署收据摘要前 12 位>[-opening|-reexecute|
  -unit-inherit-<请求摘要前 12 位>][-to-<终点>].json``（终点不是这种空跑的默认终点时才带，例如后台验证接着的那次是
  ``-to-pre-a3``），状态 running／passed／failed／yielded，带编排器每一步的动作、结论与原因（没通过的几步一次列全）、演练根与
  日志位置。同一提交＋同一部署（同一种空跑）已有在跑或已有结论就不重复跑。

按客户端（Claude 升级 B5a-10）：本轮客户端取驱动参数文件的 ``CLIENT``（缺省 codex），空跑绑定该客户端最新的部署收据，结论
文件名带客户端表登记的前缀（Codex 为空，结论与改造前同名同形；Claude 是 ``claude-``），同一提交＋同一部署两客户端各一份；
演练总账按该客户端建。非 Codex 的结论另带 ``client`` 与没通过各步的分类（``failure_classes``、``real_failed_steps``）：
客户端表登记为尚未接入的步骤记 ``registered_pending``（写明落实它的工作项）；pre-A3 一步按演练根里的 pre-A3 认证（没通过时
是旁路文件 ``.failed-<UTC>.json``）登记的记录库与运行清单逐场景读日志里的场景结果——失败场景的链体登记了占位
（指标 ``pending``，例如等待 B2-08）、错误信息就是该占位、零请求零网络、隔离没有降级的记 ``scenario_pending``，其余记
``real``；被阻塞的步骤记 ``blocked``。``real_failed_steps`` 为空即「只差登记过的待接入与占位，没有真实失败」。

逐单元承接（B5a-08 合同，B5a-10 接入）：``--unit-inherit-request <请求>`` 原样交给入口编排器（入口门禁与 pre-A3 按请求承接记录库
里有效的单元，只执行依赖变化或缺少有效记录的单元；请求的批准与上下文核对都在入口门禁里做）；与 ``--opening``／
``--reexecute``（重新执行全集）互斥。登记来源使用精确读集策略时，显式带 ``--audit-policy <策略>``，策略按原登记合同校验、
摘要进入结论，取锁后派发前与结束收口时再次复核。策略只随逐单元承接或显式开工空跑使用；授权、撤销和来源复核仍由原门禁完成。
后台验证接着的空跑不带请求；后台明确登记了策略时，不自动启动缺少当前批准的空跑，由操作员带请求与策略显式执行。
声明级承接的采样方式（步骤 5 WP3b）：``--declared-sample off|always|every:N``（缺省 off）原样交给入口编排器，再交给入口门禁与 pre-A3 的执行器，
决定声明级命令单元的读集证据来自登记表还是每次执行自己采样；非法值在这里就拒绝。非 off 的取值写进结论的 ``declared_sample``、结论文件名
带 ``-declared-sample-<取值>``（采样方式不同的空跑是不同的空跑，不互相顶替），后台验证接着的空跑不带它；缺省时结论、文件名与命令行与改造前逐字相同。
Claude 逐单元空跑可消费本轮已有的普通或完整79接线 pre-A3 认证：准备阶段用原认证验证器核验源文件，原字节写入演练认证
坐标，随后仍由入口复核与登记复用。源认证和原记录不改写；缺源保留首次运行，坏源明确失败，不降级另跑掩盖问题。

子命令：

* ``start --runroot --data-root --vc-env --bundle --branch --commit [--opening|--reexecute|--unit-inherit-request <请求>] [--audit-policy <策略>] [--to <步骤>]
  [--entry <entry.sh>]``：起一次空跑（新会话、后台）；退出码 0 已起或已有结论，4 让路，2 用法或环境错误。
* ``run --result``（内部）：建演练根与演练总账、写演练参数文件、跑编排器、写结论。
* ``stop --runroot [--reason]``：停下在跑的空跑（先标 superseded 再整组终止，执行器收到 SIGTERM 会终止全部单元会话）。空跑与
  前台入口门禁共用调度器和入口门禁工作目录，修复轮跑定向回归之前要停下（后台验证的 ``stop`` 连空跑一起停）。
* ``status --runroot``：打印全部结论。
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
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

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
APPROVED_BY = "入口空跑（演练总账）"
CLEAN_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# 演练根里的 pre-A3 认证坐标（rehearsal_env 改写的那一份）与场景结果行的前缀（与受管 pre-A3 认证模块的 SCENARIO_RESULT_PREFIX
# 同一行，测试核对两边一致）。
PRE_A3_CERTIFICATION_NAME = "pre-a3-path-certification.json"
PRE_A3_RESULT_PREFIX = "PRE_A3_SCENARIO_RESULT "
PRE_A3_UNIT_PREFIX = "pre-a3:"
# 空跑串行锁（见模块说明「排队」）：放在 entry-gates.sh 缺省的入口门禁工作目录里，上限 3 小时，每 2 秒试一次。
SERIAL_LOCK_NAME = ".entry-dryrun.lock"
SERIAL_LOCK_TIMEOUT = 3 * 3600
SERIAL_LOCK_POLL = 2.0


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


# 演练总账的模块与目录名取客户端表（B5a-06；按本轮参数文件的 CLIENT）。这个名字是 Codex 的模块（只读视图）。
LEDGER_MODULE = _sibling("client_table").spec("codex")["project_ledger_module"]


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


def result_path(runroot: Path, commit: str, deployment: Mapping[str, Any], *, opening: bool, reexecute: bool = False,
                to: str | None = None, client: str = "codex", unit_inherit: str | None = None,
                unit_inherit_full_gates: str | None = None, audit_policy: str | None = None,
                full_set_work: str | None = None) -> Path:
    """结论文件：客户端前缀＋提交＋部署收据＋空跑种类（开工、冷跑计时、逐单元承接）＋终点。终点不是这种空跑的默认终点时才
    写进名字：后台验证接着的那次（到 pre-A3）与明确执行的那次（默认 atomic-double 或到 P0 收据）是不同的空跑，不能互相顶替。
    客户端前缀取客户端表（Codex 为空，与改造前同名）；逐单元承接另带主请求摘要，显式配套请求与读集策略另加各自摘要，缺省命名不变。"""

    prefix = _sibling("client_table").spec(client)["dryrun_conclusion_prefix"]
    kind = "-opening" if opening else "-reexecute" if reexecute else f"-unit-inherit-{unit_inherit[:12]}" if unit_inherit else ""
    if unit_inherit_full_gates:
        kind += f"-full-gates-{unit_inherit_full_gates[:12]}"
    if audit_policy:
        kind += f"-audit-policy-{audit_policy[:12]}"
    if full_set_work:
        kind += f"-source-work-{full_set_work[:12]}"
    default = OPENING_TO if opening else DEFAULT_TO
    stop = f"-to-{to}" if to and to != default else ""
    return Path(runroot) / DIRECTORY / f"{prefix}{commit[:12]}-{str(deployment['sha256'])[:12]}{kind}{stop}.json"


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
        "PRE_A3_CERTIFICATION": str(certification / PRE_A3_CERTIFICATION_NAME),
        "ENTRY_BUNDLE": bundle, "ENTRY_BRANCH": branch, "ENTRY_COMMIT": commit, "ENTRY_ROOT": str(root),
    }
    return {**dict(values), **changed}


def _env_text(values: Mapping[str, str]) -> str:
    for key, value in values.items():
        if re.search(r'["`$\\\n]', value):
            raise DryRunError(f"参数 {key} 的值含引号、反引号、$、反斜杠或换行，不能写进演练参数文件")
    return "".join(f'{key}="{value}"\n' for key, value in values.items())


# ---------------------------------------------------------------- 启动与运行

def _client(vc_env: Path) -> str:
    """本轮客户端：驱动参数文件的 CLIENT（缺省 codex）。参数文件解析不了时按缺省客户端（错误照旧由运行时报出，与改造前相同）。"""

    table = _sibling("client_table")
    try:
        return str(table.client_of(_parse_env().parse(Path(vc_env).read_text(encoding="utf-8"))))
    except (OSError, ValueError, UnicodeDecodeError):
        return str(table.DEFAULT_CLIENT)


def _audit_policy_reference(path: Path) -> dict[str, str]:
    """只绑定无链接普通文件的实际字节，沿用原登记合同；实际单元集合仍由入口门禁发现后核对。"""

    if (not path.is_absolute() or ".." in path.parts
            or any(item.is_symlink() for item in (path, *path.parents)) or not path.is_file()):
        raise DryRunError("--audit-policy 必须是无符号链接的现有普通文件，使用规范绝对路径")
    try:
        raw = _sibling("full_set_receipt").file_bytes(path)
        policy = json.loads(raw)
        units = set(policy["units"]) if isinstance(policy, dict) and isinstance(policy.get("units"), dict) else set()
        _sibling("unit_records").validate_audit_policy(policy, units)
    except (OSError, ValueError, RuntimeError) as error:
        raise DryRunError(f"登记审计策略不可读或合同无效：{error}") from error
    return {"path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}


def _verify_audit_policy(payload: Mapping[str, Any]) -> Path | None:
    """按启动时的绑定复核策略，不能在排队期间或执行后悄悄改用另一份输入声明。"""

    reference = payload.get("audit_policy")
    if reference is None:
        return None
    if not isinstance(reference, dict) or set(reference) != {"path", "sha256"} or not isinstance(reference.get("path"), str):
        raise DryRunError("登记审计策略的启动绑定缺失或格式错误")
    path = Path(reference["path"])
    if _audit_policy_reference(path) != reference:
        raise DryRunError("登记审计策略在空跑启动后变化，拒绝执行或收口")
    return path


def _full_set_work_reference(work: Path, *, commit: str, bundle: Path, branch: str) -> dict[str, Any]:
    """复用既有工作区校验器绑定原树、外置字节码缓存和 bundle；三编译缓存只绑定坐标，不递归扫描。"""
    caches = {"GOCACHE": str(work / "go-build-cache"), "GOMODCACHE": str(work / "go-mod-cache"),
              "GOTMPDIR": str(work / "go-tmp")}
    if (not work.is_absolute() or ".." in work.parts
            or any(path.is_symlink() for root in (work, *(Path(value) for value in caches.values()))
                   for path in (root, *root.parents))):
        raise DryRunError("逐单元承接来源工作区须为无符号链接的规范绝对路径")
    try:
        workspace = _sibling("entry_gates").verify_scope_workspace(work / "test-tree", work / "pycache",
                                                                  commit=commit, bundle=bundle, branch=branch)
    except (OSError, ValueError) as error:
        raise DryRunError(f"逐单元承接来源工作区核验失败：{error}") from error
    reference = {"work": str(work), "source": {"commit": commit, "bundle": str(bundle), "branch": branch},
                 "workspace": workspace, "cache_roots": caches}
    digest = hashlib.sha256(json.dumps(reference, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {**reference, "sha256": digest}


def _verify_full_set_work(payload: Mapping[str, Any]) -> Path | None:
    """排队后、派发前和收口重验同一绑定；拒绝测试树、bundle 或缓存坐标漂移。"""
    reference = payload.get("full_set_work")
    if reference is None:
        return None
    if (not payload.get("unit_inherit_request") or not isinstance(reference, dict)
            or set(reference) != {"work", "source", "workspace", "cache_roots", "sha256"}
            or not isinstance(reference.get("work"), str)):
        raise DryRunError("逐单元承接来源工作区的启动绑定缺失或格式错误")
    work = Path(reference["work"])
    current = _full_set_work_reference(work, commit=str(payload["commit"]), bundle=Path(str(payload["bundle"])),
                                       branch=str(payload["branch"]))
    if current != reference:
        raise DryRunError("逐单元承接来源工作区在空跑启动后变化，拒绝执行或收口")
    return work


def start(args: argparse.Namespace) -> tuple[int, dict[str, Any]]:
    bv = _bv()
    # B5a-10：按本轮客户端绑定它最新的部署收据，结论文件名带客户端表登记的前缀（Codex 为空，与改造前相同）。
    client = _client(args.vc_env)
    deployment = bv.latest_deployment(args.data_root, client)
    request = getattr(args, "unit_inherit_request", None)
    full_gates_request = getattr(args, "unit_inherit_full_gates_request", None)
    if full_gates_request is not None and request is None:
        raise DryRunError("full-gates 逐单元承接请求须与 --unit-inherit-request 一起提供")
    if args.opening and args.reexecute:
        raise DryRunError("--opening 已含重新执行全集，不要再加 --reexecute")
    if request is not None and (args.opening or args.reexecute):
        raise DryRunError("逐单元承接请求不能与 --opening／--reexecute（重新执行全集）同用")
    work = getattr(args, "full_set_work", None)
    if work is not None and request is None:
        raise DryRunError("--full-set-work 必须与 --unit-inherit-request 一起提供")
    work_reference = (_full_set_work_reference(Path(work), commit=args.commit, bundle=args.bundle, branch=args.branch)
                      if work is not None else None)
    policy = getattr(args, "audit_policy", None)
    if policy is not None and request is None and not args.opening:
        raise DryRunError("--audit-policy 只与 --unit-inherit-request 或显式 --opening 一起使用")
    policy_reference = _audit_policy_reference(Path(policy)) if policy is not None else None
    # 声明级承接的采样方式（步骤 5 WP3b）：缺省 off；程序化调用（测试）的参数对象可能没有这个属性。只有「没给」（属性不存在或为 None）才是缺省；
    # 空字符串是非法取值（入口脚本的正则同样拒绝），不能被 ``or`` 悄悄当成 off。
    requested = getattr(args, "declared_sample", None)
    try:
        sample = _declared_sample_mode("off" if requested is None else requested)
    except argparse.ArgumentTypeError as error:
        raise DryRunError(str(error)) from error
    request_sha256 = None
    if request is not None:
        if not Path(request).is_absolute() or Path(request).is_symlink() or not Path(request).is_file():
            raise DryRunError(f"逐单元承接请求必须是已有的普通文件（绝对路径）：{request}")
        request_sha256 = bv._sha256_file(Path(request))
    full_gates_sha256 = None
    if full_gates_request is not None:
        if not Path(full_gates_request).is_absolute() or Path(full_gates_request).is_symlink() or not Path(full_gates_request).is_file():
            raise DryRunError(f"full-gates 逐单元承接请求必须是已有的普通文件（绝对路径）：{full_gates_request}")
        full_gates_sha256 = bv._sha256_file(Path(full_gates_request))
    to = args.to or (OPENING_TO if args.opening else DEFAULT_TO)
    if to not in ALLOWED_TO:
        raise DryRunError(f"--to 只能到 {ALLOWED_TO[-1]}（VC-0 收口会发正式请求，演练根里不做）：{to}")
    path = result_path(args.runroot, args.commit, deployment, opening=args.opening, reexecute=args.reexecute, to=to, client=client,
                       unit_inherit=request_sha256, unit_inherit_full_gates=full_gates_sha256,
                       audit_policy=policy_reference["sha256"] if policy_reference else None,
                       full_set_work=work_reference["sha256"] if work_reference else None)
    if sample != "off":
        # 采样方式不同的空跑是不同的空跑：结论文件名带上它，不与同提交同部署的缺省空跑互相顶替（缺省 off 的文件名不变）。
        path = path.with_name(f"{path.stem}-declared-sample-{sample.replace(':', '-')}{path.suffix}")
    existing = _read(path)
    if existing is not None and effective_status(existing) in ("running", "passed", "failed"):
        return 0, {"action": "exists", "result": str(path), "status": effective_status(existing)}
    payload: dict[str, Any] = {
        "schema_version": SCHEMA, "commit": args.commit, "branch": args.branch, "bundle": str(args.bundle),
        "deployment": deployment, "opening": bool(args.opening), "reexecute": bool(args.reexecute), "to": to, "vc_env": str(args.vc_env),
        "data_root": str(args.data_root), "runroot": str(args.runroot), "entry": str(args.entry),
        "status": "running", "pid": None, "started_at_utc": _utc_now(), "completed_at_utc": None,
        "rehearsal_root": None, "log": None, "exit_code": None, "steps": [], "failed_steps": [], "reason": None,
        # B5a-10：非 Codex 的结论记客户端；带逐单元承接请求的空跑记请求坐标与摘要（Codex 不带请求时结论与改造前同形）。
        **({"client": client} if client != _sibling("client_table").DEFAULT_CLIENT else {}),
        **({"unit_inherit_request": {"path": str(request), "sha256": request_sha256}} if request is not None else {}),
        **({"unit_inherit_full_gates_request": {"path": str(full_gates_request), "sha256": full_gates_sha256}}
           if full_gates_request is not None else {}),
        **({"audit_policy": policy_reference} if policy_reference is not None else {}),
        **({"full_set_work": work_reference} if work_reference is not None else {}),
        # 步骤 5 WP3b：只有显式给了非 off 的采样方式才写，缺省结论与改造前同形。
        **({"declared_sample": sample} if sample != "off" else {}),
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


def _copy_pre_a3_source(payload: Mapping[str, Any], values: Mapping[str, str], rehearsal: Mapping[str, str], *, client: str) -> None:
    """接入 Claude 本轮已有的认证；旧记录的承接资格、Codex 与显式全集空跑语义不变。

    认证本身没有另外的时长期限，合法性和历史承接期限均由原 find-reusable 校验。复制成功也不代表入口通过：后续
    entry 仍核当前静态输入及记录，并把 record-reuse 收据写到演练认证旁，不能写入只读源认证目录。
    """

    if (client != "claude" or not payload.get("unit_inherit_request")
            or payload.get("opening") or payload.get("reexecute")):
        return
    location = values.get("PRE_A3_CERTIFICATION")
    if not location:
        return
    source = Path(location)
    if (not source.is_absolute() or ".." in source.parts
            or any(item.is_symlink() for item in (source, *source.parents))):
        raise DryRunError("源 pre-A3 认证必须是无符号链接的规范绝对路径")
    if not source.exists():
        return
    if not source.is_file():
        raise DryRunError("源 pre-A3 认证不是普通文件")
    if payload.get("client", "codex") != client:
        raise DryRunError("源 pre-A3 认证的本轮客户端与空跑启动绑定不一致")
    activation = values.get("POLICY_ACTIVATION")
    if not activation:
        raise DryRunError("已有源 pre-A3 认证却缺少本轮策略激活认证")
    data = Path(str(payload["data_root"]))
    def current_deployment() -> dict[str, Any]:
        # 部署在准备期间消失或损坏也须由 run 写 failed，不能只退出子进程而留下 running 结论。
        try:
            return _bv().latest_deployment(data, client)
        except _bv().ValidationError as error:
            raise DryRunError(f"源 pre-A3 认证所需的当前部署不可用：{error}") from error
    deployment = current_deployment()
    if deployment != payload.get("deployment"):
        raise DryRunError("源 pre-A3 认证核验前部署已变化，须按当前部署重新启动空跑")
    raw = source.read_bytes()
    spec = _sibling("client_table").spec(client)
    completed = subprocess.run(
        [sys.executable, "-B", "-m", spec["pre_a3_module"], "find-reusable", "--certification", str(source),
         "--deployment-receipt", str(data / "control" / deployment["receipt"]), "--policy-activation", activation,
         *spec["client_argv"]], cwd=str(data),
        env={**os.environ, "PYTHONPATH": str(data), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=300)
    if completed.returncode != 0 or completed.stdout.strip() != str(source):
        detail = (completed.stderr or completed.stdout)[-600:]
        raise DryRunError(f"源 pre-A3 认证未通过当前客户端的原验证器，拒绝降级重跑：{detail}")
    if (source.read_bytes() != raw or any(item.is_symlink() for item in (source, *source.parents))
            or current_deployment() != deployment):
        raise DryRunError("源 pre-A3 认证或部署在核验期间变化，拒绝复制未核准内容")
    destination = Path(rehearsal["PRE_A3_CERTIFICATION"])
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def _prepare(payload: Mapping[str, Any]) -> tuple[Path, Path, Path]:
    """建演练根、fixture_only 演练总账与演练参数文件，返回（演练根，空跑 RUNROOT，演练参数文件）。"""

    data_root = Path(payload["data_root"])
    (data_root / "staging").mkdir(mode=0o700, parents=True, exist_ok=True)
    # 演练根按秒取名、独占创建：同一秒里起了两次空跑（后台验证接着的与明确执行的）时，后起的那个等到下一秒再取名。
    for _attempt in range(30):
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
        root = data_root / "staging" / f"entry-dryrun-{stamp}"
        try:
            root.mkdir(mode=0o700)
            break
        except FileExistsError:
            time.sleep(0.2)
    else:
        raise DryRunError("演练根取不到不重名的时间戳")
    runroot = Path(payload["runroot"]) / DIRECTORY / f"run-{stamp}"
    for directory in (root / "evidence" / "campaigns", root / "control" / "policy-certification", runroot):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    deadline = (datetime.now(timezone.utc) + timedelta(hours=30)).strftime("%Y-%m-%dT%H:00:00Z")
    # 演练总账按本轮客户端建（客户端表：总账模块、目录名与 --client；Codex 与改造前相同）。参数文件解析不了时按缺省客户端
    # 建，错误照旧由下面建完总账之后的解析报出（与改造前同一顺序）。
    table = _sibling("client_table")
    try:
        client = table.client_of(_parse_env().parse(Path(payload["vc_env"]).read_text(encoding="utf-8")))
    except (OSError, ValueError, UnicodeDecodeError):
        client = table.DEFAULT_CLIENT
    spec = table.spec(client)
    completed = subprocess.run(
        [sys.executable, "-B", "-m", spec["project_ledger_module"], "create-project-ledger",
         "--ledger-dir", str(root / "evidence" / "campaigns" / spec["project_ledger_dir_name"]), "--project-id", f"entry-dryrun-{stamp}",
         "--absolute-deadline-utc", deadline, "--deadline-approved-by", APPROVED_BY, "--estimation-policy", "none",
         "--estimation-policy-approved-by", APPROVED_BY, "--fixture-only", *spec["client_argv"]],
        cwd=str(data_root), env={**os.environ, "PYTHONPATH": str(data_root), "PYTHONDONTWRITEBYTECODE": "1"},
        capture_output=True, text=True, timeout=300)
    if completed.returncode != 0:
        raise DryRunError(f"演练总账没建成：{(completed.stderr or completed.stdout)[-400:]}")
    values = _parse_env().parse(Path(payload["vc_env"]).read_text(encoding="utf-8"))
    env_values = rehearsal_env(values, root=root, runroot=runroot, stamp=stamp, deadline=deadline, bundle=payload["bundle"],
                               branch=payload["branch"], commit=payload["commit"])
    current_client = table.client_of(values)
    if current_client != client:
        raise DryRunError("参数文件客户端在演练根准备期间变化")
    _copy_pre_a3_source(payload, values, env_values, client=client)
    env_file = runroot / "env.sh"
    env_file.write_text(_env_text(env_values), encoding="utf-8")
    env_file.chmod(0o600)
    try:
        _sibling("entry_housekeeping").disk_policy(env_values)
    except _sibling("entry_housekeeping").HousekeepingError as error:
        raise DryRunError(str(error)) from error
    return root, runroot, env_file


def _housekeeping(path: Path, payload: Mapping[str, Any]) -> dict[str, Any]:
    """开跑前的日常维护（见模块说明），返回报告；全文另写到结论旁。根盘停线的操作员阈值取本轮参数文件的 MIN_FREE_GIB。"""

    keeper = _sibling("entry_housekeeping")
    minimum = keeper.MIN_FREE_GIB
    values: dict[str, str] = {}
    env_text = ""
    try:
        env_text = Path(payload["vc_env"]).read_text(encoding="utf-8")
        values = _parse_env().parse(env_text)
        minimum = float(values.get("MIN_FREE_GIB") or minimum)
    except (OSError, ValueError, TypeError, DryRunError) as error:
        if re.search(r"^ENGINEERING_ROOT_MAX_USED_PERCENT=", env_text, re.MULTILINE):
            raise keeper.HousekeepingError(f"工程磁盘参数拒绝：{error}") from error
    busy = set()
    for _other, current in results(Path(payload["runroot"])):
        if effective_status(current) == "running":
            for key in ("rehearsal_root", "dryrun_runroot"):
                matched = re.search(r"(\d{8}t\d{6}z)$", str(current.get(key) or ""))
                if matched:
                    busy.add(matched.group(1))
    report = keeper.run(data_root=Path(payload["data_root"]), runroot=Path(payload["runroot"]), min_free_gib=minimum,
                        busy_dryruns=busy, environment=values)
    _write_plain(path.with_name(f"{path.stem}.housekeeping.json"), report)
    return report


def _housekeeping_summary(report: Mapping[str, Any]) -> dict[str, Any]:
    go, store, dryruns, superseded = (report.get(key) or {} for key in ("go_cache", "record_store", "dryruns", "superseded_staging"))
    counts = store.get("counts") or {}
    return {
        "go_cache": {key: go.get(key) for key in ("status", "removed_files", "removed_bytes", "error") if key in go},
        "record_store": {"status": store.get("status"), "error": store.get("error"), "referenced_runs": store.get("referenced_runs"),
                         "removed": {kind: row.get("removed") for kind, row in counts.items()},
                         "removed_bytes": sum(int(row.get("removed_bytes") or 0) for row in counts.values())},
        "dryruns": {"status": dryruns.get("status"), "removed": len(dryruns.get("removed") or []), "error": dryruns.get("error")},
        "superseded_staging": {"status": superseded.get("status"), "removed": len(superseded.get("removed") or []),
                               "removed_bytes": superseded.get("removed_bytes"), "error": superseded.get("error")},
        "disk": report.get("disk"),
    }


def _write_plain(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=1) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _orchestrator_run(runroot: Path) -> dict[str, Any] | None:
    runs = sorted((runroot / "entry-runs").glob("*/run.json"))
    if not runs:
        return None
    with contextlib.suppress(OSError, ValueError):
        return json.loads(runs[-1].read_text(encoding="utf-8"))
    return None


def _update(path: Path, **fields: Any) -> dict[str, Any] | None:
    """结论还是 running 才写入（被 stop 标为 superseded 的不再改写），返回写入后的结论；不是 running 返回 None。"""

    current = _read(path)
    if current is None or current.get("status") != "running":
        return None
    updated = {**current, **fields}
    _write(path, updated)
    return updated


def _serial_lock_path(payload: Mapping[str, Any]) -> Path:
    """空跑串行锁的位置：与 entry-gates.sh 缺省的入口门禁工作目录（数据根上一级的 entry-gates-work）同一处。"""

    return Path(str(payload["data_root"])).parent / "entry-gates-work" / SERIAL_LOCK_NAME


def _acquire_serial_lock(lock_path: Path, still_running: Callable[[], bool], *, timeout: float, poll: float) -> tuple[int, float] | None:
    """取空跑串行锁（见模块说明「排队」）。

    拿到返回（文件描述符，排队秒数），调用方跑完编排器后关闭描述符即释放（进程退出也会释放）；排队期间 ``still_running()``
    为假（结论被 stop 标为 superseded）返回 None；超过 ``timeout`` 秒抛 DryRunError。
    """

    lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    started = time.monotonic()
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return fd, time.monotonic() - started
            except BlockingIOError:
                pass
            if not still_running():
                os.close(fd)
                return None
            if time.monotonic() - started >= timeout:
                raise DryRunError(f"等前一份空跑释放入口门禁工作目录超过 {int(timeout)} 秒：{lock_path}")
            time.sleep(poll)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        raise


def _run_orchestrator(payload: Mapping[str, Any], runroot: Path, env_file: Path) -> tuple[int, Path]:
    """在干净环境里跑入口编排器（``env -i``，按空跑种类带参数），返回（退出码，日志路径）。调用方持有空跑串行锁。"""

    argv = ["env", "-i", "HOME=/root", "LANG=C.UTF-8", f"PATH={CLEAN_PATH}", f"ARM64_VC_ENV={env_file}",
            "bash", str(payload["entry"]), "--to", payload["to"]]
    if payload.get("opening"):
        argv += ["--reexecute-gates", "--audit-reads"]
    elif payload.get("reexecute"):
        argv += ["--reexecute-gates"]
    elif payload.get("unit_inherit_request"):
        argv += ["--unit-inherit-request", str(payload["unit_inherit_request"]["path"])]
        # 取锁后重核每份显式批准文件；单主请求与双请求适用同一条启动绑定约束。
        for name in ("unit_inherit_request", "unit_inherit_full_gates_request"):
            if payload.get(name):
                reference = payload[name]
                request = Path(reference["path"])
                if (request.is_symlink() or not request.is_file()
                        or _bv()._sha256_file(request) != reference["sha256"]):
                    raise DryRunError(f"逐单元承接请求在空跑启动后变化：{request}")
        if payload.get("unit_inherit_full_gates_request"):
            argv += ["--unit-inherit-full-gates-request", str(payload["unit_inherit_full_gates_request"]["path"])]
    policy = _verify_audit_policy(payload)
    if policy is not None:
        if not payload.get("unit_inherit_request") and not payload.get("opening"):
            raise DryRunError("登记审计策略的空跑缺少逐单元承接请求或显式开工模式")
        if not payload.get("opening"):
            argv += ["--audit-reads"]
        argv += ["--audit-strict", "--audit-policy", str(policy)]
    work = _verify_full_set_work(payload)
    if work is not None:
        argv += ["--full-set-work", str(work)]
    if payload.get("declared_sample"):
        # 声明级承接的采样方式（步骤 5 WP3b）：结论文件是磁盘上的 JSON，派发前再核一遍取值，不把载荷里的字符串原样拼进命令行。
        try:
            argv += ["--declared-sample", _declared_sample_mode(payload["declared_sample"])]
        except argparse.ArgumentTypeError as error:
            raise DryRunError(f"空跑结论里的采样方式非法：{error}") from error
    log = runroot / "entry.out"
    with log.open("ab") as handle:
        returncode = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=handle, stderr=subprocess.STDOUT).returncode
    return returncode, log


def run(result: Path) -> int:
    path = Path(result)
    payload = _read(path)
    if payload is None:
        raise DryRunError(f"空跑结论文件不可读：{path}")
    if os.environ.get("ENTRY_DRYRUN_HOUSEKEEPING") == "off":
        report: dict[str, Any] = {"status": "skipped", "reason": "ENTRY_DRYRUN_HOUSEKEEPING=off"}
    else:
        try:
            report = _housekeeping(path, payload)
        except _sibling("entry_housekeeping").HousekeepingError as error:
            _update(path, status="failed", completed_at_utc=_utc_now(), failed_steps=["disk-policy"], reason=str(error))
            return 0
        except Exception as error:   # 维护本身出错不挡空跑，记下来
            report = {"status": "error", "error": f"{type(error).__name__}: {error}"}
    disk = report.get("disk") or {}
    payload = _update(path, housekeeping=_housekeeping_summary(report) if disk else report)
    if payload is None:
        return 0   # 已被 stop 标为 superseded
    if disk and not disk.get("ok"):
        _update(path, status="failed", completed_at_utc=_utc_now(), failed_steps=["disk"],
                reason=_sibling("entry_housekeeping").disk_reason(disk))
        return 0
    try:
        root, runroot, env_file = _prepare(payload)
    except (DryRunError, OSError, subprocess.SubprocessError) as error:
        _update(path, status="failed", completed_at_utc=_utc_now(), reason=f"演练根没准备好：{error}")
        return 0
    payload = _update(path, rehearsal_root=str(root), dryrun_runroot=str(runroot), env_file=str(env_file))
    if payload is None:
        return 0
    try:
        held = _acquire_serial_lock(_serial_lock_path(payload), lambda: (_read(path) or {}).get("status") == "running",
                                    timeout=SERIAL_LOCK_TIMEOUT, poll=SERIAL_LOCK_POLL)
    except (DryRunError, OSError) as error:
        _update(path, status="failed", completed_at_utc=_utc_now(), reason=f"空跑排队没拿到串行锁：{error}")
        return 0
    if held is None:
        return 0   # 排队期间已被 stop 标为 superseded
    lock_fd, waited = held
    try:
        _verify_full_set_work(payload)
        if waited >= 1:
            payload = _update(path, serial_lock_waited_seconds=int(waited))
            if payload is None:
                return 0
        returncode, log = _run_orchestrator(payload, runroot, env_file)
        _verify_audit_policy(payload)
        _verify_full_set_work(payload)
    except DryRunError as error:
        _update(path, status="failed", completed_at_utc=_utc_now(), failed_steps=["entry-orchestrator"], reason=str(error))
        return 0
    finally:
        os.close(lock_fd)
    summary = _orchestrator_run(runroot) or {}
    steps = [{"step_id": step.get("step_id"), "action": step.get("action"), "status": step.get("status"),
              "reasons": list(step.get("reasons") or [])[:4]} for step in summary.get("steps") or []]
    failed = [step for step in steps if step["status"] == "failed" or step["action"] in ("阻塞", "被阻塞")]
    status = "passed" if returncode == 0 and not failed and summary else "failed"
    reason = None if status == "passed" else (summary.get("stopped_because") or f"入口编排器退出码 {returncode}，日志 {log}")
    extra: dict[str, Any] = {}
    client = payload.get("client")
    if client:
        # B5a-10：非 Codex 的结论另带没通过各步的分类（见模块说明；Codex 的结论与改造前同形）。编排器没有给出没通过的步骤
        # 却没有通过（编排器本身出错、没有运行汇总）时记一项真实失败 entry-orchestrator。
        classes = classify_failures(client, failed, root)
        if status == "failed" and not classes:
            classes = {"entry-orchestrator": {"class": "real", "reasons": [str(reason)]}}
        extra = {"failure_classes": classes, "real_failed_steps": [step for step, item in classes.items() if item["class"] == "real"]}
    _update(path, status=status, completed_at_utc=_utc_now(), exit_code=returncode, log=str(log), steps=steps,
            failed_steps=[step["step_id"] for step in failed], reason=reason, **extra)
    return 0


# ---------------------------------------------------------------- 没通过各步的分类（B5a-10，非 Codex 的结论）

def _scenario_verdict(result: Mapping[str, Any]) -> tuple[str, str]:
    """一个 pre-A3 场景结果：通过、占位（链体登记了待落实的工作项并停在占位上）或真实失败，另给原因。"""

    scenario = result.get("scenario") if isinstance(result.get("scenario"), Mapping) else {}
    metrics = scenario.get("metrics") if isinstance(scenario.get("metrics"), Mapping) else {}
    attempts = result.get("network_attempts")
    if scenario.get("status") == "passed" and attempts == 0:
        return "passed", ""
    problems = []
    pending = metrics.get("pending")
    if not isinstance(pending, str) or not pending.strip():
        problems.append("链体没有登记占位（指标 pending）")
    elif pending not in str(scenario.get("error") or ""):
        problems.append("场景错误信息不是登记的占位")
    if attempts != 0 or metrics.get("upstream_requests", 0) != 0:
        problems.append(f"请求或网络计数非零（场景 {attempts!r}，链体 {metrics.get('upstream_requests')!r}）")
    if "isolation" in metrics and (metrics.get("isolation") != "namespace" or metrics.get("network_isolated") is not True):
        problems.append(f"隔离降级（{metrics.get('isolation')}，network_isolated={metrics.get('network_isolated')}）")
    if "returncode" in metrics and metrics.get("returncode") in (None, 0):
        problems.append(f"链子进程退出码 {metrics.get('returncode')!r}（超时，或没失败却判失败）")
    return ("pending", str(pending)) if not problems else ("real", "；".join(problems))


def classify_pre_a3(certification: Path) -> dict[str, Any]:
    """pre-A3 一步没通过时逐场景分类：读认证（没通过时是同目录最新的旁路文件 ``<名>.failed-<UTC>.json``）登记的记录库与运行
    清单，逐个 pre-A3 单元取执行记录与日志里那一行场景结果。返回 ``{status, certification, scenarios, pending, real}``；读不到时
    ``status`` 为 ``unavailable`` 并写明原因（按真实失败处理）。"""

    certification = Path(certification)
    candidates = [certification] if certification.is_file() else sorted(
        certification.parent.glob(f"{certification.stem}.failed-*{certification.suffix}"))
    if not candidates:
        return {"status": "unavailable", "reason": f"找不到 pre-A3 认证或旁路文件：{certification}", "pending": [], "real": []}
    source = candidates[-1]
    records = _sibling("unit_records")
    try:
        receipt = json.loads(source.read_text(encoding="utf-8"))
        store = records.RecordStore(Path(str(receipt["record_store"])))
        manifest = records._read_json(Path(str(receipt["unit_manifest"]["path"])))
    except (OSError, ValueError, KeyError, TypeError) as error:
        return {"status": "unavailable", "certification": str(source), "reason": f"认证没有登记可读的记录库与运行清单：{error}",
                "pending": [], "real": []}
    if not isinstance(manifest, Mapping):
        return {"status": "unavailable", "certification": str(source), "reason": "认证登记的运行清单读不到", "pending": [], "real": []}
    scenarios: list[dict[str, Any]] = []
    for entry in manifest.get("units") or []:
        unit_id = str((entry or {}).get("unit_id") or "") if isinstance(entry, Mapping) else ""
        if not unit_id.startswith(PRE_A3_UNIT_PREFIX):
            continue
        verdict, why = "real", "执行记录不在记录库里"
        record = records._read_json(store.record_path(unit_id, str(entry.get("record_sha256") or "")))
        if isinstance(record, Mapping):
            log = store.log_path(str((record.get("log") or {}).get("sha256") or ""))
            try:
                lines = [line for line in log.read_text(encoding="utf-8", errors="replace").splitlines()
                         if line.startswith(PRE_A3_RESULT_PREFIX)]
            except OSError:
                lines = []
            verdict, why = "real", f"日志里的场景结果行不是恰好一行（{len(lines)} 行）"
            if len(lines) == 1:
                try:
                    verdict, why = _scenario_verdict(json.loads(lines[0][len(PRE_A3_RESULT_PREFIX):]))
                except ValueError:
                    verdict, why = "real", "场景结果行不是 JSON"
        scenarios.append({"unit_id": unit_id, "verdict": verdict, "why": why[:300]})
    return {"status": "classified", "certification": str(source), "scenarios": scenarios,
            "pending": [item["unit_id"] for item in scenarios if item["verdict"] == "pending"],
            "real": [item["unit_id"] for item in scenarios if item["verdict"] == "real"]}


def classify_failures(client: str, failed: list[Mapping[str, Any]], root: Path) -> dict[str, dict[str, Any]]:
    """没通过各步的分类（见模块说明）：``registered_pending``／``scenario_pending``／``blocked``／``real``。"""

    table = _sibling("client_table")
    classes: dict[str, dict[str, Any]] = {}
    for step in failed:
        step_id = str(step.get("step_id"))
        waiting = table.pending(client, f"step:{step_id}")
        if waiting:
            classes[step_id] = {"class": "registered_pending", "work_item": waiting}
        elif step.get("action") in ("阻塞", "被阻塞"):
            classes[step_id] = {"class": "blocked", "reasons": list(step.get("reasons") or [])}
        elif step_id == "pre-a3":
            detail = classify_pre_a3(Path(root) / "control" / "policy-certification" / PRE_A3_CERTIFICATION_NAME)
            only_pending = detail["status"] == "classified" and bool(detail["pending"]) and not detail["real"]
            classes[step_id] = {"class": "scenario_pending" if only_pending else "real", "detail": detail}
        else:
            classes[step_id] = {"class": "real", "reasons": list(step.get("reasons") or [])}
    return classes


def _declared_sample_mode(value: str) -> str:
    """``--declared-sample`` 的取值：``off``（缺省）、``always`` 或 ``every:N``（N 为正整数）；非法在最外层就拒绝。
    与执行器 ``unit_executor.declared_sample_mode`` 以及入口脚本 ``entry-gates.sh`` 的正则 ``^(off|always|every:[1-9][0-9]*)$`` 同一规则（测试核对
    各层一致），免得空跑放过一个执行器才报错的值：**不去首尾空白**、不认换行、前导零与大小写变体、不是字符串一律拒绝；合法的取值就是规范写法，原样返回。"""

    if isinstance(value, str):
        if value in ("off", "always") or re.fullmatch(r"every:[1-9][0-9]*", value):
            return value
    raise argparse.ArgumentTypeError(f"--declared-sample 只接受 off、always 或 every:N（N 为正整数）：{value!r}")


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
    p_start.add_argument("--reexecute", action="store_true", help="只重新执行全集、不带审计（冷跑计时用）")
    p_start.add_argument("--unit-inherit-request", type=Path, default=None,
                         help="B5a-08 逐单元承接请求（交给入口编排器；与 --opening／--reexecute 互斥）")
    p_start.add_argument("--unit-inherit-full-gates-request", type=Path, default=None,
                         help="独立批准的 full-gates 请求；与主请求从首轮一起固定提供，两份摘要共同绑定空跑结论")
    p_start.add_argument("--full-set-work", type=Path, default=None,
                         help="逐单元承接来源后台工作区；只随主请求使用，复核原树、缓存和 bundle 并绑定结论")
    p_start.add_argument("--audit-policy", type=Path, default=None,
                         help="精确读集登记策略；与逐单元承接请求或显式开工模式一起使用，按摘要绑定并复核")
    p_start.add_argument("--declared-sample", type=_declared_sample_mode, default="off", metavar="off|always|every:N",
                         help="声明级承接命令单元的读集证据来源：off（缺省）用登记表证据，always 每次执行都带 strace 采样，every:N 每 N 次采样一次；"
                              "原样交给入口编排器，非 off 的取值绑定结论（结论文件名另带取值）")
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
