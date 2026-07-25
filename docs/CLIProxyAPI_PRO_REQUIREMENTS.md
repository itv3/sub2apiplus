# CLIProxyAPI Pro 需求与开发说明

## 1. 项目定位

CLIProxyAPI Pro 基于两个开源项目开发：

- [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)：负责模型请求、协议转换、账号调度、OAuth、API Key 和统一下游 Key。
- [CPA-Manager-Plus](https://github.com/seakee/CPA-Manager-Plus)：负责管理页面、账号管理、统计、成本、监控和运维。

开发工作区初始使用两个官方仓库作为 Git 子模块；建立自有 fork 后替换子模块地址，并继续保留官方仓库作为上游更新来源。

整体结构：

```text
CLIProxyAPI Pro
= CPA-Manager-Plus 主要改造
+ CLIProxyAPI 少量运行时改造
+ 统一安装、测试和发布
```

[sub2apiplus](https://github.com/itv3/sub2apiplus) 仅作为账号交互、协议探测、连通性测试和官方客户端兼容的参考，不作为运行依赖。

## 2. 项目结构

CLIProxyAPI Pro 对外是一个产品，内部保留两个独立组件：

```text
客户端和管理员
       |
       v
统一域名和反向代理
       |
       +-- 模型请求 --> CLIProxyAPI
       |
       +-- 管理页面 --> CPA-Manager-Plus
                          |
                          +-- Management API --> CLIProxyAPI
```

开发原则：

1. 分别维护 CLIProxyAPI fork 和 CPA-Manager-Plus fork。
2. 使用一个 CLIProxyAPI Pro 主项目锁定两个组件版本。
3. 使用 Docker Compose 一键安装，两个组件保持独立容器和数据目录。
4. 不把两个项目合并成一个 Go 工程、二进制或进程。
5. Pro 功能尽量通过新增模块和少量接入点实现，方便继续合并上游更新。

## 3. 功能需求

### 3.1 统一账号管理

提供一个统一账号页面，集中完成所有平台账号的查看、添加、编辑、测试和用量查看。

账号总览至少显示：

- 平台和认证方式。
- 账号名称或邮箱。
- 启用状态和健康状态。
- 可用模型。
- 配额、Token 用量和官方价格估算成本。
- 最近使用、最近测试和最近错误。

添加账号统一使用以下流程：

```text
选择平台
  -> 选择 OAuth 或 API Key
  -> 填写或完成授权
  -> 探测账号能力
  -> 同步并选择允许模型
  -> 连通性测试
  -> 保存
```

首期支持：Anthropic、OpenAI、Gemini、Antigravity、Grok / xAI。

界面不再把“AI 提供商”和“OAuth 登录”拆成两个添加入口，也不要求用户理解 Codex、Interactions、OpenAI 兼容等内部 Provider 类型。

#### OpenAI API Key 自动探测

添加 OpenAI API Key 时只显示“OpenAI API Key”。系统自动探测：

- OpenAI 原生 Responses。
- OpenAI 兼容 Chat Completions。
- 上游模型列表。

探测后自动创建正确配置；如果同时支持多个协议，则保存全部能力。失败时应区分认证、地址、网络、协议和上游服务错误。

#### 账号模型和用量

OAuth 和 API Key 账号都支持：

- 同步项目最新支持模型。
- 同步上游实际支持模型。
- 模型白名单。
- 模型别名和映射。
- 手工添加模型。

CLIProxyAPI 当前只支持 `excluded_models` 黑名单。`allowed_models` 可以通过 Management API 写入认证文件，但运行时尚不识别其白名单语义；Pro 需要在 CLIProxyAPI 增加运行时过滤逻辑，具体实现方式在技术设计阶段确定。

账号只参与允许模型的调度。账号详情用简单窗口显示配额、重置时间、输入/输出/缓存/推理 Token、请求次数、失败次数和官方价格估算成本。

不引入分组订阅、倍率、余额或商业计费。

### 3.2 API Key 官方客户端兼容

Anthropic 和 OpenAI API Key 账号提供“官方客户端兼容”开关。

开启后，让 Kilo、Cline、Cursor、Roo Code 等非官方客户端的上游请求尽量接近 Claude Desktop 和 Codex Desktop 直接使用该 API Key 时的请求形态。

该功能基于 CLIProxyAPI 现有 cloak 框架扩展。不开启时保持 CLIProxyAPI 原有处理；识别为官方 Claude 或 Codex 客户端时不重复改写。

#### 通用要求

- 使用可版本化的 Claude 和 Codex profile，并按真实官方客户端抓包维护。
- 先执行账号模型映射，再根据最终模型构造兼容请求。
- 测试连接必须复用正式请求的 profile 和构造链。
- 受保护的身份 Header 不允许被账号自定义 Header 覆盖。
- 页面、日志和抓包不得暴露 API Key、OAuth Token 和鉴权 Header。

#### Anthropic API Key

目标请求形态为 Claude Desktop。主要要求：

- `/v1/messages` 和 `/v1/messages/count_tokens` 使用各自独立的构造规则。
- 按 profile 对齐客户端身份 Header、Anthropic Beta 顺序、System、Metadata、Session 和缓存控制。
- 对工具名称和工具结构进行必要归一，保持请求与响应中的工具调用可以正确对应。
- 区分固定身份 Beta 和根据请求内容动态增加的功能 Beta。
- TLS 默认保持 CLIProxyAPI 原有 Transport；只有管理员明确选择 TLS profile 时才启用对应指纹。

#### OpenAI API Key

目标请求形态为 Codex Desktop。主要要求：

- 按 profile 对齐客户端身份 Header、`originator`、`x-codex-*` 和 Turn Metadata。
- 按官方请求形态补齐 Responses 所需的 Body 默认值、缓存键和客户端 Metadata。
- `/v1/responses`、Responses Compact、SSE 和 WebSocket 分别处理，不共用一套模板。
- Compact 保持独立的非流式 JSON 请求形态，不盲目补入普通 Responses 字段。
- HTTP、SSE、WebSocket 和 TLS 的选择以对应 profile 和真实抓包为准。

#### 验证要求

兼容效果必须通过以下方式验证：

- 对比官方客户端和兼容请求的 Header、Body、TLS 和路由。
- 分别验证正式请求和账号连通性测试。
- 同时回归官方客户端和 Kilo、Cline、Cursor、Roo Code 等非官方客户端。
- 以上游实际收到的请求为准，不能只根据本地 Usage 页面判断。

具体请求基线参考 [sub2apiplus README.md 第 1.1 节“API Key 官方客户端兼容”](https://github.com/itv3/sub2apiplus/blob/main/README.md#11-api-key-官方客户端兼容)，开发时不得只复制单个 Header 或常量。

该功能只要求尽量接近已验证的官方请求形态，不复制隐藏 Prompt、产品 Memory 或账号状态，也不承诺 TLS 和 HTTP/2 帧级指纹完全一致。

### 3.3 协议模型列表配置

管理员可以分别自定义每个协议模型列表中显示的模型，包括增加、删除、同步和排序。下表仅为默认值，不按 Provider 硬编码：

| 协议 | 默认模型范围 |
|---|---|
| Anthropic Messages | Claude 模型 |
| OpenAI Responses | OpenAI / Codex Responses 模型 |
| OpenAI Chat Completions | OpenAI 兼容、Grok 等模型 |
| Gemini 原生协议 | Gemini、Antigravity 等模型 |

要求：

1. 模型列表和实际调用使用同一份规则。
2. 手工填写隐藏模型时必须拒绝调用。
3. 模型映射后再次校验最终模型。
4. 保留现有标准地址，不要求新增协议专用 Base URL。

部分客户端使用静态模型目录，服务端可能无法控制其界面显示，但仍须限制实际调用。

OpenAI Responses 和 Chat Completions 客户端都可能请求同一个 `/v1/models`。当请求没有携带可识别的客户端特征时，服务端无法可靠判断应返回哪一份列表；具体识别和回退规则在技术设计阶段确定。

### 3.4 账号连通性测试

所有账号都可以在统一账号页面执行测试：

```text
选择账号和模型
  -> 应用最终模型映射
  -> 根据账号设置选择普通请求或官方兼容 profile
  -> 发起最小真实请求
  -> 显示并保存结果
```

测试结果至少区分：

- 成功。
- 认证失败。
- 模型不可用或无权限。
- 协议不兼容。
- 配额耗尽或限流。
- 网络、代理或 TLS 错误。
- 上游服务异常。

测试流量应进入 Token 和成本统计，并单独标记。HTTP 200 但正文为降级或临时不可用提示时不能判定为成功。

## 4. 组件分工

| 功能 | CPA-Manager-Plus | CLIProxyAPI |
|---|---|---|
| 统一账号页面和添加向导 | 主要实现 | 提供 Management API |
| OpenAI 协议探测 | 负责探测流程和结果判断 | 优先复用现有 Management API，不默认新增专用接口 |
| 模型白名单和映射 | 配置页面 | 运行时执行 |
| 连通性测试 | 页面和结果展示 | 执行真实请求 |
| 用量、成本和健康状态 | 主要实现 | 产生运行数据 |
| 官方客户端兼容 | 配置和状态展示 | Executor、Transport 和路由执行 |
| 协议模型范围 | 配置页面 | 过滤列表并限制调用 |

CPA-Manager-Plus 不代理普通模型请求，只通过 CLIProxyAPI Management API 管理配置和认证文件，不绕过 API 直接读写 Gateway 宿主机文件、认证目录或运行数据库。

## 5. 第一版验收标准

1. 一个页面可以查看和管理全部平台账号。
2. 添加账号必须先选平台，再选 OAuth 或 API Key。
3. OpenAI API Key 可以自动识别 Responses 和 Chat Completions 能力。
4. OAuth 和 API Key 账号都可以配置允许模型和模型映射。
5. 每个账号都可以测试连通性，并显示明确失败原因。
6. 开启官方客户端兼容后，Claude 和 Codex 请求通过抓包对比测试。
7. 管理员可以分别配置四种协议显示的模型；可识别的模型目录请求按对应配置返回，隐藏模型无法直接调用。
8. 每个账号和 Key 都能查看 Token、请求次数和官方成本估算。
9. Manager 故障不影响 CLIProxyAPI 继续处理模型请求。
10. 两个组件可以独立升级和回滚。

## 6. 建议开发顺序

1. 建立 Pro 主项目、两个 fork 和统一 Compose。
2. 定义 Management API、用量事件和协议模型策略契约。
3. 实现统一账号页面、添加向导、用量窗口和连通性测试。
4. 实现 OpenAI API Key 自动探测和账号模型配置。
5. 在 CLIProxyAPI 增加 Claude/Codex 官方客户端兼容 profile。
6. 在 CLIProxyAPI 实现协议模型策略、模型目录识别和回退规则。
7. 完成集成与升级测试后发布第一版。

## 7. 默认目录与部署

开发工作区：

```text
cliproxyapi-pro/
├── components/
│   ├── cliproxyapi/
│   └── cpa-manager-plus/
├── deploy/
├── integration/
└── docs/
```

生成两个 Docker 镜像：

```text
cliproxyapi-pro-gateway
cliproxyapi-pro-manager
```

默认安装目录：

```text
/opt/cliproxyapi-pro/
├── compose.yaml
├── .env
├── gateway/
│   ├── config.yaml
│   ├── auths/
│   └── logs/
├── manager/
│   └── data/
└── proxy/
```

`gateway` 负责模型请求，`manager` 负责管理页面和统计。两个镜像通过一个 Compose 安装，对外使用同一个域名。

`sub2apiplus` 只作为开发参考，不放入 CLIProxyAPI Pro 项目。

产品名称使用 `CLIProxyAPI Pro`；开发目录、仓库名、安装目录、Compose 项目名和 Docker 镜像名称统一使用小写 `cliproxyapi-pro`。
