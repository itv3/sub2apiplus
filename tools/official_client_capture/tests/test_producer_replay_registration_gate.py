"""生成器只读重放身份登记门禁测试（修好接着跑第 36 项事故后的门禁改进）。

门禁的定义与实现见 ``producer_replay_registration``（同目录只读辅助模块）。本模块验证四件事：

1. 当前工作树：五个生成器（第 40、42 项起含门禁收据与生产激活收据生成器）在全部部署边界处的旧
   摘要都已登记为只读重放身份，或属于门禁基线前已审计的历史豁免；豁免表恰好等于"不带豁免时的违规
   集合"（没有多余、没有遗漏）且全部来自基线前的收据；仓库内入库的门禁收据与生产激活收据，其生成器
   摘要都能被当前生成器承接；任何模块新增只读重放登记常量都必须先纳入覆盖清单。登记集合与入库收据
   按仓库实际内容判定、只要求已知条目仍在，今后按门禁提示追加登记或新收据入库时不误红。
1b. 覆盖自检（第 42 项）：静态发现"计算自身摘要、且校验路径要求等于当前"的生成器，每个都必须
   纳入覆盖清单或在分类表写明不跨工具版本重放的依据；只按门禁引入时的覆盖清单判定，当前树恰好
   报出第 40、42 项补登记的两个生成器。
2. 事故形态（只读导出真实历史，不改历史）：4cf336fbf（第 36 项提交）与 3cf1a543f（第 36 项承接
   收据已登记）都必须失败，且恰好指出 9e10bd0f… 要登记到 ``REGISTERED_REPLAY_PRODUCER_HASHES["8"]``；
   补登记后的 f9727c765、b2c088d99 与事故前的 5d218b931 都必须通过（历史已处理的版本不误报）。
3. 反证（在当前树副本上变异）：改生成器却不登记上一部署边界、删掉已有登记、登记到错误版本、
   计时账本不追加承接描述，都必须失败；按处置提示正确登记后必须通过。门禁收据生成器另覆盖第 40 项
   自身的形态：改了生成器却没登记修改前摘要 034331e5 必须失败。判定一律按变异前后违规集合的差集，
   不写死与仓库承接收据数量相关的精确集合；另一组用例追加一份模拟的"今后新增承接收据"后重跑，
   证明新增边界不会让这些用例误红。
4. 合成临时 git 仓库：变更集内部的中间提交不要求登记；不在 git 历史里的显式节点必须登记；浅克隆
   失败关闭；非 git 树无法判定；门禁基线之后的新边界不得豁免；缺失提交只跳过前后提交边界；新增
   文件的空内容前序不计边界；尚无只读重放判定的生成器如实跳过并报告。

真实形态与反证需要完整 git 历史（CI 的 capture-tools job 以 fetch-depth: 0 检出）；不是 git 工作树时
跳过，浅克隆时门禁本身失败关闭。全程只读真实仓库：历史形态用 ``git ls-tree``／``git cat-file``
导出到临时目录，变异只发生在临时副本上。
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import Iterable, Mapping
from unittest import mock

from tools.official_client_capture import codex_upgrade_gate_receipt as gate_receipt_module
from tools.official_client_capture import production_activation_receipt as activation_receipt
from tools.official_client_capture.tests import producer_replay_registration as gate


REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
TOOL_RELATIVE = "tools/official_client_capture"

# 第 36 项事故的真实历史坐标（完整提交号，只读访问）。
ITEM36_COMMIT = "4cf336fbfc735981f45503b01709f8e07d647c24"
ITEM36_RECEIPT_COMMIT = "3cf1a543fee56e975954ddc3b42570adc36fa5bd"
ITEM36B_PATCH_COMMIT = "f9727c765eaf96003577d0d6d5ee67a166655b8b"
ITEM36B_RECEIPT_COMMIT = "b2c088d99f67eacf26059f276ed0b25bdfca0649"
R15_REVIEW_FIX_COMMIT = "5d218b93166cb8911f4deced596052cf579e6bd8"
R15_REVIEW_INTERMEDIATE_COMMIT = "44c015e78a1ebfbbb17409b9e43ecf4c394d89d6"
ITEM36_MISSING_DIGEST = "9e10bd0f91b588ee6aefd0ab06ac845c248faaa45b75c906bd5df91933845f98"
ITEM36_BROKEN_DIGEST = "48304c4c7c8028b7ac613d862bff88ec7abf8d98fa21cc87fe6d46eb337a015a"
FINALIZER_REGISTERED_BOUNDARY = "06fe9886cf3bdab552dc61e556011bf8377f04389fa52a3536a457636052d788"

MODULE_CONSTANT_RE = re.compile(r"^([A-Z][A-Z0-9_]*)\s*(?::[^=\n]*)?=", re.MULTILINE)

ENVIRONMENT_SPEC = next(spec for spec in gate.PRODUCERS if spec.path == gate.ENVIRONMENT_RECEIPT_PRODUCER)
FINALIZER_SPEC = next(spec for spec in gate.PRODUCERS if spec.path == gate.RECEIPT_FINALIZER_PRODUCER)
TIMING_SPEC = next(spec for spec in gate.PRODUCERS if spec.path == gate.TIMING_LEDGER_PRODUCER)
GATE_SPEC = next(spec for spec in gate.PRODUCERS if spec.path == gate.GATE_RECEIPT_PRODUCER)
# 第 40 项修改前的门禁收据生成器摘要（5d218b931 起部署），以及 0.154 期间的受管版本。
GATE_RECEIPT_PRE_ITEM40_DIGEST = "034331e58aa96dad7b8368c231fd2ead38a826ffce18c4d76daf4b16b8e14020"
GATE_RECEIPT_0154_DIGEST = "931ae5b3f6537eaa9a8c38fa4569a9560b178c8d250d0e95aeec02f91ae29552"
GATE_RECEIPT_V4_SCHEMA = "codex-upgrade-external-gate-producer/v4"
ACTIVATION_SPEC = next(spec for spec in gate.PRODUCERS if spec.path == gate.PRODUCTION_ACTIVATION_PRODUCER)
# 第 42 项修改前的生产激活收据生成器摘要（ef262c618 起部署），以及生成过 R34 收据的 0.149.1 期间版本。
ACTIVATION_PRE_ITEM42_DIGEST = "3b4ddbf874a9164654e8496fb6f2a88fbd50b22557f16b5285f0aa0aca7d1292"
ACTIVATION_R34_DIGEST = "83ed012547421355251e0cbc28a3c8b9cb471250170e9b2d11242356cd085382"
ACTIVATION_V2_SCHEMA = "codex-production-activation-producer/v2"
# 本分支的起点提交（第 40、42 项之前），用于证明覆盖自检能发现当时缺登记的两个生成器。
BRANCH_BASE_COMMIT = "b6a62b71418358d0eb992326732389063f369e20"

# 合成临时仓库的写操作（init／commit／clone）专用的 git 隔离：不读用户全局／系统配置（签名、钩子、
# 默认分支）。只读真实仓库时不隔离，以保留 CI 检出写入全局配置的 safe.directory 等设置。
GIT_ISOLATION = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "replay-gate",
    "GIT_AUTHOR_EMAIL": "replay-gate@example.invalid",
    "GIT_COMMITTER_NAME": "replay-gate",
    "GIT_COMMITTER_EMAIL": "replay-gate@example.invalid",
}


def _git(repository: Path, *args: str, stdin: bytes | None = None, isolated: bool = False) -> bytes:
    environment = dict(os.environ)
    if isolated:
        environment.update(GIT_ISOLATION)
    completed = subprocess.run(
        ["git", "-C", str(repository), *args],
        input=stdin,
        capture_output=True,
        env=environment,
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(
            f"git {' '.join(args)} 失败：{completed.stderr.decode('utf-8', 'replace').strip()}"
        )
    return completed.stdout


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write(path: Path, data: bytes) -> None:
    """写文件并固定 0644：计时账本读取承接收据时拒绝 group/other 可写文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.chmod(path, 0o644)


def _require_full_history() -> None:
    """真实仓库不是 git 工作树时跳过；浅克隆交给当前树门禁测试失败关闭，这里跳过避免重复报错。"""

    try:
        gate.ensure_full_history(REPOSITORY_ROOT)
    except gate.GateUnavailable as error:
        raise unittest.SkipTest(f"需要 git 工作树：{error}") from error
    except gate.GateError as error:
        raise unittest.SkipTest(f"需要完整 git 历史：{error}") from error


def _ls_tree_blobs(commit: str, directory: str, *, recursive: bool) -> list[str]:
    """以 NUL 分隔读取 ls-tree（不受 core.quotePath 引号影响），只返回 blob 路径。"""

    arguments = ["ls-tree", "-z", *(["-r"] if recursive else []), commit, "--", directory]
    paths = []
    for entry in _git(REPOSITORY_ROOT, *arguments).split(b"\0"):
        if not entry:
            continue
        meta, path = entry.decode("utf-8").split("\t", 1)
        if meta.split()[1] == "blob":
            paths.append(path)
    return paths


def _export_commit_shape(commit: str, destination: Path) -> Path:
    """只读导出某个历史提交的门禁所需子树到 destination。

    导出范围：``docs/egress/maintenance`` 顶层 JSON（冻结承接图收据）与 ``tools/official_client_capture``
    （不含 tests；计时账本判定还会读取删除证明登记的历史读取器，如 ``codex_upgrade.py``）。
    """

    paths = [
        path
        for path in _ls_tree_blobs(commit, gate.MAINTENANCE_RELATIVE + "/", recursive=False)
        if path.endswith(".json")
    ]
    paths += [
        path
        for path in _ls_tree_blobs(commit, TOOL_RELATIVE + "/", recursive=True)
        if "/tests/" not in path
    ]
    raw = _git(REPOSITORY_ROOT, "cat-file", "--batch", stdin=("\n".join(f"{commit}:{p}" for p in paths) + "\n").encode())
    offset = 0
    for path in paths:
        header_end = raw.index(b"\n", offset)
        header = raw[offset:header_end].split()
        size = int(header[2])
        _write(destination / path, raw[header_end + 1 : header_end + 1 + size])
        offset = header_end + 1 + size + 1
    return destination


def _copy_worktree_shape(destination: Path) -> Path:
    """复制当前工作树（含未提交修改）的同一子树，供反证变异。"""

    maintenance = REPOSITORY_ROOT / gate.MAINTENANCE_RELATIVE
    for path in sorted(maintenance.glob("*.json")):
        _write(destination / gate.MAINTENANCE_RELATIVE / path.name, path.read_bytes())
    tool_root = REPOSITORY_ROOT / TOOL_RELATIVE
    for path in sorted(tool_root.rglob("*")):
        relative = path.relative_to(tool_root)
        if not path.is_file() or path.is_symlink() or {"tests", "__pycache__"} & set(relative.parts):
            continue
        _write(destination / TOOL_RELATIVE / relative, path.read_bytes())
    return destination


def _violation_digests(report: gate.GateReport) -> set[tuple[str, str]]:
    return {(item.producer.path, item.boundary_sha256) for item in report.violations}


def _violation_digest_set(report: gate.GateReport) -> set[str]:
    """单个生成器报告里的违规摘要集合（变异用例按它做差集判定）。"""

    return {item.boundary_sha256 for item in report.violations}


# ---------------------------------------------------------------------------
# 1. 当前工作树
# ---------------------------------------------------------------------------


class CurrentTreeRegistrationGateTest(unittest.TestCase):
    """当前工作树必须通过门禁；豁免表最小；登记常量全覆盖。"""

    @classmethod
    def setUpClass(cls) -> None:
        try:
            gate.ensure_full_history(REPOSITORY_ROOT)
        except gate.GateUnavailable as error:
            raise unittest.SkipTest(f"需要 git 工作树才能判定部署边界：{error}") from error
        # 浅克隆在这里抛 GateError，使整个测试类失败关闭（CI 必须 fetch-depth: 0）。
        cls.report = gate.check_registration(REPOSITORY_ROOT)
        cls.unexempted = gate.check_registration(REPOSITORY_ROOT, exemptions={})

    def test_every_deployment_boundary_is_registered_for_replay(self) -> None:
        self.assertTrue(self.report.passed, gate.format_report(self.report))
        # 当前工作树里每个覆盖对象都必须已有登记常量，不允许因"尚无登记机制"被跳过。
        self.assertEqual([], self.report.skipped, gate.format_report(self.report))
        for spec in gate.PRODUCERS:
            boundaries = self.report.boundaries[spec.path]
            # 至少有一个非当前的部署边界，证明门禁真的在检查历史（而不是空集合恒通过）。
            self.assertTrue(
                any(digest != self.report.current[spec.path] for digest in boundaries),
                f"{spec.path} 没有任何历史部署边界，门禁失去检查对象",
            )

    def test_historical_exemptions_equal_the_unexempted_violations(self) -> None:
        expected = {
            (path, digest) for path, digests in gate.HISTORICAL_EXEMPTIONS.items() for digest in digests
        }
        # 不带豁免时的违规集合必须恰好等于豁免表：多一条是新遗漏，少一条是陈旧豁免。
        self.assertEqual(expected, _violation_digests(self.unexempted), gate.format_report(self.unexempted))
        for path, digests in gate.HISTORICAL_EXEMPTIONS.items():
            for digest, reason in digests.items():
                boundary = self.report.boundaries[path][digest]
                self.assertTrue(reason.strip(), f"{digest} 豁免缺少依据")
                self.assertNotEqual(digest, self.report.current[path])
                self.assertTrue(
                    gate.is_pre_baseline(boundary),
                    f"{digest} 的出处全部晚于门禁基线 {gate.GATE_BASELINE_ISSUED_AT_UTC}，不得豁免",
                )
        self.assertEqual(
            {(spec.path, digest) for spec, digest, _ in self.report.exempted},
            expected,
        )

    def test_gate_baseline_matches_its_receipt(self) -> None:
        # 门禁基线常量必须与基线收据逐字一致：签发时间，以及 current_commit 处各生成器的摘要。
        path = REPOSITORY_ROOT / gate.MAINTENANCE_RELATIVE / gate.GATE_BASELINE_RECEIPT
        receipt = json.loads(path.read_bytes())
        self.assertEqual(receipt["issued_at_utc"], gate.GATE_BASELINE_ISSUED_AT_UTC)
        self.assertEqual(set(gate.GATE_BASELINE_CURRENT_SHA256), {spec.path for spec in gate.PRODUCERS})
        for producer, digest in gate.GATE_BASELINE_CURRENT_SHA256.items():
            blob = _git(REPOSITORY_ROOT, "cat-file", "blob", f"{receipt['current_commit']}:{producer}")
            self.assertEqual(_sha256(blob), digest, producer)
            self.assertNotIn(digest, gate.HISTORICAL_EXEMPTIONS.get(producer, {}))

    def test_boundaries_follow_freeze_graph_definition(self) -> None:
        environment = self.report.boundaries[gate.ENVIRONMENT_RECEIPT_PRODUCER]
        # 显式节点：r15-review-fix 收据登记的后继摘要（第 36 项事故中漏登记的版本）。
        kinds = {source.kind for source in environment[ITEM36_MISSING_DIGEST].sources}
        self.assertIn("显式 to_sha256", kinds)
        self.assertIn("current_commit", kinds)
        # 0.151 时期从工作区快照登记、不在 git 历史里的版本只以显式节点出现，且已登记。
        workspace_snapshot = "a62a269e5e4cb0e64aac21e5223ddbde8b884ecbe383b405c560e3c6ebcea527"
        self.assertEqual(
            {source.kind for source in environment[workspace_snapshot].sources},
            {"显式 from_sha256"},
        )
        # 变更集内部的中间提交 44c015e78（R15 审核意见修正中途）不是部署边界。
        intermediate = _sha256(
            _git(REPOSITORY_ROOT, "cat-file", "blob", f"{R15_REVIEW_INTERMEDIATE_COMMIT}:{gate.ENVIRONMENT_RECEIPT_PRODUCER}")
        )
        self.assertNotIn(intermediate, environment)
        # 收据终结器没有显式承接边，只能靠变更集前后提交得到部署边界。
        finalizer = self.report.boundaries[gate.RECEIPT_FINALIZER_PRODUCER]
        self.assertIn(FINALIZER_REGISTERED_BOUNDARY, finalizer)
        self.assertTrue(
            all(not source.kind.startswith("显式") for source in finalizer[FINALIZER_REGISTERED_BOUNDARY].sources)
        )

    def test_gate_receipt_boundaries_follow_producer_schema(self) -> None:
        boundaries = self.report.boundaries[gate.GATE_RECEIPT_PRODUCER]
        # 新增文件的前序是空内容摘要（upstream-merge-framework-v2 收据），它不是生成器版本。
        self.assertNotIn(gate.EMPTY_FILE_SHA256, boundaries)
        self.assertNotIn(gate.EMPTY_FILE_SHA256, self.unexempted.boundaries[gate.GATE_RECEIPT_PRODUCER])
        schemas = {
            digest: gate.historical_producer_constants(boundary.content)[0]
            for digest, boundary in boundaries.items()
            if digest != self.report.current[gate.GATE_RECEIPT_PRODUCER]
        }
        # v4 登记集合按生成器的登记常量判定，不写死：生成过收据的 0.154 版本与第 40 项修改前版本必须
        # 一直在登记里；此后每次修改生成器，门禁都会要求追加登记上一部署边界（例如 r21 部署的当前摘要），
        # 按提示登记后这里不得误红。
        registered = gate_receipt_module.REGISTERED_REPLAY_PRODUCER_HASHES[GATE_RECEIPT_V4_SCHEMA]
        self.assertLessEqual({GATE_RECEIPT_PRE_ITEM40_DIGEST, GATE_RECEIPT_0154_DIGEST}, set(registered))
        unexempted = {item.boundary_sha256: item for item in self.unexempted.violations}
        for digest, schema in schemas.items():
            with self.subTest(digest=digest[:12], schema=schema):
                if schema == "codex-upgrade-external-gate-producer/v3":
                    # v3 历史收据原样承接旧身份，无需登记。
                    self.assertNotIn(digest, unexempted)
                elif schema == GATE_RECEIPT_V4_SCHEMA and digest in registered:
                    # 已登记的 v4 部署版本只允许只读重放，不需要也不得再豁免。
                    self.assertNotIn(digest, unexempted)
                else:
                    # 其余（v1／v2 已退役格式、未部署的 R15 首版）只能靠门禁基线前的历史豁免。
                    self.assertIn(digest, unexempted)
                    self.assertIn(digest, gate.HISTORICAL_EXEMPTIONS[gate.GATE_RECEIPT_PRODUCER])
                    self.assertEqual(
                        unexempted[digest].registrable,
                        schema == GATE_RECEIPT_V4_SCHEMA,
                        "退役格式的摘要登记无效，门禁须如实指出",
                    )
        self.assertIn(GATE_RECEIPT_PRE_ITEM40_DIGEST, schemas)
        self.assertIn(GATE_RECEIPT_0154_DIGEST, schemas)

    def test_activation_receipt_boundaries_follow_producer_schema(self) -> None:
        boundaries = self.report.boundaries[gate.PRODUCTION_ACTIVATION_PRODUCER]
        current = self.report.current[gate.PRODUCTION_ACTIVATION_PRODUCER]
        registered = activation_receipt.REGISTERED_REPLAY_PRODUCER_HASHES[activation_receipt.PRODUCER_SCHEMA]
        unexempted = {item.boundary_sha256: item for item in self.unexempted.violations}
        for digest, boundary in boundaries.items():
            if digest == current:
                continue
            schema = gate.historical_producer_constants(boundary.content)[0]
            with self.subTest(digest=digest[:12], schema=schema):
                if schema == ACTIVATION_V2_SCHEMA:
                    # v2 部署边界全部登记为只读重放身份（无需豁免）。
                    self.assertIn(digest, registered)
                    self.assertNotIn(digest, unexempted)
                else:
                    # v1 收据格式已退役：登记无效，只能靠门禁基线前的历史豁免。
                    self.assertIn(digest, unexempted)
                    self.assertFalse(unexempted[digest].registrable)
                    self.assertIn(digest, gate.HISTORICAL_EXEMPTIONS[gate.PRODUCTION_ACTIVATION_PRODUCER])
        self.assertIn(ACTIVATION_PRE_ITEM42_DIGEST, boundaries)
        self.assertIn(ACTIVATION_R34_DIGEST, boundaries)

    def test_committed_receipts_have_replayable_producer_identities(self) -> None:
        # 仓库内入库的门禁收据与生产激活收据：生成它们的摘要都必须能被当前生成器按原格式承接。
        expectations = {
            "codex-upgrade-external-gate-receipt/v4": gate_receipt_module,
            "codex-production-activation-receipt/v2": activation_receipt,
        }
        checked = []
        for path in sorted((REPOSITORY_ROOT / gate.MAINTENANCE_RELATIVE).glob("*.json")):
            payload = json.loads(path.read_bytes())
            module = expectations.get(payload.get("schema_version")) if isinstance(payload, dict) else None
            if module is None:
                continue
            producer = payload["producer"]
            current = _sha256(Path(module.__file__).resolve().read_bytes())
            registered = module.REGISTERED_REPLAY_PRODUCER_HASHES[module.PRODUCER_SCHEMA]
            with self.subTest(receipt=path.name):
                self.assertEqual(producer["schema_version"], module.PRODUCER_SCHEMA)
                self.assertTrue(
                    producer["tool_sha256"] == current or producer["tool_sha256"] in registered,
                    f"{path.name} 的生成器摘要 {producer['tool_sha256']} 未登记为只读重放身份",
                )
            checked.append(path.name)
        # 至少覆盖目前已入库的这四份（证明确实在检查）；今后新入库的门禁／生产激活收据同样逐份校验，
        # 不写死清单，新收据入库时不误红。
        self.assertLessEqual(
            {
                "CODEX_CLI_0147_TO_01491_R34_PRODUCTION_ACTIVATION_RECEIPT.json",
                "CODEX_CLI_01491_TO_0151_PRODUCTION_ACTIVATION_RECEIPT.json",
                "CODEX_CLI_0151_TO_0154_POST_PROMOTION_GATE_RECEIPT.json",
                "CODEX_CLI_0151_TO_0154_PRODUCTION_ACTIVATION_RECEIPT.json",
            },
            set(checked),
        )

    def test_every_replay_registry_constant_is_covered(self) -> None:
        # 任何模块新增只读重放登记常量，都必须先纳入门禁覆盖清单。
        covered = {spec.path for spec in gate.PRODUCERS}
        tool_root = REPOSITORY_ROOT / TOOL_RELATIVE
        found: dict[str, list[str]] = {}
        for path in sorted(tool_root.rglob("*.py")):
            relative = path.relative_to(tool_root)
            if {"tests", "versions", "__pycache__"} & set(relative.parts):
                continue
            # 行首无缩进的"常量名 [: 注解] ="即模块级赋值；先用正则粗筛，命中后再用 AST 确认是模块级。
            candidates = {
                name
                for name in MODULE_CONSTANT_RE.findall(path.read_text(encoding="utf-8"))
                if gate.REGISTRY_CONSTANT_PATTERN.search(name)
            }
            if not candidates:
                continue
            tree = ast.parse(path.read_bytes(), filename=str(path))
            for node in tree.body:
                targets: Iterable[ast.expr] = ()
                if isinstance(node, ast.Assign):
                    targets = node.targets
                elif isinstance(node, ast.AnnAssign):
                    targets = (node.target,)
                for target in targets:
                    if isinstance(target, ast.Name) and target.id in candidates:
                        found.setdefault(f"{TOOL_RELATIVE}/{relative.as_posix()}", []).append(target.id)
        uncovered = {path: names for path, names in found.items() if path not in covered}
        self.assertEqual({}, uncovered, "以下生成器带只读重放登记常量，但未纳入门禁覆盖清单 PRODUCERS")
        self.assertEqual(covered, set(found), "覆盖清单中的生成器必须仍然定义只读重放登记常量")
        for spec in gate.PRODUCERS:
            self.assertIn(spec.registry, found[spec.path])


# ---------------------------------------------------------------------------
# 2. 第 36 项事故的真实历史形态
# ---------------------------------------------------------------------------


class Item36IncidentShapeTest(unittest.TestCase):
    """只读导出真实历史形态：事故形态失败，补登记与事故前形态通过。"""

    SHAPES = (
        ITEM36_COMMIT,
        ITEM36_RECEIPT_COMMIT,
        ITEM36B_PATCH_COMMIT,
        ITEM36B_RECEIPT_COMMIT,
        R15_REVIEW_FIX_COMMIT,
    )

    @classmethod
    def setUpClass(cls) -> None:
        _require_full_history()
        missing = [
            commit
            for commit, present in gate.commit_presence(
                REPOSITORY_ROOT, [("shape", {"base_commit": commit}) for commit in cls.SHAPES]
            ).items()
            if not present
        ]
        if missing:
            raise AssertionError(f"完整历史中缺少第 36 项事故坐标提交：{missing}")
        cls._temporary = tempfile.TemporaryDirectory(prefix="replay-gate-shapes-")
        cls.reports: dict[str, gate.GateReport] = {}
        for commit in cls.SHAPES:
            shape = _export_commit_shape(commit, Path(cls._temporary.name) / commit[:12])
            cls.reports[commit] = gate.check_registration(shape, git_root=REPOSITORY_ROOT)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._temporary.cleanup()

    def _assert_item36_violation(self, commit: str) -> None:
        report = self.reports[commit]
        self.assertFalse(report.passed, f"{commit[:9]} 形态应当失败")
        self.assertEqual(
            {(gate.ENVIRONMENT_RECEIPT_PRODUCER, ITEM36_MISSING_DIGEST)},
            _violation_digests(report),
            gate.format_report(report),
        )
        violation = report.violations[0]
        self.assertEqual(violation.current_sha256, ITEM36_BROKEN_DIGEST)
        self.assertEqual(violation.registry_key, "8")
        self.assertIn("ARM64 事实采集器身份漂移", violation.detail)
        message = gate.format_report(report)
        self.assertIn(ITEM36_MISSING_DIGEST, message)
        self.assertIn('REGISTERED_REPLAY_PRODUCER_HASHES["8"]', message)
        self.assertIn("codex_upgrade_arm64_environment_receipt.py", message)
        self.assertIn("upstream-codex-01561-r15-review-fix-20260924-freeze-successor.json", message)

    def test_item36_commit_shape_fails_and_names_digest_and_registry(self) -> None:
        self._assert_item36_violation(ITEM36_COMMIT)

    def test_item36_successor_receipt_does_not_bypass_gate(self) -> None:
        # 先生成承接收据（to=48304c4c）也不能绕过：前序 9e10bd0f 仍是部署边界。
        self._assert_item36_violation(ITEM36_RECEIPT_COMMIT)
        sources = {
            source.kind
            for violation in self.reports[ITEM36_RECEIPT_COMMIT].violations
            for source in violation.sources
        }
        self.assertIn("显式 predecessor_sha256s", sources)

    def test_item36b_patch_shapes_pass(self) -> None:
        for commit in (ITEM36B_PATCH_COMMIT, ITEM36B_RECEIPT_COMMIT):
            report = self.reports[commit]
            self.assertTrue(report.passed, gate.format_report(report))
            self.assertIn(
                (gate.ENVIRONMENT_RECEIPT_PRODUCER, ITEM36_BROKEN_DIGEST),
                {(spec.path, digest) for spec, digest, _ in report.exempted},
            )

    def test_pre_incident_shape_passes_without_false_positive(self) -> None:
        report = self.reports[R15_REVIEW_FIX_COMMIT]
        self.assertTrue(report.passed, gate.format_report(report))
        self.assertEqual(report.current[gate.ENVIRONMENT_RECEIPT_PRODUCER], ITEM36_MISSING_DIGEST)

    def test_generators_without_registry_in_historical_shapes_are_skipped(self) -> None:
        # 第 40、42 项之前门禁收据与生产激活收据生成器还没有登记机制：历史形态上如实跳过，
        # 不影响其它生成器的判定。
        for commit, report in self.reports.items():
            with self.subTest(commit=commit[:9]):
                self.assertEqual(
                    [spec.path for spec, _ in report.skipped],
                    [gate.GATE_RECEIPT_PRODUCER, gate.PRODUCTION_ACTIVATION_PRODUCER],
                )
                for _, reason in report.skipped:
                    self.assertIn("尚无只读重放判定所需的 REGISTERED_REPLAY_PRODUCER_HASHES", reason)


# ---------------------------------------------------------------------------
# 3. 反证：在当前树副本上变异
# ---------------------------------------------------------------------------


def _insert_into_group(source: str, pattern: str, digest: str) -> str:
    """在匹配 pattern 的 frozenset({ 开头插入一个摘要字面量；必须恰好命中一次。"""

    matches = list(re.finditer(pattern, source))
    if len(matches) != 1:
        raise AssertionError(f"登记位置 {pattern!r} 命中 {len(matches)} 次，反证夹具需要随生成器格式更新")
    end = matches[0].end()
    return source[:end] + f'"{digest}", ' + source[end:]


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise AssertionError(f"反证夹具期望 {old[:40]}… 恰好出现一次，实际 {source.count(old)} 次")
    return source.replace(old, new)


class RegistrationMutationTest(unittest.TestCase):
    """在当前树副本上模拟"改生成器"与"改登记"，证明门禁确实会拦、按提示登记后确实放行。

    判定一律按差集，不写死与仓库承接收据数量相关的精确违规集合：

    * 副本中生成器任何字节变化都会让真实 HEAD 摘要 C 变成"旧摘要"。C 是否已是部署边界，取决于
      之后有没有承接收据登记过它（785a66bdb 的收据登记了门禁收据 6cea115b、生产激活收据 ef971f5a，
      以前写死"恰好一条违规"的用例因此多出 C 而误红）。
    * ``before``：未变异副本上的违规集合；``control``：只给该生成器追加一行注释（只改字节、不动登记）
      后的违规集合。两者按类缓存。
    * "改生成器却不登记上一边界"类用例断言 ``after − before`` 恰为 {C}；"删／挪某条登记"类用例断言
      ``after − control`` 恰为该条目标摘要；"正确登记"类用例断言 ``after − before`` 为空。同时要求
      变异不得让已有违规消失，且每条违规都对应真实部署边界。
    * 基线自检只要求未变异副本复现真实树自己的门禁结果（副本保真）；真实树是否通过由第 1 组用例负责，
      同一原因不在两处报红。

    这样仓库里今后新增的承接收据，无论让 C 还是别的摘要成为新边界，都同时出现在基线与变异结果中，
    不会让用例误红；门禁本身的判定一字未改。``RegistrationMutationWithSimulatedReceiptTest`` 追加一份
    模拟承接收据重跑全部用例，证明这一点。
    """

    SIMULATE_FUTURE_RECEIPT = False
    SIMULATED_RECEIPT = "upstream-codex-replay-gate-simulated-future-20261001-freeze-successor.json"
    NEUTRAL_EDIT = "\n# 反证：只改一行注释，生成器身份即改变。\n"

    @classmethod
    def setUpClass(cls) -> None:
        _require_full_history()
        cls.baseline = gate.check_registration(REPOSITORY_ROOT)
        cls.head_commit = _git(REPOSITORY_ROOT, "rev-parse", "HEAD").decode().strip()
        cls._class_temporary = tempfile.TemporaryDirectory(prefix="replay-gate-mutation-baseline-")
        root = Path(cls._class_temporary.name)
        before_shape = cls._prepare_shape(root / "before")
        control_shape = cls._prepare_shape(root / "control")
        cls.before_by_producer: dict[str, set[str]] = {}
        cls.before_boundaries: dict[str, set[str]] = {}
        cls.control_by_producer: dict[str, set[str]] = {}
        for spec in gate.PRODUCERS:
            before = gate.check_registration(before_shape, git_root=REPOSITORY_ROOT, producers=[spec])
            cls.before_by_producer[spec.path] = _violation_digest_set(before)
            cls.before_boundaries[spec.path] = set(before.boundaries[spec.path])
            path = control_shape / spec.path
            _write(path, path.read_bytes() + cls.NEUTRAL_EDIT.encode("utf-8"))
            cls.control_by_producer[spec.path] = _violation_digest_set(
                gate.check_registration(control_shape, git_root=REPOSITORY_ROOT, producers=[spec])
            )

    @classmethod
    def tearDownClass(cls) -> None:
        cls._class_temporary.cleanup()

    @classmethod
    def _prepare_shape(cls, destination: Path) -> Path:
        shape = _copy_worktree_shape(destination)
        if cls.SIMULATE_FUTURE_RECEIPT:
            cls._write_simulated_future_receipt(shape)
        return shape

    @classmethod
    def simulated_future_digest(cls, spec: gate.ProducerSpec) -> str:
        """模拟收据里"今后部署过、副本却从未登记"的新边界摘要（最坏情况）。"""

        return _sha256(f"replay-gate-simulated-future-deployment:{spec.path}".encode("utf-8"))

    @classmethod
    def _write_simulated_future_receipt(cls, shape: Path) -> None:
        """追加一份模拟的承接收据，为每个生成器新增部署边界。

        每个生成器两条边：已知边界 → 当前摘要 C（与 785a66bdb 的收据同形态，把当前摘要登记为边界），
        以及 C → 一个从未登记的新摘要（最坏情况：新增了一个门禁会报的边界）。前后提交都写 HEAD，
        使前后提交口径同样把 C 计为边界。
        """

        # 前序取真实树上已被接受（非当前、非豁免、非违规）的已知边界：只添节点，不引入额外违规。
        unaccepted = {digest for _, digest, _ in cls.baseline.exempted} | _violation_digest_set(cls.baseline)
        transitions = []
        for spec in gate.PRODUCERS:
            current = cls.baseline.current[spec.path]
            known = sorted(
                digest
                for digest in cls.baseline.boundaries[spec.path]
                if digest != current and digest not in unaccepted
            )
            edges = [(known[0], current)] if known else []
            edges.append((current, cls.simulated_future_digest(spec)))
            for predecessor, successor in edges:
                transitions.append(
                    {
                        "path": spec.path,
                        "old_path": "",
                        "status": "M",
                        "predecessor_sha256s": [predecessor],
                        "to_sha256": successor,
                        "source_receipts": [],
                        "reason": "门禁反证：模拟今后新增的承接收据",
                    }
                )
        unsigned = {
            "schema_version": "official-egress-upstream-freeze-successor/v1",
            "issued_at_utc": "2026-10-01T00:00:00Z",
            "base_commit": cls.head_commit,
            "current_commit": cls.head_commit,
            "scope": cls.SIMULATED_RECEIPT[:-5],
            "mode": "commit",
            "transitions": transitions,
            "result": "manual_actions_required",
        }
        _write(
            shape / gate.MAINTENANCE_RELATIVE / cls.SIMULATED_RECEIPT,
            (json.dumps(unsigned, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        )

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="replay-gate-mutation-")
        self.addCleanup(self._temporary.cleanup)
        self.shape = self._prepare_shape(Path(self._temporary.name))

    def _head_digest(self, spec: gate.ProducerSpec) -> str:
        digest = self.baseline.current[spec.path]
        if digest not in self.before_boundaries[spec.path]:
            self.skipTest(f"{spec.path} 当前改动尚未登记冻结承接收据，当前摘要还不是部署边界")
        return digest

    def _mutate(self, spec: gate.ProducerSpec, transform) -> str:
        path = self.shape / spec.path
        text = transform(path.read_text(encoding="utf-8"))
        _write(path, text.encode("utf-8"))
        return _sha256(path.read_bytes())

    def _check(self, spec: gate.ProducerSpec, **options) -> gate.GateReport:
        return gate.check_registration(self.shape, git_root=REPOSITORY_ROOT, producers=[spec], **options)

    def _new_violations(self, spec: gate.ProducerSpec, report: gate.GateReport, baseline: set[str]) -> set[str]:
        """变异后相对基线新增的违规摘要；同时要求已有违规不消失、每条违规都是真实部署边界。"""

        after = _violation_digest_set(report)
        self.assertEqual(set(), baseline - after, "变异不应让基线上已有的违规消失\n" + gate.format_report(report))
        for digest in after:
            self.assertIn(digest, report.boundaries[spec.path], "每条违规都必须对应真实部署边界")
        return after - baseline

    def _assert_new_since_before(self, spec: gate.ProducerSpec, report: gate.GateReport, expected: set[str]) -> None:
        new = self._new_violations(spec, report, self.before_by_producer[spec.path])
        self.assertEqual(expected, new, gate.format_report(report))

    def _assert_new_since_control(self, spec: gate.ProducerSpec, report: gate.GateReport, expected: set[str]) -> None:
        new = self._new_violations(spec, report, self.control_by_producer[spec.path])
        self.assertEqual(expected, new, gate.format_report(report))

    @staticmethod
    def _violation_for(report: gate.GateReport, digest: str) -> gate.Violation:
        return next(item for item in report.violations if item.boundary_sha256 == digest)

    def _producer_version(self, spec: gate.ProducerSpec) -> str:
        # 测试自己解析版本号，不复用门禁的解析函数，反证替换门禁逻辑时不会波及夹具。
        match = re.search(r'^PRODUCER_VERSION\s*=\s*"([^"\n]+)"', (self.shape / spec.path).read_text(encoding="utf-8"), re.M)
        self.assertIsNotNone(match, f"{spec.path} 缺少 PRODUCER_VERSION")
        return match.group(1)

    # --- 基线自检 ---

    def test_unmodified_and_control_baselines(self) -> None:
        # 未变异副本必须复现真实树自己的门禁结果（副本保真；真实树是否通过由第 1 组用例负责，这里
        # 不重复报），追加模拟收据时另含该生成器的模拟新边界。只改字节的对照组不得让已有违规消失；
        # 相对未变异副本，摘要集合类恰好新增当前摘要 C（C 还不是部署边界时不新增），承接链类的新增
        # 全是部署边界且含 C（改了计时账本又不追加承接描述，旧边界都走不到新摘要）。
        for spec in gate.PRODUCERS:
            with self.subTest(path=spec.path):
                real = {item.boundary_sha256 for item in self.baseline.violations if item.producer.path == spec.path}
                simulated = {self.simulated_future_digest(spec)} if self.SIMULATE_FUTURE_RECEIPT else set()
                before = self.before_by_producer[spec.path]
                self.assertEqual(real | simulated, before)
                control = self.control_by_producer[spec.path]
                self.assertEqual(set(), before - control)
                added = control - before
                current = self.baseline.current[spec.path]
                head_is_boundary = current in self.before_boundaries[spec.path]
                if spec.mechanism == gate.MECHANISM_SUCCESSOR_CHAIN:
                    self.assertLessEqual(added, self.before_boundaries[spec.path])
                    if head_is_boundary:
                        self.assertIn(current, added)
                else:
                    self.assertEqual({current} if head_is_boundary else set(), added)

    # --- ARM64 环境收据（按版本分组的显式摘要集合） ---

    def test_environment_edit_without_registering_previous_boundary_fails(self) -> None:
        head = self._head_digest(ENVIRONMENT_SPEC)
        version = self._producer_version(ENVIRONMENT_SPEC)
        self._mutate(ENVIRONMENT_SPEC, lambda text: text + self.NEUTRAL_EDIT)
        report = self._check(ENVIRONMENT_SPEC)
        self._assert_new_since_before(ENVIRONMENT_SPEC, report, {head})
        self.assertEqual(self._violation_for(report, head).registry_key, version)
        self.assertIn(f'REGISTERED_REPLAY_PRODUCER_HASHES["{version}"]', gate.format_report(report))

    def test_environment_edit_cannot_exempt_previous_boundary(self) -> None:
        # 把上一部署边界（已部署版本）塞进历史豁免表也绕不过：它不是门禁基线前的旧边界。
        head = self._head_digest(ENVIRONMENT_SPEC)
        self._mutate(ENVIRONMENT_SPEC, lambda text: text + "\n# 反证：试图用豁免表绕过登记。\n")
        exemptions = {
            path: dict(digests) for path, digests in gate.HISTORICAL_EXEMPTIONS.items()
        }
        exemptions.setdefault(ENVIRONMENT_SPEC.path, {})[head] = "反证：声称该版本从未部署"
        report = self._check(ENVIRONMENT_SPEC, exemptions=exemptions)
        self._assert_new_since_before(ENVIRONMENT_SPEC, report, {head})
        self.assertIn("不得豁免", self._violation_for(report, head).detail)

    def test_environment_registration_in_producing_version_passes(self) -> None:
        head = self._head_digest(ENVIRONMENT_SPEC)
        version = self._producer_version(ENVIRONMENT_SPEC)
        self._mutate(
            ENVIRONMENT_SPEC,
            lambda text: _insert_into_group(text, rf'"{version}"\s*:\s*frozenset\(\s*\{{\s*', head),
        )
        self._assert_new_since_before(ENVIRONMENT_SPEC, self._check(ENVIRONMENT_SPEC), set())

    def test_environment_registration_in_wrong_version_fails(self) -> None:
        head = self._head_digest(ENVIRONMENT_SPEC)
        version = self._producer_version(ENVIRONMENT_SPEC)
        wrong = str(int(version) - 1)
        self._mutate(
            ENVIRONMENT_SPEC,
            lambda text: _insert_into_group(text, rf'"{wrong}"\s*:\s*frozenset\(\s*\{{\s*', head),
        )
        self._assert_new_since_before(ENVIRONMENT_SPEC, self._check(ENVIRONMENT_SPEC), {head})

    def test_environment_dropping_item36_registration_fails(self) -> None:
        # 删掉事故中的 9e10bd0f 登记：只改字节的对照组之外，恰好多出 9e10bd0f。
        self._mutate(
            ENVIRONMENT_SPEC,
            lambda text: _replace_once(text, f'"{ITEM36_MISSING_DIGEST}"', ""),
        )
        self._assert_new_since_control(ENVIRONMENT_SPEC, self._check(ENVIRONMENT_SPEC), {ITEM36_MISSING_DIGEST})

    # --- 收据终结器（扁平摘要集合） ---

    def test_finalizer_edit_without_registering_previous_boundary_fails(self) -> None:
        head = self._head_digest(FINALIZER_SPEC)
        self._mutate(FINALIZER_SPEC, lambda text: text + self.NEUTRAL_EDIT)
        report = self._check(FINALIZER_SPEC)
        self._assert_new_since_before(FINALIZER_SPEC, report, {head})
        self.assertIn("LEGACY_REPLAY_PRODUCER_HASHES", gate.format_report(report))

    def test_finalizer_registration_passes(self) -> None:
        head = self._head_digest(FINALIZER_SPEC)
        self._mutate(
            FINALIZER_SPEC,
            lambda text: _insert_into_group(text, r"LEGACY_REPLAY_PRODUCER_HASHES\s*=\s*frozenset\(\s*\{\s*", head),
        )
        self._assert_new_since_before(FINALIZER_SPEC, self._check(FINALIZER_SPEC), set())

    def test_finalizer_dropping_existing_registration_fails(self) -> None:
        self._mutate(
            FINALIZER_SPEC,
            lambda text: _replace_once(text, f'"{FINALIZER_REGISTERED_BOUNDARY}",', ""),
        )
        self._assert_new_since_control(
            FINALIZER_SPEC, self._check(FINALIZER_SPEC), {FINALIZER_REGISTERED_BOUNDARY}
        )

    # --- 计时账本（承接收据链） ---

    PROBE_RECEIPT = "upstream-codex-replay-gate-probe-20260928-freeze-successor.json"

    def _append_successor_descriptor(self, text: str) -> str:
        anchor = text.index("PRODUCER_FREEZE_SUCCESSORS = (")
        end = text.index("\n)\n", anchor)
        descriptor = (
            "\n    {\n"
            f'        "path": "{gate.MAINTENANCE_RELATIVE}/{self.PROBE_RECEIPT}",\n'
            f'        "base_commit": "{self.head_commit}",\n'
            f'        "scope": "{self.PROBE_RECEIPT[:-5]}",\n'
            '        "result": "manual_actions_required",\n'
            "    },"
        )
        return text[:end] + descriptor + text[end:]

    def _write_probe_receipt(self, predecessor: str, successor: str, path: str | None = None) -> None:
        """按 freeze-successor-generate 的字段合同合成一份承接收据（compact 自摘要，无尾换行）。"""

        unsigned = {
            "schema_version": "official-egress-upstream-freeze-successor/v1",
            "issued_at_utc": "2026-09-28T03:00:00Z",
            "base_commit": self.head_commit,
            "current_commit": "0" * 40,
            "scope": self.PROBE_RECEIPT[:-5],
            "mode": "commit",
            "extra_worktree_paths": [],
            "frozen_path_count": 1,
            "frozen_edge_count": 1,
            "changed_path_count": 1,
            "transitions": [
                {
                    "path": path or TIMING_SPEC.path,
                    "old_path": "",
                    "status": "M",
                    "predecessor_sha256s": [predecessor],
                    "to_sha256": successor,
                    "source_receipts": [
                        f"{gate.MAINTENANCE_RELATIVE}/upstream-codex-0157-batch3-fix-and-continue-20260927-freeze-successor.json"
                    ],
                    "reason": "门禁反证：合成的计时账本承接边",
                }
            ],
            "unregistered_path_count": 0,
            "unregistered_paths": [],
            "deleted_frozen_paths": [],
            "required_manual_actions": [],
            "verification": [],
            "safety": {
                "deployment_performed": False,
                "live_account_used": False,
                "official_egress_profile_changed": False,
                "production_config_changed": False,
                "wire_or_persona_selection_changed": False,
            },
            "result": "manual_actions_required",
        }
        compact = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        payload = dict(unsigned, identity_sha256=_sha256(compact))
        _write(
            self.shape / gate.MAINTENANCE_RELATIVE / self.PROBE_RECEIPT,
            (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode(),
        )

    def test_timing_ledger_edit_without_successor_descriptor_fails(self) -> None:
        head = self._head_digest(TIMING_SPEC)
        edited = self._mutate(TIMING_SPEC, lambda text: text + "\n# 反证：改了计时账本但没有追加承接描述。\n")
        self._write_probe_receipt(head, edited)
        report = self._check(TIMING_SPEC)
        new = self._new_violations(TIMING_SPEC, report, self.before_by_producer[TIMING_SPEC.path])
        self.assertIn(head, new, gate.format_report(report))
        self.assertIn("PRODUCER_FREEZE_SUCCESSORS", gate.format_report(report))

    def test_timing_ledger_edit_with_successor_descriptor_passes(self) -> None:
        head = self._head_digest(TIMING_SPEC)
        edited = self._mutate(TIMING_SPEC, self._append_successor_descriptor)
        self._write_probe_receipt(head, edited)
        report = self._check(TIMING_SPEC)
        self._assert_new_since_before(TIMING_SPEC, report, set())
        self.assertGreater(len(report.boundaries[TIMING_SPEC.path]), 2)

    # --- 按 producer schema 分组的显式摘要集合：门禁收据（第 40 项）与生产激活收据（第 42 项） ---

    def _assert_schema_keyed_violation(
        self,
        report: gate.GateReport,
        spec: gate.ProducerSpec,
        schema: str,
        digest: str,
    ) -> None:
        violation = self._violation_for(report, digest)
        self.assertEqual(violation.registry_key, schema)
        self.assertTrue(violation.registrable)
        self.assertIn("未登记为只读重放身份", violation.detail)
        message = gate.format_report(report)
        self.assertIn(f'REGISTERED_REPLAY_PRODUCER_HASHES["{schema}"]', message)
        self.assertIn(Path(spec.path).name, message)

    def _assert_dropping_registration_fails(
        self,
        spec: gate.ProducerSpec,
        schema: str,
        digest: str,
        transform=None,
    ) -> None:
        """删掉（或挪走）一条登记：与只改字节的对照组相比，恰好多出这条登记的摘要。"""

        transform = transform or (lambda text: _replace_once(text, f'"{digest}",', ""))
        self._mutate(spec, transform)
        report = self._check(spec)
        self._assert_new_since_control(spec, report, {digest})
        self._assert_schema_keyed_violation(report, spec, schema, digest)

    def _assert_next_edit_must_register_current(
        self,
        spec: gate.ProducerSpec,
        schema: str,
        predecessor: str,
    ) -> None:
        """当前摘要成为部署边界之后再次修改生成器：必须登记当前摘要，登记后不再新增违规。"""

        head = self.baseline.current[spec.path]
        # 探针收据保证当前摘要是部署边界（仓库里已有承接收据登记它时，这里不改变任何结论）。
        self._write_probe_receipt(predecessor, head, spec.path)
        self._mutate(spec, lambda text: text + "\n# 反证：再次修改生成器。\n")
        report = self._check(spec)
        self._assert_new_since_before(spec, report, {head})
        self._assert_schema_keyed_violation(report, spec, schema, head)
        self._mutate(
            spec,
            lambda text: _insert_into_group(text, r"PRODUCER_SCHEMA: frozenset\(\s*\{\s*", head),
        )
        self._assert_new_since_before(spec, self._check(spec), set())

    def test_gate_receipt_item40_edit_without_registering_pre_edit_digest_fails(self) -> None:
        # 第 40 项自身的事故形态：改了门禁收据生成器，却没把修改前的 034331e5 登记为只读重放身份。
        self._assert_dropping_registration_fails(GATE_SPEC, GATE_RECEIPT_V4_SCHEMA, GATE_RECEIPT_PRE_ITEM40_DIGEST)

    def test_gate_receipt_dropping_0154_registration_fails(self) -> None:
        self._assert_dropping_registration_fails(GATE_SPEC, GATE_RECEIPT_V4_SCHEMA, GATE_RECEIPT_0154_DIGEST)

    def test_gate_receipt_registration_under_wrong_schema_fails(self) -> None:
        def move_to_legacy_group(text: str) -> str:
            text = _replace_once(text, f'"{GATE_RECEIPT_PRE_ITEM40_DIGEST}",', "")
            anchor = "REGISTERED_REPLAY_PRODUCER_HASHES: dict[str, frozenset[str]] = {\n"
            return _replace_once(
                text,
                anchor,
                anchor + f'    LEGACY_PRODUCER_SCHEMA: frozenset({{"{GATE_RECEIPT_PRE_ITEM40_DIGEST}"}}),\n',
            )

        self._assert_dropping_registration_fails(
            GATE_SPEC, GATE_RECEIPT_V4_SCHEMA, GATE_RECEIPT_PRE_ITEM40_DIGEST, move_to_legacy_group
        )

    def test_gate_receipt_next_edit_must_register_current_digest(self) -> None:
        self._assert_next_edit_must_register_current(
            GATE_SPEC, GATE_RECEIPT_V4_SCHEMA, GATE_RECEIPT_PRE_ITEM40_DIGEST
        )

    def test_activation_item42_edit_without_registering_pre_edit_digest_fails(self) -> None:
        # 第 42 项自身的事故形态：改了生产激活收据生成器，却没把修改前的 3b4ddbf8 登记为只读重放身份。
        self._assert_dropping_registration_fails(ACTIVATION_SPEC, ACTIVATION_V2_SCHEMA, ACTIVATION_PRE_ITEM42_DIGEST)

    def test_activation_dropping_r34_registration_fails(self) -> None:
        self._assert_dropping_registration_fails(ACTIVATION_SPEC, ACTIVATION_V2_SCHEMA, ACTIVATION_R34_DIGEST)

    def test_activation_next_edit_must_register_current_digest(self) -> None:
        self._assert_next_edit_must_register_current(
            ACTIVATION_SPEC, ACTIVATION_V2_SCHEMA, ACTIVATION_PRE_ITEM42_DIGEST
        )


class RegistrationMutationWithSimulatedReceiptTest(RegistrationMutationTest):
    """追加一份模拟的"今后新增承接收据"后重跑全部变异用例：新增边界不得让它们误红。

    模拟收据为每个生成器新增两条边：已知边界 → 当前摘要（785a66bdb 收据的形态），以及当前摘要 →
    一个从未登记的新摘要（最坏情况，门禁会把它当违规报出）。未变异副本与对照组都带着这些新边界，
    变异用例按差集判定，结论与不追加时相同。
    """

    SIMULATE_FUTURE_RECEIPT = True


# ---------------------------------------------------------------------------
# 3b. 覆盖自检：静态发现"要求摘要严格等于当前、却没有登记常量"的生成器（第 42 项）
# ---------------------------------------------------------------------------


STRICT_WITH_REPLAY_SOURCE = '''
import hashlib
from pathlib import Path


def _sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_receipt(facts):
    tool = Path(__file__).resolve()
    return {"facts": facts, "producer": {"tool": str(tool), "tool_sha256": _sha256_file(tool)}}


def replay(receipt):
    if build_receipt(receipt["facts"]) != receipt:
        raise ValueError("重放结果不一致")
    return receipt
'''

WRITE_ONLY_SOURCE = '''
import hashlib
from pathlib import Path

SELF = Path(__file__)


def generate():
    return {"sha256": hashlib.sha256(SELF.read_bytes()).hexdigest()}


def checkpoint():
    return generate()
'''

SIBLING_DIGEST_SOURCE = '''
import hashlib
from pathlib import Path


def verify_sibling():
    other = Path(__file__).resolve().with_name("other_tool.py")
    return hashlib.sha256(other.read_bytes()).hexdigest()
'''


class SelfDigestCoverageTest(unittest.TestCase):
    """覆盖自检必须能发现"计算自身摘要且校验路径要求等于当前"的生成器，并要求逐个归类。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.findings = gate.find_self_digest_producers(REPOSITORY_ROOT)

    def test_every_strict_self_digest_producer_is_classified(self) -> None:
        unclassified = gate.unclassified_strict_producers(self.findings)
        self.assertEqual(
            {},
            unclassified,
            "以下生成器要求自身摘要等于当前，却既没有只读重放登记，也没有写明不跨工具版本重放的依据："
            + "；".join(
                f"{path}（校验入口 {'、'.join(finding.verification_functions)}）"
                for path, finding in sorted(unclassified.items())
            )
            + "。收据会跨工具版本重放时，加只读重放登记并纳入 PRODUCERS；否则写进 STRICT_SELF_DIGEST_WITHOUT_REGISTRY。",
        )
        strict = {path for path, finding in self.findings.items() if finding.strict}
        covered = {spec.path for spec in gate.PRODUCERS}
        allowed = set(gate.STRICT_SELF_DIGEST_WITHOUT_REGISTRY)
        self.assertEqual(set(), covered & allowed, "同一生成器不能既登记又声明不跨版本重放")
        self.assertEqual(set(), allowed - strict, "分类表中的条目已不再要求自身摘要等于当前，应删除")
        self.assertEqual(set(), covered - strict, "覆盖清单中的生成器未被识别为要求自身摘要等于当前，检测规则需复核")
        for path, reason in gate.STRICT_SELF_DIGEST_WITHOUT_REGISTRY.items():
            self.assertTrue(reason.strip(), f"{path} 缺少不跨版本重放的依据")

    def test_self_check_would_have_caught_items_40_and_42(self) -> None:
        # 只按门禁引入时的覆盖清单（前三个生成器）判定，当前树中未归类的恰好是第 40、42 项补登记的两个。
        original = [
            spec
            for spec in gate.PRODUCERS
            if spec.path not in {gate.GATE_RECEIPT_PRODUCER, gate.PRODUCTION_ACTIVATION_PRODUCER}
        ]
        self.assertEqual(
            {gate.GATE_RECEIPT_PRODUCER, gate.PRODUCTION_ACTIVATION_PRODUCER},
            set(gate.unclassified_strict_producers(self.findings, producers=original)),
        )

    def test_pre_fix_generators_are_strict_without_registry(self) -> None:
        # 分支起点（第 40、42 项之前）的两个生成器：要求自身摘要等于当前，且没有任何登记常量。
        _require_full_history()
        for path in (gate.GATE_RECEIPT_PRODUCER, gate.PRODUCTION_ACTIVATION_PRODUCER):
            with self.subTest(path=path):
                source = _git(REPOSITORY_ROOT, "cat-file", "blob", f"{BRANCH_BASE_COMMIT}:{path}")
                finding = gate.analyze_self_digest_source(source)
                self.assertIsNotNone(finding)
                self.assertTrue(finding.strict)
                self.assertIn("replay", finding.verification_functions)
                registry_constants = [
                    name
                    for name in MODULE_CONSTANT_RE.findall(source.decode("utf-8"))
                    if gate.REGISTRY_CONSTANT_PATTERN.search(name)
                ]
                self.assertEqual([], registry_constants)

    def test_detector_on_synthetic_sources(self) -> None:
        strict = gate.analyze_self_digest_source(STRICT_WITH_REPLAY_SOURCE)
        self.assertEqual(strict.digest_functions, ("build_receipt",))
        self.assertEqual(strict.verification_functions, ("replay",))
        # 只写不校验（checkpoint 不是校验入口）：计算自身摘要但不要求等于当前。
        write_only = gate.analyze_self_digest_source(WRITE_ONLY_SOURCE)
        self.assertEqual(write_only.digest_functions, ("generate",))
        self.assertFalse(write_only.strict)
        # 求的是同目录其它文件的摘要，不是自身摘要。
        self.assertIsNone(gate.analyze_self_digest_source(SIBLING_DIGEST_SOURCE))

SYNTHETIC_PRODUCER = f"{TOOL_RELATIVE}/synthetic_replay_producer.py"
SYNTHETIC_SPEC = gate.ProducerSpec(
    path=SYNTHETIC_PRODUCER,
    mechanism=gate.MECHANISM_VERSIONED_HASH_SET,
    registry="REGISTERED_REPLAY_PRODUCER_HASHES",
    judge_attributes=("REGISTERED_REPLAY_PRODUCER_HASHES", "_validated_producer_version"),
)


def _synthetic_producer(revision: str, registered: Mapping[str, Iterable[str]] | None = None) -> bytes:
    """合成一个与 ARM64 环境收据同一登记方式的最小生成器（只读重放判定逻辑同构）。"""

    groups = ",\n".join(
        f'    "{version}": frozenset({{{", ".join(repr(item) for item in sorted(digests))}}})'
        for version, digests in sorted((registered or {}).items())
    )
    return (
        f'"""合成生成器（仅供门禁测试），修订 {revision}。"""\n'
        "import hashlib\n"
        "from pathlib import Path\n\n"
        'PRODUCER_SCHEMA = "synthetic-replay-producer/v1"\n'
        'PRODUCER_VERSION = "1"\n'
        "REGISTERED_REPLAY_PRODUCER_HASHES = {\n"
        f"{groups}\n"
        "}\n\n\n"
        "class SyntheticReplayError(ValueError):\n"
        '    """合成生成器拒绝重放。"""\n\n\n'
        "def _validated_producer_version(value, *, allow_legacy_replay):\n"
        "    current = hashlib.sha256(Path(__file__).resolve().read_bytes()).hexdigest()\n"
        '    version = value.get("version")\n'
        '    if value.get("tool_sha256") == current and version == PRODUCER_VERSION:\n'
        "        return PRODUCER_VERSION\n"
        '    if allow_legacy_replay and value.get("tool_sha256") in REGISTERED_REPLAY_PRODUCER_HASHES.get(version, frozenset()):\n'
        "        return version\n"
        '    raise SyntheticReplayError("合成生成器身份漂移")\n'
    ).encode("utf-8")


class SyntheticRepository:
    """临时 git 仓库：生成器文件 + 冻结承接图收据 + 真实提交历史。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        root.mkdir(parents=True)
        _git(root, "init", "-q", "-b", "main", isolated=True)
        # 收据范围非空所需的一份无关承接收据。
        self.write_receipt("unrelated-freeze-successor.json", {
            "schema_version": "official-egress-upstream-freeze-successor/v1",
            "issued_at_utc": "2026-09-01T00:00:00Z",
            "transitions": [],
        })

    def write_producer(self, data: bytes) -> str:
        _write(self.root / SYNTHETIC_PRODUCER, data)
        return _sha256(data)

    def write_receipt(self, name: str, payload: Mapping[str, object]) -> None:
        _write(
            self.root / gate.MAINTENANCE_RELATIVE / name,
            (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )

    def commit(self, message: str) -> str:
        _git(self.root, "add", "-A", isolated=True)
        _git(self.root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, isolated=True)
        return _git(self.root, "rev-parse", "HEAD", isolated=True).decode().strip()

    def check(self, **overrides) -> gate.GateReport:
        options = {"producers": [SYNTHETIC_SPEC], "exemptions": {}}
        options.update(overrides)
        return gate.check_registration(self.root, **options)


def _change_set_receipt(base: str, current: str, predecessor: str, successor: str, issued: str) -> dict:
    return {
        "schema_version": "official-egress-upstream-freeze-successor/v1",
        "issued_at_utc": issued,
        "base_commit": base,
        "current_commit": current,
        "mode": "commit",
        "transitions": [
            {
                "path": SYNTHETIC_PRODUCER,
                "old_path": "",
                "status": "M",
                "predecessor_sha256s": [predecessor],
                "to_sha256": successor,
                "reason": "合成变更集",
            }
        ],
    }


class SyntheticRepositoryBoundaryTest(unittest.TestCase):
    """部署边界定义的细节（合成仓库，不依赖真实历史）。"""

    def setUp(self) -> None:
        if shutil.which("git") is None:
            self.skipTest("需要 git 可执行文件")
        self._temporary = tempfile.TemporaryDirectory(prefix="replay-gate-synthetic-")
        self.addCleanup(self._temporary.cleanup)
        self.base = Path(self._temporary.name)
        self.repository = SyntheticRepository(self.base / "repository")

    def _change_set(self, issued: str = "2026-09-20T00:00:00Z") -> tuple[str, str, str]:
        """构造一次变更集：a（部署）→ b（变更集内中间提交）→ c（变更集终点），再登记承接收据。"""

        repository = self.repository
        digest_a = repository.write_producer(_synthetic_producer("a"))
        base = repository.commit("部署版本 a")
        digest_b = repository.write_producer(_synthetic_producer("b"))
        repository.commit("变更集内部中间版本 b")
        digest_c = repository.write_producer(_synthetic_producer("c"))
        current = repository.commit("变更集终点 c")
        repository.write_receipt(
            "change-set-freeze-successor.json",
            _change_set_receipt(base, current, digest_a, digest_c, issued),
        )
        repository.commit("登记承接收据")
        return digest_a, digest_b, digest_c

    def test_intermediate_commit_inside_change_set_is_not_a_boundary(self) -> None:
        digest_a, digest_b, digest_c = self._change_set()
        report = self.repository.check()
        self.assertEqual({(SYNTHETIC_PRODUCER, digest_a)}, _violation_digests(report))
        self.assertNotIn(digest_b, report.boundaries[SYNTHETIC_PRODUCER])
        self.assertIn('REGISTERED_REPLAY_PRODUCER_HASHES["1"]', gate.format_report(report))
        # 只登记 a：登记本身改变了生成器，变更集终点 c 随即成为未登记的旧边界。
        self.repository.write_producer(_synthetic_producer("d", {"1": [digest_a]}))
        self.assertEqual({(SYNTHETIC_PRODUCER, digest_c)}, _violation_digests(self.repository.check()))
        # 同时登记 a 与 c 才通过；中间版本 b 始终不要求登记。
        self.repository.write_producer(_synthetic_producer("d", {"1": [digest_a, digest_c]}))
        report = self.repository.check()
        self.assertTrue(report.passed, gate.format_report(report))

    def test_explicit_node_outside_history_must_be_registered(self) -> None:
        digest_a, _, digest_c = self._change_set()
        workspace = "ab" * 32
        receipt = _change_set_receipt("f" * 40, "e" * 40, workspace, digest_c, "2026-09-21T00:00:00Z")
        self.repository.write_receipt("workspace-freeze-successor.json", receipt)
        self.repository.write_producer(_synthetic_producer("d", {"1": [digest_a, digest_c]}))
        report = self.repository.check()
        self.assertEqual({(SYNTHETIC_PRODUCER, workspace)}, _violation_digests(report))
        self.assertIsNone(report.violations[0].registry_key)
        self.assertIn("<该摘要生成收据时的 PRODUCER_VERSION>", gate.format_report(report))
        # 引用了不存在的提交：只跳过前后提交边界，并如实记录。
        self.assertEqual({"f" * 40, "e" * 40}, set(report.unreachable_commits))
        self.repository.write_producer(_synthetic_producer("d", {"1": [digest_a, digest_c, workspace]}))
        self.assertTrue(self.repository.check().passed)

    def test_empty_file_predecessor_is_not_a_boundary(self) -> None:
        # 冻结承接图把新增文件的前序记为空内容摘要：它表示"文件尚不存在"，不是生成器版本。
        digest_a = self.repository.write_producer(_synthetic_producer("a"))
        added = self.repository.commit("新增生成器 a")
        receipt = _change_set_receipt(added, added, gate.EMPTY_FILE_SHA256, digest_a, "2026-09-20T00:00:00Z")
        self.repository.write_receipt("added-freeze-successor.json", receipt)
        self.repository.write_producer(_synthetic_producer("b", {"1": [digest_a]}))
        report = self.repository.check()
        self.assertTrue(report.passed, gate.format_report(report))
        self.assertNotIn(gate.EMPTY_FILE_SHA256, report.boundaries[SYNTHETIC_PRODUCER])
        self.assertIn(digest_a, report.boundaries[SYNTHETIC_PRODUCER])

    def test_generator_without_registry_is_skipped_and_reported(self) -> None:
        # 历史形态里登记机制尚未引入的生成器：如实跳过并记入报告（当前树由测试保证不会出现）。
        self._change_set()
        text = _synthetic_producer("d").decode("utf-8").replace(
            "REGISTERED_REPLAY_PRODUCER_HASHES", "UNRELATED_CONSTANT"
        )
        self.repository.write_producer(text.encode("utf-8"))
        report = self.repository.check()
        self.assertTrue(report.passed)
        self.assertEqual(
            [(SYNTHETIC_SPEC, "生成器尚无只读重放判定所需的 REGISTERED_REPLAY_PRODUCER_HASHES")],
            report.skipped,
        )
        self.assertIn("跳过：synthetic_replay_producer.py", gate.format_report(report))

    def test_new_boundary_after_baseline_cannot_be_exempted(self) -> None:
        digest_a, _, _ = self._change_set(issued="2026-10-01T00:00:00Z")
        exemptions = {SYNTHETIC_PRODUCER: {digest_a: "合成：声称从未部署"}}
        report = self.repository.check(exemptions=exemptions)
        self.assertEqual({(SYNTHETIC_PRODUCER, digest_a)}, _violation_digests(report))
        self.assertIn("不得豁免", report.violations[0].detail)
        # 同一条豁免若出处早于基线，则按历史豁免放行并记入报告。
        report = self.repository.check(exemptions=exemptions, baseline="2026-12-31T00:00:00Z")
        self.assertTrue(report.passed, gate.format_report(report))
        self.assertEqual([(SYNTHETIC_SPEC, digest_a)], [(spec, digest) for spec, digest, _ in report.exempted])

    def test_shallow_clone_fails_closed(self) -> None:
        self._change_set()
        shallow = self.base / "shallow"
        subprocess.run(
            ["git", "clone", "-q", "--depth", "1", f"file://{self.repository.root}", str(shallow)],
            check=True,
            capture_output=True,
            env={**os.environ, **GIT_ISOLATION},
        )
        with self.assertRaises(gate.GateError) as context:
            gate.check_registration(shallow, producers=[SYNTHETIC_SPEC], exemptions={})
        self.assertIn("浅克隆", str(context.exception))

    def test_non_git_tree_is_unavailable(self) -> None:
        plain = self.base / "plain"
        _write(plain / SYNTHETIC_PRODUCER, _synthetic_producer("a"))
        _write(plain / gate.MAINTENANCE_RELATIVE / "x-freeze-successor.json", b'{"schema_version": "x-successor/v1"}\n')
        # 临时目录即使落在某个 git 工作树之下，也不许向上找到外层仓库。
        with mock.patch.dict(os.environ, {"GIT_CEILING_DIRECTORIES": str(self.base.resolve())}):
            with self.assertRaises(gate.GateUnavailable):
                gate.check_registration(plain, producers=[SYNTHETIC_SPEC], exemptions={})


if __name__ == "__main__":
    unittest.main()
