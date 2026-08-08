#!/usr/bin/env python3
"""MindMemOS MCP server —— 让 omp / Claude Desktop 等 MCP 客户端读写你的长期记忆。

stdio transport，手写 JSON-RPC 2.0，零第三方依赖（只用标准库），
避免 MCP SDK 版本变动或 venv 环境问题导致挂掉。

暴露 3 个工具：
  recall        语义检索记忆（最常用）
  remember      写入一条新记忆
  memory_stats  查库存

配置见同目录 README-mcp.md。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

from mcp_tokens import local_fallback_principal
from turn_ingest import get_default_ledger

API = os.getenv("MINDMEMOS_API", "http://127.0.0.1:8000")
USER = os.getenv("MINDMEMOS_USER", "leway")
PROFILE_MODE = os.getenv("MINDMEMOS_PROFILE_MODE", "personal").strip().lower()
ORGANIZATION_MODE = PROFILE_MODE == "organization"
TIMEOUT = float(os.getenv("MINDMEMOS_TIMEOUT", "60"))
NEO4J_USER = os.getenv("MINDMEMOS_NEO4J_USER", "neo4j")
NEO4J_PASS = os.getenv("MINDMEMOS_NEO4J_PASS", "mindmemos_dev_password")
NEO4J_CONTAINER = os.getenv("MINDMEMOS_NEO4J_CONTAINER", "mindmemos-neo4j")
QDRANT = os.getenv("MINDMEMOS_QDRANT_URL", "http://127.0.0.1:6333")


def _key() -> str:
    """Read the server-to-MM API key, separate from MCP client credentials."""
    k = os.getenv("MINDMEMOS_KEY", "")
    if k:
        return k
    key_file = os.getenv("MINDMEMOS_SERVER_KEY_FILE", "")
    if key_file:
        try:
            return open(os.path.expanduser(key_file), encoding="utf-8").read().strip()
        except Exception:
            return ""
    for p in (os.path.expanduser("~/.hermes/mindmemos.json"), "/tmp/mm_keys.json"):
        try:
            with open(p, encoding="utf-8") as f:
                d = json.load(f)
            for field in ("api_key", "key", "vanilla"):
                if d.get(field):
                    return str(d[field])
        except Exception:
            continue
    return ""


def _post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


# ------------------------------------------------------------------ 工具实现


def t_recall(args: dict) -> str:
    q = (args.get("query") or "").strip()
    if not q:
        return "错误：query 不能为空"
    requested = int(args.get("top_k") or args.get("limit") or 8)
    limit = min(8, max(1, requested))
    d = _post(
        "/v1/memory/search",
        {
            "user_id": USER,
            "query": q,
            "top_k": limit,
            "rerank": True,
            "score_threshold": 0.1,
        },
    )
    mems = (d.get("data") or {}).get("memories") or []
    if args.get("response_format") == "json":
        structured = []
        for memory in mems[:limit]:
            content = str(memory.get("memory") or "").strip()
            if not content:
                continue
            memory_id = str(memory.get("id") or "").strip()
            if not memory_id:
                memory_id = "content-" + hashlib.sha256(content.encode("utf-8")).hexdigest()[:24]
            structured.append(
                {
                    "id": memory_id,
                    "memory": content,
                    "memory_type": str(memory.get("memory_type") or memory.get("type") or "fact"),
                    "last_update_at": str(memory.get("last_update_at") or ""),
                    "event_time": memory.get("event_time"),
                    "source_timestamp": memory.get("source_timestamp"),
                }
            )
        return json.dumps(
            {"query": q, "memories": structured},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    if not mems:
        return f"没有查到与「{q}」相关的记忆。"
    records = []
    used = 0
    for m in mems[:limit]:
        txt = (m.get("memory") or "").strip()
        typ = m.get("memory_type") or m.get("type") or ""
        record = f"[{typ}] {txt}" if typ else txt
        # Reserve room for the dynamic heading and numbering. Never split a record.
        projected = used + len(record) + 8
        if projected > 3950:
            continue
        records.append(record)
        used = projected
    if not records:
        return f"没有查到与「{q}」相关的记忆。"
    lines = [f"查到 {len(records)} 条相关记忆：\n"]
    lines.extend(f"{i}. {record}" for i, record in enumerate(records, 1))
    return "\n".join(lines)


def t_remember(args: dict, *, principal=None, request_id=None) -> str:
    content = (args.get("content") or "").strip()
    if not content:
        return "错误：content 不能为空"

    trusted = principal or local_fallback_principal(
        os.getenv("MINDMEMOS_AGENT_KIND", "stdio_mcp"),
        os.getenv("MINDMEMOS_INSTANCE") or None,
    )
    session_id = (args.get("session_id") or "mcp").strip()
    event_seed = json.dumps(
        {
            "client_id": trusted.client_id,
            "session_id": session_id,
            "request_id": request_id,
            "content": content,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    event_id = "mcp-" + hashlib.sha256(event_seed.encode()).hexdigest()
    ledger = get_default_ledger()
    ledger.submit_memory(
        {
            "event_id": event_id,
            "session_id": session_id,
            "content": content,
            "safe_context": {"transport": "mcp"},
        },
        trusted,
        capture_mode="explicit_remember",
    )
    result = ledger.process_next(_post, event_id=event_id, force=True)
    if result is None or result.status != "done":
        return "已持久化排队；MindMemOS 暂不可用时会自动重试，不会丢失。"
    memories = ((result.response or {}).get("data") or {}).get("memories") or []
    count = len(memories)
    return f"已写入，抽取出 {count} 条记忆。" if count else "已提交，但没有抽取出记忆（内容可能太短或无事实性信息）。"


def t_stats(_args: dict) -> str:
    try:
        with urllib.request.urlopen(f"{QDRANT}/collections/memory_item_v1", timeout=15) as r:
            n = json.loads(r.read())["result"]["points_count"]
        return f"MindMemOS 当前记忆条数：{n}"
    except Exception as e:
        return f"查询失败：{e}"


# ---------------------------------------------------------------- 图谱类工具


def _cypher(query: str) -> list[list[str]]:
    """跑一条只读 Cypher，返回除表头外的数据行。"""
    import subprocess

    r = subprocess.run(
        [
            "docker",
            "exec",
            NEO4J_CONTAINER,
            "cypher-shell",
            "-u",
            NEO4J_USER,
            "-p",
            NEO4J_PASS,
            "--format",
            "plain",
            query,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout)[:200])
    lines = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
    return [[c.strip().strip('"') for c in ln.split(", ")] for ln in lines[1:]]


def t_related(args: dict) -> str:
    """看某个实体在图谱里跟什么关联最紧——发现自己没想到的线索。"""
    name = (args.get("entity") or "").strip()
    if not name:
        return "错误：entity 不能为空"
    safe = name.replace("\\", "").replace('"', "").lower()
    limit = int(args.get("limit") or 12)
    rows = _cypher(
        f"MATCH (e:Entity)<-[:MENTIONS]-(m:Memory)-[:MENTIONS]->(o:Entity) "
        f'WHERE toLower(e.entity_name) CONTAINS "{safe}" '
        f"AND o.entity_name <> e.entity_name "
        f"RETURN o.entity_name AS related, o.entity_type AS type, count(*) AS w "
        f"ORDER BY w DESC LIMIT {limit};"
    )
    if not rows:
        return f"图谱里没有与「{name}」关联的实体。"
    out = [f"与「{name}」共同出现最多的实体（按共现次数）：\n"]
    for r in rows:
        if len(r) >= 3:
            out.append(f"  {r[2]:>3}x  {r[0]}  [{r[1]}]")
    return "\n".join(out)


def t_by_project(args: dict) -> str:
    """列出某个项目的全部约束/铁律——开工前一次性看清规矩。"""
    proj = (args.get("project") or "").strip()
    if not proj:
        return "错误：project 不能为空"
    d = _post(
        "/v1/memory/search",
        {
            "user_id": USER,
            "query": f"{proj} 项目的约束、铁律、禁止事项、架构决策与已知限制",
            "top_k": int(args.get("top_k") or 20),
            "rerank": True,
            "score_threshold": 0.05,
        },
    )
    mems = (d.get("data") or {}).get("memories") or []
    low = proj.lower()
    hit = [m for m in mems if low in (m.get("memory") or "").lower()]
    use = hit or mems
    if not use:
        return f"没找到「{proj}」相关的记忆。"
    head = f"「{proj}」相关记忆 {len(use)} 条"
    if not hit:
        head += "（未精确匹配项目名，以下为语义最接近的结果）"
    out = [head + "：\n"]
    for i, m in enumerate(use, 1):
        out.append(f"{i}. {(m.get('memory') or '').strip()}")
    return "\n".join(out)


def _load_pinned() -> list[str]:
    """读常驻铁律（与 Hermes 插件同一个文件，单一事实源）。"""
    path = os.getenv("MINDMEMOS_PINNED") or os.path.expanduser("~/.hermes/mindmemos_pinned.md")
    try:
        raw = open(path, encoding="utf-8").read()
    except Exception:
        return []
    return [b.strip() for b in raw.split("\n§\n") if len(b.strip()) >= 8]


def t_whoami(args: dict) -> str:
    """拼出用户画像 —— MM 版的 Hermes "User"。

    为什么需要这个工具：Hermes 有独立的 USER.md 存结构化画像，
    MM 没有这个概念，身份信息散落在 fact/profile/episodic/file_knowledge
    等 6 种 mem_type 里。实测问「我是谁」只召回 1 条无关内容——
    代词类短查询跟"用户称呼其父亲为爸爸"的向量距离太远。
    所以这里按维度分别检索再合并，而不是指望一次语义检索捞全。
    """
    # 每个维度用「陈述句式」的查询，比代词更贴近记忆原文的表达。
    if ORGANIZATION_MODE:
        dims = [
            ("组织与身份", "组织名称 团队 部门 角色 职责 服务对象"),
            ("项目与业务", "公司项目 业务目标 当前阶段 负责人 关键决策"),
            ("工作环境", "公司服务器 内网地址 运行环境 系统依赖 部署配置"),
            ("协作规范", "团队协作方式 汇报格式 审批流程 禁止事项 行为准则"),
        ]
        profile_label = "组织上下文"
    else:
        dims = [
            ("称呼与身份", "用户的称呼、姓名、别名、时区与语言偏好"),
            ("家庭", "用户的儿子 女儿 配偶 妹妹 家人 健康状况 家庭收入"),
            ("工作", "用户就职的公司 部门 岗位职责 日常会议"),
            ("环境与设备", "用户的电脑 服务器 内网地址 硬件配置"),
            ("协作偏好", "用户偏好的沟通方式、汇报格式、禁止事项"),
        ]
        profile_label = "用户画像"
    only = (args.get("dimension") or "").strip()
    if only:
        dims = [d for d in dims if only in d[0]] or dims

    per = int(args.get("per_dim") or 5)
    out = [f"{profile_label}（按维度从记忆库聚合）：\n"]
    seen: set[str] = set()

    # 行为准则放最前面：这些是高权威铁律，优先级高于检索出来的内容
    if not only and args.get("include_rules", True):
        rules = _load_pinned()
        if rules:
            out.append("### 行为准则（高权威，优先级高于以下检索内容）")
            out.extend(f"  - {r}" for r in rules)
            out.append("")

    for title, q in dims:
        try:
            d = _post(
                "/v1/memory/search",
                {
                    "user_id": USER,
                    "query": q,
                    "top_k": per,
                    # 身份类查询关掉 rerank：实测 rerank 会把技术类记忆
                    # 排到身份记忆前面，反而更差
                    "rerank": False,
                    "score_threshold": 0.05,
                },
            )
            mems = (d.get("data") or {}).get("memories") or []
        except Exception as e:
            out.append(f"\n### {title}\n  （检索失败：{e}）")
            continue

        lines = []
        for m in mems:
            t = (m.get("memory") or "").strip()
            # 超长的多半是会议纪要/汇报正文被误召回，不是画像
            if not t or t in seen or len(t) > 160:
                continue
            seen.add(t)
            lines.append(f"  - {t}")
        out.append(f"\n### {title}")
        out.extend(lines or ["  （库里没有这方面的记忆）"])

    out.append("\n注：这些是从记忆库聚合的，可能不全。需要某方面细节请用 recall 追问。")
    return "\n".join(out)


SERVER_INSTRUCTIONS = f"""你已接入 MindMemOS —— {"组织与团队的长期记忆库" if ORGANIZATION_MODE else "用户的长期记忆库"}，跨机器、跨 agent 共享。

## 开始工作前

新会话的第一件事：调 `whoami` 获取{"组织上下文和行为准则" if ORGANIZATION_MODE else "用户画像和行为准则"}。
其中的“行为准则”部分权威性最高，优先于你的默认行为。

遇到使用方提起过往的项目、决策、“上次那个”、“以前怎么弄的”，先 `recall` 再回答。
**不要凭空猜测已有项目的情况**——应先查询当前记忆库。

## 什么时候要写记忆（重要）

MCP 工具和 companion Skill 本身不保证自动写入。只有安装了 runtime adapter 的客户端，
完成轮次才会由 hook 可靠摄取；没有 adapter 时必须主动调 `remember`。
即使启用了 adapter，用户纠正、明确偏好和关键技术决策仍应当场调 `remember`，服务端会去重。

遇到这些情况，**当场调 `remember`**，别等用户提醒：

1. **用户纠正了你** —— "不对，应该是…"、"我说过不要…"
   （这类最有价值，能防止同样的错误再犯）
2. **用户表达偏好或禁令** —— "以后都用 X"、"别再 Y"
3. **确定了技术方案或架构决策** —— 选了什么、为什么、否决了什么
4. **踩坑与解法** —— 报错原因 + 最终怎么解决的
5. **环境事实** —— 服务器地址、端口、路径、账号归属
6. **项目状态变化** —— 上线了、迁移了、负责人换了

反过来，**不要写**：临时调试过程、可以随时重新查到的信息、
本次任务的进度流水、大段原始日志。

## 怎么写

`remember` 收的是一段自然语言陈述，像跟人说话那样写清楚：

- ✅ "PackHorizon 生产环境用 systemd 托管执行器，不是 supervisor，
   因为需要开机自启和崩溃自愈。"
- ❌ "改了配置"（没有主语、没有原因，日后检索毫无价值）

写之前不用查重——服务端会自动做实体消解和冲突消解：
与旧记忆矛盾的内容会**就地改写旧记忆**，不会留下两条打架的记录。

## 其他工具

- `memory_stats` —— 库里有多少记忆、都是些什么
- `related_entities` —— 某个项目/人关联到哪些东西（图谱查询）
- `project_rules` —— 某个项目的专属约束和铁律，动手改代码前先查
"""

TOOLS = [
    {
        "name": "recall",
        "description": (
            f"检索 {USER} 的长期记忆库（MindMemOS）：过往项目历史、技术决策、"
            "踩过的坑、偏好、约束与运维细节。"
            "回答任何涉及『我们之前怎么做的』『某项目的约束是什么』时先查这里。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "自然语言查询"},
                "top_k": {"type": "integer", "description": "返回条数，默认 8"},
                "response_format": {
                    "type": "string",
                    "enum": ["text", "json"],
                    "description": "可选；默认 text。json 供生命周期适配器构建短上下文。",
                },
            },
            "required": ["query"],
        },
        "handler": t_recall,
    },
    {
        "name": "remember",
        "description": "把一条值得长期保留的事实写入 MindMemOS 记忆库。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容，写成完整陈述句"},
                "session_id": {"type": "string", "description": "可选，来源会话标识"},
            },
            "required": ["content"],
        },
        "handler": t_remember,
    },
    {
        "name": "memory_stats",
        "description": "查看 MindMemOS 记忆库当前条数。",
        "inputSchema": {"type": "object", "properties": {}},
        "handler": t_stats,
    },
    {
        "name": "related_entities",
        "description": (
            "在记忆图谱里查某个实体（项目名/人名/工具名）与什么关联最紧密，"
            "按共现次数排序。用于发现自己没想到的关联线索——"
            "比如查一个项目会带出它依赖的工具、涉及的组织、相关的其他项目。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "entity": {"type": "string", "description": "实体名，支持部分匹配"},
                "limit": {"type": "integer", "description": "返回条数，默认 12"},
            },
            "required": ["entity"],
        },
        "handler": t_related,
    },
    {
        "name": "project_rules",
        "description": (
            "一次性列出某个项目的全部约束、铁律、禁止事项和已知限制。"
            "**在动手改某个项目的代码前先调这个**，避免踩已经记录过的坑。"
            "比 recall 更聚焦，会优先返回明确提到该项目名的记忆。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "项目名，如 wx-gateway"},
                "top_k": {"type": "integer", "description": "检索条数，默认 20"},
            },
            "required": ["project"],
        },
        "handler": t_by_project,
    },
    {
        "name": "whoami",
        "description": (
            (
                "了解组织上下文：组织与团队、项目与业务、工作环境、协作规范。"
                "**新会话开始时、或需要了解组织背景时先调这个**——"
                "信息散落在多种记忆类型里，这个工具按公司维度聚合。"
            )
            if ORGANIZATION_MODE
            else (
                "了解用户是谁：称呼、家庭成员、工作、机器环境、协作偏好。"
                "**新会话开始时、或需要了解用户背景时先调这个**——"
                "相当于该用户的个人上下文档案。身份信息散落在多种记忆类型里，"
                "直接用 recall 查『我是谁』召不全，这个工具按维度聚合。"
            )
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "dimension": {
                    "type": "string",
                    "description": (
                        "可选，只看某维度：组织与身份/项目与业务/工作环境/协作规范"
                        if ORGANIZATION_MODE
                        else "可选，只看某维度：称呼与身份/家庭/工作/环境与设备/协作偏好"
                    ),
                },
                "per_dim": {"type": "integer", "description": "每维度条数，默认 5"},
            },
        },
        "handler": t_whoami,
    },
]
_BY_NAME = {t["name"]: t for t in TOOLS}


def call_tool(name: str, arguments: dict, *, principal=None, request_id=None) -> str:
    tool = _BY_NAME.get(name)
    if tool is None:
        raise KeyError(name)
    if name == "remember":
        return t_remember(arguments, principal=principal, request_id=request_id)
    return tool["handler"](arguments)


# ------------------------------------------------------------------ JSON-RPC


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _reply(mid, result) -> None:
    _send({"jsonrpc": "2.0", "id": mid, "result": result})


def handle(req: dict) -> None:
    method = req.get("method")
    mid = req.get("id")

    if method == "initialize":
        _reply(
            mid,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mindmemos", "version": "1.0.0"},
                # MCP 协议允许服务端下发 instructions，客户端会当系统提示注入。
                # MCP 只提供工具和行为说明；可靠的完成轮次自动写入由可选 runtime
                # adapter 负责。未安装 adapter 的客户端仍需按 instructions 主动 remember。
                "instructions": SERVER_INSTRUCTIONS,
            },
        )
    elif method == "tools/list":
        _reply(mid, {"tools": [{k: t[k] for k in ("name", "description", "inputSchema")} for t in TOOLS]})
    elif method == "tools/call":
        p = req.get("params") or {}
        tool = _BY_NAME.get(p.get("name"))
        if tool is None:
            _reply(mid, {"content": [{"type": "text", "text": f"未知工具：{p.get('name')}"}], "isError": True})
            return
        try:
            text = call_tool(p.get("name") or "", p.get("arguments") or {}, request_id=mid)
            _reply(mid, {"content": [{"type": "text", "text": text}]})
        except urllib.error.URLError as e:
            _reply(
                mid,
                {
                    "content": [{"type": "text", "text": f"连不上 MindMemOS（{API}）：{e}。检查服务是否在跑。"}],
                    "isError": True,
                },
            )
        except Exception as e:
            _reply(mid, {"content": [{"type": "text", "text": f"出错：{e}"}], "isError": True})
    elif mid is not None:
        # 未实现的方法（resources/list 等）返回标准错误码，别让客户端等超时
        _send({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
    # notifications（无 id）静默忽略


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            handle(json.loads(line))
        except Exception as e:
            print(f"[mindmemos-mcp] 解析失败: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
