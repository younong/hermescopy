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
   - `--create-tag` 要求具名分支和干净工作区，自动 rebase 最新 `origin/main`、无 force 推送当前分支，并 atomic push 唯一目标 tag；不会自动 commit/stash，也不会推送其他本地 tag。
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

注意：创建新 tag 时，发布工具默认要求当前分支为 `main` 且工作区（含未跟踪文件）干净。工具会 fetch 并 rebase 最新 `origin/main`，无 force 推送当前分支，然后精确发布目标 tag。确实要从非 main 分支发布时，必须显式使用 `--allow-non-main`；该路径同样 rebase `origin/main`，且 detached HEAD 始终拒绝。

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
- branch/tag push 失败：检查 Git remote 权限及远端并发更新；不得 force push。Atomic push 不会降级为无守卫的 tag-only push。
- tag 已验证发布但部署中止：检查远端 refs；明确要部署该不可变 commit 时再使用 `--tag <tag>` 重试，不要覆盖或删除远端 tag。
- SSH 失败：检查 SSH key、密码、端口、安全组。
- Python 依赖/bootstrap 失败：查看部署输出中的 `uv`/系统依赖错误，按服务器缺失依赖补齐。
- systemd 服务启动失败：查看 `systemctl status --no-pager hermes-gateway hermes-dashboard` 和 `journalctl -u hermes-gateway -u hermes-dashboard --since "10 min ago" --no-pager -n 200`。
- 发布错版本：用 `npm run deploy -- --tag <previous-tag>` 回滚。

## 输出要求

完成后向用户说明：

- 发布/部署的 tag。
- 是否真实部署，还是 dry-run。
- 服务器路径 `/opt/hermes/releases/<tag>` 和 `/opt/hermes/current`。
- 验证命令结果。
- 如果失败，说明失败在哪一步，不要声称发布成功。
