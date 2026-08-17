#!/usr/bin/env python3
"""生成或复算 FW-D 通用受管工具链的追加式工作区 transition。"""

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import stat
import subprocess
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
BASE_COMMIT = "73db9b1f02798cf96027356c84f118404ca984d6"
PRIOR_MANIFEST_PATH = (
    ROOT
    / "docs"
    / "egress"
    / "maintenance"
    / "multi-persona-control-workspace-transition-v2"
    / "manifest.json"
)
PRIOR_RECEIPT_PATH = PRIOR_MANIFEST_PATH.with_name("receipt.json")
PRIOR_MANIFEST_SHA256 = "1d37b7ef22da8e0284a8175cc4d159ee781cab7ed0cd37422e21af45259698be"
PRIOR_RECEIPT_SHA256 = "b6f3afc94b7b0eb1c2518f356620cb8b2d6917201214fdecf8452be010818ace"
TRANSITION_DIR = (
    ROOT / "docs" / "egress" / "maintenance" / "fw-d-control-workspace-transition"
)
MANIFEST_PATH = TRANSITION_DIR / "manifest.json"
RECEIPT_PATH = TRANSITION_DIR / "receipt.json"
VERSION_PATH = ROOT / "backend" / "cmd" / "server" / "VERSION"
EXCLUDED_PATHS = {
    MANIFEST_PATH.relative_to(ROOT).as_posix(),
    RECEIPT_PATH.relative_to(ROOT).as_posix(),
    VERSION_PATH.relative_to(ROOT).as_posix(),
}
PRESERVED_UNRELATED_PREFIXES = ("outputs/",)
ALLOWED_EXACT_PATHS = {
    "Makefile",
    "tools/fw_d_control_workspace_transition.py",
    "tools/multi_persona_control_workspace_transition.py",
}
ALLOWED_PREFIXES = ("tools/official_client_control/",)
REQUIRED_PATHS = {
    "tools/official_client_control/__init__.py",
    "tools/official_client_control/__main__.py",
    "tools/official_client_control/canonical.py",
    "tools/official_client_control/cli.py",
    "tools/official_client_control/contracts.py",
    "tools/official_client_control/errors.py",
    "tools/official_client_control/gates.py",
    "tools/official_client_control/receipts.py",
    "tools/official_client_control/store.py",
    "tools/official_client_control/schemas/bootstrap.schema.json",
    "tools/official_client_control/schemas/campaign.schema.json",
    "tools/official_client_control/schemas/envelope.schema.json",
    "tools/official_client_control/schemas/fact.schema.json",
    "tools/official_client_control/schemas/inventory.schema.json",
    "tools/official_client_control/schemas/object.schema.json",
    "tools/official_client_control/schemas/receipt.schema.json",
    "tools/official_client_control/schemas/scenario-pair.schema.json",
    "tools/official_client_control/schemas/validation-candidate.schema.json",
}

PROOF_COMMANDS = (
    {
        "id": "fw-d-control-tests",
        "category": "fw_d",
        "cwd": ".",
        "argv": ["make", "test-official-client-control"],
    },
    {
        "id": "fw-b-c-history",
        "category": "history",
        "cwd": ".",
        "argv": [
            "python3",
            "tools/multi_persona_control_workspace_transition.py",
            "--frozen-only",
        ],
    },
    {
        "id": "shared-runtime-unchanged",
        "category": "runtime_scope",
        "cwd": ".",
        "argv": [
            "git",
            "diff",
            "--exit-code",
            BASE_COMMIT,
            "--",
            "backend/internal/officialegress",
        ],
    },
    {
        "id": "codex-final-wire-and-contract",
        "category": "codex_zero_diff",
        "cwd": "backend",
        "argv": [
            "go",
            "test",
            "-count=1",
            "./internal/officialegress",
            "-run",
            "^(TestChangeset3PostIdentityAuthorityFinalWireIsFrozen|TestProvisionalSharedContractsExcludePersonaPolicyFields|TestPersonaReleaseCatalogMatchesCodexAndKeepsRollbackPair)$",
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
        raise RuntimeError(f"FW-D transition 禁止符号链接：{relative_path}")
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"FW-D transition 路径必须是普通文件：{relative_path}")
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
        raise RuntimeError(f"FW-D 提交态路径不唯一：{relative_path}")
    metadata, actual_path = records[0].split(b"\t", 1)
    if actual_path.decode("utf-8", errors="strict") != relative_path:
        raise RuntimeError(f"FW-D 提交态路径漂移：{relative_path}")
    mode, object_type, object_id = metadata.decode("ascii").split(" ")
    if object_type != "blob" or mode not in {"100644", "100755"}:
        raise RuntimeError(f"FW-D 提交态不是受支持普通文件：{relative_path}")
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
        status_code, path = text[:2], text[3:]
        paths.add(path)
        if "R" in status_code or "C" in status_code:
            if index >= len(fields) or not fields[index]:
                raise RuntimeError(f"重命名记录缺少历史路径：{text!r}")
            paths.add(fields[index].decode("utf-8", errors="strict"))
            index += 1
    return paths


def task_status_paths() -> set[str]:
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


def allowed_path(path: str) -> bool:
    return path in ALLOWED_EXACT_PATHS or any(path.startswith(prefix) for prefix in ALLOWED_PREFIXES)


def scope_of(path: str) -> str:
    if path == "Makefile":
        return "ci_gate"
    if path == "tools/multi_persona_control_workspace_transition.py":
        return "prior_transition_freeze"
    if path == "tools/fw_d_control_workspace_transition.py":
        return "transition_gate"
    if "/schemas/" in path:
        return "schema"
    if "/tests/" in path:
        return "test"
    if path.startswith("tools/official_client_control/"):
        return "control_tool"
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
        raise RuntimeError(f"FW-D transition 状态非法：{path}")


def validate_prior_transition() -> None:
    manifest_raw = PRIOR_MANIFEST_PATH.read_bytes()
    receipt_raw = PRIOR_RECEIPT_PATH.read_bytes()
    if sha256(manifest_raw) != PRIOR_MANIFEST_SHA256:
        raise RuntimeError("FW-B/FW-C 前序 manifest 原文漂移")
    if sha256(receipt_raw) != PRIOR_RECEIPT_SHA256:
        raise RuntimeError("FW-B/FW-C 前序 receipt 原文漂移")
    receipt = json.loads(receipt_raw)
    if (
        receipt.get("manifest_sha256") != PRIOR_MANIFEST_SHA256
        or receipt.get("result") != "passed"
    ):
        raise RuntimeError("FW-B/FW-C 前序摘要链非法")


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
            f"FW-D 机器证明 {command['id']}：exit_code={result.returncode} "
            f"stdout_sha256={proof['stdout_sha256']}"
        )
    return proofs


def validate_proofs(proofs: Any) -> None:
    if not isinstance(proofs, list) or len(proofs) != len(PROOF_COMMANDS):
        raise RuntimeError("FW-D transition 机器证明数量非法")
    for proof, expected in zip(proofs, PROOF_COMMANDS, strict=True):
        if (
            not isinstance(proof, dict)
            or proof.get("id") != expected["id"]
            or proof.get("category") != expected["category"]
            or proof.get("cwd") != expected["cwd"]
            or proof.get("argv") != expected["argv"]
            or proof.get("exit_code") != 0
        ):
            raise RuntimeError(f"FW-D transition 机器证明非法：{proof}")
        for prefix in ("stdout", "stderr"):
            if (
                not isinstance(proof.get(f"{prefix}_bytes"), int)
                or proof[f"{prefix}_bytes"] < 0
                or not isinstance(proof.get(f"{prefix}_sha256"), str)
                or len(proof[f"{prefix}_sha256"]) != 64
            ):
                raise RuntimeError(f"FW-D 机器证明输出摘要非法：{proof.get('id')}")


def build_transition(
    proofs: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    ancestor = subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE_COMMIT, "HEAD"],
        cwd=ROOT,
        check=False,
    )
    if ancestor.returncode != 0:
        raise RuntimeError("FW-D 基准提交不是当前 HEAD 的祖先")
    candidates = (committed_paths() | task_status_paths()) - EXCLUDED_PATHS
    unexpected = sorted(path for path in candidates if not allowed_path(path))
    if unexpected:
        raise RuntimeError(f"FW-D 变更集夹带未批准路径：{unexpected}")
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
                "reason": "建设 Persona 无关 FW-D 受管控制面，不改变 FW-B/FW-C 共享运行时",
                "machine_proofs": [command["id"] for command in PROOF_COMMANDS],
            }
        )
    paths = [entry["path"] for entry in entries]
    path_set_raw = ("\n".join(paths) + "\n").encode("utf-8")
    manifest = {
        "schema_version": "official-client-fw-d-workspace-transition/v1",
        "prior_manifest_path": PRIOR_MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "prior_manifest_sha256": PRIOR_MANIFEST_SHA256,
        "base_commit": BASE_COMMIT,
        "candidate_path_count": len(candidates),
        "transition_entry_count": len(entries),
        "transition_path_set_sha256": sha256(path_set_raw),
        "entries": entries,
        "rules": [
            "只允许 Makefile、FW-D 工具、Schema、测试及追加式 transition 门禁进入变更集",
            "backend/internal/officialegress 与生产 selector、画像和路由必须保持 FW-C 原样",
            "不允许删除、符号链接、凭据、真实新 Persona 画像或生产注册",
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
        "schema_version": "official-client-fw-d-workspace-transition-receipt/v1",
        "manifest_path": MANIFEST_PATH.relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256(manifest_raw),
        "prior_receipt_path": PRIOR_RECEIPT_PATH.relative_to(ROOT).as_posix(),
        "prior_receipt_sha256": PRIOR_RECEIPT_SHA256,
        "base_commit": BASE_COMMIT,
        "transition_entry_count": len(entries),
        "added_entry_count": sum(
            entry["before"]["file_type"] == "absent" for entry in entries
        ),
        "deleted_entry_count": sum(
            entry["after"]["file_type"] == "absent" for entry in entries
        ),
        "scope_counts": dict(sorted(scope_counts.items())),
        "shared_runtime_path_count": sum(
            path.startswith("backend/internal/officialegress/") for path in paths
        ),
        "new_persona_artifact_path_count": sum(
            any(token in path.lower() for token in ("claude", "anthropic", "grok", "gemini"))
            for path in paths
        ),
        "proofs": proofs,
        "proof_failure_count": proof_failures,
        "result": "passed" if proofs and proof_failures == 0 else "failed",
    }
    return manifest, receipt


def validate_path_closure(manifest: dict[str, Any]) -> None:
    entries = manifest.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("FW-D transition entries 非法")
    paths = [entry.get("path") for entry in entries]
    if paths != sorted(paths) or len(paths) != len(set(paths)):
        raise RuntimeError("FW-D transition 路径未严格排序或存在重复")
    missing = sorted(REQUIRED_PATHS - set(paths))
    unexpected = sorted(path for path in paths if not allowed_path(path))
    if missing or unexpected:
        raise RuntimeError(
            f"FW-D transition 路径闭集非法：missing={missing}, unexpected={unexpected}"
        )
    if not any("/tests/" in path for path in paths):
        raise RuntimeError("FW-D transition 缺少正负例测试")
    for entry in entries:
        path = entry["path"]
        validate_state(entry.get("before"), path)
        validate_state(entry.get("after"), path)
        if entry.get("deletion_allowed") or entry["after"]["file_type"] == "absent":
            raise RuntimeError(f"FW-D transition 禁止删除：{path}")
        if entry.get("scope") == "unexpected" or not entry.get("machine_proofs"):
            raise RuntimeError(f"FW-D transition 缺少 scope 或机器证明：{path}")


def write_transition() -> None:
    validate_prior_transition()
    proofs = run_proofs()
    validate_proofs(proofs)
    manifest, receipt = build_transition(proofs)
    validate_path_closure(manifest)
    if receipt["proof_failure_count"] != 0:
        raise RuntimeError("FW-D 机器证明失败，禁止写入 passed transition")
    TRANSITION_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST_PATH.write_bytes(canonical_json(manifest))
    RECEIPT_PATH.write_bytes(canonical_json(receipt))


def validate_transition() -> None:
    validate_prior_transition()
    manifest_raw = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_raw)
    receipt = json.loads(RECEIPT_PATH.read_bytes())
    validate_proofs(receipt.get("proofs"))
    expected_manifest, expected_receipt = build_transition(receipt["proofs"])
    validate_path_closure(manifest)
    if manifest != expected_manifest or receipt != expected_receipt:
        raise RuntimeError("FW-D transition 与基准提交及当前状态复算结果不一致")
    if (
        receipt.get("manifest_sha256") != sha256(manifest_raw)
        or receipt.get("deleted_entry_count") != 0
        or receipt.get("shared_runtime_path_count") != 0
        or receipt.get("new_persona_artifact_path_count") != 0
        or receipt.get("proof_failure_count") != 0
        or receipt.get("result") != "passed"
    ):
        raise RuntimeError("FW-D transition receipt 终态事实非法")
    print(
        "FW-D 通用受管工具链 transition 有效："
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
        raise RuntimeError(f"FW-D transition mutation 未被拒绝：{mutation}")
    if not allowed_path("tools/official_client_control/store.py"):
        raise RuntimeError("FW-D scope 判据拒绝了受管工具路径")
    if allowed_path("backend/internal/officialegress/executor.go"):
        raise RuntimeError("FW-D scope 判据错误放行共享运行时")
    print("FW-D transition 判据 mutation 与 scope 自测通过")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-transition", action="store_true", help="确定性生成 FW-D transition")
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
