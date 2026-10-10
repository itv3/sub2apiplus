"""严格校验离线私有启动计划；缺已审核模板或静态来源时拒绝。

这里只读调用方显式冻结的 task／recipe／官方支持附件，不创建目录、连接、
启动产品、读取环境或解引用 credential_ref。目标产品模板尚未现场审核，
生产模板集合保持空；fixture 模板只在测试作用域注入且不能取得 formal 权。
"""

from __future__ import annotations

import copy
import hashlib
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


def physical_session_root(task: Mapping[str, Any], recipe: Mapping[str, Any], bundle_root: Path,
                          path_context: Any = None) -> Path:
    """仅受管固定桥能映射逻辑根；任意 dict 或路径前缀不能授跨机映射权。"""
    if path_context is None:
        return _plain_absolute(recipe["private_session_root"], "私有会话根")
    try:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge
    except ImportError as error:
        raise ProductRecipeUnavailable("受管跨机路径桥尚未安装，禁止字典替代") from error
    physical = bridge.validate_bridge_path_context(task, path_context, bundle_root=Path(bundle_root))
    _require(isinstance(physical, Path) and physical.is_absolute(), "固定桥未返回实际 Mac 私有根")
    return _plain_absolute(str(physical), "实际 Mac 私有根")


def material_state_paths(task: Mapping[str, Any], recipe: Mapping[str, Any], bundle_root: Path,
                         path_context: Any = None) -> tuple[Path, ...]:
    """原冻结状态路径全集经过认证映射，不重写 recipe 或扩大状态范围。"""
    if path_context is not None:
        from tools.official_client_capture import claude_mac_agent_bridge as bridge
        bridge.validate_bridge_path_context(task, path_context, bundle_root=Path(bundle_root))
        mapping = dict(path_context.state_paths)
        paths = recipe["state_paths"]
        _require(isinstance(paths, list) and 0 < len(paths) <= 32
                 and len(set(paths)) == len(paths) and set(paths) == set(mapping)
                 and recipe["private_session_root"] == str(path_context.logical_session_root),
                 "状态全集与原认证映射／逻辑会话根不一致")
        # 固定桥已经认证规范绝对路径。离线 ARM64 重放不能再读取 Mac 上的
        # 活动目录，更不能按 session 前缀推导一份未经认证的物理位置。
        return tuple(Path(mapping[value]) for value in paths)
    logical = Path(recipe["private_session_root"])
    physical = physical_session_root(task, recipe, bundle_root, path_context)
    result = []
    for value in recipe["state_paths"]:
        path = _plain_absolute(value, "原冻结状态路径")
        _require(path.is_relative_to(logical) and path != logical, "状态路径越出原冻结私有根")
        result.append(physical / path.relative_to(logical))
    _require(len(set(result)) == len(result), "实际映射状态路径重复")
    return tuple(result)


def material_state_root(task: Mapping[str, Any], recipe: Mapping[str, Any], bundle_root: Path,
                        path_context: Any = None) -> Path:
    """从准确物理全集派生唯一恢复根；应用会话根与原状态根分别消费。

    桥分支只用经过认证的 physical pair，不能由调用方指定任意公共根。
    这里只推导字面，现场工厂／journal仍核当前用户私有目录、原实例、同卷
    与精确路径上限；离线原件重放不要求 Mac 目录仍存在。
    """
    if path_context is None:
        return physical_session_root(task, recipe, bundle_root)
    paths = material_state_paths(task, recipe, bundle_root, path_context)
    root = paths[0].parent
    while any(path == root or not path.is_relative_to(root) for path in paths):
        root = root.parent
    _require(len(root.parts) > 2, "认证状态全集不能以系统公共根作为恢复根")
    for private in (path_context.staging_bundle_root, path_context.staging_session_root):
        _require(not root.is_relative_to(private) and not private.is_relative_to(root),
                 "认证状态恢复根须与来源包和应用会话完全分离")
    return root


def _recipe_context(task: Mapping[str, Any], bundle_root: Path, path_context: Any = None) -> tuple[dict[str, Any], dict[str, Any]]:
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
    physical_session_root(frozen, recipe, root, path_context)
    logical_root = root if path_context is None else path_context.logical_bundle_root
    _require(session.is_relative_to(logical_root.parent) and session != logical_root.parent
             and not session.is_relative_to(logical_root) and not logical_root.is_relative_to(session),
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
                                  template: _ReviewedLaunchTemplate, *, bundle_root: Path | None = None,
                                  path_context: Any = None) -> dict[str, Any]:
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
    session = (Path(recipe["private_session_root"]) if path_context is None
               else physical_session_root(frozen, recipe, bundle_root, path_context))
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
    frozen, recipe = _recipe_context(copy.deepcopy(dict(task)), bundle_root)
    _require(frozen["execution"]["source_scope"] == "regression_fixture",
             "真实产品物料诊断不能给formal任务授予隔离资格")
    return derive_material_private_launch_plan(frozen, bundle_root=bundle_root, materials_receipt=materials_receipt)


def derive_material_private_launch_plan(task: Mapping[str, Any], *, bundle_root: Path,
                                       materials_receipt: Path, path_context: Any = None) -> dict[str, Any]:
    return _derive_material_plan(task, bundle_root=bundle_root, materials_receipt=materials_receipt,
                                 path_context=path_context, archived=False)


def derive_archived_private_launch_plan(task: Mapping[str, Any], *, bundle_root: Path,
                                        materials_receipt: Path, path_context: Any = None) -> dict[str, Any]:
    """离线重验产品固定配方；返回只读计划，不能据此启动或登记来源。"""
    return _derive_material_plan(task, bundle_root=bundle_root, materials_receipt=materials_receipt,
                                 path_context=path_context, archived=True)


def _derive_material_plan(task: Mapping[str, Any], *, bundle_root: Path,
                          materials_receipt: Path, path_context: Any, archived: bool) -> dict[str, Any]:
    """从本 task 的实读产品原件推导固定计划；formal 也只获得只读计划。

    不靠全局模板名单硬编码某次安装摘要。运行期仍须固定受管 authority、独立
    捕获及来源适配器的实际资格；本函数所有权限旗标保持 false，不读取凭据。
    """
    from tools.official_client_capture import claude_mac_product_materials as materials

    frozen, recipe = _recipe_context(copy.deepcopy(dict(task)), bundle_root, path_context)
    _require(Path(materials_receipt).parent == Path(bundle_root), "物料归档须恰好属于本task来源根")
    material = (materials.read_product_material_archive(task, Path(materials_receipt)) if archived
                else materials.read_product_materials(Path(materials_receipt)))
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
    if not desktop and material["facts"].get("vscode_activation_source") == {
            "event_name": "extensionActivationTimes", "success_outcome": "success", "requires_log_level": "trace",
            "originals": ["code-extension-host.js", "code-telemetry-service.js"]}:
        # 只给实际源码已支持的产品版本开启固定日志参数；变动形成新的冻结
        # recipe/task。参数仍来自静态闭集，调用方不能随意追加 inspector/shell。
        tokens = (*tokens, "--log", "trace")
    template = _ReviewedLaunchTemplate(
        recipe_id=recipe["recipe_id"], client_id=frozen["client_id"], source_scope=frozen["execution"]["source_scope"],
        artifact_identity_sha256=_artifact_identity(frozen),
        official_support_sha256=recipe["official_support"]["sha256"], installation_mode="standard",
        argv_tokens=tokens, env_names=env_names, private_root_names=root_names)
    executable = next(row["content"]["path"] for row in frozen["client_artifacts"] if row["role"] == "application_executable")
    session = physical_session_root(frozen, recipe, bundle_root, path_context)
    roots = {name: str(session / name) for name in root_names}
    replacements = {"{executable}": executable, **{"{" + name + "}": value for name, value in roots.items()}}
    plan = {"client_id": frozen["client_id"], "task_sha256": frozen["task_sha256"],
            "recipe_sha256": frozen["execution"]["isolation_recipe"]["sha256"], "executable": executable,
            "argv": [replacements.get(token, token) for token in tokens], "env_names": list(env_names),
            "private_roots": roots, "installation_mode": "standard",
            "official_support_bindings": [recipe["official_support"]]}
    result = _validate_private_launch_plan(frozen, recipe, plan, template, bundle_root=bundle_root, path_context=path_context)
    result["derivation"] = "public_materials_readonly"
    result["materials_receipt_sha256"] = material["receipt_sha256"]
    result["environment"] = {"HOME": roots["home"], "CLAUDE_CONFIG_DIR": roots["config"]}
    if desktop:
        result["environment"]["CLAUDE_USER_DATA_DIR"] = roots["user_data"]
    result["private_user_data_settings"] = ({} if desktop else {
        "claudeCode.environmentVariables": [{"name": name, "value": result["environment"][name]}
                                            for name in ("HOME", "CLAUDE_CONFIG_DIR")]})
    return result


def validate_material_private_launch_plan(task: Mapping[str, Any], plan: Mapping[str, Any],
                                         *, bundle_root: Path, materials_receipt: Path, path_context: Any = None) -> dict[str, Any]:
    """逐字核 material 派生计划，禁止外部追加 argv、环境名、portable 或替代 CLI。"""
    actual = derive_material_private_launch_plan(task, bundle_root=bundle_root, materials_receipt=materials_receipt,
                                                path_context=path_context)
    _require(isinstance(plan, Mapping) and dict(plan) == actual["plan"], "启动计划不是当前实读物料的固定私有计划")
    return actual


def stage_private_product_materials(materials_receipt: Path, *, private_session_root: Path,
                                   output_root: Path) -> dict[str, Any]:
    """在冻结新 task 前复制公开产品依赖，然后实读复制品生成新物料原件。

    VS Code 的 --extensions-dir 不能指向空目录同时宣称全局扩展被加载；Desktop
    的私有 userData 也必须含 pin 指定的内置 CLI 缓存。只复制已核公共制品，不
    读用户 HOME、账号或凭据，不改旧 task／material／Catalog，不启动任何产品。
    """
    import os
    import stat
    from tools.official_client_capture import claude_mac_product_materials as materials

    frozen = materials.read_product_materials(Path(materials_receipt))
    session = _plain_absolute(str(private_session_root), "新私有会话根")
    output = _plain_absolute(str(output_root), "新物料根")
    _require(not session.exists() and not output.exists() and not session.is_relative_to(output)
             and not output.is_relative_to(session), "只允许新建彼此分离的私有会话与物料根")
    for value in (session.parent, output.parent):
        info = value.lstat()
        _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
                 and stat.S_IMODE(info.st_mode) == 0o700, "新根的父目录须为当前用户私有目录")
    roots = {name: Path(value) for name, value in frozen["roots"].items()}
    _require(all(not session.is_relative_to(value) and not output.is_relative_to(value)
                 for value in roots.values()), "不能在原产品树中生成私有状态或物料")
    session.mkdir(mode=0o700)
    for name in ("home", "config", "user_data", *(("extensions",) if frozen["client_id"] == "claude-vscode" else ())):
        (session / name).mkdir(mode=0o700)

    def copy_public_tree(original: Path, copied: Path) -> None:
        # 按已实读库存逐项复制。文件读侧使用 O_NOFOLLOW，不让复制中的竞态链接
        # 导向用户状态；合法树内链接只复制链接原文，不解引用。
        inventory, signatures = materials._tree(original)
        copied.mkdir(mode=0o700)
        for row in sorted(inventory["entries"], key=lambda item: (item["path"].count("/"), item["path"])):
            if row["path"] == ".":
                continue
            target = copied / row["path"]
            if row["kind"] == "directory":
                target.mkdir(mode=0o700)
            elif row["kind"] == "symlink":
                target.symlink_to(row["target"])
            else:
                reader = materials._open(original, row["path"])
                fd = None
                try:
                    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                    before = os.fstat(reader)
                    _require(materials._signature(before) == signatures[row["path"]]
                             and stat.S_ISREG(before.st_mode), "公开文件读取前实例已变化")
                    digest, count = hashlib.sha256(), 0
                    while raw := os.read(reader, 1_048_576):
                        count += len(raw)
                        _require(count <= row["bytes"], "复制中的公开文件超过冻结长度")
                        digest.update(raw)
                        view = memoryview(raw)
                        while view:
                            written = os.write(fd, view)
                            _require(written > 0, "复制公开文件短写")
                            view = view[written:]
                    _require(count == row["bytes"] and digest.hexdigest() == row["sha256"]
                             and materials._signature(os.fstat(reader)) == materials._signature(before),
                             "复制时公开文件实例或字节漂移")
                    os.fchmod(fd, row["mode"])
                    os.fsync(fd)
                finally:
                    if fd is not None:
                        os.close(fd)
                    os.close(reader)
        for row in sorted(inventory["entries"], key=lambda item: -item["path"].count("/")):
            if row["kind"] == "directory":
                (copied if row["path"] == "." else copied / row["path"]).chmod(row["mode"])
        current, current_signatures = materials._tree(original)
        _require(current == inventory and current_signatures == signatures, "复制期间原产品树实例变化")
        copied_inventory, _ = materials._tree(copied)
        _require(copied_inventory["tree_sha256"] == inventory["tree_sha256"], "复制品树摘要与原产品不符")
    if frozen["client_id"] == "claude-vscode":
        original = roots["extension"]
        copied = session / "extensions" / original.name
        copy_public_tree(original, copied)
        staged = {"application_root": roots["application"], "extension_root": copied}
    else:
        original = roots["desktop_cli"]
        version = frozen["versions"]["embedded_cli"]
        copied = session / "user_data" / "claude-code" / version / original.name
        copied.parent.mkdir(parents=True, mode=0o700)
        copy_public_tree(original, copied)
        staged = {"application_root": roots["application"], "desktop_cli_root": copied}
    # 两套完整物料分别重读；复制期间源树漂移或复制品被置换不能只按当前摘要接受。
    current = materials.read_product_materials(Path(materials_receipt))
    _require(current == frozen, "复制依赖期间原产品物料漂移")
    result = materials.freeze_product_materials(frozen["client_id"], output_root=output, **staged)
    expected = {row["role"]: (row["kind"], row["version"], row["content"]["sha256"], row["content"]["bytes"])
                for row in frozen["client_artifacts"]}
    actual = {row["role"]: (row["kind"], row["version"], row["content"]["sha256"], row["content"]["bytes"])
              for row in result["client_artifacts"]}
    _require(actual == expected, "私有复制品的制品、独立版本或精确字节不符")
    return {"source_scope": "private_product_materials", "original_receipt_sha256": frozen["receipt_sha256"],
            "material": result, "private_session_root": str(session),
            "registration_verified": False, "formal_ready": False, "launch_allowed": False}


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
    adapter = c.source_adapter(frozen)
    if adapter is None or provider is not adapter or not callable(getattr(adapter, "verify_isolation_recipe", None)):
        raise ProductRecipeUnavailable("缺同task已登记的静态真实provider，隔离能力保持未知")
    _template(frozen, actual)
    verified = source.verify_isolation_recipe_v2(frozen, Path(bundle_root))
    _require(verified == actual, "正式来源验证后的recipe与冻结原件不同")
