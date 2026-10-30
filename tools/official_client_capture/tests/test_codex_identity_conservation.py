"""Codex 身份守恒门禁（Claude 升级控制面设计 D7、B1-07）。基线随 Codex 目标版本登记重封。

一、守什么

Codex 的采集与评估身份不得被 Claude 升级控制面或其它任何后续改动改变。以基线为准，当前受管树必须同时满足四条：

① 产出身份逐字相等：``wire_producer_sha256``、``evidence_semantics_sha256``、编排器的 wire／evidence 两个闭包摘要、
   ``checker_sha256``、``builder_sha256``、``compare_reader_sha256``、``accept_reader_sha256`` 与基线相同。没有任何过渡登记，
   也没有“登记过的后值”：只要变了，就是红。
② 受管树逐文件变化必须登记：相对基线新增、删除、修改的受管文件，只有 control 层的才允许变化，而且要逐个写进
   ``REGISTERED_CONTROL_CHANGES``（受管树相对路径 → 工作项与理由）。wire、evidence 层的任何变化一律拒绝；不允许出现
   按默认层归类的文件（``defaulted_paths`` 非空）；策略摘要变化必须对应策略文件本身的登记变化；登记项必须真的相对基线变了，
   过期登记同样拒绝。被 Codex 策略忽略的文件（``claude_`` 前缀等 Claude 专属文件）不影响 Codex 身份，无须登记。
③ 迁入共享模块 ``upgrade_control_plane.py`` 的函数与常量不得属于 wire、evidence 或评估读侧任何一个闭包。
④ ``tool-evolution`` 预览按编排器 wire 闭包摘要判定 ``wire_closure_changed``，基线之后必须仍为 false。

二、基线与重封规则

基线是某个提交上 Codex 受管树的身份快照，固化在 ``fixtures/codex_identity_conservation_baseline_<目标版本>.json``，文件的
SHA-256 钉在 ``BASELINE_SHA256``。字段与封存入口见下面第三节。

Codex 每登记一个新的目标版本，登记本身（新版本的场景清单、候选期望、事实映射、证据标签、模型表、编排器的受支持升级对与文件名
清单，以及该版本的定向样本）必然改变 wire／evidence 层、编排器闭包与读侧闭包。这是预期的身份变化，不是守恒违规；
所以基线跟着目标版本走，规则只有三条：

1. 每次 Codex 目标版本登记完成（含该版本的定向样本在内、wire／evidence／编排器的改动全部落定）之后、建 Campaign 之前，
   重封一次基线。
2. 重封只许出现在该版本登记的变更集里。登记之外的任何时候都不得改基线、不得新增过渡登记；基线之后的 Claude 或其它改动
   不得改变 Codex 的 wire／evidence 身份，control 层变化逐文件登记到 ``REGISTERED_CONTROL_CHANGES``。读侧闭包（评估器
   的 compare／accept 读取面）里的符号或整文件变化，会让两个读侧摘要变化，同样属于身份变化：要么回退，要么等下一次重封。
3. 重封时 ``REGISTERED_CONTROL_CHANGES`` 清空（登记表是“相对当前基线”的变化清单，基线换了就从零开始），新基线换新文件名
   （带目标版本号）。

旧基线 ``codex_identity_conservation_baseline.json`` 与五份读侧过渡登记（``..._reader_transition_*.json``）、一份 evidence
层过渡登记（``..._evidence_transition_20261019.json``）是上一代守恒门禁的历史原件，原样留在 fixtures 目录，不改、不删；
本模块不再读取它们，也不再为它们钉摘要（其中 evidence 层过渡登记还被 v0.2.17 源码迁移台账按摘要冻结，删它要走带删除证明的承接）。
新基线文件本身不在冻结台账里，它的完整性由本模块里钉住的 ``BASELINE_SHA256`` 保证，而本模块是冻结路径：改钉住值须追加冻结承接收据。
控制面设计附录 E 第 6 条把“合并两边各自实现的读取与校验函数”定在 Codex 某次新目标版本升级之前，重封只是把基线换到新版本登记之后的状态，
不等于做了那次合并。

登记 control 层变化：基线之后确需改动 control 层文件（包括新增、删除）时，在 ``REGISTERED_CONTROL_CHANGES`` 加一行“路径 → 工作项与理由”，
理由要写明为什么这次改动不会影响 Codex 的 wire／evidence／读侧身份；该文件若落在 wire 或 evidence 层，或改动让上面任何一个身份摘要变化，
登记也放行不了，只能回退或等重封。

三、封存入口（在要封存的受管树所在的 git 工作树里，仓库根目录下运行）::

    PYTHONDONTWRITEBYTECODE=1 python3 -m tools.official_client_capture.tests.test_codex_identity_conservation \\
        --seal-baseline tools/official_client_capture/tests/fixtures/codex_identity_conservation_baseline_<版本>.json [--replace]

* 取受管树当前状态按基线同构的 17 个顶层键算出基线：``schema_version``、``source_commit``（封存时的 HEAD 完整提交号）、
  ``policy_version``、``policy_sha256``、``files_sha256``、``wire_producer_sha256``、``evidence_semantics_sha256``、
  ``control_sha256``、``orchestrator_closure_sha256``（wire／evidence）、``checker_sha256``、``builder_sha256``、
  ``compare_reader_sha256``、``accept_reader_sha256``、``layer_counts``、``defaulted_paths``、``closure_symbols``、``entries``。
  序列化固定为缩进 1 空格、保留键序、末尾换行，与旧基线逐字节同一格式。
* 前置条件：受管树里被摘要覆盖的文件（``.py``／``.sh``／``.json``，不含 ``tests``、``versions``、``__pycache__``）必须与 HEAD
  的内容逐字一致，且没有未提交的受管文件，这样 ``source_commit`` 才真正标识被封存的字节；测试目录与文档的未提交改动不进摘要，
  允许存在（入口会提示）。``defaulted_paths`` 必须为空，先在 Codex 策略里登记层级再封存。
* 输出文件已存在时拒绝覆盖，重封同名文件要显式加 ``--replace``；输出不能落在受管树的摘要口径内。
* 封存后入口打印文件摘要：把它填进本模块的 ``BASELINE_SHA256``（输出文件名与 ``BASELINE_PATH`` 不同时一并改 ``BASELINE_PATH``），
  清空 ``REGISTERED_CONTROL_CHANGES``，再运行本模块全部测试；提交说明写明封存的 ``source_commit``。
* 不带 ``--seal-baseline`` 时按普通 unittest 运行：``python3 -m unittest tools.official_client_capture.tests.test_codex_identity_conservation``。

封存与比对共用同一个 ``compute_snapshot``，所以另有两道独立校验：封存算法（``compute_snapshot`` 加规范序列化）在旧基线的提交
（``17c87351a``）的受管树上复算过，结果与旧基线文件逐字节相同；``test_snapshot_agrees_with_production_tool_identity`` 把快照与
Codex 编排器自己的 ``_tool_identity`` 和评估器摘要函数逐字段对照。
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from unittest import mock

from tools.official_client_capture import codex_upgrade_tool_identity_policy as tip

TOOL_ROOT = Path(__file__).resolve().parents[1]
# 当前基线对应的 Codex 目标版本；重封换新文件名，旧基线原样保留（见模块说明第二节）。
BASELINE_CODEX_VERSION = "0.165.0"
BASELINE_PATH = Path(__file__).resolve().parent / "fixtures" / "codex_identity_conservation_baseline_0_165_0.json"
BASELINE_SHA256 = "5268c87bc28db8c4e7da8458e3e0efe91cd60dd8fe8e1cbfaf612517e3791c55"
BASELINE_SCHEMA = "codex-identity-conservation-baseline/v1"
# 基线的 17 个顶层键及其次序（与旧基线同构）：封存入口按这个次序写，测试按这个集合与次序校验。
BASELINE_KEYS = (
    "schema_version", "source_commit", "policy_version", "policy_sha256", "files_sha256",
    "wire_producer_sha256", "evidence_semantics_sha256", "control_sha256", "orchestrator_closure_sha256",
    "checker_sha256", "builder_sha256", "compare_reader_sha256", "accept_reader_sha256",
    "layer_counts", "defaulted_paths", "closure_symbols", "entries",
)
# 必须与基线逐字相等的六项产出身份摘要（另有 wire／evidence 两个编排器闭包摘要，见 ``identity_mismatches``）。
IDENTITY_SENSITIVE_FIELDS = (
    "wire_producer_sha256", "evidence_semantics_sha256",
    "checker_sha256", "builder_sha256", "compare_reader_sha256", "accept_reader_sha256",
)
READER_FIELDS = (("compare_reader_sha256", tip.EVALUATOR_COMPARE_READER_ROOTS), ("accept_reader_sha256", tip.EVALUATOR_ACCEPT_READER_ROOTS))
SHARED_CONTROL_PLANE = "upgrade_control_plane.py"
HEX40 = re.compile(r"[0-9a-f]{40}")
HEX64 = re.compile(r"[0-9a-f]{64}")
# 受管树摘要口径（与 ``tip._managed_tree_digests`` 同一口径）：后缀与被排除的目录名。
MANAGED_SUFFIXES = frozenset({".py", ".sh", ".json"})
MANAGED_EXCLUDED_PARTS = frozenset({"tests", "versions", "__pycache__"})
# 变异验证的临时副本树放在受管临时根 /private/tmp（CI 工作流会建立它）；没有这个目录的机器退回系统默认临时目录。
TEMP_ROOT = "/private/tmp" if Path("/private/tmp").is_dir() else None

# Codex 受管树相对当前基线的 control 层变化：受管树相对路径 → 工作项与理由。新增、删除、修改都要登记，且只能落在 control 层。
# 被 Codex 策略忽略的文件（``claude_`` 前缀等 Claude 专属文件）不影响 Codex 身份，无须登记。基线重封时清空（见模块说明第二节）。
REGISTERED_CONTROL_CHANGES: dict[str, str] = {}


# ----------------------------------------------------------------------
# 快照、比对与基线文档（封存入口与测试共用）
# ----------------------------------------------------------------------


class SealError(Exception):
    """封存前置条件不满足或输出不可写；入口把它打印成一行中文并以退出码 2 结束。"""


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """JSON 对象出现重复键就拒绝，避免后一个值悄悄盖掉前一个。"""

    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("JSON 对象键重复")
    return result


def _read_regular_file(path: Path) -> bytes:
    """读取普通文件的字节；符号链接或非普通文件不接受（基线不能经链接偷换）。"""

    if path.is_symlink() or not path.is_file():
        raise ValueError(f"必须是普通文件：{path}")
    return path.read_bytes()


def compute_snapshot(tool_root: Path = TOOL_ROOT) -> dict[str, Any]:
    """按基线同构的字段计算一棵 Codex 受管树的身份快照（不含 ``schema_version`` 与 ``source_commit``）。

    全部取自 Codex 自己的身份引擎公开函数（策略加载、受管树摘要、``compute_identity_v2``、编排器闭包、读侧闭包），与
    ``codex_upgrade._tool_identity`` 和评估器摘要函数同一口径。``closure_symbols.evaluator_reader`` 取两个读侧闭包在编排器模块内
    的符号并集（按种类分函数与常量、按名字排序）；这个口径在旧基线的提交上复算过，与旧基线逐项一致。
    """

    root = Path(tool_root)
    policy = tip.load_policy(root / tip.POLICY_FILENAME)
    digests = tip._managed_tree_digests(root)
    for relative in (tip.EVALUATOR_CHECKER_RELATIVE, tip.EVALUATOR_BUILDER_RELATIVE):
        if relative not in digests:
            raise ValueError(f"评估器直接依赖不在受管树内：{relative}")
    entries = [{"path": path, "sha256": digest} for path, digest in sorted(digests.items())]
    identity = tip.compute_identity_v2(policy, root, entries)
    wire = tip.orchestrator_closure(policy, root, list(policy["orchestrator"]["wire_roots"]), digests, layer="wire_producer")
    evidence = tip.orchestrator_closure(policy, root, list(policy["orchestrator"]["evidence_roots"]), digests, layer="evidence_semantics")
    orchestrator = Path(str(policy["orchestrator"]["file"])).with_suffix("").as_posix().replace("/", ".")
    readers = {field: tip.evaluator_reader_closure(policy, root, list(roots), digests) for field, roots in READER_FIELDS}
    reader_symbols: dict[str, set[str]] = {"function": set(), "constant": set()}
    for closure in readers.values():
        for record in closure["symbols"]:
            if record["module"] == orchestrator:
                reader_symbols[record["kind"]].add(record["name"])
    return {
        "policy_version": identity["policy_version"],
        "policy_sha256": identity["policy_sha256"],
        "files_sha256": tip._fingerprint({"entries": entries}),
        "wire_producer_sha256": identity["wire_producer_sha256"],
        "evidence_semantics_sha256": identity["evidence_semantics_sha256"],
        "control_sha256": identity["control_sha256"],
        "orchestrator_closure_sha256": {
            layer: identity["orchestrator_closures"][layer]["closure_sha256"] for layer in ("wire_producer", "evidence_semantics")},
        "checker_sha256": digests[tip.EVALUATOR_CHECKER_RELATIVE],
        "builder_sha256": digests[tip.EVALUATOR_BUILDER_RELATIVE],
        "compare_reader_sha256": readers["compare_reader_sha256"]["closure_sha256"],
        "accept_reader_sha256": readers["accept_reader_sha256"]["closure_sha256"],
        "layer_counts": identity["layer_counts"],
        "defaulted_paths": identity["defaulted_paths"],
        "closure_symbols": {
            "wire_producer": {"functions": [item["name"] for item in wire["functions"]],
                              "constants": [item["name"] for item in wire["constants"]]},
            "evidence_semantics": {"functions": [item["name"] for item in evidence["functions"]],
                                   "constants": [item["name"] for item in evidence["constants"]]},
            "evaluator_reader": {"functions": sorted(reader_symbols["function"]), "constants": sorted(reader_symbols["constant"])},
        },
        "entries": entries,
    }


def identity_mismatches(baseline: Mapping[str, Any], snapshot: Mapping[str, Any]) -> list[str]:
    """① 产出身份是否逐字相等：返回变化项的中文说明清单，空表示全部相等。"""

    problems = [f"Codex 身份敏感摘要变化：{field}" for field in IDENTITY_SENSITIVE_FIELDS if snapshot[field] != baseline[field]]
    for layer, expected in baseline["orchestrator_closure_sha256"].items():
        if snapshot["orchestrator_closure_sha256"][layer] != expected:
            problems.append(f"Codex 编排器 {layer} 闭包变化")
    return problems


def tree_change_problems(policy: Mapping[str, Any], baseline: Mapping[str, Any], snapshot: Mapping[str, Any],
                         registered: Mapping[str, str]) -> list[str]:
    """② 受管树相对基线的逐文件变化是否都已登记且只落在 control 层；返回问题清单，空表示通过。

    变化包括新增、删除、修改。被策略忽略的路径（Claude 专属文件）放行；其余路径必须登记，且必须是 control 层。登记表本身也要
    诚实：登记项必须真的变了、不能是被忽略的路径、必须写明理由。按默认层归类的文件、策略摘要与策略文件变化不一致也算问题。"""

    before = {entry["path"]: entry["sha256"] for entry in baseline["entries"]}
    after = {entry["path"]: entry["sha256"] for entry in snapshot["entries"]}
    changed = sorted(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
    problems: list[str] = []
    for path in changed:
        layer = tip.classify_path(policy, path)
        if layer is None:
            continue  # 被 Codex 策略忽略的其它客户端专属文件，只进整树摘要，不影响 Codex 身份
        if path not in registered:
            problems.append(f"Codex 受管文件变化未登记：{path}（{layer} 层）")
        elif layer != "control":
            problems.append(f"Codex 受管文件变化不在 control 层：{path}（{layer} 层）")
    for path, reason in sorted(registered.items()):
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"登记项缺少工作项与理由：{path}")
        if path not in changed:
            problems.append(f"登记项相对基线没有变化，应从 REGISTERED_CONTROL_CHANGES 删除：{path}")
        elif tip.classify_path(policy, path) is None:
            problems.append(f"登记项属于被 Codex 策略忽略的路径，无须登记：{path}")
    # 按默认层归类的文件只可能是相对基线新增的（基线封存时 defaulted_paths 必须为空），所以只对变化的路径报；
    # 真实树上的 defaulted_paths 另有主测试直接断言为空。
    for item in tip.layer_entries(policy, snapshot["entries"])["defaulted"]:
        if item["path"] in changed:
            problems.append(f"新增受管文件按默认层归类，必须在 Codex 策略里登记层级：{item['path']}")
    if (snapshot["policy_sha256"] != baseline["policy_sha256"]) != (tip.POLICY_FILENAME in changed):
        problems.append("策略摘要变化必须对应策略文件的登记变化，二者不一致")
    return problems


def build_baseline_document(snapshot: Mapping[str, Any], *, source_commit: str) -> dict[str, Any]:
    """在快照前面加上 ``schema_version`` 与 ``source_commit``，得到按约定键序排列的基线文档。"""

    if not HEX40.fullmatch(source_commit):
        raise ValueError("source_commit 必须是 40 位小写十六进制的完整提交号")
    document = {"schema_version": BASELINE_SCHEMA, "source_commit": source_commit, **snapshot}
    if tuple(document) != BASELINE_KEYS:
        raise ValueError("基线字段集合或次序与约定不符")
    return document


def serialize_baseline(document: Mapping[str, Any]) -> bytes:
    """基线的规范序列化：缩进 1 空格、保留键序、不转义非 ASCII、末尾换行（与旧基线逐字节同格式）。"""

    return (json.dumps(document, indent=1, ensure_ascii=False) + "\n").encode("utf-8")


# ----------------------------------------------------------------------
# 封存入口：前置条件（受管树必须与 HEAD 一致）与写文件
# ----------------------------------------------------------------------


def _git(directory: Path, *args: str) -> str:
    try:
        completed = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, encoding="utf-8", check=False)
    except OSError as error:
        raise SealError(f"无法运行 git：{error}") from error
    if completed.returncode != 0:
        raise SealError(f"git {' '.join(args)} 失败：{completed.stderr.strip() or completed.stdout.strip()}")
    return completed.stdout.strip()


def _in_managed_digest_scope(relative: PurePosixPath) -> bool:
    """文件是否属于受管树摘要口径（与 ``tip._managed_tree_digests`` 一致：后缀符合且路径各段不含被排除目录名）。"""

    return relative.suffix in MANAGED_SUFFIXES and not MANAGED_EXCLUDED_PARTS.intersection(relative.parts)


def head_managed_digests(tool_root: Path) -> tuple[str, dict[str, str]]:
    """HEAD 提交里受管树的逐文件摘要：（完整提交号，路径 → SHA-256）。

    直接读 HEAD 的 blob 字节（``git ls-tree`` 加 ``git cat-file --batch``），不经检出、属性转换或归档过滤，与工作树无关；
    符号链接和子模块不在口径内。"""

    top = Path(_git(tool_root, "rev-parse", "--show-toplevel"))
    prefix = PurePosixPath(_git(tool_root, "rev-parse", "--show-prefix").rstrip("/"))
    commit = _git(top, "rev-parse", "--verify", "HEAD^{commit}")
    listing = subprocess.run(["git", "-C", str(top), "ls-tree", "-r", "-z", commit, "--", prefix.as_posix()],
                             capture_output=True, check=False)
    if listing.returncode != 0:
        raise SealError(f"git ls-tree 失败：{listing.stderr.decode('utf-8', errors='replace').strip()}")
    blobs: list[tuple[str, str]] = []
    for record in listing.stdout.split(b"\0"):
        if not record:
            continue
        header, _separator, raw_path = record.partition(b"\t")
        mode, kind, object_id = header.decode("ascii").split(" ")
        relative = PurePosixPath(raw_path.decode("utf-8")).relative_to(prefix)
        if kind == "blob" and mode in {"100644", "100755"} and _in_managed_digest_scope(relative):
            blobs.append((object_id, relative.as_posix()))
    digests: dict[str, str] = {}
    with subprocess.Popen(["git", "-C", str(top), "cat-file", "--batch"], stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        assert process.stdin is not None and process.stdout is not None
        for object_id, relative in blobs:
            process.stdin.write(object_id.encode("ascii") + b"\n")
            process.stdin.flush()
            _oid, _type, size = process.stdout.readline().split()
            data = process.stdout.read(int(size))
            process.stdout.read(1)  # 每个对象后面的换行
            digests[relative] = hashlib.sha256(data).hexdigest()
        process.stdin.close()
    return commit, digests


def managed_tree_head_problems(work: Mapping[str, str], head: Mapping[str, str]) -> list[str]:
    """工作树受管文件摘要与 HEAD 的差异（修改、未提交的新增、相对 HEAD 缺失）；空表示逐字一致。"""

    problems = [f"受管文件相对 HEAD 有未提交的修改：{path}" for path in sorted(work.keys() & head.keys()) if work[path] != head[path]]
    problems += [f"受管文件不在 HEAD 里（未提交或被忽略）：{path}" for path in sorted(work.keys() - head.keys())]
    problems += [f"HEAD 里的受管文件在工作树缺失：{path}" for path in sorted(head.keys() - work.keys())]
    return problems


def seal_baseline(output: Path, *, replace: bool = False, tool_root: Path = TOOL_ROOT) -> dict[str, Any]:
    """对受管树当前状态封存一份基线并写到 ``output``；返回封存摘要。前置条件不满足抛 ``SealError``，不写任何文件。"""

    output = Path(output)
    root = Path(tool_root).resolve()
    resolved = output.resolve()
    if root in resolved.parents and _in_managed_digest_scope(PurePosixPath(resolved.relative_to(root).as_posix())):
        raise SealError(f"输出文件落在受管树的摘要口径内，会改变被封存的内容：{output}")
    if not resolved.parent.is_dir():
        raise SealError(f"输出文件所在目录不存在：{resolved.parent}")
    if output.exists() or output.is_symlink():
        if not replace:
            raise SealError(f"输出文件已存在，拒绝覆盖（同名重封请显式加 --replace）：{output}")
        if output.is_symlink() or not output.is_file():
            raise SealError(f"输出路径不是普通文件：{output}")
    commit, head = head_managed_digests(root)
    snapshot = compute_snapshot(root)
    problems = managed_tree_head_problems({entry["path"]: entry["sha256"] for entry in snapshot["entries"]}, head)
    if problems:
        raise SealError("受管树与 HEAD 不一致，先提交或还原后再封存（source_commit 必须标识被封存的字节）：\n  " + "\n  ".join(problems[:20]))
    if snapshot["defaulted_paths"]:
        raise SealError("存在按默认层归类的受管文件，先在 Codex 策略里登记层级再封存：" + "、".join(snapshot["defaulted_paths"]))
    raw = serialize_baseline(build_baseline_document(snapshot, source_commit=commit))
    if replace:
        temporary = output.with_name(f".{output.name}.sealing")
        try:
            temporary.write_bytes(raw)
            temporary.replace(output)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        try:
            with open(output, "xb") as stream:  # 排他创建：与前面的存在性检查之间不会被并发写入覆盖
                stream.write(raw)
        except FileExistsError as error:
            raise SealError(f"输出文件已存在，拒绝覆盖（同名重封请显式加 --replace）：{output}") from error
    # 提示用：仓库里除输出文件之外的未提交改动（测试目录、文档等不在受管树摘要口径内，不影响这份基线）。
    top = Path(_git(root, "rev-parse", "--show-toplevel"))
    porcelain = _git(top, "status", "--porcelain", "--untracked-files=all")
    others = [line.split(None, 1)[1] for line in porcelain.splitlines() if line.strip()]
    others = [path for path in others if (top / path).resolve() != resolved]
    return {"path": output, "source_commit": commit, "sha256": hashlib.sha256(raw).hexdigest(),
            "layer_counts": snapshot["layer_counts"], "entries": len(snapshot["entries"]), "other_changes": others}


def _seal_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="python3 -m tools.official_client_capture.tests.test_codex_identity_conservation",
        description="对 Codex 受管树当前状态封存身份守恒基线（规则见本模块说明）。",
    )
    parser.add_argument("--seal-baseline", required=True, type=Path, metavar="OUTPUT", help="基线输出路径（不存在才写；同名重封加 --replace）")
    parser.add_argument("--replace", action="store_true", help="输出文件已存在时替换它（重封同名基线）")
    arguments = parser.parse_args(argv)
    try:
        result = seal_baseline(arguments.seal_baseline, replace=arguments.replace)
    except SealError as error:
        print(f"封存失败：{error}", file=sys.stderr)
        return 2
    counts = result["layer_counts"]
    print(f"已封存 Codex 身份守恒基线：{result['path']}")
    print(f"  source_commit = {result['source_commit']}")
    print(f"  受管文件 {result['entries']} 个：wire {counts['wire_producer']}／evidence {counts['evidence_semantics']}／"
          f"control {counts['control']}／忽略 {counts['ignored']}／默认归层 {counts['defaulted']}")
    print(f"  文件摘要（填入 BASELINE_SHA256）= {result['sha256']}")
    if result["other_changes"]:
        print(f"提示：仓库另有 {len(result['other_changes'])} 项未提交改动（不在受管树摘要口径内，不影响这份基线），例如："
              + "、".join(result["other_changes"][:5]))
    print("下一步：改 BASELINE_SHA256（文件名变了同时改 BASELINE_PATH）、清空 REGISTERED_CONTROL_CHANGES，再运行本模块全部测试。")
    return 0


# ----------------------------------------------------------------------
# 变异验证用的小工具
# ----------------------------------------------------------------------


def _probe_anchor(lines: list[str], node: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.stmt | None:
    """函数体里探针的插入点：文档字符串之后的第一条语句（没有文档字符串就是第一条语句）。

    这条语句必须独占一行（行内它前面只有缩进），否则探针会插进多行签名或单行函数的中间；不满足返回 ``None``。"""

    body = node.body
    first = body[0]
    has_docstring = (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str)
                     and len(body) > 1)
    anchor = body[1] if has_docstring else first
    if anchor.lineno <= node.lineno or lines[anchor.lineno - 1][:anchor.col_offset].strip():
        return None
    return anchor


def _insert_probe_statement(source: str, name: str) -> str:
    """在顶层函数 ``name`` 的函数体里插入一条 ``pass``：语法仍合法，但该函数的源码段与摘要必然不同。"""

    definitions = [node for node in ast.parse(source).body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name]
    if not definitions:
        raise ValueError(f"编排器里没有顶层函数：{name}")
    node = definitions[-1]  # 与身份引擎一致：同名重复定义以最后一个为准
    lines = source.split("\n")
    anchor = _probe_anchor(lines, node)
    if anchor is None:
        raise ValueError(f"函数体没有独占一行的语句，无法插入探针：{name}")
    lines.insert(anchor.lineno - 1, f"{' ' * anchor.col_offset}pass  # 变异探针")
    return "\n".join(lines)


def _probe_candidates(source: str, excluded: set[str]) -> list[str]:
    """编排器里不在 ``excluded`` 的、可以插探针的顶层函数名（按出现次序）。"""

    lines = source.split("\n")
    return [node.name for node in ast.parse(source).body
            if isinstance(node, ast.FunctionDef) and node.name not in excluded and _probe_anchor(lines, node) is not None]


class CodexIdentityConservationTest(unittest.TestCase):
    """基线固定；身份敏感摘要逐字相等；受管树变化逐文件登记且只落 control 层；迁入符号不在闭包内；护栏对真实变化有反应。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.baseline_raw = _read_regular_file(BASELINE_PATH)
        cls.baseline_sha256 = hashlib.sha256(cls.baseline_raw).hexdigest()
        cls.baseline = json.loads(cls.baseline_raw, object_pairs_hook=_reject_duplicate_keys)
        cls.policy = tip.load_policy(TOOL_ROOT / tip.POLICY_FILENAME)
        cls.snapshot = compute_snapshot(TOOL_ROOT)
        # 变异验证的类级副本树：受管树摘要口径内的全部文件拷一份，各用例在副本上改文件、算完逐字节还原。路径稳定，
        # 读侧闭包的模块解析缓存（按路径与摘要）就能被没改的模块复用。
        cls.mutation_root = Path(tempfile.mkdtemp(prefix="codex-conservation-", dir=TEMP_ROOT))
        cls.addClassCleanup(shutil.rmtree, cls.mutation_root, ignore_errors=True)
        for entry in cls.snapshot["entries"]:
            target = cls.mutation_root / entry["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(TOOL_ROOT / entry["path"], target)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @contextmanager
    def _mutated_tree(self, mutations: Mapping[str, Callable[[str], str]]) -> Iterator[Path]:
        """在副本树上按 ``mutations``（受管树相对路径 → 对文件文本的变换）改文件，退出时逐字节还原。"""

        originals: dict[str, bytes] = {}
        try:
            for relative, mutate in mutations.items():
                target = self.mutation_root / relative
                originals[relative] = target.read_bytes()
                changed = mutate(originals[relative].decode("utf-8")).encode("utf-8")
                self.assertNotEqual(changed, originals[relative], f"构造的变异必须真的改了文件：{relative}")
                target.write_bytes(changed)
            yield self.mutation_root
        finally:
            for relative, data in originals.items():
                (self.mutation_root / relative).write_bytes(data)

    def _baseline_as_snapshot(self) -> dict[str, Any]:
        """基线里与快照同构的那部分字段（去掉 ``schema_version`` 与 ``source_commit``）：一份“恰好等于基线”的快照。"""

        return copy.deepcopy({key: self.baseline[key] for key in BASELINE_KEYS[2:]})

    @staticmethod
    def _tampered(base: Mapping[str, Any], changes: Mapping[str, str | None]) -> dict[str, Any]:
        """在一份快照上改受管树清单：路径 → 新摘要（新增或修改），``None`` 表示删除。"""

        digests = {entry["path"]: entry["sha256"] for entry in base["entries"]}
        for path, digest in changes.items():
            if digest is None:
                digests.pop(path)
            else:
                digests[path] = digest
        tampered = copy.deepcopy(dict(base))
        tampered["entries"] = [{"path": path, "sha256": digest} for path, digest in sorted(digests.items())]
        return tampered

    def _first_path(self, layer: str, *, entries: list[dict[str, str]] | None = None, suffix: str = ".py",
                    skip: tuple[str, ...] = ()) -> str:
        """某一层第一个符合后缀的受管文件（按路径排序，默认取当前受管树），用来构造变异，避免把具体文件名写死。"""

        return next(entry["path"] for entry in (entries or self.snapshot["entries"])
                    if tip.classify_path(self.policy, entry["path"]) == layer and entry["path"].endswith(suffix)
                    and entry["path"] not in skip)

    def _orchestrator_function_mutation(self, name: str) -> Callable[[str], str]:
        return lambda text: _insert_probe_statement(text, name)

    # ------------------------------------------------------------------
    # 基线本身
    # ------------------------------------------------------------------

    def test_baseline_is_frozen(self) -> None:
        self.assertEqual(self.baseline_sha256, BASELINE_SHA256,
                         f"Codex {BASELINE_CODEX_VERSION} 身份守恒基线被改动：基线只在 Codex 目标版本登记时重封（见模块说明第二节）")
        self.assertEqual(tuple(self.baseline), BASELINE_KEYS, "基线字段集合或次序与约定不符")
        self.assertEqual(self.baseline["schema_version"], BASELINE_SCHEMA)
        self.assertRegex(self.baseline["source_commit"], r"\A[0-9a-f]{40}\Z")
        # 文件就是封存入口的规范输出，没有被手工编辑或换过格式。
        self.assertEqual(serialize_baseline(self.baseline), self.baseline_raw)
        # 内部一致：整树摘要由逐文件清单算出；清单按路径排序且不重复；摘要字段都是 64 位十六进制；封存时没有默认归层文件。
        entries = self.baseline["entries"]
        paths = [entry["path"] for entry in entries]
        self.assertEqual(paths, sorted(set(paths)), "基线逐文件清单必须按路径排序且不重复")
        self.assertTrue(all(set(entry) == {"path", "sha256"} and HEX64.fullmatch(entry["sha256"]) for entry in entries))
        self.assertEqual(self.baseline["files_sha256"], tip._fingerprint({"entries": entries}))
        for field in ("policy_sha256", "files_sha256", "control_sha256", *IDENTITY_SENSITIVE_FIELDS):
            self.assertRegex(self.baseline[field], r"\A[0-9a-f]{64}\Z", field)
        self.assertEqual(self.baseline["defaulted_paths"], [], "封存时不允许有按默认层归类的受管文件")

    def test_baseline_document_is_built_and_serialized_canonically(self) -> None:
        """封存入口的文档构造与序列化：键序固定、提交号格式受检、序列化可逆。"""

        snapshot = {key: self.baseline[key] for key in BASELINE_KEYS[2:]}
        document = build_baseline_document(snapshot, source_commit=self.baseline["source_commit"])
        self.assertEqual(tuple(document), BASELINE_KEYS)
        self.assertEqual(json.loads(serialize_baseline(document)), self.baseline)
        for bad in ("", "abc", "G" * 40, "a" * 39, "A" * 40, "a" * 41):
            with self.subTest(source_commit=bad), self.assertRaises(ValueError):
                build_baseline_document(snapshot, source_commit=bad)
        with self.assertRaises(ValueError):
            build_baseline_document({key: value for key, value in snapshot.items() if key != "entries"}, source_commit="a" * 40)
        with self.assertRaises(ValueError):
            build_baseline_document({**snapshot, "unexpected_key": 1}, source_commit="a" * 40)

    def test_head_comparison_reports_every_kind_of_difference(self) -> None:
        """封存前置条件：工作树受管文件与 HEAD 逐字一致才通过；修改、新增、缺失各有一条说明。"""

        head = {"a.py": "1" * 64, "b.json": "2" * 64, "c.sh": "3" * 64}
        self.assertEqual(managed_tree_head_problems(dict(head), head), [])
        problems = managed_tree_head_problems({"a.py": "9" * 64, "b.json": "2" * 64, "d.py": "4" * 64}, head)
        self.assertEqual(problems, [
            "受管文件相对 HEAD 有未提交的修改：a.py",
            "受管文件不在 HEAD 里（未提交或被忽略）：d.py",
            "HEAD 里的受管文件在工作树缺失：c.sh",
        ])
        self.assertTrue(_in_managed_digest_scope(PurePosixPath("capturelib/model.py")))
        for relative in ("tests/test_x.py", "versions/a.json", "x/__pycache__/m.py", "README.md", "a.txt"):
            with self.subTest(relative=relative):
                self.assertFalse(_in_managed_digest_scope(PurePosixPath(relative)))

    # ------------------------------------------------------------------
    # ① 产出身份、④ tool-evolution
    # ------------------------------------------------------------------

    def test_identity_sensitive_digests_match_baseline(self) -> None:
        """① wire／evidence 产出身份、两个编排器闭包、checker／builder 与两个读侧摘要与基线逐字相等，没有任何过渡登记。"""

        self.assertEqual(identity_mismatches(self.baseline, self.snapshot), [],
                         "Codex 身份被改动：基线之后的改动不得改变 Codex 的 wire／evidence 身份；"
                         "属于 Codex 目标版本登记的变更则在该变更集里重封基线（见模块说明第二节）")
        # 逐项再核一遍，失败时能直接看到是哪一项、两边各是什么。
        for field in IDENTITY_SENSITIVE_FIELDS:
            self.assertEqual(self.snapshot[field], self.baseline[field], f"Codex 身份敏感摘要变化：{field}")
        for layer, expected in self.baseline["orchestrator_closure_sha256"].items():
            self.assertEqual(self.snapshot["orchestrator_closure_sha256"][layer], expected, f"Codex 编排器 {layer} 闭包变化")

    def test_wire_closure_unchanged_for_tool_evolution(self) -> None:
        """④ tool-evolution 预览按编排器 wire 闭包摘要判定 ``wire_closure_changed``，基线之后必须为 false。"""

        self.assertEqual(
            self.snapshot["orchestrator_closure_sha256"]["wire_producer"],
            self.baseline["orchestrator_closure_sha256"]["wire_producer"],
        )

    def test_snapshot_agrees_with_production_tool_identity(self) -> None:
        """守恒快照与 Codex 编排器自己的身份函数、评估器摘要函数逐字段一致：护栏算的就是生产路径上记录的那些值。"""

        from tools.official_client_capture import codex_upgrade

        production = codex_upgrade._tool_identity(include_git=False)
        for field in ("policy_version", "policy_sha256", "files_sha256", "wire_producer_sha256", "evidence_semantics_sha256",
                      "control_sha256", "entries"):
            self.assertEqual(production[field], self.snapshot[field], f"与 _tool_identity 不一致：{field}")
        for layer in ("wire_producer", "evidence_semantics"):
            self.assertEqual(production["orchestrator_closures"][layer]["closure_sha256"],
                             self.snapshot["orchestrator_closure_sha256"][layer], f"与 _tool_identity 的编排器闭包不一致：{layer}")
        evaluator = tip.evaluator_dependency_digests(TOOL_ROOT, self.policy)
        for field in tip.EVALUATOR_DIGEST_FIELDS:
            self.assertEqual(evaluator[field], self.snapshot[field], f"与评估器摘要函数不一致：{field}")

    # ------------------------------------------------------------------
    # ② 受管树逐文件变化必须登记
    # ------------------------------------------------------------------

    def test_tree_changes_are_registered_control_changes(self) -> None:
        """② 相对基线的新增、删除、修改只能落在 control 层并逐文件登记；没有默认归层文件；策略摘要变化对应策略文件的登记变化。"""

        self.assertEqual(tree_change_problems(self.policy, self.baseline, self.snapshot, REGISTERED_CONTROL_CHANGES), [],
                         "Codex 受管树变化必须逐文件登记到 REGISTERED_CONTROL_CHANGES，且只能在 control 层")
        self.assertEqual(self.snapshot["defaulted_paths"], [], "新增受管文件必须在 Codex 策略里登记层级")

    def test_tree_change_check_rejects_unregistered_changes_in_every_layer(self) -> None:
        """② 的判定函数在被篡改的受管树清单上运行：wire、evidence、control 三层的未登记变化与不诚实的登记表都被拒绝。

        被篡改的清单取自基线本身（一份“恰好等于基线”的快照再注入变化），所以不受真实树此刻是否对得上基线影响：
        真实树红时只有主测试红。"""

        reference = self.baseline
        base = self._baseline_as_snapshot()
        wire_path = self._first_path("wire_producer", entries=base["entries"])
        evidence_path = tip.EVALUATOR_CHECKER_RELATIVE
        control_path = self._first_path("control", entries=base["entries"], skip=(tip.POLICY_FILENAME,))
        claude_path = next(entry["path"] for entry in base["entries"] if tip.classify_path(self.policy, entry["path"]) is None)
        for path, layer in ((wire_path, "wire_producer"), (evidence_path, "evidence_semantics"), (control_path, "control")):
            self.assertEqual(tip.classify_path(self.policy, path), layer)
        zero = "0" * 64
        cases: tuple[tuple[str, Mapping[str, str | None], Mapping[str, str], str], ...] = (
            ("修改 wire 层文件", {wire_path: zero}, {}, f"变化未登记：{wire_path}（wire_producer 层）"),
            ("新增 wire 层文件", {"drive_unregistered_probe.py": zero}, {},
             "变化未登记：drive_unregistered_probe.py（wire_producer 层）"),
            ("删除 wire 层文件", {wire_path: None}, {}, f"变化未登记：{wire_path}（wire_producer 层）"),
            ("修改 evidence 层文件", {evidence_path: zero}, {}, f"变化未登记：{evidence_path}（evidence_semantics 层）"),
            ("新增 evidence 层文件", {"candidate_test_fact_map_0_999_0.json": zero}, {},
             "变化未登记：candidate_test_fact_map_0_999_0.json（evidence_semantics 层）"),
            ("删除 evidence 层文件", {evidence_path: None}, {}, f"变化未登记：{evidence_path}（evidence_semantics 层）"),
            ("修改 control 层文件", {control_path: zero}, {}, f"变化未登记：{control_path}（control 层）"),
            ("删除 control 层文件", {control_path: None}, {}, f"变化未登记：{control_path}（control 层）"),
            ("新增未归层文件", {"codex_upgrade_unregistered_probe.py": zero}, {},
             "新增受管文件按默认层归类，必须在 Codex 策略里登记层级：codex_upgrade_unregistered_probe.py"),
            ("wire 层文件被登记成 control 变化", {wire_path: zero}, {wire_path: "冒充登记"},
             f"变化不在 control 层：{wire_path}（wire_producer 层）"),
            ("evidence 层文件被登记成 control 变化", {evidence_path: zero}, {evidence_path: "冒充登记"},
             f"变化不在 control 层：{evidence_path}（evidence_semantics 层）"),
            ("登记了没有变化的文件", {}, {control_path: "过期登记"}, f"登记项相对基线没有变化，应从 REGISTERED_CONTROL_CHANGES 删除：{control_path}"),
            ("登记缺理由", {control_path: zero}, {control_path: "  "}, f"登记项缺少工作项与理由：{control_path}"),
            ("登记了被忽略的路径", {claude_path: zero}, {claude_path: "多余登记"},
             f"登记项属于被 Codex 策略忽略的路径，无须登记：{claude_path}"),
            ("策略文件变化但策略摘要没跟着变", {tip.POLICY_FILENAME: zero}, {tip.POLICY_FILENAME: "登记了文件变化"},
             "策略摘要变化必须对应策略文件的登记变化"),
        )
        for case, changes, registered, expected in cases:
            problems = tree_change_problems(self.policy, reference, self._tampered(base, changes), registered)
            with self.subTest(case=case):
                self.assertTrue(any(expected in problem for problem in problems), (expected, problems))
        # 放行的几种情形：已登记的 control 变化、被忽略的 Claude 专属文件的任何变化。
        self.assertEqual(tree_change_problems(self.policy, reference, base, {}), [], "未被篡改的基线快照不应有任何问题")
        self.assertEqual(tree_change_problems(self.policy, reference, self._tampered(base, {control_path: zero}),
                                              {control_path: "测试登记"}), [])
        self.assertEqual(tree_change_problems(self.policy, reference, self._tampered(base, {claude_path: zero}), {}), [])
        self.assertEqual(tree_change_problems(self.policy, reference, self._tampered(base, {"claude_unregistered_probe.py": zero}), {}), [])
        # 真正的 ② 测试方法在被篡改的清单上同样失败（判定函数确实接在测试上，而且读的是 REGISTERED_CONTROL_CHANGES）。
        name = "test_tree_changes_are_registered_control_changes"
        for case, changes in (("wire", {wire_path: zero}), ("evidence", {evidence_path: zero}), ("control", {control_path: zero})):
            tampered = type(self)(name)
            tampered.baseline, tampered.snapshot = reference, self._tampered(base, changes)
            with self.subTest(method=case), self.assertRaisesRegex(AssertionError, "变化未登记"):
                getattr(tampered, name)()
        registered_case = type(self)(name)
        registered_case.baseline, registered_case.snapshot = reference, self._tampered(base, {control_path: zero})
        # clear=True：被篡改清单只改了 control_path 一个文件，真实登记表里（相对基线确有变化的）其它登记项在这份清单上“没有变化”，不能混进来。
        with mock.patch.dict(REGISTERED_CONTROL_CHANGES, {control_path: "测试登记"}, clear=True):
            getattr(registered_case, name)()

    # ------------------------------------------------------------------
    # ③ 迁入共享模块的符号
    # ------------------------------------------------------------------

    def test_migrated_symbols_stay_outside_identity_closures(self) -> None:
        """③ 迁入共享模块的函数与常量不得属于 wire、evidence 或评估读侧闭包（基线记录的闭包与当前实算的闭包都核对）。"""

        path = TOOL_ROOT / SHARED_CONTROL_PLANE
        defined: set[str] = set()
        if path.is_file():
            functions, constants, _imports = tip._module_symbols(ast.parse(path.read_text(encoding="utf-8")))
            defined = set(functions) | set(constants)
        for label, symbols in (("基线", self.baseline["closure_symbols"]), ("当前", self.snapshot["closure_symbols"])):
            closure: set[str] = set()
            for kinds in symbols.values():
                for names in kinds.values():
                    closure.update(names)
            with self.subTest(closure=label):
                self.assertEqual(sorted(defined & closure), [], f"迁入共享模块的符号落在 Codex 身份闭包内（{label}闭包）")

    # ------------------------------------------------------------------
    # 护栏对真实变化有反应：在副本树上改真实源码，重算的摘要必须变，且判定函数点名问题
    # ------------------------------------------------------------------

    def test_wire_layer_changes_break_the_wire_identity(self) -> None:
        """wire 层文件加一行注释、wire 闭包里的编排器函数加一条语句：wire 身份变，判定函数报“未登记”或闭包变化。

        变异验证以“当前快照”为参照（与真实树此刻是否对得上基线无关），下面三个变异用例同理。"""

        reference = self.snapshot
        wire_path = self._first_path("wire_producer")
        with self._mutated_tree({wire_path: lambda text: text + "\n# 变异探针\n"}):
            changed = compute_snapshot(self.mutation_root)
        self.assertNotEqual(changed["wire_producer_sha256"], reference["wire_producer_sha256"])
        self.assertEqual(changed["evidence_semantics_sha256"], reference["evidence_semantics_sha256"], "wire 层文件不得影响 evidence 摘要")
        self.assertIn("Codex 身份敏感摘要变化：wire_producer_sha256", identity_mismatches(reference, changed))
        self.assertIn(f"Codex 受管文件变化未登记：{wire_path}（wire_producer 层）",
                      tree_change_problems(self.policy, reference, changed, {}))

        root = self.policy["orchestrator"]["wire_roots"][0]
        orchestrator = self.policy["orchestrator"]["file"]
        with self._mutated_tree({orchestrator: self._orchestrator_function_mutation(root)}):
            changed = compute_snapshot(self.mutation_root)
        self.assertNotEqual(changed["orchestrator_closure_sha256"]["wire_producer"], reference["orchestrator_closure_sha256"]["wire_producer"])
        self.assertIn("Codex 编排器 wire_producer 闭包变化", identity_mismatches(reference, changed))
        self.assertIn("Codex 身份敏感摘要变化：wire_producer_sha256", identity_mismatches(reference, changed))

    def test_evidence_layer_changes_break_the_evidence_identity(self) -> None:
        """checker／builder 文件与 evidence 闭包里的编排器函数变化：evidence 身份与对应的评估器直接依赖摘要变，wire 摘要不受 evidence 文件影响。"""

        reference = self.snapshot
        files = {path: (lambda text: text + "\n# 变异探针\n") for path in (tip.EVALUATOR_CHECKER_RELATIVE, tip.EVALUATOR_BUILDER_RELATIVE)}
        with self._mutated_tree(files):
            changed = compute_snapshot(self.mutation_root)
        for field in ("evidence_semantics_sha256", "checker_sha256", "builder_sha256"):
            self.assertNotEqual(changed[field], reference[field], field)
        self.assertEqual(changed["wire_producer_sha256"], reference["wire_producer_sha256"], "evidence 层文件不得影响 wire 摘要")
        problems = tree_change_problems(self.policy, reference, changed, {})
        for path in files:
            self.assertIn(f"Codex 受管文件变化未登记：{path}（evidence_semantics 层）", problems)

        root = self.policy["orchestrator"]["evidence_roots"][0]
        with self._mutated_tree({self.policy["orchestrator"]["file"]: self._orchestrator_function_mutation(root)}):
            changed = compute_snapshot(self.mutation_root)
        self.assertNotEqual(changed["orchestrator_closure_sha256"]["evidence_semantics"],
                            reference["orchestrator_closure_sha256"]["evidence_semantics"])
        self.assertIn("Codex 编排器 evidence_semantics 闭包变化", identity_mismatches(reference, changed))
        self.assertIn("Codex 身份敏感摘要变化：evidence_semantics_sha256", identity_mismatches(reference, changed))

    def test_evaluator_reader_changes_break_the_reader_digests(self) -> None:
        """读侧根函数改动、读侧闭包里整文件计入的模块连注释都不能改：对应的读侧摘要必须变（改动要么回退要么等下次重封）。"""

        reference = self.snapshot
        orchestrator = self.policy["orchestrator"]["file"]
        both = {orchestrator: lambda text: _insert_probe_statement(_insert_probe_statement(text, tip.EVALUATOR_COMPARE_READER_ROOTS[0]),
                                                                  tip.EVALUATOR_ACCEPT_READER_ROOTS[0])}
        with self._mutated_tree(both):
            changed = compute_snapshot(self.mutation_root)
        self.assertNotEqual(changed["compare_reader_sha256"], reference["compare_reader_sha256"])
        self.assertNotEqual(changed["accept_reader_sha256"], reference["accept_reader_sha256"])
        self.assertEqual(changed["wire_producer_sha256"], reference["wire_producer_sha256"])
        # 整文件计入读侧闭包的模块：每个读侧各取第一个，只在文件末尾加一行注释，该读侧摘要就变。
        digests = {entry["path"]: entry["sha256"] for entry in self.snapshot["entries"]}
        for field, roots in READER_FIELDS:
            modules = tip.evaluator_reader_closure(self.policy, TOOL_ROOT, list(roots), digests)["modules"]
            self.assertTrue(modules, f"{field} 的读侧闭包里应当有整文件计入的模块")
            path = modules[0]["path"]
            with self.subTest(field=field, whole_file=path), self._mutated_tree({path: lambda text: text + "\n# 变异探针\n"}):
                changed = compute_snapshot(self.mutation_root)
                self.assertNotEqual(changed[field], reference[field])
                self.assertIn(f"Codex 身份敏感摘要变化：{field}", identity_mismatches(reference, changed))

    def test_control_only_orchestrator_changes_need_registration_not_a_reseal(self) -> None:
        """不在任何闭包里的编排器函数改了：产出身份与读侧摘要都不变，只剩 control 层文件变化，登记即放行（登记表机制确实可用）。"""

        reference = self.snapshot
        orchestrator = self.policy["orchestrator"]["file"]
        inside: set[str] = set()
        for kinds in reference["closure_symbols"].values():
            for names in kinds.values():
                inside.update(names)
        name = _probe_candidates((TOOL_ROOT / orchestrator).read_text(encoding="utf-8"), inside)[0]
        with self._mutated_tree({orchestrator: self._orchestrator_function_mutation(name)}):
            changed = compute_snapshot(self.mutation_root)
        self.assertEqual(identity_mismatches(reference, changed), [], f"闭包外函数 {name} 的改动不应改变任何产出身份")
        self.assertNotEqual(changed["control_sha256"], reference["control_sha256"])
        self.assertEqual(tip.classify_path(self.policy, orchestrator), "control")
        self.assertIn(f"Codex 受管文件变化未登记：{orchestrator}（control 层）", tree_change_problems(self.policy, reference, changed, {}))
        self.assertEqual(tree_change_problems(self.policy, reference, changed, {orchestrator: "闭包外函数的 control 层改动"}), [])


if __name__ == "__main__":
    if "--seal-baseline" in sys.argv[1:]:
        raise SystemExit(_seal_main(sys.argv[1:]))
    unittest.main()
