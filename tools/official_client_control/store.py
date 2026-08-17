"""内容寻址、只写追加的 FW-D Store 与正交事实账本。"""

from __future__ import annotations

import fcntl
import os
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .canonical import (
    canonical_json_bytes,
    ensure_directory,
    expect_rfc3339,
    load_json_file,
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
    FACT_SCHEMA,
    OBJECT_KINDS,
    OBJECT_SCHEMA,
    STORE_SCHEMA,
    iter_fact_refs,
    iter_object_refs,
    iter_receipt_refs,
    validate_campaign,
    validate_fact_document,
    validate_fact_ref,
    validate_object_document,
    validate_object_ref,
    validate_receipt_document,
    validate_receipt_ref,
    validate_store_metadata,
)
from .errors import ControlError


_FACT_FILE_RE = re.compile(r"^(\d{8})-([0-9a-f]{64})\.json$")


class ControlStore:
    """Persona 无关、内容寻址且可完整重放的控制面存储。"""

    def __init__(self, root: Path) -> None:
        if not root.is_absolute():
            raise ControlError("Store 根必须是绝对路径")
        if root.is_symlink() or not root.is_dir():
            raise ControlError("Store 根必须是现有的非符号链接目录")
        self.root = root.resolve()
        verify_mode(self.root, 0o700, "Store 根")
        metadata_path = self.root / "store.json"
        metadata = load_json_file(metadata_path, "Store metadata")
        validate_store_metadata(metadata)
        verify_mode(metadata_path, 0o600, "Store metadata")

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
        lock_path = self.root / "locks" / f"{name}.lock"
        ensure_directory(lock_path.parent)
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def object_path(self, reference: dict[str, Any]) -> Path:
        parsed = validate_object_ref(reference)
        return self.root / "objects" / parsed["object_kind"] / f"{parsed['sha256']}.json"

    def receipt_path(self, reference: dict[str, Any]) -> Path:
        parsed = validate_receipt_ref(reference)
        return self.root / "receipts" / parsed["receipt_kind"] / f"{parsed['sha256']}.json"

    def seal_object(self, object_kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        """校验并只写一次封存对象；同摘要再次写入也按覆盖拒绝。"""

        if object_kind not in OBJECT_KINDS:
            raise ControlError(f"未登记 object_kind：{object_kind}")
        document = {
            "schema_version": OBJECT_SCHEMA,
            "object_kind": object_kind,
            "payload": payload,
        }
        validate_object_document(document)
        for reference in iter_object_refs(payload):
            self.load_object(reference)
        from .gates import WorkflowGates

        WorkflowGates(self).validate_new_object(document)
        content = canonical_json_bytes(document)
        digest = sha256_bytes(content)
        reference = {"object_kind": object_kind, "sha256": digest}
        path = self.object_path(reference)
        with self._lock(f"object-{object_kind}-{digest}"):
            write_once(path, content)
        return reference

    def load_object(self, reference: dict[str, Any]) -> dict[str, Any]:
        parsed = validate_object_ref(reference)
        path = self.object_path(parsed)
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"对象引用不存在：{parsed}")
        verify_mode(path, 0o600, "对象")
        content = path.read_bytes()
        if sha256_bytes(content) != parsed["sha256"]:
            raise ControlError(f"对象摘要漂移：{path}")
        document = load_json_file(path, "对象")
        validate_object_document(document)
        if document["object_kind"] != parsed["object_kind"]:
            raise ControlError(f"对象类型与路径不一致：{path}")
        return document

    def create_campaign(self, campaign: dict[str, Any]) -> dict[str, Any]:
        validate_campaign(campaign)
        self.load_object(campaign["bootstrap_ref"])
        campaign_id = campaign["campaign_id"]
        path = self.root / "campaigns" / f"{campaign_id}.json"
        with self._lock(f"campaign-{campaign_id}"):
            write_once(path, canonical_json_bytes(campaign))
        return {
            "campaign_id": campaign_id,
            "identity_sha256": campaign["identity_sha256"],
        }

    def load_campaign(self, campaign_id: str) -> dict[str, Any]:
        if not isinstance(campaign_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}", campaign_id
        ):
            raise ControlError("campaign_id 非法")
        path = self.root / "campaigns" / f"{campaign_id}.json"
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"Campaign 不存在：{campaign_id}")
        verify_mode(path, 0o600, "Campaign")
        campaign = load_json_file(path, "Campaign")
        validate_campaign(campaign)
        if campaign["campaign_id"] != campaign_id:
            raise ControlError("Campaign ID 与路径不一致")
        return campaign

    def _dimension_path(self, campaign_id: str, dimension: str) -> Path:
        self.load_campaign(campaign_id)
        if dimension not in DIMENSIONS:
            raise ControlError(f"事实维度非法：{dimension}")
        return self.root / "facts" / campaign_id / dimension

    def list_facts(self, campaign_id: str, dimension: str | None = None) -> list[dict[str, Any]]:
        dimensions = [dimension] if dimension is not None else list(DIMENSIONS)
        facts: list[dict[str, Any]] = []
        for current_dimension in dimensions:
            path = self._dimension_path(campaign_id, current_dimension)
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
                verify_mode(fact_path, 0o600, "事实")
                content = fact_path.read_bytes()
                if sha256_bytes(content) != digest:
                    raise ControlError(f"事实摘要漂移：{fact_path}")
                document = load_json_file(fact_path, "事实")
                validate_fact_document(document)
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
    ) -> dict[str, Any]:
        """通过类型门禁追加事实；不存在绕过门禁的裸追加入口。"""

        from .gates import WorkflowGates

        campaign = self.load_campaign(campaign_id)
        dimension = WorkflowGates.dimension_for(fact_kind)
        lock_name = f"fact-{campaign_id}-{dimension}"
        with self._lock(lock_name):
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
            validate_fact_document(document)
            for reference in iter_object_refs(payload):
                self.load_object(reference)
            for reference in iter_fact_refs(payload):
                referenced = self.load_fact(reference)
                if referenced["campaign_id"] != campaign_id:
                    raise ControlError("事实不得跨 Campaign 引用")
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
        return self.fact_ref(stored)

    def write_receipt(self, kind: str, receipt: dict[str, Any]) -> dict[str, Any]:
        validate_receipt_document(receipt, kind)
        for reference in iter_object_refs(receipt):
            self.load_object(reference)
        for reference in iter_fact_refs(receipt):
            self.load_fact(reference)
        for reference in iter_receipt_refs(receipt):
            self.load_receipt(reference)
        content = canonical_json_bytes(receipt)
        digest = sha256_bytes(content)
        reference = {"receipt_kind": kind, "sha256": digest}
        with self._lock(f"receipt-{kind}-{digest}"):
            write_once(self.receipt_path(reference), content)
        return reference

    def load_receipt(self, reference: dict[str, Any]) -> dict[str, Any]:
        parsed = validate_receipt_ref(reference)
        path = self.receipt_path(parsed)
        if path.is_symlink() or not path.is_file():
            raise ControlError(f"收据引用不存在：{parsed}")
        verify_mode(path, 0o600, "收据")
        content = path.read_bytes()
        if sha256_bytes(content) != parsed["sha256"]:
            raise ControlError(f"收据摘要漂移：{path}")
        receipt = load_json_file(path, "收据")
        validate_receipt_document(receipt, parsed["receipt_kind"])
        return receipt

    def list_receipt_refs(self, kind: str) -> list[dict[str, Any]]:
        if kind not in {"promotion", "activation"}:
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
        """独立复算 Store、对象图、事实链、收据和可选外部证据。"""

        from .gates import WorkflowGates
        from .receipts import replay_receipt

        metadata = load_json_file(self.root / "store.json", "Store metadata")
        validate_store_metadata(metadata)
        object_count = 0
        external_bindings: list[dict[str, Any]] = []
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
        for kind in ("promotion", "activation"):
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
