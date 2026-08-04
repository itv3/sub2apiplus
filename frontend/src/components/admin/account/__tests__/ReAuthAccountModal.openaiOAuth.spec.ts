import { defineComponent } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const {
  generateAuthUrlMock,
  exchangeCodeMock,
  reauthorizeOpenAIMock,
  applyOAuthCredentialsMock
} = vi.hoisted(() => ({
  generateAuthUrlMock: vi.fn(),
  exchangeCodeMock: vi.fn(),
  reauthorizeOpenAIMock: vi.fn(),
  applyOAuthCredentialsMock: vi.fn()
}))

vi.mock('@/stores/app', () => ({
  useAppStore: () => ({
    showError: vi.fn(),
    showSuccess: vi.fn()
  })
}))

vi.mock('@/api/admin', () => ({
  adminAPI: {
    accounts: {
      generateAuthUrl: generateAuthUrlMock,
      exchangeCode: exchangeCodeMock,
      reauthorizeOpenAI: reauthorizeOpenAIMock,
      applyOAuthCredentials: applyOAuthCredentialsMock,
      update: vi.fn()
    }
  }
}))

vi.mock('vue-i18n', async () => {
  const actual = await vi.importActual<typeof import('vue-i18n')>('vue-i18n')
  return {
    ...actual,
    useI18n: () => ({ t: (key: string) => key })
  }
})

import type { Account } from '@/types'
import ReAuthAccountModal from '../ReAuthAccountModal.vue'

const BaseDialogStub = defineComponent({
  props: { show: Boolean },
  template: '<div v-if="show"><slot /><slot name="footer" /></div>'
})

const OAuthAuthorizationFlowStub = defineComponent({
  data: () => ({
    inputMethod: 'manual',
    authCode: 'authorization-code',
    oauthState: 'oauth-state',
    projectId: '',
    sessionKey: ''
  }),
  emits: ['generate-url'],
  template: '<button data-testid="generate-oauth-url" @click="$emit(\'generate-url\')">generate</button>'
})

function testAccount(): Account {
  return {
    id: 42,
    name: 'OpenAI account',
    platform: 'openai',
    type: 'oauth',
    status: 'error',
    proxy_id: null,
    credentials: {},
    extra: {}
  } as Account
}

describe('ReAuthAccountModal OpenAI OAuth', () => {
  beforeEach(() => {
    generateAuthUrlMock.mockReset().mockResolvedValue({
      auth_url: 'https://auth.openai.com/oauth/authorize?state=oauth-state',
      session_id: 'server-session-id'
    })
    exchangeCodeMock.mockReset()
    applyOAuthCredentialsMock.mockReset()
    reauthorizeOpenAIMock.mockReset().mockResolvedValue({ ...testAccount(), status: 'active' })
  })

  it('uses the account-aware server endpoint without moving tokens through the browser', async () => {
    const wrapper = mount(ReAuthAccountModal, {
      props: { show: true, account: testAccount() },
      global: {
        stubs: {
          BaseDialog: BaseDialogStub,
          OAuthAuthorizationFlow: OAuthAuthorizationFlowStub,
          Select: true
        }
      }
    })

    await wrapper.get('[data-testid="generate-oauth-url"]').trigger('click')
    await flushPromises()
    const completeButton = wrapper.findAll('button').find((button) =>
      button.text().includes('admin.accounts.oauth.completeAuth')
    )
    expect(completeButton).toBeDefined()
    await completeButton?.trigger('click')
    await flushPromises()

    expect(reauthorizeOpenAIMock).toHaveBeenCalledWith(42, {
      session_id: 'server-session-id',
      code: 'authorization-code',
      state: 'oauth-state'
    })
    expect(exchangeCodeMock).not.toHaveBeenCalled()
    expect(applyOAuthCredentialsMock).not.toHaveBeenCalled()
    expect(wrapper.emitted('reauthorized')).toHaveLength(1)
  })
})
