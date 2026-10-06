"""Claude 受管工具身份引擎 v3（Claude 升级控制面设计 B1-07）。

与 Codex 引擎（``codex_upgrade_tool_identity_policy``，策略 v2，不改）同构地给出五个摘要，扩展三处：

* 多根：受管清单覆盖策略 ``roots`` 下的全部根（``tools/official_client_capture``、``tools/official_client_control``），
  条目键为 ``<根目录名>/<相对路径>``，后缀按策略（含 ``.mjs``）；``runtime_bindings``（node、typescript 等受管运行时的
  冻结摘要）进入 ``control_sha256``。
* 闭包跨模块按符号：编排器 wire／evidence 闭包与评估读侧闭包都按 ``evaluator-reader-closure/v2`` 的口径——编排器与
  control 层模块按符号计入；编排器闭包对与本层相同的模块整文件计入、其他层不计入（其变化由该层自己的摘要表达）；
  读侧闭包对非 control 层模块整文件计入（同 Codex）。
* 经适配器的间接调用可见：共享代码写 ``adapter.orchestrator.<名字>``、``adapter.<成员>``、``adapter.<恢复接口>``，这里按
  ``upgrade_client_adapter.ADAPTER_BINDINGS[client]`` 解析为具体模块与符号继续遍历（模块型成员后接属性时解析到该模块的
  符号）；名字不在绑定表，或 ``orchestrator`` 被赋值、传递、``getattr``，判为无法解析，``plan`` 与认证失败。适配接口的
  注册表与动态解析机制（``adapter_for``、``_resolve``、``_import_managed``、``OrchestratorFacade``）是引擎内建屏障：
  间接调用已由本引擎直接解析到实现，不再穿过这层机制。
* 闭包到达被策略忽略的文件（``ignored_prefixes``，即另一客户端的专属文件）即失败：Codex 引擎对其它层模块只是
  不计入，跨模块按符号遍历后照此处理会让 Claude 闭包实际依赖的 Codex 实现在身份里不可见。到达即说明它不再专属，
  要么在策略里归层，要么改经适配器。

纯函数（指纹、分层、模块符号表、动态调用检查、源码段、跨进程记忆化）从 Codex 引擎调用，不复制；导入解析因要覆盖
两个包而另写。本文件被 Codex 身份策略忽略（``claude_`` 前缀），不影响 Codex 的任何摘要。
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Mapping

if __package__ in {None, ""}:
    import codex_upgrade_tool_identity_policy as codex_identity
    import upgrade_client_adapter
else:
    from . import codex_upgrade_tool_identity_policy as codex_identity
    from . import upgrade_client_adapter

ToolIdentityPolicyError = codex_identity.ToolIdentityPolicyError
POLICY_SCHEMA = "tool-identity-policy/v3"
POLICY_FILENAME = "claude_tool_identity_policy_v2.json"
DEFAULT_POLICY_PATH = Path(__file__).resolve().parent / POLICY_FILENAME
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LAYERS = codex_identity.LAYERS
CLOSURE_SCHEMA = "claude-identity-closure/v3"
EVALUATOR_DIGEST_FIELDS = codex_identity.EVALUATOR_DIGEST_FIELDS
EXCLUDED_PARTS = frozenset({"tests", "versions", "__pycache__"})
ADAPTER_MODULE = "upgrade_client_adapter"
# 适配接口的注册表与动态解析机制：间接调用已按绑定表解析到实现，遍历到这些符号即停。
ADAPTER_MECHANISM_BARRIERS = frozenset({"adapter_for", "_resolve", "_import_managed", "OrchestratorFacade"})
# ClientAdapter 上客户端无关的恢复接口：方法体以字面 ``self.orchestrator.<名字>`` 转调，遍历类体即可解析。
ADAPTER_METHODS = frozenset({"recovery_reuse_check", "recovery_preview_continuity", "attempt_evolution_impact"})


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# 策略
# ---------------------------------------------------------------------------


def load_policy(path: Path | None = None) -> dict[str, Any]:
    """读取并校验 v3 策略，返回带 ``policy_sha256``／``policy_path`` 的视图。"""

    source = Path(path) if path is not None else DEFAULT_POLICY_PATH
    if source.is_symlink() or not source.is_file():
        raise ToolIdentityPolicyError(f"Claude 身份策略文件不存在或不可信：{source}")
    raw = source.read_bytes()
    try:
        payload = json.loads(raw)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ToolIdentityPolicyError("Claude 身份策略不是合法 JSON") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != POLICY_SCHEMA:
        raise ToolIdentityPolicyError("Claude 身份策略 schema 非法")
    if payload.get("client") not in upgrade_client_adapter.ADAPTER_BINDINGS:
        raise ToolIdentityPolicyError(f"Claude 身份策略 client 非法：{payload.get('client')!r}")
    version = payload.get("policy_version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ToolIdentityPolicyError("policy_version 非法")
    roots = payload.get("roots")
    if not isinstance(roots, list) or not roots or len(set(roots)) != len(roots):
        raise ToolIdentityPolicyError("roots 必须是不重复的非空列表")
    names = [str(root).rstrip("/").rsplit("/", 1)[-1] for root in roots]
    if any(not isinstance(root, str) or not root.startswith("tools/") for root in roots) or len(set(names)) != len(names):
        raise ToolIdentityPolicyError("roots 必须是 tools/ 下根目录名互不相同的路径")
    suffixes = payload.get("suffixes")
    if not isinstance(suffixes, list) or not suffixes or any(not isinstance(item, str) or not item.startswith(".") for item in suffixes):
        raise ToolIdentityPolicyError("suffixes 非法")
    layers = payload.get("layers")
    if not isinstance(layers, dict) or set(layers) != set(LAYERS):
        raise ToolIdentityPolicyError("layers 必须恰好是 wire_producer、evidence_semantics、control")
    for name, layer in layers.items():
        if not isinstance(layer, dict):
            raise ToolIdentityPolicyError(f"层 {name} 形态非法")
        for key in ("files", "prefixes", "suffixes"):
            values = layer.get(key, [])
            if not isinstance(values, list) or any(not isinstance(item, str) or not item for item in values):
                raise ToolIdentityPolicyError(f"层 {name}.{key} 必须是非空字符串列表")
    ignored = payload.get("ignored_prefixes", [])
    if not isinstance(ignored, list) or any(not isinstance(item, str) or not item for item in ignored):
        raise ToolIdentityPolicyError("ignored_prefixes 非法")
    orchestrator = payload.get("orchestrator")
    if (
        not isinstance(orchestrator, dict)
        or not isinstance(orchestrator.get("file"), str)
        or any(not isinstance(orchestrator.get(key), list) for key in ("wire_roots", "evidence_roots", "dynamic_call_names"))
    ):
        raise ToolIdentityPolicyError("orchestrator 配置非法")
    if payload.get("default_layer") not in LAYERS:
        raise ToolIdentityPolicyError("default_layer 非法")
    bindings = payload.get("runtime_bindings")
    if not isinstance(bindings, list) or any(
        not isinstance(item, dict) or set(item) != {"name", "path", "sha256", "version"}
        or not all(isinstance(item[key], str) and item[key] for key in item)
        for item in bindings
    ):
        raise ToolIdentityPolicyError("runtime_bindings 每项必须恰好含 name、path、sha256、version")
    evaluator = payload.get("evaluator")
    if (
        not isinstance(evaluator, dict)
        or set(evaluator) != {"checker", "builder", "compare_reader_roots", "accept_reader_roots"}
        or not all(isinstance(evaluator[key], str) for key in ("checker", "builder"))
        or not all(isinstance(evaluator[key], list) and evaluator[key] for key in ("compare_reader_roots", "accept_reader_roots"))
    ):
        raise ToolIdentityPolicyError("evaluator 配置非法")
    barriers = payload.get("reader_barriers", {})
    if not isinstance(barriers, dict) or any(
        not isinstance(key, str) or ":" not in key or not isinstance(value, list) for key, value in barriers.items()
    ):
        raise ToolIdentityPolicyError("reader_barriers 必须是「模块:符号」→ 调用点形态列表")
    return {**payload, "policy_sha256": _sha256(raw), "policy_path": str(source)}


def classify_path(policy: Mapping[str, Any], key: str) -> str | None:
    """多根条目键所属的层（规则与 Codex 引擎相同：忽略前缀 → 编排器 → files／suffixes → prefixes → 默认层）。"""

    return codex_identity.classify_path(policy, key)


def _root_names(policy: Mapping[str, Any]) -> list[tuple[str, str]]:
    """(根目录相对仓库的路径, 根目录名)。"""

    return [(str(root).rstrip("/"), str(root).rstrip("/").rsplit("/", 1)[-1]) for root in policy["roots"]]


def managed_tree_digests(repository_root: Path, policy: Mapping[str, Any]) -> dict[str, str]:
    """多根受管清单：``<根目录名>/<相对路径>`` → sha256；排除 tests、versions 与 __pycache__。"""

    suffixes = set(policy["suffixes"])
    digests: dict[str, str] = {}
    for relative_root, name in _root_names(policy):
        root = Path(repository_root) / relative_root
        if root.is_symlink() or not root.is_dir():
            raise ToolIdentityPolicyError(f"受管根不存在或不可信：{relative_root}")
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.suffix not in suffixes:
                continue
            parts = path.relative_to(root).parts
            if EXCLUDED_PARTS.intersection(parts):
                continue
            digests[f"{name}/{path.relative_to(root).as_posix()}"] = _sha256(path.read_bytes())
    return digests


def layer_entries(policy: Mapping[str, Any], digests: Mapping[str, str]) -> dict[str, list[dict[str, str]]]:
    """按层分组（含 ignored 与 defaulted），口径与 Codex 引擎的 ``layer_entries`` 相同。"""

    return codex_identity.layer_entries(policy, [{"path": key, "sha256": digests[key]} for key in sorted(digests)])


# ---------------------------------------------------------------------------
# 模块索引与导入解析（覆盖两个包）
# ---------------------------------------------------------------------------


def _module_index(policy: Mapping[str, Any], digests: Mapping[str, str]) -> dict[str, str]:
    """模块全名（``tools.<根目录名>.<…>``）→ 条目键；包取其 ``__init__.py``。"""

    index: dict[str, str] = {}
    names = {name for _relative, name in _root_names(policy)}
    for key in digests:
        if not key.endswith(".py"):
            continue
        root_name, _, relative = key.partition("/")
        if root_name not in names:
            continue
        parts = relative[: -len(".py")].split("/")
        if parts[-1] == "__init__":
            parts = parts[:-1]
        index[".".join(["tools", root_name, *parts])] = key
    return index


def _package_of(module: str, index: Mapping[str, str]) -> str:
    key = index.get(module, "")
    return module if key.endswith("/__init__.py") else module.rpartition(".")[0]


def _resolve_imports(
    nodes: Iterable[ast.AST],
    module: str,
    index: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """导入语句 → (模块别名 → 模块全名, 符号别名 → (模块全名, 符号))。

    覆盖包形式、相对形式、脚本方式的扁平兄弟导入（按当前模块所在根解析），以及监督器为避开作业依赖分析而写的
    ``x = importlib.import_module("兄弟模块")``。
    """

    package = _package_of(module, index)
    root_package = ".".join(module.split(".")[:2])
    aliases: dict[str, str] = {}
    symbols: dict[str, tuple[str, str]] = {}
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package
                for _ in range(node.level - 1):
                    base = base.rpartition(".")[0]
                owner = ".".join(part for part in (base, node.module or "") if part)
            else:
                owner = node.module or ""
                if owner and owner not in index and f"{root_package}.{owner}" in index:
                    owner = f"{root_package}.{owner}"
            for alias in node.names:
                name = alias.asname or alias.name
                candidate = f"{owner}.{alias.name}" if owner else alias.name
                if candidate in index:
                    aliases[name] = candidate
                elif owner in index:
                    symbols[name] = (owner, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                full = alias.name
                if full not in index and f"{root_package}.{full}" in index:
                    full = f"{root_package}.{full}"
                if full in index:
                    aliases[alias.asname or alias.name.split(".")[0]] = full
        elif (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Attribute)
            and isinstance(node.value.func.value, ast.Name)
            and node.value.func.value.id == "importlib"
            and node.value.func.attr == "import_module"
            and len(node.value.args) == 1
            and isinstance(node.value.args[0], ast.Constant)
            and isinstance(node.value.args[0].value, str)
        ):
            target = node.value.args[0].value
            full = target if target in index else f"{root_package}.{target}"
            if full in index:
                aliases[node.targets[0].id] = full
    return aliases, symbols


def _top_level_binding_nodes(tree: ast.Module) -> list[ast.AST]:
    """模块顶层（含 if／try／with 块，不进函数与类）的导入语句与 ``x = importlib.import_module(...)`` 赋值。"""

    found = list(codex_identity._top_level_import_nodes(tree))
    pending: list[ast.AST] = list(tree.body)
    while pending:
        node = pending.pop()
        if isinstance(node, ast.Assign):
            found.append(node)
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                pending.extend(getattr(node, field, []) or [])
        elif isinstance(node, ast.ExceptHandler):
            pending.extend(node.body)
    return found


# ---------------------------------------------------------------------------
# 闭包
# ---------------------------------------------------------------------------


def _bindings_for(policy: Mapping[str, Any]) -> Mapping[str, tuple[str, str | None]]:
    return upgrade_client_adapter.ADAPTER_BINDINGS[str(policy["client"])]


def _binding_module(module_name: str, index: Mapping[str, str]) -> str:
    """绑定表里的模块名（受管包内短名）→ 模块全名。"""

    for candidate in (module_name, f"tools.official_client_capture.{module_name}", f"tools.official_client_control.{module_name}"):
        if candidate in index:
            return candidate
    raise ToolIdentityPolicyError(f"适配器绑定指向的模块不在受管树内：{module_name}")


def closure(
    policy: Mapping[str, Any],
    repository_root: Path,
    digests: Mapping[str, str],
    roots: Iterable[tuple[str, str]],
    *,
    layer: str | None,
    barriers: Iterable[str] = (),
    bindings: Mapping[str, tuple[str, str | None]] | None = None,
) -> dict[str, Any]:
    """从根符号（模块全名, 符号）出发的跨模块按符号闭包。

    ``layer`` 为某层名时是编排器闭包口径（同层模块整文件、其他非 control 层不计入）；为 None 时是读侧闭包口径
    （非 control 层模块一律整文件）。``barriers`` 是「模块全名:符号」形式的屏障。
    """

    index = _module_index(policy, digests)
    active_bindings = bindings if bindings is not None else _bindings_for(policy)
    orchestrator_module = _module_of_key(str(policy["orchestrator"]["file"]), index)
    adapter_module = next((module for module in index if module.endswith(f".{ADAPTER_MODULE}")), None)
    dynamic_policy = {"orchestrator": {"dynamic_call_names": list(policy["orchestrator"]["dynamic_call_names"])}}
    stop = set(barriers)
    if adapter_module is not None:
        stop |= {f"{adapter_module}:{name}" for name in ADAPTER_MECHANISM_BARRIERS}
    parsed: dict[str, tuple[Any, ...]] = {}
    symbols: dict[tuple[str, str], dict[str, str]] = {}
    whole: dict[str, dict[str, str]] = {}
    pending: list[tuple[str, str]] = []

    def module_info(module: str) -> tuple[Any, ...]:
        if module not in parsed:
            key = index[module]
            source, tree, functions, constants, lines = codex_identity._reader_parsed_module(
                str(_key_path(repository_root, policy, key)), digests[key]
            )
            aliases, symbol_imports = _resolve_imports(_top_level_binding_nodes(tree), module, index)
            parsed[module] = (source, tree, functions, constants, aliases, symbol_imports, lines)
        return parsed[module]

    def reject_ignored(module: str, owner: str) -> str | None:
        """模块所属的层；被策略忽略的文件不得出现在闭包里。"""

        key = index.get(module)
        if key is None:
            return None
        module_layer = classify_path(policy, key)
        if module_layer is None:
            raise ToolIdentityPolicyError(f"{owner} 引用了被策略忽略的文件 {key}：须在策略里归层，或改经适配器")
        return module_layer

    def whole_module(module: str, owner: str) -> None:
        key = index.get(module)
        if key is not None and module not in whole:
            reject_ignored(module, owner)
            whole[module] = {"module": module, "path": key, "sha256": digests[key]}

    def refer(module: str, name: str, owner: str) -> None:
        module_layer = reject_ignored(module, owner)
        if module_layer is None:
            return
        if module == orchestrator_module or module_layer == "control":
            pending.append((module, name))
        elif layer is None or module_layer == layer:
            whole_module(module, owner)

    for root in roots:
        pending.append(tuple(root))  # type: ignore[arg-type]
    while pending:
        module, name = pending.pop()
        if (module, name) in symbols or f"{module}:{name}" in stop:
            continue
        if module not in index:
            continue
        source, tree, functions, constants, aliases, symbol_imports, lines = module_info(module)
        node = functions.get(name) or constants.get(name)
        if node is None:
            if module == orchestrator_module:
                raise ToolIdentityPolicyError(f"闭包根或引用的编排器符号不存在：{name}")
            whole_module(module, f"{module}.{name}")
            continue
        owner = f"{module}.{name}"
        kind = "function" if name in functions else "constant"
        symbols[(module, name)] = {
            "module": module,
            "name": name,
            "kind": kind,
            "sha256": _sha256(codex_identity._reader_source_segment(source, lines, node).encode("utf-8")),
        }
        local_aliases, local_symbols = _resolve_imports(
            [child for child in ast.walk(node) if isinstance(child, (ast.Import, ast.ImportFrom, ast.Assign))], module, index
        )
        scope_aliases = {**aliases, **local_aliases}
        scope_symbols = {**symbol_imports, **local_symbols}
        if kind == "function":
            codex_identity._reject_dynamic(node, owner, dynamic_policy, scope_aliases, functions)
        _reject_non_literal_orchestrator(node, owner)
        handled: set[int] = set()
        parents = {id(child): parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
        for child in ast.walk(node):
            if not isinstance(child, ast.Attribute):
                continue
            if isinstance(child.value, ast.Attribute) and child.value.attr == "orchestrator":
                binding = active_bindings.get(child.attr)
                if binding is None or child.attr not in upgrade_client_adapter.ORCHESTRATOR_SURFACE or binding[1] is None:
                    raise ToolIdentityPolicyError(f"{owner} 经适配器访问的编排器符号 {child.attr} 不在绑定表")
                refer(_binding_module(binding[0], index), binding[1], owner)
                handled.update({id(child), id(child.value)})
            elif isinstance(child.value, ast.Name) and child.value.id in scope_aliases:
                continue
            elif child.attr in upgrade_client_adapter.ADAPTER_MEMBERS and child.attr != "orchestrator":
                binding = active_bindings[child.attr]
                target_module = _binding_module(binding[0], index)
                if binding[1] is not None:
                    refer(target_module, binding[1], owner)
                else:
                    parent = parents.get(id(child))
                    if isinstance(parent, ast.Attribute) and parent.value is child:
                        refer(target_module, parent.attr, owner)
                        handled.add(id(parent))
                    else:
                        whole_module(target_module, owner)
                handled.add(id(child))
            elif child.attr in ADAPTER_METHODS and adapter_module is not None and id(child) not in handled:
                refer(adapter_module, "ClientAdapter", owner)
        attribute_bases: set[int] = set()
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Attribute)
                and id(child) not in handled
                and isinstance(child.value, ast.Name)
                and child.value.id in scope_aliases
                and child.value.id not in functions
                and child.value.id not in constants
            ):
                attribute_bases.add(id(child.value))
                refer(scope_aliases[child.value.id], child.attr, owner)
        for child in ast.walk(node):
            if not isinstance(child, ast.Name) or id(child) in attribute_bases or child.id == name:
                continue
            if child.id in functions or child.id in constants:
                pending.append((module, child.id))
            elif child.id in scope_symbols:
                refer(*scope_symbols[child.id], owner)
            elif child.id in scope_aliases:
                whole_module(scope_aliases[child.id], owner)
    record = {
        "schema_version": CLOSURE_SCHEMA,
        "layer": layer or "reader",
        "roots": sorted(f"{module}:{name}" for module, name in roots),
        "barriers": sorted(stop),
        "symbols": [symbols[key] for key in sorted(symbols)],
        "modules": [whole[key] for key in sorted(whole)],
    }
    return {**record, "closure_sha256": codex_identity._fingerprint(record)}


def _reject_non_literal_orchestrator(node: ast.AST, owner: str) -> None:
    """``orchestrator`` 只许以 ``<表达式>.orchestrator.<名字>`` 字面访问；被赋值、传递或 ``getattr`` 都无法静态解析。"""

    parents = {id(child): parent for parent in ast.walk(node) for child in ast.iter_child_nodes(parent)}
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute) and child.attr == "orchestrator":
            parent = parents.get(id(child))
            if not (isinstance(parent, ast.Attribute) and parent.value is child):
                raise ToolIdentityPolicyError(f"{owner} 非字面使用 orchestrator 对象，闭包无法静态解析")


def _module_of_key(key: str, index: Mapping[str, str]) -> str:
    for module, candidate in index.items():
        if candidate == key:
            return module
    raise ToolIdentityPolicyError(f"编排器文件不在受管树内：{key}")


def _key_path(repository_root: Path, policy: Mapping[str, Any], key: str) -> Path:
    root_name, _, relative = key.partition("/")
    for relative_root, name in _root_names(policy):
        if name == root_name:
            return Path(repository_root) / relative_root / relative
    raise ToolIdentityPolicyError(f"条目键不属于任何受管根：{key}")


# ---------------------------------------------------------------------------
# 身份与评估器摘要
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _algorithm_sha256() -> str:
    adapter_source = Path(upgrade_client_adapter.__file__).read_bytes()
    return _sha256(Path(__file__).read_bytes() + b"\0" + adapter_source)


def _memoized(kind: str, policy: Mapping[str, Any], digests: Mapping[str, str], compute: Any) -> dict[str, Any]:
    entry = None
    if os.environ.get(codex_identity.IDENTITY_MEMO_ENV):
        try:
            entry = codex_identity._memo_entry(kind, {
                "claude_algorithm_sha256": _algorithm_sha256(),
                "policy": codex_identity._fingerprint(dict(policy)),
                "tree": codex_identity._fingerprint(dict(digests)),
            })
        except (OSError, TypeError, ValueError):
            entry = None
        cached = codex_identity._memo_load(entry)
        if cached is not None:
            return cached
    result = compute()
    codex_identity._memo_save(entry, result)
    return result


def _orchestrator_roots(policy: Mapping[str, Any], index: Mapping[str, str], names: Iterable[str]) -> list[tuple[str, str]]:
    module = _module_of_key(str(policy["orchestrator"]["file"]), index)
    return [(module, str(name)) for name in names]


def compute_identity(policy: Mapping[str, Any], repository_root: Path | None = None) -> dict[str, Any]:
    """按 v3 策略计算 Claude 工具身份：五摘要、层计数、默认层路径与两个编排器闭包。"""

    root = Path(repository_root) if repository_root is not None else REPOSITORY_ROOT
    digests = managed_tree_digests(root, policy)

    def compute() -> dict[str, Any]:
        grouped = layer_entries(policy, digests)
        index = _module_index(policy, digests)
        wire = closure(policy, root, digests, _orchestrator_roots(policy, index, policy["orchestrator"]["wire_roots"]), layer="wire_producer")
        evidence = closure(policy, root, digests, _orchestrator_roots(policy, index, policy["orchestrator"]["evidence_roots"]), layer="evidence_semantics")
        runtime = sorted((dict(item) for item in policy["runtime_bindings"]), key=lambda item: item["name"])
        return {
            "client": str(policy["client"]),
            "policy_version": int(policy["policy_version"]),
            "policy_sha256": str(policy["policy_sha256"]),
            "wire_producer_sha256": codex_identity._fingerprint({"entries": grouped["wire_producer"], "orchestrator_closure_sha256": wire["closure_sha256"]}),
            "evidence_semantics_sha256": codex_identity._fingerprint({"entries": grouped["evidence_semantics"], "orchestrator_closure_sha256": evidence["closure_sha256"]}),
            "control_sha256": codex_identity._fingerprint({"entries": grouped["control"], "runtime_bindings": runtime}),
            "tool_files_sha256": codex_identity._fingerprint({"entries": [{"path": key, "sha256": digests[key]} for key in sorted(digests)]}),
            "layer_counts": {name: len(grouped[name]) for name in (*LAYERS, "ignored", "defaulted")},
            "defaulted_paths": [item["path"] for item in grouped["defaulted"]],
            "orchestrator_closures": {
                "wire_producer": {"closure_sha256": wire["closure_sha256"], "symbol_count": len(wire["symbols"]), "modules": [item["module"] for item in wire["modules"]]},
                "evidence_semantics": {"closure_sha256": evidence["closure_sha256"], "symbol_count": len(evidence["symbols"]), "modules": [item["module"] for item in evidence["modules"]]},
            },
        }

    return _memoized("claude-identity-v3", policy, digests, compute)


def evaluator_dependency_digests(
    repository_root: Path | None = None,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    """Claude 评估器四项依赖摘要：checker／builder 整文件摘要与 compare／accept 读侧闭包摘要。

    与 Codex 引擎同名同返回键，监督器经 ``adapter.identity_policy.evaluator_dependency_digests()`` 无参调用。
    """

    root = Path(repository_root) if repository_root is not None else REPOSITORY_ROOT
    active = dict(policy) if policy is not None else load_policy()
    digests = managed_tree_digests(root, active)
    evaluator = active["evaluator"]

    def compute() -> dict[str, Any]:
        for key in (evaluator["checker"], evaluator["builder"]):
            if key not in digests:
                raise ToolIdentityPolicyError(f"评估器直接依赖不在受管树内：{key}")
        index = _module_index(active, digests)
        barriers = sorted(f"{_module_of_name(item, index)}" for item in active.get("reader_barriers", {}))
        compare = closure(active, root, digests, _orchestrator_roots(active, index, evaluator["compare_reader_roots"]), layer=None, barriers=barriers)
        accept = closure(active, root, digests, _orchestrator_roots(active, index, evaluator["accept_reader_roots"]), layer=None, barriers=barriers)
        return {
            "checker_sha256": digests[evaluator["checker"]],
            "builder_sha256": digests[evaluator["builder"]],
            "compare_reader_sha256": compare["closure_sha256"],
            "accept_reader_sha256": accept["closure_sha256"],
        }

    return _memoized("claude-evaluator-v3", active, digests, compute)


def _module_of_name(item: str, index: Mapping[str, str]) -> str:
    """策略屏障「模块短名或全名:符号」→「模块全名:符号」。"""

    module, _, symbol = item.partition(":")
    return f"{_binding_module(module, index)}:{symbol}"
