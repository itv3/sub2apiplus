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
MODES = (FULL_SET_PASS, RE_EXECUTE)
MODE_LABELS = {FULL_SET_PASS: "全集通过", RE_EXECUTE: "重新执行全集"}
# 承接期限：默认 7 天（方案）；命令行只能调小，不能放宽。
MAX_AGE_HOURS = 168.0
PASSING_OUTCOMES = frozenset({"passed", "skipped", "expected_failure"})
MISSING = "missing"
HERE = Path(__file__).resolve().parent
# 执行器版本：执行器目录里参与调度、判定、输入解析与门禁编排的文件（存在的才算；字节码预编译工具另由执行器传入）。
# 审计结论影响记录是否通过及覆盖是否闭合，审计合同必须进入执行器身份。
EXECUTOR_FILES = ("unit_executor.py", "unit_records.py", "read_audit.py", "read_audit_runtime.py", "entry_steps.py", "entry_gates.py", "entry-gates.sh", "full_set_receipt.py", "target_platform_gate.py")
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
    """门禁清单为命令单元声明的输入：

    * ``ranges``：仓库范围；``files``：仓库里的单个文件（``{category, path}``）；``head``：HEAD 提交；
    * ``test_modules``：测试模块文件（仓库相对路径），按与采集测试单元相同的静态依赖闭包展开（E3-02 的 pre-A3 场景
      单元用），不加 HEAD——场景在数据根运行，读不到 git；受管工具树由模块所在的 ``tests`` 目录的上一级确定，
      同一受管树的依赖分析经 ``deps_cache`` 共用；
    * ``resolved``：清单生成时已算好的明细（例如数据根才有的内容）。"""

    allowed = {"ranges", "files", "test_modules", "head", "resolved", "host_paths", "require_read_audit", "runtime_contract"}
    if not isinstance(declaration, Mapping) or set(declaration) - allowed:
        raise RecordsError(f"输入声明非法：{declaration!r}")
    paths = declaration.get("host_paths", [])
    if (not isinstance(paths, list) or not all(isinstance(path, str) for path in paths)
            or len(paths) != len(set(paths)) or not isinstance(declaration.get("require_read_audit", False), bool)):
        raise RecordsError("宿主路径须为无重复的数组，完整审计要求须为布尔值")
    entries = [repo.range_entry(spec) for spec in declaration.get("ranges") or []]
    for path in paths:
        entries.append(_audit_module().host_snapshot(path))
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


def validate_audit_policy(policy: Any, command_ids: set[str]) -> dict[str, Any]:
    """登记已建合同的命令；所有未登记单元只能重跑，配置不能静默放开新增单元。"""
    if (not isinstance(policy, dict) or set(policy) != {"schema_version", "default", "units"}
            or policy.get("schema_version") != "unit-audit-policy/v1" or policy.get("default") != "reexecute-only"
            or not isinstance(policy.get("units"), dict) or set(policy["units"]) - command_ids):
        raise RecordsError("读集策略须默认重跑，且只能登记当前真实命令单元")
    for unit_id, declaration in policy["units"].items():
        if (not isinstance(declaration, dict) or set(declaration) != {"host_paths", "require_read_audit", "runtime_contract"}
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
    return policy


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
                replay = _audit_module().strict_audit_reads(json.loads(stored_trace.read_text()), current.inputs,
                    repo_root=str(audit.get("repo_root") or "/unbound-repo"), data_root=audit.get("data_root"))
                if not replay["coverage_complete"]:
                    raise RecordsError("完整读集轨迹不能按当前输入重放")
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


def verify_manifest(manifest: Mapping[str, Any], *, store: RecordStore | None) -> list[str]:
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
                     now=parse_utc(manifest.get("decided_at_utc")) or 0.0)
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
        if disposition == "inherited" and mode != FULL_SET_PASS:
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
