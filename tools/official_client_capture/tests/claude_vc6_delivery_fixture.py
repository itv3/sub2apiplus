"""Claude VC-6 交付测试的交付环境执行器替身与动作子进程接缝（B4-07 收尾；只在测试里，不进受管树）。

交付环境的真实执行器（ARM64 隔离候选环境）是 B4-07 遗留项，生产登记表 ``claude_upgrade_delivery.ENVIRONMENT_EXECUTORS`` 为空；
测试按名字 ``fixture`` 登记替身：

* ``FixtureExecutor``：测试进程里用（``test_claude_upgrade_delivery``），按冻结计划给出观测，可按「段、阶段」注入失败、中途
  抛错与环境漂移，交付门禁可按 id 判失败。
* ``FileExecutor``：父监督器动作子进程里用（``test_claude_upgrade_vc6_batch``），注入点读一份控制 JSON（测试随时改写），每次
  阶段调用追加一行到调用日志；``kill_at`` 在该阶段 ``os._exit(9)`` 模拟进程被强杀。

子进程接缝经 ``usercustomize`` 注入：只在 ``SEAM_ACTIONS_ENV`` 列出的动作子进程里登记替身（与 ``claude_vc5_chain_fixture`` 同一
做法）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import codex_upgrade_supervisor as supervisor
from tools.official_client_control.tests.fixtures import ROLLBACK_IMAGE

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
EXECUTOR_NAME = "fixture"
SEAM_CONTROL_ENV = "CLAUDE_VC6_DELIVERY_CONTROL"
SEAM_ACTIONS_ENV = "CLAUDE_VC6_DELIVERY_ACTIONS"
SEAM_REPOSITORY_ENV = "CLAUDE_VC6_DELIVERY_REPOSITORY"
ROLLBACK_PROFILE = "e" * 64
ENVIRONMENT = {
    "host_id": "arm64-candidate",
    "architecture": "linux/amd64",
    "compose_sha256": "d" * 64,
    "environment_sha256": "4" * 64,
    "configuration_sha256": "5" * 64,
    "network_sha256": "6" * 64,
}

USERCUSTOMIZE = f'''"""B4-07 收尾测试注入（不进受管树）：列出的 VC-6 动作子进程里登记交付环境执行器替身。"""
import os
import sys

if os.environ.get("{supervisor.CAMPAIGN_RUN_ACTION_ID_ENV}") in os.environ.get("{SEAM_ACTIONS_ENV}", "").split(","):
    sys.path.insert(0, os.environ["{SEAM_REPOSITORY_ENV}"])
    from tools.official_client_capture.tests import claude_vc6_delivery_fixture

    claude_vc6_delivery_fixture.install(os.environ["{SEAM_CONTROL_ENV}"])
'''


class FixtureExecutor:
    """交付环境执行器替身：按冻结计划给出观测；``failures``（段, 阶段）→ 根因（None 表示用缺省根因），
    ``crash_at``（段, 阶段）在该阶段抛错模拟中途崩溃，``environment`` 可改动模拟环境漂移。"""

    def __init__(self, environment: Mapping[str, str] | None = None) -> None:
        self.environment = dict(environment or ENVIRONMENT)
        self.failures: dict[tuple[str, str], str | None] = {}
        self.crash_at: tuple[str, str] | None = None
        self.failed_gates: set[str] = set()
        self.freezes = 0
        self.calls: list[tuple[str, str, str]] = []

    def freeze(self, request: dict[str, Any]) -> dict[str, Any]:
        self.freezes += 1
        return {
            "environment": dict(self.environment),
            "rollback": {"image_digest": ROLLBACK_IMAGE, "runtime_profile_sha256": ROLLBACK_PROFILE},
            "gates": [
                {"id": gate, "result": "failed" if gate in self.failed_gates else "pass", "facts": {"probe": gate}}
                for gate in request["delivery_gates"]
            ],
            "rollback_material": [{"id": "app-image", "result": "pass", "facts": {"image_digest": ROLLBACK_IMAGE}}],
            "deployment_configuration": [{"id": "compose", "result": "pass", "facts": {"sha256": self.environment["compose_sha256"]}}],
        }

    def _record_call(self, segment: str, stage: str, run_nonce: str) -> None:
        self.calls.append((segment, stage, run_nonce))

    def _interrupt(self, segment: str, stage: str) -> None:
        if self.crash_at == (segment, stage):
            raise RuntimeError(f"执行器在 {segment} {stage} 中途崩溃")

    def run_stage(self, request: dict[str, Any]) -> dict[str, Any]:
        segment, stage, plan = request["segment"], request["stage"], request["plan"]
        self._record_call(segment, stage, request["run_nonce"])
        self._interrupt(segment, stage)
        rollback = stage == "rollback_verified"
        failed = (segment, stage) in self.failures
        observation: dict[str, Any] = {
            "stage": stage,
            "result": "failed" if failed else "pass",
            "environment": dict(self.environment),
            "image_digest": plan["rollback_image_digest"] if rollback else plan["candidate_image_digest"],
            "runtime_profile_sha256": plan["rollback_runtime_profile_sha256"] if rollback else plan["runtime_profile_sha256"],
            "checks": [{"id": check, "result": "pass", "facts": {"stage": stage}} for check in plan["stage_checks"]],
            "observed_seconds": plan["observation_window_seconds"] if stage == "stable_observed" else 3,
            "private_files": [{"name": "stage.log", "content": f"{segment} {stage} {request['run_nonce']}\n"}],
        }
        if failed and self.failures[(segment, stage)] is not None:
            observation["root_cause_id"] = self.failures[(segment, stage)]
        return observation


class FileExecutor(FixtureExecutor):
    """动作子进程里的替身：注入点每次调用前从控制 JSON 读（``environment``、``failures`` 为 ``[[段, 阶段, 根因或 null]]``、
    ``crash_at``／``kill_at`` 为 ``[段, 阶段]``、``failed_gates``），阶段调用追加到 ``calls`` 指向的 JSONL。"""

    def __init__(self, control_path: Path) -> None:
        self.control_path = Path(control_path)
        self.kill_at: tuple[str, str] | None = None
        self.calls_path: Path | None = None
        super().__init__()
        self._load()

    def _load(self) -> None:
        control = json.loads(self.control_path.read_text(encoding="utf-8"))
        self.environment = dict(control.get("environment") or ENVIRONMENT)
        self.failures = {(str(item[0]), str(item[1])): item[2] for item in control.get("failures", [])}
        self.crash_at = tuple(control["crash_at"]) if control.get("crash_at") else None  # type: ignore[assignment]
        self.kill_at = tuple(control["kill_at"]) if control.get("kill_at") else None  # type: ignore[assignment]
        self.failed_gates = set(control.get("failed_gates", []))
        self.calls_path = Path(control["calls"])

    def freeze(self, request: dict[str, Any]) -> dict[str, Any]:
        self._load()
        return super().freeze(request)

    def _record_call(self, segment: str, stage: str, run_nonce: str) -> None:
        super()._record_call(segment, stage, run_nonce)
        assert self.calls_path is not None
        with self.calls_path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps([segment, stage]) + "\n")

    def _interrupt(self, segment: str, stage: str) -> None:
        if self.kill_at == (segment, stage):
            os._exit(9)
        super()._interrupt(segment, stage)

    def run_stage(self, request: dict[str, Any]) -> dict[str, Any]:
        self._load()
        return super().run_stage(request)


def write_control(path: Path, **fields: Any) -> Path:
    """写（覆盖）控制 JSON；``calls`` 缺省为同目录的 ``calls.jsonl``。"""

    payload = {"calls": str(Path(path).with_name("calls.jsonl")), **fields}
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return path


def read_calls(control_path: Path) -> list[tuple[str, str]]:
    calls = Path(json.loads(Path(control_path).read_text(encoding="utf-8"))["calls"])
    if not calls.exists():
        return []
    return [tuple(json.loads(line)) for line in calls.read_text(encoding="utf-8").splitlines() if line.strip()]  # type: ignore[misc]


def install(control_path: str) -> None:
    """动作子进程入口：按名字登记文件驱动的替身，进程退出前一直生效。"""

    from tools.official_client_capture import claude_upgrade_delivery

    claude_upgrade_delivery.ENVIRONMENT_EXECUTORS[EXECUTOR_NAME] = lambda: FileExecutor(Path(control_path))


def subprocess_environment(inject_dir: Path, control_path: Path, actions: list[str]) -> dict[str, str]:
    inject_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    (inject_dir / "usercustomize.py").write_text(USERCUSTOMIZE, encoding="utf-8")
    return {
        "PYTHONPATH": str(inject_dir),
        SEAM_CONTROL_ENV: str(control_path),
        SEAM_ACTIONS_ENV: ",".join(actions),
        SEAM_REPOSITORY_ENV: str(REPOSITORY_ROOT),
    }
