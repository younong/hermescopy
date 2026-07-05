# Hermes 发布工具

这个目录集中管理 Hermes 发布到阿里云服务器的工具和生产部署配置。

## 文件

- `deploy.mjs` — Node.js 发布脚本，按 Git tag 上传源码并在服务器裸机/systemd 方式部署。

详细部署说明见：`docs/deployment/alicloud.md`。

## 默认服务器

- Host: `106.15.186.104`
- User: `root`
- Remote root: `/opt/hermes`

## 核心发布规则

每次发布必须先确定 Git tag，然后只发布该 tag 中的代码。

- 新发布：`--create-tag <tag>`
- 重试/回滚：`--tag <existing-tag>`

工具使用 `git archive <tag>` 生成干净源码，在本机临时源码目录中安装 Node 依赖并构建 web/ui-tui 产物，然后把源码 + 构建产物打包上传到服务器。服务器只解包到 `/opt/hermes/releases/<tag>`、按需初始化/更新共享 Python venv、切换 `/opt/hermes/current`，最后重启 systemd 服务。发布成功后会清理本次上传的远端 tarball，并按保留策略回收旧 release。

## 服务器运行方式

当前阿里云生产路径为裸机/systemd，不在服务器上构建 Docker 镜像。

服务名：

- `hermes-gateway.service`
- `hermes-dashboard.service`

持久化目录：

```text
/opt/hermes/releases/<tag>       # 每个 tag 一个 release 目录，含本机预构建产物
/opt/hermes/current              # 当前线上版本 symlink
/opt/hermes/shared/.hermes       # 持久化数据 / HERMES_HOME
/opt/hermes/shared/.env          # 服务器本地环境变量，永不提交
/opt/hermes/shared/venv          # 共享 Python venv，仅 uv.lock 变化时更新
/opt/hermes/shared/hermes-service-runner.sh
```

Dashboard 默认只绑定 `127.0.0.1:9119`。需要访问时使用 SSH tunnel：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后在本机打开 `http://localhost:9119`。

## 服务器前置依赖

裸机部署需要服务器上有：

- systemd
- tar / gzip
- `sha256sum`
- Python 由共享 venv 提供；首次部署或 `uv.lock` 变化时，部署脚本会用 `uv sync` 初始化/更新 `/opt/hermes/shared/venv`
- 如果服务器没有 `uv`，部署脚本会用 `curl` 安装一次
- 常见编译/运行依赖按服务器实际错误补充，例如 `gcc`、`g++`、`make`、`cmake`、`python3-dev`、`python3-venv`、`ffmpeg`、`ripgrep`

Node.js/npm 只要求在本机可用。部署脚本会在从 Git tag 解出的本机临时源码目录中执行 `npm install --prefer-offline --no-audit`，并把 `web`、`ui-tui` 构建产物直接写入临时发布 artifact；服务器不再运行 npm install/build，当前 checkout 也不会留下发布构建产物。

## 常用命令

查看帮助：

```bash
npm run deploy -- --help
```

预览新 tag 发布：

```bash
npm run deploy -- --create-tag v2026.7.4 --dry-run
```

创建 tag 并发布：

```bash
npm run deploy -- --create-tag v2026.7.4
```

部署已有 tag：

```bash
npm run deploy -- --tag v2026.7.4
```

回滚到旧 tag：

```bash
npm run deploy -- --tag v2026.7.3
```

## SSH 认证

推荐使用 SSH key：

```bash
npm run deploy -- --tag v2026.7.4 --identity-file ~/.ssh/hermes-alicloud
```

临时密码登录只允许使用本机环境变量，不要写入仓库：

```bash
export HERMES_DEPLOY_PASSWORD='***'
npm run deploy -- --tag v2026.7.4
```

如果使用密码自动登录，本机需要安装 `sshpass`。密码不会被脚本打印。

## APIYI 图像模型环境变量

APIYI 令牌只放在服务器本地 `/opt/hermes/shared/.env`，不要写进仓库：

```bash
APIYI_API_KEY=***
```

可选 endpoint 覆盖：

```bash
APIYI_OPENAI_BASE_URL=https://api.apiyi.com/v1
APIYI_GEMINI_BASE_URL=https://api.apiyi.com/v1beta
```

部署脚本生成的 systemd runner 会读取 `/opt/hermes/shared/.env`，但不会打印其中内容。

## Release 保留与清理

发布成功后会删除本次上传的远端 tarball：`/opt/hermes/tmp/hermes-<tag>.tar.gz`。

旧 release 目录默认保留最近 5 个，同时永远保护本次部署版本、部署前后 `/opt/hermes/current` 指向的版本。可按需调整：

```bash
npm run deploy -- --tag v2026.7.4 --keep-releases 8
npm run deploy -- --tag v2026.7.4 --no-prune-releases
```

## 发布后检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'systemctl status --no-pager hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

APIYI smoke test 不是发布脚本的必跑步骤；需要真实调用模型时再单独执行。当前部署收尾只检查 systemd 服务状态。
