# SPEC-TLS-003 判据缺陷实证（classify 阶段 `change` 依据）

> 日期：2026-08-10。证据来源：Campaign `codex-0145-to-0147-20260809T170000Z-k35`
> 官方 attempt `20260809T154236Z-f644a82d2cb3b00b`（17/17 complete）。
> 本文只记录实证与结论，不改动任何冻结画像；判据修订按 §6.4 在 classify 阶段执行。

## 1. 现行判据

```json
{
  "id": "extension-order-diversity",
  "select": {
    "record_type": "tls_client_hello",
    "where": [{"path": "labels.transport", "operator": "equal", "value": "websocket"}]
  },
  "assertion": {
    "operator": "same_set_distinct_order",
    "path": "data.extension_types",
    "minimum_records": 4,
    "minimum_artifacts": 4,
    "minimum_distinct_orders": 2
  }
}
```

`same_set_distinct_order` 要求**全部**选中记录的 `extension_types` 集合逐一相等
（`all(item == sets[0] for item in sets)`），且来自 ≥4 个 artifact、≥2 种顺序。

## 2. 实证：判据在真实证据上不可执行

k35 官方 `direct` 抓包的 ClientHello 实测分布（`pcap_clienthello.py` 解析）：

| 子目录 | cipher 数 | 扩展数 | ALPN | 条数 |
|---|---|---|---|---|
| `codex-http` | 30 | 11 | 不 offer | 17 |
| `codex-ws` | 30 | 11 | 不 offer | 15 |
| `codex-ws` | 10 | 10 | 不 offer | 5 |

按子目录细分，每份 `codex-ws` pcap 都同时含两种栈：

| pcap | 已知固定序（native-tls） | 非固定序（rustls） |
|---|---|---|
| `s1/traffic.pcap` | 6 | 2 |
| `s2/traffic.pcap` | 6 | 2 |
| `s4/traffic.pcap` | 3 | 1 |

**根因**：Codex CLI 每次启动都会拉 `/backend-api/codex/models`，那是 native-tls 的
HTTP 握手，必然与随后的 rustls WS 握手落进同一份 pcap。现行判据依赖 artifact 级
`labels.transport=websocket`，隐含假设「WS 场景的 pcap 只含 WS 握手」——该假设不成立。

两种栈的扩展集合不同（native-tls 含 `65281`、`22`；rustls 含 `5`），因此
`sets_equal` 必然为假，`same_set_distinct_order` 恒失败。补采更多同构 pcap 不能解决。

## 3. 为什么不能靠标签分流绕开

曾考虑让 pcap 解析器按栈形态派生 record 级 `transport` 标签。**该方案不成立**：

可用于分流的信号只有 cipher 数、扩展集合与 ALPN，而这三者恰好是共用
`labels.transport` 的另外两条规则正在验证的属性——

- `SPEC-TLS-001 / cipher-count`：`all_equal(data.cipher_suite_count, 30)`；
- `SPEC-TLS-001 / alpn-absent`、`SPEC-PROTO-001 / no-alpn`：验 ALPN 缺席；
- `SPEC-TLS-003` 本身：验 `data.extension_types`。

用 cipher 数分流会使 `cipher-count` 退化为「先按 cipher=30 筛，再断言 cipher=30」的
同义反复。`direct` 抓包为加密流量，SNI 与目标端口两种栈完全相同，不存在第四个独立信号。
采集侧亦无法分离：抓包窗口覆盖整个 case，`capture.py` 无预置模型清单选项。

## 4. 建议的修订（classify 阶段标记 `change`）

保持 `select` 不变，把断言算子改为**存在性子集**语义：在选中记录中，存在一个
大小 ≥ `minimum_records`、来自 ≥ `minimum_artifacts` 个 artifact、扩展集合相同、
顺序种类 ≥ `minimum_distinct_orders` 的子集。

该修订**不降低判据强度**：仍要求 4 个独立 artifact 上出现集合一致、顺序各异的握手；
混入的 native-tls 记录既不能使判据通过，也不再使其恒假。

配套增量（已实施，见 `codex_upgrade_scenarios_0_145_0.json`）：新增
`official-ws-handshake-repeat` 与 `candidate-ws-handshake-repeat` 两个 job，用独立
batch-id 与独立证据根重跑 `codex-ws direct`，把 A02 的 artifact 数由 3 提升到 6，
满足 `minimum_artifacts=4`。

## 5. 该缺陷此前未被发现的原因

历史 25 个 Campaign 无任何一个产出过 `assertions/<candidate-id>/results.json`
（见升级计划 §10.8.12），`SPEC-TLS-003` 自 0.145 规格写出后从未真正执行过，
判据与真实采集能力的错配因此一直隐藏。
