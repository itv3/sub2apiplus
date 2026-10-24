import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'

import CodexModelVisibilityPanel from '../CodexModelVisibilityPanel.vue'
import zhLocale from '@/i18n/locales/zh'
import enLocale from '@/i18n/locales/en'
import type {
  CodexCatalogBudget,
  CodexCatalogRefreshItem,
  CodexThirdPartyCandidate,
  CodexThirdPartyGroupState,
  CodexThirdPartySourceStatus
} from '@/api/admin/codexThirdPartyModels'

const mocks = vi.hoisted(() => ({
  getAllGroups: vi.fn(),
  getGroupState: vi.fn(),
  previewChanges: vi.fn(),
  saveChanges: vi.fn(),
  markVerified: vi.fn(),
  refreshCatalogs: vi.fn(),
  listProfileAliases: vi.fn(),
  setProfileAlias: vi.fn(),
  deleteProfileAlias: vi.fn(),
  showError: vi.fn(),
  showSuccess: vi.fn(),
  showWarning: vi.fn()
}))

vi.mock('@/api/admin/groups', () => ({ getAll: mocks.getAllGroups }))
vi.mock('@/api/admin/codexThirdPartyModels', () => ({
  getGroupState: mocks.getGroupState,
  previewChanges: mocks.previewChanges,
  saveChanges: mocks.saveChanges,
  markVerified: mocks.markVerified,
  refreshCatalogs: mocks.refreshCatalogs,
  listProfileAliases: mocks.listProfileAliases,
  setProfileAlias: mocks.setProfileAlias,
  deleteProfileAlias: mocks.deleteProfileAlias
}))
vi.mock('@/stores/app', () => ({
  useAppStore: () => ({
    showError: mocks.showError,
    showSuccess: mocks.showSuccess,
    showWarning: mocks.showWarning
  })
}))
vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string, params?: Record<string, unknown>) => (params ? `${key}:${JSON.stringify(params)}` : key)
    })
  }
})

enableAutoUnmount(afterEach)

// ---------------------------------------------------------------------------
// 夹具
// ---------------------------------------------------------------------------

const GROUP_ID = 9

function group(id: number, name: string, platform: string) {
  return { id, name, platform, status: 'active' }
}

function candidate(overrides: Partial<CodexThirdPartyCandidate> & { slug: string }): CodexThirdPartyCandidate {
  return {
    display_name: overrides.slug,
    native: false,
    enabled: false,
    can_enable: true,
    published: false,
    status: 'configurable',
    capabilities: { supports_search_tool: false },
    ...overrides
  }
}

const COMPLETE_BUDGET: CodexCatalogBudget = {
  limit_bytes: 1048576,
  native_count: 1,
  enabled_third_party_count: 0,
  worst_case_bytes: 600000,
  remaining_bytes: 448576,
  complete: true
}

function source(overrides: Partial<CodexThirdPartySourceStatus> & { account_id: number }): CodexThirdPartySourceStatus {
  return {
    account_name: `账号-${overrides.account_id}`,
    platform: 'kimi',
    account_status: 'active',
    status: 'complete',
    stale: false,
    fingerprint_matches: true,
    model_count: 3,
    refreshing: false,
    ...overrides
  }
}

function groupState(options: {
  revision?: number
  candidates?: CodexThirdPartyCandidate[]
  budget?: Partial<CodexCatalogBudget>
  sources?: CodexThirdPartySourceStatus[]
  models?: CodexThirdPartyGroupState['config']['models']
} = {}): CodexThirdPartyGroupState {
  return {
    group_id: GROUP_ID,
    group_name: '混合分组',
    group_platform: 'composite',
    config: { schema_version: 1, revision: options.revision ?? 3, models: options.models ?? {} },
    evaluation: {
      group_id: GROUP_ID,
      group_platform: 'composite',
      revision: options.revision ?? 3,
      input_fingerprint: 'input-fp',
      candidates: options.candidates ?? defaultCandidates(),
      budget: { ...COMPLETE_BUDGET, ...options.budget },
      evaluated_at: '2026-10-23T00:00:00Z'
    },
    sources: options.sources ?? []
  }
}

function defaultCandidates(): CodexThirdPartyCandidate[] {
  return [
    candidate({ slug: 'gpt-5.5', native: true, can_enable: false, status: 'native' }),
    candidate({
      slug: 'kimi-k3',
      target_platform: 'kimi',
      upstream_model: 'kimi-k3',
      protocol: 'responses_native',
      account_ids: [101],
      fingerprint: 'fp-kimi',
      capabilities: {
        input_modalities: ['text', 'image'],
        reasoning_levels: ['low', 'high', 'max'],
        default_reasoning_level: 'max',
        context_window: 1048576,
        shell_type: 'shell_command',
        apply_patch_tool_type: 'freeform',
        supports_search_tool: true
      }
    }),
    candidate({ slug: 'deepseek-flash', target_platform: 'deepseek', upstream_model: 'deepseek-flash', fingerprint: 'fp-ds' }),
    candidate({
      slug: 'glm-5.3',
      can_enable: false,
      status: 'needs_capability',
      target_platform: 'zhipu',
      reasons: ['缺少上下文窗口依据']
    })
  ]
}

async function mountPanel(state: CodexThirdPartyGroupState = groupState()) {
  mocks.getAllGroups.mockResolvedValue([
    group(GROUP_ID, '混合分组', 'composite'),
    group(2, 'OpenAI 分组', 'openai'),
    group(3, 'Kimi 分组', 'kimi'),
    group(4, 'Claude 分组', 'anthropic'),
    group(5, 'Gemini 分组', 'gemini'),
    group(6, 'Antigravity 分组', 'antigravity')
  ])
  mocks.getGroupState.mockResolvedValue(state)
  const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true } } })
  await flushPromises()
  await wrapper.get('[data-testid="codex-group-select"]').setValue(String(GROUP_ID))
  await flushPromises()
  return wrapper
}

function sw(wrapper: Awaited<ReturnType<typeof mountPanel>>, slug: string) {
  return wrapper.get(`[data-testid="codex-switch-${slug}"]`)
}

beforeEach(() => {
  vi.resetAllMocks()
})

afterEach(() => {
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// 分组选择与表格展示
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 分组与表格', () => {
  it('只列出提供 Codex 在线目录的分组', async () => {
    const wrapper = await mountPanel()
    const labels = wrapper.findAll('[data-testid="codex-group-select"] option').map((option) => option.text())
    expect(labels.some((label) => label.includes('混合分组'))).toBe(true)
    expect(labels.some((label) => label.includes('OpenAI 分组'))).toBe(true)
    expect(labels.some((label) => label.includes('Kimi 分组'))).toBe(true)
    for (const excluded of ['Claude 分组', 'Gemini 分组', 'Antigravity 分组']) {
      expect(labels.some((label) => label.includes(excluded))).toBe(false)
    }
  })

  it('没有符合条件的分组时给出提示', async () => {
    mocks.getAllGroups.mockResolvedValue([group(4, 'Claude 分组', 'anthropic')])
    const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true } } })
    await flushPromises()
    expect(wrapper.find('[data-testid="codex-no-groups"]').exists()).toBe(true)
  })

  it('未选分组时不读取配置，选择后读取并展示候选', async () => {
    const wrapper = await mountPanel()
    expect(mocks.getGroupState).toHaveBeenCalledWith(GROUP_ID)
    for (const slug of ['kimi-k3', 'deepseek-flash', 'glm-5.3']) {
      expect(wrapper.find(`[data-testid="codex-row-${slug}"]`).exists()).toBe(true)
    }
  })

  it('不展示原生 GPT，也不展示没有账号承接且未开启的条目；已开启但失去承接的仍展示', async () => {
    const wrapper = await mountPanel(
      groupState({
        candidates: [
          ...defaultCandidates(),
          candidate({ slug: 'glm-5.2', can_enable: false, status: 'unavailable', reasons: ['分组内没有账号承接该模型'] }),
          candidate({ slug: 'old-model', enabled: true, can_enable: false, status: 'unavailable', reasons: ['该模型已不在分组默认启用的模型列表中'] })
        ]
      })
    )
    expect(wrapper.find('[data-testid="codex-row-gpt-5.5"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-row-glm-5.2"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-row-old-model"]').exists()).toBe(true)
    // 正常状态的预算折叠成小标签，原生数量在悬停提示里。
    expect(wrapper.get('[data-testid="codex-budget-chip"]').attributes('title')).toContain('1')
  })

  it('展示实际目标、能力摘要和状态徽标', async () => {
    const wrapper = await mountPanel(groupState({ sources: [source({ account_id: 101, account_name: 'kimi-主账号' })] }))
    const row = wrapper.get('[data-testid="codex-row-kimi-k3"]').text()
    expect(row).toContain('Kimi')
    expect(row).toContain('kimi-k3')
    expect(row).toContain('admin.codexThirdPartyModels.protocol.responses_native')
    expect(row).toContain('kimi-主账号')
    expect(row).toContain('text / image')
    expect(row).toContain('low / high / max')
    expect(row).toContain('1M')
    expect(row).toContain('apply_patch: freeform')
    expect(wrapper.get('[data-testid="codex-status-kimi-k3"]').text()).toBe('admin.codexThirdPartyModels.status.configurable')
    expect(wrapper.get('[data-testid="codex-status-glm-5.3"]').text()).toBe('admin.codexThirdPartyModels.status.needs_capability')
  })

  it('跨平台承接：实际目标列显示首选账号，并在下面列出备选账号；单平台行不显示这两行', async () => {
    const crossPlatform = candidate({
      slug: 'deepseek-flash',
      target_platform: 'custom',
      upstream_model: 'deepseek-flash',
      protocol: 'responses_native',
      account_ids: [12, 10, 11, 14, 15],
      primary_account: { id: 12, name: 'Custom-主账号', platform: 'custom' },
      alternate_accounts: [
        { id: 10, name: 'DeepSeek', platform: 'deepseek' },
        { id: 11, name: '', platform: 'command_code' },
        { id: 14, name: '备用 A', platform: 'deepseek' },
        { id: 15, name: '备用 B', platform: 'deepseek' }
      ],
      fingerprint: 'fp-cross'
    })
    const wrapper = await mountPanel(
      groupState({ candidates: [...defaultCandidates(), crossPlatform], sources: [source({ account_id: 101, account_name: 'kimi-主账号' })] })
    )

    const primary = wrapper.get('[data-testid="codex-primary-deepseek-flash"]').text()
    expect(primary).toContain('admin.codexThirdPartyModels.target.primary')
    expect(primary).toContain('Custom-主账号')
    const alternates = wrapper.get('[data-testid="codex-alternates-deepseek-flash"]').text()
    expect(alternates).toContain('admin.codexThirdPartyModels.target.alternates')
    expect(alternates).toContain('DeepSeek')
    expect(alternates).toContain('#11')
    expect(alternates).toContain('备用 A')
    // 备选最多列 3 个，其余折叠成“等 N 个”。
    expect(alternates).not.toContain('备用 B')
    expect(alternates).toContain('admin.codexThirdPartyModels.target.more')
    // 有首选账号时不再重复显示“账号：…”那一行。
    expect(wrapper.get('[data-testid="codex-row-deepseek-flash"]').text()).not.toContain('admin.codexThirdPartyModels.target.accounts')

    // 单平台行：没有首选/备选行，沿用原来的“账号：”显示。
    expect(wrapper.find('[data-testid="codex-primary-kimi-k3"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-alternates-kimi-k3"]').exists()).toBe(false)
  })

  it('不能开启的行：开关禁用并显示原因', async () => {
    const wrapper = await mountPanel()
    expect(sw(wrapper, 'glm-5.3').attributes('disabled')).toBeDefined()
    expect(wrapper.get('[data-testid="codex-reason-glm-5.3"]').text()).toBe('缺少上下文窗口依据')
    await sw(wrapper, 'glm-5.3').trigger('click')
    expect(mocks.previewChanges).not.toHaveBeenCalled()
    expect(sw(wrapper, 'kimi-k3').attributes('disabled')).toBeUndefined()
  })

  it('已开启但被阻塞的行保持开启并显示原因，仍可关闭', async () => {
    const blocked = candidate({
      slug: 'kimi-k3',
      enabled: true,
      can_enable: false,
      published: false,
      status: 'conflict',
      reasons: ['同名模型归属多个平台']
    })
    const wrapper = await mountPanel(groupState({ candidates: [blocked], models: { 'kimi-k3': { enabled: true } } }))
    const toggle = sw(wrapper, 'kimi-k3')
    expect(toggle.attributes('aria-checked')).toBe('true')
    expect(toggle.attributes('disabled')).toBeUndefined()
    expect(wrapper.get('[data-testid="codex-reason-kimi-k3"]').text()).toBe('同名模型归属多个平台')
    expect(wrapper.get('[data-testid="codex-row-kimi-k3"]').text()).toContain('admin.codexThirdPartyModels.table.enabledNotPublished')

    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 4, models: {} } })
    await toggle.trigger('click')
    await flushPromises()
    expect(mocks.previewChanges).not.toHaveBeenCalled()
    expect(mocks.saveChanges).toHaveBeenCalledWith(GROUP_ID, 3, [{ slug: 'kimi-k3', enabled: false }], undefined)
  })

  it('已验证的指纹一致时显示验证时间，指纹变化后提示失效', async () => {
    const verifiedAt = '2026-10-22T08:00:00Z'
    const wrapper = await mountPanel(groupState({
      candidates: [
        candidate({ slug: 'kimi-k3', enabled: true, status: 'verified', fingerprint: 'fp-new' }),
        candidate({ slug: 'deepseek-flash', enabled: true, fingerprint: 'fp-ds-new' })
      ],
      models: {
        'kimi-k3': { enabled: true, verification: { fingerprint: 'fp-new', verified_at: verifiedAt } },
        'deepseek-flash': { enabled: true, verification: { fingerprint: 'fp-ds-old', verified_at: verifiedAt } }
      }
    }))
    expect(wrapper.get('[data-testid="codex-row-kimi-k3"]').text()).toContain('admin.codexThirdPartyModels.verify.verifiedAt')
    expect(wrapper.get('[data-testid="codex-row-deepseek-flash"]').text()).toContain('admin.codexThirdPartyModels.verify.outdated')
    expect(wrapper.find('[data-testid="codex-verify-kimi-k3"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-verify-deepseek-flash"]').exists()).toBe(true)
  })

  it('不展示任何密钥字段', async () => {
    const wrapper = await mountPanel(groupState({ sources: [source({ account_id: 101 })] }))
    expect(wrapper.html().toLowerCase()).not.toMatch(/api[_-]?key|sk-[a-z0-9]|bearer|secret/)
  })
})

// ---------------------------------------------------------------------------
// 预算
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 目录预算', () => {
  it('正常状态只出现工具栏里的小标签，不出现整块卡片；悬停提示列出五项明细', async () => {
    const wrapper = await mountPanel(groupState({
      budget: { native_count: 6, enabled_third_party_count: 2, worst_case_bytes: 83661, remaining_bytes: 964915 }
    }))
    const chip = wrapper.get('[data-testid="codex-budget-chip"]')
    // 形如“目录 81.7 KiB / 1 MiB · 未超限”。
    expect(chip.text()).toBe('admin.codexThirdPartyModels.budget.chip:' + JSON.stringify({
      used: '81.7 KiB',
      limit: '1 MiB',
      status: 'admin.codexThirdPartyModels.budget.withinLimit'
    }))
    const title = chip.attributes('title') ?? ''
    expect(title).toContain('admin.codexThirdPartyModels.budget.nativeCount：6')
    expect(title).toContain('admin.codexThirdPartyModels.budget.enabledCount：2')
    expect(title).toContain('83,661')
    expect(title).toContain('964,915')
    expect(title).toContain('"limit":"1,048,576 B (1024.0 KiB)"')
    // 小标签在工具栏里，整块卡片与其中的明细都不出现。
    expect(wrapper.get('[data-testid="codex-toolbar"]').find('[data-testid="codex-budget-chip"]').exists()).toBe(true)
    for (const id of ['codex-budget', 'codex-budget-native', 'codex-budget-worst', 'codex-budget-incomplete', 'codex-budget-over', 'codex-budget-deferred']) {
      expect(wrapper.find(`[data-testid="${id}"]`).exists(), id).toBe(false)
    }
  })

  it('异常状态（超限、未完成、开启时检查）显示整块卡片，不显示小标签', async () => {
    for (const budget of [
      { worst_case_bytes: 1048577, remaining_bytes: -1 },
      { complete: false, reason: '基础目录账号不可得' },
      { complete: false, deferred: true }
    ]) {
      const wrapper = await mountPanel(groupState({ budget }))
      expect(wrapper.find('[data-testid="codex-budget"]').exists(), JSON.stringify(budget)).toBe(true)
      expect(wrapper.find('[data-testid="codex-budget-chip"]').exists(), JSON.stringify(budget)).toBe(false)
    }
  })

  it('超限的整块卡片仍显示原生数量、已开启数量、最坏字节数和剩余空间', async () => {
    const wrapper = await mountPanel(groupState({
      budget: { native_count: 6, enabled_third_party_count: 2, worst_case_bytes: 1048999, remaining_bytes: -423 }
    }))
    expect(wrapper.get('[data-testid="codex-budget-native"]').text()).toBe('6')
    expect(wrapper.get('[data-testid="codex-budget-enabled"]').text()).toBe('2')
    expect(wrapper.get('[data-testid="codex-budget-worst"]').text()).toContain('1,048,999')
    expect(wrapper.get('[data-testid="codex-budget-remaining"]').text()).toContain('-423')
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(true)
  })

  it('预算未完成时显示“未完成检查”，不得显示通过', async () => {
    const wrapper = await mountPanel(groupState({ budget: { complete: false, reason: '基础目录账号不可得' } }))
    expect(wrapper.get('[data-testid="codex-budget-incomplete"]').text()).toBe('admin.codexThirdPartyModels.budget.incomplete')
    expect(wrapper.get('[data-testid="codex-budget-reason"]').text()).toBe('基础目录账号不可得')
    expect(wrapper.find('[data-testid="codex-budget-chip"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(false)
  })

  it('页面读取时预算留到开启时检查：显示“开启时检查”，不显示通过，也不显示为未完成检查', async () => {
    const wrapper = await mountPanel(groupState({
      budget: { complete: false, deferred: true, worst_case_bytes: 0, remaining_bytes: 0, native_count: 0, reason: '页面读取不请求官方账号目录，目录体积在开启时检查' }
    }))
    expect(wrapper.get('[data-testid="codex-budget-deferred"]').text()).toBe('admin.codexThirdPartyModels.budget.deferred')
    expect(wrapper.get('[data-testid="codex-budget-deferred-hint"]').text()).toBe('admin.codexThirdPartyModels.budget.deferredHint')
    expect(wrapper.find('[data-testid="codex-budget-incomplete"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-budget-chip"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(false)
    // 没有真实数据时不展示一排 0 字节。
    expect(wrapper.find('[data-testid="codex-budget-worst"]').exists()).toBe(false)
  })

  it('最坏字节数超过上限时显示超限', async () => {
    const wrapper = await mountPanel(groupState({ budget: { worst_case_bytes: 1048577, remaining_bytes: -1 } }))
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-budget-chip"]').exists()).toBe(false)
  })

  it('恰好等于上限不算超限', async () => {
    const wrapper = await mountPanel(groupState({ budget: { worst_case_bytes: 1048576, remaining_bytes: 0 } }))
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-budget-chip"]').exists()).toBe(true)
  })
})

describe('CodexModelVisibilityPanel 顶部与工具栏', () => {
  it('说明框是一行简短提示，客户端配置说明默认折叠，点开才显示，再点收起', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.get('[data-testid="codex-notice"]').text()).toContain('admin.codexThirdPartyModels.notice')
    expect(wrapper.find('[data-testid="codex-client-hint"]').exists()).toBe(false)
    const toggle = wrapper.get('[data-testid="codex-client-hint-toggle"]')
    expect(toggle.text()).toContain('admin.codexThirdPartyModels.clientHintToggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')

    await toggle.trigger('click')
    expect(wrapper.get('[data-testid="codex-client-hint"]').text()).toBe('admin.codexThirdPartyModels.clientHint')
    expect(toggle.attributes('aria-expanded')).toBe('true')
    await toggle.trigger('click')
    expect(wrapper.find('[data-testid="codex-client-hint"]').exists()).toBe(false)
  })

  it('面板自己没有刷新按钮；reload 仍通过 expose 提供给页面顶部刷新', async () => {
    const wrapper = await mountPanel()
    expect(wrapper.find('[data-testid="codex-refresh-button"]').exists()).toBe(false)
    expect(typeof (wrapper.vm as unknown as { reload: unknown }).reload).toBe('function')
  })

  it('工具栏把分组、搜索、状态筛选、预算小标签和批量按钮放在同一行容器里', async () => {
    const wrapper = await mountPanel()
    const toolbar = wrapper.get('[data-testid="codex-toolbar"]')
    expect(toolbar.classes()).toContain('flex-wrap')
    for (const id of ['codex-group-select', 'codex-search', 'codex-status-filter', 'codex-budget-chip', 'codex-bulk-enable', 'codex-bulk-disable']) {
      expect(toolbar.find(`[data-testid="${id}"]`).exists(), id).toBe(true)
    }
  })

  it('分组下拉带 aria-label，输入控件不写死 h-9（自然高度，文字不被裁）', async () => {
    const wrapper = await mountPanel()
    const group = wrapper.get('[data-testid="codex-group-select"]')
    expect(group.attributes('aria-label')).toBe('admin.codexThirdPartyModels.group.label')
    for (const id of ['codex-group-select', 'codex-search', 'codex-status-filter']) {
      expect(wrapper.get(`[data-testid="${id}"]`).classes(), id).not.toContain('h-9')
    }
  })

  it('没选分组时工具栏只有分组下拉，保留“请选择分组”占位', async () => {
    mocks.getAllGroups.mockResolvedValue([group(GROUP_ID, '混合分组', 'composite')])
    const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true } } })
    await flushPromises()
    const toolbar = wrapper.get('[data-testid="codex-toolbar"]')
    expect(toolbar.find('[data-testid="codex-group-select"]').exists()).toBe(true)
    for (const id of ['codex-search', 'codex-status-filter', 'codex-budget-chip', 'codex-bulk-enable', 'codex-bulk-disable']) {
      expect(toolbar.find(`[data-testid="${id}"]`).exists(), id).toBe(false)
    }
    expect(wrapper.text()).toContain('admin.codexThirdPartyModels.group.choosePrompt')
  })
})

// ---------------------------------------------------------------------------
// 开关：开启走预览再保存，关闭直接保存
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 开关与保存', () => {
  it('开启：先预览，再带着预览指纹保存，然后重新读取', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: groupState().evaluation })
    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 4, models: { 'kimi-k3': { enabled: true } } } })
    mocks.getGroupState.mockResolvedValue(groupState({
      revision: 4,
      candidates: defaultCandidates().map((item) => (item.slug === 'kimi-k3' ? { ...item, enabled: true, published: true } : item)),
      models: { 'kimi-k3': { enabled: true } }
    }))

    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()

    expect(mocks.previewChanges).toHaveBeenCalledWith(GROUP_ID, 3, [{ slug: 'kimi-k3', enabled: true }])
    expect(mocks.saveChanges).toHaveBeenCalledWith(GROUP_ID, 3, [{ slug: 'kimi-k3', enabled: true }], 'input-fp')
    expect(mocks.previewChanges.mock.invocationCallOrder[0]).toBeLessThan(mocks.saveChanges.mock.invocationCallOrder[0])
    expect(mocks.showSuccess).toHaveBeenCalledWith('admin.codexThirdPartyModels.saveSuccess')
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
    expect(sw(wrapper, 'kimi-k3').attributes('aria-checked')).toBe('true')
  })

  it('保存成功之前不显示为开启', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: groupState().evaluation })
    let finishSave: (value: unknown) => void = () => {}
    mocks.saveChanges.mockReturnValue(new Promise((resolve) => { finishSave = resolve }))

    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()
    expect(sw(wrapper, 'kimi-k3').attributes('aria-checked')).toBe('false')
    expect(sw(wrapper, 'deepseek-flash').attributes('disabled')).toBeDefined()
    finishSave({ config: { schema_version: 1, revision: 4, models: {} } })
    await flushPromises()
  })

  it('关闭：不预览，直接保存且不带指纹', async () => {
    const wrapper = await mountPanel(groupState({
      candidates: defaultCandidates().map((item) => (item.slug === 'kimi-k3' ? { ...item, enabled: true, published: true } : item)),
      models: { 'kimi-k3': { enabled: true } }
    }))
    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 4, models: {} } })

    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()

    expect(mocks.previewChanges).not.toHaveBeenCalled()
    expect(mocks.saveChanges).toHaveBeenCalledWith(GROUP_ID, 3, [{ slug: 'kimi-k3', enabled: false }], undefined)
  })

  it('预览显示不能开启时不保存，并回显原因', async () => {
    const wrapper = await mountPanel()
    const rejectedEvaluation = groupState({
      candidates: defaultCandidates().map((item) =>
        item.slug === 'kimi-k3' ? { ...item, can_enable: false, reasons: ['路由归属冲突'] } : item)
    }).evaluation
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: rejectedEvaluation })

    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()

    expect(mocks.saveChanges).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="codex-error"]').text()).toContain('admin.codexThirdPartyModels.reject.title')
    expect(wrapper.get('[data-testid="codex-rejections"]').text()).toContain('路由归属冲突')
    expect(mocks.showError).toHaveBeenCalled()
  })

  it('预览预算未完成或超限时不保存', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({
      revision: 3,
      evaluation: groupState({ budget: { complete: false, reason: '基础目录账号不可得' } }).evaluation
    })
    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()
    expect(mocks.saveChanges).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="codex-rejections"]').text()).toContain('基础目录账号不可得')

    mocks.previewChanges.mockResolvedValue({
      revision: 3,
      evaluation: groupState({ budget: { worst_case_bytes: 1048577, remaining_bytes: -1 } }).evaluation
    })
    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()
    expect(mocks.saveChanges).not.toHaveBeenCalled()
    expect(wrapper.get('[data-testid="codex-rejections"]').text()).toContain('admin.codexThirdPartyModels.reject.overLimit')
  })

  it('服务端整批拒绝时回显逐项原因', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: groupState().evaluation })
    mocks.saveChanges.mockRejectedValue({
      status: 400,
      reason: 'CODEX_MODEL_ENABLE_REJECTED',
      message: '所选模型无法全部开启，整批均未保存',
      metadata: { 'model:kimi-k3': '能力依据在刚才发生了变化', budget: '目录体积预算检查未完成' }
    })
    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()

    const text = wrapper.get('[data-testid="codex-error"]').text()
    expect(text).toContain('所选模型无法全部开启，整批均未保存')
    expect(text).toContain('能力依据在刚才发生了变化')
    expect(text).toContain('目录体积预算检查未完成')
    expect(mocks.showError).toHaveBeenCalledWith('所选模型无法全部开启，整批均未保存')
  })

  it('保存失败时回显服务端给出的原因', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: groupState().evaluation })
    mocks.saveChanges.mockRejectedValue({ status: 500, message: '数据库暂时不可用' })
    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="codex-error"]').text()).toContain('数据库暂时不可用')
    expect(mocks.showError).toHaveBeenCalledWith('数据库暂时不可用')
    expect(sw(wrapper, 'kimi-k3').attributes('aria-checked')).toBe('false')
    expect(sw(wrapper, 'kimi-k3').attributes('disabled')).toBeUndefined()
  })

  it('409 冲突：提示后重新加载最新配置', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: groupState().evaluation })
    mocks.saveChanges.mockRejectedValue({ status: 409, reason: 'CODEX_MODEL_VISIBILITY_CONFLICT', message: '配置已被修改' })
    mocks.getGroupState.mockResolvedValue(groupState({ revision: 5 }))

    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()

    expect(mocks.showWarning).toHaveBeenCalledWith('admin.codexThirdPartyModels.conflict')
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="codex-error"]').text()).toContain('admin.codexThirdPartyModels.conflict')

    // 重新加载之后用新的 revision 再次操作。
    mocks.saveChanges.mockReset()
    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 6, models: {} } })
    mocks.previewChanges.mockResolvedValue({ revision: 5, evaluation: groupState({ revision: 5 }).evaluation })
    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()
    expect(mocks.previewChanges).toHaveBeenLastCalledWith(GROUP_ID, 5, [{ slug: 'kimi-k3', enabled: true }])
  })

  it('预览阶段的 409 同样提示并重新加载', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockRejectedValue({ status: 409, reason: 'CODEX_MODEL_VISIBILITY_CONFLICT', message: '配置已被修改' })
    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()
    expect(mocks.saveChanges).not.toHaveBeenCalled()
    expect(mocks.showWarning).toHaveBeenCalled()
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
  })

  it('读取配置失败时显示原因', async () => {
    mocks.getAllGroups.mockResolvedValue([group(GROUP_ID, '混合分组', 'composite')])
    mocks.getGroupState.mockRejectedValue({ status: 500, message: '已保存的配置无法解析' })
    const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true } } })
    await flushPromises()
    await wrapper.get('[data-testid="codex-group-select"]').setValue(String(GROUP_ID))
    await flushPromises()
    expect(wrapper.get('[data-testid="codex-error"]').text()).toContain('已保存的配置无法解析')
    expect(wrapper.find('[data-testid="codex-model-table"]').exists()).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 搜索、筛选、批量
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 搜索筛选与批量', () => {
  it('按名称、上游型号搜索', async () => {
    const wrapper = await mountPanel()
    await wrapper.get('[data-testid="codex-search"]').setValue('KIMI')
    expect(wrapper.find('[data-testid="codex-row-kimi-k3"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-row-deepseek-flash"]').exists()).toBe(false)
    await wrapper.get('[data-testid="codex-search"]').setValue('zhipu')
    expect(wrapper.find('[data-testid="codex-row-glm-5.3"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-row-kimi-k3"]').exists()).toBe(false)
    await wrapper.get('[data-testid="codex-search"]').setValue('不存在的模型')
    expect(wrapper.find('[data-testid="codex-empty-rows"]').exists()).toBe(true)
  })

  it('状态筛选只有四项：全部、已开启、未开启、不可开启', async () => {
    const wrapper = await mountPanel()
    const options = wrapper.get('[data-testid="codex-status-filter"]').findAll('option')
    expect(options.map((option) => option.attributes('value'))).toEqual(['all', 'enabled', 'disabled', 'blocked'])
    expect(options.map((option) => option.text())).toEqual([
      'admin.codexThirdPartyModels.filters.all',
      'admin.codexThirdPartyModels.filters.enabled',
      'admin.codexThirdPartyModels.filters.disabled',
      'admin.codexThirdPartyModels.filters.blocked'
    ])
  })

  it('按状态筛选', async () => {
    const wrapper = await mountPanel(groupState({
      candidates: [
        ...defaultCandidates().map((item) => (item.slug === 'deepseek-flash' ? { ...item, enabled: true } : item)),
        candidate({ slug: 'conflict-model', can_enable: false, status: 'conflict', reasons: ['归属冲突'] }),
        // 已开启但被阻塞：仍属于“已开启”，不属于“不可开启”以外的……can_enable=false 时同时算不可开启。
        candidate({ slug: 'open-blocked', enabled: true, can_enable: false, status: 'unavailable', reasons: ['失去承接'] }),
        candidate({ slug: 'over-model', can_enable: false, status: 'over_budget' })
      ]
    }))
    await wrapper.get('[data-testid="codex-status-filter"]').setValue('blocked')
    expect(wrapper.findAll('tbody tr[data-testid^="codex-row-"]').map((row) => row.attributes('data-testid')))
      .toEqual(['codex-row-glm-5.3', 'codex-row-conflict-model', 'codex-row-open-blocked', 'codex-row-over-model'])
    // 行内仍显示原因。
    expect(wrapper.get('[data-testid="codex-reason-glm-5.3"]').text()).toContain('缺少上下文窗口依据')
    expect(wrapper.get('[data-testid="codex-reason-conflict-model"]').text()).toContain('归属冲突')

    await wrapper.get('[data-testid="codex-status-filter"]').setValue('enabled')
    expect(wrapper.findAll('tbody tr[data-testid^="codex-row-"]').map((row) => row.attributes('data-testid')))
      .toEqual(['codex-row-deepseek-flash', 'codex-row-open-blocked'])

    await wrapper.get('[data-testid="codex-status-filter"]').setValue('disabled')
    expect(wrapper.find('[data-testid="codex-row-gpt-5.5"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-row-kimi-k3"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-row-deepseek-flash"]').exists()).toBe(false)

    await wrapper.get('[data-testid="codex-status-filter"]').setValue('all')
    expect(wrapper.find('[data-testid="codex-row-kimi-k3"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-row-glm-5.3"]').exists()).toBe(true)
  })

  it('批量开启：一次预览、一次保存，整批原子', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: groupState().evaluation })
    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 4, models: {} } })

    expect(wrapper.get('[data-testid="codex-bulk-enable"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="codex-select-kimi-k3"]').setValue(true)
    await wrapper.get('[data-testid="codex-select-deepseek-flash"]').setValue(true)
    expect(wrapper.get('[data-testid="codex-bulk-enable"]').text()).toContain('"count":2')
    await wrapper.get('[data-testid="codex-bulk-enable"]').trigger('click')
    await flushPromises()

    expect(mocks.previewChanges).toHaveBeenCalledTimes(1)
    expect(mocks.previewChanges).toHaveBeenCalledWith(GROUP_ID, 3, [
      { slug: 'kimi-k3', enabled: true },
      { slug: 'deepseek-flash', enabled: true }
    ])
    expect(mocks.saveChanges).toHaveBeenCalledTimes(1)
    expect(mocks.saveChanges).toHaveBeenCalledWith(GROUP_ID, 3, [
      { slug: 'kimi-k3', enabled: true },
      { slug: 'deepseek-flash', enabled: true }
    ], 'input-fp')
  })

  it('批量开启中有一项不可开启时整批都不保存', async () => {
    const wrapper = await mountPanel()
    mocks.previewChanges.mockResolvedValue({ revision: 3, evaluation: groupState().evaluation })

    await wrapper.get('[data-testid="codex-select-kimi-k3"]').setValue(true)
    await wrapper.get('[data-testid="codex-select-glm-5.3"]').setValue(true)
    await wrapper.get('[data-testid="codex-bulk-enable"]').trigger('click')
    await flushPromises()

    expect(mocks.saveChanges).not.toHaveBeenCalled()
    const rejections = wrapper.get('[data-testid="codex-rejections"]').text()
    expect(rejections).toContain('缺少上下文窗口依据')
    expect(rejections).not.toContain('kimi-k3')
  })

  it('批量关闭只包含已开启的选中项，直接保存', async () => {
    const wrapper = await mountPanel(groupState({
      candidates: defaultCandidates().map((item) =>
        item.slug === 'kimi-k3' || item.slug === 'deepseek-flash' ? { ...item, enabled: true, published: true } : item),
      models: { 'kimi-k3': { enabled: true }, 'deepseek-flash': { enabled: true } }
    }))
    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 4, models: {} } })

    await wrapper.get('[data-testid="codex-select-all"]').setValue(true)
    expect(wrapper.get('[data-testid="codex-bulk-disable"]').text()).toContain('"count":2')
    expect(wrapper.get('[data-testid="codex-bulk-enable"]').attributes('disabled')).toBeDefined()
    await wrapper.get('[data-testid="codex-bulk-disable"]').trigger('click')
    await flushPromises()

    expect(mocks.previewChanges).not.toHaveBeenCalled()
    expect(mocks.saveChanges).toHaveBeenCalledWith(GROUP_ID, 3, [
      { slug: 'kimi-k3', enabled: false },
      { slug: 'deepseek-flash', enabled: false }
    ], undefined)
  })

  it('全选只作用于当前筛选结果里的非原生行', async () => {
    const wrapper = await mountPanel()
    await wrapper.get('[data-testid="codex-search"]').setValue('kimi')
    await wrapper.get('[data-testid="codex-select-all"]').setValue(true)
    expect(wrapper.get('[data-testid="codex-bulk-enable"]').text()).toContain('"count":1')
    expect((wrapper.get('[data-testid="codex-select-kimi-k3"]').element as HTMLInputElement).checked).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// 不再展示源同步：没有“同步候选”按钮，没有来源同步状态区块，也不轮询
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 不再有源同步', () => {
  it('没有“同步候选”按钮，也没有账号模型来源同步状态区块', async () => {
    const wrapper = await mountPanel(groupState({
      sources: [source({ account_id: 101, account_name: 'kimi-主账号' }), source({ account_id: 104, refreshing: true })]
    }))
    expect(wrapper.find('[data-testid="codex-sync-button"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-sources"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-source-101"]').exists()).toBe(false)
  })

  it('页面读取只请求一次，不因账号“刷新中”而定时重新读取', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    await mountPanel(groupState({ sources: [source({ account_id: 101, refreshing: true })] }))
    expect(mocks.getGroupState).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(30000)
    await flushPromises()
    expect(mocks.getGroupState).toHaveBeenCalledTimes(1)
  })

  it('候选行的账号名称仍取自读取接口返回的账号列表', async () => {
    const wrapper = await mountPanel(groupState({ sources: [source({ account_id: 101, account_name: 'kimi-主账号' })] }))
    expect(wrapper.get('[data-testid="codex-row-kimi-k3"]').text()).toContain('kimi-主账号')
  })
})

// ---------------------------------------------------------------------------
// 标记已验证
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 标记已验证', () => {
  function enabledState() {
    return groupState({
      candidates: defaultCandidates().map((item) =>
        item.slug === 'kimi-k3' ? { ...item, enabled: true, published: true } : item),
      models: { 'kimi-k3': { enabled: true } }
    })
  }

  it('只有已开启且带指纹的行可以标记已验证', async () => {
    const wrapper = await mountPanel(enabledState())
    expect(wrapper.find('[data-testid="codex-verify-kimi-k3"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-verify-deepseek-flash"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-verify-gpt-5.5"]').exists()).toBe(false)
  })

  it('确认后带着当前指纹和备注提交，并重新读取', async () => {
    const wrapper = await mountPanel(enabledState())
    mocks.markVerified.mockResolvedValue({ config: { schema_version: 1, revision: 4, models: {} } })

    await wrapper.get('[data-testid="codex-verify-kimi-k3"]').trigger('click')
    expect(wrapper.find('[data-testid="codex-verify-form"]').exists()).toBe(true)
    await wrapper.get('[data-testid="codex-verify-note"]').setValue(' 桌面端流式与 apply_patch 验收通过 ')
    await wrapper.get('[data-testid="codex-verify-confirm"]').trigger('click')
    await flushPromises()

    expect(mocks.markVerified).toHaveBeenCalledWith(GROUP_ID, 3, 'kimi-k3', 'fp-kimi', '桌面端流式与 apply_patch 验收通过')
    expect(mocks.showSuccess).toHaveBeenCalledWith('admin.codexThirdPartyModels.verify.success')
    expect(wrapper.find('[data-testid="codex-verify-form"]').exists()).toBe(false)
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
  })

  it('可以取消，取消后不提交', async () => {
    const wrapper = await mountPanel(enabledState())
    await wrapper.get('[data-testid="codex-verify-kimi-k3"]').trigger('click')
    await wrapper.get('[data-testid="codex-verify-cancel"]').trigger('click')
    expect(wrapper.find('[data-testid="codex-verify-form"]').exists()).toBe(false)
    expect(mocks.markVerified).not.toHaveBeenCalled()
  })

  it('指纹已变化（409）时提示并重新读取', async () => {
    const wrapper = await mountPanel(enabledState())
    mocks.markVerified.mockRejectedValue({ status: 409, reason: 'CODEX_MODEL_VERIFICATION_STALE', message: '指纹已变化' })
    await wrapper.get('[data-testid="codex-verify-kimi-k3"]').trigger('click')
    await wrapper.get('[data-testid="codex-verify-confirm"]').trigger('click')
    await flushPromises()
    expect(mocks.showWarning).toHaveBeenCalled()
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
  })
})

// ---------------------------------------------------------------------------
// 供外部调用的整体刷新（Plus 页顶部“刷新”按钮）
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 暴露的 reload', () => {
  it('重新读取分组列表，并重新加载当前选中的分组', async () => {
    const wrapper = await mountPanel()
    expect(mocks.getAllGroups).toHaveBeenCalledTimes(1)
    expect(mocks.getGroupState).toHaveBeenCalledTimes(1)

    mocks.getGroupState.mockResolvedValue(groupState({ revision: 8 }))
    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    await flushPromises()

    expect(mocks.getAllGroups).toHaveBeenCalledTimes(2)
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
    expect(mocks.getGroupState).toHaveBeenLastCalledWith(GROUP_ID)
    // 重新加载之后拿到的是新的 revision。
    mocks.previewChanges.mockResolvedValue({ revision: 8, evaluation: groupState({ revision: 8 }).evaluation })
    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 9, models: {} } })
    await sw(wrapper, 'kimi-k3').trigger('click')
    await flushPromises()
    expect(mocks.previewChanges).toHaveBeenLastCalledWith(GROUP_ID, 8, [{ slug: 'kimi-k3', enabled: true }])
  })

  it('没有选中分组时只刷新分组列表，不读取配置', async () => {
    mocks.getAllGroups.mockResolvedValue([group(GROUP_ID, '混合分组', 'composite')])
    const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true } } })
    await flushPromises()
    await (wrapper.vm as unknown as { reload: () => Promise<void> }).reload()
    expect(mocks.getAllGroups).toHaveBeenCalledTimes(2)
    expect(mocks.getGroupState).not.toHaveBeenCalled()
  })
})

// ---------------------------------------------------------------------------
// 页顶“刷新”：真正刷新上游目录
// ---------------------------------------------------------------------------

type PanelVm = { refreshCatalogs: () => Promise<void>; refreshing: boolean }

function refreshResult(items: Array<Partial<CodexCatalogRefreshItem> & { account_id: number }>, state = groupState({ revision: 8 }), reused = false) {
  const full: CodexCatalogRefreshItem[] = items.map((item) => ({
    account_name: `账号-${item.account_id}`,
    platform: 'commandcode',
    kind: 'model_protocol',
    ok: true,
    elapsed_ms: 120,
    ...item
  }))
  return {
    refresh: { group_id: GROUP_ID, refreshed_at: '2026-10-24T00:00:00Z', reused, total: full.length, failed: full.filter((i) => !i.ok).length, items: full },
    state
  }
}

describe('CodexModelVisibilityPanel 暴露的 refreshCatalogs', () => {
  it('调用刷新接口，用返回的最新状态替换列表，全部成功给一条简短提示', async () => {
    const wrapper = await mountPanel()
    const fresh = groupState({
      revision: 8,
      candidates: [candidate({ slug: 'deepseek-flash', target_platform: 'commandcode', protocol: 'responses_native', fingerprint: 'fp-new' })]
    })
    mocks.refreshCatalogs.mockResolvedValue(refreshResult([{ account_id: 11 }], fresh))

    await (wrapper.vm as unknown as PanelVm).refreshCatalogs()
    await flushPromises()

    expect(mocks.refreshCatalogs).toHaveBeenCalledWith(GROUP_ID)
    expect(mocks.getAllGroups).toHaveBeenCalledTimes(2)
    // 列表被替换：只剩返回里的那一行，且不需要再调读取接口。
    expect(wrapper.find('[data-testid="codex-switch-deepseek-flash"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-switch-kimi-k3"]').exists()).toBe(false)
    expect(mocks.getGroupState).toHaveBeenCalledTimes(1)
    expect(mocks.showSuccess).toHaveBeenCalledWith('admin.codexThirdPartyModels.refresh.success:{"count":1}')
    expect(wrapper.find('[data-testid="codex-refresh-warning"]').exists()).toBe(false)
    // 之后的预览使用刷新后的 revision。
    mocks.previewChanges.mockResolvedValue({ revision: 8, evaluation: fresh.evaluation })
    mocks.saveChanges.mockResolvedValue({ config: { schema_version: 1, revision: 9, models: {} } })
    await sw(wrapper, 'deepseek-flash').trigger('click')
    await flushPromises()
    expect(mocks.previewChanges).toHaveBeenLastCalledWith(GROUP_ID, 8, [{ slug: 'deepseek-flash', enabled: true }])
  })

  it('进行中 refreshing 为 true，重复调用被忽略', async () => {
    const wrapper = await mountPanel()
    let release: (value: ReturnType<typeof refreshResult>) => void = () => {}
    mocks.refreshCatalogs.mockReturnValue(new Promise((resolve) => (release = resolve)))
    const vm = wrapper.vm as unknown as PanelVm

    const first = vm.refreshCatalogs()
    await flushPromises()
    expect(vm.refreshing).toBe(true)
    await vm.refreshCatalogs()
    expect(mocks.refreshCatalogs).toHaveBeenCalledTimes(1)

    release(refreshResult([{ account_id: 11 }]))
    await first
    await flushPromises()
    expect(vm.refreshing).toBe(false)
  })

  it('有账号刷新失败时用警告条列出账号名与原因', async () => {
    const wrapper = await mountPanel()
    mocks.refreshCatalogs.mockResolvedValue(
      refreshResult([
        { account_id: 11, account_name: 'CommandCode', ok: true },
        { account_id: 12, account_name: 'CommandCode-备用', ok: false, reason: '拉取超时' },
        { account_id: 20, account_name: '官方 ChatGPT', kind: 'official_base', ok: false, reason: '上游返回 HTTP 401' }
      ])
    )
    await (wrapper.vm as unknown as PanelVm).refreshCatalogs()
    await flushPromises()

    const warning = wrapper.get('[data-testid="codex-refresh-warning"]')
    const items = warning.findAll('li').map((li) => li.text())
    expect(items).toHaveLength(2)
    expect(items[0]).toContain('CommandCode-备用')
    expect(items[0]).toContain('拉取超时')
    expect(items[1]).toContain('官方 ChatGPT')
    expect(items[1]).toContain('上游返回 HTTP 401')
    expect(mocks.showWarning).toHaveBeenCalled()
    expect(mocks.showSuccess).not.toHaveBeenCalled()
  })

  it('短时间重复刷新复用上次结果时给出对应提示', async () => {
    const wrapper = await mountPanel()
    mocks.refreshCatalogs.mockResolvedValue(refreshResult([{ account_id: 11 }], groupState({ revision: 8 }), true))
    await (wrapper.vm as unknown as PanelVm).refreshCatalogs()
    await flushPromises()
    expect(mocks.showSuccess).toHaveBeenCalledWith('admin.codexThirdPartyModels.refresh.reused:{"count":1}')
  })

  it('刷新接口失败时回显原因，保留原有列表，并结束进行中状态', async () => {
    const wrapper = await mountPanel()
    mocks.refreshCatalogs.mockRejectedValue({ message: '网络错误' })
    const vm = wrapper.vm as unknown as PanelVm
    await vm.refreshCatalogs()
    await flushPromises()
    expect(wrapper.find('[data-testid="codex-error"]').exists()).toBe(true)
    expect(mocks.showError).toHaveBeenCalled()
    expect(wrapper.find('[data-testid="codex-switch-kimi-k3"]').exists()).toBe(true)
    expect(vm.refreshing).toBe(false)
  })

  it('刷新期间切换了分组：丢弃这份结果', async () => {
    const wrapper = await mountPanel()
    let release: (value: ReturnType<typeof refreshResult>) => void = () => {}
    mocks.refreshCatalogs.mockReturnValue(new Promise((resolve) => (release = resolve)))
    const pending = (wrapper.vm as unknown as PanelVm).refreshCatalogs()
    await flushPromises()
    mocks.getGroupState.mockResolvedValue(groupState({ revision: 1, candidates: [candidate({ slug: 'only-in-group-2' })] }))
    await wrapper.get('[data-testid="codex-group-select"]').setValue('2')
    await flushPromises()
    release(refreshResult([{ account_id: 11, ok: false, reason: '拉取超时' }]))
    await pending
    await flushPromises()
    expect(wrapper.find('[data-testid="codex-refresh-warning"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-switch-only-in-group-2"]').exists()).toBe(true)
  })

  it('没有选中分组时只刷新分组列表，不调用刷新接口', async () => {
    mocks.getAllGroups.mockResolvedValue([group(GROUP_ID, '混合分组', 'composite')])
    const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true } } })
    await flushPromises()
    await (wrapper.vm as unknown as PanelVm).refreshCatalogs()
    expect(mocks.getAllGroups).toHaveBeenCalledTimes(2)
    expect(mocks.refreshCatalogs).not.toHaveBeenCalled()
  })

  it('刷新相关文案中英文都存在', () => {
    for (const locale of [zhLocale, enLocale]) {
      const refresh = (locale as any).admin.codexThirdPartyModels.refresh
      for (const key of ['success', 'reused', 'failed', 'warningTitle', 'unknownReason']) expect(typeof refresh[key]).toBe('string')
      expect(typeof refresh.kind.model_protocol).toBe('string')
      expect(typeof refresh.kind.official_base).toBe('string')
    }
  })
})

// ---------------------------------------------------------------------------
// 关联到已有档案（型号别名）
// ---------------------------------------------------------------------------

// BaseDialog 经 Teleport 渲染到 body，这里换成就地渲染的桩，便于在面板里直接查询。
const BaseDialogStub = {
  props: ['show', 'title'],
  template: '<div v-if="show" data-testid="base-dialog-stub"><h3>{{ title }}</h3><slot /><slot name="footer" /></div>'
}

const ALIAS_PROFILES = [
  { id: 'deepseek-flash', upstream_models: ['deepseek-v4.1-flash', 'deepseek-flash'], protocols: ['responses_native'] },
  { id: 'kimi-k3', upstream_models: ['kimi-k3'], protocols: ['responses_native'] }
]

function aliasList(aliases: Array<Record<string, unknown>> = []) {
  return { revision: 1, aliases, profiles: ALIAS_PROFILES }
}

function missingProfileState(revision = 3) {
  return groupState({
    revision,
    candidates: [
      candidate({
        slug: 'deepseek-flash',
        can_enable: false,
        status: 'needs_capability',
        target_platform: 'custom',
        upstream_model: 'acme-ds:prod',
        profile_missing_model: 'acme-ds:prod',
        reasons: ['上游型号 acme-ds:prod 暂无能力档案（待补能力）']
      }),
      candidate({ slug: 'glm-5.3', can_enable: false, status: 'needs_capability', reasons: ['缺少上下文窗口依据'] })
    ]
  })
}

async function mountAliasPanel(state = missingProfileState()) {
  mocks.getAllGroups.mockResolvedValue([group(GROUP_ID, '混合分组', 'composite')])
  mocks.getGroupState.mockResolvedValue(state)
  const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true, BaseDialog: BaseDialogStub } } })
  await flushPromises()
  await wrapper.get('[data-testid="codex-group-select"]').setValue(String(GROUP_ID))
  await flushPromises()
  return wrapper
}

describe('CodexModelVisibilityPanel 关联到已有档案', () => {
  it('只有因上游型号缺能力档案而无法发布的条目，在原因下方显示“关联到已有档案”', async () => {
    const wrapper = await mountAliasPanel()
    const link = wrapper.get('[data-testid="codex-link-profile-deepseek-flash"]')
    expect(link.text()).toBe('admin.codexThirdPartyModels.profileAlias.linkAction')
    // 其它原因的待补能力条目没有这个入口。
    expect(wrapper.find('[data-testid="codex-link-profile-glm-5.3"]').exists()).toBe(false)
    // 入口紧跟在原因后面。
    const reason = wrapper.get('[data-testid="codex-reason-deepseek-flash"]')
    expect(reason.element.nextElementSibling).toBe(link.element)
  })

  it('点开弹窗：上游型号只读、档案下拉列出各档案及其已认得的型号、带确认提示', async () => {
    mocks.listProfileAliases.mockResolvedValue(aliasList())
    const wrapper = await mountAliasPanel()
    await wrapper.get('[data-testid="codex-link-profile-deepseek-flash"]').trigger('click')
    await flushPromises()

    const model = wrapper.get('[data-testid="codex-alias-model"]')
    expect((model.element as HTMLInputElement).value).toBe('acme-ds:prod')
    expect(model.attributes('readonly')).toBeDefined()
    const options = wrapper.findAll('[data-testid="codex-alias-profile"] option').map((option) => option.text())
    expect(options).toContain('deepseek-flash')
    expect(options).toContain('kimi-k3')
    await wrapper.get('[data-testid="codex-alias-profile"]').setValue('deepseek-flash')
    expect(wrapper.get('[data-testid="codex-alias-profile-models"]').text()).toContain('deepseek-v4.1-flash')
    expect(wrapper.get('[data-testid="codex-alias-hint"]').text()).toBe('admin.codexThirdPartyModels.profileAlias.confirmHint')
    expect(wrapper.get('[data-testid="codex-alias-confirm"]').attributes('disabled')).toBeUndefined()
  })

  it('没选档案时不能确认；确认后保存并自动重新读取该分组目录，条目变为可配置', async () => {
    mocks.listProfileAliases.mockResolvedValue(aliasList())
    mocks.setProfileAlias.mockResolvedValue(aliasList([{ upstream_model: 'acme-ds:prod', key: 'acme-ds', profile_id: 'deepseek-flash', valid: true, updated_by: 'user:1', updated_at: '2026-10-24T00:00:00Z' }]))
    const wrapper = await mountAliasPanel()
    await wrapper.get('[data-testid="codex-link-profile-deepseek-flash"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="codex-alias-confirm"]').attributes('disabled')).toBeDefined()

    mocks.getGroupState.mockResolvedValue(
      groupState({ revision: 3, candidates: [candidate({ slug: 'deepseek-flash', target_platform: 'custom', upstream_model: 'acme-ds:prod', protocol: 'responses_native', fingerprint: 'fp-linked' })] })
    )
    const readsBefore = mocks.getGroupState.mock.calls.length
    await wrapper.get('[data-testid="codex-alias-profile"]').setValue('deepseek-flash')
    await wrapper.get('[data-testid="codex-alias-confirm"]').trigger('click')
    await flushPromises()

    expect(mocks.setProfileAlias).toHaveBeenCalledWith('acme-ds:prod', 'deepseek-flash')
    expect(mocks.getGroupState.mock.calls.length).toBe(readsBefore + 1)
    expect(wrapper.find('[data-testid="base-dialog-stub"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-link-profile-deepseek-flash"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="codex-status-deepseek-flash"]').text()).toBe('admin.codexThirdPartyModels.status.configurable')
    expect(mocks.showSuccess).toHaveBeenCalledWith('admin.codexThirdPartyModels.profileAlias.saveSuccess')
  })

  it('关联失败时在弹窗里回显原因，不关闭、不重新读取', async () => {
    mocks.listProfileAliases.mockResolvedValue(aliasList())
    mocks.setProfileAlias.mockRejectedValue({ message: '该上游型号已被内置能力档案直接认出，无需关联' })
    const wrapper = await mountAliasPanel()
    await wrapper.get('[data-testid="codex-link-profile-deepseek-flash"]').trigger('click')
    await flushPromises()
    const readsBefore = mocks.getGroupState.mock.calls.length
    await wrapper.get('[data-testid="codex-alias-profile"]').setValue('kimi-k3')
    await wrapper.get('[data-testid="codex-alias-confirm"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="codex-alias-error"]').text()).toContain('内置能力档案')
    expect(wrapper.find('[data-testid="base-dialog-stub"]').exists()).toBe(true)
    expect(mocks.getGroupState.mock.calls.length).toBe(readsBefore)
  })

  it('工具栏“档案别名”按钮打开列表，可解除关联并重新读取目录；失效别名标出', async () => {
    const linked = { upstream_model: 'acme-ds:prod', key: 'acme-ds', profile_id: 'deepseek-flash', valid: true, updated_by: 'user:1', updated_at: '2026-10-24T00:00:00Z' }
    const stale = { upstream_model: 'old-model', key: 'old-model', profile_id: 'gone-profile', valid: false, updated_by: 'user:2', updated_at: '2026-10-20T00:00:00Z' }
    mocks.listProfileAliases.mockResolvedValue(aliasList([linked, stale]))
    mocks.deleteProfileAlias.mockResolvedValue(aliasList([stale]))
    const wrapper = await mountAliasPanel()
    await wrapper.get('[data-testid="codex-alias-list-button"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="codex-alias-row-acme-ds"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-alias-invalid-acme-ds"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="codex-alias-invalid-old-model"]').text()).toBe('admin.codexThirdPartyModels.profileAlias.invalid')

    const readsBefore = mocks.getGroupState.mock.calls.length
    await wrapper.get('[data-testid="codex-alias-unlink-acme-ds"]').trigger('click')
    await flushPromises()
    expect(mocks.deleteProfileAlias).toHaveBeenCalledWith('acme-ds:prod')
    expect(wrapper.find('[data-testid="codex-alias-row-acme-ds"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-alias-row-old-model"]').exists()).toBe(true)
    expect(mocks.getGroupState.mock.calls.length).toBe(readsBefore + 1)

    await wrapper.get('[data-testid="codex-alias-close"]').trigger('click')
    expect(wrapper.find('[data-testid="base-dialog-stub"]').exists()).toBe(false)
  })

  it('没有任何别名时列表给出空提示', async () => {
    mocks.listProfileAliases.mockResolvedValue(aliasList())
    const wrapper = await mountAliasPanel()
    await wrapper.get('[data-testid="codex-alias-list-button"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="codex-alias-empty"]').text()).toBe('admin.codexThirdPartyModels.profileAlias.empty')
  })

  it('空提示的中英文都说明可在此或该条目下关联', () => {
    expect((zhLocale as any).admin.codexThirdPartyModels.profileAlias.empty).toContain('暂无能力档案')
    expect((zhLocale as any).admin.codexThirdPartyModels.profileAlias.empty).toContain('可在此或该条目下关联到已有档案')
    expect((enLocale as any).admin.codexThirdPartyModels.profileAlias.empty).toContain('here or under that entry')
  })

  describe('档案别名窗口里的“新增关联”', () => {
    // 当前分组没有“暂无能力档案”的条目时，也能从工具栏进入窗口手动新增。
    async function openList(aliases: Array<Record<string, unknown>> = []) {
      mocks.listProfileAliases.mockResolvedValue(aliasList(aliases))
      const wrapper = await mountAliasPanel(groupState({ revision: 3 }))
      await wrapper.get('[data-testid="codex-alias-list-button"]').trigger('click')
      await flushPromises()
      return wrapper
    }

    it('输入上游型号（自由文本）、选择档案并显示其已认得的型号、带确认提示；未填完整不能保存', async () => {
      const wrapper = await openList()
      expect(wrapper.find('[data-testid="codex-link-profile-deepseek-flash"]').exists()).toBe(false)
      const confirm = () => wrapper.get('[data-testid="codex-alias-new-confirm"]')
      expect(confirm().attributes('disabled')).toBeDefined()

      const model = wrapper.get('[data-testid="codex-alias-new-model"]')
      expect(model.attributes('readonly')).toBeUndefined()
      await model.setValue('  my-company-ds:prod  ')
      expect(confirm().attributes('disabled')).toBeDefined()
      const options = wrapper.findAll('[data-testid="codex-alias-new-profile"] option').map((option) => option.text())
      expect(options).toContain('deepseek-flash')
      expect(options).toContain('kimi-k3')
      await wrapper.get('[data-testid="codex-alias-new-profile"]').setValue('deepseek-flash')
      expect(wrapper.get('[data-testid="codex-alias-new-profile-models"]').text()).toContain('deepseek-v4.1-flash')
      expect(wrapper.get('[data-testid="codex-alias-new-hint"]').text()).toBe('admin.codexThirdPartyModels.profileAlias.confirmHint')
      expect(confirm().attributes('disabled')).toBeUndefined()

      // 型号只有空白时同样不能保存。
      await model.setValue('   ')
      expect(confirm().attributes('disabled')).toBeDefined()
    })

    it('保存走 PUT，成功后刷新别名列表与当前分组目录，窗口保持打开并清空表单', async () => {
      const wrapper = await openList()
      const saved = { upstream_model: 'my-company-ds:prod', key: 'my-company-ds', profile_id: 'deepseek-flash', valid: true, updated_by: 'user:1', updated_at: '2026-10-24T00:00:00Z' }
      mocks.setProfileAlias.mockResolvedValue(aliasList([saved]))
      const readsBefore = mocks.getGroupState.mock.calls.length

      await wrapper.get('[data-testid="codex-alias-new-model"]').setValue('  my-company-ds:prod ')
      await wrapper.get('[data-testid="codex-alias-new-profile"]').setValue('deepseek-flash')
      await wrapper.get('[data-testid="codex-alias-new-confirm"]').trigger('click')
      await flushPromises()

      expect(mocks.setProfileAlias).toHaveBeenCalledWith('my-company-ds:prod', 'deepseek-flash')
      expect(mocks.getGroupState.mock.calls.length).toBe(readsBefore + 1)
      expect(wrapper.find('[data-testid="base-dialog-stub"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="codex-alias-row-my-company-ds"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="codex-alias-empty"]').exists()).toBe(false)
      expect((wrapper.get('[data-testid="codex-alias-new-model"]').element as HTMLInputElement).value).toBe('')
      expect((wrapper.get('[data-testid="codex-alias-new-profile"]').element as HTMLSelectElement).value).toBe('')
      expect(mocks.showSuccess).toHaveBeenCalledWith('admin.codexThirdPartyModels.profileAlias.saveSuccess')
    })

    it.each([
      ['型号已被内置档案认出', '该上游型号已被内置能力档案直接认出，无需关联'],
      ['档案不存在', '要关联的能力档案不存在'],
      ['格式非法', '型号别名请求不合法：上游型号含有不允许的字符']
    ])('错误就地显示：%s', async (_name, message) => {
      const wrapper = await openList()
      mocks.setProfileAlias.mockRejectedValue({ message })
      const readsBefore = mocks.getGroupState.mock.calls.length
      await wrapper.get('[data-testid="codex-alias-new-model"]').setValue('deepseek-v4.1-flash')
      await wrapper.get('[data-testid="codex-alias-new-profile"]').setValue('kimi-k3')
      await wrapper.get('[data-testid="codex-alias-new-confirm"]').trigger('click')
      await flushPromises()

      expect(wrapper.get('[data-testid="codex-alias-error"]').text()).toContain(message)
      expect(wrapper.find('[data-testid="base-dialog-stub"]').exists()).toBe(true)
      expect(mocks.getGroupState.mock.calls.length).toBe(readsBefore)
      // 输入保留，便于修改后重试。
      expect((wrapper.get('[data-testid="codex-alias-new-model"]').element as HTMLInputElement).value).toBe('deepseek-v4.1-flash')
      expect(wrapper.get('[data-testid="codex-alias-new-confirm"]').attributes('disabled')).toBeUndefined()
    })

    it('窗口为空时仍显示新增表单和说明', async () => {
      const wrapper = await openList()
      expect(wrapper.find('[data-testid="codex-alias-add"]').exists()).toBe(true)
      expect(wrapper.find('[data-testid="codex-alias-empty"]').exists()).toBe(true)
    })
  })

  it('型号别名文案中英文都存在', () => {
    for (const locale of [zhLocale, enLocale]) {
      const alias = (locale as any).admin.codexThirdPartyModels.profileAlias
      for (const key of ['toolbarButton', 'linkAction', 'linkTitle', 'listTitle', 'upstreamModel', 'profile', 'profilePlaceholder', 'profileKnows', 'addTitle', 'addModelPlaceholder', 'confirmHint', 'confirm', 'unlink', 'invalid', 'empty', 'updatedBy', 'updatedAt', 'close', 'loadFailed', 'saveFailed', 'saveSuccess', 'deleteFailed', 'deleteSuccess']) {
        expect(typeof alias[key]).toBe('string')
      }
    }
  })
})

// ---------------------------------------------------------------------------
// 切换分组
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 切换分组', () => {
  it('切换分组会清掉旧分组的选择与反馈，并忽略过期响应', async () => {
    mocks.getAllGroups.mockResolvedValue([group(GROUP_ID, '混合分组', 'composite'), group(2, 'OpenAI 分组', 'openai')])
    let resolveFirst: (value: CodexThirdPartyGroupState) => void = () => {}
    mocks.getGroupState.mockImplementation((id: number) =>
      id === GROUP_ID
        ? new Promise<CodexThirdPartyGroupState>((resolve) => { resolveFirst = resolve })
        : Promise.resolve({ ...groupState({ candidates: [candidate({ slug: 'only-in-group-2' })] }), group_id: 2 }))
    const wrapper = mount(CodexModelVisibilityPanel, { global: { stubs: { Icon: true } } })
    await flushPromises()

    await wrapper.get('[data-testid="codex-group-select"]').setValue(String(GROUP_ID))
    await wrapper.get('[data-testid="codex-group-select"]').setValue('2')
    await flushPromises()
    resolveFirst(groupState())
    await flushPromises()

    expect(wrapper.find('[data-testid="codex-row-only-in-group-2"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-row-kimi-k3"]').exists()).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// 文案
// ---------------------------------------------------------------------------

describe('Codex 三方模型启用文案', () => {
  const zh = (zhLocale as any).admin.codexThirdPartyModels
  const en = (enLocale as any).admin.codexThirdPartyModels

  it('标签名为“Codex 三方模型启用”', () => {
    expect(zh.tabLabel).toBe('Codex 三方模型启用')
    expect(en.tabLabel).toBeTruthy()
  })

  it('动态取用的状态、协议、筛选和预算文案中英文齐全', () => {
    const statuses = ['native', 'needs_capability', 'configurable', 'verified', 'conflict', 'over_budget', 'unavailable']
    for (const locale of [zh, en]) {
      for (const status of statuses) expect(locale.status[status], status).toBeTruthy()
      for (const protocol of ['responses_native', 'responses_via_chat']) expect(locale.protocol[protocol], protocol).toBeTruthy()
      for (const key of ['all', 'enabled', 'disabled', 'blocked']) expect(locale.filters[key], key).toBeTruthy()
      expect(locale.budget.deferred).toBeTruthy()
      expect(locale.budget.deferredHint).toBeTruthy()
      // 同步候选与来源同步状态已删除，文案不再保留。
      expect(locale.sources).toBeUndefined()
      expect(locale.sync).toBeUndefined()
      expect(locale.actions.syncSources).toBeUndefined()
    }
  })
})
