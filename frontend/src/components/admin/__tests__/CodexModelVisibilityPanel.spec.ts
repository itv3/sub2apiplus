import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'

import CodexModelVisibilityPanel from '../CodexModelVisibilityPanel.vue'
import zhLocale from '@/i18n/locales/zh'
import enLocale from '@/i18n/locales/en'
import type {
  CodexCatalogBudget,
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
  syncSources: vi.fn(),
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
  syncSources: mocks.syncSources
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
    for (const slug of ['gpt-5.5', 'kimi-k3', 'deepseek-flash', 'glm-5.3']) {
      expect(wrapper.find(`[data-testid="codex-row-${slug}"]`).exists()).toBe(true)
    }
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
    expect(wrapper.get('[data-testid="codex-status-gpt-5.5"]').text()).toBe('admin.codexThirdPartyModels.status.native')
  })

  it('原生 GPT 行只读：开关禁用、没有勾选框', async () => {
    const wrapper = await mountPanel()
    expect(sw(wrapper, 'gpt-5.5').attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-testid="codex-select-gpt-5.5"]').exists()).toBe(false)
    await sw(wrapper, 'gpt-5.5').trigger('click')
    expect(mocks.previewChanges).not.toHaveBeenCalled()
    expect(mocks.saveChanges).not.toHaveBeenCalled()
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
  it('展示原生数量、已开启第三方数量、最坏字节数和剩余空间', async () => {
    const wrapper = await mountPanel(groupState({
      budget: { native_count: 6, enabled_third_party_count: 2, worst_case_bytes: 642453, remaining_bytes: 406123 }
    }))
    expect(wrapper.get('[data-testid="codex-budget-native"]').text()).toBe('6')
    expect(wrapper.get('[data-testid="codex-budget-enabled"]').text()).toBe('2')
    expect(wrapper.get('[data-testid="codex-budget-worst"]').text()).toContain('642,453')
    expect(wrapper.get('[data-testid="codex-budget-remaining"]').text()).toContain('406,123')
    expect(wrapper.find('[data-testid="codex-budget-within"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-budget-incomplete"]').exists()).toBe(false)
  })

  it('预算未完成时显示“未完成检查”，不得显示通过', async () => {
    const wrapper = await mountPanel(groupState({ budget: { complete: false, reason: '基础目录账号不可得' } }))
    expect(wrapper.get('[data-testid="codex-budget-incomplete"]').text()).toBe('admin.codexThirdPartyModels.budget.incomplete')
    expect(wrapper.get('[data-testid="codex-budget-reason"]').text()).toBe('基础目录账号不可得')
    expect(wrapper.find('[data-testid="codex-budget-within"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(false)
  })

  it('最坏字节数超过上限时显示超限', async () => {
    const wrapper = await mountPanel(groupState({ budget: { worst_case_bytes: 1048577, remaining_bytes: -1 } }))
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-budget-within"]').exists()).toBe(false)
  })

  it('恰好等于上限不算超限', async () => {
    const wrapper = await mountPanel(groupState({ budget: { worst_case_bytes: 1048576, remaining_bytes: 0 } }))
    expect(wrapper.find('[data-testid="codex-budget-over"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-budget-within"]').exists()).toBe(true)
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

  it('按状态筛选', async () => {
    const wrapper = await mountPanel(groupState({
      candidates: defaultCandidates().map((item) => (item.slug === 'deepseek-flash' ? { ...item, enabled: true } : item))
    }))
    await wrapper.get('[data-testid="codex-status-filter"]').setValue('needs_capability')
    expect(wrapper.findAll('tbody tr[data-testid^="codex-row-"]').map((row) => row.attributes('data-testid')))
      .toEqual(['codex-row-glm-5.3'])

    await wrapper.get('[data-testid="codex-status-filter"]').setValue('enabled')
    expect(wrapper.findAll('tbody tr[data-testid^="codex-row-"]').map((row) => row.attributes('data-testid')))
      .toEqual(['codex-row-deepseek-flash'])

    await wrapper.get('[data-testid="codex-status-filter"]').setValue('disabled')
    expect(wrapper.find('[data-testid="codex-row-gpt-5.5"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-row-kimi-k3"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-row-deepseek-flash"]').exists()).toBe(false)
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
// 源同步
// ---------------------------------------------------------------------------

describe('CodexModelVisibilityPanel 源同步', () => {
  it('展示各账号的源同步状态与最近错误', async () => {
    const wrapper = await mountPanel(groupState({
      sources: [
        source({ account_id: 101, account_name: 'kimi-主账号', synced_at: '2026-10-23T01:00:00Z', model_count: 12 }),
        source({ account_id: 102, account_name: '智谱账号', status: 'failed', stale: true, last_error: 'Upstream model list request failed with HTTP 502', model_count: 5 }),
        source({ account_id: 103, account_name: '新账号', status: 'missing', stale: true, fingerprint_matches: false, model_count: 0 }),
        source({ account_id: 104, account_name: '刷新中账号', status: 'missing', stale: true, refreshing: true, model_count: 0 })
      ]
    }))
    expect(wrapper.get('[data-testid="codex-source-101"]').text()).toContain('admin.codexThirdPartyModels.sources.status.complete')
    expect(wrapper.get('[data-testid="codex-source-101"]').text()).toContain('"count":12')
    expect(wrapper.get('[data-testid="codex-source-102"]').text()).toContain('admin.codexThirdPartyModels.sources.status.failed')
    expect(wrapper.get('[data-testid="codex-source-error-102"]').text()).toBe('Upstream model list request failed with HTTP 502')
    expect(wrapper.get('[data-testid="codex-source-103"]').text()).toContain('admin.codexThirdPartyModels.sources.status.missing')
    expect(wrapper.get('[data-testid="codex-source-104"]').text()).toContain('admin.codexThirdPartyModels.sources.status.refreshing')
  })

  it('只有曾经成功同步过、之后账号配置又变了的源才提示“配置已变更”', async () => {
    const wrapper = await mountPanel(groupState({
      sources: [
        source({ account_id: 101, synced_at: '2026-10-23T01:00:00Z', fingerprint_matches: false }),
        source({ account_id: 102, synced_at: '2026-10-23T01:00:00Z', fingerprint_matches: true }),
        // 从未成功过的失败快照没有同步时间，也谈不上“配置变更”。
        source({ account_id: 103, status: 'failed', stale: true, fingerprint_matches: false, model_count: 0, last_error: '同步失败' })
      ]
    }))
    expect(wrapper.find('[data-testid="codex-source-changed-101"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="codex-source-changed-102"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="codex-source-changed-103"]').exists()).toBe(false)
  })

  it('点击“同步候选”触发后台同步并更新来源状态', async () => {
    const wrapper = await mountPanel(groupState({ sources: [source({ account_id: 101 })] }))
    mocks.syncSources.mockResolvedValue({ sources: [source({ account_id: 101, refreshing: true })] })

    await wrapper.get('[data-testid="codex-sync-button"]').trigger('click')
    await flushPromises()

    expect(mocks.syncSources).toHaveBeenCalledWith(GROUP_ID)
    expect(mocks.showSuccess).toHaveBeenCalledWith('admin.codexThirdPartyModels.sync.started')
    expect(wrapper.get('[data-testid="codex-source-101"]').text()).toContain('admin.codexThirdPartyModels.sources.status.refreshing')
    expect(wrapper.get('[data-testid="codex-sync-button"]').text()).toContain('admin.codexThirdPartyModels.actions.syncing')
  })

  it('同步失败时回显原因', async () => {
    const wrapper = await mountPanel()
    mocks.syncSources.mockRejectedValue({ status: 500, message: '无法启动同步' })
    await wrapper.get('[data-testid="codex-sync-button"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="codex-error"]').text()).toContain('无法启动同步')
  })

  it('有账号正在刷新时定时重新读取，直到刷新结束', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const refreshingState = groupState({ sources: [source({ account_id: 101, refreshing: true })] })
    const doneState = groupState({ sources: [source({ account_id: 101, refreshing: false })] })
    const wrapper = await mountPanel(refreshingState)
    expect(mocks.getGroupState).toHaveBeenCalledTimes(1)

    mocks.getGroupState.mockResolvedValue(doneState)
    await vi.advanceTimersByTimeAsync(3000)
    await flushPromises()
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
    expect(wrapper.get('[data-testid="codex-source-101"]').text()).toContain('admin.codexThirdPartyModels.sources.status.complete')

    // 已经没有刷新中的账号，不再继续轮询。
    await vi.advanceTimersByTimeAsync(10000)
    await flushPromises()
    expect(mocks.getGroupState).toHaveBeenCalledTimes(2)
  })

  it('卸载组件后不再轮询', async () => {
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    const wrapper = await mountPanel(groupState({ sources: [source({ account_id: 101, refreshing: true })] }))
    wrapper.unmount()
    await vi.advanceTimersByTimeAsync(10000)
    expect(mocks.getGroupState).toHaveBeenCalledTimes(1)
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

  it('动态取用的状态、协议和来源状态文案中英文齐全', () => {
    const statuses = ['native', 'needs_capability', 'configurable', 'verified', 'conflict', 'over_budget', 'unavailable']
    for (const locale of [zh, en]) {
      for (const status of statuses) expect(locale.status[status], status).toBeTruthy()
      for (const protocol of ['responses_native', 'responses_via_chat']) expect(locale.protocol[protocol], protocol).toBeTruthy()
      for (const key of ['missing', 'complete', 'empty', 'failed', 'refreshing']) expect(locale.sources.status[key], key).toBeTruthy()
    }
  })
})
