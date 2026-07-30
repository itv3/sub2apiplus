# Codex CLI 0.145.0 候选侧 42 条严格验收

## 验收目标

`candidate_42_acceptance.py` 把 `codex_upgrade_rules_0_145_0.json` 中的 42 条规则作为封闭全集。规则缺失、重复、越界，或任意规则缺少实现位置、触发条件、官方证据、候选原始证据、已执行断言、环境恢复证据时，整批验收失败。

该工具只读取归档文件，不执行提交中声明的触发命令或断言命令，避免验收动作再次修改生产环境。断言必须由抓包流程实际执行，并把结构化结果归档后再交给本工具复核。

## 文件约定

- `candidate_42_acceptance.schema.json`：整批验收提交的 JSON Schema。
- `candidate_rule_assertion_result.schema.json`：每条规则的已执行断言结果 Schema。
- `candidate_42_acceptance.example.json`：包含全部 42 个规则 ID 的填写骨架。骨架中的占位值故意不能通过验收，必须全部替换为真实内容。
- `candidate_rule_assertion_result.example.json`：单条规则断言结果的填写骨架，同样不能把占位内容用于正式验收。
- `candidate_42_acceptance.py`：最终失败关闭门禁。

提交中的所有文件路径都必须相对 `--source-root` 或 `--evidence-root`。绝对路径、`..`、符号链接、空文件、目录和常见占位文件都会失败。每个文件必须记录与磁盘内容一致的 SHA-256。

候选原始证据只能使用明确的原始证据类型，例如 `pcap`、`pcapng`、`mitm_jsonl`、`wire_dump`、`http_trace` 或 `websocket_trace`。汇总报告和断言结果不能冒充候选原始证据。`pcap`、`pcapng` 和 `mitm_jsonl` 还会执行基本格式检查。

## 断言结果

每条规则必须声明实际 checker 文件和可重放的命令参数数组。执行完成后生成独立的结构化结果文件，至少包含：

- checker 文件摘要；
- 命令参数数组的稳定摘要；
- 带时区的开始与结束时间；
- `status=pass` 和整数 `exit_code=0`；
- 至少一项逐规则语义检查；
- 每项检查的期望值、实际值及其引用的候选原始证据路径。

命令摘要的计算方式是：先将命令参数数组编码为 UTF-8 紧凑 JSON，再计算 SHA-256。可直接复用 Python 函数 `command_sha256`。只有任务成功或只有 `exit_code=0` 的文件必定失败。

## 环境恢复证据

每条规则至少需要一组 `before` 与 `after` 规范化状态文件。状态文件必须是包含实际状态字段的非空 JSON 对象，且应覆盖本次抓包可能修改的代理、fallback、CA、hosts、keeper、账号调度状态等项目。

采集时间写在文件引用的 `captured_at` 字段中，不要写入规范化状态文件本身。`before` 与 `after` 必须分别采集为两个不同文件或 inode，内容和 SHA-256 必须逐字节一致；`before` 要早于断言开始，`after` 要晚于断言结束。

## 执行方法

```bash
python3 -m tools.official_client_capture.candidate_42_acceptance \
  --submission /绝对路径/candidate-42-acceptance.json \
  --source-root /绝对路径/已部署源码快照 \
  --evidence-root /绝对路径/只读验收证据归档 \
  --report /绝对路径/candidate-42-acceptance-report.json
```

退出码 `0` 表示 42 条全部满足严格门禁；退出码 `1` 表示至少一个失败项。标准输出和 `--report` 文件均包含逐项错误代码、字段路径及规则 ID。最终验收只能以报告中的 `accepted=true`、`required_rule_count=42`、`submitted_rule_count=42`、`error_count=0` 为通过条件。

## 离线测试

```bash
python3 -m unittest -v \
  tools.official_client_capture.tests.test_candidate_42_acceptance
```

## 版本画像驱动的候选断言

`candidate_rule_expectations_0_145_0.json` 是候选验收使用的独立冻结画像。它包含
A01～A15 场景、与规则清单顺序完全一致的 42 个规则，以及逐规则语义检查；画像不导入
也不读取候选 Go 常量。画像还绑定了本规格文档的 SHA-256，checker 源码固定了画像文件
的 SHA-256。修改画像必须同步修改 checker 固定摘要并经过独立审核，不能在抓包时临时
放宽预期。

统一抓包清单遵循 `candidate_capture_manifest.schema.json`：

```json
{
  "schema_version": "codex-candidate-capture-manifest/v1",
  "codex_version": "0.145.0",
  "capture_id": "candidate-42-<批次>",
  "status": "complete",
  "artifacts": [
    {
      "path": "candidate/A09/conn001.client_to_upstream.bin",
      "sha256": "<原始文件 SHA-256>",
      "kind": "relay_binary",
      "parser": "h1_request_stream",
      "scenario_ids": ["A09"],
      "labels": {
        "protocol": "http",
        "provider": "openai_oauth",
        "endpoint_class": "auxiliary"
      }
    }
  ]
}
```

支持的独立解析入口如下：

- `pcap_client_hello`：直接从经典 pcap 解析 ClientHello、SNI、cipher、扩展序和 ALPN；
- `h1_request_stream`：直接从 relay 上行字节解析 H1 头序、body 结构和 WS 帧；
- `observation_json`／`observation_jsonl`：读取 process/http/websocket trace 中的结构化
  事实，记录必须符合 `candidate_observation.schema.json`；
- `opaque_bound_source`：保存暂不能直接解析的下行 relay、pcapng 或 TLS keylog；它
  本身不能产生断言事实，必须被结构化 trace 的 `source_artifacts` 显式引用。

结构化 trace 不允许只写 `matched=true`。它应保存可复算事实，例如 invocation_id／
connection_id 哈希序列、阶段序列、字段序列或输入输出值哈希。`source_artifacts` 必须引用
同一 capture manifest 已声明的原始文件，checker 会把 trace 与这些原始文件共同写入
`evidence_paths`。未声明来源、路径逃逸、符号链接、空文件、摘要不符、场景证据类型不全
都会失败关闭。

逐规则执行示例：

```bash
python3 tools/official_client_capture/candidate_rule_assertion.py \
  --rule-id SPEC-EP-006 \
  --capture-manifest /证据根/capture-manifest.json \
  --evidence-root /证据根 \
  --profile tools/official_client_capture/candidate_rule_expectations_0_145_0.json \
  --rule-manifest tools/official_client_capture/codex_upgrade_rules_0_145_0.json \
  --output /证据根/assertions/SPEC-EP-006.result.json
```

正式 submission 的 `assertion.command` 必须使用相同参数数组；checker 以该规范数组生成
`command_sha256`。只有所有检查通过时，输出才具有
`status=pass`、`exit_code=0`，可供最终 42 条门禁使用。

### 已部署 Go 测试事实转换

调用链、状态链和连接生命周期等抽象事实必须按
[`CANDIDATE_TEST_TRACE.md`](CANDIDATE_TEST_TRACE.md) 执行：在已部署源码快照上运行冻结的
`go test -json -count=1` 测试，再由 `candidate_test_trace.py` 生成绑定测试日志、源码摘要和
同场景 relay 的 trace。该流程不能生成或替代 HTTP、TLS、WebSocket 原始线事实；抓包缺失
时仍然失败关闭。

## 状态快照与秘密扫描

先准备一份只含稳定状态的 JSON。凭据只能记录 `*_sha256`、`*_present` 等派生值；
`access_token`、`refresh_token`、`authorization`、cookie、password 等原文，以及
timestamp、uptime、restart_count 等自然波动字段会被拒绝。

```bash
python3 tools/official_client_capture/candidate_evidence_guard.py snapshot \
  --input /受控临时目录/state-before-input.json \
  --output /证据根/restoration/before.json

# 完成抓包、精确恢复和全部离线断言后，再以相同状态生成另一个普通文件。
# 这样 after.captured_at 晚于所有 assertion.finished_at。
python3 tools/official_client_capture/candidate_evidence_guard.py snapshot \
  --input /受控临时目录/state-after-input.json \
  --output /证据根/restoration/after.json

python3 tools/official_client_capture/candidate_evidence_guard.py verify \
  --before /证据根/restoration/before.json \
  --after /证据根/restoration/after.json \
  --capture-manifest /证据根/capture-manifest.json \
  --evidence-root /证据根 \
  --secret-env SUB2API_CAPTURE_KEY \
  --secret-env OAUTH_DUMMY_REFRESH_TOKEN \
  --report /证据根/restoration/evidence-guard-report.json
```

`verify` 要求 before/after 是不同 inode 且规范化字节完全一致，并先核对 manifest 中
每个 artifact 的 SHA-256。秘密扫描覆盖文本与二进制文件，包括 pcap 和 relay 字节；
既扫描本轮通过环境变量提供的已知秘密，也扫描 Authorization、Cookie、OAuth token、
OpenAI key 和 JWT 等高置信形态。报告只记录环境变量名、规则与字节偏移，不记录秘密原文。

新增基础设施的离线测试：

```bash
python3 -m unittest -v \
  tools.official_client_capture.tests.test_candidate_rule_assertion \
  tools.official_client_capture.tests.test_candidate_evidence_guard
```

## 正式验收包生成器

`candidate_acceptance_bundle.py` 负责把上述原始材料收口成正式 submission。它不会猜测
实现位置、触发命令、官方观察或部署身份，也不会把失败断言改写为通过。由于最终门禁要求
`after.captured_at` 晚于全部断言结束时间，流程严格分为两个阶段：

1. `assert` 从统一 capture manifest 逐条运行 `candidate_rule_assertion.py`，生成恰好
   42 个结果和一个绑定 checker、画像、规则清单、manifest 摘要的索引；
2. 精确恢复环境并采集新的 `after` 规范化状态；
3. `finalize` 现场重新解析原始证据并复算 42 条 checks，要求与归档结果逐字段一致；随后
   校验实现源码、归档官方证据、执行 restoration 和秘密扫描，生成 submission，最后调用
   `candidate_42_acceptance.py` 取得正式报告。

第一阶段示例：

```bash
python3 tools/official_client_capture/candidate_acceptance_bundle.py assert \
  --source-root /绝对路径/已部署源码快照 \
  --evidence-root /绝对路径/验收证据归档 \
  --capture-manifest /绝对路径/验收证据归档/capture-manifest.json \
  --assertions-dir assertions/candidate-42-20260730T120000Z
```

`--assertions-dir` 必须是未使用过的证据根内相对路径。生成器拒绝覆盖既有断言，以免一次
失败或重跑抹去先前执行事实。

`finalize` 额外消费三份人工确认的事实映射。规则元数据必须精确覆盖 42 条规则；文件摘要
由生成器从 `source-root` 计算，符号必须真实出现在给定行范围内：

```json
{
  "schema_version": "codex-candidate-bundle-rule-metadata/v1",
  "codex_version": "0.145.0",
  "rules": [
    {
      "rule_id": "SPEC-TLS-001",
      "implementation": {
        "summary": "默认 HTTP TLS 由 0.145.0 版本画像编译并绑定到出站传输",
        "locations": [
          {
            "path": "backend/internal/service/official_egress_codex_0145_profile.go",
            "line_start": 90,
            "line_end": 105,
            "symbol": "officialCodexVersionProfile"
          }
        ]
      },
      "trigger_command": ["/验收脚本", "--scenario", "A01"]
    }
  ]
}
```

官方证据映射同样必须精确覆盖 42 条。`path` 相对 `--official-evidence-root`，摘要必须由
采集归档实际计算；`finalize` 会把字节原样归档到 evidence-root 的
`--official-bundle-prefix` 下，并在写入前后复核摘要：

```json
{
  "schema_version": "codex-candidate-official-evidence-map/v1",
  "codex_version": "0.145.0",
  "rules": [
    {
      "rule_id": "SPEC-TLS-001",
      "observation": "官方 0.145.0 默认 HTTP ClientHello 的实测结论",
      "artifacts": [
        {
          "path": "A01/http-clienthello.pcap",
          "sha256": "<64 位小写 SHA-256>",
          "kind": "pcap"
        }
      ]
    }
  ]
}
```

candidate identity 文件只允许以下五个字段；它是部署侧事实声明，生成器校验格式但无法从
离线源码反证远端容器身份，因此应直接取部署记录中的 commit、源码树摘要、不可变镜像引用、
镜像 digest 和运行版本，禁止手填推测值：

```json
{
  "git_commit": "<40 位小写提交 ID>",
  "source_tree_sha256": "<部署源码树摘要>",
  "image_reference": "sub2apiplus:<唯一构建标签>",
  "image_digest": "sha256:<64 位镜像摘要>",
  "deployed_version": "<运行实例报告的版本>"
}
```

最终组包示例：

```bash
python3 tools/official_client_capture/candidate_acceptance_bundle.py finalize \
  --source-root /绝对路径/已部署源码快照 \
  --evidence-root /绝对路径/验收证据归档 \
  --capture-manifest /绝对路径/验收证据归档/capture-manifest.json \
  --assertions-dir assertions/candidate-42-20260730T120000Z \
  --rule-metadata /绝对路径/rule-metadata.json \
  --official-evidence-map /绝对路径/official-evidence-map.json \
  --official-evidence-root /绝对路径/官方0.145.0证据 \
  --candidate-identity /绝对路径/candidate-identity.json \
  --before-state /绝对路径/验收证据归档/restoration/before.json \
  --before-captured-at 2026-07-30T11:50:00+08:00 \
  --after-state /绝对路径/验收证据归档/restoration/after.json \
  --after-captured-at 2026-07-30T12:30:00+08:00 \
  --assessment-id candidate-42-20260730T120000Z \
  --secret-env SUB2API_CAPTURE_KEY \
  --secret-env OAUTH_DUMMY_REFRESH_TOKEN
```

成功时会生成：

- `candidate-42-acceptance.json`；
- `candidate-42-acceptance-report.json`；
- `restoration/candidate-evidence-guard-report.json`；
- `assertions/<批次>/` 下的 42 个结果及 `assertion-index.json`。

正式 submission、最终报告和守卫报告同样拒绝覆盖；同一证据归档重试时应通过对应
`--*-path` 参数选择新的批次路径，保留失败批次用于审计。

只有最终报告同时满足 `accepted=true`、两项规则数均为 42、`error_count=0` 时，正式包才
合格。相关离线测试：

```bash
python3 -m unittest -v \
  tools.official_client_capture.tests.test_candidate_acceptance_bundle
```
