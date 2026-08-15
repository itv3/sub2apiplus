# Codex 官方出站抓包工具

本目录只保存抓包、解析、比较和验收工具，不维护另一套操作手册。Codex CLI 0.145.0 的
证据生成、规则正文、Sub2API 客户端仿真实现、工具责任、安全约束及升级流程，唯一以
[`docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](../../docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md) 为准。

执行任何抓包或升级任务前，必须先阅读主文档 §2.1、第四部分前置约定和 §4.1～§4.6。

唯一编排入口：

```bash
python3 tools/official_client_capture/codex_upgrade.py --help
```

`capture.py`、`capturelib/`、场景驱动器、解析器和 finalizer 是编排器的底层依赖，不是另一套
升级流程。旧的一次性脚本只有仍被版本场景清单引用时才属于正式工具链。
