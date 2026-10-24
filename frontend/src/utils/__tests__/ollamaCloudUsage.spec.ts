import { describe, expect, it } from 'vitest'
import type { Account, OllamaCloudUsageData, OllamaCloudUsageState } from '@/types'
import {
  formatOllamaBalance,
  isOllamaFreePlan,
  ollamaCookieExpiryDate,
  ollamaCookieExpiryInfo,
  ollamaQuotaResetNote,
  ollamaUsageData
} from '../ollamaCloudUsage'

const NOW = new Date('2026-10-24T12:00:00Z').getTime()
const DAY = 24 * 60 * 60 * 1000

const accountWith = (data?: OllamaCloudUsageData, eligible = true, fetchedAt = '2026-10-24T11:00:00Z'): Pick<Account, 'ollama_cloud_usage'> => ({
  ollama_cloud_usage: {
    account_id: 7,
    eligible,
    configured: true,
    auto_refresh_enabled: false,
    encryption_key_configured: true,
    snapshot: data
      ? { status: 'ok', data, fetched_at: fetchedAt, last_attempt_at: fetchedAt, next_refresh_at: fetchedAt }
      : undefined
  } as OllamaCloudUsageState
})

describe('ollamaCookieExpiryInfo', () => {
  it('is unset when missing or malformed', () => {
    expect(ollamaCookieExpiryInfo(undefined, NOW).kind).toBe('unset')
    expect(ollamaCookieExpiryInfo('', NOW).kind).toBe('unset')
    expect(ollamaCookieExpiryInfo('not-a-date', NOW).kind).toBe('unset')
  })

  it('warns within three days and counts partial days as a full day', () => {
    expect(ollamaCookieExpiryInfo(new Date(NOW + 3 * DAY).toISOString(), NOW)).toEqual({ kind: 'soon', days: 3 })
    expect(ollamaCookieExpiryInfo(new Date(NOW + 2 * DAY - 1000).toISOString(), NOW)).toEqual({ kind: 'soon', days: 2 })
    expect(ollamaCookieExpiryInfo(new Date(NOW + 1000).toISOString(), NOW)).toEqual({ kind: 'soon', days: 1 })
  })

  it('stays quiet beyond three days and flags expired after the moment passes', () => {
    expect(ollamaCookieExpiryInfo(new Date(NOW + 3 * DAY + 1000).toISOString(), NOW).kind).toBe('ok')
    expect(ollamaCookieExpiryInfo(new Date(NOW - 1000).toISOString(), NOW)).toEqual({ kind: 'expired', days: 0 })
    expect(ollamaCookieExpiryInfo(new Date(NOW - 5 * DAY).toISOString(), NOW)).toEqual({ kind: 'expired', days: 5 })
  })
})

describe('ollama display helpers', () => {
  it('formats the local expiry date as YYYY-MM-DD', () => {
    const local = new Date(2026, 10, 30, 9, 5)
    expect(ollamaCookieExpiryDate(local.toISOString())).toBe('2026-11-30')
    expect(ollamaCookieExpiryDate(undefined)).toBe('')
    expect(ollamaCookieExpiryDate('garbage')).toBe('')
  })

  it('keeps balances verbatim but spaces a letter currency code', () => {
    expect(formatOllamaBalance('$60')).toBe('$60')
    expect(formatOllamaBalance('<$0.01')).toBe('<$0.01')
    expect(formatOllamaBalance('USD0')).toBe('USD 0')
    expect(formatOllamaBalance('USD$9.50')).toBe('USD $9.50')
    expect(formatOllamaBalance('')).toBe('')
    expect(formatOllamaBalance(undefined)).toBe('')
  })

  it('recognises the free plan case-insensitively', () => {
    expect(isOllamaFreePlan('Free')).toBe(true)
    expect(isOllamaFreePlan(' free ')).toBe(true)
    expect(isOllamaFreePlan('Pro')).toBe(false)
    expect(isOllamaFreePlan(undefined)).toBe(false)
  })

  it('exposes snapshot data only for eligible Ollama accounts', () => {
    expect(ollamaUsageData(accountWith({ plan: 'Pro' }))?.plan).toBe('Pro')
    expect(ollamaUsageData(accountWith({ plan: 'Pro' }, false))).toBeUndefined()
    expect(ollamaUsageData({ ollama_cloud_usage: undefined })).toBeUndefined()
    expect(ollamaUsageData(null)).toBeUndefined()
  })
})

describe('ollamaQuotaResetNote', () => {
  const monthly = { used_percent: 10, reset_at: '2026-11-24T00:00:00Z', reset_text: 'in 4 weeks' }

  it('shows an exact reset date for paid plans', () => {
    const note = ollamaQuotaResetNote(accountWith({ plan: 'Pro', monthly }))
    expect(note).not.toBeNull()
    expect(note?.approx).toBe(false)
    expect(note?.text).toBe('in 4 weeks')
    expect(note?.fetchedAt).toBe('2026-10-24T11:00:00Z')
    expect(note?.date).toMatch(/^2026-11-2[345]$/)
  })

  it('marks approximate dates', () => {
    const note = ollamaQuotaResetNote(accountWith({ plan: 'Pro', monthly: { ...monthly, reset_approx: true } }))
    expect(note?.approx).toBe(true)
  })

  it('stays hidden for free plans, legacy windows, missing data and non-Ollama accounts', () => {
    expect(ollamaQuotaResetNote(accountWith({ plan: 'Free', monthly }))).toBeNull()
    expect(ollamaQuotaResetNote(accountWith({ plan: 'max', five_hour: { used_percent: 1, reset_at: '2026-11-24T00:00:00Z' } }))).toBeNull()
    expect(ollamaQuotaResetNote(accountWith({ plan: 'Pro', monthly: { used_percent: 1 } }))).toBeNull()
    expect(ollamaQuotaResetNote(accountWith({ plan: 'Pro', monthly }, false))).toBeNull()
    expect(ollamaQuotaResetNote(accountWith(undefined))).toBeNull()
  })
})
