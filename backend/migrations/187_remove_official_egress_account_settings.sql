-- OAuth 官方出站画像已经改为按平台与认证类型自动生效，不再使用账号级开关
-- 或账号级画像版本。清理历史 Extra 键，避免旧管理端数据造成错误认知。
UPDATE accounts
SET extra = COALESCE(extra, '{}'::jsonb)
    - 'official_egress_enabled'
    - 'official_egress_profile_version',
    updated_at = NOW()
WHERE COALESCE(extra, '{}'::jsonb) ? 'official_egress_enabled'
   OR COALESCE(extra, '{}'::jsonb) ? 'official_egress_profile_version';
