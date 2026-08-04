# 变更集 3 身份权威重构后 final-wire 证据

本目录冻结身份权威重构完成后的确定性本地 wire 观察结果。范围为 21 个 Runtime Sink、28 条 route，并同时覆盖 `active` 与 `previous`，合计 56 份 route-mode capture。

捕获从真实生产 semantic 构造/转换函数进入 production invocation 或 Forward plan，再通过正式 compiler、Executor、HTTPUpstream/req-profile/WebSocket adapter 和归一化后的本地 terminal Guard；不发送外部流量，只使用合成账号、凭据、动态 ID 与文件内容。WebSocket 证据包含 coder 握手自动头、压缩提议和由 Executor 单次能力实际消费的出站事件矩阵。

`manifest.json` 同时绑定身份权威重构前双定型参考、4 个既有 1B/1C 锚点 fixture、相关生产源码与捕获工具摘要。pre→post 只证明参考中已记录的 adapter-terminal 字段；不声称完整历史生产业务链等价。所有允许变化均在 `approved-deltas.json` 中逐 capture、逐字段路径绑定前后摘要。`secret-scan.json` 证明资产中没有保存认证值。

冻结摘要：

```text
manifest.json:
c824ffb0ab6e2429c09f9ac517cf3e6f96860c7c6ef77c229757fd690bdbcf0f

secret-scan.json:
94e400de321b64784203041ac6320186b6b8757723205681306333318f372050

approved-deltas.json:
6e7e13a94d90431a1c49bf22b72efb746e8ac63b446a3e7176b63bc8c13f1ce0
```
