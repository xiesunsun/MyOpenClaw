# S1：Shell 升级 —— 设计稿

> 调研依据：[2026-07-26-tools-sandbox-research.md](2026-07-26-tools-sandbox-research.md) §5（S1 行）。
> 前置：无（与 T1/E1 独立）。后继：S2 sandbox 落在本层之上。
> bracketed-paste 污染已在 `ec78d4a` 根除（bash `--noediting`），不在本稿范围。

## 1. 现状问题清单

| # | 问题 | 现状证据 | 后果 |
| --- | --- | --- | --- |
| 1 | **无输出上限** | `ShellExecutionResult.truncated` 恒 `False`，从未置位 | `cat` 大文件 / build log 整段进 session 与模型上下文，一条结果可吃掉几十 k token |
| 2 | **超时杀整个会话** | `_read_until_marker` 超时路径：`interrupt()` 后紧跟 `terminate()` | 一条慢命令超时 = 丢掉 cwd/env/历史，agent 必须 `shell_restart` 重来 |
| 3 | **同步 exec 阻塞事件循环** | `PersistentShell.exec` 是同步阻塞读，`ShellExecTool.execute`（async）直接调用 | 命令运行期间整个 asyncio loop 卡死；E2 的 streaming/中断上线后此问题会直接暴露 |
| 4 | **无后台任务** | 无 | 长任务（dev server、watch、下载）只能占死唯一 shell 或超时被杀 |
| 5 | **无 stdin 交互** | 无 write 入口暴露给工具层 | 需要确认输入的命令（交互式 CLI、REPL）没法用 |
| 6 | **stdout/stderr 合流** | pty 单流，`stderr` 字段只在异常路径有值 | 模型分不清正常输出与报错；`content = stdout or stderr` 语义粗糙 |
| 7 | **子进程 ANSI 残留** | `_normalize_output` 只删 `\r` | `ls --color`、进度条类输出的转义序列仍进上下文（量级远小于已修的 bracketed-paste，但存在） |
| 8 | **无危险命令拦截** | 无 | `rm -rf /`、`mkfs`、fork bomb 直接执行；S2 之前完全裸奔 |

## 2. 设计原则

1. **会话是宝贵状态**：cwd、env、后台任务都挂在会话上。任何失败路径优先保会话，杀会话是最后手段。
2. **上下文是稀缺资源**：工具结果给模型的是「摘要 + 完整产物的引用」，不是原始洪流——与全局「大输出写文件」的哲学一致。
3. **超时 ≠ 失败**：超时返回部分输出 + 命令继续在前台跑，agent 可选择继续等（`shell_wait`）、喂输入（`shell_stdin`）或打断（`shell_interrupt`）。
4. **S1 不做安全边界**：危险命令拦截只挡「明显自杀」，真正的防线是 S2 sandbox。不给拦截规则做绕过对抗设计。

## 3. 各问题的方案

### 3.1 输出三档上限（问题 1）

```
raw 采集上限   2 MiB   read 循环累计超过即停止采集，标记 truncated_raw
                       （防 yes / 死循环把内存打爆）
结果注入上限   30_000 字符
                       超过时保 head 20_000 + tail 8_000，中间替换为
                       "... [truncated N chars, full output: <path>] ..."
完整落盘       超结果上限时把完整 raw 写 workspace 的
                       .pickel/shell-output/<session>/<ts>-<seq>.log，结果里给路径
```

- 数值进 `OpenViking`——不，进 `AppConfig` 顶层？**都不对**：这是工具行为参数，走 `ShellSessionManager(output_limits=...)` 构造注入，默认值硬编码在 `tools/shell.py`。配置化留给需要时（YAGNI）。
- `metadata.truncated` 置位；`metadata.full_output_path` 给路径。
- 落盘文件不进 git（`.pickel/` 已 ignore）；按会话建目录，`shell_close` 时不删（调试价值），交给用户清理。

### 3.2 超时不杀会话（问题 2）

超时后的降级阶梯，每级都先试保会话：

```
超时到点
  1. SIGINT 前台进程组（不是 shell 本身），等 2s 内出 marker
       → 出了：会话保住，返回 timed_out=True + 已采集输出，shell_status=READY
  2. 没出：SIGKILL 前台进程组，再等 2s
       → 出了：同上
  3. 还没出（shell 自身挂了/卡死）：terminate 整个会话，shell_status=TERMINATED
```

- 前台进程组 id：pty 上 `os.tcgetpgrp(master_fd)`；等于 shell 自身 pgid 时跳过 kill（没有前台命令在跑）。
- 现状的 `interrupt()`（SIGINT 发给 shell 的进程组）是错的对象——它把信号发给 `start_new_session` 后的整个会话组，等于连 shell 一起打。改为对前台组。

### 3.3 exec 异步化（问题 3）

`ShellExecTool.execute` 里 `await asyncio.to_thread(session.shell.exec, ...)`。`PersistentShell` 内部保持同步实现不动（select 循环在工作线程里跑）。`_running` 标志已挡并发重入。

### 3.4 超时后的前台交互三件套（问题 5 + 2 的延伸）

超时返回后前台命令还活着（3.2 第 1 级失败前不杀），新增三个小工具：

| 工具 | 语义 |
| --- | --- |
| `shell_wait` | 继续等当前前台命令，直到 marker 或再次超时（参数 `timeout_ms`）。返回增量输出 |
| `shell_stdin` | 向前台命令写入文本（参数 `text`，可选 `newline=True`）。返回写入后短窗口内的增量输出 |
| `shell_interrupt` | 对前台进程组发 SIGINT（可选 `kill=True` 升级 SIGKILL）。返回增量输出与会话状态 |

- 三者共享「会话处于前台命令未完成」状态：`PersistentShell` 增 `pending_marker: str | None`，exec 超时（保住会话时）不清空，三件套据此续读。
- `shell_exec` 在 pending 状态被调用时报错并提示用三件套（或 `shell_interrupt` 后重试），不排队——排队语义复杂且模型容易困惑。

### 3.5 后台任务（问题 4）

**不复用会话 shell，每个后台任务独立 spawn**（独立 pty + 进程组），由 `ShellSessionManager` 记账：

```
shell_exec 增参数 background: bool = False
  → spawn 独立 PtyShellProcess 跑该命令，立即返回 task_id
  → 输出持续采集到环形缓冲（上限同 3.1 raw 档）+ 全量落盘同 3.1

新工具 shell_tasks：列出本会话后台任务（task_id / 命令 / 状态 / 运行时长）
新工具 shell_output：取指定 task_id 的增量输出（参数 since_offset 可选）
新工具 shell_kill：终止指定 task_id（SIGTERM → SIGKILL 阶梯）
```

- 独立 spawn 的理由：会话 shell 的 `&` 后台作业与前台命令共享 pty 输出流，输出交错无法归属；独立 pty 天然隔离，kill 语义也干净。
- 任务归属会话，`shell_close` / 会话结束时全部终止。
- 采集线程：每任务一个 reader 线程（`threading.Thread` + daemon），写环形缓冲；不引入 asyncio 任务（工具层同步读缓冲即可）。

### 3.6 stdout/stderr 分离（问题 6）

**stderr 走独立 pipe，stdout 留 pty**：

```python
self._process = subprocess.Popen(..., stdin=slave_fd, stdout=slave_fd,
                                 stderr=subprocess.PIPE, ...)
```

- 子进程视角：stdout 是 tty（保色彩/交互探测语义）、stderr 不是——绝大多数 CLI 按 stdout 判断 tty，行为不变。
- 读循环同时 select master_fd 与 stderr pipe，分别累计。
- 结果组装：`content` = stdout；stderr 非空时以 `--- stderr ---\n...` 附加在 content 尾部（模型一眼可辨），`metadata.stderr_chars` 记长度。上限与 3.1 共享总额度。
- **代价声明**：marker 写在 stdout（wrapped command 的 printf），stderr 无 marker——命令结束以 stdout marker 为准，结束后再 drain stderr 一个短窗口（50ms）。stderr 与 stdout 的相对顺序不保证（本来 pty 合流也不保证到字节级）。

### 3.7 ANSI 清洗（问题 7）

`_normalize_output` 增一层 CSI/OSC 序列剥离：

```python
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[a-zA-Z]|\][^\x07\x1b]*(?:\x07|\x1b\\))")
```

- 只剥转义序列，不动正文；`\r` 处理保持现状。
- 进度条类回写（`\r` 覆盖行）已被现状的 `\r` 删除策略压扁，不另做。

### 3.8 危险命令拦截（问题 8）

静态规则表，在 `ShellExecTool.execute` 入口检查（后台任务同样过）：

| 规则 | 例 |
| --- | --- |
| 根/家目录递归删除 | `rm -rf /`、`rm -rf ~`、`rm -rf $HOME`（含变体空格/引号） |
| 磁盘/设备写 | `mkfs`、`dd of=/dev/`、`> /dev/sd` |
| fork bomb | `:(){ :\|:& };:` 模式 |
| 全盘 chmod/chown | `chmod -R 777 /`、`chown -R ... /` |

- 命中→返回 is_error + 说明 + 「如确需执行，请用户在终端自行运行」。
- **正则挡君子**：不做 shell 解析级对抗（`$(echo rm) -rf /` 挡不住也不试图挡），真防线在 S2。规则表为模块级常量，S2 时移交 sandbox 策略层。

## 4. 工具面变化汇总

| 工具 | 变化 |
| --- | --- |
| `shell_exec` | 增 `background` 参数；超时语义变（保会话 + 可续）；输出截断 + 落盘引用；危险命令拦截 |
| `shell_restart` / `shell_close` | 不变（close 顺带杀后台任务） |
| `shell_wait` / `shell_stdin` / `shell_interrupt` | 新增（前台交互三件套） |
| `shell_tasks` / `shell_output` / `shell_kill` | 新增（后台任务三件套） |

新工具全部进 `builtin_tools()`（T1 总线自动纳管）；agent.yaml 白名单需对应增补——默认 agent 配置一并更新。

## 5. 改动清单

| 文件 | 改动 |
| --- | --- |
| `tools/shell.py` | 核心：超时阶梯、pending 状态、stderr pipe、截断落盘、ANSI 剥离、后台任务记账、六个新工具类 |
| `tools/catalog.py` | 注册六个新工具 |
| `agents/Pickle/agent.yaml` | 白名单增补 |
| `tests/tools/test_shell.py` | 新行为用例（详见 §7） |

`ToolServices` / 总线 / Run 均不动——S1 完全落在工具层内。

## 6. 与 S2 的边界

- S1 交付的是**能力与可控性**（不丢会话、可交互、可后台、输出可控）；S2 交付**安全边界**（bubblewrap、网络 allowlist、凭据 mask、backend 抽象）。
- 3.8 的规则表在 S2 移交 sandbox 策略层；3.5 的独立 spawn 点是 S2 backend 抽象的天然切入口（每个 spawn 换成 backend.spawn）。

## 7. 测试计划

| 面 | 用例 |
| --- | --- |
| 截断 | 超结果上限→head/tail 保留 + 路径引用 + truncated 置位；超 raw 上限→采集停止 |
| 超时保会话 | sleep 超时→SIGINT 后会话仍 READY、cwd 保持；SIGINT 免疫命令→SIGKILL 阶梯；shell 卡死→TERMINATED |
| 三件套 | 超时后 shell_wait 拿到 marker；shell_stdin 喂 read 命令；shell_interrupt 恢复 READY |
| 后台 | background 返回 task_id；shell_output 增量读；shell_kill 终止；close 清理全部任务 |
| stderr | stdout/stderr 分离归属；stderr 附加格式；只有 stderr 时 content 形态 |
| ANSI | 彩色输出剥离；OSC 标题序列剥离 |
| 拦截 | 规则表逐条命中；正常命令不误伤（`rm -rf ./build` 应放行） |
| 阻塞 | exec 期间事件循环可响应其他任务（to_thread 生效） |

## 8. 遗留取舍

1. **`shell_exec` 的 pending 状态拒绝新命令而非排队**——排队让「当前在跑什么」变得模型不可见，宁可显式报错引导。
2. **后台任务输出环形缓冲 + 落盘，不推事件**——E2 的事件总线上线后可考虑把后台任务完成推成 runtime 事件，本稿不做。
3. **stderr 分离牺牲字节级顺序**——pty 合流本也不保证；换来的归属清晰对模型价值更大。
4. **上限值硬编码不配置化**——需要再说（YAGNI）。
