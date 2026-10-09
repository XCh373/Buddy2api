#!/bin/bash
# Buddy2api 前台启动脚本（开发/调试用）
# 说明：
#   - 解释器优先取 BUDDY2API_PYTHON（与 start-launchd.sh、launchd plist 一致），默认 python3。
#   - 只检查依赖，不擅自 pip install，避免污染系统 Python / 全局环境。
#   - 若端口已被占用（例如 launchd 已在跑），直接报错退出，避免重复实例抢端口。
#     常驻运行请用 launchd：launchctl kickstart -k gui/$(id -u)/com.buddy2api.gateway
cd "$(dirname "$0")"

echo ""
echo "  ========================================"
echo "   Buddy 2 API"
echo "  ========================================"
echo ""

PY="${BUDDY2API_PYTHON:-python3}"
PORT="${BUDDY2API_PORT:-8787}"
LABEL="com.buddy2api.gateway"

# 检查解释器
if ! command -v "$PY" &> /dev/null; then
    echo "  [错误] 未找到 Python 解释器: $PY"
    echo "         可用 BUDDY2API_PYTHON 指定，例如："
    echo "         BUDDY2API_PYTHON=$HOME/Buddy2api/.venv/bin/python ./start.sh"
    exit 1
fi

# 检查依赖（只检查，不安装）
if ! "$PY" -c "import fastapi, uvicorn, httpx" 2>/dev/null; then
    echo "  [错误] $PY 缺少依赖 (fastapi / uvicorn / httpx)"
    echo "         请先在该解释器内安装，例如："
    echo "         \"$PY\" -m pip install fastapi 'uvicorn[standard]' httpx"
    echo "         或直接用 launchd 常驻：launchctl kickstart -k gui/$(id -u)/$LABEL"
    exit 1
fi

# 检查端口占用，避免与 launchd 实例争抢导致 Errno 48 崩溃循环
if command -v lsof &> /dev/null && lsof -nP -iTCP:"$PORT" -sTCP:LISTEN > /dev/null 2>&1; then
    echo "  [错误] 端口 $PORT 已被占用（可能 launchd 或另一个实例正在运行）"
    echo "         查看占用：lsof -nP -iTCP:$PORT -sTCP:LISTEN"
    echo "         重启常驻服务：launchctl kickstart -k gui/$(id -u)/$LABEL"
    exit 1
fi

echo "  [解释器] $PY"
echo "  [启动] http://127.0.0.1:$PORT"
echo "  [停止] Ctrl+C"
echo ""

# 管理 Token 与常驻实例保持一致（同目录受保护文件 .admin_token，同 start-launchd.sh）
if [ -z "${CB_GATEWAY_ADMIN_TOKEN:-}" ] && [ -f .admin_token ]; then
    CB_GATEWAY_ADMIN_TOKEN="$(cat .admin_token)"
    export CB_GATEWAY_ADMIN_TOKEN
fi

exec "$PY" server.py --host 127.0.0.1 --port "$PORT" "$@"
