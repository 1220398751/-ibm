#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 凡凡选股 · 全局缓存层（fanfan_cache.py）
================================================================================
  核心理念：全局单次抓取，多用户共享读取。
  服务器端每30秒抓取一次存入缓存，100个用户只读缓存，绝不各自请求东财。

  存储引擎：SQLite（Python 标准库，零依赖，单文件，适合轻量部署）
  表结构：
    - snapshot       盘中实时快照（全市场行情，每30秒 UPSERT）
    - zt_pool        涨停池（每日）
    - dt_pool        跌停池（每日）
    - lhb            龙虎榜明细（每日盘后）
    - kline          60日K线（盘后批量拉取，按股票代码存储）
    - scan_result    起爆前夜/情绪周期计算结果（盘后或盘中触发）
    - news           7×24快讯（增量更新）
    - meta           元数据（最后更新时间、抓取状态等）

  线程安全：所有写操作加锁，支持 snapshot.py 单写 + server.py 多读。
================================================================================
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime

# ==============================================================================
# 配置
# ==============================================================================
DB_PATH = os.environ.get("FANFAN_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "fanfan_cache.db"))

# 全局写锁（snapshot 单写，server 多读）
_write_lock = threading.Lock()


# ==============================================================================
# 数据库连接管理
# ==============================================================================
def get_conn():
    """获取数据库连接（check_same_thread=False 支持多线程）。"""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式，读写不阻塞
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db():
    """初始化数据库表结构（幂等，可重复调用）。"""
    conn = get_conn()
    try:
        c = conn.cursor()
        # 盘中实时快照
        c.execute("""
            CREATE TABLE IF NOT EXISTS snapshot (
                code TEXT PRIMARY KEY,
                name TEXT,
                price REAL,
                zdp REAL,
                hs REAL,
                ltsz REAL,
                amount REAL,
                mkt INTEGER,
                updated_at TEXT
            )
        """)
        # 涨停池
        c.execute("""
            CREATE TABLE IF NOT EXISTS zt_pool (
                code TEXT,
                date TEXT,
                name TEXT,
                price REAL,
                zdp REAL,
                ltsz REAL,
                hs REAL,
                lbc INTEGER,
                fbt INTEGER,
                fund REAL,
                zbc INTEGER,
                hybk TEXT,
                amount REAL,
                PRIMARY KEY (code, date)
            )
        """)
        # 跌停池
        c.execute("""
            CREATE TABLE IF NOT EXISTS dt_pool (
                code TEXT,
                date TEXT,
                name TEXT,
                price REAL,
                zdp REAL,
                ltsz REAL,
                fund REAL,
                PRIMARY KEY (code, date)
            )
        """)
        # 龙虎榜
        c.execute("""
            CREATE TABLE IF NOT EXISTS lhb (
                code TEXT,
                date TEXT,
                name TEXT,
                change REAL,
                turn REAL,
                buy REAL,
                sell REAL,
                net REAL,
                reason TEXT,
                PRIMARY KEY (code, date, reason)
            )
        """)
        # 60日K线
        c.execute("""
            CREATE TABLE IF NOT EXISTS kline (
                code TEXT PRIMARY KEY,
                data TEXT,
                updated_at TEXT
            )
        """)
        # 计算结果（起爆前夜/情绪周期等）
        c.execute("""
            CREATE TABLE IF NOT EXISTS scan_result (
                scan_type TEXT,
                date TEXT,
                data TEXT,
                updated_at TEXT,
                PRIMARY KEY (scan_type, date)
            )
        """)
        # 快讯
        c.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id TEXT PRIMARY KEY,
                title TEXT,
                summary TEXT,
                show_time TEXT,
                stock_list TEXT,
                senti INTEGER,
                created_at TEXT
            )
        """)
        # 元数据
        c.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TEXT
            )
        """)
        conn.commit()
    finally:
        conn.close()


# ==============================================================================
# 元数据操作
# ==============================================================================
def set_meta(key, value):
    """设置元数据（线程安全）。"""
    with _write_lock:
        conn = get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
                (key, json.dumps(value, ensure_ascii=False), now)
            )
            conn.commit()
        finally:
            conn.close()


def get_meta(key, default=None):
    """读取元数据。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        if row:
            return json.loads(row["value"])
        return default
    finally:
        conn.close()


# ==============================================================================
# 快照操作（盘中高频读写）
# ==============================================================================
def upsert_snapshot(stocks):
    """批量写入全市场快照（REPLACE，线程安全）。
    stocks: list of dict，每个包含 code/name/price/zdp/hs/ltsz/amount/mkt
    """
    if not stocks:
        return
    with _write_lock:
        conn = get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = [(
                s["code"], s.get("name"), s.get("price"), s.get("zdp"),
                s.get("hs"), s.get("ltsz"), s.get("amount"), s.get("mkt"), now
            ) for s in stocks]
            conn.executemany("""
                INSERT OR REPLACE INTO snapshot
                (code, name, price, zdp, hs, ltsz, amount, mkt, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            set_meta("snapshot_count", len(stocks))
            set_meta("snapshot_updated", now)
        finally:
            conn.close()


def get_snapshot(codes=None):
    """读取快照。codes=None 返回全部，否则返回指定代码。"""
    conn = get_conn()
    try:
        if codes:
            qmarks = ",".join("?" * len(codes))
            rows = conn.execute(
                f"SELECT * FROM snapshot WHERE code IN ({qmarks})", codes
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM snapshot").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==============================================================================
# 涨停/跌停池操作
# ==============================================================================
def upsert_zt_pool(pool, date_str):
    """批量写入涨停池。"""
    if not pool:
        return
    with _write_lock:
        conn = get_conn()
        try:
            rows = [(
                s["code"], date_str, s.get("name"), s.get("price"), s.get("zdp"),
                s.get("ltsz"), s.get("hs"), s.get("lbc"), s.get("fbt"),
                s.get("fund"), s.get("zbc"), s.get("hybk"), s.get("amount")
            ) for s in pool]
            conn.executemany("""
                INSERT OR REPLACE INTO zt_pool
                (code, date, name, price, zdp, ltsz, hs, lbc, fbt, fund, zbc, hybk, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
            set_meta(f"zt_count_{date_str}", len(pool))
        finally:
            conn.close()


def get_zt_pool(date_str):
    """读取某日涨停池。"""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM zt_pool WHERE date=?", (date_str,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def upsert_dt_pool(pool, date_str):
    """批量写入跌停池。"""
    if not pool:
        return
    with _write_lock:
        conn = get_conn()
        try:
            rows = [(
                s["code"], date_str, s.get("name"), s.get("price"),
                s.get("zdp"), s.get("ltsz"), s.get("fund")
            ) for s in pool]
            conn.executemany("""
                INSERT OR REPLACE INTO dt_pool
                (code, date, name, price, zdp, ltsz, fund)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
        finally:
            conn.close()


def get_dt_pool(date_str):
    """读取某日跌停池。"""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM dt_pool WHERE date=?", (date_str,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==============================================================================
# 龙虎榜操作
# ==============================================================================
def upsert_lhb(rows, date_str):
    """批量写入龙虎榜。"""
    if not rows:
        return
    with _write_lock:
        conn = get_conn()
        try:
            data = [(
                r["code"], date_str, r.get("name"), r.get("change"),
                r.get("turn"), r.get("buy"), r.get("sell"), r.get("net"), r.get("reason")
            ) for r in rows]
            conn.executemany("""
                INSERT OR REPLACE INTO lhb
                (code, date, name, change, turn, buy, sell, net, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, data)
            conn.commit()
        finally:
            conn.close()


def get_lhb(date_str):
    """读取某日龙虎榜。"""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT * FROM lhb WHERE date=?", (date_str,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ==============================================================================
# K线操作（盘后批量）
# ==============================================================================
def upsert_kline(code, data):
    """写入单只股票60日K线（data为list）。"""
    with _write_lock:
        conn = get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT OR REPLACE INTO kline (code, data, updated_at) VALUES (?, ?, ?)",
                (code, json.dumps(data, ensure_ascii=False), now)
            )
            conn.commit()
        finally:
            conn.close()


def get_kline(code):
    """读取单只股票K线。"""
    conn = get_conn()
    try:
        row = conn.execute("SELECT data FROM kline WHERE code=?", (code,)).fetchone()
        if row:
            return json.loads(row["data"])
        return None
    finally:
        conn.close()


# ==============================================================================
# 计算结果操作
# ==============================================================================
def set_scan_result(scan_type, date_str, data):
    """写入计算结果（起爆前夜/情绪周期等）。"""
    with _write_lock:
        conn = get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "INSERT OR REPLACE INTO scan_result (scan_type, date, data, updated_at) VALUES (?, ?, ?, ?)",
                (scan_type, date_str, json.dumps(data, ensure_ascii=False), now)
            )
            conn.commit()
        finally:
            conn.close()


def get_scan_result(scan_type, date_str):
    """读取计算结果。"""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT data FROM scan_result WHERE scan_type=? AND date=?",
            (scan_type, date_str)
        ).fetchone()
        if row:
            return json.loads(row["data"])
        return None
    finally:
        conn.close()


# ==============================================================================
# 快讯操作
# ==============================================================================
def upsert_news(news_list):
    """增量写入快讯（按id去重）。"""
    if not news_list:
        return
    with _write_lock:
        conn = get_conn()
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            rows = [(
                n.get("id") or n.get("art_code"),
                n.get("title"), n.get("summary"), n.get("show_time"),
                json.dumps(n.get("stockList") or [], ensure_ascii=False),
                n.get("_senti", 0), now
            ) for n in news_list]
            conn.executemany("""
                INSERT OR IGNORE INTO news
                (id, title, summary, show_time, stock_list, senti, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, rows)
            conn.commit()
        finally:
            conn.close()


def get_news(limit=80):
    """读取最新快讯。"""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM news ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["stockList"] = json.loads(d.get("stock_list") or "[]")
            d["_senti"] = d.get("senti", 0)
            result.append(d)
        return result
    finally:
        conn.close()


# ==============================================================================
# 统计信息
# ==============================================================================
def get_stats():
    """获取缓存统计信息（用于健康检查）。"""
    conn = get_conn()
    try:
        snapshot_count = conn.execute("SELECT COUNT(*) as c FROM snapshot").fetchone()["c"]
        news_count = conn.execute("SELECT COUNT(*) as c FROM news").fetchone()["c"]
        kline_count = conn.execute("SELECT COUNT(*) as c FROM kline").fetchone()["c"]
        return {
            "db_path": DB_PATH,
            "snapshot_count": snapshot_count,
            "news_count": news_count,
            "kline_count": kline_count,
            "snapshot_updated": get_meta("snapshot_updated"),
            "fetch_status": get_meta("fetch_status", "idle"),
            "last_error": get_meta("last_error"),
        }
    finally:
        conn.close()


# ==============================================================================
# 初始化（模块加载时自动建表）
# ==============================================================================
init_db()

if __name__ == "__main__":
    print(f"缓存数据库: {DB_PATH}")
    print(f"统计: {json.dumps(get_stats(), ensure_ascii=False, indent=2)}")
