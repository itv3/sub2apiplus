"""受管工具身份四层策略（方案 A2）：策略加载、分层摘要、编排器函数闭包。

五个摘要：

* ``wire_producer_sha256``：决定请求字节的文件层（场景清单、模型目录、``run_*.sh``、
  驱动脚本、runtime 脚本与镜像、relay、就地脱敏器、capturelib）加上 ``codex_upgrade.py``
  中构造 argv、env、证据根、场景解释的函数闭包。变化即需重采受影响 Job，经两阶段
  wire-producer-transition 承接；映射不到 Job 则全部 Job 受影响。
* ``evidence_semantics_sha256``：解析、标签解释、inventory、assertion bundle、seal、
  finalizer、后处理脱敏，加上 ``codex_upgrade.py`` 封存与扫描函数闭包。变化不重采，
  只追加 evaluation epoch。
* ``control_sha256``：监督器、账本、恢复、收据、closeout、部署、权限收口、根因枚举表、
  策略文件本身，以及 ``codex_upgrade.py`` 整文件（编排与门禁）。变化只重跑控制门禁。
* ``files_sha256``：整树，由 ``codex_upgrade._tool_identity`` 沿用，只作溯源与三副本一致。
* ``policy_sha256``：策略文件自身；策略变化须升 ``policy_version`` 并经 A2.6 兼容收据。

函数闭包用 ``ast`` 计算：从策略登记的根函数出发收集同模块传递调用、模块级常量与
被引用的受管模块；出现 ``eval``／``exec``／``__import__``／``globals`` 等动态调用，或对
模块别名做 ``getattr`` 动态分发，即视为无法静态解析，``plan`` 与认证失败。
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

POLICY_SCHEMA = "tool-identity-policy/v2"
POLICY_FILENAME = "tool_identity_policy_v2.json"
LAYERS = ("wire_producer", "evidence_semantics", "control")
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / POLICY_FILENAME
MANAGED_IMPORT_PREFIXES = ("tools.official_client_capture.",)


class ToolIdentityPolicyError(ValueError):
    """策略文件非法或编排器闭包无法静态解析。"""


def _fingerprint(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ---------------------------------------------------------------------------
# 跨进程记忆化（E2-02）
# ---------------------------------------------------------------------------
# compute_identity_v2 与 evaluator_dependency_digests 每个进程第一次都要 ast.parse 6.3 万行的编排器、遍历闭包
# （ARM64 约 1.3 秒与 2.4 秒），入口一轮里几十个进程、每个批次与动作前各算一遍。环境变量
# CODEX_UPGRADE_IDENTITY_MEMO 指向一个绝对路径目录时，按「被计算的受管树逐文件摘要＋策略内容＋本模块源码摘要＋
# 解释器版本」做键缓存结果：任何一个受管文件、策略或算法变了，键就不同、必然重算；结果是纯函数的输出，命中时与
# 重算逐字节相同（缓存保留原来的键顺序）。条目里保存键的原文，读取时整段比对；目录不可读、条目不符或写入失败都
# 当作未命中、照常计算。不设环境变量时行为与原来完全相同。
IDENTITY_MEMO_ENV = "CODEX_UPGRADE_IDENTITY_MEMO"
IDENTITY_MEMO_SCHEMA = "codex-upgrade-identity-memo/v1"


@functools.lru_cache(maxsize=1)
def _algorithm_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _memo_entry(kind: str, key: Mapping[str, Any]) -> tuple[Path, dict[str, Any]] | None:
    configured = os.environ.get(IDENTITY_MEMO_ENV, "")
    if not configured or not os.path.isabs(configured):
        return None
    material = {
        "schema": IDENTITY_MEMO_SCHEMA,
        "kind": kind,
        "algorithm_sha256": _algorithm_sha256(),
        "python": ".".join(str(part) for part in sys.version_info[:3]),
        **key,
    }
    return Path(configured) / f"{kind}-{_fingerprint(material)}.json", material


def _memo_load(entry: tuple[Path, dict[str, Any]] | None) -> dict[str, Any] | None:
    if entry is None:
        return None
    path, material = entry
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("key") != material or not isinstance(payload.get("value"), dict):
        return None
    return payload["value"]


def _memo_save(entry: tuple[Path, dict[str, Any]] | None, value: Mapping[str, Any]) -> None:
    if entry is None:
        return
    path, material = entry
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        # 不排序键：命中时返回的字典与重算的字典键顺序一致，调用方按插入顺序写出的收据逐字节不变。
        temporary.write_text(json.dumps({"key": material, "value": value}, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        return


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """读取策略文件，校验形状，返回带 ``policy_sha256`` 的只读视图。"""

    source = Path(path) if path is not None else DEFAULT_POLICY_PATH
    if source.is_symlink() or not source.is_file():
        raise ToolIdentityPolicyError(f"策略文件不存在或不可信：{source}")
    raw = source.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ToolIdentityPolicyError("策略文件不是合法 JSON") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != POLICY_SCHEMA:
        raise ToolIdentityPolicyError("策略文件 schema 非法")
    version = payload.get("policy_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 2:
        raise ToolIdentityPolicyError("policy_version 非法")
    layers = payload.get("layers")
    if not isinstance(layers, dict) or set(layers) != set(LAYERS):
        raise ToolIdentityPolicyError("策略 layers 必须恰好是 wire_producer、evidence_semantics、control")
    for name, layer in layers.items():
        if not isinstance(layer, dict):
            raise ToolIdentityPolicyError(f"层 {name} 形态非法")
        for key in ("files", "prefixes", "suffixes"):
            values = layer.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
                raise ToolIdentityPolicyError(f"层 {name}.{key} 必须是非空字符串列表")
    ignored = payload.get("ignored_prefixes", [])
    if not isinstance(ignored, list) or any(not isinstance(v, str) or not v for v in ignored):
        raise ToolIdentityPolicyError("ignored_prefixes 非法")
    orchestrator = payload.get("orchestrator")
    if (
        not isinstance(orchestrator, dict)
        or not isinstance(orchestrator.get("file"), str)
        or not isinstance(orchestrator.get("wire_roots"), list)
        or not isinstance(orchestrator.get("evidence_roots"), list)
        or not isinstance(orchestrator.get("dynamic_call_names"), list)
    ):
        raise ToolIdentityPolicyError("orchestrator 配置非法")
    if payload.get("default_layer") not in LAYERS:
        raise ToolIdentityPolicyError("default_layer 非法")
    return {**payload, "policy_sha256": hashlib.sha256(raw).hexdigest(), "policy_path": str(source)}


def classify_path(policy: Mapping[str, Any], path: str) -> str | None:
    """返回路径所属层；被忽略的其它客户端工具返回 None；编排器文件归 control。"""

    for prefix in policy.get("ignored_prefixes", []):
        if path.startswith(prefix):
            return None
    if path == policy["orchestrator"]["file"]:
        return "control"
    basename = path.rsplit("/", 1)[-1]
    layers = policy["layers"]
    for name in LAYERS:
        layer = layers[name]
        if path in layer.get("files", []) or basename in layer.get("files", []):
            return name
        if any(path.endswith(suffix) for suffix in layer.get("suffixes", [])):
            return name
    for name in LAYERS:
        if any(path.startswith(prefix) for prefix in layers[name].get("prefixes", [])):
            return name
    return str(policy["default_layer"])


def layer_entries(policy: Mapping[str, Any], entries: list[Mapping[str, Any]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {name: [] for name in LAYERS}
    grouped["ignored"] = []
    grouped["defaulted"] = []
    explicit = set()
    for name in LAYERS:
        explicit.update(policy["layers"][name].get("files", []))
    for entry in entries:
        path = str(entry["path"])
        layer = classify_path(policy, path)
        record = {"path": path, "sha256": str(entry["sha256"])}
        if layer is None:
            grouped["ignored"].append(record)
            continue
        grouped[layer].append(record)
        if (
            path != policy["orchestrator"]["file"]
            and path not in explicit
            and path.rsplit("/", 1)[-1] not in explicit
            and not any(path.endswith(s) for s in policy["layers"][layer].get("suffixes", []))
            and not any(path.startswith(p) for p in policy["layers"][layer].get("prefixes", []))
        ):
            grouped["defaulted"].append(record)
    return grouped


# ---------------------------------------------------------------------------
# 编排器函数闭包
# ---------------------------------------------------------------------------


def _module_symbols(tree: ast.Module) -> tuple[dict[str, ast.AST], dict[str, ast.AST], dict[str, str]]:
    functions: dict[str, ast.AST] = {}
    constants: dict[str, ast.AST] = {}
    imports: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            functions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            constants[node.target.id] = node
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level > 0 and not module:
                # ``from . import x``：x 是受管模块。
                for alias in node.names:
                    imports[alias.asname or alias.name] = alias.name
            elif module == "tools.official_client_capture":
                for alias in node.names:
                    imports[alias.asname or alias.name] = alias.name
            elif module.startswith("tools.official_client_capture."):
                # ``from tools.official_client_capture.m import symbol``：符号属于模块 m。
                owner = module.split(".", 2)[2]
                for alias in node.names:
                    imports[alias.asname or alias.name] = owner
        elif isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if name.startswith("codex_upgrade") or name == "incremental_recovery":
                    imports[alias.asname or name] = name
    return functions, constants, imports


def _reject_dynamic(node: ast.AST, owner: str, policy: Mapping[str, Any], imports: Mapping[str, str], functions: Mapping[str, Any]) -> None:
    dynamic_names = set(policy["orchestrator"]["dynamic_call_names"])
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            if func.id in dynamic_names:
                raise ToolIdentityPolicyError(f"函数 {owner} 使用动态调用 {func.id}，闭包无法静态解析")
            if func.id in {"getattr", "setattr", "delattr"} and child.args:
                # 只有对受管模块别名做属性分发才是动态调用；对参数、对象属性的
                # getattr（如 mock 的 side_effect）不改变调用图。
                first = child.args[0]
                if isinstance(first, ast.Name) and first.id in imports:
                    raise ToolIdentityPolicyError(f"函数 {owner} 对受管模块做动态属性分发，闭包无法静态解析")
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            if func.value.id == "importlib" and func.attr == "import_module":
                raise ToolIdentityPolicyError(f"函数 {owner} 使用 importlib.import_module，闭包无法静态解析")


def orchestrator_closure(
    policy: Mapping[str, Any],
    tool_root: Path,
    roots: list[str],
    entry_digests: Mapping[str, str],
    *,
    layer: str | None = None,
) -> dict[str, Any]:
    """从根函数出发计算 ``codex_upgrade.py`` 的静态闭包并给出摘要。

    闭包引用的受管模块只有与 ``layer`` 同层的才计入摘要：wire 闭包引用监督器、账本
    等 control 模块时，这些模块的变化由 control 层自己表达，不能反向作废 wire 身份。
    """

    source_path = Path(tool_root) / policy["orchestrator"]["file"]
    if source_path.is_symlink() or not source_path.is_file():
        raise ToolIdentityPolicyError("编排器文件不存在")
    source = source_path.read_text(encoding="utf-8")
    symbols = _cached_symbol_closure(
        hashlib.sha256(source.encode("utf-8")).hexdigest(),
        source,
        tuple(sorted(roots)),
        tuple(policy["orchestrator"]["dynamic_call_names"]),
    )
    function_records, constant_records, seen_modules = symbols
    module_files: list[dict[str, str]] = []
    for module in sorted(seen_modules):
        # 点分模块名映射到受管树路径；包引用落到其 __init__.py。
        base = module.replace(".", "/")
        candidates = (f"{base}.py", f"{base}/__init__.py")
        relative = next((c for c in candidates if c in entry_digests), None)
        if relative is None:
            raise ToolIdentityPolicyError(f"闭包引用的受管模块不在工具树内：{candidates[0]}")
        if layer is not None and classify_path(policy, relative) != layer:
            continue
        module_files.append({"module": module, "path": relative, "sha256": entry_digests[relative]})
    closure = {"roots": sorted(roots), "functions": list(function_records), "constants": list(constant_records), "modules": module_files}
    return {**closure, "closure_sha256": _fingerprint(closure)}


@functools.lru_cache(maxsize=1)
def _parsed_orchestrator(
    source_sha256: str, source: str
) -> tuple[dict[str, ast.AST], dict[str, ast.AST], dict[str, str], tuple[bytes, ...] | None]:
    """编排器源码的解析结果：wire 与 evidence 两组根函数共用同一次 ``ast.parse``（E1-01）。

    同时把源码预先按行切成 UTF-8 字节，供 ``_reader_source_segment`` 按字节偏移取函数与常量的源码段。
    ``ast.get_source_segment`` 每取一段都把整份源码重新分行，6.3 万行的编排器上取 700 多段，ARM64 上
    要 27 秒；预切一次之后不到 1 秒，取出的源码段逐字节相同。源码含 ``\\r`` 时不预切，退回标准实现。
    语法树常驻约 100 MB，``compute_identity_v2`` 算完两组根即释放。
    """

    del source_sha256  # 只作缓存键：内容变化即重新解析
    functions, constants, imports = _module_symbols(ast.parse(source))
    lines: tuple[bytes, ...] | None = None
    if "\r" not in source:
        parts = source.split("\n")
        lines = tuple(
            [(part + "\n").encode("utf-8") for part in parts[:-1]] + ([parts[-1].encode("utf-8")] if parts[-1] else [])
        )
    return functions, constants, imports, lines


@functools.lru_cache(maxsize=16)
def _cached_symbol_closure(
    source_sha256: str,
    source: str,
    roots: tuple[str, ...],
    dynamic_call_names: tuple[str, ...],
) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...], tuple[str, ...]]:
    """按源码摘要缓存函数／常量闭包；同一进程内反复校验不重复解析编排器。"""

    policy = {"orchestrator": {"dynamic_call_names": list(dynamic_call_names)}}
    functions, constants, imports, lines = _parsed_orchestrator(source_sha256, source)
    roots = list(roots)
    missing = [name for name in roots if name not in functions]
    if missing:
        raise ToolIdentityPolicyError(f"策略登记的根函数不存在：{missing}")
    seen_functions: set[str] = set()
    seen_constants: set[str] = set()
    seen_modules: set[str] = set()
    pending = list(roots)
    while pending:
        name = pending.pop()
        if name in seen_functions or name not in functions:
            continue
        seen_functions.add(name)
        node = functions[name]
        _reject_dynamic(node, name, policy, imports, functions)
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                if child.id in functions and child.id not in seen_functions:
                    pending.append(child.id)
                elif child.id in constants:
                    seen_constants.add(child.id)
                elif child.id in imports:
                    seen_modules.add(imports[child.id])
    # 常量之间也可能互相引用（如 frozenset 并集），递归收敛。
    changed = True
    while changed:
        changed = False
        for name in sorted(seen_constants):
            for child in ast.walk(constants[name]):
                if isinstance(child, ast.Name):
                    if child.id in constants and child.id not in seen_constants:
                        seen_constants.add(child.id)
                        changed = True
                    elif child.id in functions and child.id not in seen_functions:
                        pending.append(child.id)
                    elif child.id in imports:
                        seen_modules.add(imports[child.id])
        while pending:
            name = pending.pop()
            if name in seen_functions or name not in functions:
                continue
            seen_functions.add(name)
            _reject_dynamic(functions[name], name, policy, imports, functions)
            changed = True
            for child in ast.walk(functions[name]):
                if isinstance(child, ast.Name):
                    if child.id in functions and child.id not in seen_functions:
                        pending.append(child.id)
                    elif child.id in constants:
                        seen_constants.add(child.id)
                    elif child.id in imports:
                        seen_modules.add(imports[child.id])
    # 源码段与 ast.get_source_segment 逐字节相同（见 _parsed_orchestrator），闭包摘要不随本次提速变化。
    function_records = tuple(
        {"name": name, "sha256": hashlib.sha256(_reader_source_segment(source, lines, functions[name]).encode("utf-8")).hexdigest()}
        for name in sorted(seen_functions)
    )
    constant_records = tuple(
        {"name": name, "sha256": hashlib.sha256(_reader_source_segment(source, lines, constants[name]).encode("utf-8")).hexdigest()}
        for name in sorted(seen_constants)
    )
    return function_records, constant_records, tuple(sorted(seen_modules))


def compute_identity_v2(
    policy: Mapping[str, Any],
    tool_root: Path,
    entries: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """按策略计算 wire／evidence／control 三层摘要与策略摘要（设置了跨进程记忆化时先查缓存）。"""

    entry = None
    if os.environ.get(IDENTITY_MEMO_ENV):
        try:
            entry = _memo_entry("identity-v2", {
                "policy": _fingerprint(dict(policy)),
                "entries": _fingerprint([dict(item) for item in entries]),
                "tree": _fingerprint(_managed_tree_digests(Path(tool_root))),
            })
        except (OSError, TypeError, ValueError):
            entry = None
        cached = _memo_load(entry)
        if cached is not None:
            return cached
    result = _compute_identity_v2(policy, tool_root, entries)
    _memo_save(entry, result)
    return result


def _compute_identity_v2(
    policy: Mapping[str, Any],
    tool_root: Path,
    entries: list[Mapping[str, Any]],
) -> dict[str, Any]:
    grouped = layer_entries(policy, entries)
    digests = {str(e["path"]): str(e["sha256"]) for e in entries}
    wire_closure = orchestrator_closure(policy, tool_root, list(policy["orchestrator"]["wire_roots"]), digests, layer="wire_producer")
    evidence_closure = orchestrator_closure(policy, tool_root, list(policy["orchestrator"]["evidence_roots"]), digests, layer="evidence_semantics")
    # 两组根已算完：释放编排器语法树。闭包结果另有缓存，源码不变时同一进程不再解析。
    _parsed_orchestrator.cache_clear()
    wire = {"entries": grouped["wire_producer"], "orchestrator_closure_sha256": wire_closure["closure_sha256"]}
    evidence = {"entries": grouped["evidence_semantics"], "orchestrator_closure_sha256": evidence_closure["closure_sha256"]}
    control = {"entries": grouped["control"]}
    return {
        "policy_version": int(policy["policy_version"]),
        "policy_sha256": str(policy["policy_sha256"]),
        "wire_producer_sha256": _fingerprint(wire),
        "evidence_semantics_sha256": _fingerprint(evidence),
        "control_sha256": _fingerprint(control),
        "layer_counts": {
            "wire_producer": len(grouped["wire_producer"]),
            "evidence_semantics": len(grouped["evidence_semantics"]),
            "control": len(grouped["control"]),
            "ignored": len(grouped["ignored"]),
            "defaulted": len(grouped["defaulted"]),
        },
        "defaulted_paths": [item["path"] for item in grouped["defaulted"]],
        "orchestrator_closures": {
            "wire_producer": {"closure_sha256": wire_closure["closure_sha256"], "function_count": len(wire_closure["functions"]), "constant_count": len(wire_closure["constants"]), "modules": [m["module"] for m in wire_closure["modules"]]},
            "evidence_semantics": {"closure_sha256": evidence_closure["closure_sha256"], "function_count": len(evidence_closure["functions"]), "constant_count": len(evidence_closure["constants"]), "modules": [m["module"] for m in evidence_closure["modules"]]},
        },
    }


def layer_drift(
    policy: Mapping[str, Any],
    expected_entries: list[Mapping[str, Any]],
    current_entries: list[Mapping[str, Any]],
) -> dict[str, list[str]]:
    """逐文件比较两份清单，按层归类变化路径（新增、删除、修改都算）。"""

    before = {str(e["path"]): str(e["sha256"]) for e in expected_entries}
    after = {str(e["path"]): str(e["sha256"]) for e in current_entries}
    changed = sorted(p for p in set(before) | set(after) if before.get(p) != after.get(p))
    drift: dict[str, list[str]] = {name: [] for name in LAYERS}
    drift["ignored"] = []
    for path in changed:
        layer = classify_path(policy, path)
        drift[layer if layer is not None else "ignored"].append(path)
    return drift


# ---------------------------------------------------------------------------
# 改造 5（评估失败局部恢复）：evaluator 四项直接依赖摘要
# ---------------------------------------------------------------------------

EVALUATOR_CHECKER_RELATIVE = "candidate_rule_assertion.py"
EVALUATOR_BUILDER_RELATIVE = "build_rule_assertion_results.py"
EVALUATOR_COMPARE_READER_ROOTS = ("compare_campaign",)
EVALUATOR_ACCEPT_READER_ROOTS = ("accept_campaign",)
EVALUATOR_DIGEST_FIELDS = (
    "checker_sha256",
    "builder_sha256",
    "compare_reader_sha256",
    "accept_reader_sha256",
)


def _managed_tree_digests(tool_root: Path) -> dict[str, str]:
    """按受管口径列出工具树文件摘要（与 ``codex_upgrade._tool_tree_entries`` 同一口径）。"""

    root = Path(tool_root)
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix not in {".py", ".sh", ".json"}:
            continue
        parts = path.relative_to(root).parts
        if "tests" in parts or "versions" in parts or "__pycache__" in parts:
            continue
        digests[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def evaluator_dependency_digests(
    tool_root: Path | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """evaluator 四项直接依赖摘要（编译器冻结进 batch v3、COMMIT 前核对、evaluation-run 记录）。

    * ``checker_sha256``：``candidate_rule_assertion.py`` 整文件摘要；
    * ``builder_sha256``：``build_rule_assertion_results.py`` 整文件摘要（三方裁定的强制口径）；
    * ``compare_reader_sha256``／``accept_reader_sha256``：``codex_upgrade.py`` 中 ``compare_campaign``／
      ``accept_campaign`` 根的读侧闭包摘要（evaluator-reader-closure/v2，修好接着跑第 8 项后半：
      排除登记的守卫与账本读取，control 层按符号计入，补上延迟导入；旧口径见
      ``legacy_evaluator_reader_digests``）。

    编译器与派发入口的 commit 回调调用同一个纯函数，保证"冻结值"与"核对值"口径一致。
    """

    root = Path(tool_root) if tool_root is not None else Path(__file__).resolve().parent
    active_policy = dict(policy) if policy is not None else load_policy(root / POLICY_FILENAME)
    digests = _managed_tree_digests(root)
    for relative in (EVALUATOR_CHECKER_RELATIVE, EVALUATOR_BUILDER_RELATIVE):
        if relative not in digests:
            raise ToolIdentityPolicyError(f"evaluator 直接依赖不在工具树内：{relative}")
    entry = None
    if os.environ.get(IDENTITY_MEMO_ENV):
        try:
            entry = _memo_entry("evaluator", {"policy": _fingerprint(active_policy), "tree": _fingerprint(digests)})
        except (TypeError, ValueError):
            entry = None
        cached = _memo_load(entry)
        if cached is not None:
            return cached
    compare_closure = evaluator_reader_closure(
        active_policy, root, list(EVALUATOR_COMPARE_READER_ROOTS), digests
    )
    accept_closure = evaluator_reader_closure(
        active_policy, root, list(EVALUATOR_ACCEPT_READER_ROOTS), digests
    )
    result = {
        "checker_sha256": digests[EVALUATOR_CHECKER_RELATIVE],
        "builder_sha256": digests[EVALUATOR_BUILDER_RELATIVE],
        "compare_reader_sha256": compare_closure["closure_sha256"],
        "accept_reader_sha256": accept_closure["closure_sha256"],
    }
    _memo_save(entry, result)
    return result


# ---------------------------------------------------------------------------
# 修好接着跑第 8 项后半：评估器读侧闭包收窄
# ---------------------------------------------------------------------------
#
# 旧口径（``orchestrator_closure(layer=None)``）从 compare／accept 根出发，经 ``load_campaign_manifest``、
# ``_verify_plan_identity`` 等枢纽把几乎全部控制面拉进来，且闭包引用的受管模块整文件计入：改一行监督器、
# 账本、租约都会让两项 reader 摘要变化。b≥1 时编译授权因此失配，而 evaluation-recover 又不受理非评估器
# 缺陷的变化——修一次基础设施就卡死。新口径（evaluator-reader-closure/v2）：
#   · 根不变，根本体与评估链函数一律函数级计入；
#   · ``codex_upgrade.py`` 内登记的守卫／副作用函数是屏障（不展开、不计入），登记时写明调用点形态，
#     测试逐一核对（代码一旦开始使用纯守卫的返回值，测试即失败）；
#   · control 层模块按符号跨模块递归计入（旧口径整文件），evidence／wire 与未分层模块仍整文件计入；
#   · 函数体内的延迟导入与 ``模块.属性`` 引用按符号解析（旧口径的盲区：reconciler 的恢复段复用证明
#     明明影响 accept 的读取结果，却不在摘要里）；
#   · 账本读取（``_campaign_ledger_summary_for_baseline`` 与 timing_ledger 的 replay／stopped_phase）是
#     屏障，当前评估基线改由 compare／accept 运行时与父 run 冻结值核对（``codex_upgrade`` 负责）。
# 旧算法保留为 ``legacy_evaluator_reader_digests``，只用于识别旧口径冻结值（b≥1 编译授权与
# evaluation-recover 的变化判定）；wire／evidence 闭包所用的函数一律不改，两层摘要逐字不变。
READER_CLOSURE_SCHEMA = "evaluator-reader-closure/v2"
READER_READER_FIELDS = ("compare_reader_sha256", "accept_reader_sha256")
# ``codex_upgrade.py`` 内的读侧屏障 → 该函数在读侧闭包内允许的调用点形态。
#   discard：纯守卫，全部调用点丢弃返回值（只会拒绝，不改变读取结果）；
#   with：锁等上下文管理副作用；
#   assign／compare／return：人工审查登记——返回值只用于"不等即报错"的比较、控制面路径或 CLI 输出，
#   不进入比较收据、断言模板或验收结论；_campaign_ledger_summary_for_baseline 决定当前基线，配运行时核对。
READER_BARRIERS: dict[str, frozenset[str]] = {
    # 纯守卫（19）
    "_bind_active_lease_attempt": frozenset({"discard"}),
    "_guard_candidate_revision_write": frozenset({"discard"}),
    "_reject_contaminated_campaign": frozenset({"discard"}),
    "_verify_plan_identity": frozenset({"discard"}),
    "_verify_control_receipts": frozenset({"discard"}),
    "_validate_initial_vc_control_artifacts": frozenset({"discard"}),
    "_validate_attempt_failure_facts": frozenset({"discard"}),
    "_validate_attempt_incremental_fields": frozenset({"discard"}),
    "_validate_attempt_watchdog_bindings": frozenset({"discard"}),
    "_validate_deadline_orphan_attempt_bindings": frozenset({"discard"}),
    "_replay_attempt_evidence_permissions": frozenset({"discard"}),
    "_official_evidence_reuse_attempt_source": frozenset({"discard"}),
    "_load_classification_candidate_reuse_transition": frozenset({"discard"}),
    "_assert_sealed_stage_checkpoint_progression": frozenset({"discard"}),
    "_validate_direct_predecessor_official_attempt": frozenset({"discard"}),
    "_write_blocked_acceptance_attempt": frozenset({"discard"}),
    "_validate_candidate_readiness_binding": frozenset({"discard"}),
    "_replay_attempt_recovery_evidence_permissions": frozenset({"discard"}),
    "_verify_baseline_evaluation_epoch": frozenset({"discard"}),
    # 副作用（2）：CLI 输出的 VC 完成收据、Campaign 锁
    "_complete_vc_with_receipt": frozenset({"assign"}),
    "_campaign_lock": frozenset({"with"}),
    # 比较型守卫与控制面路径（人工审查登记）
    "_control_replacement_context": frozenset({"assign"}),
    "_supervisor_audit_bound_mapping_matches": frozenset({"compare", "return"}),
    "_imported_stage_evaluation_transition_source": frozenset({"assign"}),
    "_candidate_control_refresh_source_transition": frozenset({"assign"}),
    "_sealed_stage_timing_checkpoint": frozenset({"assign"}),
    "_successor_runtime_configuration": frozenset({"assign"}),
    "_successor_copy_expectations": frozenset({"assign"}),
    "_control_receipt_relative": frozenset({"assign"}),
    "_load_capture_reservation": frozenset({"assign", "discard"}),
    "_replay_evidence_permission_closeout": frozenset({"return"}),
    # 唯一非守卫：账本决定当前评估基线，配 compare／accept 运行时核对
    "_campaign_ledger_summary_for_baseline": frozenset({"assign"}),
}
# 其它模块的读侧屏障（模块名 → 符号）：账本回放与停线阶段只经屏障函数进入，同样由运行时核对兜底。
READER_MODULE_BARRIERS: dict[str, frozenset[str]] = {
    "codex_upgrade_timing_ledger": frozenset({"replay", "stopped_phase"}),
}
_MANAGED_PACKAGE = "tools.official_client_capture"


def _managed_module_path(module: str, digests: Mapping[str, str]) -> str | None:
    base = module.replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if candidate in digests:
            return candidate
    return None


def _reader_imports(
    nodes: Any,
    package: str,
    digests: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """从导入语句解析受管模块别名（别名 → 模块）与符号导入（别名 → (模块, 符号)）。

    覆盖包形式（``from tools.official_client_capture import m``、``from ...m import s``）、相对形式
    （``from . import m``、``from .m import s``）与脚本形式（``import m``），模块须在受管树内。
    """

    modules: dict[str, str] = {}
    symbols: dict[str, tuple[str, str]] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                base = package
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                owner = ".".join(part for part in (base, node.module or "") if part)
            elif node.module == _MANAGED_PACKAGE:
                owner = ""
            elif (node.module or "").startswith(_MANAGED_PACKAGE + "."):
                owner = (node.module or "")[len(_MANAGED_PACKAGE) + 1:]
            else:
                continue
            for alias in node.names:
                name = alias.asname or alias.name
                if not owner:
                    # 包形式 ``from tools.official_client_capture import m`` 与顶层包内的 ``from . import m``。
                    if _managed_module_path(alias.name, digests):
                        modules[name] = alias.name
                    continue
                submodule = f"{owner}.{alias.name}"
                if _managed_module_path(submodule, digests):
                    modules[name] = submodule
                elif _managed_module_path(owner, digests):
                    symbols[name] = (owner, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                full = alias.name
                if full.startswith(_MANAGED_PACKAGE + "."):
                    full = full[len(_MANAGED_PACKAGE) + 1:]
                if _managed_module_path(full, digests):
                    modules[alias.asname or alias.name.split(".")[0]] = full
    return modules, symbols


def _top_level_import_nodes(tree: ast.Module) -> list[ast.AST]:
    """模块顶层（含 if／try／with 块内、不进函数与类）的导入语句。"""

    found: list[ast.AST] = []
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            found.append(node)
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                pending.extend(getattr(node, field, []) or [])
        elif isinstance(node, ast.ExceptHandler):
            pending.extend(node.body)
    return found


@functools.lru_cache(maxsize=64)
def _reader_parsed_module(
    path: str, sha256: str
) -> tuple[str, ast.Module, dict[str, ast.AST], dict[str, ast.AST], tuple[bytes, ...] | None]:
    del sha256  # 只作缓存键：内容变化即重新解析
    source = Path(path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions, constants, _imports = _module_symbols(tree)
    # 预先按行切成 UTF-8 字节：ast.get_source_segment 每次都把整个源码重新分行，4 万行的编排器上
    # 数百个符号会慢到秒级。只含 \n 换行时逐行切片与其逐字相同；含 \r 时退回标准实现。
    lines: tuple[bytes, ...] | None = None
    if "\r" not in source:
        parts = source.split("\n")
        lines = tuple(
            [(part + "\n").encode("utf-8") for part in parts[:-1]] + ([parts[-1].encode("utf-8")] if parts[-1] else [])
        )
    return source, tree, functions, constants, lines


def _reader_source_segment(source: str, lines: tuple[bytes, ...] | None, node: ast.AST) -> str:
    """与 ``ast.get_source_segment(source, node)`` 逐字相同的源码段（行已预切时按字节偏移切片）。"""

    if lines is None or getattr(node, "end_lineno", None) is None:
        return ast.get_source_segment(source, node) or ""
    start, end = node.lineno - 1, node.end_lineno - 1
    if start == end:
        return lines[start][node.col_offset:node.end_col_offset].decode("utf-8")
    return (
        lines[start][node.col_offset:] + b"".join(lines[start + 1:end]) + lines[end][:node.end_col_offset]
    ).decode("utf-8")


def evaluator_reader_closure(
    policy: Mapping[str, Any],
    tool_root: Path,
    roots: list[str],
    digests: Mapping[str, str],
) -> dict[str, Any]:
    """读侧闭包（evaluator-reader-closure/v2）：见本节说明。返回闭包明细与 ``closure_sha256``。"""

    root = Path(tool_root)
    orchestrator = Path(str(policy["orchestrator"]["file"])).with_suffix("").as_posix().replace("/", ".")
    dynamic_policy = {"orchestrator": {"dynamic_call_names": list(policy["orchestrator"]["dynamic_call_names"])}}
    if _managed_module_path(orchestrator, digests) is None:
        raise ToolIdentityPolicyError("编排器文件不存在")
    parsed: dict[str, tuple[Any, ...]] = {}

    def module_info(module: str):
        if module not in parsed:
            relative = _managed_module_path(module, digests)
            if relative is None:
                raise ToolIdentityPolicyError(f"读侧闭包引用的受管模块不在工具树内：{module}")
            source, tree, functions, constants, lines = _reader_parsed_module(str(root / relative), digests[relative])
            package = module.rpartition(".")[0]
            aliases, symbol_imports = _reader_imports(_top_level_import_nodes(tree), package, digests)
            parsed[module] = (source, tree, functions, constants, aliases, symbol_imports, lines)
        return parsed[module]

    _source, _tree, orchestrator_functions, _constants, _aliases, _symbols, _lines = module_info(orchestrator)
    missing = [name for name in roots if name not in orchestrator_functions]
    if missing:
        raise ToolIdentityPolicyError(f"读侧闭包的根函数不存在：{missing}")
    symbols: dict[tuple[str, str], dict[str, str]] = {}
    whole: dict[str, dict[str, str]] = {}
    pending: list[tuple[str, str]] = [(orchestrator, name) for name in roots]

    def whole_module(module: str) -> None:
        relative = _managed_module_path(module, digests)
        if relative is not None and module not in whole:
            whole[module] = {"module": module, "path": relative, "sha256": digests[relative]}

    def refer(module: str, name: str) -> None:
        relative = _managed_module_path(module, digests)
        if relative is None:
            return
        if module == orchestrator or classify_path(policy, relative) == "control":
            pending.append((module, name))
        else:
            whole_module(module)

    while pending:
        module, name = pending.pop()
        if (module, name) in symbols:
            continue
        if module == orchestrator and name in READER_BARRIERS:
            continue
        if name in READER_MODULE_BARRIERS.get(module, frozenset()):
            continue
        source, tree, functions, constants, aliases, symbol_imports, lines = module_info(module)
        node = functions.get(name) or constants.get(name)
        if node is None:
            # control 模块里静态找不到的符号（类属性、再导出、动态定义）：保守整文件计入。
            if module != orchestrator:
                whole_module(module)
            continue
        kind = "function" if name in functions else "constant"
        symbols[(module, name)] = {
            "module": module,
            "name": name,
            "kind": kind,
            "sha256": hashlib.sha256(_reader_source_segment(source, lines, node).encode("utf-8")).hexdigest(),
        }
        local_aliases, local_symbols = _reader_imports(
            [child for child in ast.walk(node) if isinstance(child, (ast.Import, ast.ImportFrom))],
            module.rpartition(".")[0],
            digests,
        )
        scope_aliases = {**aliases, **local_aliases}
        scope_symbols = {**symbol_imports, **local_symbols}
        if kind == "function":
            try:
                _reject_dynamic(node, f"{module}.{name}", dynamic_policy, scope_aliases, functions)
            except ToolIdentityPolicyError:
                if module == orchestrator:
                    raise
                # control 模块里无法静态解析的函数：保守把本模块与被动态分发的模块整文件计入。
                whole_module(module)
                for child in ast.walk(node):
                    if (
                        isinstance(child, ast.Call)
                        and isinstance(child.func, ast.Name)
                        and child.func.id in {"getattr", "setattr", "delattr"}
                        and child.args
                        and isinstance(child.args[0], ast.Name)
                        and child.args[0].id in scope_aliases
                    ):
                        whole_module(scope_aliases[child.args[0].id])
        attribute_bases: set[int] = set()
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Name)
                and child.value.id in scope_aliases
                and child.value.id not in functions
                and child.value.id not in constants
            ):
                attribute_bases.add(id(child.value))
                refer(scope_aliases[child.value.id], child.attr)
        for child in ast.walk(node):
            if not isinstance(child, ast.Name) or id(child) in attribute_bases or child.id == name:
                continue
            if child.id in functions or child.id in constants:
                pending.append((module, child.id))
            elif child.id in scope_symbols:
                refer(*scope_symbols[child.id])
            elif child.id in scope_aliases:
                # 模块对象整体被传递或赋值：无法按符号追踪，保守整文件计入。
                whole_module(scope_aliases[child.id])

    closure = {
        "schema_version": READER_CLOSURE_SCHEMA,
        "roots": sorted(roots),
        "barriers": sorted(READER_BARRIERS),
        "module_barriers": sorted(f"{module}:{name}" for module, names in READER_MODULE_BARRIERS.items() for name in names),
        "symbols": [symbols[key] for key in sorted(symbols)],
        "modules": [whole[key] for key in sorted(whole)],
    }
    return {**closure, "closure_sha256": _fingerprint(closure)}


def legacy_evaluator_reader_digests(
    tool_root: Path | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """旧口径（``orchestrator_closure(layer=None)``）的两项 reader 摘要：只用于识别旧口径冻结值。"""

    root = Path(tool_root) if tool_root is not None else Path(__file__).resolve().parent
    active_policy = dict(policy) if policy is not None else load_policy(root / POLICY_FILENAME)
    digests = _managed_tree_digests(root)
    return {
        "compare_reader_sha256": orchestrator_closure(
            active_policy, root, list(EVALUATOR_COMPARE_READER_ROOTS), digests, layer=None
        )["closure_sha256"],
        "accept_reader_sha256": orchestrator_closure(
            active_policy, root, list(EVALUATOR_ACCEPT_READER_ROOTS), digests, layer=None
        )["closure_sha256"],
    }

