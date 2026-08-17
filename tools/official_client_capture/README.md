# 官方客户端出站抓包工具

本目录只保存抓包、解析、比较和验收工具，不维护另一套操作手册。共享控制框架以
[`docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md`](../../docs/OFFICIAL_CLIENT_EMULATION_FRAMEWORK.md)
为准；各客户端入口如下：

| 客户端 | 唯一权威手册 | 机器证据入口 |
|---|---|---|
| Codex CLI | [`CODEX_CLI_CLIENT_EMULATION_GUIDE.md`](../../docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md) | [`EVIDENCE_INDEX.md`](../../docs/EVIDENCE_INDEX.md) |
| Claude Code | [`CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md`](../../docs/CLAUDE_CODE_CLIENT_EMULATION_GUIDE.md) | [`claude_21220/`](claude_21220/) 的规则与覆盖台账 |

执行任何抓包或升级任务前，必须先阅读共享框架和对应客户端手册。

Codex 当前唯一编排入口：

```bash
python3 tools/official_client_capture/codex_upgrade.py --help
```

Claude 当前只有取证、bundle 分析和 2.1.220 覆盖门禁，尚无与 Codex 等价的受管升级编排器。
`capture.py`、`capturelib/`、场景驱动器、解析器和 finalizer 是底层依赖，不是另一套升级流程。
旧的一次性脚本只有仍被版本场景清单引用时才属于正式工具链。
