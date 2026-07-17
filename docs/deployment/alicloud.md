# 阿里云部署

Hermes 的阿里云生产部署使用 `deploy/` 目录里的 Node.js 工具。常规发布先确定一个 Git tag，再在本机基于该 tag 构建 web/ui-tui 产物，最后把源码 + 构建产物上传到服务器。无 tag 的受限例外是 `--ref <40-hex-commit-sha>`：它只接受已推送到 `origin` 的不可变完整 commit SHA，绝不会发布当前工作区或可移动分支。服务器只负责解包、按需初始化/更新共享 Python venv、切换 current symlink，并通过 systemd 直接运行 Hermes gateway 和 dashboard。

服务器默认配置：

- Host: `106.15.186.104`
- User: `root`
- Remote root: `/opt/hermes`

> 不要把服务器密码、API key 或 `.env` 文件提交到仓库。建议尽快改用 SSH key 登录；如果临时使用密码，放在本机环境变量 `HERMES_DEPLOY_PASSWORD` 中，并安装 `sshpass`。

## 服务器准备

当前生产路径为裸机/systemd，不需要在服务器上构建 Hermes Docker 镜像，也不在服务器上运行 npm install/build。服务器需要：

- systemd
- tar / gzip
- `sha256sum`
- Python 由共享 venv 提供；首次部署或 `uv.lock` 变化时，部署脚本会用 `uv sync` 初始化/更新 `/opt/hermes/shared/venv`
- 如果服务器没有 `uv`，部署脚本会用 `curl` 安装一次
- 常见编译/运行依赖按服务器实际错误补充，例如 `gcc`、`g++`、`make`、`cmake`、`python3-dev`、`python3-venv`、`ffmpeg`、`ripgrep`

Node.js/npm 只要求在本机可用。部署脚本会在从 Git tag 解出的临时源码目录中执行 `npm install --prefer-offline --no-audit`，并把 `web`、`ui-tui` 构建产物直接写入临时发布 artifact；不会把构建产物写回当前 checkout。

部署工具会自动创建这些目录：

```text
/opt/hermes/releases/<tag>       # 每个 tag 一个 release 目录，含本机预构建产物
/opt/hermes/current              # 指向当前 release 的 symlink
/opt/hermes/shared/.hermes       # Hermes 持久化数据 / HERMES_HOME
/opt/hermes/shared/.env          # 服务器本地环境变量，永不进 git
/opt/hermes/shared/venv          # 共享 Python venv，仅 uv.lock 变化时更新
/opt/hermes/shared/hermes-service-runner.sh
```

Systemd 服务：

```text
/etc/systemd/system/hermes-gateway.service
/etc/systemd/system/hermes-dashboard.service
```

Dashboard 只监听服务器本机 `127.0.0.1:9119`，公开入口为：

```text
https://abinllm.xyz/hermes/
```

Nginx 只终止 TLS，并代理 `/hermes` 下的 HTTP 和 WebSocket；Hermes durable local-user provider 是唯一认证层。现有 active member 可直接使用自己的 user 凭据登录，不需要先通过 admin 账号。admin 权限仍只用于 local-user 账号管理。

SSH tunnel 只作为紧急诊断方式：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后在本机打开 `http://localhost:9119`。服务始终保留 `--require-auth`，所以 tunnel 访问仍需 Hermes user 登录。

## 推荐：使用 SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/hermes-alicloud
ssh-copy-id -i ~/.ssh/hermes-alicloud.pub root@106.15.186.104
```

部署时指定 key：

```bash
npm run deploy -- --tag v2026.7.4 --identity-file ~/.ssh/hermes-alicloud
```

## 临时：使用密码登录

本工具不会读取仓库里的密码文件，也不会打印密码。若必须短期使用密码：

```bash
export HERMES_DEPLOY_PASSWORD='不要写进文档或仓库'
npm run deploy -- --tag v2026.7.4
```

需要本机安装 `sshpass`，否则使用系统 SSH 的交互式密码输入。

## APIYI 图像模型生产配置

APIYI 令牌只放在服务器本地环境文件中，不要提交到仓库：

```bash
ssh root@106.15.186.104
vim /opt/hermes/shared/.env
```

添加变量名和值：

```bash
APIYI_API_KEY=***
```

如果 APIYI 后续变更 endpoint，也可以在服务器环境中覆盖：

```bash
APIYI_OPENAI_BASE_URL=https://api.apiyi.com/v1
APIYI_GEMINI_BASE_URL=https://api.apiyi.com/v1beta
```

Hermes 中选择 APIYI 图像后端后，可用模型包括 `gpt-image-2-low`、`gpt-image-2-medium`、`gpt-image-2-high` 和 `nano-banana-2`。

部署脚本生成的 systemd runner 会读取 `/opt/hermes/shared/.env`，但不会打印其中内容。

## 发布并部署新 tag

从当前 `main` 创建 tag、推送 tag，然后部署：

```bash
npm run deploy -- --create-tag v2026.7.4
```

如果当前分支不是 `main`，工具会拒绝创建 tag。确实需要从其他分支发布时显式加：

```bash
npm run deploy -- --create-tag v2026.7.4-test --allow-non-main
```

部署过程会：

1. 在本机基于 tag 解出干净源码。
2. 在本机安装 Node workspace 依赖并构建 web dashboard 和 TUI。
3. 把源码 + 构建产物打包上传到服务器临时目录，再解包到 `/opt/hermes/releases/<tag>`。
4. 成功解包后删除本次上传的 `/opt/hermes/tmp/hermes-<tag>.tar.gz`。
5. 在服务器上按 `uv.lock` hash 判断是否需要初始化/更新 `/opt/hermes/shared/venv`。
6. 切换 `/opt/hermes/current`。
7. 写入/更新 systemd unit。
8. 重启 `hermes-gateway` 和 `hermes-dashboard`。
9. 从 loopback 带生产代理头验证 Hermes 自己的登录 gate 已生效。
10. 首次迁移时显式替换旧 Nginx 外层认证；后续发布只同步已托管 snippet，并在 `nginx -t` 成功后 reload。
11. 按 release 保留策略清理旧 `/opt/hermes/releases/<tag>` 目录。

## 首次移除 Nginx 外层认证

旧生产配置在 `/hermes/` 上使用 Nginx `auth_basic`/remember-cookie，再由 Hermes 执行一次 durable local-user 登录，因而产生两次登录。仓库中的 `deploy/nginx/hermes-dashboard.conf` 将 Nginx 限定为 TLS、path-prefix 和 WebSocket proxy，并显式关闭继承的 `auth_basic`/`auth_request`；身份认证全部交给 Hermes。

首次迁移必须单独批准并显式加参数：

```bash
npm run deploy -- --tag v2026.7.4 --migrate-nginx-hermes
```

部署工具会先启动 loopback 上的 Hermes，配置 `HERMES_DASHBOARD_PUBLIC_URL=https://abinllm.xyz/hermes`，并要求 `--require-auth --trust-proxy-headers`。只有内部检查确认未登录 HTML 返回登录重定向、受保护 API 返回 401 后，迁移 helper 才会：

1. 识别唯一、完整的旧 Hermes locations；未知、重复或部分迁移状态立即拒绝。
2. 备份 `/etc/nginx/conf.d/abinllm.conf`。
3. 仅把旧 Hermes locations 替换为 `/etc/nginx/snippets/hermes-dashboard.conf` include，保留根应用、TLS、Certbot 和 sibling locations。
4. 原子写入并执行 `nginx -t`；成功后才 reload，失败则恢复 vhost 和 snippet。

普通后续发布不静默迁移 vhost，只 reconcile 已存在的唯一 include。状态可只读检查：

```bash
ssh root@106.15.186.104 \
  'python3 /opt/hermes/current/deploy/nginx/manage_hermes_proxy.py status --vhost /etc/nginx/conf.d/abinllm.conf'
```

`--dry-run` 不连接或修改服务器；它会打印将执行的远端脚本和 migration/reconcile 模式。实际迁移前另行保存 `nginx -T`、vhost checksum、systemd unit 和服务状态。

此次迁移不修改 local-user SQLite、stable durable-store secret 或现有 admin/member 角色，不重跑 bootstrap，也不会自动删除 `.htpasswd-hermes`。只有通过 `nginx -T` 确认旧文件不再被引用后，才可人工清理。

## 部署已有 tag / 回滚

部署已有 tag：

```bash
npm run deploy -- --tag v2026.7.4
```

回滚应用版本就是重新部署上一个 tag：

```bash
npm run deploy -- --tag v2026.7.3
```

如果 Nginx 迁移本身需要回滚，恢复 helper 输出的 `abinllm.conf.hermes-backup-<timestamp>`，然后运行：

```bash
nginx -t && systemctl reload nginx
```

不要通过移除 `--require-auth`、删除 local-user store、轮换 stable secret 或重新 bootstrap 来处理故障。这些操作会破坏认证状态，而不是安全回滚。

`--tag` 模式会从 Git tag 生成源码包，不会上传当前工作区文件。构建产物随 release 上传；Python venv 为共享环境，仅当 `uv.lock` 变化时更新。

## 无 tag 部署已推送 commit

仅在明确不创建 tag 时使用完整、已推送的 commit SHA：

```bash
npm run deploy -- --ref <40-hex-commit-sha> --dry-run
npm run deploy -- --ref <40-hex-commit-sha>
```

`--ref` 拒绝 `HEAD`、分支名、短 SHA、脏工作区和 `--force`。工具从该 SHA 的 `git archive` 构建 artifact，写入来源 manifest，并部署至 `/opt/hermes/releases/commit-<sha>`；已有来源不匹配的 release 会 fail closed，不会被覆盖。回滚继续使用稳定 tag。

## Release 保留与清理

发布成功后工具会自动删除本次上传的远端 tarball：

```text
/opt/hermes/tmp/hermes-<tag>.tar.gz
```

旧 release 目录默认保留最近 5 个，同时永远保护本次部署版本、部署前后 `/opt/hermes/current` 指向的版本。保护对象超过保留数量时会超额保留，不会为了满足数量删除当前或回滚所需版本。

调整保留数量：

```bash
npm run deploy -- --tag v2026.7.4 --keep-releases 8
```

禁用旧 release 回收：

```bash
npm run deploy -- --tag v2026.7.4 --no-prune-releases
```

## Dry run

预览将执行的步骤，不创建本地 tag、不上传、不改服务器：

```bash
npm run deploy -- --create-tag v2026.7.4 --dry-run
npm run deploy -- --tag v2026.7.4 --dry-run
npm run deploy -- --tag v2026.7.4 --keep-releases 3 --dry-run
```

## 服务器状态检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'systemctl status --no-pager hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'nginx -t && nginx -T 2>/dev/null | grep -n -A25 -B5 hermes-dashboard.conf'
ssh root@106.15.186.104 'journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

迁移后使用隐私窗口访问 `https://abinllm.xyz/hermes/`：浏览器应直接显示 Hermes 登录页，不再弹出原生 Basic Auth。用一个 active member 验证 dashboard、WebSocket/PTY 和普通 owner 功能，确认账号管理仍返回 403；再用独立 admin 会话确认管理读取可用。验证 logout、过期/篡改 cookie 和非 Hermes 站点未回归。

APIYI smoke test 不是发布脚本必跑步骤；需要真实调用模型时再单独执行。

## 常用参数

```text
--host <host>            默认 106.15.186.104
--user <user>            默认 root
--port <port>            默认 22
--identity-file <path>   SSH 私钥路径
--remote-root <path>     默认 /opt/hermes
--force                  已弃用并拒绝；不可变 release 不会被覆盖
--keep-releases <n>      成功部署后保留最近 n 个 release，默认 5
--no-prune-releases      不自动清理旧 release 目录
--allow-dirty            允许工作区有改动时部署已有 tag
--dashboard-public-url   trusted loopback proxy 的公开 URL
--migrate-nginx-hermes   显式迁移已识别的旧 Hermes Nginx auth block
--dry-run                只预览，不修改本机或服务器
```
