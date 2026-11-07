"""Hyperliquid 网格 + 马丁 交易机器人 —— 命令行入口。

用法：
    python main.py web                   # 启动网页交易台（推荐）
    python main.py configs                # 列出所有实例配置
    python main.py check  -c configs/eth-grid.yaml    # 自检：配置、网络、账户（只读）
    python main.py run    -c configs/eth-grid.yaml    # 命令行启动单个实例
    python main.py status -c configs/eth-grid.yaml    # 查看账户与网格状态
    python main.py cancel-all -c configs/eth-grid.yaml
    python main.py close-all  -c configs/eth-grid.yaml
    python main.py reset-state -c configs/eth-grid.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional

from bot.broker import LiveBroker, MarketFeed, PaperBroker
from bot.config import Config, list_configs, load_config
from bot.engine import EngineHalt, GridEngine
from bot.logger import setup_logger
from bot.manager import make_engine
from bot.notify import Notifier
from bot.state import BotState
from bot.utils import fmt_px, fmt_usd, now_ms

ROOT = Path(__file__).resolve().parent

BANNER = r"""
  __  __ _   _ ____  _____ ____  _     ___ _ _   _ ___ ____
 |  |  | | | |  _ \| ____|  _ \| |   |_ _/ | | | |_ _/ ___|
 |  |__| |_| | |_) |  _| | |_) | |    | | | |_| | | |  _
 |  |  |  _  |  __/| |___|  _ <| |___ | | |  _  | | |_| |
 |__|  |_| |_|_|   |_____|_| \_\_____|___|_| |_|___|\____|
        网格 + 马丁 永续合约交易机器人   (网格有风险，请先干跑)
"""


# ------------------------------------------------------------------ 配置解析
def resolve_config(arg: Optional[str]) -> Path:
    if arg:
        p = Path(arg)
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if not p.exists():
            raise SystemExit(f"找不到配置文件：{p}")
        return p

    for cand in (ROOT / "configs" / "eth-grid.yaml", ROOT / "config.yaml"):
        if cand.exists():
            return cand
    cfgs = list_configs(ROOT / "configs")
    if cfgs:
        return cfgs[0]
    raise SystemExit("configs/ 目录下没有任何配置文件。可用 `python main.py web` 从网页里创建。")


def build(cfg: Config, logger):
    state = BotState(cfg.root / cfg.runtime.state_file, cfg.market.coin).load()
    feed = MarketFeed(cfg.env.base_url)

    if cfg.env.dry_run:
        broker = PaperBroker(cfg, logger)
        addr = cfg.env.account_address or ""
        if addr:
            av = feed.account_value(addr)
            if av > 0:
                broker.balance = av
        return broker, feed, state

    broker = LiveBroker(cfg, logger)
    return broker, feed, state


# ------------------------------------------------------------------ 命令
def cmd_check(cfg: Config, logger) -> int:
    logger.info("配置校验 ...")
    cfg.validate()
    logger.info("配置 OK")
    logger.info("%s", cfg.summary())

    feed = MarketFeed(cfg.env.base_url)
    coin = cfg.market.coin
    mid = feed.mid_price(coin)
    szd = feed.sz_decimals(coin)
    logger.info("网络连通 OK：%s 中间价 = %s，szDecimals = %d", coin, fmt_px(mid), szd)

    if cfg.strategy == "signal":
        s = cfg.signal
        size = s.size if s.size > 0 else cfg.market.base_size
        notional = size * mid
        logger.info(
            "参数体检 : 信号策略 EMA %d/%d 交叉 @ %s K 线，下单量 %.6f %s，名义约 $%s（交易所下限 $10）",
            s.fast_ema,
            s.slow_ema,
            s.interval,
            size,
            coin,
            fmt_usd(notional),
        )
        logger.info(
            "           进场 %s，出场 跟踪止损 max(%s%%, ATR×%s)%s，做空 %s",
            "回撤 %.2f%% 挂限价单（maker）" % s.entry_offset_pct if s.is_maker else "市价 IOC（taker）",
            s.trail_pct,
            s.atr_mult,
            ("，止盈 %s%%" % s.take_profit_pct) if s.take_profit_pct > 0 else "",
            "开" if s.allow_short else "关",
        )
        try:
            from bot.candles import CandleFeed, max_history_days

            bars = CandleFeed(cfg.env.base_url, interval=s.interval, limit=s.candles).closed(
                coin, limit=s.candles
            )
            logger.info(
                "           已取得 %d 根已收盘 %s K 线（该周期最多约 %.1f 天历史），指标%s",
                len(bars),
                s.interval,
                max_history_days(s.interval),
                "可算" if len(bars) >= s.slow_ema + 1 else f"不足（需要 {s.slow_ema + 1} 根）",
            )
        except Exception as e:
            logger.warning("           拉取 K 线失败（不影响下单，但启动时会再检查）：%s", e)
        logger.info("           ⚠ 上真钱前请先跑 tools/backtest.py —— 手续费是这类策略的天花板")
    elif cfg.strategy == "trigger":
        t = cfg.trigger
        notional = t.size * t.price
        logger.info(
            "参数体检 : 触发单 %s %.6f %s @ %s，名义 $%s（交易所下限 $10）",
            "买入" if t.is_buy else "卖出",
            t.size,
            coin,
            fmt_px(t.price),
            fmt_usd(notional),
        )
        if t.price < mid:
            logger.info("           现价 %s 在触发价上方，会挂在盘口等价格跌到 %s", fmt_px(mid), fmt_px(t.price))
        else:
            logger.info("           现价 %s 已达到/低于触发价，挂出后可能立即成交", fmt_px(mid))
        logger.info("           成交后：%s", f"自动止盈 +{t.tp_pct}%" if t.tp_pct > 0 else "只建仓，不挂止盈止损")
        logger.info("           杠杆 %sx(%s)，占用保证金约 $%s", cfg.market.leverage, cfg.market.margin_mode,
                    fmt_usd(notional / max(1, cfg.market.leverage)))
    else:
        # 与引擎同款公式：层价单利下移，单量按马丁倍数逐层放大（受 max_size_multiple 封顶）。
        # 注意不能简化成「首层名义 × 层数 × 倍数」——那会把累计名义低估 2~3 倍，
        # 让人误以为离风控上限很远，实际上深处几层就已经顶到上限、根本挂不出去。
        step = cfg.grid.step_pct / 100.0
        sign = 1.0 if cfg.grid.mode == "short" else -1.0

        def _mult(i: int) -> float:
            if not cfg.martingale.enabled:
                return 1.0
            return min(cfg.martingale.multiplier ** (i - 1), cfg.martingale.max_size_multiple)

        sizes = [cfg.market.base_size * _mult(i) for i in range(1, cfg.grid.levels + 1)]
        notionals = [sizes[i - 1] * mid * (1 + sign * step * i) for i in range(1, cfg.grid.levels + 1)]
        total = sum(notionals)
        cap = cfg.risk.max_total_notional_usd
        logger.info(
            "参数体检 : 第 1 层名义约 $%s，全层成交累计名义约 $%s、持仓约 %.4f %s、占用保证金约 $%s（%sx）",
            fmt_usd(notionals[0]),
            fmt_usd(total),
            sum(sizes),
            coin,
            fmt_usd(total / max(1, cfg.market.leverage)),
            cfg.market.leverage,
        )
        if total > cap:
            logger.warning(
                "           ⚠ 全层成交需要 $%s 名义，超过风控上限 $%s —— 深处几层挂不出去，网格铺不满",
                fmt_usd(total),
                fmt_usd(cap),
            )

    if not cfg.private_key:
        logger.warning("未配置 HL_PRIVATE_KEY —— 仅能跑 check / 干跑，无法真实下单")
        return 0

    live = LiveBroker(cfg, logger)
    info = live.verify_connection()
    logger.info("钱包地址 : %s", info["wallet"])
    logger.info("交易账户 : %s%s", info["account"], "   (使用 API 钱包，安全)" if info["agent_wallet"] else "   (使用主钱包私钥)")
    logger.info("账户模式 : %s", info.get("abstraction") or "default（传统账户）")
    breakdown = ""
    if info.get("spot_usdc"):
        breakdown = (
            f"  (统一账户：永续 ${fmt_usd(info['perp_account_value'])}"
            f" / 现货 USDC ${fmt_usd(info['spot_usdc'])}，"
            f"二者为同一笔钱，取较大值)"
        )
    logger.info("账户权益 : $%s%s", fmt_usd(info["account_value"]), breakdown)
    logger.info("现有挂单 : %s 笔", info["open_orders"])
    floor = cfg.risk.min_account_value_usd
    if info["account_value"] < floor:
        logger.warning(
            "⚠️  账户权益 $%s 低于风控下限 $%s —— 实盘一旦开仓就会立刻触发熔断并市价强平"
            "（白亏手续费和价差）。请先充值，或调低 risk.min_account_value_usd。",
            fmt_usd(info["account_value"]),
            fmt_usd(floor),
        )
    if not info["agent_wallet"]:
        logger.warning("⚠️  建议改用 API 钱包（Agent Wallet）：即使私钥泄漏也无法提币")

    return 0


def cmd_configs(cfg: Config, logger) -> int:
    cfgs = list_configs(ROOT / "configs")
    if not cfgs:
        print("configs/ 目录下没有配置文件。")
        return 0
    print(f"共 {len(cfgs)} 个实例配置：\n")
    print(f"{'实例名':<18}{'标的':<7}{'策略':<10}{'方向':<7}{'模式':<6}{'关键参数':<22}文件")
    print("-" * 92)
    for p in cfgs:
        try:
            c = load_config(p)
            c.root = ROOT
            if c.strategy == "trigger":
                t = c.trigger
                strat, side = "触发单", ("买入" if t.is_buy else "卖出")
                detail = f"{fmt_px(t.price)} × {t.size}"
            elif c.strategy == "signal":
                s = c.signal
                strat, side = "信号策略", ("多空" if s.allow_short else "只做多")
                detail = f"EMA {s.fast_ema}/{s.slow_ema} @ {s.interval}"
            else:
                strat = "网格+马丁"
                side = {"long": "做多", "short": "做空", "neutral": "双向"}.get(c.grid.mode, c.grid.mode)
                detail = f"{c.grid.levels} 层 / 间距 {c.grid.step_pct}%"
            print(
                f"{c.slot_name:<18}{c.market.coin:<7}{strat:<10}{side:<7}"
                f"{'干跑' if c.env.dry_run else '实盘':<6}{detail:<22}{p.name}"
            )
        except Exception as e:
            print(f"{p.stem:<18}读取失败: {str(e)[:50]}")
    print()
    return 0


def cmd_run(cfg: Config, logger, force_dry: bool) -> int:
    if force_dry:
        cfg.env.dry_run = True
    cfg.validate()

    if not cfg.env.dry_run:
        logger.warning("=" * 62)
        logger.warning("⚠️  实盘模式：将使用真实资金下单")
        if cfg.strategy == "trigger":
            logger.warning("⚠️  触发单：价格到达即成交，成交后不做任何止盈止损")
        elif cfg.strategy == "signal":
            logger.warning(
                "⚠️  信号策略：会按 EMA 交叉自动开仓，并按跟踪止损自动平仓。"
                "它会**真的下单买入卖出**，请先确认干跑结果符合预期"
            )
        else:
            logger.warning("⚠️  网格+马丁在单边行情中会持续加仓，请确认风控参数")
        logger.warning("⚠️  按 Ctrl+C 可安全退出（不会自动平仓，但会保存状态）")
        logger.warning("=" * 62)

    broker, feed, state = build(cfg, logger)
    engine = make_engine(cfg, broker, feed, state, logger, Notifier(cfg, logger, cfg.slot_name))
    try:
        engine.run()
    except EngineHalt as e:
        logger.error("启动被拒绝：%s", e)
        return 3
    except KeyboardInterrupt:
        logger.info("收到中断信号，保存状态后退出")
        state.save()
    return 0


def cmd_status(cfg: Config, logger) -> int:
    broker, feed, state = build(cfg, logger)
    coin = cfg.market.coin
    mid = feed.mid_price(coin)
    snap = broker.snapshot(coin)

    print("-" * 62)
    print(f"实例         : {cfg.slot_name}")
    strat_label = {
        "trigger": "触发单(" + ("买入" if cfg.trigger.is_buy else "卖出") + ")",
        "signal": f"信号策略(EMA {cfg.signal.fast_ema}/{cfg.signal.slow_ema} @ {cfg.signal.interval})",
    }.get(cfg.strategy, "网格 + 马丁")
    print(f"策略         : {strat_label}")
    print(f"模式         : {'干跑(Paper)' if cfg.env.dry_run else '实盘(Live)'}  {cfg.env.network}")
    print(f"标的 / 中间价: {coin} / {fmt_px(mid)}")
    if cfg.strategy == "trigger":
        t = cfg.trigger
        dist = (t.price - mid) / mid * 100.0 if mid else 0.0
        print(f"触发单       : {t.size} {coin} @ {fmt_px(t.price)}   名义 ${fmt_usd(t.size * t.price)}   距触发 {dist:+.2f}%")
    elif cfg.strategy == "signal":
        s = cfg.signal
        size = s.size if s.size > 0 else cfg.market.base_size
        slot = state.slots.get("S1") or {}
        print(
            f"信号策略     : EMA {s.fast_ema}/{s.slow_ema} 交叉 @ {s.interval}  "
            f"进场 {s.entry_mode}  跟踪止损 max({s.trail_pct}%, ATR×{s.atr_mult})"
        )
        print(
            f"下单量       : {size} {coin}（名义约 ${fmt_usd(size * mid)}）  "
            f"轮数 {state.data.get('rounds', 0)}/{s.max_rounds or '不限'}  "
            f"持仓状态 {'持仓中' if slot.get('state') == 'open' else '空仓'}"
        )
    print(f"账户权益     : ${fmt_usd(snap.account_value)}")
    print(f"净持仓       : {snap.net_position:.6f}   开仓均价: {fmt_px(snap.entry_px)}")
    print(f"浮动盈亏     : {snap.unrealized_pnl:+.4f}")
    print(f"名义价值     : ${fmt_usd(snap.total_notional)}   强平价: {fmt_px(snap.liquidation_px)}")
    print(f"挂单数       : {len(snap.open_oids)}")
    print("-" * 62)
    if cfg.strategy == "trigger":
        print(f"状态         : {state.status}   触发单已成交: {'是' if state.data.get('trigger_done') else '否'}")
        if state.data.get("trigger_done_reason"):
            print(f"完成原因     : {state.data.get('trigger_done_reason')}")
    elif cfg.strategy == "signal":
        print(f"状态         : {state.status}   信号策略结束: {'是' if state.data.get('signal_done') else '否'}")
        if state.data.get("signal_done_reason"):
            print(f"完成原因     : {state.data.get('signal_done_reason')}")
    else:
        print(f"状态         : {state.status}   锚定价: {fmt_px(state.data.get('anchor_price') or 0)}")
    print(f"累计实现盈亏 : {float(state.data.get('realized_usd', 0)):+.4f}   {'完成次数' if cfg.strategy == 'trigger' else '完成网格次数'}: {state.data.get('closed_slots', 0)}")
    if state.status == "STOPPED":
        left = max(0, (state.stopped_until_ms - now_ms()) / 1000)
        print(f"停止原因     : {state.data.get('stop_reason')}   冷却剩余: {left/60:.1f} 分钟")
    print("-" * 62)

    slots = state.slots
    if slots:
        print(f"{'单号':<6}{'方向':<6}{'状态':<12}{'数量':<12}{'成交价':<14}{'止盈价':<14}")
        for key in sorted(slots, key=lambda k: (k[0], int(k[1:]) if k[1:].isdigit() else 0)):
            s = slots[key]
            print(
                f"{key:<6}{'买' if s['side'] == 'buy' else '卖':<6}"
                f"{'待建仓/待触发' if s['state'] == 'pending' else ('空仓' if s['state'] == 'flat' else '已成交'):<12}"
                f"{s['size']:<12.6f}{fmt_px(s.get('entry_px')):<14}{fmt_px(s.get('tp_px')):<14}"
            )
    else:
        print("本地状态里没有挂单/层位记录")
    return 0


def cmd_cancel_all(cfg: Config, logger) -> int:
    broker, feed, state = build(cfg, logger)
    n = broker.cancel_all_for_coin(cfg.market.coin)
    logger.info("已撤销 %d 笔 %s 挂单", n, cfg.market.coin)
    for s in state.slots.values():
        s["entry_oid"] = None
        s["tp_oid"] = None
        s["entry_cloid"] = None
        s["tp_cloid"] = None
    state.save()
    return 0


def cmd_close_all(cfg: Config, logger) -> int:
    broker, feed, state = build(cfg, logger)
    n = broker.cancel_all_for_coin(cfg.market.coin)
    logger.info("已撤销 %d 笔挂单", n)
    res = broker.close_position(cfg.market.coin, cfg.market.close_slippage_pct)
    logger.info("平仓结果：%s", res)
    state.slots.clear()
    state.save()
    return 0


def cmd_reset_state(cfg: Config, logger) -> int:
    state = BotState(cfg.root / cfg.runtime.state_file, cfg.market.coin)
    state.reset()
    logger.info("本地状态已清空：%s", state.path)
    return 0


def cmd_web(cfg: Config, logger, args) -> int:
    from web import server

    server.run(ROOT, host=args.host, port=args.port)
    return 0


# ------------------------------------------------------------------ main
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="gridbot",
        description="Hyperliquid 网格 + 马丁 交易机器人",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "command",
        choices=["web", "configs", "check", "run", "status", "cancel-all", "close-all", "reset-state"],
    )
    ap.add_argument("-c", "--config", default=None, help="配置文件路径，默认取 configs/ 下的第一个")
    ap.add_argument("--dry-run", action="store_true", help="强制干跑（即使配置里 dry_run=false）")
    ap.add_argument("--host", default="127.0.0.1", help="网页控制台监听地址，默认仅本机")
    ap.add_argument("--port", type=int, default=8787, help="网页控制台端口，默认 8787")
    args = ap.parse_args(argv)

    config_path = resolve_config(args.config)
    cfg = load_config(config_path)
    logger = setup_logger(cfg.root / cfg.runtime.log_dir, cfg.runtime.log_level, name=f"gridbot.{cfg.slot_name}")

    handlers = {
        "web": lambda: cmd_web(cfg, logger, args),
        "configs": lambda: cmd_configs(cfg, logger),
        "check": lambda: cmd_check(cfg, logger),
        "run": lambda: cmd_run(cfg, logger, args.dry_run),
        "status": lambda: cmd_status(cfg, logger),
        "cancel-all": lambda: cmd_cancel_all(cfg, logger),
        "close-all": lambda: cmd_close_all(cfg, logger),
        "reset-state": lambda: cmd_reset_state(cfg, logger),
    }

    if args.command != "web":
        print(BANNER)
    print(f"配置文件：{config_path}\n")

    try:
        return handlers[args.command]()
    except EngineHalt as e:
        print(f"\n{e}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        logger.info("已中断")
        return 130
    except Exception as e:
        logger.exception("执行失败：%s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
