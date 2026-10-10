#!/bin/bash
# Buddy2api 启动脚本（适用于 launchd / nohup / 手动运行）
# admin token 从同目录受保护文件 .admin_token 读取（需 chmod 600），避免明文写入 plist
# Python 解释器可用环境变量 BUDDY2API_PYTHON 覆盖（默认 python3）
cd "$(dirname "$0")"
if [ -z "${CB_GATEWAY_ADMIN_TOKEN:-}" ] && [ -f .admin_token ]; then
    export CB_GATEWAY_ADMIN_TOKEN="$(cat .admin_token)"
fi
exec "${BUDDY2API_PYTHON:-python3}" server.py --host 127.0.0.1 --port "${BUDDY2API_PORT:-8787}" --no-browser
