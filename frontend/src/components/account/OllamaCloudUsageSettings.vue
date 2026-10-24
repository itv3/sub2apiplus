<template>
  <section v-if="state?.eligible" class="space-y-4 border-t border-gray-200 pt-4 dark:border-dark-600" data-testid="ollama-cloud-usage-settings">
    <div class="flex items-start justify-between gap-4">
      <div>
        <h3 class="text-sm font-semibold text-gray-900 dark:text-white">
          {{ t('admin.accounts.ollamaCloud.title') }}
        </h3>
        <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
          {{ t('admin.accounts.ollamaCloud.sessionSecurityHint') }}
        </p>
      </div>
      <span
        class="whitespace-nowrap rounded px-2 py-1 text-xs font-medium"
        :class="state.configured
          ? 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
          : 'bg-gray-100 text-gray-600 dark:bg-dark-700 dark:text-gray-300'"
      >
        {{ state.configured ? t('admin.accounts.ollamaCloud.configured') : t('admin.accounts.ollamaCloud.notConfigured') }}
      </span>
    </div>

    <div v-if="loading" class="flex h-20 items-center justify-center text-gray-400">
      <Icon name="refresh" size="sm" class="animate-spin" />
    </div>
    <template v-else>
      <div v-if="!state.encryption_key_configured" class="rounded border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-800/50 dark:bg-amber-900/20 dark:text-amber-200">
        {{ t('admin.accounts.ollamaCloud.encryptionKeyRequired') }}
      </div>

      <div
        v-if="snapshot"
        class="border-y border-gray-100 py-3 dark:border-dark-700"
        data-testid="ollama-cloud-usage-details"
      >
        <div class="grid grid-cols-[minmax(4rem,auto)_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-xs">
          <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.plan') }}</span>
          <span class="break-words text-gray-900 dark:text-white" data-testid="ollama-cloud-detail-plan">{{ snapshot.data?.plan || '-' }}</span>
          <template v-if="snapshot.data?.email">
            <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.email') }}</span>
            <span class="break-all text-gray-900 dark:text-white" data-testid="ollama-cloud-detail-email">{{ snapshot.data.email }}</span>
          </template>
          <!-- 新版页面：免费计划只有免费额度，付费计划只有月度额度；旧版页面保持 5 小时 / 7 天两行 -->
          <template v-if="snapshot.data?.free_usage">
            <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.freeUsage') }}</span>
            <!-- 与账号列表同一进度条组件；百分比取自页面进度条，悬停显示已用金额与额度重置时间 -->
            <div class="min-w-0 space-y-1">
              <UsageProgressBar
                :label="t('admin.accounts.ollamaCloud.freeUsageShort')"
                :utilization="snapshot.data.free_usage.used_percent"
                :resets-at="snapshot.data.free_usage.reset_at"
                :title="windowTitle(t('admin.accounts.ollamaCloud.freeUsage'), snapshot.data.free_usage)"
                color="amber"
                data-testid="ollama-cloud-detail-free-usage-bar"
              />
              <span class="break-words text-gray-900 dark:text-white" data-testid="ollama-cloud-detail-free-usage">{{ windowSummary(snapshot.data.free_usage) }}</span>
            </div>
          </template>
          <template v-if="snapshot.data?.monthly">
            <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.monthly') }}</span>
            <div class="min-w-0 space-y-1">
              <UsageProgressBar
                :label="t('admin.accounts.ollamaCloud.monthlyShort')"
                label-width="auto"
                :utilization="snapshot.data.monthly.used_percent"
                :resets-at="snapshot.data.monthly.reset_at"
                :title="windowTitle(t('admin.accounts.ollamaCloud.monthly'), snapshot.data.monthly)"
                color="purple"
                data-testid="ollama-cloud-detail-monthly-bar"
              />
              <span class="break-words text-gray-900 dark:text-white" data-testid="ollama-cloud-detail-monthly">{{ windowSummary(snapshot.data.monthly) }}</span>
            </div>
          </template>
          <template v-if="showLegacyWindows">
            <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.fiveHour') }}</span>
            <span class="break-words text-gray-900 dark:text-white">{{ windowSummary(snapshot.data?.five_hour) }}</span>
            <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.sevenDay') }}</span>
            <span class="break-words text-gray-900 dark:text-white">{{ windowSummary(snapshot.data?.seven_day) }}</span>
          </template>
          <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.balance') }}</span>
          <span class="break-words text-gray-900 dark:text-white">{{ formatOllamaBalance(snapshot.data?.balance) || '-' }}</span>
          <template v-if="snapshot.data?.free_models?.length">
            <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.freeModels') }}</span>
            <span class="break-words text-gray-900 dark:text-white">{{ snapshot.data.free_models.join(', ') }}</span>
          </template>
          <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.models') }}</span>
          <span class="break-words text-gray-900 dark:text-white">{{ modelSummary }}</span>
          <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.status') }}</span>
          <span class="break-words font-medium text-gray-900 dark:text-white">{{ statusLabel }}</span>
          <span class="text-gray-500 dark:text-gray-400">{{ t('admin.accounts.ollamaCloud.updatedAt') }}</span>
          <span class="break-words text-gray-900 dark:text-white">{{ formatDate(snapshot.fetched_at || snapshot.last_attempt_at) }}</span>
        </div>
        <p v-if="snapshot.last_error" class="mt-2 break-words border-t border-gray-100 pt-2 text-xs text-amber-700 dark:border-dark-700 dark:text-amber-300">
          {{ t(`admin.accounts.ollamaCloud.errors.${snapshot.last_error}`, snapshot.last_error) }}
        </p>
      </div>

      <div>
        <label class="input-label" for="ollama-cloud-session">{{ t('admin.accounts.ollamaCloud.sessionLabel') }}</label>
        <textarea
          id="ollama-cloud-session"
          v-model="session"
          rows="3"
          class="input font-mono text-xs"
          autocomplete="new-password"
          data-1p-ignore
          data-lpignore="true"
          data-bwignore="true"
          :placeholder="t('admin.accounts.ollamaCloud.sessionPlaceholder')"
        />
        <p class="input-hint">{{ t('admin.accounts.ollamaCloud.writeOnlyHint') }}</p>
      </div>

      <!-- Cookie 到期时间：管理员从浏览器 Application → Cookies → Expires 列抄来，非机密、可回显；
           网关无法自行得知。可随新 Cookie 一起保存，也可单独更新而不重填 Cookie。 -->
      <div data-testid="ollama-cloud-expiry">
        <label class="input-label" for="ollama-cloud-expiry-input">{{ t('admin.accounts.ollamaCloud.cookieExpiresLabel') }}</label>
        <div class="flex flex-wrap items-center gap-2">
          <input
            id="ollama-cloud-expiry-input"
            v-model="expiresInput"
            type="datetime-local"
            class="input w-auto text-xs"
            data-testid="ollama-cloud-expiry-input"
          />
          <button
            v-if="state.configured"
            type="button"
            class="btn btn-secondary btn-sm"
            :disabled="saving || !expiryDirty"
            data-testid="ollama-cloud-expiry-save"
            @click="saveExpiry"
          >
            {{ expiresInput ? t('admin.accounts.ollamaCloud.saveExpiry') : t('admin.accounts.ollamaCloud.clearExpiry') }}
          </button>
          <span
            v-if="state.configured"
            class="text-xs"
            :class="expiryInfo.kind === 'expired' ? 'font-medium text-red-600 dark:text-red-400' : expiryInfo.kind === 'soon' ? 'font-medium text-amber-600 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'"
            data-testid="ollama-cloud-expiry-remaining"
          >
            {{ expiryText }}
          </span>
        </div>
        <p class="input-hint">{{ t('admin.accounts.ollamaCloud.cookieExpiresHint') }}</p>
        <p v-if="state.configured" class="input-hint">{{ t('admin.accounts.ollamaCloud.sessionOptionalHint') }}</p>
      </div>

      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="btn btn-primary btn-sm"
          :disabled="saving || !session.trim() || !state.encryption_key_configured"
          data-testid="ollama-cloud-session-save"
          @click="saveSession"
        >
          <Icon name="check" size="xs" class="mr-1.5" />
          {{ t('common.save') }}
        </button>
        <button
          v-if="state.configured"
          type="button"
          class="btn btn-secondary btn-sm text-red-600 dark:text-red-400"
          :disabled="saving"
          data-testid="ollama-cloud-session-delete"
          @click="showDeleteConfirm = true"
        >
          <Icon name="trash" size="xs" class="mr-1.5" />
          {{ t('admin.accounts.ollamaCloud.deleteSession') }}
        </button>
        <button
          v-if="state.configured"
          type="button"
          class="btn btn-secondary btn-sm"
          :disabled="refreshing"
          data-testid="ollama-cloud-refresh"
          @click="refreshUsage"
        >
          <Icon name="refresh" size="xs" class="mr-1.5" :class="{ 'animate-spin': refreshing }" />
          {{ t('admin.accounts.ollamaCloud.refreshNow') }}
        </button>
      </div>

      <div v-if="state.configured" class="flex items-center justify-between gap-4 border-t border-gray-100 pt-4 dark:border-dark-700">
        <div>
          <label class="text-sm font-medium text-gray-900 dark:text-white">
            {{ t('admin.accounts.ollamaCloud.autoRefresh') }}
          </label>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ t('admin.accounts.ollamaCloud.autoRefreshHint') }}
          </p>
        </div>
        <Toggle
          :model-value="state.auto_refresh_enabled"
          :disabled="saving"
          data-testid="ollama-cloud-auto-refresh"
          @update:model-value="setAutoRefresh"
        />
      </div>
    </template>

    <ConfirmDialog
      :show="showDeleteConfirm"
      :title="t('admin.accounts.ollamaCloud.deleteSession')"
      :message="t('admin.accounts.ollamaCloud.deleteConfirm')"
      :confirm-text="t('common.delete')"
      :cancel-text="t('common.cancel')"
      :danger="true"
      @confirm="deleteSession"
      @cancel="showDeleteConfirm = false"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { adminAPI } from '@/api/admin'
import { useAppStore } from '@/stores/app'
import { extractApiErrorMessage, extractI18nErrorMessage } from '@/utils/apiError'
import type { Account, OllamaCloudUsageState, OllamaCloudUsageWindow } from '@/types'
import { formatOllamaBalance, ollamaCookieExpiryDate, ollamaCookieExpiryInfo } from '@/utils/ollamaCloudUsage'
import UsageProgressBar from './UsageProgressBar.vue'
import ConfirmDialog from '@/components/common/ConfirmDialog.vue'
import Toggle from '@/components/common/Toggle.vue'
import Icon from '@/components/icons/Icon.vue'

const props = defineProps<{ account: Account }>()
const emit = defineEmits<{ updated: [state: OllamaCloudUsageState] }>()
const { t } = useI18n()
const appStore = useAppStore()
const state = ref<OllamaCloudUsageState | null>(props.account.ollama_cloud_usage ?? null)
const session = ref('')
const expiresInput = ref('')
const loading = ref(false)
const saving = ref(false)
const refreshing = ref(false)
const showDeleteConfirm = ref(false)
const snapshot = computed(() => state.value?.snapshot)
const statusLabel = computed(() => {
  if (!snapshot.value) return t('admin.accounts.ollamaCloud.notRefreshed')
  if (snapshot.value.status === 'unauthorized') return t('admin.accounts.ollamaCloud.unauthorized')
  if (snapshot.value.status === 'failed') return t('admin.accounts.ollamaCloud.failed')
  return t('admin.accounts.ollamaCloud.ok')
})
const modelSummary = computed(() => snapshot.value?.data?.models?.map(model => {
  const window = model.window === 'five_hour'
    ? t('admin.accounts.ollamaCloud.fiveHourShort')
    : model.window === 'monthly'
      ? t('admin.accounts.ollamaCloud.monthlyShort')
      : t('admin.accounts.ollamaCloud.sevenDayShort')
  return `${window} ${model.model}: ${model.requests}`
}).join(', ') || '-')
// 旧版页面（有 5 小时 / 7 天窗口）或新版窗口都没读到时，保持原来的两行（读不到显示 -）；
// 新版页面只有免费额度或月度额度时不再显示这两行。
const showLegacyWindows = computed(() => {
  const data = snapshot.value?.data
  return !!(data?.five_hour || data?.seven_day) || !(data?.free_usage || data?.monthly)
})

// datetime-local 控件需要「本地时区的 YYYY-MM-DDTHH:mm」；后端存 UTC 的 RFC3339。
const toLocalInput = (value?: string) => {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}
const inputToISO = (value: string): string | null => {
  if (!value) return null
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? null : date.toISOString()
}
// 与账号列表用量列同一格式：会话已失效显示红字「Cookie 已过期」；否则显示「Cookie：YYYY-MM-DD 过期」，
// 已过该日期红色、3 天内黄色、其余灰色；没填写显示「未填写」。
const expiryInfo = computed(() => {
  if (snapshot.value?.status === 'unauthorized') return { kind: 'expired' as const, days: 0 }
  return ollamaCookieExpiryInfo(state.value?.session_expires_at)
})
const expiryText = computed(() => {
  if (snapshot.value?.status === 'unauthorized') return t('admin.accounts.ollamaCloud.cookieExpiredShort')
  if (expiryInfo.value.kind === 'unset') return t('admin.accounts.ollamaCloud.cookieExpiresUnset')
  return t('admin.accounts.ollamaCloud.cookieExpiresOn', { date: ollamaCookieExpiryDate(state.value?.session_expires_at) })
})
// 输入框内容与已保存值（精确到分钟）不同才允许单独保存。
const expiryDirty = computed(() => expiresInput.value !== toLocalInput(state.value?.session_expires_at))

const formatPercent = (value?: number) => typeof value === 'number' && Number.isFinite(value)
  ? `${value.toFixed(value % 1 ? 1 : 0)}%`
  : '-'
const formatDate = (value?: string) => {
  if (!value) return '-'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}
const windowSummary = (window?: OllamaCloudUsageWindow) => {
  if (!window) return '-'
  let reset = window.reset_at ? formatDate(window.reset_at) : window.reset_text
  // 页面只给相对时间时，重置时间是推算的约数，显示时标注「约」。
  if (reset && window.reset_at && window.reset_approx) {
    reset = t('admin.accounts.ollamaCloud.resetApprox', { reset })
  }
  const percent = window.used_text
    ? t('admin.accounts.ollamaCloud.monthlyUsed', { used: window.used_text, percent: formatPercent(window.used_percent) })
    : formatPercent(window.used_percent)
  return reset ? t('admin.accounts.ollamaCloud.windowWithReset', { percent, reset }) : percent
}

// 进度条悬停提示：窗口名称 + 已用金额（有则显示）+ 额度重置时间（约数时加「约」）。
const windowTitle = (name: string, window: OllamaCloudUsageWindow) => {
  const parts = [name]
  if (window.used_text) parts.push(t('admin.accounts.ollamaCloud.windowTooltipUsed', { used: window.used_text }))
  if (window.reset_at) {
    const reset = formatDate(window.reset_at)
    parts.push(t('admin.accounts.ollamaCloud.windowTooltipReset', {
      reset: window.reset_approx ? t('admin.accounts.ollamaCloud.resetApprox', { reset }) : reset
    }))
  }
  return parts.join('，')
}

const applyState = (next: OllamaCloudUsageState) => {
  state.value = next
  expiresInput.value = toLocalInput(next.session_expires_at)
  emit('updated', next)
}

const load = async () => {
  loading.value = true
  try {
    applyState(await adminAPI.accounts.getOllamaCloudUsage(props.account.id))
  } catch (error) {
    appStore.showError(extractApiErrorMessage(error, t('admin.accounts.ollamaCloud.loadFailed')))
  } finally {
    loading.value = false
  }
}

const saveSession = async () => {
  if (!session.value.trim()) return
  saving.value = true
  try {
    // 新 Cookie 一并保存输入框里的到期时间；留空表示不填（旧到期时间随旧会话清除）。
    const expiresAt = inputToISO(expiresInput.value)
    applyState(expiresAt
      ? await adminAPI.accounts.saveOllamaCloudUsageSession(props.account.id, session.value, expiresAt)
      : await adminAPI.accounts.saveOllamaCloudUsageSession(props.account.id, session.value))
    session.value = ''
    appStore.showSuccess(t('admin.accounts.ollamaCloud.sessionSaved'))
  } catch (error) {
    appStore.showError(extractApiErrorMessage(error, t('admin.accounts.ollamaCloud.sessionSaveFailed')))
  } finally {
    saving.value = false
  }
}

// 只更新到期时间（输入框为空则清除），不重填 Cookie。
const saveExpiry = async () => {
  saving.value = true
  try {
    const expiresAt = inputToISO(expiresInput.value)
    applyState(await adminAPI.accounts.setOllamaCloudUsageSessionExpiry(props.account.id, expiresAt))
    appStore.showSuccess(t(expiresAt ? 'admin.accounts.ollamaCloud.expirySaved' : 'admin.accounts.ollamaCloud.expiryCleared'))
  } catch (error) {
    appStore.showError(extractApiErrorMessage(error, t('admin.accounts.ollamaCloud.expirySaveFailed')))
  } finally {
    saving.value = false
  }
}

const deleteSession = async () => {
  saving.value = true
  showDeleteConfirm.value = false
  try {
    applyState(await adminAPI.accounts.deleteOllamaCloudUsageSession(props.account.id))
    session.value = ''
    appStore.showSuccess(t('admin.accounts.ollamaCloud.sessionDeleted'))
  } catch (error) {
    appStore.showError(extractApiErrorMessage(error, t('admin.accounts.ollamaCloud.sessionDeleteFailed')))
  } finally {
    saving.value = false
  }
}

const setAutoRefresh = async (enabled: boolean) => {
  saving.value = true
  try {
    applyState(await adminAPI.accounts.setOllamaCloudUsageAutoRefresh(props.account.id, enabled))
  } catch (error) {
    appStore.showError(extractApiErrorMessage(error, t('admin.accounts.ollamaCloud.autoRefreshFailed')))
  } finally {
    saving.value = false
  }
}

const refreshUsage = async () => {
  refreshing.value = true
  try {
    applyState(await adminAPI.accounts.refreshOllamaCloudUsage(props.account.id))
    appStore.showSuccess(t('admin.accounts.ollamaCloud.refreshSuccess'))
  } catch (error) {
    appStore.showError(extractI18nErrorMessage(
      error,
      t,
      'admin.accounts.ollamaCloud.errors',
      t('admin.accounts.ollamaCloud.refreshFailed')
    ))
  } finally {
    refreshing.value = false
  }
}

watch(() => props.account.id, () => {
  state.value = props.account.ollama_cloud_usage ?? null
  session.value = ''
  expiresInput.value = toLocalInput(state.value?.session_expires_at)
  if (!state.value) void load()
})

onMounted(() => {
  expiresInput.value = toLocalInput(state.value?.session_expires_at)
  if (!state.value) void load()
})
</script>
