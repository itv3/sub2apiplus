"""命令单元声明级承接 ``declared-inputs/v1`` 的执行器接线（步骤 5 工作包 WP3）。

被测代码：``tools/ci/unit_executor.py`` 新增的声明级部分（``ReadAuditor`` 的 declared 分支、``_gate_currents`` 并入合同探测、
``declared_contract_preflight`` 前置核验与 ``declared_contracts`` 凭证、``Recorder.write`` 带出凭证、``_executor_files`` 追加采样器、
``_run_gates_current`` 的探测回调与四个计数、``--declared-sample off|always|every:N``），以及 ``tools/ci/entry_gates.py`` 的两处极小新增
（离线开关＝True、declared Go 单元的清单 env 注入三个离线变量）。

测试分八组：

1. 端到端（真跑执行器进程，``python3 -c`` 脚本代替 go／pnpm／make，假 go／docker／node／pnpm／strace 放在 PATH 里）：方案 6.4 的正例
   （T4：改一个无关的 Claude 测试文件，声明级单元全部承接、只剩 rerun-only 重跑）与反例（T5～T8：改后端源码、改 docs/egress 基线、
   改 Makefile 里前端关键集合、换镜像 Id、缓存写热、node_modules 安装锁不一致）；
2. 记录与凭证（T9～T13）：写下的记录能被 ``unit_records`` 的消费者核验接受，凭证与前置核验失败的各种情形（含 ``go mod verify`` 自己的超时：
   卡住的核验按「没通过」处理、记录不可承接、命令照跑、整次运行只等一次）；
3. 逐次采样 β：启动前缀在 strace 外侧、覆盖失败分 failed／incomplete 两类（``DECLARED_INCOMPLETE_RULES``，集合内四条各自触发变 incomplete，
   集合外每一类变 failed，混合变 failed）、缺 strace 降级、每 N 次采样；采样器过滤器 raw 里只给评审看的诊断样例
   （coverage.unresolved_samples、顶层 detached_samples、顶层 detached_side_effects 计数）进证据的 review、不进字段闭合的 coverage，覆盖结论不变；
4. 汇总（T20）：登记数、审计通过数、消费者接受数、实际承接数四个数分开，每个被重跑的单元有原因与耗时；
5. ``_gate_currents`` 的合同探测、已登记集合、探测会话共享；
6. entry_gates 的两处新增：declared Go 单元的清单 env 带三个离线变量（非 Go 单元与 rerun-only 单元的 env 不变），
   ``declared_contract_problems`` 在门禁环境缺这三个变量时报告、齐全时不报告；
7. control 四链不变回归（T-C6～T-C12）：基线提交的执行器与新版并排（``git show`` 取出作独立模块），对同一批 control 夹具逐字比较——
   包括用假 unshare／strace 垫片走通的 ``wrap`` 成功路径、清单顶层 rerun_only_reasons 里混进 control 单元 ID 时它们仍被严格审计、
   基线提交读不到时本机跳过而 CI 失败；
8. 锁后复核之后审计器拿的必须是新事实（``AuditorFactsSyncTests``）：逐单元承接分支里常规审计器与只为声明级单元建的审计器都同步；B-09／B-10 分支里
   常规审计器与锁后复核不会同时存在（两个前提各有测试）。

测试里的路径、提交号、摘要都是合成值；本机没有 strace，逐次采样的 strace 输出由假 strace 按单元合成（经 ``-o '|…'`` 管道交给真实的过滤器进程）。
"""

from __future__ import annotations

import ast
import contextlib
import copy
import dataclasses
import functools
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from tools.ci import declared_sampler as ds
from tools.ci import entry_gates as eg
from tools.ci import unit_executor as ue
from tools.ci import unit_records as ur
from tools.official_client_capture.tests import test_ci_declared_inputs_entry as entry_tests
from tools.official_client_capture.tests.test_ci_unit_records import PASS, _commit, _config, _git, _write

REPO_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR = REPO_ROOT / "tools" / "ci" / "unit_executor.py"
# 本提交所基于的主线头（WP1c、H-1 合入之后）：基线对照取它的 unit_executor.py／entry_gates.py。这两个文件自 WP1b 的 6432c4c71 起到这里没有
# 变过，所以对照结果与取 6432c4c71 相同；读不到这个提交（浅克隆）时相关用例跳过。
BASELINE_COMMIT = "82edc945cc15fdf7f19d529b776f44a18fade5fd"
PY = sys.executable
SAMPLER_SHA = ds.file_sha256(ds.HERE)
STAMP = "2026-10-15T00:00:00Z"

# 命令单元读文件用的脚本：参数是相对工作目录的路径，逐个读一遍；退出码由 FAIL_UNITS 里的 sys.exit 控制。
READER = "import sys\nfor p in sys.argv[1:]:\n    open(p, 'rb').read()\n"
FAILING_READER = READER + "sys.exit(3)\n"

# ---------------------------------------------------------------------------
# 假工具链：PATH 里的 go／node／pnpm／docker／strace／launcher。状态放在 FAKE_STATE_DIR 下的文件里（环境变量值在两次运行间必须不变，
# 否则环境指纹变化会让全体失效），测试改状态就是改文件。
# ---------------------------------------------------------------------------

FAKE_GO = """#!@PY@
import json, os, sys, time
args = sys.argv[1:]
state = os.environ.get("FAKE_STATE_DIR", "")
if os.environ.get("FAKE_GO_LOG"):
    with open(os.environ["FAKE_GO_LOG"], "a") as handle:
        handle.write(json.dumps({"argv": args, "cwd": os.getcwd(), "GOFLAGS": os.environ.get("GOFLAGS"),
                                 "GOMODCACHE": os.environ.get("GOMODCACHE")}) + "\\n")
if args[:1] == ["version"]:
    print("go version go1.27.0 linux/arm64")
elif args[:2] == ["env", "-json"]:
    values = {"GOROOT": os.path.join(state, "goroot"), "GOTOOLDIR": os.path.join(state, "goroot", "pkg", "tool")}
    for key in args[2:]:
        values.setdefault(key, os.environ.get(key, ""))
    print(json.dumps(values))
elif args[:2] == ["mod", "verify"]:
    # go-verify-sleep 文件里写秒数：verify 先睡这么久（造「卡住的 go mod verify」，用来测它自己的超时）。
    if os.path.exists(os.path.join(state, "go-verify-sleep")):
        time.sleep(float(open(os.path.join(state, "go-verify-sleep")).read()))
    if os.path.exists(os.path.join(state, "go-verify-fail")):
        print("go: example.com/dep@v1.0.0: checksum mismatch")
        sys.exit(1)
    print("all modules verified")
else:
    sys.exit(2)
"""
FAKE_VERSION = "#!@PY@\nprint('@VERSION@')\n"
FAKE_DOCKER = """#!@PY@
import hashlib, os, sys
args = sys.argv[1:]
state = os.environ.get("FAKE_STATE_DIR", "")
if os.environ.get("FAKE_DOCKER_LOG"):
    with open(os.environ["FAKE_DOCKER_LOG"], "a") as handle:
        handle.write(" ".join(args) + "\\n")
if args[:4] == ["image", "inspect", "--format", "{{.Id}}"] and len(args) == 5:
    override = os.path.join(state, "image-" + args[4].replace("/", "_").replace(":", "_"))
    if os.path.exists(override):
        print(open(override).read().strip())
    else:
        print("sha256:" + hashlib.sha256(args[4].encode()).hexdigest())
    sys.exit(0)
sys.exit(1)
"""
# 假 strace：--version 照实回答；否则按单元（UNIT_EXECUTOR_UNIT）从 FAKE_STRACE_PLAN 取合成的 strace 行，经 -o 给的管道交给真实的过滤器进程，
# 记录自己看到的 argv 与环境（FAKE_LAUNCHER 由假启动前缀设置，strace 看得到它就说明启动前缀在 strace 外侧），再在原工作目录运行 -- 之后的命令。
FAKE_STRACE = """#!@PY@
import json, os, subprocess, sys
args = sys.argv[1:]
if args[:1] == ["--version"]:
    print("strace -- version 6.8")
    sys.exit(0)
pipe = args[args.index("-o") + 1]
command = args[args.index("--") + 1:]
unit = os.environ.get("UNIT_EXECUTOR_UNIT", "")
plan = json.load(open(os.environ["FAKE_STRACE_PLAN"])) if os.environ.get("FAKE_STRACE_PLAN") else {}
entry = plan.get(unit, {})
state = os.environ.get("FAKE_STATE_DIR", "")
with open(os.path.join(state, "strace-" + unit.replace(":", "_") + ".json"), "w") as handle:
    json.dump({"args": args, "FAKE_LAUNCHER": os.environ.get("FAKE_LAUNCHER"), "GOFLAGS": os.environ.get("GOFLAGS")}, handle)
lines = entry.get("lines", [])
data = ("\\n".join(lines) + "\\n").encode("utf-8") if lines else b""
if not entry.get("skip_pipe"):
    subprocess.run(["/bin/sh", "-c", pipe[1:]], input=data, check=True)
sys.exit(subprocess.run(command).returncode)
"""
FAKE_LAUNCHER = '#!/bin/sh\nFAKE_LAUNCHER=1 exec "$@"\n'


def install(path: Path, text: str, **substitutions: str) -> Path:
    body = text.replace("@PY@", PY)
    for key, value in substitutions.items():
        body = body.replace(f"@{key}@", value)
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


# ---------------------------------------------------------------------------
# 合成测试树与登记表
# ---------------------------------------------------------------------------

MAKEFILE = ("FRONTEND_CRITICAL_VITEST := \\\n\tsrc/a.spec.ts \\\n\tsrc/b.spec.ts\n"
            "test-frontend-critical:\n\t@pnpm --dir frontend exec vitest run $(FRONTEND_CRITICAL_VITEST)\n"
            "egress-spec-go-test:\n\t@echo egress-spec-go-test\n"
            "unrelated-target:\n\t@echo unrelated\n")
TREE_FILES = {
    ".gitignore": "node_modules/\n",
    "Makefile": MAKEFILE,
    "VERSION": "0.0.1\n",
    "backend/go.mod": entry_tests.GO_MOD,
    "backend/go.sum": "example.com/dep v1.0.0 h1:abc=\n",
    "backend/a.go": "package main\n",
    "backend/internal/repository/integration_harness_test.go": entry_tests.HARNESS,
    "docs/egress/foundation/sink-stats.md": "统计\n",
    "docs/other.md": "无关文档\n",
    "frontend/package.json": "{}\n",
    "frontend/pnpm-lock.yaml": "lockfileVersion: 9\n",
    "frontend/src/x.ts": "export const x = 1\n",
    "deploy/check.sh": "#!/bin/sh\nexit 0\n",
    "tools/official_client_capture/tests/test_claude_x.py": PASS,
    "tools/official_client_capture/tests/test_claude_y.py": PASS,
}
# 每个单元：工作目录（相对树根）、命令读的文件（相对工作目录）、类别、合同、单元 env。
UNIT_SPECS: dict[str, dict[str, Any]] = {
    "backend:go-test": {"cwd": "backend", "reads": ["go.mod", "a.go", "../docs/egress/foundation/sink-stats.md"], "kind": "go",
                        "contracts": ["toolchain:go", "cache:go-build-cache", "cache:go-mod-cache", "cache:go-tmp"],
                        "env": {"GOMAXPROCS": "2"}},
    "backend:integration": {"cwd": "backend", "reads": ["go.mod", "a.go", "../docs/egress/foundation/sink-stats.md"], "kind": "go",
                            "contracts": ["toolchain:go", "cache:go-build-cache", "cache:go-mod-cache", "service-image:postgres",
                                          "service-image:redis", "service-image:ryuk"], "env": {"GOMAXPROCS": "2", "CI": "true"}},
    "frontend:critical": {"cwd": ".", "reads": ["frontend/src/x.ts"], "kind": "frontend", "make_expansion": ["test-frontend-critical"],
                          "contracts": ["toolchain:node", "toolchain:pnpm", "toolchain:make", "cache:node-modules"], "env": {"NODE_ENV": "test"}},
    "egress-spec:egress-spec-go-test": {"cwd": ".", "reads": ["backend/a.go", "docs/egress/foundation/sink-stats.md"], "kind": "go",
                                        "make_expansion": ["egress-spec-go-test"],
                                        "contracts": ["toolchain:go", "toolchain:make", "cache:go-build-cache", "cache:go-mod-cache"],
                                        "env": {"GOMAXPROCS": "2"}},
}
RERUN_ONLY = {"deploy:check": "实时检查，重跑成本低于承接复杂度"}
DECLARED_IDS = tuple(UNIT_SPECS)
GO_UNITS = ("backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test")


def build_scope(unit_id: str, work: Path) -> dict[str, Any]:
    """一个单元的声明范围（形状取自调研 3.2；后端读 backend/ 与 docs/egress/，前端读 frontend/ 且 Makefile 由 make 展开合同覆盖）。"""

    spec = UNIT_SPECS[unit_id]
    contracts = sorted(spec["contracts"])
    ranges = [{"name": "backend", "include": ["backend/"], "exclude": []}, {"name": "egress-docs", "include": ["docs/egress/"], "exclude": []}]
    files = ["backend/go.mod", "backend/go.sum"]
    outside = [{"prefix": "/usr/local/go/", "class": "toolchain", "contract": "toolchain:go"},
               {"prefix": "{WORK}/go-mod-cache/", "class": "dependency-content", "contract": "cache:go-mod-cache"},
               {"prefix": "{WORK}/go-build-cache/", "class": "performance-cache", "contract": "cache:go-build-cache"},
               {"prefix": "{WORK}/go-tmp/", "class": "scratch"}, {"prefix": "/usr/lib/", "class": "system"}]
    repo: dict[str, Any] = {"list_ok": [], "repo_ignored": [], "head": False}
    if spec["kind"] == "frontend":
        ranges = [{"name": "frontend", "include": ["frontend/"], "exclude": []}]
        files = ["frontend/pnpm-lock.yaml", "frontend/package.json"]
        outside = [{"prefix": "/opt/node-v20/", "class": "toolchain", "contract": "toolchain:node"},
                   {"prefix": "{TMP}/", "class": "scratch"}, {"prefix": "/usr/lib/", "class": "system"}]
        repo["installed_dirs"] = [{"prefix": "frontend/node_modules/", "contract": "cache:node-modules"}]
    repo.update(ranges=ranges, files=files)
    return {"schema_version": eg.DECLARED_SCOPE_SCHEMA, "repo": repo, "make_expansion": list(spec.get("make_expansion", [])), "outside": outside,
            "contracts": contracts, "network": "loopback-only"}


def build_evidence(unit_id: str, *, tree: Path, work: Path, home: Path, tmp: Path, launcher: list[str], command: list[str]) -> dict[str, Any]:
    """登记表里的采样证据（declared-sample/v1）：读的文件都落在声明范围内，覆盖检查 C1～C7 全部通过。"""

    spec = UNIT_SPECS[unit_id]
    cwd_rel = spec["cwd"]
    base = Path(spec["cwd"])
    repo_rows = []
    for item in spec["reads"]:
        relative = os.path.normpath(str(base / item))
        repo_rows.append([f"{{TREE}}/{relative}", ["read"], True])
    outside: list[list[Any]] = []
    if spec["kind"] == "go":
        outside += [["/usr/local/go/src/", ["read", "stat"], 3], ["{WORK}/go-build-cache/ab/", ["read", "write"], 2],
                    ["{WORK}/go-mod-cache/github.com/", ["read", "stat"], 1]]
    else:
        outside += [["/opt/node-v20/lib/", ["read"], 2]]
        repo_rows.append(["{TREE}/frontend/node_modules/eslint/index.js", ["read"], False])
    if spec.get("make_expansion"):
        repo_rows.append(["{TREE}/Makefile", ["read"], True])
    argv = list(command)
    return {
        "schema_version": ur.DECLARED_SAMPLE_SCHEMA, "unit_id": unit_id,
        "command": {"launcher": list(launcher), "argv": argv, "cwd_rel": cwd_rel, "sha256": ur.declared_command_sha256(argv, cwd_rel)},
        "sampled": {"commit": "c" * 40, "utc": STAMP, "kernel": "6.8.0", "strace": "6.8", "sampler_sha256": SAMPLER_SHA,
                    "runs": [{"cache": "cold", "exit_code": 0, "lines": 100, "seconds": 2}]},
        "aliases": {"TREE": str(tree), "WORK": str(work), "HOME": str(home), "TMP": str(tmp)},
        "coverage": {"stream_complete": True, "unresolved": 0, "unresolved_reasons": {}, "unfinished_calls": 0,
                     "unresolved_process_events": 0, "lines": 100},
        "repo_accesses": sorted(repo_rows), "outside_accesses": sorted(outside), "git_commands": [], "network": [], "exit_code": 0,
        "valid": True, "invalid_reasons": [],
    }


def open_line(cwd: str, path: str, flags: str = "O_RDONLY|O_CLOEXEC", pid: int = 4242) -> str:
    """strace -f -y 格式的一条 openat 记录（与审计器、采样器测试同一写法）。"""

    return f'{pid} openat(AT_FDCWD<{cwd}>, "{path}", {flags}) = 3<{path}>'


def write_line(cwd: str, path: str) -> str:
    return open_line(cwd, path, "O_WRONLY|O_CREAT|O_TRUNC|O_CLOEXEC, 0666")


@dataclasses.dataclass
class Run:
    """一次 run-gates 运行的结果。"""

    code: int
    summary: dict[str, Any]
    manifest: dict[str, Any]
    stderr: str
    out: Path

    @property
    def executed(self) -> set[str]:
        return {entry["unit_id"] for entry in self.manifest.get("units", []) if entry["disposition"] == "executed"}

    @property
    def inherited(self) -> set[str]:
        return {entry["unit_id"] for entry in self.manifest.get("units", []) if entry["disposition"] == "inherited"}


class World:
    """合成世界：临时 git 仓库（测试树）＋假工具链＋登记表与证据＋门禁清单；``run`` 真跑执行器进程（每次一个新的输出目录，记录库跨运行保留）。"""

    def __init__(self, case: unittest.TestCase, *, launcher: Any = (), registry: bool = True, failing: tuple[str, ...] = (),
                 scope_hook: Any = None) -> None:
        directory = tempfile.TemporaryDirectory()
        case.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.failing = set(failing)
        self.tree = self.root / "tree"
        self.work = self.root / "work"
        self.home = self.root / "home"
        self.tmp = self.root / "tmp"
        self.state = self.root / "state-files"
        self.bin = self.root / "bin"
        for path in (self.tree, self.work, self.home, self.tmp, self.state, self.bin, self.state / "goroot" / "pkg" / "tool", self.state / "goroot" / "bin"):
            path.mkdir(parents=True, exist_ok=True)
        for name in ("go-build-cache", "go-mod-cache", "go-tmp"):
            (self.work / name).mkdir()
        (self.state / "goroot" / "VERSION").write_text("go1.27.0\n", encoding="utf-8")
        (self.state / "goroot" / "bin" / "go").write_text("goroot-go", encoding="utf-8")
        for tool in eg.DECLARED_GO_TOOLS:
            (self.state / "goroot" / "pkg" / "tool" / tool).write_text(tool, encoding="utf-8")
        install(self.bin / "go", FAKE_GO)
        for name, version in (("node", "v20.19.0"), ("pnpm", "10.4.1")):
            install(self.bin / name, FAKE_VERSION, VERSION=version)
        install(self.bin / "docker", FAKE_DOCKER)
        install(self.bin / "strace", FAKE_STRACE)
        install(self.bin / "fake-launcher", FAKE_LAUNCHER)
        # 启动前缀：传 "fake" 用假启动前缀（先设 FAKE_LAUNCHER=1 再 exec 后面的命令，与 ARM64 的 unshare 前缀同一个位置关系）。
        self.launcher = [str(self.bin / "fake-launcher")] if launcher == "fake" else list(launcher)
        self.strace_plan = self.root / "strace-plan.json"
        self.strace_plan.write_text("{}", encoding="utf-8")
        self.go_log, self.docker_log = self.root / "go.log", self.root / "docker.log"
        self.environment = {key: value for key, value in os.environ.items() if not key.startswith("UNIT_EXECUTOR_")}
        self.environment.update({
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(REPO_ROOT), "PATH": f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}",
            "HOME": str(self.home), "TMPDIR": str(self.tmp), "GOCACHE": str(self.work / "go-build-cache"),
            "GOMODCACHE": str(self.work / "go-mod-cache"), "GOTMPDIR": str(self.work / "go-tmp"), "FAKE_STATE_DIR": str(self.state),
            "FAKE_STRACE_PLAN": str(self.strace_plan), "FAKE_GO_LOG": str(self.go_log), "FAKE_DOCKER_LOG": str(self.docker_log)})
        # 测试树：先有一个空的基准提交（git 历史合同的测试用它当「固定的历史提交」），再把文件、登记表、证据一起提交。
        _git(self.tree, "init", "-q")
        _git(self.tree, "commit", "-q", "--allow-empty", "-m", "基准")
        self.base_commit = _git(self.tree, "rev-parse", "HEAD").strip()
        _write(self.tree, {path: text for path, text in TREE_FILES.items()})
        self.commands: dict[str, list[str]] = {}
        self.units: list[dict[str, Any]] = []
        for unit_id, spec in UNIT_SPECS.items():
            script = FAILING_READER if unit_id in self.failing else READER
            # 声明了 make 展开合同的单元，命令必须是 make（入口自检的要求，步骤 5 WP3b）：假 PATH 里的 pnpm 只打印版本，Makefile 里的目标也只是 echo。
            self.commands[unit_id] = (["make", "--no-print-directory", spec["make_expansion"][0]] if spec.get("make_expansion")
                                      else [PY, "-c", script, *spec["reads"]])
        for unit_id in RERUN_ONLY:
            self.commands[unit_id] = ["/bin/sh", "deploy/check.sh"]
        self.registry_units: dict[str, Any] = {}
        if registry:
            self._write_registry(scope_hook)
        (self.tree / "frontend" / "node_modules" / ".pnpm").mkdir(parents=True)
        (self.tree / "frontend" / "node_modules" / ".pnpm" / "lock.yaml").write_text(TREE_FILES["frontend/pnpm-lock.yaml"], encoding="utf-8")
        _commit(self.tree, "初始")
        self.runs = 0
        self.store = self.root / "store"
        self.config = _config(self.root)
        self.manifest_path = self.write_manifest()

    # -- 登记表与清单 ------------------------------------------------------------------------------------------------

    def _write_registry(self, scope_hook: Any) -> None:
        evidence_dir = self.tree / eg.DECLARED_SAMPLES_DIR
        evidence_dir.mkdir(parents=True)
        for unit_id in DECLARED_IDS:
            scope = build_scope(unit_id, self.work)
            if scope_hook is not None:
                scope_hook(unit_id, scope, self.base_commit)
            command = self.commands[unit_id]
            document = build_evidence(unit_id, tree=self.tree, work=self.work, home=self.home, tmp=self.tmp, launcher=self.launcher, command=command)
            text = ds.dumps_document(document)
            relative = f"{eg.DECLARED_SAMPLES_DIR}/{unit_id.replace(':', '-')}.sample.json"
            (self.tree / relative).write_text(text, encoding="utf-8")
            cwd_rel = document["command"]["cwd_rel"]
            self.registry_units[unit_id] = {
                "status": "declared", "scope": scope,
                "sample": {"evidence": relative, "evidence_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                           "command_sha256": ur.declared_command_sha256(command, cwd_rel),
                           "launcher_sha256": eg.declared_launcher_sha256(self.launcher), "sampler_sha256": SAMPLER_SHA}}
        for unit_id, reason in RERUN_ONLY.items():
            self.registry_units[unit_id] = {"status": "rerun-only", "reason": reason}
        (self.tree / eg.DECLARED_REGISTRY_PATH).write_text(
            json.dumps({"schema_version": eg.DECLARED_REGISTRY_SCHEMA, "units": self.registry_units}, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8")

    def write_manifest(self, *, audit_policy: dict[str, Any] | None = None, env_overrides: dict[str, dict[str, str]] | None = None,
                       plain_units: tuple[str, ...] = ()) -> Path:
        """门禁清单：每个单元的旧式声明（整仓四段＋HEAD）经 apply_declared_inputs 换成声明级声明（有登记表时）。
        ``env_overrides`` 在应用登记表之前往指定单元的 env 里加键（造「已有同名变量」的冲突）；``plain_units`` 是不在登记表里的普通命令单元。"""

        units, gates = [], []
        for unit_id in [*UNIT_SPECS, *RERUN_ONLY, *plain_units]:
            spec = UNIT_SPECS.get(unit_id, {"cwd": ".", "env": {}})
            cwd = str(self.tree / spec["cwd"]) if spec["cwd"] != "." else str(self.tree)
            command = self.commands.get(unit_id) or ["/bin/sh", "deploy/check.sh"]
            item: dict[str, Any] = {"unit_id": unit_id, "argv": [*self.launcher, *command], "cwd": cwd, "cores": 1, "memory_mb": 128,
                                    "timeout_seconds": 120, "weight": 1.0, "inputs": eg.command_inputs([]), "reservation_restartable": True}
            env = {**spec["env"], **(env_overrides or {}).get(unit_id, {})}
            if env:
                item["env"] = env
            units.append(item)
            gates.append({"gate_id": unit_id, "units": [unit_id]})
        manifest: dict[str, Any] = {"schema_version": ue.GATES_SCHEMA, "profile": "full-gates", "test_groups": [], "units": units, "gates": gates,
                                    "scheduling": {"example": 1}}
        if audit_policy is not None:
            manifest["audit_policy"] = audit_policy
        eg.apply_declared_inputs(manifest, self.tree, launcher=self.launcher)
        path = self.root / "gates.json"
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        return path

    # -- 树与状态 ----------------------------------------------------------------------------------------------------

    def commit(self, files: dict[str, str], message: str = "改动") -> str:
        _write(self.tree, files)
        _commit(self.tree, message)
        return _git(self.tree, "rev-parse", "HEAD").strip()

    def set_image_id(self, reference: str, image_id: str) -> None:
        self.image_override_file(reference).write_text(image_id + "\n", encoding="utf-8")

    def flag(self, name: str, on: bool = True) -> None:
        path = self.state / name
        if on:
            path.write_text("1", encoding="utf-8")
        else:
            path.unlink(missing_ok=True)

    def flag_text(self, name: str, text: str | None) -> None:
        """带内容的状态文件（假工具读它的内容）：text 为 None 就删掉。"""

        path = self.state / name
        if text is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(text, encoding="utf-8")

    def image_override_file(self, reference: str) -> Path:
        """假 docker 读的「这个镜像引用的 Id 被换成了什么」文件（World.set_image_id 写的就是它）。"""

        return self.state / ("image-" + reference.replace("/", "_").replace(":", "_"))

    def set_strace_plan(self, plan: dict[str, Any]) -> None:
        self.strace_plan.write_text(json.dumps(plan), encoding="utf-8")

    def cwd_of(self, unit_id: str) -> str:
        spec = UNIT_SPECS.get(unit_id, {"cwd": "."})
        return str(self.tree / spec["cwd"]) if spec["cwd"] != "." else str(self.tree)

    def good_lines(self, unit_id: str) -> list[str]:
        """β 的假 strace 输出：命令真实会读的文件（落在声明范围内）＋工具链＋缓存读写，覆盖检查全部通过。"""

        spec = UNIT_SPECS[unit_id]
        cwd = self.cwd_of(unit_id)
        lines = [open_line(cwd, str(self.tree / os.path.normpath(str(Path(spec["cwd"]) / item)))) for item in spec["reads"]]
        if spec["kind"] == "go":
            lines += [open_line(cwd, "/usr/local/go/src/fmt/print.go"), open_line(cwd, f"{self.work}/go-build-cache/ab/abcd-d"),
                      write_line(cwd, f"{self.work}/go-build-cache/ab/new-a")]
        else:
            lines += [open_line(cwd, "/opt/node-v20/lib/node.js"), open_line(cwd, f"{self.tree}/frontend/node_modules/eslint/index.js")]
        if spec.get("make_expansion"):
            lines.append(open_line(cwd, f"{self.tree}/Makefile"))
        return lines

    def plan_good_strace(self, **overrides: list[str]) -> dict[str, Any]:
        """每个声明级单元一份合格的合成 strace 输出（overrides 按单元换掉某几个单元的行）并写进假 strace 的计划文件。"""

        plan = {unit_id: {"lines": overrides.get(unit_id, self.good_lines(unit_id))} for unit_id in DECLARED_IDS}
        self.set_strace_plan(plan)
        return plan

    # -- 运行 --------------------------------------------------------------------------------------------------------

    def run(self, *extra: str, mode: str = "re-execute", env: dict[str, str] | None = None, expect_summary: bool = True,
            bootstrap: str | None = None) -> Run:
        """真跑一次 run-gates。``bootstrap`` 是一段在执行器模块 import 之后、``main`` 之前运行的 Python 代码（里面的 ``ue`` 就是执行器模块）：
        用来在子进程里调小模块常量、给模块级函数挂钩子——这些改不了命令行，又要走完整的调度流程。"""

        self.runs += 1
        out = self.root / f"out-{self.runs}"
        arguments = ["run-gates", "--manifest", str(self.manifest_path), "--config", str(self.config),
                     "--weights", str(self.root / "none.json"), "--durations", str(self.root / "none.json"), "--parallel", "4", "--cores", "4",
                     "--state-dir", str(self.root / "state"), "--out-dir", str(out), "--shared-caches", "off", "--record-store", str(self.store),
                     "--mode", mode, *extra]
        if bootstrap is None:
            command = [PY, str(EXECUTOR), *arguments]
        else:
            program = ("import os, sys\nsys.path.insert(0, os.environ['PYTHONPATH'])\nimport tools.ci.unit_executor as ue\n"
                       + bootstrap + "\nsys.exit(ue.main(sys.argv[1:]))\n")
            command = [PY, "-c", program, *arguments]
        completed = subprocess.run(command, cwd=self.tree, capture_output=True, text=True, timeout=600, env={**self.environment, **(env or {})})
        summary_path, manifest_path = out / "summary.json", out / "unit-manifest.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.is_file() else {}
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
        if expect_summary and not summary:
            raise AssertionError(f"执行器没有写出汇总（退出码 {completed.returncode}）：{completed.stderr[-3000:]}")
        return Run(completed.returncode, summary, manifest, completed.stderr, out)

    # -- 记录库 ------------------------------------------------------------------------------------------------------

    def records(self, unit_id: str, *, kind: str = "formal") -> list[tuple[Path, dict[str, Any]]]:
        found = [(path, record) for path, record in ur.RecordStore(self.store).candidates(unit_id) if record and record.get("kind") == kind]
        return sorted(found, key=lambda item: (item[1]["completed_at_utc"], item[0].name))

    def latest(self, unit_id: str) -> dict[str, Any]:
        return self.records(unit_id)[-1][1]

    def record_of(self, run: Run, unit_id: str) -> dict[str, Any]:
        """某次运行为某单元写下的那条正式记录（按运行 ID 取，同一秒内的多次运行不会串）。"""

        return next(record for _path, record in self.records(unit_id) if record["run"]["run_id"] == run.summary["run_id"])


# ---------------------------------------------------------------------------
# 第一组：端到端（真跑执行器进程）
# ---------------------------------------------------------------------------

UNRELATED_CLAUDE_TEST = "tools/official_client_capture/tests/test_claude_x.py"
OFFLINE = {"GOFLAGS": "-mod=readonly", "GOPROXY": "off", "GOTOOLCHAIN": "local"}


def current_for(record: dict[str, Any]) -> Any:
    """按一条记录自己封存的规格与输入造「当前事实」（环境、策略、执行器也取记录里的）：用来问消费者「这条记录现在可不可以承接」。"""

    return ur.Current(record["unit_id"], "command", record["spec"], record["spec_sha256"], record["inputs"], record["inputs_sha256"], True)


def facts_for(record: dict[str, Any], **overrides: Any) -> Any:
    values = dict(policy_sha256=record["policy_sha256"], environment=record["environment"], environment_sha256=record["environment_sha256"],
                  executor=record["executor"], max_age_hours=168.0, now=ur.parse_utc(record["completed_at_utc"]) + 5)
    values.update(overrides)
    return ur.RunFacts(**values)


def assert_go_mod_verify_ran_in_the_module_root(case: unittest.TestCase, world: "World", log_line: str) -> None:
    """假 go 的日志行（``FAKE_GO_LOG`` 里 ``mod verify`` 那一行）记着它被调用时的 ``os.getcwd()``：``go mod verify`` 必须在含 ``go.mod`` 的模块根目录
    （测试树的 backend/）里跑——真实 go 在没有 go.mod 的目录（例如测试树根）里 ``go mod verify`` 会失败，三个 Go 单元的记录就全部不可承接
    （失败关闭，丢的是承接收益）。独立验收 #5 一般 6：此前没有任何用例断言工作目录，把 ``cwd=str(cwd)`` 改成 ``cwd=None`` 所有测试仍全绿。"""

    entry = json.loads(log_line)
    case.assertEqual(entry["argv"], ["mod", "verify"], log_line)
    cwd = Path(entry["cwd"])
    case.assertEqual(cwd, world.tree / "backend", f"go mod verify 的工作目录应是测试树里的 backend/（go.mod 所在目录），实际 {cwd}")
    case.assertTrue((cwd / "go.mod").is_file(), f"go mod verify 的工作目录里必须有 go.mod：{cwd}")
    case.assertNotEqual(cwd, world.tree, "不能在测试树根（没有 go.mod）里跑")


class EndToEndInheritanceTests(unittest.TestCase):
    """方案 6.4 的正例与反例（T4～T8、T10、T11）：Run1 重新执行全集，改动并提交后 Run2 全集通过模式只重跑受影响的单元。"""

    def scenario(self, mutate: Any, **world_options: Any) -> tuple[World, Run, Run]:
        world = World(self, **world_options)
        first = world.run()
        self.assertEqual(first.code, 0, first.stderr[-2000:])
        mutate(world)
        second = world.run(mode="full-set-pass")
        self.assertEqual(second.code, 0, second.stderr[-2000:])
        self.assertEqual(second.summary["unit_manifest"]["self_check"], "passed", second.summary["unit_manifest"])
        return world, first, second

    def test_t4_changing_an_unrelated_claude_test_file_inherits_every_declared_unit(self) -> None:
        world, first, second = self.scenario(lambda w: w.commit({UNRELATED_CLAUDE_TEST: PASS + "# 无关改动\n"}))
        self.assertEqual(first.executed, set(DECLARED_IDS) | set(RERUN_ONLY), "Run1 重新执行全集")
        self.assertEqual(second.inherited, set(DECLARED_IDS), "声明级单元全部承接")
        self.assertEqual(second.executed, set(RERUN_ONLY), "executed 只剩 rerun-only 的")
        reasons = {entry["unit_id"]: entry for entry in second.manifest["units"]}
        self.assertEqual(reasons["deploy:check"]["reasons"], [RERUN_ONLY["deploy:check"]], "rerun-only 的重跑原因就是登记的原因")
        # 承接消费者对清单的重验也通过（历史重放，不读外部事实）。
        self.assertEqual(ur.verify_manifest(second.manifest, store=ur.RecordStore(world.store), historical=True), [])
        # 对照：没有登记表的同一流程，改同一个文件后全部重跑（整仓四段与 HEAD 都变了）。
        _legacy, _first, legacy_second = self.scenario(lambda w: w.commit({UNRELATED_CLAUDE_TEST: PASS + "# 无关改动\n"}), registry=False)
        self.assertEqual(legacy_second.inherited, set(), "无登记表：旧声明，改任何文件都全部重跑")
        self.assertTrue(all("head" in "；".join(entry["reasons"]) for entry in legacy_second.manifest["units"]),
                        [entry["reasons"] for entry in legacy_second.manifest["units"]])
        self.assertNotIn("declared_inputs", legacy_second.summary, "没有登记表：汇总里没有声明级的键")

    def test_the_records_carry_the_frozen_declared_shape_and_pass_the_consumer(self) -> None:
        world = World(self)
        first = world.run()
        store = ur.RecordStore(world.store)
        for unit_id in DECLARED_IDS:
            record = world.record_of(first, unit_id)
            audit = record["read_audit"]
            registry = world.registry_units[unit_id]["sample"]
            self.assertEqual((audit["coverage_scope"], audit["coverage_complete"], audit["status"], audit["undeclared_count"], audit["undeclared"]),
                             ("declared-inputs/v1", True, "passed", 0, []), unit_id)
            self.assertEqual(audit["sample_origin"], "registry")
            self.assertEqual(audit["inputs_sha256"], record["inputs_sha256"])
            self.assertEqual(audit["repo_root"], str(world.tree))
            self.assertEqual(audit["trace"]["sha256"], registry["evidence_sha256"], "α'：封存的就是登记表证据，摘要等于登记摘要")
            self.assertTrue(Path(audit["trace"]["stored"]).is_file())
            self.assertEqual(ur.file_sha256(store.log_path(registry["evidence_sha256"])), registry["evidence_sha256"], "证据按内容摘要封存进记录库")
            scope_entry = next(entry for entry in record["inputs"] if entry["name"] == "declared-inputs-scope")
            self.assertEqual(audit["sample_binding"], {"unit_id": unit_id, "command_sha256": registry["command_sha256"],
                                                      "scope_sha256": ur.sha256_json(scope_entry["detail"]["scope"]),
                                                      "evidence_sha256": registry["evidence_sha256"]})
            self.assertTrue({"repo_paths", "outside_paths", "classes"} <= set(audit["coverage"]))
            self.assertNotIn("head", [entry["name"] for entry in record["inputs"]], "声明级单元的输入里没有 HEAD")
            self.assertTrue(any(entry["name"].startswith(("contract:toolchain:", "contract:cache:")) for entry in record["inputs"]), "合同条目并进了输入")
            self.assertIn("declared_sampler.py", record["executor"]["files"], "含声明级单元的运行，采样器字节进执行器版本")
            path = store.record_path(unit_id, record["record_sha256"])
            self.assertEqual(ur.check_record(store, path, record, current_for(record), facts_for(record)), [], f"{unit_id}：消费者核验接受")
        self.assertEqual(first.summary["declared_inputs"]["consumer_accepted"], len(DECLARED_IDS), "执行器自己也按承接用的核验检查过刚写下的记录")
        deploy = world.record_of(first, "deploy:check")
        self.assertNotIn("declared_contracts", deploy, "rerun-only 记录没有声明级的键")
        self.assertNotIn("read_audit", deploy)
        self.assertIs(deploy["inheritable"], False)
        self.assertEqual(deploy["not_inheritable_reason"], RERUN_ONLY["deploy:check"])

    def test_t5_changing_backend_source_reruns_backend_and_egress_spec_but_not_frontend(self) -> None:
        _world, _first, second = self.scenario(lambda w: w.commit({"backend/a.go": "package main\n// 改\n"}))
        self.assertEqual(second.executed, {"backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test", "deploy:check"})
        self.assertEqual(second.inherited, {"frontend:critical"})

    def test_t6_a_docs_egress_baseline_change_reruns_only_the_units_that_read_it(self) -> None:
        _world, _first, second = self.scenario(lambda w: w.commit({"docs/egress/foundation/sink-stats.md": "统计2\n"}))
        self.assertEqual(second.executed, {"backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test", "deploy:check"})
        self.assertEqual(second.inherited, {"frontend:critical"})
        _world, _first, unrelated = self.scenario(lambda w: w.commit({"docs/other.md": "无关文档，改了\n"}))
        self.assertEqual(unrelated.inherited, set(DECLARED_IDS), "无关的 docs 改动：backend 也承接")

    def test_t7_changing_the_frontend_critical_set_in_the_makefile_reruns_only_frontend(self) -> None:
        swapped = MAKEFILE.replace("\tsrc/a.spec.ts \\\n\tsrc/b.spec.ts", "\tsrc/b.spec.ts \\\n\tsrc/a.spec.ts")
        self.assertNotEqual(swapped, MAKEFILE)
        _world, _first, second = self.scenario(lambda w: w.commit({"Makefile": swapped}))
        self.assertEqual(second.executed, {"frontend:critical", "deploy:check"})
        self.assertEqual(second.inherited, {"backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test"},
                         "egress-spec-go-test 的 make 展开不含这个变量：承接")
        reasons = next(entry for entry in second.manifest["units"] if entry["unit_id"] == "frontend:critical")["reasons"]
        self.assertIn("contract:make-expansion:test-frontend-critical", "；".join(reasons))
        # Makefile 里与这些单元无关的目标改动：全部承接（对比「整个 Makefile 当输入」的做法）。
        _world, _first, unrelated = self.scenario(lambda w: w.commit({"Makefile": MAKEFILE.replace("echo unrelated", "echo changed")}))
        self.assertEqual(unrelated.inherited, set(DECLARED_IDS))

    def test_t8_a_different_image_id_reruns_only_the_integration_unit(self) -> None:
        def swap(world: World) -> None:
            world.set_image_id("postgres:18.1-alpine3.23", "sha256:" + "9" * 64)
            world.commit({"docs/other.md": "x\n"})

        _world, _first, second = self.scenario(swap)
        self.assertEqual(second.executed, {"backend:integration", "deploy:check"})
        reasons = next(entry for entry in second.manifest["units"] if entry["unit_id"] == "backend:integration")["reasons"]
        self.assertIn("contract:service-image:postgres", "；".join(reasons))

    def test_t10_writing_the_cache_hot_between_runs_does_not_cost_any_inheritance(self) -> None:
        def heat(world: World) -> None:
            shard = world.work / "go-build-cache" / "ab"
            shard.mkdir()
            (shard / "abcd-d").write_text("编译产物", encoding="utf-8")
            (world.work / "go-tmp" / "go-build123").mkdir()
            os.utime(world.work / "go-build-cache", (time.time() + 5, time.time() + 5))
            world.commit({"docs/other.md": "x\n"})

        _world, _first, second = self.scenario(heat)
        self.assertEqual(second.inherited, set(DECLARED_IDS))
        self.assertEqual(second.executed, set(RERUN_ONLY))

    def test_t11_an_install_lock_that_differs_from_the_lockfile_makes_the_record_uninheritable(self) -> None:
        world = World(self)
        first = world.run()
        self.assertTrue(world.record_of(first, "frontend:critical")["inheritable"])
        # node_modules 里的安装锁副本被换成别的，同时前端源码变了（frontend:critical 要重跑，前置核验才轮得到它）。
        (world.tree / "frontend/node_modules/.pnpm/lock.yaml").write_text("lockfileVersion: 8\n", encoding="utf-8")
        world.commit({"frontend/src/x.ts": "export const x = 2\n"})
        second = world.run(mode="full-set-pass")
        self.assertEqual(second.code, 0, second.stderr[-1500:])
        self.assertEqual(second.executed, {"frontend:critical", "deploy:check"})
        record = world.record_of(second, "frontend:critical")
        self.assertIs(record["passed"], True, "命令本身的结论照常")
        self.assertIs(record["inheritable"], False)
        self.assertIn("declared-contract-preflight", record["not_inheritable_reason"])
        self.assertIn("安装锁副本", record["not_inheritable_reason"])
        credential = next(item for item in record["declared_contracts"] if item["id"] == "node-modules")
        self.assertIs(credential["verified_before_run"], False)
        self.assertNotEqual(credential["observed"]["installed_lock_sha256"], credential["observed"]["lock_file_sha256"])
        third = world.run(mode="full-set-pass")
        self.assertEqual(third.executed, {"frontend:critical", "deploy:check"}, "Run3 不承接：来源记录不可承接")
        self.assertEqual(third.inherited, {"backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test"})
        # 装对之后新记录可承接，再下一轮就承接了。
        (world.tree / "frontend/node_modules/.pnpm/lock.yaml").write_text(TREE_FILES["frontend/pnpm-lock.yaml"], encoding="utf-8")
        fourth = world.run(mode="full-set-pass")
        self.assertEqual(fourth.executed, {"frontend:critical", "deploy:check"})
        self.assertTrue(world.record_of(fourth, "frontend:critical")["inheritable"])
        fifth = world.run(mode="full-set-pass")
        self.assertEqual(fifth.inherited, set(DECLARED_IDS))


# ---------------------------------------------------------------------------
# 第二组：合同条目、前置核验与凭证（T9～T14）
# ---------------------------------------------------------------------------


class ContractAndPreflightTests(unittest.TestCase):
    def entries_of(self, record: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {entry["name"]: entry for entry in record["inputs"]}

    def test_go_units_run_offline_and_the_probe_sees_the_unit_values(self) -> None:
        world = World(self)
        manifest = json.loads(world.manifest_path.read_text(encoding="utf-8"))
        units = {unit["unit_id"]: unit for unit in manifest["units"]}
        for unit_id in GO_UNITS:
            self.assertEqual({key: units[unit_id]["env"][key] for key in OFFLINE}, OFFLINE, f"{unit_id}：离线变量写进了清单 env")
        self.assertEqual(units["backend:go-test"]["env"]["GOMAXPROCS"], "2", "原有的 env 键不动")
        self.assertEqual(units["frontend:critical"]["env"], {"NODE_ENV": "test"}, "非 Go 的 declared 单元 env 与改前逐字相同")
        self.assertNotIn("env", units["deploy:check"], "rerun-only 单元 env 与改前相同（原来就没有）")
        first = world.run()
        record = world.record_of(first, "backend:go-test")
        self.assertEqual({key: record["spec"]["env"][key] for key in OFFLINE}, OFFLINE, "离线变量随单元规格进记录")
        go = self.entries_of(record)["contract:toolchain:go"]["detail"]
        self.assertEqual(go["requires"], {"env_equals": OFFLINE})
        self.assertEqual(go["requirements"], {"met": True, "unmet": {}}, "探测环境叠上了单元清单 env 里的 go 变量：要求满足")
        self.assertEqual({key: go["env"][key] for key in OFFLINE}, OFFLINE)
        self.assertEqual(ur.entries_sha256(record["inputs"]), record["inputs_sha256"])
        frontend = world.record_of(first, "frontend:critical")
        self.assertNotIn("contract:toolchain:go", self.entries_of(frontend), "前端单元不声明 go 合同")

    def test_a_manifest_that_lacks_an_offline_variable_makes_that_record_uninheritable(self) -> None:
        world = World(self)
        manifest = json.loads(world.manifest_path.read_text(encoding="utf-8"))
        next(unit for unit in manifest["units"] if unit["unit_id"] == "backend:go-test")["env"].pop("GOFLAGS")
        world.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        first = world.run()
        self.assertEqual(first.code, 0, "命令本身的结论照常，整次运行不失败")
        record = world.record_of(first, "backend:go-test")
        self.assertIs(record["inheritable"], False)
        self.assertIn("离线", record["not_inheritable_reason"])
        self.assertIn("GOFLAGS", record["not_inheritable_reason"])
        self.assertTrue(world.record_of(first, "backend:integration")["inheritable"], "其它单元不受影响")
        second = world.run(mode="full-set-pass")
        self.assertIn("backend:go-test", second.executed, "来源记录不可承接，下一轮重跑")
        self.assertIn("backend:integration", second.inherited)

    def test_cache_roots_inside_the_tree_make_the_go_records_uninheritable(self) -> None:
        world = World(self)
        first = world.run(env={"GOCACHE": str(world.tree / "gocache")})
        self.assertEqual(first.code, 0)
        for unit_id in GO_UNITS:
            record = world.record_of(first, unit_id)
            self.assertIs(record["inheritable"], False, unit_id)
            self.assertIn("GOCACHE 必须是工作根内、源码树外的独立子目录", record["not_inheritable_reason"])
            credentials = {item["id"]: item for item in record["declared_contracts"]}
            self.assertIs(credentials["go-build-cache"]["verified_before_run"], False)
            self.assertIs(credentials["go-build-cache"]["observed"]["root_ok"], False)
        self.assertTrue(world.record_of(first, "frontend:critical")["inheritable"], "前端单元没有 go 缓存合同，不受影响")

    def test_go_mod_verify_runs_once_per_modcache_and_a_failure_blocks_inheritance_until_it_passes(self) -> None:
        world = World(self)
        world.flag("go-verify-fail")
        first = world.run()
        self.assertEqual(first.code, 0)
        verifies = [line for line in world.go_log.read_text(encoding="utf-8").splitlines() if '"argv": ["mod", "verify"]' in line]
        self.assertEqual(len(verifies), 1, "三个 Go 单元共用同一个 GOMODCACHE：同一次执行器调用里只核一次")
        self.assertIn("GOMODCACHE", verifies[0])
        assert_go_mod_verify_ran_in_the_module_root(self, world, verifies[0])
        for unit_id in GO_UNITS:
            record = world.record_of(first, unit_id)
            self.assertIs(record["inheritable"], False, unit_id)
            self.assertIn("go mod verify 没有通过", record["not_inheritable_reason"])
            credential = {item["id"]: item for item in record["declared_contracts"]}["go-mod-cache"]
            self.assertEqual((credential["verified_before_run"], credential["observed"]["verify"]), (False, "failed"))
        world.flag("go-verify-fail", False)
        second = world.run(mode="full-set-pass")
        self.assertEqual(second.executed, set(GO_UNITS) | set(RERUN_ONLY), "来源记录不可承接，Go 单元重跑")
        self.assertEqual(second.inherited, {"frontend:critical"})
        for unit_id in GO_UNITS:
            record = world.record_of(second, unit_id)
            credential = {item["id"]: item for item in record["declared_contracts"]}["go-mod-cache"]
            self.assertEqual((record["inheritable"], credential["verified_before_run"], credential["observed"]["verify"]), (True, True, "ok"))
            self.assertEqual(credential["observed"]["go_sum_sha256"], hashlib.sha256(TREE_FILES["backend/go.sum"].encode()).hexdigest())
        third = world.run(mode="full-set-pass")
        self.assertEqual(third.inherited, set(DECLARED_IDS))

    def test_a_go_mod_verify_that_hangs_makes_the_go_records_uninheritable_but_the_commands_still_run(self) -> None:
        """go mod verify 在调度线程里同步执行，卡住不能拖住调度线程：这一步有自己的超时（模块常量 DECLARED_GO_VERIFY_TIMEOUT_SECONDS，缺省 180 秒，
        这里在子进程里调成 1 秒），超时按「核验没通过」处理——三个 Go 单元的记录不可承接、命令照跑（结论不变、整次运行不失败）、凭证里写明
        原因；卡住的核验整次运行只等一次（结果按 GOMODCACHE 记忆）。"""

        world = World(self)
        world.flag_text("go-verify-sleep", "60")   # 假 go 的 verify 睡 60 秒；没有超时的话这一轮要等一分钟
        began = time.monotonic()
        first = world.run(bootstrap="ue.DECLARED_GO_VERIFY_TIMEOUT_SECONDS = 1")
        elapsed = time.monotonic() - began
        self.assertEqual(first.code, 0, first.stderr[-2000:])
        self.assertLess(elapsed, 50, "没有被 60 秒的 verify 拖住")
        verifies = [line for line in world.go_log.read_text(encoding="utf-8").splitlines() if '"argv": ["mod", "verify"]' in line]
        self.assertEqual(len(verifies), 1, "三个 Go 单元共用同一个 GOMODCACHE：超时的核验也只做一次，不是每个单元各等一次")
        for unit_id in GO_UNITS:
            record = world.record_of(first, unit_id)
            self.assertIs(record["passed"], True, f"{unit_id}：命令照跑，结论不受影响")
            self.assertIs(record["inheritable"], False, unit_id)
            self.assertIn("declared-contract-preflight", record["not_inheritable_reason"])
            self.assertIn("go mod verify 超过 1 秒还没结束", record["not_inheritable_reason"], "原因里写明是超时")
            credential = {item["id"]: item for item in record["declared_contracts"]}["go-mod-cache"]
            self.assertIs(credential["verified_before_run"], False)
            self.assertEqual(credential["observed"]["verify"], "timeout", "凭证里写明是超时，不是普通的核验失败")
            self.assertIn("超过 1 秒", credential["observed"]["problem"])
            self.assertIs(credential["observed"]["root_ok"], True)
            self.assertTrue(record["read_audit"]["status"] == "passed", "读集审计是另一回事：照常通过")
        self.assertTrue(world.record_of(first, "frontend:critical")["inheritable"], "不声明 go 缓存合同的前端单元不受影响")
        # 卡住的是来源记录：下一轮这些单元不被承接、要重跑；核验恢复后新记录可承接。
        world.flag_text("go-verify-sleep", None)
        second = world.run(mode="full-set-pass")
        self.assertEqual(second.executed, set(GO_UNITS) | set(RERUN_ONLY))
        self.assertEqual(second.inherited, {"frontend:critical"})
        for unit_id in GO_UNITS:
            credential = {item["id"]: item for item in world.record_of(second, unit_id)["declared_contracts"]}["go-mod-cache"]
            self.assertEqual((world.record_of(second, unit_id)["inheritable"], credential["observed"]["verify"]), (True, "ok"))

    def test_the_default_go_mod_verify_timeout_is_180_seconds(self) -> None:
        self.assertEqual(ue.DECLARED_GO_VERIFY_TIMEOUT_SECONDS, 180)
        self.assertLess(ue.DECLARED_GO_VERIFY_TIMEOUT_SECONDS, 900, "比合同探测会话默认 runner（entry_steps.default_runner）的 900 秒短")

    def test_t9_a_changed_toolchain_reruns_only_the_units_that_declare_it(self) -> None:
        world = World(self)
        world.run()
        install(world.bin / "go", FAKE_GO + "\n# 工具链升级\n")  # 可执行文件摘要变了
        world.commit({"docs/other.md": "x\n"})
        second = world.run(mode="full-set-pass")
        self.assertEqual(second.executed, set(GO_UNITS) | set(RERUN_ONLY))
        self.assertEqual(second.inherited, {"frontend:critical"}, "不声明 go 工具链的前端单元承接")
        reasons = next(entry for entry in second.manifest["units"] if entry["unit_id"] == "backend:go-test")["reasons"]
        self.assertIn("contract:toolchain:go", "；".join(reasons))

    def test_credentials_have_the_documented_shape_and_are_verified_before_the_unit_starts(self) -> None:
        world = World(self)
        first = world.run()
        for unit_id in DECLARED_IDS:
            record = world.record_of(first, unit_id)
            started = ur.parse_utc(record["started_at_utc"])
            scope = next(entry for entry in record["inputs"] if entry["name"] == "declared-inputs-scope")["detail"]["scope"]
            required = {name.split(":", 1)[1] for name in scope["contracts"] if name.split(":", 1)[0] in ("cache", "service-image")}
            credentials = record["declared_contracts"]
            self.assertEqual({item["id"] for item in credentials}, required, unit_id)
            for item in credentials:
                self.assertTrue({"id", "kind", "verified_before_run", "observed", "verified_at_utc"} <= set(item), item)
                self.assertTrue(item["verified_before_run"], item)
                self.assertLessEqual(ur.parse_utc(item["verified_at_utc"]), started, "运行前核验：核验时刻不晚于单元启动")
        postgres = {item["id"]: item for item in world.record_of(first, "backend:integration")["declared_contracts"]}["postgres"]
        entries = self.entries_of(world.record_of(first, "backend:integration"))
        self.assertEqual(postgres["observed"]["image_id"], entries["contract:service-image:postgres"]["detail"]["image_id"])
        self.assertIn("image inspect --format {{.Id}} postgres:18.1-alpine3.23", world.docker_log.read_text(encoding="utf-8"))

    def test_a_tampered_registry_evidence_fails_closed_on_the_alpha_prime_path(self) -> None:
        world = World(self)
        relative = world.registry_units["backend:go-test"]["sample"]["evidence"]
        world.commit({relative: (world.tree / relative).read_text(encoding="utf-8").replace('"exit_code": 0', '"exit_code": 0 ')})
        first = world.run()
        self.assertEqual(first.code, 1, "登记表证据的字节摘要与范围条目登记的不符：声明级单元失败，不静默放过")
        record = world.record_of(first, "backend:go-test")
        self.assertIs(record["passed"], False)
        self.assertEqual(record["read_audit"]["status"], "failed")
        self.assertEqual(record["read_audit"]["undeclared"][0]["rule"], "evidence-missing")
        self.assertEqual(first.summary["declared_inputs"]["audit_failed"], 1)
        self.assertEqual(first.summary["declared_inputs"]["audit_passed"], len(DECLARED_IDS) - 1)

    def test_legacy_audit_reads_run_reports_declared_units_in_the_read_audit_section(self) -> None:
        world = World(self)
        first = world.run("--audit-reads")
        self.assertEqual(first.code, 0, first.stderr[-2000:])
        declared = first.summary["read_audit"]["declared"]
        self.assertEqual((declared["units"], declared["failed"], declared["incomplete"]), (sorted(DECLARED_IDS), [], []))
        self.assertEqual(declared["sample_origin"], {"registry": sorted(DECLARED_IDS), "run": []})
        report = json.loads(Path(first.summary["read_audit"]["report"]).read_text(encoding="utf-8"))
        self.assertEqual(report["declared"], declared)
        exempt = report["units"]["deploy:check"]
        self.assertEqual((exempt["declared_exempt"], exempt["status"], exempt["undeclared_count"]), ("rerun-only", "passed", 0),
                         "rerun-only 单元不审计（永不成为承接来源），不能因为清单标了 inheritable:false、当前输入为空而被判『无从核对』")
        self.assertEqual(declared["rerun_only_exempt"], ["deploy:check"])
        self.assertNotIn("declared", run_without_declared_units_summary(self)["read_audit"], "没有声明级结果的审计汇总里没有 declared 键")

    def test_sample_flag_accepts_only_off_always_or_every_n(self) -> None:
        for good, expected in (("off", "off"), ("always", "always"), ("every:3", "every:3"), ("every:12", "every:12")):
            self.assertEqual(ue.declared_sample_mode(good), expected)
        # 与入口脚本 entry-gates.sh 的正则同样严格（独立验收 #3 一般 11）：首尾空白、换行、前导零、大小写变体都拒绝，不做任何规范化。
        for bad in ("", "on", "every", "every:0", "every:-1", "every:x", "every:1.5", "ALWAYS", "every:3 4", " every:12 ", " off", "off ", "always\n", "\talways",
                    "every:3\n", "every:03", "Every:3", None, 3, ["always"]):
            with self.subTest(bad=bad), self.assertRaises(Exception) as caught:
                ue.declared_sample_mode(bad)
            self.assertIn("--declared-sample", str(caught.exception))
        world = World(self)
        completed = subprocess.run([PY, str(EXECUTOR), "run-gates", "--manifest", str(world.manifest_path), "--declared-sample", "sometimes"],
                                   cwd=world.tree, capture_output=True, text=True, env=world.environment, timeout=120)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--declared-sample", completed.stderr)
        self.assertEqual(ue._parse(["run-gates", "--manifest", "m.json"]).declared_sample, "off", "缺省 off")
        self.assertEqual(ue._parse(["run-gates", "--manifest", "m.json", "--declared-sample", "every:4"]).declared_sample, "every:4")
        for padded in (" always", "always\n", "every:4 ", "every:04"):   # 命令行解析层同样不规范化
            with self.subTest(padded=padded), contextlib.redirect_stderr(io.StringIO()) as captured, self.assertRaises(SystemExit) as exited:
                ue._parse(["run-gates", "--manifest", "m.json", "--declared-sample", padded])
            self.assertEqual(exited.exception.code, 2)
            self.assertIn("--declared-sample", captured.getvalue())


# ---------------------------------------------------------------------------
# 第三组：逐次采样 β（--declared-sample）
# ---------------------------------------------------------------------------

GARBAGE = "4242 THIS IS NOT A SYSCALL LINE ###"


class SampledEvidenceTests(unittest.TestCase):
    """β：每次执行都带 strace 采样（假 strace 按单元合成输出，经真实的过滤器进程聚合）。"""

    def sampled(self, world: World, *extra: str, **kwargs: Any) -> Run:
        return world.run("--declared-sample", "always", *extra, **kwargs)

    def strace_record(self, world: World, unit_id: str) -> dict[str, Any]:
        return json.loads((world.state / ("strace-" + unit_id.replace(":", "_") + ".json")).read_text(encoding="utf-8"))

    def test_always_writes_run_origin_records_that_the_consumer_accepts_and_a_later_run_inherits(self) -> None:
        world = World(self)
        world.plan_good_strace()
        first = self.sampled(world)
        self.assertEqual(first.code, 0, first.stderr[-2500:])
        store = ur.RecordStore(world.store)
        for unit_id in DECLARED_IDS:
            record = world.record_of(first, unit_id)
            audit = record["read_audit"]
            registry = world.registry_units[unit_id]["sample"]
            self.assertEqual((audit["status"], audit["coverage_complete"], audit["sample_origin"]), ("passed", True, "run"), unit_id)
            self.assertNotEqual(audit["trace"]["sha256"], registry["evidence_sha256"], "β：封存的是本次运行自己的证据，字节摘要不同于登记证据")
            self.assertEqual(audit["sample_binding"]["evidence_sha256"], registry["evidence_sha256"], "绑定的仍是登记表证据")
            self.assertEqual(ur.file_sha256(store.log_path(audit["trace"]["sha256"])), audit["trace"]["sha256"], "本次证据按内容摘要封存")
            sample = json.loads(store.log_path(audit["trace"]["sha256"]).read_text(encoding="utf-8"))
            self.assertEqual(sample["schema_version"], "declared-sample/v1")
            self.assertEqual(sample["unit_id"], unit_id)
            self.assertEqual(sample["sampled"]["sampler_sha256"], SAMPLER_SHA, "采样器摘要＝本次实际运行的 declared_sampler.py 的字节摘要")
            self.assertEqual(sample["command"]["argv"], world.commands[unit_id])
            self.assertEqual(sample["command"]["launcher"], world.launcher)
            self.assertEqual((sample["exit_code"], sample["valid"], sample["invalid_reasons"]), (0, True, []))
            self.assertEqual(sample["aliases"]["TREE"], str(world.tree))
            self.assertEqual(sample["aliases"]["WORK"], str(world.work), "WORK＝GOCACHE 的父目录")
            self.assertEqual(sample["sampled"]["runs"][0]["exit_code"], 0)
            self.assertIn("kernel", sample["sampled"])
            rows = {path: (kinds, tracked) for path, kinds, tracked in sample["repo_accesses"]}
            first_read = os.path.normpath(str(Path(UNIT_SPECS[unit_id]["cwd"]) / UNIT_SPECS[unit_id]["reads"][0]))
            self.assertEqual(rows["{TREE}/" + first_read], (["read"], True), "仓库内路径标 tracked")
            path = store.record_path(unit_id, record["record_sha256"])
            self.assertEqual(ur.check_record(store, path, record, current_for(record), facts_for(record)), [], f"{unit_id}：run 来源记录过消费者核验")
        declared = first.summary["declared_inputs"]
        self.assertEqual((declared["sample_mode"], declared["audit_passed"], declared["consumer_accepted"]), ("always", 4, 4))
        world.commit({UNRELATED_CLAUDE_TEST: PASS + "# 无关\n"})
        second = world.run(mode="full-set-pass")
        self.assertEqual(second.inherited, set(DECLARED_IDS), "run 来源的记录同样可以被后续运行承接（缺省 off 的运行也承接）")
        self.assertEqual(second.summary["declared_inputs"]["sample_mode"], "off")

    def test_the_launcher_is_outside_strace_and_the_unit_environment_reaches_the_traced_process(self) -> None:
        world = World(self, launcher="fake")
        world.plan_good_strace()
        first = self.sampled(world)
        self.assertEqual(first.code, 0, first.stderr[-2500:])
        for unit_id in DECLARED_IDS:
            seen = self.strace_record(world, unit_id)
            self.assertEqual(seen["FAKE_LAUNCHER"], "1", "启动前缀先于 strace 运行：它设的变量 strace 看得到")
            args = seen["args"]
            command = args[args.index("--") + 1:]
            self.assertEqual(command, ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', world.cwd_of(unit_id), *world.commands[unit_id]],
                             "被跟踪的是切目录再 exec 命令的 sh，不含启动前缀")
            self.assertEqual(args[args.index("-o") + 1][0], "|")
            self.assertIn("verbose=execve,execveat,connect", args)
            self.assertEqual(args[args.index("-s") + 1], "4096")
        self.assertEqual(self.strace_record(world, "backend:go-test")["GOFLAGS"], "-mod=readonly", "单元 env 里的离线值到了被跟踪的进程")
        record = world.record_of(first, "backend:go-test")
        self.assertEqual(record["spec"]["argv"][0], world.launcher[0])
        sample = json.loads(ur.RecordStore(world.store).log_path(record["read_audit"]["trace"]["sha256"]).read_text(encoding="utf-8"))
        self.assertEqual(sample["command"]["launcher"], world.launcher, "证据里记着启动前缀，消费者据此核 spec.argv")
        self.assertEqual(first.summary["declared_inputs"]["consumer_accepted"], 4)

    def test_garbled_stream_only_makes_the_record_incomplete_and_the_command_verdict_is_unchanged(self) -> None:
        world = World(self)
        world.plan_good_strace(**{"backend:go-test": world.good_lines("backend:go-test") + [GARBAGE]})
        first = self.sampled(world)
        self.assertEqual(first.code, 0, "只因证据不完整：整次运行不失败")
        self.assertEqual(first.summary["status"], "passed")
        record = world.record_of(first, "backend:go-test")
        audit = record["read_audit"]
        self.assertIs(record["passed"], True, "命令本身的结论不变")
        self.assertEqual((audit["status"], audit["coverage_complete"], audit["undeclared_count"], audit["undeclared"]), ("incomplete", False, 0, []))
        self.assertEqual(audit["incomplete"]["rules"], ["C7-unresolved"])
        self.assertIn("unrecognized-line", audit["incomplete"]["reason"])
        self.assertIs(record["inheritable"], False)
        self.assertTrue(record["not_inheritable_reason"].startswith("declared-sample-incomplete:C7-unresolved"), record["not_inheritable_reason"])
        self.assertEqual(audit["sample_origin"], "run")
        self.assertTrue(Path(audit["trace"]["stored"]).is_file(), "不完整的证据也留着，便于查原因")
        declared = first.summary["declared_inputs"]
        self.assertEqual((declared["audit_passed"], declared["audit_incomplete"], declared["audit_failed"]), (3, 1, 0))
        self.assertIn("backend:go-test", declared["consumer_rejected"], "消费者不接受不完整的记录")
        self.assertEqual(first.summary["unit_manifest"]["self_check"], "passed")
        self.assertEqual(world.record_of(first, "backend:integration")["inheritable"], True, "其它单元不受影响")
        second = world.run(mode="full-set-pass")
        self.assertIn("backend:go-test", second.executed, "永不成为承接来源：下一轮重跑")
        self.assertEqual(second.inherited, {"backend:integration", "frontend:critical", "egress-spec:egress-spec-go-test"})

    def test_strace_garbage_lines_of_vanished_threads_are_dropped_by_the_filter_and_cost_the_record_nothing(self) -> None:
        """ARM64 上抓到的真实现象：strace 对中途消失的线程打印垃圾行（约每三百万行一次）。采样器的过滤器丢弃形态 A、B 的垃圾行并计入
        coverage.detached_markers，这条链路上（过滤器→证据→覆盖重算）记录照样 passed、可承接；原样行留在证据的 review 里供事后查看，
        而不是进 coverage（证据 coverage 的字段集合是闭合的）。垃圾行里「恰好像路径」的参数也不会被当成一次读取。"""

        from tools.official_client_capture.tests import declared_sampler_trace_fixture as real

        garbage_a, garbage_b = real.GARBLED_DETACHED_A.splitlines()[2], real.GARBLED_LOST_RESULT_B.splitlines()[1]
        world = World(self)
        world.plan_good_strace(**{"backend:go-test": world.good_lines("backend:go-test") + [garbage_a, garbage_b]})
        first = self.sampled(world)
        self.assertEqual(first.code, 0, first.stderr[-2500:])
        record = world.record_of(first, "backend:go-test")
        self.assertEqual((record["read_audit"]["status"], record["passed"], record["inheritable"]), ("passed", True, True))
        sample = json.loads(ur.RecordStore(world.store).log_path(record["read_audit"]["trace"]["sha256"]).read_text(encoding="utf-8"))
        self.assertEqual(sample["coverage"]["detached_markers"], 2)
        self.assertEqual(sample["coverage"]["unresolved_reasons"], {}, "垃圾行不再留下假的未解析原因")
        # 夹具里的两条真实垃圾行名字都是 lsetxattr（副作用类）：采样器在 raw 顶层记 detached_side_effects=2，执行器也把它放进证据的 review（WP1g），
        # 与原样行并列；coverage 的字段集合仍然闭合，记录的结论不变（上面的 passed／inheritable、下面的消费者核验）。
        self.assertEqual(sample["review"], {"detached_samples": [garbage_a, garbage_b], "detached_side_effects": 2}, "原样行与副作用类条数随证据封存")
        self.assertNotIn("detached_side_effects", sample["coverage"], "计数只进 review，不进字段闭合的 coverage")
        self.assertNotIn("/usr/local/go/src/internal/abi", json.dumps(sample["outside_accesses"]), "形态 B 里像路径的参数不算一次读取")
        self.assertEqual(ur.check_record(ur.RecordStore(world.store), ur.RecordStore(world.store).record_path("backend:go-test", record["record_sha256"]),
                                         record, current_for(record), facts_for(record)), [], "消费者照样接受")

    def test_dropped_garbage_lines_of_a_non_side_effect_name_leave_no_side_effect_count_in_the_evidence(self) -> None:
        """被丢弃的垃圾行里没有副作用类名字时，raw 不写 detached_side_effects（有才写），证据的 review 里也就没有这个键：只留原样行样例。
        混着副作用类的行时，计数只数副作用类那几条（总数 detached_markers 仍是全部被丢弃的行）。"""

        plain = "711006 syscall_0x15a17b(0, 0x42ce66593f50, 0, 0x8, 0, 0x2 <detached ...>"
        side = '711008 lsetxattr(0xffffffffffffff00, "/p", "", 0, 0) = ?'
        for lines, expected_review, markers in (([plain], {"detached_samples": [plain]}, 1),
                                                ([plain, side], {"detached_samples": [side, plain], "detached_side_effects": 1}, 2)):
            with self.subTest(lines=len(lines)):
                world = World(self)
                world.plan_good_strace(**{"backend:go-test": world.good_lines("backend:go-test") + lines})
                first = self.sampled(world)
                self.assertEqual(first.code, 0, first.stderr[-2500:])
                record = world.record_of(first, "backend:go-test")
                self.assertEqual((record["read_audit"]["status"], record["passed"], record["inheritable"]), ("passed", True, True), "结论不变")
                sample = json.loads(ur.RecordStore(world.store).log_path(record["read_audit"]["trace"]["sha256"]).read_text(encoding="utf-8"))
                self.assertEqual(sample["coverage"]["detached_markers"], markers)
                self.assertEqual(sample["review"], expected_review)

    def test_tolerated_unfinished_calls_at_eof_keep_their_samples_in_the_evidence_and_the_record_passes(self) -> None:
        """多线程进程退出时永远配不上的 <unfinished ...>（形态 C）：不超过上限就被容忍；过滤器留的起始行样例是证据认识的可选键
        coverage.unfinished_samples，必须留在证据里（只有按原因归类的样例才挪进 review）。"""

        world = World(self)
        cwd = world.cwd_of("backend:go-test")
        pointer = f"740774 newfstatat(AT_FDCWD<{cwd}>, 0x1948d3defda0,  <unfinished ...>"
        world.plan_good_strace(**{"backend:go-test": world.good_lines("backend:go-test") + [pointer]})
        first = self.sampled(world)
        self.assertEqual(first.code, 0, first.stderr[-2500:])
        record = world.record_of(first, "backend:go-test")
        self.assertEqual((record["read_audit"]["status"], record["inheritable"]), ("passed", True))
        sample = json.loads(ur.RecordStore(world.store).log_path(record["read_audit"]["trace"]["sha256"]).read_text(encoding="utf-8"))
        self.assertEqual(sample["coverage"]["unresolved_reasons"], {"unfinished-calls-at-eof": 1})
        self.assertEqual(len(sample["coverage"]["unfinished_samples"]), 1)
        self.assertIn("<unfinished ...>", sample["coverage"]["unfinished_samples"][0])
        self.assertNotIn("review", sample, "没有按原因归类的样例、也没有被丢弃的垃圾行：不带 review")

    def test_an_empty_strace_stream_and_a_missing_filter_output_are_incomplete_too(self) -> None:
        for label, change, rules, fragment in (("strace 一行都没跟踪到", {"lines": []}, ["C7-lines"], "采样没有产生任何输出行"),
                                                ("过滤器没有产出", {"skip_pipe": True}, [], "filter-output-missing")):
            with self.subTest(label):
                world = World(self)
                plan = world.plan_good_strace()
                plan["backend:go-test"].update(change)
                world.set_strace_plan(plan)
                first = self.sampled(world)
                self.assertEqual(first.code, 0, first.stderr[-2000:])
                record = world.record_of(first, "backend:go-test")
                self.assertEqual((record["read_audit"]["status"], record["passed"], record["inheritable"]), ("incomplete", True, False))
                self.assertEqual(record["read_audit"]["incomplete"]["rules"], rules)
                self.assertIn(fragment, record["not_inheritable_reason"])

    def test_a_real_undeclared_access_fails_the_record_and_the_whole_run(self) -> None:
        world = World(self)
        stray = open_line(world.cwd_of("backend:go-test"), str(world.tree / "frontend/src/x.ts"))
        world.plan_good_strace(**{"backend:go-test": world.good_lines("backend:go-test") + [stray]})
        first = self.sampled(world)
        self.assertEqual(first.code, 1, "出现集合之外的规则（C1-undeclared）：整次运行判失败，不静默降级")
        self.assertEqual(first.summary["status"], "failed")
        record = world.record_of(first, "backend:go-test")
        self.assertIs(record["passed"], False)
        self.assertEqual((record["read_audit"]["status"], record["read_audit"]["undeclared_count"]), ("failed", 1))
        self.assertEqual([(item["rule"], item["path"]) for item in record["read_audit"]["undeclared"]], [("C1-undeclared", "{TREE}/frontend/src/x.ts")])
        self.assertEqual(first.summary["declared_inputs"]["audit_failed"], 1)
        self.assertIn("backend:go-test", first.summary["failed_units"])
        # 混合：既有集合内的证据问题又有集合外的真访问 → failed。
        mixed = World(self)
        mixed_stray = open_line(mixed.cwd_of("backend:go-test"), str(mixed.tree / "frontend/src/x.ts"))
        mixed.plan_good_strace(**{"backend:go-test": mixed.good_lines("backend:go-test") + [GARBAGE, mixed_stray]})
        second = self.sampled(mixed)
        self.assertEqual(second.code, 1)
        audit = mixed.record_of(second, "backend:go-test")["read_audit"]
        self.assertEqual(audit["status"], "failed")
        self.assertEqual({item["rule"] for item in audit["undeclared"]}, {"C1-undeclared", "C7-unresolved"})

    def test_a_command_that_exits_nonzero_under_sampling_fails_like_it_always_did(self) -> None:
        world = World(self, failing=("backend:go-test",))
        world.plan_good_strace()
        first = self.sampled(world)
        self.assertEqual(first.code, 1)
        record = world.record_of(first, "backend:go-test")
        self.assertEqual((record["exit_code"], record["passed"], record["read_audit"]["status"]), (3, False, "failed"))
        self.assertEqual([item["rule"] for item in record["read_audit"]["undeclared"]], ["C7-exit"], "命令自己退出码非零：C7-exit 在集合之外，判失败")

    def test_a_command_that_is_not_the_registered_one_fails_in_both_modes(self) -> None:
        for mode_arguments in ((), ("--declared-sample", "always")):
            with self.subTest(mode=mode_arguments):
                world = World(self)
                world.plan_good_strace()
                manifest = json.loads(world.manifest_path.read_text(encoding="utf-8"))
                next(unit for unit in manifest["units"] if unit["unit_id"] == "backend:go-test")["argv"].append("a.go")
                world.manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
                first = world.run(*mode_arguments)
                self.assertEqual(first.code, 1, first.stderr[-1500:])
                record = world.record_of(first, "backend:go-test")
                self.assertEqual([item["rule"] for item in record["read_audit"]["undeclared"]], ["C7-command"])
                self.assertIs(record["passed"], False)

    def test_every_n_samples_the_first_execution_and_then_every_nth(self) -> None:
        world = World(self)
        world.plan_good_strace()
        origins = []
        for _ in range(3):
            run = world.run("--declared-sample", "every:2")
            self.assertEqual(run.code, 0, run.stderr[-1500:])
            origins.append({unit_id: world.record_of(run, unit_id)["read_audit"]["sample_origin"] for unit_id in DECLARED_IDS})
        self.assertEqual([set(item.values()) for item in origins], [{"run"}, {"registry"}, {"run"}], "第 1、3 次（库里已有 0、2 条正式记录）采样，第 2 次不采")
        self.assertEqual(self.strace_record(world, "backend:go-test")["args"][0], "-f", "strace 确实被调用过")

    def test_off_never_starts_strace_and_costs_nothing(self) -> None:
        world = World(self)
        world.plan_good_strace()
        first = world.run()
        self.assertEqual(first.code, 0)
        self.assertFalse(list(world.state.glob("strace-*.json")), "缺省 off：一次 strace 都不起（零运行时开销）")
        self.assertEqual({world.record_of(first, unit_id)["read_audit"]["sample_origin"] for unit_id in DECLARED_IDS}, {"registry"})


# ---------------------------------------------------------------------------
# 第四组：进程内夹具与逐项单元测试
# ---------------------------------------------------------------------------


class ToggleRunner:
    """探测用的 runner：默认走真实的子进程（PATH 里是假工具链），命令行里含某个片段的调用按预置结果返回——模拟「算当前事实之后、单元启动前，
    docker／git 的状态变了」。"""

    def __init__(self) -> None:
        self.overrides: dict[str, tuple[int, str]] = {}
        self.calls: list[str] = []

    def __call__(self, argv: Any, env: Any, cwd: Any) -> tuple[int, str]:
        text = " ".join(argv)
        self.calls.append(text)
        for fragment, result in self.overrides.items():
            if fragment in text:
                return result
        return eg._entry_steps_module().default_runner(argv, env, cwd)


class Harness:
    """进程内夹具：合成世界＋切到测试树＋环境换成世界的环境（PATH 里是假工具链）＋探测工厂与声明级运行上下文；currents 由真实的
    ``_gate_currents`` 算出。用例结束自动复位。"""

    def __init__(self, case: unittest.TestCase, *, probe: Any = None, sample_mode: str = "off", manifest_options: dict[str, Any] | None = None,
                 **world_options: Any) -> None:
        self.case = case
        self.world = World(case, **world_options)
        if manifest_options is not None:
            self.world.manifest_path = self.world.write_manifest(**manifest_options)
        stack = contextlib.ExitStack()
        case.addCleanup(stack.close)
        stack.enter_context(contextlib.chdir(self.world.tree))
        stack.enter_context(mock.patch.dict(os.environ, self.world.environment, clear=True))
        _groups, self.units, _gates, self.manifest = ue.load_gates_manifest(self.world.manifest_path, machine_cores=4)
        self.unit_by_id = {unit.unit_id: unit for unit in self.units}
        token = ue._DECLARED_PROBE.set(probe or eg.declared_probe)
        stack.callback(ue._DECLARED_PROBE.reset, token)
        declared, rerun_only = ue._declared_unit_ids(self.manifest)
        self.run = ue._DeclaredRun(entry=eg, manifest=self.manifest, tree=self.world.tree, sample_mode=sample_mode, declared=declared,
                                   rerun_only=rerun_only)
        token = ue._DECLARED_RUN.set(self.run)
        stack.callback(ue._DECLARED_RUN.reset, token)
        self.currents = self.compute()

    def compute(self) -> dict[str, Any]:
        return ue._gate_currents(ue._records_module(), groups=[], group_units={}, command_units=self.units, manifest=self.manifest, timeout=120.0)

    def auditor(self, **kwargs: Any) -> Any:
        return ue.ReadAuditor(out_dir=self.world.root / "audit-out", repo_root=self.world.tree, data_root=None, currents=self.currents, **kwargs)

    def argv_of(self, unit_id: str) -> list[str]:
        unit = self.unit_by_id[unit_id]
        return ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd, *unit.command]

    def outcome(self, unit_id: str, *, exit_code: int | None = 0, seconds: float = 1.5) -> Any:
        return ue.Outcome(unit=self.unit_by_id[unit_id], kind="formal", exit_code=exit_code, signal=None, timed_out=False, seconds=seconds, cpu_seconds=0.0,
                          max_rss_mb=0.0, orphans=[], log_path=self.world.root / "unit.log", result=None)

    def running(self, unit_id: str) -> Any:
        base = self.world.root / "running"
        base.mkdir(exist_ok=True)
        return ue.Running(self.unit_by_id[unit_id], 1, 0.0, base / "unit.log", base / "unit.result.json", base / "unit.record.json", "formal")

    def sampled_collect(self, unit_id: str, *, lines: list[str] | None = None, mutate: Any = None, exit_code: int | None = 0,
                        raw_text: str | None = None) -> tuple[dict[str, Any], Any, Any]:
        """β 的一次 wrap→（命令运行，这里用合成的 strace 流代替）→collect：返回（read_audit 结果, 审计器, 单元的 Outcome）。"""

        self.run.sample_mode = "always"
        unit, auditor = self.unit_by_id[unit_id], self.auditor()
        self.run.preflights[unit_id] = ue.declared_contract_preflight(unit, self.currents[unit_id])  # 调度器在 launch 之前做的前置核验
        wrapped = auditor.wrap(unit, self.argv_of(unit_id))
        state = auditor._declared_state[unit_id]
        self.case.assertTrue(state["sampled"], state)
        self.case.assertNotEqual(wrapped, self.argv_of(unit_id))
        if raw_text is not None:
            Path(state["raw"]).write_text(raw_text, encoding="utf-8")
        elif lines != "no-raw":
            stream = lines if lines is not None else self.world.good_lines(unit_id)
            raw = ds.filter_stream((line + "\n" for line in stream), state["aliases"], unit_id=unit_id)
            if mutate is not None:
                mutate(raw)
            ds.write_document(state["raw"], raw)
        outcome = self.outcome(unit_id, exit_code=exit_code)
        auditor.note_outcome(outcome)
        result = auditor.collect(unit)
        outcome.extra["read_audit"] = result
        outcome.extra["reexecute_only"] = False
        auditor.declared_finish(self.running(unit_id), outcome, result)
        return result, auditor, outcome


class VerdictTests(unittest.TestCase):
    """覆盖不通过的两类处理（调度者 10-14 定案，WP1b 核对规则词表后收紧）：incomplete 与 failed 的判据是一个闭合的集合。"""

    def vocabulary(self) -> set[str]:
        import inspect

        return set(re.findall(r'"(C[0-9]-[a-z0-9-]+)"', inspect.getsource(ur.declared_coverage)))

    def test_the_incomplete_rule_set_is_pinned_and_a_subset_of_the_coverage_vocabulary(self) -> None:
        self.assertEqual(ue.DECLARED_INCOMPLETE_RULES, frozenset({"C7-stream", "C7-unresolved", "C7-lines", "C7-detached"}))
        vocabulary = self.vocabulary()
        # C7-detached 由记录层的 declared_coverage 产生（WP1d 已合入）：集合里的每个规则名都必须是 declared_coverage 真有的规则，没有例外。
        # 谁在词表里删掉了一条规则（或改了名），这里立刻红，而不是悄悄让执行器多认一个永远不会出现的名字。
        self.assertIn("C7-detached", vocabulary, "记录层必须认识 C7-detached（WP1d）")
        self.assertTrue(ue.DECLARED_INCOMPLETE_RULES <= vocabulary, "集合里的规则名必须是 declared_coverage 真有的规则")
        self.assertEqual({rule for rule in vocabulary if rule.startswith("C7")} - ue.DECLARED_INCOMPLETE_RULES,
                         {"C7-command", "C7-exit", "C7-format", "C7-invalid"}, "其余四条 C7 规则不是偶发的证据问题，一律 failed")
        self.assertIsInstance(ue.DECLARED_INCOMPLETE_RULES, frozenset)

    def test_verdict_matrix(self) -> None:
        def findings(*rules: str) -> list[dict[str, str]]:
            return [{"rule": rule, "path": "", "kinds": [], "reason": "x"} for rule in rules]

        self.assertEqual(ue.declared_audit_verdict([]), "passed")
        for rule in sorted(ue.DECLARED_INCOMPLETE_RULES):
            self.assertEqual(ue.declared_audit_verdict(findings(rule)), "incomplete", rule)
        self.assertEqual(ue.declared_audit_verdict(findings(*sorted(ue.DECLARED_INCOMPLETE_RULES))), "incomplete", "集合内四条叠在一起仍是 incomplete")
        outside = sorted(self.vocabulary() - ue.DECLARED_INCOMPLETE_RULES)
        self.assertGreaterEqual(len(outside), 20)
        for rule in outside:
            self.assertEqual(ue.declared_audit_verdict(findings(rule)), "failed", f"{rule}：集合之外一律 failed")
            for member in sorted(ue.DECLARED_INCOMPLETE_RULES):
                self.assertEqual(ue.declared_audit_verdict(findings(member, rule)), "failed", f"{member}＋{rule}：混合一律 failed")
        self.assertEqual(ue.declared_audit_verdict(findings("C9-brand-new-rule")), "failed", "新增的未归类规则默认 failed")
        self.assertEqual(ue.declared_audit_verdict(findings("evidence-missing")), "failed", "执行器自己的失败项也是 failed")

    def test_the_detached_rule_is_incomplete_alone_and_failed_when_mixed_with_anything_else(self) -> None:
        """WP3b：C7-detached（被丢弃的 strace 标记行总数超过上限）和 C7-stream 同类，是「证据不足以证明覆盖」而不是违规。这里是纯函数层的判定，
        不依赖 declared_coverage 是否已经认识这条规则。"""

        def findings(*rules: str) -> list[dict[str, str]]:
            return [{"rule": rule, "path": "", "kinds": [], "reason": "x"} for rule in rules]

        self.assertIn("C7-detached", ue.DECLARED_INCOMPLETE_RULES)
        self.assertEqual(ue.declared_audit_verdict(findings("C7-detached")), "incomplete")
        self.assertEqual(ue.declared_audit_verdict(findings("C7-detached", "C7-stream", "C7-lines", "C7-unresolved")), "incomplete")
        for other in ("C1-undeclared", "C7-exit", "C7-command", "C7-format", "C7-invalid", "C9-new"):
            self.assertEqual(ue.declared_audit_verdict(findings("C7-detached", other)), "failed", other)

    def canned_coverage(self, total: int) -> Any:
        """替换掉记录层的 declared_coverage：明细只有一项集合内的 C7-lines，总数由 ``total`` 说了算（大于 1 就是被截断了）。"""

        real = ue._records_module().declared_coverage

        def canned(sample: Any, scope: Any, **kwargs: Any) -> dict[str, Any]:
            result = real(sample, scope, **kwargs)
            return {**result, "ok": False, "undeclared": [{"rule": "C7-lines", "path": "", "kinds": [], "reason": "示例"}],
                    "counts": {**result["counts"], "undeclared": total}}

        return mock.patch.object(ue._records_module(), "declared_coverage", side_effect=canned)

    def test_truncated_findings_fail_closed(self) -> None:
        """明细被截断（总数大于返回的条数）时，看不到的那部分里可能有集合之外的规则：即使拿到的这批全在集合内，也判 failed，不能判『只是证据不完整』。"""

        harness = Harness(self)   # 世界（含登记表自检）要在打补丁之前建好
        with self.canned_coverage(250):
            result, _auditor, outcome = harness.sampled_collect("backend:go-test")
        self.assertEqual((result["status"], result["undeclared_count"]), ("failed", 250))
        self.assertIs(outcome.passed, False, "失败关闭：整次运行判失败")
        self.assertNotIn("declared_not_inheritable", outcome.extra)

    def test_whole_findings_of_the_same_kind_stay_incomplete(self) -> None:
        """对照：总数等于返回的条数（没截断）时，同样的集合内明细仍是 incomplete——截断守卫不影响正常情形。"""

        harness = Harness(self)
        with self.canned_coverage(1):
            result, _auditor, outcome = harness.sampled_collect("backend:go-test")
        self.assertEqual((result["status"], result["undeclared_count"]), ("incomplete", 0))
        self.assertIs(outcome.passed, True)
        self.assertTrue(outcome.extra["declared_not_inheritable"].startswith("declared-sample-incomplete:"))

    def test_the_executor_classifies_a_detached_finding_by_the_rule_name_alone(self) -> None:
        """执行器的归类只认规则名，与记录层怎样算出 C7-detached（阈值多少）无关。记录层的真规则阈值是 64 条（见下一个用例）；这里用一个包装
        declared_coverage 的替身，把阈值故意压到 3 条（标记行超过 3 条就多报一项 C7-detached），所以 5 条标记行只有替身会报——
        走完执行器的整条判定路径：只有这一项→incomplete（记录不可承接、命令结论不变）；和真访问混在一起→failed。"""

        harness = Harness(self)
        records = ue._records_module()
        real = records.declared_coverage

        def with_detached(sample: Any, scope: Any, **kwargs: Any) -> dict[str, Any]:
            result = real(sample, scope, **kwargs)
            if sample["coverage"].get("detached_markers", 0) > 3 and not any(item["rule"] == "C7-detached" for item in result["undeclared"]):
                findings = sorted([*result["undeclared"], {"rule": "C7-detached", "path": "", "kinds": [], "reason": "被丢弃的标记行超过替身的阈值"}],
                                  key=lambda item: (item["rule"], item["path"], item["reason"]))
                return {**result, "ok": False, "undeclared": findings, "counts": {**result["counts"], "undeclared": len(findings)}}
            return result

        unit_id = "backend:go-test"
        flood = lambda raw: raw["coverage"].update(detached_markers=5)   # noqa: E731 - 低于真规则的 64，只有替身会触发
        with mock.patch.object(records, "declared_coverage", side_effect=with_detached):
            calm, *_ = harness.sampled_collect(unit_id)
            result, _auditor, outcome = harness.sampled_collect(unit_id, mutate=flood)
            cwd, tree = harness.world.cwd_of(unit_id), harness.world.tree
            mixed, *_ = harness.sampled_collect(unit_id, lines=harness.world.good_lines(unit_id) + [open_line(cwd, str(tree / "frontend/src/x.ts"))],
                                                mutate=flood)
        self.assertEqual(calm["status"], "passed")
        self.assertEqual((result["status"], result["undeclared_count"], result["incomplete"]["rules"]), ("incomplete", 0, ["C7-detached"]))
        self.assertIs(outcome.passed, True)
        self.assertTrue(outcome.extra["declared_not_inheritable"].startswith("declared-sample-incomplete:C7-detached"))
        self.assertEqual(mixed["status"], "failed")
        self.assertEqual({item["rule"] for item in mixed["undeclared"]}, {"C1-undeclared", "C7-detached"})

    def test_a_detached_marker_flood_makes_the_record_incomplete(self) -> None:
        """记录层的 declared_coverage 的 C7-detached 规则（WP1d）：被丢弃的标记行不超过 64 条照常通过，超过 64 条只让该次记录不可承接，
        和别的违规混在一起则判 failed。规则必须存在——记录层丢了这条规则，本用例**失败**而不是跳过（过去在规则合入之前自动跳过，
        合入之后依然保留跳过逻辑，等于一道随时可以悄悄失效的保护）。"""

        self.assertIn("C7-detached", self.vocabulary(), "记录层的 declared_coverage 必须认识 C7-detached（WP1d）：没有它，大面积被丢弃的标记行无人拦")
        harness = Harness(self)
        unit_id = "backend:go-test"

        def flood(count: int) -> Any:
            return lambda raw: raw["coverage"].update(detached_markers=count)

        result, _auditor, outcome = harness.sampled_collect(unit_id, mutate=flood(64))
        self.assertEqual(result["status"], "passed", "恰好 64 条不触发")
        result, _auditor, outcome = harness.sampled_collect(unit_id, mutate=flood(65))
        self.assertEqual((result["status"], result["undeclared_count"], result["incomplete"]["rules"]), ("incomplete", 0, ["C7-detached"]))
        self.assertIs(outcome.passed, True, "命令结论不变，整次运行不因此失败")
        self.assertTrue(outcome.extra["declared_not_inheritable"].startswith("declared-sample-incomplete:"))
        cwd, tree = harness.world.cwd_of(unit_id), harness.world.tree
        mixed, *_ = harness.sampled_collect(unit_id, lines=harness.world.good_lines(unit_id) + [open_line(cwd, str(tree / "frontend/src/x.ts"))],
                                            mutate=flood(65))
        self.assertEqual(mixed["status"], "failed", "集合内的 C7-detached 和真访问混在一起：failed")

    def test_in_set_rules_each_become_incomplete_and_everything_else_fails_when_the_stream_is_really_collected(self) -> None:
        harness = Harness(self)
        unit_id = "backend:go-test"
        cwd = harness.world.cwd_of(unit_id)
        tree = harness.world.tree
        good = harness.world.good_lines(unit_id)

        def stream(**coverage: Any) -> Any:
            return lambda raw: raw["coverage"].update(**coverage)

        in_set = {
            "C7-stream": dict(mutate=stream(stream_complete=False)),
            "C7-unresolved（乱码行）": dict(lines=good + [GARBAGE]),
            "C7-unresolved（流尾悬着的调用超过上限）": dict(mutate=stream(unresolved=65, unresolved_reasons={"unfinished-calls-at-eof": 65}, unfinished_calls=65)),
            "C7-lines": dict(lines=[]),
            "C7-detached（被丢弃的标记行超过 64 条）": dict(mutate=stream(detached_markers=65)),
        }
        for label, options in in_set.items():
            with self.subTest(label):
                result, _auditor, outcome = harness.sampled_collect(unit_id, **options)
                self.assertEqual((result["status"], result["coverage_complete"], result["undeclared_count"], result["undeclared"]),
                                 ("incomplete", False, 0, []), result)
                self.assertTrue(set(result["incomplete"]["rules"]) <= ue.DECLARED_INCOMPLETE_RULES, result["incomplete"])
                self.assertTrue(outcome.extra["declared_not_inheritable"].startswith("declared-sample-incomplete:"))
                self.assertIs(outcome.passed, True, "命令本身通过、读集只是证明不了：该次执行不判失败")
        outside = {
            "C1-undeclared": dict(lines=good + [open_line(cwd, str(tree / "frontend/src/x.ts"))]),
            "C1-list-undeclared": dict(lines=good + [f"4242 getdents64(3<{tree}/frontend>, 0x1 /* 3 entries */, 32768) = 80"]),
            "C2-outside-undeclared": dict(lines=good + [open_line(cwd, "/opt/secret/key.pem")]),
            "C3-missing-undeclared": dict(lines=good + [f'4242 openat(AT_FDCWD<{cwd}>, "{tree}/frontend/none", O_RDONLY|O_CLOEXEC) = -1 ENOENT (No such file or directory)']),
            "C4-write-declared": dict(lines=good + [write_line(cwd, str(tree / "backend/a.go"))]),
            "C5-network": dict(mutate=lambda raw: raw.update(network=[{"family": "AF_INET", "target": "8.8.8.8:53"}])),
            "C6-head": dict(mutate=lambda raw: raw.update(git_commands=[["git", "show", "HEAD:Makefile"]])),
            "C7-exit": dict(exit_code=2),
        }
        for rule, options in outside.items():
            with self.subTest(rule):
                result, _auditor, outcome = harness.sampled_collect(unit_id, **options)
                self.assertEqual(result["status"], "failed", result)
                self.assertGreaterEqual(result["undeclared_count"], 1)
                self.assertIn(rule.split("（")[0], {item["rule"] for item in result["undeclared"]}, result["undeclared"])
                self.assertNotIn("declared_not_inheritable", outcome.extra)
                self.assertIs(outcome.passed, False, "真违规：记录判失败，整次运行判失败")
        with self.subTest("混合：集合内的证据问题＋集合外的真访问"):
            result, *_ = harness.sampled_collect(unit_id, lines=good + [GARBAGE, open_line(cwd, str(tree / "frontend/src/x.ts"))])
            self.assertEqual(result["status"], "failed")
            self.assertEqual({item["rule"] for item in result["undeclared"]}, {"C1-undeclared", "C7-unresolved"})
        for label, options in (("过滤器的输出不是 JSON", dict(raw_text="{不是 json")), ("过滤器的输出形状不对", dict(raw_text='{"schema_version": "x"}'))):
            with self.subTest(label):
                result, *_ = harness.sampled_collect(unit_id, **options)
                self.assertEqual(result["status"], "failed", "组装证据出错是实现缺陷，响亮失败，不降级")
                self.assertEqual([item["rule"] for item in result["undeclared"]], ["C7-format"])
        with self.subTest("过滤器没产出（被清理或崩溃）"):
            result, *_ = harness.sampled_collect(unit_id, lines="no-raw")
            self.assertEqual(result["status"], "incomplete")
            self.assertTrue(result["incomplete"]["reason"].startswith("filter-output-missing"), result["incomplete"])


class PureHelperTests(unittest.TestCase):
    def test_protocol_constants_match_unit_records_and_entry_gates(self) -> None:
        self.assertEqual(ue.DECLARED_SCOPE_ENTRY, ur.DECLARED_SCOPE_ENTRY)
        self.assertEqual(ue.DECLARED_COVERAGE_SCOPE, ur.DECLARED_COVERAGE_SCOPE)
        self.assertEqual(ue._DECLARED_PROBE_ENV_KEYS, eg.DECLARED_GO_ENV_KEYS)

    def test_aliases_follow_the_gate_environment_and_fall_back_to_an_unused_placeholder(self) -> None:
        env = {"GOCACHE": "/w/go-build-cache", "HOME": "/h", "TMPDIR": "/t"}
        self.assertEqual(ue._declared_aliases(env, tree="/tree", fallback="/out/audit"), {"TREE": "/tree", "WORK": "/w", "HOME": "/h", "TMP": "/t"})
        fallback = ue._declared_aliases({"GOCACHE": "relative/cache", "HOME": "/", "TMPDIR": ""}, tree="/tree", fallback="/out/audit")
        placeholder = "/out/audit/declared-alias-unavailable"
        self.assertEqual((fallback["WORK"], fallback["TMP"]), (placeholder, "/tmp"))
        self.assertEqual(ds.AliasTable(fallback).values["WORK"], placeholder, "占位别名对采样器是合法的")
        self.assertNotEqual(ue._declared_aliases({"GOCACHE": "/x"}, tree="/tree", fallback="/o")["WORK"], "/", "GOCACHE 在根下：别名不能是根，用占位")

    def test_the_run_sample_marks_tracked_paths_and_copies_the_rest_of_the_raw(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = root / "tree"
            _git(root, "init", "-q", str(tree))
            _write(tree, {"backend/go.mod": "module x\n", "backend/sub/a.go": "package a\n"})
            _commit(tree, "初始")
            raw = {"lines": 12, "aliases": {"TREE": str(tree), "WORK": "/w", "HOME": "/h", "TMP": "/t"},
                   "coverage": {"stream_complete": True, "unresolved": 0, "unresolved_reasons": {}, "unfinished_calls": 0, "unresolved_process_events": 0,
                                "lines": 12, "detached_markers": 1},
                   "repo_accesses": [["{TREE}/backend/go.mod", ["read"]], ["{TREE}/backend/sub", ["dir", "stat"]], ["{TREE}/backend/new.go", ["read"]],
                                     ["{TREE}/.git/objects/", ["read"]], ["{TREE}", ["stat"]]],
                   "outside_accesses": [["/usr/local/go/src/", ["read"], 3]], "git_commands": [["git", "show", "a:b"]],
                   "network": [{"family": "AF_UNIX", "target": "/run/x.sock"}]}
            sampler = ds
            sample = ue._declared_sample_from_raw(raw, unit_id="u", launcher=["l"], command=["go", "test"], cwd_rel="backend", exit_code=0, seconds=2.5,
                                                  tracked=sampler.tracked_sets(tree), commit="c" * 40, strace_version="6.8", sampler=sampler,
                                                  records=ue._records_module())
            self.assertEqual([[path, tracked] for path, _kinds, tracked in sample["repo_accesses"]],
                             [["{TREE}/backend/go.mod", True], ["{TREE}/backend/sub", True], ["{TREE}/backend/new.go", False],
                              ["{TREE}/.git/objects/", False], ["{TREE}", True]], "跟踪的文件、含跟踪文件的目录为真；未跟踪与 .git 内部为假")
            self.assertEqual(sample["command"], {"launcher": ["l"], "argv": ["go", "test"], "cwd_rel": "backend",
                                                 "sha256": ur.declared_command_sha256(["go", "test"], "backend")})
            self.assertEqual((sample["exit_code"], sample["valid"], sample["invalid_reasons"]), (0, True, []))
            self.assertEqual(sample["sampled"]["sampler_sha256"], SAMPLER_SHA)
            self.assertEqual(sample["sampled"]["runs"], [{"cache": "formal", "exit_code": 0, "lines": 12, "seconds": 2.5}])
            for key in ("aliases", "coverage", "outside_accesses", "git_commands", "network"):
                self.assertEqual(sample[key], raw[key], key)
            self.assertNotIn("review", sample, "不采信采样器 merge 的评审材料")
            ur._declared_validate_sample(sample)  # 字段闭合、类型合格

    def test_review_only_diagnostics_of_the_raw_go_into_review_and_never_into_the_closed_coverage(self) -> None:
        """过滤器的 raw 带只给评审看的诊断样例：coverage.unresolved_samples、顶层 detached_samples。证据的 coverage 字段集合是闭合的，
        整份照搬会被判 C7-format——它们要按采样器自己的合并做法放进 review；unfinished_samples 是证据认识的可选键，留在 coverage 里。"""

        raw = {"lines": 9, "aliases": {"TREE": "/t", "WORK": "/w", "HOME": "/h", "TMP": "/tmp"},
               "coverage": {"stream_complete": True, "unresolved": 2, "unresolved_reasons": {"unparsed": 1, "unfinished-calls-at-eof": 1},
                            "unfinished_calls": 1, "unresolved_process_events": 0, "lines": 9, "detached_markers": 2,
                            "unresolved_samples": {"unparsed": ["4242 THIS IS GARBAGE ###"]},
                            "unfinished_samples": ["4242 openat(AT_FDCWD<{TREE}>, \"x\" <unfinished ...>"]},
               "detached_samples": ["4242 ???( <detached ...>"],
               "repo_accesses": [], "outside_accesses": [], "git_commands": [], "network": []}
        sample = ue._declared_sample_from_raw(raw, unit_id="u", launcher=[], command=["go", "test"], cwd_rel="backend", exit_code=0, seconds=1.0,
                                              tracked=(set(), set()), commit="c" * 40, strace_version="6.8", sampler=ds, records=ue._records_module())
        ur._declared_validate_sample(sample)  # 以前整份照搬 raw 的 coverage，这里会抛「字段集合是闭合的」
        self.assertEqual(set(sample["coverage"]), set(ue._DECLARED_SAMPLE_COVERAGE_KEYS))
        self.assertEqual(sample["coverage"]["unfinished_samples"], raw["coverage"]["unfinished_samples"])
        self.assertEqual(sample["review"], {"unresolved_samples": {"unparsed": ["4242 THIS IS GARBAGE ###"]},
                                            "detached_samples": ["4242 ???( <detached ...>"]}, "诊断样例随证据封存，事后查得到乱码行长什么样")
        self.assertEqual(set(ue._DECLARED_SAMPLE_COVERAGE_KEYS) - {"detached_markers", "unfinished_samples"},
                         {"stream_complete", "unresolved", "unresolved_reasons", "unfinished_calls", "unresolved_process_events", "lines"})
        closed = {**sample, "coverage": {**sample["coverage"], "unresolved_samples": raw["coverage"]["unresolved_samples"]}}
        with self.assertRaisesRegex(ValueError, "闭合"):
            ur._declared_validate_sample(closed)  # 证明这个键确实不能留在 coverage 里（验证侧的字段集合是闭合的）
        bare = {**raw, "coverage": {key: value for key, value in raw["coverage"].items() if key != "unresolved_samples"}, "detached_samples": []}
        plain = ue._declared_sample_from_raw(bare, unit_id="u", launcher=[], command=["go", "test"], cwd_rel="backend", exit_code=0, seconds=1.0,
                                             tracked=(set(), set()), commit="c" * 40, strace_version="6.8", sampler=ds, records=ue._records_module())
        self.assertNotIn("review", plain, "没有诊断样例就不带 review 键")

    def test_the_side_effect_count_of_the_raw_goes_into_review_only_and_changes_no_verdict(self) -> None:
        """采样器在 raw 顶层新增的 ``detached_side_effects``（被丢弃的垃圾行里名字属于副作用类的条数，有才写）：执行器把它同 ``detached_samples`` 一样放进证据的
        review——验证侧不解释 review，所以覆盖检查的结论与没有这个键时逐项相同；coverage 的字段集合不变（闭合），没有这个键或为 0 时 review 里也没有。"""

        from tools.official_client_capture.tests import test_ci_declared_inputs_records as records_tests

        raw = {"lines": 9, "aliases": {"TREE": "/t", "WORK": "/w", "HOME": "/h", "TMP": "/tmp"},
               "coverage": {"stream_complete": True, "unresolved": 0, "unresolved_reasons": {}, "unfinished_calls": 0, "unresolved_process_events": 0,
                            "lines": 9, "detached_markers": 3},
               "detached_samples": ['711008 lsetxattr(0xffffffffffffff00, "/p", "", 0, 0) = ?', "711006 syscall_0x15a17b(0x1 <detached ...>"],
               "detached_side_effects": 1,
               "repo_accesses": [], "outside_accesses": [], "git_commands": [], "network": []}

        def build(document: dict[str, Any]) -> dict[str, Any]:
            return ue._declared_sample_from_raw(document, unit_id="u", launcher=[], command=["go", "test"], cwd_rel="backend", exit_code=0, seconds=1.0,
                                                tracked=(set(), set()), commit="c" * 40, strace_version="6.8", sampler=ds, records=ue._records_module())

        sample = build(raw)
        ur._declared_validate_sample(sample)
        self.assertEqual(sample["review"], {"detached_samples": raw["detached_samples"], "detached_side_effects": 1})
        self.assertEqual(sample["coverage"], raw["coverage"], "coverage 原样照搬 raw 的（证据认识的）键，没有多也没有少")
        self.assertLessEqual(set(sample["coverage"]), set(ue._DECLARED_SAMPLE_COVERAGE_KEYS), "coverage 的字段集合没变（闭合）")
        self.assertNotIn("detached_side_effects", sample["coverage"])
        self.assertNotIn("detached_side_effects", sample)
        for absent in ({key: value for key, value in raw.items() if key != "detached_side_effects"}, {**raw, "detached_side_effects": 0}):
            without = build(absent)
            self.assertNotIn("detached_side_effects", without["review"], "没有这个键或为 0（采样器不写）时，review 里也没有")
            self.assertEqual(without["coverage"], sample["coverage"])
            self.assertEqual(without["repo_accesses"], sample["repo_accesses"])
        # 计数与样例各自独立地进 review：只有计数没有样例（raw 校验允许）时 review 里只有计数；两者都有时并列。
        only_count = build({**raw, "detached_samples": []})
        self.assertEqual(only_count["review"], {"detached_side_effects": 1})
        self.assertEqual(build({**raw, "detached_side_effects": 3})["review"]["detached_side_effects"], 3, "原样照搬计数，不改写")
        # 覆盖结论不变：同一份范围对「带计数的证据」与「不带计数的证据」给出逐项相同的结果；合格的证据加上这个计数仍然合格。
        scope = records_tests.make_scope()
        plain = records_tests.make_sample()
        counted = {**plain, "review": {"detached_side_effects": 7, "detached_samples": ["711008 lsetxattr(0x1) = ?"]}}
        self.assertEqual(ur.declared_coverage(counted, scope), ur.declared_coverage(plain, scope))
        self.assertIs(ur.declared_coverage(counted, scope)["ok"], True)
        ur._declared_validate_sample(counted)


class WrapTests(unittest.TestCase):
    """``ReadAuditor.wrap`` 的 declared 分支：α' 零开销，β 的命令构造，以及各种降级与放行。"""

    def test_alpha_prime_returns_the_argv_untouched_and_starts_nothing(self) -> None:
        harness = Harness(self)
        auditor = harness.auditor()
        for unit_id in DECLARED_IDS:
            argv = harness.argv_of(unit_id)
            self.assertEqual(auditor.wrap(harness.unit_by_id[unit_id], argv), argv, unit_id)
            self.assertIs(auditor._declared_state[unit_id]["sampled"], False)
        self.assertFalse([path for path in auditor.dir.iterdir()], "α'：不产生任何采样文件")

    def test_beta_builds_launcher_then_strace_then_the_filter_pipe_then_the_shell_wrapped_command(self) -> None:
        harness = Harness(self, launcher="fake", sample_mode="always")
        auditor = harness.auditor()
        unit_id = "backend:go-test"
        unit = harness.unit_by_id[unit_id]
        wrapped = auditor.wrap(unit, harness.argv_of(unit_id))
        launcher = harness.world.launcher
        strace = shutil.which("strace", path=harness.world.environment["PATH"])
        self.assertEqual(wrapped[:len(launcher)], launcher, "启动前缀在最外侧")
        self.assertEqual(wrapped[len(launcher)], strace)
        options = list(ds.STRACE_OPTIONS)
        start = len(launcher) + 1
        self.assertEqual(wrapped[start:start + len(options)], options, "strace 选项沿用采样器的")
        self.assertEqual(wrapped[start + len(options)], "-o")
        pipe = wrapped[start + len(options) + 1]
        self.assertTrue(pipe.startswith("|"))
        parts = pipe[1:].split(" ")
        state = auditor._declared_state[unit_id]
        self.assertEqual(parts[:5], [sys.executable, "-B", str(ds.HERE), "filter", "--output"])
        self.assertEqual(Path(parts[5]), state["raw"])
        self.assertEqual(parts[6:10], ["--unit-id", unit_id, "--chunk-lines", str(ds.DEFAULT_CHUNK_LINES)])
        self.assertEqual(parts[10:], ["--alias", f"TREE={harness.world.tree}", "--alias", f"WORK={harness.world.work}", "--alias", f"HOME={harness.world.home}",
                                      "--alias", f"TMP={harness.world.tmp}"])
        tail = wrapped[start + len(options) + 2:]
        self.assertEqual(tail, ["--", "/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd, *harness.world.commands[unit_id]],
                         "被跟踪的是切目录再 exec 命令的 sh；启动前缀已剥掉，不在 strace 里面重复")
        self.assertEqual((state["launcher"], state["command"], state["cwd_rel"]), (launcher, harness.world.commands[unit_id], "backend"))

    def test_missing_strace_degrades_to_an_incomplete_record_without_failing_the_unit(self) -> None:
        harness = Harness(self, sample_mode="always")
        real_which = shutil.which
        unit_id = "backend:go-test"
        auditor, unit = harness.auditor(), harness.unit_by_id[unit_id]
        with mock.patch.object(ue.shutil, "which", side_effect=lambda name, path=None: None if name == "strace" else real_which(name, path=path)):
            argv = harness.argv_of(unit_id)
            self.assertEqual(auditor.wrap(unit, argv), argv, "缺 strace：不包，照常执行")
        harness.run.preflights[unit_id] = ue.declared_contract_preflight(unit, harness.currents[unit_id])
        outcome = harness.outcome(unit_id)
        auditor.note_outcome(outcome)
        result = auditor.collect(unit)
        self.assertEqual((result["status"], result["coverage_complete"], result["undeclared_count"]), ("incomplete", False, 0))
        self.assertTrue(result["incomplete"]["reason"].startswith("no-strace"))
        self.assertNotIn("trace", result, "没有采样就没有证据")
        outcome.extra["read_audit"] = result
        auditor.declared_finish(harness.running(unit_id), outcome, result)
        self.assertTrue(outcome.extra["declared_not_inheritable"].startswith("declared-sample-incomplete:no-strace"))
        self.assertIs(outcome.passed, True)
        store = ur.RecordStore(harness.world.root / "store-degrade")
        recorder = ue.Recorder(out_dir=harness.world.root / "rec", mode="re-execute", run_id="r1", policy="p" * 64, executor={"files": {}, "sha256": "e" * 64},
                               environment=[], currents=harness.currents, store=store)
        record = recorder.write(harness.running(unit_id), outcome)
        self.assertEqual((record["passed"], record["inheritable"]), (True, False))
        self.assertTrue(record["not_inheritable_reason"].startswith("declared-sample-incomplete:no-strace"))
        self.assertEqual(len(record["declared_contracts"]), 3, "凭证照常带出")

    def test_a_pipe_path_the_shell_cannot_take_degrades_the_same_way(self) -> None:
        harness = Harness(self, sample_mode="always")
        unit_id = "backend:go-test"
        auditor = ue.ReadAuditor(out_dir=harness.world.root / "has space", repo_root=harness.world.tree, data_root=None, currents=harness.currents)
        argv = harness.argv_of(unit_id)
        self.assertEqual(auditor.wrap(harness.unit_by_id[unit_id], argv), argv)
        self.assertTrue(auditor._declared_state[unit_id]["degraded"].startswith("filter-pipe"))
        result = auditor.collect(harness.unit_by_id[unit_id])
        self.assertEqual(result["status"], "incomplete")

    def test_a_command_that_is_not_the_registered_one_is_failed_not_degraded(self) -> None:
        harness = Harness(self, sample_mode="always")
        unit_id = "backend:go-test"
        unit = dataclasses.replace(harness.unit_by_id[unit_id], command=(*harness.unit_by_id[unit_id].command, "extra"))
        auditor = harness.auditor()
        argv = ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd, *unit.command]
        self.assertEqual(auditor.wrap(unit, argv), argv)
        self.assertIn("command_sha256", auditor._declared_state[unit_id]["identity"])
        result = auditor.collect(unit)
        self.assertEqual((result["status"], [item["rule"] for item in result["undeclared"]]), ("failed", ["C7-command"]))

    def test_a_working_directory_outside_the_tree_cannot_be_split_and_fails(self) -> None:
        harness = Harness(self, sample_mode="always")
        unit_id = "backend:go-test"
        unit = dataclasses.replace(harness.unit_by_id[unit_id], cwd=str(harness.world.root))
        auditor = harness.auditor()
        auditor.wrap(unit, ["true"])
        self.assertEqual(auditor.collect(unit)["status"], "failed")

    def test_declared_only_auditor_ignores_every_other_unit(self) -> None:
        harness = Harness(self)
        auditor = harness.auditor(strict=True)
        auditor.declared_only = True
        deploy = harness.unit_by_id["deploy:check"]
        self.assertEqual(auditor.wrap(deploy, ["true"]), ["true"], "只管声明级单元：其它单元不包 strace（严格模式也一样）")
        self.assertTrue(auditor.handles_declared(harness.unit_by_id["backend:go-test"]))
        self.assertFalse(auditor.handles_declared(deploy))

    def test_rerun_only_units_are_never_audited_and_never_fail_the_audit(self) -> None:
        for strict in (False, True):
            with self.subTest(strict=strict):
                harness = Harness(self)
                auditor = harness.auditor(strict=strict)
                deploy = harness.unit_by_id["deploy:check"]
                self.assertEqual(auditor.wrap(deploy, ["true"]), ["true"])
                result = auditor.collect(deploy)
                self.assertEqual((result["status"], result["undeclared_count"], result["coverage_complete"], result["declared_exempt"]),
                                 ("passed", 0, False, "rerun-only"))
                outcome = harness.outcome("deploy:check")
                outcome.extra["read_audit"] = result
                self.assertIs(outcome.passed, True)
                self.assertTrue(ur.derived_pass({"exit_code": 0, "signal": None, "timed_out": False, "unit_type": "command", "passed": True,
                                                 "read_audit": result})[0], "记录层的结论判据也接受这个结果形状")


class GateCurrentsTests(unittest.TestCase):
    """``_gate_currents``：并入合同条目、每次现探测、会话共享、已登记集合。"""

    def count(self, world: World, text: str) -> int:
        return world.go_log.read_text(encoding="utf-8").count(text) if world.go_log.exists() else 0

    def test_declared_units_get_contract_entries_and_others_do_not(self) -> None:
        harness = Harness(self)
        for unit_id in DECLARED_IDS:
            current = harness.currents[unit_id]
            names = [entry["name"] for entry in current.inputs]
            scope = next(entry for entry in current.inputs if entry["name"] == "declared-inputs-scope")["detail"]["scope"]
            self.assertEqual(sorted(name for name in names if name.startswith("contract:")),
                             sorted(f"contract:{contract}" for contract in [*scope["contracts"], *[f"make-expansion:{t}" for t in scope["make_expansion"]]]),
                             unit_id)
            self.assertFalse([name for name in names if name == "head" or name.startswith("repo:")], "没有 head，也没有整仓四段")
            self.assertTrue(current.inheritable)
            self.assertEqual(ur.entries_sha256(current.inputs), current.inputs_sha256)
        deploy = harness.currents["deploy:check"]
        self.assertEqual((deploy.inputs, deploy.inheritable, deploy.reason), (None, False, RERUN_ONLY["deploy:check"]), "rerun-only：清单字段，不探测")

    def test_every_call_probes_again_and_one_call_probes_each_fact_once(self) -> None:
        harness = Harness(self)
        world = harness.world
        self.assertEqual(self.count(world, '"argv": ["env", "-json"'), 1, "三个 Go 单元共用一个探测会话：go env 只探一次")
        self.assertEqual(world.docker_log.read_text(encoding="utf-8").count("image inspect"), 3, "postgres／redis／ryuk 各探一次（integration 单元一个）")
        before = {unit_id: current.inputs_sha256 for unit_id, current in harness.currents.items()}
        again = harness.compute()
        self.assertEqual(self.count(world, '"argv": ["env", "-json"'), 2, "每次算当前事实都重新探测")
        self.assertEqual({unit_id: current.inputs_sha256 for unit_id, current in again.items()}, before, "事实没变：摘要逐字相同")
        world.set_image_id("postgres:18.1-alpine3.23", "sha256:" + "9" * 64)
        changed = harness.compute()
        self.assertNotEqual(changed["backend:integration"].inputs_sha256, before["backend:integration"], "镜像换了：下一次探测看见新值，不沿用旧值")
        for unit_id in ("backend:go-test", "frontend:critical", "egress-spec:egress-spec-go-test"):
            self.assertEqual(changed[unit_id].inputs_sha256, before[unit_id], unit_id)

    def test_the_probe_environment_carries_the_unit_go_variables_only(self) -> None:
        runner = ToggleRunner()
        seen: list[dict[str, str] | None] = []

        def factory(**kwargs: Any) -> Any:
            seen.append(kwargs.get("environ"))
            return eg.declared_probe(runner=runner, **kwargs)

        harness = Harness(self, probe=factory)
        self.assertEqual(len(seen), 2, "go 单元共用一个会话，前端（没有 go 变量）另用一个")
        with_overlay = [env for env in seen if env is not None]
        self.assertEqual(len(with_overlay), 1)
        self.assertEqual({key: with_overlay[0][key] for key in OFFLINE}, OFFLINE)
        self.assertNotIn("GOMAXPROCS", {key for key in with_overlay[0] if key not in harness.world.environment}, "探测环境只叠 go 环境变量，不叠 GOMAXPROCS／CI")
        go = next(entry for entry in harness.currents["backend:go-test"].inputs if entry["name"] == "contract:toolchain:go")
        self.assertEqual(go["detail"]["requirements"], {"met": True, "unmet": {}})

    def test_a_missing_probe_callback_is_a_programming_error_and_a_failed_probe_is_not_an_exception(self) -> None:
        harness = Harness(self)
        token = ue._DECLARED_PROBE.set(None)
        try:
            with self.assertRaisesRegex(ue.ExecutorError, "_DECLARED_PROBE"):
                harness.compute()
        finally:
            ue._DECLARED_PROBE.reset(token)
        runner = ToggleRunner()
        runner.overrides["image inspect"] = (127, "docker: 命令不存在")
        token = ue._DECLARED_PROBE.set(functools.partial(eg.declared_probe, runner=runner))
        try:
            failed = harness.compute()
        finally:
            ue._DECLARED_PROBE.reset(token)
        entry = next(entry for entry in failed["backend:integration"].inputs if entry["name"] == "contract:service-image:postgres")
        self.assertTrue(entry["detail"]["image_id"].startswith("unavailable("), "探测失败写 unavailable，不抛")
        self.assertTrue(failed["backend:integration"].inheritable, "当前事实照算")
        self.assertTrue(any("unavailable" in problem for problem in eg.declared_contract_problems(
            [item for item in failed["backend:integration"].inputs if item["name"].startswith("contract:")])))

    def test_the_registered_set_keeps_declared_and_rerun_only_units_out_of_the_reexecute_policy(self) -> None:
        policy = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}}
        harness = Harness(self, manifest_options={"audit_policy": policy, "plain_units": ("plain:cmd",)})
        plain = harness.currents["plain:cmd"]
        self.assertIn("reexecute-only-policy", [entry["name"] for entry in plain.inputs])
        self.assertEqual((plain.inheritable, plain.reason), (False, "输入合同未闭合：只验证本次执行结果，禁止承接"), "没登记的普通命令单元：旧行为")
        for unit_id in DECLARED_IDS:
            current = harness.currents[unit_id]
            self.assertNotIn("reexecute-only-policy", [entry["name"] for entry in current.inputs], unit_id)
            self.assertTrue(current.inheritable, unit_id)
        deploy = harness.currents["deploy:check"]
        self.assertEqual((deploy.inputs, deploy.inheritable, deploy.reason), (None, False, RERUN_ONLY["deploy:check"]), "rerun-only 的原因不被『输入合同未闭合』覆盖")

    def test_a_dirty_tree_makes_declared_units_uninheritable_without_probing(self) -> None:
        harness = Harness(self)
        (harness.world.tree / "backend" / "stray.go").write_text("package main\n", encoding="utf-8")
        probes = self.count(harness.world, '"argv": ["env", "-json"')
        dirty = harness.compute()
        for unit_id in DECLARED_IDS:
            self.assertIsNone(dirty[unit_id].inputs, unit_id)
            self.assertFalse(dirty[unit_id].inheritable)
            self.assertIn("测试树不干净", dirty[unit_id].reason)
        self.assertEqual(self.count(harness.world, '"argv": ["env", "-json"'), probes, "树不干净：输入不完整，不做合同探测")


def add_history(commits: list[str] | None = None) -> Any:
    """scope_hook：给 backend:go-test 的范围加上 git 历史（固定的历史提交，缺省是世界的基准提交）。"""

    def hook(unit_id: str, scope: dict[str, Any], base_commit: str) -> None:
        if unit_id == "backend:go-test":
            scope["repo"]["git_history"] = {"commits": commits or [base_commit], "reason": "测试用固定历史提交"}

    return hook


class PreflightTests(unittest.TestCase):
    """``declared_contract_preflight``：单元启动前的前置核验与运行前凭证。"""

    def check(self, harness: Harness, unit_id: str, *, unit: Any = None, current: Any = None) -> dict[str, Any]:
        return ue.declared_contract_preflight(unit or harness.unit_by_id[unit_id], current or harness.currents[unit_id])

    @staticmethod
    def by_id(result: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {item["id"]: item for item in result["credentials"]}

    @staticmethod
    def with_env(unit: Any, **values: str) -> Any:
        return dataclasses.replace(unit, env=(*unit.env, *values.items()))

    def test_a_clean_environment_verifies_every_required_contract(self) -> None:
        harness = Harness(self)
        for unit_id in DECLARED_IDS:
            result = self.check(harness, unit_id)
            self.assertEqual(result["problems"], [], unit_id)
            scope = next(entry for entry in harness.currents[unit_id].inputs if entry["name"] == "declared-inputs-scope")["detail"]["scope"]
            required = {name.split(":", 1)[1] for name in scope["contracts"] if name.split(":", 1)[0] in ("cache", "service-image")}
            self.assertEqual(set(self.by_id(result)), required, unit_id)
            for item in result["credentials"]:
                self.assertEqual(set(item), {"id", "kind", "verified_before_run", "observed", "verified_at_utc"} | ({"rule"} if item["kind"] == "cache" else set()))
                self.assertIs(item["verified_before_run"], True, item)
                self.assertRegex(item["verified_at_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        credentials = self.by_id(self.check(harness, "backend:integration"))
        self.assertEqual(credentials["go-build-cache"]["observed"], {"root_ok": True})
        self.assertEqual(credentials["go-build-cache"]["rule"], "any-content-addressed")
        self.assertEqual(credentials["go-mod-cache"]["observed"]["verify"], "ok")
        self.assertEqual(credentials["ryuk"]["observed"]["image_id"].startswith("sha256:"), True)
        node = self.by_id(self.check(harness, "frontend:critical"))["node-modules"]
        self.assertEqual(node["observed"]["installed_lock_sha256"], node["observed"]["lock_file_sha256"])
        self.assertEqual(self.by_id(self.check(harness, "backend:go-test"))["go-tmp"]["rule"], "any-scratch")

    def test_go_test_without_count_1_is_flagged_and_with_it_is_not(self) -> None:
        harness = Harness(self)
        unit = harness.unit_by_id["backend:go-test"]
        bare = dataclasses.replace(unit, command=("go", "test", "./..."))
        self.assertTrue(any("-count=1" in problem for problem in self.check(harness, "backend:go-test", unit=bare)["problems"]))
        carried = dataclasses.replace(unit, command=("go", "test", "./...", "-count=1"))
        self.assertEqual(self.check(harness, "backend:go-test", unit=carried)["problems"], [])

    def test_cache_root_location_failures_are_reported_and_the_credentials_say_so(self) -> None:
        harness = Harness(self)
        world = harness.world
        link = world.work / "link-cache"
        link.symlink_to(world.work / "go-build-cache")
        other = world.root / "elsewhere"
        other.mkdir()
        cases = {
            "符号链接": ({"GOCACHE": str(link)}, "无符号链接的绝对路径"),
            "在测试树内": ({"GOCACHE": str(world.tree / "cache")}, "工作根内、源码树外"),
            "不在工作根内": ({"GOMODCACHE": str(other)}, "工作根内、源码树外"),
            "互相包含": ({"GOTMPDIR": str(world.work / "go-build-cache" / "tmp")}, "互不包含"),
        }
        for label, (overrides, fragment) in cases.items():
            with self.subTest(label):
                unit = self.with_env(harness.unit_by_id["backend:go-test"], **overrides)
                result = self.check(harness, "backend:go-test", unit=unit)
                self.assertTrue(any(fragment in problem for problem in result["problems"]), result["problems"])
                credentials = self.by_id(result)
                self.assertIs(credentials["go-build-cache"]["verified_before_run"], False)
                self.assertIs(credentials["go-build-cache"]["observed"]["root_ok"], False)
        with mock.patch.dict(os.environ):
            os.environ.pop("GOCACHE")
            result = self.check(harness, "backend:go-test")
        self.assertTrue(any("没有 GOCACHE" in problem for problem in result["problems"]), result["problems"])
        # 位置不合规时 go mod verify 不跑（verify 记 skipped），凭证不通过。
        unit = self.with_env(harness.unit_by_id["backend:go-test"], GOCACHE=str(world.tree / "cache"))
        mod = self.by_id(self.check(harness, "backend:go-test", unit=unit))["go-mod-cache"]
        self.assertEqual((mod["verified_before_run"], mod["observed"]["verify"], mod["observed"]["root_ok"]), (False, "skipped", False))

    def test_node_modules_install_lock_must_exist_equal_the_lockfile_and_the_recorded_input(self) -> None:
        harness = Harness(self)
        world = harness.world
        installed = world.tree / "frontend/node_modules/.pnpm/lock.yaml"
        installed.write_text("lockfileVersion: 8\n", encoding="utf-8")
        result = self.check(harness, "frontend:critical")
        self.assertTrue(any("不是同一份" in problem for problem in result["problems"]))
        self.assertIs(self.by_id(result)["node-modules"]["verified_before_run"], False)
        installed.unlink()
        result = self.check(harness, "frontend:critical")
        self.assertTrue(any("读不到" in problem for problem in result["problems"]), result["problems"])
        self.assertEqual(self.by_id(result)["node-modules"]["observed"]["installed_lock_sha256"], "missing")
        installed.write_text(TREE_FILES["frontend/pnpm-lock.yaml"], encoding="utf-8")
        self.assertEqual(self.check(harness, "frontend:critical")["problems"], [])
        # 锁文件摘要与记录输入里的 file: 条目不一致（算当前事实之后锁文件又被换了）。
        current = harness.currents["frontend:critical"]
        forged = [dict(entry, sha256="0" * 64) if entry["name"] == "file:frontend/pnpm-lock.yaml" else entry for entry in current.inputs]
        result = self.check(harness, "frontend:critical", current=dataclasses.replace(current, inputs=forged))
        self.assertTrue(any("不等于输入里 file:frontend/pnpm-lock.yaml" in problem for problem in result["problems"]), result["problems"])

    def test_go_sum_must_match_the_recorded_input_and_go_mod_verify_runs_once_per_cache(self) -> None:
        harness = Harness(self)
        current = harness.currents["backend:go-test"]
        forged = [dict(entry, sha256="0" * 64) if entry["name"] == "file:backend/go.sum" else entry for entry in current.inputs]
        result = self.check(harness, "backend:go-test", current=dataclasses.replace(current, inputs=forged))
        self.assertTrue(any("go.sum 摘要不等于输入里" in problem for problem in result["problems"]), result["problems"])
        verifies = lambda: harness.world.go_log.read_text(encoding="utf-8").count('"argv": ["mod", "verify"]')  # noqa: E731
        before = verifies()
        for unit_id in GO_UNITS:
            self.check(harness, unit_id)
        self.assertLessEqual(verifies() - before, 1, "同一个 GOMODCACHE 与 go.sum：本次执行器调用里只核一次")
        # 换一个 GOMODCACHE 就是另一份缓存，要再核一次。
        other = harness.world.work / "other-mod-cache"
        other.mkdir()
        count = verifies()
        self.check(harness, "backend:go-test", unit=self.with_env(harness.unit_by_id["backend:go-test"], GOMODCACHE=str(other)))
        self.assertEqual(verifies(), count + 1)
        # 每一次 go mod verify（含换了 GOMODCACHE 的那次）都在含 go.mod 的模块根目录 backend/ 里跑，而不是测试树根（独立验收 #5 一般 6）。
        lines = [line for line in harness.world.go_log.read_text(encoding="utf-8").splitlines() if '"argv": ["mod", "verify"]' in line]
        self.assertGreaterEqual(len(lines), 2)
        for line in lines:
            assert_go_mod_verify_ran_in_the_module_root(self, harness.world, line)

    def test_a_go_mod_verify_that_outlives_its_own_timeout_is_cut_off_and_judged_not_verified(self) -> None:
        """go mod verify 有自己的超时（DECLARED_GO_VERIFY_TIMEOUT_SECONDS，每次调用现取）：超过就杀掉子进程、不再等，核验按没通过处理；
        结果同其它结果一样按 GOMODCACHE 记忆，第二个单元不再等第二次。"""

        harness = Harness(self)
        world = harness.world
        world.flag_text("go-verify-sleep", "60")
        verifies = lambda: world.go_log.read_text(encoding="utf-8").count('"argv": ["mod", "verify"]')  # noqa: E731
        with mock.patch.object(ue, "DECLARED_GO_VERIFY_TIMEOUT_SECONDS", 1):
            began = time.monotonic()
            first = self.check(harness, "backend:go-test")
            waited = time.monotonic() - began
            began = time.monotonic()
            second = self.check(harness, "backend:integration")
            waited_again = time.monotonic() - began
        self.assertLess(waited, 30, "超时生效：没有等满 60 秒")
        self.assertLess(waited_again, 30)
        self.assertEqual(verifies(), 1, "第二个单元沿用记忆的超时结果，没有再跑一次")
        for result in (first, second):
            mod = self.by_id(result)["go-mod-cache"]
            self.assertEqual((mod["verified_before_run"], mod["observed"]["verify"], mod["observed"]["root_ok"]), (False, "timeout", True))
            self.assertIn("go mod verify 超过 1 秒还没结束", mod["observed"]["problem"])
            self.assertTrue(any("contract:cache:go-mod-cache" in problem and "go mod verify 超过 1 秒" in problem for problem in result["problems"]),
                            result["problems"])
            self.assertIs(self.by_id(result)["go-build-cache"]["verified_before_run"], True, "别的合同凭证不受影响")
        self.assertEqual(self.check(harness, "frontend:critical")["problems"], [], "不声明 go 缓存合同的单元不受影响")
        # 对照：sleep 不超过超时（并且常量比睡眠长）时，verify 通过，凭证写 ok——超时只因为「卡住」才触发。
        world.flag_text("go-verify-sleep", "0.2")
        other = harness.world.work / "fresh-mod-cache"
        other.mkdir()
        unit = self.with_env(harness.unit_by_id["backend:go-test"], GOMODCACHE=str(other))   # 另一份缓存：不吃记忆
        with mock.patch.object(ue, "DECLARED_GO_VERIFY_TIMEOUT_SECONDS", 30):
            ok = self.by_id(self.check(harness, "backend:go-test", unit=unit))["go-mod-cache"]
        self.assertEqual((ok["verified_before_run"], ok["observed"]["verify"]), (True, "ok"))

    def test_a_missing_go_binary_is_a_failed_verify_not_a_timeout_and_not_an_exception(self) -> None:
        harness = Harness(self)
        unit = harness.unit_by_id["backend:go-test"]
        bare = self.with_env(unit, PATH="/nonexistent-dir-for-test")
        mod = self.by_id(self.check(harness, "backend:go-test", unit=bare))["go-mod-cache"]
        self.assertEqual((mod["verified_before_run"], mod["observed"]["verify"]), (False, "failed"))
        self.assertIn("go mod verify 没有通过", mod["observed"]["problem"])

    def test_the_service_image_id_is_probed_again_just_before_the_unit_starts(self) -> None:
        runner = ToggleRunner()
        harness = Harness(self, probe=functools.partial(eg.declared_probe, runner=runner))
        self.assertEqual(self.check(harness, "backend:integration")["problems"], [])
        runner.overrides["image inspect --format {{.Id}} postgres:18.1-alpine3.23"] = (0, "sha256:" + "8" * 64 + "\n")
        result = self.check(harness, "backend:integration")
        credentials = self.by_id(result)
        self.assertEqual((credentials["postgres"]["verified_before_run"], credentials["postgres"]["observed"]["image_id"]), (False, "sha256:" + "8" * 64))
        self.assertTrue(credentials["redis"]["verified_before_run"])
        self.assertTrue(any("镜像被换过" in problem and "postgres" in problem for problem in result["problems"]), result["problems"])
        runner.overrides["image inspect --format {{.Id}} postgres:18.1-alpine3.23"] = (1, "")
        broken = self.by_id(self.check(harness, "backend:integration"))["postgres"]
        self.assertIs(broken["verified_before_run"], False)
        self.assertTrue(broken["observed"]["image_id"].startswith("unavailable("), "探测失败：凭证不通过，不抛")

    def test_git_history_commits_must_still_resolve_to_the_same_tree(self) -> None:
        runner = ToggleRunner()
        harness = Harness(self, probe=functools.partial(eg.declared_probe, runner=runner), scope_hook=add_history())
        commit = harness.world.base_commit
        result = self.check(harness, "backend:go-test")
        self.assertEqual(result["problems"], [])
        credential = self.by_id(result)[commit]
        self.assertEqual((credential["kind"], credential["verified_before_run"]), ("git-history", True))
        self.assertEqual(set(credential["observed"]), {"commit", "tree"})
        self.assertNotIn("rule", credential)
        runner.overrides["rev-parse --verify"] = (128, "fatal: Needed a single revision")
        result = self.check(harness, "backend:go-test")
        self.assertIs(self.by_id(result)[commit]["verified_before_run"], False)
        self.assertTrue(any(f"contract:git-history:{commit}" in problem for problem in result["problems"]), result["problems"])
        runner.overrides["rev-parse --verify"] = (0, "d" * 40 + "\n")
        self.assertIs(self.by_id(self.check(harness, "backend:go-test"))[commit]["verified_before_run"], False, "树对象变了")

    def test_required_offline_variables_must_really_be_in_the_unit_environment(self) -> None:
        harness = Harness(self)
        unit = harness.unit_by_id["backend:go-test"]
        stripped = dataclasses.replace(unit, env=tuple(pair for pair in unit.env if pair[0] != "GOFLAGS"))
        result = self.check(harness, "backend:go-test", unit=stripped)
        self.assertTrue(any("GOFLAGS=-mod=readonly" in problem for problem in result["problems"]), result["problems"])
        wrong = self.with_env(unit, GOPROXY="https://proxy.golang.org")
        self.assertTrue(any("GOPROXY=off" in problem for problem in self.check(harness, "backend:go-test", unit=wrong)["problems"]))
        self.assertEqual(self.check(harness, "backend:go-test")["problems"], [])

    def test_a_failure_inside_the_check_never_escapes_the_scheduler(self) -> None:
        harness = Harness(self)
        with mock.patch.object(ue, "_declared_preflight_checks", side_effect=RuntimeError("意外")):
            result = self.check(harness, "backend:go-test")
        self.assertEqual(result["credentials"], [])
        self.assertEqual(result["problems"], ["前置核验自身出错：RuntimeError: 意外"])
        token = ue._DECLARED_RUN.set(None)
        try:
            result = self.check(harness, "backend:go-test")
        finally:
            ue._DECLARED_RUN.reset(token)
        self.assertIn("没有声明级运行上下文", result["problems"][0])

    def make_scheduler(self, harness: Harness) -> Any:
        config = ue.load_config(harness.world.config)
        recorder = ue.Recorder(out_dir=harness.world.root / "sched-out", mode="re-execute", run_id="r", policy="p" * 64,
                               executor={"files": {}, "sha256": "e" * 64}, environment=[], currents=harness.currents)
        (harness.world.root / "sched-out").mkdir(exist_ok=True)
        return ue.Scheduler(out_dir=harness.world.root / "sched-out", state_dir=harness.world.root / "sched-state", parallelism=2, machine_cores=2,
                            machine_memory=4096, config=config, policy="p" * 64, start=Path("."), recorder=recorder)

    def test_the_scheduler_preflights_only_formal_declared_command_units(self) -> None:
        harness = Harness(self)
        scheduler = self.make_scheduler(harness)
        unit = harness.unit_by_id["backend:go-test"]
        scheduler._declared_preflight(unit, "diagnostic")
        scheduler._declared_preflight(harness.unit_by_id["deploy:check"], "formal")
        scheduler._declared_preflight(dataclasses.replace(unit, unit_id="not-in-currents"), "formal")
        self.assertEqual(harness.run.preflights, {}, "诊断重跑、rerun-only、不在当前事实里的单元都不做前置核验")
        scheduler._declared_preflight(unit, "formal")
        self.assertEqual(set(harness.run.preflights), {"backend:go-test"})
        self.assertEqual(harness.run.preflights["backend:go-test"]["problems"], [])


class RecorderAndExecutorFilesTests(unittest.TestCase):
    """``Recorder.write`` 带出凭证与不可承接原因；``_executor_files`` 只在含 declared 单元时追加采样器。"""

    def write(self, harness: Harness, unit_id: str, **extra: Any) -> dict[str, Any]:
        store = ur.RecordStore(harness.world.root / f"store-{len(extra)}-{unit_id.replace(':', '_')}")
        recorder = ue.Recorder(out_dir=harness.world.root / "rec", mode="re-execute", run_id="r1", policy="p" * 64, executor={"files": {}, "sha256": "e" * 64},
                               environment=[], currents=harness.currents, store=store)
        outcome = harness.outcome(unit_id)
        outcome.extra.update(extra)
        return recorder.write(harness.running(unit_id), outcome)

    def test_the_credentials_key_exists_only_on_declared_records_that_have_credentials(self) -> None:
        harness = Harness(self)
        credentials = [{"id": "go-tmp", "kind": "cache", "verified_before_run": True, "observed": {"root_ok": True}, "verified_at_utc": STAMP}]
        plain = self.write(harness, "deploy:check")
        self.assertNotIn("declared_contracts", plain)
        declared = self.write(harness, "backend:go-test", declared_contracts=credentials)
        self.assertEqual(declared["declared_contracts"], credentials)
        self.assertIn("declared_contracts", declared)
        for empty in ([], None):
            self.assertNotIn("declared_contracts", self.write(harness, "backend:go-test", declared_contracts=empty), "没有凭证就不加键")
        self.assertEqual(set(declared), set(plain) | {"declared_contracts"}, "有凭证的记录比普通记录恰好多这一个键")

    def test_the_declared_not_inheritable_reason_overrides_the_current_fact_and_later_overrides_win(self) -> None:
        harness = Harness(self)
        record = self.write(harness, "backend:go-test", declared_not_inheritable="declared-contract-preflight:示例")
        self.assertEqual((record["inheritable"], record["not_inheritable_reason"]), (False, "declared-contract-preflight:示例"))
        untouched = self.write(harness, "backend:go-test")
        self.assertEqual((untouched["inheritable"], untouched["not_inheritable_reason"]), (True, None))
        later = self.write(harness, "backend:go-test", declared_not_inheritable="x", observed_failure={"path": "p", "sha256": "s", "reservation": {}})
        self.assertEqual(later["not_inheritable_reason"], "预约中止前已登记测试失败，正式失败结论保留", "原有的覆盖逻辑排在后面，照旧生效")

    def test_the_sampler_joins_the_executor_version_only_when_declared_units_exist(self) -> None:
        helper = Path("/x/bytecode_cache.py")
        sampler = Path(ue.__file__).resolve().parent / "declared_sampler.py"
        self.assertEqual(ue._executor_files(helper), [helper], "没有声明级单元：与改前逐字相同")
        stub = types.SimpleNamespace(sidecar_paths=lambda: [Path("/x/control_read_audit.py"), Path("/x/control_read_audit_runtime.py")])
        token = ue._CONTROL_READ_AUDIT.set(stub)
        try:
            self.assertEqual(ue._executor_files(helper), [helper, Path("/x/control_read_audit.py"), Path("/x/control_read_audit_runtime.py")], "control 适配器：旧行为")
            only_rerun = ue._DECLARED_RUN.set(types.SimpleNamespace(declared=frozenset(), rerun_only=frozenset({"deploy:check"})))
            try:
                self.assertEqual(ue._executor_files(helper)[-1], Path("/x/control_read_audit_runtime.py"), "只有 rerun-only 单元：不追加")
            finally:
                ue._DECLARED_RUN.reset(only_rerun)
            declared = ue._DECLARED_RUN.set(types.SimpleNamespace(declared=frozenset({"backend:go-test"}), rerun_only=frozenset()))
            try:
                self.assertEqual(ue._executor_files(helper), [helper, Path("/x/control_read_audit.py"), Path("/x/control_read_audit_runtime.py"), sampler])
            finally:
                ue._DECLARED_RUN.reset(declared)
        finally:
            ue._CONTROL_READ_AUDIT.reset(token)
        self.assertEqual(ue._executor_files(helper), [helper])
        declared = ue._DECLARED_RUN.set(types.SimpleNamespace(declared=frozenset({"backend:go-test"}), rerun_only=frozenset()))
        try:
            version = ur.executor_version(ue._executor_files(helper))
        finally:
            ue._DECLARED_RUN.reset(declared)
        self.assertEqual(version["files"]["declared_sampler.py"], SAMPLER_SHA, "采样器字节进执行器版本")
        self.assertNotIn("declared_sampler.py", ur.executor_version(ue._executor_files(helper))["files"])


class StrictReportTests(unittest.TestCase):
    """严格审计下的承接边界判定：原判据不改，另认逐次采样证据不完整的 declared 单元与放行的 rerun-only 单元。"""

    def filled(self, harness: Harness) -> Any:
        auditor = harness.auditor(strict=True)
        for unit_id in (*DECLARED_IDS, "deploy:check"):
            auditor.collect(harness.unit_by_id[unit_id])
        return auditor

    def test_declared_passed_results_and_exempt_rerun_only_units_close_the_boundary(self) -> None:
        harness = Harness(self)
        auditor = self.filled(harness)
        self.assertEqual(sorted(auditor.results), sorted([*DECLARED_IDS, "deploy:check"]))
        report = auditor.report()
        self.assertEqual((report["status"], report["coverage_complete"], report["reuse_boundary_enforced"]), ("passed", False, True))
        declared = auditor.declared_extra()["declared"]
        self.assertEqual((declared["passed"], declared["failed"], declared["incomplete"], declared["rerun_only_exempt"]),
                         (sorted(DECLARED_IDS), [], [], ["deploy:check"]))

    def test_an_incomplete_declared_result_does_not_fail_the_strict_report_but_a_failed_one_does(self) -> None:
        harness = Harness(self)
        auditor = self.filled(harness)
        auditor.results["backend:go-test"] = dict(auditor.results["backend:go-test"], status="incomplete", coverage_complete=False, sample_origin="run",
                                                  incomplete={"reason": "示例", "rules": ["C7-lines"], "count": 1})
        report = auditor.report()
        self.assertEqual((report["status"], report["reuse_boundary_enforced"]), ("passed", True), "只因证据不完整：整次运行不因此失败")
        self.assertEqual(auditor.declared_extra()["declared"]["incomplete"], ["backend:go-test"])
        forged = dict(auditor.results["backend:go-test"], undeclared_count=1)
        auditor.results["backend:go-test"] = forged
        self.assertEqual(auditor.report()["status"], "failed", "未声明数不为零的 incomplete 不被认")
        auditor.results["backend:go-test"] = dict(forged, status="failed", undeclared_count=1,
                                                  undeclared=[{"rule": "C1-undeclared", "path": "x", "kinds": [], "reason": "x"}])
        failed = auditor.report()
        self.assertEqual((failed["status"], failed["units_with_findings"], failed["undeclared_total"]), ("failed", ["backend:go-test"], 1))

    def test_the_original_strict_rule_is_untouched_for_every_other_kind_of_result(self) -> None:
        harness = Harness(self)
        auditor = self.filled(harness)
        auditor.results["frontend:critical"] = {"status": "passed", "coverage_complete": False, "coverage_scope": "all-file-paths/v1", "undeclared_count": 0}
        self.assertEqual(auditor.report()["status"], "failed", "覆盖未闭合又不是声明级特例：严格审计照旧不通过")
        auditor = self.filled(harness)
        auditor.results["deploy:check"] = dict(auditor.results["deploy:check"], declared_exempt="something-else")
        self.assertEqual(auditor.report()["status"], "failed", "放行标记只认 rerun-only")
        auditor = self.filled(harness)
        del auditor.results["deploy:check"]
        self.assertEqual(auditor.report()["status"], "failed", "缺结果的单元仍然判失败")
        non_strict = harness.auditor()
        for unit_id in DECLARED_IDS:
            non_strict.collect(harness.unit_by_id[unit_id])
        self.assertEqual(non_strict.report()["status"], "passed")


class SummaryTests(unittest.TestCase):
    """T20：登记数、审计通过数、消费者接受数、实际承接数四个数分开，每个被重跑的单元有原因与耗时。"""

    def test_t20_the_four_numbers_are_kept_apart(self) -> None:
        world = World(self)
        first = world.run()
        self.assertEqual({key: first.summary["declared_inputs"][key] for key in ("registered", "rerun_only", "audit_passed", "consumer_accepted", "inherited", "executed")},
                         {"registered": 4, "rerun_only": 1, "audit_passed": 4, "consumer_accepted": 4, "inherited": 0, "executed": 4})
        world.commit({"backend/a.go": "package main\n// 改\n"})
        second = world.run(mode="full-set-pass")
        declared = second.summary["declared_inputs"]
        self.assertEqual({key: declared[key] for key in ("registered", "rerun_only", "audit_passed", "audit_failed", "audit_incomplete", "consumer_accepted",
                                                        "inherited", "executed", "registry_total", "sample_mode", "consumer_check")},
                         {"registered": 4, "rerun_only": 1, "audit_passed": 3, "audit_failed": 0, "audit_incomplete": 0, "consumer_accepted": 4,
                          "inherited": 1, "executed": 3, "registry_total": 5, "sample_mode": "off", "consumer_check": "checked"})
        self.assertEqual(len({declared["registered"], declared["audit_passed"], declared["consumer_accepted"], declared["inherited"]}), 3,
                         "四个数是不同口径：不能混成一个")
        self.assertEqual(set(declared["reruns"]), {"backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test", "deploy:check"})
        for unit_id, row in declared["reruns"].items():
            self.assertTrue(row["reasons"] and all(isinstance(text, str) and text for text in row["reasons"]), (unit_id, row))
            self.assertIsInstance(row["seconds"], float)
            self.assertGreaterEqual(row["seconds"], 0.0)
        self.assertIn("输入变了：scope:backend", declared["reruns"]["backend:go-test"]["reasons"][0])
        self.assertEqual(declared["reruns"]["deploy:check"]["kind"], "rerun-only")
        self.assertEqual(declared["reruns"]["deploy:check"]["not_inheritable_reason"], RERUN_ONLY["deploy:check"])
        self.assertIn("声明级承接（采样 off）：登记 4 个（只重跑 1 个），本次执行 3 个，审计通过 3、失败 0、证据不完整 0；消费者接受 4；实际承接 1", second.stderr)
        self.assertIn("重跑 backend:go-test（declared，", second.stderr)
        self.assertEqual(declared["consumer_rejected"], {})

    def test_without_a_record_store_the_consumer_check_is_skipped_and_said_so(self) -> None:
        world = World(self)
        out = world.root / "out-nostore"
        subprocess.run([PY, str(EXECUTOR), "run-gates", "--manifest", str(world.manifest_path), "--config", str(world.config),
                        "--weights", str(world.root / "none.json"), "--durations", str(world.root / "none.json"), "--parallel", "4",
                        "--cores", "4", "--state-dir", str(world.root / "state"), "--out-dir", str(out), "--shared-caches", "off"],
                       cwd=world.tree, capture_output=True, text=True, timeout=300, env=world.environment)
        # 没给记录库时清单自检本来就过不了（记录文件名不是摘要，原有行为），退出码不是这里要看的；要看的是声明级的汇总照样写出、并说明没做消费者核验。
        declared = json.loads((out / "summary.json").read_text(encoding="utf-8"))["declared_inputs"]
        self.assertEqual((declared["consumer_check"], declared["consumer_accepted"], declared["audit_passed"]), ("skipped-no-record-store", 0, 4))


class OfflineSwitchTests(unittest.TestCase):
    """entry_gates 的两处极小新增：离线开关＝True；declared Go 单元的清单 env 注入三个离线变量。"""

    def test_the_switch_is_on_and_the_values_are_the_agreed_ones(self) -> None:
        self.assertIs(eg.DECLARED_REQUIRE_OFFLINE, True)
        self.assertEqual(eg.DECLARED_OFFLINE_ENV, OFFLINE)

    def test_declared_contract_problems_report_missing_variables_and_stay_quiet_when_complete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tree = entry_tests.make_tree(root)
            toolchain = entry_tests.Toolchain(root, tree)
            keys = ("GOROOT", "GOTOOLDIR", *eg.DECLARED_GO_ENV_KEYS)
            base = entry_tests.FakeRunner(toolchain.responses())

            def runner(argv: Any, env: Any, cwd: Any) -> tuple[int, str]:
                if tuple(argv) == ("go", "env", "-json", *keys):  # go env 反映它拿到的环境：门禁环境里有没有这三个变量，探测结果就不同
                    values = {"GOROOT": str(toolchain.goroot), "GOTOOLDIR": str(toolchain.tool_dir)}
                    values.update({key: (env or {}).get(key, "") for key in eg.DECLARED_GO_ENV_KEYS})
                    return 0, json.dumps(values)
                return base(argv, env, cwd)

            scope = entry_tests.default_scope(contracts=("toolchain:go",))
            lacking = eg.declared_probe(runner=runner, environ=toolchain.environ, tree=tree)(entry_tests.declaration(scope))
            problems = eg.declared_contract_problems(lacking)
            self.assertTrue(any("离线" in problem for problem in problems), problems)
            self.assertEqual(lacking[0]["detail"]["requires"], {"env_equals": OFFLINE})
            complete = eg.declared_probe(runner=runner, environ={**toolchain.environ, **OFFLINE}, tree=tree)(entry_tests.declaration(scope))
            self.assertEqual(eg.declared_contract_problems(complete), [], "三个变量齐全：不报告")
            self.assertEqual(complete[0]["detail"]["requirements"], {"met": True, "unmet": {}})
            partial = eg.declared_probe(runner=runner, environ={**toolchain.environ, "GOPROXY": "off"}, tree=tree)(entry_tests.declaration(scope))
            self.assertTrue(any("GOFLAGS" in problem for problem in eg.declared_contract_problems(partial)))

    def test_declared_go_units_get_the_three_variables_and_other_units_keep_their_env_verbatim(self) -> None:
        def build(**overrides: Any) -> dict[str, dict[str, Any]]:
            world = World(self)
            manifest = json.loads(world.write_manifest(env_overrides=overrides).read_text(encoding="utf-8"))
            return {unit["unit_id"]: unit for unit in manifest["units"]}

        units = build()
        for unit_id in GO_UNITS:
            expected = {**UNIT_SPECS[unit_id]["env"], **OFFLINE}
            self.assertEqual(units[unit_id]["env"], expected, unit_id)
        self.assertEqual(units["frontend:critical"]["env"], {"NODE_ENV": "test"}, "非 Go 的 declared 单元：逐字不变")
        self.assertNotIn("env", units["deploy:check"], "rerun-only 单元：逐字不变")
        # 同名同值的已有变量是允许的（不冲突）。
        self.assertEqual(build(**{"backend:go-test": {"GOPROXY": "off"}})["backend:go-test"]["env"]["GOPROXY"], "off")
        # 同名不同值：plan 失败，不悄悄覆盖。
        with self.assertRaisesRegex(ValueError, "GOPROXY.*冲突"):
            build(**{"backend:go-test": {"GOPROXY": "https://proxy.golang.org"}})
        # 没有登记表：什么都不注入。
        legacy = World(self, registry=False)
        for unit in json.loads(legacy.manifest_path.read_text(encoding="utf-8"))["units"]:
            self.assertNotIn("GOPROXY", unit.get("env", {}), unit["unit_id"])


# ---------------------------------------------------------------------------
# 第五组：control 四链不变回归（T-C6～T-C12）
# ---------------------------------------------------------------------------


def git_source(relative: str) -> bytes | None:
    try:
        return subprocess.run(["git", "-C", str(REPO_ROOT), "show", f"{BASELINE_COMMIT}:{relative}"], capture_output=True, check=True,
                              stdin=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def load_baseline(relative: str, name: str) -> types.ModuleType | None:
    """基线提交的源码作为独立模块对象：``__file__`` 指向仓库同一位置（同目录的兄弟模块按路径照常加载）。"""

    source = git_source(relative)
    if source is None:
        return None
    module = types.ModuleType(name)
    module.__file__ = str(REPO_ROOT / relative)
    with mock.patch.dict(sys.modules, {name: module}):
        exec(compile(source, f"{BASELINE_COMMIT}:{relative}", "exec"), module.__dict__)  # noqa: S102 - 受控的历史源码
    return module


def require_baseline(relative: str, name: str) -> types.ModuleType:
    """基线提交的源码模块。读不到基线时：本机（环境变量 CI 为空）跳过——浅克隆、离线都可能读不到，不该挡住本机开发；CI 环境（环境变量 CI 非空，
    GitHub Actions 会设）**失败**——CI 的检出是完整历史（fetch-depth: 0），读不到就是配置出了问题，照样跳过的话，control 四链的逐字回归
    会在 CI 上悄悄变成空检查（历史被 squash／rebase 改写之后尤其如此）。"""

    module = load_baseline(relative, name)
    if module is None:
        message = f"读不到基线提交 {BASELINE_COMMIT} 的 {relative}（浅克隆或不在仓库内）"
        if os.environ.get("CI"):
            raise AssertionError(message + "：CI 环境必须读得到基线提交（actions/checkout 要 fetch-depth: 0；基线提交被改写过的话要换成新的 BASELINE_COMMIT），"
                                           "否则 control 四链的逐字回归在 CI 上静默失效")
        raise unittest.SkipTest(message)
    return module


def call_outcome(call: Any) -> tuple[str, Any]:
    """一次调用的结果或报错（异常类名＋消息）：两边逐字比较用（macOS 上 control 的隔离合同会报同一个错）。"""

    try:
        return "ok", call()
    except Exception as error:  # noqa: BLE001 - 比较的就是报错本身
        return type(error).__name__, str(error)


# control 的隔离合同（read_audit_runtime／control_read_audit_runtime 的 prepare）只在具备 unshare 的 Linux 上可用：先看 sys.platform 与 PATH 里有没有
# unshare，再读 /proc/self/ns/{mnt,net}。本机（macOS）与没装这些工具的镜像上 wrap 因此只会报同一个错，成功路径永远没被比较过。
# ControlInvariantTests.linux_isolation 用下面的垫片补上：PATH 里放假的 unshare／strace（可执行文件，wrap 只检查它们在不在，不会真的运行）、
# sys.platform 当作 linux、/proc/self/ns/* 读不到时给一个固定值（读得到时用真实值——CI 上就是真实值）。
FAKE_NAMESPACES = {"/proc/self/ns/mnt": "mnt:[4026531840]", "/proc/self/ns/net": "net:[4026531992]"}
FAKE_NAMESPACE_TOOL = "#!/bin/sh\nexit 0\n"


class ControlInvariantTests(unittest.TestCase):
    """基线提交的执行器与新版并排，对同一批 control 夹具逐字比较（调研 7.4 的 T-C6～T-C12）。所有新分支只由 declared-inputs-scope
    条目触发，control 的声明与条目里永远没有它。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline = require_baseline("tools/ci/unit_executor.py", "unit_executor_wp3_baseline")

    def setUp(self) -> None:
        from tools.ci import control_read_audit as control_audit
        from tools.ci import control_read_audit_runtime as control_runtime
        from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.tree = self.root / "tree"
        _git(self.root, "init", "-q", str(self.tree))
        _write(self.tree, {"a.txt": "a\n"})
        _commit(self.tree, "初始")
        raw = certification.plan_scenario_commands(self.root / "original" / "staging", client="claude")
        raw_path = self.root / "raw-plan.json"
        raw_path.write_text(json.dumps({"schema_version": ue.GATES_SCHEMA, "units": raw["units"], "test_groups": [], "gates": []}), encoding="utf-8")
        self.selected = ["pre-a3:claude:" + name for name in sorted(control_runtime.CONTROL_SCENARIOS)]
        prepared = control_audit.prepare_audit_policy(raw_path, self.selected, str(self.root / "private" / "output"))
        units = [{key: value for key, value in unit.items() if key != "scenario_test_file"} for unit in prepared["plan"]["units"]]
        self.plain = {"unit_id": "plain:cmd", "argv": ["true"], "cwd": str(self.tree),
                      "inputs": {"ranges": [{"category": "tests", "name": "repo:all", "include": [""], "exclude": []}]}}
        units.append(self.plain)
        self.manifest = {"schema_version": ue.GATES_SCHEMA, "profile": "pre-a3", "test_groups": [], "units": units,
                         "gates": [{"gate_id": unit["unit_id"], "units": [unit["unit_id"]]} for unit in units],
                         "audit_policy": prepared["plan"]["audit_policy"]}
        self.manifest_path = self.root / "gates.json"
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")
        self.adapter = eg.control_read_audit_for_plan(self.manifest)
        self.assertIsNotNone(self.adapter, "夹具必须真的选中 control 适配器")
        for module in (ue, self.baseline):
            token = module._CONTROL_READ_AUDIT.set(self.adapter)
            self.addCleanup(module._CONTROL_READ_AUDIT.reset, token)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.chdir(self.tree))
        self.control_ids = list(self.selected)
        self.policy_ids = [unit["unit_id"] for unit in units if unit["unit_id"] not in self.control_ids]
        # 本测试自己的垫片目录：假的 unshare／strace（见 FAKE_NAMESPACES 上面的说明）。只在 linux_isolation() 的窗口里才进 PATH。
        self.shim_bin = self.root / "shim-bin"
        self.shim_bin.mkdir()
        for tool in ("unshare", "strace"):
            install(self.shim_bin / tool, FAKE_NAMESPACE_TOOL)

    @contextlib.contextmanager
    def linux_isolation(self) -> Any:
        """窗口内让 control 四个单元的 ``wrap`` 在任何机器上都走成功路径：PATH 前面放假的 unshare／strace、``sys.platform`` 当作 linux、
        ``/proc/self/ns/*`` 读不到时给固定值（读得到时用真实值）、/dev/null、/dev/tty、/dev/zero、/dev/random、/dev/urandom 的设备号当作 Linux 上的 (1,3)、(5,0)、(1,5)、(1,8)、(1,9)
        （隔离合同在 linux 上核对设备身份，macOS 的设备号不同）。新旧两个执行器在窗口内看到的世界完全一样，所以返回的 argv 必须逐字相同。
        在本来就具备这些条件的 Linux 上，这些替换都是空操作（真实值与替换值相同）。"""

        real_readlink, real_major, real_minor = os.readlink, os.major, os.minor
        char_devices = {os.lstat(path).st_rdev: (major, minor) for path, major, minor in (("/dev/null", 1, 3), ("/dev/tty", 5, 0), ("/dev/zero", 1, 5), ("/dev/random", 1, 8), ("/dev/urandom", 1, 9)) if os.path.exists(path)}

        def readlink(path: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                return real_readlink(path, *args, **kwargs)
            except OSError:
                if str(path) in FAKE_NAMESPACES:
                    return FAKE_NAMESPACES[str(path)]
                raise

        with contextlib.ExitStack() as window:
            window.enter_context(mock.patch.dict(os.environ, {"PATH": f"{self.shim_bin}{os.pathsep}{os.environ.get('PATH', '')}"}))
            window.enter_context(mock.patch.object(sys, "platform", "linux"))
            window.enter_context(mock.patch.object(os, "readlink", readlink))
            window.enter_context(mock.patch.object(os, "major", lambda rdev: char_devices[rdev][0] if rdev in char_devices else real_major(rdev)))
            window.enter_context(mock.patch.object(os, "minor", lambda rdev: char_devices[rdev][1] if rdev in char_devices else real_minor(rdev)))
            yield

    def load(self, module: Any) -> tuple[list[Any], dict[str, Any]]:
        _groups, units, _gates, manifest = module.load_gates_manifest(self.manifest_path, machine_cores=4)
        return units, manifest

    def currents(self, module: Any, manifest_override: dict[str, Any] | None = None) -> dict[str, Any]:
        units, manifest = self.load(module)
        return module._gate_currents(module._records_module(), groups=[], group_units={}, command_units=units,
                                     manifest=manifest_override or manifest, timeout=120.0)

    @staticmethod
    def digest(currents: dict[str, Any]) -> dict[str, Any]:
        return {unit_id: (current.inputs, current.inputs_sha256, current.inheritable, current.reason, current.spec_sha256)
                for unit_id, current in currents.items()}

    def declared_context(self) -> Any:
        """一个声明级运行上下文：同一进程里既有 declared 单元又有 control 单元时，control 单元也不受影响。"""

        run = ue._DeclaredRun(entry=eg, manifest=self.manifest, tree=self.tree, sample_mode="always", declared={"backend:go-test"},
                              rerun_only={"deploy:check"})
        token = ue._DECLARED_RUN.set(run)
        self.addCleanup(ue._DECLARED_RUN.reset, token)
        return run

    def test_t_c7_gate_currents_are_identical_to_the_baseline(self) -> None:
        new, old = self.currents(ue), self.currents(self.baseline)
        self.assertEqual(set(new), set(self.control_ids) | set(self.policy_ids))
        self.assertEqual(self.digest(new), self.digest(old), "inputs／inputs_sha256／inheritable／reason／spec 逐字相同")
        self.assertIn("reexecute-only-policy", [entry["name"] for entry in new["plain:cmd"].inputs], "没登记的普通命令单元仍被追加强制重跑策略")
        self.assertTrue(all("runtime-contract" in [entry["name"] for entry in new[unit_id].inputs] for unit_id in self.control_ids))
        self.assertFalse([entry for current in new.values() for entry in current.inputs or [] if str(entry["name"]).startswith(("contract:", "declared-"))])

    def test_t_c7_a_registry_in_the_plan_changes_nothing_for_the_control_and_plain_units(self) -> None:
        old = self.currents(self.baseline)
        manifest = copy.deepcopy(self.manifest)
        manifest["declared_inputs"] = {"schema_version": eg.DECLARED_SUMMARY_SCHEMA, "declared_units": ["backend:go-test"],
                                       "rerun_only_reasons": {"deploy:check": "实时检查"}, "registered": 2}
        run = self.declared_context()
        new = self.currents(ue, manifest)
        self.assertEqual(self.digest(new), self.digest(old), "登记表只管登记表里的单元，control 与普通单元一字不变")
        self.assertIsNotNone(run)

    def request_file(self, auditor: Any, unit: Any) -> str:
        """这个审计器为这个单元写下的隔离请求文件的内容（control 单元的 wrap 成功时才有；没有就是空串）。"""

        trace = auditor.trace_path(unit)
        path = trace.with_name(trace.name + ".request.json")
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    @staticmethod
    def clear_audit_dir(auditor: Any) -> None:
        """清空审计器的 audit 目录里的文件（目录本身留着：下一次 wrap 要往里写请求文件）。"""

        for child in auditor.dir.iterdir():
            child.unlink()

    def wrap_both(self, new_auditor: Any, old_auditor: Any, unit: Any) -> tuple[tuple[str, Any], tuple[str, Any], str, str]:
        """同一个单元在新旧两个审计器上各 wrap 一次：返回（新的结果, 旧的结果, 新写的请求文件, 旧写的请求文件）。

        **两个审计器必须用同一个 out_dir**：成功的 wrap 返回的 argv 里嵌着该审计器的轨迹路径与请求文件路径（``<out_dir>/audit/<单元名>-<单元 ID 的
        sha256>.trace.json.request.json``，后缀摘要是单元 ID 的 sha256、不是随机数），``--request-sha256`` 又是整份请求（含输出路径）的摘要——
        out_dir 不同则路径与摘要必然不同，逐字比较永远不等（真有 unshare 的 Linux 上，过去给新旧两边用 audit-new／audit-old 的写法
        就红了八个子项）。先后各 wrap 一次，中间清空 audit 目录，旧的看不到新的留下的东西；请求文件每次都覆盖写，所以每次 wrap 之后立刻读。"""

        argv = ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd, *unit.command]
        self.assertEqual(new_auditor.dir, old_auditor.dir, "新旧审计器必须共用 audit 目录，否则路径派生出来的 argv 必然不同")
        new = call_outcome(lambda: new_auditor.wrap(unit, argv))
        new_request = self.request_file(new_auditor, unit)
        self.clear_audit_dir(new_auditor)
        old = call_outcome(lambda: old_auditor.wrap(unit, argv))
        old_request = self.request_file(old_auditor, unit)
        self.clear_audit_dir(old_auditor)
        return new, old, new_request, old_request

    def test_t_c6_wrap_collect_and_report_behave_identically_with_a_declared_run_in_the_process(self) -> None:
        """T-C6：``wrap``／``collect``／``report`` 与基线逐字相同，进程里有没有声明级运行上下文都一样。

        独立验收 #3 一般 5：这条用例过去在没有 unshare／strace 的机器（本机 macOS）上比的是「新旧两边抛同一个 RuntimeContractError」，control 四链
        ``wrap`` 的成功路径从来没被比过；而在有真实 unshare 的 Linux 上，新旧审计器用了不同的 out_dir，成功路径的 argv 里嵌着的轨迹路径与请求摘要必然
        不同，反而红了八个子项（四个 control 单元 × 两种标签）。现在：**无论机器上有没有真工具，窗口内都用自己的假 unshare／strace 垫片放在 PATH
        最前面**（linux_isolation），control 单元的 wrap 总是走成功路径并逐字比较（argv 与写下的请求文件），新旧审计器共用 out_dir。"""

        self.declared_context()
        currents = self.currents(ue)
        units, _manifest = self.load(ue)
        for number, (label, context) in enumerate((("没有声明级上下文", False), ("同进程里有声明级上下文", True))):
            token = None if context else ue._DECLARED_RUN.set(None)
            try:
                with self.linux_isolation():
                    out_dir = self.root / f"audit-{number}"
                    new_auditor = ue.ReadAuditor(out_dir=out_dir, repo_root=self.tree, data_root=None, currents=currents, strict=True)
                    old_auditor = self.baseline.ReadAuditor(out_dir=out_dir, repo_root=self.tree, data_root=None, currents=currents, strict=True)
                    for unit in units:
                        with self.subTest(label=label, unit=unit.unit_id):
                            new, old, new_request, old_request = self.wrap_both(new_auditor, old_auditor, unit)
                            self.assertEqual(new[0], "ok", f"wrap 在垫片窗口里必须成功——不是和基线报同一个异常就算过：{new}")
                            self.assertEqual(new, old, "wrap 返回的 argv 与基线逐字相同")
                            self.assertEqual(new_request, old_request, "写下的请求文件（含请求摘要）与基线逐字相同")
                            if unit.unit_id in self.control_ids:
                                self.assertEqual(new[1][:5], ["unshare", "-m", "-n", "--propagation", "unchanged"], "control 四链是带新网络命名空间的隔离启动前缀")
                                self.assertEqual(json.loads(new_request)["control_scenario"], unit.unit_id.removeprefix("pre-a3:claude:"))
                            else:
                                self.assertEqual(new[1], ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd, *unit.command], "没登记的普通命令单元：原样返回 argv")
                            if unit.unit_id in self.policy_ids:
                                self.assertEqual(new_auditor.collect(unit), old_auditor.collect(unit))
                    new_report = new_auditor.report()
                    new_report_text = Path(new_report["report"]).read_text(encoding="utf-8")   # 两个审计器共用报告路径：新的写完立刻读
                    old_report = old_auditor.report()
                    self.assertEqual({key: value for key, value in new_report.items() if key != "report"},
                                     {key: value for key, value in old_report.items() if key != "report"}, label)
                    self.assertNotIn("declared", json.loads(new_report_text), "没有声明级结果：报告里没有 declared 键")
                    self.assertEqual(new_auditor.declared_extra(), {})
            finally:
                if token is not None:
                    ue._DECLARED_RUN.reset(token)

    def test_t_c6_turns_red_when_the_new_wrap_deviates_by_a_single_argument(self) -> None:
        """变异自检：故意让新实现的 wrap 对 control 单元多一个参数（或改一个、少一个），T-C6 必须变红——证明这个逐字比较是有力的，
        不是「新旧两边碰巧抛同一个异常」或「比了两个空值」。把 T-C6 当子用例嵌套运行，断言它失败，失败的恰是 4 个 control 单元 × 2 种标签。"""

        original = ue.ReadAuditor.wrap
        controls = frozenset(self.control_ids)
        variants = {
            "多一个参数": lambda result: [*result, "--mutant-extra-argument"],
            "改掉一个参数": lambda result: [*result[:4], "private", *result[5:]],
            "少一个参数": lambda result: result[:-1],
        }
        for label, deviate in variants.items():
            def mutated(auditor: Any, unit: Any, argv: list[str], deviate: Any = deviate) -> list[str]:
                result = original(auditor, unit, argv)
                return deviate(result) if unit.unit_id in controls else result

            with self.subTest(label), mock.patch.object(ue.ReadAuditor, "wrap", mutated):
                case = type(self)("test_t_c6_wrap_collect_and_report_behave_identically_with_a_declared_run_in_the_process")
                result = unittest.TestResult()
                case.run(result)
            self.assertFalse(result.wasSuccessful(), f"{label}：新实现的 wrap 偏离基线，T-C6 必须变红")
            self.assertFalse(result.errors, [text for _test, text in result.errors][:1])
            self.assertEqual(len(result.failures), 8, f"{label}：4 个 control 单元 × 2 种标签")
            self.assertTrue(all("wrap 返回的 argv 与基线逐字相同" in text for _test, text in result.failures), label)

    def test_the_shims_really_decide_whether_the_success_path_is_reachable(self) -> None:
        """对照：窗口外（没有 unshare、platform 不是 linux 的机器）wrap 报错；窗口内成功——证明 T-C6 比较的是成功路径，不是自说自话。窗口结束后环境完全
        复原；在本来就是 Linux 的机器上，窗口里的替换（PATH 前置垫片之外）是空操作：能读到的真实值不被改写，真实的命名空间与设备号照旧。"""

        units, _manifest = self.load(ue)
        unit = next(unit for unit in units if unit.unit_id == self.control_ids[0])
        currents = self.currents(ue)
        argv = ["/bin/sh", "-c", 'cd -- "$0" && exec "$@"', unit.cwd, *unit.command]
        auditor = ue.ReadAuditor(out_dir=self.root / "audit-shim", repo_root=self.tree, data_root=None, currents=currents, strict=True)
        platform, path, readlink = sys.platform, os.environ.get("PATH"), os.readlink

        def real_namespace(name: str) -> str | None:
            try:
                return os.readlink(f"/proc/self/ns/{name}")
            except OSError:
                return None

        real = {name: real_namespace(name) for name in ("mnt", "net")}
        with self.linux_isolation():
            self.assertEqual(call_outcome(lambda: auditor.wrap(unit, argv))[0], "ok")
            self.assertEqual(sys.platform, "linux")
            self.assertEqual(shutil.which("unshare"), str(self.shim_bin / "unshare"), "窗口内 PATH 前面是垫片目录")
            self.assertEqual(shutil.which("strace"), str(self.shim_bin / "strace"))
            # 在本来就是 Linux 的机器上，窗口里的替换必须是空操作：真实值读得到就是真实值；读不到才用固定值。
            for name, value in real.items():
                self.assertEqual(os.readlink(f"/proc/self/ns/{name}"), value if value is not None else FAKE_NAMESPACES[f"/proc/self/ns/{name}"], name)
            self.assertEqual((os.major(os.lstat("/dev/null").st_rdev), os.minor(os.lstat("/dev/null").st_rdev)), (1, 3), "/dev/null 按 Linux 的设备号 (1,3)")
            if os.path.exists("/dev/tty"):
                self.assertEqual((os.major(os.lstat("/dev/tty").st_rdev), os.minor(os.lstat("/dev/tty").st_rdev)), (5, 0), "/dev/tty 按 Linux 的设备号 (5,0)")
            link = self.root / "probe-link"
            link.symlink_to("probe-target")
            self.assertEqual(os.readlink(link), "probe-target", "不在垫片名单里的路径照常走真实的 readlink")
            with self.assertRaises(OSError):
                os.readlink(self.root / "no-such-link")
        self.assertEqual((sys.platform, os.environ.get("PATH"), os.readlink), (platform, path, readlink), "窗口结束后环境复原")
        if not (sys.platform.startswith("linux") and shutil.which("unshare")):
            outcome = call_outcome(lambda: auditor.wrap(unit, argv))
            self.assertEqual((outcome[0], "unshare" in outcome[1]), ("RuntimeContractError", True), "窗口外：没有隔离工具的机器上只会报错")

    def forged_manifest(self, rerun_only: dict[str, str]) -> dict[str, Any]:
        manifest = copy.deepcopy(self.manifest)
        manifest["declared_inputs"] = {"schema_version": eg.DECLARED_SUMMARY_SCHEMA, "declared_units": [], "rerun_only_reasons": rerun_only,
                                       "registered": len(rerun_only)}
        return manifest

    def test_control_units_in_the_rerun_only_reasons_are_not_declared_units_and_change_nothing(self) -> None:
        """独立验收 #3 重要 2：plan 阶段禁止登记 pre-a3:* 单元，但执行器自己不能只靠上游挡——清单顶层 declared_inputs.rerun_only_reasons 若带了 control
        单元的 ID，旧代码里 ReadAuditor._declared_exempt 会放过它们（wrap 不再包隔离启动前缀、collect 返回 not-observed 的 passed），control 四链的
        严格审计被静默跳过。现在：清单层（_declared_unit_ids）和运行层（_declared_exempt）各挡一次，行为与基线逐字相同。"""

        control = {unit_id: "伪造的 rerun-only 登记" for unit_id in self.control_ids}
        forged = self.forged_manifest({**control, "plain:cmd": "真正登记的 rerun-only 单元"})
        # 第一道：清单层。control 单元从两个集合里丢掉，没登记表的 control 计划就没有声明级上下文。
        declared, rerun_only = ue._declared_unit_ids(forged)
        self.assertEqual((declared, rerun_only), (set(), {"plain:cmd"}))
        self.assertEqual(ue._declared_unit_ids(self.forged_manifest(control)), (set(), set()), "只有 control 的假登记：等于没有登记表")
        for adapter in (self.adapter, None):   # 适配器在位时认它的 CONTROL_UNIT_IDS；不在位（计划没选中 control）时靠 pre-a3: 前缀兜底
            token = ue._CONTROL_READ_AUDIT.set(adapter)
            try:
                self.assertEqual(ue._declared_unit_ids(forged), (set(), {"plain:cmd"}))
            finally:
                ue._CONTROL_READ_AUDIT.reset(token)
        # 登记的 declared 单元（输入声明带 declared_scope）同理：control 的丢掉，别的保留。
        with_declared = copy.deepcopy(forged)
        with_declared["units"].append({"unit_id": self.control_ids[0], "argv": ["true"], "cwd": str(self.tree), "inputs": {"declared_scope": {}}})
        with_declared["units"].append({"unit_id": "backend:go-test", "argv": ["true"], "cwd": str(self.tree), "inputs": {"declared_scope": {}}})
        self.assertEqual(ue._declared_unit_ids(with_declared), ({"backend:go-test"}, {"plain:cmd"}))
        # control 单元的当前事实（gate_currents）与基线逐字相同，不受假登记影响（假登记里真正登记的 plain:cmd 照旧按登记表走，不拿来比）。
        new_digest, old_digest = self.digest(self.currents(ue, forged)), self.digest(self.currents(self.baseline))
        for unit_id in self.control_ids:
            self.assertEqual(new_digest[unit_id], old_digest[unit_id], unit_id)

        units, _manifest = self.load(ue)
        currents = self.currents(ue, forged)
        control_units = [unit for unit in units if unit.unit_id in self.control_ids]
        plain = next(unit for unit in units if unit.unit_id == "plain:cmd")
        run = ue._DeclaredRun(entry=eg, manifest=forged, tree=self.tree, sample_mode="off", declared=declared, rerun_only=rerun_only)
        token = ue._DECLARED_RUN.set(run)
        try:
            with self.linux_isolation():
                new_auditor = ue.ReadAuditor(out_dir=self.root / "audit-shared", repo_root=self.tree, data_root=None, currents=currents, strict=True)
                old_auditor = self.baseline.ReadAuditor(out_dir=self.root / "audit-shared", repo_root=self.tree, data_root=None, currents=currents, strict=True)
                for unit in control_units:
                    with self.subTest(layer="清单层", unit=unit.unit_id):
                        new, old, new_request, old_request = self.wrap_both(new_auditor, old_auditor, unit)
                        self.assertEqual(new[0], "ok")
                        self.assertEqual((new, new_request), (old, old_request), "wrap：仍然包隔离启动前缀，与基线逐字相同")
                        self.assertEqual(new_auditor.collect(unit), old_auditor.collect(unit), "collect：严格审计的结果（没有轨迹就是失败）与基线逐字相同")
                self.assertTrue(new_auditor._declared_exempt(plain), "对照：真正登记的 rerun-only 单元照旧被放行")
                self.assertEqual({key: value for key, value in new_auditor.report().items() if key != "report"},
                                 {key: value for key, value in old_auditor.report().items() if key != "report"}, "汇总逐字相同")
        finally:
            ue._DECLARED_RUN.reset(token)

        # 第二道：运行层。声明级上下文可以不经清单直接构造（独立验收的最小复现就这样造的），_declared_exempt 自己也要挡。
        direct = ue._DeclaredRun(entry=eg, manifest={}, tree=self.tree, sample_mode="off", declared=set(), rerun_only=set(self.control_ids) | {"plain:cmd"})
        token = ue._DECLARED_RUN.set(direct)
        try:
            with self.linux_isolation():
                new_auditor = ue.ReadAuditor(out_dir=self.root / "audit-direct", repo_root=self.tree, data_root=None, currents=currents, strict=True)
                old_auditor = self.baseline.ReadAuditor(out_dir=self.root / "audit-direct", repo_root=self.tree, data_root=None, currents=currents, strict=True)
                for unit in control_units:
                    with self.subTest(layer="运行层", unit=unit.unit_id):
                        self.assertFalse(new_auditor._declared_exempt(unit))
                        new, old, new_request, old_request = self.wrap_both(new_auditor, old_auditor, unit)
                        self.assertEqual(new[0], "ok")
                        self.assertEqual((new, new_request), (old, old_request))
                        self.assertEqual(new_auditor.collect(unit), old_auditor.collect(unit))
                        self.assertNotIn("declared_exempt", new_auditor.results[unit.unit_id])
                self.assertTrue(new_auditor._declared_exempt(plain))
        finally:
            ue._DECLARED_RUN.reset(token)

    def test_the_control_governed_test_takes_the_union_of_the_adapter_set_and_the_pre_a3_prefix(self) -> None:
        governed = ue._declared_control_governed
        for unit_id in self.control_ids:
            self.assertTrue(governed(unit_id), unit_id)
        self.assertTrue(governed("pre-a3:vc-chain.something-else"), "任何 pre-a3 单元都不是声明级单元（登记表自检也禁止登记它们）")
        for unit_id in ("backend:go-test", "frontend:critical", "deploy:check", "plain:cmd", "xpre-a3:claude:vc-chain.claude-reconcile", "", "pre-a3"):
            self.assertFalse(governed(unit_id), unit_id)
        for not_text in (None, 3, ["pre-a3:x"], {"pre-a3:x": 1}):
            self.assertFalse(governed(not_text), not_text)
        # 适配器的集合与前缀取并：适配器说它管辖一个不带 pre-a3: 前缀的 ID，也要认；适配器是缺这个属性的桩时当空集合，前缀照认。
        stub = types.SimpleNamespace(CONTROL_UNIT_IDS=frozenset({"odd:adapter-governed"}))
        token = ue._CONTROL_READ_AUDIT.set(stub)
        try:
            self.assertTrue(governed("odd:adapter-governed"))
            self.assertTrue(governed("pre-a3:claude:anything"))
            self.assertFalse(governed("odd:other"))
        finally:
            ue._CONTROL_READ_AUDIT.reset(token)
        bare = ue._CONTROL_READ_AUDIT.set(types.SimpleNamespace(sidecar_paths=lambda: []))
        try:
            self.assertFalse(governed("odd:adapter-governed"))
            self.assertTrue(governed("pre-a3:claude:anything"))
        finally:
            ue._CONTROL_READ_AUDIT.reset(bare)

    def test_t_c8_control_records_have_the_same_keys_and_values_as_the_baseline(self) -> None:
        currents = self.currents(ue)
        units, _manifest = self.load(ue)
        for unit in units:
            if unit.unit_id not in (self.control_ids[0], "plain:cmd"):
                continue
            records = []
            for module in (ue, self.baseline):
                recorder = module.Recorder(out_dir=self.root / "rec", mode="re-execute", run_id="r1", policy="p" * 64, executor={"files": {}, "sha256": "e" * 64},
                                           environment=[], currents=currents)
                running = ue.Running(unit, 1, 0.0, self.root / "unit.log", self.root / "unit.result.json", self.root / f"unit-{len(records)}.record.json", "formal")
                audit = {"status": "passed", "coverage_complete": True, "coverage_scope": "all-file-paths/v1", "undeclared_count": 0, "undeclared": [],
                         "inputs_sha256": currents[unit.unit_id].inputs_sha256, "repo_root": str(self.tree), "data_root": None,
                         "trace": {"path": str(self.root / "t.json"), "sha256": "a" * 64}}
                outcome = ue.Outcome(unit=unit, kind="formal", exit_code=0, signal=None, timed_out=False, seconds=1.0, cpu_seconds=0.5, max_rss_mb=1.0,
                                     orphans=[], log_path=self.root / "unit.log", result=None, extra={"read_audit": audit, "reexecute_only": False},
                                     started_at_utc="2026-10-15T00:00:00Z", completed_at_utc="2026-10-15T00:00:01Z")
                records.append(recorder.write(running, outcome))
            self.assertEqual(sorted(records[0]), sorted(records[1]), "键集合与基线相同")
            self.assertNotIn("declared_contracts", records[0])
            self.assertEqual(records[0], records[1], unit.unit_id)

    def test_t_c9_executor_files_and_the_control_record_executor_do_not_change(self) -> None:
        helper = Path("/x/bytecode_cache.py")
        self.assertEqual(ue._executor_files(helper), self.baseline._executor_files(helper))
        self.assertEqual(ue._executor_files(helper)[1:], list(self.adapter.sidecar_paths()), "control：helper 加两个固定 sidecar，没有采样器")
        self.declared_context()
        self.assertEqual(ue._executor_files(helper)[:-1], self.baseline._executor_files(helper), "声明级运行：旧列表之后只多一个采样器")
        self.assertEqual(ue._executor_files(helper)[-1].name, "declared_sampler.py")
        token = ue._DECLARED_RUN.set(None)
        try:
            version = ur.executor_version(ue._executor_files(helper))
        finally:
            ue._DECLARED_RUN.reset(token)
        self.assertNotIn("declared_sampler.py", version["files"])
        self.assertEqual(version, ur.executor_version(self.baseline._executor_files(helper)))

    def test_t_c10_the_adapter_selection_still_recognises_only_the_four_control_ids(self) -> None:
        self.assertIs(eg.control_read_audit_for_plan(self.manifest), self.adapter)
        with_registry = copy.deepcopy(self.manifest)
        with_registry["declared_inputs"] = {"schema_version": eg.DECLARED_SUMMARY_SCHEMA, "declared_units": ["backend:go-test"],
                                            "rerun_only_reasons": {"deploy:check": "实时检查"}, "registered": 2}
        with_registry["units"].append({"unit_id": "backend:go-test", "argv": ["true"], "cwd": str(self.tree),
                                       "inputs": {"declared_scope": {"schema_version": eg.DECLARED_SCOPE_SCHEMA, "repo": {}}, "declared_sample": {}}})
        self.assertIs(eg.control_read_audit_for_plan(with_registry), self.adapter, "含 declared 登记的清单：仍选中同一个 control 适配器")
        only_declared = {"audit_policy": {"units": {"backend:go-test": {"host_paths": [], "require_read_audit": True}}}, "units": with_registry["units"]}
        self.assertIsNone(eg.control_read_audit_for_plan(only_declared), "audit_policy 里没有四个 control ID：不选")
        self.assertIsNone(eg.control_read_audit_for_plan({"units": with_registry["units"], "declared_inputs": with_registry["declared_inputs"]}))
        self.assertEqual(sorted(unit for unit in self.control_ids), sorted(
            "pre-a3:claude:" + name for name in ("vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause", "vc-chain.claude-tool-evolution")))

    def test_t_c11_the_control_sidecar_bytes_are_pinned(self) -> None:
        for name, digest in (("control_read_audit.py", "dc9238ae696880cd12eb17a246a09ac354d6db330b788b66ecf35c3142da2172"),
                             ("control_read_audit_runtime.py", "c779562bd4a09319ebb1a3d6ade888f65405c464386976a5192dd43bee824010")):
            self.assertEqual(hashlib.sha256((REPO_ROOT / "tools" / "ci" / name).read_bytes()).hexdigest(), digest,
                             f"{name} 的字节不得改动：改一个字节，历史 control 凭证重放就报「当前四control执行器缺完整sidecar绑定或实际字节漂移」")

    def test_t_c12_declared_code_never_touches_the_audit_module(self) -> None:
        def declared_nodes(tree: ast.Module, markers: tuple[str, ...], extra: frozenset[str] = frozenset()) -> list[ast.AST]:
            found: list[ast.AST] = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and (any(marker in node.name.lower() for marker in markers) or node.name in extra):
                    found.append(node)
            return found

        executor_tree = ast.parse((REPO_ROOT / "tools/ci/unit_executor.py").read_text(encoding="utf-8"))
        nodes = declared_nodes(executor_tree, ("declared",), frozenset({"note_outcome"}))
        names = {node.name for node in nodes}
        self.assertTrue({"_DeclaredRun", "declared_contract_preflight", "_declared_wrap", "_declared_collect", "declared_finish", "_declared_preflight",
                         "_declared_run_summary", "note_outcome"} <= names, names)
        for node in nodes:
            for child in ast.walk(node):
                if isinstance(child, ast.Name):
                    self.assertNotEqual(child.id, "_audit_module", f"{getattr(node, 'name', '?')} 里不得引用 _audit_module")
                if isinstance(child, ast.Attribute):
                    self.assertNotEqual(child.attr, "_audit_module")
                    self.assertFalse(child.attr == "module" and isinstance(child.value, ast.Name) and child.value.id == "self",
                                     f"{getattr(node, 'name', '?')} 里不得用 self.module（审计模块或 control 适配器）")
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    self.assertNotIn("read_audit", ast.dump(child), "声明级代码不导入 read_audit")
                if isinstance(child, ast.Constant) and isinstance(child.value, str):
                    self.assertNotIn("read_audit.py", child.value)
        records_tree = ast.parse((REPO_ROOT / "tools/ci/unit_records.py").read_text(encoding="utf-8"))
        # unit_records 里 declared_inputs 是既有函数（control 的宿主快照路径合法地用审计模块），声明级新增的是下面这些前缀的函数。
        added = [node for node in ast.walk(records_tree) if isinstance(node, ast.FunctionDef)
                 and node.name.startswith(("_declared", "declared_", "validate_declared", "_check_declared")) and node.name != "declared_inputs"]
        self.assertGreaterEqual(len(added), 10)
        for node in added:
            for child in ast.walk(node):
                self.assertFalse(isinstance(child, ast.Name) and child.id == "_audit_module", f"unit_records 的 {node.name} 里不得引用 _audit_module")

    def test_the_additions_are_pure_additions_apart_from_the_registered_lines(self) -> None:
        import difflib

        registered = {
            "tools/ci/unit_executor.py": [
                "    return [helper, *(adapter.sidecar_paths() if adapter is not None else [])]",
                # 以下 29 行是审计器加固（步骤 10 开发循环 WP-R5）对单元进程清理的必要改写，纯新增之外的被改写行逐组登记理由：
                # ①abort（WP-R5「进程树快照与后代清理」）：原先逐个在跑单元按 pid 与它所在会话的成员 SIGKILL 再 waitpid(pid, 0)，单元里的测试自己另起会话的后代
                # （嵌套的 run-unit 等）逃过清理；改成先取全部在跑单元的整棵子树快照、冻结、统一 SIGKILL（_kill_items），再有界地回收根进程（_reap_roots，
                # 第二次 Ctrl-C 落在取快照阶段时，原来无条件 waitpid 会永久阻塞），所以原来这 6 行被替换。
                "        for pid in list(self.running):",
                "            for member in [pid, *_session_members(pid)]:",
                "                    os.kill(member, signal.SIGKILL)",
                "            with contextlib.suppress(ChildProcessError):",
                "                os.waitpid(pid, 0)",
                "            self.running.pop(pid, None)",
                # ②_clean_orphans：参数由会话号 session_id 改成单元 item，残留成员改由 _survivors(item) 取（会话成员加另起会话的后代，按启动标记、名册和 ppid 子树找），
                # 宽限期后改为先 SIGTERM、仍在则冻结后统一 SIGKILL（_kill_items），原来按会话号逐信号循环的 9 行被替换；返回值仍是宽限期后仍存活的进程号。
                "    def _clean_orphans(self, session_id: int, *, interrupted: bool = False) -> list[int]:",
                "        members = _session_members(session_id)",
                "            members = _session_members(session_id)",
                "        for sig in (signal.SIGTERM, signal.SIGKILL):",
                "            for pid in _session_members(session_id):",
                "                with contextlib.suppress(ProcessLookupError, PermissionError):",
                "                    os.kill(pid, sig)",
                "            if sig == signal.SIGTERM and _session_members(session_id):",
                "        return members",
                # ③单元超时：注释同步（SIGKILL 的对象由进程组改为整棵子树）；30 秒宽限常量化为 TIMEOUT_KILL_GRACE_SECONDS，到期改调 _kill_items 杀整棵子树，
                # 原来只对进程组 os.killpg(SIGKILL)，另起会话的后代不受影响。
                "                # 单元自带超时（命令单元）优先，否则用调度配置的统一超时；先 SIGTERM，30 秒后仍在就 SIGKILL。",
                "                elif item.timed_out and elapsed > limit + 30:",
                "                    with contextlib.suppress(ProcessLookupError, PermissionError):",
                "                        os.killpg(pid, signal.SIGKILL)",
                # ④reap：调用 _clean_orphans 的实参由 pid 改为单元 item，随②的签名变化。
                "                orphans = self._clean_orphans(pid, interrupted=item.interruption is not None)",
                # ⑤预约中止后的活跃进程核对：原先自己读 /proc/<成员>/stat 判僵尸，只看原会话的成员；改成复用 _survivors(item)（含另起会话的后代，僵尸与已退出的不算），
                # 核对口径只会更严（多算另起会话的后代），失败仍是同一个 ExecutorError。
                "                    # 无论原单元是否已经失败，都须核对会话清理后才能授予整机。",
                "                    active = []",
                "                    for member in _session_members(pid):",
                "                        try:",
                "                            state = (Path('/proc') / str(member) / 'stat').read_text().rsplit(')', 1)[-1].split()[0]",
                "                        except FileNotFoundError:",
                "                            continue",
                "                        if state not in ('Z', 'X'):",
                "                            active.append(member)",
            ],
            "tools/ci/entry_gates.py": [
                "DECLARED_REQUIRE_OFFLINE = False",
                # 以下 9 行是步骤 5 WP3b 对独立验收「一般 2（make -n 展开探测并非只读）」的必要改写，逐条登记理由：
                # ①说明不能再声称「必须只读」——make 自己并不保证 -n 只读，改成「尽量只读、保守扫描挡住已知会执行的写法」。
                "# make -n 展开探测必须只读：下面这些写法会在 make 解析期执行任意命令或写文件，出现就不做展开合同（单元转 rerun-only）。",
                # ②③扫描函数加 targets 形参：执行期的写法（+ 行、$(MAKE)）只扫声明目标的先决条件闭包，不让无关目标挡住所有展开合同；首行文档同步。
                "def _declared_make_problem(tree: Path) -> str | None:",
                '    """测试树的 Makefile 能不能做 make -n 展开合同：含解析期求值的写法（见 _DECLARED_MAKE_UNSAFE）就不能，返回原因；可以返回 None。',
                # ④⑤⑥⑦apply_declared_inputs：文档去掉「保证永远只读」；原先整份 Makefile 只扫一次的缓存，改成按声明目标集合各扫一次。
                "      声明了 make 展开合同、而 Makefile 含解析期求值写法的，转为 rerun-only（保证展开探测永远只读）；",
                "    make_problem: str | None = None",
                "            if make_problem is None:",
                '                make_problem = _declared_make_problem(tree) or ""',
                # ⑧⑨_make_expansion：说明改成保守扫描的真实保证范围；探测环境清空 MAKEFLAGS／MAKEFILES，调用行随之改写。
                '        只读保证由 plan 阶段的 Makefile 扫描提供（见 _declared_make_problem）。"""',
                '        rc, out = self.ctx.run(["make", "-s", "--no-print-directory", "-n", target], cwd=self.tree, env=self.environ)',
            ],
        }
        for relative, expected in registered.items():
            old = git_source(relative)
            self.assertIsNotNone(old, relative)
            new = (REPO_ROOT / relative).read_text(encoding="utf-8").splitlines()
            removed = [line[1:] for line in difflib.unified_diff(old.decode("utf-8").splitlines(), new, lineterm="", n=0)
                       if line.startswith("-") and not line.startswith("---")]
            self.assertEqual(removed, expected, f"{relative}：相对基线提交 {BASELINE_COMMIT[:9]}，被改写或删掉的行只有登记的这几行（其余全是新增）。"
                                                f"若是后续批次合法改写了这个文件，请把 BASELINE_COMMIT 换成该批次之后的提交，并重新确认 control 四链的回归（本组其余用例）")


class BaselineRequirementTests(unittest.TestCase):
    """基线提交读不到时（独立验收 #3 一般 5②）：本机跳过，CI（环境变量 CI 非空）失败——CI 上悄悄跳过会让 control 四链的逐字回归静默变成空检查。"""

    @staticmethod
    def unreadable() -> Any:
        return mock.patch.object(sys.modules[__name__], "load_baseline", return_value=None)

    @staticmethod
    def raised(call: Any) -> BaseException | None:
        """调用抛出的异常（没抛返回 None）。SkipTest 也当成可断言的结果：在 assertRaises 里抛出 SkipTest 会让整个用例变成「跳过」而不是失败，
        「CI 上悄悄跳过」恰恰是要拦的退化，不能让断言自己被它吞掉。"""

        try:
            call()
        except BaseException as error:  # noqa: BLE001 - 要断言的就是抛出了什么
            return error
        return None

    def test_in_ci_an_unreadable_baseline_is_a_failure_for_the_whole_control_group(self) -> None:
        for value in ("true", "1", "yes", "false", "x"):   # 只看「非空」：GitHub Actions 设的是 true，别的非空值同样按 CI 处理
            with self.subTest(CI=value), mock.patch.dict(os.environ, {"CI": value}), self.unreadable():
                for label, call in (("require_baseline", lambda: require_baseline("tools/ci/unit_executor.py", "baseline_probe")),
                                    ("ControlInvariantTests.setUpClass", ControlInvariantTests.setUpClass)):   # 整组：setUpClass 失败＝类里所有用例都报错
                    error = self.raised(call)
                    self.assertIsInstance(error, AssertionError, f"{label}：CI 上读不到基线必须失败，不能跳过（{error!r}）")
                    self.assertIn("CI 环境必须读得到基线提交", str(error))

    def test_locally_an_unreadable_baseline_skips(self) -> None:
        for label, ci in (("没有 CI 变量", None), ("CI 为空字符串", "")):
            environment = {key: value for key, value in os.environ.items() if key != "CI"}
            if ci is not None:
                environment["CI"] = ci
            with self.subTest(label), mock.patch.dict(os.environ, environment, clear=True), self.unreadable():
                with self.assertRaises(unittest.SkipTest):
                    require_baseline("tools/ci/unit_executor.py", "baseline_probe")
                with self.assertRaises(unittest.SkipTest):
                    ControlInvariantTests.setUpClass()

    def test_a_readable_baseline_is_returned_in_both_environments(self) -> None:
        for label, environment in (("CI", {"CI": "true"}), ("本机", {})):
            with self.subTest(label), mock.patch.dict(os.environ, environment):
                if label == "本机":
                    os.environ.pop("CI", None)
                module = require_baseline("tools/ci/unit_executor.py", "unit_executor_wp3_baseline_probe")
                self.assertTrue(hasattr(module, "ReadAuditor"))
                self.assertFalse(hasattr(module, "_declared_unit_ids"), "基线是声明级承接之前的执行器")


# ---------------------------------------------------------------------------
# 第六组：逐单元承接（unit-inherit）模式与严格审计策略下的端到端
# ---------------------------------------------------------------------------


class UnitInheritEndToEndTests(unittest.TestCase):
    """B5a-08 逐单元承接在真实执行器进程里走一遍：声明级单元的记录经 排队前／锁后／发布前 三个复核点承接，消费端重验也通过。"""

    def setUp(self) -> None:
        from tools.ci import unit_inherit as ui

        self.ui = ui
        self.fs = ui.fs
        self.world = World(self)
        self.control = self.world.root / "data" / "control"
        self.control.mkdir(parents=True)
        (self.control / "codex-e2e-supervisor-enable-20261007t000000z.json").write_text(
            json.dumps({"status": "passed", "tool_files_sha256": "f" * 64}), encoding="utf-8")
        self.clock = self.world.root / "clock" / "sample.json"
        self.clock.parent.mkdir()

    def sample(self) -> None:
        self.clock.write_text(json.dumps({"status": "synchronized", "source": "替身校时源", "offset_seconds": 0,
                                          "sampled_at_utc": self.fs.timestamp(time.time())}), encoding="utf-8")

    def inherit_args(self, request: Path | None = None) -> list[str]:
        return ["--unit-inherit-client", "codex", "--unit-inherit-control", str(self.control)] + (
            ["--unit-inherit-request", str(request)] if request is not None else [])

    def request(self, name: str) -> Path:
        """执行器只判定写提案 → 校时样本、撤销查询、两份批准 → 组装请求。"""

        world, ui, fs = self.world, self.ui, self.fs
        proposal = world.root / f"proposal-{name}"
        proposed = world.run("--decide-only", "--unit-inherit-propose", str(proposal), *self.inherit_args(), mode="unit-inherit", expect_summary=False)
        self.assertEqual(proposed.code, 0, proposed.stderr[-2000:])
        binding = json.loads((proposal / "binding.json").read_text(encoding="utf-8"))
        sources = json.loads((proposal / "sources.json").read_text(encoding="utf-8"))
        now = time.time()
        self.sample()
        clock = world.root / f"request-clock-{name}.json"
        clock.write_text(json.dumps({"status": "synchronized", "source": "替身校时源", "offset_seconds": 0, "sampled_at_utc": fs.timestamp(now)}), encoding="utf-8")
        revocation = world.root / f"request-revocation-{name}.json"
        revocation.write_text(json.dumps(ui.query_revocation(world.store, sorted(sources["runs"]), "pre-ledger", now)), encoding="utf-8")
        approvals = []
        for role in sorted(fs.APPROVAL_ROLES):
            path = world.root / f"approval-{name}-{role}.json"
            path.write_text(json.dumps({"role": role, "status": "approved", "account": "替身审核人", "scope": ui.CONTRACT,
                                        "binding_sha256": binding["binding_sha256"], "approved_at_utc": fs.timestamp(now - 3600),
                                        "expires_at_utc": fs.timestamp(now + 86400)}), encoding="utf-8")
            approvals.append(path)
        output = world.root / f"request-{name}.json"
        ui.assemble_request(proposal, clock=clock, revocation=revocation, approvals=approvals, reuse_enabled=True, output=output,
                            clock_source=f"file:{self.clock}")
        return output

    def inherit(self, name: str) -> Run:
        request = self.request(name)
        self.sample()
        run = self.world.run(*self.inherit_args(request), mode="unit-inherit")
        self.assertEqual(run.code, 0, run.stderr[-2500:])
        self.assertEqual(run.manifest["mode"], "unit-inherit", run.summary.get("unit_inherit_decision"))
        return run

    def test_an_unrelated_change_inherits_every_declared_unit_and_the_published_proof_verifies(self) -> None:
        world = self.world
        self.assertEqual(world.run().code, 0)
        world.commit({UNRELATED_CLAUDE_TEST: PASS + "# 无关\n"})
        second = self.inherit("first")
        self.assertEqual((second.inherited, second.executed), (set(DECLARED_IDS), set(RERUN_ONLY)))
        self.assertEqual(second.summary["unit_inherit_decision"]["action"], "inherit-matching-units")
        self.assertEqual([item["checkpoint"] for item in second.summary["unit_inherit_decision"]["checkpoints"]], list(self.ui.CHECKPOINTS))
        declared = second.summary["declared_inputs"]
        self.assertEqual((declared["inherited"], declared["consumer_accepted"], declared["executed"]), (4, 4, 0))
        self.sample()
        consumed = subprocess.run([PY, str(REPO_ROOT / "tools/ci/unit_inherit.py"), "verify", "--manifest", second.summary["unit_manifest"]["path"],
                                   "--store", str(world.store), "--purpose", "e2e"], capture_output=True, text=True, env=world.environment)
        self.assertEqual(consumed.returncode, 0, consumed.stdout + consumed.stderr)
        self.assertEqual(json.loads(consumed.stdout)["result"]["inherited"], len(DECLARED_IDS), "消费端重验：声明级记录过 verify_published（历史重放）")

    def test_run_origin_sources_are_inherited_too_and_a_backend_change_reruns_only_what_it_touches(self) -> None:
        world = self.world
        world.plan_good_strace()
        self.assertEqual(world.run("--declared-sample", "always").code, 0)
        world.commit({"backend/a.go": "package main\n// 改\n"})
        second = self.inherit("second")
        self.assertEqual(second.inherited, {"frontend:critical"})
        self.assertEqual(second.executed, {"backend:go-test", "backend:integration", "egress-spec:egress-spec-go-test", "deploy:check"})
        self.assertEqual(second.summary["declared_inputs"]["audit_passed"], 3)
        # 这一轮（缺省 off）写下的是 registry 来源的记录；再改无关文件，四个都能承接——run 来源（上一轮的前端）与 registry 来源混在一起也行。
        world.commit({UNRELATED_CLAUDE_TEST: PASS + "# 又一次无关\n"})
        third = self.inherit("third")
        self.assertEqual(third.inherited, set(DECLARED_IDS))


# 引导代码模板：在执行器取得调度锁之前（锁排队期间事实变化）把 postgres 的镜像 Id 换掉，造出「锁后复核重算的当前事实与排队前不同」。
# 换掉哪个文件写在代码里（@FILE@），不走环境变量——环境变量会进环境指纹，多一个变量就让承接上下文与请求登记的不同。
LOCK_TIME_IMAGE_SWAP = (
    "_real_lock = ue.hold_scheduler_lock\n"
    "def _hooked_lock(*args, **kwargs):\n"
    "    open(@FILE@, 'w').write('sha256:' + '9' * 64 + chr(10))\n"
    "    return _real_lock(*args, **kwargs)\n"
    "ue.hold_scheduler_lock = _hooked_lock\n")
# 替身 full_set_receipt：只记录 assess 收到的请求里 reuse_enabled 是什么，判定恒为全部重跑。
STUB_FULL_SET_RECEIPT = """
import json, os
from types import SimpleNamespace

def read(path):
    return {"schema_version": "full-set-request/v1", "reuse_enabled": True, "receipt": None}

def live_check(request, **kwargs):
    return None

def assess(request, currents, facts, *, gate_ids, now):
    with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"reuse_enabled": request["reuse_enabled"], "units": len(currents)}) + "\\n")
    verdict = {"schema_version": "full-set-decision/v1", "decided_at_utc": "2026-10-15T00:00:00Z", "eligible": False,
               "reuse_enabled": request["reuse_enabled"] is True, "action": "reexecute-all", "reasons": ["替身：全部重跑"],
               "actor": {"effective_uid": os.geteuid(), "host": "stub"}, "source_receipt": None, "source_run_id": None, "expires_at_utc": None}
    return verdict, {unit_id: SimpleNamespace(inherit=False, reasons=["替身：全部重跑"], record=None, record_path=None) for unit_id in currents}
"""
# 替身 full_set_receipt（能真承接的版本）：逐单元用执行器自己的 records.evaluate 判定（等价于 full-set-pass 不带请求时的逐单元承接，
# 也就是 B-10 的「承接匹配的单元」），每次被调用把承接的单元记进日志。记录库路径与日志路径写在代码里（@STORE@、@LOG@），不走环境变量
# （环境变量会进环境指纹，多一个变量所有来源记录都不可承接）。
STUB_B09_RECEIPT = """
import dataclasses, json, sys

records = sys.modules["unit_records_sibling"]   # 执行器已加载的记录层，同一个实例
STORE, LOG = @STORE@, @LOG@

def read(path):
    return {"schema_version": "full-set-request/v1", "reuse_enabled": True, "receipt": None}

def live_check(request, **kwargs):
    return None

def assess(request, currents, facts, *, gate_ids, now):
    store = records.RecordStore(STORE)
    judged = dataclasses.replace(facts, require_read_audit=True, max_age_hours=24, now=now, historical_read_audit=False)
    decisions = {unit_id: records.evaluate(store, current, judged) for unit_id, current in currents.items()}
    inherited = sorted(unit_id for unit_id, decision in decisions.items() if decision.inherit)
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"inherited": inherited}) + chr(10))
    verdict = {"schema_version": "full-set-decision/v1", "decided_at_utc": "2026-10-15T00:00:00Z", "eligible": bool(inherited), "reuse_enabled": True,
               "action": "inherit-matching-units" if inherited else "reexecute-all", "reasons": [], "actor": {"effective_uid": 0, "host": "stub"},
               "source_receipt": None, "source_run_id": None, "expires_at_utc": None}
    return verdict, decisions
"""
STUB_RECEIPT_SWAP = (
    "import importlib.util\n"
    "_real_spec = importlib.util.spec_from_file_location\n"
    "def _swapped_spec(name, location=None, *args, **kwargs):\n"
    "    if name == '_full_set_receipt':\n"
    "        location = os.environ['STUB_RECEIPT']\n"
    "    return _real_spec(name, location, *args, **kwargs)\n"
    "importlib.util.spec_from_file_location = _swapped_spec\n")


class AuditorFactsSyncTests(unittest.TestCase):
    """锁后复核重算了当前事实之后，挂在调度器上的审计器拿的必须是新事实（独立验收 #3 一般 8）。

    报告怀疑：B-09 锁后复核里新增的 ``_declared_sync_auditor`` 只同步 ``declared_only`` 的审计器，``--audit-reads`` 建的常规审计器在审声明级单元时，
    复核重算 currents 之后审计结果里的 ``inputs_sha256`` 仍是旧的（读记录的消费者会拒绝，失败关闭）。结论：**B-09／B-10 分支里触发不了**——
    进入该分支要求判定里有可承接的单元，那只有 ``--mode full-set-pass`` 才有；而 ``--audit-reads`` 与 full-set-pass 互斥（要真跑才有读集）。
    **唯一可达的锁后复核＋常规审计器是逐单元承接分支，那里一直有同步（auditor.currents = currents）**。下面把这些前提和同步行为都钉住。"""

    def fixture(self) -> UnitInheritEndToEndTests:
        helper = UnitInheritEndToEndTests("test_an_unrelated_change_inherits_every_declared_unit_and_the_published_proof_verifies")
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        return helper

    def test_the_sync_helper_updates_only_the_auditor_built_for_declared_units(self) -> None:
        old, new = {"u": "旧事实"}, {"u": "新事实"}
        declared_only, regular = types.SimpleNamespace(currents=old, declared_only=True), types.SimpleNamespace(currents=old, declared_only=False)
        ue._declared_sync_auditor(types.SimpleNamespace(auditor=declared_only), new)
        ue._declared_sync_auditor(types.SimpleNamespace(auditor=regular), new)
        ue._declared_sync_auditor(types.SimpleNamespace(auditor=None), new)
        ue._declared_sync_auditor(types.SimpleNamespace(), new)
        self.assertIs(declared_only.currents, new, "只为声明级单元建的审计器：换成新事实")
        self.assertIs(regular.currents, old, "常规审计器由调用处自己同步（逐单元承接分支），这个函数不碰它")

    def recheck(self, *audit_args: str) -> None:
        fixture = self.fixture()
        world = fixture.world
        first = world.run()
        self.assertEqual(first.code, 0, first.stderr[-2000:])
        source = world.record_of(first, "backend:integration")
        world.commit({UNRELATED_CLAUDE_TEST: PASS + "# 无关\n"})
        request = fixture.request("lock")
        fixture.sample()
        swap = world.image_override_file("postgres:18.1-alpine3.23")
        run = world.run(*fixture.inherit_args(request), *audit_args, mode="unit-inherit", bootstrap=LOCK_TIME_IMAGE_SWAP.replace("@FILE@", repr(str(swap))))
        self.assertEqual(run.code, 0, run.stderr[-3000:])
        self.assertEqual(run.manifest["mode"], "unit-inherit", run.summary.get("unit_inherit_decision"))
        self.assertEqual(run.inherited, {"backend:go-test", "frontend:critical", "egress-spec:egress-spec-go-test"}, "排队前复核时大家都能承接，锁后复核把换了镜像的挤出来")
        self.assertEqual(run.executed, {"backend:integration", "deploy:check"})
        record = world.record_of(run, "backend:integration")
        self.assertNotEqual(record["inputs_sha256"], source["inputs_sha256"], "锁后复核重算的当前事实确实变了（镜像 Id 被换过）")
        audit = record["read_audit"]
        self.assertEqual(audit["inputs_sha256"], record["inputs_sha256"], "审计器用的是锁后复核重算的事实：审计绑定的输入摘要等于记录的")
        self.assertEqual((audit["status"], audit["coverage_complete"], record["inheritable"]), ("passed", True, True))
        credential = {item["id"]: item for item in record["declared_contracts"]}["postgres"]
        self.assertEqual((credential["verified_before_run"], credential["observed"]["image_id"]), (True, "sha256:" + "9" * 64))
        declared = run.summary["declared_inputs"]
        self.assertEqual((declared["inherited"], declared["audit_passed"], declared["consumer_accepted"], declared["consumer_rejected"]), (3, 1, 4, {}),
                         "消费者接受重跑出来的这条记录；审计摘要不一致的话这里会被拒")
        if "--audit-reads" in audit_args:
            self.assertEqual(run.summary["read_audit"]["status"], "passed")
            self.assertIn("backend:integration", run.summary["read_audit"]["declared"]["passed"])
        else:
            self.assertNotIn("read_audit", run.summary, "没给 --audit-reads：只有只为声明级单元建的审计器，汇总里没有旧的 read_audit 段")

    def test_a_regular_audit_reads_auditor_holds_the_lock_time_facts_after_the_unit_inherit_recheck(self) -> None:
        self.recheck("--audit-reads")

    def test_the_declared_only_auditor_holds_the_lock_time_facts_after_the_unit_inherit_recheck(self) -> None:
        self.recheck()

    def test_audit_reads_cannot_be_combined_with_the_mode_in_which_the_b09_recheck_runs(self) -> None:
        """B-09／B-10 的锁后复核只在 full-set-pass 模式下才会进（别的模式没有可承接单元）；--audit-reads 在这个模式下直接报错退出——
        两个前提合起来，常规审计器和 B-09 锁后复核不会同时存在。谁放宽了 --audit-reads 的模式限制，这里先红。"""

        world = World(self)
        missing = str(world.root / "no-such-request.json")
        for label, extra in (("全集通过", ()), ("B-09 请求", ("--full-set-request", missing)), ("B-10 请求", ("--target-platform-request", missing))):
            with self.subTest(label):
                run = world.run("--audit-reads", *extra, mode="full-set-pass", expect_summary=False)
                self.assertEqual(run.code, 2, run.stderr[-1500:])
                self.assertIn("读集审计要真跑才有读集", run.stderr)
        # 对照：re-execute 模式下 --audit-reads 照常可用。
        self.assertEqual(world.run("--audit-reads").code, 0)

    def test_a_b09_request_is_assessed_with_reuse_switched_off_in_every_mode_but_full_set_pass(self) -> None:
        """前提的另一半：非 full-set-pass 模式下执行器把请求里的 reuse_enabled 压成 False 再交给评估器，评估器在 reuse 关闭时一律判全部重跑
        （真实评估器的行为见 full_set_receipt.assess：reuse 关闭就抛 ContractError「真实承接开关关闭」），所以这些模式下 B-09 锁后复核分支不进。
        这里用替身评估器只看执行器交给它的请求。"""

        world = World(self)
        stub, log, request = world.root / "stub_full_set_receipt.py", world.root / "stub.log", world.root / "request.json"
        stub.write_text(STUB_FULL_SET_RECEIPT, encoding="utf-8")
        request.write_text("{}", encoding="utf-8")
        for mode, expected in (("re-execute", False), ("full-set-pass", True)):
            with self.subTest(mode=mode):
                log.unlink(missing_ok=True)
                run = world.run("--full-set-request", str(request), mode=mode, bootstrap=STUB_RECEIPT_SWAP,
                                env={"STUB_RECEIPT": str(stub), "STUB_LOG": str(log)})
                self.assertEqual(run.code, 0, run.stderr[-2000:])
                seen = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
                self.assertEqual([item["reuse_enabled"] for item in seen], [expected], f"{mode}：评估器收到的请求里 reuse_enabled")
                self.assertEqual(run.executed, set(DECLARED_IDS) | set(RERUN_ONLY), "替身判全部重跑：所有单元都执行")

    def test_the_b09_lock_time_recheck_gives_the_declared_only_auditor_the_new_facts(self) -> None:
        """B-09 的锁后复核分支（full-set-pass＋请求）里 ``_declared_sync_auditor`` 同步的就是只为声明级单元建的审计器——这个分支里只可能有它。
        用替身评估器（按 records.evaluate 逐单元判定，等价于 B-10 的「承接匹配的单元」）走这条分支：排队前复核时四个声明级单元都可承接，取得调度锁之前
        镜像 Id 被换掉，锁后复核重算的当前事实里 backend:integration 变了、被挤出承接集合，重新执行；它的读集审计必须绑定锁后复核的新事实。"""

        world = World(self)
        first = world.run()
        self.assertEqual(first.code, 0, first.stderr[-2000:])
        source = world.record_of(first, "backend:integration")
        world.commit({UNRELATED_CLAUDE_TEST: PASS + "# 无关\n"})
        stub, log, request = world.root / "stub_b09_receipt.py", world.root / "stub-b09.log", world.root / "request.json"
        stub.write_text(STUB_B09_RECEIPT.replace("@STORE@", repr(str(world.store))).replace("@LOG@", repr(str(log))), encoding="utf-8")
        request.write_text("{}", encoding="utf-8")
        swap = world.image_override_file("postgres:18.1-alpine3.23")
        bootstrap = (STUB_RECEIPT_SWAP.replace("os.environ['STUB_RECEIPT']", repr(str(stub))) + LOCK_TIME_IMAGE_SWAP.replace("@FILE@", repr(str(swap))))
        run = world.run("--full-set-request", str(request), mode="full-set-pass", bootstrap=bootstrap)
        self.assertEqual(run.code, 0, run.stderr[-3000:])
        calls = [json.loads(line)["inherited"] for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(calls, [sorted(DECLARED_IDS), sorted(set(DECLARED_IDS) - {"backend:integration"})],
                         "评估器被调用两次：排队前四个声明级单元都承接；锁后复核时 backend:integration 的当前事实变了，被挤出")
        self.assertEqual(run.inherited, set(DECLARED_IDS) - {"backend:integration"})
        self.assertEqual(run.executed, {"backend:integration", "deploy:check"})
        record = world.record_of(run, "backend:integration")
        self.assertNotEqual(record["inputs_sha256"], source["inputs_sha256"], "锁后复核重算的当前事实确实变了")
        self.assertEqual(record["read_audit"]["inputs_sha256"], record["inputs_sha256"], "审计器用的是锁后复核重算的事实")
        declared = run.summary["declared_inputs"]
        self.assertEqual((declared["inherited"], declared["audit_passed"], declared["consumer_accepted"], declared["consumer_rejected"]), (3, 1, 4, {}))
        self.assertNotIn("read_audit", run.summary, "full-set-pass 下不允许 --audit-reads：这里只有只为声明级单元建的审计器")


class StrictPolicyEndToEndTests(unittest.TestCase):
    """严格审计加审计策略（control 四链所在的运行形态）下，declared、rerun-only 与没登记的普通命令单元同处一次运行。"""

    def test_declared_exempt_and_policy_units_share_one_strict_run(self) -> None:
        policy = {"schema_version": "unit-audit-policy/v1", "default": "reexecute-only", "units": {}}
        world = World(self)
        world.manifest_path = world.write_manifest(audit_policy=policy, plain_units=("plain:cmd",))
        run = world.run("--audit-reads", "--audit-strict")
        self.assertEqual(run.code, 0, run.stderr[-3000:])
        audit = run.summary["read_audit"]
        self.assertEqual((audit["status"], audit["coverage_complete"], audit["reuse_boundary_enforced"]), ("passed", False, True))
        self.assertEqual(audit["reexecute_required_units"], ["plain:cmd"], "没登记的普通命令单元：旧行为，强制重跑")
        self.assertEqual(audit["declared"]["rerun_only_exempt"], ["deploy:check"])
        self.assertEqual(audit["declared"]["passed"], sorted(DECLARED_IDS))
        plain, deploy = world.record_of(run, "plain:cmd"), world.record_of(run, "deploy:check")
        self.assertEqual((plain["inheritable"], deploy["inheritable"]), (False, False))
        self.assertEqual(deploy["not_inheritable_reason"], RERUN_ONLY["deploy:check"], "rerun-only 的原因没被『输入合同未闭合』覆盖")
        self.assertIn("reexecute-only-policy", [entry["name"] for entry in plain["inputs"]])
        self.assertNotIn("reexecute-only-policy", [entry["name"] for entry in world.record_of(run, "backend:go-test")["inputs"]])
        for unit_id in DECLARED_IDS:
            self.assertEqual(world.record_of(run, unit_id)["read_audit"]["coverage_complete"], True, unit_id)
        self.assertEqual(run.summary["declared_inputs"]["audit_passed"], 4)
        # 声明级单元的审计失败在严格运行里同样判失败（不被特例吞掉）。
        broken = World(self)
        broken.manifest_path = broken.write_manifest(audit_policy=policy, plain_units=("plain:cmd",))
        relative = broken.registry_units["backend:go-test"]["sample"]["evidence"]
        broken.commit({relative: (broken.tree / relative).read_text(encoding="utf-8").replace('"exit_code": 0', '"exit_code": 0 ')})
        failed = broken.run("--audit-reads", "--audit-strict")
        self.assertEqual(failed.code, 1)
        self.assertEqual(failed.summary["read_audit"]["status"], "failed")
        self.assertEqual(failed.summary["read_audit"]["declared"]["failed"], ["backend:go-test"])


class GitHistoryEndToEndTests(unittest.TestCase):
    def test_git_history_contract_flows_through_inputs_credentials_and_the_consumer(self) -> None:
        world = World(self, scope_hook=add_history())
        first = world.run()
        self.assertEqual(first.code, 0, first.stderr[-2000:])
        record = world.record_of(first, "backend:go-test")
        names = [entry["name"] for entry in record["inputs"]]
        self.assertIn(f"contract:git-history:{world.base_commit}", names)
        credential = {item["id"]: item for item in record["declared_contracts"]}[world.base_commit]
        self.assertEqual((credential["kind"], credential["verified_before_run"]), ("git-history", True))
        self.assertEqual(credential["observed"]["tree"], _git(world.tree, "rev-parse", f"{world.base_commit}^{{tree}}").strip())
        self.assertEqual(first.summary["declared_inputs"]["consumer_accepted"], 4)
        world.commit({"docs/other.md": "x\n"})
        second = world.run(mode="full-set-pass")
        self.assertEqual(second.inherited, set(DECLARED_IDS), "HEAD 移动、新提交都不让历史合同失效")


def run_without_declared_units_summary(case: unittest.TestCase) -> dict[str, Any]:
    """没有登记表的世界跑一次带读集审计的 run-gates：汇总里不应出现任何声明级的键。"""

    world = World(case, registry=False)
    run = world.run("--audit-reads")
    case.assertEqual(run.code, 0, run.stderr[-2000:])
    case.assertNotIn("declared_inputs", run.summary)
    return run.summary


if __name__ == "__main__":
    unittest.main()
