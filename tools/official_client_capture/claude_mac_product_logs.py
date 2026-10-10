"""实读本轮产品会话原件，且要求原生事件证明真正写入者。

仅消费冻结私有 config/projects 中由本轮内置 CLI 实例实际关闭过的 JSONL。
不扫用户全局 HOME，不读凭据，不按文件名／摘要猜 PID。产品 transcript 可以
证明输入及完成文本，但不能证明 VS Code 扩展加载或完整 socket 调用；这些
来源仍须独立观察，绝不从这里推断。
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

from tools.official_client_capture import claude_client_receipt_contract as c
from tools.official_client_capture import claude_mac_client_source as source
from tools.official_client_capture import claude_mac_product_native as native
from tools.official_client_capture import claude_mac_product_authority as authority
from tools.official_client_capture import claude_mac_product_recipe as recipes


class ProductLogError(ValueError):
    """产品日志、真实写入者、私有路径或会话窗口不符。"""


class ProductLogUnavailable(ProductLogError):
    """缺写入者、完整输入／完成或稳定产品原件。"""


def _require(value: Any, message: str) -> None:
    if not value:
        raise ProductLogError(message)


def _text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    _require(isinstance(content, list), "产品消息内容不是文本或 block 数组")
    texts = []
    for block in content:
        _require(isinstance(block, dict) and isinstance(block.get("type"), str), "产品消息 block 非法")
        if block["type"] == "text":
            _require(isinstance(block.get("text"), str), "产品文本 block 缺原文")
            texts.append(block["text"])
    return "\n".join(texts)


def _transcript_events(task: Mapping[str, Any], scenario: Mapping[str, Any], frame: Mapping[str, Any],
                       raw: bytes, private_home: str) -> tuple[str, list[dict[str, Any]]]:
    _require(bool(raw) and raw.endswith(b"\n") and len(raw) <= 32*1024*1024, "产品原件截断或超限")
    rows = [json.loads(line) for line in raw.splitlines()]
    _require(len(rows) <= 65536 and all(isinstance(row, dict) for row in rows), "产品原件行数或结构非法")
    sessions: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("type") not in {"user", "assistant"} or row.get("isSidechain", False) is not False:
            continue
        sid = row.get("sessionId")
        _require(isinstance(sid, str) and bool(c.ID_RE.fullmatch(sid)) and row.get("cwd") == private_home,
                 "产品会话未绑定本轮真实私有 HOME")
        at = c.timestamp(row.get("timestamp"))
        _require(c.timestamp(task["issued_at_utc"]) <= at <= c.timestamp(frame["observed_at_utc"])
                 and at <= c.timestamp(task["execution"]["request_policy"]["deadline_utc"]), "产品原件时间越出本轮窗口")
        message = row.get("message")
        _require(isinstance(message, dict) and message.get("role") == row["type"], "产品消息角色不符")
        text = _text(message)
        if row["type"] == "user" and scenario["challenge"] in text:
            kind = "input"
        elif row["type"] == "assistant" and scenario["response_marker"] in text and message.get("stop_reason") in {"end_turn", "stop_sequence"}:
            kind = "completed"
        else:
            continue
        sessions.setdefault(sid, []).append({"session_id": sid, "scenario_id": scenario["id"],
            "kind": kind, "text": text, "observed_at_utc": row["timestamp"]})
    candidates = [(sid, events) for sid, events in sessions.items()
                  if len(events) == 2 and [row["kind"] for row in events] == ["input", "completed"]
                  and c.timestamp(events[0]["observed_at_utc"]) <= c.timestamp(events[1]["observed_at_utc"])]
    if len(candidates) != 1:
        raise ProductLogUnavailable("缺唯一真实输入／完整完成，不能猜会话")
    return candidates[0]


def replay_product_scenario(task: Mapping[str, Any], *, bundle_root: Path, plan: Mapping[str, Any],
                            scenario: Mapping[str, Any], native_frames: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """只读恢复前复制的原件；写入者和字节须在已实签的内核流中存在。"""
    root = Path(bundle_root)
    _require(dict(scenario) in task["scenarios"] and plan["task_sha256"] == task["task_sha256"], "重放场景不属于原 task")
    cli = next(row for row in task["client_artifacts"] if row["role"] == "embedded_cli")
    executed = {tuple(row["process"]["audit_token"]): row["process"] for row in native_frames
        if row["kind"] == "process" and row["event"] == "exec" and row["process"]["executable"] == cli["content"]["path"]}
    matches = []
    for observed in sorted((root/"provider-observations").glob("product-log-*.json")):
        value = json.loads(authority.read_original(observed))
        if value.get("task_sha256") != task["task_sha256"] or value.get("scenario_id") != scenario["id"]:
            continue
        frame = value["native_writer"]
        _require(frame in native_frames and frame["kind"] == "product_file"
                 and frame["process"] == executed.get(tuple(frame["process"]["audit_token"]))
                 and Path(frame["path"]).is_relative_to(Path(plan["private_roots"]["config"])/"projects")
                 and Path(frame["path"]).suffix == ".jsonl" and isinstance(frame.get("content_sha256"), str),
                 "复制产品日志缺实际内置 CLI 写入原件")
        original = observed.with_suffix(".jsonl")
        raw = authority.read_original(original, maximum=32*1024*1024)
        _require(len(raw) == frame["size"] and hashlib.sha256(raw).hexdigest() == frame["content_sha256"],
                 "复制产品日志与已签名关闭字节不同")
        sid, events = _transcript_events(task, scenario, frame, raw, plan["private_roots"]["home"])
        expected = {"task_sha256": task["task_sha256"], "scenario_id": scenario["id"], "session_id": sid,
            "native_writer": frame, "original_path": frame["path"], "session_events": events,
            "module_activation_verified": False, "socket_events_complete": False}
        _require(value == expected, "复制产品观测与原件实算不同")
        matches.append({"scenario_id": scenario["id"], "session_events": events,
            "raw_files": [c.binding(original, root), c.binding(observed, root)]})
    if len(matches) != 1:
        raise ProductLogUnavailable("复制产品原件缺失或同场景多份歧义")
    return matches[0]


def read_product_scenario(task: Mapping[str, Any], *, bundle_root: Path, plan: Mapping[str, Any],
                          scenario: Mapping[str, Any], native_frames: Sequence[Mapping[str, Any]],
                          path_context: Any = None) -> dict[str, Any]:
    """固定产品 transcript reader；结果仍只是观测，不单独授来源资格。

    原生 file-close、完整 PID/boot/start/audit-token、制品、私有 cwd 和日志时间
    逐项相符才派生会话事件。读取过程中有追加、置换、链接或多会话歧义即拒绝。
    """
    frozen = source.validate_task_v2(task)
    _require(dict(scenario) in frozen["scenarios"] and plan["task_sha256"] == frozen["task_sha256"],
             "产品日志任务或场景不属于完整冻结 task")
    root = Path(bundle_root)
    native._private_directory(root)
    recipe = c.read_json(c.bound_file(root, frozen["execution"]["isolation_recipe"]))
    session = recipes.physical_session_root(frozen, recipe, root, path_context)
    expected_roots = {name: str(session / name) for name in plan["private_roots"]}
    _require(dict(plan["private_roots"]) == expected_roots and "home" in expected_roots and "config" in expected_roots,
             "产品日志只能读取准确私有配方路径")
    log_root = Path(expected_roots["config"]) / "projects"
    artifact = next(row for row in frozen["client_artifacts"] if row["role"] == "embedded_cli")
    frames = [native.validate_native_frame(dict(frame), frozen) for frame in native_frames]
    # file-close 不替代 exec。必须看到同 audit-token 的实际内置 CLI exec，不能从
    # 产品产生了一个 JSONL 就猜真实 CLI 参与。
    writers = {tuple(row["process"]["audit_token"]): row["process"] for row in frames
               if row["kind"] == "process" and row["event"] == "exec"
               and row["process"]["executable"] == artifact["content"]["path"]}
    eligible = {}
    for frame in frames:
        if frame["kind"] != "product_file":
            continue
        process = frame["process"]
        if tuple(process["audit_token"]) not in writers:
            continue
        _require(process == writers[tuple(process["audit_token"])]
                 and process["executable"] == artifact["content"]["path"], "日志写入者与原 CLI 实例不符")
        path = Path(frame["path"])
        _require(path.is_relative_to(log_root) and path != log_root and path.suffix == ".jsonl",
                 "日志写入坐标越出本 run 私有 config/projects")
        # 同文件多次关闭只消费最后一次内核坐标，不重新发产品请求。
        eligible[path] = frame
    if not eligible:
        raise ProductLogUnavailable("没有本轮真实 CLI exec 和私有 transcript 写入原件")
    matches = []
    for path, frame in eligible.items():
        before = path.lstat()
        _require(stat.S_ISREG(before.st_mode) and before.st_uid == os.geteuid()
                 and before.st_nlink == 1 and (before.st_dev, before.st_ino, before.st_size)
                 == (frame["device"], frame["inode"], frame["size"]), "产品日志实例不是原生关闭事件对应的文件")
        if "mtime_nanoseconds" in frame:
            _require((before.st_mtime_ns, before.st_ctime_ns)
                     == (frame["mtime_nanoseconds"], frame["ctime_nanoseconds"]), "产品日志在原生关闭后又被修改")
        raw = authority.read_original(path, maximum=32 * 1024 * 1024)
        if "content_sha256" in frame:
            _require(hashlib.sha256(raw).hexdigest() == frame["content_sha256"], "产品日志字节不是内核实际关闭时的签名原件")
        after = path.lstat()
        _require((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
                 == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)
                 and bool(raw) and raw.endswith(b"\n"), "产品日志读取中漂移或尾行截断")
        try:
            sid, events = _transcript_events(frozen, scenario, frame, raw, expected_roots["home"])
        except ProductLogUnavailable:
            continue
        matches.append((path, frame, raw, sid, events))
    if len(matches) != 1:
        raise ProductLogUnavailable("产品输入／完整完成缺失或多会话歧义，不能猜归属")
    path, frame, raw, sid, events = matches[0]
    directory = root / "provider-observations"
    directory.mkdir(mode=0o700, exist_ok=True)
    native._private_directory(directory)
    key = hashlib.sha256(c.canonical({"task": frozen["task_sha256"], "scenario": scenario["id"],
            "frame": frame, "raw_sha256": hashlib.sha256(raw).hexdigest()})).hexdigest()
    original, observation = directory / ("product-log-" + key + ".jsonl"), directory / ("product-log-" + key + ".json")
    value = {"task_sha256": frozen["task_sha256"], "scenario_id": scenario["id"], "session_id": sid,
             "native_writer": frame, "original_path": str(path), "session_events": events,
             "module_activation_verified": False, "socket_events_complete": False}
    for output, data in ((original, raw), (observation, c.canonical(value))):
        if output.exists() or output.is_symlink():
            _require(authority.read_original(output) == data, "产品观测只写一次原件发生漂移")
        else:
            native._new_file(output, data)
    return {"scenario_id": scenario["id"], "session_events": events,
            "raw_files": [c.binding(original, root), c.binding(observation, root)]}
