"""Claude 升级 B5a-05：最小恢复演练四条链（pre-A3 场景，零请求、隔离副本树）。

四条链与 pre-A3 登记（``codex_upgrade_pre_a3_certification.CLAUDE_SCENARIOS``）一一对应：

* ``vc-chain.claude-reconcile``：首批采集失败 → reconcile-attempt → 批准恢复预览 → resume 只重跑失败 Job；无预约时
  reconcile-supervisor-run → N+1 重派；
* ``vc-chain.claude-deadline``：阶段预算真实到期，运行中命令与看门狗按阶段截止停 → 两本账暂停 → deadline-extend
  预览／批准、两本账之间强杀后补齐收敛 → 原 Campaign 续跑；
* ``vc-chain.claude-root-cause``：同根因累计达冻结上限只暂停、门禁只挡该根因 → 修复收据放行 → 续跑；
* ``vc-chain.claude-tool-evolution``：部署新工具 → 未登记即拒绝（零写入）→ 登记 → 有效身份更新 → Store finalizer
  通过 → 旧收据按形成期重放仍通过。

两层结构：

* **外层**（四个 ``test_*`` 方法，pre-A3 的场景入口）：把两个受管根（``official_client_capture`` 按本模块的静态依赖闭包，
  ``official_client_control`` 整根）复制成隔离副本树，放在宿主数据根之外的临时根里（Linux 固定 ``/var/tmp``，路径里带
  ``staging`` 一段，供仅夹具总账使用），再起子进程在副本树里跑链体；核对子进程结果、每条不超过 15 分钟，并把耗时与
  隔离方式写进 ``real_chain_metrics``（pre-A3 场景记录照收）。
* **链体**（四个 ``_exercise_*`` 方法）：只在副本树子进程里运行（``PYTHONPATH`` 只含副本根，模块不得回落到原仓库），
  在子进程内装替身（中继、docker、宿主命令边界由批次 2 的取证夹具提供），全部 CLI 经真实入口执行。

隔离规则：

* Linux 上以 root 运行（ARM64 采集主机、pre-A3 正式签发）时**必须**进 ``unshare -m -n`` 命名空间：新网络命名空间只有
  未启用的回环接口；宿主数据根改成只读挂载；Codex 的生产执行副本用只读 tmpfs 遮住。找不到 ``unshare`` 或命名空间建立
  失败即判失败，不降级。链体第一步在命名空间内自检（只有 ``lo``、数据根写入即失败、执行副本已遮住）。
* 其余环境（本机 macOS、非 root 的 Linux）降级为「副本树加替身」：同一套链体照常跑完全部逻辑断言，指标里记
  ``isolation=degraded``。pre-A3 只在 ARM64 上签发，降级结果进不了认证。

依赖 B2-08 的步骤（对账、resume、入口总账准入、编排器侧工具演进登记）在链体里是明确的失败占位：先跑完不依赖 B2-08 的
全部步骤与断言，到占位处以「等 B2-08：<缺的步骤>」失败——不跳过、不静默通过。所以 B2-08 落地之前四条链都不会通过，
Claude 的 pre-A3 v3 也签不出来，这是有意的；B2-08 落地后把占位替换成真实步骤。

零请求：命名空间模式由构造保证（没有可用的网络接口）；两种模式下取证都走替身中继与替身 docker，截止链只跑本地命令，
Store 子链不涉及网络。本文件位于 tests/，不进任何受管身份。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture.tests import managed_tree_copy as trees
from tools.official_client_capture.tests import project_ledger_fixture

REPO_ROOT = Path(__file__).resolve().parents[4]
# 链体在子进程里按点分名运行；不用 __name__，直接以脚本运行本文件时它是 __main__。
CHAIN_MODULE = "tools.official_client_capture.tests.real_chains." + Path(__file__).stem

# 场景 ID → 链体方法名（与 pre-A3 登记、外层 test_* 方法一一对应）。
CHAINS: Mapping[str, Mapping[str, str]] = {
    "reconcile": {"scenario": "vc-chain.claude-reconcile", "exercise": "_exercise_reconcile"},
    "deadline": {"scenario": "vc-chain.claude-deadline", "exercise": "_exercise_deadline"},
    "root-cause": {"scenario": "vc-chain.claude-root-cause", "exercise": "_exercise_root_cause"},
    "tool-evolution": {"scenario": "vc-chain.claude-tool-evolution", "exercise": "_exercise_tool_evolution"},
}

# 设计 B5a-05 的验收：ARM64 上每条不超过 15 分钟（含复制副本树）。子进程超时比它略短，留出外层收尾的时间。
CHAIN_BUDGET_SECONDS = 900
CHAIN_TIMEOUT_SECONDS = 870

# 外层交给链体的环境变量：隔离方式、副本根与原仓库根（链体据此拒绝在原仓库上改文件）。
ISOLATION_ENV = "CLAUDE_CONTROL_CHAIN_ISOLATION"
TREE_ENV = "CLAUDE_CONTROL_CHAIN_TREE"
SOURCE_REPO_ENV = "CLAUDE_CONTROL_CHAIN_SOURCE_REPO"
ISOLATION_NAMESPACE = "namespace"
ISOLATION_DEGRADED = "degraded"
IDENTITY_MEMO_ENV = "CODEX_UPGRADE_IDENTITY_MEMO"

# 链体在标准输出写一行指标（最后一行有效）；外层解析后并入 real_chain_metrics。
METRICS_PREFIX = "CLAUDE_CONTROL_CHAIN_METRICS "
# 依赖 B2-08 的步骤：失败信息一律以这几个字开头，外层据此把「占位失败」与其它失败区分开。
PENDING_PREFIX = "等 B2-08"

# 采集主机的宿主数据根（pre-A3 场景就在它的生产布局里运行）与 Codex 的生产执行副本。命名空间里前者只读、后者遮住。
HOST_DATA_ROOT = Path("/root/docker/capture-cli/data")
CODEX_CAPTURE_ROOT = Path("/root/oauth-capture")
NAMESPACE_SCRIPT = (
    "set -eu\n"
    f'data="{HOST_DATA_ROOT}"\n'
    f'capture="{CODEX_CAPTURE_ROOT}"\n'
    'if [ -d "$data" ]; then mount --bind "$data" "$data"; mount -o remount,bind,ro "$data"; fi\n'
    'if [ -d "$capture" ]; then mount -t tmpfs -o ro,mode=0555,size=64k claude-control-chain "$capture"; fi\n'
    'exec "$@"\n'
)

# 副本树里额外放的两份 Claude 文档（Codex 指南与 Framework 由副本树辅助模块自己复制）；文档不进受管身份。
CLAUDE_DOCUMENTS = ("CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md", "OFFICIAL_CLIENT_UPGRADE_CONTROL_PLANE.md")

# 工具演进链在副本树里追加注释的受管文件：Claude 策略的 control 层、不在 wire／evidence 闭包内，只改 control 摘要与整树摘要。
CONTROL_MUTATION_FILE = "claude_upgrade_store.py"

# 截止链的 Claude 最小 Campaign：目标版本取当前 Claude 目标，ID 落在 Claude Campaign ID 的形态里。
TARGET_VERSION = "2.1.285"
BASELINE_VERSION = "2.1.226"

# 工具演进链 Store 子链的三个合成 Campaign（两份前序、一份目标），沿用 Store 夹具的合成坐标。
PREDECESSOR_IDS = ("synthetic-1.0.0-claude-chain-p1", "synthetic-1.0.0-claude-chain-p2")
STORE_TARGET_ID = "synthetic-1.0.0-claude-chain-target"

# 截止链两本账之间强杀：项目总账写完 deadline_extended 返回时 SIGKILL 自己（计时账本那一笔还没写）。
SIGKILL_BETWEEN_LEDGERS = (
    "import os, signal, sys\n"
    "from tools.official_client_capture import claude_upgrade\n"
    "def crash(frame, event, arg):\n"
    "    if event == 'return' and frame.f_code.co_name == 'append_project_event' \\\n"
    "            and frame.f_locals.get('event_type') == 'deadline_extended':\n"
    "        os.kill(os.getpid(), signal.SIGKILL)\n"
    "    return crash\n"
    "sys.settrace(crash)\n"
    "raise SystemExit(claude_upgrade.main(sys.argv[1:]))\n"
)


# ---------------------------------------------------------------------------
# 外层：隔离副本树、命名空间与子进程
# ---------------------------------------------------------------------------


def _scratch_root() -> Path:
    """临时根：必须在宿主数据根与执行副本之外。Linux 固定 ``/var/tmp``（pre-A3 的场景临时目录在数据根 staging 里，命名空间内
    只读）；其它平台用系统临时目录。"""

    return Path("/var/tmp") if sys.platform.startswith("linux") else Path(tempfile.gettempdir()).resolve()


def _copy_two_root_tree(destination: Path) -> Path:
    """两个受管根的隔离副本：``official_client_capture`` 按本模块的静态依赖闭包复制（与单元输入声明同一算法），
    ``official_client_control`` 整根复制（Claude 身份覆盖它，v3 引擎缺这个根直接拒绝；Store 夹具也在它的 tests 下）。"""

    tree = trees.copy_managed_tree(destination, include_tests="closure", closure_of=Path(__file__))
    shutil.copytree(
        REPO_ROOT / "tools" / "official_client_control",
        tree / "tools" / "official_client_control",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for name in CLAUDE_DOCUMENTS:
        source = REPO_ROOT / "docs" / name
        if source.is_file():
            shutil.copy2(source, tree / "docs" / name)
    return tree


def _namespace_argv(argv: list[str]) -> list[str]:
    """在私有挂载＋网络命名空间里执行 ``argv``：宿主数据根只读、Codex 执行副本遮住、没有可用的网络接口。"""

    return ["unshare", "-m", "-n", "--propagation", "private", "sh", "-c", NAMESPACE_SCRIPT, "claude-control-chain", *argv]


def _parse_metrics(stdout: str) -> dict[str, Any]:
    lines = [line[len(METRICS_PREFIX):] for line in stdout.splitlines() if line.startswith(METRICS_PREFIX)]
    if not lines:
        return {}
    try:
        value = json.loads(lines[-1])
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


# ---------------------------------------------------------------------------
# 链体：记录、隔离自检、副本树修改与快照
# ---------------------------------------------------------------------------


class _ChainRecorder:
    """链体的步骤与事实记录；``emit`` 在标准输出写一行指标（占位失败之前也写，外层据此报告已通过的步骤）。"""

    def __init__(self, chain: str) -> None:
        self.chain = chain
        self.steps: list[str] = []
        self.facts: dict[str, Any] = {"upstream_requests": 0}
        self.started = time.monotonic()

    def step(self, name: str) -> None:
        self.steps.append(name)

    def emit(self, *, pending: str | None = None) -> None:
        payload = {
            "chain": self.chain,
            "steps": self.steps,
            "pending": pending,
            "exercise_seconds": round(time.monotonic() - self.started, 3),
            **self.facts,
        }
        print(METRICS_PREFIX + json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)


def _await_b208(case: unittest.TestCase, recorder: _ChainRecorder, missing: str) -> None:
    """依赖 B2-08 的步骤：跑到这里一律失败（不跳过、不静默通过），失败信息写明「等 B2-08」和缺的步骤。"""

    message = f"{PENDING_PREFIX}：{missing}"
    recorder.emit(pending=message)
    case.fail(message)


def _tree_root() -> Path:
    return Path(os.environ[TREE_ENV]).resolve()


def _assert_isolation(case: unittest.TestCase, recorder: _ChainRecorder) -> None:
    """链体第一步：必须由外层启动、模块都来自副本树；命名空间模式另核网络只有 lo、数据根只读、执行副本已遮住。"""

    from tools.official_client_capture import claude_upgrade
    from tools.official_client_control import receipts as store_receipts

    mode = os.environ.get(ISOLATION_ENV)
    if mode not in (ISOLATION_NAMESPACE, ISOLATION_DEGRADED) or not os.environ.get(TREE_ENV):
        case.fail("链体只能由外层隔离外壳启动（缺少隔离方式或副本根）")
    tree = _tree_root()
    source = Path(os.environ.get(SOURCE_REPO_ENV, "")).resolve()
    case.assertNotEqual(tree, source, "副本根不能是原仓库")
    for module in (claude_upgrade, store_receipts):
        case.assertIn(tree, Path(module.__file__).resolve().parents, f"{module.__name__} 回落到了副本树之外")
    if mode == ISOLATION_NAMESPACE:
        interfaces = sorted(name for _index, name in socket.if_nameindex())
        case.assertEqual(interfaces, ["lo"], "命名空间里仍有外网接口")
        if HOST_DATA_ROOT.is_dir():
            probe = HOST_DATA_ROOT / f".claude-control-chain-probe-{os.getpid()}"
            try:
                probe.write_text("x", encoding="utf-8")
            except OSError:
                pass
            else:
                probe.unlink()
                case.fail("命名空间里宿主数据根仍可写")
        if CODEX_CAPTURE_ROOT.is_dir():
            case.assertEqual(list(CODEX_CAPTURE_ROOT.iterdir()), [], "命名空间里 Codex 执行副本没有被遮住")
    recorder.facts.update(isolation=mode, network_isolated=mode == ISOLATION_NAMESPACE)
    recorder.step("隔离自检")


def _mutate_control_file(marker: str) -> Path:
    """在副本树的 control 层受管文件末尾追加一行注释，模拟修好并受监督部署的新工具；只许改副本树，不许碰原仓库。"""

    from tools.official_client_capture import claude_upgrade

    tree = _tree_root()
    target = (tree / "tools" / "official_client_capture" / CONTROL_MUTATION_FILE).resolve()
    expected = Path(claude_upgrade.__file__).resolve().with_name(CONTROL_MUTATION_FILE)
    if tree not in target.parents or target != expected or not target.is_file():
        raise AssertionError(f"只许改副本树里的受管文件：{target}")
    with target.open("a", encoding="utf-8") as stream:
        stream.write(f"\n# Claude 工具演进链：{marker}（只在隔离副本树里追加，模拟部署的新工具）\n")
    return target


def _tree_snapshot(root: Path) -> dict[str, tuple[Any, ...]]:
    """目录树快照：相对路径 → (类型, 大小, 内容摘要)。不比时间戳：零写入看的是有没有新增、删除或改写文件。"""

    entries: dict[str, tuple[Any, ...]] = {}
    if not root.exists():
        return entries
    for path in [root, *sorted(root.rglob("*"))]:
        info = path.lstat()
        regular = stat.S_ISREG(info.st_mode)
        entries[path.relative_to(root).as_posix()] = (
            stat.S_IFMT(info.st_mode),
            info.st_size if regular else None,
            hashlib.sha256(path.read_bytes()).hexdigest() if regular else None,
        )
    return entries


def _write_json(root: Path, path: Path, value: Any) -> Path:
    """写私有 JSON；从 ``root`` 往下的各级目录都收紧成 0700（总账与计时账本要求私有目录）。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent in path.parents:
        if parent == root:
            break
        parent.chmod(0o700)
    path.write_text(json.dumps(value, ensure_ascii=False) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _cli(arguments: list[str], *, timeout: float = 120.0) -> subprocess.CompletedProcess:
    """在副本树里起一个 Python 子进程（继承链体的环境：``PYTHONPATH`` 只含副本根，命名空间模式下仍在同一命名空间里）。"""

    return subprocess.run(
        [sys.executable, *arguments],
        capture_output=True,
        text=True,
        cwd=str(_tree_root()),
        env=dict(os.environ),
        timeout=timeout,
        stdin=subprocess.DEVNULL,
    )


class _DeadlineSite:
    """截止链的 Claude 最小 Campaign：规范布局的数据根、Claude 项目总账 v2（仅夹具）、计时账本 v2 与 Campaign 总计划。

    总计划只放截止路径要读的字段（客户端、ID、模式、目标版本与计时账本绑定）：项目总账与计时账本的截止、暂停与延期
    直接读 ``campaign.json``，不经编排器的总计划读取，所以这里不必写完整的 Claude 总计划。``age`` 把账本的开始时刻往前推，
    1 分钟的阶段预算因此在几秒后真实到期（不改时钟）。
    """

    def __init__(self, case: unittest.TestCase, name: str, *, age: int) -> None:
        from tools.official_client_capture import codex_upgrade_project_ledger as project
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing

        base = Path(tempfile.mkdtemp(prefix=f"deadline-{name}-")).resolve()
        case.addCleanup(shutil.rmtree, base, True)
        self.root = base / "staging"
        self.root.mkdir(mode=0o700)
        self.start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(seconds=age)
        self.project = self.root / project.ledger_dir_name("claude")
        project.create_project_ledger(
            self.project,
            project_id=f"claude-b5a05-{name}",
            absolute_deadline_utc=self.at(3600),
            deadline_approved_by="fixture",
            estimation_policy="none",
            estimation_policy_approved_by="fixture",
            fixture_only=True,
            started_at_utc=self.at(0),
            initial_precise_count=7,
            client="claude",
        )
        self.campaign_id = f"claude-code-2_1_285-b5a05-{name}"
        self.campaign = self.root / "evidence" / "campaigns" / self.campaign_id
        self.campaign.mkdir(parents=True, mode=0o700)
        self.ledger = self.root / "timing"
        timing.create_ledger(
            self.ledger,
            upgrade_id=self.campaign_id,
            baseline_version=BASELINE_VERSION,
            target_version=TARGET_VERSION,
            campaign_purpose="validation_only",
            evidence_decision="recapture",
            started_at_utc=self.at(0),
            total_budget_minutes=5,
            stage_budgets_minutes={phase: 1 for phase in timing.PHASE_ORDER},
            project_ledger_dir=self.project,
            client="claude",
        )
        _write_json(self.root, self.campaign / "campaign.json", {
            "schema_version": "official-client-upgrade-campaign/v1",
            "client": "claude",
            "campaign_id": self.campaign_id,
            "campaign_mode": "formal",
            "target_version": TARGET_VERSION,
            "control_receipts": {"upgrade_timing": {
                "ledger_dir": str(self.ledger),
                "ledger_plan_sha256": hashlib.sha256((self.ledger / "ledger.json").read_bytes()).hexdigest(),
            }},
        })
        _write_json(self.root, self.campaign / "control" / "vc" / "campaign-plan.json",
                    {"campaign_id": self.campaign_id, "original_deadline_at_utc": self.at(300)})
        case.assertEqual(project.register_existing_campaign(self.campaign)["status"], "appended")

    def at(self, seconds: int) -> str:
        return (self.start + timedelta(seconds=seconds)).isoformat()

    def originals(self) -> dict[Path, bytes]:
        """原始坐标文件：延期只追加事件，这四份字节不得改变。"""

        paths = (self.campaign / "campaign.json", self.campaign / "control" / "vc" / "campaign-plan.json",
                 self.ledger / "ledger.json", self.project / "plan.json")
        return {path: path.read_bytes() for path in paths}


def _claude_deploy_receipt(control: Path, identity: Mapping[str, Any]) -> Path:
    """新身份的 Claude 部署收据（``official-client-managed-deploy/v1`` 的最小形状，身份在 ``identity`` 对象里）。

    表示「修好的工具已受监督部署」：编排器的工具演进预览要绑定当前五摘要的部署收据（B2-08 落实时按 ``identity`` 读）。"""

    from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification

    control.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dt%H%M%Sz")
    supervisor_file = REPO_ROOT / "tools" / "official_client_capture" / "codex_upgrade_supervisor.py"
    payload = {
        "schema_version": policy_certification.CLAUDE_DEPLOY_RECEIPT_SCHEMA,
        "status": "passed",
        "client": "claude",
        "created_at_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "identity": {"policy_version": identity["policy_version"],
                     **{name: identity[name] for name in policy_certification.IDENTITY_FIELDS}},
        "supervisor_sha256": hashlib.sha256(supervisor_file.read_bytes()).hexdigest(),
    }
    path = control / f"claude-managed-deploy-{stamp}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def _synthetic_store(root: Path, name: str, campaign_id: str, *, evidence: bool) -> Any:
    """Store 夹具的合成 Campaign v2：在当前副本树上现算七项身份并建 Campaign；``evidence`` 时走完证据（供前序导入）。"""

    from tools.official_client_control.tests.fixtures import SyntheticCampaign

    path = root / name
    path.mkdir(mode=0o700)
    fixture = SyntheticCampaign(path, schema="v2")
    fixture.campaign_id = campaign_id
    fixture.bootstrap_and_campaign()
    if evidence:
        fixture.discovery_and_evidence()
    return fixture


def _import_payload(predecessor: Any, target: Any) -> dict[str, Any]:
    """前序导入登记（``regression_fixture``）：两组七项声明取双方冻结字段的独立导出值，导入原子断言台账的对象闭包。"""

    from tools.official_client_control.contracts import derive_identity_conditions
    from tools.official_client_control.receipts import predecessor_object_closure

    source = predecessor.store.load_campaign(predecessor.campaign_id)
    campaign = target.store.load_campaign(target.campaign_id)
    return {
        "client": "claude",
        "campaign_id": target.campaign_id,
        "predecessor": {
            "store_root": str(predecessor.store.root),
            "campaign_id": predecessor.campaign_id,
            "campaign_identity_sha256": source["identity_sha256"],
        },
        "imported_objects": predecessor_object_closure(predecessor.store, [predecessor.references["atomic-assertion-ledger"]]),
        "imported_fact_refs": [],
        "identity_conditions": derive_identity_conditions(campaign)[0],
        "source_identity_conditions": derive_identity_conditions(source)[0],
        "mode": "regression_fixture",
        "approved_by": "synthetic-operator",
        "approved_at_utc": target._time(),
    }


# ---------------------------------------------------------------------------
# 四条链
# ---------------------------------------------------------------------------


class ClaudeControlChainTests(unittest.TestCase):
    """外层四个场景入口（方法直接写在类体里，部署脚本按 AST 核对）与四个链体（只在副本树子进程里运行）。"""

    real_chain_metrics: dict[str, Any]

    # ---- 外层：pre-A3 登记的四个场景入口 ----

    def test_reconcile_preview_approve_resume_reruns_failed_jobs_only(self) -> None:
        self._run_chain("reconcile")

    def test_deadline_pause_extension_sigkill_resumes_original_campaign(self) -> None:
        self._run_chain("deadline")

    def test_same_root_cause_limit_pauses_until_repair_receipt(self) -> None:
        self._run_chain("root-cause")

    def test_tool_evolution_registration_moves_effective_identity(self) -> None:
        self._run_chain("tool-evolution")

    def _isolation_mode(self) -> str:
        """Linux 上以 root 运行必须进命名空间（缺 ``unshare`` 即失败，不降级）；其余环境降级为副本树加替身。"""

        if sys.platform.startswith("linux") and os.geteuid() == 0:
            if shutil.which("unshare") is None:
                self.fail("Linux root 上必须进 unshare -m -n 命名空间运行，但 PATH 里没有 unshare")
            return ISOLATION_NAMESPACE
        return ISOLATION_DEGRADED

    def _run_chain(self, chain: str) -> None:
        spec = CHAINS[chain]
        started = time.monotonic()
        mode = self._isolation_mode()
        metrics: dict[str, Any] = {}
        completed: subprocess.CompletedProcess | None = None
        copy_seconds: float | None = None
        timed_out = False
        try:
            with tempfile.TemporaryDirectory(prefix=f"claude-control-chain-{chain}-", dir=_scratch_root()) as directory:
                staging = Path(directory).resolve() / "staging"
                staging.mkdir(mode=0o700)
                tree = _copy_two_root_tree(staging / "tree")
                copy_seconds = round(time.monotonic() - started, 3)
                environment = trees.subprocess_env(tree, {
                    project_ledger_fixture.FIXTURE_ONLY_ENV: "1",
                    "TMPDIR": str(staging),
                    ISOLATION_ENV: mode,
                    TREE_ENV: str(tree),
                    SOURCE_REPO_ENV: str(REPO_ROOT),
                    # 身份记忆化用本次私有的目录：不读写宿主的记忆化目录（命名空间里它可能在只读的数据根内）。
                    IDENTITY_MEMO_ENV: str(staging / "identity-memo"),
                })
                argv = [sys.executable, "-m", "unittest", "-v", f"{CHAIN_MODULE}.{type(self).__name__}.{spec['exercise']}"]
                command = _namespace_argv(argv) if mode == ISOLATION_NAMESPACE else argv
                try:
                    completed = subprocess.run(command, capture_output=True, text=True, cwd=str(tree), env=environment,
                                               timeout=CHAIN_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL)
                except subprocess.TimeoutExpired:
                    timed_out = True
                if completed is not None:
                    metrics = _parse_metrics(completed.stdout)
        finally:
            elapsed = round(time.monotonic() - started, 3)
            # 链体报的事实在前，外层量的耗时与隔离方式在后（外层的值为准，同名键不被链体覆盖）。
            self.real_chain_metrics = {
                **{key: value for key, value in metrics.items() if key != "chain"},
                "scenario": spec["scenario"],
                "seconds": elapsed,
                "copy_seconds": copy_seconds,
                "isolation": mode,
                "network_isolated": mode == ISOLATION_NAMESPACE,
                "returncode": None if completed is None else completed.returncode,
            }
        if timed_out or elapsed >= CHAIN_BUDGET_SECONDS:
            self.fail(f"{spec['scenario']} 超过 {CHAIN_BUDGET_SECONDS} 秒（实测 {elapsed} 秒）：每条链的验收上限是 15 分钟")
        assert completed is not None
        pending = metrics.get("pending")
        if completed.returncode != 0:
            if isinstance(pending, str) and pending.startswith(PENDING_PREFIX) and PENDING_PREFIX in completed.stderr:
                self.fail(f"{spec['scenario']}：{pending}（占位之前的步骤已通过：{'、'.join(metrics.get('steps') or [])}；"
                          f"隔离={mode}；耗时 {elapsed} 秒）")
            self.fail(f"{spec['scenario']} 子进程失败（退出码 {completed.returncode}，隔离={mode}）：\n"
                      f"{completed.stdout[-3000:]}\n{completed.stderr[-6000:]}")
        self.assertNotIn("skipped=", completed.stderr, "链体不得跳过")
        self.assertIsNone(pending, "通过的链不能还留着占位")
        self.assertEqual(metrics.get("upstream_requests"), 0, "链体必须零请求")

    # ---- 链体：reconcile ----

    def _exercise_reconcile(self) -> None:
        recorder = _ChainRecorder("reconcile")
        _assert_isolation(self, recorder)
        self._failed_first_batch(recorder)
        _await_b208(self, recorder,
                    "reconcile-attempt 对账失败 attempt 并给出恢复预览 → 批准预览 → resume --rerun-failed --recovery-preview "
                    "只重跑失败 Job；无预约时 reconcile-supervisor-run → N+1 重派")

    def _failed_first_batch(self, recorder: _ChainRecorder) -> tuple[Any, Path, dict[str, Any], dict[str, Any]]:
        """首批采集失败（B2-06 已落实）：合成 probe 每次都失败，补跑两次后归档，attempt 以 failed 收口并记下带 client
        维度的根因；失败 attempt 存在时再跑 run 被拒（只能经显式 resume 续跑，不会悄悄重跑）。"""

        from tools.official_client_capture import claude_upgrade
        from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests

        capture_tests.CaptureFixture.setUpClass()
        fixture = capture_tests.CaptureFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        campaign = fixture.write_campaign(capture_tests._reduced_payload({capture_tests.SMALL_SYNTHETIC_JOB}))
        fixture.set_control({"fail": {"v4-oauth-refresh": 99}})
        run = ["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"]
        with fixture.patched():
            code, result, stderr = fixture.run_main(run)
        self.assertEqual((code, (result or {}).get("status")), (2, "failed"), stderr)
        with fixture.patched():
            _root, attempt = claude_upgrade._load_capture_attempt(
                campaign, "official", None, result["attempt_id"], _replay_evidence_permissions=False
            )
        (job,) = attempt["results"]
        self.assertEqual(job["attempt_index"], 3, "必需 Job 补跑两次后才收口")
        self.assertEqual(len(job["archived_evidence_roots"]), 2, "前两次失败的证据根已归档")
        causes = attempt["root_causes"]
        self.assertTrue(causes, "失败 attempt 必须记下根因")
        self.assertTrue(all(cause["stable_dimensions"] == {"client": "claude", "phase": "official"} for cause in causes),
                        "根因维度来自 Claude 码表、带 client")
        self.assertEqual(len(fixture.relay_calls()), 3, "替身中继只被调用 3 次（1 次加 2 次补跑），没有任何真实上游")
        with fixture.patched():
            code, _again, stderr = fixture.run_main(run)
        self.assertEqual(code, 1)
        self.assertIn("失败 attempt", stderr)
        recorder.facts.update(attempt_status="failed", relay_calls=len(fixture.relay_calls()),
                              root_causes=[cause.get("root_cause_id") for cause in causes])
        recorder.step("首批采集失败：补跑两次后归档，attempt 以 failed 收口，根因带 client 维度")
        recorder.step("失败 attempt 存在时再跑 run 被拒")
        return fixture, campaign, result, attempt

    # ---- 链体：deadline ----

    def _exercise_deadline(self) -> None:
        from tools.official_client_capture import codex_upgrade_project_ledger as project
        from tools.official_client_capture import codex_upgrade_supervisor as supervisor
        from tools.official_client_capture import codex_upgrade_timing_ledger as timing
        from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

        recorder = _ChainRecorder("deadline")
        _assert_isolation(self, recorder)

        # ① 运行中的命令按阶段截止停：全局截止 120 秒之后，但阶段预算几秒后就到期，命令必须在阶段截止处被截停。
        site = _DeadlineSite(self, "runtime", age=54)
        client = supervisor.SupervisorClient(
            site.root / "runtime-parent", campaign_id=site.campaign_id, phase="VC-0", deadline_at_epoch=time.time() + 120,
            heartbeat_seconds=.2, watchdog_timeout_seconds=3, ledger_interval_seconds=.2, terminate_owner=False,
            campaign_dir=site.campaign,
        )
        client.start()
        started = time.monotonic()
        try:
            marker = site.root / "must-not-finish.txt"
            with self.assertRaises(supervisor.SupervisorError):
                client.run_command(
                    [sys.executable, "-c", "import sys, time; from pathlib import Path; time.sleep(20); "
                     "Path(sys.argv[1]).write_text('late')", str(marker)],
                    operation="claude-chain:stage-boundary", timeout_seconds=30,
                )
            self.assertFalse(marker.exists(), "被截停的命令不得写出产物")
            self.assertLess(time.monotonic() - started, 12)
            self.assertGreater(supervisor._read_state(client.run_dir)["deadline_at_epoch"], time.time() + 90,
                               "阶段截止不改写全局截止")
            self.assertEqual(timing.inspect_ledger(site.ledger)["status"], "deadline_paused")
            self.assertFalse(project.replay_head(site.project)["terminal_campaigns"], "到期只暂停、不写终态")
        finally:
            client.stop(reason="Claude 截止链：运行中命令边界验收收口", status="failed")
        recorder.step("运行中命令按阶段截止被截停，计时账本 deadline_paused，总账无终态")

        # ② 看门狗按阶段截止停：只有心跳、没有命令时，看门狗在阶段截止处写停止收据。
        site = _DeadlineSite(self, "watchdog", age=54)
        client = supervisor.SupervisorClient(
            site.root / "runtime-parent", campaign_id=site.campaign_id, phase="VC-0", deadline_at_epoch=time.time() + 120,
            heartbeat_seconds=.2, watchdog_timeout_seconds=3, ledger_interval_seconds=.2, terminate_owner=False,
            campaign_dir=site.campaign,
        )
        client.start()
        run_dir = client.run_dir
        started = time.monotonic()
        try:
            while supervisor._read_state(run_dir)["state"] == "running" and time.monotonic() - started < 12:
                client.heartbeat("claude-chain:stage-boundary", force=True)
                time.sleep(.1)
            self.assertEqual(supervisor._read_state(run_dir)["state"], "watchdog-aborted")
            stop = json.loads((run_dir / "stop-receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(stop["reason"], "global-wall-clock-deadline-expired")
            self.assertLess(time.monotonic() - started, 12)
            self.assertEqual(timing.inspect_ledger(site.ledger)["status"], "deadline_paused")
            self.assertFalse(project.replay_head(site.project)["terminal_campaigns"])
        finally:
            client.stop(reason="Claude 截止链：看门狗边界验收收口", status="failed")
        recorder.step("看门狗按阶段截止写停止收据，计时账本 deadline_paused")

        # ③ 两本账暂停、经 Claude 编排器命令行延期，两本账之间强杀后补齐收敛。
        site = _DeadlineSite(self, "extend", age=55)
        expired = datetime.fromisoformat(str(timing.inspect_ledger(site.ledger)["stage_deadline_at_utc"]).replace("Z", "+00:00"))
        while time.time() <= expired.timestamp():
            time.sleep(min(.25, max(.01, expired.timestamp() - time.time() + .01)))
        paused = project.pause_campaign_deadline(site.campaign)
        self.assertEqual((paused["status"], paused["paused_scopes"]), ("deadline_paused", ["stage"]))
        # 暂停期间各消费入口的总账准入一律拒绝（Claude 入口接入总账准入是 B2-08 的事，这里直接核准入函数本身）。
        for command in ("campaign-run", "resume", "reuse-official-evidence", "seal"):
            with self.subTest(command=command), self.assertRaisesRegex(project.ProjectLedgerError, "暂停"):
                project.assert_campaign_admitted(site.campaign, command=command, require=True)
        originals = site.originals()
        head_before = project.replay_head(site.project)
        requests_before = timing.inspect_ledger(site.ledger)["total_live_request_count"]
        recorder.step("阶段截止真实到期，两本账暂停，暂停期间准入一律拒绝")

        orchestrator = ["-m", "tools.official_client_capture.claude_upgrade"]
        new_deadline = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        preview_run = _cli([*orchestrator, "deadline-extend", "preview", "--campaign-dir", str(site.campaign),
                            "--scope", "stage", "--phase", "VC-0", "--new-deadline-at-utc", new_deadline,
                            "--reason", "Claude 截止链：真实到期后在原 Campaign 继续"])
        self.assertEqual(preview_run.returncode, 0, preview_run.stdout + preview_run.stderr)
        preview = json.loads(preview_run.stdout)
        apply_arguments = ["deadline-extend", "apply", "--campaign-dir", str(site.campaign), "--preview", preview["preview_path"],
                           "--approve-sha256", preview["review_sha256"], "--approved-by", "fixture-reviewer"]
        killed = _cli(["-c", SIGKILL_BETWEEN_LEDGERS, *apply_arguments])
        self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stdout + killed.stderr)
        self.assertIn("extension_pending", artifacts.effective_deadlines(site.campaign)["paused_scopes"],
                      "项目总账已写延期、Campaign 账本还没写：保持暂停，不成为放行窗口")
        with self.assertRaises(project.ProjectLedgerError):
            project.assert_campaign_admitted(site.campaign, command="seal", require=True)
        recorder.step("经 Claude 命令行预览延期；批准时在两本账之间被强杀，补齐前保持暂停")

        for expected in ("appended", "duplicate"):
            completed = _cli([*orchestrator, *apply_arguments])
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual((result["project_event"], result["campaign_event"]), ("duplicate", expected))
        head_after = project.replay_head(site.project)
        self.assertEqual(
            (head_before["precise_total"], head_before["estimated_total"], head_before["root_cause_counts"]),
            (head_after["precise_total"], head_after["estimated_total"], head_after["root_cause_counts"]),
            "延期不改请求计数与根因计数",
        )
        self.assertEqual(timing.inspect_ledger(site.ledger)["total_live_request_count"], requests_before)
        self.assertEqual(sum(row["event_type"] == "deadline_extended" for row in project._load_events(site.project)), 1)
        self.assertEqual(sum(row["event_type"] == "deadline_extended" for row, _ in timing._load_events(site.ledger)), 1)
        self.assertEqual(site.originals(), originals, "延期只追加事件，原始坐标文件字节不变")
        self.assertEqual(timing.inspect_ledger(site.ledger)["status"], "active")
        self.assertIsNotNone(project.assert_campaign_admitted(site.campaign, command="resume", require=True),
                             "延期补齐后准入放行")
        recorder.facts.update(sigkill_count=1, project_extension_events=1, campaign_extension_events=1)
        recorder.step("重跑批准两次：第一次补齐 Campaign 账本、第二次幂等；计数与原始字节不变，账本回到 active、准入放行")

        _await_b208(self, recorder,
                    "延期后在原 Campaign 续跑（resume）；入口总账准入门禁（暂停期间 capture-official run／seal 被拒）；"
                    "到期的 attempt 经 reconcile-attempt 入账后只暂停")

    # ---- 链体：root-cause ----

    def _exercise_root_cause(self) -> None:
        recorder = _ChainRecorder("root-cause")
        _assert_isolation(self, recorder)
        _fixture, _campaign, _result, attempt = self._failed_first_batch(recorder)
        identifiers = sorted({str(cause.get("root_cause_id")) for cause in attempt["root_causes"]})
        self.assertTrue(identifiers and all(identifiers), "根因必须带稳定 ID（同根因累计按它计数）")
        recorder.facts["root_cause_ids"] = identifiers
        recorder.step("第一次失败的根因已形成稳定 ID")
        _await_b208(self, recorder,
                    "reconcile-attempt 把两次同根因失败入账 → 总账同根因达上限只暂停（root_cause_repair，不写终态）、"
                    "入口准入只挡该根因 → record-root-cause-repair 修复收据 → campaign-resume 放行续跑")

    # ---- 链体：tool-evolution ----

    def _exercise_tool_evolution(self) -> None:
        from tools.official_client_capture import codex_upgrade_policy_certification as policy_certification
        from tools.official_client_capture.tests import test_claude_upgrade_capture as capture_tests
        from tools.official_client_control.errors import ControlError
        from tools.official_client_control.gates import WorkflowGates
        from tools.official_client_control.receipts import (
            effective_tool_identity,
            finalize_predecessor_import,
            register_predecessor_import,
            replay_receipt,
        )

        recorder = _ChainRecorder("tool-evolution")
        _assert_isolation(self, recorder)
        store_root = Path(tempfile.mkdtemp(prefix="store-")).resolve()
        self.addCleanup(shutil.rmtree, store_root, True)

        # 夹具：Store 层的两份前序（当前副本树身份下建立并走完证据）；编排器 Campaign（总计划冻结当前身份）跑一次零请求
        # 取证，attempt 停在 awaiting_receipts。
        first = _synthetic_store(store_root, "predecessor-1", PREDECESSOR_IDS[0], evidence=True)
        second = _synthetic_store(store_root, "predecessor-2", PREDECESSOR_IDS[1], evidence=True)
        capture_tests.CaptureFixture.setUpClass()
        fixture = capture_tests.CaptureFixture()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        campaign = fixture.write_campaign(capture_tests._reduced_payload({capture_tests.SMALL_SYNTHETIC_JOB}))
        run = ["capture-official", "run", "--campaign-dir", str(campaign), "--acknowledge-live-requests"]
        with fixture.patched():
            code, result, stderr = fixture.run_main(run)
        self.assertEqual((code, (result or {}).get("status")), (2, "awaiting_receipts"), stderr)
        attempt_id = result["attempt_id"]
        recorder.step("编排器 Campaign 零请求取证，attempt 等待封存")

        # 第一次改 control 层：目标 Store Campaign 在与前序不同的工具身份下建立（前序导入的常规形态），导入第一份前序并
        # finalize 出旧收据；第二份前序先登记、暂不 finalize。
        _mutate_control_file("目标 Store Campaign 建立之前的工具修订")
        target = _synthetic_store(store_root, "target", STORE_TARGET_ID, evidence=False)
        formed_tool = target.tool_sha256
        import_first = register_predecessor_import(target.store, _import_payload(first, target))["import_ref"]
        receipt_first = finalize_predecessor_import(target.store, import_first, target._time())
        import_second = register_predecessor_import(target.store, _import_payload(second, target))["import_ref"]
        recorder.step("Store 目标 Campaign 导入第一份前序得到旧收据，第二份前序已登记待 finalize")

        # 部署新工具：第二次改 control 层，并写出五摘要等于当前副本树的 Claude 部署收据。
        _mutate_control_file("部署修好的新工具")
        identity = policy_certification.current_identity("claude")
        deployment = _claude_deploy_receipt(fixture.data / "control", identity)
        policy_certification.load_deployment_receipt(deployment, expected_identity=identity, client="claude")
        recorder.step("部署新工具：control 层改动，新身份的 Claude 部署收据校验通过")

        # ① 编排器层：未登记演进即拒绝。seal 不取执行租约，拒绝时 Campaign 目录与证据根逐字节不变。
        campaign_before, runs_before = _tree_snapshot(campaign), _tree_snapshot(fixture.data / "runs")
        with fixture.patched():
            code, _sealed, stderr = fixture.run_main(["capture-official", "seal", "--campaign-dir", str(campaign),
                                                      "--attempt-id", attempt_id])
        self.assertEqual(code, 1, stderr)
        for text in ("capture-official seal 拒绝", "尚未登记工具演进", "tool-evolution", "本次未写入任何文件"):
            self.assertIn(text, stderr)
        self.assertEqual(_tree_snapshot(campaign), campaign_before, "seal 拒绝时 Campaign 目录零写入")
        self.assertEqual(_tree_snapshot(fixture.data / "runs"), runs_before, "seal 拒绝时证据根零写入")
        code, status, stderr = fixture.run_main(["tool-evolution-status", "--campaign-dir", str(campaign)])
        self.assertEqual(code, 0, stderr)
        self.assertEqual((status["status"], status["unregistered_drift"], status["effective_index"]),
                         ("evolution_required", ["control"], 0), "只改了 control 层：漂移只报 control，wire／evidence 不变")
        attempts_before = sorted(path.name for path in (campaign / "official" / "attempts").iterdir())
        with fixture.patched():
            code, _again, stderr = fixture.run_main(run)
        self.assertEqual(code, 1, stderr)
        self.assertIn("capture-official run 拒绝", stderr)
        self.assertIn("尚未登记工具演进", stderr)
        self.assertEqual(sorted(path.name for path in (campaign / "official" / "attempts").iterdir()), attempts_before,
                         "run 拒绝在预约之前：不写 attempt")
        self.assertEqual(_tree_snapshot(fixture.data / "runs"), runs_before, "run 拒绝时证据根零写入")
        lease = json.loads((campaign / ".campaign-lease.json").read_text(encoding="utf-8"))
        # run 的拒绝发生在执行租约之内：共享租约把异常退出记成停线（与 Codex 同一语义），恢复要经 B2-08 的对账。
        self.assertEqual(lease.get("state"), "stop_the_line")
        recorder.facts.update(relay_calls=len(fixture.relay_calls()), lease_after_rejected_run=lease.get("state"))
        recorder.step("编排器层未登记即拒绝：seal 零写入、tool-evolution-status 报 control 漂移、run 预约前被拒")

        # ② Store 层：finalizer 未登记即拒，登记演进后有效身份更新、finalizer 通过，旧收据按形成期重放仍通过。
        with self.assertRaisesRegex(ControlError, "工具身份未登记演进：先执行 tool-evolution"):
            finalize_predecessor_import(target.store, import_second, target._time())
        # 这里直接经 Store 登记演进（Store 层子链的替身登记）；B2-08 落实后由编排器 tool-evolution 批准时写出这份登记，
        # evolution_receipt 指向编排器的演进收据。
        registration = target.register_evolution()
        store_campaign = target.store.load_campaign(STORE_TARGET_ID)
        effective = effective_tool_identity(target.store, store_campaign)
        self.assertEqual(effective["tool_files_sha256"], identity["tool_files_sha256"], "Store 有效身份等于新部署的工具")
        self.assertNotEqual(effective["tool_files_sha256"], formed_tool)
        receipt_second = finalize_predecessor_import(target.store, import_second, target._time())
        self.assertEqual(target.store.load_receipt(receipt_second)["formed_under"]["tool_identity_ref"], registration,
                         "登记后的新收据按新有效身份形成")
        old = replay_receipt(target.store, receipt_first)
        self.assertIsNone(old["formed_under"]["tool_identity_ref"], "旧收据仍按它形成时的初始身份重放")
        self.assertEqual(old["producer_tool_sha256"], formed_tool)
        self.assertEqual(target.store.replay(external_root=target.external_root, require_external=True)["result"], "passed")
        self.assertEqual(WorkflowGates(target.store).status(STORE_TARGET_ID)["tool_identity_index"], 1)
        recorder.facts["store_tool_identity_index"] = 1
        recorder.step("Store 层：finalizer 未登记即拒，登记演进后通过，旧收据按形成期重放、全库重放通过")

        _await_b208(self, recorder,
                    "编排器 tool-evolution 登记（_tool_evolution_preview，批准时同时登记 Store 的 tool_evolution_registration，"
                    "两侧目前没有衔接）→ 登记后 capture-official seal 两层放行；run 被拒留下的停线租约经对账恢复")


if __name__ == "__main__":
    unittest.main()
