# MigrationReceipt 运行时产物

此目录保存 `migration-receipts.json` 明确引用的 wire fixture、执行验证产物和 canary
验收产物。运行时通过 `go:embed` 读取实际文件并重算 SHA-256；未被收据引用的文件不构成
迁移证据。

生产清单当前为空。首份收据进入仓库时，必须同时提交对应产物及
`docs/changeset1a/migration-artifacts/` 下的审核副本。
