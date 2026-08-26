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

生产 artifact 始终来自不可变 Git tag。

- 新发布：开发分支先通过 PR 合入 `main`，同步本地 `main` 后使用 `--create-tag <tag>`。
- 重试/回滚：仅使用 origin 上已经发布、且本地与远端同名 tag 指向同一 commit 的 `--tag <existing-tag>`。
- 不支持按工作区、分支名或 commit SHA 直接发布；原 `--ref` 入口已删除。

常规 `--create-tag` 要求具名 `main` 和干净工作区。工具 fetch 最新 `origin/main` 后，要求 `HEAD`、本地 `main` 与 `origin/main` 完全一致；落后、领先或分叉都会停止。常规路径不会 rebase 或 push `main`，只创建并 push 唯一目标 annotated tag，不会用 `--tags` 推送其他本地 tag。

`--allow-non-main` 只保留给用户明确认定的紧急事故，不是 PR 未合入或 main 未同步时的快捷方式。应急路径仍要求具名分支和干净工作区，会 rebase 最新 `origin/main`，用绑定 observed SHA 的完整分支 ref exact lease 更新远端同名分支，再以 prepared commit lease atomic push 分支守卫与唯一 tag；rebase 冲突、detached HEAD、tag 冲突或远端并发移动都会 fail closed。通过 AI 执行时，每一条包含 `--allow-non-main` 的完整命令都必须按当前分支、tag、dry-run/真实模式单独获得用户明确批准；一般性的“发布”“继续”或此前批准不能沿用，dry-run 和真实部署也必须分别请示。

`scripts/release.py --publish` 负责 GitHub Release 和分发 artifact，不是阿里云部署入口，不能用于绕过以上 main/tag 规则。

工具使用 `git archive <tag>` 生成干净源码，在本机临时源码目录中安装 Node 依赖、构建 `web`，并用独立 lockfile 生成只含 PptxGenJS 的 PowerPoint payload，然后把生产运行源码 + 本机预构建产物打包上传到服务器。构建完成后，归档会省略 `tests/`、`website/`、`.github/` 和 `docs/` 等非运行目录。服务器只解包到 `/opt/hermes/releases/<tag>`、按 locked Python/PowerPoint 输入和架构创建或复用 root-owned immutable runtime、验证 host sandbox policy，并把 runner、Dashboard unit、policy 和 seccomp 先写入事务 staging。切换时停止旧 Dashboard，由其排空并吊销 Owner Workers；若旧版本仍有 standalone Gateway unit，则仅在迁移中停止、禁用并移除它。确认旧服务退出后才原子替换 `/opt/hermes/current` 和 staged artifacts，随后只启动 `hermes-dashboard.service`。切换前会通过真实 authenticated Bubblewrap executor 生成两页 PPTX、用 MarkItDown 校验顺序，并用 LibreOffice 转换一次 PDF。发布工具还会在远端事务前建立 authenticated 连续性会话，在候选成功或 pre-commit 回滚后用新单次 ticket 恢复同一 canonical session，再执行提交后公开真实模型冒烟。发布成功后会清理本次上传的远端 tarball 和临时冒烟数据，并按保留策略回收旧 release。历史 `/opt/hermes/releases/commit-<sha>` 目录不会在升级时被主动删除；当前或刚替换的 release 仍受保护，其他目录之后由既有保留策略自然清理，但它们不再是新发布或回滚来源。

## 服务器运行方式

当前阿里云生产路径为裸机/systemd，不在服务器上构建 Docker 镜像。

唯一候选服务：

- `hermes-dashboard.service`（同时托管 authenticated Web、Owner Worker、Session Reader 和 canonical connectors）

旧 `hermes-gateway.service` 仅在升级事务中被识别并退休；新版本不会安装或启动它。

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

然后在本机打开 `http://localhost:9119`。Dashboard 始终启用 cookie/session 认证，因此 tunnel 不会绕过 Hermes user 登录。

## 服务器前置依赖

裸机部署需要服务器上有：

- systemd
- tar / gzip
- `sha256sum`
- Python 由 root-owned、只读的版本化 runtime 提供；部署脚本会打包 uv-managed Python、locked dependencies 和最小本地命令集
- Bubblewrap 必须安装为 `/usr/bin/bwrap` 并支持发布脚本检查的 namespace、bind-fd、seccomp 与 attestation 参数
- 内核必须允许非 root user namespace 和 seccomp filter
- 如果服务器没有 `uv`，部署脚本会用 `curl` 安装一次
- PowerPoint 的 LibreOffice/font 前置依赖由 `deploy/runtime/alicloud3-powerpoint-packages.json` 精确约束。普通部署只校验并 fail closed；首次补齐时显式传 `--provision-powerpoint-deps`，仅执行该 manifest 中的 additive `dnf install`
- 常见编译/运行依赖按服务器实际错误补充，例如 `gcc`、`g++`、`make`、`cmake`、`python3-dev`、`python3-venv`、`ffmpeg`、`ripgrep`

Node.js/npm 只要求在本机可用。部署脚本会在从 Git tag 解出的本机临时源码目录中执行 workspace 构建，并在 `deploy/powerpoint-runtime` 执行 `npm ci --omit=dev --ignore-scripts --no-audit`。服务器不运行 npm install/build；PptxGenJS payload、Node、LibreOffice、字体与 MarkItDown 都进入 root-owned immutable runtime，authenticated executor 只读挂载它们。生产 `uv sync` 从阿里云 PyPI 镜像下载 `uv.lock` 已锁定的 Python wheel，避免官方 PyPI CDN 在国内链路上的大文件下载瓶颈；锁文件和校验仍决定最终版本与内容。

首次在符合 manifest 的 Alibaba Cloud Linux 3 x86_64 主机上补齐 PowerPoint 前置包：

```bash
npm run deploy -- --tag <tag> --provision-powerpoint-deps --dry-run
npm run deploy -- --tag <tag> --provision-powerpoint-deps
```

Dry-run 会披露 provisioning 与 PowerPoint runtime smoke 均为 planned，但不安装包。真实部署若在后续 pre-commit 步骤失败会恢复旧 release/policy；已经 additive 安装到主机的 RPM 不会自动卸载，但旧 runtime 不会引用它们。后续普通发布无需该 flag，只会核对精确 NEVRA 并从 package-owned 文件重新构建不可变快照。

## 常用命令

查看帮助：

```bash
npm run deploy -- --help
```

常规新 tag 发布必须先把开发分支通过 PR 合入 `main`，再同步本地 `main`：

```bash
git status --short
git switch main
git pull --ff-only origin main
npm run deploy -- --create-tag v2026.7.4 --dry-run
npm run deploy -- --create-tag v2026.7.4
```

`--dry-run` 仍执行本地和 origin Git 只读校验，但不创建 tag、不上传、不连接服务器。工具确认本地与远端 `main` 完全一致后，真实执行只发布目标 tag。

明确的紧急事故才可从 non-main 分支创建 tag：

```bash
npm run deploy -- --create-tag <emergency-tag> --allow-non-main --dry-run
npm run deploy -- --create-tag <emergency-tag> --allow-non-main
```

这两条命令通过 AI 执行时必须分别单独获得用户对完整命令的明确批准。

部署 origin 上已有的同名同 commit tag：

```bash
npm run deploy -- --tag v2026.7.4 --dry-run
npm run deploy -- --tag v2026.7.4
```

`--tag` 从不可变 tag 构建，不上传当前工作区；工作区不干净时默认拒绝，可显式使用 `--allow-dirty`，但本地修改仍不会进入 artifact。只存在于本地或与 origin 指向不同 commit 的 tag 会被拒绝。

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

## 自动对话冒烟与跨版本连续性

每次非 dry-run 发布都自动执行三层检查：

1. **跨版本连续性 watcher**：远端事务开始前，本机通过安全登录 helper 建立真实模型会话并保持浏览器连接。切换或 pre-commit 回滚时必须收到 `1012 Service Restart`；watcher 在服务不可用期间持续申请新的单次 WebSocket ticket，恢复后以稳定 `browser_id` 和 owner-scoped canonical pointer 继续同一 session lineage，再做一次 cold resume 和删除。它不会复用 ticket，也不会把 owner/session ID、prompt 或模型输出写入总结。
2. **事务内确定性冒烟**：systemd 和 Hermes 内部认证 readiness 通过后、Nginx reconcile 和 `deployment_committed` 之前，在服务器上以 `hermes` 用户、`env -i`、独立 `HOME`/`TMPDIR` 运行 `deploy/smoke-conversation.py`。它使用 loopback 假模型且禁止非 loopback 网络，不读取 `/opt/hermes/shared/.env`，覆盖 session create、provider/model 传播、附件、terminal、危险命令拒绝、流式输出、持久化、第二 gateway 进程 cold resume、继续对话和清理。失败会退出当前事务，由 EXIT trap 恢复旧 symlink/unit/policy 并启动旧版本；连续性 watcher 随后验证恢复路径。
3. **提交后公开真实 AI 冒烟**：远端 Nginx 校验成功并写入 commit marker 后，本机再次运行 `scripts/smoke_dashboard_conversation.py`。它申请单次 WebSocket ticket，连接带 path prefix 的公开 `/api/ws`，创建会话、验证真实模型 delta/completion、关闭后 cold resume、确认持久化并删除会话。

本机需要安装 `playwright-cli`，且仓库根目录 `.env.local` 只包含：

```dotenv
HERMES_DASHBOARD_BROWSER_USERNAME=...
HERMES_DASHBOARD_BROWSER_PASSWORD=...
```

不得读取、打印、手工复制、`source` 或提交该文件；凭据、cookie、ticket、prompt 输出和临时 JavaScript 不进入命令参数或发布总结。临时 Playwright 脚本使用 `0600` 并始终删除，浏览器、WebSocket、session 和远端临时目录执行 best-effort/bounded cleanup。

最终总结会明确区分：

- `rolled back before commit`
- `deployment committed and all smoke passed`
- `deployment committed but public smoke failed`

任一层失败均返回非零。连续性 prepare 在远端变更前失败时不会开始部署；pre-commit 事务失败会先恢复旧版本，再由 watcher 验证旧版本连续性，结果仍为 `rolled back before commit`。部署提交后的连续性或公开冒烟失败**不会自动回滚已提交版本**；此时先查看公开认证、ticket、WebSocket `1012`、Owner Worker drain、canonical session 和模型日志，决定修复后重试还是经人工判断发布上一个稳定 tag。runner 会输出可机器解析且已脱敏的 JSON（schema、状态、named checks、duration、cleanup、稳定 failure code/check）。`--dry-run` 只打印三层计划，不登录 Dashboard、不连接真实模型，也不修改远端。

从不含本连续性协议的旧版本首次升级时，正在运行的旧 frontend/Worker 无法被新 release 反向赋予 `1012` 和 exact Worker drain；该首次交接仍应按计划维护窗口处理，并显式传 `--initial-continuity-transition`。此 flag 只跳过旧版本无法满足的跨切换 watcher，candidate 启动后的独立公开真实 AI smoke 仍是提交后 gate。完成首次升级后不得继续使用该 flag；后续向前发布和不可变 tag 回滚都使用完整连续性路径。

## Release 保留与清理

发布成功后会删除本次上传的远端 tarball：`/opt/hermes/tmp/hermes-<tag>.tar.gz`。

旧 release 目录默认保留最近 5 个，同时永远保护本次部署版本、部署前后 `/opt/hermes/current` 指向的版本。可按需调整：

```bash
npm run deploy -- --tag v2026.7.4 --keep-releases 8
npm run deploy -- --tag v2026.7.4 --no-prune-releases
```

部署提交后还会自动回收 `/opt/hermes/runtimes/python` 中未被任何运行进程引用的旧 immutable runtime。当前候选 runtime 永远保留；进程的 executable、cwd、root、open fd、memory map 或 mount 引用命中时也会保留。无法完整检查 `/proc` 时清理会 fail closed，不会猜测删除。旧 tag 回滚如果需要已回收的 runtime，会按 lock 和 host 输入重新构建。紧急调查时可临时禁用：

```bash
npm run deploy -- --tag v2026.7.4 --no-prune-runtimes
```

## Nginx 单一登录层迁移

仓库只维护 `deploy/nginx/hermes-dashboard.conf` 这个 server-context snippet，不覆盖完整 vhost、站点根应用或 Certbot/TLS 配置。首次从旧的 Nginx Basic Auth/remember-cookie 结构迁移时，必须显式执行：

```bash
npm run deploy -- --tag v2026.7.4 --migrate-nginx-hermes
```

迁移流程先启动强制认证并启用 `--trust-proxy-headers` 的新 Hermes，从 loopback 验证 HTML 重定向和 API 401 均由 Hermes gate 提供；随后仅在旧 Hermes locations 唯一且完全匹配时备份 vhost、原子写入 include/snippet、执行 `nginx -t`，成功后 reload。未知、重复或部分迁移状态会 fail closed。后续普通发布只 reconcile 已存在的 include。

仅查看状态、不修改服务器：

```bash
ssh root@106.15.186.104 \
  'python3 /opt/hermes/current/deploy/nginx/manage_hermes_proxy.py status --vhost /etc/nginx/conf.d/abinllm.conf'
```

迁移前建议保存 `nginx -T` 和 vhost checksum。失败时优先恢复工具报告的 `abinllm.conf.hermes-backup-<timestamp>`，再执行 `nginx -t && systemctl reload nginx`。不要通过修改代码绕过强制认证、清空 local-user SQLite、轮换 durable-store secret、重跑 bootstrap、恢复 root 服务身份或放宽 owner-home ownership 检查来回滚。旧 `.htpasswd-hermes` 只可在 `nginx -T` 确认不再引用后人工清理。

## 发布后检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-dashboard && ! systemctl is-active --quiet hermes-gateway'
ssh root@106.15.186.104 'systemctl status --no-pager hermes-dashboard'
ssh root@106.15.186.104 'nginx -t && nginx -T 2>/dev/null | grep -n -A25 -B5 hermes-dashboard.conf'
ssh root@106.15.186.104 'journalctl -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

使用隐私窗口访问 `https://abinllm.xyz/hermes/`，应直接看到 Hermes 登录页而不是浏览器原生 Basic Auth challenge。用 active member 验证 sessions API、普通功能和认证 WebSocket；member 的账号管理 API 仍应为 403，admin 管理读取仍应成功。Dashboard、Owner Worker 和 `/opt/hermes/shared/.hermes/users/<owner-key>` 应使用同一个稳定 `hermes` UID/GID。现有 local-user DB、stable secret 和角色都保持不变。

发布脚本会执行 host sandbox preflight、systemd health、Hermes auth readiness、事务内确定性核心对话冒烟、Nginx validation，以及提交后的 authenticated 公开真实 AI 对话冒烟。APIYI 图像模型专项 smoke 仍不是必跑步骤；需要验证图像能力时再单独执行。
