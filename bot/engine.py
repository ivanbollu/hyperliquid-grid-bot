"""网格 + 马丁 策略引擎。

网格思路
--------
以锚定价 A 为中心，向下（long）/ 向上（short）铺 `levels` 层限价单：

    long  : 第 i 层买入价 = A * (1 - step%*i)
    每层成交后 -> 立刻在该层成交价上方挂 reduce-only 止盈单
    止盈成交   -> 该层回到"待挂单"状态，重新在原始层位挂买入单（滚动收割）

马丁
----
第 i 层下单量 = base_size * multiplier^(i-1)，并受 max_size_multiple 硬上限约束。
越跌越买、越买越多，能把持仓成本迅速摊低，但也是爆仓风险的主要来源，
所以必须配合 risk 段的风控参数使用。
"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .broker import AccountSnapshot, BrokerError, FillInfo, PaperBroker
from .config import Config
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

LEVEL_TOL = 1e-9


def _slot_sort_key(key: str) -> Tuple[str, int]:
    """B1 / S10 这样的层位键排序：先按方向，再按层号。"""
    try:
        return (key[0], int(key[1:]))
    except (IndexError, ValueError):
        return (key[:1], 0)


class EngineHalt(RuntimeError):
    """启动前置条件不满足，应当拒绝启动（例如熔断冷却期内）。"""


class GridEngine:
    def __init__(self, cfg: Config, broker, feed, state: BotState, logger, notifier: Optional[Notifier] = None):
        self.cfg = cfg
        self.broker = broker
        self.feed = feed
        self.st = state
        self.log = logger
        self.notify = notifier or Notifier(cfg, logger, cfg.slot_name)
        self.slot = cfg.slot_name
        self.coin = cfg.market.coin
        self.stopping = False
        self.stop_reason = ""
        self.error_streak = 0
        self.szd = 4
        self._loop = 0
        self._skip_warned: set = set()
        self._stop_event = threading.Event()

        # 权益曲线（供网页画图）；落盘到独立文件，重启不丢
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
        """请求停止。只置标志位，由主循环自己安全退出。"""
        self.stop_reason = reason
        self.stopping = True
        self._stop_event.set()

    def snapshot_dict(self) -> Dict[str, Any]:
        """给网页用的只读快照。只读引擎线程写好的完整对象，不做跨线程遍历。"""
        snap = self._last_snap
        levels = []
        for key in sorted(self.st.slots.keys(), key=_slot_sort_key):
            s = self.st.slots[key]
            levels.append(
                {
                    "key": key,
                    "side": s["side"],
                    "level": s["level"],
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
            "mode": self.cfg.grid.mode,
            "leverage": self.cfg.market.leverage,
            "anchor_price": self.st.data.get("anchor_price") or 0.0,
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

        # 熔断冷却检查
        if self.st.status == "STOPPED" and now_ms() < self.st.stopped_until_ms:
            remain = (self.st.stopped_until_ms - now_ms()) / 1000.0
            raise EngineHalt(
                f"处于熔断冷却中（原因：{self.st.data.get('stop_reason') or '未知'}），"
                f"还需等待 {remain / 60:.1f} 分钟。确认风险可控后可先「清除状态」。"
            )

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
        # 否则会出现「先买入、几秒后又被市价强平」，白亏手续费和价差。
        problem = equity_floor_problem(self.cfg, info)
        if problem:
            raise EngineHalt(problem)

        # 杠杆
        self.broker.set_leverage(self.coin, self.cfg.market.leverage, self.cfg.market.is_cross)

        mid = self.feed.mid_price(self.coin)
        if isinstance(self.broker, PaperBroker):
            self.broker.feed_mid(mid)
            if isinstance(info.get("account_value"), (int, float)) and info["account_value"] > 0:
                self.broker.balance = float(info["account_value"])

        # 锚定价
        anchor = float(self.cfg.grid.anchor_price or 0) or float(self.st.data.get("anchor_price") or 0) or mid
        self.st.data["anchor_price"] = anchor
        self.st.status = "RUNNING"
        self.st.save()

        self.log.info("-" * 62)
        self.log.info("%s", self.cfg.summary())
        self.log.info("当前%s中间价 : %s", self.coin, fmt_px(mid))
        self.log.info("网格锚定价 : %s", fmt_px(anchor))
        for key, side, px, sz, level in self._levels():
            tag = "买" if side == "buy" else "卖"
            self.log.info("  %-3s 第%d层  %s %-8s @ %-12s", key, level, tag, sz, fmt_px(px))
        self.log.info("-" * 62)

        snap = self.broker.snapshot(self.coin)
        self._cleanup_orphans(snap)
        self._ensure_orders(mid, snap)
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
        """前台运行：启动 + 主循环。"""
        self.start()
        self.loop()

    def loop(self) -> None:
        """主循环。可放到独立线程里跑（网页控制台就是这么用的）。"""
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
        self._maybe_reanchor(mid, snap)
        self._ensure_orders(mid, snap)
        self._record_equity(snap)
        self.st.save()

    # =================================================================
    #                            状态打印
    # =================================================================
    def _log_status(self, mid: float, snap: AccountSnapshot) -> None:
        placed = sum(
            1 for s in self.st.slots.values() if s["state"] == "pending" and s.get("entry_oid")
        )
        opened = sum(1 for s in self.st.slots.values() if s["state"] == "open")
        waiting = sum(
            1 for s in self.st.slots.values() if s["state"] == "pending" and not s.get("entry_oid")
        )
        self.log.info(
            "行情 %s | 权益 $%s | 净仓 %.6f | 浮盈 %+.2f | 名义 $%s | 交易所挂单 %d | 网格 挂单%d 持仓%d 待补%d | 已实现 %+.2f",
            fmt_px(mid),
            fmt_usd(snap.account_value),
            snap.net_position,
            snap.unrealized_pnl,
            fmt_usd(snap.total_notional),
            len(snap.open_oids),
            placed,
            opened,
            waiting,
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

        if snap.liquidation_px and self._dangerous_liquidation(mid, snap):
            self.log.warning(
                "⚠️  强平价 %s 距现价仅 %.2f%%，请留意风险",
                fmt_px(snap.liquidation_px),
                abs(snap.liquidation_px - mid) / mid * 100,
            )
        return False

    @staticmethod
    def _dangerous_liquidation(mid: float, snap: AccountSnapshot) -> bool:
        liq = snap.liquidation_px
        if not liq:
            return False
        return abs(liq - mid) / mid < 0.05

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
        self.log.critical(
            "已写入停止状态，冷却 %d 秒。bot 现在退出，需要人工确认后重启。",
            self.cfg.risk.cooldown_after_stop_sec,
        )
        self.log.critical("=" * 62)
        self.stopping = True
        self.stop_reason = f"熔断：{reason}"
        self._stop_event.set()

    # =================================================================
    #                          网格层位
    # =================================================================
    def _anchor(self) -> float:
        return float(self.st.data.get("anchor_price") or 0)

    def _size_for_level(self, level: int) -> float:
        base = self.cfg.market.base_size
        if self.cfg.martingale.enabled:
            mult = min(self.cfg.martingale.multiplier ** (level - 1), self.cfg.martingale.max_size_multiple)
            base *= mult
        return format_size(base, self.szd)

    def _level_price(self, side: str, level: int) -> float:
        a = self._anchor()
        step = self.cfg.grid.step_pct / 100.0
        px = a * (1 - step * level) if side == "buy" else a * (1 + step * level)
        return format_price(px, self.szd)

    def _levels(self) -> List[Tuple[str, str, float, float, int]]:
        out: List[Tuple[str, str, float, float, int]] = []
        for i in range(1, self.cfg.grid.levels + 1):
            sz = self._size_for_level(i)
            if self.cfg.grid.mode in ("long", "neutral"):
                out.append((f"B{i}", "buy", self._level_price("buy", i), sz, i))
            if self.cfg.grid.mode in ("short", "neutral"):
                out.append((f"S{i}", "sell", self._level_price("sell", i), sz, i))
        return out

    def _new_slot(self, key: str, side: str, level: int, size: float) -> Dict[str, Any]:
        return {
            "key": key,
            "side": side,
            "level": level,
            "state": "pending",  # pending=等建仓成交  open=已持仓等止盈
            "size": size,
            "entry_cloid": None,
            "entry_oid": None,
            "entry_placed_ms": 0,
            "entry_px": 0.0,
            "tp_cloid": None,
            "tp_oid": None,
            "tp_placed_ms": 0,
            "tp_px": 0.0,
        }

    # =================================================================
    #                      对账 / 清理 / 重锚
    # =================================================================
    def _cleanup_orphans(self, snap: AccountSnapshot) -> None:
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

    def _reconcile(self, snap: AccountSnapshot) -> None:
        gone: List[Tuple[str, Dict[str, Any], str]] = []
        for key, slot in self.st.slots.items():
            if slot["state"] == "pending" and slot.get("entry_oid"):
                if int(slot["entry_oid"]) not in snap.open_oids:
                    gone.append((key, slot, "entry"))
            elif slot["state"] == "open" and slot.get("tp_oid"):
                if int(slot["tp_oid"]) not in snap.open_oids:
                    gone.append((key, slot, "tp"))
        if not gone:
            return

        candidates = {
            int(s["entry_oid"] if k == "entry" else s["tp_oid"]) for _, s, k in gone
        }
        stamps = [
            int(s.get("entry_placed_ms") or 0) if k == "entry" else int(s.get("tp_placed_ms") or 0)
            for _, s, k in gone
        ]
        since = (min([t for t in stamps if t] or [now_ms()])) - 120_000
        fills = self.broker.poll_fills(candidates, since)

        for key, slot, kind in gone:
            oid = int(slot["entry_oid"] if kind == "entry" else slot["tp_oid"])
            f = fills.get(oid)
            if kind == "entry":
                self._on_entry_resolved(slot, f)
            else:
                self._on_tp_resolved(slot, f)

    def _on_entry_resolved(self, slot: Dict[str, Any], f: Optional[FillInfo]) -> None:
        key = slot["key"]
        if f and f.sz > 0:
            filled = min(f.sz, slot["size"] * 1.0000001)
            if f.sz < slot["size"] * 0.999:
                self.log.warning(
                    "%s 建仓单部分成交 %.6f/%.6f，未成交部分丢弃（下一轮网格会重新铺）", key, f.sz, slot["size"]
                )
            slot["state"] = "open"
            slot["size"] = filled
            slot["entry_px"] = f.px
            slot["entry_oid"] = None
            slot["entry_cloid"] = None
            slot["tp_oid"] = None
            slot["tp_cloid"] = None
            self.st.add_trade("open", slot["side"], filled, f.px)
            self.log.info("✅ %s 建仓成交 %.6f @ %s", key, filled, fmt_px(f.px))
        else:
            self.log.warning("%s 建仓单消失且无成交记录，判定为被外部撤销，稍后重挂", key)
            slot["entry_oid"] = None
            slot["entry_cloid"] = None

    def _on_tp_resolved(self, slot: Dict[str, Any], f: Optional[FillInfo]) -> None:
        key = slot["key"]
        if f and f.sz > 0:
            pnl = f.closed_pnl - f.fee
            self.st.add_realized(pnl)
            self.st.add_trade("close", "sell" if slot["side"] == "buy" else "buy", f.sz, f.px, pnl)
            total = float(self.st.data.get("realized_usd", 0.0))
            remaining = slot["size"] - f.sz
            self.log.info(
                "💰 %s 止盈成交 %.6f @ %s  净盈亏 %+.4f（累计 %+.2f）",
                key,
                f.sz,
                fmt_px(f.px),
                pnl,
                total,
            )
            self.notify.grid_closed(key, pnl, total)
            if remaining > slot["size"] * 0.001:
                slot["size"] = remaining
                slot["tp_oid"] = None
                slot["tp_cloid"] = None
                self.log.warning("%s 止盈部分成交，剩余 %.6f 重新挂止盈", key, remaining)
            else:
                self._reset_slot(slot)
        else:
            self.log.warning("%s 止盈单消失且无成交记录（可能被外部撤销），稍后重挂", key)
            slot["tp_oid"] = None
            slot["tp_cloid"] = None

    def _reset_slot(self, slot: Dict[str, Any]) -> None:
        """止盈完成后把该层复位，等待下一轮建仓。"""
        slot["state"] = "pending"
        slot["size"] = self._size_for_level(slot["level"])
        slot["entry_px"] = 0.0
        slot["entry_oid"] = None
        slot["entry_cloid"] = None
        slot["tp_oid"] = None
        slot["tp_cloid"] = None
        slot["tp_px"] = 0.0

    def _maybe_reanchor(self, mid: float, snap: AccountSnapshot) -> None:
        if not self.cfg.grid.reanchor:
            return
        a = self._anchor()
        if a <= 0:
            return
        dev = abs(mid - a) / a * 100.0
        if dev < self.cfg.grid.recenter_threshold_pct:
            return
        self.log.warning(
            "价格 %s 相对锚定价 %s 偏离 %.2f%%（阈值 %.2f%%），重新锚定网格",
            fmt_px(mid),
            fmt_px(a),
            dev,
            self.cfg.grid.recenter_threshold_pct,
        )
        self._cancel_pending_entries()
        self.st.data["anchor_price"] = mid
        self.st.save()

    def _cancel_pending_entries(self) -> int:
        oids, cleared = [], 0
        for slot in self.st.slots.values():
            if slot["state"] == "pending" and slot.get("entry_oid"):
                oids.append(int(slot["entry_oid"]))
                slot["entry_oid"] = None
                slot["entry_cloid"] = None
                cleared += 1
        if oids:
            self.broker.cancel_orders(self.coin, oids)
        return cleared

    # =================================================================
    #                            挂单
    # =================================================================
    def _ensure_orders(self, mid: float, snap: AccountSnapshot) -> None:
        r = self.cfg.risk
        day = datetime.now().strftime("%Y-%m-%d")
        budget = max(0, r.max_daily_orders - self.st.orders_today(day))
        skips: Dict[str, str] = {}
        if budget <= 0:
            skips["__daily__"] = f"今日下单量已达上限 {r.max_daily_orders}"

        levels = {lvl[0]: lvl for lvl in self._levels()}

        # 配置里已不存在的层（比如层数调小）且无挂单的，清掉
        for key in list(self.st.slots.keys()):
            s = self.st.slots[key]
            if key not in levels and s["state"] == "pending" and not s.get("entry_oid"):
                del self.st.slots[key]

        # 1) 优先补止盈（减仓，不消耗风控额度）
        for slot in self.st.slots.values():
            if slot["state"] == "open" and not slot.get("tp_oid"):
                self._place_tp(slot)

        # 2) 补齐 / 新建建仓单
        open_cnt = len(snap.open_oids)
        net = snap.net_position
        notional = snap.total_notional
        # 只统计【已在交易所真实挂着】的待成交单；本轮新挂的会在下面累加，
        # 否则同一笔会被算两次，导致风控误拦（重启后尤为明显）。
        pending_buy = sum(
            s["size"]
            for s in self.st.slots.values()
            if s["state"] == "pending" and s["side"] == "buy" and s.get("entry_oid")
        )
        pending_sell = sum(
            s["size"]
            for s in self.st.slots.values()
            if s["state"] == "pending" and s["side"] == "sell" and s.get("entry_oid")
        )

        for key, side, px, sz, level in self._levels():
            slot = self.st.slots.get(key)
            if slot is None:
                slot = self._new_slot(key, side, level, sz)
                self.st.slots[key] = slot
            if slot["state"] == "open" or slot.get("entry_oid"):
                continue
            if px <= 0:
                continue
            # 只挂在不越价的一侧，否则会立刻以 taker 成交
            if side == "buy" and px >= mid:
                continue
            if side == "sell" and px <= mid:
                continue
            if budget <= 0:
                skips[key] = f"今日下单已达上限 {r.max_daily_orders}"
                continue
            if open_cnt >= r.max_open_orders:
                skips["__max_orders__"] = f"挂单数已达上限 {r.max_open_orders}"
                break

            # 风控：持仓与名义价值
            if side == "buy":
                if net + pending_buy + sz > r.max_position_size + LEVEL_TOL:
                    skips[key] = f"多头持仓将超上限 {r.max_position_size}"
                    continue
                if notional + (pending_buy + sz) * px > r.max_total_notional_usd:
                    skips[key] = f"名义持仓将超上限 ${r.max_total_notional_usd:,.0f}"
                    continue
            else:
                if net - pending_sell - sz < -r.max_position_size - LEVEL_TOL:
                    skips[key] = f"空头持仓将超上限 {r.max_position_size}"
                    continue
                if notional + (pending_sell + sz) * px > r.max_total_notional_usd:
                    skips[key] = f"名义持仓将超上限 ${r.max_total_notional_usd:,.0f}"
                    continue

            if self._place_entry(slot):
                open_cnt += 1
                budget -= 1
                if side == "buy":
                    pending_buy += sz
                else:
                    pending_sell += sz

        # 同一层的跳过原因只提示一次，避免刷屏
        label = {"__max_orders__": "挂单数量", "__daily__": "今日下单上限"}
        for k, why in skips.items():
            if k not in self._skip_warned:
                self.log.warning("%s 受限，暂停开新仓：%s", label.get(k, k), why)
        self._skip_warned = set(skips.keys())

    def _place_entry(self, slot: Dict[str, Any]) -> bool:
        side = slot["side"]
        px = self._level_price(side, slot["level"])
        sz = slot["size"]
        counter = self.st.next_counter()
        res = self.broker.place_limit(
            self.coin, side == "buy", sz, px, False, CLOID_TAG_ENTRY, counter
        )
        slot["entry_placed_ms"] = now_ms()
        status = res.get("status")

        if status == "error":
            self.log.warning("%s 建仓单下单失败：%s", slot["key"], res.get("error"))
            return False

        self.st.bump_daily(datetime.now().strftime("%Y-%m-%d"))

        if status == "resting":
            slot["entry_oid"] = int(res["oid"])
            slot["entry_cloid"] = res.get("cloid")
            self.log.info(
                "📌 挂建仓单 %s  %s %.6f @ %s",
                slot["key"],
                "买" if side == "buy" else "卖",
                sz,
                fmt_px(res.get("req_px") or px),
            )
            return True

        if status == "filled":
            self.log.info("%s 建仓单挂出即成交（价格已穿越）", slot["key"])
            f = FillInfo(
                oid=int(res.get("oid") or 0),
                sz=float(res.get("filled_sz") or sz),
                px=float(res.get("avg_px") or px),
            )
            self._on_entry_resolved(slot, f)
            return True
        return False

    def _place_tp(self, slot: Dict[str, Any]) -> bool:
        entry = float(slot.get("entry_px") or 0)
        if entry <= 0:
            return False
        tp = self.cfg.grid.take_profit_pct / 100.0
        is_buy = slot["side"] == "sell"  # 空头网格用买入止盈
        px = entry * (1 - tp) if is_buy else entry * (1 + tp)
        counter = self.st.next_counter()
        res = self.broker.place_limit(
            self.coin, is_buy, float(slot["size"]), px, self.cfg.market.reduce_only_tp, CLOID_TAG_TP, counter
        )
        status = res.get("status")
        if status == "error":
            self.log.warning("%s 止盈单下单失败：%s", slot["key"], res.get("error"))
            return False

        self.st.bump_daily(datetime.now().strftime("%Y-%m-%d"))
        slot["tp_placed_ms"] = now_ms()

        if status == "resting":
            slot["tp_oid"] = int(res["oid"])
            slot["tp_cloid"] = res.get("cloid")
            slot["tp_px"] = float(res.get("req_px") or px)
            self.log.info(
                "🎯 挂止盈单 %s  %s %.6f @ %s",
                slot["key"],
                "买" if is_buy else "卖",
                float(slot["size"]),
                fmt_px(slot["tp_px"]),
            )
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
