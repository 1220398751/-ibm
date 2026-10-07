#!/bin/bash
# ==============================================================================
# 凡凡选股 · 一键启动脚本（start.sh）
# 启动：盘中快照抓取器（后台）+ 缓存API服务器（前台）
# 停止：./stop.sh
# ==============================================================================

cd "$(dirname "$0")"

echo "=========================================="
echo "  凡凡选股 · 全局缓存架构启动"
echo "=========================================="

# 检查 Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到 python3，请先安装 Python 3.8+"
    exit 1
fi

# 端口配置
PORT=${PORT:-8080}

# 启动盘中快照抓取器（后台）
echo ""
echo "📊 启动盘中快照抓取器（后台）..."
if [ -f "fanfan_snapshot.pid" ]; then
    OLD_PID=$(cat fanfan_snapshot.pid)
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "  快照抓取器已在运行 (PID: $OLD_PID)，跳过"
    else
        rm -f fanfan_snapshot.pid
        nohup python3 fanfan_snapshot.py > snapshot.log 2>&1 &
        echo $! > fanfan_snapshot.pid
        echo "  快照抓取器已启动 (PID: $!)"
    fi
else
    nohup python3 fanfan_snapshot.py > snapshot.log 2>&1 &
    echo $! > fanfan_snapshot.pid
    echo "  快照抓取器已启动 (PID: $!)"
fi

# 等待缓存初始化
sleep 2

# 启动API服务器（前台）
echo ""
echo "🚀 启动缓存API服务器（前台，端口 $PORT）..."
echo "   前端页面: http://localhost:$PORT/index_cache.html"
echo "   API健康检查: http://localhost:$PORT/api/health"
echo ""
echo "   按 Ctrl+C 停止API服务器（快照抓取器继续后台运行）"
echo "   完全停止: ./stop.sh"
echo "=========================================="
echo ""

python3 fanfan_server.py --port "$PORT"
