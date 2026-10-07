#!/usr/bin/env python3
"""Claude 官方取证 attempt：Job 动作体与环境闭环（Claude 升级控制面设计 B2-06）。

一、职责与边界
==============
``claude_upgrade.py capture-official run`` 的实现体。``claude_upgrade.py`` 只登记子命令参数并以同名同签名的薄壳转调本模块；
服务面（``upgrade_client_adapter.ORCHESTRATOR_SURFACE``）里归 B2-06 的 17 个成员与 Claude 身份策略登记的两个 wire 根
（``_campaign_arguments``、``_run_job_impl``）的实现都在这里。另两个 wire 根 ``_load_plan_jobs``、``_job_context`` 与场景
清单的展开、运行期变量绑定由 B2-05 在 ``claude_upgrade_plan`` 落实，本模块直接调用。

* 动作体：场景清单里每个 Job 的每一步都是 ``docker exec <容器> python3 <capture_tool_root>/claude_fw_e_relay.py
  capture --execute …``（hosts override 与 leaf cert、relay、claude 由 relay 在容器内完成），经 Campaign 租约的独立监督器
  ``SupervisorClient.run_command`` 执行；没有活跃租约时（单元测试、离线调用）以独立进程组执行并受同一条 deadline 约束。
  真实上游步骤进联网的抓包容器 ``capture_container``，合成步骤进断网容器 ``synthetic_capture_container``（见第五节）。
* 每个 Job 的环境闭环（每次执行都做一遍）：before 探针 → 每步执行前在宿主签发运行镜像凭据并核对容器内可见 → 执行步骤 →
  after 探针 → 前后快照逐字比较（漂移即判环境污染、不补跑、停止后续 Job）→ relay 清单核对；Job 终态（含有限补跑与失败证据
  归档）之后做一次 Job 级证据权限收口、写 Job 闭环收据，再追加一条 Job checkpoint。
* attempt 级：原子预约 → attempt 级 before 探针 → 逐 Job → attempt 级 after 探针（共享恢复预览按它比较连续性）→ run 末
  ``codex-upgrade-evidence-permission-closeout/v2`` 收口 → 只写一次的 ``attempt.json``。
* 与 Codex 共用的 wire 层文件（``runtime_host_receipt.py``、``capturelib/``、``upstream_byte_relay.py``、
  ``drive_claude_tui.py``、``run_claude_relay_scenario.sh``）只作为子进程调用；Codex 的环境探针模块被 Claude 身份策略忽略，
  本模块自带 Claude 探针，不导入它。

二、wire 闭包的划分
==================
wire 根 ``_run_job_impl`` 只覆盖「一次执行」本身：运行期变量绑定（含宿主凭据签发与容器内可见核对）、零请求演练的
``--dry-run`` 改写、经监督器执行步骤、证据根存在性与 relay 清单（演练时为计划回显）核对。before／after 探针与漂移判定、
补跑与归档、权限收口、checkpoint 与 attempt 收据都在 wire 根之外（``_run_job_with_retry`` 与 attempt 主流程），改这些代码
不改变 Claude 的 wire 身份。

三、坐标约定（容器坐标与宿主坐标）
==================================
场景清单把证据根写成容器坐标 ``<capture_root>/runs/claude-official/<campaign_id>/<job_id>``（``capture_root`` 缺省
``/capture``：relay 拒绝把输出写进自身源码树 ``/root/oauth-capture``），每步输出根是它下面以 probe 命名的子目录。
Job 结果与 attempt 里的 ``results[].evidence_roots`` 保持容器坐标（与冻结的 Job 一致，B1-11 计量按
``configuration.capture_root`` 映射）；宿主侧的一切读写（存在性、relay 清单、失败证据归档、权限收口）都把容器坐标经登记的
runs 别名表（``FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS`` → ``FAILED_JOB_EVIDENCE_HOST_RUN_ROOT``）映射到宿主数据根，
不依赖宿主上存在 ``/capture`` 别名。权限收口在宿主坐标上做，逻辑 runs 根取宿主 ``<数据根>/runs`` 本身；收口参数写进
attempt 的 ``evidence_permission_layout``，重放（B2-07）按同一组参数调用 ``replay_evidence_permission_closeout``。

四、零请求演练（B2-06 验收：ARM64 单 Job 零请求演练）
=====================================================
``capture-official run --zero-request-rehearsal --job-id <id>`` 只能用于 ``preflight_only`` Campaign：与正式 run 走同一条
代码路径（租约、监督器、执行树与官方二进制核验、探针、宿主凭据签发与容器内可见核对、步骤执行、权限收口、checkpoint、
attempt 收据），唯一差别是把 relay 步骤的 ``--execute`` 与请求确认参数换成 ``--dry-run``——relay 在读取凭据、改写 hosts、
启动中继与客户端之前返回计划，不发任何请求；编排器据此核对计划回显、并要求证据根保持不存在。演练 attempt 写在
``<Campaign>/rehearsal/official/attempts/``，与正式 attempt 命名空间分开，任何正式读侧都看不到它。演练用 Campaign 由本模块
命令行 ``rehearsal-campaign`` 生成，演练结果由 ``verify-rehearsal`` 只读复核（见 ``main``）。

五、两个采集容器：联网的抓包容器与断网的合成容器
================================================
合成 probe 不转发上游，但客户端仍会访问没被 hosts 覆盖的域名，并带着真实凭据。2.1.226 的 FW-F 完整取证让 21 个合成
probe 在 ``--network none`` 的容器里执行；这里沿用同一隔离，只是容器由 ARM64 compose 常驻（``capture-cli-synthetic``），
不是每轮临时创建：

* 路由：B2-05 在 plan 时就把真实上游步骤钉在 ``{capture_container}``、合成步骤钉在 ``{synthetic_capture_container}``；
  执行时 ``ScenarioReceiptContext`` 再按冻结的 ``--upstream-ip`` 复核一次（真实上游步骤必带、合成步骤禁带），写错即失败；
* 预约前：``_verify_official_binaries`` 调 ARM64 环境收据版本 2 的同一份断网容器核验（存在且运行、``network_mode``
  为 none 且没有任何地址、与抓包容器同镜像、去掉 Docker 控制面后挂载逐项相同、两边不共用 hosts 等逐容器文件、容器内
  目标二进制版本自报与摘要），两个容器里的二进制都必须等于 ``target_sha256``；``_container_preconditions`` 核对两个容器
  里的凭据、CA 与全局状态文件，正式 run 还要求两边的全局状态内容相同（只记布尔值，不记内容或摘要）；
* 每一步：宿主运行凭据按步骤实际所在的容器签发并核对容器内可见（relay 在容器内用 hostname 与 mountinfo 复核容器身份）；
* 探针：before／after 两个容器各采一份，五类快照都按角色（``capture``／``synthetic``）分开记录，任一容器漂移或残留取证
  进程都判环境污染；
* 监督器：出口守护下 ``job:`` 开头的操作会绑定唯一准入的 ``capture-cli``，失败时停掉它、并拒绝 ``docker exec`` 到别的
  容器（``codex_upgrade_supervisor._capture_begin``／``_capture_pin_command``）。断网容器不在出口准入里，合成步骤的操作名
  因此改用 ``synthetic-job:`` 前缀：不建停止绑定、不改写目标；没有网络就没有真实请求可停，残留进程由 after 探针判污染。
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
# 作为主程序运行时让包名也指向这一份，编排器延迟导入本模块时拿到同一个模块对象（与 claude_upgrade.py 同一做法）。
if __name__ == "__main__":
    sys.modules.setdefault("tools.official_client_capture.claude_upgrade_capture", sys.modules[__name__])

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256, secure_write_json
from tools.official_client_capture import claude_upgrade
from tools.official_client_capture import claude_upgrade_arm64_environment_receipt as claude_arm64
from tools.official_client_capture import claude_upgrade_plan
from tools.official_client_capture import codex_upgrade_evidence_permissions
from tools.official_client_capture import codex_upgrade_root_cause
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import codex_upgrade_wire_transition
from tools.official_client_capture import incremental_recovery
from tools.official_client_capture import upgrade_client_adapter
from tools.official_client_capture import upgrade_control_plane


# ---------------------------------------------------------------------------
# 合同常量
# ---------------------------------------------------------------------------

CLIENT = "claude"
# 编排器自有对象一律用 claude-upgrade-* 合同名（与 B1-09 的恢复预约／连续性收据同一口径），不复用 Codex 合同名。
CAPTURE_RESERVATION_SCHEMA = "claude-upgrade-capture-reservation/v1"
CAPTURE_ATTEMPT_SCHEMA = "claude-upgrade-capture-attempt/v1"
JOB_CHECKPOINT_SCHEMA = "claude-upgrade-job-checkpoint/v1"
JOB_CLOSURE_SCHEMA = "claude-upgrade-job-closure/v1"
# 探针与二进制核验 v2：同时覆盖联网的抓包容器与断网的合成容器（v1 只有抓包容器，没有任何正式 attempt 用过）。
PROBE_MANIFEST_SCHEMA = "claude-upgrade-environment-probe/v2"
BINARY_VERIFICATION_SCHEMA = "claude-upgrade-official-binary-verification/v2"
WATCHDOG_HEARTBEAT_SCHEMA = "claude-upgrade-watchdog-heartbeat/v1"
WATCHDOG_CHECKPOINT_SCHEMA = "claude-upgrade-watchdog-checkpoint/v1"
ENVIRONMENT_CONTAMINATION_SCHEMA = "claude-upgrade-environment-contamination/v1"
REHEARSAL_VERDICT_SCHEMA = "claude-upgrade-zero-request-rehearsal-verdict/v1"
# 共用 wire 层产物的合同名（只读核对，不由本模块生成）
RELAY_RUN_SCHEMA = "claude-code-fw-e-relay-run/v1"
RELAY_PLAN_SCHEMA = "claude-code-fw-e-relay-plan/v1"
HOST_RUNTIME_RECEIPT_SCHEMA = "official-client-runtime-host-receipt/v1"
RELAY_SCRIPT_NAME = claude_upgrade_plan.RELAY_SCRIPT
HOST_RUNTIME_RECEIPT_SCRIPT = "runtime_host_receipt.py"

MODE_FORMAL = "formal"
MODE_REHEARSAL = "zero_request_rehearsal"
MODES = frozenset({MODE_FORMAL, MODE_REHEARSAL})
FORMAL_ATTEMPT_STATUSES = frozenset({"awaiting_receipts", "failed", "environment_contaminated"})
REHEARSAL_ATTEMPT_STATUSES = frozenset({"rehearsal_passed", "rehearsal_failed", "environment_contaminated"})
# 演练 attempt 与正式 attempt 分目录：正式读侧（列举、活跃判定、seal、恢复）只看 official/attempts。
REHEARSAL_DIRNAME = "rehearsal"

# 探针快照种类：前后逐字相等；processes 另要求为空（没有残留的取证进程）。连续性比较取前三类（共享恢复预览用）。
PROBE_KINDS = ("containers", "configuration", "client", "processes", "listeners")
EXPECTED_EMPTY_PROBE_KINDS = frozenset({"processes"})
CONTINUITY_PROBE_KINDS = ("containers", "configuration", "client")
PROBE_STATE_FILES = {kind: f"{kind}-state.json" for kind in PROBE_KINDS}
PROBE_MANIFEST_NAME = "probe-manifest.json"
HOSTS_DIGEST_MODE = "sorted_lines_without_docker_self_reference_sha256"
CLAUDE_STATE_DIR = "/root/.claude"
CLAUDE_GLOBAL_STATE_FILE = "/root/.claude.json"
CLAUDE_STATE_LISTING_LIMIT = 4000

SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
SAFE_ABSOLUTE_PATH_RE = re.compile(r"^/[A-Za-z0-9._/+:-]+$")
IMMUTABLE_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:+-]*@sha256:[a-f0-9]{64}$")
IMAGE_ID_RE = re.compile(r"^sha256:[a-f0-9]{64}$")
CONTAINER_ID_RE = re.compile(r"^[0-9a-f]{64}$")
SHA256SUM_LINE_RE = re.compile(r"^(?P<sha256>[0-9a-f]{64}) [ *](?P<path>[^\n]+)\n?$")
MANAGED_ROOT_NAMES = ("official_client_capture", "official_client_control")
EXECUTION_TREE_SUFFIX = ("tools", "official_client_capture")

# 宿主机目标客户端（B5a-07 实体安装）；容器内同一模板（数据根 runtime/claude-<v> 只读挂载）。离线测试可改写本常量。
HOST_CLIENT_BINARY_TEMPLATE = "/opt/claude-{version}/claude"
PROBE_COMMAND_TIMEOUT_SECONDS = 30
HOST_RECEIPT_TIMEOUT_SECONDS = 60
STAGING_ORPHAN_MONITOR_WAIT_SECONDS = 10.0
CONTAMINATION_MARKER = "environment-contaminated.json"
JOBS_DIRNAME = "jobs"
JOB_CLOSURE_FILENAME = "job-closure.json"
# 官方阶段已封存的判据（B2-07 封存产物）：任一存在即拒绝新建官方 attempt。
OFFICIAL_SEALED_MARKERS = (Path("official") / "result.json", Path("control") / "vc" / "vc-1-checkpoint.json")
# 取证相关进程的识别规则（只记标签，不记命令行原文，避免把路径或参数写进证据）。
CAPTURE_PROCESS_SCRIPT_LABELS = {
    "claude_fw_e_relay.py": "relay",
    "upstream_byte_relay.py": "upstream-relay",
    "drive_claude_tui.py": "tui-driver",
    "claude_drive_tui_cli.py": "tui-driver",
    "claude_drive_desktop.py": "desktop-driver",
    "scrub_raw_bytes.py": "scrubber",
}
CAPTURE_PROCESS_BINARY_LABELS = {"claude": "claude", "mitmdump": "mitmdump", "tcpdump": "tcpdump", "tshark": "tshark"}
RELAY_ACKNOWLEDGE_FLAGS = ("--acknowledge-live-requests", "--acknowledge-synthetic-responses")
# 两个采集容器的角色 → 总计划 configuration 里的容器名字段（探针、凭据签发与前提检查都按角色区分）。
ROLE_CAPTURE = "capture"
ROLE_SYNTHETIC = "synthetic"
CONTAINER_ROLE_FIELDS = {ROLE_CAPTURE: "capture_container", ROLE_SYNTHETIC: "synthetic_capture_container"}
# 步骤操作名：真实上游步骤沿用 ``job:``（出口守护下绑定 capture-cli 的停止合同），合成步骤改用 ``synthetic-job:``。
LIVE_STEP_OPERATION_PREFIX = "job:"
SYNTHETIC_STEP_OPERATION_PREFIX = "synthetic-job:"
# relay 读取全局状态的开关与缺省值（场景清单不传时 relay 用 /root/.claude.json）。
GLOBAL_STATE_FLAG = "--claude-global-state-file"


def _client_adapter() -> Any:
    return upgrade_client_adapter.adapter_for(CLIENT)


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _private_directory(path: Path, root: Path) -> Path:
    """在 ``root`` 之内逐级创建 0700 目录（不走 ``mkdir -p`` 的默认权限），拒绝符号链接与越出 ``root``。"""

    root = Path(root)
    path = Path(path)
    try:
        relative = path.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise ConfigurationError(f"目录越过允许边界：{path}") from error
    current = root
    if current.is_symlink() or not current.is_dir():
        raise ConfigurationError(f"目录根不可信：{root}")
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ConfigurationError(f"拒绝使用符号链接目录：{current}")
        if not current.exists():
            current.mkdir(mode=0o700)
            current.chmod(0o700)
        elif not current.is_dir():
            raise ConfigurationError(f"路径不是目录：{current}")
    return path


def _argument_value(argv: Sequence[str], flag: str) -> str | None:
    """取 argv 里某个开关的唯一取值；出现多次或没有取值即返回 None 以便调用方拒绝。"""

    positions = [index for index, item in enumerate(argv) if item == flag]
    if len(positions) != 1 or positions[0] + 1 >= len(argv):
        return None
    return str(argv[positions[0] + 1])


# ---------------------------------------------------------------------------
# Campaign 坐标 → Job（wire 根 _campaign_arguments 与服务面 _campaign_jobs）
# ---------------------------------------------------------------------------

# Campaign 总计划 configuration 里取证需要的容器坐标与取证参数（B2-11 按 B2-05 plan 报告的 configuration 原样写入）。
CONFIGURATION_VARIABLES = tuple(
    name
    for name in claude_upgrade_plan.CAMPAIGN_VARIABLES
    if name not in {"campaign_id", "campaign_dir", "target_version", "target_sha256"}
)


def _scenario_binding(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    """总计划 ``inputs.target_scenarios``：Campaign 内冻结的目标场景清单副本（路径相对 Campaign 目录）。"""

    inputs = manifest.get("inputs")
    reference = inputs.get("target_scenarios") if isinstance(inputs, Mapping) else None
    if (
        not isinstance(reference, Mapping)
        or not isinstance(reference.get("path"), str)
        or not reference["path"]
        or not SHA256_RE.fullmatch(str(reference.get("sha256", "")))
    ):
        raise ConfigurationError("Claude Campaign 总计划缺少目标场景清单冻结副本绑定 inputs.target_scenarios（{path, sha256}）。")
    return reference


def _campaign_rules(manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """总计划 ``required_rules``：B2-05 plan 报告的规则编号数组（本轮逐规则重测的必需规则全集）。"""

    rules = manifest.get("required_rules")
    if (
        not isinstance(rules, list)
        or not rules
        or not all(isinstance(rule, str) and claude_upgrade_plan.RULE_RE.fullmatch(rule) for rule in rules)
        or len(rules) != len(set(rules))
    ):
        raise ConfigurationError("Claude Campaign required_rules 必须是非空、不重复的规则编号数组。")
    return tuple(rules)


def _campaign_suite(manifest: Mapping[str, Any]) -> str:
    configuration = manifest.get("configuration")
    suite = manifest.get("suite")
    if suite is None and isinstance(configuration, Mapping):
        suite = configuration.get("suite")
    suite = "full" if suite is None else suite
    if suite not in claude_upgrade_plan.JOB_SUITES:
        raise ConfigurationError("Claude Campaign suite 只能是 core 或 full。")
    return str(suite)


def _campaign_relative_file(campaign_dir: Path, relative: str) -> Path:
    """Campaign 内的相对路径（纯路径计算，不读总计划）：拒绝绝对路径、``..`` 与越出 Campaign 根。"""

    candidate = Path(relative)
    if not relative or candidate.is_absolute() or ".." in candidate.parts:
        raise ConfigurationError(f"Campaign 内文件路径非法：{relative!r}")
    path = Path(campaign_dir) / candidate
    if not path.resolve(strict=False).is_relative_to(Path(campaign_dir).resolve(strict=False)):
        raise ConfigurationError(f"Campaign 文件越过根目录：{relative}")
    return path


def _campaign_arguments(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    candidate_id: str | None = None,
    runtime_image: str | None = None,
    profile_id: str | None = None,
    profile_digest: str | None = None,
    build_id: str | None = None,
    deployed_version: str | None = None,
    candidate_image_id: str | None = None,
    source_tree_sha256: str | None = None,
    candidate_purpose: str | None = None,
) -> argparse.Namespace:
    """wire 根：把已校验的 Claude 总计划映射成 Job 展开参数（与 B2-05 ``plan_context`` 的 Campaign 变量一一对应）。

    只读取调用方传入的总计划字典，不调用 ``load_campaign_manifest`` 等 control 函数（wire 闭包保持小，改 control 代码不被
    判成 wire 变化）。``configuration`` 必须给全 B2-05 plan 报告列出的容器坐标与取证参数；缺项即失败关闭，不在运行期补
    默认值（默认值只在 plan 时按场景清单变量合同代入一次，冻结进总计划）。
    """

    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ConfigurationError("Claude Campaign 总计划缺少 configuration。")
    missing = [name for name in CONFIGURATION_VARIABLES if not isinstance(configuration.get(name), str) or not configuration[name]]
    if missing:
        raise ConfigurationError(f"Claude Campaign configuration 缺少取证坐标：{missing}")
    reference = _scenario_binding(manifest)
    run_id = str(manifest["campaign_id"]) + (f"-{candidate_id}" if candidate_id else "")
    values = {name: str(configuration[name]) for name in CONFIGURATION_VARIABLES}
    # 候选阶段变量（B4-01）：B4-01 之前建立的总计划没有这几项，official-only 的 Campaign 允许为空字符串；候选 run 之前另行
    # 失败关闭核对非空。这里只读不补默认值。
    for name in claude_upgrade_plan.CANDIDATE_CAMPAIGN_VARIABLES:
        value = configuration.get(name, "")
        if not isinstance(value, str):
            raise ConfigurationError(f"Claude Campaign configuration.{name} 必须是字符串。")
        values[name] = value
    if runtime_image:
        values["runtime_image"] = str(runtime_image)
    return argparse.Namespace(
        command="capture-candidate" if candidate_id else "capture-official",
        campaign_dir=campaign_dir,
        output=campaign_dir,
        campaign_id=run_id,
        campaign_mode=manifest.get("campaign_mode"),
        campaign_purpose=manifest.get("campaign_purpose"),
        baseline_version=str(manifest.get("baseline_version", "")),
        target_version=str(manifest.get("target_version", "")),
        target_sha256=str(manifest.get("target_sha256", "")),
        scenario_manifest=_campaign_relative_file(campaign_dir, str(reference["path"])),
        scenario_manifest_sha256=str(reference["sha256"]),
        suite=_campaign_suite(manifest),
        candidate_id=candidate_id,
        profile_id=profile_id,
        profile_digest=profile_digest,
        build_id=build_id,
        deployed_version=deployed_version,
        candidate_image_id=candidate_image_id,
        source_tree_sha256=source_tree_sha256,
        candidate_purpose=candidate_purpose,
        max_wall_seconds=None,
        heartbeat_seconds=None,
        **values,
    )


def _verify_plan_job_rows(manifest: Mapping[str, Any], jobs: Sequence[Any], phase: str) -> None:
    """总计划 ``jobs`` 冻结行（plan 写入时）与展开结果逐项一致：同一组 ID，带执行摘要的行摘要相等。"""

    rows = manifest.get("jobs")
    if not rows:
        return
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) and isinstance(row.get("id"), str) for row in rows):
        raise ConfigurationError("Claude Campaign jobs 冻结行形态非法。")
    phase_rows = {str(row["id"]): row for row in rows if row.get("phase", phase) == phase}
    expanded = {job.job_id: job for job in jobs}
    if set(phase_rows) != set(expanded):
        raise ConfigurationError(
            f"Claude Campaign 冻结的 {phase} Job 与场景清单展开结果不一致："
            f"缺 {sorted(set(phase_rows) - set(expanded))}，多 {sorted(set(expanded) - set(phase_rows))}"
        )
    for job_id, row in phase_rows.items():
        frozen = row.get("execution_sha256")
        if frozen is not None and frozen != _job_execution_sha256(expanded[job_id]):
            raise ConfigurationError(f"Claude Campaign 冻结的 Job {job_id} 执行摘要漂移。")


def _campaign_jobs(
    campaign_dir: Path,
    manifest: dict[str, Any],
    phase: str,
    *,
    candidate_id: str | None = None,
    runtime_image: str | None = None,
    profile_id: str | None = None,
    profile_digest: str | None = None,
    build_id: str | None = None,
    deployed_version: str | None = None,
    candidate_image_id: str | None = None,
    source_tree_sha256: str | None = None,
    candidate_purpose: str | None = None,
    use_approved_scenario: bool | None = None,
) -> list[claude_upgrade.Job]:
    """服务面成员：从 Campaign 冻结的场景清单副本展开某阶段的 Job（B2-05 的 ``read_scenario_manifest`` 加
    ``expand_scenario_jobs``；可变总手册之后的改动不击穿已建 Campaign，规格章节摘要不再复核）。

    候选阶段（B4-01b）只需要 ``candidate_id``：Claude 候选步骤只引用 Campaign 变量（候选五个变量与官方取证坐标），
    ``campaign_id`` 展开成 ``<campaign_id>-<candidate_id>``；共享流程按 Codex 习惯传入的候选身份参数（运行镜像、画像、
    构建 ID 等）在 Claude 一律不参与展开，免得 run 与 seal 因传参不同得到不同的 Job。冻结行核对先按不带候选 ID 的展开
    （与 plan 时同一上下文）逐项比较执行摘要，证明冻结模板没变，再返回带候选 ID 的展开。

    批准场景（分类之后）的展开依赖 VC-2 的产物，不在 B4-01 范围内，这里不猜测其语义。
    """

    if use_approved_scenario:
        raise claude_upgrade._pending("_campaign_jobs（批准场景）", "B4-04／B4-05（VC-5 评估读侧）")
    if phase not in {"official", "candidate"}:
        raise ConfigurationError(f"未知取证阶段：{phase}")
    if phase == "official":
        if candidate_id is not None:
            raise ConfigurationError("official 阶段展开不得携带 candidate-id。")
        selected = _expand_phase_jobs(
            campaign_dir,
            manifest,
            phase,
            candidate_id=None,
            runtime_image=runtime_image,
            profile_id=profile_id,
            profile_digest=profile_digest,
            build_id=build_id,
            deployed_version=deployed_version,
            candidate_image_id=candidate_image_id,
            source_tree_sha256=source_tree_sha256,
            candidate_purpose=candidate_purpose,
        )
        _verify_plan_job_rows(manifest, selected, phase)
        return selected
    if not isinstance(candidate_id, str) or not SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("candidate 阶段展开必须给出合法的 candidate-id。")
    missing = [
        name
        for name in claude_upgrade_plan.CANDIDATE_CAMPAIGN_VARIABLES
        if not isinstance((manifest.get("configuration") or {}).get(name), str) or not manifest["configuration"][name]
    ]
    if missing:
        raise ConfigurationError(f"Claude Campaign 没有冻结候选变量 {missing}，不能展开 candidate 阶段任务。")
    del runtime_image, profile_id, profile_digest, build_id, deployed_version, candidate_image_id, source_tree_sha256, candidate_purpose
    _verify_plan_job_rows(manifest, _expand_phase_jobs(campaign_dir, manifest, phase, candidate_id=None), phase)
    return _expand_phase_jobs(campaign_dir, manifest, phase, candidate_id=candidate_id)


def _expand_phase_jobs(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    phase: str,
    *,
    candidate_id: str | None,
    **identity: Any,
) -> list[claude_upgrade.Job]:
    """按冻结场景清单副本展开全部 Job 并校验，返回指定阶段的 Job（``identity`` 是 official 阶段沿用的 Codex 同名参数）。"""

    arguments = claude_upgrade._campaign_arguments(campaign_dir, manifest, candidate_id=candidate_id, **identity)
    rules = _campaign_rules(manifest)
    path = Path(arguments.scenario_manifest)
    if path.is_symlink() or not path.is_file() or file_sha256(path) != arguments.scenario_manifest_sha256:
        raise ConfigurationError("Claude Campaign 冻结的目标场景清单副本缺失或摘要漂移。")
    payload = claude_upgrade_plan.read_scenario_manifest(
        path, expected_version=arguments.target_version, rules=rules, verify_source_spec=False
    )
    context = claude_upgrade._job_context(arguments)
    claude_upgrade_plan.validate_campaign_context(payload, context)
    jobs = claude_upgrade_plan.expand_scenario_jobs(payload, context, job_factory=claude_upgrade.Job, suite=arguments.suite)
    claude_upgrade_plan.validate_jobs(jobs, rules)
    selected = [job for job in jobs if job.phase == phase]
    if not selected:
        raise ConfigurationError(f"Claude 场景清单没有 {phase} 阶段任务。")
    return selected


# ---------------------------------------------------------------------------
# Job 执行摘要、工具依赖与增量元数据
# ---------------------------------------------------------------------------


def _job_execution_payload(job: claude_upgrade.Job) -> dict[str, Any]:
    """参与 Job 执行摘要的完整定义（步骤里的运行期变量保持占位，摘要因此稳定；含 probe_id 与运行期变量声明）。"""

    return {
        "id": job.job_id,
        "phase": job.phase,
        "suites": list(job.suites),
        "steps": [dict(step) for step in job.steps],
        "evidence_roots": list(job.evidence_roots),
        "required": job.required,
        "required_scenario_receipts": list(job.required_scenario_receipts),
    }


def _job_execution_sha256(job: claude_upgrade.Job) -> str:
    """服务面成员：Job 执行定义的摘要（不含规则映射与场景说明，与 Codex 同一取舍）。"""

    return claude_upgrade._fingerprint(_job_execution_payload(job))


def _normalized_tool_dependency(path: str) -> str:
    """场景清单的依赖路径相对 ``official_client_capture``；Claude 工具身份逐文件键带根目录名。"""

    return path if path.split("/", 1)[0] in MANAGED_ROOT_NAMES else f"{MANAGED_ROOT_NAMES[0]}/{path}"


def _tool_entry_digest_map(tool_identity: Mapping[str, Any]) -> dict[str, str]:
    entries = tool_identity.get("entries")
    if not isinstance(entries, list):
        raise ConfigurationError("Claude 工具身份缺少逐文件清单。")
    index: dict[str, str] = {}
    for entry in entries:
        path = entry.get("path") if isinstance(entry, Mapping) else None
        digest = entry.get("sha256") if isinstance(entry, Mapping) else None
        if not isinstance(path, str) or not isinstance(digest, str) or not SHA256_RE.fullmatch(digest) or path in index:
            raise ConfigurationError("Claude 工具身份逐文件条目非法。")
        index[path] = digest
    return index


def _job_tool_dependency_files(
    job: claude_upgrade.Job,
    tool_identity: Mapping[str, Any],
) -> dict[str, str]:
    """服务面成员：Job 显式声明的工具依赖 → 文件摘要（键为工具身份的逐文件键）。

    Claude 不做引用扫描：未声明、或声明的文件不在受管清单里即失败关闭。受管清单是两个受管根下的全部文件（含被 Claude
    身份策略忽略、不进分层摘要的文件，例如 ``drive_codex_model_catalog.py``），所以这些依赖同样逐文件取到摘要。
    """

    declared = tuple(getattr(job, "tool_dependencies", ()) or ())
    if not declared:
        raise ConfigurationError(f"Claude Job {job.job_id} 没有声明 tool_dependencies。")
    normalized = sorted({_normalized_tool_dependency(str(path)) for path in declared})
    if len(normalized) != len(declared):
        raise ConfigurationError(f"Claude Job {job.job_id} 的 tool_dependencies 归一到受管根后出现重复。")
    index = _tool_entry_digest_map(tool_identity)
    missing = [path for path in normalized if path not in index]
    if missing:
        raise ConfigurationError(f"Claude Job {job.job_id} 声明的工具依赖不在受管工具清单中：" + "、".join(missing))
    return {path: index[path] for path in normalized}


def _job_incremental_metadata(
    job: claude_upgrade.Job,
    identity: Mapping[str, Any] | None = None,
    tool_identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Job 可独立比较的依赖与缓存键（与 Codex 同口径的 result_key；Claude 没有组件概念）。

    合成 Job（例如 Job 演练的失败生命周期作业）没有工具依赖声明，依赖行为空；场景清单 Job 一律声明。
    """

    current_tool = tool_identity if tool_identity is not None else claude_upgrade._tool_identity(include_git=False)
    files = _job_tool_dependency_files(job, current_tool) if getattr(job, "tool_dependencies", ()) else {}
    rows = [
        {"id": "tool-file:" + hashlib.sha256(path.encode("utf-8")).hexdigest()[:24], "status": "passed", "result_sha256": digest}
        for path, digest in files.items()
    ]
    identity_sha = claude_upgrade._fingerprint(dict(identity)) if identity is not None else "0" * 64
    dependency_sha = incremental_recovery.dependency_digest(rows)
    input_sha = _job_execution_sha256(job)
    environment_sha = claude_upgrade._fingerprint({"client": CLIENT, "phase": job.phase, "identity_sha256": identity_sha})
    return {
        "tool_dependency_files": files,
        "dependency_sha256": dependency_sha,
        "input_sha256": input_sha,
        "environment_sha256": environment_sha,
        "result_key": incremental_recovery.result_key(
            component="job",
            item_id=job.job_id,
            input_sha256=input_sha,
            environment_sha256=environment_sha,
            dependency_sha256=dependency_sha,
        ),
    }


# ---------------------------------------------------------------------------
# 坐标布局：容器坐标 ↔ 宿主坐标
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureLayout:
    """一次取证的坐标布局：宿主数据根、容器内的数据根别名与工具执行副本、两个采集容器与运行镜像。

    ``capture_container`` 是联网的抓包容器（真实上游步骤），``synthetic_container`` 是与它同镜像、同挂载的断网容器
    （合成步骤）。两个容器挂载相同，所以容器坐标到宿主坐标的映射对两者一致。
    """

    host_data_root: Path
    capture_root: PurePosixPath
    capture_container: str
    capture_tool_root: PurePosixPath
    host_tool_root: Path
    runtime_image: str
    synthetic_container: str

    @property
    def host_runs_root(self) -> Path:
        return self.host_data_root / "runs"

    def containers(self) -> dict[str, str]:
        """角色 → 容器名（``capture``、``synthetic``）。"""

        return {ROLE_CAPTURE: self.capture_container, ROLE_SYNTHETIC: self.synthetic_container}

    def container_data_aliases(self) -> tuple[PurePosixPath, ...]:
        """容器内指向同一宿主数据根的别名：``capture_root`` 与登记 runs 别名的上级目录（``/capture``、``/root/oauth-capture``）。"""

        aliases = [self.capture_root]
        for runs_alias in claude_upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS:
            parent = PurePosixPath(runs_alias).parent
            if parent not in aliases:
                aliases.append(parent)
        return tuple(aliases)


def _host_data_root(campaign_dir: Path) -> Path:
    """从规范 ``<数据根>/evidence/campaigns/<id>`` 坐标恢复宿主数据根（与 VC-0 收口的宿主数据根同一规则）。"""

    campaign = Path(campaign_dir).resolve(strict=True)
    if campaign.parent.name != "campaigns" or campaign.parent.parent.name != "evidence":
        raise ConfigurationError("Campaign 不在规范的宿主 <数据根>/evidence/campaigns/<id> 下，无法映射容器坐标。")
    return campaign.parent.parent.parent


def _capture_layout(campaign_dir: Path, manifest: Mapping[str, Any]) -> CaptureLayout:
    """读取并核对坐标布局：两套映射来源（``capture_root`` 与登记的 runs 别名表）必须一致，否则失败证据归档会映射错位。"""

    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ConfigurationError("Claude Campaign 总计划缺少 configuration。")
    capture_root = str(configuration.get("capture_root", ""))
    tool_root = str(configuration.get("capture_tool_root", ""))
    container = str(configuration.get("capture_container", ""))
    synthetic_container = str(configuration.get("synthetic_capture_container", ""))
    runtime_image = str(configuration.get("runtime_image", ""))
    for label, value in (("capture_root", capture_root), ("capture_tool_root", tool_root)):
        if not SAFE_ABSOLUTE_PATH_RE.fullmatch(value) or ".." in PurePosixPath(value).parts:
            raise ConfigurationError(f"Claude Campaign configuration.{label} 不是安全绝对路径。")
    if not SAFE_ID_RE.fullmatch(container):
        raise ConfigurationError("Claude Campaign configuration.capture_container 非法。")
    if not SAFE_ID_RE.fullmatch(synthetic_container) or synthetic_container == container:
        raise ConfigurationError(
            "Claude Campaign configuration.synthetic_capture_container 必须是与 capture_container 不同的断网容器名。"
        )
    if not IMMUTABLE_IMAGE_RE.fullmatch(runtime_image):
        raise ConfigurationError("Claude Campaign configuration.runtime_image 必须是 repository@sha256 引用。")
    if PurePosixPath(tool_root).parts[-2:] != EXECUTION_TREE_SUFFIX:
        raise ConfigurationError("Claude Campaign configuration.capture_tool_root 必须以 tools/official_client_capture 结尾。")
    host_data_root = _host_data_root(campaign_dir)
    container_runs = PurePosixPath(capture_root) / "runs"
    if container_runs not in tuple(PurePosixPath(item) for item in claude_upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS):
        raise ConfigurationError(f"capture_root 的 runs 目录 {container_runs} 不是登记的容器 runs 别名。")
    registered_host_runs = Path(claude_upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT)
    if registered_host_runs.resolve(strict=False) != (host_data_root / "runs").resolve(strict=False):
        raise ConfigurationError("Campaign 所在宿主数据根与登记的失败证据宿主 runs 根不一致，容器坐标无法唯一映射。")
    return CaptureLayout(
        host_data_root=host_data_root,
        capture_root=PurePosixPath(capture_root),
        capture_container=container,
        capture_tool_root=PurePosixPath(tool_root),
        host_tool_root=Path(__file__).resolve().parent,
        runtime_image=runtime_image,
        synthetic_container=synthetic_container,
    )


def _step_route(argv: Sequence[str], layout: CaptureLayout) -> tuple[str, str]:
    """步骤的执行容器 → ``(角色, 容器名)``：真实上游步骤（冻结了 ``--upstream-ip``）只能在联网的抓包容器里执行，合成步骤
    只能在断网容器里执行。

    B2-05 在 plan 时已按同一规则校验 ``docker exec {capture_container}``／``{synthetic_capture_container}``；这里在执行与签发
    凭据时再核一次，任何偏离（包括 Campaign 冻结副本被改、容器名与布局不符）都在发出命令之前失败。判断依据取
    ``--upstream-ip`` 而不是请求确认参数，因为零请求演练会去掉确认参数，而 ``--upstream-ip`` 两种模式都保留。
    """

    command = [str(item) for item in argv]
    if _is_candidate_step(command):
        # 候选步骤（B4-01b）：宿主侧候选采集脚本包着中继候选模式命令，一律在联网的抓包容器里执行（网关经 Docker 网络连
        # 容器内的 connect 中继；合成计划的隔离由中继合成画像与网关账号临时代理保证，见 claude_upgrade_plan）。
        inner = command[2:]
        expected = layout.capture_container
        if command[1] != f"{layout.capture_tool_root}/{claude_upgrade_plan.CANDIDATE_SCRIPT}":
            raise ConfigurationError("候选步骤的宿主脚本不是冻结的工具执行副本里的候选采集脚本。")
        if len(inner) < 4 or Path(inner[0]).name != "docker" or inner[1] != "exec" or inner[2] != expected:
            raise ConfigurationError(f"候选步骤的中继命令必须在容器 {expected} 里执行。")
        return ROLE_CAPTURE, expected
    if len(command) < 4 or Path(command[0]).name != "docker" or command[1] != "exec":
        raise ConfigurationError("取证步骤必须以 docker exec <容器> 执行。")
    live = _argument_value(command, "--upstream-ip") is not None
    role = ROLE_CAPTURE if live else ROLE_SYNTHETIC
    expected = layout.containers()[role]
    if command[2] != expected:
        kind = "真实上游" if live else "合成"
        raise ConfigurationError(f"{kind}步骤必须在容器 {expected} 里执行，实际目标 {command[2]}。")
    return role, expected


def _is_candidate_step(argv: Sequence[str]) -> bool:
    """候选步骤的外形：``bash <工具根>/claude_run_candidate_capture.sh docker exec …``（B2-05 的候选步骤合同）。"""

    return (
        len(argv) >= 2
        and Path(str(argv[0])).name == "bash"
        and PurePosixPath(str(argv[1])).name == claude_upgrade_plan.CANDIDATE_SCRIPT
    )


def _host_step_argv(argv: Sequence[str], layout: CaptureLayout) -> list[str]:
    """执行用的 argv：候选步骤的宿主脚本是容器坐标（工具执行副本），映射到同一副本的宿主坐标后在宿主执行；其余原样。

    执行树核验（``_verify_execution_tree``）保证这份副本与受管树逐字一致；脚本经 ``dirname`` 找同一副本里的共享监督器。
    """

    command = [str(item) for item in argv]
    if not _is_candidate_step(command):
        return command
    return [command[0], str(_host_path(command[1], layout)), *command[2:]]


def _host_path(container_path: str | PurePosixPath, layout: CaptureLayout) -> Path:
    """容器坐标 → 宿主坐标：只接受落在数据根容器别名之下的规范绝对路径。"""

    value = PurePosixPath(str(container_path))
    if not value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts[1:]):
        raise ConfigurationError(f"容器坐标不是规范绝对路径：{container_path}")
    for alias in layout.container_data_aliases():
        try:
            relative = value.relative_to(alias)
        except ValueError:
            continue
        return layout.host_data_root.joinpath(*relative.parts)
    raise ConfigurationError(f"容器坐标不在数据根容器别名之下：{container_path}")


def _container_path(host_path: Path, layout: CaptureLayout) -> PurePosixPath:
    """宿主坐标 → 容器坐标（取 ``capture_root`` 别名）。"""

    try:
        relative = Path(host_path).resolve(strict=False).relative_to(layout.host_data_root.resolve(strict=False))
    except ValueError as error:
        raise ConfigurationError(f"宿主路径不在数据根内，容器里不可见：{host_path}") from error
    return layout.capture_root.joinpath(*relative.parts)


# ---------------------------------------------------------------------------
# 外部命令边界（探针／凭据签发走有界子进程；动作体走监督器）
# ---------------------------------------------------------------------------


def _run_probe_command(
    argv: Sequence[str],
    *,
    operation: str,
    timeout_seconds: float = PROBE_COMMAND_TIMEOUT_SECONDS,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """执行一条只读宿主命令（docker inspect／exec、宿主凭据签发）；错误消息不回显命令输出。"""

    command = [str(item) for item in argv]
    try:
        if deadline is not None:
            completed = incremental_recovery.run_bounded_subprocess(
                command,
                timeout=timeout_seconds,
                deadline=deadline,
                operation=operation,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                heartbeat=heartbeat,
            )
        else:
            completed = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=timeout_seconds,
                shell=False,
            )
    except incremental_recovery.WallClockTimeoutError:
        raise
    except (OSError, subprocess.SubprocessError) as error:
        raise ConfigurationError(f"{operation} 执行失败：{type(error).__name__}") from error
    if check and completed.returncode != 0:
        raise ConfigurationError(f"{operation} 执行失败（退出码 {completed.returncode}）。")
    return completed


def _run_step_command(
    argv: Sequence[str],
    *,
    operation: str,
    timeout_seconds: float,
    environment: Mapping[str, str],
    log_stream: Any,
    job_id: str,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> int:
    """执行动作体的一步：活跃租约下经独立监督器 ``SupervisorClient.run_command``；否则独立进程组、受 deadline 约束。

    无租约时单步超时回收整个进程组并返回 124（与 Codex 同一口径）；全局 deadline 到期抛 ``WallClockTimeoutError``。
    """

    lease = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
    command = [str(item) for item in argv]
    if lease is not None and callable(getattr(lease, "run_command", None)):
        completed = lease.run_command(
            command,
            operation=operation,
            timeout_seconds=float(timeout_seconds),
            env=dict(environment),
            output_stream=log_stream,
            heartbeat_callback=heartbeat,
            job_id=job_id,
        )
        return int(completed.returncode)
    process = subprocess.Popen(
        command,
        env=dict(environment),
        stdin=subprocess.DEVNULL,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    started = time.monotonic()
    interval = float(getattr(deadline, "heartbeat_seconds", claude_upgrade.DEFAULT_HEARTBEAT_SECONDS)) if deadline else 1.0
    try:
        while True:
            if deadline is not None:
                deadline.check(operation)
            remaining_step = float(timeout_seconds) - (time.monotonic() - started)
            if remaining_step <= 0:
                incremental_recovery.terminate_process_group(process, deadline=deadline)
                return 124
            wait_for = min(remaining_step, max(0.05, interval))
            if deadline is not None:
                wait_for = min(wait_for, max(0.001, deadline.remaining_seconds))
            try:
                return int(process.wait(timeout=max(0.001, wait_for)))
            except subprocess.TimeoutExpired:
                if heartbeat is not None:
                    heartbeat(operation)
    except BaseException:
        incremental_recovery.terminate_process_group(process, deadline=deadline)
        raise


# ---------------------------------------------------------------------------
# attempt deadline、心跳与超时 checkpoint（与 Codex 同一语义，合同名为 Claude）
# ---------------------------------------------------------------------------


def _bind_attempt_deadline_metadata(
    deadline: incremental_recovery.WallClockDeadline,
    phase: str,
) -> incremental_recovery.WallClockDeadline:
    """给复用 deadline 绑定 attempt 元数据，不改动任何时间锚。"""

    if phase not in {"official", "candidate"}:
        raise ConfigurationError("capture attempt deadline phase 非法。")
    deadline.phase = phase  # type: ignore[attr-defined]
    if not hasattr(deadline, "last_completed_job_id"):
        deadline.last_completed_job_id = None  # type: ignore[attr-defined]
    return deadline


def _attempt_deadline(
    arguments: argparse.Namespace,
    phase: str,
) -> incremental_recovery.WallClockDeadline:
    """服务面成员：为一次 attempt 建立唯一的全阶段 deadline（预算与心跳上限与 Codex 相同）。"""

    raw_budget = getattr(arguments, "max_wall_seconds", None)
    budget = claude_upgrade.DEFAULT_ATTEMPT_WALL_SECONDS if raw_budget is None else raw_budget
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise ConfigurationError("--max-wall-seconds 必须是整数")
    if budget <= 0 or budget > claude_upgrade.MAX_ATTEMPT_WALL_SECONDS:
        raise ConfigurationError(f"--max-wall-seconds 必须在 1～{claude_upgrade.MAX_ATTEMPT_WALL_SECONDS} 秒之间")
    raw_heartbeat = getattr(arguments, "heartbeat_seconds", None)
    heartbeat = claude_upgrade.DEFAULT_HEARTBEAT_SECONDS if raw_heartbeat is None else raw_heartbeat
    if isinstance(heartbeat, bool) or not isinstance(heartbeat, int):
        raise ConfigurationError("--heartbeat-seconds 必须是整数")
    if heartbeat <= 0 or heartbeat > claude_upgrade.MAX_HEARTBEAT_SECONDS:
        raise ConfigurationError(f"--heartbeat-seconds 必须在 1～{claude_upgrade.MAX_HEARTBEAT_SECONDS} 秒之间")
    try:
        deadline = incremental_recovery.WallClockDeadline(budget, label=f"{phase}-capture-attempt")
    except incremental_recovery.IncrementalRecoveryError as error:
        raise ConfigurationError(str(error)) from error
    deadline.heartbeat_seconds = heartbeat  # type: ignore[attr-defined]
    _bind_attempt_deadline_metadata(deadline, phase)
    return deadline


def _write_attempt_heartbeat(
    path: Path,
    deadline: incremental_recovery.WallClockDeadline,
    *,
    operation: str,
    last_completed_job_id: str | None = None,
    force: bool = False,
    allow_expired: bool = False,
    attempt_root: Path | None = None,
) -> None:
    """原子更新不含秘密的 attempt 心跳，并同步活跃租约（租约失败立即向上抛出）。"""

    now = time.monotonic()
    last = getattr(deadline, "_last_heartbeat_monotonic", None)
    interval = float(getattr(deadline, "heartbeat_seconds", claude_upgrade.DEFAULT_HEARTBEAT_SECONDS))
    if not force and isinstance(last, (int, float)) and now - last < interval:
        return
    if not allow_expired:
        deadline.check(operation)
    operation = claude_upgrade._normalize_watchdog_operation(operation)
    lease = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
    if lease is not None:
        lease.heartbeat(operation, force=force, allow_expired=allow_expired)
    if last_completed_job_id is not None and not SAFE_ID_RE.fullmatch(str(last_completed_job_id)):
        raise ConfigurationError("watchdog last_completed_job_id 非法。")
    if attempt_root is not None:
        attempt_root = attempt_root.resolve(strict=True)
        if (
            not attempt_root.is_dir()
            or path.is_symlink()
            or path.parent.resolve(strict=False) != attempt_root
            or path.name != "watchdog-heartbeat.json"
        ):
            raise ConfigurationError("watchdog heartbeat 必须位于当前 attempt 根目录。")
    if last_completed_job_id is not None:
        deadline.last_completed_job_id = last_completed_job_id  # type: ignore[attr-defined]
    secure_write_json(
        path,
        {
            "schema_version": WATCHDOG_HEARTBEAT_SCHEMA,
            "phase": str(getattr(deadline, "phase", "stage")),
            "operation": str(operation)[:256],
            "elapsed_seconds": round(deadline.elapsed_seconds, 3),
            "remaining_seconds": round(deadline.remaining_seconds, 3),
            "last_completed_job_id": getattr(deadline, "last_completed_job_id", None),
            "updated_at_utc": claude_upgrade._utc_now(),
        },
    )
    deadline._last_heartbeat_monotonic = now  # type: ignore[attr-defined]


def _write_timeout_checkpoint(
    attempt_root: Path,
    deadline: incremental_recovery.WallClockDeadline,
    *,
    operation: str,
    last_completed_job_id: str | None = None,
) -> Path:
    """以不可覆盖方式保存超时终态；写失败由调用方继续停线。"""

    attempt_root = attempt_root.resolve(strict=True)
    if last_completed_job_id is not None and not SAFE_ID_RE.fullmatch(str(last_completed_job_id)):
        raise ConfigurationError("timeout checkpoint 的 Job 身份非法。")
    path = attempt_root / "timeout-checkpoint.json"
    claude_upgrade._secure_write_json_once(
        path,
        {
            "schema_version": WATCHDOG_CHECKPOINT_SCHEMA,
            "status": "timeout",
            "phase": str(getattr(deadline, "phase", "stage")),
            "operation": claude_upgrade._normalize_watchdog_operation(operation)[:256],
            "elapsed_seconds": round(deadline.elapsed_seconds, 3),
            "remaining_seconds": round(deadline.remaining_seconds, 3),
            "budget_seconds": deadline.budget_seconds,
            "last_completed_job_id": last_completed_job_id or getattr(deadline, "last_completed_job_id", None),
            "recorded_at_utc": claude_upgrade._utc_now(),
        },
    )
    return path


def _wait_for_monitor_finalization(run_dir: Path, state: Mapping[str, Any]) -> dict[str, Any]:
    """服务面成员：monitor 仍在线时给它有界时间封存终态，避免与入口重复追加事件（与 Codex 同一语义）。"""

    monitor_pid = state.get("monitor_pid")
    if not isinstance(monitor_pid, int) or isinstance(monitor_pid, bool) or monitor_pid <= 0:
        return dict(state)
    deadline = time.monotonic() + STAGING_ORPHAN_MONITOR_WAIT_SECONDS
    current = dict(state)
    while time.monotonic() < deadline:
        current = codex_upgrade_supervisor._read_state(run_dir)
        if current.get("state") in codex_upgrade_supervisor.TERMINAL_STATES:
            return current
        if not codex_upgrade_supervisor._owner_alive(monitor_pid):
            return current
        time.sleep(0.1)
    current = codex_upgrade_supervisor._read_state(run_dir)
    if current.get("state") in codex_upgrade_supervisor.ACTIVE_STATES and codex_upgrade_supervisor._owner_alive(monitor_pid):
        raise ConfigurationError(f"父 run {run_dir.name} 的 monitor 仍在线且尚未封存终态，请稍后重试派发。")
    return current


# ---------------------------------------------------------------------------
# 环境探针（Claude 自带，只读；不导入被 Claude 策略忽略的 Codex 探针模块）
# ---------------------------------------------------------------------------

# 在采集容器内执行的一次性只读脚本：hosts 行、文件摘要／元数据、Claude 状态目录清单、取证进程标签与监听端口。
# 只读 Claude 状态与凭据文件的元数据（存在、大小、mtime、inode），不读内容；CA 签名 PEM（含私钥）同样只记元数据。
_CONTAINER_PROBE_SCRIPT = r"""
import hashlib, json, os, stat, sys
args = json.loads(sys.argv[1])
def meta(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return {"path": path, "exists": False}
    kind = "link" if stat.S_ISLNK(st.st_mode) else "dir" if stat.S_ISDIR(st.st_mode) else "file" if stat.S_ISREG(st.st_mode) else "other"
    return {"path": path, "exists": True, "type": kind, "size": st.st_size, "mtime_ns": st.st_mtime_ns, "inode": st.st_ino, "mode": stat.S_IMODE(st.st_mode)}
def digest(path):
    try:
        with open(path, "rb") as handle:
            value = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1048576), b""):
                value.update(chunk)
        return value.hexdigest()
    except FileNotFoundError:
        return None
def listing(root, limit):
    rows, truncated = [], False
    if not os.path.isdir(root) or os.path.islink(root):
        return rows, truncated
    for current, directories, files in os.walk(root):
        directories.sort()
        for name in sorted(directories + files):
            full = os.path.join(current, name)
            item = meta(full)
            item["path"] = os.path.relpath(full, root)
            rows.append(item)
            if len(rows) >= limit:
                return rows, True
    return rows, truncated
with open("/etc/hosts", encoding="utf-8", errors="replace") as handle:
    hosts = handle.read().splitlines()
own = {os.getpid(), os.getppid()}
processes = []
for name in os.listdir("/proc"):
    if not name.isdigit() or int(name) in own:
        continue
    try:
        with open(f"/proc/{name}/cmdline", "rb") as handle:
            raw = handle.read()
    except OSError:
        continue
    parts = [item.decode("utf-8", "replace") for item in raw.split(b"\0") if item]
    if parts:
        processes.append(parts)
labels = set()
for parts in processes:
    first = os.path.basename(parts[0])
    if first in args["binary_labels"]:
        labels.add(args["binary_labels"][first])
    for item in parts:
        base = os.path.basename(item)
        if base in args["script_labels"]:
            labels.add(args["script_labels"][base])
listeners = set()
for family in ("tcp", "tcp6"):
    try:
        with open(f"/proc/net/{family}", encoding="ascii") as handle:
            next(handle)
            for line in handle:
                fields = line.split()
                if len(fields) > 3 and fields[3] == "0A":
                    listeners.add((family, int(fields[1].rsplit(":", 1)[1], 16)))
    except (OSError, StopIteration):
        pass
state, truncated = listing(args["state_dir"], args["listing_limit"])
print(json.dumps({
    "hosts_lines": hosts,
    "resolv_conf": {"exists": os.path.isfile("/etc/resolv.conf"), "sha256": digest("/etc/resolv.conf")},
    "ca_bundle": {"exists": os.path.isfile("/etc/ssl/certs/ca-certificates.crt"), "sha256": digest("/etc/ssl/certs/ca-certificates.crt")},
    "mitm_ca_cert": dict(meta(args["ca_cert"]), sha256=digest(args["ca_cert"])),
    "mitm_ca_signing_pem": meta(args["ca_signing_pem"]),
    "claude_bin": meta(args["claude_bin"]),
    "global_state": meta(args["global_state"]),
    "credentials_file": meta(args["credentials_file"]),
    "state_dir": {"path": args["state_dir"], "entries": state, "truncated": truncated},
    "capture_processes": sorted(labels),
    "tcp_listen_ports": [{"family": family, "port": port} for family, port in sorted(listeners)],
}, sort_keys=True))
"""


def _docker_inspect(container: str, *, deadline: Any | None, heartbeat: Any | None) -> dict[str, Any]:
    completed = _run_probe_command(["docker", "inspect", container], operation=f"probe:{container}:inspect", deadline=deadline, heartbeat=heartbeat)
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ConfigurationError("docker inspect 未返回合法 JSON。") from error
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise ConfigurationError(f"docker inspect {container} 结果非法。")
    return value[0]


def _container_record(role: str, container: str, item: Mapping[str, Any]) -> dict[str, Any]:
    """一个采集容器的 inspect 快照（角色、身份、网络与挂载）；断网容器的 ``networks`` 只应有 ``none`` 且没有地址。"""

    state = item.get("State") if isinstance(item.get("State"), Mapping) else {}
    config = item.get("Config") if isinstance(item.get("Config"), Mapping) else {}
    host = item.get("HostConfig") if isinstance(item.get("HostConfig"), Mapping) else {}
    networks = (item.get("NetworkSettings") or {}).get("Networks") if isinstance(item.get("NetworkSettings"), Mapping) else {}
    mounts = item.get("Mounts") if isinstance(item.get("Mounts"), list) else []
    container_id = str(item.get("Id", ""))
    if not CONTAINER_ID_RE.fullmatch(container_id) or not IMAGE_ID_RE.fullmatch(str(item.get("Image", ""))):
        raise ConfigurationError(f"采集容器 {container} 的 ID 或 image ID 格式非法。")
    return {
        "role": role,
        "name": container,
        "id": container_id,
        "image_id": str(item.get("Image", "")),
        "image_reference": str(config.get("Image", "")),
        "status": str(state.get("Status", "")),
        "running": state.get("Running") is True,
        "started_at": str(state.get("StartedAt", "")),
        "restart_count": int(item.get("RestartCount", 0) or 0),
        "privileged": host.get("Privileged") is True,
        "network_mode": str(host.get("NetworkMode", "")),
        "networks": {
            str(name): str(binding.get("IPAddress", ""))
            for name, binding in sorted((networks or {}).items())
            if isinstance(binding, Mapping)
        },
        "mounts": sorted(
            (
                {
                    "destination": str(mount.get("Destination", "")),
                    "source": str(mount.get("Source", "")),
                    "rw": mount.get("RW") is True,
                    "type": str(mount.get("Type", "")),
                }
                for mount in mounts
                if isinstance(mount, Mapping)
            ),
            key=lambda row: (row["destination"], row["source"]),
        ),
    }


def _probe_targets(configuration: Mapping[str, Any]) -> list[tuple[str, str]]:
    """探针目标：``[(capture, 抓包容器), (synthetic, 断网容器)]``；缺任一容器名即失败关闭（不悄悄只探一个）。"""

    targets: list[tuple[str, str]] = []
    for role, field in CONTAINER_ROLE_FIELDS.items():
        name = str(configuration.get(field, ""))
        if not SAFE_ID_RE.fullmatch(name):
            raise ConfigurationError(f"环境探针缺少 {field}（两个采集容器都要探）。")
        targets.append((role, name))
    if targets[0][1] == targets[1][1]:
        raise ConfigurationError("环境探针的抓包容器与断网容器不能是同一个容器。")
    return targets


def _hosts_digest(lines: Sequence[str], container_id: str) -> tuple[str, int]:
    """hosts 行排序后取摘要，剔除 Docker 写给容器自身的 ``<IP> <容器 ID 前 12 位>`` 行（与 Codex 探针同一吸收规则）。"""

    self_names = {container_id, container_id[:12]}
    retained = [line for line in lines if not (len(line.split()) == 2 and line.split()[1] in self_names)]
    payload = "".join(f"{line}\n" for line in sorted(retained))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), len(retained)


def _collect_probe_states(
    manifest: Mapping[str, Any],
    *,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, dict[str, Any]]:
    """只读采集五类快照：两个采集容器各一次 ``docker inspect`` 加一次 ``docker exec`` 只读脚本（离线测试替换本函数）。

    每类快照按角色分开记录（``containers`` 是带角色的列表，其余是 ``targets.<角色>``）；``processes`` 的标签带角色前缀
    （例如 ``synthetic:relay``），任一容器里残留取证进程都让它非空。
    """

    configuration = manifest["configuration"]
    arguments = {
        "ca_cert": str(configuration["ca_cert"]),
        "ca_signing_pem": str(configuration["ca_signing_pem"]),
        "claude_bin": str(configuration["claude_bin"]),
        "credentials_file": str(configuration["credentials_file"]),
        "global_state": CLAUDE_GLOBAL_STATE_FILE,
        "state_dir": CLAUDE_STATE_DIR,
        "listing_limit": CLAUDE_STATE_LISTING_LIMIT,
        "binary_labels": CAPTURE_PROCESS_BINARY_LABELS,
        "script_labels": CAPTURE_PROCESS_SCRIPT_LABELS,
    }
    records: list[dict[str, Any]] = []
    configurations: dict[str, Any] = {}
    clients: dict[str, Any] = {}
    listeners: dict[str, Any] = {}
    processes: list[str] = []
    for role, container in _probe_targets(configuration):
        record = _container_record(role, container, _docker_inspect(container, deadline=deadline, heartbeat=heartbeat))
        completed = _run_probe_command(
            ["docker", "exec", container, "python3", "-c", _CONTAINER_PROBE_SCRIPT, json.dumps(arguments, sort_keys=True)],
            operation=f"probe:{container}:read-only-state",
            deadline=deadline,
            heartbeat=heartbeat,
        )
        try:
            observed = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise ConfigurationError(f"采集容器 {container} 的只读探针未返回合法 JSON。") from error
        if not isinstance(observed, Mapping):
            raise ConfigurationError(f"采集容器 {container} 的只读探针结果不是对象。")
        hosts_sha256, hosts_lines = _hosts_digest(observed.get("hosts_lines") or [], record["id"])
        records.append(record)
        configurations[role] = {
            "hosts_sha256": hosts_sha256,
            "hosts_line_count": hosts_lines,
            "resolv_conf": observed.get("resolv_conf"),
            "ca_bundle": observed.get("ca_bundle"),
            "mitm_ca_cert": observed.get("mitm_ca_cert"),
            "mitm_ca_signing_pem": observed.get("mitm_ca_signing_pem"),
        }
        clients[role] = {
            "claude_bin": observed.get("claude_bin"),
            "global_state": observed.get("global_state"),
            "credentials_file": observed.get("credentials_file"),
            "state_dir": observed.get("state_dir"),
        }
        listeners[role] = list(observed.get("tcp_listen_ports") or [])
        processes.extend(f"{role}:{label}" for label in observed.get("capture_processes") or [])
    return {
        "containers": {"probe_kind": "containers", "containers": records},
        "configuration": {"probe_kind": "configuration", "hosts_digest_mode": HOSTS_DIGEST_MODE, "targets": configurations},
        "client": {"probe_kind": "client", "targets": clients},
        "processes": {"probe_kind": "processes", "capture_processes": sorted(processes)},
        "listeners": {"probe_kind": "listeners", "targets": listeners},
    }


def _exclusive_write_bytes(path: Path, payload: bytes) -> None:
    """以 0600 临时文件加独占硬链接原子落盘，永不覆盖已有结果。"""

    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ConfigurationError(f"探针输出已存在，拒绝覆盖：{path.name}") from error
        path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _probe_capture_environment(
    manifest: dict[str, Any],
    output_dir: Path,
    phase: str,
    *,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """服务面成员：采一次只读环境探针，独占写入五份快照与 ``probe-manifest.json``（共享恢复预览以同一函数复采）。"""

    if phase not in {"before", "after"}:
        raise ConfigurationError("环境探针 phase 只能是 before 或 after。")
    if deadline is not None:
        deadline.check(f"environment-probe:{phase}:start")
    output_dir = Path(output_dir)
    if output_dir.is_symlink() or (output_dir.exists() and not output_dir.is_dir()):
        raise ConfigurationError("环境探针输出路径不可信。")
    for name in (*PROBE_STATE_FILES.values(), PROBE_MANIFEST_NAME):
        if (output_dir / name).exists() or (output_dir / name).is_symlink():
            raise ConfigurationError(f"环境探针输出已存在，拒绝覆盖：{name}")
    states = _collect_probe_states(manifest, deadline=deadline, heartbeat=heartbeat)
    if set(states) != set(PROBE_KINDS):
        raise ConfigurationError("环境探针快照种类不闭合。")
    if not output_dir.parent.is_dir():
        raise ConfigurationError(f"环境探针输出的上级目录不存在：{output_dir.parent}")
    output_dir.mkdir(exist_ok=True, mode=0o700)
    output_dir.chmod(0o700)
    snapshots: list[dict[str, Any]] = []
    for kind in PROBE_KINDS:
        payload = _canonical_bytes(states[kind])
        path = output_dir / PROBE_STATE_FILES[kind]
        _exclusive_write_bytes(path, payload)
        comparison: dict[str, Any] = {"mode": "byte_equal"}
        if kind in EXPECTED_EMPTY_PROBE_KINDS:
            comparison["rule"] = "expected_empty"
        snapshots.append(
            {
                "kind": kind,
                "path": path.name,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "bytes": len(payload),
                "comparison": comparison,
            }
        )
    probe = {
        "schema_version": PROBE_MANIFEST_SCHEMA,
        "client": CLIENT,
        "phase": phase,
        "observed_at_utc": claude_upgrade._utc_now(),
        "targets": dict(_probe_targets(manifest["configuration"])),
        "snapshots": snapshots,
        "capture_processes": list(states["processes"]["capture_processes"]),
    }
    _exclusive_write_bytes(output_dir / PROBE_MANIFEST_NAME, _canonical_bytes(probe))
    if deadline is not None:
        deadline.check(f"environment-probe:{phase}:complete")
    return probe


def _probe_snapshot_digests(manifest: Mapping[str, Any]) -> dict[str, str]:
    """服务面成员：探针清单 → 快照种类到摘要（与 Codex 同一读取口径）。"""

    digests: dict[str, str] = {}
    for snapshot in manifest.get("snapshots") or []:
        if not isinstance(snapshot, dict):
            continue
        kind = snapshot.get("kind")
        digest = snapshot.get("sha256")
        if isinstance(kind, str) and isinstance(digest, str):
            digests[kind] = digest
    return digests


def _probe_drift(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """前后探针的漂移种类：逐字不等的快照，加上任一侧存在残留取证进程（processes 必须为空）。"""

    previous = _probe_snapshot_digests(before)
    current = _probe_snapshot_digests(after)
    drifted = [kind for kind in PROBE_KINDS if previous.get(kind) is None or previous.get(kind) != current.get(kind)]
    if (before.get("capture_processes") or after.get("capture_processes")) and "processes" not in drifted:
        drifted.append("processes")
    return drifted


def _probe_binding(root: Path, path: Path) -> dict[str, Any]:
    return {"path": path.relative_to(root).as_posix(), "sha256": file_sha256(path), "bytes": path.stat().st_size}


# ---------------------------------------------------------------------------
# 执行前提：执行树、官方二进制、容器内凭据与 CA 文件
# ---------------------------------------------------------------------------


def _verify_execution_tree(capture_root: Path | None) -> None:
    """服务面成员：采集实际执行的工具副本（``<capture_root>/tools/official_client_capture``）与受管树逐字一致。

    语义与 Codex 相同：执行位置不存在时放行（那是路径配置问题，执行时自然失败），存在但有文件缺失或摘要不符即拒绝。
    Claude 的执行副本坐标是 ``configuration.capture_tool_root``，调用方先把它映射成宿主坐标再取上两级目录传入。
    """

    if capture_root is None:
        return
    managed_root = Path(__file__).resolve().parent
    execution_root = Path(capture_root) / "tools" / "official_client_capture"
    try:
        if not execution_root.is_dir() or execution_root.is_symlink():
            return
    except OSError:
        return
    if execution_root.resolve() == managed_root.resolve():
        return
    actual = {entry["path"]: entry["sha256"] for entry in claude_upgrade._tool_tree_entries(execution_root)}
    drift = [entry["path"] for entry in claude_upgrade._tool_tree_entries(managed_root) if actual.get(entry["path"]) != entry["sha256"]]
    if drift:
        raise ConfigurationError(
            f"采集执行位置与受管工具树不一致，实际会跑到未受校验的副本：{execution_root}；共 {len(drift)} 个文件漂移，前几个："
            + "、".join(drift[:5])
        )


def _container_image_reference_verified(
    container: str,
    runtime_image: str,
    *,
    deadline: Any | None,
    heartbeat: Any | None,
) -> str:
    """运行容器的 config image ID 与其 RepoDigests 必须包含冻结的不可变引用。"""

    image_id = _run_probe_command(
        ["docker", "inspect", "--format", "{{.Image}}", container],
        operation=f"official-binary:{container}:image-id",
        deadline=deadline,
        heartbeat=heartbeat,
    ).stdout.strip()
    if not IMAGE_ID_RE.fullmatch(image_id):
        raise ConfigurationError("无法读取运行容器的实际 image ID。")
    raw = _run_probe_command(
        ["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image_id],
        operation=f"official-binary:{container}:repo-digests",
        deadline=deadline,
        heartbeat=heartbeat,
    ).stdout
    try:
        digests = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigurationError("运行镜像 RepoDigests 不是合法 JSON。") from error
    if not isinstance(digests, list) or runtime_image not in digests:
        raise ConfigurationError("冻结的 runtime_image 不是运行镜像实际 RepoDigests 中的不可变引用。")
    return image_id


def _verify_official_binaries(
    manifest: dict[str, Any],
    *,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """服务面成员：任何真实请求之前核对目标客户端的三处安装、断网容器与运行镜像。

    * 抓包容器里实际执行的 ``/opt/claude-<v>/claude``：复用 B1-10 已验收的版本探针（隔离空 HOME 的 ``--version`` 恰为
      ``<v> (Claude Code)``，探针前后容器内 sha256sum 一致），摘要必须等于 ``target_sha256``；
    * 断网容器（合成步骤）：调 ARM64 环境收据版本 2 的同一份核验 ``_synthetic_container_observation``——存在且运行、
      ``network_mode`` 为 none 且没有地址、与抓包容器同镜像、去掉 Docker 控制面后挂载逐项相同、不共用逐容器文件，
      容器内同一路径的二进制版本自报一致、摘要同为 ``target_sha256``；
    * 宿主 ``/opt/claude-<v>/claude``（B5a-07 实体安装）：摘要同为 ``target_sha256``；
    * 运行容器的镜像 RepoDigests 含冻结的 ``runtime_image``（断网容器与它同一 image ID）。
    """

    configuration = manifest["configuration"]
    target_version = str(manifest["target_version"])
    target_sha256 = str(manifest["target_sha256"])
    official = manifest.get("official_identity")
    if not isinstance(official, Mapping) or official.get("binary_sha256") != target_sha256:
        raise ConfigurationError("Campaign official_identity.binary_sha256 与 target_sha256 不一致。")
    try:
        target = claude_arm64.client_binary_target(target_version)
    except claude_arm64.Arm64EnvironmentReceiptError as error:
        raise ConfigurationError(str(error)) from error
    if configuration.get("capture_container") != target["container"] or configuration.get("claude_bin") != target["path"]:
        raise ConfigurationError(
            f"Claude 取证坐标必须是容器 {target['container']} 里的 {target['path']}（B5a-07 安装形态），"
            f"实际 {configuration.get('capture_container')}:{configuration.get('claude_bin')}。"
        )
    if configuration.get("synthetic_capture_container") != claude_arm64.SYNTHETIC_CONTAINER:
        raise ConfigurationError(
            f"合成步骤的断网容器必须是 {claude_arm64.SYNTHETIC_CONTAINER}（与 ARM64 环境收据版本 2 同一合同），"
            f"实际 {configuration.get('synthetic_capture_container')}。"
        )
    if deadline is not None:
        deadline.check("official-binary:start")
    image_id = _container_image_reference_verified(
        str(configuration["capture_container"]), str(configuration["runtime_image"]), deadline=deadline, heartbeat=heartbeat
    )
    try:
        with claude_arm64._shared_deadline_scope(deadline, heartbeat):
            client_binary, probe = claude_arm64._client_binary_observation(target)
    except claude_arm64.Arm64EnvironmentReceiptError as error:
        raise ConfigurationError(f"容器内目标客户端核验失败：{error}") from error
    if client_binary.get("sha256") != target_sha256:
        raise ConfigurationError("容器内目标客户端摘要与 target_sha256 不一致。")
    try:
        with claude_arm64._shared_deadline_scope(deadline, heartbeat):
            synthetic = claude_arm64._synthetic_container_observation(target_version)
    except claude_arm64.Arm64EnvironmentReceiptError as error:
        raise ConfigurationError(f"断网容器核验失败：{error}") from error
    synthetic_binary = synthetic.get("client_binary") if isinstance(synthetic.get("client_binary"), Mapping) else {}
    if synthetic_binary.get("sha256") != target_sha256 or dict(synthetic_binary) != dict(client_binary):
        raise ConfigurationError("断网容器里的目标客户端摘要与 target_sha256 或抓包容器里的那份不一致。")
    if synthetic.get("image_id") != image_id or synthetic.get("name") != configuration.get("synthetic_capture_container"):
        raise ConfigurationError("断网容器与已核对 RepoDigests 的运行镜像不是同一 image ID，或容器名不符。")
    host_binary = Path(HOST_CLIENT_BINARY_TEMPLATE.format(version=target_version))
    if host_binary.is_symlink() or not host_binary.is_file():
        raise ConfigurationError(f"宿主机缺少目标客户端实体安装：{host_binary}（B5a-07）")
    host_sha256 = file_sha256(host_binary)
    if host_sha256 != target_sha256:
        raise ConfigurationError("宿主机目标客户端摘要与 target_sha256 不一致。")
    if deadline is not None:
        deadline.check("official-binary:complete")
    return {
        "schema_version": BINARY_VERIFICATION_SCHEMA,
        "target_version": target_version,
        "target_sha256": target_sha256,
        "container_binary": dict(client_binary),
        "container_probe": dict(probe),
        "synthetic_container": {
            key: synthetic[key]
            for key in (
                "name", "container_id", "image_id", "network_mode", "network_names", "reference_container",
                "reference_container_id", "shared_mounts_sha256", "shared_mount_count", "excluded_reference_mounts",
                "client_binary", "client_binary_probe",
            )
        },
        "host_binary": {"path": str(host_binary), "sha256": host_sha256},
        "runtime_image": {"reference": str(configuration["runtime_image"]), "image_id": image_id},
        "verified_at_utc": claude_upgrade._utc_now(),
    }


def _planned_global_state_file(jobs: Sequence[Any]) -> str:
    """计划步骤里 relay 读取的全局状态文件：步骤显式给 ``--claude-global-state-file`` 时取它，否则取 relay 缺省值。

    全部步骤必须指向同一个文件，否则前提检查与探针覆盖不到其余路径，失败关闭。
    """

    paths = {
        _argument_value([str(item) for item in step.get("argv") or []], GLOBAL_STATE_FLAG) or CLAUDE_GLOBAL_STATE_FILE
        for job in jobs
        for step in getattr(job, "steps", ())
    }
    if len(paths) > 1:
        raise ConfigurationError(f"计划步骤的全局状态文件不唯一：{sorted(paths)}")
    path = next(iter(paths), CLAUDE_GLOBAL_STATE_FILE)
    if not SAFE_ABSOLUTE_PATH_RE.fullmatch(path) or ".." in PurePosixPath(path).parts:
        raise ConfigurationError("计划步骤的全局状态文件不是安全绝对路径。")
    return path


def _container_preconditions(
    manifest: Mapping[str, Any],
    layout: CaptureLayout,
    *,
    mode: str,
    jobs: Sequence[Any] = (),
    deadline: Any | None,
    heartbeat: Any | None,
    phase: str = "official",
) -> dict[str, Any]:
    """两个采集容器里取证必需文件的存在性，以及全局状态在两边是否相同。正式 run 缺任一项或不同即在预约前失败；演练只记录。

    * 凭据、CA 签名 PEM、CA 证书、全局状态（relay 读 ``/root/.claude.json``：抓包容器的 TUI／账号状态 probe 与断网容器的
      OAuth refresh probe 都要它）：``test -f``，只看是否存在；
    * 全局状态不在共享挂载里（容器私有层），两个容器各有一份：两边都在时各在容器内取 sha256 比较，结果只记布尔值，
      不记内容或摘要。凭据与 CA 在共享挂载里，同一份由断网容器的挂载核对（``_verify_official_binaries``）保证。
    * 候选阶段（B4-01b）：候选步骤只在抓包容器里执行，客户端只持有网关 API Key、TUI 用内存临时 home，不读官方凭据与
      全局状态；只核抓包容器里的 CA 签名 PEM 与 CA 证书（connect 中继签叶证书、宿主脚本把 CA 装进服务容器）。
    """

    configuration = manifest["configuration"]
    candidate = phase == "candidate"
    global_state = CLAUDE_GLOBAL_STATE_FILE if candidate else _planned_global_state_file(jobs)
    names = ("ca_signing_pem", "ca_cert") if candidate else ("credentials_file", "ca_signing_pem", "ca_cert")
    files = {name: str(configuration[name]) for name in names}
    if not candidate:
        files["global_state"] = global_state
    roles = {ROLE_CAPTURE: layout.capture_container} if candidate else layout.containers()
    targets: dict[str, Any] = {}
    missing: list[str] = []
    for role, container in roles.items():
        observed: dict[str, Any] = {}
        for name, path in files.items():
            completed = _run_probe_command(
                ["docker", "exec", container, "test", "-f", path],
                operation=f"precondition:{role}:{name}",
                deadline=deadline,
                heartbeat=heartbeat,
                check=False,
            )
            observed[name] = {"path": path, "present": completed.returncode == 0}
            if completed.returncode != 0:
                missing.append(f"{role}:{name}")
        targets[role] = {"container": container, "files": observed}
    identical: bool | None = None
    if candidate:
        if mode == MODE_FORMAL and missing:
            raise ConfigurationError(f"抓包容器缺少候选取证必需文件：{sorted(missing)}。")
        return {"targets": targets, "global_state": None, "missing": sorted(missing), "enforced": mode == MODE_FORMAL}
    if all(targets[role]["files"]["global_state"]["present"] for role in targets):
        digests = {
            _container_file_sha256(
                container,
                PurePosixPath(global_state),
                operation=f"precondition:{role}:global-state-identity",
                deadline=deadline,
                heartbeat=heartbeat,
            )
            for role, container in layout.containers().items()
        }
        identical = len(digests) == 1
    if mode == MODE_FORMAL:
        if missing:
            raise ConfigurationError(
                f"采集容器缺少取证必需文件：{sorted(missing)}（凭据需先完成官方 OAuth 授权，见 B5a-07；断网容器的全局状态需从"
                "抓包容器同步一份）。"
            )
        if identical is not True:
            raise ConfigurationError("抓包容器与断网容器里的全局状态文件内容不同，须重新同步后再取证。")
    return {
        "targets": targets,
        "global_state": {"path": global_state, "identical_across_containers": identical},
        "missing": sorted(missing),
        "enforced": mode == MODE_FORMAL,
    }


# ---------------------------------------------------------------------------
# relay 步骤：运行期绑定、零请求改写与清单核对
# ---------------------------------------------------------------------------


def _relay_step_coordinates(argv: Sequence[str]) -> dict[str, Any]:
    """relay ``capture`` 步骤的关键坐标；B2-05 保证每一步都是这个动作体。

    ``probe_id`` 是步骤身份，取输出目录名（B2-05 保证输出根是证据根下以步骤 probe 命名的子目录）：主模型步骤就是中继的
    ``--probe-id``；非主模型（模型能力取证）步骤是「<--model>-<--probe-id>」，中继清单里记的仍是基础 probe 与该模型
    （``relay_probe_id``、``model``）。
    """

    scripts = [index for index, item in enumerate(argv) if item == RELAY_SCRIPT_NAME or str(item).endswith(f"/{RELAY_SCRIPT_NAME}")]
    if len(scripts) != 1 or scripts[0] + 1 >= len(argv) or argv[scripts[0] + 1] != "capture":
        raise ConfigurationError(f"步骤不是 {RELAY_SCRIPT_NAME} capture 动作体。")
    output_root = _argument_value(argv, "--output-root")
    relay_probe = _argument_value(argv, "--probe-id")
    model = _argument_value(argv, "--model")
    coordinates = {
        "output_root": output_root,
        "run_id": _argument_value(argv, "--run-id"),
        "probe_id": PurePosixPath(output_root).name if output_root else None,
        "relay_probe_id": relay_probe,
        "model": model,
        "expected_version": _argument_value(argv, "--expected-version"),
        "expected_sha256": _argument_value(argv, "--expected-sha256"),
        "upstream_ip": _argument_value(argv, "--upstream-ip"),
        "live": "--acknowledge-live-requests" in argv,
        "synthetic": "--acknowledge-synthetic-responses" in argv,
        "execute": "--execute" in argv,
    }
    if not coordinates["output_root"] or not coordinates["run_id"] or not relay_probe:
        raise ConfigurationError("relay 步骤缺少 --output-root／--run-id／--probe-id。")
    if coordinates["probe_id"] not in {relay_probe, f"{model}-{relay_probe}"}:
        raise ConfigurationError("relay 步骤的输出目录名既不是 --probe-id，也不是「<--model>-<--probe-id>」。")
    return coordinates


def _rehearsal_argv(argv: Sequence[str]) -> list[str]:
    """零请求演练：``--execute`` 换成 ``--dry-run``，去掉请求确认参数，其余参数逐字保留。"""

    if list(argv).count("--execute") != 1:
        raise ConfigurationError("零请求演练只能改写恰有一个 --execute 的 relay 步骤。")
    return ["--dry-run" if item == "--execute" else str(item) for item in argv if item not in RELAY_ACKNOWLEDGE_FLAGS]


def _step_run_nonce(attempt_nonce: str, job_id: str, run_index: int, step_index: int) -> str:
    """每次执行每一步的 nonce：由 attempt nonce 派生，同一 attempt 内唯一、可复算。"""

    material = f"claude-upgrade-step-nonce/v1|{attempt_nonce}|{job_id}|{run_index}|{step_index}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _container_file_sha256(
    container: str,
    path: PurePosixPath,
    *,
    operation: str = "host-runtime-receipt:container-visibility",
    deadline: Any | None,
    heartbeat: Any | None,
) -> str:
    """在指定容器内用 ``/usr/bin/sha256sum`` 取一个文件的摘要（输出必须是该路径的单行标准格式）。"""

    completed = _run_probe_command(
        ["docker", "exec", container, "/usr/bin/sha256sum", str(path)],
        operation=operation,
        deadline=deadline,
        heartbeat=heartbeat,
    )
    match = SHA256SUM_LINE_RE.fullmatch(completed.stdout)
    if match is None or match.group("path") != str(path):
        raise ConfigurationError("容器内 sha256sum 输出格式非法或路径不符。")
    return match.group("sha256")


def _issue_host_runtime_receipt(
    context: "ScenarioReceiptContext",
    *,
    job_id: str,
    probe_id: str,
    run_index: int,
    step_index: int,
    container: str,
    deadline: Any | None,
    heartbeat: Any | None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """每步执行前在宿主签发运行镜像凭据（``runtime_host_receipt.py``，15 分钟有效）并核对它在该步所在容器内逐字可见。

    ``container`` 是该步实际执行的容器（真实上游步骤是抓包容器，合成步骤是断网容器）：relay 在容器内用 hostname 与
    mountinfo 里的完整容器 ID 复核凭据绑定的容器，签错容器会让 relay 在任何请求之前拒绝。
    返回 ``(运行期变量取值, 凭据绑定)``；relay 执行时再核对凭据的 nonce、镜像、容器身份与执行源，二者构成同一条链。
    """

    layout = context.layout
    if layout is None:
        raise ConfigurationError("运行期变量绑定缺少坐标布局。")
    if container not in layout.containers().values():
        raise ConfigurationError(f"宿主运行凭据只能签给登记的两个采集容器，实际 {container}。")
    receipts_root = _private_directory(context.job_run_root(job_id, run_index) / "receipts", context.attempt_root)
    output = receipts_root / f"step-{step_index:02d}-{probe_id}.host-runtime-receipt.json"
    if output.exists() or output.is_symlink():
        raise ConfigurationError(f"宿主运行凭据已存在，拒绝覆盖：{output.name}")
    nonce = _step_run_nonce(context.run_nonce, job_id, run_index, step_index)
    _run_probe_command(
        [
            sys.executable,
            str(layout.host_tool_root / HOST_RUNTIME_RECEIPT_SCRIPT),
            "--container",
            container,
            "--runtime-image",
            layout.runtime_image,
            "--capture-tool-root",
            str(layout.host_tool_root),
            "--output",
            str(output),
            "--run-nonce",
            nonce,
        ],
        operation=f"job:{job_id}:step-{step_index}:host-runtime-receipt",
        timeout_seconds=HOST_RECEIPT_TIMEOUT_SECONDS,
        deadline=deadline,
        heartbeat=heartbeat,
    )
    receipt = claude_upgrade._read_json(output, "宿主运行凭据")
    bound = receipt.get("container") if isinstance(receipt.get("container"), Mapping) else {}
    if (
        receipt.get("schema_version") != HOST_RUNTIME_RECEIPT_SCHEMA
        or receipt.get("run_nonce") != nonce
        or receipt.get("runtime_image_reference") != layout.runtime_image
        or receipt.get("repo_digest_verified") is not True
        or bound.get("name") != container
        or not CONTAINER_ID_RE.fullmatch(str(bound.get("id", "")))
    ):
        raise ConfigurationError("宿主运行凭据的 schema、nonce、镜像或容器身份与本次取证不符。")
    digest = file_sha256(output)
    container_path = _container_path(output, layout)
    if _container_file_sha256(container, container_path, deadline=deadline, heartbeat=heartbeat) != digest:
        raise ConfigurationError(f"宿主运行凭据在容器 {container} 内不可见或摘要不符（容器坐标映射错位）。")
    values = {"host_runtime_receipt": str(container_path), "host_runtime_receipt_sha256": digest, "run_nonce": nonce}
    binding = {
        "path": output.relative_to(context.attempt_root).as_posix(),
        "sha256": digest,
        "container_path": str(container_path),
        "run_nonce": nonce,
        "container_name": container,
        "container_id": str(bound["id"]),
        "issued_at_utc": str(receipt.get("issued_at_utc", "")),
    }
    return values, binding


def _relay_manifest_check(
    coordinates: Mapping[str, Any],
    layout: CaptureLayout,
    *,
    receipt: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """正式执行后核对 relay 清单：完成、完整 M、终态秘密扫描、hosts 恢复与中继停止、客户端身份与本步凭据绑定。

    ``candidate`` 是候选步骤的期望（B4-01b，``claude_upgrade_candidate_capture.relay_manifest_expectation``）：另核清单是
    候选侧、网关账号与 API Key 一致、没有读官方 OAuth 凭据、上游是 connect 中继。
    """

    output_root = PurePosixPath(str(coordinates["output_root"]))
    manifest_path = _host_path(output_root / "relay-manifest.json", layout)
    row: dict[str, Any] = {"probe_id": coordinates["probe_id"], "output_root": str(output_root), "status": "failed"}
    if manifest_path.is_symlink() or not manifest_path.is_file():
        row["failure_code"] = "missing"
        return row
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        row["failure_code"] = "unreadable"
        return row
    row["manifest"] = {"path": str(output_root / "relay-manifest.json"), "sha256": file_sha256(manifest_path)}
    client = payload.get("client") if isinstance(payload.get("client"), Mapping) else {}
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), Mapping) else {}
    host_receipt = runtime.get("host_runtime_receipt") if isinstance(runtime.get("host_runtime_receipt"), Mapping) else {}
    checks = (
        (
            "schema-mismatch",
            payload.get("schema_version") == RELAY_RUN_SCHEMA
            and payload.get("probe_id") == coordinates["relay_probe_id"]
            and (coordinates["probe_id"] == coordinates["relay_probe_id"] or payload.get("model") == coordinates["model"]),
        ),
        ("incomplete", payload.get("status") == "complete"),
        ("m-binding-open", (payload.get("m_binding") or {}).get("complete") is True),
        ("secret-scan-failed", (payload.get("secret_scan") or {}).get("passed") is True),
        ("cleanup-unconfirmed", payload.get("cleanup") == {"relay_stopped": True, "hosts_restored": True}),
        (
            "client-identity-mismatch",
            str(client.get("version", "")).split(" ", 1)[0] == coordinates["expected_version"]
            and client.get("sha256") == coordinates["expected_sha256"],
        ),
        (
            "runtime-binding-mismatch",
            receipt is None
            or (host_receipt.get("sha256") == receipt.get("sha256") and host_receipt.get("run_nonce") == receipt.get("run_nonce")),
        ),
    )
    for failure_code, passed in checks:
        if not passed:
            row["failure_code"] = failure_code
            return row
    if candidate is not None:
        from tools.official_client_capture import claude_upgrade_candidate_capture

        failure = claude_upgrade_candidate_capture.candidate_manifest_failure(payload, candidate)
        if failure is not None:
            row["failure_code"] = failure
            return row
    row["status"] = "passed"
    return row


def _relay_plan_check(coordinates: Mapping[str, Any], log_path: Path, *, candidate: bool = False) -> dict[str, Any]:
    """零请求演练后核对 relay 计划回显：计划 schema、身份、版本、零生产改动、真实请求意图与冻结参数一致（候选步骤另核
    计划是候选侧的 connect 中继）。"""

    row: dict[str, Any] = {"probe_id": coordinates["probe_id"], "status": "failed"}
    try:
        raw = log_path.read_bytes()
        plan = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        row["failure_code"] = "plan-unreadable"
        return row
    row["plan_sha256"] = hashlib.sha256(raw).hexdigest()
    row["live_requests_planned"] = plan.get("live_requests") is True
    # 演练 argv 已去掉请求确认参数；真实上游意图以冻结的 --upstream-ip 判断（B2-05：真实 probe 必带、合成 probe 禁带）。
    checks = (
        plan.get("schema_version") == RELAY_PLAN_SCHEMA,
        plan.get("run_id") == coordinates["run_id"] and plan.get("probe_id") == coordinates["relay_probe_id"],
        # 非主模型（模型能力取证）步骤：计划回显的模型必须是步骤身份里的模型（与正式执行的 relay 清单核对同一口径）。
        coordinates["probe_id"] == coordinates["relay_probe_id"] or plan.get("model") == coordinates["model"],
        plan.get("version") == coordinates["expected_version"],
        plan.get("production_changes") is False,
        (plan.get("live_requests") is True) == (coordinates["upstream_ip"] is not None),
        plan.get("upstream_ip") == coordinates["upstream_ip"],
    )
    if not all(checks):
        row["failure_code"] = "plan-mismatch"
        return row
    if candidate:
        from tools.official_client_capture import claude_upgrade_candidate_capture

        failure = claude_upgrade_candidate_capture.candidate_plan_failure(plan)
        if failure is not None:
            row["failure_code"] = failure
            return row
    row["status"] = "passed"
    return row


# ---------------------------------------------------------------------------
# Job 执行（wire 根 _run_job_impl）与有限补跑（服务面 _run_job_with_retry）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScenarioReceiptContext:
    """一次 attempt 的 Job 执行上下文（与 Codex 同名同位置参数；Claude 另带模式、坐标布局与两个执行回调）。

    wire 根 ``_run_job_impl`` 只经本对象的两个方法签发运行期变量、执行步骤，回调实现（宿主凭据签发、监督器执行）
    属于 control 层，不进 Claude 的 wire 闭包。``detached()`` 是没有 attempt 的执行上下文（例如 Job 演练的合成作业）：
    能执行步骤，不能签发运行期变量，也不做环境闭环。
    """

    campaign_id: str = ""
    attempt_id: str = ""
    run_nonce: str = ""
    evidence_root: Path | None = None
    campaign_dir: Path | None = None
    attempt_root: Path | None = None
    mode: str = MODE_FORMAL
    layout: CaptureLayout | None = None
    manifest: Mapping[str, Any] | None = None

    @classmethod
    def detached(cls) -> "ScenarioReceiptContext":
        return cls()

    @property
    def environmental(self) -> bool:
        """是否做环境闭环（探针、证据根核对、Job 级收口）：只有 attempt 内的上下文才做。"""

        return self.attempt_root is not None and self.layout is not None and self.manifest is not None

    def job_root(self, job_id: str) -> Path:
        """Job 闭环目录 ``<attempt>/evidence/jobs/<job>``：其下 ``evidence``、``logs`` 是 Job 级收口的两个内部根。"""

        if self.evidence_root is None:
            raise ConfigurationError("没有 attempt 的执行上下文没有 Job 闭环目录。")
        return self.evidence_root / JOBS_DIRNAME / job_id

    def job_run_root(self, job_id: str, run_index: int) -> Path:
        return self.job_root(job_id) / "evidence" / f"run-{run_index}"

    def runtime_values(
        self,
        *,
        job_id: str,
        probe_id: str,
        run_index: int,
        step_index: int,
        argv: Sequence[str],
        deadline: Any | None,
        heartbeat: Any | None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        """运行期变量的取值与凭据绑定：每步执行前在宿主签发运行镜像凭据（见 ``_issue_host_runtime_receipt``）。

        ``argv`` 是该步未绑定运行期变量的冻结 argv：先按 ``_step_route`` 复核执行容器（真实上游步骤进抓包容器、合成步骤进
        断网容器），再把凭据签给该容器。
        """

        if not self.environmental or self.layout is None:
            raise ConfigurationError("运行期变量（宿主运行凭据与 nonce）只能在 attempt 上下文里签发。")
        _role, container = _step_route(argv, self.layout)
        return _issue_host_runtime_receipt(
            self,
            job_id=job_id,
            probe_id=probe_id,
            run_index=run_index,
            step_index=step_index,
            container=container,
            deadline=deadline,
            heartbeat=heartbeat,
        )

    def execute_step(
        self,
        argv: Sequence[str],
        *,
        operation: str,
        timeout_seconds: float,
        environment: Mapping[str, str],
        log_path: Path,
        job_id: str,
        deadline: Any | None,
        heartbeat: Any | None,
    ) -> int:
        """执行一步并把 stdout／stderr 写进 0600 日志（活跃租约下经独立监督器）。

        attempt 上下文里先按 ``_step_route`` 复核执行容器；合成步骤的操作名从 ``job:…`` 改成 ``synthetic-job:…``：出口守护下
        监督器只给 ``job:`` 开头的操作建立 capture-cli 停止绑定，并拒绝 ``docker exec`` 到别的容器，断网容器不在出口准入里
        （模块文档第五节）。没有 attempt 的执行上下文（Job 演练的合成作业）不做路由，原样执行。
        """

        if self.layout is not None:
            role, _container = _step_route(argv, self.layout)
            if not operation.startswith(LIVE_STEP_OPERATION_PREFIX):
                raise ConfigurationError(f"取证步骤的操作名必须以 {LIVE_STEP_OPERATION_PREFIX} 开头：{operation}")
            if role == ROLE_SYNTHETIC:
                operation = SYNTHETIC_STEP_OPERATION_PREFIX + operation[len(LIVE_STEP_OPERATION_PREFIX):]
            # 候选步骤的宿主脚本在宿主执行：容器坐标映射到同一工具执行副本的宿主坐标（收据里的 argv 仍记冻结的容器坐标）。
            argv = _host_step_argv(argv, self.layout)
        with log_path.open("w", encoding="utf-8") as log:
            os.chmod(log_path, 0o600)
            return _run_step_command(
                argv,
                operation=operation,
                timeout_seconds=timeout_seconds,
                environment=environment,
                log_stream=log,
                job_id=job_id,
                deadline=deadline,
                heartbeat=heartbeat,
            )


def _step_log_path(log_root: Path, job_id: str, step_index: int, attempt_index: int) -> Path:
    return log_root / (f"{job_id}-{step_index}.log" if attempt_index == 1 else f"{job_id}-retry{attempt_index}-{step_index}.log")


def _run_job_impl(
    job: claude_upgrade.Job,
    log_root: Path,
    attempt_index: int = 1,
    scenario_context: Any | None = None,
    *,
    identity: Mapping[str, Any] | None = None,
    tool_identity: Mapping[str, Any] | None = None,
    deadline: Any | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """wire 根：按已展开的 Job 执行一次（顺序执行各步，非零即停），返回各步的执行事实。

    只接收展开好的 Job 与执行上下文，不读总计划、不调用 control 函数：每步先经上下文签发运行期变量、用 B2-05 的
    ``bind_step_runtime`` 绑定，零请求演练把 relay 改写成 ``--dry-run``，再经上下文执行。证据根与 relay 清单核对、
    增量元数据与终态判定在 wire 根之外（``_run_job_with_retry``）完成。``identity``／``tool_identity`` 只为与 Codex
    同签名保留，这里不使用。

    ``scenario_context`` 是 ``ScenarioReceiptContext``，``deadline`` 是 ``WallClockDeadline``；两处注解有意写成 ``Any``：
    身份引擎按符号收集闭包时会经注解把类整体拉进来，执行上下文的回调（宿主凭据签发、监督器执行）与 deadline 都属于
    control 层，不应进入 wire 闭包（闭包模块集合由 ``test_claude_upgrade_capture`` 锁定）。
    """

    del identity, tool_identity
    if scenario_context is None:
        raise ConfigurationError("_run_job_impl 必须由 _run_job_with_retry 提供执行上下文。")
    started = time.time()
    if deadline is not None:
        deadline.check(f"job:{job.job_id}:start")
    rehearsal = scenario_context.mode == MODE_REHEARSAL
    steps: list[dict[str, Any]] = []
    observations: list[dict[str, str]] = []
    for index, raw_step in enumerate(job.steps, 1):
        if deadline is not None:
            deadline.check(f"job:{job.job_id}:step-{index}:prepare")
        if heartbeat is not None:
            heartbeat(f"job:{job.job_id}:step-{index}:start")
        probe_id = str(raw_step.get("probe_id") or f"step-{index}")
        declared = dict(raw_step.get("runtime_variables") or {})
        receipt: dict[str, Any] | None = None
        if declared:
            try:
                values, receipt = scenario_context.runtime_values(
                    job_id=job.job_id,
                    probe_id=probe_id,
                    run_index=attempt_index,
                    step_index=index,
                    argv=list(raw_step["argv"]),
                    deadline=deadline,
                    heartbeat=heartbeat,
                )
            except ConfigurationError as error:
                steps.append(
                    {"step": index, "probe_id": probe_id, "argv": [], "return_code": None, "log": None,
                     "mode": scenario_context.mode, "failure": "host-runtime-receipt", "error": str(error)[:500]}
                )
                observations.append({"check_id": f"job.{job.job_id}.step-{index}.host-runtime-receipt", "failure_code": "issue-failed"})
                break
            step = claude_upgrade_plan.bind_step_runtime(raw_step, {name: values[name] for name in declared})
        else:
            step = dict(raw_step)
        argv = [str(item) for item in step["argv"]]
        if rehearsal:
            argv = _rehearsal_argv(argv)
        environment = os.environ.copy()
        environment.update({str(key): str(value) for key, value in dict(step.get("environment") or {}).items()})
        log_path = _step_log_path(log_root, job.job_id, index, attempt_index)
        return_code = scenario_context.execute_step(
            argv,
            operation=f"job:{job.job_id}:step-{index}:attempt-{attempt_index}",
            timeout_seconds=float(step.get("timeout", 1800)),
            environment=environment,
            log_path=log_path,
            job_id=job.job_id,
            deadline=deadline,
            heartbeat=heartbeat,
        )
        row: dict[str, Any] = {
            "step": index,
            "probe_id": probe_id,
            "argv": [Path(argv[0]).name, *argv[1:]],
            "return_code": return_code,
            "log": str(log_path),
            "mode": scenario_context.mode,
        }
        if receipt is not None:
            row["host_runtime_receipt"] = receipt
        steps.append(row)
        if heartbeat is not None:
            heartbeat(f"job:{job.job_id}:step-{index}:complete")
        if return_code != 0:
            observations.append({"check_id": f"job.{job.job_id}.step-{index}", "failure_code": "nonzero-exit"})
            break
    complete = len(steps) == len(job.steps) and all(row["return_code"] == 0 for row in steps)
    return {
        "id": job.job_id,
        "attempt_index": attempt_index,
        "mode": scenario_context.mode,
        "status": "complete" if complete else "failed",
        "duration_seconds": round(time.time() - started, 3),
        "steps": steps,
        "failure_observations": observations,
    }


def _evidence_root_has_bytes(path: Path) -> bool:
    for current, _directories, files in os.walk(path):
        for name in files:
            candidate = Path(current) / name
            try:
                if not candidate.is_symlink() and candidate.is_file() and candidate.stat().st_size > 0:
                    return True
            except OSError:
                continue
    return False


def _detached_host_path(root: str) -> Path:
    """没有坐标布局时（Job 演练的合成作业）：落在登记 runs 别名下的容器坐标经别名表映射，其余按宿主路径。"""

    value = PurePosixPath(str(root))
    for alias in claude_upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS:
        try:
            value.relative_to(PurePosixPath(alias))
        except ValueError:
            continue
        return claude_upgrade._failed_job_evidence_host_route(str(value))[0]
    return Path(str(root))


def _finalize_job_result(
    job: claude_upgrade.Job,
    raw: Mapping[str, Any],
    context: ScenarioReceiptContext,
    incremental: Mapping[str, Any],
) -> dict[str, Any]:
    """把一次执行的事实整理成 Codex 同形的 Job 收据：逐步核对 relay 清单（演练为计划回显）、核对证据根、合并增量元数据。

    正式执行：证据根必须存在且非空，每个成功的 relay 步骤都要有完成、闭合且与本步凭据绑定一致的 relay 清单；
    零请求演练：每步计划回显必须与冻结参数一致、零生产改动，证据根必须保持不存在。

    候选 Job（B4-01b）：relay 清单另核候选身份（``claude_upgrade_candidate_capture.candidate_manifest_failure``）；正式执行
    的每个执行过的步骤（成败都算）另核宿主脚本的恢复报告，结果放在 ``restoration_checks``，任一失败即环境污染（由
    ``_run_job_with_retry`` 停止补跑与后续 Job）。演练不改任何东西、不写恢复报告，``restoration_checks`` 为空。
    """

    layout = context.layout
    candidate_job = job.phase == "candidate"
    candidate_expectation: dict[str, Any] | None = None
    if candidate_job and layout is not None and context.manifest is not None:
        from tools.official_client_capture import claude_upgrade_candidate_capture

        candidate_expectation = claude_upgrade_candidate_capture.relay_manifest_expectation(context.manifest)
    observations: list[dict[str, str]] = [dict(item) for item in raw.get("failure_observations") or []]
    relay_checks: list[dict[str, Any]] = []
    restoration_checks: list[dict[str, Any]] = []
    for row in raw.get("steps") or []:
        if candidate_expectation is not None and context.mode == MODE_FORMAL:
            restoration = claude_upgrade_candidate_capture.restoration_check(row, layout, context.manifest)
            if restoration is not None:
                restoration_checks.append(restoration)
                if restoration["status"] == "failed":
                    observations.append(
                        {"check_id": f"job.{job.job_id}.restoration.{restoration['probe_id']}", "failure_code": str(restoration["failure_code"])}
                    )
        if row.get("return_code") != 0 or layout is None:
            continue
        coordinates = _relay_step_coordinates(row["argv"])
        check = (
            _relay_plan_check(coordinates, Path(str(row["log"])), candidate=candidate_job)
            if context.mode == MODE_REHEARSAL
            else _relay_manifest_check(coordinates, layout, receipt=row.get("host_runtime_receipt"), candidate=candidate_expectation)
        )
        relay_checks.append(check)
        if check["status"] != "passed":
            kind = "rehearsal" if context.mode == MODE_REHEARSAL else "relay"
            observations.append({"check_id": f"job.{job.job_id}.{kind}.{row['probe_id']}", "failure_code": str(check["failure_code"])})
    existing: list[str] = []
    missing: list[str] = []
    empty: list[str] = []
    unexpected: list[str] = []
    for root in job.evidence_roots:
        host_root = _host_path(root, layout) if layout is not None else _detached_host_path(root)
        present = host_root.exists() or host_root.is_symlink()
        if context.mode == MODE_REHEARSAL:
            if present:
                unexpected.append(str(root))
            continue
        if not present or host_root.is_symlink() or not host_root.is_dir():
            missing.append(str(root))
            continue
        existing.append(str(root))
        if not _evidence_root_has_bytes(host_root):
            empty.append(str(root))
    if missing:
        observations.append({"check_id": f"job.{job.job_id}.evidence", "failure_code": "missing"})
    if empty:
        observations.append({"check_id": f"job.{job.job_id}.evidence", "failure_code": "empty"})
    if unexpected:
        observations.append({"check_id": f"job.{job.job_id}.evidence", "failure_code": "unexpected-in-rehearsal"})
    checks_ok = layout is None or all(item["status"] == "passed" for item in relay_checks)
    restored_ok = all(item["status"] != "failed" for item in restoration_checks)
    complete = raw.get("status") == "complete" and checks_ok and restored_ok and not missing and not empty and not unexpected
    result = {
        "id": job.job_id,
        "phase": job.phase,
        "required": job.required,
        "execution_sha256": _job_execution_sha256(job),
        "status": "complete" if complete else "failed",
        "attempt_index": raw.get("attempt_index"),
        "description": job.description,
        "duration_seconds": raw.get("duration_seconds"),
        "mode": context.mode,
        "steps": list(raw.get("steps") or []),
        "relay_checks": relay_checks,
        "evidence_roots": existing,
        "missing_evidence_patterns": missing,
        "empty_evidence_patterns": empty,
        "unexpected_evidence_roots": unexpected,
        "covers": list(job.covers),
        "scenario_ids": list(job.scenario_ids),
        "scenario_receipts": [],
        "scenario_receipt_failures": [],
        "track": getattr(job, "track", "main"),
        "model_id": getattr(job, "model_id", ""),
        "expected_use_responses_lite": False,
        "required_model_receipt": False,
        "model_condition_receipt": None,
        "model_condition_receipt_failure": None,
        "disposition": "executed",
        "failure_observations": observations if not complete else [],
        "tool_dependency_files": dict(incremental["tool_dependency_files"]),
        "input_sha256": incremental["input_sha256"],
        "environment_sha256": incremental["environment_sha256"],
        "dependency_sha256": incremental["dependency_sha256"],
        "incremental_result_key": incremental["result_key"],
    }
    if candidate_job:
        result["restoration_checks"] = restoration_checks
    return result


def _archive_failed_job_evidence(result: dict[str, Any], attempt_index: int) -> list[str]:
    """把失败证据整体归档为同级 ``<名>.failed-attempt<N>``，同步重定位收据里的路径；返回归档后的容器坐标。

    与 Codex 同一语义与同一组登记常量（``claude_upgrade.FAILED_JOB_EVIDENCE_*``，调用时读取，Job 演练可改写）：固定证据根
    必须清出，否则下一次补跑会在任何请求前被「拒绝覆盖」卡死。
    """

    raw_roots = result.get("evidence_roots") or []
    if not isinstance(raw_roots, list):
        raise ConfigurationError("失败任务 evidence_roots 必须是数组。")
    if not raw_roots:
        return []
    host_root = Path(claude_upgrade.FAILED_JOB_EVIDENCE_HOST_RUN_ROOT)
    if not host_root.is_absolute() or host_root.is_symlink() or not host_root.is_dir():
        raise ConfigurationError("失败任务证据宿主 runs 根不存在或不可信。")
    planned: list[tuple[Path, Path, PurePosixPath, PurePosixPath]] = []
    seen: set[Path] = set()
    for value in raw_roots:
        source, relative = claude_upgrade._failed_job_evidence_host_route(value)
        claude_upgrade._reject_symlink_components(source, host_root, "失败任务证据根")
        if not source.exists():
            continue
        if source.is_symlink() or not source.is_dir():
            raise ConfigurationError("失败任务证据根不是可信目录。")
        if source in seen or any(source.is_relative_to(item) or item.is_relative_to(source) for item in seen):
            raise ConfigurationError("失败任务证据根重复或嵌套映射到同一宿主目录。")
        seen.add(source)
        archive_name = f"{source.name}.failed-attempt{attempt_index}"
        archived = source.with_name(archive_name)
        suffix = 1
        while archived.exists() or archived.is_symlink():
            suffix += 1
            archive_name = f"{source.name}.failed-attempt{attempt_index}-{suffix}"
            archived = source.with_name(archive_name)
        planned.append((source, archived, relative, relative.with_name(archive_name)))
    moved: list[tuple[Path, Path]] = []
    try:
        for source, archived, _relative, _archived_relative in planned:
            source.rename(archived)
            moved.append((source, archived))
    except OSError as error:
        rollback_complete = True
        for source, archived in reversed(moved):
            try:
                archived.rename(source)
            except OSError:
                rollback_complete = False
        number = error.errno if isinstance(error.errno, int) else "none"
        name = errno.errorcode.get(error.errno, "UNKNOWN") if isinstance(error.errno, int) else "UNKNOWN"
        raise ConfigurationError(
            f"失败任务证据归档失败：error_type={type(error).__name__} errno={number}({name}) "
            f"rollback_complete={str(rollback_complete).lower()}"
        ) from error
    replacements: dict[str, str] = {}
    for _source, _archived, relative, archived_relative in planned:
        for alias in claude_upgrade.FAILED_JOB_EVIDENCE_CONTAINER_RUN_ROOTS:
            replacements[str(PurePosixPath(alias).joinpath(*relative.parts))] = str(PurePosixPath(alias).joinpath(*archived_relative.parts))
    archived_values = [str(_rebase_value(str(value), replacements)) for value in raw_roots]
    rebased = _rebase_value(result, replacements)
    result.clear()
    result.update(rebased)
    return archived_values


def _rebase_value(value: Any, replacements: Mapping[str, str]) -> Any:
    """递归改写证据根及其内部文件引用，不改动其他任务事实。"""

    if isinstance(value, str):
        for before, after in sorted(replacements.items(), key=lambda item: len(item[0]), reverse=True):
            if value == before:
                return after
            if value.startswith(before + "/"):
                return after + value[len(before):]
        return value
    if isinstance(value, list):
        return [_rebase_value(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: _rebase_value(item, replacements) for key, item in value.items()}
    return value


def _failure_codes() -> dict[str, Any]:
    return codex_upgrade_root_cause.load_codes(_client_adapter().root_cause_codes_path)


def _failure_dimensions(codes: Mapping[str, Any], phase: str) -> dict[str, str]:
    return upgrade_client_adapter.root_cause_dimensions(_client_adapter(), codes, "campaign-run.action-failed", {"phase": phase})


def _normalize_failure_observations(observations: Sequence[Mapping[str, Any]], *, phase: str) -> list[dict[str, str]]:
    """校验并按稳定二元组排序去重（Claude 根因码表，维度含 client），不保留重试次数或诊断正文。"""

    if not observations:
        return []
    codes = _failure_codes()
    try:
        normalized, _causes = codex_upgrade_root_cause.describe_failure_observations(
            observations,
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            stable_dimensions=_failure_dimensions(codes, phase),
            codes=codes,
        )
    except codex_upgrade_root_cause.RootCauseError as error:
        raise ConfigurationError(f"attempt 失败观测非法：{error}") from error
    return [{"check_id": item["check_id"], "failure_code": item["failure_code"]} for item in normalized]


def _job_failure_observations(result: Mapping[str, Any]) -> list[dict[str, str]]:
    """Job 收据里的结构化失败观测；没有任何具体观测的失败记为 ``job.<id>``／``failed``。"""

    supplied = result.get("failure_observations") or []
    if not isinstance(supplied, list) or not all(isinstance(item, Mapping) for item in supplied):
        raise ConfigurationError("Job failure_observations 必须是对象数组。")
    raw = [dict(item) for item in supplied]
    if result.get("status") == "failed" and not raw and result.get("id"):
        raw.append({"check_id": f"job.{result['id']}", "failure_code": "failed"})
    return _normalize_failure_observations(raw, phase=str(result.get("phase", "official")))


def _probe_job_environment(
    context: ScenarioReceiptContext,
    job_id: str,
    run_index: int,
    phase: str,
    *,
    deadline: Any | None,
    heartbeat: Any | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Job 一次执行的 before／after 探针：写在 ``<job>/evidence/run-<n>/environment/<phase>``，返回 (探针清单, 绑定)。"""

    if context.manifest is None or context.attempt_root is None:
        raise ConfigurationError("Job 环境探针缺少 attempt 上下文。")
    output = context.job_run_root(job_id, run_index) / "environment" / phase
    _private_directory(output.parent, context.attempt_root)
    probe = claude_upgrade._probe_capture_environment(dict(context.manifest), output, phase, deadline=deadline, heartbeat=heartbeat)
    return probe, _probe_binding(context.attempt_root, output / PROBE_MANIFEST_NAME)


def _residual_process_result(job: claude_upgrade.Job, context: ScenarioReceiptContext, attempt_index: int) -> dict[str, Any]:
    """执行前已有残留取证进程：不执行任何步骤，Job 记失败（零请求）。"""

    return {
        "id": job.job_id,
        "attempt_index": attempt_index,
        "mode": context.mode,
        "status": "failed",
        "duration_seconds": 0.0,
        "steps": [],
        "failure_observations": [{"check_id": f"job.{job.job_id}.environment.processes", "failure_code": "residual"}],
    }


def _run_job_with_retry(
    job: claude_upgrade.Job,
    log_root: Path,
    scenario_context: ScenarioReceiptContext | None = None,
    *,
    identity: Mapping[str, Any] | None = None,
    tool_identity: Mapping[str, Any] | None = None,
    deadline: incremental_recovery.WallClockDeadline | None = None,
    heartbeat: Any | None = None,
) -> dict[str, Any]:
    """服务面成员：在同一 attempt 内对失败 Job 做有限补跑，返回最后一次的收据（与 Codex 同一补跑与归档语义）。

    有 attempt 上下文时每次执行前后各采一次环境探针：执行前已有残留取证进程、或执行后任一快照漂移，都判环境污染——
    不补跑、归档证据、收据带 ``environment_contaminated``，由 attempt 主流程停止后续 Job。补跑上限与退避
    （``claude_upgrade.JOB_RETRY_LIMIT``／``JOB_RETRY_DELAY_SECONDS``）在调用时读取，Job 演练可改写。
    """

    context = scenario_context if scenario_context is not None else ScenarioReceiptContext.detached()
    incremental = _job_incremental_metadata(job, identity=identity, tool_identity=tool_identity)
    attempt_index = 1
    observed: list[dict[str, str]] = []
    environment_runs: list[dict[str, Any]] = []
    archived_roots: list[str] = []

    def finish(result: dict[str, Any], *, archive: bool, contaminated: bool = False) -> dict[str, Any]:
        if archive:
            archived_roots.extend(_archive_failed_job_evidence(result, attempt_index))
        result["failure_observations"] = list(observed)
        result["environment_runs"] = list(environment_runs)
        result["archived_evidence_roots"] = sorted(set(archived_roots) - set(result.get("evidence_roots") or []))
        result["environment_contaminated"] = contaminated
        return result

    while True:
        if deadline is not None:
            deadline.check(f"job:{job.job_id}:attempt-{attempt_index}")
        before: dict[str, Any] | None = None
        run_record: dict[str, Any] = {"run": attempt_index}
        if context.environmental:
            before, run_record["before_probe"] = _probe_job_environment(
                context, job.job_id, attempt_index, "before", deadline=deadline, heartbeat=heartbeat
            )
            if before.get("capture_processes"):
                run_record["drifted_kinds"] = ["processes"]
                environment_runs.append(run_record)
                result = _finalize_job_result(job, _residual_process_result(job, context, attempt_index), context, incremental)
                observed.extend(_job_failure_observations(result))
                return finish(result, archive=False, contaminated=True)
        raw: dict[str, Any] | None = None
        try:
            raw = claude_upgrade._run_job_impl(
                job,
                log_root,
                attempt_index,
                context,
                identity=identity,
                tool_identity=tool_identity,
                deadline=deadline,
                heartbeat=heartbeat,
            )
        finally:
            # 执行中途异常（超时、监督器停线、中断）也先尽力采 after 探针，留下污染判定所需的事实。
            if before is not None:
                if deadline is None or not deadline.expired:
                    try:
                        after, run_record["after_probe"] = _probe_job_environment(
                            context, job.job_id, attempt_index, "after", deadline=deadline, heartbeat=heartbeat
                        )
                        run_record["drifted_kinds"] = _probe_drift(before, after)
                    except (ConfigurationError, OSError) as error:
                        run_record["after_probe_error"] = type(error).__name__
                        run_record["drifted_kinds"] = list(PROBE_KINDS)
                environment_runs.append(run_record)
        result = _finalize_job_result(job, raw, context, incremental)
        if result.get("status") != "complete":
            observed = _normalize_failure_observations([*observed, *_job_failure_observations(result)], phase=job.phase)
        drifted = run_record.get("drifted_kinds") or []
        if drifted:
            observed = _normalize_failure_observations(
                [*observed, *({"check_id": f"job.{job.job_id}.environment.{kind}", "failure_code": "drifted"} for kind in drifted)],
                phase=job.phase,
            )
            result["status"] = "failed"
            return finish(result, archive=True, contaminated=True)
        if any(item.get("status") == "failed" for item in result.get("restoration_checks") or []):
            # 候选网关隔离没能证明精确恢复（恢复报告缺失、恢复失败或任一项不等）：生产形态的网关状态未知，按环境污染
            # 处理——不补跑、归档证据、停止后续 Job（观测已在 _finalize_job_result 里记下）。
            result["status"] = "failed"
            return finish(result, archive=True, contaminated=True)
        if result.get("status") == "complete":
            return finish(result, archive=False)
        if not job.required or attempt_index > int(claude_upgrade.JOB_RETRY_LIMIT):
            return finish(result, archive=True)
        archived_roots.extend(_archive_failed_job_evidence(result, attempt_index))
        attempt_index += 1
        delay = float(claude_upgrade.JOB_RETRY_DELAY_SECONDS)
        if deadline is not None:
            deadline.sleep(delay, operation=f"job:{job.job_id}:retry-backoff")
        else:
            time.sleep(delay)


# ---------------------------------------------------------------------------
# Job 闭环收口：Job 级证据权限收口一次 + 闭环收据
# ---------------------------------------------------------------------------


def _external_host_roots(values: Iterable[str], layout: CaptureLayout) -> list[Path]:
    roots: list[Path] = []
    for value in sorted(set(values)):
        host = _host_path(value, layout)
        if host.exists() and not host.is_symlink() and host.is_dir():
            roots.append(host)
    return roots


def _close_job(
    context: ScenarioReceiptContext,
    job: claude_upgrade.Job,
    result: Mapping[str, Any],
) -> dict[str, Any]:
    """Job 终态后：Job 级权限收口一次（内部根 ``<job>/evidence``、``<job>/logs`` 加本 Job 的外部证据根与归档根），再只写
    一次 ``job-closure.json``；返回闭环收据绑定（路径相对 attempt 根）。"""

    layout = context.layout
    if layout is None:
        raise ConfigurationError("Job 闭环收口缺少坐标布局。")
    job_root = context.job_root(job.job_id)
    internal = [_private_directory(job_root / "evidence", context.attempt_root), _private_directory(job_root / "logs", context.attempt_root)]
    container_roots = sorted(set(result.get("evidence_roots") or []) | set(result.get("archived_evidence_roots") or []))
    external = _external_host_roots(container_roots, layout)
    try:
        receipt_path, receipt = codex_upgrade_evidence_permissions.close_evidence_permissions(
            job_root,
            [*internal, *external],
            managed_data_root=layout.host_data_root,
            logical_runs_roots=(layout.host_runs_root,),
        )
        permission_binding = codex_upgrade_evidence_permissions.receipt_binding(job_root, receipt_path)
    except (OSError, codex_upgrade_evidence_permissions.EvidencePermissionError) as error:
        raise ConfigurationError(f"Job {job.job_id} 证据权限收口失败：{error}") from error
    closure = {
        "schema_version": JOB_CLOSURE_SCHEMA,
        "client": CLIENT,
        "mode": context.mode,
        "campaign_id": context.campaign_id,
        "attempt_id": context.attempt_id,
        "job_id": job.job_id,
        "execution_sha256": _job_execution_sha256(job),
        "status": result.get("status"),
        "attempt_index": result.get("attempt_index"),
        "environment_contaminated": bool(result.get("environment_contaminated")),
        "environment_runs": list(result.get("environment_runs") or []),
        "evidence_roots": list(result.get("evidence_roots") or []),
        "archived_evidence_roots": list(result.get("archived_evidence_roots") or []),
        "host_evidence_roots": [str(path) for path in external],
        "evidence_permission_closeout": {
            **permission_binding,
            "path": (job_root / permission_binding["path"]).relative_to(context.attempt_root).as_posix(),
            "boundary_sha256": receipt["boundary_sha256"],
            "entry_count": receipt["entry_count"],
        },
        "recorded_at_utc": claude_upgrade._utc_now(),
    }
    closure_path = job_root / JOB_CLOSURE_FILENAME
    claude_upgrade._secure_write_json_once(closure_path, closure)
    return {
        "path": closure_path.relative_to(context.attempt_root).as_posix(),
        "sha256": file_sha256(closure_path),
        "bytes": closure_path.stat().st_size,
    }


# ---------------------------------------------------------------------------
# attempt：预约、收据与读侧（服务面成员）
# ---------------------------------------------------------------------------


def _attempt_relative(mode: str, phase: str = "official", candidate_id: str | None = None) -> Path:
    """attempt 的阶段相对目录：正式 run 在 ``official``／``candidates/<cid>``，演练在 ``rehearsal/`` 下同名（正式读侧看不到）。"""

    relative = claude_upgrade._capture_attempt_relative(phase, candidate_id)
    return relative if mode == MODE_FORMAL else Path(REHEARSAL_DIRNAME) / relative


def _attempt_failure_facts(
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    """服务面成员：汇总 attempt 与各 Job 的结构化观测，生成一一对应的根因（Claude 根因码表，维度含 client）。"""

    phase = str(payload.get("phase", ""))
    raw = payload.get("failure_observations", [])
    if not isinstance(raw, list) or not all(isinstance(item, Mapping) for item in raw):
        raise ConfigurationError("attempt failure_observations 必须是对象数组。")
    observations: list[Mapping[str, Any]] = list(raw)
    for result in payload.get("results", []) or []:
        if isinstance(result, Mapping):
            observations.extend(_job_failure_observations(result))
    codes = _failure_codes()
    try:
        return codex_upgrade_root_cause.describe_failure_observations(
            observations,
            component="supervisor",
            stable_error_code="campaign-run.action-failed",
            stable_dimensions=_failure_dimensions(codes, phase),
            codes=codes,
        )
    except codex_upgrade_root_cause.RootCauseError as error:
        raise ConfigurationError(f"attempt 失败根因编码失败：{error}") from error


def _validate_attempt_failure_facts(payload: Mapping[str, Any]) -> None:
    expected_observations, expected_causes = _attempt_failure_facts(
        {**payload, "failure_observations": payload.get("attempt_failure_observations", [])}
    )

    def identity(causes: Any) -> Any:
        return [
            {key: value for key, value in item.items() if key != "codes_sha256"} if isinstance(item, Mapping) else item
            for item in (causes or [])
        ]

    if payload.get("failure_observations") != expected_observations or identity(payload.get("root_causes")) != identity(expected_causes):
        raise ConfigurationError("attempt 失败观测或根因数组漂移。")


def _reservation_required_fields() -> frozenset[str]:
    return frozenset({
        "schema_version", "client", "mode", "campaign_id", "campaign_mode", "campaign_purpose", "campaign_manifest_sha256",
        "phase", "candidate_id", "candidate_purpose", "attempt_id", "run_nonce", "started_at_utc", "identity_sha256",
        "planned_jobs", "reservation_digest",
    })


def _load_reservation(
    campaign_dir: Path,
    attempt_root: Path,
    *,
    phase: str,
    candidate_id: str | None,
    manifest: Mapping[str, Any],
    mode: str,
) -> dict[str, Any]:
    path = attempt_root / "reservation.json"
    claude_upgrade._reject_symlink_components(path, campaign_dir, "抓包预约收据")
    if not path.is_file() or path.is_symlink():
        raise ConfigurationError(f"抓包 attempt 缺少原子预约收据：{attempt_root.name}")
    payload = claude_upgrade._read_json(path, "抓包预约收据")
    required = _reservation_required_fields()
    optional = {"campaign_lease", "tool_evolution"}
    unsigned = dict(payload)
    digest = unsigned.pop("reservation_digest", None)
    if (
        payload.get("schema_version") != CAPTURE_RESERVATION_SCHEMA
        or not required.issubset(payload)
        or set(payload) - required - optional
        or payload.get("client") != CLIENT
        or payload.get("mode") != mode
        or payload.get("campaign_id") != manifest["campaign_id"]
        or payload.get("campaign_mode") != manifest["campaign_mode"]
        or payload.get("campaign_purpose") != manifest["campaign_purpose"]
        or payload.get("campaign_manifest_sha256") != file_sha256(campaign_dir / "campaign.json")
        or payload.get("phase") != phase
        or payload.get("candidate_id") != (candidate_id if phase == "candidate" else None)
        or payload.get("candidate_purpose") != (manifest["campaign_purpose"] if phase == "candidate" else None)
        or payload.get("attempt_id") != attempt_root.name
        or not SHA256_RE.fullmatch(str(payload.get("run_nonce", "")))
        or not claude_upgrade._is_rfc3339_timestamp(payload.get("started_at_utc"))
        or not SHA256_RE.fullmatch(str(payload.get("identity_sha256", "")))
        or not SHA256_RE.fullmatch(str(digest))
        or claude_upgrade._fingerprint(unsigned) != digest
    ):
        raise ConfigurationError("抓包预约身份或摘要不一致。")
    lease_binding = payload.get("campaign_lease")
    if lease_binding is not None and (
        not isinstance(lease_binding, Mapping)
        or set(lease_binding) != {"path", "owner_nonce", "deadline_at_utc"}
        or lease_binding.get("path") != claude_upgrade.CAMPAIGN_LEASE_FILENAME
        or not SHA256_RE.fullmatch(str(lease_binding.get("owner_nonce", "")))
        or not claude_upgrade._is_rfc3339_timestamp(lease_binding.get("deadline_at_utc"))
    ):
        raise ConfigurationError("抓包预约 Campaign lease 绑定非法。")
    planned = payload.get("planned_jobs")
    if not isinstance(planned, list) or not planned:
        raise ConfigurationError("抓包预约缺少计划任务。")
    seen: set[str] = set()
    for item in planned:
        if (
            not isinstance(item, dict)
            or set(item) != {"id", "required", "execution_sha256"}
            or not SAFE_ID_RE.fullmatch(str(item.get("id", "")))
            or item["id"] in seen
            or not isinstance(item.get("required"), bool)
            or not SHA256_RE.fullmatch(str(item.get("execution_sha256", "")))
        ):
            raise ConfigurationError("抓包预约计划任务非法或重复。")
        seen.add(str(item["id"]))
    return payload


def _load_capture_reservation(
    campaign_dir: Path,
    attempt_root: Path,
    *,
    phase: str,
    candidate_id: str | None,
    _manifest: Mapping[str, Any] | None = None,
    _historical_manifest_controls: bool = False,
) -> dict[str, Any]:
    """服务面成员：读取正式 attempt 原子发布前即存在的不可变预约收据（Claude 没有历史控制纪元）。"""

    del _historical_manifest_controls
    manifest = dict(_manifest) if isinstance(_manifest, Mapping) else claude_upgrade.load_campaign_manifest(campaign_dir)
    return _load_reservation(campaign_dir, attempt_root, phase=phase, candidate_id=candidate_id, manifest=manifest, mode=MODE_FORMAL)


def _campaign_attempt_roots(
    campaign_dir: Path,
) -> list[tuple[str, str | None, Path]]:
    """服务面成员：枚举 Campaign 内全部正式 attempt 目录（不含演练命名空间），忽略未发布的隐藏临时目录。"""

    scopes: list[tuple[str, str | None, Path]] = [("official", None, campaign_dir / "official" / "attempts")]
    candidates_root = campaign_dir / "candidates"
    if candidates_root.exists():
        if candidates_root.is_symlink() or not candidates_root.is_dir():
            raise ConfigurationError("候选抓包目录不可信。")
        for candidate_root in sorted(candidates_root.iterdir()):
            if not candidate_root.is_dir() or candidate_root.is_symlink():
                continue
            if not SAFE_ID_RE.fullmatch(candidate_root.name):
                raise ConfigurationError("候选抓包目录包含非法 candidate-id。")
            scopes.append(("candidate", candidate_root.name, candidate_root / "attempts"))
    attempts: list[tuple[str, str | None, Path]] = []
    for phase, candidate_id, attempts_root in scopes:
        if not attempts_root.exists():
            continue
        if attempts_root.is_symlink() or not attempts_root.is_dir():
            raise ConfigurationError("抓包 attempts 目录不可信。")
        for attempt_root in sorted(attempts_root.iterdir()):
            if not attempt_root.is_dir() or attempt_root.is_symlink():
                continue
            if not SAFE_ID_RE.fullmatch(attempt_root.name):
                if attempt_root.name.startswith(".reservation-"):
                    continue
                raise ConfigurationError("抓包目录包含非法 attempt-id。")
            attempts.append((phase, candidate_id, attempt_root))
    return attempts


def _ordered_capture_attempts(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
    *,
    _historical_manifest_controls: bool = False,
    _manifest: Mapping[str, Any] | None = None,
) -> list[tuple[Path, dict[str, Any]]]:
    """服务面成员：按预约微秒时间倒序排列正式 attempt，编号仅作为同刻平局键。"""

    root = campaign_dir / claude_upgrade._capture_attempt_relative(phase, candidate_id) / "attempts"
    if not root.is_dir() or root.is_symlink():
        return []
    attempts: list[tuple[Path, dict[str, Any]]] = []
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink() or not SAFE_ID_RE.fullmatch(path.name):
            continue
        attempts.append(
            (
                path,
                claude_upgrade._load_capture_reservation(
                    campaign_dir,
                    path,
                    phase=phase,
                    candidate_id=candidate_id,
                    _manifest=_manifest,
                    _historical_manifest_controls=_historical_manifest_controls,
                ),
            )
        )
    return sorted(
        attempts,
        key=lambda item: (claude_upgrade._rfc3339_datetime(item[1]["started_at_utc"], "抓包预约 started_at_utc"), item[0].name),
        reverse=True,
    )


def _verify_attempt_document(
    campaign_dir: Path,
    attempt_root: Path,
    payload: Mapping[str, Any],
    reservation: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    phase: str,
    mode: str,
) -> None:
    """attempt 收据的身份、摘要、状态、失败事实与制品绑定（心跳、超时 checkpoint、Job checkpoint、Job 闭环收据）。"""

    unsigned = dict(payload)
    expected_digest = unsigned.pop("attempt_digest", None)
    statuses = FORMAL_ATTEMPT_STATUSES if mode == MODE_FORMAL else REHEARSAL_ATTEMPT_STATUSES
    if (
        payload.get("schema_version") != CAPTURE_ATTEMPT_SCHEMA
        or payload.get("client") != CLIENT
        or payload.get("mode") != mode
        or payload.get("campaign_id") != manifest["campaign_id"]
        or payload.get("campaign_mode") != reservation.get("campaign_mode")
        or payload.get("campaign_purpose") != reservation.get("campaign_purpose")
        or payload.get("campaign_manifest_sha256") != file_sha256(campaign_dir / "campaign.json")
        or payload.get("attempt_id") != attempt_root.name
        or payload.get("phase") != phase
        or payload.get("candidate_id") != reservation.get("candidate_id")
        or payload.get("candidate_purpose") != reservation.get("candidate_purpose")
        or payload.get("run_nonce") != reservation.get("run_nonce")
        or payload.get("started_at_utc") != reservation.get("started_at_utc")
        or not claude_upgrade._is_rfc3339_timestamp(payload.get("completed_at_utc"))
        or not isinstance(payload.get("identity"), Mapping)
        or claude_upgrade._fingerprint(payload["identity"]) != reservation.get("identity_sha256")
        or payload.get("reservation")
        != {
            "path": (attempt_root / "reservation.json").relative_to(campaign_dir).as_posix(),
            "sha256": file_sha256(attempt_root / "reservation.json"),
        }
        or not SHA256_RE.fullmatch(str(expected_digest))
        or claude_upgrade._fingerprint(unsigned) != expected_digest
        or payload.get("status") not in statuses
    ):
        raise ConfigurationError("抓包 attempt 身份、摘要或状态不一致。")
    planned = {str(item["id"]): str(item["execution_sha256"]) for item in reservation["planned_jobs"]}
    for result in payload.get("results") or []:
        if not isinstance(result, Mapping) or planned.get(str(result.get("id"))) != result.get("execution_sha256"):
            raise ConfigurationError("抓包 attempt 任务不在原子预约内或执行摘要漂移。")
        closure = result.get("job_closure")
        if closure is not None:
            path = attempt_root / str(closure.get("path", ""))
            if path.is_symlink() or not path.is_file() or file_sha256(path) != closure.get("sha256"):
                raise ConfigurationError(f"Job {result.get('id')} 的闭环收据缺失或摘要漂移。")
    _validate_attempt_failure_facts(payload)
    if phase == "candidate":
        # 候选 attempt（B4-01b）：网关前后快照、恢复核验与 selector 观察文件逐个绑定，运行条件字段闭合。
        from tools.official_client_capture import claude_upgrade_candidate_capture

        claude_upgrade_candidate_capture.verify_attempt_bindings(attempt_root, payload)
    watchdog = payload.get("watchdog") if isinstance(payload.get("watchdog"), Mapping) else {}
    for field in ("heartbeat", "timeout_checkpoint"):
        binding = watchdog.get(field)
        if binding is None:
            continue
        target = claude_upgrade._campaign_file(campaign_dir, str(binding.get("path", "")))
        if target.is_symlink() or not target.is_file() or file_sha256(target) != binding.get("sha256"):
            raise ConfigurationError(f"attempt watchdog {field} 绑定漂移。")
    checkpoint = payload.get("job_checkpoint")
    if isinstance(checkpoint, Mapping):
        store = incremental_recovery.CheckpointStore(attempt_root / "checkpoints", create=False)
        records = store.records()
        if len(records) != checkpoint.get("record_count") or (records and records[-1].get("checkpoint_sha256") != checkpoint.get("last_sha256")):
            raise ConfigurationError("attempt Job checkpoint 链与收据绑定不一致。")


def _load_capture_attempt(
    campaign_dir: Path,
    phase: str,
    candidate_id: str | None,
    attempt_id: str,
    *,
    _historical_manifest_controls: bool = False,
    _verified_campaign_manifest: Mapping[str, Any] | None = None,
    _replay_evidence_permissions: bool = True,
) -> tuple[Path, dict[str, Any]]:
    """服务面成员：读取并重验正式 run 阶段的不可变 attempt（只在 Formal Campaign 上）。

    ``_replay_evidence_permissions`` 为真时按 attempt 记录的收口参数重放证据边界——重放属于 seal 链（B2-07），经编排器的
    ``_replay_attempt_evidence_permissions``（连同 ``_evidence_root_relocations``）进行；只读核对状态的调用传假。
    """

    del _historical_manifest_controls
    manifest = (
        dict(_verified_campaign_manifest)
        if isinstance(_verified_campaign_manifest, Mapping)
        else claude_upgrade.load_campaign_manifest(campaign_dir)
    )
    claude_upgrade._require_formal_campaign(campaign_dir, manifest)
    if phase == "official" and candidate_id is not None:
        raise ConfigurationError("official attempt 不得携带 candidate-id。")
    if phase not in {"official", "candidate"}:
        raise ConfigurationError(f"未知取证阶段：{phase}")
    if not SAFE_ID_RE.fullmatch(str(attempt_id)):
        raise ConfigurationError("attempt-id 非法。")
    attempt_root = campaign_dir / claude_upgrade._capture_attempt_relative(phase, candidate_id) / "attempts" / attempt_id
    path = attempt_root / "attempt.json"
    claude_upgrade._reject_symlink_components(path, campaign_dir, "抓包 attempt 收据")
    payload = claude_upgrade._read_json(path, "抓包 attempt 收据")
    reservation = claude_upgrade._load_capture_reservation(
        campaign_dir, attempt_root, phase=phase, candidate_id=candidate_id, _manifest=manifest
    )
    _verify_attempt_document(campaign_dir, attempt_root, payload, reservation, manifest, phase=phase, mode=MODE_FORMAL)
    if _replay_evidence_permissions:
        claude_upgrade._replay_attempt_evidence_permissions(
            attempt_root,
            payload,
            root_relocations=claude_upgrade._evidence_root_relocations(
                campaign_dir, phase=phase, candidate_id=candidate_id, attempt_id=attempt_id
            ),
        )
    return attempt_root, payload


def _attempt_reconciled_terminal(campaign_dir: Path, attempt_id: str) -> Mapping[str, Any] | None:
    """已对账的孤儿 attempt（没有 attempt.json）以共享对账器的对账收据为终态，不再视为 active（B2-08 落实，与 Codex 同源）。"""

    from tools.official_client_capture import codex_upgrade_reconciler as reconciler

    try:
        return reconciler.attempt_reconciled_terminal(campaign_dir, attempt_id)
    except reconciler.ReconcilerError as error:
        raise ConfigurationError(str(error)) from error


def _invalidated_attempts(campaign_dir: Path) -> set[tuple[str, str | None, str]]:
    """被环境隔离或证据根冲突隔离作废的 attempt（B2-08 落实）；两类隔离制品都不存在时为空集。"""

    if any(
        (campaign_dir / directory).exists()
        for directory in (claude_upgrade.ENVIRONMENT_ISOLATION_DIR, claude_upgrade.EVIDENCE_CONFLICT_QUARANTINE_DIR)
    ):
        return set(claude_upgrade._rerun_all_invalidated_attempts(campaign_dir))
    return set()


def _official_sealed(campaign_dir: Path) -> bool:
    return any((campaign_dir / marker).exists() or (campaign_dir / marker).is_symlink() for marker in OFFICIAL_SEALED_MARKERS)


def _active_unsealed_attempts(
    campaign_dir: Path,
    phase: str,
    *,
    _manifest: Mapping[str, Any] | None = None,
    _baseline: int | None = None,
) -> list[str]:
    """服务面成员：列出未完成预约（预约后中断）或等待封存、且未被阶段结果绑定的正式 attempt。

    已封存阶段的 attempt 绑定由 VC 阶段读取（B3-01）给出；只读核对状态，不重放证据边界（重放属于 seal 链）。
    """

    del _baseline
    if phase == "candidate":
        return _active_unsealed_candidate_attempts(campaign_dir, _manifest=_manifest)
    if phase != "official":
        raise ConfigurationError(f"未知取证阶段：{phase}")
    sealed_attempt_id: str | None = None
    if _official_sealed(campaign_dir):
        stage = claude_upgrade._load_stage_result(campaign_dir, "capture-official", None, _verified_campaign_manifest=_manifest)
        reference = stage.get("attempt")
        if not isinstance(reference, Mapping):
            raise ConfigurationError("已封存官方阶段缺少 attempt 绑定。")
        sealed_attempt_id = Path(str(reference.get("path", ""))).parent.name
    invalidated = _invalidated_attempts(campaign_dir)
    active: list[str] = []
    for current_phase, candidate_id, attempt_root in claude_upgrade._campaign_attempt_roots(campaign_dir):
        if current_phase != "official":
            continue
        claude_upgrade._load_capture_reservation(campaign_dir, attempt_root, phase="official", candidate_id=None, _manifest=_manifest)
        attempt_path = attempt_root / "attempt.json"
        if not attempt_path.exists():
            if _attempt_reconciled_terminal(campaign_dir, attempt_root.name) is not None:
                continue
            active.append(f"official:{attempt_root.name}:reserved_or_interrupted")
            continue
        _root, attempt = claude_upgrade._load_capture_attempt(
            campaign_dir, "official", candidate_id, attempt_root.name, _verified_campaign_manifest=_manifest, _replay_evidence_permissions=False
        )
        if (
            attempt.get("status") == "awaiting_receipts"
            and attempt_root.name != sealed_attempt_id
            and ("official", None, attempt_root.name) not in invalidated
        ):
            active.append(f"official:{attempt_root.name}")
    return active


def _active_unsealed_candidate_attempts(campaign_dir: Path, *, _manifest: Mapping[str, Any] | None) -> list[str]:
    """候选阶段的未完成预约或等待封存 attempt（B4-01b，与 Codex 同一判据与 ``<candidate-id>:<attempt-id>`` 写法）。

    已封存的候选以 b0 阶段结果（``candidates/<cid>/result.json``）绑定的 attempt 为准；候选阶段结果的读取由 B4-02 落实，
    在那之前存在阶段结果即按待实现失败关闭。只读核对状态，不重放证据边界。
    """

    from tools.official_client_capture import claude_upgrade_candidate_capture

    invalidated = _invalidated_attempts(campaign_dir)
    active: list[str] = []
    sealed: dict[str, str | None] = {}
    for current_phase, candidate_id, attempt_root in claude_upgrade._campaign_attempt_roots(campaign_dir):
        if current_phase != "candidate" or candidate_id is None:
            continue
        if candidate_id not in sealed:
            sealed[candidate_id] = None
            result_path = claude_upgrade_candidate_capture.legacy_stage_result_path(campaign_dir, candidate_id)
            if result_path.exists() or result_path.is_symlink():
                stage = claude_upgrade._load_stage_result(campaign_dir, "capture-candidate", candidate_id, _verified_campaign_manifest=_manifest)
                reference = stage.get("attempt")
                if not isinstance(reference, Mapping):
                    raise ConfigurationError("已封存候选阶段缺少 attempt 绑定。")
                sealed[candidate_id] = Path(str(reference.get("path", ""))).parent.name
        claude_upgrade._load_capture_reservation(campaign_dir, attempt_root, phase="candidate", candidate_id=candidate_id, _manifest=_manifest)
        if not (attempt_root / "attempt.json").exists():
            if _attempt_reconciled_terminal(campaign_dir, attempt_root.name) is not None:
                continue
            active.append(f"{candidate_id}:{attempt_root.name}:reserved_or_interrupted")
            continue
        _root, attempt = claude_upgrade._load_capture_attempt(
            campaign_dir, "candidate", candidate_id, attempt_root.name, _verified_campaign_manifest=_manifest, _replay_evidence_permissions=False
        )
        if (
            attempt.get("status") == "awaiting_receipts"
            and attempt_root.name != sealed[candidate_id]
            and ("candidate", candidate_id, attempt_root.name) not in invalidated
        ):
            active.append(f"{candidate_id}:{attempt_root.name}")
    return active


def _failed_capture_attempts(
    campaign_dir: Path,
    *,
    manifest: Mapping[str, Any],
    phase: str = "official",
    candidate_id: str | None = None,
) -> list[str]:
    """尚未经显式 resume（B2-08；候选续跑属 B4-01c）处理的失败或污染正式 attempt（候选阶段只看本候选）。"""

    failed: list[str] = []
    for current_phase, current_candidate, attempt_root in claude_upgrade._campaign_attempt_roots(campaign_dir):
        if current_phase != phase or current_candidate != candidate_id or not (attempt_root / "attempt.json").is_file():
            continue
        _root, attempt = claude_upgrade._load_capture_attempt(
            campaign_dir, phase, current_candidate, attempt_root.name, _verified_campaign_manifest=manifest, _replay_evidence_permissions=False
        )
        if attempt.get("status") in {"failed", "environment_contaminated"}:
            failed.append(f"{current_candidate or 'official'}:{attempt_root.name}")
    return failed


def _capture_stage_attempt_context(
    campaign_dir: Path,
    stage: Mapping[str, Any],
    *,
    phase: str,
    candidate_id: str | None,
) -> tuple[Path, dict[str, Any]]:
    """服务面成员：阶段结果绑定的 attempt（路径与 attempt 收据位置逐字一致）。"""

    reference = stage.get("attempt")
    claude_upgrade._require_file_binding(reference, "抓包 attempt")
    attempt_path = claude_upgrade._campaign_file(campaign_dir, str(reference["path"]))  # type: ignore[index]
    attempt_root, attempt = claude_upgrade._load_capture_attempt(campaign_dir, phase, candidate_id, attempt_path.parent.name)
    if attempt_path != attempt_root / "attempt.json":
        raise ConfigurationError("抓包阶段 attempt 绑定路径不一致。")
    return attempt_root, attempt


def _reserve_capture_attempt(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    mode: str,
    identity: Mapping[str, Any],
    jobs: Sequence[claude_upgrade.Job],
    deadline: incremental_recovery.WallClockDeadline,
    lease: Any | None,
    phase: str = "official",
    candidate_id: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    """在 Campaign 锁内原子发布预约（隐藏临时目录 + rename），关闭 check-then-create 与空目录窗口。

    候选阶段（B4-01b）在锁内复核：本候选未封存、Campaign 没有未封存的预约或 attempt（官方与候选）、本候选没有待续跑的
    失败或污染 attempt（候选续跑属于 B4-01c）。
    """

    deadline.check("attempt:reserve:start")
    if lease is not None:
        lease.check("attempt:reserve:start")
    if phase not in {"official", "candidate"} or (phase == "candidate") != bool(candidate_id):
        raise ConfigurationError("预约的阶段与 candidate-id 不一致。")
    with claude_upgrade._campaign_lock(campaign_dir, deadline=deadline):
        if mode == MODE_FORMAL and phase == "candidate":
            from tools.official_client_capture import claude_upgrade_candidate_capture

            claude_upgrade_candidate_capture.require_unsealed(campaign_dir, str(candidate_id), revision=None)
            active = [
                *claude_upgrade._active_unsealed_attempts(campaign_dir, "official", _manifest=manifest),
                *claude_upgrade._active_unsealed_attempts(campaign_dir, "candidate", _manifest=manifest),
            ]
            if active:
                raise ConfigurationError(f"Campaign 存在未封存预约或 attempt，禁止并行 run：{active}")
            failed = _failed_capture_attempts(campaign_dir, manifest=manifest, phase="candidate", candidate_id=candidate_id)
            if failed:
                raise ConfigurationError(f"候选存在失败或污染 attempt；候选续跑由 B4-01c 落实，禁止直接再跑 capture-candidate run：{failed}")
        elif mode == MODE_FORMAL:
            if _official_sealed(campaign_dir):
                raise ConfigurationError("官方证据已经封存，禁止创建新 attempt。")
            active = claude_upgrade._active_unsealed_attempts(campaign_dir, "official", _manifest=manifest)
            if active:
                raise ConfigurationError(f"Campaign 存在未封存预约或 attempt，禁止并行 run：{active}")
            failed = _failed_capture_attempts(campaign_dir, manifest=manifest)
            if failed:
                raise ConfigurationError(f"存在失败 attempt；只能经显式 resume（B2-08）续跑，禁止直接 capture-official run：{failed}")
        lease_binding: dict[str, str] | None = None
        if lease is not None:
            if not lease.acquired:
                raise ConfigurationError("reservation 缺少 active Campaign lease。")
            payload = lease.payload
            if payload.get("campaign_id") != manifest.get("campaign_id"):
                raise ConfigurationError("reservation 与 Campaign lease 身份不一致。")
            lease_binding = {
                "path": claude_upgrade.CAMPAIGN_LEASE_FILENAME,
                "owner_nonce": str(payload.get("owner_nonce")),
                "deadline_at_utc": str(payload.get("deadline_at_utc")),
            }
        attempts_root = _private_directory(campaign_dir / _attempt_relative(mode, phase, candidate_id) / "attempts", campaign_dir)
        attempt_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + f"-{secrets.token_hex(8)}"
        if lease is not None:
            lease.bind_attempt(attempt_id)
        final_root = attempts_root / attempt_id
        if final_root.exists() or final_root.is_symlink():
            raise ConfigurationError("随机 attempt-id 发生冲突。")
        reservation: dict[str, Any] = {
            "schema_version": CAPTURE_RESERVATION_SCHEMA,
            "client": CLIENT,
            "mode": mode,
            "campaign_id": manifest["campaign_id"],
            "campaign_mode": manifest["campaign_mode"],
            "campaign_purpose": manifest["campaign_purpose"],
            "campaign_manifest_sha256": file_sha256(campaign_dir / "campaign.json"),
            "phase": phase,
            "candidate_id": candidate_id if phase == "candidate" else None,
            "candidate_purpose": manifest["campaign_purpose"] if phase == "candidate" else None,
            "attempt_id": attempt_id,
            "run_nonce": secrets.token_hex(32),
            "started_at_utc": claude_upgrade._utc_now(),
            "identity_sha256": claude_upgrade._fingerprint(dict(identity)),
            "planned_jobs": [
                {"id": job.job_id, "required": job.required, "execution_sha256": _job_execution_sha256(job)} for job in jobs
            ],
        }
        effective = claude_upgrade._campaign_effective_tool_identity(campaign_dir, manifest)
        evolution = codex_upgrade_wire_transition.reservation_binding(effective)
        if evolution is not None:
            reservation["tool_evolution"] = evolution
        if lease_binding is not None:
            reservation["campaign_lease"] = lease_binding
        reservation["reservation_digest"] = claude_upgrade._fingerprint(reservation)
        temporary_root = Path(tempfile.mkdtemp(prefix=".reservation-", dir=attempts_root))
        temporary_root.chmod(0o700)
        try:
            claude_upgrade._secure_write_json_once(temporary_root / "reservation.json", reservation)
            os.rename(temporary_root, final_root)
        except BaseException:
            shutil.rmtree(temporary_root, ignore_errors=True)
            raise
    return final_root, reservation


def _write_capture_attempt(
    campaign_dir: Path,
    attempt_root: Path,
    payload: dict[str, Any],
    *,
    manifest: Mapping[str, Any],
    reservation: Mapping[str, Any],
    mode: str,
) -> dict[str, Any]:
    """只写一次 attempt 收据：补齐预约身份、失败观测与根因，自摘要后独占发布。"""

    planned = {str(item["id"]): str(item["execution_sha256"]) for item in reservation["planned_jobs"]}
    for result in payload.get("results", []):
        if not isinstance(result, Mapping) or planned.get(str(result.get("id"))) != result.get("execution_sha256"):
            raise ConfigurationError("抓包 attempt 任务不在原子预约内或执行摘要漂移。")
    if claude_upgrade._fingerprint(dict(payload["identity"])) != reservation["identity_sha256"]:
        raise ConfigurationError("抓包 attempt 身份与原子预约不一致。")
    document = dict(payload)
    document.update(
        {
            "schema_version": CAPTURE_ATTEMPT_SCHEMA,
            "client": CLIENT,
            "mode": mode,
            "campaign_mode": reservation["campaign_mode"],
            "campaign_purpose": reservation["campaign_purpose"],
            "candidate_purpose": reservation["candidate_purpose"],
            "campaign_manifest_sha256": file_sha256(campaign_dir / "campaign.json"),
            "attempt_id": attempt_root.name,
            "run_nonce": reservation["run_nonce"],
            "started_at_utc": reservation["started_at_utc"],
            "completed_at_utc": claude_upgrade._utc_now(),
            "reservation": {
                "path": (attempt_root / "reservation.json").relative_to(campaign_dir).as_posix(),
                "sha256": file_sha256(attempt_root / "reservation.json"),
            },
        }
    )
    failure_observations, root_causes = _attempt_failure_facts(
        {**document, "failure_observations": document.get("attempt_failure_observations", [])}
    )
    document["failure_observations"] = failure_observations
    document["root_causes"] = root_causes
    document["attempt_digest"] = claude_upgrade._fingerprint(document)
    claude_upgrade._secure_write_json_once(attempt_root / "attempt.json", document)
    return document


# ---------------------------------------------------------------------------
# capture-official run：attempt 主流程
# ---------------------------------------------------------------------------


def _require_capture_budget(deadline: incremental_recovery.WallClockDeadline, *, operation: str, reservation: bool = False) -> None:
    """预约与每个 Job 之前保留完整的 after 探针与终态清理预算（共享实现，在 Claude 适配器作用域里调用）。"""

    with upgrade_control_plane.adapter_scope(_client_adapter()):
        upgrade_control_plane._require_capture_budget_before_data_action(deadline, operation=operation, reservation=reservation)


def _campaign_for_mode(campaign_dir: Path, mode: str) -> dict[str, Any]:
    """正式 run 只认 Formal Campaign；零请求演练只认 preflight_only Campaign（演练不得混入正式 attempt 序列）。"""

    if mode == MODE_FORMAL:
        return claude_upgrade._require_formal_campaign(campaign_dir)
    manifest = claude_upgrade.load_campaign_manifest(campaign_dir)
    mode_value, _purpose = claude_upgrade._campaign_coordinates(manifest)
    if mode_value != "preflight_only":
        raise ConfigurationError("零请求演练只能在 preflight_only Campaign 上运行；Formal Campaign 只接受正式 run。")
    return manifest


def _run_mode(arguments: argparse.Namespace, phase: str = "official") -> str:
    rehearsal = bool(getattr(arguments, "zero_request_rehearsal", False))
    live = bool(getattr(arguments, "acknowledge_live_requests", False))
    if rehearsal and live:
        raise ConfigurationError("--zero-request-rehearsal 与 --acknowledge-live-requests 互斥。")
    if not rehearsal and not live:
        raise ConfigurationError("抓包会产生真实请求，必须同时确认 --acknowledge-live-requests（零请求演练用 --zero-request-rehearsal）。")
    if getattr(arguments, "job_id", None) and not rehearsal:
        raise ConfigurationError(f"--job-id 只用于零请求演练；正式 run 执行冻结的全部 {phase} Job，续跑由 resume 处理。")
    if rehearsal and not getattr(arguments, "job_id", None):
        raise ConfigurationError("零请求演练必须用 --job-id 指定一个 Job。")
    return MODE_REHEARSAL if rehearsal else MODE_FORMAL


def _attempt_identity(manifest: Mapping[str, Any], layout: CaptureLayout, mode: str) -> dict[str, Any]:
    """attempt 的官方运行坐标身份（不含 nonce 与证据路径）：恢复时按它判同身份。"""

    return {
        "client": CLIENT,
        "phase": "official",
        "mode": mode,
        "target_version": str(manifest["target_version"]),
        "target_sha256": str(manifest["target_sha256"]),
        "official_identity_sha256": claude_upgrade._fingerprint(dict(manifest["official_identity"])),
        "runtime_image": layout.runtime_image,
        "capture_container": layout.capture_container,
        "synthetic_capture_container": layout.synthetic_container,
        "capture_tool_root": str(layout.capture_tool_root),
        "claude_bin": str(manifest["configuration"]["claude_bin"]),
    }


def _error_record(error: BaseException | None) -> dict[str, str] | None:
    if error is None:
        return None
    return {"type": type(error).__name__, "message": str(error)[:1000]}


def run_capture_attempt(
    arguments: argparse.Namespace,
    phase: str = "official",
    *,
    _lease: Any | None = None,
    _manifest: dict[str, Any] | None = None,
    _deadline: incremental_recovery.WallClockDeadline | None = None,
) -> dict[str, Any]:
    """执行一次官方取证 attempt（正式或零请求演练），返回不含秘密的摘要。

    预约之前的门禁不写任何 attempt 数据（只有租约与监督器自身的状态文件）：模式与确认参数（取租约之前）、Campaign 模式、
    污染与封存状态、工具身份已登记、Job 展开、坐标布局、执行树、官方二进制与容器内必需文件。预约之后无论成败都写出
    attempt 收据（中断信号在写完收据后再抛出）。
    """

    if phase not in {"official", "candidate"}:
        raise ConfigurationError(f"未知取证阶段：{phase}")
    mode = _run_mode(arguments, phase)
    candidate_id: str | None = None
    if phase == "candidate":
        from tools.official_client_capture import claude_upgrade_candidate_capture as candidate_capture

        candidate_id = candidate_capture.require_candidate_id(arguments)
    campaign_dir = Path(arguments.campaign_dir)
    claude_upgrade._validate_existing_campaign_path(campaign_dir)
    # 一律用解析后的真实路径：权限收口按路径字符串绑定内部根，收据重放必须得到同一组字符串。
    campaign_dir = campaign_dir.resolve(strict=True)
    active = claude_upgrade._ACTIVE_CAMPAIGN_LEASE
    if _lease is None and active is not None:
        if not claude_upgrade._lease_identity_matches(active, campaign_dir, phase=phase, candidate_id=candidate_id):
            raise ConfigurationError("capture attempt 与当前 Campaign lease 的 phase／candidate 不一致。")
        _lease = active
        _deadline = _bind_attempt_deadline_metadata(_deadline or active.deadline, phase)
    if _lease is None:
        manifest = _manifest or _campaign_for_mode(campaign_dir, mode)
        deadline = _deadline or claude_upgrade._attempt_deadline(arguments, phase)
        with claude_upgrade.CampaignLease(
            campaign_dir,
            phase=phase,
            candidate_id=candidate_id,
            deadline=deadline,
            command=f"capture-{phase}",
            allow_stale_recovery=False,
            campaign_id=str(manifest.get("campaign_id", "")),
        ) as lease:
            return run_capture_attempt(arguments, phase, _lease=lease, _manifest=manifest, _deadline=deadline)

    manifest = _manifest or _campaign_for_mode(campaign_dir, mode)
    deadline = _bind_attempt_deadline_metadata(_deadline or _lease.deadline, phase)
    # —— 预约前门禁（零写入） ——
    if (campaign_dir / CONTAMINATION_MARKER).exists():
        if mode == MODE_FORMAL:
            claude_upgrade._reject_contaminated_campaign(campaign_dir)
        raise ConfigurationError("Campaign 已记录环境污染；演练 Campaign 请重新生成，正式 Campaign 走环境隔离（B2-08）。")
    if mode == MODE_FORMAL and phase == "official" and _official_sealed(campaign_dir):
        raise ConfigurationError("官方证据已经封存，禁止重复抓包。")
    current_tool = claude_upgrade._tool_identity(include_git=False)
    claude_upgrade._require_tool_evolution_registered(campaign_dir, manifest, current=current_tool, action=f"capture-{phase} run")
    admission: Any | None = None
    selector_observation: dict[str, Any] | None = None
    if phase == "candidate":
        # 候选入口门（B4-01b，见 claude_upgrade_candidate_capture 第一节）：正式 run 依次核 VC-4、Store、构建收据、服务镜像、
        # 运行坐标覆盖与未封存；演练只核候选变量与覆盖收据。之后全程使用入口门给出的生效清单。
        admit = candidate_capture.formal_admission if mode == MODE_FORMAL else candidate_capture.rehearsal_admission
        admission = admit(campaign_dir, manifest, str(candidate_id), deadline=deadline)
        manifest = admission.manifest
    jobs = claude_upgrade._campaign_jobs(campaign_dir, manifest, phase, candidate_id=candidate_id)
    requested_job = getattr(arguments, "job_id", None)
    if mode == MODE_REHEARSAL:
        jobs = [job for job in jobs if job.job_id == requested_job]
        if len(jobs) != 1:
            raise ConfigurationError(f"冻结的 {phase} Job 里没有 {requested_job}。")
    layout = _capture_layout(campaign_dir, manifest)
    host_tool_parent = _host_path(layout.capture_tool_root, layout).parents[1]
    claude_upgrade._verify_execution_tree(host_tool_parent)
    binary_verification = claude_upgrade._verify_official_binaries(manifest, deadline=deadline)
    preconditions = _container_preconditions(manifest, layout, mode=mode, jobs=jobs, deadline=deadline, heartbeat=None, phase=phase)
    if admission is not None:
        identity = candidate_capture.attempt_identity(manifest, layout, mode, admission)
        # 账号刷新触发的步骤（演练时宿主脚本的只读前检同样核管理凭据）：预约前确认环境里给出了管理凭据。
        candidate_capture.require_admin_token_for(jobs)
        # 网关只读就绪核对（不落盘）：残留临时代理、临时 CA、运行目录或账号形态不对时在预约前拒绝，不留失败 attempt。
        readiness = candidate_capture.probe_gateway_environment(manifest, layout.host_data_root, "readiness", deadline=deadline)
        not_ready = candidate_capture.readiness_failures(readiness)
        if not_ready:
            raise ConfigurationError(f"候选网关环境未就绪：{not_ready}（先清理上一轮残留或修正专用账号与 API Key）。")
        # 生产 selector 只读观察：正式 run 必须有 Campaign 冻结的 Active Release 目录（预约前读一次，预约后原样落盘）。
        selector_observation = candidate_capture.observe_selectors(campaign_dir, manifest, required=mode == MODE_FORMAL)
    else:
        identity = _attempt_identity(manifest, layout, mode)
    for job in jobs:
        _job_tool_dependency_files(job, current_tool)
    _require_capture_budget(deadline, operation="attempt:reservation-admission", reservation=True)
    # —— 原子预约（此后一律写 attempt 收据） ——
    attempt_root, reservation = _reserve_capture_attempt(
        campaign_dir,
        manifest,
        mode=mode,
        identity=identity,
        jobs=jobs,
        deadline=deadline,
        lease=_lease,
        phase=phase,
        candidate_id=candidate_id,
    )
    log_root = _private_directory(attempt_root / "logs", campaign_dir)
    evidence_root = _private_directory(attempt_root / "evidence", campaign_dir)
    environment_root = _private_directory(evidence_root / "environment", evidence_root)
    heartbeat_path = attempt_root / "watchdog-heartbeat.json"
    context = ScenarioReceiptContext(
        campaign_id=str(manifest["campaign_id"]),
        attempt_id=attempt_root.name,
        run_nonce=str(reservation["run_nonce"]),
        evidence_root=evidence_root,
        campaign_dir=campaign_dir,
        attempt_root=attempt_root,
        mode=mode,
        layout=layout,
        manifest=manifest,
    )

    def heartbeat(operation: str) -> None:
        _write_attempt_heartbeat(heartbeat_path, deadline, operation=operation, attempt_root=attempt_root)

    results: list[dict[str, Any]] = []
    attempt_observations: list[dict[str, str]] = []
    execution_error: BaseException | None = None
    restoration_error: BaseException | None = None
    before_manifest: dict[str, Any] | None = None
    after_manifest: dict[str, Any] | None = None
    attempt_drift: list[str] = []
    checkpoint_store: incremental_recovery.CheckpointStore | None = None
    timeout_checkpoint_path: Path | None = None
    contaminated = False
    # 候选阶段（B4-01b）：网关前后快照、漂移字段、selector 观察文件与网关坐标。
    gateway_before: dict[str, Any] | None = None
    gateway_before_path: Path | None = None
    gateway_after_path: Path | None = None
    gateway_drifted: list[str] = []
    selector_path: Path | None = None
    gateway: dict[str, Any] | None = None
    try:
        checkpoint_store = incremental_recovery.CheckpointStore(attempt_root / "checkpoints")
        _write_attempt_heartbeat(heartbeat_path, deadline, operation="attempt:reserved", force=True, attempt_root=attempt_root)
        deadline.check("attempt:before-probe")
        before_manifest = claude_upgrade._probe_capture_environment(dict(manifest), environment_root / "before", "before", deadline=deadline, heartbeat=heartbeat)
        if before_manifest.get("capture_processes"):
            attempt_observations.append({"check_id": "attempt.environment.processes", "failure_code": "residual"})
            raise ConfigurationError("采集容器（抓包或断网）存在残留取证进程，环境不干净，拒绝执行任何 Job。")
        if admission is not None:
            deadline.check("attempt:gateway-before")
            gateway_before = candidate_capture.probe_gateway_environment(
                manifest, layout.host_data_root, "before", deadline=deadline, heartbeat=heartbeat
            )
            gateway_before_path = candidate_capture.write_gateway_snapshot(environment_root, gateway_before)
            gateway = candidate_capture.gateway_coordinates(admission.variables, service_port=gateway_before["service"]["port"])
            not_ready = candidate_capture.readiness_failures(gateway_before)
            if not_ready:
                attempt_observations.extend({"check_id": f"attempt.gateway.{code}", "failure_code": "not-ready"} for code in not_ready)
                raise ConfigurationError(f"候选网关环境未就绪：{not_ready}，拒绝执行任何 Job。")
            if selector_observation is not None:
                selector_path = environment_root / candidate_capture.SELECTOR_FILENAME
                claude_upgrade._secure_write_json_once(selector_path, selector_observation)
        for job in jobs:
            _require_capture_budget(deadline, operation=f"job:{job.job_id}:admission")
            _write_attempt_heartbeat(heartbeat_path, deadline, operation=f"job:{job.job_id}:start", attempt_root=attempt_root)
            job_root = _private_directory(context.job_root(job.job_id), attempt_root)
            _private_directory(job_root / "evidence", attempt_root)
            job_log_root = _private_directory(job_root / "logs", attempt_root)
            result = claude_upgrade._run_job_with_retry(
                job,
                job_log_root,
                context,
                identity=identity,
                tool_identity=current_tool,
                deadline=deadline,
                heartbeat=heartbeat,
            )
            closure_error: BaseException | None = None
            try:
                result["job_closure"] = _close_job(context, job, result)
            except ConfigurationError as error:
                closure_error = error
                result["status"] = "failed"
                result["job_closure"] = None
                result["job_closure_error"] = _error_record(error)
                result["failure_observations"] = _normalize_failure_observations(
                    [*result.get("failure_observations", []), {"check_id": f"job.{job.job_id}.evidence-permissions", "failure_code": "closeout-failed"}],
                    phase=job.phase,
                )
            results.append(result)
            records = checkpoint_store.records()
            checkpoint_store.append(
                {
                    "checkpoint_schema_version": JOB_CHECKPOINT_SCHEMA,
                    "client": CLIENT,
                    "mode": mode,
                    "campaign_id": manifest["campaign_id"],
                    "phase": phase,
                    "attempt_id": attempt_root.name,
                    "run_nonce": reservation["run_nonce"],
                    "item_id": job.job_id,
                    "status": "complete" if result.get("status") == "complete" else "failed",
                    "disposition": "executed",
                    "result_sha256": incremental_recovery.digest(result),
                    "result_key": result.get("incremental_result_key"),
                    "result": result,
                    "previous_checkpoint_sha256": records[-1].get("checkpoint_sha256") if records else None,
                }
            )
            claude_upgrade._secure_write_json_once(attempt_root / f"job-{job.job_id}.json", result)
            _write_attempt_heartbeat(
                heartbeat_path, deadline, operation=f"job:{job.job_id}:complete", last_completed_job_id=job.job_id, force=True, attempt_root=attempt_root
            )
            if closure_error is not None:
                raise closure_error
            if result.get("environment_contaminated"):
                contaminated = True
                break
    except BaseException as error:
        execution_error = error
    finally:
        try:
            if before_manifest is not None and not deadline.expired:
                deadline.check("attempt:after-probe")
                after_manifest = claude_upgrade._probe_capture_environment(
                    dict(manifest), environment_root / "after", "after", deadline=deadline, heartbeat=heartbeat
                )
                attempt_drift = _probe_drift(before_manifest, after_manifest)
                if attempt_drift:
                    contaminated = True
                    attempt_observations.extend(
                        {"check_id": f"attempt.environment.{kind}", "failure_code": "drifted"} for kind in attempt_drift
                    )
        except BaseException as error:
            restoration_error = error
            if before_manifest is not None:
                contaminated = True
                attempt_observations.append({"check_id": "attempt.environment.after-probe", "failure_code": "probe-failed"})
        if admission is not None and gateway_before is not None:
            # 候选网关 after 快照：与 before 逐字段比较（GATEWAY_COMPARED_FIELDS），漂移或采不到都判环境污染。deadline 到期
            # 也要采（每条只读命令各有 30 秒上限）：生产形态的网关状态不能因为预算用完就不核验。
            try:
                gateway_after = candidate_capture.probe_gateway_environment(manifest, layout.host_data_root, "after")
                gateway_after_path = candidate_capture.write_gateway_snapshot(environment_root, gateway_after)
                gateway_drifted = candidate_capture.gateway_drift(gateway_before, gateway_after)
                if gateway_drifted:
                    contaminated = True
                    attempt_observations.extend(
                        {"check_id": f"attempt.gateway.{name}", "failure_code": "drifted"} for name in gateway_drifted
                    )
            except BaseException as error:
                restoration_error = restoration_error or error
                contaminated = True
                attempt_observations.append({"check_id": "attempt.gateway.after-probe", "failure_code": "probe-failed"})

    if isinstance(execution_error, incremental_recovery.WallClockTimeoutError) or deadline.expired:
        try:
            _write_attempt_heartbeat(heartbeat_path, deadline, operation="attempt:timeout", force=True, allow_expired=True, attempt_root=attempt_root)
            timeout_checkpoint_path = _write_timeout_checkpoint(
                attempt_root,
                deadline,
                operation=getattr(execution_error, "operation", "attempt:deadline") or "attempt:deadline",
                last_completed_job_id=getattr(deadline, "last_completed_job_id", None),
            )
        except BaseException as error:
            execution_error = execution_error or error

    # 候选阶段（B4-01b）：网关隔离恢复核验在权限收口之前落盘（收口边界要包含它）；结果已是归档改写之后的终值。
    candidate_fields: dict[str, Any] = {}
    if admission is not None:
        gateway_recovery: dict[str, Any] = {"schema_version": candidate_capture.GATEWAY_RECOVERY_SCHEMA, "status": "failed", "file": None}
        recovery_path: Path | None = None
        recovery_status = "failed"
        try:
            recovery_path, recovery_status = candidate_capture.write_recovery(
                environment_root,
                results,
                before=gateway_before_path,
                after=gateway_after_path,
                drifted=gateway_drifted,
                evidence_root=evidence_root,
            )
            gateway_recovery = {
                "schema_version": candidate_capture.GATEWAY_RECOVERY_SCHEMA,
                "status": recovery_status,
                "file": _probe_binding(evidence_root, recovery_path),
            }
        except BaseException as error:
            execution_error = execution_error or error
        candidate_fields = {
            "gateway": gateway,
            "gateway_environment": {
                "schema_version": candidate_capture.GATEWAY_ENVIRONMENT_SCHEMA,
                "before": _probe_binding(evidence_root, gateway_before_path) if gateway_before_path is not None else None,
                "after": _probe_binding(evidence_root, gateway_after_path) if gateway_after_path is not None else None,
                "compared_fields": list(candidate_capture.GATEWAY_COMPARED_FIELDS),
                "drifted_fields": list(gateway_drifted),
            },
            "gateway_recovery": gateway_recovery,
            "selector_observed": (
                {"schema_version": candidate_capture.SELECTOR_OBSERVATION_SCHEMA, **_probe_binding(evidence_root, selector_path)}
                if selector_path is not None
                else None
            ),
            # 与 B4-02 证据侧约定的字段（见 claude_upgrade_candidate_capture.candidate_gateway_facts）。
            "candidate_gateway": candidate_capture.candidate_gateway_facts(
                evidence_root=evidence_root,
                gateway=gateway,
                run_conditions=candidate_capture.run_condition_digests(
                    manifest=manifest,
                    gateway=gateway or {},
                    capture_before=(environment_root / "before" / PROBE_MANIFEST_NAME) if before_manifest else None,
                    gateway_before=gateway_before_path,
                ),
                capture_before=(environment_root / "before" / PROBE_MANIFEST_NAME) if before_manifest else None,
                capture_after=(environment_root / "after" / PROBE_MANIFEST_NAME) if after_manifest else None,
                capture_drifted=attempt_drift,
                gateway_before=gateway_before_path,
                gateway_after=gateway_after_path,
                gateway_drifted=gateway_drifted,
                results=results,
                recovery=recovery_path,
                recovery_status=recovery_status,
                selector=(
                    candidate_capture.runtime_catalog_snapshot(selector_observation, admission.persona)
                    if selector_path is not None and selector_observation is not None and admission.persona is not None
                    else None
                ),
            ),
        }

    container_roots = sorted(
        {
            str(root)
            for result in results
            for root in [*(result.get("evidence_roots") or []), *(result.get("archived_evidence_roots") or [])]
        }
    )
    external_roots = _external_host_roots(container_roots, layout)
    closeout_roots = [evidence_root, log_root, *external_roots]
    permission_binding: dict[str, Any] | None = None
    permission_error: BaseException | None = None
    try:
        receipt_path, _receipt = codex_upgrade_evidence_permissions.close_evidence_permissions(
            attempt_root, closeout_roots, managed_data_root=layout.host_data_root, logical_runs_roots=(layout.host_runs_root,)
        )
        permission_binding = codex_upgrade_evidence_permissions.receipt_binding(attempt_root, receipt_path)
    except BaseException as error:
        permission_error = error
        execution_error = execution_error or error

    planned_ok = {job.job_id for job in jobs if job.required} <= {
        str(item.get("id")) for item in results if item.get("status") == "complete"
    }
    clean = execution_error is None and restoration_error is None and after_manifest is not None and permission_binding is not None
    if contaminated:
        status = "environment_contaminated"
    elif mode == MODE_FORMAL:
        status = "awaiting_receipts" if clean and planned_ok else "failed"
    else:
        status = "rehearsal_passed" if clean and planned_ok else "rehearsal_failed"
    checkpoint_summary: dict[str, Any] | None = None
    if checkpoint_store is not None:
        try:
            records = checkpoint_store.records()
            checkpoint_summary = {
                "schema_version": JOB_CHECKPOINT_SCHEMA,
                "path": (attempt_root / "checkpoints").relative_to(campaign_dir).as_posix(),
                "record_count": len(records),
                "last_sequence": records[-1].get("checkpoint_sequence") if records else None,
                "last_sha256": records[-1].get("checkpoint_sha256") if records else None,
            }
        except BaseException as error:
            execution_error = execution_error or error
    watchdog = {
        "schema_version": WATCHDOG_HEARTBEAT_SCHEMA,
        "budget_seconds": deadline.budget_seconds,
        "heartbeat_seconds": getattr(deadline, "heartbeat_seconds", claude_upgrade.DEFAULT_HEARTBEAT_SECONDS),
        "elapsed_seconds": round(deadline.elapsed_seconds, 3),
        "remaining_seconds": round(deadline.remaining_seconds, 3),
        "heartbeat": (
            {"path": heartbeat_path.relative_to(campaign_dir).as_posix(), "sha256": file_sha256(heartbeat_path), "bytes": heartbeat_path.stat().st_size}
            if heartbeat_path.is_file() and not heartbeat_path.is_symlink()
            else None
        ),
        "timeout_checkpoint": (
            {
                "path": timeout_checkpoint_path.relative_to(campaign_dir).as_posix(),
                "sha256": file_sha256(timeout_checkpoint_path),
                "bytes": timeout_checkpoint_path.stat().st_size,
            }
            if timeout_checkpoint_path is not None and timeout_checkpoint_path.is_file()
            else None
        ),
        "last_completed_job_id": getattr(deadline, "last_completed_job_id", None),
    }
    environment = {
        "evidence_root": str(evidence_root),
        "before_probe": _probe_binding(evidence_root, environment_root / "before" / PROBE_MANIFEST_NAME) if before_manifest else None,
        "after_probe": _probe_binding(evidence_root, environment_root / "after" / PROBE_MANIFEST_NAME) if after_manifest else None,
        "drifted_kinds": attempt_drift,
        "continuity_probe_kinds": list(CONTINUITY_PROBE_KINDS),
    }
    if admission is not None:
        # 与 Codex 候选 attempt 同名的恢复收据绑定（共享的候选恢复准入读 environment.restoration_report）：Claude 取网关
        # 隔离恢复核验文件，路径相对 <attempt>/evidence。
        environment["restoration_report"] = candidate_fields["gateway_recovery"]["file"]
    attempt = _write_capture_attempt(
        campaign_dir,
        attempt_root,
        {
            "campaign_id": manifest["campaign_id"],
            "phase": phase,
            "candidate_id": candidate_id,
            "status": status,
            "identity": identity,
            "results": results,
            "attempt_failure_observations": _normalize_failure_observations(attempt_observations, phase=phase) if attempt_observations else [],
            "evidence_roots": [str(path) for path in closeout_roots],
            "evidence_permission_layout": {
                "managed_data_root": str(layout.host_data_root),
                "logical_runs_roots": [str(layout.host_runs_root)],
            },
            "evidence_permission_closeout": permission_binding,
            "evidence_permission_error": _error_record(permission_error),
            "layout": {
                "host_data_root": str(layout.host_data_root),
                "capture_root": str(layout.capture_root),
                "capture_container": layout.capture_container,
                "synthetic_capture_container": layout.synthetic_container,
                "capture_tool_root": str(layout.capture_tool_root),
                "runtime_image": layout.runtime_image,
            },
            "environment": environment,
            "binary_verification": binary_verification,
            "preconditions": preconditions,
            "watchdog": watchdog,
            "job_checkpoint": checkpoint_summary,
            "execution_error": _error_record(execution_error),
            "restoration_error": _error_record(restoration_error),
            "live_request_count_planned": 0 if mode == MODE_REHEARSAL else None,
            "next_gate": (
                f"capture-{phase} seal（{'B2-07' if phase == 'official' else 'B4-02'}）" if status == "awaiting_receipts"
                else "复核 verify-rehearsal" if status == "rehearsal_passed"
                else "环境隔离与恢复（B2-08）" if status == "environment_contaminated"
                else "resume --rerun-failed（B2-08）" if mode == MODE_FORMAL and phase == "official"
                else "候选续跑（B4-01c）" if mode == MODE_FORMAL
                else "修复后重新演练"
            ),
            **candidate_fields,
        },
        manifest=manifest,
        reservation=reservation,
        mode=mode,
    )
    if status == "environment_contaminated":
        try:
            claude_upgrade._secure_write_json_once(
                campaign_dir / CONTAMINATION_MARKER,
                {
                    "schema_version": ENVIRONMENT_CONTAMINATION_SCHEMA,
                    "client": CLIENT,
                    "mode": mode,
                    "phase": phase,
                    "attempt_id": attempt_root.name,
                    "drifted_kinds": attempt_drift,
                    "contaminated_jobs": sorted(str(item.get("id")) for item in results if item.get("environment_contaminated")),
                    **(
                        {"candidate_id": candidate_id, "gateway_drifted_fields": list(gateway_drifted)}
                        if phase == "candidate"
                        else {}
                    ),
                    "recorded_at_utc": claude_upgrade._utc_now(),
                },
            )
        except (ConfigurationError, OSError):
            # attempt.json 的 environment_contaminated 是主事实，标记只是提示。
            pass
    if isinstance(execution_error, (KeyboardInterrupt, SystemExit)):
        raise execution_error
    return {
        "status": status,
        "mode": mode,
        "phase": phase,
        **({"candidate_id": candidate_id} if phase == "candidate" else {}),
        "attempt_id": attempt_root.name,
        "attempt": str(attempt_root),
        "attempt_digest": attempt["attempt_digest"],
        "run_nonce": reservation["run_nonce"],
        "started_at_utc": reservation["started_at_utc"],
        "results": [
            {
                "id": item.get("id"),
                "status": item.get("status"),
                "attempt_index": item.get("attempt_index"),
                "environment_contaminated": bool(item.get("environment_contaminated")),
            }
            for item in results
        ],
        "environment": environment,
        "execution_error": _error_record(execution_error),
        "next_gate": attempt["next_gate"],
    }


def add_capture_official_run_arguments(target: argparse.ArgumentParser) -> None:
    """``capture-official run`` 的参数（seal 的参数由 B2-07 增加）。"""

    target.add_argument("--acknowledge-live-requests", action="store_true", help="正式取证会发真实请求，必须显式确认。")
    target.add_argument(
        "--zero-request-rehearsal",
        action="store_true",
        help="零请求演练：只用于 preflight_only Campaign，relay 改为 --dry-run，不发任何请求（B2-06 验收）。",
    )
    target.add_argument("--job-id", help="零请求演练要执行的单个 official Job。")
    target.add_argument(
        "--max-wall-seconds",
        type=int,
        default=None,
        help=f"一次受管命令的单调墙钟预算（默认 {claude_upgrade.DEFAULT_ATTEMPT_WALL_SECONDS} 秒）。",
    )
    target.add_argument("--heartbeat-seconds", type=int, default=None, help="watchdog heartbeat 间隔（秒）。")


def capture_candidate_run_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``claude_upgrade.py capture-candidate run``（B4-01b）：返回 (摘要, 退出码)，退出码口径与 ``capture-official run`` 相同。

    ``awaiting_receipts`` 在父批次（campaign-run）里是合法暂停点记 0、直接 CLI 记诊断码 2；演练通过记 0；预约之前的拒绝以
    ``ConfigurationError`` 抛出（入口记 1）。run 不写 Store：候选三类 Store 事实都由 seal（B4-02）一次写完。
    """

    try:
        result = run_capture_attempt(arguments, "candidate")
    except (incremental_recovery.IncrementalRecoveryError, codex_upgrade_supervisor.SupervisorError) as error:
        raise ConfigurationError(f"capture-candidate run 在预约前停止：{type(error).__name__}：{error}") from error
    status = result.get("status")
    if status == "rehearsal_passed":
        return result, 0
    if status == "awaiting_receipts" and os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV) == "1":
        return result, 0
    return result, 2


def capture_official_run_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``claude_upgrade.py capture-official run``：返回 (摘要, 退出码)。

    退出码与 Codex 同一口径：父批次（campaign-run）里 ``awaiting_receipts`` 是合法暂停点记 0，直接 CLI 记诊断码 2；
    演练通过记 0，其余状态记 2；预约之前的拒绝以 ``ConfigurationError`` 抛出（入口记 1）。
    """

    try:
        result = run_capture_attempt(arguments, "official")
    except (incremental_recovery.IncrementalRecoveryError, codex_upgrade_supervisor.SupervisorError) as error:
        raise ConfigurationError(f"capture-official run 在预约前停止：{type(error).__name__}：{error}") from error
    status = result.get("status")
    if status == "rehearsal_passed":
        return result, 0
    if status == "awaiting_receipts" and os.environ.get(codex_upgrade_supervisor.CAMPAIGN_RUN_CONTEXT_ENV) == "1":
        return result, 0
    return result, 2


# ---------------------------------------------------------------------------
# 零请求演练：演练 Campaign 生成与只读复核（本模块命令行）
# ---------------------------------------------------------------------------


def build_rehearsal_campaign(arguments: argparse.Namespace) -> dict[str, Any]:
    """生成零请求演练用的 preflight_only Campaign：走 B2-05 的 plan 展开与校验，冻结场景清单副本与 plan 报告内容。

    只写新 Campaign 目录（必须不存在，父目录须是 ``<数据根>/evidence/campaigns``）；不读计时账本与环境收据、不签控制收据，
    因此它永远不能进入正式阶段（Formal 只由 VC-0 收口创建）。
    """

    campaign_dir = Path(arguments.campaign_dir)
    if not campaign_dir.is_absolute() or campaign_dir.exists() or campaign_dir.is_symlink():
        raise ConfigurationError("--campaign-dir 必须是不存在的绝对路径。")
    parent = campaign_dir.parent
    if parent.is_symlink() or not parent.is_dir() or parent.name != "campaigns" or parent.parent.name != "evidence":
        raise ConfigurationError("演练 Campaign 必须建在已存在的 <数据根>/evidence/campaigns 下（与正式 Campaign 同一坐标规则）。")
    plan_argv = [
        "plan", "--campaign-dir", str(campaign_dir), "--campaign-mode", "preflight_only", "--campaign-purpose", "validation_only",
        "--baseline-version", arguments.baseline_version, "--target-version", arguments.target_version,
        "--target-sha256", arguments.target_sha256, "--runtime-image", arguments.runtime_image, "--model", arguments.model,
        "--upstream-ip", arguments.upstream_ip, "--suite", arguments.suite,
    ]
    if arguments.campaign_id:
        plan_argv += ["--campaign-id", arguments.campaign_id]
    for flag, value in (
        ("--scenario-manifest", arguments.scenario_manifest),
        ("--claude-bin", arguments.claude_bin),
        ("--capture-root", arguments.capture_root),
        ("--capture-tool-root", arguments.capture_tool_root),
        ("--capture-container", arguments.capture_container),
        ("--synthetic-capture-container", arguments.synthetic_capture_container),
        ("--credentials-file", arguments.credentials_file),
        ("--ca-signing-pem", arguments.ca_signing_pem),
        ("--ca-cert", arguments.ca_cert),
        # 候选五个变量（B4-01b）：给出时演练 Campaign 也能做候选零请求演练（capture-candidate run --zero-request-rehearsal）。
        ("--service-container", getattr(arguments, "service_container", None)),
        ("--keeper-container", getattr(arguments, "keeper_container", None)),
        ("--postgres-container", getattr(arguments, "postgres_container", None)),
        ("--claude-account-id", getattr(arguments, "claude_account_id", None)),
        ("--api-key-id", getattr(arguments, "api_key_id", None)),
    ):
        if value:
            plan_argv += [flag, str(value)]
    plan_arguments = claude_upgrade._build_parser().parse_args(plan_argv)
    claude_upgrade_plan.prepare_plan_arguments(plan_arguments)
    rules = claude_upgrade_plan.load_baseline_rules(plan_arguments.rule_manifest, plan_arguments.baseline_version)
    claude_upgrade_plan.verify_target_rules(plan_arguments, rules)
    # 与 plan 的 _load_plan_jobs 同一展开与校验，只是不复核规格章节摘要：演练在 ARM64 数据根上执行，受管树之外的 docs/
    # 不一定部署（正式 plan 在入口预检里复核章节摘要，Campaign 冻结副本此后也不再复核）。
    payload = claude_upgrade_plan.read_scenario_manifest(
        Path(plan_arguments.scenario_manifest),
        expected_version=plan_arguments.target_version,
        rules=rules,
        verify_source_spec=False,
    )
    context = claude_upgrade._job_context(plan_arguments)
    claude_upgrade_plan.validate_campaign_context(payload, context)
    jobs = claude_upgrade_plan.expand_scenario_jobs(payload, context, job_factory=claude_upgrade.Job, suite=plan_arguments.suite)
    claude_upgrade_plan.validate_jobs(jobs, rules)
    if plan_arguments.suite == "full":
        claude_upgrade_plan.validate_full_coverage(jobs, rules)
    inputs = {"target_scenarios": claude_upgrade_plan._binding(Path(plan_arguments.scenario_manifest))}
    report = claude_upgrade_plan.plan_report(plan_arguments, jobs, rules, inputs)
    campaign_dir.mkdir(mode=0o700)
    inputs_dir = _private_directory(campaign_dir / "inputs", campaign_dir)
    scenario_copy = inputs_dir / "target-scenarios.json"
    copied = claude_upgrade._secure_copy_file_once(Path(plan_arguments.scenario_manifest), scenario_copy)
    target_sha256 = str(plan_arguments.target_sha256)
    job_rows = [
        {"id": job.job_id, "phase": job.phase, "execution_sha256": _job_execution_sha256(job), "steps": len(job.steps)}
        for job in jobs
    ]
    manifest = {
        "schema_version": claude_upgrade.CAMPAIGN_SCHEMA,
        "client": CLIENT,
        "campaign_id": plan_arguments.campaign_id,
        "created_at_utc": claude_upgrade._utc_now(),
        "campaign_mode": "preflight_only",
        "campaign_purpose": "validation_only",
        "baseline_version": plan_arguments.baseline_version,
        "target_version": plan_arguments.target_version,
        "target_sha256": target_sha256,
        "official_identity": {"binary_sha256": target_sha256, "source": "zero-request-rehearsal（未经 B2-01 冻结核对）"},
        "baseline_identity": {},
        "tool_identity": claude_upgrade._tool_identity(include_git=False),
        "control_receipts": {},
        "inputs": {"target_scenarios": {"path": scenario_copy.relative_to(campaign_dir).as_posix(), "sha256": copied["sha256"]}},
        "configuration": {"platform": claude_upgrade.CAMPAIGN_PLATFORM, **report["configuration"]},
        "suite": report["suite"],
        "required_rules": list(report["required_rules"]),
        "coverage_plan": report["coverage_plan"],
        "jobs": job_rows,
        "vc_control": {},
        "predecessor": None,
        "rehearsal_only": True,
    }
    raw = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _exclusive_write_bytes(campaign_dir / "campaign.json", raw)
    _exclusive_write_bytes(campaign_dir / "campaign.sha256", (hashlib.sha256(raw).hexdigest() + "\n").encode("ascii"))
    loaded = claude_upgrade.load_campaign_manifest(campaign_dir)
    expanded = claude_upgrade._campaign_jobs(campaign_dir, loaded, "official")
    return {
        "schema_version": "claude-upgrade-rehearsal-campaign/v1",
        "campaign_dir": str(campaign_dir),
        "campaign_id": loaded["campaign_id"],
        "campaign_mode": loaded["campaign_mode"],
        "target_version": loaded["target_version"],
        "jobs": [{"id": job.job_id, "steps": len(job.steps)} for job in expanded],
        "next_command": (
            f"claude_upgrade.py capture-official run --campaign-dir {campaign_dir} --zero-request-rehearsal --job-id <Job ID>"
        ),
        "next_candidate_command": (
            f"claude_upgrade.py capture-candidate run --campaign-dir {campaign_dir} --candidate-id <候选 ID> "
            "--zero-request-rehearsal --job-id <candidate Job ID>"
        ),
    }


def verify_rehearsal_attempt(campaign_dir: Path, attempt_id: str, candidate_id: str | None = None) -> dict[str, Any]:
    """只读复核一次零请求演练：收据身份与摘要、checkpoint 链、两级权限收口重放、探针前后一致、零请求事实。

    给出 ``candidate_id`` 时复核候选演练（B4-01b）：另核网关前后快照逐字段相等、没有写任何恢复报告。
    """

    campaign_dir = Path(campaign_dir)
    claude_upgrade._validate_existing_campaign_path(campaign_dir)
    campaign_dir = campaign_dir.resolve(strict=True)
    manifest = _campaign_for_mode(campaign_dir, MODE_REHEARSAL)
    if not SAFE_ID_RE.fullmatch(str(attempt_id)):
        raise ConfigurationError("attempt-id 非法。")
    phase = "candidate" if candidate_id is not None else "official"
    attempt_root = campaign_dir / _attempt_relative(MODE_REHEARSAL, phase, candidate_id) / "attempts" / attempt_id
    payload = claude_upgrade._read_json(attempt_root / "attempt.json", "演练 attempt 收据")
    reservation = _load_reservation(campaign_dir, attempt_root, phase=phase, candidate_id=candidate_id, manifest=manifest, mode=MODE_REHEARSAL)
    checks: list[dict[str, Any]] = []

    def check(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    try:
        _verify_attempt_document(campaign_dir, attempt_root, payload, reservation, manifest, phase=phase, mode=MODE_REHEARSAL)
        check("attempt-document", True)
    except ConfigurationError as error:
        check("attempt-document", False, str(error))
    check("status", payload.get("status") == "rehearsal_passed", str(payload.get("status")))
    layout_record = payload.get("evidence_permission_layout") or {}
    managed = Path(str(layout_record.get("managed_data_root", "")))
    logical = tuple(Path(item) for item in layout_record.get("logical_runs_roots") or [])
    try:
        codex_upgrade_evidence_permissions.replay_evidence_permission_closeout(
            attempt_root,
            [Path(item) for item in payload.get("evidence_roots") or []],
            payload.get("evidence_permission_closeout") or {},
            managed_data_root=managed,
            logical_runs_roots=logical,
        )
        check("attempt-permission-closeout-replay", True)
    except (OSError, codex_upgrade_evidence_permissions.EvidencePermissionError) as error:
        check("attempt-permission-closeout-replay", False, str(error))
    layout = _capture_layout(campaign_dir, manifest)
    declared_roots = {job.job_id: job.evidence_roots for job in claude_upgrade._campaign_jobs(campaign_dir, manifest, phase, candidate_id=candidate_id)}
    for result in payload.get("results") or []:
        job_id = str(result.get("id"))
        closure_binding = result.get("job_closure") or {}
        closure_path = attempt_root / str(closure_binding.get("path", ""))
        try:
            closure = claude_upgrade._read_json(closure_path, "Job 闭环收据")
            job_root = closure_path.parent
            internal = [job_root / "evidence", job_root / "logs"]
            external = [Path(item) for item in closure.get("host_evidence_roots") or []]
            binding = dict(closure["evidence_permission_closeout"])
            codex_upgrade_evidence_permissions.replay_evidence_permission_closeout(
                job_root,
                [*internal, *external],
                {key: binding[key] if key != "path" else Path(binding["path"]).name for key in ("path", "sha256", "bytes")},
                managed_data_root=managed,
                logical_runs_roots=logical,
            )
            runs = closure.get("environment_runs") or []
            check(f"job:{job_id}:permission-closeout-replay", True)
            check(f"job:{job_id}:environment-stable", bool(runs) and all(not run.get("drifted_kinds") for run in runs))
        except (ConfigurationError, KeyError, OSError, codex_upgrade_evidence_permissions.EvidencePermissionError) as error:
            check(f"job:{job_id}:closure", False, str(error))
        steps = result.get("steps") or []
        try:
            routed = bool(steps) and all(
                _step_route(step.get("argv") or [], layout)[1] == (step.get("host_runtime_receipt") or {}).get("container_name")
                for step in steps
            )
            check(f"job:{job_id}:container-routing", routed)
        except ConfigurationError as error:
            check(f"job:{job_id}:container-routing", False, str(error))
        check(
            f"job:{job_id}:all-steps-dry-run",
            bool(steps) and all("--dry-run" in (step.get("argv") or []) and "--execute" not in (step.get("argv") or []) for step in steps),
        )
        check(
            f"job:{job_id}:no-acknowledgement",
            all(not set(RELAY_ACKNOWLEDGE_FLAGS) & set(step.get("argv") or []) for step in steps),
        )
        relay = result.get("relay_checks") or []
        check(f"job:{job_id}:relay-plans", len(relay) == len(steps) and all(row.get("status") == "passed" for row in relay))
        # 零请求的直接证据：冻结的证据根（relay 输出根的上级）在宿主上不存在，relay 没有写过任何输出。
        roots = declared_roots.get(job_id, ())
        absent = bool(roots) and all(not _host_path(root, layout).exists() for root in roots)
        check(f"job:{job_id}:evidence-roots-absent", not result.get("unexpected_evidence_roots") and absent)
    try:
        incremental_recovery.CheckpointStore(attempt_root / "checkpoints", create=False).records()
        check("job-checkpoint-chain", True)
    except incremental_recovery.IncrementalRecoveryError as error:
        check("job-checkpoint-chain", False, str(error))
    environment = payload.get("environment") or {}
    check("attempt-environment-stable", bool(environment.get("before_probe")) and bool(environment.get("after_probe")) and not environment.get("drifted_kinds"))
    if phase == "candidate":
        gateway_environment = payload.get("gateway_environment") or {}
        check(
            "gateway-environment-stable",
            bool(gateway_environment.get("before")) and bool(gateway_environment.get("after")) and not gateway_environment.get("drifted_fields"),
        )
        check(
            "gateway-untouched",
            (payload.get("gateway_recovery") or {}).get("status") == "passed"
            and all(not result.get("restoration_checks") for result in payload.get("results") or []),
        )
    verification = payload.get("binary_verification") if isinstance(payload.get("binary_verification"), Mapping) else {}
    synthetic = verification.get("synthetic_container") if isinstance(verification.get("synthetic_container"), Mapping) else {}
    check(
        "synthetic-container-verified",
        verification.get("schema_version") == BINARY_VERIFICATION_SCHEMA
        and synthetic.get("name") == layout.synthetic_container
        and synthetic.get("network_mode") == "none"
        and (synthetic.get("client_binary") or {}).get("sha256") == manifest.get("target_sha256"),
    )
    verdict = "passed" if all(item["passed"] for item in checks) else "failed"
    return {
        "schema_version": REHEARSAL_VERDICT_SCHEMA,
        "campaign_id": manifest["campaign_id"],
        "attempt_id": attempt_id,
        "verdict": verdict,
        "live_request_count": 0,
        "checks": checks,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude_upgrade_capture.py", description="Claude 官方取证零请求演练工具（B2-06）")
    commands = parser.add_subparsers(dest="command", required=True)
    rehearsal = commands.add_parser("rehearsal-campaign", help="生成零请求演练用的 preflight_only Campaign")
    rehearsal.add_argument("--campaign-dir", type=Path, required=True)
    rehearsal.add_argument("--baseline-version", default="2.1.226")
    rehearsal.add_argument("--target-version", required=True)
    rehearsal.add_argument("--target-sha256", required=True)
    rehearsal.add_argument("--runtime-image", required=True)
    rehearsal.add_argument("--model", required=True)
    rehearsal.add_argument("--upstream-ip", required=True)
    rehearsal.add_argument("--campaign-id", default="")
    rehearsal.add_argument("--suite", choices=("core", "full"), default="full")
    for flag in (
        "--scenario-manifest", "--claude-bin", "--capture-root", "--capture-tool-root", "--capture-container",
        "--synthetic-capture-container", "--credentials-file", "--ca-signing-pem", "--ca-cert",
    ):
        rehearsal.add_argument(flag, default=None)
    for flag in ("--service-container", "--keeper-container", "--postgres-container", "--claude-account-id", "--api-key-id"):
        rehearsal.add_argument(flag, default=None, help="候选零请求演练用的候选变量（可选，B4-01b）")
    verify = commands.add_parser("verify-rehearsal", help="只读复核一次零请求演练 attempt")
    verify.add_argument("--campaign-dir", type=Path, required=True)
    verify.add_argument("--attempt-id", required=True)
    verify.add_argument("--candidate-id", default=None, help="复核候选演练 attempt 时给出候选 ID（B4-01b）")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _build_parser().parse_args(argv)
    try:
        if arguments.command == "rehearsal-campaign":
            result, code = build_rehearsal_campaign(arguments), 0
        else:
            result = verify_rehearsal_attempt(arguments.campaign_dir, arguments.attempt_id, arguments.candidate_id)
            code = 0 if result["verdict"] == "passed" else 2
    except ConfigurationError as error:
        print(f"Claude 取证演练失败：{error}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
