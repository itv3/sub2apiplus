# 变更集 3：全 Codex 端点统一 Executor 与 Forward 收敛

验收状态：`ready_for_audit`

修复授权：`approved_for_remediation_with_constraints`

## 3.0 冻结规则

本目录中的 3.0 产物是变更集 3 修改生产代码前取得的静态审核快照，不得由测试、扫描器或生成命令自动覆盖。任何更新都必须经过显式审核，并说明旧摘要、新摘要和原因。

冻结基线来自分支 `main`、HEAD `38a9929eac35a39c86de2f27de8f7a805d7dae52` 上的已验收脏工作区。后续禁止使用 `git clean`、`git reset` 或 checkout 覆盖这份工作区。

文件说明：

- `workspace-status.txt`：可读的完整 `git status --porcelain=v1` 投影；NUL 原文摘要记录在 `workspace-baseline.json`。
- `untracked-files.sha256`：基线时全部 182 个未跟踪文件的相对路径和逐文件 SHA-256。
- `codex-binding-freeze.json`：27 个 `codex-cli` binding、route、候选和有效状态的冻结投影。
- `acceptance-matrix.json`：4 个不可回归锚点、19 个待迁移 sink、3 个 facade 证据和 OAuth exchange 例外。
- `workspace-baseline.json`：工作区、账本、Executor、adapter 与门禁文件的汇总摘要。

OAuth exchange 永久保持 `transport_only`；三个 facade 只有在候选真实消失时才能签发 RemovalReceipt，否则必须合法重分类为 transport infrastructure。

## 实施后审核工件

- `acceptance-amendment.json`：保持原始 `19/23` 历史事实不变，并绑定原矩阵、binding freeze、Catalog Amendment 及两组 exclusion 完整证据，推导有效目标 `17/21`。
- `pre_identity_authority_refactor_reference/`：身份权威重构前双定型实现的 28 route × active/previous 确定性参考，不声称代表变更集 3 开发前 legacy wire。
- `post_identity_authority_refactor_final_wire/`：身份权威重构后从 production semantic bridge 现场重建的 56 份 adapter-terminal final-wire、统一严格比较、4 个既有锚点比较及 secret-scan receipt；对重构前参考的证明范围显式受限。
- `migration-receipts.json`：17 个真实 Runtime Sink 的独立 canary→enforced 收据链；既有变更集 1B/1C 收据保持原文不变。
- `migration-artifacts/changeset3/`：本地确定性 Executor 重放、wire 脱敏记录和 canary acceptance；明确标记未发送外部流量。
- `removal-receipts.json`：15 条已真实消失的旧业务／legacy delegation 候选，由对应 enforced MigrationReceipt 接管。
- `facade-audit.json`：三个仍真实存在的 facade 保留为 transport infrastructure，不签虚假 RemovalReceipt。
- `pairing-gate-evaluation.json`：825 行旧 pairing 门禁的删除条件尚未全部成立，因此继续保留。
- `bootstrap-inventory-lock.json`：保持 bootstrap inventory 原文不变，重新锁定支持多变更集收据和 Executor route 证明的扫描算法。
- `acceptance-report.md`：变更集 3 最终边界、强制修订逐项结论和全量验证命令；表示已完成开发自审，等待正式审核。

最终运行边界采用用户确认后的真实口径：21 个 `codex_profile` Runtime Sink 为 `enforced`；`codex.admin_test.chat_completions` 和 `codex.admin_test.keeper` 是 API Key 专用 scope exclusion；`codex.oauth.exchange` 是唯一 `transport_only` Runtime 例外。
