#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 凡凡选股 · 缓存 API 服务器（fanfan_server.py）
================================================================================
  核心理念：前端只读缓存，绝不直接接触东财接口。
  本服务器从 SQLite 缓存读取数据，通过 REST API 提供给前端网页。
  所有东财请求由 fanfan_snapshot.py（盘中）和 fanfan_scanner.py（盘后）完成。

  API 接口：
    GET  /api/health          健康检查
    GET  /api/stats           缓存统计信息
    GET  /api/snapshot        全市场实时快照（支持 ?codes=code1,code2 过滤）
    GET  /api/zt/{date}       涨停池（date 格式 YYYY-MM-DD，默认今天）
    GET  /api/dt/{date}       跌停池
    GET  /api/lhb/{date}      龙虎榜
    GET  /api/news            7×24快讯（?limit=80）
    GET  /api/scan/{type}     计算结果（type=boom/emotion，默认今天）
    GET  /api/kline/{code}    单只股票60日K线
    GET  /api/review          AI复盘结果（?date=YYYY-MM-DD）
    POST /api/review/generate 触发生成AI复盘（?date=YYYY-MM-DD&force=1&demo=1）
    GET  /api/review/status   复盘生成状态
    GET  /                     前端静态页面（index.html）

  运行方式：
    python3 fanfan_server.py --port 8080
    python3 fanfan_server.py --port 8080 --static ./web  # 指定静态文件目录

  依赖：仅 Python 3.8+ 标准库 + 同目录 fanfan_cache.py
================================================================================
"""

import argparse
import json
import os
import sys
import threading
import urllib.parse
from datetime import datetime
from http.server import HTTPServer, SimpleHTTPRequestHandler

import fanfan_cache as cache

# 复盘生成锁（防止同时生成多个复盘）
_review_lock = threading.Lock()
_review_generating = False

# ==============================================================================
# 配置
# ==============================================================================
DEFAULT_PORT = 8080
STATIC_DIR = os.path.dirname(os.path.abspath(__file__))  # 默认静态文件目录


# ==============================================================================
# CORS 与 JSON 响应工具
# ==============================================================================
def json_response(handler, data, status=200):
    """发送 JSON 响应（带 CORS 头）。"""
    body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler, message, status=404):
    """发送错误响应。"""
    json_response(handler, {"error": message, "status": status}, status=status)


# ==============================================================================
# 请求处理器
# ==============================================================================
class FanfanHandler(SimpleHTTPRequestHandler):
    """凡凡选股 API 请求处理器。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=STATIC_DIR, **kwargs)

    def log_message(self, format, *args):
        """简化日志输出。"""
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}", flush=True)

    def do_OPTIONS(self):
        """处理 CORS 预检请求。"""
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        """处理 GET 请求，路由到 API 或静态文件。"""
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)

        # API 路由
        if path.startswith("/api/"):
            self.handle_api(path, query)
            return

        # 静态文件（前端页面）
        super().do_GET()

    def handle_api(self, path, query):
        """API 路由分发。"""
        try:
            # 健康检查
            if path == "/api/health":
                json_response(self, {
                    "status": "ok",
                    "service": "fanfan-cache-server",
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "fetch_status": cache.get_meta("fetch_status", "unknown"),
                })
                return

            # 缓存统计
            if path == "/api/stats":
                json_response(self, cache.get_stats())
                return

            # 全市场快照
            if path == "/api/snapshot":
                codes = query.get("codes", [None])[0]
                if codes:
                    code_list = [c.strip() for c in codes.split(",") if c.strip()]
                    data = cache.get_snapshot(code_list)
                else:
                    data = cache.get_snapshot()
                json_response(self, {
                    "count": len(data),
                    "updated_at": cache.get_meta("snapshot_updated"),
                    "data": data,
                })
                return

            # 涨停池
            if path.startswith("/api/zt/"):
                date_str = path.split("/")[-1]
                if date_str == "today" or not date_str:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                data = cache.get_zt_pool(date_str)
                json_response(self, {"date": date_str, "count": len(data), "data": data})
                return

            # 跌停池
            if path.startswith("/api/dt/"):
                date_str = path.split("/")[-1]
                if date_str == "today" or not date_str:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                data = cache.get_dt_pool(date_str)
                json_response(self, {"date": date_str, "count": len(data), "data": data})
                return

            # 龙虎榜
            if path.startswith("/api/lhb/"):
                date_str = path.split("/")[-1]
                if date_str == "today" or not date_str:
                    date_str = datetime.now().strftime("%Y-%m-%d")
                data = cache.get_lhb(date_str)
                json_response(self, {"date": date_str, "count": len(data), "data": data})
                return

            # 快讯
            if path == "/api/news":
                limit = int(query.get("limit", [80])[0])
                data = cache.get_news(limit=limit)
                json_response(self, {"count": len(data), "data": data})
                return

            # 计算结果（起爆前夜/情绪周期）
            if path.startswith("/api/scan/"):
                scan_type = path.split("/")[-1]
                date_str = query.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
                data = cache.get_scan_result(scan_type, date_str)
                if data is None:
                    error_response(self, f"未找到 {scan_type} 计算结果（{date_str}），请先运行 fanfan_scanner.py", status=404)
                    return
                json_response(self, {"type": scan_type, "date": date_str, "data": data})
                return

            # K线
            if path.startswith("/api/kline/"):
                code = path.split("/")[-1]
                data = cache.get_kline(code)
                if data is None:
                    error_response(self, f"未找到 {code} 的K线数据，请先运行 fanfan_scanner.py --type kline", status=404)
                    return
                json_response(self, {"code": code, "count": len(data), "data": data})
                return

            # AI复盘 - 获取结果
            if path == "/api/review":
                date_str = query.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
                data = cache.get_scan_result("review", date_str)
                if data is None:
                    json_response(self, {
                        "date": date_str,
                        "status": "not_generated",
                        "message": "复盘尚未生成，请调用 POST /api/review/generate 触发生成",
                        "generating": _review_generating,
                    })
                    return
                json_response(self, {
                    "date": date_str,
                    "status": "ready",
                    "content": data.get("content"),
                    "generated_at": data.get("generated_at"),
                    "mode": data.get("mode"),
                    "generating": _review_generating,
                })
                return

            # AI复盘 - 触发生成（异步）
            if path == "/api/review/generate":
                date_str = query.get("date", [datetime.now().strftime("%Y-%m-%d")])[0]
                force = query.get("force", ["0"])[0] == "1"
                demo = query.get("demo", ["0"])[0] == "1"

                if _review_generating:
                    json_response(self, {
                        "status": "already_generating",
                        "message": "复盘正在生成中，请稍候...",
                        "date": date_str,
                    })
                    return

                def _generate_async():
                    global _review_generating
                    with _review_lock:
                        _review_generating = True
                    try:
                        import fanfan_review as review_mod
                        review_mod.generate_review(date_str=date_str, force=force, demo=demo)
                    except Exception as e:
                        print(f"[复盘生成失败] {e}")
                    finally:
                        with _review_lock:
                            _review_generating = False

                threading.Thread(target=_generate_async, daemon=True).start()
                json_response(self, {
                    "status": "started",
                    "message": "复盘生成已启动，请通过 GET /api/review 查询结果",
                    "date": date_str,
                    "estimated_time": "30-60秒",
                })
                return

            # AI复盘 - 生成状态
            if path == "/api/review/status":
                json_response(self, {
                    "generating": _review_generating,
                    "message": "正在生成中..." if _review_generating else "空闲",
                })
                return

            # 未匹配的 API
            error_response(self, f"未知 API: {path}", status=404)

        except Exception as e:
            error_response(self, f"服务器内部错误: {str(e)}", status=500)


# ==============================================================================
# 主入口
# ==============================================================================
def main():
    global STATIC_DIR
    parser = argparse.ArgumentParser(description="凡凡选股 · 缓存 API 服务器")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口，默认{DEFAULT_PORT}")
    parser.add_argument("--host", type=str, default="0.0.0.0", help="监听地址，默认0.0.0.0（所有网卡）")
    parser.add_argument("--static", type=str, default=None, help="静态文件目录，默认脚本所在目录")
    args = parser.parse_args()

    if args.static:
        STATIC_DIR = args.static

    print("=" * 60)
    print("🚀 凡凡选股 · 缓存 API 服务器启动")
    print(f"   监听: http://{args.host}:{args.port}")
    print(f"   缓存: {cache.DB_PATH}")
    print(f"   静态文件: {STATIC_DIR}")
    print("=" * 60)
    print("API 接口：")
    print("  GET /api/health          健康检查")
    print("  GET /api/stats           缓存统计")
    print("  GET /api/snapshot        全市场快照")
    print("  GET /api/zt/{date}       涨停池")
    print("  GET /api/dt/{date}       跌停池")
    print("  GET /api/lhb/{date}      龙虎榜")
    print("  GET /api/news            快讯")
    print("  GET /api/scan/{type}     计算结果（boom/emotion）")
    print("  GET /api/kline/{code}    K线")
    print("  GET /                     前端页面")
    print("=" * 60)
    print("⚠️  本服务器只读缓存，不直接请求东财接口。")
    print("   请确保 fanfan_snapshot.py（盘中）和 fanfan_scanner.py（盘后）正在运行。")
    print("=" * 60)

    try:
        server = HTTPServer((args.host, args.port), FanfanHandler)
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n🛑 服务器已停止")
        server.server_close()
    except OSError as e:
        print(f"❌ 端口 {args.port} 被占用: {e}")
        print(f"   请使用 --port 参数指定其他端口，例如: python3 fanfan_server.py --port 8081")
        sys.exit(1)


if __name__ == "__main__":
    main()
