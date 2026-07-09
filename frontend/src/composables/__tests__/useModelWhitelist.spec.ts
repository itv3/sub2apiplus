import { beforeAll, describe, expect, it, vi } from 'vitest'

const antigravityOfficialModels = vi.hoisted(() => [
  { id: 'gemini-3.5-flash-extra-low', display_name: 'Gemini 3.5 Flash Low', model_enum: 'MODEL_PLACEHOLDER_M187', thinking_budget: 1000, created_at: '2026-06-29T00:00:00Z', is_reasoning: true },
  { id: 'gemini-3.5-flash-low', display_name: 'Gemini 3.5 Flash Medium', model_enum: 'MODEL_PLACEHOLDER_M20', thinking_budget: 4000, created_at: '2026-06-29T00:00:00Z', is_reasoning: true },
  { id: 'gemini-3-flash-agent', display_name: 'Gemini 3.5 Flash High', model_enum: 'MODEL_PLACEHOLDER_M132', thinking_budget: 10000, created_at: '2026-06-29T00:00:00Z', is_reasoning: true },
  { id: 'gemini-3.1-pro-low', display_name: 'Gemini 3.1 Pro Low', model_enum: 'MODEL_PLACEHOLDER_M36', thinking_budget: 1001, created_at: '2026-02-19T00:00:00Z', is_reasoning: true },
  { id: 'gemini-pro-agent', display_name: 'Gemini 3.1 Pro High', model_enum: 'MODEL_PLACEHOLDER_M16', thinking_budget: 10001, created_at: '2026-02-19T00:00:00Z', is_reasoning: true },
  { id: 'claude-sonnet-4-6', display_name: 'Claude Sonnet 4.6', model_enum: 'MODEL_PLACEHOLDER_M35', thinking_budget: 1024, created_at: '2026-02-17T00:00:00Z', is_reasoning: true },
  { id: 'claude-opus-4-6-thinking', display_name: 'Claude Opus 4.6 Thinking', model_enum: 'MODEL_PLACEHOLDER_M26', thinking_budget: 1024, created_at: '2026-02-05T00:00:00Z', is_reasoning: true },
  { id: 'gpt-oss-120b-medium', display_name: 'GPT-OSS 120B Medium', model_enum: 'MODEL_OPENAI_GPT_OSS_120B_MEDIUM', thinking_budget: 8192, created_at: '2026-06-29T00:00:00Z', is_reasoning: true }
])

vi.mock('@/api/admin/accounts', () => ({
  getAntigravityDefaultModelMapping: vi.fn(),
  getAntigravityOfficialModels: vi.fn(async () => ({
    models: antigravityOfficialModels,
    mapping: Object.fromEntries(antigravityOfficialModels.map((model) => [model.id, model.id]))
  }))
}))

import {
  allModels,
  buildModelMappingObject,
  fetchAntigravityOfficialModels,
  getModelsByPlatform,
  getOfficialAntigravityDisplayMappings,
  getOfficialAntigravityModelIDs,
  splitModelMappingObject
} from '../useModelWhitelist'

describe('useModelWhitelist', () => {
  beforeAll(async () => {
    await fetchAntigravityOfficialModels()
  })

  it('openai 模型列表包含 GPT-5.4 官方快照', () => {
    const models = getModelsByPlatform('openai')

    expect(models).toContain('gpt-5.4')
    expect(models).toContain('gpt-5.4-mini')
    expect(models).toContain('gpt-5.4-2026-03-05')
    expect(models).toContain('codex-auto-review')
  })

  it('openai 模型列表不再暴露已下线的 ChatGPT 登录 Codex 模型', () => {
    const models = getModelsByPlatform('openai')

    expect(models).not.toContain('gpt-5')
    expect(models).not.toContain('gpt-5.1')
    expect(models).not.toContain('gpt-5.1-codex')
    expect(models).not.toContain('gpt-5.1-codex-max')
    expect(models).not.toContain('gpt-5.1-codex-mini')
    expect(models).not.toContain('gpt-5.2-codex')
  })

  it('antigravity 模型列表由后端官方描述表提供', () => {
    const models = getModelsByPlatform('antigravity')

    expect(models).toEqual(antigravityOfficialModels.map((model) => model.id))
  })

  it('Claude 模型列表包含新发布的 Claude 模型', () => {
    expect(getModelsByPlatform('claude')).toContain('claude-fable-5')
    expect(getModelsByPlatform('claude')).toContain('claude-opus-4-8')
    expect(getModelsByPlatform('antigravity')).not.toContain('claude-fable-5')
    expect(getModelsByPlatform('antigravity')).not.toContain('claude-opus-4-8')
  })

  it('xAI 模型列表包含 Grok 4.5 官方模型和别名', () => {
    const models = getModelsByPlatform('grok')

    expect(models).toContain('grok-4.5')
    expect(models).toContain('grok-4.5-latest')
    expect(models).toContain('grok-build-latest')
  })

  it('combined 模式支持 Grok 4.5 官方别名映射', () => {
    const mapping = buildModelMappingObject(
      'combined',
      ['grok-4.5'],
      [
        { from: 'grok-latest', to: 'grok-4.5' },
        { from: 'grok-4.5-latest', to: 'grok-4.5' },
        { from: 'grok-build-latest', to: 'grok-4.5' }
      ]
    )

    expect(mapping).toEqual({
      'grok-4.5': 'grok-4.5',
      'grok-latest': 'grok-4.5',
      'grok-4.5-latest': 'grok-4.5',
      'grok-build-latest': 'grok-4.5'
    })
  })

  it('grok 模型列表包含 Composer 默认项和兼容别名', () => {
    const models = getModelsByPlatform('grok')

    expect(models).toContain('grok-composer-2.5-fast')
    expect(models).toContain('grok-composer')
    expect(models).toContain('composer-2.5')
  })

  it('gemini 模型列表包含原生生图模型', () => {
    const models = getModelsByPlatform('gemini')

    expect(models).toContain('gemini-2.5-flash-image')
    expect(models).toContain('gemini-3.1-flash-image')
    expect(models.indexOf('gemini-3.1-flash-image')).toBeLessThan(models.indexOf('gemini-2.0-flash'))
    expect(models.indexOf('gemini-2.5-flash-image')).toBeLessThan(models.indexOf('gemini-2.5-flash'))
  })

  it('antigravity 模型列表不再混入非官方图片兼容项', () => {
    const models = getModelsByPlatform('antigravity')

    expect(models).not.toContain('gemini-2.5-flash-image')
    expect(models).not.toContain('gemini-3.1-flash-image')
    expect(models).not.toContain('gemini-3-pro-image')
    expect(models).not.toContain('gemini-3.1-pro')
  })

  it('antigravity 模型列表包含官方 Flash 和 GPT-OSS 模型', () => {
    const models = getModelsByPlatform('antigravity')

    expect(models).toContain('gemini-3.5-flash-extra-low')
    expect(models).toContain('gemini-3.5-flash-low')
    expect(models).toContain('gemini-3-flash-agent')
    expect(models).toContain('gemini-pro-agent')
    expect(models).toContain('gpt-oss-120b-medium')
  })

  it('通用候选列表不再内置 Antigravity 官方发包模型', () => {
    const values = allModels.map(model => model.value)

    expect(values).not.toContain('gemini-pro-agent')
    expect(values).not.toContain('gpt-oss-120b-medium')
  })

  it('Antigravity 官方白名单默认使用抓包确认的 8 个发包 model', () => {
    expect(getOfficialAntigravityModelIDs()).toEqual([
      'gemini-3.5-flash-extra-low',
      'gemini-3.5-flash-low',
      'gemini-3-flash-agent',
      'gemini-3.1-pro-low',
      'gemini-pro-agent',
      'claude-sonnet-4-6',
      'claude-opus-4-6-thinking',
      'gpt-oss-120b-medium'
    ])
  })

  it('Antigravity 官方显示映射默认使用界面显示名到发包 model', () => {
    expect(getOfficialAntigravityDisplayMappings()).toEqual([
      { from: 'Gemini 3.5 Flash Low', to: 'gemini-3.5-flash-extra-low' },
      { from: 'Gemini 3.5 Flash Medium', to: 'gemini-3.5-flash-low' },
      { from: 'Gemini 3.5 Flash High', to: 'gemini-3-flash-agent' },
      { from: 'Gemini 3.1 Pro Low', to: 'gemini-3.1-pro-low' },
      { from: 'Gemini 3.1 Pro High', to: 'gemini-pro-agent' },
      { from: 'Claude Sonnet 4.6', to: 'claude-sonnet-4-6' },
      { from: 'Claude Opus 4.6 Thinking', to: 'claude-opus-4-6-thinking' },
      { from: 'GPT-OSS 120B Medium', to: 'gpt-oss-120b-medium' }
    ])
  })

  it('whitelist 模式会忽略通配符条目', () => {
    const mapping = buildModelMappingObject('whitelist', ['claude-*', 'gemini-3.1-flash-image'], [])
    expect(mapping).toEqual({
      'gemini-3.1-flash-image': 'gemini-3.1-flash-image'
    })
  })

  it('whitelist 模式会保留 GPT-5.4 官方快照的精确映射', () => {
    const mapping = buildModelMappingObject('whitelist', ['gpt-5.4-2026-03-05'], [])

    expect(mapping).toEqual({
      'gpt-5.4-2026-03-05': 'gpt-5.4-2026-03-05'
    })
  })

  it('whitelist keeps GPT-5.4 mini exact mappings', () => {
    const mapping = buildModelMappingObject('whitelist', ['gpt-5.4-mini'], [])

    expect(mapping).toEqual({
      'gpt-5.4-mini': 'gpt-5.4-mini'
    })
  })

  it('combined 模式会同时保留白名单身份映射和模型映射', () => {
    const mapping = buildModelMappingObject(
      'combined',
      ['gpt-5.4', 'claude-*'],
      [
        { from: 'gpt-latest', to: 'gpt-5.4' },
        { from: 'gpt-5.4', to: 'gpt-5.4-mini' }
      ]
    )

    expect(mapping).toEqual({
      'gpt-5.4': 'gpt-5.4-mini',
      'gpt-latest': 'gpt-5.4'
    })
  })

  it('splitModelMappingObject 会把身份映射还原成白名单，其余保留为映射', () => {
    const parsed = splitModelMappingObject({
      'gpt-5.4': 'gpt-5.4',
      'gpt-latest': 'gpt-5.4',
      ' ': 'gpt-empty',
      broken: 123
    })

    expect(parsed).toEqual({
      allowedModels: ['gpt-5.4'],
      modelMappings: [{ from: 'gpt-latest', to: 'gpt-5.4' }]
    })
  })
})

describe('Antigravity 官方模型缓存失败重试', () => {
  it('首次 API 失败不会缓存空数组，后续成功仍会刷新白名单和默认映射', async () => {
    vi.resetModules()
    const warnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const getAntigravityOfficialModels = vi.fn()
      .mockRejectedValueOnce(new Error('temporary failure'))
      .mockResolvedValueOnce({
        models: [
          {
            id: 'gemini-3.5-flash-low',
            display_name: 'Gemini 3.5 Flash Medium',
            model_enum: 'MODEL_PLACEHOLDER_M20',
            thinking_budget: 4000,
            created_at: '2026-06-29T00:00:00Z',
            is_reasoning: true
          }
        ],
        mapping: { 'gemini-3.5-flash-low': 'gemini-3.5-flash-low' }
      })

    vi.doMock('@/api/admin/accounts', () => ({
      getAntigravityDefaultModelMapping: vi.fn(),
      getAntigravityOfficialModels
    }))

    try {
      const module = await import('../useModelWhitelist')

      await expect(module.fetchAntigravityDefaultMappings()).resolves.toEqual([])
      expect(module.getOfficialAntigravityModelIDs()).toEqual([])

      await expect(module.fetchAntigravityDefaultMappings()).resolves.toEqual([
        { from: 'Gemini 3.5 Flash Medium', to: 'gemini-3.5-flash-low' }
      ])
      expect(module.getOfficialAntigravityModelIDs()).toEqual(['gemini-3.5-flash-low'])
      expect(getAntigravityOfficialModels).toHaveBeenCalledTimes(2)
    } finally {
      warnSpy.mockRestore()
      vi.doUnmock('@/api/admin/accounts')
    }
  })
})
