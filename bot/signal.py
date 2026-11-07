"""信号策略引擎：EMA 交叉 + 跟踪止损。

它和网格 / 触发单的根本区别
--------------------------
网格和触发单都是**挂单等成交**，成交之后就结束了；
信号策略是一个完整的「开仓 → 持有 → 平仓」循环，会**主动平仓**，
所以它才谈得上「根据行情变化实时买卖」。

决策逻辑
--------
每根 K 线**收盘后**计算指标（不用未收盘的那根，避免信号重绘）：

  快线上穿慢线（金叉）+ 趋势过滤通过  → 开多
  快线下穿慢线（死叉）              → 平多 / （允许做空时）开空

持仓期间的出场有三条路，谁先到谁生效：

  1. 跟踪止损：止损价 = 持仓期间的最有利价 − max(trail_pct%, ATR×atr_mult)
     创新高时止损自动上移，回撤到止损价就市价平掉
  2. 反向交叉：出现相反方向的交叉立即平仓
  3. 固定止盈（可选，take_profit_pct > 0 时启用）

⚠️ 关于「能不能赚钱」
-------------------
这个引擎本身是忠实执行信号的，但**信号好不好，得看回测**。
手续费是这类策略的天花板：往返 0.03%（全 maker）~0.09%（全 taker）。
上真钱之前请先跑：

    python tools/backtest.py --interval 15m --days 30 --sweep

干跑（dry_run: true）可以直接跑，用真实行情、纸面撮合，不花一分钱。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from .broker import AccountSnapshot, FillInfo, PaperBroker
from .candles import CandleFeed, interval_ms
from .config import Config
from .engine import EngineHalt
from .indicators import (
    atr,
    cross_down,
    cross_up,
    describe_signal,
    ema,
    ema_state,
    trail_stop_price,
)
from .notify import Notifier
from .state import BotState
from .utils import (
    CLOID_TAG_ENTRY,
    CLOID_TAG_TP,
    equity_floor_problem,
    fmt_px,
    fmt_usd,
    format_price,
    format_size,
    is_bot_cloid,
    now_ms,
)

SLOT_KEY = "S1"
LEVEL_TOL = 1e-9
FEE_MAKER_BPS = 1.5
FEE_TAKER_BPS = 4.5


class SignalEngine:
    """指标驱动、会主动平仓的策略引擎。接口与 GridEngine / TriggerEngine 一致。"""

    def __init__(self, cfg: Config, broker, feed, state: BotState, logger, notifier: Optional[Notifier] = None):
        self.cfg = cfg
        self.broker = broker
        self.feed = feed
        self.st = state
        self.log = logger
        self.notify = notifier or Notifier(cfg, logger, cfg.slot_name)
        self.slot = cfg.slot_name
        self.coin = cfg.market.coin
        self.sg = cfg.signal
        self.stopping = False
        self.stop_reason = ""
        self.error_streak = 0
        self.szd = 4
        self._loop = 0
        self._stop_event = threading.Event()
        self._warned: set = set()
        self._warned_at: Dict[str, float] = {}

        # K 线源（自己带缓存，主循环 5 秒一次不会把它打爆）
        self.candles = CandleFeed(
            cfg.env.base_url,
            interval=self.sg.interval,
            limit=self.sg.candles,
            cache_sec=self.sg.candle_cache_sec,
        )

        # 最近一次算出来的指标（给网页展示用）
        self._sig: Dict[str, Any] = {}
        self._atr_val: Optional[float] = None
        self._last_kline_ms = 0

        self._equity: deque = deque(maxlen=4000)
        self._equity_path = Path(cfg.root) / "state" / f"equity_{cfg.slot_name or 'default'}.jsonl"
        self._last_equity_ms = 0
        self._last_snap: Optional[AccountSnapshot] = None
        self._last_mid = 0.0
        self._last_error = ""
        self._started_ms = 0

    # =================================================================
    #                       外部控制（供网页调用）
    # =================================================================
    def request_stop(self, reason: str = "用户手动停止") -> None:
        self.stop_reason = reason
        self.stopping = True
        self._stop_event.set()

    def _position_side(self) -> str:
        slot = self.st.slots.get(SLOT_KEY)
        if not slot or slot.get("state") != "open":
            return "flat"
        return "long" if slot.get("side") == "buy" else "short"

    def _stop_px(self) -> float:
        slot = self.st.slots.get(SLOT_KEY)
        if not slot or slot.get("state") != "open":
            return 0.0
        is_long = slot.get("side") == "buy"
        return trail_stop_price(
            float(slot.get("entry_px") or 0),
            float(slot.get("extreme_px") or 0),
            is_long,
            self.sg.trail_pct,
            self._atr_val,
            self.sg.atr_mult,
        )

    def snapshot_dict(self) -> Dict[str, Any]:
        snap = self._last_snap
        slot = self.st.slots.get(SLOT_KEY)
        levels = []
        if slot and slot.get("state") in ("pending", "open"):
            levels.append(
                {
                    "key": SLOT_KEY,
                    "side": slot.get("side", "buy"),
                    "level": 1,
                    "state": slot["state"],
                    "size": slot.get("size") or 0.0,
                    "entry_px": slot.get("entry_px") or None,
                    "tp_px": self._stop_px() or None,
                    "has_order": bool(slot.get("entry_oid")),
                }
            )
        sig = dict(self._sig)
        return {
            "slot": self.slot,
            "coin": self.coin,
            "running": not self.stopping,
            "loop": self._loop,
            "uptime_sec": round((now_ms() - self._started_ms) / 1000.0, 1) if self._started_ms else 0,
            "mid": self._last_mid,
            "network": self.cfg.env.network,
            "dry_run": self.cfg.env.dry_run,
            "mode": f"signal-{self.sg.interval}",
            "strategy": "signal",
            "signal": {
                "interval": self.sg.interval,
                "fast_ema": self.sg.fast_ema,
                "slow_ema": self.sg.slow_ema,
                "trend_ema": self.sg.trend_ema,
                "entry_mode": self.sg.entry_mode,
                "entry_offset_pct": self.sg.entry_offset_pct,
                "trail_pct": self.sg.trail_pct,
                "atr_mult": self.sg.atr_mult,
                "take_profit_pct": self.sg.take_profit_pct,
                "allow_short": self.sg.allow_short,
                "max_rounds": self.sg.max_rounds,
                "rounds": int(self.st.data.get("rounds", 0)),
                "size": self._size(),
                "position": self._position_side(),
                "phase": self._phase_label(),
                "desc": sig.get("desc", "预热中"),
                "fast": sig.get("fast"),
                "slow": sig.get("slow"),
                "close": sig.get("close"),
                "bars": sig.get("bars", 0),
                "atr": self._atr_val,
                "stop_px": self._stop_px() or None,
                "extreme_px": (slot or {}).get("extreme_px") or None,
                "last_kline_ms": self._last_kline_ms,
            },
            "leverage": self.cfg.market.leverage,
            "anchor_price": sig.get("close") or 0.0,
            "account_value": snap.account_value if snap else 0.0,
            "net_position": snap.net_position if snap else 0.0,
            "entry_px": (snap.entry_px if snap else None),
            "unrealized_pnl": snap.unrealized_pnl if snap else 0.0,
            "total_notional": snap.total_notional if snap else 0.0,
            "margin_used": snap.margin_used if snap else 0.0,
            "liquidation_px": (snap.liquidation_px if snap else None),
            "open_orders": len(snap.open_oids) if snap else 0,
            "realized_usd": float(self.st.data.get("realized_usd", 0.0)),
            "closed_slots": int(self.st.data.get("closed_slots", 0)),
            "orders_today": int(self.st.data.get("orders_today", 0)),
            "status": self.st.status,
            "stop_reason": self.st.data.get("stop_reason") or "",
            "stopped_until_ms": self.st.stopped_until_ms,
            "error": self._last_error,
            "levels": levels,
            "trades": self.st.trades()[-60:],
            "equity": list(self._equity),
            "limits": {
                "max_total_notional_usd": self.cfg.risk.max_total_notional_usd,
                "max_position_size": self.cfg.risk.max_position_size,
                "max_unrealized_loss_usd": self.cfg.risk.max_unrealized_loss_usd,
                "min_account_value_usd": self.cfg.risk.min_account_value_usd,
                "max_open_orders": self.cfg.risk.max_open_orders,
                "max_daily_orders": self.cfg.risk.max_daily_orders,
            },
        }

    # =================================================================
    #                            启动
    # =================================================================
    def start(self) -> None:
        self.szd = self.feed.sz_decimals(self.coin)
        if isinstance(self.broker, PaperBroker):
            self.broker.set_sz_decimals(self.coin, self.szd)

        if self.st.status == "STOPPED" and now_ms() < self.st.stopped_until_ms:
            remain = (self.st.stopped_until_ms - now_ms()) / 1000.0
            raise EngineHalt(
                f"处于熔断冷却中（原因：{self.st.data.get('stop_reason') or '未知'}），"
                f"还需等待 {remain / 60:.1f} 分钟。确认风险可控后可先「清除状态」。"
            )

        mid = self.feed.mid_price(self.coin)
        self._check_plan(mid)

        info = self.broker.verify_connection()
        self.log.info("钱包地址   : %s", info.get("wallet"))
        self.log.info("交易账户   : %s%s", info.get("account"), "  (使用 API 钱包)" if info.get("agent_wallet") else "")
        self.log.info("网络       : %s", info.get("network"))
        self.log.info("账户模式   : %s", info.get("abstraction") or "default（传统账户）")
        breakdown = ""
        if info.get("spot_usdc"):
            breakdown = (
                f"  (永续 ${fmt_usd(float(info.get('perp_account_value') or 0))}"
                f" / 现货 USDC ${fmt_usd(float(info.get('spot_usdc') or 0))}，同一笔钱取较大值)"
            )
        self.log.info("账户权益   : $%s%s", fmt_usd(float(info.get("account_value") or 0)), breakdown)
        self.log.info("现有挂单   : %s 笔", info.get("open_orders"))

        # 权益下限必须在「开仓之前」拦下来，否则会出现「先开仓、几秒后被强平」
        problem = equity_floor_problem(self.cfg, info)
        if problem:
            raise EngineHalt(problem)

        self.broker.set_leverage(self.coin, self.cfg.market.leverage, self.cfg.market.is_cross)
        if isinstance(self.broker, PaperBroker):
            self.broker.feed_mid(mid)
            if isinstance(info.get("account_value"), (int, float)) and info["account_value"] > 0:
                self.broker.balance = float(info["account_value"])

        self.log.info("-" * 62)
        self.log.info("%s", self.cfg.summary())
        self.log.info("当前%s中间价 : %s", self.coin, fmt_px(mid))

        # 预热：指标需要足够的历史，不够就明确拒绝启动，别让它空转
        self._refresh_candles(force=True)
        if not self._sig:
            bars = self.candles.closed(self.coin, limit=self.sg.candles)
            raise EngineHalt(
                f"K 线数据不足，指标算不出来：拿到 {len(bars)} 根已收盘 "
                f"{self.sg.interval} K 线，至少需要 {self.sg.slow_ema + 1} 根"
                f"（slow_ema={self.sg.slow_ema}）。请调小 slow_ema 或换更小的周期。"
            )
        self.log.info("信号状态     : %s", self._sig.get("desc"))
        self.log.info(
            "             快线 %s / 慢线 %s / 收盘 %s / ATR %s",
            fmt_px(self._sig.get("fast")),
            fmt_px(self._sig.get("slow")),
            fmt_px(self._sig.get("close")),
            fmt_px(self._atr_val),
        )
        slot = self.st.slots.get(SLOT_KEY)
        if slot and slot.get("state") == "open":
            self.log.info(
                "接管已有持仓 : %s %.6f @ %s，当前跟踪止损 %s",
                "多" if slot.get("side") == "buy" else "空",
                slot.get("size") or 0,
                fmt_px(slot.get("entry_px")),
                fmt_px(self._stop_px()),
            )
        self.log.info("-" * 62)

        if SLOT_KEY not in self.st.slots:
            self.st.slots[SLOT_KEY] = self._new_slot()
        self.st.data["signal_rounds"] = int(self.st.data.get("rounds", 0))
        self.st.status = "RUNNING"
        self.st.save()

        snap = self.broker.snapshot(self.coin)
        self._cleanup_orphans(snap)
        self.st.save()

        self._last_snap = snap
        self._last_mid = mid
        self._started_ms = now_ms()
        self.stopping = False
        self._stop_event.clear()
        self._load_equity()
        self._record_equity(snap, force=True)

        self.log.info("启动完成，进入主循环（间隔 %.0f 秒）", self.cfg.runtime.loop_interval_sec)
        if self.notify.on_stop:
            self.notify.started(self.coin, self.cfg.env.network, snap.account_value, self.cfg.env.dry_run)

    def _size(self) -> float:
        raw = self.sg.size if self.sg.size > 0 else self.cfg.market.base_size
        return format_size(raw, self.szd)

    def _check_plan(self, mid: float) -> None:
        """启动前拦掉明显会被拒单 / 违反风控的参数。"""
        from .config import MIN_ORDER_NOTIONAL_USD

        sz = self._size()
        if sz <= 0:
            raise EngineHalt(
                f"下单量规整后为 0（signal.size={self.sg.size}，market.base_size="
                f"{self.cfg.market.base_size}，{self.coin} 的最小小数位是 {self.szd}）"
            )
        notional = sz * mid
        if notional < MIN_ORDER_NOTIONAL_USD - 1e-9:
            need = MIN_ORDER_NOTIONAL_USD / mid
            raise EngineHalt(
                f"下单量太小：{sz} × {fmt_px(mid)} = ${notional:,.2f}，"
                f"低于 Hyperliquid 永续合约最低下单金额 ${MIN_ORDER_NOTIONAL_USD:,.0f}。\n"
                f"请把 signal.size 提到 {need:.4f} 以上（或调高 market.base_size）。"
            )
        if sz > self.cfg.risk.max_position_size + LEVEL_TOL:
            raise EngineHalt(
                f"下单量 {sz} 超过净持仓上限 {self.cfg.risk.max_position_size}，"
                "请先调高 risk.max_position_size"
            )
        if notional > self.cfg.risk.max_total_notional_usd + LEVEL_TOL:
            raise EngineHalt(
                f"名义价值 ${notional:,.2f} 超过名义持仓上限 "
                f"${self.cfg.risk.max_total_notional_usd:,.0f}，请先调高它"
            )

    # =================================================================
    #                          权益曲线
    # =================================================================
    def _load_equity(self) -> None:
        try:
            if not self._equity_path.exists():
                return
            lines = self._equity_path.read_text(encoding="utf-8").strip().splitlines()[-4000:]
            for ln in lines:
                try:
                    pt = json.loads(ln)
                    if isinstance(pt, list) and len(pt) >= 2:
                        self._equity.append([float(pt[0]), float(pt[1])])
                except Exception:
                    continue
            self.log.info("已载入 %d 个权益曲线采样点", len(self._equity))
        except Exception as e:
            self.log.debug("载入权益曲线失败：%s", e)

    def _record_equity(self, snap: AccountSnapshot, force: bool = False) -> None:
        ts = now_ms()
        if not force and ts - self._last_equity_ms < self.cfg.runtime.equity_interval_sec * 1000:
            return
        self._last_equity_ms = ts
        point = [ts, round(snap.account_value, 6)]
        self._equity.append(point)
        try:
            self._equity_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._equity_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(point) + "\n")
        except Exception as e:
            self.log.debug("写入权益曲线失败：%s", e)

    # =================================================================
    #                            主循环
    # =================================================================
    def run(self) -> None:
        self.start()
        self.loop()

    def loop(self) -> None:
        try:
            while not self.stopping:
                t0 = time.time()
                try:
                    self.tick()
                    self.error_streak = 0
                    self._last_error = ""
                except KeyboardInterrupt:
                    raise
                except Exception as e:
                    self.error_streak += 1
                    self._last_error = f"{type(e).__name__}: {e}"
                    self.log.exception("主循环异常（连续第 %d 次）：%s", self.error_streak, e)
                    if self.error_streak >= 8:
                        self.log.critical("连续异常过多，主动退出以避免不可控状态")
                        self.notify.error(f"连续异常过多，已停机：{self._last_error}")
                        break
                wait = max(0.2, self.cfg.runtime.loop_interval_sec - (time.time() - t0))
                if self._stop_event.wait(wait):
                    break
        finally:
            self.stopping = True
            try:
                self.st.save()
            except Exception:
                pass

    def tick(self) -> None:
        self._loop += 1
        mid = self.feed.mid_price(self.coin)
        if isinstance(self.broker, PaperBroker):
            self.broker.feed_mid(mid)

        snap = self.broker.snapshot(self.coin)
        self._last_mid = mid
        self._last_snap = snap
        self._log_status(mid, snap)

        if self._risk_check(mid, snap):
            return

        self._refresh_candles()
        self._reconcile(snap)

        snap = self.broker.snapshot(self.coin)
        self._last_snap = snap

        slot = self.st.slots.get(SLOT_KEY)
        if slot is None:
            slot = self._new_slot()
            self.st.slots[SLOT_KEY] = slot

        if slot.get("state") == "open":
            self._manage_position(slot, mid)
        else:
            self._ensure_entry(slot, mid, snap)

        # 平掉最后一轮后立刻收尾。放在这里而不是只靠 _ensure_entry，
        # 否则「已完成」要等到下一轮循环（默认 5 秒后）才显示，网页状态滞后。
        if (
            not self.st.data.get("signal_done")
            and slot.get("state") == "flat"
            and self._rounds_exhausted()
        ):
            self._finish("已达轮数上限 %d 轮" % self.sg.max_rounds)

        self._record_equity(snap)
        self.st.save()

    # =================================================================
    #                           行情 / 指标
    # =================================================================
    def _refresh_candles(self, force: bool = False) -> None:
        try:
            bars = self.candles.closed(self.coin, limit=self.sg.candles, force=force)
        except Exception as e:
            self._warn_once("candles", "拉取 K 线失败，本轮跳过信号判断：%s", e)
            return

        if not bars:
            self._warn_once("candles_empty", "K 线接口返回空，本轮跳过信号判断")
            return

        state = ema_state(bars, self.sg.fast_ema, self.sg.slow_ema, self.sg.trend_ema)
        if state is None:
            self._warn_once(
                "warmup",
                "K 线只有 %d 根，不足 %d 根，指标还在预热",
                len(bars),
                max(self.sg.fast_ema, self.sg.slow_ema) + 1,
            )
            return

        self._last_kline_ms = bars[-1].t
        state["kline_ms"] = bars[-1].t
        closes = [float(c.c) for c in bars]
        fast = ema(closes, self.sg.fast_ema)
        slow = ema(closes, self.sg.slow_ema)
        # 明确用「已收盘 K 线的最后两根」判断交叉
        state["up_cross"] = cross_up(fast, slow, -1)
        state["down_cross"] = cross_down(fast, slow, -1)
        state["desc"] = describe_signal(state)

        a = atr(bars, self.sg.atr_period)
        self._atr_val = float(a[-1]) if a and a[-1] is not None else None
        self._sig = state
        self._warned.discard("candles")
        self._warned.discard("warmup")

    # =================================================================
    #                            状态打印
    # =================================================================
    def _phase_label(self) -> str:
        if self.st.data.get("signal_done"):
            return "已完成"
        slot = self.st.slots.get(SLOT_KEY)
        if slot is None:
            return "待启动"
        if slot.get("state") == "open":
            return "持仓中"
        if slot.get("state") == "pending":
            return "挂单中"
        return "等待信号"

    def _log_status(self, mid: float, snap: AccountSnapshot) -> None:
        stop = self._stop_px()
        extra = f"止损 {fmt_px(stop)}" if stop else "空仓"
        self.log.info(
            "行情 %s | 权益 $%s | 净仓 %.6f | 浮盈 %+.2f | 名义 $%s | 挂单 %d | %s：%s | %s | 已实现 %+.2f",
            fmt_px(mid),
            fmt_usd(snap.account_value),
            snap.net_position,
            snap.unrealized_pnl,
            fmt_usd(snap.total_notional),
            len(snap.open_oids),
            self.coin,
            self._phase_label(),
            extra,
            float(self.st.data.get("realized_usd", 0.0)),
        )

    # =================================================================
    #                            风控
    # =================================================================
    def _risk_check(self, mid: float, snap: AccountSnapshot) -> bool:
        r = self.cfg.risk

        if snap.account_value < r.min_account_value_usd:
            self._kill_switch(
                f"账户权益 ${snap.account_value:.2f} 低于下限 ${r.min_account_value_usd:.2f}", mid
            )
            return True

        if snap.unrealized_pnl <= -abs(r.max_unrealized_loss_usd):
            self._kill_switch(
                f"浮亏 ${snap.unrealized_pnl:.2f} 已触及熔断线 -${abs(r.max_unrealized_loss_usd):.2f}", mid
            )
            return True

        if snap.liquidation_px and abs(snap.liquidation_px - mid) / mid < 0.05:
            self.log.warning(
                "⚠️  强平价 %s 距现价仅 %.2f%%，请留意风险",
                fmt_px(snap.liquidation_px),
                abs(snap.liquidation_px - mid) / mid * 100,
            )
        return False

    def _kill_switch(self, reason: str, mid: float) -> None:
        self.log.critical("=" * 62)
        self.log.critical("🚨 触发熔断：%s", reason)
        self.log.critical("动作：撤销全部本 bot 挂单 → 市价平掉 %s 持仓", self.coin)
        try:
            n = self.broker.cancel_all_for_coin(self.coin)
            self.log.critical("已撤销 %d 笔挂单", n)
            res = self.broker.close_position(self.coin, self.cfg.market.close_slippage_pct)
            self.log.critical("平仓结果：%s", res)
        except Exception as e:
            self.log.critical("熔断处理过程中出错（请手工检查账户！）：%s", e)

        self.st.status = "STOPPED"
        self.st.data["stop_reason"] = reason
        self.st.stopped_until_ms = now_ms() + self.cfg.risk.cooldown_after_stop_sec * 1000
        self.st.slots.clear()
        self.st.save()
        self.notify.killed(self.coin, reason)
        self.log.critical("已写入停止状态，冷却 %d 秒。bot 现在退出，需要人工确认后重启。", self.cfg.risk.cooldown_after_stop_sec)
        self.log.critical("=" * 62)
        self.stopping = True
        self.stop_reason = f"熔断：{reason}"
        self._stop_event.set()

    # =================================================================
    #                        层位 / 对账
    # =================================================================
    def _new_slot(self) -> Dict[str, Any]:
        return {
            "key": SLOT_KEY,
            "side": "buy",
            "level": 1,
            "state": "flat",  # flat=空仓 pending=入场单挂着 open=持仓中
            "size": self._size(),
            "entry_cloid": None,
            "entry_oid": None,
            "entry_placed_ms": 0,
            "entry_filled_ms": 0,
            "entry_px": 0.0,
            "entry_fee": 0.0,
            "extreme_px": 0.0,
            "signal_kline_ms": 0,
            "tp_cloid": None,
            "tp_oid": None,
            "tp_placed_ms": 0,
            "tp_px": 0.0,
        }

    def _warn_once(self, key: str, msg: str, *args) -> None:
        if key in self._warned:
            return
        self._warned.add(key)
        self.log.warning(msg, *args)

    def _cleanup_orphans(self, snap: AccountSnapshot) -> None:
        tracked = set()
        for s in self.st.slots.values():
            for f in ("entry_oid", "tp_oid"):
                if s.get(f):
                    tracked.add(int(s[f]))
        orphans = []
        for o in self.broker.open_orders(self.coin):
            oid = int(o["oid"])
            if oid in tracked:
                continue
            cloid = o.get("cloid")
            if cloid and is_bot_cloid(str(cloid)):
                orphans.append(oid)
            else:
                self.log.warning("发现非本 bot 的挂单（oid=%s cloid=%s），不会触碰", oid, cloid)
        if orphans:
            self.log.warning("清理本 bot 遗留挂单 %d 笔：%s", len(orphans), orphans)
            self.broker.cancel_orders(self.coin, orphans)

    def _reconcile(self, snap: AccountSnapshot) -> None:
        slot = self.st.slots.get(SLOT_KEY)
        if slot is None:
            return
        if slot.get("state") == "pending" and slot.get("entry_oid"):
            oid = int(slot["entry_oid"])
            if oid not in snap.open_oids:
                since = int(slot.get("entry_placed_ms") or 0) - 120_000
                f = self.broker.poll_fills({oid}, since).get(oid)
                self._on_entry_resolved(slot, f)

    def _on_entry_resolved(self, slot: Dict[str, Any], f: Optional[FillInfo]) -> None:
        if f and f.sz > 0:
            planned = float(slot["size"])
            filled = min(f.sz, planned * 1.0000001)
            if f.sz < planned * 0.999:
                self.log.warning("入场单部分成交 %.6f/%.6f，未成交部分撤销", f.sz, planned)
            slot["state"] = "open"
            slot["size"] = filled
            slot["entry_px"] = f.px
            slot["entry_fee"] = float(f.fee or 0.0)
            slot["extreme_px"] = f.px
            slot["entry_oid"] = None
            slot["entry_cloid"] = None
            slot["entry_filled_ms"] = now_ms()
            self.st.add_trade("open", slot["side"], filled, f.px)
            self.log.info(
                "✅ 入场成交：%s %s %.6f @ %s（名义 $%s）",
                "买入" if slot["side"] == "buy" else "卖出",
                self.coin,
                filled,
                fmt_px(f.px),
                fmt_usd(filled * f.px),
            )
            self.notify.send(
                f"✅ 信号开仓 {self.coin} {'买入' if slot['side'] == 'buy' else '卖出'} "
                f"{filled} @ {fmt_px(f.px)}\n跟踪止损：{fmt_px(self._stop_px())}"
            )
        else:
            if isinstance(self.broker, PaperBroker):
                self.log.info("干跑重启：本地记录里还有一张未成交的挂单，重新挂出")
            else:
                self.log.warning("入场单从盘口消失且查不到成交记录（可能被外部撤销），稍后重挂")
            slot["entry_oid"] = None
            slot["entry_cloid"] = None
            slot["state"] = "flat"

    # =================================================================
    #                            入场
    # =================================================================
    def _rounds_exhausted(self) -> bool:
        if self.sg.max_rounds <= 0:
            return False
        return int(self.st.data.get("rounds", 0)) >= self.sg.max_rounds

    def _ensure_entry(self, slot: Dict[str, Any], mid: float, snap: AccountSnapshot) -> None:
        if self.st.data.get("signal_done"):
            return
        if self._rounds_exhausted():
            self._finish("已达轮数上限 %d 轮" % self.sg.max_rounds)
            return

        if slot.get("state") == "pending" and slot.get("entry_oid"):
            # 挂单超时未成交 → 撤掉，等下一轮信号（价格没回撤说明这单不该追）
            ttl = self.sg.entry_ttl_sec
            placed = int(slot.get("entry_placed_ms") or 0)
            if ttl > 0 and placed and (now_ms() - placed) > ttl * 1000:
                self.log.info("入场挂单 %s 秒未成交，撤掉等下一次信号", int(ttl))
                self.broker.cancel_orders(self.coin, [int(slot["entry_oid"])])
                slot["entry_oid"] = None
                slot["entry_cloid"] = None
                slot["state"] = "flat"
            return

        sig = self._sig
        if not sig:
            return

        side = ""
        if sig.get("up_cross") and sig.get("trend_ok_long", True):
            side = "buy"
        elif sig.get("down_cross") and self.sg.allow_short and sig.get("trend_ok_short", True):
            side = "sell"
        if not side:
            return

        # 同一根 K 线只做一次决策，避免同一信号反复下单
        kline_ms = int(sig.get("kline_ms") or self._last_kline_ms)
        if int(slot.get("signal_kline_ms") or 0) == kline_ms:
            return

        sz = self._size()
        px_ref = mid
        if snap.total_notional + sz * px_ref > self.cfg.risk.max_total_notional_usd + LEVEL_TOL:
            self._warn_once("notional", "名义持仓将超上限 $%s，已跳过本次信号", f"{self.cfg.risk.max_total_notional_usd:,.0f}")
            return
        if sz > self.cfg.risk.max_position_size + LEVEL_TOL:
            self._warn_once("pos", "下单量 %.6f 超过净持仓上限 %s，已跳过", sz, self.cfg.risk.max_position_size)
            return
        if snap.net_position != 0:
            self._warn_once("has_pos", "账户里已有 %s 持仓（%.6f），信号策略不叠加，跳过", self.coin, snap.net_position)
            return

        is_buy = side == "buy"
        counter = self.st.next_counter()
        if self.sg.is_maker:
            off = self.sg.entry_offset_pct / 100.0
            px = format_price(mid * (1 - off) if is_buy else mid * (1 + off), self.szd)
            res = self.broker.place_limit(self.coin, is_buy, sz, px, False, CLOID_TAG_ENTRY, counter)
        else:
            slip = self.cfg.market.close_slippage_pct / 100.0
            px = format_price(mid * (1 + slip) if is_buy else mid * (1 - slip), self.szd)
            res = self.broker.place_limit(self.coin, is_buy, sz, px, False, CLOID_TAG_ENTRY, counter, tif="Ioc")

        slot["entry_placed_ms"] = now_ms()
        slot["signal_kline_ms"] = kline_ms
        slot["side"] = side
        status = res.get("status")

        if status == "error":
            self._warn_once("entry_fail", "入场单下单失败：%s", res.get("error"))
            slot["signal_kline_ms"] = 0  # 允许下一轮重试
            return

        self.st.bump_daily(datetime.now().strftime("%Y-%m-%d"))
        self._warned.discard("entry_fail")

        if status == "resting":
            slot["state"] = "pending"
            slot["entry_oid"] = int(res["oid"])
            slot["entry_cloid"] = res.get("cloid")
            self.log.info(
                "📌 信号入场挂单：%s %s %.6f @ %s（现价 %s，等回撤）",
                "买" if is_buy else "卖",
                self.coin,
                sz,
                fmt_px(res.get("req_px") or px),
                fmt_px(mid),
            )
            return

        if status == "filled":
            self.log.info("信号入场立即成交（%s）", sig.get("desc"))
            f = FillInfo(
                oid=int(res.get("oid") or 0),
                sz=float(res.get("filled_sz") or sz),
                px=float(res.get("avg_px") or px),
            )
            self._on_entry_resolved(slot, f)

    # =================================================================
    #                            持仓管理
    # =================================================================
    def _manage_position(self, slot: Dict[str, Any], mid: float) -> None:
        is_long = slot.get("side") == "buy"
        entry = float(slot.get("entry_px") or 0)
        if entry <= 0:
            slot["state"] = "flat"
            return

        # 用已收盘 K 线的高低点更新极值，避免 5 秒轮询漏掉插针
        bars = self.candles.fetch(self.coin, limit=2)
        if bars:
            born = int(slot.get("entry_filled_ms") or 0)
            step = interval_ms(self.sg.interval)
            for c in bars:
                if c.t + step <= born:
                    continue  # 只看建仓那一刻之后的 K 线
                if is_long:
                    slot["extreme_px"] = max(float(slot.get("extreme_px") or entry), float(c.h))
                else:
                    slot["extreme_px"] = min(float(slot.get("extreme_px") or entry), float(c.l))
        slot["extreme_px"] = (
            max(float(slot.get("extreme_px") or entry), mid)
            if is_long
            else min(float(slot.get("extreme_px") or entry), mid)
        )

        stop = trail_stop_price(entry, float(slot.get("extreme_px") or entry), is_long, self.sg.trail_pct, self._atr_val, self.sg.atr_mult)

        # 1) 跟踪止损
        if stop > 0 and ((is_long and mid <= stop) or (not is_long and mid >= stop)):
            self._close_position(slot, mid, f"跟踪止损 {fmt_px(stop)}")
            return

        # 2) 固定止盈
        if self.sg.take_profit_pct > 0:
            tp = entry * (1 + self.sg.take_profit_pct / 100.0) if is_long else entry * (1 - self.sg.take_profit_pct / 100.0)
            if (is_long and mid >= tp) or (not is_long and mid <= tp):
                self._close_position(slot, mid, f"止盈 {self.sg.take_profit_pct}%")
                return

        # 3) 反向交叉
        sig = self._sig
        if self.sg.exit_on_cross and sig:
            kline_ms = int(sig.get("kline_ms") or self._last_kline_ms)
            if int(slot.get("signal_kline_ms") or 0) != kline_ms:
                opposite = bool(sig.get("down_cross")) if is_long else bool(sig.get("up_cross"))
                if opposite:
                    slot["signal_kline_ms"] = kline_ms
                    self._close_position(slot, mid, "反向交叉")
                    return

    def _close_position(self, slot: Dict[str, Any], mid: float, reason: str) -> bool:
        is_long = slot.get("side") == "buy"
        sz = float(slot.get("size") or 0)
        if sz <= 0:
            slot["state"] = "flat"
            return False

        slip = self.cfg.market.close_slippage_pct / 100.0
        # 反手平仓：多单用卖出平，空单用买入平；IOC 保证「要平就一定平掉」
        px = format_price(mid * (1 - slip) if is_long else mid * (1 + slip), self.szd)
        counter = self.st.next_counter()
        res = self.broker.place_limit(self.coin, not is_long, sz, px, True, CLOID_TAG_TP, counter, tif="Ioc")
        status = res.get("status")
        if status == "error":
            self._warn_once("close_fail", "平仓失败（下一轮重试）：%s", res.get("error"))
            return False

        self.st.bump_daily(datetime.now().strftime("%Y-%m-%d"))
        self._warned.discard("close_fail")

        oid = int(res.get("oid") or 0)
        exit_px = float(res.get("avg_px") or px)
        filled = float(res.get("filled_sz") or sz)
        f = None
        if oid:
            try:
                f = self.broker.poll_fills({oid}, now_ms() - 300_000).get(oid)
            except Exception:
                f = None
        if f and f.px:
            exit_px, filled = f.px, (f.sz or filled)

        entry = float(slot.get("entry_px") or 0)
        notional = entry * filled
        entry_fee = float(slot.get("entry_fee") or 0.0)
        if f and (f.closed_pnl or f.fee):
            # 交易所给的 closedPnl 只含本笔（平仓）的盈亏，开仓那笔的手续费要单独扣，
            # 否则每轮都会少算一次开仓成本，回测看着赚、实盘对不上账。
            pnl = float(f.closed_pnl) - float(f.fee) - entry_fee
            fee_note = float(f.fee or 0) + entry_fee
        else:
            # 拿不到成交明细时按价格差估算，并扣掉两边的估算手续费（宁可算亏一点）
            gross = (exit_px - entry) * filled * (1 if is_long else -1)
            entry_bps = FEE_MAKER_BPS if self.sg.is_maker else FEE_TAKER_BPS
            est_fee = notional * (entry_bps + FEE_TAKER_BPS) / 10000.0
            pnl = gross - est_fee
            fee_note = est_fee

        self.st.add_realized(pnl)
        self.st.data["rounds"] = int(self.st.data.get("rounds", 0)) + 1
        self.st.add_trade("close", "sell" if is_long else "buy", filled, exit_px, pnl)
        total = float(self.st.data.get("realized_usd", 0.0))
        self.log.info("=" * 62)
        self.log.info(
            "💰 平仓（%s）%s %.6f  %s → %s  净盈亏 %+.4f（累计 %+.2f，手续费 %.4f）",
            reason,
            self.coin,
            filled,
            fmt_px(entry),
            fmt_px(exit_px),
            pnl,
            total,
            float(fee_note or 0),
        )
        self.log.info("=" * 62)
        self.notify.send(
            f"💰 信号平仓（{reason}）\n{self.coin} {filled}  {fmt_px(entry)} → {fmt_px(exit_px)}\n"
            f"本笔 {pnl:+.4f}　累计 {total:+.2f}"
        )
        slot["state"] = "flat"
        slot["size"] = self._size()
        slot["entry_px"] = 0.0
        slot["entry_fee"] = 0.0
        slot["extreme_px"] = 0.0
        slot["entry_oid"] = None
        slot["entry_cloid"] = None
        slot["tp_oid"] = None
        slot["tp_cloid"] = None
        slot["tp_px"] = 0.0
        return True

    def _finish(self, reason: str) -> None:
        self.st.data["signal_done"] = True
        self.st.data["signal_done_reason"] = reason
        self.st.status = "DONE"
        self.st.save()
        self.log.critical("=" * 62)
        self.log.critical("✅ 信号策略结束：%s", reason)
        self.log.critical("实例会继续运行以便实时查看持仓与浮盈；确认无误后可手动「停止」。")
        self.log.critical("=" * 62)
        self.notify.send(f"✅ 信号策略结束\n{self.coin}\n{reason}")
