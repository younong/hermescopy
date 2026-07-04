# Hermes 发布工具

这个目录集中管理 Hermes 发布到阿里云服务器的工具和生产部署配置。

## 文件

- `deploy.mjs` — Node.js 发布脚本。
- `docker-compose.prod.yml` — 生产环境 Docker Compose override。

详细部署说明见：`docs/deployment/alicloud.md`。

## 默认服务器

- Host: `106.15.186.104`
- User: `root`
- Remote root: `/opt/hermes`

## 核心发布规则

每次发布必须先确定 Git tag，然后只发布该 tag 中的代码。

- 新发布：`--create-tag <tag>`
- 重试/回滚：`--tag <existing-tag>`

工具使用 `git archive <tag>` 打包源码，避免把当前未提交工作区误传到服务器。

## 常用命令

查看帮助：

```bash
npm run deploy -- --help
```

预览新 tag 发布：

```bash
npm run deploy -- --create-tag v2026.7.3 --dry-run
```

创建 tag 并发布：

```bash
npm run deploy -- --create-tag v2026.7.3
```

部署已有 tag：

```bash
npm run deploy -- --tag v2026.7.3
```

回滚到旧 tag：

```bash
npm run deploy -- --tag v2026.7.2
```

## SSH 认证

推荐使用 SSH key：

```bash
npm run deploy -- --tag v2026.7.3 --identity-file ~/.ssh/hermes-alicloud
```

临时密码登录只允许使用本机环境变量，不要写入仓库：

```bash
export HERMES_DEPLOY_PASSWORD='***'
npm run deploy -- --tag v2026.7.3
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

## 远端目录结构

```text
/opt/hermes/releases/<tag>   # 每个 tag 一个 release 目录
/opt/hermes/current          # 当前线上版本 symlink
/opt/hermes/shared/.hermes   # 持久化数据
/opt/hermes/shared/.env      # 服务器本地环境变量，永不提交
```

## APIYI smoke test

发布后在服务器容器内测试 `gpt-image-2-medium` 和 `nano-banana-2`：

```bash
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec -T gateway python deploy/smoke-apiyi.py'
```

## 发布后检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps'
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs --tail=100 gateway'
```
