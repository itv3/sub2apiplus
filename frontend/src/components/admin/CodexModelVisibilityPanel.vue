<template>
  <!-- 面板放在表格页布局的固定高度卡片里（该卡片隐藏溢出），必须自己纵向滚动，否则下方模型表格会被截掉 -->
  <div class="min-h-0 flex-1 space-y-4 overflow-y-auto p-4" data-testid="codex-model-visibility-panel">
    <!-- 功能说明：启用只决定能否在 Codex 中选到，不改变调用权限 -->
    <div class="rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600 dark:border-dark-700 dark:bg-dark-800 dark:text-gray-300">
      <p>{{ t('admin.codexThirdPartyModels.notice') }}</p>
      <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ t('admin.codexThirdPartyModels.clientHint') }}</p>
    </div>

    <!-- 分组选择与整体操作 -->
    <div class="flex flex-wrap items-end justify-between gap-3">
      <div class="min-w-0 flex-1 sm:flex-none">
        <label for="codex-third-party-group" class="mb-1 block text-sm font-medium text-gray-700 dark:text-gray-200">
          {{ t('admin.codexThirdPartyModels.group.label') }}
        </label>
        <select
          id="codex-third-party-group"
          v-model="selectedGroupValue"
          data-testid="codex-group-select"
          class="input h-9 w-full sm:w-80"
          :disabled="groupsLoading || busy"
        >
          <option value="" disabled>{{ t('admin.codexThirdPartyModels.group.placeholder') }}</option>
          <option v-for="group in eligibleGroups" :key="group.id" :value="String(group.id)">
            {{ group.name }} · {{ platformDisplayName(group.platform) }}
          </option>
        </select>
      </div>
      <div class="flex flex-wrap items-center gap-2">
        <button
          type="button"
          class="btn btn-secondary"
          data-testid="codex-refresh-button"
          :disabled="!state || loading || busy"
          @click="reload()"
        >
          <Icon name="refresh" size="md" :class="loading ? 'animate-spin' : ''" />
          <span class="hidden sm:inline">{{ t('admin.codexThirdPartyModels.actions.refresh') }}</span>
        </button>
      </div>
    </div>

    <p v-if="!groupsLoading && eligibleGroups.length === 0" data-testid="codex-no-groups" class="text-sm text-gray-500 dark:text-gray-400">
      {{ t('admin.codexThirdPartyModels.group.empty') }}
    </p>

    <!-- 保存失败等反馈：回显具体原因 -->
    <div
      v-if="errorMessage"
      role="alert"
      data-testid="codex-error"
      class="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300"
    >
      <p>{{ errorMessage }}</p>
      <ul v-if="rejections.length > 0" class="mt-2 list-disc space-y-1 pl-5 text-xs" data-testid="codex-rejections">
        <li v-for="item in rejections" :key="item.key">
          <span class="font-medium">{{ item.label }}</span>：{{ item.text }}
        </li>
      </ul>
    </div>

    <div v-if="!selectedGroupId" class="rounded-lg border border-dashed border-gray-300 p-8 text-center text-sm text-gray-500 dark:border-dark-600 dark:text-gray-400">
      {{ t('admin.codexThirdPartyModels.group.choosePrompt') }}
    </div>

    <div v-else-if="loading && !state" class="p-8 text-center text-sm text-gray-500 dark:text-gray-400" data-testid="codex-loading">
      {{ t('admin.codexThirdPartyModels.loading') }}
    </div>

    <template v-else-if="state">
      <!-- 目录预算 -->
      <section
        class="rounded-lg border border-gray-200 p-4 dark:border-dark-700"
        data-testid="codex-budget"
        :aria-label="t('admin.codexThirdPartyModels.budget.title')"
      >
        <div class="flex flex-wrap items-center justify-between gap-2">
          <h3 class="text-sm font-semibold text-gray-900 dark:text-white">{{ t('admin.codexThirdPartyModels.budget.title') }}</h3>
          <span
            v-if="budget.deferred"
            data-testid="codex-budget-deferred"
            class="inline-flex rounded-full bg-sky-100 px-2.5 py-1 text-xs font-medium text-sky-700 dark:bg-sky-900/30 dark:text-sky-300"
          >
            {{ t('admin.codexThirdPartyModels.budget.deferred') }}
          </span>
          <span
            v-else-if="!budget.complete"
            data-testid="codex-budget-incomplete"
            class="inline-flex rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-800 dark:bg-amber-900/30 dark:text-amber-300"
          >
            {{ t('admin.codexThirdPartyModels.budget.incomplete') }}
          </span>
          <span
            v-else-if="budgetOverLimit"
            data-testid="codex-budget-over"
            class="inline-flex rounded-full bg-red-100 px-2.5 py-1 text-xs font-medium text-red-700 dark:bg-red-900/30 dark:text-red-300"
          >
            {{ t('admin.codexThirdPartyModels.budget.overLimit') }}
          </span>
          <span
            v-else
            data-testid="codex-budget-within"
            class="inline-flex rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300"
          >
            {{ t('admin.codexThirdPartyModels.budget.withinLimit') }}
          </span>
        </div>
        <p v-if="budget.deferred" class="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid="codex-budget-deferred-hint">
          {{ t('admin.codexThirdPartyModels.budget.deferredHint') }}
        </p>
        <p v-else-if="!budget.complete" class="mt-2 text-xs text-amber-700 dark:text-amber-300" data-testid="codex-budget-reason">
          {{ budget.reason || t('admin.codexThirdPartyModels.budget.incompleteHint') }}
        </p>
        <dl v-if="!budget.deferred" class="mt-3 grid grid-cols-2 gap-3 text-sm md:grid-cols-4">
          <div>
            <dt class="text-xs text-gray-500 dark:text-gray-400">{{ t('admin.codexThirdPartyModels.budget.nativeCount') }}</dt>
            <dd class="font-medium text-gray-900 dark:text-white" data-testid="codex-budget-native">{{ budget.native_count }}</dd>
          </div>
          <div>
            <dt class="text-xs text-gray-500 dark:text-gray-400">{{ t('admin.codexThirdPartyModels.budget.enabledCount') }}</dt>
            <dd class="font-medium text-gray-900 dark:text-white" data-testid="codex-budget-enabled">{{ budget.enabled_third_party_count }}</dd>
          </div>
          <div>
            <dt class="text-xs text-gray-500 dark:text-gray-400">{{ t('admin.codexThirdPartyModels.budget.worstCase') }}</dt>
            <dd class="font-medium text-gray-900 dark:text-white" data-testid="codex-budget-worst">{{ formatBytes(budget.worst_case_bytes) }}</dd>
          </div>
          <div>
            <dt class="text-xs text-gray-500 dark:text-gray-400">{{ t('admin.codexThirdPartyModels.budget.remaining') }}</dt>
            <dd class="font-medium text-gray-900 dark:text-white" data-testid="codex-budget-remaining">{{ formatBytes(budget.remaining_bytes) }}</dd>
          </div>
        </dl>
        <p class="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid="codex-budget-limit">
          {{ t('admin.codexThirdPartyModels.budget.limit', { limit: formatBytes(budget.limit_bytes) }) }}
        </p>
      </section>

      <!-- 搜索、筛选、批量操作 -->
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex flex-1 flex-wrap items-center gap-2">
          <input
            v-model="searchText"
            type="search"
            data-testid="codex-search"
            class="input h-9 w-full sm:w-64"
            :placeholder="t('admin.codexThirdPartyModels.filters.searchPlaceholder')"
            :aria-label="t('admin.codexThirdPartyModels.filters.searchPlaceholder')"
          />
          <select
            v-model="statusFilter"
            data-testid="codex-status-filter"
            class="input h-9 w-full sm:w-44"
            :aria-label="t('admin.codexThirdPartyModels.filters.status')"
          >
            <option v-for="option in statusFilterOptions" :key="option.value" :value="option.value">{{ option.label }}</option>
          </select>
        </div>
        <div class="flex flex-wrap items-center gap-2">
          <button
            type="button"
            class="btn btn-primary"
            data-testid="codex-bulk-enable"
            :disabled="busy || bulkEnableSlugs.length === 0"
            @click="applyBulk(true)"
          >
            {{ t('admin.codexThirdPartyModels.actions.enableSelected', { count: bulkEnableSlugs.length }) }}
          </button>
          <button
            type="button"
            class="btn btn-secondary"
            data-testid="codex-bulk-disable"
            :disabled="busy || bulkDisableSlugs.length === 0"
            @click="applyBulk(false)"
          >
            {{ t('admin.codexThirdPartyModels.actions.disableSelected', { count: bulkDisableSlugs.length }) }}
          </button>
        </div>
      </div>

      <!-- 候选模型表 -->
      <div class="overflow-x-auto rounded-lg border border-gray-200 dark:border-dark-700">
        <table class="min-w-full divide-y divide-gray-200 text-sm dark:divide-dark-700" data-testid="codex-model-table">
          <thead class="bg-gray-50 text-left text-xs font-medium uppercase text-gray-500 dark:bg-dark-800 dark:text-gray-400">
            <tr>
              <th class="w-10 px-3 py-2">
                <input
                  type="checkbox"
                  data-testid="codex-select-all"
                  :aria-label="t('admin.codexThirdPartyModels.columns.selectAll')"
                  :checked="allVisibleSelected"
                  :disabled="busy || selectableVisibleRows.length === 0"
                  @change="toggleSelectAll"
                />
              </th>
              <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.columns.model') }}</th>
              <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.columns.target') }}</th>
              <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.columns.capabilities') }}</th>
              <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.columns.status') }}</th>
              <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.columns.toggle') }}</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-gray-100 bg-white dark:divide-dark-700 dark:bg-dark-900">
            <tr v-if="visibleRows.length === 0" data-testid="codex-empty-rows">
              <td colspan="6" class="px-3 py-8 text-center text-gray-500 dark:text-gray-400">
                {{ t('admin.codexThirdPartyModels.table.empty') }}
              </td>
            </tr>
            <tr v-for="row in visibleRows" :key="row.slug" :data-testid="`codex-row-${row.slug}`" class="align-top">
              <td class="px-3 py-3">
                <input
                  v-if="!row.native"
                  type="checkbox"
                  :data-testid="`codex-select-${row.slug}`"
                  :aria-label="t('admin.codexThirdPartyModels.columns.selectRow', { name: row.display_name || row.slug })"
                  :checked="selected.has(row.slug)"
                  :disabled="busy"
                  @change="toggleSelected(row.slug)"
                />
              </td>
              <td class="px-3 py-3">
                <div class="font-medium text-gray-900 dark:text-white">{{ row.display_name || row.slug }}</div>
                <div v-if="row.display_name && row.display_name !== row.slug" class="text-xs text-gray-500 dark:text-gray-400">{{ row.slug }}</div>
                <p v-if="rowNote(row)" :data-testid="`codex-reason-${row.slug}`" class="mt-1 max-w-xs text-xs" :class="rowNoteClass(row)">
                  {{ rowNote(row) }}
                </p>
              </td>
              <td class="px-3 py-3 text-xs text-gray-600 dark:text-gray-300">
                <template v-if="row.native">
                  <span>{{ t('admin.codexThirdPartyModels.target.native') }}</span>
                </template>
                <template v-else>
                  <div v-if="row.target_platform">{{ platformDisplayName(row.target_platform) }}</div>
                  <div v-if="row.upstream_model" class="font-mono">{{ row.upstream_model }}</div>
                  <div v-if="row.protocol" class="text-gray-500 dark:text-gray-400">{{ protocolLabel(row.protocol) }}</div>
                  <div v-if="accountLabels(row).length > 0" class="text-gray-500 dark:text-gray-400">
                    {{ t('admin.codexThirdPartyModels.target.accounts') }}{{ accountLabels(row).join('、') }}
                  </div>
                  <div v-if="!row.target_platform && !row.upstream_model" class="text-gray-400">{{ t('admin.codexThirdPartyModels.target.unknown') }}</div>
                </template>
              </td>
              <td class="px-3 py-3 text-xs text-gray-600 dark:text-gray-300">
                <div v-for="part in capabilityParts(row)" :key="part">{{ part }}</div>
                <span v-if="capabilityParts(row).length === 0" class="text-gray-400">{{ t('admin.codexThirdPartyModels.capabilities.unknown') }}</span>
              </td>
              <td class="px-3 py-3">
                <span
                  class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium"
                  :class="statusBadgeClass(row.status)"
                  :data-testid="`codex-status-${row.slug}`"
                >
                  {{ statusLabel(row.status) }}
                </span>
                <div v-if="row.enabled && !row.native && !row.published" class="mt-1 text-xs text-amber-600 dark:text-amber-400">
                  {{ t('admin.codexThirdPartyModels.table.enabledNotPublished') }}
                </div>
                <div v-if="verificationText(row)" class="mt-1 text-xs text-gray-500 dark:text-gray-400">{{ verificationText(row) }}</div>
              </td>
              <td class="px-3 py-3">
                <div class="flex flex-col items-start gap-2">
                  <button
                    type="button"
                    role="switch"
                    :data-testid="`codex-switch-${row.slug}`"
                    class="relative inline-flex h-6 w-11 flex-shrink-0 rounded-full border-2 border-transparent transition-colors duration-200 ease-in-out focus:outline-none focus:ring-2 focus:ring-primary-500 focus:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-60 dark:focus:ring-offset-dark-800"
                    :class="row.enabled ? 'bg-primary-600' : 'bg-gray-200 dark:bg-dark-600'"
                    :aria-checked="row.enabled"
                    :aria-label="`${row.display_name || row.slug} ${t('admin.codexThirdPartyModels.toggle.label')}`"
                    :title="toggleTitle(row)"
                    :disabled="toggleDisabled(row)"
                    @click="toggleRow(row)"
                  >
                    <span
                      class="pointer-events-none inline-block h-5 w-5 transform rounded-full bg-white shadow ring-0 transition duration-200 ease-in-out"
                      :class="row.enabled ? 'translate-x-5' : 'translate-x-0'"
                    />
                  </button>
                  <span v-if="row.native" class="text-xs text-gray-500 dark:text-gray-400">{{ t('admin.codexThirdPartyModels.toggle.nativeReadonly') }}</span>
                  <button
                    v-if="canVerify(row)"
                    type="button"
                    class="text-xs font-medium text-primary-600 hover:underline disabled:opacity-50 dark:text-primary-400"
                    :data-testid="`codex-verify-${row.slug}`"
                    :disabled="busy"
                    @click="startVerify(row.slug)"
                  >
                    {{ t('admin.codexThirdPartyModels.actions.markVerified') }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- 标记已验证：绑定当前指纹 -->
      <section
        v-if="verifyingRow"
        class="rounded-lg border border-primary-200 bg-primary-50 p-4 dark:border-primary-900/60 dark:bg-primary-950/30"
        data-testid="codex-verify-form"
      >
        <h3 class="text-sm font-semibold text-primary-800 dark:text-primary-200">
          {{ t('admin.codexThirdPartyModels.verify.title', { name: verifyingRow.display_name || verifyingRow.slug }) }}
        </h3>
        <p class="mt-1 text-xs text-primary-700 dark:text-primary-300">{{ t('admin.codexThirdPartyModels.verify.hint') }}</p>
        <label for="codex-verify-note" class="mt-3 block text-xs font-medium text-gray-700 dark:text-gray-200">
          {{ t('admin.codexThirdPartyModels.verify.note') }}
        </label>
        <input
          id="codex-verify-note"
          v-model="verifyNote"
          type="text"
          maxlength="500"
          data-testid="codex-verify-note"
          class="input mt-1 w-full"
          :placeholder="t('admin.codexThirdPartyModels.verify.notePlaceholder')"
        />
        <div class="mt-3 flex justify-end gap-2">
          <button type="button" class="btn btn-secondary" data-testid="codex-verify-cancel" :disabled="busy" @click="cancelVerify">
            {{ t('admin.codexThirdPartyModels.actions.cancel') }}
          </button>
          <button type="button" class="btn btn-primary" data-testid="codex-verify-confirm" :disabled="busy" @click="submitVerify">
            {{ t('admin.codexThirdPartyModels.actions.confirmVerified') }}
          </button>
        </div>
      </section>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAppStore } from '@/stores/app'
import { getAll as getAllGroups } from '@/api/admin/groups'
import { getGroupState, markVerified, previewChanges, saveChanges } from '@/api/admin/codexThirdPartyModels'
import type {
  CodexCatalogBudget,
  CodexThirdPartyCandidate,
  CodexThirdPartyChange,
  CodexThirdPartyCatalogEvaluation,
  CodexThirdPartyGroupState,
  CodexThirdPartyModelStatus
} from '@/api/admin/codexThirdPartyModels'
import { platformDisplayName, platformUsesGateway } from '@/constants/platformCatalog'
import { extractApiErrorMessage } from '@/utils/apiError'
import type { AdminGroup } from '@/types'
import Icon from '@/components/icons/Icon.vue'

interface RejectionItem {
  key: string
  label: string
  text: string
}

const { t } = useI18n()
const appStore = useAppStore()

// 状态与协议的文案键。放在对象里按键取值，本地化测试会逐项核对中英文都存在。
const STATUS_LABEL_KEYS: Record<CodexThirdPartyModelStatus, string> = {
  native: 'admin.codexThirdPartyModels.status.native',
  needs_capability: 'admin.codexThirdPartyModels.status.needs_capability',
  configurable: 'admin.codexThirdPartyModels.status.configurable',
  verified: 'admin.codexThirdPartyModels.status.verified',
  conflict: 'admin.codexThirdPartyModels.status.conflict',
  over_budget: 'admin.codexThirdPartyModels.status.over_budget',
  unavailable: 'admin.codexThirdPartyModels.status.unavailable'
}
const STATUS_BADGE_CLASSES: Record<CodexThirdPartyModelStatus, string> = {
  native: 'bg-gray-100 text-gray-700 dark:bg-dark-700 dark:text-gray-200',
  needs_capability: 'bg-amber-100 text-amber-800 dark:bg-amber-900/30 dark:text-amber-300',
  configurable: 'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-300',
  verified: 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300',
  conflict: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  over_budget: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  unavailable: 'bg-gray-100 text-gray-600 dark:bg-dark-700 dark:text-gray-300'
}
const PROTOCOL_LABEL_KEYS: Record<string, string> = {
  responses_native: 'admin.codexThirdPartyModels.protocol.responses_native',
  responses_via_chat: 'admin.codexThirdPartyModels.protocol.responses_via_chat'
}

// ---------------------------------------------------------------------------
// 分组
// ---------------------------------------------------------------------------

const groups = ref<AdminGroup[]>([])
const groupsLoading = ref(false)
const selectedGroupValue = ref('')

/** 只有 Composite 与走 OpenAI 网关的平台才提供 Codex 在线目录。 */
function isEligibleGroup(group: AdminGroup): boolean {
  return group.platform === 'composite' || platformUsesGateway(group.platform, 'openai')
}

const eligibleGroups = computed(() => groups.value.filter(isEligibleGroup))
const selectedGroupId = computed<number | null>(() => {
  const id = Number(selectedGroupValue.value)
  return selectedGroupValue.value !== '' && Number.isInteger(id) && id > 0 ? id : null
})

async function loadGroups() {
  groupsLoading.value = true
  try {
    groups.value = await getAllGroups()
  } catch (error) {
    errorMessage.value = extractApiErrorMessage(error, t('admin.codexThirdPartyModels.group.loadFailed'))
    appStore.showError(errorMessage.value)
  } finally {
    groupsLoading.value = false
  }
}

// ---------------------------------------------------------------------------
// 状态读取与轮询
// ---------------------------------------------------------------------------

const state = ref<CodexThirdPartyGroupState | null>(null)
const loading = ref(false)
const busy = ref(false)
const errorMessage = ref('')
const rejections = ref<RejectionItem[]>([])

let loadSeq = 0

function clearFeedback() {
  errorMessage.value = ''
  rejections.value = []
}

/** 读取分组状态。silent 为 true 时不显示加载占位，也不清空已有反馈（用于保存后刷新）。 */
async function loadState(groupId: number, options: { silent?: boolean } = {}) {
  const seq = ++loadSeq
  if (!options.silent) {
    loading.value = true
    clearFeedback()
  }
  try {
    const next = await getGroupState(groupId)
    if (seq !== loadSeq || selectedGroupId.value !== groupId) return
    state.value = next
    pruneSelection()
  } catch (error) {
    if (seq !== loadSeq || selectedGroupId.value !== groupId) return
    errorMessage.value = extractApiErrorMessage(error, t('admin.codexThirdPartyModels.loadFailed'))
    if (!options.silent) {
      state.value = null
      appStore.showError(errorMessage.value)
    }
  } finally {
    if (seq === loadSeq) loading.value = false
  }
}

function reload() {
  if (selectedGroupId.value) void loadState(selectedGroupId.value)
}

/** 供 Plus 页顶部“刷新”按钮调用：重新读取分组列表，并重新加载当前选中的分组。 */
async function reloadAll() {
  const groupId = selectedGroupId.value
  await Promise.all([loadGroups(), groupId ? loadState(groupId) : Promise.resolve()])
}

defineExpose({ reload: reloadAll })

watch(selectedGroupValue, () => {
  state.value = null
  selected.value = new Set()
  verifyingSlug.value = ''
  clearFeedback()
  if (selectedGroupId.value) void loadState(selectedGroupId.value)
})

onMounted(() => {
  void loadGroups()
})

onBeforeUnmount(() => {
  loadSeq += 1
})

// ---------------------------------------------------------------------------
// 候选行、筛选、搜索
// ---------------------------------------------------------------------------

const searchText = ref('')
const statusFilter = ref('all')

// 表格只展示可管理的第三方模型：
//   - 原生 GPT 不受开关控制，不展示（体积预算里仍计入原生数量）；
//   - 没有任何账号承接、且未开启过的条目（例如只剩 Composite 路由、承接账号已暂停）不展示；
//   - 已开启但失去承接的条目仍展示，方便管理员关闭。
const rows = computed<CodexThirdPartyCandidate[]>(() =>
  (state.value?.evaluation.candidates ?? []).filter(
    (row) => !row.native && !(row.status === 'unavailable' && !row.enabled)
  )
)

// 状态筛选只有四项：全部、已开启、未开启、不可开启。
const statusFilterOptions = computed(() => [
  { value: 'all', label: t('admin.codexThirdPartyModels.filters.all') },
  { value: 'enabled', label: t('admin.codexThirdPartyModels.filters.enabled') },
  { value: 'disabled', label: t('admin.codexThirdPartyModels.filters.disabled') },
  { value: 'blocked', label: t('admin.codexThirdPartyModels.filters.blocked') }
])

// 不可开启：状态为待补能力、配置冲突、目录超限或不可用，且服务端判定不能开启。行内仍显示原因。
const BLOCKED_STATUSES: CodexThirdPartyModelStatus[] = ['needs_capability', 'conflict', 'over_budget', 'unavailable']
function isBlockedRow(row: CodexThirdPartyCandidate): boolean {
  return !row.native && !row.can_enable && BLOCKED_STATUSES.includes(row.status)
}

const visibleRows = computed(() => {
  const keyword = searchText.value.trim().toLowerCase()
  return rows.value.filter((row) => {
    if (keyword) {
      const haystack = [row.slug, row.display_name, row.upstream_model, row.target_platform]
        .filter(Boolean)
        .join(' ')
        .toLowerCase()
      if (!haystack.includes(keyword)) return false
    }
    if (statusFilter.value === 'enabled') return row.enabled && !row.native
    if (statusFilter.value === 'disabled') return !row.enabled && !row.native
    if (statusFilter.value === 'blocked') return isBlockedRow(row)
    return true
  })
})

function statusLabel(status: CodexThirdPartyModelStatus): string {
  const key = STATUS_LABEL_KEYS[status]
  return key ? t(key) : String(status)
}

function statusBadgeClass(status: CodexThirdPartyModelStatus): string {
  return STATUS_BADGE_CLASSES[status] ?? STATUS_BADGE_CLASSES.unavailable
}

function protocolLabel(protocol: string): string {
  const key = PROTOCOL_LABEL_KEYS[protocol]
  return key ? t(key) : protocol
}

/** 候选来自哪些账号：优先显示账号名称，查不到时退回账号 ID。最多列出 3 个。 */
function accountLabels(row: CodexThirdPartyCandidate): string[] {
  const ids = row.account_ids ?? []
  const names = new Map((state.value?.sources ?? []).map((source) => [source.account_id, source.account_name]))
  const labels = ids.slice(0, 3).map((id) => names.get(id) || `#${id}`)
  if (ids.length > 3) labels.push(t('admin.codexThirdPartyModels.target.more', { count: ids.length - 3 }))
  return labels
}

function formatContext(tokens: number): string {
  if (tokens >= 1048576 && tokens % 1048576 === 0) return `${tokens / 1048576}M`
  if (tokens >= 1000) return `${Math.round(tokens / 1000)}K`
  return String(tokens)
}

/** 能力摘要：输入模态、推理档位、上下文窗口、编码工具。缺失的项直接省略，不猜测。 */
function capabilityParts(row: CodexThirdPartyCandidate): string[] {
  const caps = row.capabilities
  if (!caps) return []
  const parts: string[] = []
  if (caps.input_modalities?.length) {
    parts.push(t('admin.codexThirdPartyModels.capabilities.input', { value: caps.input_modalities.join(' / ') }))
  }
  if (caps.reasoning_levels?.length) {
    parts.push(
      t('admin.codexThirdPartyModels.capabilities.reasoning', {
        value: caps.reasoning_levels.join(' / '),
        default: caps.default_reasoning_level || '-'
      })
    )
  }
  if (caps.context_window) {
    parts.push(t('admin.codexThirdPartyModels.capabilities.context', { value: formatContext(caps.context_window) }))
  }
  const tools: string[] = []
  if (caps.shell_type) tools.push(caps.shell_type)
  if (caps.apply_patch_tool_type) tools.push(`apply_patch: ${caps.apply_patch_tool_type}`)
  if (caps.supports_search_tool) tools.push(t('admin.codexThirdPartyModels.capabilities.searchTool'))
  if (tools.length) parts.push(t('admin.codexThirdPartyModels.capabilities.tools', { value: tools.join(' / ') }))
  return parts
}

/** 行内说明：不能开启时给出原因；已开启但被阻塞时保持开启并说明阻塞原因。 */
function rowNote(row: CodexThirdPartyCandidate): string {
  if (row.native) return ''
  const reasons = (row.reasons ?? []).join('；')
  if (!row.enabled && !row.can_enable) {
    return reasons || t('admin.codexThirdPartyModels.table.cannotEnableFallback')
  }
  if (row.enabled && (!row.can_enable || !row.published)) {
    return reasons || t('admin.codexThirdPartyModels.table.blockedFallback')
  }
  return ''
}

function rowNoteClass(row: CodexThirdPartyCandidate): string {
  return row.enabled ? 'text-amber-600 dark:text-amber-400' : 'text-gray-500 dark:text-gray-400'
}

function verificationText(row: CodexThirdPartyCandidate): string {
  const verification = state.value?.config.models[row.slug]?.verification
  if (!verification) return ''
  // 验收记录的指纹和当前候选指纹一致时才算有效，否则提示需要重新验收。
  if (row.fingerprint && verification.fingerprint === row.fingerprint) {
    return t('admin.codexThirdPartyModels.verify.verifiedAt', { time: formatTime(verification.verified_at) })
  }
  return t('admin.codexThirdPartyModels.verify.outdated')
}

// ---------------------------------------------------------------------------
// 目录预算
// ---------------------------------------------------------------------------

const emptyBudget: CodexCatalogBudget = {
  limit_bytes: 0,
  native_count: 0,
  enabled_third_party_count: 0,
  worst_case_bytes: 0,
  remaining_bytes: 0,
  complete: false,
  deferred: false
}
const budget = computed<CodexCatalogBudget>(() => state.value?.evaluation.budget ?? emptyBudget)
const budgetOverLimit = computed(() => budget.value.complete && budget.value.worst_case_bytes > budget.value.limit_bytes)

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes)) return '-'
  return `${bytes.toLocaleString('en-US')} B (${(bytes / 1024).toFixed(1)} KiB)`
}

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

// ---------------------------------------------------------------------------
// 开关、批量、保存
// ---------------------------------------------------------------------------

const selected = ref<Set<string>>(new Set())

// 全选只选当前筛选结果里“能被批量操作”的行：原生 GPT 只读；还不能开启的行批量开启必然被整批拒绝，
// 不放进全选里（仍可单独勾选，用来查看逐项拒绝原因）。
const selectableVisibleRows = computed(() => visibleRows.value.filter((row) => !row.native && (row.enabled || row.can_enable)))
const allVisibleSelected = computed(
  () => selectableVisibleRows.value.length > 0 && selectableVisibleRows.value.every((row) => selected.value.has(row.slug))
)
const selectedRows = computed(() => rows.value.filter((row) => !row.native && selected.value.has(row.slug)))
const bulkEnableSlugs = computed(() => selectedRows.value.filter((row) => !row.enabled).map((row) => row.slug))
const bulkDisableSlugs = computed(() => selectedRows.value.filter((row) => row.enabled).map((row) => row.slug))

/** 重新读取后，去掉已经不存在的选中项。 */
function pruneSelection() {
  const known = new Set(rows.value.filter((row) => !row.native).map((row) => row.slug))
  const next = new Set([...selected.value].filter((slug) => known.has(slug)))
  if (next.size !== selected.value.size) selected.value = next
}

function toggleSelected(slug: string) {
  const next = new Set(selected.value)
  if (next.has(slug)) next.delete(slug)
  else next.add(slug)
  selected.value = next
}

function toggleSelectAll() {
  const next = new Set(selected.value)
  if (allVisibleSelected.value) {
    for (const row of selectableVisibleRows.value) next.delete(row.slug)
  } else {
    for (const row of selectableVisibleRows.value) next.add(row.slug)
  }
  selected.value = next
}

/** 原生 GPT 只读；还不能开启的行开关禁用。已开启的行永远允许关闭，哪怕它当前被阻塞。 */
function toggleDisabled(row: CodexThirdPartyCandidate): boolean {
  if (busy.value || row.native) return true
  return !row.enabled && !row.can_enable
}

function toggleTitle(row: CodexThirdPartyCandidate): string {
  if (row.native) return t('admin.codexThirdPartyModels.toggle.nativeReadonly')
  if (!row.enabled && !row.can_enable) return rowNote(row)
  return t('admin.codexThirdPartyModels.toggle.label')
}

function toggleRow(row: CodexThirdPartyCandidate) {
  if (toggleDisabled(row)) return
  void applyChanges([{ slug: row.slug, enabled: !row.enabled }])
}

function applyBulk(enabled: boolean) {
  const slugs = enabled ? bulkEnableSlugs.value : bulkDisableSlugs.value
  if (slugs.length === 0) return
  void applyChanges(slugs.map((slug) => ({ slug, enabled })))
}

function modelLabel(slug: string): string {
  const row = rows.value.find((item) => item.slug === slug)
  return row?.display_name || slug
}

/** 用预览得到的评估结果检查被开启的模型，返回不满足条件的逐项原因。 */
function collectRejections(evaluation: CodexThirdPartyCatalogEvaluation, changes: CodexThirdPartyChange[]): RejectionItem[] {
  const items: RejectionItem[] = []
  for (const change of changes) {
    if (!change.enabled) continue
    const candidate = evaluation.candidates.find((item) => item.slug === change.slug)
    let text = ''
    if (!candidate) text = t('admin.codexThirdPartyModels.reject.notCandidate')
    else if (candidate.native) text = t('admin.codexThirdPartyModels.reject.native')
    else if (!candidate.can_enable) {
      text = (candidate.reasons ?? []).join('；') || t('admin.codexThirdPartyModels.table.cannotEnableFallback')
    }
    if (text) items.push({ key: `model:${change.slug}`, label: modelLabel(change.slug), text })
  }
  const b = evaluation.budget
  if (!b.complete) {
    items.push({
      key: 'budget',
      label: t('admin.codexThirdPartyModels.budget.title'),
      text: b.reason || t('admin.codexThirdPartyModels.budget.incompleteHint')
    })
  } else if (b.worst_case_bytes > b.limit_bytes) {
    items.push({
      key: 'budget',
      label: t('admin.codexThirdPartyModels.budget.title'),
      text: t('admin.codexThirdPartyModels.reject.overLimit', {
        worst: formatBytes(b.worst_case_bytes),
        limit: formatBytes(b.limit_bytes)
      })
    })
  }
  return items
}

/** 把服务端整批拒绝时返回的逐项原因（metadata）整理成列表。 */
function rejectionsFromMetadata(metadata: Record<string, string> | undefined): RejectionItem[] {
  if (!metadata) return []
  return Object.entries(metadata).map(([key, text]) => ({
    key,
    label: key.startsWith('model:') ? modelLabel(key.slice('model:'.length)) : t('admin.codexThirdPartyModels.budget.title'),
    text
  }))
}

interface ApiErrorShape {
  status?: number
  reason?: string
  metadata?: Record<string, string>
}

/** 统一处理保存、预览、验收失败：409 提示后重新加载，整批拒绝回显逐项原因，其余回显服务端原因。 */
function handleActionError(error: unknown, groupId: number, fallbackKey: string) {
  const info = (typeof error === 'object' && error !== null ? error : {}) as ApiErrorShape
  if (info.status === 409) {
    errorMessage.value = t('admin.codexThirdPartyModels.conflict')
    appStore.showWarning(errorMessage.value)
    void loadState(groupId, { silent: true })
    return
  }
  errorMessage.value = extractApiErrorMessage(error, t(fallbackKey))
  if (info.reason === 'CODEX_MODEL_ENABLE_REJECTED') rejections.value = rejectionsFromMetadata(info.metadata)
  appStore.showError(errorMessage.value)
}

/**
 * 应用一批开关变更。开启先预览再保存：预览返回的评估必须允许全部被开启的模型且预算完整不超限，
 * 才带着输入指纹保存；关闭直接保存。任何一步失败都不会产生部分变更。
 */
async function applyChanges(changes: CodexThirdPartyChange[]) {
  const current = state.value
  if (!current || busy.value) return
  const groupId = current.group_id
  const revision = current.config.revision
  busy.value = true
  clearFeedback()
  try {
    let fingerprint: string | undefined
    if (changes.some((change) => change.enabled)) {
      const preview = await previewChanges(groupId, revision, changes)
      const rejected = collectRejections(preview.evaluation, changes)
      if (rejected.length > 0) {
        errorMessage.value = t('admin.codexThirdPartyModels.reject.title')
        rejections.value = rejected
        appStore.showError(errorMessage.value)
        return
      }
      fingerprint = preview.evaluation.input_fingerprint
    }
    await saveChanges(groupId, revision, changes, fingerprint)
    appStore.showSuccess(t('admin.codexThirdPartyModels.saveSuccess'))
    selected.value = new Set()
    await loadState(groupId, { silent: true })
  } catch (error) {
    handleActionError(error, groupId, 'admin.codexThirdPartyModels.saveFailed')
  } finally {
    busy.value = false
  }
}

// ---------------------------------------------------------------------------
// 标记已验证
// ---------------------------------------------------------------------------

const verifyingSlug = ref('')
const verifyNote = ref('')
const verifyingRow = computed(() => rows.value.find((row) => row.slug === verifyingSlug.value) ?? null)

function canVerify(row: CodexThirdPartyCandidate): boolean {
  return !row.native && row.enabled && !!row.fingerprint && row.status !== 'verified'
}

function startVerify(slug: string) {
  verifyingSlug.value = slug
  verifyNote.value = ''
}

function cancelVerify() {
  verifyingSlug.value = ''
  verifyNote.value = ''
}

async function submitVerify() {
  const current = state.value
  const row = verifyingRow.value
  if (!current || !row || !row.fingerprint || busy.value) return
  const groupId = current.group_id
  busy.value = true
  clearFeedback()
  try {
    await markVerified(groupId, current.config.revision, row.slug, row.fingerprint, verifyNote.value.trim() || undefined)
    appStore.showSuccess(t('admin.codexThirdPartyModels.verify.success'))
    cancelVerify()
    await loadState(groupId, { silent: true })
  } catch (error) {
    handleActionError(error, groupId, 'admin.codexThirdPartyModels.verify.failed')
  } finally {
    busy.value = false
  }
}
</script>
