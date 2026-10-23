<template>
  <AppLayout>
    <TablePageLayout>
      <template #filters>
        <div class="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h2 class="text-lg font-semibold text-gray-900 dark:text-white">
              {{ t('admin.plusEnhancements.title') }}
            </h2>
            <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
              {{ t('admin.plusEnhancements.description') }}
            </p>
          </div>
          <button
            type="button"
            class="btn btn-secondary"
            :disabled="mimicLoading"
            :title="t('admin.apiKeyMimic.refresh')"
            @click="activeTab === 'codexModels' ? codexPanelRef?.reload() : loadMimicAccounts()"
          >
            <Icon name="refresh" size="md" :class="mimicLoading ? 'animate-spin' : ''" />
            <span class="hidden sm:inline">{{ t('admin.apiKeyMimic.refresh') }}</span>
          </button>
        </div>

        <div
          v-if="activeTab !== 'codexModels'"
          data-testid="oauth-official-egress-built-in"
          class="mt-4 rounded-lg border border-primary-200 bg-primary-50 px-4 py-3 dark:border-primary-900/60 dark:bg-primary-950/30"
        >
          <div class="text-sm font-semibold text-primary-800 dark:text-primary-200">
            {{ t('admin.plusEnhancements.officialEgress.title') }}
          </div>
          <p class="mt-1 text-xs text-primary-700 dark:text-primary-300">
            {{ t('admin.plusEnhancements.officialEgress.description') }}
          </p>
        </div>

        <div class="mt-4 inline-flex rounded-lg border border-gray-200 bg-white p-1 dark:border-dark-700 dark:bg-dark-800">
          <button
            type="button"
            class="rounded-md px-3 py-2 text-sm font-medium transition-colors"
            :class="activeTab === 'mimic' ? activeTabClass : inactiveTabClass"
            @click="activeTab = 'mimic'"
          >
            {{ t('admin.plusEnhancements.tabs.mimic') }}
          </button>
          <button
            type="button"
            class="rounded-md px-3 py-2 text-sm font-medium transition-colors"
            :class="activeTab === 'standard' ? activeTabClass : inactiveTabClass"
            @click="activeTab = 'standard'"
          >
            {{ t('admin.plusEnhancements.tabs.standard') }}
          </button>
          <button
            type="button"
            class="rounded-md px-3 py-2 text-sm font-medium transition-colors"
            data-testid="plus-tab-codex-models"
            :class="activeTab === 'codexModels' ? activeTabClass : inactiveTabClass"
            @click="activeTab = 'codexModels'"
          >
            {{ t('admin.codexThirdPartyModels.tabLabel') }}
          </button>
        </div>
      </template>

      <template #table>
        <div v-if="activeTab !== 'codexModels'" class="space-y-3">
          <div v-if="activeTab === 'standard'" class="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600 dark:border-dark-700 dark:bg-dark-800 dark:text-gray-300">
            <p>{{ t('admin.standardApiCompat.description') }}</p>
            <p class="mt-1 text-xs">{{ t('admin.standardApiCompat.hint') }}</p>
          </div>
          <div class="flex flex-wrap items-center justify-end gap-2">
            <label :for="`${activeTab}-status-filter`" class="text-sm font-medium text-gray-700 dark:text-gray-200">
              {{ t('admin.apiKeyMimic.filters.status') }}
            </label>
            <select :id="`${activeTab}-status-filter`" v-model="compatibilityStatusFilter" :data-testid="`${activeTab}-status-filter`" class="input w-full sm:w-44">
              <option value="all">{{ t('admin.apiKeyMimic.filters.all') }}</option>
              <option value="enabled">{{ t('admin.apiKeyMimic.filters.enabled') }}</option>
              <option value="disabled">{{ t('admin.apiKeyMimic.filters.disabled') }}</option>
            </select>
          </div>

          <DataTable
            :columns="mimicColumns"
            :data="activeTab === 'standard' ? visibleStandardAccounts : visibleMimicAccounts"
            :loading="mimicLoading"
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
              <span class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium" :class="platformBadgeClass(row.platform)">
                {{ platformLabel(row.platform) }}
              </span>
            </template>

            <template #cell-compatible="{ row }">
              <button
                type="button"
                role="switch"
                class="relative inline-flex h-6 w-11 flex-shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 dark:focus:ring-offset-dark-800"
                :class="isCompatibilityEnabled(row) ? 'bg-primary-600' : 'bg-gray-200 dark:bg-dark-600'"
                :aria-checked="isCompatibilityEnabled(row)"
                :aria-label="`${row.name} ${compatibilityLabel}`"
                :title="isOtherCompatibilityEnabled(row) ? t('admin.standardApiCompat.conflict') : compatibilityLabel"
                :disabled="mimicUpdatingIds.has(row.id) || (isOtherCompatibilityEnabled(row) && !isCompatibilityEnabled(row))"
                @click.stop="activeTab === 'standard' ? toggleStandardAPICompat(row) : toggleMimic(row)"
              >
                <span
                  class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out"
                  :class="isCompatibilityEnabled(row) ? 'translate-x-5' : 'translate-x-0'"
                />
              </button>
            </template>

            <template #cell-status="{ row }">
              <div class="flex flex-wrap gap-2">
                <span
                  v-for="item in compatibilityStatusLabels(row)"
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
                  {{ t(activeTab === 'standard' ? 'admin.standardApiCompat.empty' : 'admin.apiKeyMimic.empty') }}
                </p>
              </div>
            </template>
          </DataTable>
        </div>
        <CodexModelVisibilityPanel v-else ref="codexPanelRef" />

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
import CodexModelVisibilityPanel from '@/components/admin/CodexModelVisibilityPanel.vue'

const ANTHROPIC_MIMIC_KEY = 'anthropic_apikey_mimic_claude_code'
const ANTHROPIC_STANDARD_API_KEY = 'anthropic_apikey_standard_api_compat'
const OPENAI_MIMIC_KEY = 'openai_apikey_mimic_codex_cli'
const TLS_FINGERPRINT_KEY = 'enable_tls_fingerprint'

type TabKey = 'mimic' | 'standard' | 'codexModels'
type FeatureStatusFilter = 'all' | 'enabled' | 'disabled'

const { t } = useI18n()
const appStore = useAppStore()

const activeTab = ref<TabKey>('mimic')
const codexPanelRef = ref<InstanceType<typeof CodexModelVisibilityPanel> | null>(null)
const activeTabClass = 'bg-primary-600 text-white shadow-sm'
const inactiveTabClass = 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-dark-700'

const mimicAccounts = ref<Account[]>([])
const mimicLoading = ref(false)
const mimicUpdatingIds = ref<Set<number>>(new Set())
const mimicStatusFilter = ref<FeatureStatusFilter>('all')
const standardStatusFilter = ref<FeatureStatusFilter>('all')
const compatibilityStatusFilter = computed({
  get: () => activeTab.value === 'standard' ? standardStatusFilter.value : mimicStatusFilter.value,
  set: (value: FeatureStatusFilter) => {
    if (activeTab.value === 'standard') standardStatusFilter.value = value
    else mimicStatusFilter.value = value
  }
})

const compatibilityLabel = computed(() => t(activeTab.value === 'standard' ? 'admin.standardApiCompat.toggle' : 'admin.apiKeyMimic.columns.compatible'))

const mimicColumns = computed<Column[]>(() => [
  { key: 'account', label: t('admin.apiKeyMimic.columns.account') },
  { key: 'platform', label: t('admin.apiKeyMimic.columns.platform'), sortable: true },
  { key: 'compatible', label: compatibilityLabel.value },
  { key: 'status', label: t('admin.apiKeyMimic.columns.status') }
])

const visibleMimicAccounts = computed(() => mimicAccounts.value
  .filter(account => matchesFeatureStatus(isMimicEnabled(account), mimicStatusFilter.value))
  .sort((a, b) => compareFeatureStatus(isMimicEnabled(a), isMimicEnabled(b), a.id, b.id)))
const visibleStandardAccounts = computed(() => mimicAccounts.value
  .filter(account => account.platform === 'anthropic' && account.type === 'apikey')
  .filter(account => matchesFeatureStatus(isStandardAPICompatEnabled(account), standardStatusFilter.value))
  .sort((a, b) => compareFeatureStatus(isStandardAPICompatEnabled(a), isStandardAPICompatEnabled(b), a.id, b.id)))

function matchesFeatureStatus(enabled: boolean, filter: FeatureStatusFilter): boolean {
  if (filter === 'enabled') return enabled
  if (filter === 'disabled') return !enabled
  return true
}

function compareFeatureStatus(leftEnabled: boolean, rightEnabled: boolean, leftID: number, rightID: number): number {
  if (leftEnabled !== rightEnabled) return leftEnabled ? -1 : 1
  return rightID - leftID
}

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
    if (extra.anthropic_passthrough === true) labels.push(t('admin.apiKeyMimic.statusLabels.passthroughAlsoEnabled'))
    return labels
  }

  labels.push(t('admin.apiKeyMimic.statusLabels.codexCli'))
  if (extra.openai_passthrough === true || extra.openai_oauth_passthrough === true) labels.push(t('admin.apiKeyMimic.statusLabels.passthroughAlsoEnabled'))
  return labels
}

async function fetchAccounts(platform: 'anthropic' | 'openai', type?: string): Promise<Account[]> {
  const pageSize = 100
  let page = 1
  const result: Account[] = []

  while (true) {
    const response = await adminAPI.accounts.list(page, pageSize, {
      platform,
      ...(type ? { type } : {}),
      sort_by: 'id',
      sort_order: 'desc'
    })
    result.push(...response.items)
    if (page >= response.pages || response.items.length === 0) break
    page += 1
  }

  return result
}

async function loadMimicAccounts() {
  try {
    mimicLoading.value = true
    const [anthropicAccounts, openaiAccounts] = await Promise.all([
      fetchAccounts('anthropic', 'apikey'),
      fetchAccounts('openai', 'apikey')
    ])
    mimicAccounts.value = [...anthropicAccounts, ...openaiAccounts]
      .filter(account => account.type === 'apikey' && (account.platform === 'anthropic' || account.platform === 'openai'))
      .sort((a, b) => b.id - a.id)
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || t('admin.apiKeyMimic.loadFailed'))
  } finally {
    mimicLoading.value = false
  }
}

function buildMimicPatch(account: Account, enabled: boolean): Record<string, unknown> {
  if (account.platform === 'anthropic') {
    const patch: Record<string, unknown> = {
      [ANTHROPIC_MIMIC_KEY]: enabled
    }
    if (enabled) {
      patch[TLS_FINGERPRINT_KEY] = true
    }
    return patch
  }

  const patch: Record<string, unknown> = {
    [OPENAI_MIMIC_KEY]: enabled
  }
  if (enabled) {
    patch[TLS_FINGERPRINT_KEY] = true
  }
  return patch
}

async function toggleMimic(account: Account) {
  if (mimicUpdatingIds.value.has(account.id)) return

  const enabled = !isMimicEnabled(account)
  if (enabled && isStandardAPICompatEnabled(account)) {
    appStore.showWarning(t('admin.standardApiCompat.conflict'))
    return
  }
  mimicUpdatingIds.value = new Set(mimicUpdatingIds.value).add(account.id)

  try {
    const updated = await adminAPI.accounts.updateExtra(account.id, buildMimicPatch(account, enabled))
    mimicAccounts.value = mimicAccounts.value.map(item => item.id === updated.id ? updated : item)
    appStore.showSuccess(t('admin.apiKeyMimic.updateSuccess'))
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || t('admin.apiKeyMimic.updateFailed'))
  } finally {
    const next = new Set(mimicUpdatingIds.value)
    next.delete(account.id)
    mimicUpdatingIds.value = next
  }
}

function isStandardAPICompatEnabled(account: Account): boolean {
  return account.platform === 'anthropic' && account.type === 'apikey' && account.extra?.[ANTHROPIC_STANDARD_API_KEY] === true
}

function isCompatibilityEnabled(account: Account): boolean {
  return activeTab.value === 'standard' ? isStandardAPICompatEnabled(account) : isMimicEnabled(account)
}

function isOtherCompatibilityEnabled(account: Account): boolean {
  if (account.platform !== 'anthropic') return false
  return activeTab.value === 'standard' ? isMimicEnabled(account) : isStandardAPICompatEnabled(account)
}

function compatibilityStatusLabels(account: Account): string[] {
  const labels = activeTab.value === 'standard'
    ? [t(isStandardAPICompatEnabled(account) ? 'admin.standardApiCompat.enabled' : 'admin.apiKeyMimic.statusLabels.disabled')]
    : statusLabels(account)
  if (isOtherCompatibilityEnabled(account)) labels.push(t('admin.standardApiCompat.conflict'))
  return labels
}

async function toggleStandardAPICompat(account: Account) {
  if (mimicUpdatingIds.value.has(account.id)) return
  const enabled = !isStandardAPICompatEnabled(account)
  if (enabled && isMimicEnabled(account)) {
    appStore.showWarning(t('admin.standardApiCompat.conflict'))
    return
  }
  mimicUpdatingIds.value = new Set(mimicUpdatingIds.value).add(account.id)
  try {
    const updated = await adminAPI.accounts.updateExtra(account.id, { [ANTHROPIC_STANDARD_API_KEY]: enabled })
    mimicAccounts.value = mimicAccounts.value.map(item => item.id === updated.id ? updated : item)
    appStore.showSuccess(t('admin.standardApiCompat.updateSuccess'))
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || t('admin.standardApiCompat.updateFailed'))
  } finally {
    const next = new Set(mimicUpdatingIds.value)
    next.delete(account.id)
    mimicUpdatingIds.value = next
  }
}

onMounted(() => {
  loadMimicAccounts()
})
</script>
