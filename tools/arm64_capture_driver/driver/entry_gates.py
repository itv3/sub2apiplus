#!/usr/bin/env python3
"""入口门禁（E2-04）：把一组 make 子检查或整份入口门禁交给统一调度执行器（``tools/ci/unit_executor.py``）一次运行。

子命令：

* ``make-checks``：``make check-egress-spec``／``check-egress-spec-ci`` 的实际执行方式。Makefile 把这两个目标拆成一组
  互不依赖的子检查目标（``EGRESS_SPEC_CHECKS``），这里把每个子检查展开成一个命令单元（``make --no-print-directory
  <子检查>``），在整机额度内并行执行：一项失败其余照跑，全部跑完再汇总，失败项逐个列出并附日志尾部。
  退出码与执行器相同：0 全部通过、1 有子检查失败、2 用法或配置错误。

执行记录目录：环境变量 ``UNIT_EXECUTOR_OUT_DIR`` 给出时放在它下面的 ``<名称>`` 子目录（与同一 make 进程里的采集工具
测试记录分开），否则放在临时目录。子检查按原环境运行（不准备字节码共享层与身份记忆化：调用方环境里已有的前缀照常
继承），与原来串行的 make 先决目标同一环境。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXECUTOR = HERE / "unit_executor.py"
COMMANDS_SCHEMA = "unit-executor-commands/v1"
EGRESS_SPEC_UNIT_PREFIX = "egress-spec:"

# check-egress-spec 子检查的额度与预计秒数。只做 Python 静态检查的子检查占 1 核、1 GB；其余要编译 Go（go run／test／
# vet／build、扫描器）的占 2 核、3～4 GB（GOMAXPROCS 随额度下发，编译并行度跟着它）。没登记的子检查按 Go 类保守估计。
LIGHT_CHECKS = frozenset({
    "check-egress-spec-local-source",
    "test-official-client-control",
    "test-upstream-merge-tools",
    "egress-spec-version-leak-self-test",
    "egress-spec-version-leak",
    "egress-spec-changeset5-symbols-self-test",
    "egress-spec-changeset5-symbols",
    "egress-spec-changeset6-transition-self-test",
    "egress-spec-changeset6-transition",
    "egress-spec-maintenance-transition-self-test",
    "egress-spec-maintenance-transition",
    "egress-spec-multi-persona-transition-self-test",
    "egress-spec-multi-persona-transition",
    "egress-spec-fw-d-transition-self-test",
    "egress-spec-fw-e-workspace-self-test",
    "egress-spec-fw-e-workspace",
    "egress-spec-fw-e-completeness-self-test",
    "egress-spec-fw-e-completeness",
    "egress-spec-fw-e-runtime-evidence-self-test",
    "egress-spec-fw-e-runtime-evidence",
    "egress-spec-fw-e-r-disposition-self-test",
    "egress-spec-fw-e-r-disposition",
    "egress-spec-changeset6-benchmark-self-test",
    "egress-spec-changeset6-benchmark",
    "egress-spec-audit-index-self-test",
    "egress-spec-ledger-completeness",
    "egress-spec-gofmt",
    "egress-spec-changeset6-conflict-self-test",
    "egress-spec-changeset6-conflict",
    "egress-spec-maintenance-conflict-self-test",
    "egress-spec-maintenance-conflict",
    "egress-spec-runtime-catalog-self-test",
})
# 单进程内存峰值超过 3 GB 的 Go 子检查（ARM64 实测 3.7～4.3 GB）额度 5 GB，其余 Go 类 3 GB。
HEAVY_GO_CHECKS = frozenset({"egress-spec-go-test", "egress-spec-egressscan-self-test", "egress-scanner-check"})
# 预计秒数只决定并行段的派发顺序（从长到短），不影响结论：取 ARM64 实测（E2-04，10-01，入口门禁一次运行），
# 没登记的按类别取默认值（Python 类实测 1～3 秒，Go 类 2～20 秒）。
CHECK_SECONDS: dict[str, float] = {
    "egress-spec-go-test": 112.0,
    "egress-spec-egressscan-self-test": 57.0,
    "test-upstream-merge-tools": 43.0,
    "egress-scanner-check": 33.0,
    "check-egress-bootstrap-replay": 31.0,
    "egress-spec-go-build": 20.0,
    "test-official-client-control": 17.0,
    "egress-spec-wire-diff": 15.0,
    "egress-spec-changeset5-symbols": 11.0,
    "egress-spec-service-guard": 7.0,
    "egress-spec-repository-guard": 5.0,
}


# 子检查的额度：Python 类 1 核、1 GB；Go 类实测 CPU 约 1.5 核（GOMAXPROCS=2），按 1.5 核排程、内部并行度显式保持 2，内存 3 GB，
# 峰值超过 3 GB 的三项 5 GB；单项超时 30 分钟；没登记秒数的按类别取默认值。
EGRESS_LIGHT_QUOTA = {"cores": 1, "memory_mb": 1024}
EGRESS_GO_QUOTA = {"cores": 1.5, "memory_mb": 3072}
EGRESS_HEAVY_GO_MEMORY_MB = 5120
EGRESS_GO_ENV = {"GOMAXPROCS": "2"}
EGRESS_TIMEOUT_SECONDS = 1800
EGRESS_DEFAULT_SECONDS = {"light": 2.0, "go": 10.0}


def egress_spec_unit(target: str, *, cwd: str) -> dict[str, Any]:
    """一个子检查目标对应的命令单元（入口门禁与 ``make-checks`` 同一份定义）。"""

    light = target in LIGHT_CHECKS
    quota = EGRESS_LIGHT_QUOTA if light else EGRESS_GO_QUOTA
    unit = {
        "unit_id": f"{EGRESS_SPEC_UNIT_PREFIX}{target}",
        "argv": ["make", "--no-print-directory", target],
        "cwd": cwd,
        "cores": quota["cores"],
        "memory_mb": EGRESS_HEAVY_GO_MEMORY_MB if target in HEAVY_GO_CHECKS else quota["memory_mb"],
        "timeout_seconds": EGRESS_TIMEOUT_SECONDS,
        "weight": CHECK_SECONDS.get(target, EGRESS_DEFAULT_SECONDS["light" if light else "go"]),
    }
    if not light:
        unit["env"] = dict(EGRESS_GO_ENV)
    return unit


def _out_dir(name: str) -> Path:
    stamp = time.strftime("%Y%m%dt%H%M%Sz", time.gmtime())
    base = os.environ.get("UNIT_EXECUTOR_OUT_DIR")
    path = Path(base) / name if base else Path(tempfile.gettempdir()) / "egress-spec-checks" / f"{name}-{stamp}-{os.getpid()}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _tail(path: str, lines: int = 30) -> list[str]:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-lines:]
    except OSError:
        return ["（日志不可读）"]


def make_checks(name: str, targets: list[str], *, cwd: Path | None = None) -> int:
    """把 make 子检查展开成命令单元并行执行，全部跑完再汇总；失败项附日志尾部（CI 里只能看到这份输出）。"""

    if not targets or len(set(targets)) != len(targets):
        print(f"{name}：子检查清单为空或有重复", file=sys.stderr)
        return 2
    workdir = str(Path(cwd or os.getcwd()).resolve())
    out_dir = _out_dir(name)
    manifest = out_dir / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": COMMANDS_SCHEMA, "units": [egress_spec_unit(t, cwd=workdir) for t in targets]},
                                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{name}：{len(targets)} 项子检查交给统一调度执行器并行执行，全部跑完再汇总（记录 {out_dir}）", file=sys.stderr, flush=True)
    completed = subprocess.run([sys.executable, str(EXECUTOR), "run-commands", "--manifest", str(manifest), "--out-dir", str(out_dir),
                                "--shared-caches", "off"], stdin=subprocess.DEVNULL)
    summary_path = out_dir / "summary.json"
    if completed.returncode != 0 and summary_path.is_file():
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        failed = [row for row in summary.get("units", []) if not row.get("passed")]
        for row in failed:
            target = str(row["unit_id"]).removeprefix(EGRESS_SPEC_UNIT_PREFIX)
            reason = f"信号 {row['signal']}" if row.get("signal") else "超时" if row.get("timed_out") else f"退出码 {row.get('exit_code')}"
            print(f"===== 子检查未通过：make {target}（{reason}）日志末尾：", file=sys.stderr)
            for line in _tail(row["log"]):
                print(f"  {line}", file=sys.stderr)
        if failed:
            print(f"{name} 未通过：{', '.join(str(r['unit_id']).removeprefix(EGRESS_SPEC_UNIT_PREFIX) for r in failed)}", file=sys.stderr)
    return completed.returncode


# ---------------------------------------------------------------------------
# 入口门禁清单（plan）：一次运行的全部门禁项
# ---------------------------------------------------------------------------

GATES_SCHEMA = "unit-executor-gates/v1"
GATES_SUMMARY_SCHEMA = "unit-executor-gates-summary/v1"
ENTRY_SUMMARY_SCHEMA = "arm64-entry-gates/v1"
P0_EVIDENCE_SCHEMA = "codex-p0-offline-gate-evidence/v1"
# E3-03：门禁项承接了单元（全集通过模式）时的 P0 证据形状：以执行器的运行清单为证据，签发与 VC-0 收口按记录库逐条
# 重验（codex_upgrade_vc_receipt.verify_p0_manifest_evidence）。
P0_EVIDENCE_SCHEMA_V2 = "codex-p0-offline-gate-evidence/v2"
P0_GATES = ("test-capture-tools", "check-egress-spec")
FULL_GATES_SCHEMA = "arm64-full-gates/v1"
PREFLIGHT_SCHEMA = "arm64-vc0-target-gate-preflight/v1"
COMMANDS_SUMMARY_SCHEMA = "unit-executor-commands-summary/v1"
CAPTURE_GROUP = "capture-tools"
PRE_A3_PREFIX = "pre-a3:"
# pre-A3 场景的调度额度与预计秒数（E2-04 第三次验收的 ARM64 实测：秒数、CPU 占比）。认证模块 plan 给的是统一的 1 核，而 44 个
# 场景一半以上 CPU 占比不到 0.5（等子进程、等超时为主），按 1 核排程会让 4 核额度用不满。额度＝实测占比向上取到 0.25 的
# 倍数、最低 0.25；最长的 vc1-recovery 链在关键路径上，保底 1 核。没登记的场景按 0.5 核。内存峰值实测不超过 0.5 GB。
PRE_A3_MEASURED: dict[str, tuple[float, float]] = {
    "vc-chain.vc1-recovery-chain": (548.0, 0.75),
    "vc-chain.vc1-capture": (212.0, 0.91),
    "segment-recovery.completed-job-reuse": (204.0, 0.54),
    "deadline-extension.sigkill-resume": (185.0, 0.03),
    "vc-chain.late-stage-faults": (91.0, 0.70),
    "vc-chain.full-validation-only": (50.4, 0.66),
    "stage-recovery.committed-classify": (27.2, 0.77),
    "runtime-egress.kernel-faults": (26.0, 0.15),
    "runtime-egress.guard-recovery": (16.0, 0.38),
}
PRE_A3_CRITICAL = frozenset({"vc-chain.vc1-recovery-chain"})
PRE_A3_DEFAULT_CORES = 0.5
PRE_A3_MEMORY_MB = 768
# pre-A3 场景（E3-02）：认证模块 plan --staging-parent 给的单元命令只带稳定内容，入口门禁按场景测试模块声明输入，可以
# 承接，认证从单元执行记录组装。E2-03 的旧清单（plan --staging-root）命令带每次新建的认证根，承接不了，仍标不可承接。
PRE_A3_NOT_INHERITABLE = "pre-A3 场景清单是 E2-03 的旧形式（命令带每次新建的认证根）：承接不了，改用认证模块 plan --staging-parent（E3-02）"
# pre-A3 场景读的部署脚本副本（测试树与数据根的这一份由入口门禁的部署一致性核对保证相同）。
PRE_A3_DEPLOY_SCRIPT = "tools/arm64_supervised_deploy.py"
# 场景运行器（pre-A3 认证模块 run-scenario）在模块级导入的测试目录夹具：每个场景都会读，不一定在场景测试模块的闭包里
# （E3-04 读集审计实测；测试核对运行器的模块级测试导入都在这里）。
PRE_A3_RUNNER_FIXTURES = ("tools/official_client_capture/tests/project_ledger_fixture.py",)
# 数据根里场景会探测的位置：项目总账按 Campaign 目录往上最多六层找 upgrade-project-ledger，场景的临时根在
# staging/pre-a3-scenarios 下，会探到 staging/upgrade-project-ledger。那里有总账时场景行为会变，所以声明为输入（现在
# 不存在，记为 missing；一旦出现，输入摘要就变、场景重跑）。E3-04 读集审计实测。
PRE_A3_DATA_ROOT_PROBES = ("{D}/staging/upgrade-project-ledger",)


def pre_a3_quota(name: str) -> tuple[float, float | None]:
    """pre-A3 场景的调度额度（核）与预计秒数（没有实测时为 None，沿用认证模块给的值）。"""

    if name in PRE_A3_CRITICAL:
        return 1.0, PRE_A3_MEASURED[name][0]
    if name in PRE_A3_MEASURED:
        seconds, ratio = PRE_A3_MEASURED[name]
        return max(0.25, math.ceil(ratio * 4) / 4), seconds
    return PRE_A3_DEFAULT_CORES, None

# make test 的组成（与 Makefile 的 test 目标对应：test-backend 拆成 go test 与 lint 两项、test-frontend 拆成三项）。
# test-official-client-control 在 make test 里是 check-egress-spec 的子检查，这里单列一个门禁项，用的就是那个子检查单元。
MAKE_TEST_GATES = (
    "backend-go-test", "backend-lint", "frontend-lint", "frontend-typecheck", "frontend-critical",
    "test-capture-tools", "test-official-client-control", "check-egress-spec",
)
# 部署前全量门禁在 make test 之外的五项（与 CI 的 test、golangci-lint、shell 作业对齐）。
FULL_GATES_EXTRA = ("backend-unit", "backend-integration", "lint-unit", "lint-integration", "deploy-scripts")
PROFILES: dict[str, tuple[str, ...]] = {
    "preflight": MAKE_TEST_GATES,
    "full-gates": MAKE_TEST_GATES + FULL_GATES_EXTRA,
    "entry": MAKE_TEST_GATES + FULL_GATES_EXTRA + ("pre-a3",),
    # 单独签 pre-A3（驱动 lib.sh 与编排器的单独路径，E3-02）：只有场景单元，同样写记录、可承接。
    "pre-a3": ("pre-a3",),
}
# 门禁项的字面命令（门禁记录、P0 证据里写的就是它；实际执行方式见各单元）。
GATE_COMMANDS: dict[str, tuple[list[str], str]] = {
    "test-capture-tools": (["make", "test-capture-tools"], "."),
    "check-egress-spec": (["make", "check-egress-spec"], "."),
    "test-official-client-control": (["make", "test-official-client-control"], "."),
    "backend-go-test": (["go", "test", "./...", "-count=1"], "backend"),
    "backend-lint": (["golangci-lint", "run", "--timeout=30m", "--allow-serial-runners", "./..."], "backend"),
    "frontend-lint": (["pnpm", "--dir", "frontend", "run", "lint:check"], "."),
    "frontend-typecheck": (["pnpm", "--dir", "frontend", "run", "typecheck"], "."),
    "frontend-critical": (["make", "test-frontend-critical"], "."),
    "backend-unit": (["go", "test", "-tags=unit", "./...", "-count=1"], "backend"),
    "backend-integration": (["go", "test", "-tags=integration", "./...", "-count=1"], "backend"),
    "lint-unit": (["golangci-lint", "run", "--timeout=30m", "--allow-serial-runners", "--build-tags=unit"], "backend"),
    "lint-integration": (["golangci-lint", "run", "--timeout=30m", "--allow-serial-runners", "--build-tags=integration"], "backend"),
    "deploy-scripts": (["deploy-scripts"], "."),
    "pre-a3": (["python3", "-m", "tools.official_client_capture.codex_upgrade_pre_a3_certification", "run-scenario"], "."),
}
# 额度按 ARM64 实测的 CPU／墙钟比与单进程内存峰值（E2-04，10-01，入口门禁一次运行）：
# * 后端 go test 三组各 10～11.5 分钟，CPU 只用约 1.1 核（编译之外大半时间在等待），单进程峰值 4～4.5 GB——原来按 2 核申请，
#   两组就占满 4 核额度、其它单元干等；改按 1.1 核（GOMAXPROCS 随额度向上取整为 2，编译仍可两路并行）、5 GB；
# * golangci-lint 三组各约 10 秒（结果缓存命中，冷缓存会更久），峰值约 250 MB。golangci-lint 默认只许一个实例运行，第二个
#   实例直接报「parallel golangci-lint is running」退出（ARM64 第二次验收实测），所以三组都带 --allow-serial-runners 排队等锁；
# * 前端 lint、typecheck 约 1.1～1.2 核（typecheck 峰值约 2 GB），vitest 约 1.9 核。
# 第三次验收（Go 构建缓存热）实测后端三组 CPU 只用 0.66～0.69 核：按 0.7 核排程，内部并行度另行显式给 GOMAXPROCS=2（不随额度
# 降成 1，否则 go test 的包并行度也变 1）。首次冷编译时 CPU 约 1.1 核，三组同时会短时超订，只在每台机器第一次出现。
GO_QUOTA = {"cores": 0.7, "memory_mb": 5120, "timeout_seconds": 3600}
GO_ENV = {"GOMAXPROCS": "2"}
LINT_QUOTA = {"cores": 1, "memory_mb": 3072, "timeout_seconds": 3600}
FRONTEND_QUOTA = {"cores": 1.2, "memory_mb": 3072, "timeout_seconds": 1800}
VITEST_QUOTA = {"cores": 2, "memory_mb": 2048, "timeout_seconds": 1800}
PREREQUISITES_QUOTA = {"cores": 1, "memory_mb": 256, "timeout_seconds": 120}
DEPLOY_QUOTA = {"cores": 1, "memory_mb": 512, "timeout_seconds": 600}
# 各单元的预计秒数（同样取实测，决定派发顺序）。
GATE_SECONDS: dict[str, float] = {
    "backend-go-test": 525.0, "backend-unit": 584.0, "backend-integration": 546.0,
    "backend-lint": 11.0, "lint-unit": 9.0, "lint-integration": 8.0,
    "frontend-lint": 53.0, "frontend-typecheck": 73.0, "frontend-critical": 23.0,
    "capture:prerequisites": 1.0, "deploy-scripts": 2.0,
}


def scheduling_table() -> dict[str, Any]:
    """命令单元的额度表与预计秒数（E3-01）：门禁清单原样带上，执行器算进调度策略版本——改了其中任何一项（额度、
    内部并行度、超时、派发顺序用的秒数），全部单元不承接（方案「调度策略变了全部重跑」）。与组合无关。"""

    return {
        "egress_spec": {"light_checks": sorted(LIGHT_CHECKS), "heavy_go_checks": sorted(HEAVY_GO_CHECKS), "light_quota": EGRESS_LIGHT_QUOTA,
                        "go_quota": EGRESS_GO_QUOTA, "heavy_go_memory_mb": EGRESS_HEAVY_GO_MEMORY_MB, "go_env": EGRESS_GO_ENV,
                        "timeout_seconds": EGRESS_TIMEOUT_SECONDS, "default_seconds": EGRESS_DEFAULT_SECONDS, "seconds": CHECK_SECONDS},
        "pre_a3": {"measured": {name: list(value) for name, value in PRE_A3_MEASURED.items()}, "critical": sorted(PRE_A3_CRITICAL),
                   "default_cores": PRE_A3_DEFAULT_CORES, "memory_mb": PRE_A3_MEMORY_MB},
        "go_quota": GO_QUOTA, "go_env": GO_ENV, "lint_quota": LINT_QUOTA, "frontend_quota": FRONTEND_QUOTA, "vitest_quota": VITEST_QUOTA,
        "prerequisites_quota": PREREQUISITES_QUOTA, "deploy_quota": DEPLOY_QUOTA, "gate_seconds": GATE_SECONDS,
    }
# 部署脚本测试从测试树的 CI 定义逐行取出（shell 作业与 test 作业里以 /bin/sh 或 /bin/bash 执行 deploy/ 下脚本的行）。
DEPLOY_TEST_LINE = re.compile(r"^[ \t]*(?:run:[ \t]*)?(/bin/(?:ba)?sh(?: -n)? deploy/[A-Za-z0-9._/-]+)[ \t]*$", re.M)
DEPLOY_TEST_SHAPE = re.compile(r"^/bin/(?:ba)?sh(?: -n)? deploy/[A-Za-z0-9._/-]+$")
# 只能在 macOS 上执行的部署脚本测试：Linux 上记为「不在本平台执行」（CI 在 macos-15 上照常执行）。
MACOS_ONLY_DEPLOY_TESTS: dict[str, str] = {
    "/bin/bash deploy/tests/apple-container-test.sh":
        "Apple container 部署脚本测试用 BSD stat（stat -f '%Lp'），GNU stat 的 -f 是文件系统状态、语义不同，"
        "在 Linux 上必然失败；CI 的 shell 作业在 macos-15 上执行它",
}


def egress_spec_checks(tree: Path) -> list[str]:
    """从测试树的 Makefile 读子检查清单（``make print-egress-spec-checks``，与 ``make check-egress-spec`` 同一份）。"""

    completed = subprocess.run(["make", "-s", "--no-print-directory", "print-egress-spec-checks"], cwd=tree,
                               capture_output=True, text=True, stdin=subprocess.DEVNULL)
    targets = completed.stdout.split()
    if completed.returncode != 0 or not targets or len(set(targets)) != len(targets):
        raise ValueError(f"读不到测试树的子检查清单（make print-egress-spec-checks）：{completed.stderr.strip()[-300:]}")
    return targets


def deploy_tests(tree: Path) -> list[str]:
    """从测试树的 CI 定义逐行取出部署脚本测试；取到的每一行都要符合安全形状，否则拒绝（绝不执行非预期内容）。"""

    workflow = tree / ".github" / "workflows" / "backend-ci.yml"
    try:
        text = workflow.read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"读不到 CI 定义：{workflow}（{error}）") from error
    commands = [match.group(1) for match in DEPLOY_TEST_LINE.finditer(text)]
    if not commands:
        raise ValueError("CI 定义里没有取到部署脚本测试")
    for command in commands:
        if not DEPLOY_TEST_SHAPE.fullmatch(command):
            raise ValueError(f"部署脚本测试命令不合法：{command}")
    if len(set(commands)) != len(commands):
        raise ValueError("CI 定义里的部署脚本测试有重复")
    return commands


def _deploy_unit_id(command: str) -> str:
    parts = command.split()
    name = parts[-1].removeprefix("deploy/").replace("/", "-")
    return f"deploy:{name}{'-syntax' if '-n' in parts else ''}"


def _unit_records_module() -> Any:
    """同目录的 unit_records.py（E3-01 输入范围的标准定义）：按路径加载，与执行器用的是同一份。"""

    name = "unit_records_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "unit_records.py")
        if spec is None or spec.loader is None:
            raise ValueError(f"找不到同目录的 unit_records.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


# 受管工具树（仓库相对路径）：命令单元的输入范围按它划分受管工具、测试目录、docs 与其余部分。
MANAGED_TREE = "tools/official_client_capture"


def command_inputs(extra: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """命令单元的输入声明（E3-01，按 E2-05 从宽）：整个仓库（受管工具树、测试目录、docs、其余部分四段）＋ HEAD 提交，
    再加门禁项另列的输入（已算好的明细）。执行器在测试树里按跟踪文件算各段摘要；读集审计（E3-04）落地后再收窄。"""

    records = _unit_records_module()
    ranges = records.standard_ranges(MANAGED_TREE)
    return {"ranges": [ranges[key] for key in records.COMMAND_RANGES], "head": True, "resolved": list(extra or [])}


def environment_facts(tree: Path, typescript_module: str | None) -> list[dict[str, Any]]:
    """门禁清单带给执行器的环境事实（E3-01 环境指纹的一部分，执行器自己再加系统、Python、软件包与环境变量）：
    工具链版本与测试树前端依赖的摘要。在执行器同一份环境里算（驱动 entry-gates.sh 用同一份白名单环境跑 plan）。"""

    steps = _entry_steps_module()
    ctx = steps.Context(params={}, driver_dir=HERE, steps_dir=Path(tempfile.gettempdir()))
    items = [steps.Input("environment", "env", fact) for fact in ("go", "node", "pnpm", "golangci_lint", "docker", "bash")]
    for path in ([typescript_module] if typescript_module else []) + [str(Path(tree) / "frontend" / "node_modules" / ".modules.yaml")]:
        items.append(steps.Input("environment", "file", path))
    return sorted((steps.resolve(item, ctx) for item in items), key=lambda entry: entry["name"])


def historical_source_input(root: str | None) -> dict[str, Any]:
    """check-egress-spec 子检查另列的输入：历史源码树（0.149.1，CODEX_0_149_1_SOURCE_ROOT）的目录摘要（E2-05 同）。"""

    steps = _entry_steps_module()
    ctx = steps.Context(params={}, driver_dir=HERE, steps_dir=Path(tempfile.gettempdir()))
    return steps.resolve(steps.Input("environment", "tree", root or "/nonexistent-historical-source-root", "pycache"), ctx)


def pre_a3_data_root_inputs(data_root: Path) -> list[dict[str, Any]]:
    """pre-A3 场景读、但测试树里没有或不保证与测试树一致的数据根内容（E2-05 的 pre-A3 输入里的三项）：冻结台账目录、
    录制回放数据、alpine 镜像；另加场景会探测的数据根位置（``PRE_A3_DATA_ROOT_PROBES``，E3-04）。门禁清单生成时在数据根
    算好，作为已算明细（``resolved``）带给每个场景单元。"""

    steps = _entry_steps_module()
    ctx = steps.Context(params={"D": str(Path(data_root).resolve())}, driver_dir=HERE, steps_dir=Path(tempfile.gettempdir()))
    items = (steps.MAINTENANCE, steps.Input("environment", "recorded"), steps.Input("environment", "image_ref", "alpine:3.21"),
             *(steps.Input("environment", "tree", probe, "pycache") for probe in PRE_A3_DATA_ROOT_PROBES))
    return [steps.resolve(item, ctx) for item in items]


def pre_a3_inputs(test_file: str, data_root_inputs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """pre-A3 场景单元的输入声明（E3-02）：按 E2-05 的 pre-A3 输入范围，测试按场景收窄。

    受管树（不含测试）、夹具、文档（两份指南在 docs/ 下）、部署脚本副本、场景运行器模块级导入的测试夹具
    （``PRE_A3_RUNNER_FIXTURES``）、场景测试模块的静态依赖闭包（执行器展开），加数据根算好的几项。前几样按测试树内容算：入口门禁跑 pre-A3 之前已核对数据根部署的受管树、指南与部署脚本就是
    测试树这一份。不含 HEAD 与仓库其余部分：场景在数据根运行，读不到 git，也没有后端、前端。"""

    ranges = _unit_records_module().standard_ranges(MANAGED_TREE)
    return {"ranges": [ranges["managed"], ranges["tests-fixtures"], ranges["docs"]],
            "files": [{"category": "managed", "path": PRE_A3_DEPLOY_SCRIPT},
                      *({"category": "tests", "path": fixture} for fixture in PRE_A3_RUNNER_FIXTURES)],
            "test_modules": [test_file], "resolved": list(data_root_inputs or [])}


def plan_gates(
    tree: Path,
    *,
    profile: str,
    launcher: list[str],
    typescript_module: str | None = None,
    pre_a3_units: Path | None = None,
    pre_a3_env: dict[str, str] | None = None,
    pre_a3_data_inputs: list[dict[str, Any]] | None = None,
    platform: str | None = None,
    environment: list[dict[str, Any]] | None = None,
    egress_extra_inputs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """生成入口门禁清单（``unit-executor-gates/v1``）。

    测试树里的单元（测试组与命令单元）都套 ``launcher``（ARM64 上是私有挂载命名空间里遮住生产别名，与 isolated_run
    同一做法）；pre-A3 场景在数据根的生产布局里运行，不套隔离（受管树经生产别名访问的分支也要覆盖到），环境另给。

    E3-01：清单另带命令单元额度表（``scheduling``，算进调度策略版本）、环境事实（``environment``，算进环境指纹，
    由命令行入口在执行器同一份环境里算好传入）、每个命令单元的输入声明（``inputs``）。
    E3-02：pre-A3 场景清单是新形式（单元带 ``scenario_test_file``）时按 ``pre_a3_inputs`` 声明输入、可以承接；
    旧形式仍标不可承接。
    """

    if profile not in PROFILES:
        raise ValueError(f"未知的门禁组合：{profile}")
    gates_wanted = PROFILES[profile]
    if ("pre-a3" in gates_wanted) != (pre_a3_units is not None):
        raise ValueError("含 pre-A3 的门禁组合（entry、pre-a3）必须给出 pre-A3 场景清单，其余组合不得给出")
    tree = Path(tree).resolve()
    workdir, backend = str(tree), str(tree / "backend")
    platform = platform or sys.platform
    units: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []

    def command(unit_id: str, argv: list[str], cwd: str, quota: dict[str, Any], weight: float, env: dict[str, str] | None = None,
                extra_inputs: list[dict[str, Any]] | None = None) -> str:
        unit = {"unit_id": unit_id, "argv": [*launcher, *argv], "cwd": cwd, **quota, "weight": weight, "inputs": command_inputs(extra_inputs)}
        if env:
            unit["env"] = env
        units.append(unit)
        return unit_id

    groups: list[dict[str, Any]] = []
    # check-egress-spec 的子检查先展开：门禁项 test-official-client-control 在清单里排在它前面，用的却是它的子检查单元。
    egress_units: list[str] = []
    if "check-egress-spec" in gates_wanted:
        for target in egress_spec_checks(tree):
            unit = egress_spec_unit(target, cwd=workdir)
            egress_units.append(command(unit["unit_id"], unit["argv"], workdir,
                                        {key: unit[key] for key in ("cores", "memory_mb", "timeout_seconds")}, unit["weight"], unit.get("env"),
                                        extra_inputs=egress_extra_inputs))
    for gate_id in gates_wanted:
        if gate_id == "test-capture-tools":
            groups.append({"group_id": CAPTURE_GROUP, "start": "tools/official_client_capture/tests", "pattern": "test_*.py",
                           "env": {"CLAUDE_AST_TYPESCRIPT_MODULE": typescript_module} if typescript_module else {},
                           "launcher": list(launcher)})
            prerequisites = command("capture:prerequisites", ["make", "--no-print-directory", "test-capture-tools-prerequisites"], workdir,
                                    PREREQUISITES_QUOTA, GATE_SECONDS["capture:prerequisites"])
            gates.append({"gate_id": gate_id, "units": [prerequisites], "test_groups": [CAPTURE_GROUP]})
        elif gate_id == "check-egress-spec":
            gates.append({"gate_id": gate_id, "units": list(egress_units)})
        elif gate_id == "test-official-client-control":
            shared = f"{EGRESS_SPEC_UNIT_PREFIX}test-official-client-control"
            if shared not in egress_units:
                raise ValueError("子检查清单里没有 test-official-client-control（同名门禁项用的就是这个子检查单元）")
            gates.append({"gate_id": gate_id, "units": [shared]})
        elif gate_id in ("backend-go-test", "backend-unit", "backend-integration"):
            argv, _cwd = GATE_COMMANDS[gate_id]
            # integration 带 CI=true：没有 Docker 时失败而不是静默跳过（integration_harness_test.go）。
            env = {**GO_ENV, "CI": "true"} if gate_id == "backend-integration" else dict(GO_ENV)
            gates.append({"gate_id": gate_id, "units": [command(f"backend:{gate_id.removeprefix('backend-')}", argv, backend, GO_QUOTA,
                                                                GATE_SECONDS[gate_id], env)]})
        elif gate_id in ("backend-lint", "lint-unit", "lint-integration"):
            argv, _cwd = GATE_COMMANDS[gate_id]
            gates.append({"gate_id": gate_id, "units": [command(f"backend:{gate_id}", argv, backend, LINT_QUOTA, GATE_SECONDS[gate_id])]})
        elif gate_id in ("frontend-lint", "frontend-typecheck"):
            argv, _cwd = GATE_COMMANDS[gate_id]
            gates.append({"gate_id": gate_id, "units": [command(f"frontend:{gate_id.removeprefix('frontend-')}", argv, workdir, FRONTEND_QUOTA,
                                                                GATE_SECONDS[gate_id])]})
        elif gate_id == "frontend-critical":
            gates.append({"gate_id": gate_id, "units": [command("frontend:critical", ["make", "--no-print-directory", "test-frontend-critical"],
                                                                workdir, VITEST_QUOTA, GATE_SECONDS[gate_id])]})
        elif gate_id == "deploy-scripts":
            members, skipped = [], []
            for line in deploy_tests(tree):
                if line in MACOS_ONLY_DEPLOY_TESTS and platform != "darwin":
                    skipped.append({"command": line.split(), "reason": MACOS_ONLY_DEPLOY_TESTS[line]})
                    continue
                members.append(command(_deploy_unit_id(line), line.split(), workdir, DEPLOY_QUOTA, GATE_SECONDS["deploy-scripts"]))
            if not members:
                raise ValueError("部署脚本测试在本平台一项都不执行")
            gates.append({"gate_id": gate_id, "units": members, "not_executed": skipped})
        elif gate_id == "pre-a3":
            assert pre_a3_units is not None
            payload = json.loads(Path(pre_a3_units).read_text(encoding="utf-8"))
            if payload.get("schema_version") != COMMANDS_SCHEMA or not isinstance(payload.get("units"), list) or not payload["units"]:
                raise ValueError(f"pre-A3 场景清单格式非法：{pre_a3_units}")
            members = []
            for unit in payload["units"]:
                if not str(unit.get("unit_id", "")).startswith(PRE_A3_PREFIX):
                    raise ValueError(f"pre-A3 场景清单里有非 pre-A3 单元：{unit.get('unit_id')}")
                merged = {key: value for key, value in unit.items() if key != "scenario_test_file"}
                cores, seconds = pre_a3_quota(str(unit["unit_id"]).removeprefix(PRE_A3_PREFIX))
                merged.update(cores=cores, memory_mb=PRE_A3_MEMORY_MB)
                test_file = unit.get("scenario_test_file")
                if isinstance(test_file, str) and test_file:
                    merged["inputs"] = pre_a3_inputs(test_file, pre_a3_data_inputs)
                else:
                    merged.update(inheritable=False, not_inheritable_reason=PRE_A3_NOT_INHERITABLE)
                if seconds is not None:
                    merged["weight"] = seconds
                if pre_a3_env:
                    merged["env"] = {**(unit.get("env") or {}), **pre_a3_env}
                units.append(merged)
                members.append(unit["unit_id"])
            gates.append({"gate_id": gate_id, "units": members})
    manifest = {"schema_version": GATES_SCHEMA, "profile": profile, "test_groups": groups, "units": units, "gates": gates,
                "scheduling": scheduling_table()}
    if environment is not None:
        manifest["environment"] = environment
    return manifest


# ---------------------------------------------------------------------------
# 导出（export）：门禁记录、P0 证据、预跑记录、全量门禁摘要、pre-A3 子汇总
# ---------------------------------------------------------------------------


def _seconds_between(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    parse = lambda value: time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))  # noqa: E731
    return float(parse(end) - parse(start))


def _entry_steps_module() -> Any:
    """同目录的 entry_steps.py（仓库 tools/ci 与驱动副本里都和本文件同目录）：按路径加载，不受当前目录与 PYTHONPATH 影响。"""

    name = "entry_steps_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "entry_steps.py")
        if spec is None or spec.loader is None:
            raise ValueError(f"找不到同目录的 entry_steps.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def _write(path: Path, payload: Any) -> str:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return str(path)


# 测试树受管工具的五摘要：与发布认证、pre-A3 认证用同一个函数（codex_upgrade_policy_certification.current_identity）。
IDENTITY_FIELDS = ("policy_sha256", "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "tool_files_sha256")
_TREE_IDENTITY_SNIPPET = (
    "import json\n"
    "from tools.official_client_capture import codex_upgrade_policy_certification as certification\n"
    "identity = certification.current_identity()\n"
    "print(json.dumps({name: identity[name] for name in certification.IDENTITY_FIELDS}))\n"
)
TREE_IDENTITY_TIMEOUT_SECONDS = 900


def tree_tool_identity(tree: str) -> dict[str, str]:
    """测试树受管工具的五摘要（v2 P0 证据绑定，E3-03）：在测试树里起子进程算，不写字节码。签发时与发布认证登记的
    身份比对——入口门禁跑之前已核对数据根部署的受管树就是测试树这一份，两边应当相等。"""

    completed = subprocess.run([sys.executable, "-B", "-c", _TREE_IDENTITY_SNIPPET], cwd=tree, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                               timeout=TREE_IDENTITY_TIMEOUT_SECONDS)
    lines = completed.stdout.strip().splitlines()
    if completed.returncode != 0 or not lines:
        raise ValueError(f"测试树的工具五摘要算不出来（退出码 {completed.returncode}）：{completed.stderr.strip()[-300:]}")
    identity = json.loads(lines[-1])
    if (not isinstance(identity, dict) or sorted(identity) != sorted(IDENTITY_FIELDS)
            or not all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) for value in identity.values())):
        raise ValueError(f"测试树的工具五摘要格式不对：{lines[-1][:300]}")
    return identity


def _p0_v2_context(summary: dict[str, Any], tree: str, tool_identity: Any) -> tuple[dict[str, Any] | None, str]:
    """v2 P0 证据的共同部分（运行清单引用、记录库、测试树五摘要）；条件不满足时返回（None, 原因）。

    条件：执行器给了记录库、清单自检通过且已发布进记录库（发布的那一份摘要等于汇总登记的）、测试树五摘要算得出。"""

    manifest = summary.get("unit_manifest") or {}
    store = summary.get("record_store")
    run_id = str(summary.get("run_id") or "")
    if not store or manifest.get("self_check") != "passed" or not re.fullmatch(r"[0-9A-Za-z._-]+", run_id):
        return None, "执行器没有记录库，或运行清单自检没通过（清单不发布进记录库），没有可重验的执行记录"
    published = Path(str(store)) / "runs" / f"{run_id}.json"
    try:
        payload = json.loads(published.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        payload = None
    if not isinstance(payload, dict) or payload.get("manifest_sha256") != manifest.get("manifest_sha256"):
        return None, f"运行清单没有发布进记录库，或发布的不是本次运行的那一份：{published}"
    try:
        identity = (tool_identity or tree_tool_identity)(tree)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        return None, f"测试树的工具五摘要算不出来：{error}"
    return {"unit_manifest": {"run_id": run_id, "manifest_sha256": manifest["manifest_sha256"], "mode": summary.get("mode")},
            "record_store": str(Path(str(store)).resolve()), "tool_identity": identity}, ""


def export_records(
    manifest_path: Path,
    summary_path: Path,
    out: Path,
    *,
    source: dict[str, str],
    subject: str,
    round_id: str,
    tree: str,
    isolation: str,
    host: str,
    architecture: str,
    target_version: str | None = None,
    bytecode_cache: str | None = None,
    tool_identity: Any = None,
) -> dict[str, Any]:
    """从一次运行的执行器汇总导出全部记录（写到 ``out``），返回入口门禁总摘要（同时写成 ``out/entry-gates.json``）。

    * 门禁记录 ``logs/<门禁项>.gate.json``：与 lib.sh 的 write_gate_json 同一组字段，另带成员单元、失败单元、不在本平台
      执行的项，以及这个门禁项的输入明细与输入摘要（E2-05，清单见 entry_steps.GATE_INPUTS；源码提交取测试树的提交）；
      ``logs/full-regression.gate.json`` 是 make test 的组成全部通过与否（预跑与全量门禁都引用它）；
    * P0 证据 ``p0/{check-egress-spec,test-capture-tools}.json``：两项门禁都没有承接单元时是 v1——手写 P0 脚本的同一
      形状，声明「make 命令的一次运行」（P0 收据的 facts 照原样从它们取数），test-capture-tools 另列逐条跳过清单，
      check-egress-spec 另列逐个子检查。任一项承接了单元（E3-01 全集通过模式）时两份都写 v2（E3-03）：在 v1 字段
      之外登记运行清单（run_id、自摘要、模式）、记录库、门禁项的命令单元与测试组、本次执行／承接的单元数和测试树的
      工具五摘要（``tool_identity`` 可注入，默认在测试树里起子进程算），签发与 VC-0 收口按记录库逐条重验。记录库或
      已发布的清单缺失、五摘要算不出来时不写，总摘要的 ``p0_evidence_withheld`` 写明原因；
    * pre-A3 子汇总 ``pre-a3-executor-summary.json``：只含 ``pre-a3:`` 单元的命令单元汇总，供认证 issue 核对；
    * ``full-gates-summary.json``、``preflight.json``：全量门禁与 VC-0 预跑的原形状记录（组合里有对应门禁项时才写）。
    """

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    summary = json.loads(Path(summary_path).read_text(encoding="utf-8"))
    if manifest.get("schema_version") != GATES_SCHEMA or summary.get("schema_version") != GATES_SUMMARY_SCHEMA:
        raise ValueError("门禁清单或执行器汇总的格式不对")
    out = Path(out)
    rows = {row["unit_id"]: row for row in summary["units"]}
    gate_rows = {row["gate_id"]: row for row in summary["gates"]}
    wanted = [gate["gate_id"] for gate in manifest["gates"]]
    if sorted(gate_rows) != sorted(wanted):
        raise ValueError("执行器汇总的门禁项与清单不一致")
    profile = manifest.get("profile")
    records: dict[str, dict[str, Any]] = {}
    steps = _entry_steps_module()
    inputs_ctx = steps.Context(params={**os.environ, "ENTRY_COMMIT": str(source.get("tree_head") or source.get("commit") or "")},
                               driver_dir=HERE, steps_dir=out)

    def gate_record(gate_id: str, exit_code: int, started: str | None, completed: str | None, extra: dict[str, Any]) -> dict[str, Any]:
        argv, workdir = GATE_COMMANDS.get(gate_id, (["make", "test"], "."))
        return {
            "gate_id": gate_id, "command": argv, "working_directory": workdir, "host": host, "architecture": architecture,
            "started_at_utc": started, "completed_at_utc": completed, "exit_code": exit_code, "tree": tree,
            "tree_head": source.get("tree_head"), "isolation": isolation, **extra,
        }

    mode = summary.get("mode")
    for gate_id in wanted:
        row = gate_rows[gate_id]
        failed = list(row["failed_units"])
        inputs = steps.gate_inputs(gate_id, inputs_ctx)
        record = gate_record(gate_id, 0 if row["status"] == "passed" else 1, row["started_at_utc"], row["completed_at_utc"], {
            "status": row["status"], "units": row["units"], "test_groups": row["test_groups"], "failed_units": failed,
            "not_executed": row["not_executed"], "unit_seconds": row["unit_seconds"],
            "failed_logs": [rows[unit_id]["log"] for unit_id in failed if unit_id in rows],
            "executor_summary": str(summary_path), "inputs": inputs, "inputs_sha256": steps.inputs_sha256(inputs),
            # E3-01：本门禁项承接的单元（起止时间只算本次执行的单元）与运行模式。
            "mode": mode, "inherited_units": list(row.get("inherited_units") or []),
        })
        _write(out / "logs" / f"{gate_id}.gate.json", record)
        records[gate_id] = record
    composites: dict[str, Any] = {}
    if all(gate_id in records for gate_id in MAKE_TEST_GATES):
        members = [records[gate_id] for gate_id in MAKE_TEST_GATES]
        failed_gates = [record["gate_id"] for record in members if record["exit_code"] != 0]
        regression = gate_record("full-regression", 0 if not failed_gates else 1,
                                 min((r["started_at_utc"] for r in members if r["started_at_utc"]), default=None),
                                 max((r["completed_at_utc"] for r in members if r["completed_at_utc"]), default=None),
                                 {"status": "passed" if not failed_gates else "failed", "composed_of": list(MAKE_TEST_GATES),
                                  "failed_gates": failed_gates, "executor_summary": str(summary_path)})
        composites["full-regression"] = {"exit_code": regression["exit_code"], "gate_json": _write(out / "logs" / "full-regression.gate.json", regression)}
    p0: dict[str, str] = {}
    withheld: dict[str, str] = {}
    p0_gates = [gate_id for gate_id in P0_GATES if gate_id in records]
    inherited_any = any(records[gate_id]["inherited_units"] for gate_id in p0_gates)
    v2: dict[str, Any] | None = None
    if inherited_any:
        v2, why = _p0_v2_context(summary, tree, tool_identity)
        if v2 is None:
            for gate_id in p0_gates:
                withheld[gate_id] = (f"本次运行承接了 {len(records[gate_id]['inherited_units'])} 个单元（全集通过模式），要写以运行清单为证据的"
                                     f" v2 P0 证据，但{why}；要签 P0 收据请修好后重跑，或用重新执行全集（--mode re-execute）")

    def gate_members(gate_id: str) -> list[str]:
        record = records[gate_id]
        return list(record["units"]) + [unit_id for group_id in record["test_groups"] for unit_id in summary["test_groups"][group_id]["units"]]

    def v2_fields(gate_id: str, test_group: str | None) -> dict[str, Any]:
        """v2 证据在 v1 字段之外的部分：运行清单引用、记录库、门禁项的命令单元与测试组、本次执行／承接的单元数、五摘要。"""

        assert v2 is not None
        dispositions = [rows[unit_id].get("disposition", "executed") if unit_id in rows else "not_run" for unit_id in gate_members(gate_id)]
        return {**v2, "units": list(records[gate_id]["units"]), "test_group": test_group,
                "unit_counts": {"executed": dispositions.count("executed"), "inherited": dispositions.count("inherited")}}

    if "test-capture-tools" in records and "test-capture-tools" not in withheld:
        group = summary["test_groups"][CAPTURE_GROUP]
        counts = group["counts"]
        record = records["test-capture-tools"]
        full_set_problems = sum(len(group["full_set"][key]) for key in ("missing", "duplicated", "unexpected", "units_not_run"))
        # 门禁项没通过时 failed 至少记 1：前置检查失败、单元崩溃没写结果等不体现在用例计数里。
        failed = counts["failed"] + counts["error"] + counts["unexpected_success"] + full_set_problems
        failed = max(1, failed) if record["exit_code"] else 0
        evidence = {
            "schema_version": P0_EVIDENCE_SCHEMA, "gate_id": "test-capture-tools", "command": ["make", "test-capture-tools"],
            "working_directory": tree, "git_commit": source.get("commit"), "status": record["status"], "exit_code": record["exit_code"],
            "passed": counts["passed"] + counts["expected_failure"], "failed": failed,
            "approved_skip": counts["skipped"], "unexpected_skip": 0,
            "elapsed_seconds": _seconds_between(record["started_at_utc"], record["completed_at_utc"]),
            "raw_errors": record["failed_units"] + [f"{key}: {group['full_set'][key][:5]}" for key in ("missing", "duplicated", "unexpected", "units_not_run") if group["full_set"][key]],
            "temporary_asset_inventory": [],
            "executed_as": "入口门禁一次运行：统一调度执行器按模块（重模块拆块、独占名单单独）在整机额度内并行执行测试组全集",
            "expected_tests": group["expected_tests"], "reported_tests": group["reported_tests"],
            "skipped": group["skipped"], "executor_summary": str(summary_path),
        }
        if v2 is not None:
            evidence.update(schema_version=P0_EVIDENCE_SCHEMA_V2, **v2_fields("test-capture-tools", CAPTURE_GROUP),
                            executed_as="入口门禁一次运行（全集通过模式）：测试组全集由本次执行的单元与承接以往运行正式执行记录的单元"
                                        "合成，逐单元引用运行清单里的执行记录")
        p0["test-capture-tools"] = _write(out / "p0" / "test-capture-tools.json", evidence)
    if "check-egress-spec" in records and "check-egress-spec" not in withheld:
        record = records["check-egress-spec"]
        checks = [{"target": unit_id.removeprefix(EGRESS_SPEC_UNIT_PREFIX), "passed": rows[unit_id]["passed"] if unit_id in rows else False,
                   "exit_code": rows[unit_id]["exit_code"] if unit_id in rows else None,
                   "seconds": rows[unit_id]["seconds"] if unit_id in rows else None, "log": rows[unit_id]["log"] if unit_id in rows else None}
                  for unit_id in record["units"]]
        passed_checks = sum(1 for item in checks if item["passed"])
        evidence = {
            "schema_version": P0_EVIDENCE_SCHEMA, "gate_id": "check-egress-spec", "command": ["make", "check-egress-spec"],
            "working_directory": tree, "git_commit": source.get("commit"), "status": record["status"], "exit_code": record["exit_code"],
            "passed": passed_checks, "failed": len(checks) - passed_checks, "approved_skip": 0, "unexpected_skip": 0,
            "elapsed_seconds": _seconds_between(record["started_at_utc"], record["completed_at_utc"]),
            "raw_errors": record["failed_units"], "temporary_asset_inventory": [],
            "executed_as": "入口门禁一次运行：Makefile 的 EGRESS_SPEC_CHECKS 子检查各一个单元并行执行（与 make check-egress-spec 同一份清单）",
            "checks": checks, "executor_summary": str(summary_path),
        }
        if v2 is not None:
            evidence.update(schema_version=P0_EVIDENCE_SCHEMA_V2, **v2_fields("check-egress-spec", None),
                            executed_as="入口门禁一次运行（全集通过模式）：EGRESS_SPEC_CHECKS 子检查各一个单元，本次执行的与承接以往运行"
                                        "正式执行记录的合成全集，逐单元引用运行清单里的执行记录")
        p0["check-egress-spec"] = _write(out / "p0" / "check-egress-spec.json", evidence)
    pre_a3_summary = None
    if "pre-a3" in records:
        pre_rows = [{**row, "kind": "formal"} for row in summary["units"] if str(row["unit_id"]).startswith(PRE_A3_PREFIX)]
        pre_failed = [row["unit_id"] for row in pre_rows if not row["passed"]]
        pre_a3_summary = _write(out / "pre-a3-executor-summary.json", {
            "schema_version": COMMANDS_SUMMARY_SCHEMA, "status": "passed" if not pre_failed else "failed",
            "policy_sha256": summary["policy_sha256"], "elapsed_seconds": summary["elapsed_seconds"], "unit_count": len(pre_rows),
            "failed_units": pre_failed, "units_not_run": [u for u in summary["units_not_run"] if u.startswith(PRE_A3_PREFIX)], "units": pre_rows,
            "diagnostic": [item for item in summary["diagnostic"] if str(item["unit_id"]).startswith(PRE_A3_PREFIX)],
            "exported_from": str(summary_path),
        })
    status = "passed" if summary["status"] == "passed" else "failed"
    entry = {
        "schema_version": ENTRY_SUMMARY_SCHEMA,
        "statement": "入口门禁一次运行的记录：P0 证据、VC-0 预跑记录、部署前全量门禁与 pre-A3 场景都取自这一次运行",
        "subject_id": subject, "round": round_id, "profile": profile, "status": status, "source": source,
        "gates": [{"gate_id": gate_id, "status": records[gate_id]["status"], "exit_code": records[gate_id]["exit_code"],
                   "inputs_sha256": records[gate_id]["inputs_sha256"], "gate_json": f"logs/{gate_id}.gate.json"} for gate_id in wanted],
        "composites": composites, "p0_evidence": p0, "p0_evidence_withheld": withheld,
        "p0_evidence_schema": (P0_EVIDENCE_SCHEMA_V2 if v2 is not None else P0_EVIDENCE_SCHEMA) if p0 else None,
        "pre_a3_executor_summary": pre_a3_summary,
        "executor_summary": str(summary_path), "elapsed_seconds": summary["elapsed_seconds"],
        "max_cores_in_use": summary.get("max_cores_in_use"), "test_tree": tree, "bytecode_cache": bytecode_cache,
        # E3-01：运行模式、承接计数、执行记录清单与记录库。
        "mode": mode, "inheritance": summary.get("inheritance"), "unit_manifest": summary.get("unit_manifest"),
        "record_store": summary.get("record_store"),
        # E3-04：读集审计（带 --audit-reads 时才有）：结论、有未声明读取的单元与明细位置。
        "read_audit": summary.get("read_audit"),
    }
    if all(gate_id in records for gate_id in MAKE_TEST_GATES + FULL_GATES_EXTRA):
        order = ("full-regression",) + FULL_GATES_EXTRA
        entry["full_gates_summary"] = _write(out / "full-gates-summary.json", {
            "schema_version": FULL_GATES_SCHEMA, "purpose": "deploy-precondition-and-verification", "campaign_receipt": False,
            "statement": "部署前提与验证记录，不是 Campaign 收据；门禁全部在 ARM64 隔离测试树执行（入口门禁一次运行）",
            "subject_id": subject, "round": round_id, "status": "passed" if all(
                (composites["full-regression"]["exit_code"] if gate_id == "full-regression" else records[gate_id]["exit_code"]) == 0 for gate_id in order) else "failed",
            "source": source,
            "gates": [{"gate_id": gate_id, "exit_code": composites["full-regression"]["exit_code"] if gate_id == "full-regression" else records[gate_id]["exit_code"],
                       "gate_json": f"logs/{gate_id}.gate.json"} for gate_id in order],
            "test_tree": tree, "bytecode_cache": bytecode_cache, "entry_gates_summary": "entry-gates.json",
        })
    if "full-regression" in composites:
        regression = json.loads(Path(composites["full-regression"]["gate_json"]).read_text(encoding="utf-8"))
        entry["preflight"] = _write(out / "preflight.json", {
            "schema_version": PREFLIGHT_SCHEMA, "purpose": "vc0-preflight", "accept_gate_receipt": False,
            "statement": "只作 VC-0 预检，不是 VC-5 accept 的门禁收据；accept 前仍须在候选门禁目录执行正式目标平台门禁",
            "subject_id": subject, "round": round_id, "target_version": target_version,
            "status": "passed" if regression["exit_code"] == 0 else "failed", "source": source,
            "gate": {"exit_code": regression["exit_code"], "started_at_utc": regression["started_at_utc"],
                     "completed_at_utc": regression["completed_at_utc"], "gate_json": "logs/full-regression.gate.json",
                     "executor_summary": str(summary_path), "test_tree": tree, "bytecode_cache": bytecode_cache},
        })
    _write(out / "entry-gates.json", entry)
    return entry


def _parse(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    checks = sub.add_parser("make-checks", help="把 make 子检查展开成命令单元并行执行（make check-egress-spec 用）")
    checks.add_argument("--name", required=True, help="这组子检查的名称（日志与记录目录用）")
    checks.add_argument("targets", nargs="+", help="子检查的 make 目标")
    plan = sub.add_parser("plan", help="生成入口门禁清单（unit-executor-gates/v1）")
    plan.add_argument("--tree", type=Path, required=True, help="测试树（执行器在这里运行）")
    plan.add_argument("--profile", choices=sorted(PROFILES), required=True)
    plan.add_argument("--launcher-json", default="[]", help="测试树单元的启动前缀（JSON 字符串列表）")
    plan.add_argument("--typescript-module", default=None)
    plan.add_argument("--pre-a3-units", type=Path, default=None, help="pre-A3 场景清单（认证模块 plan 生成）")
    plan.add_argument("--pre-a3-env", action="append", default=[], help="pre-A3 场景的环境变量 KEY=VALUE，可重复")
    plan.add_argument("--pre-a3-data-root", type=Path, default=None,
                      help="数据根：在这里算 pre-A3 场景单元的数据根输入（冻结台账、录制数据、alpine 镜像，E3-02）")
    plan.add_argument("--historical-source-root", default=None, help="历史源码树（check-egress-spec 子检查另列的输入，E3-01）")
    plan.add_argument("--output", type=Path, required=True)
    export = sub.add_parser("export", help="从一次运行的执行器汇总导出门禁记录、P0 证据与预跑／全量门禁记录")
    export.add_argument("--manifest", type=Path, required=True)
    export.add_argument("--summary", type=Path, required=True)
    export.add_argument("--out", type=Path, required=True)
    export.add_argument("--subject", required=True)
    export.add_argument("--round", default="")
    export.add_argument("--target-version", default=None)
    export.add_argument("--tree", required=True)
    export.add_argument("--isolation", required=True)
    export.add_argument("--host", required=True)
    export.add_argument("--architecture", required=True)
    export.add_argument("--bytecode-cache", default=None)
    export.add_argument("--source", action="append", default=[], help="来源坐标 KEY=VALUE（bundle、branch、commit、tree_head 等），可重复")
    return parser.parse_args(argv)


def _pairs(items: list[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        if not sep or not key:
            raise ValueError(f"{label} 必须是 KEY=VALUE：{item}")
        result[key] = value
    return result


def main(argv: list[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "make-checks":
            return make_checks(args.name, list(args.targets))
        if args.command == "plan":
            launcher = json.loads(args.launcher_json)
            if not isinstance(launcher, list) or not all(isinstance(part, str) and part for part in launcher):
                raise ValueError("--launcher-json 必须是非空字符串组成的列表")
            data_inputs = pre_a3_data_root_inputs(args.pre_a3_data_root) if args.pre_a3_data_root is not None else None
            manifest = plan_gates(args.tree, profile=args.profile, launcher=launcher, typescript_module=args.typescript_module,
                                  pre_a3_units=args.pre_a3_units, pre_a3_env=_pairs(args.pre_a3_env, "--pre-a3-env") or None,
                                  pre_a3_data_inputs=data_inputs,
                                  environment=environment_facts(args.tree, args.typescript_module),
                                  egress_extra_inputs=[historical_source_input(args.historical_source_root)])
            _write(args.output, manifest)
            print(json.dumps({"profile": args.profile, "gates": [g["gate_id"] for g in manifest["gates"]], "units": len(manifest["units"]),
                              "test_groups": [g["group_id"] for g in manifest["test_groups"]],
                              "not_executed": [n for g in manifest["gates"] for n in g.get("not_executed", [])]}, ensure_ascii=False))
            return 0
        if args.command == "export":
            entry = export_records(args.manifest, args.summary, args.out, source=_pairs(args.source, "--source"), subject=args.subject,
                                   round_id=args.round, tree=args.tree, isolation=args.isolation, host=args.host,
                                   architecture=args.architecture, target_version=args.target_version, bytecode_cache=args.bytecode_cache)
            print(json.dumps({"status": entry["status"], "gates": {g["gate_id"]: g["status"] for g in entry["gates"]},
                              "composites": entry["composites"], "p0_evidence": entry["p0_evidence"],
                              "p0_evidence_schema": entry["p0_evidence_schema"], "p0_evidence_withheld": entry["p0_evidence_withheld"]},
                             ensure_ascii=False))
            return 0
    except (OSError, ValueError, KeyError) as error:
        print(f"入口门禁：{error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
