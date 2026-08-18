#!/usr/bin/env python3
"""复核 FW-E 全 host/path 运行证据的追加式 transition 与阻断收据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_COMMIT = "db9a30747b6c729a692e056026d5611a92cebc0b"
ENVIRONMENT_FIX_COMMIT = "8b1cef38c1b99b59b62aaac286b41978f0f467c5"
TARGET_VERSION = "2.1.226"
PRIOR_RECEIPT_PATH = (
    ROOT
    / "docs/egress/maintenance/fw-e-completeness-supplement/receipt.json"
)
PRIOR_RECEIPT_SHA256 = (
    "2b19d07cf143f3a7593426d8efb02b9630c5095020ce296874034f230930db53"
)
TRANSITION_DIR = (
    ROOT / "docs/egress/maintenance/fw-e-runtime-evidence-supplement"
)
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_CHANGED_PATHS = {
    "Makefile",
    "docs/egress/maintenance/fw-e-runtime-evidence-supplement/manifest.json",
    "tools/fw_e_runtime_evidence_transition.py",
    "tools/official_client_capture/claude_fw_e.py",
    "tools/official_client_capture/tests/test_claude_fw_e.py",
}

LOCAL_EVIDENCE_BINDINGS = {
    "capture_index": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-capture-db9a30747/capture-index-full.json",
        "sha256": "9382492469491dbe1a5482569bf8fd4fb00764904d55aece3a87af8c3a4b0714",
    },
    "matrix": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/crosswalk-v3-runtime-unreviewed/matrix.json",
        "sha256": "20e08183bf1a04ce550d4408a6161f7fde81cc5e928bc96d6f8acd8ef02e89ae",
    },
    "closure": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/crosswalk-v3-runtime-unreviewed/closure.json",
        "sha256": "ff8fc5e1cefddebe9eacf552798bfc2403dd37ecfeb02af3df2cb11e13f400ef",
    },
    "campaign_secret_inventory": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-capture-db9a30747/finalization/"
        "campaign-secret-inventory.json",
        "sha256": "fb17ed48dfb12077d8d7aa9fcca727ee6af9808f4d0558098e3889753f7175c2",
    },
    "after_final": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-capture-db9a30747/finalization/"
        "after-final.json",
        "sha256": "ed2f400171b93967ec30fd972417f0df499cc2b3d3e7b5c8fd05c0d746e96119",
    },
    "production_compare_final": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-capture-db9a30747/finalization/"
        "production-compare-final.json",
        "sha256": "5c37efa237eba2d9ba36dbd9e90261681d7b8762e9e446dde4b91f233e9a6569",
    },
    "production_safety_final": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/"
        "completeness-supplement/runtime-capture-db9a30747/finalization/"
        "production-safety-final.json",
        "sha256": "93f55b6279c99154d8cb1c7801f448f86df10d49c77f957977bd0828512e2043",
    },
}

EXPECTED_CAPTURE = {
    "result": "passed",
    "baseline_version": "2.1.220",
    "target_version": "2.1.226",
    "control_case_count": 8,
    "target_case_count": 8,
    "scenarios": ["a1", "s1", "s2", "s4"],
    "evidence_modes": ["direct", "mitm"],
    "capture_host_scopes": ["all"],
    "channels": ["A1", "J", "L", "M", "P"],
    "relay_r_present": False,
    "network_observation_count": 4,
    "host_prefilter_disabled": True,
}

EXPECTED_CLOSURE = {
    "result": "blocked",
    "unresolved_total": 414,
    "source_candidate_count": 34,
    "hitcc_clue_count": 69,
    "target_sink_count": 307,
    "runtime_observation_count": 4,
}

EXPECTED_FINALIZATION = {
    "campaign_secret_scan_result": "passed",
    "campaign_file_count": 325,
    "campaign_byte_count": 3649150,
    "campaign_inventory_sha256": (
        "e2960d23b1faf3fb51bab1ef2339b0ec94716be02e8edff55a35cc599b81a0c9"
    ),
    "production_compare_result": "passed",
    "production_difference_count": 0,
    "production_image_id": (
        "sha256:9399e13dea365354311476b919b39d2c9d28d538d125fa7fc397745a7101c096"
    ),
    "production_restart_count": 0,
    "production_health_http_status": 200,
    "fatal_log_count": 0,
    "egress_guard_failure_log_count": 0,
    "isolated_container_absent": True,
}


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """生成控制 Store 使用的带末尾换行规范 JSON。"""

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
    """返回一个提交中普通文件的稳定身份。"""

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
    """复算基准到运行证据封存提交的完整文件状态。"""

    entries: list[dict[str, Any]] = []
    for relative_path in sorted(EXPECTED_CHANGED_PATHS):
        before = commit_file_state(BASE_COMMIT, relative_path)
        after = commit_file_state(sealed_commit, relative_path)
        if after["existence"] != "present" or before == after:
            raise RuntimeError(f"运行证据补强路径未新增或未变化：{relative_path}")
        entries.append({"path": relative_path, "before": before, "after": after})
    return entries


def require_ancestor(ancestor: str, descendant: str, label: str) -> None:
    """要求指定提交处于封存提交祖先链上。"""

    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=ROOT,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"{label} 不在封存提交祖先链上")


def validate_prior_receipt() -> None:
    """固定上一份阻断收据，禁止原位补签。"""

    raw = PRIOR_RECEIPT_PATH.read_bytes()
    if sha256(raw) != PRIOR_RECEIPT_SHA256:
        raise RuntimeError("上一份 FW-E 完整性补强收据原文漂移")
    receipt = json.loads(raw)
    if receipt.get("sealed_commit") != (
        "83d02f1ac07c983cd6c39f13384034849eae7d4e"
    ) or receipt.get("result") != "blocked":
        raise RuntimeError("上一份 FW-E 完整性补强收据身份非法")


def capture_facts(capture: dict[str, Any]) -> dict[str, Any]:
    """提取全 host/path Campaign 的可复算事实。"""

    control = capture.get("control") if isinstance(capture.get("control"), dict) else {}
    target = capture.get("target") if isinstance(capture.get("target"), dict) else {}
    network = (
        capture.get("network_inventory")
        if isinstance(capture.get("network_inventory"), dict)
        else {}
    )

    def case_count(group: dict[str, Any]) -> int:
        runs = group.get("runs") if isinstance(group.get("runs"), list) else []
        return sum(
            int(run.get("case_count", 0))
            for run in runs
            if isinstance(run, dict)
        )

    return {
        "result": capture.get("result"),
        "baseline_version": capture.get("baseline_version"),
        "target_version": capture.get("target_version"),
        "control_case_count": case_count(control),
        "target_case_count": case_count(target),
        "scenarios": target.get("scenarios"),
        "evidence_modes": target.get("evidence_modes"),
        "capture_host_scopes": target.get("capture_host_scopes"),
        "channels": capture.get("channels"),
        "relay_r_present": capture.get("relay") is not None,
        "network_observation_count": network.get("target_observation_count"),
        "host_prefilter_disabled": network.get("host_prefilter_disabled"),
    }


def closure_facts(closure: dict[str, Any]) -> dict[str, Any]:
    """提取四方矩阵仍未闭合的精确计数。"""

    unresolved = (
        closure.get("unresolved")
        if isinstance(closure.get("unresolved"), dict)
        else {}
    )
    return {
        "result": closure.get("result"),
        "unresolved_total": closure.get("unresolved_total"),
        "source_candidate_count": len(unresolved.get("source_candidate_ids", [])),
        "hitcc_clue_count": len(unresolved.get("hitcc_clue_ids", [])),
        "target_sink_count": len(unresolved.get("target_sink_ids", [])),
        "runtime_observation_count": len(
            unresolved.get("runtime_observation_ids", [])
        ),
    }


def validate_local_evidence() -> None:
    """复核被 gitignore 排除的私有证据原文和终态安全事实。"""

    documents: dict[str, dict[str, Any]] = {}
    for label, binding in LOCAL_EVIDENCE_BINDINGS.items():
        path = ROOT / binding["path"]
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"本地证据缺失：{label}={path}")
        if sha256(path.read_bytes()) != binding["sha256"]:
            raise RuntimeError(f"本地证据摘要漂移：{label}")
        documents[label] = load_json(path, label)

    capture = documents["capture_index"]
    if capture.get("schema_version") != "claude-code-fw-e-capture-index/v1":
        raise RuntimeError("全 host/path capture index schema 非法")
    if capture_facts(capture) != EXPECTED_CAPTURE:
        raise RuntimeError(f"全 host/path Campaign 事实漂移：{capture_facts(capture)}")
    for label in ("control", "target"):
        group = capture[label]
        privacy = group.get("privacy_controls", {})
        if privacy.get("required_values") != {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
        } or privacy.get("result") != "passed":
            raise RuntimeError(f"{label} 隐私开关实际值或门禁漂移")

    matrix = documents["matrix"]
    closure = documents["closure"]
    if closure.get("schema_version") != "claude-code-fw-e-completeness/v1":
        raise RuntimeError("运行补强 closure schema 非法")
    if closure.get("matrix_sha256") != sha256(canonical_json_bytes(matrix)):
        raise RuntimeError("运行补强 closure 未绑定当前矩阵")
    if closure_facts(closure) != EXPECTED_CLOSURE:
        raise RuntimeError(f"运行补强 closure 事实漂移：{closure_facts(closure)}")

    secret = documents["campaign_secret_inventory"]
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
        raise RuntimeError("Campaign 终态精确秘密扫描或文件 inventory 非法")

    compare = documents["production_compare_final"]
    if compare.get("result") != "passed" or compare.get("differences") != []:
        raise RuntimeError("隔离容器清理后的生产比较未通过")
    safety = documents["production_safety_final"]
    application = safety.get("application", {})
    isolation = safety.get("isolation_cleanup", {})
    if (
        safety.get("passed") is not True
        or safety.get("deployment_performed") is not False
        or safety.get("production_route_or_sink_changed") is not False
        or application.get("image_id")
        != EXPECTED_FINALIZATION["production_image_id"]
        or application.get("restart_count") != 0
        or application.get("health_http_status") != 200
        or application.get("fatal_log_count") != 0
        or application.get("egress_guard_failure_log_count") != 0
        or isolation.get("absent") is not True
        or isolation.get("campaign_preserved") is not True
    ):
        raise RuntimeError("Vircs 生产安全或隔离清理事实非法")
    print("FW-E 私有运行证据复核有效：414 项未闭合，R 缺失，结果保持 blocked")


def validate_transition(*, local_evidence: bool) -> None:
    """复核追加 manifest、封存提交、阻断收据和可选私有证据。"""

    validate_prior_receipt()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = load_json(MANIFEST_PATH, "FW-E runtime evidence manifest")
    receipt = load_json(RECEIPT_PATH, "FW-E runtime evidence receipt")
    sealed_commit = receipt.get("sealed_commit")
    if not isinstance(sealed_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", sealed_commit
    ):
        raise RuntimeError("运行证据补强收据 sealed_commit 非法")
    require_ancestor(BASE_COMMIT, sealed_commit, "运行证据补强基准")
    require_ancestor(ENVIRONMENT_FIX_COMMIT, sealed_commit, "环境摘要修正提交")
    changed = {
        item.decode("utf-8")
        for item in run_git(
            "diff", "--name-only", "-z", f"{BASE_COMMIT}..{sealed_commit}"
        ).split(b"\0")
        if item
    }
    if changed != EXPECTED_CHANGED_PATHS:
        raise RuntimeError(
            "FW-E 运行证据补强路径闭集漂移："
            f"missing={sorted(EXPECTED_CHANGED_PATHS - changed)}, "
            f"unexpected={sorted(changed - EXPECTED_CHANGED_PATHS)}"
        )
    if set(manifest.get("expected_changed_paths", [])) != EXPECTED_CHANGED_PATHS:
        raise RuntimeError("FW-E 运行证据 manifest 路径集合非法")
    if (
        manifest.get("schema_version")
        != "official-client-fw-e-runtime-evidence-supplement-transition/v1"
        or manifest.get("base_commit") != BASE_COMMIT
        or manifest.get("target_version") != TARGET_VERSION
        or manifest.get("prior_receipt_sha256") != PRIOR_RECEIPT_SHA256
    ):
        raise RuntimeError("FW-E 运行证据 manifest 身份非法")

    entries = transition_entries(sealed_commit)
    state_sha256 = sha256(canonical_json_bytes(entries))
    if (
        receipt.get("schema_version")
        != "official-client-fw-e-runtime-evidence-supplement-receipt/v1"
        or receipt.get("manifest_sha256") != sha256(manifest_raw)
        or receipt.get("base_commit") != BASE_COMMIT
        or receipt.get("target_version") != TARGET_VERSION
        or receipt.get("changed_path_count") != len(entries)
        or receipt.get("change_state_sha256") != state_sha256
        or receipt.get("prior_receipt_sha256") != PRIOR_RECEIPT_SHA256
        or receipt.get("local_evidence_bindings") != LOCAL_EVIDENCE_BINDINGS
        or receipt.get("capture") != EXPECTED_CAPTURE
        or receipt.get("closure") != EXPECTED_CLOSURE
        or receipt.get("finalization") != EXPECTED_FINALIZATION
        or receipt.get("implementation_result") != "passed"
        or receipt.get("evidence_closure_result") != "blocked"
        or receipt.get("production_runtime_path_count") != 0
        or receipt.get("deployment_performed") is not False
        or receipt.get("result") != "blocked"
    ):
        raise RuntimeError("FW-E 运行证据阻断收据与封存状态不一致")
    if local_evidence:
        validate_local_evidence()
    print(
        "FW-E 运行证据 transition 有效："
        f"{len(entries)} 项实现已封存，证据闭集保持 blocked"
    )


def self_test() -> None:
    """检查关键判据不会误签 R、生产发布或 Evidence 批准。"""

    if any(path.startswith("backend/") for path in EXPECTED_CHANGED_PATHS):
        raise RuntimeError("FW-E 运行证据补强不得夹带生产后端路径")
    if RECEIPT_PATH.relative_to(ROOT).as_posix() in EXPECTED_CHANGED_PATHS:
        raise RuntimeError("收据不得递归进入自身封存路径")
    if EXPECTED_CAPTURE["relay_r_present"] is not False:
        raise RuntimeError("当前 Campaign 不得伪造 R 通道")
    if EXPECTED_CLOSURE["result"] != "blocked" or (
        EXPECTED_CLOSURE["unresolved_total"] != 414
    ):
        raise RuntimeError("FW-E 运行证据必须保留真实阻断结论")
    if not all(
        SHA256_RE.fullmatch(binding["sha256"])
        for binding in LOCAL_EVIDENCE_BINDINGS.values()
    ):
        raise RuntimeError("FW-E 私有运行证据摘要非法")
    print("FW-E 运行证据 transition 判据自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--local-evidence",
        action="store_true",
        help="额外复核被 gitignore 排除的私有运行证据原文",
    )
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
        return 0
    validate_transition(local_evidence=arguments.local_evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
