# iLink Bot 多用户扫码注册与 Owner 隔离方案

> 更新时间：2026-07-24
>
> 状态：目标架构与分阶段实施方案（尚未实现）
>
> 相关文档：[Hermes 多用户隔离方案](2026-07-07-multi-user-isolation-plan.md)、[Relay ↔ Connector Contract](../relay-connector-contract.md)、[Session 生命周期](../session-lifecycle.md)

## 0. 执行摘要

在没有企业微信账号的前提下，Hermes 可以使用腾讯微信 iLink Bot（微信 ClawBot）实现普通微信用户扫码授权后聊天。当前仓库已经具备二维码登录、长轮询收消息、`context_token` 管理以及文本/媒体回复等基础 iLink 能力，但现有实现面向单个 Gateway/Owner，不能直接作为多租户自动注册服务。

本方案的产品语义是：

```text
每个新用户获得一个短期、专属的 iLink 授权二维码
  -> 用户用普通微信扫码并确认
  -> Hermes 自动创建或恢复 canonical user
  -> canonical user 绑定不可变 owner_key
  -> 用户进入自己的微信 ClawBot 会话
  -> 首条消息被路由到该用户的隔离 Owner Worker
```

需要特别区分：iLink 的 `get_bot_qrcode` 二维码是一次性登录/绑定授权二维码，**不是**一个可永久印刷并供任意访客重复扫码的公开 Bot 联系方式。公开海报可以指向 Hermes 注册页，由注册页为每次注册动态生成专属二维码。

目标架构采用：

1. **iLink Enrollment Service**：生成并轮询一次性二维码，幂等注册用户；
2. **Channel Identity Registry**：可信地维护 iLink 身份、canonical user 和 Owner 绑定；
3. **Central iLink Connector**：集中维护每个已绑定账号的 `getupdates`、游标、上下文令牌和发送状态；
4. **Channel Dispatcher**：把入站消息路由到 exact Owner Worker，并复用现有 `tui_gateway` JSON-RPC；
5. **隔离 Owner Worker**：继续作为 Session、Memory、Skills、Workspace、凭据和 Agent runtime 的用户隔离边界。

正式多用户部署仍须遵守[多用户隔离方案](2026-07-07-multi-user-isolation-plan.md)规定的发布基线：Control Plane 可信派生 Owner，生产环境中的每个 Owner Worker 使用获批准的 per-owner 执行沙箱。iLink 绑定不能替代该隔离边界。

---

## 1. 需求、约束与非目标

### 1.1 用户体验

目标流程：

```text
用户打开 Hermes 注册页
  -> 页面动态展示专属 iLink 二维码
  -> 用户使用普通微信扫码并确认
  -> Hermes 后台自动注册并绑定 Owner
  -> 用户在微信 ClawBot 会话发送第一条消息
  -> Hermes 在同一会话回复
```

用户不需要：

- 企业微信或企业账号；
- 公众号关注；
- 预先注册 Hermes 账号；
- 输入配对码；
- 回到 Dashboard 手工确认 Owner 绑定。

### 1.2 必须满足的系统约束

- 注册、重复扫码、授权结果重试必须幂等；
- 每个 canonical user 只能解析到一个不可变 Owner；
- 二维码、浏览器或入站消息携带的 `owner_key` 一律不可信；
- 每个 Owner 的 Session、Memory、Skills、配置、Workspace 和 Agent runtime 相互隔离；
- 回复必须使用对应 iLink account/peer 最近有效的 `context_token`；
- iLink 账号凭据不得进入日志、浏览器响应、二维码状态响应或 Agent 上下文；
- 同一绑定的消息串行处理，不同 Owner 可以并行；
- Worker generation 变化后必须重新获取 exact-worker lease 并恢复会话；
- 平台限流、Token 生命周期和商业化多租户许可在扩大上线前必须验证。

### 1.3 非目标

第一阶段不承诺：

- 一张固定 iLink 二维码供无限用户重复扫码；
- 微信群聊；
- 主动营销或无入站上下文的任意消息触达；
- 跨 Owner 共享 Memory 或 Workspace；
- 规避微信平台限流、授权或商业使用规则；
- 用 iLink 身份代替操作系统级 Owner 隔离。

---

## 2. 当前仓库能力与缺口

### 2.1 已有 iLink 能力

`gateway/platforms/weixin.py` 已实现以下协议路径：

| 能力 | 当前实现 |
| --- | --- |
| API 基地址 | `ILINK_BASE_URL = https://ilinkai.weixin.qq.com` |
| 获取二维码 | `EP_GET_BOT_QR` / `qr_login()` |
| 轮询二维码状态 | `EP_GET_QR_STATUS` / `qr_login()` |
| 长轮询收消息 | `EP_GET_UPDATES` / `_get_updates()` / `_poll_loop()` |
| 发送消息 | `EP_SEND_MESSAGE` / `_send_message()` / `send()` |
| 输入状态 | `EP_SEND_TYPING` / `_send_typing()` |
| 媒体上传 | `EP_GET_UPLOAD_URL` 及媒体上传、加密逻辑 |
| Poll 游标 | `_load_sync_buf()` / `_save_sync_buf()` |
| 会话上下文 | 按 account + peer 保存并读取 `context_token` |
| 入站身份 | `_process_message()` 读取 `from_user_id` 并构造 `SessionSource` |

确认二维码后，当前 `qr_login()` 会读取并持久化类似以下授权结果：

```text
bot_token
ilink_bot_id
ilink_user_id
baseurl
```

这些能力应被提取为可复用的 iLink transport，而不是重新实现另一套协议客户端。

### 2.2 当前模型的缺口

现有 `WeixinAdapter` 的生命周期属于一个正在运行的 Gateway/Owner。直接用于多租户会产生以下问题：

1. **没有公开注册事务**：二维码确认后不会创建 canonical user 和不可变 Owner 绑定；
2. **Owner 派生不稳定**：若未来新增 Dashboard 登录方式，按新 provider 派生可能创建第二个 Owner；
3. **长轮询依赖 Worker 常驻**：Owner Worker 停止后，该用户的 `getupdates` 也停止，无法由新微信消息唤醒 Worker；
4. **缺少集中幂等与队列**：消息去重、授权重放、失败恢复和投递状态不具备 Control Plane 级持久化语义；
5. **当前 pairing 不等于注册**：`gateway/pairing.py` 表示管理员批准某个 Gateway 用户，不表达外部身份对 canonical user/Owner 的所有权；
6. **现有 Adapter 假设单 Owner**：不能仅凭任意入站 `from_user_id` 自动创建 Owner，否则攻击者可以制造无限账号和跨租户路由风险。

---

## 3. 产品入口与二维码语义

### 3.1 推荐入口

公开二维码应指向 Hermes 注册页，而不是直接长期复用某个 iLink 二维码：

```text
https://hermes.example.com/join?scene=poster-202607
```

页面加载后调用 Enrollment API，为本次注册创建短期二维码：

```http
POST /api/public/ilink/enrollments
GET  /api/public/ilink/enrollments/{attempt_id}
```

`scene` 只用于来源归因和风控，不能决定 canonical user、Owner、worker 或文件路径。

### 3.2 iLink 二维码状态

Enrollment Service 调用：

```http
GET https://ilinkai.weixin.qq.com/ilink/bot/get_bot_qrcode?bot_type=3
GET https://ilinkai.weixin.qq.com/ilink/bot/get_qrcode_status?qrcode=<token>
```

典型状态机：

```text
waiting -> scanned -> confirmed
    \                    /
     +------ expired ---+
```

每个二维码 attempt 必须：

- 使用不可枚举的内部 `attempt_id`；
- 具有服务端截止时间；
- 限制每 IP、设备和时间窗口的创建次数；
- 授权完成后停止轮询并原子消费；
- 不向浏览器返回 `bot_token`、`ilink_bot_id` 的内部路由信息或 `owner_key`；
- 过期后创建新 attempt，而不是复用已确认二维码。

---

## 4. 目标组件架构

```text
┌────────────────────── 用户注册入口 ──────────────────────┐
│                                                         │
│  公开入口二维码 -> Hermes 注册页                         │
│                         │                               │
│                         ▼                               │
│             POST /api/public/ilink/enrollments          │
│                         │                               │
│             展示本次注册专属 iLink QR                    │
└─────────────────────────┬───────────────────────────────┘
                          │ 微信扫码并确认
                          ▼
┌────────────────────── 微信 iLink 服务 ───────────────────┐
│ get_bot_qrcode / get_qrcode_status                      │
│ getupdates / sendmessage / sendtyping / getuploadurl    │
└─────────────────────────┬───────────────────────────────┘
                          │ confirmed / updates
                          ▼
┌──────────────────── Hermes Control Plane ────────────────┐
│                                                         │
│  ┌───────────────────────────────────────────────────┐  │
│  │ iLink Enrollment Service                          │  │
│  │ attempt -> confirmed identity + encrypted account │  │
│  └───────────────────────┬───────────────────────────┘  │
│                          ▼                              │
│  ┌───────────────────────────────────────────────────┐  │
│  │ Channel Identity Registry                         │  │
│  │ ilink identity -> canonical_user_id -> owner_key  │  │
│  └───────────────────────┬───────────────────────────┘  │
│                          ▼                              │
│  ┌───────────────────────────────────────────────────┐  │
│  │ Central iLink Connector                           │  │
│  │ account pollers / cursor / context token / send   │  │
│  └───────────────────────┬───────────────────────────┘  │
│                          ▼                              │
│  ┌───────────────────────────────────────────────────┐  │
│  │ Durable Queue + Channel Dispatcher                │  │
│  │ binding -> trusted OwnerContext                   │  │
│  │ supervisor.get_or_start(owner)                    │  │
│  │ exact-worker UDS/OWP1 Gateway client              │  │
│  └───────────────────────┬───────────────────────────┘  │
└──────────────────────────┼──────────────────────────────┘
                           │ session.create / resume
                           │ prompt.submit
                           ▼
┌──────────────────── Isolated Owner Worker ───────────────┐
│ HERMES_HOME=<global>/users/<owner_key>                   │
│                                                         │
│ owner-local state.db / Memory / Skills / config         │
│ credentials / workspaces / live runtime                 │
│                                                         │
│ OwnerWorkerGatewayRuntime -> tui_gateway -> AIAgent      │
└──────────────────────────┬──────────────────────────────┘
                           │ assistant result/events
                           ▼
┌────────────────────── Outbound Queue ────────────────────┐
│ owner result -> binding/account/peer -> context_token    │
│ -> iLink sendmessage -> delivery state                  │
└──────────────────────────┬──────────────────────────────┘
                           ▼
                    微信 ClawBot 会话
```

### 4.1 信任边界

```text
不可信：浏览器、二维码参数、消息文本、媒体、from_user_id 的业务用途声明
可信：  iLink TLS 响应 + 已绑定账号凭据、Control Plane registry、authority lease
隔离：  exact Owner Worker + per-owner sandbox
```

`from_user_id` 是平台观察到的身份字段，但只有在它与已确认 Enrollment 创建的绑定匹配时才获得 Owner 路由权。任何请求都不得通过前端或消息参数直接指定 `owner_key`。

---

## 5. 注册与首条消息时序

```text
用户       注册页       Enrollment      iLink       Identity DB      Connector      Supervisor     Worker
 │           │              │             │              │               │              │           │
 │─打开页面─▶│              │             │              │               │              │           │
 │           │─create──────▶│             │              │               │              │           │
 │           │              │─get QR─────▶│              │               │              │           │
 │           │◀─QR + attempt│◀────────────│              │               │              │           │
 │─微信扫码───────────────────────────────▶│              │               │              │           │
 │─确认授权───────────────────────────────▶│              │               │              │           │
 │           │              │─poll status▶│              │               │              │           │
 │           │              │◀─confirmed──│              │               │              │           │
 │           │              │─transactional upsert──────▶│               │              │           │
 │           │              │  canonical user / owner    │               │              │           │
 │           │              │  encrypted iLink account   │               │              │           │
 │           │◀─confirmed───│             │              │               │              │           │
 │           │              │             │              │─activate─────▶│              │           │
 │           │              │             │              │               │─getupdates──▶iLink       │
 │─微信消息───────────────────────────────▶│              │               │◀─message─────│           │
 │           │              │             │              │               │─dedup/queue─▶│           │
 │           │              │             │              │               │─get_or_start▶│           │
 │           │              │             │              │               │              │─start────▶│
 │           │              │             │              │               │─create/resume───────────▶│
 │           │              │             │              │               │─prompt.submit───────────▶│
 │           │              │             │              │               │◀─assistant result────────│
 │           │              │             │              │               │─sendmessage─▶iLink       │
 │◀────────────────────────────── Hermes reply ─────────────────────────────────────────────────────│
```

注册事务必须先提交 identity/owner/account 绑定，再激活 Poller。这样 Poller 收到消息时一定能解析可信 Owner；不得先收消息再根据任意 sender 临时创建 Owner。

---

## 6. 身份、Owner 与账号绑定模型

### 6.1 规范身份链

```text
iLink 授权结果
  -> external_identity
  -> canonical_user_id
  -> immutable owner_key
  -> OwnerContext
  -> exact Owner Worker
```

建议引入独立的 Control Plane 注册库，例如：

```text
<global HERMES_HOME>/control-plane/channel_identities.sqlite3
```

它不能放在某个 Owner 的 `state.db` 中，也不应复用 worker authority 数据库或 pairing 文件。

### 6.2 建议表结构

#### `canonical_users`

```text
id                  internal random user id
status              active / suspended / deleted
created_at
updated_at
```

#### `owner_bindings`

```text
canonical_user_id
owner_key
runtime_status
created_at
```

约束：

```text
UNIQUE(canonical_user_id)
UNIQUE(owner_key)
```

#### `external_identities`

```text
id
canonical_user_id
provider                    # weixin_ilink
subject_lookup_hash         # HMAC of normalized ilink_user_id
subject_ciphertext          # optional encrypted value when operationally required
status
created_at
last_seen_at
```

产品若规定“一个微信 iLink 用户只能拥有一个 Hermes Owner”，则启用：

```text
UNIQUE(provider, subject_lookup_hash)
```

该约束依赖 `ilink_user_id` 的稳定性和重复授权语义，必须在平台 PoC 中验证后锁定。

#### `ilink_accounts`

```text
id
external_identity_id
ilink_bot_id
bot_token_ciphertext
baseurl
status                      # active / reauth_required / revoked / suspended
credential_version
created_at
last_connected_at
```

约束：

```text
UNIQUE(ilink_bot_id)
```

#### `channel_bindings`

```text
binding_id
external_identity_id
ilink_account_id
expected_peer_lookup_hash
status
created_at
```

`expected_peer_lookup_hash` 用于验证入站 `from_user_id` 确实属于授权用户。若平台实测表明 `ilink_user_id` 与入站 peer ID 不是同一语义，应记录并验证正确的稳定字段，而不是放宽为接受任意 sender。

#### `channel_sessions`

```text
binding_id
owner_key
stored_session_id
last_worker_generation
created_at
updated_at
```

#### `inbound_messages`

```text
account_id
provider_message_id
binding_id
payload_ciphertext_or_reference
status                      # queued / processing / completed / failed
attempt_count
received_at
```

约束：

```text
UNIQUE(account_id, provider_message_id)
```

若平台消息缺少稳定 message ID，应使用已验证的复合幂等键；仅按消息文本 hash 去重不能作为最终保证，因为用户可能合法重复发送相同内容。

#### `outbound_messages`

```text
id
binding_id
inbound_message_id
payload
status                      # pending / sending / delivered / failed / expired
attempt_count
provider_message_id
next_attempt_at
created_at
```

### 6.3 凭据保护

推荐：

```text
subject_lookup_hash = HMAC(server_lookup_key, normalized_subject)
bot_token_ciphertext = AEAD_encrypt(versioned_server_key, bot_token)
```

要求：

- lookup key 与 encryption key 分离并支持版本轮换；
- 密钥属于 Control Plane/Connector 的 control-only secret storage；
- `bot_token` 不写入普通日志、错误详情、SessionSource、Agent prompt 或客户端响应；
- 数据库备份必须按敏感凭据等级保护；
- Connector 解密凭据时只授予对应 account poller，不能批量注入 Owner Worker 环境；
- 授权撤销或重绑后立即停用旧 credential version。

### 6.4 Dashboard 账号认领

未来用户通过邮箱、Passkey 或其他 provider 登录 Dashboard 时，新增身份必须链接到现有 `canonical_user_id`：

```text
weixin_ilink identity ─┐
email/passkey identity ├─> canonical_user_id -> existing owner_key
other provider ────────┘
```

不得直接按新的 `(auth_provider, tenant_id, owner_user_id)` 再派生一个 Owner。账号认领需要短期、单次、只存 hash 的 claim token，并在 Control Plane 中完成可信身份合并。

---

## 7. Central iLink Connector

### 7.1 为什么不能只依赖 Owner Worker 内的 Adapter

若每个 Owner Worker 内运行当前 `WeixinAdapter`：

```text
Worker 停止 -> getupdates 停止 -> 收不到新消息 -> 无法由消息唤醒 Worker
```

小规模 PoC 可以让 Worker 常驻，但正式架构应分离：

```text
Connector 生命周期：长期维护 iLink 收发
Owner Worker 生命周期：仅在 Agent turn 需要时启动/保活/回收
```

### 7.2 Poller 模型

每个活跃 iLink account 至少维护：

```text
account_id
credential_version
encrypted bot token reference
baseurl
get_updates_buf
poll lease owner
last successful poll
consecutive failures
backoff deadline
```

多 Connector 实例部署时，需要 account-level lease/fencing，保证同一账号只有一个有效 poller 推进游标。接管规则必须避免两个实例同时消费并重复提交消息。

### 7.3 `context_token` 不变量

当前适配器已按 account + peer 管理 `context_token`。集中化后，持久化键至少为：

```text
(ilink_account_id, peer_id_lookup_hash)
```

必须满足：

- 只使用同一 account/peer 最近有效的 token；
- 不跨账号、Owner 或 peer 复用；
- token 更新和 inbound message 入队具有明确顺序；
- outbound 重试固定其对应上下文，不从其他用户消息借 token；
- 平台返回会话过期时按协议处理，禁止无限重试；
- token 内容不得进入 Agent 上下文或日志。

### 7.4 消息顺序和幂等

同一个 `binding_id` 使用串行队列：

```text
message N -> Agent 完成或确定失败 -> message N+1
```

不同 binding 可以并行。Provider 重放、Poller 接管和进程崩溃都必须通过数据库幂等约束防止重复 Agent turn。状态建议为：

```text
received -> queued -> processing -> agent_completed
         -> outbound_pending -> delivered
                              -> failed/expired
```

Agent 已完成但 iLink 发送失败时，应保留独立 outbound 状态，不能重新执行 Agent 来“重试回复”。

---

## 8. Channel Dispatcher 与 Owner Worker

### 8.1 可信路由

Dispatcher 的输入是内部 `binding_id` 或注册库解析结果，而不是外部提供的 Owner：

```text
inbound account + authentic peer
  -> active channel binding
  -> canonical user
  -> owner binding
  -> trusted OwnerContext
  -> OwnerWorkerSupervisor.get_or_start(owner)
```

任何查找缺失、不一致、被撤销或 suspended 都必须 fail closed，并将消息置于明确的不可投递状态。

### 8.2 Worker 调用路径

Dispatcher 应增加一个服务端内部 `OwnerWorkerGatewayClient`：

1. 获取 exact `OwnerWorkerHandle`；
2. 取得绑定当前 worker generation 的 authority lease；
3. 通过 Unix socket 和 OWP1 bootstrap 连接 Owner Worker `/api/ws`；
4. 新会话调用 `session.create`；
5. 冷恢复调用 `session.resume`；
6. 每条入站消息调用 `prompt.submit`；
7. 消费结构化 gateway events，提取最终 assistant response；
8. 将结果写入 outbound queue，由 Connector 调用 iLink `sendmessage`。

不得在 Control Plane、Enrollment Service 或 Connector 中直接实例化 `AIAgent`，也不得建立第二套 Session/Agent runtime。

### 8.3 SessionSource

建议使用内部 ID：

```python
SessionSource(
    platform=Platform.WEIXIN,
    chat_id=binding_id,
    user_id=canonical_user_id,
    chat_type="dm",
    owner_key=owner_key,      # metadata/audit only
    tenant_id=tenant_id,
)
```

`SessionSource.owner_key` 仅用于元数据和审计，不构成授权。授权来自可信 registry、Control Plane 路由和 exact-worker lease。原始 `ilink_user_id`、`from_user_id` 和 `bot_token` 不进入 session key。

---

## 9. API 契约草案

### 9.1 创建 Enrollment

```http
POST /api/public/ilink/enrollments
Content-Type: application/json

{
  "scene": "poster-202607"
}
```

响应：

```json
{
  "attempt_id": "enr_opaque_random_value",
  "qr_content": "weixin://...",
  "status": "waiting",
  "expires_at": 1780000000
}
```

`qr_content` 是 iLink 返回的可扫描内容；服务端不得自行拼接内部 qrcode token 作为替代。

### 9.2 查询 Enrollment

```http
GET /api/public/ilink/enrollments/{attempt_id}
```

响应只暴露：

```json
{
  "status": "waiting|scanned|confirmed|expired|failed",
  "expires_at": 1780000000,
  "next_action": "wait|open_wechat|retry"
}
```

不得暴露：

- `bot_token`；
- `ilink_bot_id`；
- `ilink_user_id`；
- `owner_key`；
- owner home；
- worker ID、socket 或 generation；
- 平台原始错误中的敏感内容。

### 9.3 浏览器完成态

浏览器只需要知道授权已完成。若需要让用户随后进入 Dashboard，可签发短期的 enrollment completion credential，再通过独立 account-claim 流程建立 Dashboard session；不能把 iLink token 当作浏览器登录 Cookie。

---

## 10. 仓库改造建议

### 10.1 提取可复用 iLink transport

从 `gateway/platforms/weixin.py` 提取协议能力到聚焦模块，例如：

```text
gateway/weixin_ilink/
├── client.py
├── models.py
├── enrollment.py
├── media.py
└── state.py
```

原 `WeixinAdapter` 继续调用这些模块，保证现有单 Owner 用法兼容。不要让核心模块反向依赖具体多租户产品服务。

### 10.2 新增 Control Plane 身份注册

建议：

```text
hermes_cli/channel_identity/
├── models.py
├── store.py
├── registration.py
├── owner_resolution.py
└── account_linking.py
```

职责包括 canonical user、external identity、Owner binding、账号状态、幂等事务和 claim。

### 10.3 新增 iLink Enrollment/Connector

长期建议将厂商 Connector 作为独立服务或独立集成仓库，Hermes core 只保留通用、已验证的渠道路由接口。若先在本仓库做产品 PoC，模块需与 per-owner Gateway Adapter 明确分层，例如：

```text
hermes_cli/channel_connectors/weixin_ilink/
├── enrollment.py
├── account_store.py
├── poller.py
├── poller_supervisor.py
└── sender.py
```

这是一项产品集成，不应通过 vendor-specific 特判污染通用 Relay contract。若后续多个渠道需要动态 Owner 路由，应先定义通用 Channel Dispatcher 契约，再让 iLink 成为第一个具体消费者。

### 10.4 新增 Dispatcher

建议：

```text
hermes_cli/channel_dispatch/
├── dispatcher.py
├── gateway_client.py
├── session_router.py
├── inbound_queue.py
└── outbound_queue.py
```

可复用：

- `hermes_cli/dashboard_auth/owner_context.py` 的 OwnerContext 结构，但需增加 registry-backed 的可信构造路径；
- `hermes_cli/owner_runtime.py` 的 owner runtime 目录和环境；
- `hermes_cli/owner_worker/supervisor.py` 的 worker 生命周期和 generation fencing；
- `hermes_cli/owner_worker/ws_routes.py` 的 exact-worker `/api/ws`；
- `tui_gateway/ws.py`、`tui_gateway/server.py` 的 JSON-RPC Agent 路径；
- `gateway/session.py` 的 SessionSource 和 session key 规则。

### 10.5 不应复用为主身份库的组件

- `gateway/pairing.py`：它表达 owner 对 Gateway 用户的批准，不表达 canonical ownership；
- owner-local `state.db`：绑定存在于 Owner 建立之前，且 Control Plane 需要路由；
- `authority.sqlite3`：它负责 worker generation、lease 和 replay authority，不负责外部渠道身份；
- 浏览器 Dashboard Session：iLink 授权结果不是现有交互式认证 provider session。

---

## 11. 安全、风控与配额

公开 Enrollment API 至少需要：

- IP、设备、ASN 和时间窗口级创建速率限制；
- 单 attempt 并发轮询限制；
- 最大未完成 attempt 数；
- 二维码过期和原子消费；
- 数据库唯一约束抵御并发重复确认；
- 失败、撤销、重绑和异常 sender 的审计；
- 每 Owner 每日消息数、模型 Token、并发 turn 和工具执行配额；
- Workspace、上传文件类型和大小配额；
- 全局模型费用熔断；
- 可疑注册挑战或 CAPTCHA；
- canonical user、external identity 和 account 三层 suspension；
- 日志中的身份、二维码 token、消息内容和凭据去敏。

Control Plane 和 Connector 可访问 iLink API，但 Owner Worker/Tool Executor 的网络范围仍遵守部署 egress profile。共享 iLink credential 不能出现在 Owner 工具可读取的环境或挂载中。

---

## 12. 失败恢复

### 12.1 二维码过期

- attempt 标记 `expired`；
- 停止轮询；
- 用户显式重试后创建新二维码；
- 不复用旧 qrcode token。

### 12.2 授权确认与数据库提交之间崩溃

- 使用 durable attempt 保存必要的确认处理状态；
- 重试确认处理必须由 identity/account 唯一约束幂等化；
- 不得因为重试创建第二个 Owner；
- 凭据写入失败时不激活 Poller。

### 12.3 Poller 崩溃或实例接管

- 使用 account lease + fencing；
- 从持久化 `get_updates_buf` 恢复；
- provider message 进入 durable dedup 表后再推进可确认的本地处理状态；
- 接管时允许重复拉取，但不允许重复 Agent turn。

### 12.4 Owner Worker 重启

- 丢弃旧 generation 的 gateway 连接；
- 重新调用 `get_or_start()`；
- 取得新 generation lease；
- 用 `stored_session_id` 执行 `session.resume`；
- 根据 inbound 状态判断是否允许重新提交 prompt。

### 12.5 Agent 成功但发送失败

- 保留 assistant result；
- 仅重试 outbound；
- 不重新调用 Agent；
- 达到平台重试上限或上下文失效后标记 `failed`/`expired`，并提供运营可见状态。

### 12.6 Token 撤销或失效

- 停止对应 poller 和发送；
- account 标记 `reauth_required` 或 `revoked`；
- Owner 数据保留；
- 用户重新授权后更新 account credential 并继续解析到同一 canonical user/Owner。

---

## 13. 分阶段实施

### Phase 0：平台 PoC 与条款确认

用 1～10 个真实普通微信账号验证：

- 二维码扫码和确认流程；
- `ilink_user_id`、`ilink_bot_id`、入站 `from_user_id` 的稳定性及关系；
- 重复授权、换设备、撤销授权和 Token 失效语义；
- `context_token` 有效期和无 token 发送行为；
- 长轮询数量、频率和服务端 IP 限制；
- 媒体能力和大小限制；
- 是否允许商业化、多租户服务托管大量用户 `bot_token`。

未确认平台许可前，不进行大规模公开投放。

### Phase 1：小规模文本 MVP

为了最快验证完整链路，可以暂时采用：

```text
每用户专属二维码
  -> 注册 canonical user/Owner
  -> 凭据存入 owner 安全范围
  -> 常驻 Owner Worker
  -> 现有 WeixinAdapter 收发文本
```

限制：

- 仅允许小规模内测；
- 明确记录常驻 Worker 的资源成本；
- 不声称支持空闲 Worker 回收；
- 仍须保证 per-owner sandbox 和凭据隔离；
- 该阶段产物不得阻碍 Phase 2 transport 提取。

### Phase 2：Central Connector 与按需 Worker

- 提取 iLink client/transport；
- 建立 Control Plane channel identity registry；
- 建立 account poller supervisor、lease 和 durable cursor；
- 建立 inbound/outbound queue；
- 实现 exact-owner Gateway client；
- 第一条消息按需启动 Owner Worker；
- Worker 空闲回收后仍持续接收 iLink 消息。

### Phase 3：可靠性与账号生命周期

- 凭据轮换和重授权；
- 撤销检测；
- Poller 分片和接管；
- 配额、封禁、费用熔断和运营状态；
- Dashboard account claim；
- 文本之外的图片、文件、语音和视频；
- 指标、告警、备份恢复和数据删除流程。

---

## 14. 验证计划

所有涉及 Owner Worker、session/resume、Gateway、身份、凭据和网络 I/O 的实现都属于 Strict 路径。除聚焦单测外，必须验证真实路由和隔离边界。

### 14.1 iLink transport

- 二维码响应必须优先使用完整 `qrcode_img_content`；
- waiting/scanned/confirmed/expired 状态机；
- 网络超时、错误 JSON、限流和退避；
- `getupdates` cursor 持久化与恢复；
- `context_token` 的 account + peer 隔离；
- 文本、媒体和空回复行为；
- credential/token 不出现在日志和异常中。

### 14.2 Enrollment 与身份注册

- 同一确认结果并发提交只创建一个 canonical user；
- 重复授权恢复原 Owner；
- 不同用户生成不同 Owner；
- attempt 不可枚举、过期和单次消费；
- 浏览器不能读取 token、Owner 或 worker 信息；
- 数据库不可用时 fail closed；
- suspended/revoked identity 不可路由；
- 账号认领后 owner_key 不变。

### 14.3 Connector 与队列

- 同一 account 只有一个有效 poller；
- lease 接管后旧 poller 不能推进有效状态；
- 重复 provider message 只产生一个 Agent turn；
- 同一 binding 串行、不同 binding 并行；
- Agent 完成后发送失败只重试 outbound；
- cursor、message 和 context token 崩溃恢复保持一致性。

### 14.4 Dispatcher 与 real-path 集成

至少覆盖：

```text
模拟 confirmed enrollment
  -> 创建 canonical user/owner/account binding
  -> 模拟 iLink inbound message
  -> durable dedup/queue
  -> OwnerWorkerSupervisor.get_or_start
  -> 真实 Owner Worker /api/ws
  -> session.create 或 session.resume
  -> prompt.submit
  -> assistant result
  -> outbound queue
  -> 模拟 iLink sendmessage
```

两个不同 iLink 用户的集成测试必须断言：

```text
owner_key 不同
HERMES_HOME 不同
state.db 不同
session 历史不串
Memory/Skills/Workspace 不可互访
context_token 不串
旧 worker generation 不可继续使用
前端或消息伪造 owner_key 不改变路由
```

测试通过仓库标准入口运行：

```bash
scripts/run_tests.sh tests/path/to/affected_test.py
```

---

## 15. 上线门槛与待确认事项

在公开上线前必须得到明确答案：

1. 腾讯是否允许一个 Hermes 服务托管大量用户各自授权的 `bot_token`；
2. 是否允许商业化、多租户 SaaS 使用；
3. 每个服务端 IP、账号和进程允许的并发长轮询数量；
4. 消息、媒体、输入状态和上传的频率限制；
5. Token 固定期限、续期和撤销通知机制；
6. `ilink_user_id` 是否跨重复授权稳定；
7. `ilink_user_id` 与入站 `from_user_id` 的权威对应关系；
8. 相同微信用户重复授权时是否产生新的 `ilink_bot_id`；
9. ClawBot 能力是否对全部目标用户开放或存在灰度/白名单；
10. 用户数据保存、删除、导出和隐私告知要求。

任何答案与本方案假设不一致时，应调整身份唯一约束、Poller 模型或产品流程，不能通过放宽 Owner 路由验证来兼容。

---

## 16. 最终决策

没有企业账号时，iLink Bot 是实现“普通微信扫码授权后与个人 Hermes 助手聊天”的可行优先方案。Hermes 应采用“公开注册页动态生成每用户专属二维码”，而不是宣传一张永久 iLink Bot 二维码。

推荐最终闭环：

```text
专属 iLink 授权二维码
  -> 幂等 canonical user 注册
  -> 不可变 Owner 绑定
  -> Central iLink Connector 持续收发
  -> Channel Dispatcher 可信路由
  -> exact isolated Owner Worker
  -> 现有 tui_gateway / AIAgent
```

先通过小规模真实账号 PoC 验证平台语义和商业使用条件，再实施集中 Poller 和按需 Worker 的规模化架构。无论采用 MVP 还是最终架构，Owner 只能由 Control Plane 的可信注册绑定派生，绝不能由二维码参数、浏览器或入站消息直接选择。

## 参考资料

- [腾讯官方 iLink 插件：Tencent/openclaw-weixin](https://github.com/Tencent/openclaw-weixin)
- [腾讯官方插件中文说明](https://github.com/Tencent/openclaw-weixin/blob/main/README.zh_CN.md)
- [腾讯云：微信 ClawBot 配置说明](https://cloud.tencent.com/document/product/1291/129132)
- [iLink Bot 社区协议说明](https://www.wechatbot.dev/zh/protocol)
- [Qwen Code 微信渠道文档](https://qwenlm.github.io/qwen-code-docs/zh/users/features/channels/weixin/)
