export default {
  codexThirdPartyModels: {
    // Plus 增强页里的标签名
    tabLabel: 'Codex 三方模型启用',
    notice: '启用只决定能否在 Codex 中选到并带正确配置，不改变调用权限；第三方模型默认全部关闭。',
    clientHintToggle: '客户端配置说明',
    clientHint: '客户端需要在 provider 下配置 model_catalog_url 才会拉取在线目录。目录响应上限 1 MiB、请求须在 5 秒内完成；客户端约每 4 分 30 秒刷新一次，开关变化最迟约 5 分钟在选择器中体现。纯第三方分组全部关闭时目录为空，客户端需要显式配置 model。',
    loading: '正在读取分组配置…',
    loadFailed: '读取 Codex 三方模型配置失败',
    saveSuccess: '已保存',
    saveFailed: '保存失败',
    conflict: '该分组的配置已被其他人修改，已重新加载最新内容，请确认后重试。',
    group: {
      label: '分组',
      placeholder: '请选择分组',
      choosePrompt: '请先选择一个分组，再管理该分组在 Codex 中显示的第三方模型。',
      empty: '没有符合条件的分组。只有 Composite 与走 OpenAI 协议的平台分组才提供 Codex 在线目录。',
      loadFailed: '读取分组列表失败'
    },
    actions: {
      enableSelected: '批量开启（{count}）',
      disableSelected: '批量关闭（{count}）',
      markVerified: '标记已验证',
      confirmVerified: '确认已验证',
      cancel: '取消'
    },
    budget: {
      title: '目录体积预算',
      incomplete: '未完成检查',
      incompleteHint: '预算检查尚未完成，不能确认开启后目录不会超限。',
      chip: '目录 {used} / {limit} · {status}',
      deferred: '开启时检查',
      deferredHint: '页面读取不向官方账号请求目录，目录体积预算在开启（预览与保存）时检查；检查通过前不会开启任何模型。',
      overLimit: '已超限',
      withinLimit: '未超限',
      nativeCount: '原生 GPT 数量',
      enabledCount: '已开启第三方数量',
      worstCase: '最坏完整目录字节数',
      remaining: '剩余空间',
      limit: '客户端目录上限：{limit}'
    },
    filters: {
      searchPlaceholder: '搜索模型名称、上游型号或平台',
      status: '状态筛选',
      all: '全部',
      enabled: '已开启',
      disabled: '未开启',
      blocked: '不可开启'
    },
    columns: {
      selectAll: '选择当前筛选结果中的全部模型',
      selectRow: '选择 {name}',
      model: '模型名称',
      target: '实际目标',
      capabilities: '能力摘要',
      status: '状态',
      toggle: '在 Codex 中显示'
    },
    table: {
      empty: '没有符合条件的模型。',
      enabledNotPublished: '已开启，但当前未发布',
      cannotEnableFallback: '当前不满足开启条件',
      blockedFallback: '已开启，但当前被阻塞，未进入目录'
    },
    target: {
      native: '原生 GPT（沿用基础目录）',
      accounts: '账号：',
      primary: '首选：{account}',
      alternates: '备选：{accounts}',
      unknown: '未确认归属',
      more: '等 {count} 个'
    },
    capabilities: {
      unknown: '能力依据不足',
      input: '输入：{value}',
      reasoning: '推理档位：{value}（默认 {default}）',
      context: '上下文：{value}',
      searchTool: '工具发现',
      tools: '工具：{value}'
    },
    status: {
      native: '原生',
      needs_capability: '待补能力',
      configurable: '配置可用',
      verified: '已验证',
      conflict: '配置冲突',
      over_budget: '目录超限',
      unavailable: '不可用'
    },
    protocol: {
      responses_native: 'Responses 原生',
      responses_via_chat: 'Responses 转 Chat'
    },
    toggle: {
      label: '在 Codex 中显示',
      nativeReadonly: '原生 GPT，只读'
    },
    verify: {
      title: '标记“{name}”已验证',
      hint: '请确认已用该分组的真实 Key 在 Codex 中完成流式回复、工具调用和工具结果续接等验收。验收记录绑定当前路由与能力指纹，指纹变化后自动失效。',
      note: '备注（可选）',
      notePlaceholder: '例如：桌面端流式、apply_patch、图片输入均已验证',
      verifiedAt: '已验证于 {time}',
      outdated: '验收记录已过期，需要重新验证',
      success: '已标记为已验证',
      failed: '标记已验证失败'
    },
    reject: {
      title: '所选模型无法全部开启，整批均未保存',
      notCandidate: '不在当前分组的候选列表中',
      native: '原生 GPT 不受该开关控制',
      overLimit: '开启后目录最坏体积 {worst}，超过上限 {limit}'
    }
  }
}
