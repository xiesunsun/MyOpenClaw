# Shell OS Sandbox 设计

## 1. 目标

所有本地 `bash` 命令默认进入宿主平台提供的进程级安全边界：

| 平台 | Backend | 执行入口 |
| --- | --- | --- |
| Linux | Bubblewrap | `bwrap` |
| macOS | Seatbelt | `/usr/bin/sandbox-exec` |

沙箱约束整个 Shell 进程树，而不是判断命令字符串是否“危险”。模型不能通过工具参数关闭沙箱。

## 2. 策略合同

`SandboxPolicy` 是平台无关的策略数据，`wrap_command()` 只负责把同一策略翻译成当前平台的命令：

```text
SandboxPolicy
├── Linux  → bwrap arguments
└── macOS  → Seatbelt profile + path parameters
```

默认策略：

- 宿主文件系统可读。
- workspace、系统临时目录和 `allow_write` 可写。
- Pickel 配置、agent 定义和 Runtime 自身代码拒绝写入。
- `~/.pickel`、`~/.ssh`、`~/.aws`、GCloud、Kubernetes、Docker 配置及 `deny_read` 拒绝读取。
- 凭据形状的环境变量在 spawn 前剥离。
- 网络行为暂时保持原有兼容语义：允许访问；网络权限拆分后再由独立策略字段控制。

Home 顶层目录名目前仍可枚举。完全隐藏 Home 会影响语言版本管理器、包管理器及用户级开发配置，需作为独立策略变更评估，不能伪装成实现细节。

## 3. Linux

Bubblewrap 使用只读根挂载，再叠加 workspace、`/tmp` 和 `allow_write` 的可写 bind mount。敏感目录使用 `tmpfs` 遮蔽，自保护路径在 workspace bind 后重新以只读方式覆盖。

`--new-session` 必须保留：Bubblewrap 官方说明它可避免未过滤 `TIOCSTI` 时的终端注入风险，同时保证当前持久 PTY 的 job control 语义。

## 4. macOS

Seatbelt profile 使用 deny-by-default：

- 显式允许进程派生、同沙箱信号、只读文件访问、PTY 和常用开发 Runtime 所需的 sysctl/IPC。
- 只为 workspace、临时目录和 `allow_write` 生成写规则。
- 自保护路径生成拒写规则，敏感路径生成拒读规则。
- 路径通过 `sandbox-exec -D` 参数传入 profile，避免把路径拼进 SBPL。
- 固定使用 `/usr/bin/sandbox-exec`，不从 `PATH` 查找安全边界程序。

Apple 已将 `sandbox-exec` 标记为 deprecated，但动态 CLI workspace 无法用静态 App Sandbox entitlement 表达。当前实现因此采用与 Codex 和 Anthropic Sandbox Runtime 相同的现实方案，并通过可用性探测与 `strict` 控制降级。未来若 Apple 移除该入口，桌面发行形态应迁移到签名 helper/App Sandbox。

参考：

- [Apple App Sandbox](https://developer.apple.com/documentation/security/app-sandbox)
- [Bubblewrap 官方仓库](https://github.com/containers/bubblewrap)
- [OpenAI Codex sandboxing](https://github.com/openai/codex/tree/main/codex-rs/sandboxing)
- [Anthropic Sandbox Runtime](https://github.com/anthropic-experimental/sandbox-runtime)

## 5. 失败语义

| 条件 | `strict=false` | `strict=true` |
| --- | --- | --- |
| 平台 backend 不可用 | 警告、裸跑、继续过滤环境变量 | 拒绝创建 Shell |
| profile/参数无法启动 | Shell 返回 Runtime 错误 | Shell 返回 Runtime 错误 |
| 命令被沙箱拒绝 | 命令退出码和 stderr | 同左 |

`sandboxed` 只在 OS backend 已进入启动命令时为 `true`，并同时返回给模型合同和 Runtime 观测数据。

## 6. 验收

- Linux 参数保持只读根、可写 workspace、自保护覆盖和敏感目录遮蔽。
- macOS 实机验证 workspace 可写、Home 其他位置不可写、`deny_read` 不可读。
- Bash、Python、PTY、stderr 分离和超时恢复在 Seatbelt 下可用。
- 非 strict 降级会明确记录 warning；strict 不允许静默裸跑。
- 工具合同中不存在关闭或扩大沙箱权限的参数。
