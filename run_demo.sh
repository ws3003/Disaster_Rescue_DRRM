#!/usr/bin/env bash
# =============================================================================
# Disaster_Rescue_DRRM V4.0 Edge Demo 一键启动脚本 (Raspberry Pi)
# =============================================================================
# 功能: 进入项目目录, 启动边缘预览+上传 (edge_client --preview)。
# 注意:
#   - 需在 Raspberry Pi 上, 且已装好 python + 依赖 (见 requirements_pi_v4_demo.txt)。
#   - 先配置好 config/edge.yaml 的 server.api_url。
#   - 摄像头模式仅支持树莓派 Picamera2; headless 无桌面时按 q 无法退出预览,
#     请直接 Ctrl+C。
# =============================================================================

set -e

# 项目根目录 (脚本所在目录的上一级; 兼容在根目录执行)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

cd "$PROJECT_ROOT" || { echo "无法进入项目目录: $PROJECT_ROOT"; exit 1; }

echo "================================================================"
echo "[V4.0 Edge Demo] 项目目录: $(pwd)"
echo "[V4.0 Edge Demo] 启动 edge_client --preview ..."
echo "  - 摄像头实时预览 + 感知 + 上传 + Agent + DingTalk 报警"
echo "  - 按 q 退出窗口 (headless 环境用 Ctrl+C)"
echo "================================================================"

# 建议用虚拟环境 python; 若存在 venv 则优先
if [ -x "$PROJECT_ROOT/venv/bin/python" ]; then
    PYTHON="$PROJECT_ROOT/venv/bin/python"
elif [ -x "$PROJECT_ROOT/venv/Scripts/python.exe" ]; then
    PYTHON="$PROJECT_ROOT/venv/Scripts/python.exe"   # Windows 兼容
else
    PYTHON="python"
fi

# 从 src/ 目录运行, 使顶层 import (camera/edge_perception)可用
cd "$PROJECT_ROOT/src" || { echo "无法进入 src/"; exit 1; }

if "$PYTHON" edge_client.py --preview; then
    echo "[V4.0 Edge Demo] edge_client 结束 (正常退出)"
else
    echo "[V4.0 Edge Demo] edge_client 退出码非 0, 请查看上方日志"
    exit 1
fi