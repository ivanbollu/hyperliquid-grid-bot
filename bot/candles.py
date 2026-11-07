"""K 线数据源。

信号策略（趋势跟随 / 均值回归）都需要历史 K 线算指标。这里直接打
Hyperliquid 的 `candleSnapshot` 接口，不走 SDK —— 原因有两个：
  * SDK 的 `Info` 会顺带建立 websocket 连接，信号策略只要 REST 快照；
  * 回测脚本要在没有私钥、甚至不装 SDK 的环境里也能跑。

数据源是公开只读接口，不需要私钥。

注意：K 线接口单次返回有上限，拉长周期要**分页续拉**（见 fetch 的循环）。
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

# Hyperliquid 支持的 K 线周期
VALID_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d")

# 接口单次返回的硬上限（实测：请求 9000 根也只回 5000 根，且总是"最近的 5000 根"）。
# 所以**不能靠分页往更早翻**：5m 只有约 17.5 天历史，15m 约 30 天，1h 约 60 天。
# 回测想要更长样本，就把周期调大（15m/1h），而不是死磕 5m。
MAX_BARS_PER_REQUEST = 5000

_INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def interval_ms(interval: str) -> int:
    """周期的毫秒数。"""
    key = str(interval).strip().lower()
    if key not in _INTERVAL_MS:
        raise ValueError(f"不支持的 K 线周期 {interval!r}，可选：{'/'.join(VALID_INTERVALS)}")
    return _INTERVAL_MS[key]


def max_history_days(interval: str) -> float:
    """该周期理论上能取到多少天历史（受单次返回上限约束）。"""
    return MAX_BARS_PER_REQUEST * interval_ms(interval) / 86_400_000.0


@dataclass
class Candle:
    t: int  # 开盘时间（毫秒）
    o: float
    h: float
    l: float
    c: float
    v: float = 0.0
    n: int = 0  # 成交笔数
    closed: bool = False  # 是否已收盘（最后一根通常是正在形成的）

    def as_dict(self) -> Dict[str, Any]:
        return {
            "t": self.t,
            "o": self.o,
            "h": self.h,
            "l": self.l,
            "c": self.c,
            "v": self.v,
            "n": self.n,
            "closed": self.closed,
        }


def _parse(raw: Dict[str, Any], interval: str, now: int) -> Optional[Candle]:
    try:
        t = int(raw["t"])
        c = Candle(
            t=t,
            o=float(raw["o"]),
            h=float(raw["h"]),
            l=float(raw["l"]),
            c=float(raw["c"]),
            v=float(raw.get("v") or 0),
            n=int(raw.get("n") or 0),
        )
    except (KeyError, TypeError, ValueError):
        return None
    # 开盘时间 + 周期 <= 现在 => 这根已经收盘，不会再有变化
    c.closed = t + interval_ms(interval) <= now
    return c


class CandleFeed:
    """带缓存的只读 K 线源。

    缓存有两层意义：
      * 省接口调用：主循环 5 秒一次，而 5 分钟 K 线 5 分钟内不会变；
      * 保证同一轮里多次取用拿到的是**同一份**数据，指标不会前后打架。
    """

    def __init__(self, base_url: str, interval: str = "5m", limit: int = 300, cache_sec: float = 15.0):
        self.base_url = str(base_url).rstrip("/")
        self.interval = str(interval).strip().lower()
        interval_ms(self.interval)  # 提前校验，配错了要在启动时就报
        self.limit = max(20, int(limit))
        self.cache_sec = max(0.0, float(cache_sec))

        self._cache: Dict[str, List[Candle]] = {}
        self._cache_ms: Dict[str, float] = {}
        self._warned = ""

    # ------------------------------------------------------------ 底层请求
    def _post(self, payload: Dict[str, Any], timeout: float = 20.0) -> Any:
        req = urllib.request.Request(
            f"{self.base_url}/info",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())

    def _fetch_range(self, coin: str, start_ms: int, end_ms: int) -> List[Candle]:
        raw = self._post(
            {
                "type": "candleSnapshot",
                "req": {
                    "coin": coin,
                    "interval": self.interval,
                    "startTime": int(start_ms),
                    "endTime": int(end_ms),
                },
            }
        )
        if not isinstance(raw, list):
            return []
        now = int(time.time() * 1000)
        out = []
        for item in raw:
            if isinstance(item, dict):
                c = _parse(item, self.interval, now)
                if c is not None:
                    out.append(c)
        return out

    @property
    def last_error(self) -> str:
        return self._warned

    # ------------------------------------------------------------ 对外
    def fetch(self, coin: str, limit: int = 0, force: bool = False) -> List[Candle]:
        """取最近 N 根 K 线（含正在形成的那根）。

        接口单次最多给 `MAX_BARS_PER_REQUEST` 根，且只保留最近的那一段历史
        （实测：5m 约 17.5 天、15m 约 30 天、1h 约 60 天，再往前的窗口直接返回空）。
        因此这里**不做分页**——翻页翻不回来，只会白打接口；
        要更长样本请改用更大的周期。
        """
        coin = str(coin).upper()
        want = min(int(limit or self.limit), MAX_BARS_PER_REQUEST)
        now_ms = int(time.time() * 1000)

        cached = self._cache.get(coin) or []
        fresh = (
            cached
            and not force
            and len(cached) >= want
            and (time.time() - self._cache_ms.get(coin, 0.0)) < self.cache_sec
        )
        if fresh:
            return cached[-want:]

        step = interval_ms(self.interval)
        try:
            page = self._fetch_range(coin, now_ms - step * want, now_ms)
        except Exception as e:
            self._warned = f"{type(e).__name__}: {e}"
            if cached:
                # 网络抖动时用旧数据顶着，总比让主循环崩掉好
                return cached[-want:]
            raise

        page = [c for c in page if c.t <= now_ms]
        if not page:
            self._warned = "接口没有返回任何 K 线"
            return cached[-want:] if cached else []

        self._warned = ""
        page.sort(key=lambda c: c.t)
        if len(page) >= len(cached):
            self._cache[coin] = page
            self._cache_ms[coin] = time.time()
        return page[-want:]

    def closed(self, coin: str, limit: int = 0, force: bool = False) -> List[Candle]:
        """只要**已收盘**的 K 线。

        指标必须算在收盘价上：正在形成的那根随时会变，拿它算指标会在同一根
        K 线里反复出信号（俗称「信号重绘」），回测看着很美，实盘会来回挨打。
        """
        bars = self.fetch(coin, limit=limit, force=force)
        return [c for c in bars if c.closed]

    def last_price(self, coin: str) -> float:
        bars = self.fetch(coin, limit=2)
        return bars[-1].c if bars else 0.0

    def clear_cache(self) -> None:
        self._cache.clear()
        self._cache_ms.clear()
