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


@functools.lru_cache(maxsize=16)
def _cached_symbol_closure(
    source_sha256: str,
    source: str,
    roots: tuple[str, ...],
    dynamic_call_names: tuple[str, ...],
) -> tuple[tuple[dict[str, str], ...], tuple[dict[str, str], ...], tuple[str, ...]]:
    """按源码摘要缓存函数／常量闭包；同一进程内反复校验不重复解析 4 万行。"""

    policy = {"orchestrator": {"dynamic_call_names": list(dynamic_call_names)}}
    tree = ast.parse(source)
    functions, constants, imports = _module_symbols(tree)
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
    function_records = tuple(
        {"name": name, "sha256": hashlib.sha256((ast.get_source_segment(source, functions[name]) or "").encode("utf-8")).hexdigest()}
        for name in sorted(seen_functions)
    )
    constant_records = tuple(
        {"name": name, "sha256": hashlib.sha256((ast.get_source_segment(source, constants[name]) or "").encode("utf-8")).hexdigest()}
        for name in sorted(seen_constants)
    )
    return function_records, constant_records, tuple(sorted(seen_modules))


def compute_identity_v2(
    policy: Mapping[str, Any],
    tool_root: Path,
    entries: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """按策略计算 wire／evidence／control 三层摘要与策略摘要。"""

    grouped = layer_entries(policy, entries)
    digests = {str(e["path"]): str(e["sha256"]) for e in entries}
    wire_closure = orchestrator_closure(policy, tool_root, list(policy["orchestrator"]["wire_roots"]), digests, layer="wire_producer")
    evidence_closure = orchestrator_closure(policy, tool_root, list(policy["orchestrator"]["evidence_roots"]), digests, layer="evidence_semantics")
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
