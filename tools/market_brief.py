"""行情诊断 —— 把当前市场状态量化成一段人话，并给出「该做什么 / 别做什么」。

★ 重要：它**不预测涨跌**，也不构成投资建议。它做的是三件事：
  1. 把多周期趋势、波动率、区间位置、资金费率量化成客观状态；
  2. 把状态映射到「现在这种环境的赔率偏向哪边」，并明确指出**不该做的事**；
  3. 标出关键价位（网格挂单价、区间上下沿、爆仓红线），
     目的不是告诉你买卖点，而是**防止你在错误的位置做错误的动作**
     —— 比如涨到区间上沿去追多、跌到爆仓线附近还加仓。

用法：
  python tools/market_brief.py                                  # 只看 ETH 行情
  python tools/market_brief.py --coin BTC
  python tools/market_brief.py --config configs/eth-grid.yaml --margin 200
                                                                # 带上你网格的关键价位
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

from bot.candles import CandleFeed  # noqa: E402
from bot.indicators import atr, ema  # noqa: E402
from bot.utils import now_ms  # noqa: E402

API_BASE = "https://api.hyperliquid.xyz"
API_INFO = API_BASE + "/info"

# (周期, 取多少根, 权重) —— 4h 权重最高：周期越长，噪音越少
TIMEFRAMES = (("15m", 400, 0.5), ("1h", 700, 1.0), ("4h", 500, 1.5))
TF_NAME = {"15m": "短期", "1h": "中期", "4h": "长期"}

W = 72  # 输出宽度


def _post(payload: dict):
    req = urllib.request.Request(
        API_INFO,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def _bar(coin: str, interval: str, limit: int):
    try:
        return CandleFeed(API_BASE, interval=interval, limit=limit).closed(coin, limit=limit)
    except Exception:
        return []


def _trend(bars) -> tuple[float, dict]:
    """单周期趋势打分 ∈ [-2, +2]。只用已收盘 K 线。"""
    closes = [b.c for b in bars]
    if len(closes) < 205:
        return 0.0, {}
    e20, e50, e200 = ema(closes, 20), ema(closes, 50), ema(closes, 200)
    a, b, c = e20[-1], e50[-1], e200[-1]
    px = closes[-1]
    if a is None or b is None or c is None:
        return 0.0, {}
    if a > b > c and px > a:
        s, lab = 2.0, "强多头"
    elif a > b:
        s, lab = 1.0, "偏多"
    elif a < b < c and px < a:
        s, lab = -2.0, "强空头"
    elif a < b:
        s, lab = -1.0, "偏空"
    else:
        s, lab = 0.0, "走平"
    return s, {"label": lab, "ema20": a, "ema50": b, "ema200": c, "close": px}


def _vol_profile(h1, period: int = 14, days: int = 30):
    """波动率状态：当前 ATR% 在近 N 天里的分位。分位低=没肉，分位高=风险大。"""
    if len(h1) < period + 30:
        return None
    start = max(period + 2, len(h1) - days * 24)
    vals = []
    for i in range(start, len(h1) + 1, 6):  # 每 6 小时采一个点，够用且快
        sub = h1[:i]
        av = atr(sub, period)
        if av and av[-1]:
            vals.append(av[-1] / sub[-1].c * 100)
    if not vals:
        return None
    cur = vals[-1]
    rank = sum(1 for v in vals if v <= cur) / len(vals) * 100
    return {"atr_pct": cur, "rank": rank, "n": len(vals)}


def _range_pos(h1, days: int = 30):
    """当前价在近 N 天区间里的位置（0%=最低，100%=最高）。

    区间取【收盘价的 5%~95% 分位】，而不是最低 low / 最高 high ——
    单根 K 线的长影线会把区间撑大，用它切出来的"买入参考价"会明显偏低。
    """
    w = h1[-days * 24:] if len(h1) >= days * 24 else h1
    if not w:
        return None
    cs = sorted(b.c for b in w)
    n = len(cs)
    lo, hi = cs[int(n * 0.05)], cs[min(n - 1, int(n * 0.95))]
    px = h1[-1].c
    pos = (px - lo) / (hi - lo) * 100 if hi > lo else 50.0
    return {
        "hi": hi, "lo": lo, "pos": pos, "px": px, "days": days,
        "raw_lo": min(b.l for b in w), "raw_hi": max(b.h for b in w),
    }


def _funding(coin: str, days: int = 7):
    try:
        f = _post({"type": "fundingHistory", "coin": coin, "startTime": now_ms() - days * 86400_000})
    except Exception:
        return None
    if not f or len(f) < 3:
        return None
    ts = [int(x["time"]) for x in f]
    span_h = (ts[-1] - ts[0]) / 3_600_000
    if span_h <= 0:
        return None
    rates = [float(x["fundingRate"]) for x in f]
    total = sum(rates)
    return {
        "last_pct": rates[-1] * 100,
        "ann_pct": total / span_h * 24 * 365 * 100,
        "days": span_h / 24,
        "pos_ratio": sum(1 for r in rates if r > 0) / len(rates) * 100,
    }


def _line(ch: str = "-") -> str:
    return ch * W


def main() -> int:
    ap = argparse.ArgumentParser(description="行情诊断（状态量化，不是预测）")
    ap.add_argument("--coin", default="ETH")
    ap.add_argument("--config", default="", help="网格配置，用来显示层价与爆仓价")
    ap.add_argument("--margin", type=float, default=0.0, help="网格保证金，用于估算爆仓价")
    ap.add_argument("--mmr", type=float, default=1.5, help="维持保证金率 %%（默认 1.5）")
    ap.add_argument("--no-funding", action="store_true")
    args = ap.parse_args()

    coin = args.coin.upper()
    print()
    print(_line("="))
    print(f" {coin} 行情诊断 · {time.strftime('%Y-%m-%d %H:%M')}")
    print(_line("="))

    # ---------- 1) 多周期趋势 ----------
    scores: list[float] = []
    details: list[tuple[str, dict, float]] = []
    for iv, lim, wt in TIMEFRAMES:
        bars = _bar(coin, iv, lim)
        if not bars:
            print(f"  {iv:<4} 拉取失败，跳过")
            continue
        s, d = _trend(bars)
        scores.append(s * wt)
        details.append((iv, d, wt))
        if d:
            print(f"  {TF_NAME[iv]:<4}{iv:<4} EMA20 {d['ema20']:>10,.1f}   "
                  f"EMA50 {d['ema50']:>10,.1f}   EMA200 {d['ema200']:>10,.1f}   → {d['label']}")

    total = sum(scores)
    if total >= 3.0:
        t_lab, t_key = "强势多头", "strong_bull"
    elif total >= 1.5:
        t_lab, t_key = "偏多", "bull"
    elif total <= -3.0:
        t_lab, t_key = "强势空头", "strong_bear"
    elif total <= -1.5:
        t_lab, t_key = "偏空", "bear"
    else:
        t_lab, t_key = "震荡", "range"
    print()
    print(f"  ▸ 趋势判定：{t_lab}（综合分 {total:+.1f}，范围 -6 ~ +6）")

    h1 = _bar(coin, "1h", 700)
    if not h1:
        print("  1h K 线拉取失败，无法继续。稍后重试。")
        return 1
    px = h1[-1].c
    print(f"  ▸ 当前价　：{px:,.2f}")

    # ---------- 2) 波动率 ----------
    vp = _vol_profile(h1)
    if vp:
        if vp["rank"] < 30:
            v_lab, v_key = "低（没什么波动，网格会很少成交）", "low"
        elif vp["rank"] > 70:
            v_lab, v_key = "高（单根 1h 波动大，止损容易被打到）", "high"
        else:
            v_lab, v_key = "中性", "normal"
        print(f"  ▸ 波动率　：ATR(14) = {vp['atr_pct']:.2f}%  "
              f"近 30 天分位 {vp['rank']:.0f}%  → {v_lab}")
    else:
        v_key = "normal"

    # ---------- 3) 区间位置 ----------
    rp = _range_pos(h1)
    if rp:
        if rp["pos"] < 30:
            p_lab, p_key = "区间下沿", "low"
        elif rp["pos"] > 70:
            p_lab, p_key = "区间上沿", "high"
        else:
            p_lab, p_key = "区间中部", "mid"
        print(f"  ▸ 区间位置：近 30 天收盘 {rp['lo']:,.0f} ~ {rp['hi']:,.0f}"
              f"（含影线极值 {rp['raw_lo']:,.0f} ~ {rp['raw_hi']:,.0f}），"
              f"现价 {rp['pos']:.0f}% 分位 → {p_lab}")
    else:
        p_key = "mid"

    # ---------- 4) 资金费率 ----------
    fd = None if args.no_funding else _funding(coin)
    if fd:
        side = "多头累计付费" if fd["ann_pct"] > 0 else "空头累计付费"
        print(f"  ▸ 资金费率：当期 {fd['last_pct']:+.4f}%/次　"
              f"近 {fd['days']:.0f} 天累计年化 {fd['ann_pct']:+.2f}%（{side}）")

    # ---------- 5) 关键价位 ----------
    cfg = None
    if args.config:
        try:
            from bot.config import load_config
            cfg = load_config(args.config)
        except Exception as e:
            print(f"  （配置读取失败，跳过网格价位：{e}）")

    print()
    print(_line())
    print(" 操作建议")
    print(_line())

    scen = f"{t_lab} · 波动{v_lab.split('（')[0]} · {p_lab if rp else '—'}"
    print(f" 当前场景：{scen}")
    print()

    ok, warn, bad = [], [], []

    # —— 网格 ——
    if t_key == "range":
        if v_key == "low":
            ok.append("网格：环境对（震荡），但波动太小 —— 挂上多半不动，成交会很少")
        elif v_key == "high":
            ok.append("网格：适合。震荡是网格的利润来源，只是波动偏大，格距可放宽、层数可减")
        else:
            ok.append("网格：✓ 适合 —— 震荡 + 中性波动，是网格最舒服的环境")
    elif t_key in ("bull", "strong_bull"):
        warn.append("网格：能跑，但价格一路涨上去后 8 层挂单会全部落到市价下方（停摆），"
                    "记得开 reanchor 或手动重锚")
    else:
        bad.append("网格：✗ 危险 —— 做多网格在下跌趋势里会被连续打穿，"
                   "越跌越买 = 越亏越大，这时该做的是减层或停机")

    # —— 现货 / 加仓 ——
    if p_key == "low":
        if t_key in ("bull", "strong_bull", "range"):
            ok.append("加仓：✓ 位置在区间下沿，可以分批接（分 2~3 笔，别一次打满）")
        else:
            warn.append("加仓：位置够低了，但趋势向下 —— 可以小量试，等价格站稳再补")
    elif p_key == "high":
        bad.append("加仓：✗ 别追 —— 区间上沿追多的赔率最差，这里是减仓区不是买入区")
    else:
        warn.append("加仓：○ 区间中部，持有为主。不加也不减，等价格走到两端再动")

    # —— 减仓 ——
    if p_key == "high":
        ok.append("减仓：○ 价格在区间上沿，可以考虑减掉一点（比如 1/4~1/3）落袋")
    elif t_key in ("bear", "strong_bear"):
        ok.append("减仓：✓ 趋势向下，优先降杠杆/减仓，而不是扛")
    elif p_key == "low":
        bad.append("减仓：✗ 别在这里割 —— 区间下沿杀跌是最典型的亏钱动作")

    if fd and fd["ann_pct"] > 20:
        warn.append(f"持仓成本：资金费率年化 {fd['ann_pct']:+.1f}%，多头拥挤，"
                    f"做多的持有成本明显偏高")
    elif fd and fd["ann_pct"] < -5:
        warn.append(f"持仓成本：资金费率年化 {fd['ann_pct']:+.1f}%，空头拥挤，做空要付费")

    for x in ok:
        print(f"  {x}")
    for x in warn:
        print(f"  {x}")
    for x in bad:
        print(f"  {x}")

    # ---------- 6) 参考价位 ----------
    if rp:
        rng = rp["hi"] - rp["lo"]
        print()
        print(_line())
        print(" 参考价位（按近 30 天区间切分，不是预测目标）")
        print(_line())
        print(f"  卖出参考   {rp['lo'] + rng * 0.95:>10,.0f}   （区间 95% 分位）")
        print(f"             {rp['lo'] + rng * 0.85:>10,.0f}   （区间 85% 分位）")
        print(f"  现价       {rp['px']:>10,.0f}")
        print(f"  买入参考   {rp['lo'] + rng * 0.15:>10,.0f}   （区间 15% 分位）")
        print(f"             {rp['lo'] + rng * 0.05:>10,.0f}   （区间 5% 分位）")

    # ---------- 7) 你自己网格的价位 ----------
    if cfg is not None and cfg.strategy == "grid":
        try:
            import grid_plan
            szd = 4
            meta = _post({"type": "meta"})
            for u in meta.get("universe", []):
                if u.get("name") == coin:
                    szd = int(u.get("szDecimals", 4))
                    break
            anchor = cfg.grid.anchor_price or px
            rows = grid_plan.levels_of(cfg, anchor, szd)
            print()
            print(_line())
            print(f" 你的网格挂单价（以 {anchor:,.0f} 锚定）")
            print(_line())
            print("  " + "  ".join(f"{r['px']:,.0f}" for r in rows))
            print(f"  现价下方 {(1 - rows[0]['px'] / px) * 100:.2f}% ~ {(1 - rows[-1]['px'] / px) * 100:.2f}%"
                  f"　全层持仓 {sum(r['sz'] for r in rows):.4f} {coin}"
                  f"　总名义 ${sum(r['notional'] for r in rows):,.0f}")
            if args.margin > 0:
                tot_nt = sum(r["notional"] for r in rows)
                tot_sz = sum(r["sz"] for r in rows)
                avg = tot_nt / tot_sz
                mmr = args.mmr / 100.0
                liq = (avg * tot_sz - args.margin) / (tot_sz * (1 - mmr))
                print(f"  峰值保证金 ${tot_nt / cfg.market.leverage:,.0f}"
                      f"　爆仓价 ≈ {liq:,.0f}（{(liq / px - 1) * 100:+.1f}%）")
        except Exception as e:
            print(f"  （网格价位计算失败：{e}）")

    print()
    print(_line("="))
    print(" ⚠ 这不是预测。以上全部是「当前状态的客观量化」——它不能告诉你明天涨还是跌，")
    print("   只能告诉你：在这个位置、这种波动、这种趋势下，哪些动作划算、哪些明显不划算。")
    print("   真正的用处是拦住你自己在错误位置做错误动作，而不是替你决定买卖点。")
    print(_line("="))
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
