-- 记录长上下文计费是否改变了请求的 token 价格，使历史用量无需根据总额推断即可解释实际费用。
ALTER TABLE usage_logs
    ADD COLUMN IF NOT EXISTS long_context_billing_applied BOOLEAN NOT NULL DEFAULT FALSE;
