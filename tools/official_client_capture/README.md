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

该入口只负责 stable 冻结、bundle 差分、控制组／目标组证据复核、由目标闭集派生的动态规则台账和
EvidenceFact 封存；不得生成画像、Snapshot、Persona 注册或生产绑定，也不得自行签发
EvidenceApprovalFact。目标规则数量不得固定为 2.1.220 的 57 条。

FW-E 补强链为：

```bash
python3 tools/official_client_capture/claude_fw_e.py analyze-bundles --help
python3 tools/official_client_capture/claude_fw_e_crosswalk.py --help
python3 tools/official_client_capture/claude_fw_e.py rule-assessments --help
python3 tools/official_client_capture/claude_fw_e.py seal --help
```

`analyze-bundles` 自动调用 `claude_bundle_ast.mjs`，再由 `claude_target_inventory.py` 合并 AST 与无截断
词法候选。`claude_fw_e_crosswalk.py --require-closed` 必须绑定关闭 host 预筛的新 capture index；
任何目标 sink、历史候选、HitCC 线索或运行 host／path 未处置都会失败。`capture.py`、`capturelib/`、
场景驱动器、解析器和 finalizer 是其底层依赖，不是另一套升级流程。旧的一次性脚本只有仍被版本场景
清单引用时才属于正式工具链。
