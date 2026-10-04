#!/usr/bin/env python3
"""VC-4 建树和构建网络合同；不改变正式构建参数 v2 的精确字段集合。

网络批准在创建或替换候选树之前检查，实际构建的模式、镜像、命令和日志另写收据。
这两份合同均由 VC-4 输入／输出续跑凭证绑定，历史没有凭证时不得猜测复用。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import stat
import subprocess
import sys

sys.dont_write_bytecode = True
# 独立命令仅要求 ARM64_VC_ENV；先安全解析本轮 D，再导入该数据根的受管工具。
# 被续跑模块导入时沿用调用方已加载的本轮 D，不在模块导入期间改变参数。
CLI_CONFIG = None
if __name__ == "__main__":
    from driver_config import load_config
    try:
        CLI_CONFIG = load_config()
    except (KeyError, ValueError, OSError) as error:
        print(f"VC-4 参数拒绝加载：{error}", file=sys.stderr)
        raise SystemExit(3)
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
from tools.official_client_capture import codex_upgrade_candidate_build as build
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts

TREES = ("source", "gate-tree", "build-tree", "plan-source")
TREE_SCHEMA = "arm64-vc4-trees/v1"
APPROVAL_SCHEMA = "arm64-vc4-build-network-approval/v1"
NETWORK_SCHEMA = "arm64-vc4-build-network/v1"
RESUME_SCHEMA = "arm64-vc4-resume/v2"


def plain_path(path: Path) -> None:
    if not path.is_absolute() or ".." in path.parts or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError("VC-4 路径必须是不含符号链接和父目录跳转的绝对路径")


def binding(path: Path) -> dict:
    plain_path(path)
    if not path.is_file():
        raise ValueError(f"缺少可信普通文件：{path}")
    return {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "bytes": path.stat().st_size}


def read(path: Path, *, private: bool = False) -> dict:
    binding(path)
    info = path.stat()
    if private and (info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600):
        raise ValueError("网络批准文件属主或权限不符合合同")
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("VC-4 凭证必须为对象")
    return value


def write_once(path: Path, value: dict) -> dict:
    plain_path(path)
    value = {**value, "binding_sha256": artifacts.digest(value)}
    with path.open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)
    return value


def bound(path: Path, schema: str) -> dict:
    value = read(path)
    if value.get("schema_version") != schema or value.get("binding_sha256") != artifacts.digest(
            {key: item for key, item in value.items() if key != "binding_sha256"}):
        raise ValueError("VC-4 凭证 schema 或自摘要错误")
    return value


def timestamp(value: str) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise ValueError("网络批准时间必须为 UTC 秒级时间")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def network_binding(config: dict) -> dict:
    """只读生成准确的网络批准范围；生成意向不代表批准已经存在。"""
    base, data = Path(config["B"]), Path(config["D"])
    plain_path(base)
    if base != data / "candidates" / config["CAND"]:
        raise ValueError("候选树根未绑定本轮数据根和 Candidate")
    mode = config.get("VC4_BUILD_NETWORK", "default")
    if mode not in {"default", "none", "host"}:
        raise ValueError("未知构建网络模式")
    return {
        "campaign_id": config["NEW"], "candidate_id": config["CAND"], "git_commit": config["C"],
        "target_architecture": "linux/arm64", "network_mode": mode, "data_root": str(data),
        "host": {"name": platform.node(), "architecture": platform.machine(), "kernel": platform.release()},
        "bundle": binding(Path(config["BUNDLE"])),
        "driver": {name: binding(Path(__file__).parent / name)["sha256"]
                   for name in ("build.sh", "vc4_contract.py")},
        "impact_scope": "仅本轮候选镜像 Dockerfile RUN 步骤使用指定网络，不修改宿主路由或切换生产网关",
    }


def network_inputs(config: dict, *, now: datetime | None = None) -> dict:
    """只读核对本轮明确请求的网络模式；绝不代签批准或自动切换 host。"""
    expected = network_binding(config)
    mode = expected["network_mode"]
    approval_path = config.get("VC4_BUILD_NETWORK_APPROVAL")
    approval = None
    if mode == "host" and not approval_path:
        raise ValueError("host 构建网络缺少专项批准，禁止建树及构建")
    if approval_path:
        path = Path(approval_path)
        value = read(path, private=True)
        if (set(value) != {"schema_version", "status", "binding", "approved_by", "approved_at_utc", "expires_at_utc"}
                or value["schema_version"] != APPROVAL_SCHEMA or value["status"] != "approved"
                or value["binding"] != expected or not isinstance(value["approved_by"], str)
                or not value["approved_by"].strip()):
            raise ValueError("构建网络批准身份、状态、范围或输入绑定不一致")
        current = now or datetime.now(timezone.utc)
        if not timestamp(value["approved_at_utc"]) <= current < timestamp(value["expires_at_utc"]):
            raise ValueError("构建网络批准尚未生效或已过期")
        approval = {"file": binding(path), "approved_by": value["approved_by"],
                    "approved_at_utc": value["approved_at_utc"], "expires_at_utc": value["expires_at_utc"]}
    return {"binding": expected, "approval": approval}


def tree_facts(base: Path, commit: str) -> dict:
    """完整历史和测试树无 vendor 同时成立；只有 build-tree 可注入 vendor。"""
    result = {}
    for name in TREES:
        root = base / name
        plain_path(root)
        def git(*args: str) -> str:
            return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True,
                                  text=True, timeout=120).stdout.strip()
        head, count = git("rev-parse", "HEAD"), int(git("rev-list", "--count", "HEAD"))
        if head != commit or git("rev-parse", "--is-shallow-repository") != "false" or count <= 10000:
            raise ValueError(f"{name} 不满足指定提交和完整历史合同")
        vendor = root / "backend/vendor"
        if name != "build-tree" and (vendor.exists() or vendor.is_symlink()):
            raise ValueError(f"{name} 不得含 vendor")
        allowed = set()
        if name == "plan-source":
            for plan in root.glob("docs/egress/lifecycle/*/gate-plan.json"):
                for generated in (plan.with_name("gate-plan-rehearsal.json"), plan.with_name("gate-plan-dispatch.json")):
                    if generated.exists():
                        if generated.is_symlink() or generated.read_bytes() != plan.read_bytes():
                            raise ValueError("计划树的派发副本与批准计划不同")
                        allowed.add("?? " + generated.relative_to(root).as_posix())
        if any(line not in allowed for line in git("status", "--porcelain", "--untracked-files=all").splitlines()):
            raise ValueError(f"{name} 不洁净")
        result[name] = {"git_commit": head, "commit_count": count, "git_tree": git("rev-parse", "HEAD^{tree}")}
    return result


def record_trees(base: Path, config: dict, vendor_mode: str) -> dict:
    vendor = base / "build-tree/backend/vendor"
    return write_once(base / "tree-preparation.json", {
        "schema_version": TREE_SCHEMA, "status": "complete", "campaign_id": config["NEW"],
        "candidate_id": config["CAND"], "git_commit": config["C"], "trees": tree_facts(base, config["C"]),
        "dependencies": {"go_mod": binding(base / "source/backend/go.mod"),
                         "go_sum": binding(base / "source/backend/go.sum"),
                         "vendor_modules": binding(vendor / "modules.txt"),
                         "vendor_sha256": build.scan_tree_inventory(vendor)["inventory_sha256"]},
        "vendor_mode": vendor_mode, "completed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    })


def verify_trees(base: Path, config: dict) -> dict:
    receipt = bound(base / "tree-preparation.json", TREE_SCHEMA)
    if (receipt.get("status") != "complete" or receipt.get("campaign_id") != config["NEW"]
            or receipt.get("candidate_id") != config["CAND"] or receipt.get("git_commit") != config["C"]
            or receipt.get("vendor_mode") not in {"copied_previous", "regenerated_missing", "regenerated_changed"}
            or receipt["trees"] != tree_facts(base, config["C"])):
        raise ValueError("建树凭证与本轮输入或实际树不一致")
    vendor = base / "build-tree/backend/vendor"
    actual = {"go_mod": binding(base / "source/backend/go.mod"), "go_sum": binding(base / "source/backend/go.sum"),
              "vendor_modules": binding(vendor / "modules.txt"),
              "vendor_sha256": build.scan_tree_inventory(vendor)["inventory_sha256"]}
    if actual != receipt["dependencies"]:
        raise ValueError("建树后的依赖或 vendor 漂移")
    return binding(base / "tree-preparation.json")


def check_command(command: list[str], mode: str, base: Path) -> None:
    if command[:2] != ["docker", "build"] or command[-1:] != [str(base / "artifacts/ctx")]:
        raise ValueError("实际构建命令未绑定 Docker 构建和候选上下文")
    modes = []
    for index, item in enumerate(command):
        if item == "--network":
            modes.append(command[index + 1] if index + 1 < len(command) else "")
        elif item.startswith("--network="):
            modes.append(item.split("=", 1)[1])
    if modes != ([] if mode == "default" else [mode]):
        raise ValueError("实际构建命令的网络模式与批准不一致")
    if any(item == "--allow" or item.startswith("--allow=") for item in command):
        raise ValueError("实际构建命令含未批准的额外 entitlement")


def record_network(base: Path, config: dict, pre_build: Path, *, exit_code: int, image_id: str,
                   started_at: str, command: list[str], completed_at: datetime | None = None) -> dict:
    started = timestamp(started_at)
    finished = completed_at or datetime.now(timezone.utc)
    if started > finished:
        raise ValueError("构建开始时间晚于完成时间")
    # 即使构建过程中批准过期，仍登记失败收据和实际日志，不追认窗口外的构建。
    contract = network_inputs(config, now=started)
    before = bound(pre_build, RESUME_SCHEMA)
    if before["inputs"]["build_network"] != contract:
        raise ValueError("构建期间网络批准或输入漂移")
    check_command(command, contract["binding"]["network_mode"], base)
    if exit_code == 0 and not re.fullmatch(r"sha256:[a-f0-9]{64}", image_id):
        raise ValueError("成功构建缺少完整镜像摘要")
    window_ok = contract["approval"] is None or finished < timestamp(contract["approval"]["expires_at_utc"])
    return write_once(base / "artifacts/build-network-receipt.json", {
        "schema_version": NETWORK_SCHEMA, "status": "complete" if exit_code == 0 and window_ok else "failed",
        "contract": contract, "pre_build": binding(pre_build), "command": command,
        "exit_code": exit_code, "image_id": image_id, "docker_build_log": binding(base / "artifacts/docker-build.log"),
        "started_at_utc": started_at, "completed_at_utc": finished.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "approval_window_respected": window_ok,
    })


def verify_network(base: Path, config: dict, pre_build: Path, image_id: str) -> dict:
    receipt = bound(base / "artifacts/build-network-receipt.json", NETWORK_SCHEMA)
    contract = network_inputs(config)
    if (receipt.get("status") != "complete" or receipt.get("exit_code") != 0 or not receipt.get("approval_window_respected") or receipt.get("image_id") != image_id
            or receipt["contract"] != contract or receipt["pre_build"] != binding(pre_build)
            or receipt["docker_build_log"] != binding(base / "artifacts/docker-build.log")
            or bound(pre_build, RESUME_SCHEMA)["inputs"]["build_network"] != contract):
        raise ValueError("实际构建网络收据、镜像、日志或冻结输入漂移")
    check_command(receipt["command"], contract["binding"]["network_mode"], base)
    return binding(base / "artifacts/build-network-receipt.json")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("network-intent", "network-check", "record-trees", "record-network"))
    parser.add_argument("--base", type=Path)
    parser.add_argument("--vendor-mode", choices=("copied_previous", "regenerated_missing", "regenerated_changed"))
    parser.add_argument("--pre-build", type=Path)
    parser.add_argument("--exit-code", type=int, default=0)
    parser.add_argument("--image-id", default="")
    parser.add_argument("--started-at", default="")
    parser.add_argument("--command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    try:
        from driver_config import load_config
        config = CLI_CONFIG if CLI_CONFIG is not None else load_config()
        base = args.base or Path(config["B"])
        if args.action == "network-intent":
            value = {"schema_version": APPROVAL_SCHEMA, "status": "requires_manual_approval", "binding": network_binding(config)}
        elif args.action == "network-check":
            value = network_inputs(config)
            if args.pre_build and bound(args.pre_build, RESUME_SCHEMA)["inputs"]["build_network"] != value:
                raise ValueError("构建前的网络批准与冻结输入不一致")
        elif args.action == "record-trees":
            value = record_trees(base, config, args.vendor_mode)
        else:
            value = record_network(base, config, args.pre_build, exit_code=args.exit_code, image_id=args.image_id,
                                   started_at=args.started_at, command=args.command or [])
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        if args.action == "record-network" and not value["approval_window_respected"]:
            print("构建网络批准已过期，失败收据已登记，禁止使用该构建续跑", file=sys.stderr)
            return 3
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, build.CandidateBuildError) as error:
        print(f"VC-4 前提合同拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
