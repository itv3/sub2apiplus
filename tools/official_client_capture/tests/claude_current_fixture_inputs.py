"""为当前 Claude 回归冻结独立输入，不授予正式规格采纳或候选完成权。

工具夹具保留真实策略原字节，并冻结隔离受管树的完整七项身份与逐文件摘要。
场景夹具只在新文件中替换 ``source_spec.sha256``，原模板与所有声明输入均留存；
正式 ``read_scenario_manifest`` 仍按默认严格模式核对当前指南。两类夹具都以
``regression_fixture`` 标识来源，验证时拒绝冻结之后的输入、输出或记录漂移。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from tools.official_client_capture import claude_upgrade_plan as plan
from tools.official_client_control import receipts
from tools.official_client_control.canonical import canonical_json_bytes
from tools.official_client_control.tests.fixtures import ManagedToolTree


IDENTITY_FIELDS = (
    "client", "policy_version", "policy_sha256", "wire_producer_sha256",
    "evidence_semantics_sha256", "control_sha256", "tool_files_sha256",
)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _plain_bytes(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"回归夹具输入不是普通文件：{path}")
    return path.read_bytes()


def _create(path: Path, raw: bytes) -> None:
    """只创建新文件，禁止重跑覆盖已冻结的原件或来源记录。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        stream.write(raw)
    path.chmod(0o600)


@dataclass(frozen=True)
class CurrentToolFixtureInputs:
    """期望值存成不可变字节，取回字典后的变异不能悄悄修改冻结期望。"""

    directory: Path
    policy_relative: Path
    policy_bytes: bytes
    identity_bytes: bytes
    digests_bytes: bytes
    record_bytes: bytes

    def check_identity(self, identity: dict[str, Any]) -> None:
        expected = json.loads(self.identity_bytes)
        if set(identity) != set(IDENTITY_FIELDS):
            raise ValueError("回归夹具的七项工具身份字段不闭合")
        for name in IDENTITY_FIELDS:
            if identity[name] != expected[name] or type(identity[name]) is not type(expected[name]):
                raise ValueError(f"回归夹具的工具身份不符：{name}")

    def verify(self, tree: ManagedToolTree, identity: dict[str, Any]) -> None:
        if _plain_bytes(tree.root / self.policy_relative) != self.policy_bytes:
            raise ValueError("回归夹具的真实策略原字节漂移")
        if _plain_bytes(self.directory / "policy.original.json") != self.policy_bytes:
            raise ValueError("回归夹具的策略冻结原件漂移")
        if _plain_bytes(self.directory / "inputs.json") != self.record_bytes:
            raise ValueError("回归夹具的工具输入记录漂移")
        with tree.active():
            manifest = receipts.current_tool_manifest("claude")
            if canonical_json_bytes(manifest["digests"]) != self.digests_bytes:
                raise ValueError("回归夹具的完整受管文件清单漂移")
            self.check_identity(receipts.tool_identity_from_manifest(manifest))
        self.check_identity(identity)


def freeze_current_tool_inputs(tree: ManagedToolTree, directory: Path) -> CurrentToolFixtureInputs:
    """从真实源策略取原字节，身份由实际引擎在隔离副本上计算一次并冻结。"""

    engine = receipts._identity_engine("claude")
    policy_relative = Path(engine.DEFAULT_POLICY_PATH).relative_to(engine.REPOSITORY_ROOT)
    policy_bytes = _plain_bytes(Path(engine.DEFAULT_POLICY_PATH))
    if _plain_bytes(tree.root / policy_relative) != policy_bytes:
        raise ValueError("回归夹具策略副本不等于真实源策略原字节")
    with tree.active():
        manifest = receipts.current_tool_manifest("claude")
        identity = receipts.tool_identity_from_manifest(manifest)
    if set(identity) != set(IDENTITY_FIELDS):
        raise ValueError("实际身份引擎未返回完整七项工具身份")
    record = {
        "source_scope": "regression_fixture",
        "policy": {"path": policy_relative.as_posix(), "sha256": _sha(policy_bytes), "bytes": len(policy_bytes)},
        "tool_identity": identity,
        "managed_digests": manifest["digests"],
    }
    frozen = CurrentToolFixtureInputs(
        directory, policy_relative, policy_bytes, canonical_json_bytes(identity),
        canonical_json_bytes(manifest["digests"]), canonical_json_bytes(record),
    )
    _create(directory / "policy.original.json", policy_bytes)
    _create(directory / "inputs.json", frozen.record_bytes)
    frozen.verify(tree, identity)
    return frozen


def scenario_input_files(template: Path) -> dict[str, Path]:
    """枚举生成与严格读回实际使用的原件；每份文件同时记录完整字节摘要与长度。"""

    payload = json.loads(_plain_bytes(template))
    rules_path = plan._repository_file(payload["rule_manifest"]["path"], "回归夹具规则清单")
    rules = json.loads(_plain_bytes(rules_path))
    files = {"template": template, "generator": Path(__file__), "reader": Path(plan.__file__)}
    for name in ("source_spec", "rule_manifest", "probe_catalog", "version_facts"):
        files[name] = plan._repository_file(payload[name]["path"], f"回归夹具 {name}")
    files["baseline_rule_manifest"] = plan._repository_file(
        rules["baseline_rule_manifest"]["path"], "回归夹具基线规则清单"
    )
    files["schema"] = template.parent / payload["$schema"]
    for relative in ("capturelib/model.py", "capturelib/security.py", "capturelib/claude_fw_f_v3.py", "capturelib/claude_fw_f_v4.py"):
        files[relative] = plan._repository_file(
            "tools/official_client_capture/" + relative, f"回归夹具读侧依赖 {relative}"
        )
    return files


@dataclass(frozen=True)
class CurrentScenarioFixtureInputs:
    directory: Path
    manifest_path: Path
    manifest_bytes: bytes
    record_bytes: bytes
    sources: tuple[tuple[str, Path, bytes], ...]

    def verify(self) -> None:
        for name, path, raw in self.sources:
            if _plain_bytes(path) != raw:
                raise ValueError(f"回归夹具的来源输入漂移：{name}")
            if _plain_bytes(self.directory / "originals" / name) != raw:
                raise ValueError(f"回归夹具的冻结原件漂移：{name}")
        if _plain_bytes(self.manifest_path) != self.manifest_bytes:
            raise ValueError("回归夹具的新场景文件漂移")
        if _plain_bytes(self.directory / "inputs.json") != self.record_bytes:
            raise ValueError("回归夹具的场景输入记录漂移")
        payload = json.loads(self.manifest_bytes)
        rules = json.loads(dict((name, raw) for name, _path, raw in self.sources)["rule_manifest"])["required_rules"]
        # 不传 verify_source_spec，持续验证正式读侧的默认严格行为。
        plan.read_scenario_manifest(self.manifest_path, expected_version=payload["claude_version"], rules=rules)


def freeze_current_scenario_inputs(template: Path, directory: Path) -> CurrentScenarioFixtureInputs:
    """生成独立场景输入；仅允许 source_spec.sha256 一个字段变化，不改正式模板。"""

    sources = tuple((name, path, _plain_bytes(path)) for name, path in sorted(scenario_input_files(template).items()))
    originals = {name: raw for name, _path, raw in sources}
    payload = json.loads(originals["template"])
    current_sha = plan.guide_section_sha256(dict((name, path) for name, path, _raw in sources)["source_spec"], payload["source_spec"]["fragment"])
    old_sha = payload["source_spec"]["sha256"]
    if old_sha != current_sha and originals["template"].count(old_sha.encode()) != 1:
        raise ValueError("回归夹具的来源章节摘要不能唯一替换")
    manifest_bytes = originals["template"].replace(old_sha.encode(), current_sha.encode(), 1)
    expected = deepcopy(payload)
    expected["source_spec"]["sha256"] = current_sha
    if json.loads(manifest_bytes) != expected:
        raise ValueError("回归夹具替换越出 source_spec.sha256")
    plan.validate_scenario_manifest(expected)
    records = {
        name: {"path": str(path.resolve()), "snapshot": "originals/" + name, "sha256": _sha(raw), "bytes": len(raw)}
        for name, path, raw in sources
    }
    record = {
        "source_scope": "regression_fixture",
        "allowed_changed_field": "source_spec.sha256",
        "source_section": {"fragment": payload["source_spec"]["fragment"], "old_sha256": old_sha, "current_sha256": current_sha},
        "inputs": records,
        "generated": {"path": "current-scenarios.json", "sha256": _sha(manifest_bytes), "bytes": len(manifest_bytes)},
    }
    frozen = CurrentScenarioFixtureInputs(
        directory, directory / "current-scenarios.json", manifest_bytes, canonical_json_bytes(record), sources
    )
    for name, _path, raw in sources:
        _create(directory / "originals" / name, raw)
    _create(frozen.manifest_path, manifest_bytes)
    _create(directory / "inputs.json", frozen.record_bytes)
    frozen.verify()
    return frozen
