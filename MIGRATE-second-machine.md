# 把另一台机器的 Hermes 记忆接进 MindMemOS

目标：两台机器共用同一份长期记忆——在 A 机记住的事，B 机立刻能查到。

**为什么不用 MCP 做迁移**：MCP 是给 agent 边聊边查/写用的，一次搬几百条会很慢，
中途断了还不好续。迁移用直连 API 的脚本，几分钟搞定且能断点续传。
迁移完成后日常使用照样可以走 MCP。

---

## 前提：确认网络通

在**另一台机器**上执行：

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://192.168.1.246:8000/docs
```

期望 `200`。拿不到就检查：
- Mac Studio 上 MM API 是否在跑：`launchctl list | grep mindmemos`
- 是否同一个内网、防火墙是否放行 8000

> API 已绑 `*:8000`（所有网卡），不需要额外改配置。

---

## 第 1 步：拷迁移脚本与基础配置

```bash
scp ~/Projects/MindMemOS/migrate_hermes_to_mm.py  另一台机器:~/
scp ~/.hermes/mindmemos.json                      另一台机器:~/.hermes/
```

Hermes Provider 的 recall 统一走 MCP。先在 :8666「访问令牌」页为新机器的 Hermes
实例签发独立 write Key，再通过安全 secret channel 写入：

```json
{
  "mcp_url": "http://192.168.1.246:8765/mcp",
  "api_key": "（新机器 Hermes 实例专属 Key）",
  "ingest_url": "http://192.168.1.246:8765/ingest/turn",
  "recall_limit": 8,
  "auto_context_max_items": 3,
  "auto_context_chars": 1800,
  "auto_memory_chars": 560,
  "session_context_chars": 6000,
  "query_cache_seconds": 1800
}
```

两台机器通过各自的实例 Key 访问同一 MCP 服务，即可共享业务记忆；credential rotation 时复用同一个 stable `client_id`。

---

## 第 2 步：先 dry-run 看要灌什么

```bash
python3 ~/migrate_hermes_to_mm.py --dry-run
```

会列出 MEMORY.md / USER.md / daily 下所有文件和字符数，**不写库**。
确认无误再进行下一步。

---

## 第 3 步：正式迁移

```bash
python3 ~/migrate_hermes_to_mm.py
```

- 长文自动按 12000 字符切片（段落边界优先，单段超长则硬切）
- 每条内容带机器名标记，日后能分清来源
- 断点续传：状态存 `~/.hermes/mm_migrate_state.json`，
  中断后重跑会自动跳过已灌的
- 每条之间 sleep 0.3s，不会把 Hub 打爆

本机 69 个文件 / 12 万字符作参考，大概几分钟。

---

## 第 4 步：把那台 Hermes 的记忆源切到 MM

```bash
hermes config set memory.provider mindmemos
hermes config set memory.memory_enabled false
hermes config set memory.user_profile_enabled false
```

插件本体必须从 MindMemOS Git 仓库安装，不要从某台机器的运行目录反向拷源码：

```bash
cd /path/to/MindMemOS
export HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
python3 adapters/hermes/install.py --hermes-home "$HERMES_HOME"
python3 adapters/hermes/install.py --hermes-home "$HERMES_HOME" --check
```

当前 Provider 自带 durable spool 与 HTTP ingest 客户端，不再配置旧的
`ingest_client_module` / `ingest_spool` / `ingest_key` 字段。

---

## 第 5 步：验证闭环

在**那台机器**上：

```bash
hermes chat "记一下：B机验证暗号是 XXXX-1234"
```

在**这台机器**上：

```bash
hermes chat "B机验证暗号是什么？"
```

答得出来 = 两台机器共享成功。

---

## 回滚

内置的 MEMORY.md / USER.md 不会被删改，随时可以退回：

```bash
hermes config set memory.provider builtin
hermes config set memory.memory_enabled true
hermes config set memory.user_profile_enabled true
```

---

## 顺带：那台机器也能用 MCP

为该 MCP client/机器实例再签发一条独立 Key（不要与 Hermes adapter 共用），然后按
调用方自己的 MCP 配置机制注册：

```text
Name: mindmemos
Transport: Streamable HTTP
Endpoint: http://192.168.1.246:8765/mcp
Authentication: HTTP bearer Key
```

Skill / extension / MCP 配置位置由调用方 runtime 决定，不在本指南写死。注册后获取
`http://192.168.1.246:8765/skills/mindmemos-memory/SKILL.md`，按调用方原生机制安装并
验证 `whoami`。需要可靠自动写入时再安装对应 runtime adapter；只有 MCP + Skill 不
等于 completed-turn 自动捕获。

---

## 注意

- **MM 服务挂了，那台机器也会查不到记忆**（本机已配 launchd 自愈 + 每日备份）。
  身份/汇报格式类的常驻内容存在各自本地 `~/.hermes/mindmemos_pinned.md`，不受影响。
- 跨机走的是内网明文 HTTP。仅限家庭内网使用，不要暴露到公网。
