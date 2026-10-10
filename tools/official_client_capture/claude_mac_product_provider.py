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
from tools.official_client_capture import claude_mac_product_logs as product_logs
from tools.official_client_capture import claude_mac_product_flows as product_flows
from tools.official_client_capture import claude_mac_product_modules as product_modules


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
                 materials_receipt: Path, authority: authority_tools.ProductInstallationAuthority,
                 path_context: Any = None) -> None:
        self._task = copy.deepcopy(source.validate_task_v2(task))
        self._root = Path(bundle_root)
        self._root_instance = native._private_directory(self._root)
        self._material = Path(materials_receipt)
        self._path_context = path_context
        self._plan = recipes.derive_material_private_launch_plan(self._task, bundle_root=self._root,
                                                               materials_receipt=self._material, path_context=path_context)
        self._authority = authority
        self._adapter = c.source_adapter(self._task)
        self._monitor = native.DarwinProductMonitor(self._task, bundle_root=self._root, authority=authority,
                                                   path_context=path_context)
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
        self._flows: product_flows.DarwinFlowMonitor | None = None
        self._log_offset_seconds: int | None = None
        self._module_observation: dict[str, Any] | None = None
        self._closed_observations: dict[str, Any] | None = None
        self._module_references: tuple[dict[str, Any], ...] = ()

    def _guard(self, task: Mapping[str, Any], root: Path | None = None, *, dispatch: bool = False) -> None:
        _require(source.validate_task_v2(task) == self._task and (root is None or root == self._root),
                 "provider须消费完整原 task 与原目录")
        adapter = c.source_adapter(self._task)
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
        if self._log_offset_seconds is not None:
            current = datetime.now().astimezone().utcoffset()
            _require(current is not None and current.total_seconds() == self._log_offset_seconds,
                     "产品日志时区在观察期间变化，不能猜激活时刻")
        actual = recipes.derive_material_private_launch_plan(self._task, bundle_root=self._root,
                                                            materials_receipt=self._material, path_context=self._path_context)
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
        self._guard(task, bundle_root, dispatch=True)
        _require(self._log_offset_seconds is None, "产品监测与本地日志时区只能冻结一次")
        offset = datetime.now().astimezone().utcoffset()
        _require(offset is not None and offset.total_seconds().is_integer(), "无法冻结实际产品日志时区")
        self._log_offset_seconds = int(offset.total_seconds())
        # 具体固定来源必须在真实产品启动前准备，不由 adapter 传入任意 callback。
        installed = product_flows.load_network_installation()
        self._flows = product_flows.DarwinFlowMonitor(self._task, bundle_root=self._root, installed=installed)
        flow_ready = self._flows.start()
        try:
            ready = self._monitor.start()
        except BaseException:
            # 已开始的真实网络来源须保留失败尾部，不能遗留自己的 bridge。
            self._flows._abort_bridge()
            raise
        return self._reply("monitoring", {"started_at_utc": ready["observed_at_utc"], "native_ready": ready,
                                         "flow_ready": flow_ready, "log_utc_offset_seconds": self._log_offset_seconds})

    def recheck_monitoring(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task, dispatch=True)
        value = self._monitor.recheck()
        # ES 在 root 服务；真实 UI 权限必须由原用户下的固定映像另行实测。
        ui = subprocess.run([str(self._authority.executable), "--product-ui-check-v1"], capture_output=True,
                            timeout=min(self._monitor._remaining(), 10), check=False, env={})
        actual = json.loads(ui.stdout) if ui.returncode == 0 else {}
        if (actual != {"schema_version": "claude-mac-product-ui-check/v1", "user_id": os.geteuid(),
                      "accessibility_trusted": True}):
            raise ProductProviderUnavailable("系统尚未授予真实 UI 权限；不启动应用或弹权限申请")
        return self._reply("recheck", {"active": True, "native_ready": value, "user_ui_check": actual})

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
        # 输入只发一次。随后在原冻结期限内被动等实际日志，不续算 deadline。
        while True:
            self._monitor.recheck()
            _require(self._process.poll() is None, "产品在实际完成原件形成前退出，禁止重发输入")
            try:
                if callable(observe):
                    observed = observe(self._task, self, dict(scenario), input_fact)
                else:
                    observed = product_logs.read_product_scenario(self._task, bundle_root=self._root,
                        plan=self._plan["plan"], scenario=scenario, native_frames=self._monitor.records,
                        path_context=getattr(self, "_path_context", None))
                break
            except product_logs.ProductLogUnavailable:
                # “尚未完整”是等待条件；坏原件和身份冲突保持失败，不能吞掉继续猜。
                if self._flows is not None:
                    self._flows.read()
                time.sleep(min(self._monitor._remaining(), 0.1))
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
        if self._flows is not None:
            self._flows.quiesce_owned(tuple(self._monitor.records))
        if self._stopped_at is None:
            terminated: set[tuple[int, ...]] = set()
            # 先子后父；每轮从 ES 实际完整 token 重算，捕获回收期间的新子进程。
            while True:
                lifecycles = native.owned_process_lifecycles(tuple(self._monitor.records), self._task)
                live = {token: row["start"]["process"] for token, row in lifecycles.items() if row["end"] is None}
                if not live:
                    break
                if self._flows is not None:
                    self._flows.quiesce_owned(tuple(self._monitor.records))
                def depth(token: tuple[int, ...]) -> int:
                    seen = set()
                    while token in live:
                        _require(token not in seen, "ES owned 子树父链成环，不能按 PID 回收")
                        seen.add(token)
                        token = tuple(live[token]["parent_audit_token"])
                    return len(seen)
                depths = {token: depth(token) for token in live}
                deepest = max(depths.values())
                for token in sorted(live):
                    # 叶节点尚未有实际 exit 前，不能先杀祖先破坏生命周期闭合。
                    if depths[token] != deepest:
                        continue
                    if token in terminated:
                        continue
                    instance = live[token]
                    # 内核同时核 pidversion；进程已自然退出时只能等待其 ES exit。
                    requested = subprocess.run([str(self._authority.executable), "--product-stop-v1"],
                        input=c.canonical({"task_sha256": self._task["task_sha256"], "process": instance}) + b"\n",
                        capture_output=True, timeout=min(self._monitor._remaining(), 30), check=False)
                    prefix = "product-stop-" + str(instance["pid"]) + "-" + str(instance["pidversion"])
                    native._new_file(self._root / "provider-observations" / (prefix + ".stdout"), requested.stdout)
                    native._new_file(self._root / "provider-observations" / (prefix + ".stderr"), requested.stderr)
                    if requested.returncode == 0:
                        stop_request = json.loads(requested.stdout)
                        _require(stop_request["task_sha256"] == self._task["task_sha256"]
                            and stop_request["audit_token"] == list(token) and stop_request["returncode"] == 0
                            and stop_request["stopped"] is False, "原生回收请求不符；请求不能冒充真实 exit")
                    else:
                        _require(requested.returncode == 78, "原生回收器拒绝非法输入，禁止按 PID 重试")
                    terminated.add(token)
                self._monitor.recheck()
                time.sleep(min(self._monitor._remaining(), 0.01))
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
        # 状态恢复会删除本轮日志，必须现在收口、复制模块原件。它失败不改变
        # 已实测的产品停止事实；恢复照常执行，最终清单保留 unknown。
        self._collect_closed_observations()
        return self._reply("stop", {"process_instance": self._instance, "stopped": True, "stopped_at_utc": self._stopped_at})

    def _collect_closed_observations(self) -> None:
        if self._closed_observations is not None:
            for reference in self._module_references:
                c.bound_file(self._root, reference)
            return
        self._cleanup = True
        unknown = []
        closed, flow_attestation = {}, None
        try:
            closed = self._monitor.close()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            unknown.append("原生事件收口失败：" + type(error).__name__)
        try:
            flow_attestation = self._flows.close() if self._flows is not None else None
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            unknown.append("原生网络收口失败：" + type(error).__name__)
        if closed.get("unclosed_instances") != 0 or closed.get("first_error") is not None:
            unknown.append("产品子进程或原生事件链未全部收口")
        verified_native = getattr(self._monitor, "origin", None)
        if not isinstance(verified_native, dict) or verified_native.get("kernel_event_chain_closed") is not True:
            unknown.append("当前原生尾屏障仍待现场资格，不能按完整事件消费")
        if self._task["client_id"] == "claude-vscode":
            try:
                observed = product_modules.read_vscode_activation(self._task, bundle_root=self._root,
                    plan=self._plan["plan"], materials_receipt=self._material,
                    native_frames=tuple(self._monitor.records), log_utc_offset_seconds=self._log_offset_seconds)
                self._module_observation = observed["module_observation"]
                self._module_references = tuple(observed["raw_files"])
            except (OSError, ValueError) as error:
                unknown.append(str(error))
        self._closed_observations = {"closed": not unknown, "native_closed": closed,
            "flow_attestation": flow_attestation, "module_observation": self._module_observation,
            "unknown_reasons": unknown}
        native._new_file(self._root / "provider-observations" / "closed-originals.json", c.canonical(self._closed_observations))

    def close_observations(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task)
        _require(self._stopped_at is not None, "真实产品尚未停止，不能收口封存")
        self._collect_closed_observations()
        cached = self._closed_observations
        _require(json.loads(authority_tools.read_original(self._root / "provider-observations" / "closed-originals.json")) == cached,
                 "状态恢复前的封存原件已经漂移")
        closed, flow_attestation = cached["native_closed"], cached["flow_attestation"]
        unknown = cached["unknown_reasons"]
        return self._reply("close", {"closed": not unknown, "native_closed": closed,
                                     "flow_attestation": flow_attestation,
                                     "module_observation": self._module_observation},
                           unknown=tuple(unknown), references=self._module_references)

    def _rehydrate_exited_originals(self, task: Mapping[str, Any]) -> dict[str, Any]:
        """只读重建现存已签原件；不启动 helper、不发信号、不写新返回。"""
        self._guard(task)
        from tools.official_client_capture import claude_mac_product_origins as origins
        observed = origins.read_native_original(self._task, self._root, self._authority)
        applications = [row for row in observed["lifecycles"].values()
            if row["start"]["event"] == "exec" and row["start"]["process"]["executable"] == self._plan["plan"]["executable"]]
        _require(len(applications) == 1 and applications[0]["end_reason"] == "exit",
                 "原产品没有唯一真实退出，不能以磁盘 PID 回收")
        closed = json.loads(authority_tools.read_original(self._root/"provider-observations/closed-originals.json"))
        _require(isinstance(closed, dict) and set(closed) ==
                 {"closed", "native_closed", "flow_attestation", "module_observation", "unknown_reasons"}
                 and closed["native_closed"] == observed["frames"][-1]
                 and type(closed["closed"]) is bool and isinstance(closed["unknown_reasons"], list)
                 and closed["closed"] == (not closed["unknown_reasons"]), "原产品收口缓存与已签尾部不同")
        module, references = closed["module_observation"], ()
        if module is not None:
            _require(self._task["client_id"] == "claude-vscode", "非扩展产品混入模块激活事实")
            activation = product_modules.replay_vscode_activation(self._task, bundle_root=self._root,
                plan=self._plan["plan"], materials_receipt=self._material, native_frames=observed["frames"],
                log_utc_offset_seconds=module["log_utc_offset_seconds"])
            _require(activation["module_observation"] == module, "恢复后模块缓存与封存原件不同")
            references = tuple(activation["raw_files"])
        completed = {}
        inherited = {sid for row in self._task["scenario_reuse"] for sid in row["scenario_ids"]}
        for scenario in self._task["scenarios"]:
            if scenario["id"] in inherited:
                continue
            try:
                completed[scenario["id"]] = product_logs.replay_product_scenario(self._task, bundle_root=self._root,
                    plan=self._plan["plan"], scenario=scenario, native_frames=observed["frames"])
            except (ValueError, OSError):
                # 缺场景原件只影响最终成功资格，不能阻止已经证明安全的状态恢复。
                continue
        previous = [int(path.name[:4]) for path in (self._root/"provider-observations").glob("*.json")
                    if path.name[:4].isdigit() and path.name[4:5] == "-"]
        self._instance = copy.deepcopy(applications[0]["start"]["process"])
        self._started_at = applications[0]["start"]["observed_at_utc"]
        self._stopped_at = applications[0]["end"]["observed_at_utc"]
        self._monitor.records, self._monitor.origin = copy.deepcopy(observed["frames"]), observed
        self._cleanup = True
        self._closed_observations = closed
        self._module_observation, self._module_references = module, references
        self._completed, self._phases = completed, ["historical"] * max(previous, default=0)
        if module is not None:
            self._log_offset_seconds = module["log_utc_offset_seconds"]
        return {"process_instance": self._instance, "stopped": True, "stopped_at_utc": self._stopped_at,
                "native_original": c.binding(self._root/"native-events.jsonl", self._root)}

    def rehydrate_recovery(self, task: Mapping[str, Any], original_reply: Mapping[str, Any]) -> None:
        """重复恢复只复核历史结果和原件，填回新 daemon 的纯内存缓存。"""
        _require(original_reply["phase"] == "recovery_owned" and original_reply["task_sha256"] == self._task["task_sha256"]
                 and original_reply["completeness"]["status"] == "complete", "恢复重建缺准确原结果")
        actual = self._rehydrate_exited_originals(task)
        _require(all(original_reply["facts"].get(name) == actual[name]
                     for name in ("process_instance", "stopped", "stopped_at_utc")), "历史恢复结果与真实退出原件不符")
        if "native_original" in original_reply["facts"]:
            _require(original_reply["facts"]["native_original"] == actual["native_original"], "历史恢复原生绑定漂移")

    def recover_owned(self, task: Mapping[str, Any]) -> dict[str, Any]:
        self._guard(task)
        if self._process is None or self._instance is None:
            # 新 daemon 只能消费已经退出的原件，不能用磁盘 PID 猜归属。
            return self._reply("recovery_owned", self._rehydrate_exited_originals(task))
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
