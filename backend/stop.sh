#!/bin/bash
# ==============================================================================
# 凡凡选股 · 停止脚本（stop.sh）
# 停止：盘中快照抓取器 + 缓存API服务器
# ==============================================================================

cd "$(dirname "$0")"

echo "=========================================="
echo "  凡凡选股 · 停止服务"
echo "=========================================="

# 停止快照抓取器
if [ -f "fanfan_snapshot.pid" ]; then
    PID=$(cat fanfan_snapshot.pid)
    if kill -0 "$PID" 2>/dev/null; then
        echo "📊 停止快照抓取器 (PID: $PID)..."
        kill "$PID"
        sleep 1
        if kill -0 "$PID" 2>/dev/null; then
            kill -9 "$PID"
        fi
        echo "  已停止"
    else
        echo "  快照抓取器未在运行"
    fi
    rm -f fanfan_snapshot.pid
else
    echo "  未找到快照抓取器PID文件"
fi

# 停止API服务器（通过端口查找）
PORT=${PORT:-8080}
API_PID=$(lsof -ti:$PORT 2>/dev/null || echo "")
if [ -n "$API_PID" ]; then
    echo "🚀 停止API服务器 (PID: $API_PID, 端口: $PORT)..."
    kill "$API_PID" 2>/dev/null
    sleep 1
    if kill -0 "$API_PID" 2>/dev/null; then
        kill -9 "$API_PID" 2>/dev/null
    fi
    echo "  已停止"
else
    echo "  API服务器未在运行（端口 $PORT）"
fi

# 清理数据库锁
rm -f fanfan_cache.db-wal fanfan_cache.db-shm 2>/dev/null

echo ""
echo "✅ 所有服务已停止"
echo "=========================================="
