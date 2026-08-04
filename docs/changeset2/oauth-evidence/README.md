# OAuth 0.145.0 应用层 wire 证据

本目录保存从 Vircs 隔离抓包镜像取回的脱敏 OAuth 应用层证据。采集运行使用官方
`codex-cli 0.145.0` 二进制，网络模式为 `none`，所有请求由本地合成服务 fail-close
处理。fixture 在写盘前已清除授权码、PKCE verifier 和 refresh token。

本证据足以修正现有 `oauth_refresh` 的 Header、JSON Body 与字段顺序；它没有覆盖
`alpn-capability` 能力矩阵和完整连接生命周期场景，因此 authorization-code exchange
继续保持 `transport_only`，不创建正式 EndpointID，也不改变其 enforcement 状态。

来源运行：`Vircs:/root/oauth-capture/runs/official-oauth-appwire-20260803-1785721508040`

远端脱敏原件摘要：exchange `46f3d9f8…`、refresh `14827bb5…`、verification
`f35ff903…`。仓库 fixture 统一为 LF 且 verification 收窄为 CS2 所需投影，当前文件
摘要由同目录 `SHA256SUMS` 复算。
