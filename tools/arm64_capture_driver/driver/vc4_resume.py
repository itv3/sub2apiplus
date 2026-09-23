#!/usr/bin/env python3
"""VC-4 续跑凭证：实际构建输入、本轮批准的成功门禁及产物一致才复用。

upload-wait 检查构建结束时冻结的中间凭证；full 还必须重放完整实现测试收据。
旧轮次没有凭证时不根据成功日志猜测可复用，重新执行后自然生成新版凭证。
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
from tools.official_client_capture import codex_upgrade as upgrade
from tools.official_client_capture import codex_upgrade_candidate_build as build
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts
from tools.official_client_capture import codex_upgrade_vc_receipt as vc_receipt

SCHEMA = "arm64-vc4-resume/v1"
TREES = ("source", "gate-tree", "build-tree", "plan-source")


def run(*command: str, cwd: Path | None = None) -> str:
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True,
                          timeout=300).stdout.strip()


def read(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"缺少可信普通文件：{path}")
    return json.loads(path.read_text())


def write_once(path: Path, value: dict) -> None:
    value = {**value, "binding_sha256": artifacts.digest(value)}
    with path.open("x") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def bound(path: Path) -> dict:
    value = read(path)
    if value.get("schema_version") != SCHEMA or value.get("binding_sha256") != artifacts.digest(
            {key: item for key, item in value.items() if key != "binding_sha256"}):
        raise ValueError(f"续跑凭证 schema 或摘要错误：{path}")
    return value


def binding(path: Path) -> dict:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"产物不存在或为链接：{path}")
    return {"sha256": build.file_sha256(path), "bytes": path.stat().st_size}


def image(reference: str, *, prepare: bool = False) -> dict:
    try:
        value = json.loads(run("docker", "image", "inspect", reference))[0]
    except subprocess.CalledProcessError:
        if not prepare:
            raise
        run("docker", "pull", "--platform", "linux/arm64", reference)
        value = json.loads(run("docker", "image", "inspect", reference))[0]
    if value.get("Os") != "linux" or value.get("Architecture") != "arm64":
        raise ValueError(f"镜像不是 linux/arm64：{reference}")
    return {"image_id": value["Id"], "repo_digests": sorted(value.get("RepoDigests") or [])}


def context() -> tuple[Path, Path, dict, dict]:
    base = Path(os.environ["B"])
    campaign = Path(os.environ["D"]) / "evidence/campaigns" / os.environ["NEW"]
    manifest = upgrade.load_campaign_manifest(campaign)
    classification = upgrade._load_stage_result(campaign, "classify")
    _, _, requirements = upgrade._load_vc3_gate_requirements(campaign, manifest, classification)
    return base, campaign, manifest, requirements


def source_digest(root: Path, *, plan: bool = False, injected_dist: bool = False) -> str:
    """计划副本保留派发输出；build tree 的 dist 由独立装配摘要约束，不混入源码输入。"""

    allowed = set()
    if plan:
        for path in root.glob("docs/egress/lifecycle/*/gate-plan.json"):
            for name in ("gate-plan-rehearsal.json", "gate-plan-dispatch.json"):
                generated = path.with_name(name)
                if generated.exists():
                    if generated.is_symlink() or generated.read_bytes() != path.read_bytes():
                        raise ValueError("计划副本的派发产物与批准计划不同")
                    allowed.add(generated.relative_to(root).as_posix())
    status = run("git", "status", "--porcelain", "--untracked-files=all", cwd=root)
    if any(line not in {"?? " + name for name in allowed} for line in status.splitlines()):
        raise ValueError(f"源码树不洁净：{root.name}")
    if not allowed and not injected_dist:
        return upgrade._directory_tree_digest(root)
    entries = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if (not path.is_file() or path.is_symlink() or relative.as_posix() in allowed
                or (injected_dist and relative.parts[:4] == ("backend", "internal", "web", "dist"))
                or any(part in upgrade.SKIP_DIRECTORIES for part in relative.parts)):
            continue
        entries.append({"path": relative.as_posix(), "size": path.stat().st_size, "sha256": build.file_sha256(path)})
    return upgrade._fingerprint({"entries": entries})


def inputs(*, prepare: bool = False) -> dict:
    base, _, manifest, requirements = context()
    trees = {}
    for name in TREES:
        root = base / name
        head = run("git", "rev-parse", "HEAD", cwd=root)
        if head != os.environ["C"]:
            raise ValueError(f"{name} HEAD 与 C 不一致")
        trees[name] = source_digest(root, plan=name == "plan-source", injected_dist=name == "build-tree")
    if len(set(trees.values())) != 1:
        raise ValueError("四棵树的实际源码摘要不相同")
    # 默认 Dockerfile 中的两项 ARG 和前端镜像一并绑定；不从镜像存在推断旧结果有效。
    dockerfile = (base / "source/Dockerfile.goreleaser").read_text()
    references = dict(re.findall(r"^ARG (ALPINE_IMAGE|POSTGRES_IMAGE)=([^\s]+)$", dockerfile, re.M))
    if set(references) != {"ALPINE_IMAGE", "POSTGRES_IMAGE"}:
        raise ValueError("Dockerfile 基础镜像参数不完整")
    references["NODE_IMAGE"] = "node:20-slim"
    images = {key: {"reference": ref, **image(ref, prepare=prepare)} for key, ref in references.items()}
    if any(not row["repo_digests"] for row in images.values()):
        raise ValueError("基础镜像缺少可固定的 RepoDigest")
    node = run("docker", "run", "--rm", "--network", "none", images["NODE_IMAGE"]["image_id"], "node", "--version")
    source = base / "source"
    go_version = run("go", "version", cwd=base / "build-tree/backend")
    if run("go", "version", cwd=base / "gate-tree/backend") != go_version:
        raise ValueError("门禁与构建树的 Go 工具链不一致")
    return {
        "subject": {"campaign_id": manifest["campaign_id"], "candidate_id": os.environ["CAND"],
                    "upgrade_id": os.environ["UP"], "campaign_purpose": manifest["campaign_purpose"],
                    "baseline_version": manifest["baseline_version"], "target_version": manifest["target_version"],
                    "attempt_id": None},
        "git_commit": os.environ["C"], "tree_sha256": trees, "target_architecture": "linux/arm64",
        "affected_gates": sorted(row["gate_id"] for row in requirements["requirements"] if row["kind"] == "affected_rule"),
        "requirements_sha256": artifacts.digest(requirements),
        "dependencies": {"go_mod": binding(source / "backend/go.mod"), "go_sum": binding(source / "backend/go.sum"),
                         "vendor_sha256": build.scan_tree_inventory(base / "build-tree/backend/vendor")["inventory_sha256"]},
        "toolchain": {"go_version": go_version, "node_version": node}, "base_images": images,
        "driver_build_sha256": {name: binding(Path(__file__).parent / name)["sha256"]
                                for name in ("trees.sh", "frontend.sh", "vc4-gates.sh", "implementation_gates.py", "build.sh", "vc4-facts.sh")},
        "frontend_deviation_approved_by": os.environ["FRONTEND_DEVIATION_APPROVED_BY"],
    }


def outputs(evidence: Path, current: dict) -> dict:
    base = Path(os.environ["B"])
    log = evidence / "logs/implementation.log"
    receipt = check_receipt(evidence, current) if (evidence / "receipt.json").exists() else None
    reuse = receipt["assertions"].get("reuse") if receipt else None
    reuse_all = bool(reuse and reuse["mode"] == "all")
    text = "" if reuse_all else log.read_text()
    successes = re.findall(r"^exit_code=(\d+)$", text, re.M)
    tree = current["tree_sha256"]["source"]
    _, _, _, requirements = context()
    if not reuse_all and (successes != ["0"] * len(requirements["requirements"]) or not re.search(r"^GATES_DONE ", text, re.M)
            or f"commit={current['git_commit']}\n" not in text
            or f"gate_tree_sha256={tree}\n" not in text
            or f"gate_tree_sha256_after={tree}\n" not in text):
        raise ValueError("本轮实现门禁未全部成功或没有绑定实际 TREE")
    params = read(base / "artifacts/build-parameters.json")
    binary = base / "artifacts/sub2api"
    build.validate_build_parameters(params, candidate_id=os.environ["CAND"], source_root=base / "source",
        git_commit=os.environ["C"], binary_path=binary, binary_sha256=build.file_sha256(binary),
        binary_bytes=binary.stat().st_size, build_tree=base / "build-tree", docker_context=base / "artifacts/ctx",
        frontend_dist_source=base / "frontend-dist", target_architecture="linux/arm64", image_id=params["docker_build"]["image_id"])
    actual_image = image(params["docker_build"]["image_id"])
    if params["frontend"]["node_version"] != current["toolchain"]["node_version"]:
        raise ValueError("前端实际 Node 版本与冻结输入不一致")
    # 三项装配校验保持正式构建合同，完整镜像运行复验仍由 record-candidate-build 执行。
    inventory = build.build_inventory_receipt(params, candidate_id=os.environ["CAND"],
        image_id=actual_image["image_id"], source_root=base / "source", build_tree=base / "build-tree",
        docker_context=base / "artifacts/ctx", binary_path=binary)
    frontend = build.build_frontend_provenance(params, candidate_id=os.environ["CAND"],
        image_id=actual_image["image_id"], source_root=base / "source", git_commit=os.environ["C"],
        build_tree=base / "build-tree", frontend_dist_source=base / "frontend-dist")
    return {"parameters_sha256": artifacts.digest(params), "image_id": actual_image["image_id"],
            "implementation_log": None if reuse_all else binding(log), "binary": binding(binary),
            "build_inventory_sha256": artifacts.digest(inventory), "frontend_provenance_sha256": artifacts.digest(frontend),
            "source_transition": binding(base / "artifacts/source-transition.json"),
            "built_at_utc": binding(base / "artifacts/built-at-utc.txt")}


def open_revision() -> None:
    """账本已允许新 revision 才承接；候选仍待审核时保持拒绝，不代替作废审核。"""

    _, campaign, manifest, _ = context()
    _, current = upgrade._current_candidate_revision_record(campaign, manifest)
    if current is None:
        initial, supersedes = True, None
    elif current["candidate_id"] == os.environ["CAND"]:
        initial = current["revision"] == 1
        supersedes = current["supersedes"]["candidate_id"] if current["supersedes"] else None
    else:
        initial, supersedes = False, current["candidate_id"]
    result = upgrade.open_candidate_revision(argparse.Namespace(campaign_dir=campaign,
        candidate_id=os.environ["CAND"], initial=initial, supersedes=supersedes))
    print(f"REVISION_OPENED r{result['revision']} {result['candidate_id']}")


def build_flags(evidence: Path, default: str) -> str:
    """只换制品且源码相同，保留原 ldflags，避免构建日期本身导致实现测试输入失效。"""

    _, campaign, manifest, _ = context()
    _, revision = upgrade._current_candidate_revision_record(campaign, manifest)
    if revision is None or revision["supersedes"] is None:
        return default
    path = campaign / "candidates" / revision["supersedes"]["candidate_id"] / "build-receipt.json"
    if not path.exists():
        return default
    previous = artifacts.validate_candidate_build_receipt(read(path))
    current = bound(evidence / "pre-build.json")["inputs"]
    if (previous["source"]["git_commit"] != current["git_commit"]
            or previous["source"]["tree_sha256"] != current["tree_sha256"]["source"]):
        return default
    command = previous["build"]["parameters"]["go_build"]["command"]
    if command.count("-ldflags") != 1 or command.index("-ldflags") + 1 >= len(command):
        return default
    return command[command.index("-ldflags") + 1]


def early_test_mode(evidence: Path) -> str:
    """确定必然全量重跑时保留前端／门禁并行；可能复用时等真实构建参数齐全再裁定。"""

    _, campaign, manifest, requirements = context()
    _, revision = upgrade._current_candidate_revision_record(campaign, manifest)
    if revision is None or revision["supersedes"] is None:
        return "full"
    path = campaign / "candidates" / revision["supersedes"]["candidate_id"] / "build-receipt.json"
    if not path.exists():
        return "full"
    previous = artifacts.validate_candidate_build_receipt(read(path))["build"].get("inputs")
    if previous is None:
        return "full"
    current = bound(evidence / "pre-build.json")["inputs"]
    known = {"source_tree_sha256": current["tree_sha256"]["source"],
        "go_mod_sha256": current["dependencies"]["go_mod"]["sha256"],
        "go_sum_sha256": current["dependencies"]["go_sum"]["sha256"],
        "vendor_sha256": current["dependencies"]["vendor_sha256"],
        "go_version": current["toolchain"]["go_version"].split()[2],
        "node_version": current["toolchain"]["node_version"],
        "base_images": {key: row["repo_digests"][0] for key, row in current["base_images"].items()},
        "target_architecture": current["target_architecture"], "requirements_sha256": requirements["requirements_sha256"]}
    return "full" if any(value != previous[key] for key, value in known.items()) else "deferred"


def check_receipt(evidence: Path, current: dict) -> dict:
    receipt = vc_receipt.replay(evidence, "receipt.json")
    assertions = receipt["assertions"]
    affected = sorted(row["gate_id"] for row in assertions["gates"] if row["kind"] == "affected")
    if (receipt["kind"] != "implementation_tests" or receipt["status"] != "passed"
            or receipt["subject"] != current["subject"] or assertions["git_commit"] != current["git_commit"]
            or assertions["source_tree_sha256"] != current["tree_sha256"]["source"]
            or assertions["target_architecture"] != current["target_architecture"]
            or affected != current["affected_gates"]):
        raise ValueError("完整收据未绑定当前源码树、架构、Campaign 或 affected 闭集")
    return receipt


def verify(evidence: Path, mode: str) -> dict:
    checkpoint = bound(evidence / "upload-wait.json")
    if checkpoint.get("stage") != "upload-wait":
        raise ValueError("没有等待前阶段成功凭证")
    current = inputs()
    if checkpoint["inputs"] != current:
        raise ValueError("构建输入漂移；旧日志和镜像不能作为续跑依据")
    if checkpoint["outputs"] != outputs(evidence, current):
        raise ValueError("等待前产物或构建参数摘要漂移")
    if mode == "full" or (evidence / "receipt.json").exists():
        check_receipt(evidence, current)
    return checkpoint


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "record-upload-wait", "check", "open-revision", "build-flags", "early-test-mode"))
    parser.add_argument("--evidence-root", type=Path, required=True)
    parser.add_argument("--mode", choices=("upload-wait", "full"), default="full")
    parser.add_argument("--default", default="")
    args = parser.parse_args()
    try:
        evidence = args.evidence_root.resolve(strict=True)
        if args.action == "open-revision":
            open_revision()
            return 0
        if args.action == "build-flags":
            print(build_flags(evidence, args.default))
            return 0
        if args.action == "early-test-mode":
            print(early_test_mode(evidence))
            return 0
        if args.action == "prepare":
            write_once(evidence / "pre-build.json", {"schema_version": SCHEMA, "stage": "pre-build",
                                                     "inputs": inputs(prepare=True)})
        elif args.action == "record-upload-wait":
            before = bound(evidence / "pre-build.json")
            current = inputs()
            if before["inputs"] != current:
                raise ValueError("门禁／构建执行期间输入漂移，不能登记成功")
            write_once(evidence / "upload-wait.json", {"schema_version": SCHEMA, "stage": "upload-wait",
                "inputs": current, "outputs": outputs(evidence, current)})
        else:
            verify(evidence, args.mode)
        print(f"VC4_INPUTS_VERIFIED action={args.action} mode={args.mode}")
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError, upgrade.ConfigurationError,
            build.CandidateBuildError, vc_receipt.VCReceiptError) as error:
        print(f"VC-4 续跑拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
