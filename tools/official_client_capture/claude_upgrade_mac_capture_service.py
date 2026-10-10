"""原 regression_fixture 服务的固定薄壳，用途与旧合同不变。"""
from __future__ import annotations
import argparse
import signal
from pathlib import Path
from typing import Any
from tools.official_client_capture import claude_mac_capture_contract as c
from tools.official_client_capture import claude_mac_capture_fixture_policy as policy
from tools.official_client_capture.claude_mac_capture_engine_core import (
    RawCaptureCore, ConnectionWorker, _mac, receive, serve)

class CaptureEngine(RawCaptureCore):
    """只选择旧夹具策略，不接受正式任务或正式预算。"""
    _policy = policy

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--configuration", type=Path, required=True)
    parser.add_argument("--configuration-sha256", required=True)
    arguments = parser.parse_args()
    configuration = c.read_json(arguments.configuration)
    c.require(c.digest(configuration) == arguments.configuration_sha256, "启动配置 SHA 与独立冻结值不符")
    engine = CaptureEngine(configuration)
    def request_stop(_number: int, _frame: Any) -> None:
        if (engine.root / "reservation.json").exists():
            engine.close(engine.task_sha, "shutdown")
        engine.stop_event.set()
    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    serve(engine)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
