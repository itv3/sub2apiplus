"""当前 Claude VC-6 控制回归共用夹具：原 Store、真实 typed 合同和独立守卫。

候选资格沿用调用方已经完成的 VC-5；回滚资格在同一 Store、同一外部根中
经既有事实生产器建立。仅文件现场和探针观测是 regression_fixture 输入。
守卫执行、恢复、原件导出、Store 发布、收据与 checkpoint 不替换。
正式登记表只在测试上下文中临时安装，退出即恢复；本文件不能授予正式认证。
"""

from __future__ import annotations

import contextlib
import json
import multiprocessing
import os
import signal
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
from tools.official_client_control.canonical import canonical_sha256, sha256_file

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR_NAME = "arm64-isolated"
CONTROL_SCHEMA = "claude-typed-vc6-test-source/v1"
SEAM_CONTROL_ENV = "CLAUDE_TYPED_VC6_CONTROL"
SEAM_ACTIONS_ENV = "CLAUDE_TYPED_VC6_ACTIONS"
SEAM_REPOSITORY_ENV = "CLAUDE_TYPED_VC6_REPOSITORY"
_STACKS: list[contextlib.ExitStack] = []


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


def serve_guard(configuration: dict, context: dict, control_path: Path) -> None:
    """独立进程组持有真实 GuardEngine；动作强杀不会连带终止守卫。"""

    os.setsid()
    identity = executor.guard_identity(configuration, context, formal=False)
    engine = guard.GuardEngine(configuration, context, ControlledFileRuntime(configuration, context, control_path), identity)
    root = Path(context["data_root"])
    key = guard.read_bytes(guard.check_binding(root, configuration["guard"]["channel_key_binding"]))
    guard.serve(engine, root / configuration["guard"]["socket_relative"], key)


@contextlib.contextmanager
def source_scope(control_path: Path) -> Iterator[None]:
    """父读取与动作子进程共用来源登记；不改正式空登记表或可信校验器。"""

    control = _control(control_path)
    root = Path(control["context"]["data_root"])
    configuration = guard.read_json(guard.check_binding(root, control["context"]["configuration_ref"]))
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

    stack = contextlib.ExitStack()
    stack.enter_context(source_scope(Path(control_path)))
    _STACKS.append(stack)


USERCUSTOMIZE = f'''"""仅指定 VC-6 测试动作安装回归来源。"""
import os
import sys
if os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") in os.environ.get("{SEAM_ACTIONS_ENV}", "").split(","):
    sys.path.insert(0, os.environ["{SEAM_REPOSITORY_ENV}"])
    from tools.official_client_capture.tests import claude_typed_vc6_fixture
    claude_typed_vc6_fixture.install(os.environ["{SEAM_CONTROL_ENV}"])
'''


def subprocess_environment(inject_dir: Path, control_path: Path, actions: list[str]) -> dict[str, str]:
    guard.private_directory(inject_dir)
    (inject_dir / "usercustomize.py").write_text(USERCUSTOMIZE, encoding="utf-8")
    return {"PYTHONPATH": str(inject_dir), SEAM_CONTROL_ENV: str(control_path),
            SEAM_ACTIONS_ENV: ",".join(actions), SEAM_REPOSITORY_ENV: str(REPOSITORY_ROOT)}


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
