"""技术指标（纯函数，无副作用，方便离线单测与回测复用）。

设计原则
--------
* **只吃数值序列，不碰网络、不碰配置** —— 回测和实盘共用同一份实现，
  两边不会因为「各写一套公式」而算出不同结果。
* 返回值长度与输入等长，算不出来的位置是 `None`（而不是 0），
  免得把「还没有值」当成「值是 0」，在均线交叉判断里造出假信号。
* 交叉判定一律用**上一根已收盘 K 线**对**当前已收盘 K 线**，不用未收盘的那根。
"""

from __future__ import annotations

from typing import List, Optional, Sequence, Tuple

from .candles import Candle


# ---------------------------------------------------------------- 均线
def ema(values: Sequence[float], period: int) -> List[Optional[float]]:
    """指数移动平均。

    第 (period-1) 个位置用前 period 个值的简单平均播种，
    之后的递推式 alpha = 2/(period+1)。这与主流行情软件一致。
    """
    p = int(period)
    if p < 1:
        raise ValueError("period 必须 >= 1")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < p:
        return out

    seed = sum(float(v) for v in values[:p]) / p
    out[p - 1] = seed
    alpha = 2.0 / (p + 1.0)
    prev = seed
    for i in range(p, n):
        prev = alpha * float(values[i]) + (1.0 - alpha) * prev
        out[i] = prev
    return out


def sma(values: Sequence[float], period: int) -> List[Optional[float]]:
    """简单移动平均。"""
    p = int(period)
    if p < 1:
        raise ValueError("period 必须 >= 1")
    n = len(values)
    out: List[Optional[float]] = [None] * n
    if n < p:
        return out
    window = sum(float(v) for v in values[:p])
    out[p - 1] = window / p
    for i in range(p, n):
        window += float(values[i]) - float(values[i - p])
        out[i] = window / p
    return out


# ---------------------------------------------------------------- 波动率
def true_range(candles: Sequence[Candle]) -> List[float]:
    """真实波幅：max(高-低, |高-昨收|, |低-昨收|)。"""
    out: List[float] = []
    prev_close: Optional[float] = None
    for c in candles:
        if prev_close is None:
            out.append(float(c.h) - float(c.l))
        else:
            out.append(
                max(
                    float(c.h) - float(c.l),
                    abs(float(c.h) - prev_close),
                    abs(float(c.l) - prev_close),
                )
            )
        prev_close = float(c.c)
    return out


def atr(candles: Sequence[Candle], period: int = 14) -> List[Optional[float]]:
    """平均真实波幅（Wilder 平滑）。

    用来做跟踪止损的宽度：波动大时止损放宽，避免被正常噪声扫出去。
    """
    p = int(period)
    if p < 1:
        raise ValueError("period 必须 >= 1")
    tr = true_range(candles)
    n = len(tr)
    out: List[Optional[float]] = [None] * n
    if n < p:
        return out
    prev = sum(tr[:p]) / p
    out[p - 1] = prev
    for i in range(p, n):
        prev = (prev * (p - 1) + tr[i]) / p
        out[i] = prev
    return out


# ---------------------------------------------------------------- 交叉
def cross_up(a: Sequence[Optional[float]], b: Sequence[Optional[float]], i: int = -1) -> bool:
    """a 在 i 处上穿 b（前一根在下方或相等，当前一根在上方）。"""
    ia, ib = _resolve(i, len(a)), _resolve(i, len(b))
    if ia < 1 or ib < 1:
        return False
    a0, a1 = a[ia - 1], a[ia]
    b0, b1 = b[ib - 1], b[ib]
    if None in (a0, a1, b0, b1):
        return False
    return float(a0) <= float(b0) and float(a1) > float(b1)


def cross_down(a: Sequence[Optional[float]], b: Sequence[Optional[float]], i: int = -1) -> bool:
    """a 在 i 处下穿 b。"""
    ia, ib = _resolve(i, len(a)), _resolve(i, len(b))
    if ia < 1 or ib < 1:
        return False
    a0, a1 = a[ia - 1], a[ia]
    b0, b1 = b[ib - 1], b[ib]
    if None in (a0, a1, b0, b1):
        return False
    return float(a0) >= float(b0) and float(a1) < float(b1)


def _resolve(i: int, n: int) -> int:
    return (n + i) if i < 0 else i


# ---------------------------------------------------------------- 跟踪止损
def trail_stop_price(
    entry_px: float,
    extreme_px: float,
    is_long: bool,
    trail_pct: float = 0.0,
    atr_val: Optional[float] = None,
    atr_mult: float = 0.0,
) -> float:
    """跟踪止损价。

    距离取两种口径里**更宽**的那个（>0 才参与比较）：
      * 百分比：extreme * trail_pct%
      * 波动率：ATR * atr_mult

    只用一个口径的话，低波动时止损会贴得太近、高波动时又太远；
    取更宽的那个，宁可少被打断，也不要被噪声扫出去后行情继续走。
    """
    extreme = float(extreme_px)
    if extreme <= 0:
        return 0.0
    dist = 0.0
    if trail_pct and trail_pct > 0:
        dist = extreme * float(trail_pct) / 100.0
    if atr_val and atr_mult and float(atr_val) > 0 and float(atr_mult) > 0:
        dist = max(dist, float(atr_val) * float(atr_mult))
    if dist <= 0:
        return 0.0
    return extreme - dist if is_long else extreme + dist


# ---------------------------------------------------------------- 组合信号
def ema_state(
    candles: Sequence[Candle], fast: int, slow: int, trend: int = 0
) -> Optional[dict]:
    """基于**已收盘** K 线算出的 EMA 状态。

    返回 None 表示数据不足（还没走完 slow/trend 的预热期）。
    """
    bars = [c for c in candles if getattr(c, "closed", True)]
    need = max(int(fast), int(slow)) + 1
    if trend and trend > 0:
        need = max(need, int(trend) + 1)
    if len(bars) < need:
        return None

    closes = [float(c.c) for c in bars]
    f = ema(closes, fast)
    s = ema(closes, slow)
    t = ema(closes, trend) if trend and trend > 0 else None

    if f[-1] is None or s[-1] is None:
        return None

    state = {
        "fast": float(f[-1]),
        "slow": float(s[-1]),
        "up_cross": cross_up(f, s),
        "down_cross": cross_down(f, s),
        "above": float(f[-1]) > float(s[-1]),
        "trend_ok_long": True,
        "trend_ok_short": True,
        "close": closes[-1],
        "bars": len(bars),
    }
    if t is not None and t[-1] is not None:
        state["trend"] = float(t[-1])
        state["trend_ok_long"] = closes[-1] > float(t[-1])
        state["trend_ok_short"] = closes[-1] < float(t[-1])
    return state


def describe_signal(state: Optional[dict]) -> str:
    """给人看的一句话信号描述。"""
    if not state:
        return "数据不足（预热中）"
    if state["up_cross"]:
        return "金叉（快线上穿慢线）→ 买入信号"
    if state["down_cross"]:
        return "死叉（快线下穿慢线）→ 卖出信号"
    return "多头排列" if state["above"] else "空头排列"


def ema_pair(candles: Sequence[Candle], fast: int, slow: int) -> Tuple[List[Optional[float]], List[Optional[float]]]:
    """给网页画图用的两条均线序列（与 K 线一一对应，含未收盘那根）。"""
    closes = [float(c.c) for c in candles]
    return ema(closes, fast), ema(closes, slow)
