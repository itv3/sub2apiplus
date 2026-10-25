#!/usr/bin/env python3
"""Codex 升级开工检查补的两项外部事实检查（优化第 14 项）：模型可用性探测、main 上的 Security Scan 必须先核绿。

为什么要有：一次 Codex 升级里“外部变化与过时记录在开工后才暴露”——模型被上游下线、main 上的 Security Scan 早就红着，都是开工做了几个小时之后才发现。
这两项都是几秒钟能查清的外部事实，放进入口便宜检查（``entry-preflight.sh``，建账本之前一次查全）里，开工当场拦下。第三项（atomic-double 记录
沿用时核对时效窗口）在入口判定里（``entry_steps.py``），不在这里。

* ``models``：向中转拉一次模型列表（``GET <基地址>/models``，OpenAI 风格 ``data[].id``，也认 Codex 目录风格 ``models[].slug``），要求本轮用到的每个模型
  都在列表里；可选再发一个最小请求（``--minimal``，``POST <基地址>/responses``，``max_output_tokens`` 取最小）确认模型真的能用。模型不在列表、列表拉不到、
  最小请求被拒（4xx／5xx）都算不通过——“模型已下线”当场拦下。密钥只从文件读（0600 普通文件），不进命令行、不写日志。
* ``security-scan``：main 上最近一次已完成的 Security Scan 运行（``gh run list``）必须是 success，且对应的提交就是 main 当前的头（有新提交就要先等它的扫描
  跑完），且不早于时效窗口（缺省 36 小时）；红了、还没跑完、过期、查不到都不通过——“有红先修”。

退出码：0 通过，1 不通过（原因打印到标准输出，逐条中文），3 没有配置（入口便宜检查把它也算未通过，要跳过必须显式写明，见 ``entry-preflight.sh``）。
本模块只依赖标准库；网络与 ``gh`` 调用都可注入，测试不碰真实网络。
"""

from __future__ import annotations

import argparse
import json
import os
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
EXIT_OK, EXIT_FAILED, EXIT_UNCONFIGURED = 0, 1, 3


class CheckError(RuntimeError):
    """配置缺失或不合法（退出码 3）。"""


class FetchError(RuntimeError):
    """请求没送达（连不上、超时）：属于“检查不通过”（退出码 1），不是配置问题。"""


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
    completed = subprocess.run(["gh", *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60, check=False)
    return completed.returncode, completed.stdout if completed.returncode == 0 else completed.stderr


def check_security_scan(repo: str, *, branch: str = "main", workflow: str = "security-scan.yml", max_age_hours: float = DEFAULT_SCAN_MAX_AGE_HOURS,
                        gh: Callable[[Sequence[str]], tuple[int, str]] = default_gh, now: float | None = None) -> list[str]:
    """main 头上最近一次已完成的 Security Scan 必须 success 且不过期。仓库一律显式给（本仓库的 gh 默认会查到上游，见 -R 约定）。"""

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
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
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
