import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import OpenAIQuotaResetCell from '../OpenAIQuotaResetCell.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import type { Account } from '@/types'
import { refreshOpenAIQuota, resetOpenAIQuota, type OpenAIQuotaRefreshResult } from '@/api/admin/accounts'

vi.mock('@/api/admin/accounts', () => ({
  refreshOpenAIQuota: vi.fn(),
  resetOpenAIQuota: vi.fn(),
  refreshOpenAIReferrals: vi.fn(),
  sendOpenAIReferralInvite: vi.fn(),
}))

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({
      t: (key: string, params?: Record<string, unknown>) =>
        params?.time ? `${key}:${params.time}` : params?.count ? `${key}:${params.count}` : key,
    }),
  }
})

// 缓存水合会丢弃已过期的重置卡，因此缓存类用例必须使用未来时间。
const FUTURE_EXPIRY_EARLY = '2099-07-03T04:05:06Z'
const FUTURE_EXPIRY_LATE = '2099-07-05T04:05:06Z'
const PAST_EXPIRY = '2020-07-03T04:05:06Z'

function makeAccount(overrides: Partial<Account>): Account {
  return {
    id: 1,
    name: 'acc',
    platform: 'openai',
    type: 'oauth',
    proxy_id: null,
    concurrency: 3,
    priority: 50,
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
    session_window_status: null,
    ...overrides,
  }
}

// 第二个按钮(橙色)是 reset 按钮::disabled="resetting||loading||!canReset" :title="resetButtonTitle"
const resetButton = (wrapper: ReturnType<typeof mount>) =>
  wrapper.findAll('button')[1]

beforeEach(() => {
  vi.mocked(refreshOpenAIQuota).mockReset()
  vi.mocked(resetOpenAIQuota).mockReset()
})

describe('OpenAIQuotaResetCell — Codex 点数', () => {
  const points = (wrapper: ReturnType<typeof mount>) => wrapper.get('[data-testid="codex-credits"]')
  // 按钮内只有「点数」文案和一个数值 span；断言取 span 的完整文本，比 toContain 更严，能发现多显示了小数。
  const pointsValue = (wrapper: ReturnType<typeof mount>) => points(wrapper).get('span').text()
  const balance = { has_credits: true, unlimited: false, balance: '12345678901234567890.0123' }
  // 页面只显示整数部分：小数点及其后内容直接舍去，不四舍五入；整数部分超过 2^53 也必须保持完整。
  const balanceInteger = '12345678901234567890'

  // 余额字符串到页面显示的对照表，同时驱动「实时查询」和「缓存回显」两条路径。
  const balanceCases = [
    { name: '示例值 57333.8597510000 舍去小数', balance: '57333.8597510000', expected: '57333' },
    { name: '0.9 舍去后是 0，不四舍五入成 1', balance: '0.9', expected: '0' },
    { name: '1.9999 不进位', balance: '1.9999', expected: '1' },
    { name: '0.0000 显示 0', balance: '0.0000', expected: '0' },
    { name: '没有小数点的整数原样显示', balance: '42', expected: '42' },
    { name: '超长整数部分保持完整', balance: '12345678901234567890.0123', expected: balanceInteger },
    { name: '超过 2^53 的整数不丢精度（Number 会变成 ...992）', balance: '9007199254740993', expected: '9007199254740993' },
    { name: '超长整数部分加超长小数部分', balance: '99999999999999999999999999999999.99999999999999999999', expected: '99999999999999999999999999999999' },
    { name: '前后空白先被去掉', balance: '  57333.85  ', expected: '57333' },
    { name: '整数部分为空的 .9 显示 0', balance: '.9', expected: '0' },
    { name: '小数点后为空的 5. 显示 5', balance: '5.', expected: '5' },
    { name: '带正号的 +7.8 去掉正号', balance: '+7.8', expected: '7' },
    { name: '多余前导零 007.5 规整为 7', balance: '007.5', expected: '7' },
    { name: '负零 -0.0 显示 0（通过非负检查的唯一负号写法）', balance: '-0.0', expected: '0' },
    { name: '负数仍走「可用」', balance: '-1.5', expected: 'admin.accounts.openaiQuotaReset.pointsAvailable' },
    { name: '科学计数法无法按字符串截断，走「可用」', balance: '1.5e3', expected: 'admin.accounts.openaiQuotaReset.pointsAvailable' },
    { name: '十六进制写法走「可用」', balance: '0x10', expected: 'admin.accounts.openaiQuotaReset.pointsAvailable' },
  ]

  // 非金额的状态值必须原样显示，不受取整影响。
  const stateCases = [
    { name: 'zero', credits: { has_credits: false, unlimited: false, balance: '0' }, expected: '0' },
    { name: 'zero with a leftover fraction string', credits: { has_credits: false, unlimited: false, balance: '3.7' }, expected: '0' },
    { name: 'unlimited takes precedence', credits: { has_credits: false, unlimited: true, balance: null }, expected: 'admin.accounts.openaiQuotaReset.pointsUnlimited' },
    { name: 'unlimited ignores a numeric balance', credits: { has_credits: true, unlimited: true, balance: '88.5' }, expected: 'admin.accounts.openaiQuotaReset.pointsUnlimited' },
    { name: 'hidden balance', credits: { has_credits: true, unlimited: false, balance: null }, expected: 'admin.accounts.openaiQuotaReset.pointsAvailable' },
    { name: 'empty balance', credits: { has_credits: true, unlimited: false, balance: '   ' }, expected: 'admin.accounts.openaiQuotaReset.pointsAvailable' },
    { name: 'invalid balance', credits: { has_credits: true, unlimited: false, balance: 'NaN' }, expected: 'admin.accounts.openaiQuotaReset.pointsAvailable' },
    { name: 'absent credits', credits: undefined, expected: '—' },
    { name: 'null credits', credits: null, expected: '—' },
  ]

  it.each([
    ...balanceCases.map(({ name, balance: value, expected }) => ({
      name,
      credits: { has_credits: true, unlimited: false, balance: value },
      expected,
    })),
    ...stateCases,
  ])('queries and displays $name independently of reset cards', async ({ credits, expected }) => {
    vi.mocked(refreshOpenAIQuota).mockResolvedValue({
      credits, fetched_at: 1770000000, cache_persisted: false, credits_cache_persisted: true,
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account: makeAccount({}) } })
    expect(points(wrapper).text()).toContain('—')
    await points(wrapper).trigger('click')
    await flushPromises()
    expect(refreshOpenAIQuota).toHaveBeenCalledWith(1)
    expect(pointsValue(wrapper)).toBe(expected)
    expect(resetButton(wrapper).attributes('disabled')).toBeDefined()
    expect(resetOpenAIQuota).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  // 缓存回显：从账号 extra 里的快照直接水合，不发上游请求；显示规则必须与实时查询一致。
  it.each([
    ...balanceCases.map(({ name, balance: value, expected }) => ({
      name,
      credits: { has_credits: true, unlimited: false, balance: value },
      expected,
    })),
    ...stateCases.filter((item) => item.credits != null),
  ])('rehydrates and displays $name from the cached snapshot', ({ credits, expected }) => {
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account: makeAccount({
      extra: { codex_credits_snapshot: { credits: credits as typeof balance, fetched_at: 1770000000 } },
    }) } })
    expect(pointsValue(wrapper)).toBe(expected)
    expect(refreshOpenAIQuota).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  // 只改页面显示：缓存快照和接口返回里的原始点数字符串必须保持完整精度，不被组件改写。
  it('keeps the raw points string untouched in the cached snapshot and in the query result', async () => {
    const snapshotCredits = { has_credits: true, unlimited: false, balance: '57333.8597510000' }
    const account = makeAccount({
      extra: { codex_credits_snapshot: { credits: snapshotCredits, fetched_at: 1770000000 } },
    })
    const queried = { has_credits: true, unlimited: false, balance: '0.9' }
    const result = { credits: queried, fetched_at: 1770000001, cache_persisted: false, credits_cache_persisted: true }
    vi.mocked(refreshOpenAIQuota).mockResolvedValue(result)
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })
    expect(pointsValue(wrapper)).toBe('57333')
    expect(account.extra?.codex_credits_snapshot?.credits?.balance).toBe('57333.8597510000')

    await points(wrapper).trigger('click')
    await flushPromises()
    expect(pointsValue(wrapper)).toBe('0')
    expect(result.credits.balance).toBe('0.9')
    // 按钮提示里也不应出现带小数的点数
    expect(points(wrapper).attributes('title')).not.toContain('8597510000')
    wrapper.unmount()
  })

  it('rehydrates points with their query timestamp without an upstream request', () => {
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account: makeAccount({
      extra: { codex_credits_snapshot: { credits: balance, fetched_at: 1770000000 } },
    }) } })
    expect(pointsValue(wrapper)).toBe(balanceInteger)
    expect(points(wrapper).attributes('title')).toContain('admin.accounts.openaiQuotaReset.pointsUpdatedAt:')
    expect(refreshOpenAIQuota).not.toHaveBeenCalled()
    expect(resetButton(wrapper).attributes('disabled')).toBeDefined()
    wrapper.unmount()
  })

  it('keeps the last balance on a request failure, but clears it on a successful unknown response', async () => {
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account: makeAccount({
      extra: { codex_credits_snapshot: { credits: balance, fetched_at: 1770000000 } },
    }) } })
    vi.mocked(refreshOpenAIQuota).mockRejectedValueOnce(new Error('network unavailable'))
    await points(wrapper).trigger('click')
    await flushPromises()
    expect(pointsValue(wrapper)).toBe(balanceInteger)
    expect(wrapper.text()).toContain('network unavailable')

    vi.mocked(refreshOpenAIQuota).mockResolvedValueOnce({ fetched_at: 1770000001, cache_persisted: true, credits_cache_persisted: true })
    await points(wrapper).trigger('click')
    await flushPromises()
    expect(points(wrapper).text()).toContain('—')
    expect(wrapper.text()).not.toContain(balanceInteger)
    wrapper.unmount()
  })

  it('reports a points-cache write failure while displaying the live balance', async () => {
    vi.mocked(refreshOpenAIQuota).mockResolvedValue({
      credits: balance, fetched_at: 1770000000, cache_persisted: true, credits_cache_persisted: false,
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account: makeAccount({}) } })
    await points(wrapper).trigger('click')
    await flushPromises()
    expect(pointsValue(wrapper)).toBe(balanceInteger)
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.pointsCachePersistFailed')
    wrapper.unmount()
  })

  it('does not apply a pending response to another account row', async () => {
    let resolve!: (value: OpenAIQuotaRefreshResult) => void
    vi.mocked(refreshOpenAIQuota).mockReturnValue(new Promise((done) => { resolve = done }))
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account: makeAccount({}) } })
    await points(wrapper).trigger('click')
    expect(points(wrapper).attributes('disabled')).toBeDefined()
    await wrapper.setProps({ account: makeAccount({ id: 2 }) })
    resolve({ credits: balance, fetched_at: 1770000000, cache_persisted: true, credits_cache_persisted: true })
    await flushPromises()
    expect(points(wrapper).text()).toContain('—')
    expect(points(wrapper).attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })
})

describe('OpenAIQuotaResetCell — 外审 F6:影子禁用重置', () => {
  it('影子账号(parent_account_id 非空)的 reset 按钮被禁用且提示在母账号重置', () => {
    const account = makeAccount({ parent_account_id: 100 })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    const btn = resetButton(wrapper)
    expect(btn.attributes('disabled')).toBeDefined()
    expect(btn.attributes('title')).toBe('admin.accounts.openaiQuotaReset.resetTooltipShadow')
    wrapper.unmount()
  })

  it('普通账号(无 parent_account_id)未查询时禁用原因是「需先查询」而非影子提示', () => {
    const account = makeAccount({ parent_account_id: null })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    const btn = resetButton(wrapper)
    // 未加载数据时本就 disabled(无次数),但提示语必须是 needQuery,不得是 shadow 提示。
    expect(btn.attributes('title')).toBe('admin.accounts.openaiQuotaReset.resetTooltipNeedQuery')
    wrapper.unmount()
  })

  it('从账号 extra 缓存恢复重置卡次数和到期时间', () => {
    const account = makeAccount({
      parent_account_id: null,
      extra: {
        codex_reset_credit_snapshot: {
          available_count: 2,
          credits: [
            { expires_at: FUTURE_EXPIRY_LATE },
            { expires_at: FUTURE_EXPIRY_EARLY },
          ],
        },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    expect(refreshOpenAIQuota).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.count')
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.expiresAt:')
    expect(wrapper.text()).toContain('+1')
    expect(resetButton(wrapper).attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('缓存中的重置卡全部过期时视为未知,不点亮重置入口', () => {
    const account = makeAccount({
      parent_account_id: null,
      extra: {
        codex_reset_credit_snapshot: {
          available_count: 1,
          credits: [{ expires_at: PAST_EXPIRY }],
        },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    expect(wrapper.text()).not.toContain('admin.accounts.openaiQuotaReset.expiresAt:')
    const btn = resetButton(wrapper)
    expect(btn.attributes('disabled')).toBeDefined()
    expect(btn.attributes('title')).toBe('admin.accounts.openaiQuotaReset.resetTooltipNeedQuery')
    wrapper.unmount()
  })

  it('缓存次数向未过期的明细数量收敛', () => {
    const account = makeAccount({
      parent_account_id: null,
      extra: {
        codex_reset_credit_snapshot: {
          available_count: 3,
          credits: [
            { expires_at: PAST_EXPIRY },
            { expires_at: FUTURE_EXPIRY_EARLY },
          ],
        },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.count1')
    expect(wrapper.text()).not.toContain('+1')
    expect(resetButton(wrapper).attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('查询后默认折叠为最早到期时间,点击 +N 展开完整列表', async () => {
    vi.mocked(refreshOpenAIQuota).mockResolvedValue({
      rate_limit_reset_credits: {
        available_count: 3,
        credits: [
          { expires_at: '2026-07-05T04:05:06Z' },
          { expires_at: '2026-07-03T04:05:06Z' },
          { expires_at: 'not-a-date' },
        ],
      },
      fetched_at: 1770000000,
      cache_persisted: true,
    })

    const account = makeAccount({ parent_account_id: null })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    await wrapper.findAll('button')[0].trigger('click')
    await flushPromises()

    expect(refreshOpenAIQuota).toHaveBeenCalledWith(1)
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.expiresAt:')
    expect(wrapper.text()).toContain('+2')
    expect(wrapper.text()).not.toContain('not-a-date')

    const toggle = wrapper.find('[data-testid="reset-credit-expiry-toggle"]')
    expect(toggle.exists()).toBe(true)
    expect(toggle.attributes('aria-expanded')).toBe('false')
    await toggle.trigger('click')

    expect(toggle.attributes('aria-expanded')).toBe('true')
    expect(wrapper.find('[data-testid="reset-credit-expiry-details"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('not-a-date')
    expect(wrapper.text()).not.toContain('undefined')
    wrapper.unmount()
  })

  it('只有一张重置卡时不显示展开按钮', async () => {
    vi.mocked(refreshOpenAIQuota).mockResolvedValue({
      rate_limit_reset_credits: {
        available_count: 1,
        credits: [
          { expires_at: '2026-07-03T04:05:06Z' },
        ],
      },
      fetched_at: 1770000000,
      cache_persisted: true,
    })

    const account = makeAccount({ parent_account_id: null })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    await wrapper.findAll('button')[0].trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-testid="reset-credit-expiry-toggle"]').exists()).toBe(false)
    expect(wrapper.find('[data-testid="reset-credit-expiry-details"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.expiresAt:')
    wrapper.unmount()
  })

  // 快照写库被拒绝(上游未返回到期明细)不得吞掉这次成功的上游读取,
  // 否则次数永远显示不出来、重置入口被永久禁用。
  it('快照持久化失败时仍显示实时次数并给出警告', async () => {
    vi.mocked(refreshOpenAIQuota).mockResolvedValue({
      rate_limit_reset_credits: { available_count: 2 },
      fetched_at: 1770000000,
      cache_persisted: false,
    })

    const account = makeAccount({ parent_account_id: null })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    await wrapper.findAll('button')[0].trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.count2')
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.refreshCachePersistFailed')
    expect(resetButton(wrapper).attributes('disabled')).toBeUndefined()
    wrapper.unmount()
  })

  it('重置成功后直接使用响应中的最新缓存并回传恢复后的账号', async () => {
    const recoveredAccount = makeAccount({
      parent_account_id: null,
      status: 'active',
      error_message: null,
    })
    vi.mocked(resetOpenAIQuota).mockResolvedValue({
      code: 'success',
      windows_reset: 1,
      cache_refreshed: true,
      account_state_recovered: true,
      quota: {
        credits: { has_credits: true, unlimited: false, balance: '999.25' },
        rate_limit_reset_credits: {
          available_count: 0,
          credits: [],
        },
        fetched_at: 1770000000,
      },
      account: recoveredAccount,
    })
    const account = makeAccount({
      parent_account_id: null,
      extra: {
        codex_reset_credit_snapshot: {
          available_count: 1,
          credits: [{ expires_at: FUTURE_EXPIRY_EARLY }],
        },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    await resetButton(wrapper).trigger('click')
    wrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
    await flushPromises()

    expect(resetOpenAIQuota).toHaveBeenCalledWith(1)
    expect(refreshOpenAIQuota).not.toHaveBeenCalled()
    // 重置响应里的余额 999.25 也只显示整数部分
    expect(wrapper.get('[data-testid="codex-credits"] span').text()).toBe('999')
    expect(wrapper.text()).not.toContain('admin.accounts.openaiQuotaReset.expiresAt:')
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.resetSuccess')
    expect(wrapper.emitted('account-updated')).toEqual([[recoveredAccount]])
    wrapper.unmount()
  })

  // 缓存回读失败不影响「账号状态已恢复」这一主目标:恢复后的账号行必须照常回传,
  // 否则列表会继续显示已经不存在的限流状态。
  it('缓存刷新失败时仍回传恢复后的账号并把次数标为未知', async () => {
    const recoveredAccount = makeAccount({
      parent_account_id: null,
      status: 'active',
      rate_limit_reset_at: null,
    })
    vi.mocked(resetOpenAIQuota).mockResolvedValue({
      code: 'success',
      windows_reset: 1,
      cache_refreshed: false,
      account_state_recovered: true,
      warning_code: 'reset_credit_cache_refresh_failed',
      account: recoveredAccount,
    })
    const account = makeAccount({
      parent_account_id: null,
      extra: {
        codex_reset_credit_snapshot: {
          available_count: 1,
          credits: [{ expires_at: FUTURE_EXPIRY_EARLY }],
        },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    await resetButton(wrapper).trigger('click')
    wrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
    await flushPromises()

    expect(refreshOpenAIQuota).not.toHaveBeenCalled()
    // 次数未知(隐藏)但仍展示已持久化的到期明细,重置入口保持禁用直到重新查询。
    expect(wrapper.text()).not.toContain('admin.accounts.openaiQuotaReset.count1')
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.expiresAt:')
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.resetCacheRefreshFailed')
    expect(resetButton(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.emitted('account-updated')).toEqual([[recoveredAccount]])
    wrapper.unmount()
  })

  it('账号状态恢复失败时停止后续步骤并提示手动恢复', async () => {
    vi.mocked(resetOpenAIQuota).mockResolvedValue({
      code: 'success',
      windows_reset: 1,
      cache_refreshed: false,
      account_state_recovered: false,
      warning_code: 'account_state_recovery_failed',
    })
    const account = makeAccount({
      parent_account_id: null,
      extra: {
        codex_reset_credit_snapshot: {
          available_count: 1,
          credits: [{ expires_at: FUTURE_EXPIRY_EARLY }],
        },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })

    await resetButton(wrapper).trigger('click')
    wrapper.findComponent(ConfirmDialog).vm.$emit('confirm')
    await flushPromises()

    expect(refreshOpenAIQuota).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('admin.accounts.openaiQuotaReset.resetAccountRecoveryFailed')
    expect(resetButton(wrapper).attributes('disabled')).toBeDefined()
    expect(wrapper.emitted('account-updated')).toBeUndefined()
    wrapper.unmount()
  })
})

describe('OpenAIQuotaResetCell 自动用卡运行态', () => {
  it.each([
    ['checking', 'checking'],
    ['available', 'available'],
    ['resetting', 'resetting'],
    ['success', 'success'],
    ['no_credit', 'noCredit'],
    ['failed', 'failed'],
  ] as const)('展示 %s 状态且不需要暴露卡标识', (status, labelKey) => {
    const account = makeAccount({
      extra: {
        auto_reset_credit_enabled: true,
        codex_auto_reset_credit_state: {
          status,
          trigger_window: '5h',
          available_count: 1,
          checked_at: '2099-07-03T04:05:06Z',
          error_code: status === 'failed' ? 'RESET_FAILED' : undefined,
        },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })
    const state = wrapper.get('[data-testid="auto-reset-credit-state"]')
    expect(state.text()).toContain(`admin.accounts.openaiQuotaReset.autoStatus.${labelKey}`)
    expect(state.text()).toContain('5h')
    expect(state.text()).not.toContain('credit_id')
    wrapper.unmount()
  })

  it('开关关闭时不显示历史运行态', () => {
    const account = makeAccount({
      extra: {
        auto_reset_credit_enabled: false,
        codex_auto_reset_credit_state: { status: 'success', available_count: 1 },
      },
    })
    const wrapper = mount(OpenAIQuotaResetCell, { props: { account } })
    expect(wrapper.find('[data-testid="auto-reset-credit-state"]').exists()).toBe(false)
    wrapper.unmount()
  })
})
