"""Claude 候选 revision 的编排器成员与入口（Claude 升级控制面设计 B3-05 编排器部分，随 B3-04 第三步落实）。

Claude 的候选 revision 有两份登记，必须一致：

* **编排器侧**（与 Codex 同形，共享流程 ``revision-open`` 写）：``control/vc/revisions/r<N>/revision.json`` 与 ``COMMIT``，
  计时账本的 ``stage_revision`` 事件（激活）；
* **Store 侧**（B3-05 Store 部分）：``candidate_revision_recorded`` 事实决定**当前**有效候选，写入门禁据此拒绝不属于当前
  revision 的候选级事实与收据。

一、服务面成员（``claude_upgrade.py`` 里的同名成员只转调）
=========================================================
* ``read_candidate_revision_record``：读 revision 目录（与 Codex 同一判据）；
* ``current_candidate_revision_record``：账本最后一条 ``stage_revision`` 决定当前 revision，目录 COMMIT 与事件一致，再核 Store
  的当前 revision 与候选与之相同（编排器先写、Store 后写：两者之间被强杀时这里拒绝，重跑 ``revision-open`` 收敛）。Claude
  没有改造前的历史 Campaign，不认隐含 r1：没有 ``stage_revision`` 即 ``(None, None)``；
* ``require_candidate_revision``：没有激活的 revision 即拒绝（先 ``revision-open --initial``）；
* ``implicit_r1_candidate_id``：一律拒绝（Claude 没有隐含 r1；共享流程只在拿不到 revision 记录时才问它，Claude 的上面两个
  成员保证拿得到）；
* ``candidate_identity_snapshot``：作废用的旧候选身份快照，构建收据读 Claude 的客户端中立合同，其余来源与 Codex 相同；
* ``_candidate_failed_run_for_review``：由 ``candidate_review_required`` 事件定位引起它的失败父 run，逐字照搬 Codex
  （Claude 的 COMMIT 目录与父 run 清单与 Codex 同一布局，测试锁定与 Codex 源码逐字一致）。

二、``revision-open``（``revision_open``）
=======================================
顺序是「Store 预检（零写入）→ 共享流程（revision.json＋COMMIT → 账本 stage_revision）→ Store ``candidate_revision_recorded``」：

* 预检先判定 Store 侧能否承接：``--initial`` 要求 Store 没有 revision 事实（或恰是同一候选的 r1：幂等重放）；
  ``--supersedes <旧>`` 要求 Store 当前 revision 的候选就是旧候选，并且 Store 已有该 revision 的作废对象（新事实必须以
  ``invalidation_ref`` 引用它）——作废对象缺失说明 ``invalidate-candidate apply`` 的 Store 侧没写完，先以同一批准摘要重跑它。
  预检不过即拒绝，编排器侧零写入，两份登记不会被预检挡出分叉。
* Store 事实的 ``issued_at`` 取 revision.json 的 ``opened_at_utc``，幂等键是 ``claude-upgrade:revision-open``＋payload：
  共享流程与 Store 之间被强杀时重跑同一命令，共享流程走幂等分支、Store 事实补写（已写则直接复用）。
* 写完再按 ``current_candidate_revision_record`` 复核两份登记一致。

三、``invalidate-candidate``（``invalidate_candidate``）
=====================================================
共享流程完成审核与编排器侧（preview 的诊断草案与 review_sha256；apply 的 invalidation.json、根因入账、二次判定与账本
``candidate_invalidated``）。apply 通过之后（``status == applied``，含幂等重放）由本模块写 Store 侧：

* 证据清单：一个 ``operational_evidence`` 通用清单（``claude-upgrade-candidate-invalidation-r<N>``），条目是编排器作废记录
  （路径、文件摘要、收据摘要、根因、诊断审核摘要）与诊断登记的每个证据文件绑定；
* 作废对象 ``candidate_invalidation``：``revision_ref`` 引用该 revision 的 Store revision 事实，``reason`` 写根因与作废记录
  摘要，批准人取诊断的审核人、批准时刻取作废记录的 ``recorded_at_utc``（只写一次的冻结值，重跑逐字相同）。Store 的两步
  批准由编排器以同一审核结论转写：审核人在共享 preview 审过的诊断草案已经确定性地决定了这份对象的全部内容；
* 预算暂停（``paused``）与永久停线（``permanent_stop``）不写 Store。apply 与 Store 之间被强杀时以同一批准摘要重跑
  apply：共享流程幂等，Store 侧补写（对象封存带 ``--allow-existing``）。

四、revision seal（``seal_candidate_revision``，Claude 自实现的 ``_seal_candidate_revision``）
=========================================================================================
``record-candidate-build`` 在 Store 冻结之后、构建收据写出之前写 ``control/vc/revisions/r<N>/seal.json``（显式 r1 也写），
合同与 Codex 相同（``codex-upgrade-candidate-revision-seal/v1``）：r≥2 以被取代候选 invalidation.json 冻结的身份快照为
基准，源码层与构建层全部相同即拒绝（不是新候选）。被取代候选写失效标记 ``candidates/<旧>/superseded-by.json``，合同名是
Claude 的 ``claude-upgrade-candidate-superseded/v1``（设计第 323 行：这是 ``_seal_candidate_revision`` 留原位自实现的唯一
差别）。``record-candidate-build`` 在任何写入之前先以 ``write=False`` 预检一次，全同的候选不会留下 Store 冻结事实。

本文件被 Codex 身份策略忽略（``claude_`` 前缀），在 Claude 策略里按 ``claude_upgrade_`` 前缀归 control 层。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.official_client_capture import claude_upgrade_seal
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_candidate_build
from tools.official_client_capture import codex_upgrade_supervisor
from tools.official_client_capture import upgrade_control_plane
from tools.official_client_capture import codex_upgrade_vc_artifacts as vc_artifacts
from tools.official_client_capture.capturelib.model import ConfigurationError
from tools.official_client_capture.capturelib.security import file_sha256
from tools.official_client_control import receipts as store_receipts
from tools.official_client_control.contracts import invalidation_review_sha256
from tools.official_client_control.errors import ControlError

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
# Store 写入的临时输入（用完即删）：Campaign 级控制面写入放 control/store-inputs，不放进 revisions/（那里只许 r<N> 目录）。
STORE_SCRATCH_RELATIVE = Path("control") / "store-inputs"
REVISION_OPEN_LOGICAL_ACTION = "claude-upgrade:revision-open"
INVALIDATION_EVIDENCE_MANIFEST_PREFIX = "claude-upgrade-candidate-invalidation"
OPERATIONAL_EVIDENCE_SCHEMA = "official-client-operational-evidence/v1"
REVISION_SEAL_FILENAME = "seal.json"
SUPERSEDED_MARKER_FILENAME = "superseded-by.json"
SUPERSEDED_MARKER_SCHEMA = "claude-upgrade-candidate-superseded/v1"
# 照搬 Codex ``_candidate_failed_run_for_review`` 用到的模块级名字（与 Codex 编排器同值）。
CANDIDATE_VC_PHASES = vc_artifacts.CANDIDATE_PHASES
_VC_SEQUENCE_FILE_RE = re.compile(r"^([0-9]{4})-(vc-[0-6])\.json$")


def _orchestrator() -> Any:
    from tools.official_client_capture import claude_upgrade

    return claude_upgrade


def read_candidate_revision_record(
    campaign_dir: Path,
    revision: int,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """读取 revision 目录的 revision.json 与 COMMIT（缺失返回 None，形态异常即拒绝；与 Codex 同一判据）。"""

    orchestrator = _orchestrator()
    directory = orchestrator._candidate_revision_dir(campaign_dir, revision)
    record_path = directory / "revision.json"
    commit_path = directory / "COMMIT"
    record: dict[str, Any] | None = None
    commit: dict[str, Any] | None = None
    if record_path.exists() or record_path.is_symlink():
        if record_path.is_symlink() or not record_path.is_file():
            raise ConfigurationError(f"候选 revision r{revision} 记录路径不可信。")
        try:
            record = vc_artifacts.validate_candidate_revision(orchestrator._read_json(record_path, f"候选 revision r{revision} 记录"))
        except vc_artifacts.VCArtifactError as error:
            raise ConfigurationError(str(error)) from error
        if record["revision"] != revision:
            raise ConfigurationError(f"候选 revision r{revision} 记录的 revision 与目录不一致。")
    if commit_path.exists() or commit_path.is_symlink():
        if commit_path.is_symlink() or not commit_path.is_file():
            raise ConfigurationError(f"候选 revision r{revision} COMMIT 路径不可信。")
        try:
            commit = vc_artifacts.validate_candidate_revision_commit(orchestrator._read_json(commit_path, f"候选 revision r{revision} COMMIT"))
        except vc_artifacts.VCArtifactError as error:
            raise ConfigurationError(str(error)) from error
        if record is None or commit["record_sha256"] != record["record_sha256"] or commit["revision"] != revision:
            raise ConfigurationError(f"候选 revision r{revision} COMMIT 未绑定同目录的 revision.json。")
    return record, commit


def _ledger_dir(campaign_dir: Path, manifest: Mapping[str, Any]) -> Path | None:
    controls = manifest.get("control_receipts")
    timing = controls.get("upgrade_timing") if isinstance(controls, Mapping) else None
    if not isinstance(timing, Mapping):
        return None
    return _orchestrator()._campaign_timing_ledger_dir(campaign_dir, manifest)


def store_current_revision(campaign_dir: Path, manifest: Mapping[str, Any]) -> tuple[int, str | None, bool]:
    """Store 的当前候选 revision：``(revision, 候选, 是否显式)``；没有 revision 事实时是隐含 r1（候选 None、显式 False）。"""

    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    campaign = dict(store_campaign)
    explicit = bool(store_receipts.candidate_revision_facts(store, campaign))
    revision, candidate = store_receipts.current_candidate_revision(store, campaign)
    return int(revision), candidate, explicit


def current_candidate_revision_record(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
) -> tuple[int | None, dict[str, Any] | None]:
    """当前候选 revision 及其 revision.json：编排器侧（账本 + 目录）决定，Store 侧必须一致。"""

    orchestrator = _orchestrator()
    ledger_dir = _ledger_dir(campaign_dir, manifest)
    last = orchestrator._ledger_last_stage_revision(ledger_dir) if ledger_dir is not None else None
    store_revision, store_candidate, explicit = store_current_revision(campaign_dir, manifest)
    if last is None:
        if explicit:
            raise ConfigurationError(
                f"Store 已登记候选 revision r{store_revision}（候选 {store_candidate}），编排器没有对应的 stage_revision：两份登记不一致。"
            )
        return None, None
    revision = int(last["revision"])
    record, commit = read_candidate_revision_record(campaign_dir, revision)
    if record is None or commit is None:
        raise ConfigurationError(f"账本已激活候选 revision r{revision}，但 revision 目录缺少记录或 COMMIT。")
    if commit["commit_sha256"] != last.get("revision_commit_sha256"):
        raise ConfigurationError(f"候选 revision r{revision} 的 COMMIT 与账本 stage_revision 不一致。")
    if record["candidate_id"] != last.get("candidate_id"):
        raise ConfigurationError(f"候选 revision r{revision} 的候选与账本 stage_revision 不一致。")
    if not explicit or (store_revision, store_candidate) != (revision, record["candidate_id"]):
        raise ConfigurationError(
            f"编排器已激活候选 revision r{revision}（候选 {record['candidate_id']}），Store 当前是 r{store_revision}"
            f"（候选 {store_candidate}）：重跑 revision-open 收敛两份登记。"
        )
    return revision, record


def current_candidate_revision(campaign_dir: Path, manifest: Mapping[str, Any]) -> int | None:
    return current_candidate_revision_record(campaign_dir, manifest)[0]


def require_candidate_revision(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    *,
    action: str,
) -> tuple[int, dict[str, Any] | None]:
    revision, record = current_candidate_revision_record(campaign_dir, manifest)
    if revision is None:
        raise ConfigurationError(f"{action} 需要激活的候选 revision；请先执行 revision-open --initial。")
    return revision, record


def candidate_identity_snapshot(
    campaign_dir: Path,
    candidate_id: str,
    candidate_source: Path | None,
) -> dict[str, Any]:
    """旧候选身份快照（来源优先级：构建收据 → attempt 的 candidate identity → --candidate-source；与 Codex 同一组字段）。

    构建收据读 Claude 的客户端中立合同（``official-client-upgrade-candidate-build-receipt/v1``）；git_commit 与
    source_tree_sha256 至少取得一项，两项都取不到即失败关闭。
    """

    orchestrator = _orchestrator()
    snapshot: dict[str, Any] = {
        "git_commit": None,
        "source_tree_sha256": None,
        "image_id": None,
        "binary_sha256": None,
        "image_digest": None,
        "build_parameters_sha256": None,
        "build_parameters_input_sha256": None,
        "build_inputs": None,
        "build_receipt_sha256": None,
        "snapshot_sources": [],
    }
    build_path = orchestrator._candidate_build_receipt_path(campaign_dir, candidate_id)
    if build_path.is_file() and not build_path.is_symlink():
        try:
            receipt = vc_artifacts.validate_official_client_candidate_build_receipt(orchestrator._read_json(build_path, "候选构建收据"))
        except vc_artifacts.VCArtifactError as error:
            raise ConfigurationError(f"旧候选构建收据不可信：{error}") from error
        snapshot.update(
            git_commit=receipt["source"]["git_commit"],
            source_tree_sha256=receipt["source"]["tree_sha256"],
            image_id=receipt["image"]["image_id"],
            binary_sha256=receipt["binary"]["sha256"],
            image_digest=receipt["image"]["manifest_digest"],
            build_parameters_sha256=receipt["build"]["parameters_sha256"],
            build_inputs=receipt["build"].get("inputs"),
            build_receipt_sha256=file_sha256(build_path),
        )
        if receipt["build"]["parameters"].get("schema_version") == codex_upgrade_candidate_build.BUILD_PARAMETERS_SCHEMA:
            snapshot["build_parameters_input_sha256"] = codex_upgrade_candidate_build.digest(
                codex_upgrade_candidate_build.implementation_parameter_projection(receipt["build"]["parameters"])
            )
        snapshot["snapshot_sources"].append("build_receipt")
    if snapshot["git_commit"] is None or snapshot["source_tree_sha256"] is None:
        attempts_root = campaign_dir / "candidates" / candidate_id / "attempts"
        latest: Mapping[str, Any] | None = None
        if attempts_root.is_dir() and not attempts_root.is_symlink():
            for attempt_root in sorted(attempts_root.iterdir()):
                attempt_path = attempt_root / "attempt.json"
                if attempt_root.is_symlink() or not attempt_path.is_file() or attempt_path.is_symlink():
                    continue
                identity = orchestrator._read_json(attempt_path, "候选 attempt 收据").get("identity")
                if isinstance(identity, Mapping):
                    latest = identity
        if latest is not None:
            used = False
            commit = latest.get("git_commit")
            tree = latest.get("source_tree_sha256")
            image_id = latest.get("image_id")
            if snapshot["git_commit"] is None and isinstance(commit, str) and GIT_COMMIT_RE.fullmatch(commit):
                snapshot["git_commit"] = commit
                used = True
            if snapshot["source_tree_sha256"] is None and isinstance(tree, str) and SHA256_RE.fullmatch(tree):
                snapshot["source_tree_sha256"] = tree
                used = True
            if snapshot["image_id"] is None and isinstance(image_id, str) and image_id:
                snapshot["image_id"] = image_id
                used = True
            if used:
                snapshot["snapshot_sources"].append("attempt_candidate_identity")
    if snapshot["git_commit"] is None or snapshot["source_tree_sha256"] is None:
        if candidate_source is not None:
            root = Path(candidate_source)
            if not root.is_absolute() or root.is_symlink() or not root.is_dir():
                raise ConfigurationError("--candidate-source 必须是可信绝对目录。")
            root = root.resolve(strict=True)
            commit = orchestrator._git_commit(root)
            used = False
            if snapshot["git_commit"] is None and isinstance(commit, str) and GIT_COMMIT_RE.fullmatch(commit):
                snapshot["git_commit"] = commit
                used = True
            if snapshot["source_tree_sha256"] is None:
                snapshot["source_tree_sha256"] = orchestrator._directory_tree_digest(root)
                used = True
            if used:
                snapshot["snapshot_sources"].append("candidate_source")
        elif snapshot["git_commit"] is None and snapshot["source_tree_sha256"] is None:
            raise ConfigurationError("旧候选既无构建收据也无 attempt 身份投影，必须提供 --candidate-source 指向旧候选源码树。")
    if snapshot["git_commit"] is None and snapshot["source_tree_sha256"] is None:
        raise ConfigurationError("旧候选身份快照无法取得 git_commit 与 source_tree_sha256，拒绝作废。")
    return snapshot


def implicit_r1_candidate_id(campaign_dir: Path, manifest: Mapping[str, Any]) -> str:
    """服务面 ``_implicit_r1_candidate_id`` 的 Claude 版：Claude 没有隐含 r1，一律拒绝（见模块说明第一节）。"""

    del campaign_dir, manifest
    raise ConfigurationError(
        "Claude Campaign 没有隐含 r1：候选 revision 一律由 revision-open --initial 显式登记（编排器与 Store 两份登记）。"
    )


# ---------------------------------------------------------------------------
# 照搬 Codex：由 candidate_review_required 事件定位失败父 run
# ---------------------------------------------------------------------------


def _read_staging_commit(commit_path: Path) -> dict[str, Any]:
    """转调编排器服务面的同名成员（共享实现在 ``upgrade_control_plane._read_staging_commit``）。"""

    return _orchestrator()._read_staging_commit(commit_path)


def _candidate_failed_run_for_review(
    campaign_dir: Path,
    manifest: Mapping[str, Any],
    candidate_id: str,
    review_event: Mapping[str, Any],
) -> dict[str, Any]:
    """由 candidate_review_required 事件定位引起它的那一次失败父 run（不依赖对账收据）。

    遍历本 Campaign 候选级阶段的正式 COMMIT → ``parent_run_dir`` → 父 run 的动作失败事实，
    用与监督器收账相同的失败摘要（Campaign／阶段／批次序号／失败动作／失败分类）比对事件
    id 的摘要段，并要求内层清单绑定本候选／本 revision；必须恰好命中一个。
    """

    expected_prefix = codex_upgrade_supervisor.parse_candidate_review_event_id(review_event.get("event_id"))
    if expected_prefix is None:
        raise ConfigurationError("candidate_review_required 事件 id 形态非法，无法绑定失败父 run。")
    review_revision = review_event.get("revision")
    campaign_id = str(manifest.get("campaign_id", ""))
    commits_root = campaign_dir / "control" / "vc" / "commits"
    if commits_root.is_symlink():
        raise ConfigurationError("COMMIT 目录不得是符号链接。")
    matched: list[dict[str, Any]] = []
    if commits_root.is_dir():
        for path in sorted(commits_root.iterdir()):
            match = _VC_SEQUENCE_FILE_RE.fullmatch(path.name)
            if match is None or path.is_symlink() or not path.is_file():
                raise ConfigurationError(f"COMMIT 目录含非法条目：{path.name}")
            commit = _read_staging_commit(path)
            if commit["campaign_id"] != campaign_id or str(commit["phase"]) not in CANDIDATE_VC_PHASES:
                continue
            run_dir = Path(str(commit.get("parent_run_dir", "")))
            if not run_dir.is_absolute():
                continue
            try:
                facts = codex_upgrade_supervisor.campaign_run_failure_facts(run_dir, campaign_dir=campaign_dir)
            except codex_upgrade_supervisor.SupervisorError as error:
                raise ConfigurationError(f"父 run {run_dir.name} 的失败事实不可信：{error}") from error
            if facts is None:
                continue
            if (
                facts["candidate_id"] != candidate_id
                or facts["candidate_revision"] != review_revision
                or facts["batch_sequence"] != commit["sequence"]
                or facts["failure_digest"][: codex_upgrade_supervisor.FAILURE_DIGEST_PREFIX_LENGTH] != expected_prefix
            ):
                continue
            matched.append(facts)
    if len(matched) != 1:
        raise ConfigurationError(
            f"candidate_review_required（事件 {review_event.get('event_id')}）无法唯一定位候选 {candidate_id} 的失败父 run"
            f"（命中 {len(matched)} 个）。"
        )
    return matched[0]


# ---------------------------------------------------------------------------
# revision seal
# ---------------------------------------------------------------------------


def seal_candidate_revision(
    campaign_dir: Path,
    revision_record: Mapping[str, Any],
    *,
    candidate_commit: str | None,
    source_tree_sha256: str,
    image_id: str | None,
    build_receipt_sha256: str,
    vc3_stage_receipt_sha256: str,
    binary_sha256: str | None,
    image_digest: str | None,
    build_parameters_sha256: str | None,
    build_parameters_input_sha256: str | None,
    write: bool = True,
) -> dict[str, Any]:
    """写当前 revision 的 seal.json（同一性变化证明），已存在时按内容核对；``write=False`` 只构造与校验、不落盘。

    与 Codex ``_seal_candidate_revision`` 同一判据，只是被取代候选的失效标记用 Claude 的合同名（见模块说明第四节）。
    """

    orchestrator = _orchestrator()
    revision = int(revision_record["revision"])
    superseded: dict[str, Any] | None = None
    supersedes = revision_record.get("supersedes")
    if isinstance(supersedes, Mapping):
        old_candidate_id = str(supersedes["candidate_id"])
        invalidation_path = campaign_dir / "candidates" / old_candidate_id / orchestrator.CANDIDATE_INVALIDATION_FILENAME
        if invalidation_path.is_symlink() or not invalidation_path.is_file():
            raise ConfigurationError(f"被取代候选 {old_candidate_id} 缺少 invalidation.json。")
        try:
            invalidation = vc_artifacts.validate_candidate_invalidation(orchestrator._read_json(invalidation_path, "候选作废记录"))
        except vc_artifacts.VCArtifactError as error:
            raise ConfigurationError(str(error)) from error
        if file_sha256(invalidation_path) != str(supersedes["invalidation_receipt"]["sha256"]):
            raise ConfigurationError("被取代候选的 invalidation.json 与 revision 记录绑定漂移。")
        snapshot = invalidation["identity_snapshot"]
        superseded = {
            "revision": int(supersedes["revision"]),
            "candidate_id": old_candidate_id,
            **{
                key: snapshot.get(key)
                for key in (
                    "git_commit", "source_tree_sha256", "image_id", "binary_sha256", "image_digest",
                    "build_parameters_sha256", "build_parameters_input_sha256",
                )
            },
        }
    try:
        seal = vc_artifacts.build_candidate_revision_seal(
            campaign_id=str(revision_record["campaign_id"]),
            revision=revision,
            candidate_id=str(revision_record["candidate_id"]),
            candidate_commit=candidate_commit,
            source_tree_sha256=source_tree_sha256,
            image_id=image_id,
            binary_sha256=binary_sha256,
            image_digest=image_digest,
            build_parameters_sha256=build_parameters_sha256,
            build_parameters_input_sha256=build_parameters_input_sha256,
            build_receipt_sha256=build_receipt_sha256,
            vc3_stage_receipt_sha256=vc3_stage_receipt_sha256,
            superseded=superseded,
            sealed_at_utc=orchestrator._utc_now(),
        )
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(f"revision-seal 拒绝：{error}") from error
    if not write:
        return seal
    seal_path = orchestrator._candidate_revision_dir(campaign_dir, revision) / REVISION_SEAL_FILENAME
    orchestrator._write_or_verify_json_ignoring(seal_path, seal, volatile=("sealed_at_utc", "seal_sha256"))
    stored = orchestrator._read_json(seal_path, "候选 revision seal")
    if isinstance(supersedes, Mapping):
        marker = campaign_dir / "candidates" / str(supersedes["candidate_id"]) / SUPERSEDED_MARKER_FILENAME
        orchestrator._write_or_verify_json_ignoring(
            marker,
            {
                "schema_version": SUPERSEDED_MARKER_SCHEMA,
                "candidate_id": str(supersedes["candidate_id"]),
                "superseded_by": {"revision": revision, "candidate_id": str(revision_record["candidate_id"])},
            },
            volatile=(),
        )
    return stored


# ---------------------------------------------------------------------------
# revision-open：共享流程＋Store candidate_revision_recorded
# ---------------------------------------------------------------------------


def _store_session(campaign_dir: Path, store: Any, store_campaign: Mapping[str, Any]) -> Any:
    return claude_upgrade_seal._StoreSession(Path(store.root), str(store_campaign["campaign_id"]), campaign_dir / STORE_SCRATCH_RELATIVE)


def _store_revision_target(
    store: Any,
    campaign: Mapping[str, Any],
    candidate_id: str,
    *,
    initial: bool,
    supersedes_id: str | None,
) -> dict[str, Any]:
    """revision-open 在 Store 侧要写（或已写）的 ``candidate_revision_recorded``：``{payload, existing}``（见模块说明第二节）。"""

    facts = store_receipts.candidate_revision_facts(store, dict(campaign))
    if initial:
        payload = {"revision": 1, "candidate_id": candidate_id, "supersedes": None, "invalidation_ref": None}
        if not facts:
            return {"payload": payload, "existing": None}
        if len(facts) == 1 and facts[0]["payload"] == payload:
            return {"payload": payload, "existing": facts[0]}
        last = facts[-1]["payload"]
        raise ConfigurationError(
            f"Store 已登记候选 revision r{last['revision']}（候选 {last['candidate_id']}），--initial 只能幂等重放同一候选的 r1。"
        )
    if not facts:
        raise ConfigurationError("Store 没有可取代的候选 revision；新 Campaign 请用 --initial。")
    last = facts[-1]
    if last["payload"]["candidate_id"] == candidate_id:
        # 幂等重放：Store 已以本候选登记了取代 supersedes_id 的新 revision。
        previous = facts[-2]["payload"] if len(facts) >= 2 else None
        if last["payload"]["revision"] < 2 or previous is None or previous["candidate_id"] != supersedes_id:
            raise ConfigurationError(f"Store 的当前候选已是 {candidate_id}，但它不是取代 {supersedes_id} 的新 revision。")
        return {"payload": dict(last["payload"]), "existing": last}
    current = int(last["payload"]["revision"])
    if last["payload"]["candidate_id"] != supersedes_id:
        raise ConfigurationError(
            f"Store 当前 revision r{current} 的候选是 {last['payload']['candidate_id']}，不是 --supersedes {supersedes_id}。"
        )
    try:
        found = store_receipts.revision_invalidation(store, dict(campaign), current)
    except ControlError as error:
        raise ConfigurationError(str(error)) from error
    if found is None:
        raise ConfigurationError(
            f"Store 尚未作废 r{current}（候选 {supersedes_id}）：先以同一批准摘要重跑 invalidate-candidate apply，让 Store 侧作废收敛。"
        )
    payload = {
        "revision": current + 1,
        "candidate_id": candidate_id,
        "supersedes": dict(store.fact_ref(last)),
        "invalidation_ref": dict(found[0]),
    }
    return {"payload": payload, "existing": None}


def _revision_mode(arguments: argparse.Namespace) -> tuple[str, bool, str | None]:
    candidate_id = str(getattr(arguments, "candidate_id", "") or "")
    if not _orchestrator().SAFE_ID_RE.fullmatch(candidate_id):
        raise ConfigurationError("--candidate-id 格式非法。")
    initial = bool(getattr(arguments, "initial", False))
    supersedes_id = getattr(arguments, "supersedes", None)
    if initial == bool(supersedes_id):
        raise ConfigurationError("revision-open 必须且只能给出 --initial 或 --supersedes 之一。")
    return candidate_id, initial, (str(supersedes_id) if supersedes_id else None)


def revision_open(arguments: argparse.Namespace) -> dict[str, Any]:
    """``revision-open`` 的 Claude 实现：Store 预检 → 共享流程 → Store 事实 → 复核两份登记（见模块说明第二节）。

    由 ``claude_upgrade._run_shared_subcommand`` 在 Claude 适配器作用域里调用。
    """

    orchestrator = _orchestrator()
    campaign_dir = Path(arguments.campaign_dir)
    candidate_id, initial, supersedes_id = _revision_mode(arguments)
    manifest = orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
        claude_upgrade_seal._require_store_tool_current(store, store_campaign)
        _store_revision_target(store, store_campaign, candidate_id, initial=initial, supersedes_id=supersedes_id)
    result = upgrade_control_plane.open_candidate_revision(arguments)
    revision = int(result["revision"])
    with orchestrator._campaign_lock(campaign_dir):
        record, commit = read_candidate_revision_record(campaign_dir, revision)
        last = orchestrator._ledger_last_stage_revision(orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest))
        if (
            record is None
            or commit is None
            or last is None
            or int(last["revision"]) != revision
            or last.get("candidate_id") != candidate_id
            or last.get("revision_commit_sha256") != commit["commit_sha256"]
        ):
            raise ConfigurationError(f"revision-open 之后编排器侧 r{revision} 的目录登记与账本 stage_revision 不一致。")
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
        target = _store_revision_target(store, store_campaign, candidate_id, initial=initial, supersedes_id=supersedes_id)
        if int(target["payload"]["revision"]) != revision:
            raise ConfigurationError(f"Store 侧要登记 r{target['payload']['revision']}，与编排器激活的 r{revision} 不一致。")
        if target["existing"] is not None:
            fact_ref, reused = dict(store.fact_ref(target["existing"])), True
        else:
            fact = _store_session(campaign_dir, store, store_campaign).record_fact(
                "candidate-revision-record",
                "candidate_revision_recorded",
                target["payload"],
                issued_at=str(record["opened_at_utc"]),
                logical_action=REVISION_OPEN_LOGICAL_ACTION,
            )
            fact_ref, reused = dict(fact["fact_ref"]), bool(fact["reused"])
        current, _record = current_candidate_revision_record(campaign_dir, manifest)
        if current != revision:
            raise ConfigurationError(f"revision-open 之后两份登记仍不一致（当前 r{current}，期望 r{revision}）。")
    return {
        **result,
        "store": {"candidate_revision_fact_ref": fact_ref, "reused": reused, "payload": target["payload"]},
    }


# ---------------------------------------------------------------------------
# invalidate-candidate：共享流程＋Store 作废对象
# ---------------------------------------------------------------------------


def invalidate_candidate(arguments: argparse.Namespace) -> dict[str, Any]:
    """``invalidate-candidate`` 的 Claude 实现：共享流程之后，apply 通过时写 Store 作废对象（见模块说明第三节）。

    由 ``claude_upgrade._run_shared_subcommand`` 在 Claude 适配器作用域里调用。
    """

    orchestrator = _orchestrator()
    result = upgrade_control_plane.invalidate_candidate(arguments)
    if result.get("status") == "preview":
        result["store_invalidation"] = {
            "object_kind": "candidate_invalidation",
            "written_on": "apply",
            "derivation": (
                "apply 通过（账本 candidate_invalidated）后由编排器写 Store 作废对象：revision_ref 引用该 revision 的 Store 事实，"
                "证据为 operational_evidence 清单（作废记录与诊断证据绑定），批准人取本诊断的审核人、批准时刻取作废记录时刻；"
                "Store 两步批准以本 review_sha256 的审核结论转写，不另行审核。"
            ),
        }
        return result
    if result.get("status") != "applied":
        return result
    campaign_dir = Path(arguments.campaign_dir)
    manifest = orchestrator._require_formal_campaign(campaign_dir)
    with orchestrator._campaign_lock(campaign_dir):
        store_result = _record_store_invalidation(campaign_dir, manifest, str(result["candidate_id"]), int(result["revision"]))
    return {**result, "store": store_result}


def _record_store_invalidation(campaign_dir: Path, manifest: Mapping[str, Any], candidate_id: str, revision: int) -> dict[str, Any]:
    orchestrator = _orchestrator()
    ledger_dir = orchestrator._campaign_timing_ledger_dir(campaign_dir, manifest)
    last = orchestrator._ledger_last_event_of_type(ledger_dir, "candidate_invalidated")
    if last is None or last[0].get("candidate_id") != candidate_id or int(last[0].get("revision") or 0) != revision:
        raise ConfigurationError(f"账本没有对候选 {candidate_id}（r{revision}）的 candidate_invalidated 事件，Store 不作废。")
    invalidation_path = campaign_dir / "candidates" / candidate_id / orchestrator.CANDIDATE_INVALIDATION_FILENAME
    if invalidation_path.is_symlink() or not invalidation_path.is_file():
        raise ConfigurationError(f"候选 {candidate_id} 缺少 invalidation.json。")
    try:
        invalidation = vc_artifacts.validate_candidate_invalidation(orchestrator._read_json(invalidation_path, "候选作废记录"))
    except vc_artifacts.VCArtifactError as error:
        raise ConfigurationError(str(error)) from error
    if invalidation["candidate_id"] != candidate_id or invalidation["revision"] != revision:
        raise ConfigurationError("invalidation.json 与被作废候选／revision 不一致。")
    store, store_campaign = claude_upgrade_store.open_campaign_store(campaign_dir, manifest)
    campaign = dict(store_campaign)
    revision_facts = [fact for fact in store_receipts.candidate_revision_facts(store, campaign) if fact["payload"]["revision"] == revision]
    if len(revision_facts) != 1 or revision_facts[0]["payload"]["candidate_id"] != candidate_id:
        raise ConfigurationError(f"Store 没有候选 {candidate_id} 的 r{revision} revision 事实：两份 revision 登记不一致。")
    diagnosis = invalidation["diagnosis"]
    relative = invalidation_path.relative_to(campaign_dir).as_posix()
    entries = [
        {
            "id": "invalidation-record",
            "facts": {
                "path": relative,
                "sha256": file_sha256(invalidation_path),
                "receipt_sha256": invalidation["receipt_sha256"],
                "root_cause_id": invalidation["root_cause_id"],
                "review_sha256": diagnosis["review_sha256"],
            },
        },
        *(
            {"id": f"evidence-{index:03d}", "facts": dict(reference)}
            for index, reference in enumerate(diagnosis["evidence_refs"], start=1)
        ),
    ]
    evidence_manifest = {
        "schema_version": OPERATIONAL_EVIDENCE_SCHEMA,
        "persona": dict(store_campaign["persona"]),
        "manifest_id": f"{INVALIDATION_EVIDENCE_MANIFEST_PREFIX}-r{revision}",
        "entries": sorted(entries, key=lambda entry: str(entry["id"])),
    }
    session = _store_session(campaign_dir, store, store_campaign)
    evidence_ref = session.seal_object("operational_evidence", evidence_manifest)
    review = {
        "campaign_id": str(store_campaign["campaign_id"]),
        "candidate_id": candidate_id,
        "revision": revision,
        "revision_ref": dict(store.fact_ref(revision_facts[0])),
        "reason": (
            f"{invalidation['root_cause_id']}：候选源码需要变化。编排器作废记录 {relative}（receipt_sha256 "
            f"{invalidation['receipt_sha256']}），诊断审核摘要 {diagnosis['review_sha256']}。"
        ),
        "evidence_refs": [evidence_ref],
    }
    payload = {
        **review,
        "approved_by": str(diagnosis["reviewer"]),
        "approved_at_utc": str(invalidation["recorded_at_utc"]),
        "review_sha256": invalidation_review_sha256(review),
    }
    outcome = session._with_input(
        payload,
        lambda path: claude_upgrade_seal.store_cli(
            [
                "candidate-invalidate", "--store", session.root, "--campaign", session.campaign_id, "--input", path,
                "--approve-sha256", payload["review_sha256"], "--allow-existing",
            ]
        ),
    )
    return {
        "candidate_invalidation_ref": dict(outcome["object_ref"]),
        "reused": bool(outcome["reused"]),
        "evidence_ref": evidence_ref,
        "review_sha256": payload["review_sha256"],
    }
