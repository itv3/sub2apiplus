# 入口拒绝日志清理

该维护命令从 `ops_error_logs` 删除历史入口拒绝记录，不会匹配无关的认证错误或上游错误。除非传入 `--execute`，否则只执行预览；命令始终要求提供明确的 RFC3339 截止时间。

```sh
go run ./cmd/cleanup-ingress-reject-logs --before 2026-07-17T00:00:00Z
go run ./cmd/cleanup-ingress-reject-logs --before 2026-07-17T00:00:00Z --execute
```

只有在所有应用实例均完成升级、旧实例不会再写入早于截止时间的入口拒绝记录后，才能执行实际删除。分类器会保留 `USER_NOT_FOUND`、数据库错误、额度或计费错误以及上游失败等不应清理的记录。

确认发布和清理结果后，在维护窗口执行 `backend/scripts/finalize-ingress-reject-cleanup.sql`，删除已弃用的明文 Key 审计表和归属字段。
