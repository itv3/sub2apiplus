"""校验当前 Codex CLI 0.151 工作区 successor，不读取或改写历史收据。"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import subprocess
import unittest
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
SUCCESSOR = ROOT / "docs/egress/maintenance/codex-cli-0151-worktree-successor.json"
POST_BOOTSTRAP_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.3-post-bootstrap-source-successor.json"
)
RELEASE_PREP_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.3-release-prep-source-successor.json"
)
VERSION_SYNC_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.3-version-sync-successor.json"
)
RECONNECT_REPAIR_TRANSITION = (
    ROOT / "docs/egress/maintenance/openai-ws-reconnect-repair-source-transition.json"
)
RECONNECT_REPAIR_GATE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.3-reconnect-repair-gate-successor.json"
)
TOOL_IDENTITY_SYNC_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.3-tool-identity-sync-successor.json"
)
# B1（2026-09-16）：freeze-successor-generate 产生的通用 successor 收据不再逐份显式登记，
# 而是按 schema 从 maintenance 目录 glob；新增收据不必再改本文件，也不再需要用
# --extra-worktree-path 把本文件自身登记进收据。非 freeze 形态的六份历史收据仍显式列出。
MAINTENANCE_ROOT = ROOT / "docs/egress/maintenance"
FREEZE_SUCCESSOR_SCHEMA = "official-egress-upstream-freeze-successor/v1"
EXPLICIT_SUCCESSORS = (
    POST_BOOTSTRAP_SUCCESSOR,
    RELEASE_PREP_SUCCESSOR,
    VERSION_SYNC_SUCCESSOR,
    RECONNECT_REPAIR_TRANSITION,
    RECONNECT_REPAIR_GATE_SUCCESSOR,
    TOOL_IDENTITY_SYNC_SUCCESSOR,
)
# 2026-09-10 早期由旧算法签名、从未进入本门禁显式列表的五份 freeze successor：
# 它们的 identity_sha256 既不是紧凑形态也不是缩进形态，历史收据只读不得改写，
# 因此只豁免自摘要核对；基准提交关系与摘要边形态仍按同一规则校验。
LEGACY_IDENTITY_FREEZE_SUCCESSORS = frozenset(
    {
        "upstream-doc-sync-20260910-freeze-successor.json",
        "upstream-flaky-test-repair-freeze-successor.json",
        "upstream-tooling-batch1-freeze-successor.json",
        "upstream-tooling-batch2-freeze-successor.json",
        "upstream-tooling-batch4-freeze-successor.json",
    }
)
HISTORICAL_LEDGER = "docs/egress/maintenance/historical-source-drift-successor.json"
SHA256_LENGTH = 64
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def git(*arguments: str) -> bytes:
    return subprocess.check_output(
        ["git", *arguments], cwd=ROOT, stderr=subprocess.DEVNULL
    )


def git_is_ancestor(ancestor: str, descendant: str) -> bool:
    """确认 successor 基准提交仍是当前工作区提交的祖先。"""

    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def current_state(path: str) -> dict[str, Any]:
    absolute = ROOT / Path(path)
    metadata = absolute.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AssertionError(f"当前 successor 路径不是普通文件：{path}")
    raw = absolute.read_bytes()
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "size": len(raw),
        "sha256": sha256(raw),
    }


def base_state(commit: str, path: str) -> dict[str, Any]:
    try:
        raw = git("show", f"{commit}:{path}")
    except subprocess.CalledProcessError:
        return {
            "existence": "absent",
            "file_type": "absent",
            "mode": "",
            "size": 0,
            "sha256": "",
        }
    tree = git("ls-tree", "-z", commit, "--", path).split(b"\0", 1)[0]
    mode = tree.split(b" ", 1)[0].decode("ascii")
    return {
        "existence": "present",
        "file_type": "regular",
        "mode": "0755" if mode == "100755" else "0644",
        "size": len(raw),
        "sha256": sha256(raw),
    }


def freeze_successor_receipts() -> list[Path]:
    """按 schema 从 maintenance 目录收集全部 freeze successor 收据（确定性排序）。"""

    found: list[Path] = []
    for candidate in sorted(MAINTENANCE_ROOT.glob("*.json")):
        if candidate.is_symlink() or not candidate.is_file():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("schema_version") == FREEZE_SUCCESSOR_SCHEMA:
            found.append(candidate)
    return found


_RECEIPT_CACHE: dict[str, list[tuple[Path, dict[str, Any]]]] = {}


def _validated_receipts() -> list[tuple[Path, dict[str, Any]]]:
    """读取并校验显式收据与 glob 到的 freeze successor；同一进程内只校验一次。"""

    cached = _RECEIPT_CACHE.get("receipts")
    if cached is not None:
        return cached
    loaded: list[tuple[Path, dict[str, Any]]] = []
    for receipt_path in (*EXPLICIT_SUCCESSORS, *freeze_successor_receipts()):
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        _validate_successor_receipt(
            payload,
            skip_identity=receipt_path.name in LEGACY_IDENTITY_FREEZE_SUCCESSORS,
        )
        loaded.append((receipt_path, payload))
    _RECEIPT_CACHE["receipts"] = loaded
    return loaded


def successor_edges(path: str) -> list[tuple[str, str]]:
    """读取显式登记与按 schema glob 的后继边，不接受未校验收据。"""

    edges: list[tuple[str, str]] = []
    for _receipt_path, payload in _validated_receipts():
        edges.extend(_successor_edges_from_payload(payload, path))
    return edges


def _validate_successor_receipt(
    payload: dict[str, Any], *, skip_identity: bool = False
) -> None:
    """校验后继收据身份，以及 commit／worktree 模式的基准连续性。"""

    identity = payload.get("identity_sha256")
    unsigned = dict(payload)
    unsigned.pop("identity_sha256", None)
    canonical = json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    if not isinstance(identity, str) or not SHA256_PATTERN.fullmatch(identity):
        raise AssertionError("后继收据 identity_sha256 非法")
    if not skip_identity and sha256(canonical) != identity:
        raise AssertionError("后继收据自摘要不一致")

    base_commit = payload.get("base_commit")
    current_commit = payload.get("current_commit")
    head_commit = git("rev-parse", "HEAD").decode().strip()
    if (
        not isinstance(base_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", base_commit)
        or not git_is_ancestor(base_commit, head_commit)
    ):
        raise AssertionError("后继收据基准提交关系非法")

    mode = payload.get("mode", "commit")
    if mode == "worktree":
        if current_commit is not None:
            raise AssertionError("worktree 后继收据不得声明 current_commit")
        return
    if (
        mode != "commit"
        or not isinstance(current_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", current_commit)
        or not git_is_ancestor(base_commit, current_commit)
        or not git_is_ancestor(current_commit, head_commit)
    ):
        raise AssertionError("commit 后继收据提交关系非法")


def _successor_edges_from_payload(
    payload: dict[str, Any], path: str
) -> list[tuple[str, str]]:
    """提取指定路径的显式 successor 摘要边。"""

    edges: list[tuple[str, str]] = []
    for transition in payload.get("transitions", []):
        if not isinstance(transition, dict) or transition.get("path") != path:
            continue
        predecessors = transition.get("predecessor_sha256s")
        successor = transition.get("to_sha256")
        if (
            not isinstance(predecessors, list)
            or not isinstance(successor, str)
            or not SHA256_PATTERN.fullmatch(successor)
            or any(
                not isinstance(predecessor, str)
                or not SHA256_PATTERN.fullmatch(predecessor)
                or predecessor == successor
                for predecessor in predecessors
            )
        ):
            raise AssertionError("后继收据摘要边非法")
        edges.extend((predecessor, successor) for predecessor in predecessors)
    return edges


def successor_reaches(path: str, predecessor: str, current: str) -> bool:
    """只沿显式登记的摘要边前进，保持未登记漂移 fail-close。"""

    if (
        not SHA256_PATTERN.fullmatch(predecessor)
        or not SHA256_PATTERN.fullmatch(current)
        or predecessor == current
    ):
        return False
    edges = successor_edges(path)
    queue = [predecessor]
    visited = {predecessor}
    while queue and len(visited) <= 512:
        node = queue.pop(0)
        for edge_from, edge_to in edges:
            if edge_from != node:
                continue
            if edge_to == current:
                return True
            if edge_to not in visited:
                visited.add(edge_to)
                queue.append(edge_to)
    return False


class Codex0151WorktreeSuccessorTest(unittest.TestCase):
    def test_current_worktree_successor_is_frozen(self) -> None:
        payload = json.loads(SUCCESSOR.read_text(encoding="utf-8"))
        self.assertEqual(
            payload["schema_version"],
            "sub2apiplus-codex-cli-0151-worktree-successor/v1",
        )
        self.assertEqual(payload["scope"], "codex-cli-0.151-current-worktree")
        self.assertFalse(payload["policy"]["historical_receipts_rewrite_allowed"])
        self.assertFalse(payload["policy"]["historical_source_drift_ledger_used"])
        self.assertFalse(payload["policy"]["arm64_deployment_allowed"])
        self.assertTrue(
            git_is_ancestor(
                payload["base_commit"],
                git("rev-parse", "HEAD").decode().strip(),
            )
        )

        identity = payload["identity_sha256"]
        self.assertIsInstance(identity, str)
        self.assertEqual(len(identity), SHA256_LENGTH)
        unsigned = dict(payload)
        unsigned.pop("identity_sha256")
        canonical = (json.dumps(unsigned, ensure_ascii=False, indent=2) + "\n").encode()
        self.assertEqual(sha256(canonical), identity)

        entries = payload["entries"]
        paths = [entry["path"] for entry in entries]
        self.assertEqual(paths, sorted(paths))
        self.assertEqual(len(paths), len(set(paths)))
        self.assertGreater(len(paths), 0)
        for entry in entries:
            path = entry["path"]
            self.assertFalse(path.startswith("/"))
            self.assertFalse(path.startswith("../"))
            self.assertNotEqual(path, HISTORICAL_LEDGER)
            self.assertNotEqual(entry["before"], entry["after"])
            self.assertEqual(entry["before"], base_state(payload["base_commit"], path))
            actual = current_state(path)
            if actual != entry["after"]:
                self.assertEqual(actual["mode"], entry["after"]["mode"])
                self.assertEqual(actual["file_type"], entry["after"]["file_type"])
                self.assertTrue(
                    successor_reaches(path, entry["after"]["sha256"], actual["sha256"]),
                    f"当前摘要未沿已登记 successor 边承接：{path}",
                )


if __name__ == "__main__":
    unittest.main()
