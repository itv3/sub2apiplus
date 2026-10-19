import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { AntigravityOfficialModelDescriptor } from '@/api/admin/accounts'

// fork 已把默认映射改为由官方描述表（getAntigravityOfficialModels）生成：
// 映射的 from = display_name || id，to = id，不再调用旧的 getAntigravityDefaultModelMapping。
// 因此这里 mock 新接口；旧接口仍导出一个空 mock，避免被测模块导入时缺失导出。
const getOfficialModels = vi.hoisted(() => vi.fn())
vi.mock('@/api/admin/accounts', () => ({
  getAntigravityDefaultModelMapping: vi.fn(),
  getAntigravityOfficialModels: getOfficialModels
}))

/** 构造一条官方模型描述符：display_name 对应默认映射的 from，id 对应 to。 */
function officialModel(displayName: string, id: string): AntigravityOfficialModelDescriptor {
  return {
    id,
    display_name: displayName,
    model_enum: 'MODEL_PLACEHOLDER_TEST',
    thinking_budget: 4000,
    created_at: '2026-06-29T00:00:00Z',
    is_reasoning: true
  }
}

/** 构造接口成功响应；mapping 字段由描述符推导，被测逻辑只读取 models。 */
function officialPayload(models: AntigravityOfficialModelDescriptor[]) {
  return {
    models,
    mapping: Object.fromEntries(models.map((model) => [model.id, model.id]))
  }
}

// 描述表 [alias -> upstream-model] 及其对应的默认映射
const aliasModels = [officialModel('alias', 'upstream-model')]
const aliasMapping = [{ from: 'alias', to: 'upstream-model' }]

beforeEach(() => {
  vi.resetModules()
  getOfficialModels.mockReset()
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => vi.restoreAllMocks())

describe('Antigravity default mapping cache', () => {
  it('retries after a failed fetch rather than caching the empty fallback', async () => {
    getOfficialModels
      .mockRejectedValueOnce(new Error('offline'))
      .mockResolvedValueOnce(officialPayload(aliasModels))
    const { fetchAntigravityDefaultMappings: fetch } = await import('../useModelWhitelist')
    expect(await fetch()).toEqual([])
    expect(await fetch()).toEqual(aliasMapping)
    expect(getOfficialModels).toHaveBeenCalledTimes(2)
  })

  it('caches a successful mapping', async () => {
    getOfficialModels.mockResolvedValue(officialPayload(aliasModels))
    const { fetchAntigravityDefaultMappings: fetch } = await import('../useModelWhitelist')
    expect(await fetch()).toEqual(aliasMapping)
    expect(await fetch()).toEqual(aliasMapping)
    expect(getOfficialModels).toHaveBeenCalledTimes(1)
  })

  it('still caches a successful empty descriptor table', async () => {
    getOfficialModels.mockResolvedValue(officialPayload([]))
    const { fetchAntigravityDefaultMappings: fetch } = await import('../useModelWhitelist')
    expect(await fetch()).toEqual([])
    expect(await fetch()).toEqual([])
    expect(getOfficialModels).toHaveBeenCalledTimes(1)
  })

  it('does not erase a successful concurrent result when an older request fails', async () => {
    let reject!: (error: Error) => void
    getOfficialModels
      .mockImplementationOnce(() => new Promise((_, fail) => { reject = fail }))
      .mockResolvedValueOnce(officialPayload(aliasModels))
    const { fetchAntigravityDefaultMappings: fetch } = await import('../useModelWhitelist')
    const older = fetch()
    // 较新的请求先成功，写入官方描述表缓存与默认映射缓存
    expect(await fetch()).toEqual(aliasMapping)
    // 较旧的请求随后失败
    reject(new Error('offline'))
    const olderResult = await older
    // 旧请求失败不得把成功结果抹成空数组：后续调用仍应命中缓存，拿到成功映射
    expect(await fetch()).toEqual(aliasMapping)
    // 旧请求本身也应返回并发成功者写入的映射，而不是自己的空兜底
    expect(olderResult).toEqual(aliasMapping)
    expect(getOfficialModels).toHaveBeenCalledTimes(2)
  })
})
