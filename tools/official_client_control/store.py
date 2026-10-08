"""内容寻址、只写追加的 FW-D Store 与正交事实账本。"""

from __future__ import annotations

import fcntl
import os
import re
import stat
import threading
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from typing import Any, Iterable, Iterator

from .canonical import (
    canonical_json_bytes,
    canonical_sha256,
    ensure_directory,
    expect_rfc3339,
    load_json_file,
    parse_json_bytes,
    resolve_relative,
    sha256_bytes,
    sha256_file,
    validate_external_binding,
    verify_mode,
    write_once,
)
from .contracts import (
    CAMPAIGN_SCHEMA,
    DIMENSIONS,
    FACT_DIMENSION,
    FACT_LOOKUP_SCHEMA,
    FACT_SCHEMA,
    FACT_V2_SCHEMA,
    OBJECT_KINDS,
    OBJECT_SCHEMA,
    RECEIPT_KINDS,
    STORE_SCHEMA,
    campaign_is_v2,
    fact_is_v2,
    iter_fact_refs,
    iter_object_refs,
    iter_receipt_refs,
    validate_campaign,
    validate_fact_document,
    validate_fact_ref,
    validate_idempotency_key,
    validate_object_document,
    validate_object_ref,
    validate_producer_run,
    validate_receipt_document,
    validate_receipt_ref,
    validate_store_metadata,
)
from .errors import ControlError


_FACT_FILE_RE = re.compile(r"^(\d{8})-([0-9a-f]{64})\.json$")


class ControlStore:
    """Persona 无关、内容寻址且可完整重放的控制面存储。"""

    def __init__(self, root: Path, *, read_only: bool = False) -> None:
        if not root.is_absolute():
            raise ControlError("Store 根必须是绝对路径")
        if root.is_symlink() or not root.is_dir():
            raise ControlError("Store 根必须是现有的非符号链接目录")
        # 只读打开（B1-14 读前序 Store）：额外接受去掉写权限的归档权限（根 0500、文件 0400），任何写入都拒绝。
        self.read_only = read_only
        self._lock_state = threading.local()
        self._root_modes = (0o700, 0o500) if read_only else (0o700,)
        self._file_modes = (0o600, 0o400) if read_only else (0o600,)
        self.root = root.resolve()
        self._verify_mode(self.root, self._root_modes, "Store 根")
        metadata_path = self.root / "store.json"
        metadata = load_json_file(metadata_path, "Store metadata")
        validate_store_metadata(metadata)
        self._verify_mode(metadata_path, self._file_modes, "Store metadata")

    @classmethod
    def open_read_only(cls, root: Path) -> "ControlStore":
        """只读打开一个 Store（例如前序证据所在的 Store）：只能读取，锁与任何写入都拒绝。"""

        return cls(root, read_only=True)

    @staticmethod
    def _verify_mode(path: Path, modes: tuple[int, ...], label: str) -> None:
        if len(modes) == 1:
            verify_mode(path, modes[0], label)
            return
        actual = stat.S_IMODE(path.stat().st_mode)
        if actual not in modes:
            raise ControlError(
                f"{label} 权限必须是 {' 或 '.join(oct(mode) for mode in modes)}，实际为 {oct(actual)}"
            )

    @classmethod
    def initialize(cls, root: Path, created_at_utc: str) -> "ControlStore":
        if not root.is_absolute():
            raise ControlError("Store 根必须是绝对路径")
        expect_rfc3339(created_at_utc, "created_at_utc")
        if root.exists():
            if root.is_symlink() or not root.is_dir():
                raise ControlError("Store 根路径不可信")
            if any(root.iterdir()):
                raise ControlError("Store 初始化要求空目录")
            root.chmod(0o700)
        else:
            ensure_directory(root)
        for relative in ("objects", "campaigns", "facts", "receipts", "locks"):
            ensure_directory(root / relative)
        metadata = {"schema_version": STORE_SCHEMA, "created_at_utc": created_at_utc}
        write_once(root / "store.json", canonical_json_bytes(metadata))
        return cls(root)

    @contextmanager
    def _lock(self, name: str) -> Iterator[None]:
        # 每条写入路径都先取锁（对象、事实、收据、Campaign），只读实例在这里统一拒绝。
        if self.read_only:
            raise ControlError(f"只读打开的 Store 禁止写入：{self.root}")
        held = getattr(self._lock_state, "held", None)
        if held is None:
            held = self._lock_state.held = set()
        if name in held:
            # 同一实例、同一线程的 finalizer → 直接发布复核复用维度锁；其他线程仍取真实 flock。
            yield
            return
        lock_path = self.root / "locks" / f"{name}.lock"
        ensure_directory(lock_path.parent)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            held.add(name)
            try:
                yield
            finally:
                held.remove(name)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _dimension_locks(self, campaign_id: str, dimensions: Iterable[str]) -> Iterator[None]:
        """按 ``DIMENSIONS`` 的固定顺序取同一 Campaign 若干维度的事实锁（与 ``append_fact`` 同名），避免死锁。"""

        wanted = set(dimensions)
        with ExitStack() as stack:
            for dimension in DIMENSIONS:
                if dimension in wanted:
                    stack.enter_context(self._lock(f"fact-{campaign_id}-{dimension}"))
            yield

    def object_path(self, reference: dict[str, Any]) -> Path:
        parsed = validate_object_ref(reference)
        return self.root / "objects" / parsed["object_kind"] / f"{parsed['sha256']}.json"

    def receipt_path(self, reference: dict[str, Any]) -> Path:
        parsed = validate_receipt_ref(reference)
        return self.root / "receipts" / parsed["receipt_kind"] / f"{parsed['sha256']}.json"

    @staticmethod
    def _holds_identical(path: Path, content: bytes) -> bool:
        """不可变文件已存在且字节与 ``content`` 完全相同（内容寻址路径下即同一份制品）。"""

        return not path.is_symlink() and path.is_file() and path.read_bytes() == content

    def _write_once_or_reuse(
        self, path: Path, content: bytes, lock_name: str, *, allow_existing: bool
    ) -> bool:
        """write-once 写入；``allow_existing`` 时目标已有同内容即视为复用。返回是否复用。

        同一路径的写入方（对象、收据、Campaign）都持同名锁，锁内「先查后写」没有并发窗口；write-once 的其它失败
        （内容不同的覆盖、父目录落盘失败）照常抛出，不当作复用。
        """

        with self._lock(lock_name):
            if allow_existing and self._holds_identical(path, content):
                return True
            write_once(path, content)
        return False

    def seal_object(
        self, object_kind: str, payload: dict[str, Any], *, allow_existing: bool = False
    ) -> dict[str, Any]:
        """校验并只写一次封存对象；同摘要再次写入按覆盖拒绝，``allow_existing=True`` 时同内容返回原引用。"""

        return self.seal_object_outcome(
            object_kind, payload, allow_existing=allow_existing
        )["object_ref"]

    def seal_object_outcome(
        self, object_kind: str, payload: dict[str, Any], *, allow_existing: bool = False
    ) -> dict[str, Any]:
        """同 ``seal_object``，返回 ``{"object_ref": 引用, "reused": 是否复用已封存的同内容对象}``。

        ``allow_existing``（Claude 升级 B1-13）：同内容对象已封存时直接返回原引用，不再跑写入时门禁。对象内容寻址，
        同摘要即同一对象，它首次封存时已过门禁、重放另有对象图复核；重试时的写入时门禁反而会因对象自身已存在
        而失败（例如演进登记的「接在当前链尾」）。
        """

        if object_kind not in OBJECT_KINDS:
            raise ControlError(f"未登记 object_kind：{object_kind}")
        document = {
            "schema_version": OBJECT_SCHEMA,
            "object_kind": object_kind,
            "payload": payload,
        }
        validate_object_document(document)
        content = canonical_json_bytes(document)
        digest = sha256_bytes(content)
        reference = {"object_kind": object_kind, "sha256": digest}
        path = self.object_path(reference)
        if allow_existing and self._holds_identical(path, content):
            self.load_object(reference)
            return {"object_ref": reference, "reused": True}
        for referenced in iter_object_refs(payload):
            self.load_object(referenced)
        from .gates import WorkflowGates

        # 工具演进登记按 Campaign 串行：链门禁（index、previous_ref、from_identity）与写入在同一把锁内，
        # 防止两次并发登记都按同一个链尾通过而分叉。
        # 前序导入登记（B1-14）与该 Campaign 的 evidence 维度事实写入共用维度锁：登记门禁「列入的 EvidencePackage
        # 没被本 Campaign 事实当本地证据引用」与 EvidenceFact 的导入判定不会交错。
        # 候选作废（B3-05）与候选级事实写入（validation、deployment 两个维度）共用维度锁：作废门禁核完「当前 revision、
        # 候选未验收」之后，不会有候选级事实插在作废对象落盘之前。交付阶段失效与恢复段预约（B4-07）与交付事实写入
        # 共用 deployment 维度锁：链状态（最新段已收口、待消费的失效）在锁内判定，不会与新交付事实交错。
        if object_kind == "tool_evolution_registration":
            serial: Any = self._lock(f"tool-evolution-{payload['campaign_id']}")
        elif object_kind == "predecessor_import":
            serial = self._lock(f"fact-{payload['campaign_id']}-{FACT_DIMENSION['evidence_recorded']}")
        elif object_kind == "candidate_invalidation":
            serial = self._dimension_locks(
                payload["campaign_id"],
                (FACT_DIMENSION["candidate_frozen"], FACT_DIMENSION["candidate_delivery_recorded"]),
            )
        elif object_kind in {"candidate_delivery_stage_invalidation", "candidate_delivery_recovery"}:
            serial = self._dimension_locks(
                payload["campaign_id"], (FACT_DIMENSION["candidate_delivery_recorded"],)
            )
        else:
            serial = nullcontext()
        with serial:
            WorkflowGates(self).validate_new_object(document)
            reused = self._write_once_or_reuse(
                path, content, f"object-{object_kind}-{digest}", allow_existing=allow_existing
            )
        return {"object_ref": reference, "reused": reused}

    def object_bytes(self, reference: dict[str, Any]) -> bytes:
        """对象文件的原始字节（经权限、摘要与合同核验）；前序导入按字节复制与比对时用。

        返回前再按引用摘要核一次读到的字节，保证交出去的正是被引用的那份内容。
        """

        parsed = validate_object_ref(reference)
        self.load_object(parsed)
        path = self.object_path(parsed)
        content = path.read_bytes()
        if sha256_bytes(content) != parsed["sha256"]:
            raise ControlError(f"对象摘要漂移：{path}")
        return content

    def import_object(self, reference: dict[str, Any], content: bytes) -> bool:
        """把前序 Store 的对象逐字节复制进本 Store（B1-14），返回是否复用已有的同内容对象。

        内容寻址：字节摘要必须等于引用，且就是规范 JSON；对象合同有效、类型可导入且只引用对象（绑定前序 Campaign
        控制状态、或引用事实／收据的对象不可导入），嵌套对象引用已在本 Store（调用方按依赖顺序复制闭包），对象图
        按重放口径复核（不跑写入时门禁——对象在前序 Store 形成时已过门禁）。
        """

        from .contracts import require_importable_object
        from .gates import WorkflowGates

        parsed = validate_object_ref(reference, "imported_object")
        if sha256_bytes(content) != parsed["sha256"]:
            raise ControlError(f"导入对象字节与引用摘要不符：{parsed}")
        document = parse_json_bytes(content, "导入对象")
        validate_object_document(document)
        if document["object_kind"] != parsed["object_kind"]:
            raise ControlError(f"导入对象类型与引用不一致：{parsed}")
        if canonical_json_bytes(document) != content:
            raise ControlError(f"导入对象不是规范 JSON：{parsed}")
        require_importable_object(document)
        for nested in iter_object_refs(document["payload"]):
            self.load_object(nested)
        WorkflowGates(self).validate_imported_object(document)
        return self._write_once_or_reuse(
            self.object_path(parsed),
            content,
            f"object-{parsed['object_kind']}-{parsed['sha256']}",
            allow_existing=True,
        )

    def list_object_refs(self, kind: str) -> list[dict[str, Any]]:
        """列出某类型的全部对象引用并逐个核对摘要与类型；目录不存在时为空。"""

        if kind not in OBJECT_KINDS:
            raise ControlError(f"未登记 object_kind：{kind}")
        directory = self.root / "objects" / kind
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ControlError(f"对象目录不可信：{directory}")
        references: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(
                r"[0-9a-f]{64}\.json", path.name
            ):
                raise ControlError(f"对象路径非法：{path}")
            reference = {"object_kind": kind, "sha256": path.stem}
            self.load_object(reference)
            references.append(reference)
        return references

    def load_object(self, reference: dict[str, Any]) -> dict[str, Any]:
        parsed = validate_object_ref(reference)
        path = self.object_path(parsed)
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"对象引用不存在：{parsed}")
        self._verify_mode(path, self._file_modes, "对象")
        content = path.read_bytes()
        if sha256_bytes(content) != parsed["sha256"]:
            raise ControlError(f"对象摘要漂移：{path}")
        document = load_json_file(path, "对象")
        validate_object_document(document)
        if document["object_kind"] != parsed["object_kind"]:
            raise ControlError(f"对象类型与路径不一致：{path}")
        return document

    def create_campaign(
        self, campaign: dict[str, Any], *, allow_existing: bool = False
    ) -> dict[str, Any]:
        """建立不可变 Campaign。

        Campaign v2 另核对冻结的工具身份与当前受管代码一致：现算多根受管清单的指纹等于
        ``tool_identity.tool_files_sha256``，三层摘要等于 v3 引擎现算值，清单内 Store 口径条目复算等于
        ``tool_bundle_sha256``；Store 任一文件被改而没有进入冻结清单（或没进受管清单）即拒绝。v1 行为不变。

        ``allow_existing``（B1-13，编排器重跑建 Campaign 的步骤时用）：同 ID 的 Campaign 已存在且字节相同即返回原
        结果并附 ``reused: true``，不再核对当前代码（Campaign 冻结的是建立时的身份）；内容不同仍拒绝。
        """

        validate_campaign(campaign)
        campaign_id = campaign["campaign_id"]
        path = self.root / "campaigns" / f"{campaign_id}.json"
        content = canonical_json_bytes(campaign)
        result: dict[str, Any] = {
            "campaign_id": campaign_id,
            "identity_sha256": campaign["identity_sha256"],
        }
        if allow_existing and (path.exists() or path.is_symlink()):
            if canonical_json_bytes(self.load_campaign(campaign_id)) != content:
                raise ControlError(f"Campaign 已存在且内容不同，禁止覆盖：{campaign_id}")
            return result | {"reused": True}
        self.load_object(campaign["bootstrap_ref"])
        if campaign_is_v2(campaign):
            from .receipts import verify_campaign_tool_manifest

            verify_campaign_tool_manifest(campaign)
        reused = self._write_once_or_reuse(
            path, content, f"campaign-{campaign_id}", allow_existing=allow_existing
        )
        return result | {"reused": reused} if allow_existing else result

    def load_campaign(self, campaign_id: str) -> dict[str, Any]:
        if not isinstance(campaign_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}", campaign_id
        ):
            raise ControlError("campaign_id 非法")
        path = self.root / "campaigns" / f"{campaign_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"Campaign 不存在：{campaign_id}")
        self._verify_mode(path, self._file_modes, "Campaign")
        campaign = load_json_file(path, "Campaign")
        validate_campaign(campaign)
        if campaign["campaign_id"] != campaign_id:
            raise ControlError("Campaign ID 与路径不一致")
        return campaign

    def _campaign_dimension_path(
        self, campaign_id: str, dimension: str
    ) -> tuple[dict[str, Any], Path]:
        campaign = self.load_campaign(campaign_id)
        if dimension not in DIMENSIONS:
            raise ControlError(f"事实维度非法：{dimension}")
        return campaign, self.root / "facts" / campaign_id / dimension

    def _dimension_path(self, campaign_id: str, dimension: str) -> Path:
        return self._campaign_dimension_path(campaign_id, dimension)[1]

    def list_facts(self, campaign_id: str, dimension: str | None = None) -> list[dict[str, Any]]:
        dimensions = [dimension] if dimension is not None else list(DIMENSIONS)
        facts: list[dict[str, Any]] = []
        for current_dimension in dimensions:
            campaign, path = self._campaign_dimension_path(campaign_id, current_dimension)
            # v1 Campaign 只含 fact v1、v2 Campaign 只含 fact v2，两种合同不得混在一条链里。
            expects_v2 = campaign_is_v2(campaign)
            if not path.exists():
                continue
            if path.is_symlink() or not path.is_dir():
                raise ControlError(f"事实目录不可信：{path}")
            previous: str | None = None
            expected_sequence = 1
            for fact_path in sorted(path.iterdir()):
                if fact_path.is_symlink() or not fact_path.is_file():
                    raise ControlError(f"事实路径不是可信普通文件：{fact_path}")
                match = _FACT_FILE_RE.fullmatch(fact_path.name)
                if match is None:
                    raise ControlError(f"事实文件名非法：{fact_path.name}")
                sequence = int(match.group(1))
                digest = match.group(2)
                if sequence != expected_sequence:
                    raise ControlError(f"事实链缺号或分叉：{fact_path}")
                self._verify_mode(fact_path, self._file_modes, "事实")
                content = fact_path.read_bytes()
                if sha256_bytes(content) != digest:
                    raise ControlError(f"事实摘要漂移：{fact_path}")
                document = load_json_file(fact_path, "事实")
                validate_fact_document(document)
                if fact_is_v2(document) != expects_v2:
                    raise ControlError(f"事实合同版本与 Campaign 版本不一致：{fact_path}")
                if (
                    document["campaign_id"] != campaign_id
                    or document["dimension"] != current_dimension
                    or document["sequence"] != sequence
                    or document["previous_fact_sha256"] != previous
                ):
                    raise ControlError(f"事实链身份或前序不一致：{fact_path}")
                fact = dict(document)
                fact["_sha256"] = digest
                facts.append(fact)
                previous = digest
                expected_sequence += 1
        return facts

    def fact_ref(self, fact: dict[str, Any]) -> dict[str, Any]:
        return {
            "campaign_id": fact["campaign_id"],
            "dimension": fact["dimension"],
            "sequence": fact["sequence"],
            "sha256": fact["_sha256"],
        }

    def load_fact(self, reference: dict[str, Any]) -> dict[str, Any]:
        parsed = validate_fact_ref(reference)
        facts = self.list_facts(parsed["campaign_id"], parsed["dimension"])
        for fact in facts:
            if fact["sequence"] == parsed["sequence"]:
                if fact["_sha256"] != parsed["sha256"]:
                    raise ControlError("事实引用摘要不匹配")
                return fact
        raise ControlError(f"事实引用不存在：{parsed}")

    def append_fact(
        self,
        campaign_id: str,
        fact_kind: str,
        payload: dict[str, Any],
        issued_at_utc: str,
        *,
        idempotency_key: str | None = None,
        producer_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """通过类型门禁追加事实，返回事实引用；不存在绕过门禁的裸追加入口。

        带幂等键时语义见 ``append_fact_outcome``；同键同内容返回的是原事实的引用。
        """

        return self.append_fact_outcome(
            campaign_id,
            fact_kind,
            payload,
            issued_at_utc,
            idempotency_key=idempotency_key,
            producer_run=producer_run,
        )["fact_ref"]

    def append_fact_outcome(
        self,
        campaign_id: str,
        fact_kind: str,
        payload: dict[str, Any],
        issued_at_utc: str,
        *,
        idempotency_key: str | None = None,
        producer_run: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加事实并返回 ``{"fact_ref": 引用, "reused": 是否复用原事实}``。

        Campaign v2 的事实写成 fact v2：先核对当前受管代码匹配有效工具身份，再以当前形成期坐标（有效身份引用、
        当前候选 revision、当前评估基线）作为 ``formed_under``。v1 Campaign 的事实不带幂等键与 producer_run，行为不变。

        幂等（Claude 升级 B1-13，动作结果 WAL 第 ① 步）：维度锁内先按幂等键查本 Campaign 的事实——
        * 同键、同种类、同 payload 摘要：返回原事实引用并 ``reused: true``，不新增事实。只比 payload，不比
          ``formed_under``、``issued_at_utc``、``producer_run``：工具演进登记后或在 N+1 重派的新 run 里重试，仍是同一次
          逻辑写入，原事实保留其形成期坐标与原 producer_run（对账器据此认出是哪个 run 写下的）。
        * 同键异内容（种类或 payload 不同，含其它维度已用此键）：拒绝。
        * 无键：照常追加。
        写入时有效性先于查链：当前受管代码与有效工具身份不一致（未登记演进）时，重试同样被拒，先登记演进。
        """

        from .gates import WorkflowGates

        campaign = self.load_campaign(campaign_id)
        dimension = WorkflowGates.dimension_for(fact_kind)
        expects_v2 = campaign_is_v2(campaign)
        if not expects_v2 and (idempotency_key is not None or producer_run is not None):
            raise ControlError("v1 Campaign 的事实不支持 idempotency_key／producer_run")
        validate_idempotency_key(idempotency_key)
        validate_producer_run(producer_run)
        lock_name = f"fact-{campaign_id}-{dimension}"
        with self._lock(lock_name):
            formed_under: dict[str, Any] | None = None
            if expects_v2:
                from .receipts import current_formed_under

                formed_under = current_formed_under(self, campaign)
            if idempotency_key is not None:
                existing = self.find_fact_by_idempotency_key(campaign_id, idempotency_key)
                if existing is not None:
                    self._require_same_logical_write(existing, fact_kind, payload)
                    return {"fact_ref": self.fact_ref(existing), "reused": True}
            chain = self.list_facts(campaign_id, dimension)
            sequence = len(chain) + 1
            previous = chain[-1]["_sha256"] if chain else None
            document = {
                "schema_version": FACT_SCHEMA,
                "campaign_id": campaign_id,
                "dimension": dimension,
                "fact_kind": fact_kind,
                "sequence": sequence,
                "previous_fact_sha256": previous,
                "issued_at_utc": issued_at_utc,
                "payload": payload,
            }
            if expects_v2:
                document["schema_version"] = FACT_V2_SCHEMA
                document["formed_under"] = formed_under
                document["idempotency_key"] = idempotency_key
                document["producer_run"] = producer_run
            validate_fact_document(document)
            for reference in iter_object_refs(payload):
                self.load_object(reference)
            for reference in iter_fact_refs(payload):
                # 消费端核验（B1-14）：跨 Campaign 的事实引用必须获授权；v2 在解引用前核验，v1 保持原顺序与原文案。
                if expects_v2 and reference["campaign_id"] != campaign_id:
                    self._authorize_cross_campaign(
                        campaign, reference, reference["campaign_id"], "事实不得跨 Campaign 引用"
                    )
                referenced = self.load_fact(reference)
                if referenced["campaign_id"] != campaign_id:
                    self._authorize_cross_campaign(
                        campaign, reference, referenced["campaign_id"], "事实不得跨 Campaign 引用"
                    )
            for reference in iter_receipt_refs(payload):
                self.load_receipt(reference)
            WorkflowGates(self).validate_append(campaign, document)
            content = canonical_json_bytes(document)
            digest = sha256_bytes(content)
            path = self._dimension_path(campaign_id, dimension) / (
                f"{sequence:08d}-{digest}.json"
            )
            write_once(path, content)
        stored = dict(document)
        stored["_sha256"] = digest
        return {"fact_ref": self.fact_ref(stored), "reused": False}

    def _authorize_cross_campaign(
        self,
        campaign: dict[str, Any],
        reference: dict[str, Any],
        owner_campaign_id: str,
        label: str,
    ) -> None:
        from .receipts import authorize_cross_campaign_reference

        authorize_cross_campaign_reference(
            self, campaign, reference, owner_campaign_id=owner_campaign_id, label=label
        )

    @staticmethod
    def _require_same_logical_write(
        existing: dict[str, Any], fact_kind: str, payload: dict[str, Any]
    ) -> None:
        if existing["fact_kind"] != fact_kind or canonical_sha256(
            existing["payload"]
        ) != canonical_sha256(payload):
            raise ControlError(
                f"幂等键 {existing['idempotency_key']} 已写入内容不同的事实"
                f"（{existing['dimension']}#{existing['sequence']} {existing['fact_kind']}）：同键异内容拒绝"
            )

    def find_fact_by_idempotency_key(
        self, campaign_id: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        """按幂等键在 Campaign 全部维度查找事实（只读、不加锁、不核当前工具身份）；没有返回 None。

        键在 Campaign 内唯一（写入时查链保证，重放复核）；查到多条说明 Store 已不一致，失败关闭。v1 Campaign 的
        事实没有幂等键，查询即拒绝，避免对账器把「查不到」误判成「动作未执行」。
        """

        campaign = self.load_campaign(campaign_id)
        if not campaign_is_v2(campaign):
            raise ControlError("v1 Campaign 的事实没有幂等键：fact-lookup 只用于 Campaign v2")
        if validate_idempotency_key(idempotency_key, "idempotency_key") is None:
            raise ControlError("fact-lookup 需要幂等键")
        matches = [
            fact
            for fact in self.list_facts(campaign_id)
            if fact["idempotency_key"] == idempotency_key
        ]
        if len(matches) > 1:
            raise ControlError(f"幂等键在多条事实上重复，Store 已不一致：{idempotency_key}")
        return matches[0] if matches else None

    def fact_lookup(self, campaign_id: str, idempotency_key: str) -> dict[str, Any]:
        """``fact-lookup`` 的结果（合同 ``official-client-control-fact-lookup/v1``），供对账器按幂等键补账。

        ``found`` 为 false 时其余事实字段为 null；为 true 时给出事实引用、种类、payload 摘要、签发时间、形成期坐标与
        写下它的 producer_run。查不到即「动作未写 Store」，查到而账本没有即「Store 已写、账本未记」。
        """

        fact = self.find_fact_by_idempotency_key(campaign_id, idempotency_key)
        return {
            "schema_version": FACT_LOOKUP_SCHEMA,
            "campaign_id": campaign_id,
            "idempotency_key": idempotency_key,
            "found": fact is not None,
            "fact_ref": self.fact_ref(fact) if fact is not None else None,
            "fact_kind": fact["fact_kind"] if fact is not None else None,
            "payload_sha256": canonical_sha256(fact["payload"]) if fact is not None else None,
            "issued_at_utc": fact["issued_at_utc"] if fact is not None else None,
            "formed_under": fact["formed_under"] if fact is not None else None,
            "producer_run": fact["producer_run"] if fact is not None else None,
        }

    def write_receipt(
        self, kind: str, receipt: dict[str, Any], *, allow_existing: bool = False
    ) -> dict[str, Any]:
        """写入不可覆盖收据并返回引用；``allow_existing=True`` 时同内容收据已存在即返回原引用。"""

        return self.write_receipt_outcome(kind, receipt, allow_existing=allow_existing)[
            "receipt_ref"
        ]

    def write_receipt_outcome(
        self, kind: str, receipt: dict[str, Any], *, allow_existing: bool = False
    ) -> dict[str, Any]:
        """写入收据，返回 ``{"receipt_ref": 引用, "reused": 是否复用}``。

        收据合同版本必须与所属 Campaign 一致，v2 收据的形成期坐标必须是当前值。``allow_existing``（B1-13）：同内容
        收据已存在时直接返回原引用，不再核对「当前值」——收据是确定性重建的结果，同内容即同一次写入；改了任何
        字段（含形成期坐标）的收据内容不同，仍按新写入核对、旧结果冒充当前写入照样被拒。
        """

        validate_receipt_document(receipt, kind)
        from . import delivery_execution
        if kind == "candidate_delivery" and receipt.get("schema_version") == delivery_execution.RECEIPT_V4:
            with self._dimension_locks(receipt["campaign_id"], ("deployment",)):
                return self._write_receipt_outcome_locked(kind, receipt, allow_existing=allow_existing)
        return self._write_receipt_outcome_locked(kind, receipt, allow_existing=allow_existing)

    def _write_receipt_outcome_locked(
        self, kind: str, receipt: dict[str, Any], *, allow_existing: bool
    ) -> dict[str, Any]:
        """v4 调用方已持 deployment 锁，其余合同沿用原发布顺序。"""

        # v4 直接发布与幂等复用也重验来源和全操作闭集；不只接受自摘要正确的收据。
        from . import delivery_execution
        if kind == "candidate_delivery" and receipt.get("schema_version") == delivery_execution.RECEIPT_V4:
            delivery_execution.validate_delivery_receipt(self, receipt)
        content = canonical_json_bytes(receipt)
        digest = sha256_bytes(content)
        reference = {"receipt_kind": kind, "sha256": digest}
        path = self.receipt_path(reference)
        if allow_existing and self._holds_identical(path, content):
            self.load_receipt(reference)
            return {"receipt_ref": reference, "reused": True}
        for referenced in iter_object_refs(receipt):
            self.load_object(referenced)
        for referenced in iter_fact_refs(receipt):
            self.load_fact(referenced)
        for referenced in iter_receipt_refs(receipt):
            self.load_receipt(referenced)
        from .receipts import require_receipt_current

        require_receipt_current(self, receipt)
        if kind == "candidate_delivery" and receipt.get("schema_version") == delivery_execution.RECEIPT_V4:
            for existing_reference in self.list_receipt_refs("candidate_delivery"):
                existing = self.load_receipt(existing_reference)
                if existing["candidate_ref"] == receipt["candidate_ref"] and existing["acceptance_ref"] == receipt["acceptance_ref"] and existing_reference != reference:
                    raise ControlError("同一 Candidate／Acceptance 的候选交付收据不得覆盖或重新签发")
        reused = self._write_once_or_reuse(
            path, content, f"receipt-{kind}-{digest}", allow_existing=allow_existing
        )
        return {"receipt_ref": reference, "reused": reused}

    def load_receipt(self, reference: dict[str, Any]) -> dict[str, Any]:
        parsed = validate_receipt_ref(reference)
        path = self.receipt_path(parsed)
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"收据引用不存在：{parsed}")
        self._verify_mode(path, self._file_modes, "收据")
        content = path.read_bytes()
        if sha256_bytes(content) != parsed["sha256"]:
            raise ControlError(f"收据摘要漂移：{path}")
        receipt = load_json_file(path, "收据")
        validate_receipt_document(receipt, parsed["receipt_kind"])
        return receipt

    def list_receipt_refs(self, kind: str) -> list[dict[str, Any]]:
        if kind not in RECEIPT_KINDS:
            raise ControlError(f"未知收据类型：{kind}")
        directory = self.root / "receipts" / kind
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise ControlError(f"收据目录不可信：{directory}")
        references: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or not path.is_file() or not re.fullmatch(
                r"[0-9a-f]{64}\.json", path.name
            ):
                raise ControlError(f"收据路径非法：{path}")
            reference = {"receipt_kind": kind, "sha256": path.stem}
            self.load_receipt(reference)
            references.append(reference)
        return references

    def _verify_external_binding(self, external_root: Path, binding: dict[str, Any]) -> None:
        parsed = validate_external_binding(binding, "external_binding")
        path = resolve_relative(external_root, parsed["path"])
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"外部证据不存在或不可信：{parsed['path']}")
        if path.stat().st_size != parsed["bytes"] or sha256_file(path) != parsed["sha256"]:
            raise ControlError(f"外部证据摘要漂移：{parsed['path']}")

    def _iter_external_bindings(self, value: Any) -> Iterator[dict[str, Any]]:
        if isinstance(value, dict):
            if set(value) == {"path", "sha256", "bytes"}:
                yield validate_external_binding(value, "external_binding")
                return
            for item in value.values():
                yield from self._iter_external_bindings(item)
        elif isinstance(value, list):
            for item in value:
                yield from self._iter_external_bindings(item)

    def replay(
        self,
        *,
        external_root: Path | None = None,
        require_external: bool = False,
    ) -> dict[str, Any]:
        """独立复算 Store、对象图、事实链、收据和可选外部证据。

        前序导入（B1-14）复制进来的对象：只有被 read_only_reuse 导入收据授权的，其外部证据才计入本 Store 的外部
        核验（本 Campaign 依赖它们）；只作回归夹具或尚未 finalize 的导入对象，外部证据随前序 Store 自己的重放核验。
        """

        from .gates import WorkflowGates
        from .receipts import fixture_only_imported_objects, replay_receipt

        metadata = load_json_file(self.root / "store.json", "Store metadata")
        validate_store_metadata(metadata)
        object_count = 0
        external_bindings: list[dict[str, Any]] = []
        unbound_imports = fixture_only_imported_objects(self)
        for kind in sorted(OBJECT_KINDS):
            directory = self.root / "objects" / kind
            if not directory.exists():
                continue
            if directory.is_symlink() or not directory.is_dir():
                raise ControlError(f"对象目录不可信：{directory}")
            for path in sorted(directory.iterdir()):
                if path.is_symlink() or not path.is_file() or not re.fullmatch(
                    r"[0-9a-f]{64}\.json", path.name
                ):
                    raise ControlError(f"对象路径非法：{path}")
                digest = path.stem
                document = self.load_object({"object_kind": kind, "sha256": digest})
                for reference in iter_object_refs(document["payload"]):
                    self.load_object(reference)
                WorkflowGates(self).validate_object_graph(
                    {"object_kind": kind, "sha256": digest}
                )
                if (kind, digest) not in unbound_imports:
                    external_bindings.extend(self._iter_external_bindings(document["payload"]))
                object_count += 1

        campaign_count = 0
        fact_count = 0
        campaigns_dir = self.root / "campaigns"
        for path in sorted(campaigns_dir.iterdir()):
            if path.is_symlink() or not path.is_file() or path.suffix != ".json":
                raise ControlError(f"Campaign 路径非法：{path}")
            campaign = self.load_campaign(path.stem)
            external_bindings.extend(self._iter_external_bindings(campaign))
            facts = self.list_facts(campaign["campaign_id"])
            WorkflowGates(self).replay_campaign(campaign, facts)
            fact_count += len(facts)
            campaign_count += 1

        receipt_count = 0
        receipts_dir = self.root / "receipts"
        for kind in RECEIPT_KINDS:
            directory = receipts_dir / kind
            if not directory.exists():
                continue
            for path in sorted(directory.iterdir()):
                if path.is_symlink() or not path.is_file() or not re.fullmatch(
                    r"[0-9a-f]{64}\.json", path.name
                ):
                    raise ControlError(f"收据路径非法：{path}")
                reference = {"receipt_kind": kind, "sha256": path.stem}
                replay_receipt(self, reference)
                external_bindings.extend(
                    self._iter_external_bindings(self.load_receipt(reference))
                )
                receipt_count += 1

        if external_bindings and external_root is None and require_external:
            raise ControlError("完整重放要求提供 external_root")
        if external_root is not None:
            if not external_root.is_absolute() or external_root.is_symlink() or not external_root.is_dir():
                raise ControlError("external_root 必须是可信绝对目录")
            for binding in external_bindings:
                self._verify_external_binding(external_root.resolve(), binding)

        return {
            "schema_version": "official-client-control-replay-result/v1",
            "result": "passed",
            "objects": object_count,
            "campaigns": campaign_count,
            "facts": fact_count,
            "receipts": receipt_count,
            "external_bindings": len(external_bindings),
            "external_verified": external_root is not None,
        }
