#!/usr/bin/env python3
"""冻结首轮收据，并生成或校验 FW-B 后继多 Persona 工作区 transition。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import stat
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_COMMIT = "a698aebe1f0dfb279e7a4f137fff9394be8534e3"
V1_TRANSITION_DIR = (
    ROOT
    / "docs"
    / "egress"
    / "maintenance"
    / "multi-persona-control-workspace-transition"
)
V1_MANIFEST_PATH = V1_TRANSITION_DIR / "manifest.json"
V1_RECEIPT_PATH = V1_TRANSITION_DIR / "receipt.json"
V1_SOURCE_TRANSITION_PATH = (
    ROOT / "docs" / "egress" / "maintenance" / "multi-persona-control-source-transition.json"
)
V1_TEST_TRANSITION_PATH = (
    ROOT / "docs" / "egress" / "maintenance" / "multi-persona-control-test-transition.json"
)
V1_FROZEN_SHA256 = {
    V1_SOURCE_TRANSITION_PATH: "139f4844085942b709a68b61d2b51f863189ada780d361ace91cad8a6ae86bb2",
    V1_TEST_TRANSITION_PATH: "bb9b3e749dcc705b11d7b22bf2d6bb6f7d04bba8a1ccf5d9ae54d04475f21814",
    V1_MANIFEST_PATH: "876059bb1eee72bd20ad0f0ac9d09dd01c388f908bcba02b06507c4bb1986d15",
    V1_RECEIPT_PATH: "19becb4b094be770e06cd4365c5c1ce679be60dc652fc2e3a285419f7223aa18",
}

TRANSITION_DIR = (
    ROOT
    / "docs"
    / "egress"
    / "maintenance"
    / "multi-persona-control-workspace-transition-v2"
)
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
V2_FROZEN_MANIFEST_SHA256 = "1d37b7ef22da8e0284a8175cc4d159ee781cab7ed0cd37422e21af45259698be"
V2_FROZEN_RECEIPT_SHA256 = "b6f3afc94b7b0eb1c2518f356620cb8b2d6917201214fdecf8452be010818ace"
VERSION_PATH = ROOT / "backend" / "cmd" / "server" / "VERSION"
EXCLUDED_PATHS = {
    V1_MANIFEST_PATH.relative_to(ROOT).as_posix(),
    V1_RECEIPT_PATH.relative_to(ROOT).as_posix(),
    MANIFEST_PATH.relative_to(ROOT).as_posix(),
    RECEIPT_PATH.relative_to(ROOT).as_posix(),
    VERSION_PATH.relative_to(ROOT).as_posix(),
}
PRESERVED_UNRELATED_PREFIXES = ("outputs/",)

# 本轮只抽取 Codex 已证明成熟的共享控制骨架。路径闭集既防止漏记，也防止把
# Claude 遗留实现、画像事实或无关工作区变化夹带进本变更集。
EXPECTED_TRANSITION_PATHS = {
    "Makefile",
    "backend/internal/officialegress/bundle.go",
    "backend/internal/officialegress/changeset3_post_identity_authority_final_wire_frozen_test.go",
    "backend/internal/officialegress/compiler.go",
    "backend/internal/officialegress/dialect_registry.go",
    "backend/internal/officialegress/executor.go",
    "backend/internal/officialegress/executor_invocation_test.go",
    "backend/internal/officialegress/executor_one_shot_retirement_test.go",
    "backend/internal/officialegress/guard.go",
    "backend/internal/officialegress/migration_receipts.go",
    "backend/internal/officialegress/officialegress_test.go",
    "backend/internal/officialegress/persona_control_test.go",
    "backend/internal/officialegress/persona_registry.go",
    "backend/internal/officialegress/persona_release_catalog.go",
    "backend/internal/officialegress/release_catalog.go",
    "backend/internal/officialegress/route_catalog.go",
    "backend/internal/officialegress/types.go",
    "backend/internal/officialegress/upstream_v0177_source_transition_test.go",
    "backend/internal/officialegress/websocket_session.go",
    "backend/internal/service/compatibility_code_retirement_closure_test.go",
    "backend/internal/service/official_egress_activation_fact.go",
    "backend/internal/service/upstream_v0177_source_transition_test.go",
    "docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md",
    "docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md",
    "docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md",
    "docs/egress/maintenance/multi-persona-control-source-transition.json",
    "docs/egress/maintenance/multi-persona-control-source-transition-v2.json",
    "docs/egress/maintenance/multi-persona-control-test-transition.json",
    "docs/egress/maintenance/multi-persona-control-test-transition-v2.json",
    "tools/maintenance_workspace_transition.py",
    "tools/multi_persona_control_workspace_transition.py",
}

PROOF_COMMANDS = (
    {
        "id": "full-official-egress-and-service",
        "category": "regression",
        "cwd": "backend",
        "argv": [
            "go", "test", "-count=1",
            "./internal/officialegress/...", "./internal/service/...",
        ],
    },
    {
        "id": "codex-final-wire",
        "category": "codex_zero_diff",
        "cwd": "backend",
        "argv": [
            "go", "test", "-count=1", "./internal/officialegress",
            "-run", "^TestChangeset3PostIdentityAuthorityFinalWireIsFrozen$",
        ],
    },
    {
        "id": "shared-contract-boundary",
        "category": "contract",
        "cwd": "backend",
        "argv": [
            "go", "test", "-count=1", "./internal/officialegress",
            "-run", "^(TestProvisionalSharedContractsExcludePersonaPolicyFields|TestSharedExecutorControlRejectsCrossLayerFactMismatch|TestPersonaReleaseCatalogMatchesCodexAndKeepsRollbackPair)$",
        ],
    },
)


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def run_git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT)


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
        raise RuntimeError(f"多 Persona transition 禁止符号链接：{relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(
            f"多 Persona transition 路径必须是普通文件或明确缺失：{relative_path}"
        )
    raw = absolute.read_bytes()
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "size": len(raw),
        "sha256": sha256(raw),
    }


def commit_state(commit: str, relative_path: str) -> dict[str, Any]:
    raw = run_git("ls-tree", "-z", commit, "--", relative_path)
    if not raw:
        return empty_state()
    records = [record for record in raw.split(b"\0") if record]
    if len(records) != 1:
        raise RuntimeError(f"提交态路径解析结果不唯一：{relative_path}")
    metadata, actual_path = records[0].split(b"\t", 1)
    if actual_path.decode("utf-8", errors="strict") != relative_path:
        raise RuntimeError(f"提交态路径解析漂移：{relative_path}")
    mode, object_type, object_id = metadata.decode("ascii").split(" ")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise RuntimeError(f"提交态路径不是受支持的普通文件：{relative_path}")
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
            raise RuntimeError(f"无法解析 git status 记录：{text!r}")
        status_code, path = text[:2], text[3:]
        paths.add(path)
        if "R" in status_code or "C" in status_code:
            if index >= len(fields) or not fields[index]:
                raise RuntimeError(f"重命名记录缺少历史路径：{text!r}")
            paths.add(fields[index].decode("utf-8", errors="strict"))
            index += 1
    return paths


def task_status_paths() -> set[str]:
    """排除用户工作区中与本变更无关、且不会进入提交的保留路径。"""

    return {
        path
        for path in status_paths()
        if not any(path.startswith(prefix) for prefix in PRESERVED_UNRELATED_PREFIXES)
    }


def committed_paths() -> set[str]:
    raw = run_git("diff", "--name-only", "-z", f"{BASE_COMMIT}..HEAD")
    return {
        value.decode("utf-8", errors="strict")
        for value in raw.split(b"\0")
        if value
    }


def scope_of(path: str) -> str:
    if path.startswith("backend/internal/officialegress/"):
        return "officialegress_control"
    if path.startswith("backend/internal/service/"):
        return "service_boundary"
    if path.startswith("docs/egress/maintenance/"):
        return "receipts"
    if path.startswith("docs/"):
        return "documentation"
    if path.startswith("tools/") or path == "Makefile":
        return "source_gate"
    return "unexpected"


def validate_state(value: Any, path: str) -> None:
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
        raise RuntimeError(f"多 Persona transition 状态非法：{path}")


def build_transition(
    proofs: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, "HEAD"],
        cwd=ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("多 Persona transition 基准提交不是当前 HEAD 的祖先")
    candidates = (committed_paths() | task_status_paths()) - EXCLUDED_PATHS
    entries: list[dict[str, Any]] = []
    for path in sorted(candidates):
        before = commit_state(BASE_COMMIT, path)
        after = current_state(path)
        if before == after:
            continue
        entries.append(
            {
                "path": path,
                "scope": scope_of(path),
                "before": before,
                "after": after,
                "deletion_allowed": False,
                "reason": "从成熟 Codex 执行链抽取最小多 Persona 共享控制层并证明 Codex wire 零差异",
                "machine_proofs": [
                    "make check-egress-spec",
                    "go test ./internal/officialegress/... ./internal/service/...",
                    "docs/egress/maintenance/multi-persona-control-source-transition.json",
                    "docs/egress/maintenance/multi-persona-control-test-transition.json",
                ],
            }
        )
    paths = [entry["path"] for entry in entries]
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    manifest = {
        "schema_version": "official-egress-multi-persona-control-workspace-transition/v2",
        "prior_transition": V1_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "prior_transition_sha256": V1_FROZEN_SHA256[V1_MANIFEST_PATH],
        "base_commit": BASE_COMMIT,
        "candidate_path_count": len(candidates),
        "transition_entry_count": len(entries),
        "transition_path_set_sha256": sha256(path_set_raw),
        "entries": entries,
        "rules": [
            "基准提交后的已提交路径与当前完整 git status 路径取并集",
            "本轮路径必须与 FW-A/FW-B/FW-C Codex-only 后继变更批准闭集完全相等",
            "before 固定来自基准提交，after 来自当前普通文件或明确缺失",
            "不允许删除、符号链接、Claude 遗留实现或画像事实进入本变更集",
            "v1/v2 manifest 与 receipt 因自引用循环排除，VERSION 因发版流水线自动回写排除",
            "outputs/ 是用户保留的无关未跟踪产物，不进入本变更集或提交",
        ],
    }
    manifest_raw = canonical_json(manifest)
    scope_counts: dict[str, int] = {}
    for entry in entries:
        scope_counts[entry["scope"]] = scope_counts.get(entry["scope"], 0) + 1
    proofs = [] if proofs is None else proofs
    proof_failures = sum(proof.get("exit_code") != 0 for proof in proofs)
    zero_diff_failures = sum(
        proof.get("category") == "codex_zero_diff" and proof.get("exit_code") != 0
        for proof in proofs
    )
    receipt = {
        "schema_version": "official-egress-multi-persona-control-workspace-transition-receipt/v2",
        "manifest_path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256(manifest_raw),
        "prior_receipt_path": V1_RECEIPT_PATH.relative_to(ROOT).as_posix(),
        "prior_receipt_sha256": V1_FROZEN_SHA256[V1_RECEIPT_PATH],
        "base_commit": BASE_COMMIT,
        "transition_entry_count": len(entries),
        "added_entry_count": sum(
            entry["before"]["file_type"] == "absent" for entry in entries
        ),
        "deleted_entry_count": sum(
            entry["after"]["file_type"] == "absent" for entry in entries
        ),
        "scope_counts": dict(sorted(scope_counts.items())),
        "proofs": proofs,
        "proof_failure_count": proof_failures,
        "codex_zero_diff_assertion_failure_count": zero_diff_failures,
        "claude_runtime_path_count": sum(
            path.startswith("backend/") and "claude" in path.lower() for path in paths
        ),
        "result": "passed" if proofs and proof_failures == 0 else "failed",
    }
    return manifest, receipt


def validate_path_closure(manifest: dict[str, Any]) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("多 Persona transition entries 非法")
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("多 Persona transition 路径未严格排序或存在重复")
    if set(paths) != EXPECTED_TRANSITION_PATHS:
        missing = sorted(EXPECTED_TRANSITION_PATHS - set(paths))
        unexpected = sorted(set(paths) - EXPECTED_TRANSITION_PATHS)
        raise RuntimeError(
            f"多 Persona transition 路径闭集不一致：missing={missing} unexpected={unexpected}"
        )
    for entry in entries:
        path = entry["path"]
        validate_state(entry.get("before"), path)
        validate_state(entry.get("after"), path)
        if entry.get("deletion_allowed") or entry["after"]["file_type"] == "absent":
            raise RuntimeError(f"多 Persona transition 禁止删除：{path}")
        if entry.get("scope") == "unexpected" or not entry.get("machine_proofs"):
            raise RuntimeError(f"多 Persona transition 缺少受审 scope 或机器证明：{path}")
        if path.startswith("backend/") and "claude" in path.lower():
            raise RuntimeError(f"多 Persona transition 夹带 Claude 运行时路径：{path}")


def validate_frozen_v1() -> None:
    """复核首轮 transition 原文，后继收窄只能通过 v2 追加承接。"""

    for path, expected in V1_FROZEN_SHA256.items():
        raw = path.read_bytes()
        if sha256(raw) != expected:
            raise RuntimeError(
                f"多 Persona v1 transition 历史原文漂移：{path.relative_to(ROOT)}"
            )

    source = json.loads(V1_SOURCE_TRANSITION_PATH.read_bytes())
    test = json.loads(V1_TEST_TRANSITION_PATH.read_bytes())
    manifest = json.loads(V1_MANIFEST_PATH.read_bytes())
    receipt = json.loads(V1_RECEIPT_PATH.read_bytes())
    if (
        source.get("schema_version")
        != "official-egress-multi-persona-control-source-transition/v1"
        or source.get("result") != "passed"
        or test.get("schema_version")
        != "official-egress-multi-persona-control-test-transition/v1"
        or test.get("prior_transition")
        != V1_SOURCE_TRANSITION_PATH.relative_to(ROOT).as_posix()
        or test.get("prior_transition_sha256")
        != V1_FROZEN_SHA256[V1_SOURCE_TRANSITION_PATH]
        or test.get("result") != "passed"
        or manifest.get("schema_version")
        != "official-egress-multi-persona-control-workspace-transition/v1"
        or receipt.get("schema_version")
        != "official-egress-multi-persona-control-workspace-transition-receipt/v1"
        or receipt.get("manifest_sha256") != V1_FROZEN_SHA256[V1_MANIFEST_PATH]
        or receipt.get("transition_entry_count") != len(manifest.get("entries", []))
        or receipt.get("result") != "passed"
    ):
        raise RuntimeError("多 Persona v1 transition 历史摘要链非法")


def run_proofs() -> list[dict[str, Any]]:
    """执行 FW-B/C 本地机器证明，并只冻结可复算的命令与输出摘要。"""

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
            f"机器证明 {command['id']} 完成：exit_code={result.returncode} "
            f"stdout_sha256={proof['stdout_sha256']}"
        )
    return proofs


def validate_proofs(proofs: Any) -> None:
    if not isinstance(proofs, list) or len(proofs) != len(PROOF_COMMANDS):
        raise RuntimeError("多 Persona transition 机器证明数量非法")
    for proof, expected in zip(proofs, PROOF_COMMANDS, strict=True):
        if (
            not isinstance(proof, dict)
            or proof.get("id") != expected["id"]
            or proof.get("category") != expected["category"]
            or proof.get("cwd") != expected["cwd"]
            or proof.get("argv") != expected["argv"]
            or proof.get("exit_code") != 0
        ):
            raise RuntimeError(f"多 Persona transition 机器证明非法：{proof}")
        for prefix in ("stdout", "stderr"):
            byte_count = proof.get(f"{prefix}_bytes")
            digest = proof.get(f"{prefix}_sha256")
            if (
                not isinstance(byte_count, int)
                or byte_count < 0
                or not isinstance(digest, str)
                or len(digest) != 64
            ):
                raise RuntimeError(
                    f"多 Persona transition 机器证明输出摘要非法：{proof.get('id')}"
                )


def write_transition() -> None:
    validate_frozen_v1()
    proofs = run_proofs()
    manifest, receipt = build_transition(proofs)
    validate_path_closure(manifest)
    if receipt["proof_failure_count"] != 0:
        raise RuntimeError("机器证明失败，禁止写入 passed transition")
    TRANSITION_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_bytes(canonical_json(manifest))
    RECEIPT_PATH.write_bytes(canonical_json(receipt))


def validate_transition() -> None:
    validate_frozen_v1()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_raw)
    receipt = json.loads(RECEIPT_PATH.read_bytes())
    proofs = receipt.get("proofs")
    validate_proofs(proofs)
    expected_manifest, expected_receipt = build_transition(proofs)
    validate_path_closure(manifest)
    if manifest != expected_manifest or receipt != expected_receipt:
        raise RuntimeError("多 Persona transition 与基准提交及当前状态的复算结果不一致")
    if (
        receipt.get("manifest_sha256") != sha256(manifest_raw)
        or receipt.get("proof_failure_count") != 0
        or receipt.get("codex_zero_diff_assertion_failure_count") != 0
        or receipt.get("claude_runtime_path_count") != 0
        or receipt.get("deleted_entry_count") != 0
        or receipt.get("result") != "passed"
        or "codex_wire_delta_count" in receipt
        or "claude_path_count" in receipt
    ):
        raise RuntimeError("多 Persona transition receipt 终态事实非法")
    print(
        "多 Persona 控制层工作区 transition 有效："
        f"{len(manifest['entries'])} 项，manifest SHA-256={sha256(manifest_raw)}"
    )


def validate_frozen_transition() -> None:
    """只复核已接受 v1/v2 原文，不把 FW-D 后继变更吸收到旧收据。"""

    validate_frozen_v1()
    manifest_raw = MANIFEST_PATH.read_bytes()
    receipt_raw = RECEIPT_PATH.read_bytes()
    if sha256(manifest_raw) != V2_FROZEN_MANIFEST_SHA256:
        raise RuntimeError("多 Persona v2 workspace transition 历史原文漂移")
    if sha256(receipt_raw) != V2_FROZEN_RECEIPT_SHA256:
        raise RuntimeError("多 Persona v2 workspace transition receipt 历史原文漂移")
    manifest = json.loads(manifest_raw)
    receipt = json.loads(receipt_raw)
    if (
        manifest.get("schema_version")
        != "official-egress-multi-persona-control-workspace-transition/v2"
        or manifest.get("prior_transition_sha256")
        != V1_FROZEN_SHA256[V1_MANIFEST_PATH]
        or receipt.get("schema_version")
        != "official-egress-multi-persona-control-workspace-transition-receipt/v2"
        or receipt.get("manifest_sha256") != V2_FROZEN_MANIFEST_SHA256
        or receipt.get("prior_receipt_sha256")
        != V1_FROZEN_SHA256[V1_RECEIPT_PATH]
        or receipt.get("transition_entry_count") != len(manifest.get("entries", []))
        or receipt.get("result") != "passed"
    ):
        raise RuntimeError("多 Persona v2 transition 历史摘要链非法")
    print("多 Persona 控制层 v1/v2 transition 历史原文与摘要链有效")


def self_test() -> None:
    present = {
        "existence": "present",
        "file_type": "regular",
        "mode": "0644",
        "size": 3,
        "sha256": "a" * 64,
    }
    validate_state(present, "sample.go")
    validate_state(empty_state(), "sample.go")
    for mutation in (
        {**present, "file_type": "symlink"},
        {**present, "mode": "0777"},
        {**present, "sha256": "a" * 63},
        {**empty_state(), "existence": "present"},
    ):
        try:
            validate_state(mutation, "mutation.go")
        except RuntimeError:
            continue
        raise RuntimeError(f"多 Persona transition mutation 未被拒绝：{mutation}")
    print("多 Persona 控制层工作区 transition 判据 mutation 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-transition", action="store_true", help="确定性生成 transition")
    parser.add_argument("--self-test", action="store_true", help="运行判据 mutation 自测")
    parser.add_argument(
        "--frozen-only",
        action="store_true",
        help="只验证已接受 v1/v2 历史 transition，不吸收 FW-D 后继变更",
    )
    args = parser.parse_args()
    if args.write_transition:
        if args.frozen_only:
            raise RuntimeError("--write-transition 不能与 --frozen-only 同时使用")
        write_transition()
    if args.self_test:
        self_test()
        return 0
    if args.frozen_only:
        validate_frozen_transition()
        return 0
    validate_transition()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
