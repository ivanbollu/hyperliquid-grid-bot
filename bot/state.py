"""状态持久化。

状态文件是 bot 的"记忆"：重启后靠它和交易所对账，
所以宁可写得频繁一点，也不能丢。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict

from .utils import now_ms

STATE_VERSION = 1


def _empty_state(coin: str) -> Dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "coin": coin,
        "status": "RUNNING",  # RUNNING | STOPPED
        "stopped_until_ms": 0,
        "stop_reason": "",
        "anchor_price": 0.0,
        "counter": 0,
        "realized_usd": 0.0,
        "closed_slots": 0,
        "day": "",
        "orders_today": 0,
        "slots": {},  # key -> slot dict
        "trades": [],  # 最近成交（滚动保留，供网页展示）
        "updated_ms": 0,
    }


class BotState:
    def __init__(self, path: Path, coin: str):
        self.path = Path(path)
        self.coin = coin
        self.data: Dict[str, Any] = _empty_state(coin)

    # ------------------------------------------------------------ 读写
    def load(self) -> "BotState":
        if self.path.exists():
            try:
                obj = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(obj, dict) and obj.get("version") == STATE_VERSION:
                    # 交易对变了就丢弃旧状态，避免串味
                    if obj.get("coin", self.coin) != self.coin:
                        raise ValueError("coin mismatch")
                    self.data = obj
                    return self
            except Exception:
                pass
            # 状态损坏：备份后重建，不去猜
            try:
                self.path.with_suffix(f".corrupt-{now_ms()}.json").write_text(
                    self.path.read_text(encoding="utf-8"), encoding="utf-8"
                )
            except Exception:
                pass
        self.data = _empty_state(self.coin)
        self.save()
        return self

    def save(self) -> None:
        self.data["updated_ms"] = now_ms()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def reset(self) -> None:
        self.data = _empty_state(self.coin)
        self.save()

    # ------------------------------------------------------------ 快捷存取
    @property
    def status(self) -> str:
        return self.data.get("status", "RUNNING")

    @status.setter
    def status(self, v: str) -> None:
        self.data["status"] = v

    @property
    def stopped_until_ms(self) -> int:
        return int(self.data.get("stopped_until_ms", 0) or 0)

    @stopped_until_ms.setter
    def stopped_until_ms(self, v: int) -> None:
        self.data["stopped_until_ms"] = int(v)

    @property
    def slots(self) -> Dict[str, Any]:
        return self.data.setdefault("slots", {})

    def next_counter(self) -> int:
        self.data["counter"] = int(self.data.get("counter", 0)) + 1
        return self.data["counter"]

    def add_realized(self, usd: float) -> None:
        self.data["realized_usd"] = float(self.data.get("realized_usd", 0.0)) + float(usd)
        self.data["closed_slots"] = int(self.data.get("closed_slots", 0)) + 1

    # ------------------------------------------------------------ 成交记录
    MAX_TRADES = 200

    def add_trade(self, kind: str, side: str, size: float, price: float, pnl: float = 0.0) -> None:
        """kind: open(建仓) | close(止盈平仓) | close_all(一键平仓)"""
        trades = self.data.setdefault("trades", [])
        trades.append(
            {
                "ts": now_ms(),
                "kind": kind,
                "side": side,
                "size": round(float(size), 10),
                "price": float(price),
                "pnl": round(float(pnl), 6),
            }
        )
        if len(trades) > self.MAX_TRADES:
            del trades[: len(trades) - self.MAX_TRADES]

    def trades(self) -> list:
        return list(self.data.get("trades", []))

    # ------------------------------------------------------------ 每日计数
    def bump_daily(self, day: str, n: int = 1) -> int:
        if self.data.get("day") != day:
            self.data["day"] = day
            self.data["orders_today"] = 0
        self.data["orders_today"] = int(self.data.get("orders_today", 0)) + n
        return self.data["orders_today"]

    def orders_today(self, day: str) -> int:
        if self.data.get("day") != day:
            return 0
        return int(self.data.get("orders_today", 0))
