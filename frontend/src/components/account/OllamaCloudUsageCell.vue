<template>
  <div
    v-if="state?.eligible"
    class="min-w-0 max-w-full space-y-1"
    data-testid="ollama-cloud-usage-cell"
  >
    <!-- 套餐小标签与余额：新版页面（免费 / Pro）与旧版页面都可能带这两项 -->
    <div v-if="plan || balance" class="flex flex-wrap items-center gap-1.5">
      <span
        v-if="plan"
        class="inline-flex items-center rounded px-1.5 py-0.5 text-[10px] font-medium leading-4"
        :class="planChipClass"
        data-testid="ollama-cloud-plan"
      >
        {{ plan }}
      </span>
      <span
        v-if="balance"
        class="text-[10px] font-medium leading-4 text-gray-700 dark:text-gray-200"
        :title="t('admin.accounts.ollamaCloud.balance')"
        data-testid="ollama-cloud-balance"
      >
        {{ balance }}
      </span>
    </div>
    <UsageProgressBar
      v-if="snapshot?.data?.free_usage"
      :label="t('admin.accounts.ollamaCloud.freeUsageShort')"
      :utilization="snapshot.data.free_usage.used_percent"
      :resets-at="snapshot.data.free_usage.reset_at"
      :title="windowTitle(t('admin.accounts.ollamaCloud.freeUsage'), snapshot.data.free_usage)"
      color="amber"
      data-testid="ollama-cloud-free-usage"
    />
    <!-- 月度额度：与 5h/7d 同一进度条组件；英文标签 Monthly 较长，用 auto 宽度避免被固定宽度裁切。
         悬停显示已用金额与额度重置时间 -->
    <UsageProgressBar
      v-if="snapshot?.data?.monthly"
      :label="t('admin.accounts.ollamaCloud.monthlyShort')"
      label-width="auto"
      :utilization="snapshot.data.monthly.used_percent"
      :resets-at="snapshot.data.monthly.reset_at"
      :title="windowTitle(t('admin.accounts.ollamaCloud.monthly'), snapshot.data.monthly)"
      color="purple"
      data-testid="ollama-cloud-monthly"
    />
    <UsageProgressBar
      v-if="snapshot?.data?.five_hour"
      label="5h"
      :utilization="snapshot.data.five_hour.used_percent"
      :resets-at="snapshot.data.five_hour.reset_at"
      color="indigo"
      data-testid="ollama-cloud-five-hour"
    />
    <UsageProgressBar
      v-if="snapshot?.data?.seven_day"
      label="7d"
      :utilization="snapshot.data.seven_day.used_percent"
      :resets-at="snapshot.data.seven_day.reset_at"
      color="emerald"
      data-testid="ollama-cloud-seven-day"
    />
    <!-- Cookie 状态：会话失效红字「Cookie 已过期」，否则显示管理员填写的到期日期（过期红、3 天内黄、其余灰） -->
    <div
      v-if="cookieNotice"
      class="text-[10px] font-medium leading-4"
      :class="cookieNoticeClass"
      :title="state.session_expires_at ? new Date(state.session_expires_at).toLocaleString() : undefined"
      data-testid="ollama-cloud-cookie-status"
    >
      {{ cookieNotice.text }}
    </div>
    <div v-if="state.configured" class="flex items-center pt-0.5">
      <button
        type="button"
        class="inline-flex items-center gap-0.5 rounded px-1.5 py-0.5 text-[10px] font-medium text-blue-600 transition-colors hover:bg-blue-50 disabled:cursor-not-allowed disabled:opacity-50 dark:text-blue-400 dark:hover:bg-blue-900/30"
        :disabled="refreshing"
        data-testid="ollama-cloud-usage-query"
        @click="refreshUsage"
      >
        <svg
          class="h-2.5 w-2.5"
          :class="{ 'animate-spin': refreshing }"
          fill="none"
          stroke="currentColor"
          viewBox="0 0 24 24"
        >
          <path
            stroke-linecap="round"
            stroke-linejoin="round"
            stroke-width="2"
            d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15"
          />
        </svg>
        {{ t('admin.accounts.usageWindow.activeQuery') }}
      </button>
    </div>
  </div>
  <span v-else class="text-sm text-gray-400 dark:text-dark-500">-</span>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { adminAPI } from '@/api/admin'
import type { Account, OllamaCloudUsageState, OllamaCloudUsageWindow } from '@/types'
import { formatOllamaBalance, isOllamaFreePlan, ollamaCookieExpiryDate, ollamaCookieExpiryInfo } from '@/utils/ollamaCloudUsage'
import UsageProgressBar from './UsageProgressBar.vue'

const props = defineProps<{ account: Account }>()
const emit = defineEmits<{ updated: [state: OllamaCloudUsageState] }>()
const { t } = useI18n()
const state = ref(props.account.ollama_cloud_usage)
const refreshing = ref(false)
const snapshot = computed(() => state.value?.snapshot)
// 进度条悬停提示：窗口名称 + 已用金额（有则显示）+ 额度重置时间（约数时加「约」）。
const windowTitle = (name: string, window: OllamaCloudUsageWindow) => {
  const parts = [name]
  if (window.used_text) parts.push(t('admin.accounts.ollamaCloud.windowTooltipUsed', { used: window.used_text }))
  if (window.reset_at) {
    const date = new Date(window.reset_at)
    if (!Number.isNaN(date.getTime())) {
      const reset = date.toLocaleString()
      parts.push(t('admin.accounts.ollamaCloud.windowTooltipReset', {
        reset: window.reset_approx ? t('admin.accounts.ollamaCloud.resetApprox', { reset }) : reset
      }))
    }
  }
  return parts.join('，')
}
const plan = computed(() => snapshot.value?.data?.plan?.trim() || '')
const balance = computed(() => formatOllamaBalance(snapshot.value?.data?.balance))
// 免费计划用灰色小标签，付费计划用紫色，与账号列表平台/类型列的套餐徽章保持一致。
const planChipClass = computed(() => isOllamaFreePlan(plan.value)
  ? 'bg-gray-100 text-gray-600 dark:bg-gray-700 dark:text-gray-300'
  : 'bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-300')
// Cookie 状态行：会话失效（快照 unauthorized）优先显示红字「Cookie 已过期」；
// 否则管理员填了到期时间就显示「Cookie：YYYY-MM-DD 过期」，已过该日期红色、3 天内黄色、其余灰色。
const cookieNotice = computed<{ kind: 'expired' | 'soon' | 'ok'; text: string } | null>(() => {
  if (!state.value?.configured) return null
  if (snapshot.value?.status === 'unauthorized') {
    return { kind: 'expired', text: t('admin.accounts.ollamaCloud.cookieExpiredShort') }
  }
  const expiry = ollamaCookieExpiryInfo(state.value.session_expires_at)
  if (expiry.kind === 'unset') return null
  return {
    kind: expiry.kind,
    text: t('admin.accounts.ollamaCloud.cookieExpiresOn', { date: ollamaCookieExpiryDate(state.value.session_expires_at) })
  }
})
const cookieNoticeClass = computed(() => {
  switch (cookieNotice.value?.kind) {
    case 'expired': return 'text-red-600 dark:text-red-400'
    case 'soon': return 'text-amber-600 dark:text-amber-400'
    default: return 'text-gray-500 dark:text-gray-400'
  }
})

watch(() => props.account.ollama_cloud_usage, (next) => {
  state.value = next
})

const refreshUsage = async () => {
  if (refreshing.value) return
  refreshing.value = true
  try {
    const next = await adminAPI.accounts.refreshOllamaCloudUsage(props.account.id)
    state.value = next
    emit('updated', next)
  } catch (error) {
    console.error('Failed to refresh Ollama Cloud usage:', error)
  } finally {
    refreshing.value = false
  }
}
</script>
