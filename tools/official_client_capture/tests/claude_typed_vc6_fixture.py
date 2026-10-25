"""当前 Claude VC-6 控制回归共用夹具：原 Store、真实 typed 合同和独立守卫。

候选资格沿用调用方已经完成的 VC-5；回滚资格在同一 Store、同一外部根中
经既有事实生产器建立。仅文件现场和探针观测是 regression_fixture 输入。
守卫执行、恢复、原件导出、Store 发布、收据与 checkpoint 不替换。
正式登记表只在测试上下文中临时安装，退出即恢复；本文件不能授予正式认证。
"""

from __future__ import annotations

import atexit
import contextlib
import copy
import functools
import hashlib
from contextvars import ContextVar
import json
import multiprocessing
import os
import signal
import stat
import tempfile
import time
import traceback
from pathlib import Path, PurePosixPath
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import claude_upgrade_release as release
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import claude_acceptance_contract as acceptance
from tools.official_client_capture import claude_recorded_evaluation_contract as recorded
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests.test_claude_real_delivery_integration import DeliveryCampaign, QualifiedFileRuntime, fixture_projection
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_capture.tests import claude_fault_chain_world as stops
from tools.official_client_capture.tests import claude_gate_reader_memo as gate_reader_memo
from tools.official_client_capture.tests import claude_tool_snapshot_memo as tool_snapshot_memo
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_json_bytes, canonical_sha256, sha256_bytes, sha256_file

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR_NAME = "arm64-isolated"
CONTROL_SCHEMA = "claude-typed-vc6-test-source/v1"
SEAM_CONTROL_ENV = "CLAUDE_TYPED_VC6_CONTROL"
SEAM_ACTIONS_ENV = "CLAUDE_TYPED_VC6_ACTIONS"
SEAM_REPOSITORY_ENV = "CLAUDE_TYPED_VC6_REPOSITORY"
DIAGNOSTIC_ROOT_ENV = "CLAUDE_TYPED_VC6_DIAGNOSTIC_ROOT"
_STACKS: list[contextlib.ExitStack] = []


class _ActiveFileReads:
    """同一次只读调用的原字节坐标；命中核元数据，收尾再次完整实读。

    文件与根内祖先核设备、inode、类型权限、所有者、长度及纳秒时间；
    根外祖先核设备、inode、类型权限和所有者，避免无关临时目录成员干扰。
    目录闭集仍由原消费者逐次检查。活状态属于对象，复制 ContextVar
    也不能延长寿命；退出清除原字节、解析副本及纯计算结果。
    """

    def __init__(self, root: Path, error: Any):
        self.root, self.error, self.active = root, error, True
        self.files: dict[Path, tuple[Any, bytes, Any]] = {}
        self.documents: dict[Path, dict[str, Any]] = {}
        self.digests: dict[Path, str] = {}
        self.contracts: dict[bytes, dict[str, Any]] = {}

    def require_active(self) -> None:
        if self.active is not True:
            raise self.error("typed 原件读取作用域已退出")

    def metadata(self, path: Path) -> tuple[Any, ...]:
        self.require_active()
        if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(self.root):
            raise self.error("typed 原件读取越出本次根")
        entries = []
        try:
            for index, current in enumerate((path, *path.parents)):
                info = current.lstat()
                if (stat.S_ISLNK(info.st_mode)
                        or (index == 0 and not stat.S_ISREG(info.st_mode))
                        or (index != 0 and not stat.S_ISDIR(info.st_mode))):
                    raise self.error("typed 原件或祖先类型发生漂移")
                coordinate = (str(current), info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
                if index == 0 or current.is_relative_to(self.root):
                    coordinate += (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                entries.append(coordinate)
        except OSError as error:
            raise self.error("typed 原件或祖先已不可读") from error
        return tuple(entries)

    def read(self, path: Path, read_bytes: Any = None, *, current: bool = False) -> bytes:
        self.require_active()
        previous = self.files.get(path)
        before = self.metadata(path)
        if previous is not None and before != previous[0]:
            raise self.error("typed 原件路径、权限或元数据漂移")
        if previous is not None and not current:
            return previous[1]
        reader = read_bytes if read_bytes is not None else Path.read_bytes
        raw = reader(path)
        after = self.metadata(path)
        if before != after or not isinstance(raw, bytes) or (previous is not None and raw != previous[1]):
            raise self.error("typed 原件实读期间或原字节发生漂移")
        if previous is None:
            self.files[path] = (after, raw, reader)
        return raw

    def verify_metadata(self) -> None:
        self.require_active()
        for path, (expected, _raw, _reader) in self.files.items():
            if self.metadata(path) != expected:
                raise self.error("typed 原件路径、权限或元数据漂移")

    def verify_full(self) -> None:
        self.require_active()
        for path, (_metadata, _raw, reader) in list(self.files.items()):
            self.read(path, reader, current=True)

    def close(self) -> None:
        self.active = False
        self.files.clear()
        self.documents.clear()
        self.digests.clear()
        self.contracts.clear()


class _LocalActiveFileReads(_ActiveFileReads):
    """本地清单按轮核全体坐标；共同祖先轮首轮尾各核一次。

    元数据基线仍属于同一个活批次，不缓存验证通过结论。每轮的祖先去重
    仅合并重复 lstat；正常和异常收尾继续完整实读并核对全部原字节。
    公共 recorded 读取仍使用原 _ActiveFileReads，不承接这里的轮次策略。
    """

    def __init__(self, root: Path, error: Any):
        super().__init__(root, error)
        self.ancestors: dict[Path, tuple[Any, ...]] = {}

    def _coordinate(self, path: Path, *, file: bool) -> tuple[Any, ...]:
        self.require_active()
        if file and (not path.is_absolute() or ".." in path.parts or not path.is_relative_to(self.root)):
            raise self.error("typed 原件读取越出本次根")
        try:
            info = path.lstat()
        except OSError as error:
            raise self.error("typed 原件或祖先已不可读") from error
        if (stat.S_ISLNK(info.st_mode) or (file and not stat.S_ISREG(info.st_mode))
                or (not file and not stat.S_ISDIR(info.st_mode))):
            raise self.error("typed 原件或祖先类型发生漂移")
        coordinate = (str(path), info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid)
        if file or path.is_relative_to(self.root):
            coordinate += (info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        return coordinate

    def _verify_ancestors(self, paths: Any = ()) -> None:
        self.require_active()
        current = set(self.ancestors)
        for path in paths:
            if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(self.root):
                raise self.error("typed 原件读取越出本次根")
            if path not in self.files:
                current.update(path.parents)
        for path in sorted(current):
            observed = self._coordinate(path, file=False)
            if self.ancestors.setdefault(path, observed) != observed:
                raise self.error("typed 原件路径、权限或元数据漂移")

    def read_all(self, rows: list[tuple[Path, Any]], *, current: bool = True) -> None:
        """首读与尾读完整实读；纯读批次命中仍核全清单坐标，异常也核轮尾。"""
        self.require_active()
        self._verify_ancestors(path for path, _reader in rows)
        try:
            for path, reader in rows:
                previous = self.files.get(path)
                before = self._coordinate(path, file=True)
                if previous is not None and before != previous[0]:
                    raise self.error("typed 原件路径、权限或元数据漂移")
                if previous is not None and not current:
                    continue
                raw = reader(path)
                after = self._coordinate(path, file=True)
                if (before != after or not isinstance(raw, bytes)
                        or (previous is not None and raw != previous[1])):
                    raise self.error("typed 原件实读期间或原字节发生漂移")
                if previous is None:
                    self.files[path] = (after, raw, reader)
        finally:
            self._verify_ancestors()

    def verify_metadata(self) -> None:
        self.require_active()
        for path, (expected, _raw, _reader) in self.files.items():
            if self._coordinate(path, file=True) != expected:
                raise self.error("typed 原件路径、权限或元数据漂移")
        self._verify_ancestors()

    def verify_full(self) -> None:
        self.read_all([(path, reader) for path, (_metadata, _raw, reader) in list(self.files.items())])
        self.verify_metadata()

    def close(self) -> None:
        super().close()
        self.ancestors.clear()


@contextlib.contextmanager
def _recorded_read_scope(root: Path) -> Iterator[None]:
    """只在一次原 manifest 验证内复用公共读取和画像纯计算。

    原 policy 校验、逐 probe 验证、附件实读及目录闭集均照常执行。
    仅原 optional_policy 所读取的公共文件加入读集；独有 probe 原件
    不进入缓存。每次独立 manifest 调用重新首读，正常和异常退出
    都完整复读并失效；返回 JSON 与合同均为独立副本。
    """

    original_manifest, original_policy = recorded.verify_recorded_manifest, recorded.optional_policy
    original_read, original_binding = recorded._read, recorded.binding
    original_contract = acceptance.build_contract_payload
    active: ContextVar[Any] = ContextVar("typed_vc6_recorded_public_reads", default=None)
    collecting: ContextVar[bool] = ContextVar("typed_vc6_recorded_collect_public", default=False)

    def snapshot_for(path: Path) -> Any:
        snapshot = active.get()
        if snapshot is not None:
            snapshot.require_active()
            if path.is_absolute() and path.is_relative_to(root) and (collecting.get() or path in snapshot.files):
                return snapshot
        return None

    def binding(path: Path) -> dict[str, Any]:
        snapshot = snapshot_for(path)
        if snapshot is None:
            return original_binding(path)
        raw = snapshot.read(path)
        if path not in snapshot.digests:
            snapshot.digests[path] = sha256_bytes(raw)
        return {"path": str(path), "sha256": snapshot.digests[path], "bytes": len(raw)}

    def read(path: Path) -> dict[str, Any]:
        snapshot = snapshot_for(path)
        if snapshot is None:
            return original_read(path)
        try:
            raw = snapshot.read(path)
            if path not in snapshot.documents:
                value = json.loads(raw, object_pairs_hook=recorded._pairs)
                if not isinstance(value, dict):
                    raise recorded.RecordedEvaluationError("录制合同 JSON 必须是对象")
                snapshot.documents[path] = value
        except (OSError, ValueError) as error:
            raise recorded.RecordedEvaluationError(f"录制合同不能读取：{path}") from error
        return copy.deepcopy(snapshot.documents[path])

    def policy(state: Any) -> Any:
        if active.get() is None:
            return original_policy(state)
        active.get().require_active()
        token = collecting.set(True)
        try:
            return original_policy(state)
        finally:
            collecting.reset(token)

    def contract(profile: Any) -> dict[str, Any]:
        snapshot = active.get()
        if snapshot is None or not collecting.get():
            return original_contract(profile)
        snapshot.verify_metadata()
        # 原函数仍推导首次载荷；画像和侧别限定表全部进入精确输入键。
        key = recorded._canonical([profile, sorted(acceptance.SIDE_RESTRICTED_CHECKS.items()),
            sorted(acceptance.WIRE_RECORD_TYPES), sorted(acceptance.INTERNAL_RECORD_TYPES),
            sorted(acceptance.KNOWN_RECORD_TYPES), acceptance.SIDES,
            acceptance.CONTRACT_SCHEMA, acceptance.COVERAGE_CHECK_ID,
            acceptance.MODE_DUAL_WIRE, acceptance.MODE_CANDIDATE_PROFILE])
        if key not in snapshot.contracts:
            snapshot.contracts[key] = copy.deepcopy(original_contract(profile))
        return copy.deepcopy(snapshot.contracts[key])

    def manifest(value: Any, observed_root: Path) -> Any:
        if observed_root != root or value.get("schema_version") != recorded.EVALUATION_SCHEMA:
            return original_manifest(value, observed_root)
        snapshot = _ActiveFileReads(root, recorded.RecordedEvaluationError)
        token = active.set(snapshot)
        try:
            return original_manifest(value, observed_root)
        finally:
            try:
                snapshot.verify_full()
            finally:
                snapshot.close()
                active.reset(token)

    with mock.patch.object(recorded, "verify_recorded_manifest", manifest), \
            mock.patch.object(recorded, "optional_policy", policy), \
            mock.patch.object(recorded, "_read", read), \
            mock.patch.object(recorded, "binding", binding), \
            mock.patch.object(acceptance, "build_contract_payload", contract):
        yield


def _diagnostic_root() -> Path | None:
    """显式开关只定位私有测试日志，缺省完全沿用原测试行为。"""

    configured = os.environ.get(DIAGNOSTIC_ROOT_ENV)
    if not configured:
        return None
    root = Path(configured)
    guard.private_directory(root)
    return root


@contextlib.contextmanager
def retain_setup_world() -> Iterator[None]:
    """仅保留 VC-6 setUp 创建的首个世界；其余清理与临时目录原样执行。

    基类先建世界，再创建出口隔离目录。只脱离首个目录的自动删除器，
    其路径不搬移、不复制。基类已登记的守卫、出口与 registry 清理不变。
    """

    root = _diagnostic_root()
    if root is None:
        yield
        return
    create = tempfile.TemporaryDirectory
    retained: list[Any] = []

    def first_world(*arguments: Any, **options: Any) -> Any:
        directory = create(*arguments, **options)
        if not retained:
            directory._finalizer.detach()
            directory.cleanup = lambda: None
            retained.append(directory)
        return directory

    try:
        with mock.patch.object(tempfile, "TemporaryDirectory", side_effect=first_world):
            yield
    finally:
        if retained:
            world = Path(retained[0].name).resolve()
            guard.write_json_once(root / f"retained-world-{world.name}.json", {
                "schema_version": "claude-typed-vc6-test-diagnostic-world/v1",
                "fixture_only": True,
                "world_path": str(world),
                "repository": str(REPOSITORY_ROOT),
                "retained_in_place": True,
                "guard_and_registry_cleanup": "unchanged",
            })


@contextlib.contextmanager
def action_diagnostics(
    sequence: int, action: dict[str, Any], environment: dict[str, str], control_path: Path,
) -> Iterator[Path | None]:
    """只为指定 VC-6 动作保留原输出，不改父命令、判据或异常传播。

    仍以 capture_output=False 执行同一 argv，仅把原 DEVNULL 接到 600 文件。
    每次派发新建目录，完整日志和非密钥接缝绑定只写一次，旧现场不覆盖。
    """

    root = _diagnostic_root()
    if root is None:
        yield None
        return
    action_id = str(action["action_id"])
    if action_id not in {delivery.RUN_ACTION_ID, delivery.FINALIZE_ACTION_ID}:
        raise ValueError("诊断范围仅限两个 VC-6 交付动作")
    control = _control(control_path)
    directory = Path(tempfile.mkdtemp(prefix=f"{sequence:04d}-{action_id}-", dir=root))
    output_path = directory / "action-output.log"
    run_command = supervisor.SupervisorClient.run_command

    def record_action(client: Any, argv: Any, **options: Any) -> Any:
        actual_environment = options.get("env") or {}
        if (options.get("job_id") != action_id
                or actual_environment.get(supervisor.CAMPAIGN_RUN_ACTION_ID_ENV) != action_id
                or options.get("capture_output") is not False
                or options.get("output_stream") is not None):
            return run_command(client, argv, **options)
        guard.write_json_once(directory / "action-inputs.json", {
            "schema_version": "claude-typed-vc6-test-diagnostic-inputs/v1",
            "fixture_only": True,
            "sequence": sequence,
            "action_id": action_id,
            "command": list(argv),
            "capture_output": False,
            "timeout_seconds": options.get("timeout_seconds"),
            "cleanup_grace_seconds": options.get("cleanup_grace_seconds", 0),
            "seam_environment": {key: actual_environment.get(key) for key in environment},
            "control_path": str(control_path),
            "configuration_ref": control["context"]["configuration_ref"],
            "injection_binding": guard.binding(Path(environment["PYTHONPATH"]),
                                                Path(environment["PYTHONPATH"]) / "usercustomize.py"),
        })
        descriptor = os.open(output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                             | getattr(os, "O_NOFOLLOW", 0), 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            forwarded = dict(options, output_stream=stream)
            try:
                return run_command(client, argv, **forwarded)
            finally:
                stream.flush()
                os.fsync(stream.fileno())

    with mock.patch.object(supervisor.SupervisorClient, "run_command", new=record_action):
        yield output_path


def diagnostic_outcome(
    outcome: tuple[int, dict | None, str], output_path: Path | None,
) -> tuple[int, dict | None, str]:
    """失败摘要只追加原件位置与短尾部；结果对象、退出码和完整日志不变。"""

    code, result, stderr = outcome
    if output_path is None or code == 0:
        return outcome
    tail = ""
    if output_path.exists():
        with output_path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            stream.seek(max(0, stream.tell() - 2000))
            tail = stream.read().decode("utf-8", errors="replace")
    return code, result, stderr + f"\nVC-6 动作原件：{output_path}\n{tail}"


def _control(path: Path) -> dict[str, Any]:
    """只消费显式回归来源；配置路径与摘要仍按原始字节核对。"""

    value = guard.read_json(path)
    if (value.get("schema_version") != CONTROL_SCHEMA or value.get("fixture_only") is not True
            or value.get("formal_version_semantics_certified") is not False):
        raise guard.GuardError("typed VC-6 来源没有限定为测试")
    root = Path(value["context"]["data_root"])
    raw_path = guard.check_binding(root, value["context"]["configuration_ref"])
    configuration = executor.validate_configuration(guard.read_json(raw_path))
    executor.validate_execution_context(value["context"], configuration)
    if configuration["source_scope"] != "regression_fixture":
        raise guard.GuardError("测试来源不能安装正式执行器")
    return value


def write_control(path: Path, **fields: Any) -> Path:
    """只改故障控制，不改冻结配置、原 Store 或已有调用日志。"""

    value = _control(path)
    if set(fields) - {"failures", "kill_at", "crash_at", "failed_gates"}:
        raise ValueError("typed 夹具只允许设置已声明的故障")
    value.update(failures=[], kill_at=None, crash_at=None, failed_gates=[])
    value.update(fields)
    guard.atomic_json(path, value)
    return path


def read_calls(path: Path, *, physical: bool = False) -> list[tuple[str, str]]:
    """默认返回阶段尝试；physical=True 只返回独立守卫实际执行的阶段。"""

    value = _control(path)
    calls = Path(value["physical_calls" if physical else "calls"])
    if not calls.exists():
        return []
    return [tuple(json.loads(line)) for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()]


def _append_call(path: str, segment: str, stage: str) -> None:
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(json.dumps([segment, stage]) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class ControlledFileRuntime(QualifiedFileRuntime):
    """从原件读取故障；实际阶段仍核 prepared、armed、索引与持久化日志。"""

    def __init__(self, configuration: dict, context: dict, control_path: Path):
        super().__init__(configuration, context)
        self.control_path = control_path

    def execute(self, stage: str, target: dict, environment: dict, directory: Path, checkpoint: Any) -> dict:
        prepared = guard.read_json(directory / "prepared.json")
        segment = str(prepared["request"]["run_nonce"]).rsplit("-", 1)[-1]
        control = _control(self.control_path)
        _append_call(control["physical_calls"], segment, stage)
        fault = guard.read_json(self.root / "fault.json")
        fault["execute"] = "failed" if any(item[:2] == [segment, stage] for item in control["failures"]) else "normal"
        if control["crash_at"] == [segment, stage]:
            fault["execute"] = "exception"
        guard.atomic_json(self.root / "fault.json", fault)
        return super().execute(stage, target, environment, directory, checkpoint)

    def freeze(self, request: dict, directory: Path) -> dict:
        value = super().freeze(request, directory)
        failed = set(_control(self.control_path)["failed_gates"])
        for row in value["gates"]:
            if row["id"] in failed:
                row["result"] = "failed"
        return value

    def collect_completed_delivery_admission(self, request: dict, directory: Path, stable_execution: Any, *, source: Any) -> dict:
        """在已有文件现场边界实读资格和故障；不返回调用方预先给定的 pass。"""
        if (not isinstance(source, execution.VerifiedSource) or source.trusted_data_root != self.root
                or source.configuration_sha256 != canonical_sha256(self.configuration)
                or getattr(source.gate_projection, "successor", None) is None):
            raise guard.GuardError("typed 当前测量缺本次真实材料来源")
        execution._revalidate_guard_continuity(source, required=True)
        guard.private_directory(directory)
        qualification = self._check_completed_qualification(source)
        state = guard.read_json(self.root / "state.json")
        fault = guard.read_json(self.root / "fault.json")
        control = _control(self.control_path)
        base = guard.read_bound_json(self.root, self.configuration["paths"]["compose_binding"])
        overrides = {role: guard.read_bound_json(self.root, self.configuration[role]["override_binding"]) for role in ("candidate", "rollback")}
        profiles = {role: guard.read_bound_bytes(self.root, self.configuration[role]["profile_binding"]) for role in ("candidate", "rollback")}
        snapshot = {"state": state, "fault": fault, "control": control, "base": base, "overrides": overrides,
                    "profile_sha256_by_role": {role: hashlib.sha256(raw).hexdigest() for role, raw in profiles.items()}}
        snapshot_path = directory / "file-scene.json"
        qualification_path = directory / "qualification.json"
        guard.write_json_once(snapshot_path, snapshot)
        guard.write_json_once(qualification_path, qualification)
        target = {key: self.configuration["candidate"][key] for key in ("image_digest", "runtime_profile_sha256")}
        observation = self.observation("stable_observed", target, directory / "observed")
        bindings = guard.all_bindings([guard.binding(self.root, snapshot_path), guard.binding(self.root, qualification_path), observation])
        pair = (state["image_digest"], state["runtime_profile_sha256"])
        allowed = {(self.configuration[role]["image_digest"], self.configuration[role]["runtime_profile_sha256"]) for role in ("candidate", "rollback")}
        failed = set(control["failed_gates"])
        sound = fault["freeze"] == "normal" and fault["restore"] == "normal"
        values = {"compose-diff": qualification["candidate_compose"] != qualification["rollback_compose"] and qualification["result"] == "pass",
                  "dependency-connectivity": sound, "entry-routing": sound, "evidence-inventory": bool(bindings), "health": sound,
                  "image-identity": pair in allowed and profiles["candidate"] != profiles["rollback"] and all(hashlib.sha256(profiles[role]).hexdigest() == self.configuration[role]["runtime_profile_sha256"] for role in profiles),
                  "observation-window": stable_execution["result"] == "pass" and stable_execution["observed_seconds"] >= request["observation_window_seconds"] == self.configuration["limits"]["observation_window_seconds"],
                  "rollback-availability": qualification["qualification_replays"][1]["result"] == "pass", "secret-scan": True}
        credential = guard.read_bound_bytes(self.root, self.configuration["probes"]["credential_binding"]).strip()
        channel_key = guard.read_bound_bytes(self.root, self.configuration["guard"]["channel_key_binding"])
        public = canonical_json_bytes({"environment": state["environment"], "actual": observation["actual"], "request_records": observation["request_records"]})
        values["secret-scan"] = all(secret not in raw for secret in (credential, channel_key) if secret for raw in [public, *(guard.read_bound_bytes(self.root, item) for item in observation["after_bindings"])])
        gates = [{"id": name, "result": "pass" if values[name] and name not in failed else "failed",
                  "facts": {"source": "current-completed-file-readonly-admission", "bindings": bindings}} for name in executor.DELIVERY_GATES]
        return {"environment": state["environment"], "rollback": {key: self.configuration["rollback"][key] for key in ("image_digest", "runtime_profile_sha256")},
                "actual": observation["actual"], "gates": gates, "bindings": sorted(bindings, key=lambda item: item["path"])}


class ControlledExecutor(executor.Arm64DeliveryExecutor):
    """准备前强杀复现原替身的中断位置；armed 后强杀由独立专项另验。"""

    def __init__(self, configuration: dict, execution_context: dict, control_path: Path):
        super().__init__(configuration, execution_context)
        self.control_path = control_path

    def _completed_delivery_runtime(self):
        return ControlledFileRuntime(self.configuration, self.context, self.control_path)

    def prepare_stage(self, request: dict) -> dict:
        control = _control(self.control_path)
        segment = str(request["run_nonce"]).rsplit("-", 1)[-1]
        _append_call(control["calls"], segment, request["stage"])
        if control["kill_at"] == [segment, request["stage"]]:
            # 原 FileExecutor 在阶段入口记调用后即退出，没有物理动作。本点保留该语义。
            os.kill(os.getpid(), signal.SIGKILL)
        return super().prepare_stage(request)


class ScopedMacFixtureAdapter:
    """只重放本轮冻结首轮的 Mac 原件，不授予产品派发能力。"""

    def __init__(self, tasks: dict[str, dict[str, Any]]):
        from tools.official_client_capture.tests.claude_mac_v2_fixture import FixtureSourceAdapter
        self.tasks = tasks
        self.original = FixtureSourceAdapter()

    def _require_task(self, task: Any, capture_root: Path) -> dict[str, Any]:
        from tools.official_client_capture import claude_client_receipt_contract as contract
        row = self.tasks.get(task.get("task_sha256"))
        if row is None or dict(task) != row["task"] or Path(capture_root) != row["capture_root"]:
            raise guard.GuardError("typed Mac 来源不属于本轮冻结首轮的准确 task")
        contract.bound_file(row["attempt_root"], row["task_binding"])
        # 原 adapter 每次重新核 fixture_only Claude 总账；原件漂移不能
        # 因测试作用域已登记而继续取得权限。
        self.original._roots(task, capture_root)
        return row

    def verify_origin(self, task: Any, bundle_root: Path, capture_root: Path) -> None:
        row = self._require_task(task, capture_root)
        if Path(bundle_root) != row["bundle_root"]:
            raise guard.GuardError("typed Mac 来源包不是准确 task 的冻结原包")
        self.original.verify_origin(task, bundle_root, capture_root)

    def verify_capture_origin(self, task: Any, capture_root: Path) -> None:
        self._require_task(task, capture_root)
        self.original.verify_capture_origin(task, capture_root)

    def verify_undispatched(self, task: Any, capture_root: Path) -> None:
        self._require_task(task, capture_root)
        self.original.verify_undispatched(task, capture_root)

    def verify_isolation_recipe(self, task: Any, recipe: Any, root: Path) -> None:
        row = self._require_task(task, Path(task["execution"]["capture_root"]))
        if Path(root) != row["bundle_root"]:
            raise guard.GuardError("typed Mac 隔离原件不属于准确 task 的冻结原包")
        self.original.verify_isolation_recipe(task, recipe, root)

    def owns_services(self, services: Any) -> bool:
        return False

    def owns_recorder(self, recorder: Any) -> bool:
        return False


def _fixture_mac_candidate_history(campaign: Path, candidate_id: str) -> list[str]:
    """只沿原 revision 摘要链取得当前候选及前序，不能扫描任意候选目录授予来源。

    交付派发的 Campaign 级检查会重验旧候选封存结果，所以 r2 的测试作用域
    也必须能读 r1 原件。无显式 revision 的旧单候选夹具仍只登记当前候选。
    原 revision reader 核记录与 COMMIT；这里额外核 Campaign、取代关系及
    前序摘要，完整客户端原件仍在下方逐项绑定和验证。
    """
    revisions = upgrade._existing_candidate_revisions(campaign)
    if not revisions:
        return [candidate_id]
    candidates: list[str] = []
    previous = None
    for number in revisions:
        record, commit = upgrade._read_candidate_revision_record(campaign, number)
        if (record is None or commit is None or number != len(candidates) + 1
                or record["campaign_id"] != campaign.name or commit["campaign_id"] != campaign.name
                or commit["candidate_id"] != record["candidate_id"]):
            raise guard.GuardError("typed Mac 前序 revision 记录或 COMMIT 不完整／不同源")
        if previous is not None:
            supersedes = record["supersedes"]
            if (record["previous_revision_sha256"] != previous["record_sha256"]
                    or supersedes["candidate_id"] != previous["candidate_id"]
                    or supersedes["revision"] != previous["revision"]):
                raise guard.GuardError("typed Mac 前序 revision 摘要或取代关系不一致")
        if record["candidate_id"] in candidates:
            raise guard.GuardError("typed Mac revision 重复使用候选")
        candidates.append(record["candidate_id"])
        if record["candidate_id"] == candidate_id:
            return candidates
        previous = record
    raise guard.GuardError("typed Mac 当前候选不在已登记的 revision 链中")


@contextlib.contextmanager
def fixture_mac_source_scope(configuration: dict, data_root: Path) -> Iterator[None]:
    """材料准备、父读侧及新进程各自登记；退出逐字恢复原 registry。

    仅沿资格的精确 Campaign、当前候选及已登记前序的首轮收据取得 task。冻结 task
    全文、原字节绑定和包／捕获坐标一起钉住；fixture_only Claude 总账
    必须与原 attempt 同源。正式任务或其它世界不能借用本测试 authority。
    """
    from tools.official_client_capture import claude_client_receipt_contract as contract
    from tools.official_client_capture import claude_upgrade_compare as compare
    from tools.official_client_capture.tests import claude_client_receipt_fixture as client_fixture
    from tools.official_client_capture.tests.claude_mac_v2_fixture import FixturePatch

    if configuration.get("source_scope") != "regression_fixture":
        raise guard.GuardError("typed Mac authority 只允许明确的回归配置")
    tasks = {}
    for role in ("candidate", "rollback"):
        q = configuration[role]["qualification"]
        parent = guard.read_json(guard.check_binding(data_root, q["sealed_result_binding"]))
        if parent.get("external_gate") is None:
            continue
        campaign = data_root / "evidence/campaigns" / q["campaign_id"]
        if (parent.get("campaign_id"), parent.get("candidate_id")) != (q["campaign_id"], q["candidate_id"]):
            raise guard.GuardError("typed Mac accept 与当前资格归属不符")
        current_path = upgrade._legacy_stage_path(campaign, "capture-candidate", q["candidate_id"])[1]
        current = guard.read_json(current_path)
        if not current.get("client_evidence", {}).get("client_bindings"):
            # 不含 Mac 原件的旧资格夹具不需要 Mac 来源权限；不为它新增
            # revision 前置，也不从其它候选目录寻找可以授权的附件。
            continue
        for candidate_id in _fixture_mac_candidate_history(campaign, q["candidate_id"]):
            round_path = upgrade._legacy_stage_path(campaign, "capture-candidate", candidate_id)[1]
            round_one = guard.read_json(round_path)
            client_bindings = round_one.get("client_evidence", {}).get("client_bindings", {})
            if not client_bindings:
                continue
            if (round_one.get("campaign_id"), round_one.get("candidate_id")) != (q["campaign_id"], candidate_id):
                raise guard.GuardError("typed Mac 冻结首轮、accept 与资格归属不符")
            attempt_root = compare._bound_campaign_file(campaign, round_one["attempt"], "typed Mac 冻结首轮 attempt").parent
            for client_id, client_binding in client_bindings.items():
                contract.require(client_id in contract.CLIENTS, "typed Mac 必须为已登记的 Claude 客户端")
                path = compare._bound_campaign_file(campaign, client_binding, "typed Mac 冻结首轮客户端收据")
                contract.require(path.parent == attempt_root / "client-evidence", "typed Mac 收据必须归属冻结首轮 attempt")
                receipt = contract.read_json(path)
                task_path = contract.bound_file(attempt_root, receipt["task"])
                task = contract.validate_task(contract.read_json(task_path))
                contract.require(task["client_id"] == client_id and task["campaign_id"] == q["campaign_id"]
                    and task["candidate_id"] == candidate_id
                    and task["execution"]["recorder_id"] == client_fixture.RECORDER,
                    "typed Mac task 的客户端、候选或 recorder 归属不符")
                contract.verify_task_attempt(attempt_root, task)
                bundle_root = contract.bound_file(attempt_root, receipt["bundle"]).parent
                capture_root = Path(task["execution"]["capture_root"])
                row = {"task": task, "task_binding": dict(receipt["task"]), "attempt_root": attempt_root,
                    "bundle_root": bundle_root, "capture_root": capture_root}
                # 原 FixturePatch 的来源验证器负责真实 fixture_only 总账及临时根。
                adapter = ScopedMacFixtureAdapter({task["task_sha256"]: row})
                adapter._require_task(task, capture_root)
                previous = tasks.setdefault(task["task_sha256"], row)
                if previous != row:
                    raise guard.GuardError("typed Mac 同一 task 存在不同的冻结坐标")
    if not tasks:
        yield
        return
    with FixturePatch(), mock.patch.dict(contract.SOURCE_ADAPTERS,
            {client_fixture.RECORDER: ScopedMacFixtureAdapter(tasks)}):
        yield


@contextlib.contextmanager
def guard_diagnostics(engine: guard.GuardEngine) -> Iterator[None]:
    """测试开关开启时保存守卫异常原件；原异常、返回值和 RPC 判据均不变。"""

    root = _diagnostic_root()
    if root is None:
        yield
        return

    def observe(name: str, operation: Any) -> Any:
        @functools.wraps(getattr(guard.GuardEngine, name))
        def invoke(*arguments: Any, **options: Any) -> Any:
            try:
                return operation(*arguments, **options)
            except Exception as error:
                # 不记录请求参数或局部变量；日志失败也不能覆盖原始执行异常。
                with contextlib.suppress(Exception):
                    directory = Path(tempfile.mkdtemp(prefix=f"guard-{name}-", dir=root))
                    guard.write_json_once(directory / "exception.json", {
                        "schema_version": "claude-typed-vc6-guard-diagnostic/v1",
                        "fixture_only": True,
                        "formal_certification": False,
                        "operation": name,
                        "exception_type": type(error).__name__,
                        "message": str(error),
                        "traceback": traceback.format_exc(),
                    })
                raise
        return invoke

    with contextlib.ExitStack() as stack:
        for name in ("complete_startup", "status", "inspect", "prepare", "run", "finish",
                     "heartbeat", "recover_pending", "inventory", "export", "export_batch",
                     "export_local", "export_batch_local", "watchdog"):
            stack.enter_context(mock.patch.object(engine, name, new=observe(name, getattr(engine, name))))
        yield


def serve_guard(configuration: dict, context: dict, control_path: Path) -> None:
    """独立进程组持有真实 GuardEngine；动作强杀不会连带终止守卫。"""

    os.setsid()
    root = Path(context["data_root"])
    # 守卫进程自己读控制文件里的合成世界声明（fork 时父进程的作用域不一定已打开）。
    with fixture_mac_source_scope(configuration, root), \
            synthetic_gate_subject_scope(_control(control_path).get("synthetic_gate_subject") is True):
        identity = executor.guard_identity(configuration, context, formal=False)
        engine = guard.GuardEngine(configuration, context, ControlledFileRuntime(configuration, context, control_path), identity)
        key = guard.read_bytes(guard.check_binding(root, configuration["guard"]["channel_key_binding"]))
        with guard_diagnostics(engine):
            guard.serve(engine, root / configuration["guard"]["socket_relative"], key)


@contextlib.contextmanager
def source_scope(control_path: Path) -> Iterator[None]:
    """父读取与动作子进程共用来源登记；不改正式空登记表或可信校验器。"""

    control = _control(control_path)
    root = Path(control["context"]["data_root"])
    configuration = guard.read_json(guard.check_binding(root, control["context"]["configuration_ref"]))
    with fixture_mac_source_scope(configuration, root), \
            synthetic_gate_subject_scope(control.get("synthetic_gate_subject") is True):
        reader = ControlledExecutor(configuration, control["context"], control_path)

        def factory(name: str, observed: dict, context: dict) -> ControlledExecutor:
            stable = {key: value for key, value in context.items() if key not in {"segment", "run_nonce"}}
            expected = {key: value for key, value in control["context"].items() if key not in {"segment", "run_nonce"}}
            if name != EXECUTOR_NAME or observed != configuration or stable != expected:
                raise guard.GuardError("typed 回归 factory 被用于其他配置或世界")
            return ControlledExecutor(observed, context, control_path)

        def verify(store: Any, content: Any, origin: Any) -> execution.VerifiedSource:
            if store.root.resolve() != (root / configuration["candidate"]["qualification"]["store_relative"]).resolve():
                raise guard.GuardError("typed 来源被用于其他 Store")
            source = execution.VerifiedSource(**reader.verify_origin_evidence(content, origin))
            return delivery.compose_parent_publication_source(store, content, origin, source)

        def verify_batch(store: Any, items: list[dict[str, Any]]) -> list[execution.VerifiedSource]:
            if store.root.resolve() != (root / configuration["candidate"]["qualification"]["store_relative"]).resolve():
                raise guard.GuardError("typed 批量来源被用于其他 Store")
            return [delivery.compose_parent_publication_source(store, item["content"], item["origin"], execution.VerifiedSource(**row))
                for item, row in zip(items, reader.verify_origin_evidence_batch(items))]

        verify.verify_batch = verify_batch

        from tools.official_client_control.store import ControlStore
        from tools.official_client_capture.tests.claude_fault_chain_world import _read_only_fact_index

        expected_store_root = (root / configuration["candidate"]["qualification"]["store_relative"]).resolve()
        original_handoff, original_validate_plan = delivery._vc5_handoff, execution.validate_plan
        original_validate_graph = execution.validate_evidence_graph
        original_validate_package = execution.validate_delivery_package
        original_qualification_view = executor._QualificationStoreView
        original_source_scope, original_verified_source = execution._source_batch_scope, execution._verified_source
        read_store: ContextVar[Any] = ContextVar("typed_vc6_read_once_store", default=None)
        local_reads: ContextVar[Any] = ContextVar("typed_vc6_local_source_reads", default=None)

        def local_source_material(source: Any) -> Any:
            """与共享整图使用同一准确类型规则；不从任意 Mapping 属性推断绑定能力。"""
            return stops._source_original_material(source, root, error=guard.GuardError)

        @contextlib.contextmanager
        def source_batch_scope(store: Any, items: list[dict[str, Any]]) -> Iterator[None]:
            # 原工厂先取得并验证精确清单；只为同根只读活批次登记读取生命周期。
            # 可写消费者的审核会合法设置发布目录权限，不能冻结其目录元数据。
            # 它们完整沿原来源读取路径，不能借只读优化跨越目录或产物写入。
            with original_source_scope(store, items):
                batch, previous = execution._SOURCE_BATCH.get(), local_reads.get()
                if (batch is None or batch.active is not True or batch.store is not store
                        or store.read_only is not True
                        or store.root != expected_store_root
                        or (type(store) is not ControlStore and store is not read_store.get())):
                    yield
                    return
                if previous is not None and previous[0] is batch:
                    previous[1].require_active()
                    yield
                    return
                snapshot = _LocalActiveFileReads(root, guard.GuardError)
                materials = {}
                token = local_reads.set((batch, snapshot, materials))
                try:
                    # 原 provider 已验真每一项；从精确类型取得绑定闭集后仍
                    # 按其真实 Mapping 首读。父 publication／seed 不漏出
                    # 首尾快照，元数据也不代替原件或父 Store 发布证明。
                    selected = {}
                    for key, source in batch.sources.items():
                        material = local_source_material(source)
                        if material is None:
                            continue
                        bindings, originals = material
                        # producer 清单可变；首轮保存已验绑定与提供者，中途不得增减或换件。
                        materials[key] = (source, originals, bindings)
                        for path, binding in bindings.items():
                            previous = selected.setdefault(path, (binding, originals))
                            if previous[0] != binding:
                                raise guard.GuardError("typed 批量来源对同一路径声明不同原件")
                    snapshot.read_all([(root / path, lambda _actual, name=path, original=originals: original[name])
                        for path, (_binding, originals) in sorted(selected.items())])
                    yield
                finally:
                    try:
                        try:
                            # 可变 producer 在最后一次消费后也不得换件或增减清单。
                            for key, (source, originals, bindings) in materials.items():
                                material = local_source_material(source)
                                if (batch.sources.get(key) is not source or material is None
                                        or material[1] is not originals or material[0] != bindings):
                                    raise guard.GuardError("typed 来源返回前精确清单、提供者或绑定闭集漂移")
                        finally:
                            # 收尾仍在原 batch 失效和清空之前完整实读，异常路径同样复核。
                            snapshot.verify_full()
                    finally:
                        snapshot.close()
                        materials.clear()
                        local_reads.reset(token)

        def verified_source(store: Any, content: Any, origin: Any) -> execution.VerifiedSource:
            current, batch = local_reads.get(), execution._SOURCE_BATCH.get()
            if current is not None:
                current[1].require_active()
            if (current is None or current[0] is not batch or batch.active is not True
                    or batch.store is not store or store.root != expected_store_root):
                return original_verified_source(store, content, origin)
            verified = batch.sources.get(canonical_sha256(execution._source_request(content, origin)))
            material = local_source_material(verified)
            if material is None:
                if canonical_sha256(execution._source_request(content, origin)) in current[2]:
                    raise guard.GuardError("typed 本次已验来源失去精确原件清单")
                return original_verified_source(store, content, origin)
            # 精确本地清单在只读批次中首尾完整实读，中间逐次核全清单坐标；
            # 原 Mapping 不替换，所以连续性、Profile／门禁投影和 typed 的实际
            # 绑定仍由原读者当前实读。可写批次保留每次完整实读，不提前授予输出。
            execution._origin(origin)
            verifier = execution._source_verifier(str(origin["authority"]))
            if verifier is None or batch.verifiers.get(origin["authority"]) is not verifier:
                raise execution.ControlError("本次批量来源的 Store、通道登记或生命周期已变化")
            core = {key: value for key, value in content.items() if key != "origin"}
            bindings, originals = material
            registered = current[2].get(canonical_sha256(execution._source_request(content, origin)))
            if (registered is None or registered[0] is not verified or registered[1] is not originals
                    or registered[2] != bindings):
                raise guard.GuardError("typed 本次来源精确清单、提供者或绑定闭集漂移")
            snapshot = current[1]
            snapshot.read_all([(root / path, lambda _actual, name=path: originals[name])
                for path in bindings], current=store.read_only is not True)
            execution._check_verified_source(verified, core, origin)
            if verified.guard_continuity is not None:
                from tools.official_client_control.canonical import parse_json_bytes
                identity = parse_json_bytes(verified.guard_continuity.channel_identity_bytes)
                if not guard.owner_alive({key: identity[key]
                        for key in ("boot_id", "pid", "process_start", "process_group_id")}):
                    raise execution.ControlError("批量取证后的当前守卫实例已消失或 PID 被重用")
            for binding in execution.typed_bindings(content):
                execution._source_bytes(verified, binding)
            snapshot.verify_metadata()
            return verified

        class ScopedQualificationStoreView(original_qualification_view):
            """同次纯读的资格视图使用既有事实索引；原过滤与原件校验保持完整。"""

            def __init__(self, store: Any):
                self._scoped_index = None
                indexed = read_store.get()
                batch = execution._SOURCE_BATCH.get()
                if batch is not None and (batch.active is not True or batch.store is not indexed):
                    indexed = None
                if (indexed is not None and type(store) is ControlStore
                        and store.read_only is True and store.root == expected_store_root):
                    # 新句柄仍由原构造器验证根；这里只借同方法活索引，不复用资格通过结论。
                    # 索引保留父消费者的权限闭集，资格视图仍实读原字节并做自身末尾复核。
                    indexed.require_active()
                    self._scoped_index = indexed
                    store = indexed
                super().__init__(store)

            def require_current(self) -> None:
                # 资格视图也持有事实副本；底层索引到期后不能从自己的副本返回旧记录。
                if self._scoped_index is not None:
                    self._scoped_index.require_active()

            def load_campaign(self, campaign_id: str) -> dict[str, Any]:
                self.require_current()
                return super().load_campaign(campaign_id)

            def _fact_snapshot(self, campaign_id: str) -> list[dict[str, Any]]:
                self.require_current()
                return super()._fact_snapshot(campaign_id)

            def verify_fact_snapshots(self) -> None:
                self.require_current()
                super().verify_fact_snapshots()

        @contextlib.contextmanager
        def index_scope(store: Any) -> Iterator[Any]:
            # 普通完整 replay 另建索引；只登记原工厂实际产出的同根活索引，不认鸭子属性。
            previous = read_store.get()
            eligible = (store.read_only is True and store.root == expected_store_root
                        and (type(store) is ControlStore or store is previous))
            with _read_only_fact_index(store) as indexed:
                if not eligible:
                    yield indexed
                    return
                indexed.require_active()
                token = read_store.set(indexed)
                try:
                    yield indexed
                finally:
                    read_store.reset(token)

        def read_once(store: Any, consume: Any) -> Any:
            # 只优化本回归世界的一次只读消费；嵌套批次与其它句柄沿原接线。
            if (type(store) is not ControlStore or store.root != expected_store_root
                    or store.read_only is not False or execution._SOURCE_BATCH.get() is not None
                    or execution._QUALIFICATION_READ_BATCH.get() is not None):
                return consume(store)
            reader = ControlStore.open_read_only(store.root)
            # 只读打开不得放宽原可写消费者的路径权限闭集。
            reader._root_modes, reader._file_modes = store._root_modes, store._file_modes
            with _read_only_fact_index(reader) as indexed:
                indexed._root_modes, indexed._file_modes = reader._root_modes, reader._file_modes
                token = read_store.set(indexed)
                try:
                    result = consume(indexed)
                    indexed.verify_fact_snapshots()
                finally:
                    read_store.reset(token)
            return result

        def handoff(store: Any, campaign: Any, candidate_id: str) -> dict[str, Any]:
            return read_once(store, lambda indexed: original_handoff(indexed, campaign, candidate_id))

        def validate_plan(store: Any, plan: Any, *, current_execution: bool = False) -> None:
            # 原 Plan 消费者自带精确来源批次和资格批次；用途与当前身份校验原样传递。
            read_once(store, lambda indexed: original_validate_plan(indexed, plan,
                current_execution=current_execution))

        def validate_graph(store: Any, payload: Any, *, current_execution: bool = False,
                           frozen_configuration_ref: Any = None) -> execution.VerifiedSource:
            # 原图读取及来源导出仍完整执行；返回前复核并销毁索引，之后才允许原封存写入。
            return read_once(store, lambda indexed: original_validate_graph(indexed, payload,
                current_execution=current_execution, frozen_configuration_ref=frozen_configuration_ref))

        def validate_package(store: Any, package: Any, *, current_execution: bool = False) -> None:
            # 原包先建立来源／资格批次；在入口包住索引，嵌套 Plan／图才使用同一次读侧。
            read_once(store, lambda indexed: original_validate_package(indexed, package,
                current_execution=current_execution))

        with mock.patch.dict(delivery.ENVIRONMENT_EXECUTORS, {EXECUTOR_NAME: factory}, clear=True), \
                mock.patch.dict(execution.SOURCE_VERIFIERS, {executor.AUTHORITY: verify}, clear=True), \
                mock.patch.object(delivery, "_vc5_handoff", handoff), \
                mock.patch.object(execution, "validate_plan", validate_plan), \
                mock.patch.object(execution, "validate_evidence_graph", validate_graph), \
                mock.patch.object(execution, "validate_delivery_package", validate_package), \
                mock.patch.object(execution, "_source_batch_scope", source_batch_scope), \
                mock.patch.object(execution, "_verified_source", verified_source), \
                mock.patch.object(executor, "_QualificationStoreView", ScopedQualificationStoreView), \
                mock.patch.object(stops, "_read_only_fact_index", index_scope), \
                _recorded_read_scope(root):
            yield


def install(control_path: str) -> None:
    """只供明示的测试子进程接缝；正式 CLI 没有这条入口。"""

    if os.environ.get(supervisor.CAMPAIGN_RUN_ACTION_ID_ENV) not in {delivery.RUN_ACTION_ID, delivery.FINALIZE_ACTION_ID}:
        raise guard.GuardError("typed 接缝仅允许两个明确的 VC-6 测试动作")
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(source_scope(Path(control_path)))
        # 调用方（test_claude_upgrade_vc6_batch）显式给出缓存目录时，动作子进程与测试进程、守卫共用
        # 「相同输入的独立 reader 结果」复用；没有这个环境变量（其他所有测试）则完全不启用，见 claude_gate_reader_memo。
        memo_directory = os.environ.get(gate_reader_memo.MEMO_ENV)
        if memo_directory:
            stack.enter_context(gate_reader_memo.gate_reader_memo(Path(memo_directory)))
        # 同理：受管工具清单快照（492 个文件逐个读取加 sha256）在整条链里被反复计算上千次而输入不变。调用方显式给出统计
        # 目录时，动作子进程在核对文件状态没变的前提下复用本进程已算好的结果，见 claude_tool_snapshot_memo。
        snapshot_directory = os.environ.get(tool_snapshot_memo.MEMO_ENV)
        if snapshot_directory:
            stack.enter_context(tool_snapshot_memo.tool_snapshot_memo(Path(snapshot_directory)))
    except BaseException:
        stack.close()
        raise
    _STACKS.append(stack)
    atexit.register(stack.close)


USERCUSTOMIZE = f'''"""仅指定 VC-6 测试动作安装回归来源。"""
import os
import sys
if os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") in os.environ.get("{SEAM_ACTIONS_ENV}", "").split(","):
    sys.path.insert(0, os.environ["{SEAM_REPOSITORY_ENV}"])
    from tools.official_client_capture.tests import claude_typed_vc6_fixture
    claude_typed_vc6_fixture.install(os.environ["{SEAM_CONTROL_ENV}"])
'''


def subprocess_environment(inject_dir: Path, control_path: Path, actions: list[str]) -> dict[str, str]:
    if not actions or any(action not in {delivery.RUN_ACTION_ID, delivery.FINALIZE_ACTION_ID} for action in actions):
        raise ValueError("typed 注入只允许两个明确的 VC-6 测试动作")
    guard.private_directory(inject_dir)
    (inject_dir / "usercustomize.py").write_text(USERCUSTOMIZE, encoding="utf-8")
    return {"PYTHONPATH": str(inject_dir), SEAM_CONTROL_ENV: str(control_path),
            SEAM_ACTIONS_ENV: ",".join(actions), SEAM_REPOSITORY_ENV: str(REPOSITORY_ROOT)}


def _fixture_qualification_gate_root(store: Any, qualification: dict, data_root: Path) -> Path:
    """只从同源封存 accept 取得门禁坐标，并实读它绑定的原收据。

    外部门禁根可以位于数据根之外，不能按 Campaign、候选或评估基线猜目录。
    这里不创建目录或改写原件；完整资格图、权限范围与原 reader 仍由既有
    来源准备和导入合同核对。
    """
    from tools.official_client_capture import claude_qualification_material_source as source_module

    source_module._check_four_roots(store, qualification)
    parent_binding = qualification["sealed_result_binding"]
    parent_raw = guard.read_bytes(guard.check_binding(data_root, parent_binding))
    if len(parent_raw) != parent_binding["bytes"] or sha256_bytes(parent_raw) != parent_binding["sha256"]:
        raise guard.GuardError("typed 门禁坐标的封存 accept 原件漂移")
    parent = guard.parse_json_bytes(parent_raw, "typed 封存 accept")
    if (parent.get("schema_version") != "claude-upgrade-acceptance/v1"
            or parent.get("candidate_id") != qualification["candidate_id"]
            or parent.get("production_state") != "not_activated"
            or parent.get("store", {}).get("acceptance_ref") != qualification["acceptance_ref"]
            or parent.get("store", {}).get("validation_completed_ref") != qualification["validation_ref"]):
        raise guard.GuardError("typed 门禁坐标的封存 accept 与资格四根不同源")
    external = guard.exact(parent.get("external_gate"), {"evidence_root", "receipt"}, "typed 封存门禁父域")
    locator = external["evidence_root"]
    if not isinstance(locator, str):
        raise guard.GuardError("typed 封存门禁根必须为规范绝对路径")
    root = Path(locator)
    if not root.is_absolute() or str(root) != locator or ".." in root.parts:
        raise guard.GuardError("typed 封存门禁根必须为规范绝对路径")
    guard.check_binding(root, external["receipt"])
    return root


_CANDIDATE_EVIDENCE_LEVELS = frozenset({"attempts", "seal", "revisions"})


def _is_candidate_evidence_document(binding_path: str) -> bool:
    """候选证据级文档的判定：路径形如 evidence/campaigns/<c>/candidates/<cand>/(attempts|seal|revisions)/…。

    这类文档（attempt、恢复、client-evidence、封存清单等）里的绑定路径相对 attempt 根、恢复根或所在目录，不是数据根；
    生产里由读集投影逐类展开。
    """

    parts = PurePosixPath(binding_path).parts
    return ("candidates" in parts and len(parts) > parts.index("candidates") + 2
            and parts[parts.index("candidates") + 2] in _CANDIDATE_EVIDENCE_LEVELS)


@contextlib.contextmanager
def synthetic_gate_subject_scope(enabled: bool) -> Iterator[None]:
    """合成 seam 世界的门禁 subject 只做纯推导，不请求严格读集归档。

    ``test_claude_upgrade_vc6_batch`` 的 VC-5 链世界（Campaign、官方来源、候选源码都是合成的）不具备严格读集
    归档要求的真实布局：没有 create_campaign 写出的 bootstrap 控制副本，官方 capture 绑定用 Campaign 坐标，
    候选源码根也不在数据根内。subject 摘要本身仍走原函数推导，导入与消费两侧同值；严格归档由
    ``test_claude_sealed_gate_materials`` 与 ARM64 真实链覆盖。只在显式声明的合成世界打开，
    真实候选世界不得使用；父进程由 ``source_scope`` 持有，动作子进程由 ``install`` 经同一个 ``source_scope`` 进入。
    """

    if not enabled:
        yield
        return
    from tools.official_client_capture import claude_qualification_material_source as source_module

    real = source_module.qualification_gate_subject

    def pure(store: Any, qualification: Any, data_root: Any, parent: Any, *, read_bindings: Any = None,
             read_projections: Any = None, **kwargs: Any) -> Any:
        del read_bindings, read_projections
        return real(store, qualification, data_root, parent, **kwargs)

    from tools.official_client_control import sealed_gate_bindings as bindings_module

    real_projector = bindings_module._gate_binding_projector

    def projector_with_candidate_evidence_leaves(proof: Any) -> Any:
        inner = real_projector(proof)

        def project(parent_binding: Any, raw: bytes) -> Any:
            projected = inner(parent_binding, raw)
            if projected is None and _is_candidate_evidence_document(str(parent_binding["path"])):
                # 合成世界没有读集投影：候选证据级文档里的绑定相对 attempt／恢复根或所在目录，不是数据根，
                # 朴素展开必然找不到；生产里由读集投影展开，这里当叶子（文档自身仍完整实读验字节）。
                return []
            return projected

        return project

    with mock.patch.object(source_module, "qualification_gate_subject", pure), \
            mock.patch.object(bindings_module, "_gate_binding_projector", projector_with_candidate_evidence_leaves):
        yield


def prepare_fixture_qualification_materials(store: Any, configuration: dict, data_root: Path, *, gate_root: Path,
                                            synthetic_gate_subject: bool = False) -> dict:
    """本轮 Mac 原 reader 的 authority 延续到来源准备和完整导入结束。"""
    with fixture_mac_source_scope(configuration, data_root), synthetic_gate_subject_scope(synthetic_gate_subject):
        return _prepare_fixture_qualification_materials(store, configuration, data_root, gate_root=gate_root)


def _prepare_fixture_qualification_materials(store: Any, configuration: dict, data_root: Path, *, gate_root: Path,
                                            consume_sources: Any = None) -> dict:
    """只在显式本机夹具布局里取得来源；正式 registry 与身份均保持关闭。"""
    from tools.official_client_control import receipts
    from tools.official_client_control.store import ControlStore
    from tools.official_client_control.canonical import canonical_sha256
    from tools.official_client_capture import claude_gate_material_worker as reader
    from tools.official_client_capture import claude_qualification_material_source as source_module
    from tools.official_client_capture import claude_qualification_material_import as importer
    # 夹具刚用可写句柄完成回退事实；资格重放必须另开同根只读句柄。
    # 不改调用方的权限旗标，也不改原 Store 的字节或文件权限。
    qualification_store = ControlStore.open_read_only(store.root)
    repository = receipts._tool_repository_root()
    target_root = repository / "tools/ci"
    if consume_sources is None and not target_root.exists() and repository != REPOSITORY_ROOT:
        # 隔离 Store 测试树只复制两受管根；五个历史 reader 属于固定外根。
        # 仅复制缺失的隔离树模块，不改变任何资格、历史收据或正式安装。
        import shutil
        shutil.copytree(REPOSITORY_ROOT / "tools/ci", target_root, ignore=shutil.ignore_patterns("__pycache__"))
    identity = reader.compute_gate_reader_identity(repository, formed_context={"fixture_only": True})
    sources = []
    for role in ("candidate", "rollback"):
        q = configuration[role]["qualification"]
        provenance = {"fixture_contract": "qualified-gate-fixture/v1" if role == "candidate" else "explicit-no-gate-rollback/v1",
                      "formation_context": {"fixture_only": True}}
        capabilities = ()
        if role == "candidate":
            capability = source_module.freeze_root_capability(gate_root, "gate_evidence", authority=source_module.FIXTURE_AUTHORITY,
                reference_id="fixture-qualified-gate", reference_sha256=canonical_sha256(provenance),
                allowed_relative_paths=[path.relative_to(gate_root).as_posix() for path in sorted(gate_root.rglob("*")) if path.is_file()])
            capabilities = (capability,)
        intake = source_module.VerifiedIntakeContext(source_module.FIXTURE_AUTHORITY, "regression_fixture", data_root,
            repository, provenance, capabilities, identity, source_module._TOKEN)
        sources.append(source_module.prepare_qualification_source(qualification_store, q, role=role, intake_context=intake))

    def fixture_resolver(actual_store, qualification, reference, *, purpose):
        if reference not in sources or reference.qualification != qualification or reference.intake.authority != source_module.FIXTURE_AUTHORITY:
            raise guard.GuardError("本机夹具来源引用不属于本轮实际取得的能力")
        source_module._check_four_roots(actual_store, qualification)
        return reference

    registrations = {(source_module.FIXTURE_AUTHORITY, purpose): fixture_resolver for purpose in source_module.PURPOSES}
    with mock.patch.dict(source_module.QUALIFICATION_SOURCE_RESOLVERS, registrations):
        if consume_sources is not None:
            # 仅显式后继准备沿用本轮同一两角色来源与临时 registry；不另造来源逻辑。
            return consume_sources(qualification_store, sources)
        return importer.prepare_qualification_material_import(qualification_store, sources, managed_data_root=data_root)


def close_recorded_guards() -> None:
    """原 runner 全链最终消费成功后调用；单方法完成或失败不得调用此出口。

    只沿本方法登记的准确坐标重验原配置、Store 四根及完整活身份，再有界终止。
    不扫描宿主进程、不创建守卫、不改交付原件或源身份；冷原守卫只作已退出处理。
    """

    index = stops.load_state('typed-live-guards') or {'records': []}
    if stops.record_root() is None:
        raise guard.GuardError('清理原记录守卫缺少本方法记录坐标')
    with mock.patch.dict(os.environ, {stops.METHOD_RESUME_ENV: '1'}):
        for name in index['records']:
            saved = stops.load_state(name)
            if saved is None or not saved.get('guard_identity'):
                raise guard.GuardError('原守卫清理索引缺配置或完整身份，保留现场')
            if name != 'typed-vc6:' + str(saved['campaign']):
                raise guard.GuardError('原守卫清理索引不属于准确 Campaign')
            instance = TypedDeliveryFixture(saved['campaign'], saved['candidate_id'], control_path=saved['control_path'])
            instance.close(force=True)


def _read_unstarted_fixture_world(data: Path, campaign: Path, control_path: Path, record_name: str) -> tuple[dict, dict]:
    """只承接原游标许可、未启动守卫且未发布配置的首次初始化窗口。

    基础配置、文件绑定和零动作现场全部从原坐标实读；缺件或已有守卫痕迹
    都拒绝，不删除半产物，也不再次调用 fixture_world 覆盖原输入。
    """

    if not os.environ.get(stops.METHOD_RESUME_ENV) or not stops.initialization_allowed(record_name):
        raise guard.GuardError('承接未完成初始化缺原游标许可')
    forbidden = [data / 'guard', campaign / 'control/typed-vc6-configuration.json', control_path,
        control_path.with_name('typed-vc6-guard.log'), control_path.with_name('typed-vc6-calls.jsonl'),
        control_path.with_name('typed-vc6-physical-calls.jsonl')]
    if any(path.exists() or path.is_symlink() for path in forbidden):
        raise guard.GuardError('首次初始化已有发布配置或守卫痕迹，不能按未启动现场承接')
    configuration = executor.validate_configuration(guard.read_json(data / 'inputs/configuration.json'))
    if (configuration['schema_version'] != executor.CONFIGURATION_SCHEMA
            or configuration['source_scope'] != 'regression_fixture'
            or configuration['paths']['guard_root_relative'] != 'guard'):
        raise guard.GuardError('首次初始化只能承接原基础文件夹具配置')
    for binding in guard.all_bindings(configuration):
        guard.check_binding(data, binding)
    docker = configuration['runtime']['docker_binding']
    docker_path = Path(docker['absolute_path'])
    try:
        docker_relative = docker_path.relative_to(data).as_posix()
    except ValueError as error:
        raise guard.GuardError('首次初始化的文件执行器越出原 data_root') from error
    guard.check_binding(data, {'path': docker_relative, 'sha256': docker['sha256'], 'bytes': docker['bytes']})
    if docker_path.stat().st_mode & 0o777 != 0o700:
        raise guard.GuardError('首次初始化的文件执行器权限发生漂移')
    environment = {'host_id': 'ARM64', 'architecture': 'arm64',
        'compose_sha256': configuration['paths']['compose_binding']['sha256'], 'environment_sha256': 'c' * 64,
        'configuration_sha256': canonical_sha256(configuration), 'network_sha256': 'd' * 64}
    expected_state = {'image_digest': configuration['rollback']['image_digest'],
        'runtime_profile_sha256': configuration['rollback']['runtime_profile_sha256'],
        'environment': environment, 'switch_count': 0, 'restore_count': 0}
    if (guard.read_json(data / 'state.json') != expected_state
            or guard.read_json(data / 'fault.json') != {'execute': 'normal', 'restore': 'normal', 'freeze': 'normal'}):
        raise guard.GuardError('首次初始化现场已发生动作或不再是原基础配置')
    return configuration, environment


def _read_initial_rollback_qualification(store: Any, data: Path, configuration: dict,
                                         candidate_qualification: dict) -> dict:
    """只读重验同 Store 已封存的回滚资格，不重新生产 Campaign 或收据。"""

    from tools.official_client_capture import claude_qualification_material_source as materials
    from tools.official_client_control.store import ControlStore

    store = ControlStore.open_read_only(store.root)
    sealed_path = data / 'control/typed-vc6-rollback/sealed-result.json'
    sealed = guard.read_json(sealed_path)
    if (sealed.get('schema_version') != 'claude-upgrade-acceptance/v1'
            or sealed.get('candidate_id') != 'synthetic-candidate-baseline'
            or sealed.get('production_state') != 'not_activated'):
        raise guard.GuardError('首次初始化缺原回滚资格的独立封存终态')
    references = guard.exact(sealed.get('store'), {'acceptance_ref', 'validation_completed_ref'}, '原回滚终态引用')
    validation = store.load_fact(references['validation_completed_ref'])
    payload = validation['payload']
    qualification = {'store_relative': store.root.relative_to(data).as_posix(), 'external_root_relative': '.',
        'campaign_id': 'typed-rollback-' + canonical_sha256(candidate_qualification)[:32],
        'candidate_id': sealed['candidate_id'], 'candidate_ref': payload.get('candidate_ref'),
        'build_receipt_ref': payload.get('candidate_build_receipt_ref'),
        'validation_ref': references['validation_completed_ref'], 'acceptance_ref': references['acceptance_ref'],
        'sealed_result_binding': guard.binding(data, sealed_path)}
    candidate, _acceptance = materials._check_four_roots(store, qualification)
    build = store.load_receipt(qualification['build_receipt_ref'])
    rollback = configuration['rollback']
    if (candidate['payload'].get('image_digest') != rollback['image_digest']
            or build.get('image', {}).get('image_digest') != rollback['image_digest']
            or build.get('profile_identity', {}).get('profile_digest') != rollback['runtime_profile_sha256']):
        raise guard.GuardError('原回滚资格与首次初始化的镜像或 Profile 不一致')
    executor.replay_qualification_graph(store, qualification, data, data)
    return qualification


class TypedDeliveryFixture:
    """已完成 VC-5 的原世界接入 typed 文件现场；持有守卫直到最后一次重放。"""

    def __init__(self, campaign: Path, candidate_id: str, *, control_path: Path | None = None,
                 synthetic_gate_subject: bool = False):
        self.campaign = campaign.resolve(strict=True)
        self.synthetic_gate_subject = synthetic_gate_subject
        self.candidate_id = candidate_id
        self.process: multiprocessing.Process | None = None
        self.recorded_pid: int | None = None
        self.saved_guard_identity: dict[str, Any] | None = None
        self.restored_guard = False
        self.retained = stops.record_root() is not None
        self.record_name = 'typed-vc6:' + str(self.campaign)
        saved = stops.load_state(self.record_name)
        if saved is not None:
            if not os.environ.get(stops.METHOD_RESUME_ENV):
                raise guard.GuardError('旧 typed 现场只能经原方法显式恢复，不能重新建场')
            self._restore(saved, control_path=control_path)
            return
        if os.environ.get(stops.METHOD_RESUME_ENV) and not stops.initialization_allowed(self.record_name):
            raise guard.GuardError('原 typed 现场缺完整配置与守卫停点，不能事后建场')
        manifest = upgrade.load_campaign_manifest(campaign)
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, manifest)
        self.data = capture._host_data_root(campaign)
        if not execution.requires_current_contract(store, store_campaign):
            raise guard.GuardError("typed 共用夹具要求当前 policy 4，不能降为历史交付")
        handoff = delivery._vc5_handoff(store, store_campaign, candidate_id)
        self.control_path = control_path or self.data / "control/typed-vc6-control.json"
        # 已获原游标许可的半初始化只读原件；其余仍要求首次建场坐标为空。
        existing = [relative for relative in ("inputs", "guard", "state.json", "fault.json")
                    if (self.data / relative).exists() or (self.data / relative).is_symlink()]
        continuing_initialization = bool(existing) and bool(os.environ.get(stops.METHOD_RESUME_ENV))
        if continuing_initialization:
            configuration, environment = _read_unstarted_fixture_world(
                self.data, campaign, self.control_path, self.record_name)
        else:
            if existing:
                raise guard.GuardError(f"typed 文件现场坐标已存在：{existing[0]}")
            configuration, _, environment = fixture_world(self.data)
        profile_stage = upgrade._load_stage_result(campaign, "stage-profile")
        staged = release.load_staged_release(campaign, profile_stage["staged_release"])
        profile_path = campaign / release.STAGED_RELEASE_ROOT / staged["release_entry"]["profile"]["path"]
        if sha256_file(profile_path) != handoff["runtime_profile_sha256"]:
            raise guard.GuardError("原候选暂存 Profile 与 VC-5 构建不一致")
        profile_copy = self.data / "inputs/current-qualified-profile.json"
        if continuing_initialization:
            if guard.read_bytes(profile_copy) != guard.read_bytes(profile_path):
                raise guard.GuardError('原候选 Profile 副本与暂存原件不一致，禁止覆盖承接')
        else:
            guard.write_once(profile_copy, guard.read_bytes(profile_path))
        configuration["candidate"].update(image_digest=handoff["candidate_image_digest"],
            runtime_profile_sha256=handoff["runtime_profile_sha256"], profile_binding=guard.binding(self.data, profile_copy))
        store_relative = store.root.relative_to(self.data).as_posix()
        _, accept_path = upgrade._stage_path(campaign, "accept", candidate_id)
        candidate_qualification = {"store_relative": store_relative, "external_root_relative": ".",
            "campaign_id": store_campaign["campaign_id"], "candidate_id": candidate_id,
            "candidate_ref": handoff["candidate_ref"], "build_receipt_ref": handoff["candidate_build_receipt_ref"],
            "validation_ref": handoff["validation_completed_ref"], "acceptance_ref": handoff["acceptance_ref"],
            "sealed_result_binding": guard.binding(self.data, accept_path)}
        configuration["candidate"]["qualification"] = candidate_qualification
        if continuing_initialization:
            configuration["rollback"]["qualification"] = _read_initial_rollback_qualification(
                store, self.data, configuration, candidate_qualification)
        else:
            rollback_root = self.data / "control/typed-vc6-rollback"
            guard.private_directory(rollback_root)
            rollback = DeliveryCampaign(rollback_root, schema="v2", store=store, external_root=self.data,
                                        external_prefix="control/typed-vc6-rollback/external")
            rollback.materials = configuration
            rollback.campaign_id = "typed-rollback-" + canonical_sha256(candidate_qualification)[:32]
            rollback.bootstrap_and_campaign(platforms=["linux/arm64"], campaign_purpose="validation_only")
            rollback.discovery_and_evidence()
            rollback.profile_objects()
            rollback.select_profile("rollback")
            rollback.profile_approve("validation_only")
            rollback.strict_validate(tag="baseline")
            sealed = {"schema_version": "claude-upgrade-acceptance/v1", "candidate_id": "synthetic-candidate-baseline",
                "store": {"acceptance_ref": rollback.references["acceptance-baseline"],
                          "validation_completed_ref": rollback.references["validation-completed-baseline"]},
                "production_state": "not_activated"}
            sealed_path = rollback_root / "sealed-result.json"
            guard.write_json_once(sealed_path, sealed)
            configuration["rollback"]["qualification"] = {"store_relative": store_relative, "external_root_relative": ".",
                "campaign_id": rollback.campaign_id, "candidate_id": sealed["candidate_id"],
                "candidate_ref": rollback.references["candidate-baseline"],
                "build_receipt_ref": rollback.references["candidate-build-receipt-baseline"],
                "validation_ref": rollback.references["validation-completed-baseline"],
                "acceptance_ref": rollback.references["acceptance-baseline"],
                "sealed_result_binding": guard.binding(self.data, sealed_path)}
        from tools.official_client_control.qualification_material_import import requires_qualification_materials
        if requires_qualification_materials(store, store_campaign):
            # 新夹具在配置与现场冻结前准备原件；不给已冻结旧 world 补叶子。
            gate_root = _fixture_qualification_gate_root(store, candidate_qualification, self.data)
            configuration.update(schema_version=executor.MATERIAL_CONFIGURATION_SCHEMA,
                qualification_materials_binding=prepare_fixture_qualification_materials(store, configuration,
                    self.data, gate_root=gate_root, synthetic_gate_subject=synthetic_gate_subject))
        self.configuration = executor.validate_configuration(configuration)
        self.configuration_path = campaign / "control/typed-vc6-configuration.json"
        guard.write_json_once(self.configuration_path, configuration)
        self.configuration_binding = guard.binding(self.data, self.configuration_path)
        delivery_id = f"{candidate_id}-d1"
        self.context = {"campaign_id": store_campaign["campaign_id"], "candidate_id": candidate_id,
            "delivery_id": delivery_id, "segment": "dr0", "run_nonce": delivery_id + "-dr0",
            "data_root": str(self.data), "configuration_ref": self.configuration_binding}
        environment.update(architecture="linux/arm64", configuration_sha256=canonical_sha256(configuration),
                           environment_sha256=canonical_sha256(fixture_projection()))
        state = guard.read_json(self.data / "state.json")
        state["environment"] = environment
        guard.atomic_json(self.data / "state.json", state)
        self.control_path = control_path or self.data / "control/typed-vc6-control.json"
        guard.write_json_once(self.control_path, {"schema_version": CONTROL_SCHEMA, "fixture_only": True,
            "formal_version_semantics_certified": False, "context": self.context,
            "calls": str(self.control_path.with_name("typed-vc6-calls.jsonl")),
            "physical_calls": str(self.control_path.with_name("typed-vc6-physical-calls.jsonl")),
            "failures": [], "kill_at": None, "crash_at": None, "failed_gates": [],
            **({"synthetic_gate_subject": True} if synthetic_gate_subject else {})})
        self._save_record()

    def _save_record(self) -> None:
        """首次记录固定坐标；恢复活守卫时同值只读，不改写原停点。"""

        payload = {'campaign': self.campaign, 'candidate_id': self.candidate_id,
            'data': self.data, 'configuration_binding': self.configuration_binding,
            'control_path': self.control_path, 'context': self.context,
            'guard_identity': self.saved_guard_identity}
        if self.restored_guard:
            if stops.load_state(self.record_name) != payload:
                raise guard.GuardError('恢复原typed停点与当前活身份不一致，禁止改写')
            index = stops.load_state('typed-live-guards')
            if index is None or self.record_name not in index['records']:
                raise guard.GuardError('恢复原typed停点缺活守卫索引，禁止补写')
            return
        stops.save_state(self.record_name, payload)
        if self.saved_guard_identity is not None and stops.record_root() is not None:
            index = stops.load_state('typed-live-guards') or {'records': []}
            if self.record_name not in index['records']:
                stops.save_state('typed-live-guards', {'records': [*index['records'], self.record_name]})

    def _restore(self, saved: dict[str, Any], *, control_path: Path | None) -> None:
        """只读原 control/config/VC-5 四根；不调用首次建场、回滚资格或 import。"""

        self.data = capture._host_data_root(self.campaign)
        if (saved['campaign'] != self.campaign or saved['candidate_id'] != self.candidate_id
                or saved['data'] != self.data or (control_path is not None and control_path != saved['control_path'])):
            raise guard.GuardError('typed 停点的 Campaign、候选或受管根已改变')
        self.control_path = saved['control_path']
        control = _control(self.control_path)
        self.context = control['context']
        if self.context != saved['context'] or self.context['data_root'] != str(self.data):
            raise guard.GuardError('typed 停点的原固定上下文已改变')
        self.configuration_binding = self.context['configuration_ref']
        if self.configuration_binding != saved['configuration_binding']:
            raise guard.GuardError('typed 停点的原配置绑定已改变')
        self.configuration_path = guard.check_binding(self.data, self.configuration_binding)
        self.configuration = executor.validate_configuration(guard.read_json(self.configuration_path))
        manifest = upgrade.load_campaign_manifest(self.campaign)
        store, campaign = claude_upgrade_store.open_campaign_store(self.campaign, manifest)
        handoff = delivery._vc5_handoff(store, campaign, self.candidate_id)
        qualification = self.configuration['candidate']['qualification']
        expected = {'store_relative': store.root.relative_to(self.data).as_posix(), 'external_root_relative': '.',
            'campaign_id': campaign['campaign_id'], 'candidate_id': self.candidate_id,
            'candidate_ref': handoff['candidate_ref'], 'build_receipt_ref': handoff['candidate_build_receipt_ref'],
            'validation_ref': handoff['validation_completed_ref'], 'acceptance_ref': handoff['acceptance_ref']}
        if (any(qualification[key] != value for key, value in expected.items())
                or self.configuration['candidate']['image_digest'] != handoff['candidate_image_digest']
                or self.configuration['candidate']['runtime_profile_sha256'] != handoff['runtime_profile_sha256']):
            raise guard.GuardError('typed 停点的 Store、VC-5 四根、镜像或 Profile 已改变')
        from tools.official_client_capture import claude_qualification_material_source as materials
        materials._check_four_roots(store, qualification)
        self.saved_guard_identity = saved['guard_identity']
        if not self.saved_guard_identity:
            raise guard.GuardError('typed 停点没有原守卫活身份，禁止启动新守卫追认')
        self.recorded_pid = int(self.saved_guard_identity['pid'])
        self.restored_guard = True

    def _bind_live_guard(self) -> None:
        """同一原 namespace 内握手；冷守卫、PID 重用或配置不一致均拒绝。"""

        if self.saved_guard_identity is not None and not guard.owner_alive({key: self.saved_guard_identity[key]
                for key in ('boot_id', 'pid', 'process_start', 'process_group_id')}):
            raise guard.GuardError('原守卫已死或 PID 被复用；该停点不可续作，禁止新 guard 补旧身份')
        with fixture_mac_source_scope(self.configuration, self.data):
            reader = executor.create_executor(EXECUTOR_NAME, self.configuration, self.context)
            status = reader._status()
        if status.get('configuration_sha256') != canonical_sha256(self.configuration):
            raise guard.GuardError('原守卫握手的 configuration_sha256 不一致')
        identity = status['guard_identity']
        if self.saved_guard_identity is not None and identity != self.saved_guard_identity:
            raise guard.GuardError('原守卫完整身份已改变，禁止换 origin 续作')
        self.saved_guard_identity = dict(identity)
        self.recorded_pid = int(identity['pid'])

    def __enter__(self) -> "TypedDeliveryFixture":
        retained = getattr(self, 'retained', stops.record_root() is not None)
        self.retained = retained
        if self.saved_guard_identity is None:
            if retained:
                # 原 serve_guard 自己 setsid。fork 子进程不注册 multiprocessing 的
                # atexit join；同时断开 runner 管道，失败后活守卫保留在原世界。
                self.recorded_pid = os.fork()
                if self.recorded_pid == 0:
                    try:
                        signal.signal(signal.SIGTERM, signal.SIG_DFL)
                        signal.signal(signal.SIGINT, signal.SIG_DFL)
                        descriptor = os.open(self.control_path.with_name('typed-vc6-guard.log'), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                        os.dup2(descriptor, 1)
                        os.dup2(descriptor, 2)
                        os.close(descriptor)
                        null = os.open(os.devnull, os.O_RDONLY)
                        os.dup2(null, 0)
                        os.close(null)
                        serve_guard(self.configuration, self.context, self.control_path)
                    except BaseException:
                        traceback.print_exc()
                    finally:
                        os._exit(1)
            else:
                self.process = multiprocessing.get_context("fork").Process(target=serve_guard,
                    args=(self.configuration, self.context, self.control_path))
                self.process.start()
        try:
            deadline = time.monotonic() + 8
            socket_path = self.data / self.configuration["guard"]["socket_relative"]
            while not socket_path.exists():
                if (self.process is not None and not self.process.is_alive()) or time.monotonic() >= deadline:
                    raise guard.GuardError("typed VC-6 独立守卫启动失败")
                time.sleep(0.01)
            if retained:
                self._bind_live_guard()
                self._save_record()
            self.stack = contextlib.ExitStack()
            self.stack.enter_context(source_scope(self.control_path))
            return self
        except BaseException:
            self.close(force=not self.restored_guard)
            raise

    def prepare_material_successor(self, *, plan_ref: dict | None, delivery_lock_fd: int) -> dict | None:
        """原 driver 在交付锁内显式调用一次；握手与来源作用域本身不创建关联。

        两角色沿既有明确 fixture intake 重新取得当前 reader；部署绑定只由当前
        有效登记指向的真实演进原件推出，不能构造收据或改原 guard／配置／Plan。
        """
        from tools.official_client_control import receipts
        from tools.official_client_capture import claude_qualification_material_import as importer
        from tools.official_client_capture import claude_gate_material_worker as material_reader
        from tools.official_client_capture.claude_gate_material_worker import _plain_file
        from tools.official_client_control.qualification_material_import import validate_import_shape

        if getattr(self, "stack", None) is None or self.configuration["source_scope"] != "regression_fixture":
            raise guard.GuardError("typed 后继准备必须在原 __enter__ 的明确回归来源作用域内")
        manifest = upgrade.load_campaign_manifest(self.campaign)
        store, campaign = claude_upgrade_store.open_campaign_store(self.campaign, manifest)
        expected_root = self.data / self.configuration["candidate"]["qualification"]["store_relative"]
        if store.root != expected_root or campaign["campaign_id"] != self.context["campaign_id"]:
            raise guard.GuardError("typed 后继准备必须原同根 Store 与 Campaign")
        if plan_ref is None or self.configuration["schema_version"] != executor.MATERIAL_CONFIGURATION_SCHEMA:
            return None
        plan = store.load_object(plan_ref)["payload"]
        if (plan["campaign_id"], plan["candidate_id"], plan["delivery_id"], plan["configuration_sha256"]) != (
                self.context["campaign_id"], self.candidate_id, self.context["delivery_id"], canonical_sha256(self.configuration)):
            raise guard.GuardError("typed 后继准备的 Plan 不是原固定交付")
        stages = receipts.delivery_chain_state(store, plan["campaign_id"], plan["delivery_id"])
        if not stages.complete() or stages.pending is not None:
            return None
        # 后继使用已有双受管根与原读取器的实物依赖解析：同级 CI 或独立
        # driver 的五个钉住文件。读取身份计算会核完整字节，不以目录存在
        # 代替准入，也不触发首次夹具的缺模块补拷。
        repository = receipts._tool_repository_root()
        original_import = validate_import_shape(guard.read_bound_json(self.data, self.configuration["qualification_materials_binding"]))
        original_reader = guard.read_bound_json(self.data, original_import["reader_identity_binding"])
        current_reader = material_reader.compute_gate_reader_identity(repository, formed_context={"fixture_only": True})
        if original_reader == dict(current_reader.document):
            return None
        if receipts.delivery_receipts(store, plan["campaign_id"], plan_ref):
            raise guard.GuardError("原 Plan 已有完成收据，禁止补材料关联或重新组包")
        self._bind_live_guard()
        chain = receipts.tool_evolutions(store, campaign)
        if not chain:
            raise guard.GuardError("typed 后继准备缺当前有效工具演进登记")
        evolution = chain[-1][1]["evolution_receipt"]
        relative = executor.validate_relative_path(evolution["path"], "typed 有效演进原件")
        evolution_path = guard.managed_path(self.data, self.campaign.relative_to(self.data).as_posix() + "/" + relative)
        raw, _metadata = _plain_file(evolution_path)
        if sha256_bytes(raw) != evolution["sha256"]:
            raise guard.GuardError("typed 后继有效登记的演进原件漂移")
        document = executor.parse_json_bytes(raw)
        deployment = document.get("bindings", {}).get("deployment_receipt", {})
        locator = deployment.get("path")
        if not isinstance(locator, str):
            raise guard.GuardError("typed 后继演进原件缺真实控制部署坐标")
        path = Path(locator)
        if not path.is_absolute() or str(path) != locator or ".." in path.parts or not path.is_relative_to(self.data):
            raise guard.GuardError("typed 后继控制部署必须演进原件所指的同数据根绝对坐标")
        control_raw, _metadata = _plain_file(path)
        if sha256_bytes(control_raw) != deployment.get("sha256"):
            raise guard.GuardError("typed 后继演进所指控制部署原件漂移")
        control_binding = {"path": path.relative_to(self.data).as_posix(), "sha256": sha256_bytes(control_raw), "bytes": len(control_raw)}
        reader = executor.create_executor(EXECUTOR_NAME, self.configuration, self.context)
        source = reader.read_material_successor_preparation_source(store, plan_ref=plan_ref)
        gate_root = _fixture_qualification_gate_root(store, self.configuration["candidate"]["qualification"], self.data)

        def consume_sources(qualification_store: Any, sources: list[Any]) -> dict:
            # 产品封存与验真仍用原可写 Store；intake 使用独立同根只读句柄。
            if qualification_store.root != store.root:
                raise guard.GuardError("typed 后继 intake 与原 Store 不是同根")
            return importer.prepare_material_successor(store, sources, managed_data_root=self.data,
                plan_ref=plan_ref, source=source, control_deployment_binding=control_binding,
                delivery_lock_fd=delivery_lock_fd)

        return _prepare_fixture_qualification_materials(store, self.configuration, self.data,
            gate_root=gate_root, consume_sources=consume_sources)

    def close(self, *, force: bool = False) -> None:
        if getattr(self, "stack", None) is not None:
            self.stack.close()
            self.stack = None
        if getattr(self, 'retained', False):
            if not force:
                return
            if self.recorded_pid is None:
                return
            pid = self.recorded_pid
            if self.saved_guard_identity is not None:
                actual = {key: self.saved_guard_identity[key] for key in ('boot_id', 'pid', 'process_start', 'process_group_id')}
                if guard.owner_alive(actual):
                    # 终止前核完整服务实例身份，不能只凭某个仍存活的 PID。
                    self._bind_live_guard()
            else:
                # 只有首次启动尚未握手的亲生子进程可走这条失败清理路径。
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        return
                except ChildProcessError:
                    raise guard.GuardError('无完整守卫身份且不是原子进程，拒绝终止')
                try:
                    actual = guard.process_identity(pid)
                except guard.GuardError:
                    return

            def reaped() -> bool:
                try:
                    if os.waitpid(pid, os.WNOHANG)[0] == pid:
                        return True
                except ChildProcessError:
                    pass
                return not guard.owner_alive(actual)

            def terminate(sig: int) -> bool:
                if reaped():
                    return True
                if not guard.owner_alive(actual):
                    return True
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    return True
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if reaped():
                        return True
                    time.sleep(0.02)
                return reaped()

            if not terminate(signal.SIGTERM) and not terminate(signal.SIGKILL):
                raise guard.GuardError('原守卫有界终止后仍未退出，保留现场并拒绝无界等待')
            return
        if self.process is not None:
            self.process.terminate()
            self.process.join(timeout=5)
            if self.process.is_alive():
                self.process.kill()
                self.process.join(timeout=5)

    def __exit__(self, *ignored: Any) -> None:
        self.close()

    def run_action(self) -> dict[str, Any]:
        return delivery.run_action(self.campaign, self.candidate_id, environment_executor=EXECUTOR_NAME,
            observation_window_seconds=self.configuration["limits"]["observation_window_seconds"],
            executor_configuration=self.configuration_path, executor_configuration_sha256=self.configuration_binding["sha256"])
