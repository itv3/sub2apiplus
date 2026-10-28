#!/usr/bin/env python3
"""整链空跑自检用的假客户端：复刻上一版官方客户端的一条启动规则，不联网、不读任何凭据。

规则（上一版升级里入口第 13 步探测 5 个组合 3 个失败的根因）：TUI 带功能开关覆盖（``--disable`` 或 ``--enable``）而没有
``--no-daemon`` 时，客户端会去核对共享后台服务的功能状态，不一致就打印拒绝原因并以非零码退出；其余组合正常启动
（这里用“打印横幅后等待”代表 TUI 起来了）。

只由 ``chain_rehearsal_blindspots`` 的启动探测自检使用：证明探测在“新版二进制拒绝调用点参数”时确实能报出。真实二进制不走这里。
"""

from __future__ import annotations

import sys
import time

arguments = sys.argv[1:]
overrides = [item for item in arguments if item in {"--disable", "--enable"}]
if overrides and "--no-daemon" not in arguments:
    sys.stderr.write(
        "Error: Cannot use the shared background server with feature overrides; "
        "rerun with --no-daemon to use the embedded app-server.\n"
    )
    raise SystemExit(1)
sys.stdout.write("OpenAI Codex (stub) ready\n")
sys.stdout.flush()
time.sleep(30)
