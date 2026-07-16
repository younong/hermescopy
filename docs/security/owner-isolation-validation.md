# Owner Isolation Release Evidence Index

> 更新时间：2026-07-10  
> 状态：Phase 0 evidence index；当前 owner-isolation foundation 的验证入口，不是 GA 认证或完整安全声明。  
> 权威路线图：[多用户隔离分阶段发布计划](../plans/2026-07-10-ws-ticket-logout-revocation-plan.md)。架构规范见 [Control Plane、Owner Runtime 与执行沙箱](../plans/2026-07-07-multi-user-isolation-plan.md)，部署基线见 [Docker 执行沙箱方案](../plans/docker.md)。

## 1. 使用规则与当前声明

本索引将每项 release evidence 绑定到路线图阶段、实现/测试入口、当前状态和以后阶段的缺口。证据必须包含运行版本/commit、环境、命令、结果、时间、执行者与失败调查链接；测试名称或计划文字本身不是发布证据。

**当前 baseline 只是一组候选 foundation，尚未满足 Phase 1 及以后阶段，也绝不满足 multi-user-isolated GA。** 尤其尚未由本索引证明：共享 authority 的跨副本原子 revoke/replay、authorization epoch、worker generation lease、全 owner-sensitive routing、FD-relative filesystem safety、OS-level executor isolation、mandatory egress、deployment verification record、resource governance、迁移/canary 或独立对抗签核。未完成的 gate 必须保持关闭，不能把已有 application-layer 测试解释为 Docker、网络、进程或 GA 保证。

### 证据状态词

| 状态 | 含义 |
| --- | --- |
| `foundation_present` | 当前共享工作树中已有候选实现或测试；仍需本阶段验收。 |
| `planned` | 仅有契约/计划，没有可作为该阶段完成证明的实现证据。 |
| `not_ga` | 即使所有当前条目通过，也不能支撑 GA 声明。 |

## 2. Phase ledger 与 evidence map

| rollout phase | 当前状态 | 当前 foundation / 关键位置 | 候选自动化证据 | 尚缺的 release evidence / gate |
| --- | --- | --- | --- | --- |
| 0 契约、威胁模型、兼容基线 | `ready_for_review` | 本索引；[rollout plan](../plans/2026-07-10-ws-ticket-logout-revocation-plan.md) 的 Phase 0 contract/ADR/ledger | 文档链接、Markdown/pytest collect 检查 | security、architecture、migration/rollback review；abuse-case tabletop；old/new participant compatibility review |
| 1 shared authority、epoch、browser ticket revoke | `planned` | `hermes_cli/dashboard_auth/owner_context.py`、`routes.py`、`middleware.py`、`ws_tickets.py`；`tests/hermes_cli/test_dashboard_auth_ws_{tickets,auth}.py`、`test_dashboard_auth_middleware.py` | 当前 unit tests 可证明 ticket/owner-context 的单进程基础行为 | authority-backed atomic epoch/revoke/replay、cross-replica logout-vs-consume、IdP/membership revoke、outage fail-closed、key rotation/recovery；gate `shared_authority_epoch` |
| 2 worker generation、lease、lifecycle | `planned` | `hermes_cli/owner_runtime.py`、`hermes_cli/owner_worker/`；`tests/hermes_cli/test_owner_runtime.py`、`test_owner_worker.py` | worker spawn/health/path-bound foundation tests | authority lease/CAS、monotonic generation、drain/revoke/crash/PID reuse race、多进程 chaos；gate `owner_worker_generation` |
| 3 owner HTTP/WS/session/Gateway/TUI/PTY routing | `foundation_present` | `hermes_cli/web_server.py`、`session_api.py`、`gateway/{session,mirror,channel_directory}.py`、`tui_gateway/{server,ws}.py`、`web/src/lib/browserIdentity.ts`、`web/src/lib/useDashboardAuthIdentity.ts` | `tests/hermes_cli/test_dashboard_auth_gate.py`、`test_web_server*.py`、`tests/gateway/test_{session_store_owner_filter,mirror,channel_directory}.py`、`tests/test_tui_gateway_owner_workspace_cwd.py`、`web/src/lib/browserIdentity.test.ts` | all owner-sensitive endpoints and long connections bound to epoch/generation; A/B E2E including mid-stream revoke, cross-server reconnect and stale channel; gate `owner_routed_data_plane` |
| 4 safe filesystem/workspace compatibility | `planned` | `hermes_cli/owner_runtime.py`、`tools/{checkpoint_manager,process_registry}.py`、`tests/hermes_cli/test_web_server_{files,fs}.py` | current path/cwd tests are regression candidates only | Safe Filesystem Abstraction, FD-relative/openat2 or equivalent protocol, traversal/symlink/TOCTOU/FD/checkpoint adversarial matrix; gate `safe_owner_filesystem` |
| 5 isolated Tool Executor and credential broker | `planned` | `tools/{checkpoint_manager,process_registry}.py` are only integration surfaces | no phase-complete evidence currently indexed | per-task process/container, minimal credentials/FD/mount/env, revoke cleanup and adversarial OS tests; gate `isolated_tool_executor` |
| 6 mandatory egress and deployment verification | `planned` | [Docker plan](../plans/docker.md) defines the target contract | no implementation proof currently indexed | production-like Docker profile, host-observed record, mandatory egress reachability/drift tests; gate `mandatory_egress_verification` |
| 7 resource, audit, observability | `planned` | dashboard auth and runtime modules are future event sources | no phase-complete evidence currently indexed | quota/noisy-neighbor, audit completeness/redaction, cleanup soak and incident drill; gate `multi_user_resource_governance` |
| 8 migration, canary, adversarial validation and GA | `planned` / `not_ga` | phase plans define target inputs | no GA evidence currently indexed | versioned migration parity, non-host-network canary, independent adversarial validation and all phase sign-offs; gate `multi_user_isolated_ga` |

## 3. Current foundation validation matrix (phases 0–3)

The following components are preserved as candidate foundation from the current shared working tree. This table intentionally maps only what is observable now; it does not claim that a component already meets the final semantics assigned to its phase.

| foundation component | phase mapping | current verification focus | evidence artifact to attach before phase sign-off |
| --- | --- | --- | --- |
| `dashboard_auth/owner_context.py`, auth middleware/routes and `/api/auth/me` | 0 → 1 | trusted session-derived owner context; no client owner selection | focused pytest output for owner-context/auth middleware and security review record |
| `dashboard_auth/ws_tickets.py` and web-server WS auth | 0 → 1 → 3 | ticket audience/path/replay behavior; external vs internal credential separation | unit plus multi-replica atomic/revoke/outage/key-rotation test results (later; not yet present) |
| `owner_runtime.py`, `owner_worker/` supervisor/client/entrypoint | 0 → 2 | owner-local home/workspace/environment and worker health foundations | spawn/health test output now; later lease/generation lifecycle/chaos results |
| `web_server.py`, `session_api.py`, dashboard auth gate | 0 → 3 | Control Plane gate and candidate owner-worker proxy behavior | A/B HTTP/WS/PTY evidence, including stale selector and revoke scenarios |
| `gateway/session.py`, `mirror.py`, `channel_directory.py`, `tui_gateway/{server,ws}.py` | 0 → 3 | owner metadata/filtering and owner workspace cwd foundations | A/B Gateway/TUI/reconnect/stale-channel test output |
| browser identity, chat/sidebar/pages and `useDashboardAuthIdentity.ts` | 0 → 3 | owner-namespaced browser ID and logout/user-switch cleanup foundation | browser unit test output plus manual A/B connection-cleanup record |

## 4. Phase 0 review checklist

- [ ] Principal, trust-source and forbidden-input contract is reviewed against every external and internal entrypoint.
- [ ] Scope tuple, authorization-decision shape, epoch-bump events, propagation SLO, cache limit and authority-outage behavior are accepted.
- [ ] Browser ticket and internal credential namespaces, audience/operation bindings, replay and key-rotation/recovery semantics are accepted.
- [ ] The Phase 1 shared-authority ADR has a named owner and confirms atomicity, availability and recovery requirements before implementation begins.
- [ ] Legacy client/state upgrade, rejection and rollback matrix is accepted; no authenticated multi-user fallback can read global/legacy state.
- [ ] Current owner-isolation changes are preserved as candidate foundation; no Phase 1+ authorization/routing behavior is represented as complete by this document.

## 5. Focused baseline commands

Run from repository root. These commands are focused regression/collection checks for the preserved foundation; passing output must be stored with the release candidate and does **not** satisfy Phase 1–8 gates by itself.

```bash
pytest -q \
  tests/hermes_cli/test_dashboard_auth_middleware.py \
  tests/hermes_cli/test_dashboard_auth_ws_tickets.py \
  tests/hermes_cli/test_dashboard_auth_ws_auth.py \
  tests/hermes_cli/test_dashboard_auth_gate.py \
  tests/hermes_cli/test_owner_runtime.py \
  tests/hermes_cli/test_owner_worker.py

pytest -q \
  tests/hermes_cli/test_web_server.py \
  tests/hermes_cli/test_web_server_files.py \
  tests/hermes_cli/test_web_server_fs.py \
  tests/gateway/test_session_store_owner_filter.py \
  tests/gateway/test_channel_directory.py \
  tests/gateway/test_mirror.py \
  tests/test_tui_gateway_owner_workspace_cwd.py

./node_modules/.bin/vitest run web/src/lib/browserIdentity.test.ts
```

If the frontend test command or dependency set differs in the checked-out worktree, record the exact substitute command and reason rather than marking the frontend evidence as passed.

## 6. Historical application-layer checklist (foundation only)

The scenarios below remain useful manual regression cases. They establish at most the application-layer boundary represented by the current foundation and do not replace the phase-specific evidence above.

### Preconditions

- Dashboard auth is enabled (`app.state.auth_required == true` / non-loopback bind).
- `HERMES_OWNER_SECRET` is stable or matches `<global_home>/control-plane/owner_secret`.
- Owner worker backend is `process` mode unless the evidence explicitly says otherwise.
- Test users A and B have distinct authenticated `user_id` values.

### Identity, homes and session sentinels

1. Log in as A and B, call `GET /api/auth/me`, and record redacted identity summaries.
2. Verify distinct `owner_key`, `isolation_mode == "owner_worker"`, `legacy_sessions_imported == false`, and no `owner_home` response field.
3. Place global/default, A-only and B-only session sentinels as controlled test data.
4. Verify A and B can list only their own sentinel; cross-owner detail returns 404; global/default data is not returned.

### Control Plane, WS and worker checks

- In authenticated mode, verify `GET /api/profiles/sessions`, `/api/config`, `/api/memory`, and unmigrated/legacy profile selectors fail closed rather than reading global state.
- For `/api/pty`, `/api/ws`, `/api/pub`, `/api/events`, mint each user’s browser ticket and verify the Control Plane bridges to the corresponding owner worker. Verify internal credentials and wrong owner/audience/path/control-home material are rejected.
- Via the Control Plane client, verify worker health reports `ready`, requested owner key, expected owner-local `HERMES_HOME`/workspace root and no forbidden environment variables. The supervisor must reject a mismatch.
- Verify owner runtime indices (`state.db`, session/channel data, logs, checkpoints, process registry and default workspace) remain below that owner’s home; verify browser localStorage is owner-namespaced and switch/logout closes old WS/PTY/reconnect work.

### Expected result and limitation

A and B must not see, resume, mutate, delete, search, export or receive live events for each other through the covered application HTTP/WS/UI paths, and authenticated Control Plane must not directly serve covered owner-sensitive state. This is **not** a claim that later-phase epoch propagation, OS process/filesystem/network isolation, release migration, or GA requirements are satisfied.
