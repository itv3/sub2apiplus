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

/** 跨平台承接时路由结构里的一个账号（只含展示信息，不含凭据）。 */
export interface CodexThirdPartyRouteAccount {
  id: number
  name: string
  platform: string
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
  /**
   * 仅 Composite 分组按账号级跨平台候选池承接时出现：首选账号决定 target_platform，
   * 备选账号是同一承接分组里的其余账号（只读展示）。
   */
  primary_account?: CodexThirdPartyRouteAccount
  alternate_accounts?: CodexThirdPartyRouteAccount[]
  protocol?: string
  profile_id?: string
  fingerprint?: string
  /** 非空表示该候选因这个上游型号没有能力档案而无法发布（待补能力），页面可提供“关联到已有档案”。 */
  profile_missing_model?: string
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
  /**
   * 为 true 表示页面读取时没有发起任何上游请求、也没有官方账号目录缓存，体积预算留到预览与保存（开启）时检查。
   * 此时 complete 为 false，页面显示“开启时检查”。
   */
  deferred?: boolean
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

/** 刷新的目录类别：model_protocol 是按模型分流账号的上游模型目录，official_base 是官方 ChatGPT 账号的基础目录。 */
export type CodexCatalogRefreshKind = 'model_protocol' | 'official_base'

/** 一个账号一类目录的刷新结果；失败原因是分类后的简短说明，不含地址与凭据。 */
export interface CodexCatalogRefreshItem {
  account_id: number
  account_name: string
  platform: string
  kind: CodexCatalogRefreshKind
  ok: boolean
  reason?: string
  elapsed_ms: number
  model_count?: number
}

export interface CodexCatalogRefreshReport {
  group_id: number
  refreshed_at: string
  /** 为 true 表示短时间内重复刷新，直接复用了上一次的结果，没有再请求上游。 */
  reused: boolean
  total: number
  failed: number
  items: CodexCatalogRefreshItem[]
}

/** 刷新接口的返回：逐账号结果，加刷新后重新读取的分组状态（与读取接口同结构，可直接替换）。 */
export interface CodexThirdPartyRefreshResult {
  refresh: CodexCatalogRefreshReport
  state: CodexThirdPartyGroupState
}

/** 一份内置能力档案的摘要：下拉里列出各档案 ID 及其已认得的型号。 */
export interface CodexProfileSummary {
  id: string
  upstream_models: string[]
  protocols: string[]
}

/** 一条型号别名：把上游型号关联到已有档案。valid 为 false 表示档案已被删除或改名，不参与匹配。 */
export interface CodexProfileAlias {
  upstream_model: string
  /** 规范化后的键（别名表主键）。 */
  key: string
  profile_id: string
  valid: boolean
  updated_by: string
  updated_at: string
}

export interface CodexProfileAliasList {
  revision: number
  aliases: CodexProfileAlias[]
  profiles: CodexProfileSummary[]
}

const base = (groupId: number) => `/admin/codex-third-party-models/${groupId}`

/** 读取分组的展示配置、候选评估结果和账号名称；只查数据库，不发上游请求，也不触发源刷新。 */
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

/**
 * 同步刷新分组页面判定依赖的上游目录（按模型分流账号的上游模型目录、官方账号基础目录），
 * 返回逐账号结果和刷新后的分组状态。服务端单次最长约 20 秒，同一分组 10 秒内重复调用复用上次结果。
 */
export async function refreshCatalogs(groupId: number): Promise<CodexThirdPartyRefreshResult> {
  const { data } = await apiClient.post<CodexThirdPartyRefreshResult>(`${base(groupId)}/refresh`, undefined, {
    timeout: 45000
  })
  return data
}

const aliasPath = '/admin/codex-third-party-profile-aliases'

/** 列出全部型号别名（含失效项）与可供关联的内置能力档案。 */
export async function listProfileAliases(): Promise<CodexProfileAliasList> {
  const { data } = await apiClient.get<CodexProfileAliasList>(aliasPath)
  return data
}

/** 新增或更新一条关联（幂等）：上游型号必须非空、档案必须存在，已被内置档案直接认出的型号会被拒绝。 */
export async function setProfileAlias(upstreamModel: string, profileId: string): Promise<CodexProfileAliasList> {
  const { data } = await apiClient.put<CodexProfileAliasList>(aliasPath, {
    upstream_model: upstreamModel,
    profile_id: profileId
  })
  return data
}

/** 解除关联（幂等）。 */
export async function deleteProfileAlias(upstreamModel: string): Promise<CodexProfileAliasList> {
  const { data } = await apiClient.delete<CodexProfileAliasList>(aliasPath, { params: { upstream_model: upstreamModel } })
  return data
}

export const codexThirdPartyModelsAPI = {
  getGroupState,
  previewChanges,
  saveChanges,
  markVerified,
  refreshCatalogs,
  listProfileAliases,
  setProfileAlias,
  deleteProfileAlias
}

export default codexThirdPartyModelsAPI
