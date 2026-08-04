# 身份权威重构前参考

本目录冻结变更集 3 身份权威重构前的当前实现观察结果，正式名称为
`pre_identity_authority_refactor_reference`。它不声称代表变更集 3 开发前的
legacy wire，也不改写变更集 1B/1C 的既有收据或 fixture。

范围为 17 个新增迁移 Sink 的 24 条 route，加 4 个既有 enforced 锚点的 4 条
route；每条 route 同时捕获 `active` 与 `previous`，共 56 个 route-mode capture。
捕获只使用合成认证材料、账号、动态 ID、文件和正文，不发送外部流量。

`manifest.json` 由生产 Catalog、Executor、正式 adapter 与本地 terminal Guard
生成，并绑定相关源码、ProfileSpec、ReleaseCatalog、adapter 及捕获工具摘要。
`secret-scan.json` 绑定 manifest 摘要和扫描结果。

参考同时如实记录一项迁移前缺陷：Responses WebSocket 的 compression
normalization 在自动补 offer 后会被当前 terminal Guard 判为
`request_modified_after_finalize`。最终重构必须修复该缺陷；除此以外，post-refactor
比较不得擅自改变已冻结 wire 事实。
