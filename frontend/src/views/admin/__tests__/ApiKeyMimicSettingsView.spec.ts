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
      updateExtra: updateExtraMock,
      getKeeperProjects: vi.fn(),
      getKeeperState: vi.fn(),
      getKeeperSettings: vi.fn(),
      getAvailableModels: vi.fn()
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
    vi.mocked(adminAPI.accounts.getKeeperProjects).mockReset()
    vi.mocked(adminAPI.accounts.getKeeperState).mockReset()
    vi.mocked(adminAPI.accounts.getKeeperSettings).mockReset()
    vi.mocked(adminAPI.accounts.getAvailableModels).mockReset()
    showErrorMock.mockReset()
    showSuccessMock.mockReset()
    showWarningMock.mockReset()

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

  it('keeper 运行时部分失败时仍可加载账号配置', async () => {
    vi.mocked(adminAPI.accounts.list).mockImplementation(async (_page, _pageSize, filters) => {
      if (filters?.platform === 'anthropic') {
        return page([
          account(1, 'anthropic-api-key', 'anthropic', 'apikey', { keeper_keepalive_enabled: true })
        ])
      }
      if (filters?.platform === 'openai') {
        return page([
          account(3, 'openai-api-key', 'openai', 'apikey', { keeper_keepalive_enabled: true })
        ])
      }
      return page([])
    })
    vi.mocked(adminAPI.accounts.getKeeperProjects).mockRejectedValue(new Error('keeper unavailable'))

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

    expect(wrapper.text()).toContain('anthropic-api-key')
    expect(wrapper.text()).toContain('openai-api-key')
    expect(showWarningMock).toHaveBeenCalledWith('admin.accountKeepalive.messages.runtimePartialUnavailable')
  })

  it('保活历史会单独显示完整错误详情', async () => {
    const fullError = 'unexpected status 502 Bad Gateway: {"code":502,"message":"account 9 is not schedulable"}, request id: ee60f84f-2188-4484-ba8e-fafdd713afa3'
    const stdout = '{"method":"error","message":"api_retry: unexpected status 502 Bad Gateway","request_id":"d06ac338-8e20-4926-ad1a-80fb15594cf9"}'

    vi.mocked(adminAPI.accounts.getKeeperState).mockResolvedValue({
      version: '0.1.146-6',
      targets: [
        {
          id: 'target-9',
          name: 'account-9',
          account_id: 9,
          platform: 'openai',
          account_type: 'apikey',
          enabled: true,
          model: 'gpt-5',
          sessions: [
            {
              id: 'session-9',
              status: 'error',
              model: 'gpt-5',
              mode: 'fresh',
              prompt: 'ping',
              reply_text: '',
              summary: 'unexpected status 502 Bad Gateway',
              error: fullError,
              stdout,
              started_at: '2026-07-09T06:44:51Z',
              completed_at: '2026-07-09T06:45:20Z'
            }
          ]
        }
      ]
    })

    const wrapper = mountView()
    await flushPromises()

    const keepaliveTab = wrapper.findAll('button').find(button => button.text() === 'admin.plusEnhancements.tabs.keepalive')
    expect(keepaliveTab).toBeTruthy()
    await keepaliveTab!.trigger('click')
    await flushPromises()

    const historyTab = wrapper.findAll('button').find(button => button.text() === 'admin.accountKeepalive.tabs.history')
    expect(historyTab).toBeTruthy()
    await historyTab!.trigger('click')
    await flushPromises()

    const detailsBlocks = wrapper.findAll('details')
    const modelReplyDetails = detailsBlocks.find(details => details.find('summary').text() === 'admin.accountKeepalive.labels.modelReply')
    const errorDetails = detailsBlocks.find(details => details.find('summary').text() === 'admin.accountKeepalive.labels.errorDetails')

    expect(modelReplyDetails).toBeTruthy()
    expect(modelReplyDetails!.text()).toContain('-')
    expect(modelReplyDetails!.text()).not.toContain(fullError)
    expect(errorDetails).toBeTruthy()
    expect(errorDetails!.text()).toContain(fullError)
    expect(errorDetails!.text()).toContain('[stdout]')
    expect(errorDetails!.text()).toContain('d06ac338-8e20-4926-ad1a-80fb15594cf9')
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
      openai_apikey_mimic_codex_cli: false,
      openai_apikey_mimic_codex_profile: 'desktop_0_142'
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
        openai_apikey_mimic_codex_profile: 'desktop_0_142',
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
      openai_apikey_mimic_codex_profile: 'desktop_0_142',
      enable_tls_fingerprint: true
    })
  })
})
