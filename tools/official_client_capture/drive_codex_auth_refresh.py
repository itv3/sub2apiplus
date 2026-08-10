#!/usr/bin/env python3
"""驱动官方 Codex CLI 立即发出一次真实的 OAuth token 刷新。

为什么需要它
------------
A13 要采的是刷新请求的真实 wire 形态。此前只有两条路，都不通：

- 改 `auth.json` 的 `last_refresh`：0.147 的 `should_refresh_proactively`
  （`login/src/auth/manager.rs:2762-2783`）先解 access token JWT 的 `exp`，解得出就
  直接按 `exp <= now + 5min` 返回，根本不看 `last_refresh`；
- 等 JWT 自然进入 5 分钟窗口：access token 有效期约 8 天，采集要卡在到期前那 5 分钟。

第三条路是官方自己提供的：app-server 的 `getAuthStatus` 接受 `refreshToken: true`，
`account_processor.rs:934-947` 会直接调用 `auth_manager.refresh_token()`；而
`refresh_token()`（`manager.rs:2623-2660`）**不检查 exp**——只对 API key 与 PAT 认证
提前返回，ChatGPT OAuth 认证一律走真实刷新。

所以这不是伪造，而是官方 CLI 自身的正常代码路径，且随时可复现。

刷新成功后 CLI 会用轮换后的 refresh_token 改写 `auth.json`，采集侧不回灌
（见 SCN-REALITY-01 §14.2）。

用法：

    python3 drive_codex_auth_refresh.py --codex-version 0.147.0 \\
        --events-output <run>/scenario-observations/A13-auth-events.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import threading
import time


class AppServer:
    def __init__(self, argv: list[str]):
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self.responses: dict[int, dict] = {}
        self.notifications: list[dict] = []
        self.call_errors: list[dict] = []
        self.lock = threading.Lock()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()
        self._next_id = 0

    def _pump_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                if "id" in msg and ("result" in msg or "error" in msg):
                    self.responses[msg["id"]] = msg
                else:
                    self.notifications.append(msg)
                    if msg.get("method"):
                        print(f"    [通知] {msg['method']}", flush=True)

    def _pump_stderr(self):
        for line in self.proc.stderr:
            s = line.strip()
            if any(k in s.lower() for k in ("error", "refresh", "warn", "panic", "auth")):
                print(f"    [stderr] {s[:400]}", flush=True)

    def call(self, method: str, params: dict, timeout: float = 60.0):
        self._next_id += 1
        rid = self._next_id
        request = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        print(f"  → {method}", flush=True)
        self.proc.stdin.write(json.dumps(request) + "\n")
        self.proc.stdin.flush()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            with self.lock:
                if rid in self.responses:
                    msg = self.responses.pop(rid)
                    if "error" in msg:
                        print(f"    ✗ {json.dumps(msg['error'], ensure_ascii=False)[:200]}",
                              flush=True)
                        self.call_errors.append({"method": method, "error": msg["error"]})
                        return None
                    print("    ✓", flush=True)
                    return msg.get("result")
            time.sleep(0.1)
        print(f"    ✗ 超时 {timeout}s", flush=True)
        self.call_errors.append({"method": method, "error": {"message": f"timeout {timeout}s"}})
        return None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _observe_auth(path: pathlib.Path) -> dict | None:
    """读取 auth.json 的非秘密观测：exp、token 摘要、文件字节摘要。

    token 本体绝不返回、绝不落盘。
    """

    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    token = ((document.get("tokens") or {}).get("access_token")) or ""
    if not token:
        return None
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = int(json.loads(base64.urlsafe_b64decode(payload))["exp"])
    except Exception:  # noqa: BLE001
        return None
    import datetime

    return {
        "exp_at_utc": datetime.datetime.fromtimestamp(exp, datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
        "auth_file_sha256": hashlib.sha256(raw).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--codex-bin", default="/root/.local/bin/codex")
    parser.add_argument("--codex-version", default="0.147.0")
    parser.add_argument("--auth-json", default="/root/.codex/auth.json")
    parser.add_argument("--events-output", default=None)
    parser.add_argument("--disable", action="append", default=[])
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+\.\d+", args.codex_version):
        parser.error("--codex-version 必须是三段数字")

    auth_path = pathlib.Path(args.auth_json)
    before = _observe_auth(auth_path)
    if before is None:
        print("无法读取采集账号的 auth.json 或其中没有 access_token", flush=True)
        return 2

    argv = [args.codex_bin, "app-server"]
    for feature in args.disable or []:
        argv += ["--disable", feature]
    print(f"启动: {' '.join(argv)}", flush=True)
    srv = AppServer(argv)
    status = None
    try:
        initialized = srv.call("initialize", {
            "clientInfo": {"name": "codex_exec", "version": args.codex_version,
                           "title": "Codex"},
            "capabilities": {},
        })
        if initialized is None:
            print("initialize 失败，无法继续", flush=True)
            return 2
        # v2 的 `account/read` 带 refreshToken=true 会在返回前触发一次主动刷新
        # （`app-server-protocol/src/protocol/v2/account.rs:481-489`：
        # 「requests a proactive token refresh before returning … triggers the normal
        # refresh-token flow」），最终落到 auth_manager.refresh_token()，不看 exp 窗口。
        #
        # 注意 v1 的 getAuthStatus 也有同名参数，但 initialize 后 app-server 跑在 v2，
        # 那条路不会生效——实测调用返回成功却一个刷新请求都没发。
        status = srv.call("account/read", {"refreshToken": True}, timeout=90)
        if status is None:
            print("account/read 失败，未发出刷新请求", flush=True)
    finally:
        after = _observe_auth(auth_path)
        _write_events(args.events_output, srv, before, after, status)
        srv.close()
    if status is None:
        return 2
    # 刷新成功必然改写 auth.json；前后一致说明请求没真正落盘。
    after = _observe_auth(auth_path)
    if after is None or after["auth_file_sha256"] == before["auth_file_sha256"]:
        print("auth.json 未发生变化，刷新没有真正落盘", flush=True)
        return 3
    print("刷新完成，凭据已被轮换改写", flush=True)
    return 0


def _write_events(output, srv: AppServer, before, after, status) -> None:
    """落原始观测；只提取事实，成败由收据构建器判定。"""

    if not output:
        return
    with srv.lock:
        notifications = list(srv.notifications)
        errors = list(srv.call_errors)
    import datetime

    payload = {
        "schema_version": "codex-egress-auth-refresh-events/v1",
        # 与 scenario_receipts.A13_TRIGGERS 的登记值一致。
        "trigger": "app_server_refresh_request",
        "observed_at_utc": datetime.datetime.now(datetime.timezone.utc)
        .isoformat()
        .replace("+00:00", "Z"),
        "notifications": notifications,
        "call_errors": errors,
        "refresh_requested": True,
        "status_returned": status is not None,
        "before": before,
        "after": after,
    }
    path = pathlib.Path(output)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(f"  事件日志: {path}", flush=True)


if __name__ == "__main__":
    sys.exit(main())
