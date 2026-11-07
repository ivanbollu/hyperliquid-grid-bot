"""EMA 趋势跟随策略回测。

**为什么必须先跑这个**
----------------------
手续费是策略可行性的天花板：maker 0.015%/边、taker 0.045%/边，
往返一次就是 0.03%~0.09%。而 ETH 分钟级波动常常只有 0.05%~0.15%。
所以「看起来能赚」的信号，扣完手续费很可能就是负的 ——
上真钱之前，先用真实历史 K 线算清楚。

**怎么做才诚实**
----------------
* 信号只用**已收盘** K 线计算，成交放在**下一根** K 线，不做「用未来价格成交」的美梦；
* 同一根 K 线里既触及止盈又触及止损时，一律按**先止损**处理（最坏情况）；
* 入场按 maker 算就要求价格真的回撤到挂单价（用当根最低/最高价判断），
  没碰到就算「这单没成交」，不假装成交；
* 顺势止损按 taker 计费（真实的止损单是吃单成交的，别按 maker 自欺欺人）；
* 输出里同时给出「前半段 / 后半段」分段结果 —— 只有两段都成立才谈得上稳健，
  后半段崩掉的参数多半是过拟合。

用法
----
    python tools/backtest.py                                  # 默认 15m / 30 天
    python tools/backtest.py --interval 5m --days 17          # 5m 只有约 17.5 天历史
    python tools/backtest.py --entry taker --trail 0.8
    python tools/backtest.py --sweep                          # 扫参数（小心过拟合）
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.candles import Candle, CandleFeed, max_history_days  # noqa: E402
from bot.indicators import atr, cross_down, cross_up, ema, trail_stop_price  # noqa: E402

FEE_MAKER_BPS = 1.5  # 0.015%
FEE_TAKER_BPS = 4.5  # 0.045%


@dataclass
class Params:
    coin: str = "ETH"
    interval: str = "15m"
    days: int = 30
    fast: int = 12
    slow: int = 48
    trend: int = 0  # >0 时要求收盘价在长期均线上/下方才允许开多/开空
    atr_period: int = 14
    atr_mult: float = 2.0
    trail_pct: float = 0.6
    take_profit_pct: float = 0.0
    entry_mode: str = "maker"  # maker=挂回撤价等成交 | taker=下一根开盘价直接吃
    entry_offset_pct: float = 0.05  # maker 模式的挂单偏移
    allow_short: bool = False
    size_usd: float = 11.0
    leverage: int = 5


@dataclass
class Trade:
    side: str
    entry_t: int
    entry_px: float
    exit_t: int
    exit_px: float
    reason: str
    pnl: float
    fee: float
    bars: int


@dataclass
class Result:
    trades: List[Trade] = field(default_factory=list)
    equity: List[Tuple[int, float]] = field(default_factory=list)
    bars: int = 0
    first_t: int = 0
    last_t: int = 0
    first_close: float = 0.0
    last_close: float = 0.0
    missed_entries: int = 0

    # ------------------------------------------------------------ 统计
    @property
    def n(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl > 0]

    @property
    def losses(self) -> List[Trade]:
        return [t for t in self.trades if t.pnl <= 0]

    @property
    def net(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def fees(self) -> float:
        return sum(t.fee for t in self.trades)

    @property
    def gross(self) -> float:
        return sum(t.pnl + t.fee for t in self.trades)

    @property
    def win_rate(self) -> float:
        return (len(self.wins) / self.n * 100.0) if self.n else 0.0

    @property
    def avg_win(self) -> float:
        w = self.wins
        return sum(t.pnl for t in w) / len(w) if w else 0.0

    @property
    def avg_loss(self) -> float:
        l = self.losses
        return sum(t.pnl for t in l) / len(l) if l else 0.0

    @property
    def profit_factor(self) -> float:
        win = sum(t.pnl for t in self.wins)
        loss = abs(sum(t.pnl for t in self.losses))
        if loss <= 0:
            return float("inf") if win > 0 else 0.0
        return win / loss

    @property
    def max_drawdown(self) -> float:
        peak = 0.0
        worst = 0.0
        for _, v in self.equity:
            peak = max(peak, v)
            worst = min(worst, v - peak)
        return worst

    @property
    def hold_return_pct(self) -> float:
        if not self.first_close:
            return 0.0
        return (self.last_close - self.first_close) / self.first_close * 100.0


def load_candles(p: Params, verbose: bool = True) -> List[Candle]:
    """拉取足够多的已收盘 K 线 + 一小段预热数据。"""
    want = min(5000, int(p.days * 86_400_000 / _step_ms(p.interval)) + max(p.slow, p.trend) + 50)
    base = "https://api.hyperliquid.xyz"
    feed = CandleFeed(base, interval=p.interval, limit=want, cache_sec=0.0)
    bars = feed.closed(p.coin, limit=want, force=True)
    if verbose:
        span = (bars[-1].t - bars[0].t) / 86_400_000 if bars else 0
        print(
            f"K 线：{p.coin} {p.interval}  {len(bars)} 根已收盘  跨度 {span:.1f} 天"
            f"（接口上限约 {max_history_days(p.interval):.1f} 天）"
        )
        if bars:
            print(
                f"     {time.strftime('%Y-%m-%d %H:%M', time.localtime(bars[0].t / 1000))}"
                f"  →  {time.strftime('%Y-%m-%d %H:%M', time.localtime(bars[-1].t / 1000))}"
            )
    return bars


def _step_ms(interval: str) -> int:
    from bot.candles import interval_ms

    return interval_ms(interval)


def simulate(bars: List[Candle], p: Params) -> Result:
    """逐根 K 线推进的模拟。所有决策都只用「当前已收盘」的信息。"""
    res = Result(bars=len(bars))
    if len(bars) < max(p.fast, p.slow, p.trend or 0) + 5:
        return res

    closes = [float(c.c) for c in bars]
    f_ema = ema(closes, p.fast)
    s_ema = ema(closes, p.slow)
    t_ema = ema(closes, p.trend) if p.trend > 0 else None
    atr_arr = atr(bars, p.atr_period)

    res.first_t = bars[0].t
    res.first_close = closes[0]
    res.last_t = bars[-1].t
    res.last_close = closes[-1]

    warm = max(p.fast, p.slow, p.atr_period, p.trend or 0) + 1

    pos = 0  # 0=空仓 1=多 -1=空
    entry_px = entry_t = entry_i = 0
    extreme = 0.0
    pending: Optional[Tuple[int, Optional[float], int]] = None  # (方向, 限价或None, 触发根)
    cash = 0.0
    equity = [(bars[0].t, 0.0)]

    def open_pos(side: int, px: float, t: int, i: int) -> None:
        nonlocal pos, entry_px, entry_t, entry_i, extreme
        pos = side
        entry_px = px
        entry_t = t
        entry_i = i
        extreme = px

    def make_trade(side: int, exit_px: float, t: int, i: int, reason: str) -> Trade:
        is_long = side > 0
        notional = p.size_usd
        gross = notional * (exit_px - entry_px) / entry_px * (1 if is_long else -1)
        fee_in = notional * (FEE_MAKER_BPS if p.entry_mode == "maker" else FEE_TAKER_BPS) / 10000.0
        fee_out = notional * FEE_TAKER_BPS / 10000.0
        return Trade(
            side="long" if is_long else "short",
            entry_t=entry_t,
            entry_px=entry_px,
            exit_t=t,
            exit_px=exit_px,
            reason=reason,
            pnl=gross - fee_in - fee_out,
            fee=fee_in + fee_out,
            bars=max(1, i - entry_i),
        )

    for i in range(warm, len(bars)):
        bar = bars[i]
        prev = bars[i - 1]

        # ---------- 1) 处理上一根收盘后挂出的入场单 ----------
        if pending is not None:
            side, limit_px, sig_i = pending
            pending = None
            if limit_px is None:
                open_pos(side, float(bar.o), bar.t, i)
            elif (bar.l <= limit_px) if side > 0 else (bar.h >= limit_px):
                open_pos(side, limit_px, bar.t, i)
            else:
                res.missed_entries += 1

        # ---------- 2) 持仓管理：更新极值 + 检查出场 ----------
        if pos != 0:
            is_long = pos > 0
            extreme = max(extreme, float(bar.h)) if is_long else min(extreme, float(bar.l))
            atr_prev = atr_arr[i - 1] if i - 1 < len(atr_arr) else None
            stop = trail_stop_price(entry_px, extreme, is_long, p.trail_pct, atr_prev, p.atr_mult)
            tp = 0.0
            if p.take_profit_pct > 0:
                tp = (
                    entry_px * (1 + p.take_profit_pct / 100.0)
                    if is_long
                    else entry_px * (1 - p.take_profit_pct / 100.0)
                )

            exit_px = 0.0
            reason = ""
            # 同一根里可能既碰止损又碰止盈：按先止损处理（最坏情况）
            if stop > 0 and ((is_long and float(bar.l) <= stop) or (not is_long and float(bar.h) >= stop)):
                # 跳空时按开盘价成交（比止损价更差），别假装总能按理论价跑掉
                if is_long and float(bar.o) < stop:
                    exit_px, reason = float(bar.o), "跟踪止损(跳空)"
                elif (not is_long) and float(bar.o) > stop:
                    exit_px, reason = float(bar.o), "跟踪止损(跳空)"
                else:
                    exit_px, reason = stop, "跟踪止损"
            elif tp > 0 and ((is_long and float(bar.h) >= tp) or (not is_long and float(bar.l) <= tp)):
                exit_px, reason = tp, "止盈"

            if exit_px > 0:
                tr = make_trade(pos, exit_px, bar.t, i, reason)
                cash += tr.pnl
                res.trades.append(tr)
                pos = 0
                extreme = 0.0

        # ---------- 3) 收盘后判断新信号（空仓才开新仓） ----------
        if pos == 0 and pending is None:
            up = cross_up(f_ema, s_ema, i)
            down = cross_down(f_ema, s_ema, i)
            trend_long_ok = True
            trend_short_ok = True
            if t_ema is not None and t_ema[i] is not None:
                trend_long_ok = closes[i] > float(t_ema[i])
                trend_short_ok = closes[i] < float(t_ema[i])

            side = 0
            if up and trend_long_ok:
                side = 1
            elif down and p.allow_short and trend_short_ok:
                side = -1

            if side != 0:
                if p.entry_mode == "maker":
                    off = p.entry_offset_pct / 100.0
                    limit = closes[i] * (1 - off) if side > 0 else closes[i] * (1 + off)
                    pending = (side, limit, i)
                else:
                    pending = (side, None, i)

        # ---------- 4) 持仓中遇到反向交叉：下一根开盘平掉 ----------
        if pos != 0:
            is_long = pos > 0
            opposite = cross_down(f_ema, s_ema, i) if is_long else cross_up(f_ema, s_ema, i)
            if opposite and i + 1 < len(bars):
                nxt = bars[i + 1]
                tr = make_trade(pos, float(nxt.o), nxt.t, i + 1, "反向交叉")
                cash += tr.pnl
                res.trades.append(tr)
                pos = 0
                extreme = 0.0

        equity.append((bar.t, cash))

    res.equity = equity
    return res


# ---------------------------------------------------------------- 输出
def fmt_ts(ms: int) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(ms / 1000))


def report(res: Result, p: Params, title: str = "") -> str:
    lines = []
    span = (res.last_t - res.first_t) / 86_400_000 if res.last_t else 0
    lines.append("=" * 74)
    lines.append(
        f"{title or '回测结果'}    {p.coin} {p.interval}  {span:.1f} 天  {res.bars} 根 K 线"
    )
    lines.append(
        f"参数：EMA {p.fast}/{p.slow}"
        + (f" + 趋势过滤 {p.trend}" if p.trend else "")
        + f"  跟踪止损 max({p.trail_pct}%, ATR×{p.atr_mult})"
        + (f"  止盈 {p.take_profit_pct}%" if p.take_profit_pct else "")
        + f"  入场 {p.entry_mode}"
        + ("（偏移 %.2f%%）" % p.entry_offset_pct if p.entry_mode == "maker" else "")
        + f"  仓位 ${p.size_usd:.0f}  做空 {'开' if p.allow_short else '关'}"
    )
    lines.append("=" * 74)
    lines.append(f"  交易次数        : {res.n}")
    if res.n:
        lines.append(f"  胜率            : {res.win_rate:.1f}%  （{len(res.wins)} 胜 / {len(res.losses)} 负）")
        lines.append(f"  平均盈利        : {res.avg_win:+.4f} USDC")
        lines.append(f"  平均亏损        : {res.avg_loss:+.4f} USDC")
        pf = res.profit_factor
        lines.append(f"  盈亏比(总)      : {'∞' if pf == float('inf') else f'{pf:.2f}'}")
    lines.append(f"  毛收益(未扣费)  : {res.gross:+.4f} USDC")
    lines.append(f"  手续费合计      : {res.fees:.4f} USDC")
    lines.append(f"  净收益          : {res.net:+.4f} USDC   ← 扣掉手续费后的真实结果")
    lines.append(f"  最大回撤        : {res.max_drawdown:+.4f} USDC")
    if p.size_usd:
        lines.append(f"  净收益率        : {res.net / p.size_usd * 100:+.3f}%  (相对单笔仓位 ${p.size_usd:.0f})")
    lines.append(f"  同期买了不动    : {res.hold_return_pct:+.2f}%（{p.size_usd * res.hold_return_pct / 100:+.4f} USDC）")
    if res.missed_entries:
        lines.append(f"  未成交挂单      : {res.missed_entries} 次（价格没回撤到挂单价，这单就不做）")

    if res.n:
        lines.append("-" * 74)
        lines.append("  逐笔明细（最多 20 笔）：")
        for t in res.trades[:20]:
            lines.append(
                f"    {t.side:<5} {fmt_ts(t.entry_t)} @ {t.entry_px:>9,.2f} → "
                f"{fmt_ts(t.exit_t)} @ {t.exit_px:>9,.2f}  "
                f"{(t.exit_px - t.entry_px) / t.entry_px * 100 * (1 if t.side == 'long' else -1):+6.2f}%  "
                f"{t.pnl:+8.4f}  {t.reason}"
            )
    lines.append("=" * 74)
    return "\n".join(lines)


def verdict(res: Result, p: Params) -> str:
    if res.n == 0:
        return "结论：这段时间内没有产生任何信号 —— 参数不适合当前行情（慢线太长或周期太长）。"
    if res.net > 0:
        return (
            f"结论：扣掉手续费后 **净赚 {res.net:+.4f} USDC**（{res.n} 笔，胜率 {res.win_rate:.0f}%）。"
            "注意这只是一个历史样本，不代表未来。"
        )
    per_trade = res.net / res.n
    return (
        f"结论：扣掉手续费后 **净亏 {res.net:+.4f} USDC**（{res.n} 笔，平均每笔 {per_trade:+.4f}）。\n"
        f"      手续费吃掉 {res.fees:.4f}，毛收益只有 {res.gross:+.4f} —— "
        f"{'信号本身是亏的' if res.gross <= 0 else '信号能赚，但赚的不够付手续费'}。\n"
        f"      这种情况下不要上真钱：要么放大每笔目标价差（加长周期 / 放宽跟踪止损），要么换策略。"
    )


def split_halves(bars: List[Candle], p: Params) -> Tuple[Result, Result]:
    mid = len(bars) // 2
    return simulate(bars[: mid + 1], p), simulate(bars[mid:], p)


def run_sweep(bars: List[Candle], p: Params) -> None:
    print("\n" + "=" * 74)
    print("参数扫描（同一段历史里挑最优 = 典型过拟合，只当参考）")
    print("=" * 74)
    print(f"{'EMA':<12}{'跟踪止损':<18}{'交易':<6}{'胜率':<8}{'净收益':<12}{'后半段净收益':<14}")
    print("-" * 74)
    rows = []
    for fast in (8, 12, 20):
        for slow in (34, 48, 96):
            if fast >= slow:
                continue
            for trail in (0.4, 0.8, 1.5):
                cand = Params(**{**p.__dict__, "fast": fast, "slow": slow, "trail_pct": trail})
                r = simulate(bars, cand)
                _, second = split_halves(bars, cand)
                rows.append((f"{fast}/{slow}", f"{trail}% / ATR×{p.atr_mult}", r, second))
    rows.sort(key=lambda x: x[2].net, reverse=True)
    for ema_s, trail_s, r, second in rows:
        print(
            f"{ema_s:<12}{trail_s:<18}{r.n:<6}{r.win_rate:<8.1f}{r.net:<+12.4f}{second.net:<+14.4f}"
        )
    print("-" * 74)
    print("筛选建议：净收益为正 **且** 后半段也为正 的组合才值得继续研究；")
    print("          整段最优但后半段亏的，基本是在历史上「拟合」出来的。")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="EMA 趋势跟随策略回测")
    ap.add_argument("--coin", default="ETH")
    ap.add_argument("--interval", default="15m", help="K 线周期（5m 只有约 17.5 天历史）")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--fast", type=int, default=12)
    ap.add_argument("--slow", type=int, default=48)
    ap.add_argument("--trend", type=int, default=0, help=">0 时启用长期均线趋势过滤")
    ap.add_argument("--trail", type=float, default=0.6, help="跟踪止损百分比")
    ap.add_argument("--atr-period", type=int, default=14)
    ap.add_argument("--atr-mult", type=float, default=2.0)
    ap.add_argument("--take-profit", type=float, default=0.0)
    ap.add_argument("--entry", choices=["maker", "taker"], default="maker")
    ap.add_argument("--offset", type=float, default=0.05, help="maker 入场挂单偏移（%%）")
    ap.add_argument("--short", action="store_true", help="允许做空")
    ap.add_argument("--size-usd", type=float, default=11.0)
    ap.add_argument("--sweep", action="store_true")
    args = ap.parse_args(argv)

    p = Params(
        coin=args.coin.upper(),
        interval=args.interval,
        days=args.days,
        fast=args.fast,
        slow=args.slow,
        trend=args.trend,
        atr_period=args.atr_period,
        atr_mult=args.atr_mult,
        trail_pct=args.trail,
        take_profit_pct=args.take_profit,
        entry_mode=args.entry,
        entry_offset_pct=args.offset,
        allow_short=args.short,
        size_usd=args.size_usd,
    )

    bars = load_candles(p)
    if len(bars) < p.slow + 10:
        print("K 线太少，无法回测")
        return 1

    res = simulate(bars, p)
    print(report(res, p))
    print(verdict(res, p))

    first, second = split_halves(bars, p)
    print()
    print(f"分段稳健性检验（同一套参数，前后两段各跑一次）：")
    print(f"  前半段：{first.n} 笔  胜率 {first.win_rate:.0f}%  净收益 {first.net:+.4f}")
    print(f"  后半段：{second.n} 笔  胜率 {second.win_rate:.0f}%  净收益 {second.net:+.4f}")
    if first.n and second.n and (first.net > 0) != (second.net > 0):
        print("  ⚠️ 两段一正一负 —— 这个策略的稳定性存疑，别急着上真钱。")

    if args.sweep:
        run_sweep(bars, p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
