import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'

import type { Account, AccountPlatform, AccountType } from '@/types'
import ApiKeyMimicSettingsView from '../ApiKeyMimicSettingsView.vue'
import { adminAPI } from '@/api/admin'

const { updateExtraMock, showErrorMock, showSuccessMock, showWarningMock } = vi.hoisted(() => ({
  updateExtraMock: vi.fn(),
  showErrorMock: vi.fn(),
  showSuccessMock: vi.fn(),
  showWarningMock: vi.fn()
}))

vi.mock('@/stores/app', () => ({
  useAppStore: () => ({
    showError: showErrorMock,
    showSuccess: showSuccessMock,
    showWarning: showWarningMock
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
      updateExtra: updateExtraMock
    }
  }
}))

const DataTableStub = defineComponent({
  name: 'DataTableStub',
  props: {
    data: {
      type: Array,
      default: () => []
    }
  },
  template: `
    <div>
      <template v-if="data.length > 0">
        <div v-for="row in data" :key="row.id">
          <slot name="cell-account" :row="row" />
          <slot name="cell-compatible" :row="row" />
          <slot name="cell-status" :row="row" />
        </div>
      </template>
      <slot v-else name="empty" />
    </div>
  `
})

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
        DataTable: DataTableStub,
        Icon: true
      }
    }
  })
}

describe('ApiKeyMimicSettingsView', () => {
  beforeEach(() => {
    vi.mocked(adminAPI.accounts.list).mockReset()
    updateExtraMock.mockReset()
    showErrorMock.mockReset()
    showSuccessMock.mockReset()
    showWarningMock.mockReset()

    vi.mocked(adminAPI.accounts.list).mockResolvedValue(page([]))
  })

  it('仅保留官方客户端兼容与三方客户端兼容两个入口', async () => {
    const wrapper = mountView()
    await flushPromises()

    const tabLabels = wrapper.findAll('button')
      .map(button => button.text())
      .filter(label => label.startsWith('admin.plusEnhancements.tabs.'))
    expect(tabLabels).toEqual([
      'admin.plusEnhancements.tabs.mimic',
      'admin.plusEnhancements.tabs.standard'
    ])
  })

  it('显示 OAuth 官方客户端伪装已内置启用', async () => {
    const wrapper = mountView()
    await flushPromises()

    const notice = wrapper.get('[data-testid="oauth-official-egress-built-in"]')
    expect(notice.text()).toContain('admin.plusEnhancements.officialEgress.title')
    expect(notice.text()).toContain('admin.plusEnhancements.officialEgress.description')
  })

  it('三方客户端兼容仅显示 Anthropic API Key，支持独立筛选及开关', async () => {
    const disabled = account(112, 'anthropic-standard-disabled', 'anthropic', 'apikey')
    const enabled = account(111, 'anthropic-standard-enabled', 'anthropic', 'apikey', { anthropic_apikey_standard_api_compat: true })
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => page(filters?.platform === 'anthropic'
      ? [disabled, enabled, account(113, 'anthropic-oauth', 'anthropic', 'oauth')]
      : [account(114, 'openai-api', 'openai', 'apikey')]))
    updateExtraMock.mockResolvedValue({ ...disabled, extra: { anthropic_apikey_standard_api_compat: true } })

    const wrapper = mountView()
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'admin.plusEnhancements.tabs.standard')!.trigger('click')
    expect(wrapper.text()).toContain('admin.standardApiCompat.description')
    expect(wrapper.text()).not.toContain('anthropic-oauth')
    expect(wrapper.text()).not.toContain('openai-api')
    expect(wrapper.text().indexOf(enabled.name)).toBeLessThan(wrapper.text().indexOf(disabled.name))
    await wrapper.get('[data-testid="standard-status-filter"]').setValue('disabled')
    expect(wrapper.text()).not.toContain(enabled.name)
    await wrapper.get(`[aria-label="${disabled.name} admin.standardApiCompat.toggle"]`).trigger('click')
    await flushPromises()
    expect(updateExtraMock).toHaveBeenCalledWith(112, { anthropic_apikey_standard_api_compat: true })
    expect(showSuccessMock).toHaveBeenCalledWith('admin.standardApiCompat.updateSuccess')
    expect(wrapper.text()).not.toContain(disabled.name)
  })

  it('两个兼容入口均阻止开启冲突模式', async () => {
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => page(filters?.platform === 'anthropic'
      ? [account(1, 'standard-enabled', 'anthropic', 'apikey', { anthropic_apikey_standard_api_compat: true }),
        account(2, 'mimic-enabled', 'anthropic', 'apikey', { anthropic_apikey_mimic_claude_code: true })]
      : []))
    const wrapper = mountView()
    await flushPromises()
    const mimicSwitch = wrapper.get('[aria-label="standard-enabled admin.apiKeyMimic.columns.compatible"]')
    expect(mimicSwitch.attributes('disabled')).toBeDefined()
    await mimicSwitch.trigger('click')
    await wrapper.findAll('button').find(button => button.text() === 'admin.plusEnhancements.tabs.standard')!.trigger('click')
    const standardSwitch = wrapper.get('[aria-label="mimic-enabled admin.standardApiCompat.toggle"]')
    expect(standardSwitch.attributes('disabled')).toBeDefined()
    await standardSwitch.trigger('click')
    expect(wrapper.text()).toContain('admin.standardApiCompat.conflict')
    expect(updateExtraMock).not.toHaveBeenCalled()
  })

  it('三方客户端兼容保存失败后保留原开关状态', async () => {
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => page(filters?.platform === 'anthropic'
      ? [account(112, 'anthropic-api', 'anthropic', 'apikey')]
      : []))
    updateExtraMock.mockRejectedValue({ response: { data: { detail: '保存冲突' } } })
    const wrapper = mountView()
    await flushPromises()
    await wrapper.findAll('button').find(button => button.text() === 'admin.plusEnhancements.tabs.standard')!.trigger('click')
    const toggle = wrapper.get('[aria-label="anthropic-api admin.standardApiCompat.toggle"]')
    await toggle.trigger('click')
    await flushPromises()
    expect(toggle.attributes('aria-checked')).toBe('false')
    expect(toggle.attributes('disabled')).toBeUndefined()
    expect(showErrorMock).toHaveBeenCalledWith('保存冲突')
  })

  it('官方客户端兼容列表默认启用优先并支持状态筛选', async () => {
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => {
      if (filters?.platform === 'anthropic') {
        return page([
          account(30, 'anthropic-disabled', 'anthropic', 'apikey'),
          account(10, 'anthropic-enabled', 'anthropic', 'apikey', { anthropic_apikey_mimic_claude_code: true })
        ])
      }
      if (filters?.platform === 'openai') {
        return page([
          account(40, 'openai-disabled', 'openai', 'apikey'),
          account(20, 'openai-enabled', 'openai', 'apikey', { openai_apikey_mimic_codex_cli: true })
        ])
      }
      return page([])
    })

    const wrapper = mountView()
    await flushPromises()

    const initialText = wrapper.text()
    expect(initialText.indexOf('openai-enabled')).toBeLessThan(initialText.indexOf('anthropic-disabled'))
    expect(initialText.indexOf('anthropic-enabled')).toBeLessThan(initialText.indexOf('openai-disabled'))

    await wrapper.get('[data-testid="mimic-status-filter"]').setValue('enabled')
    expect(wrapper.text()).toContain('openai-enabled')
    expect(wrapper.text()).toContain('anthropic-enabled')
    expect(wrapper.text()).not.toContain('openai-disabled')
    expect(wrapper.text()).not.toContain('anthropic-disabled')

    await wrapper.get('[data-testid="mimic-status-filter"]').setValue('disabled')
    expect(wrapper.text()).not.toContain('openai-enabled')
    expect(wrapper.text()).not.toContain('anthropic-enabled')
    expect(wrapper.text()).toContain('openai-disabled')
    expect(wrapper.text()).toContain('anthropic-disabled')
  })

  it('关闭 mimic 时不会自动写回 TLS 指纹开关', async () => {
    const mimicAccount = account(10, 'openai-mimic', 'openai', 'apikey', {
      openai_apikey_mimic_codex_cli: true,
      enable_tls_fingerprint: true
    })
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => {
      if (filters?.platform === 'openai') return page([mimicAccount])
      return page([])
    })
    updateExtraMock.mockResolvedValue({
      ...mimicAccount,
      extra: { ...mimicAccount.extra, openai_apikey_mimic_codex_cli: false }
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[role="switch"]').trigger('click')
    await flushPromises()

    expect(updateExtraMock).toHaveBeenCalledTimes(1)
    expect(updateExtraMock).toHaveBeenCalledWith(10, {
      openai_apikey_mimic_codex_cli: false
    })
  })

  it('开启 mimic 时会补齐 TLS 指纹开关', async () => {
    const plainAccount = account(11, 'openai-plain', 'openai', 'apikey')
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => {
      if (filters?.platform === 'openai') return page([plainAccount])
      return page([])
    })
    updateExtraMock.mockResolvedValue({
      ...plainAccount,
      extra: {
        openai_apikey_mimic_codex_cli: true,
        enable_tls_fingerprint: true
      }
    })

    const wrapper = mountView()
    await flushPromises()

    await wrapper.get('[role="switch"]').trigger('click')
    await flushPromises()

    expect(updateExtraMock).toHaveBeenCalledTimes(1)
    expect(updateExtraMock).toHaveBeenCalledWith(11, {
      openai_apikey_mimic_codex_cli: true,
      enable_tls_fingerprint: true
    })
  })
})
