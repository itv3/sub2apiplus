import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import type { Account, AccountPlatform, AccountType } from '@/types'
import ApiKeyMimicSettingsView from '../ApiKeyMimicSettingsView.vue'
import { adminAPI } from '@/api/admin'

vi.mock('@/stores/app', () => ({
  useAppStore: () => ({
    showError: vi.fn(),
    showSuccess: vi.fn()
  })
}))

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string, params?: Record<string, unknown>) => params ? `${key}:${JSON.stringify(params)}` : key
    })
  }
})

vi.mock('@/api/admin', () => ({
  adminAPI: {
    accounts: {
      list: vi.fn(),
      getKeeperProjects: vi.fn(),
      getKeeperState: vi.fn(),
      getKeeperSettings: vi.fn(),
      getAvailableModels: vi.fn()
    }
  }
}))

function account(id: number, name: string, platform: AccountPlatform, type: AccountType, extra: Record<string, unknown> = {}): Account {
  return {
    id,
    name,
    platform,
    type,
    extra,
    proxy_id: null,
    concurrency: 1,
    priority: 0,
    status: 'active',
    error_message: null,
    last_used_at: null,
    expires_at: null,
    auto_pause_on_expired: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    schedulable: true,
    rate_limited_at: null,
    rate_limit_reset_at: null,
    overload_until: null,
    temp_unschedulable_until: null,
    temp_unschedulable_reason: null,
    session_window_start: null,
    session_window_end: null,
    session_window_status: null
  } as Account
}

function page(items: Account[]) {
  return {
    items,
    total: items.length,
    page: 1,
    page_size: 100,
    pages: 1
  }
}

function mountView() {
  return mount(ApiKeyMimicSettingsView, {
    global: {
      stubs: {
        AppLayout: { template: '<div><slot /></div>' },
        TablePageLayout: { template: '<div><slot name="filters" /><slot name="table" /></div>' },
        DataTable: true,
        Icon: true
      }
    }
  })
}

describe('ApiKeyMimicSettingsView', () => {
  beforeEach(() => {
    vi.mocked(adminAPI.accounts.list).mockReset()
    vi.mocked(adminAPI.accounts.getKeeperProjects).mockReset()
    vi.mocked(adminAPI.accounts.getKeeperState).mockReset()
    vi.mocked(adminAPI.accounts.getKeeperSettings).mockReset()
    vi.mocked(adminAPI.accounts.getAvailableModels).mockReset()

    vi.mocked(adminAPI.accounts.list).mockResolvedValue(page([]))
    vi.mocked(adminAPI.accounts.getKeeperProjects).mockResolvedValue(['project-a'])
    vi.mocked(adminAPI.accounts.getKeeperState).mockResolvedValue({})
    vi.mocked(adminAPI.accounts.getKeeperSettings).mockResolvedValue({})
    vi.mocked(adminAPI.accounts.getAvailableModels).mockResolvedValue([])
  })

  it('保活账号列表只加载并展示 OpenAI/Anthropic API Key 账号', async () => {
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => {
      if (filters?.platform === 'anthropic') {
        return page([
          account(1, 'anthropic-api-key', 'anthropic', 'apikey', { keeper_keepalive_enabled: true }),
          account(2, 'anthropic-oauth', 'anthropic', 'oauth', { keeper_keepalive_enabled: true })
        ])
      }
      if (filters?.platform === 'openai') {
        return page([
          account(3, 'openai-api-key', 'openai', 'apikey', { keeper_keepalive_enabled: true }),
          account(4, 'openai-upstream', 'openai', 'upstream', { keeper_keepalive_enabled: true })
        ])
      }
      return page([])
    })

    const wrapper = mountView()
    await flushPromises()

    const keepaliveTab = wrapper.findAll('button').find(button => button.text() === 'admin.plusEnhancements.tabs.keepalive')
    expect(keepaliveTab).toBeTruthy()
    await keepaliveTab!.trigger('click')
    await flushPromises()

    const settingsTab = wrapper.findAll('button').find(button => button.text() === 'admin.accountKeepalive.tabs.settings')
    expect(settingsTab).toBeTruthy()
    await settingsTab!.trigger('click')
    await flushPromises()

    expect(adminAPI.accounts.list).toHaveBeenCalledWith(1, 100, expect.objectContaining({
      platform: 'anthropic',
      type: 'apikey'
    }))
    expect(adminAPI.accounts.list).toHaveBeenCalledWith(1, 100, expect.objectContaining({
      platform: 'openai',
      type: 'apikey'
    }))
    expect(wrapper.text()).toContain('anthropic-api-key')
    expect(wrapper.text()).toContain('openai-api-key')
    expect(wrapper.text()).not.toContain('anthropic-oauth')
    expect(wrapper.text()).not.toContain('openai-upstream')
  })
})
