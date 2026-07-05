import { describe, expect, it, vi } from 'vitest'

vi.mock('@/api/admin/accounts', () => ({
  getAntigravityDefaultModelMapping: vi.fn()
}))

import {
  buildModelMappingObject,
  getModelsByPlatform,
  getOfficialAntigravityDisplayMappings,
  getOfficialAntigravityModelIDs,
  splitModelMappingObject
} from '../useModelWhitelist'

describe('useModelWhitelist', () => {
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

  it('antigravity 模型列表包含图片模型兼容项', () => {
    const models = getModelsByPlatform('antigravity')

    expect(models).toContain('gemini-2.5-flash-image')
    expect(models).toContain('gemini-3.1-flash-image')
    expect(models).toContain('gemini-3-pro-image')
  })

  it('Claude 模型列表包含新发布的 Claude 模型', () => {
    expect(getModelsByPlatform('claude')).toContain('claude-fable-5')
    expect(getModelsByPlatform('antigravity')).toContain('claude-fable-5')
    expect(getModelsByPlatform('claude')).toContain('claude-opus-4-8')
    expect(getModelsByPlatform('antigravity')).toContain('claude-opus-4-8')
  })

  it('gemini 模型列表包含原生生图模型', () => {
    const models = getModelsByPlatform('gemini')

    expect(models).toContain('gemini-2.5-flash-image')
    expect(models).toContain('gemini-3.1-flash-image')
    expect(models.indexOf('gemini-3.1-flash-image')).toBeLessThan(models.indexOf('gemini-2.0-flash'))
    expect(models.indexOf('gemini-2.5-flash-image')).toBeLessThan(models.indexOf('gemini-2.5-flash'))
  })

  it('antigravity 模型列表会把新的 Gemini 图片模型排在前面', () => {
    const models = getModelsByPlatform('antigravity')

    expect(models.indexOf('gemini-3.1-flash-image')).toBeLessThan(models.indexOf('gemini-2.5-flash'))
    expect(models.indexOf('gemini-2.5-flash-image')).toBeLessThan(models.indexOf('gemini-2.5-flash-lite'))
  })

  it('antigravity 模型列表包含 Gemini 3.1 Pro 通用别名', () => {
    const models = getModelsByPlatform('antigravity')

    expect(models).toContain('gemini-3.1-pro')
  })

  it('antigravity 模型列表包含官方 Flash 和 GPT-OSS 模型', () => {
    const models = getModelsByPlatform('antigravity')

    expect(models).toContain('gemini-3.5-flash-extra-low')
    expect(models).toContain('gemini-3.5-flash-low')
    expect(models).toContain('gemini-3-flash-agent')
    expect(models).toContain('gemini-pro-agent')
    expect(models).toContain('gpt-oss-120b-medium')
  })

  it('Antigravity 官方白名单默认使用抓包确认的 8 个发包 model', () => {
    expect(getOfficialAntigravityModelIDs()).toEqual([
      'claude-opus-4-6-thinking',
      'claude-sonnet-4-6',
      'gemini-3-flash-agent',
      'gemini-3.1-pro-low',
      'gemini-3.5-flash-extra-low',
      'gemini-3.5-flash-low',
      'gemini-pro-agent',
      'gpt-oss-120b-medium'
    ])
  })

  it('Antigravity 官方显示映射默认使用界面显示名到发包 model', () => {
    expect(getOfficialAntigravityDisplayMappings()).toEqual([
      { from: 'Claude Opus 4.6 Thinking', to: 'claude-opus-4-6-thinking' },
      { from: 'Claude Sonnet 4.6', to: 'claude-sonnet-4-6' },
      { from: 'GPT-OSS 120B Medium', to: 'gpt-oss-120b-medium' },
      { from: 'Gemini 3.1 Pro High', to: 'gemini-pro-agent' },
      { from: 'Gemini 3.1 Pro Low', to: 'gemini-3.1-pro-low' },
      { from: 'Gemini 3.5 Flash High', to: 'gemini-3-flash-agent' },
      { from: 'Gemini 3.5 Flash Low', to: 'gemini-3.5-flash-extra-low' },
      { from: 'Gemini 3.5 Flash Medium', to: 'gemini-3.5-flash-low' }
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
