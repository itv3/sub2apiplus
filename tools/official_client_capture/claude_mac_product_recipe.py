"""严格校验离线私有启动计划；缺已审核模板或静态来源时拒绝。

这里只读调用方显式冻结的 task／recipe／官方支持附件，不创建目录、连接、
启动产品、读取环境或解引用 credential_ref。目标产品模板尚未现场审核，
生产模板集合保持空；fixture 模板只在测试作用域注入且不能取得 formal 权。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source


_RECIPE_SCHEMA = "claude-upgrade-client-isolation-recipe/v2"
_RECIPE_FIELDS = frozenset({"schema_version", "client_id", "deployment", "official_support",
                            "endpoint", "credential_ref", "private_session_root", "state_paths",
                            "recipe_id", "request_policy"})
_PLAN_FIELDS = frozenset({"client_id", "task_sha256", "recipe_sha256", "executable", "argv",
                         "env_names", "private_roots", "installation_mode", "official_support_bindings"})
_PRIVATE_ROOT_NAMES = frozenset({"home", "user_data", "extensions", "config"})


class ProductRecipeError(ValueError):
    """离线计划或冻结 recipe 字段、路径、制品／模板绑定不符。"""


class ProductRecipeUnavailable(ProductRecipeError):
    """目标模板或真实 provider 尚未审核，不能发布可用隔离方法。"""


@dataclass(frozen=True)
class _ReviewedLaunchTemplate:
    """静态源码审核的完整模板，不接受调用方扩白名单或动态 handler。"""

    recipe_id: str
    client_id: str
    source_scope: str
    artifact_identity_sha256: str
    official_support_sha256: str
    installation_mode: str
    argv_tokens: tuple[str, ...]
    env_names: tuple[str, ...]
    private_root_names: tuple[str, ...]


# 尚无经过当前目标产品、原生观测与隔离验收的模板。只能经后续受管源码审核
# 新增，不能从 JSON、环境、CLI、任意 provider 属性或用户计划加载模板。
_REVIEWED_TEMPLATES: tuple[_ReviewedLaunchTemplate, ...] = ()


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ProductRecipeError(message)


def _plain_absolute(value: Any, label: str) -> Path:
    _require(isinstance(value, str) and bool(value) and not any(ord(char) < 32 for char in value),
             f"{label}须为明确路径字符串")
    path = Path(value)
    _require(path.is_absolute() and ".." not in path.parts and str(path) == value,
             f"{label}须为规范绝对路径")
    c.require_plain_path(path)
    return path


def _recipe_context(task: Mapping[str, Any], bundle_root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """复用现有 task 与附件读侧；这里只核结构，不认证产品隔离能力。"""
    frozen = source.validate_task_v2(task)
    root = _plain_absolute(str(bundle_root), "来源根")
    recipe = c.read_json(c.bound_file(root, frozen["execution"]["isolation_recipe"]))
    _require(set(recipe) == _RECIPE_FIELDS and recipe["schema_version"] == _RECIPE_SCHEMA,
             "recipe须为现有v2精确字段，不能降级或附加权限旗标")
    execution = frozen["execution"]
    for field, expected in (("client_id", frozen["client_id"]), ("deployment", execution["source_deployments"]["mac"]),
                            ("endpoint", execution["endpoint"]), ("credential_ref", execution["credential_ref"]),
                            ("request_policy", execution["request_policy"])):
        _require(recipe[field] == expected, f"recipe的{field}与冻结任务不符")
    source._file_binding(recipe["official_support"], "官方支持")
    c.bound_file(root, recipe["official_support"])
    session = _plain_absolute(recipe["private_session_root"], "私有会话根")
    _require(session.is_relative_to(root.parent) and session != root.parent
             and not session.is_relative_to(root) and not root.is_relative_to(session),
             "私有会话根须在本run中，且与原件根完全分离")
    paths = recipe["state_paths"]
    _require(isinstance(paths, list) and bool(paths) and len(paths) <= 32,
             "recipe须冻结有界状态路径全集")
    normalized = [_plain_absolute(value, "冻结状态路径") for value in paths]
    _require(len(set(normalized)) == len(normalized), "冻结状态路径重复")
    _require(isinstance(recipe["recipe_id"], str) and bool(c.ID_RE.fullmatch(recipe["recipe_id"])),
             "recipe ID非法")
    return frozen, recipe


def _artifact_identity(task: Mapping[str, Any]) -> str:
    """只使用冻结制品字段；文件存在或读时磁盘摘要不算实际产品参与。"""
    rows = [{"role": artifact["role"], "kind": artifact["kind"], "product": artifact["product"],
             "version": artifact["version"], "platform": artifact["platform"],
             "content_kind": artifact["content"]["kind"], "content_sha256": artifact["content"]["sha256"]}
            for artifact in task["client_artifacts"]]
    return c.digest(sorted(rows, key=lambda value: value["role"]))


def _template(task: Mapping[str, Any], recipe: Mapping[str, Any]) -> _ReviewedLaunchTemplate:
    candidates = [template for template in _REVIEWED_TEMPLATES
                  if template.recipe_id == recipe["recipe_id"] and template.client_id == task["client_id"]
                  and template.source_scope == task["execution"]["source_scope"]]
    if len(candidates) != 1:
        raise ProductRecipeUnavailable("当前客户端／版本／用途没有唯一已审核模板，隔离能力保持未知")
    template = candidates[0]
    _validate_template(task, recipe, template)
    return template


def _validate_template(task: Mapping[str, Any], recipe: Mapping[str, Any],
                       template: _ReviewedLaunchTemplate) -> None:
    """所有入口共用模板绑定与闭集检查，诊断模板不能扩宽正式合同。"""
    _require(template.client_id == task["client_id"] and template.recipe_id == recipe["recipe_id"]
             and template.source_scope == task["execution"]["source_scope"], "模板客户端／recipe／用途不符")
    _require(template.artifact_identity_sha256 == _artifact_identity(task)
             and template.official_support_sha256 == recipe["official_support"]["sha256"],
             "制品／版本或官方支持与已审核模板不符")
    _require(template.installation_mode == "standard" and bool(template.private_root_names)
             and len(set(template.private_root_names)) == len(template.private_root_names)
             and set(template.private_root_names) <= _PRIVATE_ROOT_NAMES,
             "已审核模板安装模式或私有路径集合非法；portable不能沿用普通安装参数")
    _require(len(set(template.env_names)) == len(template.env_names), "模板环境名称重复")


def validate_private_launch_plan(task: Mapping[str, Any], plan: Mapping[str, Any],
                                 *, bundle_root: Path) -> dict[str, Any]:
    """只读校验审核模板派生的完整 argv／目录／环境名称，不授启动权。

    argv中只有静态模板字面和固定目录占位符；不接收任意shell、handler或用户
    白名单。env_names只含名称，没有环境值，也不读取当前os.environ。
    """
    _require(isinstance(plan, Mapping) and set(plan) == _PLAN_FIELDS, "离线启动计划字段闭集非法")
    frozen, recipe = _recipe_context(task, bundle_root)
    template = _template(frozen, recipe)
    return _validate_private_launch_plan(frozen, recipe, plan, template)


def _validate_private_launch_plan(frozen: Mapping[str, Any], recipe: Mapping[str, Any],
                                  plan: Mapping[str, Any],
                                  template: _ReviewedLaunchTemplate) -> dict[str, Any]:
    """正式模板入口和实读物料诊断入口共用同一份计划校验，不接收动态权限。"""
    _require(isinstance(plan, Mapping) and set(plan) == _PLAN_FIELDS, "离线启动计划字段闭集非法")
    _validate_template(frozen, recipe, template)
    execution = frozen["execution"]
    _require(plan["client_id"] == frozen["client_id"] and plan["task_sha256"] == frozen["task_sha256"]
             and plan["recipe_sha256"] == execution["isolation_recipe"]["sha256"], "计划没有绑定准确task／recipe原件")
    _require(plan["installation_mode"] == template.installation_mode, "实际安装模式没有按模板冻结或portable覆盖参数")
    artifact = next(row for row in frozen["client_artifacts"] if row["role"] == "application_executable")
    executable = _plain_absolute(artifact["content"]["path"], "冻结应用executable")
    _require(plan["executable"] == str(executable), "不得用另装CLI或任意shell替换真实应用executable")
    session = Path(recipe["private_session_root"])
    roots = {name: str(session / name) for name in template.private_root_names}
    _require(isinstance(plan["private_roots"], Mapping) and dict(plan["private_roots"]) == roots,
             "私有路径必须恰好来自审核模板与本run会话根")
    for value in roots.values():
        _plain_absolute(value, "私有子根")
    substitutions = {"{executable}": str(executable), "{private_session_root}": str(session)}
    substitutions.update({"{" + name + "}": value for name, value in roots.items()})
    argv = []
    for token in template.argv_tokens:
        _require(isinstance(token, str) and bool(token) and not any(ord(char) < 32 for char in token),
                 "模板argv字面非法")
        if "{" in token or "}" in token:
            _require(token in substitutions, "模板含未审核的占位符或内嵌表达式")
            argv.append(substitutions[token])
        else:
            _require(not any(char in token for char in ";|`$"), "模板不接受shell表达式")
            argv.append(token)
    _require(bool(argv) and argv[0] == str(executable), "模板argv须以冻结应用executable开始")
    _require(isinstance(plan["argv"], list) and plan["argv"] == argv, "计划argv与审核模板不符，不能加参数或handler")
    _require(isinstance(plan["env_names"], list) and plan["env_names"] == list(template.env_names),
             "环境名称须恰好为模板固定集合，不能传值或扩白名单")
    _require(plan["official_support_bindings"] == [recipe["official_support"]],
             "计划没有绑定准确的官方支持原件")
    return {"source_scope": "readonly_diagnostic", "registration_verified": False,
            "formal_ready": False, "launch_allowed": False, "template_matched": True,
            "plan": copy.deepcopy(dict(plan)), "recipe": copy.deepcopy(recipe)}


def derive_readonly_private_launch_plan(task: Mapping[str, Any], *, bundle_root: Path,
                                       materials_receipt: Path) -> dict[str, Any]:
    """从实读公开产品物料派生固定诊断计划，保持正式模板与来源登记表关闭。

    仅接受regression_fixture。环境名称和值都由本run私有根确定，不从调用方
    JSON、当前环境或credential_ref取值。这里只形成计划，现场隔离和真实CLI
    参与仍须由独立观测与原有正式资格合同证明。
    """
    from tools.official_client_capture import claude_mac_product_materials as materials

    frozen, recipe = _recipe_context(copy.deepcopy(dict(task)), bundle_root)
    _require(frozen["execution"]["source_scope"] == "regression_fixture",
             "真实产品物料诊断不能给formal任务授予隔离资格")
    _require(Path(materials_receipt).parent == Path(bundle_root), "物料归档须恰好属于本task来源根")
    material = materials.read_product_materials(Path(materials_receipt))
    _require(material["client_id"] == frozen["client_id"]
             and material["client_artifacts"] == frozen["client_artifacts"],
             "实读产品物料与冻结task的客户端／制品／独立版本不一致")
    _require(material["official_support"] == recipe["official_support"],
             "recipe须绑定实读产品公开源码支持原件")
    desktop = frozen["client_id"] == "claude-desktop"
    root_names = ("home", "user_data", "config") if desktop else ("home", "user_data", "extensions", "config")
    env_names = ("HOME", "CLAUDE_USER_DATA_DIR", "CLAUDE_CONFIG_DIR") if desktop else ("HOME", "CLAUDE_CONFIG_DIR")
    tokens = (("{executable}",) if desktop else
              ("{executable}", "--user-data-dir", "{user_data}", "--extensions-dir", "{extensions}", "--new-window"))
    template = _ReviewedLaunchTemplate(
        recipe_id=recipe["recipe_id"], client_id=frozen["client_id"], source_scope="regression_fixture",
        artifact_identity_sha256=_artifact_identity(frozen),
        official_support_sha256=recipe["official_support"]["sha256"], installation_mode="standard",
        argv_tokens=tokens, env_names=env_names, private_root_names=root_names)
    executable = next(row["content"]["path"] for row in frozen["client_artifacts"] if row["role"] == "application_executable")
    roots = {name: str(Path(recipe["private_session_root"]) / name) for name in root_names}
    replacements = {"{executable}": executable, **{"{" + name + "}": value for name, value in roots.items()}}
    plan = {"client_id": frozen["client_id"], "task_sha256": frozen["task_sha256"],
            "recipe_sha256": frozen["execution"]["isolation_recipe"]["sha256"], "executable": executable,
            "argv": [replacements.get(token, token) for token in tokens], "env_names": list(env_names),
            "private_roots": roots, "installation_mode": "standard",
            "official_support_bindings": [recipe["official_support"]]}
    result = _validate_private_launch_plan(frozen, recipe, plan, template)
    result["derivation"] = "public_materials_readonly"
    result["materials_receipt_sha256"] = material["receipt_sha256"]
    result["environment"] = {"HOME": roots["home"], "CLAUDE_CONFIG_DIR": roots["config"]}
    if desktop:
        result["environment"]["CLAUDE_USER_DATA_DIR"] = roots["user_data"]
    result["private_user_data_settings"] = ({} if desktop else {
        "claudeCode.environmentVariables": [{"name": name, "value": result["environment"][name]}
                                            for name in ("HOME", "CLAUDE_CONFIG_DIR")]})
    return result


def check_recipe_qualification(task: Mapping[str, Any], recipe: Mapping[str, Any],
                               *, bundle_root: Path, provider: Any = None) -> None:
    """只经现有静态来源核隔离资格；任意对象／旗标不能替代来源provider。

    provider必须是SOURCE_ADAPTERS中本task绑定的同一个静态来源适配器，最终
    仍由现有verify_isolation_recipe_v2消费原件。本批没有正式模板／provider，
    故本函数不能发布可用recipe；fixture永远不能取得formal资格。
    """
    frozen, actual = _recipe_context(task, bundle_root)
    _require(isinstance(recipe, Mapping) and dict(recipe) == actual, "资格核验须消费准确recipe原件")
    if frozen["execution"]["source_scope"] != "formal":
        raise ProductRecipeUnavailable("回归fixture不能认证真实产品隔离资格")
    adapter = c.SOURCE_ADAPTERS.get(frozen["execution"]["recorder_id"])
    if adapter is None or provider is not adapter or not callable(getattr(adapter, "verify_isolation_recipe", None)):
        raise ProductRecipeUnavailable("缺同task已登记的静态真实provider，隔离能力保持未知")
    _template(frozen, actual)
    verified = source.verify_isolation_recipe_v2(frozen, Path(bundle_root))
    _require(verified == actual, "正式来源验证后的recipe与冻结原件不同")
