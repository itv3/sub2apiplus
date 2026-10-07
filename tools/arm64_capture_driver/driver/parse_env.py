#!/usr/bin/env python3
"""每轮参数文件（$ARM64_VC_ENV）的安全解析器：绝不 source、绝不执行。

2026-09-22 审核 P1：lib.sh 原本用正则放行 ``KEY=.*`` 后直接 ``source``，``KEY=$(cmd)``／反引号／``;``
都会被 bash 执行。本解析器只接受：

* 注释行（``#`` 开头）与空行；
* ``KEY=VALUE`` 或 ``KEY="VALUE"``（整值一对双引号），必填键齐全，额外键只能来自 ``OPTIONAL_KEYS``；
* VALUE 里只允许引用**本文件前面已定义**的键（``$NAME`` 或 ``${NAME}``），由本解析器展开；任何其他 ``$``
  形态、反引号、``;``、``&``、``|``、``<``、``>``、``(``、``)``、``\\``、换行、回车一律拒绝。

输出：每键一行 ``export KEY='value'``（``shlex.quote``），lib.sh 只 ``eval`` 这些赋值。任何问题以退出码 2
失败并把原因写到 stderr（lib.sh 先捕获输出再 eval，失败即退出）。

Claude 升级 B5a-06 起按客户端解析：可选键 ``CLIENT``（缺省 codex）决定必填键、逐键格式核对与派生坐标，取值来自同目录的
客户端表 ``client_table.py``。目标客户端的二进制、摘要与账号键通用化为 ``CLIENT_BIN``／``CLIENT_BIN_SHA256``／
``CLIENT_ACCOUNT_ID``：Claude 只认通用键；Codex 的旧键 ``CODEX_BIN``／``CODEX_BIN_SHA256``／``CODEX_ACCOUNT_ID`` 照旧
有效，通用键在 Codex 参数文件里是它们的别名，解析后归一成旧名导出（各阶段脚本读旧名）。一个客户端的参数文件里出现另一
客户端的专属键即拒绝。改造前的 Codex 参数文件（没有 ``CLIENT``）解析与派生结果逐字不变；主程序只在末尾多输出新可选键
的 ``unset``（清掉外层环境残留的另一客户端参数）。
"""

from __future__ import annotations

import importlib.util
import re
import shlex
import sys
from pathlib import Path
from typing import Any

# Codex 的必填键（改造前的全集，顺序即缺键报错的顺序）。其它客户端的必填键由它去掉 Codex 专属键、加上该客户端的
# 专属键得到（``required_keys``；专属键见客户端表 exclusive_required_keys）。
REQUIRED_KEYS = (
    "ROUND", "STAMP", "D", "RUNROOT", "NEW", "IN", "UP", "CAND", "PREV_CANDIDATE", "HISTORY_TEST_TREE",
    "C", "DC", "RECEIPT", "BUNDLE", "BUNDLE_BRANCH", "OFFICIAL_CAMPAIGN", "OFFICIAL_STOP_LEDGER", "OFFICIAL_STOP_RECEIPT",
    "INPUT_RULE_MIGRATION", "INPUT_TARGET_SNAPSHOT", "PROJECT_DEADLINE_UTC", "STAGE_BUDGETS", "MIN_FREE_GIB",
    "FRONTEND_DEVIATION_APPROVED_BY", "PROFILE_ID", "PROFILE_DIGEST", "KILO_BIN", "KILO_VERSION", "KILO_SHA256",
    "COMPOSE_DIR", "COMPOSE_BACKUP", "PRODUCTION_IMAGE",
    "BASELINE_VERSION", "TARGET_VERSION", "TARGET_PROFILE_ID", "CODEX_BIN", "CODEX_BIN_SHA256",
    "OFFICIAL_ASSET_SHA256", "MAIN_MODEL", "LITE_MODEL", "CODEX_ACCOUNT_ID", "API_KEY_ID",
    "PREDECESSOR_CAMPAIGN", "POLICY_COMPAT_RECEIPT", "POLICY_ACTIVATION", "RELEASE_CERTIFICATION",
)
# 路径有统一默认布局，已有部署可显式覆盖；不可推导的身份必须在使用它的阶段提供。
OPTIONAL_KEYS = (
    # B 由本轮 Candidate 派生；保留旧参数作为相等断言，禁止借它选择另一候选树。
    "B",
    "BASELINE_SOURCE", "TARGET_SOURCE", "ACTIVE_PROFILE", "TARGET_PACKAGE", "TARGET_CODE_MODE_HOST_SHA256",
    "CAPTURE_RUNTIME_IMAGE", "PREVIOUS_POLICY", "PRE_A3_CERTIFICATION", "GATE_MAPPING_INPUT",
    "RETIRE_VERSION", "HISTORICAL_SOURCE_ROOT", "JWTGEN_BIN", "EVIDENCE_DECISION",
    # 入口编排器（E2-06）：入口门禁的源码坐标（部署到数据根的那一份工具提交），以及建账本之后各产物（计时账本、环境
    # 收据、预检 Campaign、Job 演练、启动探测）所在的根，默认就是数据根；验收演练放在数据根 staging 下的独立目录里。
    "ENTRY_BUNDLE", "ENTRY_BRANCH", "ENTRY_COMMIT", "ENTRY_ROOT",
    # P0 收据（E2-07 固定步骤）的回退依据：上一版本可回退点的收据文件（如前序 Campaign 的画像目录晋升收据）。
    "P0_ROLLBACK_EVIDENCE",
    # VC-5 补齐批准和 token 的最低剩余有效期；不继承外层残留批准。
    "VC5_ADMISSION_APPROVAL", "VC5_ADMIN_TOKEN_MIN_SECONDS",
    # B-10 只消费本轮显式请求；来源工作树／缓存须经过同提交与读集重放。
    "VC5_TARGET_REQUEST", "VC5_TARGET_TREE", "VC5_TARGET_PYCACHE", "VC5_TARGET_RECORD_STORE",
    # VC-4 只接受本轮显式网络模式和专项批准，不承接外层残留的 host 网络开关。
    "VC4_BUILD_NETWORK", "VC4_BUILD_NETWORK_APPROVAL",
    # B5a-06：本轮客户端与目标客户端的通用键。对 Codex 它们可缺省（通用键是旧键的别名），对 Claude 后三个必填（见
    # required_keys）；放在可选键里，没有出现时主程序照样 unset，外层环境残留的另一客户端参数不会被带进本轮。
    "CLIENT", "CLIENT_BIN", "CLIENT_BIN_SHA256", "CLIENT_ACCOUNT_ID",
)
ASSIGNMENT = re.compile(r"^([A-Z_][A-Z0-9_]*)=(.*)$")
REFERENCE = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)")
FORBIDDEN = set("`;&|<>()\\\r\n")


class EnvFileError(ValueError):
    pass


def client_table() -> Any:
    """同目录的客户端表 ``client_table.py``：按路径加载（不受当前目录与 PYTHONPATH 影响），各模块共用一个模块对象。

    驱动各处（本模块、入口步骤与编排器、入口门禁……）都以 ``client_table_sibling`` 登记同一份；``tools/ci`` 与驱动里的
    两份副本逐字相同，先加载的那份为准。只在用到时加载：导入本模块不读客户端表。"""

    name = "client_table_sibling"
    module = sys.modules.get(name)
    if module is not None:
        return module
    path = Path(__file__).resolve().with_name("client_table.py")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None or not path.is_file():
        raise EnvFileError(f"驱动缺少客户端表：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


def client_spec(values: dict[str, str]) -> Any:
    """参数所属客户端在客户端表里的整行；``CLIENT`` 不认识即拒绝。"""

    table = client_table()
    try:
        return table.spec(table.client_of(values))
    except table.ClientTableError as error:
        raise EnvFileError(str(error)) from error


def required_keys(client: str) -> tuple[str, ...]:
    """客户端的必填键：Codex 即 ``REQUIRED_KEYS``（逐字不变）；其它客户端去掉别的客户端的专属键、加上自己的专属键。"""

    table = client_table()
    own = table.spec(client)
    foreign = set(table.foreign_keys(client))
    return (*(key for key in REQUIRED_KEYS if key not in foreign),
            *(key for key in own["exclusive_required_keys"] if key not in REQUIRED_KEYS))


def stale_keys(exported: dict[str, str], client: str) -> list[str]:
    """本轮没有导出、要从调用方环境里清掉的键：可选键（原顺序）加上另一客户端的专属键。

    同一个 shell 先加载过别的参数文件时，上一轮的认证坐标、源码提交、身份参数会被悄悄带进本轮（10-02 E2-06 验收时，删掉
    的必填身份参数就是这样从外层环境继承回来的）；B5a-06 起另一客户端的参数（例如 Claude 轮次之后的 CODEX_BIN 残留）
    同理清掉。Codex 的另一客户端专属键都已在可选键里，清理集合只比改造前多出新增的四个可选键。"""

    keys = [key for key in OPTIONAL_KEYS if key not in exported]
    keys += [key for key in client_table().foreign_keys(client) if key not in exported and key not in keys]
    return keys


def plain_path(value: str, label: str) -> Path:
    """核对路径本身及所有已有父目录；不建目录、不解引用后再掩盖符号链接。"""

    path = Path(value)
    if not path.is_absolute() or str(path) != value or ".." in path.parts or path == Path("/"):
        raise EnvFileError(f"{label} 必须是规范绝对路径，且不得含父目录跳转")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise EnvFileError(f"{label} 不得含符号链接：{part}")
    return path


def coordinates(values: dict[str, str]) -> dict[str, str]:
    """初始化坐标只按显式选择派生，不宣称 Campaign 或候选已经存在、通过验收。"""

    data = plain_path(values["D"], "D")
    plain_path(values["RUNROOT"], "RUNROOT")
    campaign = data / "evidence" / "campaigns" / values["NEW"]
    candidate = data / "candidates" / values["CAND"]
    if "B" in values and values["B"] != str(candidate):
        raise EnvFileError("B 与本轮 D/candidates/CAND 不一致，拒绝旧候选路径")
    roots = {
        "B": str(candidate), "NEWDIR": str(campaign),
        "W": str(data / "control" / values["IN"]),
        "TOOLS": str(data / "tools" / "official_client_capture"),
        "L": str(data / "control" / f"{values['UP']}-timing-ledger"),
        "G": str(data / "control" / f"{values['NEW']}-candidate-gates"),
        "AS": str(data / "control" / f"{values['NEW']}-assertions"),
        "CAMPAIGN_CONTROL_ROOT": str(campaign / "control" / "vc"),
        "CAMPAIGN_RECEIPT_ROOT": str(campaign / "control" / "vc" / "receipts"),
        "CANDIDATE_EVIDENCE_ROOT": str(campaign / "candidates" / values["CAND"]),
        "CANDIDATE_RECEIPT_ROOT": str(campaign / "control" / "vc" / "receipts" / values["CAND"]),
    }
    for key, value in roots.items():
        path = plain_path(value, key)
        if path.exists() and not path.is_dir():
            raise EnvFileError(f"{key} 不是目录：{path}")
    roots.update(ROUND_CONTEXT_STATE="initialization_coordinates", CANDIDATE_REVISION="", CANDIDATE_REVISION_ROOT="")
    return roots


def parse_assignments(text: str, allowed_keys: tuple[str, ...] | list[str] | set[str] | frozenset[str]) -> dict[str, str]:
    """词法层：逐行解析 ``KEY=VALUE``，键必须在 ``allowed_keys`` 内，值按本模块规则拒绝命令形态并展开已定义键引用。

    驱动参数文件（``parse``）与修好接着跑轮次参数文件（``fix_and_continue.py``，第 35 项）共用这一层，保证两类
    参数文件是**同一安全解析**；必填键与逐键格式校验由各自调用方在其后完成。
    """

    allowed = frozenset(allowed_keys)
    values: dict[str, str] = {}
    for number, raw in enumerate(text.split("\n"), 1):
        line = raw.rstrip("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = ASSIGNMENT.match(line)
        if match is None:
            raise EnvFileError(f"第 {number} 行不是 KEY=VALUE 赋值")
        key, value = match.group(1), match.group(2)
        if key not in allowed:
            raise EnvFileError(f"第 {number} 行的键不在允许集合内：{key}")
        if key in values:
            raise EnvFileError(f"第 {number} 行重复定义：{key}")
        if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
            value = value[1:-1]
        elif '"' in value or "'" in value:
            raise EnvFileError(f"第 {number} 行的值含未闭合或内嵌引号：{key}")
        bad = sorted(ch for ch in set(value) if ch in FORBIDDEN)
        if bad:
            raise EnvFileError(f"第 {number} 行的值含禁止字符 {bad!r}：{key}")
        if "$" in value:
            stripped = REFERENCE.sub("", value)
            if "$" in stripped:
                raise EnvFileError(f"第 {number} 行的值含不允许的 $ 形态（只允许 $NAME／${{NAME}} 引用已定义键）：{key}")

            def lookup(found: re.Match[str]) -> str:
                name = found.group(1) or found.group(2)
                if name not in values:
                    raise EnvFileError(f"第 {number} 行引用了未定义（或后定义）的键：{name}")
                return values[name]

            value = REFERENCE.sub(lookup, value)
        if not value:
            raise EnvFileError(f"第 {number} 行的值为空：{key}")
        values[key] = value
    for key in ("VC5_TARGET_REQUEST", "VC5_TARGET_TREE", "VC5_TARGET_PYCACHE", "VC5_TARGET_RECORD_STORE"):
        if key in values and (not values[key].startswith("/") or ".." in values[key].split("/")):
            raise EnvFileError(f"{key} 必须是不含 .. 的绝对路径")
    return values


def _normalize_aliases(values: dict[str, str], aliases: Any) -> dict[str, str]:
    """把本客户端的别名键归一成规范键（Codex：通用键 → 旧键），保持参数文件里的先后位置；同一参数两处写了不同值即拒绝。"""

    for alias, canonical in aliases.items():
        if alias in values and canonical in values and values[alias] != values[canonical]:
            raise EnvFileError(f"{alias} 是 {canonical} 的别名，两处取值不同")
    normalized: dict[str, str] = {}
    for key, value in values.items():
        normalized.setdefault(aliases.get(key, key), value)
    return normalized


def parse(text: str) -> dict[str, str]:
    values = parse_assignments(text, (*REQUIRED_KEYS, *OPTIONAL_KEYS))
    # B5a-06：先定客户端（CLIENT 缺省 codex），再按客户端核对。没有 CLIENT、也没写通用键的 Codex 参数文件，下面每一步
    # 的结果与改造前相同（别名归一不改动、另一客户端专属键为空集、必填键与逐键核对的键和顺序不变）。
    spec = client_spec(values)
    values = _normalize_aliases(values, spec["key_aliases"])
    foreign = [key for key in client_table().foreign_keys(spec["client"]) if key in values]
    if foreign:
        raise EnvFileError(f"CLIENT={spec['client']} 的参数文件不得含其它客户端的专属键：{foreign}")
    missing = [key for key in required_keys(spec["client"]) if key not in values]
    if missing:
        raise EnvFileError(f"参数文件缺少键：{missing}")
    for key in ("C", "DC"):
        if not re.fullmatch(r"[0-9a-f]{40}", values[key]):
            raise EnvFileError(f"{key} 必须是完整 40 位小写 sha1")
    for key in ("BASELINE_VERSION", "TARGET_VERSION"):
        if not re.fullmatch(r"\d+\.\d+\.\d+", values[key]):
            raise EnvFileError(f"{key} 必须是明确的三段版本号")
    for key in (spec["binary_sha256_key"], "OFFICIAL_ASSET_SHA256", "PROFILE_DIGEST", *spec["extra_sha256_keys"]):
        if not re.fullmatch(r"[0-9a-f]{64}", values[key]):
            raise EnvFileError(f"{key} 必须是本轮审核的完整 SHA-256")
    if "TARGET_CODE_MODE_HOST_SHA256" in values and not re.fullmatch(r"[0-9a-f]{64}", values["TARGET_CODE_MODE_HOST_SHA256"]):
        raise EnvFileError("TARGET_CODE_MODE_HOST_SHA256 必须是本轮审核的完整 SHA-256")
    for key in (spec["account_key"], "API_KEY_ID"):
        if not re.fullmatch(r"[1-9][0-9]*", values[key]):
            raise EnvFileError(f"{key} 必须是本轮明确指定的正整数")
    if values["PROFILE_ID"] != values["TARGET_PROFILE_ID"]:
        raise EnvFileError("PROFILE_ID 与 TARGET_PROFILE_ID 不一致")
    for key in ("MAIN_MODEL", "LITE_MODEL", "TARGET_PROFILE_ID"):
        if not re.fullmatch(r"[A-Za-z0-9_.:/-]+", values[key]):
            raise EnvFileError(f"{key} 包含非法标识字符")
    for key in ("ROUND", "STAMP", "NEW", "IN", "UP", "CAND"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", values[key]):
            raise EnvFileError(f"{key} 必须是单层安全标识")
    if "RETIRE_VERSION" in values and not re.fullmatch(r"\d+\.\d+\.\d+", values["RETIRE_VERSION"]):
        raise EnvFileError("RETIRE_VERSION 必须是明确的三段版本号")
    if values.get("EVIDENCE_DECISION", "recapture") not in {"reuse", "recapture"}:
        raise EnvFileError("EVIDENCE_DECISION 只能是 reuse 或 recapture")
    if "ENTRY_COMMIT" in values and not re.fullmatch(r"[0-9a-f]{40}", values["ENTRY_COMMIT"]):
        raise EnvFileError("ENTRY_COMMIT 必须是完整 40 位小写 sha1")
    if "ENTRY_BRANCH" in values and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", values["ENTRY_BRANCH"]):
        raise EnvFileError("ENTRY_BRANCH 只允许字母、数字与 ._/-")
    if "ENTRY_BUNDLE" in values and not values["ENTRY_BUNDLE"].startswith("/"):
        raise EnvFileError("ENTRY_BUNDLE 必须是绝对路径")
    if "ENTRY_ROOT" in values:
        # 容器经 /capture 只看得到数据根；演练根只能放在数据根 staging 下（atomic-double 与发布认证的容器内重放要求）。
        root, data = values["ENTRY_ROOT"].rstrip("/"), values["D"].rstrip("/")
        if root != data and not (root.startswith(f"{data}/staging/") and ".." not in root.split("/")):
            raise EnvFileError("ENTRY_ROOT 只能是数据根 D，或数据根 staging 下的演练目录")
    if "P0_ROLLBACK_EVIDENCE" in values and (
        not values["P0_ROLLBACK_EVIDENCE"].startswith("/") or ".." in values["P0_ROLLBACK_EVIDENCE"].split("/")
    ):
        raise EnvFileError("P0_ROLLBACK_EVIDENCE 必须是不含 .. 的绝对路径")
    if "VC5_ADMISSION_APPROVAL" in values and (
        not values["VC5_ADMISSION_APPROVAL"].startswith("/") or ".." in values["VC5_ADMISSION_APPROVAL"].split("/")
    ):
        raise EnvFileError("VC5_ADMISSION_APPROVAL 必须是不含 .. 的绝对路径")
    if "VC5_ADMIN_TOKEN_MIN_SECONDS" in values and (
        not values["VC5_ADMIN_TOKEN_MIN_SECONDS"].isdigit() or int(values["VC5_ADMIN_TOKEN_MIN_SECONDS"]) < 1800
    ):
        raise EnvFileError("VC5_ADMIN_TOKEN_MIN_SECONDS 必须是至少 1800 的整数")
    if values.get("VC4_BUILD_NETWORK", "default") not in {"default", "none", "host"}:
        raise EnvFileError("VC4_BUILD_NETWORK 只能是 default、none 或 host")
    if "VC4_BUILD_NETWORK_APPROVAL" in values and (
        not values["VC4_BUILD_NETWORK_APPROVAL"].startswith("/") or ".." in values["VC4_BUILD_NETWORK_APPROVAL"].split("/")
    ):
        raise EnvFileError("VC4_BUILD_NETWORK_APPROVAL 必须是不含 .. 的绝对路径")
    # 先核对根路径；lib.sh 只有解析成功后才允许建立 RUNROOT。
    values["B"] = coordinates(values)["B"]
    return values


def derive(values: dict[str, str]) -> dict[str, str]:
    """只从已校验参数派生坐标；不自动选择账号、制品摘要或先前发布认证。

    随客户端变的派生（Campaign 与轮次前缀、清单文件名、候选生命周期目录、官方制品布局、pre-A3 认证文件名）取客户端表的
    模板，键与键的先后与改造前相同；Codex 的模板代入后与改造前写死的字面逐字相同。"""

    data, target, baseline = values["D"], values["TARGET_VERSION"], values["BASELINE_VERSION"]
    spec = client_spec(values)
    current, previous = client_table().format_fields(target), client_table().format_fields(baseline)
    tools = f"{data}/tools/official_client_capture"
    prefix = spec["campaign_prefix"].format(**current)
    certification = spec["pre_a3_certification_name"].format(stamp=values["STAMP"])
    result = {
        "CAMPAIGN_PREFIX": prefix, "TARGET_TAG": spec["target_tag"].format(**current),
        "RULES_JSON": f"{tools}/{spec['rules_manifest'].format(**current)}",
        "BASELINE_RULES_JSON": f"{tools}/{spec['rules_manifest'].format(**previous)}",
        "SCENARIOS_JSON": f"{tools}/{spec['scenarios_manifest'].format(**current)}",
        "BASELINE_SCENARIOS_JSON": f"{tools}/{spec['scenarios_manifest'].format(**previous)}",
        "EXPECTATIONS_JSON": f"{tools}/{spec['expectations_manifest'].format(**current)}",
        "PROFILE_PATCH_JSON": f"{tools}/{spec['profile_patch_manifest'].format(**current)}",
        "LIFECYCLE_DIR": spec["lifecycle_dir"].format(**current),
        "CANDIDATE_IMAGE_REPOSITORY": f"sub2apiplus-{prefix}-candidate",
        "BASELINE_SOURCE": f"{data}/{spec['official_source'].format(**previous)}",
        "TARGET_SOURCE": f"{data}/{spec['official_source'].format(**current)}",
        "TARGET_PACKAGE": f"{data}/{spec['official_package'].format(**current)}",
        "ACTIVE_PROFILE": f"{data}/control/{values['IN']}/baseline-profile.json",
        "PRE_A3_CERTIFICATION": f"{data}/control/policy-certification/{certification}",
        "HISTORICAL_SOURCE_ROOT": f"{data}/official/historical-gate-source",
        "JWTGEN_BIN": f"{data}/private-tools/jwtgen",
        "EVIDENCE_DECISION": "recapture",
        "ENTRY_ROOT": data,
        "VC4_BUILD_NETWORK": "default",
    }
    result.update({key: values[key] for key in OPTIONAL_KEYS if key in values})
    result.update(coordinates(values))
    return result


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("用法：parse_env.py <参数文件>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    try:
        if path.is_symlink() or not path.is_file():
            raise EnvFileError(f"参数文件不存在或不可信：{path}")
        values = parse(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, EnvFileError) as error:
        print(f"参数文件拒绝加载：{error}", file=sys.stderr)
        return 2
    exported = {**values, **derive(values)}
    for key, value in exported.items():
        sys.stdout.write(f"export {key}={shlex.quote(value)}\n")
    # 参数文件里没有、也没有派生默认值的可选键，以及另一客户端的专属键：清掉调用方环境里残留的旧值（见 stale_keys）。
    for key in stale_keys(exported, client_table().client_of(values)):
        sys.stdout.write(f"unset {key}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
