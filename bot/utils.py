"""通用工具：精度处理、cloid 生成、时间戳。

Hyperliquid 对价格和数量的精度有硬性要求，送单前必须自己处理好，
否则交易所会直接拒单（返回 error）。

价格规则（永续）：
  * 有效数字 <= 5 位
  * 小数位数 <= 6 - szDecimals
数量规则：
  * 小数位数 <= szDecimals
"""

from __future__ import annotations

import time
from decimal import ROUND_DOWN, Decimal

CLOID_TAG_ENTRY = 0x01  # 网格建仓单
CLOID_TAG_TP = 0x02  # 网格止盈单
CLOID_TAG_MANUAL = 0x09  # 手动下单


def now_ms() -> int:
    """当前毫秒时间戳。"""
    return int(time.time() * 1000)


def _dec(x) -> Decimal:
    return Decimal(str(x))


def round_sig_figs(x: float, sig: int = 5) -> Decimal:
    """按有效数字向下取整。"""
    if x == 0:
        return Decimal(0)
    d = _dec(x)
    step = Decimal(1).scaleb(d.adjusted() - (sig - 1))
    return d.quantize(step, rounding=ROUND_DOWN)


def format_price(px: float, sz_decimals: int, is_perp: bool = True) -> float:
    """把价格规整成 Hyperliquid 能接受的格式。"""
    if px is None or px <= 0:
        return 0.0
    d = round_sig_figs(px, 5)
    max_dec = max(0, (6 if is_perp else 8) - int(sz_decimals))
    if d.as_tuple().exponent < -max_dec:
        d = d.quantize(Decimal(1).scaleb(-max_dec), rounding=ROUND_DOWN)
        d = round_sig_figs(float(d), 5)
    return float(d)


def format_size(sz: float, sz_decimals: int) -> float:
    """把数量规整成 Hyperliquid 能接受的格式。"""
    if sz is None or sz <= 0:
        return 0.0
    d = _dec(sz).quantize(Decimal(1).scaleb(-int(sz_decimals)), rounding=ROUND_DOWN)
    return float(d)


def make_cloid(tag: int, counter: int) -> str:
    """生成 16 字节 cloid：0x + 2 位类型 + 30 位序号。

    用 cloid 跟踪自己的订单，重启后也能和交易所对账，
    比只依赖 oid 更可靠。
    """
    return f"0x{tag & 0xFF:02x}{counter & ((1 << 120) - 1):030x}"


def is_bot_cloid(cloid: str) -> bool:
    """判断一个 cloid 是不是本 bot 下的。"""
    if not isinstance(cloid, str) or not cloid.startswith("0x") or len(cloid) != 34:
        return False
    try:
        return int(cloid[2:4], 16) in (CLOID_TAG_ENTRY, CLOID_TAG_TP, CLOID_TAG_MANUAL)
    except ValueError:
        return False


def fmt_usd(x: float) -> str:
    return f"{x:,.2f}"


def fmt_px(x: float) -> str:
    if x is None:
        return "-"
    if x >= 1000:
        return f"{x:,.2f}"
    return f"{x:.6g}"


def equity_floor_problem(cfg, info: dict) -> str:
    """启动前的权益下限体检：不通过返回错误说明，通过返回空串。

    为什么必须在「挂单之前」查：主循环里的熔断是在**成交之后**才跑的，
    权益不足时会出现「先买入、几秒后又被市价强平」，白亏手续费和价差（真金白银）。

    统一账户（unifiedAccount）下现货 USDC 与永续 accountValue 是**同一笔钱**的两个
    视角（实测：持仓时两者相等，且现货被 100% hold）。`info['account_value']`
    已按「取较大值」的口径给出真实权益，切勿再相加，否则权益会被算成两倍。
    """
    floor = float(getattr(getattr(cfg, "risk", None), "min_account_value_usd", 0) or 0)
    equity = float(info.get("account_value") or 0)
    if equity >= floor:
        return ""
    detail = ""
    if info.get("spot_usdc"):
        detail = (
            f"（统一账户：永续 ${float(info.get('perp_account_value') or 0):,.2f}"
            f" / 现货 USDC ${float(info.get('spot_usdc') or 0):,.2f}，二者为同一笔钱，已取较大值）"
        )
    return (
        f"账户权益 ${equity:,.2f} 低于风控下限 ${floor:,.2f}{detail}。\n"
        f"现在启动的话，挂单一成交就会立刻被熔断并市价平仓，白亏手续费和价差。\n"
        f"请先充值，或调低 risk.min_account_value_usd。"
    )
