#!/usr/bin/env python3
"""把另一台机器的 Hermes 记忆迁进 MindMemOS。

在**那台机器**上跑。会把 MEMORY.md / USER.md / daily/*.md 全部灌进
中心 MM 库，之后两台机器共享同一份大脑。

用法：
    python3 migrate_hermes_to_mm.py --dry-run     # 先看要灌什么，不写库
    python3 migrate_hermes_to_mm.py               # 真正执行
    python3 migrate_hermes_to_mm.py --resume      # 中断后续传

特性：
  - 断点续传：状态存 ~/.hermes/mm_migrate_state.json，重跑自动跳过已灌的
  - 长文自动切片（12000 字符），避免抽取超时
  - 每条带机器名标记，方便日后区分来源
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

# ============ 改这里 ============
MM_API = os.getenv("MM_API", "http://192.168.1.246:8000")
MM_KEY = os.getenv("MM_KEY", "")  # 留空则从 ~/.hermes/mindmemos.json 读
USER_ID = os.getenv("MM_USER", "leway")  # 两台机器必须一致才能共享
# ================================

MEM_DIR = os.path.expanduser("~/.hermes/memories")
STATE = os.path.expanduser("~/.hermes/mm_migrate_state.json")
CHUNK = 12000
MIN_CHARS = 80
HOST = socket.gethostname().split(".")[0]


def load_key() -> str:
    if MM_KEY:
        return MM_KEY
    for p in (os.path.expanduser("~/.hermes/mindmemos.json"),):
        try:
            with open(p, encoding="utf-8") as f:
                k = json.load(f).get("api_key")
            if k:
                return str(k)
        except Exception:
            pass
    sys.exit("找不到 API key：设环境变量 MM_KEY，或在 ~/.hermes/mindmemos.json 里配 api_key")


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"done": [], "counts": {}}


def save_state(s: dict) -> None:
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=2)


def chunks(text: str) -> list[str]:
    """按段落边界切片；单段落超长时按字符硬切，别漏内容。"""
    if len(text) <= CHUNK:
        return [text]
    out, cur = [], ""
    for para in text.split("\n\n"):
        # 单个段落就超长：先冲掉暂存，再把它硬切成若干片
        if len(para) > CHUNK:
            if cur:
                out.append(cur)
                cur = ""
            for i in range(0, len(para), CHUNK):
                out.append(para[i : i + CHUNK])
            continue
        if len(cur) + len(para) + 2 > CHUNK and cur:
            out.append(cur)
            cur = para
        else:
            cur = f"{cur}\n\n{para}" if cur else para
    if cur:
        out.append(cur)
    return out


def push(key: str, label: str, content: str) -> int:
    body = json.dumps(
        {
            "user_id": USER_ID,
            "session_id": f"migrate-{HOST}",
            "messages": [{"role": "user", "content": content}],
            "mode": "sync",
            "metadata": {"source": "hermes-migrate", "host": HOST, "label": label},
        }
    ).encode()
    req = urllib.request.Request(
        f"{MM_API}/v1/memory/add",
        data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        d = json.loads(r.read())
    return len((d.get("data") or {}).get("memories") or [])


def collect() -> list[tuple[str, str]]:
    """返回 [(label, content), ...]"""
    items: list[tuple[str, str]] = []
    for name in ("MEMORY.md", "USER.md"):
        p = os.path.join(MEM_DIR, name)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if len(t) >= MIN_CHARS:
                # 带上机器名，日后能分清是哪台机器的记忆
                items.append((f"{name}", f"【{HOST} 机器的 Hermes {name}】\n\n{t}"))

    daily = os.path.join(MEM_DIR, "daily")
    if os.path.isdir(daily):
        for fn in sorted(os.listdir(daily)):
            if not fn.endswith(".md"):
                continue
            p = os.path.join(daily, fn)
            with open(p, encoding="utf-8") as f:
                t = f.read().strip()
            if len(t) >= MIN_CHARS:
                date = fn[:-3]
                items.append((f"daily/{fn}", f"【{HOST} 机器 {date} 的每日复盘】\n\n{t}"))
    return items


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只看要灌什么，不写库")
    ap.add_argument("--resume", action="store_true", help="跳过已灌的（默认就会跳）")
    args = ap.parse_args()

    key = load_key()
    items = collect()
    if not items:
        sys.exit(f"{MEM_DIR} 下没找到可迁移的记忆文件")

    state = load_state()
    done = set(state["done"])
    todo = [it for it in items if it[0] not in done]

    print(f"机器名     : {HOST}")
    print(f"目标 MM    : {MM_API}  (user_id={USER_ID})")
    print(f"发现文件   : {len(items)} 个，待灌 {len(todo)} 个，已灌 {len(items) - len(todo)} 个")
    total_chars = sum(len(c) for _, c in todo)
    print(f"待灌字符   : {total_chars:,}（约 {sum(len(chunks(c)) for _, c in todo)} 个切片）")

    if args.dry_run:
        print("\n--dry-run，不写库。前 10 个：")
        for label, c in todo[:10]:
            print(f"  {label:28s} {len(c):>7,} 字符")
        return

    # 灌之前先确认能连上，别跑一半才发现不通
    try:
        urllib.request.urlopen(f"{MM_API}/docs", timeout=15)
    except Exception as e:
        sys.exit(f"连不上 MM（{MM_API}）：{e}\n检查那台机器的 MM API 是否在跑、防火墙是否放行 8000。")

    print("\n开始灌库…\n")
    ok = fail = added = 0
    for i, (label, content) in enumerate(todo, 1):
        parts = chunks(content)
        n = 0
        try:
            for j, part in enumerate(parts, 1):
                tag = f"{label} [{j}/{len(parts)}]" if len(parts) > 1 else label
                n += push(key, tag, part)
                time.sleep(0.3)  # 别把 Hub 打爆
            ok += 1
            added += n
            done.add(label)
            state["done"] = sorted(done)
            state["counts"][label] = n
            save_state(state)
            print(f"  [{i}/{len(todo)}] ✅ {label:28s} -> {n} 条")
        except urllib.error.HTTPError as e:
            fail += 1
            print(f"  [{i}/{len(todo)}] ❌ {label:28s} HTTP {e.code}: {e.read()[:120].decode(errors='ignore')}")
        except Exception as e:
            fail += 1
            print(f"  [{i}/{len(todo)}] ❌ {label:28s} {type(e).__name__}: {str(e)[:110]}")

    print(f"\n完成：成功 {ok} 个文件，失败 {fail} 个，新增 {added} 条记忆")
    if fail:
        print("失败的重跑一次本脚本即可（已成功的会自动跳过）")


if __name__ == "__main__":
    main()
