# 官方出站机器证据

本目录保存 `docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md` 当前实现使用的机器基线、收据和可复算
证据。它只在本地门禁、CI、版本升级、上游合并、回归和审计时使用，不是服务运行时配置，也
不复制进生产镜像。

`docs/CODEX_CLI_CLIENT_EMULATION_GUIDE.md` 是客户端仿真与版本演进唯一的人类可读规范；
`maintenance/` 不再保存单次升级计划、缺陷报告、评审稿或操作笔记。证据目录内保留的其他
Markdown 仅承担目录导航、当前 route 分类或机器证据来源说明，不得形成第二套规范。

| 目录 | 当前责任 |
|---|---|
| `foundation/` | 发送面基线、统计和 persona 分类 |
| `lifecycle/` | bootstrap、legacy 边界、迁移与删除收据 |
| `browser/` | 浏览器身份的脱敏抓包 |
| `guard/` | Guard 演练和验证记录 |
| `release/` | ReleaseBundle 兼容锁与 OAuth 证据 |
| `migration/` | Runtime Sink 迁移收据与 final-wire 基线 |
| `source-freeze/` | 不可变源码基线收据 |
| `consolidation/` | Executor 收口、冲突清单和 final-wire 对照 |
| `validation/` | 性能、冲突和最终 wire 验收资产 |
| `maintenance/` | 当前上游 overlay、工作区 transition、机器验收回执和退休收据 |

封存 JSON 中的 `docs/changeset*` 或 `docs/maintenance` 字段记录证据生成时的历史路径，属于摘要
原文，不表示这些旧目录仍存在。当前路径映射由
`maintenance/evidence-directory-consolidation-source-transition.json` 和工作区 transition 固定。
已封存资产不得原地改写；后续清理和路径变化通过新的 maintenance 收据追加。

完整校验入口：

```bash
make check-egress-spec
```
