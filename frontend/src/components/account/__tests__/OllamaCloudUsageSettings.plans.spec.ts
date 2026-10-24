import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import OllamaCloudUsageSettings from '../OllamaCloudUsageSettings.vue'
import type { Account, OllamaCloudUsageData, OllamaCloudUsageState } from '@/types'

const api = vi.hoisted(() => ({
  getOllamaCloudUsage: vi.fn(),
  saveOllamaCloudUsageSession: vi.fn(),
  setOllamaCloudUsageSessionExpiry: vi.fn(),
  deleteOllamaCloudUsageSession: vi.fn(),
  setOllamaCloudUsageAutoRefresh: vi.fn(),
  refreshOllamaCloudUsage: vi.fn()
}))
const notifications = vi.hoisted(() => ({ showSuccess: vi.fn(), showError: vi.fn() }))

vi.mock('@/api/admin', () => ({ adminAPI: { accounts: api } }))
vi.mock('@/stores/app', () => ({ useAppStore: () => notifications }))
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
  data?: OllamaCloudUsageData,
  overrides: Partial<OllamaCloudUsageState> = {},
  status: 'ok' | 'unauthorized' = 'ok'
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

const mountSettings = (state: OllamaCloudUsageState) => mount(OllamaCloudUsageSettings, { props: { account: account(state) } })

// 本地时区的 datetime-local 取值
const localInput = (date: Date) => {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

describe('OllamaCloudUsageSettings plans and Cookie expiry', () => {
  beforeEach(() => {
    Object.values(api).forEach(mock => mock.mockReset())
    notifications.showSuccess.mockReset()
    notifications.showError.mockReset()
  })
  afterEach(() => vi.useRealTimers())

  it('free plan shows plan, email, free usage and free models but no 5h/7d rows', async () => {
    const wrapper = mountSettings(stateWith({
      plan: 'Free',
      email: 'user@example.com',
      balance: '$0',
      free_usage: { used_percent: 0, reset_at: '2026-11-14T05:33:24Z', reset_text: 'in 2 weeks' },
      free_models: ['beam', 'gemma4:31b']
    }))
    await flushPromises()

    const details = wrapper.get('[data-testid="ollama-cloud-usage-details"]')
    expect(wrapper.get('[data-testid="ollama-cloud-detail-plan"]').text()).toBe('Free')
    expect(wrapper.get('[data-testid="ollama-cloud-detail-email"]').text()).toBe('user@example.com')
    expect(wrapper.get('[data-testid="ollama-cloud-detail-free-usage"]').text()).toContain('0%')
    expect(details.text()).toContain('beam, gemma4:31b')
    expect(details.text()).not.toContain('admin.accounts.ollamaCloud.fiveHour')
    expect(details.text()).not.toContain('admin.accounts.ollamaCloud.sevenDay')
    expect(details.text()).not.toContain('admin.accounts.ollamaCloud.monthly')
  })

  it('new paid page shows the monthly row with used amount, percent and an approximate reset', async () => {
    const wrapper = mountSettings(stateWith({
      plan: 'Pro',
      balance: '$60',
      monthly: { used_percent: 20, used_text: '$12', reset_at: '2026-11-24T00:00:00Z', reset_approx: true },
      models: [{ model: 'gemma4:31b', window: 'monthly', requests: 40 }]
    }))
    await flushPromises()

    const monthly = wrapper.get('[data-testid="ollama-cloud-detail-monthly"]').text()
    expect(monthly).toContain('admin.accounts.ollamaCloud.monthlyUsed')
    expect(monthly).toContain('\\"used\\":\\"$12\\"')
    expect(monthly).toContain('admin.accounts.ollamaCloud.resetApprox')
    expect(wrapper.get('[data-testid="ollama-cloud-usage-details"]').text()).toContain('admin.accounts.ollamaCloud.monthlyShort gemma4:31b: 40')
    expect(wrapper.get('[data-testid="ollama-cloud-usage-details"]').text()).not.toContain('admin.accounts.ollamaCloud.fiveHour')
  })

  it('edit page shows the total balance and lists added credits separately, with the monthly limit', async () => {
    const wrapper = mountSettings(stateWith({
      plan: 'Pro',
      balance: '$60',
      added_credits: '$0',
      monthly: { used_percent: 0, used_text: '$0', limit_text: '$60', reset_at: '2026-11-24T12:00:00Z', reset_text: 'in 4 weeks' }
    }))
    await flushPromises()

    expect(wrapper.get('[data-testid="ollama-cloud-detail-balance"]').text()).toBe('$60')
    expect(wrapper.get('[data-testid="ollama-cloud-detail-added-credits"]').text()).toBe('$0')
    const monthly = wrapper.get('[data-testid="ollama-cloud-detail-monthly"]').text()
    expect(monthly).toContain('admin.accounts.ollamaCloud.monthlyUsedOf')
    expect(monthly).toContain('\\"limit\\":\\"$60\\"')
    const bar = wrapper.getComponent('[data-testid="ollama-cloud-detail-monthly-bar"]')
    expect(bar.attributes('title')).toContain('admin.accounts.ollamaCloud.windowTooltipLimit|{"limit":"$60"}')
  })

  it('hides the added-credits row when the page has none', async () => {
    const wrapper = mountSettings(stateWith({ plan: 'max', balance: '$0' }))
    await flushPromises()
    expect(wrapper.find('[data-testid="ollama-cloud-detail-added-credits"]').exists()).toBe(false)
  })

  it('edit page also draws the free and monthly windows as progress bars with tooltips', async () => {
    const monthly = mountSettings(stateWith({
      plan: 'Pro',
      monthly: { used_percent: 20, used_text: '$12', reset_at: '2026-11-24T00:00:00Z' }
    }))
    await flushPromises()
    const monthlyBar = monthly.getComponent('[data-testid="ollama-cloud-detail-monthly-bar"]')
    expect(monthlyBar.props('utilization')).toBe(20)
    expect(monthlyBar.props('label')).toBe('admin.accounts.ollamaCloud.monthlyShort')
    expect(monthlyBar.attributes('title')).toContain('admin.accounts.ollamaCloud.windowTooltipUsed|{"used":"$12"}')

    const free = mountSettings(stateWith({ plan: 'Free', free_usage: { used_percent: 0, reset_at: '2026-11-14T05:33:24Z' } }))
    await flushPromises()
    const freeBar = free.getComponent('[data-testid="ollama-cloud-detail-free-usage-bar"]')
    expect(freeBar.props('utilization')).toBe(0)
    expect(freeBar.props('label')).toBe('admin.accounts.ollamaCloud.freeUsageShort')
  })

  it('edit page lists the latest ten recent requests with raw time text, model and cost', async () => {
    const recent = Array.from({ length: 12 }, (_, i) => ({ time_text: `${i + 1} minutes ago`, model: `model-${i}`, cost: '<$0.01' }))
    const wrapper = mountSettings(stateWith({ plan: 'Pro', recent_requests: recent }))
    await flushPromises()

    const list = wrapper.get('[data-testid="ollama-cloud-recent-requests"]')
    expect(list.findAll('li')).toHaveLength(11) // 10 行 + 「另有 N 条」
    expect(list.text()).toContain('1 minutes ago')
    expect(list.text()).toContain('model-9')
    expect(list.text()).not.toContain('model-10')
    expect(list.text()).toContain('<$0.01')
    expect(list.text()).toContain('"count":2')

    const none = mountSettings(stateWith({ plan: 'Pro' }))
    await flushPromises()
    expect(none.find('[data-testid="ollama-cloud-recent-requests"]').exists()).toBe(false)
  })

  it('legacy page keeps the 5 hour and 7 day rows and has no monthly or free row', async () => {
    const wrapper = mountSettings(stateWith({
      plan: 'max',
      balance: '$0',
      five_hour: { used_percent: 5.6, reset_at: '2026-07-23T03:00:00Z' },
      seven_day: { used_percent: 14.2, reset_at: '2026-07-29T00:00:00Z' }
    }))
    await flushPromises()

    const details = wrapper.get('[data-testid="ollama-cloud-usage-details"]').text()
    expect(details).toContain('admin.accounts.ollamaCloud.fiveHour')
    expect(details).toContain('5.6%')
    expect(details).toContain('admin.accounts.ollamaCloud.sevenDay')
    expect(details).toContain('14.2%')
    expect(wrapper.find('[data-testid="ollama-cloud-detail-monthly"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="ollama-cloud-detail-monthly-bar"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="ollama-cloud-detail-free-usage"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="ollama-cloud-detail-email"]').exists()).toBe(false)
  })

  it('keeps both legacy rows with a dash when the page exposed nothing recognisable', async () => {
    const wrapper = mountSettings(stateWith({ plan: 'Pro' }))
    await flushPromises()
    const details = wrapper.get('[data-testid="ollama-cloud-usage-details"]').text()
    expect(details).toContain('admin.accounts.ollamaCloud.fiveHour')
    expect(details).toContain('admin.accounts.ollamaCloud.sevenDay')
  })

  describe('Cookie expiry input', () => {
    it('echoes the saved expiry and shows it as Cookie：YYYY-MM-DD 过期', async () => {
      const expires = new Date(2026, 10, 30, 8, 30, 0)
      const wrapper = mountSettings(stateWith({ plan: 'Pro' }, { session_expires_at: expires.toISOString() }))
      await flushPromises()

      const input = wrapper.get('[data-testid="ollama-cloud-expiry-input"]')
      expect((input.element as HTMLInputElement).value).toBe(localInput(expires))
      expect(wrapper.get('[data-testid="ollama-cloud-expiry-remaining"]').text())
        .toBe('admin.accounts.ollamaCloud.cookieExpiresOn|{"date":"2026-11-30"}')
    })

    it('colors the line amber within three days and red when expired or the session is unauthorized', async () => {
      vi.useFakeTimers()
      vi.setSystemTime(FIXED_NOW)
      const soon = mountSettings(stateWith({ plan: 'Pro' }, { session_expires_at: new Date(FIXED_NOW.getTime() + 86400000).toISOString() }))
      expect(soon.get('[data-testid="ollama-cloud-expiry-remaining"]').classes()).toContain('text-amber-600')
      const past = mountSettings(stateWith({ plan: 'Pro' }, { session_expires_at: new Date(FIXED_NOW.getTime() - 86400000).toISOString() }))
      expect(past.get('[data-testid="ollama-cloud-expiry-remaining"]').classes()).toContain('text-red-600')
      const dead = mountSettings(stateWith({ plan: 'Pro' }, {}, 'unauthorized'))
      const deadLine = dead.get('[data-testid="ollama-cloud-expiry-remaining"]')
      expect(deadLine.text()).toBe('admin.accounts.ollamaCloud.cookieExpiredShort')
      expect(deadLine.classes()).toContain('text-red-600')
      const unset = mountSettings(stateWith({ plan: 'Pro' }))
      expect(unset.get('[data-testid="ollama-cloud-expiry-remaining"]').text()).toBe('admin.accounts.ollamaCloud.cookieExpiresUnset')
    })

    it('updates only the expiry without a Cookie, then clears it', async () => {
      const saved = stateWith({ plan: 'Pro' }, { session_expires_at: '2026-12-01T00:00:00.000Z' })
      api.setOllamaCloudUsageSessionExpiry.mockResolvedValueOnce(saved)
      const wrapper = mountSettings(stateWith({ plan: 'Pro' }))
      await flushPromises()

      const save = wrapper.get('[data-testid="ollama-cloud-expiry-save"]')
      expect(save.attributes('disabled')).toBeDefined() // 未改动时不可点
      const picked = new Date(2026, 11, 1, 9, 0, 0)
      await wrapper.get('[data-testid="ollama-cloud-expiry-input"]').setValue(localInput(picked))
      expect(save.attributes('disabled')).toBeUndefined()
      await save.trigger('click')
      await flushPromises()

      expect(api.setOllamaCloudUsageSessionExpiry).toHaveBeenCalledWith(7, picked.toISOString())
      expect(api.saveOllamaCloudUsageSession).not.toHaveBeenCalled()
      expect(notifications.showSuccess).toHaveBeenCalledWith('admin.accounts.ollamaCloud.expirySaved')

      api.setOllamaCloudUsageSessionExpiry.mockResolvedValueOnce(stateWith({ plan: 'Pro' }))
      await wrapper.get('[data-testid="ollama-cloud-expiry-input"]').setValue('')
      await wrapper.get('[data-testid="ollama-cloud-expiry-save"]').trigger('click')
      await flushPromises()
      expect(api.setOllamaCloudUsageSessionExpiry).toHaveBeenLastCalledWith(7, null)
      expect(notifications.showSuccess).toHaveBeenLastCalledWith('admin.accounts.ollamaCloud.expiryCleared')
    })

    it('sends the expiry together with a new Cookie, and omits it when the field is empty', async () => {
      api.saveOllamaCloudUsageSession.mockResolvedValue(stateWith({ plan: 'Pro' }))
      const wrapper = mountSettings(stateWith(undefined, { configured: false }))
      await flushPromises()

      const picked = new Date(2026, 11, 1, 9, 0, 0)
      await wrapper.get('#ollama-cloud-session').setValue('wos-session=abc')
      await wrapper.get('[data-testid="ollama-cloud-expiry-input"]').setValue(localInput(picked))
      await wrapper.get('[data-testid="ollama-cloud-session-save"]').trigger('click')
      await flushPromises()
      expect(api.saveOllamaCloudUsageSession).toHaveBeenLastCalledWith(7, 'wos-session=abc', picked.toISOString())

      await wrapper.get('#ollama-cloud-session').setValue('wos-session=def')
      await wrapper.get('[data-testid="ollama-cloud-expiry-input"]').setValue('')
      await wrapper.get('[data-testid="ollama-cloud-session-save"]').trigger('click')
      await flushPromises()
      expect(api.saveOllamaCloudUsageSession).toHaveBeenLastCalledWith(7, 'wos-session=def')
    })

    it('explains where to copy the expiry from', async () => {
      const wrapper = mountSettings(stateWith({ plan: 'Pro' }))
      await flushPromises()
      expect(wrapper.text()).toContain('admin.accounts.ollamaCloud.cookieExpiresHint')
    })
  })
})
