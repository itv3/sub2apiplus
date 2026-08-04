# 变更集 6 DMIT 实机热修报告

日期：2026-08-04

## 问题与修复

变更集 6 首次部署到 DMIT 测试服务后，真实 WebSocket 请求触发
`request_modified_after_finalize`。根因是 `github.com/coder/websocket` 在进入 HTTP
RoundTripper 前会把 `wss` 规范化为 `https`，而最终线 Guard 的请求摘要此前把二者视为不同
协议，因而将传输库内部的等价规范化误判为请求篡改。

- 本地修复提交：`6198075cd47495d7e623a0a27a3a1e9c9407ac30`；
- 修复后摘要按传输语义规范化协议：WebSocket 路径的 `wss`／`https` 等价，`ws`／`http`
  等价，同时继续区分安全与非安全传输；
- 回归测试覆盖最终线协议别名、Linux/amd64 Lite 模型 WebSocket 失败后回退 HTTP、
  response-lite 与 zstd 事实，以及非等价协议仍然 fail-close。

## DMIT 构建与真实请求验证

- 镜像：`sub2apiplus:changeset6-wsfix-6198075cd`；
- 镜像摘要：`sha256:247dc43b759014376467d4aa406cbbb5ae2dac0bdd7ea4f47335a36068af8fd4`；
- 镜像内版本：`0.1.169-1-cs6-wsfix`；
- OCI revision 与本地修复提交完全一致；
- 使用 DMIT 测试服务已有 OAuth 账号和 API Key 连续执行两次 `gpt-5.6-luna`
  `/v1/responses` 流式请求，均返回 HTTP 200，均收到 `response.completed` 和预期标记；
- `/v1/models` 返回 HTTP 200 并包含 `gpt-5.6-luna`；
- 连续请求后的日志中未再出现 `request_modified_after_finalize`；
- 服务、PostgreSQL、Redis 与 keeper 均保持运行，应用健康检查通过。

验证过程未记录、打印或写入任何账号密钥。

## 回归与清理

- 本地完整 Go 回归、前端回归、capture tools、聚焦 race 与差异格式检查均通过；
- DMIT 构建缓存和临时构建目录已清理；
- DMIT 磁盘占用恢复到 57%，可用空间约 8.3 GB；
- 未向 GitHub 或其他远端推送任何提交。

结论：修复后的变更集 6 已在 DMIT 测试服务通过真实账号连续请求验证，可以正常使用。
