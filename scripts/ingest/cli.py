#!/usr/bin/env python3
"""把 Word / Excel / PPT / PDF / 文本 批量灌进 MindMemOS。

用法：
    ingest.py ~/Documents/资料           # 整个目录（递归）
    ingest.py a.docx b.xlsx              # 指定文件
    ingest.py ~/资料 --dry-run           # 只看会导入什么，不写库
    ingest.py ~/资料 --tag 中投项目       # 打标签，方便日后检索
    ingest.py ~/资料 --workers 4         # 并发（默认 3）

支持：.docx .doc .xlsx .xls .csv .pptx .ppt .pdf .md .txt

设计要点（踩过的坑）：
  - Excel **按行拼成 "列名: 值" 的句子**，不整表塞。整表丢进去向量检索
    根本无从下手，拆成行才能命中"某某项目的负责人是谁"这类问题。
  - 每个切片带文件名和来源路径，检索到之后能追溯回原始文档。
  - 断点续传：已导入的文件记在 .ingest_done.json，中断后重跑不会重复。
  - 跳过临时文件（~$开头）和过短内容（少于 MIN_CHARS）。
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import sys
import threading
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from extractor import EXTS, chunks, extract  # noqa: E402

CFG_PATH = os.path.expanduser("~/.hermes/mindmemos.json")
STATE = os.path.expanduser("~/.mm_ingest_done.json")

_lock = threading.Lock()


def cfg() -> dict:
    with open(CFG_PATH, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ 写库


def push(text: str, c: dict, src: str, tag: str = "") -> bool:
    label = f"[文档：{src}]" + (f"[{tag}]" if tag else "")
    body = json.dumps(
        {
            "user_id": c["user_id"],
            "session_id": f"ingest-{hashlib.md5(src.encode()).hexdigest()[:10]}",
            "messages": [{"role": "user", "content": f"{label}\n{text}"}],
            "mode": "sync",
        },
        ensure_ascii=False,
    ).encode()
    req = urllib.request.Request(
        f"{c['base_url'].rstrip('/')}/v1/memory/add",
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {c['api_key']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return json.loads(r.read()).get("code") in ("ok", 0, "0")
    except Exception as e:
        print(f"    ✗ 写入失败：{type(e).__name__}: {str(e)[:90]}")
        return False


def load_done() -> dict:
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except Exception:
        return {}


def save_done(d: dict) -> None:
    with _lock:
        json.dump(d, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)


def collect(paths: list[str]) -> list[str]:
    files = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            files.append(p)
        elif os.path.isdir(p):
            for root, _, names in os.walk(p):
                for n in names:
                    if n.startswith("~$") or n.startswith("."):
                        continue  # Office 临时文件
                    if os.path.splitext(n)[1].lower() in EXTS:
                        files.append(os.path.join(root, n))
    return sorted(set(files))


def main() -> None:
    ap = argparse.ArgumentParser(description="把文档批量灌进 MindMemOS")
    ap.add_argument("paths", nargs="+", help="文件或目录")
    ap.add_argument("--dry-run", action="store_true", help="只看不写")
    ap.add_argument("--tag", default="", help="给这批文档打个标签")
    ap.add_argument("--workers", type=int, default=3, help="并发数，默认 3")
    ap.add_argument("--redo", action="store_true", help="忽略断点记录，全部重导")
    a = ap.parse_args()

    try:
        c = cfg()
    except Exception as e:
        sys.exit(f"读不到 {CFG_PATH}：{e}")

    files = collect(a.paths)
    if not files:
        sys.exit("没找到可导入的文件。支持：" + " ".join(sorted(EXTS)))

    done = {} if a.redo else load_done()
    todo = [f for f in files if f not in done]
    print(f"发现 {len(files)} 个文件，待导入 {len(todo)} 个（已导入 {len(files) - len(todo)} 个，用 --redo 可重来）\n")

    stats = {"ok": 0, "skip": 0, "fail": 0, "chunks": 0}

    def handle(path: str) -> None:
        rel = os.path.basename(path)
        try:
            text = extract(path)
        except Exception as e:
            print(f"  ✗ {rel}：抽取失败 {type(e).__name__}: {str(e)[:70]}")
            stats["fail"] += 1
            return
        cs = chunks(text)
        if not cs:
            print(f"  – {rel}：内容太少，跳过")
            stats["skip"] += 1
            return
        print(f"  · {rel}：{len(text)} 字符 → {len(cs)} 片")
        stats["chunks"] += len(cs)
        if a.dry_run:
            stats["ok"] += 1
            return
        good = sum(1 for ch in cs if push(ch, c, rel, a.tag))
        if good:
            done[path] = {"chunks": good}
            save_done(done)
            stats["ok"] += 1
        else:
            stats["fail"] += 1

    if a.dry_run:
        for f in todo:
            handle(f)
    else:
        with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
            list(ex.map(handle, todo))

    print(
        f"\n{'（预演）' if a.dry_run else ''}"
        f"成功 {stats['ok']} · 跳过 {stats['skip']} · 失败 {stats['fail']}"
        f" · 共 {stats['chunks']} 片"
    )
    if not a.dry_run and stats["ok"]:
        print("断点记录：" + STATE)


if __name__ == "__main__":
    main()
