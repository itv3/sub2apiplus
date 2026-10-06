# 最终 512KiB 窗口独立解码证据

四个最终帧均通过独立 Zstandard CLI 1.5.7 解码；单帧、512KiB 窗口、准确 FCS、DictID=0、内容校验和与原文 SHA-256 全部通过。生产相关源码在导出前后未变化。

校验和沿用 zstd 内容校验和（XXH64 低 32 位，并非 CRC）；独立解码成功同时验证内容校验和。

固定 JSON 沿用原 2MiB／8MiB 窗口对照，导出调用当前生产 compressRequestBodyZstd，等级 3、单槽弱引用缓存。此证据针对压缩阶段，不替代正式 JSON 定型、完整转发或峰值内存验收。本次没有运行性能基准。

| 形态 | 原文尺寸 | 原文字节 | 512KiB wire 字节 | 对 2MiB 变化 | 对 8MiB 变化 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 原生 | 16.8MiB | 17,622,304 | 11,272,381 | +18,752 B（+0.16663%） | +29,065 B（+0.25851%） |
| 原生 | 50.0MiB | 52,441,843 | 33,549,014 | +62,741 B（+0.18736%） | +73,981 B（+0.22100%） |
| 显式 instructions | 16.8MiB | 17,622,340 | 11,272,429 | +18,744 B（+0.16656%） | +29,056 B（+0.25843%） |
| 显式 instructions | 50.0MiB | 52,441,879 | 33,549,070 | +62,750 B（+0.18739%） | +73,989 B（+0.22103%） |

所有百分比都以对应旧窗口的压缩结果大小为分母；原文及旧帧大小均与既存 independent-decoder-results.json 逐项核对。

复现文件：final512-export_test.go、final512-overlay.json、verify-final512.py、final512-commands.txt。Go overlay 只添加临时测试视图，没有写入仓库。

完整记录：independent-decoder-final512-results.json；导出日志：final512-export.log；各帧独立列表：*.frame.txt；源码摘要：final512-source-sha256.json。

运行环境：go version go1.27.1 darwin/arm64；*** Zstandard CLI (64-bit) v1.5.7, by Yann Collet ***。

仓库提交：23a8502193089ae024d6c819ef455e31b1a5a34a（包含本次已存在的工作区改动；以源码摘要限定导出版本）。
