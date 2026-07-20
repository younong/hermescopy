---
name: hermes-release
description: Hermes 项目发布到阿里云服务器的标准流程；当用户要求发布、部署、打 tag、回滚、查看线上状态或操作 106.15.186.104 上的 Hermes 服务时使用。
allowed-tools:
  - Read
  - Bash
---

# Hermes 发布 Skill

本 skill 用于把 Hermes 按 Git tag 发布到阿里云服务器。

默认服务器：

- Host: `106.15.186.104`
- SSH user: `root`
- SSH identity file: `~/.ssh/hermes_apiyi_ed25519`
- Remote root: `/opt/hermes`
- 发布工具：`npm run deploy -- ...`
- 详细文档：`docs/deployment/alicloud.md`
- Node.js 发布脚本：`deploy/deploy.mjs`

## 核心规则

1. **必须先有 tag，再发布**
   - 新版本发布：先人工提交代码，再使用 `--create-tag <tag>`。
   - `--create-tag` 要求具名分支和干净工作区，自动 rebase 最新 `origin/main`，再以绑定 rebase 前远端分支精确 SHA 的 `--force-with-lease=<完整 ref>:<observed SHA>` 更新原 PR/源分支，并以 prepared commit 精确 lease atomic push 唯一目标 tag；不会自动 commit/stash，也不会推送其他本地 tag。
   - 重试或回滚：使用 `--tag <existing-tag>`。
   - 不允许发布未打 tag 的当前工作区。

2. **不要把密码或 secret 写入仓库**
   - 不要编辑代码、文档、配置去保存真实服务器密码。
   - 优先建议 SSH key。
   - 临时密码登录只能使用本机环境变量 `HERMES_DEPLOY_PASSWORD`，且不要打印其值。

3. **真实部署前先 dry-run**
   - 对发布命令先执行 `--dry-run`。
   - 检查 tag、host、remote root、SSH/SCP 命令是否符合预期。

4. **真实部署是外部变更**
   - 在执行非 dry-run 发布前，确认用户确实要发布到服务器。
   - 如果用户已经明确说“现在发布/直接发布/执行部署”，可以继续。

5. **两层冒烟是发布结果的一部分**
   - 事务内确定性对话 smoke 在 Nginx/commit 前运行；失败必须报告 `rolled back before commit`。
   - 远端 commit 后自动运行 authenticated 公开真实 AI smoke；失败必须报告 `deployment committed but public smoke failed`、返回非零，且不得自动回滚已提交版本。
   - `--dry-run` 只确认两层 smoke 均为 `planned`，不得登录或调用模型。

## 常用命令

### 查看帮助

```bash
npm run deploy -- --help
```

### 新建 tag 并发布

```bash
npm run deploy -- --create-tag v2026.7.3 --dry-run
npm run deploy -- --create-tag v2026.7.3
```

### 发布已有 tag

```bash
npm run deploy -- --tag v2026.7.3 --dry-run
npm run deploy -- --tag v2026.7.3
```

### 回滚

回滚就是发布上一个稳定 tag：

```bash
npm run deploy -- --tag <previous-tag> --dry-run
npm run deploy -- --tag <previous-tag>
```

### 使用 SSH key

默认使用本机私钥文件 `~/.ssh/hermes_apiyi_ed25519`（只记录文件路径，不记录私钥内容）：

```bash
npm run deploy -- --tag v2026.7.3 --identity-file ~/.ssh/hermes_apiyi_ed25519
```

### 临时密码登录

不要输出密码值。只提示用户在本会话中执行：

```bash
export HERMES_DEPLOY_PASSWORD='***'
npm run deploy -- --tag v2026.7.3
```

如果缺少 `sshpass`，系统 SSH 可能会要求交互输入密码。

## APIYI 图像模型发布检查

如果本次发布涉及 APIYI 图像模型：

- 确认代码/文档/日志中没有真实 `APIYI_API_KEY`。
- 只在服务器本地 `/opt/hermes/shared/.env` 配置：

```bash
APIYI_API_KEY=***
```

- 可选 endpoint 覆盖：

```bash
APIYI_OPENAI_BASE_URL=https://api.apiyi.com/v1
APIYI_GEMINI_BASE_URL=https://api.apiyi.com/v1beta
```

- 发布后如需真实调用模型，再额外验证 `gpt-image-2-medium` 和 `nano-banana-2`。这不是默认发布成功判定；默认收尾只检查 systemd 服务状态：

```bash
ssh root@106.15.186.104 'set -a; [ ! -f /opt/hermes/shared/.env ] || . /opt/hermes/shared/.env; set +a; cd /opt/hermes/current && /opt/hermes/shared/venv/bin/python deploy/smoke-apiyi.py'
```

## 发布前检查

运行或确认：

```bash
git status --short
git branch --show-current
git tag --list | tail -n 20
node --check deploy/deploy.mjs
node --check ui-tui/scripts/build.mjs
npm run deploy -- --help
```

注意：创建新 tag 时，发布工具默认要求当前分支为 `main` 且工作区（含未跟踪文件）干净。工具会 fetch 并 rebase 最新 `origin/main`，用完整分支 ref 和 rebase 前 observed SHA 的精确 lease 更新原远端分支，然后精确发布目标 tag。确实要从非 main 分支发布时，必须显式使用 `--allow-non-main`；该路径同样 rebase `origin/main`，且 detached HEAD 始终拒绝。远端分支并发变化会使 lease 失效并停止发布。

## 自动两层冒烟

发布命令自动执行：

1. 远端 deterministic smoke：以非 root `hermes`、`env -i` 和隔离临时目录运行 loopback 假模型核心对话，覆盖 attachment、terminal、approval deny、stream、persistence/cold resume/continuation/delete。它不读取 `/opt/hermes/shared/.env`，也不允许非 loopback 网络。失败仍在 deployment commit 前，现有 trap 自动恢复旧版本。
2. 本机 public smoke：远端 commit 后用 `scripts/smoke_dashboard_conversation.py` 登录公开 Dashboard，申请单次 WebSocket ticket，经 prefixed `/api/ws` 和 Owner Worker 调用真实模型，再 cold resume 并删除 session。

第二层要求本机 `playwright-cli` 和仓库根目录 Git 忽略、`0600` 的 `.env.local`，其中配置 `HERMES_DASHBOARD_BROWSER_USERNAME`、`HERMES_DASHBOARD_BROWSER_PASSWORD`。绝不读取、打印、手工复制、`source` 或提交该文件；不要让凭据、cookie、ticket 或模型回复进入命令参数和总结。

始终读取最终 aggregate summary 和两个 runner 的脱敏 JSON。若 public smoke 失败，线上部署已经 committed；先查 auth/WebSocket/Owner Worker/model 日志，人工决定修复重试或发布上一稳定 tag，禁止脚本自动回滚。

## 发布后验证

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'systemctl status --no-pager hermes-gateway hermes-dashboard'
ssh root@106.15.186.104 'journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200'
```

Dashboard 默认只监听服务器本机 `127.0.0.1`。访问方式：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后打开 `http://localhost:9119`。

## 失败处理

- tag 创建失败：检查 tag 是否已存在（本地或远端）、工作区是否干净。
- rebase 失败：工具会尝试 abort 并停止；人工检查与最新 `origin/main` 的冲突，解决并提交后重试。
- branch/tag push 失败：检查 Git remote 权限及远端并发更新；精确 lease 失效时 fetch、检查并重新 rebase/retry。除该发布路径的完整 ref + observed SHA lease 外，仍禁止无守卫的 `--force`、裸/隐式 lease 和 `+` refspec；Atomic push 不会降级为无守卫的 tag-only push。
- tag 已验证发布但部署中止：检查远端 refs；明确要部署该不可变 commit 时再使用 `--tag <tag>` 重试，不要覆盖或删除远端 tag。
- SSH 失败：检查 SSH key、密码、端口、安全组。
- Python 依赖/bootstrap 失败：查看部署输出中的 `uv`/系统依赖错误，按服务器缺失依赖补齐。
- systemd 服务启动失败：查看 `systemctl status --no-pager hermes-gateway hermes-dashboard` 和 `journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200`。
- `rolled back before commit`：查看 deterministic smoke 的稳定 failure `code/check`；旧版本应已恢复，不要声称新版本发布成功。
- `deployment committed but public smoke failed`：命令非零但新版本已在线；不要声称全部成功，也不要自动回滚。检查公开 auth/ticket/WebSocket/Owner Worker/model 后人工决策。
- 发布错版本：用 `npm run deploy -- --tag <previous-tag>` 回滚。

## 输出要求

完成后向用户说明：

- 发布/部署的 tag。
- 是否真实部署，还是 dry-run。
- 服务器路径 `/opt/hermes/releases/<tag>` 和 `/opt/hermes/current`。
- deterministic smoke 和 public smoke 的各自状态、稳定 failure `code/check`（如有）及 cleanup 结果；不得包含 assistant 内容或认证材料。
- aggregate outcome 必须原样归类为 `rolled back before commit`、`deployment committed and all smoke passed` 或 `deployment committed but public smoke failed`；dry-run 标记两层均为 `planned`。
- 验证命令结果。
- 如果失败，说明失败在哪一步，不要声称发布成功；public smoke 失败时同时明确部署已经 committed 且未自动回滚。
