#!/bin/bash
# Buddy2api 启动脚本（适用于 launchd / nohup / 手动运行）
# 默认 loopback 鉴权与 `python server.py` 一致；显式设置 CB_GATEWAY_ADMIN_TOKEN 才启用管理 Token。
# Python 解释器可用环境变量 BUDDY2API_PYTHON 覆盖（默认 python3）
cd "$(dirname "$0")"
exec "${BUDDY2API_PYTHON:-python3}" server.py --host 127.0.0.1 --port "${BUDDY2API_PORT:-8787}" --no-browser
