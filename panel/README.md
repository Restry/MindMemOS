# MindMemOS LAN Panel

MindMemOS 的内网管理面板，默认监听 `8666`。提供记忆浏览、关系图谱、行为准则、文档导入、访问令牌和模型路由设置。

## Source of truth

面板源码属于主仓库 `Restry/MindMemOS`：

```text
panel/
```

本机运行目录是 `/Users/leway/Projects/MindMemOS/panel`；235 生产部署目录是
`/opt/mindmemos-company/panel`。旧的独立 `/Users/leway/Projects/mm-panel` 只保留历史，
不再作为源码或运行真源。

“召回评测”模块位于：

```text
panel/recall_evaluation.py
panel/recall_judge.py
panel/test_recall_evaluation.py
panel/test_recall_judge.py
```

## MINDMEM·OS dashboard

首页使用真实 `/api/all` 快照绘制最近 30 天累计写入 SVG：北京时间空缺日期由服务端补零，
30 个日期点覆盖完整绘图区并随容器宽度重算；白色 runner 每 6 秒沿真实路径巡航，
`prefers-reduced-motion` 会关闭循环动画。点击日期点会打开唯一的“最新新增”视图，按
`Asia/Shanghai` 自然日过滤完整记忆列表；该视图保留来源、类型、Agent、捕获方式、噪音
筛选和连续加载能力，并提供清除日期筛选。实现不依赖 React/Recharts。

Memory Command Terminal 只提供安全的面板操作：普通文本和 `/search` 执行语义检索，
`/whoami`、`/recent`、`/upload`、`/graph`、`/tokens`、`/models` 切换页面，
`/refresh` 刷新快照。它不接受 shell 或任意服务器命令。功能 Tabs 固定在页面内容顶部，默认打开首页；语义搜索不占用 Tab，Memory Command Terminal 固定浮动在页面底部，动态建议问题显示在 READY 状态下方，搜索结果在 Terminal 上方的可关闭浮层展示。

## 模型设置

“模型设置”页分成两层：

- Endpoint 注册表：统一维护多个 OpenAI-compatible Endpoint 与 Key。
- 模型路由：LLM、Embedding、Rerank 只从全局缓存目录选择模型。

新增或修改 Endpoint 时，面板服务端调用一次 `/models`（Endpoint 通常已经以 `/v1` 结尾，即最终请求 `/v1/models`），并把模型 ID、来源 Endpoint 和抓取时间持久化到权限为 `0600` 的运行时缓存。页面加载、重新进入模型页和输入搜索都只读取缓存，不访问远端；只有“保存 Endpoint”“刷新缓存”“刷新全部缓存”会重新调用远端。

API 响应不会返回 Key。编辑 Endpoint 时 Key 留空会保留现有值。模型保存前会验证缓存归属并执行真实能力测试；Rerank 会执行最小 rerank 请求。配置保存仍会创建 `0600` 备份并通过固定命令重载 API/MCP。

相关环境变量：

```text
MM_MODEL_CONFIG_PATH
MM_MODEL_CONFIG_BACKUP_DIR
MM_MODEL_ENDPOINTS_PATH
MM_MODEL_RELOAD_COMMAND
MINDMEMOS_API_KEY
MINDMEMOS_CONFIG_PATH
MINDMEMOS_PANEL_API_KEY_ID
MINDMEMOS_PANEL_MEMORY_ALGORITHM
MINDMEMOS_PANEL_KEYS
MINDMEMOS_PROVIDER_CONFIG
```

记忆 API credential 的读取顺序是显式环境变量、API 运行配置 `auth.api_key_file`、legacy
panel keys 文件、标准 `~/.hermes/mindmemos.json`。运行配置中的密钥和 API Key 文件保持未跟踪，不进入提交。

## 本地运行

```bash
python3 server.py
```

生产环境由 launchd（macOS）或 systemd（Linux）管理。`reload_models.sh` 是 macOS 本地实例使用的固定重载脚本。
