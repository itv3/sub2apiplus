# 变更集 5 复审验收报告

## 1. 当前结论

- 当前状态：`ready_for_re_review`。
- 上轮状态 `changes_requested` 的两个 P1 和一个 P2 已完成修复。
- 本变更集未同步远端上游；分类基线仍为 `26d894ef4f50645a4bf1030e378ac892f17d0223`。
- 远端观察值 `825ca7b1fc9335f904bc077f051de815fb61e47f` 不参与 diff、分类或验收；本地旧跟踪引用 `12d811bd7` 未作为远端事实使用。
- 验证过程未访问真实外部服务，也未使用真实凭据。

## 2. final-wire 三段时间链

### 2.1 original pre

`pre-refactor-final-wire/` 是变更集 5 开发前的不可变时间锚点，已恢复为字节级原值：

- manifest：`959b3179c0a54ffa81fa58057e43994e237f1f8565de0f28788a01f18e2d316d`；
- secret-scan：`6446d2ce06d745dd1240a513b77d261cca89a719ab9e6e34164891fce60e7488`；
- receipt：`3cb84244a4a5b0a1d056bdff5616eea8bcc49c668f6c3039722c4c8e187edabd`。

原始文件不再由当前生成器覆盖。

### 2.2 normalized pre

`normalized-pre-refactor-final-wire/` 位于独立目录：

- manifest：`51501f4c140a417d81c3d1a8f8525be4ef2f77c11248ffa63ca8e50150d011f7`；
- secret-scan：`281bcfae80abb2b60156a71fa8d3d3fe9667f52196762a0ba15794bf1c82e438`；
- receipt：`90e6ddfdaa9d200ec0a51523b4ff6cec8d56b6c67fd5ddb2d51d9b10382c8491`。

original → normalized 只允许两个精确 capture key 的 `/connection_pool_digest` 变化。两条 delta 均绑定 before/after SHA-256；错误 capture、错误路径、错误 before/after、缺少一条及增加第三条均由 mutation 测试拒绝。

normalization transition SHA-256：`c037cc323431ac3180ab98e9e229d3cc1e6e3a34371b8c793dbf3ff25b7c2445`。

### 2.3 post

`post-refactor-final-wire/` 记录当前完成状态：

- manifest：`3b1dfc541086da317c288bc064016acda9003780f907c3d5ae6c1720a7a78fb5`；
- secret-scan：`ee808ca8738c8d04e5d4c5ad3ae09b4e649c32758c8f982f0b65874416f6ffc2`；
- receipt：`9a9c8d86f4b35a33f5f6615e27e21bec25e7860eae5e533fec9333897554c1de`。

normalized → post 对 28 条 route、active/previous 共 56 份 capture 使用 `finalwirecontract.Compare` 和空允许列表逐项比较，差异为 0。两次独立 normalized 重建及一次 post 重建均与冻结文件字节一致。

## 3. workspace baseline 与 transition

### 3.1 workspace baseline

冻结基线保持原文：

- `protected_prior_artifacts`：546 项；
- `incidental_non_authoritative_paths`：2 项；
- `changeset5_prerequisite_artifacts`：10 项；
- 合计：558 项。

`.vite/vitest/results.json` 和 `backend/-h` 只属于 incidental，未升级为权威成果。

### 3.2 workspace transition

当前状态逐项复算 SHA-256、存在状态、普通文件类型和权限：

- transition：50 项；
  - protected：47 项；
  - prerequisite：3 项；
  - incidental：0 项；
- 与基线完全相同：508 项；
- 显式删除：2 项，均满足 `before=regular`、`after=absent`、`deletion_allowed=true`。

transition 外全部冻结路径与基线完全一致；重复登记、虚假登记、漏登记、错误删除许可、权限／类型漂移和符号链接均失败。

- transition manifest：`5e144f2f88b62c27647ab0557a7020a19fb1a54dedb0ef598e13f6c387716698`；
- transition receipt：`e38384f401dcd2cb93f4e900926cb84b606f29685237c8394edb718ffaa87af1`。

manifest 与 receipt 由最终当前状态确定性重建，并由 Go 源码独立锚定。

## 4. raw conflict inventory 与 effective conflict inventory

### 4.1 raw conflict inventory

原始 pre/post full 和 governable inventory 保持不可变：

- full：`260 → 247`；
- raw governable：`103 → 90`。

### 4.2 effective conflict inventory

`buildOpenAICodexImagesRequestBody` 的原始 `non_official` 漏判通过结构化 overlay 修订为 `official_egress_exclusive`：

- overlay 绑定完整 unit key、原始 pre/post inventory SHA-256、签名和前后 AST SHA-256；
- effective governable 统一由 `raw governable + amendment` 计算；
- effective governable：`104 → 91`；
- 迁出单元仍为 13，未误算为 14；
- `amendedUnit` 参数及按名称跳过逻辑已删除；
- 任意其他 `non_official` 单元变化仍由 mutation 测试拒绝。

## 5. P2 独立证据锁

- `0145-symbol-allowlist.json` 固定文件 SHA-256、schema、changeset、精确两个名称、完整理由及排序集合指纹。
- `egress-surface-inventory.json` 固定文件 SHA-256、52 个 surface、2 个排除项、排除理由及排序集合指纹。
- 清单与模拟扫描结果同时增加一项、等量替换或修改排除理由均不能绕过独立锁。

## 6. 其余核心验收结果

- 21 个 Runtime Sink 精确集合保持 `enforced`，全部进入统一 Executor。
- 旧 attach/finalizer/helper 的生产定义和调用均为 0。
- 52 个完整出站面与 36 个冲突文件继续使用独立门禁。
- 136 个被修改的上游 declaration 集合前后指纹完全一致；新增、删除或重命名为 0。
- `internal/officialegress/...` 直接及传递依赖均未进入 service、repository 或 handler。
- `wire_gen.go` 与 Dockerfile 冲突单元未扩大，生成结果可确定性重建。

## 7. 验证结果

以下命令均通过：

```text
make check-egress-spec
go test ./... -count=1
go test -race ./internal/officialegress/... -count=1
go test -race ./internal/service（变更集 5、Runtime Sink、WS Guard 聚焦集合）
go test -race ./internal/repository（官方出站、OAuth、ReqProfile 聚焦集合）
go vet ./...
go build ./...
make test-frontend test-capture-tools
pnpm --dir frontend run test:run
git diff --check
```

- 完整 Vitest：199 个测试文件、1370 项测试全部通过。
- capture tools：326 项通过、3 项跳过。
- 前端输出只有 Node localStorage 参数、Browserslist 数据、Vue 测试组件和 jsdom 模拟网络等非失败警告。

## 8. 证据索引

- original pre：`docs/changeset5/pre-refactor-final-wire/`；
- normalized pre：`docs/changeset5/normalized-pre-refactor-final-wire/`；
- post：`docs/changeset5/post-refactor-final-wire/`；
- normalization transition：`docs/changeset5/final-wire-normalization-transition.json`；
- workspace baseline：`docs/changeset5/workspace-baseline/`；
- workspace transition：`docs/changeset5/workspace-transition/`；
- raw conflict inventory：`docs/changeset5/conflict-inventory/`、`post-refactor-conflict-inventory/`；
- effective conflict inventory：`docs/changeset5/conflict-classification-amendments.json`；
- 冲突迁移收据：`docs/changeset5/conflict-migration-receipt.json`。
