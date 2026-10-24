import { beforeEach, describe, expect, it, vi } from 'vitest'

const { put } = vi.hoisted(() => ({ put: vi.fn() }))

vi.mock('@/api/client', () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), put, delete: vi.fn() }
}))

import { saveOllamaCloudUsageSession, setOllamaCloudUsageSessionExpiry } from '@/api/admin/accounts'

const state = {
  account_id: 7,
  eligible: true,
  configured: true,
  auto_refresh_enabled: false,
  encryption_key_configured: true,
  session_expires_at: '2026-11-30T00:00:00Z'
}

describe('admin Ollama Cloud usage session expiry API', () => {
  beforeEach(() => {
    put.mockReset()
    put.mockResolvedValue({ data: state })
  })

  it('sends only the session when no expiry is given (unchanged legacy payload)', async () => {
    await saveOllamaCloudUsageSession(7, 'wos-session=a')
    expect(put).toHaveBeenCalledWith('/admin/accounts/7/ollama-cloud-usage/session', { session: 'wos-session=a' })
  })

  it('sends the optional expiry together with a new Cookie', async () => {
    await expect(saveOllamaCloudUsageSession(7, 'wos-session=a', '2026-11-30T00:00:00Z')).resolves.toEqual(state)
    expect(put).toHaveBeenCalledWith('/admin/accounts/7/ollama-cloud-usage/session', {
      session: 'wos-session=a',
      session_expires_at: '2026-11-30T00:00:00Z'
    })
  })

  it('updates or clears only the expiry without a Cookie', async () => {
    await setOllamaCloudUsageSessionExpiry(7, '2026-12-01T00:00:00Z')
    expect(put).toHaveBeenLastCalledWith('/admin/accounts/7/ollama-cloud-usage/session', {
      session_expires_at: '2026-12-01T00:00:00Z'
    })
    await setOllamaCloudUsageSessionExpiry(7, null)
    expect(put).toHaveBeenLastCalledWith('/admin/accounts/7/ollama-cloud-usage/session', { session_expires_at: null })
  })
})
