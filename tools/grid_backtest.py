"""网格 + 马丁策略回测。

**为什么要有这个**
------------------
EMA 趋势策略有 `tools/backtest.py`，但网格一直没有回测工具 ——
而网格恰好是唯一在费率结构上健康的策略（step 0.8% vs 往返费 0.03%，
占比不到 4%）。不把它算清楚，就没法回答「该做哪个方向」。

**网格和趋势的收益来源完全不同**
--------------------------------
趋势策略赌「方向判对」，网格赌「价格在区间里来回震荡」。
所以网格回测要看的不是胜率，而是三件事：
  * 完成了多少轮（每轮 = 一次买 + 一次止盈）
  * 最大浮亏有多大（价格单边跑出区间时，仓位会越积越深）
  * 峰值占用多少名义（决定需要多少保证金）

**怎么做才诚实**
----------------
* 只用已收盘 K 线，不碰正在形成的那根；
* 买入按「K 线最低价触及层价」判定，成交价就按**层价**算（不占滑点便宜）；
* 止盈按「K 线最高价触及止盈价」判定，成交价按**止盈价**算；
* **同一根 K 线内刚买入的仓位，不允许在同一根里止盈** ——
  否则会出现「用最低价买、用最高价卖」的双重获利，回测会虚高；
* 每笔成交都按 maker 计费（限价单成交），默认 0.015%/边；
* 名义占用超过 `max_total_notional_usd` 就拒绝成交（复现真实风控）；
* 输出同时给「前半段 / 后半段」，只在前半段赚钱的参数一律不采信。

用法
----
    python tools/grid_backtest.py                          # 15m / 30 天 / 默认参数
    python tools/grid_backtest.py --interval 1h --days 60
    python tools/grid_backtest.py --step 0.5 --levels 12
    python tools/grid_backtest.py --no-martingale          # 关掉马丁对比
    python tools/grid_backtest.py --sweep                  # 扫 step × levels
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.candles import Candle, CandleFeed, max_history_days  # noqa: E402


@dataclass
class GridParams:
    coin: str = "ETH"
    interval: str = "15m"
    days: int = 30

    step_pct: float = 0.8        # 层间距（%）
    levels: int = 8              # 向下铺几层
    tp_pct: float = 0.8          # 每层止盈幅度（%）
    base_usd: float = 25.0       # 第 1 层名义（USD）
    mult: float = 1.4            # 马丁倍数
    max_mult: float = 8.0        # 单笔放大上限（倍数）
    max_notional: float = 500.0  # 名义占用上限（USD）

    fee_bps: float = 1.5         # 单边费率（基点），maker 1.5 / taker 4.5
    anchor_mode: str = "fixed"   # fixed=起点锚定不动 | rolling=偏离超阈值就重锚
    recenter_pct: float = 6.0    # 重锚阈值（%）
    leverage: int = 5            # 用来把名义占用换算成需要的保证金


@dataclass
class Layer:
    """一层挂单。成交后变成一笔持仓，同时挂出止盈价。"""
    px: float          # 挂单价（= 成交价）
    usd: float         # 名义
    filled_at: int = -1
    tp_px: float = 0.0


@dataclass
class GridResult:
    rounds: int = 0
    gross: float = 0.0
    fees: float = 0.0
    net: float = 0.0
    max_unrealized: float = 0.0
    peak_notional: float = 0.0
    final_qty: float = 0.0
    final_unrealized: float = 0.0
    recenters: int = 0
    blocked: int = 0            # 被名义上限拦掉的成交次数
    bars: int = 0
    first_px: float = 0.0
    last_px: float = 0.0
    first_t: int = 0
    last_t: int = 0

    @property
    def hold_pct(self) -> float:
        if not self.first_px:
            return 0.0
        return (self.last_px - self.first_px) / self.first_px * 100.0

    @property
    def margin_needed(self) -> float:
        return self.peak_notional / 5.0

    @property
    def span_days(self) -> float:
        return (self.last_t - self.first_t) / 86_400_000.0 if self.last_t else 0.0


def load_bars(p: GridParams, verbose: bool = True) -> List[Candle]:
    from bot.candles import interval_ms

    want = min(5000, int(p.days * 86_400_000 / interval_ms(p.interval)) + 5)
    feed = CandleFeed("https://api.hyperliquid.xyz", interval=p.interval, limit=want, cache_sec=0.0)
    bars = feed.closed(p.coin, limit=want, force=True)
    if verbose and bars:
        span = (bars[-1].t - bars[0].t) / 86_400_000
        print(
            f"K 线：{p.coin} {p.interval}  {len(bars)} 根已收盘  跨度 {span:.1f} 天"
            f"（该周期上限约 {max_history_days(p.interval):.1f} 天）"
        )
        print(
            f"     {time.strftime('%Y-%m-%d %H:%M', time.localtime(bars[0].t / 1000))}"
            f"  →  {time.strftime('%Y-%m-%d %H:%M', time.localtime(bars[-1].t / 1000))}"
        )
    return bars


def _build_layers(anchor: float, p: GridParams) -> List[Layer]:
    """以 anchor 为顶，向下铺 levels 层。第 1 层最靠近锚点、名义最小。"""
    out: List[Layer] = []
    for i in range(1, p.levels + 1):
        px = anchor * (1.0 - i * p.step_pct / 100.0)
        mult = min(p.mult ** (i - 1), p.max_mult)
        out.append(Layer(px=px, usd=p.base_usd * mult))
    return out


def simulate(bars: List[Candle], p: GridParams) -> GridResult:
    res = GridResult(bars=len(bars))
    if not bars:
        return res

    res.first_px, res.last_px = bars[0].c, bars[-1].c
    res.first_t, res.last_t = bars[0].t, bars[-1].t

    fee = p.fee_bps / 10_000.0
    anchor = bars[0].c
    pending = _build_layers(anchor, p)
    held: List[Layer] = []
    used = 0.0  # 当前持仓成本名义

    for idx, b in enumerate(bars):
        # ---- 1) 止盈：只处理「上一根及更早」成交的仓位（避免同根 K 线低买高卖）
        keep: List[Layer] = []
        for L in held:
            if idx > L.filled_at and b.h >= L.tp_px:
                qty = L.usd / L.px
                gross = qty * (L.tp_px - L.px)
                cost = L.usd * fee + qty * L.tp_px * fee
                res.rounds += 1
                res.gross += gross
                res.fees += cost
                res.net += gross - cost
                used -= L.usd
            else:
                keep.append(L)
        held = keep

        # ---- 2) 买入：K 线最低价触及层价才成交，成交价按层价算
        still: List[Layer] = []
        for L in pending:
            if b.l <= L.px:
                if used + L.usd > p.max_notional + 1e-9:
                    res.blocked += 1
                    still.append(L)  # 风控拦住，订单继续挂着
                    continue
                L.filled_at = idx
                L.tp_px = L.px * (1.0 + p.tp_pct / 100.0)
                held.append(L)
                used += L.usd
            else:
                still.append(L)
        pending = still

        # ---- 3) 记录风险指标
        notional = sum(L.usd for L in held)
        if notional > res.peak_notional:
            res.peak_notional = notional
        unreal = sum((b.c - L.px) * (L.usd / L.px) for L in held)
        if -unreal > res.max_unrealized:
            res.max_unrealized = -unreal

        # ---- 4) 重锚：价格跑出区间后以新价为中心重铺（已成交的仓位不动）
        if p.anchor_mode == "rolling":
            if abs(b.c - anchor) / anchor * 100.0 >= p.recenter_pct:
                anchor = b.c
                pending = _build_layers(anchor, p)
                res.recenters += 1

    last = bars[-1].c
    res.final_qty = sum(L.usd / L.px for L in held)
    res.final_unrealized = sum((last - L.px) * (L.usd / L.px) for L in held)
    return res


def report(res: GridResult, p: GridParams, title: str = "") -> str:
    lines = []
    head = title or f"{p.coin} {p.interval} · step {p.step_pct}% × {p.levels} 层 · 止盈 {p.tp_pct}%"
    lines.append("=" * 74)
    lines.append(head)
    lines.append("=" * 74)
    lines.append(
        f"行情：{res.span_days:.1f} 天  {res.first_px:,.2f} → {res.last_px:,.2f}"
        f"（{'涨' if res.hold_pct >= 0 else '跌'} {abs(res.hold_pct):.2f}%）"
    )
    lines.append(
        f"马丁：{p.mult}x（上限 {p.max_mult}x）   名义上限 ${p.max_notional:,.0f}"
        f"   锚定：{'固定' if p.anchor_mode == 'fixed' else f'滚动(偏离{p.recenter_pct}%重锚)'}"
    )
    lines.append("-" * 74)
    lines.append(f"完成轮数        {res.rounds} 次（买 + 止盈 = 1 轮）")
    lines.append(f"毛收益          {res.gross:+.4f} USDC")
    lines.append(f"手续费          -{res.fees:.4f} USDC")
    lines.append(f"已实现净收益    {res.net:+.4f} USDC")
    if res.final_qty:
        lines.append(
            f"期末持仓        {res.final_qty:.4f} 币（成本名义 ${res.peak_notional - 0:,.0f} 级别）"
        )
        lines.append(f"期末浮盈亏      {res.final_unrealized:+.4f} USDC（按最后收盘价）")
        lines.append(f"合计            {res.net + res.final_unrealized:+.4f} USDC  ← 含浮亏的真实账")
    lines.append("-" * 74)
    lines.append(f"峰值名义占用    ${res.peak_notional:,.2f}   （{p.leverage}x 下需保证金 ${res.peak_notional/p.leverage:,.2f}）")
    lines.append(f"最大浮亏        -{res.max_unrealized:.4f} USDC")
    if res.blocked:
        lines.append(f"被名义上限拦下  {res.blocked} 次（说明层数铺不满，参数不匹配）")
    if p.anchor_mode == "rolling":
        lines.append(f"重锚次数        {res.recenters} 次")
    return "\n".join(lines)


def verdict(res: GridResult, p: GridParams) -> str:
    total = res.net + res.final_unrealized
    margin = res.peak_notional / p.leverage
    if margin <= 0:
        return "这段行情里没有成交，参数区间太宽，降低 step_pct 或加大 base_usd。"
    roi = total / margin * 100.0
    parts = []
    if total > 0:
        parts.append(f"净利润 {total:+.4f} USDC，按峰值保证金 ${margin:,.0f} 算是 {roi:+.2f}%")
    else:
        parts.append(f"净亏 {total:+.4f} USDC，按峰值保证金 ${margin:,.0f} 算是 {roi:+.2f}%")
    if res.max_unrealized > max(margin * 0.3, 5):
        parts.append(f"最大浮亏 -{res.max_unrealized:.2f} 相对保证金偏大，扛不扛得住要想清楚")
    if res.final_qty > 0:
        parts.append(f"期末还压着 {res.final_qty:.4f} 币没平掉 —— 网格的钱有一部分是「没实现」的")
    return "；".join(parts) + "。"


def split_halves(bars: List[Candle], p: GridParams) -> Tuple[GridResult, GridResult]:
    mid = len(bars) // 2
    return simulate(bars[:mid], p), simulate(bars[mid:], p)


def run_sweep(bars: List[Candle], p: GridParams) -> None:
    steps = (0.3, 0.5, 0.8, 1.2, 2.0)
    levels = (4, 6, 8, 12)
    print()
    print("=" * 74)
    print("参数扫描：行 = 格间距，列 = 层数。表内是「已实现 + 浮亏」合计 USDC")
    print("            括号里是最大浮亏。只在前半段也成立的行才值得看。")
    print("=" * 74)
    header = "step \\ 层数 ".ljust(12) + "".join(f"{f'{n} 层':>15}" for n in levels)
    print(header)
    best: Optional[Tuple[float, float, int]] = None
    for s in steps:
        row = f"{s:>5.1f}%      "
        for n in levels:
            q = GridParams(**{**p.__dict__, "step_pct": s, "levels": n})
            r = simulate(bars, q)
            total = r.net + r.final_unrealized
            row += f"{total:>+8.2f}(-{r.max_unrealized:>4.1f})"
            if best is None or total > best[0]:
                best = (total, s, n)
        print(row)
    if best:
        print(f"\n整段最优：step {best[1]}% / {best[2]} 层 → {best[0]:+.4f} USDC")
        q = GridParams(**{**p.__dict__, "step_pct": best[1], "levels": best[2]})
        a, b = split_halves(bars, q)
        ta, tb = a.net + a.final_unrealized, b.net + b.final_unrealized
        mark = "两段都为正，值得继续看" if (ta > 0 and tb > 0) else "但后半段为负 → 多半是过拟合"
        print(f"         前半段 {ta:+.4f}   后半段 {tb:+.4f}   {mark}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="网格 + 马丁回测")
    ap.add_argument("--coin", default="ETH")
    ap.add_argument("--interval", default="15m")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--step", type=float, default=0.8, help="层间距 %%")
    ap.add_argument("--levels", type=int, default=8)
    ap.add_argument("--tp", type=float, default=0.8, help="每层止盈 %%")
    ap.add_argument("--base-usd", type=float, default=25.0, help="第 1 层名义")
    ap.add_argument("--mult", type=float, default=1.4, help="马丁倍数")
    ap.add_argument("--max-mult", type=float, default=8.0)
    ap.add_argument("--max-notional", type=float, default=500.0)
    ap.add_argument("--leverage", type=int, default=5)
    ap.add_argument("--anchor", choices=["fixed", "rolling"], default="fixed")
    ap.add_argument("--recenter", type=float, default=6.0)
    ap.add_argument("--no-martingale", action="store_true", help="马丁倍数设为 1.0")
    ap.add_argument("--sweep", action="store_true")
    a = ap.parse_args(argv)

    p = GridParams(
        coin=a.coin, interval=a.interval, days=a.days,
        step_pct=a.step, levels=a.levels, tp_pct=a.tp,
        base_usd=a.base_usd, mult=1.0 if a.no_martingale else a.mult,
        max_mult=a.max_mult, max_notional=a.max_notional,
        leverage=a.leverage, anchor_mode=a.anchor, recenter_pct=a.recenter,
    )

    bars = load_bars(p)
    if not bars:
        print("没有取到 K 线，无法回测。")
        return 1

    r = simulate(bars, p)
    print(report(r, p))
    print("-" * 74)
    print("判定：" + verdict(r, p))

    a1, a2 = split_halves(bars, p)
    t1, t2 = a1.net + a1.final_unrealized, a2.net + a2.final_unrealized
    print()
    print("分段稳健性（同一套参数，前后两段各跑一次）：")
    print(f"  前半段：{a1.rounds:>3} 轮  净 {t1:+.4f}   最大浮亏 -{a1.max_unrealized:.2f}")
    print(f"  后半段：{a2.rounds:>3} 轮  净 {t2:+.4f}   最大浮亏 -{a2.max_unrealized:.2f}")

    if a.sweep:
        run_sweep(bars, p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
