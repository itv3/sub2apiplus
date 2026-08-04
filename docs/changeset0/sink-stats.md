<!-- 此文件由 backend/cmd/egressscan -mode stats 生成，请勿手工修改。 -->
# 发送面基线统计

> 数据源：`sink-baseline.json`；bootstrap commit：`38a9929eac35a39c86de2f27de8f7a805d7dae52`。

- 基线记录：180 条；
- 构建上下文：darwin/arm64、linux/amd64；包-上下文：212 个；类型检查兜底文件：0 个；
- 可静态确定 method：86 条；可确定完整 target/host/path：14 条；
- in-scope 候选：47 条；RuntimeSinkID：34 个。

## Persona

| persona | 候选数 |
|---|---:|
| `chatgpt-web` | 3 |
| `codex-cli` | 38 |
| `dead-code` | 2 |
| `infrastructure` | 11 |
| `out-of-scope` | 122 |
| `unclassified` | 4 |

## In-scope 迁移归属

| 变更集 | 候选数 |
|---|---:|
| `1B` | 21 |
| `2` | 2 |
| `3` | 24 |

## 端点证据状态

| endpoint_evidence | 候选数 |
|---|---:|
| `codex_profile` | 30 |
| `external_persona` | 3 |
| `missing` | 4 |
| `not_applicable` | 142 |
| `transport_only` | 1 |

## In-scope 责任人

| owner | 候选数 |
|---|---:|
| `czs` | 47 |

