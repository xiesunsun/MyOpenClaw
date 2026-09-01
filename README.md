# Pickel

Pickel 是一个本地优先、受控执行、可观测、可扩展的 Agent Runtime。

## 文档入口

Runtime 重构的当前合同只有下面七份文档。涉及实体、方法、字段、持久化、恢复或 Runtime 边界时，先按「命名 → 实体决策 → 领域合同 → 实施计划」阅读；其他文档不能覆盖这些合同。

| 文档 | 唯一职责 |
| --- | --- |
| [Agent Runtime 重构命名约束](docs/upgrade/2026-08-10-agent-runtime-naming.md) | 唯一术语、组件职责和历史名称迁移 |
| [Runtime 实体决策](docs/upgrade/2026-08-24-runtime-entity-decisions.md) | 已确认实体、值对象和跨领域边界 |
| [数据库实体设计](docs/upgrade/2026-07-12-db-entities.md) | SQLite v14 表、字段、约束、事务和迁移 |
| [Operation 持久化与恢复模型](docs/upgrade/2026-08-11-operation-recovery-model.md) | Operation、AgentRunState、Intent、审批、取消和恢复 |
| [配置系统升级设计](docs/upgrade/2026-07-25-config-system-design.md) | Pickel 配置来源、分层合并、SecretRef 和 Package 输入 |
| [Agent Runtime 重构实施计划](docs/upgrade/2026-08-24-agent-runtime-refactoring-plan.md) | 重构批次、实施顺序和验收门槛 |
| [观测驱动 Runtime 评审结论](docs/upgrade/2026-08-27-observation-driven-runtime-findings.md) | 当前观测口径、Context 压缩、Parent/Child 驱动和性能治理 |

### 历史与领域参考

除上面七份当前合同外，`docs/upgrade/` 中的设计稿和实施计划、`docs/superpowers/` 中的规格/计划，以及 `docs/openviking/` 和 [排障手册](docs/troubleshooting.md) 都是历史记录或独立领域参考。它们不会改变当前 Runtime 的实体和边界。

其中 [Query → Context → Chat Completion 升级设计](docs/upgrade/2026-07-12-query-context-harness.md) 与 [模型请求组装设计](docs/upgrade/2026-07-25-request-prepare-design.md) 保留阶段性数据流和实现背景，但文内旧的 Runtime 命名已由当前命名合同替代。Tool、Extension、MCP、Shell、Skill 或 Anthropic 协议等独立领域，只有在当前合同未覆盖具体细节时才参考对应历史文档。

## 开发校验

本地与 CI 使用同一组命令：

```bash
uv sync --locked --dev
uv run black --check src tests
uv run ruff check src tests
uv run pytest -q --cov=pickel --cov-report=term --cov-fail-under=75
```
