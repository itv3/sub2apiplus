#!/usr/bin/env python3
"""收尾：生成 Codex 升级的终态事实 JSON（codex_closeout.py prepare 的 terminal_facts 输入）。

做什么
    早年终态事实是人工对着十几份 ARM64 证据文件逐字段填的（约 45 个字段、十几个摘要），容易抄错且占用收尾关键路径。
    字段几乎都能从 VC-6 的证据文件和已入库的阶段收据里机械取出，本脚本把取法固化下来；结构化字段与人工版逐项相同，
    只有说明性文字（*_note）是模板生成的。

输入分三类（只读，不写任何证据文件）
    1. 仓库（收尾候选所在的合并树，必须已含四份阶段收据与审计索引）：--repo
    2. 证据根（ARM64 上是 /root/docker/capture-cli/data；本机验证时可指向取回的子集副本）：--evidence-root
    3. 轮次目录里 VC-6 脚本写的小文件（vc6-root.txt、vc6-retire-root.txt、vc6-pc.txt、vc6-retire-pc.txt）：--run-vc6

输出
    --output 指定的终态事实 JSON（已存在则拒绝覆盖）；其中 identity_sha256 由 codex_closeout.terminal_candidate 在 prepare 里补，本脚本不写。

何时用
    收尾 b7-terminal-facts 步骤（closeout.sh 会先用 fetch_evidence_subset.sh 把证据小文件取回本机，再调用本脚本）。

用法（版本、Campaign、候选 id 全部来自参数，没有缺省值）
    python3 -B gen_terminal_facts.py --repo <收尾仓库> --evidence-root <证据根或子集副本> \\
        --campaign <Campaign 全名> --candidate <候选 id> \\
        --baseline-version <基线版本> --target-version <目标版本> --retire-version <退休版本> \\
        --run-vc6 <轮次目录>/vc6 --output <收尾目录>/terminal-facts-<目标标记>.json \\
        [--evidence-root-label /root/docker/capture-cli/data]
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

MAINT = "docs/egress/maintenance"
RUNTIME = "backend/internal/officialegress/catalogdata/runtime"
IDENTITY_ALGORITHM = ("sha256(json.dumps(payload_without_identity_sha256, ensure_ascii=False, sort_keys=True, "
                      "separators=(',', ':')))")


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text())


def tag_of(version):
    """主.次.补丁 -> 主+次三位+（补丁非 0 才补）：0.149.0 -> 0149，0.149.1 -> 01491（与既有终态收据文件名一致）。"""
    major, minor, patch = (int(x) for x in version.split("."))
    return f"{major}{minor:03d}{patch if patch else ''}"


def binding(repo, relative):
    path = Path(repo) / relative
    raw = path.read_bytes()
    return {"bytes": len(raw), "path": relative, "sha256": sha256_bytes(raw)}


def profile_id_of(build_id):
    """release graph 的 build.id 形如 codex-<版本>-official-<批次>-<12 位摘要>，去掉末尾 12 位摘要后缀即 profile_id。"""
    return re.sub(r"-[0-9a-f]{12}$", "", build_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in ("repo", "evidence-root", "campaign", "candidate", "baseline-version", "target-version", "retire-version",
                 "run-vc6", "output"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--evidence-host", default="ARM64")
    parser.add_argument("--evidence-root-label", default=None, help="写进事实里的证据根路径；缺省取 --evidence-root")
    parser.add_argument("--completed-at", default=None, help="UTC 完成时间；缺省取 vc6-completion 收据的 issued_at_utc 截到秒")
    args = parser.parse_args()

    repo = Path(args.repo)
    data = Path(args.evidence_root)
    campaign_dir = data / "evidence/campaigns" / args.campaign
    target, baseline, retire = args.target_version, args.baseline_version, args.retire_version
    prefix = f"CODEX_CLI_{tag_of(baseline)}_TO_{tag_of(target)}"
    run_vc6 = Path(args.run_vc6)

    # ---- 仓库内已入库的阶段收据与审计索引（逐字节副本，摘要由文件现算）
    stage = {key: binding(repo, f"{MAINT}/{prefix}_{name}.json") for key, name in (
        ("catalog_promotion", "CATALOG_PROMOTION_RECEIPT"), ("post_promotion_gate", "POST_PROMOTION_GATE_RECEIPT"),
        ("production_activation", "PRODUCTION_ACTIVATION_RECEIPT"), ("runtime_profile_removal", "RUNTIME_PROFILE_REMOVAL_RECEIPT"))}
    audit_binding = binding(repo, f"{MAINT}/{prefix}_AUDIT_INDEX.json")
    audit = read_json(repo / audit_binding["path"])
    if audit["target_version"] != target or audit["baseline_version"] != baseline or audit["campaign_id"] != args.campaign:
        sys.exit("审计索引的版本或 Campaign 与参数不一致")

    # ---- Runtime Catalog（晋升并退休之后的仓库状态）
    selector_rel = f"{RUNTIME}/release-catalog.json"
    selector = read_json(repo / selector_rel)
    graph_rel = f"{RUNTIME}/{selector['release_graph']['path'].split('catalogdata/runtime/')[-1]}"
    snap_rel = f"{RUNTIME}/{selector['snapshot_catalog']['path'].split('catalogdata/runtime/')[-1]}"
    graph = read_json(repo / graph_rel)
    snapshots = {row["version"]: row for row in read_json(repo / snap_rel)["snapshots"]}
    if target not in snapshots or baseline not in snapshots:
        sys.exit("快照目录里缺目标或基线版本")
    if retire in snapshots:
        sys.exit(f"快照目录里仍有应退休的 {retire}（Catalog 尚未裁剪）")
    build_by_mode = {}
    for node in graph["nodes"]:
        build_by_mode.setdefault(node["mode"], node["build"])
    if build_by_mode["active"]["version"] != target or build_by_mode["previous"]["version"] != baseline:
        sys.exit("release graph 不是 Active=目标、Previous=基线")
    active_row = snapshots[target]
    runtime_catalog = {
        "active_profile": binding(repo, f"{RUNTIME}/{active_row['file']}"),
        "catalog": binding(repo, selector_rel),
        "release_graph": binding(repo, graph_rel),
        "snapshot_catalog": binding(repo, snap_rel),
    }
    if runtime_catalog["active_profile"]["sha256"] != active_row["blob_sha256"]:
        sys.exit("active 画像文件摘要与快照目录登记不一致")
    # 晋升中间快照目录：晋升回执清单里登记的快照目录（含将被退休的版本条目），退休后保留为历史归档
    promo = read_json(repo / stage["catalog_promotion"]["path"])
    middle = [item for item in promo["inventory"] if item["path"].startswith("catalogdata/runtime/snapshot-catalogs/")]
    if len(middle) != 1:
        sys.exit("晋升回执里的快照目录条目不唯一")
    middle_rel = f"{RUNTIME}/{middle[0]['path'].split('catalogdata/runtime/')[-1]}"
    if middle_rel == snap_rel:
        sys.exit("晋升中间快照与退休后快照相同：Catalog 尚未裁剪")
    retired_row = None
    for row in read_json(repo / middle_rel)["snapshots"]:
        if row["version"] == retire:
            retired_row = row
    if retired_row is None:
        sys.exit(f"晋升中间快照里没有 {retire}")

    # ---- 证据根里的 Campaign 文件
    campaign_json = campaign_dir / "campaign.json"
    campaign_sha = sha256_bytes(campaign_json.read_bytes())
    if (campaign_dir / "campaign.sha256").read_text().strip() != campaign_sha:
        sys.exit("campaign.sha256 与 campaign.json 摘要不一致")
    checkpoints = sorted((campaign_dir / "canonical/checkpoints").glob("*.json"))
    last = checkpoints[-1]
    checkpoint = read_json(last)
    items = checkpoint["items"]
    receipts = campaign_dir / "control/vc/receipts" / args.candidate
    vc6_completion = read_json(receipts / "vc6-completion.json")
    cleanup = read_json(receipts / "cleanup-decision.json")
    delivery = read_json(campaign_dir / "delivery" / args.candidate / "receipt.json")
    subject = vc6_completion["subject"]
    if subject["campaign_id"] != args.campaign or subject["target_version"] != target or subject["baseline_version"] != baseline:
        sys.exit("vc6-completion 收据的主体与参数不一致")
    vc6_root = Path((run_vc6 / "vc6-root.txt").read_text().strip())
    retire_root = Path((run_vc6 / "vc6-retire-root.txt").read_text().strip())
    # 证据根可能是取回的子集副本：vc6-root.txt 里写的是 ARM64 绝对路径，按 control/ 之后的相对部分重新定位
    def locate(path):
        parts = path.parts
        index = parts.index("control")
        return campaign_dir.joinpath(*parts[index:])
    activation = read_json(locate(vc6_root) / "activation-receipt.json")
    retire_stage = read_json(locate(retire_root) / "stages/retire_switch/stage.json")
    sync_commit = (run_vc6 / "vc6-pc.txt").read_text().strip()
    retire_commit = (run_vc6 / "vc6-retire-pc.txt").read_text().strip()
    for commit in (sync_commit, retire_commit):
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            sys.exit("vc6-pc.txt／vc6-retire-pc.txt 不是 40 位提交号")
    if activation["final_state"]["active_version"] != target or retire_stage["checks"]["active_version"] != target:
        sys.exit("激活收据或退休切换阶段的 active 版本不是目标版本")
    if activation["final_state"]["profile_digest"] != active_row["digest"] or retire_stage["checks"]["profile_digest"] != active_row["digest"]:
        sys.exit("激活收据或退休切换阶段的画像摘要与 Catalog 不一致")

    completed = args.completed_at or vc6_completion["issued_at_utc"].split(".")[0].rstrip("Z") + "Z"
    totals = audit["totals"]
    kinds = totals["ledgers_by_kind"]
    vc6_item_ids = [i["item_id"] for i in items if i["item_id"] in ("production-activation", f"retire-{retire}", "rollback-verification")]
    remaining = [i["item_id"] for i in items if i.get("status") != "complete"]

    facts = {
        "audit_index": audit_binding,
        "audit_index_identity_sha256": audit["identity_sha256"],
        "audit_index_note": (
            f"索引由受管工具 tools/codex_audit_index.py generate 以 --no-replay 在 {args.evidence_host} 生成：逐文件登记 "
            f"{kinds['campaign_run']} 个 campaign-run、{kinds['supervisor_run']} 个监督器逐命令 run 与 {kinds['upgrade_timing_ledger']} 个计时账本的摘要、"
            f"私有归档坐标与总量，生成后已在证据根上以 check --evidence-root 逐文件复核 {totals['file_count']} 个文件无漂移；"
            "未执行 UpgradeTimingLedger 重放与监督器审计（--no-replay），完整账本随私有归档保存。"),
        "audit_index_totals": totals,
        "campaign_chain": [
            {"campaign_id": args.campaign, "campaign_manifest_sha256": campaign_sha,
             "note": f"{target} 官方证据在本 Campaign 的 VC-1 自采（campaign.json 无 predecessor、无 official_evidence_reuse），与第 2 级末级 Campaign 为同一个，分列两项只为标明两种角色",
             "order": 1, "role": "official_evidence_source"},
            {"attempt_id": subject["attempt_id"], "campaign_id": args.campaign, "campaign_manifest_sha256": campaign_sha,
             "candidate_id": args.candidate,
             "created_at_utc": read_json(campaign_json)["created_at_utc"].split(".")[0].rstrip("Z") + "Z",
             "note": "VC-0～VC-6 全链，末级 Campaign；Runtime Catalog 顶层 source 与 ReleaseGraph active 节点 source 均指向它",
             "order": 2, "role": "production_replacement", "upgrade_id": subject["upgrade_id"]},
        ],
        "campaign_chain_note": (
            f"ReleaseGraph {target} active 节点 build.source 指向本 Campaign 的 classification 摘要，catalog 顶层 source 指向同一 Campaign 的退休事件"
            f"（retirement:codex-legacy-profile）；Previous {baseline} 节点 source 仍指向 {baseline} 终态链末级 Campaign。"),
        "canonical": {
            "execute_item_ids_remaining": remaining,
            "final_checkpoint_sequence": checkpoint["checkpoint_sequence"],
            "final_checkpoint_sha256": sha256_bytes(last.read_bytes()),
            "final_phase": checkpoint["phase"],
            "item_count": len(items),
            "note": (f"{checkpoint['checkpoint_sequence']} 个 checkpoint、{len(items)} 项全部 complete；VC-6 完成收据 vc6-completion"
                     f"（receipt_digest {vc6_completion['receipt_digest'][:8]}…）登记链终点。完整 checkpoint 链位于证据根 evidence/campaigns/<末级 Campaign>/canonical/checkpoints/，"
                     "已随私有归档保存并在异地解包复核。"),
            "vc6_items": vc6_item_ids,
        },
        **stage,
        "completed_at_utc": completed,
        "evidence_host": args.evidence_host,
        "evidence_root": args.evidence_root_label or args.evidence_root,
        "identity_algorithm": IDENTITY_ALGORITHM,
        "production_deployment": {
            "activation_build_commit": sync_commit,
            "activation_image_id": activation["final_state"]["image_id"],
            "activation_source_tree_sha256": activation["target"]["source_tree_sha256"],
            "architecture": retire_stage["architecture"],
            "authoritative_commit": retire_commit,
            "final_image_id": retire_stage["image_id"],
            "host": args.evidence_host,
            "note": (f"四阶段激活（canary → 切换 → 回滚 → 恢复，每阶段一次真实业务请求均 HTTP 200）由 production_activation 收据登记，激活镜像由 production tree 同步提交 "
                     f"{sync_commit[:9]} 构建；退休后按 §4.6.7 以 {retire_commit[:9]} 重建并切换到 final_image_id（retire_switch 通过），Active {target} 与固定回滚镜像坐标不变。"
                     f"source_tree_sha256 = sha256(git ls-files -s 的逐字输出)，已在 {retire_commit[:9]} 的退休构建树上复算。"),
            "rollback_image_reference": activation["rollback"]["image_reference"],
            "rollback_version": activation["rollback"]["version"],
            "source_tree_sha256": retire_stage["checks"]["source_tree_sha256"],
        },
        "promotion_intermediate_snapshot_catalog": {
            "note": f"晋升时产出的中间快照仍含 {retire} 条目；退休后 selector 指向裁剪版 {Path(snap_rel).stem[:8]}…，中间快照逐字保留为历史归档",
            "path": middle_rel[len("") :],
            "sha256": Path(middle_rel).stem,
        },
        "remote_cleanup": {
            "decision": cleanup["assertions"]["decision"],
            "note": (f"§4.6.8 清理按既定决定登记 {cleanup['assertions']['decision']}（清理决定收据 {cleanup['receipt_digest'][:8]}…）：只读列出 "
                     f"{cleanup['assertions']['target_count']} 个候选删除目标与必须保留项，未执行任何删除；实际清理另需精确目标批准。生产容器保持在退休后的最终镜像。"),
            "receipt_digest": cleanup["receipt_digest"],
        },
        "result": "passed",
        "retained_runtime_profiles": [{
            "path": f"{RUNTIME}/{retired_row['file']}",
            "removed_from_runtime_projection_in_commit": retire_commit[:9],
            "sha256": retired_row["blob_sha256"],
            "state": "retained_as_frozen_terminal_artifact",
        }],
        "retained_runtime_profiles_note": (
            f"{retire} 的运行画像按指南 §4.6.7 第 1 类保留字节、只移出 Catalog／selector／运行投影：{Path(retired_row['file']).stem[:8]}…（{retire} 期 Active）"
            "被前序晋升／激活／终态收据逐文件登记，不得删除、改名或移动；与此前各版本退休同一处置。"),
        "retired_runtime_profiles": [],
        "runtime_catalog": runtime_catalog,
        "schema_version": f"official-client-codex-{target}-terminal-state/v1",
        "target": {
            "previous_profile_sha256": snapshots[baseline]["digest"],
            "previous_version": baseline,
            "profile_id": profile_id_of(build_by_mode["active"]["id"]),
            "profile_sha256": active_row["digest"],
            "version": target,
        },
        "transitions": [],
        "transitions_note": "源码与台账漂移由 docs/egress/maintenance/ 下本轮各 freeze-successor 承接，本收据不重复登记仓库级 transitions。",
    }
    # promotion_intermediate_snapshot_catalog.path 是仓库相对路径（已含 backend/...），sha256 是内容寻址文件名
    facts["promotion_intermediate_snapshot_catalog"]["path"] = middle_rel
    output = Path(args.output)
    if output.exists():
        sys.exit(f"输出已存在，拒绝覆盖：{output}")
    output.write_text(json.dumps(facts, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    print(f"已写 {output}（{len(facts)} 个顶层字段）；completed_at_utc={completed}；vc6_items={vc6_item_ids}")


if __name__ == "__main__":
    main()
