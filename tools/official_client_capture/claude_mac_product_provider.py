"""真实产品执行接点：固定私有配方、原生监测、应用进程与产品原件收集。

默认正式登记仍关闭。实例须由原静态来源工厂拥有，派发前重验受管安装、
产品字节、原捕获及可执行预算上界。真实 UI 输入、产品日志和 socket 样本
分别保存；模块激活或联合 transport-map 缺证据时留下 partial，绝不猜通过。
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_authority as authority_tools
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_recipe as recipes
from tools.official_client_capture import claude_mac_process_identity as identity_tools
from tools.official_client_capture import claude_mac_product_logs as product_logs


class ProductProviderError(ValueError):
    """产品接点、任务、原始观测或所属实例不符。"""


class ProductProviderUnavailable(ProductProviderError):
    """正式现场资格、预算上界或产品观测材料不完整。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise ProductProviderError(message)


class DarwinProductObservationProvider:
    """构造只读；所有执行方法都重验原静态 source，不能从用户 JSON 加 handler。"""

    def __init__(self, task: Mapping[str, Any], *, bundle_root: Path,
                 materials_receipt: Path, authority: authority_tools.ProductInstallationAuthority) -> None:
        self._task = copy.deepcopy(source.validate_task_v2(task))
        self._root = Path(bundle_root)
        self._root_instance = native._private_directory(self._root)
        self._material = Path(materials_receipt)
        self._plan = recipes.derive_material_private_launch_plan(self._task, bundle_root=self._root,
                                                               materials_receipt=self._material)
        self._authority = authority
        self._adapter = c.SOURCE_ADAPTERS.get(self._task["execution"]["recorder_id"])
        self._monitor = native.DarwinProductMonitor(self._task, bundle_root=self._root, authority=authority)
        self._process: subprocess.Popen | None = None
        self._instance: dict[str, Any] | None = None
        self._before_stop: dict[str, Any] | None = None
        self._started_at: str | None = None
        self._stopped_at: str | None = None
        self._phases: list[str] = []
        self._scenarios: set[str] = set()
        self._completed: dict[str, dict[str, Any]] = {}
        self._origin: dict[str, Any] | None = None
        self._cleanup = False

    def _guard(self, task: Mapping[str, Any], root: Path | None = None, *, dispatch: bool = False) -> None:
        _require(source.validate_task_v2(task) == self._task and (root is None or root == self._root),
                 "provider须消费完整原 task 与原目录")
        adapter = c.SOURCE_ADAPTERS.get(self._task["execution"]["recorder_id"])
        if adapter is None or adapter is not self._adapter:
            raise ProductProviderUnavailable("产品静态来源未登记或已替换，零启动拒绝")
        owns = getattr(adapter, "owns_observation_provider", None)
        _require(callable(owns) and owns(self, self._task, self._root) is True, "provider不是原静态工厂拥有的实例")
        qualification = getattr(adapter, "verify_product_qualification", None)
        if not callable(qualification):
            raise ProductProviderUnavailable("静态来源缺产品／模块／捕获／预算上界资格核验，零启动拒绝")
        qualification(self._task, self._root, self, dispatch=dispatch)
        _require(native._private_directory(self._root) == self._root_instance, "provider原件根被替换")
        self._authority.revalidate(self._task["execution"]["source_deployments"]["mac"])
        actual = recipes.derive_material_private_launch_plan(self._task, bundle_root=self._root,
                                                            materials_receipt=self._material)
        _require(actual == self._plan, "真实产品物料或私有配方已经变化")
        if dispatch:
            _require(not self._cleanup and datetime.now(timezone.utc)
                     < c.timestamp(self._task["execution"]["request_policy"]["deadline_utc"]),
                     "产品冻结期限已过或已经回收，禁止续算与再派发")

    def _reply(self, phase: str, facts: Mapping[str, Any], *, unknown: tuple[str, ...] = (),
               references: tuple[dict[str, Any], ...] = ()) -> dict[str, Any]:
        directory = self._root / "provider-observations"
        directory.mkdir(mode=0o700, exist_ok=True)
        native._private_directory(directory)
        self._phases.append(phase)
        value = {"task_sha256": self._task["task_sha256"], "task_nonce": self._task["task_nonce"],
                 "producer_run_id": self._task["execution"]["producer_run_id"],
                 "deployment": self._task["execution"]["source_deployments"]["mac"], "phase": phase,
                 "facts": copy.deepcopy(dict(facts)),
                 "completeness": {"status": "unknown" if unknown else "complete",
                                  "first_sequence": len(self._phases), "last_sequence": len(self._phases)},
                 "unknown_reasons": list(unknown)}
        path = directory / f"{len(self._phases):04d}-{phase}.json"
        native._new_file(path, c.canonical(value))
        return {**value, "raw_files": [c.binding(path, self._root), *references]}

    def start_monitoring(self, task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]:
        self._guard(task, bundle_root)
        ready = self._monitor.start()
        return self._reply("monitoring", {"started_at_utc": ready["observed_at_utc"], "native_ready": ready})

    def recheck_monitoring(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task, dispatch=True)
        value = self._monitor.recheck()
        if value.get("accessibility_trusted") is not True:
            raise ProductProviderUnavailable("系统尚未授予真实 UI 权限；不启动应用或弹权限申请")
        return self._reply("recheck", {"active": True, "native_ready": value})

    def launch(self, task: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task, dispatch=True)
        _require(self._process is None and dict(plan) == self._plan["plan"], "只能派发一次原产品固定计划")
        self._monitor.recheck()
        for value in plan["private_roots"].values():
            native._private_directory(Path(value))
        # 私有目录参数必须真的指向本次冻结的复制品，不能启动后才补假模块材料。
        roles = {row["role"]: row for row in self._task["client_artifacts"]}
        if self._task["client_id"] == "claude-vscode":
            _require(Path(roles["extension_package"]["content"]["path"]).is_relative_to(Path(plan["private_roots"]["extensions"])),
                     "任务扩展尚未在私有 extensions-dir 冻结；须先 stage 后建新 task")
        else:
            _require(Path(roles["embedded_cli"]["content"]["path"]).is_relative_to(Path(plan["private_roots"]["user_data"]) / "claude-code"),
                     "任务内置 CLI 尚未按 Desktop pin 冻结到私有缓存；须先 stage 后建新 task")
        environment = getattr(self._adapter, "product_environment", None)
        if not callable(environment):
            raise ProductProviderUnavailable("缺静态来源的私有凭据／候选 endpoint 接线")
        env = environment(self._task, self, copy.deepcopy(self._plan))
        allowed = set(self._plan["environment"]) | {"ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"}
        _require(isinstance(env, Mapping) and set(env) == allowed
                 and all(isinstance(value, str) and "\x00" not in value for value in env.values())
                 and all(env[name] == value for name, value in self._plan["environment"].items())
                 and env["ANTHROPIC_BASE_URL"] == self._task["execution"]["endpoint"],
                 "环境闭集或候选 endpoint 不符；拒绝 shell、DYLD 与其他未审核注入")
        self._process = subprocess.Popen(list(plan["argv"]), cwd=plan["private_roots"]["home"],
            env=dict(env), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
        row = self._monitor.wait_for("process", pid=self._process.pid)
        _require(row["event"] == "exec" and row["process"]["executable"] == plan["executable"],
                 "原生观察未证明真实应用 exec；不能用 Popen PID 冒充参与")
        self._instance = copy.deepcopy(row["process"])
        self._started_at = row["observed_at_utc"]
        return self._reply("launch", {"process_instance": self._instance, "started_at_utc": self._started_at,
                                       "native_exec": row})

    def drive(self, task: Mapping[str, Any], scenario: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task, dispatch=True)
        _require(self._process is not None and self._instance is not None and self._process.poll() is None
                 and dict(scenario) in self._task["scenarios"] and scenario["id"] not in self._scenarios,
                 "场景不属于唯一仍在运行的原产品实例，禁止重复派发")
        inherited = {item for previous in self._task["scenario_reuse"] for item in previous["scenario_ids"]}
        _require(scenario["id"] not in inherited, "已经承接的场景不能重新发请求")
        self._monitor.recheck()
        self._scenarios.add(scenario["id"])
        request = {"task_sha256": self._task["task_sha256"], "pid": self._process.pid,
                   "application_executable": self._plan["plan"]["executable"], "challenge": scenario["challenge"]}
        driven = subprocess.run([str(self._authority.executable), "--product-ui-v1"], input=c.canonical(request) + b"\n",
                                capture_output=True, timeout=min(self._monitor._remaining(), 60), check=False)
        native._new_file(self._root / "provider-observations" / (scenario["id"] + "-ui.stdout"), driven.stdout)
        native._new_file(self._root / "provider-observations" / (scenario["id"] + "-ui.stderr"), driven.stderr)
        _require(driven.returncode == 0, "实际产品 UI 输入失败；原件保留，不自动重发")
        input_fact = json.loads(driven.stdout)
        _require(input_fact.get("kind") == "input_observed" and input_fact["task_sha256"] == self._task["task_sha256"]
                 and input_fact["challenge"] == scenario["challenge"] and input_fact["completion_observed"] is False,
                 "UI 原件不符；输入事实不能冒充完成事实")
        # 模块调用和完成须来自产品真实日志解析器，来源工厂必须已核该版本格式。
        observe = getattr(self._adapter, "observe_product_scenario", None)
        if not callable(observe):
            try:
                observed = product_logs.read_product_scenario(self._task, bundle_root=self._root,
                    plan=self._plan["plan"], scenario=scenario, native_frames=self._monitor.records)
            except (ValueError, OSError) as error:
                return self._reply("drive", {"scenario_id": scenario["id"], "ui_input": input_fact,
                                            "log_error_type": type(error).__name__},
                                   unknown=("缺本轮真实产品输入／完成原件，不能猜会话与模块归属",))
        else:
            observed = observe(self._task, self, dict(scenario), input_fact)
        _require(isinstance(observed, dict) and set(observed) == {"scenario_id", "session_events", "raw_files"}
                 and observed["scenario_id"] == scenario["id"] and isinstance(observed["raw_files"], list),
                 "产品场景实际观测合同不符")
        for binding in observed["raw_files"]:
            c.bound_file(self._root, binding)
        _require(any(row.get("kind") == "completed" and scenario["response_marker"] in str(row.get("text", ""))
                     for row in observed["session_events"]), "产品日志缺真实完成事件")
        self._completed[scenario["id"]] = observed
        return self._reply("drive", {"scenario_id": scenario["id"], "ui_input": input_fact,
                                     "product_observation": observed})

    def stop_owned(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task)
        _require(self._process is not None and self._instance is not None, "没有完整原实例，不能按 PID 回收其他产品")
        self._cleanup = True
        self._monitor.begin_cleanup()
        if self._stopped_at is None:
            if self._process.poll() is None:
                self._before_stop = identity_tools.read_process_snapshot(self._process.pid)
                _require(self._before_stop["executable_path"] == self._instance["executable"]
                         and self._before_stop["start_time_seconds"] == self._instance["start_seconds"]
                         and self._before_stop["start_time_microseconds"] == self._instance["start_microseconds"],
                         "回收前原 PID 启动实例已变化，拒绝发送信号")
                # 内核 API 同时核 PID 和 pidversion，堵住“先检查 PID 再 kill”的竞态。
                requested = subprocess.run([str(self._authority.executable), "--product-stop-v1"],
                    input=c.canonical({"task_sha256": self._task["task_sha256"], "process": self._instance}) + b"\n",
                    capture_output=True, timeout=min(self._monitor._remaining(), 30), check=False)
                path = self._root / "provider-observations" / "product-stop.stdout"
                native._new_file(path, requested.stdout)
                native._new_file(path.with_suffix(".stderr"), requested.stderr)
                _require(requested.returncode == 0, "audit-token 回收失败，禁止退回 PID 信号")
                stop_request = json.loads(requested.stdout)
                _require(stop_request["task_sha256"] == self._task["task_sha256"]
                         and stop_request["audit_token"] == self._instance["audit_token"]
                         and stop_request["returncode"] == 0 and stop_request["stopped"] is False,
                         "原生回收请求不符；请求不能冒充真实 exit")
            self._process.wait(timeout=self._monitor._remaining())
            exits = [value for value in self._monitor.records if value["kind"] == "process"
                     and value["event"] == "exit" and value["process"]["pid"] == self._process.pid]
            while not exits:
                self._monitor.recheck()
                time.sleep(min(self._monitor._remaining(), 0.01))
                exits = [value for value in self._monitor.records if value["kind"] == "process"
                         and value["event"] == "exit" and value["process"]["pid"] == self._process.pid]
            _require(exits[-1]["process"]["start_seconds"] == self._instance["start_seconds"]
                     and exits[-1]["process"]["start_microseconds"] == self._instance["start_microseconds"],
                     "exit 原件属于另一启动实例")
            self._stopped_at = exits[-1]["observed_at_utc"]
        return self._reply("stop", {"process_instance": self._instance, "stopped": True, "stopped_at_utc": self._stopped_at})

    def close_observations(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task)
        self._cleanup = True
        closed = self._monitor.close()
        unknown = []
        if closed.get("unclosed_instances") != 0 or closed.get("first_error") is not None:
            unknown.append("产品子进程或原生事件链未全部收口")
        if closed.get("drain_status") != "complete":
            unknown.append("当前原生尾屏障仍待现场资格，不能按完整事件消费")
        return self._reply("close", {"closed": not unknown, "native_closed": closed}, unknown=tuple(unknown))

    def recover_owned(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task)
        if self._process is None or self._instance is None:
            raise ProductProviderUnavailable("新 provider 缺原 Popen／内核实例，不能仅凭磁盘 PID 回收")
        value = self.stop_owned(task)
        return self._reply("recovery_owned", value["facts"])

    def observation_inventory(self, task: Mapping[str, Any], bundle_root: Path) -> dict[str, Any]:
        self._guard(task, bundle_root)
        export = getattr(self._adapter, "export_product_observations", None)
        if not callable(export):
            return self._reply("inventory", {"files_available": False},
                               unknown=("缺受管产品模块／socket 与独立捕获联合映射，不能签完整来源",))
        actual = export(self._task, self, tuple(copy.deepcopy(self._monitor.records)), copy.deepcopy(self._completed))
        _require(isinstance(actual, dict) and set(actual) == {"files", "origin"}, "产品最终观测库存合同非法")
        for binding in actual["files"]:
            c.bound_file(self._root, binding)
        origin = actual["origin"]
        _require(isinstance(origin, dict) and origin.get("task_sha256") == self._task["task_sha256"]
                 and origin.get("deployment") == self._task["execution"]["source_deployments"]["mac"], "来源原件身份不符")
        verify = getattr(self._adapter, "verify_product_origin", None)
        if not callable(verify):
            raise ProductProviderUnavailable("缺产品原件真实来源重验，不能用导出清单的摘要代替")
        verify(self._task, self._root, self, origin)
        return self._reply("inventory", {"files_available": True, **actual})
