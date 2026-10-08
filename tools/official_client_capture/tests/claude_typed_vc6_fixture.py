"""当前 Claude VC-6 控制回归共用夹具：原 Store、真实 typed 合同和独立守卫。

候选资格沿用调用方已经完成的 VC-5；回滚资格在同一 Store、同一外部根中
经既有事实生产器建立。仅文件现场和探针观测是 regression_fixture 输入。
守卫执行、恢复、原件导出、Store 发布、收据与 checkpoint 不替换。
正式登记表只在测试上下文中临时安装，退出即恢复；本文件不能授予正式认证。
"""

from __future__ import annotations

import atexit
import contextlib
import json
import multiprocessing
import os
import signal
import tempfile
import time
from pathlib import Path
from typing import Any, Iterator
from unittest import mock

from tools.official_client_capture import claude_upgrade as upgrade
from tools.official_client_capture import claude_upgrade_capture as capture
from tools.official_client_capture import claude_upgrade_arm64_delivery_executor as executor
from tools.official_client_capture import claude_upgrade_delivery as delivery
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard
from tools.official_client_capture import claude_upgrade_release as release
from tools.official_client_capture import claude_upgrade_store
from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_capture.tests.test_claude_real_delivery_integration import DeliveryCampaign, QualifiedFileRuntime, fixture_projection
from tools.official_client_capture.tests.test_claude_upgrade_arm64_delivery_executor import fixture_world
from tools.official_client_control import delivery_execution as execution
from tools.official_client_control.canonical import canonical_sha256, sha256_bytes, sha256_file

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR_NAME = "arm64-isolated"
CONTROL_SCHEMA = "claude-typed-vc6-test-source/v1"
SEAM_CONTROL_ENV = "CLAUDE_TYPED_VC6_CONTROL"
SEAM_ACTIONS_ENV = "CLAUDE_TYPED_VC6_ACTIONS"
SEAM_REPOSITORY_ENV = "CLAUDE_TYPED_VC6_REPOSITORY"
DIAGNOSTIC_ROOT_ENV = "CLAUDE_TYPED_VC6_DIAGNOSTIC_ROOT"
_STACKS: list[contextlib.ExitStack] = []


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


class ControlledExecutor(executor.Arm64DeliveryExecutor):
    """准备前强杀复现原替身的中断位置；armed 后强杀由独立专项另验。"""

    def __init__(self, configuration: dict, execution_context: dict, control_path: Path):
        super().__init__(configuration, execution_context)
        self.control_path = control_path

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


@contextlib.contextmanager
def fixture_mac_source_scope(configuration: dict, data_root: Path) -> Iterator[None]:
    """材料准备、父读侧及新进程各自登记；退出逐字恢复原 registry。

    仅沿资格的精确 Campaign、候选与固定首轮收据取得 task。冻结 task
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
        round_path = upgrade._legacy_stage_path(campaign, "capture-candidate", q["candidate_id"])[1]
        round_one = guard.read_json(round_path)
        client_bindings = round_one.get("client_evidence", {}).get("client_bindings", {})
        if not client_bindings:
            continue
        if (parent.get("campaign_id"), parent.get("candidate_id"), round_one.get("campaign_id"),
                round_one.get("candidate_id")) != (q["campaign_id"], q["candidate_id"], q["campaign_id"], q["candidate_id"]):
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
                and task["candidate_id"] == q["candidate_id"]
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


def serve_guard(configuration: dict, context: dict, control_path: Path) -> None:
    """独立进程组持有真实 GuardEngine；动作强杀不会连带终止守卫。"""

    os.setsid()
    root = Path(context["data_root"])
    with fixture_mac_source_scope(configuration, root):
        identity = executor.guard_identity(configuration, context, formal=False)
        engine = guard.GuardEngine(configuration, context, ControlledFileRuntime(configuration, context, control_path), identity)
        key = guard.read_bytes(guard.check_binding(root, configuration["guard"]["channel_key_binding"]))
        guard.serve(engine, root / configuration["guard"]["socket_relative"], key)


@contextlib.contextmanager
def source_scope(control_path: Path) -> Iterator[None]:
    """父读取与动作子进程共用来源登记；不改正式空登记表或可信校验器。"""

    control = _control(control_path)
    root = Path(control["context"]["data_root"])
    configuration = guard.read_json(guard.check_binding(root, control["context"]["configuration_ref"]))
    with fixture_mac_source_scope(configuration, root):
        reader = executor.create_executor(EXECUTOR_NAME, configuration, control["context"])

        def factory(name: str, observed: dict, context: dict) -> ControlledExecutor:
            stable = {key: value for key, value in context.items() if key not in {"segment", "run_nonce"}}
            expected = {key: value for key, value in control["context"].items() if key not in {"segment", "run_nonce"}}
            if name != EXECUTOR_NAME or observed != configuration or stable != expected:
                raise guard.GuardError("typed 回归 factory 被用于其他配置或世界")
            return ControlledExecutor(observed, context, control_path)

        def verify(store: Any, content: Any, origin: Any) -> execution.VerifiedSource:
            if store.root.resolve() != (root / configuration["candidate"]["qualification"]["store_relative"]).resolve():
                raise guard.GuardError("typed 来源被用于其他 Store")
            return execution.VerifiedSource(**reader.verify_origin_evidence(content, origin))

        with mock.patch.dict(delivery.ENVIRONMENT_EXECUTORS, {EXECUTOR_NAME: factory}, clear=True), \
                mock.patch.dict(execution.SOURCE_VERIFIERS, {executor.AUTHORITY: verify}, clear=True):
            yield


def install(control_path: str) -> None:
    """只供明示的测试子进程接缝；正式 CLI 没有这条入口。"""

    if os.environ.get(supervisor.CAMPAIGN_RUN_ACTION_ID_ENV) not in {delivery.RUN_ACTION_ID, delivery.FINALIZE_ACTION_ID}:
        raise guard.GuardError("typed 接缝仅允许两个明确的 VC-6 测试动作")
    stack = contextlib.ExitStack()
    try:
        stack.enter_context(source_scope(Path(control_path)))
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


def prepare_fixture_qualification_materials(store: Any, configuration: dict, data_root: Path, *, gate_root: Path) -> dict:
    """本轮 Mac 原 reader 的 authority 延续到来源准备和完整导入结束。"""
    with fixture_mac_source_scope(configuration, data_root):
        return _prepare_fixture_qualification_materials(store, configuration, data_root, gate_root=gate_root)


def _prepare_fixture_qualification_materials(store: Any, configuration: dict, data_root: Path, *, gate_root: Path) -> dict:
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
    if not target_root.exists() and repository != REPOSITORY_ROOT:
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
        return importer.prepare_qualification_material_import(qualification_store, sources, managed_data_root=data_root)


class TypedDeliveryFixture:
    """已完成 VC-5 的原世界接入 typed 文件现场；持有守卫直到最后一次重放。"""

    def __init__(self, campaign: Path, candidate_id: str, *, control_path: Path | None = None):
        self.campaign = campaign.resolve(strict=True)
        self.candidate_id = candidate_id
        manifest = upgrade.load_campaign_manifest(campaign)
        store, store_campaign = claude_upgrade_store.open_campaign_store(campaign, manifest)
        self.data = capture._host_data_root(campaign)
        if not execution.requires_current_contract(store, store_campaign):
            raise guard.GuardError("typed 共用夹具要求当前 policy 4，不能降为历史交付")
        handoff = delivery._vc5_handoff(store, store_campaign, candidate_id)
        # 文件现场的固定路径只在空坐标上创建，不能覆盖调用方原件。
        for relative in ("inputs", "guard", "state.json", "fault.json"):
            if (self.data / relative).exists():
                raise guard.GuardError(f"typed 文件现场坐标已存在：{relative}")
        configuration, _, environment = fixture_world(self.data)
        profile_stage = upgrade._load_stage_result(campaign, "stage-profile")
        staged = release.load_staged_release(campaign, profile_stage["staged_release"])
        profile_path = campaign / release.STAGED_RELEASE_ROOT / staged["release_entry"]["profile"]["path"]
        if sha256_file(profile_path) != handoff["runtime_profile_sha256"]:
            raise guard.GuardError("原候选暂存 Profile 与 VC-5 构建不一致")
        profile_copy = self.data / "inputs/current-qualified-profile.json"
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
        configuration["candidate"]["qualification"] = candidate_qualification
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
                    self.data, gate_root=gate_root))
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
            "failures": [], "kill_at": None, "crash_at": None, "failed_gates": []})
        self.process: multiprocessing.Process | None = None

    def __enter__(self) -> "TypedDeliveryFixture":
        self.process = multiprocessing.get_context("fork").Process(target=serve_guard,
            args=(self.configuration, self.context, self.control_path))
        self.process.start()
        try:
            deadline = time.monotonic() + 8
            socket_path = self.data / self.configuration["guard"]["socket_relative"]
            while not socket_path.exists():
                if not self.process.is_alive() or time.monotonic() >= deadline:
                    raise guard.GuardError("typed VC-6 独立守卫启动失败")
                time.sleep(0.01)
            self.stack = contextlib.ExitStack()
            self.stack.enter_context(source_scope(self.control_path))
            return self
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if getattr(self, "stack", None) is not None:
            self.stack.close()
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
