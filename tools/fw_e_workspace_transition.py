#!/usr/bin/env python3
"""生成或复算 FW-E 取证与 observation-only 发送面的追加式工作区 transition。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import stat
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_COMMIT = "7cbbb76e37118479a4618702357b62a95e9c88ec"
PRIOR_DIR = (
    ROOT / "docs" / "egress" / "maintenance" / "fw-d-control-workspace-transition"
)
PRIOR_MANIFEST_PATH = PRIOR_DIR / "manifest.json"
PRIOR_RECEIPT_PATH = PRIOR_DIR / "receipt.json"
PRIOR_MANIFEST_SHA256 = "0993579139bdbd15180af83509cee0343177c7ecc958b5199bf9376a27fbd7e4"
PRIOR_RECEIPT_SHA256 = "d32b7a4ab68afb8e92a757edc3e02d63f7b11e198ecd50a2ac7288df2107ded2"
TRANSITION_DIR = ROOT / "docs" / "egress" / "maintenance" / "fw-e-workspace-transition"
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
VERSION_PATH = ROOT / "backend" / "cmd" / "server" / "VERSION"
SOURCE_TRANSITION_PATH = (
    ROOT
    / "docs"
    / "egress"
    / "maintenance"
    / "fw-e-observation-source-transition.json"
)
SOURCE_TRANSITION_SHA256 = "dce3acb964556fdf6b6c1338463598d9256784831fa2e2667b143224da7dd36b"
FROZEN_ORCHESTRATOR_PATH = ROOT / "tools" / "official_client_capture" / "claude_fw_e.py"
FROZEN_ORCHESTRATOR_SHA256 = "3e1f383ab8e480d055957158d8171f7a9eaa257b00411c7f9c695f8d24412dcb"

OBSERVATION_RUNTIME_PATHS = {
    "backend/internal/officialegress/catalog.go",
    "backend/internal/officialegress/catalogdata/fw-e-legacy-observation-sinks.json",
    "backend/internal/officialegress/legacy_observation_catalog.go",
    "backend/internal/officialegress/route_catalog.go",
    "backend/internal/officialegress/sink_ids.go",
    "backend/internal/repository/claude_legacy_observation.go",
    "backend/internal/repository/claude_oauth_service.go",
    "backend/internal/repository/claude_usage_service.go",
    "backend/internal/service/account_test_service.go",
    "backend/internal/service/gateway_count_tokens.go",
    "backend/internal/service/gateway_upstream_request.go",
    "backend/internal/service/official_egress_1a_binding.go",
    "backend/internal/service/upstream_models.go",
}
OBSERVATION_TEST_PATHS = {
    "backend/internal/officialegress/fw_e_observation_source_transition_test.go",
    "backend/internal/officialegress/legacy_baseline_test.go",
    "backend/internal/officialegress/legacy_observation_catalog_test.go",
    "backend/internal/officialegress/officialegress_test.go",
    "backend/internal/officialegress/upstream_v0177_source_transition_test.go",
    "backend/internal/officialegress/websocket_tokenless_retirement_test.go",
    "backend/internal/repository/claude_legacy_observation_test.go",
    "backend/internal/service/claude_fw_e_observation_binding_test.go",
    "backend/internal/service/compatibility_code_retirement_closure_test.go",
    "backend/internal/service/fw_e_observation_source_transition_test.go",
    "backend/internal/service/upstream_v0177_source_transition_test.go",
}
EVIDENCE_TOOL_PATHS = {
    "tools/official_client_capture/capture.py",
    "tools/official_client_capture/capturelib/environment.py",
    "tools/official_client_capture/capturelib/identity.py",
    "tools/official_client_capture/capturelib/security.py",
    "tools/official_client_capture/claude_fw_e.py",
    "tools/official_client_capture/claude_fw_e_relay.py",
    "tools/official_client_capture/claude_fw_e_runtime_snapshot.py",
    "tools/official_client_capture/claude_oauth_refresh.py",
    "tools/official_client_capture/runtime_host_receipt.py",
    "tools/official_client_capture/scrub_raw_bytes.py",
}
EVIDENCE_TEST_PATHS = {
    "tools/official_client_capture/tests/test_capture_validation.py",
    "tools/official_client_capture/tests/test_claude_fw_e.py",
    "tools/official_client_capture/tests/test_claude_fw_e_relay.py",
    "tools/official_client_capture/tests/test_claude_fw_e_runtime_snapshot.py",
    "tools/official_client_capture/tests/test_claude_oauth_refresh.py",
    "tools/official_client_capture/tests/test_environment.py",
    "tools/official_client_capture/tests/test_security.py",
}
CONTROL_PATHS = {
    "tools/official_client_control/contracts.py",
    "tools/official_client_control/fw_e.py",
    "tools/official_client_control/gates.py",
    "tools/official_client_control/schemas/inventory.schema.json",
    "tools/official_client_control/tests/fixtures.py",
    "tools/official_client_control/tests/test_fw_e.py",
    "tools/official_client_control/tests/test_negative_gates.py",
}
DOCUMENT_PATHS = {
    "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
    "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "tools/official_client_capture/README.md",
}
TRANSITION_PATHS = {
    "docs/egress/maintenance/fw-e-observation-source-transition.json",
    "tools/fw_e_workspace_transition.py",
}
CI_PATHS = {"Makefile"}
ALLOWED_PATHS = (
    OBSERVATION_RUNTIME_PATHS
    | OBSERVATION_TEST_PATHS
    | EVIDENCE_TOOL_PATHS
    | EVIDENCE_TEST_PATHS
    | CONTROL_PATHS
    | DOCUMENT_PATHS
    | TRANSITION_PATHS
    | CI_PATHS
)
REQUIRED_PATHS = set(ALLOWED_PATHS)
EXCLUDED_PATHS = {
    MANIFEST_PATH.relative_to(ROOT).as_posix(),
    RECEIPT_PATH.relative_to(ROOT).as_posix(),
    VERSION_PATH.relative_to(ROOT).as_posix(),
}
PRESERVED_UNRELATED_PREFIXES = ("outputs/",)

PROOF_COMMANDS = (
    {
        "id": "capture-tools",
        "category": "evidence_tool",
        "cwd": ".",
        "argv": ["make", "test-capture-tools"],
    },
    {
        "id": "official-client-control",
        "category": "control_tool",
        "cwd": ".",
        "argv": ["make", "test-official-client-control"],
    },
    {
        "id": "backend-regression",
        "category": "runtime",
        "cwd": "backend",
        "argv": ["go", "test", "./...", "-count=1"],
    },
    {
        "id": "prior-history",
        "category": "history",
        "cwd": ".",
        "argv": [
            "python3",
            "tools/multi_persona_control_workspace_transition.py",
            "--frozen-only",
        ],
    },
    {
        "id": "runtime-selector-unchanged",
        "category": "runtime_scope",
        "cwd": ".",
        "argv": [
            "git",
            "diff",
            "--exit-code",
            BASE_COMMIT,
            "--",
            "backend/internal/officialegress/catalogdata/runtime",
            "backend/internal/officialegress/persona_registry.go",
            "backend/internal/officialegress/persona_release_catalog.go",
            "backend/internal/officialegress/release_catalog.go",
        ],
    },
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def run_git(*arguments: str) -> bytes:
    return subprocess.check_output(["git", *arguments], cwd=ROOT)


def empty_state() -> dict[str, Any]:
    return {
        "existence": "absent",
        "file_type": "absent",
        "mode": "",
        "size": 0,
        "sha256": "",
    }


def current_state(relative_path: str) -> dict[str, Any]:
    absolute = ROOT / pathlib.PurePosixPath(relative_path)
    try:
        metadata = absolute.lstat()
    except FileNotFoundError:
        return empty_state()
    if stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError(f"FW-E transition 禁止符号链接：{relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"FW-E transition 路径必须是普通文件：{relative_path}")
    content = absolute.read_bytes()
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "size": len(content),
        "sha256": sha256(content),
    }


def commit_state(commit: str, relative_path: str) -> dict[str, Any]:
    raw = run_git("ls-tree", "-z", commit, "--", relative_path)
    if not raw:
        return empty_state()
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise RuntimeError(f"FW-E 提交态路径不唯一：{relative_path}")
    metadata, actual_path = records[0].split(b"\t", 1)
    if actual_path.decode("utf-8", errors="strict") != relative_path:
        raise RuntimeError(f"FW-E 提交态路径漂移：{relative_path}")
    mode, object_type, object_id = metadata.decode("ascii").split(" ")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise RuntimeError(f"FW-E 提交态不是受支持普通文件：{relative_path}")
    content = run_git("cat-file", "blob", object_id)
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": "0755" if mode == "100755" else "0644",
        "size": len(content),
        "sha256": sha256(content),
    }


def status_paths() -> set[str]:
    raw = run_git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    fields = raw.split(b"\0")
    paths: set[str] = set()
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        text = field.decode("utf-8", errors="strict")
        if len(text) < 4:
            raise RuntimeError(f"无法解析 git status：{text!r}")
        status_code, relative = text[:2], text[3:]
        paths.add(relative)
        if "R" in status_code or "C" in status_code:
            if index >= len(fields) or not fields[index]:
                raise RuntimeError(f"重命名记录缺少历史路径：{text!r}")
            paths.add(fields[index].decode("utf-8", errors="strict"))
            index += 1
    return paths


def task_status_paths() -> set[str]:
    return {
        relative
        for relative in status_paths()
        if not any(relative.startswith(prefix) for prefix in PRESERVED_UNRELATED_PREFIXES)
    }


def committed_paths() -> set[str]:
    raw = run_git("diff", "--name-only", "-z", f"{BASE_COMMIT}..HEAD")
    return {
        value.decode("utf-8", errors="strict")
        for value in raw.split(b"\0")
        if value
    }


def scope_of(relative: str) -> str:
    if relative in CI_PATHS:
        return "ci_gate"
    if relative in OBSERVATION_RUNTIME_PATHS:
        return "observation_runtime"
    if relative in OBSERVATION_TEST_PATHS:
        return "observation_test"
    if relative in EVIDENCE_TOOL_PATHS:
        return "evidence_tool"
    if relative in EVIDENCE_TEST_PATHS:
        return "evidence_test"
    if relative in CONTROL_PATHS:
        return "control_tool"
    if relative in DOCUMENT_PATHS:
        return "documentation"
    if relative in TRANSITION_PATHS:
        return "transition"
    return "unexpected"


def validate_state(value: Any, relative: str) -> None:
    if value == empty_state():
        return
    if (
        not isinstance(value, dict)
        or value.get("existence") != "present"
        or value.get("file_type") != "regular"
        or value.get("mode") not in {"0644", "0755"}
        or not isinstance(value.get("size"), int)
        or value["size"] < 0
        or not isinstance(value.get("sha256"), str)
        or len(value["sha256"]) != 64
    ):
        raise RuntimeError(f"FW-E transition 状态非法：{relative}")


def validate_prior_transition() -> None:
    manifest_raw = PRIOR_MANIFEST_PATH.read_bytes()
    receipt_raw = PRIOR_RECEIPT_PATH.read_bytes()
    if sha256(manifest_raw) != PRIOR_MANIFEST_SHA256:
        raise RuntimeError("FW-D manifest 原文漂移")
    if sha256(receipt_raw) != PRIOR_RECEIPT_SHA256:
        raise RuntimeError("FW-D receipt 原文漂移")
    receipt = json.loads(receipt_raw)
    if (
        receipt.get("manifest_sha256") != PRIOR_MANIFEST_SHA256
        or receipt.get("result") != "passed"
        or receipt.get("shared_runtime_path_count") != 0
        or receipt.get("new_persona_artifact_path_count") != 0
    ):
        raise RuntimeError("FW-D 前序摘要链非法")


def validate_frozen_sources() -> None:
    if sha256(SOURCE_TRANSITION_PATH.read_bytes()) != SOURCE_TRANSITION_SHA256:
        raise RuntimeError("FW-E observation source transition 漂移")
    if sha256(FROZEN_ORCHESTRATOR_PATH.read_bytes()) != FROZEN_ORCHESTRATOR_SHA256:
        raise RuntimeError("FW-E 冻结编排器漂移")


def validate_no_strict_persona_additions() -> None:
    forbidden = (
        b"PersonaClaude",
        b'Persona("claude-code")',
        b"SinkStateCanaryEnforce",
        b"SinkStateEnforced",
        b"profile_schema",
        b"release_artifact",
        b"runtime_selector",
    )
    for relative in sorted(OBSERVATION_RUNTIME_PATHS):
        if commit_state(BASE_COMMIT, relative) == empty_state():
            added = (ROOT / relative).read_bytes()
        else:
            completed = subprocess.run(
                ["git", "diff", "--unified=0", BASE_COMMIT, "--", relative],
                cwd=ROOT,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if completed.returncode not in {0, 1}:
                raise RuntimeError(f"无法复核 FW-E 运行时增量：{relative}")
            added = b"\n".join(
                line[1:]
                for line in completed.stdout.splitlines()
                if line.startswith(b"+") and not line.startswith(b"+++")
            )
        matched = [token.decode("ascii") for token in forbidden if token in added]
        if matched:
            raise RuntimeError(f"FW-E 提前登记 strict Persona 事实：{relative}: {matched}")


def run_proofs() -> list[dict[str, Any]]:
    proofs: list[dict[str, Any]] = []
    for command in PROOF_COMMANDS:
        result = subprocess.run(
            command["argv"],
            cwd=ROOT / command["cwd"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        proof = {
            "id": command["id"],
            "category": command["category"],
            "cwd": command["cwd"],
            "argv": command["argv"],
            "exit_code": result.returncode,
            "stdout_bytes": len(result.stdout),
            "stdout_sha256": sha256(result.stdout),
            "stderr_bytes": len(result.stderr),
            "stderr_sha256": sha256(result.stderr),
        }
        proofs.append(proof)
        print(
            f"FW-E 机器证明 {command['id']}：exit_code={result.returncode} "
            f"stdout_sha256={proof['stdout_sha256']}"
        )
    return proofs


def validate_proofs(proofs: Any) -> None:
    if not isinstance(proofs, list) or len(proofs) != len(PROOF_COMMANDS):
        raise RuntimeError("FW-E transition 机器证明数量非法")
    for proof, expected in zip(proofs, PROOF_COMMANDS, strict=True):
        if (
            not isinstance(proof, dict)
            or proof.get("id") != expected["id"]
            or proof.get("category") != expected["category"]
            or proof.get("cwd") != expected["cwd"]
            or proof.get("argv") != expected["argv"]
            or proof.get("exit_code") != 0
        ):
            raise RuntimeError(f"FW-E transition 机器证明非法：{proof}")
        for prefix in ("stdout", "stderr"):
            if (
                not isinstance(proof.get(f"{prefix}_bytes"), int)
                or proof[f"{prefix}_bytes"] < 0
                or not isinstance(proof.get(f"{prefix}_sha256"), str)
                or len(proof[f"{prefix}_sha256"]) != 64
            ):
                raise RuntimeError(f"FW-E 机器证明输出摘要非法：{proof.get('id')}")


def build_transition(
    proofs: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, "HEAD"],
        cwd=ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("FW-E 基准提交不是当前 HEAD 的祖先")
    candidates = (committed_paths() | task_status_paths()) - EXCLUDED_PATHS
    unexpected = sorted(candidates - ALLOWED_PATHS)
    if unexpected:
        raise RuntimeError(f"FW-E 变更集夹带未批准路径：{unexpected}")
    entries: list[dict[str, Any]] = []
    for relative in sorted(candidates):
        before = commit_state(BASE_COMMIT, relative)
        after = current_state(relative)
        if before == after:
            continue
        entries.append(
            {
                "path": relative,
                "scope": scope_of(relative),
                "before": before,
                "after": after,
                "deletion_allowed": False,
                "reason": "完成 FW-E 最新 stable 取证、当前 Inventory 与 observation-only 发送面封存，不提前实现 Claude Persona",
                "machine_proofs": [command["id"] for command in PROOF_COMMANDS],
            }
        )
    paths = [entry["path"] for entry in entries]
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    manifest = {
        "schema_version": "official-client-fw-e-workspace-transition/v1",
        "prior_manifest_path": PRIOR_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "prior_manifest_sha256": PRIOR_MANIFEST_SHA256,
        "base_commit": BASE_COMMIT,
        "candidate_path_count": len(candidates),
        "transition_entry_count": len(entries),
        "transition_path_set_sha256": sha256(path_set_raw),
        "entries": entries,
        "rules": [
            "只允许 FW-E 取证、控制面、文档、observation-only 调用点和追加式 transition 进入变更集",
            "不得登记 Claude Persona、ProfileSchema、Snapshot、ReleaseArtifact 或 production strict binding",
            "Codex runtime selector、release catalog、画像和 final wire 必须保持 FW-C 原样",
            "流量类别是否出现不作一致性维度；essential 只界定 strict wire／PAIR 范围，telemetry 与 nonessential 仅记录且零流量不构成差异",
            "outputs/ 是用户无关未跟踪产物，不进入 manifest 或提交",
        ],
    }
    manifest_raw = canonical_json(manifest)
    proofs = [] if proofs is None else proofs
    proof_failures = sum(proof.get("exit_code") != 0 for proof in proofs)
    scope_counts: dict[str, int] = {}
    for entry in entries:
        scope_counts[entry["scope"]] = scope_counts.get(entry["scope"], 0) + 1
    receipt = {
        "schema_version": "official-client-fw-e-workspace-transition-receipt/v1",
        "manifest_path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256(manifest_raw),
        "prior_receipt_path": PRIOR_RECEIPT_PATH.relative_to(ROOT).as_posix(),
        "prior_receipt_sha256": PRIOR_RECEIPT_SHA256,
        "source_transition_path": SOURCE_TRANSITION_PATH.relative_to(ROOT).as_posix(),
        "source_transition_sha256": SOURCE_TRANSITION_SHA256,
        "base_commit": BASE_COMMIT,
        "transition_entry_count": len(entries),
        "added_entry_count": sum(
            entry["before"]["file_type"] == "absent" for entry in entries
        ),
        "deleted_entry_count": sum(
            entry["after"]["file_type"] == "absent" for entry in entries
        ),
        "scope_counts": dict(sorted(scope_counts.items())),
        "observation_runtime_path_count": sum(
            relative in OBSERVATION_RUNTIME_PATHS for relative in paths
        ),
        "production_selector_path_count": 0,
        "claude_persona_artifact_path_count": 0,
        "profile_snapshot_release_path_count": 0,
        "traffic_observation_policy": {
            "traffic_presence_comparison": "disabled",
            "strict_wire_traffic_classes": ["essential"],
            "record_only_traffic_classes": ["nonessential", "telemetry"],
            "absence_of_record_only_traffic": "conformant_not_a_difference",
        },
        "frozen_orchestrator_sha256": FROZEN_ORCHESTRATOR_SHA256,
        "proofs": proofs,
        "proof_failure_count": proof_failures,
        "result": "passed" if proofs and proof_failures == 0 else "failed",
    }
    return manifest, receipt


def validate_path_closure(manifest: dict[str, Any]) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("FW-E transition entries 非法")
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("FW-E transition 路径未严格排序或存在重复")
    missing = sorted(REQUIRED_PATHS - set(paths))
    unexpected = sorted(set(paths) - ALLOWED_PATHS)
    if missing or unexpected:
        raise RuntimeError(
            f"FW-E transition 路径闭集非法：missing={missing}, unexpected={unexpected}"
        )
    for entry in entries:
        relative = entry["path"]
        validate_state(entry.get("before"), relative)
        validate_state(entry.get("after"), relative)
        if entry.get("deletion_allowed") or entry["after"]["file_type"] == "absent":
            raise RuntimeError(f"FW-E transition 禁止删除：{relative}")
        if entry.get("scope") == "unexpected" or not entry.get("machine_proofs"):
            raise RuntimeError(f"FW-E transition 缺少 scope 或机器证明：{relative}")


def write_transition() -> None:
    validate_prior_transition()
    validate_frozen_sources()
    validate_no_strict_persona_additions()
    proofs = run_proofs()
    validate_proofs(proofs)
    manifest, receipt = build_transition(proofs)
    validate_path_closure(manifest)
    if receipt["proof_failure_count"] != 0:
        raise RuntimeError("FW-E 机器证明失败，禁止写入 passed transition")
    TRANSITION_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_bytes(canonical_json(manifest))
    RECEIPT_PATH.write_bytes(canonical_json(receipt))


def validate_transition() -> None:
    validate_prior_transition()
    validate_frozen_sources()
    validate_no_strict_persona_additions()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_raw)
    receipt = json.loads(RECEIPT_PATH.read_bytes())
    validate_proofs(receipt.get("proofs"))
    expected_manifest, expected_receipt = build_transition(receipt["proofs"])
    validate_path_closure(manifest)
    if manifest != expected_manifest or receipt != expected_receipt:
        raise RuntimeError("FW-E transition 与基准提交及当前状态复算结果不一致")
    if (
        receipt.get("manifest_sha256") != sha256(manifest_raw)
        or receipt.get("deleted_entry_count") != 0
        or receipt.get("production_selector_path_count") != 0
        or receipt.get("claude_persona_artifact_path_count") != 0
        or receipt.get("profile_snapshot_release_path_count") != 0
        or receipt.get("proof_failure_count") != 0
        or receipt.get("result") != "passed"
    ):
        raise RuntimeError("FW-E transition receipt 终态事实非法")
    print(
        "FW-E 工作区 transition 有效："
        f"{len(manifest['entries'])} 项，manifest SHA-256={sha256(manifest_raw)}"
    )


def self_test() -> None:
    present = {
        "existence": "present",
        "file_type": "regular",
        "mode": "0644",
        "size": 3,
        "sha256": "a" * 64,
    }
    validate_state(present, "sample.py")
    validate_state(empty_state(), "sample.py")
    for mutation in (
        {**present, "file_type": "symlink"},
        {**present, "mode": "0777"},
        {**present, "sha256": "a" * 63},
        {**empty_state(), "existence": "present"},
    ):
        try:
            validate_state(mutation, "mutation.py")
        except RuntimeError:
            continue
        raise RuntimeError(f"FW-E transition mutation 未被拒绝：{mutation}")
    if scope_of("backend/internal/officialegress/legacy_observation_catalog.go") != "observation_runtime":
        raise RuntimeError("FW-E scope 判据拒绝 observation-only 运行时")
    if "backend/internal/officialegress/persona_registry.go" in ALLOWED_PATHS:
        raise RuntimeError("FW-E scope 判据错误放行 Persona 注册")
    print("FW-E transition 判据 mutation 与 scope 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-transition", action="store_true", help="确定性生成 FW-E transition")
    parser.add_argument("--self-test", action="store_true", help="运行判据 mutation 与 scope 自测")
    args = parser.parse_args()
    if args.write_transition:
        write_transition()
    if args.self_test:
        self_test()
        return 0
    validate_transition()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
