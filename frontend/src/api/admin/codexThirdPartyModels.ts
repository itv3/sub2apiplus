/**
 * Codex 三方模型启用：管理端接口与本模块类型。
 *
 * 管理员按「分组 + 公开模型名」开启第三方模型，网关在 Codex 在线目录里发布已开启的模型。
 * 开启必须先「预览」再「保存」：预览返回目录评估结果与输入指纹，保存时带回指纹，
 * 服务端会重新评估并核对；关闭不依赖预览，直接保存。
 *
 * 响应里不含任何账号凭据。本文件不并入 api/admin/index.ts，由面板组件直接引用。
 */

import { apiClient } from '../client'

/** 候选模型在页面上的状态。 */
export type CodexThirdPartyModelStatus =
  | 'native'
  | 'needs_capability'
  | 'configurable'
  | 'verified'
  | 'conflict'
  | 'over_budget'
  | 'unavailable'

/**
 * 账号模型 ID 源快照的状态：missing 表示从未同步；official_skipped 表示官方 OpenAI 账号
 * （OAuth、SetupToken 或官方地址的 API Key），不可能承接第三方模型，不同步也不计为过期。
 */
export type CodexCatalogSourceStatus = 'missing' | 'complete' | 'empty' | 'failed' | 'official_skipped'

export interface CodexModelVerification {
  fingerprint: string
  verified_at: string
  note?: string
}

export interface CodexModelVisibilityEntry {
  enabled: boolean
  verification?: CodexModelVerification
}

/** 分组展示配置；revision 为 0 表示尚未保存过。 */
export interface CodexModelVisibilityConfig {
  schema_version: number
  revision: number
  models: Record<string, CodexModelVisibilityEntry>
}

export interface CodexCapabilitySummary {
  input_modalities?: string[]
  reasoning_levels?: string[]
  default_reasoning_level?: string
  context_window?: number
  shell_type?: string
  apply_patch_tool_type?: string
  supports_search_tool: boolean
  web_search_tool_type?: string
}

export interface CodexThirdPartyCandidate {
  slug: string
  display_name: string
  /** 原生 GPT 条目，只读展示。 */
  native: boolean
  /** 管理员意图：在 Codex 中启用。 */
  enabled: boolean
  /** 静态发布条件满足、允许开启。 */
  can_enable: boolean
  /** 按当前配置会进入最终目录。 */
  published: boolean
  status: CodexThirdPartyModelStatus
  reasons?: string[]
  sources?: string[]
  target_platform?: string
  upstream_model?: string
  account_ids?: number[]
  protocol?: string
  profile_id?: string
  fingerprint?: string
  capabilities: CodexCapabilitySummary
  entry_bytes?: number
}

export interface CodexCatalogBudget {
  limit_bytes: number
  native_count: number
  enabled_third_party_count: number
  worst_case_bytes: number
  remaining_bytes: number
  /** 为 false 时预算检查未完成，不得显示为通过。 */
  complete: boolean
  reason?: string
  base_account_ids?: number[]
}

export interface CodexThirdPartyCatalogEvaluation {
  group_id: number
  group_platform: string
  revision: number
  /** 除开关外全部评估输入的摘要；保存开启时用来确认预览仍然有效。 */
  input_fingerprint: string
  candidates: CodexThirdPartyCandidate[]
  budget: CodexCatalogBudget
  evaluated_at: string
}

export interface CodexThirdPartySourceStatus {
  account_id: number
  account_name: string
  platform: string
  account_status: string
  status: CodexCatalogSourceStatus
  synced_at?: string
  attempted_at?: string
  stale: boolean
  fingerprint_matches: boolean
  model_count: number
  last_error?: string
  refreshing: boolean
}

export interface CodexThirdPartyGroupState {
  group_id: number
  group_name: string
  group_platform: string
  config: CodexModelVisibilityConfig
  evaluation: CodexThirdPartyCatalogEvaluation
  sources: CodexThirdPartySourceStatus[]
}

export interface CodexThirdPartyChange {
  slug: string
  enabled: boolean
}

export interface CodexThirdPartyPreviewResult {
  revision: number
  evaluation: CodexThirdPartyCatalogEvaluation
}

export interface CodexThirdPartySaveResult {
  config: CodexModelVisibilityConfig
  evaluation?: CodexThirdPartyCatalogEvaluation
}

export interface CodexThirdPartyVerifyResult {
  config: CodexModelVisibilityConfig
}

export interface CodexThirdPartySyncResult {
  sources: CodexThirdPartySourceStatus[]
}

const base = (groupId: number) => `/admin/codex-third-party-models/${groupId}`

/** 读取分组的展示配置、候选评估结果和源快照状态；缺失或过期的源会在后台自动刷新。 */
export async function getGroupState(groupId: number): Promise<CodexThirdPartyGroupState> {
  const { data } = await apiClient.get<CodexThirdPartyGroupState>(base(groupId))
  return data
}

/** 在当前配置的副本上叠加变更，返回评估结果与输入指纹；不落库。 */
export async function previewChanges(
  groupId: number,
  expectedRevision: number,
  changes: CodexThirdPartyChange[]
): Promise<CodexThirdPartyPreviewResult> {
  const { data } = await apiClient.post<CodexThirdPartyPreviewResult>(`${base(groupId)}/preview`, {
    expected_revision: expectedRevision,
    changes
  })
  return data
}

/**
 * 保存一批变更，整批原子提交。含开启的变更必须携带预览返回的 inputFingerprint；
 * 纯关闭可以省略。
 */
export async function saveChanges(
  groupId: number,
  expectedRevision: number,
  changes: CodexThirdPartyChange[],
  inputFingerprint?: string
): Promise<CodexThirdPartySaveResult> {
  const { data } = await apiClient.put<CodexThirdPartySaveResult>(base(groupId), {
    expected_revision: expectedRevision,
    changes,
    ...(inputFingerprint ? { input_fingerprint: inputFingerprint } : {})
  })
  return data
}

/** 把一次真实调用验收绑定到候选当前的指纹；指纹变化后验收记录自动失效。 */
export async function markVerified(
  groupId: number,
  expectedRevision: number,
  slug: string,
  fingerprint: string,
  note?: string
): Promise<CodexThirdPartyVerifyResult> {
  const { data } = await apiClient.post<CodexThirdPartyVerifyResult>(`${base(groupId)}/verify`, {
    expected_revision: expectedRevision,
    slug,
    fingerprint,
    ...(note ? { note } : {})
  })
  return data
}

/** 对分组全部账号强制刷新模型 ID 源；后台执行，立即返回各账号状态。 */
export async function syncSources(groupId: number): Promise<CodexThirdPartySyncResult> {
  const { data } = await apiClient.post<CodexThirdPartySyncResult>(`${base(groupId)}/sync`)
  return data
}

export const codexThirdPartyModelsAPI = {
  getGroupState,
  previewChanges,
  saveChanges,
  markVerified,
  syncSources
}

export default codexThirdPartyModelsAPI
