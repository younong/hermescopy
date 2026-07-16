# Hermes 执行沙箱部署方案：Docker 强隔离基线

> 更新时间：2026-07-10
>
> 状态：authenticated 多用户部署的目标安全基线（不是当前全量部署实现声明）
>
> 主架构：[Control Plane、Owner Runtime 与执行沙箱](2026-07-07-multi-user-isolation-plan.md)

本文件定义 Linux host 上的 Docker 容器如何作为 authenticated 多用户的唯一支持执行沙箱实现。在主方案明确的信任假设下，它满足所需的 OS 强制隔离基线：Linux host、Docker daemon/runtime、获批准镜像供应链、host-side supervisor 和 deployment profile 必须受信。Docker 不是信任根，不独立证明 owner 身份，也不声称防护恶意宿主管理员、daemon/内核被攻陷、容器逃逸或获批准镜像被攻陷。gVisor 如启用，只是 Docker 容器的 runtime hardening 选项，不是独立部署模型；微虚拟机、独立 UID/GID + DAC/ACL、mount namespace、裸进程和非 Linux 平台均不在本版本支持范围。它不是独立的认证或路由方案，也不代表现有所有生产部署均已采用 Docker：**Control Plane 仍负责可信身份、OwnerContext 派生和请求路由；Sandbox 负责限制已路由到某个 owner 的 Owner Runtime Worker 与 Tool Executor 实际能访问的文件、进程、权限和网络。**

## 1. 发布门槛

authenticated 多用户部署必须在 Linux host 上启用获批准的 **host-observed deployment verification record（宿主观测部署验证记录，以下简称 deployment verification record）**，并以 Docker per-owner container 运行。每个 owner runtime 或受限执行环境必须拥有：

- 独立有效安全主体；
- 仅能看见当前 owner 的文件系统视图；
- 不可跨 owner 复用的进程、容器、tmpfs、socket 和内存状态；
- 公网默认可达、内部敏感目标默认拒绝且可审计的网络策略；
- 可验证的 capability、syscall、资源和危险宿主机集成限制。

单独满足“一个 owner 一个进程”“设置 `HERMES_HOME`”“不同目录名”或“同一 Unix UID 下的目录权限约定”，**不构成充分隔离**。同 UID 进程可能直接打开其他 owner 的绝对路径。

deployment verification record 是版本化、机器可验证的 admission evidence，且由 host-side 受信 supervisor/Control Plane 从 Docker/OCI runtime metadata 读取、比对和签发。它至少覆盖：approved immutable image digest、Docker/runtime 配置与 deployment manifest/profile 版本（启用 gVisor 时含 runtime ID/version）、Docker container ID 与 lifecycle/start identity、`owner_key`、`worker_id`、worker generation、effective non-root UID/GID、host-observed mount source/target/mode、network attachment/egress profile、read-only rootfs、capability、`no-new-privileges`、seccomp/AppArmor/SELinux 或 gVisor 设置、tmpfs、CPU/memory/PID 限额、verifier identity、record ID/version、观测时间/新鲜度与 audit correlation ID。

它证明 worker admission 时 host 所观察到的 Docker 配置及其 owner/worker 绑定；它不证明 host、Docker daemon、内核、镜像行为或 runtime 没有未来漏洞。容器自报、容器内环境变量或路径、worker 提供的 container ID 和浏览器参数都只可用于诊断，不能建立 owner 身份、挂载归属、profile 合规或授权。受信 supervisor/Control Plane 在接纳 worker 前必须将 record 绑定到 `owner_key`、worker generation 和容器实例，并在 lifecycle 变化时重新检查；record 缺少、陈旧、失配、无法验证或运行时配置漂移时必须 fail closed、拒绝或摘流，并记录 record ID/version 与拒绝原因的去敏审计。

authenticated 多用户部署仅支持 Docker container 作为实现方式；gVisor 如启用，仅是 Docker runtime hardening option。没有获批准的 deployment verification record 时，Control Plane 必须拒绝 authenticated 多用户启动或服务，不能降级为共享 UID/shared-home 运行。local / unauthenticated legacy 模式不满足本基线，不能与 authenticated 多用户运行时混用。

## 2. 分层、路径视图与数据根

```text
可信登录态 / WS ticket
  -> Control Plane：认证、OwnerContext、路由、生命周期
  -> host-side 受信 supervisor：生成/验证 deployment verification record 与 mount 契约
  -> Owner Runtime Worker：HERMES_HOME=<runtime_owner_home>
  -> Tool Executor：任务级 workspace/tmp/egress profile
  -> Docker per-owner container（可选 gVisor runtime hardening）
  -> 仅 owner-local persistent/workspace/runtime + 只读 global resources
```

必须区分宿主持久化路径与 sandbox 内逻辑路径：

| 名称 | 含义 | 授权语义 |
| --- | --- | --- |
| `host_global_home` | Control Plane 所在宿主/受信 supervisor 管理的持久化根 | 仅用于受控部署和持久化管理，不是 worker 授权输入 |
| `host_owner_home` | `<host_global_home>/users/<owner_key>` | 仅由 Control Plane 根据 `OwnerContext` 选择 |
| `runtime_owner_home` | worker namespace 中的逻辑 owner 根，亦即 `HERMES_HOME` | 由已验证 mount 契约决定，不是浏览器或容器自述的身份依据 |
| sandbox mount view | 例如将 `host_owner_home` 挂载为 `/owner`、只读全局资源挂载为 `/global` | 仅为运行时路径；不得要求其与宿主绝对路径字符串相等 |

文件系统主键统一使用主方案的 `owner_key`：

```text
<host_global_home>/
  control-plane/             # 仅控制面 secret、supervisor 元数据、去敏日志与 control-only IPC
  users/
    <owner_key>/             # host_owner_home；可在 sandbox 内映射为 /owner
      state.db
      persistent/
      workspaces/
      runtime/                # owner 业务 runtime，不承载 Control Plane 控制认证
      memories/
      skills/
```

- `user_id`、`tenant_id`、`auth_provider` 只来自可信 session/ticket，并由 Control Plane 统一派生 `owner_key`；容器环境、路径参数或浏览器提交的字段不能决定 owner home。
- Control Plane 只在受控 bootstrap 时原子创建/确认 `host_owner_home`，并验证父目录所有权、权限和 mount source；删除、迁移、保留期变更与 key rotation 是独立管理动作，不由 logout 或容器退出触发。
- owner sandbox 只挂载其自身 `host_owner_home`，不能挂载父级 `users/`、其他 owner home、任意可写 global root 或宿主用户目录。
- 全局 skills、模板和公共知识库可只读挂载。它们不是 owner 可写区；修改必须经独立受控发布/审核流程。
- runtime、workspace、持久化产物和临时数据必须分离。容器退出后仅 owner-local persistent 数据可保留；tmpfs、socket、临时文件和残留进程必须清理。

## 3. Docker 强隔离 profile

### 3.1 不跨 owner 复用

- 一个容器/沙箱实例只绑定一个 `owner_key`，owner identity 在启动后不可改变。
- 空闲池只能按 owner 分区；**绝不**将 A 的实例、进程、tmpfs、socket、浏览器 profile 或内存状态分配给 B。
- 同 owner 的热复用只有在安全主体、挂载范围、网络 profile 与 owner identity 全部相同，且已清理非持久中间态和残留进程时才允许。
- Worker/执行器崩溃、超时或退出后，Control Plane 只能为同一个 owner 启动新实例；不能为恢复性能复用其他 owner 的实例。

### 3.2 最小权限运行要求

每个 Docker container 至少必须提供：

- 非 root 的 owner-bound 有效安全主体；如容器使用 user namespace，则宿主映射不得让运行时拥有其他 owner home 的读取能力；
- owner-local 可写 bind mount / volume；全局资源只读；根文件系统只读；
- 受大小和执行策略限制的 tmpfs 临时目录；
- `--cap-drop=ALL`、`no-new-privileges`、受限 seccomp / AppArmor / SELinux / gVisor 或等价 capability 与 syscall 控制；
- CPU、内存、PID、文件描述符、磁盘、I/O 和输出速率等资源限额；
- 无特权设备访问及最小化用户/组身份。

严禁：

- `--privileged`；
- host network、host PID、host IPC；
- Docker/containerd/CRI socket；
- 宿主 `/proc`、`/sys`、设备节点、用户目录或任意 host root 的危险挂载；
- FUSE、mount 权限、额外 device 和能改变 mount/namespace 边界的 capability；
- 可写的全局技能、模板、知识库或父级 `users/` 挂载。

示意（具体 image、UID、gVisor runtime、资源限额和 egress proxy 由部署配置确定）：

```bash
docker run --rm \
  --read-only \
  --user <owner-runtime-uid>:<owner-runtime-gid> \
  --cap-drop=ALL \
  --security-opt no-new-privileges \
  --pids-limit <approved-limit> \
  --memory <approved-limit> \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=<approved-limit> \
  --mount type=bind,source=<host_owner_home>,target=/owner,rw \
  --mount type=bind,source=<global_readonly>,target=/global,readonly \
  --network <public-egress-network-profile> \
  hermes-owner-runtime:<approved-version>
```

该命令只是联网 worker 的结构示意，不能绕过本文件的 identity、mount、egress、IPC 与验收要求。`<host_owner_home>` 是宿主挂载源，`/owner` 是 sandbox 内的 `runtime_owner_home` / `HERMES_HOME` 逻辑路径；二者不需要、也不应被比较为同一绝对路径字符串。`<public-egress-network-profile>` 必须允许经获允许 resolver 的公共互联网与正常 DNS，同时在不可绕过的网络边界拒绝 IPv4/IPv6 loopback、link-local、私网、IPv6 ULA、CGNAT、宿主机/节点、cluster Pod/Service/overlay CIDR、同节点内部服务、metadata、控制面及其内部 DNS/解析路径。`--network none` 保留给明确无需联网的受限工具执行器；访问受保护目标或需要更严格公网边界的 workload 才必须经受控 proxy/profile。

## 4. 文件访问：Safe Filesystem Abstraction 的部署边界

容器只挂当前 owner home 是必要防线，但不是应用层文件安全的替代品。主方案的 [Safe Filesystem Abstraction 与 Workspace Compatibility Layer](2026-07-07-multi-user-isolation-plan.md#34-safe-filesystem-abstraction-与-workspace-compatibility-layer) 是所有不可信 owner-relative 路径授权和安全敏感 mutation 的权威规范，包括 workspace、上传、解包、下载、日志、checkpoint、socket、临时文件和子进程 cwd。此处仅规定 Docker deployment profile 如何为该协议提供不可逃逸的 mount 与身份边界。

禁止以下不安全模式：

```text
resolve_path(owner, relative_path)
if target.startswith(owner_root):
    open(target)
```

以及任何“`realpath()` / `resolve()` 检查后，再以路径字符串执行 `open`、`mkdir`、`rename`、`delete`”的变体。检查与使用之间可能发生 symlink、rename、bind mount 或其他 TOCTOU 替换。

Safe Filesystem Abstraction 必须采用：

1. 从预先安全打开的 owner 允许根目录 FD 出发，仅处理相对路径；
2. Linux 优先 `openat2` 的 `RESOLVE_BENEATH`、`RESOLVE_NO_SYMLINKS`、`RESOLVE_NO_MAGICLINKS` 和必要时 `RESOLVE_NO_XDEV`；
3. Linux 上无 `openat2` 时，通过 `openat` / `dir_fd` 逐层打开、`O_NOFOLLOW`、目录类型检查、`fstat` 实现 descriptor-relative 原子操作；authenticated 多用户部署不支持其他平台；
4. 创建、原子替换、rename、删除、解包和 socket 创建均在同一受控根目录 FD 下完成；实际打开对象还要进行类型、device/inode 或等价复核；
5. 拒绝绝对路径、空路径、`..`、symlink、magic link、跨 mount/device 以及非批准文件类型；结合 owner-only mount、独立安全主体和禁止外部 link 引入，处理 hard-link、rename race 与 bind mount 风险。

## 5. 公网默认可达与受保护目标 egress 例外

### 5.1 强制 egress 参考拓扑与网络基线

本版本使用以下参考拓扑；部署可替换具体 gateway/proxy 产品，但不得改变“workload 只能经单一强制执行点出网、control-only 面不可从 workload network 到达”的语义：

```text
Owner Runtime Worker / Tool Executor workload network
  -> approved DNS path + mandatory egress gateway/proxy
  -> public internet

Control Plane / supervisor control-only IPC or isolated control network
  -> never reachable from workload network
```

Worker 与 Tool Executor 只接入 workload network，不得拥有 host network、Docker socket、host PID/IPC 或直接 public-network attachment；只有 gateway/proxy/route 才能向公网转发。仅设置 `HTTP_PROXY` 不构成强制执行。DNS 也必须经获批准 resolver/proxy；执行点同时校验 DNS 结果与实际连接目标，拒绝 direct DNS、direct IP、proxy bypass 以及到 control-only/internal 地址的路径。

authenticated 多用户的联网 worker/sandbox 可以按 profile 访问**公共互联网**，但所有 allow/deny 决策必须由上述不可绕过的网络执行点强制；不得只由 worker 代码或 hostname 字符串比较。该执行点必须为所有 egress 产生连接级去敏 allow/deny 审计。其网络 profile 必须在网络边界默认拒绝以下目标，并由部署版本化配置具体 CIDR/路由集合：

- IPv4/IPv6 loopback、IPv4/IPv6 link-local、宿主机/节点本地和同节点其他 workload；
- IPv4 私网/RFC1918、IPv6 ULA 与 CGNAT；
- cluster Pod/Service/overlay CIDR、云 metadata endpoint 与集群控制面；
- 解析或访问上述受保护目标所需的内部 DNS/网络路径。

正常公网 DNS 解析和模型 API、网页访问、软件更新等常规公网访问可达，不要求逐域名 allowlist；“公共互联网可达”仅指 profile 明确支持的 resolver 与协议，并不表示任意 direct transport 可达。Control Plane 与 Worker 的控制流仍走受控 IPC，不得把本地网络可达性当作授权。

| Profile | 主体 | 网络语义 |
| --- | --- | --- |
| `control-only` | Control Plane / supervisor | 工具执行器不可达；仅允许独立批准的运维 egress。 |
| `owner-public` | 需要公网的 Owner Runtime Worker | 只经强制 gateway/proxy 和获批准 DNS 访问支持的公网协议；内部敏感范围默认拒绝。 |
| `tool-none` | 默认 Tool Executor | `--network none` 或等价无路由隔离。 |
| `tool-public` | 有明确公网任务需求的 Tool Executor | 独立于 worker profile，经同一强制 gateway/proxy 与默认拒绝集合出网。 |
| `protected-target` | 窄范围例外 workload | 短期、用途绑定，受 FQDN/SNI、证书、DNS、协议与端口限制的 gateway/proxy route。 |

Tool Executor 不得自动继承比所属任务更宽的 egress capability；特别是 `owner-public` 与 `protected-target` 不自动传递给 spawn 的工具、子容器或浏览器执行器。

### 5.2 受保护目标与额外约束的 egress policy

只有以下情况需要版本化 egress policy：访问前述受保护目标，或 workload 需要比“公网默认可达”更严格的网络范围。该 policy 不能用于绕过 owner、IPC 或网络边界。

每个 policy 至少绑定：

```text
owner / workload / purpose / approver / policy-version / issued-at / expires-at
allowed protocol / FQDN or SNI / port / route via approved egress proxy
certificate and DNS constraints
```

执行要求：

- 受保护目标例外或收紧型 workload 通过受控 egress proxy/gateway 或等价可验证 allowlist 实施，而不是仅在应用层比较 hostname 字符串；
- 该例外仅允许指定 FQDN/SNI、协议和端口；禁止裸 IP、任意端口、通配范围和借由内网/metadata 的绕过；
- DNS 由受控 resolver/proxy 处理，实际连接同时验证解析结果、目标、SNI/TLS 与策略，防止 DNS rebinding、SNI 不匹配和相邻域名绕过；
- `protected-target` 仅在获批准 workload 中通过受控 egress proxy/gateway 生效；它不自动继承给 spawn 的 Tool Executor、子容器或浏览器执行器；
- policy 到期、撤销或不匹配时自动拒绝，不依赖人工修改镜像或重启全部服务。

所有 egress 的连接级 allow/deny 必须由网络执行点生成去敏审计；每个受保护目标的 allow/deny、策略变更、临时例外和到期撤销还必须记录：策略 ID/版本、owner 摘要、worker/container、workload/用途、目标类别或域名摘要、端口、结果、拒绝原因和 correlation ID。不得记录 prompt、Authorization header、API key、完整 URL query、请求体或会话正文。

## 6. Control Plane ↔ Worker IPC 部署要求

Docker container 不是身份来源。Control Plane 仍须向 Worker 提供主方案定义的 owner-bound internal capability：绑定 issuer/key version、派生 owner、`worker_id + generation + owner_key` audience、scope、协议版本、短 `exp` 与单次 `jti`。Worker restart/generation 变化必须立即废止旧 capability；握手后的请求绑定已认证连接并防重放。

Control Plane/supervisor 在 host-side control-only area 维护内部 capability 和外部 WS ticket 的**唯一权威** replay store；owner container、worker 和 Tool Executor 均不可访问。主方案的 [路由、IPC 与请求语义](2026-07-07-multi-user-isolation-plan.md#4-路由ipc-与请求语义) 权威定义 `jti` 原子消费、audience、传输绑定、重放拒绝和 restart invalidation。部署层只要求：store 不可用时 fail closed；worker/container restart 必须递增 generation、关闭旧连接、废止旧 audience/capability、重新生成 deployment verification record 并 mint 新 capability；旧 generation `jti` 不可迁移。supervisor 重启若无法可信恢复 replay store，所有未消费 capability/ticket 必须失效并要求重新认证/re-mint。

部署层必须将 Control Plane ↔ Owner Runtime Worker 的控制面与 owner 业务面、Tool Executor 执行面分离：控制 IPC、内部 capability、mTLS 私钥、权威 replay state 和 supervisor metadata 位于 host-side `control-only` area，不能被 owner container 或 Tool Executor 读取、写入、挂载或通过 FD/env 继承。owner-local 业务 socket 可以位于 owner runtime，但不得承担 Control Plane 控制认证；Tool Executor 不是 Control Plane peer，只能使用任务所需 workspace/tmp/egress profile，默认不拥有内部 capability、外部 WS ticket、认证材料、控制 socket 或超出任务 profile 的网络权限。

部署层要求：

- 优先 Unix domain socket，置于 host-side control-only 区域，并由独立安全主体最小权限拥有；同时校验 peer credential。共享 UID 下仅依赖 socket mode 不合格。
- 若使用 loopback TCP，必须使用 mTLS，双方验证证书链和精确 target audience；仅监听 `127.0.0.1` 不构成认证。
- 浏览器绝不能直连 worker；外部 WS ticket 仅由 Control Plane 消费，是短期、single-use、exact-audience-bound 的 bearer credential，包含可信 principal 材料、issuer/key version、protocol version、`jti`、`exp` 和 route/protocol audience。Control Plane 必须在 upgrade 前验证 session/revocation/membership、owner 与 audience，并原子消费 `jti`；成功或竞争消费后所有副本/重试均拒绝，reconnect/new connection/retry 必须重新认证并 mint 新 ticket。ticket 不得转发给 Worker 或作为内部 IPC capability。
- Worker spawn 的 Tool Executor 使用最小白名单环境和由 Workspace Compatibility Layer 选定的 cwd/tmp，显式关闭或不继承无关 FD、control-only env 与认证材料。
- 错误 issuer、owner、audience、worker generation、scope、过期/重复 token、异常 peer 或重放均必须拒绝并生成去敏安全审计。

## 7. 结构化审计与回收

至少记录：

```text
timestamp | event_type | owner_key_digest | worker_id/container_id |
policy_id/version | action/target_class | outcome | failure_reason | correlation_id
```

记录 worker/container 创建、deployment verification record ID/version 验证失败或漂移、owner mismatch、IPC capability 拒绝、replay consume/reject、replay-store restore failure、generation rollover/restart invalidation、路径拒绝、全部 egress 的连接级 allow/deny、受保护目标 policy 变更/到期、回收、crash 和 restart。常规日志不得包含 prompt、会话正文、ticket/token、文件内容、完整命令参数、Authorization header 或密钥。

容器/worker 回收必须删除 owner-local 的非持久 socket、tmpfs、临时产物和残留进程；host-side control-only replay state 由 Control Plane/supervisor 按 token expiry 与 restart-invalidation 规则管理，不能转移给 owner container 或其他 owner 实例。

## 8. 发布验收清单

- [ ] authenticated 多用户部署仅在 Linux host 上使用 Docker per-owner container；deployment verification record 缺少、陈旧、无法验证、失配或配置漂移时，Control Plane 拒绝启动/服务；不会降级为共享 UID/shared-home，并审计 record ID/version、image digest、container/generation 绑定与拒绝原因。
- [ ] host/runtime 路径映射由已验证 mount 契约证明：`host_owner_home` 仅对应当前 owner 的 `runtime_owner_home`（如 `/owner`），不依赖或要求容器内外绝对路径字符串相等。
- [ ] A/B 即使同宿主 UID，也无法列举、读取或打开对方 owner home、DB、runtime、workspace、log、checkpoint 或 socket。
- [ ] 每个 owner runtime 的安全主体、可见 mount、可写目录与业务 socket 权限仅覆盖其 owner；Control Plane 控制 IPC/认证材料位于工具不可见的 control-only 区域；不存在 privileged、host namespace、runtime socket、父级 `users/` 或危险 host mount。
- [ ] 沙箱根只读、非 root、capability/syscall 收敛、资源限额和临时 tmpfs 已验证；不跨 owner 复用容器、进程、tmpfs、socket 或内存状态。
- [ ] symlink、rename race、bind mount、hard-link、archive extraction 和“检查后替换目标”测试均不能越出安全根；实现使用 [Safe Filesystem Abstraction](2026-07-07-multi-user-isolation-plan.md#34-safe-filesystem-abstraction-与-workspace-compatibility-layer) 所要求的 FD-relative/openat2 或等价原子协议，而非字符串前缀或单纯 realpath 检查。
- [ ] host-side 权威 replay store 在 bootstrap/WS upgrade 前原子消费 `token_class + issuer/key-version + jti + exact_audience`；拒绝错误 issuer/owner/audience/generation/scope、过期或重复 `jti`、跨连接重放和不合规 UDS peer/mTLS。worker/container restart 后旧 capability 立即失效；无法可信恢复 replay store 时 outstanding token fail closed；Tool Executor 无法取得或继承 control socket、internal capability、WS ticket、认证材料或无关 FD/env。
- [ ] workload network 只有经 mandatory egress gateway/proxy 的出网路径，且 control-only 面从 workload network 不可达；`owner-public`/`tool-public` 仅能访问支持的公网协议和获允许 resolver 的公网 DNS，`tool-none` 无网络，且全部 egress 产生连接级去敏 allow/deny 审计。所有 profile 均无法访问 IPv4/IPv6 loopback、link-local、私网、IPv6 ULA、CGNAT、宿主机/节点、cluster CIDR、metadata、控制面或其内部 DNS 路径。
- [ ] 使用 `protected-target` 时，只能经 proxy/allowlist 访问指定协议、FQDN/SNI 和端口；IP 直连、DNS rebinding、SNI 不匹配、相邻域名和未批准目标均失败。
- [ ] 外部 WS ticket 仅由 Control Plane 消费，精确绑定 public WS route/protocol audience，短期 single-use；首次消费或竞争消费后副本/重试均失败，reconnect 必须重新认证并 mint 新 ticket，ticket 不会转发给 Worker。
- [ ] 隔离、deployment verification record、路径、IPC、replay 和 egress 的 allow/deny/异常/回收行为均产生去敏、可关联审计记录。

## 一句话总结

**OwnerContext 决定“谁有资格访问”，Owner Runtime Worker 决定“在哪个 owner runtime 中执行并如何调度 Tool Executor”，deployment verification record 与 per-owner container 决定“在受信 host/runtime 假设下，工作负载实际能接触什么”。** 三者缺一不可。