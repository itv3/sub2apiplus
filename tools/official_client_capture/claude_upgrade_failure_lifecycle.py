#!/usr/bin/env python3
"""Claude 的失败生命周期探针（Claude 升级控制面设计 B2-11 第二部分：补齐 Claude 发布认证要求的 Job 演练探针）。

一、为什么要有
==============
发布认证（``certify_release``）要求 Job 演练收据带失败生命周期探针摘要 ``failure_lifecycle_probe_sha256``，两客户端同一
字段。Codex 的这个探针证明：在真实 ``campaign-run`` 父监督器与父租约下，用编排器自己的有限补跑（``_run_job_with_retry``）
跑一个必然失败、零网络的作业，按生产补跑上限（两次）走满三次；每次失败的证据都被整体改名为 ``<源>.failed-attempt<N>``
（固定证据根被清出，下一次才能重建——三次都建成，就证明了「补跑—归档」的先后）；宿主与容器各别名看到同一设备号与 inode、
同一份 marker；父监督器的事件与审计终态完整；清理同样在监督下执行。也就是 ARM64 上经 bind mount 的失败补跑与证据归档链
真实成立、而且受监督。

Claude 的补跑与归档是另一份实现（``claude_upgrade_capture._run_job_with_retry``／``_archive_failed_job_evidence``，容器坐标经
登记的 runs 别名表 ``claude_upgrade.FAILED_JOB_EVIDENCE_*`` 映射回宿主），B2-06 已为这个探针预留了接口：没有 attempt 的执行
上下文 ``ScenarioReceiptContext.detached()``，以及不带工具依赖声明的合成作业。本模块按同一组事实为 Claude 采一份探针，
由 Claude 的 Job 演练画像（``claude_upgrade_job_rehearsal``）放进 facts，发布认证与 Formal 都按统一字段核对，不按客户端分叉。

二、做法
========
父进程（Job 演练采集时由画像的 ``rehearsal_early_probes`` 调 ``capture_probe``）：

* 在宿主数据根 ``runtime/`` 下用 ``mkdtemp`` 原子建本轮唯一的 0700 夹具（Campaign 目录、监督器状态目录、日志根）；
* 写 Claude 新合同（``official-client-campaign-run/v1``，带 ``client``）的单动作清单，动作是本文件的 worker 子命令（受管
  Python 按路径执行本文件，与 Codex 的 worker 同形），跑真实 ``campaign-run`` 父监督器；
* 读 worker 结果、父 run 的审计与事件：父监督器以 ``queue-complete`` 停止、审计完整，三次步骤执行各有开始与失败事件，
  两个容器的只读核对与清理各有开始与结束事件；组装探针并按 ``validate_probe`` 校验后返回；
* ``finally`` 只清理预先声明的四个精确目录（固定证据根与三份归档）与夹具目录本身，不扫描其他运行资产。

worker（``campaign-run`` 子进程里的 ``failure-lifecycle-worker`` 子命令）：

* 只在本进程改写 Claude 编排器的失败证据归档坐标（``FAILED_JOB_EVIDENCE_HOST_RUN_ROOT``／``_CONTAINER_RUN_ROOTS``，取父进程
  传入的值，生产上就是登记值）与补跑退避（``JOB_RETRY_DELAY_SECONDS`` 改成 0，消除 30 秒等待；生产常量与调用方进程都不变），
  并核对补跑上限仍是 2；
* 在 Claude 父租约（附加到父监督器）里调 Claude 的有限补跑执行一个合成作业：在断网容器里 ``python3 -c`` 确认没有默认路由，
  在固定证据根建目录、写 marker，以 23 退出——三次都失败，每次的证据都被归档；
* 宿主逐个读三份归档的 marker、设备号与 inode；抓包容器与断网容器各执行一次只读核对（经父租约），每个容器别名下的三份
  归档都与宿主事实逐项相同；宿主 Python 受监督地删掉三份归档（每个目录只能有那一个 marker），写只写一次的 worker 结果。

三、与 Codex 探针的差别
======================
* 合同名（``official-client-failure-lifecycle-*``）与 Claude 新合同清单；
* 失败作业在断网容器里执行——Claude 合成步骤的真实执行位置，网络隔离由容器本身提供（Claude 运行镜像不要求 bubblewrap），
  脚本仍核对没有默认路由；Codex 是在 ``capture-cli`` 里用 bubblewrap 开新的网络命名空间；
* 容器别名取 Claude 的登记值（``capture_root`` 与登记 runs 别名的上级目录），两个容器各核一次；
* Claude 的有限补跑不发 attempt 级事件（attempt 级的开始与结束由 attempt 主流程记录），父监督器事件只核步骤级、核对与清理；
* 最终结果另核 ``archived_evidence_roots``（前两次的归档根）与 ``environment_contaminated``（没有 attempt 上下文时恒为假）。

本模块属 Claude 身份策略的 control 层（``claude_upgrade_`` 前缀），被 Codex 身份策略忽略（``claude_`` 前缀）；不改共享
Job 演练收据模块，只调用它的通用小函数（字段闭合、规范 JSON、只写一次、读 JSON）。
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
# 按路径作为主程序运行（campaign-run 派发的 worker）时让包路径也指向这一份：本模块的异常类与常量只有一个模块对象。
if __name__ == "__main__":
    sys.modules.setdefault("tools.official_client_capture.claude_upgrade_failure_lifecycle", sys.modules[__name__])

from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as job_rehearsal
from tools.official_client_capture import incremental_recovery

CLIENT = "claude"
SCHEMA = "official-client-failure-lifecycle-probe/v1"
WORKER_SCHEMA = "official-client-failure-lifecycle-worker/v1"
MARKER_SCHEMA = "official-client-failure-lifecycle-marker/v1"
PREDECESSOR_SCHEMA = "official-client-failure-lifecycle-predecessor/v1"
JOB_ID = "vc1-failure-lifecycle-probe"
ACTION_ID = "failure-lifecycle"
ACTION_OPERATION = "VC-1:failure-lifecycle"
WORKER_COMMAND = "failure-lifecycle-worker"
# 生产补跑上限是两次：首次加两次补跑，一共三次执行、三份归档。
RETRY_LIMIT = 2
ATTEMPT_COUNT = RETRY_LIMIT + 1
DEADLINE_SECONDS = 120
STEP_TIMEOUT_SECONDS = 30
COMMAND_TIMEOUT_SECONDS = 30
# 失败脚本的预期退出码（脚本里写死同一个值，测试核对两处一致）。
FAILURE_EXIT_CODE = 23
SOURCE_PREFIX = "claude-failure-lifecycle-"
CAMPAIGN_PREFIX = "p0-failure-lifecycle-"
MARKER_NAME = "failure-lifecycle-marker.json"
CLEANUP_OPERATION = "failure-lifecycle:cleanup"
VERIFY_OPERATION_PREFIX = "failure-lifecycle:verify-aliases:"
# 只读核对的两个容器：启动器名 → 总计划 configuration 字段（与 Job 演练画像同一组；失败作业在断网容器里执行）。
VERIFY_CONTAINER_FIELDS: tuple[tuple[str, str], ...] = (
    ("capture_container", "capture_container"),
    ("synthetic_container", "synthetic_capture_container"),
)
EXECUTION_CONTAINER_FIELD = "synthetic_capture_container"
CAMPAIGN_RUN_LABEL = "Claude VC-1 失败生命周期 campaign-run 演练"
# 父监督器上必须出现的事件：三次步骤执行各一对（开始、失败），两个容器的只读核对与清理各一对（开始、结束）。
REQUIRED_EVENT_COUNT = ATTEMPT_COUNT * 2 + len(VERIFY_CONTAINER_FIELDS) * 2 + 2
PROBE_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "client",
        "campaign_run_schema_version",
        "campaign_id",
        "job_id",
        "source_name",
        "execution_container",
        "verified_containers",
        "capture_root",
        "container_aliases",
        "host_runs_root",
        "retry_limit",
        "attempt_count",
        "network_isolated",
        "live_request_count",
        "archives",
        "final_result",
        "parent_supervisor",
        "cleanup_verified",
    }
)
ARCHIVE_FIELDS = frozenset(
    {"attempt_index", "archive_name", "host_archive", "container_archives", "device", "inode", "marker_sha256", "marker"}
)
MARKER_FIELDS = frozenset({"schema_version", "source_name", "attempt_index", "network_isolated", "live_request_count"})
FINAL_RESULT_FIELDS = frozenset({"status", "attempt_index", "evidence_roots", "archived_evidence_roots", "environment_contaminated"})
PARENT_FIELDS = frozenset({"run_state", "audit_incomplete", "event_count", "required_event_count", "actions"})

# 失败作业（在断网容器里执行）：先确认本命名空间没有默认路由，再按已有归档数推出这是第几次执行；固定证据根必须已被
# 上一次归档清出，才能重新建目录并写 marker，最后以 23 退出。三次都建成即证明补跑与归档的先后。
FAILURE_SCRIPT = r'''
import json,os,pathlib,sys
root=pathlib.Path(sys.argv[1])
source_name=sys.argv[2]
marker_name=sys.argv[3]
lines=pathlib.Path('/proc/net/route').read_text(encoding='ascii').splitlines()[1:]
if any(len(line.split())>1 and line.split()[1]=='00000000' for line in lines):
    raise SystemExit('default-route-present')
attempt=1
while (root.parent/f'{source_name}.failed-attempt{attempt}').exists():
    attempt+=1
if attempt>3:
    raise SystemExit('unexpected-attempt')
if root.exists() or root.is_symlink():
    raise SystemExit('fixed-source-not-cleared')
os.mkdir(root,0o700)
payload={
    'schema_version':'official-client-failure-lifecycle-marker/v1',
    'source_name':source_name,
    'attempt_index':attempt,
    'network_isolated':True,
    'live_request_count':0,
}
raw=(json.dumps(payload,sort_keys=True,separators=(',',':'))+'\n').encode()
flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0)
descriptor=os.open(root/marker_name,flags,0o600)
with os.fdopen(descriptor,'wb') as stream:
    stream.write(raw)
    stream.flush()
    os.fsync(stream.fileno())
raise SystemExit(23)
'''
# 容器内只读核对：每个别名下的三份归档都是真实目录、同一设备号与 inode，marker 字节相同且内容合规。
VERIFY_SCRIPT = r'''
import hashlib,json,pathlib,sys
aliases=[pathlib.Path(value) for value in json.loads(sys.argv[1])]
source_name=sys.argv[2]
marker_name=sys.argv[3]
entries=[]
for attempt in range(1,4):
    paths=[alias/'runs'/f'{source_name}.failed-attempt{attempt}' for alias in aliases]
    if any(not path.is_dir() or path.is_symlink() for path in paths):
        raise SystemExit('archive-missing')
    metadata=[path.stat() for path in paths]
    if len({(item.st_dev,item.st_ino) for item in metadata})!=1:
        raise SystemExit('archive-alias-mismatch')
    raw_values=[(path/marker_name).read_bytes() for path in paths]
    if len(set(raw_values))!=1:
        raise SystemExit('marker-alias-mismatch')
    marker=json.loads(raw_values[0])
    if marker.get('attempt_index')!=attempt or marker.get('network_isolated') is not True or marker.get('live_request_count')!=0:
        raise SystemExit('marker-invalid')
    entries.append({
        'attempt_index':attempt,
        'container_archives':[str(path) for path in paths],
        'device':metadata[0].st_dev,
        'inode':metadata[0].st_ino,
        'marker_sha256':hashlib.sha256(raw_values[0]).hexdigest(),
        'marker':marker,
    })
print(json.dumps({'status':'passed','entries':entries},sort_keys=True))
'''
# 受监督清理：每个归档目录里只能有那一个 marker，删掉 marker 与目录后核对都不在了。
CLEANUP_SCRIPT = r'''
import json,pathlib,sys
paths=[pathlib.Path(value) for value in json.loads(sys.argv[1])]
marker_name=sys.argv[2]
for path in paths:
    if path.is_symlink() or not path.is_dir():
        raise SystemExit('cleanup-target-invalid')
    children=list(path.iterdir())
    if len(children)!=1 or children[0].name!=marker_name or children[0].is_symlink() or not children[0].is_file():
        raise SystemExit('cleanup-target-not-bounded')
for path in paths:
    (path/marker_name).unlink()
    path.rmdir()
if any(path.exists() or path.is_symlink() for path in paths):
    raise SystemExit('cleanup-incomplete')
'''


class FailureLifecycleError(job_rehearsal.JobRehearsalReceiptError):
    """失败生命周期探针没通过、事实不合规或坐标不可信（Job 演练采集与重放按 Job 演练错误处理）。"""


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def _supervisor() -> Any:
    from tools.official_client_capture import codex_upgrade_supervisor

    return codex_upgrade_supervisor


def _safe_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not job_rehearsal.SAFE_ID_RE.fullmatch(value):
        raise FailureLifecycleError(f"{label} 不是安全标识")
    return value


def _absolute_posix(value: Any, label: str) -> PurePosixPath:
    text = str(value) if value is not None else ""
    path = PurePosixPath(text)
    if not path.is_absolute() or str(path) != text or any(part in {"", ".", ".."} for part in path.parts[1:]):
        raise FailureLifecycleError(f"{label} 不是规范绝对路径：{value!r}")
    return path


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def registered_host_runs_root() -> Path:
    """登记的失败证据宿主 runs 根（调用时读取，Job 演练测试可改写）。"""

    return Path(_orchestrator().FAILED_JOB_EVIDENCE_HOST_RUN_ROOT)


def container_aliases(capture_root: Any) -> list[PurePosixPath]:
    """容器里指向同一宿主数据根的别名：``capture_root`` 加登记 runs 别名的上级目录，去重后按字符串排序。

    ``capture_root/runs`` 必须是登记的 runs 别名之一（与 ``claude_upgrade_capture._capture_layout`` 同一规则），否则失败证据
    归档会映射错位。"""

    root = _absolute_posix(capture_root, "capture_root")
    registered = tuple(PurePosixPath(str(item)) for item in _orchestrator().FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS)
    if root / "runs" not in registered:
        raise FailureLifecycleError(f"capture_root 的 runs 目录 {root / 'runs'} 不是登记的容器 runs 别名")
    return sorted({root, *(item.parent for item in registered)}, key=str)


def _archive_name(source_name: str, attempt_index: int) -> str:
    return f"{source_name}.failed-attempt{attempt_index}"


def _expected_host_paths(host_runs_root: Path, source_name: str) -> list[Path]:
    return [host_runs_root / source_name, *(host_runs_root / _archive_name(source_name, index) for index in range(1, ATTEMPT_COUNT + 1))]


def _cleanup_artifacts(host_runs_root: Path, source_name: str) -> None:
    """只清理本轮预先声明的四个精确目录（固定证据根与三份归档）；越过 runs 根的路径一律拒绝。"""

    for path in _expected_host_paths(host_runs_root, source_name):
        try:
            path.relative_to(host_runs_root)
        except ValueError as error:
            raise FailureLifecycleError("失败生命周期清理路径越过 runs 根") from error
        if path.is_symlink():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()


def _private_directory(path: Path, label: str) -> Path:
    if not path.is_absolute() or path.is_symlink() or not path.is_dir():
        raise FailureLifecycleError(f"{label} 不是现有非符号链接绝对目录：{path}")
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        raise FailureLifecycleError(f"{label} 权限必须是 0700：{path}")
    return path


# ---------------------------------------------------------------------------
# worker：campaign-run 子进程里走完真实的失败、补跑与归档链
# ---------------------------------------------------------------------------


def worker(arguments: argparse.Namespace) -> dict[str, Any]:
    """在 Claude 父租约里调 Claude 的有限补跑执行必然失败的合成作业，核对三份归档与两个容器的别名同源，受监督清理。"""

    orchestrator = _orchestrator()
    campaign_id = _safe_id(arguments.campaign_id, "campaign_id")
    source_name = _safe_id(arguments.source_name, "source_name")
    if not campaign_id.startswith(CAMPAIGN_PREFIX) or not source_name.startswith(SOURCE_PREFIX):
        raise FailureLifecycleError("失败生命周期 worker 的 Campaign 或证据名不是本探针的形态")
    capture_container = _safe_id(arguments.capture_container, "capture_container")
    synthetic_container = _safe_id(arguments.synthetic_container, "synthetic_container")
    if capture_container == synthetic_container:
        raise FailureLifecycleError("抓包容器与断网容器必须不同")
    capture_root = _absolute_posix(arguments.capture_root, "capture_root")
    try:
        raw_aliases = json.loads(arguments.container_aliases)
    except (TypeError, json.JSONDecodeError) as error:
        raise FailureLifecycleError("容器别名表不是 JSON") from error
    if not isinstance(raw_aliases, list) or not raw_aliases:
        raise FailureLifecycleError("容器别名表必须是非空数组")
    aliases = [_absolute_posix(item, "容器别名") for item in raw_aliases]
    if [str(item) for item in aliases] != sorted({str(item) for item in aliases}) or capture_root not in aliases:
        raise FailureLifecycleError("容器别名表必须排序、无重复且包含 capture_root")
    host_runs_root = Path(arguments.host_runs_root)
    if not host_runs_root.is_absolute() or host_runs_root.is_symlink() or not host_runs_root.is_dir():
        raise FailureLifecycleError("宿主 runs 根不可信")
    campaign_dir = _private_directory(Path(arguments.campaign_dir), "worker Campaign 目录")
    log_root = _private_directory(Path(arguments.log_root), "worker 日志根")
    output = Path(arguments.output)
    if not output.is_absolute() or output.is_symlink() or output.exists():
        raise FailureLifecycleError("worker 输出路径不可信或已存在")
    try:
        output.parent.resolve(strict=True).relative_to(campaign_dir.parent.resolve(strict=True))
    except (OSError, ValueError) as error:
        raise FailureLifecycleError("worker 输出越过夹具根") from error
    expected_paths = _expected_host_paths(host_runs_root, source_name)
    if any(path.exists() or path.is_symlink() for path in expected_paths):
        raise FailureLifecycleError("失败生命周期探针路径发生碰撞")

    logical_source = capture_root / "runs" / source_name
    # 只在这个零网络 worker 进程里改写：归档坐标取父进程传入的值（生产即登记值），退避改成 0；补跑上限必须仍是生产值。
    orchestrator.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT = host_runs_root
    orchestrator.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS = tuple(alias / "runs" for alias in aliases)
    if orchestrator.JOB_RETRY_LIMIT != RETRY_LIMIT:
        raise FailureLifecycleError("生产 Job 补跑上限不再是两次")
    orchestrator.JOB_RETRY_DELAY_SECONDS = 0
    job = orchestrator.Job(
        job_id=JOB_ID,
        phase="official",
        suites=("full",),
        description="VC-1 失败、补跑与证据归档离线演练（Claude）",
        steps=(
            {
                "argv": [
                    "docker", "exec", synthetic_container, "python3", "-c", FAILURE_SCRIPT,
                    str(logical_source), source_name, MARKER_NAME,
                ],
                "environment": {},
                "timeout": STEP_TIMEOUT_SECONDS,
            },
        ),
        evidence_roots=(str(logical_source),),
        covers=(),
    )
    deadline = incremental_recovery.WallClockDeadline(DEADLINE_SECONDS, label="failure-lifecycle-worker")
    worker_result: dict[str, Any] | None = None
    try:
        with orchestrator.CampaignLease(
            campaign_dir,
            phase="official",
            candidate_id=None,
            deadline=deadline,
            attempt_id="failure-lifecycle-attempt",
            command=WORKER_COMMAND,
            campaign_id=campaign_id,
        ) as lease:
            # 合成作业没有工具依赖声明：增量元数据不读工具身份，显式给空身份，免得 worker 现算整树身份（不影响任何探针事实）。
            result = orchestrator._run_job_with_retry(
                job,
                log_root,
                None,
                tool_identity={},
                deadline=deadline,
                heartbeat=lambda operation: lease.heartbeat(operation),
            )
            final_root = str(capture_root / "runs" / _archive_name(source_name, ATTEMPT_COUNT))
            earlier = [str(capture_root / "runs" / _archive_name(source_name, index)) for index in range(1, ATTEMPT_COUNT)]
            # 最后一次执行的那一步必须是失败脚本自己的退出码（不是没跑起来、默认路由或证据根没清出之类的其他失败）。
            if (
                result.get("status") != "failed"
                or [row.get("return_code") for row in result.get("steps") or [] if isinstance(row, Mapping)] != [FAILURE_EXIT_CODE]
                or result.get("attempt_index") != ATTEMPT_COUNT
                or result.get("evidence_roots") != [final_root]
                or result.get("archived_evidence_roots") != earlier
                or result.get("environment_contaminated") is not False
            ):
                raise FailureLifecycleError("失败生命周期作业没有完成三次失败归档")

            archives: list[dict[str, Any]] = []
            for attempt_index in range(1, ATTEMPT_COUNT + 1):
                archive_name = _archive_name(source_name, attempt_index)
                host_archive = host_runs_root / archive_name
                marker_path = host_archive / MARKER_NAME
                if host_archive.is_symlink() or not host_archive.is_dir() or marker_path.is_symlink() or not marker_path.is_file():
                    raise FailureLifecycleError("失败生命周期宿主归档缺失")
                marker_raw = marker_path.read_bytes()
                try:
                    marker = json.loads(marker_raw)
                except (UnicodeError, json.JSONDecodeError) as error:
                    raise FailureLifecycleError("失败生命周期 marker 非法") from error
                metadata = host_archive.stat()
                archives.append(
                    {
                        "attempt_index": attempt_index,
                        "archive_name": archive_name,
                        "host_archive": str(host_archive),
                        "container_archives": [str(alias / "runs" / archive_name) for alias in aliases],
                        "device": metadata.st_dev,
                        "inode": metadata.st_ino,
                        "marker_sha256": job_rehearsal._sha256_bytes(marker_raw),
                        "marker": marker,
                    }
                )
            expected_entries = [{key: item[key] for key in item if key not in {"archive_name", "host_archive"}} for item in archives]
            verified: list[str] = []
            for launcher, container in (("capture_container", capture_container), ("synthetic_container", synthetic_container)):
                completed = lease.run_command(
                    ["docker", "exec", container, "python3", "-c", VERIFY_SCRIPT,
                     json.dumps([str(alias) for alias in aliases]), source_name, MARKER_NAME],
                    operation=VERIFY_OPERATION_PREFIX + launcher,
                    timeout_seconds=COMMAND_TIMEOUT_SECONDS,
                    job_id=JOB_ID,
                )
                try:
                    facts = json.loads(completed.stdout)
                except (TypeError, UnicodeError, json.JSONDecodeError) as error:
                    raise FailureLifecycleError(f"{container} 的别名核对输出非法") from error
                if completed.returncode != 0 or facts != {"status": "passed", "entries": expected_entries}:
                    raise FailureLifecycleError(f"{container} 看到的归档与宿主事实不同源")
                verified.append(container)

            cleanup = lease.run_command(
                [sys.executable, "-c", CLEANUP_SCRIPT, json.dumps([item["host_archive"] for item in archives]), MARKER_NAME],
                operation=CLEANUP_OPERATION,
                timeout_seconds=COMMAND_TIMEOUT_SECONDS,
                job_id=JOB_ID,
            )
            if cleanup.returncode != 0 or any(path.exists() or path.is_symlink() for path in expected_paths):
                raise FailureLifecycleError("失败生命周期受监督清理未闭合")
            worker_result = {
                "schema_version": WORKER_SCHEMA,
                "status": "passed",
                "client": CLIENT,
                "campaign_id": campaign_id,
                "job_id": JOB_ID,
                "source_name": source_name,
                "execution_container": synthetic_container,
                "verified_containers": verified,
                "capture_root": str(capture_root),
                "container_aliases": [str(alias) for alias in aliases],
                "host_runs_root": str(host_runs_root),
                "retry_limit": int(orchestrator.JOB_RETRY_LIMIT),
                "attempt_count": ATTEMPT_COUNT,
                "network_isolated": True,
                "live_request_count": 0,
                "archives": archives,
                "final_result": {
                    "status": result["status"],
                    "attempt_index": result["attempt_index"],
                    "evidence_roots": list(result["evidence_roots"]),
                    "archived_evidence_roots": list(result["archived_evidence_roots"]),
                    "environment_contaminated": result["environment_contaminated"],
                },
                "cleanup_verified": True,
            }
            job_rehearsal._write_once(output, worker_result)
    finally:
        # 正常路径已由父监督器完成清理；这里只是异常兜底，仍只触碰本轮随机名称对应的四个精确目录。
        _cleanup_artifacts(host_runs_root, source_name)
    if worker_result is None:
        raise FailureLifecycleError("失败生命周期 worker 未生成结果")
    return worker_result


# ---------------------------------------------------------------------------
# 父进程：真实 campaign-run 父监督器
# ---------------------------------------------------------------------------


def _run_campaign(argv: list[str], timeout: int) -> bytes:
    """执行父监督器：Job 演练采集里绑定当前 deadline 与心跳（共享收据模块的模块级状态，调用时读取）；失败时保留诊断。

    campaign-run 对动作失败返回结构化 stdout 和非零退出码，stderr 可能为空：报错时取 stdout，不退化成空错误。"""

    deadline = job_rehearsal._ACTIVE_DEADLINE
    heartbeat = job_rehearsal._ACTIVE_HEARTBEAT
    try:
        if deadline is not None:
            completed = incremental_recovery.run_bounded_subprocess(
                argv, timeout=timeout, deadline=deadline, operation=CAMPAIGN_RUN_LABEL, check=False, capture_output=True,
                heartbeat=heartbeat,
            )
        else:
            completed = subprocess.run(argv, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as error:
        raise FailureLifecycleError(f"{CAMPAIGN_RUN_LABEL}执行失败：{error}") from error
    if completed.returncode != 0:
        message = completed.stderr.decode("utf-8", errors="replace")[:500].strip()
        if not message:
            message = completed.stdout.decode("utf-8", errors="replace")[:500].strip()
        raise FailureLifecycleError(f"{CAMPAIGN_RUN_LABEL}失败：{message or f'returncode={completed.returncode}，子进程未返回诊断'}")
    return completed.stdout


def _required_event_pairs() -> set[tuple[str, str]]:
    pairs = {
        (event_type, f"job:{JOB_ID}:step-1:attempt-{attempt_index}")
        for attempt_index in range(1, ATTEMPT_COUNT + 1)
        for event_type in ("action-started", "action-failed")
    }
    for launcher, _field in VERIFY_CONTAINER_FIELDS:
        pairs.update({("action-started", VERIFY_OPERATION_PREFIX + launcher), ("action-finished", VERIFY_OPERATION_PREFIX + launcher)})
    pairs.update({("action-started", CLEANUP_OPERATION), ("action-finished", CLEANUP_OPERATION)})
    return pairs


def worker_command(
    *,
    campaign_dir: Path,
    campaign_id: str,
    configuration: Mapping[str, Any],
    aliases: Sequence[PurePosixPath],
    host_runs_root: Path,
    source_name: str,
    log_root: Path,
    output: Path,
) -> list[str]:
    """campaign-run 动作的命令：受管 Python 按路径执行本文件的 worker 子命令（与 Codex worker 同形）。"""

    return [
        sys.executable, str(Path(__file__).resolve()), WORKER_COMMAND,
        "--campaign-dir", str(campaign_dir),
        "--campaign-id", campaign_id,
        "--capture-container", str(configuration["capture_container"]),
        "--synthetic-container", str(configuration[EXECUTION_CONTAINER_FIELD]),
        "--capture-root", str(configuration["capture_root"]),
        "--container-aliases", json.dumps([str(alias) for alias in aliases]),
        "--host-runs-root", str(host_runs_root),
        "--source-name", source_name,
        "--log-root", str(log_root),
        "--output", str(output),
    ]


def capture_probe(configuration: Mapping[str, Any]) -> dict[str, Any]:
    """用真实 campaign-run 父监督器（Claude 新合同）与 Claude 父租约演练完整的失败、补跑与归档链，返回校验过的探针。"""

    supervisor = _supervisor()
    for _launcher, field in VERIFY_CONTAINER_FIELDS:
        _safe_id(configuration.get(field), field)
    if configuration.get("capture_container") == configuration.get(EXECUTION_CONTAINER_FIELD):
        raise FailureLifecycleError("抓包容器与断网容器必须不同")
    aliases = container_aliases(configuration.get("capture_root"))
    host_runs_root = registered_host_runs_root()
    runtime_root = host_runs_root.parent / "runtime"
    if host_runs_root.is_symlink() or not host_runs_root.is_dir() or runtime_root.is_symlink() or not runtime_root.is_dir():
        raise FailureLifecycleError(f"失败生命周期探针的宿主 runs 根或 runtime 根不可信：{host_runs_root.parent}")

    nonce = secrets.token_hex(12)
    source_name = f"{SOURCE_PREFIX}{nonce}"
    campaign_id = f"{CAMPAIGN_PREFIX}{nonce}"
    fixture = Path(tempfile.mkdtemp(prefix=f".{source_name}-", dir=runtime_root))
    fixture.chmod(0o700)
    campaign_dir, state_dir, log_root = fixture / "campaign", fixture / "supervisor", fixture / "logs"
    worker_output, manifest_path, predecessor_path = fixture / "worker-result.json", fixture / "campaign-run.json", fixture / "vc0-checkpoint.json"
    expected_paths = _expected_host_paths(host_runs_root, source_name)
    try:
        for directory in (campaign_dir, state_dir, log_root):
            directory.mkdir(mode=0o700)
        job_rehearsal._write_once(campaign_dir / "campaign.json", {"campaign_id": campaign_id, "campaign_mode": "formal", "client": CLIENT})
        predecessor = {
            "schema_version": PREDECESSOR_SCHEMA,
            "campaign_id": campaign_id,
            "phase": "VC-0",
            "status": "completed",
            "live_request_count": 0,
        }
        job_rehearsal._write_once(predecessor_path, predecessor)
        action = {
            "action_id": ACTION_ID,
            "operation": ACTION_OPERATION,
            "timeout_seconds": DEADLINE_SECONDS,
            "command": worker_command(
                campaign_dir=campaign_dir, campaign_id=campaign_id, configuration=configuration, aliases=aliases,
                host_runs_root=host_runs_root, source_name=source_name, log_root=log_root, output=worker_output,
            ),
            "item_ids": [JOB_ID],
        }
        deadline_at = datetime.now(timezone.utc) + timedelta(seconds=DEADLINE_SECONDS + 30)
        manifest = supervisor.build_batched_campaign_run_manifest(
            campaign_id=campaign_id,
            campaign_plan_sha256=job_rehearsal._fingerprint({"campaign_id": campaign_id, "probe": SCHEMA}),
            batch_id="failure-lifecycle-batch",
            batch_sequence=1,
            batch_sha256=job_rehearsal._fingerprint(action),
            phase="official",
            predecessor_checkpoint={
                "path": str(predecessor_path),
                "sha256": job_rehearsal._sha256_file(predecessor_path),
                "phase": "VC-0",
                "checkpoint_sha256": job_rehearsal._fingerprint(predecessor),
            },
            original_deadline_at_utc=deadline_at.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            actions=[action],
            execute_items=[JOB_ID],
            reuse_items=[],
            client=CLIENT,
        )
        job_rehearsal._write_once(manifest_path, manifest)
        raw = _run_campaign(
            [
                sys.executable, str(Path(supervisor.__file__).resolve()), "campaign-run",
                "--state-dir", str(state_dir), "--manifest", str(manifest_path),
                "--heartbeat-seconds", "0.2", "--watchdog-timeout-seconds", "5", "--ledger-interval-seconds", "0.2",
            ],
            DEADLINE_SECONDS + 30,
        )
        try:
            campaign_result = json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as error:
            raise FailureLifecycleError("失败生命周期 campaign-run 输出非法") from error
        worker_facts, _raw = job_rehearsal._load_json(worker_output, "失败生命周期 worker 结果")
        run_dir = Path(str(campaign_result.get("run_dir", "")))
        try:
            run_dir.resolve(strict=True).relative_to(state_dir.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise FailureLifecycleError("失败生命周期父 run 目录越界") from error
        audit = supervisor._audit_command(run_dir)
        events = [json.loads(line) for line in (run_dir / "events.ndjson").read_text(encoding="utf-8").splitlines() if line.strip()]
        pairs = {(str(item.get("event_type")), str(item.get("operation"))) for item in events if isinstance(item, Mapping)}
        required = _required_event_pairs()
        actions = campaign_result.get("actions")
        if (
            campaign_result.get("campaign_id") != campaign_id
            or campaign_result.get("status") != "stopped"
            or campaign_result.get("reason") != "queue-complete"
            or campaign_result.get("execute_items") != [JOB_ID]
            or campaign_result.get("reuse_items") != []
            or actions != [{"action_id": ACTION_ID, "returncode": 0, "status": "passed"}]
            or audit.get("state") != "stopped"
            or audit.get("audit_incomplete") is not False
            or not required.issubset(pairs)
        ):
            missing = sorted(f"{event}:{operation}" for event, operation in required - pairs)
            raise FailureLifecycleError(f"失败生命周期父监督器没有形成完整的成功终态（缺少事件：{missing[:6]}）")
        if worker_facts.get("schema_version") != WORKER_SCHEMA or worker_facts.get("status") != "passed":
            raise FailureLifecycleError("失败生命周期 worker 结果不是通过")
        probe = {
            "schema_version": SCHEMA,
            "status": "passed",
            "client": CLIENT,
            "campaign_run_schema_version": manifest["schema_version"],
            "campaign_id": campaign_id,
            **{
                key: worker_facts.get(key)
                for key in (
                    "job_id", "source_name", "execution_container", "verified_containers", "capture_root", "container_aliases",
                    "host_runs_root", "retry_limit", "attempt_count", "network_isolated", "live_request_count", "archives",
                    "final_result",
                )
            },
            "parent_supervisor": {
                "run_state": audit.get("state"),
                "audit_incomplete": audit.get("audit_incomplete"),
                "event_count": audit.get("event_count"),
                "required_event_count": len(required),
                "actions": actions,
            },
            "cleanup_verified": worker_facts.get("cleanup_verified") is True
            and not any(path.exists() or path.is_symlink() for path in expected_paths),
        }
        return validate_probe(probe, configuration)
    finally:
        _cleanup_artifacts(host_runs_root, source_name)
        # 夹具是 runtime 根下由 mkdtemp 原子建立的本轮唯一目录：先拒绝顶层符号链接，再只删除这个精确目录。
        if fixture.is_symlink():
            fixture.unlink()
        elif fixture.is_dir():
            shutil.rmtree(fixture)


# ---------------------------------------------------------------------------
# 校验：采集后与重放时同一份
# ---------------------------------------------------------------------------


def validate_probe(value: Any, configuration: Mapping[str, Any]) -> dict[str, Any]:
    """重放失败生命周期探针：身份与坐标、三次失败归档、两个容器的核对、父监督器终态与清理的联合事实。"""

    probe = job_rehearsal._expect(value, set(PROBE_FIELDS), "probes.failure_lifecycle")
    capture_root = _absolute_posix(configuration.get("capture_root"), "执行合同 capture_root")
    aliases = container_aliases(capture_root)
    campaign_id = _safe_id(probe.get("campaign_id"), "failure_lifecycle.campaign_id")
    source_name = _safe_id(probe.get("source_name"), "failure_lifecycle.source_name")
    expected_containers = [configuration.get(field) for _launcher, field in VERIFY_CONTAINER_FIELDS]
    host_runs_root = registered_host_runs_root()
    if (
        probe.get("schema_version") != SCHEMA
        or probe.get("status") != "passed"
        or probe.get("client") != CLIENT
        or probe.get("campaign_run_schema_version") != _supervisor().OFFICIAL_CLIENT_CAMPAIGN_RUN_SCHEMA
        or not campaign_id.startswith(CAMPAIGN_PREFIX)
        or not source_name.startswith(SOURCE_PREFIX)
        or campaign_id[len(CAMPAIGN_PREFIX):] != source_name[len(SOURCE_PREFIX):]
        or probe.get("job_id") != JOB_ID
        or probe.get("execution_container") != configuration.get(EXECUTION_CONTAINER_FIELD)
        or probe.get("verified_containers") != expected_containers
        or probe.get("capture_root") != str(capture_root)
        or probe.get("container_aliases") != [str(alias) for alias in aliases]
        or probe.get("host_runs_root") != str(host_runs_root)
        or probe.get("retry_limit") != RETRY_LIMIT
        or probe.get("attempt_count") != ATTEMPT_COUNT
        or probe.get("network_isolated") is not True
        or probe.get("live_request_count") != 0
        or probe.get("cleanup_verified") is not True
    ):
        raise FailureLifecycleError("失败生命周期探针的身份、坐标、补跑或零网络事实非法")

    archives = probe.get("archives")
    if not isinstance(archives, list) or len(archives) != ATTEMPT_COUNT:
        raise FailureLifecycleError("失败生命周期归档数量非法")
    seen: set[tuple[int, int]] = set()
    for attempt_index, raw_archive in enumerate(archives, 1):
        archive = job_rehearsal._expect(raw_archive, set(ARCHIVE_FIELDS), f"probes.failure_lifecycle.archives.{attempt_index}")
        marker = job_rehearsal._expect(archive.get("marker"), set(MARKER_FIELDS), f"probes.failure_lifecycle.archives.{attempt_index}.marker")
        archive_name = _archive_name(source_name, attempt_index)
        device, inode = archive.get("device"), archive.get("inode")
        if (
            archive.get("attempt_index") != attempt_index
            or archive.get("archive_name") != archive_name
            or archive.get("host_archive") != str(host_runs_root / archive_name)
            or archive.get("container_archives") != [str(alias / "runs" / archive_name) for alias in aliases]
            or not _is_int(device)
            or device < 0
            or not _is_int(inode)
            or inode <= 0
            or (device, inode) in seen
            or marker
            != {
                "schema_version": MARKER_SCHEMA,
                "source_name": source_name,
                "attempt_index": attempt_index,
                "network_isolated": True,
                "live_request_count": 0,
            }
            or archive.get("marker_sha256") != job_rehearsal._sha256_bytes(job_rehearsal._canonical(marker))
        ):
            raise FailureLifecycleError(f"失败生命周期第 {attempt_index} 次归档事实非法")
        seen.add((device, inode))

    final = job_rehearsal._expect(probe.get("final_result"), set(FINAL_RESULT_FIELDS), "probes.failure_lifecycle.final_result")
    runs = capture_root / "runs"
    if final != {
        "status": "failed",
        "attempt_index": ATTEMPT_COUNT,
        "evidence_roots": [str(runs / _archive_name(source_name, ATTEMPT_COUNT))],
        "archived_evidence_roots": [str(runs / _archive_name(source_name, index)) for index in range(1, ATTEMPT_COUNT)],
        "environment_contaminated": False,
    }:
        raise FailureLifecycleError("失败生命周期最终作业结果非法")

    parent = job_rehearsal._expect(probe.get("parent_supervisor"), set(PARENT_FIELDS), "probes.failure_lifecycle.parent_supervisor")
    event_count, required_count = parent.get("event_count"), parent.get("required_event_count")
    if (
        parent.get("run_state") != "stopped"
        or parent.get("audit_incomplete") is not False
        or not _is_int(event_count)
        or not _is_int(required_count)
        or required_count != REQUIRED_EVENT_COUNT
        or event_count < required_count
        or parent.get("actions") != [{"action_id": ACTION_ID, "returncode": 0, "status": "passed"}]
    ):
        raise FailureLifecycleError("失败生命周期父监督器终态非法")
    return probe


# ---------------------------------------------------------------------------
# 命令行：只有 campaign-run 派发的隐藏 worker 子命令
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Claude 失败生命周期探针（只供 Job 演练的 campaign-run 动作调用）")
    commands = parser.add_subparsers(dest="command", required=True)
    worker_parser = commands.add_parser(WORKER_COMMAND, help=argparse.SUPPRESS)
    for flag in (
        "--campaign-dir", "--campaign-id", "--capture-container", "--synthetic-container", "--capture-root",
        "--container-aliases", "--host-runs-root", "--source-name", "--log-root", "--output",
    ):
        worker_parser.add_argument(flag, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)
    arguments = build_parser().parse_args(argv)
    try:
        result = worker(arguments)
    except Exception as error:  # noqa: BLE001 - 隐藏动作：任何异常都要给父监督器留下有界、脱敏的诊断
        try:
            _supervisor().write_campaign_run_action_diagnostic(failure_kind="handled-error", error=error)
        except Exception:  # noqa: BLE001 - 诊断写不出不掩盖原始失败
            pass
        print(f"Claude 失败生命周期 worker 未通过：{type(error).__name__}: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"status": result["status"], "job_id": result["job_id"]}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
