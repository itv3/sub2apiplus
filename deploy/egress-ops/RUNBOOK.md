# 更换指定出口：运维流程（egress-ctl）

适用：sub2apiplus 与 capture-cli 的出口要从一个出口换成另一个（换出口机、换出口机的公网地址、换隧道公钥或接口等）。
出口值只存在于 `/etc/sub2api-egress/policy.json`（源端与出口端各一份，内容相同，root:root、0600）。
**换出口 = 改这份策略 + 按本流程重装两端守护**；升级工具（Codex、Claude 两侧）不需要改任何文件、不需要重签或
重建 Campaign，入口步骤也不会因此失效。守护层“出口不是指定值就断网”的逻辑不动。

本目录的 `egress-ctl` 不属于任何受管根，不在冻结台账里，也不被任何门禁扫描；它只做编排：
校验草稿 → 对比差异 → 备份与审计 → 出口端先、源端后调用既有 `egress-install` → `egress-check` 通过才放行 → 失败回滚。

## 0. 硬性规则

- **不要新旧两个出口同时放进 `allowed_public_ipv4`**：那是真放宽。宁可短暂闭锁。脚本会拒绝这种草稿。
- **只改策略文件、不重装 = 自动断网**（守护发现策略摘要与安装记录不一致就闭锁），不是放行。所以不要手工改完就走，
  一律用脚本。
- 维护窗口内进行：没有进行中的升级派发、入口、后台验证、VC 链。正在跑的正式 run 遇到出口变化会写不可逆的
  `egress-pause.json`。
- 出口机若是冷备机（例如 BWG），**只装出口守护，不要启动 sub2apiplus 容器**。
- 未经老板明确同意，不在生产库、生产机上做任何事；本流程只作用于 ARM64（源端）与老板指定的出口机。
- 授权人必须是老板本人（或老板明确授权的人）；草稿里的 `authorization.actor` 必须等于 `--authorized-by`。

## 1. 前置检查（只读）

在 ARM64 上以 root 执行：

```bash
python3 deploy/egress-ops/egress-ctl audit-verify          # 审计链完整（首次无日志会报读不到，属正常）
systemctl is-active sub2api-egress-guard.service wg-quick@wg-egress.service
# 当前守护状态由第 3 步 apply --dry-run 里的只读 egress-check 一并给出
```

- ARM64 必须能免密 ssh 到出口机的 root（`ssh -o BatchMode=yes root@<出口机> true` 成功）；做不到先配好，脚本不会交互输入密码。
- 出口机需有 `python3`、`wireguard-tools`、`nftables`；入站放行源端到出口机的 UDP 隧道端口；若出口机上有 Docker，
  按上次经验补 FORWARD 放行（工具不管这两条前置条件）；出口机上要有独立私钥 `/etc/sub2api-egress/private.key`
  （root:root、0600），公钥写进新策略的 `nodes.exit.public_key`。

## 2. 起草新策略

复制现行策略，只改下面这些，其余（服务、依赖、探针、租期）原样：

- `allowed_public_ipv4`：新出口最终的公网 SNAT 地址（单个）；
- `nodes.exit.{endpoint, public_key, public_interface, mtu}`（出口机变了才改）；
- `authorization.{actor, authorized_at_utc, reason}`；
- `revision`（必须大于现行）、`policy_id`（按需）。

## 3. 校验与预览（不改任何状态）

```bash
python3 deploy/egress-ops/egress-ctl validate --draft draft.json
python3 deploy/egress-ops/egress-ctl apply --dry-run --draft draft.json \
    --authorized-by <授权人> --reason "<原因>" --exit-ssh root@<出口机>
```

`validate` 用**现行运行包**里的 `validate_egress_policy` 校验；`--dry-run` 另外打印新旧差异、执行计划和当前
`egress-check` 结果，不写策略、不写审计、不装任何东西。核对差异里只有预期的字段变化。

## 4. 执行

```bash
python3 deploy/egress-ops/egress-ctl apply --draft draft.json \
    --authorized-by <授权人> --reason "<原因>" --exit-ssh root@<出口机> --confirm-maintenance-window
```

脚本依次：备份源端旧策略与 `installed.json` 到 `/var/lib/sub2api-egress/policy-backups/` → 追加审计 `begin` →
出口机：备份它的旧策略、必要时把同一运行包原样推过去（内容寻址，运行包 ID 不变）、写入新策略、
`egress-install --role exit` → 源端：写入新策略、`egress-install --role origin`（都从现行运行包内执行，不换运行包）→
轮询 `egress-check`（默认最长 120 秒）并确认出口端守护 active → 追加审计 `result`。

退出码：0 成功；1 参数或校验错误（未改任何状态）；2 失败但已回滚并核对通过；3 失败且回滚后仍未通过。

## 5. 失败与回滚

任一步失败，脚本把旧策略写回两端、仍按“出口端先、源端后”重装，再 `egress-check`，并把每步结果写进审计 `result.rollback`。
返回 3 时守护保持自动闭锁（受保护容器上不去外网，不会泄漏），人工按审计里的失败步骤排查；备份在
`/var/lib/sub2api-egress/policy-backups/`。出口机是全新机器且原本没有策略时，回滚不动它（审计里会写明）。

## 6. 验收与观察（脚本之外，人工做）

1. 容器内用三个探针站（如 ifconfig.me、ipify、icanhazip）核对出口地址等于新策略；DB、Redis、入站健康、上游 TLS 复核。
2. 故障用例复测：停源端守护、停出口端守护、重启 capture-cli，闭锁应在租期内、恢复应很快；稳定观察至少 15 分钟，
   看 `/var/lib/sub2api-egress/events.jsonl` 的状态翻转次数（换了链路，丢包与延迟特性可能变）。
3. 旧出口机的守护保留到新链验收完再处理；ARM64 上旧 `wg1` 的 rule 是否一并处理由老板定。
4. 观察合格后才恢复升级派发。

## 7. 审计

`/var/lib/sub2api-egress/audit.jsonl`（root、0600，只追加）。每条带序号、前一条的 `entry_sha256`、UTC 时间，
记录：事件（`begin`／`result`）、operation_id、授权人、原因、操作者与主机、旧新 `policy_sha256` 与 revision、旧新允许出口、
出口机、运行包 ID、两端安装结果与回滚结果。`audit-verify` 检查哈希链；**每个 `begin` 都应有同 operation_id 的 `result`**，
缺失说明脚本中途被打断，须人工核对两端状态。哈希链只能发现中间篡改或删头，发现不了丢掉结尾，所以要看配对。

## 8. 与升级工具的关系（给调度者与升级操作员）

- 入口编排器不再把 `policy.json` 当 `environment-p0` 的输入：换策略后入口步骤判定一律不变。
- 保护不变弱：每次入口都执行的 `entry-preflight`（`guard.sh pre-plan` 里的 `egress-check`）和环境收据采集（collect 前后各一次实时准入）
  仍然实时核对“守护在位、出口等于策略”，不通过就停止派发。
- 进行中的 run 在出口变化时仍会被监督器暂停（设计如此，所以要在两次运行之间换）。
