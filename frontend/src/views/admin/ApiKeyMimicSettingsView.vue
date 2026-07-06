<template>
  <AppLayout>
    <TablePageLayout>
      <template #filters>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-lg font-semibold text-gray-900 dark:text-white">
              {{ t('admin.apiKeyMimic.title') }}
            </h2>
            <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
              {{ t('admin.apiKeyMimic.description') }}
            </p>
          </div>
          <button
            type="button"
            class="btn btn-secondary"
            :disabled="loading"
            :title="t('admin.apiKeyMimic.refresh')"
            @click="loadAccounts"
          >
            <Icon name="refresh" size="md" :class="loading ? 'animate-spin' : ''" />
            <span class="hidden sm:inline">{{ t('admin.apiKeyMimic.refresh') }}</span>
          </button>
        </div>
      </template>

      <template #table>
        <DataTable
          :columns="columns"
          :data="accounts"
          :loading="loading"
          default-sort-key="id"
          default-sort-order="desc"
        >
          <template #cell-account="{ row }">
            <div class="min-w-0">
              <div class="truncate font-medium text-gray-900 dark:text-white">
                {{ row.name }}
              </div>
              <div class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                #{{ row.id }}
              </div>
            </div>
          </template>

          <template #cell-platform="{ row }">
            <span
              class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium"
              :class="platformBadgeClass(row.platform)"
            >
              {{ platformLabel(row.platform) }}
            </span>
          </template>

          <template #cell-compatible="{ row }">
            <button
              type="button"
              role="switch"
              class="relative inline-flex h-6 w-11 flex-shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 dark:focus:ring-offset-dark-800"
              :class="isMimicEnabled(row) ? 'bg-primary-600' : 'bg-gray-200 dark:bg-dark-600'"
              :aria-checked="isMimicEnabled(row)"
              :aria-label="`${row.name} ${t('admin.apiKeyMimic.columns.compatible')}`"
              :disabled="updatingIds.has(row.id)"
              @click.stop="toggleMimic(row)"
            >
              <span
                class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out"
                :class="isMimicEnabled(row) ? 'translate-x-5' : 'translate-x-0'"
              />
            </button>
          </template>

          <template #cell-status="{ row }">
            <div class="flex flex-wrap gap-2">
              <span
                v-for="item in statusLabels(row)"
                :key="item"
                class="inline-flex rounded-full bg-gray-100 px-2.5 py-1 text-xs font-medium text-gray-700 dark:bg-dark-700 dark:text-gray-200"
              >
                {{ item }}
              </span>
            </div>
          </template>

          <template #empty>
            <div class="flex flex-col items-center py-6 text-gray-500 dark:text-gray-400">
              <Icon name="inbox" size="xl" class="mb-4 h-12 w-12 text-gray-400 dark:text-dark-500" />
              <p class="text-lg font-medium text-gray-900 dark:text-gray-100">
                {{ t('admin.apiKeyMimic.empty') }}
              </p>
            </div>
          </template>
        </DataTable>
      </template>
    </TablePageLayout>
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { adminAPI } from '@/api/admin'
import { useAppStore } from '@/stores/app'
import type { Account, AccountPlatform } from '@/types'
import type { Column } from '@/components/common/types'

import AppLayout from '@/components/layout/AppLayout.vue'
import TablePageLayout from '@/components/layout/TablePageLayout.vue'
import DataTable from '@/components/common/DataTable.vue'
import Icon from '@/components/icons/Icon.vue'

const ANTHROPIC_MIMIC_KEY = 'anthropic_apikey_mimic_claude_code'
const OPENAI_MIMIC_KEY = 'openai_apikey_mimic_codex_cli'
const OPENAI_PROFILE_KEY = 'openai_apikey_mimic_codex_profile'
const TLS_FINGERPRINT_KEY = 'enable_tls_fingerprint'
const DEFAULT_CODEX_PROFILE = 'desktop_0_142'

const { t } = useI18n()
const appStore = useAppStore()

const accounts = ref<Account[]>([])
const loading = ref(false)
const updatingIds = ref<Set<number>>(new Set())

const columns = computed<Column[]>(() => [
  { key: 'account', label: t('admin.apiKeyMimic.columns.account') },
  { key: 'platform', label: t('admin.apiKeyMimic.columns.platform'), sortable: true },
  { key: 'compatible', label: t('admin.apiKeyMimic.columns.compatible') },
  { key: 'status', label: t('admin.apiKeyMimic.columns.status') }
])

function platformLabel(platform: AccountPlatform): string {
  if (platform === 'anthropic') return t('admin.apiKeyMimic.platformLabels.anthropic')
  if (platform === 'openai') return t('admin.apiKeyMimic.platformLabels.openai')
  return platform
}

function platformBadgeClass(platform: AccountPlatform): string {
  if (platform === 'anthropic') return 'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-300'
  if (platform === 'openai') return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
  return 'bg-gray-100 text-gray-700 dark:bg-dark-700 dark:text-gray-200'
}

function isMimicEnabled(account: Account): boolean {
  const extra = account.extra || {}
  if (account.platform === 'anthropic') return extra[ANTHROPIC_MIMIC_KEY] === true
  if (account.platform === 'openai') return extra[OPENAI_MIMIC_KEY] === true
  return false
}

function statusLabels(account: Account): string[] {
  const extra = account.extra || {}
  const enabled = isMimicEnabled(account)
  const labels: string[] = []

  if (!enabled) {
    labels.push(t('admin.apiKeyMimic.statusLabels.disabled'))
    return labels
  }

  if (account.platform === 'anthropic') {
    labels.push(t('admin.apiKeyMimic.statusLabels.claudeCode'))
    if (extra.anthropic_passthrough === true) {
      labels.push(t('admin.apiKeyMimic.statusLabels.passthroughAlsoEnabled'))
    }
    return labels
  }

  const profile = String(extra[OPENAI_PROFILE_KEY] || DEFAULT_CODEX_PROFILE)
  if (profile === DEFAULT_CODEX_PROFILE) {
    labels.push(t('admin.apiKeyMimic.statusLabels.codexDesktop'))
  } else if (profile === 'cli_rs_0_125') {
    labels.push(t('admin.apiKeyMimic.statusLabels.codexCli'))
  } else {
    labels.push(t('admin.apiKeyMimic.statusLabels.unknownCodexProfile', { profile }))
  }
  if (extra.openai_passthrough === true || extra.openai_oauth_passthrough === true) {
    labels.push(t('admin.apiKeyMimic.statusLabels.passthroughAlsoEnabled'))
  }
  return labels
}

async function fetchPlatformAccounts(platform: 'anthropic' | 'openai'): Promise<Account[]> {
  const pageSize = 100
  let page = 1
  const result: Account[] = []

  while (true) {
    const response = await adminAPI.accounts.list(page, pageSize, {
      platform,
      type: 'apikey',
      sort_by: 'id',
      sort_order: 'desc'
    })
    result.push(...response.items)
    if (page >= response.pages || response.items.length === 0) break
    page += 1
  }

  return result
}

async function loadAccounts() {
  try {
    loading.value = true
    const [anthropicAccounts, openaiAccounts] = await Promise.all([
      fetchPlatformAccounts('anthropic'),
      fetchPlatformAccounts('openai')
    ])
    accounts.value = [...anthropicAccounts, ...openaiAccounts]
      .filter(account => account.type === 'apikey' && (account.platform === 'anthropic' || account.platform === 'openai'))
      .sort((a, b) => b.id - a.id)
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || t('admin.apiKeyMimic.loadFailed'))
  } finally {
    loading.value = false
  }
}

function buildMimicPatch(account: Account, enabled: boolean): Record<string, unknown> {
  if (account.platform === 'anthropic') {
    return {
      [ANTHROPIC_MIMIC_KEY]: enabled,
      [TLS_FINGERPRINT_KEY]: enabled
    }
  }

  return {
    [OPENAI_MIMIC_KEY]: enabled,
    [OPENAI_PROFILE_KEY]: String(account.extra?.[OPENAI_PROFILE_KEY] || DEFAULT_CODEX_PROFILE),
    [TLS_FINGERPRINT_KEY]: enabled
  }
}

async function toggleMimic(account: Account) {
  if (updatingIds.value.has(account.id)) return

  const enabled = !isMimicEnabled(account)
  updatingIds.value = new Set(updatingIds.value).add(account.id)

  try {
    const updated = await adminAPI.accounts.updateExtra(account.id, buildMimicPatch(account, enabled))
    accounts.value = accounts.value.map(item => item.id === updated.id ? updated : item)
    appStore.showSuccess(t('admin.apiKeyMimic.updateSuccess'))
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || t('admin.apiKeyMimic.updateFailed'))
  } finally {
    const next = new Set(updatingIds.value)
    next.delete(account.id)
    updatingIds.value = next
  }
}

onMounted(() => {
  loadAccounts()
})
</script>
