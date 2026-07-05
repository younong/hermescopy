# 阿里云部署

Hermes 的阿里云生产部署使用 `deploy/` 目录里的 Node.js 工具。发布规则是：先确定一个 Git tag，再在本机基于该 tag 构建 web/ui-tui 产物，最后把源码 + 构建产物上传到服务器。服务器只负责解包、按需初始化/更新共享 Python venv、切换 current symlink，并通过 systemd 直接运行 Hermes gateway 和 dashboard。

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

Dashboard 默认只监听服务器本机 `127.0.0.1:9119`。需要访问时使用 SSH tunnel：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后在本机打开 `http://localhost:9119`。

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
9. 服务健康检查通过后，按 release 保留策略清理旧 `/opt/hermes/releases/<tag>` 目录。

## 部署已有 tag / 回滚

部署已有 tag：

```bash
npm run deploy -- --tag v2026.7.4
```

回滚就是重新部署上一个 tag：

```bash
npm run deploy -- --tag v2026.7.3
```

`--tag` 模式会从 Git tag 生成源码包，不会上传当前工作区文件。构建产物随 release 上传；Python venv 为共享环境，仅当 `uv.lock` 变化时更新。

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
ssh root@106.15.186.104 'journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

APIYI smoke test 不是发布脚本必跑步骤；需要真实调用模型时再单独执行。当前部署收尾只检查 systemd 服务状态。

## 常用参数

```text
--host <host>            默认 106.15.186.104
--user <user>            默认 root
--port <port>            默认 22
--identity-file <path>   SSH 私钥路径
--remote-root <path>     默认 /opt/hermes
--force                  删除并重建服务器上同名 release 目录
--keep-releases <n>      成功部署后保留最近 n 个 release，默认 5
--no-prune-releases      不自动清理旧 release 目录
--allow-dirty            允许工作区有改动时部署已有 tag
--dry-run                只预览，不修改本机或服务器
```
