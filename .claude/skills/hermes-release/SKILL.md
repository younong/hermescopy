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
- Remote root: `/opt/hermes`
- 发布工具：`npm run deploy -- ...`
- 详细文档：`docs/deployment/alicloud.md`
- Node.js 发布脚本：`deploy/deploy.mjs`

## 核心规则

1. **必须先有 tag，再发布**
   - 新版本发布：使用 `--create-tag <tag>`。
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

```bash
npm run deploy -- --tag v2026.7.3 --identity-file ~/.ssh/hermes-alicloud
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

- 发布后至少验证 `gpt-image-2-medium` 和 `nano-banana-2` 各一次：

```bash
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec -T gateway python deploy/smoke-apiyi.py'
```

## 发布前检查

运行或确认：

```bash
git status --short
git branch --show-current
git tag --list | tail -n 20
node --check deploy/deploy.mjs
docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml config
```

注意：创建新 tag 时，发布工具默认要求当前分支为 `main` 且工作区干净。确实要从非 main 分支发布时，必须显式使用 `--allow-non-main`。

## 发布后验证

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps'
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs --tail=100 gateway'
```

Dashboard 默认只监听服务器本机 `127.0.0.1`。访问方式：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后打开 `http://localhost:9119`。

## 失败处理

- tag 创建失败：检查 tag 是否已存在、工作区是否干净。
- push 失败：检查 Git remote 权限。
- SSH 失败：检查 SSH key、密码、端口、安全组。
- Docker build/up 失败：查看远端 `docker compose logs`。
- 发布错版本：用 `npm run deploy -- --tag <previous-tag>` 回滚。

## 输出要求

完成后向用户说明：

- 发布/部署的 tag。
- 是否真实部署，还是 dry-run。
- 服务器路径 `/opt/hermes/releases/<tag>` 和 `/opt/hermes/current`。
- 验证命令结果。
- 如果失败，说明失败在哪一步，不要声称发布成功。
