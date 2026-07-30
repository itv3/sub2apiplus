#!/usr/bin/env python3
"""通过 Codex app-server 触发一次真实的 Responses compact 请求。"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, TextIO


PROMPT = "只回复 COMPACT_TURN_READY，不调用任何工具。"


def secure_write(path: Path, content: str) -> None:
    """以 0600 权限原子保存私有抓包伴随材料。"""

    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(
        temporary,
        os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def provider_values(
    mode: str, codex_version: str = "0.145.0"
) -> tuple[str, ...]:
    """返回官方 HTTP 与 Sub2API HTTP 的可复现 provider 配置。"""

    if not re.fullmatch(r"\d+\.\d+\.\d+", codex_version):
        raise ValueError("Codex 版本必须是三段数字。")

    if mode == "official-http":
        return (
            'model_provider="official_openai_http"',
            'model_providers.official_openai_http.name="OpenAI"',
            (
                'model_providers.official_openai_http.base_url='
                '"https://chatgpt.com/backend-api/codex"'
            ),
            'model_providers.official_openai_http.wire_api="responses"',
            'model_providers.official_openai_http.requires_openai_auth=true',
            'model_providers.official_openai_http.supports_websockets=false',
            (
                "model_providers.official_openai_http.http_headers.version="
                f'"{codex_version}"'
            ),
            "features.responses_websockets_v2=false",
        )
    if mode == "sub2api-http":
        return (
            'model_provider="sub2api_compact_http"',
            'model_providers.sub2api_compact_http.name="Sub2API"',
            (
                'model_providers.sub2api_compact_http.base_url='
                '"http://127.0.0.1:18081/v1"'
            ),
            'model_providers.sub2api_compact_http.env_key="SUB2API_API_KEY"',
            'model_providers.sub2api_compact_http.wire_api="responses"',
            'model_providers.sub2api_compact_http.requires_openai_auth=false',
            'model_providers.sub2api_compact_http.supports_websockets=false',
            "features.responses_websockets_v2=false",
        )
    raise ValueError(f"不支持的 compact 模式：{mode}")


def build_app_server_command(
    codex_bin: str, mode: str, codex_version: str = "0.145.0"
) -> list[str]:
    """构造不包含任何认证值的 app-server 命令。"""

    values = (
        "check_for_update_on_startup=false",
        "analytics.enabled=false",
        "feedback.enabled=false",
        'otel.exporter="none"',
        "otel.log_user_prompt=false",
        'approval_policy="never"',
        'sandbox_mode="read-only"',
        'shell_environment_policy.inherit="none"',
        *provider_values(mode, codex_version),
    )
    command = [codex_bin, "app-server", "--strict-config", "--stdio"]
    for value in values:
        command.extend(["-c", value])
    return command


def protocol_requests(model: str, thread_id: str = "") -> dict[str, dict[str, Any]]:
    """生成固定 JSON-RPC 请求，便于单测锁定 compact 生命周期。"""

    requests: dict[str, dict[str, Any]] = {
        "initialize": {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "sub2api-capture",
                    "title": "Sub2API Capture",
                    "version": "1.0.0",
                },
                "capabilities": {"experimentalApi": True},
            },
        },
        "initialized": {"method": "initialized", "params": {}},
        "thread_start": {
            "id": 2,
            "method": "thread/start",
            "params": {
                "model": model,
                "cwd": "/work",
                "approvalPolicy": "never",
                "sandbox": "read-only",
            },
        },
    }
    if thread_id:
        requests["turn_start"] = {
            "id": 3,
            "method": "turn/start",
            "params": {
                "threadId": thread_id,
                "input": [{"type": "text", "text": PROMPT, "text_elements": []}],
            },
        }
        requests["compact_start"] = {
            "id": 4,
            "method": "thread/compact/start",
            "params": {"threadId": thread_id},
        }
    return requests


def extract_thread_id(response: dict[str, Any]) -> str:
    """从 thread/start 响应提取线程标识，格式不符时失败关闭。"""

    result = response.get("result")
    result = result if isinstance(result, dict) else {}
    thread = result.get("thread")
    thread = thread if isinstance(thread, dict) else {}
    value = thread.get("id")
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("thread/start 响应缺少 thread.id。")
    return value.strip()


class AppServerClient:
    """对单个 app-server 子进程执行有超时的 JSONL 请求。"""

    def __init__(self, command: list[str], environment: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            command,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        self.records: list[dict[str, Any]] = []
        self.stderr_lines: list[str] = []
        self.lines: queue.Queue[str | None] = queue.Queue()
        if self.process.stdout is None or self.process.stderr is None:
            self.close()
            raise RuntimeError("app-server stdout/stderr 不可用。")
        self.stdout_thread = threading.Thread(
            target=self._pump_stdout,
            args=(self.process.stdout,),
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._pump_stderr,
            args=(self.process.stderr,),
            daemon=True,
        )
        self.stdout_thread.start()
        self.stderr_thread.start()

    def _pump_stdout(self, stream: TextIO) -> None:
        try:
            for line in stream:
                self.lines.put(line)
        finally:
            self.lines.put(None)

    def _pump_stderr(self, stream: TextIO) -> None:
        self.stderr_lines.extend(iter(stream.readline, ""))

    def send(self, payload: dict[str, Any]) -> None:
        if self.process.stdin is None:
            raise RuntimeError("app-server stdin 不可用。")
        self.process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def next_record(self, deadline: float) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("等待 app-server JSON-RPC 事件超时。")
        try:
            line = self.lines.get(timeout=remaining)
        except queue.Empty as error:
            raise TimeoutError("等待 app-server JSON-RPC 事件超时。") from error
        if line is None:
            raise RuntimeError("app-server 在场景完成前关闭 stdout。")
        try:
            value = json.loads(line)
        except ValueError as error:
            raise RuntimeError("app-server 输出了非 JSONL 数据。") from error
        if not isinstance(value, dict):
            raise RuntimeError("app-server 输出的 JSON-RPC 事件不是对象。")
        self.records.append(value)
        return value

    def wait_response(self, request_id: int, deadline: float) -> dict[str, Any]:
        while True:
            record = self.next_record(deadline)
            if record.get("id") != request_id:
                continue
            if "error" in record:
                raise RuntimeError(f"JSON-RPC 请求 {request_id} 返回错误。")
            return record

    def notification_count(self, method: str) -> int:
        return sum(record.get("method") == method for record in self.records)

    def wait_notification(
        self,
        method: str,
        deadline: float,
        *,
        after_count: int = 0,
    ) -> dict[str, Any]:
        if self.notification_count(method) > after_count:
            return next(
                record
                for record in reversed(self.records)
                if record.get("method") == method
            )
        while True:
            record = self.next_record(deadline)
            if (
                record.get("method") == method
                and self.notification_count(method) > after_count
            ):
                return record

    def close(self) -> None:
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()
        for thread in (getattr(self, "stdout_thread", None), getattr(self, "stderr_thread", None)):
            if thread is not None:
                thread.join(timeout=2)


def run_scenario(
    *,
    codex_bin: str,
    mode: str,
    model: str,
    timeout: int,
    codex_version: str = "0.145.0",
) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
    """执行首轮请求和手动 compact，并返回不可伪造的生命周期计数。"""

    environment = dict(os.environ)
    if mode == "official-http":
        environment.pop("OPENAI_API_KEY", None)
        environment.pop("SUB2API_API_KEY", None)
    elif not environment.get("SUB2API_API_KEY"):
        raise RuntimeError("Sub2API compact 模式缺少 SUB2API_API_KEY。")
    deadline = time.monotonic() + timeout
    client = AppServerClient(
        build_app_server_command(codex_bin, mode, codex_version), environment
    )
    valid = False
    error_type = ""
    thread_id = ""
    try:
        initial = protocol_requests(model)
        client.send(initial["initialize"])
        client.wait_response(1, deadline)
        client.send(initial["initialized"])
        client.send(initial["thread_start"])
        thread_id = extract_thread_id(client.wait_response(2, deadline))

        active = protocol_requests(model, thread_id)
        turn_completed_before = client.notification_count("turn/completed")
        client.send(active["turn_start"])
        client.wait_response(3, deadline)
        client.wait_notification(
            "turn/completed",
            deadline,
            after_count=turn_completed_before,
        )

        compact_turn_completed_before = client.notification_count("turn/completed")
        client.send(active["compact_start"])
        client.wait_response(4, deadline)
        client.wait_notification(
            "turn/completed",
            deadline,
            after_count=compact_turn_completed_before,
        )
        valid = True
    except BaseException as error:
        error_type = type(error).__name__
    finally:
        client.close()

    methods = [
        record.get("method")
        for record in client.records
        if isinstance(record.get("method"), str)
    ]
    summary = {
        "schema_version": "codex-compact-capture/v1",
        "mode": mode,
        "codex_version": codex_version,
        "model": model,
        "valid": valid,
        "error_type": error_type,
        "protocol_record_count": len(client.records),
        "turn_completed_count": methods.count("turn/completed"),
        "compact_turn_completed": methods.count("turn/completed") >= 2,
        "thread_compacted_count": methods.count("thread/compacted"),
        "thread_id_present": bool(thread_id),
    }
    return summary, client.records, "".join(client.stderr_lines)


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-bin", default="/usr/local/bin/codex-capture")
    parser.add_argument(
        "--mode", choices=("official-http", "sub2api-http"), required=True
    )
    parser.add_argument("--model", required=True)
    parser.add_argument("--codex-version", default="0.145.0")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=int, default=300)
    return parser.parse_args()


def main() -> int:
    arguments = parse_arguments()
    arguments.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    arguments.output_dir.chmod(0o700)
    summary, records, stderr = run_scenario(
        codex_bin=arguments.codex_bin,
        mode=arguments.mode,
        model=arguments.model,
        timeout=arguments.timeout,
        codex_version=arguments.codex_version,
    )
    runtime_secret = os.environ.get("SUB2API_API_KEY")
    serialized = "".join(
        json.dumps(record, ensure_ascii=False) + "\n" for record in records
    )
    if runtime_secret:
        serialized = serialized.replace(runtime_secret, "<redacted-runtime-secret>")
        stderr = stderr.replace(runtime_secret, "<redacted-runtime-secret>")
    secure_write(arguments.output_dir / "protocol.jsonl", serialized)
    secure_write(arguments.output_dir / "stderr.log", stderr)
    secure_write(
        arguments.output_dir / "summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
