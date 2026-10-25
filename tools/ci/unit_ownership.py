#!/usr/bin/env python3
"""门禁单元按客户端归属与按改动选择（Codex 升级工具优化第 9 项）。

问题：入口门禁、后台验证、收尾 full-gates 每轮都把 Claude 的重集成测试和 Codex 的一起跑完（474 个单元，其中可剔除的
Claude 单元约占一半核·秒），Codex 升级时这些测试几乎不会因 Codex 改动而变红，却要占约一半墙钟。

做法：给每个采集工具测试模块标"归属"（codex／claude／shared），Codex 计划只跑 codex、shared、廉价的 claude 单元；
但"改了什么要重测什么"仍要保证——计划按 ``git diff 基点..候选提交`` 的改动文件，把确实会被改动影响的 Claude 重单元
自动并入。不确定的一律并入（失败关闭）。

本文件是**纯标准库**的选择器与校验器，数据在 ``tools/ci/unit_ownership.json``（随测试树提交，物化后的结果，评审能看 diff）：

* ``derive``：从测试树现推每个测试模块的归属（规则按顺序，先命中先用）；
* ``check``：数据文件与现推结果一致、覆盖全部测试模块、联动表与 ARM64 依赖守卫成立（CI 里由测试调用）；
* ``materialize``：用现推结果加核·秒标定重新生成数据文件；
* ``select``：按客户端、基点和候选提交算出门禁清单里的 ``selection`` 块（谁跑、谁跳过、为什么并入）；
* ``report``／``diff-report``：Codex 计划与全集的差集清单（逐个带理由）、两次运行清单的差集核对；
* ``calibrate``：用一轮运行清单与单元记录重算模块核·秒。

并入规则（顺序求并集，见 ``select``）：

1. 归属计划：剔除对象只有"归属对方客户端、模块核·秒不低于廉价阈值、没被钉住"的模块，其余照跑；
2. 改动的测试模块自身、改动的 Python 文件被某个重模块（含其测试夹具）直接导入——并入该模块；Claude 面文件的改动沿
   Claude 面文件与夹具的导入链传递；
3. 枢纽文件（Codex 命名却被 Claude 面文件直接导入）被改：**只提示**（``advisories``），不并入（拍板：收尾全集与 GitHub CI 兜底）；
4. 联动文件（版本登记机械改动的 Claude 场景清单、期望画像、冻结摘要）：只并入直接验证它们的模块；
5. 非 Python 改动：按路径字面量反向索引与受管树内数据文件的读取者并入；
6. 失败关闭：改动路径不在已登记的目录范围、Python 文件解析失败、受管树内数据文件找不到任何读取者——并入全部重模块；
   归属表或选择器自己在改动里——本轮不筛选（全集）。

选择结果写进门禁清单的 ``selection`` 块，执行器据此只保留标 ``run`` 的模块；清单里单元 ID、单元规格、额度都不变，
所以和全集运行的记录逐字相通。

使用与限制：

* 入口：``entry-gates.sh --gate-scope client-plan --select-base <最近一次发版的 40 位提交>``、``background-validate.sh start`` 同名选项；
  本版只启用 Codex 计划（数据文件 ``enabled_clients``）。收尾 full-gates、VC-4／VC-5 的 ``make test`` 保持全集，不用本选项；
* 新增或改名测试模块后要重新物化（``python3 -B tools/ci/unit_ownership.py materialize``），``check``（CI 里由测试调用）会提醒；
* 1 跳口径（测试及其一层夹具直接导入）是设计取舍：更远的间接依赖由收尾全集与 GitHub CI 全集兜底；符号级收窄、读集叠加留作阶段二。
"""

from __future__ import annotations

import argparse
import ast
import csv
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

SCHEMA = "unit-ownership/v1"
SELECTION_SCHEMA = "unit-executor-gates-selection/v1"
DATA_PATH = "tools/ci/unit_ownership.json"
# 归属表与选择器自己（含驱动副本）：出现在改动里就不筛选，防止候选提交自己改规则给自己放水。
SELF_PATHS = (DATA_PATH, "tools/ci/unit_ownership.py", "tools/arm64_capture_driver/driver/unit_ownership.py")
TESTS_DIR = "tools/official_client_capture/tests"
MANAGED_ROOTS = ("tools/official_client_capture", "tools/official_client_control")
GRAPH_ROOT = "tools"
CLIENTS = ("codex", "claude")
SCOPE_CLIENT_PLAN = "client-plan"
SCOPE_FULL = "full"
RUN, SKIP = "run", "skip"
SHA1 = re.compile(r"[0-9a-f]{40}")
GLOB_CHARS = "*?[{"
# 路径字面量反向索引用的"顶层"名：测试里写 ``"docs/..."``、``Path(...) / "tools" / ...`` 时的起点。
INDEX_TOPS = ("docs", "backend", "frontend", "deploy", ".github", "tools", "assets", "scripts", "openspec", "skills")
INDEX_TOP_FILES = ("Makefile", "README.md", "DEV_GUIDE.md", "Dockerfile")
# 只出现一个目录层级的引用（如单独的 ``tools``、``docs``）不计：Claude 重模块常有 ``Path(...) / "tools"`` 这类拼接。
BARE_DIRECTORY_TOPS = frozenset(INDEX_TOPS)
_PATH_REF = re.compile(
    r"(?:(?<=[\s\"'(\[=:,])|^)((?:" + "|".join(re.escape(item) for item in INDEX_TOPS) + r")/[A-Za-z0-9_./\-*{}]+|"
    + "|".join(re.escape(item) for item in INDEX_TOP_FILES) + r")")
_STRING_MODULE = re.compile(r"tools[./][A-Za-z_][A-Za-z0-9_]*(?:[./][A-Za-z0-9_]+)*")
_STRING_SCRIPT = re.compile(r"[\"'/ ]([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\.py\b")
_DYNAMIC_CALLS = frozenset({"import_module", "__import__", "spec_from_file_location", "run_path", "run_module"})


class OwnershipError(ValueError):
    """归属数据、基点或测试树不满足要求（调用方当作配置／用法错误，不降级成全集）。继承 ValueError：入口门禁的命令行把它和别的配置错误一样报成退出码 2。"""


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------


def canonical(value: Any) -> bytes:
    """与执行器、记录库同一种规范化 JSON（键排序、紧凑分隔、不转义中文）。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def seal(payload: Mapping[str, Any], key: str = "selection_sha256") -> dict[str, Any]:
    """给载荷加自摘要（对去掉 ``key`` 的整份内容求摘要）。"""

    body = {name: value for name, value in payload.items() if name != key}
    return {**body, key: sha256_json(body)}


def seal_ok(payload: Mapping[str, Any], key: str = "selection_sha256") -> bool:
    body = {name: value for name, value in payload.items() if name != key}
    return payload.get(key) == sha256_json(body)


def other_client(client: str) -> str:
    if client not in CLIENTS:
        raise OwnershipError(f"不认识的客户端：{client!r}")
    return "claude" if client == "codex" else "codex"


def name_owner(rel: str) -> str:
    """按路径与文件名判归属：控制面目录与名字含 claude 的归 claude，名字含 codex 的归 codex，其余 shared。"""

    name = PurePosixPath(rel).name
    if "official_client_control" in rel or "claude" in name:
        return "claude"
    if "codex" in name:
        return "codex"
    return "shared"


def is_fixture(rel: str) -> bool:
    """测试夹具：直接放在测试目录下、名字不以 test_ 开头的 Python 文件（含包的 ``__init__.py``）。"""

    path = PurePosixPath(rel)
    return str(path.parent) == TESTS_DIR and not path.name.startswith("test_") and path.suffix == ".py"


def is_test_module(rel: str) -> bool:
    path = PurePosixPath(rel)
    return str(path.parent) == TESTS_DIR and path.name.startswith("test_") and path.suffix == ".py"


def is_managed(rel: str) -> bool:
    return any(rel == root or rel.startswith(root + "/") for root in MANAGED_ROOTS)


def in_managed_non_test(rel: str) -> bool:
    return is_managed(rel) and "/tests/" not in rel


# ---------------------------------------------------------------------------
# 导入图（从工作树文件现建，只读）
# ---------------------------------------------------------------------------


def _chain_parts(node: ast.AST) -> list[str | None]:
    """``Path(...) / "a" / "b"`` 这类除法链里的常量片段；遇到非常量记 None。"""

    parts: list[str | None] = []

    def walk(item: ast.AST) -> None:
        if isinstance(item, ast.BinOp) and isinstance(item.op, ast.Div):
            walk(item.left)
            walk(item.right)
        elif isinstance(item, ast.Constant) and isinstance(item.value, str):
            parts.append(item.value)
        else:
            parts.append(None)

    walk(node)
    return parts


def path_refs_of(tree: ast.AST) -> set[str]:
    """一个模块里写死的仓库路径引用（字符串里的路径与除法链拼出的路径）。"""

    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            for match in _PATH_REF.finditer(node.value):
                refs.add(match.group(1).rstrip("/"))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = _chain_parts(node)
            for index, part in enumerate(parts):
                if part is None or not (part in INDEX_TOPS or part in INDEX_TOP_FILES or part.split("/")[0] in INDEX_TOPS):
                    continue
                segments: list[str] = []
                for follow in parts[index:]:
                    if follow is None:
                        break
                    segments.append(follow.strip("/"))
                if segments:
                    refs.add("/".join(segments))
    return refs


class Graph:
    """``tools/`` 下全部 Python 文件的导入图。

    边：``import``／``from … import …``（含函数内导入）、字符串里写全的仓库内模块路径（子进程 ``-m``、脚本路径）、
    字符串里的裸脚本名（stem 唯一且含下划线才算）。另记每个文件的路径字面量引用与"动态加载""解析失败"标记。
    """

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        base = self.root / GRAPH_ROOT
        files: list[str] = []
        if base.is_dir():
            for path in sorted(base.rglob("*.py")):
                if "__pycache__" in path.parts or path.is_symlink() or not path.is_file():
                    continue
                files.append(path.relative_to(self.root).as_posix())
        self.files = files
        self.text: dict[str, str] = {rel: (self.root / rel).read_text(encoding="utf-8", errors="replace") for rel in files}
        self.dotted: dict[str, str] = {}
        for rel in files:
            parts = list(PurePosixPath(rel).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            self.dotted[".".join(parts)] = rel
        self.imports: dict[str, set[str]] = {}
        self.refs: dict[str, set[str]] = {}
        self.dynamic: set[str] = set()
        self.unparsed: set[str] = set()
        for rel in files:
            self._analyze(rel)
        stems: dict[str, list[str]] = {}
        for rel in files:
            stems.setdefault(PurePosixPath(rel).stem, []).append(rel)
        for rel in files:
            for match in _STRING_SCRIPT.finditer(self.text[rel]):
                candidates = stems.get(match.group(1), [])
                if len(candidates) == 1 and candidates[0] != rel:
                    self.imports[rel].add(candidates[0])

    def resolve(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        while parts:
            hit = self.dotted.get(".".join(parts))
            if hit is not None:
                return hit
            parts = parts[:-1]
        return None

    def _analyze(self, rel: str) -> None:
        imports: set[str] = set()
        self.imports[rel] = imports
        self.refs[rel] = set()
        try:
            tree = ast.parse(self.text[rel])
        except (SyntaxError, ValueError, RecursionError):
            self.unparsed.add(rel)
            return
        package = ".".join(PurePosixPath(rel).with_suffix("").parts[:-1])
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    hit = self.resolve(alias.name)
                    if hit is not None:
                        imports.add(hit)
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    up = package.split(".") if package else []
                    up = up[: len(up) - (node.level - 1)] if node.level > 1 else up
                    base = ".".join(up + ([base] if base else []))
                hit = self.resolve(base) if base else None
                if hit is not None:
                    imports.add(hit)
                for alias in node.names:
                    sub = self.resolve(f"{base}.{alias.name}") if base else None
                    if sub is not None:
                        imports.add(sub)
            elif isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
                if name in _DYNAMIC_CALLS:
                    self.dynamic.add(rel)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for match in _STRING_MODULE.finditer(node.value):
                    dotted = re.sub(r"\.py$", "", match.group(0).replace("/", "."))
                    hit = self.resolve(dotted)
                    if hit is not None:
                        imports.add(hit)
        imports.discard(rel)
        self.refs[rel] = path_refs_of(tree)

    # -- 可达集 -------------------------------------------------------------

    def fixture_closure(self, module_rel: str) -> set[str]:
        """测试模块自身加它经夹具之间的导入链能到达的全部夹具（用于路径字面量、文本提及这类"测试自己写了什么"的口径）。"""

        seen = {module_rel}
        stack = [module_rel]
        while stack:
            current = stack.pop()
            for target in self.imports.get(current, ()):
                if is_fixture(target) and target not in seen:
                    seen.add(target)
                    stack.append(target)
        return seen

    def direct_reach(self, module_rel: str) -> set[str]:
        """测试模块直接导入的文件，加上它直接导入的每个夹具再直接导入的文件（只穿一层夹具，设计的"1 跳"口径）。"""

        direct = set(self.imports.get(module_rel, set()))
        out = set(direct)
        for target in direct:
            if is_fixture(target):
                out |= self.imports.get(target, set())
        return out

    def chain_reach(self, module_rel: str) -> set[str]:
        """沿夹具与 Claude 面文件的导入链能到达的文件（Claude 面文件改动时用，比一层夹具更宽，宁可多拉）。"""

        seen = {module_rel}
        stack = [module_rel]
        out: set[str] = {module_rel}
        while stack:
            current = stack.pop()
            for target in self.imports.get(current, ()):
                out.add(target)
                if (is_fixture(target) or name_owner(target) == "claude") and target not in seen:
                    seen.add(target)
                    stack.append(target)
        return out

    def path_index(self, module_rel: str) -> set[str]:
        out: set[str] = set()
        for member in self.fixture_closure(module_rel):
            out |= self.refs.get(member, set())
        return out


# ---------------------------------------------------------------------------
# 数据文件
# ---------------------------------------------------------------------------

_DATA_KEYS = frozenset({
    "schema_version", "description", "calibration", "cheap_core_seconds", "enabled_clients", "infra_prefixes", "overrides", "pinned",
    "reviewed_hermetic", "p3_literals", "coupled", "recognized_areas", "generic_refs", "generic_names", "hubs", "shared_kernel", "modules",
})
_MODULE_KEYS = frozenset({"owner", "rule", "core_seconds", "codex_plan", "claude_plan"})


def data_path(tree: Path) -> Path:
    return Path(tree) / DATA_PATH


def load_data(tree: Path) -> dict[str, Any]:
    """读并校验归属数据；格式不对一律报错（选择器不在坏数据上降级）。"""

    path = data_path(tree)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise OwnershipError(f"读不到归属数据 {DATA_PATH}：{error}") from error
    return validate_data(payload)


def validate_data(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA:
        raise OwnershipError("归属数据格式版本不对")
    if set(payload) != _DATA_KEYS:
        raise OwnershipError(f"归属数据的键不对：多 {sorted(set(payload) - _DATA_KEYS)}，缺 {sorted(_DATA_KEYS - set(payload))}")
    cheap = payload["cheap_core_seconds"]
    if isinstance(cheap, bool) or not isinstance(cheap, (int, float)) or cheap < 0:
        raise OwnershipError("廉价阈值必须是非负数")
    if not isinstance(payload["enabled_clients"], list) or not set(payload["enabled_clients"]) <= set(CLIENTS):
        raise OwnershipError("enabled_clients 只能是 codex／claude 的子集")
    modules = payload["modules"]
    if not isinstance(modules, dict) or not modules:
        raise OwnershipError("modules 必须是非空对象")
    for name, row in modules.items():
        if not isinstance(row, dict) or set(row) != _MODULE_KEYS:
            raise OwnershipError(f"模块 {name} 的字段不对")
        if row["owner"] not in ("codex", "claude", "shared"):
            raise OwnershipError(f"模块 {name} 的归属不合法：{row['owner']!r}")
        seconds = row["core_seconds"]
        if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0):
            raise OwnershipError(f"模块 {name} 的核·秒不合法")
        for key in ("codex_plan", "claude_plan"):
            if row[key] not in (RUN, SKIP):
                raise OwnershipError(f"模块 {name} 的 {key} 只能是 run／skip")
    for key in ("overrides", "pinned", "reviewed_hermetic"):
        if not isinstance(payload[key], dict) or not all(isinstance(reason, (str, dict)) and reason for reason in payload[key].values()):
            raise OwnershipError(f"{key} 必须是 模块名→理由 的对象，理由不得为空")
    for key in ("infra_prefixes", "p3_literals", "recognized_areas", "generic_refs", "generic_names", "hubs", "shared_kernel"):
        if not isinstance(payload[key], list) or not all(isinstance(item, str) and item for item in payload[key]):
            raise OwnershipError(f"{key} 必须是非空字符串列表")
    if not isinstance(payload["coupled"], list):
        raise OwnershipError("coupled 必须是列表")
    for entry in payload["coupled"]:
        if (not isinstance(entry, dict) or not {"patterns", "modules", "reason"} <= set(entry)
                or not set(entry) <= {"patterns", "modules", "reason", "ignore_assignments"}
                or not entry["patterns"] or not entry["modules"] or not str(entry["reason"]).strip()):
            raise OwnershipError(f"联动表条目不合法：{entry!r}")
    return payload


def plan_state(row: Mapping[str, Any], client: str, *, cheap: float, pinned: bool) -> str:
    """一个模块在指定客户端计划里是跑还是跳：只有"归属对方客户端、核·秒已标定且不低于廉价阈值、没被钉住"才跳过。"""

    seconds = row["core_seconds"]
    if pinned or row["owner"] != other_client(client) or seconds is None or seconds < cheap:
        return RUN
    return SKIP


# ---------------------------------------------------------------------------
# 推导（derive）
# ---------------------------------------------------------------------------


def discover_modules(tree: Path) -> list[str]:
    return sorted(path.stem for path in (Path(tree) / TESTS_DIR).glob("test_*.py") if path.is_file())


def mentions(text: str) -> tuple[int, int]:
    return len(re.findall("claude", text, re.I)), len(re.findall("codex", text, re.I))


def managed_imports(graph: Graph, module_rel: str) -> set[str]:
    """测试模块及其夹具链直接导入的受管树文件（不含测试目录里的文件）。"""

    closure = graph.fixture_closure(module_rel)
    out: set[str] = set()
    for member in closure:
        out |= {target for target in graph.imports.get(member, set()) if in_managed_non_test(target)}
    return out


def derive_owner(module: str, text: str, affinity: Mapping[str, int], data: Mapping[str, Any]) -> tuple[str, str]:
    """一个测试模块的归属与命中规则（顺序：人工覆盖、名字含 claude、名字含 codex、基础设施前缀、单边证据、默认共用）。"""

    claude_mentions, codex_mentions = mentions(text)
    override = data["overrides"].get(module)
    if override is not None:
        owner = override["owner"] if isinstance(override, dict) else None
        if owner not in ("codex", "claude", "shared"):
            raise OwnershipError(f"覆盖表 {module} 缺少合法的 owner")
        return owner, "override"
    if "claude" in module:
        return "claude", "name:claude"
    if "codex" in module:
        return "codex", "name:codex"
    if module.startswith(tuple(data["infra_prefixes"])):
        return "shared", "infra-prefix"
    if claude_mentions == 0 and codex_mentions >= 5 and affinity["claude"] == 0:
        return "codex", "evidence:codex-only"
    return "shared", "default-shared"


def derive_affinity(graph: Graph, module: str) -> dict[str, int]:
    rel = f"{TESTS_DIR}/{module}.py"
    counts = {"claude": 0, "codex": 0, "shared": 0}
    for target in managed_imports(graph, rel):
        counts[name_owner(target)] += 1
    return counts


def derive_modules(tree: Path, data: Mapping[str, Any], graph: Graph | None = None) -> dict[str, dict[str, str]]:
    graph = graph or Graph(tree)
    out: dict[str, dict[str, str]] = {}
    for module in discover_modules(tree):
        rel = f"{TESTS_DIR}/{module}.py"
        owner, rule = derive_owner(module, graph.text.get(rel, ""), derive_affinity(graph, module), data)
        out[module] = {"owner": owner, "rule": rule}
    return out


def derive_kernel(graph: Graph) -> tuple[list[str], list[str]]:
    """共用内核与枢纽（均只看受管树里的非测试文件）：

    * ``shared_kernel``：名字中性（不含 claude／codex），同时被 Claude 面文件和 Codex 面文件直接导入；
    * ``hubs``：名字含 codex（不含 claude），却被 Claude 面文件直接导入——Codex 升级几乎必改，Claude 重集成测试又在库式复用。
    """

    claude_importers: dict[str, int] = {}
    codex_importers: dict[str, int] = {}
    for importer in graph.files:
        owner = name_owner(importer)
        if owner == "shared":
            continue
        for target in graph.imports.get(importer, ()):
            if not in_managed_non_test(target) or target == importer:
                continue
            bucket = claude_importers if owner == "claude" else codex_importers
            bucket[target] = bucket.get(target, 0) + 1
    kernel = sorted(target for target in claude_importers
                    if name_owner(target) == "shared" and codex_importers.get(target) and not target.endswith("/__init__.py"))
    hubs = sorted(target for target in claude_importers if name_owner(target) == "codex")
    return kernel, hubs


def module_core_seconds(calibration: Mapping[str, float], module: str) -> float | None:
    value = calibration.get(module)
    return None if value is None else round(float(value), 1)


def materialize(tree: Path, base_data: Mapping[str, Any], calibration: Mapping[str, float], *, graph: Graph | None = None) -> dict[str, Any]:
    """按现推结果和核·秒标定生成数据文件的全部派生部分（模块表、枢纽、共用内核、计划状态）；人工登记部分原样带过来。"""

    graph = graph or Graph(tree)
    data = {key: base_data[key] for key in _DATA_KEYS if key in base_data}
    derived = derive_modules(tree, base_data, graph)
    cheap = base_data["cheap_core_seconds"]
    modules: dict[str, Any] = {}
    for module, row in derived.items():
        seconds = module_core_seconds(calibration, module)
        item = {"owner": row["owner"], "rule": row["rule"], "core_seconds": seconds}
        pinned = module in base_data["pinned"]
        item["codex_plan"] = plan_state(item, "codex", cheap=cheap, pinned=pinned)
        item["claude_plan"] = plan_state(item, "claude", cheap=cheap, pinned=pinned)
        modules[module] = item
    kernel, hubs = derive_kernel(graph)
    data["modules"] = modules
    data["shared_kernel"] = kernel
    data["hubs"] = hubs
    return data


# ---------------------------------------------------------------------------
# 校验（check）
# ---------------------------------------------------------------------------


def check(tree: Path, *, graph: Graph | None = None) -> list[str]:
    """数据文件与测试树的一致性与守卫；返回问题列表，空＝通过。"""

    tree = Path(tree)
    problems: list[str] = []
    try:
        data = load_data(tree)
    except OwnershipError as error:
        return [str(error)]
    graph = graph or Graph(tree)
    found = discover_modules(tree)
    table = data["modules"]
    missing = sorted(set(found) - set(table))
    stale = sorted(set(table) - set(found))
    if missing:
        problems.append(f"测试模块没有登记归属（新模块要重新物化归属表）：{missing[:8]}")
    if stale:
        problems.append(f"归属表里有已经不存在的测试模块：{stale[:8]}")
    derived = derive_modules(tree, data, graph)
    for module, row in derived.items():
        recorded = table.get(module)
        if recorded is not None and (recorded["owner"], recorded["rule"]) != (row["owner"], row["rule"]):
            problems.append(f"{module}：数据文件写的是 {recorded['owner']}（{recorded['rule']}），现推是 {row['owner']}（{row['rule']}）")
    cheap = data["cheap_core_seconds"]
    for module, row in table.items():
        for client in CLIENTS:
            want = plan_state(row, client, cheap=cheap, pinned=module in data["pinned"])
            if row[f"{client}_plan"] != want:
                problems.append(f"{module}：{client}_plan 写的是 {row[f'{client}_plan']}，按归属与阈值应为 {want}")
    kernel, hubs = derive_kernel(graph)
    if sorted(data["shared_kernel"]) != kernel:
        problems.append(f"共用内核清单与现推不一致：数据多 {sorted(set(data['shared_kernel']) - set(kernel))[:5]}，缺 {sorted(set(kernel) - set(data['shared_kernel']))[:5]}")
    if sorted(data["hubs"]) != hubs:
        problems.append(f"枢纽清单与现推不一致：数据多 {sorted(set(data['hubs']) - set(hubs))[:5]}，缺 {sorted(set(hubs) - set(data['hubs']))[:5]}")
    for module in data["pinned"]:
        if module not in found:
            problems.append(f"钉住表里的模块不存在：{module}")
    for module in data["reviewed_hermetic"]:
        if module not in found:
            problems.append(f"已复核隔离表里的模块不存在：{module}")
    for module in data["overrides"]:
        if module not in found:
            problems.append(f"覆盖表里的模块不存在：{module}")
    coupled_modules: set[str] = set()
    for entry in data["coupled"]:
        for module in entry["modules"]:
            coupled_modules.add(module)
            if module not in found:
                problems.append(f"联动表里的模块不存在：{module}")
            elif table.get(module, {}).get("owner") != "claude":
                problems.append(f"联动表里的模块必须归属 Claude（它们才是被剔除的对象）：{module}")
    problems.extend(hermetic_problems(tree, data, graph))
    return problems


def hermetic_problems(tree: Path, data: Mapping[str, Any], graph: Graph) -> list[str]:
    """ARM64 依赖守卫：已启用客户端的计划里可能被剔除的模块（含夹具）里不得出现访问真实 ARM64 安装或数据根的路径字面量，
    除非在已复核隔离表里登记了理由（逐条人工判过只是配置字面量、判断存在性后跳过或写在临时目录里）。"""

    problems: list[str] = []
    cheap = data["cheap_core_seconds"]
    literals = data["p3_literals"]
    for module, row in data["modules"].items():
        if module in data["reviewed_hermetic"]:
            continue
        if all(plan_state(row, client, cheap=cheap, pinned=module in data["pinned"]) == RUN for client in data["enabled_clients"]):
            continue
        rel = f"{TESTS_DIR}/{module}.py"
        if rel not in graph.text:
            continue
        for member in sorted(graph.fixture_closure(rel)):
            hit = [literal for literal in literals if literal in graph.text.get(member, "")]
            if hit:
                problems.append(f"{module}（{member}）含访问真实 ARM64 环境的路径字面量 {hit}：判为钉住（pinned）或在已复核隔离表登记理由")
    return problems


# ---------------------------------------------------------------------------
# git 与改动
# ---------------------------------------------------------------------------


def _git(tree: Path, *args: str, check_rc: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", "-C", str(tree), "-c", "core.quotepath=off", *args], capture_output=True, check=False)
    if check_rc and result.returncode != 0:
        raise OwnershipError(f"git {' '.join(args[:2])} 失败：{result.stderr.decode('utf-8', 'replace').strip()[:240]}")
    return result


def resolve_commits(tree: Path, base: str, head: str | None) -> tuple[str, str]:
    """校验并返回（基点, 候选提交）：都是 40 位完整提交，候选提交就是测试树当前 HEAD 且树干净，基点是候选提交的祖先。"""

    tree = Path(tree)
    if not SHA1.fullmatch(base):
        raise OwnershipError(f"基点必须是 40 位小写提交：{base!r}")
    actual = _git(tree, "rev-parse", "--verify", "HEAD^{commit}").stdout.decode().strip()
    head = head or actual
    if not SHA1.fullmatch(head):
        raise OwnershipError(f"候选提交必须是 40 位小写提交：{head!r}")
    if head != actual:
        raise OwnershipError(f"候选提交 {head} 不是测试树当前 HEAD {actual}")
    if _git(tree, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        raise OwnershipError("测试树不干净，不能按改动选择")
    resolved = _git(tree, "rev-parse", "--verify", f"{base}^{{commit}}", check_rc=False)
    if resolved.returncode != 0 or resolved.stdout.decode().strip() != base:
        raise OwnershipError(f"基点 {base} 不在测试树的历史里")
    if _git(tree, "merge-base", "--is-ancestor", base, head, check_rc=False).returncode != 0:
        raise OwnershipError(f"基点 {base} 不是候选提交 {head} 的祖先")
    return base, head


def changed_files(tree: Path, base: str, head: str) -> list[tuple[str, str]]:
    """基点到候选提交的改动（状态, 路径），重命名拆成删除加新增；按路径排序。"""

    raw = _git(tree, "diff", "--name-status", "-z", "--no-renames", base, head).stdout
    tokens = raw.split(b"\0")
    if tokens and tokens[-1] == b"":
        tokens.pop()
    if len(tokens) % 2:
        raise OwnershipError("git diff --name-status -z 输出格式不对")
    pairs = [(tokens[index].decode("utf-8", "replace")[:1], tokens[index + 1].decode("utf-8")) for index in range(0, len(tokens), 2)]
    return sorted(pairs, key=lambda item: (item[1], item[0]))


RELEASE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9]+)?")


def latest_release_base(tree: Path) -> tuple[str, str]:
    """基点的缺省来源（设计：最近一次发版 tag 的提交）：测试树 HEAD 的祖先里最近的发版 tag（形如 ``v0.2.18``、``v0.2.18-1``），
    返回（40 位提交, tag 名）。测试树里没有 tag、或最近的 tag 不是发版格式，抛 ``OwnershipError``（调用方在 auto 模式下回退全集）。

    只认发版 tag：发版前 GitHub CI 全集是绿的、收尾在 ARM64 过了全量门禁，所以"这个提交上 Claude 单元已知是绿的"成立；
    不拿 main 头当基点（它可能还没发版，没过收尾全集）。"""

    tree = Path(tree)
    described = _git(tree, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*", "HEAD", check_rc=False)
    name = described.stdout.decode("utf-8", "replace").strip()
    if described.returncode != 0 or not RELEASE_TAG.fullmatch(name):
        raise OwnershipError("测试树里找不到最近的发版 tag（describe --tags --match 'v[0-9]*'）：补齐 tag 或显式给 --select-base")
    commit = _git(tree, "rev-parse", "--verify", f"{name}^{{commit}}").stdout.decode().strip()
    if not SHA1.fullmatch(commit):
        raise OwnershipError(f"发版 tag {name} 解析不出 40 位提交")
    return commit, name


def _git_show(tree: Path, revision: str, path: str) -> str | None:
    result = _git(tree, "show", f"{revision}:{path}", check_rc=False)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


def _normalized_python(text: str | None, ignore: Sequence[str]) -> str | None:
    """把指定名字的赋值右值抹掉后的 AST 转储；解析失败返回 None（调用方当作不是联动）。"""

    if text is None:
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None
    ignored = set(ignore)
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id in ignored for target in node.targets):
            node.value = ast.Constant(value=None)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id in ignored and node.value is not None:
            node.value = ast.Constant(value=None)
    return ast.dump(tree, include_attributes=False)


# ---------------------------------------------------------------------------
# 选择（select）
# ---------------------------------------------------------------------------


def recognized(path: str, data: Mapping[str, Any]) -> bool:
    """改动路径在已登记的目录范围里（顶层目录／文件与 tools 的子目录）；新顶层目录、新的 tools 子目录不认识，失败关闭。"""

    parts = PurePosixPath(path).parts
    if len(parts) >= 2 and parts[0] == "tools" and len(parts) == 2:
        return True  # tools 下的单个脚本
    for area in data["recognized_areas"]:
        if area.endswith("/"):
            if path.startswith(area):
                return True
        elif path == area:
            return True
    return False


def _ref_matches(ref: str, path: str) -> bool:
    cut = min((ref.find(char) for char in GLOB_CHARS if char in ref), default=-1)
    if cut >= 0:
        if fnmatch.fnmatchcase(path, ref):
            return True
        prefix = ref[:cut].rsplit("/", 1)[0] if "/" in ref[:cut] else ""
        return bool(prefix) and (path == prefix or path.startswith(prefix + "/"))
    return path == ref or path.startswith(ref + "/")


def _usable_refs(refs: Iterable[str], data: Mapping[str, Any]) -> set[str]:
    generic = set(data["generic_refs"])
    out = set()
    for ref in refs:
        if not ref or ref in generic or ref in BARE_DIRECTORY_TOPS:
            continue
        out.add(ref)
    return out


class Selector:
    """一次选择的现场：测试树、归属数据、导入图和惰性缓存。"""

    def __init__(self, tree: Path, data: Mapping[str, Any], client: str, graph: Graph | None = None) -> None:
        self.tree = Path(tree)
        self.data = data
        self.client = client
        self.graph = graph or Graph(self.tree)
        self.found = discover_modules(self.tree)
        table = data["modules"]
        cheap = data["cheap_core_seconds"]
        self.heavy: list[str] = sorted(
            module for module in self.found
            if module in table and plan_state(table[module], client, cheap=cheap, pinned=module in data["pinned"]) == SKIP)
        self.hubs = set(data["hubs"])
        self._direct: dict[str, set[str]] = {}
        self._chain: dict[str, set[str]] = {}
        self._index: dict[str, set[str]] = {}

    def rel(self, module: str) -> str:
        return f"{TESTS_DIR}/{module}.py"

    def direct(self, module: str) -> set[str]:
        if module not in self._direct:
            self._direct[module] = self.graph.direct_reach(self.rel(module))
        return self._direct[module]

    def chain(self, module: str) -> set[str]:
        if module not in self._chain:
            self._chain[module] = self.graph.chain_reach(self.rel(module))
        return self._chain[module]

    def index(self, module: str) -> set[str]:
        if module not in self._index:
            self._index[module] = _usable_refs(self.graph.path_index(self.rel(module)), self.data)
        return self._index[module]

    def importers_of(self, target: str, *, chain: bool | None = None) -> list[str]:
        """会因 ``target`` 这个 Python 文件改动而被拉入的重模块：自己就是它，或（一层夹具内）直接导入它。

        ``chain`` 缺省按文件归属：Claude 面文件的改动沿夹具与 Claude 面文件的导入链传递（宁可多拉），其余文件只看直接导入。"""

        use_chain = name_owner(target) == "claude" if chain is None else chain
        out = []
        for module in self.heavy:
            if self.rel(module) == target or target in (self.chain(module) if use_chain else self.direct(module)):
                out.append(module)
        return out

    def mentioning(self, reader: str) -> list[str]:
        """测试目录里的 ``reader`` 文件（测试模块或夹具）提到了某个文件时，会被拉入的重模块：模块自己或它的夹具闭包含这个文件。"""

        return [module for module in self.heavy if reader in self.graph.fixture_closure(self.rel(module))]


def select(tree: Path, *, client: str, base: str, head: str | None = None, data: Mapping[str, Any] | None = None,
           include_claude: bool = False, graph: Graph | None = None) -> dict[str, Any]:
    """算出门禁清单里的 ``selection`` 块（见模块说明里的并入规则）。出错（数据坏、基点不对、树不干净）一律抛 ``OwnershipError``。"""

    tree = Path(tree)
    data = validate_data(dict(data)) if data is not None else load_data(tree)
    if client not in CLIENTS:
        raise OwnershipError(f"不认识的客户端：{client!r}")
    if client not in data["enabled_clients"]:
        raise OwnershipError(f"本版只启用 {data['enabled_clients']} 的计划，不支持 --client {client}")
    base, head = resolve_commits(tree, base, head)
    changes = changed_files(tree, base, head)
    selector = Selector(tree, data, client, graph)
    found = selector.found
    table = data["modules"]
    unclassified = sorted(module for module in found if module not in table)
    present = set(found)
    stale = sorted(module for module in table if module not in present)
    pulled: dict[str, set[str]] = {}
    advisories: dict[str, list[str]] = {}
    fail_closed: list[dict[str, Any]] = []
    fallback: list[str] = []

    def pull(modules: Iterable[str], because: str) -> None:
        for module in modules:
            pulled.setdefault(module, set()).add(because)

    def close(reason: str, path: str) -> None:
        fail_closed.append({"reason": reason, "file": path})
        pull(selector.heavy, f"失败关闭：{reason}：{path}")

    def advise(hub: str, modules: Iterable[str]) -> None:
        advisories.setdefault(hub, [])
        advisories[hub] = sorted(set(advisories[hub]) | set(modules))

    heavy = set(selector.heavy)
    for status, path in changes:
        if path in SELF_PATHS:
            fallback.append(f"归属表或选择器本身在改动里：{path}")
    if include_claude:
        pull(selector.heavy, "手动开关 --include-claude")
    elif not fallback:
        for status, path in changes:
            _handle_change(selector, data, tree, base, head, status, path, heavy, pull, close, advise)
    state = {module: RUN for module in found}
    if not fallback:
        for module in selector.heavy:
            if module not in pulled:
                state[module] = SKIP
    scope = SCOPE_FULL if fallback else SCOPE_CLIENT_PLAN
    seconds = {module: table[module]["core_seconds"] or 0.0 for module in selector.heavy}
    advisory_rows = []
    for hub, modules in sorted(advisories.items()):
        advisory_rows.append({"file": hub, f"{other_client(client)}_modules": len(modules),
                              "core_seconds": round(sum(seconds.get(module, 0.0) for module in modules), 1), "modules": modules})
    selection = {
        "schema_version": SELECTION_SCHEMA,
        "client": client,
        "scope": scope,
        "include_claude": bool(include_claude),
        "ownership_sha256": sha256_file(data_path(tree)),
        "selector_sha256": sha256_file(Path(__file__)),
        "base_commit": base,
        "head_commit": head,
        "changed_files_count": len(changes),
        "changed_files_sha256": sha256_json([[status, path] for status, path in changes]),
        "modules": dict(sorted(state.items())),
        "pulled": [{"module": module, "because": sorted(reasons)} for module, reasons in sorted(pulled.items())],
        "advisories": advisory_rows,
        "unclassified": unclassified,
        "stale_modules": stale,
        "fail_closed": sorted(fail_closed, key=lambda item: (item["file"], item["reason"])),
        "fallback_reasons": sorted(fallback),
    }
    return seal(selection)


def _handle_change(selector: Selector, data: Mapping[str, Any], tree: Path, base: str, head: str, status: str, path: str,
                   heavy: set[str], pull: Any, close: Any, advise: Any) -> None:
    if not recognized(path, data):
        close("改动路径不在已登记的目录范围", path)
        return
    # 联动文件：只并入直接验证它们的模块（Python 联动文件只在抹掉登记的赋值后其余完全不变时才算联动）。
    for entry in data["coupled"]:
        if not any(fnmatch.fnmatchcase(path, pattern) for pattern in entry["patterns"]):
            continue
        ignore = entry.get("ignore_assignments") or []
        if path.endswith(".py") and ignore:
            before = _normalized_python(_git_show(tree, base, path), ignore)
            after = _normalized_python(_git_show(tree, head, path), ignore)
            if status != "M" or before is None or after is None or before != after:
                continue
        pull([module for module in entry["modules"] if module in heavy], path)
        return
    if path.endswith(".py") and path.startswith(GRAPH_ROOT + "/"):
        _handle_python(selector, status, path, pull, close, advise)
        return
    _handle_other(selector, data, path, pull, close, advise)


def _handle_python(selector: Selector, status: str, path: str, pull: Any, close: Any, advise: Any) -> None:
    graph = selector.graph
    if status == "D" or path not in graph.imports:
        # 已删除的 Python 文件：文本里还提到它的模块名的文件就是它原来的使用者（留着的导入现在会失败），
        # 按"这些使用者被改动"处理：测试目录里的使用者看谁的夹具闭包含它，库文件看谁导入它。
        token = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(PurePosixPath(path).stem) + r"(?![A-Za-z0-9_])")
        users = [member for member in graph.files if member != path and token.search(graph.text[member])]
        hit: set[str] = set()
        for user in users:
            hit |= set(selector.mentioning(user) if user.startswith(TESTS_DIR + "/") else selector.importers_of(user))
        if path in selector.hubs:
            advise(path, sorted(hit))
        else:
            pull(sorted(hit), path)
        return
    if path in graph.unparsed:
        close("Python 文件解析失败，无法判断谁依赖它", path)
        return
    modules = selector.importers_of(path)
    if path in selector.hubs:
        advise(path, modules)
    else:
        pull(modules, path)


_VERSIONED = re.compile(r"_[0-9]+(?:_[0-9]+)+")


def reader_needles(name: str) -> list[str]:
    """在 Python 源码里找一个数据文件读取者时用的字面量：文件名本身；带版本号的文件名（按版本动态拼出来读）再加版本号之前的前缀。"""

    needles = [name]
    match = _VERSIONED.search(name)
    if match is not None:
        prefix = name[: match.start() + 1]
        if len(prefix) >= 8:
            needles.append(prefix)
    return needles


def _handle_other(selector: Selector, data: Mapping[str, Any], path: str, pull: Any, close: Any, advise: Any) -> None:
    graph = selector.graph
    if not is_managed(path):
        # 受管树之外（后端、前端、文档、部署、工作流、根目录文件、tools 下的其它脚本）：路径字面量反向索引，
        # 重模块（含夹具）里写死的仓库路径按前缀匹配改动。
        pull([module for module in selector.heavy if any(_ref_matches(ref, path) for ref in selector.index(module))], path)
        return
    name = PurePosixPath(path).name
    if name in set(data["generic_names"]) or name.endswith((".md", ".txt")):
        return
    # 受管树内的数据文件、脚本：找读取者（Python 源码里提到它的文件名）。读取者是枢纽只提示；是测试目录文件看谁的闭包含它；
    # 是库文件按它被谁直接导入（Claude 命名的数据文件沿 Claude 面链）。
    needles = reader_needles(name)
    readers = [member for member in graph.files if any(needle in graph.text[member] for needle in needles)]
    chain = name_owner(path) == "claude"
    for reader in readers:
        if reader in selector.hubs:
            advise(path, selector.importers_of(reader, chain=False))
        elif reader.startswith(TESTS_DIR + "/"):
            pull(selector.mentioning(reader), path)
        else:
            pull(selector.importers_of(reader, chain=chain), path)
    if not readers and name_owner(path) != "codex" and not name.startswith("candidate_"):
        close("受管树内数据文件找不到任何读取者，无法判断谁依赖它", path)


# ---------------------------------------------------------------------------
# 报告
# ---------------------------------------------------------------------------


def skip_reason(module: str, row: Mapping[str, Any], client: str, *, cheap: float) -> str:
    seconds = row["core_seconds"]
    if row["owner"] != other_client(client):
        return f"归属 {row['owner']}，{client} 计划内"
    if seconds is None:
        return "归属对方客户端但核·秒未标定：保守照跑"
    if seconds < cheap:
        return f"廉价保留（模块合计 {seconds:.0f} 核·秒 < {cheap:.0f}）：剔除几乎不省时，保留更稳"
    return (f"归属 {row['owner']}（{row['rule']}）且模块合计 {seconds:.0f} 核·秒；没有 ARM64 真实环境依赖（守卫已核）；"
            "改动并入规则兜底")


def report(tree: Path, client: str) -> list[dict[str, Any]]:
    """指定客户端计划与全集的差集清单：每个模块一行，剔除的写明理由。"""

    data = load_data(tree)
    cheap = data["cheap_core_seconds"]
    rows = []
    for module in sorted(data["modules"]):
        row = data["modules"][module]
        state = row[f"{client}_plan"]
        rows.append({"module": module, "owner": row["owner"], "rule": row["rule"], "core_seconds": row["core_seconds"],
                     "plan": state, "reason": skip_reason(module, row, client, cheap=cheap)})
    return rows


def _unit_module(unit_id: str) -> str:
    return re.split(r"[#!]", unit_id)[0].removesuffix("+pkg")


def diff_report(full_manifest: Mapping[str, Any], plan_manifest: Mapping[str, Any]) -> dict[str, Any]:
    """两次运行清单（全集与计划）的差集：被剔除的单元、各自的理由、单元规格摘要在交集上是否逐个相同。"""

    full = {unit["unit_id"]: unit for unit in full_manifest.get("units", [])}
    plan = {unit["unit_id"]: unit for unit in plan_manifest.get("units", [])}
    selection = plan_manifest.get("client_selection") or plan_manifest.get("selection") or {}
    modules = selection.get("modules") or {}
    dropped = sorted(set(full) - set(plan))
    added = sorted(set(plan) - set(full))
    rows = []
    for unit_id in dropped:
        module = _unit_module(unit_id)
        rows.append({"unit_id": unit_id, "module": module, "selection_state": modules.get(module),
                     "reason": "选择器标 skip：归属对方客户端、核·秒不低于阈值，且没有被改动拉入" if modules.get(module) == SKIP
                     else "清单 selection 里这个模块不是 skip：差集与选择记录对不上"})
    spec_mismatch = sorted(unit_id for unit_id in set(full) & set(plan)
                           if full[unit_id].get("spec_sha256") != plan[unit_id].get("spec_sha256"))
    return {"dropped": rows, "added": added, "spec_mismatch": spec_mismatch, "common": len(set(full) & set(plan)),
            "consistent": not added and not spec_mismatch and all(row["selection_state"] == SKIP for row in rows)}


def calibration_from_csv(path: Path) -> dict[str, float]:
    """从逐单元表（列 unit_id、kind、core_seconds，如设计阶段的 units.csv）汇总模块核·秒。"""

    out: dict[str, float] = {}
    with Path(path).open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("kind") != "test":
                continue
            module = _unit_module(row["unit_id"])
            out[module] = out.get(module, 0.0) + float(row["core_seconds"])
    return out


def calibration_from_manifest(manifest_path: Path) -> dict[str, float]:
    """从一轮运行清单（unit-manifest.json）和它引用的单元记录汇总模块核·秒（记录里的 seconds×cores）。"""

    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    out: dict[str, float] = {}
    for unit in manifest.get("units", []):
        if unit.get("unit_type") != "test":
            continue
        record = json.loads(Path(unit["record_path"]).read_text(encoding="utf-8"))
        module = _unit_module(unit["unit_id"])
        out[module] = out.get(module, 0.0) + float(record["seconds"]) * float(record["cores"])
    return out


def audit_check(data: Mapping[str, Any], records: Iterable[Mapping[str, Any]], roots: Sequence[str]) -> list[str]:
    """P4 自动化：严格读集记录里，可被剔除的模块对真实数据根或驱动安装有 read／exec 访问就判红。

    ``records`` 是单元记录（带 ``unit_id`` 与 ``read_audit.host_path_modes``）；``roots`` 是真实根路径前缀。"""

    cheap = data["cheap_core_seconds"]
    problems: list[str] = []
    for record in records:
        module = _unit_module(str(record.get("unit_id", "")))
        row = data["modules"].get(module)
        if row is None or all(plan_state(row, client, cheap=cheap, pinned=module in data["pinned"]) == RUN for client in data["enabled_clients"]):
            continue
        modes =((record.get("read_audit") or {}).get("host_path_modes")) or {}
        for path, how in sorted(modes.items()):
            if any(str(path).startswith(root) for root in roots) and set(how if isinstance(how, (list, tuple, set)) else [how]) & {"read", "exec"}:
                problems.append(f"{module}：严格读集里 {path} 被 {how} 访问，不得只凭归属剔除")
    return problems


# ---------------------------------------------------------------------------
# 命令行
# ---------------------------------------------------------------------------


def _write_json(path: Path | None, payload: Any) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if path is None:
        sys.stdout.write(text)
    else:
        Path(path).write_text(text, encoding="utf-8")


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, text in (("derive", "从测试树现推每个测试模块的归属"), ("check", "数据文件与测试树一致性及守卫"),
                       ("report", "指定客户端计划与全集的差集清单（逐个带理由）")):
        item = sub.add_parser(name, help=text)
        item.add_argument("--tree", type=Path, default=Path.cwd())
        if name == "report":
            item.add_argument("--client", choices=CLIENTS, default="codex")
            item.add_argument("--format", choices=("json", "csv"), default="json")
    mat = sub.add_parser("materialize", help="按现推结果与核·秒标定重新生成数据文件")
    mat.add_argument("--tree", type=Path, default=Path.cwd())
    mat.add_argument("--calibration-csv", type=Path, help="逐单元表（unit_id、kind、core_seconds 列）")
    mat.add_argument("--calibration-run", type=Path, help="一轮运行清单 unit-manifest.json（读它引用的单元记录）")
    mat.add_argument("--output", type=Path, default=None)
    sel = sub.add_parser("select", help="算出门禁清单的 selection 块")
    sel.add_argument("--tree", type=Path, default=Path.cwd())
    sel.add_argument("--client", choices=CLIENTS, default="codex")
    sel.add_argument("--base", required=True)
    sel.add_argument("--head", default=None)
    sel.add_argument("--include-claude", action="store_true")
    sel.add_argument("--output", type=Path, default=None)
    rep = sub.add_parser("diff-report", help="两次运行清单（全集、计划）的差集核对")
    rep.add_argument("full_manifest", type=Path)
    rep.add_argument("plan_manifest", type=Path)
    cal = sub.add_parser("calibrate", help="用运行清单与单元记录重算模块核·秒（打印 JSON，不改数据文件）")
    cal.add_argument("--run", type=Path, required=True)
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    sys.dont_write_bytecode = True
    args = _parse(sys.argv[1:] if argv is None else argv)
    try:
        if args.command == "derive":
            data = load_data(args.tree)
            _write_json(None, derive_modules(args.tree, data))
            return 0
        if args.command == "check":
            problems = check(args.tree)
            for problem in problems:
                print(problem, file=sys.stderr)
            print("归属表检查通过" if not problems else f"归属表检查未通过：{len(problems)} 项")
            return 0 if not problems else 1
        if args.command == "report":
            rows = report(args.tree, args.client)
            if args.format == "csv":
                writer = csv.DictWriter(sys.stdout, fieldnames=["module", "owner", "rule", "core_seconds", "plan", "reason"])
                writer.writeheader()
                writer.writerows(rows)
            else:
                _write_json(None, rows)
            return 0
        if args.command == "materialize":
            data = load_data(args.tree)
            if args.calibration_csv is not None:
                calibration = calibration_from_csv(args.calibration_csv)
            elif args.calibration_run is not None:
                calibration = calibration_from_manifest(args.calibration_run)
            else:
                calibration = {name: row["core_seconds"] for name, row in data["modules"].items() if row["core_seconds"] is not None}
            payload = materialize(args.tree, data, calibration)
            _write_json(args.output or data_path(args.tree), payload)
            return 0
        if args.command == "select":
            _write_json(args.output, select(args.tree, client=args.client, base=args.base, head=args.head, include_claude=args.include_claude))
            return 0
        if args.command == "diff-report":
            result = diff_report(json.loads(args.full_manifest.read_text(encoding="utf-8")), json.loads(args.plan_manifest.read_text(encoding="utf-8")))
            _write_json(None, result)
            return 0 if result["consistent"] else 1
        if args.command == "calibrate":
            _write_json(None, {name: round(value, 1) for name, value in sorted(calibration_from_manifest(args.run).items())})
            return 0
    except (OwnershipError, OSError, ValueError, KeyError) as error:
        print(f"归属表：{error}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
