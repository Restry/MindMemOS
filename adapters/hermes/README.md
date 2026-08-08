# Hermes Memory Provider for MindMemOS

这是 Hermes 与 MindMemOS 的**完整 Memory Provider 集成**，不是只添加一个 MCP Server。

## Source of truth

仓库源码：

```text
adapters/hermes/mindmemos/__init__.py
adapters/hermes/mindmemos/plugin.yaml
```

Hermes 运行时安装位置：

```text
$HERMES_HOME/plugins/mindmemos/
```

运行目录是部署产物，仓库文件是唯一源码真源。不要直接修改运行目录后忘记回写仓库。

## Provider responsibilities

`MindMemOSProvider` 实现 Hermes 的 `MemoryProvider` 接口：

- `system_prompt_block()`：通过 MCP `whoami` 注入常驻身份、偏好和高权威规则；
- `prefetch()`：每个 primary turn 前通过 MCP `recall` 自动语义召回；
- MCP 工具：`whoami` / `recall` / `project_rules` / `remember` 供显式调用；
- `sync_turn()`：primary turn 完成后写入本地 durable spool，再异步提交 ingest endpoint；
- `on_memory_write()`：通过 MCP `remember` 镜像 Hermes `memory` tool 的高价值显式写入；
- provenance 检查：拒绝递归捕获已经来自 MindMemOS 的内容。

Agent 召回统一通过受认证的 MindMemOS MCP；completed-turn 自动写入通过 ingest endpoint，短暂断网由本地 spool 重试。Provider 不支持直连 `/v1/memory/search`。

## Install or update

```bash
python3 adapters/hermes/install.py
python3 adapters/hermes/install.py --check
```

安装器只同步 `__init__.py` 和 `plugin.yaml`：

- 不复制或生成 Key；
- 不覆盖 `$HERMES_HOME/mindmemos.json`；
- 不修改 Hermes memory provider 开关；
- 支持 `--hermes-home` 安装到其他 profile。

复制 `mindmemos.example.json` 为 `$HERMES_HOME/mindmemos.json`，再通过安全渠道填写 recall Key 和该 Hermes+机器实例专属 write Key。不要把真实配置提交到 Git。

启用：

```bash
hermes plugins enable mindmemos
hermes config set memory.provider mindmemos
hermes config set memory.memory_enabled false
hermes config set memory.user_profile_enabled false
```

配置变化后重启 Hermes CLI 或 gateway。验证：

```bash
hermes memory status
python3 adapters/hermes/install.py --check
```

期望 `Provider: mindmemos`、`Status: available`，且安装文件与仓库源码一致。

## Configuration

运行配置：

```text
$HERMES_HOME/mindmemos.json
```

主要字段（当前 MCP 主路径）：

- `mcp_url` / `api_key`：受认证的 MindMemOS MCP endpoint；
- `recall_limit`：每轮自动召回上限；
- `ingest_url`：completed-turn ingest endpoint；
- `auto_ingest` / `min_write_chars`：自动写入策略；
- `background_flush`：后台重试本地 spool；
- `request_timeout_seconds`：MCP/ingest 请求超时。

每个 Agent + 机器实例必须使用独立 Key。Key 不得进入源码、命令参数、聊天、日志或 hook payload。

## Difference from MCP

- **MCP**：所有 Agent 共用的唯一记忆工具协议，提供 `whoami` / `recall` / `project_rules` / `remember`。
- **Memory Provider**：Hermes 专用生命周期适配器；仍调用同一个 MCP，只把 `whoami`、每轮 `recall` 和 completed-turn 写入自动化。
- **Companion Skill**：行为建议，告诉不支持 Provider 的 Agent 何时调用工具。

只配置 MCP 不等于 Hermes Memory Provider 已接管；只安装 Skill 也不保证 completed-turn 自动写入。

## Rollback

内置 `MEMORY.md` / `USER.md` 从未删除。回滚：

```bash
hermes config set memory.provider builtin
hermes config set memory.memory_enabled true
hermes config set memory.user_profile_enabled true
```

然后重启 Hermes。确认不再使用后，可执行：

```bash
hermes plugins disable mindmemos
```
