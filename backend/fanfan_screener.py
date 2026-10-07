#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 凡凡选股 · Python 版（A股短线数据筛选 + 妖股概率评分）
================================================================================
  对应网页版「凡凡选股」（单文件 HTML）的核心逻辑，用纯 Python 重新实现。
  数据源：东方财富公开接口（涨停池 / 龙虎榜 / 分时 / 7×24 快讯）。

  功能：
    1. 拉取指定交易日的涨停池（93 家级别）、龙虎榜明细（盘后披露）
    2. 近三日涨停合并筛选（三交易日内有过涨停）
    3. 核心条件：流通市值 ≤ 上限 / 最低连板数 / 排除 ST
    4. 竞价条件：9:25 竞价涨幅 > X%（估算模式逐股拉分时真实计算）
               竞价换手率 > X%、委卖 > 委买（需 Level-2，公开接口不可得，
               仅演示模式生成示例数据）
    5. 妖股概率六维评分（满分 100）：连板高度 + 封板强度 + 活跃度 +
       市值 + 龙虎榜资金 + 板块热度
    6. 板块透视：涨停池 + 龙虎榜按行业板块聚合资金流向
    7. 快讯利好 / 利空标注（关键词规则，仅供参考）
    8. 输出：控制台表格 / CSV / HTML 报告

  依赖：仅 Python 3.8+ 标准库（urllib / json / csv / argparse），无第三方库。

  运行示例：
    python3 fanfan_screener.py                          # 默认今天，核心条件筛选
    python3 fanfan_screener.py --date 2026-09-04        # 指定交易日
    python3 fanfan_screener.py --mcap 200 --lb 1        # 市值≤200亿，至少二板
    python3 fanfan_screener.py --score 55               # 只看评分≥55 的候选
    python3 fanfan_screener.py --auction                # 逐股拉分时计算 9:25 竞价涨幅（较慢）
    python3 fanfan_screener.py --boom --neg --score 60   # 起爆前夜：基因→K线粘合→公告过滤→评分
    python3 fanfan_screener.py --demo                   # 竞价数据用演示模式（含示例数据）
    python3 fanfan_screener.py --csv out.csv --html out.html   # 导出 CSV 与 HTML 报告

  风险提示：本工具仅作数据整理与筛选参考，不构成任何投资建议。
            妖股概率为多因子模型评分，不预示任何实际走势。
            数据来自东方财富公开接口，可能存在延迟或缺失。
================================================================================
"""

import argparse
import csv
import json
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date as _date, datetime, timedelta

# ==============================================================================
# 常量与接口地址
# ==============================================================================
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 涨停池：返回当日所有涨停股（含连板、封板资金、炸板次数、行业板块等）
URL_ZT = ("https://push2ex.eastmoney.com/getTopicZTPool"
          "?ut=7eea3edcaed734bea9cbfc24409ed989&dpt=wz.ztzt"
          "&Pageindex=0&pagesize=600&sort=fbt%3Aasc&date={date}")
# 龙虎榜：盘后约 17:00 披露，按交易日过滤
URL_LHB = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
           "?reportName=RPT_DAILYBILLBOARD_DETAILSNEW&columns=ALL"
           "&pageNumber=1&pageSize=600&sortTypes=-1"
           "&sortColumns=BILLBOARD_NET_AMT&source=WEB&client=WEB"
           "&filter=(TRADE_DATE%3D'{date}')")
# 分时：第一条为 9:15 集合竞价，9:25 为竞价定盘点（f2 时间形如 2609070925）
URL_TRENDS = ("https://push2his.eastmoney.com/api/qt/stock/trends/get"
              "?secid={secid}&fields1=f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13"
              "&fields2=f51,f52,f53,f54,f55,f56,f57,f58&ndays=1&iscr=0")
# 7×24 快讯：无 Referer 时正常返回（带外部 Referer 会被防火墙拦截）
URL_NEWS = ("https://np-listapi.eastmoney.com/comm/web/getFastNewsList"
            "?client=web&biz=web_724&fastColumn=102&sortEnd={se}"
            "&pageSize=80&req_trace=py{ts}")

# ===== 起爆前夜（v5 新增）=====
# 全市场实时行情：push2delay 节点（CORS 友好），pz 上限 100，fid=f12 按代码排序保证分页稳定
URL_CLIST = ("https://push2delay.eastmoney.com/api/qt/clist/get"
             "?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f12"
             "&fs={fs}&fields=f2,f3,f8,f10,f12,f14,f21")
BOOM_FS = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
# 60日K线：腾讯复权日K（CORS），61 根
URL_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={secid},day,,,60,qfq"
# 个股公告：东财（CORS），用于负面过滤
URL_ANN = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
           "?sr=-1&page_size=10&page_index=1&ann_type=A&client_source=web&stock_list={code}")
# 概念板块涨幅榜（参考展示）
URL_BOARD = ("https://push2delay.eastmoney.com/api/qt/clist/get"
             "?pn=1&pz=8&po=1&np=1&fltt=2&invt=2&fid=f3&fs=m:90+t:2+f:!50&fields=f3,f14")
# 负面公告硬雷词（命中即剔除）与提示词（仅标记不剔除）
BOOM_BEAR_KW = ["减持", "质押", "违规", "处罚", "立案", "警示", "预亏", "预减", "退市", "解禁", "终止", "下修",
                "爆雷", "诉讼", "冻结", "下调", "流拍", "失败", "低于预期", "问询函", "监管函", "关注函", "亏损",
                "逾期", "失信", "被执行", "商誉减值", "业绩变脸", "清仓", "减持计划", "立案调查"]
BOOM_WARN_KW = ["风险提示", "异常波动"]

# 妖股概率 · 六维评分（与网页版完全一致）
SCORE_LB = {1: 8, 2: 14, 3: 19, 4: 23}          # 连板（≥5 板按 25 分）
SCORE_FBT = {935: 8, 1000: 6, 1030: 4}          # 首封时间（9:35 前 / 10:00 前 / 10:30 前 / 其他 2 分）
SCORE_FUND_RATIO = {5: 7, 2: 5, 0.5: 3}         # 封单比（封板资金/流通市值 %）
SCORE_ZBC = {0: 5, 1: 2}                        # 炸板次数（0 次 / 1 次 / 其他 1 分）
SCORE_TURN = [(5, 20, 15), (3, 30, 10), (1, 40, 6)]   # 换手率区间 → 活跃分（其余 3）
SCORE_MCAP = [(30, 15), (60, 12), (100, 9), (200, 6)] # 流通市值（亿）→ 市值分（其余 3）
SCORE_SECTOR_HOT = {5: 10, 4: 7, 3: 6, 2: 4, 1: 2}    # 板块内涨停家数 → 板块热度分

# 妖股档位
LEVELS = [
    (85, "妖王候选 lv4"),
    (70, "妖气冲天 lv3"),
    (55, "雏妖初现 lv2"),
    (40, "蓄势待发 lv1"),
]

# 利好 / 利空关键词（快讯情绪判断，A 股语义：红涨绿跌）
SENTI_BULL = ["涨停", "大涨", "利好", "中标", "签约", "增持", "回购", "预增", "获批", "通过",
              "突破", "涨价", "提价", "订单", "合作", "战略", "新高", "翻倍", "投产", "扩产",
              "量产", "批准", "收购", "并购", "合并", "重组", "注入", "降息", "降准", "补贴",
              "支持", "创新高", "签订"]
SENTI_BEAR = ["跌停", "大跌", "利空", "减持", "质押", "违规", "处罚", "立案", "警示", "预亏",
              "预减", "下滑", "退市", "解禁", "终止", "下修", "爆雷", "风险", "亏损", "召回",
              "调查", "诉讼", "冻结", "下调", "流拍", "失败", "低于预期"]


# ==============================================================================
# 基础工具
# ==============================================================================
def fetch_json(url, timeout=20, retries=3):
    """请求 JSON 接口，失败自动重试（间隔递增）。"""
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "ignore"))
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(0.7 * (i + 1))
    raise RuntimeError(f"接口请求失败: {last}")


def ymd(d):
    return d.strftime("%Y-%m-%d")


def ymdn(d):
    return d.strftime("%Y%m%d")


def add_days(d, n):
    return d + timedelta(days=n)


def fmt_wan(v):
    """金额格式化：亿 / 万 / 元。"""
    if v is None or v != v:  # NaN
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v / 1e8:.2f}亿"
    if a >= 1e4:
        return f"{v / 1e4:.0f}万"
    return f"{v:.0f}"


def fmt_pct(v, d=2):
    if v is None or v != v:
        return "—"
    return ("+" if v > 0 else "") + f"{v:.{d}f}%"


def fmt_time(fbt):
    """首封时间 92501 → 09:25（先补零再切分）。"""
    try:
        s = str(int(fbt)).zfill(6)
        return f"{s[:2]}:{s[2:4]}"
    except Exception:
        return "—"


def norm_sector(name):
    """板块名归一化：去罗马数字、去空白。"""
    if not name:
        return ""
    for c in "ⅠⅡⅢⅣⅤ":
        name = name.replace(c, "")
    return name.replace(" ", "").replace("\u3000", "")


# ==============================================================================
# 数据层
# ==============================================================================
def load_zt_pool(date_str):
    """拉取某交易日涨停池 → 结构化列表。"""
    j = fetch_json(URL_ZT.format(date=ymdn(datetime.strptime(date_str, "%Y-%m-%d"))))
    pool = (j.get("data") or {}).get("pool") or []
    out = []
    for it in pool:
        out.append({
            "code": str(it.get("c")),
            "mkt": it.get("m"),
            "name": it.get("n"),
            "price": (it.get("p") or 0) / 1000,  # 涨停池 p 单位为厘（0.001元）
            "zdp": it.get("zdp") or 0,
            "amount": it.get("amount") or 0,
            "ltsz": it.get("ltsz") or 0,          # 流通市值（元）
            "hs": it.get("hs") or 0,              # 换手率 %
            "lbc": it.get("lbc") or 0,            # 连板数
            "fbt": it.get("fbt") or 0,            # 首封时间
            "lbt": it.get("lbt") or 0,
            "fund": it.get("fund") or 0,          # 封板资金（元）
            "zbc": it.get("zbc") or 0,            # 炸板次数
            "hybk": it.get("hybk") or "—",
            "zttj_days": (it.get("zttj") or {}).get("days") or 0,
            "date": date_str,
        })
    return out


def load_lhb(date_str):
    """拉取某交易日龙虎榜明细 → 结构化列表（盘后披露，当日盘中为空）。"""
    j = fetch_json(URL_LHB.format(date=date_str))
    rows = ((j.get("result") or {}).get("data")) or []
    out = []
    for it in rows:
        out.append({
            "code": str(it.get("SECURITY_CODE")),
            "name": it.get("SECURITY_NAME_ABBR"),
            "change": it.get("CHANGE_RATE"),
            "turn": it.get("TURNOVERRATE"),
            "buy": it.get("BILLBOARD_BUY_AMT") or 0,
            "sell": it.get("BILLBOARD_SELL_AMT") or 0,
            "net": it.get("BILLBOARD_NET_AMT") or 0,
            "buy_r": it.get("BUY_RATIO"),
            "sell_r": it.get("SELL_RATIO"),
            "deal_r": it.get("DEAL_AMOUNT_RATIO"),
            "accum": it.get("ACCUM_AMOUNT") or 0,
            "fcap": it.get("FREE_MARKET_CAP") or 0,
            "reason": it.get("EXPLANATION") or "—",
            "inst": it.get("EXPLAIN") or "",
        })
    return out


def load_trends(mkt, code):
    """拉取个股分时（旧接口，CORS 友好）。mkt: 1=沪, 0=深。"""
    secid = f"{mkt}.{code}"
    j = fetch_json(URL_TRENDS.format(secid=secid))
    return j.get("data") or []


def extract_open(trends):
    """从分时中提取 9:25 竞价定盘价（分），返回 None 表示不可用。"""
    if not trends:
        return None
    for pt in trends:
        t = str(pt.get("f2") or "")
        if len(t) >= 12 and t[8:12] == "0925":
            v = pt.get("f4")  # 收（即该分钟收盘价）
            if v and v > 0:
                return v
            break
    first = trends[0]
    if first and first.get("f3"):
        return first["f3"]
    return None


def load_news(pages=3):
    """拉取 7×24 快讯（分页合并，第一页失败才报错）。"""
    seen = {}
    se = ""
    for p in range(pages):
        try:
            j = fetch_json(URL_NEWS.format(se=se, ts=time.time()))
        except Exception:
            if p == 0:
                raise
            break
        lst = ((j.get("data") or {}).get("fastNewsList")) or []
        if not lst:
            break
        for n in lst:
            seen[n.get("code") or n.get("id") or len(seen)] = n
        se = (j.get("data") or {}).get("sortEnd") or ""
        if not se:
            break
    return list(seen.values())


# ==============================================================================
# 筛选与评分
# ==============================================================================
def near3_days(date_str, pool):
    """近三个有数据的交易日涨停池合并（含所选日，往前探测最多 6 个自然日）。"""
    d = datetime.strptime(date_str, "%Y-%m-%d").date()
    groups = {date_str: pool}
    near = [pool] if pool else []
    for i in range(1, 7):
        if len(near) >= 3:
            break
        ds = ymd(add_days(d, -i))
        try:
            p = load_zt_pool(ds)
        except Exception:
            p = []
        if p:
            groups[ds] = p
            near.append(p)
    return groups, near


def prev_close(stock):
    """反推昨收：当日涨停用 price/(1+zdp/100)；否则用最近一次涨停收盘价近似。"""
    if stock["zdp"] and abs(stock["zdp"] - 100) < 0.01:
        return stock["price"] / (1 + stock["zdp"] / 100)
    return stock.get("_last_zt_price") or stock["price"]


def score_stock(s, lhb_map, sector_hot):
    """妖股六维评分（满分 100），返回 (总分, 各维度明细, 档位)。"""
    lbc = s["lbc"]
    lb_score = SCORE_LB.get(lbc, 25 if lbc >= 5 else 2)

    # 封板强度：首封时间 + 封单比 + 炸板次数
    fbt_min = int(s["fbt"]) // 100
    fbt_score = 2
    for t in (935, 1000, 1030):
        if fbt_min <= t:
            fbt_score = SCORE_FBT[t]
            break
    fund_ratio = (s["fund"] / s["ltsz"] * 100) if s["ltsz"] else 0
    fr_score = 1
    for t in (5, 2, 0.5):
        if fund_ratio >= t:
            fr_score = SCORE_FUND_RATIO[t]
            break
    zbc_score = SCORE_ZBC.get(s["zbc"], 1)
    seal_score = min(fbt_score + fr_score + zbc_score, 20)

    # 活跃度（换手率）
    turn = s["hs"]
    act_score = 3
    for lo, hi, sc in SCORE_TURN:
        if lo <= turn <= hi:
            act_score = sc
            break

    # 市值
    mcap_yi = s["ltsz"] / 1e8
    mcap_score = 3
    for cap, sc in SCORE_MCAP:
        if mcap_yi < cap:
            mcap_score = sc
            break

    # 龙虎榜资金（默认 7 分；净买>0 且占比>10% → 12，否则 9；净卖 → 4；机构买入 +3）
    lb = lhb_map.get(s["code"])
    if lb:
        if lb["net"] > 0:
            lhb_score = 12 if (lb["deal_r"] or 0) > 10 else 9
        else:
            lhb_score = 4
        if "机构" in (lb["inst"] or ""):
            lhb_score = min(lhb_score + 3, 15)
    else:
        lhb_score = 7

    # 板块热度
    hot_score = SCORE_SECTOR_HOT.get(sector_hot.get(s["hybk"], 0), 0)

    total = min(lb_score + seal_score + act_score + mcap_score + lhb_score + hot_score, 100)
    lv = "观望 lv0"
    for thr, name in LEVELS:
        if total >= thr:
            lv = name
            break
    detail = {
        "连板": lb_score, "封板": seal_score, "活跃": act_score,
        "市值": mcap_score, "龙虎榜资金": lhb_score, "板块热度": hot_score,
    }
    return total, detail, lv, fund_ratio


def demo_auction(stocks, cap_max):
    """演示模式：为候选股生成带标记的示例竞价数据（公开接口无批量竞价挂单）。"""
    demo = {}
    import random
    random.seed(7)
    for s in stocks:
        if s["ltsz"] > cap_max:
            continue
        auc_up = round(random.uniform(-3, 8), 2)
        demo[s["code"]] = {
            "open_px": s["price"] * (1 + auc_up / 100),
            "auc_up": auc_up,
            "auc_turn": round(random.uniform(0.5, 6), 2),
            "sell_vol": round(random.uniform(1, 20), 2),
            "buy_vol": round(random.uniform(1, 20), 2),
            "demo": True,
        }
    return demo


# ==============================================================================
# 板块透视与快讯情绪
# ==============================================================================
def sector_aggregate(pool, lhb):
    """涨停池按行业板块聚合 + 龙虎榜净买按代码映射聚合。"""
    agg = {}
    for s in pool:
        k = s["hybk"] or "其他"
        g = agg.setdefault(k, {
            "name": k, "zt": 0, "lb": 0, "max_lb": 0, "seal": 0,
            "zbc": 0, "net": 0, "lhb_n": 0, "codes": set(),
        })
        g["zt"] += 1
        if s["lbc"] >= 2:
            g["lb"] += 1
        g["max_lb"] = max(g["max_lb"], s["lbc"])
        g["seal"] += s["fund"]
        g["zbc"] += s["zbc"]
        g["codes"].add(s["code"])
    code2sec = {c: k for k, g in agg.items() for c in g["codes"]}
    for r in lhb:
        k = code2sec.get(str(r["code"]))
        if k and k in agg:
            agg[k]["net"] += r["net"]
            agg[k]["lhb_n"] += 1
    return agg, code2sec


def news_sentiment(title):
    """关键词规则判断利好/利空/中性（A 股语义：红涨绿跌）。"""
    b = sum(1 for w in SENTI_BULL if w in title)
    be = sum(1 for w in SENTI_BEAR if w in title)
    return "利好" if b > be else ("利空" if be > b else "中性")


def match_news(news, agg, code2sec):
    """快讯 → 板块匹配（关联个股代码 + 板块名关键词）。"""
    kw_map = {}
    for k in agg:
        n = norm_sector(k)
        if len(n) >= 2 and n not in kw_map:
            kw_map[n] = k
    matched = []
    for n in news:
        sects = set()
        for st in (n.get("stockList") or []):
            code = str(st).split(".")[-1]
            if code in code2sec:
                sects.add(code2sec[code])
        t = (n.get("title") or "") + (n.get("summary") or "")
        for kw, sec in kw_map.items():
            if kw in t:
                sects.add(sec)
        if sects:
            matched.append({"news": n, "sectors": sorted(sects),
                            "senti": news_sentiment(t)})
    return matched


# ==============================================================================
# 输出
# ==============================================================================
def console_report(rows, stats, sectors, matched_news, date_str):
    """控制台表格输出。"""
    w = print
    w("=" * 100)
    w(f"  凡凡选股 · {date_str}  涨停 {stats['zt']} 家 | 连板 {stats['lb']} 只 | "
      f"龙虎榜 {stats['lhb']} 条 | 命中 {stats['hit']} 只")
    w("=" * 100)
    if not rows:
        w("  无满足条件的股票，请放宽筛选或切换交易日。")
    for r in rows:
        w(f"  {r['code']} {r['name']}  连板{r['lbc']}板  评分{r['score']} "
          f"[{r['level']}]  板块:{r['hybk']}  现价{r['price']:.2f} "
          f"涨幅{fmt_pct(r['zdp'])}  市值{r['ltsz']/1e8:.0f}亿 "
          f"换手{r['hs']:.1f}%  封单比{r['fund_ratio']:.1f}%  龙虎榜净买{fmt_wan(r['lhb_net'])}")
        if r.get("auc_up") is not None:
            w(f"       竞价涨幅{fmt_pct(r['auc_up'])} 竞价换手{r.get('auc_turn','—')}% "
              f"委卖{r.get('sell_vol','—')} 委买{r.get('buy_vol','—')}"
              f"{'  [演示]' if r.get('auc_demo') else ''}")
    if sectors:
        w("-" * 100)
        w("  板块资金流（涨停池 + 龙虎榜聚合）")
        for g in sorted(sectors.values(), key=lambda x: -x["net"])[:8]:
            w(f"  {g['name']:<10} 涨停{g['zt']:>3} 连板{g['lb']:>2} 最高{g['max_lb']}板 "
              f"封板{fmt_wan(g['seal'])} 净买{fmt_wan(g['net'])} 上榜{g['lhb_n']} 炸板{g['zbc']}")
    if matched_news:
        w("-" * 100)
        w("  板块相关快讯（利好/利空/中性）")
        for m in matched_news[:10]:
            w(f"  [{m['senti']}] [{','.join(m['sectors'])}] {(m['news'].get('title') or '')[:46]}")
    w("=" * 100)
    w("  风险提示：仅作数据整理参考，不构成投资建议。股市有风险，交易需谨慎。")


def export_csv(rows, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["日期", "代码", "名称", "现价", "涨跌幅%", "连板数", "流通市值亿", "换手率%",
                    "首封时间", "封板资金", "封单比%", "炸板次数", "板块", "评分", "妖股档位",
                    "龙虎榜净买", "龙虎榜占比%", "上榜原因", "竞价涨幅%", "竞价换手%", "是否演示"])
        for r in rows:
            w.writerow([r["date"], r["code"], r["name"], f"{r['price']:.2f}",
                        f"{r['zdp']:.2f}", r["lbc"], f"{r['ltsz']/1e8:.2f}",
                        f"{r['hs']:.2f}", fmt_time(r["fbt"]), int(r["fund"]),
                        f"{r['fund_ratio']:.2f}", r["zbc"], r["hybk"], r["score"],
                        r["level"], r["lhb_net"], r["lhb_deal_r"], r["lhb_reason"],
                        r.get("auc_up", ""), r.get("auc_turn", ""),
                        "是" if r.get("auc_demo") else ""])
    print(f"CSV 已导出: {path}")


def export_html(rows, stats, sectors, matched_news, date_str, path):
    """导出与网页版同主题的深色 HTML 报告。"""
    trs = []
    for r in rows:
        up = "up" if r["zdp"] > 0 else "down"
        auc = ""
        if r.get("auc_up") is not None:
            auc = f"{fmt_pct(r['auc_up'])} / {r.get('auc_turn','—')}%" \
                  + (" (演示)" if r.get("auc_demo") else "")
        trs.append(
            f"<tr><td>{r['code']}</td><td>{r['name']}</td>"
            f"<td class='{up}'>{r['lbc']}板</td><td>{fmt_pct(r['zdp'])}</td>"
            f"<td>{r['ltsz']/1e8:.0f}亿</td><td>{r['hs']:.1f}%</td>"
            f"<td>{fmt_time(r['fbt'])}</td><td>{fmt_wan(r['fund'])}</td>"
            f"<td>{r['fund_ratio']:.1f}%</td><td>{r['hybk']}</td>"
            f"<td><b>{r['score']}</b></td><td>{r['level']}</td>"
            f"<td class='{('up' if r['lhb_net']>0 else 'down') if r['lhb_net'] else ''}'>{fmt_wan(r['lhb_net'])}</td>"
            f"<td>{auc}</td></tr>")
    secs = "".join(
        f"<tr><td>{g['name']}</td><td>{g['zt']}</td><td>{g['lb']}</td>"
        f"<td>{g['max_lb']}板</td><td>{fmt_wan(g['seal'])}</td>"
        f"<td class='{'up' if g['net']>=0 else 'down'}'>{fmt_wan(g['net'])}</td>"
        f"<td>{g['lhb_n']}</td><td>{g['zbc']}</td></tr>"
        for g in sorted(sectors.values(), key=lambda x: -x["net"])[:10])
    news = "".join(
        f"<li><span class='tag {('bull' if m['senti']=='利好' else 'bear' if m['senti']=='利空' else 'neu')}'>"
        f"{m['senti']}</span> <b>[{','.join(m['sectors'])}]</b> "
        f"{(m['news'].get('title') or '')[:60]}</li>"
        for m in matched_news[:15])
    html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>凡凡选股 · {date_str} 报告</title>
<style>
:root{{--bg:#0b0f14;--panel:#121922;--border:#22303e;--text:#e3ebf5;--muted:#8fa3b8;--red:#ff5d5d;--down:#31c48d;--gold:#ffd166}}
*{{box-sizing:border-box}}body{{background:var(--bg);color:var(--text);font-family:'Noto Sans SC',system-ui,sans-serif;margin:0;padding:16px}}
h1{{font-size:20px}}h2{{font-size:15px;color:var(--gold);margin-top:26px}}
.card{{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px;margin-top:12px;overflow-x:auto}}
table{{width:100%;border-collapse:collapse;font-size:12px;min-width:760px}}
th,td{{padding:7px 8px;border-bottom:1px solid #182230;text-align:right;white-space:nowrap}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2),th:nth-child(10),td:nth-child(10){{text-align:left}}
.up{{color:var(--red)}}.down{{color:var(--down)}}
.tag{{display:inline-block;font-size:10px;border-radius:8px;padding:1px 6px;margin-right:4px}}
.bull{{background:rgba(255,93,93,.15);color:var(--red)}}.bear{{background:rgba(49,196,141,.15);color:var(--down)}}.neu{{background:#2a3644;color:var(--muted)}}
.kpi{{display:flex;gap:10px;flex-wrap:wrap}} .kpi div{{background:var(--panel);border:1px solid var(--border);border-radius:10px;padding:10px 14px}}
.kpi b{{display:block;font-size:20px;color:var(--gold)}} .kpi span{{font-size:11px;color:var(--muted)}}
.foot{{margin-top:20px;font-size:11px;color:var(--muted)}}
</style></head><body>
<h1>凡凡选股 · {date_str}</h1>
<div class="kpi">
<div><b>{stats['zt']}</b><span>当日涨停家数</span></div>
<div><b>{stats['lb']}</b><span>连板股合计</span></div>
<div><b>{stats['lhb']}</b><span>龙虎榜上榜</span></div>
<div><b>{stats['hit']}</b><span>命中全部筛选</span></div>
</div>
<h2>妖股候选（六维评分）</h2>
<div class="card"><table>
<tr><th>代码</th><th>名称</th><th>连板</th><th>涨跌幅</th><th>流通市值</th><th>换手</th><th>首封</th><th>封板资金</th><th>封单比</th><th>板块</th><th>评分</th><th>档位</th><th>龙虎榜净买</th><th>竞价涨幅/换手</th></tr>
{trs or '<tr><td colspan="14">无满足条件的股票</td></tr>'}
</table></div>
<h2>板块资金流（涨停池 + 龙虎榜聚合）</h2>
<div class="card"><table>
<tr><th>板块</th><th>涨停</th><th>连板</th><th>最高板</th><th>封板资金合计</th><th>龙虎榜净买</th><th>上榜家数</th><th>炸板</th></tr>
{secs or '<tr><td colspan="8">暂无数据</td></tr>'}
</table></div>
<h2>板块相关快讯（利好 / 利空 / 中性）</h2>
<div class="card"><ul style="list-style:none;padding:0;margin:0;font-size:12px">{news or '<li>无匹配快讯</li>'}</ul></div>
<div class="foot">风险提示：本工具仅作数据整理参考，不构成任何投资建议。股市有风险，交易需谨慎。数据来自东方财富公开接口，可能存在延迟或缺失。</div>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"HTML 报告已导出: {path}")


# ==============================================================================
# 起爆前夜（--boom）
# ==============================================================================
def boom_secid(code):
    return ("sh" if str(code)[:2] in ("60", "68", "90", "92", "51") else "sz") + str(code)


def boom_load_genes(days, base_date):
    """近 N 个交易日涨停池 → 涨停基因库（跳过非交易日，探测 N+6 自然日）。"""
    d = datetime.strptime(base_date, "%Y-%m-%d").date()
    tries = [ymd(add_days(d, -i)) for i in range(days + 7)]
    genes = {}
    got = 0
    for idx, ds in enumerate(tries):
        try:
            p = load_zt_pool(ds)
        except Exception:
            p = []
        if not p:
            continue
        got += 1
        for s in p:
            old = genes.get(s["code"])
            if not old:
                genes[s["code"]] = {"code": s["code"], "name": s["name"], "hybk": s["hybk"],
                                    "ltsz": s["ltsz"], "ztCount": 1, "maxLb": s["lbc"],
                                    "lastIdx": idx, "lastDate": ds}
            else:
                old["ztCount"] += 1
                old["maxLb"] = max(old["maxLb"], s["lbc"])
                if idx < old["lastIdx"]:
                    old["lastIdx"], old["lastDate"] = idx, ds
        if got >= days:
            break
    return genes


def boom_load_market(genes, mcap_max, workers=8):
    """全市场实时行情（clist 分页 100/页，约 60 页并发）→ 过滤。"""
    total = 6000
    pages = list(range(1, (total + 99) // 100 + 1))

    def one(p):
        j = fetch_json(URL_CLIST.format(pn=p, fs=BOOM_FS))
        return (j.get("data") or {}).get("diff") or []

    rows = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for chunk in ex.map(one, pages):
            rows.extend(chunk or [])
    cands, seen = [], set()
    for it in rows:
        if not it or not it.get("f12"):
            continue
        code = str(it["f12"])
        if code in seen:
            continue
        seen.add(code)
        g = genes.get(code)
        if not g:
            continue
        name = str(it.get("f14") or "")
        if "ST" in name.upper():
            continue
        try:
            zdp = float(it.get("f3"))
        except (TypeError, ValueError):
            continue
        if not (-3 <= zdp < 3):
            continue
        try:
            ltsz = float(it.get("f21") or 0)
        except (TypeError, ValueError):
            continue
        if not ltsz or ltsz > mcap_max * 1e8:
            continue
        try:
            price = float(it.get("f2") or 0)
            vol_ratio = float(it.get("f10") or 0)
        except (TypeError, ValueError):
            price, vol_ratio = 0, 0
        cands.append({"code": code, "name": name, "price": price, "zdp": zdp,
                      "ltsz": ltsz, "volRatio": vol_ratio, "gene": g})
    return cands


def boom_kline(code):
    """腾讯 60 日复权K线：[date, open, close, high, low, vol]。"""
    secid = boom_secid(code)
    j = fetch_json(URL_KLINE.format(secid=secid))
    d = (j.get("data") or {}).get(secid) or {}
    return d.get("qfqday") or d.get("day") or []


def boom_stats(kl):
    """K线指标：均线（5/10/20/30/60）、粘合度、筹码穿透、竞价锚点。"""
    closes = [float(k[2]) for k in kl]
    cur = closes[-1] if closes else 0

    def ma(n):
        s = closes[-n:]
        return sum(s) / len(s) if s else 0

    mas = [ma(n) for n in (5, 10, 20, 30, 60)]
    pos = [v for v in mas if v > 0]
    sticky = (max(pos) - min(pos)) / cur * 100 if pos and cur else 99
    # 筹码分布：60日量价均匀分配到 [低,高] 11 档
    bins = {}
    for k in kl:
        h, l, v = float(k[3]), float(k[4]), float(k[5])
        if not (h > 0 and l > 0 and v > 0):
            continue
        step = (h - l) / 10
        for i in range(11):
            px = round((l + step * i) * 100)
            bins[px] = bins.get(px, 0) + v / 11
    tot = sum(bins.values())
    above = sum(v for px, v in bins.items() if px > cur * 100)
    trap_pct = above / tot * 100 if tot else 50
    # 竞价异动锚点：近5日（不含当日）均量 × 8%
    vols = [float(k[5]) for k in kl[-6:-1]]
    avg_vol = sum(vols) / len(vols) if vols else 0
    anchor = avg_vol * 0.08
    return {"cur": cur, "mas": mas, "sticky": sticky, "trap_pct": trap_pct,
            "avg_vol": avg_vol, "anchor": anchor}


def boom_check_news(code):
    """负面公告过滤：返回 (bears, warns)。"""
    j = fetch_json(URL_ANN.format(code=code))
    lst = (j.get("data") or {}).get("list") or []
    bears, warns = [], []
    for it in lst[:8]:
        t = str(it.get("title") or it.get("art_title") or "")
        for kw in BOOM_BEAR_KW:
            if kw in t and kw not in bears:
                bears.append(kw)
        for kw in BOOM_WARN_KW:
            if kw in t and kw not in warns:
                warns.append(kw)
    return bears, warns


def boom_hot_top(pool):
    hot = {}
    for s in pool:
        hot[s["hybk"]] = hot.get(s["hybk"], 0) + 1
    return [k for k, _ in sorted(hot.items(), key=lambda x: -x[1])]


def boom_score(c, hot_top):
    g = c["gene"]
    zt = g["ztCount"]
    s1 = 25 if zt >= 4 else 21 if zt == 3 else 16 if zt == 2 else 10
    st = c["stats"]["sticky"]
    s2 = 20 if st < 0.8 else 16 if st < 1.2 else 12 if st < 1.6 else 8
    v = c["volRatio"]
    s3 = 20 if 2 <= v <= 3.5 else 15 if (1.6 <= v < 2) or (3.5 < v <= 4.5) else 10
    mc = c["ltsz"] / 1e8
    s4 = 15 if mc < 30 else 12 if mc < 60 else 9 if mc < 100 else 6 if mc < 200 else 3
    gap = g["lastIdx"]
    s5 = 20 if gap <= 3 else 15 if gap <= 6 else 10 if gap <= 10 else 5
    base = s1 + s2 + s3 + s4 + s5
    bonus = 0
    if hot_top:
        idx = hot_top.index(g["hybk"]) if g["hybk"] in hot_top else -1
        bonus = 20 if 0 <= idx < 3 else 10 if idx < 8 else 0
    return {"base": base, "bonus": bonus, "total": base + bonus, "parts": (s1, s2, s3, s4, s5)}


def cmd_boom(args):
    """起爆前夜全流程：基因 → 全市场过滤 → K线硬检查 → 公告过滤 → 评分输出。"""
    from concurrent.futures import ThreadPoolExecutor
    mcap_max = args.mcap
    min_score = args.score
    days = args.boom_days
    base = args.date
    print(f"══ 起爆前夜 · 基准日 {base} · 涨停基因窗口 {days} 日 ══")
    print("阶段 1/5：拉取近 %d 日涨停池构建基因库…" % days)
    genes = boom_load_genes(days, base)
    print(f"  涨停基因库 {len(genes)} 只")
    print("阶段 2/5：拉取全市场实时行情（clist 并发）…")
    cands = boom_load_market(genes, mcap_max, workers=8)
    print(f"  全市场过滤后 {len(cands)} 只（涨幅-3~3% / 非ST / 市值≤{mcap_max:.0f}亿 / 有基因）")
    print(f"阶段 3/5：逐股拉 60 日K线（线程池并发）…")
    passed = []
    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(boom_kline, c["code"]): c for c in cands}
        done = 0
        for f in futs:
            c = futs[f]
            try:
                kl = f.result()
                st = boom_stats(kl)
                c["stats"] = st
                if st["sticky"] < args.boom_sticky and 1.3 <= c["volRatio"] <= 5:
                    passed.append(c)
            except Exception:
                pass
            done += 1
            if done % 50 == 0:
                print(f"    K线分析 {done}/{len(cands)}")
    print(f"  K线硬检查通过 {len(passed)} 只（均线粘合<{args.boom_sticky:.1f}% & 量比1.3~5）")
    final = passed
    if args.neg:
        print(f"阶段 4/5：负面公告过滤 {len(passed)} 只…")
        clean = []
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(boom_check_news, c["code"]): c for c in passed}
            for f in futs:
                c = futs[f]
                try:
                    bears, warns = f.result()
                except Exception:
                    bears, warns = [], []
                c["bears"], c["warns"] = bears, warns
                if not bears:
                    clean.append(c)
        print(f"  负面公告剔除 {len(passed) - len(clean)} 只（减持/立案/预亏等硬雷）")
        final = clean
    print("阶段 5/5：五项评分 + 板块联动加权…")
    pool = load_zt_pool(base) if args.date else []
    hot_top = boom_hot_top(pool) if (not args.no_sector) and pool else None
    for c in final:
        c["score"] = boom_score(c, hot_top)
    out = [c for c in final if c["score"]["total"] >= min_score]
    out.sort(key=lambda c: -c["score"]["total"])
    w = print
    w("=" * 104)
    w(f"  起爆前夜候选 · {base} · 基因 {len(genes)} → 过滤 {len(cands)} → K线 {len(passed)} → 最终 {len(out)}（≥{min_score}分）")
    w("=" * 104)
    if not out:
        w("  无满足条件的股票，请降低评分门槛或调整参数。")
    for i, c in enumerate(out, 1):
        g, st, sc = c["gene"], c["stats"], c["score"]
        warn = ("⚠" + c["warns"][0]) if c.get("warns") else "—"
        w(f"  {i:>2}. {c['code']} {c['name']}  现价{c['price']:.2f} {fmt_pct(c['zdp'])}  "
          f"市值{c['ltsz']/1e8:.0f}亿  基因{g['ztCount']}次/{g['maxLb']}板(距{g['lastIdx']}天)  "
          f"粘合{st['sticky']:.2f}%  量比{c['volRatio']:.2f}  穿透{(100-st['trap_pct']):.0f}%  "
          f"竞价锚点>{st['anchor']/1e4:.1f}万手  [{g['hybk']}] {warn}")
        w(f"      五项 {sc['parts']} = {sc['base']} + 板块 {sc['bonus']} = {sc['total']}分  "
          f"均线 MA5-60: {', '.join(f'{v:.2f}' for v in st['mas'])}")
    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            wr = csv.writer(f)
            wr.writerow(["代码", "名称", "现价", "今日涨跌%", "流通市值亿", "涨停基因次数", "最高板",
                         "距最后涨停天", "均线粘合%", "量比", "筹码穿透%", "竞价锚点万手", "板块",
                         "公告预警", "五项分", "板块加分", "总分"])
            for c in out:
                g, st, sc = c["gene"], c["stats"], c["score"]
                wr.writerow([c["code"], c["name"], f"{c['price']:.2f}", f"{c['zdp']:.2f}",
                             f"{c['ltsz']/1e8:.1f}", g["ztCount"], g["maxLb"], g["lastIdx"],
                             f"{st['sticky']:.2f}", f"{c['volRatio']:.2f}", f"{100-st['trap_pct']:.0f}",
                             f"{st['anchor']/1e4:.1f}", g["hybk"], "/".join(c.get("warns") or []),
                             sc["base"], sc["bonus"], sc["total"]])
        print(f"CSV 已导出: {args.csv}")
    w("=" * 104)
    w("  风险提示：仅作数据整理参考，不构成投资建议。均线粘合/量比/筹码为简化估算，竞价锚点为5日均量参考。")


# ==============================================================================
# 主流程
# ==============================================================================
def main():
    ap = argparse.ArgumentParser(description="凡凡选股 · Python 版（A股短线数据筛选 + 妖股概率评分）")
    ap.add_argument("--date", default=ymd(_date.today()), help="交易日 YYYY-MM-DD（默认今天）")
    ap.add_argument("--mcap", type=float, default=300, help="流通市值上限（亿，默认 300）")
    ap.add_argument("--lb", type=int, default=0, help="最低连板数（0=含首板，1=至少二板）")
    ap.add_argument("--score", type=float, default=0, help="妖股评分最低门槛（0-100）")
    ap.add_argument("--no-st", action="store_true", help="排除 ST / *ST")
    ap.add_argument("--auction", action="store_true", help="估算模式：逐股拉分时计算 9:25 竞价涨幅（较慢）")
    ap.add_argument("--demo", action="store_true", help="演示模式：生成示例竞价数据（带演示标记）")
    ap.add_argument("--auc-up", type=float, default=5, help="竞价涨幅下限 百分比（估算/演示模式生效）")
    ap.add_argument("--auc-turn", type=float, default=2, help="竞价换手率下限 百分比（仅演示模式生效）")
    ap.add_argument("--csv", default="", help="导出 CSV 路径")
    ap.add_argument("--html", default="", help="导出 HTML 报告路径")
    ap.add_argument("--boom", action="store_true", help="起爆前夜模式：基因→全市场→K线粘合→公告过滤→评分")
    ap.add_argument("--boom-days", type=int, default=15, help="起爆前夜涨停基因窗口（日，默认 15）")
    ap.add_argument("--boom-sticky", type=float, default=2.0, help="起爆前夜均线粘合上限 百分比（默认 2.0）")
    ap.add_argument("--neg", action="store_true", help="起爆前夜负面公告过滤（默认关，建议开启）")
    ap.add_argument("--no-sector", action="store_true", help="起爆前夜关闭板块联动加权")
    args = ap.parse_args()

    if args.boom:
        cmd_boom(args)
        return

    date_str = args.date
    cap_max = args.mcap * 1e8
    print(f"正在加载 {date_str} 涨停池与龙虎榜…")
    pool = load_zt_pool(date_str)
    lhb = load_lhb(date_str)
    print(f"  涨停 {len(pool)} 家 | 龙虎榜 {len(lhb)} 条")
    if not pool:
        print(f"{date_str} 无涨停数据（可能为非交易日或已收盘未更新），请换日期。")
        return

    # 近三日涨停合并
    groups, near = near3_days(date_str, pool)
    near_codes = {}
    for p in near:
        for s in p:
            near_codes.setdefault(s["code"], s)
    print(f"  近三日合并涨停标的 {len(near_codes)} 只")

    # 龙虎榜映射
    lhb_map = {}
    for r in lhb:
        lhb_map.setdefault(str(r["code"]), r)

    # 板块聚合
    agg, _ = sector_aggregate(pool, lhb)
    sector_hot = {k: g["zt"] for k, g in agg.items()}

    # 候选：近三日涨停 ∩ 市值过滤 ∩ 最低连板 ∩ 排除ST
    cands = []
    for code, s in near_codes.items():
        if s["ltsz"] <= 0 or s["ltsz"] > cap_max:
            continue
        if s["lbc"] < args.lb:
            continue
        if args.no_st and ("ST" in (s["name"] or "").upper()):
            continue
        cands.append(s)
    print(f"  核心条件命中 {len(cands)} 只（市值≤{args.mcap:.0f}亿 / 连板≥{args.lb}）")

    # 竞价数据
    auc = {}
    if args.demo:
        auc = demo_auction(cands, cap_max)
        print("  [演示模式] 已生成示例竞价数据（仅演示用途）")
    elif args.auction:
        print("  [估算模式] 逐股拉取分时计算 9:25 竞价涨幅（可能需要 1-3 分钟）…")
        for i, s in enumerate(cands):
            try:
                tr = load_trends(s["mkt"], s["code"])
                op = extract_open(tr)
                if op:
                    pc = prev_close(s)
                    auc[s["code"]] = {"open_px": op / 100, "auc_up": round((op / 100 / pc - 1) * 100, 2),
                                      "auc_turn": None, "sell_vol": None, "buy_vol": None, "demo": False}
            except Exception:
                pass
            if (i + 1) % 20 == 0:
                print(f"    进度 {i + 1}/{len(cands)}")
        print(f"  竞价估算完成，可用 {len(auc)} 只")

    # 筛选 + 评分
    rows = []
    for s in cands:
        total, detail, lv, fr = score_stock(s, lhb_map, sector_hot)
        lb = lhb_map.get(s["code"])
        a = auc.get(s["code"])
        # 竞价涨幅过滤（估算/演示模式下）——注意 9:25 竞价涨幅需 > 阈值
        auc_up = a["auc_up"] if a else None
        if auc_up is not None and auc_up <= args.auc_up:
            continue
        auc_turn = a["auc_turn"] if a else None
        if auc_turn is not None and auc_turn <= args.auc_turn:
            continue
        if total < args.score:
            continue
        rows.append({
            "date": date_str, "code": s["code"], "name": s["name"], "price": s["price"],
            "zdp": s["zdp"], "lbc": s["lbc"], "ltsz": s["ltsz"], "hs": s["hs"],
            "fbt": s["fbt"], "fund": s["fund"], "fund_ratio": fr, "zbc": s["zbc"],
            "hybk": s["hybk"], "score": total, "level": lv,
            "lhb_net": lb["net"] if lb else 0,
            "lhb_deal_r": (lb["deal_r"] if lb else None),
            "lhb_reason": (lb["reason"] if lb else "—"),
            "auc_up": auc_up, "auc_turn": auc_turn,
            "sell_vol": a["sell_vol"] if a else None, "buy_vol": a["buy_vol"] if a else None,
            "auc_demo": (a["demo"] if a else False),
        })
    rows.sort(key=lambda r: -r["score"])

    stats = {
        "zt": len(pool),
        "lb": sum(1 for s in pool if s["lbc"] >= 2),
        "lhb": len(lhb),
        "hit": len(rows),
    }

    # 快讯
    matched_news = []
    try:
        news = load_news()
        matched_news = match_news(news, agg, {c: k for k, g in agg.items() for c in g["codes"]})
        print(f"  快讯 {len(news)} 条，匹配板块 {len(matched_news)} 条")
    except Exception as e:
        print(f"  快讯加载失败（可忽略）: {e}")

    console_report(rows, stats, agg, matched_news, date_str)
    if args.csv:
        export_csv(rows, args.csv)
    if args.html:
        export_html(rows, stats, agg, matched_news, date_str, args.html)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已中断。")
        sys.exit(1)
