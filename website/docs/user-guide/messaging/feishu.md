---
sidebar_position: 11
title: "Feishu / Lark"
description: "Connect multiple Feishu or Lark bots as independently configured AI employees"
---

# Feishu / Lark

Hermes can run multiple Feishu or Lark self-built applications at the same time. Each connected bot is an independent **AI employee** with its own system prompt, model registration, tool/Skill/MCP allowlists, workspace scope, knowledge paths, and execution limits.

Hermes manages the local connection and employee policy. It does **not** create, publish, suspend, or delete the external application in Feishu or Lark. Perform those actions in the corresponding Developer Console.

## Supported behavior

- One Hermes deployment can keep multiple managed bot accounts connected concurrently.
- Direct messages from human users are accepted.
- In group chats, a bot responds only when the message contains a structural mention of that exact bot or directly replies to a message previously sent by that exact bot.
- Text that merely contains a display name is not treated as a mention.
- Bot, application, and system-authored messages are rejected.
- Replies to another bot, another chat, a human message, or an unknown message do not trigger the bot.
- Separate chats and Feishu thread roots use separate retained sessions.

The current retained-channel implementation uses the official Feishu/Lark SDK WebSocket connection. A public webhook endpoint is not required.

## 1. Create and configure each external app

Repeat these steps for every bot you want Hermes to run.

1. Open the Developer Console:
   - Feishu: [https://open.feishu.cn/](https://open.feishu.cn/)
   - Lark: [https://open.larksuite.com/](https://open.larksuite.com/)
2. Create a self-built application.
3. Enable the **Bot** capability.
4. In **Permissions**, grant the permissions needed to receive and send messages. At minimum, enable the permissions presented by the console for receiving messages and sending messages as the bot.
5. In **Events and Callbacks**, select **Long Connection / WebSocket** and subscribe to `im.message.receive_v1`.
6. Create and publish an application version. Enterprise installations may require administrator approval.
7. Copy the App ID and App Secret from **Credentials & Basic Info**.

:::warning
Treat App Secret, Encrypt Key, and Verification Token as secrets. Do not paste them into chat messages, source files, or documentation.
:::

## 2. Enable the Feishu provider

Open **Dashboard → Channels** and enable the Feishu / Lark provider. The provider switch controls whether managed account connectors are allowed to run.

## 3. Add an AI employee

On the Feishu channel card, select **Add employee** and enter:

- App ID
- App Secret
- `feishu` or `lark` domain
- optional Encrypt Key and Verification Token
- employee name and role
- system prompt
- chat model registration
- toolset, Skill, and MCP server allowlists
- Owner-relative workspace and knowledge paths
- execution limits

Hermes verifies the App ID and App Secret with the real provider and resolves the bot identity before saving. Credentials and employee profiles are encrypted in the control-plane database. API responses never return stored secrets or ciphertext.

A managed account belongs immutably to the authenticated Hermes Owner who created it. Another Owner cannot list, inspect, rotate, start, or roll over that account.

## AI employee policy

Policy is configured per bot account. The first version does not support group-specific prompt overrides.

The policy controls:

| Field | Meaning |
|---|---|
| Name / role | Human-readable employee identity |
| System prompt | Stable instructions for the employee |
| Model registration | Exact configured chat model |
| Toolsets | Concrete tool groups; wildcard `all`/`*` is rejected |
| Skills | Skills exposed to this employee |
| MCP servers | Named MCP servers exposed to this employee |
| Workspace path | Relative path inside this Owner's workspace |
| Knowledge paths | Relative paths inside this Owner's permitted roots |
| Limits | Maximum iterations and optional token limit |

Paths must be relative and remain inside the authenticated Owner's runtime roots. Absolute paths, `..` traversal, escaping symlinks, and cross-Owner paths are rejected.

## Immutable conversation snapshots

When a Feishu chat or thread creates a retained Hermes session, Hermes pins the employee profile revision and fingerprint to that conversation. Editing the employee later does not mutate an active conversation.

This behavior preserves conversation semantics, safe cold resume, and prompt caching:

- existing conversations continue using their original prompt, model, tools, Skills, MCP scope, paths, and limits;
- new conversations use the latest profile revision;
- select **Roll over sessions** to retire this bot's idle channel mappings so subsequent messages create sessions with the latest revision;
- rollover is rejected while the selected account has messages actively processing.

Rollover affects only the selected managed account. Other bots and their sessions are unchanged.

## Account lifecycle

Each employee has independent lifecycle controls:

- **Test** — verifies the currently encrypted credentials and bot identity.
- **Suspend** — stops only this bot's connector while retaining encrypted configuration and audit/session data.
- **Resume** — starts only this bot's connector.
- **Rotate secret** — verifies the candidate secret before persisting it and requires that it resolve to the same immutable bot identity.
- **Revoke** — terminally stops local use of the account while preserving required audit/session records.

Suspending or revoking one employee does not stop other Feishu employees. Revoking in Hermes does not delete or disable the external Feishu/Lark application; manage the external application separately in the Developer Console.

Secret inputs are never refilled in the Dashboard. When rotating App Secret, leaving optional Encrypt Key and Verification Token fields blank preserves their existing encrypted values.

## Group-chat admission

Hermes uses provider-verified structure rather than display text:

1. The sender must be a human user.
2. A structural mention must match an immutable identity of the current bot account, or the direct parent message must match a persisted outbound receipt from this bot in this chat.
3. Only the current bot's verified mention placeholder is removed from the prompt; mentions of other people or bots remain.
4. A verified reply inherits the exact conversation/thread scope of the persisted outbound message.

The first valid message in a direct chat or group creates an Owner-bound local chat binding for that managed account. Different human members of the same admitted group can then participate in that group's retained conversation.

## Troubleshooting

### The account fails its connection test

- Confirm that the App ID and App Secret belong to the same application.
- Confirm the selected domain is `feishu` for Feishu China or `lark` for Lark international.
- Confirm the app is published and installed for the intended tenant.
- Rotate the App Secret in Hermes after changing it in the Developer Console.

Provider response bodies and access tokens are deliberately redacted. Use the stable Dashboard status and server logs without copying secrets into support messages.

### Direct messages do not arrive

Confirm that the bot capability is enabled, the app version is published/approved, Long Connection is selected, and `im.message.receive_v1` is subscribed.

### Group messages do not trigger

Use the client's real `@` picker to mention the exact bot, or directly reply to a message that bot sent in the same chat. Typing the bot's name as plain text, using `@all`, replying to a human, or replying to another bot is intentionally ignored.

### A profile edit does not affect an existing chat

This is expected. Existing retained conversations use an immutable policy snapshot. Use **Roll over sessions** after ensuring no message is processing, or start a new chat/thread.
