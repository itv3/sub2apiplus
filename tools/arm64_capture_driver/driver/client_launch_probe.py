#!/usr/bin/env python3
"""VC-0 客户端启动探测（R19）宿主侧：取证前用目标客户端按各 TUI 作业的真实参数启动一次。

为什么要有
----------
Job 演练是零请求的，只核作业定义与二进制，不在作业的真实工作目录启动 TUI。目标版本新增的
启动交互（信任目录、模型迁移、登录、hooks 审查等）会让 TUI 卡在首帧之前，而驱动的 PTY 不设
窗口尺寸、弹窗不可见，直到 VC-1 取证失败才暴露（上一轮升级的 E1：信任目录确认框）。本探测把这类问题前移到 VC-0。

做法
----
1. 从预检 Campaign 展开全部作业（与 Job 演练同一入口，只读），挑出经
   ``run_official_relay_scenario.sh`` 运行 TUI 场景的步骤，按场景调用点的展开规则算出受管驱动
   ``drive_codex_tui.py`` 的参数，去重得到启动组合；
2. 每个组合在采集容器的私有命名空间里跑一次运行器（``client_launch_probe_runner.py``，经标准输入
   送入）：只有回环网络、本地替身终结全部请求、写入全部落在覆盖层；替身收到正文含口令的首个 turn
   请求才算通过；
3. 失败的组合在全新命名空间里以 48×160 窗口复跑一次，只用于识别拦住首帧的交互屏；
4. 开跑前等上一次被中断的运行器按硬截止退出；探测前后在命名空间外对容器真实状态做指纹比对，并检查没有残留进程；
5. 报告写入 ``--output-dir``（每次运行一个 attempts/<时间> 子目录，最新结果原子替换 report.json）。

退出码：run 通过 0、探测失败 4、参数或环境错误 2；verify 通过 0、否则 1。
``--test-mutation``／``--only-scenario`` 只用于验收，带它们的报告 verify 一律拒绝。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import string
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

REPORT_SCHEMA = "arm64-client-launch-probe/v1"
REPORT_NAME = "report.json"
RUNNER_PATH = Path(__file__).resolve().with_name("client_launch_probe_runner.py")
RESULT_MARKER = "CLIENT_LAUNCH_PROBE_RESULT "
RELAY_SCRIPT_NAME = "run_official_relay_scenario.sh"
# run_official_relay_scenario.sh 顶部的默认值（静态测试逐项对照脚本）。
RELAY_DEFAULTS = {
    "CAPTURE_CONTAINER": "capture-cli",
    "CAPTURE_ROOT": "/root/oauth-capture",
    "CODEX_BIN": "/root/.local/bin/codex",
    "MODEL": "gpt-5.4",
    "DISABLE_FEATURES": "plugins apps",
}
TOOL_ROOT_SUFFIX = "tools/official_client_capture"
# 场景 → 调用点的工作目录、固定参数与参与展开的环境变量（静态测试执行脚本调用点的真实展开逐项对照）。
TUI_SCENARIOS: dict[str, dict[str, Any]] = {
    "compact-tui": {
        "marker": "__COMPACT_TUI__",
        "cwd": "/tmp/tui-probe",
        "fixed": [],
        "variables": ("CONTEXT_WINDOW", "TUI_ENABLE", "TUI_DISABLE", "DISABLE_FEATURES"),
    },
    "image-repeat-tui": {
        "marker": "__PROMPT_TUI_IMAGE__",
        "cwd": "/tmp/tui-probe",
        "fixed": [],
        "variables": ("DISABLE_FEATURES",),
    },
    "search-repeat-tui": {
        "marker": "__PROMPT_TUI_SEARCH__",
        "cwd": "/tmp/tui-probe",
        "fixed": [],
        "variables": ("DISABLE_FEATURES",),
    },
    "guardian-tui": {
        "marker": "__GUARDIAN_TUI__",
        "cwd": "/work",
        "fixed": [
            "--no-bypass", "--approval-policy", "on-request", "--sandbox-mode", "workspace-write",
            "--config", 'approvals_reviewer="auto_review"',
        ],
        "variables": ("DISABLE_FEATURES",),
    },
    "review-tui": {
        "marker": "__REVIEW_TUI__",
        "cwd": "/tmp/review-probe",
        "fixed": [],
        "variables": ("DISABLE_FEATURES",),
    },
}
STUB_HOSTS = ["chatgpt.com", "api.openai.com", "auth.openai.com", "ab.chatgpt.com"]
# 覆盖层目标：CODEX_HOME、各 TUI 工作目录所在树与系统证书目录（运行器再核对 cwd 与 CODEX_HOME 被覆盖）。
OVERLAY_TARGETS = ["/root/.codex", "/tmp", "/var/tmp", "/work", "/etc/ssl/certs"]
FINGERPRINT_FILES = ["/etc/hosts", "/etc/ssl/certs/ca-certificates.crt", "/root/.codex/config.toml", "/root/.codex/auth.json"]
PROMPT_HOLD_SECONDS = 20
# 驱动固定先读 3 秒、等启动标志最多 30 秒（0x0 窗口下标志总是认不出）、再等 2 秒才发口令。
DEADLINE_SECONDS = 90
HOST_TIMEOUT_SECONDS = DEADLINE_SECONDS + 120
# 运行器硬截止为 DEADLINE_SECONDS + 45 秒；开跑前最多等这么久让上一次被中断的运行器退出。
QUIESCENCE_TIMEOUT_SECONDS = DEADLINE_SECONDS + 90
QUIESCENCE_POLL_SECONDS = 5.0
DIAGNOSTIC_WINDOW = [48, 160]
FEATURE_RE = re.compile(r"^[A-Za-z0-9_]+$")
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@+=-]+$")
MUTATION_RE = re.compile(r"^(untrust|unack_migration):[A-Za-z0-9_./-]+$")
UNSHARE = ["unshare", "--net", "--mount", "--pid", "--fork", "--mount-proc", "--propagation", "private"]


class ProbeConfigError(ValueError):
    """作业定义或参数不满足探测前提：按错误退出，不生成通过报告。"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def shell_words(value: str, label: str) -> list[str]:
    """复刻未加引号变量展开的分词（默认 IFS）；含通配符的词拒绝，避免与 bash 路径展开不一致。"""

    words = value.split()
    for word in words:
        if any(char in word for char in "*?[]"):
            raise ProbeConfigError(f"{label} 含通配符，探测无法与 bash 展开保持一致：{word!r}")
    return words


def _features(value: str, label: str) -> list[str]:
    words = shell_words(value, label)
    for word in words:
        if not FEATURE_RE.fullmatch(word):
            raise ProbeConfigError(f"{label} 含非法 feature 名：{word!r}")
    return words


def drive_options(scenario: str, environment: Mapping[str, str]) -> list[str]:
    """按 run_official_relay_scenario.sh 对应分支的展开规则，算出影响客户端命令行的驱动参数。

    提示词、预热、斜杠命令、保持时间与日志路径只影响交互流程，不进入客户端命令行，不在此列。
    参数顺序与调用点一致（驱动按出现顺序累积 --enable／--disable）。
    """

    spec = TUI_SCENARIOS[scenario]
    options: list[str] = []
    if scenario == "compact-tui":
        # ctx_opt="--context-window $CONTEXT_WINDOW"，调用点未加引号展开
        if environment.get("CONTEXT_WINDOW"):
            options += ["--context-window", *shell_words(environment["CONTEXT_WINDOW"], "CONTEXT_WINDOW")]
        for feature in _features(environment.get("TUI_ENABLE", ""), "TUI_ENABLE"):
            options += ["--enable", feature]
        # ${TUI_DISABLE:+--disable $TUI_DISABLE}：只有第一个词前面带 --disable（与脚本一致）
        if environment.get("TUI_DISABLE"):
            options += ["--disable", *_features(environment["TUI_DISABLE"], "TUI_DISABLE")]
    options += list(spec["fixed"])
    # DISABLE_FEATURES=${DISABLE_FEATURES:-"plugins apps"}：空串与未设置同样取默认值
    for feature in _features(environment.get("DISABLE_FEATURES") or RELAY_DEFAULTS["DISABLE_FEATURES"], "DISABLE_FEATURES"):
        options += ["--disable", feature]
    return options


def _relay_step(step: Mapping[str, Any]) -> bool:
    argv = step.get("argv")
    return isinstance(argv, list) and any(
        isinstance(item, str) and PurePosixPath(item).name == RELAY_SCRIPT_NAME for item in argv
    )


def tui_combos(jobs: Iterable[Any]) -> list[dict[str, Any]]:
    """从已展开的作业里挑出 TUI 场景步骤，按客户端启动身份去重。"""

    combos: dict[str, dict[str, Any]] = {}
    for job in jobs:
        for index, step in enumerate(job.steps, 1):
            if not _relay_step(step):
                continue
            environment = step.get("environment")
            if not isinstance(environment, Mapping):
                raise ProbeConfigError(f"{job.job_id} 第 {index} 步环境非法")
            scenario = environment.get("SCENARIO") or ""
            if scenario not in TUI_SCENARIOS:
                continue
            env = {str(key): str(value) for key, value in environment.items()}
            capture_root = env.get("CAPTURE_ROOT") or RELAY_DEFAULTS["CAPTURE_ROOT"]
            identity = {
                "scenario": scenario,
                "container": env.get("CAPTURE_CONTAINER") or RELAY_DEFAULTS["CAPTURE_CONTAINER"],
                "tool_root": env.get("CAPTURE_TOOL_ROOT") or f"{capture_root}/{TOOL_ROOT_SUFFIX}",
                "codex_bin": env.get("CODEX_BIN") or RELAY_DEFAULTS["CODEX_BIN"],
                "model": env.get("MODEL") or RELAY_DEFAULTS["MODEL"],
                "cwd": TUI_SCENARIOS[scenario]["cwd"],
                "drive_options": drive_options(scenario, env),
            }
            for key in ("container", "model"):
                if not SAFE_VALUE_RE.fullmatch(identity[key]):
                    raise ProbeConfigError(f"{job.job_id} 的 {key} 含非法字符：{identity[key]!r}")
            for key in ("tool_root", "codex_bin"):
                path = identity[key]
                if not path.startswith("/") or ".." in path.split("/") or not SAFE_VALUE_RE.fullmatch(path):
                    raise ProbeConfigError(f"{job.job_id} 的 {key} 必须是绝对路径：{path!r}")
            combo_id = f"{scenario}-{_sha256_bytes(_canonical(identity))[:12]}"
            combo = combos.setdefault(combo_id, {"combo_id": combo_id, **identity, "job_ids": []})
            combo["job_ids"].append(str(job.job_id))
    return [combos[key] for key in sorted(combos)]


def load_campaign_jobs(campaign_dir: Path) -> tuple[dict[str, Any], list[Any]]:
    """与 Job 演练同一入口展开预检 Campaign 的全部作业（只读）。"""

    from tools.official_client_capture import codex_upgrade  # noqa: PLC0415 - 只在采集主机上运行时导入

    manifest = codex_upgrade.load_campaign_manifest(campaign_dir)
    jobs = [
        *codex_upgrade._campaign_jobs(campaign_dir, manifest, "official", use_approved_scenario=False),
        *codex_upgrade._campaign_jobs(campaign_dir, manifest, "candidate", use_approved_scenario=False),
    ]
    return manifest, jobs


def new_token() -> str:
    """只含大写字母：不含数字（避免误选弹窗编号选项）、不含空白与标点。"""

    return "ZQXPROBE" + "".join(secrets.choice(string.ascii_uppercase) for _ in range(20))


def runner_config(combo: Mapping[str, Any], *, token: str, test_mutations: list[str], diagnostic_window: list[int] | None) -> dict[str, Any]:
    return {
        "combo_id": combo["combo_id"],
        "tool_root": combo["tool_root"],
        "codex_bin": combo["codex_bin"],
        "model": combo["model"],
        "cwd": combo["cwd"],
        "drive_options": list(combo["drive_options"]),
        "token": token,
        "prompt_hold_seconds": PROMPT_HOLD_SECONDS,
        "deadline_seconds": DEADLINE_SECONDS,
        "overlay_targets": list(OVERLAY_TARGETS),
        "stub_hosts": list(STUB_HOSTS),
        "test_mutations": list(test_mutations),
        "diagnostic_window": diagnostic_window,
    }


def parse_runner_output(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.startswith(RESULT_MARKER)]
    if not lines:
        raise ProbeConfigError("运行器没有输出结果行")
    result = json.loads(lines[-1][len(RESULT_MARKER):])
    if not isinstance(result, dict) or result.get("status") not in {"passed", "failed"}:
        raise ProbeConfigError("运行器结果非法")
    return result


def invoke_runner(container: str, config: Mapping[str, Any], runner: bytes) -> dict[str, Any]:
    """在采集容器的私有命名空间里执行一次运行器；宿主侧超时只作兜底（运行器自带硬截止）。"""

    argv = ["docker", "exec", "-i", container, *UNSHARE, "python3", "-", json.dumps(config, ensure_ascii=False, separators=(",", ":"))]
    try:
        completed = subprocess.run(argv, input=runner, capture_output=True, timeout=HOST_TIMEOUT_SECONDS, check=False)
    except subprocess.TimeoutExpired:
        return {"status": "failed", "reason": "host_timeout"}
    stdout = completed.stdout.decode("utf-8", "replace")
    try:
        return parse_runner_output(stdout)
    except (ProbeConfigError, ValueError):
        return {
            "status": "failed",
            "reason": "runner_output_invalid",
            "exit_code": completed.returncode,
            "stderr_tail": completed.stderr.decode("utf-8", "replace")[-600:],
        }


FINGERPRINT_SCRIPT = r'''
import hashlib, json, os, sys
roots, files = json.loads(sys.argv[1])
entries = {}
for root in roots:
    if not os.path.isdir(root):
        entries[root] = "missing"
        continue
    for current, dirs, names in os.walk(root):
        dirs.sort()
        for name in sorted(dirs + names):
            path = os.path.join(current, name)
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                continue
            entries[path] = [st.st_mode, st.st_size, st.st_mtime_ns]
        if len(entries) > 500000:
            print(json.dumps({"error": "too_many_entries"}))
            sys.exit(0)
for path in files:
    try:
        entries["sha256:" + path] = hashlib.sha256(open(path, "rb").read()).hexdigest()
    except OSError:
        entries["sha256:" + path] = "unreadable"
print(json.dumps({"entries": entries}))
'''


def container_fingerprint(container: str) -> dict[str, Any]:
    """命名空间外对容器真实状态取指纹（覆盖层目标的全部条目 + 关键文件内容摘要）。"""

    argv = ["docker", "exec", "-i", container, "python3", "-", json.dumps([OVERLAY_TARGETS, FINGERPRINT_FILES])]
    completed = subprocess.run(argv, input=FINGERPRINT_SCRIPT.encode("utf-8"), capture_output=True, timeout=300, check=False)
    if completed.returncode != 0:
        raise ProbeConfigError(f"容器状态指纹失败：{completed.stderr.decode('utf-8', 'replace')[-300:]}")
    payload = json.loads(completed.stdout.decode("utf-8"))
    if "entries" not in payload:
        raise ProbeConfigError(f"容器状态指纹失败：{payload}")
    return payload["entries"]


def fingerprint_diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    changed = sorted(path for path in set(before) | set(after) if before.get(path) != after.get(path))
    return changed


def leftover_processes(container: str, tokens: list[str]) -> list[str]:
    """探测结束后容器内不得残留运行器（私有命名空间 1 号进程）或带本次口令的驱动进程。"""

    completed = subprocess.run(["docker", "exec", container, "ps", "-eo", "args="], capture_output=True, timeout=60, check=False)
    if completed.returncode != 0:
        raise ProbeConfigError("无法列出容器进程")
    prefix = " ".join(UNSHARE)
    return [
        line.strip() for line in completed.stdout.decode("utf-8", "replace").splitlines()
        if line.strip().startswith(prefix) or any(token in line for token in tokens)
    ]


def wait_for_quiescence(container: str, *, timeout_seconds: float) -> float:
    """开跑前等上一次被中断的探测运行器按自身硬截止退出；超时仍在则失败关闭。

    宿主侧进程被中断时，容器里的运行器不会随 docker exec 客户端一起退出，而是跑到自身截止
    （DEADLINE_SECONDS + 45 秒）。立即重跑若不等它，前后指纹与残留进程检查会被它干扰。
    """

    started = time.monotonic()
    while True:
        running = leftover_processes(container, [])
        if not running:
            return round(time.monotonic() - started, 3)
        if time.monotonic() - started >= timeout_seconds:
            raise ProbeConfigError(f"容器内仍有上一次的探测运行器未退出：{running[:3]}")
        time.sleep(QUIESCENCE_POLL_SECONDS)


def _summarize_run(result: Mapping[str, Any]) -> dict[str, Any]:
    keys = (
        "status", "reason", "error", "duration_seconds", "network_interfaces", "overlays", "test_mutations",
        "drive_codex_tui_sha256", "models_catalog", "diagnostic_window", "drive_exit_code", "token_request",
        "stub_requests", "detected_screens", "tui_log_bytes", "tui_visible_tail", "drive_output_tail",
        "exit_code", "stderr_tail",
    )
    return {key: result[key] for key in keys if key in result}


def seal_report(report: dict[str, Any]) -> dict[str, Any]:
    unsigned = {key: value for key, value in report.items() if key != "report_sha256"}
    return {**unsigned, "report_sha256": _sha256_bytes(_canonical(unsigned))}


def evaluate(report: Mapping[str, Any]) -> list[str]:
    """报告通过条件；返回不通过的原因（空表示通过）。"""

    problems = []
    for combo in report["combos"]:
        if combo["run"].get("status") != "passed":
            problems.append(f"{combo['combo_id']} 未通过：{combo['run'].get('reason')}")
        elif combo["run"].get("network_interfaces") != ["lo"]:
            problems.append(f"{combo['combo_id']} 未在只有回环的私有网络中运行")
    if report["fingerprint"]["changed"]:
        problems.append(f"探测前后容器状态不一致：{report['fingerprint']['changed'][:10]}")
    if report["leftover_processes"]:
        problems.append("容器内残留探测进程")
    if report["test_mutations"]:
        problems.append("带验收变更的运行不能作为放行依据")
    if report["only_scenarios"]:
        problems.append("只探测部分场景的运行不能作为放行依据")
    return problems


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, path)


def cmd_run(arguments: argparse.Namespace) -> int:
    campaign_dir = arguments.campaign_dir.resolve(strict=True)
    for mutation in arguments.test_mutation:
        if not MUTATION_RE.fullmatch(mutation):
            raise ProbeConfigError(f"不支持的验收变更：{mutation}")
    for scenario in arguments.only_scenario:
        if scenario not in TUI_SCENARIOS:
            raise ProbeConfigError(f"未知 TUI 场景：{scenario}")
    manifest, jobs = load_campaign_jobs(campaign_dir)
    combos = [
        combo for combo in tui_combos(jobs)
        if not arguments.only_scenario or combo["scenario"] in arguments.only_scenario
    ]
    containers = sorted({combo["container"] for combo in combos})
    if len(containers) > 1:
        raise ProbeConfigError(f"TUI 作业分布在多个容器：{containers}")
    runner = RUNNER_PATH.read_bytes()
    output = arguments.output_dir
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    attempt = output / "attempts" / datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%S%fz")
    attempt.mkdir(mode=0o700, parents=True)
    started = _utc_now()
    tokens: list[str] = []
    results = []
    quiescence_wait = wait_for_quiescence(containers[0], timeout_seconds=QUIESCENCE_TIMEOUT_SECONDS) if containers else 0.0
    before = container_fingerprint(containers[0]) if containers else {}
    for combo in combos:
        token = new_token()
        tokens.append(token)
        run = invoke_runner(combo["container"], runner_config(combo, token=token, test_mutations=arguments.test_mutation, diagnostic_window=None), runner)
        diagnostic = None
        if run.get("status") != "passed":
            # 判定只看真实条件那次；失败后在全新命名空间里以可读窗口复跑，只取交互屏诊断。
            diagnostic_token = new_token()
            tokens.append(diagnostic_token)
            diagnostic = _summarize_run(invoke_runner(
                combo["container"],
                runner_config(combo, token=diagnostic_token, test_mutations=arguments.test_mutation, diagnostic_window=DIAGNOSTIC_WINDOW),
                runner,
            ))
        results.append({**combo, "run": _summarize_run(run), "diagnostic": diagnostic})
        print(json.dumps({"combo_id": combo["combo_id"], "status": run.get("status"), "reason": run.get("reason"),
                          "screens": (diagnostic or {}).get("detected_screens", run.get("detected_screens"))}, ensure_ascii=False), flush=True)
    after = container_fingerprint(containers[0]) if containers else {}
    leftovers = leftover_processes(containers[0], tokens) if containers else []
    data_root = arguments.data_root
    host_drive = data_root / TOOL_ROOT_SUFFIX / "drive_codex_tui.py" if data_root else None
    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA,
        "campaign_id": manifest.get("campaign_id"),
        "campaign_dir": str(campaign_dir),
        "baseline_version": manifest.get("baseline_version"),
        "target_version": manifest.get("target_version"),
        "started_at_utc": started,
        "completed_at_utc": _utc_now(),
        "probe_tool_sha256": _sha256_bytes(Path(__file__).resolve().read_bytes()),
        "runner_sha256": _sha256_bytes(runner),
        "host_drive_codex_tui_sha256": _sha256_bytes(host_drive.read_bytes()) if host_drive and host_drive.is_file() else None,
        "tui_job_count": sum(len(combo["job_ids"]) for combo in combos),
        "quiescence_wait_seconds": quiescence_wait,
        "combos": results,
        "fingerprint": {
            "entries": len(before),
            "before_sha256": _sha256_bytes(_canonical(before)),
            "after_sha256": _sha256_bytes(_canonical(after)),
            "changed": fingerprint_diff(before, after)[:50],
        },
        "leftover_processes": leftovers[:20],
        "test_mutations": list(arguments.test_mutation),
        "only_scenarios": list(arguments.only_scenario),
    }
    # 容器内实际执行的受管驱动必须就是数据根里部署的那一份。
    drive_hashes = {combo["run"].get("drive_codex_tui_sha256") for combo in results} - {None}
    report["drive_codex_tui_mismatch"] = bool(
        report["host_drive_codex_tui_sha256"] and drive_hashes and drive_hashes != {report["host_drive_codex_tui_sha256"]}
    )
    problems = evaluate(report)
    if report["drive_codex_tui_mismatch"]:
        problems.append("容器内受管驱动与数据根部署版本不一致")
    report["problems"] = problems
    report["status"] = "passed" if not problems else "failed"
    report = seal_report(report)
    _write_json(attempt / REPORT_NAME, report)
    _write_json(output / REPORT_NAME, report)
    print(json.dumps({"status": report["status"], "report": str(output / REPORT_NAME), "combos": len(combos),
                      "tui_jobs": report["tui_job_count"], "problems": problems}, ensure_ascii=False), flush=True)
    return 0 if report["status"] == "passed" else 4


def verify_report(output: Path, campaign_dir: Path | None) -> dict[str, Any]:
    path = output / REPORT_NAME
    if not path.is_file() or path.is_symlink():
        raise ProbeConfigError(f"缺少探测报告：{path}")
    report = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(report, dict) or report.get("schema_version") != REPORT_SCHEMA:
        raise ProbeConfigError("探测报告版本非法")
    if seal_report({key: value for key, value in report.items() if key != "report_sha256"}) != report:
        raise ProbeConfigError("探测报告自摘要不一致")
    problems = evaluate(report)
    if report.get("drive_codex_tui_mismatch"):
        problems.append("容器内受管驱动与数据根部署版本不一致")
    if report.get("status") != "passed" or problems or report.get("problems"):
        raise ProbeConfigError(f"客户端启动探测未通过：{problems or report.get('problems')}")
    if campaign_dir is not None:
        from tools.official_client_capture import codex_upgrade  # noqa: PLC0415

        manifest = codex_upgrade.load_campaign_manifest(campaign_dir.resolve(strict=True))
        if manifest.get("campaign_id") != report.get("campaign_id") or manifest.get("target_version") != report.get("target_version"):
            raise ProbeConfigError("探测报告与预检 Campaign 身份不一致")
    return report


def cmd_verify(arguments: argparse.Namespace) -> int:
    try:
        report = verify_report(arguments.output_dir, arguments.campaign_dir)
    except (ProbeConfigError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "failed", "error": str(error)}, ensure_ascii=False))
        return 1
    print(json.dumps({"status": "passed", "campaign_id": report["campaign_id"], "combos": len(report["combos"]),
                      "report_sha256": report["report_sha256"]}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="对预检 Campaign 的全部 TUI 作业组合执行启动探测")
    run.add_argument("--campaign-dir", type=Path, required=True)
    run.add_argument("--output-dir", type=Path, required=True)
    run.add_argument("--data-root", type=Path, default=None, help="数据根（核对容器内受管驱动与部署版本一致）")
    run.add_argument("--test-mutation", action="append", default=[], help="仅验收：在覆盖层 config.toml 上制造缺陷")
    run.add_argument("--only-scenario", action="append", default=[], help="仅验收：只探测指定场景")
    run.set_defaults(handler=cmd_run)
    verify = commands.add_parser("verify", help="复核最新探测报告已通过")
    verify.add_argument("--output-dir", type=Path, required=True)
    verify.add_argument("--campaign-dir", type=Path, default=None)
    verify.set_defaults(handler=cmd_verify)
    arguments = parser.parse_args(argv)
    try:
        return int(arguments.handler(arguments))
    except (ProbeConfigError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
