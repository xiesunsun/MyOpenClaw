# S2 Sandbox（进程级）设计稿

日期：2026-07-26。前置：S1 shell 升级（已完成）。调研：`docs/upgrade/2026-07-26-tools-sandbox-research.md` §3.4。

目标：给 shell 会话（前台 + 后台任务）加进程级沙箱——bubblewrap 文件系统边界 + 凭据保护 + 拒写自身，落在 `PtyShellProcess.spawn` 这一个咽喉点上。

## 1. 范围

**做**：bubblewrap 文件系统边界；凭据文件读拒绝；凭据环境变量剥离；拒写自身配置/代码；默认开 + 缺依赖降级 + strict 模式；会话级逃生口。

**不做（后续按需）**：网络 proxy allowlist / TLS 终止 / mask 注入（独立大工程，凭据已被 deny/剥离收窄泄露面）；backend 抽象 local/docker/ssh（S2b）；MCP 子进程沙箱（`.mcp.json` 即用户信任声明）；沙箱策略 extension 化（pi-sandbox 形态，需 spawn hook 位点，YAGNI）；seccomp/Landlock（候选路线，bwrap 生态更成熟且与 Claude Code 对齐）。

## 2. 决策记录

| 问题 | 决策 | 理由 |
| --- | --- | --- |
| 本期范围 | 仅进程级（bwrap） | 调研建议分期；收益立刻兑现在 shell_exec |
| 网络 | 不限 | proxy 链是独立大工程；凭据保护已收窄泄露面 |
| 默认态 | 默认开，缺 bwrap 降级裸跑 + warning | 安全默认且不破坏无 bwrap 环境 |
| strict | `sandbox.strict: true` 时缺依赖拒绝创建 shell | 供生产/无人值守场景 |
| 逃生口 | 会话级（`shell_restart` 参数），默认忽略 | pickel 是持久 shell，per-command 逃逸不成立；无人审流程下模型可自主逃逸等于没有沙箱 |

## 3. 架构

一个新模块 + 两处接线：

```
src/pickel/tools/sandbox.py     # SandboxPolicy：settings 解析、env 过滤、bwrap 参数生成、可用性探测
src/pickel/tools/shell.py       # PtyShellProcess.spawn 接 policy：包 bwrap + 过滤 env
src/pickel/app/boot.py          # 从 app_config.sandbox 构造 policy，传给 ShellSessionManager
```

前台 `PersistentShell` 与后台 `BackgroundTask` 共用 `PtyShellProcess.spawn` —— 一处接线全覆盖。

### 3.1 SandboxPolicy

```python
@dataclass(frozen=True)
class SandboxPolicy:
    enabled: bool = True
    strict: bool = False
    allow_disable: bool = False
    allow_write: tuple[str, ...] = ()   # 额外放行写的路径
    deny_read: tuple[str, ...] = ()     # 额外读拒绝路径
    env_deny: tuple[str, ...] = ()      # 额外剥离的环境变量名
    env_allow: tuple[str, ...] = ()     # 从默认剥离规则中豁免的名字
```

settings.json 顶层 `sandbox` 段解析（全局+项目合并沿用现有 settings 机制），缺省即全默认值。

### 3.2 文件系统边界（bwrap）

参数生成顺序（后写的绑定覆盖先写的）：

```
bwrap --die-with-parent
      --ro-bind / /                    # 整机只读
      --dev /dev --proc /proc
      --bind <workspace> <workspace>   # 写=workspace
      --bind /tmp /tmp                 # 写=/tmp（session 输出目录在 workspace 或 /tmp 下，天然覆盖）
      [--bind <allow_write_i> ...]     # settings 增补
      --ro-bind <self_j> <self_j>      # 拒写自身：盖回只读（见 3.3）
      --tmpfs <deny_read_k>            # 凭据目录：tmpfs 掩盖，读写全不可见
      -- <shell_program> ...
```

- **写默认** = workspace + `/tmp`；**读默认** = 全机减 deny 清单。
- **读拒绝默认清单**（存在才加）：`~/.pickel`、`~/.ssh`、`~/.aws`、`~/.config/gcloud`、`~/.kube`、`~/.docker`；settings `deny_read` 增补。符号链接 resolve 后再加入。
- pty：slave fd 经 stdio 继承进 bwrap 内进程，控制终端语义不变（bwrap 不 setsid）；S1 的 job control / stderr 管道（`pass_fds`）照常——bwrap 默认继承传入 fd。

### 3.3 拒写自身（自修改的许可证之前，先拒）

写掩盖、读放行（`--ro-bind` 盖回只读）：

- `~/.pickel`（已在读拒绝清单里，天然含写拒绝）
- `<project_root>/.pickel`
- `<project_root>/agents`
- pickel 代码：源码树的 `src/pickel`（或安装场景的包目录，取 `pickel.__file__` 的包根）

workspace 就是 pickel 仓库时（本项目日常），`src/pickel` 与 `agents/` 在 `--bind workspace` 之后被盖回只读——agent 改不了自身代码与配置。确需放行时用 `sandbox.allow_write` 显式声明（人的授权），V1 的门控机制来了再谈自动化。

### 3.4 凭据环境变量剥离

与 bwrap 无关，纯 spawn 层，**降级裸跑时也生效**：

- 默认模式：变量名匹配 `*_API_KEY` / `*_TOKEN` / `*_SECRET` / `*_PASSWORD` / `*_CREDENTIALS` / `*_ACCESS_KEY`（大小写不敏感）即剥离。
- `env_deny` 增补精确名；`env_allow` 豁免（如用户确要给 shell `GITHUB_TOKEN`）。
- 实现点：`PtyShellProcess.spawn` 构造 `shell_env` 后过 `policy.filter_env(shell_env)`。

### 3.5 默认开、降级与 strict

- spawn 时 `shutil.which("bwrap")` 探测：
  - 有 → bwrap 强制生效；
  - 无且 `strict=False` → 记一次 warning，裸跑（env 剥离仍生效）；
  - 无且 `strict=True` → 抛 `RuntimeError`，工具层转 is_error（"sandbox unavailable and sandbox.strict is on"）。
- `sandbox.enabled: false` → 完全关闭（含 env 剥离），行为回到今天。
- metadata：`ShellExecTool` 结果增 `sandboxed: bool`，模型与日志可见当前边界状态。

### 3.6 会话级逃生口

- `shell_restart` 增参数 `sandbox: boolean`（默认 true）。`sandbox: false` 时：
  - `allow_disable=False`（默认）→ 忽略参数、照常沙箱重建，结果 content 注明被忽略；
  - `allow_disable=True` → 重建非沙箱 shell（env 剥离也跳过），metadata `sandboxed: false`。
- `shell_exec` 不带逃生参数；后台任务跟随当前策略。

## 4. 错误处理汇总

| 场景 | 行为 |
| --- | --- |
| bwrap 缺失（默认） | warning 一次，裸跑降级，env 剥离仍生效 |
| bwrap 缺失（strict） | 创建 shell 抛错 → 工具 is_error |
| bwrap 启动失败（参数/权限） | 同 shell 启动失败现状：TERMINATED + is_error |
| deny 路径不存在 | 跳过该条，不报错 |
| `sandbox: false` 请求但未授权 | 忽略 + content 注明 + 照常沙箱 |

## 5. 测试计划

| 层 | 覆盖 |
| --- | --- |
| policy 单测 | settings 解析与默认值、env 过滤（模式/deny/allow）、bwrap 参数生成（绑定顺序、self-protect 盖回、deny tmpfs、路径不存在跳过） |
| 降级单测 | mock 无 bwrap：warning + shell 可用 + env 仍剥离；strict：建 shell 抛错 |
| 集成（skipIf 无 bwrap） | 沙箱内：写 workspace 成功、写 `~/.pickel` 失败、读 deny 路径不可见、剥离的 env 不在 `env` 输出里、S1 全部 shell 语义（超时/三件套/后台）在沙箱内回归 |
| 手动验收 | 真会话：`cat ~/.pickel/.env` 不可见、`env | grep API_KEY` 为空、`touch src/pickel/x` 失败、正常开发命令（git/uv/pytest）可用 |

集成测试与验收前需安装：`sudo apt install bubblewrap`（一次性，用户执行）。

## 6. 遗留取舍

1. **网络不设防**——出站通道存在，但凭据文件读不到、env 里没有，可外泄面已收窄；proxy allowlist 是下一期。
2. **策略层在 core 不做 extension**——spawn hook 位点等有第二个消费者再造；pi-sandbox 形态记录在案。
3. **`/proc` 全量挂载**——`--proc` 标准挂载，进程列表对沙箱内可见；隔离到 pid namespace（`--unshare-pid`）会破坏 job control 探测（`tcgetpgrp` 语义不变但 `ps` 类工具行为变），首版不动。
4. **bwrap 内嵌套容器场景不支持**——容器内跑 pickel 需 `sandbox.enabled: false` 或等 S2b 的 backend 抽象。
5. **Landlock 候选**——内核 6.17 原生支持、零依赖，但 Python 生态弱；若 bwrap 路线遇阻可切换，策略层接口不变。
