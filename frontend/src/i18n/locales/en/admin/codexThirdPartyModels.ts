export default {
  codexThirdPartyModels: {
    // Tab label on the Plus enhancements page
    tabLabel: 'Codex third-party models',
    notice: 'Enabling only decides whether a model can be picked in Codex with the right configuration; it does not change call permissions. Third-party models are all off by default.',
    clientHintToggle: 'Client configuration notes',
    clientHint: 'The client only fetches the online catalog when model_catalog_url is configured on the provider. The catalog response is limited to 1 MiB and must finish within 5 seconds; the client refreshes about every 4.5 minutes, so a switch change shows up in the picker within about 5 minutes. A third-party-only group with everything off returns an empty catalog, so the client must set model explicitly.',
    loading: 'Loading group configuration…',
    loadFailed: 'Failed to load Codex third-party model configuration',
    saveSuccess: 'Saved',
    saveFailed: 'Failed to save',
    conflict: 'This group was changed by someone else. The latest content has been reloaded; please review and try again.',
    group: {
      label: 'Group',
      placeholder: 'Select a group',
      choosePrompt: 'Select a group first, then manage which third-party models it shows in Codex.',
      empty: 'No eligible groups. Only Composite groups and groups on OpenAI-protocol platforms provide the Codex online catalog.',
      loadFailed: 'Failed to load groups'
    },
    actions: {
      enableSelected: 'Enable selected ({count})',
      disableSelected: 'Disable selected ({count})',
      markVerified: 'Mark verified',
      confirmVerified: 'Confirm verified',
      cancel: 'Cancel'
    },
    budget: {
      title: 'Catalog size budget',
      incomplete: 'Check incomplete',
      incompleteHint: 'The budget check has not completed, so it cannot be confirmed that enabling will keep the catalog within the limit.',
      chip: 'Catalog {used} / {limit} · {status}',
      deferred: 'Checked on enable',
      deferredHint: 'The page does not request the official account catalog when loading; the catalog size budget is checked when you enable a model (preview and save), and nothing is enabled before that check passes.',
      overLimit: 'Over limit',
      withinLimit: 'Within limit',
      nativeCount: 'Native GPT models',
      enabledCount: 'Enabled third-party models',
      worstCase: 'Worst-case catalog bytes',
      remaining: 'Remaining space',
      limit: 'Client catalog limit: {limit}'
    },
    filters: {
      searchPlaceholder: 'Search model name, upstream model or platform',
      status: 'Status filter',
      all: 'All',
      enabled: 'Enabled',
      disabled: 'Not enabled',
      blocked: 'Cannot enable'
    },
    columns: {
      selectAll: 'Select all models in the current filter',
      selectRow: 'Select {name}',
      model: 'Model',
      target: 'Actual target',
      capabilities: 'Capabilities',
      status: 'Status',
      toggle: 'Show in Codex'
    },
    table: {
      empty: 'No matching models.',
      enabledNotPublished: 'Enabled, but not published right now',
      cannotEnableFallback: 'Does not meet the conditions to be enabled',
      blockedFallback: 'Enabled, but currently blocked and not in the catalog'
    },
    target: {
      native: 'Native GPT (from the base catalog)',
      accounts: 'Accounts: ',
      primary: 'Primary: {account}',
      alternates: 'Alternates: {accounts}',
      unknown: 'Ownership not confirmed',
      more: '+{count} more'
    },
    capabilities: {
      unknown: 'Insufficient capability data',
      input: 'Input: {value}',
      reasoning: 'Reasoning: {value} (default {default})',
      context: 'Context: {value}',
      searchTool: 'tool search',
      tools: 'Tools: {value}'
    },
    status: {
      native: 'Native',
      needs_capability: 'Needs capability',
      configurable: 'Configurable',
      verified: 'Verified',
      conflict: 'Conflict',
      over_budget: 'Over budget',
      unavailable: 'Unavailable'
    },
    protocol: {
      responses_native: 'Responses native',
      responses_via_chat: 'Responses via Chat'
    },
    toggle: {
      label: 'Show in Codex',
      nativeReadonly: 'Native GPT, read-only'
    },
    verify: {
      title: 'Mark "{name}" as verified',
      hint: 'Confirm that streaming replies, tool calls and tool-result continuation have been verified in Codex with a real key of this group. The record is bound to the current route and capability fingerprint and becomes invalid when the fingerprint changes.',
      note: 'Note (optional)',
      notePlaceholder: 'For example: desktop streaming, apply_patch and image input verified',
      verifiedAt: 'Verified at {time}',
      outdated: 'Verification is outdated, verify again',
      success: 'Marked as verified',
      failed: 'Failed to mark as verified'
    },
    reject: {
      title: 'Not all selected models can be enabled; nothing was saved',
      notCandidate: 'Not in this group candidate list',
      native: 'Native GPT is not controlled by this switch',
      overLimit: 'Worst-case catalog size after enabling is {worst}, over the {limit} limit'
    }
  }
}
