/** OpenAI subscription SKU identity and Codex display names. Other providers use their own labels. */
export function normalizePlanType(value?: string | null): string {
  return (value || '').trim().toLowerCase().replace(/[\s_-]+/g, '')
}

/** Canonical comparison key; labels may be shared by distinct SKUs. */
export function openAIPlanTypeKey(value?: string | null): string {
  const key = normalizePlanType(value)
  return key === 'chatgptpro' ? 'pro' : key
}

// Wire names from openai/codex b1e72963, protocol/src/account.rs.
// sci、sci_plus 是 Codex 0.164.0 新增的学术研究者套餐（rust-v0.164.0 的 protocol/src/auth.rs，
// wire 名 "sci"、"sci_plus"；/wham/usage 响应里的 plan_type 也可能出现）。
export const openAIPlanTypes = [
  'free', 'go', 'plus', 'prolite', 'pro', 'promax', 'team',
  'self_serve_business_usage_based', 'self_serve_business_prolite', 'business',
  'enterprise', 'ent26', 'enterprise_cbp_usage_based', 'enterprise_cbp_automation',
  'edu', 'edu_plus', 'edu_pro', 'sci', 'sci_plus', 'unknown'
] as const

/** Keep Status SKU names distinct from the account families used in analytics. */
export function openAIPlanTypeLabel(value?: string | null, display: 'status' | 'analytics' = 'status'): string {
  switch (openAIPlanTypeKey(value)) {
    case 'free': return 'Free'
    case 'go': return 'Go'
    case 'plus': return 'Plus'
    case 'prolite': return 'Pro 100'
    case 'pro': return 'Pro 200'
    case 'promax': return 'Pro 500'
    case 'team':
    case 'selfservebusinessusagebased': return 'Business'
    case 'business': return display === 'status' ? 'Enterprise' : 'Business'
    case 'selfservebusinessprolite': return display === 'status' ? 'Business Premium' : 'Business'
    case 'enterprisecbpautomation': return display === 'status' ? 'Enterprise (Automation)' : 'Enterprise'
    case 'enterprise':
    case 'ent26':
    case 'enterprisecbpusagebased': return 'Enterprise'
    case 'edu': return display === 'status' ? 'Edu' : 'Education'
    case 'eduplus': return display === 'status' ? 'Edu Plus' : 'Education'
    case 'edupro': return display === 'status' ? 'Edu Pro' : 'Education'
    // 状态展示用官方名；分析口径按官方 tui/src/analytics/models.rs 归入 Business 族（不是 Enterprise）。
    case 'sci': return display === 'status' ? 'Academic Researchers' : 'Business'
    case 'sciplus': return display === 'status' ? 'Academic Researchers Plus' : 'Business'
    case 'unknown': return display === 'status' ? 'Unknown' : 'Account'
    default: return ''
  }
}
