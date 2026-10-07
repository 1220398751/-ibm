#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 凡凡选股 · 盘中快照抓取器（fanfan_snapshot.py）
================================================================================
  核心理念：全局单次抓取，多用户共享读取。
  本脚本是唯一的东财请求源，每30秒抓取一次轻量快照存入 SQLite 缓存。
  前端网页只从缓存服务器读数据，绝不直接接触东财接口。

  三大防护机制：
  1. 数据分层：盘中只抓快照（极轻），K线深度计算留给盘后 scanner.py
  2. 防重叠锁：上一轮没跑完，下一轮直接跳过，绝不堆积
  3. 指数退避：403/超时立即停止硬拉，等待时间指数增长（30s→60s→120s...上限10分钟）

  盘中抓取内容（每30秒）：
    - 全市场实时快照（现价/涨幅/换手/流通市值/成交额）—— URL_CLIST 分页
    - 涨停池 —— URL_ZT
    - 跌停池 —— URL_DT
    - 7×24快讯（增量）—— URL_NEWS

  运行方式：
    python3 fanfan_snapshot.py              # 前台运行（Ctrl+C 停止）
    python3 fanfan_snapshot.py --interval 30  # 自定义刷新间隔
    python3 fanfan_snapshot.py --once       # 只跑一次（用于测试/cron）
    nohup python3 fanfan_snapshot.py &      # 后台守护进程

  依赖：仅 Python 3.8+ 标准库 + 同目录 fanfan_cache.py
================================================================================
"""

import argparse
import json
import signal
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime

import fanfan_cache as cache

# ==============================================================================
# 常量与接口
# ==============================================================================
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 全市场实时行情（push2delay，CORS友好，pz上限100）
URL_CLIST = ("https://push2delay.eastmoney.com/api/qt/clist/get"
             "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
             "&fs={fs}&fields=f2,f3,f8,f10,f12,f14,f21")
CLIST_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"

# 涨停池
URL_ZT = ("https://push2ex.eastmoney.com/getTopicZTPool"
          "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
          "&Pageindex=0&pagesize=600&sort=fbt%3Aasc&date={date}")
# 跌停池
URL_DT = ("https://push2ex.eastmoney.com/getTopicDTPool"
          "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
          "&Pageindex=0&pagesize=600&sort=fund%3Aasc&date={date}")
# 快讯
URL_NEWS = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
            "?client=web&biz=web_724&fastColumn=102&sortEnd={se}"
            "&pageSize=80&req_trace=py{ts}")

# 利好/利空关键词
SENTI_BULL = ["涨停", "大涨", "利好", "中标", "签约", "增持", "回购", "预增", "获批", "通过",
              "突破", "涨价", "提价", "订单", "合作", "战略", "新高", "翻倍", "投产", "扩产",
              "量产", "批准", "收购", "并购", "合并", "重组", "注入", "降息", "降准", "补贴",
              "支持", "创新高", "签订"]
SENTI_BEAR = ["跌停", "大跌", "利空", "减持", "质押", "违规", "处罚", "立案", "警示", "预亏",
              "预减", "下滑", "退市", "解禁", "终止", "下修", "爆雷", "风险", "亏损", "召回",
              "调查", "诉讼", "冻结", "下调", "流拍", "失败", "低于预期"]

# ==============================================================================
# 全局状态
# ==============================================================================
_is_fetching = False          # 防重叠锁
_running = True                # 运行标志（Ctrl+C 时设为 False）
_consecutive_errors = 0       # 连续错误次数（用于指数退避）
_backoff_until = 0            # 退避截止时间戳


# ==============================================================================
# 基础工具
# ==============================================================================
def fetch_json(url, timeout=15):
    """请求 JSON，失败抛异常（由上层做退避）。"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def ymdn(d):
    return d.strftime("%Y%m%d")


def is_trading_hours():
    """判断当前是否在盘中交易时间（9:30-11:30, 13:00-15:00）。"""
    now = datetime.now().time()
    morning = dtime(9, 30) <= now <= dtime(11, 30)
    afternoon = dtime(13, 0) <= now <= dtime(15, 0)
    return morning or afternoon


def is_weekday():
    """判断是否为工作日（周一到周五）。"""
    return datetime.now().weekday() < 5


def log(msg):
    """带时间戳的日志。"""
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ==============================================================================
# 抓取函数（各数据源独立，失败不影响其他）
# ==============================================================================
def fetch_snapshot():
    """抓取全市场实时快照（分页，线程池并发）。
    返回 list of dict，每个包含 code/name/price/zdp/hs/ltsz/amount/mkt
    """
    all_stocks = []
    # 先拉第一页获取总数
    j = fetch_json(URL_CLIST.format(pn=1, fs=CLIST_FS))
    data = j.get("data") or {}
    total = data.get("total", 0)
    if total == 0:
        return []
    pages = (total + 99) // 100  # 向上取整

    def fetch_page(pn):
        try:
            j = fetch_json(URL_CLIST.format(pn=pn, fs=CLIST_FS))
            diff = (j.get("data") or {}).get("diff") or []
            return diff
        except Exception as e:
            log(f"  快照第{pn}页失败: {e}")
            return []

    # 线程池并发拉取各页（最多8线程）
    with ThreadPoolExecutor(max_workers=min(8, pages)) as ex:
        futures = [ex.submit(fetch_page, pn) for pn in range(1, pages + 1)]
        for f in as_completed(futures):
            all_stocks.extend(f.result())

    # 结构化
    result = []
    for s in all_stocks:
        code = str(s.get("f12") or "")
        if not code:
            continue
        result.append({
            "code": code,
            "name": s.get("f14"),
            "price": s.get("f2"),
            "zdp": s.get("f3"),
            "hs": s.get("f8"),
            "ltsz": s.get("f21"),  # 流通市值（元）
            "amount": s.get("f6"),
            "mkt": 1 if code.startswith("6") else 0,
        })
    return result


def fetch_zt_pool():
    """抓取涨停池。"""
    today = datetime.now()
    j = fetch_json(URL_ZT.format(date=ymdn(today)))
    pool = (j.get("data") or {}).get("pool") or []
    result = []
    for it in pool:
        result.append({
            "code": str(it.get("c")),
            "name": it.get("n"),
            "price": (it.get("p") or 0) / 1000,
            "zdp": it.get("zdp") or 0,
            "ltsz": it.get("ltsz") or 0,
            "hs": it.get("hs") or 0,
            "lbc": it.get("lbc") or 0,
            "fbt": it.get("fbt") or 0,
            "fund": it.get("fund") or 0,
            "zbc": it.get("zbc") or 0,
            "hybk": it.get("hybk") or "—",
            "amount": it.get("amount") or 0,
        })
    return result


def fetch_dt_pool():
    """抓取跌停池。"""
    today = datetime.now()
    j = fetch_json(URL_DT.format(date=ymdn(today)))
    pool = (j.get("data") or {}).get("pool") or []
    result = []
    for it in pool:
        result.append({
            "code": str(it.get("c")),
            "name": it.get("n"),
            "price": (it.get("p") or 0) / 1000,
            "zdp": it.get("zdp") or 0,
            "ltsz": it.get("ltsz") or 0,
            "fund": it.get("fund") or 0,
        })
    return result


def classify_senti(title, summary=""):
    """快讯利好/利空分类。返回 1=利好, -1=利空, 0=中性。"""
    text = (title or "") + (summary or "")
    bull = sum(1 for kw in SENTI_BULL if kw in text)
    bear = sum(1 for kw in SENTI_BEAR if kw in text)
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def fetch_news():
    """抓取7×24快讯（增量）。"""
    ts = int(time.time() * 1000)
    j = fetch_json(URL_NEWS.format(se="", ts=ts))
    data = j.get("data") or {}
    lst = data.get("list") or data.get("fastNewsList") or []
    result = []
    for n in lst:
        title = n.get("title") or n.get("Art_Title") or ""
        summary = n.get("summary") or n.get("Art_Content") or ""
        result.append({
            "id": n.get("art_code") or n.get("id") or str(ts) + str(len(result)),
            "title": title,
            "summary": summary,
            "show_time": n.get("show_time") or n.get("Art_ShowTime") or "",
            "stockList": n.get("stockList") or [],
            "_senti": classify_senti(title, summary),
        })
    return result


# ==============================================================================
# 单次抓取任务（核心）
# ==============================================================================
def fetch_once():
    """执行一次完整的盘中快照抓取。
    返回 (success_count, total_count) 用于统计。
    """
    global _is_fetching
    if _is_fetching:
        log("⏭ 上一轮未完成，跳过本轮（防重叠锁生效）")
        return 0, 0

    _is_fetching = True
    cache.set_meta("fetch_status", "fetching")
    start = time.time()
    success = 0
    total = 4  # 快照/涨停/跌停/快讯

    try:
        today_str = datetime.now().strftime("%Y-%m-%d")

        # 1. 全市场快照（最重，但仍是轻量接口）
        try:
            log("📊 抓取全市场快照...")
            stocks = fetch_snapshot()
            cache.upsert_snapshot(stocks)
            log(f"  ✓ 快照 {len(stocks)} 只")
            success += 1
        except Exception as e:
            log(f"  ✗ 快照失败: {e}")
            cache.set_meta("last_error", f"snapshot: {e}")

        # 2. 涨停池
        try:
            log("🔥 抓取涨停池...")
            zt = fetch_zt_pool()
            cache.upsert_zt_pool(zt, today_str)
            log(f"  ✓ 涨停 {len(zt)} 家")
            success += 1
        except Exception as e:
            log(f"  ✗ 涨停池失败: {e}")
            cache.set_meta("last_error", f"zt: {e}")

        # 3. 跌停池
        try:
            log("💧 抓取跌停池...")
            dt = fetch_dt_pool()
            cache.upsert_dt_pool(dt, today_str)
            log(f"  ✓ 跌停 {len(dt)} 家")
            success += 1
        except Exception as e:
            log(f"  ✗ 跌停池失败: {e}")
            cache.set_meta("last_error", f"dt: {e}")

        # 4. 快讯（增量，失败影响最小）
        try:
            log("📰 抓取快讯...")
            news = fetch_news()
            cache.upsert_news(news)
            log(f"  ✓ 快讯 {len(news)} 条")
            success += 1
        except Exception as e:
            log(f"  ✗ 快讯失败: {e}")

        elapsed = time.time() - start
        cache.set_meta("fetch_status", "idle")
        cache.set_meta("last_fetch_elapsed", round(elapsed, 2))
        log(f"✅ 本轮完成: {success}/{total} 成功，耗时 {elapsed:.1f}s")
        return success, total

    except Exception as e:
        cache.set_meta("fetch_status", "error")
        cache.set_meta("last_error", str(e))
        log(f"❌ 本轮异常: {e}")
        return success, total
    finally:
        _is_fetching = False


# ==============================================================================
# 指数退避逻辑
# ==============================================================================
def should_backoff():
    """检查是否处于退避期。返回 True 表示应跳过本轮。"""
    global _backoff_until
    now = time.time()
    if now < _backoff_until:
        remain = int(_backoff_until - now)
        log(f"⏳ 指数退避中，剩余 {remain}s（连续错误 {_consecutive_errors} 次）")
        return True
    return False


def update_backoff(success_count, total_count):
    """根据本轮结果更新退避状态。
    连续失败 → 退避时间指数增长（30s→60s→120s→240s→480s→600s上限）
    成功 → 重置错误计数
    """
    global _consecutive_errors, _backoff_until
    if success_count == 0:
        _consecutive_errors += 1
        # 指数退避：30 * 2^(n-1)，上限600秒（10分钟）
        delay = min(30 * (2 ** (_consecutive_errors - 1)), 600)
        _backoff_until = time.time() + delay
        log(f"⚠️ 本轮全部失败，进入指数退避：{delay}s 后重试（第{_consecutive_errors}次）")
    else:
        if _consecutive_errors > 0:
            log(f"✅ 恢复正常，重置退避（之前连续错误 {_consecutive_errors} 次）")
        _consecutive_errors = 0
        _backoff_until = 0


# ==============================================================================
# 主循环
# ==============================================================================
def signal_handler(sig, frame):
    """Ctrl+C 优雅退出。"""
    global _running
    log("\n🛑 收到停止信号，正在退出...")
    _running = False


def main():
    parser = argparse.ArgumentParser(description="凡凡选股 · 盘中快照抓取器")
    parser.add_argument("--interval", type=int, default=30, help="刷新间隔（秒），默认30")
    parser.add_argument("--once", action="store_true", help="只运行一次（测试用）")
    parser.add_argument("--force", action="store_true", help="强制运行（忽略交易时间判断）")
    args = parser.parse_args()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    log("=" * 60)
    log("🚀 凡凡选股 · 盘中快照抓取器启动")
    log(f"   刷新间隔: {args.interval}s | 缓存: {cache.DB_PATH}")
    log(f"   防重叠锁: 已启用 | 指数退避: 已启用（30s→10min）")
    log("=" * 60)

    if args.once:
        log("🔧 单次运行模式")
        fetch_once()
        return

    while _running:
        try:
            # 非交易时间不抓取（除非 --force）
            if not args.force and not (is_weekday() and is_trading_hours()):
                now = datetime.now().strftime("%H:%M:%S")
                log(f"💤 非交易时间 ({now})，等待中...（每60秒检查一次）")
                # 非交易时间每60秒检查一次是否进入交易时间
                for _ in range(60):
                    if not _running:
                        break
                    time.sleep(1)
                continue

            # 指数退避检查
            if should_backoff():
                time.sleep(args.interval)
                continue

            # 执行抓取
            success, total = fetch_once()
            update_backoff(success, total)

            # 等待下一轮
            for _ in range(args.interval):
                if not _running:
                    break
                time.sleep(1)

        except Exception as e:
            log(f"❌ 主循环异常: {e}")
            time.sleep(5)

    log("👋 抓取器已停止")
    cache.set_meta("fetch_status", "stopped")


if __name__ == "__main__":
    main()
