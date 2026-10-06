# 请求内存测量复现入口

在仓库 `backend` 目录执行。以下以原生 16.8MiB 样本为例；完整矩阵执行前，按 cases 中的形态、尺寸、wire 和 WS 目录逐项准备齐全。测量用例默认跳过；准备样本与真正采样必须分开。

## 1. 在待验收容器外准备样本

```sh
memoryRunRoot=/tmp/sub2api-memory-recheck-20261006
mkdir -p "$memoryRunRoot/fixtures" "$memoryRunRoot/ws-fixtures"

GOTOOLCHAIN=local \
SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_FIXTURE=1 \
SUB2API_OFFICIAL_EGRESS_MEMORY_SHAPE=native \
SUB2API_OFFICIAL_EGRESS_MEMORY_BODY_MIB=16.8 \
SUB2API_OFFICIAL_EGRESS_MEMORY_FIXTURE="$memoryRunRoot/fixtures/native-16.8.json" \
SUB2API_OFFICIAL_EGRESS_MEMORY_WIRE_FIXTURE="$memoryRunRoot/fixtures/native-16.8.zst" \
go test ./internal/service -run '^TestOfficialEgressMemoryPrepareFixture$' -count=1 -v

GOTOOLCHAIN=local \
SUB2API_OFFICIAL_EGRESS_MEMORY_PREPARE_WS_SESSION_FIXTURE=1 \
SUB2API_OFFICIAL_EGRESS_MEMORY_SHAPE=native \
SUB2API_OFFICIAL_EGRESS_MEMORY_FIXTURE="$memoryRunRoot/fixtures/native-16.8.json" \
SUB2API_OFFICIAL_EGRESS_MEMORY_REQUESTS_PER_SLOT=2 \
SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR="$memoryRunRoot/ws-fixtures/native-16.8" \
go test ./internal/service -run '^TestOfficialEgressWSMemoryPrepareFixture$' -count=1 -v
```

压缩 wire 文件须使用新路径，避免覆盖已有对照；已有固定 JSON 会被复用。WS 目录生成 `manifest.json` 与逐轮帧，测量前会核对长度及摘要。仓库归档的 `ws-fixture-manifests/` 只含历史摘要，不含帧正文；重新随机生成的样本不能当作相同原文对照。

## 2. 构建并在 ARM64 宿主运行独立容器

先按所选 cases 准备其全部样本，再将本报告资产中的 runner／cases 和构建的二进制放到测量宿主。以下目录仍以同一个 `memoryRunRoot` 为例；在远端执行时需重新设置为实际挂载目录。

```sh
GOTOOLCHAIN=local CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
go test -c -o "$memoryRunRoot/memory-final.test" ./internal/service

python3 ../docs/REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/run_container_matrix.py \
  --root "$memoryRunRoot" --binary memory-final.test --label recheck-final-1 \
  --cases ../docs/REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/container-final-cases.json
```

runner 每场启用 `SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE=1`，创建全新 768MiB、2 CPU、禁止外网的容器，使用只读挂载的正文和 WS manifest。默认 `GOGC=100`，实验 cases 可覆盖；WS 运行 `TestOfficialEgressWSSessionReadMemoryProfile`，HTTP 运行 `TestOfficialEgressHTTPForwardMemoryProfile`。结果写入 `<标签>-results.jsonl` 和 `logs-<标签>/`，结束后删除该场测试容器。结果 JSON 包含实际命令、容器退出状态及样本摘要；同一标签会追加结果，应为每轮选择新标签。

本地 WS 冒烟也可直接设置 `SUB2API_OFFICIAL_EGRESS_MEMORY_PROFILE=1`、`SUB2API_OFFICIAL_EGRESS_MEMORY_WS_SESSION_FIXTURE_DIR` 后运行上述 WS 测试名；它不产生 Linux cgroup 数据。不得在每次请求间强制 GC，也不得把 fixture 生成放入待验收容器后再宣称 `memory.peak` 只属于请求。
