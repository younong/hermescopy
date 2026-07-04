# 阿里云部署

Hermes 的生产部署使用 `deploy/` 目录里的 Node.js 工具。发布规则是：先确定一个 Git tag，再把该 tag 对应的源码包上传到服务器，由服务器在对应 release 目录中构建并启动 Docker Compose。

服务器默认配置：

- Host: `106.15.186.104`
- User: `root`
- Remote root: `/opt/hermes`

> 不要把服务器密码、API key 或 `.env` 文件提交到仓库。建议尽快改用 SSH key 登录；如果临时使用密码，放在本机环境变量 `HERMES_DEPLOY_PASSWORD` 中，并安装 `sshpass`。

## 首次服务器准备

服务器需要安装 Docker 和 Docker Compose 插件。部署工具会自动创建这些目录：

```text
/opt/hermes/releases/<tag>   # 每个 tag 一个 release 目录
/opt/hermes/current          # 指向当前 release 的 symlink
/opt/hermes/shared/.hermes   # Hermes 持久化数据
/opt/hermes/shared/.env      # 服务器本地环境变量，永不进 git
```

如需配置运行时环境变量，在服务器上编辑：

```bash
ssh root@106.15.186.104
vim /opt/hermes/shared/.env
```

## 推荐：使用 SSH key

```bash
ssh-keygen -t ed25519 -f ~/.ssh/hermes-alicloud
ssh-copy-id -i ~/.ssh/hermes-alicloud.pub root@106.15.186.104
```

部署时指定 key：

```bash
npm run deploy -- --tag v2026.7.3 --identity-file ~/.ssh/hermes-alicloud
```

## 临时：使用密码登录

本工具不会读取仓库里的密码文件，也不会打印密码。若必须短期使用密码：

```bash
export HERMES_DEPLOY_PASSWORD='不要写进文档或仓库'
npm run deploy -- --tag v2026.7.3
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

发布后在服务器容器内做 APIYI smoke test：

```bash
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml exec -T gateway python deploy/smoke-apiyi.py'
```

该脚本只输出模型名、成功/失败、图片路径或错误，不会打印 APIYI 令牌。

## 发布并部署新 tag

从当前 `main` 创建 tag、推送 tag，然后部署：

```bash
npm run deploy -- --create-tag v2026.7.3
```

如果当前分支不是 `main`，工具会拒绝创建 tag。确实需要从其他分支发布时显式加：

```bash
npm run deploy -- --create-tag v2026.7.3-test --allow-non-main
```

## 部署已有 tag / 回滚

部署已有 tag：

```bash
npm run deploy -- --tag v2026.7.3
```

回滚就是重新部署上一个 tag：

```bash
npm run deploy -- --tag v2026.7.2
```

`--tag` 模式会从 Git tag 生成源码包，不会上传当前工作区文件。

## Dry run

预览将执行的步骤，不创建本地 tag、不上传、不改服务器：

```bash
npm run deploy -- --create-tag v2026.7.3 --dry-run
npm run deploy -- --tag v2026.7.3 --dry-run
```

## 服务器状态检查

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml ps'
ssh root@106.15.186.104 'cd /opt/hermes/current && docker compose -f docker-compose.yml -f deploy/docker-compose.prod.yml logs --tail=100 gateway'
```

Dashboard 默认只绑定服务器本机 `127.0.0.1`。需要访问时使用 SSH tunnel：

```bash
ssh -L 9119:localhost:9119 root@106.15.186.104
```

然后在本机打开 `http://localhost:9119`。

## 常用参数

```text
--host <host>            默认 106.15.186.104
--user <user>            默认 root
--port <port>            默认 22
--identity-file <path>   SSH 私钥路径
--remote-root <path>     默认 /opt/hermes
--force                  删除并重建服务器上同名 release 目录
--allow-dirty            允许工作区有改动时部署已有 tag
--dry-run                只预览，不修改本机或服务器
```
