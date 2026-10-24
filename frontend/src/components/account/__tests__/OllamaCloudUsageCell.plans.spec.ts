import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OllamaCloudUsageCell from '../OllamaCloudUsageCell.vue'
import UsageProgressBar from '../UsageProgressBar.vue'
import type { Account, OllamaCloudUsageData, OllamaCloudUsageState } from '@/types'

vi.mock('@/api/admin', () => ({ adminAPI: { accounts: { refreshOllamaCloudUsage: vi.fn() } } }))

// t 返回「键」或「键|参数」，方便断言插值内容。
vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string, params?: Record<string, unknown>) => (params ? `${key}|${JSON.stringify(params)}` : key)
    })
  }
})

const FIXED_NOW = new Date('2026-10-24T12:00:00Z')

const stateWith = (
  data: OllamaCloudUsageData | undefined,
  overrides: Partial<OllamaCloudUsageState> = {},
  status: 'ok' | 'unauthorized' | 'failed' = 'ok'
): OllamaCloudUsageState => ({
  account_id: 7,
  eligible: true,
  configured: true,
  auto_refresh_enabled: false,
  encryption_key_configured: true,
  snapshot: data
    ? {
        status,
        data,
        fetched_at: '2026-10-24T11:00:00Z',
        last_attempt_at: '2026-10-24T11:00:00Z',
        next_refresh_at: '2026-10-24T12:00:00Z'
      }
    : undefined,
  ...overrides
})

const account = (state: OllamaCloudUsageState): Account =>
  ({ id: 7, name: 'ollama', platform: 'openai', type: 'apikey', ollama_cloud_usage: state }) as unknown as Account

const mountCell = (state: OllamaCloudUsageState) => mount(OllamaCloudUsageCell, { props: { account: account(state) } })
const barLabels = (wrapper: ReturnType<typeof mountCell>) => wrapper.findAllComponents(UsageProgressBar).map(bar => bar.props('label'))

describe('OllamaCloudUsageCell plans', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.setSystemTime(FIXED_NOW)
  })
  afterEach(() => vi.useRealTimers())

  it('free plan shows the free-usage bar, plan chip and balance, without 5h/7d/monthly', () => {
    const wrapper = mountCell(stateWith({
      plan: 'Free',
      balance: '$0',
      free_usage: { used_percent: 0, reset_at: '2026-11-14T05:33:24Z' }
    }))

    expect(barLabels(wrapper)).toEqual(['admin.accounts.ollamaCloud.freeUsageShort'])
    expect(wrapper.get('[data-testid="ollama-cloud-free-usage"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="ollama-cloud-five-hour"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="ollama-cloud-seven-day"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="ollama-cloud-monthly"]').exists()).toBe(false)
    const chip = wrapper.get('[data-testid="ollama-cloud-plan"]')
    expect(chip.text()).toBe('Free')
    expect(chip.classes()).toContain('bg-gray-100')
    expect(wrapper.get('[data-testid="ollama-cloud-balance"]').text()).toBe('$0')
  })

  it('paid plan on the new page shows the monthly bar with plan and balance', () => {
    const wrapper = mountCell(stateWith({
      plan: 'Pro',
      balance: '$60',
      monthly: { used_percent: 20, used_text: '$12', reset_at: '2026-11-24T00:00:00Z' }
    }))

    const bars = wrapper.findAllComponents(UsageProgressBar)
    expect(bars).toHaveLength(1)
    expect(bars[0].props()).toMatchObject({
      label: 'admin.accounts.ollamaCloud.monthlyShort',
      utilization: 20,
      resetsAt: '2026-11-24T00:00:00Z'
    })
    expect(wrapper.find('[data-testid="ollama-cloud-five-hour"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="ollama-cloud-plan"]').classes()).toContain('bg-violet-100')
    expect(wrapper.get('[data-testid="ollama-cloud-balance"]').text()).toBe('$60')
  })

  it('draws monthly and free usage with the shared progress bar and a tooltip with used amount and reset time', () => {
    const monthly = mountCell(stateWith({
      plan: 'Pro',
      monthly: { used_percent: 20, used_text: '$12', reset_at: '2026-11-24T00:00:00Z', reset_approx: true }
    }))
    const monthlyBar = monthly.getComponent('[data-testid="ollama-cloud-monthly"]') as unknown as ReturnType<typeof monthly.getComponent>
    expect(monthly.findAllComponents(UsageProgressBar)).toHaveLength(1)
    expect(monthlyBar.props('label')).toBe('admin.accounts.ollamaCloud.monthlyShort')
    // 百分比来自页面进度条；英文 Monthly 较长，用 auto 宽度
    expect(monthlyBar.props('utilization')).toBe(20)
    expect(monthlyBar.props('labelWidth')).toBe('auto')
    const title = monthlyBar.attributes('title') || ''
    expect(title).toContain('admin.accounts.ollamaCloud.monthly')
    expect(title).toContain('admin.accounts.ollamaCloud.windowTooltipUsed|{"used":"$12"}')
    expect(title).toContain('admin.accounts.ollamaCloud.windowTooltipReset')
    expect(title).toContain('admin.accounts.ollamaCloud.resetApprox') // 约数标注

    const free = mountCell(stateWith({ plan: 'Free', free_usage: { used_percent: 37, reset_at: '2026-11-14T05:33:24Z' } }))
    const freeBar = free.getComponent('[data-testid="ollama-cloud-free-usage"]') as unknown as ReturnType<typeof free.getComponent>
    expect(freeBar.props('utilization')).toBe(37)
    expect(freeBar.attributes('title')).toContain('admin.accounts.ollamaCloud.windowTooltipReset')
    expect(freeBar.attributes('title')).not.toContain('resetApprox')
  })

  it('legacy page keeps the 5h/7d bars and never shows monthly or free usage', () => {
    const wrapper = mountCell(stateWith({
      plan: 'max',
      balance: '$0',
      five_hour: { used_percent: 5.6, reset_at: '2026-07-23T03:00:00Z' },
      seven_day: { used_percent: 14.2, reset_at: '2026-07-29T00:00:00Z' }
    }))

    expect(barLabels(wrapper)).toEqual(['5h', '7d'])
    expect(wrapper.find('[data-testid="ollama-cloud-monthly"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="ollama-cloud-free-usage"]').exists()).toBe(false)
  })

  it('shows every window the page provides when legacy and monthly coexist', () => {
    const wrapper = mountCell(stateWith({
      plan: 'Pro',
      five_hour: { used_percent: 1 },
      seven_day: { used_percent: 2 },
      monthly: { used_percent: 3 }
    }))

    expect(barLabels(wrapper)).toEqual(['admin.accounts.ollamaCloud.monthlyShort', '5h', '7d'])
  })

  it('renders the usual placeholder for accounts that are not Ollama eligible', () => {
    const wrapper = mountCell(stateWith({ plan: 'Pro' }, { eligible: false }))
    expect(wrapper.find('[data-testid="ollama-cloud-usage-cell"]').exists()).toBe(false)
    expect(wrapper.text()).toBe('-')
  })

  describe('cookie status line', () => {
    const notice = (wrapper: ReturnType<typeof mountCell>) => wrapper.find('[data-testid="ollama-cloud-cookie-status"]')

    it('shows the expiry date in YYYY-MM-DD and stays gray when far away', () => {
      const expires = new Date(2026, 10, 30, 12, 0, 0)
      const wrapper = mountCell(stateWith({ plan: 'Pro' }, { session_expires_at: expires.toISOString() }))
      const line = notice(wrapper)
      expect(line.text()).toBe('admin.accounts.ollamaCloud.cookieExpiresOn|{"date":"2026-11-30"}')
      expect(line.classes()).toContain('text-gray-500')
      // 不再显示「剩 N 天」
      expect(wrapper.text()).not.toContain('cookieExpiringShort')
    })

    it('turns amber within three days and red once the date has passed', () => {
      const soon = mountCell(stateWith({ plan: 'Pro' }, { session_expires_at: new Date(FIXED_NOW.getTime() + 2 * 86400000).toISOString() }))
      expect(notice(soon).classes()).toContain('text-amber-600')
      const past = mountCell(stateWith({ plan: 'Pro' }, { session_expires_at: new Date(FIXED_NOW.getTime() - 86400000).toISOString() }))
      expect(notice(past).classes()).toContain('text-red-600')
      expect(notice(past).text()).toContain('cookieExpiresOn')
    })

    it('an unauthorized snapshot shows the red expired notice and wins over the date', () => {
      const wrapper = mountCell(stateWith(
        { plan: 'Pro' },
        { session_expires_at: new Date(FIXED_NOW.getTime() + 30 * 86400000).toISOString() },
        'unauthorized'
      ))
      const line = notice(wrapper)
      expect(line.text()).toBe('admin.accounts.ollamaCloud.cookieExpiredShort')
      expect(line.classes()).toContain('text-red-600')
    })

    it('shows nothing without a configured session or without any expiry information', () => {
      expect(notice(mountCell(stateWith({ plan: 'Pro' }))).exists()).toBe(false)
      expect(notice(mountCell(stateWith({ plan: 'Pro' }, { configured: false, session_expires_at: '2026-11-30T00:00:00Z' }))).exists()).toBe(false)
    })
  })
})
