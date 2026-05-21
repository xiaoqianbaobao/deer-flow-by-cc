# DeerFlow 本地 Docker 运行与登录指南

这篇文档面向第一次在本机把 DeerFlow 跑起来的使用者，目标是让你从零开始完成下面整条链路：

- 准备本地环境
- 启动 Docker 开发环境
- 启用真实账号登录（identity）
- 使用管理员账号登录 DeerFlow
- 知道如何查看日志、停止、重启和排查常见问题

默认示例基于当前仓库在 Windows 开发机上的推荐方式编写；如果你使用 macOS / Linux，也可以参考同样的 `.env`、Docker Compose 和 URL。

## 1. 最终效果

完成本文后，你应该能做到：

- 在浏览器打开 `http://localhost:2026/login`
- 看到正常的邮箱 / 密码登录表单
- 使用管理员账号登录成功
- 进入 `http://localhost:2026/workspace`

默认本地管理员账号如下：

- 邮箱：`admin@local.deerflow`
- 密码：`DeerFlow123!`
- Bootstrap 重置 token：`deerflow-bootstrap-local`

## 2. 前提条件

开始前请确认本机已经安装：

- Docker Desktop，并且 Docker Engine 处于运行状态
- Git for Windows
- 可选：`make`

如果你在 Windows 上没有 `make`，也没关系，本文会优先给出可直接执行的命令。

## 3. 项目目录

假设你的项目目录是：

```text
d:\workspace\deer-flow-by-cc
```

后续所有命令都默认在这个目录执行。

## 4. 第一次启动前需要知道的事

当前仓库的 Docker 开发环境支持两种状态：

- 不启用 identity：直接进入工作区，不走登录
- 启用 identity：启动 Postgres、Redis、JWT、登录接口和登录页

如果你想跑通真正的账号登录链路，关键开关是：

```bash
ENABLE_IDENTITY=true
```

只要这个开关打开，当前仓库里的启动脚本会自动帮你补齐本地默认值并完成这些动作：

- 启动 `postgres`
- 启动 `redis`
- 生成 `config/identity.yaml`
- 跑数据库迁移
- 生成 JWT 密钥
- 初始化首个管理员密码

## 5. 推荐启动方式

### 5.1 Windows 推荐命令

在 PowerShell 中执行：

```powershell
cd d:\workspace\deer-flow-by-cc

if (!(Test-Path .env)) {
  New-Item -ItemType File -Path .env | Out-Null
}

if (-not (Select-String -Path .env -Pattern '^ENABLE_IDENTITY=' -Quiet -ErrorAction SilentlyContinue)) {
  Add-Content .env 'ENABLE_IDENTITY=true'
}

$env:NPM_REGISTRY='https://registry.npmmirror.com'
$env:UV_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple'
$env:APT_MIRROR='mirrors.aliyun.com'

.\scripts\run-with-git-bash.cmd ./scripts/docker.sh init
.\scripts\run-with-git-bash.cmd ./scripts/docker.sh start
```

说明：

- 第一段会确保项目根目录下存在 `.env`
- 第二段会把 `ENABLE_IDENTITY=true` 写入 `.env`
- 后面三个环境变量是国内网络环境下推荐的镜像源
- `docker.sh start` 会自动完成 identity 的依赖和初始化

### 5.2 如果你本机有 `make`

```bash
cd d:/workspace/deer-flow-by-cc
echo "ENABLE_IDENTITY=true" >> .env
make docker-init
make docker-start
```

如果 `.env` 里已经有 `ENABLE_IDENTITY=` 这一行，就不要重复追加，直接改成 `true` 即可。

## 6. 启动过程中会发生什么

当 `ENABLE_IDENTITY=true` 时，启动脚本会依次完成这些事情：

1. 检查并补齐 `config.yaml`、`.env`、`frontend/.env`、`extensions_config.json`
2. 补齐 identity 相关默认环境变量
3. 生成 `config/identity.yaml`
4. 启动 `postgres` 和 `redis`
5. 在容器内执行：
   - `make db-upgrade`
   - `make identity-bootstrap`
   - `make identity-keys`
6. 给默认管理员账号初始化密码
7. 启动：
   - `gateway`
   - `langgraph`
   - `frontend`
   - `nginx`
   - 以及按 `config.yaml` 判断是否需要 `provisioner`

## 7. 启动成功后如何验证

### 7.1 看容器

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
```

你应该能看到类似这些容器：

- `deer-flow-nginx`
- `deer-flow-frontend`
- `deer-flow-gateway`
- `deer-flow-langgraph`
- `deer-flow-dev-postgres-1`
- `deer-flow-dev-redis-1`

### 7.2 看登录页

浏览器打开：

- [http://localhost:2026/login](http://localhost:2026/login)

如果启动成功，你应该看到：

- Email 输入框
- Password 输入框
- `Sign in` 按钮

而不是：

- `Could not reach the auth service.`
- 空白页
- 自动跳回无登录态的工作区

## 8. 登录链路

### 8.1 使用默认管理员账号登录

在登录页输入：

- Email：`admin@local.deerflow`
- Password：`DeerFlow123!`

登录成功后会进入：

- [http://localhost:2026/workspace](http://localhost:2026/workspace)

通常会进一步跳转到：

- `http://localhost:2026/workspace/chats/new`

### 8.2 登录后能做什么

这个默认账号是平台管理员，因此你可以：

- 使用普通聊天和工作区功能
- 打开管理员页面
- 管理组织、用户和注册码
- 验证 identity 鉴权链路是否工作正常

## 9. OIDC 是怎么接入的

如果你暂时只需要邮箱 + 密码登录，当前配置已经够用，不需要额外操作。

如果你还想接入 OIDC，例如：

- Keycloak
- Okta
- Azure AD

需要编辑：

- `config/identity.yaml`

当前仓库已经提供一个最小可用文件：

```yaml
oidc:
  providers: {}
```

你也可以从模板复制：

```bash
cp config/identity.yaml.example config/identity.yaml
```

然后为某个 provider 填写：

- `issuer`
- `client_id`
- `client_secret`

只有真正配置了 provider 后，登录页才会出现对应的 OIDC 登录按钮。

## 10. 常用命令

### 10.1 查看日志

```powershell
cd d:\workspace\deer-flow-by-cc
.\scripts\run-with-git-bash.cmd ./scripts/docker.sh logs
```

### 10.2 停止服务

```powershell
cd d:\workspace\deer-flow-by-cc
.\scripts\run-with-git-bash.cmd ./scripts/docker.sh stop
```

### 10.3 重启服务

最简单的方法是先停再起：

```powershell
cd d:\workspace\deer-flow-by-cc
.\scripts\run-with-git-bash.cmd ./scripts/docker.sh stop

$env:NPM_REGISTRY='https://registry.npmmirror.com'
$env:UV_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple'
$env:APT_MIRROR='mirrors.aliyun.com'

.\scripts\run-with-git-bash.cmd ./scripts/docker.sh start
```

## 11. 关键文件说明

本地 Docker + 登录链路主要依赖这些文件：

- `scripts/docker.sh`
  - 开发模式启动入口
- `docker/docker-compose-dev.yaml`
  - 开发模式的容器编排
- `.env`
  - 本地环境变量，包括 `ENABLE_IDENTITY=true`
- `config/identity.yaml`
  - OIDC provider 配置
- `config.yaml`
  - DeerFlow 主配置
- `extensions_config.json`
  - MCP Server 配置

## 12. 常见问题

### 12.1 登录页显示 `Could not reach the auth service.`

优先检查：

- `.env` 里是否有 `ENABLE_IDENTITY=true`
- `postgres` 和 `redis` 是否已经启动
- `gateway` 容器是否正常

可以先看：

```powershell
docker ps --format "table {{.Names}}\t{{.Status}}"
curl.exe -i http://localhost:2026/api/auth/providers
```

正常情况下，`/api/auth/providers` 应该返回 `200`。

### 12.2 登录页有表单，但账号登不上去

先确认你是否用了默认本地管理员账号：

- `admin@local.deerflow`
- `DeerFlow123!`

如果你改过 `.env` 里的 bootstrap 管理员配置，实际可登录账号要以你自己的配置为准。

### 12.3 前端报 `Unable to add filesystem: <illegal path>`

这不是登录问题，而是 `extensions_config.json` 里的 `filesystem` MCP 路径非法。

当前仓库在 Docker 开发环境里已经把默认路径修成容器内真实存在的：

```json
"/app"
```

如果你又手动改回了模板占位值，例如：

```json
"/path/to/allowed/files"
```

就会重新触发这个错误。

### 12.4 Windows 上为什么要走 `run-with-git-bash.cmd`

因为项目里的脚本是 bash 脚本。直接在原生 PowerShell / `cmd.exe` 执行 `scripts/docker.sh` 不稳定，Windows 上推荐通过：

```powershell
.\scripts\run-with-git-bash.cmd ./scripts/docker.sh start
```

让 Git Bash 去执行。

### 12.5 我不想启用登录，只想直接进工作区

把 `.env` 里的：

```bash
ENABLE_IDENTITY=true
```

改成：

```bash
ENABLE_IDENTITY=false
```

然后重启服务即可。

## 13. 一份最短版清单

如果你只想快速照抄，按这个最短步骤走：

```powershell
cd d:\workspace\deer-flow-by-cc

if (!(Test-Path .env)) {
  New-Item -ItemType File -Path .env | Out-Null
}

if (-not (Select-String -Path .env -Pattern '^ENABLE_IDENTITY=' -Quiet -ErrorAction SilentlyContinue)) {
  Add-Content .env 'ENABLE_IDENTITY=true'
}

$env:NPM_REGISTRY='https://registry.npmmirror.com'
$env:UV_INDEX_URL='https://pypi.tuna.tsinghua.edu.cn/simple'
$env:APT_MIRROR='mirrors.aliyun.com'

.\scripts\run-with-git-bash.cmd ./scripts/docker.sh start
```

然后打开：

- [http://localhost:2026/login](http://localhost:2026/login)

登录：

- 邮箱：`admin@local.deerflow`
- 密码：`DeerFlow123!`

进入：

- [http://localhost:2026/workspace](http://localhost:2026/workspace)
