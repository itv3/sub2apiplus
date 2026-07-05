import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import ModelWhitelistSelector from '../ModelWhitelistSelector.vue'
import { accountsAPI } from '@/api/admin/accounts'

vi.mock('@/stores/app', () => ({
  useAppStore: () => ({
    showError: vi.fn(),
    showSuccess: vi.fn(),
    showInfo: vi.fn()
  })
}))

vi.mock('@/api/admin/accounts', () => ({
  accountsAPI: {
    syncUpstreamModels: vi.fn(),
    syncUpstreamModelsPreview: vi.fn()
  }
}))

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string) => key
    })
  }
})

describe('ModelWhitelistSelector', () => {
  beforeEach(() => {
    vi.mocked(accountsAPI.syncUpstreamModels).mockReset()
    vi.mocked(accountsAPI.syncUpstreamModelsPreview).mockReset()
  })

  it('Antigravity 同步上游模型时替换当前白名单', async () => {
    vi.mocked(accountsAPI.syncUpstreamModels).mockResolvedValue({
      models: ['gemini-pro-agent', 'claude-sonnet-4-6']
    })

    const wrapper = mount(ModelWhitelistSelector, {
      props: {
        modelValue: ['gemini-3.1-pro-high', 'old-model'],
        platform: 'antigravity',
        accountId: 11
      },
      global: {
        stubs: {
          ModelIcon: true,
          Icon: true
        }
      }
    })

    const syncButton = wrapper.findAll('button').find(button => button.text().includes('syncUpstreamModels'))
    expect(syncButton).toBeTruthy()
    await syncButton!.trigger('click')
    await flushPromises()

    expect(wrapper.emitted('update:modelValue')?.at(-1)?.[0]).toEqual(['gemini-pro-agent', 'claude-sonnet-4-6'])
  })
})
