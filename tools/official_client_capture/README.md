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

Claude 的 FW-E 受管入口为：

```bash
python3 tools/official_client_capture/claude_fw_e.py --help
```

该入口只负责 stable 冻结、bundle 差分、控制组／目标组证据复核、57 条迁移台账和
EvidenceFact 封存；不得生成画像、Snapshot、Persona 注册或生产绑定，也不得自行签发
EvidenceApprovalFact。`capture.py`、`capturelib/`、场景驱动器、解析器和 finalizer 是其底层
依赖，不是另一套升级流程。旧的一次性脚本只有仍被版本场景清单引用时才属于正式工具链。
