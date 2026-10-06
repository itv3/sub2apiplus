# 小请求原生 WS 复测

发送副本、单轮正文索引、逐项历史比较及跨轮保留优化后，同一 ARM64 宿主、固定样本、新旧交替各三次的中位数如下。旧版为上一轮 HTTP 耗时优化最终版，并非上游原始代码。

| 4MiB 会话 | 旧峰值 → 新峰值 | 旧耗时 → 新耗时 |
| --- | --- | --- |
| 两轮 | 67.6 → 47.4MiB（−29.8%） | 1496 → 1324ms（−11.5%） |
| 十轮 | 76.0 → 48.7MiB（−35.9%） | 7145 → 6295ms（−11.9%） |

两轮／十轮累计分配中位数由 390.3／1896.6MiB 降至 125.9／591.3MiB。小请求原生 WS 仍未达到 2.5 倍；16.8MiB 两形态桥接复测均约 2.47 倍。共保留 38 场原始结果：19 场新矩阵、7 场旧版基线、12 场交替对照，全部无 OOM、结束后系统内存映射归零。

测量含本地客户端与假上游的 WS 开销，不含公网、TLS、完整鉴权调度或生产混合负载。峰值为同一时刻的 Go HeapInuse 增量加匿名映射；分母为各连接最大单帧之和，不累计轮数。cgroup 另行记录。新矩阵覆盖 0.3／1／4／8MiB、1／2／10 轮、两连接并发及显式指令；真实传输类型逐场记录。

## 核对证据

在仓库根目录执行；后续源码有意变化时，省略 `--repository` 可仅核对历史归档。

```sh
python3 docs/REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/native-ws/verify_results.py --repository "$PWD"
```

`validation.json` 与压缩原始日志保留完整后端回归、差分与所有权 race、最终 48 项门禁，以及修复前的失败记录。一项大帧桥接 race 在并行重负载下超时，未改超时，单独重跑通过。旧证据 `../final/`、`../speed/` 保持原样。

## 重跑测量

固定样本和两版二进制位于实验宿主 `ARM64` 的 `/tmp/sub2api-request-memory-20261006.Vd9iuP`，摘要见 `fixture-manifests.json`、`runtime.json`。目录若被清理，新生成样本不能冒充历史对照。样本生成方法沿用 [原复现说明](../final/reproduce.md)，本轮小请求会话预生成 10 轮文件，各场按用例取前 1／2／10 轮。

在 `backend` 目录构建，并把产物、本目录 runner 和三份 cases 文件复制到实验目录：

```sh
GOTOOLCHAIN=go1.27.1 CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
go test -c -o /tmp/native-ws-recheck.test ./internal/service
```

实验宿主上串行执行；每轮使用新标签，避免结果追加到旧记录。容器固定 768MiB、2 CPU、GOMEMLIMIT=512MiB、GOGC=100、GOMAXPROCS=2、无外网。

```sh
wsRunRoot=/tmp/sub2api-request-memory-20261006.Vd9iuP
python3 "$wsRunRoot/run_container_matrix.py" --root "$wsRunRoot" \
  --binary native-ws-recheck.test --label ws-recheck-matrix --cases "$wsRunRoot/cases.json"
for wsRound in 1 2 3; do
  python3 "$wsRunRoot/run_container_matrix.py" --root "$wsRunRoot" \
    --binary service-speed-boundary-linux-arm64.test --label "ws-recheck-old-$wsRound" --cases "$wsRunRoot/performance-cases.json"
  python3 "$wsRunRoot/run_container_matrix.py" --root "$wsRunRoot" \
    --binary native-ws-recheck.test --label "ws-recheck-new-$wsRound" --cases "$wsRunRoot/performance-cases.json"
done
```
