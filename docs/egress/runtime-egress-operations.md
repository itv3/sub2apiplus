# 指定出口运行时保护与迁移

本说明对应 R15。策略、密钥、网络状态与守护运行包独立于 Codex 工具认证；文档和代码存在不代表生产已经安装。
生产安装、容器重建和验收事实须登记在本轮发布记录中。本轮用户只授权 `sub2apiplus` 与 `capture-cli` 使用 DMIT
公网 `69.63.195.102`，故障测试不切换生产出口。

## 一、策略与状态

- 两端使用相同的 `/etc/sub2api-egress/policy.json`，root:root、0600；父目录由 root 管理，不允许其他用户写入。
  内容按 `codex_runtime_egress_policy.schema.json` 和运行时跨字段规则校验。授权人、时间、原因、revision 必须完整。
- 两端私钥分别存放在 root 专有文件；策略只含公钥。禁止将私钥、Docker 环境凭据或抓包账号写入日志、Git 或收据。
- 为业务新建专用 WireGuard 接口及 /30 地址。不得接管既有 `wg1` 或影响监控 Peer。接口、公钥、端点、MTU、DNS、
  公网出口和内网依赖均从策略读取；端点 IP 与最终公网 SNAT IP 可以不同。
- 守护发布 `/run/sub2api-egress/status.json`，包含策略摘要、boot ID、内核单调时钟租期、共享检查、逐容器绑定和探针。
  安装记录与追加事件分别为 `/var/lib/sub2api-egress/installed.json`、`events.jsonl`。完整策略事实进入 v8 环境收据。
- 默认每 0.5 秒核验共享路径并更新最多 3 秒的内核租期；每 15 秒刷新三个 HTTPS 探针，至少两个成功且无冲突。
  守护失联的业务闭锁上界为租期；升级读状态轮询最多再增加 0.5 秒。命令清理沿用原 cleanup grace，单独记录实际时长。
- BPF 探针目的表没有元素超时，守护每轮按内核中的实际键与当前解析结果对账：删除旧地址、写入当前地址，共享保护失效时清空。
  探针域名 IP 轮换不会累积到表上限；守护重启后同样以内核实际键为准。
- 环境收据的实时准入与事实采集共用同一校验时刻，重放按原时刻复算状态年龄与观测时效。Rust TLS 探针使用采集参数给出的
  本轮目标版本 `/opt/codex-<目标版本>/bin/codex`；新建或承接 Campaign 的 P0 必须由当前 producer 采集且探针版本等于目标版本。

## 二、两端保护与信任边界

源宿主先安装 cgroup egress BPF，再让受保护容器在专用 systemd slice 下启动。每个放行键同时绑定 cgroup ID、
容器网卡索引和源 IPv4；新容器、新网卡及 IPv6 业务默认不放行。bridge 层按真实 host ifindex 与源地址重新分类，
不依赖跨 veth 会被清除的 skb mark；inet 晚期检查确保实际转发路径和源 NAT 合规。两端 WireGuard 外层 UDP 也受
端点、物理接口和端口约束。出口端只允许该隧道业务来源经指定物理接口转发，并在 SNAT 后核对允许公网源地址。

内网按容器、共同网络地址、协议和端口逐项授权，不放行整个私网或 mihomo。已登记的正常入站服务回复单独处理；
无关容器只按核验过的 host ifindex 登记 bypass。守护退出不删除任何保护，租期到期时新旧业务连接都闭锁。

本轮 `capture-cli` 是 privileged 且挂载 Docker socket，属于可信 root 运维边界。本方案处理错误配置、出口漂移、
重建和守护故障，不宣称能抵御已取得宿主 root 权限的恶意程序。

## 三、首次迁移顺序

1. 在隔离 staging 完成策略、历史兼容、监督器暂停、真实内核和守护故障链。保存两端原 compose、网络规则和服务状态。
   维护窗口内先停止升级派发，确认无采集；定向停止两个受保护业务容器，再安装新保护。
2. 根据实际必要依赖填策略。当前应用需要 postgres TCP 5432、redis TCP 6379；抓包场景需要应用访问 capture-cli 的
   TCP 18443（候选 relay）与 TCP 18080（MITM）。capture-cli 需要访问应用 TCP 8080。其他依赖逐项核验后登记，
   不因某次测试失败临时扩大成私网通配。抓包容器本地的官方 relay 443、MITM 内部端口走容器 loopback。
3. 两端准备独立密钥、相同策略、WireGuard、nft；源端另需 clang、gcc、libbpf-dev 和 cgroup v2。先安装出口端，
   再安装源端。入口如下，`--key` 传本端密钥路径，命令不输出密钥：

   ```bash
   python3 tools/arm64_supervised_deploy.py egress-install --role exit
   python3 tools/arm64_supervised_deploy.py egress-install --role origin
   ```

   安装器将最小 Python 运行闭包保存为 `/var/lib/sub2api-egress/bundles/<摘要>/`，systemd 只引用该持久目录。
   bootstrap 先于 Docker 和专用 WireGuard 启动；规则原子替换时动态放行集为空，私钥配置也原子写入。
4. 用 `egress-compose` 生成每个服务的可审核覆盖文件和直接 DNS 文件，例如：

   ```bash
   python3 tools/arm64_supervised_deploy.py egress-compose \
     --container sub2apiplus --compose-service sub2api \
     --output-dir /etc/sub2api-egress/compose-r1-app
   python3 tools/arm64_supervised_deploy.py egress-compose \
     --container capture-cli --compose-service capture-cli \
     --output-dir /etc/sub2api-egress/compose-r1-capture
   ```

   该入口只读取 Docker 清单并生成文件，不重建容器。将审核过的 cgroup_parent、DNS、resolv.conf 挂载和依赖
   extra_hosts 合入实际 compose；候选切换、live attestation、回滚使用的备份必须继承这些字段。不得只读挂载
   `/etc/hosts`，抓包工具仍需临时修改并恢复它。DNS 不得继续依赖 Docker embedded DNS 转发到宿主出口。
5. 新保护已默认闭锁且两个业务容器停止后，才定向停用旧 `sub2api-wg1-killswitch.service` 并删除它拥有的旧表；
   旧 wg1/其他 Peer 和非本业务路由不删除。旧表会拒绝新专用接口，不能与新业务放行长期并用，也不能先卸旧保护再安装。
6. 定向重建两个业务容器。守护确认全部网卡、cgroup、DNS、精确依赖和两端路径后，只先发探针租期；两容器各自
   完成独立出口验证才放行业务。probing 期间 BPF 只放行探针目的，数据库、缓存等内网依赖与入站业务同样闭锁，
   应用对依赖的 TCP 连接在准入完成后由重传建立；须实测确认重建后应用不会因依赖短暂不可达而退出、重启循环。
   执行 `egress-check`，再检查 DB、Redis、入站健康、DNS、上游 TLS、抓包和证据写入。
   保存延迟、吞吐与资源水位变化，确认无异常后恢复升级派发。此时不补发任何历史官方请求。
7. 受监督部署预检只按受限运维策略与持续守护的实时状态核验出口，不再比对固定服务商、隧道接口或路由表；部署后核验
   枚举容器内全部 `/opt/codex-<版本>/bin/codex` 逐个执行 `--version`，不写死某一客户端版本。

## 四、策略变更、故障和恢复

采集脚本中已声明的正常 `docker restart` 或单服务 compose 重建统一经过监督器的 `egress-transition` 入口。
该入口只执行这两类本地命令，开始前要求双容器合规；正式运行时还绑定父 run、存活进程及创建时间、命令摘要、策略和
不可覆盖的维护声明。等待最多 60 秒且不延长原截止，只容许指定容器暂时缺失或重新探测，共享保护及另一容器须继续
合规。内核仍阻断未完成准入的业务；维护期间禁止新派发和生成通过的环境收据。两容器通过普通准入后，才能记录成功
并进入下一采集步骤。其他容器故障、错误配置、出口冲突、共享失效或超时仍立即进入不可逆暂停。已暂停后的 cleanup
只允许恢复本地容器配置，不能恢复该 run 的采集许可。所有维护声明、结束事实与维护命令输出日志（0600）保存在父 run 的
`egress-transitions/`，失败原因附带输出尾部。维护入口先安装父监督器清理信号处理，再写不可覆盖的维护声明；拒绝并行维护时
不触碰其他维护进程的声明。

用户明确改选出口时，维护窗口内闭锁业务，更新授权 revision 和两端网络配置，再执行通用安装及实时准入。
不得修改工具中的服务商常量或重签工具发布认证。旧 BWG/DMIT 收据按原 producer 只读重放；出口变化不进入
版本化等价投影，镜像、抓包拓扑和其他真实依赖仍影响结果等价性。

单容器故障撤销该容器的业务租期，保留仅面向登记 HTTPS 来源的探针通路。另一容器须共享保护有效且自身合规才继续。
必要依赖只在依赖容器运行时放行其精确地址。依赖容器受控重建、停止或处于 restarting 期间，本容器只暂缓这一项依赖，
自身出口准入、探针结果和内核租期不变；两个受保护容器互为依赖时，维护入口重建其中一个，另一个仍能保持合规。
共同网络缺失、运行中依赖没有地址或地址为公网等配置错误，仍使本容器不合规并闭锁。
共享故障清空两个容器的可信观测；修好后两者都须重新探测。策略缺失、权限不合规、摘要变更、状态过期或观测冲突都拒绝。

任何出口故障使正式升级 run 写入不可覆盖的 `egress-pause.json`。执行端和独立监督器停止派发、请求清理，且不重复发送
清理信号。进程组绑定 PID 与 Linux 创建时间，避免误杀复用 PID。网络恢复不能让原 run 自动续跑；先按不确定窗口对账
Job 与请求，再按已有恢复规则从合法 checkpoint 启动后继。缺少清理事实或无法证明的请求不能按“当前出口正确”追认。
每个新 Job 记录实际执行时段及父 run 身份。对账、复用与封存均核对原 `egress-pause.json`：窗口之前完成的结果
继续按原依赖判据复用，相交结果归入 `indeterminate` 并补采；修复后的实时状态不会覆盖这一判定。
归档必须连同 Job 引用的父 run、暂停记录和最后可信状态保留，不能只留 attempt 字节而删除其证明。

工具回退也必须保留指定出口保护；旧工具不能读取新策略时保持闭锁并修复，不为兼容旧硬编码切换出口。
隔离测试覆盖范围与实测时限以发布收据为准；未实际验证的宿主重启、故障和性能项不得标记为已保证。
