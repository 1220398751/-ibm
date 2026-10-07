#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 凡凡选股 · 盘后深度计算器（fanfan_scanner.py）
================================================================================
  核心理念：数据分层——盘中只抓轻量快照，盘后才做重型K线计算。
  本脚本从缓存读取盘中快照，针对候选池逐股拉取60日K线，计算均线粘合、
  量比、起爆前夜评分、情绪周期量化等深度指标，结果写回缓存供前端读取。

  计算内容：
    1. 起爆前夜（boom）：15日涨停基因 → 全市场过滤 → 60日K线均线粘合+量比
       → 五项评分（均线粘合/量能/位置/基因/板块）+ 负面公告过滤
    2. 情绪周期（emotion）：五维量化打分（连板20+涨停20+炸板15+溢价15+跌涨比8）
       → 五阶段判定（冰点/启动/主升/高潮/退潮）→ 主线梯队真龙辨识
    3. K线批量预取：针对近15日涨停基因库，批量拉取60日K线存入缓存

  运行方式：
    python3 fanfan_scanner.py                  # 盘后全量计算（默认）
    python3 fanfan_scanner.py --type boom      # 只算起爆前夜
    python3 fanfan_scanner.py --type emotion   # 只算情绪周期
    python3 fanfan_scanner.py --type kline     # 只批量预取K线
    python3 fanfan_scanner.py --conc 8         # 自定义并发数

  依赖：Python 3.8+ 标准库 + 同目录 fanfan_cache.py + fanfan_snapshot.py（接口常量）
================================================================================
"""

import argparse
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import fanfan_cache as cache

# ==============================================================================
# 常量与接口
# ==============================================================================
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

# 60日K线（腾讯复权日K，CORS友好）
URL_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={secid},day,,,{days},qfq"
# 个股公告（负面过滤）
URL_ANN = ("https://np-anotice-stock.eastmoney.com/api/security/ann"
           "?sr=-1&page_size=10&page_index=1&ann_type=A&client_source=web&stock_list={code}")

# 负面公告硬雷词
BEAR_KW = ["减持", "质押", "违规", "处罚", "立案", "警示", "预亏", "预减", "退市", "解禁",
           "终止", "下修", "爆雷", "诉讼", "冻结", "下调", "流拍", "失败", "低于预期",
           "问询函", "监管函", "关注函", "亏损", "逾期", "失信", "被执行", "商誉减值",
           "业绩变脸", "清仓", "减持计划", "立案调查"]

# ==============================================================================
# 基础工具
# ==============================================================================
def fetch_json(url, timeout=15):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "ignore"))


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_secid(code):
    """股票代码 → 腾讯 secid（sh600000 / sz000001）。"""
    if code.startswith("6"):
        return f"sh{code}"
    return f"sz{code}"


# ==============================================================================
# K线获取与计算
# ==============================================================================
def fetch_kline(code, days=60):
    """拉取单只股票日K线（默认60日，龙头宝典用250日），返回 list of [date, open, close, high, low, vol]。"""
    # 先查缓存（250日查询直接跳过缓存，避免混用短K线）
    if days <= 60:
        cached = cache.get_kline(code)
        if cached:
            return cached
    # 缓存未命中，拉取
    try:
        secid = get_secid(code)
        j = fetch_json(URL_KLINE.format(secid=secid, days=days))
        data = j.get("data") or {}
        # 腾讯接口可能返回 qfqday 或 day
        klines = data.get("qfqday") or data.get("day") or []
        result = []
        for k in klines:
            if len(k) >= 6:
                result.append([k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])])
        # 写入缓存
        if result and days <= 60:
            cache.upsert_kline(code, result)
        return result
    except Exception as e:
        log(f"  K线 {code} 失败: {e}")
        return []


def calc_ma(klines, period):
    """计算N日均线（返回最后一个值）。"""
    if len(klines) < period:
        return None
    closes = [k[2] for k in klines[-period:]]
    return sum(closes) / period


def calc_ma_convergence(klines):
    """计算均线粘合度：MA5/MA10/MA20/MA60 的标准差/均值（越小越粘合）。
    返回 (粘合度百分比, ma5, ma10, ma20, ma60)
    """
    ma5 = calc_ma(klines, 5)
    ma10 = calc_ma(klines, 10)
    ma20 = calc_ma(klines, 20)
    ma60 = calc_ma(klines, 60)
    mas = [m for m in [ma5, ma10, ma20, ma60] if m]
    if len(mas) < 3:
        return None, ma5, ma10, ma20, ma60
    avg = sum(mas) / len(mas)
    if avg == 0:
        return None, ma5, ma10, ma20, ma60
    variance = sum((m - avg) ** 2 for m in mas) / len(mas)
    std = variance ** 0.5
    convergence = (std / avg) * 100  # 百分比
    return round(convergence, 2), ma5, ma10, ma20, ma60


def calc_volume_ratio(klines):
    """计算量比：今日成交量 / 过去5日平均成交量。"""
    if len(klines) < 6:
        return None
    today_vol = klines[-1][5]
    avg5_vol = sum(k[5] for k in klines[-6:-1]) / 5
    if avg5_vol == 0:
        return None
    return round(today_vol / avg5_vol, 2)


def calc_sticky_penetration(klines):
    """估算筹码穿透率：上方套牢盘压力（简化版）。
    返回 0-100，值越小表示上方套牢盘越少。
    """
    if len(klines) < 20:
        return None
    current_price = klines[-1][2]
    # 统计近60日中收盘价高于当前价的天数占比（近似套牢盘）
    above = sum(1 for k in klines if k[2] > current_price)
    penetration = (above / len(klines)) * 100
    return round(penetration, 2)


# ==============================================================================
# 负面公告过滤
# ==============================================================================
def check_negative_announcement(code):
    """检查个股近期是否有负面公告。返回 (is_negative, keywords)。"""
    try:
        j = fetch_json(URL_ANN.format(code=code))
        data = j.get("data") or {}
        anns = data.get("list") or []
        hit_kw = []
        for ann in anns[:5]:  # 只看最近5条
            title = ann.get("title") or ""
            for kw in BEAR_KW:
                if kw in title:
                    hit_kw.append(kw)
                    break
        return len(hit_kw) > 0, hit_kw
    except Exception:
        return False, []


# ==============================================================================
# 起爆前夜计算
# ==============================================================================
def calc_boom_score(klines, convergence, vol_ratio, gene_days, sector_zt_count):
    """起爆前夜五项评分（满分100）。
    1. 均线粘合度（30分）：粘合<2%=30, <5%=20, <8%=10, 其他=0
    2. 量比（25分）：1.3-5.0=25, 1.0-1.3=15, 0.8-1.0=8, 其他=0
    3. 位置（20分）：近60日涨幅<30%=20, <50%=12, <80%=5, 其他=0
    4. 涨停基因（15分）：近3日有涨停=15, 近7日=10, 近15日=5, 其他=0
    5. 板块热度（10分）：板块涨停≥5=10, ≥3=7, ≥1=4, 其他=0
    """
    score = 0
    details = {}

    # 1. 均线粘合
    if convergence is not None:
        if convergence < 2:
            s = 30
        elif convergence < 5:
            s = 20
        elif convergence < 8:
            s = 10
        else:
            s = 0
        score += s
        details["convergence"] = {"value": convergence, "score": s}

    # 2. 量比
    if vol_ratio is not None:
        if 1.3 <= vol_ratio <= 5.0:
            s = 25
        elif 1.0 <= vol_ratio < 1.3:
            s = 15
        elif 0.8 <= vol_ratio < 1.0:
            s = 8
        else:
            s = 0
        score += s
        details["volume_ratio"] = {"value": vol_ratio, "score": s}

    # 3. 位置（近60日涨幅）
    if klines and len(klines) >= 2:
        gain_60 = (klines[-1][2] / klines[0][2] - 1) * 100 if klines[0][2] > 0 else 0
        if gain_60 < 30:
            s = 20
        elif gain_60 < 50:
            s = 12
        elif gain_60 < 80:
            s = 5
        else:
            s = 0
        score += s
        details["position"] = {"value": round(gain_60, 2), "score": s}

    # 4. 涨停基因
    if gene_days <= 3:
        s = 15
    elif gene_days <= 7:
        s = 10
    elif gene_days <= 15:
        s = 5
    else:
        s = 0
    score += s
    details["gene"] = {"days": gene_days, "score": s}

    # 5. 板块热度
    if sector_zt_count >= 5:
        s = 10
    elif sector_zt_count >= 3:
        s = 7
    elif sector_zt_count >= 1:
        s = 4
    else:
        s = 0
    score += s
    details["sector"] = {"zt_count": sector_zt_count, "score": s}

    return score, details


def run_boom_scan(conc=6, min_score=50, mcap_max=300):
    """执行起爆前夜全流程计算。"""
    log("=" * 50)
    log("💥 起爆前夜计算启动")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 1. 构建近15日涨停基因库
    log("📌 步骤1：构建近15日涨停基因库...")
    gene_pool = {}  # code -> {name, last_zt_days, sector}
    for i in range(15):
        d = datetime.now() - timedelta(days=i)
        if d.weekday() >= 5:
            continue  # 跳过周末
        date_str = d.strftime("%Y-%m-%d")
        zt = cache.get_zt_pool(date_str)
        for s in zt:
            code = s["code"]
            if code not in gene_pool:
                gene_pool[code] = {
                    "name": s.get("name"),
                    "last_zt_days": i,
                    "sector": s.get("hybk", "—"),
                    "ltsz": s.get("ltsz", 0),
                }
    log(f"  基因库: {len(gene_pool)} 只")

    # 2. 从快照过滤：涨幅-3%~3% / 非ST / 流通市值≤上限
    log("📌 步骤2：全市场快照过滤...")
    snapshot = cache.get_snapshot()
    snap_map = {s["code"]: s for s in snapshot}
    candidates = []
    for code, gene in gene_pool.items():
        s = snap_map.get(code)
        if not s:
            continue
        zdp = s.get("zdp") or 0
        name = s.get("name") or ""
        ltsz = (s.get("ltsz") or 0) / 1e8  # 转亿
        if not (-3 <= zdp <= 3):
            continue
        if "ST" in name or "*ST" in name or "退" in name:
            continue
        if ltsz > mcap_max:
            continue
        candidates.append({
            "code": code,
            "name": name,
            "price": s.get("price"),
            "zdp": zdp,
            "hs": s.get("hs"),
            "ltsz": ltsz,
            "sector": gene["sector"],
            "gene_days": gene["last_zt_days"],
        })
    log(f"  初筛候选: {len(candidates)} 只（涨幅-3~3% / 非ST / 市值≤{mcap_max}亿 / 有涨停基因）")

    if not candidates:
        log("⚠️ 无候选股，跳过K线分析")
        cache.set_scan_result("boom", today_str, {"candidates": [], "message": "无候选股"})
        return

    # 3. 逐股拉K线 + 计算指标（线程池并发）
    log(f"📌 步骤3：拉取K线并计算指标（并发{conc}）...")
    results = []
    lock = __import__("threading").Lock()
    done = [0]

    def analyze(stock):
        code = stock["code"]
        klines = fetch_kline(code)
        if not klines or len(klines) < 20:
            return None
        convergence, ma5, ma10, ma20, ma60 = calc_ma_convergence(klines)
        vol_ratio = calc_volume_ratio(klines)
        penetration = calc_sticky_penetration(klines)

        # 硬性检查
        if convergence is None or convergence >= 2:
            return None
        if vol_ratio is None or not (1.3 <= vol_ratio <= 5.0):
            return None

        # 板块涨停数
        sector_zt = cache.get_zt_pool(today_str)
        sector_zt_count = sum(1 for z in sector_zt if z.get("hybk") == stock["sector"])

        # 五项评分
        score, details = calc_boom_score(
            klines, convergence, vol_ratio, stock["gene_days"], sector_zt_count
        )

        # 负面公告过滤
        is_neg, neg_kw = check_negative_announcement(code)

        result = {
            **stock,
            "convergence": convergence,
            "vol_ratio": vol_ratio,
            "penetration": penetration,
            "ma5": round(ma5, 2) if ma5 else None,
            "ma10": round(ma10, 2) if ma10 else None,
            "ma20": round(ma20, 2) if ma20 else None,
            "ma60": round(ma60, 2) if ma60 else None,
            "score": score,
            "score_details": details,
            "negative": is_neg,
            "negative_kw": neg_kw,
        }
        with lock:
            done[0] += 1
            if done[0] % 10 == 0:
                log(f"  进度: {done[0]}/{len(candidates)}")
        return result

    with ThreadPoolExecutor(max_workers=conc) as ex:
        futures = [ex.submit(analyze, s) for s in candidates]
        for f in as_completed(futures):
            r = f.result()
            if r and r["score"] >= min_score:
                results.append(r)

    # 按评分降序
    results.sort(key=lambda x: x["score"], reverse=True)
    log(f"✅ 起爆前夜完成: {len(results)} 只候选（评分≥{min_score}）")

    # 写入缓存
    cache.set_scan_result("boom", today_str, {
        "candidates": results,
        "gene_pool_size": len(gene_pool),
        "initial_filter": len(candidates),
        "min_score": min_score,
        "mcap_max": mcap_max,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })
    return results


# ==============================================================================
# 情绪周期计算
# ==============================================================================
def run_emotion_scan():
    """执行情绪周期五维量化计算。"""
    log("=" * 50)
    log("🎭 情绪周期计算启动")
    today_str = datetime.now().strftime("%Y-%m-%d")

    # 读取数据
    zt_pool = cache.get_zt_pool(today_str)
    dt_pool = cache.get_dt_pool(today_str)
    snapshot = cache.get_snapshot()
    snap_map = {s["code"]: s for s in snapshot}

    # 维度1：最高连板
    max_lb = max((s.get("lbc") or 0) for s in zt_pool) if zt_pool else 0
    lb3_count = sum(1 for s in zt_pool if (s.get("lbc") or 0) >= 3)

    # 维度2：涨停家数
    zt_count = len(zt_pool)

    # 维度3：炸板率（全市场涨幅≥9.5% - 涨停数）/ 曾触涨停数
    touched_zt = sum(1 for s in snapshot if (s.get("zdp") or 0) >= 9.5)
    zbc_count = max(0, touched_zt - zt_count)
    zbc_rate = (zbc_count / touched_zt * 100) if touched_zt > 0 else None

    # 维度4：赚钱效应（昨日涨停股今日平均涨跌幅）
    yest_premium = None
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    yest_zt = cache.get_zt_pool(yesterday)
    if yest_zt:
        premiums = []
        for s in yest_zt:
            snap = snap_map.get(s["code"])
            if snap and snap.get("zdp") is not None:
                premiums.append(snap["zdp"])
        if premiums:
            yest_premium = sum(premiums) / len(premiums)

    # 维度5：跌/涨停比
    dt_count = len(dt_pool)
    dt_ratio = (dt_count / zt_count) if zt_count > 0 else None

    # 连板晋级率（今日≥2板数 / 昨日涨停数）
    promotion_rate = None
    if yest_zt:
        today_lb2 = sum(1 for s in zt_pool if (s.get("lbc") or 1) >= 2)
        promotion_rate = today_lb2 / len(yest_zt) * 100

    # 五维打分
    score = 0
    details = {}

    # 连板（20分）
    if max_lb >= 7:
        s1 = 20
    elif max_lb >= 5:
        s1 = 15
    elif max_lb >= 4:
        s1 = 10
    elif max_lb >= 3:
        s1 = 6
    else:
        s1 = 2
    score += s1
    details["max_lb"] = {"value": max_lb, "score": s1}

    # 涨停家数（20分）
    if zt_count > 80:
        s2 = 20
    elif zt_count >= 50:
        s2 = 14
    elif zt_count >= 20:
        s2 = 8
    else:
        s2 = 2
    score += s2
    details["zt_count"] = {"value": zt_count, "score": s2}

    # 炸板率（15分）
    if zbc_rate is not None:
        if zbc_rate < 20:
            s3 = 15
        elif zbc_rate < 30:
            s3 = 12
        elif zbc_rate < 40:
            s3 = 8
        elif zbc_rate < 50:
            s3 = 4
        else:
            s3 = 0
    else:
        s3 = 8
    score += s3
    details["zbc_rate"] = {"value": round(zbc_rate, 2) if zbc_rate else None, "score": s3}

    # 赚钱效应（15分）
    if yest_premium is not None:
        if yest_premium > 3:
            s4 = 15
        elif yest_premium >= 1:
            s4 = 12
        elif yest_premium >= 0:
            s4 = 8
        elif yest_premium >= -2:
            s4 = 4
        else:
            s4 = 0
    else:
        s4 = 6
    score += s4
    details["yest_premium"] = {"value": round(yest_premium, 2) if yest_premium is not None else None, "score": s4}

    # 跌/涨停比（8分）
    if dt_ratio is not None:
        if dt_ratio == 0:
            s5 = 8
        elif dt_ratio < 0.1:
            s5 = 6
        elif dt_ratio < 0.3:
            s5 = 4
        elif dt_ratio < 0.5:
            s5 = 2
        else:
            s5 = 0
    else:
        s5 = 4
    score += s5
    details["dt_ratio"] = {"value": round(dt_ratio, 2) if dt_ratio else None, "score": s5}

    # 阶段判定（退潮优先）
    fade_signals = 0
    fade_details = []
    if max_lb <= 3:
        fade_signals += 1
        fade_details.append("连板压缩至≤3板")
    if zbc_rate is not None and zbc_rate >= 50:
        fade_signals += 1
        fade_details.append("炸板率≥50%")
    if yest_premium is not None and yest_premium < 0:
        fade_signals += 1
        fade_details.append("赚钱效应转负")
    if dt_count >= 10:
        fade_signals += 1
        fade_details.append("跌停≥10家")
    if promotion_rate is not None and promotion_rate < 20:
        fade_signals += 1
        fade_details.append("晋级率<20%")

    strong_fade = (yest_premium is not None and yest_premium < -2) and (zbc_rate is not None and zbc_rate >= 40)

    if strong_fade or fade_signals >= 3:
        phase = "fade"
        phase_name = "退潮期"
    elif score >= 60:
        phase = "climax"
        phase_name = "高潮期"
    elif score >= 35:
        # 主升硬条件：连板≥5 或 晋级率≥30%
        if max_lb >= 5 or (promotion_rate is not None and promotion_rate >= 30):
            phase = "main"
            phase_name = "主升期"
        else:
            phase = "start"
            phase_name = "启动期（降级）"
    elif score >= 15:
        phase = "start"
        phase_name = "启动期"
    else:
        phase = "ice"
        phase_name = "冰点期"

    # 主线梯队（按行业聚合涨停）
    ladder = {}
    for s in zt_pool:
        sector = s.get("hybk") or "其他"
        if sector not in ladder:
            ladder[sector] = {"name": sector, "stocks": [], "max_lb": 0, "zt_count": 0}
        ladder[sector]["stocks"].append(s)
        ladder[sector]["max_lb"] = max(ladder[sector]["max_lb"], s.get("lbc") or 0)
        ladder[sector]["zt_count"] += 1
    ladder_list = sorted(ladder.values(), key=lambda x: x["zt_count"], reverse=True)[:10]

    result = {
        "phase": phase,
        "phase_name": phase_name,
        "score": score,
        "score_details": details,
        "max_lb": max_lb,
        "lb3_count": lb3_count,
        "zt_count": zt_count,
        "dt_count": dt_count,
        "zbc_rate": round(zbc_rate, 2) if zbc_rate else None,
        "yest_premium": round(yest_premium, 2) if yest_premium is not None else None,
        "dt_ratio": round(dt_ratio, 2) if dt_ratio else None,
        "promotion_rate": round(promotion_rate, 2) if promotion_rate else None,
        "fade_signals": fade_details,
        "ladder": ladder_list,
        "position": {
            "ice": "0~10%",
            "start": "20~30%",
            "main": "50~80%",
            "climax": "逐步降至0",
            "fade": "0%",
        }.get(phase, "—"),
        "action": {
            "ice": "空仓观望，仅在跌停数骤减时极轻仓试错破冰首板",
            "start": "轻仓试错，聚焦最先封板的先锋或一进二换手板",
            "main": "重仓围绕主线，做首次良性分歧低吸或弱转强确认",
            "climax": "只卖不买，分批减仓，去弱留强",
            "fade": "无条件清仓，停止一切低吸和打板",
        }.get(phase, "—"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    cache.set_scan_result("emotion", today_str, result)
    log(f"✅ 情绪周期完成: {phase_name}（{score}/78分），涨停{zt_count}/跌停{dt_count}，最高{max_lb}板")
    return result


# ==============================================================================
# K线批量预取
# ==============================================================================
def prefetch_klines(conc=8):
    """批量预取近15日涨停基因库的K线，存入缓存。"""
    log("=" * 50)
    log("📈 K线批量预取启动")

    # 构建基因库
    gene_codes = set()
    for i in range(15):
        d = datetime.now() - timedelta(days=i)
        if d.weekday() >= 5:
            continue
        zt = cache.get_zt_pool(d.strftime("%Y-%m-%d"))
        for s in zt:
            gene_codes.add(s["code"])

    log(f"  基因库: {len(gene_codes)} 只")

    # 检查哪些已有缓存
    to_fetch = []
    for code in gene_codes:
        if not cache.get_kline(code):
            to_fetch.append(code)
    log(f"  需拉取: {len(to_fetch)} 只（其余已在缓存）")

    if not to_fetch:
        log("✅ 全部已有缓存，无需拉取")
        return

    done = [0]
    lock = __import__("threading").Lock()

    def fetch_one(code):
        fetch_kline(code)
        with lock:
            done[0] += 1
            if done[0] % 20 == 0:
                log(f"  进度: {done[0]}/{len(to_fetch)}")

    with ThreadPoolExecutor(max_workers=conc) as ex:
        list(ex.map(fetch_one, to_fetch))

    log(f"✅ K线预取完成: {len(to_fetch)} 只")


# ==============================================================================
# 龙头宝典 · 盘后扫描（大庄控盘 / 命门线反转 / 凤还巢 / 妖股筹码透视 / 龙头梯队）
# ==============================================================================
def bd_kline_stats(kl):
    """完整K线统计：closes/vols/MA5/10/20/60/60均量/历史天量/涨停标记。
    返回 dict；数据不足时返回 None（容错：新股/停牌跳过）。"""
    n = len(kl)
    if n < 90:
        return None
    closes = [k[2] for k in kl]
    opens = [k[1] for k in kl]
    highs = [k[3] for k in kl]
    lows = [k[4] for k in kl]
    vols = [k[5] for k in kl]
    cur = closes[-1]

    def ma(period):
        s = closes[-period:]
        return sum(s) / len(s) if s else 0.0

    ma5, ma10, ma20, ma60 = ma(5), ma(10), ma(20), ma(60)
    ma5_prev = (sum(closes[-6:-1]) / 5) if n >= 6 else 0.0
    ma_v60 = (sum(vols[-60:]) / 60) if n >= 60 else 0.0

    # 历史天量（近250日，不含最近10日）与天量日最高价
    hist_max_vol, hist_max_high = 0.0, 0.0
    for i in range(max(0, n - 10)):
        if vols[i] > hist_max_vol:
            hist_max_vol, hist_max_high = vols[i], highs[i]
    recent10 = max(vols[-10:]) if n >= 10 else 0.0
    h250 = max(highs[-250:])
    l250 = min(lows[-250:])

    # 涨停标记（实体涨停，排除一字/T字：振幅<=1%）
    limit_ups = []
    for i in range(1, n):
        prev_close = closes[i - 1]
        if prev_close <= 0:
            continue
        pct = (closes[i] / prev_close - 1) * 100
        if pct >= 9.5 and highs[i] > lows[i]:
            is_one_word = (highs[i] - lows[i]) <= closes[i] * 0.01
            limit_ups.append({"i": i, "is_one_word": is_one_word,
                              "close": closes[i], "open": opens[i],
                              "high": highs[i], "low": lows[i], "vol": vols[i]})
    return {"n": n, "closes": closes, "opens": opens, "highs": highs, "lows": lows,
            "vols": vols, "cur": cur, "ma5": ma5, "ma5_prev": ma5_prev,
            "ma10": ma10, "ma20": ma20, "ma60": ma60, "ma_v60": ma_v60,
            "hist_max_vol": hist_max_vol, "hist_max_high": hist_max_high,
            "recent10": recent10, "h250": h250, "l250": l250, "limit_ups": limit_ups}


def bd_dazhuang(st, vol_ratio):
    """模块一：大庄控盘/越过山丘量。返回 dict(买点/保护位/卖出) 或 None。"""
    if st is None or st["n"] < 130:            # 上市不足半年跳过
        return None
    if not (st["recent10"] > st["hist_max_vol"] * 1.02):
        return None                            # 近10日量未越过历史天量
    if not (st["cur"] < st["h250"] * 0.5):
        return None                            # 非历史低位（跌幅需>50%）
    seg = st["lows"][-90:]
    pit = min(seg) if seg else 0
    if not (pit < st["h250"] * 0.62):
        return None                            # 无挖坑动作
    if not (st["cur"] > pit * 1.15):
        return None                            # 坑未收回
    if not (vol_ratio > 3):
        return None                            # 量比>3
    tian = st["hist_max_high"]
    buy_low, buy_high = tian * 0.97, tian * 1.03
    stop = buy_low * 0.94
    return {"verdict": "大庄控盘·越过山丘量",
            "buy": f"股价触及历史天量线 ±3%（{buy_low:.2f}~{buy_high:.2f}）买入",
            "stop": f"买入下沿向下6%设保护位：{stop:.2f}",
            "sell": "MACD死叉 或 5日线下穿10日线 → 趋势结束卖出",
            "extra": f"历史天量线 {tian:.2f}（最大套牢盘）· 近10日量 {st['recent10']/1e4:.0f}万"}


def bd_mingmen(st):
    """模块二：命门线反转。返回 dict 或 None。"""
    if st is None or st["n"] < 150:
        return None
    closes = st["closes"]
    # 1) 最近一次站上MA60 → 计算此前被压制天数
    last_above = -1
    for i in range(len(closes) - 1, -1, -1):
        if i < 59:
            break
        m60 = sum(closes[i - 59:i + 1]) / 60
        if closes[i] > m60:
            last_above = i
            break
    suppress = (len(closes) - 1 - last_above) if last_above >= 0 else len(closes)
    if suppress < 60:
        return None                            # 压制不足3个月
    # 2) 突破后累计≥4次非连续涨停
    after = [u for u in st["limit_ups"] if u["i"] > last_above]
    non_consec = []
    for u in after:
        if not non_consec or u["i"] - non_consec[-1]["i"] > 1:
            non_consec.append(u)
    if len(non_consec) < 4:
        return None
    # 3) 洗盘跌破60日线和平均成本线
    band_high = max(st["highs"][last_above:]) if last_above >= 0 else st["h250"]
    avg_cost = (st["ma60"] + band_high) / 2
    if not (st["cur"] < st["ma60"] and st["cur"] < avg_cost):
        return None
    vol_today = st["vols"][-1]
    back_above = st["cur"] > st["ma60"]
    vol_ok = vol_today > st["ma_v60"]
    cross_up = (st["ma5_prev"] <= st["ma60"] and st["ma5"] > st["ma60"]) or \
               (st["ma5"] > st["ma60"] and st["ma5"] > st["ma5_prev"])
    green = back_above and vol_ok and cross_up
    return {"verdict": "命门线反转 · 🟢 绿灯买点" if green else "命门线反转 · 观察（洗盘末端）",
            "buy": ("三条件齐备：站上60日线+量>60均量+5日拐头向上 → 低吸介入"
                    if green else "等待 站上60日线+量>60均量+5日上穿60日线 三条件齐备再介入"),
            "stop": f"跌破60日均线（{st['ma60']:.2f}）或平均成本线（{avg_cost:.2f}）离场",
            "sell": "5日线下穿10日线 → 趋势结束，卖出",
            "extra": f"被压制 {suppress} 天 · 突破后非连续涨停 {len(non_consec)} 次 · 平均成本线 {avg_cost:.2f}"}


def bd_fenghuang(st):
    """模块三：凤还巢（金凤凰 / 双凤还巢）。返回 dict 或 None。"""
    if st is None or st["n"] < 90:
        return None
    lu = st["limit_ups"]
    if not lu:
        return None
    last = lu[-1]
    body = st["closes"][last["i"]] - st["opens"][last["i"]]
    solid = (not last["is_one_word"]) and body > st["closes"][last["i"]] * 0.06
    if not solid:
        return None
    days_after = st["n"] - 1 - last["i"]
    # 金凤凰：涨停后放量洗盘3~5天 + 末端承接阳线
    if 3 <= days_after <= 5:
        wash = any(st["vols"][i] > last["vol"] * 0.6 for i in range(last["i"] + 1, st["n"]))
        end_yang = st["closes"][-1] > st["opens"][-1]
        if wash and end_yang and st["cur"] > last["close"]:
            return {"verdict": "金凤凰 · 凤凰抬着头买阳不买阴",
                    "buy": f"涨停板上方买阳（现价{st['cur']:.2f} > 涨停价{last['close']:.2f}），缩量洗盘只买阳线",
                    "stop": f"保护位：涨停板收盘价 {last['close']:.2f}，跌破无条件离场",
                    "sell": "次日冲高不涨停即离场；5日线下穿10日线卖出",
                    "extra": f"涨停{last['close']:.2f}后放量洗盘 {days_after} 天 · 末端出承接阳线"}
    # 双凤还巢：连续两根涨停启动 → 回调5~8天
    if len(lu) >= 2:
        b2, b1 = lu[-1], lu[-2]
        if b2["i"] - b1["i"] <= 4:
            days2 = st["n"] - 1 - b2["i"]
            if 5 <= days2 <= 8:
                b2_half = (b2["high"] + b2["low"]) / 2
                b1_half = (b1["high"] + b1["low"]) / 2
                end_yang = st["closes"][-1] > st["opens"][-1]
                if end_yang and st["cur"] <= b2_half * 1.02:
                    return {"verdict": "双凤还巢 · 买在凤凰要张嘴",
                            "buy": f"第二根涨停板下1/2区域（约{b2_half:.2f}）强力承接处买入",
                            "stop": f"保护位：第一根涨停板1/2位 {b1_half:.2f}，跌破清仓",
                            "sell": "断板即走，不参与首阴",
                            "extra": f"连续两板（{b1['close']:.2f}→{b2['close']:.2f}）后强势回调 {days2} 天"}
    return None


def bd_chip(st):
    """模块四：妖股筹码透视（量价分布近似筹码）。返回 dict 或 None。"""
    if st is None or st["n"] < 120:
        return None
    bins = {}
    seg_n = min(120, st["n"])
    for i in range(seg_n, 0, -1):
        h = st["highs"][st["n"] - i]
        l = st["lows"][st["n"] - i]
        v = st["vols"][st["n"] - i]
        if h <= 0 or l <= 0 or v <= 0:
            continue
        step = (h - l) / 20
        for j in range(21):
            px = int(round((l + step * j) * 100))
            bins[px] = bins.get(px, 0) + v / 21
    tot, above, peak_px, peak_v = 0.0, 0.0, 0, 0.0
    cur100 = int(st["cur"] * 100)
    for px, v in bins.items():
        tot += v
        if px > cur100:
            above += v
        if v > peak_v:
            peak_v, peak_px = v, px
    trap_pct = (above / tot * 100) if tot else 50.0
    peak_price = peak_px / 100
    if trap_pct >= 20:
        return None                            # 上方套牢太多
    if not (peak_price <= st["cur"] * 1.06 and peak_price >= st["cur"] * 0.6):
        return None                            # 密集峰不在低位
    return {"verdict": "筹码低位密集 · 主力吃饱",
            "buy": f"突破低位密集峰上沿（约{peak_price * 1.03:.2f}）时买入",
            "stop": f"跌破密集峰下沿（约{peak_price * 0.95:.2f}）止损",
            "sell": "放量滞涨 / 高位换手剧增 减仓",
            "extra": f"现价上方套牢筹码仅 {trap_pct:.1f}% · 密集峰中心 {peak_price:.2f}（机构重仓筹码发散标的已过滤）"}


def run_baodian_scan(conc=8):
    """龙头宝典盘后扫描：涨停池候选 → 250日K线（线程池）→ 5模块判定 → 写缓存。"""
    log("📖 龙头宝典盘后扫描启动（并发 %d）" % conc)
    # 候选池：最近涨停池 + 全市场快照（量比>2 预筛）
    today = datetime.now().strftime("%Y-%m-%d")
    pool_map = {}
    for day in range(8):
        d = (datetime.now() - timedelta(days=day)).strftime("%Y-%m-%d")
        zt = cache.get_zt_pool(d) or []
        if zt:
            today = d
            for s in zt:
                if s.get("code") and s["code"] not in pool_map:
                    pool_map[s["code"]] = {"code": s["code"], "name": s.get("name", ""),
                                           "hybk": s.get("hybk", ""), "vol_ratio": float(s.get("hs") or 0)}
            break
    snap = cache.get_snapshot()
    if snap:
        for s in snap:
            code = str(s.get("code") or "")
            if not code or code in pool_map:
                continue
            name = str(s.get("name") or "")
            if "ST" in name.upper():
                continue
            vr = float(s.get("vol_ratio") or 0)
            if vr > 2:                          # 预筛量比>2
                pool_map[code] = {"code": code, "name": name, "hybk": s.get("hybk", ""), "vol_ratio": vr}
    pool = list(pool_map.values())
    log(f"候选池 {len(pool)} 只，逐股拉取250日K线…")
    out = {"date": today, "dazhuang": [], "mingmen": [], "fenghuang": [], "chip": [], "ladder": {}}
    done = 0
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(fetch_kline, p["code"], 250): p for p in pool}
        for fut in as_completed(futs):
            p = futs[fut]
            done += 1
            if done % 50 == 0 or done == len(pool):
                log(f"  K线 {done}/{len(pool)}")
            try:
                kl = fut.result()
            except Exception:
                kl = []                          # 容错：跳过，不崩溃
            st = bd_kline_stats(kl) if kl else None
            if st is None:
                continue                         # 新股/停牌/数据不足，跳过
            r1 = bd_dazhuang(st, p["vol_ratio"])
            r2 = bd_mingmen(st)
            r3 = bd_fenghuang(st)
            r4 = bd_chip(st)
            base = {"code": p["code"], "name": p["name"], "hybk": p["hybk"]}
            if r1:
                out["dazhuang"].append({**base, **r1})
            if r2:
                out["mingmen"].append({**base, **r2})
            if r3:
                out["fenghuang"].append({**base, **r3})
            if r4:
                out["chip"].append({**base, **r4})
    # 模块六：龙头梯队（基于涨停池）
    zt = cache.get_zt_pool(today) or []
    if zt:
        by_lb = {}
        for s in zt:
            l = int(s.get("lbc") or 1)
            by_lb.setdefault(l, []).append({"code": s.get("code"), "name": s.get("name"), "hybk": s.get("hybk", "")})
        sec_map = {}
        for s in zt:
            k = s.get("hybk") or "其他"
            sec_map.setdefault(k, []).append(s)
        top_sec = max(sec_map.items(), key=lambda kv: len(kv[1])) if sec_map else ("", [])
        out["ladder"] = {"by_lb": {str(k): v for k, v in sorted(by_lb.items(), reverse=True)},
                         "max_lb": max(by_lb) if by_lb else 0,
                         "top_sec": top_sec[0], "top_sec_count": len(top_sec[1])}
    cache.set_scan_result("baodian", today, out)
    log(f"✅ 龙头宝典完成：大庄 {len(out['dazhuang'])} · 命门线 {len(out['mingmen'])} · "
        f"凤还巢 {len(out['fenghuang'])} · 筹码透视 {len(out['chip'])}")


# ==============================================================================
# 主入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="凡凡选股 · 盘后深度计算器")
    parser.add_argument("--type", choices=["all", "boom", "emotion", "kline", "baodian"], default="all",
                        help="计算类型：all=全部, boom=起爆前夜, emotion=情绪周期, kline=K线预取, baodian=龙头宝典")
    parser.add_argument("--conc", type=int, default=6, help="并发线程数，默认6")
    parser.add_argument("--min-score", type=int, default=50, help="起爆前夜最低评分，默认50")
    parser.add_argument("--mcap-max", type=int, default=300, help="流通市值上限（亿），默认300")
    args = parser.parse_args()

    log("=" * 60)
    log("🔧 凡凡选股 · 盘后深度计算器启动")
    log(f"   计算类型: {args.type} | 并发: {args.conc}")
    log("=" * 60)

    start = time.time()

    if args.type in ("all", "kline"):
        prefetch_klines(conc=args.conc)

    if args.type in ("all", "boom"):
        run_boom_scan(conc=args.conc, min_score=args.min_score, mcap_max=args.mcap_max)

    if args.type in ("all", "emotion"):
        run_emotion_scan()

    if args.type in ("all", "baodian"):
        run_baodian_scan(conc=max(4, args.conc))

    elapsed = time.time() - start
    log(f"\n🎉 全部计算完成，总耗时 {elapsed:.1f}s")


if __name__ == "__main__":
    main()
