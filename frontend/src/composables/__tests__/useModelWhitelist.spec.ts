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
  getPresetMappingsByPlatform,
  splitModelMappingObject
} from '../useModelWhitelist'
import { BUILTIN_PLATFORM_CATALOG, resetPlatformCatalog, setPlatformCatalog } from '@/constants/platformCatalog'

describe('useModelWhitelist', () => {
  beforeAll(async () => {
    await fetchAntigravityOfficialModels()
  })

  it('平台清单中没有内置模型列表的多协议供应商不预填白名单', () => {
    setPlatformCatalog({
      ...BUILTIN_PLATFORM_CATALOG,
      platforms: [
        ...BUILTIN_PLATFORM_CATALOG.platforms,
        {
          id: 'acme_router',
          display_name: 'Acme Router',
          gateway: 'openai',
          cn_provider: false,
          multi_protocol: {
            default_mode: 'standard',
            routing: 'by_model',
            modes: [{ mode: 'standard', base_urls: { chat_completions: 'https://api.acme-router.example/v1' } }]
          }
        }
      ]
    })
    try {
      expect(getModelsByPlatform('acme_router')).toEqual([])
      expect(getModelsByPlatform('kimi').length).toBeGreaterThan(0)
      // 非多协议供应商的未知平台维持原有回退。
      expect(getModelsByPlatform('bedrock')).toEqual(getModelsByPlatform('anthropic'))
    } finally {
      resetPlatformCatalog()
    }
  })

  it('openai 模型列表包含 GPT-5.4 官方快照', () => {
    const models = getModelsByPlatform('openai')

    expect(models).toContain('gpt-5.4')
    expect(models).toContain('gpt-5.4-mini')
    expect(models).toContain('gpt-5.4-2026-03-05')
    expect(models).toContain('codex-auto-review')
    expect(models).toContain('gpt-5.6')
    expect(models).toContain('gpt-6')
    expect(models).toContain('gpt-6-astra')
    expect(models).toContain('gpt-6.1-sol')
    expect(models).toContain('gpt-6-sol')
    expect(models).toContain('gpt-6-luna')
  })

  it('openai 预设映射包含 GPT-6 别名和 Astra', () => {
    expect(getPresetMappingsByPlatform('openai')).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: 'GPT-6.1 Sol', from: 'gpt-6.1-sol', to: 'gpt-6.1-sol' }),
      expect.objectContaining({ label: 'GPT-6', from: 'gpt-6', to: 'gpt-6' }),
      expect.objectContaining({ label: 'GPT-6 Astra', from: 'gpt-6-astra', to: 'gpt-6-astra' })
    ]))
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
    expect(getModelsByPlatform('claude')).toContain('claude-fable-5-1')
    // Antigravity 白名单只来自后端官方描述表（桩数据里没有 Fable），与下方 fable-5 / opus-4-8 的反向断言保持一致。
    expect(getModelsByPlatform('antigravity')).not.toContain('claude-fable-5-1')
    expect(getModelsByPlatform('claude')).toContain('claude-fable-5')
    expect(getModelsByPlatform('claude')).toContain('claude-opus-5-5')
    expect(getModelsByPlatform('antigravity')).not.toContain('claude-opus-5-5')
    expect(getModelsByPlatform('claude')).toContain('claude-sonnet-5-5')
    expect(getModelsByPlatform('antigravity')).not.toContain('claude-sonnet-5-5')
    expect(getModelsByPlatform('claude')).toContain('claude-opus-4-8')
    expect(getModelsByPlatform('antigravity')).not.toContain('claude-fable-5')
    expect(getModelsByPlatform('antigravity')).not.toContain('claude-opus-4-8')
  })

  it('Claude Sonnet 5.5 预设使用各平台的官方模型 ID', () => {
    expect(getPresetMappingsByPlatform('claude')).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: 'Sonnet 5.5', from: 'claude-sonnet-5-5', to: 'claude-sonnet-5-5' })
    ]))
    expect(getPresetMappingsByPlatform('bedrock')).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: 'Sonnet 5.5', from: 'claude-sonnet-5-5', to: 'global.anthropic.claude-sonnet-5-5' })
    ]))
  })

  it('Claude 默认白名单保存后允许 Haiku 5.5，预设使用同一模型 ID', () => {
    const models: string[] = getModelsByPlatform('anthropic')
    const mapping: Record<string, string> | null = buildModelMappingObject('whitelist', models, [])

    expect(mapping?.['claude-haiku-5-5']).toBe('claude-haiku-5-5')
    expect(getPresetMappingsByPlatform('anthropic')).toEqual(expect.arrayContaining([
      expect.objectContaining({ label: 'Haiku 5.5', from: 'claude-haiku-5-5', to: 'claude-haiku-5-5' })
    ]))
  })

  it('xAI 模型列表包含 Grok 4.5 官方模型和别名', () => {
    const models = getModelsByPlatform('grok')

    expect(models).toContain('grok-4.6')
    expect(models).toContain('grok-4.6-latest')
    expect(models).toContain('grok-4.5')
    expect(models).toContain('grok-4.5-latest')
    expect(models).toContain('grok-build-latest')
    expect(models).toContain('grok-imagine-image-2.0')
    expect(models).toContain('grok-imagine-video-1.5')
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
    expect(models).not.toContain('grok-composer')
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

  // 上游把 Antigravity 的 Argon 模型与预设写死在前端；本分支的 Antigravity 模型与预设只来自后端官方描述表，
  // 所以这里只断言 gemini 平台的 Argon 真实 ID，以及别名不会被当作 Antigravity 发包 model。
  // Argon 三档在描述表提供时能否进入白名单与预设，见文件末尾“官方描述表包含 Gemini 4 Argon 时”。
  it('Gemini 4 Argon 在 gemini 平台使用真实模型 ID，别名不会出现在 antigravity 白名单', () => {
    expect(getModelsByPlatform('gemini')).toContain('gemini-4-argon')
    expect(getModelsByPlatform('antigravity')).not.toContain('gemini-4-argon')
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

  it('combined mode retains a mapping when a whitelist entry has the same source', () => {
    expect(buildModelMappingObject('combined', ['gpt-latest'], [{ from: 'gpt-latest', to: 'deepseek-chat' }])).toEqual({
      'gpt-latest': 'deepseek-chat'
    })
  })

  it('split mapping keeps only identity entries in the whitelist after reopening', () => {
    expect(splitModelMappingObject({ 'gpt-latest': 'deepseek-chat', 'gpt-5.4': 'gpt-5.4' })).toEqual({
      allowedModels: ['gpt-5.4'],
      modelMappings: [{ from: 'gpt-latest', to: 'deepseek-chat' }]
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

describe('Antigravity 官方描述表包含 Gemini 4 Argon 时', () => {
  it('白名单使用三档真实模型 ID，预设映射由描述表生成而不是前端硬编码', async () => {
    vi.resetModules()
    const argonModels = [
      { id: 'gemini-4-argon-high', display_name: 'Gemini 4 Argon High', model_enum: 'MODEL_PLACEHOLDER_ARGON_HIGH', thinking_budget: 10000, created_at: '2026-10-15T00:00:00Z', is_reasoning: true },
      { id: 'gemini-4-argon-medium', display_name: 'Gemini 4 Argon Medium', model_enum: 'MODEL_PLACEHOLDER_ARGON_MEDIUM', thinking_budget: 4000, created_at: '2026-10-15T00:00:00Z', is_reasoning: true },
      { id: 'gemini-4-argon-low', display_name: 'Gemini 4 Argon Low', model_enum: 'MODEL_PLACEHOLDER_ARGON_LOW', thinking_budget: 1000, created_at: '2026-10-15T00:00:00Z', is_reasoning: true }
    ]

    vi.doMock('@/api/admin/accounts', () => ({
      getAntigravityDefaultModelMapping: vi.fn(),
      getAntigravityOfficialModels: vi.fn(async () => ({
        models: argonModels,
        mapping: Object.fromEntries(argonModels.map((model) => [model.id, model.id]))
      }))
    }))

    try {
      const module = await import('../useModelWhitelist')
      await module.fetchAntigravityOfficialModels()

      const agModels = module.getModelsByPlatform('antigravity')
      expect(agModels).toEqual(['gemini-4-argon-high', 'gemini-4-argon-medium', 'gemini-4-argon-low'])
      expect(agModels).not.toContain('gemini-4-argon')
      expect(module.getPresetMappingsByPlatform('antigravity')).toEqual([
        expect.objectContaining({ label: 'Gemini 4 Argon High', from: 'Gemini 4 Argon High', to: 'gemini-4-argon-high' }),
        expect.objectContaining({ label: 'Gemini 4 Argon Medium', from: 'Gemini 4 Argon Medium', to: 'gemini-4-argon-medium' }),
        expect.objectContaining({ label: 'Gemini 4 Argon Low', from: 'Gemini 4 Argon Low', to: 'gemini-4-argon-low' })
      ])
    } finally {
      vi.doUnmock('@/api/admin/accounts')
    }
  })
})
