# 最终请求内存验收复现

本目录只保存最终 42 场结果及验证证据；上一级目录保留前一阶段资料。大正文、测试二进制与样本中的随机密文不入库。原测量文件在实验宿主 `ARM64` 的 `/tmp/sub2api-request-memory-20261006.Vd9iuP`，本机取回证据位于 `/tmp/sub2api-memory-optimization-20261006`。临时目录可能被清理；重新生成样本时须登记新摘要，不能宣称与历史正文逐字节相同。

## 1. 核对现有证据

在仓库根目录执行，不需要容器或正文：

```sh
python3 docs/REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/final/verify_results.py --repository "$PWD"
```

脚本核对全部原始字节数、2.5 倍判定、42 场退出／OOM／资源释放、48 项 CI 结果、日志及源码摘要。后续有意修改了源码时，可去掉 `--repository` 只核对历史证据。`evidence-sha256.json` 是归档完整性清单，不是发布签名。

`container-logs.jsonl`、`ci-logs.jsonl`、`targeted-test-logs.jsonl` 每行保存原路径、原文件名、原始 UTF-8 内容及 SHA-256，避免 `.log` 被仓库忽略而遗漏。`backend-regression.log.gz` 是 service 等业务包的完整原日志，准确失败状态及后续修复说明另见 `backend-regression-summary.json`。`ci-summary.json`、`ci-manifest.json` 与 `ci-unit-records.jsonl` 保留统一执行器的原始记录。

## 2. 在待测容器外准备正文

使用 Go 1.27.1，在仓库 `backend` 目录执行。以下为原生 16.8MiB；按 `cases.json` 的唯一形态和尺寸准备其余文件。既有固定文件由测试复用，不要覆盖既有对照。

```sh
memoryRunRoot=/tmp/sub2api-memory-recheck-20261006
mkdir -p "$memoryRunRoot/fixtures" "$memoryRunRoot/ws-fixtures"

GOTOOLCHAIN=go1.27.1 \
SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_FIXTURE=1 \
SUB2API_OFFICIAL_EGRESS_MEMORY_SHAPE=native \
SUB2API_OFFICIAL_EGRESS_MEMORY_BODY_MIB=16.8 \
SUB2API_OFFICIAL_EGRESS_MEMORY_FIXTURE="$memoryRunRoot/fixtures/native-16.8.json" \
SUB2API_OFFICIAL_EGRESS_MEMORY_WIRE_FIXTURE="$memoryRunRoot/fixtures/native-16.8.zst" \
go test ./internal/service -run '^TestOfficialEgressMemoryPrepareFixture$' -count=1 -v

GOTOOLCHAIN=go1.27.1 \
SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_WS_SESSION_FIXTURE=1 \
SUB2API_OFFICIAL_EGRESS_MEMORY_SHAPE=native \
SUB2API_OFFICIAL_EGRESS_MEMORY_FIXTURE="$memoryRunRoot/fixtures/native-16.8.json" \
SUB2API_OFFICIAL_EGRESS_MEMORY_REQUESTS_PER_SLOT=2 \
SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR="$memoryRunRoot/ws-fixtures/native-16.8" \
go test ./internal/service -run '^TestOfficialEgressWSMemoryPrepareFixture$' -count=1 -v
```

完整 HTTP 样本为原生／显式 `instructions` 各 0.3、1、4、16.8、33.6、50、80MiB，以及难压缩 16.8、50、80MiB。zstd wire 需要原生 16.8、50MiB 及显式指令 16.8、50MiB。WS 预制目录为原生 4、16.8、50MiB 与显式指令 16.8MiB，每个两帧。所有样本和 WS manifest 都在待测容器外生成，实际长度及摘要见 `fixture-manifests.json`。

工具续接样本在原生 16.8／50MiB 的 `input` 尾部再加下面两个对象，顶层其他内容不变，保存为 `tool-continuation-16.8.json`、`tool-continuation-50.json`。原测量使用 UTF-8、非 ASCII 转义、无额外缩进的 JSON：

```json
[
  {"type":"custom_tool_call","name":"exec","call_id":"call_memory_active","input":"继续检查结果","status":"completed"},
  {"type":"custom_tool_call_output","call_id":"call_memory_active","output":"上一步工具执行完成，请继续处理剩余任务"}
]
```

坏密文重试由 `cases.json` 中的 `retry_first` 触发，桩上游实际返回错误，服务清洗后再次发送；不要用单纯重复调用替代这个场景。

## 3. 编译并串行运行全新容器

```sh
GOTOOLCHAIN=go1.27.1 CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
go test -c -o "$memoryRunRoot/memory-final.test" ./internal/service
```

把二进制、全部样本、WS 目录、本目录的 runner 和 cases 复制到 Linux ARM64 实验宿主。以下在宿主上执行，`memoryRunRoot` 改为实际实验目录：

```sh
python3 "$memoryRunRoot/run_container_matrix.py" \
  --root "$memoryRunRoot" --binary memory-final.test \
  --label recheck-owned-final-1 --cases "$memoryRunRoot/cases.json"
```

runner 的底图是 `ghcr.io/itv3/sub2apiplus:0.2.13-2`；原始镜像 digest 记于 `summary.json`，重测前应核对它。每场新建 768MiB、2 CPU、无外网容器，固定 GOGC=100、GOMEMLIMIT=512MiB、GOMAXPROCS=2，使用普通磁盘。结果追加至 `<标签>-results.jsonl`，每场结束移除测试容器；每轮选新标签，避免混入旧记录。runner 名称只针对自己的实验容器，不涉及生产服务。

必须读取 `request_memory_peak_bytes`，不能用纯堆字段代替。WS 分母使用各连接最大单帧之和；连续 HTTP 使用同时在途原文，不累计所有请求字节。不要在请求间强制 GC、提高阈值、改用 tmpfs 或从目标中扣掉匿名映射。新结果不得覆盖本目录的最终归档。

## 4. 回归范围

最终门禁命令为 `GOTOOLCHAIN=go1.27.1 make check-egress-spec-ci`，包括 48 项并行检查；精确子命令、退出码和耗时见执行器记录。业务目录另执行 `go build ./cmd/server`、handler／httputil／service／openai_ws_v2 的 `go vet`，以及正文所有权、跨轮持有、早响应异步上传、坏密文重试和入口准入的功能／race 验证。大正文测量串行运行，避免并行实验相互污染 cgroup 和延迟。

本目录证明约定矩阵的结果，未覆盖真实公网、完整鉴权／调度 handler 和长期生产混合负载；不作为调低线上预算系数或提高请求上限的自动授权。
