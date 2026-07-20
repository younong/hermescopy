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

常规发布先确定 Git tag，然后只发布该 tag 中的代码。

- 新发布：`--create-tag <tag>`
- 重试/回滚：`--tag <existing-tag>`
- 无 tag 的受限例外：`--ref <40-hex-commit-sha>`，仅发布已推送到 `origin` 的不可变完整 commit SHA；不会打 tag，也绝不会上传当前工作区或接受分支名、`HEAD`、短 SHA。

新发布前必须人工提交代码。`--create-tag` 要求具名分支和干净工作区，fetch 最新 `origin/main`，以 `--no-autostash` rebase 当前分支，无 force 地推送远端同名分支，然后只创建并 atomic push 指定的 annotated tag。默认只允许 `main`；`--allow-non-main` 保留，但同样必须 rebase 最新 `origin/main`，且不允许 detached HEAD 或 force push。rebase、non-fast-forward、tag 冲突或远端校验失败都会在部署前 fail closed。工具不会自动 commit/stash，也不会用 `--tags` 推送无关 tag。

工具使用 `git archive <tag>` 生成干净源码，在本机临时源码目录中安装 Node 依赖并构建 web/ui-tui 产物，然后把源码 + 构建产物打包上传到服务器。服务器只解包到 `/opt/hermes/releases/<tag>`、按 `uv.lock + 架构` 创建或复用 root-owned immutable Python runtime、验证 host sandbox policy、切换 `/opt/hermes/current`，最后以稳定的非 root `hermes` user/group 重启 systemd 服务。发布成功后会清理本次上传的远端 tarball，并按保留策略回收旧 release。

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
/opt/hermes/runtimes/python/<runtime-id> # root-owned immutable Python runtime
/opt/hermes/shared/hermes-service-runner.sh
/etc/hermes/executor-sandbox.json      # root-owned host sandbox policy
/etc/hermes/executor-x86_64.bpf        # root-owned seccomp artifact
```

Dashboard 只绑定 `127.0.0.1:9119`，生产入口为：

```text
https://abinllm.xyz/hermes/
```

Nginx 只负责 TLS、`/hermes` 路径和 HTTP/WebSocket 反代；Hermes durable local-user provider 是唯一登录层。现有 active member（例如 `user2`–`user5`）可直接登录，不需要先使用 admin 凭据。admin 角色仍只用于账号管理。

SSH tunnel 仅作为紧急诊断入口：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后在本机打开 `http://localhost:9119`。Dashboard 仍以 `--require-auth` 运行，因此 tunnel 不会绕过 Hermes user 登录。

## 服务器前置依赖

裸机部署需要服务器上有：

- systemd
- tar / gzip
- `sha256sum`
- Python 由 root-owned、只读的版本化 runtime 提供；部署脚本会打包 uv-managed Python、locked dependencies 和最小本地命令集
- Bubblewrap 必须安装为 `/usr/bin/bwrap` 并支持发布脚本检查的 namespace、bind-fd、seccomp 与 attestation 参数
- 内核必须允许非 root user namespace 和 seccomp filter
- 如果服务器没有 `uv`，部署脚本会用 `curl` 安装一次
- 常见编译/运行依赖按服务器实际错误补充，例如 `gcc`、`g++`、`make`、`cmake`、`python3-dev`、`python3-venv`、`ffmpeg`、`ripgrep`

Node.js/npm 只要求在本机可用。部署脚本会在从 Git tag 解出的本机临时源码目录中执行 `npm install --prefer-offline --no-audit`，并把 `web`、`ui-tui` 构建产物直接写入临时发布 artifact；服务器不再运行 npm install/build，当前 checkout 也不会留下发布构建产物。

## 常用命令

查看帮助：

```bash
npm run deploy -- --help
```

预览新 tag 发布（仍要求代码已提交且工作区干净，只做远端只读检查，不 rebase/push/tag）：

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

不创建 tag 时，部署已推送的完整 commit SHA：

```bash
npm run deploy -- --ref <40-hex-commit-sha> --dry-run
npm run deploy -- --ref <40-hex-commit-sha>
```

`--ref` 仍要求干净工作树，并将 release 目录固定命名为 `commit-<sha>`；artifact 中的 `.hermes-release.json` 必须与该 SHA 一致，已有不同来源的 release 不会被覆盖。

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

## Nginx 单一登录层迁移

仓库只维护 `deploy/nginx/hermes-dashboard.conf` 这个 server-context snippet，不覆盖完整 vhost、站点根应用或 Certbot/TLS 配置。首次从旧的 Nginx Basic Auth/remember-cookie 结构迁移时，必须显式执行：

```bash
npm run deploy -- --tag v2026.7.4 --migrate-nginx-hermes
```

迁移流程先启动 `--require-auth --trust-proxy-headers` 的新 Hermes，并从 loopback 验证 HTML 重定向和 API 401 均由 Hermes gate 提供；随后仅在旧 Hermes locations 唯一且完全匹配时备份 vhost、原子写入 include/snippet、执行 `nginx -t`，成功后 reload。未知、重复或部分迁移状态会 fail closed。后续普通发布只 reconcile 已存在的 include。

仅查看状态、不修改服务器：

```bash
ssh root@106.15.186.104 \
  'python3 /opt/hermes/current/deploy/nginx/manage_hermes_proxy.py status --vhost /etc/nginx/conf.d/abinllm.conf'
```

迁移前建议保存 `nginx -T` 和 vhost checksum。失败时优先恢复工具报告的 `abinllm.conf.hermes-backup-<timestamp>`，再执行 `nginx -t && systemctl reload nginx`。不要通过删除 `--require-auth`、清空 local-user SQLite、轮换 durable-store secret、重跑 bootstrap、恢复 root 服务身份或放宽 owner-home ownership 检查来回滚。旧 `.htpasswd-hermes` 只可在 `nginx -T` 确认不再引用后人工清理。

## 发布后检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'systemctl status --no-pager hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'nginx -t && nginx -T 2>/dev/null | grep -n -A25 -B5 hermes-dashboard.conf'
ssh root@106.15.186.104 'journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

使用隐私窗口访问 `https://abinllm.xyz/hermes/`，应直接看到 Hermes 登录页而不是浏览器原生 Basic Auth challenge。用 active member 验证 sessions API、普通功能和 WebSocket/PTY；member 的账号管理 API 仍应为 403，admin 管理读取仍应成功。gateway、dashboard、Owner Worker 和 `/opt/hermes/shared/.hermes/users/<owner-key>` 应使用同一个稳定 `hermes` UID/GID。现有 local-user DB、stable secret 和角色都保持不变。

发布脚本会执行 host sandbox preflight、systemd health、Hermes auth readiness 和 Nginx validation。APIYI smoke test 不是必跑步骤；需要真实调用模型时再单独执行。
