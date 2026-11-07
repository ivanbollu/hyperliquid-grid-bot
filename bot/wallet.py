"""钱包账户只读查询。

网页上「连接钱包」拿到的只是一个 EVM 地址，这里用这个地址去 Hyperliquid
的公开 Info 接口查真实账户数据（权益、持仓、挂单、成交、手续费等级）。

安全边界（这一层是刻意做窄的）：
  * 只用地址查询公开数据，不碰私钥、不签名、不下任何单。
  * 地址只做格式校验，不做任何「可信」假设 —— 查谁都是一个结果。
  * 全部是只读 HTTP 请求，连 API 钱包都不需要。
"""

from __future__ import annotations

import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from hyperliquid.info import Info
from hyperliquid.utils import constants

_ADDR_RE = re.compile(r"^0[xX][0-9a-fA-F]{40}$")

_NETWORKS = {
    "mainnet": constants.MAINNET_API_URL,
    "testnet": constants.TESTNET_API_URL,
}

MAX_FILLS = 60


# =====================================================================
#                              基础工具
# =====================================================================
def normalize_address(addr: Any) -> str:
    """校验并归一化 EVM 地址。非法返回空串。

    Hyperliquid 内部一律用小写地址作索引，这里统一转小写，
    避免大小写不同导致同一账户被当成两个。
    """
    s = str(addr or "").strip()
    if not _ADDR_RE.match(s):
        return ""
    return "0x" + s[2:].lower()


def is_address(addr: Any) -> bool:
    return bool(normalize_address(addr))


def short_address(addr: Any) -> str:
    a = normalize_address(addr)
    return f"{a[:6]}…{a[-4:]}" if a else ""


def base_url(network: str) -> str:
    return _NETWORKS.get(str(network or "").strip().lower(), constants.MAINNET_API_URL)


def _f(v: Any, default: float = 0.0) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    if f != f or f in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return f


def _i(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _side(s: Any) -> str:
    """Hyperliquid 用 "B"/"A" 表示买/卖。"""
    return "buy" if str(s).upper() in ("B", "BUY") else "sell"


# =====================================================================
#                              查询器
# =====================================================================
class WalletReader:
    """只读账户查询器。

    * 每个网络缓存一个 Info 客户端（它的构造会拉一次 perp meta，比较贵）
    * 查询结果做秒级缓存，防止前端连点导致请求风暴
    """

    def __init__(self, cache_ttl: float = 3.0):
        self._lock = threading.Lock()
        self._infos: Dict[str, Info] = {}
        self._cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}
        self._ttl = float(cache_ttl)

    # ---------------------------------------------------------- 内部
    def _info(self, network: str) -> Info:
        url = base_url(network)
        with self._lock:
            info = self._infos.get(url)
            if info is None:
                info = Info(url, skip_ws=True)
                self._infos[url] = info
            return info

    # ---------------------------------------------------------- 主入口
    def fetch(self, address: Any, network: str = "mainnet", full: bool = True) -> Dict[str, Any]:
        addr = normalize_address(address)
        if not addr:
            raise ValueError("钱包地址格式不正确（应为 0x 开头的 40 位十六进制）")

        net = "testnet" if str(network or "").lower().startswith("test") else "mainnet"
        key = f"{net}:{addr}:{int(bool(full))}"
        now = time.time()
        with self._lock:
            hit = self._cache.get(key)
            if hit and now - hit[0] < self._ttl:
                return hit[1]

        info = self._info(net)
        errors: List[str] = []

        perp = self._perp(info, addr, errors)
        spot = self._spot(info, addr, errors)
        orders = self._orders(info, addr, errors)

        out: Dict[str, Any] = {
            "ok": True,
            "address": addr,
            "short": short_address(addr),
            "network": net,
            "ts": int(now * 1000),
            "perp": perp,
            "spot": spot,
            "open_orders": orders,
            "fills": [],
            "fees": {},
            "history": {},
            "errors": errors,
        }

        if full:
            out["fills"] = self._fills(info, addr, errors)
            out["fees"] = self._fees(info, addr, errors)
            out["history"] = self._history(info, addr, errors)

        with self._lock:
            self._cache[key] = (now, out)
            if len(self._cache) > 64:  # 简单清理，避免无限增长
                for k in sorted(self._cache, key=lambda x: self._cache[x][0])[:32]:
                    self._cache.pop(k, None)
        return out

    # ---------------------------------------------------------- 各分项
    @staticmethod
    def _perp(info: Info, addr: str, errors: List[str]) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "account_value": 0.0,
            "total_notional": 0.0,
            "margin_used": 0.0,
            "withdrawable": 0.0,
            "cross_account_value": 0.0,
            "cross_maintenance_margin": 0.0,
            "positions": [],
        }
        try:
            st = info.user_state(addr) or {}
        except Exception as e:
            errors.append(f"永续账户查询失败：{e}")
            return out

        ms = st.get("marginSummary") or {}
        cms = st.get("crossMarginSummary") or {}
        out.update(
            account_value=_f(ms.get("accountValue")),
            total_notional=_f(ms.get("totalNtlPos")),
            margin_used=_f(ms.get("totalMarginUsed")),
            withdrawable=_f(st.get("withdrawable")),
            cross_account_value=_f(cms.get("accountValue")),
            cross_maintenance_margin=_f(st.get("crossMaintenanceMarginUsed")),
        )

        for p in st.get("assetPositions") or []:
            pos = (p or {}).get("position") or {}
            lev = pos.get("leverage") or {}
            size = _f(pos.get("szi"))
            if size == 0:
                continue
            out["positions"].append(
                {
                    "coin": pos.get("coin"),
                    "size": size,
                    "entry_px": _f(pos.get("entryPx")),
                    "value": _f(pos.get("positionValue")),
                    "unrealized_pnl": _f(pos.get("unrealizedPnl")),
                    "roe": _f(pos.get("returnOnEquity")),
                    "liquidation_px": _f(pos.get("liquidationPx")),
                    "margin_used": _f(pos.get("marginUsed")),
                    "leverage": lev.get("value"),
                    "leverage_type": lev.get("type") or "",
                }
            )
        out["positions"].sort(key=lambda x: abs(x["value"]), reverse=True)
        return out

    @staticmethod
    def _spot(info: Info, addr: str, errors: List[str]) -> List[Dict[str, Any]]:
        try:
            ss = info.spot_user_state(addr) or {}
        except Exception as e:
            errors.append(f"现货账户查询失败：{e}")
            return []
        out = []
        for b in ss.get("balances") or []:
            total = _f((b or {}).get("total"))
            hold = _f((b or {}).get("hold"))
            if total == 0 and hold == 0:
                continue
            out.append(
                {
                    "coin": (b or {}).get("coin"),
                    "total": total,
                    "hold": hold,
                    "entry_ntl": _f((b or {}).get("entryNtl")),
                }
            )
        out.sort(key=lambda x: abs(x["total"]), reverse=True)
        return out

    @staticmethod
    def _orders(info: Info, addr: str, errors: List[str]) -> List[Dict[str, Any]]:
        try:
            raw = info.frontend_open_orders(addr) or []
        except Exception as e:
            errors.append(f"挂单查询失败：{e}")
            return []
        out = []
        for o in raw:
            o = o or {}
            out.append(
                {
                    "oid": _i(o.get("oid")),
                    "coin": o.get("coin"),
                    "side": _side(o.get("side")),
                    "size": _f(o.get("sz")),
                    "orig_size": _f(o.get("origSz")),
                    "price": _f(o.get("limitPx")),
                    "trigger_px": _f(o.get("triggerPx")),
                    "is_trigger": bool(o.get("isTrigger")),
                    "reduce_only": bool(o.get("reduceOnly")),
                    "order_type": o.get("orderType") or "",
                    "ts": _i(o.get("timestamp")),
                    "cloid": o.get("cloid"),
                }
            )
        out.sort(key=lambda x: x["ts"])
        return out

    @staticmethod
    def _fills(info: Info, addr: str, errors: List[str]) -> List[Dict[str, Any]]:
        try:
            raw = info.user_fills(addr) or []
        except Exception as e:
            errors.append(f"成交记录查询失败：{e}")
            return []
        raw = sorted(raw, key=lambda x: _i((x or {}).get("time")), reverse=True)[:MAX_FILLS]
        out = []
        for f in raw:
            f = f or {}
            out.append(
                {
                    "coin": f.get("coin"),
                    "side": _side(f.get("side")),
                    "size": _f(f.get("sz")),
                    "price": _f(f.get("px")),
                    "closed_pnl": _f(f.get("closedPnl")),
                    "fee": _f(f.get("fee")),
                    "dir": f.get("dir") or "",
                    "ts": _i(f.get("time")),
                    "crossed": bool(f.get("crossed")),
                    "liquidation": bool(f.get("liquidation")),
                    "hash": f.get("hash") or "",
                }
            )
        return out

    @staticmethod
    def _fees(info: Info, addr: str, errors: List[str]) -> Dict[str, Any]:
        try:
            raw = info.user_fees(addr) or {}
        except Exception as e:
            errors.append(f"手续费查询失败：{e}")
            return {}
        if not isinstance(raw, dict):
            return {}
        sched = raw.get("feeSchedule") or {}
        return {
            "cross_rate": _f(raw.get("userCrossRate") or sched.get("cross")),
            "add_rate": _f(raw.get("userAddRate") or sched.get("add")),
            "vip": raw.get("userVipClass") or raw.get("vipClass") or "",
            "referral_discount": _f(raw.get("activeReferralDiscount")),
            "builder_rebate": _f(raw.get("activeBuilderFeeDiscount") or raw.get("builderFeeDiscount")),
        }

    @staticmethod
    def _history(info: Info, addr: str, errors: List[str]) -> Dict[str, List[List[float]]]:
        """账户权益历史（官方 `portfolio` 接口），给抽屉里的迷你曲线用。"""
        try:
            raw = info.portfolio(addr) or []
        except Exception as e:
            errors.append(f"权益历史查询失败：{e}")
            return {}
        out: Dict[str, List[List[float]]] = {}
        for item in raw:
            if not (isinstance(item, (list, tuple)) and len(item) == 2):
                continue
            label, body = item[0], item[1] or {}
            pts = body.get("accountValueHistory") or []
            clean = []
            for pt in pts:
                if isinstance(pt, (list, tuple)) and len(pt) >= 2:
                    t = _i(pt[0])
                    v = _f(pt[1], float("nan"))
                    if t and v == v:
                        clean.append([t, v])
            if clean:
                out[str(label)] = clean
        return out


# 全局单例：server 层直接用
_reader: Optional[WalletReader] = None
_reader_lock = threading.Lock()


def get_reader() -> WalletReader:
    global _reader
    with _reader_lock:
        if _reader is None:
            _reader = WalletReader()
        return _reader
