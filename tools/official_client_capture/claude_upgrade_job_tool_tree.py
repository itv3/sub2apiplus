"""Claude Job v2 的双根读集：冻结策略原字节、真实枚举与无 I/O 的形成期校验。

宿主与容器使用同一段有界脚本。忽略层文件也计入整树，只排除冻结后缀之外的文件
及登记的排除目录。本模块只读取源码，不执行被枚举工具或原生构建物。
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_job_rehearsal_receipt as shared

READ_CONTRACT_SCHEMA = "claude-job-tool-tree-read-contract/v1"
ROOTS = ("tools/official_client_capture", "tools/official_client_control")
REQUIRED_SUFFIXES = frozenset({".py", ".sh", ".json", ".mjs", ".m"})
EXCLUDED_PARTS = ("__pycache__", "tests", "versions")
TREE_NAMES = ("managed_host", "execution_host", "execution_container", "execution_synthetic_container")
MAX_POLICY_BYTES = 256 * 1024
MAX_TREE_ENTRIES = 20000
SHA256 = re.compile(r"^[0-9a-f]{64}$")

# 所有运行域使用同一源码，不导入项目模块、不写文件、不解释被枚举文件。
ENUMERATION_SCRIPT = r'''
import hashlib,json,pathlib,stat,sys
request=json.loads(sys.argv[1])
if set(request)!={"root","roots","suffixes","excluded_parts","read_contract_sha256"}:
    raise SystemExit("枚举请求字段不闭合")
root=pathlib.Path(request["root"])
if not root.is_absolute() or ".." in root.parts or root.is_symlink() or not root.is_dir():
    raise SystemExit("仓库根不存在或不可信")
suffixes=set(request["suffixes"]); excluded=set(request["excluded_parts"])
entries=[]; observations=[]
for relative_root in request["roots"]:
    relative=pathlib.PurePosixPath(relative_root)
    if relative.is_absolute() or ".." in relative.parts or str(relative)!=relative_root:
        raise SystemExit("受管根坐标非法")
    current=root.joinpath(*relative.parts)
    metadata=current.lstat()
    is_directory=stat.S_ISDIR(metadata.st_mode)
    is_symlink=stat.S_ISLNK(metadata.st_mode)
    observations.append({"path":str(current),"is_directory":is_directory,"is_symlink":is_symlink})
    if not is_directory or is_symlink:
        raise SystemExit("受管根不存在或是符号链接："+str(current))
    for path in sorted(current.rglob("*")):
        parts=path.relative_to(current).parts
        if excluded.intersection(parts):
            continue
        metadata=path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise SystemExit("读集中含符号链接："+str(path))
        if stat.S_ISREG(metadata.st_mode) and path.suffix in suffixes:
            entries.append({"path":relative.name+"/"+path.relative_to(current).as_posix(),
                            "sha256":hashlib.sha256(path.read_bytes()).hexdigest()})
            if len(entries)>20000:
                raise SystemExit("工具树条目超过上限")
entries.sort(key=lambda item:item["path"])
raw=json.dumps({"entries":entries},ensure_ascii=False,sort_keys=True,separators=(",",":")).encode("utf-8")
print(json.dumps({"root":str(root),"root_observations":observations,
                  "read_contract_sha256":request["read_contract_sha256"],
                  "entry_count":len(entries),"files_sha256":hashlib.sha256(raw).hexdigest(),
                  "entries":entries},ensure_ascii=False,sort_keys=True))
'''


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise shared.JobRehearsalReceiptError(message)


def _unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        _require(name not in result, "冻结策略含重复 JSON 键")
        result[name] = value
    return result


def _policy(raw: str, expected_sha256: str) -> dict[str, Any]:
    _require(isinstance(raw, str), "冻结策略原件必须是 UTF-8 字符串")
    try:
        encoded = raw.encode("utf-8")
    except UnicodeError as error:
        raise shared.JobRehearsalReceiptError("冻结策略原件不是 UTF-8") from error
    _require(0 < len(encoded) <= MAX_POLICY_BYTES, "冻结策略原件大小非法")
    _require(isinstance(expected_sha256, str) and SHA256.fullmatch(expected_sha256) is not None
             and hashlib.sha256(encoded).hexdigest() == expected_sha256, "冻结策略原字节摘要不一致")
    try:
        payload = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (ValueError, UnicodeError) as error:
        raise shared.JobRehearsalReceiptError("冻结策略原件不是合法 JSON") from error
    _require(isinstance(payload, dict) and payload.get("schema_version") == "tool-identity-policy/v3"
             and payload.get("client") == "claude", "冻结策略不是 Claude v3 合同")
    roots = payload.get("roots")
    suffixes = payload.get("suffixes")
    _require(isinstance(roots, list) and roots == list(ROOTS), "Claude v2 必须完整声明两个受管根")
    _require(isinstance(suffixes, list) and all(isinstance(item, str) for item in suffixes)
             and len(suffixes) == len(set(suffixes)) and REQUIRED_SUFFIXES.issubset(suffixes)
             and all(item.startswith(".") and "/" not in item and "\\" not in item for item in suffixes),
             "Claude v2 后缀必须完整包含 .m、.mjs 与三个基础后缀")
    return payload


def build_read_contract(policy_path: Path, expected_policy_sha256: str) -> dict[str, Any]:
    """从实际策略文件冻结原字节，拒绝用另一份策略构造更窄读集。"""
    path = Path(policy_path)
    _require(path.is_file() and not path.is_symlink(), "实际 Claude 策略文件不存在或不可信")
    try:
        raw = path.read_bytes().decode("utf-8")
    except UnicodeError as error:
        raise shared.JobRehearsalReceiptError("实际策略不是 UTF-8") from error
    policy = _policy(raw, expected_policy_sha256)
    return validate_read_contract({
        "schema_version": READ_CONTRACT_SCHEMA, "policy_json": raw,
        "policy_sha256": expected_policy_sha256, "roots": list(policy["roots"]),
        "suffixes": list(policy["suffixes"]), "excluded_parts": list(EXCLUDED_PARTS),
    }, expected_policy_sha256)


def validate_read_contract(value: Any, expected_policy_sha256: str) -> dict[str, Any]:
    """纯校验冻结策略原件与声明，不读取当前磁盘或身份引擎。"""
    contract = shared._expect(value, {
        "schema_version", "policy_json", "policy_sha256", "roots", "suffixes", "excluded_parts",
    }, "tool_tree_read_contract")
    _require(contract["schema_version"] == READ_CONTRACT_SCHEMA
             and contract["policy_sha256"] == expected_policy_sha256, "双根读集版本或策略绑定不一致")
    policy = _policy(contract["policy_json"], expected_policy_sha256)
    _require(contract["roots"] == policy["roots"] and contract["suffixes"] == policy["suffixes"]
             and contract["excluded_parts"] == list(EXCLUDED_PARTS), "读集声明缩窄或排除目录漂移")
    return dict(contract)


def _request(root: str, contract: Mapping[str, Any]) -> dict[str, Any]:
    scope = validate_read_contract(contract["tool_tree_read_contract"], str(contract["policy_sha256"]))
    _require(isinstance(root, str) and root.startswith("/") and "\\" not in root and "\x00" not in root
             and str(PurePosixPath(root)) == root and ".." not in PurePosixPath(root).parts, "观测根坐标非法")
    return {"root": root, "roots": scope["roots"], "suffixes": scope["suffixes"],
            "excluded_parts": scope["excluded_parts"], "read_contract_sha256": shared._fingerprint(scope)}


def _parse_observation(raw: str | bytes, contract: Mapping[str, Any], label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_pairs)
    except ValueError as error:
        raise shared.JobRehearsalReceiptError(f"{label}枚举未返回合法 JSON") from error
    return validate_tree(value, contract, label)


def observe_host_tree(tool_root: Path, contract: Mapping[str, Any]) -> dict[str, Any]:
    """宿主两根用本机子进程真实枚举，与容器脚本使用同一实现。"""
    root = Path(tool_root).absolute().parents[1]
    request = _request(str(root), contract)
    raw = shared._run([sys.executable, "-B", "-c", ENUMERATION_SCRIPT, json.dumps(request, ensure_ascii=False)],
                      "Claude 宿主双根工具树探针")
    return _parse_observation(raw, contract, "宿主")


def observe_container_tree(container: str, tool_root: str, contract: Mapping[str, Any]) -> dict[str, Any]:
    """按已有 deadline 执行容器探针，只读两个根的源码和目录事实。"""
    request = _request(str(PurePosixPath(tool_root).parents[1]), contract)
    raw = shared._run(["docker", "exec", container, "python3", "-B", "-c", ENUMERATION_SCRIPT,
                      json.dumps(request, ensure_ascii=False)], f"Claude {container} 双根工具树探针")
    return _parse_observation(raw, contract, container)


def validate_tree(value: Any, contract: Mapping[str, Any], label: str) -> dict[str, Any]:
    """纯校验根事实、规范条目与 v3 整树摘要，拒绝同漏项的假一致。"""
    scope = validate_read_contract(contract["tool_tree_read_contract"], str(contract["policy_sha256"]))
    tree = shared._expect(value, {
        "root", "root_observations", "read_contract_sha256", "entry_count", "files_sha256", "entries",
    }, label)
    root = tree["root"]
    _request(root, contract)
    observations = tree["root_observations"]
    _require(isinstance(observations, list) and len(observations) == len(scope["roots"]), f"{label}根观测不完整")
    for expected_root, observation in zip(scope["roots"], observations):
        row = shared._expect(observation, {"path", "is_directory", "is_symlink"}, f"{label}.root_observations")
        _require(row["path"] == str(PurePosixPath(root) / expected_root)
                 and row["is_directory"] is True and row["is_symlink"] is False, f"{label}受管根不存在或不可信")
    _require(tree["read_contract_sha256"] == shared._fingerprint(scope), f"{label}读集摘要不一致")
    entries = tree["entries"]
    _require(isinstance(entries, list) and 0 < len(entries) <= MAX_TREE_ENTRIES
             and type(tree["entry_count"]) is int and tree["entry_count"] == len(entries), f"{label}条目数量非法")
    names: list[str] = []
    root_names = {PurePosixPath(item).name for item in scope["roots"]}
    observed_names: set[str] = set()
    for item in entries:
        row = shared._expect(item, {"path", "sha256"}, f"{label}.entries")
        name = row["path"]
        _require(isinstance(name, str) and "\\" not in name and "\x00" not in name, f"{label}条目路径非法")
        path = PurePosixPath(name)
        _require(not path.is_absolute() and str(path) == name and len(path.parts) >= 2
                 and all(part not in {"", ".", ".."} for part in path.parts)
                 and path.parts[0] in root_names and path.suffix in scope["suffixes"]
                 and not set(scope["excluded_parts"]).intersection(path.parts[1:]), f"{label}条目越出冻结读集")
        _require(isinstance(row["sha256"], str) and SHA256.fullmatch(row["sha256"]) is not None, f"{label}条目摘要非法")
        names.append(name); observed_names.add(path.parts[0])
    _require(names == sorted(set(names)) and observed_names == root_names, f"{label}条目乱序、重复或漏根")
    expected = shared._fingerprint({"entries": entries})
    _require(tree["files_sha256"] == expected == contract["tool_files_sha256"], f"{label}完整工具树与冻结合同不一致")
    return dict(tree)


def validate_tool_trees(trees: Any, contract: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """纯校验四份八根观测，每份必须与冻结合同闭合。"""
    values = shared._expect(trees, set(TREE_NAMES), "tool_trees")
    result = {name: validate_tree(values[name], contract, f"tool_trees.{name}") for name in TREE_NAMES}
    first = result[TREE_NAMES[0]]["entries"]
    _require(all(item["entries"] == first for item in result.values()), "四份双根工具树条目不一致")
    return result
