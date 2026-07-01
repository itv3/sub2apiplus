# Sub2API Plus

Sub2API Plus 是基于 [Wei-Shaw/sub2api](https://github.com/Wei-Shaw/sub2api) 的个人自用 fork。

本仓库用于维护自建镜像、私有部署和定制补丁，不作为公开项目介绍页使用。对外仓库名、Docker 镜像名、页面标题和默认站点名使用 `sub2apiplus` / `Sub2API Plus`；为降低上游同步成本，Go module、Go import、二进制名、systemd service 名和部分默认路径仍保留 `sub2api`。

## 维护重点

- 长期跟随上游 Sub2API 更新，按需合并 `upstream/main` 或上游 release tag。
- 自定义补丁尽量集中记录在 [PATCHES.md](PATCHES.md)。
- API Key 客户端伪装方案记录在 [sub2api-apikey客户端伪装.md](sub2api-apikey客户端伪装.md)。
- Docker 镜像发布到 `ghcr.io/itv3/sub2apiplus`。
- ARM64 部署使用 Docker Compose，只替换应用镜像，不动数据库、Redis、Nginx 和数据卷。

## 本地验证

后端 Go module 位于 `backend` 目录：

```bash
cd backend
make test-unit
make test-integration
```

前端位于 `frontend` 目录：

```bash
make test-frontend
```

## 发版流程

1. 更新 `backend/cmd/server/VERSION`。
2. 提交代码并推送 `main`。
3. 创建并推送 `v<version>` tag。
4. 通过 `.github/workflows/release.yml` 发布 GitHub Release 和 GHCR 镜像。
5. 在 ARM64 上拉取正式镜像并用 Docker Compose 重建 `sub2apiplus` 应用容器。

Release notes 只写当前版本变化和验证结果，不再重复安装命令、仓库链接和文档链接。

## 风险说明

Sub2API Plus 只用于技术研究和自有环境验证。使用本项目接入第三方 AI 服务可能违反相关服务商的用户协议，也可能带来账号限制、服务中断、额度损失或其他风险。请仅在遵守所在地法律法规和服务商条款的前提下使用。
