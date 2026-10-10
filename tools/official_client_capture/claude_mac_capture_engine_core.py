"""独立捕获的中性字节核心：有界转发、原件 WAL、关闭与不可变导出。

本模块不选择用途、不授来源权、不登记适配器。两个静态薄壳各自先验任务、
安装与预算合同，之后复用同一份网络算法；核心不从JSON加载策略或命令。
"""

from __future__ import annotations

import argparse
import base64
import fcntl
import hashlib
import hmac
import os
import secrets
import select
import signal
import socket
import ssl
import stat
import threading
import time
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_capture_wire as wire


def _mac(key: bytes, value: Any) -> str:
    return hmac.new(key, c.canonical(value), hashlib.sha256).hexdigest()


def receive(connection: socket.socket) -> bytes:
    raw = bytearray()
    while not raw.endswith(b"\n"):
        block = connection.recv(min(65536, c.MAX_RPC_BYTES + 1 - len(raw)))
        c.require(bool(block), "采集控制通道提前关闭")
        raw.extend(block)
        c.require(len(raw) <= c.MAX_RPC_BYTES, "采集 RPC 超过大小上界")
    return bytes(raw)


def _endpoint(value: tuple[Any, ...]) -> dict[str, Any]:
    return {"address": value[0], "port": value[1]}


class RawCaptureCore:
    """固定任务的持久服务；旧 producer 未决时不会重新监听或重启产品。"""

    _policy = None

    def __init__(self, configuration: Mapping[str, Any]):
        c.require(self._policy is not None, "字节核心不能直接构造或自行选择用途")
        self.configuration = self._policy.validate_configuration(dict(configuration))
        self.versions = self._policy.contract_versions(self.configuration)
        self.task = self._policy.verify_task_configuration(self.configuration)
        self.task_sha = self.task["task_sha256"]
        self.data_root = Path(self.configuration["data_root"])
        self.root = c.private_directory(c.managed_path(self.data_root, self.configuration["capture_relative"]))
        self.socket_path = c.managed_path(self.data_root, self.configuration["control_socket_relative"])
        c.private_directory(self.socket_path.parent)
        self.key = c.read_bytes(c.check_binding(self.data_root, self.configuration["channel_key_binding"]))
        self._lock_descriptor = os.open(self.socket_path.parent / "capture-owner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(self._lock_descriptor)
            c.require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid() and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600, "采集所有权锁类型、所有者或权限非法")
            fcntl.flock(self._lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException as error:
            os.close(self._lock_descriptor)
            if isinstance(error, c.CaptureError):
                raise
            raise c.CaptureError("相同采集根已有独立服务持锁") from error
        self._lock = threading.RLock()
        self._operation_lock = threading.RLock()
        self._stop_accept = threading.Event()
        self.stop_event = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._workers: dict[str, ConnectionWorker] = {}
        self._connections: dict[str, dict[str, Any]] = {}
        self._parsers: dict[str, wire.H1Parser] = {}
        self._events: list[dict[str, Any]] = []
        self._reasons: list[str] = []
        self._wire_bytes = 0
        self._next_connection = 0
        self._phase = "initial"
        self._inventory: dict[str, Any] | None = None
        self._closed = False
        self._dependencies = None
        self._seen_headers: set[tuple[Any, ...]] = set()
        try:
            if self.versions["version"] == 2:
                from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
                self._policy.verify_protocol_runtime(self.configuration)
                c.write_once(self.root / "runtime-environment.json", c.read_bytes(
                    c.check_binding(self.data_root, self.configuration["runtime_environment_binding"])))
                c.write_json_once(self.root / "native-configuration.json", self.configuration)
                self._dependencies = self._policy.prepare_protocol_snapshot(self.configuration, self.root)
            from tools.official_client_capture.claude_upgrade_delivery_recovery_guard import process_identity
            owner = {"configuration_sha256": c.digest(self.configuration), "task_sha256": self.task_sha, "source_scope": self._policy.SCOPE, "runtime_bindings": self._policy.runtime_bindings(self.configuration), "process": process_identity(os.getpid()), "started_at_utc": c.utc_now()}
            existing = self.root / "daemon-owner.json"
            if existing.exists():
                previous = c.read_json(existing)
                c.require(all(previous[name] == owner[name] for name in ("configuration_sha256", "task_sha256", "source_scope", "runtime_bindings")), "旧采集配置、任务或有效运行代码已漂移")
            else:
                c.write_json_once(existing, owner)
            self._load_journal()
            self._recover_startup()
        except BaseException:
            self.dispose()
            raise

    def _check_task(self, task_sha256: Any) -> None:
        c.require(task_sha256 == self.task_sha, "采集操作未绑定固定任务")

    def _budget(self, receipt: Mapping[str, Any]) -> None:
        self._policy.verify_budget(self.task, receipt)

    def _load_journal(self) -> None:
        path = self.root / "journal.jsonl"
        if not path.exists():
            return
        raw = c.read_bytes(path)
        c.require(not raw or raw.endswith(b"\n"), "采集 WAL 裂尾；保留原件与预算未决")
        previous = "0" * 64
        for index, line in enumerate(raw.splitlines(), 1):
            fields = {"sequence", "previous_sha256", "event", "at_utc", "data", "sha256"}
            if self.versions["version"] == 2:
                fields.add("schema_version")
            event = c.exact(c.parse_json(line), fields, "采集 WAL")
            if self.versions["version"] == 2:
                c.require(event["schema_version"] == "claude-upgrade-native-wal/v2", "WAL 与形成期版本不符")
            core = {name: value for name, value in event.items() if name != "sha256"}
            c.require(type(event["sequence"]) is int and event["sequence"] == index and event["previous_sha256"] == previous and event["sha256"] == c.digest(core), "采集 WAL 序号或摘要链漂移")
            events = {"reserved", "ready", "connection_opened", "segment", "request_header", "eof", "connection_closed", "incomplete", "sealing"}
            if self.versions["version"] == 2:
                events |= {"protocol_selected", "transport_validated"}
            c.require(isinstance(event["data"], dict) and event["event"] in events, "采集 WAL 事件不在固定合同内")
            c.timestamp(event["at_utc"])
            self._events.append(event)
            previous = event["sha256"]

    def _journal(self, name: str, data: Mapping[str, Any], *, at_utc: str | None = None) -> None:
        with self._lock:
            c.require(self._inventory is None, "已提交采集不能再追加原件")
            core = {"sequence": len(self._events) + 1, "previous_sha256": self._events[-1]["sha256"] if self._events else "0" * 64, "event": name, "at_utc": at_utc or c.utc_now(), "data": dict(data)}
            if self.versions["version"] == 2:
                core["schema_version"] = "claude-upgrade-native-wal/v2"
            event = {**core, "sha256": c.digest(core)}
            path = self.root / "journal.jsonl"
            descriptor = os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(c.canonical(event) + b"\n")
                    handle.flush()
                    os.fsync(descriptor)
                c.fsync_directory(self.root)
            finally:
                os.close(descriptor)
            self._events.append(event)

    def mark_incomplete(self, reason: str) -> None:
        with self._lock:
            if reason not in self._reasons:
                self._reasons.append(reason)
                self._journal("incomplete", {"reason": reason})

    def _recover_startup(self) -> None:
        if (self.root / "native-capture-manifest.json").exists():
            self._phase = "committed"
            self._publish_inventory()
            return
        if not (self.root / "reservation.json").exists():
            return
        # 旧进程消失后只重读原件，不能重建 ByteRecorder 或续开同一 producer。
        self._phase = "recovering"
        if self.versions["version"] == 2:
            from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
            self._parsers, self._connections, sizes, tails = h2_contract.replay_events(
                self.root, self.configuration, self._events, self._dependencies,
                parser_factory=self._policy.create_parser)
            self._next_connection = len(self._connections)
            self._wire_bytes = sum(sizes.values())
            for event in self._events:
                if event["event"] == "incomplete" and event["data"]["reason"] not in self._reasons:
                    self._reasons.append(event["data"]["reason"])
            if tails:
                self._reasons.append("raw 原件存在 WAL 未确认尾段")
            self.mark_incomplete("旧 capture 缺已提交终态，重启只收口为 truncated")
            self._seal_native("truncated")
            return
        expected_sizes: dict[str, int] = {}
        for event in self._events:
            data, at = event["data"], event["at_utc"]
            if event["event"] == "connection_opened":
                connection_id = data["connection_id"]
                self._connections[connection_id] = dict(data)
                self._parsers[connection_id] = wire.H1Parser(connection_id, self.configuration["limits"])
                self._next_connection += 1
            elif event["event"] == "segment":
                path = c.managed_path(self.root, data["path"])
                raw = c.read_bytes(path)
                offset, amount = data["offset"], data["bytes"]
                c.require(type(offset) is int and type(amount) is int and offset == expected_sizes.get(data["path"], 0) and amount > 0, "恢复 raw offset 不闭合")
                part = raw[offset:offset + amount]
                c.require(len(part) == amount and hashlib.sha256(part).hexdigest() == data["sha256"], "恢复 raw 原件与 WAL 不符")
                expected_sizes[data["path"]] = offset + amount
                self._wire_bytes += amount
                parser = self._parsers[data["connection_id"]]
                if not parser.errors:
                    try:
                        parser.feed(data["direction"], part, at)
                    except c.CaptureError:
                        pass
            elif event["event"] == "eof":
                try:
                    self._parsers[data["connection_id"]].eof(data["direction"])
                except c.CaptureError:
                    pass
            elif event["event"] == "connection_closed":
                self._connections[data["connection_id"]] = dict(data)
            elif event["event"] == "incomplete":
                if data["reason"] not in self._reasons:
                    self._reasons.append(data["reason"])
        for relative, amount in expected_sizes.items():
            if len(c.read_bytes(c.managed_path(self.root, relative))) != amount:
                self._reasons.append("raw 原件存在 WAL 未确认尾段")
        # 即使全部已知 segment 可解析，也不能补出 SIGKILL 后丢失的 EOF 或关闭时间。
        self.mark_incomplete("旧 capture 缺已提交终态，重启只收口为 truncated")
        self._seal_native("truncated")

    def reserve(self, task: Mapping[str, Any], hold: Mapping[str, Any]) -> dict[str, Any]:
        with self._operation_lock:
            c.require(c.canonical(dict(task)) == c.canonical(self.task), "reserve 必须引用固定完整 task")
            c.require(self._phase in {"initial", "reserved", "ready"}, "已关闭或恢复的 producer 不能重新预约")
            self._budget(hold)
            c.write_json_once(self.root / "budget-reservation.json", dict(hold))
            path = self.root / "reservation.json"
            if path.exists():
                reservation = c.read_json(path)
                c.require(reservation["budget_reservation"] == c.binding(self.root, self.root / "budget-reservation.json"), "旧预约预算原件漂移")
                return reservation
            reservation = {"schema_version": "claude-upgrade-client-reservation/v2", "task_sha256": self.task_sha, "candidate": self.task["candidate"], **{name: self.task["execution"][name] for name in ("capture_id", "producer_run_id", "endpoint", "request_budget")}, "reserved_at_utc": c.utc_now(), "deadline_utc": self.task["execution"]["request_policy"]["deadline_utc"], "budget_reservation": c.binding(self.root, self.root / "budget-reservation.json"), "source_deployments": self.task["execution"]["source_deployments"]}
            c.write_json_once(path, reservation)
            self._journal("reserved", {"reservation": c.binding(self.root, path)})
            self._phase = "reserved"
            return reservation

    def wait_ready(self, task_sha256: str) -> dict[str, Any]:
        with self._operation_lock:
            self._check_task(task_sha256)
            c.require(self._phase in {"reserved", "ready"}, "未预约或已收口的 capture 不能 ready")
            self._budget(c.read_json(self.root / "budget-reservation.json"))
            if self.versions["version"] == 2:
                self._policy.verify_protocol_runtime(self.configuration)
                self._dependencies.revalidate()
                self._dependencies.new_hpack_decoder(self.configuration["limits"]["max_header_bytes"])
                c.write_json_once(self.root / "ready-environment.json", {
                    "configuration_sha256": c.digest(self.configuration),
                    "runtime_environment": c.binding(self.root, self.root / "runtime-environment.json"),
                    "dependencies": self._dependencies.receipt()})
            if self._phase == "ready":
                c.require(self._listener is not None and not self._stop_accept.is_set(), "旧 ready listener 已失效")
                return c.read_json(self.root / "ready.json")
            endpoint = self.configuration["listener"]
            listener = socket.socket(socket.AF_INET6 if ":" in endpoint["host"] else socket.AF_INET, socket.SOCK_STREAM)
            listener.settimeout(0.1)
            try:
                listener.bind((endpoint["host"], endpoint["port"]))
                listener.listen(self.configuration["limits"]["max_connections"])
                c.require(_endpoint(listener.getsockname()) == {"address": endpoint["host"], "port": endpoint["port"]}, "listener 实际地址与配置不符")
                ready_core = {"schema_version": "claude-upgrade-client-capture-ready/v2", "task_sha256": self.task_sha, "capture_id": self.task["execution"]["capture_id"], "reservation": c.binding(self.root, self.root / "reservation.json"), "deployment": self.configuration["deployment"], "ready_at_utc": c.utc_now()}
                c.write_json_once(self.root / "ready-observation.json", ready_core)
                ready = {**ready_core, "observation": c.binding(self.root, self.root / "ready-observation.json")}
                c.write_json_once(self.root / "ready.json", ready)
                self._journal("ready", {"ready": c.binding(self.root, self.root / "ready.json"), "listener": _endpoint(listener.getsockname())})
                self._listener, self._phase = listener, "ready"
                self._accept_thread = threading.Thread(target=self._accept_loop, name="claude-native-accept", daemon=True)
                self._accept_thread.start()
                return ready
            except BaseException:
                listener.close()
                self.mark_incomplete("固定 listener 准备失败，未发布可用 ready")
                raise

    def _accept_loop(self) -> None:
        while not self._stop_accept.is_set():
            try:
                connection, _address = self._listener.accept()
            except socket.timeout:
                continue
            except OSError:
                if not self._stop_accept.is_set():
                    self.mark_incomplete("listener 非预期关闭")
                return
            with self._lock:
                self._next_connection += 1
                number = self._next_connection
                connection_id = f"{self.task['execution']['capture_id']}.conn-{number:06d}"
                row = {"connection_id": connection_id, "client_endpoint": _endpoint(connection.getpeername()), "server_endpoint": _endpoint(connection.getsockname()), "opened_at_utc": c.utc_now(), "closed_at_utc": None}
                if self.versions["version"] == 2:
                    row.update(protocol=None, protocol_observation=None, transport_observation=None)
                self._connections[connection_id] = row
                self._journal("connection_opened", row, at_utc=row["opened_at_utc"])
                worker = ConnectionWorker(self, connection, connection_id, number)
                self._workers[connection_id] = worker
                self._parsers[connection_id] = worker.parser
                if number > self.configuration["limits"]["max_connections"] or sum(item.is_alive() for item in self._workers.values()) >= self.task["execution"]["request_policy"]["max_concurrency"]:
                    self.mark_incomplete("实际连接超过冻结并发或总连接上界")
                    worker.request_stop()
                worker.start()

    def bind_parser(self, worker: ConnectionWorker, protocol: str) -> None:
        """首片应用字节前同锁更新两处引用，原协议选择先于 segment WAL。"""
        from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
        with self._lock:
            c.require(worker.parser is None and self._parsers[worker.connection_id] is None and
                      all(value == 0 for value in worker.recorder.offsets.values()), "应用字节后禁止更换协议解析器")
            path = worker.directory / "protocol-observation.json"
            observation = {"protocol": protocol, "mode": self.configuration["protocol_policy"]["mode"],
                           "configuration_sha256": c.digest(self.configuration), "tls": dict(worker.tls_facts)}
            c.write_json_once(path, observation)
            binding = c.binding(self.root, path)
            parser = self._policy.create_parser(worker.connection_id, protocol, self.configuration,
                                               self._dependencies, self.on_request_header)
            self._journal("protocol_selected", {"connection_id": worker.connection_id, "protocol": protocol,
                "observation": binding, "dependency_manifest": c.binding(self.root, self.root / "h2-dependencies/production_decoder-dependencies.json"),
                "runtime_environment": c.binding(self.root, self.root / "runtime-environment.json")})
            worker.parser = self._parsers[worker.connection_id] = parser
            self._connections[worker.connection_id].update(protocol=protocol, protocol_observation=binding)

    def record_transport(self, worker: ConnectionWorker, path: Path) -> None:
        if self.versions["version"] == 2:
            with self._lock:
                binding = c.binding(self.root, path)
                self._journal("transport_validated", {"connection_id": worker.connection_id, "observation": binding})
                self._connections[worker.connection_id]["transport_observation"] = binding

    def record_segment(self, worker: ConnectionWorker, direction: str, raw: bytes) -> None:
        observed = c.utc_now()
        with self._lock:
            offset = worker.recorder.offsets[direction]
            worker.recorder.write(direction, raw)
            self._wire_bytes += len(raw)
            path = worker.directory / f"conn{worker.number:03d}.{direction}.bin"
            self._journal("segment", {"connection_id": worker.connection_id, "direction": direction, "path": path.relative_to(self.root).as_posix(), "offset": offset, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}, at_utc=observed)
            c.require(worker.parser is not None and self._parsers[worker.connection_id] is worker.parser,
                      "原生协议未选择或两处 parser 引用不一致")
            worker.parser.feed(direction, raw, observed)
            # 准入拒绝不得打断同一已持久化片段的正文解析；解析完毕仍绝不转发。
            c.require(worker.forward_error is None, worker.forward_error or "请求转发被拒绝")
            c.require(self._wire_bytes <= self.configuration["limits"]["max_wire_bytes"], "实际 wire 字节超过冻结上界")
            c.require(self._policy.timestamp(observed) < self._policy.timestamp(self.task["execution"]["request_policy"]["deadline_utc"]), "收到字节时冻结 deadline 已到")

    def on_request_header(self, row: Mapping[str, Any]) -> None:
        header = {name: row[name] for name in ("connection_id", "request_ordinal", "observed_at_utc", "host", "method", "path")}
        if self.versions["version"] == 2:
            from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
            worker = self._workers[row["connection_id"]]
            c.require(worker.parser is self._parsers[row["connection_id"]], "预算头计数 parser 引用不一致")
            key = (row["connection_id"], row.get("stream_id"), row["request_ordinal"])
            c.require(key not in self._seen_headers, "原生请求头重复计数")
            self._seen_headers.add(key)
            header = h2_contract.header_row(row, self._connections[row["connection_id"]]["protocol"])
        self._journal("request_header", header, at_utc=row["observed_at_utc"])
        models = sum(item["method"] == "POST" and item["path"].split("?", 1)[0] in wire.MODEL_PATHS for parser in self._parsers.values() if parser is not None for item in parser.requests)
        try:
            self._policy.verify_request(self.task, row)
            c.require(models <= self.task["execution"]["request_budget"], "真实模型请求超过原任务上界；仍保留已观察 POST")
            self._budget(c.read_json(self.root / "budget-reservation.json"))
        except Exception as error:
            reason = str(error) if isinstance(error, c.CaptureError) else "原预算占用重验失败：" + type(error).__name__
            self._workers[row["connection_id"]].forward_error = reason
            self.mark_incomplete(reason)

    def record_eof(self, worker: ConnectionWorker, direction: str) -> None:
        with self._lock:
            self._journal("eof", {"connection_id": worker.connection_id, "direction": direction})
            if worker.parser is not None:
                c.require(self._parsers[worker.connection_id] is worker.parser, "EOF parser 引用不一致")
                worker.parser.eof(direction)

    def record_closed(self, worker: ConnectionWorker) -> None:
        with self._lock:
            row = dict(self._connections[worker.connection_id])
            row["closed_at_utc"] = c.utc_now()
            path = worker.directory / "connection-observation.json"
            c.write_json_once(path, row)
            row["observation"] = c.binding(self.root, path)
            self._connections[worker.connection_id] = row
            self._journal("connection_closed", row, at_utc=row["closed_at_utc"])

    def close(self, task_sha256: str, reason: str = "normal") -> dict[str, Any]:
        with self._operation_lock:
            self._check_task(task_sha256)
            c.require(reason in {"normal", "failure", "deadline", "shutdown", "recover"}, "关闭原因不在固定枚举")
            if self._inventory is not None:
                return self.inventory(task_sha256)
            if (self.root / "native-capture-manifest.json").exists():
                self._publish_inventory()
                return self.inventory(task_sha256)
            c.require((self.root / "reservation.json").exists(), "没有真实预约，不能补造零收口")
            self._phase = "draining"
            self._stop_accept.set()
            if self._listener is not None:
                self._listener.close()
            if self._accept_thread:
                self._accept_thread.join(1)
                c.require(not self._accept_thread.is_alive(), "accept 尚未停止，预约保持未决")
            deadline = time.monotonic() + self.configuration["limits"]["drain_seconds"]
            while time.monotonic() < deadline:
                active = [item for item in self._workers.values() if item.is_alive()]
                if not active:
                    break
                for worker in active:
                    with self._lock:
                        if worker.prepared and not worker.parser.has_pending():
                            worker.request_stop()
                time.sleep(0.01)
            active = [item for item in self._workers.values() if item.is_alive()]
            if active:
                self.mark_incomplete("close/drain 超时，保留真实 partial 原件")
                for worker in active:
                    worker.request_stop(force=True)
            end = time.monotonic() + 2
            for worker in self._workers.values():
                worker.join(max(0, end - time.monotonic()))
            c.require(not any(item.is_alive() for item in self._workers.values()), "采集 worker 尚未停止，不能发布 immutable export")
            if reason in {"deadline", "failure"}:
                self.mark_incomplete("采集由冻结 deadline 或失败路径收口")
            complete = not self._reasons and all(row.get("closed_at_utc") is not None and row.get("observation") for row in self._connections.values()) and all(parser is not None and not parser.errors and not parser.has_pending() and all(row["status"] == "complete" for row in parser.outcome_rows()) for parser in self._parsers.values())
            if self.versions["version"] == 2:
                complete = complete and (self.root / "ready.json").exists() and (self.root / "ready-environment.json").exists() and all(row.get("protocol") and row.get("transport_observation") for row in self._connections.values())
            return self._seal_native("complete" if complete else "truncated")

    def _seal_native(self, status: str) -> dict[str, Any]:
        with self._lock:
            self._journal("sealing", {"status": status})
            if self.versions["version"] == 2:
                from tools.official_client_capture import claude_mac_capture_h2_contract as h2_contract
                self._policy.verify_protocol_runtime(self.configuration)
                requests, outcomes, frames, blocks = h2_contract.parser_rows(self._parsers)
                for name, rows in (("native-h2-frames.jsonl", frames), ("native-h2-header-blocks.jsonl", blocks)):
                    c.write_once(self.root / name, b"".join(c.canonical(row) + b"\n" for row in rows))
                self._dependencies.new_hpack_decoder(self.configuration["limits"]["max_header_bytes"])
                c.write_json_once(self.root / "h2-dependency-read-set.json", self._dependencies.receipt())
            else:
                requests = [row for parser in self._parsers.values() for row in parser.request_rows()]
                outcomes = [row for parser in self._parsers.values() for row in parser.outcome_rows()]
            for name, rows in (("native-connections.jsonl", list(self._connections.values())), ("native-requests.jsonl", requests), ("native-outcomes.jsonl", outcomes)):
                c.write_once(self.root / name, b"".join(c.canonical(row) + b"\n" for row in rows))
            journal_raw = c.read_bytes(self.root / "journal.jsonl")
            journal_path = self.root / ("journal-prefix-" + hashlib.sha256(journal_raw).hexdigest()[:24] + ".jsonl")
            c.write_once(journal_path, journal_raw)
            raw_files = [c.binding(self.root, path) for path in sorted(self.root.rglob("*.bin"))]
            manifest = {"schema_version": self.versions["native"], "source_scope": self._policy.SCOPE, "task_sha256": self.task_sha, "candidate": self.task["candidate"], **{name: self.task["execution"][name] for name in ("capture_id", "producer_run_id", "endpoint")}, "deployment": self.configuration["deployment"], "configuration_sha256": c.digest(self.configuration), "runtime_bindings": c.read_json(self.root / "daemon-owner.json")["runtime_bindings"], "status": status, "reasons": list(dict.fromkeys(self._reasons)), "closed_at_utc": c.utc_now(), "reservation": c.binding(self.root, self.root / "reservation.json"), "ready": c.binding(self.root, self.root / "ready.json") if (self.root / "ready.json").exists() else None, "connections": c.binding(self.root, self.root / "native-connections.jsonl"), "requests": c.binding(self.root, self.root / "native-requests.jsonl"), "outcomes": c.binding(self.root, self.root / "native-outcomes.jsonl"), "journal": c.binding(self.root, journal_path), "raw_files": raw_files}
            if self.versions["version"] == 2:
                manifest.update(protocols=sorted({row.get("protocol") for row in self._connections.values()} - {None}))
                manifest.update(self._policy.native_extra(self.configuration))
                for key, name in (("configuration", "native-configuration.json"), ("runtime_environment", "runtime-environment.json"),
                    ("dependency_manifest", "h2-dependencies/production_decoder-dependencies.json"),
                    ("dependency_read_set", "h2-dependency-read-set.json"), ("h2_frames", "native-h2-frames.jsonl"),
                    ("h2_header_blocks", "native-h2-header-blocks.jsonl")):
                    manifest[key] = c.binding(self.root, self.root / name)
                manifest["ready_environment"] = c.binding(self.root, self.root / "ready-environment.json") if (self.root / "ready-environment.json").exists() else None
            c.write_json_once(self.root / "native-capture-manifest.json", manifest)
            self._phase = "committed"
            self._publish_inventory()
            return self.inventory(self.task_sha)

    def _publish_inventory(self) -> None:
        path = self.root / "export-inventory.json"
        if path.exists():
            inventory = c.read_json(path)
        else:
            originals = []
            for item in sorted(self.root.rglob("*")):
                c.plain_path(item)
                if item.is_dir():
                    continue
                if item.name in {"export-inventory.json", "export-commit.json"}:
                    continue
                originals.append(c.binding(self.root, item))
            core = {"schema_version": self.versions["inventory"], "source_scope": self._policy.SCOPE, "task_sha256": self.task_sha, "capture_id": self.task["execution"]["capture_id"], "producer_run_id": self.task["execution"]["producer_run_id"], "configuration_sha256": c.digest(self.configuration), "deployment": self.configuration["deployment"], "native_manifest": c.binding(self.root, self.root / "native-capture-manifest.json"), "files": originals}
            core["schema_version"] = self.versions["inventory"]
            inventory = {**core, "export_id": c.digest(core)}
            c.write_json_once(path, inventory)
        native = self._policy.verify_export(self.root, inventory, task_sha256=self.task_sha, expected_version=self.versions["version"])
        c.require(native["configuration_sha256"] == c.digest(self.configuration) and native["runtime_bindings"] == self._policy.runtime_bindings(self.configuration), "导出有效代码或配置已漂移")
        commit = {"schema_version": self.versions["commit"], "task_sha256": self.task_sha, "export_id": inventory["export_id"], "inventory": c.binding(self.root, path)}
        c.write_json_once(self.root / "export-commit.json", commit)
        c.fsync_directory(self.root)
        self._inventory = inventory
        self._committed_requests = len(c.read_bytes(c.check_binding(self.root, native["requests"])).splitlines())
        self._committed_connections = len(c.read_bytes(c.check_binding(self.root, native["connections"])).splitlines())

    def inventory(self, task_sha256: str, export_id: str | None = None) -> dict[str, Any]:
        with self._operation_lock:
            self._check_task(task_sha256)
            c.require(self._inventory is not None, "采集尚未持久关闭，不能导出")
            c.require(export_id is None or export_id == self._inventory["export_id"], "不可变 export ID 冲突")
            self._policy.verify_export(self.root, self._inventory, task_sha256=self.task_sha)
            c.require(c.read_json(self.root / "export-inventory.json") == self._inventory, "持久 inventory 已漂移")
            commit = c.exact(c.read_json(self.root / "export-commit.json"), {"schema_version", "task_sha256", "export_id", "inventory"}, "export commit")
            c.require(commit == {"schema_version": self.versions["commit"], "task_sha256": self.task_sha, "export_id": self._inventory["export_id"], "inventory": c.binding(self.root, self.root / "export-inventory.json")}, "export commit 已漂移")
            return self._inventory

    def export_chunk(self, task_sha256: str, export_id: str, relative: str, offset: int, limit: int) -> dict[str, Any]:
        with self._operation_lock:
            inventory = self.inventory(task_sha256, export_id)
            references = [item for item in inventory["files"] if item["path"] == relative]
            c.require(len(references) == 1, "export 不能读取 inventory 以外的原件")
            c.require(type(offset) is int and 0 <= offset <= references[0]["bytes"] and type(limit) is int and 0 < limit <= c.MAX_CHUNK_BYTES, "导出 offset 或大小非法")
            raw = c.read_bytes(c.check_binding(self.root, references[0]))[offset:offset + limit]
            return {"export_id": export_id, "path": relative, "offset": offset, "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "data_base64": base64.b64encode(raw).decode("ascii")}

    def recover(self, task_sha256: str) -> dict[str, Any]:
        self._check_task(task_sha256)
        c.require(self._inventory is not None, "仍存活的 capture 不能靠 recover 重启或续监听；先 close")
        return self.inventory(task_sha256)

    def status(self) -> dict[str, Any]:
        return {"source_scope": self._policy.SCOPE, "task_sha256": self.task_sha, "phase": self._phase, "connections": self._committed_connections if self._inventory else len(self._connections), "requests": self._committed_requests if self._inventory else sum(len(parser.requests) for parser in self._parsers.values() if parser is not None), "export_id": self._inventory["export_id"] if self._inventory else None}

    def dispose(self) -> None:
        if not getattr(self, "_closed", False):
            self._closed = True
            descriptor = getattr(self, "_lock_descriptor", None)
            if descriptor is not None:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)


class ConnectionWorker(threading.Thread):
    """单线程复制两条 socket 腿，避免 SSL 对象被多个线程并发读写。"""

    def __init__(self, engine: RawCaptureCore, connection: socket.socket, connection_id: str, number: int):
        super().__init__(name="claude-native-" + connection_id, daemon=True)
        self.engine, self.front, self.connection_id, self.number = engine, connection, connection_id, number
        self.directory = c.private_directory(engine.root / "connections" / connection_id)
        self.recorder = wire.DurableByteRecorder(self.directory, number)
        self.parser = None if engine.versions["version"] == 2 else wire.H1Parser(connection_id, engine.configuration["limits"], engine.on_request_header)
        self.upstream: socket.socket | None = None
        self.stop_requested = threading.Event()
        self.tls_facts: dict[str, Any] = {}
        self.offers: list[str] = []
        self.prepared = False
        self.forced_stop = False
        self.forward_error: str | None = None

    def request_stop(self, *, force: bool = False) -> None:
        self.stop_requested.set()
        if not force:
            return
        self.forced_stop = True
        for connection in (self.front, self.upstream):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

    def _front_tls(self) -> None:
        configuration = self.engine.configuration
        self.front.settimeout(configuration["limits"]["socket_timeout_seconds"])
        material = configuration["listener"]["tls"]
        if material is None:
            return
        self.offers, hello = wire.peek_client_hello(self.front, configuration["limits"]["socket_timeout_seconds"])
        c.write_once(self.directory / "tls-client-hello.bin", hello)
        v2 = self.engine.versions["version"] == 2
        if not v2:
            c.require(all(item == "http/1.1" for item in self.offers), "TLS ClientHello 提供 H2 或未支持协议；不降级 H1")
        supported = [item for item in self.offers if item in {"h2", "http/1.1"}] if v2 else self.offers
        c.require(not self.offers or bool(supported), "ClientHello 不含已实现的 ALPN 协议")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(c.check_binding(self.engine.data_root, material["certificate"])), str(c.check_binding(self.engine.data_root, material["private_key"])))
        if self.offers:
            context.set_alpn_protocols(supported)
        self.front = context.wrap_socket(self.front, server_side=True)
        self.tls_facts = {"client_alpn_offers": self.offers, "client_negotiated_alpn": self.front.selected_alpn_protocol(), "client_tls_version": self.front.version()}
        if v2:
            self.tls_facts["front_server_alpn"] = supported
            c.write_json_once(self.directory / "client-tls-observation.json", self.tls_facts)
            selected = self.front.selected_alpn_protocol()
            c.require(selected in supported if self.offers else
                      (selected is None and configuration["protocol_policy"]["allow_no_alpn_h1"]),
                      "客户端实际 ALPN 与冻结能力及原 offer 不符")
        else:
            c.require(self.front.selected_alpn_protocol() == ("http/1.1" if self.offers else None), "客户端实际 ALPN 与真实 offer 不符")
        c.write_json_once(self.directory / "client-tls-observation.json", self.tls_facts)

    def _connect_upstream(self) -> None:
        configuration = self.engine.configuration
        endpoint = configuration["upstream"]
        upstream = socket.create_connection((endpoint["host"], endpoint["port"]), timeout=configuration["limits"]["socket_timeout_seconds"])
        self.upstream = upstream
        if endpoint["tls"] is not None:
            context = ssl.create_default_context(cafile=str(c.check_binding(self.engine.data_root, endpoint["tls"]["ca"])))
            if self.offers:
                context.set_alpn_protocols(self.offers)
            upstream = context.wrap_socket(upstream, server_hostname=endpoint["tls"]["server_hostname"])
            self.upstream = upstream
            selected = upstream.selected_alpn_protocol()
            if self.engine.versions["version"] == 1:
                c.require(selected == ("http/1.1" if self.offers else None), "上游实际 ALPN 与捕获腿不一致")
            self.tls_facts.update(upstream_negotiated_alpn=selected, upstream_tls_version=upstream.version())
        if self.engine.versions["version"] == 2:
            self.tls_facts["upstream_alpn_offers"] = list(self.offers)
        path = self.directory / "upstream-connection-observation.json"
        c.write_json_once(path, {"client_endpoint": _endpoint(upstream.getsockname()), "server_endpoint": _endpoint(upstream.getpeername()), "observed_at_utc": c.utc_now(), "tls": self.tls_facts})
        if self.engine.versions["version"] == 2 and endpoint["tls"] is not None:
            c.require(self.tls_facts["upstream_negotiated_alpn"] == self.tls_facts["client_negotiated_alpn"] and
                      self.tls_facts["upstream_alpn_offers"] == self.tls_facts["client_alpn_offers"],
                      "两腿真实 ALPN 不一致，首片原字节禁止转发")
        self.engine.record_transport(self, path)
        upstream.settimeout(configuration["limits"]["socket_timeout_seconds"])

    def run(self) -> None:
        front_eof = False
        try:
            if self.stop_requested.is_set():
                return
            self._front_tls()
            if self.engine.versions["version"] == 2:
                selected = self.tls_facts.get("client_negotiated_alpn")
                self.engine.bind_parser(self, "h2" if selected == "h2" else "h1")
            self.prepared = True
            while not self.stop_requested.is_set():
                candidates = ([] if front_eof else [self.front]) + ([self.upstream] if self.upstream else [])
                if not candidates:
                    break
                pending_ssl = [item for item in candidates if isinstance(item, ssl.SSLSocket) and item.pending()]
                ready = pending_ssl or select.select(candidates, [], [], 0.05)[0]
                for connection in ready:
                    direction = wire.CLIENT_DIRECTION if connection is self.front else wire.SERVER_DIRECTION
                    try:
                        raw = connection.recv(65536)
                    except socket.timeout:
                        continue
                    if not raw:
                        self.engine.record_eof(self, direction)
                        if direction == wire.SERVER_DIRECTION:
                            return
                        front_eof = True
                        if self.upstream is None:
                            return
                        try:
                            self.upstream.shutdown(socket.SHUT_WR)
                        except OSError:
                            pass
                        continue
                    # WAL 与原始字节已 fsync、请求已计入之后才允许转发。
                    self.engine.record_segment(self, direction, raw)
                    if direction == wire.CLIENT_DIRECTION and self.upstream is None:
                        self._connect_upstream()
                    target = self.upstream if direction == wire.CLIENT_DIRECTION else self.front
                    target.sendall(raw)
        except BaseException as error:
            if not self.stop_requested.is_set():
                self.engine.mark_incomplete(str(error) if isinstance(error, c.CaptureError) else "真实 socket 或 TLS 采集失败：" + type(error).__name__)
        finally:
            # 正常停止前把已入内核／SSL 缓冲的字节纳入原件，不能只因 parser 空就签零值。
            if self.stop_requested.is_set() and not self.forced_stop and self.prepared:
                try:
                    end = time.monotonic() + 0.05
                    while time.monotonic() < end:
                        candidates = [item for item in (self.front, self.upstream) if item is not None]
                        ready = [item for item in candidates if isinstance(item, ssl.SSLSocket) and item.pending()] or select.select(candidates, [], [], 0)[0]
                        if not ready:
                            break
                        progressed = False
                        for item in ready:
                            item.settimeout(0.05)
                            raw = item.recv(65536)
                            if raw:
                                progressed = True
                                direction = wire.CLIENT_DIRECTION if item is self.front else wire.SERVER_DIRECTION
                                self.engine.record_segment(self, direction, raw)
                                self.engine.mark_incomplete("正常停止边界收到未转发的缓冲字节")
                        if not progressed:
                            break
                except (OSError, c.CaptureError):
                    self.engine.mark_incomplete("停止边界缓冲字节不能完整解析")
            if not self.prepared and self.engine.configuration["listener"]["tls"] is not None:
                self.engine.mark_incomplete("TLS 握手没有完整完成")
            for connection in (self.front, self.upstream):
                if connection is not None:
                    try:
                        connection.close()
                    except OSError:
                        pass
            self.recorder.close()
            for direction in wire.DIRECTIONS:
                if self.parser is None or not self.parser.states[direction]["eof"]:
                    try:
                        self.engine.record_eof(self, direction)
                    except c.CaptureError:
                        self.engine.mark_incomplete("关闭连接时存在未完成 H1 消息" if self.engine.versions["version"] == 1
                                                    else "关闭连接时存在未完成原生消息")
            if self.parser is None:
                self.engine.mark_incomplete("连接缺真实协议选择，不能签零值成功")
            self.engine.record_closed(self)


def serve(engine: RawCaptureCore) -> None:
    """已完成恢复才发布控制监听；错误 key／nonce 不能调用固定 task 的状态机。"""
    path = engine.socket_path
    if path.exists():
        info = path.lstat()
        c.require(stat.S_ISSOCK(info.st_mode) and info.st_uid == os.getuid(), "旧 control socket 类型或所有者非法")
        path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    path.chmod(0o600)
    server.listen(16)
    server.settimeout(0.1)
    nonces: set[str] = set()
    try:
        while not engine.stop_event.is_set():
            if engine._phase == "ready" and engine._policy.timestamp(c.utc_now()) >= engine._policy.timestamp(engine.task["execution"]["request_policy"]["deadline_utc"]):
                engine.close(engine.task_sha, "deadline")
            try:
                connection, _ = server.accept()
            except socket.timeout:
                continue
            nonce = ""
            with connection:
                try:
                    connection.settimeout(5)
                    request = c.exact(c.parse_json(receive(connection)), {"protocol", "nonce", "action", "payload", "mac"}, "采集 RPC")
                    core = {name: request[name] for name in ("protocol", "nonce", "action", "payload")}
                    nonce = request["nonce"]
                    c.require(request["protocol"] == engine.versions["rpc"] and isinstance(nonce, str) and len(nonce) == 48 and isinstance(request["mac"], str) and hmac.compare_digest(request["mac"], _mac(engine.key, core)), "采集 RPC 来源鉴权失败")
                    c.require(nonce not in nonces, "采集 RPC nonce 已重放")
                    c.require(len(nonces) < 65536, "采集 RPC nonce 数超过有界能力")
                    nonces.add(nonce)
                    action, payload = request["action"], request["payload"]
                    if action == "status":
                        c.exact(payload, set(), "status 请求")
                        value = engine.status()
                    elif action == "reserve":
                        c.exact(payload, {"task", "hold"}, "reserve 请求")
                        value = engine.reserve(payload["task"], payload["hold"])
                    elif action == "ready":
                        c.exact(payload, {"task_sha256"}, "ready 请求")
                        value = engine.wait_ready(payload["task_sha256"])
                    elif action in {"close", "shutdown"}:
                        c.exact(payload, {"task_sha256", "reason"}, "close 请求")
                        value = engine.close(payload["task_sha256"], payload["reason"])
                        if action == "shutdown":
                            engine.stop_event.set()
                    elif action == "inventory":
                        c.exact(payload, {"task_sha256", "export_id"}, "inventory 请求")
                        value = engine.inventory(payload["task_sha256"], payload["export_id"])
                    elif action == "export":
                        c.exact(payload, {"task_sha256", "export_id", "path", "offset", "limit"}, "export 请求")
                        value = engine.export_chunk(payload["task_sha256"], payload["export_id"], payload["path"], payload["offset"], payload["limit"])
                    elif action == "recover":
                        c.exact(payload, {"task_sha256"}, "recover 请求")
                        value = engine.recover(payload["task_sha256"])
                    else:
                        raise c.CaptureError("未定义的采集 RPC 操作")
                    body = {"ok": True, "value": value}
                except Exception as error:
                    body = {"ok": False, "value": str(error) if isinstance(error, (ValueError,)) else "采集操作失败：" + type(error).__name__}
                response = {"protocol": engine.versions["rpc"], "nonce": nonce, "body": body}
                try:
                    connection.sendall(c.canonical({**response, "mac": _mac(engine.key, response)}) + b"\n")
                except OSError:
                    # 响应丢失不撤销已提交原件；同 producer 下次只同步或恢复。
                    pass
    finally:
        server.close()
        if path.exists() and not path.is_symlink():
            path.unlink()
        engine.dispose()
