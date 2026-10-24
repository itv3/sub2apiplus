import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import AccountsView from '../AccountsView.vue'

const { listAccounts, listWithEtag, getBatchTodayStats, getAllProxies, getAllGroups } = vi.hoisted(() => ({
  listAccounts: vi.fn(),
  listWithEtag: vi.fn(),
  getBatchTodayStats: vi.fn(),
  getAllProxies: vi.fn(),
  getAllGroups: vi.fn()
}))

vi.mock('@/api/admin', () => ({
  adminAPI: {
    accounts: {
      list: listAccounts,
      listWithEtag,
      getBatchTodayStats,
      getUpstreamBillingProbeSettings: vi.fn().mockResolvedValue({ enabled: true, interval_minutes: 30 }),
      delete: vi.fn(),
      batchClearError: vi.fn(),
      batchRefresh: vi.fn(),
      toggleSchedulable: vi.fn()
    },
    proxies: { getAll: getAllProxies },
    groups: { getAll: getAllGroups }
  }
}))

vi.mock('@/stores/app', () => ({
  useAppStore: () => ({ showError: vi.fn(), showSuccess: vi.fn(), showInfo: vi.fn() })
}))
vi.mock('@/stores/auth', () => ({ useAuthStore: () => ({ token: 'test-token' }) }))
vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string, params?: Record<string, unknown>) => (params ? `${key}|${JSON.stringify(params)}` : key)
    })
  }
})

// 只渲染名称列与平台/类型列两个单元格槽。
const DataTableStub = {
  props: ['columns', 'data'],
  template: `
    <div>
      <div v-for="row in data" :key="row.id" :data-row="row.id">
        <div data-cell="name"><slot name="cell-name" :row="row" :value="row.name" /></div>
        <div data-cell="platform"><slot name="cell-platform_type" :row="row" /></div>
      </div>
    </div>
  `
}

const baseRow = {
  proxy_id: null,
  concurrency: 1,
  priority: 1,
  status: 'active',
  schedulable: true,
  group_ids: [],
  extra: {}
}

const ollamaState = (data: Record<string, unknown>) => ({
  account_id: 1,
  eligible: true,
  configured: true,
  auto_refresh_enabled: false,
  encryption_key_configured: true,
  snapshot: {
    status: 'ok',
    fetched_at: '2026-10-24T11:00:00Z',
    last_attempt_at: '2026-10-24T11:00:00Z',
    next_refresh_at: '2026-10-24T12:00:00Z',
    data
  }
})

const rows = [
  {
    ...baseRow,
    id: 1,
    name: 'ollama-pro',
    platform: 'openai',
    type: 'apikey',
    credentials: { base_url: 'https://ollama.com', api_key: 'x' },
    ollama_cloud_usage: ollamaState({
      plan: 'Pro',
      email: 'ollama-user@example.com',
      monthly: { used_percent: 10, reset_at: '2026-11-24T00:00:00Z', reset_text: 'in 4 weeks' }
    })
  },
  {
    ...baseRow,
    id: 2,
    name: 'ollama-free',
    platform: 'openai',
    type: 'apikey',
    credentials: { base_url: 'https://ollama.com', api_key: 'y' },
    ollama_cloud_usage: ollamaState({
      plan: 'Free',
      email: 'free-user@example.com',
      free_usage: { used_percent: 0, reset_at: '2026-11-14T05:33:24Z' },
      // 免费计划带月度窗口也不显示额度重置行
      monthly: { used_percent: 0, reset_at: '2026-11-24T00:00:00Z' }
    })
  },
  {
    ...baseRow,
    id: 3,
    name: 'ollama-approx',
    platform: 'openai',
    type: 'apikey',
    credentials: { base_url: 'https://ollama.com', api_key: 'z' },
    ollama_cloud_usage: ollamaState({
      plan: 'Max',
      monthly: { used_percent: 10, reset_at: '2026-11-07T00:00:00Z', reset_text: 'in 2 weeks', reset_approx: true }
    })
  },
  {
    ...baseRow,
    id: 4,
    name: 'plain-key',
    platform: 'openai',
    type: 'apikey',
    credentials: { base_url: 'https://relay.example.com', api_key: 'k' }
  },
  {
    ...baseRow,
    id: 5,
    name: 'chatgpt',
    platform: 'openai',
    type: 'oauth',
    credentials: { plan_type: 'pro' },
    extra: { email_address: 'chatgpt@example.com' }
  }
]

function mountView() {
  return mount(AccountsView, {
    global: {
      stubs: {
        AppLayout: { template: '<div><slot /></div>' },
        TablePageLayout: { template: '<div><slot name="filters" /><slot name="table" /><slot name="pagination" /></div>' },
        DataTable: DataTableStub,
        HelpTooltip: { props: ['content'], template: '<span><slot name="trigger" /></span>' },
        Pagination: true,
        ConfirmDialog: true,
        AccountTableActions: { template: '<div><slot name="beforeCreate" /><slot name="after" /></div>' },
        AccountTableFilters: true,
        AccountBulkActionsBar: true,
        AccountActionMenu: true,
        ImportDataModal: true,
        ReAuthAccountModal: true,
        AccountTestModal: true,
        AccountStatsModal: true,
        ScheduledTestsPanel: true,
        SyncFromCrsModal: true,
        TempUnschedStatusModal: true,
        ErrorPassthroughRulesModal: true,
        TLSFingerprintProfilesModal: true,
        CreateAccountModal: true,
        EditAccountModal: true,
        BulkEditAccountModal: true,
        AccountCapacityCell: true,
        AccountStatusIndicator: true,
        AccountTodayStatsCell: true,
        AccountGroupsCell: true,
        AccountUsageCell: true,
        Icon: true
      }
    }
  })
}

describe('admin AccountsView Ollama email and plan badge', () => {
  beforeEach(() => {
    localStorage.clear()
    listWithEtag.mockReset()
    getBatchTodayStats.mockReset()
    getAllProxies.mockReset()
    getAllGroups.mockReset()
    listAccounts.mockReset()
    listAccounts.mockResolvedValue({ items: rows, total: rows.length, page: 1, page_size: 20, pages: 1 })
    listWithEtag.mockResolvedValue({ notModified: true, etag: null, data: null })
    getBatchTodayStats.mockResolvedValue({ stats: {} })
    getAllProxies.mockResolvedValue([])
    getAllGroups.mockResolvedValue([])
  })

  const cell = (wrapper: ReturnType<typeof mountView>, id: number, name: 'name' | 'platform') =>
    wrapper.get(`[data-row="${id}"] [data-cell="${name}"]`)

  it('shows the login email under the Ollama account name', async () => {
    const wrapper = mountView()
    await flushPromises()

    expect(cell(wrapper, 1, 'name').text()).toContain('ollama-pro')
    expect(cell(wrapper, 1, 'name').text()).toContain('ollama-user@example.com')
    expect(cell(wrapper, 2, 'name').text()).toContain('free-user@example.com')
    // 没有读到邮箱的 Ollama 账号不显示邮箱行
    expect(cell(wrapper, 3, 'name').text()).toBe('ollama-approx')
  })

  it('shows the plan badge in the platform/type column using the plan text, not ChatGPT tiers', async () => {
    const wrapper = mountView()
    await flushPromises()

    const pro = cell(wrapper, 1, 'platform')
    expect(pro.text()).toContain('Pro')
    expect(pro.text()).not.toContain('Pro 200')
    expect(pro.html()).toContain('bg-violet-100')
    const free = cell(wrapper, 2, 'platform')
    expect(free.text()).toContain('Free')
    expect(free.html()).toContain('bg-gray-100')
    expect(cell(wrapper, 3, 'platform').text()).toContain('Max')
  })

  it('shows the quota reset row only for paid plans with a monthly reset, marking approximate dates', async () => {
    const wrapper = mountView()
    await flushPromises()

    const exact = cell(wrapper, 1, 'platform').get('[data-testid="platform-type-badge-note"]')
    expect(exact.text()).toMatch(/^admin\.accounts\.ollamaQuotaResets 2026-11-2[345]$/)
    expect(exact.attributes('title')).toContain('admin.accounts.ollamaQuotaResetsTitle')
    expect(exact.attributes('title')).toContain('in 4 weeks')

    const approx = cell(wrapper, 3, 'platform').get('[data-testid="platform-type-badge-note"]')
    expect(approx.text()).toMatch(/^admin\.accounts\.ollamaQuotaResetsApprox 11-0[678]$/)
    expect(approx.attributes('title')).toContain('admin.accounts.ollamaQuotaResetsApproxTitle')

    expect(cell(wrapper, 2, 'platform').find('[data-testid="platform-type-badge-note"]').exists()).toBe(false)
  })

  it('leaves non-Ollama accounts unchanged', async () => {
    const wrapper = mountView()
    await flushPromises()

    const plain = cell(wrapper, 4, 'platform')
    expect(plain.find('[data-testid="platform-type-badge-note"]').exists()).toBe(false)
    expect(plain.text()).not.toContain('Pro')
    expect(plain.text()).not.toContain('Free')
    expect(cell(wrapper, 4, 'name').text()).toBe('plain-key')

    const chatgpt = cell(wrapper, 5, 'platform')
    expect(chatgpt.text()).toContain('Pro 200')
    expect(cell(wrapper, 5, 'name').text()).toContain('chatgpt@example.com')
  })
})
