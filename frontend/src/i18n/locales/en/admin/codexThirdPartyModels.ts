export default {
  codexThirdPartyModels: {
    // Tab label on the Plus enhancements page
    tabLabel: 'Codex third-party models',
    notice: '"Enabled" only decides whether the model is published to the Codex model picker with the right configuration. Disabled does not mean calls are blocked: call permissions are still decided by the key, group allowlist, routing and account rules. Third-party models are off by default.',
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
      refresh: 'Refresh',
      syncSources: 'Sync candidates',
      syncing: 'Syncing…',
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
      overLimit: 'Over limit',
      withinLimit: 'Within limit',
      nativeCount: 'Native GPT models',
      enabledCount: 'Enabled third-party models',
      worstCase: 'Worst-case catalog bytes',
      remaining: 'Remaining space',
      limit: 'Client catalog limit: {limit}'
    },
    sources: {
      title: 'Account model source sync status',
      empty: 'This group has no usable accounts.',
      modelCount: '{count} models',
      syncedAt: 'Synced at {time}',
      stale: 'Stale, waiting for refresh',
      configChanged: 'Account configuration changed, sync again',
      status: {
        missing: 'Not synced',
        complete: 'Synced',
        empty: 'Upstream returned an empty list',
        failed: 'Sync failed',
        refreshing: 'Refreshing',
        official_skipped: 'Official account, no sync needed'
      }
    },
    filters: {
      searchPlaceholder: 'Search model name, upstream model or platform',
      status: 'Status filter',
      all: 'All statuses',
      enabled: 'Enabled',
      disabled: 'Not enabled'
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
      empty: 'No matching models. Click "Sync candidates" to read the latest model list from the account upstreams.',
      enabledNotPublished: 'Enabled, but not published right now',
      cannotEnableFallback: 'Does not meet the conditions to be enabled',
      blockedFallback: 'Enabled, but currently blocked and not in the catalog'
    },
    target: {
      native: 'Native GPT (from the base catalog)',
      accounts: 'Accounts: ',
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
    sync: {
      started: 'Background sync started; results refresh automatically when it finishes',
      failed: 'Failed to sync candidates'
    },
    reject: {
      title: 'Not all selected models can be enabled; nothing was saved',
      notCandidate: 'Not in this group candidate list',
      native: 'Native GPT is not controlled by this switch',
      overLimit: 'Worst-case catalog size after enabling is {worst}, over the {limit} limit'
    }
  }
}
