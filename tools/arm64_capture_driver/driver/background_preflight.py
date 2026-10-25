#!/usr/bin/env python3
"""后台验证起跑前置检查（Codex 升级工具优化第 4 项）：起跑前逐项核对冻结收据、基线重封、重部署、准入绑定，不通过就拒绝起跑。

为什么要有：一次 Codex 升级里后台验证白跑过两次，原因都是“起跑顺序错”——先于冻结承接收据起跑（冻结测试在两个多小时后才红），先于重部署起跑
（部署换了，结论作废）。后台验证一轮约 136 分钟，顺序错的代价是整轮；这些前提全都能在几秒到一两分钟内查清，所以起跑前先查，不通过就不起。

四项检查（互相独立，一项不过其余照查，最后一起汇总；任一项不过即拒绝起跑）：

* ``freeze-receipts`` 冻结收据：被验证提交的树里，每个登记在冻结台账里的路径，当前内容的摘要都得是台账登记过的摘要（``tools.upstream_merge.freeze``
  的 ``load_frozen_edges``，与门禁里 Go／Python 冻结测试同一张承接图）。源码改了而承接收据还没单独提交，就是未承接——冻结测试必红。
* ``baseline-reseal`` 基线重封：受管工具树的身份摘要必须和测试树里固化的守恒基线一致（Codex 跑身份守恒测试，Claude 跑工具身份策略测试，与门禁里同一份测试）。
  改了受管工具文件而没有重封基线，守恒测试必红。
* ``redeploy`` 重部署：数据根最新的受管工具部署收据登记的整树摘要，必须等于被验证提交的测试树按该客户端策略现算的整树摘要，部署一致性各部分（受管整树含测试、
  指南、部署脚本副本）逐项相同；已纳管的每个客户端都核对。没重新部署，后台验证的结论绑在旧部署上，部署一换就作废。
* ``admission-binding`` 准入绑定：这次起跑产生的结论，必须是 VC-5 准入（``vc5_admission`` → ``background_validation.check``）按“当前部署”能查到的那一份：
  (1) 部署收据此刻仍是最新的一份，没有在检查期间换掉；(2) 已纳管客户端都在要绑定的部署集合里；(3) 结论按这套部署算出的绑定键与准入查找所用的键一致；
  (4) 已安装的驱动（有 ``install.py`` 的安装布局）复验通过——它的安装收据绑定的部署收据仍是最新，否则 guard 会在派发前停线。

树的来源：``--tree`` 直接给一棵检出；或 ``--bundle/--branch/--commit``，由本模块从 bundle 克隆并检出到临时目录（用完即删）。
子命令 ``check``：打印 JSON 报告（``background-preflight/v1``），退出码 0 全部通过、3 有检查没过、2 用法或环境错误。``background_validation.py start --preflight on``
在起跑前调用同一套检查。
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "background-preflight/v1"
HERE = Path(__file__).resolve().parent
CHECK_NAMES = ("freeze-receipts", "baseline-reseal", "redeploy", "admission-binding")
# 各客户端的基线重封检查：与门禁里同一份测试（改受管工具而没重封基线，这份测试必红）。
BASELINE_TESTS = {
    "codex": "tools.official_client_capture.tests.test_codex_identity_conservation",
    "claude": "tools.official_client_capture.tests.test_claude_tool_identity_policy",
}
BASELINE_TEST_TIMEOUT_SECONDS = 900
GIT_TIMEOUT_SECONDS = 900
UNCOVERED_LIMIT = 20

# 冻结覆盖检查在被验证的树里运行（用树自己的 tools.upstream_merge）：与 load_frozen_edges 的算法逐字一致，只多一步“当前摘要 ∉ 已登记摘要”。
_FREEZE_SNIPPET = """\
import hashlib, json, sys
from pathlib import Path
sys.dont_write_bytecode = True
from tools.upstream_merge.freeze import load_frozen_edges
root = Path('.').resolve()
_edges, known, _receipts = load_frozen_edges(root)
uncovered = []
for path, digests in sorted(known.items()):
    item = root / path
    if item.is_symlink() or not item.is_file():
        continue
    if hashlib.sha256(item.read_bytes()).hexdigest() not in digests:
        uncovered.append(path)
print(json.dumps({'registered': len(known), 'uncovered': uncovered}))
"""


class PreflightError(RuntimeError):
    """用法或环境错误（不是检查没通过）。"""


def _sibling(name: str) -> Any:
    key = f"{name}_preflight_sibling"
    if key not in sys.modules:
        spec = importlib.util.spec_from_file_location(key, HERE / f"{name}.py")
        if spec is None or spec.loader is None:
            raise PreflightError(f"找不到同目录的 {name}.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    return sys.modules[key]


def _result(name: str, problems: list[str], **detail: Any) -> dict[str, Any]:
    return {"name": name, "passed": not problems, "problems": problems, "detail": detail}


def _run(argv: Sequence[str], *, cwd: Path, timeout: float, env: Mapping[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": ".", **(env or {})}
    try:
        return subprocess.run(list(argv), cwd=str(cwd), env=environment, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired as error:
        return subprocess.CompletedProcess(list(argv), 124, "", f"超时（{timeout:.0f} 秒）：{error}")


# ---------------------------------------------------------------------------
# 四项检查
# ---------------------------------------------------------------------------


def check_freeze_receipts(tree: Path, *, runner: Callable[..., Any] = _run) -> dict[str, Any]:
    completed = runner([sys.executable, "-B", "-c", _FREEZE_SNIPPET], cwd=tree, timeout=300)
    try:
        lines = [line for line in completed.stdout.splitlines() if line.strip().startswith("{")]
        report = json.loads(lines[-1])
        uncovered = list(report["uncovered"])
    except (ValueError, IndexError, KeyError, TypeError):
        return _result("freeze-receipts", [f"冻结覆盖算不出来（退出码 {completed.returncode}）：{(completed.stderr or completed.stdout)[-400:].strip()}"])
    if completed.returncode != 0:
        return _result("freeze-receipts", [f"冻结覆盖检查退出码 {completed.returncode}：{completed.stderr[-400:].strip()}"])
    problems = []
    if uncovered:
        shown = "、".join(uncovered[:UNCOVERED_LIMIT]) + (f" 等共 {len(uncovered)} 个" if len(uncovered) > UNCOVERED_LIMIT else "")
        problems.append(f"{len(uncovered)} 个冻结路径的当前内容没有承接收据登记：{shown}。先按 freeze-successor-generate 生成承接收据并"
                        "【另行提交】（源码与收据不能同一提交），再对收据提交起后台验证——否则冻结测试两个多小时后才红")
    return _result("freeze-receipts", problems, registered=report.get("registered"), uncovered=uncovered)


def check_baseline_reseal(tree: Path, client: str, *, runner: Callable[..., Any] = _run) -> dict[str, Any]:
    module = BASELINE_TESTS.get(client)
    if module is None:
        return _result("baseline-reseal", [f"客户端 {client!r} 没有登记基线重封检查（fail-closed）"])
    completed = runner([sys.executable, "-B", "-m", "unittest", module], cwd=tree, timeout=BASELINE_TEST_TIMEOUT_SECONDS)
    problems = []
    if completed.returncode != 0:
        tail = "\n".join((completed.stderr or completed.stdout).strip().splitlines()[-6:])
        problems.append(f"受管工具身份与固化的守恒基线不一致（{module} 退出码 {completed.returncode}）。改了受管工具文件要【单独提交】重封基线后再起后台验证：{tail[-500:]}")
    return _result("baseline-reseal", problems, test=module, returncode=completed.returncode)


def check_redeploy(tree: Path, data_root: Path, clients: Sequence[str], *, steps: Any = None, pycache: Path | None = None) -> dict[str, Any]:
    steps = steps or _sibling("entry_steps")
    control = Path(data_root) / "control"
    problems: list[str] = []
    checked: dict[str, str] = {}
    for client in clients:
        try:
            path, payload = steps.latest_deploy_receipt(control, client)
            key = steps.CT.spec(client)["deploy_receipt_identity_key"]
            identity = payload if key is None else payload.get(key)
            recorded = identity.get("tool_files_sha256") if isinstance(identity, Mapping) else None
            actual = steps.tree_tool_files_sha256(Path(tree), client, pycache=pycache)
        except steps.EntryStepsError as error:
            problems.append(f"{client}：{error}")
            continue
        checked[client] = path.name
        if recorded != actual:
            problems.append(f"{client}：数据根部署的受管树（{str(recorded)[:12]}）不是本提交（测试树受管树 {actual[:12]}）：先受监督部署本提交，再起后台验证（部署收据 {path.name}）")
        mismatch = steps.deploy_consistency(Path(tree), Path(data_root), client)
        if mismatch:
            problems.append(f"{client}：数据根与本提交的部署一致性不符：{' '.join(mismatch)}（整树摘要相同也不行）：先受监督部署本提交并同步部署脚本副本")
    return _result("redeploy", problems, receipts=checked)


def check_admission_binding(runroot: Path, data_root: Path, commit: str, client: str, deployment: Mapping[str, Any],
                            deployments: Mapping[str, Mapping[str, Any]] | None, *, background: Any = None) -> dict[str, Any]:
    background = background or _sibling("background_validation")
    problems: list[str] = []
    try:
        latest = background.latest_deployment(Path(data_root), client)
    except background.ValidationError as error:
        return _result("admission-binding", [f"取不到当前部署收据：{error}"])
    if latest != dict(deployment):
        problems.append("检查期间部署收据换了（准入按最新一份查结论，现在要绑定的这份已经不是最新）：等部署稳定后再起")
    managed = background.managed_clients(Path(data_root))
    if deployments is not None:
        missing = [name for name in managed if name not in deployments]
        if missing:
            problems.append(f"要绑定的部署收据集合缺少已纳管客户端 {missing}（准入按集合里该客户端的收据查结论）")
        if deployments.get(client) != dict(deployment):
            problems.append(f"部署收据集合里的 {client} 与本轮部署收据不是同一份")
    elif len(managed) > 1:
        problems.append(f"数据根纳管了多个客户端 {managed}，起跑却没有给出部署收据集合")
    # 准入查找结论用 for_deployment(runroot, 当前部署, client)：按这套部署算出的结论文件必须能被它认出。
    path = background.result_path(Path(runroot), commit, deployment, deployments)
    simulated = {"deployment": dict(deployment), **({"deployments": dict(deployments)} if deployments else {})}
    if not background._binds(simulated, latest, client):
        problems.append("按当前部署收据算出的结论绑定，准入的查找认不出来（绑定键不一致）")
    install = HERE.parent / "install.py"
    if install.is_file() and (HERE.parent / "manifest.json").is_file():
        completed = _run([sys.executable, "-B", str(install), "verify", "--target", str(HERE.parent), "--data-root", str(data_root),
                          "--client", client], cwd=HERE.parent, timeout=300, env={"PYTHONPATH": ""})
        if completed.returncode != 0:
            problems.append("已安装驱动复验不通过（安装收据绑定的部署收据不是最新，或驱动文件被改）："
                            + (completed.stderr or completed.stdout).strip()[-300:] + "。先重装驱动再起后台验证")
    return _result("admission-binding", problems, result=str(path), clients=managed)


# ---------------------------------------------------------------------------
# 编排
# ---------------------------------------------------------------------------


def materialize_tree(bundle: Path, branch: str, commit: str, destination: Path) -> Path:
    """从 bundle 检出被验证的提交（只读用的临时树）。bundle、分支、提交任一对不上都报环境错误。"""

    def git(*args: str, cwd: Path | None = None) -> str:
        completed = subprocess.run(["git", "-c", "core.fsmonitor=false", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
                                   stdin=subprocess.DEVNULL, timeout=GIT_TIMEOUT_SECONDS, check=False,
                                   env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"})
        if completed.returncode != 0:
            raise PreflightError(f"git {' '.join(args[:2])} 失败：{completed.stderr.strip()[-300:]}")
        return completed.stdout

    git("clone", "--quiet", "--no-checkout", "--branch", branch, str(bundle), str(destination))
    git("checkout", "--quiet", "--detach", commit, cwd=destination)
    head = git("rev-parse", "HEAD", cwd=destination).strip()
    if head != commit:
        raise PreflightError(f"检出的提交 {head} 不是要验证的 {commit}")
    return destination


def run_checks(*, tree: Path, commit: str, data_root: Path, runroot: Path, client: str, deployment: Mapping[str, Any],
               deployments: Mapping[str, Mapping[str, Any]] | None, only: Sequence[str] | None = None,
               runner: Callable[..., Any] = _run, steps: Any = None, background: Any = None) -> dict[str, Any]:
    """四项检查全部执行，汇总成一份报告；``only`` 只跑指定项（诊断用）。"""

    wanted = tuple(only) if only else CHECK_NAMES
    unknown = [name for name in wanted if name not in CHECK_NAMES]
    if unknown:
        raise PreflightError(f"不认识的检查项：{unknown}（可选 {list(CHECK_NAMES)}）")
    clients = sorted(deployments) if deployments else [client]
    started = time.time()
    results: list[dict[str, Any]] = []
    for name in wanted:
        began = time.monotonic()
        if name == "freeze-receipts":
            result = check_freeze_receipts(tree, runner=runner)
        elif name == "baseline-reseal":
            result = check_baseline_reseal(tree, client, runner=runner)
        elif name == "redeploy":
            result = check_redeploy(tree, data_root, clients, steps=steps)
        else:
            result = check_admission_binding(runroot, data_root, commit, client, deployment, deployments, background=background)
        result["seconds"] = round(time.monotonic() - began, 3)
        results.append(result)
    failed = [item["name"] for item in results if not item["passed"]]
    return {"schema_version": SCHEMA, "commit": commit, "client": client, "checked_at_unix": int(started), "passed": not failed,
            "failed_checks": failed, "checks": results}


def format_failures(report: Mapping[str, Any]) -> str:
    lines = [f"起跑前置检查不通过（{'、'.join(report['failed_checks'])}），拒绝起跑后台验证："]
    for item in report["checks"]:
        if not item["passed"]:
            lines.extend(f"  [{item['name']}] {problem}" for problem in item["problems"])
    return "\n".join(lines)


def preflight_with_bundle(*, bundle: Path, branch: str, commit: str, data_root: Path, runroot: Path, client: str, deployment: Mapping[str, Any],
                          deployments: Mapping[str, Mapping[str, Any]] | None, scratch_parent: Path, only: Sequence[str] | None = None) -> dict[str, Any]:
    """从 bundle 检出临时树、跑检查、删掉临时树。临时树放在 ``scratch_parent`` 下（不碰后台工作目录，免得影响其来源快照绑定）。"""

    scratch_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = Path(tempfile.mkdtemp(prefix=f"preflight-{commit[:12]}-", dir=scratch_parent))
    try:
        tree = materialize_tree(bundle, branch, commit, destination / "tree")
        return run_checks(tree=tree, commit=commit, data_root=data_root, runroot=runroot, client=client, deployment=deployment,
                          deployments=deployments, only=only)
    finally:
        shutil.rmtree(destination, ignore_errors=True)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="跑四项起跑前置检查，打印 JSON 报告")
    check.add_argument("--runroot", type=Path, required=True)
    check.add_argument("--data-root", type=Path, required=True)
    check.add_argument("--commit", required=True)
    check.add_argument("--client", default="codex")
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--tree", type=Path, help="被验证提交的检出（已有）")
    source.add_argument("--bundle", type=Path, help="从 bundle 检出（与 --branch 同用）")
    check.add_argument("--branch", default=None)
    check.add_argument("--only", default=None, help="只跑指定检查项，逗号分隔")
    check.add_argument("--scratch", type=Path, default=None, help="临时树的父目录（缺省 <runroot>/background-validation/preflight）")
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        background = _sibling("background_validation")
        deployment = background.latest_deployment(args.data_root, args.client)
        deployments = background.deployment_set(args.data_root)
        only = [item for item in (args.only or "").split(",") if item] or None
        if args.tree is not None:
            report = run_checks(tree=args.tree, commit=args.commit, data_root=args.data_root, runroot=args.runroot, client=args.client,
                                deployment=deployment, deployments=deployments, only=only)
        else:
            if not args.branch:
                raise PreflightError("--bundle 要同时给 --branch")
            scratch = args.scratch or (args.runroot / background.DIRECTORY / "preflight")
            report = preflight_with_bundle(bundle=args.bundle, branch=args.branch, commit=args.commit, data_root=args.data_root,
                                           runroot=args.runroot, client=args.client, deployment=deployment, deployments=deployments,
                                           scratch_parent=scratch, only=only)
    except (PreflightError, getattr(_sibling("background_validation"), "ValidationError", PreflightError)) as error:
        print(f"起跑前置检查：{error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        print(format_failures(report), file=sys.stderr)
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    sys.exit(main())
