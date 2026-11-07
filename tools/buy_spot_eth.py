"""买现货 ETH —— Hyperliquid 现货对 `@151`（UETH/USDC）。

★ 安全设计：默认是【预览模式】，只打印「会买多少、大概花多少钱」，绝不下单。
  确认数字无误后，加 `--yes` 才真正发出订单。这是花真钱的操作，请自己看清楚。

用法：
  python tools/buy_spot_eth.py --usd 700            # 预览（推荐先跑这个）
  python tools/buy_spot_eth.py --usd 700 --yes      # 真下单

前置条件：
  · .env 里的 HL_PRIVATE_KEY = API 钱包私钥（66 位）
  · 配置文件 env.account_address = 主钱包地址（用 API 钱包时必须填）
  · 账户的现货 USDC 余额 >= 要花的钱
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bot.config import load_config  # noqa: E402
from bot.utils import format_price, format_size  # noqa: E402

SPOT_PAIR = "@151"  # UETH / USDC
SPOT_SZD = 4        # UETH 的 szDecimals


def load_pk() -> str:
    p = ROOT / ".env"
    if not p.exists():
        raise SystemExit("找不到 .env，请先按 .env.example 建好并填 HL_PRIVATE_KEY")
    for ln in p.read_text(encoding="utf-8").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        k, _, v = s.partition("=")
        if k.strip() == "HL_PRIVATE_KEY":
            v = v.strip().strip('"').strip("'")
            if not v or "粘贴" in v:
                raise SystemExit(".env 里的 HL_PRIVATE_KEY 还是占位符，没填真私钥")
            return v
    raise SystemExit(".env 里没有 HL_PRIVATE_KEY")


def main() -> int:
    ap = argparse.ArgumentParser(description="买入现货 ETH（默认仅预览）")
    ap.add_argument("--config", default="configs/eth-grid.yaml", help="用来取主钱包地址")
    ap.add_argument("--usd", type=float, default=700.0, help="打算花多少 USDC")
    ap.add_argument("--slippage", type=float, default=0.3, help="IOC 限价滑点容忍 %%（默认 0.3）")
    ap.add_argument("--yes", action="store_true", help="★ 真的下单（不加则只预览）")
    args = ap.parse_args()

    try:
        from hyperliquid.exchange import Exchange
        from hyperliquid.info import Info
        from hyperliquid.utils import constants
        import eth_account
    except ImportError as e:
        print("缺少依赖：", e)
        print("请先 pip install -r requirements.txt")
        return 1

    cfg = load_config(args.config)
    pk = load_pk()
    account = eth_account.Account.from_key(pk)
    master = (cfg.env.account_address or "").strip() or account.address

    print("=" * 70)
    print("买入现货 ETH（@151 = UETH/USDC）")
    print("=" * 70)
    print(f"  签名钱包（API 钱包）: {account.address}")
    print(f"  交易账户（主钱包）  : {master}")
    if not cfg.env.account_address:
        print("  ⚠ 配置里 env.account_address 为空 —— 将按「签名钱包就是主钱包」处理。")
        print("    如果你用的是 API 钱包，请在配置里填主钱包地址，否则交易会记到错误的账户上。")

    info = Info(constants.MAINNET_API_URL, skip_ws=True)

    # 现货盘口
    book = info.l2_snapshot(SPOT_PAIR)
    lv = book.get("levels") or []
    if not lv or not lv[0] or not lv[1]:
        print("拿不到 @151 的盘口，稍后重试")
        return 1
    bid = float(lv[0][0]["px"])
    ask = float(lv[1][0]["px"])
    mid = (bid + ask) / 2

    print(f"  现货盘口            : bid {bid:,.2f} / ask {ask:,.2f}（中间价 {mid:,.2f}）")

    sz = format_size(args.usd / mid, SPOT_SZD)
    limit_px = format_price(ask * (1 + args.slippage / 100.0), SPOT_SZD, is_perp=False)
    cost = sz * mid

    print("-" * 70)
    print(f"  计划买入            : {sz:.4f} ETH")
    print(f"  预估花费            : ≈ {cost:,.2f} USDC（按中间价）")
    print(f"  限价（IOC，滑点上限）: {limit_px:,.2f} USDC/ETH")
    print(f"  最坏成交花费        : ≈ {sz * limit_px:,.2f} USDC")

    # 账户余额核对
    try:
        spot = info.spot_user_state(master)
        usdc = 0.0
        for b in spot.get("balances", []):
            if b.get("coin") == "USDC":
                usdc = float(b.get("total", 0))
        print(f"  现货 USDC 余额      : {usdc:,.2f}")
        if usdc < sz * limit_px:
            print(f"  ✗ 余额不足：需要约 {sz * limit_px:,.2f}，只有 {usdc:,.2f}。请先充值。")
            return 1
        print("  ✓ 余额足够")
    except Exception as e:
        print(f"  （余额查询失败，跳过核对：{e}）")

    if not args.yes:
        print("-" * 70)
        print("  当前是【预览模式】，没有下单。")
        print("  确认上面的数字没问题后，加 --yes 重新运行即可真正买入：")
        print(f"      python tools/buy_spot_eth.py --usd {args.usd:g} --yes")
        print("=" * 70)
        return 0

    print("-" * 70)
    print("  正在下单 ...")
    exch = Exchange(account, constants.MAINNET_API_URL, account_address=master)
    res = exch.order(SPOT_PAIR, True, sz, limit_px, {"limit": {"tif": "Ioc"}})

    ok = False
    detail = ""
    try:
        statuses = res["response"]["data"]["statuses"]
        st = statuses[0]
        if "filled" in st:
            f = st["filled"]
            ok = True
            detail = f"成交 {f.get('totalSz')} @ {f.get('avgPx')}（oid {f.get('oid')}）"
        elif "resting" in st:
            ok = True
            detail = f"挂在盘口 oid={st['resting'].get('oid')}（IOC 未立即成交，稍后可再跑一次）"
        elif "error" in st:
            detail = f"交易所拒绝：{st['error']}"
        else:
            detail = str(st)
    except Exception:
        detail = str(res)

    print(f"  返回：{detail}")
    print("=" * 70)
    if ok:
        print("完成。可以再跑一次预览确认持仓，或在网页上看现货余额。")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
