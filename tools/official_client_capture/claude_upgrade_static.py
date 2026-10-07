"""Claude VC-1 静态分析动作（Claude 升级控制面设计 B2-11）：``claude_upgrade.py analyze-target --campaign-dir``。

一、做什么
==========
对 Campaign 冻结的官方二进制（总计划 ``configuration.official_freeze_path``，B2-01 冻结目录）跑两步离线分析，零请求：

1. ``claude_fw_e.analyze_bundles``：提取 Bun bundle 模块、模块清单、词法可达索引、目标 AST inventory 与目标 SinkInventory，
   并与 Campaign 冻结的 2.1.220 词法基线（``inputs.static_baseline_anchors``／``inputs.static_baseline_reachability``）做静态
   差异；
2. ``claude_sink_containment.mjs``：对同一份 SinkInventory 逐 sink 给出 AST 包含关系证据（模块清单模式）。

node 与 TypeScript 取宿主数据根下的受管运行时（Claude 身份策略 ``runtime_bindings``：``runtime/claude-tooling/node`` 与
``runtime/claude-tooling/typescript/lib/typescript.js``），摘要必须等于策略登记值；不在 PATH 里另找。

二、产物与收据（``official/static-analysis/``）
==============================================
在 ``official/.static-analysis-<随机>`` 里生成全部产物与收据 ``static-analysis.json``（合同
``claude-upgrade-static-analysis/v1``），完成后整目录改名到规范坐标：规范坐标要么不存在、要么是一份完整且自洽的分析。
收据绑定冻结收据、基线两件、运行时两件与全部产物（相对规范目录的路径、摘要、字节数），记录 containment 闭集结论；
产物里 ``linux-arm64/target-sink-inventory.json`` 与 ``linux-arm64/sink-containment.json`` 就是 VC-1 seal 要的两份静态输入。

三、幂等与中断
==============
规范目录已存在时只重放收据（逐项核对绑定与输入），一致即复用、不再分析；不一致失败关闭（不覆盖）。被强杀留下的临时目录
不带收据或没改名，重跑先删掉本 Campaign ``official/`` 下全部 ``.static-analysis-*`` 临时目录再从头分析。

四、在批次里的位置
==================
首批（VC-1 序号 1）恰有一个 ``capture-official`` 动作（共享监督器的 VC-1 普通恢复链要求，见 ``claude_upgrade_campaign``
第六节）；静态分析是首批之后、seal 之前的 VC-1 批次动作（``static_action``：动作 ``vc1-01-analyze-target``，项同名）。批次
按 action_id 排序执行，``vc1_seal_actions`` 给出驱动派发的两种 VC-1 封存批次（B2-11 第二部分）：

* 预览批次：静态分析 ``vc1-01-analyze-target`` 在前、seal 预览 ``vc1-02-seal-preview`` 在后（预览停在等批准，是批次内的
  合法暂停点）；只读复用前序的 Campaign 没有预览，同一批次里 seal 直接完成；
* 批准批次：只有 seal 批准 ``vc1-03-seal-approve``（带预览给出的 review_sha256）。

动作 ID 不用 Codex 的 ``seal-official-preview``／``seal-official-approve``：监督器按那两个名字执行 Codex 的断言证据包门禁，
Claude 没有断言包。VC-1 seal 不给 ``--target-inventory``／``--sink-containment`` 时取本动作的规范产物（``canonical_outputs``）；
显式给出且规范产物存在时两者必须逐字节相同。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层；分析本身在
evidence 层的 ``claude_fw_e``（只调用、不修改）。
"""

from __future__ import annotations

import argparse
import os
import secrets
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import ensure_private_directory, file_sha256

STATIC_ROOT = Path("official") / "static-analysis"
TEMP_PREFIX = ".static-analysis-"
RECEIPT_NAME = "static-analysis.json"
RECEIPT_SCHEMA = "claude-upgrade-static-analysis/v1"
RESULT_SCHEMA = "claude-upgrade-static-analysis-result/v1"
PLATFORM = "linux/arm64"
PLATFORM_DIRNAME = "linux-arm64"
CONTAINMENT_NAME = "sink-containment.json"
CONTAINMENT_TOOL = Path(__file__).resolve().with_name("claude_sink_containment.mjs")
RUNTIME_BINDING_NAMES = ("node", "typescript")
# 收据绑定的产物：名字 → 相对规范目录的路径。
OUTPUTS = {
    "static_diff": "static-diff.json",
    "bundle_anchors": f"{PLATFORM_DIRNAME}/bundle-anchors.json",
    "bundle_manifest": f"{PLATFORM_DIRNAME}/bundle-manifest.json",
    "reachability_index": f"{PLATFORM_DIRNAME}/reachability-index.json",
    "target_native_ast": f"{PLATFORM_DIRNAME}/target-native-ast.json",
    "target_sink_inventory": f"{PLATFORM_DIRNAME}/target-sink-inventory.json",
    "sink_containment": f"{PLATFORM_DIRNAME}/{CONTAINMENT_NAME}",
}
RECEIPT_FIELDS = frozenset(
    {
        "schema_version",
        "client",
        "campaign_id",
        "target_version",
        "target_sha256",
        "platform",
        "official_freeze",
        "baseline",
        "runtime",
        "outputs",
        "containment",
        "target_sink_count",
        "live_request_count",
    }
)
# 批次动作（首批之后、seal 之前的 VC-1 批次）。
ACTION_ID = "vc1-01-analyze-target"
ACTION_OPERATION = "VC-1:vc1-01-analyze-target"
ACTION_TIMEOUT_SECONDS = 3600
CONTAINMENT_TIMEOUT_SECONDS = 3600


def _host_data_root(campaign_dir: Path) -> Path:
    from tools.official_client_capture import claude_upgrade_capture

    return claude_upgrade_capture._host_data_root(campaign_dir)


def _binding(path: Path, base: Path) -> dict[str, Any]:
    """文件绑定：相对 ``base`` 的路径（两边先解析，避免临时目录经符号链接时相对路径算错）、摘要与字节数。"""

    resolved = Path(path).resolve(strict=True)
    return {
        "path": resolved.relative_to(Path(base).resolve(strict=True)).as_posix(),
        "sha256": file_sha256(resolved),
        "bytes": resolved.stat().st_size,
    }


def runtime_tools(data_root: Path) -> dict[str, dict[str, str]]:
    """宿主数据根下的受管 node 与 TypeScript：路径与版本取 Claude 身份策略 ``runtime_bindings``，摘要必须等于登记值。"""

    from tools.official_client_capture import claude_tool_identity_policy

    try:
        policy = claude_tool_identity_policy.load_policy()
    except claude_tool_identity_policy.ToolIdentityPolicyError as error:
        raise ConfigurationError(f"Claude 身份策略不可读：{error}") from error
    bindings = {str(item["name"]): item for item in policy["runtime_bindings"]}
    result: dict[str, dict[str, str]] = {}
    for name in RUNTIME_BINDING_NAMES:
        binding = bindings.get(name)
        if binding is None:
            raise ConfigurationError(f"Claude 身份策略没有登记受管运行时 {name}。")
        relative = PurePosixPath(str(binding["path"]))
        if relative.is_absolute() or ".." in relative.parts:
            raise ConfigurationError(f"受管运行时 {name} 的登记路径不是数据根内相对路径。")
        path = Path(data_root).joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"宿主数据根缺少受管运行时 {name}：{path}")
        digest = file_sha256(path)
        if digest != binding["sha256"]:
            raise ConfigurationError(f"受管运行时 {name} 的摘要 {digest} 不是策略登记的 {binding['sha256']}。")
        if name == "node" and not os.access(path, os.X_OK):
            raise ConfigurationError(f"受管 node 不可执行：{path}")
        result[name] = {"path": str(path), "sha256": digest, "version": str(binding["version"])}
    return result


def _official_freeze(manifest: Mapping[str, Any]) -> tuple[Path, str]:
    configuration = manifest.get("configuration")
    if not isinstance(configuration, Mapping):
        raise ConfigurationError("Claude Campaign 总计划缺少 configuration。")
    path = Path(str(configuration.get("official_freeze_path", "")))
    digest = configuration.get("official_freeze_sha256")
    if not path.is_absolute() or path.is_symlink() or not path.is_file() or file_sha256(path) != digest:
        raise ConfigurationError("Campaign 冻结的官方冻结收据缺失或摘要漂移（configuration.official_freeze_path／sha256）。")
    return path, str(digest)


def _frozen_inputs(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Path]:
    from tools.official_client_capture import claude_upgrade_seal

    return {
        name: claude_upgrade_seal._frozen_input(campaign_dir, manifest, f"static_baseline_{name}")
        for name in ("anchors", "reachability")
    }


def _expected_inputs(campaign_dir: Path, manifest: Mapping[str, Any], data_root: Path) -> dict[str, Any]:
    """收据里记录的输入：冻结收据、两份基线（相对宿主数据根）与运行时两件。"""

    freeze_path, freeze_sha256 = _official_freeze(manifest)
    baseline = _frozen_inputs(campaign_dir, manifest)
    return {
        "official_freeze": {"path": str(freeze_path), "sha256": freeze_sha256},
        "baseline": {name: _binding(path, data_root) for name, path in sorted(baseline.items())},
        "runtime": runtime_tools(data_root),
        "_paths": {"freeze": freeze_path, **{f"baseline_{name}": path for name, path in baseline.items()}},
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade._read_json(path, label)


def load_receipt(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """重放规范目录里的收据：没有规范目录返回 None；有则逐项核对输入与产物绑定，任一不符失败关闭（不覆盖）。"""

    root = Path(campaign_dir) / STATIC_ROOT
    if not root.exists() and not root.is_symlink():
        return None
    if root.is_symlink() or not root.is_dir():
        raise ConfigurationError(f"静态分析规范目录不可信：{root}")
    receipt = _read_json(root / RECEIPT_NAME, "静态分析收据")
    if set(receipt) != RECEIPT_FIELDS or receipt.get("schema_version") != RECEIPT_SCHEMA:
        raise ConfigurationError("静态分析收据不是 claude-upgrade-static-analysis/v1 或字段不闭合。")
    if (
        receipt.get("client") != "claude"
        or receipt.get("campaign_id") != manifest.get("campaign_id")
        or receipt.get("target_version") != manifest.get("target_version")
        or receipt.get("target_sha256") != manifest.get("target_sha256")
        or receipt.get("platform") != PLATFORM
        or receipt.get("live_request_count") != 0
    ):
        raise ConfigurationError("静态分析收据不属于本 Campaign 或目标版本。")
    data_root = _host_data_root(campaign_dir)
    expected = _expected_inputs(campaign_dir, manifest, data_root)
    for key in ("official_freeze", "baseline", "runtime"):
        if receipt[key] != expected[key]:
            raise ConfigurationError(f"静态分析收据记录的 {key} 与 Campaign 冻结输入不一致（输入漂移）。")
    outputs = receipt.get("outputs")
    if not isinstance(outputs, Mapping) or set(outputs) != set(OUTPUTS):
        raise ConfigurationError("静态分析收据的产物绑定不闭合。")
    for name, relative in OUTPUTS.items():
        path = root / relative
        binding = outputs[name]
        if (
            not isinstance(binding, Mapping)
            or binding.get("path") != relative
            or path.is_symlink()
            or not path.is_file()
            or file_sha256(path) != binding.get("sha256")
            or path.stat().st_size != binding.get("bytes")
        ):
            raise ConfigurationError(f"静态分析产物 {relative} 缺失或摘要漂移。")
    return receipt


def canonical_outputs(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Path] | None:
    """seal 的静态输入默认值：规范收据重放通过时返回收据、目标 SinkInventory 与 containment 的路径；没有规范目录返回 None。"""

    receipt = load_receipt(campaign_dir, manifest)
    if receipt is None:
        return None
    root = (Path(campaign_dir) / STATIC_ROOT).resolve(strict=True)
    return {
        "receipt": root / RECEIPT_NAME,
        "target_inventory": root / OUTPUTS["target_sink_inventory"],
        "sink_containment": root / OUTPUTS["sink_containment"],
    }


def static_action(campaign_dir: Path) -> dict[str, Any]:
    """VC-1 批次里的静态分析动作（首批之后、seal 之前）：受管 Python 直接调用 ``claude_upgrade.py analyze-target``。"""

    from tools.official_client_capture import claude_upgrade_batch

    return claude_upgrade_batch.managed_action(
        ACTION_ID,
        ACTION_OPERATION,
        ["analyze-target", "--campaign-dir", str(Path(campaign_dir).resolve(strict=False))],
        item_ids=[ACTION_ID],
        timeout_seconds=ACTION_TIMEOUT_SECONDS,
    )


SEAL_PREVIEW_ACTION_ID = "vc1-02-seal-preview"
SEAL_APPROVE_ACTION_ID = "vc1-03-seal-approve"
SEAL_TIMEOUT_SECONDS = 3600
SHA256_LENGTH = 64


def vc1_seal_actions(
    campaign_dir: Path,
    *,
    approve_seal_sha256: str | None = None,
    discovery_dispositions: Path | None = None,
) -> list[dict[str, Any]]:
    """首批之后的 VC-1 封存批次动作（驱动生成动作计划用）：预览批次是 [静态分析, seal 预览]，批准批次是 [seal 批准]。

    要封存的 attempt 取 Campaign 状态里处于 ``official_awaiting_receipts`` 的那个正式 attempt（别的状态按状态给出的下一步
    处理，这里拒绝）；只读复用前序的 Campaign 没有 attempt，seal 也没有预览与批准，只有预览批次一种。
    ``discovery_dispositions`` 是发现项的人工处置文件（Campaign 内），预览与批准要给同一份。
    """

    from tools.official_client_capture import claude_upgrade
    from tools.official_client_capture import claude_upgrade_batch

    campaign_dir = Path(campaign_dir).resolve(strict=True)
    manifest = claude_upgrade._require_formal_campaign(campaign_dir)
    seal_argv = ["capture-official", "seal", "--campaign-dir", str(campaign_dir)]
    if manifest.get("predecessor") is None:
        status = claude_upgrade.campaign_status(campaign_dir)
        attempt = status.get("official_attempt") or {}
        if status.get("status") != "official_awaiting_receipts" or not attempt.get("attempt_id"):
            raise ConfigurationError(
                f"VC-1 封存批次要求官方 attempt 处于 awaiting_receipts，当前 {status.get('status')}：{status.get('next_command')}"
            )
        seal_argv += ["--attempt-id", str(attempt["attempt_id"])]
    elif approve_seal_sha256 is not None:
        raise ConfigurationError("只读复用前序的 Campaign 的 seal 没有预览与批准，不接受批准摘要。")
    if discovery_dispositions is not None:
        seal_argv += ["--discovery-dispositions", str(discovery_dispositions)]
    if approve_seal_sha256 is None:
        preview = claude_upgrade_batch.managed_action(
            SEAL_PREVIEW_ACTION_ID, f"VC-1:{SEAL_PREVIEW_ACTION_ID}", seal_argv,
            item_ids=[SEAL_PREVIEW_ACTION_ID], timeout_seconds=SEAL_TIMEOUT_SECONDS,
        )
        return [static_action(campaign_dir), preview]
    if len(approve_seal_sha256) != SHA256_LENGTH or any(character not in "0123456789abcdef" for character in approve_seal_sha256):
        raise ConfigurationError("seal 批准摘要必须是 64 位小写十六进制 review_sha256。")
    return [
        claude_upgrade_batch.managed_action(
            SEAL_APPROVE_ACTION_ID, f"VC-1:{SEAL_APPROVE_ACTION_ID}", [*seal_argv, "--approve-seal-sha256", approve_seal_sha256],
            item_ids=[SEAL_APPROVE_ACTION_ID], timeout_seconds=SEAL_TIMEOUT_SECONDS,
        )
    ]


def _remove_stale_temporaries(official_root: Path) -> list[str]:
    """删掉被中断的分析留下的临时目录（只认本 Campaign ``official/`` 下 ``.static-analysis-`` 前缀的直接子目录）。"""

    removed = []
    if not official_root.is_dir():
        return removed
    for path in sorted(official_root.iterdir()):
        if path.name.startswith(TEMP_PREFIX) and path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            removed.append(path.name)
    return removed


def _run_containment(temp_root: Path, runtime: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    command = [
        runtime["node"]["path"],
        str(CONTAINMENT_TOOL),
        "--bundle-manifest",
        OUTPUTS["bundle_manifest"],
        "--inventory",
        OUTPUTS["target_sink_inventory"],
        "--ast-inventory",
        OUTPUTS["target_native_ast"],
        "--output",
        OUTPUTS["sink_containment"],
        "--typescript-module",
        runtime["typescript"]["path"],
    ]
    try:
        completed = subprocess.run(
            command, cwd=temp_root, capture_output=True, text=True, check=False, timeout=CONTAINMENT_TIMEOUT_SECONDS
        )
    except subprocess.TimeoutExpired as error:
        raise ConfigurationError(f"sink containment 超时（{CONTAINMENT_TIMEOUT_SECONDS} 秒）。") from error
    if completed.returncode != 0:
        raise ConfigurationError(f"sink containment 失败：{completed.stderr.strip()[-2000:]}")
    return _read_json(temp_root / OUTPUTS["sink_containment"], "sink containment 证据")


def analyze_target(campaign_dir: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    """执行（或复用）VC-1 静态分析，返回结果摘要；见模块说明第一～三节。"""

    from tools.official_client_capture import claude_fw_e

    campaign_dir = Path(campaign_dir)
    existing = load_receipt(campaign_dir, manifest)
    if existing is not None:
        return _result(campaign_dir, existing, reused=True, removed=[])
    data_root = _host_data_root(campaign_dir)
    expected = _expected_inputs(campaign_dir, manifest, data_root)
    official_root = ensure_private_directory(campaign_dir / "official", campaign_dir)
    removed = _remove_stale_temporaries(official_root)
    temp_root = official_root / f"{TEMP_PREFIX}{secrets.token_hex(8)}"
    paths = expected["_paths"]
    try:
        static_diff = claude_fw_e.analyze_bundles(
            paths["freeze"].parent,
            temp_root,
            paths["baseline_anchors"],
            paths["baseline_reachability"],
            node_binary=expected["runtime"]["node"]["path"],
            typescript_module_path=Path(expected["runtime"]["typescript"]["path"]),
        )
    except claude_fw_e.FWEEvidenceError as error:
        raise ConfigurationError(f"VC-1 静态分析失败：{error}") from error
    if (
        static_diff.get("target_version") != manifest.get("target_version")
        or set((static_diff.get("target_platforms") or {})) != {PLATFORM}
        or static_diff["target_platforms"][PLATFORM].get("binary_sha256") != manifest.get("target_sha256")
    ):
        raise ConfigurationError("静态分析结果的目标版本、平台或二进制摘要与 Campaign 不一致。")
    containment = _run_containment(temp_root, expected["runtime"])
    inventory = _read_json(temp_root / OUTPUTS["target_sink_inventory"], "目标 SinkInventory")
    completeness = containment.get("completeness") if isinstance(containment.get("completeness"), Mapping) else {}
    receipt = {
        "schema_version": RECEIPT_SCHEMA,
        "client": "claude",
        "campaign_id": manifest["campaign_id"],
        "target_version": manifest["target_version"],
        "target_sha256": manifest["target_sha256"],
        "platform": PLATFORM,
        "official_freeze": expected["official_freeze"],
        "baseline": expected["baseline"],
        "runtime": expected["runtime"],
        "outputs": {name: _binding(temp_root / relative, temp_root) for name, relative in OUTPUTS.items()},
        "containment": {
            "result": completeness.get("result"),
            "evidence_row_count": completeness.get("evidence_row_count"),
            "unmatched_sink_ids": list(completeness.get("unmatched_sink_ids") or []),
        },
        "target_sink_count": len(inventory.get("sinks") or []),
        "live_request_count": 0,
    }
    from tools.official_client_capture import claude_upgrade_vc

    claude_upgrade_vc.write_json_once(temp_root / RECEIPT_NAME, receipt)
    canonical = campaign_dir / STATIC_ROOT
    try:
        os.rename(temp_root, canonical)
    except OSError:
        # 并发的另一次分析已先改名：以规范目录为准（重放核对），丢弃本次临时目录。
        if not canonical.is_dir():
            raise
        shutil.rmtree(temp_root)
        return _result(campaign_dir, load_receipt(campaign_dir, manifest) or {}, reused=True, removed=removed)
    return _result(campaign_dir, load_receipt(campaign_dir, manifest) or {}, reused=False, removed=removed)


def _result(campaign_dir: Path, receipt: Mapping[str, Any], *, reused: bool, removed: list[str]) -> dict[str, Any]:
    root = Path(campaign_dir) / STATIC_ROOT
    return {
        "schema_version": RESULT_SCHEMA,
        "status": "complete",
        "reused": reused,
        "campaign_id": receipt.get("campaign_id"),
        "receipt": str(root / RECEIPT_NAME),
        "receipt_sha256": file_sha256(root / RECEIPT_NAME),
        "target_inventory": str(root / OUTPUTS["target_sink_inventory"]),
        "sink_containment": str(root / OUTPUTS["sink_containment"]),
        "containment": receipt.get("containment"),
        "target_sink_count": receipt.get("target_sink_count"),
        "removed_temporaries": removed,
        "live_request_count": 0,
    }


def analyze_target_command(arguments: argparse.Namespace) -> tuple[dict[str, Any], int]:
    """``analyze-target``：读总计划后执行；零请求，失败报错退出 1（由编排器入口统一处理异常）。"""

    from tools.official_client_capture import claude_upgrade

    campaign_dir = Path(arguments.campaign_dir)
    manifest = claude_upgrade.load_campaign_manifest(campaign_dir)
    return analyze_target(campaign_dir, manifest), 0
