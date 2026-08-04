#!/usr/bin/env bash
# 把 mindmemos-memory skill 装到这台机器上。
#
# 用法（在目标机器上）：
#     bash install-skill.sh
#
# 规则：skill 实体只放 ~/.agents/skills/（唯一真源），
# 各 agent 用软链指过去，不要各自复制一份。
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/mindmemos-memory" && pwd)"
TRUE_SRC="$HOME/.agents/skills/mindmemos-memory"

echo "→ 安装 mindmemos-memory skill"

# 1. 实体进唯一真源
mkdir -p "$HOME/.agents/skills"
if [ -e "$TRUE_SRC" ] && [ ! -L "$TRUE_SRC" ]; then
    rm -rf "$TRUE_SRC"
fi
cp -R "$SRC" "$TRUE_SRC"
echo "  ✅ 真源：$TRUE_SRC"

# 2. 各 agent 软链过去。目录不存在说明没装那个 agent，跳过即可。
link_for() {
    local dir="$1" name="$2"
    [ -d "$dir" ] || return 0
    ln -sfn "$TRUE_SRC" "$dir/mindmemos-memory"
    echo "  ✅ $name → $dir/mindmemos-memory"
}
link_for "$HOME/.claude/skills"  "Claude Code"
link_for "$HOME/.hermes/skills"  "Hermes"
link_for "$HOME/.codex/skills"   "Codex"
link_for "$HOME/.cursor/skills"  "Cursor"

echo
echo "完成。注意 skill 只是深度参考——"
echo "真正保证 agent 主动写记忆的是 MCP 服务端下发的 instructions，"
echo "接上 MCP 就自动生效，不需要这个 skill 也能工作。"
