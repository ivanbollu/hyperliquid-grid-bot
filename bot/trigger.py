"""触发单（条件单）策略引擎。

和网格的区别
------------
网格是在价格区间里铺很多层，反复吃差价；
触发单只做一件事：**价格到达指定位置时下这一单**，适合做单次建仓。

两种触发方式（trigger.order_type）
----------------------------------
  * limit  —— 直接把限价单挂在盘口。价格跌到（买）/ 涨到（卖）该价位自然成交。
              最直观，而且通常是 maker 成交，手续费最低。
  * market —— 不挂单，只盯行情。中间价穿越触发价后立刻下 IOC 市价单成交。

成交之后
--------
  * trigger.tp_pct > 0  -> 自动挂 reduce-only 限价止盈（复用网格止盈那套逻辑）
  * trigger.once = true -> 建仓（或止盈）完成后标记「已完成」，不再补挂；
                            但主循环继续跑，方便你在网页上实时看持仓和浮盈。

风控沿用 risk 段：名义上限、净持仓上限、权益下限；成交后浮亏熔断也会生效。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from .broker import AccountSnapshot, FillInfo, PaperBroker
from .config import Config
from .engine import EngineHalt
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

SLOT_KEY = "T1"  # 触发单只有一个层位，用固定键，方便复用网格那套状态结构
LEVEL_TOL = 1e-9


class TriggerEngine:
    """价格触发的单次下单引擎。接口与 GridEngine 保持一致。"""

    def __init__(self, cfg: Config, broker, feed, state: BotState, logger, notifier: Optional[Notifier] = None):
        self.cfg = cfg
        self.broker = broker
        self.feed = feed
        self.st = state
        self.log = logger
        self.notify = notifier or Notifier(cfg, logger, cfg.slot_name)
        self.slot = cfg.slot_name
        self.coin = cfg.market.coin
        self.tr = cfg.trigger
        self.stopping = False
        self.stop_reason = ""
        self.error_streak = 0
        self.szd = 4
        self._loop = 0
        self._stop_event = threading.Event()
        self._warned: set = set()

        # 权益曲线（供网页画图）
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

    def snapshot_dict(self) -> Dict[str, Any]:
        snap = self._last_snap
        levels = []
        s = self.st.slots.get(SLOT_KEY)
        if s:
            levels.append(
                {
                    "key": s.get("key", SLOT_KEY),
                    "side": s["side"],
                    "level": s.get("level", 1),
                    "state": s["state"],
                    "size": s["size"],
                    "entry_px": s.get("entry_px") or None,
                    "tp_px": s.get("tp_px") or None,
                    "has_order": bool(s.get("entry_oid")),
                }
            )
        return {
            "slot": self.slot,
            "coin": self.coin,
            "running": not self.stopping,
            "loop": self._loop,
            "uptime_sec": round((now_ms() - self._started_ms) / 1000.0, 1) if self._started_ms else 0,
            "mid": self._last_mid,
            "network": self.cfg.env.network,
            "dry_run": self.cfg.env.dry_run,
            "mode": "trigger-buy" if self.tr.is_buy else "trigger-sell",
            "strategy": "trigger",
            "trigger": {
                "side": self.tr.side,
                "price": self.tr.price,
                "size": self.tr.size,
                "order_type": self.tr.order_type,
                "once": self.tr.once,
                "tp_pct": self.tr.tp_pct,
                "notional": round(self.tr.size * self.tr.price, 6),
                "distance_pct": self._distance_pct(),
                "phase": self._phase_label(),
            },
            "leverage": self.cfg.market.leverage,
            "anchor_price": self.tr.price,
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

        self._check_plan()

        info = self.broker.verify_connection()
        self.log.info("钱包地址   : %s", info.get("wallet"))
        self.log.info("交易账户   : %s%s", info.get("account"), "  (使用 API 钱包)" if info.get("agent_wallet") else "")
        self.log.info("网络       : %s", info.get("network"))
        self.log.info("账户模式   : %s", info.get("abstraction") or "default（传统账户）")
        breakdown = ""
        if info.get("spot_usdc"):
            breakdown = (
                f"  (永续 ${fmt_usd(float(info.get('perp_account_value') or 0))}"
                f" + 现货 USDC ${fmt_usd(float(info.get('spot_usdc') or 0))})"
            )
        self.log.info("账户权益   : $%s%s", fmt_usd(float(info.get("account_value") or 0)), breakdown)
        self.log.info("现有挂单   : %s 笔", info.get("open_orders"))

        # 权益下限必须在「挂单之前」拦下来：主循环的熔断发生在成交之后，
        # 否则就是「先买入、几秒后又被市价强平」，白亏手续费和价差。
        problem = equity_floor_problem(self.cfg, info)
        if problem:
            raise EngineHalt(problem)

        self.broker.set_leverage(self.coin, self.cfg.market.leverage, self.cfg.market.is_cross)

        mid = self.feed.mid_price(self.coin)
        if isinstance(self.broker, PaperBroker):
            self.broker.feed_mid(mid)
            if isinstance(info.get("account_value"), (int, float)) and info["account_value"] > 0:
                self.broker.balance = float(info["account_value"])

        self.st.data["trigger_price"] = self.tr.price
        self.st.data["trigger_side"] = self.tr.side
        self.st.status = "RUNNING"
        self.st.save()

        self.log.info("-" * 62)
        self.log.info("%s", self.cfg.summary())
        self.log.info("当前%s中间价 : %s", self.coin, fmt_px(mid))
        self.log.info("触发单        : %s %s %.6f @ %s", "买入" if self.tr.is_buy else "卖出", self.coin, self.tr.size, fmt_px(self.tr.price))
        if self.tr.order_type == "limit":
            self.log.info("挂单方式      : 限价单挂在盘口，价格到达 %s 即成交", fmt_px(self.tr.price))
        else:
            self.log.info("挂单方式      : 价格穿越 %s 后立即市价成交", fmt_px(self.tr.price))
        self.log.info("-" * 62)

        snap = self.broker.snapshot(self.coin)
        self._cleanup_orphans(snap)
        self._ensure_entry(mid, snap)
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

    def _check_plan(self) -> None:
        """启动前把明显会被交易所拒单的参数拦在这里，别等到挂在盘口才发现。"""
        from .config import MIN_ORDER_NOTIONAL_USD

        notional = self.tr.size * self.tr.price
        if notional < MIN_ORDER_NOTIONAL_USD - 1e-9:
            raise EngineHalt(
                f"下单量太小：{self.tr.size} × {self.tr.price} = ${notional:,.2f}，"
                f"低于 Hyperliquid 永续合约最低下单金额 ${MIN_ORDER_NOTIONAL_USD:,.0f}。\n"
                f"请把 trigger.size 提到 {MIN_ORDER_NOTIONAL_USD / self.tr.price:.4f} 以上。"
            )
        if self.tr.size > self.cfg.risk.max_position_size + LEVEL_TOL:
            raise EngineHalt(
                f"下单量 {self.tr.size} 超过净持仓上限 {self.cfg.risk.max_position_size}，请先调高 risk.max_position_size"
            )
        if notional > self.cfg.risk.max_total_notional_usd + LEVEL_TOL:
            raise EngineHalt(
                f"名义价值 ${notional:,.2f} 超过名义持仓上限 ${self.cfg.risk.max_total_notional_usd:,.0f}，请先调高 risk.max_total_notional_usd"
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

        self._reconcile(snap)

        snap = self.broker.snapshot(self.coin)
        self._last_snap = snap
        self._ensure_entry(mid, snap)
        slot = self.st.slots.get(SLOT_KEY)
        if slot is not None and slot["state"] == "open" and not slot.get("tp_oid") and self.tr.tp_pct > 0:
            self._place_tp(slot)
        self._record_equity(snap)
        self.st.save()

    # =================================================================
    #                            状态打印
    # =================================================================
    def _phase_label(self) -> str:
        if self.st.data.get("trigger_done"):
            return "已完成"
        slot = self.st.slots.get(SLOT_KEY)
        if slot is None:
            return "待挂单"
        if slot["state"] == "open":
            return "已成交·持仓中"
        if slot.get("entry_oid"):
            return "挂单中"
        return "等待触发"

    def _log_status(self, mid: float, snap: AccountSnapshot) -> None:
        self.log.info(
            "行情 %s | 权益 $%s | 净仓 %.6f | 浮盈 %+.2f | 名义 $%s | 交易所挂单 %d | 触发单：%s",
            fmt_px(mid),
            fmt_usd(snap.account_value),
            snap.net_position,
            snap.unrealized_pnl,
            fmt_usd(snap.total_notional),
            len(snap.open_oids),
            self._phase_label(),
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
        self.log.critical("已写入停止状态，冷却 %d 秒。", self.cfg.risk.cooldown_after_stop_sec)
        self.log.critical("=" * 62)
        self.stopping = True
        self.stop_reason = f"熔断：{reason}"
        self._stop_event.set()

    # =================================================================
    #                        触发单：层位与下单
    # =================================================================
    def _size(self) -> float:
        return format_size(self.tr.size, self.szd)

    def _distance_pct(self) -> float:
        """触发价相对当前中间价的偏离（正=需要涨到，负=需要跌到）。"""
        if not self._last_mid:
            return 0.0
        return round((self.tr.price - self._last_mid) / self._last_mid * 100.0, 4)

    def _condition_met(self, mid: float) -> bool:
        """market 模式用到价判断；limit 模式不需要（单子本来就挂在盘口）。"""
        return mid <= self.tr.price if self.tr.is_buy else mid >= self.tr.price

    def _new_slot(self) -> Dict[str, Any]:
        return {
            "key": SLOT_KEY,
            "side": self.tr.side,
            "level": 1,
            "state": "pending",  # pending=等成交  open=已建仓
            "size": self._size(),
            "entry_cloid": None,
            "entry_oid": None,
            "entry_placed_ms": 0,
            "entry_filled_ms": 0,
            "entry_px": 0.0,
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
        """清理本 bot 自己遗留、但状态里没记录的挂单；别人的单绝不碰。"""
        tracked = set()
        for s in self.st.slots.values():
            if s.get("entry_oid"):
                tracked.add(int(s["entry_oid"]))
            if s.get("tp_oid"):
                tracked.add(int(s["tp_oid"]))

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

    # ------------------------------------------------------------ 对账
    def _reconcile(self, snap: AccountSnapshot) -> None:
        slot = self.st.slots.get(SLOT_KEY)
        if slot is None:
            return

        if slot["state"] == "pending" and slot.get("entry_oid"):
            oid = int(slot["entry_oid"])
            if oid not in snap.open_oids:
                since = int(slot.get("entry_placed_ms") or 0) - 120_000
                f = self.broker.poll_fills({oid}, since).get(oid)
                self._on_entry_resolved(slot, f)
        elif slot["state"] == "open" and slot.get("tp_oid"):
            oid = int(slot["tp_oid"])
            if oid not in snap.open_oids:
                since = int(slot.get("tp_placed_ms") or 0) - 120_000
                f = self.broker.poll_fills({oid}, since).get(oid)
                self._on_tp_resolved(slot, f)

    def _on_entry_resolved(self, slot: Dict[str, Any], f: Optional[FillInfo]) -> None:
        if f and f.sz > 0:
            planned = float(slot["size"])
            filled = min(f.sz, planned * 1.0000001)
            if f.sz < planned * 0.999:
                self.log.warning("触发单部分成交 %.6f/%.6f，未成交部分不再补", f.sz, planned)
            slot["state"] = "open"
            slot["size"] = filled
            slot["entry_px"] = f.px
            slot["entry_oid"] = None
            slot["entry_cloid"] = None
            slot["entry_filled_ms"] = now_ms()
            self.st.add_trade("open", slot["side"], filled, f.px)
            self.log.info(
                "✅ 触发单成交：%s %s %.6f @ %s（名义 $%s）",
                "买入" if slot["side"] == "buy" else "卖出",
                self.coin,
                filled,
                fmt_px(f.px),
                fmt_usd(filled * f.px),
            )
            self.notify.send(
                f"✅ 触发单成交\n{self.coin} {'买入' if slot['side'] == 'buy' else '卖出'} "
                f"{filled} @ {fmt_px(f.px)}\n名义：${fmt_usd(filled * f.px)}"
            )
            self._after_entry(slot)
        else:
            if isinstance(self.broker, PaperBroker):
                # 干跑时撮合是内存态，重启后自然查不到旧挂单，属正常恢复，不必告警
                self.log.info("干跑重启：本地记录里还有一张未成交的触发单，重新挂出")
            else:
                self.log.warning("触发单从盘口消失且查不到成交记录（可能被外部撤销），稍后重新挂出")
            slot["entry_oid"] = None
            slot["entry_cloid"] = None

    def _after_entry(self, slot: Dict[str, Any]) -> None:
        if self.tr.tp_pct > 0:
            self._place_tp(slot)
            return
        self._finish_round("触发单已成交（未设止盈）")

    def _on_tp_resolved(self, slot: Dict[str, Any], f: Optional[FillInfo]) -> None:
        if f and f.sz > 0:
            pnl = f.closed_pnl - f.fee
            self.st.add_realized(pnl)
            self.st.add_trade("close", "sell" if slot["side"] == "buy" else "buy", f.sz, f.px, pnl)
            total = float(self.st.data.get("realized_usd", 0.0))
            remaining = slot["size"] - f.sz
            self.log.info(
                "💰 止盈成交 %.6f @ %s  净盈亏 %+.4f（累计 %+.2f）",
                f.sz,
                fmt_px(f.px),
                pnl,
                total,
            )
            self.notify.send(f"💰 止盈成交 {self.coin} {f.sz} @ {fmt_px(f.px)}\n本笔 {pnl:+.4f}　累计 {total:+.2f}")
            if remaining > slot["size"] * 0.001:
                slot["size"] = remaining
                slot["tp_oid"] = None
                slot["tp_cloid"] = None
                self.log.warning("止盈部分成交，剩余 %.6f 重新挂止盈", remaining)
            else:
                self._finish_round("止盈成交，触发单完成")
        else:
            self.log.warning("止盈单消失且无成交记录（可能被外部撤销），稍后重挂")
            slot["tp_oid"] = None
            slot["tp_cloid"] = None

    def _finish_round(self, reason: str) -> None:
        if not self.tr.once:
            self.log.info("本轮结束（%s），按配置重建触发单", reason)
            self._reset_slot()
            return
        self.st.data["trigger_done"] = True
        self.st.data["trigger_done_reason"] = reason
        self.st.status = "DONE"
        self.st.save()
        self.log.critical("=" * 62)
        self.log.critical("✅ %s —— 触发单任务完成。", reason)
        self.log.critical("实例会继续运行以便实时查看持仓与浮盈；确认持仓无误后可手动「停止」。")
        self.log.critical("=" * 62)
        self.notify.send(f"✅ 触发单完成\n{self.coin}\n{reason}\n实例仍在运行，用于监控持仓")

    def _reset_slot(self) -> None:
        self.st.slots[SLOT_KEY] = self._new_slot()
        self.st.data.pop("trigger_done", None)

    # =================================================================
    #                            挂单
    # =================================================================
    def _ensure_entry(self, mid: float, snap: AccountSnapshot) -> None:
        if self.st.data.get("trigger_done"):
            return
        slot = self.st.slots.get(SLOT_KEY)
        if slot is None:
            slot = self._new_slot()
            self.st.slots[SLOT_KEY] = slot
        if slot["state"] == "open" or slot.get("entry_oid"):
            return

        sz = self._size()
        if sz <= 0:
            self._warn_once("sz0", "下单量规整后为 0（trigger.size 精度不足），无法下单")
            return

        px = float(self.tr.price)
        if sz > self.cfg.risk.max_position_size + LEVEL_TOL:
            self._warn_once("pos", "下单量 %.6f 超过净持仓上限 %s，已跳过", sz, self.cfg.risk.max_position_size)
            return
        if snap.total_notional + sz * px > self.cfg.risk.max_total_notional_usd + LEVEL_TOL:
            self._warn_once(
                "notional",
                "名义持仓将超上限 $%s，已跳过",
                f"{self.cfg.risk.max_total_notional_usd:,.0f}",
            )
            return

        if self.tr.order_type == "market":
            if not self._condition_met(mid):
                return  # 还没到价，继续等
            slip = self.tr.max_slippage_pct / 100.0
            cross = mid * (1 + slip) if self.tr.is_buy else mid * (1 - slip)
            res = self.broker.place_limit(
                self.coin, self.tr.is_buy, sz, cross, False, CLOID_TAG_ENTRY, self.st.next_counter(), tif="Ioc"
            )
        else:
            res = self.broker.place_limit(
                self.coin, self.tr.is_buy, sz, px, False, CLOID_TAG_ENTRY, self.st.next_counter()
            )

        slot["entry_placed_ms"] = now_ms()
        status = res.get("status")

        if status == "error":
            self._warn_once("place_fail", "触发单下单失败：%s", res.get("error"))
            return

        self.st.bump_daily(datetime.now().strftime("%Y-%m-%d"))
        self._warned.discard("place_fail")

        if status == "resting":
            slot["entry_oid"] = int(res["oid"])
            slot["entry_cloid"] = res.get("cloid")
            self.log.info(
                "📌 触发单已挂出：%s %s %.6f @ %s（现价 %s，等价格到达）",
                "买" if self.tr.is_buy else "卖",
                self.coin,
                sz,
                fmt_px(res.get("req_px") or px),
                fmt_px(mid),
            )
            return

        if status == "filled":
            self.log.info("触发单挂出即成交（价格已穿越触发价）")
            f = FillInfo(
                oid=int(res.get("oid") or 0),
                sz=float(res.get("filled_sz") or sz),
                px=float(res.get("avg_px") or px),
            )
            self._on_entry_resolved(slot, f)

    def _place_tp(self, slot: Dict[str, Any]) -> bool:
        entry = float(slot.get("entry_px") or 0)
        if entry <= 0:
            return False
        tp = self.tr.tp_pct / 100.0
        is_buy = slot["side"] == "sell"  # 空头用买入止盈
        px = entry * (1 - tp) if is_buy else entry * (1 + tp)
        px = format_price(px, self.szd)
        res = self.broker.place_limit(
            self.coin, is_buy, float(slot["size"]), px, self.cfg.market.reduce_only_tp, CLOID_TAG_TP, self.st.next_counter()
        )
        status = res.get("status")
        if status == "error":
            self._warn_once("tp_fail", "止盈单下单失败：%s", res.get("error"))
            return False

        self.st.bump_daily(datetime.now().strftime("%Y-%m-%d"))
        slot["tp_placed_ms"] = now_ms()

        if status == "resting":
            slot["tp_oid"] = int(res["oid"])
            slot["tp_cloid"] = res.get("cloid")
            slot["tp_px"] = float(res.get("req_px") or px)
            self.log.info("🎯 挂止盈单 %s %.6f @ %s", "买" if is_buy else "卖", float(slot["size"]), fmt_px(slot["tp_px"]))
            return True

        if status == "filled":
            f = FillInfo(
                oid=int(res.get("oid") or 0),
                sz=float(res.get("filled_sz") or slot["size"]),
                px=float(res.get("avg_px") or px),
            )
            self._on_tp_resolved(slot, f)
            return True
        return False
