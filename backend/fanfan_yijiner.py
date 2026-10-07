#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 凡凡选股 · 一进二战法（fanfan_yijiner.py）
================================================================================
  战法逻辑：昨日首板 → 今日争取二板
  五步流程：
    1. 宏观择时：情绪周期非启动/主升期，直接空仓
    2. 中观选势：只在最强的2个主线板块中选股
    3. 微观筛选：孤狼战法（剔除杂毛，精选早盘封板+封单比高）
    4. 临场狙击：次日竞价确权（换手5%+/高开3-7%）+ 板上买入
    5. 风控退出：单笔≤20%仓位，跌破均价线/亏5%止损，断板清仓

  数据来源：缓存中的涨停池 + 情绪周期计算结果
  运行方式：
    python3 fanfan_yijiner.py                  # 计算今日一进二候选
    python3 fanfan_yijiner.py --date 2026-09-12  # 指定日期
    python3 fanfan_yijiner.py --top 5           # 输出前5只候选
================================================================================
"""

import argparse
import json
from datetime import datetime, timedelta

import fanfan_cache as cache

# ==============================================================================
# 筛选参数
# ==============================================================================
CONFIG = {
    "max_mcap": 100,          # 流通市值上限（亿）
    "max_price": 20,          # 股价上限（元）
    "min_fund_ratio": 1.0,    # 封单比下限（%）
    "max_fbt": 1030,          # 封板时间上限（10:30，格式HHMM）
    "max_zbc": 0,             # 炸板次数上限（0=不允许炸板）
    "top_sectors": 2,         # 最强板块数量
    "top_candidates": 3,      # 输出候选数量
    "position_single": 0.20,  # 单票仓位上限
}


# ==============================================================================
# 工具函数
# ==============================================================================
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def format_amount(v):
    """金额格式化。"""
    if v is None:
        return "—"
    a = abs(v)
    if a >= 1e8:
        return f"{v/1e8:.2f}亿"
    if a >= 1e4:
        return f"{v/1e4:.0f}万"
    return f"{v:.0f}"


def get_previous_trading_day(date_str):
    """获取上一个交易日（简单处理：跳过周末，不考虑节假日）。"""
    d = datetime.strptime(date_str, "%Y-%m-%d")
    while True:
        d -= timedelta(days=1)
        if d.weekday() < 5:  # 周一到周五
            return d.strftime("%Y-%m-%d")


# ==============================================================================
# 第一步：宏观择时
# ==============================================================================
def check_timing(emotion_data):
    """检查情绪周期是否适合做一进二。
    返回 (can_trade, phase, reason)
    """
    if not emotion_data:
        return False, "未知", "无情绪周期数据，请先运行 fanfan_scanner.py --type emotion"

    phase = emotion_data.get("phase", "")
    phase_name = emotion_data.get("phase_name", "未知")
    score = emotion_data.get("score", 0)

    if phase in ("start", "main"):
        return True, phase_name, f"情绪周期处于{phase_name}（{score}/78分），是一进二黄金窗口"
    elif phase == "climax":
        return False, phase_name, f"情绪周期处于{phase_name}（{score}/78分），只卖不买，空仓观望"
    elif phase in ("ice", "fade"):
        return False, phase_name, f"情绪周期处于{phase_name}（{score}/78分），绝对空仓，任何形态都会失效"
    else:
        return False, phase_name, f"情绪周期处于{phase_name}，非启动/主升期，谨慎操作"


# ==============================================================================
# 第二步：中观选势（找最强主线板块）
# ==============================================================================
def find_top_sectors(zt_pool, top_n=2):
    """从涨停池中找出涨停家数最多的前N个板块。
    返回 [(sector_name, count, max_lb, stocks), ...]
    """
    sector_map = {}
    for s in zt_pool:
        sector = s.get("hybk") or "其他"
        if sector not in sector_map:
            sector_map[sector] = {"name": sector, "count": 0, "max_lb": 0, "stocks": []}
        sector_map[sector]["count"] += 1
        sector_map[sector]["max_lb"] = max(sector_map[sector]["max_lb"], s.get("lbc") or 1)
        sector_map[sector]["stocks"].append(s)

    sectors = sorted(sector_map.values(), key=lambda x: x["count"], reverse=True)
    return sectors[:top_n]


# ==============================================================================
# 第三步：微观筛选（孤狼战法）
# ==============================================================================
def filter_candidates(stocks, config=None):
    """孤狼战法：剔除杂毛，精选优质首板。
    返回 (passed, rejected)
    """
    cfg = config or CONFIG
    passed = []
    rejected = []

    for s in stocks:
        reasons = []
        code = s.get("code")
        name = s.get("name")
        price = s.get("price") or 0
        ltsz = (s.get("ltsz") or 0) / 1e8  # 转亿
        lbc = s.get("lbc") or 1
        fbt = s.get("fbt") or 0
        zbc = s.get("zbc") or 0
        fund = s.get("fund") or 0
        hs = s.get("hs") or 0

        # 绝对剔除条件
        if lbc != 1:
            reasons.append(f"非首板（{lbc}板）")
        if price > cfg["max_price"]:
            reasons.append(f"股价过高（{price:.2f}元>{cfg['max_price']}元）")
        if ltsz > cfg["max_mcap"]:
            reasons.append(f"市值过大（{ltsz:.1f}亿>{cfg['max_mcap']}亿）")
        if zbc > cfg["max_zbc"]:
            reasons.append(f"反复炸板（{zbc}次）")
        if fbt > cfg["max_fbt"] and fbt > 0:
            reasons.append(f"封板过晚（{fbt//100}:{fbt%100:02d}，晚于10:30）")

        # 封单比计算
        fund_ratio = (fund / s.get("ltsz") * 100) if s.get("ltsz") else 0
        if fund_ratio < cfg["min_fund_ratio"] and fund > 0:
            reasons.append(f"封单比不足（{fund_ratio:.2f}%<{cfg['min_fund_ratio']}%）")

        # 一字板判断（开盘价=涨停价，且换手率极低）
        if hs < 1 and fbt <= 930:
            reasons.append("疑似一字板（买不到）")

        if reasons:
            rejected.append({"stock": s, "reasons": reasons})
        else:
            # 计算精选评分
            score = calc_yijiner_score(s, fund_ratio)
            passed.append({
                "code": code,
                "name": name,
                "price": price,
                "ltsz": ltsz,
                "hs": hs,
                "fbt": fbt,
                "fbt_str": f"{fbt//100}:{fbt%100:02d}" if fbt else "—",
                "fund": fund,
                "fund_ratio": round(fund_ratio, 2),
                "zbc": zbc,
                "sector": s.get("hybk"),
                "score": score,
            })

    # 按评分降序
    passed.sort(key=lambda x: x["score"], reverse=True)
    return passed, rejected


def calc_yijiner_score(s, fund_ratio):
    """一进二候选评分（满分100）。
    1. 封板时间（30分）：9:30前=30, 10:00前=25, 10:30前=20, 其他=10
    2. 封单比（25分）：>5%=25, >3%=20, >1%=15, 其他=5
    3. 换手率（20分）：5-15%=20, 3-20%=15, 1-30%=10, 其他=5
    4. 市值（15分）：<30亿=15, <50亿=12, <80亿=8, <100亿=5
    5. 股价（10分）：<10元=10, <15元=7, <20元=4
    """
    score = 0
    fbt = s.get("fbt") or 0

    # 封板时间
    if fbt <= 930:
        score += 30
    elif fbt <= 1000:
        score += 25
    elif fbt <= 1030:
        score += 20
    else:
        score += 10

    # 封单比
    if fund_ratio > 5:
        score += 25
    elif fund_ratio > 3:
        score += 20
    elif fund_ratio > 1:
        score += 15
    else:
        score += 5

    # 换手率
    hs = s.get("hs") or 0
    if 5 <= hs <= 15:
        score += 20
    elif 3 <= hs <= 20:
        score += 15
    elif 1 <= hs <= 30:
        score += 10
    else:
        score += 5

    # 市值
    ltsz = (s.get("ltsz") or 0) / 1e8
    if ltsz < 30:
        score += 15
    elif ltsz < 50:
        score += 12
    elif ltsz < 80:
        score += 8
    else:
        score += 5

    # 股价
    price = s.get("price") or 0
    if price < 10:
        score += 10
    elif price < 15:
        score += 7
    elif price < 20:
        score += 4
    else:
        score += 0

    return score


# ==============================================================================
# 第四步：临场狙击指南（次日竞价+买入）
# ==============================================================================
def generate_trading_guide(candidates, emotion_data):
    """生成次日操作指南。"""
    if not candidates:
        return "无符合条件的候选股，今日空仓观望。"

    guide = []
    guide.append("## 🎯 次日操作指南\n")

    # 竞价研判
    guide.append("### 一、竞价研判（9:15-9:25）\n")
    guide.append("对每只候选股，重点观察以下指标：\n")
    guide.append("| 指标 | 达标条件 | 不达标处理 |")
    guide.append("|------|---------|-----------|")
    guide.append("| 竞价换手 | ≥首板全天成交量5% | 接力意愿弱，放弃 |")
    guide.append("| 高开幅度 | 3%-7%最佳 | >7%易被砸，<3%放弃 |")
    guide.append("| 盘口 | 未匹配买单>卖单 | 卖压重，放弃 |")
    guide.append("| 板块联动 | 同板块小弟高开 | 集体低开，放弃 |\n")

    # 买入动作
    guide.append("### 二、买入动作（9:30后）\n")
    guide.append("- **只打板，不半路**：放弃半路追高，只在即将封板或封板瞬间（卖一大单被吃）以涨停价打板买入")
    guide.append("- **用价格劣势换确定性**：当天可能买在最高点，但换取次日行情的确定性")
    guide.append("- **放弃原则**：开盘后分时线直接跌破均价线，或同板块小弟集体低开，无条件放弃\n")

    # 候选股清单
    guide.append("### 三、今日候选股（按优先级）\n")
    for i, c in enumerate(candidates[:3], 1):
        guide.append(f"**{i}. {c['name']}（{c['code']}）** - 评分{c['score']}分")
        guide.append(f"   - 首板封板时间：{c['fbt_str']}，封单比：{c['fund_ratio']}%")
        guide.append(f"   - 收盘价：{c['price']:.2f}元，流通市值：{c['ltsz']:.1f}亿，换手：{c['hs']:.1f}%")
        guide.append(f"   - 所属板块：{c['sector']}")
        guide.append(f"   - 次日竞价目标：换手≥{c['hs']*0.05:.1f}%（首板换手5%），高开3-7%")
        guide.append("")

    # 风控
    guide.append("### 四、风控纪律（铁律）\n")
    guide.append(f"- **仓位上限**：单笔买入不超过总仓位的{int(CONFIG['position_single']*100)}%（试错）")
    guide.append("- **炸板处理**：小单砸大概率回封；大单砸且板块走弱，次日果断离场")
    guide.append("- **止损纪律**：跌破分时均价线或亏损达5%，无条件割肉")
    guide.append("- **断板清仓**：次日若断板，无论盈亏，清仓离场，绝不抱有反包幻想\n")

    return "\n".join(guide)


# ==============================================================================
# 主计算流程
# ==============================================================================
def run_yijiner(date_str=None, top_n=3):
    """执行一进二战法全流程计算。"""
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    log(f"📊 一进二战法计算（{date_str}）")

    # 第一步：宏观择时
    log("📌 第一步：宏观择时...")
    emotion = cache.get_scan_result("emotion", date_str)
    can_trade, phase, timing_reason = check_timing(emotion)
    log(f"  情绪阶段：{phase}，可交易：{can_trade}")
    log(f"  {timing_reason}")

    if not can_trade:
        result = {
            "date": date_str,
            "can_trade": False,
            "phase": phase,
            "emotion": emotion,
            "reason": timing_reason,
            "top_sectors": [],
            "candidates": [],
            "rejected": [],
            "guide": f"当前情绪周期处于{phase}，系统执行空仓纪律。\n\n{timing_reason}",
            "config": CONFIG,
        }
        cache.set_scan_result("yijiner", date_str, result)
        log("⚠️ 非启动/主升期，候选池已隐藏")
        return result

    # 获取昨日涨停池（一进二的候选池是昨日首板）
    yesterday = get_previous_trading_day(date_str)
    log(f"📌 读取昨日（{yesterday}）涨停池...")
    yest_zt = cache.get_zt_pool(yesterday)
    log(f"  昨日涨停：{len(yest_zt)}家")

    # 第二步：中观选势
    log("📌 第二步：中观选势（找最强主线板块）...")
    top_sectors = find_top_sectors(yest_zt, CONFIG["top_sectors"])
    for i, sec in enumerate(top_sectors, 1):
        log(f"  {i}. {sec['name']} - {sec['count']}只涨停，最高{sec['max_lb']}板")

    # 只在最强板块中筛选首板
    sector_names = [s["name"] for s in top_sectors]
    sector_firstboards = []
    for sec in top_sectors:
        for s in sec["stocks"]:
            if (s.get("lbc") or 1) == 1:
                sector_firstboards.append(s)
    log(f"  最强板块中的首板：{len(sector_firstboards)}只")

    # 第三步：微观筛选
    log("📌 第三步：微观筛选（孤狼战法）...")
    passed, rejected = filter_candidates(sector_firstboards)
    log(f"  通过筛选：{len(passed)}只，被剔除：{len(rejected)}只")

    # 取前N只
    candidates = passed[:top_n]

    # 第四步：生成操作指南
    log("📌 第四步：生成操作指南...")
    guide = generate_trading_guide(candidates, emotion)

    # 组装结果
    result = {
        "date": date_str,
        "yesterday": yesterday,
        "can_trade": True,
        "phase": phase,
        "emotion": emotion,
        "reason": timing_reason,
        "top_sectors": [{"name": s["name"], "count": s["count"], "max_lb": s["max_lb"]} for s in top_sectors],
        "candidates": candidates,
        "rejected": [{"name": r["stock"].get("name"), "code": r["stock"].get("code"), "reasons": r["reasons"]} for r in rejected[:20]],
        "guide": guide,
        "config": CONFIG,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    # 写入缓存
    cache.set_scan_result("yijiner", date_str, result)
    log(f"✅ 一进二计算完成：{len(candidates)}只候选")
    return result


# ==============================================================================
# 命令行入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="凡凡选股 · 一进二战法")
    parser.add_argument("--date", type=str, default=None, help="计算日期（YYYY-MM-DD），默认今天")
    parser.add_argument("--top", type=int, default=3, help="输出候选数量，默认3")
    parser.add_argument("--output", type=str, default=None, help="输出到文件")
    args = parser.parse_args()

    print("=" * 60)
    print("  凡凡选股 · 一进二战法")
    print("=" * 60)

    result = run_yijiner(date_str=args.date, top_n=args.top)

    print("\n" + "=" * 60)
    if not result["can_trade"]:
        print(f"❌ 当前情绪：{result['phase']}")
        print(f"   {result['reason']}")
        print("   系统已隐藏候选池，执行空仓纪律")
    else:
        print(f"✅ 当前情绪：{result['phase']}，可做一进二")
        print(f"   最强板块：{', '.join(s['name'] for s in result['top_sectors'])}")
        print(f"   候选股：{len(result['candidates'])}只")
        print()
        for i, c in enumerate(result["candidates"], 1):
            print(f"  {i}. {c['name']}（{c['code']}）评分{c['score']} | {c['fbt_str']}封板 | 封单比{c['fund_ratio']}% | {c['ltsz']:.1f}亿")
        print()
        print(result["guide"])
    print("=" * 60)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False, indent=2))
        print(f"\n📝 结果已保存到：{args.output}")


if __name__ == "__main__":
    main()
