#!/bin/bash
# Yu-Agent 启动脚本：自动定位项目根目录 + 选择 Python 环境 + 检查依赖
set -e

# 定位项目根目录（本脚本在 scripts/ 下，根目录是上一级）
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

# 1. 选择 Python 解释器
PYTHON=""
if [ -f "$PROJECT_ROOT/.venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/.venv/bin/python"
    echo "使用项目虚拟环境: .venv"
elif [ -f "/Users/yuling/.workbuddy/binaries/python/envs/default/bin/python" ]; then
    PYTHON="/Users/yuling/.workbuddy/binaries/python/envs/default/bin/python"
    echo "使用 WorkBuddy 隔离环境"
else
    PYTHON="$(command -v python3 || command -v python)"
    echo "使用系统 Python: $PYTHON"
fi

# 2. 检查 yu_agent 包是否可用（优先已安装，否则用 src 目录）
if ! "$PYTHON" -c "import yu_agent" >/dev/null 2>&1; then
    if [ -d "$PROJECT_ROOT/src/yu_agent" ]; then
        export PYTHONPATH="$PROJECT_ROOT/src:${PYTHONPATH}"
        echo "未安装包，使用 src/ 目录（PYTHONPATH 方式）"
    else
        echo "错误：找不到 yu_agent 包，请先执行: uv pip install -e '.[gui,macos]'"
        exit 1
    fi
fi

# 3. 检查关键依赖，缺失则提示安装
MISSING=""
for mod in pydantic yaml PIL requests; do
    if ! "$PYTHON" -c "import $mod" >/dev/null 2>&1; then
        MISSING="$MISSING $mod"
    fi
done
if [ -n "$MISSING" ]; then
    echo ""
    echo "缺少依赖:$MISSING"
    echo "请执行: uv pip install -e '.[gui,macos]'"
    echo ""
    exit 1
fi

echo "启动 Yu-Agent GUI..."
exec "$PYTHON" -m yu_agent gui
