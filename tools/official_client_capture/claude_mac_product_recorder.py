"""九方法的内部状态／诊断桥；本批只接受显式回归夹具。

状态恢复使用真实 StateJournal。观测委托静态来源拥有的 fixture provider，
没有产品启动、UI、信号、凭据或网络实现。formal 在任何取锁、写入和 provider
调用之前拒绝；这里不生成 source-manifest、正式 origin 或成功收据。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Iterator, Mapping, Protocol

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_recipe as recipe_tools
from tools.official_client_capture.claude_mac_state_journal import SnapshotLimits, StateJournal

if TYPE_CHECKING:
    from tools.official_client_capture.claude_upgrade_mac_candidate_services import CandidateMacClientServices


class ProductRecorderError(ValueError):
    """内部上下文、阶段、原件或只写一次合同不符。"""


class ProductRecorderUnavailable(ProductRecorderError):
    """正式能力或完整原件尚未交付；不能用诊断冒充成功。"""


@dataclass(frozen=True)
class StateSetupOperation:
    """静态工厂预先冻结的已知状态意图，不接受操作函数。"""

    operation_id: str
    expected_after: Mapping[str, Any]


@dataclass(frozen=True)
class RecorderStatePlan:
    """一个私有根与准确路径全集；没有全局 HOME 或凭据快捷入口。"""

    private_state_root: Path
    frozen_paths: tuple[str, ...]
    symlink_targets: Mapping[str, str]
    limits: SnapshotLimits
    setup_operations: tuple[StateSetupOperation, ...]
    launch_plan: Mapping[str, Any]


class ProductObservationProvider(Protocol):
    """内部观测接点；结构完整性不授予正式来源资格。"""

    def start_monitoring(self, task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]: ...
    def recheck_monitoring(self, task: Mapping[str, Any]) -> dict[str, Any]: ...
    def launch(self, task: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]: ...
    def drive(self, task: Mapping[str, Any], scenario: Mapping[str, Any]) -> dict[str, Any]: ...
    def stop_owned(self, task: Mapping[str, Any]) -> dict[str, Any]: ...
    def close_observations(self, task: Mapping[str, Any]) -> dict[str, Any]: ...
    def recover_owned(self, task: Mapping[str, Any]) -> dict[str, Any]: ...
    def observation_inventory(self, task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]: ...


_FROZEN = "diagnostic/frozen-recorder.json"
_WAL = "diagnostic/recorder-lifecycle.jsonl"
_FIRST = "diagnostic/first-partial/manifest.json"
_ANCHOR = "diagnostic/first-partial-anchor.json"
_REPLY_FIELDS = frozenset({"task_sha256", "task_nonce", "producer_run_id", "deployment", "phase",
                           "raw_files", "facts", "completeness", "unknown_reasons"})
_FIXTURE_EVENTS = ("prepared", "monitor_ready", "product_started", "product_stopped", "state_restored", "closed")


def _require(condition: Any, message: str) -> None:
    if not condition:
        raise ProductRecorderError(message)


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _relative(value: Any) -> str:
    _require(isinstance(value, str) and bool(value) and len(value.encode()) <= 1024, "内部相对路径非法")
    parts = value.split("/")
    _require(not value.startswith("/") and all(part not in {"", ".", ".."} for part in parts), "内部路径越界")
    _require(all(len(part.encode()) <= 255 and not any(ord(char) < 32 for char in part) for part in parts), "内部路径分量非法")
    return PurePosixPath(value).as_posix()


def _directory(path: Path) -> dict[str, int]:
    c.require_plain_path(path)
    info = path.lstat()
    _require(stat.S_ISDIR(info.st_mode) and info.st_uid == os.geteuid()
             and stat.S_IMODE(info.st_mode) & 0o077 == 0, "内部目录不是当前用户的私有普通目录")
    return {"dev": info.st_dev, "ino": info.st_ino}


class NativeProductRecorder:
    """薄接线；真实产品与正式来源仍明确 unavailable。

    静态 adapter 还须提供 owns_observation_provider(provider, task, bundle_root)，
    核实例确由它为原 task 构造。本批没有正式 adapter；测试只临时登记 fixture。
    构造器纯只读，所有可变数据深拷贝，调用方不能事后改计划扩大状态读写范围。
    """

    def __init__(self, task: Mapping[str, Any], *, bundle_root: Path,
                 services: CandidateMacClientServices, provider: ProductObservationProvider,
                 state_plan: RecorderStatePlan) -> None:
        self._task = copy.deepcopy(source.validate_task_v2(task))
        _require(isinstance(state_plan, RecorderStatePlan), "缺静态构造的状态计划")
        self._plan = copy.deepcopy(state_plan)
        self._plan.limits.validate()
        self._root = Path(bundle_root)
        self._state = Path(self._plan.private_state_root)
        self._root_instance = _directory(self._root)
        self._state_instance = _directory(self._state)
        self._services, self._provider = services, provider
        self._adapter = c.SOURCE_ADAPTERS.get(self._task["execution"]["recorder_id"])
        execution = self._task["execution"]
        self._identity = {"task_sha256": self._task["task_sha256"], "task_nonce": self._task["task_nonce"],
                          "producer_run_id": execution["producer_run_id"],
                          "deadline_utc": execution["request_policy"]["deadline_utc"]}
        self._frozen = {"schema_version": "claude-mac-recorder-frozen/internal-v1", "task": self._task,
                        "identity": self._identity, "bundle_root": str(self._root), "bundle_instance": self._root_instance,
                        "private_state_root": str(self._state), "state_instance": self._state_instance,
                        "state_plan": {"paths": list(self._plan.frozen_paths), "symlink_targets": dict(self._plan.symlink_targets),
                                       "limits": asdict(self._plan.limits),
                                       "setup_operations": [asdict(operation) for operation in self._plan.setup_operations],
                                       "launch_plan": dict(self._plan.launch_plan)}}
        if (self._root / "state-journal").exists():
            self._frozen["journal_instance"] = _directory(self._root / "state-journal")
        self._journal: StateJournal | None = None
        self._ready = False
        self._dispatched = False
        self._instance: dict[str, Any] | None = None
        self._quiescent = False
        self._failures: list[dict[str, str]] = []
        self._expected_wal: bytes | None = None
        self._prepared_here = False

    def _guard(self, task: Mapping[str, Any], bundle_root: Path | None = None) -> None:
        # 此检查必须先于所有取锁、写入、provider 和所有权回调；旗标不能解除。
        if self._task["execution"]["source_scope"] != "regression_fixture":
            raise ProductRecorderUnavailable("本批未交付正式产品观测后端与隔离资格")
        _require(source.validate_task_v2(task) == self._task, "完整原 task 已变化")
        _require(bundle_root is None or Path(bundle_root) == self._root, "来源根不是原目录")
        _require(_directory(self._root) == self._root_instance and _directory(self._state) == self._state_instance,
                 "原目录 dev/ino 已变化")
        if "journal_instance" in self._frozen:
            _require(_directory(self._root / "state-journal") == self._frozen["journal_instance"], "原 journal 目录 dev/ino 已变化")
        adapter = c.SOURCE_ADAPTERS.get(self._task["execution"]["recorder_id"])
        _require(adapter is not None and adapter is self._adapter, "原静态来源未登记或已替换")
        for name, args in (("owns_recorder", (self,)), ("owns_services", (self._services,)),
                           ("owns_observation_provider", (self._provider, self._task, self._root))):
            check = getattr(adapter, name, None)
            _require(callable(check) and check(*args) is True, "依赖不属于原静态 fixture 来源")

    @contextmanager
    def _parent(self, relative: str) -> Iterator[tuple[int, str]]:
        parts = _relative(relative).split("/")
        fd = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            _require({"dev": info.st_dev, "ino": info.st_ino} == self._root_instance,
                     "证据根在打开时被替换")
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                try:
                    info = os.fstat(next_fd)
                    _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0
                             and info.st_dev == self._root_instance["dev"], "内部目录权限或文件系统不符")
                except BaseException:
                    os.close(next_fd)
                    raise
                previous, fd = fd, next_fd
                os.close(previous)
            yield fd, parts[-1]
        finally:
            os.close(fd)

    def _mkdir(self, relative: str) -> None:
        with self._parent(relative) as (fd, leaf):
            try:
                os.mkdir(leaf, 0o700, dir_fd=fd)
                os.fsync(fd)
            except FileExistsError:
                pass
            child = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            try:
                info = os.fstat(child)
                _require(info.st_uid == os.geteuid() and stat.S_IMODE(info.st_mode) & 0o077 == 0
                         and info.st_dev == self._root_instance["dev"], "诊断目录不安全")
            finally:
                os.close(child)

    def _read(self, relative: str) -> bytes:
        with self._parent(relative) as (parent, leaf):
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            try:
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                         and stat.S_IMODE(info.st_mode) == 0o600, "原件不是私有普通单链接文件")
                _require(info.st_size <= self._plan.limits.max_wal_bytes, "内部原件超限")
                chunks, size = [], 0
                while chunk := os.read(fd, 65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    _require(size <= self._plan.limits.max_wal_bytes, "内部读取超限")
                ended = os.fstat(fd)
                _require((info.st_size, info.st_mtime_ns, info.st_ctime_ns) ==
                         (ended.st_size, ended.st_mtime_ns, ended.st_ctime_ns), "原件读取期间变化")
                current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
                _require((current.st_dev, current.st_ino) == (info.st_dev, info.st_ino), "原件读取时路径被替换")
                return b"".join(chunks)
            finally:
                os.close(fd)

    def _optional(self, relative: str) -> bytes | None:
        try:
            return self._read(relative)
        except FileNotFoundError:
            return None

    def _create(self, relative: str, raw: bytes) -> None:
        _require(len(raw) <= self._plan.limits.max_wal_bytes, "内部写入超限")
        with self._parent(relative) as (parent, leaf):
            try:
                fd = os.open(leaf, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            except FileExistsError:
                _require(self._read(relative) == raw, "只写一次原件被修改或内容不同")
                return
            try:
                remaining = memoryview(raw)
                while remaining:
                    count = os.write(fd, remaining)
                    _require(count > 0, "原件写入没有前进")
                    remaining = remaining[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent)

    def _binding(self, relative: str) -> dict[str, Any]:
        raw = self._read(relative)
        return {"path": relative, "sha256": _sha(raw), "bytes": len(raw)}

    def _bound(self, binding: Mapping[str, Any]) -> bytes:
        _require(isinstance(binding, Mapping) and set(binding) == {"path", "sha256", "bytes"}, "原件绑定闭集非法")
        raw = self._read(_relative(binding["path"]))
        _require(binding["sha256"] == _sha(raw) and type(binding["bytes"]) is int
                 and binding["bytes"] == len(raw), "不可变快照摘要或字节数漂移")
        return raw

    def _rows(self) -> list[dict[str, Any]]:
        _require(json.loads(self._read(_FROZEN)) == self._frozen, "完整冻结 task／路径／原目录实例已变化")
        raw = self._read(_WAL)
        _require(self._expected_wal is None or raw == self._expected_wal, "活动生命周期 WAL 被改写或截短")
        _require(bool(raw) and raw.endswith(b"\n"), "没有完整生命周期 WAL，不能推断 no-launch")
        rows: list[dict[str, Any]] = []
        for line in raw.splitlines():
            row = json.loads(line)
            fields = {"sequence", "previous_sha256", "identity", "event", "payload", "observed_at_utc", "sha256"}
            _require(isinstance(row, dict) and set(row) == fields and row["identity"] == self._identity
                     and row["sequence"] == len(rows) + 1, "生命周期 WAL 身份或序号非法")
            _require(row["previous_sha256"] == (rows[-1]["sha256"] if rows else "0" * 64)
                     and row["sha256"] == c.digest({key: value for key, value in row.items() if key != "sha256"}),
                     "生命周期 WAL 断链或内容漂移")
            c.timestamp(row["observed_at_utc"])
            rows.append(row)
        _require(rows[0]["event"] == "prepare_intent" and raw == b"".join(c.canonical(row) + b"\n" for row in rows),
                 "生命周期 WAL 起点或规范字节非法")
        return rows

    def _append(self, event: str, payload: Any) -> None:
        current = self._optional(_WAL)
        rows = self._rows() if current is not None else []
        _require(rows or event == "prepare_intent", "缺首次准备意图，禁止补造阶段")
        body = {"sequence": len(rows) + 1, "previous_sha256": rows[-1]["sha256"] if rows else "0" * 64,
                "identity": self._identity, "event": event, "payload": payload, "observed_at_utc": _now()}
        raw = c.canonical({**body, "sha256": c.digest(body)}) + b"\n"
        _require(len(current or b"") + len(raw) <= self._plan.limits.max_wal_bytes, "生命周期 WAL 超限")
        with self._parent(_WAL) as (parent, leaf):
            fd = os.open(leaf, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and info.st_uid == os.geteuid()
                         and stat.S_IMODE(info.st_mode) == 0o600 and info.st_size == len(current or b""), "生命周期 WAL 写入前漂移")
                view = memoryview(raw)
                while view:
                    count = os.write(fd, view)
                    _require(count > 0, "WAL 写入没有前进")
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent)
        self._expected_wal = (current or b"") + raw

    def _failure(self, phase: str, error: BaseException) -> None:
        value = {"phase": phase, "error_type": type(error).__name__}
        self._failures.append(value)
        try:
            self._append("failure", value)
        except BaseException:
            # 记录失败也不能覆盖首异常；partial 会明确缺失或损坏的 WAL。
            pass

    def _release(self) -> None:
        journal, self._journal = self._journal, None
        if journal is not None:
            journal.__exit__(None, None, None)

    def _new_journal(self) -> StateJournal:
        return StateJournal(self._identity, self._root / "state-journal", self._state, self._plan.frozen_paths,
                            symlink_targets=self._plan.symlink_targets, limits=self._plan.limits)

    def _preflight(self) -> dict[str, Any]:
        verified = recipe_tools.validate_private_launch_plan(self._task, self._plan.launch_plan, bundle_root=self._root)
        recipe = verified["recipe"]
        expected = {str(self._state / path) for path in self._plan.frozen_paths}
        _require(set(recipe["state_paths"]) == expected and len(recipe["state_paths"]) == len(expected), "状态路径不是 recipe 的准确全集")
        _require(self._state.is_relative_to(Path(recipe["private_session_root"]))
                 and self._state_instance["dev"] == self._root_instance["dev"], "私有状态根或文件系统不符")
        return recipe

    def _fixture_event(self, event: str, at: str) -> None:
        existing = self._optional("recorder-journal.jsonl") or b""
        rows = [json.loads(line) for line in existing.splitlines()]
        prefix = [row["event"] for row in rows]
        expected = list(_FIXTURE_EVENTS)
        if prefix and prefix[-1] == "state_restore_failed":
            expected[4] = "state_restore_failed"
        if event == "state_restore_failed":
            expected[4] = event
        _require(prefix == expected[:len(prefix)] and len(prefix) < len(expected)
                 and event == expected[len(prefix)], "fixture journal 阶段不闭合")
        _require(not rows or c.timestamp(at) >= c.timestamp(rows[-1]["observed_at_utc"]), "fixture 事件时间倒置")
        observed = {"event": event, "task_sha256": self._task["task_sha256"], "observed_at_utc": at}
        relative = f"fixture-observations/{len(rows) + 1:02d}-{event}.json"
        self._create(relative, c.canonical(observed))
        row = {**observed, "observation": self._binding(relative)}
        # 这里只形成夹具前缀；没有正式来源清单，不能被导入为成功。
        with self._parent("recorder-journal.jsonl") as (parent, leaf):
            fd = os.open(leaf, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            try:
                info = os.fstat(fd)
                _require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and stat.S_IMODE(info.st_mode) == 0o600,
                         "fixture journal 原件不安全")
                raw = c.canonical(row) + b"\n"
                _require(os.write(fd, raw) == len(raw), "fixture journal 短写")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(parent)

    def _call(self, phase: str, method: str, *args: Any, intent: Mapping[str, Any] | None = None) -> dict[str, Any]:
        self._append(phase + "_intent", dict(intent or {}))
        reply = getattr(self._provider, method)(*args)
        _require(isinstance(reply, dict) and set(reply) == _REPLY_FIELDS, "provider 返回字段闭集非法")
        for key, value in (("task_sha256", self._identity["task_sha256"]), ("task_nonce", self._identity["task_nonce"]),
                           ("producer_run_id", self._identity["producer_run_id"]), ("phase", phase),
                           ("deployment", self._task["execution"]["source_deployments"]["mac"])):
            _require(reply[key] == value, "provider 原件上下文不符")
        refs = reply["raw_files"]
        _require(isinstance(refs, list) and 1 <= len(refs) <= 64 and isinstance(reply["facts"], dict)
                 and isinstance(reply["completeness"], dict) and isinstance(reply["unknown_reasons"], list), "provider 缺实际原件")
        for ref in refs:
            _require(ref["path"].startswith("provider-observations/"), "provider 原件不能占用保留路径")
            self._bound(ref)
        _require(json.loads(self._bound(refs[0])) == {key: value for key, value in reply.items() if key != "raw_files"},
                 "provider 返回与实际观测原件不符")
        self._append(phase + "_result", reply)
        return reply

    def prepare_state(self, task: Mapping[str, Any], bundle_root: Path) -> None:
        self._guard(task, bundle_root)
        try:
            self._preflight()
            _require(self._journal is None and self._optional(_WAL) is None, "已有生命周期只能显式 recover，不能重新 prepare")
            journal = self._new_journal()
            journal.__enter__()
            self._journal = journal
            self._frozen["journal_instance"] = _directory(self._root / "state-journal")
            self._mkdir("diagnostic")
            self._mkdir("fixture-observations")
            self._create(_FROZEN, c.canonical(self._frozen))
            self._append("prepare_intent", {})
            self._prepared_here = True
            journal.prepare()
            self._create("state-before.bin", self._read("state-journal/state-before.bin"))
            self._create("diagnostic/before-bridge.json", c.canonical({"source_scope": "internal_state_only",
                         "original": self._binding("state-journal/state-before.bin"), "copy": self._binding("state-before.bin")}))
            prepared = next(row for row in (json.loads(line) for line in self._read("state-journal/journal.jsonl").splitlines())
                            if row["event"] == "prepared")
            self._fixture_event("prepared", prepared["observed_at_utc"])
            self._append("prepared", journal.evidence())
            for operation in self._plan.setup_operations:
                journal.record_intent(operation.operation_id, operation.expected_after)
                journal.apply_intent(operation.operation_id)
        except BaseException as error:
            self._failure("prepare", error)
            self._release()
            raise

    def start_monitoring(self, task: Mapping[str, Any], bundle_root: Path) -> None:
        self._guard(task, bundle_root)
        try:
            _require(self._journal is not None and not self._ready, "监测必须在本次 prepare 后且仅一次")
            reply = self._call("monitoring", "start_monitoring", self._task, self._root)
            _require(reply["completeness"].get("status") == "complete", "监测未知或已丢失")
            at = reply["facts"]["started_at_utc"]
            self._create("monitor-ready.json", c.canonical({"task_sha256": self._identity["task_sha256"],
                         "deployment": self._task["execution"]["source_deployments"]["mac"], "started_at_utc": at}))
            self._fixture_event("monitor_ready", at)
            self._ready = True
        except BaseException as error:
            self._failure("monitoring", error)
            raise

    def recheck_dispatch(self, task: Mapping[str, Any]) -> None:
        self._guard(task)
        try:
            _require(self._journal is not None and self._ready and not self._dispatched, "缺当前监测或已派发")
            self._services.recheck_dispatch(self._task)
            reply = self._call("recheck", "recheck_monitoring", self._task)
            _require(reply["completeness"].get("status") == "complete", "当前监测未知或已丢失")
            self._preflight()
            self._dispatched = True
        except BaseException as error:
            self._failure("recheck", error)
            raise

    def _launch(self, task: Mapping[str, Any], recipe: Mapping[str, Any], client_id: str) -> None:
        self._guard(task)
        try:
            _require(task["client_id"] == client_id and dict(recipe) == self._preflight(), "产品或 recipe 不是冻结原件")
            _require(self._journal is not None and self._dispatched
                     and not any(row["event"] == "launch_intent" for row in self._rows()), "未准入或已有 launch-intent")
            reply = self._call("launch", "launch", self._task, copy.deepcopy(dict(self._plan.launch_plan)))
            _require(reply["completeness"].get("status") == "complete" and isinstance(reply["facts"].get("process_instance"), dict)
                     and bool(reply["facts"]["process_instance"]), "缺原产品实例，不能造 product_started")
            self._instance = copy.deepcopy(reply["facts"]["process_instance"])
            self._fixture_event("product_started", reply["facts"]["started_at_utc"])
        except BaseException as error:
            self._failure("launch", error)
            raise

    def launch_desktop(self, task: Mapping[str, Any], recipe: Mapping[str, Any]) -> None:
        self._launch(task, recipe, "claude-desktop")

    def launch_vscode(self, task: Mapping[str, Any], recipe: Mapping[str, Any]) -> None:
        self._launch(task, recipe, "claude-vscode")

    def drive_scenario(self, task: Mapping[str, Any], scenario: Mapping[str, Any]) -> None:
        self._guard(task)
        try:
            inherited = {item for reuse in task["scenario_reuse"] for item in reuse["scenario_ids"]}
            _require(self._instance is not None and dict(scenario) in task["scenarios"]
                     and scenario["id"] not in inherited, "场景不属于本次原产品或已承接")
            _require(not any(row["event"] == "drive_intent" and row["payload"].get("scenario_id") == scenario["id"]
                             for row in self._rows()), "场景已执行，不自动重发")
            reply = self._call("drive", "drive", self._task, dict(scenario), intent={"scenario_id": scenario["id"]})
            _require(reply["completeness"].get("status") == "complete"
                     and reply["facts"].get("scenario_id") == scenario["id"], "场景实际结果未知或不符")
        except BaseException as error:
            self._failure("drive", error)
            raise

    def stop_product(self, task: Mapping[str, Any]) -> None:
        self._guard(task)
        try:
            _require(self._prepared_here, "本实例没有完整调用历史，不能推断 no-launch；须显式 recover")
            rows = self._rows()
            if self._quiescent:
                return
            if not any(row["event"] == "launch_intent" for row in rows):
                self._quiescent = True
                if self._journal is not None:
                    self._append("no_launch", {"scope": "this_fixture_recorder_only"})
                return
            _require(self._instance is not None, "launch 结果未知，不能凭 PID 或缺原件回收")
            reply = self._call("stop", "stop_owned", self._task)
            _require(reply["completeness"].get("status") == "complete" and reply["facts"].get("stopped") is True
                     and reply["facts"].get("process_instance") == self._instance, "原 owned 实例停止未知，不恢复状态")
            self._fixture_event("product_stopped", reply["facts"]["stopped_at_utc"])
            self._quiescent = True
        except BaseException as error:
            self._failure("stop", error)
            self._release()
            raise

    def restore_state(self, task: Mapping[str, Any], bundle_root: Path) -> None:
        self._guard(task, bundle_root)
        try:
            _require(self._journal is not None and self._quiescent, "没有原实例停止事实或活动状态锁，禁止恢复")
            self._append("state_restore_intent", {})
            evidence = self._journal.restore()
            self._create("state-after.bin", self._read("state-journal/state-after.bin"))
            self._create("diagnostic/after-bridge.json", c.canonical({"source_scope": "internal_state_only",
                         "original": self._binding("state-journal/state-after.bin"), "copy": self._binding("state-after.bin")}))
            self._append("state_restored", evidence)
            if self._instance is not None:
                self._fixture_event("state_restored", _now())
        except BaseException as error:
            self._failure("restore", error)
            self._release()
            raise

    def _verify_partial(self) -> dict[str, Any]:
        manifest = json.loads(self._read(_FIRST))
        anchor = json.loads(self._read(_ANCHOR))
        _require(anchor == {"identity": self._identity, "manifest": self._binding(_FIRST)}, "首 partial 锚点漂移")
        _require(manifest["identity"] == self._identity and manifest["frozen_context"] == self._frozen
                 and manifest["source_scope"] == "internal_state_only"
                 and all(manifest[key] is False for key in ("registration_verified", "formal_ready", "launch_allowed")),
                 "首 partial 的完整冻结上下文不符")
        for item in manifest["files"]:
            _require(item["snapshot"]["path"].startswith("diagnostic/first-partial/snapshots/"), "首 partial 不能绑定活跃原件")
            frozen = self._bound(item["snapshot"])
            current = self._read(item["origin_path"])
            if item["origin_path"] in {_WAL, "state-journal/journal.jsonl"}:
                _require(current.startswith(frozen), "活跃 WAL 修改了首 partial 历史前缀")
            else:
                _require(current == frozen, "首次来源原件已被修改")
        return manifest

    def _snapshots(self, directory: str) -> list[dict[str, Any]]:
        self._mkdir(directory)
        self._mkdir(directory + "/snapshots")
        paths = {_FROZEN, _WAL, "state-before.bin", "state-after.bin", "recorder-journal.jsonl", "monitor-ready.json",
                 "diagnostic/before-bridge.json", "diagnostic/after-bridge.json"}
        state_root = self._root / "state-journal"
        if state_root.exists():
            # 原子替换的 trash/stage 是有权限语义的状态节点，不冒充普通来源文件；
            # 其完整状态在实际 WAL 的不可变 materials 中，恢复仍由本体逐项核验。
            paths.update("state-journal/" + name for name in ("frozen-state.json", "state-before.bin", "state-after.bin", "journal.jsonl"))
            paths.update(path.relative_to(self._root).as_posix() for path in state_root.glob("failure-*.json"))
            materials = state_root / "materials"
            if materials.exists():
                paths.update(path.relative_to(self._root).as_posix() for path in materials.iterdir())
        try:
            rows = self._rows()
        except (ValueError, OSError, KeyError, TypeError):
            rows = []
        for row in rows:
            if row["event"].endswith("_result") and isinstance(row["payload"], dict):
                paths.update(ref["path"] for ref in row["payload"].get("raw_files", []))
        observations = self._root / "fixture-observations"
        if observations.exists():
            paths.update(path.relative_to(self._root).as_posix() for path in observations.iterdir())
        provider_observations = self._root / "provider-observations"
        if provider_observations.exists():
            paths.update(path.relative_to(self._root).as_posix() for path in provider_observations.iterdir())
        _require(len(paths) <= self._plan.limits.max_entries, "封存文件数量超限")
        files, total = [], 0
        for index, relative in enumerate(sorted(paths)):
            raw = self._optional(relative)
            if raw is None:
                continue
            total += len(raw)
            _require(total <= self._plan.limits.max_total_bytes, "封存精确快照总量超限")
            snapshot = f"{directory}/snapshots/{index:04d}.bin"
            self._create(snapshot, raw)
            files.append({"origin_path": relative, "snapshot": self._binding(snapshot)})
        return files

    def seal_sources(self, task: Mapping[str, Any], bundle_root: Path, *, status: str) -> None:
        try:
            self._guard(task, bundle_root)
            _require(status in {"complete", "failed", "truncated"}, "上游终态非法")
            if self._optional(_FIRST) is not None:
                self._verify_partial()
            else:
                self._mkdir("diagnostic")
                close_error = None
                unknown = ["正式原生来源与完整 v2 封装未交付"]
                try:
                    rows = self._rows()
                except (ValueError, OSError, KeyError, TypeError):
                    rows = []
                    unknown.append("生命周期 WAL 缺失或损坏，不推断 no-launch 或调用未知 close")
                if not any(row["event"] == "close_intent" for row in rows):
                    if rows:
                        try:
                            self._call("close", "close_observations", self._task)
                        except BaseException as error:
                            close_error = error
                            self._failure("close", error)
                self._mkdir("diagnostic/first-partial")
                files = self._snapshots("diagnostic/first-partial")
                try:
                    rows = self._rows()
                    lifecycle_snapshot = next(item["snapshot"] for item in files if item["origin_path"] == _WAL)
                    _require(self._bound(lifecycle_snapshot) == self._read(_WAL), "封存期间生命周期 WAL 变化")
                except (ValueError, OSError, KeyError, TypeError, StopIteration):
                    rows = []
                    unknown.append("封存时无法重验完整生命周期历史")
                manifest = {"schema_version": "claude-mac-recorder-partial/internal-v1", "identity": self._identity,
                            "frozen_context": self._frozen, "client_id": self._task["client_id"],
                            "source_deployments": self._task["execution"]["source_deployments"], "upstream_status": status,
                            "diagnostic_status": "incomplete" if status == "complete" else status,
                            "lifecycle_status": "complete" if rows else "unknown",
                            "source_scope": "internal_state_only", "registration_verified": False,
                            "formal_ready": False, "launch_allowed": False, "files": files,
                            "phase_results": [{"sequence": row["sequence"], "event": row["event"]} for row in rows],
                            "failure_types": self._failures, "unknown_reasons": unknown,
                            "sealed_at_utc": _now()}
                self._create(_FIRST, c.canonical(manifest))
                self._create(_ANCHOR, c.canonical({"identity": self._identity, "manifest": self._binding(_FIRST)}))
                if close_error is not None:
                    raise close_error
            if status == "complete":
                raise ProductRecorderUnavailable("只有内部 partial，不能生成正式来源或返回产品成功")
        except BaseException as error:
            self._failure("seal", error)
            raise
        finally:
            self._release()

    def _recovery_receipt(self, evidence: Mapping[str, Any], *, error: BaseException | None = None) -> dict[str, Any]:
        # 只在独立状态复核之后计算键；存在旧恢复文件绝不是跳过实际读取的理由。
        key = c.digest({"frozen_context": self._frozen, "first_partial": self._binding(_FIRST),
                        "state": dict(evidence), "lifecycle": self._binding(_WAL),
                        "error_type": type(error).__name__ if error else None})
        directory = "diagnostic/recovery-" + key
        relative = directory + "/manifest.json"
        existing = self._optional(relative)
        if existing is not None:
            result = json.loads(existing)
            _require(result["key"] == key and result["identity"] == self._identity and result["frozen_context"] == self._frozen,
                     "恢复原件完整冻结上下文漂移")
            _require(json.loads(self._read(directory + "/anchor.json")) ==
                     {"identity": self._identity, "manifest": self._binding(relative)}, "恢复清单只写一次锚点漂移")
            _require(result["state_evidence"] == dict(evidence) and result["first_partial"] == self._binding(_FIRST)
                     and result["status"] == ("failed" if error else evidence["status"])
                     and result["error_type"] == (type(error).__name__ if error else None)
                     and result["source_scope"] == "internal_state_only"
                     and all(result[field] is False for field in ("registration_verified", "formal_ready", "launch_allowed")),
                     "历史恢复清单与本次实际复核不符")
            for item in result["files"]:
                _require(item["snapshot"]["path"].startswith(directory + "/snapshots/"), "恢复快照越界")
                self._bound(item["snapshot"])
            return result
        files = self._snapshots(directory)
        result = {"schema_version": "claude-mac-recorder-recovery/internal-v1", "key": key, "identity": self._identity,
                  "frozen_context": self._frozen, "first_partial": self._binding(_FIRST),
                  "status": "failed" if error else evidence["status"], "source_scope": "internal_state_only",
                  "registration_verified": False, "formal_ready": False, "launch_allowed": False,
                  "state_evidence": dict(evidence), "files": files, "error_type": type(error).__name__ if error else None,
                  "observed_at_utc": _now()}
        self._create(relative, c.canonical(result))
        self._create(directory + "/anchor.json", c.canonical({"identity": self._identity, "manifest": self._binding(relative)}))
        return result

    def recover(self, task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]:
        self._guard(task, bundle_root)
        try:
            self._preflight()
            partial = self._verify_partial()
            _require(partial.get("lifecycle_status") == "complete", "首 partial 已认定生命周期历史未知，不能恢复为 no-launch")
            rows = self._rows()
            for relative in ("state-journal/frozen-state.json", "state-journal/state-before.bin", "state-journal/journal.jsonl"):
                self._read(relative)
            _require(self._journal is None, "活动 recorder 不能同时 recover")
            journal = self._new_journal()
            journal.__enter__()
            self._journal = journal
            journal.prepare()
            if any(row["event"] == "launch_intent" for row in rows):
                originals = [row["payload"]["facts"]["process_instance"] for row in rows if row["event"] == "launch_result"]
                _require(bool(originals), "缺原 owned 实例结果，不调用未知回收")
                reply = self._call("recovery_owned", "recover_owned", self._task)
                _require(reply["completeness"].get("status") == "complete"
                         and reply["facts"].get("stopped") is True
                         and reply["facts"].get("process_instance") == originals[0], "恢复时原 owned 实例仍未知")
            evidence = journal.recover()
            # 每一次都由本体独立读取当前状态；重复返回前再验首 partial 的快照和完整 task。
            self._guard(task, bundle_root)
            self._verify_partial()
            return self._recovery_receipt(evidence)
        except BaseException as error:
            self._failure("recover", error)
            if self._journal is not None:
                try:
                    self._recovery_receipt(self._journal.evidence(), error=error)
                except BaseException:
                    pass
            raise
        finally:
            self._release()
