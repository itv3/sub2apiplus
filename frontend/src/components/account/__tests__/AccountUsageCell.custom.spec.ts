import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import AccountUsageCell from '../AccountUsageCell.vue'
import { isMultiProtocolApiKeyPlatform, showsProviderUsageWindow } from '../credentialsBuilder'
import type { Account } from '@/types'

// 本文件是本仓库独有的测试：自定义多协议平台（custom）的用量列显示。
// 单独成文件，是为了不往上游维护的 AccountUsageCell.spec.ts 里加用例，减少合并冲突。

const { getUsage } = vi.hoisted(() => ({
  getUsage: vi.fn()
}))

vi.mock('@/api/admin', () => ({
  adminAPI: {
    accounts: {
      getUsage
    }
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

function makeAccount(overrides: Partial<Account>): Account {
  return {
    id: 1,
    name: 'account',
    platform: 'custom',
    type: 'apikey',
    proxy_id: null,
    concurrency: 1,
    priority: 1,
    status: 'active',
    error_message: null,
    last_used_at: null,
    expires_at: null,
    auto_pause_on_expired: true,
    created_at: '2026-10-25T00:00:00Z',
    updated_at: '2026-10-25T00:00:00Z',
    schedulable: true,
    rate_limited_at: null,
    rate_limit_reset_at: null,
    overload_until: null,
    temp_unschedulable_until: null,
    temp_unschedulable_reason: null,
    session_window_start: null,
    session_window_end: null,
    session_window_status: null,
    ...overrides
  }
}

const todayStats = {
  requests: 12,
  tokens: 3_400,
  cost: 1.234,
  standard_cost: 1.234,
  user_cost: 0.567
}

// 子组件全部替换成带 data-test 的桩，只断言本组件自己的分支选择。
const stubs = {
  UsageProgressBar: {
    props: ['label', 'utilization'],
    template: '<div data-test="usage-bar">{{ label }}|{{ utilization }}</div>'
  },
  AccountQuotaInfo: true,
  OllamaCloudUsageCell: {
    props: ['account'],
    template: '<div data-test="embedded-ollama">ollama</div>'
  },
  OpenCodeGoUsageCell: {
    props: ['account'],
    template: '<div data-test="opencode-go-cell" />'
  },
  CNProviderQuotaCell: {
    template: '<div data-test="cn-quota-cell" />'
  },
  CNProviderBalanceCell: {
    template: '<div data-test="cn-balance-cell" />'
  }
}

describe('showsProviderUsageWindow', () => {
  it('custom 虽是多协议供应商，但不走供应商额度／余额分支', () => {
    expect(isMultiProtocolApiKeyPlatform('custom')).toBe(true)
    expect(showsProviderUsageWindow('custom')).toBe(false)
  })

  it('其他多协议供应商保持原行为', () => {
    for (const platform of ['deepseek', 'kimi', 'zhipu', 'minimax', 'command_code', 'cline', 'opencode_go']) {
      expect(showsProviderUsageWindow(platform), platform).toBe(isMultiProtocolApiKeyPlatform(platform))
      expect(showsProviderUsageWindow(platform), platform).toBe(true)
    }
  })

  it('非多协议平台返回 false', () => {
    expect(showsProviderUsageWindow('openai')).toBe(false)
    expect(showsProviderUsageWindow('anthropic')).toBe(false)
    expect(showsProviderUsageWindow('')).toBe(false)
  })
})

describe('AccountUsageCell 自定义多协议平台', () => {
  beforeEach(() => {
    getUsage.mockReset()
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockImplementation(() => ({
        matches: true,
        media: '(min-width: 768px)',
        onchange: null,
        addListener: vi.fn(),
        removeListener: vi.fn(),
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn()
      }))
    })
  })

  it('custom 账号显示今日统计行，不再只显示「-」', async () => {
    const wrapper = mount(AccountUsageCell, {
      props: { account: makeAccount({ id: 7001 }), todayStats },
      global: { stubs }
    })
    await flushPromises()

    expect(wrapper.text()).toContain('12 req')
    expect(wrapper.text()).toContain('A $1.23')
    expect(wrapper.text()).toContain('U $0.57')
    expect(wrapper.text().trim()).not.toBe('-')
    // 不应走多协议供应商分支：没有 CN 额度／余额子单元格，也不会请求用量接口。
    expect(wrapper.find('[data-test="cn-quota-cell"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="cn-balance-cell"]').exists()).toBe(false)
    expect(getUsage).not.toHaveBeenCalled()
  })

  it('custom 账号设了额度上限时显示额度进度条', async () => {
    const wrapper = mount(AccountUsageCell, {
      props: {
        account: makeAccount({ id: 7002, quota_limit: 10, quota_used: 2.5 } as Partial<Account>),
        todayStats
      },
      global: { stubs }
    })
    await flushPromises()

    expect(wrapper.text()).toContain('12 req')
    expect(wrapper.text()).toContain('total|25')
  })

  it('custom 账号没有任何数据时才回落到「-」', async () => {
    const wrapper = mount(AccountUsageCell, {
      props: { account: makeAccount({ id: 7003 }), todayStats: null },
      global: { stubs }
    })
    await flushPromises()

    expect(wrapper.text().trim()).toBe('-')
  })

  it('custom 下的 Ollama 账号同时显示 Ollama 用量和今日统计', async () => {
    const wrapper = mount(AccountUsageCell, {
      props: {
        account: makeAccount({
          id: 7004,
          ollama_cloud_usage: {
            account_id: 7004,
            eligible: true,
            configured: true,
            auto_refresh_enabled: true,
            encryption_key_configured: true,
            snapshot: null
          }
        } as Partial<Account>),
        todayStats
      },
      global: { stubs }
    })
    await flushPromises()

    expect(wrapper.find('[data-test="embedded-ollama"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('12 req')
    expect(wrapper.text()).toContain('A $1.23')
    expect(wrapper.find('[data-test="cn-quota-cell"]').exists()).toBe(false)
  })

  it('其他多协议平台（kimi coding）仍走原分支：渲染 CN 子单元格，不渲染今日统计', async () => {
    const wrapper = mount(AccountUsageCell, {
      props: {
        account: makeAccount({
          id: 7005,
          platform: 'kimi',
          credentials: { account_mode: 'coding' }
        }),
        todayStats
      },
      global: { stubs }
    })
    await flushPromises()

    expect(wrapper.find('[data-test="cn-quota-cell"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="cn-balance-cell"]').exists()).toBe(true)
    expect(wrapper.text()).not.toContain('12 req')
  })

  it('其他多协议平台（deepseek）没有探测可用时仍显示「-」占位符', async () => {
    const wrapper = mount(AccountUsageCell, {
      props: {
        account: makeAccount({
          id: 7006,
          platform: 'deepseek',
          credentials: { account_mode: 'coding' }
        }),
        todayStats
      },
      global: { stubs }
    })
    await flushPromises()

    expect(wrapper.text()).toContain('-')
    expect(wrapper.text()).not.toContain('12 req')
  })
})
