"""只读核对显式冻结制品与外部诊断原件；不启动产品或认证真实 provider。

外部 capability_results 即使携带摘要正确的原件，也只是 supplied_unverified。
本批没有原生进程、flow、模块、UI 或隔离配方 provider，不把布尔自报变成 available。
正式来源尚未登记，formal_ready 与 launch_allowed 始终 false。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture.claude_mac_recorder import measure_artifact


SCHEMA = "claude-mac-capability-probe/v1"
DIAGNOSTIC_SCHEMA = "claude-mac-capability-diagnostic/v1"
CAPABILITIES = ("process_events", "module_activation", "socket_events", "ui_driver", "isolation_recipe", "dispatch_upper_bound")


class CapabilityProbeError(ValueError):
    """探测参数非法；不执行产品、Shell 或权限申请。"""


def _diagnostic(name: str, value: Any) -> dict[str, Any]:
    unknown = {"capability": name, "status": "unknown", "origin": "provider_unimplemented",
               "reason": "本批没有真实 provider；外部诊断不能授予能力"}
    if value is None:
        return unknown
    try:
        if not isinstance(value, Mapping) or set(value) != {"evidence"}:
            raise CapabilityProbeError("外部能力结果须携带原件；不接受布尔自报")
        ref = value["evidence"]
        if not isinstance(ref, Mapping) or set(ref) != {"path", "sha256", "bytes"}:
            raise CapabilityProbeError("能力原件绑定非法")
        path = Path(ref["path"])
        if not path.is_absolute() or ".." in path.parts:
            raise CapabilityProbeError("能力原件须为规范绝对路径")
        for item in (path, *path.parents):
            if item.is_symlink():
                raise CapabilityProbeError("能力原件路径包含 symlink")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 65536:
                raise CapabilityProbeError("能力原件须为有界普通单链接文件")
            raw = os.read(fd, 65537)
            ended = os.fstat(fd)
            if (info.st_size, info.st_mtime_ns) != (ended.st_size, ended.st_mtime_ns):
                raise CapabilityProbeError("能力原件读取期间变化")
        finally:
            os.close(fd)
        if type(ref["bytes"]) is not int or len(raw) != ref["bytes"] or hashlib.sha256(raw).hexdigest() != ref["sha256"]:
            raise CapabilityProbeError("能力原件摘要或字节数不符")
        fact = json.loads(raw)
        if not isinstance(fact, dict) or set(fact) != {"schema_version", "capability", "result", "observer", "observed_at_utc"}:
            raise CapabilityProbeError("诊断原件字段非法")
        if fact["schema_version"] != DIAGNOSTIC_SCHEMA or fact["capability"] != name or fact["result"] not in {"available", "missing", "invalid", "unknown"}:
            raise CapabilityProbeError("诊断原件身份或结果非法")
        if not isinstance(fact["observer"], str) or not fact["observer"] or len(fact["observer"]) > 128 or not isinstance(fact["observed_at_utc"], str):
            raise CapabilityProbeError("诊断原件观测者或时间非法")
        moment = datetime.fromisoformat(fact["observed_at_utc"].replace("Z", "+00:00"))
        if moment.tzinfo is None or moment.utcoffset().total_seconds() != 0:
            raise CapabilityProbeError("诊断时间须明确为 UTC")
        return {**unknown, "origin": "supplied_unverified", "supplied_result": fact["result"], "evidence": dict(ref)}
    except (ValueError, OSError, TypeError, KeyError) as error:
        return {"capability": name, "status": "invalid", "origin": "rejected_diagnostic", "reason": str(error)}


class CapabilityProbe:
    """仅检查调用方指定的冻结制品，不扫描用户 HOME 或读取账号配置。"""

    @staticmethod
    def check(frozen_artifacts: Sequence[Mapping[str, Any]], capability_results: Mapping[str, Any] | None = None,
              *, registered: bool = False) -> dict[str, Any]:
        if isinstance(frozen_artifacts, (str, bytes)) or not frozen_artifacts or len(frozen_artifacts) > 16:
            raise CapabilityProbeError("须指定 1 至 16 个冻结制品")
        if type(registered) is not bool:
            raise CapabilityProbeError("登记状态类型非法")
        if capability_results is not None and (not isinstance(capability_results, Mapping) or not set(capability_results) <= set(CAPABILITIES)):
            raise CapabilityProbeError("能力名称不在固定清单")
        results, ids = [], set()
        for artifact in frozen_artifacts:
            artifact_id = artifact.get("artifact_id") if isinstance(artifact, Mapping) else None
            try:
                if not isinstance(artifact_id, str) or not artifact_id or artifact_id in ids:
                    raise CapabilityProbeError("制品 ID 缺失或重复")
                ids.add(artifact_id)
                expected = artifact["content"]
                if not isinstance(expected, Mapping) or set(expected) != {"kind", "path", "sha256", "bytes"}:
                    raise CapabilityProbeError("制品内容绑定非法")
                if expected["kind"] not in {"file", "directory"} or type(expected["bytes"]) is not int or expected["bytes"] < 0 \
                        or not isinstance(expected["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", expected["sha256"]):
                    raise CapabilityProbeError("制品类型、摘要或字节数非法")
                path = Path(expected["path"])
                if not path.is_absolute() or ".." in path.parts:
                    raise CapabilityProbeError("制品路径须为规范绝对路径")
                if not path.exists() and not path.is_symlink():
                    results.append({"artifact_id": artifact_id, "status": "missing", "path": str(path)})
                    continue
                actual = measure_artifact(path)
                if actual != dict(expected):
                    results.append({"artifact_id": artifact_id, "status": "invalid", "reason": "实际制品与冻结内容不符", "actual": actual})
                else:
                    results.append({"artifact_id": artifact_id, "status": "matched", "actual": actual})
            except (ValueError, OSError, TypeError, KeyError) as error:
                results.append({"artifact_id": artifact_id, "status": "invalid", "reason": str(error)})
        facts = capability_results or {}
        capabilities = [_diagnostic(name, facts.get(name)) for name in CAPABILITIES]
        return {"schema_version": SCHEMA, "artifacts": results, "artifacts_match": all(r["status"] == "matched" for r in results),
                "capabilities": capabilities, "source_scope": "readonly_diagnostic", "reported_registered": registered,
                "registration_verified": False, "formal_ready": False, "launch_allowed": False,
                "status": "incomplete", "reason": "本批未实现真实 provider 与正式来源登记，制品匹配不是启动权"}
