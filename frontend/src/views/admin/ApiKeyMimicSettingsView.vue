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

        <div v-else class="rounded-lg border border-gray-200 bg-gray-50/70 p-4 shadow-sm dark:border-dark-700 dark:bg-dark-900/40">
          <div class="mb-4 flex flex-wrap items-center gap-1 rounded-md border border-gray-200 bg-white p-1 dark:border-dark-700 dark:bg-dark-800">
            <button
              v-for="tab in keepaliveTabs"
              :key="tab.key"
              type="button"
              class="rounded px-4 py-2 text-sm font-medium transition-colors"
              :class="keepaliveTab === tab.key ? 'bg-primary-600 text-white shadow-sm' : 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-dark-700'"
              @click="keepaliveTab = tab.key"
            >
              {{ tab.label }}
            </button>
          </div>

          <div v-if="keepaliveTab === 'overview'" class="space-y-4">
            <div class="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-gray-200 bg-white p-4 dark:border-dark-700 dark:bg-dark-800">
              <div>
                <div class="text-sm font-semibold text-gray-900 dark:text-white">keeper 状态</div>
                <div class="mt-1 text-xs text-gray-500 dark:text-gray-400">
                  keeper {{ keeperVersion || '-' }} · 项目目录 {{ keepaliveProjectOptions.length }} 个 · 题库 {{ promptBankRows.length }} 条
                </div>
              </div>
            </div>
            <div class="grid gap-3 md:grid-cols-4">
              <div class="rounded-lg border border-gray-200 bg-white p-4 dark:border-dark-700 dark:bg-dark-800">
                <div class="text-xs text-gray-500">总账号数</div>
                <div class="mt-2 text-2xl font-semibold">{{ keepaliveDashboard.total }}</div>
              </div>
              <div class="rounded-lg border border-gray-200 bg-white p-4 dark:border-dark-700 dark:bg-dark-800">
                <div class="text-xs text-gray-500">已启用</div>
                <div class="mt-2 text-2xl font-semibold">{{ keepaliveDashboard.enabled }}</div>
              </div>
              <div class="rounded-lg border border-gray-200 bg-white p-4 dark:border-dark-700 dark:bg-dark-800">
                <div class="text-xs text-gray-500">今日成功 / 失败</div>
                <div class="mt-2 text-2xl font-semibold">{{ keepaliveDashboard.todaySuccess }} / {{ keepaliveDashboard.todayFailure }}</div>
              </div>
              <div class="rounded-lg border border-gray-200 bg-white p-4 dark:border-dark-700 dark:bg-dark-800">
                <div class="text-xs text-gray-500">当前运行中</div>
                <div class="mt-2 text-2xl font-semibold">{{ keepaliveDashboard.running }}</div>
              </div>
            </div>
            <div class="overflow-x-auto rounded-lg border border-gray-200 bg-white dark:border-dark-700 dark:bg-dark-900">
              <table class="min-w-[1280px] divide-y divide-gray-200 text-sm dark:divide-dark-700">
                <thead class="bg-gray-100 text-gray-700 dark:bg-dark-800 dark:text-gray-200">
                  <tr>
                    <th class="px-4 py-3 text-left">账号</th>
                    <th class="px-4 py-3 text-left">平台 / 类型</th>
                    <th class="px-4 py-3 text-left">模型</th>
                    <th class="px-4 py-3 text-left">状态</th>
                    <th class="px-4 py-3 text-left">24小时用量 / 费用</th>
                    <th class="px-4 py-3 text-left">时间</th>
                    <th class="px-4 py-3 text-left">执行次数</th>
                    <th class="px-4 py-3 text-left">最近结果</th>
                    <th class="px-4 py-3 text-left">操作</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200 bg-white dark:divide-dark-700 dark:bg-dark-900">
                  <tr v-for="row in keepaliveOverviewRows" :key="row.name">
                    <td class="px-4 py-3 font-medium text-gray-900 dark:text-white">{{ row.name }}</td>
                    <td class="px-4 py-3">{{ row.platform || '-' }}<div class="text-xs text-gray-500">{{ row.account_type || '-' }}</div></td>
                    <td class="px-4 py-3">{{ row.model || '-' }}</td>
                    <td class="px-4 py-3">
                      <span class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium" :class="keepaliveStatusClass(row)">
                        {{ row.current_status || (row.running ? '执行中' : row.enabled ? '等待下次' : '关闭') }}
                      </span>
                      <div v-if="row.status_detail || row.last_error" class="mt-1 max-w-xs truncate text-xs text-red-500">{{ row.status_detail || row.last_error }}</div>
                    </td>
                    <td class="px-4 py-3 text-xs text-gray-600 dark:text-gray-300" v-html="formatOverviewUsageCost(row.usage_24h_cost || row.total_usage_cost || buildUsageCostSummary(row.sessions))"></td>
                    <td class="px-4 py-3 text-xs text-gray-600 dark:text-gray-300">
                      <div>最近：{{ formatDateTime(row.last_finished_at || row.last_keepalive_received_at || row.last_keepalive_started_at) }}</div>
                      <div>下次：{{ formatDateTime(row.next_run_at) }}</div>
                    </td>
                    <td class="px-4 py-3 text-sm">
                      {{ row.execution_count ?? sessionCounts(row.sessions).total }}
                      <div class="text-xs text-gray-500">成功 {{ row.success_count ?? sessionCounts(row.sessions).success }} / 失败 {{ row.failure_count ?? sessionCounts(row.sessions).failure }}</div>
                    </td>
                    <td class="max-w-md truncate px-4 py-3">{{ row.last_message_summary || latestSessionSummary(row) }}</td>
                    <td class="min-w-40 whitespace-nowrap px-4 py-3">
                      <button type="button" class="btn btn-secondary btn-sm" :disabled="keepaliveUpdatingIds.has(row.account_id)" @click="runKeepalive(row.name)">立即执行</button>
                      <button type="button" class="btn btn-secondary btn-sm ml-2" @click="openHistory(row.name)">历史</button>
                    </td>
                  </tr>
                  <tr v-if="keepaliveOverviewRows.length === 0">
                    <td colspan="9" class="px-4 py-6 text-center text-gray-500">还没有配置账号。</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>

          <div v-else-if="keepaliveTab === 'settings'" class="max-h-[calc(100vh-340px)] space-y-6 overflow-y-auto pr-2">
            <div class="flex justify-start">
              <button type="button" class="btn btn-primary" @click="openKeepaliveModal()">添加账号</button>
            </div>
            <div class="overflow-x-auto rounded-lg border border-gray-200 bg-white dark:border-dark-700 dark:bg-dark-900">
              <table class="min-w-[960px] divide-y divide-gray-200 text-sm dark:divide-dark-700">
                <thead class="bg-gray-100 text-gray-700 dark:bg-dark-800 dark:text-gray-200">
                  <tr>
                    <th class="px-4 py-3 text-left">账号</th>
                    <th class="px-4 py-3 text-left">平台 / 类型</th>
                    <th class="px-4 py-3 text-left">模型</th>
                    <th class="px-4 py-3 text-left">工作目录</th>
                    <th class="px-4 py-3 text-left">频率</th>
                    <th class="px-4 py-3 text-left">编辑</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200 bg-white dark:divide-dark-700 dark:bg-dark-900">
                  <tr v-for="row in configuredKeepaliveRows" :key="row.account.id">
                    <td class="px-4 py-3">{{ row.account.name }}<div class="text-xs text-gray-500">{{ row.form.enabled ? '已启用' : '已禁用' }}</div></td>
                    <td class="px-4 py-3">{{ row.account.platform }}<div class="text-xs text-gray-500">{{ row.account.type || '-' }}</div></td>
                    <td class="px-4 py-3">{{ row.form.model || '-' }}<div class="text-xs text-gray-500">{{ row.form.mode === 'fresh' ? '全新会话' : '接续上次会话' }}</div></td>
                    <td class="px-4 py-3">{{ row.form.workspace || '-' }}</td>
                    <td class="px-4 py-3">{{ row.form.intervalMinutes }} 分钟<div class="text-xs text-gray-500">{{ row.form.workStart }} - {{ row.form.workEnd }}</div></td>
                    <td class="px-4 py-3"><button type="button" class="btn btn-secondary btn-sm" @click="openKeepaliveModal(row)">编辑</button></td>
                  </tr>
                  <tr v-if="configuredKeepaliveRows.length === 0">
                    <td colspan="6" class="px-4 py-6 text-center text-gray-500">还没有配置账号。</td>
                  </tr>
                </tbody>
              </table>
            </div>

            <section class="space-y-3">
              <div class="flex items-center justify-between">
                <h3 class="text-base font-semibold text-gray-900 dark:text-white">提示词题库</h3>
                <button type="button" class="btn btn-secondary btn-sm" @click="openPromptModal()">添加问题</button>
              </div>
              <label class="block text-sm font-medium text-gray-700 dark:text-gray-200">全局约束提示词</label>
              <textarea v-model="promptGuard" class="input min-h-24 w-full"></textarea>
              <button type="button" class="btn btn-primary btn-sm" :disabled="keepaliveLoading" @click="savePromptSettings()">保存约束</button>
              <div class="max-h-96 overflow-auto rounded-lg border border-gray-200 bg-white dark:border-dark-700 dark:bg-dark-900">
                <table class="min-w-[960px] divide-y divide-gray-200 text-sm dark:divide-dark-700">
                  <thead class="sticky top-0 z-10 bg-gray-100 text-gray-700 dark:bg-dark-800 dark:text-gray-200">
                    <tr>
                      <th class="px-4 py-3 text-left">适用范围</th>
                      <th class="px-4 py-3 text-left">项目</th>
                      <th class="px-4 py-3 text-left">问题内容</th>
                      <th class="px-4 py-3 text-left">状态</th>
                      <th class="px-4 py-3 text-left">操作</th>
                    </tr>
                  </thead>
                  <tbody class="divide-y divide-gray-200 bg-white dark:divide-dark-700 dark:bg-dark-900">
                    <tr v-for="(item, index) in promptBankRows" :key="item.id || index">
                      <td class="px-4 py-3">{{ item.scope === 'project' ? '指定项目' : '通用' }}</td>
                      <td class="px-4 py-3">{{ item.scope === 'project' ? projectName(item.project_path) : '-' }}</td>
                      <td class="px-4 py-3">{{ item.text }}</td>
                      <td class="px-4 py-3">{{ item.enabled === false ? '停用' : '启用' }}</td>
                      <td class="px-4 py-3"><button type="button" class="btn btn-secondary btn-sm" @click="openPromptModal(index)">编辑</button></td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </section>
          </div>

          <div v-else class="space-y-4">
            <select v-model="historyTarget" class="input h-9 max-w-sm">
              <option value="">全部账号</option>
              <option v-for="row in keepaliveOverviewRows" :key="row.name" :value="row.name">{{ row.name }}</option>
            </select>
            <div
              ref="keeperHistoryScrollRef"
              class="max-h-[calc(100vh-340px)] overflow-x-auto overflow-y-auto rounded-t-lg border border-gray-200 bg-white dark:border-dark-700 dark:bg-dark-900"
              @scroll="syncHistoryScrollFromTable"
            >
              <table class="table-fixed divide-y divide-gray-200 text-sm dark:divide-dark-700" :style="{ minWidth: `${HISTORY_TABLE_WIDTH}px` }">
                <thead class="bg-gray-100 text-gray-700 dark:bg-dark-800 dark:text-gray-200">
                  <tr>
                    <th class="w-44 px-4 py-3 text-left">时间</th>
                    <th class="w-48 px-4 py-3 text-left">账号</th>
                    <th class="w-56 px-4 py-3 text-left">状态</th>
                    <th class="w-56 px-4 py-3 text-left">模型</th>
                    <th class="w-72 px-4 py-3 text-left">用量 / 费用</th>
                    <th class="w-96 px-4 py-3 text-left">结果摘要</th>
                    <th class="w-[1120px] px-4 py-3 text-left">详情</th>
                  </tr>
                </thead>
                <tbody class="divide-y divide-gray-200 bg-white dark:divide-dark-700 dark:bg-dark-900">
                  <tr v-for="row in keeperHistoryRows" :key="row.session.id">
                    <td class="px-4 py-3 text-xs">{{ formatDateTime(row.session.started_at) }}<div>{{ formatDateTime(row.session.completed_at) }}</div></td>
                    <td class="truncate px-4 py-3" :title="row.target.name">{{ row.target.name }}</td>
                    <td class="px-4 py-3">
                      <span class="inline-flex rounded-full px-2.5 py-1 text-xs font-medium" :class="sessionStatusClass(row.session)">
                        {{ sessionStatusLabel(row.session) }}
                      </span>
                      <div v-if="sessionError(row.session)" class="mt-1 truncate text-xs text-red-500" :title="sessionError(row.session)">
                        {{ sessionError(row.session) }}
                      </div>
                    </td>
                    <td class="px-4 py-3">{{ sessionModel(row.session) }}<div class="text-xs text-gray-500">{{ sessionMode(row.session) === 'fresh' ? '全新会话' : '接续上次会话' }}</div></td>
                    <td class="px-4 py-3 text-xs" v-html="formatSessionUsageCost(row.session)"></td>
                    <td class="px-4 py-3">
                      <div class="truncate" :title="sessionSummary(row.session)">{{ sessionSummary(row.session) }}</div>
                    </td>
                    <td class="px-4 py-3">
                      <details>
                        <summary class="cursor-pointer text-primary-600 dark:text-primary-400">提示词</summary>
                        <pre class="mt-2 max-h-72 w-full overflow-y-auto whitespace-pre-wrap break-words rounded border border-gray-200 bg-gray-50 p-2 text-xs dark:border-dark-700 dark:bg-dark-800">{{ sessionPrompt(row.session) || '-' }}</pre>
                      </details>
                      <details class="mt-2">
                        <summary class="cursor-pointer text-primary-600 dark:text-primary-400">模型回复</summary>
                        <pre class="mt-2 max-h-72 w-full overflow-y-auto whitespace-pre-wrap break-words rounded border border-gray-200 bg-gray-50 p-2 text-xs dark:border-dark-700 dark:bg-dark-800">{{ sessionReply(row.session) || '-' }}</pre>
                      </details>
                    </td>
                  </tr>
                  <tr v-if="keeperHistoryRows.length === 0">
                    <td colspan="7" class="px-4 py-6 text-center text-gray-500">还没有会话历史。</td>
                  </tr>
                </tbody>
              </table>
            </div>
            <div
              v-if="historyScrollMax > 0"
              class="sticky bottom-0 z-20 rounded-b-lg border border-t-0 border-gray-200 bg-white px-3 py-2 dark:border-dark-700 dark:bg-dark-900"
            >
              <input
                type="range"
                min="0"
                step="1"
                class="block h-2 w-full cursor-pointer accent-primary-600"
                aria-label="会话历史横向滚动"
                :max="historyScrollMax"
                :value="historyScrollLeft"
                @input="syncHistoryScrollFromSlider"
              />
            </div>
          </div>

          <div v-if="keepaliveModalOpen" class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-6">
            <div class="w-full max-w-3xl rounded-lg bg-white shadow-xl dark:bg-dark-800">
              <div class="flex items-center justify-between border-b border-gray-200 px-5 py-4 dark:border-dark-700">
                <h3 class="text-lg font-semibold">{{ editingKeepaliveRow ? '编辑账号' : '添加账号' }}</h3>
                <button type="button" class="btn btn-secondary btn-sm" @click="closeKeepaliveModal">关闭</button>
              </div>
              <div class="grid gap-4 p-5 md:grid-cols-2">
                <label class="space-y-1 text-sm">账号
                  <select v-model.number="keepaliveModal.accountId" class="input" :disabled="!!editingKeepaliveRow" @change="onKeepaliveAccountChange">
                    <option :value="0">选择账号</option>
                    <option v-for="account in keepaliveCandidateAccounts" :key="account.id" :value="account.id">{{ account.name }} (#{{ account.id }})</option>
                  </select>
                </label>
                <label class="space-y-1 text-sm">平台
                  <input class="input" :value="selectedKeepaliveAccount?.platform || ''" disabled />
                </label>
                <label class="space-y-1 text-sm">执行模式
                  <select v-model="keepaliveModal.form.mode" class="input">
                    <option value="resume_last">接续上次会话</option>
                    <option value="fresh">全新会话</option>
                  </select>
                </label>
                <label class="space-y-1 text-sm">模型
                  <select v-model="keepaliveModal.form.model" class="input">
                    <option value="">选择模型</option>
                    <option v-for="model in modalModelOptions" :key="model" :value="model">{{ model }}</option>
                  </select>
                </label>
                <label class="space-y-1 text-sm">保活频率（分钟）
                  <input v-model.number="keepaliveModal.form.intervalMinutes" class="input" type="number" min="1" />
                </label>
                <label class="space-y-1 text-sm">工作开始时间
                  <input v-model.trim="keepaliveModal.form.workStart" class="input" placeholder="04:00" />
                </label>
                <label class="space-y-1 text-sm">工作结束时间
                  <input v-model.trim="keepaliveModal.form.workEnd" class="input" placeholder="24:00" />
                </label>
                <label class="space-y-1 text-sm">工作目录
                  <select v-model="keepaliveModal.form.workspace" class="input">
                    <option value="">选择项目目录</option>
                    <option v-for="project in keepaliveProjectOptions" :key="project" :value="project">{{ project }}</option>
                  </select>
                </label>
                <label class="space-y-1 text-sm md:col-span-2">自定义 prompt
                  <textarea v-model.trim="keepaliveModal.form.prompt" class="input min-h-24" placeholder="留空则优先从提示词题库抽取；题库为空时使用内置只读问题"></textarea>
                </label>
                <label class="flex items-center gap-2 text-sm md:col-span-2">
                  <input v-model="keepaliveModal.form.enabled" type="checkbox" />
                  启用该账号
                </label>
              </div>
              <div class="flex justify-end gap-2 border-t border-gray-200 px-5 py-4 dark:border-dark-700">
                <button v-if="editingKeepaliveRow" type="button" class="btn btn-danger" @click="deleteKeepaliveAccount">删除</button>
                <button type="button" class="btn btn-secondary" @click="closeKeepaliveModal">取消</button>
                <button type="button" class="btn btn-primary" @click="saveKeepaliveModal">保存</button>
              </div>
            </div>
          </div>

          <div v-if="promptModalOpen" class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-6">
            <div class="w-full max-w-2xl rounded-lg bg-white shadow-xl dark:bg-dark-800">
              <div class="flex items-center justify-between border-b border-gray-200 px-5 py-4 dark:border-dark-700">
                <h3 class="text-lg font-semibold">{{ editingPromptIndex === null ? '添加问题' : '编辑问题' }}</h3>
                <button type="button" class="btn btn-secondary btn-sm" @click="closePromptModal">关闭</button>
              </div>
              <div class="grid gap-4 p-5 md:grid-cols-2">
                <label class="space-y-1 text-sm">适用范围
                  <select v-model="promptModal.scope" class="input">
                    <option value="global">通用</option>
                    <option value="project">指定项目</option>
                  </select>
                </label>
                <label class="space-y-1 text-sm">项目
                  <select v-model="promptModal.project_path" class="input" :disabled="promptModal.scope !== 'project'">
                    <option value="">选择项目</option>
                    <option v-for="project in keepaliveProjectOptions" :key="project" :value="`/workspace/projects/${project}`">{{ project }}</option>
                  </select>
                </label>
                <label class="space-y-1 text-sm md:col-span-2">问题内容
                  <textarea v-model.trim="promptModal.text" class="input min-h-28"></textarea>
                </label>
                <label class="flex items-center gap-2 text-sm md:col-span-2">
                  <input v-model="promptModal.enabled" type="checkbox" />
                  启用这个问题
                </label>
              </div>
              <div class="flex justify-end gap-2 border-t border-gray-200 px-5 py-4 dark:border-dark-700">
                <button v-if="editingPromptIndex !== null" type="button" class="btn btn-danger" @click="deletePrompt">删除</button>
                <button type="button" class="btn btn-secondary" @click="closePromptModal">取消</button>
                <button type="button" class="btn btn-primary" @click="savePrompt">保存</button>
              </div>
            </div>
          </div>
        </div>
      </template>
    </TablePageLayout>
  </AppLayout>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
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
const HISTORY_TABLE_WIDTH = 2400

const KEEPER_ENABLED_KEY = 'keeper_keepalive_enabled'
const KEEPER_INTERVAL_KEY = 'keeper_keepalive_interval_minutes'
const KEEPER_MODEL_KEY = 'keeper_keepalive_model'
const KEEPER_MODE_KEY = 'keeper_keepalive_mode'
const KEEPER_WORKSPACE_KEY = 'keeper_keepalive_workspace'
const KEEPER_WORK_START_KEY = 'keeper_keepalive_work_start'
const KEEPER_WORK_END_KEY = 'keeper_keepalive_work_end'
const KEEPER_PROMPT_KEY = 'keeper_keepalive_prompt'

type TabKey = 'mimic' | 'keepalive'
type KeepaliveTabKey = 'overview' | 'settings' | 'history'

interface KeepaliveForm {
  enabled: boolean
  intervalMinutes: number
  model: string
  mode: 'resume_last' | 'fresh'
  workspace: string
  workStart: string
  workEnd: string
  prompt: string
}

interface KeepaliveRow {
  id: number
  account: Account
  form: KeepaliveForm
}

interface PromptQuestion {
  id?: string
  scope: 'global' | 'project'
  project_path?: string
  enabled: boolean
  text: string
}

const { t } = useI18n()
const appStore = useAppStore()

const activeTab = ref<TabKey>('mimic')
const activeTabClass = 'bg-primary-600 text-white shadow-sm'
const inactiveTabClass = 'text-gray-600 hover:bg-gray-100 dark:text-gray-300 dark:hover:bg-dark-700'
const keepaliveTabs: Array<{ key: KeepaliveTabKey; label: string }> = [
  { key: 'overview', label: '概览' },
  { key: 'settings', label: '配置' },
  { key: 'history', label: '会话历史' }
]

const mimicAccounts = ref<Account[]>([])
const mimicLoading = ref(false)
const mimicUpdatingIds = ref<Set<number>>(new Set())

const keepaliveRows = ref<KeepaliveRow[]>([])
const keepaliveLoading = ref(false)
const keepaliveUpdatingIds = ref<Set<number>>(new Set())
const keepaliveProjectOptions = ref<string[]>([])
const keepaliveModelOptions = ref<Record<number, string[]>>({})
const keepaliveModelLoadingIds = ref<Set<number>>(new Set())
const keepaliveTab = ref<KeepaliveTabKey>('overview')
const keeperState = ref<any>({})
const keeperVersion = ref('')
const promptGuard = ref('')
const promptBankRows = ref<PromptQuestion[]>([])
const historyTarget = ref('')
const keeperHistoryScrollRef = ref<HTMLElement | null>(null)
const historyScrollLeft = ref(0)
const historyScrollMax = ref(0)
const keepaliveModalOpen = ref(false)
const editingKeepaliveRow = ref<KeepaliveRow | null>(null)
const keepaliveModal = ref<{ accountId: number; form: KeepaliveForm }>({ accountId: 0, form: emptyKeepaliveForm() })
const promptModalOpen = ref(false)
const editingPromptIndex = ref<number | null>(null)
const promptModal = ref<PromptQuestion>({ scope: 'global', project_path: '', enabled: true, text: '' })

const currentLoading = computed(() => activeTab.value === 'mimic' ? mimicLoading.value : keepaliveLoading.value)

const mimicColumns = computed<Column[]>(() => [
  { key: 'account', label: t('admin.apiKeyMimic.columns.account') },
  { key: 'platform', label: t('admin.apiKeyMimic.columns.platform'), sortable: true },
  { key: 'compatible', label: t('admin.apiKeyMimic.columns.compatible') },
  { key: 'status', label: t('admin.apiKeyMimic.columns.status') }
])

const configuredKeepaliveRows = computed(() => keepaliveRows.value.filter(row => isKeepaliveConfigured(row.account)))
const keepaliveCandidateAccounts = computed(() => keepaliveRows.value.map(row => row.account))
const selectedKeepaliveAccount = computed(() => keepaliveCandidateAccounts.value.find(account => account.id === keepaliveModal.value.accountId) || null)
const modalModelOptions = computed(() => {
  const accountID = keepaliveModal.value.accountId
  const options = accountID ? (keepaliveModelOptions.value[accountID] || []) : []
  const model = keepaliveModal.value.form.model
  if (model && !options.includes(model)) return [model, ...options]
  return options
})
const keepaliveOverviewRows = computed(() => {
  const overview = Array.isArray(keeperState.value?.overview) ? keeperState.value.overview : []
  const targets = Array.isArray(keeperState.value?.targets) ? keeperState.value.targets : []
  const configured = Array.isArray(keeperState.value?.configured_targets) ? keeperState.value.configured_targets : []
  if (overview.length > 0) {
    const normalizedTargets = targets.map((target: any) => normalizeKeeperTarget(target, configured))
    const targetByAccountID = new Map(normalizedTargets.filter((target: any) => target.account_id > 0).map((target: any) => [target.account_id, target]))
    const targetByName = new Map(normalizedTargets.map((target: any) => [String(target.name || ''), target]))
    return overview.map((row: any) => {
      const accountID = Number(row.account_id || row.AccountID || 0)
      return normalizeOverviewRow(row, targetByAccountID.get(accountID) || targetByName.get(String(row.name || row.Name || '')))
    })
  }
  const configuredNames = new Set(configured.map((item: any) => String(item.name || item.Name || '').trim()).filter(Boolean))
  return targets
    .filter((target: any) => configuredNames.size === 0 || configuredNames.has(String(target.name || target.Name || '').trim()))
    .map((target: any) => normalizeKeeperTarget(target, configured))
})
const keepaliveDashboard = computed(() => {
  const dashboard = keeperState.value?.dashboard || {}
  const rows = keepaliveOverviewRows.value
  const fallback = rows.reduce((acc: any, row: any) => {
    acc.total += 1
    if (row.enabled) acc.enabled += 1
    if (row.running) acc.running += 1
    for (const session of row.sessions || []) {
      if (!isToday(session.completed_at || session.started_at)) continue
      if (session.status === 'success') acc.todaySuccess += 1
      if (session.status === 'error') acc.todayFailure += 1
    }
    return acc
  }, { total: 0, enabled: 0, running: 0, todaySuccess: 0, todayFailure: 0 })
  return {
    total: Number(dashboard.total_targets ?? dashboard.total_accounts ?? fallback.total),
    enabled: Number(dashboard.enabled_targets ?? dashboard.enabled_accounts ?? fallback.enabled),
    running: Number(dashboard.running_count ?? fallback.running),
    todaySuccess: Number(dashboard.today_successes ?? fallback.todaySuccess),
    todayFailure: Number(dashboard.today_failures ?? fallback.todayFailure)
  }
})
const keeperHistoryRows = computed(() => {
  const rows: Array<{ target: any; session: any }> = []
  for (const target of keepaliveOverviewRows.value) {
    if (historyTarget.value && target.name !== historyTarget.value) continue
    for (const session of target.sessions || []) rows.push({ target, session })
  }
  return rows.sort((a, b) => new Date(b.session.started_at || b.session.StartedAt || 0).getTime() - new Date(a.session.started_at || a.session.StartedAt || 0).getTime())
})

function refreshHistoryScrollMetrics() {
  const el = keeperHistoryScrollRef.value
  if (!el) {
    historyScrollLeft.value = 0
    historyScrollMax.value = 0
    return
  }
  const max = Math.max(0, el.scrollWidth - el.clientWidth)
  historyScrollMax.value = max
  if (el.scrollLeft > max) el.scrollLeft = max
  historyScrollLeft.value = Math.min(max, Math.max(0, el.scrollLeft))
}

function scheduleHistoryScrollMetrics() {
  nextTick(() => {
    refreshHistoryScrollMetrics()
  })
}

function syncHistoryScrollFromTable() {
  const el = keeperHistoryScrollRef.value
  if (!el) return
  historyScrollLeft.value = Math.min(historyScrollMax.value, Math.max(0, el.scrollLeft))
}

function syncHistoryScrollFromSlider(event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  const left = Number.isFinite(value) ? value : 0
  historyScrollLeft.value = left
  if (keeperHistoryScrollRef.value) {
    keeperHistoryScrollRef.value.scrollLeft = left
  }
}

function handleHistoryResize() {
  if (activeTab.value === 'keepalive' && keepaliveTab.value === 'history') {
    refreshHistoryScrollMetrics()
  }
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
    const [anthropicAccounts, openaiAccounts, projects, state, settings] = await Promise.all([
      fetchAccounts('anthropic'),
      fetchAccounts('openai'),
      adminAPI.accounts.getKeeperProjects(),
      adminAPI.accounts.getKeeperState(),
      adminAPI.accounts.getKeeperSettings()
    ])
    keepaliveProjectOptions.value = projects
    keeperState.value = state || {}
    keeperVersion.value = String(settings?.version || state?.version || '')
    promptGuard.value = String(settings?.prompt_guard || state?.prompt_guard || '')
    promptBankRows.value = Array.isArray(settings?.prompt_bank) ? settings.prompt_bank : (Array.isArray(state?.prompt_bank) ? state.prompt_bank : [])
    keepaliveRows.value = [...anthropicAccounts, ...openaiAccounts]
      .filter(account => account.platform === 'anthropic' || account.platform === 'openai')
      .sort((a, b) => b.id - a.id)
      .map(account => ({ id: account.id, account, form: buildKeepaliveForm(account, projects) }))
    await Promise.allSettled(keepaliveRows.value.map(row => loadModelOptions(row)))
    scheduleHistoryScrollMetrics()
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

function buildKeepaliveForm(account: Account, projects = keepaliveProjectOptions.value): KeepaliveForm {
  const extra = account.extra || {}
  const workspace = typeof extra[KEEPER_WORKSPACE_KEY] === 'string' ? extra[KEEPER_WORKSPACE_KEY] : ''
  return {
    enabled: extra[KEEPER_ENABLED_KEY] === true,
    intervalMinutes: normalizeInterval(extra[KEEPER_INTERVAL_KEY]),
    model: typeof extra[KEEPER_MODEL_KEY] === 'string' ? extra[KEEPER_MODEL_KEY] : '',
    mode: typeof extra[KEEPER_MODE_KEY] === 'string' ? normalizeKeepaliveMode(extra[KEEPER_MODE_KEY]) : defaultKeepaliveModeForAccount(account),
    workspace: workspace || (projects.length === 1 ? projects[0] : ''),
    workStart: typeof extra[KEEPER_WORK_START_KEY] === 'string' ? extra[KEEPER_WORK_START_KEY] : '04:00',
    workEnd: typeof extra[KEEPER_WORK_END_KEY] === 'string' ? extra[KEEPER_WORK_END_KEY] : '24:00',
    prompt: typeof extra[KEEPER_PROMPT_KEY] === 'string' ? extra[KEEPER_PROMPT_KEY] : ''
  }
}

function emptyKeepaliveForm(): KeepaliveForm {
  return {
    enabled: true,
    intervalMinutes: 8,
    model: '',
    mode: 'fresh',
    workspace: '',
    workStart: '04:00',
    workEnd: '24:00',
    prompt: ''
  }
}

function isKeepaliveConfigured(account: Account): boolean {
  const extra = account.extra || {}
  return extra[KEEPER_ENABLED_KEY] === true ||
    String(extra[KEEPER_MODEL_KEY] || '').trim() !== '' ||
    String(extra[KEEPER_WORKSPACE_KEY] || '').trim() !== '' ||
    String(extra[KEEPER_PROMPT_KEY] || '').trim() !== ''
}

function normalizeKeeperTarget(target: any, configured: any[]): any {
  const name = String(target.name || target.Name || '')
  const config = configured.find((item: any) => String(item.name || item.Name || '') === name) || {}
  const sessions = Array.isArray(target.sessions) ? target.sessions : (Array.isArray(target.Sessions) ? target.Sessions : [])
  return {
    name,
    account_id: Number(target.account_id || target.AccountID || config.account_id || config.AccountID || 0),
    platform: target.platform || target.Platform || config.platform || config.Platform || '',
    account_type: target.account_type || target.AccountType || config.account_type || config.AccountType || '',
    executor: target.executor || target.Executor || config.executor || config.Executor || '',
    model: target.model || target.Model || config.model || config.Model || '',
    mode: normalizeKeepaliveMode(target.mode || target.Mode || config.mode || config.Mode),
    enabled: Boolean(target.enabled ?? target.Enabled ?? config.enabled ?? config.Enabled),
    running: Boolean(target.running ?? target.Running),
    current_status: target.current_status || target.CurrentStatus || '',
    status_class: target.status_class || target.StatusClass || '',
    status_detail: target.status_detail || target.StatusDetail || target.last_error || target.LastError || '',
    last_message_summary: target.last_message_summary || target.LastMessageSummary || '',
    execution_count: target.execution_count ?? target.ExecutionCount ?? sessionCounts(sessions).total,
    success_count: target.success_count ?? target.SuccessCount ?? sessionCounts(sessions).success,
    failure_count: target.failure_count ?? target.FailureCount ?? sessionCounts(sessions).failure,
    usage_24h_cost: target.usage_24h_cost || target.Usage24hCost,
    total_usage_cost: target.total_usage_cost || target.TotalUsageCost,
    last_keepalive_started_at: target.last_keepalive_started_at || target.LastKeepaliveStartedAt,
    last_keepalive_received_at: target.last_keepalive_received_at || target.LastKeepaliveReceivedAt,
    last_finished_at: target.last_finished_at || target.LastFinishedAt || target.last_keepalive_received_at || target.LastKeepaliveReceivedAt,
    next_run_at: target.next_run_at || target.NextRunAt,
    sessions
  }
}

function normalizeOverviewRow(row: any, target?: any): any {
  return {
    ...target,
    name: String(row.name || row.Name || target?.name || ''),
    account_id: Number(row.account_id || row.AccountID || target?.account_id || 0),
    platform: row.platform || row.Platform || target?.platform || '',
    account_type: row.account_type || row.AccountType || target?.account_type || '',
    executor: row.executor || row.Executor || target?.executor || '',
    model: row.model || row.Model || target?.model || '',
    mode: normalizeKeepaliveMode(row.mode || row.Mode || target?.mode),
    enabled: Boolean(row.enabled ?? row.Enabled ?? target?.enabled),
    running: Boolean(row.running ?? row.Running ?? target?.running),
    current_status: row.current_status || row.CurrentStatus || target?.current_status || '',
    status_class: row.status_class || row.StatusClass || target?.status_class || '',
    status_detail: row.status_detail || row.StatusDetail || target?.status_detail || '',
    last_message_summary: row.last_message_summary || row.LastMessageSummary || target?.last_message_summary || '',
    consecutive_failures: Number(row.consecutive_failures || row.ConsecutiveFailures || target?.consecutive_failures || 0),
    execution_count: Number(row.execution_count ?? row.ExecutionCount ?? target?.execution_count ?? 0),
    success_count: Number(row.success_count ?? row.SuccessCount ?? target?.success_count ?? 0),
    failure_count: Number(row.failure_count ?? row.FailureCount ?? target?.failure_count ?? 0),
    last_keepalive_started_at: row.last_started_at || row.LastStartedAt || target?.last_keepalive_started_at,
    last_keepalive_received_at: row.last_keepalive_received_at || row.LastKeepaliveReceivedAt || row.last_finished_at || row.LastFinishedAt || target?.last_keepalive_received_at,
    last_finished_at: row.last_finished_at || row.LastFinishedAt || target?.last_finished_at,
    next_run_at: row.next_run_at || row.NextRunAt || target?.next_run_at,
    usage_24h_cost: row.usage_24h_cost || row.Usage24hCost || target?.usage_24h_cost,
    total_usage_cost: row.total_usage_cost || row.TotalUsageCost || target?.total_usage_cost,
    sessions: Array.isArray(target?.sessions) ? target.sessions : []
  }
}

function projectName(path: string | undefined): string {
  const value = String(path || '')
  const prefix = '/workspace/projects/'
  if (value.startsWith(prefix)) return value.slice(prefix.length)
  return value || '-'
}

function normalizeInterval(value: unknown): number {
  const n = Number(value)
  if (!Number.isFinite(n) || n <= 0) return 8
  return Math.max(1, Math.round(n))
}

function defaultKeepaliveModeForAccount(account?: Account | null): 'resume_last' | 'fresh' {
  return account?.platform === 'openai' ? 'fresh' : 'resume_last'
}

function normalizeKeepaliveMode(value: unknown): 'resume_last' | 'fresh' {
  return String(value || '').trim() === 'fresh' ? 'fresh' : 'resume_last'
}

async function loadModelOptions(row: KeepaliveRow) {
  if (keepaliveModelOptions.value[row.account.id] || keepaliveModelLoadingIds.value.has(row.account.id)) return
  keepaliveModelLoadingIds.value = new Set(keepaliveModelLoadingIds.value).add(row.account.id)
  try {
    const models = await adminAPI.accounts.getAvailableModels(row.account.id)
    const ids = Array.from(new Set(
      models
        .map((model: any) => String(model.id || model.name || model.display_name || '').trim())
        .filter(Boolean)
    ))
    keepaliveModelOptions.value = {
      ...keepaliveModelOptions.value,
      [row.account.id]: ids
    }
  } finally {
    const next = new Set(keepaliveModelLoadingIds.value)
    next.delete(row.account.id)
    keepaliveModelLoadingIds.value = next
  }
}

function formatDateTime(value: string | null | undefined): string {
  if (!value) return t('admin.accountKeepalive.labels.neverUsed')
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return '-'
  return date.toLocaleString()
}

function isToday(value: string | null | undefined): boolean {
  if (!value) return false
  const date = new Date(value)
  if (!Number.isFinite(date.getTime())) return false
  const now = new Date()
  return date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth() && date.getDate() === now.getDate()
}

function sessionCounts(sessions: any[] = []) {
  return sessions.reduce((acc, session) => {
    if (session.status === 'success') {
      acc.total += 1
      acc.success += 1
    } else if (session.status === 'error') {
      acc.total += 1
      acc.failure += 1
    }
    return acc
  }, { total: 0, success: 0, failure: 0 })
}

function buildUsageCostSummary(sessions: any[] = []) {
  const since = Date.now() - 24 * 60 * 60 * 1000
  const summary = sessions.reduce((acc, session) => {
    const time = new Date(session.completed_at || session.started_at || 0).getTime()
    if (!Number.isFinite(time) || time < since) return acc
    acc.total_tokens += Number(session.usage?.total_tokens || 0)
    const cost = Number(session.billing?.actual_cost || session.billing?.total_cost || 0)
    if (cost > 0 || session.billing?.available) {
      acc.total_cost += cost
      acc.has_cost = true
    }
    return acc
  }, { total_tokens: 0, currency: 'USD', total_cost: 0, has_cost: false, precise: true })
  return summary.total_tokens > 0 || summary.has_cost ? summary : null
}

function keepaliveStatusClass(row: any): string {
  const statusClass = String(row.status_class || '')
  if (statusClass === 'err') return 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300'
  if (statusClass === 'warn' || row.running) return 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
  if (statusClass === 'ok' || row.enabled) return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
  return 'bg-gray-100 text-gray-600 dark:bg-dark-700 dark:text-gray-300'
}

function sessionStatus(session: any): string {
  return String(session?.status || session?.Status || '').trim()
}

function sessionStatusLabel(session: any): string {
  const status = sessionStatus(session)
  if (status === 'success') return '成功'
  if (status === 'error') return '失败'
  if (status === 'running') return '执行中'
  return status || '-'
}

function sessionStatusClass(session: any): string {
  const status = sessionStatus(session)
  if (status === 'success') return 'bg-emerald-100 text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-300'
  if (status === 'error') return 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300'
  if (status === 'running') return 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300'
  return 'bg-gray-100 text-gray-600 dark:bg-dark-700 dark:text-gray-300'
}

function sessionError(session: any): string {
  return String(session?.error || session?.Error || '').trim()
}

function sessionPrompt(session: any): string {
  return String(session?.prompt || session?.Prompt || '').trim()
}

function sessionReply(session: any): string {
  return String(session?.reply_text || session?.ReplyText || '').trim()
}

function sessionModel(session: any): string {
  return String(session?.model || session?.Model || '').trim() || '-'
}

function sessionMode(session: any): 'resume_last' | 'fresh' {
  return normalizeKeepaliveMode(session?.mode || session?.Mode)
}

function sessionSummary(session: any): string {
  return String(sessionReply(session) || sessionError(session) || session?.summary || session?.Summary || sessionStatusLabel(session) || '-')
}

function escapeHtml(value: unknown): string {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;')
}

function formatNumber(value: unknown): string {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n.toLocaleString('zh-CN') : '0'
}

function formatMoney(value: unknown): string {
  const n = Number(value || 0)
  return Number.isFinite(n) ? n.toFixed(6) : '0.000000'
}

function formatOverviewUsageCost(summary: any): string {
  if (!summary || !Number(summary.total_tokens || 0)) return '-'
  const tokenText = `Token：${formatNumber(summary.total_tokens)}`
  if (!summary.has_cost) return `${escapeHtml(tokenText)}<div class="mt-1 text-gray-500">费用：未配置价格</div>`
  const currency = summary.currency || 'USD'
  const label = summary.precise === false ? '估算费用' : '费用'
  return `${escapeHtml(tokenText)}<div class="mt-1 text-gray-500">${escapeHtml(`${label}：${currency} ${formatMoney(summary.total_cost)}`)}</div>`
}

function formatSessionUsageCost(session: any): string {
  const usage = session?.usage || session?.Usage || {}
  const billing = session?.billing || session?.Billing || {}
  const input = formatNumber(usage.input_tokens ?? usage.InputTokens)
  const output = formatNumber(usage.output_tokens ?? usage.OutputTokens)
  const cached = formatNumber(usage.cache_read_tokens ?? usage.cached_input_tokens ?? usage.CacheReadTokens)
  const cacheCreated = formatNumber(usage.cache_creation_tokens ?? usage.cache_creation_input_tokens ?? usage.CacheCreationTokens)
  const usageText = `输入 ${input} / 输出 ${output} / 缓存读取 ${cached} / 缓存创建 ${cacheCreated}`
  if (!billing.available && !Number(billing.actual_cost || billing.total_cost || billing.ActualCost || billing.TotalCost || 0)) {
    return `${escapeHtml(usageText)}<div class="mt-1 text-gray-500">费用：未配置价格</div>`
  }
  const cost = Number(billing.actual_cost || billing.total_cost || billing.ActualCost || billing.TotalCost || 0)
  return `${escapeHtml(usageText)}<div class="mt-1 text-gray-500">${escapeHtml(`费用：USD ${formatMoney(cost)}`)}</div>`
}

function buildKeepalivePatch(row: KeepaliveRow): Record<string, unknown> {
  return {
    [KEEPER_ENABLED_KEY]: row.form.enabled,
    [KEEPER_INTERVAL_KEY]: normalizeInterval(row.form.intervalMinutes),
    [KEEPER_MODEL_KEY]: row.form.model,
    [KEEPER_MODE_KEY]: row.form.mode,
    [KEEPER_WORKSPACE_KEY]: row.form.workspace,
    [KEEPER_WORK_START_KEY]: row.form.workStart,
    [KEEPER_WORK_END_KEY]: row.form.workEnd,
    [KEEPER_PROMPT_KEY]: row.form.prompt
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

async function openKeepaliveModal(row?: KeepaliveRow) {
  editingKeepaliveRow.value = row || null
  const account = row?.account || keepaliveCandidateAccounts.value[0]
  keepaliveModal.value = {
    accountId: account?.id || 0,
    form: row ? { ...row.form } : {
      ...emptyKeepaliveForm(),
      mode: defaultKeepaliveModeForAccount(account),
      workspace: keepaliveProjectOptions.value.length === 1 ? keepaliveProjectOptions.value[0] : ''
    }
  }
  keepaliveModalOpen.value = true
  if (keepaliveModal.value.accountId) await loadModalModels()
}

function closeKeepaliveModal() {
  keepaliveModalOpen.value = false
  editingKeepaliveRow.value = null
}

async function onKeepaliveAccountChange() {
  const account = selectedKeepaliveAccount.value
  if (account) {
    keepaliveModal.value.form.mode = defaultKeepaliveModeForAccount(account)
  }
  await loadModalModels()
}

async function loadModalModels() {
  const account = selectedKeepaliveAccount.value
  if (!account) return
  const row = keepaliveRows.value.find(item => item.account.id === account.id)
  if (row) await loadModelOptions(row)
}

async function saveKeepaliveModal() {
  const account = selectedKeepaliveAccount.value
  if (!account) {
    appStore.showError('请选择账号')
    return
  }
  if (!keepaliveModal.value.form.model) {
    appStore.showError('请选择模型')
    return
  }
  if (!keepaliveModal.value.form.workspace) {
    appStore.showError('请选择工作目录')
    return
  }
  const row: KeepaliveRow = {
    id: account.id,
    account,
    form: {
      ...keepaliveModal.value.form,
      intervalMinutes: normalizeInterval(keepaliveModal.value.form.intervalMinutes)
    }
  }
  await saveKeepalive(row)
  closeKeepaliveModal()
  await loadKeepaliveAccounts()
}

async function deleteKeepaliveAccount() {
  const account = selectedKeepaliveAccount.value
  if (!account) return
  const patch: Record<string, unknown> = {
    [KEEPER_ENABLED_KEY]: false,
    [KEEPER_INTERVAL_KEY]: null,
    [KEEPER_MODEL_KEY]: '',
    [KEEPER_MODE_KEY]: '',
    [KEEPER_WORKSPACE_KEY]: '',
    [KEEPER_WORK_START_KEY]: '',
    [KEEPER_WORK_END_KEY]: '',
    [KEEPER_PROMPT_KEY]: ''
  }
  await adminAPI.accounts.updateExtra(account.id, patch)
  closeKeepaliveModal()
  await loadKeepaliveAccounts()
}

async function runKeepalive(target: string) {
  if (!target) return
  try {
    keepaliveUpdatingIds.value = new Set(keepaliveUpdatingIds.value).add(Number(keepaliveOverviewRows.value.find((row: any) => row.name === target)?.account_id || 0))
    await adminAPI.accounts.runKeeperTarget(target)
    appStore.showSuccess('已提交立即执行')
    await loadKeepaliveAccounts()
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || '立即执行失败')
  } finally {
    keepaliveUpdatingIds.value = new Set()
  }
}

function openHistory(target: string) {
  historyTarget.value = target
  keepaliveTab.value = 'history'
}

function latestSessionSummary(row: any): string {
  const sessions = Array.isArray(row.sessions) ? row.sessions : []
  const latest = sessions[0]
  if (!latest) return '-'
  return sessionSummary(latest)
}

function openPromptModal(index?: number) {
  editingPromptIndex.value = typeof index === 'number' ? index : null
  const item = editingPromptIndex.value === null ? null : promptBankRows.value[editingPromptIndex.value]
  promptModal.value = item ? { ...item } : { scope: 'global', project_path: '', enabled: true, text: '' }
  promptModalOpen.value = true
}

function closePromptModal() {
  promptModalOpen.value = false
  editingPromptIndex.value = null
}

async function savePrompt() {
  if (!promptModal.value.text.trim()) {
    appStore.showError('问题内容不能为空')
    return
  }
  if (promptModal.value.scope === 'project' && !promptModal.value.project_path) {
    appStore.showError('指定项目问题必须选择项目')
    return
  }
  const next = [...promptBankRows.value]
  const item = {
    ...promptModal.value,
    id: promptModal.value.id || `prompt-${Date.now()}`
  }
  if (editingPromptIndex.value === null) next.push(item)
  else next[editingPromptIndex.value] = item
  promptBankRows.value = next
  await savePromptSettings()
  closePromptModal()
}

async function deletePrompt() {
  if (editingPromptIndex.value === null) return
  const next = [...promptBankRows.value]
  next.splice(editingPromptIndex.value, 1)
  promptBankRows.value = next
  await savePromptSettings()
  closePromptModal()
}

async function savePromptSettings() {
  try {
    const saved = await adminAPI.accounts.saveKeeperSettings({
      prompt_guard: promptGuard.value,
      prompt_bank: promptBankRows.value
    })
    keeperVersion.value = String(saved?.version || keeperVersion.value)
    promptGuard.value = String(saved?.prompt_guard || promptGuard.value)
    promptBankRows.value = Array.isArray(saved?.prompt_bank) ? saved.prompt_bank : promptBankRows.value
    appStore.showSuccess('提示词设置已保存')
    await loadKeepaliveAccounts()
  } catch (error: any) {
    appStore.showError(error.response?.data?.detail || '保存提示词设置失败')
  }
}

watch(activeTab, (tab) => {
  if (tab === 'keepalive' && keepaliveRows.value.length === 0) {
    loadKeepaliveAccounts()
  }
  if (tab === 'keepalive') {
    scheduleHistoryScrollMetrics()
  }
})

watch(keepaliveTab, (tab) => {
  if (tab === 'history') {
    scheduleHistoryScrollMetrics()
  }
})

watch(historyTarget, () => {
  scheduleHistoryScrollMetrics()
})

watch(keeperHistoryRows, () => {
  scheduleHistoryScrollMetrics()
})

onMounted(() => {
  loadMimicAccounts()
  window.addEventListener('resize', handleHistoryResize)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleHistoryResize)
})
</script>
