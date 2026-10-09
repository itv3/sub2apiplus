#!/usr/bin/env python3
"""固定 ARM64 交付守卫安装入口；不接受任意命令、模块或数据根。

prepare 先生成真实 systemd 生命周期证明；activate 消费已经冻结的
配置 v2 与上下文。serve 只供固定 systemd unit 调用。首次自检没有
交付操作权；进入正式启用后损坏一律拒绝，不回退自检。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import stat
import sys

sys.dont_write_bytecode = True


def _managed_module():
    """先按既有驱动安装合同验真，随后才加载固定双受管根中的实现。"""
    target = Path("/root/arm64-capture-driver")
    data_root = Path("/root/docker/capture-cli/data")
    if platform.system() != "Linux" or platform.machine().lower() not in {"arm64", "aarch64"} or os.geteuid() != 0:
        raise ValueError("守卫安装仅允许 ARM64 root 受管入口")
    for path, mode in ((target, 0o700), (target / "install.py", 0o700), (Path(__file__), 0o700)):
        if path.is_symlink() or path.stat().st_uid != 0 or stat.S_IMODE(path.stat().st_mode) != mode:
            raise ValueError("固定驱动入口的所有者或权限非法")
    if Path(__file__) != target / "driver/delivery_guard.py":
        raise ValueError("守卫入口不是实际固定安装原件")
    path = target / "install.py"
    raw = path.read_bytes()
    specification = importlib.util.spec_from_file_location("_delivery_guard_installer", path)
    install = importlib.util.module_from_spec(specification)
    exec(compile(raw, str(path), "exec", dont_inherit=True), install.__dict__)
    if path.read_bytes() != raw:
        raise ValueError("驱动安装器加载期间漂移")
    manifest = install.load_manifest(target)
    install.verify_tree(target, manifest, check_mode=True, require_root_owner=True)
    install.verify_install(target, data_root, "claude")
    sys.path.insert(0, str(data_root))
    from tools.official_client_capture import claude_upgrade_delivery_installation as installation
    if Path(installation.__file__).parent != data_root / "tools/official_client_capture":
        raise ValueError("守卫安装实现不属于实际受管根")
    return installation


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "activate", "serve", "probe-child"))
    parser.add_argument("--instance", required=True)
    parser.add_argument("--execution-context", type=Path)
    args = parser.parse_args(argv)
    if (args.command == "activate") != (args.execution_context is not None):
        parser.error("仅 activate 必须提供 --execution-context")
    try:
        installation = _managed_module()
        if args.command == "serve":
            return installation.serve_installation(args.instance)
        if args.command == "probe-child":
            return installation.probe_child(args.instance)
        result = (installation.prepare_installation(args.instance) if args.command == "prepare"
                  else installation.activate_installation(args.instance, args.execution_context))
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        sys.stderr.write(str(error) + "\n")
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
