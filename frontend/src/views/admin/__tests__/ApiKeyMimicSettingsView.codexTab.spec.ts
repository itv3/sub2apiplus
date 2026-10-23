import { beforeEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'

import ApiKeyMimicSettingsView from '../ApiKeyMimicSettingsView.vue'
import { adminAPI } from '@/api/admin'

const panelReload = vi.hoisted(() => vi.fn())

vi.mock('@/stores/app', () => ({
  useAppStore: () => ({ showError: vi.fn(), showSuccess: vi.fn(), showWarning: vi.fn() })
}))

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return { ...actual, useI18n: () => ({ t: (key: string) => key }) }
})

vi.mock('@/api/admin', () => ({
  adminAPI: { accounts: { list: vi.fn(), updateExtra: vi.fn() } }
}))

// 面板自身的行为在 CodexModelVisibilityPanel.spec.ts 里测试，这里只确认标签的接入。
const PanelStub = defineComponent({
  name: 'CodexModelVisibilityPanel',
  setup(_, { expose }) {
    // 与真实面板一样通过 expose 提供 reload，供页面顶部“刷新”按钮调用。
    expose({ reload: panelReload })
    return {}
  },
  template: '<div data-testid="codex-panel-stub" />'
})

const DataTableStub = defineComponent({
  name: 'DataTableStub',
  template: '<div data-testid="account-table-stub"><slot name="empty" /></div>'
})

function mountView() {
  return mount(ApiKeyMimicSettingsView, {
    global: {
      stubs: {
        AppLayout: { template: '<div><slot /></div>' },
        TablePageLayout: { template: '<div><slot name="filters" /><slot name="table" /></div>' },
        DataTable: DataTableStub,
        CodexModelVisibilityPanel: PanelStub,
        Icon: true
      }
    }
  })
}

describe('ApiKeyMimicSettingsView Codex 三方模型启用标签', () => {
  beforeEach(() => {
    panelReload.mockReset()
    vi.mocked(adminAPI.accounts.list).mockReset()
    vi.mocked(adminAPI.accounts.list).mockResolvedValue({ items: [], total: 0, page: 1, page_size: 100, pages: 1 })
  })

  it('新增独立标签，默认仍显示原账号表而不是新面板', async () => {
    const wrapper = mountView()
    await flushPromises()
    expect(wrapper.get('[data-testid="plus-tab-codex-models"]').text()).toBe('admin.codexThirdPartyModels.tabLabel')
    expect(wrapper.find('[data-testid="account-table-stub"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-panel-stub"]').exists()).toBe(false)
  })

  it('切到新标签只渲染新面板，不进入原账号表分支', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="plus-tab-codex-models"]').trigger('click')

    expect(wrapper.find('[data-testid="codex-panel-stub"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="account-table-stub"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="mimic-status-filter"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="standard-status-filter"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('admin.standardApiCompat.description')
  })

  it('从新标签切回原标签时恢复账号表', async () => {
    const wrapper = mountView()
    await flushPromises()
    await wrapper.get('[data-testid="plus-tab-codex-models"]').trigger('click')
    await wrapper.findAll('button').find((button) => button.text() === 'admin.plusEnhancements.tabs.standard')!.trigger('click')
    expect(wrapper.find('[data-testid="codex-panel-stub"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="account-table-stub"]').exists()).toBe(true)
  })

  it('顶部“刷新”在新标签下刷新面板，在原标签下仍刷新账号数据', async () => {
    const wrapper = mountView()
    await flushPromises()
    const refreshButton = () => wrapper.findAll('button').find((button) => button.text() === 'admin.apiKeyMimic.refresh')!
    const accountListCalls = () => vi.mocked(adminAPI.accounts.list).mock.calls.length

    // 原标签：刷新账号数据，不碰面板。
    const callsAtStart = accountListCalls()
    await refreshButton().trigger('click')
    await flushPromises()
    expect(accountListCalls()).toBeGreaterThan(callsAtStart)
    expect(panelReload).not.toHaveBeenCalled()

    // 新标签：只让面板重新加载，不再读取账号数据。
    await wrapper.get('[data-testid="plus-tab-codex-models"]').trigger('click')
    const callsBeforeCodexRefresh = accountListCalls()
    await refreshButton().trigger('click')
    await flushPromises()
    expect(panelReload).toHaveBeenCalledTimes(1)
    expect(accountListCalls()).toBe(callsBeforeCodexRefresh)
  })
})
