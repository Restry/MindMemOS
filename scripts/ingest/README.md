# 文档导入（Word / Excel / PPT / PDF）

把本地文档灌进 MindMemOS 记忆库。

> **MM 原生没有文档上传能力** —— `/v1/memory/add` 只收纯文本，
> 没有 `UploadFile` / multipart 路由。所以解析这一层需要自己做，
> 这个目录就是补上这块。

## 两种用法

### 1. 界面上传（少量文件，推荐）

面板 <http://192.168.1.246:8666> → **上传文档** tab，
拖进去或点击选择，可多选、可打标签。

### 2. 命令行（大批量、整个目录）

**必须用 MM 的 venv**，抽取库装在那里：

```bash
cd ~/Projects/MindMemOS

.venv/bin/python3 scripts/ingest/cli.py ~/资料 --dry-run    # 先预演
.venv/bin/python3 scripts/ingest/cli.py ~/资料 --tag 项目名  # 正式导入
```

| 参数 | 说明 |
|---|---|
| `--dry-run` | 只看会导入什么，不写库 |
| `--tag` | 给这批文档打标签，检索时能过滤 |
| `--workers` | 并发数，默认 3 |
| `--redo` | 忽略断点记录，全部重导 |

断点记录在 `~/.mm_ingest_done.json`，**中断后重跑不会重复导入**。

## 文件说明

| 文件 | 作用 |
|---|---|
| `extractor.py` | 抽取 + 切片。**CLI 和面板共用这一份**，避免两边行为不一致 |
| `cli.py` | 命令行入口，负责遍历目录、断点续传、并发写库 |

面板通过 `_load_extractor()` 加载 `extractor.py`：面板跑在系统 Python，
而 docx/pypdf/pptx 装在 venv 里（系统 Python 受 PEP 668 保护装不了），
所以要把 venv 的 site-packages 挂进 `sys.path`。

## 支持格式

`.docx .doc .xlsx .xls .csv .pptx .ppt .pdf .md .txt .markdown`

## 两个关键设计

**Excel 按行拼成「列名: 值」**，不整表塞：

```
设备名称: Mac Studio；IP地址: 192.168.1.246；用途: 本地AI推理；负责人: 刘炜
```

整张表丢进向量库，语义检索无从下手。拆成行之后，
"Mac Studio 的 IP 是多少" 才能精确命中到具体那一行。

**Word 的表格也要抽**（`doc.tables`）—— 很多关键信息都在表里，
只抽 `paragraphs` 会漏掉。

## 已知限制

- **扫描版 PDF 抽不出文字**，需要 OCR（暂未实现）
- 少于 80 字符的内容会跳过，没有检索价值
- `~$` 开头的 Office 临时文件自动忽略
