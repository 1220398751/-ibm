#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
9:25 竞价狙击台 - 数据生成器
功能：
  1. 读取候选股列表
  2. 从东方财富获取竞价数据（9:25开盘价、竞价成交量、昨日收盘价、昨日成交量）
  3. 计算竞价涨幅、竞价量比
  4. 判定红绿灯信号（绿/黄/红）
  5. 生成口语化执行指令
  6. 输出 auction_data.json

使用方法：
  # 单次生成（手动）
  python auction_generator.py

  # 循环模式（9:25-9:30每3秒生成一次）
  python auction_generator.py --loop

  # 指定候选股文件
  python auction_generator.py --candidates candidates.json

  # 指定输出文件
  python auction_generator.py --output auction_data.json

候选股配置文件格式（candidates.json）：
{
  "candidates": [
    {"code": "002547", "name": "春兴精工", "yesterday_status": "首板"},
    {"code": "300502", "name": "新易盛", "yesterday_status": "首板"}
  ]
}
"""

import json
import time
import datetime
import argparse
import sys
import os
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

# ============================================================
# 配置
# ============================================================

# 默认候选股（可通过 --candidates 参数覆盖）
DEFAULT_CANDIDATES = [
    {"code": "002547", "name": "春兴精工", "yesterday_status": "首板"},
    {"code": "300502", "name": "新易盛", "yesterday_status": "首板"},
    {"code": "600520", "name": "文一科技", "yesterday_status": "二连板"},
    {"code": "002245", "name": "蔚蓝锂芯", "yesterday_status": "首板"},
    {"code": "300672", "name": "国科微", "yesterday_status": "首板"},
]

# 信号判定阈值
SIGNAL_THRESHOLDS = {
    "green": {
        "change_min": 3.0,      # 高开≥3%
        "change_max": 7.0,      # 高开≤7%
        "volume_ratio_min": 5.0  # 竞价量比≥5%
    },
    "yellow": {
        "change_min": 8.0,      # 高开≥8%
        "volume_ratio_min": 3.0  # 量比≥3%
    },
    "red": {
        "change_max": 3.0,       # 高开<3%
        "volume_ratio_max": 3.0   # 量比<3%
    }
}

# 东方财富行情接口
EASTMONEY_QUOTE_URL = "https://push2.eastmoney.com/api/qt/ulist.np/get"


# ============================================================
# 数据获取
# ============================================================

def get_stock_codes_with_prefix(codes):
    """给股票代码添加市场前缀（0=深市，1=沪市）"""
    prefixed = []
    for code in codes:
        if code.startswith(('6', '9')):
            prefixed.append(f"1.{code}")  # 沪市
        else:
            prefixed.append(f"0.{code}")  # 深市
    return prefixed


def fetch_realtime_quotes(codes):
    """
    从东方财富获取实时行情数据
    返回字典：{code: {price, change, volume, amount, turnover, volume_ratio, name, prev_close}}
    """
    if not codes:
        return {}

    prefixed = get_stock_codes_with_prefix(codes)
    secids = ",".join(prefixed)
    fields = "f2,f3,f5,f6,f8,f10,f12,f14,f18"
    url = f"{EASTMONEY_QUOTE_URL}?fltt=2&secids={secids}&fields={fields}&_={int(time.time() * 1000)}"

    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://quote.eastmoney.com/"
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        result = {}
        items = data.get("data", {}).get("diff", [])
        for item in items:
            code = item.get("f12", "")
            result[code] = {
                "price": item.get("f2", 0) or 0,           # 最新价（9:25时为竞价价格）
                "change": item.get("f3", 0) or 0,          # 涨跌幅
                "volume": item.get("f5", 0) or 0,          # 成交量（手）
                "amount": item.get("f6", 0) or 0,          # 成交额
                "turnover": item.get("f8", 0) or 0,        # 换手率
                "volume_ratio": item.get("f10", 0) or 0,   # 量比
                "name": item.get("f14", ""),                # 名称
                "prev_close": item.get("f18", 0) or 0       # 昨收
            }
        return result
    except (URLError, HTTPError, json.JSONDecodeError, KeyError) as e:
        print(f"[ERROR] 获取行情数据失败: {e}", file=sys.stderr)
        return {}


def fetch_yesterday_volume(code, date_str):
    """
    获取昨日全天成交量（手）
    从K线接口获取昨日成交量
    """
    # 腾讯财经K线接口
    if code.startswith(('6', '9')):
        symbol = f"sh{code}"
    else:
        symbol = f"sz{code}"

    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,5,qfq"

    try:
        req = Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        # 解析K线数据
        kline_data = data.get("data", {}).get(symbol, {})
        day_data = kline_data.get("qfqday") or kline_data.get("day") or []

        if not day_data:
            return 0

        # 找到昨日的K线（最后一根是今天，倒数第二根是昨天）
        # 格式：[date, open, close, high, low, volume]
        yesterday_kline = None
        today = datetime.datetime.now().strftime("%Y-%m-%d")

        for k in reversed(day_data):
            if k[0] != today:
                yesterday_kline = k
                break

        if yesterday_kline and len(yesterday_kline) >= 6:
            return float(yesterday_kline[5])  # 成交量（手）
        return 0
    except Exception as e:
        print(f"[WARN] 获取{code}昨日成交量失败: {e}", file=sys.stderr)
        return 0


# ============================================================
# 信号判定与执行指令生成
# ============================================================

def determine_signal(auction_change, auction_volume_ratio):
    """
    判定红绿灯信号
    🟢 符合预期：高开3%-7%，且竞价量比≥5%
    🟡 风险警告：高开超过8%
    🔴 低于预期：高开不足3%，或量比不足3%
    """
    # 红色优先：双重不达标或明显弱势
    if auction_change < 3.0 or auction_volume_ratio < 3.0:
        return "red"
    # 黄色：高开过多
    if auction_change >= 8.0:
        return "yellow"
    # 绿色：符合预期
    if 3.0 <= auction_change <= 7.0 and auction_volume_ratio >= 5.0:
        return "green"
    # 临界情况（高开3-8%但量比3-5%）：黄色警告
    return "yellow"


def generate_instruction(stock, signal):
    """
    生成口语化执行指令
    """
    name = stock["name"]
    code = stock["code"]
    change = stock["auction_change"]
    volume_ratio = stock["auction_volume_ratio"]
    price = stock["auction_price"]
    prev_close = stock.get("yesterday_close", price)

    if signal == "green":
        # 绿色：符合预期，给出买入指令
        if change >= 5.0:
            strength = "强势"
            position = "2.5成"
        else:
            strength = "稳健"
            position = "2成"

        stop_price = round(price * 0.97, 2)  # 止损价（-3%）

        instruction = (
            f"<span class='action'>半路买点</span>：竞价符合预期（高开{change:.2f}%，量比{volume_ratio:.1f}%），"
            f"{strength}，资金抢筹明显。开盘后若分时不破黄线（预估{price:.2f}元附近），可买入{position}；"
            f"若秒板放弃排队，等回封机会。"
            f"<span class='danger'>止损</span>：跌破分时黄线或亏损5%无条件清仓，跌破{stop_price}元立即离场。"
        )
        return instruction

    elif signal == "yellow":
        # 黄色：风险警告，谨慎操作
        if change >= 8.0:
            instruction = (
                f"<span class='warn'>风险警告</span>：高开超过{change:.2f}%，随时可能被砸。"
                f"竞价量比{volume_ratio:.1f}%说明有资金抢筹，但高开过多诱多风险大。"
                f"<span class='action'>操作建议</span>：不追高，观察开盘后5分钟走势，"
                f"若快速封板且封单坚决可轻仓（1成）排板；若开盘冲高回落直接放弃。"
                f"<span class='danger'>止损</span>：跌破竞价价{price:.2f}元立即离场。"
            )
        else:
            # 量比不足但高开尚可
            instruction = (
                f"<span class='warn'>风险警告</span>：高开{change:.2f}%尚可，但竞价量比仅{volume_ratio:.1f}%（不足5%），"
                f"资金抢筹力度不够。"
                f"<span class='action'>操作建议</span>：谨慎参与，开盘后观察量能是否放大，"
                f"若放量上攻可轻仓（1成）试错；若量能持续萎缩直接放弃。"
                f"<span class='danger'>止损</span>：跌破{price * 0.98:.2f}元立即离场。"
            )
        return instruction

    else:
        # 红色：低于预期，放弃
        reasons = []
        if change < 3.0:
            if change < 0:
                reasons.append(f"低开{abs(change):.2f}%")
            else:
                reasons.append(f"高开仅{change:.2f}%（不足3%）")
        if volume_ratio < 3.0:
            reasons.append(f"竞价量比仅{volume_ratio:.1f}%（不足3%）")

        reason_str = "且".join(reasons) if reasons else "不达标"

        instruction = (
            f"<span class='danger'>放弃</span>：{reason_str}，资金接力意愿弱，无人抢筹。"
            f"直接从自选股删除，不要抱有幻想。"
            f"强行买入极易吃大面，等待下一个符合标准的标的。"
        )
        return instruction


# ============================================================
# 主逻辑
# ============================================================

def generate_auction_data(candidates, output_file="auction_data.json"):
    """
    生成竞价数据JSON
    """
    if not candidates:
        print("[WARN] 候选股列表为空", file=sys.stderr)
        return False

    codes = [c["code"] for c in candidates]
    today = datetime.datetime.now().strftime("%Y-%m-%d")
    now_time = datetime.datetime.now().strftime("%H:%M:%S")

    print(f"[INFO] 正在获取 {len(candidates)} 只候选股的竞价数据...")

    # 1. 获取实时行情（9:25时为竞价数据）
    quotes = fetch_realtime_quotes(codes)

    if not quotes:
        print("[ERROR] 未能获取行情数据", file=sys.stderr)
        return False

    # 2. 处理每只股票
    result_candidates = []
    for cand in candidates:
        code = cand["code"]
        name = cand.get("name", "")
        yesterday_status = cand.get("yesterday_status", "首板")

        quote = quotes.get(code)
        if not quote:
            print(f"[WARN] 未获取到 {code} {name} 的数据", file=sys.stderr)
            continue

        # 竞价数据
        auction_price = quote["price"]
        auction_change = quote["change"]
        auction_volume = quote["volume"]  # 手
        prev_close = quote["prev_close"]

        # 获取昨日成交量
        yesterday_volume = fetch_yesterday_volume(code, today)

        # 计算竞价量比（竞价成交量 / 昨日全天成交量 * 100%）
        if yesterday_volume > 0:
            auction_volume_ratio = (auction_volume / yesterday_volume) * 100
        else:
            auction_volume_ratio = 0

        # 判定信号
        signal = determine_signal(auction_change, auction_volume_ratio)

        # 构建股票数据
        stock_data = {
            "code": code,
            "name": name or quote["name"],
            "yesterday_status": yesterday_status,
            "yesterday_close": prev_close,
            "yesterday_volume": yesterday_volume,
            "auction_price": auction_price,
            "auction_change": auction_change,
            "auction_volume": auction_volume,
            "auction_volume_ratio": round(auction_volume_ratio, 1),
            "signal": signal,
            "instruction": ""
        }

        # 生成执行指令
        stock_data["instruction"] = generate_instruction(stock_data, signal)

        result_candidates.append(stock_data)

        signal_icon = {"green": "🟢", "yellow": "🟡", "red": "🔴"}[signal]
        print(f"  {signal_icon} {name}({code}) 高开{auction_change:+.2f}% 量比{auction_volume_ratio:.1f}% -> {signal}")

    # 3. 输出JSON
    output_data = {
        "date": today,
        "update_time": now_time,
        "candidates": result_candidates
    }

    try:
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(output_data, f, ensure_ascii=False, indent=2)
        print(f"[OK] 数据已写入 {output_file}（{len(result_candidates)} 只候选股，更新时间 {now_time}）")
        return True
    except IOError as e:
        print(f"[ERROR] 写入文件失败: {e}", file=sys.stderr)
        return False


def load_candidates_from_file(filepath):
    """从文件加载候选股列表"""
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("candidates", [])
    except (IOError, json.JSONDecodeError) as e:
        print(f"[ERROR] 加载候选股文件失败: {e}", file=sys.stderr)
        return []


def is_auction_window():
    """判断当前是否在竞价窗口（9:25-9:30）"""
    now = datetime.datetime.now()
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    auction_start = 9 * 3600 + 25 * 60
    auction_end = 9 * 3600 + 30 * 60
    return auction_start <= seconds < auction_end


def run_loop_mode(candidates, output_file, interval=3):
    """循环模式：竞价窗口内每interval秒生成一次"""
    print("[INFO] 循环模式启动，等待 9:25 竞价窗口...")
    print(f"[INFO] 竞价窗口内每 {interval} 秒生成一次数据")

    while True:
        if is_auction_window():
            print(f"\n[INFO] === 竞价窗口中 {datetime.datetime.now().strftime('%H:%M:%S')} ===")
            generate_auction_data(candidates, output_file)
            time.sleep(interval)
        else:
            # 非竞价窗口，每秒检查一次时间
            now = datetime.datetime.now()
            if now.hour >= 10:
                print("[INFO] 已过竞价窗口，退出循环模式")
                break
            time.sleep(1)


# ============================================================
# 入口
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="9:25 竞价狙击台 - 数据生成器")
    parser.add_argument("--candidates", "-c", help="候选股配置文件路径（JSON格式）")
    parser.add_argument("--output", "-o", default="auction_data.json", help="输出文件路径（默认: auction_data.json）")
    parser.add_argument("--loop", action="store_true", help="循环模式（9:25-9:30每3秒生成一次）")
    parser.add_argument("--interval", type=int, default=3, help="循环模式刷新间隔（秒，默认: 3）")
    args = parser.parse_args()

    # 加载候选股
    if args.candidates:
        candidates = load_candidates_from_file(args.candidates)
        if not candidates:
            print("[ERROR] 候选股列表为空，请检查配置文件", file=sys.stderr)
            sys.exit(1)
    else:
        candidates = DEFAULT_CANDIDATES
        print("[INFO] 使用默认候选股列表（可通过 --candidates 参数自定义）")

    print(f"[INFO] 候选股数量: {len(candidates)}")
    print(f"[INFO] 输出文件: {args.output}")

    # 运行模式
    if args.loop:
        run_loop_mode(candidates, args.output, args.interval)
    else:
        success = generate_auction_data(candidates, args.output)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
