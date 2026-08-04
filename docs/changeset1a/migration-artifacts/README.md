# MigrationReceipt 审核产物

此目录是生产嵌入迁移产物的审核副本。每条 route 必须分别提供：

- 最终 wire fixture；
- 与 Sink、route、persona authority、TokenIssuer、Adapter、Transport 关联的执行验证 JSON；
- 从 canary 升到 enforced 时使用的 canary acceptance JSON。

CI 会读取实际文件、重算 SHA-256 并校验验证 JSON 与收据声明逐字段一致。仅填写合法摘要
但不提交对应文件无法升级 Catalog。
