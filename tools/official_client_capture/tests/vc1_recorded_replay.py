"""R18 ②③：0.156.1 录制官方证据的零请求回放基座（测试夹具，不进受管身份）。

用途：在隔离的副本受管树里，用录制的 0.156.1 官方 Job 证据字节代替真实抓包，真跑 VC-0 收口建 Formal
Campaign、VC-1 首批与恢复、权限收口、断言包、seal 与 VC-2 分类草案，全程零真实请求。

边界（依据 R18 调研结论）：

* 录制字节里固化了 run_id（由 campaign_id 派生）与模型收据 evidence_root 的绝对路径，所以回放 Campaign
  沿用录制的 campaign_id，证据根仍落在 ``/root/oauth-capture/runs``；
* 全部进程运行在私有挂载命名空间：副本仓库根绑定到 ``/root/oauth-capture``，副本 runs 绑定到宿主数据根
  ``runs``（``_archive_failed_job_evidence`` 硬编码了该宿主路径），宿主数据根其余部分重新挂载为只读，
  任何越界写入都会失败；链前后另对生产录制目录做 stat 哨兵比对；
* 替身经副本根 ``sitecustomize.py``（环境变量开关）安装：Job 步骤命令换成复制录制证据的小脚本后仍交给
  真实 ``SupervisorClient.run_command`` 执行（事件、超时、日志、重试归档都是真的）；ARM64 事实采集在
  依赖模块层替换；官方二进制校验与环境探针定义在编排器内部，检测到受管编排器脚本入口时以包模块接管
  main 后替换。其余全部真跑。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

RECORDED_CAMPAIGN_ID = "c01561-formal-vc1-r1-20260925t043158z"
HOST_DATA_ROOT = Path("/root/docker/capture-cli/data")
CAPTURE_ROOT = Path("/root/oauth-capture")
TARGET_VERSION = "0.156.1"
BASELINE_VERSION = "0.154.0"
# 录制 Campaign 位置可由环境变量覆盖（默认 ARM64 宿主数据根下的原位置，只读复制）。
RECORDED_CAMPAIGN_ENV = "CODEX_VC1_RECORDED_CAMPAIGN_DIR"
# 回放状态文件：存在即激活 sitecustomize 替身；内容见 ``write_state``。
REPLAY_STATE_ENV = "CODEX_VC1_REPLAY_STATE"
GUARDIAN_JOB_ID = "official-relay-guardian-review"
SNAPSHOT_DIRNAME = "recorded"
SITECUSTOMIZE_SOURCE = (
    '"""R18 回放链：副本树启动钩子，只在回放状态环境变量存在时安装替身（测试夹具，不进受管身份）。"""\n'
    "import os\n"
    f'if os.environ.get("{REPLAY_STATE_ENV}"):\n'
    "    from tools.official_client_capture.tests import vc1_recorded_replay as _vc1_replay\n"
    "    _vc1_replay.activate()\n"
)


class ReplayError(RuntimeError):
    pass


def scratch_root() -> Path:
    """回放链的临时根：必须在宿主数据根与 /root/oauth-capture 之外（命名空间内宿主数据根只读；pre-A3 生产布局下
    默认临时目录可能落在数据根的 staging 里）。Linux 固定用 /var/tmp。"""

    return Path("/var/tmp") if sys.platform.startswith("linux") else Path(tempfile.gettempdir()).resolve()


def recorded_campaign_dir() -> Path:
    return Path(os.environ.get(RECORDED_CAMPAIGN_ENV) or HOST_DATA_ROOT / "evidence" / "campaigns" / RECORDED_CAMPAIGN_ID)


def available() -> bool:
    """回放链只在具备录制数据、root 与 unshare 的 Linux 上运行；否则调用方如实 skip。"""

    try:
        present = (recorded_campaign_dir() / "campaign.json").is_file()
    except PermissionError:
        return False
    return (
        present
        and sys.platform.startswith("linux")
        and os.geteuid() == 0
        and shutil.which("unshare") is not None
    )


def _read(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write(path: Path, payload: Any) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _runs_relative(logical: str) -> str:
    """录制结果里的逻辑证据根 → 相对 runs 根的规范路径（拒绝越界）。"""

    relative = PurePosixPath(logical).relative_to(PurePosixPath(str(CAPTURE_ROOT / "runs")))
    if not relative.parts or any(part in {"", ".", ".."} for part in relative.parts):
        raise ReplayError(f"录制证据根不在 runs 下：{logical}")
    return relative.as_posix()


# ---------------------------------------------------------------------------
# 录制快照（只读复制）与生产哨兵
# ---------------------------------------------------------------------------


def _recorded_attempts(recorded: Path) -> list[tuple[Path, dict[str, Any]]]:
    rows = []
    for attempt_root in sorted((recorded / "official" / "attempts").iterdir()):
        path = attempt_root / "attempt.json"
        if attempt_root.is_dir() and path.is_file():
            rows.append((attempt_root, _read(path)))
    return rows


def build_snapshot(staging: Path) -> dict[str, Any]:
    """把录制 Campaign 的官方证据只读复制进 ``staging/recorded``，返回索引（也写入 index.json）。

    成功证据取最后一次 awaiting_receipts attempt 的全部结果（30 个承接自首次、guardian 为补跑成功）；
    guardian 的失败归档（``.failed-attempt<N>``）另存，供恢复链回放失败重试；官方二进制校验收据取首个 attempt。
    """

    recorded = recorded_campaign_dir()
    attempts = _recorded_attempts(recorded)
    final = [row for row in attempts if row[1].get("status") == "awaiting_receipts"]
    if len(final) != 1:
        raise ReplayError(f"录制 Campaign 应恰好有一个 awaiting_receipts attempt：{[r[0].name for r in attempts]}")
    final_root, final_attempt = final[0]
    jobs: dict[str, list[str]] = {}
    for result in final_attempt["results"]:
        if result.get("status") != "complete":
            raise ReplayError(f"录制最终 attempt 的作业未完成：{result.get('id')}")
        jobs[str(result["id"])] = [_runs_relative(str(root)) for root in result["evidence_roots"]]
    guardian_roots = jobs.get(GUARDIAN_JOB_ID)
    if not guardian_roots or len(guardian_roots) != 1:
        raise ReplayError("录制 guardian 作业证据根不唯一")
    guardian_failures = sorted(
        path.name for path in (HOST_DATA_ROOT / "runs").glob(PurePosixPath(guardian_roots[0]).name + ".failed-attempt*")
        if path.is_dir()
    )
    snapshot = Path(staging) / SNAPSHOT_DIRNAME
    if snapshot.exists():
        raise ReplayError(f"快照目录已存在：{snapshot}")
    runs = snapshot / "runs"
    runs.mkdir(parents=True, mode=0o700)
    relatives = sorted({item for roots in jobs.values() for item in roots} | set(guardian_failures))
    for relative in relatives:
        source = HOST_DATA_ROOT / "runs" / relative
        if source.is_symlink() or not source.is_dir():
            raise ReplayError(f"录制证据根缺失或不可信：{source}")
        destination = runs / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
    first_root = attempts[0][0]
    verification = snapshot / "official-binary-verification.json"
    shutil.copy2(first_root / "official-binary-verification.json", verification)
    index = {
        "recorded_campaign_id": RECORDED_CAMPAIGN_ID,
        "recorded_campaign_dir": str(recorded),
        "final_attempt_id": final_root.name,
        "jobs": jobs,
        "guardian_failures": guardian_failures,
        "binary_verification": str(verification),
        "runs": str(runs),
    }
    _write(snapshot / "index.json", index)
    return index


def production_sentinel(index: Mapping[str, Any]) -> dict[str, list[Any]]:
    """生产录制目录的 stat 哨兵（路径、类型、大小、mode、mtime、ctime）：链前后必须逐项相等。"""

    roots = [Path(str(index["recorded_campaign_dir"]))]
    roots += [HOST_DATA_ROOT / "runs" / item for roots_ in index["jobs"].values() for item in roots_]
    roots += [HOST_DATA_ROOT / "runs" / item for item in index["guardian_failures"]]
    entries: dict[str, list[Any]] = {}
    for root in sorted(set(roots)):
        for path in [root, *sorted(root.rglob("*"))]:
            metadata = path.lstat()
            entries[str(path)] = [metadata.st_mode, metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns]
    return entries


# ---------------------------------------------------------------------------
# 私有挂载命名空间
# ---------------------------------------------------------------------------

NAMESPACE_SCRIPT = (
    "set -eu\n"
    f'data="{HOST_DATA_ROOT}"\n'
    f'capture="{CAPTURE_ROOT}"\n'
    'mount --bind "$data" "$data"\n'
    'mount -o remount,bind,ro "$data"\n'
    'mount --bind "$0" "$capture"\n'
    'mount --bind "$0/runs" "$data/runs"\n'
    'exec "$@"\n'
)


def namespace_argv(tree_root: Path, argv: Sequence[str]) -> list[str]:
    """在私有挂载＋网络命名空间里执行 ``argv``：副本根→/root/oauth-capture，副本 runs→宿主 runs，宿主数据根只读；
    新网络命名空间没有任何外网接口，全程零真实请求由构造保证。"""

    return ["unshare", "-m", "-n", "--propagation", "private", "sh", "-c", NAMESPACE_SCRIPT, str(Path(tree_root)), *argv]


def assert_network_isolated() -> None:
    """命名空间内自检：当前网络命名空间只有回环接口（if_nameindex 查的是本进程所在命名空间；
    /sys/class/net 反映的是 sysfs 挂载时的命名空间，不能用来判断）。"""

    import socket

    interfaces = sorted(name for _index, name in socket.if_nameindex())
    if interfaces != ["lo"]:
        raise ReplayError(f"命名空间仍有外网接口：{interfaces}")


def assert_namespace(tree_root: Path) -> None:
    """命名空间内自检：两处 runs 指向副本、宿主数据根只读（写入即 EROFS）。"""

    tree_runs = (Path(tree_root) / "runs").stat()
    for path in (CAPTURE_ROOT / "runs", HOST_DATA_ROOT / "runs"):
        metadata = path.stat()
        if (metadata.st_dev, metadata.st_ino) != (tree_runs.st_dev, tree_runs.st_ino):
            raise ReplayError(f"命名空间绑定失效：{path} 未指向副本 runs")
    probe = HOST_DATA_ROOT / f".r18-namespace-probe-{os.getpid()}"
    try:
        probe.write_text("x", encoding="utf-8")
    except OSError:
        return
    probe.unlink()
    raise ReplayError("命名空间内宿主数据根仍可写，拒绝继续")


def install_sitecustomize(tree_root: Path) -> Path:
    path = Path(tree_root) / "sitecustomize.py"
    path.write_text(SITECUSTOMIZE_SOURCE, encoding="utf-8")
    path.chmod(0o600)
    return path


# ---------------------------------------------------------------------------
# 回放状态与替身
# ---------------------------------------------------------------------------


def write_state(path: Path, index: Mapping[str, Any], **settings: Any) -> Path:
    """写回放状态：``guardian_plan`` 为 guardian 各次执行的结果序列（success／fail），``stall_job``
    为进入步骤前一直阻塞到被父监督器截止清理的作业（制造首批超时），``counter_dir`` 记录已消耗的执行次数。"""

    state = {
        "index": dict(index),
        "guardian_plan": list(settings.get("guardian_plan", ["success"])),
        "stall_job": settings.get("stall_job"),
        "job_retry_delay_seconds": settings.get("job_retry_delay_seconds"),
        "counter_dir": str(settings["counter_dir"]),
    }
    Path(state["counter_dir"]).mkdir(parents=True, exist_ok=True, mode=0o700)
    return _write(path, state)


def update_state(path: Path, **settings: Any) -> Path:
    state = _read(path)
    state.update(settings)
    return _write(path, state)


def _load_state() -> dict[str, Any]:
    return _read(Path(os.environ[REPLAY_STATE_ENV]))


# Job 步骤回放脚本（``python3 -S -c``：不加载 site，只用标准库）。第 1 步把本作业的录制证据根逐字节复制到
# 逻辑证据根（命名空间内即副本 runs），其余步骤直接成功；guardian 按计划序列决定成败。
STEP_SCRIPT = r'''
import json, os, shutil, sys, time
state = json.load(open(sys.argv[1], encoding="utf-8"))
operation = sys.argv[2]
parts = operation.split(":")
job_id, step = parts[1], parts[2]
index = state["index"]
if step != "step-1":
    print("录制回放：" + job_id + " " + step + " 无动作", flush=True)
    raise SystemExit(0)
counter_dir = state["counter_dir"]
count_path = os.path.join(counter_dir, job_id + ".count")
count = int(open(count_path).read()) if os.path.exists(count_path) else 0
with open(count_path, "w") as handle:
    handle.write(str(count + 1))
runs = index["runs"]
roots = index["jobs"][job_id]
outcome = "success"
sources = [os.path.join(runs, item) for item in roots]
if job_id == state.get("guardian_job", "official-relay-guardian-review"):
    plan = state.get("guardian_plan") or ["success"]
    outcome = plan[min(count, len(plan) - 1)]
    if outcome == "fail":
        failures = index["guardian_failures"]
        sources = [os.path.join(runs, failures[min(count, len(failures) - 1)])]
for source, relative in zip(sources, roots):
    destination = os.path.join(state.get("capture_runs", "/root/oauth-capture/runs"), relative)
    if os.path.lexists(destination):
        print("录制回放：目标证据根已存在 " + destination, file=sys.stderr, flush=True)
        raise SystemExit(70)
    os.makedirs(os.path.dirname(destination), mode=0o700, exist_ok=True)
    shutil.copytree(source, destination, symlinks=True, copy_function=shutil.copy2)
print("录制回放：" + job_id + " 第 " + str(count + 1) + " 次执行，复制 " + str(len(sources)) + " 个证据根，结果 " + outcome, flush=True)
raise SystemExit(0 if outcome == "success" else 1)
'''


def _install_module_patches(state_path: str) -> None:
    """依赖模块层替身：Job 步骤命令回放、ARM64 事实采集。先于编排器导入安装，from-import 也拿到替身。"""

    from tools.official_client_capture import codex_upgrade_arm64_environment_receipt as arm
    from tools.official_client_capture import codex_upgrade_supervisor as supervisor

    client = supervisor.SupervisorClient
    if getattr(client.run_command, "_r18_replay", False):
        return
    original = client.run_command

    def run_command(self: Any, argv: Sequence[str], *, operation: str, **kwargs: Any) -> Any:
        if isinstance(operation, str) and operation.startswith("job:"):
            argv = [sys.executable, "-S", "-c", STEP_SCRIPT, state_path, operation]
        return original(self, argv, operation=operation, **kwargs)

    run_command._r18_replay = True  # type: ignore[attr-defined]
    client.run_command = run_command  # type: ignore[method-assign]

    def collect_facts(*, phase: str, subject_id: str, rust_tls_codex_version: str, **_kwargs: Any) -> dict[str, Any]:
        # 与 VC-0 收据同一合成事实构造器、同一连续性种子：前后收据的连续性身份一致（零网络）。
        from tools.official_client_capture.tests import control_receipt_fixtures as crf

        with tempfile.TemporaryDirectory(prefix="r18-arm-") as directory:
            root = Path(directory)
            crf.create_arm_receipt(root, phase=phase, subject_id=subject_id, prefix="replay",
                                   rust_tls_codex_version=rust_tls_codex_version)
            return _read(root / "replay-facts.json")

    arm.collect_facts = collect_facts  # type: ignore[assignment]


def install_orchestrator_patches(codex_upgrade: Any, state: Mapping[str, Any]) -> None:
    """编排器内部替身：官方二进制校验返回录制收据；环境探针写五份规范化快照（与候选侧替身同形）。"""

    verification = Path(str(state["index"]["binary_verification"]))

    def verify_official_binaries(_manifest: Any, **_kwargs: Any) -> dict[str, Any]:
        return _read(verification)

    def probe(_manifest: Any, target: Path, phase: str, **_kwargs: Any) -> dict[str, Any]:
        from tools.official_client_capture import codex_upgrade_environment_probe as probe_module
        from tools.official_client_capture.tests import test_codex_upgrade

        fixture = test_codex_upgrade.CodexUpgradeTest
        target = Path(target)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        snapshots = []
        for key, filename in probe_module.STATE_FILES.items():
            payload = fixture._database_state(after=True) if key == "database" else {"probe_kind": f"recorded_replay_{key}", "stable_value": "restored"}
            fixture._write_state_snapshot(target / filename, payload)
            snapshots.append(probe_module._snapshot_binding(target / filename, (target / filename).read_bytes(), key))
        document = {"schema_version": "codex-upgrade-environment-probe/v1", "phase": phase,
                    "observed_at_utc": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "snapshots": snapshots}
        _write(target / "probe-manifest.json", document)
        return document

    codex_upgrade._verify_official_binaries = verify_official_binaries
    codex_upgrade._probe_capture_environment = probe
    if state.get("job_retry_delay_seconds") is not None:
        # 作业重试间隔（生产 30 秒）在回放里缩短；重试次数与归档逻辑不变。
        codex_upgrade.JOB_RETRY_DELAY_SECONDS = float(state["job_retry_delay_seconds"])
    stall_job = state.get("stall_job")
    if stall_job:
        # 首批超时（D1）：指定作业在进入任何步骤之前阻塞（对应真实现场“前置依赖计算空档里收到清理信号”，
        # 没有日志、没有请求），直到父监督器按动作截止发清理信号。
        original_run_job = codex_upgrade.run_job

        def run_job(job: Any, *args: Any, **kwargs: Any) -> Any:
            if getattr(job, "job_id", None) == stall_job:
                while True:
                    time.sleep(1)
            return original_run_job(job, *args, **kwargs)

        codex_upgrade.run_job = run_job


def _orchestrator_arguments() -> list[str] | None:
    """若本进程是 ``python3 [选项] <副本>/codex_upgrade.py …`` 的脚本入口，返回其参数；否则 None。"""

    try:
        raw = Path("/proc/self/cmdline").read_bytes().split(b"\0")
    except OSError:
        return None
    arguments = [item.decode("utf-8", "surrogateescape") for item in raw if item]
    managed = Path(__file__).resolve().parents[1] / "codex_upgrade.py"
    for position, item in enumerate(arguments[1:], 1):
        if item in {"-c", "-m"}:
            return None
        if item.startswith("-"):
            continue
        try:
            same = Path(item).resolve() == managed
        except OSError:
            same = False
        return arguments[position + 1:] if same else None
    return None


def activate() -> None:
    """sitecustomize 入口：安装依赖模块层替身；受管编排器脚本入口改以包模块运行并装编排器内部替身。"""

    state_path = os.environ[REPLAY_STATE_ENV]
    _install_module_patches(state_path)
    arguments = _orchestrator_arguments()
    if arguments is None:
        return
    from tools.official_client_capture import codex_upgrade

    install_orchestrator_patches(codex_upgrade, _read(Path(state_path)))
    sys.argv = [str(Path(codex_upgrade.__file__)), *arguments]
    code = 1
    try:
        result = codex_upgrade.main(arguments)
        code = result if isinstance(result, int) else 0
    except SystemExit as stop:
        code = stop.code if isinstance(stop.code, int) else (0 if stop.code is None else 1)
    except BaseException:
        traceback.print_exc()
        code = 1
    finally:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:
                pass
    os._exit(code)
