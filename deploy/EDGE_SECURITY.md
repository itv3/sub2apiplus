# 边缘代理与 HTTP 入口安全

Sub2API Plus 支持长时间运行的 SSE 和 WebSocket 请求。入口防护不应设置统一的响应 `WriteTimeout`，否则会提前终止正常的长文本生成和流式连接。

## 应用默认限制

- `server.max_header_bytes: 65536`：将 HTTP/1 请求头限制为 64 KiB，Go 会将其映射为对应的 HTTP/2 header-list 上限。
- `server.read_header_timeout: 10`：限制慢速请求头攻击，不限制请求处理时间和响应流持续时间。
- `server.max_request_body_size: 268435456`：全局 256 MiB 请求体安全上限。
- `gateway.max_body_size: 268435456`：供多模态、Gemini、图像、视频和批量图像端点使用的网关上限。
- `gateway.text_max_body_size: 33554432`：将已知纯文本端点 `/embeddings` 和 `/alpha/search` 限制为 32 MiB。
- H2C 默认每个连接最多 50 个并发流、2 MiB 连接上传窗口和 512 KiB 单流上传窗口。
- 无效凭据滥用防护按可信客户端 IP 统计，IPv6 按 `/64` 聚合：60 秒内允许 120 次失败，超限后封锁 60 秒。该限制按实例生效；多实例的统一防护仍应由负载均衡器、CDN 或 WAF 完成。

不要增加覆盖整个应用的单一请求信号量。一个合法 SSE 请求可能持续数十分钟，长期占用该信号量。连接数和未认证请求应在边缘控制；已认证用户和 API Key 的并发仍由应用负责。

## 可信客户端 IP

Sub2API Plus 默认关闭 `security.trust_forwarded_ip_for_api_key_acl`，升级时也会保留已有选择。关闭时以 Gin 的 `server.trusted_proxies` 链为准；该列表只能填写直接连接 Sub2API Plus 的代理 IP/CIDR，通常是本机 Nginx/Caddy 地址或私有负载均衡子网，显式留空表示不信任任何转发 IP。

开启原始转发头接管后，日志和安全敏感链路会按顺序检查 `security.forwarded_client_ip_headers` 中的自定义请求头，再回退到 `CF-Connecting-IP`、`X-Real-IP` 和 `X-Forwarded-For`。请求头名称不区分大小写，加载时会规范化、去重，并限制为最多 16 个合法 HTTP 字段名；值必须包含 IP 字面量，可使用逗号分隔，非法值会被跳过，并优先选择公网地址。

自定义请求头可通过 YAML 或逗号分隔的 `SECURITY_FORWARDED_CLIENT_IP_HEADERS` 环境变量配置；显式空环境变量会清除 YAML 值。管理后台也可以实时修改，保存后无需重启。每个请求会同时快照开关和请求头列表，避免一次请求混用新旧设置；关闭接管开关后，自定义请求头会被完全忽略。

新安装会在数据库初始化时保存自定义请求头列表，现有安装缺少数据库值时会从 YAML 回填。隐藏的迁移标记可防止后续管理员选择被覆盖。设置读取失败或持久化列表格式错误时，系统会安全回退到可信代理模式并清空自定义请求头；迁移写入失败时，本次进程继续使用已计算的安全模式并记录警告。

原始转发头接管不会验证直接对端，因此所有内置和自定义请求头都可能被直连客户端伪造。仅当防火墙已限制源站只能由 CDN 或负载均衡器访问时才可开启，并要求边缘代理覆盖这些请求头，而不是在客户端值后追加内容。

同机代理示例：

```yaml
server:
  trusted_proxies:
    - 127.0.0.1/32
    - ::1/128
```

## Nginx 基线

在 `http` 块中定义共享限流区。以下数值只是偏保守的起点，应按真实合法流量调整，不能直接视为所有环境的容量目标。

```nginx
limit_conn_zone $binary_remote_addr zone=sub2api_conn:20m;
limit_req_zone  $binary_remote_addr zone=sub2api_auth:20m rate=5r/s;
limit_req_zone  $binary_remote_addr zone=sub2api_api:40m rate=30r/s;
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

server {
    listen 443 ssl http2;
    server_name api.example.com;

    client_header_timeout 10s;
    client_max_body_size 256m;
    large_client_header_buffers 4 16k;
    limit_conn sub2api_conn 40;

    # 登录、注册、2FA、验证码和 OAuth 等真实认证路由。
    location ^~ /api/v1/auth/ {
        limit_req zone=sub2api_auth burst=10 nodelay;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://127.0.0.1:8080;
    }

    location ~ ^/(v1/)?(embeddings|alpha/search)$ {
        client_max_body_size 32m;
        limit_req zone=sub2api_api burst=60 nodelay;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://127.0.0.1:8080;
    }

    location / {
        limit_req zone=sub2api_api burst=60 nodelay;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 1800s;
        proxy_send_timeout 1800s;
        proxy_pass http://127.0.0.1:8080;
    }
}
```

除非 Nginx 的 real-IP 处理已严格限制为明确的可信代理 CIDR，否则不要使用请求传入的 `$http_x_forwarded_for`。

## Caddy 与 CDN

仓库内置的 `deploy/Caddyfile` 设置了 64 KiB 请求头上限、10 秒请求头超时和 256 MiB 请求体上限，并用 TCP 对端地址覆盖转发地址，因此它是“客户端直连 Caddy”的基线。

Caddy 位于 CDN 后方时，不能原样使用 `{remote_host}` 转发配置，否则所有用户都会被归属到 CDN 出口地址，入口拒绝聚合和无效鉴权限流也会把无关用户合并统计。

CDN 部署应先用防火墙限制源站只接受当前 CDN 出口 CIDR，再将这些准确范围配置为 Caddy 可信代理，并从 Caddy 解析后的 `{client_ip}` 生成上游请求头：

```caddyfile
{
	servers {
		trusted_proxies static 192.0.2.0/24 2001:db8:1234::/48
		trusted_proxies_strict
		client_ip_headers CF-Connecting-IP X-Forwarded-For
	}
}

api.example.com {
	reverse_proxy 127.0.0.1:8080 {
		header_up X-Real-IP {client_ip}
		header_up X-Forwarded-For {client_ip}
	}
}
```

示例中的文档网段必须替换为 CDN 官方发布并持续维护的出口网段。只有在源站直连已被阻断、且 Caddy 仅信任这些 TCP 对端时，`CF-Connecting-IP` 才可安全使用。Sub2API Plus 的 `server.trusted_proxies` 还需填写 Caddy 地址或其私有子网，使应用只接受 Caddy 重写后的头。

Caddy 核心不提供通用请求速率限制；应使用可信 CDN/WAF、受支持的限流模块或主机防火墙。

CDN/WAF 应在流量到达源站前执行连接数、请求头/请求体、机器人挑战以及按 IP/ASN 的速率限制。源站入口只允许 CDN 出口 CIDR 或私有负载均衡器，应用端口不能直接暴露到公网。

## DDoS 能力边界

应用层检查只能减少连接进入 Go 服务后的放大效应，无法吸收流量型攻击、TLS 洪泛、带宽耗尽或大规模分布式来源攻击。这些风险需要上游网络容量、CDN/WAF、云厂商防火墙和源站隔离共同处理。拒绝风暴期间还应避免高基数指标或逐请求写入数据库的安全日志。
