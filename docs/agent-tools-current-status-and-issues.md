# Agent Tools Current Status and Issues

## 目的

这份文档只记录当前 Agent 工具系统的实际实现状态，以及今天在真实对话中已经暴露出来的问题。

它不是设计文档，也不是修复方案文档。

重点是：

- 当前工具架构是什么
- 当前 Agent 实际暴露了哪些工具
- 当前交互中出现了哪些问题
- 这些问题的根因分别是什么
- 哪些问题后续必须处理

## 当前工具架构

当前工具链路如下：

1. `Agent` 在 `config.yaml` 中声明 `tools`
2. `TurnRunner` 根据 `tool_ids` 从 `ToolRegistry` 中解析工具
3. `TurnRunner` 将工具 `ToolSpec` 传给 provider
4. `GeminiProvider` 将 `ToolSpec` 转成 function calling schema
5. 模型返回 `ToolCall`
6. `TurnRunner` 执行对应工具
7. `ToolExecutionResult` 被写回 `Session`

当前关键实现文件：

- `src/myopenclaw/tools/base.py`
- `src/myopenclaw/tools/catalog.py`
- `src/myopenclaw/tools/filesystem.py`
- `src/myopenclaw/tools/policy.py`
- `src/myopenclaw/tools/shell.py`
- `src/myopenclaw/runtime/runner.py`
- `src/myopenclaw/conversation/message.py`
- `src/myopenclaw/conversation/session.py`

## 当前已实现的工具

当前 builtin catalog 中注册了以下工具：

- `echo`
- `read`
- `write`
- `bash`

其中：

- `read` 和 `write` 依赖 `PathAccessPolicy`
- `bash` 依赖 `ShellSessionManager`

## 当前 Pickle Agent 配置

当前 `Pickle` 配置中的工具为：

- `read`
- `write`
- `bash`

也就是说，当前运行中的 Pickle 已经不再是只有 `read` 的状态。

## 当前各工具的实际行为

### `read`

当前 `read` 是文本文件读取工具，不是目录浏览工具。

它支持：

- 读取 workspace 内文件
- 相对路径解析
- 行范围读取
- 最大字符数截断

它不支持：

- 目录列举
- 把目录当作文件读取

因此 `read(path='.')` 会报目录错误，这是符合当前实现的。

### `write`

当前 `write` 是一个多动作文件编辑工具。

当前实际支持的 `action` 只有：

- `create`
- `overwrite`
- `append`
- `replace`
- `insert`

当前不支持：

- `write`
- `create_file_from_string`
- 其他模型自行猜测出来的 action 名

### `bash`

当前 `bash` 是本地 shell 工具，不是沙箱 shell。

它支持：

- 会话级 `cwd` 记忆
- `restart`
- timeout
- 输出截断

它通过 `/bin/zsh -lc` 执行命令。

## 今天在真实交互中暴露出来的问题

## 问题 1：模型会调用不存在的工具名

真实交互中，模型调用过：

- `run_command(...)`
- `write_to_file(...)`

但当前系统实际只注册了：

- `read`
- `write`
- `bash`

因此 runtime 返回了：

- `Tool 'run_command' is not available.`
- `Tool 'write_to_file' is not available.`

### 根因

- 模型没有被足够明确地约束“只能调用哪些工具名”
- 工具名设计不够强约束，模型容易按常见 agent 经验自行猜测
- 当前没有对工具别名做兼容

## 问题 2：缺少目录浏览工具

真实交互中，模型首先尝试：

- `read(path='.')`

结果报错：

- `Path is a directory`

### 根因

- 当前系统没有 `list_dir` / `ls` 类工具
- 模型在想先观察 workspace 时，只能拿 `read('.')` 试探目录
- `read` 本身并不应该承担目录浏览职责

### 结论

当前工具集不完整。

如果要让 Agent 更稳定地在 workspace 内工作，目录浏览工具是必需项，而不是可选优化。

## 问题 3：`write` 工具的 schema 不足以约束模型

真实交互中，模型调用过：

- `write(action='write', ...)`
- `write(action='create_file_from_string', ...)`

而当前 `write` 实现只接受固定几种 action。

所以 runtime 返回了：

- `Unsupported write action: write`
- `Unsupported write action: create_file_from_string`

### 根因

- 工具名叫 `write`
- 但 schema 里的 `action` 只是普通 `string`
- schema 没有用 `enum` 把合法 action 值钉死
- schema 也没有详细描述每个 action 的语义

### 结论

当前 `write` 工具对模型不够友好。

模型会自然推断：

- 既然工具名叫 `write`
- 那 `action='write'` 应该可用

但实际实现并不接受这个值。

这会导致：

- 模型频繁试错
- token 消耗增加
- 最终回退到 `bash`

## 问题 4：`bash` 会成为兜底写文件工具

在 `write` 两次失败后，模型转而调用：

- `bash(command='cat <<EOF > file ...')`

而当前 `bash` 成功执行了该命令，所以文件最终还是被写出来了。

### 根因

- `write` 工具约束不清
- `bash` 权限过宽
- 模型会把 `bash` 当作万能逃生出口

### 结论

如果 `bash` 比 `write` 更稳定，模型以后会越来越倾向直接用 `bash` 改文件，而绕过结构化写入工具。

这会削弱：

- `write` 的价值
- 结构化编辑能力
- 后续做更细粒度权限控制的空间

## 问题 5：当前 `bash` 权限过大

这是目前最重要的问题之一。

当前 `bash` 的权限模型是：

- 初始 `cwd` 在 workspace
- 纯 `cd xxx` 会被限制在 workspace 内
- 普通 shell 命令没有真正的 sandbox

这意味着：

- `bash` 不是 workspace 沙箱
- `bash` 不是受限文件系统工具
- `bash` 的实际权限接近当前进程所在系统用户权限

### 当前已经具备的限制

- timeout
- 输出截断
- `cd` 的 workspace 边界

### 当前缺失的限制

- 普通命令的 workspace 边界限制
- 命令 allowlist / denylist
- 环境变量白名单
- 文件系统隔离
- 真正的 sandbox

### 结论

当前 `bash` 可用，但不安全。

## 问题 6：工具能力和模型提示之间还没有完全对齐

当前模型在真实交互中暴露出两个倾向：

- 按通用 agent 经验猜工具名
- 按通用 agent 经验猜 `write` 的 action 名

这说明当前工具系统还缺一个关键环节：

- 明确告诉模型当前有哪些工具
- 每个工具该怎么用
- 不要猜不存在的工具名和参数值

### 结论

当前不仅是 runtime 问题，也是 tool schema 和 agent instruction 对齐问题。

## 当前系统可以工作的部分

当前已经能够工作的部分：

- `read` 能正确读 workspace 内文本文件
- `write` 在正确 action 下能工作
- `bash` 能执行命令并保留 session 级 cwd
- runtime 能把 tool result metadata 写回 session
- CLI 能显示 tool call 和 tool result

## 当前最需要优先处理的问题

按优先级排序，当前最需要处理的是：

1. `bash` 权限过大
2. 缺少目录浏览工具
3. `write` schema 约束不足
4. 模型会调用不存在的工具名
5. 工具提示和实际能力没有完全对齐

## 暂不在今天处理的问题

今天先不继续修复，先记录现状。

后续可以单独拆成几个任务：

- 收紧 `bash` 权限模型
- 增加 `list_dir` / `ls` 工具
- 修正 `write` 的 schema
- 决定是否给 `write` 增加 action 别名兼容
- 明确 Agent prompt 中的工具使用约束
