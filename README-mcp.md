# MindMemOS MCP、durable ingestion 与 provenance 运维

## 数据流

```text
primary Agent completed turn
  → runtime adapter local spool
  → POST :8765/ingest/turn (instance write Key)
  → ~/.hermes/mindmemos_turn_ingest.sqlite3
  → retry worker
  → POST :8000/v1/memory/add
  → MindMemOS extraction/dedup
  → provenance sidecar keyed by memory_id
  → :8666 panel chips + filters
```

MCP tools、companion Skill、runtime adapter 是三层独立能力：

- MCP 提供 `whoami`、`recall`、`project_rules`、`remember` 等工具。
- Skill 规定 Agent 何时读写；它是 advisory，不保证所有 Agent 自动执行。
- Runtime adapter 才是可靠自动写入层。没有 adapter 时只能依赖显式 `remember`。

## Credential principal

每个 Agent + 机器实例使用独立 Key。`mcp_tokens.py` 只保存 SHA-256；明文只在签发时返回一次。

Token record 包含：

- stable `client_id`（credential rotation 时复用）
- `agent_kind`
- machine `instance`
- credential id
- display name
- `read` / `write` scope
- authority

旧 JSON token record 与 legacy single-token file 仍可用，服务端给它们明确的 `legacy_fallback` identity。MCP `remember` 不接受调用方自报的 `client`；`app_id` 与 `agent_id` 由 :8765 根据 Key 注入。

内网 :8666 的「访问令牌」页负责 issuance/revoke/rotation。localhost、RFC1918（包括 `192.168.1.223`）与 IPv6 ULA 可访问；公网 :8765 没有 issuance route。

## Durable collector

默认 ledger：

```text
~/.hermes/mindmemos_turn_ingest.sqlite3
```

状态：`pending → processing → done`；失败进入 `error`，到期后按指数退避重新 claim。进程中断留下的 stale `processing` 会恢复成 `error`。

查看状态：

```bash
python3 /Users/leway/Projects/MindMemOS/turn_ingest.py status
```

手动唤醒 error：

```bash
python3 /Users/leway/Projects/MindMemOS/turn_ingest.py retry
```

清理已完成事件：

```bash
python3 /Users/leway/Projects/MindMemOS/turn_ingest.py cleanup
```

默认策略：

- accepted-but-undelivered event 不按时间删除
- active backlog 上限 50,000；满时拒绝新事件，不覆盖旧事件
- `done` ingestion event 保留 30 天
- detailed capture rows 保留 365 天
- compact origin/contributor/last-source lineage 长期保留

Collector 在 SQLite commit 后才返回 HTTP 202；:8000 暂时不可用不会丢 turn，也不会让 Agent 等待抽取完成。重复 `event_id` + 相同 payload 幂等；同 ID 不同 payload/principal 返回 409。

## Provenance model

Sidecar tables keyed by `memory_id` store：

- `origin`：最早来源
- `contributors`：所有 stable client
- `last_source`：最近来源、credential、operation 与时间
- `memory_captures`：event、capture mode、authority、operation

`UPDATE`、`REINFORCEMENT` 会追加 contributor；`MERGE` 会继承 related memories 的 origin 与 contributors，再追加本次来源。

Capture mode 与 Agent identity / authority 分离：

- `auto_hook`：completed-turn adapter
- `explicit_remember`：MCP `remember` 或 trusted explicit ingestion
- `import`：面板文档导入

Panel 对 memory ids 做批量 SQLite 查询，不逐卡查询。卡片显示 `HERMES · FRIES · AUTO` 这类 chip；浏览页可按 client/instance 与 capture mode 过滤。时间继续统一按 `Asia/Shanghai` 渲染。

Panel 与召回评测源码统一位于本仓库 `panel/`。本机从
`/Users/leway/Projects/MindMemOS/panel` 运行，235 部署至
`/opt/mindmemos-company/panel`；旧的独立 `mm-panel` 目录不再是源码真源。

## Runtime adapters

### Hermes

Repository source of truth：

```text
adapters/hermes/mindmemos/__init__.py
adapters/hermes/mindmemos/plugin.yaml
adapters/hermes/install.py
```

运行时安装到 `$HERMES_HOME/plugins/mindmemos/`。使用
`python3 adapters/hermes/install.py` 同步，`--check` 验证运行文件与 Git 源码一致；
真实 `mindmemos.json` 与 Key 不进入仓库。

Hermes Agent 的 Recall 统一通过受认证的 HTTP MCP；Provider 不再直连 `/v1/memory/search`。
Primary-context `sync_turn` 与显式 memory mirror 先写本地 durable spool：

```text
$HERMES_HOME/mindmemos-spool/
```

然后异步提交 `ingest_url`。当前 `mindmemos.json` 使用 `mcp_url`、`api_key`、
`ingest_url`、自动胶囊预算与重试配置；不再使用旧的 `ingest_key`、`ingest_spool`、
`ingest_client_module`。非-primary context 不写；带 MindMemOS provenance metadata 的消息不递归捕获。
完整接入步骤见 `adapters/hermes/README.md`。

### Claude Code

Source：

```text
adapters/claude_code/mindmemos_hook.py
adapters/claude_code/install.py
```

Installer 只复制 adapter、写 mode-0600 adapter config/key file，并输出 machine-readable hook snippet；**不会修改 `~/.claude/settings.json`**。

```bash
python3 adapters/claude_code/install.py
```

通过 runtime secret store 提供 `MINDMEMOS_INGEST_KEY`，或预先写好 installer 指向的 mode-0600 key file。不要把 Key 放在命令参数、聊天或日志中。

Hook 配对 `UserPromptSubmit` 与 `Stop`，优先用 Stop 的 `last_assistant_message`；缺失时短暂重读 transcript 处理落盘延迟。Duplicate Stop 使用相同 stable event id。只抽 user/final assistant text，不读取 thinking/tool content。

### Pi / OMP

Source：

```text
adapters/pi_omp/mindmemos-provenance.ts
adapters/pi_omp/install.py
```

```bash
python3 adapters/pi_omp/install.py
```

Installer 复制 extension 并写 mode-0600 config/key file；**不会修改 live Pi/OMP daemon/settings**。Extension 只监听 `agent_end`，不监听 `turn_end`。当 `event.messages` 是完整历史时，它从后向前只选最新 completed user/final-assistant pair，并用 session/message timestamps 生成稳定 event id。

当前 Pi Feishu launch command 含 `--no-extensions`。文件安装后该 daemon 仍不会加载，必须由父 Agent 明确修改启动参数或显式 `-e` 加载；本次实现没有改 live plist/settings。

## 服务重启与检查

```bash
launchctl kickstart -k gui/$(id -u)/com.leway.mindmemos.mcp
launchctl kickstart -k gui/$(id -u)/com.leway.mindmemos.panel
curl -fsS http://127.0.0.1:8765/health
curl -fsS http://127.0.0.1:8666/api/health
```

`/ingest/turn` smoke 必须使用唯一测试 marker。完成后删除/归档所有返回 memory ids，并按 exact event/memory ids 调 `TurnLedger.purge` 清 sidecar；禁止 fuzzy cleanup。

## Security

- Key 不写入 source、repo、hook payload、chat 或日志。
- Token listing 不返回 hash/plaintext。
- Adapter spool 与 server ledger 不保存 bearer Key。
- :8765 只提供 MCP、authenticated ingestion、health、llms.txt 与 read-only Skill。
- :8666 token management 只信 TCP source address，不信 `X-Forwarded-For`。
- Hook 默认不采集 thinking、tool logs、tool results 或完整 transcript。

## Rollback

起始 commit：`4371b7523cd5e5e8831ef3017169411c943608cc`

预先备份：

```text
/Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340
```

恢复旧实现：

```bash
cp /Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340/MindMemOS/mcp_http_server.py /Users/leway/Projects/MindMemOS/
cp /Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340/MindMemOS/mcp_server.py /Users/leway/Projects/MindMemOS/
cp /Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340/MindMemOS/mcp_tokens.py /Users/leway/Projects/MindMemOS/
cp /Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340/MindMemOS/SKILL.md /Users/leway/Projects/MindMemOS/skills/mindmemos-memory/SKILL.md
cp /Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340/mm-panel/server.py /Users/leway/Projects/mm-panel/
cp /Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340/mm-panel/index.html /Users/leway/Projects/mm-panel/
cp /Users/leway/.hermes/backups/mindmemos-provenance-20260805-215340/hermes-plugin/__init__.py adapters/hermes/mindmemos/__init__.py
python3 adapters/hermes/install.py
```

随后撤销 Hermes provenance credential、从 `~/.hermes/mindmemos.json` 移除 `ingest_*` 字段，并重启 :8765/:8666。Claude/Pi live settings 本次未修改，无需恢复。

新文件 `turn_ingest.py`、`adapters/` 与新增 tests 可在确认回滚后删除。默认保留 SQLite ledger 便于审计；只有确认不需要未送达事件和 provenance 后才删除。
