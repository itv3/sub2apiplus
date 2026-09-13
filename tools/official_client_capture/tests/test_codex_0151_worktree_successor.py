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
# 2026-09-10：模型能力在请求内钉住（openai_gateway_forward.go / openai_model_capabilities.go）
# 及随之同步的受管工具身份三件套，由 freeze-successor-generate 以 commit 模式生成。
CAPABILITY_PIN_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-capability-pin-freeze-successor.json"
)
# 2026-09-10：发版机器人 VERSION 同步提交（0.2.3-3）对 backend/cmd/server/VERSION
# 产生的后继摘要，由 freeze-successor-generate 以 commit 模式生成。
VERSION_SYNC_0233_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/version-sync-0.2.3-3-freeze-successor.json"
)
# 2026-09-10：上游 v0.2.4 合并（MiniMax 接入、长流 HTTP/2 保活、客户端断开取消）
# 对已冻结路径产生的后继摘要，由 freeze-successor-generate 以 commit 模式一次性生成。
UPSTREAM_V024_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.4-freeze-successor.json"
)
# 2026-09-11：发版 VERSION 同步（0.2.4-1）与 §5.2 精简改写的后继摘要。
DOC_TRIM_20260911_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-doc-trim-20260911-freeze-successor.json"
)
# 2026-09-11：upstream_merge 工具改进（建议端补齐承接分类、拒绝时给出已知答案）的后继摘要。
TOOL_HINTS_20260911_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-tool-hints-20260911-freeze-successor.json"
)
# 2026-09-11：Codex 升级工具改造批次 1（候选层运行坐标覆盖、campaign-run 强制派发泛化、
# 监督器派发超时竞争修复、codex-p0-rehearsal 目标）对已冻结路径产生的后继摘要，
# 由 freeze-successor-generate 以 commit 模式生成。
CODEX_UPGRADE_BATCH1_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-codex-upgrade-batch1-freeze-successor.json"
)
# 2026-09-11：批次 2（reuse-official-evidence 正式命令，把已封存官方阶段只读导入新 Campaign）
# 对已冻结路径产生的后继摘要，由 freeze-successor-generate 以 commit 模式生成。
CODEX_UPGRADE_BATCH2_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-codex-upgrade-batch2-freeze-successor.json"
)
# 2026-09-11：批次 3（§5.7 完成定义门禁化：check_ledger_completeness 通用校验终态收据 +
# officialegress/service 通用终态测试）对已冻结路径产生的后继摘要，同样以 commit 模式生成。
CODEX_UPGRADE_BATCH3_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-codex-upgrade-batch3-freeze-successor.json"
)
# 2026-09-11：v0.2.4-2 发版后 github-actions bot 的 VERSION 同步提交对 VERSION 产生的后继摘要，
# 由 freeze-successor-generate 以 commit 模式生成。
V0242_VERSION_SYNC_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.4-2-version-sync-freeze-successor.json"
)
# 2026-09-11：capture-cli 宿主目录、输出边界、迁移兼容与污染清理规范的后继摘要。
CAPTURE_CLI_DIRECTORY_GOVERNANCE_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-capture-cli-directory-governance-20260911-freeze-successor.json"
)
# 2026-09-11：Codex CLI 0.154.0 画像工具、目标清单和 Astra Lite 轨政策的后继摘要。
CODEX_0154_EMULATION_UPGRADE_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-emulation-upgrade-20260911-freeze-successor.json"
)
# 2026-09-12：修正 0.154 画像补丁对活动画像文件摘要的绑定，并同步受管工具摘要。
CODEX_0154_PROFILE_PATCH_BINDING_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-profile-patch-binding-20260912-freeze-successor.json"
)
# 2026-09-12：fresh Campaign canonical 初始化、动态旧 Previous 退休和对应框架语义的后继摘要。
CODEX_0154_CANONICAL_PRODUCTION_CHAIN_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-canonical-production-chain-20260912-freeze-successor.json"
)
# 2026-09-12：ARM64 受管工具把计时账本直接读取的 maintenance 文档纳入
# 同一可回滚部署事务，并覆盖首次新增文档的回滚语义。
CODEX_0154_RUNTIME_DOC_CLOSURE_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-runtime-doc-closure-20260912-freeze-successor.json"
)
# 2026-09-12：运行时文档坐标改为监督器可接受的 metadata 数组值，避免路径
# 中的斜杠被误用为事件对象键。
CODEX_0154_RUNTIME_DOC_METADATA_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-runtime-doc-metadata-20260912-freeze-successor.json"
)
# 2026-09-12：文档候选准备步骤的摘要也改为监督器可接受的路径值数组，
# 并由真实函数返回值回归覆盖。
CODEX_0154_RUNTIME_DOC_CANDIDATE_METADATA_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-runtime-doc-candidate-metadata-20260912-freeze-successor.json"
)
# 2026-09-12：Framework 与两份客户端指南建立 VC-0～VC-6 唯一执行入口，
# 同步澄清账号、模型可见性和运行坐标的身份边界。
CLIENT_VC_STAGE_NAVIGATION_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/client-vc-stage-navigation-20260912-freeze-successor.json"
)
# 2026-09-12：Claude VC-1～VC-3 合同拆分为证据包、规则迁移账本与原子断言账本，
# 同步冻结指南说明及本显式 successor 列表。
CLAUDE_VC1_VC3_CONTRACT_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-claude-vc1-vc3-contract-20260912-freeze-successor.json"
)
# 2026-09-13：Codex 第四部分与 VC-0～VC-6 工具、测试及部署摘要最终对齐。
CODEX_VC_FINAL_ALIGNMENT_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-vc-final-alignment-20260913-freeze-successor.json"
)
# 2026-09-13：v0.2.4-3 发版提交更新 VERSION 后产生的后继摘要，
# 由 freeze-successor-generate 以 commit 模式生成。
V0243_VERSION_SYNC_FREEZE_SUCCESSOR = (
    ROOT / "docs/egress/maintenance/upstream-v0.2.4-3-version-sync-freeze-successor.json"
)
# 2026-09-13：Codex VC-0 固定公网出口由 DMIT 切换到 BWG，并把环境 producer
# 升级为 v4；历史 v3 DMIT 收据只读重放。由 freeze-successor-generate 以
# commit 模式生成对应后继摘要。
CODEX_0154_BWG_EGRESS_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-bwg-egress-20260913-freeze-successor.json"
)
# 2026-09-13：VC-1 首批暴露父租约缺少 deadline 与失败根因不可见；工具修复
# 提交后，以独立 freeze successor 承接受管工具和本显式列表的新摘要。
CODEX_0154_VC1_PARENT_LEASE_REPAIR_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-vc1-parent-lease-repair-20260913-freeze-successor.json"
)
# 2026-09-13：P0 补齐四个精确可写子挂载的真实跨别名探针，并约束
# 冻结 Job 的证据根；由 freeze-successor-generate 以 commit 模式登记后继摘要。
CODEX_0154_WRITABLE_RUN_ROOT_REPAIR_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-writable-run-root-repair-20260913-freeze-successor.json"
)
# 2026-09-13：失败 Job 的容器证据别名在宿主归档前映射到登记 runs 子树，
# P0 同时实测创建、宿主归档、双别名读取与清理闭环。
CODEX_0154_FAILED_EVIDENCE_ARCHIVE_ROUTE_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-failed-evidence-archive-route-20260913-freeze-successor.json"
)
# 2026-09-13：VC-0 原子收口、Formal plan 直接调用拒绝和真实三轮
# 失败生命周期演练的后继摘要。
CODEX_0154_VC0_ATOMIC_CLOSEOUT_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-vc0-atomic-closeout-20260913-freeze-successor.json"
)
# 2026-09-13：campaign-run 分批演练改由受管工具生成并独立重放，禁止继续
# 手工拼装 Formal 收口所需的 P0 证明。
CODEX_0154_CAMPAIGN_RUN_REHEARSAL_CLOSURE_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-campaign-run-rehearsal-closure-20260913-freeze-successor.json"
)
# 2026-09-13：Job 演练隐藏 worker 补齐任意工作目录下的绝对路径入口，
# 并保留父监督器只写 stdout 时的结构化失败诊断。
CODEX_0154_JOB_REHEARSAL_ENTRYPOINT_CLOSURE_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-job-rehearsal-entrypoint-closure-20260913-freeze-successor.json"
)
# 2026-09-13：Campaign 冻结规则与双份场景输入时保留原始 JSON 字节，
# 防止 preflight 格式化导致场景绑定的规则摘要漂移。
CODEX_0154_CAMPAIGN_INPUT_BYTE_FREEZE_SUCCESSOR = (
    ROOT
    / "docs/egress/maintenance/upstream-codex-0154-campaign-input-byte-freeze-20260913-freeze-successor.json"
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


def successor_edges(path: str) -> list[tuple[str, str]]:
    """读取显式登记的后继边，不扫描未登记收据。"""

    edges: list[tuple[str, str]] = []
    for receipt_path in (
        POST_BOOTSTRAP_SUCCESSOR,
        RELEASE_PREP_SUCCESSOR,
        VERSION_SYNC_SUCCESSOR,
        RECONNECT_REPAIR_TRANSITION,
        RECONNECT_REPAIR_GATE_SUCCESSOR,
        TOOL_IDENTITY_SYNC_SUCCESSOR,
        CAPABILITY_PIN_FREEZE_SUCCESSOR,
        VERSION_SYNC_0233_FREEZE_SUCCESSOR,
        UPSTREAM_V024_FREEZE_SUCCESSOR,
        DOC_TRIM_20260911_FREEZE_SUCCESSOR,
        TOOL_HINTS_20260911_FREEZE_SUCCESSOR,
        CODEX_UPGRADE_BATCH1_FREEZE_SUCCESSOR,
        CODEX_UPGRADE_BATCH2_FREEZE_SUCCESSOR,
        CODEX_UPGRADE_BATCH3_FREEZE_SUCCESSOR,
        V0242_VERSION_SYNC_FREEZE_SUCCESSOR,
        CAPTURE_CLI_DIRECTORY_GOVERNANCE_FREEZE_SUCCESSOR,
        CODEX_0154_EMULATION_UPGRADE_FREEZE_SUCCESSOR,
        CODEX_0154_PROFILE_PATCH_BINDING_FREEZE_SUCCESSOR,
        CODEX_0154_CANONICAL_PRODUCTION_CHAIN_FREEZE_SUCCESSOR,
        CODEX_0154_RUNTIME_DOC_CLOSURE_FREEZE_SUCCESSOR,
        CODEX_0154_RUNTIME_DOC_METADATA_FREEZE_SUCCESSOR,
        CODEX_0154_RUNTIME_DOC_CANDIDATE_METADATA_FREEZE_SUCCESSOR,
        CLIENT_VC_STAGE_NAVIGATION_FREEZE_SUCCESSOR,
        CLAUDE_VC1_VC3_CONTRACT_FREEZE_SUCCESSOR,
        CODEX_VC_FINAL_ALIGNMENT_FREEZE_SUCCESSOR,
        V0243_VERSION_SYNC_FREEZE_SUCCESSOR,
        CODEX_0154_BWG_EGRESS_FREEZE_SUCCESSOR,
        CODEX_0154_VC1_PARENT_LEASE_REPAIR_FREEZE_SUCCESSOR,
        CODEX_0154_WRITABLE_RUN_ROOT_REPAIR_FREEZE_SUCCESSOR,
        CODEX_0154_FAILED_EVIDENCE_ARCHIVE_ROUTE_FREEZE_SUCCESSOR,
        CODEX_0154_VC0_ATOMIC_CLOSEOUT_FREEZE_SUCCESSOR,
        CODEX_0154_CAMPAIGN_RUN_REHEARSAL_CLOSURE_FREEZE_SUCCESSOR,
        CODEX_0154_JOB_REHEARSAL_ENTRYPOINT_CLOSURE_FREEZE_SUCCESSOR,
        CODEX_0154_CAMPAIGN_INPUT_BYTE_FREEZE_SUCCESSOR,
    ):
        payload = json.loads(receipt_path.read_text(encoding="utf-8"))
        _validate_successor_receipt(payload)
        edges.extend(_successor_edges_from_payload(payload, path))
    return edges


def _validate_successor_receipt(payload: dict[str, Any]) -> None:
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
    if sha256(canonical) != identity:
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
