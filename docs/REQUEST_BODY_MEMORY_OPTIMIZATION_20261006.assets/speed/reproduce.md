# 请求体内存与耗时复测

最终版本共测 42 场，35 场大正文全部 ≤2.5 倍，最高 **2.4803 倍**；无 OOM，结束后匿名映射计数全部归零。完整原始结果见 `results.jsonl`，源码与二进制摘要见 `source-sha256.json`、`summary.json`。

同一 ARM64 宿主、同一固定样本、同一容器参数下，各形态独立重复三次。两并发槽，每槽连续四次 16.8MiB；表中为整场耗时中位数。

| 形态 | 旧纯内存方案 | 本轮方案 | 中位数变化 |
| --- | ---: | ---: | ---: |
| 原生 | 5.878 秒 | 5.944 秒 | +1.1% |
| 显式指令 | 6.163 秒 | 6.064 秒 | −1.6% |

两组性能对照的请求峰值最高均约 1.92 倍，cgroup 峰值分别不超过 90.3MiB、86.8MiB。耗时恢复至旧方案的测量区间；额度耗尽后的磁盘回退不承诺同速。小请求 WS 4MiB 两轮约 67.1MiB，仍是独立待优化项。

压缩输出采用 1MiB 系统内存块，每次完整转发最多占入口原文的 75%，进程压缩输出最多 64MiB，按完整容量计量。额度不足或映射不可用时转存普通文件；两者均不可用时返回服务端存储错误。每份定型正文只压缩一次，编译摘要在写出时计算，签名及 Guard 继续独立读取实际正文。

## 核对归档

在仓库根目录执行：

```sh
python3 docs/REQUEST_BODY_MEMORY_OPTIMIZATION_20261006.assets/speed/verify_results.py --repository "$PWD"
```

脚本核对 42 场结果、6 组新旧性能对照、54 份容器日志、48 项门禁、源码及归档摘要。后续源码有意变化时，去掉 `--repository` 只核对历史证据。旧磁盘版本证据保留在 `../final/`，未改写。

## 重跑容器

样本生成方式沿用 [原复现说明](../final/reproduce.md)。同一性能比较必须使用同一随机正文，摘要见 `fixture-manifests.json`。本次样本和旧纯内存 v8 二进制保存在实验宿主 `ARM64` 的 `/tmp/sub2api-request-memory-20261006.Vd9iuP`；目录若被清理，不能把新生成正文或其他二进制冒充本次历史对照。

在 `backend` 目录使用 Go 1.27.1 构建，再把二进制、本目录 runner 和 cases 放入实验目录：

```sh
GOTOOLCHAIN=go1.27.1 CGO_ENABLED=0 GOOS=linux GOARCH=arm64 \
go test -c -o /tmp/memory-speed-recheck.test ./internal/service
```

在实验宿主执行，每次使用新标签；两条命令串行运行：

```sh
memoryRunRoot=/tmp/sub2api-request-memory-20261006.Vd9iuP
python3 "$memoryRunRoot/run_container_matrix.py" --root "$memoryRunRoot" \
  --binary memory-speed-recheck.test --label speed-recheck-matrix --cases "$memoryRunRoot/cases.json"
python3 "$memoryRunRoot/run_container_matrix.py" --root "$memoryRunRoot" \
  --binary memory-speed-recheck.test --label speed-recheck-performance --cases "$memoryRunRoot/performance-cases.json"
```

保持容器 768MiB、2 CPU、GOMEMLIMIT=512MiB、GOGC=100、GOMAXPROCS=2。按同一时刻的 Go 堆加匿名映射计峰值；WS 分母为各连接最大单帧之和。cgroup 单独记录，包含页缓存；不使用 tmpfs，不在请求内强制 GC。测试桩按流丢弃上游正文，不代表公网延迟或长期生产混合负载。

`validation.json` 记录回归范围和执行顺序。最终 48 项门禁、定向 race、服务冻结及业务 vet 均通过；首次门禁的依赖边界失败与工具测试权限错误原日志另行保留，不改记为通过。尚未提交、发布或调整线上容量参数。
