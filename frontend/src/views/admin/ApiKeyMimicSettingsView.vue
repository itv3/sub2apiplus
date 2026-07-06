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
            :disabled="currentLoading"
            :title="t(activeTab === 'mimic' ? 'admin.apiKeyMimic.refresh' : 'admin.accountKeepalive.refresh')"
            @click="activeTab === 'mimic' ? loadMimicAccounts() : loadKeepaliveAccounts()"
          >
            <Icon name="refresh" size="md" :class="currentLoading ? 'animate-spin' : ''" />
            <span class="hidden sm:inline">{{ t(activeTab === 'mimic' ? 'admin.apiKeyMimic.refresh' : 'admin.accountKeepalive.refresh') }}</span>
          </button>
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
            :class="activeTab === 'keepalive' ? activeTabClass : inactiveTabClass"
            @click="activeTab = 'keepalive'"
          >
            {{ t('admin.plusEnhancements.tabs.keepalive') }}
          </button>
        </div>
      </template>

      <template #table>
        <DataTable
          v-if="activeTab === 'mimic'"
          :columns="mimicColumns"
          :data="mimicAccounts"
          :loading="mimicLoading"
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
            <span class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium" :class="platformBadgeClass(row.platform)">
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
              :disabled="mimicUpdatingIds.has(row.id)"
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

        <div v-else class="space-y-4">
          <div class="rounded-lg border border-blue-200 bg-blue-50 px-4 py-3 text-sm text-blue-800 dark:border-blue-900/60 dark:bg-blue-950/30 dark:text-blue-200">
            {{ t('admin.accountKeepalive.labels.sidecarHint') }}
          </div>

          <DataTable
            :columns="keepaliveColumns"
            :data="keepaliveRows"
            :loading="keepaliveLoading"
            default-sort-key="id"
            default-sort-order="desc"
          >
            <template #cell-account="{ row }">
              <div class="min-w-0">
                <div class="truncate font-medium text-gray-900 dark:text-white">{{ row.account.name }}</div>
                <div class="mt-1 text-xs text-gray-500 dark:text-gray-400">#{{ row.account.id }}</div>
              </div>
            </template>

            <template #cell-platform="{ row }">
              <div class="space-y-1">
                <span class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium" :class="platformBadgeClass(row.account.platform)">
                  {{ platformLabel(row.account.platform) }}
                </span>
                <div class="text-xs text-gray-500 dark:text-gray-400">{{ row.account.type }}</div>
              </div>
            </template>

            <template #cell-enabled="{ row }">
              <button
                type="button"
                role="switch"
                class="relative inline-flex h-6 w-11 flex-shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 dark:focus:ring-offset-dark-800"
                :class="row.form.enabled ? 'bg-primary-600' : 'bg-gray-200 dark:bg-dark-600'"
                :aria-checked="row.form.enabled"
                :aria-label="`${row.account.name} ${t('admin.accountKeepalive.columns.enabled')}`"
                :disabled="keepaliveUpdatingIds.has(row.account.id)"
                @click.stop="row.form.enabled = !row.form.enabled"
              >
                <span
                  class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out"
                  :class="row.form.enabled ? 'translate-x-5' : 'translate-x-0'"
                />
              </button>
            </template>

            <template #cell-interval="{ row }">
              <input
                v-model.number="row.form.intervalMinutes"
                type="number"
                min="5"
                step="5"
                class="input h-9 w-24"
                :disabled="keepaliveUpdatingIds.has(row.account.id)"
              />
            </template>

            <template #cell-model="{ row }">
              <input
                v-model.trim="row.form.model"
                type="text"
                class="input h-9 min-w-[9rem]"
                :placeholder="t('admin.accountKeepalive.placeholders.model')"
                :disabled="keepaliveUpdatingIds.has(row.account.id)"
              />
            </template>

            <template #cell-executor="{ row }">
              <select v-model="row.form.executor" class="input h-9 min-w-[7rem]" :disabled="keepaliveUpdatingIds.has(row.account.id)">
                <option value="codex">codex</option>
                <option value="claude">claude</option>
              </select>
            </template>

            <template #cell-workspace="{ row }">
              <input
                v-model.trim="row.form.workspace"
                type="text"
                class="input h-9 min-w-[9rem]"
                :placeholder="t('admin.accountKeepalive.placeholders.workspace')"
                :disabled="keepaliveUpdatingIds.has(row.account.id)"
              />
            </template>

            <template #cell-schedule="{ row }">
              <div class="space-y-1 text-xs text-gray-600 dark:text-gray-300">
                <div>{{ t('admin.accountKeepalive.labels.lastUsed') }}：{{ formatDateTime(row.account.last_used_at) }}</div>
                <div>{{ t('admin.accountKeepalive.labels.nextRun') }}：{{ formatDateTime(nextKeepaliveAt(row)) }}</div>
                <div>{{ t('admin.accountKeepalive.labels.workWindow') }}：{{ row.form.workStart }} - {{ row.form.workEnd }}</div>
              </div>
            </template>

            <template #cell-status="{ row }">
              <div class="space-y-1">
                <span
                  class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium"
                  :class="isKeepaliveDue(row) ? 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300' : 'bg-gray-100 text-gray-700 dark:bg-dark-700 dark:text-gray-200'"
                >
                  {{ row.form.enabled ? (isKeepaliveDue(row) ? t('admin.accountKeepalive.labels.due') : t('admin.accountKeepalive.labels.waiting')) : t('admin.accountKeepalive.labels.disabled') }}
                </span>
                <div class="max-w-[18rem] truncate text-xs text-gray-500 dark:text-gray-400">
                  {{ String(row.account.extra?.keeper_last_status || t('admin.accountKeepalive.labels.noResult')) }}
                </div>
              </div>
            </template>

            <template #cell-actions="{ row }">
              <button
                type="button"
                class="btn btn-primary btn-sm"
                :disabled="keepaliveUpdatingIds.has(row.account.id)"
                @click="saveKeepalive(row)"
              >
                {{ t('admin.accountKeepalive.save') }}
              </button>
            </template>

            <template #empty>
              <div class="flex flex-col items-center py-6 text-gray-500 dark:text-gray-400">
                <Icon name="inbox" size="xl" class="mb-4 h-12 w-12 text-gray-400 dark:text-dark-500" />
                <p class="text-lg font-medium text-gray-900 dark:text-gray-100">
                  {{ t('admin.accountKeepalive.labels.empty') }}
                </p>
              </div>
            </template>
          </DataTable>
        </div>
      </template>
    </TablePageLayout>
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
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

const KEEPER_ENABLED_KEY = 'keeper_keepalive_enabled'
const KEEPER_INTERVAL_KEY = 'keeper_keepalive_interval_minutes'
const KEEPER_MODEL_KEY = 'keeper_keepalive_model'
const KEEPER_EXECUTOR_KEY = 'keeper_keepalive_executor'
const KEEPER_WORKSPACE_KEY = 'keeper_keepalive_workspace'
const KEEPER_WORK_START_KEY = 'keeper_keepalive_work_start'
const KEEPER_WORK_END_KEY = 'keeper_keepalive_work_end'

type TabKey = 'mimic' | 'keepalive'

interface KeepaliveForm {
  enabled: boolean
  intervalMinutes: number
  model: string
  executor: 'codex' | 'claude'
  workspace: string
  workStart: string
  workEnd: string
}

interface KeepaliveRow {
  id: number
  account: Account
  form: KeepaliveForm
}

const { t } = useI18n()
const appStore = useAppStore()

const activeTab = ref<TabKey>('mimic')
const activeTabClass = 'bg-primary-600 text-white shadow-sm'
const inactiveTabClass = 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-dark-700'

const mimicAccounts = ref<Account[]>([])
const mimicLoading = ref(false)
const mimicUpdatingIds = ref<Set<number>>(new Set())

const keepaliveRows = ref<KeepaliveRow[]>([])
const keepaliveLoading = ref(false)
const keepaliveUpdatingIds = ref<Set<number>>(new Set())

const currentLoading = computed(() => activeTab.value === 'mimic' ? mimicLoading.value : keepaliveLoading.value)

const mimicColumns = computed<Column[]>(() => [
  { key: 'account', label: t('admin.apiKeyMimic.columns.account') },
  { key: 'platform', label: t('admin.apiKeyMimic.columns.platform'), sortable: true },
  { key: 'compatible', label: t('admin.apiKeyMimic.columns.compatible') },
  { key: 'status', label: t('admin.apiKeyMimic.columns.status') }
])

const keepaliveColumns = computed<Column[]>(() => [
  { key: 'account', label: t('admin.accountKeepalive.columns.account') },
  { key: 'platform', label: t('admin.accountKeepalive.columns.platform') },
  { key: 'enabled', label: t('admin.accountKeepalive.columns.enabled') },
  { key: 'interval', label: t('admin.accountKeepalive.columns.interval') },
  { key: 'model', label: t('admin.accountKeepalive.columns.model') },
  { key: 'executor', label: t('admin.accountKeepalive.columns.executor') },
  { key: 'workspace', label: t('admin.accountKeepalive.columns.workspace') },
  { key: 'schedule', label: t('admin.accountKeepalive.columns.schedule') },
  { key: 'status', label: t('admin.accountKeepalive.columns.status') },
  { key: 'actions', label: t('admin.accountKeepalive.columns.actions') }
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
    if (extra.anthropic_passthrough === true) labels.push(t('admin.apiKeyMimic.statusLabels.passthroughAlsoEnabled'))
    return labels
  }

  const profile = String(extra[OPENAI_PROFILE_KEY] || DEFAULT_CODEX_PROFILE)
  if (profile === DEFAULT_CODEX_PROFILE) labels.push(t('admin.apiKeyMimic.statusLabels.codexDesktop'))
  else if (profile === 'cli_rs_0_125') labels.push(t('admin.apiKeyMimic.statusLabels.codexCli'))
  else labels.push(t('admin.apiKeyMimic.statusLabels.unknownCodexProfile', { profile }))
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

async function loadKeepaliveAccounts() {
  try {
    keepaliveLoading.value = true
    const [anthropicAccounts, openaiAccounts] = await Promise.all([
      fetchAccounts('anthropic'),
      fetchAccounts('openai')
    ])
    keepaliveRows.value = [...anthropicAccounts, ...openaiAccounts]
      .filter(account => account.platform === 'anthropic' || account.platform === 'openai')
      .sort((a, b) => b.id - a.id)
      .map(account => ({ id: account.id, account, form: buildKeepaliveForm(account) }))
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || t('admin.accountKeepalive.messages.loadFailed'))
  } finally {
    keepaliveLoading.value = false
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
  if (mimicUpdatingIds.value.has(account.id)) return

  const enabled = !isMimicEnabled(account)
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

function buildKeepaliveForm(account: Account): KeepaliveForm {
  const extra = account.extra || {}
  const defaultExecutor = account.platform === 'anthropic' ? 'claude' : 'codex'
  return {
    enabled: extra[KEEPER_ENABLED_KEY] === true,
    intervalMinutes: normalizeInterval(extra[KEEPER_INTERVAL_KEY]),
    model: typeof extra[KEEPER_MODEL_KEY] === 'string' ? extra[KEEPER_MODEL_KEY] : '',
    executor: extra[KEEPER_EXECUTOR_KEY] === 'claude' ? 'claude' : defaultExecutor,
    workspace: typeof extra[KEEPER_WORKSPACE_KEY] === 'string' ? extra[KEEPER_WORKSPACE_KEY] : '',
    workStart: typeof extra[KEEPER_WORK_START_KEY] === 'string' ? extra[KEEPER_WORK_START_KEY] : '04:00',
    workEnd: typeof extra[KEEPER_WORK_END_KEY] === 'string' ? extra[KEEPER_WORK_END_KEY] : '24:00'
  }
}

function normalizeInterval(value: unknown): number {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return 90
  return Math.max(5, Math.round(n))
}

function nextKeepaliveAt(row: KeepaliveRow): string | null {
  const base = latestDate(row.account.last_used_at, String(row.account.extra?.keeper_last_keepalive_at || ''))
  if (!base) return null
  return new Date(base.getTime() + row.form.intervalMinutes * 60_000).toISOString()
}

function isKeepaliveDue(row: KeepaliveRow): boolean {
  if (!row.form.enabled) return false
  const next = nextKeepaliveAt(row)
  if (!next) return true
  return new Date(next).getTime() <= Date.now()
}

function latestDate(...values: Array<string | null | undefined>): Date | null {
  let latest: Date | null = null
  for (const value of values) {
    if (!value) continue
    const date = new Date(value)
    if (!Number.isFinite(date.getTime())) continue
    if (!latest || date.getTime() > latest.getTime()) latest = date
  }
  return latest
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return t('admin.accountKeepalive.labels.neverUsed')
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '-'
  return date.toLocaleString()
}

function buildKeepalivePatch(row: KeepaliveRow): Record<string, unknown> {
  return {
    [KEEPER_ENABLED_KEY]: row.form.enabled,
    [KEEPER_INTERVAL_KEY]: normalizeInterval(row.form.intervalMinutes),
    [KEEPER_MODEL_KEY]: row.form.model,
    [KEEPER_EXECUTOR_KEY]: row.form.executor,
    [KEEPER_WORKSPACE_KEY]: row.form.workspace,
    [KEEPER_WORK_START_KEY]: row.form.workStart,
    [KEEPER_WORK_END_KEY]: row.form.workEnd
  }
}

async function saveKeepalive(row: KeepaliveRow) {
  if (keepaliveUpdatingIds.value.has(row.account.id)) return
  keepaliveUpdatingIds.value = new Set(keepaliveUpdatingIds.value).add(row.account.id)

  try {
    const updated = await adminAPI.accounts.updateExtra(row.account.id, buildKeepalivePatch(row))
    keepaliveRows.value = keepaliveRows.value.map(item => item.account.id === updated.id ? { id: updated.id, account: updated, form: buildKeepaliveForm(updated) } : item)
    appStore.showSuccess(t('admin.accountKeepalive.messages.saveSuccess'))
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || t('admin.accountKeepalive.messages.saveFailed'))
  } finally {
    const next = new Set(keepaliveUpdatingIds.value)
    next.delete(row.account.id)
    keepaliveUpdatingIds.value = next
  }
}

watch(activeTab, (tab) => {
  if (tab === 'keepalive' && keepaliveRows.value.length === 0) {
    loadKeepaliveAccounts()
  }
})

onMounted(() => {
  loadMimicAccounts()
})
</script>
