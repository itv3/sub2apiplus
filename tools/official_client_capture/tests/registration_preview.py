"""登记阶段离线预演：用上一版真实候选证据，对新登记的标签与判据一次跑完 seal 预览、assert-rules 的全部检查（测试夹具，不进受管身份）。

为什么要有它
------------
每个版本登记都要新写一份证据标签声明和一份候选期望清单。登记测试里的观测是手造的，标签与判据的错位只有到 VC-5 封存预览、
assert-rules 才会暴露，而且是“撞一个、修一个”：上一版升级里，封存预览先被 SPEC-WS-005 的 7 条 first-business-frame 检查拒收
（标签写成原始帧序号 1，候选网关把压缩消息拆成多帧，实际在序号 7），改完部署、等后台验证、重起之后，assert-rules 又败在
SPEC-BODY-003（判据要求 0，实际 1）。两处合起来在关键路径上耗了约 3 小时，其中后台验证 1.9 小时。

本工具把这两类缺陷前移到登记阶段、**一次报全**：
1. 拿上一版候选的真实断言证据（原始中继字节与派生观测），按新声明**重新编目**（和 ``prepare_assertion_bundle.sh`` 同一条路径：
   编目 → 收口 → 派生 → 回填 manifest），得到新标签下的 capture manifest；
2. 载入观测（``load_observations``，选择键没命中帧、标签冲突、证据与 manifest 不一致都在这里报）；
3. 逐 check 做 seal 预检的选择器可达性（与 ``assertion_gate._verify_selector_reachability`` 同判据，但**不在第一个未命中处停下**，
   把全部未命中一次列全，并给出诊断：哪个条件把观测筛光了、哪些帧带着标签却不满足其余条件、按消息序号解读应落在哪一帧）；
4. 场景×kind 覆盖、wire 观测互斥、候选 trace 收据——封存预览里与标签、判据无关但同属 seal 门禁的几项；
5. assert-rules：按验收契约对本侧每条规则的每个 check 求值（``evaluate_rule``），列出全部不符的 check 和实际值；
   与第 3 项同源（选择器没命中导致的空集失败）的单独标注，不重复计数。

范围与缺口（报告里逐项写明，不假装覆盖）
------------------------------------------
* 证据来自**上一版**。新声明里选中、而上一版证据里没有的原始文件，只有给出完整原始证据根（``--candidate-roots-dir``）才能回放，
  否则编目阶段会如实报“声明选中了证据里没有的文件”。
* 新版判据里写死的版本字面量（如 UA、``version`` 头）与上一版证据对不上：用 ``--rewrite-version 新=旧`` 把清单里的字符串换成证据版本再跑。
* 候选 trace（go test 日志 → 内部状态事实）默认沿用上一版证据里已生成的 ``candidate-trace/``，不重放（重放需要候选源码快照与新版事实映射）。
* compare 阶段读 Campaign 里的封存阶段结果与表面指纹（``compare_campaign``），与标签、判据无关，本工具不重放；
  provenance 重放用重新编目得到的证据根自身，只证明新包内部自洽，不证明与 ARM64 原始证据根一致。
* 官方侧封存预览用的是仓库冻结画像（``acceptance_profile_path``）与目标版本弃用标签延后规则，这里没有重放；
  官方侧只跑与候选侧同口径的“双线判据规则”可达性与求值（对应 assert-rules 的官方侧）。

用法（仓库根执行，所有命令零写入原证据）::

    PYTHONDONTWRITEBYTECODE=1 python3 -B -m tools.official_client_capture.tests.registration_preview preview \\
        --candidate-evidence <上一版候选 attempt 目录或 assertion-bundle 目录> \\
        --labels <新版证据标签声明.json> --profile <新版候选期望清单.json> \\
        [--official-evidence <上一版官方 attempt 目录或 assertion-bundle 目录>] \\
        [--candidate-roots-dir <原始证据根集合目录>] [--rewrite-version 新版本=证据版本] \\
        [--work <工作目录>] [--keep] [--json-out <结果.json>]

退出码：0 全部通过；1 发现缺陷（选择器不可达、判据不符、标签冲突……）；2 输入有误或引擎自身出错；3 没有缺陷但有未执行项。
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from tools.official_client_capture import acceptance_contract as contract_module
from tools.official_client_capture import assertion_gate
from tools.official_client_capture import build_assertion_bundle as bundle_module
from tools.official_client_capture import build_evidence_catalog as catalog_module
from tools.official_client_capture import candidate_rule_assertion as cra
from tools.official_client_capture import derive_official_observations as derive_module

SCHEMA = "registration-preview/v1"
PASSED = "passed"
FAILED = "failed"
NOT_RUN = "not_run"
SKIPPED = "skipped"  # 调用方明确要求跳过（--candidate-only），不影响结论
ERROR = "error"
EXIT_CODES = {"clean": 0, "defects_found": 1, "harness_error": 2, "incomplete": 3}
TRACE_PREFIX = assertion_gate.CANDIDATE_TRACE_PREFIX
ACTUAL_TEXT_LIMIT = 260


class PreviewInputError(RuntimeError):
    """命令行输入有误（路径不存在、证据结构不认识……），不是证据本身的缺陷。"""


# ---------------------------------------------------------------------------
# 结果模型
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Step:
    name: str
    status: str
    summary: str
    details: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass
class Defect:
    """一条缺陷：kind 取 selector-unreachable／assertion-mismatch／config-error 等。"""

    kind: str
    side: str
    rule: str | None
    check: str | None
    message: str
    hint: str | None = None
    same_origin_as: str | None = None


@dataclasses.dataclass
class SideReport:
    side: str
    steps: list[Step] = dataclasses.field(default_factory=list)
    defects: list[Defect] = dataclasses.field(default_factory=list)

    def add(self, step: Step) -> Step:
        self.steps.append(step)
        return step


# ---------------------------------------------------------------------------
# 上一版证据的定位
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class EvidenceSet:
    bundle_dir: Path
    attempt_json: Path | None
    codex_version: str
    capture_id: str
    root_names: list[str]


def locate_evidence(path: Path) -> EvidenceSet:
    """接受 attempt 目录或 assertion-bundle 目录，找出 bundle、attempt.json 与证据版本。"""

    path = Path(path)
    if (path / "evidence" / "assertion-bundle").is_dir():
        bundle_dir = path / "evidence" / "assertion-bundle"
        attempt_json = path / "attempt.json"
    elif (path / "assertion-bundle" / "capture-manifest.json").is_file():
        # 离线副本布局：<目录>/assertion-bundle 与 <目录>/attempt.json（只拉回了这两样）
        bundle_dir = path / "assertion-bundle"
        attempt_json = path / "attempt.json"
    elif (path / "capture-manifest.json").is_file():
        bundle_dir = path
        candidate = path.parent.parent / "attempt.json"
        attempt_json = candidate if candidate.is_file() else None
    else:
        raise PreviewInputError(f"不是 attempt 目录也不是 assertion-bundle 目录：{path}")
    if attempt_json is not None and not attempt_json.is_file():
        attempt_json = None
    try:
        manifest = json.loads((bundle_dir / "capture-manifest.json").read_text(encoding="utf-8"))
        provenance = json.loads((bundle_dir / "provenance.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise PreviewInputError(f"无法读取证据包的 capture-manifest.json／provenance.json：{error}") from error
    names = sorted({entry["source_root"] for entry in provenance["entries"]})
    return EvidenceSet(
        bundle_dir=bundle_dir,
        attempt_json=attempt_json,
        codex_version=str(manifest.get("codex_version")),
        capture_id=str(manifest.get("capture_id")),
        root_names=names,
    )


def job_roots_for_side(
    evidence: EvidenceSet,
    declaration: Mapping[str, Any],
    side: str,
    roots_dir: Path | None,
) -> tuple[dict[str, list[tuple[str, Path]]], dict[str, Path], list[str]]:
    """job → [(inventory 前缀, 证据根目录)]；返回 (job_roots, 源根名→目录, 说明)。

    根目录优先取 ``roots_dir/<根名>``（完整原始证据根），没有就用上一版 bundle 里的同名子目录（只含上一版声明选中的文件）。
    job 与根名的对应来自 attempt.json 的 results[].evidence_roots（根名取路径最后一段，与 prepare_assertion_bundle.sh 同口径）。
    """

    if evidence.attempt_json is None:
        raise PreviewInputError(
            "找不到 attempt.json：按新声明编目需要 job→证据根的对应关系。请传 attempt 目录，或把 attempt.json 放在 bundle 上两级目录。"
        )
    attempt = json.loads(evidence.attempt_json.read_text(encoding="utf-8"))
    declared_jobs = {
        entry["job_id"] for entry in declaration["entries"] if entry["side"] == side
    }
    notes: list[str] = []
    job_roots: dict[str, list[tuple[str, Path]]] = {}
    source_roots: dict[str, Path] = {}
    for result in attempt.get("results", []):
        job_id = result.get("id")
        if job_id not in declared_jobs or result.get("status") != "complete":
            continue
        for raw in result.get("evidence_roots") or []:
            name = Path(raw).name
            if name not in evidence.root_names:
                notes.append(f"job {job_id} 的证据根 {name} 不在上一版证据包里，已跳过")
                continue
            directory = roots_dir / name if roots_dir is not None and (roots_dir / name).is_dir() else evidence.bundle_dir / name
            if not directory.is_dir():
                raise PreviewInputError(f"证据根目录不存在：{directory}")
            job_roots.setdefault(job_id, []).append((name, directory))
            source_roots[name] = directory
    missing = sorted(declared_jobs - set(job_roots))
    if missing:
        notes.append(f"新声明里有 job 在上一版证据里没有已完成的结果，未编目：{missing}")
    if not job_roots:
        raise PreviewInputError(f"{side} 侧没有任何可编目的 job（声明 job：{sorted(declared_jobs)}）")
    return job_roots, source_roots, notes


# ---------------------------------------------------------------------------
# 重新编目（与 prepare_assertion_bundle.sh 同路径）
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Rebuilt:
    bundle_dir: Path
    manifest_path: Path
    manifest: dict[str, Any]
    source_roots: dict[str, Path]
    catalog: dict[str, Any]
    notes: list[str]


def rebuild_bundle(
    evidence: EvidenceSet,
    declaration: Mapping[str, Any],
    side: str,
    work_dir: Path,
    roots_dir: Path | None,
) -> Rebuilt:
    job_roots, source_roots, notes = job_roots_for_side(evidence, declaration, side, roots_dir)
    built = catalog_module.build_catalog(declaration, job_roots, side=side)
    bundle_dir = work_dir / assertion_gate.BUNDLE_DIR_NAME
    bundle_module.build_bundle(source_roots, built["bundle_plan"]["entries"], bundle_dir)
    if built["derivation_plan"]["entries"]:
        derive_module.derive_observations(bundle_dir, built["derivation_plan"]["entries"])
    manifest = catalog_module.finalize_manifest(
        built["manifest_draft"],
        bundle_dir,
        codex_version=evidence.codex_version,
        capture_id=evidence.capture_id,
    )
    old_trace = evidence.bundle_dir / TRACE_PREFIX.rstrip("/")
    if side == "candidate" and old_trace.is_dir():
        # 候选 trace 沿用上一版：原样拷目录、把上一版 manifest 里 candidate-trace/ 下的 artifact 并进来（摘要按拷贝后的文件现算）。
        shutil.copytree(old_trace, bundle_dir / TRACE_PREFIX.rstrip("/"))
        old_manifest = json.loads((evidence.bundle_dir / "capture-manifest.json").read_text(encoding="utf-8"))
        carried = []
        for artifact in old_manifest["artifacts"]:
            if artifact["path"].startswith(TRACE_PREFIX):
                artifact = dict(artifact)
                artifact["sha256"] = cra.file_sha256(bundle_dir / artifact["path"])
                carried.append(artifact)
        manifest["artifacts"] = sorted([*manifest["artifacts"], *carried], key=lambda item: item["path"])
        notes.append(f"候选 trace 沿用上一版证据（{len(carried)} 个 artifact），未重放")
    manifest_path = bundle_dir / assertion_gate.MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return Rebuilt(bundle_dir, manifest_path, manifest, source_roots, built, notes)


def manifest_drift(old: Mapping[str, Any], new: Mapping[str, Any]) -> dict[str, Any]:
    """新旧 manifest 的逐 artifact 差异（路径集合、标签、帧标签）；只比对两边都有的字段。"""

    old_by_path = {item["path"]: item for item in old["artifacts"]}
    new_by_path = {item["path"]: item for item in new["artifacts"]}
    changed: list[dict[str, Any]] = []
    for path in sorted(old_by_path.keys() & new_by_path.keys()):
        before, after = old_by_path[path], new_by_path[path]
        fields = [
            key for key in ("kind", "parser", "scenario_ids", "labels", "frame_labels", cra.ENVIRONMENT_PROBE_SNI_FIELD)
            if before.get(key) != after.get(key)
        ]
        if fields:
            changed.append({
                "path": path,
                "fields": fields,
                **{f"old_{key}": before.get(key) for key in fields},
                **{f"new_{key}": after.get(key) for key in fields},
            })
    return {
        "artifact_count_old": len(old_by_path),
        "artifact_count_new": len(new_by_path),
        "only_in_old": sorted(old_by_path.keys() - new_by_path.keys()),
        "only_in_new": sorted(new_by_path.keys() - old_by_path.keys()),
        "changed": changed,
    }


# ---------------------------------------------------------------------------
# 观测与帧标签的描述、诊断
# ---------------------------------------------------------------------------


def brief(observation: cra.Observation) -> dict[str, Any]:
    data = observation.data
    result: dict[str, Any] = {"record_id": observation.record_id.rsplit("#", 1)[-1], "scenario": observation.scenario_id}
    if observation.record_type == "websocket_frame":
        result["artifact"] = short_artifact(observation.artifact_path)
        for key in ("frame_index", "opcode", "event_type", "message_fragment_count", "message_start_frame_index"):
            if key in data:
                result[key] = data[key]
    else:
        for key in ("method", "path"):
            if key in data:
                result[key] = data[key]
    return result


def short_artifact(path: str) -> str:
    """artifact 路径里带长长的 Campaign／根名前缀，报告里只留场景与连接那一段。"""

    return path.split("_scenarios_")[-1] if "_scenarios_" in path else path[-70:]


def frame_label_report(manifest: Mapping[str, Any], observations: Sequence[cra.Observation]) -> dict[str, Any]:
    """每个带帧标签的 artifact：各选择键落到哪一帧，以及落点是不是消息的起始帧。"""

    entries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for artifact in manifest["artifacts"]:
        frame_labels = artifact.get("frame_labels")
        if not frame_labels:
            continue
        frames = [
            item.data for item in observations
            if item.record_type == "websocket_frame" and item.artifact_path == artifact["path"]
        ]
        by_index = {fact["frame_index"]: fact for fact in frames}
        resolved: list[dict[str, Any]] = []
        for key, labels in frame_labels.items():
            single = cra.resolve_frame_label_selectors({key: labels}, frames, artifact["path"])
            for target, merged in single.items():
                fact = by_index[target]
                landed_on_start = fact.get("opcode") in cra.WS_DATA_MESSAGE_OPCODES
                item = {
                    "key": key,
                    "frame_index": target,
                    "opcode": fact.get("opcode"),
                    "event_type": fact.get("event_type"),
                    "labels": merged,
                    "message_start": landed_on_start,
                }
                resolved.append(item)
                if not landed_on_start:
                    warnings.append(
                        f"{short_artifact(artifact['path'])}：键 {key!r} 落在续帧（原始帧 {target}，操作码 {fact.get('opcode')}，"
                        f"属于起始于帧 {fact.get('message_start_frame_index')} 的消息），续帧没有 event_type 与消息体事实，"
                        f"按此打的标签 {merged} 不会被任何读取消息体的判据选中"
                    )
        entries.append({"artifact": artifact["path"], "resolved": resolved})
    return {"artifacts": entries, "warnings": warnings}


def _condition_text(condition: Mapping[str, Any]) -> str:
    return f"{condition.get('path')} {condition.get('operator')} {json.dumps(condition.get('value'), ensure_ascii=False)}"


def diagnose_select(
    select: Mapping[str, Any],
    default_scenarios: Sequence[str],
    observations: Sequence[cra.Observation],
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """选择器零命中时的诊断：逐条件命中数、去掉某个条件后是否可达、带标签的观测落在哪、按消息序号解读该落在哪一帧。"""

    record_type = select["record_type"]
    scenarios = select.get("scenario_ids", list(default_scenarios))
    base = [item for item in observations if item.record_type == record_type and item.scenario_id in scenarios]
    conditions = list(select.get("where") or [])
    diagnosis: dict[str, Any] = {
        "base_candidates": len(base),
        "conditions": [],
        "culprits": [],
    }
    for index, condition in enumerate(conditions):
        alone = [item for item in base if cra._where_matches(item, [condition])]
        others = conditions[:index] + conditions[index + 1 :]
        without = [item for item in base if cra._where_matches(item, others)]
        entry: dict[str, Any] = {
            "condition": _condition_text(condition),
            "matches_alone": len(alone),
            "matches_all_other_conditions": len(without),
        }
        diagnosis["conditions"].append(entry)
        if without:
            # 去掉这个条件就能命中：它就是把观测筛光的条件。
            entry["without_it_would_match"] = [brief(item) for item in without[:6]]
            diagnosis["culprits"].append(entry["condition"])
        path = condition.get("path")
        if isinstance(path, str) and path.startswith("labels.") and condition.get("operator") == "equal":
            key, value = path[len("labels."):], condition.get("value")
            carriers = [item for item in base if item.labels.get(key) == value]
            entry["label_carriers"] = [brief(item) for item in carriers[:6]]
            entry["label_hints"] = _label_hints(carriers, without, observations, manifest, key, value)
    return diagnosis


def _label_hints(
    carriers: Sequence[cra.Observation],
    wanted: Sequence[cra.Observation],
    observations: Sequence[cra.Observation],
    manifest: Mapping[str, Any],
    label: str,
    value: str,
) -> list[str]:
    """带标签的帧不满足其余条件时，给出“按消息序号解读该落在哪一帧”的提示。

    声明用原始帧序号 k 写标签，通常是按“一消息一帧”（官方客户端的形态）数的第 k 条消息；候选网关把消息拆成多帧后，序号 k 的帧
    不再是第 k 条消息的起始帧。提示里给出第 k 条数据消息的真实起始帧，以及可直接替换的选择键。
    """

    hints: list[str] = []
    wanted_ids = {item.record_id for item in wanted}
    artifacts = {item["path"]: item for item in manifest["artifacts"]}
    for carrier in carriers:
        if carrier.record_id in wanted_ids or carrier.record_type != "websocket_frame":
            continue
        artifact = artifacts.get(carrier.artifact_path, {})
        keys = [
            key for key, labels in (artifact.get("frame_labels") or {}).items()
            if labels.get(label) == value
        ]
        starts = sorted(
            (
                item.data for item in observations
                if item.record_type == "websocket_frame"
                and item.artifact_path == carrier.artifact_path
                and item.scenario_id == carrier.scenario_id
                and item.data.get("opcode") in cra.WS_DATA_MESSAGE_OPCODES
            ),
            key=lambda fact: fact["frame_index"],
        )
        for key in keys:
            parsed = cra.parse_frame_label_selector(key)
            if parsed is None or parsed[0] != "frame":
                continue
            ordinal = parsed[1]
            data = carrier.data
            text = (
                f"标签 {label}={value} 写在原始帧序号 {key}，该帧是 {data.get('opcode')} 帧"
                + ("" if data.get("event_type") else "（没有 event_type）")
            )
            if data.get("opcode") not in cra.WS_DATA_MESSAGE_OPCODES:
                text += f"，属于起始于帧 {data.get('message_start_frame_index')} 的消息的续帧"
            if ordinal < len(starts):
                target = starts[ordinal]
                same_type = [fact for fact in starts if fact.get("event_type") == target.get("event_type")]
                text += (
                    f"；若意图是第 {ordinal} 条消息（官方一消息一帧时序号与帧序号相同），该消息的起始帧是原始帧序号 "
                    f"{target['frame_index']}（原写 {key}、实际 {target['frame_index']}），"
                    f"建议把键改成 message:{ordinal} 或 event:{target.get('event_type', '<类型>')}:{same_type.index(target)}"
                )
            hints.append(text)
    return hints


# ---------------------------------------------------------------------------
# 选择器可达性（不在第一个未命中处停下）
# ---------------------------------------------------------------------------


def collect_unreachable(
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    observations: Sequence[cra.Observation],
    manifest: Mapping[str, Any],
    side: str,
) -> tuple[int, int, list[dict[str, Any]]]:
    """与 assertion_gate._verify_selector_reachability 同判据，但列全所有未命中；返回（规则数、check 数、未命中项）。"""

    modes = contract["validation_modes"]
    unreachable: list[dict[str, Any]] = []
    rules = checks = 0
    for rule in profile["rules"]:
        rule_id = rule["rule_id"]
        if side == "official" and modes[rule_id] != contract_module.MODE_DUAL_WIRE:
            continue
        rules += 1
        for check in rule["checks"]:
            if not contract_module.check_applies_to_side(contract, rule_id, check["id"], side):
                continue
            assertion = check.get("assertion") or {}
            if assertion.get("operator") == "count_equal" and assertion.get("value") == 0:
                checks += 1
                continue
            checks += 1
            matched = cra._select_observations(observations, check["select"], rule["scenario_ids"])
            if matched:
                continue
            unreachable.append({
                "rule": rule_id,
                "check": check["id"],
                "diagnosis": diagnose_select(check["select"], rule["scenario_ids"], observations, manifest),
            })
    return rules, checks, unreachable


def crosscheck_with_real_gate(
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    observations: Sequence[cra.Observation],
    side: str,
    unreachable: Sequence[Mapping[str, Any]],
) -> tuple[bool, str]:
    """用真正的 seal 预检函数复核：它是否也拒绝，以及拒绝的是否正是我列出的第一项。"""

    try:
        assertion_gate._verify_selector_reachability(profile, contract, list(observations), side)
    except assertion_gate.AssertionGateError as error:
        if not unreachable:
            return False, f"真实预检拒绝了，而本工具没有列出任何未命中项：{error}"
        first = unreachable[0]
        needle = f"规则 {first['rule']} 的 check {first['check']}"
        if needle not in str(error):
            return False, f"真实预检拒绝的不是本工具列出的第一项（{needle}）：{error}"
        return True, str(error)
    if unreachable:
        return False, "真实预检放行了，而本工具列出了未命中项（引擎判据与真实预检不一致）"
    return True, "真实预检通过"


# ---------------------------------------------------------------------------
# assert-rules：逐规则逐 check 求值
# ---------------------------------------------------------------------------


def evaluate_rules(
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    observations: Sequence[cra.Observation],
    manifest: Mapping[str, Any],
    side: str,
    unreachable_ids: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], list[Defect]]:
    modes = contract["validation_modes"]
    outcomes: list[dict[str, Any]] = []
    defects: list[Defect] = []
    for rule in profile["rules"]:
        rule_id = rule["rule_id"]
        if side == "official" and modes[rule_id] != contract_module.MODE_DUAL_WIRE:
            continue
        try:
            results = cra.evaluate_rule(profile, rule_id, observations, manifest, side)
        except cra.AssertionConfigurationError as error:
            outcomes.append({"rule": rule_id, "status": "error", "message": str(error)})
            defects.append(Defect("config-error", side, rule_id, None, f"规则求值失败：{error}"))
            continue
        expected_ids = contract_module.expected_check_ids_for_side(contract, rule_id, side)
        got_ids = [item["id"] for item in results]
        failed = [item for item in results if not item["passed"]]
        outcome: dict[str, Any] = {
            "rule": rule_id,
            "mode": modes[rule_id],
            "checks": len(results),
            "failed": [item["id"] for item in failed],
            "status": "pass" if not failed else "fail",
        }
        if got_ids != expected_ids:
            outcome["check_closure"] = {"expected": expected_ids, "got": got_ids}
            defects.append(Defect(
                "check-closure", side, rule_id, None,
                f"本侧 check 集合与验收契约不一致：缺少 {sorted(set(expected_ids) - set(got_ids))}、多出 {sorted(set(got_ids) - set(expected_ids))}",
            ))
        outcomes.append(outcome)
        for item in failed:
            origin = None
            if (rule_id, item["id"]) in unreachable_ids:
                origin = f"{rule_id}:{item['id']} 的选择器未命中任何观测（见 seal 预检未命中项）"
            actual = json.dumps(item.get("actual"), ensure_ascii=False, sort_keys=True)
            if len(actual) > ACTUAL_TEXT_LIMIT:
                actual = actual[:ACTUAL_TEXT_LIMIT] + "…"
            expected = json.dumps(item.get("expected"), ensure_ascii=False, sort_keys=True)
            defects.append(Defect(
                "assertion-mismatch", side, rule_id, item["id"],
                f"判据不符：期望 {expected[:ACTUAL_TEXT_LIMIT]}，实际 {actual}",
                hint=_mismatch_hint(rule, item),
                same_origin_as=origin,
            ))
    return outcomes, defects


def _mismatch_hint(rule: Mapping[str, Any], result: Mapping[str, Any]) -> str | None:
    """判据写成固定值而实际值是别的固定值时，提示“判据可能过严/过松”。"""

    expected = result.get("expected") or {}
    actual = result.get("actual")
    if not isinstance(actual, Mapping):
        return None
    values = actual.get("values")
    if (
        expected.get("operator") == "all_equal"
        and isinstance(values, list)
        and values
        and len(set(json.dumps(item, sort_keys=True) for item in values)) == 1
        and json.dumps(values[0], sort_keys=True) != json.dumps(expected.get("value"), sort_keys=True)
    ):
        return (
            f"所有 {len(values)} 条命中观测的实际值一致（{json.dumps(values[0], ensure_ascii=False)}）而判据要求 "
            f"{json.dumps(expected.get('value'), ensure_ascii=False)}：判据与实际行为不一致，先核对是判据写严了还是实现有偏差"
        )
    return None


# ---------------------------------------------------------------------------
# 一侧的完整预演
# ---------------------------------------------------------------------------


def rewrite_versions(value: Any, rewrites: Mapping[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in rewrites.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [rewrite_versions(item, rewrites) for item in value]
    if isinstance(value, dict):
        return {key: rewrite_versions(item, rewrites) for key, item in value.items()}
    return value


def run_side(
    *,
    side: str,
    evidence: EvidenceSet,
    declaration: Mapping[str, Any],
    profile: Mapping[str, Any],
    contract: Mapping[str, Any],
    work_dir: Path,
    roots_dir: Path | None,
) -> SideReport:
    report = SideReport(side)
    old_manifest = json.loads((evidence.bundle_dir / "capture-manifest.json").read_text(encoding="utf-8"))

    # 1) 重新编目、收口、派生、回填 manifest
    try:
        rebuilt = rebuild_bundle(evidence, declaration, side, work_dir, roots_dir)
    except (catalog_module.EvidenceCatalogError, bundle_module.AssertionBundleError, derive_module.ObservationDerivationError) as error:
        report.add(Step("rebuild", FAILED, f"按新声明重新编目失败：{error}"))
        report.defects.append(Defect("catalog-error", side, None, None, str(error)))
        return report
    drift = manifest_drift(old_manifest, rebuilt.manifest)
    report.add(Step(
        "rebuild",
        PASSED,
        f"重新编目 {drift['artifact_count_new']} 个 artifact（上一版 {drift['artifact_count_old']}）；"
        f"标签/帧标签变化 {len(drift['changed'])} 项，新增 {len(drift['only_in_new'])}、消失 {len(drift['only_in_old'])}",
        {"drift": drift, "notes": rebuilt.notes},
    ))

    # 2) 载入观测
    try:
        manifest, observations = cra.load_observations(rebuilt.manifest_path, rebuilt.bundle_dir, evidence.codex_version)
    except cra.AssertionConfigurationError as error:
        report.add(Step("load-observations", FAILED, str(error)))
        report.defects.append(Defect("manifest-load", side, None, None, str(error)))
        return report
    report.add(Step("load-observations", PASSED, f"载入 {len(observations)} 条观测"))

    # 3) 帧标签落点
    frame_report = frame_label_report(manifest, observations)
    report.add(Step(
        "frame-labels",
        PASSED if not frame_report["warnings"] else FAILED,
        f"{sum(len(item['resolved']) for item in frame_report['artifacts'])} 个帧标签落点"
        + (f"，{len(frame_report['warnings'])} 处落在续帧上" if frame_report["warnings"] else ""),
        frame_report,
    ))
    for warning in frame_report["warnings"]:
        report.defects.append(Defect("frame-label-on-continuation", side, None, None, warning))

    # 4) seal 门禁里与标签、判据无关的几项
    side_coverage = contract["side_coverage"].get(side) or {}
    gate_notes: list[str] = []
    try:
        bundle_module.verify_manifest_kind_coverage(manifest, side_coverage)
        assertion_gate._verify_wire_observation_exclusivity(manifest, list(observations))
        trace_sha = assertion_gate._verify_candidate_trace(rebuilt.bundle_dir, manifest)
        gate_notes.append("场景×kind 覆盖、wire 观测互斥、候选 trace 收据" + ("均通过" if trace_sha else "（无 trace 目录）通过"))
        report.add(Step("seal-gate-static", PASSED, gate_notes[0]))
    except (bundle_module.AssertionBundleError, assertion_gate.AssertionGateError) as error:
        report.add(Step("seal-gate-static", FAILED, str(error)))
        report.defects.append(Defect("seal-gate", side, None, None, str(error)))

    # 5) 选择器可达性（列全）并用真实预检复核
    rules, checks, unreachable = collect_unreachable(profile, contract, observations, manifest, side)
    consistent, real_message = crosscheck_with_real_gate(profile, contract, observations, side, unreachable)
    report.add(Step(
        "selector-reachability",
        PASSED if not unreachable else FAILED,
        f"核对 {rules} 条规则 {checks} 个 check，选择器零命中 {len(unreachable)} 个"
        + ("" if consistent else "；【引擎自检不一致】" + real_message),
        {"unreachable": unreachable, "real_gate": real_message, "consistent_with_real_gate": consistent},
    ))
    for item in unreachable:
        hints = [
            hint
            for condition in item["diagnosis"]["conditions"]
            for hint in condition.get("label_hints", [])
        ]
        culprits = item["diagnosis"]["culprits"]
        report.defects.append(Defect(
            "selector-unreachable", side, item["rule"], item["check"],
            "seal 预检会拒绝：select 在本侧观测上零命中"
            + (f"（去掉条件 {culprits} 之一就能命中）" if culprits else "（基础候选观测为 0：证据缺失或场景/记录类型不符）"),
            hint="；".join(dict.fromkeys(hints)) or None,
        ))
    if not consistent:
        report.defects.append(Defect("engine-self-check", side, None, None, real_message))

    # 6) assert-rules
    unreachable_ids = {(item["rule"], item["check"]) for item in unreachable}
    outcomes, defects = evaluate_rules(profile, contract, observations, manifest, side, unreachable_ids)
    failed_rules = [item["rule"] for item in outcomes if item["status"] != "pass"]
    report.add(Step(
        "assert-rules",
        PASSED if not failed_rules else FAILED,
        f"{len(outcomes)} 条规则求值，未通过 {len(failed_rules)} 条"
        + (f"：{', '.join(failed_rules)}" if failed_rules else ""),
        {"rules": outcomes},
    ))
    report.defects.extend(defects)
    return report


# ---------------------------------------------------------------------------
# 总流程、输出
# ---------------------------------------------------------------------------


def load_profile(path: Path, rewrites: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = contract_module.load_profile(path)
    if rewrites:
        profile = rewrite_versions(profile, rewrites)
    return profile, contract_module.build_contract_payload(profile)


def run_preview(
    *,
    candidate_evidence: Path,
    labels: Path,
    profile_path: Path,
    official_evidence: Path | None = None,
    candidate_roots_dir: Path | None = None,
    official_roots_dir: Path | None = None,
    rewrites: Mapping[str, str] | None = None,
    work: Path | None = None,
    keep: bool = False,
    candidate_only: bool = False,
) -> dict[str, Any]:
    rewrites = dict(rewrites or {})
    declaration = catalog_module.load_label_declaration(Path(labels))
    profile, contract = load_profile(Path(profile_path), rewrites)
    own_work = work is None
    work_root = Path(tempfile.mkdtemp(prefix="registration-preview-")) if own_work else Path(work)
    work_root.mkdir(parents=True, exist_ok=True)
    sides: list[SideReport] = []
    try:
        jobs: list[tuple[str, Path, Path | None]] = [("candidate", candidate_evidence, candidate_roots_dir)]
        if official_evidence is not None:
            jobs.append(("official", official_evidence, official_roots_dir))
        for side, path, roots_dir in jobs:
            side_dir = work_root / side
            if side_dir.exists():
                shutil.rmtree(side_dir)
            side_dir.mkdir(parents=True)
            sides.append(run_side(
                side=side,
                evidence=locate_evidence(Path(path)),
                declaration=declaration,
                profile=profile,
                contract=contract,
                work_dir=side_dir,
                roots_dir=roots_dir,
            ))
        if official_evidence is None:
            skipped = SideReport("official")
            if candidate_only:
                skipped.add(Step("official-side", SKIPPED, "按 --candidate-only 只预演候选侧"))
            else:
                skipped.add(Step("official-side", NOT_RUN, "未提供官方侧证据（--official-evidence）：官方侧可达性与双线判据求值未预演；只预演候选侧请加 --candidate-only"))
            sides.append(skipped)
    finally:
        if own_work and not keep:
            shutil.rmtree(work_root, ignore_errors=True)
    defects = [dataclasses.asdict(defect) for side in sides for defect in side.defects]
    not_run = [step.name for side in sides for step in side.steps if step.status == NOT_RUN]
    verdict = "defects_found" if defects else ("incomplete" if not_run else "clean")
    return {
        "schema_version": SCHEMA,
        "labels": str(labels),
        "profile": str(profile_path),
        "rewrites": rewrites,
        "work_dir": str(work_root) if (keep or not own_work) else None,
        "verdict": verdict,
        "defect_count": len(defects),
        "defects": defects,
        "sides": [{"side": side.side, "steps": [dataclasses.asdict(step) for step in side.steps]} for side in sides],
    }


def render_text(result: Mapping[str, Any]) -> str:
    lines = [
        f"登记阶段离线预演：{result['verdict']}（缺陷 {result['defect_count']} 处）",
        f"  标签声明：{result['labels']}",
        f"  候选期望清单：{result['profile']}",
    ]
    if result["rewrites"]:
        lines.append(f"  清单版本字面量改写：{result['rewrites']}")
    for side in result["sides"]:
        lines.append(f"[{side['side']}]")
        for step in side["steps"]:
            lines.append(f"  - {step['name']}：{step['status']}  {step['summary']}")
    if result["defects"]:
        lines.append("缺陷明细（同一规则、同一类型、同一提示的合并成一条）：")
        groups: dict[tuple[Any, ...], list[Mapping[str, Any]]] = {}
        for defect in result["defects"]:
            if defect["kind"] == "selector-unreachable":
                key: tuple[Any, ...] = (defect["kind"], defect["side"], defect["rule"], defect["hint"])
            elif defect["kind"] == "assertion-mismatch" and defect["same_origin_as"]:
                key = (defect["kind"], defect["side"], defect["rule"], "same-origin")
            else:
                key = (defect["kind"], defect["side"], defect["rule"], defect["check"], defect["message"])
            groups.setdefault(key, []).append(defect)
        for number, members in enumerate(groups.values(), 1):
            head = members[0]
            where = "／".join(part for part in (head["side"], head["rule"]) if part)
            checks = "、".join(member["check"] for member in members if member["check"])
            if head["kind"] == "selector-unreachable":
                lines.append(f"  {number}. [{head['kind']}] {where}：{len(members)} 个 check 的 select 零命中，seal 预检会拒绝：{checks}")
            elif head["kind"] == "assertion-mismatch" and head["same_origin_as"]:
                lines.append(f"  {number}. [{head['kind']}] {where}：{len(members)} 个 check 因选择器零命中而判据求值为空集（与上一条同源，不重复计）：{checks}")
            else:
                suffix = f"／{head['check']}" if head["check"] else ""
                lines.append(f"  {number}. [{head['kind']}] {where}{suffix}：{head['message']}")
            if head["hint"]:
                lines.append(f"     提示：{head['hint']}")
    return "\n".join(lines)


def _parse_rewrites(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        old, separator, new = value.partition("=")
        if not separator or not old or not new:
            raise PreviewInputError(f"--rewrite-version 必须是 新=旧 形式：{value}")
        result[old] = new
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="登记阶段离线预演（标签、判据对上一版真实候选证据）")
    sub = parser.add_subparsers(dest="command", required=True)
    preview = sub.add_parser("preview", help="按新声明重新编目并跑 seal 预检与 assert-rules 全部检查")
    preview.add_argument("--candidate-evidence", type=Path, required=True)
    preview.add_argument("--official-evidence", type=Path)
    preview.add_argument("--labels", type=Path, required=True, help="新版证据标签声明")
    preview.add_argument("--profile", type=Path, required=True, help="新版候选期望清单（或批准画像）")
    preview.add_argument("--candidate-roots-dir", type=Path)
    preview.add_argument("--official-roots-dir", type=Path)
    preview.add_argument("--rewrite-version", action="append", default=[], metavar="新=旧")
    preview.add_argument("--work", type=Path)
    preview.add_argument("--keep", action="store_true")
    preview.add_argument("--candidate-only", action="store_true", help="明确只预演候选侧（不提供官方证据也不算未执行）")
    preview.add_argument("--json-out", type=Path)
    arguments = parser.parse_args(argv)
    try:
        result = run_preview(
            candidate_evidence=arguments.candidate_evidence,
            labels=arguments.labels,
            profile_path=arguments.profile,
            official_evidence=arguments.official_evidence,
            candidate_roots_dir=arguments.candidate_roots_dir,
            official_roots_dir=arguments.official_roots_dir,
            rewrites=_parse_rewrites(arguments.rewrite_version),
            work=arguments.work,
            keep=arguments.keep,
            candidate_only=arguments.candidate_only,
        )
    except (PreviewInputError, catalog_module.EvidenceCatalogError, contract_module.AcceptanceContractError) as error:
        print(f"预演输入有误：{error}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - 引擎自身出错要原样暴露
        traceback.print_exc()
        return 2
    if arguments.json_out is not None:
        arguments.json_out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(render_text(result))
    return EXIT_CODES[result["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
