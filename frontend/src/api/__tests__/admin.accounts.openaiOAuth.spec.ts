import { beforeEach, describe, expect, it, vi } from 'vitest'

const { post } = vi.hoisted(() => ({
  post: vi.fn()
}))

vi.mock('@/api/client', () => ({
  apiClient: { post }
}))

import { createOpenAIFromOAuth, reauthorizeOpenAI } from '@/api/admin/accounts'

describe('OpenAI OAuth 账号受管入口', () => {
  beforeEach(() => {
    post.mockReset()
    post.mockResolvedValue({ data: { id: 42, platform: 'openai', type: 'oauth' } })
  })

  it('新建账号只向服务端提交授权上下文和本地配置', async () => {
    const payload = {
      session_id: 'session-id',
      code: 'authorization-code',
      state: 'oauth-state',
      name: 'OpenAI account',
      credential_extras: { model_mapping: { 'gpt-5': 'gpt-5' } },
      extra: { openai_long_context_billing_enabled: false }
    }

    await createOpenAIFromOAuth(payload)

    expect(post).toHaveBeenCalledWith('/admin/openai/create-from-oauth', payload)
    expect(payload).not.toHaveProperty('credentials')
    expect(payload).not.toHaveProperty('privacy_mode')
  })

  it('重授权只提交授权上下文，不由前端回传 Credentials 或 Extra', async () => {
    const payload = {
      session_id: 'session-id',
      code: 'authorization-code',
      state: 'oauth-state'
    }

    await reauthorizeOpenAI(42, payload)

    expect(post).toHaveBeenCalledWith('/admin/openai/accounts/42/reauthorize', payload)
    expect(payload).not.toHaveProperty('credentials')
    expect(payload).not.toHaveProperty('extra')
  })
})
