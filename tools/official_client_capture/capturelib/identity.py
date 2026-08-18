"""官方客户端抓包执行源的确定性身份。"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .model import ConfigurationError
from .security import file_sha256


# 只绑定真实参与抓包、场景执行、解析与最终 manifest 的运行文件。
# 测试、缓存和其他专项驱动不进入摘要，避免无关变更使既有证据失效。
CAPTURE_SOURCE_RELATIVE_PATHS = (
    "capture.py",
    "claude_oauth_refresh.py",
    "claude_fw_e_relay.py",
    "claude_fw_e_runtime_snapshot.py",
    "drive_claude_tui.py",
    "upstream_byte_relay.py",
    "scrub_raw_bytes.py",
    "capturelib/__init__.py",
    "capturelib/analysis.py",
    "capturelib/environment.py",
    "capturelib/identity.py",
    "capturelib/lifecycle.py",
    "capturelib/manifest.py",
    "capturelib/model.py",
    "capturelib/recovery.py",
    "capturelib/claude_fw_f_v3.py",
    "capturelib/scenarios.py",
    "capturelib/security.py",
    "addons/mitm_capture.py",
)


def canonical_json_sha256(value: Any) -> str:
    """返回 JSON 值的稳定 SHA-256。"""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def capture_source_bundle_identity(tool_root: Path) -> dict[str, Any]:
    """枚举固定执行源并计算聚合摘要。"""

    root = tool_root.resolve()
    entries: list[dict[str, Any]] = []
    for relative in CAPTURE_SOURCE_RELATIVE_PATHS:
        path = root / relative
        if path.is_symlink() or not path.is_file():
            raise ConfigurationError(f"抓包执行源不存在或不是可信普通文件：{path}")
        entries.append(
            {
                "path": relative,
                "size": path.stat().st_size,
                "sha256": file_sha256(path),
            }
        )
    return {
        "algorithm": "canonical-json-sha256",
        "files": entries,
        "sha256": canonical_json_sha256(entries),
    }
