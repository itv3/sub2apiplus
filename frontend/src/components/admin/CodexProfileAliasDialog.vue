<template>
  <BaseDialog
    :show="show"
    :title="mode === 'link' ? t('admin.codexThirdPartyModels.profileAlias.linkTitle') : t('admin.codexThirdPartyModels.profileAlias.listTitle')"
    width="wide"
    @close="emit('close')"
  >
    <div data-testid="codex-alias-dialog" class="space-y-4">
      <p v-if="errorMessage" role="alert" data-testid="codex-alias-error" class="rounded-lg border border-red-200 bg-red-50 p-3 text-sm text-red-700 dark:border-red-900/60 dark:bg-red-950/30 dark:text-red-300">
        {{ errorMessage }}
      </p>

      <!-- 关联到已有档案：上游型号只读，选择档案后确认 -->
      <template v-if="mode === 'link'">
        <div>
          <label class="block text-xs font-medium text-gray-700 dark:text-gray-200">
            {{ t('admin.codexThirdPartyModels.profileAlias.upstreamModel') }}
          </label>
          <input type="text" readonly data-testid="codex-alias-model" class="input mt-1 w-full font-mono" :value="upstreamModel" />
        </div>
        <div>
          <label for="codex-alias-profile" class="block text-xs font-medium text-gray-700 dark:text-gray-200">
            {{ t('admin.codexThirdPartyModels.profileAlias.profile') }}
          </label>
          <select id="codex-alias-profile" v-model="selectedProfile" data-testid="codex-alias-profile" class="input mt-1 w-full" :disabled="loading || saving">
            <option value="" disabled>{{ t('admin.codexThirdPartyModels.profileAlias.profilePlaceholder') }}</option>
            <option v-for="profile in profiles" :key="profile.id" :value="profile.id">
              {{ profile.id }}
            </option>
          </select>
          <p v-if="selectedSummary" data-testid="codex-alias-profile-models" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ t('admin.codexThirdPartyModels.profileAlias.profileKnows', { models: selectedSummary.upstream_models.join('、') }) }}
          </p>
        </div>
        <p data-testid="codex-alias-hint" class="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
          {{ t('admin.codexThirdPartyModels.profileAlias.confirmHint') }}
        </p>
      </template>

      <!-- 已关联别名列表：可新增关联，也可解除关联 -->
      <template v-else>
        <!-- 新增关联：自由输入上游型号，从下拉选择档案 -->
        <section class="space-y-3 rounded-lg border border-gray-200 p-3 dark:border-dark-700" data-testid="codex-alias-add">
          <h4 class="text-sm font-semibold text-gray-900 dark:text-white">{{ t('admin.codexThirdPartyModels.profileAlias.addTitle') }}</h4>
          <div>
            <label for="codex-alias-new-model" class="block text-xs font-medium text-gray-700 dark:text-gray-200">
              {{ t('admin.codexThirdPartyModels.profileAlias.upstreamModel') }}
            </label>
            <input
              id="codex-alias-new-model"
              v-model="newModel"
              type="text"
              maxlength="200"
              data-testid="codex-alias-new-model"
              class="input mt-1 w-full font-mono"
              :placeholder="t('admin.codexThirdPartyModels.profileAlias.addModelPlaceholder')"
              :disabled="saving"
            />
          </div>
          <div>
            <label for="codex-alias-new-profile" class="block text-xs font-medium text-gray-700 dark:text-gray-200">
              {{ t('admin.codexThirdPartyModels.profileAlias.profile') }}
            </label>
            <select id="codex-alias-new-profile" v-model="newProfile" data-testid="codex-alias-new-profile" class="input mt-1 w-full" :disabled="loading || saving">
              <option value="" disabled>{{ t('admin.codexThirdPartyModels.profileAlias.profilePlaceholder') }}</option>
              <option v-for="profile in profiles" :key="profile.id" :value="profile.id">{{ profile.id }}</option>
            </select>
            <p v-if="newSummary" data-testid="codex-alias-new-profile-models" class="mt-1 text-xs text-gray-500 dark:text-gray-400">
              {{ t('admin.codexThirdPartyModels.profileAlias.profileKnows', { models: newSummary.upstream_models.join('、') }) }}
            </p>
          </div>
          <p data-testid="codex-alias-new-hint" class="rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800 dark:border-amber-900/60 dark:bg-amber-950/30 dark:text-amber-300">
            {{ t('admin.codexThirdPartyModels.profileAlias.confirmHint') }}
          </p>
          <div class="flex justify-end">
            <button type="button" class="btn btn-primary" data-testid="codex-alias-new-confirm" :disabled="!canAdd || loading || saving" @click="addAlias">
              {{ t('admin.codexThirdPartyModels.profileAlias.confirm') }}
            </button>
          </div>
        </section>

        <p v-if="!loading && aliases.length === 0" data-testid="codex-alias-empty" class="text-sm text-gray-500 dark:text-gray-400">
          {{ t('admin.codexThirdPartyModels.profileAlias.empty') }}
        </p>
        <div v-else class="overflow-x-auto rounded-lg border border-gray-200 dark:border-dark-700">
          <table class="min-w-full divide-y divide-gray-200 text-sm dark:divide-dark-700" data-testid="codex-alias-table">
            <thead class="bg-gray-50 text-left text-xs font-medium text-gray-500 dark:bg-dark-800 dark:text-gray-400">
              <tr>
                <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.profileAlias.upstreamModel') }}</th>
                <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.profileAlias.profile') }}</th>
                <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.profileAlias.updatedBy') }}</th>
                <th class="px-3 py-2">{{ t('admin.codexThirdPartyModels.profileAlias.updatedAt') }}</th>
                <th class="px-3 py-2"></th>
              </tr>
            </thead>
            <tbody class="divide-y divide-gray-100 dark:divide-dark-700">
              <tr v-for="alias in aliases" :key="alias.key" :data-testid="`codex-alias-row-${alias.key}`">
                <td class="px-3 py-2 font-mono text-xs">{{ alias.upstream_model }}</td>
                <td class="px-3 py-2 text-xs">
                  <span>{{ alias.profile_id }}</span>
                  <span v-if="!alias.valid" :data-testid="`codex-alias-invalid-${alias.key}`" class="ml-2 inline-flex rounded-full bg-red-100 px-2 py-0.5 text-red-700 dark:bg-red-900/30 dark:text-red-300">
                    {{ t('admin.codexThirdPartyModels.profileAlias.invalid') }}
                  </span>
                </td>
                <td class="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">{{ alias.updated_by || '-' }}</td>
                <td class="px-3 py-2 text-xs text-gray-500 dark:text-gray-400">{{ formatTime(alias.updated_at) }}</td>
                <td class="px-3 py-2 text-right">
                  <button
                    type="button"
                    class="btn btn-secondary"
                    :data-testid="`codex-alias-unlink-${alias.key}`"
                    :disabled="saving"
                    @click="unlink(alias)"
                  >
                    {{ t('admin.codexThirdPartyModels.profileAlias.unlink') }}
                  </button>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
    </div>

    <template #footer>
      <div class="flex justify-end gap-2">
        <button type="button" class="btn btn-secondary" data-testid="codex-alias-close" @click="emit('close')">
          {{ mode === 'link' ? t('admin.codexThirdPartyModels.actions.cancel') : t('admin.codexThirdPartyModels.profileAlias.close') }}
        </button>
        <button
          v-if="mode === 'link'"
          type="button"
          class="btn btn-primary"
          data-testid="codex-alias-confirm"
          :disabled="!selectedProfile || loading || saving"
          @click="link"
        >
          {{ t('admin.codexThirdPartyModels.profileAlias.confirm') }}
        </button>
      </div>
    </template>
  </BaseDialog>
</template>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAppStore } from '@/stores/app'
import { deleteProfileAlias, listProfileAliases, setProfileAlias } from '@/api/admin/codexThirdPartyModels'
import type { CodexProfileAlias, CodexProfileSummary } from '@/api/admin/codexThirdPartyModels'
import { extractApiErrorMessage } from '@/utils/apiError'
import BaseDialog from '@/components/common/BaseDialog.vue'

/**
 * 型号别名对话框，两种模式：
 *  - link：把一个“暂无能力档案”的上游型号关联到已有档案（上游型号只读，选择档案后确认）；
 *  - list：列出已关联的别名，可解除关联；档案已失效的别名标出，不参与匹配。
 * 关联或解除成功后发出 changed，由父组件重新读取分组目录。
 */
const props = defineProps<{
  show: boolean
  mode: 'link' | 'list'
  upstreamModel?: string
}>()
const emit = defineEmits<{
  (e: 'close'): void
  (e: 'changed'): void
}>()

const { t } = useI18n()
const appStore = useAppStore()

const profiles = ref<CodexProfileSummary[]>([])
const aliases = ref<CodexProfileAlias[]>([])
const selectedProfile = ref('')
const loading = ref(false)
const saving = ref(false)
const errorMessage = ref('')

const selectedSummary = computed(() => profiles.value.find((profile) => profile.id === selectedProfile.value) ?? null)

// 列表模式下“新增关联”表单：上游型号自由输入，档案从下拉选择。
const newModel = ref('')
const newProfile = ref('')
const newSummary = computed(() => profiles.value.find((profile) => profile.id === newProfile.value) ?? null)
const canAdd = computed(() => newModel.value.trim() !== '' && newProfile.value !== '')

function formatTime(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString()
}

async function load() {
  loading.value = true
  errorMessage.value = ''
  try {
    const result = await listProfileAliases()
    profiles.value = result.profiles
    aliases.value = result.aliases
    // 该型号已有关联时，默认选中它当前关联的档案。
    const existing = result.aliases.find((alias) => alias.upstream_model === props.upstreamModel && alias.valid)
    selectedProfile.value = existing?.profile_id ?? ''
  } catch (error) {
    errorMessage.value = extractApiErrorMessage(error, t('admin.codexThirdPartyModels.profileAlias.loadFailed'))
  } finally {
    loading.value = false
  }
}

watch(
  () => props.show,
  (visible) => {
    if (visible) {
      selectedProfile.value = ''
      newModel.value = ''
      newProfile.value = ''
      void load()
    }
  },
  { immediate: true }
)

async function link() {
  if (!props.upstreamModel || !selectedProfile.value || saving.value) return
  saving.value = true
  errorMessage.value = ''
  try {
    await setProfileAlias(props.upstreamModel, selectedProfile.value)
    appStore.showSuccess(t('admin.codexThirdPartyModels.profileAlias.saveSuccess'))
    emit('changed')
    emit('close')
  } catch (error) {
    errorMessage.value = extractApiErrorMessage(error, t('admin.codexThirdPartyModels.profileAlias.saveFailed'))
  } finally {
    saving.value = false
  }
}

/**
 * 列表模式下新增关联：保存走 PUT；错误（型号已被内置档案认出、档案不存在、格式非法）就地显示。
 * 成功后刷新别名列表并通知父组件重新读取当前分组目录，窗口保持打开。
 */
async function addAlias() {
  if (!canAdd.value || saving.value) return
  saving.value = true
  errorMessage.value = ''
  try {
    const result = await setProfileAlias(newModel.value.trim(), newProfile.value)
    aliases.value = result.aliases
    profiles.value = result.profiles
    newModel.value = ''
    newProfile.value = ''
    appStore.showSuccess(t('admin.codexThirdPartyModels.profileAlias.saveSuccess'))
    emit('changed')
  } catch (error) {
    errorMessage.value = extractApiErrorMessage(error, t('admin.codexThirdPartyModels.profileAlias.saveFailed'))
  } finally {
    saving.value = false
  }
}

async function unlink(alias: CodexProfileAlias) {
  if (saving.value) return
  saving.value = true
  errorMessage.value = ''
  try {
    const result = await deleteProfileAlias(alias.upstream_model)
    aliases.value = result.aliases
    profiles.value = result.profiles
    appStore.showSuccess(t('admin.codexThirdPartyModels.profileAlias.deleteSuccess'))
    emit('changed')
  } catch (error) {
    errorMessage.value = extractApiErrorMessage(error, t('admin.codexThirdPartyModels.profileAlias.deleteFailed'))
  } finally {
    saving.value = false
  }
}
</script>
