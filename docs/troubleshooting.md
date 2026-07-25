# 常见问题排查（平凡但难定位）

**位置约定**：本文件放在 `docs/troubleshooting.md`。  
开源项目常见放法见文末；本仓库把「使用/运行期踩坑」放 `docs/`，与 `docs/upgrade/`（设计合同）分开。

---

## 1. 启动即崩：`socksio` / SOCKS proxy

**现象**

```text
ImportError: Using SOCKS proxy, but the 'socksio' package is not installed.
Make sure to install httpx using `pip install httpx[socks]`.
```

**原因**

- Shell 里配置了 SOCKS（`ALL_PROXY` / `HTTPS_PROXY` / `HTTP_PROXY` = `socks5://...`）
- Anthropic SDK → **httpx**，默认 `trust_env=True`，会读系统代理
- 走 SOCKS 需要可选包 **`socksio`**；未安装时在**创建客户端**阶段就失败，请求尚未发出

**说明**

- 这是**传输层**代理，不等于 API 的 `base_url`
- `config.yaml` 里 `api_base: https://api.anthropic.com` 时，目标仍是官方；只是出网可能经 SOCKS

**处理**

```bash
# 项目已声明依赖 httpx[socks] 时
uv sync

# 或临时不用系统代理启动
env -u ALL_PROXY -u HTTPS_PROXY -u HTTP_PROXY -u all_proxy -u https_proxy -u http_proxy \
  uv run pickel
```

---

## 2. 以为走了代理 / 以为没走官方：`ANTHROPIC_BASE_URL` vs `api_base`

**现象**

- 环境有 `ANTHROPIC_BASE_URL=http://...`（本地 CLI 代理等）
- 误以为 Pickel 一定打到该地址，或反过来以为一定直连

**原因（Anthropic Python SDK 行为）**

| 构造客户端时 | `base_url` 来源 |
|-------------|----------------|
| **未传** `base_url` | 读环境变量 `ANTHROPIC_BASE_URL`；没有则默认 `https://api.anthropic.com` |
| **传了** `base_url` | **只用传入值**，覆盖 env |

Pickel 从配置读取 `api_base` 并传给 `AsyncAnthropic(base_url=...)`。  
因此 **`config.yaml` 的 `api_base` 优先于 `ANTHROPIC_BASE_URL`**。

**处理**

- 要走官方：保持 / 设置  
  `providers.anthropic.models.<model>.api_base: https://api.anthropic.com`
- 要走自建网关：把同一字段改成网关 URL（或 `${ANTHROPIC_BASE_URL}`）
- 自查实际客户端：

```bash
uv run python -c "
from pickel.app.boot import Boot
from pickel.config.loader import Config
from pickel.providers.factory import create_llm_provider
cfg = Boot.from_config(Config.load()).app_config
mc = cfg.resolve_model_config(cfg.agents[cfg.default_agent].llm)
p = create_llm_provider(mc)
print('api_base=', mc.api_base)
print('client.base_url=', p.client.base_url)
"
```

---

## 3. Anthropic `403 Request not allowed`

**现象**

```text
anthropic.PermissionDeniedError: Error code: 403
{'error': {'type': 'forbidden', 'message': 'Request not allowed'}}
```

Chat UI 已起来，**第一次 generate** 时失败。

**原因**

- 请求已到达 **当前 `base_url` 对应服务端**，被拒绝（权限 / 账号策略 / 区域 / key 类型等）
- **不是** Session 库、ReAct、CLI 组装逻辑的典型故障
- 连 `GET /v1/models` 也 403 时，多半是 key/账号对当前 endpoint 整体不可用，而非单一 model 名拼错

**排查顺序**

1. 确认实际 `client.base_url`（见上一节）
2. 同一 key、同一 base 用最小脚本复现（绕过 Pickel）
3. 区分：官方 403 vs 网关 401/502（key 不匹配、模型未接入）

**处理**

- 官方：换有 API 权限的 key、核对账号与出口网络
- 网关：key、模型 id 必须以网关侧为准，不能照搬官方 model 名

---

## 4. 会话库仍是旧 schema / 行为怪异

**现象**

- 升级 harness 后仍像旧会话模型
- 或读写报缺列 / 缺表相关错误

**原因**

- 会话库路径：`~/.pickel/sessions.db`（或 `PICKEL_HOME` 下的 `sessions.db`）
- 当前 schema：`user_version=3`（`sessions` + `session_entries`）
- `_ensure_schema` 使用 `CREATE TABLE IF NOT EXISTS`：**不改已有旧表结构**

旧库特征示例：`user_version=0`，存在 `session_messages`，无 `session_entries`。  
更早版本曾把库放在项目旁 `.pickel/sessions.db`；`pickel migrate` 可读该旧路径并导入全局库。

**处理**

```bash
# 弃用全局旧库（可先备份）
mv ~/.pickel/sessions.db ~/.pickel/sessions.db.bak-old
# 再启动 pickel，会按 v3 建空库
uv run pickel
```

旧会话不会自动导入（除非走 migrate 流程）。

---

## 5. 启动命令速查

```bash
uv run pickel
uv run pickel chat
uv run pickel chat --agent Pickle
uv run pickel chat --session-id <id>
uv run pickel sessions
uv run pickel sessions delete <id>
# 兼容旧 yaml：uv run pickel chat --config config.yaml
```

默认从分层配置发现（`~/.pickel` + 项目 `.pickel` / `agents`）。  
入口：`pickel` → `pickel.cli.main`。

---

## 分层对照（避免张冠李戴）

| 层级 | 典型信号 | 先查什么 |
|------|----------|----------|
| 依赖 / 传输 | 启动 traceback、`socksio`、连接超时 | 代理 env、`uv sync`、httpx socks |
| 路由 / endpoint | 打到了意外主机 | `api_base` vs `ANTHROPIC_BASE_URL` |
| API 权限 | 403 / 401 / 网关 unknown model | key、账号、模型 id、网关目录 |
| 本地状态 | 会话/schema 异常 | `~/.pickel/sessions.db` 是否旧库 |

---

## 附录：这类文档在开源项目里一般放哪

| 放法 | 常见场景 | 例子倾向 |
|------|----------|----------|
| **`docs/troubleshooting.md`** | 运行期踩坑、环境/依赖/配置 | 中大型、已有 `docs/` 的项目（**本仓库采用**） |
| **`docs/faq.md`** | 产品向问答（怎么用、概念） | 与 troubleshooting 可并存；FAQ 偏「问」，troubleshooting 偏「炸了怎么拆」 |
| **仓库根 `TROUBLESHOOTING.md` / `FAQ.md`** | 小项目、希望根目录一眼看到 | 根目录文件少时合适 |
| **README 一节** | 只有 2～3 条高频问题 | 条目变多后应拆出 |
| **GitHub Wiki** | 频繁改、非正式版本管理 | 协作随意时有用；难做 PR 审文档 |
| **`.github/ISSUE_TEMPLATE` + 链到 docs** | 报 bug 前自检 | 与 troubleshooting 互补，不替代正文 |

**本仓库选择**：`docs/troubleshooting.md`  
- 与 `docs/upgrade/`（设计合同）同层、不同职责  
- 根目录保持 README / LICENSE / 配置，不堆过程文档  
- 需要时在 README「故障排查」加一行链接即可  
