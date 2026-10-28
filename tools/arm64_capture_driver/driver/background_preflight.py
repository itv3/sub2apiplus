#!/usr/bin/env python3
"""后台验证起跑前置检查（Codex 升级工具优化第 4 项）：起跑前逐项核对冻结收据、基线重封、重部署、准入绑定、主机 Go 版本、本机完整测试收据、TUI 预探收据七项，不通过就拒绝起跑。

为什么要有：一次 Codex 升级里后台验证白跑过两次，原因都是“起跑顺序错”——先于冻结承接收据起跑（冻结测试在两个多小时后才红），先于重部署起跑
（部署换了，结论作废）。后台验证一轮约 136 分钟，顺序错的代价是整轮；这些前提全都能在几秒到一两分钟内查清，所以起跑前先查，不通过就不起。
之后又白跑过一次，原因是“环境与被验证提交不匹配”（第五项 host-toolchain，见下）。

七项检查（互相独立，一项不过其余照查，最后一起汇总；任一项不过即拒绝起跑）：

* ``freeze-receipts`` 冻结收据：被验证提交的树里，每个登记在冻结台账里的路径，当前内容的摘要都得是台账登记过的摘要（``tools.upstream_merge.freeze``
  的 ``load_frozen_edges``，与门禁里 Go／Python 冻结测试同一张承接图）。源码改了而承接收据还没单独提交，就是未承接——冻结测试必红。
* ``baseline-reseal`` 基线重封：受管工具树的身份摘要必须和测试树里固化的守恒基线一致（Codex 跑身份守恒测试，Claude 跑工具身份策略测试，与门禁里同一份测试）。
  改了受管工具文件而没有重封基线，守恒测试必红。
* ``redeploy`` 重部署：数据根最新的受管工具部署收据登记的整树摘要，必须等于被验证提交的测试树按该客户端策略现算的整树摘要，部署一致性各部分（受管整树含测试、
  指南、部署脚本副本）逐项相同；已纳管的每个客户端都核对。没重新部署，后台验证的结论绑在旧部署上，部署一换就作废。
* ``admission-binding`` 准入绑定：这次起跑产生的结论，必须是 VC-5 准入（``vc5_admission`` → ``background_validation.check``）按“当前部署”能查到的那一份：
  (1) 部署收据此刻仍是最新的一份，没有在检查期间换掉；(2) 已纳管客户端都在要绑定的部署集合里；(3) 结论按这套部署算出的绑定键与准入查找所用的键一致；
  (4) 已安装的驱动（有 ``install.py`` 的安装布局）复验通过——它的安装收据绑定的部署收据仍是最新，否则 guard 会在派发前停线。
* ``host-toolchain`` 主机工具链（后加的第五项）：执行器单元实际用到的那个 ``go``，版本不得低于被验证提交 ``backend/go.mod`` 的 ``go`` 指令
  （按（主,次,补丁）比较，补丁缺省按 0）。
  事故：上游合并把 ``go`` 指令抬到 1.27.3，采集主机 ``/usr/local/go`` 还是 1.27.2。声明级出站单元运行期强制离线（``GOTOOLCHAIN=local``，见
  ``entry_gates.DECLARED_OFFLINE_ENV``），其中由 ``make`` 起、在仓库根执行 ``cd backend && go run …`` 的两个（``egress-spec:egress-scanner-check``、
  ``egress-spec:egress-spec-egressscan-self-test``）只能用主机自己的 go，报 ``go.mod requires go >= 1.27.3 (running go 1.27.2; GOTOOLCHAIN=local)``
  退出 2，整轮后台验证（子集约 69 分钟、全集几个小时）白跑成 failed。直接在 ``backend/`` 下跑、``GOTOOLCHAIN`` 缺省 auto 的单元会自动切到模块缓存里的
  新工具链而通过，所以这个不匹配只对离线单元发作，不在起跑前查就要等跑到那两个单元才红。
  取法：用执行器同一个固定 PATH（``EXEC_PATH``，与 ``entry-gates.sh`` 里的值逐字相同，测试核对两处一致；与操作员 shell 的 PATH 无关）解析 ``go``，按离线单元的
  环境运行 ``go version`` 并解析 ``goX.Y.Z``：``GOFLAGS``／``GOPROXY``／``GOTOOLCHAIN`` 三个变量强制成离线单元的值（``HOST_GO_PROBE_ENV``，测试核对与
  ``DECLARED_OFFLINE_ENV`` 一致），其余 ``GO*``／``CGO_*`` 与执行器一样照调用方放行。强制 ``GOTOOLCHAIN=local`` 是关键：不强制的话 go 会按 go.mod 悄悄切换
  工具链，测到的是切换后的版本，恰好掩盖主机 Go 太旧。
  失败关闭：``backend/go.mod`` 读不到或没有恰好一个可解析的 go 指令、PATH 里找不到 go、go 跑不起来／超时／退出码非零、输出解析不了（开发版、候选版、被改写的
  输出一律不认），都算不通过，不猜。
  ``toolchain`` 行的取舍：只作提示，不作硬门槛。它的作用是在 go 命令被允许切换工具链（``GOTOOLCHAIN`` 为 auto／path）、而本机工具链又低于它时，挑一个更高的去用；
  ``GOTOOLCHAIN=local`` 时 go 命令不据它做任何决定，只有 ``go`` 指令是硬要求。所以主机 Go 低于 toolchain 行但不低于 go 指令时照样放行，只在报告 detail 里记
  ``toolchain_note``（缺省 auto 的单元会尝试切到模块缓存或网络里的那个工具链，缓存里没有就会失败，值得知会，但那是另一类风险，硬拦会误杀本来能跑的起跑）。
  只核 ``backend/go.mod``：门禁命令编译的仓库内模块只有它（另一个 ``fingerprint_proxy`` 模块不被任何门禁命令引用，由抓包流程自己构建）。

* ``local-capture-receipt`` 本机完整测试收据（第六项，Codex 升级工具优化第 4 项）：被验证提交必须已有“本机完整 ``make test-capture-tools`` 通过”的本地收据
  （``local/local-capture-tools-receipt.sh`` 生成，放 ``<运行根>/local-receipts/``）：提交号一致、命令是完整目标、退出码 0、工作树干净、封口对得上。上一轮入口门禁第三轮
  就红在只跑了定向测试的修复提交上（注释里写了版本号，``test_driver_client_table`` 红，白跑 1.8 小时并多部署一次）。核对逻辑在 ``opening_checks.py``（入口便宜检查共用）。
* ``tui-prelaunch-receipt`` TUI 零请求启动预探收据（第七项，优化第 5 项）：新版二进制对各 TUI 场景（compact、ws-idle-close、daemon、guardian）做过零请求启动预探且全部通过
  （``tui-prelaunch-receipt.sh`` 生成，同一收据目录）。参数文件（``--vc-env``）能读到 ``TARGET_VERSION``／``CODEX_BIN_SHA256`` 时，一并核对收据对的就是本轮目标版本与登记的二进制。
* 第六、七项只对 Codex 做（Claude 轮次通过并在 detail 记 ``not_applicable``）。

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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

SCHEMA = "background-preflight/v1"
HERE = Path(__file__).resolve().parent
# 检查项的固定顺序（报告里的 checks 数组、--only 的可选值都按它）；新增检查只追加在末尾，前面各项的位置不变。
CHECK_NAMES = ("freeze-receipts", "baseline-reseal", "redeploy", "admission-binding", "host-toolchain", "local-capture-receipt", "tui-prelaunch-receipt")
# 各客户端的基线重封检查：与门禁里同一份测试（改受管工具而没重封基线，这份测试必红）。
BASELINE_TESTS = {
    "codex": "tools.official_client_capture.tests.test_codex_identity_conservation",
    "claude": "tools.official_client_capture.tests.test_claude_tool_identity_policy",
}
BASELINE_TEST_TIMEOUT_SECONDS = 900
GIT_TIMEOUT_SECONDS = 900
UNCOVERED_LIMIT = 20

# ---- 第五项 host-toolchain 用到的常量 ----
# 执行器单元看到的固定 PATH：与 entry-gates.sh 里的 EXEC_PATH 逐字相同（测试核对两处一致）。执行器用 env -i 起每个单元，PATH 只有这一个值，
# 所以“单元用哪个 go”由它决定，与操作员 shell 的 PATH（lib.sh 会往前面加 go 目录）无关。
EXEC_PATH = "/usr/local/go/bin:/opt/node-v20/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
# 被验证提交里决定 Go 版本要求的文件（门禁命令编译的仓库内模块只有它）。
GO_MOD_RELATIVE = "backend/go.mod"
# 探测主机 go 版本时强制的三个变量：与声明级单元运行期的离线要求同值（entry_gates.DECLARED_OFFLINE_ENV，测试核对一致）。其中 GOTOOLCHAIN=local 是关键：
# 不强制的话 go 会按 go.mod 悄悄切换到别的工具链，go version 报出的是切换后的版本，恰好掩盖主机 Go 太旧。
HOST_GO_PROBE_ENV = {"GOFLAGS": "-mod=readonly", "GOPROXY": "off", "GOTOOLCHAIN": "local"}
HOST_GO_PROBE_TIMEOUT_SECONDS = 60
# go.mod 里的 go／toolchain 指令各占一行（取值后只许空白或 // 注释）；版本号只认 X.Y 与 X.Y.Z，带 rc／beta／devel 之类后缀的一律不认（失败关闭）。
_GO_DIRECTIVE = re.compile(r"[ \t]*go[ \t]+(?P<value>[^\s/]+)[ \t]*(?://.*)?")
_TOOLCHAIN_DIRECTIVE = re.compile(r"[ \t]*toolchain[ \t]+(?P<value>[^\s/]+)[ \t]*(?://.*)?")
_GO_MOD_VERSION = re.compile(r"([0-9]+)\.([0-9]+)(?:\.([0-9]+))?")
# toolchain 行的取值形如 go1.27.5（也可能带 -厂商后缀，或写成 default）；只取前面的数字部分比较，认不出来（default 等）就不比较。
_TOOLCHAIN_NAME = re.compile(r"go([0-9]+)\.([0-9]+)(?:\.([0-9]+))?")
# `go version` 的输出：go version go1.27.2 linux/arm64（版本号后必须紧跟空白或结束，rc／beta／devel 版本不匹配）。
_GO_VERSION_OUTPUT = re.compile(r"go version go([0-9]+)\.([0-9]+)(?:\.([0-9]+))?(?:[ \t].*)?")

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
# 七项检查
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


def check_baseline_reseal(tree: Path, client: str | Sequence[str], *, runner: Callable[..., Any] = _run) -> dict[str, Any]:
    """基线重封检查：``client`` 可以是一个客户端名，也可以是客户端名的序列——两客户端都纳管时要逐个核对（验收修正建议 12：只核本轮客户端的话，
    另一客户端的受管工具被改而没重封基线也会放行起跑）。任一客户端没有登记检查（fail-closed）或测试不通过，整项不通过，问题里写明是哪个客户端。"""

    clients = [client] if isinstance(client, str) else list(client)
    problems: list[str] = []
    tests: dict[str, str] = {}
    codes: dict[str, int] = {}
    for name in clients:
        module = BASELINE_TESTS.get(name)
        if module is None:
            problems.append(f"客户端 {name!r} 没有登记基线重封检查（fail-closed）")
            continue
        completed = runner([sys.executable, "-B", "-m", "unittest", module], cwd=tree, timeout=BASELINE_TEST_TIMEOUT_SECONDS)
        tests[name], codes[name] = module, completed.returncode
        if completed.returncode != 0:
            tail = "\n".join((completed.stderr or completed.stdout).strip().splitlines()[-6:])
            prefix = f"{name}：" if len(clients) > 1 else ""
            problems.append(f"{prefix}受管工具身份与固化的守恒基线不一致（{module} 退出码 {completed.returncode}）。改了受管工具文件要【单独提交】重封基线后再起后台验证：{tail[-500:]}")
    if len(clients) == 1 and tests:
        # 单个客户端：detail 的形状与改造前相同。
        return _result("baseline-reseal", problems, test=tests[clients[0]], returncode=codes[clients[0]])
    return _result("baseline-reseal", problems, tests=tests, returncodes=codes)


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


def parse_go_mod(text: str) -> tuple[list[str], list[str]]:
    """从 go.mod 文本里取出全部 ``go`` 指令与 ``toolchain`` 指令的取值（原文，按出现顺序），分别返回。

    按行匹配：指令独占一行，取值之后只许空白或 ``//`` 注释。require 块里缩进的模块行不会被误认——模块路径紧跟在 ``go`` 后面的是 ``go.xxx/…``（``go`` 后没有空白），
    取值后面还跟着版本号的行也过不了“取值后只许空白或注释”这一关。"""

    go_values: list[str] = []
    toolchain_values: list[str] = []
    for line in text.splitlines():
        matched = _GO_DIRECTIVE.fullmatch(line)
        if matched:
            go_values.append(matched.group("value"))
            continue
        matched = _TOOLCHAIN_DIRECTIVE.fullmatch(line)
        if matched:
            toolchain_values.append(matched.group("value"))
    return go_values, toolchain_values


def _version_tuple(major: str, minor: str, patch: str | None) -> tuple[int, int, int]:
    """主、次、补丁转成可比较的整数元组；补丁缺省按 0（go.mod 的 ``go 1.27`` 与老式 ``go version go1.27`` 都可能没有补丁段）。"""

    return (int(major), int(minor), int(patch or 0))


def _host_go_environment(exec_path: str, environ: Mapping[str, str]) -> dict[str, str]:
    """运行 ``go version`` 的环境：沿用执行器白名单环境的思路（PATH、LANG 固定；HOME、TZ、TMPDIR 与 ``GO*``／``CGO_*`` 照调用方放行，其余一律不带），
    再把三个 Go 变量强制成离线单元的值（``HOST_GO_PROBE_ENV``）——不联网，也不让 go 因 go.mod 较新而切换到别的工具链。"""

    env = {"PATH": exec_path, "LANG": "C.UTF-8"}
    for name in ("HOME", "TZ", "TMPDIR"):
        if name in environ:
            env[name] = environ[name]
    for name, value in environ.items():
        if name.startswith(("GO", "CGO_")):
            env[name] = value
    env.update(HOST_GO_PROBE_ENV)
    return env


def check_host_toolchain(tree: Path, *, exec_path: str | None = None, environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """主机工具链检查：执行器单元实际用的 ``go`` 不得低于被验证提交 ``backend/go.mod`` 的 ``go`` 指令（背景、取法与 ``toolchain`` 行的取舍见模块文档串）。

    ``exec_path`` 缺省取执行器固定 PATH（``EXEC_PATH``），``environ`` 缺省取当前进程环境；两者只给测试注入假 go 与固定环境用，生产路径不传。
    不联网、不写任何文件。``detail`` 记下所有取到的事实（要求的版本、go 的路径与版本、go.mod 的 toolchain 行），失败时也带上已经取到的那部分。"""

    name = "host-toolchain"
    path_value = EXEC_PATH if exec_path is None else exec_path
    source_env = os.environ if environ is None else environ
    detail: dict[str, Any] = {"go_mod": GO_MOD_RELATIVE, "required_go": None, "toolchain_line": None, "exec_path": path_value,
                              "go_path": None, "host_go": None, "go_version_output": None}

    def fail(message: str) -> dict[str, Any]:
        return _result(name, [message], **detail)

    # 一、被验证提交要求的版本：backend/go.mod 里恰好一个、能解析的 go 指令。读不到、没有、有多个、写法认不出来都失败关闭。
    try:
        text = (Path(tree) / GO_MOD_RELATIVE).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return fail(f"读不到被验证提交的 {GO_MOD_RELATIVE}，没法核对主机 Go 版本（失败关闭）：{error}")
    go_values, toolchain_values = parse_go_mod(text)
    detail["toolchain_line"] = toolchain_values[0] if toolchain_values else None
    if len(go_values) != 1:
        listed = f"（{'、'.join('go ' + value for value in go_values)}）" if go_values else ""
        return fail(f"{GO_MOD_RELATIVE} 里应当恰有一个 go 指令，实际有 {len(go_values)} 个{listed}，没法确定被验证提交要求的 Go 版本（失败关闭）")
    required_text = go_values[0]
    detail["required_go"] = required_text
    required_match = _GO_MOD_VERSION.fullmatch(required_text)
    if required_match is None:
        return fail(f"{GO_MOD_RELATIVE} 的 go 指令 go {required_text} 不是 X.Y 或 X.Y.Z 形式，没法和主机 Go 比较（失败关闭）")
    required = _version_tuple(*required_match.groups())

    # 二、执行器单元用的 go：在执行器的固定 PATH 里按顺序找（与操作员 shell 的 PATH 无关）。
    go_path = shutil.which("go", path=path_value)
    if go_path is None:
        return fail(f"执行器的固定 PATH 里找不到 go：{path_value}。执行器单元用这个 PATH 解析 go（与操作员 shell 的 PATH 无关），"
                    f"要先在采集主机装好不低于 go {required_text} 的 Go（常规位置 /usr/local/go），再起后台验证")
    detail["go_path"] = go_path

    # 三、运行 go version：离线单元的环境（强制 GOTOOLCHAIN=local），站在根目录（与被验证的树没有任何关系）。
    try:
        completed = subprocess.run([go_path, "version"], cwd=os.sep, env=_host_go_environment(path_value, source_env), capture_output=True,
                                   text=True, errors="replace", timeout=HOST_GO_PROBE_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, check=False)
    except subprocess.TimeoutExpired:
        return fail(f"运行 {go_path} version 超过 {HOST_GO_PROBE_TIMEOUT_SECONDS} 秒没有返回，没法确定主机 Go 版本（失败关闭）")
    except OSError as error:
        return fail(f"{go_path} 运行不起来（{error}）：可能不是本机架构的可执行文件，或没有执行权限，没法确定主机 Go 版本（失败关闭）")
    output = (completed.stdout or "").strip()
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    detail["go_version_output"] = first_line[:200]
    if completed.returncode != 0:
        return fail(f"{go_path} version 退出码 {completed.returncode}：{(completed.stderr or output).strip()[-300:]}。没法确定主机 Go 版本（失败关闭）")
    parsed = _GO_VERSION_OUTPUT.fullmatch(first_line)
    if parsed is None:
        return fail(f"{go_path} version 的输出解析不了（只认正式发布版的 go version goX.Y.Z …，开发版、候选版一律不认）：{first_line[:200]!r}（失败关闭）")
    host = _version_tuple(*parsed.groups())
    host_text = ".".join(part for part in parsed.groups() if part is not None)
    detail["host_go"] = host_text

    # 四、比较。硬门槛只有 go 指令；toolchain 行只记提示（理由见模块文档串）。
    if host < required:
        return fail(f"主机 Go 太旧：执行器单元用的 go 是 {go_path}（go{host_text}），低于被验证提交 {GO_MOD_RELATIVE} 要求的 go {required_text}。"
                    "后果：声明级 Go 单元运行期强制 GOTOOLCHAIN=local（不联网、不切换工具链），只能用主机自己的 go；由 make 起、在仓库根执行 cd backend && go run … 的 "
                    "egress-spec:egress-scanner-check、egress-spec:egress-spec-egressscan-self-test 就会报 "
                    f"go.mod requires go >= {required_text} (running go {host_text}; GOTOOLCHAIN=local) 并退出 2，整轮后台验证白跑。"
                    f"处理：把主机 Go 升级到不低于 {required_text}——官方完整 GOROOT 可从模块缓存 …/pkg/mod/golang.org/toolchain@v0.0.1-go{'.'.join(map(str, required))}.linux-arm64 拷出，"
                    "换到原路径（真目录，不用符号链接；属主 root、去掉组和其他人的写权限）；升级必须在没有后台验证运行时进行（环境一变，在跑的验证作废）；换完再起跑")
    suggested = _TOOLCHAIN_NAME.match(detail["toolchain_line"] or "")
    if suggested is not None and host < _version_tuple(*suggested.groups()):
        detail["toolchain_note"] = (f"{GO_MOD_RELATIVE} 的 toolchain 行建议 {detail['toolchain_line']}，主机 go{host_text} 低于它但满足 go 指令，放行。"
                                    "只有 GOTOOLCHAIN 缺省（auto）的单元会尝试切到模块缓存或网络里的那个工具链，缓存里没有就会失败；"
                                    "GOTOOLCHAIN=local 的离线单元不据 toolchain 行做决定。要消除这个提示，把主机 Go 升到不低于它")
    return _result(name, [], **detail)


# ---------------------------------------------------------------------------
# 起跑前两张收据（第六、七项）
# ---------------------------------------------------------------------------

RECEIPT_DIR_NAME = "local-receipts"


def _env_value(vc_env: Path | None, key: str) -> str | None:
    """从本轮参数文件里读一个 ``KEY="值"`` 的字面赋值（只读文本，不 source、不执行）；读不到返回 ``None``。"""

    if vc_env is None:
        return None
    try:
        text = Path(vc_env).read_text(encoding="utf-8")
    except OSError:
        return None
    match = re.search(rf'(?m)^(?:export )?{re.escape(key)}=(?:"([^"\n]*)"|\'([^\'\n]*)\'|([^\s"\'#]*))\s*$', text)
    if not match:
        return None
    value = next((group for group in match.groups() if group is not None), "")
    return value or None


def check_local_capture_receipt(runroot: Path, commit: str, client: str) -> dict[str, Any]:
    if client != "codex":
        return _result("local-capture-receipt", [], not_applicable=f"客户端 {client} 没有这条流程")
    checks = _sibling("opening_checks")
    directory = Path(runroot) / RECEIPT_DIR_NAME
    return _result("local-capture-receipt", checks.check_capture_receipt(directory, commit), receipt_dir=str(directory), commit=commit)


def check_tui_prelaunch_receipt(runroot: Path, client: str, vc_env: Path | None) -> dict[str, Any]:
    if client != "codex":
        return _result("tui-prelaunch-receipt", [], not_applicable=f"客户端 {client} 没有这条流程")
    checks = _sibling("opening_checks")
    directory = Path(runroot) / RECEIPT_DIR_NAME
    version, binary_sha = _env_value(vc_env, "TARGET_VERSION"), _env_value(vc_env, "CODEX_BIN_SHA256")
    return _result("tui-prelaunch-receipt", checks.check_tui_receipt(directory, target_version=version, binary_sha256=binary_sha),
                   receipt_dir=str(directory), target_version=version, bound_to_registered_binary=binary_sha is not None)


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
               runner: Callable[..., Any] = _run, steps: Any = None, background: Any = None,
               exec_path: str | None = None, environ: Mapping[str, str] | None = None, vc_env: Path | None = None) -> dict[str, Any]:
    """七项检查全部执行，汇总成一份报告；``only`` 只跑指定项（诊断用）。

    ``exec_path``／``environ`` 只给主机工具链检查（host-toolchain）用，缺省取执行器固定 PATH 与当前进程环境，生产路径不传；测试用它们注入假 go。
    ``vc_env`` 是本轮参数文件，TUI 预探收据检查用它读目标版本与登记的二进制摘要（读不到就只核收据自身）。"""

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
            result = check_baseline_reseal(tree, clients, runner=runner)
        elif name == "redeploy":
            result = check_redeploy(tree, data_root, clients, steps=steps)
        elif name == "host-toolchain":
            result = check_host_toolchain(tree, exec_path=exec_path, environ=environ)
        elif name == "local-capture-receipt":
            result = check_local_capture_receipt(runroot, commit, client)
        elif name == "tui-prelaunch-receipt":
            result = check_tui_prelaunch_receipt(runroot, client, vc_env)
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
                          deployments: Mapping[str, Mapping[str, Any]] | None, scratch_parent: Path, only: Sequence[str] | None = None,
                          vc_env: Path | None = None) -> dict[str, Any]:
    """从 bundle 检出临时树、跑检查、删掉临时树。临时树放在 ``scratch_parent`` 下（不碰后台工作目录，免得影响其来源快照绑定）。"""

    scratch_parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = Path(tempfile.mkdtemp(prefix=f"preflight-{commit[:12]}-", dir=scratch_parent))
    try:
        tree = materialize_tree(bundle, branch, commit, destination / "tree")
        return run_checks(tree=tree, commit=commit, data_root=data_root, runroot=runroot, client=client, deployment=deployment,
                          deployments=deployments, only=only, vc_env=vc_env)
    finally:
        shutil.rmtree(destination, ignore_errors=True)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check", help="跑七项起跑前置检查，打印 JSON 报告")
    check.add_argument("--runroot", type=Path, required=True)
    check.add_argument("--data-root", type=Path, required=True)
    check.add_argument("--commit", required=True)
    check.add_argument("--client", default="codex")
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--tree", type=Path, help="被验证提交的检出（已有）")
    source.add_argument("--bundle", type=Path, help="从 bundle 检出（与 --branch 同用）")
    check.add_argument("--branch", default=None)
    check.add_argument("--only", default=None, help="只跑指定检查项，逗号分隔（" + "、".join(CHECK_NAMES) + "）")
    check.add_argument("--scratch", type=Path, default=None, help="临时树的父目录（缺省 <runroot>/background-validation/preflight）")
    check.add_argument("--vc-env", type=Path, default=None, help="本轮参数文件（TUI 预探收据检查读目标版本与登记的二进制摘要；缺省只核收据自身）")
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
                                deployment=deployment, deployments=deployments, only=only, vc_env=args.vc_env)
        else:
            if not args.branch:
                raise PreflightError("--bundle 要同时给 --branch")
            scratch = args.scratch or (args.runroot / background.DIRECTORY / "preflight")
            report = preflight_with_bundle(bundle=args.bundle, branch=args.branch, commit=args.commit, data_root=args.data_root,
                                           runroot=args.runroot, client=args.client, deployment=deployment, deployments=deployments,
                                           scratch_parent=scratch, only=only, vc_env=args.vc_env)
    except (PreflightError, getattr(_sibling("background_validation"), "ValidationError", PreflightError)) as error:
        print(f"起跑前置检查：{error}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        print(format_failures(report), file=sys.stderr)
    return 0 if report["passed"] else 3


if __name__ == "__main__":
    sys.exit(main())
