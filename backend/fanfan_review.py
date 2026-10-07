#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 凡凡选股 · AI复盘模块（fanfan_review.py）
================================================================================
  功能：从缓存读取当日市场数据，调用AI接口生成专业复盘报告。

  复盘内容：
    1. 当日市场综述（情绪阶段、涨跌家数、连板高度、赚钱效应）
    2. 主线板块分析（最强板块、龙头标的、梯队结构）
    3. 龙虎榜资金动向（机构/游资买卖方向、重点个股）
    4. 重要事件与快讯（利好利空梳理、影响分析）
    5. 次日预期（情绪走向、关注方向、潜在风险）
    6. 操作建议（仓位建议、关注标的、纪律提醒）

  AI接口配置（环境变量）：
    FANFAN_AI_API_KEY   - API密钥（必填）
    FANFAN_AI_BASE_URL   - 接口地址（默认 https://api.openai.com/v1）
    FANFAN_AI_MODEL      - 模型名称（默认 gpt-4o-mini）
    FANFAN_AI_TIMEOUT    - 超时秒数（默认 60）

  运行方式：
    python3 fanfan_review.py                  # 生成今日复盘
    python3 fanfan_review.py --date 2026-09-12  # 指定日期
    python3 fanfan_review.py --force          # 强制重新生成（忽略缓存）
    python3 fanfan_review.py --demo           # 演示模式（不调用AI，生成模板示例）

  依赖：Python 3.8+ 标准库 + 同目录 fanfan_cache.py
       （AI调用使用 urllib，无需额外安装 openai 库）
================================================================================
"""

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta

import fanfan_cache as cache

# ==============================================================================
# AI接口配置
# ==============================================================================
AI_API_KEY = os.environ.get("FANFAN_AI_API_KEY", "")
AI_BASE_URL = os.environ.get("FANFAN_AI_BASE_URL", "https://api.openai.com/v1")
AI_MODEL = os.environ.get("FANFAN_AI_MODEL", "gpt-4o-mini")
AI_TIMEOUT = int(os.environ.get("FANFAN_AI_TIMEOUT", "60"))

# ==============================================================================
# 系统提示词（专业复盘分析师角色）
# ==============================================================================
SYSTEM_PROMPT = """你是一位资深A股短线交易复盘分析师，拥有10年以上超短交易经验。
你的复盘风格：客观、精准、数据驱动、不吹不黑、直击核心。

复盘要求：
1. 所有分析必须基于提供的数据，不要编造不存在的个股或数据
2. 情绪判断要结合连板高度、涨停家数、炸板率、赚钱效应综合判断
3. 主线板块分析要指出龙头、梯队完整性、持续性
4. 龙虎榜分析要区分机构和游资动向，指出重点个股
5. 次日预期要给出明确的情绪走向判断（回暖/延续/分歧/退潮）
6. 操作建议要具体，包括仓位、关注方向、止损纪律
7. 风险提示要诚实，不回避潜在风险

输出格式：使用Markdown，结构清晰，重点内容加粗。
字数控制在800-1200字，不要过长。"""

# ==============================================================================
# 数据汇总
# ==============================================================================
def collect_market_data(date_str):
    """从缓存收集当日市场数据，构建复盘上下文。"""
    result = {
        "date": date_str,
        "emotion": None,
        "zt_pool": [],
        "dt_pool": [],
        "lhb": [],
        "news": [],
        "snapshot_count": 0,
    }

    # 情绪周期计算结果
    emotion = cache.get_scan_result("emotion", date_str)
    if emotion:
        result["emotion"] = {
            "phase": emotion.get("phase_name"),
            "score": emotion.get("score"),
            "max_lb": emotion.get("max_lb"),
            "zt_count": emotion.get("zt_count"),
            "dt_count": emotion.get("dt_count"),
            "zbc_rate": emotion.get("zbc_rate"),
            "yest_premium": emotion.get("yest_premium"),
            "promotion_rate": emotion.get("promotion_rate"),
            "position": emotion.get("position"),
            "action": emotion.get("action"),
            "fade_signals": emotion.get("fade_signals", []),
            "ladder": emotion.get("ladder", [])[:5],
        }

    # 涨停池
    zt = cache.get_zt_pool(date_str)
    result["zt_pool"] = [{
        "code": s.get("code"),
        "name": s.get("name"),
        "price": s.get("price"),
        "zdp": s.get("zdp"),
        "lbc": s.get("lbc"),
        "fbt": s.get("fbt"),
        "fund": s.get("fund"),
        "zbc": s.get("zbc"),
        "hs": s.get("hs"),
        "ltsz": s.get("ltsz"),
        "hybk": s.get("hybk"),
    } for s in zt[:50]]  # 最多50只，避免prompt过长

    # 跌停池
    dt = cache.get_dt_pool(date_str)
    result["dt_pool"] = [{
        "code": s.get("code"),
        "name": s.get("name"),
        "zdp": s.get("zdp"),
        "fund": s.get("fund"),
    } for s in dt[:20]]

    # 龙虎榜（按净买额排序，取前20）
    lhb = cache.get_lhb(date_str)
    lhb_sorted = sorted(lhb, key=lambda x: x.get("net") or 0, reverse=True)
    result["lhb"] = [{
        "code": s.get("code"),
        "name": s.get("name"),
        "change": s.get("change"),
        "buy": s.get("buy"),
        "sell": s.get("sell"),
        "net": s.get("net"),
        "reason": s.get("reason"),
    } for s in lhb_sorted[:20]]

    # 快讯（取最新30条，标注利好利空）
    news = cache.get_news(limit=30)
    result["news"] = [{
        "title": n.get("title"),
        "show_time": n.get("show_time"),
        "senti": n.get("_senti", 0),
        "stocks": [s.get("name") if isinstance(s, dict) else s for s in (n.get("stockList") or [])][:3],
    } for n in news]

    # 快照统计
    stats = cache.get_stats()
    result["snapshot_count"] = stats.get("snapshot_count", 0)

    return result


# ==============================================================================
# Prompt构建
# ==============================================================================
def build_review_prompt(data):
    """根据市场数据构建复盘prompt。"""
    date_str = data["date"]
    emotion = data.get("emotion")

    prompt = f"""请对 {date_str} A股市场进行专业复盘。

## 一、市场情绪数据
"""

    if emotion:
        prompt += f"""
- 情绪阶段：{emotion.get('phase', '未知')}（量化得分 {emotion.get('score', '—')}/78）
- 最高连板：{emotion.get('max_lb', '—')}板
- 涨停家数：{emotion.get('zt_count', '—')}家
- 跌停家数：{emotion.get('dt_count', '—')}家
- 炸板率：{emotion.get('zbc_rate', '—')}%
- 昨日涨停今日溢价：{emotion.get('yest_premium', '—')}%
- 连板晋级率：{emotion.get('promotion_rate', '—')}%
- 建议仓位：{emotion.get('position', '—')}
- 操作方向：{emotion.get('action', '—')}
"""
        if emotion.get("fade_signals"):
            prompt += f"- ⚠️ 退潮信号：{', '.join(emotion['fade_signals'])}\n"
    else:
        prompt += "- （暂无情绪周期计算结果，请先运行 fanfan_scanner.py --type emotion）\n"

    # 主线板块
    prompt += "\n## 二、主线板块梯队\n"
    if emotion and emotion.get("ladder"):
        for i, sector in enumerate(emotion["ladder"][:5], 1):
            stocks = sector.get("stocks", [])
            dragon = next((s for s in stocks if (s.get("lbc") or 1) == sector.get("max_lb")), None)
            prompt += f"\n### {i}. {sector.get('name', '未知')}（{sector.get('count', 0)}只涨停，最高{sector.get('max_lb', 0)}板）\n"
            if dragon:
                prompt += f"- 龙头：{dragon.get('name')}（{dragon.get('code')}，{sector.get('max_lb')}板，封单{format_amount(dragon.get('fund'))}）\n"
            mid = [s for s in stocks if 2 <= (s.get("lbc") or 1) < sector.get("max_lb", 0)]
            if mid:
                prompt += f"- 中位：{', '.join(s.get('name') for s in mid[:5])}\n"
            low = [s for s in stocks if (s.get("lbc") or 1) == 1]
            if low:
                prompt += f"- 首板：{', '.join(s.get('name') for s in low[:8])}\n"
    else:
        # 从涨停池按行业聚合
        sector_map = {}
        for s in data.get("zt_pool", []):
            k = s.get("hybk") or "其他"
            if k not in sector_map:
                sector_map[k] = {"count": 0, "max_lb": 0, "stocks": []}
            sector_map[k]["count"] += 1
            sector_map[k]["max_lb"] = max(sector_map[k]["max_lb"], s.get("lbc") or 1)
            sector_map[k]["stocks"].append(s)
        sectors = sorted(sector_map.values(), key=lambda x: x["count"], reverse=True)[:5]
        for i, sector in enumerate(sectors, 1):
            prompt += f"\n### {i}. {sector['stocks'][0].get('hybk', '未知')}（{sector['count']}只涨停，最高{sector['max_lb']}板）\n"
            top = sorted(sector["stocks"], key=lambda x: x.get("lbc") or 1, reverse=True)[:5]
            prompt += "- 代表：" + ", ".join(f"{s.get('name')}({s.get('lbc') or 1}板)" for s in top) + "\n"

    # 连板高度榜
    prompt += "\n## 三、连板高度榜（前10）\n"
    zt_sorted = sorted(data.get("zt_pool", []), key=lambda x: x.get("lbc") or 1, reverse=True)[:10]
    for i, s in enumerate(zt_sorted, 1):
        prompt += f"{i}. {s.get('name')}（{s.get('code')}）- {s.get('lbc') or 1}板，封单{format_amount(s.get('fund'))}，换手{s.get('hs', 0):.1f}%，行业{s.get('hybk')}\n"

    # 龙虎榜
    prompt += "\n## 四、龙虎榜资金动向（净买额前10）\n"
    if data.get("lhb"):
        for i, s in enumerate(data["lhb"][:10], 1):
            direction = "净买入" if (s.get("net") or 0) >= 0 else "净卖出"
            prompt += f"{i}. {s.get('name')}（{s.get('code')}）- {direction}{format_amount(abs(s.get('net') or 0))}，涨跌幅{s.get('change', 0):.2f}%，原因：{s.get('reason')}\n"
    else:
        prompt += "- （暂无龙虎榜数据，盘后约17:00披露）\n"

    # 跌停
    if data.get("dt_pool"):
        prompt += f"\n## 五、跌停股（共{len(data['dt_pool'])}只）\n"
        for s in data["dt_pool"][:10]:
            prompt += f"- {s.get('name')}（{s.get('code')}），封单{format_amount(s.get('fund'))}\n"

    # 快讯
    prompt += "\n## 六、重要快讯（利好利空标注）\n"
    bull_news = [n for n in data.get("news", []) if n.get("senti") == 1][:5]
    bear_news = [n for n in data.get("news", []) if n.get("senti") == -1][:5]
    if bull_news:
        prompt += "\n利好：\n"
        for n in bull_news:
            prompt += f"- {n.get('title')}"
            if n.get("stocks"):
                prompt += f"（相关：{', '.join(n['stocks'])}）"
            prompt += "\n"
    if bear_news:
        prompt += "\n利空：\n"
        for n in bear_news:
            prompt += f"- {n.get('title')}"
            if n.get("stocks"):
                prompt += f"（相关：{', '.join(n['stocks'])}）"
            prompt += "\n"
    if not bull_news and not bear_news:
        prompt += "- （暂无明显利好利空快讯）\n"

    # 复盘要求
    prompt += """
## 复盘输出要求

请按以下结构输出复盘报告：

### 📊 一、当日市场综述
（情绪阶段、整体表现、赚钱效应、核心特征，200字以内）

### 🔥 二、主线板块分析
（最强板块是谁？龙头是谁？梯队是否完整？持续性如何？）

### 💰 三、龙虎榜资金动向
（机构和游资在买什么卖什么？重点个股分析）

### 🔮 四、次日预期
（情绪会回暖还是退潮？关注哪些方向？潜在风险是什么？）

### 🎯 五、操作建议
（仓位建议、关注标的、买入时机、止损纪律）

### ⚠️ 六、风险提示
（需要警惕的风险点）

记住：所有分析必须基于上面提供的数据，不要编造不存在的个股。
如果数据不足，请明确说明，不要强行分析。"""

    return prompt


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


# ==============================================================================
# AI接口调用（OpenAI兼容格式）
# ==============================================================================
def call_ai(system_prompt, user_prompt):
    """调用AI接口（OpenAI兼容格式），返回生成的文本。"""
    if not AI_API_KEY:
        raise RuntimeError(
            "未配置AI接口API密钥。\n"
            "请设置环境变量：\n"
            "  export FANFAN_AI_API_KEY='你的API密钥'\n"
            "  export FANFAN_AI_BASE_URL='https://api.openai.com/v1'（可选）\n"
            "  export FANFAN_AI_MODEL='gpt-4o-mini'（可选）\n"
            "或使用 --demo 演示模式。"
        )

    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    payload = {
        "model": AI_MODEL,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.7,
        "max_tokens": 2000,
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {AI_API_KEY}",
        },
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as r:
        result = json.loads(r.read().decode("utf-8"))

    return result["choices"][0]["message"]["content"]


# ==============================================================================
# 演示模式（不调用AI，生成模板示例）
# ==============================================================================
def generate_demo_review(data):
    """演示模式：基于数据生成模板化复盘（不调用AI）。"""
    date_str = data["date"]
    emotion = data.get("emotion")
    phase = emotion.get("phase", "未知") if emotion else "未知"
    zt_count = len(data.get("zt_pool", []))
    dt_count = len(data.get("dt_pool", []))
    max_lb = max((s.get("lbc") or 1) for s in data.get("zt_pool", [])) if data.get("zt_pool") else 0

    # 主线板块
    sector_map = {}
    for s in data.get("zt_pool", []):
        k = s.get("hybk") or "其他"
        sector_map[k] = sector_map.get(k, 0) + 1
    top_sectors = sorted(sector_map.items(), key=lambda x: x[1], reverse=True)[:3]

    review = f"""# {date_str} A股复盘报告（演示模式）

> ⚠️ 本报告为演示模式生成，未调用AI接口。配置API密钥后可获得AI深度分析。

## 📊 一、当日市场综述

{date_str}，A股市场情绪处于**{phase}**阶段。
全天共{zt_count}只个股涨停，{dt_count}只个股跌停，最高连板高度为**{max_lb}板**。

"""

    if emotion:
        review += f"""量化得分：{emotion.get('score', '—')}/78分
炸板率：{emotion.get('zbc_rate', '—')}%
昨日涨停今日溢价：{emotion.get('yest_premium', '—')}%
建议仓位：{emotion.get('position', '—')}

"""

    review += """## 🔥 二、主线板块分析

今日主线板块：

"""
    for i, (name, count) in enumerate(top_sectors, 1):
        review += f"{i}. **{name}** - {count}只涨停\n"

    review += """
（演示模式仅展示板块统计，AI模式将提供深度梯队分析和持续性判断）

## 💰 三、龙虎榜资金动向

"""
    if data.get("lhb"):
        top_buy = [s for s in data["lhb"] if (s.get("net") or 0) > 0][:3]
        top_sell = [s for s in data["lhb"] if (s.get("net") or 0) < 0][:3]
        if top_buy:
            review += "**净买入前三：**\n"
            for s in top_buy:
                review += f"- {s.get('name')}：净买入{format_amount(s.get('net'))}\n"
            review += "\n"
        if top_sell:
            review += "**净卖出前三：**\n"
            for s in top_sell:
                review += f"- {s.get('name')}：净卖出{format_amount(abs(s.get('net')))}\n"
    else:
        review += "（暂无龙虎榜数据，盘后约17:00披露）\n"

    review += """
## 🔮 四、次日预期

（演示模式不提供AI预测，配置API密钥后可获得情绪走向判断和关注方向）

## 🎯 五、操作建议

（演示模式不提供AI建议，配置API密钥后可获得仓位建议和关注标的）

## ⚠️ 六、风险提示

1. 本报告基于公开数据整理，不构成任何投资建议
2. 股市有风险，交易需谨慎
3. 演示模式内容仅供参考，AI模式将提供更深度的分析

---
*生成时间：{time} | 数据来源：东方财富公开接口*
""".format(time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    return review


# ==============================================================================
# 复盘生成主函数
# ==============================================================================
def generate_review(date_str=None, force=False, demo=False):
    """生成复盘报告。

    Args:
        date_str: 日期，默认今天
        force: 是否强制重新生成（忽略缓存）
        demo: 是否使用演示模式

    Returns:
        dict: {date, content, generated_at, mode, cached}
    """
    if not date_str:
        date_str = datetime.now().strftime("%Y-%m-%d")

    # 检查缓存
    if not force:
        cached = cache.get_scan_result("review", date_str)
        if cached and cached.get("content"):
            print(f"📋 使用缓存的复盘报告（{date_str}）")
            return {
                "date": date_str,
                "content": cached["content"],
                "generated_at": cached.get("generated_at", ""),
                "mode": cached.get("mode", "ai"),
                "cached": True,
            }

    print(f"🔍 收集 {date_str} 市场数据...")
    data = collect_market_data(date_str)

    if not data.get("zt_pool") and not data.get("emotion"):
        print("⚠️ 缓存中无当日数据，请先运行 fanfan_snapshot.py 抓取数据")
        # 仍然生成一个空数据复盘
        data["date"] = date_str

    if demo:
        print("🎭 演示模式：生成模板化复盘...")
        content = generate_demo_review(data)
        mode = "demo"
    else:
        print(f"🤖 调用AI接口生成复盘（模型：{AI_MODEL}）...")
        prompt = build_review_prompt(data)
        try:
            content = call_ai(SYSTEM_PROMPT, prompt)
            mode = "ai"
        except Exception as e:
            print(f"❌ AI调用失败：{e}")
            print("🔄 回退到演示模式...")
            content = generate_demo_review(data)
            mode = "demo-fallback"

    # 缓存结果
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cache.set_scan_result("review", date_str, {
        "content": content,
        "generated_at": generated_at,
        "mode": mode,
    })

    print(f"✅ 复盘报告生成完成（{date_str}，模式：{mode}）")
    return {
        "date": date_str,
        "content": content,
        "generated_at": generated_at,
        "mode": mode,
        "cached": False,
    }


# ==============================================================================
# 命令行入口
# ==============================================================================
def main():
    parser = argparse.ArgumentParser(description="凡凡选股 · AI复盘生成器")
    parser.add_argument("--date", type=str, default=None, help="复盘日期（YYYY-MM-DD），默认今天")
    parser.add_argument("--force", action="store_true", help="强制重新生成（忽略缓存）")
    parser.add_argument("--demo", action="store_true", help="演示模式（不调用AI）")
    parser.add_argument("--output", type=str, default=None, help="输出到文件（.md）")
    args = parser.parse_args()

    print("=" * 60)
    print("  凡凡选股 · AI复盘生成器")
    print("=" * 60)

    result = generate_review(
        date_str=args.date,
        force=args.force,
        demo=args.demo,
    )

    print("\n" + "=" * 60)
    print(result["content"])
    print("=" * 60)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(f"# {result['date']} A股复盘报告\n\n")
            f.write(f"> 生成时间：{result['generated_at']} | 模式：{result['mode']}\n\n")
            f.write(result["content"])
        print(f"\n📝 复盘报告已保存到：{args.output}")


if __name__ == "__main__":
    main()
