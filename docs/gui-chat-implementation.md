# Hermes GUI Chat 实现文档

## 概览

本文描述 Hybrid GUI Chat 的实现方案。目标是在现有 Dashboard 中新增一个真正的 GUI Chat，同时保留当前 TUI/xterm Chat。

当前 Terminal Chat 链路：

```text
web/src/pages/ChatPage.tsx
  -> /api/pty
  -> hermes_cli/web_server.py::pty_ws
  -> PtyBridge.spawn(node ui-tui/dist/entry.js)
  -> xterm.js
```

目标 GUI Chat 链路：

```text
web React GUI components
  -> /api/ws
  -> hermes_cli/web_server.py::gateway_ws
  -> tui_gateway/server.py
  -> AIAgent
  -> structured events
  -> React state/rendering
```

## 关键原则

1. **复用现有后端能力**：不新增平行 agent runtime。
2. **保留 Terminal Chat**：GUI 初期新增入口，不替换 `/api/pty`。
3. **事件结构化优先**：前端不解析 ANSI 文本来构建 GUI。
4. **渐进交付**：先对话闭环，再工具卡片，再审批/任务/文件视图。
5. **可回退**：GUI 失败时用户可以切回 Terminal Chat。

## 相关现有文件

### 前端

- `web/src/pages/ChatPage.tsx`
  - 当前 Terminal Chat 页面。
  - 使用 `/api/pty` 和 xterm.js。
- `web/src/lib/browserIdentity.ts`
  - 提供稳定 `browser_id`。
- `web/src/components/ChatSidebar.tsx`
  - 已有 session/sidebar 相关交互，可复用部分 UI 思路。

### 后端

- `hermes_cli/web_server.py`
  - `/api/pty`：Terminal Chat PTY WebSocket。
  - `/api/ws`：Dashboard Chat sidecar WebSocket。
- `tui_gateway/server.py`
  - session 创建、agent 构建、事件发送、slash worker、approval 等核心逻辑。
- `tui_gateway/ws.py`
  - WebSocket transport 和 session 生命周期。
- `run_agent.py`
  - `AIAgent` runtime。

## 前端新增结构

建议新增目录：

```text
web/src/pages/GuiChatPage.tsx
web/src/features/gui-chat/
  api.ts
  protocol.ts
  reducer.ts
  types.ts
  components/
    GuiChatShell.tsx
    MessageList.tsx
    MessageBubble.tsx
    Composer.tsx
    ToolCallCard.tsx
    ImageArtifactCard.tsx
    AttachmentPreview.tsx
    ApprovalCard.tsx
    SessionSidebar.tsx
    InspectorPanel.tsx
```

### `GuiChatPage.tsx`

职责：

- 页面级路由入口。
- 初始化 WebSocket。
- 挂载 Chat shell。
- 处理页面卸载 cleanup。

### `api.ts`

职责：

- 封装 `/api/ws` 连接。
- 发送 JSON-RPC 请求。
- 分发服务端事件。
- 处理 reconnect。

建议 API：

```ts
connectGuiChat(options): GuiChatConnection
connection.request(method, params): Promise<Result>
connection.onEvent(listener): unsubscribe
connection.close()
```

### `protocol.ts`

职责：

- 定义 GUI 使用的 JSON-RPC 方法和事件类型。
- 与后端事件 schema 对齐。

示例类型：

```ts
type GuiChatEvent =
  | { type: "session.info"; sessionId: string; model?: string }
  | { type: "message.user"; id: string; text: string }
  | { type: "message.assistant.delta"; id: string; delta: string }
  | { type: "message.assistant.done"; id: string }
  | { type: "tool.start"; id: string; name: string; input?: unknown }
  | { type: "tool.output"; id: string; chunk: string }
  | { type: "tool.done"; id: string; ok: boolean; error?: string }
  | {
      type: "artifact.image";
      id: string;
      messageId?: string;
      source: { kind: "artifact" | "url"; value: string };
      mimeType: "image/png" | "image/jpeg" | "image/webp" | "image/gif";
      title?: string;
      width?: number;
      height?: number;
    }
  | { type: "approval.request"; id: string; payload: unknown }
  | { type: "error"; message: string };
```

### `reducer.ts`

职责：

- 把事件归并成前端状态。
- 管理消息、工具调用、审批、连接状态。

建议状态：

```ts
interface GuiChatState {
  connection: "connecting" | "open" | "closed" | "error";
  sessionId?: string;
  model?: string;
  messages: ChatMessage[];
  toolCalls: Record<string, ToolCallState>;
  artifacts: Record<string, ArtifactState>;
  approvals: Record<string, ApprovalState>;
  isGenerating: boolean;
  error?: string;
}
```

## 后端协议实现

### 优先复用 `/api/ws`

GUI Chat 不应新增独立 HTTP agent endpoint。优先复用：

```text
hermes_cli/web_server.py::gateway_ws
  -> tui_gateway/ws.py
  -> tui_gateway/server.py
```

第一步需要盘点现有 `/api/ws` 支持的方法。重点确认：

- session 创建
- session 恢复
- 用户消息提交
- stop/cancel
- session info
- tool events
- image/artifact events
- approval events
- error events

如果已有方法名和事件足够，前端直接接入；如果缺失，则在 `tui_gateway/server.py` 补结构化事件。

### 推荐 GUI 所需最小方法

```text
session.create
session.resume
chat.send
chat.stop
approval.respond
session.list
session.close
```

实际命名应以当前 `tui_gateway` 已有协议为准，不要为了 GUI 另起一套重复命名。

### 推荐 GUI 所需最小事件

```text
session.info
message.user
message.assistant.delta
message.assistant.done
tool.start
tool.output
tool.done
artifact.image
artifact.created
approval.request
approval.resolved
error
```

如果当前后端已有类似事件，前端做 adapter；如果没有，后端补 emit。

## MVP 实现步骤

### Step 1：协议盘点

只读梳理：

- `hermes_cli/web_server.py::gateway_ws`
- `tui_gateway/ws.py`
- `tui_gateway/server.py` 中 JSON-RPC dispatch 和 `_emit(...)`
- 前端现有 sidecar 使用点

输出一张表：

```text
方法/事件 | 当前是否存在 | 参数 | GUI 是否需要 | 缺口
```

### Step 2：新增页面入口

新增 `GuiChatPage.tsx`，并在 Dashboard 路由/导航中增加入口。

建议入口文案：

```text
Chat GUI (beta)
```

保留原入口：

```text
Chat Terminal
```

### Step 3：建立 WebSocket 和 session

前端：

1. 打开 `/api/ws`。
2. 认证沿用现有 Dashboard token/internal credential 机制。
3. 创建或恢复 session。
4. 收到 `session.info` 后渲染模型/状态。

后端：

- 尽量不改；必要时补返回字段。

### Step 4：发送消息和渲染回复

前端 Composer：

- Enter 发送。
- Shift+Enter 换行。
- 发送后 append user message。
- 设置 `isGenerating=true`。

事件处理：

- assistant delta：追加到当前 assistant message。
- assistant done：结束生成。
- error：显示错误卡片。

### Step 5：基础 Markdown 渲染

建议使用现有项目依赖或轻量 Markdown renderer。第一版至少支持：

- 段落
- 列表
- inline code
- fenced code block
- links

如果依赖尚未存在，优先复用已有前端依赖，不为 MVP 引入过重编辑器。

### Step 6：图片 artifact 和附件展示

第一版 GUI Chat 应明确支持图片直接显示在对话中，而不是只显示服务器路径或文件名。

需求：

- Assistant 消息可以挂载图片 artifact。
- Tool result 可以挂载图片 artifact，例如图像生成、截图、浏览器自动化截图、文件读取结果。
- 图片卡片支持缩略图、点击放大、打开原图、下载、复制链接。
- 支持 `png`、`jpeg`、`webp`、`gif`。
- 前端不直接渲染任意服务器绝对路径；后端必须把图片包装为 artifact id 或受控 URL。
- 图片 artifact 要能和 message id / tool call id 关联，方便在对应消息或工具卡片下展示。

建议组件：

```text
ImageArtifactCard
  thumbnail
  title / mime type / dimensions
  open original
  download
  copy link
```

### Step 7：工具调用卡片

先支持通用卡片：

```text
ToolCallCard
  name
  status: running/succeeded/failed
  input summary
  output preview
  expandable full output
```

后续再做 Bash/File/Edit/Deploy 专用展示。

### Step 8：停止生成

Composer 右侧按钮在生成中变为：

```text
Stop
```

调用现有 stop/cancel 方法。若后端还没有 GUI 可用方法，则复用 TUI gateway 的取消能力补 JSON-RPC 方法。

## 后端改动建议

### 事件补齐位置

优先在 `tui_gateway/server.py` 已有 `_emit(...)` 调用附近补事件，而不是在 web_server 层解析输出。

原则：

- agent/runtime 知道语义，应该由它发结构化事件。
- web_server 只负责 WebSocket 传输和认证。

### 图片 artifact 协议

GUI Chat 需要一个受控 artifact 机制来展示图片，避免把服务器绝对路径直接暴露给浏览器。

推荐能力：

```text
GET  /api/artifacts/{artifact_id}
POST /api/artifacts
```

其中：

- `GET /api/artifacts/{artifact_id}` 用于读取已登记的图片 artifact。
- `POST /api/artifacts` 用于未来支持用户上传图片或粘贴图片。
- 第一阶段可以只实现读取/展示 agent 和工具生成的图片；上传可放到后续阶段。

artifact metadata 建议包含：

```json
{
  "id": "img_abc123",
  "kind": "image",
  "mime_type": "image/png",
  "path": "server-managed-path",
  "url": "/api/artifacts/img_abc123",
  "session_id": "...",
  "message_id": "...",
  "tool_call_id": "...",
  "created_at": "..."
}
```

安全要求：

- artifact 文件必须位于 Hermes 管理目录或明确允许的输出目录内。
- 禁止通过 artifact API 读取任意服务器路径。
- 使用 Dashboard 现有鉴权。
- 限制文件大小和 MIME type。
- 不在事件中直接暴露真实服务器路径。

### 错误处理

所有 agent 初始化/运行错误都应至少发：

```json
{
  "type": "error",
  "message": "...",
  "phase": "agent_init | run | tool | approval"
}
```

GUI 显示错误卡片，并提供：

- 复制错误
- 重试
- 切换 Terminal Chat

### Tool event 标准化

如果当前事件名称不统一，增加 adapter 层，不要一次性大迁移。

建议后端保留原事件，同时补 GUI-friendly 字段：

```json
{
  "id": "tool-call-id",
  "name": "Bash",
  "title": "Run systemctl status",
  "status": "running",
  "input": {...},
  "output": "...",
  "error": null
}
```

## 测试策略

### 前端单测

重点测 reducer：

- assistant delta 合并。
- image artifact event 挂载到正确 message/tool call。
- tool start/output/done 状态流转。
- error event 展示。
- reconnect 后状态不重复。

### 前端集成测试

使用 Playwright 或现有测试工具：

1. 打开 GUI Chat。
2. mock `/api/ws`。
3. 发送消息。
4. 推送 assistant delta。
5. 推送 image artifact event。
6. 断言消息、图片和工具卡片渲染正确。

### 后端测试

重点测：

- GUI 所需 JSON-RPC 方法可创建 session。
- agent init 失败时发结构化 error。
- tool events 包含 GUI 所需字段。
- image artifact 只能读取受控目录内图片，不能越权读取任意路径。
- stop/cancel 能中断运行。

### 手工验收

1. 本地启动 Dashboard。
2. 打开 Terminal Chat，确认旧路径可用。
3. 打开 GUI Chat，完成多轮对话。
4. 触发一个工具调用，确认显示卡片。
5. 触发或 mock 一个图片 artifact，确认图片可在对话中预览、放大、下载。
6. 刷新 GUI 页面，确认不会产生新的 PTY Node 进程。
7. 部署到阿里云后，通过 SSH tunnel 验证。

## 部署注意事项

GUI Chat 前端产物随 `web` workspace build 进入：

```text
hermes_cli/web_dist
```

当前阿里云 bare-metal 发布流程会在本机构建 web/ui-tui，再上传产物。新增 GUI 页面后，发布前至少确认：

```bash
npm run build --workspace web
node --check deploy/deploy.mjs
```

发布后验证：

```bash
ssh root@106.15.186.104 'readlink /opt/hermes/current'
ssh root@106.15.186.104 'systemctl is-active hermes-gateway hermes-dashboard'
ssh -L 9119:localhost:9119 root@106.15.186.104
```

浏览器打开：

```text
http://localhost:9119
```

## 建议里程碑

### Milestone 1：GUI Chat MVP

- 新页面入口。
- `/api/ws` 连接。
- session create/resume。
- 发送消息。
- assistant 流式文本。
- 错误卡片。
- Stop 按钮。

### Milestone 2：图片展示和工具卡片

- ImageArtifactCard。
- Assistant 消息内图片预览。
- Tool result 图片预览。
- 通用 ToolCallCard。
- Bash 输出折叠。
- 文件读写摘要。
- 失败状态。

### Milestone 3：审批、任务和图片输入

- ApprovalCard。
- Task panel。
- Background task status。
- 用户上传/粘贴图片附件。
- 多图附件预览。

### Milestone 4：默认 GUI

- GUI Chat 作为默认 Chat。
- Terminal Chat 作为 fallback。
- 观测 PTY 使用率和残留进程。

## 开发注意事项

1. 不要从 GUI 直接 shell out 或绕过 gateway。
2. 不要在前端解析 ANSI 来恢复语义。
3. 不要删除 `/api/pty`，直到 GUI 覆盖全部关键能力。
4. 新协议字段应向后兼容。
5. 错误必须结构化传给前端，不只写日志。
6. GUI 状态应能从事件流重建，避免隐藏状态散落在组件里。
7. 图片和附件必须走 artifact 机制，不能把服务器绝对路径当作浏览器 URL 直接渲染。

## 下一步建议

下一步先做 Phase 0：协议盘点。完成后再决定 MVP 需要补哪些后端事件和前端组件。