import type { Account, OllamaCloudUsageData, OllamaCloudUsageState } from '@/types'

const DAY_MS = 24 * 60 * 60 * 1000

/** Cookie 到期提醒的提前天数：不足这个天数时在列表里提醒。 */
export const OLLAMA_COOKIE_WARN_DAYS = 3

export type OllamaCookieExpiryKind = 'unset' | 'ok' | 'soon' | 'expired'

export interface OllamaCookieExpiryInfo {
  kind: OllamaCookieExpiryKind
  /** ok/soon：向上取整的剩余天数（不含已过期）；expired：已过期的整天数 */
  days: number
}

/**
 * 根据管理员填写的 Cookie 到期时间计算提醒状态。
 * - 未填写或格式不对：unset
 * - 已过到期时间：expired（days 为已过期整天数，当天过期为 0）
 * - 剩余不足 3 天：soon（days 向上取整，最小为 1；不足一天也算 1 天）
 * - 其余：ok
 */
export function ollamaCookieExpiryInfo(expiresAt?: string | null, now: number = Date.now()): OllamaCookieExpiryInfo {
  if (!expiresAt) return { kind: 'unset', days: 0 }
  const time = new Date(expiresAt).getTime()
  if (Number.isNaN(time)) return { kind: 'unset', days: 0 }
  const diff = time - now
  if (diff <= 0) return { kind: 'expired', days: Math.floor(-diff / DAY_MS) }
  const days = Math.max(1, Math.ceil(diff / DAY_MS))
  return { kind: days <= OLLAMA_COOKIE_WARN_DAYS ? 'soon' : 'ok', days }
}

/** 把到期时间格式化成本地时区的 YYYY-MM-DD；缺失或格式不对返回空串。 */
export function ollamaCookieExpiryDate(expiresAt?: string | null): string {
  if (!expiresAt) return ''
  const date = new Date(expiresAt)
  if (Number.isNaN(date.getTime())) return ''
  const mm = String(date.getMonth() + 1).padStart(2, '0')
  const dd = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${mm}-${dd}`
}

/** 快照读到的套餐是否为免费计划（不区分大小写）。 */
export function isOllamaFreePlan(plan?: string | null): boolean {
  return (plan || '').trim().toLowerCase() === 'free'
}

/**
 * 余额原文照原样显示，只把「USD0」这类字母币种紧贴数字的写法补成「USD 0」，
 * 与其他供应商余额的「币种 数值」样式对齐；「$60」「<$0.01」保持不变。
 */
export function formatOllamaBalance(raw?: string | null): string {
  const value = (raw || '').trim()
  if (!value) return ''
  return value.replace(/^([A-Za-z]{3})(?=[<≈~$\d-])/, '$1 ')
}

/** 取账号的 Ollama 用量快照数据，非 Ollama 账号或没有快照返回 undefined。 */
export function ollamaUsageData(account: Pick<Account, 'ollama_cloud_usage'> | null | undefined): OllamaCloudUsageData | undefined {
  const state: OllamaCloudUsageState | undefined = account?.ollama_cloud_usage
  if (!state?.eligible) return undefined
  return state.snapshot?.data
}

/**
 * 平台/类型列「额度重置」一行：只对付费计划的月度额度窗口显示（免费计划与解析不到时不显示）。
 * 返回精确日期（本地时区的 YYYY-MM-DD）、是否为约数、页面原文与抓取时间，供悬停提示使用。
 */
export function ollamaQuotaResetNote(
  account: Pick<Account, 'ollama_cloud_usage'> | null | undefined
): { date: string; approx: boolean; text: string; fetchedAt: string } | null {
  const state = account?.ollama_cloud_usage
  if (!state?.eligible) return null
  const data = state.snapshot?.data
  const monthly = data?.monthly
  if (!data || !monthly?.reset_at || isOllamaFreePlan(data.plan)) return null
  const reset = new Date(monthly.reset_at)
  if (Number.isNaN(reset.getTime())) return null
  const yyyy = reset.getFullYear()
  const mm = String(reset.getMonth() + 1).padStart(2, '0')
  const dd = String(reset.getDate()).padStart(2, '0')
  return {
    date: `${yyyy}-${mm}-${dd}`,
    approx: monthly.reset_approx === true,
    text: monthly.reset_text || '',
    fetchedAt: state.snapshot?.fetched_at || state.snapshot?.last_attempt_at || ''
  }
}
