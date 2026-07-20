# 阿里云部署

Hermes 的阿里云生产部署使用 `deploy/` 目录里的 Node.js 工具。常规发布先确定一个 Git tag，再在本机基于该 tag 构建 web/ui-tui 产物，最后把源码 + 构建产物上传到服务器。无 tag 的受限例外是 `--ref <40-hex-commit-sha>`：它只接受已推送到 `origin` 的不可变完整 commit SHA，绝不会发布当前工作区或可移动分支。服务器只负责解包、创建按 `uv.lock + 架构` 标识的不可变 Python runtime、配置 authenticated Tool Executor 沙箱、切换 current symlink，并通过 systemd 直接运行 Hermes gateway 和 dashboard。

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
- Python 由 root-owned、只读的版本化 runtime 提供；部署脚本会把 uv-managed Python base、非 editable 的 locked 依赖和最小本地命令集一起打包到 `/opt/hermes/runtimes/python/<runtime-id>`，不会原地修改运行中的环境，也不依赖 sandbox 外部解释器路径
- Bubblewrap 必须安装为 `/usr/bin/bwrap`，并支持 `--bind-fd`、`--ro-bind-fd`、`--size`、`--uid`、`--gid`、`--cap-drop`、`--seccomp`、`--remount-ro` 和 `--info-fd`；不满足时发布在切换前 fail closed
- 内核必须允许非 root user namespace 和 seccomp filter
- 如果服务器没有 `uv`，部署脚本会用 `curl` 安装一次
- 常见编译/运行依赖按服务器实际错误补充，例如 `gcc`、`g++`、`make`、`cmake`、`python3-dev`、`python3-venv`、`ffmpeg`、`ripgrep`

Node.js/npm 只要求在本机可用。部署脚本会在从 Git tag 解出的临时源码目录中执行 `npm install --prefer-offline --no-audit`，并把 `web`、`ui-tui` 构建产物直接写入临时发布 artifact；不会把构建产物写回当前 checkout。

部署工具会自动创建这些目录：

```text
/opt/hermes/releases/<tag>       # 每个 tag 一个 release 目录，含本机预构建产物
/opt/hermes/current              # 指向当前 release 的 symlink
/opt/hermes/shared/.hermes       # Hermes 持久化数据 / HERMES_HOME
/opt/hermes/shared/.env          # 服务器本地环境变量，永不进 git
/opt/hermes/runtimes/python/<runtime-id> # root-owned 不可变 Python runtime
/opt/hermes/shared/hermes-service-runner.sh
/etc/hermes/executor-sandbox.json      # root-owned host sandbox policy
/etc/hermes/executor-x86_64.bpf        # root-owned seccomp cBPF artifact
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

先人工检查并提交所有要发布的代码；发布工具不会执行 `git add`、`commit` 或 `stash`。工作区（包括未跟踪文件）不干净时，新 tag 发布会直接拒绝：

```bash
git status --short
git commit  # 按实际改动选择并提交
npm run deploy -- --create-tag v2026.7.4
```

`--create-tag` 会先精确获取最新 `origin/main`，将当前发布分支 rebase 到该基线，再无 force 地把 prepared commit 推送到远端同名分支。分支推送成功后才创建 annotated tag，并通过 atomic push 同时守卫远端分支和发布唯一目标 tag；不会使用 `--tags` 推送其他本地 tag。这样，即使本地 `main` 干净但过期，也不能在同步最新 `origin/main` 前发布。

如果当前分支不是 `main`，工具会拒绝创建 tag。确实需要从其他具名分支发布时显式加：

```bash
npm run deploy -- --create-tag v2026.7.4-test --allow-non-main
```

`--allow-non-main` 仍会把当前分支 rebase 到最新 `origin/main`，再推送远端同名分支；它不允许 detached HEAD，也不会在 non-fast-forward 时 force push。遇到 rebase 冲突或远端分支并发更新时，工具会停止且不发布新 tag，需要人工检查并解决后重试。

新 tag 的 Git 准备和部署过程会：

1. 要求具名发布分支和干净工作区，并确认本地/远端目标 tag 都不存在。
2. Fetch 最新 `origin/main`，以 `--no-autostash` rebase 当前发布分支；冲突时 abort 并停止。
3. 无 force 地推送 prepared commit 到远端同名分支。
4. 在 prepared commit 创建 annotated tag，并 atomic push 该分支守卫和唯一目标 tag。
5. 校验本地 tag、远端 tag 和远端分支都指向同一 prepared commit；不一致时停止部署。

随后部署会：

1. 在本机基于 tag 解出干净源码。
2. 在本机安装 Node workspace 依赖并构建 web dashboard 和 TUI。
3. 把源码 + 构建产物打包上传到服务器临时目录，再解包到 `/opt/hermes/releases/<tag>`。
4. 成功解包后删除本次上传的 `/opt/hermes/tmp/hermes-<tag>.tar.gz`。
5. 在服务器上按 `uv.lock + 架构` 创建或复用不可变 runtime。
6. 校验 Bubblewrap 能力，安装 root-owned seccomp artifact 和 `/etc/hermes/executor-sandbox.json`，执行 policy preflight。
7. 只有 preflight 成功后才切换 `/opt/hermes/current` 并写入 systemd unit。
8. 以稳定的非 root `hermes` user/group 重启 gateway 和 dashboard；unit 的 `ExecStartPre` 会再次验证 sandbox policy。
9. 从 loopback 带生产代理头验证 Hermes 自己的登录 gate 已生效。
10. 在部署事务内以 `hermes` 用户和干净环境运行确定性核心对话冒烟；它只连接 loopback 假模型，不读取生产 `.env`，并覆盖附件、tool/approval、流、持久化和 cold resume。
11. 首次迁移时显式替换旧 Nginx 外层认证；后续发布只同步已托管 snippet，并在 `nginx -t` 成功后 reload；随后写入远端 deployment commit marker。
12. 远端提交成功后，本机通过 authenticated 公开 Dashboard、单次 WebSocket ticket、prefixed `/api/ws` 和真实模型运行第二层对话冒烟。
13. 服务、认证、确定性冒烟或 Nginx 检查失败时恢复部署前的 current symlink、runner、systemd units、sandbox policy 和 seccomp artifact，再重启旧版本；公开冒烟失败发生在 commit 之后，返回非零并报告验证失败，但不会自动回滚已提交版本。

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

`--dry-run` 不连接或修改服务器；它会打印将执行的远端脚本、migration/reconcile 模式，以及两层冒烟计划，但不会登录 Dashboard 或调用真实模型。实际迁移前另行保存 `nginx -T`、vhost checksum、systemd unit 和服务状态。

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

不要通过移除 `--require-auth`、删除 local-user store、轮换 stable secret、重新 bootstrap、恢复 root 服务身份或放宽 owner-home ownership 检查来处理故障。这些操作会破坏认证或 owner 隔离，而不是安全回滚。

`--tag` 模式会从 Git tag 生成源码包，不会上传当前工作区文件。构建产物随 release 上传；Python runtime 不可变，回滚不会被新版本依赖覆盖。

## Authenticated 本地工具范围

当前 bare-metal policy 只允许 `tool-none`：owner workspace 内的 `read_file`、`write_file`、`patch`、`search_files`、本地 skill 读取和无网络 terminal 可以在 Bubblewrap 中运行。terminal 提供部署时复制并绑定到 runtime 的最小命令集（`bash`、`sh`、`ls`、`pwd`、`printf`、`cat`、`grep`、`find`）以及 runtime Python，不会把宿主 `/usr` 整体暴露给 owner。每次调用使用独立 user/PID/IPC/mount/network namespace、non-root UID/GID、只读 release/runtime、私有 tmpfs、seccomp 和 post-spawn `/proc` attestation；executor 在 attestation 完成前阻塞在 start gate。

`tool-public` 与 `protected-target` 继续在 spawn 前明确拒绝：`authenticated network egress is not configured`。Authenticated 会话会按当前 executor policy 过滤模型可见工具，因此只允许 `tool-none` 的生产环境不会向模型展示必然失败的 browser/media 直连工具；该过滤不替代 spawn 前的最终拒绝。不要通过关闭 `--unshare-net` 或回退到进程全局 tool registry 来恢复联网工具。

Authenticated 会话中的 `web_search` 与 `web_extract` 使用独立的 one-shot web relay：Tool Executor 保持 `tool-none` 和私有 network namespace，只继承绑定 exact executor identity/invocation 的 socketpair descriptor；owner worker 校验绑定后，以 owner-scoped `config.yaml`、`.env` 和 `auth.json` 执行现有 web provider。API key/token 不进入 executor env、argv、mount 或 bootstrap。该 relay 不接受任意 tool name、provider、header 或通用 HTTP 请求，也不会给 browser、terminal、code execution、plugin 或 MCP 工具增加网络权限。

生产 immutable runtime 通过单独的 locked `ddgs` extra 提供无密钥的 `web_search` 基线；已配置的付费/自托管 provider 仍按既有优先级覆盖它。工具可见性按能力判断：DDGS 只支持 search，因此没有 Firecrawl、Tavily、Exa 或 Parallel 等 extract provider 时，`web_extract` 不会向模型暴露，也不会因为 DDGS 已安装而错误显示为可用。

`web.backend` / `web.search_backend` 选择 Hermes provider；`web.ddgs_backend` 只选择 DDGS 包内部的单个 text engine。默认 `auto` 会并发/轮询多个 engine，但当前阿里云网络无法稳定访问其中若干站点，可能等到 Hermes 的 30 秒总超时。每个 owner 的 `config.yaml` 应配置一个已验证可达的 engine：

```yaml
web:
  search_backend: "ddgs"
  ddgs_backend: "yandex"
```

该值是 owner-scoped 非敏感配置，只接受一个已知 engine；未知值和逗号分隔列表会 fail closed。查询仍由 exact one-shot owner-side relay 执行，executor 继续使用 `tool-none`、`--unshare-net` 和私有 network namespace；这不是给 browser、terminal 或其他工具放开直连网络。

诊断 policy：

```bash
sudo -u hermes env \
  PYTHONPATH=/opt/hermes/current \
  /opt/hermes/runtimes/python/<runtime-id>/bin/python -c \
  'from hermes_cli.owner_worker.host_sandbox import host_sandbox_deployment_policy; host_sandbox_deployment_policy()'
```

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

## 新 tag Git 失败处理

- 工作区不干净：人工选择要提交的文件并 commit，或自行 stash；发布工具不会自动处理。
- Rebase 冲突：工具会尝试 `git rebase --abort` 并停止。检查分支状态，人工解决与最新 `origin/main` 的冲突后重试。
- 分支 push 被拒绝：说明远端同名分支存在 non-fast-forward 或并发更新；fetch 并检查新增提交，禁止 force push 发布。
- Atomic tag push 失败：工具不会降级为无守卫的 tag-only push，也不会覆盖/删除远端 tag；未发布且由本次创建的本地 tag会安全清理。
- Tag 已验证发布但后续校验、构建或部署停止：tag 是不可变发布来源，不会自动删除。检查远端分支/tag 后，只有明确要部署该 commit 时才用 `npm run deploy -- --tag <tag>` 重试。

## Dry run

预览将执行的步骤，不 rebase、不 push、不创建本地 tag、不上传、不改服务器。新 tag dry-run 仍要求具名分支和干净工作区，并做远端只读检查；如果 rebase 后 commit 尚不可知，输出使用 `<post-rebase-commit>`：

```bash
npm run deploy -- --create-tag v2026.7.4 --dry-run
npm run deploy -- --tag v2026.7.4 --dry-run
npm run deploy -- --tag v2026.7.4 --keep-releases 3 --dry-run
```

## 自动冒烟、凭据与结果判定

公开真实 AI 冒烟需要在执行发布的本机安装 `playwright-cli`，并在仓库根目录准备 Git 忽略、当前用户所有、权限严格为 `0600` 的 `.env.local`：

```dotenv
HERMES_DASHBOARD_BROWSER_USERNAME=...
HERMES_DASHBOARD_BROWSER_PASSWORD=...
```

不要读取、打印、手工复制、`source` 或提交 `.env.local`。登录 helper 只在进程内加载凭据，用 mode-`0600` 临时 JavaScript 驱动浏览器，并对异常做脱敏；凭据、cookie、WebSocket ticket、模型回复均不写入 argv 或最终总结。公开 smoke 有总 timeout，且无论成功失败都会 best-effort close/delete session、关闭 WebSocket/Playwright 并删除临时脚本。事务内 smoke 使用独立临时 `HOME`/workspace，完成后由 runner 和部署 EXIT trap 双重清理。

两个 smoke runner 输出独立的 machine-readable JSON，部署脚本再输出 aggregate release summary。只接受以下结果语义：

- `rolled back before commit`：远端事务未提交；旧部署已由 trap 恢复。排查 deterministic smoke 的 failure `code/check` 后重试。
- `deployment committed and all smoke passed`：部署与发布验证均成功。
- `deployment committed but public smoke failed`：线上版本已提交，但公开路径/真实模型验证失败；命令返回非零，且不会自动回滚。立即检查 auth、ticket、WebSocket、Owner Worker、模型配置和日志，再人工决定修复重试或发布上一稳定 tag。

`--dry-run` 只展示两层 smoke 命令和 `planned` 总结，不读取本机凭据、不打开浏览器、不调用模型。

## 服务器状态检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'systemctl status --no-pager hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'nginx -t && nginx -T 2>/dev/null | grep -n -A25 -B5 hermes-dashboard.conf'
ssh root@106.15.186.104 'journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

迁移后使用隐私窗口访问 `https://abinllm.xyz/hermes/`：浏览器应直接显示 Hermes 登录页，不再弹出原生 Basic Auth。用一个 active member 验证 dashboard、WebSocket/PTY、sessions API 和普通 owner 功能，确认账号管理仍返回 403；再用独立 admin 会话确认管理读取可用。验证 logout、过期/篡改 cookie 和非 Hermes 站点未回归。

AI 执行上述生产浏览器验收时，先运行 `python3 scripts/playwright_dashboard_login.py`；它从 Git 忽略的本机 `.env.local` 读取凭据，并保留已认证的 `hermes-validation` 会话。后续统一使用 `playwright-cli -s=hermes-validation ...`，结束后运行 `playwright-cli -s=hermes-validation close`。不得读取、输出或提交 `.env.local` 内容；member/admin 分别验收时使用各自独立的本机会话和凭据。

APIYI 图像模型专项 smoke 不是发布脚本必跑步骤；需要验证图像能力时再单独执行。发布脚本已自动执行 host sandbox preflight、systemd 状态、Hermes auth readiness、确定性核心对话 smoke、Nginx validation 和 authenticated 公开真实文本模型 smoke；更宽的生产验收仍应使用真实 authenticated 用户验证跨 owner 隔离、`web_search` 经 relay 成功，以及 browser 等 direct-egress 工具继续按 policy 隐藏并在直接调用时拒绝。

## 常用参数

```text
--host <host>            默认 106.15.186.104
--user <user>            默认 root
--port <port>            默认 22
--identity-file <path>   SSH 私钥路径
--remote-root <path>     默认 /opt/hermes
--force                  已废弃并拒绝；不可变 release 不会被替换
--keep-releases <n>      成功部署后保留最近 n 个 release，默认 5
--no-prune-releases      不自动清理旧 release 目录
--allow-dirty            允许工作区有改动时部署已有 tag
--dashboard-public-url   trusted loopback proxy 的公开 URL
--migrate-nginx-hermes   显式迁移已识别的旧 Hermes Nginx auth block
--dry-run                只预览，不修改本机或服务器
```
