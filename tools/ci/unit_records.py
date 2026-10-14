#!/usr/bin/env python3
"""单元执行记录与承接（E3-01）：统一调度执行器（``unit_executor.py``）的记录、记录库、输入范围、承接判定与清单自检。

为什么要有：一轮门禁里只要有一个单元失败，原来这一轮的结果就整体作废，下一轮全部重来。这里给每个单元的每次执行
落一条不可变记录，记录独立于认证和收据存在；下一轮只承接「正式执行、结论通过、单元规格与输入摘要都没变、调度
策略版本与环境指纹没变、执行器没变、没过期限」的记录，其余重新执行。诊断执行（并行失败后的单独重跑）的记录永远
不承接——否则会出现「并行失败 → 单独诊断通过 → 下一轮把诊断结果当正式结果接过来」，绕过根因定位（方案 D4、D6）。

* **记录**（``unit-execution-record/v1``）：单元 ID 与类型、执行类别（正式／诊断）、所在运行、执行器版本（执行器目录里
  参与调度、判定、输入解析与门禁编排的文件逐个摘要）、调度策略版本、环境指纹与明细、单元规格（测试单元＝起点、
  模式、测试 ID 集合、启动前缀、测试组环境、额度、独占、超时；命令单元＝argv、工作目录、环境、额度、独占、超时）、
  输入明细与摘要（类别同 E2-05）、测试 ID 与逐个结论、退出状态、是否被信号终止、是否超时、用量、日志摘要、起止
  时间、自摘要。
* **记录库**（``--record-store``）：``records/<单元 ID 摘要前 16 位>/<记录自摘要>.json`` 按内容寻址、只增不改；日志按
  内容摘要存 ``logs/<sha256>.log``；每次运行的清单存 ``runs/<run_id>.json``（自检通过才存）。清理由入口日常维护
  （``entry_housekeeping.py``，E4-02）做：保留期内写入的与仍被 v2 认证、P0 v2 证据引用的运行连同其记录与日志都留着。
* **承接**（只在 ``run-gates``、给了记录库、模式为全集通过时）：见 ``check_record``——逐条核对上面每一项，另外要求
  该记录所在运行的清单把它列为这个单元的正式执行、日志在库且摘要相符；拿不准就执行。
* **输入**：仓库内容按 ``git ls-files`` 的跟踪文件逐个算 sha256（承接模式要求测试树干净）。采集工具测试单元＝受管
  工具树（测试组起点上一级、不含测试目录）、本模块静态依赖闭包里的测试目录文件、夹具目录、docs、仓库其余部分，
  整目录读取测试的模块另加整个测试目录，闭包里有真实链的另加全部辅助模块与真实链目录（方案原文），读真实仓库
  git 历史的另加 HEAD；命令单元的输入由门禁清单声明（E2-05 从宽：整个仓库＋HEAD＋门禁项另列的输入）。输入范围拿不准
  的一律从宽（方案 D3），按读集核查的证据收窄（10-02 逐模块实跑追踪），读集审计（E3-04）兜底漏声明。
* **环境指纹**：系统、架构、内核、Python 版本与构建、已装 Python 分发包、系统软件包（dpkg）、有效用户、主机名、整机
  核数、执行器进程能看到的全部环境变量（只排除决定缓存位置的两个与执行器自己的 ``UNIT_EXECUTOR_*``），再加门禁
  清单给出的工具链版本与前端依赖摘要。任何一项变了，全部单元不承接（方案「环境指纹变了全部重跑」）。
* **清单**（``unit-execution-manifest/v1``）：每次运行逐单元写明「本次执行」还是「承接」及其依据（承接写原运行与
  记录摘要，执行写不承接的原因），连同测试组全集与诊断执行。**自检**（``verify_manifest``）：执行＋承接＝全集、
  不重复；重新执行全集模式不得有承接项；每条承接都追到原始的正式执行记录并逐项重验；测试组全部记录的测试 ID
  并集等于全集。
* **逐单元承接**（B5a-08，``--mode unit-inherit``，合同见同目录 ``unit_inherit.py``）：来源记录可来自多次运行、多个提交，
  逐单元判定承接或重跑。清单另引用决策收据（``unit-inherit-decision/v1``，与清单一起发布进记录库的
  ``unit-inherit/decisions/<run_id>.json``），自检另核对：承接项都要求读集覆盖闭合；决策收据在库、自摘要与清单登记的
  相符、属于本次运行且三个复核点都通过；承接项的来源（运行与记录摘要）、执行项与决策收据逐单元一致，上下文与来源
  集合摘要与决策收据相同。

子命令：``verify --manifest <清单> [--store <记录库>]``（重验一次运行的清单，退出码 0 通过、1 不通过）。
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

RECORD_SCHEMA = "unit-execution-record/v1"
MANIFEST_SCHEMA = "unit-execution-manifest/v1"
FULL_SET_PASS = "full-set-pass"
RE_EXECUTE = "re-execute"
# B5a-08：逐单元承接（合同见 unit_inherit.py）。清单另引用决策收据，决策收据放在记录库的 unit-inherit 目录。
UNIT_INHERIT = "unit-inherit"
MODES = (FULL_SET_PASS, RE_EXECUTE, UNIT_INHERIT)
MODE_LABELS = {FULL_SET_PASS: "全集通过", RE_EXECUTE: "重新执行全集", UNIT_INHERIT: "逐单元承接"}
INHERITING_MODES = frozenset({FULL_SET_PASS, UNIT_INHERIT})
UNIT_INHERIT_DIR = "unit-inherit"
UNIT_INHERIT_DECISION_SCHEMA = "unit-inherit-decision/v1"
UNIT_INHERIT_CHECKPOINTS = ("queue", "lock", "publish")
# 承接期限：默认 7 天（方案）；命令行只能调小，不能放宽。
MAX_AGE_HOURS = 168.0
PASSING_OUTCOMES = frozenset({"passed", "skipped", "expected_failure"})
MISSING = "missing"
HERE = Path(__file__).resolve().parent
# 执行器版本：执行器目录里参与调度、判定、输入解析与门禁编排的文件（存在的才算；字节码预编译工具另由执行器传入）。
# 审计结论影响记录是否通过及覆盖是否闭合，审计合同必须进入执行器身份。
EXECUTOR_FILES = ("unit_executor.py", "unit_records.py", "read_audit.py", "read_audit_runtime.py", "entry_steps.py", "entry_gates.py", "entry-gates.sh", "full_set_receipt.py", "target_platform_gate.py", "unit_inherit.py")
# 只决定缓存放在哪里的环境变量（内容按源码摘要校验或按整树摘要做键），不进环境指纹；执行器自己的控制变量也不进。
ENV_CACHE_ONLY = frozenset({"PYTHONPYCACHEPREFIX", "CODEX_UPGRADE_IDENTITY_MEMO"})
ENV_EXECUTOR_PREFIX = "UNIT_EXECUTOR_"
# 命令单元的输入范围（E2-05 从宽：整个仓库＝受管工具树＋测试目录＋docs＋其余部分）。
COMMAND_RANGES = ("managed", "tests", "docs", "rest")


class RecordsError(RuntimeError):
    """记录库、清单或输入解析错误。"""


def _audit_module():
    name = "unit_records_read_audit"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, HERE / "read_audit.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


# ---------------------------------------------------------------------------
# 摘要与时间
# ---------------------------------------------------------------------------


def canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_bytes(canonical(value))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def path_digest(path: Path) -> str | None:
    """文件内容摘要；符号链接记指向（不跟进去）；不存在或不是普通文件返回 None。"""

    path = Path(path)
    if path.is_symlink():
        return sha256_bytes(b"L\0" + os.readlink(path).encode("utf-8"))
    if path.is_file():
        return file_sha256(path)
    return None


def seal(payload: Mapping[str, Any], key: str) -> dict[str, Any]:
    body = {name: value for name, value in payload.items() if name != key}
    return {**body, key: sha256_json(body)}


def seal_ok(payload: Mapping[str, Any], key: str) -> bool:
    body = {name: value for name, value in payload.items() if name != key}
    return payload.get(key) == sha256_json(body)


def entries_sha256(entries: Iterable[Mapping[str, Any]]) -> str:
    """输入或环境明细的总摘要：排序后的（名字, 摘要）对，与 entry_steps.inputs_sha256 同一算法。"""

    return sha256_json(sorted([str(entry["name"]), str(entry["sha256"])] for entry in entries))


def value_entry(category: str, name: str, value: str, **detail: Any) -> dict[str, Any]:
    return {"category": category, "name": name, "sha256": sha256_bytes(f"{name}={value}".encode("utf-8")),
            "detail": {"value": value[:300], **detail}}


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_utc(text: Any) -> float | None:
    try:
        return datetime.strptime(str(text), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def new_run_id() -> str:
    return f"{time.strftime('%Y%m%dt%H%M%Sz', time.gmtime())}-{os.getpid()}-{secrets.token_hex(3)}"


def _span(hours: float) -> str:
    """时长的可读写法：两小时以内按分钟，否则按小时。"""

    return f"{hours * 60:.1f} 分钟" if hours < 2 else f"{hours:.1f} 小时"


def _diff_names(left: Sequence[Mapping[str, Any]] | None, right: Sequence[Mapping[str, Any]] | None) -> list[str]:
    """两份明细里摘要不同（或只在一边出现）的项名。"""

    old = {str(entry.get("name")): entry.get("sha256") for entry in left or [] if isinstance(entry, Mapping)}
    new = {str(entry.get("name")): entry.get("sha256") for entry in right or [] if isinstance(entry, Mapping)}
    return sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))


# ---------------------------------------------------------------------------
# 仓库内容索引与输入范围
# ---------------------------------------------------------------------------


def _git(root: Path, *args: str) -> bytes:
    # 输入核验保持只读，避免 status 的可选索引刷新让已绑定的 .git/index 快照失效。
    completed = subprocess.run(["git", "--no-optional-locks", "-C", str(root), *args], capture_output=True, stdin=subprocess.DEVNULL, timeout=300)
    if completed.returncode != 0:
        raise RecordsError(f"git {' '.join(args)} 失败：{completed.stderr.decode('utf-8', 'replace').strip()[-300:]}")
    return completed.stdout


def standard_ranges(occ: str) -> dict[str, dict[str, Any]]:
    """标准输入范围（仓库相对路径前缀，目录以 / 结尾，空串＝整个仓库）：``occ`` 是受管工具树（测试组起点的上一级）。"""

    occ = occ.strip("/") + "/"
    tests = f"{occ}tests/"
    return {
        "managed": {"category": "managed", "name": "repo:managed", "include": [occ], "exclude": [tests]},
        "tests": {"category": "tests", "name": "repo:tests", "include": [tests], "exclude": []},
        "tests-fixtures": {"category": "tests", "name": "repo:tests-fixtures", "include": [f"{tests}fixtures/"], "exclude": []},
        "tests-real-chains": {"category": "tests", "name": "repo:tests-real-chains", "include": [f"{tests}real_chains/"], "exclude": []},
        "docs": {"category": "docs", "name": "repo:docs", "include": ["docs/"], "exclude": []},
        "rest": {"category": "tests", "name": "repo:rest", "include": [""], "exclude": [occ, "docs/"]},
    }


def _matches(path: str, prefix: str) -> bool:
    if prefix == "":
        return True
    return path.startswith(prefix) if prefix.endswith("/") else path == prefix


class RepoIndex:
    """仓库跟踪文件的内容索引：``git ls-files`` 列出跟踪文件，逐个算内容 sha256（符号链接记指向）；同一次运行只算一遍。

    ``dirty`` 是 ``git status --porcelain --untracked-files=all`` 的输出：不干净时输入不完整（未跟踪文件不在范围里），
    调用方据此判本次不承接。"""

    def __init__(self, root: Path, head: str, files: dict[str, str], dirty: list[str]) -> None:
        self.root = root
        self.head = head
        self.files = files
        self.dirty = dirty
        self._ranges: dict[tuple[tuple[str, ...], tuple[str, ...]], tuple[str, int]] = {}

    @classmethod
    def load(cls, start: Path) -> "RepoIndex":
        top = Path(_git(Path(start), "rev-parse", "--show-toplevel").decode("utf-8").strip()).resolve()
        head = _git(top, "rev-parse", "HEAD").decode("ascii").strip()
        listing = [item.decode("utf-8") for item in _git(top, "ls-files", "-z").split(b"\0") if item]
        status = [item.decode("utf-8", "replace") for item in _git(top, "status", "--porcelain=v1", "-z", "--untracked-files=all").split(b"\0") if item]
        files = {relative: path_digest(top / relative) or MISSING for relative in sorted(set(listing))}
        return cls(top, head, files, status)

    @property
    def clean(self) -> bool:
        return not self.dirty

    def relative(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()

    def range_digest(self, include: Sequence[str], exclude: Sequence[str]) -> tuple[str, int]:
        key = (tuple(include), tuple(exclude))
        if key not in self._ranges:
            total, count = hashlib.sha256(), 0
            for relative, digest in self.files.items():
                if any(_matches(relative, prefix) for prefix in include) and not any(_matches(relative, prefix) for prefix in exclude):
                    total.update(f"{relative}\0{digest}\n".encode("utf-8"))
                    count += 1
            self._ranges[key] = (total.hexdigest(), count)
        return self._ranges[key]

    def range_entry(self, spec: Mapping[str, Any]) -> dict[str, Any]:
        include, exclude = list(spec.get("include") or []), list(spec.get("exclude") or [])
        if not include or not all(isinstance(item, str) and not item.startswith("/") and ".." not in item.split("/") for item in include + exclude):
            raise RecordsError(f"输入范围非法：{spec!r}")
        digest, count = self.range_digest(include, exclude)
        return {"category": str(spec["category"]), "name": str(spec["name"]), "sha256": digest,
                "detail": {"include": include, "exclude": exclude, "files": count}}

    def file_entry(self, category: str, path: Path) -> dict[str, Any]:
        relative = self.relative(path)
        digest = self.files.get(relative) or path_digest(self.root / relative) or MISSING
        return {"category": category, "name": f"file:{relative}", "sha256": digest, "detail": {"path": relative}}

    def head_entry(self) -> dict[str, Any]:
        return value_entry("tests", "head", self.head, note="HEAD 提交（读真实仓库 git 历史的单元）")


# ---------------------------------------------------------------------------
# 采集工具测试的静态依赖闭包
# ---------------------------------------------------------------------------

# 人工核查确认的「整目录读取测试目录」的模块（静态依赖覆盖不到：子进程 discover、整树复制、按冻结清单或场景表在运行时
# 读取、导入测试文件）：模块名 → 原因。依据 10-02 对 HEAD 34ae7c998 的逐模块静态核查与实跑追踪（审计钩子记录对测试目录
# 的列举、读取、复制）。自动识别（discover 调用、copy_managed_tree 没显式传 include_tests=False）之外的补充；以后新增
# 的漏判由读集审计（E3-04）兜底。只复制夹具目录的（test_codex_upgrade_arm64_environment_receipt）不在其中：夹具目录本就
# 是每个采集单元的输入。
WHOLE_TESTS_READERS: dict[str, str] = {
    "test_ci_unit_executor": "子进程 unit_executor.py plan 对整个测试目录做 discover（实跑导入 186 个测试模块）",
    "test_dry_run": "shutil.copytree 复制整棵受管工具树，连测试目录一起",
    "test_codex_0151_worktree_successor": "按冻结清单 codex-cli-0151-worktree-successor.json 逐个读文件，清单里有测试文件",
    "test_arm64_supervised_deploy": "按 pre-A3 场景表读取、复制测试模块（含真实链）",
    "test_codex_upgrade_pre_a3_certification": "按 pre-A3 认证模块的场景表在运行时导入并执行测试模块",
    "test_claude_fw_e_complete_campaign": "本地忽略区有证据数据时 freeze_campaign 整树复制受管工具树（含测试目录）；没有数据时整类跳过，拿不准，从宽",
    "test_arm64_supervised_deploy_transaction": "在受管树副本上真实部署：shutil.copytree 复制两个受管根（含测试目录），pre-A3 入口核对读取测试模块",
    "test_driver_client_table": "按 pre-A3 场景单元的输入声明展开测试模块的静态依赖闭包（unit_records.declared_inputs 读测试目录里的模块源码），"
                                "核对第二受管根只让 Claude 场景单元失效（B5a-06）",
    "test_ci_unit_inherit": "同 test_driver_client_table：按两客户端 pre-A3 场景单元的输入声明展开静态依赖闭包（读测试目录里的模块源码），"
                            "核对只改第二受管根时 Claude 场景单元重跑、Codex 场景单元承接（B5a-08）",
}
# 人工核查确认的「读真实仓库 git 历史」的模块：模块名 → 原因（同一次核查；git 走 PATH 包装记录真实仓库上的调用）。
# 这些模块读历史提交、对象或祖先关系，结论可能随提交变，加 HEAD 输入。不做自动识别：测试里的 git 命令绝大多数作用在
# 测试自建的临时仓库上，按命令字符串识别会把它们都算进来。
GIT_READERS: dict[str, str] = {
    "test_codex_0151_worktree_successor": "对真实仓库 rev-parse、show、ls-tree、merge-base（读 HEAD、历史 blob 与祖先关系）",
    "test_producer_replay_registration_gate": "对真实仓库 ls-tree、cat-file、rev-parse（读历史对象）",
    "test_codex_upgrade_rollback_readback": "git archive 导出真实仓库历史提交的 tools 树",
    "test_claude_fw_g_acceptance": "经 claude_fw_g_acceptance 对真实仓库 ls-tree 历史提交",
    "test_codex_01491_terminal_state": "经 tools/check_ledger_completeness 对真实仓库 git log、git show",
    "test_arm64_supervised_deploy_codex_parity": "对真实仓库 git show 读 B5a-01 开工基线提交的部署脚本，与改造后逐字节对照 Codex 收据",
    "test_pre_a3_codex_parity": "对真实仓库 git show 读 B5a-04 开工基线提交的 pre-A3 认证模块与入口门禁，逐字对照 Codex 场景单元与输入声明",
    "test_driver_client_table_codex_parity": "对真实仓库 git show 读 B5a-06 开工基线提交的驱动与入口基础设施（参数解析、入口步骤与编排器、"
                                             "入口门禁、各驱动脚本），逐字对照 Codex 的派生、步骤判定与命令行",
    "test_unit_inherit_codex_parity": "对真实仓库 git archive／git show 读 B5a-08 开工基线提交的 tools/ci、P0 收据与 pre-A3 认证模块、入口门禁脚本，"
                                      "逐字对照 Codex 重新执行全集与 B-09 路径的产物、判定与源码残差",
}
# 读集审计（E3-04）实测、静态闭包漏掉的测试目录读取：模块名 → {测试目录相对路径: 原因}。来源都是受管模块之间的函数内
# 导入（静态闭包按设计不追，见 TestDependencies 的说明）：被测代码运行时延迟导入别的受管模块，后者在模块级导入测试目录
# 里的夹具。只登记审计实测读到的，以后再漏的由每次升级开工的入口空跑（带审计）报出来再补。
EXTRA_TEST_READS: dict[str, dict[str, str]] = {
    "test_codex_upgrade_campaign_run_rehearsal_receipt": {
        "project_ledger_fixture.py": "被测模块在函数内导入 codex_upgrade_vc0_closeout，其模块级导入链经 pre-A3 认证模块到这个夹具"
                                     "（10-02 读集审计实测读到）",
    },
    "test_codex_upgrade_main_module": {
        "project_ledger_fixture.py": "子进程以主程序运行编排器，延迟导入 vc0_closeout 等模块，导入链同上（10-02 读集审计实测读到）",
    },
}
# 只读 HEAD 提交号、结论不随提交变的模块（10-02 老板定：按实跑核查的证据收窄）：它们只经
# codex_upgrade._tool_identity()（默认 include_git）执行 git rev-parse HEAD，提交号只写进同一次运行里生成、比对的工具
# 身份与收据；测试夹具里的提交号全是合成值，没有断言真实 HEAD。所以不加 HEAD 输入——否则每次提交这批最重的模块
# （test_codex_upgrade 4 块、attempt_recovery 6 块等）都要重跑，修测试后的补跑与全量差不多。登记在这里留作依据：
# 读集审计（E3-04）看到它们读 .git 时按这张表核对「只读 HEAD 与引用、不读对象」。
HEAD_ID_ONLY_READERS: dict[str, str] = {name: "经 codex_upgrade._tool_identity()（默认 include_git）只读 HEAD 提交号" for name in (
    "test_codex_upgrade", "test_certify_release", "test_codex_upgrade_job_rehearsal_receipt", "test_codex_upgrade_vc0_closeout",
    "test_codex_upgrade_pre_a3_certification", "test_codex_upgrade_campaign_resume", "test_codex_upgrade_candidate_revision",
    "test_codex_upgrade_candidate_stage_replay", "test_codex_upgrade_evaluation_baseline", "test_codex_upgrade_evaluation_recovery",
    "test_codex_upgrade_evaluation_attempt_recovery", "test_codex_upgrade_evidence_integrity", "test_codex_upgrade_stage_recovery",
    "test_codex_upgrade_stage_replay_binding", "test_codex_upgrade_staging_dispatch")}


@dataclass
class Flags:
    whole_tests: list[str] = field(default_factory=list)
    git: list[str] = field(default_factory=list)


def _import_names(node: ast.AST) -> list[str]:
    """一条 import 语句可能指向的模块全名（``from a import b`` 记 ``a`` 与 ``a.b``；相对导入去掉点）。"""

    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom):
        base = node.module or ""
        return ([base] if base else []) + [f"{base}.{alias.name}" if base else alias.name for alias in node.names]
    return []


class TestDependencies:
    """静态依赖闭包：节点是受管工具树（``occ``，含测试目录与真实链子目录）里的每个 ``.py``。

    * 测试目录文件：源码文本里以整词出现的模块名都连边——测试目录模块与受管模块都算（覆盖各种 import、字符串里的子进程
      脚本、``__import__``、``-m`` 与按路径加载）；
    * 受管模块：只按 import 语句连边——模块级导入连到受管模块与测试模块（导入它就会一起加载的，例如 pre-A3 认证模块
      在模块级导入 project_ledger_fixture），函数内导入只连到测试模块（pre-A3 认证、零请求 smoke 模块在函数里按名字导入
      测试）。受管模块之间的函数内导入不追：编排器只在函数里才碰到 pre-A3 认证模块，追进去几乎每个测试都会连到
      test_codex_upgrade，任何测试文件一改全体重跑；这类间接读取靠人工核查名单与读集审计（E3-04）兜底。

    闭包里只有测试目录文件作输入（受管工具树整体另是一项输入）。整目录读取的自动识别只看模块自身与闭包里的辅助模块
    （测试目录里不以 test_ 开头的模块）：闭包里的别的测试模块、真实链只是被引用（导入里面的类或函数），它们自己的
    测试会不会复制整棵树与本模块无关——10-02 实跑核查里，闭包含真实链的 4 个评估类模块都没有读整个测试目录。"""

    def __init__(self, occ: Path) -> None:
        self.occ = Path(occ).resolve()
        self.tests_dir = self.occ / "tests"
        self.files = sorted(path for path in self.occ.rglob("*.py") if "__pycache__" not in path.parts)
        self.test_stems: dict[str, list[Path]] = {}
        self.managed_stems: dict[str, list[Path]] = {}
        for path in self.files:
            if path.stem != "__init__":
                (self.test_stems if self.is_test_file(path) else self.managed_stems).setdefault(path.stem, []).append(path)
        names = sorted(set(self.test_stems) | set(self.managed_stems), key=len, reverse=True)
        self.pattern = re.compile(r"\b(" + "|".join(re.escape(name) for name in names) + r")\b") if names else None
        self._text: dict[Path, str] = {}
        self._direct: dict[Path, frozenset[Path]] = {}
        self._auto: dict[Path, Flags] = {}

    def is_test_file(self, path: Path) -> bool:
        return self.tests_dir in Path(path).resolve().parents

    def text(self, path: Path) -> str:
        if path not in self._text:
            try:
                self._text[path] = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                self._text[path] = ""
        return self._text[path]

    def _resolve(self, dotted: str) -> list[Path]:
        parts = [part for part in dotted.split(".") if part]
        if not parts:
            return []
        if "tests" in parts:
            rest = parts[parts.index("tests") + 1:]
            return list(self.test_stems.get(rest[0], [])) if rest else []
        for candidate in (parts[-1], parts[-2] if len(parts) > 1 else ""):
            if candidate in self.managed_stems:
                return list(self.managed_stems[candidate])
        # 裸名导入测试模块（测试目录在导入路径上时）。
        return list(self.test_stems.get(parts[0], [])) if len(parts) == 1 else []

    def direct(self, path: Path) -> frozenset[Path]:
        if path not in self._direct:
            found: set[Path] = set()
            if self.is_test_file(path):
                if self.pattern is not None:
                    for name in set(self.pattern.findall(self.text(path))):
                        found.update(self.test_stems.get(name, []))
                        found.update(self.managed_stems.get(name, []))
            else:
                try:
                    tree = ast.parse(self.text(path))
                except (SyntaxError, ValueError):
                    tree = None
                if tree is not None:
                    top = {name for node in tree.body for name in _import_names(node)}
                    nested = {name for node in ast.walk(tree) for name in _import_names(node)} - top
                    found.update(target for name in top for target in self._resolve(name))
                    found.update(target for name in nested for target in self._resolve(name) if self.is_test_file(target))
            found.discard(path)
            self._direct[path] = frozenset(found)
        return self._direct[path]

    def closure(self, path: Path) -> list[Path]:
        """模块的闭包里的测试目录文件（含自身）。"""

        path = Path(path).resolve()
        seen, stack = {path}, [path]
        while stack:
            for nxt in self.direct(stack.pop()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        return sorted(item for item in seen if self.is_test_file(item))

    def auto_flags(self, path: Path) -> Flags:
        if path not in self._auto:
            flags = Flags()
            try:
                tree = ast.parse(self.text(path))
            except (SyntaxError, ValueError):
                flags.whole_tests.append(f"{path.name} 源码解析失败，从宽")
                flags.git.append(f"{path.name} 源码解析失败，从宽")
                self._auto[path] = flags
                return flags
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ""
                if name == "discover":
                    flags.whole_tests.append(f"{path.name}:{node.lineno} 调用 discover")
                elif name == "copy_managed_tree" and not any(
                        keyword.arg == "include_tests" and isinstance(keyword.value, ast.Constant) and keyword.value.value in (False, "closure")
                        for keyword in node.keywords):
                    # include_tests="closure" 只复制调用方模块的静态依赖闭包、辅助模块、夹具与真实链目录（E3-04）：闭包里有真实链
                    # 的单元本就声明了这些。
                    flags.whole_tests.append(f"{path.name}:{node.lineno} copy_managed_tree 没显式传 include_tests=False 或 \"closure\""
                                             "（默认连测试目录一起复制）")
            self._auto[path] = flags
        return self._auto[path]

    def module_flags(self, module_file: Path) -> Flags:
        """模块的整目录读取与读 git 标记：模块自身与闭包里的辅助模块自动识别到的，加上人工核查名单。"""

        merged = Flags()
        module_file = Path(module_file).resolve()
        for path in self.closure(module_file):
            if path != module_file and path.name.startswith("test_"):
                continue
            flags = self.auto_flags(path)
            merged.whole_tests.extend(flags.whole_tests)
            merged.git.extend(flags.git)
        stem = Path(module_file).stem
        if stem in WHOLE_TESTS_READERS:
            merged.whole_tests.append(f"人工核查：{WHOLE_TESTS_READERS[stem]}")
        if stem in GIT_READERS:
            merged.git.append(f"人工核查：{GIT_READERS[stem]}")
        return merged


def _closure_entries(repo: RepoIndex, deps: TestDependencies, module_file: Path, *, git: bool) -> list[dict[str, Any]]:
    """测试模块的闭包输入：闭包里的测试文件、读集审计实测补登的文件（``EXTRA_TEST_READS``）与 ``tests/__init__.py``；
    碰到真实链另加整个 helper 与真实链目录；整目录读取的另加整个测试目录；``git`` 为真时读 git 历史的另加 HEAD。"""

    ranges = standard_ranges(repo.relative(deps.occ))
    entries: list[dict[str, Any]] = []
    tests = deps.closure(module_file)
    # 读集审计实测、静态闭包漏掉的读取（EXTRA_TEST_READS）。
    tests.extend(deps.tests_dir / relative for relative in EXTRA_TEST_READS.get(Path(module_file).stem, {})
                 if (deps.tests_dir / relative).is_file())
    init = deps.tests_dir / "__init__.py"
    if init.exists():
        tests.append(init)
    if any("real_chains" in path.relative_to(deps.tests_dir).parts for path in tests):
        # 真实链另加整个 helper 与真实链目录（方案 E3-01）：辅助模块＝测试目录顶层不以 test_ 开头的模块。
        entries.append(repo.range_entry(ranges["tests-real-chains"]))
        tests.extend(path for path in deps.tests_dir.glob("*.py") if not path.name.startswith("test_"))
    for path in sorted(set(tests)):
        entries.append(repo.file_entry("tests", path))
    flags = deps.module_flags(module_file)
    if flags.whole_tests:
        entry = repo.range_entry(ranges["tests"])
        entry["detail"]["reasons"] = sorted(set(flags.whole_tests))[:20]
        entries.append(entry)
    if git and flags.git:
        entry = repo.head_entry()
        entry["detail"]["reasons"] = sorted(set(flags.git))[:20]
        entries.append(entry)
    return entries


def test_unit_inputs(repo: RepoIndex, deps: TestDependencies, module_file: Path) -> list[dict[str, Any]]:
    """采集工具测试单元的输入明细（见模块说明）。"""

    ranges = standard_ranges(repo.relative(deps.occ))
    entries = [repo.range_entry(ranges[key]) for key in ("managed", "tests-fixtures", "docs", "rest")]
    entries.extend(_closure_entries(repo, deps, module_file, git=True))
    return _unique(entries)


def _repo_file(repo: RepoIndex, relative: Any) -> Path:
    if (not isinstance(relative, str) or not relative or relative.startswith("/") or "\\" in relative
            or any(part in ("", ".", "..") for part in relative.split("/"))):
        raise RecordsError(f"仓库相对路径非法：{relative!r}")
    return repo.root / relative


def declared_inputs(repo: RepoIndex, declaration: Mapping[str, Any],
                    deps_cache: dict[Path, TestDependencies] | None = None) -> list[dict[str, Any]]:
    """门禁清单为命令或已登记测试单元声明的输入：

    * ``ranges``：仓库范围；``files``：仓库里的单个文件（``{category, path}``）；``head``：HEAD 提交；
    * ``test_modules``：测试模块文件（仓库相对路径），按与采集测试单元相同的静态依赖闭包展开（E3-02 的 pre-A3 场景
      单元用），不加 HEAD——场景在数据根运行，读不到 git；受管工具树由模块所在的 ``tests`` 目录的上一级确定，
      同一受管树的依赖分析经 ``deps_cache`` 共用；
    * ``resolved``：清单生成时已算好的明细（例如数据根才有的内容）。"""

    # 声明级承接（declared-inputs/v1）：声明里带 declared_scope 键就走专门分支；其它声明（含 control 四链的）一字不变。
    if isinstance(declaration, Mapping) and "declared_scope" in declaration:
        return _declared_scope_inputs(repo, declaration)

    allowed = {"ranges", "files", "test_modules", "head", "resolved", "host_paths", "require_read_audit", "runtime_contract",
               "metadata_dependencies"}
    if not isinstance(declaration, Mapping) or set(declaration) - allowed:
        raise RecordsError(f"输入声明非法：{declaration!r}")
    paths = declaration.get("host_paths", [])
    if (not isinstance(paths, list) or not all(isinstance(path, str) for path in paths)
            or len(paths) != len(set(paths)) or not isinstance(declaration.get("require_read_audit", False), bool)):
        raise RecordsError("宿主路径须为无重复的数组，完整审计要求须为布尔值")
    dependencies = (validate_metadata_dependencies(declaration["metadata_dependencies"], paths)
                    if "metadata_dependencies" in declaration else {})
    entries = [repo.range_entry(spec) for spec in declaration.get("ranges") or []]
    for path in paths:
        entries.append(new_host_snapshot(path, dependencies.get(path)))
    if "runtime_contract" in declaration:
        try:
            entries.append(_audit_module()._runtime_module().contract_entry(declaration["runtime_contract"]))
        except (RuntimeError, OSError, ValueError) as error:
            raise RecordsError(str(error)) from error
    if paths or declaration.get("require_read_audit") or "runtime_contract" in declaration:
        entries.append(value_entry("policy", "require-read-audit", "all-file-paths/v1"))
    for item in declaration.get("files") or []:
        if not isinstance(item, Mapping) or not isinstance(item.get("category"), str) or set(item) != {"category", "path"}:
            raise RecordsError(f"文件输入声明非法：{item!r}")
        entries.append(repo.file_entry(item["category"], _repo_file(repo, item["path"])))
    cache = deps_cache if deps_cache is not None else {}
    for relative in declaration.get("test_modules") or []:
        module_file = _repo_file(repo, relative).resolve()
        occ = next((parent.parent for parent in module_file.parents if parent.name == "tests"), None)
        if occ is None or not module_file.is_file():
            raise RecordsError(f"测试模块不存在或不在 tests 目录下：{relative}")
        if occ not in cache:
            cache[occ] = TestDependencies(occ)
        entries.extend(_closure_entries(repo, cache[occ], module_file, git=False))
    if declaration.get("head"):
        entries.append(repo.head_entry())
    for entry in declaration.get("resolved") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("sha256"), str):
            raise RecordsError(f"输入明细非法：{entry!r}")
        entries.append(dict(entry))
    if not entries:
        raise RecordsError("输入声明为空")
    return _unique(entries)


def validate_audit_policy(policy: Any, unit_ids: set[str]) -> dict[str, Any]:
    """只登记本轮真实规划的命令与测试单元；未登记单元只能重跑，未知单元拒绝。

    每个登记单元的键集合是 ``host_paths``、``require_read_audit``、``runtime_contract`` 三个必备键，外加一个可选键
    ``metadata_dependencies``（宿主快照 v3 的元数据依赖声明，见 ``validate_metadata_dependencies``）。"""
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "default", "units"}
            or policy.get("schema_version") != "unit-audit-policy/v1" or policy.get("default") != "reexecute-only"
            or not isinstance(policy.get("units"), dict) or set(policy["units"]) - unit_ids):
        raise RecordsError("读集策略须默认重跑，且只能登记当前真实命令或测试单元")
    for unit_id, declaration in policy["units"].items():
        # 三个必备键之外只允许一个可选键 metadata_dependencies（宿主快照 v3 的元数据依赖声明）；缺省时与过去完全相同。
        if (not isinstance(declaration, dict)
                or set(declaration) - {"metadata_dependencies"} != {"host_paths", "require_read_audit", "runtime_contract"}
                or declaration["require_read_audit"] is not True):
            raise RecordsError(f"单元 {unit_id} 必须登记精确输入、隔离合同并要求完整审计")
        paths = declaration["host_paths"]
        if (not isinstance(paths, list) or not all(isinstance(path, str) and path.startswith("/")
                and str(Path(path)) == path and ".." not in Path(path).parts for path in paths) or len(paths) != len(set(paths))):
            raise RecordsError("登记的宿主输入不是唯一的规范绝对路径")
        try:
            _audit_module()._runtime_module().validate_contract(declaration["runtime_contract"])
        except RuntimeError as error:
            raise RecordsError(str(error)) from error
        if "metadata_dependencies" in declaration:
            validate_metadata_dependencies(declaration["metadata_dependencies"], paths)
    return policy


def validate_metadata_dependencies(value: Any, host_paths: Sequence[str]) -> dict[str, dict[str, Any]]:
    """审计策略里一个单元的元数据依赖声明 ``[{path, fields, basis}]`` 的校验，返回（路径 → 规范化的 ``{fields, basis}``）。

    宿主快照 v3 默认不比较 atime／mtime／ctime／inode／device（路径级轨迹分不清程序用了 stat 的哪个字段，见
    read_audit 的 v3 一节）；单元确实依赖其中某项时必须在这里逐路径声明：path 须是该单元 ``host_paths`` 里的路径且不重复，
    fields 是上述五项的非空无重复子集，basis 是非空字符串（源码位置或调用合同的审核依据，没有依据不得声明）。"""

    if not isinstance(value, list):
        raise RecordsError("元数据依赖声明须是数组")
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "fields", "basis"}:
            raise RecordsError("元数据依赖声明的每一项须恰好含 path、fields、basis")
        path = item["path"]
        if not isinstance(path, str) or path not in host_paths:
            raise RecordsError("元数据依赖声明的路径必须出现在该单元的 host_paths 里")
        if path in result:
            raise RecordsError("同一路径的元数据依赖声明不能重复")
        try:
            result[path] = _audit_module().normalize_metadata_dependency({"fields": item["fields"], "basis": item["basis"]})
        except RuntimeError as error:
            raise RecordsError(f"元数据依赖声明非法（{path}）：{error}") from error
    return result


def _control_adapter(audit: Any) -> bool:
    """审计模块是不是 control 四链的适配器：适配器（control_read_audit.py）复制 read_audit 的全部名字，另带
    ``CONTROL_UNIT_IDS`` 与 ``sidecar_paths``；普通的 read_audit 模块没有这两个名字。"""

    return hasattr(audit, "CONTROL_UNIT_IDS") and hasattr(audit, "sidecar_paths")


def new_host_snapshot(path: str, dependency: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """计划期新取一份宿主快照。普通审计模块取 v3（按单元实际访问方式比较，噪声不再让单元作废）；选中 control 适配器的
    整次运行保持 v2：control 四链的 sidecar 只认 v2，它们继续产出并重放 v2（即使这次运行里还有别的单元，也宁可统一
    保守）。v2 没有“元数据依赖”这个语义，control 运行里出现声明直接拒绝。"""

    audit = _audit_module()
    if _control_adapter(audit):
        if dependency is not None:
            raise RecordsError("元数据依赖声明只适用于宿主快照 v3，control 四链运行不支持")
        return audit.host_snapshot(path)
    return audit.host_snapshot_v3(path, dependency)


def _unique(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_name: dict[str, dict[str, Any]] = {}
    for entry in entries:
        previous = by_name.get(entry["name"])
        if previous is not None and previous["sha256"] != entry["sha256"]:
            raise RecordsError(f"同名输入的摘要不一致：{entry['name']}")
        by_name[entry["name"]] = entry
    return [by_name[name] for name in sorted(by_name)]


# ---------------------------------------------------------------------------
# 环境指纹与执行器版本
# ---------------------------------------------------------------------------


def _distributions_digest() -> str:
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - 3.8 以上都有
        return "unavailable"
    items = sorted({f"{(dist.metadata.get('Name') or '').lower()}=={dist.version}" for dist in metadata.distributions()})
    return sha256_json(items)


def _system_packages_digest() -> str:
    try:
        completed = subprocess.run(["dpkg-query", "-W", "-f=${Package} ${Version} ${Architecture}\\n"], capture_output=True,
                                   stdin=subprocess.DEVNULL, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return "unavailable"
    if completed.returncode != 0:
        return "unavailable"
    return sha256_bytes(b"\n".join(sorted(completed.stdout.splitlines())))


def executor_environment(environ: Mapping[str, str]) -> list[dict[str, Any]]:
    """执行器自己能确定的环境事实，加上它的进程环境变量（单元继承的就是这一份）。"""

    facts = {
        "system": platform.system(),
        "arch": platform.machine(),
        "kernel": platform.release(),
        "python": f"{platform.python_implementation()} {platform.python_version()}",
        "python_build": sys.version,
        "python_distributions": _distributions_digest(),
        "system_packages": _system_packages_digest(),
        "euid": str(os.geteuid()),
        "hostname": socket.gethostname(),
        "cpu_count": str(os.cpu_count()),
    }
    entries = [value_entry("environment", f"executor:{name}", value) for name, value in facts.items()]
    for name in sorted(environ):
        if name in ENV_CACHE_ONLY or name.startswith(ENV_EXECUTOR_PREFIX):
            continue
        # 只记名字与值的摘要（代理变量等可能带凭据，值不落盘）。
        entries.append({"category": "environment", "name": f"envvar:{name}",
                        "sha256": sha256_bytes(f"{name}={environ[name]}".encode("utf-8")), "detail": {"name": name}})
    return entries


def merge_environment(own: list[dict[str, Any]], declared: Iterable[Any]) -> list[dict[str, Any]]:
    entries = list(own)
    for entry in declared or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str) or not isinstance(entry.get("sha256"), str):
            raise RecordsError(f"环境事实非法：{entry!r}")
        entries.append(dict(entry))
    return _unique(entries)


def executor_version(extra: Sequence[Path] = ()) -> dict[str, Any]:
    files: dict[str, str] = {}
    for path in [HERE / name for name in EXECUTOR_FILES] + [Path(item) for item in extra]:
        digest = path_digest(path) if path.exists() else None
        if digest is not None:
            files[path.name] = digest
    return {"files": files, "sha256": sha256_json(files)}


# ---------------------------------------------------------------------------
# 记录与记录库
# ---------------------------------------------------------------------------


def seal_record(body: Mapping[str, Any]) -> dict[str, Any]:
    return seal({**body, "schema_version": RECORD_SCHEMA}, "record_sha256")


def derived_pass(record: Mapping[str, Any]) -> tuple[bool, str]:
    """按记录的原始字段重新判定结论（不只看 passed 字段）。"""

    if record.get("exit_code") != 0 or record.get("signal") is not None or record.get("timed_out") is not False:
        return False, "退出状态不是成功"
    audit = record.get("read_audit")
    reexecute_only = (isinstance(audit, Mapping) and audit.get("status") == "reexecute_required"
                      and record.get("inheritable") is False and audit.get("coverage_complete") is False
                      and any(entry.get("name") == "reexecute-only-policy" and entry.get("sha256") == audit.get("reexecute_policy_sha256")
                              for entry in record.get("inputs") or []))
    if audit is not None and not reexecute_only and (not isinstance(audit, Mapping) or audit.get("undeclared_count") != 0 or audit.get("status") != "passed"):
        return False, "读集审计未通过"
    if record.get("unit_type") == "test":
        tests, ids = record.get("tests"), record.get("test_ids")
        if not isinstance(tests, dict) or not isinstance(ids, list) or len(ids) != len(set(ids)) or set(tests) != set(ids) or not ids:
            return False, "测试结果与测试 ID 集合不符"
        failing = [test_id for test_id, item in tests.items() if not isinstance(item, Mapping) or item.get("outcome") not in PASSING_OUTCOMES]
        if failing:
            return False, f"有 {len(failing)} 个测试没通过"
    elif record.get("unit_type") != "command":
        return False, "单元类型不认识"
    if record.get("passed") is not True:
        return False, "记录结论不是通过"
    return True, ""


def _bucket(unit_id: str) -> str:
    return sha256_bytes(unit_id.encode("utf-8"))[:16]


def _publish(directory: Path, name: str, data: bytes) -> Path:
    """按内容寻址发布：先写临时文件再 link 成正式名（已存在即同名同内容，读的一方核对摘要），内容只增不改。

    已有同名文件时刷新它的修改时间（续期）：同样的日志会被新记录再次引用，记录库清理（``entry_housekeeping``，E4-02）按
    修改时间判断新旧，移走后会复查修改时间，期间被续期的放回原处。恰好已被移走就照常重新写入。"""

    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    final = directory / name
    if final.exists():
        try:
            os.utime(final)
            return final
        except FileNotFoundError:
            pass
    temporary = directory / f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        try:
            os.link(temporary, final)
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    return final


class RecordStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        # 运行清单只增不改（O_EXCL 写入），同一次判定里按 run_id 缓存：几百个单元的承接都追到同一两份原运行清单。
        self._manifests: dict[str, dict[str, Any] | None] = {}

    def record_path(self, unit_id: str, record_sha256: str) -> Path:
        return self.root / "records" / _bucket(unit_id) / f"{record_sha256}.json"

    def log_path(self, digest: str) -> Path:
        return self.root / "logs" / f"{digest}.log"

    def manifest_path(self, run_id: str) -> Path:
        return self.root / "runs" / f"{run_id}.json"

    def decision_path(self, run_id: str) -> Path:
        """逐单元承接运行的决策收据（B5a-08）：与清单同一个运行 ID，只增不改。"""

        return self.root / UNIT_INHERIT_DIR / "decisions" / f"{run_id}.json"

    def put_record(self, record: Mapping[str, Any]) -> Path:
        if not seal_ok(record, "record_sha256"):
            raise RecordsError("记录自摘要不符，拒绝入库")
        data = (json.dumps(record, ensure_ascii=False, indent=1, sort_keys=True) + "\n").encode("utf-8")
        return _publish(self.root / "records" / _bucket(str(record["unit_id"])), f"{record['record_sha256']}.json", data)

    def put_log(self, path: Path, digest: str) -> Path:
        return _publish(self.root / "logs", f"{digest}.log", Path(path).read_bytes())

    def put_manifest(self, manifest: Mapping[str, Any]) -> Path:
        path = self.manifest_path(str(manifest["run_id"]))
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=1, sort_keys=True)
            handle.write("\n")
        self._manifests.pop(str(manifest["run_id"]), None)
        return path

    def candidates(self, unit_id: str) -> list[tuple[Path, dict[str, Any] | None]]:
        bucket = self.root / "records" / _bucket(unit_id)
        found: list[tuple[Path, dict[str, Any] | None]] = []
        if not bucket.is_dir():
            return found
        for path in sorted(bucket.glob("*.json")):
            record = _read_json(path)
            if record is None or record.get("unit_id") == unit_id:
                found.append((path, record))
        return found

    def manifest(self, run_id: str) -> dict[str, Any] | None:
        if not isinstance(run_id, str) or not re.fullmatch(r"[0-9A-Za-z._-]+", run_id):
            return None
        if self._manifests.get(run_id) is None:
            self._manifests[run_id] = _read_json(self.manifest_path(run_id))
        return self._manifests[run_id]


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        if Path(path).is_symlink():
            return None
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


# ---------------------------------------------------------------------------
# 承接判定
# ---------------------------------------------------------------------------


@dataclass
class Current:
    """本次运行里一个单元的当前事实。"""

    unit_id: str
    unit_type: str
    spec: dict[str, Any]
    spec_sha256: str
    inputs: list[dict[str, Any]] | None
    inputs_sha256: str | None
    inheritable: bool
    reason: str = ""


@dataclass
class RunFacts:
    policy_sha256: str
    environment: list[dict[str, Any]]
    environment_sha256: str
    executor: dict[str, Any]
    max_age_hours: float
    now: float
    require_read_audit: bool = False
    historical_read_audit: bool = False
    # 声明级承接的内部标记：头部包装递归调用 check_record 取通用问题时置位，防止无限递归；外部调用方不应设置。
    declared_route: bool = False


@dataclass
class Decision:
    record: dict[str, Any] | None = None
    record_path: Path | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def inherit(self) -> bool:
        return self.record is not None


def _listed_as_formal(manifest: Mapping[str, Any], unit_id: str, record_sha256: str) -> bool:
    return any(isinstance(entry, Mapping) and entry.get("unit_id") == unit_id and entry.get("disposition") == "executed"
               and entry.get("record_sha256") == record_sha256 for entry in manifest.get("units") or [])


def check_record(store: RecordStore, path: Path, record: dict[str, Any] | None, current: Current, facts: RunFacts,
                 *, thorough: bool = True) -> list[str]:
    """一条候选记录不能承接的原因（空列表＝可以承接）。便宜的核对在前；``thorough`` 为假时前面已有问题就不再做贵的
    核对（读原运行清单、重算日志摘要）。"""

    # 声明级承接：当前输入里有 declared-inputs-scope 条目的单元，读集判据换成声明覆盖（头部包装：递归调用本函数取通用问题）。
    if not getattr(facts, "declared_route", False) and _declared_scope_entry(current.inputs) is not None:
        return _check_declared_route(store, path, record, current, facts, thorough=thorough)

    if record is None:
        return ["记录读不出来"]
    problems: list[str] = []
    if record.get("schema_version") != RECORD_SCHEMA:
        problems.append("记录格式版本不对")
    if not seal_ok(record, "record_sha256") or Path(path).stem != record.get("record_sha256"):
        problems.append("记录自摘要不符（被改过）")
    if record.get("kind") != "formal":
        problems.append("诊断执行的记录永不承接")
    if record.get("inheritable") is not True:
        problems.append("来源记录明确不可承接")
    if record.get("unit_id") != current.unit_id or record.get("unit_type") != current.unit_type:
        problems.append("单元 ID 或类型不符")
    passed, why = derived_pass(record)
    if not passed:
        problems.append(f"该次执行没通过（{why}）")
    if record.get("spec_sha256") != current.spec_sha256:
        problems.append("单元规格变了（测试集合、命令、环境、额度、独占或超时）")
    if record.get("inputs_sha256") != current.inputs_sha256:
        changed = _diff_names(record.get("inputs"), current.inputs)
        problems.append(f"输入变了：{'、'.join(changed[:8]) or '（声明不同）'}{' 等' if len(changed) > 8 else ''}")
    if record.get("policy_sha256") != facts.policy_sha256:
        problems.append("调度策略版本变了")
    if record.get("environment_sha256") != facts.environment_sha256:
        changed = _diff_names(record.get("environment"), facts.environment)
        problems.append(f"环境变了：{'、'.join(changed[:8]) or '（明细不同）'}{' 等' if len(changed) > 8 else ''}")
    executor = record.get("executor") if isinstance(record.get("executor"), Mapping) else {}
    if executor.get("sha256") != facts.executor.get("sha256"):
        old, new = executor.get("files") or {}, facts.executor.get("files") or {}
        changed = sorted(name for name in set(old) | set(new) if old.get(name) != new.get(name))
        problems.append(f"执行器变了：{'、'.join(changed) or '（版本不同）'}")
    required_audit = facts.require_read_audit or any(entry.get("name") == "require-read-audit" for entry in current.inputs or [])
    if required_audit:
        audit = record.get("read_audit") or {}
        if (audit.get("coverage_complete") is not True or audit.get("coverage_scope") != "all-file-paths/v1"
                or audit.get("inputs_sha256") != record.get("inputs_sha256")):
            problems.append("读集覆盖未闭合，不能承接")
        else:
            trace = audit.get("trace") or {}
            digest = str(trace.get("sha256") or "")
            stored_trace = store.log_path(digest) if re.fullmatch(r"[0-9a-f]{64}", digest) else None
            try:
                if stored_trace is None or not stored_trace.is_file() or file_sha256(stored_trace) != digest:
                    raise RecordsError("完整读集轨迹缺失或摘要漂移")
                trace_document = json.loads(stored_trace.read_text())
                replay = _audit_module().strict_audit_reads(trace_document, current.inputs,
                    repo_root=str(audit.get("repo_root") or "/unbound-repo"), data_root=audit.get("data_root"),
                    historical=facts.historical_read_audit)
                if not replay["coverage_complete"]:
                    raise RecordsError("完整读集轨迹不能按当前输入重放")
                # 宿主快照 v3 的条目摘要只绑无条件比较集：来源记录观察过的内容／目录项在这里按它的访问方式补比，封存的访问类型
                # 也在这里与轨迹交叉核对；没有 v3 条目（v2 记录）时返回空，行为与过去相同。
                problems.extend(_audit_module().host_inputs_inherit_problems(trace_document, record.get("inputs"), current.inputs, audit))
            except (OSError, ValueError, RuntimeError) as error:
                problems.append(str(error))
    completed = parse_utc(record.get("completed_at_utc"))
    if completed is None:
        problems.append("完成时间不可读")
    else:
        age_hours = (facts.now - completed) / 3600
        if age_hours > facts.max_age_hours:
            problems.append(f"超过承接期限（{_span(age_hours)}前完成，期限 {_span(facts.max_age_hours)}）")
        elif age_hours < -0.1:
            problems.append("完成时间在未来")
    if problems and not thorough:
        return problems
    run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
    origin = store.manifest(str(run.get("run_id")))
    if origin is None or not seal_ok(origin, "manifest_sha256") or origin.get("schema_version") != MANIFEST_SCHEMA:
        problems.append("原运行的清单缺失或自摘要不符（那次运行没有正常结束或清单被改过）")
    elif not _listed_as_formal(origin, current.unit_id, str(record.get("record_sha256"))):
        problems.append("原运行的清单没有把这条记录列为该单元的正式执行")
    log = record.get("log") if isinstance(record.get("log"), Mapping) else {}
    digest = str(log.get("sha256") or "")
    stored = store.log_path(digest) if re.fullmatch(r"[0-9a-f]{64}", digest) else None
    if stored is None or not stored.is_file() or file_sha256(stored) != digest:
        problems.append("日志不在记录库里或摘要不符")
    return problems


def evaluate(store: RecordStore, current: Current, facts: RunFacts) -> Decision:
    """在记录库里给一个单元找可承接的记录：可承接的取完成时间最新的一条；没有时写出最近一条正式记录不能承接的原因。"""

    if not current.inheritable:
        return Decision(reasons=[current.reason or "本单元不可承接"])
    candidates = store.candidates(current.unit_id)
    if not candidates:
        return Decision(reasons=["记录库里没有这个单元的记录"])

    def completed(item: tuple[Path, dict[str, Any] | None]) -> tuple[float, str]:
        record = item[1] or {}
        return (parse_utc(record.get("completed_at_utc")) or 0.0, item[0].name)

    ordered = sorted(candidates, key=completed, reverse=True)
    for path, record in ordered:
        if not check_record(store, path, record, current, facts, thorough=False) and not check_record(store, path, record, current, facts):
            return Decision(record=record, record_path=path, reasons=[])
    formal = [item for item in ordered if (item[1] or {}).get("kind") == "formal"] or ordered
    path, record = formal[0]
    reasons = check_record(store, path, record, current, facts)
    return Decision(reasons=[f"最近一条记录（{(record or {}).get('completed_at_utc', '时间不明')}）不能承接：{'；'.join(reasons)}"])


# ---------------------------------------------------------------------------
# 清单与自检
# ---------------------------------------------------------------------------


def build_manifest(**fields: Any) -> dict[str, Any]:
    return seal({"schema_version": MANIFEST_SCHEMA, **fields}, "manifest_sha256")


def verify_manifest(manifest: Mapping[str, Any], *, store: RecordStore | None, historical: bool = False) -> list[str]:
    """清单自检（见模块说明）：返回问题列表，空＝通过。"""

    problems: list[str] = []
    if manifest.get("schema_version") != MANIFEST_SCHEMA:
        return ["清单格式版本不对"]
    if not seal_ok(manifest, "manifest_sha256"):
        problems.append("清单自摘要不符（被改过）")
    mode = manifest.get("mode")
    if mode not in MODES:
        problems.append(f"模式不认识：{mode!r}")
    planned = manifest.get("planned_units")
    entries = manifest.get("units")
    if not isinstance(planned, list) or not isinstance(entries, list):
        return problems + ["清单缺少规划单元或单元列表"]
    ids = [entry.get("unit_id") if isinstance(entry, Mapping) else None for entry in entries]
    duplicated = sorted({str(unit_id) for unit_id in ids if ids.count(unit_id) > 1})
    if duplicated:
        problems.append(f"单元重复出现：{duplicated[:5]}")
    missing = sorted(set(planned) - set(ids))
    extra = sorted(str(unit_id) for unit_id in set(ids) - set(planned))
    if missing or extra:
        problems.append(f"执行＋承接≠全集：缺 {missing[:5]}，多 {extra[:5]}")
    facts = RunFacts(policy_sha256=str(manifest.get("policy_sha256")), environment=list(manifest.get("environment") or []),
                     environment_sha256=str(manifest.get("environment_sha256")), executor=dict(manifest.get("executor") or {}),
                     max_age_hours=float(manifest.get("inheritance_max_age_hours") or 0),
                     now=parse_utc(manifest.get("decided_at_utc")) or 0.0, historical_read_audit=historical,
                     require_read_audit=mode == UNIT_INHERIT)
    if facts.environment_sha256 != entries_sha256(facts.environment):
        problems.append("清单的环境指纹与明细不符")
    if facts.max_age_hours > MAX_AGE_HOURS:
        problems.append(f"承接期限超过 {MAX_AGE_HOURS:g} 小时")
    group_tests: dict[str, list[str]] = {}
    for entry in entries:
        if not isinstance(entry, Mapping):
            problems.append("单元项格式非法")
            continue
        unit_id, disposition = str(entry.get("unit_id")), entry.get("disposition")
        if disposition not in ("executed", "inherited"):
            problems.append(f"{unit_id}：处置只能是本次执行或承接，实际是 {disposition!r}")
            continue
        if disposition == "inherited" and mode not in INHERITING_MODES:
            problems.append(f"{unit_id}：重新执行全集模式不得有承接项")
        record_path = Path(str(entry.get("record_path") or ""))
        record = _read_json(record_path) if entry.get("record_path") else None
        if record is None:
            problems.append(f"{unit_id}：记录缺失（{record_path}）")
            continue
        if record_path.stem != entry.get("record_sha256") or not seal_ok(record, "record_sha256") or record.get("record_sha256") != entry.get("record_sha256"):
            problems.append(f"{unit_id}：记录自摘要不符或与清单登记的不是同一条")
            continue
        if record.get("kind") != "formal" or record.get("unit_id") != unit_id:
            problems.append(f"{unit_id}：记录不是本单元的正式执行")
            continue
        if record.get("spec_sha256") != entry.get("spec_sha256") or record.get("inputs_sha256") != entry.get("inputs_sha256"):
            problems.append(f"{unit_id}：记录的单元规格或输入摘要与清单不符")
        run = record.get("run") if isinstance(record.get("run"), Mapping) else {}
        if disposition == "executed":
            if run.get("run_id") != manifest.get("run_id"):
                problems.append(f"{unit_id}：登记为本次执行，记录却来自别的运行")
            if store is not None and not store.record_path(unit_id, str(record["record_sha256"])).is_file():
                problems.append(f"{unit_id}：本次执行的记录没有入库")
        else:
            if store is None:
                problems.append(f"{unit_id}：承接项没有记录库可核验")
            else:
                current = Current(unit_id=unit_id, unit_type=str(record.get("unit_type")), spec={}, spec_sha256=str(entry.get("spec_sha256")),
                                  inputs=list(record.get("inputs") or []), inputs_sha256=str(entry.get("inputs_sha256")), inheritable=True)
                reasons = check_record(store, record_path, record, current, facts)
                if run.get("run_id") == manifest.get("run_id"):
                    reasons.append("承接项的记录来自本次运行")
                if reasons:
                    problems.append(f"{unit_id}：承接核验不通过：{'；'.join(reasons)}")
        group = entry.get("test_group")
        if group:
            tests = record.get("tests") if isinstance(record.get("tests"), Mapping) else {}
            group_tests.setdefault(str(group), []).extend(str(test_id) for test_id in tests)
    for group, expected in (manifest.get("test_groups") or {}).items():
        reported = group_tests.get(str(group), [])
        if sorted(reported) != sorted(expected) or len(reported) != len(set(reported)):
            missing_tests = sorted(set(expected) - set(reported))
            extra_tests = sorted(set(reported) - set(expected))
            problems.append(f"测试组 {group}：记录里的测试 ID 并集与全集不符（缺 {len(missing_tests)}、多 {len(extra_tests)}、"
                            f"重复 {len(reported) - len(set(reported))}）")
    for entry in manifest.get("diagnostic") or []:
        record = _read_json(Path(str(entry.get("record_path") or ""))) if isinstance(entry, Mapping) else None
        if record is None or not seal_ok(record, "record_sha256") or record.get("kind") != "diagnostic":
            problems.append(f"诊断执行 {entry.get('unit_id') if isinstance(entry, Mapping) else entry}：记录缺失或不是诊断执行")
    if mode == UNIT_INHERIT:
        problems.extend(_unit_inherit_problems(manifest, entries, store))
    return problems


def _unit_inherit_problems(manifest: Mapping[str, Any], entries: list[Any], store: RecordStore | None) -> list[str]:
    """逐单元承接清单与决策收据的一致性（B5a-08，见模块说明）。凭证与批准的时效由执行器的复核点与消费端
    （unit_inherit.verify_published）判定，这里只核对两份文件说的是同一件事。"""

    reference = manifest.get("unit_inherit_decision")
    if not isinstance(reference, Mapping) or set(reference) != {"path", "decision_sha256", "context_sha256", "sources_sha256"}:
        return ["逐单元承接清单缺少决策收据引用"]
    if store is None:
        return ["逐单元承接清单没有记录库可核验决策收据"]
    path = store.decision_path(str(manifest.get("run_id")))
    decision = _read_json(path) if str(reference["path"]) == str(path) else None
    if (decision is None or decision.get("schema_version") != UNIT_INHERIT_DECISION_SCHEMA or not seal_ok(decision, "decision_sha256")
            or decision.get("decision_sha256") != reference["decision_sha256"]):
        return ["决策收据不在记录库的规定位置、自摘要不符或与清单登记的不是同一份"]
    problems: list[str] = []
    if decision.get("run_id") != manifest.get("run_id") or decision.get("action") != "inherit-matching-units":
        problems.append("决策收据不属于本次运行，或不是三个复核点都通过的承接决策")
    if decision.get("context_sha256") != reference["context_sha256"] or decision.get("sources_sha256") != reference["sources_sha256"]:
        problems.append("清单登记的上下文或来源集合摘要与决策收据不一致")
    checkpoints = decision.get("checkpoints") if isinstance(decision.get("checkpoints"), list) else []
    if (tuple(item.get("checkpoint") if isinstance(item, Mapping) else None for item in checkpoints) != UNIT_INHERIT_CHECKPOINTS
            or not all(item.get("authorized") is True for item in checkpoints)):
        problems.append("决策收据的复核点不完整或有拒绝")
    units = decision.get("units") if isinstance(decision.get("units"), Mapping) else {}
    rows = {str(entry.get("unit_id")): entry for entry in entries if isinstance(entry, Mapping)}
    if set(units) != set(rows):
        problems.append("决策收据的单元集合与清单不同")
    for unit_id, entry in sorted(rows.items()):
        row = units.get(unit_id) if isinstance(units.get(unit_id), Mapping) else {}
        source = row.get("source") if isinstance(row.get("source"), Mapping) else {}
        if entry.get("disposition") == "inherited":
            basis = entry.get("basis") if isinstance(entry.get("basis"), Mapping) else {}
            if (row.get("disposition") != "inherited" or source.get("record_sha256") != entry.get("record_sha256")
                    or source.get("run_id") != basis.get("run_id")):
                problems.append(f"{unit_id}：承接项的来源与决策收据不一致")
        elif row.get("disposition") != "executed":
            problems.append(f"{unit_id}：执行项在决策收据里不是执行")
    return problems


# ---------------------------------------------------------------------------
# 声明级承接（declared-inputs/v1）
#
# 解决什么问题：命令单元（backend／frontend／egress-spec 整条命令）原先声明「整个仓库四段＋HEAD」，改任何一个文件、换任何一次
# 提交，这些单元的输入摘要全部变化，一个都不能承接。声明级承接把输入换成「该命令真实读到的目录与文件＋外部输入合同」，并要求
# 一份采样证据（strace 读集：仓库内逐路径、仓库外按目录聚合）证明「采样到的路径全部落在声明范围内」。
#
# 本章是验证侧代码，被受管工具钉住（改一个字节就要补钉值、重跑 pre-A3 九链），所以刻意写成纯函数，并守住四条信任边界：
# 1. 记录里写的结论不可信。read_audit.undeclared_count、coverage 计数只是执行侧自己的判断；消费者一律从记录库取封存的证据
#    文件，用 declared_coverage 重新计算——篡改结论、篡改证据、篡改范围，都过不了（见 _check_declared_audit）。
# 2. 历史重放不读外部事实。historical_read_audit=True 时只核封存物（记录、证据、合同凭证）的格式与相互一致，绝不探测当前
#    磁盘、docker、git：历史凭证必须在任何机器、任何时候重放出同一结论。当前环境是否仍与来源一致，由通用的 inputs_sha256
#    比较保证（当前输入由执行器按声明实时算出，含生产侧探测的 contract:* 条目），不在本章。
# 3. 不 import read_audit，不调用 _audit_module()。control 四链用 control_read_audit 单独加载本文件的一份实例，并把该实例的
#    _audit_module 指到自己的适配器；本章若碰它，就等于给 control 链加了耦合。覆盖检查、证据解析、合同凭证核验全部在这里自足。
# 4. 新分支只由两个条件触发：声明里出现 declared_scope 键（declared_inputs 头部），或当前输入里出现名为
#    declared-inputs-scope 的条目（check_record 头部）。control 四链的声明与条目里两者都没有，永远走不到这里，
#    既有函数与常量的行为字节不变。
# ---------------------------------------------------------------------------

DECLARED_REGISTRY_SCHEMA = "declared-inputs-registry/v1"
DECLARED_SCOPE_SCHEMA = "declared-scope/v1"
DECLARED_SAMPLE_SCHEMA = "declared-sample/v1"
DECLARED_CONTRACT_SCHEMA = "declared-contract/v1"
# 进入单元输入的条目名，以及记录 read_audit.coverage_scope 的取值。
DECLARED_SCOPE_ENTRY = "declared-inputs-scope"
DECLARED_COVERAGE_SCOPE = "declared-inputs/v1"
# 访问类型沿用审计器的词汇：read／exec／write／missing 是「判」的，stat／dir／list 只是元数据。
DECLARED_KINDS = frozenset({"read", "exec", "write", "missing", "stat", "dir", "list"})
# 仓库外路径的 class → 允许的访问类型。语义分类只在验证侧由声明范围的 outside 完成，采样器只按目录聚合、不分类，
# 所以不会出现两处分类规则漂移。
DECLARED_CLASSES: dict[str, frozenset[str]] = {
    # 工具链：只读、可执行；内容由合同 toolchain:*（版本行＋二进制摘要）绑定。
    "toolchain": frozenset({"read", "exec", "stat", "dir", "list", "missing"}),
    # 依赖内容（Go 模块缓存）：不许写——命令在写就说明它在联网下载，违背离线合同；内容由 go.sum 与合同 cache:* 约束。
    "dependency-content": frozenset({"read", "stat", "dir", "list", "missing"}),
    # 性能缓存（Go 构建缓存）：内容寻址，只影响速度，可读、可写、可执行；初态规则由合同 cache:* 约束。
    # 允许 exec 的原因：Go 1.24 起 ``go run``／``go tool`` 构建出的可执行文件缓存在 GOCACHE 里，并从那里执行。Go 缓存是内容寻址、
    # 只影响速度的缓存，信任模型与读缓存相同——读进来的缓存内容同样会影响命令的行为，执行只是它的另一种用法。风险：有人能写
    # GOCACHE 就能替换缓存里的可执行文件，让命令去执行它；这不在威胁模型内（能写工作根的人本来就能改一切），所以不为它另加
    # 约束。其它类（toolchain／dependency-content／system／virtual／home-config）的可执行性一律不变。
    "performance-cache": frozenset({"read", "write", "exec", "stat", "dir", "list", "missing"}),
    # 临时输出：命令自产自销，不绑定。
    "scratch": frozenset({"read", "exec", "write", "missing", "stat", "dir", "list"}),
    # 系统库与配置：由环境指纹（dpkg 包摘要、内核、Python 版本）覆盖。
    "system": frozenset({"read", "exec", "stat", "dir", "list", "missing"}),
    # 虚拟文件系统（/proc、/sys、/dev）：不绑定。
    "virtual": frozenset({"read", "stat", "dir", "list", "missing"}),
    # HOME 下会影响命令行为的配置：逐路径登记「存在则内容摘要，不存在则 missing」，合同 host-config:*。
    "home-config": frozenset({"read", "stat", "missing"}),
}
# 容忍的 unresolved 原因（以 : 结尾的按前缀匹配，其余精确匹配）；其余原因一律判证据无效：
# * ``symlink-metadata-not-bound:`` ——pnpm 的 node_modules 全是符号链接，lstat／O_NOFOLLOW 会让严格解析给出这类原因，它只增加
#   计数、不丢路径；
# * ``unfinished-calls-at-eof`` ——多线程进程（node、vitest、go）退出时，strace 不会为还在系统调用里的线程打印 resumed，流结束时
#   就留下少量永远配不上的 ``<unfinished ...>``。被打断的调用没有返回成功，不贡献读集；但数量必须有上限（见下），免得大面积
#   丢失的流被当成正常现象。
DECLARED_TOLERATED_UNRESOLVED = ("symlink-metadata-not-bound:", "unfinished-calls-at-eof")
# unfinished-calls-at-eof 的计数上限：超过即证据无效。
_DECLARED_MAX_UNFINISHED_AT_EOF = 64
# coverage.unfinished_samples（给评审看的悬着调用样例）的体量上限：只核类型和长度，不据此放行或拒绝。
_DECLARED_MAX_UNFINISHED_SAMPLES = 8
_DECLARED_MAX_SAMPLE_CHARS = 300

# 声明范围 contracts 里可以列的合同类型；git-history、make-expansion 的合同由 repo.git_history、make_expansion 派生。
_DECLARED_SCOPE_CONTRACT_TYPES = ("toolchain", "cache", "service-image", "host-config")
# 必须挂合同的 class → 它要求的合同类型。
_DECLARED_CLASS_CONTRACT = {"toolchain": "toolchain", "dependency-content": "cache", "performance-cache": "cache", "home-config": "host-config"}
# 声明了某工具链，就必须把它的依赖锁作为单文件条目列进 repo.files（范围聚合摘要里的锁文件不能被消费者单独交叉核对）。
_DECLARED_TOOLCHAIN_LOCKS = {"go": ("backend/go.mod", "backend/go.sum"), "pnpm": ("frontend/pnpm-lock.yaml", "frontend/package.json")}
# 缓存合同的规则 → 它对应的角色；首发只实现这四条（不可变初态快照 snapshot 留到阶段 2）。
_DECLARED_CACHE_RULES = {"any-content-addressed": "performance-cache", "any-scratch": "scratch",
                         "lock-derived-pnpm": "dependency-install", "lock-derived-go-sum": "dependency-content"}
_DECLARED_CACHE_LOCATIONS = frozenset({"work", "tree", "tmp", "home"})
# 锁文件缺省位置（缓存合同没写 lock_file 时按这两个核对）。
_DECLARED_LOCK_DEFAULTS = {"lock-derived-pnpm": "frontend/pnpm-lock.yaml", "lock-derived-go-sum": "backend/go.sum"}
# Go 等工具沿祖先目录找模块与版本控制目录时会探测这些名字（不存在）：祖先探测不算依赖仓库外内容，按内置规则放行。
_DECLARED_ANCESTOR_PROBES = frozenset({"go.mod", "go.work", ".hg", ".svn", ".bzr", ".fossil-settings"})
# 仓库外「噪声探测」放行根（只对 missing／stat／dir 生效，见 ``declared_coverage`` 的 C2／C3 说明）：这些目录属于系统状态——
# PATH 搜索、cgo 编译器的头文件与库搜索路径、解析器配置、Node 逐级向上找模块，都会对它们之下大量不存在的候选路径做 stat／open。
# 系统状态不绑定，由环境指纹近似，与 system 类同口径；读取、写入、执行存在的东西仍然要有声明前缀。
_DECLARED_PROBE_ROOTS = ("/usr/", "/opt/", "/var/", "/run/", "/etc/", "/lib/", "/lib64/", "/bin/", "/sbin/", "/srv/", "/mnt/", "/media/",
                         "/home/", "/root/", "/tmp/")
# 根目录列表条目的名字（``repo.root_listing`` 为真时产生的输入条目）；``scope:`` 前缀与范围条目同一族，所以 range 不得叫这个名字。
DECLARED_ROOT_LISTING_NAME = "root-listing"
DECLARED_ROOT_LISTING_ENTRY = f"scope:{DECLARED_ROOT_LISTING_NAME}"
# 写入空设备是无害的丢弃输出（merge 也按这个精确路径豁免），/dev/ 下其它路径照旧不许写。
_DECLARED_DEV_NULL = "/dev/null"
# 返回的未覆盖明细上限（总数另在 counts 里），避免十万路径的证据把报告撑爆。
_DECLARED_MAX_FINDINGS = 200
_DECLARED_SHA256 = re.compile(r"[0-9a-f]{64}")
_DECLARED_COMMIT = re.compile(r"[0-9a-f]{40}")
_DECLARED_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_DECLARED_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_DECLARED_RANGE_NAME = re.compile(r"[a-z0-9][a-z0-9._-]*")
_DECLARED_MAKE_TARGET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+/-]*")
_DECLARED_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_DECLARED_ALIAS_PATH = re.compile(r"\{(WORK|HOME|TMP)\}(?:/(.*))?", re.S)
# 采样证据里 git 命令出现的 HEAD 一类引用：声明级单元不依赖 HEAD，出现就判违规。前面是路径分量、标识符字符的不算
# （.git/HEAD、origin/HEAD 只是路径或远端引用），但「提交..HEAD」「提交...HEAD」这种范围写法里的 HEAD 要算。
_DECLARED_HEAD_REF = re.compile(r"(?:(?<![A-Za-z0-9_./-])|(?<=\.\.))(?:FETCH_|ORIG_|MERGE_)?HEAD(?![A-Za-z0-9_-])")
_DECLARED_COMMIT_TOKEN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")


# 采样器把 .git/objects 下成千上万个内容寻址文件聚合成这一个目录键（仓库内路径里唯一带尾部 / 的键）；git 子进程的 argv 不可见
# （strace 没有解码 execve 参数）时，采样器留下这个哨兵而不是猜——验证侧见到它一律判违规，宁可拒绝也不漏判。
_DECLARED_GIT_OBJECTS_KEY = "{TREE}/.git/objects/"
_DECLARED_GIT_HIDDEN = "<argv-not-visible>"


def _declared_display(text: Any) -> str:
    """进入报告的路径文本：strace 里的非 UTF-8 路径经 surrogateescape 读入后是孤立代理字符，原样写进 JSON（ensure_ascii=False）再
    按 UTF-8 编码会抛异常；这里换成反斜杠转义，保证任何报告都能安全落盘。"""

    return str(text).encode("utf-8", "backslashreplace").decode("utf-8")


def _declared_reason_tolerated(reason: str) -> bool:
    """unresolved 原因是否在容忍集合里：``DECLARED_TOLERATED_UNRESOLVED`` 里以 : 结尾的项按前缀匹配，其余项精确匹配（免得
    ``unfinished-calls-at-eof-…`` 之类名字相近的别种原因蹭上豁免）。"""

    return any(reason.startswith(item) if item.endswith(":") else reason == item for item in DECLARED_TOLERATED_UNRESOLVED)


def declared_command_sha256(argv: Sequence[str], cwd_rel: str) -> str:
    """命令身份摘要：去掉启动前缀（launcher）的 argv 加相对测试树根的工作目录。登记表的 command_sha256、采样证据的
    command.sha256、记录里 sample_binding.command_sha256 都用这一个算法，命令变了证据就不再适用。"""

    return sha256_json({"argv": list(argv), "cwd_rel": cwd_rel})


def _declared_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _declared_keys(value: Any, *, required: Iterable[str] = (), optional: Iterable[str] = (), where: str) -> None:
    """字段集合闭合：必需键缺一个、多出任何未知键都拒绝；容器只认 dict（与 JSON 一一对应）。"""

    if not isinstance(value, dict):
        raise ValueError(f"{where} 必须是对象")
    required, optional = set(required), set(optional)
    keys = set(value)
    if not all(isinstance(key, str) for key in keys):
        raise ValueError(f"{where} 的键必须是字符串")
    missing, extra = sorted(required - keys), sorted(keys - required - optional)
    if missing or extra:
        raise ValueError(f"{where} 的字段集合是闭合的：缺 {missing}，多 {extra}")


def _declared_strings(value: Any, where: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{where} 必须是字符串数组")
    if len(value) != len(set(value)):
        raise ValueError(f"{where} 里有重复项")
    if nonempty and not value:
        raise ValueError(f"{where} 不得为空")
    return value


def _declared_rel(value: Any, where: str, *, directory: bool | None = None) -> str:
    """仓库相对路径（前缀或单个文件）：非空、不以 / 开头、无反斜杠与控制字符、无空／.／.. 分量；``directory`` 为真要求以 /
    结尾（目录前缀），为假要求不以 / 结尾（单个文件），为 None 不限制。空串不允许：空串＝整个仓库，正是声明级承接要去掉的。"""

    if not isinstance(value, str) or not value or value.startswith("/") or any(ch in value for ch in "\\\0\n\r"):
        raise ValueError(f"{where} 必须是非空、不以 / 开头、不含反斜杠与控制字符的仓库相对路径：{value!r}")
    if directory is True and not value.endswith("/"):
        raise ValueError(f"{where} 必须是以 / 结尾的目录前缀：{value!r}")
    if directory is False and value.endswith("/"):
        raise ValueError(f"{where} 必须是单个文件路径（不以 / 结尾）：{value!r}")
    parts = value.split("/")
    if value.endswith("/"):
        parts = parts[:-1]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{where} 不得含空、. 或 .. 分量：{value!r}")
    return value


def _declared_ignored_item(value: Any, where: str) -> str:
    """``repo.repo_ignored`` 的一项：以 / 结尾的目录前缀（字面匹配，语义不变，不支持通配符），或不以 / 结尾的仓库相对 glob。

    glob 只支持 ``*``（一段路径内任意个字符）与 ``?``（一段路径内一个字符），都不跨 /，整条相对路径必须完全匹配（不含它的
    子路径——要整个目录请写以 / 结尾的前缀）；不允许绝对路径、空／.／.. 分量、``**``（不支持跨目录）、方括号字符类；不含通配符的
    项就是一个精确路径。例如 ``frontend/vitest.config.ts.timestamp-*``（vite 在 frontend/ 根下写、读回、再删的随机名临时文件）。"""

    if isinstance(value, str) and value.endswith("/"):
        _declared_rel(value, where, directory=True)
        if any(char in value for char in "*?[]"):
            raise ValueError(f"{where} 以 / 结尾的是目录前缀，不支持通配符；要通配请写不以 / 结尾的 glob：{value!r}")
        return value
    _declared_rel(value, where, directory=False)
    if "**" in value:
        raise ValueError(f"{where} 不支持 **（* 不跨 /，要整个目录请写以 / 结尾的前缀）：{value!r}")
    if "[" in value or "]" in value:
        raise ValueError(f"{where} 的 glob 只支持 * 与 ?，不支持方括号字符类：{value!r}")
    return value


def _declared_glob(pattern: str) -> "re.Pattern[str]":
    """仓库相对 glob → 正则（用 ``fullmatch`` 整条匹配）：``*`` 是一段内任意个字符、``?`` 是一段内一个字符，都不跨 /；其它
    字符按字面。"""

    return re.compile("".join("[^/]*" if char == "*" else "[^/]" if char == "?" else re.escape(char) for char in pattern))


def _declared_outside_prefix(value: Any, where: str) -> str:
    """outside 的前缀：绝对路径，或 {WORK}／{HOME}／{TMP} 别名加路径（{TREE} 是仓库根，不在这里写）。以 / 结尾是目录前缀，
    否则是精确路径。整个根目录、整个 {WORK}／{HOME} 范围过宽，拒绝（{TMP} 本身就是临时目录，可以整个声明）。"""

    if not isinstance(value, str) or any(ch in value for ch in "\\\0\n\r"):
        raise ValueError(f"{where} 必须是不含反斜杠与控制字符的字符串：{value!r}")
    alias = re.fullmatch(r"\{(WORK|HOME|TMP)\}/(.*)", value, re.S)
    if alias is not None:
        name, body = alias.groups()
        if body == "":
            if name != "TMP":
                raise ValueError(f"{where} 不得把整个 {{{name}}} 作为前缀（范围过宽），请写到具体子目录或文件：{value!r}")
            return value
    elif value.startswith("/"):
        body = value[1:]
        if body == "":
            raise ValueError(f"{where} 不得把根目录 / 作为前缀（范围过宽）")
    else:
        raise ValueError(f"{where} 只能以 /、{{WORK}}/、{{HOME}}/ 或 {{TMP}}/ 开头（{{TREE}} 是仓库根，不在 outside 里写）：{value!r}")
    parts = body.split("/")
    if body.endswith("/"):
        parts = parts[:-1]
    if any(part in ("", ".", "..") for part in parts):
        raise ValueError(f"{where} 不得含空、. 或 .. 分量：{value!r}")
    return value


def _declared_unix_socket(value: Any, where: str) -> str:
    """AF_UNIX 目标：以 / 开头的规范绝对路径（套接字是文件，不以 / 结尾；不是 {WORK} 之类的别名路径，也不是抽象套接字）。"""

    if (not isinstance(value, str) or not value.startswith("/") or value.endswith("/") or any(ch in value for ch in "\\\0\n\r")
            or any(part in ("", ".", "..") for part in value[1:].split("/"))):
        raise ValueError(f"{where} 的每项必须是以 / 开头、不以 / 结尾、无空／.／.. 分量的规范绝对路径：{value!r}")
    return value


def _declared_contract_id(value: Any, where: str) -> tuple[str, str]:
    """``contracts`` 里的合同 ID：``<类型>:<名字>``，类型限 toolchain／cache／service-image／host-config。"""

    if not isinstance(value, str) or value.count(":") != 1:
        raise ValueError(f"{where} 必须是「类型:名字」形式的合同 ID：{value!r}")
    kind, name = value.split(":")
    if kind not in _DECLARED_SCOPE_CONTRACT_TYPES:
        raise ValueError(f"{where} 的合同类型未知：{kind!r}（已知 {list(_DECLARED_SCOPE_CONTRACT_TYPES)}；git-history、make-expansion "
                         "的合同由 repo.git_history、make_expansion 派生，不在 contracts 里列）")
    if not _DECLARED_NAME.fullmatch(name):
        raise ValueError(f"{where} 的合同名字只能含字母、数字、点、下划线、连字符：{value!r}")
    return kind, name


def _declared_requires_shape(value: Any, where: str) -> dict[str, Any]:
    """命令运行期要求：``argv_contains``（命令行里必须出现的参数）、``go_test_argv_contains``（命令是 ``go test`` 时才要求出现
    的参数，例如防止测试结果缓存成为隐藏输入的 ``-count=1``；命令不是 go test 时不要求——同一个 Go 构建缓存也被 go vet、
    lint 一类不接受该参数的命令使用）与 ``env_equals``（环境变量必须等于的值，例如离线运行的 GOPROXY=off）；消费者拿封存的
    spec 与环境指纹核对。"""

    _declared_keys(value, optional={"argv_contains", "go_test_argv_contains", "env_equals"}, where=where)
    if not value:
        raise ValueError(f"{where} 至少要有 argv_contains、go_test_argv_contains 或 env_equals 之一")
    for key in ("argv_contains", "go_test_argv_contains"):
        if key in value:
            tokens = _declared_strings(value[key], f"{where}.{key}", nonempty=True)
            if not all(tokens):
                raise ValueError(f"{where}.{key} 的参数不得为空串")
    if "env_equals" in value:
        env = value["env_equals"]
        if (not isinstance(env, dict) or not env
                or not all(isinstance(key, str) and _DECLARED_ENV_NAME.fullmatch(key) and isinstance(item, str) for key, item in env.items())):
            raise ValueError(f"{where}.env_equals 必须是非空的 {{环境变量名: 字符串值}}")
    return value


def validate_declared_scope(scope: Mapping[str, Any]) -> dict[str, Any]:
    """校验声明范围 ``declared-scope/v1``，返回深拷贝；非法抛 ``ValueError``（文本写明违反的规则）。纯函数，不读磁盘。

    规则（字段集合处处闭合：多一个键、缺一个必需键都拒绝；容器只认 dict／list，不认元组与其它映射，保证与 JSON 一一对应）：

    * 顶层：``schema_version``＝``declared-scope/v1`` 与 ``repo`` 必需；``make_expansion``、``outside``、``contracts``、
      ``network``、``unix_sockets``、``requires`` 可选（缺省＝空）。至少要声明一个 range 或 file。
    * ``repo.ranges``：每项恰为 ``{name, include, exclude}``；``name`` 唯一（进入输入的条目名是 ``scope:<name>``）；``include``
      非空，``include``／``exclude`` 的每项是仓库相对前缀（见 ``_declared_rel``），不允许空串。``repo.files``：单个文件的仓库
      相对路径。``repo.list_ok``：人工评审过的可枚举目录（以 / 结尾，只放行目录本身，不递归）。``repo.repo_ignored``：命令的
      输出区（构建产物、代码生成的临时目录、前端缓存、vite 的随机名临时文件）：每项是以 / 结尾的目录前缀，或不以 / 结尾的仓库
      相对 glob（只支持 ``*`` 与 ``?``，不跨 /，整条路径完全匹配；见 ``_declared_ignored_item``）；匹配到的路径允许写，允许读回
      本次采样里同一路径也写过的文件，不允许读别的未跟踪路径（覆盖检查 C1）。
      ``repo.installed_dirs``：依赖安装目录，每项恰为 ``{prefix, contract}``——``prefix`` 是以 / 结尾的仓库
      相对前缀（例如 ``frontend/node_modules/``，pnpm 安装结果是未跟踪路径），``contract`` 是 ``cache:*`` 类合同并出现在
      ``contracts`` 里（内容由合同绑定，例如 lock-derived-pnpm：安装锁副本必须等于锁文件条目）；这些前缀下读取不要求已跟踪，
      写入仍不允许（除非同时落在 ``repo_ignored`` 前缀内，如 vitest 缓存）。前缀互不重复，且不得包含（等于或位于其上）任何
      range 的 include、``repo.files``、``repo.list_ok``，免得安装目录的豁免吞掉已声明的输入。
      ``repo.root_listing``（布尔，缺省 false）：命令会枚举测试树根目录（例如 make 在树根找 Makefile 与隐含规则候选，pnpm 向上找工作区
      根），并在树根下探测一批不存在的配置候选名（``package.json``、``.browserslistrc``、``eslint.config.js``、``RCS`` 等）。为真时：
      覆盖检查放行树根目录自身的 dir／list／stat／read，以及树根直接子项（``{TREE}/<单个名字>``）的 missing 探测；绑定方式是输入里多一个
      ``scope:root-listing`` 条目，摘要＝测试树根下 **被 git 跟踪的顶层名字**（文件名；目录名带尾斜杠）排序后的规范化 JSON 摘要，
      同名的跟踪文件一出现摘要就变。边界：只绑定跟踪的顶层名字，树根下未跟踪的新文件不绑定（与「未跟踪读取一律违规」同一口径）；
      树根以下的不存在路径不在放行之列（仍要被 range 覆盖）。``root-listing`` 是保留的 range 名。
      ``repo.head`` 只能是 false：读 HEAD 的单元登记为 rerun-only 并保留 head 条目，不做声明级承接。``repo.git_history``：
      ``{commits, reason}``，``commits`` 是非空的、互不重复的 40 位小写提交号。
    * ``make_expansion``：make 目标名列表；非空时 ``Makefile`` 由展开合同覆盖，所以不得再出现在 ``repo.files`` 或任何
      ``repo.ranges`` 的 ``include`` 里（两种绑定并存，会让「改无关目标的 recipe」也使单元失效）。
    * ``outside``：每项 ``{prefix, class}`` 或 ``{prefix, class, contract}``；``prefix`` 见 ``_declared_outside_prefix``，互不
      重复；``class`` 取 ``DECLARED_CLASSES`` 的键；toolchain／dependency-content／performance-cache／home-config 必须带
      ``contract``，合同类型与 class 对应（toolchain、cache、cache、host-config）并出现在 ``contracts`` 里；其它 class 不得带。
    * ``contracts``：合同 ID 列表（见 ``_declared_contract_id``）；声明了 ``toolchain:go`` 必须在 ``repo.files`` 里单独列出
      ``backend/go.mod``、``backend/go.sum``，声明了 ``toolchain:pnpm`` 必须列出 ``frontend/pnpm-lock.yaml``、
      ``frontend/package.json``（依赖锁要有自己的单文件条目，消费者才能拿安装结果与它交叉核对）。
    * ``network``：只允许 ``"loopback-only"``（外连只许回环地址）。``unix_sockets``：允许命令连接的 AF_UNIX 目标（例如
      ``/var/run/docker.sock``）——以 / 开头的规范绝对路径（不是别名路径，不以 / 结尾，无空／.／.. 分量），互不重复，缺省为空。
    * ``requires``：命令运行期要求（可选，默认关），见 ``_declared_requires_shape``。"""

    if not isinstance(scope, dict):
        raise ValueError("声明范围必须是对象")
    _declared_keys(scope, required={"schema_version", "repo"},
                   optional={"make_expansion", "outside", "contracts", "network", "unix_sockets", "requires"}, where="声明范围")
    if scope["schema_version"] != DECLARED_SCOPE_SCHEMA:
        raise ValueError(f"声明范围的 schema_version 必须是 {DECLARED_SCOPE_SCHEMA}：{scope['schema_version']!r}")
    repo = scope["repo"]
    _declared_keys(repo, optional={"ranges", "files", "list_ok", "repo_ignored", "installed_dirs", "root_listing", "head", "git_history"},
                   where="声明范围 repo")
    if "root_listing" in repo and not isinstance(repo["root_listing"], bool):
        raise ValueError(f"repo.root_listing 必须是布尔值：{repo['root_listing']!r}")
    ranges = repo.get("ranges", [])
    if not isinstance(ranges, list):
        raise ValueError("repo.ranges 必须是数组")
    names: list[str] = []
    includes: list[str] = []
    for index, item in enumerate(ranges):
        where = f"repo.ranges[{index}]"
        _declared_keys(item, required={"name", "include", "exclude"}, where=where)
        if not isinstance(item["name"], str) or not _DECLARED_RANGE_NAME.fullmatch(item["name"]):
            raise ValueError(f"{where}.name 只能含小写字母、数字、点、下划线、连字符，且以字母或数字开头：{item['name']!r}")
        if item["name"] == DECLARED_ROOT_LISTING_NAME:
            raise ValueError(f"{where}.name 不能叫 {DECLARED_ROOT_LISTING_NAME}：它是 repo.root_listing 的条目名 {DECLARED_ROOT_LISTING_ENTRY} 保留的")
        names.append(item["name"])
        include = _declared_strings(item["include"], f"{where}.include", nonempty=True)
        exclude = _declared_strings(item["exclude"], f"{where}.exclude")
        for prefix in include:
            _declared_rel(prefix, f"{where}.include")
        for prefix in exclude:
            _declared_rel(prefix, f"{where}.exclude")
        includes.extend(include)
    if len(names) != len(set(names)):
        raise ValueError("repo.ranges 的 name 必须唯一")
    files = _declared_strings(repo.get("files", []), "repo.files")
    for path in files:
        _declared_rel(path, "repo.files", directory=False)
    list_ok = _declared_strings(repo.get("list_ok", []), "repo.list_ok")
    for path in list_ok:
        _declared_rel(path, "repo.list_ok", directory=True)
    for item in _declared_strings(repo.get("repo_ignored", []), "repo.repo_ignored"):
        _declared_ignored_item(item, "repo.repo_ignored")
    if not ranges and not files:
        raise ValueError("声明范围至少要声明一个 repo.ranges 或 repo.files")
    if repo.get("head", False) is not False:
        raise ValueError("repo.head 只能是 false：读 HEAD 的单元登记为 rerun-only 并保留 head 条目，不做声明级承接")
    history = repo.get("git_history")
    commits: list[str] = []
    if history is not None:
        _declared_keys(history, required={"commits", "reason"}, where="repo.git_history")
        commits = _declared_strings(history["commits"], "repo.git_history.commits", nonempty=True)
        if not all(_DECLARED_COMMIT.fullmatch(commit) for commit in commits):
            raise ValueError("repo.git_history.commits 的每项必须是 40 位小写十六进制提交号")
        if not isinstance(history["reason"], str) or not history["reason"].strip():
            raise ValueError("repo.git_history.reason 必须写明为什么读这些历史提交")
    targets = _declared_strings(scope.get("make_expansion", []), "make_expansion")
    if not all(_DECLARED_MAKE_TARGET.fullmatch(target) for target in targets):
        raise ValueError("make_expansion 的每项必须是 make 目标名（字母数字开头，只含字母、数字、点、下划线、加号、连字符、斜杠）")
    if targets and ("Makefile" in files or any(_matches("Makefile", prefix) for prefix in includes)):
        raise ValueError("声明了 make_expansion 后，Makefile 由展开合同覆盖，不得再列进 repo.files 或被 repo.ranges 的 include 覆盖")
    contracts = _declared_strings(scope.get("contracts", []), "contracts")
    for contract in contracts:
        _declared_contract_id(contract, "contracts")
    for tool, locks in _DECLARED_TOOLCHAIN_LOCKS.items():
        if f"toolchain:{tool}" in contracts and not set(locks) <= set(files):
            raise ValueError(f"声明了 toolchain:{tool} 就必须在 repo.files 里单独列出依赖锁 {list(locks)}")
    installed = repo.get("installed_dirs", [])
    if not isinstance(installed, list):
        raise ValueError("repo.installed_dirs 必须是数组")
    installed_prefixes: list[str] = []
    for index, item in enumerate(installed):
        where = f"repo.installed_dirs[{index}]"
        _declared_keys(item, required={"prefix", "contract"}, where=where)
        prefix = _declared_rel(item["prefix"], f"{where}.prefix", directory=True)
        kind, _name = _declared_contract_id(item["contract"], f"{where}.contract")
        if kind != "cache" or item["contract"] not in contracts:
            raise ValueError(f"{where}.contract 必须是 cache:<名字> 类型的合同，并出现在 contracts 里（安装目录的内容由合同绑定）：{item['contract']!r}")
        # 豁免不能吞掉已声明的输入：安装目录前缀必须在 range、单文件、枚举目录的「下面」，不能等于或盖住它们。
        declared = [*includes, *files, *list_ok]
        if any(entry == prefix or entry.startswith(prefix) for entry in declared):
            raise ValueError(f"{where}.prefix 不得等于或包含已声明的 range／file／list_ok（安装目录的豁免会吞掉它们）：{prefix!r}")
        installed_prefixes.append(prefix)
    if len(installed_prefixes) != len(set(installed_prefixes)):
        raise ValueError("repo.installed_dirs 的 prefix 必须互不重复")
    outside = scope.get("outside", [])
    if not isinstance(outside, list):
        raise ValueError("outside 必须是数组")
    prefixes: list[str] = []
    for index, item in enumerate(outside):
        where = f"outside[{index}]"
        _declared_keys(item, required={"prefix", "class"}, optional={"contract"}, where=where)
        prefixes.append(_declared_outside_prefix(item["prefix"], f"{where}.prefix"))
        if not isinstance(item["class"], str) or item["class"] not in DECLARED_CLASSES:
            raise ValueError(f"{where}.class 未知：{item['class']!r}（已知 {sorted(DECLARED_CLASSES)}）")
        needed = _DECLARED_CLASS_CONTRACT.get(item["class"])
        if needed is None and "contract" in item:
            raise ValueError(f"{where}：class={item['class']} 不绑定合同，不得带 contract")
        if needed is not None:
            if "contract" not in item:
                raise ValueError(f"{where}：class={item['class']} 必须带 contract（{needed}:<名字>）")
            kind, _name = _declared_contract_id(item["contract"], f"{where}.contract")
            if kind != needed or item["contract"] not in contracts:
                raise ValueError(f"{where}.contract 必须是 {needed}:<名字> 类型的合同，并出现在 contracts 里：{item['contract']!r}")
    if len(prefixes) != len(set(prefixes)):
        raise ValueError("outside 的 prefix 必须互不重复")
    if scope.get("network", "loopback-only") != "loopback-only":
        raise ValueError('network 只允许 "loopback-only"')
    for target in _declared_strings(scope.get("unix_sockets", []), "unix_sockets"):
        _declared_unix_socket(target, "unix_sockets")
    if "requires" in scope:
        _declared_requires_shape(scope["requires"], "requires")
    return json.loads(json.dumps(scope, ensure_ascii=False))


def _declared_sample_ref(value: Any) -> dict[str, str]:
    """进入 ``declared-inputs-scope`` 条目的采样引用：恰为 evidence_sha256、command_sha256、sampler_sha256 三项（各 64 位小写
    十六进制）。证据摘要是证据文件原始字节的 sha256（与记录里 read_audit.trace.sha256、记录库 logs/<摘要>.log 的内容寻址
    同一个值；生产侧用规范化 JSON 字节写证据文件时，它同时等于规范化摘要）。"""

    _declared_keys(value, required={"evidence_sha256", "command_sha256", "sampler_sha256"}, where="采样引用")
    if not all(isinstance(item, str) and _DECLARED_SHA256.fullmatch(item) for item in value.values()):
        raise ValueError("采样引用的 evidence_sha256、command_sha256、sampler_sha256 必须是 64 位小写十六进制")
    return {key: value[key] for key in sorted(value)}


def _declared_scope_entry(inputs: Sequence[Mapping[str, Any]] | None) -> Mapping[str, Any] | None:
    """输入明细里的 declared-inputs-scope 条目（没有返回 None）。"""

    for entry in inputs or []:
        if isinstance(entry, Mapping) and entry.get("name") == DECLARED_SCOPE_ENTRY:
            return entry
    return None


def _declared_scope_inputs(repo: "RepoIndex", declaration: Mapping[str, Any]) -> list[dict[str, Any]]:
    """声明级承接的输入展开（``declared_inputs`` 遇到 ``declared_scope`` 键时走这里）。

    声明字典恰为 ``{"declared_scope": 声明范围, "declared_sample": 采样引用}``。展开结果：

    * ``declared-inputs-scope``（类别 policy）：摘要＝``sha256_json({"scope": 范围, "sample": 采样引用})``，detail 带范围全文与
      采样引用——改范围、改证据、改命令、换采样器，都让这一项变，单元失效；
    * ``scope:<名字>``：每个 range 一项（目录范围摘要，用现有的 ``RepoIndex.range_entry``）；
    * ``file:<路径>``：每个单文件一项（现有的 ``RepoIndex.file_entry``）；
    * ``scope:root-listing``（类别 tests）：只在 ``repo.root_listing`` 为真时产生，摘要＝测试树根下被 git 跟踪的顶层名字排序后的
      规范化 JSON 摘要，detail 带名字列表（见 ``_declared_root_listing_entry``）。

    **没有 head**：命令不读 HEAD 提交号（读 git 历史的单元不走声明级承接）。``contract:*`` 条目不在这里产生——合同事实要
    探测当前环境，由生产侧（执行器）探测后追加；验证侧只核封存值的格式（见 ``_check_declared_audit``）。"""

    if not isinstance(declaration, Mapping) or set(declaration) != {"declared_scope", "declared_sample"}:
        raise RecordsError(f"声明级输入声明须恰含 declared_scope 与 declared_sample 两个键：{declaration!r}")
    try:
        scope = validate_declared_scope(declaration["declared_scope"])
        sample = _declared_sample_ref(declaration["declared_sample"])
        binding = {"scope": scope, "sample": sample}
        # 摘要也放进 try：声明里带无法编码成 UTF-8 的字符（孤立代理字符）时 sha256_json 抛 UnicodeEncodeError（ValueError 的子类），
        # 同样按「声明非法」报，不让别的异常类型漏出去。
        binding_sha256 = sha256_json(binding)
    except ValueError as error:
        raise RecordsError(f"声明级输入声明非法：{error}") from error
    entries = [{"category": "policy", "name": DECLARED_SCOPE_ENTRY, "sha256": binding_sha256, "detail": binding}]
    for item in scope["repo"].get("ranges", []):
        entries.append(repo.range_entry({"category": "tests", "name": f"scope:{item['name']}", "include": list(item["include"]),
                                         "exclude": list(item["exclude"])}))
    for relative in scope["repo"].get("files", []):
        entries.append(repo.file_entry("tests", _repo_file(repo, relative)))
    if scope["repo"].get("root_listing"):
        entries.append(_declared_root_listing_entry(repo))
    return _unique(entries)


def _declared_root_names(tracked: Iterable[str]) -> list[str]:
    """测试树根下被 git 跟踪的顶层名字：文件取文件名，目录取目录名加尾斜杠；排序、去重。``tracked`` 是仓库相对路径的集合
    （``RepoIndex.files`` 的键）。"""

    return sorted({relative.split("/", 1)[0] + ("/" if "/" in relative else "") for relative in tracked})


def _declared_root_listing_entry(repo: "RepoIndex") -> dict[str, Any]:
    """``repo.root_listing`` 的输入条目：绑定「树根下被跟踪的顶层名字」这份清单。覆盖检查放行树根下一批不存在的配置候选名
    （``package.json``、``eslint.config.js`` 等）时，它们的「不存在」就靠这个摘要兜底——同名的跟踪文件一出现，摘要就变，
    单元不再被承接。取数用现有的 ``RepoIndex.files``，不另开 git 调用。只绑定跟踪的名字：树根下未跟踪的新文件不在里面。"""

    names = _declared_root_names(repo.files)
    return {"category": "tests", "name": DECLARED_ROOT_LISTING_ENTRY, "sha256": sha256_json(names), "detail": {"names": names}}


_DECLARED_SAMPLE_KEYS = frozenset({"schema_version", "unit_id", "command", "sampled", "aliases", "coverage", "repo_accesses",
                                   "outside_accesses", "git_commands", "network", "exit_code"})
# 采样器（declared_sampler.py merge）另外写入的三个键：valid／invalid_reasons 是它自己对采样有效性的判断，review 是给人看的评审
# 材料（冷热差异、写入集合、网络、docker 等，格式不规整、体量可能很大）。验证侧不信任也不解释 review，只要求 valid 为真；
# 手工构造的证据可以不带这三个键。
_DECLARED_SAMPLE_OPTIONAL = frozenset({"valid", "invalid_reasons", "review"})


def _declared_validate_sample(sample: Any) -> dict[str, Any]:
    """采样证据 ``declared-sample/v1`` 的格式核验（字段闭合；只核结构与类型，不判断读集是否被声明覆盖——那是
    ``declared_coverage`` 的事）。不合格抛 ``ValueError``。

    * ``command``：``{launcher, argv, cwd_rel, sha256}``——``sha256`` 必须等于 ``declared_command_sha256(argv, cwd_rel)``，
      证据自己的命令摘要必须与自己的命令内容一致；``cwd_rel`` 是相对测试树的路径，树根本身写 ``.``（或空串）；
    * ``coverage``：``{stream_complete, unresolved, unresolved_reasons, unfinished_calls, unresolved_process_events, lines}``，
      另可带非负整数 ``detached_markers``（冷缓存采样里 ``<pid> ???( <detached ...>`` 的 strace 标记行，由采样器预处理计数，
      不算 unresolved），以及可选的 ``unfinished_samples``（流结束时悬着的调用样例，字符串数组，最多 8 条、每条最长 300 字符，
      只供评审：验证侧只核类型和长度，不据其内容放行或拒绝）；
    * ``repo_accesses``：``[路径, 访问类型数组, 是否跟踪]`` 数组，仓库内路径完整保留（唯一的目录聚合键是 ``.git/objects/``）；
      ``outside_accesses``：``[路径, 访问类型数组, 计数]``，仓库外路径按目录聚合（键以 / 结尾＝该目录下直接子项的访问）；路径
      各自唯一；
    * ``git_commands``：命令行数组；``network``：``{family, target}`` 数组；``exit_code``：整数（采样超时被终止时采样器写
      null，这样的证据不会通过 C7）；
    * 可选 ``valid``（布尔）、``invalid_reasons``（字符串数组，须与 valid 互为非）、``review``（对象，不解释）。"""

    _declared_keys(sample, required=_DECLARED_SAMPLE_KEYS, optional=_DECLARED_SAMPLE_OPTIONAL, where="采样证据")
    if "valid" in sample and not isinstance(sample["valid"], bool):
        raise ValueError("采样证据 valid 必须是布尔值")
    if "invalid_reasons" in sample and (not isinstance(sample["invalid_reasons"], list)
                                        or not all(isinstance(item, str) for item in sample["invalid_reasons"])):
        raise ValueError("采样证据 invalid_reasons 必须是字符串数组")
    if "valid" in sample and "invalid_reasons" in sample and sample["valid"] == bool(sample["invalid_reasons"]):
        raise ValueError("采样证据的 valid 必须恰好等于 invalid_reasons 为空")
    if "review" in sample and not isinstance(sample["review"], dict):
        raise ValueError("采样证据 review 必须是对象")
    if sample["schema_version"] != DECLARED_SAMPLE_SCHEMA:
        raise ValueError(f"采样证据的 schema_version 必须是 {DECLARED_SAMPLE_SCHEMA}：{sample['schema_version']!r}")
    if not isinstance(sample["unit_id"], str) or not sample["unit_id"]:
        raise ValueError("采样证据的 unit_id 必须是非空字符串")
    command = sample["command"]
    _declared_keys(command, required={"launcher", "argv", "cwd_rel", "sha256"}, where="采样证据 command")
    if not isinstance(command["launcher"], list) or not all(isinstance(item, str) for item in command["launcher"]):
        raise ValueError("采样证据 command.launcher 必须是字符串数组")
    if (not isinstance(command["argv"], list) or not command["argv"] or not all(isinstance(item, str) for item in command["argv"])
            or not command["argv"][0]):
        raise ValueError("采样证据 command.argv 必须是以非空命令名开头的字符串数组")
    cwd_rel = command["cwd_rel"]
    if not isinstance(cwd_rel, str):
        raise ValueError("采样证据 command.cwd_rel 必须是字符串")
    if cwd_rel not in ("", "."):
        _declared_rel(cwd_rel, "采样证据 command.cwd_rel", directory=False)
    if command["sha256"] != declared_command_sha256(command["argv"], cwd_rel):
        raise ValueError("采样证据 command.sha256 与 command 的 argv、cwd_rel 内容不符")
    if not isinstance(sample["sampled"], dict):
        raise ValueError("采样证据 sampled 必须是对象")
    aliases = sample["aliases"]
    if not isinstance(aliases, dict) or not all(isinstance(key, str) and isinstance(item, str) for key, item in aliases.items()):
        raise ValueError("采样证据 aliases 必须是 {别名: 路径字符串}")
    coverage = sample["coverage"]
    _declared_keys(coverage, required={"stream_complete", "unresolved", "unresolved_reasons", "unfinished_calls",
                                       "unresolved_process_events", "lines"}, optional={"detached_markers", "unfinished_samples"},
                   where="采样证据 coverage")
    if not isinstance(coverage["stream_complete"], bool):
        raise ValueError("采样证据 coverage.stream_complete 必须是布尔值")
    if "unfinished_samples" in coverage:
        samples = coverage["unfinished_samples"]
        if (not isinstance(samples, list) or len(samples) > _DECLARED_MAX_UNFINISHED_SAMPLES
                or not all(isinstance(item, str) and len(item) <= _DECLARED_MAX_SAMPLE_CHARS for item in samples)):
            raise ValueError(f"采样证据 coverage.unfinished_samples 必须是字符串数组，最多 {_DECLARED_MAX_UNFINISHED_SAMPLES} 条、每条最长 "
                             f"{_DECLARED_MAX_SAMPLE_CHARS} 字符（只供评审，验证侧不据其内容放行或拒绝）")
    for key in ("unresolved", "unfinished_calls", "unresolved_process_events", "lines", "detached_markers"):
        if key in coverage and (not _declared_int(coverage[key]) or coverage[key] < 0):
            raise ValueError(f"采样证据 coverage.{key} 必须是非负整数")
    reasons = coverage["unresolved_reasons"]
    if (not isinstance(reasons, dict) or not all(isinstance(key, str) and key and _declared_int(item) and item >= 1
                                                 for key, item in reasons.items())):
        raise ValueError("采样证据 coverage.unresolved_reasons 必须是 {原因: 正整数计数}")
    for name, width, last in (("repo_accesses", 3, bool), ("outside_accesses", 3, int)):
        rows = sample[name]
        if not isinstance(rows, list):
            raise ValueError(f"采样证据 {name} 必须是数组")
        seen: set[str] = set()
        for row in rows:
            if (not isinstance(row, list) or len(row) != width or not isinstance(row[0], str) or not row[0]
                    or not isinstance(row[1], list) or not row[1] or not all(isinstance(kind, str) for kind in row[1])):
                raise ValueError(f"采样证据 {name} 的每项必须是 [路径, 访问类型数组, {'是否跟踪' if last is bool else '计数'}]：{row!r}")
            if (last is bool and not isinstance(row[2], bool)) or (last is int and (not _declared_int(row[2]) or row[2] < 0)):
                raise ValueError(f"采样证据 {name} 的第三项类型不对（{'布尔值' if last is bool else '非负整数'}）：{row!r}")
            if row[0] in seen:
                raise ValueError(f"采样证据 {name} 的路径重复：{row[0]!r}")
            seen.add(row[0])
    git_commands = sample["git_commands"]
    if (not isinstance(git_commands, list) or not all(isinstance(argv, list) and argv and all(isinstance(item, str) for item in argv)
                                                      for argv in git_commands)):
        raise ValueError("采样证据 git_commands 必须是命令行（非空字符串数组）的数组")
    network = sample["network"]
    if not isinstance(network, list):
        raise ValueError("采样证据 network 必须是数组")
    for item in network:
        _declared_keys(item, required={"family", "target"}, where="采样证据 network 项")
        if not isinstance(item["family"], str) or not isinstance(item["target"], str):
            raise ValueError("采样证据 network 项的 family、target 必须是字符串")
    if sample["exit_code"] is not None and not _declared_int(sample["exit_code"]):
        raise ValueError("采样证据 exit_code 必须是整数或 null")
    return sample


def _declared_clean_path(text: str) -> bool:
    """规范路径体：不含空、.、.. 分量，不含反斜杠与控制字符（允许末尾一个 /，表示目录聚合）。空串不是路径体。"""

    if not text or any(ch in text for ch in "\\\0\n\r"):
        return False
    parts = text.split("/")
    if text.endswith("/"):
        parts = parts[:-1]
    return all(part not in ("", ".", "..") for part in parts)


def _declared_covers(prefix: str, key: str) -> bool:
    """仓库外路径 ``key`` 是否落在声明前缀 ``prefix`` 之下：目录前缀（以 / 结尾）覆盖其下全部路径与目录自身，其它前缀只覆盖
    精确相等的路径。"""

    if prefix.endswith("/"):
        return key.startswith(prefix) or key + "/" == prefix
    return key == prefix


def _declared_loopback(family: str, target: str) -> bool | None:
    """网络目标是否回环地址（127.0.0.0/8、::1，含映射到回环的 IPv6）；解析不出返回 None。"""

    import ipaddress

    # 写法：IPv4 为「地址」或「地址:端口」；IPv6 为「[地址]:端口」或不带端口的「地址」（不带方括号的「地址:端口」有歧义，
    # 整串按地址解析，解析不出就判失败）。
    host = target
    if target.startswith("["):
        host = target[1:].split("]", 1)[0]
    elif family == "AF_INET" and target.count(":") == 1:
        host = target.split(":", 1)[0]
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return None
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped is not None and mapped.is_loopback))


def declared_coverage(sample: Mapping[str, Any], scope: Mapping[str, Any], *, command_sha256: str | None = None) -> dict[str, Any]:
    """覆盖检查（C1～C7）：采样证据里的每条访问是否落在声明范围内。**纯函数**，执行侧（登记自检、正式执行）与验证侧
    （``_check_declared_audit`` 重算）共用同一个实现；返回 ``{"ok": bool, "undeclared": [{rule, path, kinds, reason}],
    "counts": {...}}``。``undeclared`` 按（规则, 路径）排序并截断到前 200 条，总数在 ``counts["undeclared"]``。

    证据或范围格式不合格也不抛异常，而是记一条 ``C0-scope``／``C7-format`` 未覆盖——调用方不必再包 try。``command_sha256``
    给定时（登记表里登记的命令摘要），C7 另核证据的命令摘要与它相等。

    仓库外路径的键沿用采样器的约定：以 / 结尾是目录聚合（该目录下直接子项的访问，目录自身被枚举也算在它名下），不以 /
    结尾是精确路径（只出现在 /dev、/proc、/sys、/etc、``{HOME}`` 下和别名根自身）。

    * **C1 仓库内路径**（``{TREE}/相对路径``；``{TREE}/.git/objects/`` 是对象库的聚合键）：``.git/`` 下的访问只在声明了
      ``repo.git_history`` 时允许且不得写、不得执行；命中某个 range（include 命中且不被 exclude 命中）或 files 的路径，读／执行
      要求「已跟踪」（读了未跟踪文件，说明依赖不在摘要里），写入一律违规（命令不得改写声明范围内的文件）；命中 ``make_expansion``
      的 ``Makefile`` 视为被展开合同覆盖；声明内容的上级目录只看元数据（stat／dir）放行；枚举目录（list）要求目录本身被某个
      range 覆盖或列在 ``list_ok``；其它路径违规。
      ``repo_ignored`` 命中的路径（命令的输出区：以 / 结尾的目录前缀，如 ent 代码生成在测试树里写、读回、运行结束即删的临时目录；
      或仓库相对 glob，如 vite 在 frontend/ 根下写、读回、再删的 ``vitest.config.ts.timestamp-*``）：允许写；允许 read／exec／
      list，**但只限本次采样里同一路径也出现了 write 的路径**（命令自己写出再读回，不是外部输入）；只读不写的未跟踪路径仍然违规
      （``C1-untracked-read``）；已跟踪文件出现在这里是登记错误。
      ``repo.installed_dirs`` 前缀（依赖安装目录，例如 pnpm 的 ``frontend/node_modules/``，里面是未跟踪路径）下：read／exec／
      stat／dir／list／missing **不要求已跟踪**（内容由对应的 ``cache:*`` 合同绑定，不靠范围摘要）；写入不允许
      （``C4-write-installed``），除非同时落在 ``repo_ignored`` 前缀内（例如 ``frontend/node_modules/.vite/`` 的 vitest 缓存）；
      前缀之外的未跟踪读取照旧违规。
      ``repo.root_listing`` 为真时：测试树根目录自身（``{TREE}``，不含子项）的 dir／list／stat／read 放行（写入、执行照旧违规），
      树根直接子项（路径恰为 ``{TREE}/<单个名字>``）的 missing 探测放行——make 在树根找隐含规则候选（``RCS``、``SCCS``）、pnpm／
      eslint 在树根找配置候选，这些名字「不存在」由输入里的 ``scope:root-listing`` 条目绑定（同名的跟踪文件一出现摘要就变）。
      为假或缺省时与从前逐字相同。树根以下的 missing（``{TREE}/src/…``）、非声明目录的 stat 都不放行：仍要被 range 覆盖。
    * **C2 仓库外路径**：取 ``outside`` 里最长前缀匹配的项，访问类型必须在该 class 允许的集合内。没有前缀命中时：
      某个已声明 ``outside`` 前缀、测试树、``{WORK}``、``{HOME}``、``{TMP}``（取自证据的 aliases）的**祖先目录**，且只有 stat／dir，
      视为覆盖通过（与仓库内「声明内容的上级目录算覆盖」同构）；**测试树的严格祖先目录**（``/``、``/var/``、``/var/tmp/``、``{WORK}``
      等键）还放行 list／read（目录自身被枚举、被打开；Node、Go 向上找工作区根与模块根），不含它们的后代；
      **噪声探测**：访问类型只有 missing／stat／dir 的键，满足下列任一条放行（不要求声明前缀）——路径含 ``node_modules`` 路径段
      （Node／TypeScript／ESLint 的模块解析候选）；路径在 ``_DECLARED_PROBE_ROOTS`` 之下（``/usr/``、``/opt/``、``/var/`` 等系统目录：
      PATH 搜索、cgo 编译器搜索路径、解析器配置的探测，系统状态不绑定、由环境指纹近似，与 system 类同口径）；路径（不分大小写）
      等于、位于证据 aliases 里 TREE／WORK／HOME／TMP 路径之下，或是这些路径的祖先（含 ``/``；TypeScript 会对自身路径做大小写变体探测）。
      写入、执行、对已存在的非祖先目录的 read 不在放行之列，仍要有声明前缀；其余违规 ``C2-outside-undeclared``。
    * **C3 missing 探测**：仓库内 missing 路径必须被 range 覆盖，或属祖先探测（树根下的 go.mod／go.work／.hg 等），或（声明了
      ``repo.root_listing`` 时）是树根的直接子项。仓库外因为目录聚合丢了名字，沿测试树祖先链的目录（Go 往上找 go.work 与模块、
      版本控制目录会逐级探测）放宽到 stat／dir／missing（以及上面的 list／read）；噪声探测见 C2；其它目录里的 missing 违规。
    * **C4 写入**：只能写 performance-cache／scratch 类仓库外路径与 ``repo_ignored``；写入工具链、依赖内容、系统、HOME 配置、
      声明范围内的路径一律违规（命令在写依赖缓存＝在联网下载，违背离线合同）。唯一的例外是精确路径 ``/dev/null``：写空设备
      是无害的丢弃输出（采样器 merge 也按这个精确路径豁免），``/dev/`` 下其它路径照旧不许写。
      performance-cache 类除了读写还可**执行**：Go 1.24 起 ``go run``／``go tool`` 构建出的可执行文件缓存在 GOCACHE 里并从那里执行；
      Go 缓存是内容寻址、只影响速度的缓存，信任模型与读缓存相同。风险：有人能写 GOCACHE 就能替换缓存里的可执行文件，让命令去执行它
      ——这不在威胁模型内（能写工作根的人本来就能改一切）。toolchain／dependency-content／system／virtual／home-config 类的可执行性不变。
    * **C5 网络**：回环地址（AF_INET／AF_INET6）放行；AF_UNIX 目标必须列在 ``scope.unix_sockets``；AF_UNSPEC（UDP 套接字
      断开关联，不是外连）放行；其它外连与不可判的目标一律违规。
    * **C6 git 命令**：命令行里出现的 40 位提交号必须都在 ``repo.git_history.commits`` 里，不得引用 HEAD 一类浮动引用，
      argv 不可见（采样器留下的哨兵）也违规。
    * **C7 证据完整性**：采样器没有判这份采样无效（valid、invalid_reasons）、流已结束、unresolved 的原因全部属容忍集合
      （``DECLARED_TOLERATED_UNRESOLVED``）且总数与分项一致；其中 ``unfinished-calls-at-eof``（多线程进程退出时永远配不上的
      ``<unfinished ...>``）最多容忍 64 个，超过即无效，``unfinished_calls`` 也不得超出它能解释的部分；没有未归属的进程事件、
      行数大于零、退出码为零。"""

    findings: list[dict[str, Any]] = []
    counts: dict[str, Any] = {"repo_paths": 0, "outside_paths": 0, "classes": {}, "ignored_paths": 0, "installed_paths": 0, "git_paths": 0,
                              "exempt_paths": 0, "root_listing_paths": 0, "probe_paths": 0, "git_commands": 0, "network": 0}

    def report(rule: str, path: Any, kinds: Iterable[str], reason: str) -> None:
        findings.append({"rule": rule, "path": _declared_display(path), "kinds": sorted({_declared_display(kind) for kind in kinds}),
                         "reason": _declared_display(reason)})

    def finish() -> dict[str, Any]:
        ordered = sorted(findings, key=lambda item: (item["rule"], item["path"], item["reason"]))
        counts["undeclared"] = len(ordered)
        return {"ok": not ordered, "undeclared": ordered[:_DECLARED_MAX_FINDINGS], "counts": counts}

    try:
        scope = validate_declared_scope(scope)
    except ValueError as error:
        report("C0-scope", "", [], f"声明范围非法：{error}")
        return finish()
    try:
        sample = _declared_validate_sample(sample)
    except ValueError as error:
        report("C7-format", "", [], f"采样证据格式非法：{error}")
        return finish()

    # ---- C7：证据本身是否完整可信 ----
    coverage = sample["coverage"]
    if sample.get("valid") is False or sample.get("invalid_reasons"):
        shown = "；".join(str(item) for item in (sample.get("invalid_reasons") or [])[:3])
        report("C7-invalid", "", [], f"采样器自己判这份采样无效{'：' + shown if shown else ''}")
    if coverage["stream_complete"] is not True:
        report("C7-stream", "", [], "采样输出流没有完整结束")
    reasons = coverage["unresolved_reasons"]
    for reason in sorted(reasons):
        if not _declared_reason_tolerated(reason):
            report("C7-unresolved", reason, [], f"存在不可容忍的未解析原因（容忍集合 {list(DECLARED_TOLERATED_UNRESOLVED)}，以 : 结尾的按前缀匹配，其余精确匹配）")
    # 流结束时悬着的未配对调用（多线程进程退出时 strace 不为还在系统调用里的线程打印 resumed）：被容忍，但有上限。
    at_eof = reasons.get("unfinished-calls-at-eof", 0)
    if at_eof > _DECLARED_MAX_UNFINISHED_AT_EOF:
        report("C7-unresolved", "unfinished-calls-at-eof", [], f"流结束时悬着的未配对调用 {at_eof} 个，超过容忍上限 {_DECLARED_MAX_UNFINISHED_AT_EOF}")
    if coverage["unfinished_calls"] > at_eof:
        report("C7-unresolved", "", [], f"未配对的调用 {coverage['unfinished_calls']} 个，超出被容忍的 unfinished-calls-at-eof（{at_eof}）能解释的部分")
    if coverage["unresolved_process_events"]:
        report("C7-unresolved", "", [], f"有 {coverage['unresolved_process_events']} 个未归属的进程事件（子进程的事件没等到父进程的派生记录，读集不完整）")
    if coverage["unresolved"] != sum(reasons.values()):
        report("C7-unresolved", "", [], f"unresolved 总数 {coverage['unresolved']} 与各原因计数之和不符，存在未说明的未解析项")
    if coverage["lines"] <= 0:
        report("C7-lines", "", [], "采样没有产生任何输出行")
    if sample["exit_code"] != 0:
        report("C7-exit", "", [], f"被采样的命令退出码不是 0：{sample['exit_code']}")
    if command_sha256 is not None and sample["command"]["sha256"] != command_sha256:
        report("C7-command", "", [], "证据的命令摘要与登记的不符（命令变了，证据不再适用）")

    # ---- 声明范围的预处理 ----
    repo = scope["repo"]
    rules = [(item["include"], item["exclude"]) for item in repo.get("ranges", [])]
    files = set(repo.get("files", []))
    list_ok = {path.rstrip("/") for path in repo.get("list_ok", [])}
    ignored = list(repo.get("repo_ignored", []))
    ignored_dirs = [item for item in ignored if item.endswith("/")]
    ignored_globs = [_declared_glob(item) for item in ignored if not item.endswith("/")]
    installed = [item["prefix"] for item in repo.get("installed_dirs", [])]
    root_listing = repo.get("root_listing") is True
    history = repo.get("git_history")
    make_expansion = bool(scope.get("make_expansion"))
    outside = scope.get("outside", [])
    unix_sockets = set(scope.get("unix_sockets", []))

    def in_ranges(text: str) -> bool:
        return any(any(_matches(text, prefix) for prefix in include) and not any(_matches(text, prefix) for prefix in exclude)
                   for include, exclude in rules)

    def covered(relative: str) -> bool:
        # 目录自身（``relative + "/"``）也算：include 是目录前缀时，目录本身属于声明范围。
        return in_ranges(relative) or in_ranges(relative + "/") or relative in files or (make_expansion and relative == "Makefile")

    def under_ignored(relative: str) -> bool:
        # 目录前缀：其下的全部路径（含目录自身）；glob：整条相对路径完全匹配（不含它的子路径）。
        return (any(_matches(relative, prefix) or _matches(relative + "/", prefix) for prefix in ignored_dirs)
                or any(pattern.fullmatch(relative) for pattern in ignored_globs))

    def under_installed(relative: str) -> bool:
        return any(_matches(relative, prefix) or _matches(relative + "/", prefix) for prefix in installed)

    directories: set[str] = set()

    def add_ancestors(path: str, *, itself: bool) -> None:
        parts = path.rstrip("/").split("/")
        for count in range(1, len(parts) + (1 if itself else 0)):
            directories.add("/".join(parts[:count]))

    for include, _exclude in rules:
        for prefix in include:
            add_ancestors(prefix, itself=prefix.endswith("/"))
    for path in files:
        add_ancestors(path, itself=False)
    for path in list_ok:
        add_ancestors(path, itself=True)
    for item in ignored:
        if item.endswith("/"):
            add_ancestors(item, itself=True)
            continue
        literal: list[str] = []  # glob 的目录部分里，第一个带通配符的分量之前的字面目录
        for part in item.split("/")[:-1]:
            if "*" in part or "?" in part:
                break
            literal.append(part)
        if literal:
            add_ancestors("/".join(literal), itself=True)
    for prefix in installed:
        add_ancestors(prefix, itself=True)

    # ---- C1／C3／C4：仓库内路径 ----
    for path, raw_kinds, tracked in sample["repo_accesses"]:
        counts["repo_paths"] += 1
        kinds = set(raw_kinds)
        if kinds - DECLARED_KINDS:
            report("C0-kind", path, kinds, f"未知访问类型 {sorted(kinds - DECLARED_KINDS)}")
            continue
        if path == "{TREE}":
            relative = ""
        elif path == _DECLARED_GIT_OBJECTS_KEY:
            relative = ".git/objects"
        elif path.startswith("{TREE}/") and not path.endswith("/") and _declared_clean_path(path[len("{TREE}/"):]):
            relative = path[len("{TREE}/"):]
        else:
            report("C1-path", path, kinds, "仓库内路径必须是 {TREE}/ 加规范相对路径（无空、.、.. 分量，不以 / 结尾；对象库聚合键 {TREE}/.git/objects/ 除外）")
            continue
        if relative == "":
            if kinds <= {"stat", "dir"}:
                counts["exempt_paths"] += 1
            elif root_listing and kinds <= {"stat", "dir", "list", "read"}:
                counts["root_listing_paths"] += 1  # repo.root_listing：只放行树根目录自身，不含子项；写入、执行照旧违规
            else:
                report("C1-undeclared", path, kinds, "测试树根目录只允许 stat／dir（枚举或读取树根不在任何声明里；声明 repo.root_listing 后才许枚举与读取）")
            continue
        if relative == ".git" or relative.startswith(".git/"):
            if history is None:
                report("C1-git-undeclared", path, kinds, "读了 .git，但声明范围没有 repo.git_history")
            elif kinds - {"read", "stat", "dir", "list", "missing"}:
                report("C1-git-kinds", path, kinds, "对 .git 只允许读取类访问（read／stat／dir／list／missing），不得写入或执行")
            else:
                counts["git_paths"] += 1
            continue
        in_installed, in_ignored = under_installed(relative), under_ignored(relative)
        if in_installed or in_ignored:
            # 安装目录与输出区可以重叠（node_modules/.vite/）：写入按输出区放行，读取按安装目录放行。
            if in_ignored and tracked:
                report("C1-ignored-tracked", path, kinds, "repo_ignored 前缀下出现了已跟踪文件，它应属声明范围而不是输出区")
            elif "write" in kinds and not in_ignored:
                report("C4-write-installed", path, kinds, "命令写入了依赖安装目录（安装结果由合同绑定，不许改；缓存之类的输出须另登记 repo_ignored 前缀）")
            elif not in_installed and kinds & {"read", "exec", "list"} and "write" not in kinds:
                report("C1-untracked-read", path, kinds, "repo_ignored 前缀下只读不写的未跟踪路径：它不是本次命令自己产出的，内容不在任何摘要里"
                                                         "（自产物读回要求同一次采样里这个路径也出现 write）")
            else:
                counts["installed_paths" if in_installed else "ignored_paths"] += 1
            continue
        if covered(relative):
            if "write" in kinds:
                report("C4-write-declared", path, kinds, "命令写入了声明范围内的路径（声明范围是只读输入，输出须落在 repo_ignored 或 scratch）")
            elif kinds & {"read", "exec"} and not tracked:
                report("C1-untracked-read", path, kinds, "读了声明范围内的未跟踪文件，它的内容不在范围摘要里")
            continue
        if kinds == {"missing"} and relative in _DECLARED_ANCESTOR_PROBES:
            counts["exempt_paths"] += 1
            continue
        if relative in directories and kinds <= {"stat", "dir"}:
            counts["exempt_paths"] += 1
            continue
        if "list" in kinds and relative in list_ok and kinds <= {"list", "stat", "dir"}:
            counts["exempt_paths"] += 1
            continue
        if kinds == {"missing"}:
            if root_listing and "/" not in relative:
                counts["root_listing_paths"] += 1  # 树根的直接子项：它「不存在」由 scope:root-listing 条目绑定（同名跟踪文件一出现摘要就变）
                continue
            report("C3-missing-undeclared", path, kinds, "探测了声明范围外的不存在路径（它将来出现会改变命令行为）")
        elif "list" in kinds:
            report("C1-list-undeclared", path, kinds, "枚举了声明范围外的目录：须被某个 range 覆盖或列入 repo.list_ok")
        else:
            report("C1-undeclared", path, kinds, "读写了声明范围外的仓库内路径")

    # ---- C2／C3／C4：仓库外路径 ----
    # 键空间里的祖先目录集合。键用采样器的别名写法：WORK／HOME／TMP 之下的路径是 {WORK}/… 这样的别名路径，其余是绝对路径，
    # 所以先把证据 aliases 里的绝对路径按同样的规则（最长前缀优先）换成键的写法，再算祖先。
    roots: dict[str, str | None] = {}
    for name in ("TREE", "WORK", "HOME", "TMP"):
        value = sample["aliases"].get(name)
        roots[name] = value if isinstance(value, str) and value.startswith("/") and value == os.path.normpath(value) and value != "/" else None

    def to_key(absolute: str) -> str:
        best: tuple[str, str] | None = None
        for name in ("WORK", "HOME", "TMP"):
            base = roots[name]
            if base is not None and (absolute == base or absolute.startswith(base + "/")) and (best is None or len(base) > len(best[1])):
                best = (name, base)
        return absolute if best is None else "{" + best[0] + "}" + absolute[len(best[1]):]

    def ancestors_of(absolute: str) -> set[str]:
        pieces = absolute.split("/")
        return {to_key("/".join(pieces[:count]) or "/") for count in range(1, len(pieces))}

    declared_ancestors: set[str] = set()
    for entry in outside:
        parts = entry["prefix"].rstrip("/").split("/")
        for count in range(1, len(parts)):
            declared_ancestors.add("/".join(parts[:count]) or "/")
    for name, base in roots.items():
        if base is not None:
            declared_ancestors |= ancestors_of(base)
            if name != "TREE":
                declared_ancestors.add("{" + name + "}")
    tree_chain: set[str] = ancestors_of(roots["TREE"]) if roots["TREE"] is not None else set()
    alias_lowered = [base.lower() for base in roots.values() if base is not None]  # 证据 aliases 里可信的绝对路径，小写（TypeScript 会探测大写变体）

    def absolute_of(key: str) -> str | None:
        """键的绝对路径写法：绝对路径键原样；{WORK}／{HOME}／{TMP} 别名键换成证据 aliases 里的绝对路径；换不出来返回 None。"""

        alias = _DECLARED_ALIAS_PATH.fullmatch(key)
        if alias is None:
            return key if key.startswith("/") else None
        base = roots[alias.group(1)]
        return None if base is None else base + ("/" + alias.group(2) if alias.group(2) else "")

    def is_probe_noise(key: str) -> bool:
        """仓库外「噪声探测」：没有声明前缀命中、访问类型又只有 missing／stat／dir 的键，满足下列任一条就不要求声明——
        (c) 路径含 ``node_modules`` 路径段（Node／TypeScript／ESLint 的模块解析候选，包括指向宿主上不存在的 /work/… 的符号链接）；
        (a) 路径在 ``_DECLARED_PROBE_ROOTS`` 之下（系统状态，不绑定）；
        (b) 路径（不分大小写）等于、位于证据 aliases 里 TREE／WORK／HOME／TMP 路径之下，或是这些路径的祖先（含 /）。"""

        if "node_modules" in key.split("/"):
            return True
        absolute = absolute_of(key)
        if absolute is None:
            return False
        normal = absolute.rstrip("/") or "/"
        if any((normal + "/").startswith(root) for root in _DECLARED_PROBE_ROOTS):
            return True
        lowered = normal.lower()
        return lowered == "/" or any(lowered == base or lowered.startswith(base + "/") or base.startswith(lowered + "/") for base in alias_lowered)

    for path, raw_kinds, _count in sample["outside_accesses"]:
        counts["outside_paths"] += 1
        kinds = set(raw_kinds)
        if kinds - DECLARED_KINDS:
            report("C0-kind", path, kinds, f"未知访问类型 {sorted(kinds - DECLARED_KINDS)}")
            continue
        alias = _DECLARED_ALIAS_PATH.fullmatch(path)
        body = path[1:] if path.startswith("/") else (alias.group(2) or "x") if alias is not None else ""
        if path.startswith("{TREE}") or (path != "/" and not _declared_clean_path(body)):
            report("C2-path", path, kinds, "仓库外路径必须是绝对路径或 {WORK}／{HOME}／{TMP} 别名路径（规范、无 .. 分量；仓库内路径属 repo_accesses）")
            continue
        matches = [entry for entry in outside if _declared_covers(entry["prefix"], path)]
        if matches:
            entry = max(matches, key=lambda item: len(item["prefix"]))
            counts["classes"][entry["class"]] = counts["classes"].get(entry["class"], 0) + 1
            forbidden = kinds - DECLARED_CLASSES[entry["class"]]
            if path == _DECLARED_DEV_NULL:
                forbidden = forbidden - {"write"}  # 写空设备是无害的丢弃输出；只限这一个精确路径，/dev/ 下其它路径照旧不许写
            if forbidden:
                rule = "C4-write-class" if "write" in forbidden else "C2-kinds"
                report(rule, path, kinds, f"class={entry['class']}（前缀 {entry['prefix']}）不允许的访问类型 {sorted(forbidden)}")
            continue
        directory = path.rstrip("/") or "/"
        if kinds <= {"stat", "dir"} and directory in declared_ancestors:
            counts["exempt_paths"] += 1
            continue
        if kinds <= {"stat", "dir", "missing"} and directory in tree_chain:
            counts["exempt_paths"] += 1
            continue
        if kinds <= {"stat", "dir", "missing", "list", "read"} and directory in tree_chain:
            counts["probe_paths"] += 1  # 测试树的严格祖先目录：目录自身被枚举、被打开（Node 向上找工作区根、Go 找模块根）；后代不在此列
            continue
        if kinds == {"missing"} and os.path.basename(directory) in _DECLARED_ANCESTOR_PROBES and os.path.dirname(directory) in tree_chain:
            counts["exempt_paths"] += 1
            continue
        if kinds <= {"missing", "stat", "dir"} and is_probe_noise(path):
            counts["probe_paths"] += 1  # 噪声探测：写入、执行、对已存在的非祖先目录的 read 仍然要有声明前缀
            continue
        report("C3-missing-undeclared" if kinds == {"missing"} else "C2-outside-undeclared", path, kinds,
               "探测了没有声明前缀的仓库外不存在路径" if kinds == {"missing"} else "访问了没有声明前缀（outside）的仓库外路径")

    # ---- C5：网络 ----
    counts["network"] = len(sample["network"])
    for item in sample["network"]:
        family, target = item["family"], item["target"]
        if family == "AF_UNSPEC" and target == "":
            continue  # connect 的地址族是 AF_UNSPEC：UDP 套接字断开关联，不是外连
        if family in ("AF_INET", "AF_INET6"):
            loopback = _declared_loopback(family, target)
            if loopback is None:
                report("C5-network", target, [], f"{family} 目标解析不出地址")
            elif not loopback:
                report("C5-network", target, [], f"{family} 外连了非回环地址")
        elif family == "AF_UNIX":
            if target not in unix_sockets:
                report("C5-unix-undeclared", target, [], "AF_UNIX 目标没有列在声明范围的 unix_sockets 里")
        else:
            report("C5-network", target, [], f"不认识或不可判的网络协议族 {family}（只允许回环地址、登记的 AF_UNIX 目标与 AF_UNSPEC）")

    # ---- C6：git 命令 ----
    counts["git_commands"] = len(sample["git_commands"])
    declared_commits = set((history or {}).get("commits", []))
    for argv in sample["git_commands"]:
        if os.path.basename(argv[0]) != "git":
            report("C6-git", argv[0], [], "git_commands 里混入了非 git 命令")
            continue
        if _DECLARED_GIT_HIDDEN in argv:
            report("C6-git-argv-hidden", " ".join(argv), [], "git 子进程的 argv 不可见，读了哪些历史提交说不清")
            continue
        for argument in argv[1:]:
            for token in _DECLARED_COMMIT_TOKEN.findall(argument):
                if token.lower() not in declared_commits:
                    report("C6-commit-undeclared", token.lower(), [], "git 命令引用了没有登记在 repo.git_history.commits 里的提交")
            if _DECLARED_HEAD_REF.search(argument):
                report("C6-head", argument, [], "git 命令引用了 HEAD 一类浮动引用：声明级单元不依赖 HEAD")
    return finish()


def _declared_unavailable(value: Any) -> bool:
    """合同条目的 detail 里是否带探测失败的值（``unavailable(...)``，沿用入口步骤的约定）：探测失败的合同不能作承接依据。"""

    if isinstance(value, str):
        return value.startswith("unavailable")
    if isinstance(value, dict):
        return any(_declared_unavailable(item) for item in value.values())
    if isinstance(value, list):
        return any(_declared_unavailable(item) for item in value)
    return False


def _declared_contract_entry_problems(entry: Mapping[str, Any], kind: str, name: str) -> list[str]:
    """合同条目 ``contract:<类型>:<名字>`` 的格式核验：只核封存值，不探测当前环境（历史重放不能依赖当前磁盘、docker、git）。

    共同要求：摘要是 64 位十六进制；detail 带 ``schema_version``＝``declared-contract/v1``、``type``、``id``，且与条目名一致；
    detail 里没有探测失败的值。各类型再核调研 4.x 写明的字段，文档没写明的字段不猜（宽松），以免合法记录被误拒。"""

    label = f"合同条目 contract:{kind}:{name}"
    problems: list[str] = []
    if not isinstance(entry.get("sha256"), str) or not _DECLARED_SHA256.fullmatch(entry["sha256"]):
        problems.append(f"{label} 的摘要不是 64 位十六进制")
    detail = entry.get("detail")
    if not isinstance(detail, dict):
        return problems + [f"{label} 缺少 detail"]
    if detail.get("schema_version") != DECLARED_CONTRACT_SCHEMA or detail.get("type") != kind or detail.get("id") != name:
        return problems + [f"{label} 的 detail 须带 schema_version={DECLARED_CONTRACT_SCHEMA}、type={kind}、id={name}"]
    if _declared_unavailable(detail):
        problems.append(f"{label} 探测失败（unavailable），不能作承接依据")
    if kind == "toolchain":
        if not isinstance(detail.get("version_line"), str) or not detail["version_line"].strip():
            problems.append(f"{label} 缺少版本行 version_line")
        if not isinstance(detail.get("executable_sha256"), str) or not _DECLARED_SHA256.fullmatch(detail["executable_sha256"]):
            problems.append(f"{label} 的可执行文件摘要 executable_sha256 不是 64 位十六进制")
        if "requires" in detail:  # 例如打开离线要求时 toolchain:go 带 env_equals
            try:
                _declared_requires_shape(detail["requires"], f"{label}.requires")
            except ValueError as error:
                problems.append(str(error))
    elif kind == "cache":
        rule, role = detail.get("rule"), detail.get("role")
        if not isinstance(rule, str) or rule not in _DECLARED_CACHE_RULES:
            problems.append(f"{label} 的规则 rule 未知：{rule!r}（已知 {sorted(_DECLARED_CACHE_RULES)}）")
        elif _DECLARED_CACHE_RULES[rule] != role:
            problems.append(f"{label} 的角色 role={role!r} 与规则 {rule} 不匹配（应为 {_DECLARED_CACHE_RULES[rule]}）")
        if not isinstance(detail.get("location"), str) or detail["location"] not in _DECLARED_CACHE_LOCATIONS:
            problems.append(f"{label} 的 location 须是 {sorted(_DECLARED_CACHE_LOCATIONS)} 之一：{detail.get('location')!r}")
        for key in ("lock_file", "installed_lock"):
            if key in detail:
                try:
                    _declared_rel(detail[key], f"{label}.{key}", directory=False)
                except ValueError as error:
                    problems.append(str(error))
        if "requires" in detail:
            try:
                _declared_requires_shape(detail["requires"], f"{label}.requires")
            except ValueError as error:
                problems.append(str(error))
    elif kind == "service-image":
        if not isinstance(detail.get("image_id"), str) or not _DECLARED_IMAGE_ID.fullmatch(detail["image_id"]):
            problems.append(f"{label} 的镜像 image_id 不是 sha256:<64 位十六进制>")
        if not isinstance(detail.get("reference"), str) or not detail["reference"]:
            problems.append(f"{label} 缺少镜像引用 reference")
    elif kind == "git-history" and not _DECLARED_COMMIT.fullmatch(name):
        problems.append(f"{label} 的 ID 不是 40 位小写提交号")
    elif kind == "make-expansion" and not _DECLARED_MAKE_TARGET.fullmatch(name):
        problems.append(f"{label} 的 ID 不是 make 目标名")
    return problems


def _declared_requires_problems(requires: Mapping[str, Any], record: Mapping[str, Any], label: str) -> list[str]:
    """命令运行期要求（argv_contains、go_test_argv_contains、env_equals）对照记录里封存的单元规格与环境指纹：``spec.env`` 里
    显式写了的变量以它为准（单元环境覆盖执行器环境），否则看环境指纹里 ``envvar:<名字>`` 条目的摘要。``go_test_argv_contains``
    只在命令行里有相邻的 ``go``、``test`` 两个词时才核（与入口门禁侧 ``declared_contract_problems`` 同一判法），并且要求参数出现在
    ``go test`` 之后。"""

    problems: list[str] = []
    spec = record.get("spec") if isinstance(record.get("spec"), Mapping) else {}
    argv = spec.get("argv") if isinstance(spec.get("argv"), list) else []
    unit_env = spec.get("env") if isinstance(spec.get("env"), Mapping) else {}
    environment = record.get("environment")
    fingerprint = {str(item.get("name")): item.get("sha256") for item in (environment if isinstance(environment, list) else [])
                   if isinstance(item, Mapping)}
    for token in requires.get("argv_contains", []):
        if token not in argv:
            problems.append(f"{label} 要求命令行含 {token}，记录封存的 spec.argv 里没有")
    go_test = next((index for index in range(len(argv) - 1) if argv[index] == "go" and argv[index + 1] == "test"), None)
    if go_test is not None:
        for token in requires.get("go_test_argv_contains", []):
            if token not in argv[go_test:]:
                problems.append(f"{label} 要求 go test 命令含 {token}，记录封存的 spec.argv 里没有")
    for key, value in requires.get("env_equals", {}).items():
        if key in unit_env:
            if unit_env[key] != value:
                problems.append(f"{label} 要求环境变量 {key}={value}，记录封存的 spec.env 里是别的值")
        elif fingerprint.get("envvar:" + key) != sha256_bytes(f"{key}={value}".encode("utf-8")):
            problems.append(f"{label} 要求环境变量 {key}={value}，记录封存的环境指纹里不是这个值")
    return problems


def _declared_observed_problems(kind: str, name: str, observed: Mapping[str, Any], detail: Mapping[str, Any],
                                files: Mapping[str, str]) -> list[str]:
    """运行前凭证的 ``observed`` 是否满足合同规则（调研 4.2、4.3）。``files`` 是记录输入里 ``file:<路径>`` 条目的摘要：
    lock-derived 规则要求安装锁副本摘要不但与锁文件自己相等，还要等于输入里那个单文件条目的摘要，也就是「真实依赖」。"""

    label = f"合同凭证 {kind}:{name}"
    problems: list[str] = []
    if kind == "cache":
        rule = detail.get("rule")
        if rule in ("any-content-addressed", "any-scratch"):
            if observed.get("root_ok") is not True:
                problems.append(f"{label} 的缓存根位置核验未通过（observed.root_ok 须为 true）")
            hint = observed.get("entries_hint")
            if hint is not None and (not _declared_int(hint) or hint < 0):
                problems.append(f"{label} 的 observed.entries_hint 须是非负整数")
        elif rule in ("lock-derived-pnpm", "lock-derived-go-sum"):
            lock_file = detail.get("lock_file") or _DECLARED_LOCK_DEFAULTS[rule]
            installed_key, lock_key = (("installed_lock_sha256", "lock_file_sha256") if rule == "lock-derived-pnpm"
                                       else ("go_sum_sha256", "go_sum_sha256"))
            installed, locked = observed.get(installed_key), observed.get(lock_key)
            if not all(isinstance(item, str) and _DECLARED_SHA256.fullmatch(item) for item in (installed, locked)):
                problems.append(f"{label} 的 observed.{installed_key}／{lock_key} 须是 64 位十六进制")
            elif installed != locked:
                problems.append(f"{label} 的安装结果与锁文件不一致（{installed_key} 不等于 {lock_key}）")
            elif files.get(f"file:{lock_file}") != locked:
                problems.append(f"{label} 的锁文件摘要不等于记录输入里 file:{lock_file} 条目的摘要（锁文件须作为单文件输入列出且一致）")
            if rule == "lock-derived-go-sum" and observed.get("verify") not in ("ok", "skipped"):
                problems.append(f"{label} 的 observed.verify 须是 ok 或 skipped")
    elif kind == "service-image":
        if observed.get("image_id") != detail.get("image_id"):
            problems.append(f"{label} 的启动前镜像 Id 复核值与输入里的 image_id 不一致（镜像在 plan 之后被换过）")
    if _declared_unavailable(observed):
        problems.append(f"{label} 的 observed 带探测失败的值（unavailable）")
    return problems


def _declared_credential_problems(record: Mapping[str, Any], scope: Mapping[str, Any], contracts: Mapping[str, Mapping[str, Any]],
                                  files: Mapping[str, str]) -> list[str]:
    """记录新键 ``declared_contracts``（进 record_sha256、不进 inputs_sha256）的核验：运行前核验凭证数组，每项
    ``{id, kind, verified_before_run, observed, verified_at_utc}``（可另带 ``rule``，须与合同条目的规则一致）。

    * 必须有凭证的：``scope.contracts`` 里的 ``cache:*``、``service-image:*``（缓存根位置／安装锁副本／go.sum 初态、启动前镜像
      Id 复核都是单元启动前才能拍下的事实）；其余合同（工具链、host-config、git-history、make-expansion）已由 inputs 里的
      条目摘要绑定，凭证可有可无，有则同样核验；不在范围里的 ID 一律拒绝，重复拒绝；
    * ``verified_before_run`` 必须是 True，``verified_at_utc`` 可解析且不晚于记录的 started_at_utc（运行前核验）；
    * ``observed`` 必须满足合同规则（见 ``_declared_observed_problems``）。
    凭证缺失、为 false 或 observed 不满足规则，都使记录不可承接。"""

    raw = record.get("declared_contracts")
    items = [] if raw is None else raw
    if not isinstance(items, list):
        return ["声明级承接：declared_contracts 不是数组"]
    required = {contract for contract in scope.get("contracts", []) if contract.split(":", 1)[0] in ("cache", "service-image")}
    # 允许出现凭证的合同：声明范围列出的全部合同，加上由 git_history、make_expansion 派生的合同（后两者没有凭证也行）。
    allowed = set(scope.get("contracts", []))
    allowed.update(f"git-history:{commit}" for commit in (scope["repo"].get("git_history") or {}).get("commits", []))
    allowed.update(f"make-expansion:{target}" for target in scope.get("make_expansion", []))
    started = parse_utc(record.get("started_at_utc"))
    problems: list[str] = []
    given: set[str] = set()
    for index, item in enumerate(items):
        where = f"declared_contracts[{index}]"
        try:
            _declared_keys(item, required={"id", "kind", "verified_before_run", "observed", "verified_at_utc"}, optional={"rule"}, where=where)
        except ValueError as error:
            problems.append(f"声明级承接：{error}")
            continue
        kind, ident = item["kind"], item["id"]
        if kind not in (*_DECLARED_SCOPE_CONTRACT_TYPES, "git-history", "make-expansion") or not isinstance(ident, str) or not ident:
            problems.append(f"声明级承接：{where} 的 kind／id 非法：{kind!r}／{ident!r}")
            continue
        full = ident if ident.startswith(f"{kind}:") else f"{kind}:{ident}"
        if full not in allowed:
            problems.append(f"声明级承接：{where} 的合同 {full} 不在声明范围的合同里")
            continue
        if full in given:
            problems.append(f"声明级承接：合同凭证 {full} 重复")
            continue
        given.add(full)
        if item["verified_before_run"] is not True:
            problems.append(f"声明级承接：合同凭证 {full} 的 verified_before_run 不是 true（未在单元启动前核验）")
        verified = parse_utc(item["verified_at_utc"])
        if verified is None or (started is not None and verified > started):
            problems.append(f"声明级承接：合同凭证 {full} 的 verified_at_utc 不可解析或晚于单元启动时间")
        if not isinstance(item["observed"], dict):
            problems.append(f"声明级承接：合同凭证 {full} 的 observed 不是对象")
            continue
        entry = contracts.get(f"contract:{full}")
        detail = entry.get("detail") if isinstance(entry, Mapping) and isinstance(entry.get("detail"), dict) else {}
        if "rule" in item and item["rule"] != detail.get("rule"):
            problems.append(f"声明级承接：合同凭证 {full} 的 rule 与合同条目的规则不一致")
        problems.extend("声明级承接：" + text for text in _declared_observed_problems(kind, full.split(":", 1)[1], item["observed"], detail, files))
    for missing in sorted(required - given):
        problems.append(f"声明级承接：缺少合同凭证 {missing}（该合同的初态须在单元启动前核验并写进 declared_contracts）")
    return problems


def _check_declared_route(store: "RecordStore", path: Path, record: dict[str, Any] | None, current: "Current", facts: "RunFacts",
                          *, thorough: bool = True) -> list[str]:
    """``check_record`` 头部包装：当前输入里有 ``declared-inputs-scope`` 条目的单元（声明级承接）走这里。

    通用项（格式、自摘要、正式执行、单元、结论、规格、输入摘要、策略、环境、执行器、期限、原运行清单、日志）全部交给原
    ``check_record``——复制一份 facts，``require_read_audit`` 置假（声明级单元的读集判据不是旧的 all-file-paths/v1）、
    ``declared_route`` 置真（防递归）后调用一次，得到通用问题；然后追加声明级读集覆盖的问题（``_check_declared_audit``）。
    没有任何一项通用核对被绕过，只是替换了「读集覆盖」这一项的判据。"""

    import copy

    generic = copy.copy(facts)
    generic.require_read_audit = False
    generic.declared_route = True
    problems = check_record(store, path, record, current, generic, thorough=thorough)
    if record is None or (problems and not thorough):
        return problems
    return problems + _check_declared_audit(store, record, current, facts)


def _check_declared_audit(store: "RecordStore", record: Mapping[str, Any], current: "Current", facts: "RunFacts") -> list[str]:
    """声明级读集覆盖的消费者核验（只读封存物，不探测当前磁盘、docker、git；``facts.historical_read_audit`` 为真或假时做的事
    完全相同——当前环境与来源是否一致由通用的输入摘要比较保证）。返回问题列表，空＝通过。

    1. 范围条目：记录输入里恰有一个 ``declared-inputs-scope``，输入明细重算出的摘要等于 ``inputs_sha256``，条目的声明范围
       通过 ``validate_declared_scope``，条目摘要等于 ``sha256_json({scope, sample})``；声明了 ``repo.root_listing`` 的，输入里
       还要有恰好一个格式合格、摘要自洽的 ``scope:root-listing`` 条目（没声明就不该有）；
    2. ``read_audit``：``coverage_scope``＝``declared-inputs/v1``、覆盖闭合、状态通过、未声明数为零、绑定同一个
       ``inputs_sha256``、``sample_binding`` 与范围条目登记的单元／命令／范围／证据摘要逐项相等；
       ``sample_origin`` 取 ``registry``（缺省；证据就是登记表里的那份，``trace.sha256`` 必须等于登记的证据摘要）或 ``run``
       （逐次采样：证据是这一次运行自己采样的，``trace.sha256`` 是它的字节摘要，不要求等于登记摘要；``sample_binding.evidence_sha256``
       仍是登记的证据摘要）；
    3. 证据：从记录库取封存的采样证据（``trace.sha256`` 指的那份），字节摘要等于 ``trace.sha256``，格式合格，属于本单元，命令摘要
       等于登记的；采样器摘要：registry 来源只在证据自称了采样器时才要求等于登记的，run 来源要求必须有且等于登记的；
       **命令绑定**——记录封存的 spec（其摘要必须等于 spec_sha256）里的 argv 等于证据的 launcher＋argv，工作目录等于
       ``read_audit.repo_root``＋``cwd_rel``，也就是证据描述的正是这条记录实际执行的命令；
    4. **重算覆盖**：用 ``declared_coverage`` 对证据和范围重新计算，必须全部落在声明内。**不信任**证据或 ``read_audit`` 里写的
       结论：把 ``undeclared_count`` 改成 0 也过不了这一步；
    5. 合同：输入里的 ``contract:*`` 条目与声明范围要求的合同一一对应、格式合格（不探测当前环境）；
       ``declared_contracts`` 运行前凭证见 ``_declared_credential_problems``；命令运行期要求（requires）对照封存的 spec 核对；
       ``repo.installed_dirs`` 绑定的合同必须是依赖安装结果合同（role＝dependency-install）。"""

    problems: list[str] = []

    def fail(text: str) -> list[str]:
        problems.append("声明级承接：" + text)
        return problems

    # ---- 1. 范围条目 ----
    inputs = record.get("inputs")
    if not isinstance(inputs, list):
        return fail("记录没有输入明细")
    try:
        if entries_sha256(inputs) != record.get("inputs_sha256"):
            return fail("记录的输入明细重算出的摘要与 inputs_sha256 不符（明细被改过）")
    except (KeyError, TypeError):
        return fail("记录的输入明细格式非法")
    names = [item.get("name") for item in inputs if isinstance(item, Mapping)]
    if len(names) != len(set(names)):
        return fail("记录的输入明细里有重名条目")
    scoped = [item for item in inputs if isinstance(item, Mapping) and item.get("name") == DECLARED_SCOPE_ENTRY]
    if len(scoped) != 1:
        return fail(f"记录须恰含一个 {DECLARED_SCOPE_ENTRY} 条目（实际 {len(scoped)} 个）")
    entry = scoped[0]
    detail = entry.get("detail")
    if entry.get("category") != "policy" or not isinstance(detail, dict) or set(detail) != {"scope", "sample"}:
        return fail(f"{DECLARED_SCOPE_ENTRY} 条目的格式非法（须为 policy 类别，detail 恰含 scope 与 sample）")
    try:
        scope = validate_declared_scope(detail["scope"])
        reference = _declared_sample_ref(detail["sample"])
    except ValueError as error:
        return fail(f"{DECLARED_SCOPE_ENTRY} 条目的声明范围或采样引用非法：{error}")
    if entry.get("sha256") != sha256_json({"scope": scope, "sample": reference}):
        return fail(f"{DECLARED_SCOPE_ENTRY} 条目的摘要与它自带的范围全文、采样引用不符")
    # repo.root_listing：声明了就必须有且只有一个 scope:root-listing 条目（覆盖检查对树根枚举与树根下不存在候选名的放行，靠它绑定），
    # 没声明就不该有（range 不得叫 root-listing，不会误撞）。这里只核封存物自洽：名字清单是排序去重的字符串数组，摘要等于它的规范化
    # JSON 摘要；清单是否等于「当前」树根下被跟踪的顶层名字，由通用的输入摘要比较保证。
    listing = [item for item in inputs if isinstance(item, Mapping) and item.get("name") == DECLARED_ROOT_LISTING_ENTRY]
    if scope["repo"].get("root_listing"):
        names = listing[0].get("detail", {}).get("names") if len(listing) == 1 and isinstance(listing[0].get("detail"), dict) else None
        try:
            consistent = (len(listing) == 1 and listing[0].get("category") == "tests" and isinstance(names, list)
                          and all(isinstance(name, str) for name in names) and names == sorted(set(names))
                          and listing[0].get("sha256") == sha256_json(names))
        except ValueError:  # 名字里有无法编码的字符
            consistent = False
        if not consistent:
            problems.append(f"声明级承接：声明范围要求 repo.root_listing，但输入里没有恰好一个格式合格、摘要自洽的 {DECLARED_ROOT_LISTING_ENTRY} 条目"
                            f"（类别 tests，detail.names 为排序去重的字符串数组，摘要等于它的规范化 JSON 摘要；实际 {len(listing)} 个）")
    elif listing:
        problems.append(f"声明级承接：声明范围没有 repo.root_listing，输入里却有 {DECLARED_ROOT_LISTING_ENTRY} 条目")

    # ---- 2. read_audit ----
    audit = record.get("read_audit")
    if not isinstance(audit, Mapping):
        return fail("记录没有 read_audit")
    if (audit.get("coverage_scope") != DECLARED_COVERAGE_SCOPE or audit.get("coverage_complete") is not True
            or audit.get("status") != "passed" or not _declared_int(audit.get("undeclared_count")) or audit["undeclared_count"] != 0
            or audit.get("undeclared") not in (None, [])):
        return fail(f"read_audit 不是通过的 {DECLARED_COVERAGE_SCOPE} 覆盖（coverage_scope、coverage_complete、status、undeclared_count 须全部成立）")
    if audit.get("inputs_sha256") != record.get("inputs_sha256"):
        return fail("read_audit 绑定的 inputs_sha256 与记录不符")
    origin = audit.get("sample_origin", "registry")
    if origin not in ("registry", "run"):
        return fail(f"read_audit.sample_origin 只能是 registry 或 run：{audit.get('sample_origin')!r}")
    repo_root = audit.get("repo_root")
    if not isinstance(repo_root, str) or not repo_root.startswith("/"):
        return fail("read_audit.repo_root 缺失或不是绝对路径")
    trace, binding = audit.get("trace"), audit.get("sample_binding")
    if not isinstance(trace, Mapping) or not isinstance(trace.get("sha256"), str) or not _DECLARED_SHA256.fullmatch(trace["sha256"]):
        return fail("read_audit.trace.sha256 缺失或不是 64 位十六进制")
    try:
        _declared_keys(binding, required={"unit_id", "command_sha256", "scope_sha256", "evidence_sha256"}, where="read_audit.sample_binding")
    except ValueError as error:
        return fail(str(error))
    digest = trace["sha256"]
    expected_binding = {"unit_id": record.get("unit_id"), "command_sha256": reference["command_sha256"],
                        "scope_sha256": sha256_json(scope), "evidence_sha256": reference["evidence_sha256"]}
    if origin == "run":
        # run 来源（逐次采样）：trace.sha256 是这一次运行自己采样的证据的字节摘要，与登记表里的证据通常不同字节，所以不要求它等于
        # 登记的证据摘要；sample_binding.evidence_sha256 仍须是范围条目登记的那份证据的摘要，说明这次运行是对着哪份登记证据与哪份
        # 范围核的。本次证据自己的合格与否，由下面第 3、4 步核（封存、摘要、格式、单元、命令、采样器、重算覆盖）。
        if binding != expected_binding:
            return fail("read_audit（run 来源）的 sample_binding 与范围条目登记的单元、命令、范围、登记证据摘要不一致")
    elif binding != expected_binding or digest != reference["evidence_sha256"]:
        return fail("read_audit 的证据绑定（sample_binding、trace.sha256）与范围条目登记的单元、命令、范围、证据摘要不一致")

    # ---- 3. 证据与命令绑定 ----
    stored = store.log_path(digest)
    try:
        if not stored.is_file() or file_sha256(stored) != digest:
            return fail("封存的采样证据缺失或摘要漂移")
        sample = _declared_validate_sample(json.loads(stored.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        return fail(f"封存的采样证据读不出来或格式非法：{error}")
    command = sample["command"]
    if sample["unit_id"] != record.get("unit_id"):
        return fail("封存的采样证据属于别的单元")
    sampler = sample["sampled"].get("sampler_sha256")
    if origin == "run":
        # run 来源：采样是这一次运行自己做的，所用采样器必须就是范围条目登记的那一个；证据里没写采样器摘要同样不可承接。
        if sampler != reference["sampler_sha256"]:
            problems.append("声明级承接：本次运行的采样证据缺少采样器摘要，或与范围条目登记的不一致（run 来源要求采样器就是登记的那一个）")
    elif sampler is not None and sampler != reference["sampler_sha256"]:
        problems.append("声明级承接：采样证据自称的采样器摘要与范围条目登记的不一致")
    spec = record.get("spec")
    if not isinstance(spec, Mapping) or sha256_json(spec) != record.get("spec_sha256"):
        return fail("记录封存的 spec 与 spec_sha256 不符，无法据它核对命令")
    if record.get("unit_type") != "command" or spec.get("type") != "command":
        return fail("声明级承接只适用于命令单元")
    if command["sha256"] != reference["command_sha256"]:
        return fail("采样证据的命令摘要与范围条目登记的不符")
    if spec.get("argv") != command["launcher"] + command["argv"]:
        return fail("记录执行的命令行不是采样证据描述的那条命令（spec.argv 不等于证据的 launcher＋argv）")
    working = os.path.normpath(os.path.join(repo_root, "" if command["cwd_rel"] == "." else command["cwd_rel"]))
    if not isinstance(spec.get("cwd"), str) or os.path.normpath(spec["cwd"]) != working:
        return fail("记录执行的工作目录不是采样证据描述的那个（read_audit.repo_root＋cwd_rel 与 spec.cwd 不符）")

    # ---- 4. 重算覆盖（不信任任何已写下的结论）----
    result = declared_coverage(sample, scope, command_sha256=reference["command_sha256"])
    if not result["ok"]:
        shown = "；".join(f"{item['rule']} {item['path'] or item['reason']}" for item in result["undeclared"][:3])
        problems.append(f"声明级承接：重算读集覆盖不成立（共 {result['counts']['undeclared']} 处未覆盖，例如 {shown}）")

    # ---- 5. 合同条目、运行前凭证、运行期要求 ----
    expected = {f"contract:{contract}": tuple(contract.split(":", 1)) for contract in scope.get("contracts", [])}
    for commit in (scope["repo"].get("git_history") or {}).get("commits", []):
        expected[f"contract:git-history:{commit}"] = ("git-history", commit)
    for target in scope.get("make_expansion", []):
        expected[f"contract:make-expansion:{target}"] = ("make-expansion", target)
    present = {item["name"]: item for item in inputs if isinstance(item, Mapping) and str(item.get("name")).startswith("contract:")}
    for name in sorted(set(expected) - set(present)):
        problems.append(f"声明级承接：缺少声明范围要求的合同条目 {name}")
    for name in sorted(set(present) - set(expected)):
        problems.append(f"声明级承接：多出声明范围没有要求的合同条目 {name}")
    requirements: list[tuple[Any, str]] = [(scope["requires"], "声明范围的 requires")] if "requires" in scope else []
    for name in sorted(set(expected) & set(present)):
        kind, ident = expected[name]
        problems.extend("声明级承接：" + text for text in _declared_contract_entry_problems(present[name], kind, ident))
        contract_detail = present[name].get("detail")
        if kind in ("cache", "toolchain") and isinstance(contract_detail, dict) and "requires" in contract_detail:
            requirements.append((contract_detail["requires"], f"合同 {name} 的 requires"))
    for requires, label in requirements:
        try:
            _declared_requires_shape(requires, label)
        except ValueError:
            continue  # 形状非法已在合同条目核验里报过
        problems.extend("声明级承接：" + text for text in _declared_requires_problems(requires, record, label))
    # 依赖安装目录里的未跟踪读取，唯一的凭据是它绑定的合同；合同必须真的是「安装结果由锁文件决定」那一类（role＝dependency-install，
    # 规则 lock-derived-pnpm），否则一个空洞的合同（比如 any-scratch）就能放行任意未跟踪读取。
    for item in scope["repo"].get("installed_dirs", []):
        entry = present.get(f"contract:{item['contract']}")
        contract_detail = entry.get("detail") if isinstance(entry, Mapping) else None
        if isinstance(contract_detail, dict) and contract_detail.get("role") != "dependency-install":
            problems.append(f"声明级承接：安装目录 {item['prefix']} 绑定的合同 {item['contract']} 不是依赖安装结果合同"
                            "（role 须为 dependency-install，规则 lock-derived-pnpm）")
    files = {str(item["name"]): str(item["sha256"]) for item in inputs if isinstance(item, Mapping) and str(item.get("name")).startswith("file:")}
    problems.extend(_declared_credential_problems(record, scope, present, files))
    return problems


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    verify = sub.add_parser("verify", help="重验一次运行的清单")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--store", type=Path, default=None, help="记录库（默认取清单里登记的）")
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    manifest = _read_json(args.manifest)
    if manifest is None:
        print(json.dumps({"status": "failed", "problems": ["清单读不出来"]}, ensure_ascii=False))
        return 1
    root = args.store or (Path(manifest["record_store"]) if manifest.get("record_store") else None)
    problems = verify_manifest(manifest, store=RecordStore(root) if root else None)
    print(json.dumps({"status": "passed" if not problems else "failed", "mode": manifest.get("mode"), "run_id": manifest.get("run_id"),
                      "units": len(manifest.get("units") or []), "problems": problems}, ensure_ascii=False, indent=1))
    return 0 if not problems else 1


if __name__ == "__main__":
    sys.exit(main())
