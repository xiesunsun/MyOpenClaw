# 配置与运行层升级 — 实施计划

> **For agentic workers:** 按任务顺序实现；步骤用 checkbox 跟踪。设计依据：`docs/upgrade/2026-07-25-config-system-design.md`。  
> 相关：`docs/upgrade/2026-07-12-db-entities.md`（Session 表需增 `cwd`）。

**Goal:** 将单文件只读 `config.yaml` + 项目旁会话库，升级为 pickel 分层配置、全局会话库与目录过滤，并收口运行层命名（Boot / Run / Provider）。

**Architecture:** 磁盘分层（Settings / Models / Auth / Agents）由 Config 合并为 AppConfig；Boot 组装 Run；会话落 `~/.pickel/sessions.db`，列表默认按 `cwd` 过滤；Environ 为进程覆盖；ExecutionStrategy 保留。

**Tech Stack:** Python 3.12、Pydantic、Typer、SQLite、现有 pytest。

**产品路径（确认）：** CLI `pickel`，家目录 `~/.pickel/`，项目 `.pickel/`；代码包暂仍 `pickel`，包更名 `pickel` 放 **P3**。

---

## 文件地图（目标）

| 路径 | 职责 |
|------|------|
| `src/pickel/config/paths.py` | `home_dir()` → `~/.pickel`；`discover_project_root(cwd)`；会话库路径 |
| `src/pickel/config/settings.py` | Settings 读写 global/project JSON |
| `src/pickel/config/models.py` | Models 读 models.json |
| `src/pickel/config/auth.py` | Auth 读 auth.json |
| `src/pickel/config/agents.py` | Agents 扫 `agents/<id>/` |
| `src/pickel/config/environ.py` | Environ 进程覆盖（内存） |
| `src/pickel/config/loader.py` | Config：合并 → AppConfig |
| `src/pickel/config/migrate.py` | 旧 config.yaml → 分层文件 + 迁库 |
| `src/pickel/config/app_config.py` | AppConfig 只读结果（改加载来源，保留类型名） |
| `src/pickel/app/boot.py` | Boot（替 AppAssembly） |
| `src/pickel/runs/run.py` | Run.open / Run.turn（替 RuntimeContext+Deps+Coordinator） |
| `src/pickel/providers/base.py` | Provider（原 BaseLLMProvider） |
| `src/pickel/cli/main.py` | 默认无 `--config`；sessions `--all` |
| `agents/<id>/agent.yaml` | Agent 定义（P1） |

**删除（目标态）：** `AgentRuntimeContext`、`RunDependencies` 独立文件语义、`AgentCoordinator`、`Default*Resolver`、`AppAssembly`（改名后删旧文件）。

**保留：** `ExecutionStrategy`、`ReActStrategy`、`ChatLoop`。

---

## 阶段总览

```text
P0  可跑：新加载 + 全局 db + cwd + Boot/Run.open
P1  Agent 目录 + migrate
P2  Environ 写回 + Run.turn + Provider 改名 + /model
P3  去旧路径 + 包名 pickel（可选独立 PR）
后话 PlanExecute / Reflection / dynamic workflow
```

每阶段结束：`uv run pytest` 全绿；本仓库可用 `pickel chat` 起对话。

---

## P0 — 配置加载 + 全局会话 + Boot/Run 雏形

### Task 0.1: 路径与家目录

**Files:**
- Create: `src/pickel/config/paths.py`
- Create: `tests/config/test_paths.py`

- [ ] **Step 1: 测试** `home_dir` 默认 `Path.home() / ".pickel"`；`sessions_db_path()` 指向其下 `sessions.db`；`discover_project_root` 向上找含 `.pickel` 或 `agents` 的目录。

- [ ] **Step 2: 实现** `paths.py`（可用环境变量 `PICKEL_HOME` 覆盖家目录，便于测试）。

- [ ] **Step 3: 跑测**

```bash
uv run pytest tests/config/test_paths.py -v
```

- [ ] **Step 4: Commit** `feat(config): 增加 pickel 路径发现`

---

### Task 0.2: Settings / Models / Auth 读取与合并

**Files:**
- Create: `src/pickel/config/settings.py`
- Create: `src/pickel/config/models_file.py`（或 `models.py`，避免与 model_config 混淆时用 `models_catalog.py`）
- Create: `src/pickel/config/auth.py`
- Create: `src/pickel/config/loader.py`
- Modify: `src/pickel/config/app_config.py` — `AppConfig` 仍为结果类型；增加从 dict 构建；**去掉对单 yaml 的唯一依赖**（yaml 适配进 migrate/兼容层）
- Test: `tests/config/test_loader.py`、扩展 `tests/config/test_app_config.py`

**合并顺序：** 内置默认 < global settings/models/auth < project settings/models。

- [ ] **Step 1: 测试** 在 tmp `PICKEL_HOME` 写 settings/models/auth，project 覆盖 `default_agent`，断言 `Config.load()` 得到正确 AppConfig；`${ENV}` 展开与现逻辑一致。

- [ ] **Step 2: 实现** 各模块；`Config.load(cwd=..., home=...)` → `AppConfig`。

- [ ] **Step 3: 兼容** `AppConfig.load(path: Path)` 若指向旧 yaml：内部调用 migrate-to-memory 或专用 `load_legacy_yaml`，**仅 P0 过渡**，标注 deprecated。

- [ ] **Step 4: pytest** `tests/config/ -v`

- [ ] **Step 5: Commit** `feat(config): Settings/Models/Auth 分层加载`

**P0 字段最小集（与现 config.yaml 对齐）：**

```text
settings: default_agent, default_llm, default_file_access_mode,
          default_skills_path, react_max_steps, context_cli_turn_window, openviking(策略)
models: providers.*.models.* 能力字段
auth: providers.*.api_key/api_base, openviking 密钥
agents: P0 仍可读「legacy yaml 内 agents 段」或临时仍从兼容加载；目录扫描 P1
```

---

### Task 0.3: Session 增加 `cwd`

**Files:**
- Modify: `src/pickel/conversations/session.py` — 字段 `cwd: str`
- Modify: `src/pickel/conversations/session_storage_mapper.py`
- Modify: `src/pickel/conversations/session_preview.py`（列表展示可选 cwd）
- Modify: `src/pickel/persistence/sqlite_session_repository.py` — 建表/读写 `cwd`；**新库 schema**：在 user_version 策略上 **升到 3**（设计原文 user_version=2；本计划规定 **version=3 含 cwd**）。空库直建；旧库不做自动迁移（与 db-entities「不做旧库兼容」一致），迁移命令负责导入。
- Modify: `docs/upgrade/2026-07-12-db-entities.md` — 补 `cwd` 列与索引 `(cwd, updated_at)`
- Test: `tests/persistence/test_sqlite_session_repository.py`、`tests/conversations/test_session*.py`

- [ ] **Step 1: 测试** create/load 带 `cwd`；`list` 可按 cwd 过滤。

- [ ] **Step 2: 实现** Session / mapper / SQLite。

- [ ] **Step 3: pytest** 相关测试。

- [ ] **Step 4: Commit** `feat(session): sessions 表增加 cwd`

---

### Task 0.4: Sessions 全局路径 + 列表过滤

**Files:**
- Modify: `src/pickel/app/assembly.py`（即将变 Boot）— `db_path = paths.sessions_db_path()`，**不再** `root / .pickel / sessions.db`
- Modify: `src/pickel/conversations/service.py` — `start` 写入当前 cwd；`list_sessions(cwd=..., all=False)`
- Modify: `src/pickel/cli/main.py` — `sessions` 默认 cwd 过滤；`--all`
- Test: `tests/cli/test_main_sessions.py`、`tests/conversations/test_session_service.py`

- [ ] **Step 1: 测试** 两个 cwd 各建 session；默认 list 只见当前 cwd；`--all` 全见。

- [ ] **Step 2: 实现**。

- [ ] **Step 3: pytest**

- [ ] **Step 4: Commit** `feat(session): 全局 ~/.pickel/sessions.db 与 cwd 过滤`

---

### Task 0.5: Run.open 合并 RuntimeContext；Boot 雏形

**Files:**
- Create: `src/pickel/runs/run.py` — `Run` dataclass + `open(agent, ...)` + 暂保留 `to` 兼容字段供 strategy 使用
- Modify: `src/pickel/runs/strategy/base.py`、`react.py` — 参数 `deps: RunDependencies` → `run: Run`（或别名属性保持过渡一个 PR 内改完）
- Modify: `src/pickel/runs/coordinator.py` — P0 可仍用 Coordinator 调 strategy，但 **构造 deps 只来自 Run**；或 P0 已把 `run_turn` 挂到 Run（优先 **一步到位 Run.turn** 若测试成本可接受）
- Delete/停止导出: `AgentRuntimeContext`、`DefaultProviderResolver`、`DefaultToolResolver`
- Create: `src/pickel/app/boot.py` — `Boot.from_config(...)` / `from_legacy_yaml(path)`
- Modify: `src/pickel/app/assembly.py` — 薄包装转调 Boot **或直接删除并改所有引用**
- Modify: `src/pickel/cli/chat.py`、`cli/main.py`、`tests/app/test_assembly.py`、`tests/runs/*`

**建议 P0 对 Coordinator：** 实现 `Run.turn`，`AgentCoordinator` 删除；ChatLoop 调 `run.turn`。

- [ ] **Step 1: 测试** 现有 `tests/runs/test_runner.py`、`test_events.py`、`test_react_checkpoint.py` 改为构造 `Run`。

- [ ] **Step 2: 实现 Run + 改 strategy 签名 + 删 RuntimeContext。**

- [ ] **Step 3: Boot 替换 Assembly 引用。**

- [ ] **Step 4: `uv run pytest` 全绿**

- [ ] **Step 5: Commit** `refactor(run): Run.open/turn 取代 RuntimeContext 与 Coordinator`

---

### Task 0.6: P0 验收

- [ ] 本仓库：`PICKEL_HOME` 可指向 tmp；或写入真实 `~/.pickel` 后：

```bash
uv run pickel chat --agent Pickle
# 或过渡：uv run pickel chat --config config.yaml
uv run pickel sessions
uv run pickel sessions --all
```

- [ ] 确认新 session 落在 `~/.pickel/sessions.db`（或测试 home）
- [ ] Commit 若有文档小改：`docs: P0 验收说明`

**P0 出口标准：** 不依赖项目旁 `.pickel/sessions.db`；分层文件可读（或 yaml 兼容加载）；chat + sessions 过滤可用。

---

## P1 — Agents 目录 + 迁移命令

### Task 1.1: Agents 扫描 `agents/<id>/`

**Files:**
- Create: `src/pickel/config/agents.py`
- Create: 本仓库 `agents/Pickle/agent.yaml`（从 config.yaml 抽出）
- Modify: `Config.load` 合并 agent 定义
- Test: `tests/config/test_agents.py`

`agent.yaml` 字段：`workspace_path`、`tools`、`file_access_mode`、`llm`、`remote_agent_id`、`skills_path`；行为读 `AGENT.md`。

- [ ] 测试扫描与解析
- [ ] 实现
- [ ] Commit `feat(config): Agents 目录发现`

---

### Task 1.2: `pickel config migrate`

**Files:**
- Create: `src/pickel/config/migrate.py`
- Modify: `src/pickel/cli/main.py` — subcommand `config migrate --from config.yaml`
- Test: `tests/config/test_migrate.py`（tmp 目录，不写用户真 home，用 `PICKEL_HOME`）

迁移步骤：

1. 解析旧 yaml  
2. 写 `$PICKEL_HOME/{settings,models,auth}.json`（auth 已存在则合并不覆盖密钥）  
3. 写 `agents/*/agent.yaml`  
4. 若存在项目 `.pickel/sessions.db`：导入到全局库并补 `cwd=project_root`，源文件改名 `.bak`  
5. `config.yaml` → `config.yaml.bak`

- [ ] 测试端到端 migrate  
- [ ] 实现  
- [ ] 对本仓库执行 migrate（开发者环境，确认后）  
- [ ] Commit `feat(cli): pickel config migrate`

---

### Task 1.3: CLI 默认走 Config 发现

**Files:**
- Modify: `cli/main.py` — `--config` 可选；默认 `Config.load(cwd)`
- Modify: README 启动示例（用户未要求大改文档时可只改最小命令示例）

- [ ] 无 `--config` 能在已 migrate 的环境启动  
- [ ] Commit `feat(cli): 默认分层配置发现`

**P1 出口：** 可不依赖 `config.yaml` agents 段；migrate 可重复跑（幂等或明确报已迁移）。

---

## P2 — Environ、写回、Provider 改名

### Task 2.1: Environ

**Files:**
- Create: `src/pickel/config/environ.py`
- Modify: `Run` — 持有 `Environ`；`resolve_model_config` 叠 Environ
- Test: `tests/config/test_environ.py`、runs 相关

- [ ] 进程内改 model/thinking 影响下一次 generate，不写盘  
- [ ] Commit `feat(config): Environ 进程覆盖`

---

### Task 2.2: Settings 写回

**Files:**
- Modify: `settings.py` — `set(key, value, scope=global|project)` + 文件锁（`fcntl` 或原子写 tempfile+replace）
- Modify: ChatLoop 斜杠命令或 CLI：`/model` 设 Environ；`/model default` 写 Settings（具体 UX 实现时定一条）
- Test: `tests/config/test_settings_write.py`

- [ ] 写回后 reload 可见  
- [ ] Commit `feat(config): Settings 可写回`

---

### Task 2.3: Provider 改名

**Files:**
- Modify: `providers/base.py` — `class Provider`
- Modify: 所有 `BaseLLMProvider` 引用与测试 stub  
- Modify: `providers/__init__.py` 导出

- [ ] `rg BaseLLMProvider` 应为 0  
- [ ] pytest providers + runs  
- [ ] Commit `refactor(provider): BaseLLMProvider 更名为 Provider`

---

### Task 2.4: P2 验收

- [ ] chat 内切换 model（Environ）  
- [ ] 设默认写入 `~/.pickel/settings.json`  
- [ ] `uv run pytest` 全绿  

---

## P3 — 清理与包名

### Task 3.1: 扫掉旧路径与旧 API

- [ ] 删除 `AppConfig.load` 旧 yaml 主路径或移到 `migrate` only  
- [ ] 代码中无 `.pickel` 硬编码（除 migrate 读旧库）  
- [ ] README / troubleshooting 路径改 `~/.pickel`  
- [ ] Commit `chore: 移除 pickel 数据路径与旧 config 主路径`

### Task 3.2: 包名 `pickel` → `pickel`（独立大 PR）

- [ ] 目录 `src/pickel/`，改 pyproject packages 与 script  
- [ ] 全量 import 替换  
- [ ] 可保留一版 deprecation 说明，**不**长期双包  
- [ ] Commit `refactor: 包名迁移为 pickel`

**P3 可与功能迭代拆开，不阻塞 P0–P2。**

---

## 测试策略

| 类型 | 做法 |
|------|------|
| 单元 | tmp_path + `PICKEL_HOME` |
| 不写真实 home | 默认测试必须隔离 |
| 回归 | 每 Task 后相关文件；每阶段全量 `uv run pytest` |
| 手工 | migrate 后 chat / sessions / --all |

---

## 风险与顺序约束

```text
0.3 cwd  schema  → 0.4 全局库过滤
0.2 Config       → 0.5 Boot
0.5 Run          → strategy 签名
1.1 Agents       → 1.2 migrate 写出 agent.yaml
2.x 依赖 P0/P1 可跑
3.2 包名最后
```

- OpenViking bypass 表仍可与 sessions.db **同库文件**（全局路径下），注意连接路径统一。  
- 不做旧 SQLite user_version 自动升级；migrate 导入或空库。  
- Strategy 扩展（PlanExecute 等）**不在本计划**。

---

## 提交信息约定

- 中文或 `feat(config):` / `refactor(run):` 前缀  
- 小步提交，对应 Task  

---

## 执行方式

计划就绪后可选：

1. **Subagent 逐 Task** — 每任务新代理 + 复查  
2. **本会话顺序执行** — checkpoint 在阶段末  

---

## 验收清单（全部完成后）

- [ ] `~/.pickel/{settings,models,auth,sessions.db}` 为权威位置  
- [ ] 列表默认 cwd 过滤；`--all` 全库  
- [ ] 无 AgentRuntimeContext / RunDependencies / AgentCoordinator / BaseLLMProvider  
- [ ] 有 Boot、Run、Provider、Environ、Config  
- [ ] ExecutionStrategy / ReActStrategy / ChatLoop 仍在  
- [ ] `pickel config migrate` 可从旧 yaml 迁入  
- [ ] 测试全绿  
