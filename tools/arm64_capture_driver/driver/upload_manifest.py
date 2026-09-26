#!/usr/bin/env python3
"""本机上传闭合清单；READY 不代替文件完整性校验，心跳不参与产物摘要。"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

SCHEMA = "arm64-driver-upload/v1"
EXCLUDED = {"impl-logs/READY", "impl-logs/HEARTBEAT", "impl-logs/upload-manifest.json"}


def manifest(root: Path) -> dict:
    rows = []
    for name in ("impl-logs", "local-gates"):
        directory = root / name
        if directory.is_symlink() or not directory.is_dir():
            raise ValueError(f"缺少上传目录：{name}")
        for path in sorted(directory.rglob("*")):
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                raise ValueError(f"上传产物不得为符号链接：{relative}")
            if path.is_file() and relative not in EXCLUDED:
                rows.append({"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                             "bytes": path.stat().st_size})
    paths = {row["path"] for row in rows}
    if not {"impl-logs/check-egress-spec.log", "impl-logs/cross-check/check-egress-spec.C-only.local.log"}.issubset(paths):
        raise ValueError("上传缺少本机实现门禁与候选提交交叉核对日志")
    return {"schema_version": SCHEMA, "files": sorted(rows, key=lambda row: row["path"])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "verify"))
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    try:
        value = manifest(args.root)
        path = args.root / "impl-logs/upload-manifest.json"
        if path.is_symlink():
            raise ValueError("上传清单不得为链接")
        if args.action == "create":
            path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            path.chmod(0o600)
        else:
            if json.loads(path.read_text()) != value:
                raise ValueError("上传文件集合或摘要不一致")
            # 完整收据在上传后才生成，先核对本机日志的实际候选和承接提交，防止旧 READY 被误用。
            if "C" in os.environ and "DC" in os.environ:
                spec = (args.root / "impl-logs/check-egress-spec.log").read_text()
                cross = (args.root / "impl-logs/cross-check/check-egress-spec.C-only.local.log").read_text()
                if (f"candidate_commit={os.environ['C']}（" not in spec
                        or f"executed_on_commit={os.environ['DC']}（" not in spec
                        or f"commit={os.environ['C']}\n" not in cross):
                    raise ValueError("上传日志没有绑定当前候选与承接提交")
        print(f"UPLOAD_VERIFIED files={len(value['files'])}")
        return 0
    except (OSError, ValueError) as error:
        print(f"上传产物拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
