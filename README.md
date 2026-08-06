# MindMemOS LAN Panel

MindMemOS 的内网管理面板，默认监听 `8666`。提供记忆浏览、关系图谱、行为准则、文档导入、访问令牌和模型路由设置。

## MINDMEM·OS dashboard

首页使用真实 `/api/all` 快照绘制最近 30 天累计写入 SVG：北京时间空缺日期由服务端补零，
折线首次加载时绘制一次，白色 runner 每 6 秒沿真实路径巡航；`prefers-reduced-motion`
会关闭循环动画。实现不依赖 React/Recharts。

Memory Command Terminal 只提供安全的面板操作：普通文本和 `/search` 执行语义检索，
`/whoami`、`/browse`、`/recent`、`/upload`、`/graph`、`/tokens`、`/models` 切换页面，
`/refresh` 刷新快照。它不接受 shell 或任意服务器命令。

## 模型设置

“模型设置”页管理三类路由：

- LLM
- Embedding
- Rerank

读取接口不会返回 API Key，只返回是否已经配置。Key 输入框留空会保留现有值。保存前必须验证真实模型能力；Rerank 会执行最小 rerank 请求，而不是只检查 `/models`。保存时会创建权限为 `0600` 的配置备份，并通过固定的服务端命令重载 API/MCP。

相关环境变量：

```text
MM_MODEL_CONFIG_PATH
MM_MODEL_CONFIG_BACKUP_DIR
MM_MODEL_RELOAD_COMMAND
MINDMEMOS_API_KEY
MINDMEMOS_PANEL_KEYS
MINDMEMOS_PROVIDER_CONFIG
```

记忆 API credential 的读取顺序是环境变量、legacy panel keys 文件、标准
`~/.hermes/mindmemos.json`。运行配置和 API Key 不进入本仓库。

## 本地运行

```bash
python3 server.py
```

生产环境由 launchd（macOS）或 systemd（Linux）管理。`reload_models.sh` 是 macOS 本地实例使用的固定重载脚本。
