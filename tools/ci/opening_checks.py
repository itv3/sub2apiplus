#!/usr/bin/env python3
"""Codex 升级开工检查补的两项外部事实检查（优化第 14 项）：模型可用性探测、main 上的 Security Scan 必须先核绿。

为什么要有：一次 Codex 升级里“外部变化与过时记录在开工后才暴露”——模型被上游下线、main 上的 Security Scan 早就红着，都是开工做了几个小时之后才发现。
这两项都是几秒钟能查清的外部事实，放进入口便宜检查（``entry-preflight.sh``，建账本之前一次查全）里，开工当场拦下。第三项（atomic-double 记录
沿用时核对时效窗口）在入口判定里（``entry_steps.py``），不在这里。

* ``models``：向中转拉一次模型列表（``GET <基地址>/models``，OpenAI 风格 ``data[].id``，也认 Codex 目录风格 ``models[].slug``），要求本轮用到的每个模型
  都在列表里；可选再发一个最小请求（``--minimal``，``POST <基地址>/responses``，``max_output_tokens`` 取最小）确认模型真的能用。模型不在列表、列表拉不到、
  最小请求被拒（4xx／5xx）都算不通过——“模型已下线”当场拦下。密钥只从文件读（0600 普通文件），不进命令行、不写日志。
* ``security-scan``：main 上最近一次已完成的 Security Scan 运行（``gh run list``）必须是 success，且对应的提交就是 main 当前的头（有新提交就要先等它的扫描
  跑完），且不早于时效窗口（缺省 36 小时）；红了、还没跑完、过期、查不到都不通过——“有红先修”。本机没装 ``gh``、``gh`` 起不来或调用超时，同样是“不通过”
  （原因写明怎么办：装好并登录 gh，或显式跳过），不抛 Python 回溯。

* ``capture-receipt-*``（Codex 升级工具优化第 4 项）：要进入入口门禁或后台验证的修复提交，必须先有“本机对这个提交完整跑过 ``make test-capture-tools`` 且通过”的本地收据
  （``local/local-capture-tools-receipt.sh`` 生成；完整提交号、退出码、开始结束时间、命令、工作树是否干净、日志摘要，自带摘要封口）。上一轮入口门禁第三轮就是修复提交只跑了
  定向测试、在注释里写了版本号，``test_driver_client_table`` 红，白跑 1.8 小时并多部署一次。起跑前核对：收据在、封口对、提交号一致、命令就是完整目标、退出码 0、工作树干净、耗时合理。
* ``tui-receipt-*``（第 5 项）：新版二进制对各 TUI 场景（compact、ws-idle-close、daemon、guardian）做过一次“零请求启动预探”，且全部通过的收据（``tui-prelaunch-receipt.sh`` 生成）。
  新版客户端行为变化（上一轮取消了 daemon 功能白名单，带 ``--disable`` 的 TUI 报 ``--no-daemon`` 退出）会在排在两小时门禁之后的客户端启动探测才暴露；预探把它前移到开工准备阶段。
  实际预探由 ARM64 上的预探流程做，本模块只定收据的形状与起跑前核对（版本等于本轮目标版本、二进制摘要等于登记值、必需场景齐全且全部通过、零请求）。
收据目录缺省 ``$RUNROOT/local-receipts``；本机收据要上传到 ARM64 的这个目录。

退出码：0 通过，1 不通过（原因打印到标准输出，逐条中文），3 没有配置（入口便宜检查把它也算未通过，要跳过必须显式写明，见 ``entry-preflight.sh``）。
本模块只依赖标准库；网络与 ``gh`` 调用都可注入，测试不碰真实网络。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

DEFAULT_SCAN_MAX_AGE_HOURS = 36.0
DEFAULT_TIMEOUT_SECONDS = 20.0
# gh 单次调用的超时秒数（查运行列表与查头提交各调一次）；放在模块级，测试可以改小。
GH_TIMEOUT_SECONDS = 60.0
EXIT_OK, EXIT_FAILED, EXIT_UNCONFIGURED = 0, 1, 3


class CheckError(RuntimeError):
    """配置缺失或不合法（退出码 3）。"""


class FetchError(RuntimeError):
    """请求没送达（连不上、超时）：属于“检查不通过”（退出码 1），不是配置问题。"""


class GhUnavailable(RuntimeError):
    """gh 没法用（没装、起不来、调用超时）：属于“检查不通过”（退出码 1），不是配置问题。消息就是写给操作员看的中文原因与处置办法。"""


Fetch = Callable[[str, str, Mapping[str, str], bytes | None, float], tuple[int, bytes]]


def urllib_fetch(method: str, url: str, headers: Mapping[str, str], body: bytes | None, timeout: float) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:   # noqa: S310 - 地址由调用方在本机配置里给出
            return response.status, response.read(4 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        return error.code, error.read(64 * 1024)
    except (urllib.error.URLError, OSError, TimeoutError) as error:
        raise FetchError(f"请求 {url} 失败：{error}") from error


def read_key(path: Path) -> str:
    """密钥文件：普通文件、不是符号链接、属主自己、权限不宽于 0600；内容去掉首尾空白后非空。"""

    try:
        info = path.lstat()
    except OSError as error:
        raise CheckError(f"读不到密钥文件 {path}：{error}") from error
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise CheckError(f"密钥文件必须是属主自己、权限 0600 的普通文件：{path}")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise CheckError(f"密钥文件是空的：{path}")
    return key


def listed_models(document: Any) -> set[str]:
    """模型列表里的全部模型名：OpenAI 风格 ``data[].id``，Codex 目录风格 ``models[].slug``（也认 ``id``／``name``）。"""

    found: set[str] = set()
    if not isinstance(document, Mapping):
        return found
    for key in ("data", "models"):
        items = document.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, Mapping):
                    found.update(str(item[field]) for field in ("id", "slug", "name") if isinstance(item.get(field), str))
                elif isinstance(item, str):
                    found.add(item)
    return found


def check_models(base_url: str, key: str, models: Sequence[str], *, minimal: bool = False, fetch: Fetch = urllib_fetch,
                 timeout: float = DEFAULT_TIMEOUT_SECONDS) -> list[str]:
    """返回不通过的原因（空列表＝通过）。"""

    wanted = [name for name in models if name]
    if not wanted:
        raise CheckError("没有给出要探测的模型")
    base = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": "sub2apiplus-opening-check/1"}
    problems: list[str] = []
    try:
        status, body = fetch("GET", base + "/models", headers, None, timeout)
    except FetchError as error:
        return [f"模型列表拉不到：{error}"]
    if status != 200:
        return [f"模型列表拉不到：GET {base}/models 返回 HTTP {status}"]
    try:
        available = listed_models(json.loads(body.decode("utf-8")))
    except ValueError:
        return [f"模型列表不是合法 JSON：GET {base}/models"]
    if not available:
        problems.append(f"模型列表是空的或格式不认识：GET {base}/models")
    for name in wanted:
        if name not in available:
            problems.append(f"模型 {name} 不在列表里（可能已下线）；列表有 {len(available)} 个模型")
    if minimal and not problems:
        for name in wanted:
            payload = json.dumps({"model": name, "input": "ping", "max_output_tokens": 16, "store": False}).encode("utf-8")
            try:
                status, body = fetch("POST", base + "/responses", {**headers, "Content-Type": "application/json"}, payload, timeout)
            except FetchError as error:
                problems.append(f"模型 {name} 的最小请求没送达：{error}")
                continue
            if not 200 <= status < 300:
                problems.append(f"模型 {name} 的最小请求被拒绝：POST {base}/responses 返回 HTTP {status}：{body[:160].decode('utf-8', 'replace')}")
    return problems


def default_gh(argv: Sequence[str]) -> tuple[int, str]:
    """调用 ``gh``，返回（退出码，成功时的标准输出／失败时的标准错误）。

    gh 没装、起不来、超时不让 Python 回溯漏出去（ARM64 没装 gh，原来日志是一屏回溯、看不懂），统一抛 ``GhUnavailable``，消息是中文原因加处置办法；
    跳过办法与 ``entry-preflight.sh`` 的规矩一致：``OPENING_CHECKS_SKIP`` 列项目名 main-security-scan，同时写 ``OPENING_CHECKS_SKIP_REASON``。"""

    skip = "确需跳过就用 OPENING_CHECKS_SKIP=main-security-scan 加 OPENING_CHECKS_SKIP_REASON 写明在别处核对过"
    try:
        completed = subprocess.run(["gh", *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=GH_TIMEOUT_SECONDS, check=False)
    except FileNotFoundError as error:
        raise GhUnavailable("本机没有 gh：Security Scan 一项无法核对，要么装好并登录 gh，要么用 OPENING_CHECKS_SKIP=main-security-scan "
                            "加 OPENING_CHECKS_SKIP_REASON 写明在别处核对过") from error
    except subprocess.TimeoutExpired as error:
        raise GhUnavailable(f"gh {' '.join(argv[:2])} 超时（{GH_TIMEOUT_SECONDS:g} 秒没有返回）：Security Scan 一项无法核对；"
                            f"先查本机到 GitHub 的网络和 gh 的登录状态再重跑，{skip}") from error
    except OSError as error:   # 例如 gh 在 PATH 里但没有执行权限、不是本机架构的可执行文件
        raise GhUnavailable(f"gh 运行不起来（{error}）：Security Scan 一项无法核对；先修好 gh（安装是否完整、有没有执行权限），{skip}") from error
    return completed.returncode, completed.stdout if completed.returncode == 0 else completed.stderr


def check_security_scan(repo: str, *, branch: str = "main", workflow: str = "security-scan.yml", max_age_hours: float = DEFAULT_SCAN_MAX_AGE_HOURS,
                        gh: Callable[[Sequence[str]], tuple[int, str]] = default_gh, now: float | None = None) -> list[str]:
    """main 头上最近一次已完成的 Security Scan 必须 success 且不过期。仓库一律显式给（本仓库的 gh 默认会查到上游，见 -R 约定）。

    返回不通过的原因（空列表＝通过）。gh 没装、起不来、超时（``GhUnavailable``）也是“没过”，原因就是它的中文消息，不抛回溯。"""

    try:
        return _security_scan_problems(repo, branch=branch, workflow=workflow, max_age_hours=max_age_hours, gh=gh, now=now)
    except GhUnavailable as error:
        return [str(error)]


def _security_scan_problems(repo: str, *, branch: str, workflow: str, max_age_hours: float, gh: Callable[[Sequence[str]], tuple[int, str]],
                            now: float | None) -> list[str]:
    now = time.time() if now is None else now
    code, output = gh(["run", "list", "-R", repo, "--workflow", workflow, "--branch", branch, "--limit", "10",
                       "--json", "status,conclusion,headSha,createdAt,databaseId,url"])
    if code != 0:
        return [f"查不到 {repo} 的 Security Scan 运行（gh 退出码 {code}）：{output.strip()[-200:]}"]
    try:
        runs = json.loads(output)
    except ValueError:
        return ["gh 返回的运行列表不是合法 JSON"]
    if not isinstance(runs, list) or not runs:
        return [f"{repo} 的 {branch} 上没有 Security Scan 运行记录"]
    code, head = gh(["api", f"repos/{repo}/commits/{branch}", "--jq", ".sha"])
    head = head.strip()
    if code != 0 or len(head) != 40:
        return [f"查不到 {repo} {branch} 的当前头提交（gh 退出码 {code}）"]
    for run in runs:
        if run.get("headSha") == head:
            if run.get("status") != "completed":
                return [f"{branch} 当前头 {head[:12]} 的 Security Scan 还没跑完（状态 {run.get('status')}）：等它跑完再开工 {run.get('url', '')}"]
            if run.get("conclusion") != "success":
                return [f"{branch} 当前头 {head[:12]} 的 Security Scan 是红的（结论 {run.get('conclusion')}）：先修好再开工 {run.get('url', '')}"]
            try:
                created = datetime.strptime(str(run.get("createdAt")), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
            except ValueError:
                return [f"Security Scan 运行时间读不出：{run.get('createdAt')!r}"]
            age = (now - created) / 3600
            if age > max_age_hours:
                return [f"{branch} 当前头 {head[:12]} 的 Security Scan 绿是 {age:.1f} 小时前的，超过时效 {max_age_hours:g} 小时：重新跑一次再开工"]
            return []
    return [f"{branch} 当前头 {head[:12]} 没有对应的 Security Scan 运行（最近的运行对应的是别的提交）：先在 main 上跑一次并核绿再开工"]


# ---------------------------------------------------------------------------
# 起跑前两张收据（Codex 升级工具优化第 4、5 项）：本机完整 make test-capture-tools 的本地收据、新版二进制 TUI 零请求启动预探收据
# ---------------------------------------------------------------------------

RECEIPT_DIR_NAME = "local-receipts"
CAPTURE_RECEIPT_SCHEMA = "local-capture-tools-receipt/v1"
CAPTURE_COMMAND = ["make", "test-capture-tools"]
# 完整跑一次本机约 48 分钟；低于这个下限一定不是完整跑过（假收据或只跑了零头）。
CAPTURE_MIN_SECONDS = 120.0
TUI_RECEIPT_SCHEMA = "tui-prelaunch-receipt/v1"
TUI_RECEIPT_NAME = "tui-prelaunch.json"
# 必需的 TUI 场景（取驱动 ``client_launch_probe.TUI_SCENARIOS`` 里的场景名，测试核对都存在）：compact、ws-idle-close、daemon、guardian，
# 以及会话内改推理强度／换模型／压缩后续聊的四个步骤场景（effort-update-ws、effort-update-http、model-switch-ws、compact-continue-ws；
# 其中 effort-update-http 的调用点带只走 HTTP 的 provider 配置序列，新版二进制若不认这些覆盖，在 2 小时门禁之后才会暴露）。
# 另外四个（image-repeat-tui、search-repeat-tui、review-tui、effort-unsupported-ws）可选，登记了就必须通过。
# 步骤场景只对目标版本支持 reasoning effort 更新的客户端有意义（驱动脚本按版本门限拒绝更旧的版本）。
REQUIRED_TUI_SCENARIOS = ("compact-tui", "ws-idle-close-tui", "daemon-tui", "guardian-tui",
                          "effort-update-ws", "effort-update-http", "model-switch-ws", "compact-continue-ws")
_COMMIT = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UTC = "%Y-%m-%dT%H:%M:%SZ"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def seal_receipt(body: Mapping[str, Any]) -> dict[str, Any]:
    """收据封口：``receipt_sha256`` 是不含它自己的规范 JSON 的 SHA-256。改任何字段都会让封口对不上。"""

    unsealed = {key: value for key, value in body.items() if key != "receipt_sha256"}
    return {**unsealed, "receipt_sha256": hashlib.sha256(_canonical(unsealed)).hexdigest()}


def receipt_sealed(receipt: Mapping[str, Any]) -> bool:
    return isinstance(receipt.get("receipt_sha256"), str) and seal_receipt(receipt)["receipt_sha256"] == receipt["receipt_sha256"]


def _write_private(path: Path, value: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(_canonical(value) + b"\n")
    os.replace(temporary, path)
    return path


def _load_receipt(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """读收据：（内容，问题）。符号链接、读不出、不是 JSON 对象都算问题。"""

    try:
        if path.is_symlink():
            return None, f"收据 {path} 是符号链接，拒绝"
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, ValueError) as error:
        return None, f"收据 {path} 读不出或不是合法 JSON：{error}"
    if not isinstance(value, dict):
        return None, f"收据 {path} 不是 JSON 对象"
    return value, None


def _parse_utc(value: Any) -> float | None:
    try:
        return datetime.strptime(str(value), _UTC).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def summarize_log(text: str) -> str:
    """收据里留的日志摘要：先是前 6 行 ``FAIL:``／``ERROR:``（红在哪个用例），再是最后 3 行（汇总行）；被拦下时一眼看出红在哪。"""

    lines = text.splitlines()
    failures = [line for line in lines if line.startswith(("FAIL:", "ERROR:"))][:6]
    tail = [line for line in lines[-3:] if line not in failures]
    return "\n".join([*failures, *tail])


def capture_receipt_path(directory: Path, commit: str) -> Path:
    return Path(directory) / f"capture-tools-{commit}.json"


def write_capture_receipt(directory: Path, *, commit: str, exit_code: int, started_at_utc: str, finished_at_utc: str, command: Sequence[str] = CAPTURE_COMMAND,
                          host: str = "", tree_clean: bool = True, log_sha256: str = "", log_tail: str = "") -> Path:
    """写“本机完整 make test-capture-tools”本地收据。失败（退出码非 0）的运行也写，如实记录红：被拦下时能看到是哪个提交红了。"""

    if not _COMMIT.fullmatch(commit):
        raise CheckError("收据需要完整的 40 位小写提交号")
    started, finished = _parse_utc(started_at_utc), _parse_utc(finished_at_utc)
    if started is None or finished is None or finished < started:
        raise CheckError("开始／结束时间不合法（要 UTC 的 YYYY-MM-DDTHH:MM:SSZ，结束不早于开始）")
    body = {"schema_version": CAPTURE_RECEIPT_SCHEMA, "commit": commit, "command": list(command), "exit_code": int(exit_code),
            "started_at_utc": started_at_utc, "finished_at_utc": finished_at_utc, "duration_seconds": int(finished - started),
            "host": host, "tree_clean": bool(tree_clean), "log_sha256": log_sha256, "log_tail": log_tail[-1200:]}
    return _write_private(capture_receipt_path(directory, commit), seal_receipt(body))


def check_capture_receipt(directory: Path, commit: str, *, min_seconds: float = CAPTURE_MIN_SECONDS) -> list[str]:
    """起跑前核对本机完整测试收据；返回不通过的原因（空列表＝通过）。每条原因都写明怎么处理。"""

    path = capture_receipt_path(directory, commit)
    how = ("先在本机对这个提交跑 bash tools/arm64_capture_driver/driver/local/local-capture-tools-receipt.sh"
           "（完整 make test-capture-tools，本机约 48 分钟、不占门禁），把收据上传到 " + str(Path(directory)) + " 后再起跑")
    if not _COMMIT.fullmatch(str(commit or "")):
        return [f"起跑提交号不是完整的 40 位小写提交（{commit!r}），无法核对本地收据"]
    receipt, problem = _load_receipt(path)
    if problem:
        return [problem]
    if receipt is None:
        return [f"没有提交 {commit[:12]} 的本机完整 make test-capture-tools 本地收据（{path}）：{how}。"
                "上一轮就是修复提交只跑了定向测试、注释里写了版本号致 test_driver_client_table 红，入口门禁白跑 1.8 小时并多部署一次"]
    problems = []
    if receipt.get("schema_version") != CAPTURE_RECEIPT_SCHEMA:
        problems.append(f"收据合同不是 {CAPTURE_RECEIPT_SCHEMA}（{receipt.get('schema_version')!r}）")
    if not receipt_sealed(receipt):
        problems.append("收据封口（receipt_sha256）对不上：内容被改过或不是本工具生成的")
    if receipt.get("commit") != commit:
        problems.append(f"收据记录的提交是 {str(receipt.get('commit'))[:12]}，不是要起跑的 {commit[:12]}：部署起跑的提交必须就是本机跑过完整测试的那一个，{how}")
    if receipt.get("command") != CAPTURE_COMMAND:
        problems.append(f"收据记录的命令是 {receipt.get('command')!r}，不是完整目标 {CAPTURE_COMMAND!r}（定向测试不算）")
    code = receipt.get("exit_code")
    if type(code) is not int or code != 0:
        tail = str(receipt.get("log_tail") or "").strip().splitlines()[-9:]
        problems.append(f"本机 make test-capture-tools 在提交 {commit[:12]} 上没有通过（退出码 {code!r}）：先修好并对修复后的提交重跑完整测试，不要带着红的提交起跑"
                        + (f"；日志末尾：{' | '.join(tail)}" if tail else ""))
    if receipt.get("tree_clean") is not True:
        problems.append("收据记录运行时工作树不干净（有未提交改动或未跟踪文件）：测试跑的不是这个提交本身")
    started, finished = _parse_utc(receipt.get("started_at_utc")), _parse_utc(receipt.get("finished_at_utc"))
    if started is None or finished is None or finished < started:
        problems.append("收据的开始／结束时间不合法")
    elif finished - started < min_seconds:
        problems.append(f"收据记录的耗时只有 {int(finished - started)} 秒，低于 {min_seconds:g} 秒：完整 make test-capture-tools 本机约 48 分钟，这不像完整跑过一次")
    return problems


def tui_receipt_path(directory: Path) -> Path:
    return Path(directory) / TUI_RECEIPT_NAME


def parse_scenario_spec(spec: str) -> dict[str, Any]:
    """命令行的场景结果 ``名称=状态[:退出码[:尾部文字]]``，状态只有 passed／failed。"""

    name, separator, rest = spec.partition("=")
    status, _, extra = rest.partition(":")
    if not separator or not name or status not in ("passed", "failed"):
        raise CheckError(f"场景结果写法不对（要 名称=passed 或 名称=failed[:退出码[:尾部文字]]）：{spec!r}")
    code_text, _, tail = extra.partition(":")
    row: dict[str, Any] = {"name": name, "status": status}
    if code_text:
        if not code_text.lstrip("-").isdigit():
            raise CheckError(f"场景 {name} 的退出码不是整数：{code_text!r}")
        row["exit_code"] = int(code_text)
    if tail:
        row["tail"] = tail[-600:]
    return row


def write_tui_receipt(directory: Path, *, client_version: str, binary_path: str, binary_sha256: str, scenarios: Sequence[Mapping[str, Any]],
                      started_at_utc: str, finished_at_utc: str, host: str = "", live_request_count: int = 0, note: str = "") -> Path:
    """写“新版二进制 TUI 零请求启动预探”收据。失败的场景也如实记录（带退出码与屏幕尾部），被拦下时能看到是哪个场景怎么退出的。"""

    if not _SHA256.fullmatch(binary_sha256):
        raise CheckError("二进制摘要要 64 位小写十六进制")
    if _parse_utc(started_at_utc) is None or _parse_utc(finished_at_utc) is None:
        raise CheckError("开始／结束时间不合法（要 UTC 的 YYYY-MM-DDTHH:MM:SSZ）")
    names = [str(row.get("name")) for row in scenarios]
    if len(set(names)) != len(names):
        raise CheckError("场景名重复")
    body = {"schema_version": TUI_RECEIPT_SCHEMA, "client_version": client_version, "binary_path": binary_path, "binary_sha256": binary_sha256,
            "scenarios": [dict(row) for row in scenarios], "started_at_utc": started_at_utc, "finished_at_utc": finished_at_utc,
            "host": host, "live_request_count": int(live_request_count), "note": note}
    return _write_private(tui_receipt_path(directory), seal_receipt(body))


def check_tui_receipt(directory: Path, *, target_version: str | None = None, binary_sha256: str | None = None) -> list[str]:
    """起跑前核对 TUI 零请求启动预探收据；返回不通过的原因（空列表＝通过）。

    ``target_version``／``binary_sha256`` 给了就核对收据对的是不是本轮目标版本与登记的二进制（后台验证起跑拿不到时不核对，入口便宜检查两个都给）。"""

    path = tui_receipt_path(directory)
    how = ("在 ARM64 上用新版二进制对各 TUI 场景做一次零请求启动预探，再用 bash tui-prelaunch-receipt.sh write 生成收据（必需场景："
           + "、".join(REQUIRED_TUI_SCENARIOS) + "），收据放在 " + str(Path(directory)))
    receipt, problem = _load_receipt(path)
    if problem:
        return [problem]
    if receipt is None:
        return [f"没有新版二进制 TUI 零请求启动预探收据（{path}）：{how}。"
                "上一轮取消 daemon 功能白名单，带 --disable 的 TUI 报 --no-daemon 退出，在两小时门禁之后的客户端启动探测才暴露"]
    problems = []
    if receipt.get("schema_version") != TUI_RECEIPT_SCHEMA:
        problems.append(f"收据合同不是 {TUI_RECEIPT_SCHEMA}（{receipt.get('schema_version')!r}）")
    if not receipt_sealed(receipt):
        problems.append("预探收据封口（receipt_sha256）对不上：内容被改过或不是本工具生成的")
    if target_version is not None and receipt.get("client_version") != target_version:
        problems.append(f"预探收据对的客户端版本是 {receipt.get('client_version')!r}，不是本轮目标版本 {target_version!r}：{how}")
    if binary_sha256 is not None and receipt.get("binary_sha256") != binary_sha256:
        problems.append("预探收据的二进制摘要与本轮登记的客户端二进制摘要不一致：预探的不是这一份二进制")
    if receipt.get("live_request_count") != 0 or type(receipt.get("live_request_count")) is not int:
        problems.append(f"预探收据的真实请求数是 {receipt.get('live_request_count')!r}，不是 0：零请求预探不得发出任何真实请求")
    rows = receipt.get("scenarios")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return [*problems, "预探收据的 scenarios 不是场景结果列表"]
    by_name = {str(row.get("name")): row for row in rows}
    missing = [name for name in REQUIRED_TUI_SCENARIOS if name not in by_name]
    if missing:
        problems.append(f"预探收据缺少必需场景：{'、'.join(missing)}")
    for name, row in by_name.items():
        if row.get("status") != "passed":
            tail = str(row.get("tail") or "").strip().replace("\n", " ")[:300]
            problems.append(f"场景 {name} 预探没通过（状态 {row.get('status')!r}，退出码 {row.get('exit_code')!r}）"
                            + (f"：{tail}" if tail else "") + "。新版客户端的 TUI 启动行为变了，先在登记变更集里修驱动再重新预探")
    return problems


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    models = sub.add_parser("models", help="模型可用性探测")
    models.add_argument("--base-url", default=os.environ.get("MODEL_PROBE_URL", ""), help="中转基地址（含 /v1），缺省取环境变量 MODEL_PROBE_URL")
    models.add_argument("--key-file", type=Path, default=Path(os.environ["MODEL_PROBE_KEY_FILE"]) if os.environ.get("MODEL_PROBE_KEY_FILE") else None,
                        help="只含密钥的 0600 文件，缺省取环境变量 MODEL_PROBE_KEY_FILE")
    models.add_argument("--models", default="", help="要探测的模型，逗号分隔")
    models.add_argument("--minimal", action="store_true", help="另发最小请求确认真能用")
    scan = sub.add_parser("security-scan", help="main 上的 Security Scan 先核绿")
    scan.add_argument("--repo", default=os.environ.get("SECURITY_SCAN_REPO", ""), help="owner/name，缺省取环境变量 SECURITY_SCAN_REPO")
    scan.add_argument("--branch", default="main")
    scan.add_argument("--workflow", default="security-scan.yml")
    scan.add_argument("--max-age-hours", type=float, default=DEFAULT_SCAN_MAX_AGE_HOURS)
    # ---- 起跑前两张收据（优化第 4、5 项） ----
    write = sub.add_parser("capture-receipt-write", help="写本机完整 make test-capture-tools 本地收据（由 local-capture-tools-receipt.sh 调用）")
    write.add_argument("--dir", type=Path, required=True)
    write.add_argument("--commit", required=True)
    write.add_argument("--exit-code", type=int, required=True)
    write.add_argument("--started", required=True)
    write.add_argument("--finished", required=True)
    write.add_argument("--host", default="")
    write.add_argument("--tree-clean", choices=("true", "false"), required=True)
    write.add_argument("--log", type=Path, default=None, help="完整日志文件（记摘要与末尾几行）")
    check = sub.add_parser("capture-receipt-check", help="起跑前核对本地收据")
    check.add_argument("--dir", type=Path, required=True)
    check.add_argument("--commit", required=True)
    check.add_argument("--min-seconds", type=float, default=CAPTURE_MIN_SECONDS)
    tui_write = sub.add_parser("tui-receipt-write", help="写 TUI 零请求启动预探收据（由 tui-prelaunch-receipt.sh 调用）")
    tui_write.add_argument("--dir", type=Path, required=True)
    tui_write.add_argument("--version", required=True)
    tui_write.add_argument("--binary", required=True)
    tui_write.add_argument("--binary-sha256", required=True)
    tui_write.add_argument("--scenario", action="append", default=[], help="名称=passed 或 名称=failed[:退出码[:尾部文字]]，可重复")
    tui_write.add_argument("--started", required=True)
    tui_write.add_argument("--finished", required=True)
    tui_write.add_argument("--host", default="")
    tui_write.add_argument("--live-requests", type=int, default=0)
    tui_write.add_argument("--note", default="")
    tui_check = sub.add_parser("tui-receipt-check", help="起跑前核对 TUI 预探收据")
    tui_check.add_argument("--dir", type=Path, required=True)
    tui_check.add_argument("--version", default=None)
    tui_check.add_argument("--binary-sha256", default=None)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "capture-receipt-write":
            log = args.log.read_bytes() if args.log is not None else b""
            path = write_capture_receipt(args.dir, commit=args.commit, exit_code=args.exit_code, started_at_utc=args.started, finished_at_utc=args.finished,
                                         host=args.host, tree_clean=args.tree_clean == "true", log_sha256=hashlib.sha256(log).hexdigest() if args.log else "",
                                         log_tail=summarize_log(log.decode("utf-8", "replace")))
            print(f"本地收据已写：{path}")
            return EXIT_OK
        if args.command == "tui-receipt-write":
            path = write_tui_receipt(args.dir, client_version=args.version, binary_path=args.binary, binary_sha256=args.binary_sha256,
                                     scenarios=[parse_scenario_spec(item) for item in args.scenario], started_at_utc=args.started,
                                     finished_at_utc=args.finished, host=args.host, live_request_count=args.live_requests, note=args.note)
            print(f"预探收据已写：{path}")
            return EXIT_OK
        if args.command in ("capture-receipt-check", "tui-receipt-check"):
            if args.command == "capture-receipt-check":
                problems = check_capture_receipt(args.dir, args.commit, min_seconds=args.min_seconds)
            else:
                problems = check_tui_receipt(args.dir, target_version=args.version, binary_sha256=args.binary_sha256)
            for problem in problems:
                print(problem)
            if not problems:
                print("通过")
            return EXIT_FAILED if problems else EXIT_OK
        if args.command == "models":
            if not args.base_url or args.key_file is None or not args.models:
                raise CheckError("模型探测没有配置：需要中转基地址（MODEL_PROBE_URL）、密钥文件（MODEL_PROBE_KEY_FILE）和模型名")
            problems = check_models(args.base_url, read_key(args.key_file), [name.strip() for name in args.models.split(",")], minimal=args.minimal)
        else:
            if not args.repo:
                raise CheckError("Security Scan 核对没有配置：需要仓库 owner/name（SECURITY_SCAN_REPO）")
            problems = check_security_scan(args.repo, branch=args.branch, workflow=args.workflow, max_age_hours=args.max_age_hours)
    except CheckError as error:
        print(f"未配置或不可用：{error}")
        return EXIT_UNCONFIGURED
    for problem in problems:
        print(problem)
    if not problems:
        print("通过")
    return EXIT_FAILED if problems else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
