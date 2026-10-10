"""VS Code 成功激活原件：ES 入口读、实际 extension host 与产品 success 日志。

开始激活日志或 extension.js 打开都不足以通过。必须同时核本次私有应用的
唯一 extension host、冻结入口字节、它产生的激活开始日志、产品真实 success
遥测和写入者内核原件。只读取私有日志，不启动调试器或注入观察扩展。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_product_authority as authority
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_materials as materials


class ProductModuleError(ValueError):
    """模块、实际宿主、日志原件或完成时间不符。"""


class ProductModuleUnavailable(ProductModuleError):
    """当前产品的成功激活原件尚未齐备。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise ProductModuleError(message)


def _read_log(frame: Mapping[str, Any], root: Path) -> bytes:
    path = Path(frame["path"])
    _require(path.is_relative_to(root) and path != root and path.suffix == ".log", "模块日志越出本轮私有 logs 根")
    before = path.lstat()
    _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid() and before.st_nlink == 1
             and (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
             == tuple(frame[name] for name in ("device", "inode", "size", "mtime_nanoseconds", "ctime_nanoseconds")),
             "模块日志不是真实内核关闭时的原文件字节")
    raw = authority.read_original(path, maximum=32 * 1024 * 1024)
    if "content_sha256" in frame:
        _require(hashlib.sha256(raw).hexdigest() == frame["content_sha256"], "模块日志字节不是内核实际关闭时的签名原件")
    after = path.lstat()
    _require(all(getattr(before, name) == getattr(after, name) for name in
                 ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")) and raw.endswith(b"\n"),
             "模块日志读时漂移或尾行截断")
    return raw


def read_vscode_activation(task: Mapping[str, Any], *, bundle_root: Path, plan: Mapping[str, Any],
                           materials_receipt: Path, native_frames: Sequence[Mapping[str, Any]],
                           log_utc_offset_seconds: int) -> dict[str, Any]:
    return _activation(task, bundle_root=bundle_root, plan=plan, materials_receipt=materials_receipt,
        native_frames=native_frames, log_utc_offset_seconds=log_utc_offset_seconds, archived=False)


def replay_vscode_activation(task: Mapping[str, Any], *, bundle_root: Path, plan: Mapping[str, Any],
                            materials_receipt: Path, native_frames: Sequence[Mapping[str, Any]],
                            log_utc_offset_seconds: int) -> dict[str, Any]:
    """恢复后从复制原件与已实签 close 字节重验，不重读已经恢复的全局路径。"""
    return _activation(task, bundle_root=bundle_root, plan=plan, materials_receipt=materials_receipt,
        native_frames=native_frames, log_utc_offset_seconds=log_utc_offset_seconds, archived=True)


def _activation(task: Mapping[str, Any], *, bundle_root: Path, plan: Mapping[str, Any],
                materials_receipt: Path, native_frames: Sequence[Mapping[str, Any]],
                log_utc_offset_seconds: int, archived: bool) -> dict[str, Any]:
    """把两类真实原件闭合为模块观测；不单独授 formal 来源权。

    日志为产品本机时区，偏移由静态 provider 在启动前冻结并在退出后重验。
    不用文件 mtime 反推激活成功，不从当前时间补造缺失的日志时间。
    """
    _require(task["client_id"] == "claude-vscode" and plan["task_sha256"] == task["task_sha256"], "模块必须绑定原 VS Code task")
    _require(type(log_utc_offset_seconds) is int and -86400 < log_utc_offset_seconds < 86400, "模块日志时区非法")
    material = (materials.read_product_material_archive(task, materials_receipt) if archived
                else materials.read_product_materials(materials_receipt))
    _require(material["client_artifacts"] == task["client_artifacts"], "模块冻结物料不符")
    if material["facts"].get("vscode_activation_source") != {
            "event_name": "extensionActivationTimes", "success_outcome": "success", "requires_log_level": "trace",
            "originals": ["code-extension-host.js", "code-telemetry-service.js"]}:
        raise ProductModuleUnavailable("当前产品未冻结成功激活解析器的真实源码原件")
    _require(plan["argv"][-2:] == ["--log", "trace"], "本轮私有配方没有开启已核版本的成功原件日志")
    artifacts = {row["role"]: row for row in task["client_artifacts"]}
    entry, host = artifacts["extension_entry"], artifacts["extension_host"]
    module = Path(entry["content"]["path"])
    raw_entry = authority.read_original(Path(bundle_root)/"public/extension-entry.js" if archived else module)
    _require(len(raw_entry) == entry["content"]["bytes"] and hashlib.sha256(raw_entry).hexdigest() == entry["content"]["sha256"],
             "真实扩展入口字节已经变化")
    frames = [native.validate_native_frame(dict(row), task) for row in native_frames]
    hosts = {tuple(row["process"]["audit_token"]): row for row in frames if row["kind"] == "process"
             and row["event"] == "exec" and row["process"]["executable"] == host["content"]["path"]}
    if len(hosts) != 1:
        raise ProductModuleUnavailable("本次私有产品没有唯一真实 extension host，不能猜模块宿主")
    host_token, host_frame = next(iter(hosts.items()))
    opens = [row for row in frames if row["kind"] == "product_module_open" and row["path"] == str(module)
             and tuple(row["process"]["audit_token"]) == host_token]
    if not opens:
        raise ProductModuleUnavailable("没有实际 extension host 打开冻结扩展入口的 ES 原件")
    opened = opens[0]
    if not archived:
        info = module.lstat()
        _require((info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                 == tuple(opened[name] for name in ("device", "inode", "size", "mtime_nanoseconds", "ctime_nanoseconds")),
                 "模块在实际加载后被置换或修改")
    _require(opened["size"] == entry["content"]["bytes"], "模块 OPEN 大小与冻结入口不一致")
    executed = {tuple(row["process"]["audit_token"]): row for row in frames
                if row["kind"] == "process" and row["event"] == "exec"}
    log_root = Path(plan["private_roots"]["user_data"]) / "logs"
    eligible = {row["path"]: row for row in frames if row["kind"] == "product_file"
                and Path(row["path"]).is_relative_to(log_root)
                and tuple(row["process"]["audit_token"]) in executed
                and {"mtime_nanoseconds", "ctime_nanoseconds"} <= set(row)}
    start_logs, success_logs, originals = [], [], {}
    zone = timezone(timedelta(seconds=log_utc_offset_seconds))
    prefix = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})\] \[(?:trace|info)\] (.*)$")
    for path, frame in eligible.items():
        if archived:
            _require(isinstance(frame.get("content_sha256"), str), "复制模块日志缺内核实际字节签名")
            candidates = []
            for copy_path in sorted((Path(bundle_root)/"provider-observations").glob("activation-*.log")):
                candidate = authority.read_original(copy_path, maximum=32*1024*1024)
                if hashlib.sha256(candidate).hexdigest() == frame["content_sha256"]:
                    candidates.append(candidate)
            if not candidates:
                continue
            _require(len(candidates) == 1 and len(candidates[0]) == frame["size"] and candidates[0].endswith(b"\n"),
                     "复制模块日志与已实签原件不符或出现歧义")
            raw = candidates[0]
        else:
            raw = _read_log(frame, log_root)
        for line in raw.decode("utf-8", "strict").splitlines():
            match = prefix.fullmatch(line)
            if not match:
                continue
            at = datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=zone).astimezone(timezone.utc)
            text = match.group(2)
            _require(c.timestamp(task["issued_at_utc"]) <= at <= c.timestamp(frame["observed_at_utc"]), "模块日志时间越出本任务窗口")
            if "ExtensionService#_doActivateExtension anthropic.claude-code," in text:
                _require(Path(path).name == "exthost.log" and tuple(frame["process"]["audit_token"]) == host_token,
                         "模块开始日志不是原 extension host 实际写入")
                start_logs.append((at, frame, text)); originals[path] = raw
            if text.startswith("telemetry/extensionActivationTimes "):
                document = json.loads(text.partition(" ")[2])
                properties = document.get("properties", {})
                measurements = document.get("measurements", {})
                if properties.get("id") != "anthropic.claude-code":
                    continue
                _require(properties.get("extensionVersion") == artifacts["extension_package"]["version"]["value"]
                         and properties.get("outcome") == "success" and measurements.get("pluginHostTelemetry") == 1
                         and all(type(measurements.get(name)) in {int, float} and measurements[name] >= 0
                                 for name in ("codeLoadingTime", "activateCallTime", "activateResolvedTime")),
                         "真实产品没有证明扩展成功 activate resolved")
                success_logs.append((at, frame, document)); originals[path] = raw
    if len(start_logs) != 1 or len(success_logs) != 1:
        raise ProductModuleUnavailable("缺唯一实际开始／成功原件；开始日志或脚本打开不能冒充已激活")
    begin, success = start_logs[0], success_logs[0]
    # VS Code 开始日志在模块实际 OPEN 之前，success 在 activate resolved 之后。
    _require(c.timestamp(host_frame["observed_at_utc"]) <= begin[0]
             <= c.timestamp(opened["observed_at_utc"]) <= success[0]
             <= c.timestamp(task["execution"]["request_policy"]["deadline_utc"]), "实际模块加载／成功顺序不闭合")
    directory = Path(bundle_root) / "provider-observations"
    if not archived:
        directory.mkdir(mode=0o700, exist_ok=True)
    native._private_directory(directory)
    references = []
    for path, raw in sorted(originals.items()):
        out = directory / ("activation-" + hashlib.sha256(path.encode() + raw).hexdigest() + ".log")
        if out.exists():
            _require(authority.read_original(out) == raw, "模块原件只写一次发生冲突")
        else:
            _require(not archived, "模块复制原件缺失，离线重验不能补写成功")
            native._new_file(out, raw)
        references.append(c.binding(out, Path(bundle_root)))
    value = {"task_sha256": task["task_sha256"], "artifact_id": entry["artifact_id"],
        "host_process": copy.deepcopy(host_frame["process"]), "module_open": opened,
        "loaded_at_utc": opened["observed_at_utc"], "activated_at_utc": success[0].isoformat(),
        "activation_begin": copy.deepcopy(begin[1]), "activation_success": copy.deepcopy(success[1]),
        "success_payload": copy.deepcopy(success[2]), "log_utc_offset_seconds": log_utc_offset_seconds}
    out = directory / ("activation-" + c.digest(value) + ".json")
    if out.exists():
        _require(authority.read_original(out) == c.canonical(value), "模块派生观测发生冲突")
    else:
        _require(not archived, "模块观测原件缺失，离线重验不能补写成功")
        native._new_file(out, c.canonical(value))
    return {"module_observation": value, "raw_files": [*references, c.binding(out, Path(bundle_root))]}
