#!/usr/bin/env python3
"""Claude 的 VC-4 派发（Claude 升级 B3-04a，``vc4-claude.sh`` 调用）：两个批次的动作计划、plan 预演与 record 零写入预览、收尾摘要。

为什么不沿用 Codex 的 ``vc4.sh``：它的计划命令写死 ``codex_upgrade.py``，plan 预演进程内调 ``codex_upgrade.plan_candidate_gates``，
record 预演替换 ``codex_upgrade`` 的三个内部写点（构建收据路径、阶段完成、revision seal）并比对 Campaign 写域。Claude 的
接口由路线二在设计 B3-04「第三步实施结果」答复：

* 两个子命令的批次动作必须是受管 Python 直接调 ``claude_upgrade.py``、旗标精确闭合（``claude_upgrade_batch`` 的阶段幂等重派
  合同 ``STAGE_REPLAY_CONTRACTS["VC-4"]``）——计划一律经 ``claude_upgrade_batch.managed_action``／``action_plan`` 生成，生成后
  再按合同自检；
* ``record-candidate-build --preview`` 是零写入预览（全部复算与 revision seal 预检，退出码 2，父监督器里拒绝），代替 Codex
  那种替换内部写点的预演；
* ``plan-candidate-gates`` 的生成模式只写一次输出：预演在批次外以同一组旗标跑一次、输出换成 ``gate-plan-rehearsal.json``，与
  提交内的 ``gate-plan.json`` 逐字节比对后删除。

子命令（只读，或只写本轮输入目录 W 下的动作计划与候选计划副本 ``plan-source`` 里的预演文件）：

* ``tree``：打印候选源码树摘要（编排器 ``_directory_tree_digest``，与实现门禁同一算法）；
* ``plan-gates --output <W 下的计划>``：plan-candidate-gates 批次计划（``--candidate-source`` 是候选目录的 ``plan-source``，
  ``--output`` 是其中的 ``gate-plan-dispatch.json``）；
* ``plan-rehearsal``：批次外生成模式预演，比对后删除预演文件；
* ``record-plan --evidence-root <E> --output <W 下的计划>``：record-candidate-build 批次计划（十九个旗标，用途取 Campaign
  冻结的 ``campaign_purpose``，平台取 Campaign 平台）；
* ``record-preview --plan <计划>``：同一组旗标加 ``--preview`` 在批次外跑一次，前后比对 Campaign 写域（``candidates``、
  ``control/vc``、``control-store``）全部文件摘要；
* ``summary``：VC-4 checkpoint、候选构建收据（客户端中立合同）与计时账本状态。

只给 VC-4 派发脚本为 ``vc4-claude.sh`` 的客户端用（客户端表 ``vc4_dispatch_script``），其余客户端拒绝。退出码：0 成功；3 拒绝。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True
sys.path.insert(0, os.environ.get("D", str(Path(__file__).resolve().parents[3])))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import vc4_client  # noqa: E402

from tools.official_client_capture import codex_upgrade_timing_ledger as timing_ledger  # noqa: E402
from tools.official_client_capture import codex_upgrade_vc_artifacts as artifacts  # noqa: E402

DISPATCH_SCRIPT = "vc4-claude.sh"
PLAN_ACTION = "plan-candidate-gates"
RECORD_ACTION = "record-candidate-build"
PLAN_TIMEOUT_SECONDS = 900
RECORD_TIMEOUT_SECONDS = 1800
TARGET_ARCHITECTURE = "linux/arm64"
# record --preview 零写入的核对范围：候选目录、VC 控制制品与受管 Store（与 record-candidate-build 的写域相同）。
WRITE_DOMAIN = ("candidates", "control/vc", "control-store")


def _binding() -> vc4_client._Binding:
    """本轮客户端的绑定；VC-4 派发脚本不是本脚本的客户端在加载编排器之前就拒绝。"""

    row = vc4_client.parse_env.client_table().spec(vc4_client.client())
    if row["vc4_dispatch_script"] != DISPATCH_SCRIPT:
        raise ValueError(f"本轮客户端（{row['client']}）的 VC-4 派发脚本是 {row['vc4_dispatch_script']}，不是 {DISPATCH_SCRIPT}")
    return vc4_client.current()


def _batch() -> Any:
    from tools.official_client_capture import claude_upgrade_batch

    return claude_upgrade_batch


def _campaign() -> Path:
    return Path(os.environ["NEWDIR"])


def _lifecycle(tree: str) -> Path:
    return Path(os.environ["B"]) / tree / os.environ["LIFECYCLE_DIR"]


def _write_plan(path: Path, plan: dict[str, Any]) -> None:
    """计划只写进本轮输入目录 W（同一路径重跑时覆盖，0600）。"""

    if path.parent.resolve(strict=True) != Path(os.environ["W"]).resolve(strict=True):
        raise ValueError(f"动作计划只能写在本轮输入目录 W 下：{path}")
    if path.is_symlink():
        raise ValueError(f"动作计划路径是符号链接：{path}")
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.chmod(0o600)


def _checked_plan(action: dict[str, Any], campaign: Path) -> dict[str, Any]:
    """按共享合同生成批次计划，再按 Claude 的 VC-4 阶段幂等重派合同自检（形态不闭合即拒绝，不等派发失败）。"""

    batch = _batch()
    plan = batch.action_plan([action], phase="VC-4")
    subcommand, mode, _flags = batch._parse_contract("VC-4", batch._orchestrator_argv(action["command"]), campaign)
    if (subcommand, action["operation"]) != (action["action_id"], f"VC-4:{action['action_id']}"):
        raise ValueError(f"动作 {action['action_id']} 与子命令 {subcommand}（{mode}）不一致")
    return plan


def plan_gates_argv(output_name: str) -> list[str]:
    plan_source = Path(os.environ["B"]) / "plan-source"
    lifecycle = _lifecycle("plan-source")
    return [
        PLAN_ACTION, "--campaign-dir", str(_campaign()), "--candidate-id", os.environ["CAND"],
        "--candidate-source", str(plan_source), "--mapping", str(lifecycle / "gate-mapping.json"),
        "--output", str(lifecycle / output_name),
    ]


def plan_gates(output: Path) -> dict[str, Any]:
    _binding()
    action = _batch().managed_action(
        PLAN_ACTION, f"VC-4:{PLAN_ACTION}", plan_gates_argv("gate-plan-dispatch.json"), item_ids=[PLAN_ACTION], timeout_seconds=PLAN_TIMEOUT_SECONDS
    )
    plan = _checked_plan(action, _campaign())
    _write_plan(output, plan)
    print(f"plan-candidate-gates 计划 -> {output}")
    return plan


def plan_rehearsal() -> None:
    """批次外跑一次生成模式（输出换成预演文件），与提交内 gate-plan.json 逐字节比对后删除。"""

    binding = _binding()
    rehearsal = _lifecycle("plan-source") / "gate-plan-rehearsal.json"
    committed = _lifecycle("source") / "gate-plan.json"
    if rehearsal.exists() or rehearsal.is_symlink():
        raise ValueError(f"上一次预演文件还在：{rehearsal}（vc4-claude.sh 预演前先删掉）")
    try:
        code, result = binding.cli(plan_gates_argv(rehearsal.name))
        if (code, result.get("status"), result.get("mode")) != (0, "complete", "gate_plan"):
            raise ValueError(f"plan 预演没有完成：退出码 {code}，状态 {result.get('status')}，模式 {result.get('mode')}")
        if rehearsal.read_bytes() != committed.read_bytes():
            raise ValueError("预演 plan 与提交内 gate-plan.json 不是逐字相同")
        print(f"预演结果：{json.dumps({key: result.get(key) for key in ('status', 'gate_count', 'plan_sha256', 'live_request_count')}, ensure_ascii=False)}")
        print("预演 plan 与提交内 gate-plan.json 逐字一致")
    finally:
        rehearsal.unlink(missing_ok=True)


def record_argv(evidence: Path) -> list[str]:
    binding = _binding()
    base = Path(os.environ["B"])
    manifest = binding.upgrade.load_campaign_manifest(_campaign())
    platform = str((manifest.get("configuration") or {}).get("platform", ""))
    if platform != TARGET_ARCHITECTURE:
        raise ValueError(f"候选构建只产出 {TARGET_ARCHITECTURE}，Campaign 平台是 {platform or '（缺）'}")
    parameters = json.loads((base / "artifacts" / "build-parameters.json").read_text(encoding="utf-8"))
    image_id = str(parameters["docker_build"]["image_id"])
    if not image_id.startswith("sha256:") or len(image_id) != 71:
        raise ValueError(f"构建参数里的镜像 ID 不合法：{image_id}")
    built_at = (base / "artifacts" / "built-at-utc.txt").read_text(encoding="utf-8").strip()
    # 构建标识与 Codex vc4.sh 同一规则：候选、提交前 9 位与构建时刻（去掉 - 与 :、小写）。
    build_id = f"{os.environ['CAND']}-{os.environ['C'][:9]}-{built_at.replace('-', '').replace(':', '').lower()}"
    return [
        RECORD_ACTION,
        "--campaign-dir", str(_campaign()),
        "--candidate-id", os.environ["CAND"],
        "--candidate-purpose", str(manifest["campaign_purpose"]),
        "--candidate-source", str(base / "source"),
        "--candidate-binary", str(base / "artifacts" / "sub2api"),
        "--runtime-image", f"{os.environ['CANDIDATE_IMAGE_REPOSITORY']}@{image_id}",
        "--candidate-image-id", image_id,
        "--build-id", build_id,
        "--deployed-version", os.environ["TARGET_VERSION"],
        "--target-architecture", TARGET_ARCHITECTURE,
        "--build-parameters", str(base / "artifacts" / "build-parameters.json"),
        "--build-tree", str(base / "build-tree"),
        "--docker-context", str(base / "artifacts" / "ctx"),
        "--frontend-dist-source", str(base / "frontend-dist"),
        "--catalog-stage-dir", str(_lifecycle("source") / "catalog-stage"),
        "--source-transition", str(base / "artifacts" / "source-transition.json"),
        "--gate-plan", str(_lifecycle("source") / "gate-plan.json"),
        "--implementation-test-root", str(evidence),
        "--implementation-test-receipt", str(evidence / "receipt.json"),
    ]


def record_plan(evidence: Path, output: Path) -> dict[str, Any]:
    argv = record_argv(evidence)
    action = _batch().managed_action(RECORD_ACTION, f"VC-4:{RECORD_ACTION}", argv, item_ids=[RECORD_ACTION], timeout_seconds=RECORD_TIMEOUT_SECONDS)
    plan = _checked_plan(action, _campaign())
    _write_plan(output, plan)
    print(f"record-candidate-build 计划已写: {output}（build_id={argv[argv.index('--build-id') + 1]}）")
    return plan


def write_domain_digests(campaign: Path) -> dict[str, str]:
    """record-candidate-build 写域内全部条目的摘要（符号链接与特殊文件按类型记，判零写入用）。"""

    rows: dict[str, str] = {}
    for name in WRITE_DOMAIN:
        root = campaign / name
        if not root.exists():
            continue
        for directory, subdirectories, files in os.walk(root, followlinks=False):
            current = Path(directory)
            for entry in sorted(subdirectories):
                rows[(current / entry).relative_to(campaign).as_posix() + "/"] = "directory"
            for entry in sorted(files):
                path = current / entry
                key = path.relative_to(campaign).as_posix()
                rows[key] = "symlink" if path.is_symlink() else (hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "special")
    return rows


def record_preview(plan_path: Path) -> dict[str, Any]:
    binding = _binding()
    plan = artifacts.validate_action_plan(json.loads(plan_path.read_text(encoding="utf-8")), phase="VC-4")
    (action,) = plan["actions"]
    argv = _batch()._orchestrator_argv(action["command"])
    if not argv or argv[0] != RECORD_ACTION:
        raise ValueError("record 预览只接受 record-candidate-build 计划")
    campaign = _campaign()
    before = write_domain_digests(campaign)
    code, result = binding.cli([*argv, "--preview"])
    after = write_domain_digests(campaign)
    if before != after:
        changed = sorted(set(before.items()) ^ set(after.items()))
        raise ValueError(f"零写入预览在 Campaign 写域留下了变更：{changed[:10]}")
    if (code, result.get("status"), result.get("mode")) != (2, "preview", "record_build_preview"):
        raise ValueError(f"record 预览没有给出预览结果：退出码 {code}，状态 {result.get('status')}，模式 {result.get('mode')}")
    print(f"预演零副作用：Campaign 写域快照前后一致，条目数 {len(after)}")
    summary = {key: result.get(key) for key in ("status", "candidate_id", "candidate_revision", "release_sha256", "live_request_count")}
    summary["writes"] = len(result.get("writes") or [])
    summary["revision_seal"] = result.get("revision_seal")
    print(f"预演结果: {json.dumps(summary, ensure_ascii=False)}")
    return result


def summary() -> None:
    binding = _binding()
    upgrade = binding.upgrade
    campaign = _campaign()
    manifest = upgrade.load_campaign_manifest(campaign)
    revision, record = upgrade._current_candidate_revision_record(campaign, manifest)
    if revision is None:
        raise ValueError("没有账本激活的候选 revision")
    plan = upgrade._vc_campaign_plan(campaign, manifest)
    path, checkpoint = upgrade._replay_vc_checkpoint(campaign, plan, "VC-4", revision=revision)
    print("VC-4 checkpoint:", {"revision": revision, "path": path.relative_to(campaign).as_posix(),
                               **{key: checkpoint.get(key) for key in ("phase", "status", "completed_at_utc")},
                               "checkpoint_sha256": str(checkpoint.get("checkpoint_sha256"))[:16]})
    receipt_path = upgrade._candidate_build_receipt_path(campaign, os.environ["CAND"])
    receipt = artifacts.validate_official_client_candidate_build_receipt(json.loads(receipt_path.read_text(encoding="utf-8")))
    print("build-receipt:", {key: receipt.get(key) for key in ("client", "candidate_revision", "candidate_id", "receipt_digest")},
          "image:", receipt["image"]["image_id"], "| source:", receipt["source"]["git_commit"][:9], "| build_id:", receipt["build"]["build_id"])
    ledger = timing_ledger.inspect_ledger(upgrade._campaign_timing_ledger_dir(campaign, manifest))
    print("账本:", {key: ledger.get(key) for key in ("status", "active_phase", "head_sequence")})


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Claude 的 VC-4 派发（B3-04a）")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("tree", help="候选源码树摘要")
    gates = sub.add_parser("plan-gates", help="plan-candidate-gates 批次计划")
    gates.add_argument("--output", type=Path, required=True)
    sub.add_parser("plan-rehearsal", help="批次外生成模式预演并比对")
    record = sub.add_parser("record-plan", help="record-candidate-build 批次计划")
    record.add_argument("--evidence-root", type=Path, required=True)
    record.add_argument("--output", type=Path, required=True)
    preview = sub.add_parser("record-preview", help="record-candidate-build 零写入预览")
    preview.add_argument("--plan", type=Path, required=True)
    sub.add_parser("summary", help="VC-4 checkpoint、候选构建收据与账本状态")
    args = parser.parse_args(argv)
    try:
        if args.action == "tree":
            print(_binding().upgrade._directory_tree_digest(Path(os.environ["B"]) / "source"))
        elif args.action == "plan-gates":
            plan_gates(args.output)
        elif args.action == "plan-rehearsal":
            plan_rehearsal()
        elif args.action == "record-plan":
            record_plan(args.evidence_root.resolve(strict=True), args.output)
        elif args.action == "record-preview":
            record_preview(args.plan)
        else:
            summary()
        return 0
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(f"Claude VC-4 派发拒绝：{error}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
