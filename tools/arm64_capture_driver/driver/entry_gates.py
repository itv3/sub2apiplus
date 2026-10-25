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
import copy
import hashlib
import importlib.util
import json
import math
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping, Sequence

HERE = Path(__file__).resolve().parent
EXECUTOR = HERE / "unit_executor.py"
COMMANDS_SCHEMA = "unit-executor-commands/v1"
EGRESS_SPEC_UNIT_PREFIX = "egress-spec:"


def _client_table() -> Any:
    """同目录的客户端表 client_table.py（B5a-06）：按路径加载，与驱动其它模块共用一个模块对象（client_table_sibling）。"""

    name = "client_table_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "client_table.py")
        if spec is None or spec.loader is None:
            raise ValueError(f"找不到同目录的 client_table.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


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
# B5a-04：pre-A3 场景单元按客户端分前缀（与认证模块 CLIENT_UNIT_PREFIXES 同值，测试核对）。Claude 单元仍在 pre-a3 门禁项之下，
# 单元 ID 不同，同一记录库里与 Codex 单元互不承接；一份场景清单只属一个客户端（门禁记录的字面命令按它取）。
PRE_A3_CLIENT_PREFIXES: dict[str, str] = {"codex": PRE_A3_PREFIX, "claude": "pre-a3:claude:"}


def pre_a3_unit_client(unit_id: Any) -> str | None:
    """pre-A3 场景单元属于哪个客户端（最长匹配的前缀）；不是 pre-A3 单元时为 None。"""

    text = str(unit_id)
    matches = [client for client, prefix in PRE_A3_CLIENT_PREFIXES.items() if text.startswith(prefix)]
    return max(matches, key=lambda client: len(PRE_A3_CLIENT_PREFIXES[client])) if matches else None
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
    # 定向回归（E4-01，修复轮部署前提）：只含采集工具测试组。全集通过下执行器只执行静态依赖闭包里有改动文件的测试单元，
    # 其余承接记录库里的有效记录——这就是「按改动文件选出」。改动落在闭包选不出的地方（后端、前端、文档、出站规格）时
    # 用 --with-gates 人工补门禁项；全量门禁由后台验证（full-gates 组合）跑。
    "regression": ("test-capture-tools",),
}
# --with-gates 能补的门禁项：除 pre-A3 外的全部（pre-A3 要场景清单与认证坐标，走 entry／pre-a3 组合）。
EXTRA_GATE_CHOICES = tuple(gate for gate in MAKE_TEST_GATES + FULL_GATES_EXTRA)


def profile_gates(profile: str, with_gates: Sequence[str] = ()) -> tuple[str, ...]:
    """组合的门禁项加上人工补的（按 make test、全量门禁的固定顺序，去重）。"""

    if profile not in PROFILES:
        raise ValueError(f"未知的门禁组合：{profile}")
    unknown = sorted(set(with_gates) - set(EXTRA_GATE_CHOICES))
    if unknown:
        raise ValueError(f"--with-gates 只能补这些门禁项：{', '.join(EXTRA_GATE_CHOICES)}；不认识 {unknown}")
    wanted = set(PROFILES[profile]) | set(with_gates)
    return tuple(gate for gate in (*MAKE_TEST_GATES, *FULL_GATES_EXTRA, "pre-a3") if gate in wanted)
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
# pre-A3 门禁项按客户端的字面命令（B5a-04）：Codex 就是上表那一条（逐字不变），Claude 的场景单元命令另带 --client claude
# （B5a-06 起取客户端表的 client_argv，Codex 为空）。
PRE_A3_GATE_COMMANDS: dict[str, tuple[list[str], str]] = {
    client: ([*GATE_COMMANDS["pre-a3"][0], *_client_table().spec(client)["client_argv"]], GATE_COMMANDS["pre-a3"][1])
    for client in _client_table().CLIENTS
}


GO_PACKAGE_WRAPPER = "go_test_packages.py"


def go_package_argv(argv: Sequence[str]) -> list[str]:
    """后端 go test 门禁项实际执行的命令（优化第 3 项）：按包运行的包装器 ``go_test_packages.py``。

    门禁记录、P0 证据里写的字面命令仍是 ``GATE_COMMANDS`` 里的 ``go test … -count=1``（收尾门禁按它核对）；实际执行换成包装器：首轮核对
    ``go test -json`` 的包清单与 ``go list`` 的期望包集合逐个对上（没跑到的包按失败处理），补跑时只补跑失败的包。包装器自己固定带
    ``-count=1``（入口门禁缓存合同）。标签取自字面命令里的 ``-tags=…``。"""

    if list(argv[:2]) != ["go", "test"] or "-count=1" not in argv:
        raise ValueError(f"不是带 -count=1 的 go test 门禁命令：{list(argv)}")
    tags = next((item.split("=", 1)[1] for item in argv if item.startswith("-tags=")), "")
    return ["python3", "-B", str(HERE / GO_PACKAGE_WRAPPER), *(["--tags", tags] if tags else []), "--pattern", "./..."]


def gate_literal_command(gate_id: str, units: Sequence[str] = ()) -> tuple[list[str], str]:
    """门禁记录里写的字面命令：pre-a3 门禁项按它的场景单元所属客户端取（清单只属一个客户端，规划时已核对），
    其余门禁项照旧查表；没有单元时按 Codex（与改造前相同）。"""

    if gate_id == "pre-a3":
        clients = {pre_a3_unit_client(unit_id) for unit_id in units} - {None}
        return PRE_A3_GATE_COMMANDS[clients.pop() if len(clients) == 1 else "codex"]
    return GATE_COMMANDS.get(gate_id, (["make", "test"], "."))
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


def control_read_audit_for_plan(manifest: Mapping[str, Any]) -> Any:
    """当前四control规划选择固定adapter；普通客户端与未登记单元保持历史默认。"""
    selected = (manifest.get("audit_policy") or {}).get("units") or {}
    names = {"pre-a3:claude:" + name for name in (
        "vc-chain.claude-reconcile", "vc-chain.claude-deadline", "vc-chain.claude-root-cause", "vc-chain.claude-tool-evolution")}
    if not any(unit_id in names and "runtime_contract" in declaration for unit_id, declaration in selected.items()):
        return None
    key = "_entry_control_read_audit_" + str(HERE).replace("/", "_")
    if key not in sys.modules:
        path = HERE / "control_read_audit.py"
        if path.is_symlink() or not path.is_file():
            raise ValueError("当前四control固定adapter缺失或为链接")
        spec = importlib.util.spec_from_file_location(key, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[key] = module
        spec.loader.exec_module(module)
    module = sys.modules[key]
    if not module.selected_plan(manifest):
        raise ValueError("当前规划没有匹配四control固定合同")
    return module


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


def pre_a3_data_root_inputs(data_root: Path, client: str = "codex", *,
                            pre_a3_env: Mapping[str, str] | None = None) -> list[dict[str, Any]]:
    """pre-A3 场景读、但测试树里没有或不保证与测试树一致的数据根内容：与 E2-05 的 pre-A3 步骤同一份
    （``entry_steps.pre_a3_data_root``：冻结台账目录、录制数据、alpine 镜像、断言打包脚本、项目总账探测位置；Claude 另加
    它自己的总账目录的两处探测位置，B5a-06）。门禁清单生成时在数据根算好，作为已算明细（``resolved``）带给每个场景单元。"""

    steps = _entry_steps_module()
    ctx = steps.Context(params={"D": str(Path(data_root).resolve()), **dict(pre_a3_env or {})},
                        driver_dir=HERE, steps_dir=Path(tempfile.gettempdir()))
    return [steps.resolve(item, ctx) for item in steps.pre_a3_data_root(client)]


def pre_a3_fixture_environment(client: str, values: Mapping[str, str] | None) -> dict[str, str]:
    """只接收 Claude 正式入口登记的白名单来源；缺目录或经符号链接转向时在派发前失败，不让叶测试 skip 冒充认证。
    Codex 无新来源键，原来的环境合同保持不变。"""

    keys = _client_table().spec(client)["pre_a3_fixture_keys"]
    if not keys:
        return {}
    provided = dict(values or {})
    allowed = {"PYTHONPATH", "PYTHONPYCACHEPREFIX", "CODEX_UPGRADE_IDENTITY_MEMO", *keys}
    if set(provided) - allowed:
        raise ValueError(f"Claude pre-A3 不接受未登记的环境键：{sorted(set(provided) - allowed)}")
    result = {}
    for key in keys:
        value = provided.get(key)
        if not isinstance(value, str) or not value.startswith("/") or ".." in value.split("/"):
            raise ValueError(f"Claude pre-A3 缺少显式绝对来源目录：{key}")
        path = Path(value)
        if path.is_symlink() or not path.is_dir() or path.resolve() != path:
            raise ValueError(f"Claude pre-A3 来源目录不存在或经过符号链接：{key}")
        if not any(path.iterdir()):
            raise ValueError(f"Claude pre-A3 来源目录为空：{key}")
        result[key] = value
    return result


def pre_a3_fixture_resolved(client: str, values: Mapping[str, str] | None) -> list[dict[str, Any]]:
    """按与入口步骤完全相同的输入声明现算只读来源摘要，拒绝空树；调用者不能只透传环境而遗漏内容绑定。"""

    environment = pre_a3_fixture_environment(client, values)
    steps = _entry_steps_module()
    ctx = steps.Context(params=environment, driver_dir=HERE, steps_dir=Path(tempfile.gettempdir()))
    result = [steps.resolve(item, ctx) for item in steps.pre_a3_fixture_inputs(client)]
    if any(item.get("sha256") == "missing" or item.get("detail", {}).get("files") == 0 for item in result):
        raise ValueError("Claude pre-A3 来源目录为空或无法取得内容摘要")
    return result


def pre_a3_units_client(units: Path) -> str:
    """pre-A3 场景清单属于哪个客户端（按单元 ID 前缀）：没有可认的单元按 codex；混有多个客户端即拒绝（同 plan_gates）。"""

    payload = json.loads(Path(units).read_text(encoding="utf-8"))
    clients = {pre_a3_unit_client(unit.get("unit_id", "")) for unit in payload.get("units") or [] if isinstance(unit, dict)} - {None}
    if len(clients) > 1:
        raise ValueError(f"pre-A3 场景清单混有多个客户端的单元：{sorted(clients)}")
    return clients.pop() if clients else "codex"


def _extra_root_ranges(client: str) -> list[dict[str, Any]]:
    """客户端身份覆盖、但不在标准范围里的受管根（Claude 的 tools/official_client_control，B5a-06）：受管部分（含 schemas/，
    不含测试）与测试目录各一个范围。Codex 只有 official_client_capture（标准范围已覆盖），这里为空。"""

    ranges = []
    for root in _client_table().spec(client)["managed_roots"]:
        if root == MANAGED_TREE:
            continue
        prefix, name = root.strip("/") + "/", Path(root).name
        ranges.append({"category": "managed", "name": f"repo:{name}", "include": [prefix], "exclude": [f"{prefix}tests/"]})
        ranges.append({"category": "tests", "name": f"repo:{name}-tests", "include": [f"{prefix}tests/"], "exclude": []})
    return ranges


def pre_a3_inputs(test_file: str, data_root_inputs: list[dict[str, Any]] | None, client: str = "codex") -> dict[str, Any]:
    """pre-A3 场景单元的输入声明（E3-02）：按 E2-05 的 pre-A3 输入范围，测试按场景收窄。

    受管树（不含测试）、夹具、文档（两份指南在 docs/ 下）、部署脚本副本、场景运行器模块级导入的测试夹具
    （``PRE_A3_RUNNER_FIXTURES``）、场景测试模块的静态依赖闭包（执行器展开），加数据根算好的几项。前几样按测试树内容算：
    入口门禁跑 pre-A3 之前已核对数据根部署的受管树、指南与部署脚本就是测试树这一份。不含 HEAD 与仓库其余部分：场景在
    数据根运行，读不到 git，也没有后端、前端。

    B5a-06：Claude 场景单元另声明第二受管根 ``tools/official_client_control``（受管部分含 ``schemas/``，测试目录单列）：只改
    它的 ``store.py`` 或任一 schema，Claude 场景单元的输入摘要随之变化、场景失效重跑；Codex 场景单元不声明它，不受影响。
    Codex 的声明与改造前逐字相同。"""

    ranges = _unit_records_module().standard_ranges(MANAGED_TREE)
    return {"ranges": [ranges["managed"], ranges["tests-fixtures"], ranges["docs"], *_extra_root_ranges(client)],
            "files": [{"category": "managed", "path": PRE_A3_DEPLOY_SCRIPT},
                      *({"category": "tests", "path": fixture} for fixture in PRE_A3_RUNNER_FIXTURES)],
            "test_modules": [test_file], "resolved": list(data_root_inputs or [])}


RECORDED_SPEC_KEY = "CLAUDE_ENTRY_FULL79_SPEC"
RECORDED_SPEC_SHA_KEY = "CLAUDE_ENTRY_FULL79_SPEC_SHA256"
RECORDED_FULL79_ID = "pre-a3:claude:vc-chain.claude-full-validation-only"


def recorded_full79_bridge(path: Path, digest: str, *, client: str,
                           fixture_env: Mapping[str, str]) -> dict[str, Any]:
    """实读外持的完整79单元与原账；只接线，不生成世界、延期或承接资格。

    外持文件是九场景正规清单中已冻结的完整79单元，必须包含六项夹具和原账的真实预算来源全集。
    正规延期新增事件与收据也必须逐项冻结；旧包48项是当时的数量，不是删减新原件的上限。
    路径、记录根、CARRY意图和原账锚点全部保持原字节；目录内容与文件逐项重算后才允许派发。
    """
    from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
    from tools.official_client_capture.tests import claude_recorded_budget_carry as carry
    from tools.official_client_capture.tests import claude_recorded_full_validation_driver as full
    from tools.official_client_capture.tests import claude_recorded_chain_source as source

    def plain(value: Any) -> Path:
        if not isinstance(value, str):
            raise ValueError("完整79接线路径必须是字符串")
        result = Path(value)
        if (not result.is_absolute() or str(result) != value or ".." in result.parts
                or any(part.is_symlink() for part in (result, *result.parents))):
            raise ValueError("完整79接线只接受无链接的规范绝对路径")
        return result

    path = plain(str(path))
    if client != "claude" or not re.fullmatch(r"[0-9a-f]{64}", digest) or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError("完整79外持单元的客户端或SHA不符")
    unit = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(unit, dict) or set(unit) != {"unit_id", "argv", "cwd", "env", "cores", "memory_mb", "timeout_seconds", "weight", "inputs"}:
        raise ValueError("完整79外持单元字段不闭合")
    argv, env = unit["argv"], unit["env"]
    if (unit["unit_id"] != RECORDED_FULL79_ID or not isinstance(argv, list) or len(argv) != 12
            or argv[:8] != ["/usr/bin/python3", "-m", "tools.official_client_capture.codex_upgrade_pre_a3_certification",
                           "run-scenario", "--client", "claude", "--name", "vc-chain.claude-full-validation-only"]
            or argv[8] != "--staging-parent" or argv[10] != "--scenario-root" or not isinstance(env, dict)):
        raise ValueError("完整79接线不得改变场景、客户端或执行命令")
    parent, scenario = plain(argv[9]), plain(argv[11])
    if "staging" not in parent.parts or scenario.parent != parent:
        raise ValueError("完整79固定场景必须是staging父目录的直接子根")
    allowed = {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", *_client_table().spec("claude")["pre_a3_fixture_keys"],
               full.BUDGET_CARRY_INTENT_ENV, full.BUDGET_CARRY_DIGEST_ENV, "CLAUDE_RECORDED_CHAIN_RECORD_DIR"}
    if set(env) - allowed or any(env.get(key) != value for key, value in pre_a3_fixture_environment("claude", fixture_env).items()):
        raise ValueError("完整79外持环境含未批准键或与本轮夹具路径不同")
    for key in (full.BUDGET_CARRY_INTENT_ENV, "CLAUDE_RECORDED_CHAIN_RECORD_DIR", "PYTHONPATH"):
        plain(env.get(key))
    plain(unit["cwd"])
    if (unit["cores"] != 1 or unit["memory_mb"] != 1024 or unit["timeout_seconds"] !=
            certification.claude_unit_timeout_seconds("vc-chain.claude-full-validation-only")):
        raise ValueError("完整79额度或正式时间合同漂移")
    intent_path = plain(env[full.BUDGET_CARRY_INTENT_ENV])
    raw = json.loads(intent_path.read_text(encoding="utf-8"))
    data = plain(raw.get("data_root"))
    if data.name != "data" or data.parent.name != "staging" or data.parents[2] != scenario / "tmp":
        raise ValueError("完整79固定P/S/T/W/D与CARRY数据根不闭合")
    intent, binding = full._carry_intent(str(intent_path), env.get(full.BUDGET_CARRY_DIGEST_ENV), data)
    observed = datetime.now(timezone.utc)
    snapshot = carry._snapshot(Path(intent["predecessor_data"]), intent["predecessor_campaign_id"], observed)
    for field in ("project_head", "timing_head", "project_plan_sha256", "timing_plan_sha256"):
        if snapshot[field] != intent["expected_" + field]:
            raise ValueError("完整79原账外持锚点漂移：" + field)
    bindings = [binding, *snapshot["source_bindings"], *intent["preserved_bindings"].values()]
    steps = _entry_steps_module()
    ctx = steps.Context(params={}, driver_dir=HERE, steps_dir=path.parent)
    resolved, seen = [], set()
    for item in bindings:
        target = plain(item["path"])
        source.bound(item)
        if str(target) in seen:
            raise ValueError("完整79预算来源重复，不能改变冻结来源集合")
        resolved.append(steps.resolve(steps.Input("environment", "file", str(target)), ctx))
        seen.add(str(target))
    current = pre_a3_fixture_resolved("claude", fixture_env) + resolved
    test_file = certification.scenario_test_module("vc-chain.claude-full-validation-only", "claude").replace(".", "/") + ".py"
    if len(resolved) < 48 or unit["inputs"] != pre_a3_inputs(test_file, current, "claude"):
        raise ValueError("完整79外持六项夹具与真实预算来源全集缺失或内容漂移")
    return {"unit": unit, "resolved": resolved, "fixtures": current[:len(current) - len(resolved)],
            "spec_input": steps.resolve(steps.Input("environment", "file", str(path)), ctx)}


def verify_recorded_reuse(tree: Path, certification_path: Path, bridge: dict[str, Any], data_inputs: list[dict[str, Any]]) -> None:
    """原认证已由find-reusable验真后，另核当前静态输入；不把不可承接记录改成当前执行。

    独立九场景来源没有入口额外数据根声明时不追认它；已有额外声明则同样必须按当前值核对。
    """
    from tools.official_client_capture import codex_upgrade_pre_a3_certification as certification
    records = _unit_records_module()
    # 不依赖调用顺序的口头约定：入口按当前身份再核原认证及其已发布记录，损坏引用在此拒绝。
    payload = certification.verify_certification(certification_path, client="claude")
    store = certification._RecordStore(Path(payload["record_store"]))
    manifest = store.read(store.manifest_path(payload["unit_manifest"]["run_id"]))
    repo, cache = records.RepoIndex.load(tree), {}
    for name in certification.scenario_names("claude"):
        unit_id = certification.scenario_unit_prefix("claude") + name
        entry = next(item for item in manifest["units"] if item["unit_id"] == unit_id)
        record = store.read(store.record_path(unit_id, entry["record_sha256"]))
        fixed = bridge["fixtures"] + (bridge["resolved"] if unit_id == RECORDED_FULL79_ID else [])
        test_file = certification.scenario_test_module(name, "claude").replace(".", "/") + ".py"
        saved = {item["name"]: item for item in record["inputs"] if item["name"] != "reexecute-only-policy"}
        mandatory = records.declared_inputs(repo, pre_a3_inputs(test_file, fixed, "claude"), cache)
        optional = {item["name"]: item for item in [*data_inputs, bridge["spec_input"]]}
        expected = {item["name"]: item for item in mandatory}
        expected.update({key: optional[key] for key in saved if key in optional})
        if set(saved) != set(expected) or any(saved[key]["sha256"] != expected[key]["sha256"] for key in expected):
            raise ValueError("复用认证的当前输入已变化：" + unit_id)
        if unit_id == RECORDED_FULL79_ID:
            spec = record["spec"]
            keys = ("CLAUDE_RECORDED_CHAIN_RECORD_DIR", "CLAUDE_RECORDED_BUDGET_CARRY_INTENT", "CLAUDE_RECORDED_BUDGET_CARRY_INTENT_SHA256")
            if spec["argv"] != bridge["unit"]["argv"] or any(spec["env"].get(key) != bridge["unit"]["env"][key] for key in keys):
                raise ValueError("复用认证的固定79根或外持意图不同")


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
    with_gates: Sequence[str] = (),
    input_scopes: Sequence[dict[str, Any]] = (),
    audit_policy: dict[str, Any] | None = None,
    recorded_bridge: dict[str, Any] | None = None,
    declared_registry: Path | None = None,
) -> dict[str, Any]:
    """生成入口门禁清单（``unit-executor-gates/v1``）。

    测试树里的单元（测试组与命令单元）都套 ``launcher``（ARM64 上是私有挂载命名空间里遮住生产别名，与 isolated_run
    同一做法）；pre-A3 场景在数据根的生产布局里运行，不套隔离（受管树经生产别名访问的分支也要覆盖到），环境另给。

    E3-01：清单另带命令单元额度表（``scheduling``，算进调度策略版本）、环境事实（``environment``，算进环境指纹，
    由命令行入口在执行器同一份环境里算好传入）、每个命令单元的输入声明（``inputs``）。
    E3-02：pre-A3 场景清单是新形式（单元带 ``scenario_test_file``）时按 ``pre_a3_inputs`` 声明输入、可以承接；
    旧形式仍标不可承接。
    E4-01：``with_gates`` 是人工补的门禁项（定向回归选不出时），并进组合的门禁项（``profile_gates``）。
    声明级承接（步骤 5）：测试树里有登记表 ``tools/ci/command_input_scopes.json``（或 ``declared_registry`` 显式指定）时，
    登记为 declared 的命令单元改用声明级输入、登记为 rerun-only 的标为不可承接（``apply_declared_inputs``）；没有登记表、
    或单元没登记，输出与改造前逐字相同。
    """

    gates_wanted = profile_gates(profile, with_gates)
    if recorded_bridge is not None:
        if audit_policy is None:
            raise ValueError("固定79接线必须显式提供冻结的audit-policy，不能改变其它单元承接策略")
        if RECORDED_FULL79_ID in audit_policy.get("units", {}):
            raise ValueError("固定79尚无闭合运行时读集，不能登记为可承接")
    if ("pre-a3" in gates_wanted) != (pre_a3_units is not None):
        raise ValueError("含 pre-A3 的门禁组合（entry、pre-a3）必须给出 pre-A3 场景清单，其余组合不得给出")
    tree = Path(tree).resolve()
    workdir, backend = str(tree), str(tree / "backend")
    platform = platform or sys.platform
    units: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []

    def command(unit_id: str, argv: list[str], cwd: str, quota: dict[str, Any], weight: float, env: dict[str, str] | None = None,
                extra_inputs: list[dict[str, Any]] | None = None) -> str:
        unit = {"unit_id": unit_id, "argv": [*launcher, *argv], "cwd": cwd, **quota, "weight": weight,
                "inputs": command_inputs(extra_inputs), "reservation_restartable": True}
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
                argv = line.split()
                # 语法检查已固定 Bash；显式绑定 SHELL，避免白名单环境缺值时无关的用户数据库／NSS 查询。
                # 环境值进入单元规格摘要；其它实际脚本执行仍保留原环境。
                syntax_env = {"SHELL": "/bin/bash"} if argv[:2] == ["/bin/bash", "-n"] else None
                members.append(command(_deploy_unit_id(line), argv, workdir, DEPLOY_QUOTA, GATE_SECONDS["deploy-scripts"], syntax_env))
            if not members:
                raise ValueError("部署脚本测试在本平台一项都不执行")
            gates.append({"gate_id": gate_id, "units": members, "not_executed": skipped})
        elif gate_id == "pre-a3":
            assert pre_a3_units is not None
            payload = json.loads(Path(pre_a3_units).read_text(encoding="utf-8"))
            if payload.get("schema_version") != COMMANDS_SCHEMA or not isinstance(payload.get("units"), list) or not payload["units"]:
                raise ValueError(f"pre-A3 场景清单格式非法：{pre_a3_units}")
            unit_clients = {pre_a3_unit_client(unit.get("unit_id", "")) for unit in payload["units"]}
            if len(unit_clients - {None}) > 1:
                raise ValueError(f"pre-A3 场景清单混有多个客户端的单元：{sorted(item for item in unit_clients if item)}")
            unit_client = next(iter(unit_clients - {None}), "codex")
            fixture_inputs = pre_a3_fixture_resolved(unit_client, pre_a3_env)
            fixture_names = {item["name"] for item in fixture_inputs}
            data_inputs = [item for item in pre_a3_data_inputs or [] if item["name"] not in fixture_names] + fixture_inputs
            members = []
            for unit in payload["units"]:
                if not str(unit.get("unit_id", "")).startswith(PRE_A3_PREFIX):
                    raise ValueError(f"pre-A3 场景清单里有非 pre-A3 单元：{unit.get('unit_id')}")
                merged = {key: value for key, value in unit.items() if key != "scenario_test_file"}
                # 按客户端去掉完整前缀；Claude 场景名不能残留 claude:，否则已测额度永远匹配不上。
                unit_prefix = PRE_A3_CLIENT_PREFIXES[pre_a3_unit_client(unit["unit_id"]) or "codex"]
                cores, seconds = pre_a3_quota(str(unit["unit_id"]).removeprefix(unit_prefix))
                merged.update(cores=cores, memory_mb=PRE_A3_MEMORY_MB)
                test_file = unit.get("scenario_test_file")
                if isinstance(test_file, str) and test_file:
                    merged["inputs"] = pre_a3_inputs(test_file, data_inputs, pre_a3_unit_client(unit["unit_id"]) or "codex")
                else:
                    merged.update(inheritable=False, not_inheritable_reason=PRE_A3_NOT_INHERITABLE)
                if seconds is not None:
                    merged["weight"] = seconds
                if pre_a3_env:
                    merged["env"] = {**(unit.get("env") or {}), **pre_a3_env}
                # 正式 Claude 链保留日志与录制副本。它们是本单元的产物，不把可变产物内容写进来源输入摘要。
                if fixture_inputs and "--staging-parent" in merged["argv"]:
                    parent = Path(merged["argv"][merged["argv"].index("--staging-parent") + 1])
                    if not parent.is_absolute() or "staging" not in parent.parts or ".." in parent.parts:
                        raise ValueError("Claude pre-A3 记录父目录必须位于显式 staging 根")
                    record_id = hashlib.sha256(unit["unit_id"].encode()).hexdigest()[:16]
                    merged["env"] = {**merged.get("env", {}),
                                     "CLAUDE_RECORDED_CHAIN_RECORD_DIR": str(parent / "claude-chain-records" / record_id)}
                if recorded_bridge is not None and unit["unit_id"] == RECORDED_FULL79_ID:
                    # 原P/S/T/W/D、record与CARRY保持外持值；仅当前代码位置由已核部署的数据根提供。
                    approved = recorded_bridge["unit"]
                    merged = {**approved, "cwd": unit["cwd"], "env": {**approved["env"], "PYTHONPATH": unit["cwd"]},
                              "inputs": pre_a3_inputs(test_file, data_inputs + recorded_bridge["resolved"] +
                                                       [recorded_bridge["spec_input"]], "claude")}
                units.append(merged)
                members.append(unit["unit_id"])
            gates.append({"gate_id": gate_id, "units": members})
    # 优化第 3 项：后端三组 go test 实际由按包运行的包装器执行（门禁记录里的字面命令不变）。放在单元生成之后统一替换，上面生成单元的行不动。
    for gate_id in ("backend-go-test", "backend-unit", "backend-integration"):
        for unit in units:
            if unit["unit_id"] == f"backend:{gate_id.removeprefix('backend-')}":
                unit["argv"] = [*launcher, *go_package_argv(GATE_COMMANDS[gate_id][0])]
    manifest = {"schema_version": GATES_SCHEMA, "profile": profile, "test_groups": groups, "units": units, "gates": gates,
                "scheduling": scheduling_table()}
    if environment is not None:
        manifest["environment"] = environment
    if audit_policy is not None:
        unit_ids = {unit["unit_id"] for unit in units}
        if isinstance(audit_policy, dict) and isinstance(audit_policy.get("units"), dict) and set(audit_policy["units"]) - unit_ids:
            unit_ids.update(_planned_test_ids(tree, groups))
        _unit_records_module().validate_audit_policy(audit_policy, unit_ids)
        manifest["audit_policy"] = audit_policy
        for unit in units:
            if unit["unit_id"] in audit_policy["units"]:
                unit["inputs"] = audit_policy["units"][unit["unit_id"]]
    apply_declared_inputs(manifest, tree, launcher=launcher, input_scopes=input_scopes, registry_path=declared_registry)
    apply_input_scopes(manifest, input_scopes)
    return manifest


def verify_scope_workspace(tree: Path, bytecode_cache: Path, *, commit: str, bundle: Path, branch: str) -> dict[str, Any]:
    """收窄复验沿用原树和缓存；只读核验身份，禁止用重建掩盖快照漂移。"""
    for path in (tree, bytecode_cache, bundle):
        if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError("收窄验收工作区必须使用无符号链接的绝对路径")
    if not tree.is_dir() or not bytecode_cache.is_dir() or not bundle.is_file():
        raise ValueError("收窄验收需要保留原测试树、字节码缓存和普通 bundle 文件")
    if bytecode_cache == tree or tree in bytecode_cache.parents:
        raise ValueError("字节码缓存必须位于测试树外")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("收窄验收需要完整提交号")
    git = ["git", "--no-optional-locks", "-C", str(tree)]
    def read_git(*args: str) -> str:
        result = subprocess.run([*git, *args], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError("收窄工作区 Git 凭证核验失败：" + " ".join(args[:2]))
        return result.stdout.strip()
    if read_git("rev-parse", "HEAD") != commit or read_git("status", "--porcelain", "--untracked-files=all"):
        raise ValueError("原测试树提交不符或存在未登记变更，须重新建立读集")
    read_git("bundle", "verify", str(bundle))
    refs = [line.split() for line in read_git("bundle", "list-heads", str(bundle)).splitlines()]
    if not any(value == commit and ref in {branch, "refs/heads/" + branch} for value, ref in refs):
        raise ValueError("bundle 分支与保留测试树的提交不一致")
    return {"status": "passed", "tree": str(tree), "bytecode_cache": str(bytecode_cache), "commit": commit,
            "bundle_sha256": _unit_records_module().file_sha256(bundle), "workspace_rebuilt": False}


def prepare_scope_successor(tree: Path, bytecode_cache: Path, *, commit: str, bundle: Path, branch: str,
                            lock_directory: Path, lock_owner: int) -> dict[str, Any]:
    """入口持锁后只原位更新普通文件内容；保留未改实物，仍由原工作区与读集消费者核验。

    不使用 checkout 替换文件，也不恢复时间戳。仅接受干净的分离 HEAD、真实后继及同模式内容差异；新增、删除、
    改名、链接、模式变化均拒绝。Git 对象／index／HEAD 的真实变化照常保留，读取它们的记录仍按实际输入失效。
    """
    for path in (tree, bytecode_cache, bundle, lock_directory):
        if not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents)):
            raise ValueError("后继工作区只接受无符号链接的绝对路径")
    if (tree.name != "test-tree" or not tree.is_dir() or not (tree / ".git").is_dir()
            or (tree / ".git").is_symlink() or not bytecode_cache.is_dir() or not bundle.is_file()):
        raise ValueError("后继准备需要原 test-tree 克隆、普通 bundle 与现有缓存")
    if bytecode_cache == tree or tree in bytecode_cache.parents:
        raise ValueError("字节码缓存必须位于测试树外")
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("后继准备需要完整提交号")
    work = tree.parent
    expected_lock = work.parent / ".entry-gates-locks" / hashlib.sha256(str(work).encode()).hexdigest() / "held"

    def check_lock() -> None:
        if lock_directory != expected_lock or lock_owner != os.getppid() or lock_owner <= 1:
            raise ValueError("后继准备必须由持有本 WORK 锁的入口父进程调用")
        for directory in (lock_directory, work / ".entry-gates.lock"):
            pid_path = directory / "pid"
            if (any(path.is_symlink() for path in (directory, pid_path)) or not directory.is_dir()
                    or not pid_path.is_file() or directory.stat().st_uid != os.geteuid()
                    or pid_path.stat().st_uid != os.geteuid() or pid_path.read_text().strip() != str(lock_owner)):
                raise ValueError("后继准备的入口锁或兼容锁不属于当前父进程")
            if stat.S_IMODE(directory.stat().st_mode) != 0o700 or stat.S_IMODE(pid_path.stat().st_mode) != 0o600:
                raise ValueError("后继准备的入口锁权限不符")

    git = ["git", "--no-optional-locks", "-C", str(tree), "-c", "core.hooksPath=/dev/null",
           "-c", "gc.auto=0", "-c", "maintenance.auto=false"]

    def run_git(*args: str) -> bytes:
        result = subprocess.run([*git, *args], capture_output=True, stdin=subprocess.DEVNULL)
        if result.returncode:
            raise ValueError("后继工作区 Git 核验失败：" + " ".join(args[:2]))
        return result.stdout

    def require_clean() -> None:
        if run_git("status", "--porcelain", "--untracked-files=all"):
            raise ValueError("原测试树存在未登记变更，拒绝后继准备")
        if any(row[:1] == b"S" or row[:1].islower() for row in run_git("ls-files", "-v", "-z").split(b"\0") if row):
            raise ValueError("原测试树含隐藏工作区变化的索引标记")

    check_lock()
    if run_git("rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("后继准备拒绝浅克隆")
    if subprocess.run([*git, "symbolic-ref", "-q", "HEAD"], capture_output=True).returncode != 1:
        raise ValueError("后继准备只接受分离 HEAD，不修改工作分支")
    require_clean()
    old_head = run_git("rev-parse", "HEAD").decode().strip()
    bundle_sha = _unit_records_module().file_sha256(bundle)
    run_git("bundle", "verify", str(bundle))
    refs = [line.split() for line in run_git("bundle", "list-heads", str(bundle)).decode().splitlines()]
    if not any(value == commit and ref in {branch, "refs/heads/" + branch} for value, ref in refs):
        raise ValueError("bundle 分支与后继提交不一致")
    run_git("fetch", "--no-tags", "--no-write-fetch-head", "--no-auto-maintenance", str(bundle), branch)
    run_git("merge-base", "--is-ancestor", old_head, commit)
    raw = run_git("diff-tree", "--no-commit-id", "--no-renames", "--raw", "-r", "-z", old_head, commit).split(b"\0")
    changes: list[tuple[Path, bytes, bytes, os.stat_result]] = []
    for index in range(0, len(raw) - 1, 2):
        fields = raw[index].decode().split()
        relative = raw[index + 1].decode()
        if (len(fields) != 5 or fields[0] not in (":100644", ":100755") or fields[1] != fields[0][1:]
                or fields[4] != "M" or not relative or Path(relative).is_absolute() or ".." in Path(relative).parts):
            raise ValueError("后继只允许同模式普通文件内容修改；新增、删除、改名或模式变化须另行处理")
        path = tree / relative
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise ValueError("后继差异路径含符号链接")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError("后继差异必须是单链接普通文件")
        old_data = run_git("cat-file", "blob", fields[2])
        new_data = run_git("cat-file", "blob", fields[3])
        if path.read_bytes() != old_data:
            raise ValueError("后继差异文件与旧提交原始字节不符")
        changes.append((path, old_data, new_data, info))
    if not changes:
        raise ValueError("后继提交没有普通文件内容差异")
    require_clean()
    if run_git("rev-parse", "HEAD").decode().strip() != old_head or _unit_records_module().file_sha256(bundle) != bundle_sha:
        raise ValueError("后继准备期间原 HEAD 或 bundle 变化")
    check_lock()
    # 所有差异先验证并读齐，再开始写入；不创建/替换目录项，不把已变元数据伪装回来源状态。
    for path, old_data, new_data, info in changes:
        check_lock()
        descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "r+b") as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino, opened.st_nlink, opened.st_mode) !=
                    (info.st_dev, info.st_ino, info.st_nlink, info.st_mode) or stream.read() != old_data):
                raise ValueError("后继差异文件在原位写入前变化，拒绝继续")
            stream.seek(0)
            stream.write(new_data)
            stream.truncate()
            stream.flush()
            os.fsync(stream.fileno())
    check_lock()
    run_git("read-tree", commit)
    run_git("update-ref", "--no-deref", "HEAD", commit, old_head)
    result = verify_scope_workspace(tree, bytecode_cache, commit=commit, bundle=bundle, branch=branch)
    print(f"后继原位准备：{old_head} → {commit}，更新 {len(changes)} 个普通文件；未重建测试树或缓存", file=sys.stderr)
    return result


def _planned_test_ids(tree: Path, groups: Sequence[dict[str, Any]]) -> set[str]:
    """登记测试合同时复用现有只读规划入口；执行器仍须按本轮实际调度配置再核对。"""
    unit_ids: set[str] = set()
    for group in groups:
        completed = subprocess.run([sys.executable, "-B", str(HERE / "unit_executor.py"), "plan",
                                    "--start", group["start"], "--pattern", group["pattern"]],
                                   cwd=tree, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if completed.returncode:
            raise _unit_records_module().RecordsError("测试单元规划失败，不能登记未知单元：" + completed.stderr.strip()[-300:])
        payload = json.loads(completed.stdout)
        unit_ids.update(unit["unit_id"] for unit in payload["units"])
    return unit_ids


def apply_input_scopes(manifest: dict[str, Any], scopes: Sequence[dict[str, Any]],
                       *, test_specs: Mapping[str, dict[str, Any]] | None = None) -> None:
    """提案必须重放；测试规格只有发现后才能完整核对，清单仅运输，不赋予旧记录承接资格。"""
    if not scopes:
        return
    records = _unit_records_module()
    audit = records._audit_module()
    units = {unit["unit_id"]: unit for unit in manifest["units"]}
    seen = set()
    test_scopes = []
    for scope in scopes:
        unit_id = scope.get("unit_id")
        spec = scope.get("spec") or {}
        is_test = spec.get("type") == "test"
        if unit_id in seen or (not is_test and unit_id not in units):
            raise ValueError("收窄提案重复或不属于当前命令或测试单元")
        seen.add(unit_id)
        for key in ("source_record", "source_trace"):
            binding = scope.get(key) or {}
            path = Path(binding.get("path", ""))
            if (not path.is_absolute() or any(item.is_symlink() for item in (path, *path.parents))
                    or records.file_sha256(path) != binding.get("sha256")):
                raise ValueError("收窄来源证据缺失或摘要漂移")
        replay = audit.narrow_input_scope(Path(scope["source_record"]["path"]), Path(scope["source_trace"]["path"]))
        if replay != scope:
            raise ValueError("收窄提案无法重放，不能自填路径或改成已启用")
        if is_test:
            if test_specs is None:
                # plan 尚无最终拆块；这里只核运输目标，实际 ID 与完整规格由 run-gates 发现后核对。
                if not any(spec.get("start") == group["start"] and spec.get("pattern") == group["pattern"]
                           for group in manifest.get("test_groups", [])):
                    raise ValueError("收窄提案不属于当前测试组")
            elif unit_id not in test_specs or spec != test_specs[unit_id]:
                raise ValueError("收窄提案的测试单元不存在或完整规格与当前单元不同")
            test_scopes.append(scope)
            continue
        unit = units[unit_id]
        spec = scope["spec"]
        for key, value in (("type", "command"), ("unit_id", unit_id), ("argv", unit["argv"]), ("cwd", unit["cwd"]),
                           ("env", {k: v for k, v in unit.get("env", {}).items() if k not in records.ENV_CACHE_ONLY}),
                           ("cores", unit["cores"]), ("memory_mb", unit["memory_mb"]),
                           ("exclusive", bool(unit.get("exclusive", False))), ("timeout_seconds", unit["timeout_seconds"])):
            if spec.get(key) != value:
                raise ValueError("收窄提案的命令、环境或额度与当前单元不同：" + key)
        unit["inputs"] = scope["inputs"]
        unit["input_scope"] = scope
    if test_scopes:
        manifest["test_input_scopes"] = test_scopes


# ---------------------------------------------------------------------------
# 命令单元声明级承接（declared-inputs/v1，步骤 5 的 WP2）：登记表加载与自检、合同探测、plan 接线
# ---------------------------------------------------------------------------
# 背景：65 个命令单元原来都声明「整个仓库四段＋HEAD」，改任何一个无关文件、换一次提交，65 个全部失效。声明级承接让命令的
# 输入声明由真实读集采样得出（登记表＋证据文件），机器核查「采样路径全部落在声明范围内」，再绑定一组外部输入合同（工具链、
# 缓存、服务镜像、git 历史、make 展开、HOME 配置）；改一个无关文件后这些单元不再重跑。
#
# 本节只做 entry_gates 一侧，分三块：
# 1. 登记表读取与自检（plan 阶段，失败关闭：登记表自相矛盾就拒绝生成清单，立刻暴露，而不是留到执行时才发现）；
# 2. 合同探测（生产侧，需要子进程；执行器每次算「当前事实」时重新探测，见 declared_probe）；
# 3. plan_gates 接线（apply_declared_inputs）。
# 范围校验、覆盖检查与消费者核验在 unit_records.py（validate_declared_scope、declared_coverage），这里只调用它们的公开函数，
# 不重复实现；执行器侧的采样与前置核验在 unit_executor.py。
#
# 与 control 四链的关系：四链单元（pre-a3:claude:vc-chain.claude-{reconcile,deadline,root-cause,tool-evolution}）靠 audit_policy
# 与固定 sidecar 取得读集适配器。本节对它们零触碰：登记表禁止登记任何 pre-a3 单元，不读写 audit_policy，清单上的
# declared_inputs 汇总是独立顶层键；control_read_audit_for_plan 的选择逻辑一字未动。

DECLARED_REGISTRY_SCHEMA = "declared-inputs-registry/v1"
DECLARED_SCOPE_SCHEMA = "declared-scope/v1"
DECLARED_SAMPLE_SCHEMA = "declared-sample/v1"
DECLARED_CONTRACT_SCHEMA = "declared-contract/v1"
DECLARED_SUMMARY_SCHEMA = "declared-inputs-summary/v1"
# 登记表与采样证据都放在测试树里随提交走：改登记表不需要重部署驱动，也不动驱动清单（与 deploy_tests 读 CI 定义同一做法）。
DECLARED_REGISTRY_PATH = "tools/ci/command_input_scopes.json"
DECLARED_SAMPLES_DIR = "tools/ci/command_input_samples"
DECLARED_STATUSES = ("declared", "rerun-only")
# 命令单元全集同时出现的组合：登记表里的单元必须都在规划里（拼错单元 ID 要立刻报错）；其余组合（preflight、pre-a3、
# regression）只规划其中一部分，登记了但本次没规划的单元直接跳过。
DECLARED_FULL_PROFILES = ("full-gates", "entry")
_DECLARED_SHA256 = re.compile(r"[0-9a-f]{64}")
_DECLARED_COMMIT = re.compile(r"[0-9a-f]{40}")
_DECLARED_MAKE_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]*")
_DECLARED_IMAGE_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:@+-]*")
_DECLARED_SAMPLE_REQUIRED = ("evidence", "evidence_sha256", "command_sha256", "launcher_sha256", "sampler_sha256")
_DECLARED_SAMPLE_OPTIONAL = ("sampled_commit", "sampled_at_utc", "runs", "reaudit_every_days")

# ---- 合同定义表 ----
# 都是代码常量：改动会改变执行器版本（本文件在 EXECUTOR_FILES 里），声明级记录整体重新产生一次，所以只登记评审过的合同。
# 登记表里的 scope.contracts 只写合同 ID（类型:名字），定义不由登记表给出——否则登记表可以自己放宽合同。
DECLARED_TOOLCHAINS: dict[str, list[str]] = {
    "go": ["go", "version"], "node": ["node", "--version"], "pnpm": ["pnpm", "--version"], "make": ["make", "--version"],
}
# go 命令行为受这些环境变量的「生效值」影响（go env 已合并环境变量与 go env -w 写下的配置文件）。
DECLARED_GO_ENV_KEYS = ("GOFLAGS", "GOPROXY", "GOTOOLCHAIN", "GOEXPERIMENT", "CGO_ENABLED", "GOENV")
DECLARED_GO_TOOLS = ("compile", "link", "asm", "vet")
# 命令运行期离线要求（调研 Q8，调度者定为可选项）：GOFLAGS=-mod=readonly、GOPROXY=off、GOTOOLCHAIN=local 让 go 命令既不联网
# 下载模块，也不因 go.mod 的 go 行较新而悄悄切换到下载来的工具链（那会让工具链藏进 GOMODCACHE，成为不在合同里的输入）。
# 开关：把 DECLARED_REQUIRE_OFFLINE 改成 True（改本文件即改执行器版本，声明级记录整体重新产生一次），或在创建探测会话时
# 显式传 require_offline=True。打开后 toolchain:go 条目带 requires.env_equals 与逐项核对结果（requirements），
# 门禁环境没有真的注入这三个变量时 requirements.met 为 False，declared_contract_problems 会报告，单元不得承接。
# 步骤 5 工作包 WP3：调度者 10-14 定案首发就开（ARM64 摸底证明 10 个首发命令在离线下都能通过，采样也是在这三个变量下做的，
# 证据只对离线运行成立）。变量由 apply_declared_inputs 写进 declared Go 单元的清单 env；执行器探测合同时把单元 env 里的 Go 变量叠到
# 探测环境上，所以 requirements.met 反映的是单元真正会看到的值。
DECLARED_REQUIRE_OFFLINE = True
DECLARED_OFFLINE_ENV = {"GOFLAGS": "-mod=readonly", "GOPROXY": "off", "GOTOOLCHAIN": "local"}
# 缓存合同的稳定声明（角色、位置类别、规则）：不含缓存内容，也不含绝对路径。缓存内容会随运行由冷变热，进了摘要，
# 首次执行就会让所有未受影响的单元失去承接资格；位置只写类别（work＝工作根内，tree＝测试树内，tmp＝临时目录），
# 这样跨工作根也不失效。requires 里的 go_test_argv_contains 的含义：命令是 go test 时必须带这些参数（防测试结果缓存成为隐藏输入）。
DECLARED_CACHES: dict[str, dict[str, Any]] = {
    "go-build-cache": {"role": "performance-cache", "location": "work", "environment": "GOCACHE", "rule": "any-content-addressed",
                       "requires": {"go_test_argv_contains": ["-count=1"]}},
    "go-mod-cache": {"role": "dependency-content", "location": "work", "environment": "GOMODCACHE", "rule": "lock-derived-go-sum",
                     "lock_file": "backend/go.sum"},
    "go-tmp": {"role": "scratch", "location": "work", "environment": "GOTMPDIR", "rule": "any-scratch"},
    "tmp": {"role": "scratch", "location": "tmp", "environment": "TMPDIR", "rule": "any-scratch"},
    "node-modules": {"role": "dependency-install", "location": "tree", "path": "frontend/node_modules", "rule": "lock-derived-pnpm",
                     "lock_file": "frontend/pnpm-lock.yaml", "installed_lock": "frontend/node_modules/.pnpm/lock.yaml"},
    "vite-cache": {"role": "performance-cache", "location": "tree", "path": "frontend/node_modules/.vite", "rule": "any-content-addressed"},
    "tsbuildinfo": {"role": "performance-cache", "location": "tree", "path": "frontend", "glob": "tsconfig*.tsbuildinfo",
                    "rule": "any-content-addressed"},
}
# 服务镜像：引用从源码常量解析（不在登记表里写死），解析失败 plan 失败。postgres 可被 SUB2API_TEST_POSTGRES_IMAGE 覆盖，
# 覆盖只对 postgres: 前缀生效（selectDockerImage，integration_harness_test.go）；redis 固定。门禁白名单环境不放行这个变量，
# 所以门禁里实际引用恒为默认标签，但合同仍按「执行器环境里该变量的有无」计算，将来放行时不会漏绑。
# testcontainers 起任何容器都会先起 Ryuk 回收容器，它的镜像由 testcontainers-go 的 ReaperDefaultImage 常量决定（v0.40.0 起固定、
# 不可用环境变量覆盖），是不在测试源码里的隐藏输入，所以单独作为 ryuk 合同绑定。
DECLARED_IMAGE_SOURCE_FILE = "backend/internal/repository/integration_harness_test.go"
DECLARED_SERVICE_IMAGES: dict[str, dict[str, str]] = {
    "postgres": {"const": "postgresImageTag", "override_env": "SUB2API_TEST_POSTGRES_IMAGE", "override_prefix": "postgres:"},
    "redis": {"const": "redisImageTag"},
    "ryuk": {"module": "github.com/testcontainers/testcontainers-go", "go_mod": "backend/go.mod", "const": "ReaperDefaultImage"},
}
# Ryuk 默认镜像登记表（testcontainers-go 版本 → ReaperDefaultImage）。解析顺序：GOMODCACHE 里该版本源码
# （internal/config/config.go）优先，没有（plan 在预热编译之前，模块缓存还是空的）就用本表；两处都没有就是「解析不到」：
# plan 阶段拒绝（登记方法：升级 testcontainers-go 时把新版本的值补进本表，测试会核对本表与 backend/go.mod 的版本一致）。
# 解析来源只写进条目的 detail（resolved_from），不进摘要——否则缓存由冷变热会改变摘要。
DECLARED_RYUK_DEFAULT_IMAGES = {"v0.40.0": "testcontainers/ryuk:0.13.0"}
DECLARED_RYUK_SOURCE = "internal/config/config.go"
# testcontainers 的 Hub 前缀配置（环境变量或 ~/.testcontainers.properties 的 hub.image.name.prefix）会给所有 Docker Hub 镜像
# 加前缀，实际镜像名不再是源码常量；检测到时镜像条目失败关闭（image_id 记 unavailable），单元不得承接。
DECLARED_HUB_PREFIX_ENV = "TESTCONTAINERS_HUB_IMAGE_NAME_PREFIX"
DECLARED_HUB_PREFIX_PROPERTY = "hub.image.name.prefix"
# HOME 下会影响命令行为、但现状没有任何东西绑定的配置文件：存在则内容摘要，不存在记 missing（缺失也是一种状态）。
# host-config 合同的编号是开放的：采样器（declared_sampler）按采到的 {HOME}/ 路径派生编号（{HOME}/.config/go/env →
# host-config:config-go-env），路径写在 scope.outside 的 home-config 行里（prefix 是 {HOME}/ 下的精确文件，contract 指向合同编号），
# 探测时从 scope 取路径——这样采样器新采到一个 HOME 配置，不必改本文件就能绑定；绑定只会增加被监视的文件，不会放宽任何合同。
# scope 里没有对应 outside 行时，用下表兜底（编号按同一条派生规则：去掉开头的点，非字母数字换成连字符）。
DECLARED_HOST_CONFIGS = {"gitconfig": ".gitconfig", "docker-config-json": ".docker/config.json", "npmrc": ".npmrc",
                         "config-go-env": ".config/go/env", "testcontainers-properties": ".testcontainers.properties"}
DECLARED_TESTCONTAINERS_PROPERTIES = ".testcontainers.properties"
_DECLARED_HOST_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
# 条目 detail 里只供人读、不进摘要的字段（探测到的可执行文件路径、make 展开文本前缀、解析来源）。
DECLARED_INFO_KEYS = frozenset({"text", "executable", "resolved_from", "error"})
# make -n 展开探测要尽量只读（make 自己并不保证 -n 只读，见下面的补充）：下面这些写法会在 make 解析期执行任意命令或写文件，出现就不做展开合同（单元转 rerun-only）。
# $(shell/wildcard/eval/call) 与 include 是调研指定的；另加 file/load/guile/exec（同样有副作用）与 `VAR != cmd`（shell 赋值）。
# 函数名后必须紧跟空白才算函数调用、include 后必须跟文件名才算指令，所以 $(file_list)、$(filter …) 或叫 include-check 的目标
# 不会误伤；以 TAB 开头的是配方行，不做指令与赋值判断（配方里的 $(shell …) 仍然会被第一条抓到——make 打印配方时也会展开它）。
# 整行注释先去掉再扫。
# 步骤 5 WP3b 补充（独立验收一般 2）：-n 并不保证只读——GNU Make 手册（「实际执行」一节）说明，带 + 前缀的配方行（+ 可与 @、- 混在行首）、含
# $(MAKE)／${MAKE} 的配方行（递归 make 要继续传 -n，所以不被拦住），以及重建 makefile 自身的规则（目标是 Makefile／makefile／GNUmakefile，
# include 进来的文件同理，-n 也照样先重建 makefile）都会在 -n 下真的执行。所以扫描另挡三类：自重建规则与 .RECIPEPREFIX（配方行不再以 TAB 开头，
# 扫描就认不出配方行）是解析期的，看整份文件；+ 行与 $(MAKE) 是执行期的，只看声明目标的先决条件闭包里的配方行（闭包解析不了就退回整份文件，
# 不让与声明单元无关的目标——真实仓库的 build-backend、test-backend、test-frontend 就用 $(MAKE)——挡住所有展开合同）。
# 保证的范围就是这份扫描：命中的写法不做展开合同；没命中却仍有副作用的写法（例如配方宏展开出 + 行）不在保证内，靠 Makefile 评审兜底。探测时另清空
# MAKEFLAGS 与 MAKEFILES，免得调用方环境里的 -j、-t 之类选项或额外 makefile 改变展开语义。
_DECLARED_MAKE_UNSAFE = re.compile(
    r"[$][({]\s*(?:shell|wildcard|eval|call|file|load|guile|exec)(?=\s)"
    r"|^[ ]*-?(?:include|sinclude|load)(?=[ \t]+\S)"
    r"|^(?!\t)[ ]*(?:(?:export|override|private)[ \t]+)*[^\s:=#]+[ ]*!=", re.M)
# 在 -n 下仍会真的执行的配方行：以 + 标记（+ 可与 @、- 任意顺序混在行首的前缀里），或含 $(MAKE)／${MAKE}。
_DECLARED_MAKE_ALWAYS_RUN = re.compile(r"^\t[@\- \t]*\+|[$][({]MAKE[)}]", re.M)
_DECLARED_MAKE_RECIPEPREFIX = re.compile(r"^[ ]*\.RECIPEPREFIX\b", re.M)
# make 启动时会先重建的 makefile 自身的名字（GNU Make 按 GNUmakefile、makefile、Makefile 的顺序找）。
_DECLARED_MAKEFILE_NAMES = frozenset({"GNUmakefile", "makefile", "Makefile"})
# 粗解析 Makefile 规则用：变量引用、赋值行、目标特定变量、特殊目标。
_DECLARED_MAKE_VAR_REF = re.compile(r"[$](?:\(([A-Za-z0-9_.-]+)\)|\{([A-Za-z0-9_.-]+)\})")
_DECLARED_MAKE_ASSIGN = re.compile(r"^(?:(?:export|override|private)[ \t]+)*([A-Za-z0-9_.-]+)[ \t]*(:{1,3}=|[?+!]?=)[ \t]*(.*)$")
_DECLARED_MAKE_TARGET_VARIABLE = re.compile(r"^[^:=#]*:[ \t]*(?:(?:export|override|private)[ \t]+)*[A-Za-z0-9_.-]+[ \t]*(?::{1,3}=|[?+!]?=)")
_DECLARED_MAKE_SPECIAL_TARGETS = frozenset({".PHONY", ".SUFFIXES", ".PRECIOUS", ".INTERMEDIATE", ".SECONDARY", ".NOTPARALLEL", ".ONESHELL",
                                            ".POSIX", ".SILENT", ".IGNORE", ".LOW_RESOLUTION_TIME", ".DELETE_ON_ERROR",
                                            ".EXPORT_ALL_VARIABLES", ".WAIT"})


def _declared_records() -> Any:
    """声明级承接依赖 unit_records 的两个纯函数（validate_declared_scope、declared_coverage）；缺失时给出明确原因。"""

    records = _unit_records_module()
    missing = [name for name in ("validate_declared_scope", "declared_coverage") if not hasattr(records, name)]
    if missing:
        raise ValueError("unit_records.py 缺少声明级承接所需的函数：" + "、".join(missing))
    return records


def _declared_sha256_json(value: Any) -> str:
    """与执行器、记录同一份规范化 JSON 摘要，保证这里算的和消费者重算的逐字节一致。"""

    return _unit_records_module().sha256_json(value)


def _declared_file_sha256(path: Path) -> str:
    """文件内容摘要（跟随符号链接读目标）；不存在记 missing、不是普通文件记 not-a-file、读不了记 unreadable(原因)：
    这些值都与有内容时的摘要不同，状态变化可见。"""

    try:
        if not path.exists():
            return "missing"
        if not path.is_file():
            return "not-a-file"
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as error:
        return f"unreadable({type(error).__name__})"


def _declared_fail(unit_id: str | None, message: str) -> ValueError:
    return ValueError(f"声明级承接登记表：{'单元 ' + unit_id + ' ' if unit_id else ''}{message}")


def declared_launcher_sha256(launcher: Sequence[str]) -> str:
    """启动前缀（计划里单元 argv 开头的 unshare 等隔离命令）的规范化摘要。启动前缀不属命令身份，但它改变命令看到的文件
    系统（ARM64 上遮住生产别名），采样时与现在不同就必须重采，所以单独登记、单独核对。本机 plan 的启动前缀为空时是空数组的摘要。"""

    return _declared_sha256_json([str(part) for part in launcher])


def declared_command_sha256(argv: Sequence[str], cwd: str | Path | None, tree: Path) -> str:
    """命令身份摘要：去掉启动前缀的 argv ＋ 相对测试树的工作目录（测试树根记 ``.``）的规范化 JSON 摘要。
    采样证据里的 ``command.sha256`` 用同一算法（``sha256_json({"argv", "cwd_rel"})``），命令变了证据就不再适用。"""

    cwd_rel = "." if cwd is None else Path(cwd).relative_to(Path(tree)).as_posix()
    return _declared_sha256_json({"argv": [str(part) for part in argv], "cwd_rel": cwd_rel})


# ---- 登记表读取与自检 ----
# 登记表格式（declared-inputs-registry/v1）：
#   {"schema_version": "declared-inputs-registry/v1",
#    "units": {
#      "<单元ID>": {"status": "declared", "scope": <declared-scope/v1>,
#                   "sample": {"evidence": "tools/ci/command_input_samples/<名>.sample.json", "evidence_sha256": <证据文件原始字节的 sha256>,
#                              "command_sha256": <declared_command_sha256>, "launcher_sha256": <declared_launcher_sha256>,
#                              "sampler_sha256": <采样器摘要>, 可选：sampled_commit、sampled_at_utc、runs、reaudit_every_days}},
#      "<单元ID>": {"status": "rerun-only", "reason": "<为什么只重跑>", 可选 "head": true（读 git 历史的单元，保留旧的 head 条目）}}}
# 采样证据文件（declared-sample/v1）由 declared_sampler 产生，登记表的各个摘要都由它起草；自检把它们与计划逐项核对。


def _declared_registry_file(tree: Path, path: Path | None) -> Path:
    return Path(path) if path is not None else Path(tree) / DECLARED_REGISTRY_PATH


def _declared_tree_path_problem(tree: Path, path: Path) -> str | None:
    """路径在测试树之下时：从测试树到文件的每一级都不许是符号链接，且 resolve 之后仍在测试树内；有问题返回原因，没有返回 None。
    独立验收一般 5：只拒绝最后一级符号链接时，整个 tools/ci 或样本目录是指向树外的符号链接也能通过。测试树自己的上级目录不查（临时目录常带
    符号链接，例如 macOS 的 /var）；不在测试树之下的路径（命令行 --declared-registry 显式指定的外部登记表，测试用）不归这里管。"""

    root = Path(os.path.abspath(tree))
    try:
        relative = Path(os.path.abspath(path)).relative_to(root)
    except ValueError:
        return None
    current = root
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            return f"{current} 是符号链接"
    try:
        current.resolve().relative_to(root.resolve())
    except ValueError:
        return f"{path} 解析之后不在测试树内"
    return None


def load_declared_registry(tree: Path, *, path: Path | None = None) -> dict[str, Any] | None:
    """读测试树的登记表 ``tools/ci/command_input_scopes.json``；缺失返回 None（行为即旧声明，逐字不变）。

    ``path`` 显式指定（命令行 ``--declared-registry``，测试用）时必须存在。格式只做外形检查（顶层键闭合、schema 对），
    单元级的完整自检在 ``validate_declared_registry``。符号链接一律拒绝：登记表要随提交走，不能指到树外。"""

    target = _declared_registry_file(tree, path)
    if target.is_symlink():
        raise _declared_fail(None, f"登记表是符号链接：{target}")
    problem = _declared_tree_path_problem(tree, target)
    if problem is not None:
        raise _declared_fail(None, f"登记表的路径不在测试树的真实目录内（{problem}）：{target}")
    if not target.exists():
        if path is not None:
            raise _declared_fail(None, f"指定的登记表不存在：{target}")
        return None
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except RecursionError as error:
        raise _declared_fail(None, f"登记表的 JSON 嵌套过深，解析不了：{target}") from error
    except (OSError, ValueError) as error:
        raise _declared_fail(None, f"登记表读不出来或不是 JSON：{target}（{error}）") from error
    if (not isinstance(payload, dict) or set(payload) != {"schema_version", "units"}
            or payload["schema_version"] != DECLARED_REGISTRY_SCHEMA or not isinstance(payload["units"], dict)):
        raise _declared_fail(None, f"登记表格式非法（顶层键须恰为 schema_version、units，schema 为 {DECLARED_REGISTRY_SCHEMA}）：{target}")
    return payload


def _declared_known_contract(ident: str) -> bool:
    """合同编号是不是评审过的：工具链、缓存、服务镜像是封闭表；host-config 编号开放（只要格式合法），
    路径是否存在由 ``_declared_host_config_paths`` 另核。"""

    kind, _, name = ident.partition(":")
    return ((kind == "toolchain" and name in DECLARED_TOOLCHAINS) or (kind == "cache" and name in DECLARED_CACHES)
            or (kind == "service-image" and name in DECLARED_SERVICE_IMAGES)
            or (kind == "host-config" and _DECLARED_HOST_NAME.fullmatch(name) is not None))


def _declared_host_config_paths(scope: Mapping[str, Any], name: str) -> list[str]:
    """host-config:<name> 绑定的 HOME 相对路径（升序、去重）：取 scope.outside 里引用这个合同的 ``{HOME}/…`` 精确文件；
    没有这样的行就用内置表兜底；都没有返回空列表。prefix 不是 {HOME}/ 下的精确文件（目录前缀、``..``、空名）就抛 ValueError——
    home-config 逐路径登记，目录内容会随运行变化（例如遥测计数），不能当成稳定的配置绑定。"""

    head, paths = "{HOME}/", set()
    for row in scope.get("outside") or []:
        if isinstance(row, Mapping) and row.get("contract") == f"host-config:{name}":
            prefix = row.get("prefix")
            if (not isinstance(prefix, str) or not prefix.startswith(head) or prefix.endswith("/") or len(prefix) == len(head)
                    or ".." in prefix.split("/") or "\\" in prefix):
                raise ValueError(f"host-config:{name} 的路径必须是 {head} 下的精确文件：{prefix!r}")
            paths.add(prefix[len(head):])
    if not paths and name in DECLARED_HOST_CONFIGS:
        paths.add(DECLARED_HOST_CONFIGS[name])
    return sorted(paths)


def _declared_launcher_prefix(unit_id: str, unit: Mapping[str, Any], launcher: Sequence[str] | None, launcher_sha256: str) -> list[str]:
    """单元 argv 里的启动前缀。给了 launcher（plan_gates 的实参）就以它为准；没给（直接调自检函数）时按登记的
    ``launcher_sha256`` 在 argv 的前缀里反查——反查不到就是启动前缀与采样时不同。"""

    argv = unit["argv"]
    if launcher is not None:
        prefix = [str(part) for part in launcher]
        if argv[:len(prefix)] != prefix or len(argv) == len(prefix):
            raise _declared_fail(unit_id, "的命令不以计划的启动前缀开头，或去掉前缀后没有命令")
    else:
        for size in range(len(argv)):
            if declared_launcher_sha256(argv[:size]) == launcher_sha256:
                prefix = list(argv[:size])
                break
        else:
            raise _declared_fail(unit_id, "登记的 launcher_sha256 与计划里的启动前缀不一致（采样时的隔离环境与现在不同，必须重采）")
    if declared_launcher_sha256(prefix) != launcher_sha256:
        raise _declared_fail(unit_id, "登记的 launcher_sha256 与计划里的启动前缀不一致（采样时的隔离环境与现在不同，必须重采）")
    return prefix


def _declared_load_evidence(unit_id: str, tree: Path, relative: Any) -> tuple[dict[str, Any], str]:
    """读采样证据文件：只许 tools/ci/command_input_samples/ 下一层的 .json 普通文件，返回（解析结果, 原始字节的 sha256）。"""

    pure = PurePosixPath(relative) if isinstance(relative, str) and relative and "\\" not in relative else None
    if (pure is None or pure.is_absolute() or ".." in pure.parts or str(pure) != relative or pure.suffix != ".json"
            or str(pure.parent) != DECLARED_SAMPLES_DIR or pure.name.startswith(".")):
        raise _declared_fail(unit_id, f"的证据路径必须是 {DECLARED_SAMPLES_DIR}/ 下的 .json 文件：{relative!r}")
    path = Path(tree) / pure
    if path.is_symlink() or not path.is_file():
        raise _declared_fail(unit_id, f"的证据文件不存在或是符号链接：{relative}")
    problem = _declared_tree_path_problem(Path(tree), path)
    if problem is not None:
        raise _declared_fail(unit_id, f"的证据路径不在测试树的真实目录内（{problem}）：{relative}")
    raw = path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8"))
    except RecursionError as error:
        raise _declared_fail(unit_id, f"的证据文件 JSON 嵌套过深，解析不了：{relative}") from error
    except ValueError as error:
        raise _declared_fail(unit_id, f"的证据文件不是 JSON：{relative}（{error}）") from error
    return evidence, hashlib.sha256(raw).hexdigest()


def _declared_check_contracts(unit_id: str, scope: Mapping[str, Any], tree: Path) -> None:
    """合同层面的自检（范围与覆盖由 unit_records 的函数负责）：合同 ID 都是评审过的、make 展开要带 make 版本、
    服务镜像要把 Ryuk 一并绑定、镜像引用能从源码解析（解析失败 plan 失败）、git 提交号是完整的 40 位。"""

    contracts = [str(item) for item in scope.get("contracts") or []]
    unknown = sorted(item for item in contracts if not _declared_known_contract(item))
    if unknown:
        raise _declared_fail(unit_id, f"登记了未评审的合同：{unknown}")
    if scope.get("make_expansion") and "toolchain:make" not in contracts:
        raise _declared_fail(unit_id, "声明了 make 展开合同，必须同时声明 toolchain:make（make 版本变了展开语义可能变）")
    for item in contracts:
        if item.startswith("host-config:"):
            try:
                paths = _declared_host_config_paths(scope, item.partition(":")[2])
            except ValueError as error:
                raise _declared_fail(unit_id, f"的合同 {item} 不合法：{error}") from error
            if not paths:
                raise _declared_fail(unit_id, f"的合同 {item} 没有对应的 {{HOME}}/ 路径（scope.outside 里没有引用它的行，也不是内置的常见配置）")
    images = [item.partition(":")[2] for item in contracts if item.startswith("service-image:")]
    if images and "ryuk" not in images:
        raise _declared_fail(unit_id, "声明了服务镜像合同，必须同时声明 service-image:ryuk（testcontainers 起任何容器都会先起 Ryuk）")
    for name in images:
        try:
            _declared_image_default(Path(tree), name, os.environ, None)
        except ValueError as error:
            raise _declared_fail(unit_id, f"的服务镜像 {name} 引用解析不了：{error}") from error
    repo = scope.get("repo") if isinstance(scope.get("repo"), Mapping) else {}
    history = repo.get("git_history") if isinstance(repo.get("git_history"), Mapping) else {}
    for commit in history.get("commits") or []:
        if not isinstance(commit, str) or not _DECLARED_COMMIT.fullmatch(commit):
            raise _declared_fail(unit_id, f"的 git_history 提交号必须是完整的 40 位小写十六进制：{commit!r}")
    for target in scope.get("make_expansion") or []:
        if not isinstance(target, str) or not _DECLARED_MAKE_TARGET.fullmatch(target):
            raise _declared_fail(unit_id, f"的 make 展开目标名非法：{target!r}")


def _declared_check_entry(unit_id: str, entry: Mapping[str, Any], unit: Mapping[str, Any], tree: Path,
                          launcher: Sequence[str] | None) -> dict[str, Any]:
    """一个 declared 登记项的完整自检，返回写进计划的声明（scope 全文＋证据绑定三摘要）。任何一项不符都抛 ValueError。

    顺序：采样绑定字段形状 → 命令与启动前缀摘要和计划一致 → 证据文件摘要 → 证据自身一致 → scope 合法 → 登记时覆盖
    必须 ok（不 ok 说明登记表自相矛盾，立即暴露）→ 合同自检。"""

    records = _declared_records()
    sample = entry["sample"]
    required, optional = set(_DECLARED_SAMPLE_REQUIRED), set(_DECLARED_SAMPLE_OPTIONAL)
    if not isinstance(sample, Mapping) or (required - set(sample)) or (set(sample) - required - optional):
        raise _declared_fail(unit_id, f"的 sample 字段非法（必填 {sorted(required)}，可选 {sorted(optional)}）")
    for key in ("evidence_sha256", "command_sha256", "launcher_sha256", "sampler_sha256"):
        if not isinstance(sample[key], str) or not _DECLARED_SHA256.fullmatch(sample[key]):
            raise _declared_fail(unit_id, f"的 sample.{key} 必须是 64 位小写十六进制")
    prefix = _declared_launcher_prefix(unit_id, unit, launcher, sample["launcher_sha256"])
    command = list(unit["argv"][len(prefix):])
    try:
        command_sha256 = declared_command_sha256(command, unit.get("cwd"), tree)
    except ValueError as error:
        raise _declared_fail(unit_id, f"的工作目录不在测试树内：{unit.get('cwd')!r}") from error
    if command_sha256 != sample["command_sha256"]:
        raise _declared_fail(unit_id, "登记的 command_sha256 与计划里的命令不一致（命令或工作目录变了，证据不再适用，必须重采）")
    evidence, evidence_sha256 = _declared_load_evidence(unit_id, tree, sample["evidence"])
    if evidence_sha256 != sample["evidence_sha256"]:
        raise _declared_fail(unit_id, "的证据文件摘要与登记的 evidence_sha256 不一致（证据被改过或登记表没同步）")
    recorded = evidence.get("command") if isinstance(evidence, Mapping) else None
    if (not isinstance(evidence, Mapping) or evidence.get("schema_version") != DECLARED_SAMPLE_SCHEMA or evidence.get("unit_id") != unit_id
            or not isinstance(recorded, Mapping) or not isinstance(recorded.get("argv"), list)
            or not isinstance(recorded.get("launcher"), list) or not isinstance(recorded.get("cwd_rel"), str)):
        raise _declared_fail(unit_id, f"的证据文件不是属于本单元的 {DECLARED_SAMPLE_SCHEMA}")
    if evidence.get("valid", True) is not True:
        # 采样器（declared_sampler）自己判过采样有效性（退出码、git status 前后、未跟踪读取、网络、写入范围等）；
        # 它判无效的证据只能登记 rerun-only，不能当 declared 的依据。
        raise _declared_fail(unit_id, f"的证据文件标记采样无效：{list(evidence.get('invalid_reasons') or [])[:3]}")
    if (declared_launcher_sha256(recorded["launcher"]) != sample["launcher_sha256"]
            or _declared_sha256_json({"argv": recorded["argv"], "cwd_rel": recorded["cwd_rel"]}) != recorded.get("sha256")
            or recorded.get("sha256") != sample["command_sha256"]):
        raise _declared_fail(unit_id, "的证据文件里记录的命令或启动前缀与登记的摘要不一致")
    sampled = evidence.get("sampled")
    if isinstance(sampled, Mapping) and sampled.get("sampler_sha256") not in (None, sample["sampler_sha256"]):
        raise _declared_fail(unit_id, "的证据文件记录的采样器摘要与登记的 sampler_sha256 不一致")
    try:
        scope = records.validate_declared_scope(copy.deepcopy(entry["scope"]))
    except (ValueError, TypeError, KeyError) as error:
        raise _declared_fail(unit_id, f"的 scope 不合法：{error}") from error
    try:
        coverage = records.declared_coverage(evidence, scope)
    except (ValueError, TypeError, KeyError) as error:
        raise _declared_fail(unit_id, f"的覆盖检查无法完成（证据或 scope 形状不对）：{error}") from error
    if not isinstance(coverage, Mapping) or coverage.get("ok") is not True:
        undeclared = coverage.get("undeclared") if isinstance(coverage, Mapping) else None
        raise _declared_fail(unit_id, f"登记时采样覆盖检查不通过（登记表自相矛盾）：{list(undeclared or [])[:5]}")
    _declared_check_contracts(unit_id, scope, tree)
    if scope.get("make_expansion") and os.path.basename(command[0]) != "make":
        # 步骤 5 WP3b（独立验收一般 7）：Makefile 靠 make 展开合同豁免（不必再整份算进输入）只对 make 命令成立。别的命令（例如 go test）要是自己去读
        # Makefile，Makefile 里无关目标的改动不会让它失效，漏掉对 Makefile 的真实依赖，所以单元命令（去掉启动前缀后）的第一个词必须是 make。
        raise _declared_fail(unit_id, f"声明了 make 展开合同，但单元命令的第一个词是 {command[0]!r} 而不是 make（展开合同豁免 Makefile，只对 make 命令成立）")
    return {"scope": scope, "sample": {"evidence_sha256": sample["evidence_sha256"], "command_sha256": sample["command_sha256"],
                                       "sampler_sha256": sample["sampler_sha256"]}}


def _declared_resolve(registry: Any, units: Sequence[Mapping[str, Any]], tree: Path, *, launcher: Sequence[str] | None,
                      audit_policy: Any, input_scopes: Sequence[Mapping[str, Any]], strict: bool) -> dict[str, Any]:
    """登记表自检的全部逻辑，返回应用所需的结果（validate_declared_registry 只是丢弃结果的薄封装）。

    * 单元 ID：禁止登记 pre-a3:*（control 四链就在其中）；同一单元不能同时在 audit_policy 与登记表里登记，
      也不能与 --input-scope 收窄提案重复（它们各自要求不同的审计合同，并存会互相覆盖）；
    * ``strict``：登记的单元必须都在规划里；非 strict 时登记了但本次没规划的单元跳过（preflight 等组合只规划一部分）。
      仅在 macOS 上执行的部署脚本测试在 Linux 上不规划，登记了也不算缺；
    * rerun-only 必须写明原因；declared 的证据、命令、启动前缀、覆盖与合同逐项核对（见 _declared_check_entry）。"""

    tree = Path(tree).resolve()
    if (not isinstance(registry, Mapping) or set(registry) != {"schema_version", "units"}
            or registry.get("schema_version") != DECLARED_REGISTRY_SCHEMA or not isinstance(registry.get("units"), Mapping)):
        raise _declared_fail(None, "登记表格式非法")
    planned = {str(unit.get("unit_id")): unit for unit in units if isinstance(unit, Mapping)}
    policy_units = audit_policy.get("units") if isinstance(audit_policy, Mapping) else None
    audit_ids = set(policy_units) if isinstance(policy_units, Mapping) else set()
    scope_ids = {str(item.get("unit_id")) for item in input_scopes if isinstance(item, Mapping)}
    tolerated_missing = {_deploy_unit_id(line) for line in MACOS_ONLY_DEPLOY_TESTS}
    declared: dict[str, dict[str, Any]] = {}
    rerun_only: dict[str, dict[str, Any]] = {}
    not_in_plan: list[str] = []
    for unit_id in sorted(registry["units"], key=str):
        entry = registry["units"][unit_id]
        if not isinstance(unit_id, str) or not unit_id:
            raise _declared_fail(None, f"单元 ID 非法：{unit_id!r}")
        if unit_id.startswith(PRE_A3_PREFIX):
            raise _declared_fail(unit_id, "是 pre-A3 场景单元（control 四链就在其中），禁止登记声明级承接")
        if unit_id in audit_ids:
            raise _declared_fail(unit_id, "已在 audit_policy 里登记，不能同时登记声明级承接")
        if unit_id in scope_ids:
            raise _declared_fail(unit_id, "已有 --input-scope 收窄提案，不能同时登记声明级承接")
        if not isinstance(entry, Mapping) or entry.get("status") not in DECLARED_STATUSES:
            raise _declared_fail(unit_id, f"的 status 必须是 {'、'.join(DECLARED_STATUSES)}")
        if entry["status"] == "rerun-only":
            reason = entry.get("reason")
            if set(entry) - {"status", "reason", "head"} or not isinstance(reason, str) or not reason.strip():
                raise _declared_fail(unit_id, "登记为 rerun-only 必须写明原因（reason），且只许 status、reason、head 三个键")
            if "head" in entry and not isinstance(entry["head"], bool):
                raise _declared_fail(unit_id, "的 head 必须是布尔值")
        elif set(entry) != {"status", "scope", "sample"}:
            raise _declared_fail(unit_id, "登记为 declared 的键须恰为 status、scope、sample")
        unit = planned.get(unit_id)
        if unit is None:
            if strict and unit_id not in tolerated_missing:
                raise _declared_fail(unit_id, "不在本次规划的命令单元里（单元 ID 写错，或已改名、退役）")
            not_in_plan.append(unit_id)
            continue
        argv = unit.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(part, str) for part in argv):
            raise _declared_fail(unit_id, "在计划里不是命令单元")
        if entry["status"] == "rerun-only":
            rerun_only[unit_id] = {"reason": entry["reason"].strip(), "head": bool(entry.get("head", False))}
        else:
            declared[unit_id] = _declared_check_entry(unit_id, entry, unit, tree, launcher)
    return {"declared": declared, "rerun_only": rerun_only, "not_in_plan": not_in_plan}


def validate_declared_registry(registry: Any, units: Sequence[Mapping[str, Any]], tree: Path, *, launcher: Sequence[str] | None = None,
                               audit_policy: Any = None, input_scopes: Sequence[Mapping[str, Any]] = (), strict: bool = True) -> None:
    """登记表自检（plan 阶段，失败关闭）：非法抛 ValueError，通过返回 None。

    ``units`` 是规划好的命令单元（清单 ``units``）。可选参数：``launcher`` 不给时按登记的 launcher_sha256 在 argv 前缀里反查；
    ``audit_policy``、``input_scopes`` 给出时核对重复登记；``strict=False`` 时登记了但本次没规划的单元跳过。详见 ``_declared_resolve``。"""

    _declared_resolve(registry, units, tree, launcher=launcher, audit_policy=audit_policy, input_scopes=input_scopes, strict=strict)


def _declared_make_logical_lines(text: str) -> list[str]:
    """Makefile 的逻辑行：行尾奇数个反斜杠把下一行接上来（接缝处补一个空格——配方行里的 + 前缀只看逻辑行的行首）。"""

    logical: list[str] = []
    pending = ""
    for line in text.split("\n"):
        merged = pending + line
        if (len(merged) - len(merged.rstrip("\\"))) % 2 == 1:
            pending = merged[:-1] + " "
            continue
        logical.append(merged)
        pending = ""
    if pending:
        logical.append(pending.rstrip())
    return logical


def _declared_make_colon(line: str) -> int | None:
    """规则行里第一个不在 $(…)／${…} 内的冒号的位置；没有返回 None。"""

    depth, index = 0, 0
    while index < len(line):
        char = line[index]
        if char == "$" and index + 1 < len(line) and line[index + 1] in "({":
            depth, index = depth + 1, index + 2
            continue
        if char in ")}" and depth > 0:
            depth -= 1
        elif char == ":" and depth == 0:
            return index
        index += 1
    return None


def _declared_make_expand(text: str, variables: Mapping[str, Sequence[str]], depth: int = 0, unreliable: frozenset[str] = frozenset()) -> str | None:
    """展开 $(NAME)／${NAME}：只认 Makefile 里用 = := ::= += 定义过的普通变量；函数调用、自动变量、没定义的变量（可能来自环境）、用 ?= 定义的变量
    （环境里同名的会覆盖它）、展开过深都返回 None（调用方据此退回整份文件的保守扫描）。"""

    if depth > 8:
        return None
    out: list[str] = []
    index = 0
    while index < len(text):
        if text[index] != "$":
            out.append(text[index])
            index += 1
            continue
        match = _DECLARED_MAKE_VAR_REF.match(text, index)
        if match is None or (match.group(1) or match.group(2)) not in variables or (match.group(1) or match.group(2)) in unreliable:
            return None
        inner = _declared_make_expand(" ".join(variables[match.group(1) or match.group(2)]), variables, depth + 1, unreliable)
        if inner is None:
            return None
        out.append(inner)
        index = match.end()
    return "".join(out)


def _declared_make_macro_lines(logical: Sequence[str]) -> list[str]:
    """define … endef 宏体里的行。宏展开出来的行也可能带 + 前缀（make 看的是展开之后的行首），所以不管哪个目标引用它，宏体都在扫描范围内。"""

    lines: list[str] = []
    inside = False
    for line in logical:
        if inside:
            if re.match(r"[ \t]*endef\b", line):
                inside = False
            else:
                lines.append(line)
        elif re.match(r"[ \t]*define[ \t]", line):
            inside = True
    return lines


def _declared_make_parse(logical: Sequence[str]) -> dict[str, tuple[list[str], list[str]]] | None:
    """粗解析 Makefile 的显式规则，只服务「make -n 会执行哪些配方行」这一个问题：返回 {目标: (先决条件, 配方行)}。
    认识的写法之外一律返回 None（模式规则与静态模式规则、后缀规则、.DEFAULT、.SECONDEXPANSION、先决条件里的函数调用或没定义的变量、通配符……），
    调用方退回整份文件的保守扫描——这里宁可解析不了，也不漏掉一条会执行的配方。条件指令里的规则不管条件真假一律计入（取并集）；
    define 宏体里的行不是规则，由 _declared_make_macro_lines 另行收集。"""

    variables: dict[str, list[str]] = {}
    unreliable: set[str] = set()
    rules: dict[str, tuple[list[str], list[str]]] = {}
    current: list[str] = []
    in_define = False
    for line in logical:
        if in_define:
            in_define = re.match(r"[ \t]*endef\b", line) is None
            continue
        if re.match(r"[ \t]*define[ \t]", line):
            in_define, current = True, []
            continue
        if line.startswith("\t"):
            for target in current:
                rules[target][1].append(line)
            continue
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue   # 空行与注释行不结束规则（配方行之间可以夹着它们）
        current = []
        if re.match(r"(?:ifeq|ifneq|ifdef|ifndef|else|endif)\b", stripped):
            continue
        assignment = _DECLARED_MAKE_ASSIGN.match(stripped)
        if assignment is not None:
            name, operator, value = assignment.groups()
            if operator == "+=":
                variables.setdefault(name, []).append(value)
            elif operator in ("=", ":=", "::=", ":::="):
                variables[name] = [value]
            elif operator == "?=":
                variables.setdefault(name, [value])
                unreliable.add(name)   # 环境里同名的会覆盖它：只有真被目标名或先决条件引用到时才放弃解析
            continue
        if _DECLARED_MAKE_TARGET_VARIABLE.match(stripped):
            continue   # 目标特定变量：不是依赖关系
        colon = _declared_make_colon(stripped)
        if colon is None:
            if re.match(r"(?:export|unexport|vpath|undefine)\b", stripped):
                continue
            return None
        head, rest = stripped[:colon], stripped[colon + 1:]
        rest = rest[1:] if rest.startswith(":") else rest   # 双冒号规则
        prerequisite_text, _, inline = rest.partition(";")
        if _declared_make_colon(prerequisite_text) is not None:
            return None   # 静态模式规则
        frozen = frozenset(unreliable)
        names, prerequisites = (_declared_make_expand(head, variables, 0, frozen), _declared_make_expand(prerequisite_text, variables, 0, frozen))
        if names is None or prerequisites is None or any(char in names + prerequisites for char in "*?["):
            return None   # 解析不了，或带通配符（make 解析时会按文件系统展开）
        targets = [item[2:] if item.startswith("./") else item for item in names.split()]
        if any("%" in item or item in (".DEFAULT", ".SECONDEXPANSION") or (item.startswith(".") and item not in _DECLARED_MAKE_SPECIAL_TARGETS)
               for item in targets):
            return None
        targets = [item for item in targets if item not in _DECLARED_MAKE_SPECIAL_TARGETS]
        for target in targets:
            entry = rules.setdefault(target, ([], []))
            entry[0].extend(item for item in prerequisites.replace("|", " ").split())
            if inline.strip():
                entry[1].append("\t" + inline.strip())
        current = targets
    return rules


def _declared_make_self_rebuild(logical: Sequence[str]) -> str | None:
    """规则的目标里有 Makefile／makefile／GNUmakefile（自重建规则）就返回该目标名。make 启动时会先重建 makefile 自己，-n 也一样，
    规则的配方因此真的执行。按规则行的原始文本找（不展开变量），赋值行与配方行不看。"""

    for line in logical:
        stripped = line.strip()
        if line.startswith("\t") or not stripped or stripped.startswith("#") or _DECLARED_MAKE_ASSIGN.match(stripped):
            continue
        colon = _declared_make_colon(stripped)
        if colon is None or stripped[colon + 1:colon + 2] == "=":
            continue
        for item in stripped[:colon].split():
            if (item[2:] if item.startswith("./") else item) in _DECLARED_MAKEFILE_NAMES:
                return item
    return None


def _declared_make_reachable_recipes(logical: Sequence[str], targets: Sequence[str]) -> str | None:
    """声明目标的先决条件闭包里的全部配方行（再加所有 define 宏体——宏展开出的行也可能带 +），拼成一段文本；解析不了返回 None。"""

    rules = _declared_make_parse(logical)
    if rules is None:
        return None
    seen: set[str] = set()
    pending = [item[2:] if item.startswith("./") else item for item in targets]
    recipes = ["\t" + line for line in _declared_make_macro_lines(logical)]   # 宏体当配方行扫（补一个 TAB，行首前缀的判断才一致）
    while pending:
        target = pending.pop()
        if target in seen:
            continue
        seen.add(target)
        prerequisites, lines = rules.get(target, ([], []))
        recipes.extend(lines)
        pending.extend(item[2:] if item.startswith("./") else item for item in prerequisites)
    return "\n".join(recipes)


def _declared_make_problem(tree: Path, targets: Sequence[str] | None = None) -> str | None:
    """测试树的 Makefile 能不能做 make -n 展开合同：含解析期求值的写法（见 _DECLARED_MAKE_UNSAFE）、自重建规则、.RECIPEPREFIX，以及 -n 下仍会
    执行的配方行（+ 行、$(MAKE)，见 _DECLARED_MAKE_ALWAYS_RUN）就不能，返回原因；可以返回 None。这份扫描就是只读保证的全部范围（没命中扫描的副作用写法不在保证内）。
    ``targets`` 给了就只看这些目标的先决条件闭包里的配方行（执行期的写法只有被执行到才有副作用）；不给，或闭包解析不了，就看整份文件。
    只看 make 实际会读的那一份（GNUmakefile > makefile > Makefile）。"""

    for name in ("GNUmakefile", "makefile", "Makefile"):
        path = Path(tree) / name
        if path.is_file():
            break
    else:
        return "测试树没有 Makefile，make 展开合同无法探测"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        return f"读不到 {name}：{error}"
    match = _DECLARED_MAKE_UNSAFE.search(re.sub(r"^[ ]*#.*$", "", text, flags=re.M))
    if match:
        return f"{name} 含解析期求值的写法（{match.group(0).strip()[:40]}）：make -n 展开探测不是只读，不能用 make 展开合同，改为只重跑"
    # 步骤 5 WP3b：解析期的另两类（看整份文件）——自重建规则（make -n 也会先重建 makefile 自己）与 .RECIPEPREFIX（配方行不再以 TAB 开头）。
    cleaned = re.sub(r"^[ ]*#.*$", "", text, flags=re.M)
    logical = _declared_make_logical_lines(cleaned)
    rebuilt = _declared_make_self_rebuild(logical)
    if rebuilt is not None:
        return f"{name} 含重建 makefile 自身的规则（目标 {rebuilt}）：make -n 展开探测不是只读，不能用 make 展开合同，改为只重跑"
    if _DECLARED_MAKE_RECIPEPREFIX.search(cleaned):
        return f"{name} 改了 .RECIPEPREFIX（配方行不再以 TAB 开头，扫描认不出配方行）：make -n 展开探测不是只读，不能用 make 展开合同，改为只重跑"
    # 执行期的（+ 行、$(MAKE)）：给了目标就只看它们的先决条件闭包里的配方行，闭包解析不了或没给目标就看整份文件。
    scope, scanned = "", cleaned + "".join("\n\t" + line for line in _declared_make_macro_lines(logical))
    if targets:
        reachable = _declared_make_reachable_recipes(logical, targets)
        if reachable is not None:
            scope, scanned = f"声明目标 {'、'.join(str(item) for item in targets)} 的先决条件闭包里", reachable
    always = _DECLARED_MAKE_ALWAYS_RUN.search(scanned)
    if always:
        line = scanned[scanned.rfind("\n", 0, always.start()) + 1:].split("\n", 1)[0].strip()
        return (f"{name} {scope}含 make -n 下仍会真的执行的写法（{line[:60]}）：make -n 展开探测不是只读，不能用 make 展开合同，改为只重跑")
    return None


def apply_declared_inputs(manifest: dict[str, Any], tree: Path, *, launcher: Sequence[str] = (),
                          input_scopes: Sequence[Mapping[str, Any]] = (), registry_path: Path | None = None) -> None:
    """在 plan_gates 末尾对清单原地后处理：

    * 登记表缺失 → 什么都不做，输出与改造前逐字相同，也不加 ``declared_inputs`` 键；登记表存在但没有登记任何本次规划的
      单元 → 各单元仍与改造前逐字相同，只多出（计数为零的）``declared_inputs`` 汇总键，记下登记表的摘要；
    * ``status=declared`` 的命令单元：``inputs`` 换成 ``{"declared_scope": scope, "declared_sample": {evidence_sha256,
      command_sha256, sampler_sha256}}``（展开由 unit_records 做，不含 head；合同条目由执行器每次算当前事实时用 declared_probe 追加）。
      声明了 make 展开合同、而 Makefile 含解析期求值写法、自重建规则，或声明目标的先决条件闭包里有 -n 下仍会执行的配方行（+ 行、$(MAKE)）的，
      转为 rerun-only（保守扫描挡住已知会在 make -n 下执行命令的写法；没命中扫描的副作用写法不在保证内，见 _DECLARED_MAKE_UNSAFE 旁的说明）；
    * ``status=rerun-only``：``inheritable: false`` 与 ``not_inheritable_reason``（登记的原因）；带 ``head: true`` 的保留旧的 head 条目，
      其余去掉 head；
    * 独立顶层键 ``manifest["declared_inputs"]`` 汇总：登记数、declared 数、rerun-only 数及每个 rerun-only 的原因
      （最终汇报要求「每项重跑原因与耗时有记录」），不触碰 audit_policy 与 control 四链。

    登记表自相矛盾时抛 ValueError（plan 失败）。``launcher`` 是计划的启动前缀（用来从 argv 里切出命令），``input_scopes`` 是本次
    的收窄提案（核对重复）；两者都由 plan_gates 传入。"""

    tree = Path(tree).resolve()
    registry = load_declared_registry(tree, path=registry_path)
    if registry is None:
        return
    units = manifest.get("units") or []
    resolved = _declared_resolve(registry, units, tree, launcher=list(launcher), audit_policy=manifest.get("audit_policy"),
                                 input_scopes=input_scopes, strict=manifest.get("profile") in DECLARED_FULL_PROFILES)
    rerun = dict(resolved["rerun_only"])
    converted: dict[str, str] = {}
    declared: dict[str, dict[str, Any]] = {}
    make_problems: dict[tuple[str, ...], str] = {}   # 声明目标集合 → 原因（空串＝可以）；同一组目标只扫一次
    for unit_id, info in resolved["declared"].items():
        if info["scope"].get("make_expansion"):
            make_key = tuple(str(item) for item in info["scope"]["make_expansion"])
            if make_key not in make_problems:
                make_problems[make_key] = _declared_make_problem(tree, make_key) or ""
            make_problem = make_problems[make_key]
            if make_problem:
                converted[unit_id] = make_problem
                rerun[unit_id] = {"reason": make_problem, "head": False}
                continue
        declared[unit_id] = info
    for unit in units:
        unit_id = unit.get("unit_id")
        if unit_id in declared:
            unit["inputs"] = {"declared_scope": declared[unit_id]["scope"], "declared_sample": declared[unit_id]["sample"]}
            if "toolchain:go" in (declared[unit_id]["scope"].get("contracts") or []):
                # 命令运行期离线（WP3 定案）：go 命令不联网下载模块、不悄悄切换工具链。变量写进该单元清单的 env（随单元规格进记录），
                # 已有同名变量且值不同就让 plan 失败——不悄悄覆盖，免得单元的原有设置被改掉而没人知道。
                env = dict(unit.get("env") or {})
                for key, value in DECLARED_OFFLINE_ENV.items():
                    if key in env and env[key] != value:
                        raise _declared_fail(unit_id, f"的清单 env 已有 {key}={env[key]!r}，与声明级承接要求的离线值 {value!r} 冲突（不覆盖，请先处理）")
                    env[key] = value
                unit["env"] = env
        elif unit_id in rerun:
            unit["inheritable"] = False
            unit["not_inheritable_reason"] = rerun[unit_id]["reason"]
            if isinstance(unit.get("inputs"), dict) and not rerun[unit_id]["head"]:
                unit["inputs"] = {key: value for key, value in unit["inputs"].items() if key != "head"}
    target = _declared_registry_file(tree, registry_path)
    manifest["declared_inputs"] = {
        "schema_version": DECLARED_SUMMARY_SCHEMA,
        "registry": {"path": DECLARED_REGISTRY_PATH if registry_path is None else str(registry_path),
                     "sha256": _unit_records_module().file_sha256(target)},
        "registered": len(registry["units"]), "declared": len(declared), "rerun_only": len(rerun),
        "not_in_plan": len(resolved["not_in_plan"]),
        "declared_units": sorted(declared), "rerun_only_reasons": {unit_id: rerun[unit_id]["reason"] for unit_id in sorted(rerun)},
        "converted_to_rerun_only": dict(sorted(converted.items())), "not_in_plan_units": sorted(resolved["not_in_plan"]),
    }


# ---- 合同探测（生产侧，需要子进程） ----


def _go_compile_warmup_module() -> Any:
    """同目录的 go_compile_warmup.py（三缓存目录位置检查 cache_paths 的唯一实现）：按路径加载。"""

    name = "go_compile_warmup_sibling"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "go_compile_warmup.py")
        if spec is None or spec.loader is None:
            raise ValueError(f"找不到同目录的 go_compile_warmup.py：{HERE}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        try:
            spec.loader.exec_module(module)
        except BaseException:
            sys.modules.pop(name, None)
            raise
    return sys.modules[name]


def declared_cache_locations(tree: Path, work: Path, environ: Mapping[str, str]) -> dict[str, Path]:
    """GOCACHE／GOMODCACHE／GOTMPDIR 三个缓存根的位置核验，直接复用预热收据用的 ``go_compile_warmup.cache_paths``：
    绝对路径、无符号链接、在工作根内、不在测试树内、互不包含；不合规抛 ValueError。给执行器的启动前置核验用——
    缓存条目本身只写稳定声明，位置是否合规是「运行前凭证」，不进输入摘要。"""

    return _go_compile_warmup_module().cache_paths(Path(tree).resolve(), Path(work).resolve(), environ)


def _declared_scope_of(declaration: Mapping[str, Any]) -> Mapping[str, Any]:
    """从单元的 declared 声明里取 scope（也接受直接给 scope 本身）。"""

    if isinstance(declaration, Mapping) and isinstance(declaration.get("declared_scope"), Mapping):
        return declaration["declared_scope"]
    if isinstance(declaration, Mapping) and declaration.get("schema_version") == DECLARED_SCOPE_SCHEMA:
        return declaration
    raise ValueError("不是声明级输入声明：缺少 declared_scope")


def _declared_contract_entry(kind: str, ident: str, facts: Mapping[str, Any], info: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """合同条目：摘要只覆盖稳定的事实（facts），人读的附加信息（info）只进 detail。facts 里不得有会随运行变化的内容。"""

    body = {"schema_version": DECLARED_CONTRACT_SCHEMA, "type": kind, "id": ident, "facts": dict(facts)}
    detail = {"schema_version": DECLARED_CONTRACT_SCHEMA, "type": kind, "id": ident, **facts, **dict(info or {})}
    return {"category": "environment", "name": f"contract:{kind}:{ident}", "sha256": _declared_sha256_json(body), "detail": detail}


def _declared_image_default(tree: Path, ident: str, environ: Mapping[str, str], work: Path | None) -> tuple[str, dict[str, Any], str]:
    """服务镜像的默认引用，返回（引用, 来源说明, 解析来源）。解析不了抛 ValueError。

    postgres／redis 从测试脚手架的 Go 常量解析；ryuk 从 backend/go.mod 取 testcontainers-go 版本，再读 GOMODCACHE 里该版本
    源码的 ReaperDefaultImage，模块缓存里没有就用 DECLARED_RYUK_DEFAULT_IMAGES。"""

    spec = DECLARED_SERVICE_IMAGES.get(ident)
    if spec is None:
        raise ValueError(f"未知的服务镜像合同：service-image:{ident}")
    if "module" not in spec:
        try:
            text = (Path(tree) / DECLARED_IMAGE_SOURCE_FILE).read_text(encoding="utf-8")
        except OSError as error:
            raise ValueError(f"读不到服务镜像来源文件 {DECLARED_IMAGE_SOURCE_FILE}：{error}") from error
        match = re.search(rf'^\s*{re.escape(spec["const"])}\s*=\s*"([^"\n]+)"', text, re.M)
        if match is None or not _DECLARED_IMAGE_REF.fullmatch(match.group(1)):
            raise ValueError(f"{DECLARED_IMAGE_SOURCE_FILE} 里解析不到常量 {spec['const']} 的镜像引用")
        return match.group(1), {"file": DECLARED_IMAGE_SOURCE_FILE, "const": spec["const"]}, "source"
    try:
        go_mod = (Path(tree) / spec["go_mod"]).read_text(encoding="utf-8")
    except OSError as error:
        raise ValueError(f"读不到 {spec['go_mod']}：{error}") from error
    found = re.search(rf'^\s*(?:require\s+)?{re.escape(spec["module"])}\s+(v[0-9A-Za-z.+-]+)', go_mod, re.M)
    if found is None:
        raise ValueError(f"{spec['go_mod']} 里没有 {spec['module']} 的版本，解析不了 Ryuk 默认镜像")
    version = found.group(1)
    source = {"module": spec["module"], "version": version, "const": spec["const"]}
    modcache = environ.get("GOMODCACHE") or (str(Path(work) / "go-mod-cache") if work is not None else None)
    if modcache:
        path = Path(modcache) / "github.com" / "testcontainers" / f"testcontainers-go@{version}" / DECLARED_RYUK_SOURCE
        try:
            match = re.search(rf'{re.escape(spec["const"])}\s*=\s*"([^"\n]+)"', path.read_text(encoding="utf-8"))
        except OSError:
            match = None
        if match is not None and _DECLARED_IMAGE_REF.fullmatch(match.group(1)):
            return match.group(1), source, "module-cache"
    if version in DECLARED_RYUK_DEFAULT_IMAGES:
        return DECLARED_RYUK_DEFAULT_IMAGES[version], source, "builtin-table"
    raise ValueError(f"testcontainers-go {version} 的 Ryuk 默认镜像解析不到：请把它的 {spec['const']} 登记进 DECLARED_RYUK_DEFAULT_IMAGES")


def _declared_walk(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    """条目事实里的叶子（点分路径, 值），跳过只供人读的字段。"""

    if isinstance(value, Mapping):
        return [leaf for key, item in value.items() if key not in DECLARED_INFO_KEYS for leaf in _declared_walk(item, f"{prefix}{key}.")]
    if isinstance(value, (list, tuple)):
        return [leaf for index, item in enumerate(value) for leaf in _declared_walk(item, f"{prefix}{index}.")]
    return [(prefix.rstrip("."), value)]


class DeclaredProbe:
    """一次合同探测会话（用 ``declared_probe`` 创建）。

    两条规则缺一不可：
    1. **每个会话都重新探测**。执行器在启动、锁后复核、发布前复核各算一次「当前事实」，复核必须看到此刻的真实情况：
       plan 与执行之间 docker pull 换了镜像、工具链被升级，沿用旧值会把已变化的环境错认成相同，把过期记录当成可承接。
    2. **同一会话内同一事实只探测一次**。65 个单元共用同样的工具链、镜像条目，逐单元重探既慢（可执行文件要整个算摘要），
       也会让同一次计算里出现两个互相矛盾的事实。会话内的缓存按条目键记忆，会话结束即丢弃，下一个会话从头探测。

    探测失败不抛异常：值写成 ``unavailable(rc=N)``（沿用 entry_steps 的约定），与成功值不同，所以不会被错误承接；
    ``declared_contract_problems`` 把这类情形列成原因，供执行器把单元标为不可承接。
    """

    def __init__(self, *, runner: Any = None, environ: Mapping[str, str] | None = None, tree: Path | None = None,
                 work: Path | None = None, require_offline: bool | None = None) -> None:
        self.steps = _entry_steps_module()
        self.environ = None if environ is None else dict(environ)
        self.tree = Path(tree if tree is not None else Path.cwd()).resolve()
        self.require_offline = DECLARED_REQUIRE_OFFLINE if require_offline is None else bool(require_offline)
        base = runner if runner is not None else self.steps.default_runner

        def bound(argv: Sequence[str], env: Mapping[str, str] | None, cwd: Path | None) -> tuple[int, str]:
            # entry_steps 的输入解析（例如 image_ref）调 ctx.run 时不带环境；统一补上本会话的环境，
            # 保证所有探测命令（docker、go、make、git）看到的是同一份环境（缺省即进程环境，与执行器一致）。
            return base(argv, self.environ if env is None else env, cwd)

        self.ctx = self.steps.Context(params={}, driver_dir=HERE, steps_dir=Path(tempfile.gettempdir()), runner=bound)
        cache = self._env().get("GOCACHE")
        self.work = Path(work).resolve() if work is not None else (Path(cache).parent if cache else None)
        self._memo: dict[tuple[str, ...], dict[str, Any]] = {}

    def __call__(self, declaration: Mapping[str, Any]) -> list[dict[str, Any]]:
        return self.entries(declaration)

    def _env(self) -> Mapping[str, str]:
        return self.environ if self.environ is not None else os.environ

    def _first_line(self, argv: Sequence[str], cwd: Path) -> str:
        rc, out = self.ctx.run(list(argv), cwd=cwd, env=self.environ)
        lines = [line.strip() for line in str(out).splitlines() if line.strip()]
        return lines[0][:300] if rc == 0 and lines else f"unavailable(rc={rc})"

    def _which(self, name: str) -> Path | None:
        path_value = self._env().get("PATH")
        found = shutil.which(name, path=os.defpath if path_value is None else path_value)
        return Path(os.path.realpath(found)) if found else None

    def _memoized(self, key: tuple[str, ...], build: Any) -> dict[str, Any]:
        if key not in self._memo:
            self._memo[key] = build()
        return copy.deepcopy(self._memo[key])

    def _module_dir(self, repo: Mapping[str, Any]) -> Path:
        """go 命令的工作目录＝go.mod 所在目录：go 的工具链选择（GOTOOLCHAIN=auto 时按 go.mod 的 go 行切换）取决于它，
        在别处探测会看到与单元实际使用的不同的工具链。"""

        for item in repo.get("files") or []:
            path = item.get("path") if isinstance(item, Mapping) else item
            if isinstance(path, str) and (path == "go.mod" or path.endswith("/go.mod")):
                return (self.tree / path).parent
        return self.tree / "backend" if (self.tree / "backend" / "go.mod").is_file() else self.tree

    def entries(self, declaration: Mapping[str, Any]) -> list[dict[str, Any]]:
        """给定单元的 declared 声明，返回它的全部 ``contract:*`` 条目（按名字排序）。"""

        scope = _declared_scope_of(declaration)
        repo = scope.get("repo") if isinstance(scope.get("repo"), Mapping) else {}
        module_dir = self._module_dir(repo)
        found: dict[str, dict[str, Any]] = {}
        for ident in scope.get("contracts") or []:
            entry = self._contract(str(ident), module_dir, scope)
            found[entry["name"]] = entry
        history = repo.get("git_history") if isinstance(repo.get("git_history"), Mapping) else {}
        for commit in history.get("commits") or []:
            entry = self._memoized(("git-history", str(commit)), lambda commit=str(commit): self._git_history(commit))
            found[entry["name"]] = entry
        for target in scope.get("make_expansion") or []:
            entry = self._memoized(("make-expansion", str(target)), lambda target=str(target): self._make_expansion(target))
            found[entry["name"]] = entry
        return [found[name] for name in sorted(found)]

    def _contract(self, ident: str, module_dir: Path, scope: Mapping[str, Any]) -> dict[str, Any]:
        kind, _, name = ident.partition(":")
        if kind == "toolchain":
            return self._memoized(("toolchain", name, str(module_dir) if name == "go" else ""), lambda: self._toolchain(name, module_dir))
        if kind == "cache":
            return self._memoized(("cache", name), lambda: self._cache(name))
        if kind == "service-image":
            return self._memoized(("service-image", name), lambda: self._service_image(name))
        if kind == "host-config":
            if not _DECLARED_HOST_NAME.fullmatch(name):
                raise ValueError(f"未知的声明合同：{ident}")
            paths = _declared_host_config_paths(scope, name)
            if not paths:
                raise ValueError(f"未知的 HOME 配置合同（scope.outside 里没有引用它的 {{HOME}}/ 行，也不是内置的常见配置）：{ident}")
            return self._memoized(("host-config", name, *paths), lambda: self._host_config(name, paths))
        raise ValueError(f"未知的声明合同：{ident}")

    def _toolchain(self, name: str, module_dir: Path) -> dict[str, Any]:
        """版本行＋白名单 PATH 解析到的可执行文件摘要；go 另加 GOROOT（VERSION、go、compile／link／asm／vet 的摘要）与关键环境的生效值。
        go 在 go.mod 所在目录探测；其余在测试树根探测（单元里 pnpm 也是从树根以 --dir frontend 调用，corepack 取版本看当前目录）。"""

        if name not in DECLARED_TOOLCHAINS:
            raise ValueError(f"未知的工具链合同：toolchain:{name}")
        cwd = module_dir if name == "go" else self.tree
        found = self._which(name)
        facts: dict[str, Any] = {"version_line": self._first_line(DECLARED_TOOLCHAINS[name], cwd),
                                 "executable_sha256": "missing" if found is None else _declared_file_sha256(found)}
        if name == "go":
            facts.update(self._go_facts(cwd))
        facts["value"] = facts["version_line"]
        return _declared_contract_entry("toolchain", name, facts, {} if found is None else {"executable": str(found)})

    def _go_facts(self, cwd: Path) -> dict[str, Any]:
        keys = ("GOROOT", "GOTOOLDIR", *DECLARED_GO_ENV_KEYS)
        rc, out = self.ctx.run(["go", "env", "-json", *keys], cwd=cwd, env=self.environ)
        try:
            payload = json.loads(out)
            if rc != 0 or not isinstance(payload, dict) or not payload.get("GOROOT") or not payload.get("GOTOOLDIR"):
                raise ValueError("go env 输出不可用")
            values = {key: str(payload.get(key, "")) for key in keys}
        except ValueError:
            marker = f"unavailable(rc={rc})"
            facts: dict[str, Any] = {"goroot": marker, "env": marker}
        else:
            root, tool_dir = Path(values["GOROOT"]), Path(values["GOTOOLDIR"])
            facts = {"goroot": {"path": values["GOROOT"], "version_sha256": _declared_file_sha256(root / "VERSION"),
                                "go_sha256": _declared_file_sha256(root / "bin" / "go"),
                                "tools": {tool: _declared_file_sha256(tool_dir / tool) for tool in DECLARED_GO_TOOLS}},
                     "env": {key: values[key] for key in DECLARED_GO_ENV_KEYS}}
        if self.require_offline:
            env = facts["env"]
            unmet = ({key: {"expected": expected, "actual": env.get(key)} for key, expected in DECLARED_OFFLINE_ENV.items()
                      if env.get(key) != expected} if isinstance(env, dict) else {"*": {"expected": "go env 可用", "actual": env}})
            facts["requires"] = {"env_equals": dict(DECLARED_OFFLINE_ENV)}
            facts["requirements"] = {"met": not unmet, "unmet": unmet}
        return facts

    def _cache(self, name: str) -> dict[str, Any]:
        """缓存合同：只有稳定声明（角色、位置类别、规则），不含内容也不含绝对路径。"""

        definition = DECLARED_CACHES.get(name)
        if definition is None:
            raise ValueError(f"未知的缓存合同：cache:{name}")
        return _declared_contract_entry("cache", name, {**copy.deepcopy(definition), "value": definition["rule"]})

    def _hub_prefix_configured(self) -> bool:
        if str(self._env().get(DECLARED_HUB_PREFIX_ENV, "")).strip():
            return True
        home = self._env().get("HOME")
        if not home:
            return False
        try:
            text = (Path(home) / DECLARED_TESTCONTAINERS_PROPERTIES).read_text(encoding="utf-8")
        except (OSError, ValueError):
            return False
        # properties 格式：键与值之间是 = 或 : 或空白；只在同一行内找值（空值不算配置）。
        return re.search(rf'^[ \t]*{re.escape(DECLARED_HUB_PREFIX_PROPERTY)}(?:[ \t]*[=:][ \t]*|[ \t]+)\S', text, re.M) is not None

    def _service_image(self, name: str) -> dict[str, Any]:
        """服务镜像：实际引用（默认引用，postgres 可被环境变量覆盖）＋ ``docker image inspect`` 的镜像 Id（复用 entry_steps 的
        image_ref 输入）。引用解析不了、或配置了 Hub 前缀时失败关闭：镜像 Id 记 unavailable。"""

        spec = DECLARED_SERVICE_IMAGES.get(name)
        if spec is None:
            raise ValueError(f"未知的服务镜像合同：service-image:{name}")
        override_env = spec.get("override_env")
        override = str(self._env().get(override_env, "")).strip() if override_env else ""
        try:
            default, source, resolved_from = _declared_image_default(self.tree, name, self._env(), self.work)
        except ValueError as error:
            facts = {"reference": "unresolved", "default_reference": "unresolved", "source": None, "override_env": override_env,
                     "override_value": override or None, "hub_prefix_configured": self._hub_prefix_configured(),
                     "image_id": "unavailable(unresolved-reference)", "value": "unavailable(unresolved-reference)"}
            return _declared_contract_entry("service-image", name, facts, {"error": str(error)})
        reference = override if override and default.startswith(spec.get("override_prefix", "\0")) else default
        hub_prefix = self._hub_prefix_configured()
        if hub_prefix:
            image_id = "unavailable(hub-prefix-configured)"
        else:
            probed = self.steps.resolve(self.steps.Input("environment", "image_ref", reference), self.ctx)
            image_id = str(probed.get("detail", {}).get("value", "unavailable(missing)"))
        facts = {"reference": reference, "default_reference": default, "source": source, "override_env": override_env,
                 "override_value": override or None, "hub_prefix_configured": hub_prefix, "image_id": image_id, "value": image_id}
        return _declared_contract_entry("service-image", name, facts, {"resolved_from": resolved_from})

    def _git_history(self, commit: str) -> dict[str, Any]:
        """git 历史合同：固定提交的树对象 ID。内容寻址，新提交不改旧对象，所以 HEAD 移动不使条目变化；提交缺失（浅克隆）时
        记 unavailable。"""

        if not _DECLARED_COMMIT.fullmatch(commit):
            raise ValueError(f"git 历史合同的提交号必须是完整的 40 位小写十六进制：{commit!r}")
        tree_id = self._first_line(["git", "rev-parse", "--verify", f"{commit}^{{tree}}"], self.tree)
        return _declared_contract_entry("git-history", commit, {"commit": commit, "tree": tree_id, "value": tree_id})

    def _make_expansion(self, target: str) -> dict[str, Any]:
        """make 展开合同：``make -s --no-print-directory -n <目标>`` 的展开文本摘要，替代把整个 Makefile 当输入——只有这个目标
        实际会执行的命令变了才失效。测试树的绝对路径（$(CURDIR) 及由它派生的变量）规范化为 {TREE}，换工作根不失效。
        plan 阶段的 Makefile 保守扫描（见 _declared_make_problem）把已知会在 make -n 下执行命令的写法挡在外面；没命中扫描却仍有副作用的写法不在保证内。
        探测环境清空 MAKEFLAGS 与 MAKEFILES：调用方环境里的 -j、-t 之类选项或额外的 makefile 不该改变展开语义（单元真正执行时也是白名单环境）。"""

        if not _DECLARED_MAKE_TARGET.fullmatch(target):
            raise ValueError(f"make 展开目标名非法：{target!r}")
        rc, out = self.ctx.run(["make", "-s", "--no-print-directory", "-n", target], cwd=self.tree,
                               env={**self._env(), "MAKEFLAGS": "", "MAKEFILES": ""})
        if rc != 0:
            return _declared_contract_entry("make-expansion", target, {"target": target, "expansion_sha256": f"unavailable(rc={rc})",
                                                                      "lines": None, "value": f"unavailable(rc={rc})"})
        text = str(out)
        for absolute in sorted({str(self.tree), os.path.realpath(self.tree)}, key=len, reverse=True):
            # 只替换整段路径前缀（后面不能紧跟路径字符），免得把共享前缀的别的目录（如 test-tree-hist）截坏。
            text = re.sub(re.escape(absolute) + r"(?![\w.+-])", lambda _match: "{TREE}", text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        facts = {"target": target, "expansion_sha256": digest, "lines": len([line for line in text.splitlines() if line.strip()]),
                 "value": digest}
        return _declared_contract_entry("make-expansion", target, facts, {"text": text[:2000]})

    def _host_config(self, name: str, paths: Sequence[str]) -> dict[str, Any]:
        """HOME 下配置文件：每个路径存在则内容摘要，不存在记 missing（缺失也是一种状态）；位置只写 {HOME} 别名，换用户目录
        不改摘要的含义。HOME 不可用时失败关闭（unavailable）。"""

        home = self._env().get("HOME")
        usable = bool(home) and os.path.isabs(str(home))
        files = [{"alias": "{HOME}/" + relative, "state": _declared_file_sha256(Path(str(home)) / relative) if usable else "unavailable(no-home)"}
                 for relative in paths]
        value = files[0]["state"] if len(files) == 1 else _declared_sha256_json(files)
        return _declared_contract_entry("host-config", name, {"files": files, "value": value})


def declared_probe(*, runner: Any = None, environ: Mapping[str, str] | None = None, tree: Path | None = None,
                   work: Path | None = None, require_offline: bool | None = None) -> DeclaredProbe:
    """创建一个合同探测会话（探测回调工厂），给执行器经 ``ContextVar _DECLARED_PROBE`` 取用。

    用法（unit_executor）：``_run_gates_current`` 加载 entry 模块后 ``_DECLARED_PROBE.set(entry.declared_probe)``（设的是工厂）；
    ``_gate_currents`` 每次被调用时，若有命令单元的 inputs 含 ``declared_scope``，先 ``session = _DECLARED_PROBE.get()()``
    （参数全部可省：``tree`` 缺省取当前目录＝测试树，``environ`` 缺省取进程环境，``work`` 缺省取 GOCACHE 的父目录），
    再对每个这样的单元 ``entries = session(item["inputs"])``，把条目并进它的输入明细。每次 ``_gate_currents`` 新建会话＝
    重新探测，会话内多个单元共享探测结果（见 DeclaredProbe）。``runner`` 是 ``entry_steps.Context`` 的注入点
    （签名 ``(argv, env, cwd) -> (rc, stdout)``），测试用假 runner 模拟 docker／go／node／make。"""

    return DeclaredProbe(runner=runner, environ=environ, tree=tree, work=work, require_offline=require_offline)


def declared_contract_entries(declaration: Mapping[str, Any], *, runner: Any = None, environ: Mapping[str, str] | None = None,
                              tree: Path | None = None, work: Path | None = None, require_offline: bool | None = None) -> list[dict[str, Any]]:
    """一次性探测：给定单元的 declared 声明，现取合同事实，返回 ``contract:*`` 条目列表（每项
    ``{category: "environment", name: "contract:<类型>:<ID>", sha256, detail: {schema_version: "declared-contract/v1", type, id, …}}``）。
    等价于新建一个会话只探测这一个声明；要在多个单元间共享探测结果，用 ``declared_probe``。"""

    return declared_probe(runner=runner, environ=environ, tree=tree, work=work, require_offline=require_offline).entries(declaration)


# go test 的 -count 取值（-count=N、--count=N、-count N 三种写法）：缓存合同要求 -count=1，任何别的取值都与它矛盾（Go 以最后一次出现为准）。
_GO_COUNT_FLAG = re.compile(r"--?count(?:=(.*))?")
# 按包运行的包装器自己固定带 -count=1，门禁命令里就不必再写。
WRAPPER_GUARANTEED_FLAGS = ("-count=1",)


def go_count_values(words: Sequence[str]) -> list[str]:
    """命令词里出现的全部 -count 取值（按出现顺序）。"""

    values: list[str] = []
    for index, word in enumerate(words):
        match = _GO_COUNT_FLAG.fullmatch(str(word))
        if match is not None:
            values.append(match.group(1) if match.group(1) is not None else (str(words[index + 1]) if index + 1 < len(words) else ""))
    return values


def declared_contract_problems(entries: Sequence[Mapping[str, Any]], *, argv: Sequence[str] | None = None) -> list[str]:
    """合同条目里「不能承接」的情形（空列表＝没有问题），给执行器的前置核验用：

    * 任何事实值是 ``unavailable(…)``（探测失败：docker 不在、go 不在、提交缺失、镜像引用解析不了、配置了 Hub 前缀等）；
    * 可执行文件缺失；
    * 离线要求打开时 ``requirements.met`` 不是 True（门禁环境没有真的注入 GOFLAGS／GOPROXY／GOTOOLCHAIN）；
    * 命令是按包运行的包装器（``go_test_packages.py``，优化第 3 项）时同样核缓存合同：包装器内部强制 ``-count=1``，视为满足；包装器参数里再出现
      取值不是 1 的 ``-count`` 与此矛盾，判问题。裸 ``go test`` 命令里出现取值不是 1 的 ``-count`` 也按同一条判问题，两种命令形式结论一致；
    * 给了 ``argv`` 时，命令是 ``go test`` 而缺缓存合同要求的参数（如 ``-count=1``，防测试结果缓存成为隐藏输入）。``argv`` 可以带
      启动前缀也可以不带：在其中找相邻的 ``go``、``test`` 两个词，找到才算 go test 命令。"""

    problems: list[str] = []
    words = list(argv) if argv is not None else []
    go_test = next((index for index in range(len(words) - 1) if words[index:index + 2] == ["go", "test"]), None)
    wrapper = next((index for index, word in enumerate(words) if go_test is None and os.path.basename(str(word)) == GO_PACKAGE_WRAPPER), None)
    for entry in entries:
        name, detail = str(entry.get("name")), entry.get("detail") or {}
        for path, value in _declared_walk(detail):
            if path != "value" and isinstance(value, str) and value.startswith("unavailable("):
                problems.append(f"{name}：{path} 探测失败（{value}）")
        if detail.get("executable_sha256") == "missing":
            problems.append(f"{name}：找不到可执行文件")
        requirements = detail.get("requirements")
        if isinstance(requirements, Mapping) and requirements.get("met") is not True:
            problems.append(f"{name}：命令运行期离线要求未满足（{requirements.get('unmet')}）")
        requires = detail.get("requires")
        flags = requires.get("go_test_argv_contains") if isinstance(requires, Mapping) else None
        if flags and go_test is not None:
            lacking = [flag for flag in flags if flag not in words[go_test:]]
            if lacking:
                problems.append(f"{name}：go test 命令缺少参数 {lacking}")
        elif flags and wrapper is not None:
            lacking = [flag for flag in flags if flag not in WRAPPER_GUARANTEED_FLAGS and flag not in words[wrapper + 1:]]
            if lacking:
                problems.append(f"{name}：go test 包装器命令缺少参数 {lacking}")
        start = go_test if go_test is not None else (wrapper + 1 if wrapper is not None else None)
        if flags and "-count=1" in flags and start is not None:
            clash = [value for value in go_count_values(words[start:]) if value != "1"]
            if clash:
                problems.append(f"{name}：命令里的 -count 取值 {clash} 与缓存合同要求的 -count=1 矛盾")
    return problems


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
# B5a-06：按客户端算（Claude 由 v3 引擎按 Claude 策略算两根身份）——客户端名作为位置参数传给 current_identity；Codex 不带参数，
# 调用与改造前相同。
_TREE_IDENTITY_SNIPPET = (
    "import json, sys\n"
    "from tools.official_client_capture import codex_upgrade_policy_certification as certification\n"
    "identity = certification.current_identity(*sys.argv[1:])\n"
    "print(json.dumps({name: identity[name] for name in certification.IDENTITY_FIELDS}))\n"
)
TREE_IDENTITY_TIMEOUT_SECONDS = 900


def tree_tool_identity(tree: str, client: str = "codex") -> dict[str, str]:
    """测试树受管工具的五摘要（v2 P0 证据绑定，E3-03）：在测试树里起子进程算，不写字节码。签发时与发布认证登记的
    身份比对——入口门禁跑之前已核对数据根部署的受管树就是测试树这一份，两边应当相等。"""

    completed = subprocess.run([sys.executable, "-B", "-c", _TREE_IDENTITY_SNIPPET, *_client_table().spec(client)["identity_args"]],
                               cwd=tree, capture_output=True, text=True,
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


def _p0_v2_context(summary: dict[str, Any], tree: str, tool_identity: Any, client: str = "codex") -> tuple[dict[str, Any] | None, str]:
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
        identity = tool_identity(tree) if tool_identity else tree_tool_identity(tree, client)
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
    # 本轮客户端（参数 CLIENT，B5a-06）：pre-A3 门禁项的输入清单与 v2 P0 证据的测试树五摘要按它取。
    try:
        client = inputs_ctx.client
    except steps.EntryStepsError as error:
        raise ValueError(str(error)) from error

    def gate_record(gate_id: str, exit_code: int, started: str | None, completed: str | None, extra: dict[str, Any]) -> dict[str, Any]:
        argv, workdir = gate_literal_command(
            gate_id, next((gate["units"] for gate in manifest["gates"] if gate["gate_id"] == gate_id), ()))
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
            # 优化第 2 项：打开补跑的运行才有这个键；门禁项通过但其中这几个单元是补跑才通过的，单列、不静默放行。
            **({"retry_passed_units": list(row["retry_passed_units"])} if "retry_passed_units" in row else {}),
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
    # 承接单元来自哪种模式（只影响说明文字）：全集通过（E3-01）或逐单元承接（B5a-08，签发时另按决策收据重验形成期证明）。
    inherit_label = "逐单元承接模式" if mode == "unit-inherit" else "全集通过模式"
    v2: dict[str, Any] | None = None
    if inherited_any:
        v2, why = _p0_v2_context(summary, tree, tool_identity, client)
        if v2 is None:
            for gate_id in p0_gates:
                withheld[gate_id] = (f"本次运行承接了 {len(records[gate_id]['inherited_units'])} 个单元（{inherit_label}），要写以运行清单为证据的"
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
                            executed_as=f"入口门禁一次运行（{inherit_label}）：测试组全集由本次执行的单元与承接以往运行正式执行记录的单元"
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
                            executed_as=f"入口门禁一次运行（{inherit_label}）：EGRESS_SPEC_CHECKS 子检查各一个单元，本次执行的与承接以往运行"
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
    if isinstance(summary.get("retry"), dict):
        # 优化第 2 项：补跑（打开时才有）。总摘要只列规则与单元清单；两次尝试的记录与日志摘要在执行器汇总的 retry.units 里。
        retry = summary["retry"]
        entry["retry"] = {key: retry.get(key) for key in ("schema_version", "enabled", "window_rounds", "pass_limit", "retry_passed",
                                                         "retry_failed", "retry_denied", "not_retriable")}
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
    plan.add_argument("--with-gates", default="", help="人工补的门禁项（逗号分隔；定向回归选不出时用，E4-01）")
    plan.add_argument("--launcher-json", default="[]", help="测试树单元的启动前缀（JSON 字符串列表）")
    plan.add_argument("--typescript-module", default=None)
    plan.add_argument("--pre-a3-units", type=Path, default=None, help="pre-A3 场景清单（认证模块 plan 生成）")
    plan.add_argument("--pre-a3-env", action="append", default=[], help="pre-A3 场景的环境变量 KEY=VALUE，可重复")
    plan.add_argument("--pre-a3-data-root", type=Path, default=None,
                      help="数据根：在这里算 pre-A3 场景单元的数据根输入（冻结台账、录制数据、alpine 镜像，E3-02）")
    plan.add_argument("--historical-source-root", default=None, help="历史源码树（check-egress-spec 子检查另列的输入，E3-01）")
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--input-scope", type=Path, action="append", default=[], help="本轮命令或测试单元的输入收窄提案，可重复指定；必须重新执行严格审计")
    plan.add_argument("--audit-policy", type=Path, help="明确登记可验收输入合同；未登记单元一律重跑，不宣称覆盖完整")
    plan.add_argument("--recorded-full79-spec", type=Path, help="Claude已批准完整79单元的外持JSON")
    plan.add_argument("--recorded-full79-spec-sha256", help="上述原字节的外持SHA；不能自行刷新")
    plan.add_argument("--declared-registry", type=Path, default=None,
                      help="命令单元声明级承接登记表（缺省取测试树的 tools/ci/command_input_scopes.json，缺失即不启用；测试用）")
    recorded = sub.add_parser("recorded-full79-inputs", help="只读核固定79接线及真实夹具、预算来源")
    recorded.add_argument("--spec", type=Path, required=True)
    recorded.add_argument("--sha256", required=True)
    recorded.add_argument("--pre-a3-env", action="append", default=[])
    recorded.add_argument("--certification", type=Path)
    recorded.add_argument("--tree", type=Path)
    recorded.add_argument("--data-root", type=Path)
    workspace = sub.add_parser("verify-scope-workspace", help="只读核对收窄复验沿用的测试树、缓存和 bundle")
    workspace.add_argument("--tree", type=Path, required=True)
    workspace.add_argument("--bytecode-cache", type=Path, required=True)
    workspace.add_argument("--commit", required=True)
    workspace.add_argument("--bundle", type=Path, required=True)
    workspace.add_argument("--branch", required=True)
    successor = sub.add_parser("prepare-scope-successor", help="持有入口锁时仅原位准备普通文件内容后继")
    successor.add_argument("--tree", type=Path, required=True)
    successor.add_argument("--bytecode-cache", type=Path, required=True)
    successor.add_argument("--commit", required=True)
    successor.add_argument("--bundle", type=Path, required=True)
    successor.add_argument("--branch", required=True)
    successor.add_argument("--lock-directory", type=Path, required=True)
    successor.add_argument("--lock-owner", type=int, required=True)
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
        if args.command == "recorded-full79-inputs":
            env = _pairs(args.pre_a3_env, "--pre-a3-env")
            bridge = recorded_full79_bridge(args.spec, args.sha256, client="claude", fixture_env=env)
            if args.certification is not None:
                if args.tree is None or args.data_root is None:
                    raise ValueError("复用输入复核必须有当前测试树与数据根")
                verify_recorded_reuse(args.tree, args.certification, bridge,
                                      pre_a3_data_root_inputs(args.data_root, "claude", pre_a3_env=env))
            print(json.dumps({"inputs": [bridge["spec_input"], *bridge["fixtures"], *bridge["resolved"]]}, ensure_ascii=False))
            return 0
        if args.command == "make-checks":
            return make_checks(args.name, list(args.targets))
        if args.command == "verify-scope-workspace":
            print(json.dumps(verify_scope_workspace(args.tree, args.bytecode_cache, commit=args.commit,
                                                    bundle=args.bundle, branch=args.branch), ensure_ascii=False))
            return 0
        if args.command == "prepare-scope-successor":
            print(json.dumps(prepare_scope_successor(args.tree, args.bytecode_cache, commit=args.commit,
                bundle=args.bundle, branch=args.branch, lock_directory=args.lock_directory,
                lock_owner=args.lock_owner), ensure_ascii=False))
            return 0
        if args.command == "plan":
            launcher = json.loads(args.launcher_json)
            if not isinstance(launcher, list) or not all(isinstance(part, str) and part for part in launcher):
                raise ValueError("--launcher-json 必须是非空字符串组成的列表")
            client = pre_a3_units_client(args.pre_a3_units) if args.pre_a3_units is not None else "codex"
            pre_a3_env = _pairs(args.pre_a3_env, "--pre-a3-env") or None
            if (args.recorded_full79_spec is None) != (args.recorded_full79_spec_sha256 is None):
                raise ValueError("完整79外持文件与SHA必须同时提供")
            bridge = (recorded_full79_bridge(args.recorded_full79_spec, args.recorded_full79_spec_sha256,
                                            client=client, fixture_env=pre_a3_env or {})
                      if args.recorded_full79_spec is not None else None)
            data_inputs = (pre_a3_data_root_inputs(args.pre_a3_data_root, client, pre_a3_env=pre_a3_env)
                           if args.pre_a3_data_root is not None else None)
            with_gates = [gate for gate in args.with_gates.split(",") if gate]
            manifest = plan_gates(args.tree, profile=args.profile, launcher=launcher, typescript_module=args.typescript_module,
                                  input_scopes=[json.loads(path.read_text()) for path in args.input_scope],
                                  audit_policy=json.loads(args.audit_policy.read_text()) if args.audit_policy else None,
                                  recorded_bridge=bridge,
                                  declared_registry=args.declared_registry,
                                  with_gates=with_gates,
                                  pre_a3_units=args.pre_a3_units, pre_a3_env=pre_a3_env,
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
