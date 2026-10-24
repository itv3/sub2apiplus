import { beforeEach, describe, expect, it, vi } from 'vitest'

const client = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('../../client', () => ({ apiClient: client }))

import { deleteProfileAlias, listProfileAliases, refreshCatalogs, setProfileAlias } from '../codexThirdPartyModels'

describe('codexThirdPartyModels API', () => {
  beforeEach(() => {
    for (const fn of Object.values(client)) fn.mockReset()
  })

  it('刷新上游目录：POST /refresh，超时放宽到 45 秒', async () => {
    client.post.mockResolvedValue({ data: { refresh: { total: 1 }, state: { group_id: 7 } } })
    const result = await refreshCatalogs(7)
    expect(client.post).toHaveBeenCalledWith('/admin/codex-third-party-models/7/refresh', undefined, { timeout: 45000 })
    expect(result.state.group_id).toBe(7)
  })

  it('型号别名：列出、新增或更新、解除关联', async () => {
    const list = { revision: 1, aliases: [], profiles: [] }
    client.get.mockResolvedValue({ data: list })
    client.put.mockResolvedValue({ data: list })
    client.delete.mockResolvedValue({ data: list })

    expect(await listProfileAliases()).toBe(list)
    expect(client.get).toHaveBeenCalledWith('/admin/codex-third-party-profile-aliases')

    await setProfileAlias('acme-ds:prod', 'deepseek-flash')
    expect(client.put).toHaveBeenCalledWith('/admin/codex-third-party-profile-aliases', {
      upstream_model: 'acme-ds:prod',
      profile_id: 'deepseek-flash'
    })

    await deleteProfileAlias('vendor/acme-ds:prod')
    expect(client.delete).toHaveBeenCalledWith('/admin/codex-third-party-profile-aliases', {
      params: { upstream_model: 'vendor/acme-ds:prod' }
    })
  })
})
