"""交易所接入层。

上层策略只认 Broker 这套接口，不直接碰 SDK。
这样同一套策略既能跑真实盘（LiveBroker），也能跑干跑模拟（PaperBroker）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from hyperliquid.utils import constants
from hyperliquid.utils.types import Cloid

from .config import Config
from .utils import CLOID_TAG_ENTRY, CLOID_TAG_TP, format_price, format_size, make_cloid, now_ms


@dataclass
class AccountSnapshot:
    account_value: float = 0.0
    net_position: float = 0.0  # 有符号：正=多，负=空
    entry_px: Optional[float] = None
    unrealized_pnl: float = 0.0
    total_notional: float = 0.0
    margin_used: float = 0.0
    liquidation_px: Optional[float] = None
    open_oids: Set[int] = field(default_factory=set)


@dataclass
class FillInfo:
    oid: int
    sz: float = 0.0
    px: float = 0.0
    closed_pnl: float = 0.0
    fee: float = 0.0


class BrokerError(RuntimeError):
    pass


# =====================================================================
#                              真实盘
# =====================================================================
class LiveBroker:
    """封装 Hyperliquid Info / Exchange。"""

    def __init__(self, cfg: Config, logger):
        self.cfg = cfg
        self.log = logger
        self.base_url = cfg.env.base_url
        self.is_mainnet = cfg.env.is_mainnet

        if not cfg.private_key:
            raise BrokerError("缺少私钥：请在 .env 里设置 HL_PRIVATE_KEY")

        self.wallet = Account.from_key(cfg.private_key)

        # 使用 API 钱包时，交易归属主钱包；account_address 必须填主钱包地址
        self.account_address = cfg.env.account_address or self.wallet.address
        self.using_agent_wallet = bool(cfg.env.account_address) and (
            cfg.env.account_address.lower() != self.wallet.address.lower()
        )

        self.info = Info(self.base_url, skip_ws=True)
        self.exchange = Exchange(self.wallet, self.base_url, account_address=cfg.env.account_address or None)

        self.sz_decimals_cache: Dict[str, int] = {}
        # 账户模式缓存：unifiedAccount 下现货 USDC 直接充当永续保证金
        self._abstraction: Optional[str] = None

    # ------------------------------------------------------------ 基础信息
    def sz_decimals(self, coin: str) -> int:
        if coin not in self.sz_decimals_cache:
            asset = self.info.coin_to_asset[coin]
            self.sz_decimals_cache[coin] = int(self.info.asset_to_sz_decimals[asset])
        return self.sz_decimals_cache[coin]

    def mid_price(self, coin: str) -> float:
        mids = self.info.all_mids()
        if coin not in mids:
            raise BrokerError(f"行情里找不到 {coin}，请检查 market.coin 是否拼写正确")
        return float(mids[coin])

    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> None:
        resp = self.exchange.update_leverage(leverage, coin, is_cross)
        self._check_ok(resp, f"设置杠杆 {leverage}x")

    # ------------------------------------------------------------ 账户模式
    def account_abstraction(self) -> str:
        """Hyperliquid 账户模式（首次查询后缓存）。

        `unifiedAccount`（统一账户）下，现货里的 USDC 直接充当永续保证金；
        此时永续侧 `marginSummary.accountValue` 只反映"已被占用的那部分"，
        单独拿它做风控会把有资金的账户误判成"权益不足"，进而熔断强平。
        """
        if self._abstraction is None:
            try:
                self._abstraction = str(
                    self.info.query_user_abstraction_state(self.account_address) or ""
                )
            except Exception as e:  # 老账户 / 接口变动：按传统模式处理
                self.log.debug("查询账户模式失败，按传统模式处理：%s", e)
                self._abstraction = ""
        return self._abstraction

    def spot_usdc(self) -> float:
        """现货账户里的 USDC 余额（统一账户下即可用于永续的保证金）。"""
        try:
            ss = self.info.spot_user_state(self.account_address) or {}
        except Exception as e:
            self.log.warning("查询现货余额失败：%s", e)
            return 0.0
        return sum(
            float(b.get("total") or 0)
            for b in (ss.get("balances") or [])
            if b.get("coin") == "USDC"
        )

    def equity(self) -> float:
        """真实可用权益。

        统一账户（unifiedAccount）下现货 USDC **本身就是**永续保证金，两者是
        同一笔钱的两个视角：有仓位时 `accountValue` 与现货 USDC 完全相等，
        直接相加会把权益算成两倍；无仓位时 `accountValue` 会归零，此时只有
        现货余额是真的。所以取两者的较大值 —— 既不重复计算，也不漏算。

        （实测：持仓时 perp $18.9639 / spot $18.9639 / spot hold 100%，
          且全天没有任何现货→永续划转记录，证明确为同一笔钱。）
        """
        st = self.info.user_state(self.account_address)
        perp = float((st.get("marginSummary") or {}).get("accountValue") or 0)
        if self.account_abstraction() == "unifiedAccount":
            return max(perp, self.spot_usdc())
        return perp

    # ------------------------------------------------------------ 账户
    def snapshot(self, coin: str) -> AccountSnapshot:
        st = self.info.user_state(self.account_address)
        oo = self.info.open_orders(self.account_address)

        snap = AccountSnapshot()
        for p in st.get("assetPositions", []):
            pos = p.get("position", {})
            # 只认本策略的标的。手工开的仓 / 其它策略的仓不能混进本策略的风控，
            # 否则别的币浮亏会触发本策略熔断（把好仓一起平掉），
            # 别的币的名义也会顶爆 risk.max_total_notional_usd。
            if pos.get("coin") != coin:
                continue
            snap.net_position = float(pos.get("szi") or 0)
            snap.unrealized_pnl = float(pos.get("unrealizedPnl") or 0)
            snap.total_notional = abs(float(pos.get("positionValue") or 0))
            ep = pos.get("entryPx")
            snap.entry_px = float(ep) if ep not in (None, "") else None
            lp = pos.get("liquidationPx")
            snap.liquidation_px = float(lp) if lp not in (None, "") else None

        ms = st.get("marginSummary", {}) or {}
        snap.account_value = float(ms.get("accountValue") or 0)
        # 统一账户：现货 USDC 与永续 accountValue 是同一笔钱，取大者（详见 equity()）
        if self.account_abstraction() == "unifiedAccount":
            snap.account_value = max(snap.account_value, self.spot_usdc())
        snap.margin_used = float(ms.get("totalMarginUsed") or 0)
        snap.open_oids = {int(o["oid"]) for o in oo if o.get("coin") == coin}
        return snap

    def open_orders(self, coin: str) -> List[Dict[str, Any]]:
        return [o for o in self.info.open_orders(self.account_address) if o.get("coin") == coin]

    # ------------------------------------------------------------ 成交
    def poll_fills(self, candidate_oids: Set[int], since_ms: int) -> Dict[int, FillInfo]:
        """只查我们关心的那几个 oid 的成交，省额度。"""
        if not candidate_oids:
            return {}
        try:
            fills = self.info.user_fills_by_time(self.account_address, max(0, int(since_ms)))
        except Exception as e:  # 网络抖动不该让主循环挂掉
            self.log.warning("查询成交失败：%s", e)
            return {}

        agg: Dict[int, Dict[str, float]] = {}
        for f in fills or []:
            try:
                oid = int(f.get("oid") or 0)
            except (TypeError, ValueError):
                continue
            if oid not in candidate_oids:
                continue
            sz = float(f.get("sz") or 0)
            px = float(f.get("px") or 0)
            a = agg.setdefault(oid, {"sz": 0.0, "notional": 0.0, "pnl": 0.0, "fee": 0.0})
            a["sz"] += sz
            a["notional"] += sz * px
            a["pnl"] += float(f.get("closedPnl") or 0)
            a["fee"] += float(f.get("fee") or 0)

        out: Dict[int, FillInfo] = {}
        for oid, a in agg.items():
            avg = a["notional"] / a["sz"] if a["sz"] else 0.0
            out[oid] = FillInfo(oid=oid, sz=a["sz"], px=avg, closed_pnl=a["pnl"], fee=a["fee"])
        return out

    # ------------------------------------------------------------ 下单
    def place_limit(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        px: float,
        reduce_only: bool,
        tag: int,
        counter: int,
        tif: str = "Gtc",
    ) -> Dict[str, Any]:
        """返回 {"status": "resting"|"filled"|"error", "oid":..., "avg_px":..., "filled_sz":..., "error":...}

        tif: Gtc(挂单等待) | Ioc(立即成交否则取消) | Alo(只做 maker)
        """
        szd = self.sz_decimals(coin)
        sz_r = format_size(sz, szd)
        px_r = format_price(px, szd)
        if sz_r <= 0 or px_r <= 0:
            return {"status": "error", "error": f"数量/价格规整后为 0 (sz={sz}->{sz_r}, px={px}->{px_r})"}

        cloid = make_cloid(tag, counter)
        try:
            resp = self.exchange.order(
                name=coin,
                is_buy=is_buy,
                sz=sz_r,
                limit_px=px_r,
                order_type={"limit": {"tif": tif}},
                reduce_only=reduce_only,
                cloid=Cloid.from_str(cloid),
            )
        except Exception as e:
            return {"status": "error", "error": f"下单异常：{e}", "cloid": cloid}

        res = self._parse_order_resp(resp)
        res["cloid"] = cloid
        res["req_sz"] = sz_r
        res["req_px"] = px_r
        return res

    def cancel_orders(self, coin: str, oids: List[int]) -> bool:
        oids = [int(o) for o in oids if o]
        if not oids:
            return True
        try:
            resp = self.exchange.bulk_cancel([{"coin": coin, "oid": o} for o in oids])
        except Exception as e:
            self.log.warning("撤单异常：%s", e)
            return False
        return self._check_ok(resp, f"批量撤单 {len(oids)} 笔", quiet=True)

    def cancel_all_for_coin(self, coin: str) -> int:
        oo = self.open_orders(coin)
        if not oo:
            return 0
        oids = [int(o["oid"]) for o in oo]
        ok = self.cancel_orders(coin, oids)
        return len(oids) if ok else 0

    def close_position(self, coin: str, slippage_pct: float) -> Dict[str, Any]:
        """市价全平该标的的净持仓。"""
        try:
            resp = self.exchange.market_close(coin, slippage=float(slippage_pct) / 100.0)
        except Exception as e:
            return {"status": "error", "error": f"平仓异常：{e}"}
        if resp is None:
            return {"status": "ok", "note": "无持仓"}
        return self._parse_order_resp(resp) if isinstance(resp, dict) else {"status": "error", "error": str(resp)}

    # ------------------------------------------------------------ 内部
    @staticmethod
    def _parse_order_resp(resp: Any) -> Dict[str, Any]:
        if not isinstance(resp, dict):
            return {"status": "error", "error": f"非法响应：{resp!r}"}
        if resp.get("status") != "ok":
            return {"status": "error", "error": str(resp.get("response") or resp)}
        statuses = (((resp.get("response") or {}).get("data") or {}).get("statuses")) or []
        if not statuses:
            return {"status": "error", "error": "响应中没有 statuses"}
        st = statuses[0]
        if not isinstance(st, dict):
            return {"status": "error", "error": str(st)}
        if "error" in st:
            return {"status": "error", "error": str(st["error"])}
        if "resting" in st:
            return {"status": "resting", "oid": int(st["resting"]["oid"])}
        if "filled" in st:
            f = st["filled"]
            return {
                "status": "filled",
                "oid": int(f.get("oid") or 0),
                "avg_px": float(f.get("avgPx") or 0),
                "filled_sz": float(f.get("totalSz") or 0),
            }
        return {"status": "error", "error": f"未知状态：{st}"}

    def _check_ok(self, resp: Any, what: str, quiet: bool = False) -> bool:
        try:
            st = resp.get("status") if isinstance(resp, dict) else None
            if st == "ok":
                return True
            if not quiet:
                self.log.warning("%s 失败：%s", what, resp)
            else:
                self.log.debug("%s 失败：%s", what, resp)
        except Exception:
            pass
        return False

    def verify_connection(self) -> Dict[str, Any]:
        """启动前的连通性自检（只读）。"""
        out: Dict[str, Any] = {
            "wallet": self.wallet.address,
            "account": self.account_address,
            "agent_wallet": self.using_agent_wallet,
            "network": self.cfg.env.network,
        }
        st = self.info.user_state(self.account_address)
        ms = st.get("marginSummary", {}) or {}
        perp_value = float(ms.get("accountValue") or 0)
        abstraction = self.account_abstraction()
        spot = self.spot_usdc() if abstraction == "unifiedAccount" else 0.0
        out["abstraction"] = abstraction
        out["perp_account_value"] = perp_value
        out["spot_usdc"] = spot
        # 统一账户下两者是同一笔钱，不能相加（详见 equity()）
        out["account_value"] = max(perp_value, spot) if abstraction == "unifiedAccount" else perp_value
        out["mids"] = len(self.info.all_mids())
        out["open_orders"] = len(self.info.open_orders(self.account_address))
        return out


# =====================================================================
#                              行情（只读）
# =====================================================================
class MarketFeed:
    """只读行情源，不需要私钥。干跑模式和自检都用它。"""

    def __init__(self, base_url: str):
        self.info = Info(base_url, skip_ws=True)
        self._szd: Dict[str, int] = {}

    def mid_price(self, coin: str) -> float:
        mids = self.info.all_mids()
        if coin not in mids:
            raise BrokerError(f"行情里找不到 {coin}，请检查 market.coin 是否拼写正确")
        return float(mids[coin])

    def sz_decimals(self, coin: str) -> int:
        if coin not in self._szd:
            asset = self.info.coin_to_asset[coin]
            self._szd[coin] = int(self.info.asset_to_sz_decimals[asset])
        return self._szd[coin]

    def account_value(self, address: str) -> float:
        """只读账户权益。统一账户下需把现货 USDC 一起算，否则会显示成 0。"""
        try:
            st = self.info.user_state(address)
            total = float((st.get("marginSummary") or {}).get("accountValue") or 0)
            try:
                abstraction = str(self.info.query_user_abstraction_state(address) or "")
            except Exception:
                abstraction = ""
            if abstraction == "unifiedAccount":
                ss = self.info.spot_user_state(address) or {}
                total += sum(
                    float(b.get("total") or 0)
                    for b in (ss.get("balances") or [])
                    if b.get("coin") == "USDC"
                )
            return total
        except Exception:
            return 0.0


# =====================================================================
#                              干跑模拟
# =====================================================================
class PaperBroker:
    """本地撮合模拟。

    规则：限价买单在 市场价 <= 挂单价 时成交；限价卖单在 市场价 >= 挂单价 时成交，
    成交价按挂单价算（保守，对网格有利空不夸大）。
    不模拟排队、资金费率、滑点；手续费按 fee_bps 扣。
    """

    def __init__(self, cfg: Config, logger, start_balance: float = 100.0, fee_bps: float = 1.5):
        self.cfg = cfg
        self.log = logger
        self.balance = float(start_balance)  # 已实现现金（USD）
        self.fee_bps = float(fee_bps)
        self.fees_paid = 0.0

        self._oid = 900000
        self.orders: Dict[int, Dict[str, Any]] = {}
        self._fill_queue: Dict[int, FillInfo] = {}

        self.position = 0.0
        self.avg_entry: Optional[float] = None

        self._mid = 0.0
        self.sz_dec: Dict[str, int] = {}

    # ------------------------------------------------------------ 基本信息
    def set_sz_decimals(self, coin: str, n: int) -> None:
        """由主流程注入真实精度，避免干跑和实盘的数量处理不一致。"""
        self.sz_dec[coin] = int(n)

    def sz_decimals(self, coin: str) -> int:
        # 拿不到链上元数据时用一份常见的默认值兜底
        if coin not in self.sz_dec:
            self.sz_dec[coin] = {"BTC": 5, "ETH": 4, "SOL": 2, "HYPE": 2, "DOGE": 0, "XRP": 1}.get(coin, 3)
        return self.sz_dec[coin]

    def mid_price(self, coin: str) -> float:
        if self._mid <= 0:
            raise BrokerError("干跑模式尚未取得行情价格")
        return self._mid

    def feed_mid(self, px: float) -> None:
        """由主循环喂入最新中间价并撮合本地的挂单。"""
        self._mid = float(px)
        self._match()

    def set_leverage(self, coin: str, leverage: int, is_cross: bool) -> None:
        self.log.info("[干跑] 设置杠杆 %sx (%s)", leverage, "全仓" if is_cross else "逐仓")

    # ------------------------------------------------------------ 撮合
    def _match(self) -> None:
        for oid, o in list(self.orders.items()):
            px = o["px"]
            hit = (o["is_buy"] and self._mid <= px) or ((not o["is_buy"]) and self._mid >= px)
            if not hit:
                continue
            del self.orders[oid]
            # IOC（市价）按当时中间价成交，而不是按那个故意打偏的限价，
            # 否则模拟出来的滑点会比现实夸张得多
            self._apply_fill(o, self._mid if o.get("ioc") else px)

    def _apply_fill(self, o: Dict[str, Any], px: float) -> None:
        sz = o["sz"]
        signed = sz if o["is_buy"] else -sz
        notional = sz * px
        fee = notional * self.fee_bps / 10000.0
        self.fees_paid += fee
        self.balance -= fee

        prev = self.position
        closed_pnl = 0.0

        if prev == 0 or (prev > 0) == (signed > 0):
            # 同向加仓
            new_pos = prev + signed
            if prev == 0:
                self.avg_entry = px
            else:
                self.avg_entry = (self.avg_entry * abs(prev) + px * abs(signed)) / abs(new_pos)
        else:
            # 反向：先平掉现有仓位，剩余部分反手
            closing = min(abs(prev), abs(signed))
            if self.avg_entry is not None:
                closed_pnl = (
                    closing * (px - self.avg_entry) if prev > 0 else closing * (self.avg_entry - px)
                )
            self.balance += closed_pnl
            new_pos = prev + signed
            if abs(new_pos) < 1e-12:
                new_pos = 0.0
                self.avg_entry = None
            elif (prev > 0) != (new_pos > 0):
                self.avg_entry = px  # 反手，成本重设
        self.position = new_pos

        self._fill_queue[o["oid"]] = FillInfo(
            oid=o["oid"], sz=sz, px=px, closed_pnl=closed_pnl, fee=fee
        )
        if len(self._fill_queue) > 500:  # 防止长期运行时内存无上限增长
            for k in list(self._fill_queue)[:100]:
                self._fill_queue.pop(k, None)
        self.log.info(
            "[干跑成交] %s %s %.6f @ %s  平仓盈亏=%+.2f  手续费=%.4f",
            "买" if o["is_buy"] else "卖",
            self.cfg.market.coin,
            sz,
            px,
            closed_pnl,
            fee,
        )

    # ------------------------------------------------------------ 查询
    def snapshot(self, coin: str) -> AccountSnapshot:
        snap = AccountSnapshot()
        snap.net_position = self.position
        snap.entry_px = self.avg_entry
        snap.open_oids = set(self.orders.keys())
        if self._mid > 0 and self.position != 0 and self.avg_entry:
            upnl = self.position * (self._mid - self.avg_entry)
            snap.unrealized_pnl = upnl
            snap.total_notional = abs(self.position) * self._mid
        snap.account_value = self.balance + snap.unrealized_pnl
        return snap

    def open_orders(self, coin: str) -> List[Dict[str, Any]]:
        return [
            {"oid": oid, "coin": coin, "side": "B" if o["is_buy"] else "A", "sz": o["sz"], "limitPx": o["px"]}
            for oid, o in self.orders.items()
        ]

    def poll_fills(self, candidate_oids: Set[int], since_ms: int) -> Dict[int, FillInfo]:
        out = {oid: self._fill_queue.pop(oid) for oid in list(self._fill_queue) if oid in candidate_oids}
        return out

    # ------------------------------------------------------------ 下单
    def place_limit(
        self,
        coin: str,
        is_buy: bool,
        sz: float,
        px: float,
        reduce_only: bool,
        tag: int,
        counter: int,
        tif: str = "Gtc",
    ) -> Dict[str, Any]:
        szd = self.sz_decimals(coin)
        sz_r = format_size(sz, szd)
        px_r = format_price(px, szd)
        if sz_r <= 0 or px_r <= 0:
            return {"status": "error", "error": f"数量/价格规整后为 0 (sz={sz}->{sz_r}, px={px}->{px_r})"}

        if reduce_only and self.position != 0:
            if (self.position > 0) == is_buy:
                return {"status": "error", "error": "reduce-only 单会加仓，已被本地拦截"}

        self._oid += 1
        oid = self._oid
        self.orders[oid] = {
            "oid": oid,
            "is_buy": is_buy,
            "sz": sz_r,
            "px": px_r,
            "reduce_only": reduce_only,
            "cloid": make_cloid(tag, counter),
            "ioc": str(tif).upper() == "IOC",
        }
        # 挂上去立刻检查是否已经可以成交（价格已穿越）
        self._match()

        if oid in self.orders:
            cloid = self.orders[oid]["cloid"]
            if str(tif).upper() == "IOC":
                # IOC 不允许留在盘口：没立即成交就撤掉
                del self.orders[oid]
                return {"status": "error", "error": "IOC 未立即成交，已撤单", "cloid": cloid, "req_px": px_r}
            return {"status": "resting", "oid": oid, "cloid": cloid, "req_sz": sz_r, "req_px": px_r}

        # 返回真实成交价（IOC 会按中间价成交，而不是那个故意打偏的限价）
        f = self._fill_queue.get(oid)
        return {
            "status": "filled",
            "oid": oid,
            "cloid": make_cloid(tag, counter),
            "avg_px": f.px if f else px_r,
            "filled_sz": f.sz if f else sz_r,
        }

    def cancel_orders(self, coin: str, oids: List[int]) -> bool:
        for o in oids:
            self.orders.pop(int(o), None)
        return True

    def cancel_all_for_coin(self, coin: str) -> int:
        n = len(self.orders)
        self.orders.clear()
        return n

    def close_position(self, coin: str, slippage_pct: float) -> Dict[str, Any]:
        if self.position == 0:
            return {"status": "ok", "note": "无持仓"}
        sz = abs(self.position)
        is_buy = self.position < 0
        px = self._mid * (1 + slippage_pct / 100.0) if is_buy else self._mid * (1 - slippage_pct / 100.0)
        o = {
            "oid": self._oid + 1,
            "is_buy": is_buy,
            "sz": sz,
            "px": px,
            "reduce_only": True,
            "cloid": "manual",
        }
        self._oid += 1
        self._apply_fill(o, self._mid)  # 市价按现价成交
        return {"status": "filled", "oid": o["oid"], "avg_px": self._mid, "filled_sz": sz}

    def verify_connection(self) -> Dict[str, Any]:
        return {
            "wallet": "paper",
            "account": "paper",
            "agent_wallet": False,
            "network": f"{self.cfg.env.network}(dry-run)",
            "account_value": self.balance,
            "mids": 0,
            "open_orders": len(self.orders),
        }
