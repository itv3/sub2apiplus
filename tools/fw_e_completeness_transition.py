#!/usr/bin/env python3
"""复核 FW-E 完整性补强的追加式 transition 与阻断收据。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_COMMIT = "f5e6d8c4ed899297b11b23a80d5384f43aed84ad"
TARGET_VERSION = "2.1.226"
PRIOR_DIR = ROOT / "docs/egress/maintenance/fw-e-workspace-transition"
PRIOR_MANIFEST_PATH = PRIOR_DIR / "manifest.json"
PRIOR_RECEIPT_PATH = PRIOR_DIR / "receipt.json"
PRIOR_MANIFEST_SHA256 = "1ffd02ac81062a7ae09a0cc72cdca7a6bfe0d4616f83b448ea7bb7242c66e1f9"
PRIOR_RECEIPT_SHA256 = "b0f31e42797dc079ea7127d80f63b6eaf59e81eed1c4c6cb54131f41b6e0e9da"
TRANSITION_DIR = ROOT / "docs/egress/maintenance/fw-e-completeness-supplement"
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_CHANGED_PATHS = {
    ".github/workflows/backend-ci.yml",
    "Makefile",
    "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
    "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "docs/egress/maintenance/fw-e-completeness-supplement/manifest.json",
    "tools/fw_e_completeness_transition.py",
    "tools/fw_e_workspace_transition.py",
    "tools/official_client_capture/README.md",
    "tools/official_client_capture/addons/mitm_capture.py",
    "tools/official_client_capture/capture.py",
    "tools/official_client_capture/capturelib/analysis.py",
    "tools/official_client_capture/capturelib/lifecycle.py",
    "tools/official_client_capture/claude_bundle_ast.mjs",
    "tools/official_client_capture/claude_fw_e.py",
    "tools/official_client_capture/claude_fw_e_crosswalk.py",
    "tools/official_client_capture/claude_target_inventory.py",
    "tools/official_client_capture/extract_claude_bundle.py",
    "tools/official_client_capture/tests/test_analysis.py",
    "tools/official_client_capture/tests/test_claude_fw_e.py",
    "tools/official_client_capture/tests/test_claude_fw_e_completeness.py",
    "tools/official_client_capture/tests/test_lifecycle.py",
    "tools/official_client_control/contracts.py",
    "tools/official_client_control/fw_e.py",
    "tools/official_client_control/tests/test_fw_e.py",
}

LOCAL_EVIDENCE_BINDINGS = {
    "baseline_sink_inventory": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/baseline-sink-inventory.json",
        "sha256": "bb8c4a8fd7e5465e8a21f2b4611e6d4e99f6e473b01c161e7e4b788850bdc741",
    },
    "closure": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/crosswalk-v2-unreviewed/closure.json",
        "sha256": "813502bfeb3e67bd38f9faf5b3d14cf164ce6d7cc2aa18870041bfe57b35a209",
    },
    "hitcc_2_1_197": {
        "path": "tools/official_client_capture/claude_21220/hitcc_2_1_197_coverage.json",
        "sha256": "c942f2f52c9254c31cfa80e764bb0d8269b9cca7aeab446afcdee027e6617b5c",
    },
    "matrix": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/crosswalk-v2-unreviewed/matrix.json",
        "sha256": "1388770e35428e7f93d6d5a4120dca887734be361f6dabee1cddd9abc145b50a",
    },
    "rules_2_1_220": {
        "path": "tools/official_client_capture/claude_21220/rules_2_1_220.json",
        "sha256": "a1d2d4d8fd492416da711b56854966103d370b4899f1166b4f666f704025879d",
    },
    "source_2_1_88": {
        "path": "tools/official_client_capture/claude_21220/source_2_1_88_coverage.json",
        "sha256": "be04643165027f798cfc48051577bcfcfb1c89cd0a00daf6b09616665737f6f8",
    },
    "target_inventory_darwin_arm64": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/static-target-native-v2/darwin-arm64/target-sink-inventory.json",
        "sha256": "ee10e883846dd6b0ee8beeb434fbdcbbb179e67a7946a0f8b2f2c14bff207b45",
    },
    "target_inventory_linux_amd64": {
        "path": "local-analysis/fw-e/claude-code-stable-20260818/completeness-supplement/static-target-native-v2/linux-amd64/target-sink-inventory.json",
        "sha256": "cae96a648f206d04103626e8f3da650c9aca7eebdd177310a1d6d99b671bbcb0",
    },
}

EXPECTED_CLOSURE = {
    "result": "blocked",
    "unresolved_total": 411,
    "source_candidate_count": 34,
    "hitcc_clue_count": 69,
    "target_sink_count": 307,
    "runtime_capture_scope_count": 1,
}


def sha256(content: bytes) -> str:
    """计算字节串 SHA-256。"""

    return hashlib.sha256(content).hexdigest()


def canonical_json_bytes(value: Any) -> bytes:
    """生成与受管控制工具一致的规范 JSON。"""

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
    """读取普通 JSON 文件。"""

    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} 不是可信普通文件：{path}")
    value = json.loads(path.read_bytes())
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} 顶层必须是对象")
    return value


def commit_file_state(commit: str, relative_path: str) -> dict[str, Any]:
    """读取某提交中普通文件的身份；不存在时返回明确状态。"""

    raw = run_git("ls-tree", "-z", commit, "--", relative_path)
    if not raw:
        return {
            "existence": "absent",
            "mode": "",
            "bytes": 0,
            "sha256": "",
        }
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
    """复算基准到补强封存提交的完整文件状态。"""

    entries: list[dict[str, Any]] = []
    for relative_path in sorted(EXPECTED_CHANGED_PATHS):
        before = commit_file_state(BASE_COMMIT, relative_path)
        after = commit_file_state(sealed_commit, relative_path)
        if after["existence"] != "present" or before == after:
            raise RuntimeError(f"补强路径未新增或未发生变化：{relative_path}")
        entries.append(
            {"path": relative_path, "before": before, "after": after}
        )
    return entries


def validate_prior_receipt() -> None:
    """固定旧 FW-E 历史收据，禁止原位补签。"""

    manifest_raw = PRIOR_MANIFEST_PATH.read_bytes()
    receipt_raw = PRIOR_RECEIPT_PATH.read_bytes()
    if sha256(manifest_raw) != PRIOR_MANIFEST_SHA256:
        raise RuntimeError("旧 FW-E manifest 原文漂移")
    if sha256(receipt_raw) != PRIOR_RECEIPT_SHA256:
        raise RuntimeError("旧 FW-E receipt 原文漂移")
    receipt = json.loads(receipt_raw)
    if (
        receipt.get("manifest_sha256") != PRIOR_MANIFEST_SHA256
        or receipt.get("result") != "passed"
    ):
        raise RuntimeError("旧 FW-E 历史收据链非法")


def validate_local_evidence() -> None:
    """可选复核私有静态产物；CI 不要求携带 local-analysis。"""

    for label, binding in LOCAL_EVIDENCE_BINDINGS.items():
        path = ROOT / binding["path"]
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"本地证据缺失：{label}={path}")
        if sha256(path.read_bytes()) != binding["sha256"]:
            raise RuntimeError(f"本地证据摘要漂移：{label}")
    closure = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["closure"]["path"], "补强 closure"
    )
    matrix = load_json(
        ROOT / LOCAL_EVIDENCE_BINDINGS["matrix"]["path"], "补强 matrix"
    )
    unresolved = closure.get("unresolved")
    if not isinstance(unresolved, dict):
        raise RuntimeError("补强 closure 缺少 unresolved")
    actual = {
        "result": closure.get("result"),
        "unresolved_total": closure.get("unresolved_total"),
        "source_candidate_count": len(unresolved.get("source_candidate_ids", [])),
        "hitcc_clue_count": len(unresolved.get("hitcc_clue_ids", [])),
        "target_sink_count": len(unresolved.get("target_sink_ids", [])),
        "runtime_capture_scope_count": len(
            unresolved.get("runtime_capture_scope", [])
        ),
    }
    if actual != EXPECTED_CLOSURE:
        raise RuntimeError(f"补强 closure 事实漂移：{actual}")
    if closure.get("matrix_sha256") != sha256(canonical_json_bytes(matrix)):
        raise RuntimeError("补强 closure 未绑定当前四方矩阵")
    print("FW-E 本地四方证据复核有效：411 项未闭合，结果保持 blocked")


def validate_transition(*, local_evidence: bool) -> None:
    """复核追加 manifest、封存提交、阻断收据和可选私有证据。"""

    validate_prior_receipt()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = load_json(MANIFEST_PATH, "FW-E completeness manifest")
    receipt = load_json(RECEIPT_PATH, "FW-E completeness receipt")
    sealed_commit = receipt.get("sealed_commit")
    if not isinstance(sealed_commit, str) or not re.fullmatch(
        r"[0-9a-f]{40}", sealed_commit
    ):
        raise RuntimeError("补强收据 sealed_commit 非法")
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, sealed_commit],
        cwd=ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("补强封存提交不是基准提交后继")
    changed = {
        item.decode("utf-8")
        for item in run_git(
            "diff", "--name-only", "-z", f"{BASE_COMMIT}..{sealed_commit}"
        ).split(b"\0")
        if item
    }
    if changed != EXPECTED_CHANGED_PATHS:
        raise RuntimeError(
            "FW-E 补强路径闭集漂移："
            f"missing={sorted(EXPECTED_CHANGED_PATHS - changed)}, "
            f"unexpected={sorted(changed - EXPECTED_CHANGED_PATHS)}"
        )
    if set(manifest.get("expected_changed_paths", [])) != EXPECTED_CHANGED_PATHS:
        raise RuntimeError("FW-E 补强 manifest 路径集合非法")
    if (
        manifest.get("schema_version")
        != "official-client-fw-e-completeness-supplement-transition/v1"
        or manifest.get("base_commit") != BASE_COMMIT
        or manifest.get("target_version") != TARGET_VERSION
        or manifest.get("prior_manifest_sha256") != PRIOR_MANIFEST_SHA256
        or manifest.get("prior_receipt_sha256") != PRIOR_RECEIPT_SHA256
    ):
        raise RuntimeError("FW-E 补强 manifest 身份非法")
    entries = transition_entries(sealed_commit)
    state_sha256 = sha256(canonical_json_bytes(entries))
    if (
        receipt.get("schema_version")
        != "official-client-fw-e-completeness-supplement-receipt/v1"
        or receipt.get("manifest_sha256") != sha256(manifest_raw)
        or receipt.get("base_commit") != BASE_COMMIT
        or receipt.get("target_version") != TARGET_VERSION
        or receipt.get("changed_path_count") != len(entries)
        or receipt.get("change_state_sha256") != state_sha256
        or receipt.get("prior_receipt_sha256") != PRIOR_RECEIPT_SHA256
        or receipt.get("local_evidence_bindings") != LOCAL_EVIDENCE_BINDINGS
        or receipt.get("closure") != EXPECTED_CLOSURE
        or receipt.get("implementation_result") != "passed"
        or receipt.get("evidence_closure_result") != "blocked"
        or receipt.get("production_runtime_path_count") != 0
        or receipt.get("deployment_performed") is not False
        or receipt.get("result") != "blocked"
    ):
        raise RuntimeError("FW-E 补强阻断收据与封存状态不一致")
    if local_evidence:
        validate_local_evidence()
    print(
        "FW-E 完整性补强 transition 有效："
        f"{len(entries)} 项实现已封存，证据闭集保持 blocked"
    )


def self_test() -> None:
    """检查关键判据不会把证据阻断误判成实施失败或生产发布。"""

    if any(path.startswith("backend/") for path in EXPECTED_CHANGED_PATHS):
        raise RuntimeError("FW-E 补强不得夹带生产后端路径")
    if RECEIPT_PATH.relative_to(ROOT).as_posix() in EXPECTED_CHANGED_PATHS:
        raise RuntimeError("收据不得递归进入自身封存路径")
    if EXPECTED_CLOSURE["result"] != "blocked" or not EXPECTED_CLOSURE[
        "unresolved_total"
    ]:
        raise RuntimeError("FW-E 补强必须保留真实阻断结论")
    if not all(
        SHA256_RE.fullmatch(binding["sha256"])
        for binding in LOCAL_EVIDENCE_BINDINGS.values()
    ):
        raise RuntimeError("FW-E 本地证据摘要非法")
    print("FW-E 完整性补强 transition 判据自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--local-evidence",
        action="store_true",
        help="额外复核被 gitignore 排除的本地四方证据原文",
    )
    arguments = parser.parse_args()
    if arguments.self_test:
        self_test()
        return 0
    validate_transition(local_evidence=arguments.local_evidence)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
