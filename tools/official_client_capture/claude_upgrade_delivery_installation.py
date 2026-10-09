"""ARM64 候选交付的固定安装与来源接线。

安装先验证同一 systemd unit 的真实进程生命周期，再冻结原部署收据、
配置和上下文。生命周期原件只证明旧进程组清理及自动重启，不证明
Docker 回退；正式服务的操作恢复仍完全由原 epoch/readiness 合同控制。
这里不提供可上传的来源协议、动态插件或第二种执行器。
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import os
import platform
import re
import signal
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

from tools.official_client_control.canonical import canonical_sha256
from tools.official_client_control.errors import ControlError
from tools.official_client_capture import claude_upgrade_delivery_recovery_guard as guard

DATA_ROOT = Path("/root/docker/capture-cli/data")
DRIVER_ROOT = Path("/root/arm64-capture-driver")
UNIT_ROOT = Path("/etc/systemd/system")
PYTHON = Path("/usr/bin/python3")
INSTALL_RELATIVE = "control/delivery-installations"
LIFECYCLE_SCHEMA = "claude-delivery-systemd-lifecycle/v1"
DEPLOYMENT_SCHEMA = "claude-upgrade-delivery-guard-deployment/v1"
AUTHORITY = "claude-arm64-recovery-guard"
STARTUP_TIMEOUT = 60


def _executor():
    from tools.official_client_capture import claude_upgrade_arm64_delivery_executor
    return claude_upgrade_arm64_delivery_executor


def _private_file(path: Path, mode: int = 0o600) -> bytes:
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise guard.GuardError("安装路径不得包含符号链接")
    data = guard.read_bytes(path)
    information = path.stat()
    if information.st_uid != 0 or stat.S_IMODE(information.st_mode) != mode:
        raise guard.GuardError("安装原件必须由 root 持有且权限与安装合同一致")
    return data


def _check_directory(path: Path, *, private: bool = True) -> None:
    """读侧只核已有目录，不创建目录或修改权限；写侧另显式调用创建函数。"""
    for ancestor in (path, *path.parents):
        if ancestor.is_symlink():
            raise guard.GuardError("安装目录不得包含符号链接")
    information = path.stat()
    if (not stat.S_ISDIR(information.st_mode) or information.st_uid != os.geteuid()
            or (stat.S_IMODE(information.st_mode) != 0o700 if private else information.st_mode & 0o022)):
        raise guard.GuardError("安装目录的所有者或权限非法")


def _require_managed_install() -> None:
    """固定驱动与实际双受管根是信任起点，证据不能选择另一个安装器。"""
    if platform.system() != "Linux" or platform.machine().lower() not in {"aarch64", "arm64"} or os.geteuid() != 0:
        raise guard.GuardError("正式交付仅允许 ARM64 root 受管安装")
    for directory in (DATA_ROOT, DRIVER_ROOT):
        if not directory.is_dir() or directory.is_symlink():
            raise guard.GuardError("实际受管安装目录不存在")
        _check_directory(directory)
    path = DRIVER_ROOT / "install.py"
    raw = _private_file(path, 0o700)
    specification = importlib.util.spec_from_file_location("_claude_delivery_installer", path)
    module = importlib.util.module_from_spec(specification)
    exec(compile(raw, str(path), "exec", dont_inherit=True), module.__dict__)
    if _private_file(path, 0o700) != raw:
        raise guard.GuardError("驱动安装器加载期间漂移")
    manifest = module.load_manifest(DRIVER_ROOT)
    module.verify_tree(DRIVER_ROOT, manifest, check_mode=True, require_root_owner=True)
    module.verify_install(DRIVER_ROOT, DATA_ROOT, "claude")
    for loaded in (sys.modules[__name__], _executor(), guard):
        if Path(loaded.__file__).parent != DATA_ROOT / "tools/official_client_capture":
            raise guard.GuardError("交付模块不属于已验证的实际受管根")


def _directory(instance: str, *, create: bool = False) -> Path:
    if not isinstance(instance, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", instance) or instance.lower().startswith(("production", "dmit", "vircs")):
        raise guard.GuardError("交付安装实例名非法")
    path = guard.managed_path(DATA_ROOT, f"{INSTALL_RELATIVE}/{instance}")
    if create:
        guard.private_directory(path)
    elif path.exists():
        _check_directory(path)
    return path


def _service(instance: str) -> str:
    _directory(instance)
    return f"claude-delivery-{instance}.service"


def _activation_started(directory: Path) -> bool:
    return any((directory / name).exists() or (directory / name).is_symlink()
               for name in ("activation-intent.json", "enabled.json", "verified.json"))


def render_unit(instance: str) -> bytes:
    """unit 仅包含固定路径和实例名，不反向绑定尚未生成的配置摘要。"""
    _service(instance)
    return ("[Unit]\nDescription=Claude isolated candidate delivery " + instance + "\nAfter=docker.service\n"
            "[Service]\nType=simple\nUser=root\nUMask=0077\nKillMode=control-group\nRestart=on-failure\nRestartSec=1\n"
            "Environment=PYTHONDONTWRITEBYTECODE=1\nExecStart=" + str(PYTHON) + " -B " + str(DRIVER_ROOT / "driver/delivery_guard.py")
            + " serve --instance " + instance + "\n[Install]\nWantedBy=multi-user.target\n").encode()


def _systemctl(*arguments: str) -> str:
    command = Path("/usr/bin/systemctl") if Path("/usr/bin/systemctl").is_file() else Path("/bin/systemctl")
    return subprocess.run([str(command), *arguments], check=True, capture_output=True, text=True,
                          timeout=30, env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"}).stdout.strip()


def _service_snapshot(instance: str) -> dict[str, str]:
    output = _systemctl("show", _service(instance), "--property=MainPID,FragmentPath,KillMode,Restart,NRestarts,ControlGroup")
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if (set(fields) != {"MainPID", "FragmentPath", "KillMode", "Restart", "NRestarts", "ControlGroup"}
            or fields["FragmentPath"] != str(UNIT_ROOT / _service(instance))
            or fields["KillMode"] != "control-group" or fields["Restart"] != "on-failure"
            or not fields["ControlGroup"].endswith("/" + _service(instance))
            or not fields["MainPID"].isdigit() or int(fields["MainPID"]) < 1
            or not fields["NRestarts"].isdigit()):
        raise guard.GuardError("实际 systemd 服务与固定安装不一致")
    if _private_file(UNIT_ROOT / _service(instance), 0o644) != render_unit(instance):
        raise guard.GuardError("实际 systemd unit 字节漂移")
    return fields


def _machine() -> str:
    return hashlib.sha256(Path("/etc/machine-id").read_bytes()).hexdigest()


@contextmanager
def _installation_lock(directory: Path):
    descriptor = os.open(directory / ".install.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        if os.fstat(descriptor).st_uid != 0 or stat.S_IMODE(os.fstat(descriptor).st_mode) != 0o600:
            raise guard.GuardError("安装锁的所有者或权限非法")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


def _material(path: Path) -> dict[str, Any]:
    return guard.binding(DATA_ROOT, path)


def _read_binding(item: Mapping[str, Any]) -> bytes:
    path = guard.check_binding(DATA_ROOT, dict(item))
    # 受管源码可以保留 0644；私有配置及安装状态另在入口要求 0600。
    information = path.stat()
    if information.st_uid != 0 or information.st_mode & 0o022:
        raise guard.GuardError("绑定原件不是 root 只写的受管文件")
    return guard.read_bytes(path)


def _bootstrap(instance: str) -> dict[str, Any]:
    directory = _directory(instance)
    _private_file(directory / "bootstrap.json")
    value = guard.read_json(directory / "bootstrap.json")
    guard.exact(value, {"instance_id", "service_name", "machine_id_sha256", "service_unit_binding", "executor_binding", "guard_binding"}, "安装自检输入")
    if value["instance_id"] != instance or value["service_name"] != _service(instance) or value["machine_id_sha256"] != _machine():
        raise guard.GuardError("安装自检输入不属于本机实例")
    for key in ("service_unit_binding", "executor_binding", "guard_binding"):
        _read_binding(value[key])
    if (_read_binding(value["service_unit_binding"]) != render_unit(instance)
            or _read_binding(value["executor_binding"]) != Path(_executor().__file__).read_bytes()
            or _read_binding(value["guard_binding"]) != Path(guard.__file__).read_bytes()):
        raise guard.GuardError("安装自检绑定的源码或 unit 已漂移")
    return value


def validate_lifecycle(value: Any, bootstrap: Mapping[str, Any]) -> dict[str, Any]:
    """只接受真实采样的服务／进程关系；该证明不授予交付回退通过状态。"""
    value = guard.exact(value, {"schema_version", "scope", "bootstrap", "before", "after", "old_group_gone", "result"}, "systemd 生命周期证明")
    if (value["schema_version"] != LIFECYCLE_SCHEMA or value["scope"] != "systemd_process_lifecycle_only"
            or value["bootstrap"] != bootstrap or value["old_group_gone"] is not True or value["result"] != "pass"):
        raise guard.GuardError("生命周期证明没有绑定实际安装或混用了交付恢复结论")
    for stage in ("before", "after"):
        sample = guard.exact(value[stage], {"systemd", "main", "child"}, "生命周期采样")
        systemd = guard.exact(sample["systemd"], {"MainPID", "FragmentPath", "KillMode", "Restart", "NRestarts", "ControlGroup"}, "systemd 采样")
        if (any(not isinstance(field, str) for field in systemd.values())
                or not systemd["NRestarts"].isdigit()):
            raise guard.GuardError("生命周期 systemd 字段类型或重启计数非法")
        for role in ("main", "child"):
            identity = guard.exact(sample[role], {"boot_id", "pid", "process_start", "process_group_id"}, "生命周期进程身份")
            if (type(identity["pid"]) is not int or identity["pid"] < 1
                    or type(identity["process_group_id"]) is not int or identity["process_group_id"] < 1
                    or not isinstance(identity["boot_id"], str) or not identity["boot_id"]
                    or not isinstance(identity["process_start"], str) or not identity["process_start"]):
                raise guard.GuardError("生命周期进程身份非法")
        if (systemd["MainPID"] != str(sample["main"]["pid"]) or sample["child"]["pid"] == sample["main"]["pid"]
                or sample["main"]["process_group_id"] != sample["child"]["process_group_id"]
                or systemd["FragmentPath"] != str(UNIT_ROOT / bootstrap["service_name"])
                or systemd["KillMode"] != "control-group" or systemd["Restart"] != "on-failure"
                or not systemd["ControlGroup"].endswith("/" + bootstrap["service_name"])):
            raise guard.GuardError("生命周期采样与独立服务不一致")
    before, after = value["before"], value["after"]
    if (any(sample["main"]["boot_id"] != sample["child"]["boot_id"] for sample in (before, after))
            or before["main"]["boot_id"] != after["main"]["boot_id"]
            or (before["main"]["pid"], before["main"]["process_start"]) == (after["main"]["pid"], after["main"]["process_start"])
            or int(after["systemd"]["NRestarts"]) <= int(before["systemd"]["NRestarts"])):
        raise guard.GuardError("没有证明同一宿主的真实自动重启")
    return value


def validate_deployment_receipt(configuration: Mapping[str, Any]) -> None:
    """补核原部署收据指向的生命周期内容，不能只接受一个自报 JSON 的摘要。"""
    instance = configuration["instance_id"]
    bootstrap = _bootstrap(instance)
    path = guard.check_binding(DATA_ROOT, configuration["guard"]["deployment_receipt_binding"])
    _private_file(path)
    receipt = guard.read_json(path)
    expected = {"schema_version": DEPLOYMENT_SCHEMA, "host_id": "ARM64", "instance_id": instance,
                "service_name": bootstrap["service_name"], "machine_id_sha256": bootstrap["machine_id_sha256"],
                **{key.replace("_binding", "_sha256"): bootstrap[key]["sha256"] for key in ("service_unit_binding", "executor_binding", "guard_binding")}}
    guard.exact(receipt, {*expected, "restart_recovery_binding"}, "守卫受管部署收据")
    if any(receipt[key] != item for key, item in expected.items()):
        raise guard.GuardError("守卫部署收据与当前受管安装不一致")
    for key in ("service_unit_binding", "executor_binding", "guard_binding"):
        if configuration["guard"][key] != bootstrap[key]:
            raise guard.GuardError("配置与当前受管安装的完整绑定不一致")
    proof_path = guard.check_binding(DATA_ROOT, receipt["restart_recovery_binding"])
    _private_file(proof_path)
    if proof_path != _directory(instance) / "lifecycle.json":
        raise guard.GuardError("生命周期原件不是固定安装产物")
    validate_lifecycle(guard.read_json(proof_path), bootstrap)


def _probe_path(instance: str, identity: Mapping[str, Any], role: str) -> Path:
    return _directory(instance) / "probes" / f"{role}-{identity['pid']}-{canonical_sha256(identity)[:16]}.json"


def _await_probe(instance: str, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    last = "尚无服务采样"
    while time.monotonic() < deadline:
        try:
            state = _service_snapshot(instance)
            identity = guard.process_identity(int(state["MainPID"]))
            path = _probe_path(instance, identity, "main")
            _private_file(path)
            sample = guard.read_json(path)
            guard.exact(sample, {"systemd", "main", "child"}, "自检进程采样")
            if sample["systemd"] != state or not all(guard.owner_alive(sample[name]) for name in ("main", "child")):
                raise guard.GuardError("自检进程或服务采样已经过期")
            if previous is not None and (sample["main"] == previous["main"] or any(guard.owner_alive(previous[name]) for name in ("main", "child"))):
                raise guard.GuardError("旧进程组尚未清理或服务尚未重启")
            return sample
        except (OSError, ValueError, guard.GuardError, subprocess.SubprocessError) as error:
            last = str(error)
            time.sleep(0.1)
    raise guard.GuardError("systemd 生命周期自检超时：" + last)


def prepare_installation(instance: str) -> dict[str, Any]:
    """只安装并实测固定服务生命周期；不创建 GuardEngine 或访问 Docker。"""
    _require_managed_install()
    directory = _directory(instance, create=True)
    with _installation_lock(directory):
        if _activation_started(directory):
            raise guard.GuardError("实例已进入正式启用，不得回退到安装自检")
        unit_path = directory / "guard.service"
        guard.write_once(unit_path, render_unit(instance))
        bootstrap = {"instance_id": instance, "service_name": _service(instance), "machine_id_sha256": _machine(),
                     "service_unit_binding": _material(unit_path), "executor_binding": _material(Path(_executor().__file__)),
                     "guard_binding": _material(Path(guard.__file__))}
        guard.write_json_once(directory / "bootstrap.json", bootstrap)
        _bootstrap(instance)
        _check_directory(UNIT_ROOT, private=False)
        installed_unit = UNIT_ROOT / _service(instance)
        if installed_unit.exists() or installed_unit.is_symlink():
            if _private_file(installed_unit, 0o644) != render_unit(instance):
                raise guard.GuardError("已有 unit 与固定安装不同，禁止覆盖")
        else:
            # 完整字节 fsync 后以不覆盖链接发布；中断不留下半份最终 unit。
            descriptor, temporary_name = tempfile.mkstemp(prefix=".claude-delivery-", dir=UNIT_ROOT)
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o644)
                    stream.write(render_unit(instance))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, installed_unit, follow_symlinks=False)
                guard.sync_directory(UNIT_ROOT)
            finally:
                temporary.unlink(missing_ok=True)
        _systemctl("daemon-reload")
        _systemctl("enable", _service(instance))
        proof_path = directory / "lifecycle.json"
        if not proof_path.exists():
            _systemctl("start", _service(instance))
            try:
                before = _await_probe(instance)
                if not guard.owner_alive(before["main"]):
                    raise guard.GuardError("强杀前自检进程身份已变化")
                os.kill(before["main"]["pid"], signal.SIGKILL)
                after = _await_probe(instance, before)
                proof = {"schema_version": LIFECYCLE_SCHEMA, "scope": "systemd_process_lifecycle_only",
                         "bootstrap": bootstrap, "before": before, "after": after, "old_group_gone": True, "result": "pass"}
                guard.write_json_once(proof_path, validate_lifecycle(proof, bootstrap))
            finally:
                _systemctl("stop", _service(instance))
        else:
            validate_lifecycle(guard.read_json(proof_path), bootstrap)
            _systemctl("stop", _service(instance))
        receipt = {"schema_version": DEPLOYMENT_SCHEMA, "host_id": "ARM64", "instance_id": instance,
                   "service_name": _service(instance), "machine_id_sha256": _machine(),
                   **{key.replace("_binding", "_sha256"): bootstrap[key]["sha256"] for key in ("service_unit_binding", "executor_binding", "guard_binding")},
                   "restart_recovery_binding": _material(proof_path)}
        guard.write_json_once(directory / "deployment.json", receipt)
        return {"status": "installation_prepared", "deployment_receipt_binding": _material(directory / "deployment.json"),
                **{key: bootstrap[key] for key in ("service_unit_binding", "executor_binding", "guard_binding")},
                "scope": "systemd_process_lifecycle_only", "delivery_recovery_verified": False}


def _load_activation(instance: str, *, require_verified: bool = True) -> tuple[dict, dict, dict]:
    directory = _directory(instance)
    for name in ("activation-intent.json", "enabled.json"):
        _private_file(directory / name)
    intent = guard.read_json(directory / "activation-intent.json")
    enabled = guard.read_json(directory / "enabled.json")
    guard.exact(intent, {"configuration_binding", "execution_context_binding"}, "安装启用绑定")
    if enabled != intent:
        raise guard.GuardError("安装启用尚未收敛或绑定被替换")
    configuration = _executor().validate_configuration(guard.read_json(guard.check_binding(DATA_ROOT, intent["configuration_binding"])))
    context = _executor().validate_execution_context(guard.read_json(guard.check_binding(DATA_ROOT, intent["execution_context_binding"])), configuration)
    _private_file(guard.check_binding(DATA_ROOT, intent["configuration_binding"]))
    _private_file(guard.check_binding(DATA_ROOT, intent["execution_context_binding"]))
    if (configuration["instance_id"] != instance or configuration["source_scope"] != "formal"
            or context["data_root"] != str(DATA_ROOT) or context["configuration_ref"] != intent["configuration_binding"]
            or configuration["schema_version"] != _executor().MATERIAL_CONFIGURATION_SCHEMA):
        raise guard.GuardError("正式安装必须绑定同一 ARM64 配置 v2 和上下文")
    validate_deployment_receipt(configuration)
    if require_verified:
        _private_file(directory / "verified.json")
        verified = guard.read_json(directory / "verified.json")
        guard.exact(verified, {"activation_binding", "startup_readiness_binding"}, "安装启动复核")
        if verified["activation_binding"] != _material(directory / "enabled.json"):
            raise guard.GuardError("安装启动复核不属于当前启用绑定")
        _read_binding(verified["startup_readiness_binding"])
    return configuration, context, intent


def _current_readiness(reader: Any) -> dict[str, Any]:
    """使用既有完整注册链和 readiness 校验器，不自行创造启动通过结论。"""
    from tools.official_client_capture import claude_upgrade_guard_epochs as epochs
    status = reader._status()
    if status["recovery"]["blocked"]:
        raise guard.GuardError("正式守卫尚有启动恢复未决")
    root = guard.managed_path(DATA_ROOT, reader.configuration["paths"]["guard_root_relative"])
    registrations = [_material(path / "registration.json") for path in sorted((root / "epochs").iterdir())]
    registered = epochs.validate_registration_chain(reader.configuration, reader.context, registrations, _read_binding,
                                                     channel_identity=status["guard_identity"])
    pointer = guard.read_json(root / "epoch-registry/head.json")
    epochs.validate_registry_head(reader.configuration, reader.context, registered, pointer["snapshot_binding"], _read_binding)
    latest = guard.check_binding(DATA_ROOT, registrations[-1]).parent
    readiness = _material(latest / "readiness.json")
    epochs.validate_epoch_readiness(reader.configuration, reader.context, registered, readiness, _read_binding)
    return readiness


def activate_installation(instance: str, execution_context: Path) -> dict[str, Any]:
    """配置与上下文最后冻结；意图一旦落盘，损坏不得退回无执行权的自检。"""
    _require_managed_install()
    directory = _directory(instance, create=True)
    with _installation_lock(directory):
        if not execution_context.is_absolute() or not execution_context.is_relative_to(DATA_ROOT):
            raise guard.GuardError("上下文必须是固定受管根中的绝对路径")
        _private_file(execution_context)
        context = guard.read_json(execution_context)
        configuration_binding = context.get("configuration_ref")
        configuration = _executor().validate_configuration(guard.read_json(guard.check_binding(DATA_ROOT, configuration_binding)))
        _executor().validate_execution_context(context, configuration)
        if (configuration["instance_id"] != instance or configuration["source_scope"] != "formal"
                or context["data_root"] != str(DATA_ROOT) or configuration["schema_version"] != _executor().MATERIAL_CONFIGURATION_SCHEMA):
            raise guard.GuardError("启用输入不是当前实例的正式配置 v2")
        validate_deployment_receipt(configuration)
        intent = {"configuration_binding": configuration_binding, "execution_context_binding": _material(execution_context)}
        guard.write_json_once(directory / "activation-intent.json", intent)
        guard.write_json_once(directory / "enabled.json", intent)
        configuration, context, _ = _load_activation(instance, require_verified=False)
        if not (directory / "verified.json").exists():
            _systemctl("restart", _service(instance))
        reader = _executor().create_executor("arm64-isolated", configuration, context)
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while True:
            try:
                readiness = _current_readiness(reader)
                break
            except (OSError, KeyError, guard.GuardError, ControlError):
                if time.monotonic() >= deadline:
                    raise guard.GuardError("正式守卫启动复核超时；保留启用意图，不回落自检")
                time.sleep(0.1)
        if not (directory / "verified.json").exists():
            guard.write_json_once(directory / "verified.json", {"activation_binding": _material(directory / "enabled.json"),
                                                               "startup_readiness_binding": readiness})
        _load_activation(instance)
        return {"status": "enabled", "instance_id": instance, "activation_binding": _material(directory / "enabled.json")}


def serve_installation(instance: str) -> int:
    """同一固定 unit 的启动入口；已存在任何启用痕迹都只走正式分支。"""
    _require_managed_install()
    directory = _directory(instance)
    if _activation_started(directory):
        configuration, context, intent = _load_activation(instance, require_verified=False)
        return guard.main(["--configuration", str(DATA_ROOT / intent["configuration_binding"]["path"]),
                           "--configuration-sha256", canonical_sha256(configuration),
                           "--execution-context", str(DATA_ROOT / intent["execution_context_binding"]["path"])])
    _bootstrap(instance)
    state = _service_snapshot(instance)
    if state["MainPID"] != str(os.getpid()):
        raise guard.GuardError("安装自检不是固定 systemd 服务的 MainPID")
    child = subprocess.Popen([str(PYTHON), "-B", str(DRIVER_ROOT / "driver/delivery_guard.py"), "probe-child", "--instance", instance],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        child_identity = guard.process_identity(child.pid)
        ready_path = _probe_path(instance, child_identity, "child")
        deadline = time.monotonic() + STARTUP_TIMEOUT
        while not ready_path.exists():
            if child.poll() is not None or time.monotonic() >= deadline:
                raise guard.GuardError("安装自检子进程没有完成固定入口核验")
            time.sleep(0.1)
        _private_file(ready_path)
        if guard.read_json(ready_path) != child_identity or not guard.owner_alive(child_identity):
            raise guard.GuardError("自检子进程 readiness 与实际进程不一致")
        cgroup = Path(f"/proc/{child.pid}/cgroup").read_text()
        if not any(line.rsplit(":", 1)[-1] == state["ControlGroup"] for line in cgroup.splitlines()):
            raise guard.GuardError("安装自检子进程不属于固定服务 cgroup")
        sample = {"systemd": state, "main": guard.process_identity(os.getpid()), "child": child_identity}
        guard.write_json_once(_probe_path(instance, sample["main"], "main"), sample)
        while child.poll() is None:
            time.sleep(0.2)
        raise guard.GuardError("安装自检子进程异常退出")
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)


def probe_child(instance: str) -> int:
    """安装器唯一可派生的无操作子进程；不读取凭据、不监听、不执行现场动作。"""
    _require_managed_install()
    _bootstrap(instance)
    state = _service_snapshot(instance)
    if int(state["MainPID"]) != os.getppid():
        raise guard.GuardError("自检子进程不属于固定服务 MainPID")
    identity = guard.process_identity(os.getpid())
    guard.write_json_once(_probe_path(instance, identity, "child"), identity)
    while True:
        time.sleep(1)


def formal_factory(name: str, configuration: dict, execution_context: dict):
    _require_managed_install()
    if name != "arm64-isolated":
        raise guard.GuardError("正式工厂只接受固定 ARM64 隔离候选")
    installed, context, _ = _load_activation(configuration.get("instance_id"))
    stable = lambda value: {key: item for key, item in value.items() if key not in {"segment", "run_nonce"}}
    if installed != configuration or stable(context) != stable(execution_context):
        raise guard.GuardError("执行配置或稳定上下文与受管安装不一致")
    return _executor().create_executor(name, configuration, execution_context)


def _source_readers(store: Any, items: list[dict[str, Any]]) -> list[Any]:
    """一次只读调用仅验安装整树一次；不跨调用缓存、不接受证据提供的安装根。"""
    _require_managed_install()
    directory = guard.managed_path(DATA_ROOT, INSTALL_RELATIVE)
    if not directory.is_dir() or directory.is_symlink():
        raise guard.GuardError("固定交付安装尚未准备")
    _check_directory(directory)
    installations = []
    for path in sorted(directory.iterdir()):
        if not path.is_dir() or path.is_symlink():
            raise guard.GuardError("安装实例目录非法")
        if not _activation_started(path):
            continue
        configuration, context, _ = _load_activation(path.name)
        installations.append((configuration, context))
    readers = {}
    output = []
    for item in items:
        content, origin = item["content"], item["origin"]
        if origin.get("authority") != AUTHORITY or origin.get("source_scope") != "formal":
            raise guard.GuardError("正式来源不能承接夹具或其他 authority")
        matches = [(configuration, context) for configuration, context in installations
                   if (Path(context["data_root"]) / configuration["candidate"]["qualification"]["store_relative"] == store.root
                       and origin.get("configuration_sha256") == canonical_sha256(configuration)
                       and all(content.get(key) == context[key] for key in ("campaign_id", "delivery_id")))]
        if len(matches) != 1:
            raise guard.GuardError("来源没有唯一匹配的受管 Store、配置和交付安装")
        configuration, context = matches[0]
        key = canonical_sha256({"configuration": configuration, "context": context})
        if key not in readers:
            readers[key] = _executor().create_executor("arm64-isolated", configuration, context)
        output.append(readers[key])
    return output


def _source_reader(store: Any, content: Mapping[str, Any], origin: Mapping[str, Any]):
    return _source_readers(store, [{"content": content, "origin": origin}])[0]


def formal_source_verifier(store: Any, content: Mapping[str, Any], origin: Mapping[str, Any]):
    from tools.official_client_control.delivery_execution import VerifiedSource
    try:
        return VerifiedSource(**_source_reader(store, content, origin).verify_origin_evidence(content, origin))
    except (OSError, ValueError, guard.GuardError) as error:
        raise ControlError("Claude 真实交付来源尚未完成受管通道验收：" + str(error)) from error


def formal_source_verifier_batch(store: Any, items: list[dict[str, Any]]):
    from tools.official_client_control.delivery_execution import VerifiedSource
    try:
        groups: dict[str, tuple[Any, list]] = {}
        for index, (item, reader) in enumerate(zip(items, _source_readers(store, items))):
            key = canonical_sha256({"configuration": reader.configuration, "context": reader.context})
            groups.setdefault(key, (reader, []))[1].append((index, item))
        output = [None] * len(items)
        for reader, indexed in groups.values():
            values = reader.verify_origin_evidence_batch([item for _, item in indexed])
            if len(values) != len(indexed):
                raise guard.GuardError("来源批量响应缺少请求项")
            for (index, _), value in zip(indexed, values):
                output[index] = VerifiedSource(**value)
        return output
    except (OSError, ValueError, guard.GuardError) as error:
        raise ControlError("Claude 真实交付来源尚未完成受管通道验收：" + str(error)) from error


formal_source_verifier.verify_batch = formal_source_verifier_batch
