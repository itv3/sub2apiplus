-- 记录每条已索引系统日志的来源应用主机。
ALTER TABLE ops_system_logs
  ADD COLUMN IF NOT EXISTS host VARCHAR(255);
