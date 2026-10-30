"""整链空跑补盲区：三类上一版真实撞到、而上一版空跑没报出的问题的探针执行器（测试夹具，不进受管身份）。

上一版整链空跑（任务第 13 项）花了 2.9 小时关键路径，一个缺陷也没发现；之后正式运行暴露的缺陷里，下面三类本来就该被空跑拦住：

1. **新版二进制拒收调用点参数**（``--disable`` 被拒）：新版客户端取消了 TUI 对共享后台服务的功能开关白名单，带 ``--disable`` 的 TUI
   没有 ``--no-daemon`` 就报错退出；入口第 13 步探测 5 个组合 3 个失败，排在 2 小时门禁之后。空跑只核对“驱动参数 == 探测表”，
   从没把调用点参数交给**真的二进制**。→ ``StartupFlagRunner``：用受管驱动展开每个 TUI 调用点的真实客户端命令行，
   再交给二进制启动一次（只看启动前期有没有被拒，零请求），任何被拒的组合当场列出。
2. **标签与判据对不上真实候选证据**：见 ``registration_preview``；空跑的合成夹具里 WS 观测是手造的，从不核对标签声明与真实分片布局。
   → ``PriorCandidateReplayRunner``：把本树最新的标签声明与候选期望清单对**上一版真实候选证据**回放（seal 预检可达性＋assert-rules）。
3. **收尾门禁带收尾环境变量时的测试隔离**：收尾门禁给门禁进程设了 ``UNIT_EXECUTOR_RETRY_LEDGER``，被测试嵌套起的
   临时执行器继承，3 个单元红、整轮白跑 2.1 小时；本机不带变量全绿。→ ``closeout_env_runner``：把 ``codex_closeout.GATE_ENV_WHITELIST``
   里分类为“会被单元继承”（units）的全部环境变量（从该白名单里取，不手抄、不另写一套判定）原样注入，再跑一遍会嵌套起执行器的最小测试全集。

三个探针都带自检：干净树必须通过，历史缺陷放回去必须被报出（见 ``chain_rehearsal_catalog.BLINDSPOT_PROBES``）。
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import pty
import re
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture.tests.chain_rehearsal import (
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    Outcome,
    RunContext,
    Runner,
    UnittestRunner,
    child_environment,
    expand_placeholders,
    _tail,
)

STARTUP_RESULT_MARKER = "CHAIN_REHEARSAL_STARTUP_RESULT "
# 新版二进制拒收调用点参数时，启动前期打到终端的拒绝特征（宁可多列，误报比漏报好；命中后还要结合“提前非零退出”才算拒收）。
REJECTION_PATTERNS = (
    r"Cannot use the shared background server",
    r"rerun with --no-daemon",
    r"unexpected argument",
    r"unrecognized (?:option|argument|subcommand)",
    r"unknown (?:option|flag|argument)",
    r"invalid value .* for",
    r"^error: ",
)
# 启动探测里用的功能开关取值：与实采入口的缺省一致（relay 场景脚本的 DISABLE_FEATURES 缺省）。
DEFAULT_PROBE_ENVIRONMENT = {"DISABLE_FEATURES": "plugins apps"}
# 会嵌套起临时执行器、或读收尾环境变量的最小测试全集（按“含 ``**os.environ`` 且碰执行器/门禁/后台验证”筛出；
# 新增这类模块时 ``test_chain_rehearsal_blindspots`` 的清单自检会红，提醒补进来）。
CLOSEOUT_NESTED_EXECUTOR_MODULES = (
    "test_ci_unit_executor",
    "test_ci_unit_executor_retry",
    "test_ci_unit_executor_cleanup_hardening",
    "test_ci_unit_executor_descendant_cleanup",
    "test_ci_background_validation",
    "test_ci_background_validation_retry",
    "test_ci_background_reuse",
    "test_ci_background_scope",
    "test_ci_entry_gates",
    "test_ci_retry_ledger_across_closeouts",
    "test_ci_unit_records",
    "test_ci_gate_scope",
    "test_ci_inherit_predict",
    "test_ci_reservation_preemption",
    "test_ci_go_compile_warmup",
    "test_ci_go_test_packages",
)


# ---------------------------------------------------------------------------
# 1) 新版二进制启动探测
# ---------------------------------------------------------------------------


def unshare_network_available() -> tuple[bool, str]:
    """能否在无网络的私有命名空间里起子进程（真二进制必须在这里跑，保证零请求）。"""

    if not sys.platform.startswith("linux"):
        return False, f"不是 Linux（{sys.platform}），没法建私有网络命名空间"
    if shutil.which("unshare") is None:
        return False, "PATH 里没有 unshare"
    completed = subprocess.run(["unshare", "-rn", "true"], capture_output=True, timeout=30)
    return (completed.returncode == 0, "unshare -rn 可用" if completed.returncode == 0 else "unshare -rn 失败（无权建用户/网络命名空间）")


def run_binary_once(binary: Sequence[str], arguments: Sequence[str], *, window_seconds: float, isolate: bool,
                    home: Path) -> dict[str, Any]:
    """在伪终端里启动一次二进制，观察 ``window_seconds`` 秒：提前非零退出或输出命中拒绝特征即判被拒。"""

    command = [*binary, *arguments]
    if isolate:
        command = ["unshare", "-rn", *command]
    environment = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(home), "CODEX_HOME": str(home / ".codex"),
        "TERM": "xterm-256color", "NO_COLOR": "1", "LANG": "C.UTF-8",
        # 即使没隔离（只在伪造的假二进制上允许），也指向一个必然连不上的代理。
        "HTTPS_PROXY": "http://127.0.0.1:9", "HTTP_PROXY": "http://127.0.0.1:9",
    }
    pid, descriptor = pty.fork()
    if pid == 0:  # 子进程
        try:
            os.execvpe(command[0], command, environment)
        finally:
            os._exit(127)
    output = bytearray()
    deadline = time.monotonic() + window_seconds
    exited: int | None = None
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([descriptor], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(descriptor, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                exited = os.waitstatus_to_exitcode(status)
                # 读尽退出前的残余输出。
                while True:
                    ready, _, _ = select.select([descriptor], [], [], 0.05)
                    if not ready:
                        break
                    try:
                        chunk = os.read(descriptor, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output.extend(chunk)
                break
    finally:
        if exited is None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
                time.sleep(0.3)
                try:
                    done, _ = os.waitpid(pid, os.WNOHANG)
                except ChildProcessError:
                    break
                if done:
                    break
        try:
            os.close(descriptor)
        except OSError:
            pass
    text = output.decode("utf-8", "replace")
    matched = [pattern for pattern in REJECTION_PATTERNS if re.search(pattern, text, re.MULTILINE | re.IGNORECASE)]
    rejected = (exited is not None and exited != 0) or bool(matched)
    return {
        "arguments": list(arguments), "exited": exited, "rejected": rejected,
        "matched_patterns": matched, "output_head": text.strip()[:600],
    }


def _record_client_commands(tree: Path, scenarios: Mapping[str, Mapping[str, Any]], work: Path) -> dict[str, list[list[str]]]:
    """用受管驱动 ``drive_codex_tui.py`` 展开每个 TUI 调用点的真实客户端命令行。

    调用点的驱动参数由测试里“用 bash 真实执行调用点参数展开”的助手给出（与探测表逐项对照过）；把 ``--codex-bin`` 换成一个只记录
    命令行就退出的垫片，驱动照常拼装客户端命令行并执行它，垫片落盘后驱动因进程退出而失败——失败是预期的，只要命令行落盘就行。
    """

    from tools.official_client_capture.tests import test_client_launch_probe as callsites

    shim = work / "record-argv.py"
    shim.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys\n"
        "open(os.environ['REHEARSAL_ARGV_OUT'], 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    drive = tree / "tools" / "official_client_capture" / "drive_codex_tui.py"
    recorded: dict[str, list[list[str]]] = {}
    for scenario, spec in scenarios.items():
        arguments = callsites._expand_call_site(spec["marker"], dict(DEFAULT_PROBE_ENVIRONMENT))
        patched: list[str] = []
        skip = False
        for index, item in enumerate(arguments):
            if skip:
                skip = False
                continue
            if item == "--codex-bin":
                patched += ["--codex-bin", str(shim)]
                skip = True
            elif item == "--cwd":
                patched += ["--cwd", str(work)]
                skip = True
            elif item == "--log":
                skip = True
            else:
                patched.append(item)
        out = work / f"argv-{scenario}.jsonl"
        env = {**os.environ, "REHEARSAL_ARGV_OUT": str(out), "PYTHONDONTWRITEBYTECODE": "1"}
        # 垫片一落盘就够了；驱动随后会因客户端进程已退出而等待/失败，不等它自己结束。
        process = subprocess.Popen([sys.executable, "-B", str(drive), *patched], env=env, cwd=str(work),
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline and not (out.is_file() and out.stat().st_size > 0) and process.poll() is None:
            time.sleep(0.05)
        if process.poll() is None:
            process.kill()
        process.wait()
        lines = out.read_text(encoding="utf-8").splitlines() if out.is_file() else []
        recorded[scenario] = [json.loads(line) for line in lines]
    return recorded


def startup_flags_main(binary: Sequence[str], *, window_seconds: float, isolate: bool) -> dict[str, Any]:
    """子进程入口：展开全部 TUI 调用点的客户端命令行，逐个交给二进制启动，返回逐组合结论。"""

    from tools.official_client_capture.tests import test_client_launch_probe as callsites

    with tempfile.TemporaryDirectory(prefix="startup-flags-") as directory:
        work = Path(directory)
        recorded = _record_client_commands(Path.cwd(), callsites.probe.TUI_SCENARIOS, work)
        combos: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for scenario, commands in recorded.items():
            for command in commands:
                key = tuple(command)
                if key in seen:
                    continue
                seen.add(key)
                home = work / f"home-{len(combos)}"
                home.mkdir()
                # run_binary_once 把 CODEX_HOME 指向 home/.codex；真二进制不会替我们建这个目录，找不到就直接拒绝启动
                # （ARM64 实测：不建时 4/4 被拒，预先建好后 3 个 TUI 都能正常启动），所以调用真二进制前必须先建好。
                (home / ".codex").mkdir(parents=True)
                result = run_binary_once(binary, command, window_seconds=window_seconds, isolate=isolate, home=home)
                combos.append({"scenario": scenario, **result})
        missing = [scenario for scenario, commands in recorded.items() if not commands]
    return {"combos": combos, "scenarios_without_command": missing}


class StartupFlagRunner(Runner):
    """新版二进制启动探测：被测树里的 TUI 调用点命令行，能不能被这个二进制启动起来。

    二进制来源（按序）：构造参数 ``binary``（假二进制自检用，``{tree}`` 展开）→ 环境变量 ``REHEARSAL_CODEX_BIN``（运维给的新版真二进制）。
    都没有记“跳过＝未执行”。真二进制必须在无网络的私有命名空间里跑（``unshare -rn``），做不到同样记未执行；假二进制不联网，不要求隔离。
    """

    def __init__(self, *, binary: Sequence[str] | None = None, binary_env: str = "REHEARSAL_CODEX_BIN",
                 window_seconds: float = 6.0, timeout_seconds: float = 300.0) -> None:
        self.binary = tuple(binary) if binary else None
        self.binary_env = binary_env
        self.window_seconds = window_seconds
        self.timeout_seconds = timeout_seconds
        self.description = "startup-flags " + (" ".join(self.binary) if self.binary else f"${binary_env}")

    def run(self, context: RunContext) -> Outcome:
        if self.binary:
            binary = [expand_placeholders(item, context) for item in self.binary]
            isolate = False
        else:
            raw = os.environ.get(self.binary_env, "").strip()
            if not raw:
                return Outcome("skip", f"没有给新版二进制（环境变量 {self.binary_env}）：启动探测只能用真二进制，不用旧版代替")
            binary = [raw]
            if not Path(raw).is_file():
                return Outcome("error", f"{self.binary_env} 指向的二进制不存在：{raw}")
            ok, reason = unshare_network_available()
            if not ok:
                return Outcome("skip", f"真二进制必须在无网络的命名空间里启动以保证零请求：{reason}")
            isolate = True
        argv = [sys.executable, "-B", "-m", "tools.official_client_capture.tests.chain_rehearsal_blindspots", "_startup_flags",
                "--binary-json", json.dumps(binary), "--window", str(self.window_seconds), *(["--isolate"] if isolate else [])]
        try:
            completed = subprocess.run(argv, cwd=str(context.tree), env=child_environment(context.tree), capture_output=True,
                                       text=True, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as error:
            return Outcome("error", f"超时（{self.timeout_seconds:g} 秒）", _tail((error.stdout or "") if isinstance(error.stdout, str) else ""))
        payload = None
        for line in completed.stdout.splitlines():
            if line.startswith(STARTUP_RESULT_MARKER):
                payload = json.loads(line[len(STARTUP_RESULT_MARKER):])
        output = (completed.stdout + completed.stderr)
        if not isinstance(payload, dict):
            return Outcome("error", f"启动探测没有给出结果（退出码 {completed.returncode}）", _tail(output))
        return classify_startup(payload)


def classify_startup(payload: Mapping[str, Any]) -> Outcome:
    combos = payload.get("combos") or []
    if not combos:
        return Outcome("error", "没有展开出任何客户端命令行（调用点展开失败？）", json.dumps(payload, ensure_ascii=False)[:800])
    if payload.get("scenarios_without_command"):
        return Outcome("error", f"这些 TUI 场景的驱动没有产出客户端命令行：{payload['scenarios_without_command']}", "")
    rejected = [combo for combo in combos if combo["rejected"]]
    if rejected:
        lines = [
            f"{combo['scenario']}: {' '.join(combo['arguments'])} → 退出码 {combo['exited']}；{combo['output_head'][:240]}"
            for combo in rejected
        ]
        return Outcome("fail", f"{len(rejected)}/{len(combos)} 个客户端命令行被二进制拒收：{lines[0]}", "\n".join(lines))
    return Outcome("pass", f"{len(combos)} 个客户端命令行都能启动")


# ---------------------------------------------------------------------------
# 2) 上一版真实候选证据回放
# ---------------------------------------------------------------------------


def newest(paths: Sequence[Path]) -> Path | None:
    existing = [path for path in paths if path.is_file()]
    return max(existing, key=lambda item: tuple(int(part) for part in re.findall(r"\d+", item.stem)[-3:]), default=None)


def newest_target_files(tree: Path) -> tuple[Path | None, Path | None]:
    """被测树里最新版本的证据标签声明与候选期望清单（按文件名里的版本号取最大）。"""

    base = tree / "tools" / "official_client_capture"
    return (
        newest(sorted(base.glob("codex_upgrade_evidence_labels_*.json"))),
        newest(sorted(base.glob("candidate_rule_expectations_*.json"))),
    )


class PriorCandidateReplayRunner(Runner):
    """本树最新的标签声明与候选期望清单，对上一版真实候选证据回放 seal 预检与 assert-rules。

    证据位置由环境变量 ``REHEARSAL_PRIOR_CANDIDATE_EVIDENCE`` 给出（上一版候选 attempt 目录或 assertion-bundle 目录）；可选
    ``REHEARSAL_PRIOR_CANDIDATE_ROOTS``（完整原始证据根目录集合）、``REHEARSAL_PRIOR_OFFICIAL_EVIDENCE``、
    ``REHEARSAL_PRIOR_OFFICIAL_ROOTS``。没有证据记“未执行”，不用手造数据冒充。
    新版判据里的版本字面量与上一版证据对不上，自动用 ``--rewrite-version 新=旧`` 把清单里的字符串换成证据版本。
    """

    description = "registration-preview 回放上一版真实候选证据"

    def __init__(self, *, timeout_seconds: float = 1800.0) -> None:
        self.timeout_seconds = timeout_seconds

    def run(self, context: RunContext) -> Outcome:
        evidence = os.environ.get("REHEARSAL_PRIOR_CANDIDATE_EVIDENCE", "").strip()
        if not evidence:
            return Outcome("skip", "没有给上一版候选证据（环境变量 REHEARSAL_PRIOR_CANDIDATE_EVIDENCE）：候选侧 seal 预检/assert-rules 回放需要真实证据")
        if not Path(evidence).is_dir():
            return Outcome("error", f"REHEARSAL_PRIOR_CANDIDATE_EVIDENCE 不是目录：{evidence}")
        labels, profile = newest_target_files(context.tree)
        if labels is None or profile is None:
            return Outcome("error", "被测树里找不到证据标签声明或候选期望清单")
        label_version = json.loads(labels.read_text(encoding="utf-8")).get("codex_version")
        argv = [sys.executable, "-B", "-m", "tools.official_client_capture.tests.registration_preview", "preview",
                "--candidate-evidence", evidence, "--labels", str(labels), "--profile", str(profile)]
        prior_version = self._prior_version(Path(evidence))
        if prior_version and label_version and prior_version != label_version:
            argv += ["--rewrite-version", f"{label_version}={prior_version}"]
        for option, variable in (("--candidate-roots-dir", "REHEARSAL_PRIOR_CANDIDATE_ROOTS"),
                                 ("--official-evidence", "REHEARSAL_PRIOR_OFFICIAL_EVIDENCE"),
                                 ("--official-roots-dir", "REHEARSAL_PRIOR_OFFICIAL_ROOTS")):
            if os.environ.get(variable, "").strip():
                argv += [option, os.environ[variable].strip()]
        if not os.environ.get("REHEARSAL_PRIOR_OFFICIAL_EVIDENCE", "").strip():
            argv.append("--candidate-only")
        try:
            completed = subprocess.run(argv, cwd=str(context.tree), env=child_environment(context.tree), capture_output=True,
                                       text=True, timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            return Outcome("error", f"超时（{self.timeout_seconds:g} 秒）")
        output = completed.stdout + completed.stderr
        if completed.returncode == 0:
            return Outcome("pass", "标签与判据对上一版真实候选证据回放通过", _tail(output))
        if completed.returncode == 1:
            first = next((line.strip() for line in output.splitlines() if re.match(r"\s+\d+\. \[", line)), "")
            return Outcome("fail", f"回放报出缺陷：{first}", _tail(output, 6000))
        return Outcome("error", f"回放引擎退出码 {completed.returncode}", _tail(output))

    @staticmethod
    def _prior_version(evidence: Path) -> str | None:
        for candidate in (evidence / "evidence" / "assertion-bundle" / "capture-manifest.json",
                          evidence / "assertion-bundle" / "capture-manifest.json", evidence / "capture-manifest.json"):
            if candidate.is_file():
                return json.loads(candidate.read_text(encoding="utf-8")).get("codex_version")
        return None


# ---------------------------------------------------------------------------
# 3) 收尾环境变量下的测试隔离
# ---------------------------------------------------------------------------


def closeout_gate_environment(tree: Path) -> dict[str, str]:
    """收尾门禁注入、且会被测试单元继承的环境变量名：取 ``tools/codex_closeout.py`` 里 ``GATE_ENV_WHITELIST`` 中分类为 ``units`` 的项。

    白名单是收尾工具自己的登记表（每个注入变量必须先在这里分类，起跑前预检按它拒绝多余的注入），探针直接读它，不另写一套判定；
    语法树取字面量，不 import 被测树里的模块。``driver``（只给驱动读的入口环境文件）与 ``harmless``（无害的字节码开关）不会被单元继承，
    不需要注入。"""

    source = (tree / "tools" / "codex_closeout.py").read_text(encoding="utf-8")
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "GATE_ENV_WHITELIST" for target in node.targets):
            whitelist = ast.literal_eval(node.value)
            return {name: "" for name, entry in whitelist.items() if entry[0] == "units"}
    return {}


# 历史重放在“受管树副本”（只含受管树、驱动目录、tools/ci 与 codex_closeout）里跑，副本里没有仓库根的 Makefile、后端目录等，
# 需要它们的模块在副本上基线就红，没法拿来证明缺陷被报出。副本里只跑下面这批纯受管树即可运行的模块；
# 真实树上的一轮空跑仍跑完整清单。两份清单都含上一版实际红掉的三个模块。
CLOSEOUT_COPY_TREE_MODULES = (
    "test_ci_unit_executor_retry",
    "test_ci_background_validation_retry",
    "test_ci_retry_ledger_across_closeouts",
    "test_ci_unit_records",
    "test_ci_background_reuse",
    "test_ci_background_scope",
)


def closeout_env_runner(modules: Sequence[str] = CLOSEOUT_NESTED_EXECUTOR_MODULES, *,
                        copy_tree_modules: Sequence[str] = CLOSEOUT_COPY_TREE_MODULES,
                        timeout_seconds: float = 3600.0) -> "CloseoutEnvRunner":
    return CloseoutEnvRunner(modules, copy_tree_modules=copy_tree_modules, timeout_seconds=timeout_seconds)


class CloseoutEnvRunner(Runner):
    """把收尾门禁白名单里会被单元继承的环境变量全部注入，再跑会嵌套起执行器的最小测试全集。

    变量取值是工作目录下的占位路径（只要“设了”这件事；需要真实账本目录的用例经显式 env 字典自带）。测试包导入时会剔除
    ``GATE_EXECUTOR_ENV_NAMES``；剔除清单缺了新增变量时，嵌套执行器会继承它，对应单元就红，探针当场报出。
    """

    def __init__(self, modules: Sequence[str], *, copy_tree_modules: Sequence[str] | None = None,
                 timeout_seconds: float = 3600.0) -> None:
        self.modules = tuple(modules)
        self.copy_tree_modules = tuple(copy_tree_modules) if copy_tree_modules is not None else self.modules
        self.timeout_seconds = timeout_seconds
        self.description = "closeout-env " + " ".join(self.modules)

    def run(self, context: RunContext) -> Outcome:
        names = closeout_gate_environment(context.tree)
        if not names:
            return Outcome("error", "没有从 codex_closeout.GATE_ENV_WHITELIST 里取到任何 units 类环境变量名（源码结构变了？）")
        extra = {name: str(context.work / f"closeout-env-{name.lower()}") for name in names}
        in_copy = context.origin_tree is not None and context.origin_tree != context.tree
        modules = self.copy_tree_modules if in_copy else self.modules
        ids = [f"tools.official_client_capture.tests.{module}" for module in modules]
        outcome = UnittestRunner(ids, timeout_seconds=self.timeout_seconds, extra_env=extra).run(context)
        if outcome.verdict == "fail":
            outcome.detail = f"带收尾环境变量 {sorted(names)} 时：{outcome.detail}"
        return outcome


# ---------------------------------------------------------------------------
# 子进程入口
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="整链空跑补盲区探针的子进程入口")
    parser.add_argument("command", choices=["_startup_flags"])
    parser.add_argument("--binary-json", required=True)
    parser.add_argument("--window", type=float, default=6.0)
    parser.add_argument("--isolate", action="store_true")
    arguments = parser.parse_args(argv)
    result = startup_flags_main(json.loads(arguments.binary_json), window_seconds=arguments.window, isolate=arguments.isolate)
    print(STARTUP_RESULT_MARKER + json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
