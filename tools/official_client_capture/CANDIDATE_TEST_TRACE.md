# Codex CLI 0.145.0 候选 Go 测试事实 Trace

## 1. 用途与边界

`candidate_test_trace.py` 把已部署源码快照中实际执行的严格 Go 验收测试，转换为
`candidate_rule_assertion.py` 可读取的抽象结构化事实。它只补充原始字节无法独立表达的
调用链、状态链和生命周期事实，不能替代抓包。

生成器具有以下失败关闭约束：

- 只接受 `candidate_test_fact_map_0_145_0.json` 中精确列出的 11 个测试和 31 个事实；
- 冻结映射、规则画像、测试文件和绑定的生产 Go 源文件必须逐字节匹配 SHA-256；
- 测试必须使用 `go test -json -count=1` 实际运行，且每个测试和测试包都恰好通过一次；
- 缓存、失败、跳过、缺失或重复 marker、映射外测试和映射外事实一律失败；
- 每个事实必须绑定同场景的原始 `relay_binary`，测试日志本身不能冒充抓包；
- 只生成抽象 `process_trace` 或 `websocket_trace`，禁止生成 `http_request`、
  `tls_client_hello` 和 `websocket_frame` 原始线事实；
- 所有输出使用独占创建，已有文件一律拒绝覆盖。

A01、A02、A12、A13 以及其他 HTTP、TLS、WebSocket 原始线规则仍必须直接解析 pcap 或
relay。测试 trace 不能为缺失的原始抓包补证。

## 2. 在已部署源码快照执行测试

`SOURCE_ROOT` 必须指向与实际运行实例同一部署批次的只读源码快照，不能指向随后又发生
修改的开发目录。`EVIDENCE_ROOT` 是本轮候选抓包归档根。

```bash
set -euo pipefail

SOURCE_ROOT=/绝对路径/已部署源码快照
EVIDENCE_ROOT=/绝对路径/候选验收证据

mkdir -p "$EVIDENCE_ROOT/candidate"
(
  cd "$SOURCE_ROOT/backend"
  go test -json -count=1 ./internal/service ./internal/repository \
    -run '^(TestCandidateTraceCodex0145.*|TestUploadOfficialCodexFileExecutesProfileDrivenThreeStepFlow)$'
) >"$EVIDENCE_ROOT/candidate/go-test-candidate-0145.jsonl"
```

必须保留未经筛选或重排的原始 JSONL。不要只复制 `CANDIDATE_TRACE_FACT` 行，因为生成器
还会核对测试名、包名、`run`／`pass` 事件和包级结果。

## 3. 把日志加入原始 capture manifest

基础 manifest 先由真实候选抓包流程生成，并为 A03～A11、A14、A15 中实际使用的场景
分别声明至少一个 `relay_binary`。随后把原始 Go 测试日志声明为：

```json
{
  "path": "candidate/go-test-candidate-0145.jsonl",
  "sha256": "<原始日志的 64 位小写 SHA-256>",
  "kind": "stdout_log",
  "parser": "opaque_bound_source",
  "scenario_ids": [
    "A03",
    "A04",
    "A05",
    "A06",
    "A07",
    "A08",
    "A09",
    "A10",
    "A11",
    "A14",
    "A15"
  ],
  "labels": {
    "command": "go test -json -count=1",
    "source": "deployed-candidate"
  }
}
```

这里的 `opaque_bound_source` 只允许日志被生成后的 observation 引用，不会直接产生任何
验收事实。每个 relay 的路径、摘要、场景和方向仍按
`candidate_capture_manifest.schema.json` 独立声明。

## 4. 生成结构化 trace

```bash
python3 tools/official_client_capture/candidate_test_trace.py \
  --source-root "$SOURCE_ROOT" \
  --evidence-root "$EVIDENCE_ROOT" \
  --capture-manifest "$EVIDENCE_ROOT/capture-manifest.raw.json" \
  --go-test-artifact candidate/go-test-candidate-0145.jsonl \
  --trace-dir generated/go-test-traces \
  --output-manifest generated/capture-manifest.with-test-traces.json \
  --output-receipt generated/candidate-test-trace-receipt.json
```

成功后生成：

- `generated/go-test-traces/<场景>/go-test-facts.jsonl`：按场景分组的抽象事实；
- `generated/capture-manifest.with-test-traces.json`：保留原始 artifact，并追加 trace；
- `generated/candidate-test-trace-receipt.json`：绑定映射、画像、日志、源码和输出摘要。

后续 42 条逐规则断言必须使用追加后的 manifest。任何测试或生产源码发生修改，都必须先
审核测试与事实映射，再更新冻结摘要并重新执行整批测试，不能临时关闭摘要校验。

## 5. 本地失败关闭测试

```bash
python3 -m unittest -v \
  tools.official_client_capture.tests.test_candidate_test_trace \
  tools.official_client_capture.tests.test_candidate_rule_assertion
```
