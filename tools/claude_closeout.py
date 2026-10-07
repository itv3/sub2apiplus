#!/usr/bin/env python3
"""Claude 升级收尾的客户端部分（Claude 升级控制面设计 B5b-06「收尾编排 Claude 版」）。

收尾编排只有一份：``tools/codex_closeout.py``（候选隔离、指南人工批准、完整门禁、可恢复发布、清理与推送的日志与幂等）。
配置的合同名是 ``claude-upgrade-closeout/v1`` 时，它把随客户端变的三件事转到这里，其余流程逐字共用：

一、指南材料（``read_guide_material``）
====================================
与 Codex 同形状的材料，供指南草稿与 prepare／publish 前的封存链重放核对：

* VC-2：``claude_upgrade._load_stage_result(classify)``（六个批准文件绑定与 checkpoint 均已核对）与
  ``_effective_classification_view``（按当前评估基线生效的批准断言画像，B4-05a）；引用三项——批准的 RuleMigrationLedger
  （``rule_migration``）、批准的目标规则清单（``target_rules``）与生效断言画像（``assertion_profile_manifest``），逐个按摘要
  读取；迁移行（``spec_id``／``migration_decision``／``rationale``／``evidence_item_ids``）归一成 Codex 草稿生成器认识的
  ``entries``（``target_rule``／``baseline_rule``／``classification``／``rationale``／``evidence_refs``）；
* VC-1：官方阶段结果为采集路径时，读 seal 绑定的深度扫描清单与 seal 草案里的 capture manifest，经
  ``claude_rule_assertion.bind_capture_manifest`` 合成评估用 manifest，再按数据根取官方观测；只读复用路径沿
  ``predecessor_reuse`` 找到同数据根下的前序 Campaign（其官方阶段的 EvidenceFact 必须就是复用来源），前序本身必须是采集路径；
* 逐规则逐 check 的官方观测匹配数（``_select_observations``，与 Codex 同义：匹配数不是去重请求数）。

读侧一律用 Campaign 所在受管根（``<数据根>/tools/official_client_capture`` 与 ``tools/official_client_control``）的模块，
在独立解释器里执行（``codex_closeout.py read-material --client claude``），两棵受管树的文件摘要在重放前后必须不变。

二、第二部分摘要连锁（``source_updates``）
========================================
指南第二部分字节变化时，只做三处唯一替换（任何一处不能唯一定位即失败关闭）：

1. ``tools/official_client_capture/claude_upgrade_scenarios_*.json`` 中 ``source_spec`` 绑定 Claude 指南「第二部分」的场景清单：
   ``source_spec.sha256`` 旧章节摘要 → 新章节摘要（章节边界与 ``claude_upgrade_plan.guide_section_sha256`` 相同）；摘要已是
   历史章节（Git 历史能重放）且文件与 HEAD 一致的老清单保持原样；
2. ``claude_rule_expectations_*.json`` 的 ``generated_from.scenarios.sha256``：它是场景清单载荷的规范摘要
   （``claude_rule_assertion._canonical_sha256``，不是文件字节摘要），按第 1 步前后两份载荷复算替换；
3. ``claude_rule_assertion.py`` 的 ``FROZEN_PROFILE_SHA256S[<版本>]``：期望清单文件旧摘要 → 新摘要。

三、终态收据（``terminal_candidate``／``verify_candidate``）
==========================================================
从结构化终态事实生成候选字节，按候选仓库里的 ``tools/claude_terminal_state.py`` 校验（目标版本必须就是当前
production_active）；写进候选之后再跑一次当前生产终态门禁（恰好一份、通过校验、就是本次写的那份）。

本文件只被 ``codex_closeout`` 转调（``base`` 参数即调用方模块：共用它的路径、摘要、Git 与错误类型），不单独作入口。
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any

CLIENT = "claude"
MAINTENANCE = "docs/egress/maintenance"
TERMINAL_SUFFIX = "_TERMINAL_STATE_RECEIPT.json"
TERMINAL_STATE_TOOL = "tools/claude_terminal_state.py"
CAPTURE_ROOT = "tools/official_client_capture"
READER_ROOTS = ("tools/official_client_capture", "tools/official_client_control")
SCENARIO_GLOB = "claude_upgrade_scenarios_*.json"
EXPECTATIONS_GLOB = "claude_rule_expectations_*.json"
CHECKER = CAPTURE_ROOT + "/claude_rule_assertion.py"
FROZEN_EXPECTATIONS = "FROZEN_PROFILE_SHA256S"
FRAGMENT = "第二部分"
OFFICIAL_ROLE_STAGE = "capture-official"


def _row() -> Any:
    from tools.ci import client_table

    return client_table.spec(CLIENT)


def guide_relative() -> str:
    return "docs/" + str(_row()["guide_file"])


def part2_heading() -> str:
    return str(_row()["guide_part2_heading"])


def canonical_sha256(value: Any) -> str:
    """与 ``claude_rule_assertion._canonical_sha256`` 同一算法（期望清单登记场景清单载荷用它）。"""

    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 二、第二部分摘要连锁
# ---------------------------------------------------------------------------


def _historical_part2_hashes(repo: Path, base: Any, guide: str, heading: str) -> set[str]:
    hashes: set[str] = set()
    for commit in base.git(repo, "log", "--format=%H", "--", guide).stdout.splitlines():
        historical = base.git(repo, "show", commit + ":" + guide, check=False)
        if historical.returncode == 0 and heading in historical.stdout:
            hashes.add(base.part2_sha(historical.stdout, heading=heading))
    return hashes


def source_updates(repo: Any, guide_text: str, *, base: Any) -> dict[str, dict[str, str]]:
    """Claude 指南与三处摘要连锁的更新（见模块说明第二节）；返回值形状与 Codex ``source_updates`` 相同。"""

    repo = base.safety.plain(repo)
    guide_name, heading = guide_relative(), part2_heading()
    guide = base.inside(repo, guide_name)
    before = guide.read_text()
    old_sha, new_sha = base.part2_sha(before, heading=heading), base.part2_sha(guide_text, heading=heading)
    updates: dict[str, dict[str, str]] = {}

    def add(relative: str, content: bytes) -> None:
        path = base.inside(repo, relative)
        original = path.read_bytes()
        if original != content:
            updates[relative] = {"before_sha256": base.sha(original), "after_sha256": base.sha(content), "text": content.decode()}

    add(guide_name, guide_text.encode())
    if old_sha == new_sha:
        return updates
    directory = repo / CAPTURE_ROOT
    history: set[str] | None = None
    # 第 1 处：场景清单的 source_spec。
    payload_changes: dict[str, str] = {}
    for path in sorted(directory.glob(SCENARIO_GLOB)):
        text = path.read_text()
        value = json.loads(text)
        source = value.get("source_spec") if isinstance(value, dict) else None
        if not (isinstance(source, dict) and source.get("path") == guide_name and source.get("fragment") == FRAGMENT):
            continue
        relative = path.relative_to(repo).as_posix()
        if source.get("sha256") != old_sha:
            # 老版本场景清单保留历史指南绑定；只有 Git 历史能重放该章节摘要、且文件与 HEAD 一致时才允许保持原样。
            if history is None:
                history = _historical_part2_hashes(repo, base, guide_name, heading)
            committed = base.git(repo, "show", "HEAD:" + relative, check=False)
            if source.get("sha256") not in history or committed.returncode or committed.stdout != text:
                raise base.CloseoutError(f"已有来源摘要漂移且无历史绑定，不能批量覆盖：{path.name}")
            continue
        pattern = r'("source_spec"\s*:\s*\{[^{}]*?"sha256"\s*:\s*")' + re.escape(old_sha) + r'(")'
        content, count = re.subn(pattern, lambda match: match[1] + new_sha + match[2], text)
        if count != 1:
            raise base.CloseoutError(f"来源摘要字段不能唯一定位：{path.name}")
        changed = json.loads(content)
        expected = json.loads(text)
        expected["source_spec"]["sha256"] = new_sha
        if changed != expected:
            raise base.CloseoutError(f"场景清单替换越出 source_spec.sha256：{path.name}")
        add(relative, content.encode())
        payload_changes[canonical_sha256(value)] = canonical_sha256(changed)
    if not payload_changes:
        raise base.CloseoutError("没有发现与 Claude 指南第二部分绑定的场景清单")
    # 第 2 处：期望清单登记的场景清单载荷规范摘要。
    expectations: dict[str, tuple[str, str, str]] = {}
    for path in sorted(directory.glob(EXPECTATIONS_GLOB)):
        text = path.read_text()
        value = json.loads(text)
        generated = value.get("generated_from") if isinstance(value, dict) else None
        scenarios = generated.get("scenarios") if isinstance(generated, dict) else None
        old = scenarios.get("sha256") if isinstance(scenarios, dict) else None
        if old not in payload_changes:
            continue
        new = payload_changes[old]
        if text.count(old) != 1:
            raise base.CloseoutError(f"期望清单里的场景清单摘要不能唯一定位：{path.name}")
        content = text.replace(old, new)
        expected = json.loads(text)
        expected["generated_from"]["scenarios"]["sha256"] = new
        if json.loads(content) != expected:
            raise base.CloseoutError(f"期望清单替换越出 generated_from.scenarios.sha256：{path.name}")
        relative = path.relative_to(repo).as_posix()
        add(relative, content.encode())
        version = str(value.get("claude_version", ""))
        if version in expectations:
            raise base.CloseoutError(f"同一版本有两份期望清单受连锁影响：{version}")
        expectations[version] = (base.sha(text.encode()), base.sha(content.encode()), relative)
    # 第 3 处：检查器里按版本登记的期望清单冻结摘要。
    if expectations:
        checker_path = base.inside(repo, CHECKER)
        text = checker_path.read_text()
        frozen: dict[str, str] | None = None
        for node in ast.parse(text).body:
            target = node.target if isinstance(node, ast.AnnAssign) else (node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None)
            if isinstance(target, ast.Name) and target.id == FROZEN_EXPECTATIONS and node.value is not None:
                frozen = ast.literal_eval(node.value)
        if not isinstance(frozen, dict):
            raise base.CloseoutError("检查器缺少期望清单冻结摘要表")
        changed = text
        for version, (old, new, _relative) in sorted(expectations.items()):
            if version not in frozen:
                continue
            if frozen[version] != old:
                raise base.CloseoutError(f"检查器登记的 {version} 期望清单原摘要不符")
            pattern = r'("' + re.escape(version) + r'"\s*:\s*")' + old + r'(")'
            changed, count = re.subn(pattern, lambda match: match[1] + new + match[2], changed)
            if count != 1:
                raise base.CloseoutError(f"检查器期望清单摘要不能唯一定位：{version}")
        add(CHECKER, changed.encode())
    return updates


# ---------------------------------------------------------------------------
# 一、指南材料
# ---------------------------------------------------------------------------


def _migration_entries(document: dict[str, Any], base: Any) -> dict[str, Any]:
    """批准的 RuleMigrationLedger → Codex 草稿生成器认识的 ``entries``（字段见模块说明第一节）。"""

    try:
        baseline, target = set(document["baseline_spec_ids"]), set(document["target_spec_ids"])
        entries = [
            {
                "target_rule": row["spec_id"] if row["spec_id"] in target else None,
                "baseline_rule": row["spec_id"] if row["spec_id"] in baseline else None,
                "classification": row["migration_decision"],
                "rationale": row["rationale"],
                "evidence_refs": list(row["evidence_item_ids"]),
            }
            for row in document["rules"]
        ]
    except (KeyError, TypeError) as error:
        raise base.CloseoutError(f"批准的规则迁移清单字段不完整：{error}") from error
    return {"schema_version": document.get("schema_version"), "entries": entries}


def _bound_document(campaign: Path, binding: dict[str, Any], label: str, base: Any) -> tuple[Path, Any]:
    path = base.inside(campaign, binding["path"])
    if base.sha(path.read_bytes()) != binding["sha256"]:
        raise base.CloseoutError(f"{label}摘要漂移")
    return path, base.safety.read(path)


def _official_observations(campaign: Path, manifest: dict[str, Any], stage: dict[str, Any], upgrade: Any, checker: Any, base: Any, *, reused: bool = False) -> tuple[list[Any], dict[str, str], str]:
    """VC-1 官方观测（见模块说明第一节）：返回 (观测, capture manifest 绑定, 证据所在 Campaign)。"""

    if stage.get("mode") == "predecessor_reuse":
        if reused:
            raise base.CloseoutError("前序只读复用链只能有一级：前序 Campaign 的官方证据必须是采集路径")
        reuse = stage["predecessor_reuse"]
        predecessor_id = str(reuse["predecessor"]["campaign_id"])
        predecessor = campaign.parent / predecessor_id
        previous = upgrade.load_campaign_manifest(predecessor)
        if previous.get("campaign_id") != predecessor_id or previous.get("target_version") != manifest.get("target_version"):
            raise base.CloseoutError("只读复用的前序 Campaign 身份或目标版本不符")
        previous_stage = upgrade._load_stage_result(predecessor, OFFICIAL_ROLE_STAGE, _verified_campaign_manifest=previous)
        if (previous_stage.get("store") or {}).get("evidence_fact_ref") != reuse["source_evidence_fact_ref"]:
            raise base.CloseoutError("前序官方阶段结果的 EvidenceFact 不是只读复用来源")
        return _official_observations(predecessor, previous, previous_stage, upgrade, checker, base, reused=True)
    if stage.get("mode") != "captured" or not isinstance(stage.get("seal"), dict):
        raise base.CloseoutError("VC-1 官方阶段结果不是可重放的采集路径")
    seal = stage["seal"]
    _path, inventory = _bound_document(campaign, seal["evidence_manifest"], "VC-1 深度扫描清单", base)
    _path, draft = _bound_document(campaign, seal["seal_draft"], "VC-1 seal 草案", base)
    capture_path, capture = _bound_document(campaign, draft["drafts"]["capture_manifest"], "VC-1 capture manifest", base)
    index = {entry["path"]: entry for entry in inventory["entries"]}
    evaluation = checker.bind_capture_manifest(capture, index, capture_id=str(draft["attempt_id"]))
    observations = checker.observations_from_manifest(evaluation, campaign.parents[2])
    return observations, base.safety.file_binding(capture_path), str(manifest["campaign_id"])


def read_guide_material(campaign: Any, candidate: str, *, base: Any) -> dict[str, Any]:
    """使用 Campaign 受管根的封存读侧取得 VC-2 批准包、生效断言画像与 VC-1 官方观测；不发请求、不写文件。"""

    campaign = base.safety.plain(campaign)
    if campaign.parent.name != "campaigns" or campaign.parent.parent.name != "evidence":
        raise base.CloseoutError("Campaign 路径不是受管布局")
    reader_root = campaign.parents[2]
    sys.path.insert(0, str(reader_root))
    from tools.official_client_capture import claude_rule_assertion as checker
    from tools.official_client_capture import claude_upgrade as upgrade

    from tools.official_client_control import contracts

    if any(Path(module.__file__).resolve().parents[2] != reader_root for module in (upgrade, checker, contracts)):
        raise base.CloseoutError("材料重放须使用独立解释器，不能混入其他源码根的模块")
    reader_files = {
        str(path): base.safety.file_binding(path)["sha256"]
        for root in READER_ROOTS
        for path in sorted((reader_root / root).rglob("*"))
        if path.is_file() and path.suffix in {".py", ".json", ".sh"} and "__pycache__" not in path.parts
    }
    manifest = upgrade.load_campaign_manifest(campaign)
    classified = upgrade._load_stage_result(campaign, "classify", _verified_campaign_manifest=manifest)
    if classified.get("status") != "complete":
        raise base.CloseoutError("VC-2 尚未完整批准")
    effective = upgrade._effective_classification_view(campaign, candidate, classified)
    approved = classified["approved"]
    references = {
        "rule_migration": {key: approved["rule_migration"][key] for key in ("path", "sha256")},
        "target_rules": {key: approved["target_rules"][key] for key in ("path", "sha256")},
        "assertion_profile_manifest": {key: effective["assertion_profile_manifest"][key] for key in ("path", "sha256")},
    }
    documents = {key: _bound_document(campaign, binding, f"批准清单 {key} ", base)[1] for key, binding in references.items()}
    official = upgrade._load_stage_result(campaign, OFFICIAL_ROLE_STAGE, _verified_campaign_manifest=manifest)
    if official.get("status") != "complete":
        raise base.CloseoutError("VC-1 官方证据尚未完整封存")
    observations, capture_binding, evidence_campaign = _official_observations(campaign, manifest, official, upgrade, checker, base)
    profile = documents["assertion_profile_manifest"]
    counts = {
        rule["rule_id"]: {
            check["id"]: len(checker._select_observations(observations, check["select"], rule["scenario_ids"])) for check in rule["checks"]
        }
        for rule in profile["rules"]
    }
    if any(base.safety.file_binding(path)["sha256"] != expected for path, expected in reader_files.items()):
        raise base.CloseoutError("受管读侧在重放期间变化")
    return {
        "client": CLIENT,
        "reader_root": str(reader_root),
        "reader_files": reader_files,
        "campaign_path": str(campaign),
        "campaign_id": manifest["campaign_id"],
        "candidate_id": candidate,
        "target_version": manifest["target_version"],
        "classification_sha256": base.digest(classified),
        "effective_classification_sha256": base.digest(effective),
        "official_stage_sha256": base.digest(official),
        "official_evidence_campaign_id": evidence_campaign,
        "capture_manifest": capture_binding,
        "references": references,
        "migration": _migration_entries(documents["rule_migration"], base),
        "profile": profile,
        "official_observation_counts": counts,
    }


def reader_binding(reader_root: Path, base: Any) -> None:
    """``collect_guide_material`` 在起独立解释器之前核对受管读侧入口存在（与 Codex 绑定 ``codex_upgrade.py`` 同义）。"""

    base.safety.file_binding(reader_root / CAPTURE_ROOT / "claude_upgrade.py")


# ---------------------------------------------------------------------------
# 三、终态收据
# ---------------------------------------------------------------------------


def _terminal_module(repo: Path, base: Any) -> Any:
    return base.load_module(Path(repo) / TERMINAL_STATE_TOOL, "closeout_claude_terminal_validator")


def terminal_candidate(repo: Any, facts: dict[str, Any], receipt_relative: str, *, base: Any) -> str:
    """从结构化终态事实生成候选字节，并通过候选仓库自带的 Claude 终态校验器；不写入正式坐标。"""

    path = base.inside(repo, receipt_relative)
    prefix = str(_row()["terminal_receipt_prefix"])
    if path.parent != Path(repo) / MAINTENANCE or not path.name.startswith(prefix) or not path.name.endswith(TERMINAL_SUFFIX):
        raise base.CloseoutError("终态收据坐标不符合正式合同")
    terminal = _terminal_module(Path(repo), base)
    try:
        state = terminal.release_catalog_state(Path(repo))
        if facts.get("target", {}).get("version") != state["version"]:
            raise base.CloseoutError("新签终态必须对应当前 production_active 版本")
        value = {key: item for key, item in facts.items() if key != "identity_sha256"}
        value["identity_sha256"] = terminal.claude_terminal_state_identity(value)
        terminal.validate_claude_terminal_state_document(value, receipt_relative, root=Path(repo), catalog_state=state)
    except terminal.TerminalStateError as error:
        raise base.CloseoutError(f"Claude 终态收据不满足合同：{error}") from error
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def verify_candidate(candidate: Any, receipt_relative: str, *, base: Any) -> dict[str, Any]:
    """候选仓库写入终态收据之后的当前生产终态门禁：必须是通过校验的那一份，而不是基线豁免。"""

    terminal = _terminal_module(Path(candidate), base)
    try:
        report = terminal.validate_claude_terminal_state_receipts(Path(candidate))
    except terminal.TerminalStateError as error:
        raise base.CloseoutError(f"候选仓库的 Claude 终态门禁失败：{error}") from error
    if report.get("status") != "validated" or report.get("receipt") != receipt_relative:
        raise base.CloseoutError("候选仓库的当前生产终态不是本次签发的终态收据")
    return report
