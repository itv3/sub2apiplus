#!/usr/bin/env python3
"""复核 FW-E relay R 与逐项 disposition 的追加式阻断收据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_COMMIT = "205d7f58fa3bc6554d86f47f4b0a53e5d93ceb30"
TARGET_VERSION = "2.1.226"
PRIOR_RECEIPT_PATH = (
    ROOT
    / "docs/egress/maintenance/fw-e-runtime-evidence-supplement/receipt.json"
)
PRIOR_RECEIPT_SHA256 = (
    "de556ee14f1cddb62b401f76f2994f475e5c665450e28298a4fba1ca74d291a7"
)
TRANSITION_DIR = ROOT / "docs/egress/maintenance/fw-e-r-disposition-supplement"
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_CHANGED_PATHS = {
    "Makefile",
    "docs/egress/maintenance/fw-e-r-disposition-supplement/manifest.json",
    "tools/fw_e_r_disposition_transition.py",
    "tools/official_client_capture/README.md",
    "tools/official_client_capture/claude_bundle_ast.mjs",
    "tools/official_client_capture/claude_fw_e_crosswalk.py",
    "tools/official_client_capture/claude_fw_e_disposition_policy_2_1_226.json",
    "tools/official_client_capture/claude_fw_e_dispositions.py",
    "tools/official_client_capture/claude_sink_containment.mjs",
    "tools/official_client_capture/tests/test_claude_fw_e_completeness.py",
}

LOCAL_EVIDENCE_BINDINGS = {
    "capture_index_with_r": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-relay-205d7f58f/campaign/indexes/"
        "capture-index-full-with-r.json",
        "sha256": "4b16a52f9804e96d6454543881333af1305b42d6572682966a0caf6430b52d4a",
    },
    "campaign_identity": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-relay-205d7f58f/campaign/identity.json",
        "sha256": "e1fe7f769d1142546281184a2492979a7cfbbcf82daea966df2b9b6b51df19c6",
    },
    "production_after": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-relay-205d7f58f/finalization/after.json",
        "sha256": "df2129de20962e4367d29a5315d8cca3a418103c6bcff69feb58f685ab667787",
    },
    "campaign_secret_inventory": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-relay-205d7f58f/finalization/"
        "campaign-secret-inventory.json",
        "sha256": "c241ce9c0152cefa518451638db4c5ad1220a2ff8ee19c68bdcdf1d403c84aa9",
    },
    "production_compare": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-relay-205d7f58f/finalization/"
        "production-compare.json",
        "sha256": "20f2d7fa592fa1c98a97a89b5345fbfbe3c0e1dd8797ac99bde90f6146cbba18",
    },
    "target_sink_inventory": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/disposition-analysis-v3/target-sink-inventory.json",
        "sha256": "beac55c469ccc634ad81b09d6a142f049f732cd179cfb0e5af2885744e29688b",
    },
    "sink_containment": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/disposition-analysis-v3/sink-containment.json",
        "sha256": "444cf28b1da6040b5031e3e6f7f7275ffa131156fcde5e478d14e66faad59276",
    },
    "dispositions": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/disposition-v1-explicit/dispositions.json",
        "sha256": "6838915af8472a944c8acb1ae4a801912bbdc46b2036eb5d99dc17ce95542d41",
    },
    "explicit_review": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/disposition-v1-explicit/explicit-review.json",
        "sha256": "eabf6c84fd25224e5e4db5a4302b71fa62524a19263676ccfa1b914ff262f9e3",
    },
    "blockers": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/disposition-v1-explicit/blockers.json",
        "sha256": "a46a4aa6e3b3a6ad094ca67097737c52e53429e04eb048a8a250502456ad89fe",
    },
    "explicit_matrix": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/crosswalk-v5-explicit/matrix.json",
        "sha256": "e3dd45e99c97a5171fa1f7f24a59a16ebd7c86530ffd0070d6e8a53eb1fded0c",
    },
    "explicit_closure": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/crosswalk-v5-explicit/closure.json",
        "sha256": "f62c14e6055a8ebfc328e525ad8cb0fbc54bf319860b2a0fb22cac1b896a0544",
    },
    "require_closed_closure": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/crosswalk-v5-require-closed/closure.json",
        "sha256": "2a809734e13a8289a226429f6f76ff0e692b02c801ed14a6aaaab2c0cdc7680e",
    },
}

EXPECTED_R_CHANNEL = {
    "result": "passed",
    "baseline_version": "2.1.220",
    "target_version": "2.1.226",
    "channels": ["A1", "J", "L", "M", "P", "R"],
    "scenarios": ["a1", "s1", "s2", "s4"],
    "probe_ids": ["r-a1", "r-s1", "r-s2", "r-s4"],
    "control_probe_count": 4,
    "target_probe_count": 4,
    "target_network_observation_count": 4,
    "privacy_controls": {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
    },
}

EXPECTED_DISPOSITION_REVIEW = {
    "result": "passed",
    "denominator_counts": {
        "historical_source_candidates": 102,
        "hitcc_clues": 88,
        "hitcc_documents": 71,
        "runtime_observations": 4,
        "target_sinks": 505,
    },
    "denominator_total": 770,
    "explicit_disposition_total": 770,
    "unclassified_counts": {
        "historical_source_candidates": 29,
        "hitcc_clues": 71,
        "hitcc_documents": 69,
        "runtime_observations": 0,
        "target_sinks": 332,
    },
    "unclassified_total": 501,
}

EXPECTED_CLOSURE = {
    "result": "blocked",
    "unresolved_total": 501,
    "target_sink_total": 505,
    "target_sink_disposition_counts": {
        "mapped_managed": 17,
        "mapped_strict": 3,
        "out_of_scope_proven": 147,
        "record_only_disabled": 6,
        "unclassified": 332,
    },
    "historical_source_disposition_counts": {
        "mapped_historical": 48,
        "mapped_managed": 7,
        "out_of_scope_proven": 18,
        "unclassified": 29,
    },
    "hitcc_clue_disposition_counts": {
        "mapped_historical": 6,
        "mapped_managed": 1,
        "out_of_scope_proven": 10,
        "unclassified": 71,
    },
    "hitcc_document_disposition_counts": {
        "out_of_scope_proven": 2,
        "unclassified": 69,
    },
    "runtime_observation_disposition_counts": {"mapped_sink": 4},
    "target_add_rule_count": 0,
}

EXPECTED_FINALIZATION = {
    "campaign_secret_scan_result": "passed",
    "campaign_file_count": 233,
    "campaign_byte_count": 2074366,
    "campaign_inventory_sha256": (
        "23ff696df20a59c3b1298e77c19e0c318f0aa8f335d3e9afba4423a57267aa21"
    ),
    "production_compare_result": "passed",
    "production_difference_count": 0,
    "production_image_id": (
        "sha256:9399e13dea365354311476b919b39d2c9d28d538d125fa7fc397745a7101c096"
    ),
    "production_version": "0.1.177-4",
    "production_revision": "f5e6d8c4ed899297b11b23a80d5384f43aed84ad",
    "production_restart_count": 0,
}

EXPECTED_IMPLEMENTATION_PROOFS = [
    {
        "id": "capture-tools",
        "result": "passed",
        "passed_test_count": 757,
        "skipped_test_count": 3,
    },
    {
        "id": "official-client-control",
        "result": "passed",
        "passed_test_count": 30,
        "skipped_test_count": 0,
    },
    {"id": "real-disposition-replay", "result": "passed"},
    {"id": "require-closed", "result": "blocked-as-expected"},
    {"id": "prior-runtime-transition-replay", "result": "passed"},
]


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def sha256_file(path: pathlib.Path) -> str:
    """以流式方式计算大文件 SHA-256。"""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """生成控制 Store 使用的规范 JSON。"""

    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        + b"\n"
    )


def run_git(*arguments: str) -> bytes:
    """在仓库根执行只读 Git 命令。"""

    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def load_json(path: pathlib.Path, label: str) -> dict[str, Any]:
    """读取可信普通 JSON 文件。"""

    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} 不是可信普通文件：{path}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 顶层必须是对象")
    return value


def commit_file_state(commit: str, relative_path: str) -> dict[str, Any]:
    """返回提交中一个普通文件的稳定身份。"""

    raw = run_git("ls-tree", "-z", commit, "--", relative_path)
    if not raw:
        return {"existence": "absent", "mode": "", "bytes": 0, "sha256": ""}
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise RuntimeError(f"提交态路径不唯一：{relative_path}")
    metadata, actual = records[0].split(b"\t", 1)
    if actual.decode("utf-8") != relative_path:
        raise RuntimeError(f"提交态路径漂移：{relative_path}")
    mode, kind, object_id = metadata.decode("ascii").split(" ")
    if kind != "blob" or mode not in {"100644", "100755"}:
        raise RuntimeError(f"提交态不是受支持普通文件：{relative_path}")
    content = run_git("cat-file", "blob", object_id)
    return {
        "existence": "present",
        "mode": "0755" if mode == "100755" else "0644",
        "bytes": len(content),
        "sha256": sha256(content),
    }


def transition_entries(sealed_commit: str) -> list[dict[str, Any]]:
    """复算基准提交到封存提交的完整文件状态。"""

    entries: list[dict[str, Any]] = []
    for relative_path in sorted(EXPECTED_CHANGED_PATHS):
        before = commit_file_state(BASE_COMMIT, relative_path)
        after = commit_file_state(sealed_commit, relative_path)
        if after["existence"] != "present" or before == after:
            raise RuntimeError(f"R/disposition 路径未新增或未变化：{relative_path}")
        entries.append({"path": relative_path, "before": before, "after": after})
    return entries


def require_ancestor(ancestor: str, descendant: str, label: str) -> None:
    """要求指定提交位于封存提交祖先链上。"""

    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{label} 不在封存提交祖先链上")


def validate_prior_receipt() -> None:
    """固定上一份 FW-E 阻断收据，禁止原位补签。"""

    raw = PRIOR_RECEIPT_PATH.read_bytes()
    if sha256(raw) != PRIOR_RECEIPT_SHA256:
        raise RuntimeError("上一份 FW-E 运行证据收据原文漂移")
    receipt = json.loads(raw)
    if (
        receipt.get("sealed_commit")
        != "7c7236bb2365eabc90e212511163808ebfd989ad"
        or receipt.get("result") != "blocked"
        or receipt.get("fw_f_entered") is not False
    ):
        raise RuntimeError("上一份 FW-E 运行证据收据身份非法")


def r_channel_facts(capture: dict[str, Any]) -> dict[str, Any]:
    """提取 P/R/J/M 与控制组、目标组 relay 的闭合事实。"""

    control = capture.get("control", {})
    target = capture.get("target", {})
    relay = capture.get("relay", {})
    relay_control = relay.get("control", {})
    relay_target = relay.get("target", {})
    return {
        "result": relay.get("result"),
        "baseline_version": capture.get("baseline_version"),
        "target_version": capture.get("target_version"),
        "channels": capture.get("channels"),
        "scenarios": target.get("scenarios"),
        "probe_ids": relay.get("expected_probe_ids"),
        "control_probe_count": len(relay_control.get("runs", [])),
        "target_probe_count": len(relay_target.get("runs", [])),
        "target_network_observation_count": len(
            target.get("network_observations", [])
        ),
        "privacy_controls": target.get("privacy_controls", {}).get(
            "required_values"
        ),
    }


def disposition_review_facts(review: dict[str, Any]) -> dict[str, Any]:
    """提取逐项审阅分母和未闭项计数。"""

    return {
        key: review.get(key)
        for key in (
            "result",
            "denominator_counts",
            "denominator_total",
            "explicit_disposition_total",
            "unclassified_counts",
            "unclassified_total",
        )
    }


def closure_facts(closure: dict[str, Any]) -> dict[str, Any]:
    """提取四方矩阵的显式处置与证据闭集事实。"""

    return {
        key: closure.get(key)
        for key in (
            "result",
            "unresolved_total",
            "target_sink_total",
            "target_sink_disposition_counts",
            "historical_source_disposition_counts",
            "hitcc_clue_disposition_counts",
            "hitcc_document_disposition_counts",
            "runtime_observation_disposition_counts",
            "target_add_rule_count",
        )
    }


def validate_local_evidence() -> None:
    """复核被 gitignore 排除的 R、disposition 和生产安全证据。"""

    for label, binding in LOCAL_EVIDENCE_BINDINGS.items():
        path = ROOT / binding["path"]
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"本地证据缺失：{label}={path}")
        if sha256_file(path) != binding["sha256"]:
            raise RuntimeError(f"本地证据摘要漂移：{label}")

    capture = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["capture_index_with_r"]["path"],
        "带 R 的 capture index",
    )
    if (
        capture.get("schema_version") != "claude-code-fw-e-capture-index/v1"
        or capture.get("result") != "passed"
        or r_channel_facts(capture) != EXPECTED_R_CHANNEL
    ):
        raise RuntimeError(f"R 通道事实漂移：{r_channel_facts(capture)}")
    relay = capture["relay"]
    for label in ("control", "target"):
        group = capture[label]
        relay_group = relay[label]
        if (
            group.get("scenarios") != EXPECTED_R_CHANNEL["scenarios"]
            or group.get("capture_host_scopes") != ["all"]
            or group.get("privacy_controls", {}).get("result") != "passed"
            or relay_group.get("probe_ids") != EXPECTED_R_CHANNEL["probe_ids"]
            or any(run.get("injected_probe_env") != {} for run in relay_group["runs"])
        ):
            raise RuntimeError(f"{label} 的全 host/path、隐私开关或 R probe 漂移")

    identity = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["campaign_identity"]["path"],
        "R Campaign identity",
    )
    if (
        identity.get("schema_version") != "claude-code-fw-e-r-campaign-identity/v1"
        or identity.get("campaign")
        != "claude-code-stable-20260818-r-disposition-v1-205d7f58f"
        or identity.get("target_version") != TARGET_VERSION
        or identity.get("privacy_environment")
        != EXPECTED_R_CHANNEL["privacy_controls"]
        or identity.get("production_changes") is not False
    ):
        raise RuntimeError("R Campaign 身份或生产零变化声明漂移")

    review = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["explicit_review"]["path"],
        "显式 disposition review",
    )
    blockers = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["blockers"]["path"],
        "disposition blockers",
    )
    dispositions_sha256 = LOCAL_EVIDENCE_BINDINGS["dispositions"]["sha256"]
    if (
        review.get("schema_version")
        != "claude-code-fw-e-explicit-disposition-review/v1"
        or disposition_review_facts(review) != EXPECTED_DISPOSITION_REVIEW
        or review.get("dispositions_sha256") != dispositions_sha256
        or review.get("explicit_disposition_counts")
        != review.get("denominator_counts")
    ):
        raise RuntimeError("770 项显式 disposition review 漂移")
    policy_binding = review.get("policy_binding", {})
    policy_path = ROOT / str(policy_binding.get("path", ""))
    if (
        policy_path.is_symlink()
        or not policy_path.is_file()
        or sha256_file(policy_path) != policy_binding.get("sha256")
        or policy_path.stat().st_size != policy_binding.get("bytes")
    ):
        raise RuntimeError("显式 disposition 人工策略绑定漂移")
    if (
        blockers.get("schema_version")
        != "claude-code-fw-e-disposition-blockers/v1"
        or blockers.get("blocker_total") != 501
        or blockers.get("blocker_counts")
        != {
            "historical_source_candidates": 29,
            "hitcc_clues": 71,
            "hitcc_documents": 69,
            "target_sinks": 332,
        }
        or blockers.get("dispositions_sha256") != dispositions_sha256
        or blockers.get("result") != "blocked"
    ):
        raise RuntimeError("501 项补证队列漂移")

    matrix = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["explicit_matrix"]["path"],
        "显式 crosswalk matrix",
    )
    closure = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["explicit_closure"]["path"],
        "显式 crosswalk closure",
    )
    closed = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["require_closed_closure"]["path"],
        "require-closed closure",
    )
    if (
        closure.get("schema_version") != "claude-code-fw-e-completeness/v1"
        or closure.get("matrix_sha256") != sha256(canonical_json_bytes(matrix))
        or closure_facts(closure) != EXPECTED_CLOSURE
        or closure.get("explicit_dispositions", {}).get("result") != "passed"
        or closure.get("explicit_dispositions", {}).get("explicit_total") != 770
        or closure.get("explicit_dispositions", {}).get("expected_total") != 770
        or closure_facts(closed) != EXPECTED_CLOSURE
    ):
        raise RuntimeError("四方矩阵显式覆盖或 require-closed 阻断事实漂移")

    secret = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["campaign_secret_inventory"]["path"],
        "Campaign 秘密扫描",
    )
    scan = secret.get("secret_scan", {})
    if (
        secret.get("passed") is not True
        or secret.get("tree_stable_during_scan") is not True
        or secret.get("file_count") != EXPECTED_FINALIZATION["campaign_file_count"]
        or secret.get("byte_count") != EXPECTED_FINALIZATION["campaign_byte_count"]
        or secret.get("inventory_sha256")
        != EXPECTED_FINALIZATION["campaign_inventory_sha256"]
        or scan.get("algorithm") != "exact-byte-match/v1"
        or scan.get("matches") != []
        or scan.get("scan_errors") != []
        or scan.get("passed") is not True
    ):
        raise RuntimeError("R Campaign 秘密扫描或终态 inventory 漂移")

    after = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["production_after"]["path"],
        "生产终态快照",
    )
    compare = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["production_compare"]["path"],
        "生产前后比较",
    )
    production = next(
        (row for row in after.get("containers", []) if row.get("name") == "sub2apiplus"),
        None,
    )
    if not isinstance(production, dict):
        raise RuntimeError("生产终态缺少 sub2apiplus 容器")
    labels = production.get("image_labels", {})
    if (
        compare.get("result") != "passed"
        or compare.get("differences") != []
        or compare.get("after_sha256")
        != LOCAL_EVIDENCE_BINDINGS["production_after"]["sha256"]
        or any(item.get("acceptable") is not True for item in compare.get("health", []))
        or production.get("image_id") != EXPECTED_FINALIZATION["production_image_id"]
        or labels.get("org.opencontainers.image.version")
        != EXPECTED_FINALIZATION["production_version"]
        or labels.get("org.opencontainers.image.revision")
        != EXPECTED_FINALIZATION["production_revision"]
        or production.get("restart_count")
        != EXPECTED_FINALIZATION["production_restart_count"]
    ):
        raise RuntimeError("Vircs 生产前后零差异事实漂移")
    print("FW-E 私有证据有效：R 已补齐，770/770 已显式处置，501 项保持 blocked")


def validate_transition(*, local_evidence: bool) -> None:
    """复核追加 manifest、封存提交、阻断收据和可选私有证据。"""

    validate_prior_receipt()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = load_json(MANIFEST_PATH, "FW-E R/disposition manifest")
    receipt = load_json(RECEIPT_PATH, "FW-E R/disposition receipt")
    sealed_commit = receipt.get("sealed_commit")
    if not isinstance(sealed_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", sealed_commit
    ):
        raise RuntimeError("R/disposition 收据 sealed_commit 非法")
    require_ancestor(BASE_COMMIT, sealed_commit, "R/disposition 基准")
    changed = {
        item.decode("utf-8")
        for item in run_git(
            "diff", "--name-only", "-z", f"{BASE_COMMIT}..{sealed_commit}"
        ).split(b"\0")
        if item
    }
    if changed != EXPECTED_CHANGED_PATHS:
        raise RuntimeError(
            "FW-E R/disposition 路径闭集漂移："
            f"missing={sorted(EXPECTED_CHANGED_PATHS - changed)}, "
            f"unexpected={sorted(changed - EXPECTED_CHANGED_PATHS)}"
        )
    if set(manifest.get("expected_changed_paths", [])) != EXPECTED_CHANGED_PATHS:
        raise RuntimeError("FW-E R/disposition manifest 路径集合非法")
    if (
        manifest.get("schema_version")
        != "official-client-fw-e-r-disposition-supplement-transition/v1"
        or manifest.get("base_commit") != BASE_COMMIT
        or manifest.get("target_version") != TARGET_VERSION
        or manifest.get("prior_receipt_sha256") != PRIOR_RECEIPT_SHA256
    ):
        raise RuntimeError("FW-E R/disposition manifest 身份非法")

    entries = transition_entries(sealed_commit)
    state_sha256 = sha256(canonical_json_bytes(entries))
    if (
        receipt.get("schema_version")
        != "official-client-fw-e-r-disposition-supplement-receipt/v1"
        or receipt.get("manifest_sha256") != sha256(manifest_raw)
        or receipt.get("base_commit") != BASE_COMMIT
        or receipt.get("target_version") != TARGET_VERSION
        or receipt.get("changed_path_count") != len(entries)
        or receipt.get("change_state_sha256") != state_sha256
        or receipt.get("prior_receipt_sha256") != PRIOR_RECEIPT_SHA256
        or receipt.get("local_evidence_bindings") != LOCAL_EVIDENCE_BINDINGS
        or receipt.get("r_channel") != EXPECTED_R_CHANNEL
        or receipt.get("disposition_review") != EXPECTED_DISPOSITION_REVIEW
        or receipt.get("closure") != EXPECTED_CLOSURE
        or receipt.get("finalization") != EXPECTED_FINALIZATION
        or receipt.get("implementation_proofs") != EXPECTED_IMPLEMENTATION_PROOFS
        or receipt.get("implementation_result") != "passed"
        or receipt.get("explicit_disposition_result") != "passed"
        or receipt.get("evidence_closure_result") != "blocked"
        or receipt.get("evidence_approval_issued") is not False
        or receipt.get("snapshot_or_profile_generated") is not False
        or receipt.get("production_runtime_path_count") != 0
        or receipt.get("deployment_performed") is not False
        or receipt.get("dmit_deployment_performed") is not False
        or receipt.get("fw_f_entered") is not False
        or receipt.get("result") != "blocked"
    ):
        raise RuntimeError("FW-E R/disposition 阻断收据与封存状态不一致")
    if local_evidence:
        validate_local_evidence()
    print(
        "FW-E R/disposition transition 有效："
        f"{len(entries)} 项实现已封存，501 项证据缺口保持 blocked"
    )


def self_test() -> None:
    """检查判据不会误签范围外、生产部署或 FW-F 准入。"""

    if any(path.startswith("backend/") for path in EXPECTED_CHANGED_PATHS):
        raise RuntimeError("R/disposition 补强不得夹带生产后端路径")
    if RECEIPT_PATH.relative_to(ROOT).as_posix() in EXPECTED_CHANGED_PATHS:
        raise RuntimeError("收据不得递归进入自身封存路径")
    if EXPECTED_R_CHANNEL["result"] != "passed" or "R" not in EXPECTED_R_CHANNEL["channels"]:
        raise RuntimeError("R 通道必须真实通过")
    if (
        EXPECTED_DISPOSITION_REVIEW["denominator_total"] != 770
        or EXPECTED_DISPOSITION_REVIEW["explicit_disposition_total"] != 770
        or EXPECTED_DISPOSITION_REVIEW["unclassified_total"] != 501
    ):
        raise RuntimeError("显式覆盖与证据未闭项必须独立记录")
    if EXPECTED_CLOSURE["result"] != "blocked" or EXPECTED_CLOSURE["unresolved_total"] != 501:
        raise RuntimeError("require-closed 必须保留真实阻断结论")
    if not all(
        SHA256_RE.fullmatch(binding["sha256"])
        for binding in LOCAL_EVIDENCE_BINDINGS.values()
    ):
        raise RuntimeError("FW-E R/disposition 私有证据摘要非法")
    print("FW-E R/disposition transition 判据自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--local-evidence",
        action="store_true",
        help="额外复核被 gitignore 排除的 R 与 disposition 证据原文",
    )
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
        return 0
    validate_transition(local_evidence=arguments.local_evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
