# Hermes GUI Chat 规划

## 背景

当前 Hermes Dashboard 已经提供 Web 页面，但 Chat 区域主要是把 TUI 通过 PTY 和 xterm.js 嵌入浏览器。线上运行链路大致为：

```text
Dashboard ChatPage
  -> /api/pty WebSocket
  -> node ui-tui/dist/entry.js
  -> TUI ANSI 输出
  -> xterm.js 渲染
```

这种方案能快速复用现有 TUI 能力，但用户体验仍然是终端式：消息、工具调用、审批、任务状态、文件 diff 等都混在 ANSI 文本里，不利于 Web GUI 展示，也会引入 PTY/Node 会话生命周期问题。

目标是新增真正的 GUI Chat，而不是一次性废弃 TUI。推荐采用 Hybrid 路线：保留现有 Terminal Chat 作为 fallback，同时新增 GUI Chat，通过现有 `/api/ws` 和 `tui_gateway` 复用 Hermes agent/session/tooling 能力。

## 目标

1. 提供真正的 Web GUI Chat 体验：消息气泡、Markdown、代码块、图片内嵌展示、工具卡片、审批弹窗、任务状态等。
2. 复用现有 agent runtime、session、approval、tool events、skills、memory 等能力。
3. 不破坏现有 TUI/xterm Chat；GUI 初期作为实验入口上线。
4. 逐步减少对 `/api/pty` 和 `ui-tui/dist/entry.js` 的依赖。
5. 为后续移动端、多人协作、部署看板、任务/记忆可视化打基础。

## 非目标

1. 第一版不重写 agent runtime。
2. 第一版不删除现有 TUI/xterm Chat。
3. 第一版不要求完整覆盖 TUI 的所有快捷键和终端交互。
4. 第一版不做多人实时协作。
5. 第一版不引入独立的新后端协议，优先复用 `/api/ws`。

## 推荐方案：Hybrid GUI Chat

新增一个 GUI Chat 页面或 Tab：

```text
Chat Terminal | Chat GUI
```

- `Chat Terminal`：保留现有 xterm/TUI 路径，继续走 `/api/pty`。
- `Chat GUI`：新增结构化 GUI，走 `/api/ws` JSON-RPC / event 流。

目标链路：

```text
React GUI Chat
  -> /api/ws WebSocket
  -> tui_gateway/server.py
  -> AIAgent
  -> structured events
  -> React components
```

## 体验形态

### MVP 页面结构

```text
┌──────────────────────────────────────────────┐
│ Hermes Chat                         Model ▾  │
├──────────────────────────────────────────────┤
│ User                                         │
│   帮我检查线上 Hermes 进程                    │
│                                              │
│ Assistant                                    │
│   我会检查 gateway/dashboard 状态。           │
│                                              │
│ Tool: Bash                                   │
│   systemctl is-active hermes-gateway ...     │
│   ✓ active / active                          │
│                                              │
│ Image                                        │
│   [图片预览]  [打开原图] [下载]                │
│                                              │
├──────────────────────────────────────────────┤
│ 输入消息...                         [发送]   │
└──────────────────────────────────────────────┘
```

### 后续完整布局

```text
┌──────────────┬──────────────────────┬──────────────┐
│ Sessions     │ Chat                 │ Inspector    │
│              │                      │              │
│ 今天          │ User / Assistant     │ Tools        │
│ 昨天          │ Tool Cards           │ Files        │
│ 部署排查      │ Approvals            │ Tasks        │
│              │ Composer             │ Memory       │
└──────────────┴──────────────────────┴──────────────┘
```

## 分阶段路线

### Phase 0：协议和事件盘点

目标：确认 GUI Chat 可以复用哪些 `/api/ws` 方法和事件，哪些事件需要补齐。

产出：

- `/api/ws` 方法清单。
- session 创建/恢复/发送消息/停止生成的调用链。
- assistant delta、tool event、image artifact event、approval event、error event 的事件 schema 草案。

### Phase 1：GUI Chat MVP

目标：做出最小可用 GUI Chat。

范围：

- 新增 GUI Chat 路由或 Tab。
- 建立 `/api/ws` 连接。
- 创建或恢复 session。
- 发送用户消息。
- 流式显示 assistant 回复。
- 显示错误。
- 基础 Markdown 渲染。
- Assistant 消息内展示图片 artifact。
- 工具结果中展示图片 artifact。
- 停止生成。
- Terminal Chat 入口仍保留。

验收：

- 用户可以在 GUI 页面完成连续多轮对话。
- 遇到错误时 GUI 明确展示错误信息。
- TUI Terminal Chat 不受影响。

### Phase 1.5：图片和附件展示

目标：把 GUI 相比 TUI 的多媒体优势尽早体现出来，让图片可以直接出现在对话流里。

范围：

- Assistant 消息内直接展示图片。
- 工具调用结果中直接展示图片，例如图像生成、截图、浏览器截图、文件读取产生的图片。
- 图片卡片支持预览、点击放大、打开原图、下载、复制链接。
- 支持 `png`、`jpeg`、`webp`、`gif` 等常见浏览器可显示格式。
- 图片来源通过 artifact id 或受控 URL 表达，不在前端暴露服务器任意绝对路径。

验收：

- 当 agent 或工具产出图片时，用户不需要复制路径或打开服务器文件，可以直接在对话中查看。
- 图片访问经过 Dashboard 鉴权和路径限制。
- Terminal Chat 不受影响。

### Phase 2：工具调用 GUI 化

目标：把工具调用从终端文本升级为结构化卡片。

范围：

- Tool call started / completed / failed 卡片。
- Bash 命令、文件读写、部署步骤等常见工具的专用展示。
- 长输出折叠。
- 错误高亮。
- 可复制命令和输出。

验收：

- 工具调用不再只依赖 ANSI 文本。
- 用户可以清楚看到每个工具的状态、输入、输出和错误。

### Phase 3：审批、任务和文件视图

目标：把 Hermes 的交互能力 GUI 化。

范围：

- Approval 弹窗/卡片。
- Task list 面板。
- 文件 diff/patch 展示。
- 运行中任务/后台任务状态。
- 会话侧边栏。

验收：

- 常见开发任务可以主要在 GUI 中完成。
- Terminal Chat 成为高级/兼容入口，而不是主入口。

### Phase 4：默认切换到 GUI

目标：GUI Chat 稳定后成为默认入口。

范围：

- Dashboard 默认打开 GUI Chat。
- Terminal Chat 保留为 fallback。
- 对 `/api/pty` 增加更强的观测和清理。

验收：

- 大部分日常任务不再需要 xterm/TUI。
- 刷新/重连不会产生多余 Node PTY 进程。

## 成功指标

1. GUI Chat 能完成连续多轮对话。
2. 工具调用可读性优于 TUI 文本输出。
3. 图片类结果可以直接在对话中预览、放大和下载。
4. 失败状态清晰，不需要用户查服务器日志才能理解。
5. Terminal Chat 仍可用。
6. 刷新/重连后无 PTY/Node 进程堆积。
7. 后端不出现独立分叉的 agent runtime。

## 风险和应对

### 风险：`/api/ws` 事件不够完整

应对：先做事件盘点；MVP 只依赖必要事件，不足处在 `tui_gateway` 补结构化事件。

### 风险：GUI 与 TUI 行为不一致

应对：GUI 初期作为实验入口；复杂场景可切回 Terminal Chat。

### 风险：前端状态复杂

应对：按消息、工具调用、审批、任务分层建模；先 MVP 后面板化。

### 风险：重复实现 agent 逻辑

应对：严格复用 `tui_gateway/server.py` 和现有 session/runtime；禁止新写平行 agent runner。

### 风险：图片 artifact 暴露服务器路径或越权访问

应对：图片只能通过受控 artifact id 或受控 URL 访问；后端校验文件必须位于 Hermes 管理目录内，并使用 Dashboard 现有鉴权。前端不直接渲染任意服务器绝对路径。

## 推荐结论

采用 Hybrid GUI Chat 路线：新增 GUI Chat 页面，复用 `/api/ws` 和现有 gateway/session/agent 能力，保留 TUI Terminal Chat 作为 fallback。先交付可用 MVP，并尽早支持图片在对话中直接展示；再逐步 GUI 化工具调用、审批、任务和文件视图。