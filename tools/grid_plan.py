"""网格规模核算器 —— 改参数前先跑它，看「这套配置最多要吃多少保证金」。

为什么需要它：
  网格 + 马丁的规模不是「层数 × 单量」这么简单——每层的单量按倍数放大、
  价格按格距下移，全部成交时的峰值名义往往比直觉大好几倍。
  上真钱之前必须先把「全层成交要占多少保证金、跌到哪会爆仓」算清楚。

用法：
  python tools/grid_plan.py                          # 用 configs/eth-grid.yaml + 实时价
  python tools/grid_plan.py --config configs/xxx.yaml
  python tools/grid_plan.py --margin 200             # 指定保证金，直接看占用比例与爆仓价
  python tools/grid_plan.py --anchor 2550 --mmr 2.0  # 手动指定锚定价 / 维持保证金率
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config  # noqa: E402
from bot.utils import format_price, format_size  # noqa: E402

INFO_API = "https://api.hyperliquid.xyz/info"


def _post(body: dict):
    req = urllib.request.Request(
        INFO_API, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode())


def mid_price(coin: str) -> float:
    mids = _post({"type": "allMids"})
    return float(mids.get(coin) or 0.0)


def levels_of(cfg, anchor: float, szd: int):
    """按引擎同款公式（单利层价 + 马丁倍数封顶）算出每层明细。"""
    step = cfg.grid.step_pct / 100.0
    rows = []
    for i in range(1, cfg.grid.levels + 1):
        if cfg.martingale.enabled:
            mult = min(cfg.martingale.multiplier ** (i - 1), cfg.martingale.max_size_multiple)
        else:
            mult = 1.0
        sz = format_size(cfg.market.base_size * mult, szd)
        if cfg.grid.mode == "short":
            px = format_price(anchor * (1 + step * i), szd)
        else:
            px = format_price(anchor * (1 - step * i), szd)
        rows.append({"level": i, "mult": mult, "sz": sz, "px": px, "notional": sz * px})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="网格规模核算器")
    ap.add_argument("--config", default="configs/eth-grid.yaml")
    ap.add_argument("--anchor", type=float, default=0.0, help="锚定价；0 = 用配置里的 / 实时价")
    ap.add_argument("--szd", type=int, default=0, help="数量精度；0 = 自动查")
    ap.add_argument("--margin", type=float, default=0.0, help="可用保证金；0 = 自动推断")
    ap.add_argument("--mmr", type=float, default=1.5, help="维持保证金率 %（保守估 1.5~2）")
    ap.add_argument("--stress", action="store_true", help="打印跌到各价位时的浮亏与剩余保证金")
    args = ap.parse_args()

    cfg = load_config(args.config)
    coin = cfg.market.coin
    lev = float(cfg.market.leverage)

    szd = args.szd
    if not szd:
        try:
            meta = _post({"type": "meta"})
            for u in meta.get("universe", []):
                if u.get("name") == coin:
                    szd = int(u.get("szDecimals", 4))
                    break
        except Exception:
            pass
        szd = szd or 4

    anchor = args.anchor or cfg.grid.anchor_price
    if not anchor:
        anchor = mid_price(coin)
    if not anchor:
        print(f"拿不到 {coin} 价格，请用 --anchor 指定")
        return 1

    rows = levels_of(cfg, anchor, szd)
    tot_sz = sum(r["sz"] for r in rows)
    tot_nt = sum(r["notional"] for r in rows)
    avg_px = tot_nt / tot_sz if tot_sz else 0.0

    cap = cfg.risk.max_total_notional_usd
    # 名义上限能铺到第几层
    run = 0.0
    last_ok = 0
    for r in rows:
        if run + r["notional"] > cap:
            break
        run += r["notional"]
        last_ok = r["level"]

    margin = args.margin or (tot_nt / lev)
    mmr = args.mmr / 100.0
    liq = (avg_px * tot_sz - margin) / (tot_sz * (1 - mmr)) if tot_sz and tot_sz * (1 - mmr) else 0.0

    print("=" * 74)
    print(f"网格规模核算  ·  {Path(args.config).name}  ·  {coin}  {cfg.grid.mode}  杠杆 {lev:g}x")
    print("=" * 74)
    print(f"锚定价 {anchor:,.2f}   格距 {cfg.grid.step_pct}%   层数 {cfg.grid.levels}   "
          f"马丁 {cfg.martingale.multiplier}x（上限 {cfg.martingale.max_size_multiple}x）")
    print("-" * 74)
    print(f"{'层':>3}{'倍数':>7}{'单量':>10}{'挂单价':>12}{'名义':>11}{'累计名义':>12}")
    print("-" * 74)
    run = 0.0
    for r in rows:
        run += r["notional"]
        mark = "" if run <= cap else "  ← 超名义上限"
        print(f"{r['level']:>3}{r['mult']:>7.2f}{r['sz']:>10.4f}{r['px']:>12,.1f}"
              f"{r['notional']:>11,.2f}{run:>12,.2f}{mark}")
    print("-" * 74)
    print(f"全部成交：持仓 {tot_sz:.4f} {coin}   均价 {avg_px:,.2f}   总名义 {tot_nt:,.2f} USDC")
    print(f"峰值保证金 ≈ {tot_nt / lev:,.2f} USDC（按 {lev:g}x 估算）")
    print(f"名义上限 {cap:,.0f} → 最多铺到第 {last_ok} 层（累计 {run:,.2f}）"
          + ("  ⚠ 铺不满，调大 risk.max_total_notional_usd" if last_ok < cfg.grid.levels else "  ✓ 可铺满"))
    if margin > 0:
        use = tot_nt / lev
        print(f"可用保证金 {margin:,.2f} → 峰值占用 {use:,.2f}（{use / margin * 100:.0f}%）"
              + ("  ⚠ 占用过高，留不出缓冲" if use / margin > 0.9 else ""))
        if liq > 0:
            drop = (liq - anchor) / anchor * 100
            print(f"全层成交后爆仓价 ≈ {liq:,.0f}（较锚定价 {drop:+.1f}%，维持保证金率按 {args.mmr:g}% 估）")

    if args.stress:
        print("-" * 74)
        print(f"{'ETH 跌到':>10}{'网格浮亏':>12}{'剩余权益':>12}{'维持保证金':>12}{'距爆仓':>11}")
        print("-" * 74)
        for p in (2400, 2300, 2200, 2100, 2000, 1900, 1840, 1800):
            if p >= anchor:
                continue
            loss = (avg_px - p) * tot_sz
            remain = margin - loss
            maint = tot_sz * p * mmr
            if liq and p <= liq:
                gap = "已爆仓"
            elif liq:
                gap = f"{(p - liq) / liq * 100:+.1f}%"
            else:
                gap = ""
            print(f"{p:>10,}{-loss:>+12.2f}{remain:>12.2f}{maint:>12.2f}{gap:>11}")
        print("  （剩余权益 = 保证金 − 浮亏；维持保证金 = 持仓名义 × 维持保证金率；")
        print("    剩余权益 ≤ 维持保证金 即被强平。）")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
