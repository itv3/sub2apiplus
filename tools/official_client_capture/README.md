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

该入口只负责 stable 冻结、bundle 差分、控制组／目标组证据复核，以及发现清单、语义候选、SPEC 规则
台账和 EvidenceFact 封存；不得生成画像、Snapshot、Persona 注册或生产绑定，也不得自行签发
EvidenceApprovalFact。规则数量由已原子化且可断言的 SPEC 决定，不得按发现项数量生成。

FW-E 补强链为：

```bash
python3 tools/official_client_capture/claude_fw_e.py analyze-bundles --help
node tools/official_client_capture/claude_sink_containment.mjs \
  --bundle <cli.js> --inventory <target-sink-inventory.json> \
  --ast-inventory <target-native-ast.json> --output <sink-containment.json>
python3 tools/official_client_capture/claude_fw_e_dispositions.py --help
python3 tools/official_client_capture/claude_fw_e_validation_closure.py --help
python3 tools/official_client_capture/claude_fw_e_crosswalk.py --help
python3 tools/official_client_capture/claude_fw_e.py rule-assessments --help
python3 tools/official_client_capture/claude_fw_e_seal_plan.py --help
python3 tools/official_client_capture/claude_fw_e.py seal --help
```

`analyze-bundles` 自动调用 `claude_bundle_ast.mjs`，再由 `claude_target_inventory.py` 合并 AST 与无截断
词法候选。先用 containment 证据证明词法命中的真实 AST 位置，再由人工策略通过
`claude_fw_e_dispositions.py` 对目标 sink、2.1.88 候选、HitCC 原子线索／直接线索文档和全 host／path
运行坐标逐项签发。尚不清楚处置方式的项必须保留 `unclassified`；已原子化且绑定来源、但缺少目标语义
证明的项，只能由 `claude_fw_e_validation_closure.py` 登记为禁止进入 RuleLedger／生产的
`mapped_validation` 语义候选，并保留真实 `observed／blocked`。该工具无截断保存每个发现项，以
`source_ids` 多对一归并候选，不生成 SPEC。capture index 必须同时闭合 P／R／J／M，且关闭遥测与
非必要流量。

先运行 `claude_fw_e_crosswalk.py --require-explicit`，确认整个分母均已逐项审阅；再运行同一命令的
`--require-closed`，确认不存在 `unclassified` 后，才可继续 rule assessments 与 seal。显式覆盖通过但
证据闭集失败是合法阻断状态，不得因此进入 FW-F。`capture.py`、`capturelib/`、场景驱动器、解析器和
finalizer 是其底层依赖，不是另一套升级流程。旧的一次性脚本只有仍被版本场景清单引用时才属于正式
工具链。
