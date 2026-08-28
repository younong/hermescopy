# 阿里云部署

Hermes 的阿里云生产部署使用 `deploy/` 目录里的 Node.js 工具，生产 artifact 始终来自不可变 Git tag。常规流程是开发分支先通过 PR 合入 `main`，再从与最新 `origin/main` 完全一致的本地 `main` 创建 tag；不支持按工作区、分支名或 commit SHA 直接发布。工具在本机基于 tag 构建 `web` 和 locked PptxGenJS 产物，上传生产运行源码与预构建产物；服务器负责解包、创建不可变 runtime、配置 authenticated Tool Executor 沙箱、切换 current symlink，并通过唯一的 `hermes-dashboard.service` 运行服务。

服务器默认配置：

- Host: `106.15.186.104`
- User: `root`
- Remote root: `/opt/hermes`

> 不要把服务器密码、API key 或 `.env` 文件提交到仓库。建议使用 SSH key；如果临时使用密码，只放在本机环境变量 `HERMES_DEPLOY_PASSWORD` 中。内置 SSH/SFTP transport 不需要 `sshpass`。

## 服务器准备

当前生产路径为裸机/systemd，不需要在服务器上构建 Hermes Docker 镜像，也不在服务器上运行 npm install/build。服务器需要：

- systemd，并以 unified cgroup v2 启动；`hermes-dashboard.service` 必须委派 `cpu`、`memory`、`pids` controller
- cgroup v2 必须提供 `memory.swap.max` 与 `cgroup.freeze`；有 `cgroup.kill` 时优先使用，否则只允许 freeze + 递归 SIGKILL + `populated 0` 的验证清理
- tar / gzip
- `sha256sum`
- Python 由 root-owned、只读的版本化 runtime 提供；部署脚本会把 uv-managed Python base、非 editable 的 locked 依赖和最小本地命令集一起打包到 `/opt/hermes/runtimes/python/<runtime-id>`，不会原地修改运行中的环境，也不依赖 sandbox 外部解释器路径
- Bubblewrap 必须安装为 `/usr/bin/bwrap`，并支持 `--bind-fd`、`--ro-bind-fd`、`--size`、`--uid`、`--gid`、`--cap-drop`、`--seccomp`、`--remount-ro` 和 `--info-fd`；不满足时发布在切换前 fail closed
- 内核必须允许非 root user namespace 和 seccomp filter
- 如果服务器没有 `uv`，部署脚本会用 `curl` 安装一次
- PowerPoint 的 LibreOffice/font 前置依赖由 `deploy/runtime/alicloud3-powerpoint-packages.json` 绑定 Alibaba Cloud Linux 3 x86_64 和精确 NEVRA。普通部署只校验；首次补齐时显式使用 `--provision-powerpoint-deps`，只做 manifest 内的 additive `dnf install`
- 常见编译/运行依赖按服务器实际错误补充，例如 `gcc`、`g++`、`make`、`cmake`、`python3-dev`、`python3-venv`、`ffmpeg`、`ripgrep`

Node.js/npm 只要求在本机可用。部署脚本会在从 Git tag 解出的临时源码目录中执行 workspace 构建，并在 `deploy/powerpoint-runtime` 执行 `npm ci --omit=dev --ignore-scripts --no-audit`；不会把构建产物写回当前 checkout。服务器不运行 npm。PptxGenJS payload、Node、MarkItDown、LibreOffice 和字体都进入 root-owned immutable runtime，authenticated executor 只读挂载这些快照。生产 `uv sync` 使用阿里云 PyPI 镜像下载 `uv.lock` 已锁定的 Python wheel，避免官方 PyPI CDN 在国内链路上的大文件下载瓶颈；锁文件和校验仍决定最终版本与内容。

首次补齐 PowerPoint 前置包：

```bash
npm run deploy -- --tag <tag> --provision-powerpoint-deps --dry-run
npm run deploy -- --tag <tag> --provision-powerpoint-deps
```

Dry-run 只披露 provisioning、cgroup resource smoke 和 PowerPoint runtime smoke 计划，不安装包。真实部署在 Dashboard 启动并建立 delegated cgroup v2 subtree 后，先执行只读资源前置检查与真实 kernel resource smoke，再通过同一 cgroup 管理层和 authenticated Bubblewrap executor：用 PptxGenJS 生成两页 deck、用 MarkItDown 校验 marker 顺序，再通过 skill wrapper 执行一次 LibreOffice PDF 转换。任一已启用资源检查失败都属于 pre-commit 失败。若主机尚未迁移到 cgroup v2，部署不会自动修改启动参数或重启：Dashboard/聊天继续启动，但 authenticated Tool 保持 fail closed，PowerPoint smoke 明确标记为未执行。事务回滚不会删除已 additive 安装的 RPM，但旧 release/runtime 不会引用它们；后续发布可省略 provisioning flag，并继续严格核对 manifest。

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

Systemd 服务与 delegated cgroup：

```text
/etc/systemd/system/hermes-dashboard.service
/sys/fs/cgroup/system.slice/hermes-dashboard.service/control-plane
/sys/fs/cgroup/system.slice/hermes-dashboard.service/authenticated-owners
```

旧 `hermes-gateway.service` 仅在升级事务中被识别并退休；候选版本不会安装、启用或启动它。

Dashboard unit 使用 `Delegate=cpu memory pids`、CPU/Memory/Tasks accounting、`LimitNOFILE=65536:1048576` 和 `KillMode=mixed`。可信 bootstrap 先把 Dashboard 进程移入 `control-plane/`，再启用 unit root controller 并建立空的 `authenticated-owners/`；应用只管理该空 subtree。生产专用首版预算为：全局 1500m CPU/2304 MiB/512 PID/最多 5 worker 和 2 executor；单 owner 1000m/896 MiB/128 PID/1 executor；单 invocation 750m/512 MiB/swap 0/64 PID/64 FD/120 秒/200,000 字节。它们只针对当前 2 vCPU、约 3.48 GiB 主机，不是跨部署默认值。

Dashboard 只监听服务器本机 `127.0.0.1:9119`，公开入口为：

```text
https://abinllm.xyz/hermes/
```

Nginx 只终止 TLS，并代理 `/hermes` 下的 HTTP 和 WebSocket；Hermes durable local-user provider 是唯一认证层。现有 active member 可直接使用自己的 user 凭据登录，不需要先通过 admin 账号。admin 权限仍只用于 local-user 账号管理。

SSH tunnel 只作为紧急诊断方式：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后在本机打开 `http://localhost:9119`。服务始终启用 cookie/session 认证，所以 tunnel 访问仍需 Hermes user 登录。

## Authority 损坏恢复

部署在停止服务和切换 `current` 之前，会用候选 release 只读运行：

```bash
hermes dashboard authority status --json
```

如果 authority 无法读取、存在 `authority.sqlite3.recovery-required.json` marker，或数据库 metadata 中 `recovery_required=1`，部署会停止，且不会删除 marker、修复数据库或回滚 authority。重启无法恢复 authority；必须执行离线 recovery fencing。

先停止 Dashboard（以及可能持有 authority 生命周期锁的 owner 进程），然后运行 `hermes dashboard authority status --json` 区分恢复证据：

- **有 marker/incident ID 的损坏事件**：不要删除或改名 marker。运行 `hermes dashboard authority preserve --json` 确认 forensic copy，再从 marker 记录的 incident ID 与 SHA-256 固定来源执行恢复。仅当来源精确匹配 `SQLit` 后 byte 5 的 TLS record 损坏时才使用专用模式：
  ```bash
  hermes dashboard authority recover \
    --incident <incident-id> \
    --source <untouched-source.sqlite3> \
    --sha256 <sha256> \
    --repair-tls-offset-5 \
    --json
  ```
- **只有 metadata `recovery_required=1`、没有 incident ID 的 replay-continuity 事件**：现有 `preserve`/`recover` 命令只接受 marker-backed corruption，不能修复该状态。保持 Dashboard 离线和 authority/keyring 原件不变，保存两者的 forensic copy，并升级给 authority recovery 维护者执行专用 signer/DB fencing；不得手工把 metadata 改回 `0`、生成新 keyring 或把 marker-only 命令当作成功恢复。

恢复完成后启动 Dashboard，并验证新登录、WS ticket、Owner Worker 对话和冷 Session Reader resume。marker-backed 恢复命令不修改来源文件；它在同目录 staging DB 上验证完整性和 schema，通过 SQLite backup 重建，推进 recovery generation，撤销旧 scope/ticket/bootstrap/Worker/Reader authority，并在 DB 与 browser-ticket keyring witness 均持久化一致后才清除 marker。禁止直接恢复陈旧 DB、手工删除 marker 或跳过 recovery fencing。

## 本机发布端与 SSH host key

发布工具可从原生 Windows、macOS 或 Linux 发起，但远端部署目标仍是 Linux/systemd。首次连接前必须通过独立可信渠道核对服务器 host fingerprint，并将确认过的 key 写入本机 OpenSSH `known_hosts`；未知、变化、revoked 或无法解析的 host key 都会被拒绝。

获得连接授权后，可先运行只读检查。它只验证 host key、认证和远端 Bash，不检查 Git、不构建、不上传、不创建远端目录：

```bash
npm run deploy -- --check-connection
```

## 推荐：使用 SSH key

Key 模式使用系统 OpenSSH，可通过 agent 或私钥文件认证：

```bash
ssh-keygen -t ed25519 -f ~/.ssh/hermes-alicloud
ssh-copy-id -i ~/.ssh/hermes-alicloud.pub root@106.15.186.104
npm run deploy -- --tag v2026.7.4 --identity-file ~/.ssh/hermes-alicloud --dry-run
```

## 临时：使用密码登录

密码模式使用内置 SSH/SFTP transport，不需要 `sshpass`，也不会把密码放入子进程参数、日志或临时文件。若必须短期使用密码：

Bash：

```bash
export HERMES_DEPLOY_PASSWORD='不要写进文档或仓库'
npm run deploy -- --check-connection
npm run deploy -- --tag v2026.7.4 --dry-run
unset HERMES_DEPLOY_PASSWORD
```

PowerShell：

```powershell
$env:HERMES_DEPLOY_PASSWORD = '不要写进文档或仓库'
npm run deploy -- --check-connection
npm run deploy -- --tag v2026.7.4 --dry-run
Remove-Item Env:HERMES_DEPLOY_PASSWORD
```

推荐流程为：经授权执行 `--check-connection` → 执行发布命令的 `--dry-run` → 核对结果并获得明确批准 → 执行非 dry-run 发布。

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

常规发布必须先把开发分支通过 PR 合入 `main`，再同步本地 `main`。发布工具不会执行 `git add`、`commit`、`stash`，也不会 rebase 或 push `main`：

```bash
git status --short
git switch main
git pull --ff-only origin main
npm run deploy -- --create-tag v2026.8.26 --dry-run
npm run deploy -- --create-tag v2026.8.26
```

`--create-tag` fetch 后要求 `HEAD`、本地 `main` 和最新 `origin/main` 完全一致；本地 main 落后、领先或分叉都会停止。通过后只创建并 push 唯一目标 annotated tag，不会 push main、使用 force/lease、`+` refspec 或 `--tags`。

### 应急 non-main 发布

`--allow-non-main` 仅用于用户明确认定的紧急事故，不是 PR 未合入、main 不同步或时间紧迫时的快捷方式：

```bash
npm run deploy -- --create-tag <emergency-tag> --allow-non-main --dry-run
npm run deploy -- --create-tag <emergency-tag> --allow-non-main
```

应急路径保留严格保护：具名分支 rebase 到最新 `origin/main`，用绑定 observed SHA 的完整 ref `--force-with-lease` 更新远端同名分支，并通过 prepared commit 精确 lease atomic push 分支守卫和唯一 tag；detached HEAD、rebase 冲突、tag 冲突或远端并发移动都会停止。

通过 AI 执行时，每条含 `--allow-non-main` 的完整命令都必须针对当前分支、目标 tag 和 dry-run/真实模式单独获得用户明确批准。一般性的“发布”“继续”或此前的发布批准不能沿用；命令变化后必须重新请示。

随后部署会：

1. 在本机基于 tag 解出干净源码。
2. 在本机安装 Node workspace 依赖并构建 Web Dashboard。
3. 把生产运行源码 + 本机预构建产物打包上传到服务器临时目录；归档省略 `tests/`、`website/`、`.github/` 和 `docs/`，再解包到 `/opt/hermes/releases/<tag>`。
4. 成功解包后删除本次上传的 `/opt/hermes/tmp/hermes-<tag>.tar.gz`。
5. 在服务器上按 locked Python/PowerPoint 输入与架构创建或复用不可变 runtime。
6. 校验 Bubblewrap 能力，安装 root-owned seccomp artifact 和 `/etc/hermes/executor-sandbox.json`，执行 policy preflight。
7. 将 candidate runner、Dashboard unit、policy 和 seccomp 留在事务 staging；停止旧 Dashboard，由其关闭浏览器桥并排空、吊销 Owner Workers。若旧 standalone Gateway unit 存在，则仅在迁移中停止、禁用并移除。确认旧服务退出后，才原子安装 staged artifacts 和切换 `/opt/hermes/current`，再以稳定的非 root `hermes` user/group 只启动 Dashboard。
8. 对 delegated subtree 执行 `check-executor-cgroup-host.py --expected-soft-nofile 65536 --expected-hard-nofile 1048576 --require-mandatory`，并分别读取 `mandatoryReady` 与 `resourceReady`。Dashboard 的运行时 soft/hard `LimitNOFILE` 不匹配时部署必定停止；已迁移主机还必须通过 controller、accounting、swap/freeze 和 topology 检查，未迁移主机则明确保持 Tool fail closed，部署脚本不会修改 grub 或重启。
9. 资源层 ready 时执行 `smoke-executor-resources.py` 的真实 kernel limit/event/cleanup 检查，再通过同一 cgroup manager 启动真实 authenticated executor：在高编号 FD 压力下验证 Bubblewrap 可启动、executor 内 `RLIMIT_NOFILE` 与 policy 一致，并完成 PptxGenJS、MarkItDown、单次 LibreOffice PowerPoint runtime 以及确定性的 loopback owner-relay 网络 smoke。
10. 从 loopback 带生产代理头验证 Hermes 自己的登录 gate 已生效。
11. 在部署事务内以 `hermes` 用户、`env -i` 和独立临时 `HOME`/`TMPDIR`/`HERMES_HOME` 运行 Authority concurrency smoke，覆盖并发首次初始化、browser/Worker exact-once、Worker lease/change feed、checkpoint、integrity、schema 与 recovery 状态；它只访问合成 Authority，绝不读取生产 Authority 或 `.env`。
12. 运行隔离合成 Session Reader 性能 smoke，再运行确定性核心对话冒烟；后者只连接 loopback 假模型，不读取生产 `.env`，并覆盖附件、tool/approval、流、持久化和 cold resume。
13. 首次迁移时显式替换旧 Nginx 外层认证；后续发布只同步已托管 snippet，并在 `nginx -t` 成功后 reload；随后写入远端 deployment commit marker。
14. 远端事务开始前，本机先建立 authenticated 真实模型连续性会话并保持连接；candidate 成功或 pre-commit 回滚完成后，要求观察到 `1012`、使用新单次 ticket 重连、恢复同一 canonical session lineage、继续对话并 cold resume/delete。远端提交成功后，再通过公开 Dashboard、prefixed `/api/ws` 和真实模型运行独立公开冒烟。
15. Authority/Reader/resource/PowerPoint/服务/认证/确定性冒烟或 Nginx 检查失败时恢复部署前的 current symlink、runner、systemd units、sandbox policy 和 seccomp artifact，再启动旧版本并完成连续性验证；提交后的连续性/公开冒烟失败返回非零并报告验证失败，但不会自动回滚已提交版本。

## 首次移除 Nginx 外层认证

旧生产配置在 `/hermes/` 上使用 Nginx `auth_basic`/remember-cookie，再由 Hermes 执行一次 durable local-user 登录，因而产生两次登录。仓库中的 `deploy/nginx/hermes-dashboard.conf` 将 Nginx 限定为 TLS、path-prefix 和 WebSocket proxy，并显式关闭继承的 `auth_basic`/`auth_request`；身份认证全部交给 Hermes。

首次迁移必须单独批准并显式加参数：

```bash
npm run deploy -- --tag v2026.7.4 --migrate-nginx-hermes
```

部署工具会先启动 loopback 上的 Hermes，配置 `HERMES_DASHBOARD_PUBLIC_URL=https://abinllm.xyz/hermes`，并启用 `--trust-proxy-headers`。认证始终开启；只有内部检查确认未登录 HTML 返回登录重定向、受保护 API 返回 401 后，迁移 helper 才会：

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

不要通过修改代码绕过强制认证、删除 local-user store、轮换 stable secret、重新 bootstrap、恢复 root 服务身份或放宽 owner-home ownership 检查来处理故障。这些操作会破坏认证或 Owner 隔离，而不是安全回滚。

`--tag` 模式只用于重试或回滚 origin 上已经发布的 tag。本地 tag 与 origin 同名 tag 必须指向同一 commit，否则工具会拒绝；源码包仍从该 tag 生成，不会上传当前工作区文件。Python runtime 不可变，回滚不会被新版本依赖覆盖。

## cgroup v2 维护窗口迁移与回滚

当前生产探测基线（2026-07-23）为 legacy cgroup v1、systemd 239、Linux 5.10，Docker 26 使用 cgroupfs v1；因此迁移必须作为独立维护窗口，不属于普通 Hermes 发布。操作前保存以下只读证据：默认启动项及其完整参数、`mount`/`/proc/cgroups`、`systemctl show hermes-dashboard`、`docker info`、`docker ps` 和 SearXNG health。具体 bootloader 命令必须按主机实际配置选择，不得由 `deploy.mjs` 猜测或自动执行。

维护窗口顺序：

1. 保存当前默认内核启动项和完整 kernel arguments，确认可通过阿里云控制台进入串口/VNC 或救援模式。
2. 在 bootloader 中为**下一次启动**加入 unified cgroup v2 参数；不要删除旧启动项。对于该 systemd 版本，先在生产等价主机验证 `systemd.unified_cgroup_hierarchy=1` 的兼容性。
3. 重启后先验证 `/sys/fs/cgroup` 为单一 `cgroup2` mount，`cpu memory pids` controller 可用；再验证 Docker/containerd 的 cgroup version/driver、现有 SearXNG 容器 running/healthy、端口与查询路径。
4. 安装含 delegation unit 的 Hermes release，运行：

   ```bash
   python3 /opt/hermes/current/deploy/check-executor-cgroup-host.py \
     --managed-root /sys/fs/cgroup/system.slice/hermes-dashboard.service/authenticated-owners \
     --service hermes-dashboard.service \
     --expected-soft-nofile 65536 --expected-hard-nofile 1048576 \
     --require-ready
   python3 /opt/hermes/current/deploy/smoke-executor-resources.py \
     --managed-root /sys/fs/cgroup/system.slice/hermes-dashboard.service/authenticated-owners
   ```

5. 验证 PowerPoint smoke、A/B noisy-neighbor、小任务可用性、Dashboard/Owner Worker，以及 service 重启后没有 populated stale invocation，再记录 `cpu.stat`、`memory.events`、`pids.events` 的去敏数值。

任一 unified-v2、Docker/containerd、SearXNG 或 Hermes gate 失败时回滚：从控制台选择保存的旧启动项（或恢复保存的 kernel arguments），重启回 cgroup v1，复核 Docker/SearXNG 和旧 Hermes tag。不要通过删除 Tool admission、关闭 controller 检查、改回 root 服务、放宽 cgroup 路径或让应用自动写 bootloader 来“修复”。应用版本回滚与主机 cgroup 回滚是两个独立动作；都必须记录结果。

## 微信 iLink Connector

`channel_connectors.weixin_ilink` 默认启用，但只有 authenticated Dashboard、deployment inference/image policy、cgroup v2 resource manager 和两套版本化 keyring 全部 ready 后才接受二维码 enrollment。前置条件缺失时 Dashboard 继续工作，Chat GUI 会显示微信入口及泛化的不可用说明；Connector 不启动 poller/dispatcher，也不会绕过 Owner Worker admission。

显式关闭：

```yaml
channel_connectors:
  weixin_ilink:
    enabled: false
```

启用需要在权限受限的 `/opt/hermes/shared/.env` 中配置两套**彼此独立**的随机 32-byte keyring：

```text
HERMES_ILINK_LOOKUP_KEYS_JSON={"1":"<base64-32-bytes>"}
HERMES_ILINK_ENCRYPTION_KEYS_JSON={"1":"<different-base64-32-bytes>"}
```

真实值必须在服务器本地通过 opaque generation 生成和写入；不要在命令参数、终端输出、对话、ticket、日志或仓库中读取、打印或复制，不要让两套 keyring 共用材料。轮换时递增 active version，并保留所有仍被 channel registry 引用的旧版本。配置后通过正常服务路径重启 Dashboard，再以已认证 Owner 检查 `/api/auth/me` 的 `feature_status.weixin_ilink_connect` 为 `enabled=true`、`ready=true`，最后用指定测试微信验证二维码、Owner 绑定、文本私聊、回复和 replay protection。

中央 Connector 会按 2,000 字符上限顺序分片回复，并为每个 chunk 使用稳定幂等 ID、持久化已确认进度。临时网络、HTTP 429/5xx 和 provider 明确返回的频率限制默认最多尝试 8 次，以 2 秒为基数指数退避并封顶 300 秒；`ret=-2` 本身是歧义 code，只有明确的频率限制 message 才会重试，空/`unknown error` 或 context-token 失效信号会记为 `stale_context`，其他 `-2` 会记为 `provider_rejected`。失效 session/context、其他 HTTP 4xx、未知 provider 拒绝或重试耗尽会把 outbound/inbound 标记为 `failed`，停止热重试并解除同 binding 后续消息的顺序阻塞；`stale_context` 需要新的 inbound 刷新 context，持续 session 问题则需要重新 enrollment。可在 `channel_connectors.weixin_ilink` 下调整 `outbound_retry_seconds`、`outbound_retry_max_seconds`、`outbound_max_attempts` 和 `outbound_chunk_delay_seconds`。

## Authenticated 本地工具范围

当前 bare-metal policy 只允许 `tool-none`，并要求三层 cgroup v2 治理（全局 authenticated-owner 池、单 owner 聚合、单 invocation 叶节点）可用；资源层缺失时 Dashboard/聊天继续可用，但 authenticated tools 拒绝 dispatch。owner workspace 内的 `read_file`、`write_file`、`patch`、`search_files`、本地 skill 读取和无网络 terminal 可以在 Bubblewrap 中运行。terminal 提供部署时复制并绑定到 runtime 的最小命令集（`bash`、`sh`、`ls`、`pwd`、`printf`、`cat`、`grep`、`find`）以及 runtime Python，不会把宿主 `/usr` 整体暴露给 owner。每次调用使用独立 user/PID/IPC/mount/network namespace、non-root UID/GID、只读 release/runtime、私有 tmpfs、seccomp 和 post-spawn `/proc` attestation；executor 在 attestation 完成前阻塞在 start gate。

`tool-public` 与 `protected-target` 继续在 spawn 前明确拒绝：`authenticated network egress is not configured`。Authenticated 会话会按当前 executor policy 过滤模型可见工具，因此只允许 `tool-none` 的生产环境不会向模型展示必然失败的 browser/media 直连工具；该过滤不替代 spawn 前的最终拒绝。不要通过关闭 `--unshare-net` 或回退到进程全局 tool registry 来恢复联网工具。

Authenticated 会话中的 `web_search` 与 `web_extract` 使用独立的 one-shot web relay：Tool Executor 保持 `tool-none` 和私有 network namespace，只继承绑定 exact executor identity/invocation 的 socketpair descriptor；owner worker 校验绑定后，以 owner-scoped `config.yaml`、`.env` 和 `auth.json` 执行现有 web provider。API key/token 不进入 executor env、argv、mount 或 bootstrap。该 relay 不接受任意 tool name、provider、header 或通用 HTTP 请求，也不会给 browser、terminal、code execution、plugin 或 MCP 工具增加网络权限。部署事务内的 authenticated runtime smoke 使用无凭据的 loopback HTTP endpoint，通过真实 Bubblewrap/executor/socket framing 验证 owner relay 可执行网络 I/O；它不访问公网 provider，且 post-spawn attestation 仍要求 executor 的 network namespace 隔离。

生产 immutable runtime 通过单独的 locked `ddgs` extra 提供无密钥的 `web_search` 基线；已配置的付费/自托管 provider 仍按既有优先级覆盖它。工具可见性按能力判断：DDGS 只支持 search，因此没有 Firecrawl、Tavily、Exa 或 Parallel 等 extract provider 时，`web_extract` 不会向模型暴露，也不会因为 DDGS 已安装而错误显示为可用。

`web.backend` / `web.search_backend` 选择 Hermes provider；`web.ddgs_backend` 只选择 DDGS 包内部的单个 text engine。默认 `auto` 会并发/轮询多个 engine，但当前阿里云网络无法稳定访问其中若干站点，可能等到 Hermes 的 30 秒总超时。每个 owner 的 `config.yaml` 应配置一个已验证可达的 engine：

```yaml
web:
  search_backend: "ddgs"
  ddgs_backend: "yandex"
```

该值是 owner-scoped 非敏感配置，只接受一个已知 engine；未知值和逗号分隔列表会 fail closed。查询仍由 exact one-shot owner-side relay 执行，executor 继续使用 `tool-none`、`--unshare-net` 和私有 network namespace；这不是给 browser、terminal 或其他工具放开直连网络。

诊断 policy 与 cgroup capability：

```bash
sudo -u hermes env \
  PYTHONPATH=/opt/hermes/current \
  /opt/hermes/runtimes/python/<runtime-id>/bin/python -c \
  'from hermes_cli.owner_worker.host_sandbox import host_sandbox_deployment_policy; host_sandbox_deployment_policy()'
python3 /opt/hermes/current/deploy/check-executor-cgroup-host.py \
  --managed-root /sys/fs/cgroup/system.slice/hermes-dashboard.service/authenticated-owners \
  --service hermes-dashboard.service \
  --expected-soft-nofile 65536 --expected-hard-nofile 1048576
```

cgroup 不限制普通 workspace 文件字节数或 inode。本阶段不会把现有 `disk_bytes`/`disk_inodes` quota 声明为硬保障；当前根盘 ext4 尚未启用 per-owner project quota，且生产探测时已使用约 79%。上线必须保留磁盘容量/inode 告警。第二阶段应把 owner workspace 移入支持 project quota 的独立 ext4/XFS 文件系统，并为每个 owner 同时配置字节和 inode 硬配额。

## 历史 commit release

旧版本可能留下 `/opt/hermes/releases/commit-<sha>`。升级不会主动删除当前或刚替换的历史 release，但它们不再能作为新发布或回滚来源；未受保护的目录之后由既有保留策略自然清理。回滚统一使用 origin 上已发布的稳定 tag。

`scripts/release.py --publish` 负责 GitHub Release 和分发 artifact，不是阿里云部署入口，不能用于绕过 main/tag 约束。

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

部署事务提交后，工具还会自动回收 `/opt/hermes/runtimes/python` 中未被运行进程引用的旧 immutable Python runtime。候选 runtime 无条件保留；其他 runtime 只要仍被进程的 executable、cwd、root、open fd、memory map 或 mount 引用就会保留。检查 `/proc` 时遇到权限或读取错误会 fail closed，保留相关 runtime；因此清理不会影响当前服务或尚未退出的旧进程。旧 tag 回滚如果对应 runtime 已回收，会根据 release lock、Node/PowerPoint host 输入和 sandbox profile 重新构建，回滚耗时会增加。

需要保留全部 runtime 进行调查时可显式禁用：

```bash
npm run deploy -- --tag v2026.7.4 --no-prune-runtimes
```

## 新 tag Git 失败处理

- 工作区不干净：人工选择要提交的文件并 commit，或自行 stash；发布工具不会自动处理。
- Rebase 冲突：工具会尝试 `git rebase --abort` 并停止。检查分支状态，人工解决与最新 `origin/main` 的冲突后重试。
- 精确 lease push 被拒绝：说明远端同名分支在发布快照后发生了并发更新；fetch 并检查新增提交后重新 rebase/retry。禁止改用无守卫的 `--force`、裸/隐式 lease 或 `+` refspec。
- Atomic tag push 失败：工具不会降级为无守卫的 tag-only push，也不会覆盖/删除远端 tag；若 prepared commit lease 失效，atomic transaction 会整体拒绝；未发布且由本次创建的本地 tag 会安全清理。
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

部署提交前还会运行确定性的 Authority concurrency 与 Session Reader 性能 smoke。Authority smoke 只在独立临时目录中并发操作合成 Authority，验证 exact-once、Worker fencing/change feed、checkpoint/integrity/schema/recovery 和完整清理，绝不读取生产 Authority、共享 `.env` 或凭据。Reader smoke 只使用候选 release、隔离的合成 3,000 会话历史和本机 UDS，不读取生产状态或凭据，也不访问公网；固定检查 SQL 数量、压缩链查询计划、本地与真实 Reader 冷/热延迟、连接池及并发资源上限。标准由 `hermes_cli/session_reader/performance_contract.py` 统一定义。任一回归或清理失败都会在 commit 前终止并恢复旧部署。公开 smoke 中的 Reader list/messages 延迟会写入总结，但仅作线上观测，不作为阈值，避免把 TLS、认证、网络和共享主机抖动变成 post-commit 性能误报。

deterministic、continuity 和 public smoke runner 输出独立的 machine-readable JSON，部署脚本再输出 aggregate release summary。continuity JSON 只报告 phase、named checks、`1012` close code 和 cleanup 布尔值，不包含 ticket、cookie、owner/session ID、prompt 或模型内容。只接受以下结果语义：

- `rolled back before commit`：远端事务未提交；旧部署已由 trap 恢复。排查 Session Reader performance 或 deterministic smoke 的 failure `code/check` 后重试。
- `deployment committed and all smoke passed`：部署与发布验证均成功。
- `deployment committed but public smoke failed`：线上版本已提交，但公开路径/真实模型验证失败；命令返回非零，且不会自动回滚。立即检查 auth、ticket、WebSocket、Owner Worker、模型配置和日志，再人工决定修复重试或发布上一稳定 tag。

`--dry-run` 只展示各层 smoke 命令和 `planned` 总结，不运行性能基准、不读取本机凭据、不打开浏览器、不调用模型。

从 pre-continuity release 首次安装此协议时，已经运行的旧 frontend/Worker 无法被 candidate 反向赋予 `1012`、canonical browser pointer 或 exact Worker drain；首次交接按计划维护窗口处理并显式传 `--initial-continuity-transition`。该 flag 只豁免旧版本无法完成的跨切换 watcher，candidate 启动后的公开真实 AI smoke 仍必须通过。该版本成为基线后不得继续使用该 flag；后续向前发布和部署旧 immutable tag 的回滚都使用相同 drain-before-switch 与 continuity watcher 路径。

## 服务器状态检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-dashboard && ! systemctl is-active --quiet hermes-gateway'
ssh root@106.15.186.104 'systemctl status --no-pager hermes-dashboard'
ssh root@106.15.186.104 'nginx -t && nginx -T 2>/dev/null | grep -n -A25 -B5 hermes-dashboard.conf'
ssh root@106.15.186.104 'journalctl -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

迁移后使用隐私窗口访问 `https://abinllm.xyz/hermes/`：浏览器应直接显示 Hermes 登录页，不再弹出原生 Basic Auth。用一个 active member 验证 Dashboard、authenticated WebSocket、sessions API 和普通 Owner 功能，确认账号管理仍返回 403；再用独立 admin 会话确认管理读取可用。验证 logout、过期/篡改 cookie 和非 Hermes 站点未回归。

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
--no-prune-runtimes      不自动清理未被进程引用的旧 Python runtime
--allow-dirty            允许工作区有改动时部署已有 tag
--dashboard-public-url   trusted loopback proxy 的公开 URL
--migrate-nginx-hermes   显式迁移已识别的旧 Hermes Nginx auth block
--dry-run                只预览，不修改本机或服务器
```
