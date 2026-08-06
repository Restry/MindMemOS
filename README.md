# MindMemOS LAN Panel

MindMemOS 的内网管理面板，默认监听 `8666`。提供记忆浏览、关系图谱、行为准则、文档导入、访问令牌和模型路由设置。

## 模型设置

“模型设置”页管理三类路由：

- LLM
- Embedding
- Rerank

读取接口不会返回 API Key，只返回是否已经配置。Key 输入框留空会保留现有值。保存前必须通过兼容网关的 `/models` 检查；保存时会创建权限为 `0600` 的配置备份，并通过固定的服务端命令重载 API/MCP。

相关环境变量：

```text
MM_MODEL_CONFIG_PATH
MM_MODEL_CONFIG_BACKUP_DIR
MM_MODEL_RELOAD_COMMAND
```

运行配置和 API Key 不进入本仓库。

## 本地运行

```bash
python3 server.py
```

生产环境由 launchd（macOS）或 systemd（Linux）管理。`reload_models.sh` 是 macOS 本地实例使用的固定重载脚本。
