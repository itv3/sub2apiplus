import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import PlatformTypeBadge from '../PlatformTypeBadge.vue'

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return { ...actual, useI18n: () => ({ t: (key: string) => key }) }
})

describe('PlatformTypeBadge custom plan text (Ollama)', () => {
  it('shows Pro verbatim on the openai platform instead of the ChatGPT Pro 200 tier', () => {
    const wrapper = mount(PlatformTypeBadge, { props: { platform: 'openai', type: 'apikey', planText: 'Pro' } })
    expect(wrapper.text()).toContain('Pro')
    expect(wrapper.text()).not.toContain('Pro 200')
    expect(wrapper.html()).toContain('bg-violet-100')
  })

  it('shows Free in muted gray and Max / other paid tiers in violet', () => {
    const free = mount(PlatformTypeBadge, { props: { platform: 'anthropic', type: 'apikey', planText: 'Free' } })
    expect(free.text()).toContain('Free')
    expect(free.html()).toContain('bg-gray-100')
    const max = mount(PlatformTypeBadge, { props: { platform: 'openai', type: 'apikey', planText: 'Max' } })
    expect(max.text()).toContain('Max')
    expect(max.html()).toContain('bg-violet-100')
  })

  it('shows the note row with its tooltip under the plan badge', () => {
    const wrapper = mount(PlatformTypeBadge, {
      props: {
        platform: 'openai',
        type: 'apikey',
        planText: 'Pro',
        noteText: 'admin.accounts.ollamaQuotaResets 2026-11-24',
        noteTitle: 'Page text: in 4 weeks'
      }
    })
    const note = wrapper.get('[data-testid="platform-type-badge-note"]')
    expect(note.text()).toBe('admin.accounts.ollamaQuotaResets 2026-11-24')
    expect(note.attributes('title')).toBe('Page text: in 4 weeks')
  })

  it('does not override a real planType and leaves non-Ollama accounts unchanged', () => {
    const chatgpt = mount(PlatformTypeBadge, { props: { platform: 'openai', type: 'oauth', planType: 'pro', planText: 'Free' } })
    expect(chatgpt.text()).toContain('Pro 200')
    expect(chatgpt.text()).not.toContain('Free')

    const plain = mount(PlatformTypeBadge, { props: { platform: 'openai', type: 'apikey' } })
    expect(plain.find('[data-testid="platform-type-badge-note"]').exists()).toBe(false)
    expect(plain.text()).not.toContain('Pro')
    expect(plain.text()).not.toContain('Free')
  })
})
