# 更换指定出口：运维流程（egress-ctl，两段式）

适用：sub2apiplus 与 capture-cli 的出口要从一个出口换成另一个（换出口机、换出口机的公网地址、换隧道公钥或接口等）。
出口值只存在于 `/etc/sub2api-egress/policy.json`（源端与出口端各一份，内容相同，root:root、0600）。
**换出口 = 改这份策略 + 按本流程重装两端守护**；升级工具（Codex、Claude 两侧）不需要改任何文件、不需要重签或
重建 Campaign，入口步骤也不会因此失效。守护层“出口不是指定值就断网”的逻辑不动。

本目录的 `egress-ctl` 不属于任何受管根，不在冻结台账里，也不被任何门禁扫描。**它不连接任何其他机器**：
同一个脚本在两台机器上各在本机执行一次——**先在出口机执行，再在 ARM64（源端）执行**，两台机器之间不需要任何 ssh 凭据；
出口机执行成功后打印一行审计记录，操作员把它带到 ARM64 当参数，源端在动手之前核对两端装的是同一份策略。

脚本每一段做的事：校验草稿（用本机现行运行包里的 `validate_egress_policy`）→ 对比差异 → 备份与审计 →
从现行运行包内调用既有 `egress-install`（不换运行包）→ 核对通过才算完成 → 失败回滚本机。

## 0. 硬性规则

- **不要新旧两个出口同时放进 `allowed_public_ipv4`**：那是真放宽。宁可短暂闭锁。脚本会拒绝这种草稿。
- **只改策略文件、不重装 = 自动断网**（守护发现策略摘要与安装记录不一致就闭锁），不是放行。所以不要手工改完就走，
  一律用脚本。
- 维护窗口内进行：没有进行中的升级派发、入口、后台验证、VC 链。正在跑的正式 run 遇到出口变化会写不可逆的
  `egress-pause.json`。
- 出口机若是冷备机（例如 BWG），**只装出口守护，不要启动 sub2apiplus 容器**。
- 未经老板明确同意，不在生产库、生产机上做任何事；本流程只作用于 ARM64（源端）与老板指定的出口机。
- 授权人必须是老板本人（或老板明确授权的人）；草稿里的 `authorization.actor` 必须等于 `--authorized-by`。
- 两台机器用**同一份草稿**（同一个文件内容）。出口端 apply 成功后源端核对摘要，不一致就拒绝，所以不要改草稿再拿去另一台。

## 1. 前置检查（只读）

- 出口机：有 `python3`、`wireguard-tools`、`nftables`；入站放行源端到出口机的 UDP 隧道端口；若出口机上有 Docker，按上次经验补
  FORWARD 放行（工具不管这两条前置条件）；出口机上有独立私钥 `/etc/sub2api-egress/private.key`（root:root、0600），公钥写进新策略的
  `nodes.exit.public_key`。
- 本脚本在每台机器上都要有：从仓库 `deploy/egress-ops/egress-ctl` 拷过去即可（单文件，Python 3 标准库）。
- **全新出口机**（还没有守护，没有运行包）：把 ARM64 现行运行包目录原样拷到出口机任意位置（例如
  `scp -r` 或 `tar` 传输 `/var/lib/sub2api-egress/bundles/<摘要>/`），出口机上的命令在子命令之前加全局选项 `--bundle-dir <拷过去的目录>`（validate、dry-run 同样）；
  脚本会复核该目录里每个文件的摘要与目录身份，装好后安装器会在出口机自己的 `/var/lib/sub2api-egress/bundles/` 里落一份字节相同的运行包。
  已有守护的机器不要加这个参数（守护单元里的现行运行包优先）。
- ARM64 与出口机上各执行 `python3 egress-ctl audit-verify`（首次无日志会报读不到，属正常）。

## 2. 起草新策略

复制现行策略，只改下面这些，其余（服务、依赖、探针、租期）原样：

- `allowed_public_ipv4`：新出口最终的公网 SNAT 地址（单个）；
- `nodes.exit.{endpoint, public_key, public_interface, mtu}`（出口机变了才改）；
- `authorization.{actor, authorized_at_utc, reason}`；
- `revision`（必须大于现行）、`policy_id`（按需）。

把同一份 `draft.json` 放到两台机器上。

## 3. 校验与预览（两台机器各做一遍，不改任何状态）

```bash
# 出口机
python3 egress-ctl validate --draft draft.json
python3 egress-ctl apply --role exit --dry-run --draft draft.json --authorized-by <授权人> --reason "<原因>"
# ARM64（源端）
python3 egress-ctl apply --role origin --dry-run --draft draft.json --authorized-by <授权人> --reason "<原因>"
```

`--dry-run` 打印新旧差异、本机执行计划和当前保护状态，不写策略、不写审计、不装任何东西。核对差异里只有预期的字段变化。

## 4. 执行（先出口机，再源端）

**4.1 出口机**（root，维护窗口内）：

```bash
python3 egress-ctl [--bundle-dir <拷来的运行包目录>] apply --role exit --draft draft.json \
    --authorized-by <授权人> --reason "<原因>" --confirm-maintenance-window
```

依次：备份本机旧策略到 `/var/lib/sub2api-egress/policy-backups/` → 追加审计 `begin` → 写入新策略 →
`egress-install --role exit`（从现行运行包内执行）→ 核对守护 active 且安装记录里的策略摘要等于新策略 → 追加审计 `result`。
成功后打印两行，**把第一行整行复制下来**（审计记录，带自摘要；只想带摘要也可以只带第二行的值）：

```
EGRESS_CTL_AUDIT_RECORD {...}
new_policy_sha256=<64 位十六进制>
```

出口端失败时脚本已把本机回滚并再核对，且**不会打印**这一行，此时不要进行 4.2。

**4.2 ARM64 源端**（root，维护窗口内）：

```bash
python3 egress-ctl apply --role origin --draft draft.json --authorized-by <授权人> --reason "<原因>" \
    --confirm-maintenance-window --exit-audit-record '<出口机打印的那一整行>'
# 或：--exit-policy-sha256 <new_policy_sha256 的值>
```

脚本**先于任何改动**核对：记录自摘要正确、是出口端 `result`/`ok` 记录、`new_policy_sha256` 等于本地草稿经运行包算出的摘要、revision 一致；
不一致直接拒绝（退出码 1，没有任何改动，没有审计）。通过后：备份本机旧策略与 `installed.json` → 审计 `begin`（记下出口端记录的摘要） →
写入新策略 → `egress-install --role origin` → 轮询 `egress-check`（默认最长 120 秒）→ 审计 `result`。

退出码：0 成功；1 参数或校验错误（未改任何状态）；2 失败但本机已回滚并核对通过；3 失败且回滚后仍未通过。

## 5. 失败与回滚

- 每台机器的失败只回滚**本机**：旧策略写回、重装、再核对，并把每步结果写进本机审计 `result.rollback`。返回 3 时守护保持自动闭锁
  （受保护容器上不去外网，不会泄漏），人工按审计里的失败步骤排查。全新机器没有旧策略时无法回滚（审计会写明），须人工处理。
- 源端失败而出口端已装新策略：出口端不受影响，仍是新策略。要整体回退，**先在出口机**、**再在 ARM64** 各执行：

  ```bash
  python3 egress-ctl rollback --role exit|origin --backup /var/lib/sub2api-egress/policy-backups/<该机的备份文件> \
      --authorized-by <授权人> --reason "<原因>" --confirm-maintenance-window
  ```

  备份文件名里带角色与旧策略摘要前缀（`policy-<时间>-<角色>-<摘要前8位>.json`），用本机 `apply` 当时打印的“旧策略已备份：…”那条。

## 6. 验收与观察（脚本之外，人工做）

1. 容器内用三个探针站（如 ifconfig.me、ipify、icanhazip）核对出口地址等于新策略；DB、Redis、入站健康、上游 TLS 复核。
2. 故障用例复测：停源端守护、停出口端守护、重启 capture-cli，闭锁应在租期内、恢复应很快；稳定观察至少 15 分钟，
   看 `/var/lib/sub2api-egress/events.jsonl` 的状态翻转次数（换了链路，丢包与延迟特性可能变）。
3. 旧出口机的守护保留到新链验收完再处理；ARM64 上旧 `wg1` 的 rule 是否一并处理由老板定。
4. 观察合格后才恢复升级派发。

## 7. 审计

每台机器各有一份 `/var/lib/sub2api-egress/audit.jsonl`（root、0600，只追加）。每条带序号、前一条的 `entry_sha256`、UTC 时间，
记录：事件（`begin`／`result`）、种类（`apply`／`rollback`）、本机角色、operation_id、授权人、原因、操作者与主机、旧新 `policy_sha256` 与 revision、
旧新允许出口、运行包 ID、安装与核对结果、回滚结果；源端另记出口端交来的摘要与出口端审计记录的 `entry_sha256`，所以两台机器的审计可以互相对账。
`audit-verify` 检查哈希链；**每个 `begin` 都应有同 operation_id 的 `result`**，缺失说明脚本中途被打断，须人工核对本机状态。
哈希链只能发现中间篡改或删头，发现不了丢掉结尾，所以要看配对。

## 8. 与升级工具的关系（给调度者与升级操作员）

- 入口编排器不再把 `policy.json` 当 `environment-p0` 的输入：换策略后入口步骤判定一律不变。
- 保护不变弱：每次入口都执行的 `entry-preflight`（`guard.sh pre-plan` 里的 `egress-check`）和环境收据采集（collect 前后各一次实时准入）
  仍然实时核对“守护在位、出口等于策略”，不通过就停止派发。
- 进行中的 run 在出口变化时仍会被监督器暂停（设计如此，所以要在两次运行之间换）。
