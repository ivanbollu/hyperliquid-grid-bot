"""多实例管理器。

每个 configs/*.yaml 是一个独立的交易实例（slot），
各自持有自己的 broker / feed 复用 / state / 线程。

线程模型：
  * 每个 slot 一个后台线程跑 engine.loop()
  * 所有对 _slots 的增删改查都在 self._lock 保护下
  * 网页读取的状态全部来自 engine.snapshot_dict()，
    它是引擎线程在自己循环里组装好的完整对象，不存在"读到一半"的问题
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from .broker import LiveBroker, MarketFeed, PaperBroker
from .config import Config, list_configs, load_config, update_yaml_text
from .engine import EngineHalt, GridEngine
from .logger import attach_ring_buffer, setup_logger
from .notify import Notifier
from .signal import SignalEngine
from .state import BotState
from .trigger import TriggerEngine
from .utils import now_ms
from .wallet import is_address, normalize_address, short_address


@dataclass
class SlotRuntime:
    name: str
    config_path: Path
    logger: Any = None
    ring: Any = None
    engine: Optional[Any] = None  # GridEngine / TriggerEngine / SignalEngine，接口一致
    thread: Optional[threading.Thread] = None
    state: Optional[BotState] = None
    broker: Any = None
    last_error: str = ""
    started_ms: int = 0
    log_level: str = "INFO"

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())


def make_engine(cfg: Config, broker, feed, state: BotState, logger, notifier: Notifier):
    """按配置里的 strategy 选择引擎。三者接口完全一致，上层无需关心差异。"""
    if cfg.strategy == "trigger":
        return TriggerEngine(cfg, broker, feed, state, logger, notifier)
    if cfg.strategy == "signal":
        return SignalEngine(cfg, broker, feed, state, logger, notifier)
    return GridEngine(cfg, broker, feed, state, logger, notifier)


class SlotManager:
    def __init__(self, root: Path, configs_dir: Optional[Path] = None):
        self.root = Path(root)
        self.configs_dir = Path(configs_dir or (self.root / "configs"))
        self.configs_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._slots: Dict[str, SlotRuntime] = {}
        self._feeds: Dict[str, MarketFeed] = {}

    # =================================================================
    #                            发现
    # =================================================================
    def refresh(self) -> None:
        """扫描 configs 目录，把新的配置文件登记进来。"""
        with self._lock:
            for p in list_configs(self.configs_dir):
                name = p.stem
                if name not in self._slots:
                    self._slots[name] = SlotRuntime(name=name, config_path=p)
            # 文件被移走的，若没在跑就移除登记
            for name in list(self._slots.keys()):
                rt = self._slots[name]
                if not rt.config_path.exists() and not rt.running:
                    del self._slots[name]

    def _slot(self, name: str) -> SlotRuntime:
        self.refresh()
        if name not in self._slots:
            raise KeyError(f"找不到实例 {name}")
        return self._slots[name]

    def _feed(self, base_url: str) -> MarketFeed:
        if base_url not in self._feeds:
            self._feeds[base_url] = MarketFeed(base_url)
        return self._feeds[base_url]

    def _logger(self, name: str, level: str = "INFO"):
        rt = self._slots[name]
        if rt.logger is None or rt.log_level != level:
            rt.logger = setup_logger(
                self.root / "logs" / name, level, name=f"gridbot.{name}", with_console=True
            )
            rt.log_level = level
        rt.ring = attach_ring_buffer(rt.logger, 400)
        return rt.logger

    def load(self, name: str) -> Config:
        with self._lock:
            rt = self._slot(name)
            cfg = load_config(rt.config_path)
        cfg.root = self.root
        return cfg

    # =================================================================
    #                            列表
    # =================================================================
    def list_slots(self) -> List[Dict[str, Any]]:
        self.refresh()
        out = []
        with self._lock:
            for name, rt in sorted(self._slots.items()):
                try:
                    cfg = load_config(rt.config_path)
                    cfg.root = self.root
                    meta = cfg.as_dict()
                    meta["config_error"] = ""
                except Exception as e:
                    meta = {"slot": name, "config_error": f"{type(e).__name__}: {e}"}
                meta["name"] = name
                meta["running"] = rt.running
                meta["last_error"] = rt.last_error
                meta["uptime_sec"] = round((now_ms() - rt.started_ms) / 1000.0, 1) if rt.started_ms else 0
                meta["state_status"] = rt.state.status if rt.state else "IDLE"
                if rt.engine is not None:
                    s = rt.engine.snapshot_dict()
                    meta["live"] = {
                        "account_value": s["account_value"],
                        "unrealized_pnl": s["unrealized_pnl"],
                        "realized_usd": s["realized_usd"],
                        "net_position": s["net_position"],
                        "mid": s["mid"],
                        "open_orders": s["open_orders"],
                        "closed_slots": s["closed_slots"],
                        "loop": s["loop"],
                        "status": s["status"],
                    }
                else:
                    meta["live"] = None
                out.append(meta)
        return out

    def overview(self) -> Dict[str, Any]:
        slots = self.list_slots()
        running = [s for s in slots if s["running"]]
        total_value = total_upnl = total_realized = 0.0
        for s in running:
            live = s.get("live") or {}
            total_value += float(live.get("account_value") or 0)
            total_upnl += float(live.get("unrealized_pnl") or 0)
            total_realized += float(live.get("realized_usd") or 0)
        return {
            "slots_total": len(slots),
            "slots_running": len(running),
            "slots_live": len([s for s in slots if not (s.get("env") or {}).get("dry_run", True)]),
            "account_value": total_value,
            "unrealized_pnl": total_upnl,
            "realized_usd": total_realized,
            "has_private_key": any(s.get("env", {}).get("has_private_key") for s in slots) or False,
            "ts": now_ms(),
        }

    def snapshot(self, name: str) -> Dict[str, Any]:
        with self._lock:
            rt = self._slot(name)
            if rt.engine is not None:
                return rt.engine.snapshot_dict()
            # 未启动过：用配置 + 磁盘状态拼一个静态快照
            cfg = load_config(rt.config_path)
            cfg.root = self.root
            state = BotState(self.root / cfg.runtime.state_file, cfg.market.coin)
            if (self.root / cfg.runtime.state_file).exists():
                state.load()
            is_trigger = cfg.strategy == "trigger"
            is_signal = cfg.strategy == "signal"
            return {
                "slot": name,
                "strategy": cfg.strategy,
                "trigger": {
                    "side": cfg.trigger.side,
                    "price": cfg.trigger.price,
                    "size": cfg.trigger.size,
                    "order_type": cfg.trigger.order_type,
                    "once": cfg.trigger.once,
                    "tp_pct": cfg.trigger.tp_pct,
                    "notional": round(cfg.trigger.size * cfg.trigger.price, 6),
                    "distance_pct": 0.0,
                    "phase": "待启动" if not state.slots else "已恢复状态",
                },
                "signal": {
                    "interval": cfg.signal.interval,
                    "candles": cfg.signal.candles,
                    "candle_cache_sec": cfg.signal.candle_cache_sec,
                    "fast_ema": cfg.signal.fast_ema,
                    "slow_ema": cfg.signal.slow_ema,
                    "trend_ema": cfg.signal.trend_ema,
                    "entry_mode": cfg.signal.entry_mode,
                    "entry_offset_pct": cfg.signal.entry_offset_pct,
                    "entry_ttl_sec": cfg.signal.entry_ttl_sec,
                    "allow_short": cfg.signal.allow_short,
                    "atr_period": cfg.signal.atr_period,
                    "atr_mult": cfg.signal.atr_mult,
                    "trail_pct": cfg.signal.trail_pct,
                    "take_profit_pct": cfg.signal.take_profit_pct,
                    "exit_on_cross": cfg.signal.exit_on_cross,
                    "max_rounds": cfg.signal.max_rounds,
                    "rounds": int(state.data.get("rounds", 0)),
                    "size": cfg.signal.size or cfg.market.base_size,
                    "position": "flat",
                    "phase": "待启动",
                    "desc": "启动后开始计算指标",
                    "fast": None,
                    "slow": None,
                    "close": None,
                    "bars": 0,
                    "atr": None,
                    "stop_px": None,
                    "extreme_px": None,
                    "last_kline_ms": 0,
                },
                "coin": cfg.market.coin,
                "running": False,
                "loop": 0,
                "uptime_sec": 0,
                "mid": 0.0,
                "network": cfg.env.network,
                "dry_run": cfg.env.dry_run,
                "mode": (
                    ("trigger-buy" if cfg.trigger.is_buy else "trigger-sell")
                    if is_trigger
                    else (f"signal-{cfg.signal.interval}" if is_signal else cfg.grid.mode)
                ),
                "leverage": cfg.market.leverage,
                "anchor_price": (
                    cfg.trigger.price
                    if is_trigger
                    else (state.data.get("anchor_price") or 0.0)
                ),
                "account_value": 0.0,
                "net_position": 0.0,
                "entry_px": None,
                "unrealized_pnl": 0.0,
                "total_notional": 0.0,
                "margin_used": 0.0,
                "liquidation_px": None,
                "open_orders": 0,
                "realized_usd": float(state.data.get("realized_usd", 0.0)),
                "closed_slots": int(state.data.get("closed_slots", 0)),
                "orders_today": int(state.data.get("orders_today", 0)),
                "status": state.status if state.status != "RUNNING" else "IDLE",
                "stop_reason": state.data.get("stop_reason") or "",
                "stopped_until_ms": state.stopped_until_ms,
                "error": "",
                "levels": [
                    {
                        "key": k,
                        "side": v["side"],
                        "level": v["level"],
                        "state": v["state"],
                        "size": v["size"],
                        "entry_px": v.get("entry_px") or None,
                        "tp_px": v.get("tp_px") or None,
                        "has_order": bool(v.get("entry_oid")),
                    }
                    for k, v in state.slots.items()
                ],
                "trades": state.trades()[-60:],
                "equity": [],
                "limits": {
                    "max_total_notional_usd": cfg.risk.max_total_notional_usd,
                    "max_position_size": cfg.risk.max_position_size,
                    "max_unrealized_loss_usd": cfg.risk.max_unrealized_loss_usd,
                    "min_account_value_usd": cfg.risk.min_account_value_usd,
                    "max_open_orders": cfg.risk.max_open_orders,
                    "max_daily_orders": cfg.risk.max_daily_orders,
                },
            }

    def logs(self, name: str, n: int = 200) -> List[str]:
        with self._lock:
            rt = self._slot(name)
            if rt.ring is None:
                return []
            return rt.ring.tail(n)

    # =================================================================
    #                          启动 / 停止
    # =================================================================
    def start(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            rt = self._slot(name)
            if rt.running:
                return False, "该实例已在运行"

            try:
                cfg = load_config(rt.config_path)
                cfg.root = self.root
                cfg.validate()
            except Exception as e:
                rt.last_error = f"配置错误：{e}"
                return False, rt.last_error

            logger = self._logger(name, cfg.runtime.log_level)
            try:
                state = BotState(self.root / cfg.runtime.state_file, cfg.market.coin).load()
                feed = self._feed(cfg.env.base_url)
                if cfg.env.dry_run:
                    broker = PaperBroker(cfg, logger)
                    addr = cfg.env.account_address or ""
                    if addr:
                        av = feed.account_value(addr)
                        if av > 0:
                            broker.balance = av
                else:
                    broker = LiveBroker(cfg, logger)

                notifier = Notifier(cfg, logger, name)
                engine = make_engine(cfg, broker, feed, state, logger, notifier)
                engine.start()  # 可能抛 EngineHalt
            except EngineHalt as e:
                rt.last_error = str(e)
                logger.warning("启动被拒绝：%s", e)
                return False, str(e)
            except Exception as e:
                rt.last_error = f"{type(e).__name__}: {e}"
                logger.exception("启动失败：%s", e)
                return False, rt.last_error

            rt.engine = engine
            rt.broker = broker
            rt.state = state
            rt.last_error = ""
            rt.started_ms = now_ms()
            rt.thread = threading.Thread(target=self._run_loop, args=(rt,), name=f"grid-{name}", daemon=True)
            rt.thread.start()
            return True, "已启动"

    @staticmethod
    def _run_loop(rt: SlotRuntime) -> None:
        try:
            rt.engine.loop()
        except Exception as e:  # 兜底，避免线程静默死掉
            rt.last_error = f"运行线程异常：{type(e).__name__}: {e}"
            if rt.logger:
                rt.logger.exception("运行线程异常：%s", e)

    def stop(self, name: str, close_position: bool = False) -> Tuple[bool, str]:
        with self._lock:
            rt = self._slot(name)
            if rt.engine is None:
                return False, "该实例未启动过"
            if not rt.running:
                rt.last_error = ""
                return True, "已经处于停止状态（订单与持仓保留）"

            rt.engine.request_stop("用户手动停止" + ("并平仓" if close_position else ""))
            th = rt.thread
        # 不在锁里 join，避免阻塞其他请求
        if th is not None:
            th.join(timeout=20)

        if close_position:
            try:
                n = rt.broker.cancel_all_for_coin(rt.engine.coin)
                res = rt.broker.close_position(rt.engine.coin, rt.engine.cfg.market.close_slippage_pct)
                if rt.state:
                    rt.state.slots.clear()
                    rt.state.save()
                return True, f"已停止并平仓（撤销 {n} 笔挂单，平仓结果：{res.get('status')}）"
            except Exception as e:
                return False, f"已停止，但平仓出错，请手工检查账户：{e}"
        return True, "已停止（挂单与持仓保留，重启后会继续接管）"

    def cancel_all(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            rt = self._slot(name)
            broker = rt.broker
            coin = rt.engine.coin if rt.engine else load_config(rt.config_path).market.coin
            state = rt.state
        if broker is None:
            try:
                broker, coin, state = self._ephemeral(name)
            except Exception as e:
                return False, str(e)
        try:
            n = broker.cancel_all_for_coin(coin)
            if state:
                for s in state.slots.values():
                    s["entry_oid"] = None
                    s["tp_oid"] = None
                    s["entry_cloid"] = None
                    s["tp_cloid"] = None
                state.save()
            return True, f"已撤销 {n} 笔挂单"
        except Exception as e:
            return False, f"撤单失败：{e}"

    def close_all(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            rt = self._slot(name)
            broker = rt.broker
            coin = rt.engine.coin if rt.engine else load_config(rt.config_path).market.coin
            state = rt.state
        if broker is None:
            try:
                broker, coin, state = self._ephemeral(name)
            except Exception as e:
                return False, str(e)
        try:
            n = broker.cancel_all_for_coin(coin)
            res = broker.close_position(coin, broker.cfg.market.close_slippage_pct)
            if state:
                state.slots.clear()
                state.save()
            return True, f"已撤销 {n} 笔挂单并市价平仓（{res.get('status')}）"
        except Exception as e:
            return False, f"平仓失败：{e}"

    def _ephemeral(self, name: str):
        """实例没启动过时，临时建一个 broker 来执行撤单/平仓。"""
        rt = self._slot(name)
        cfg = load_config(rt.config_path)
        cfg.root = self.root
        logger = self._logger(name, cfg.runtime.log_level)
        state = BotState(self.root / cfg.runtime.state_file, cfg.market.coin)
        if (self.root / cfg.runtime.state_file).exists():
            state.load()
        if cfg.env.dry_run:
            broker = PaperBroker(cfg, logger)
        else:
            broker = LiveBroker(cfg, logger)
        return broker, cfg.market.coin, state

    def reset_state(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            rt = self._slot(name)
            if rt.running:
                return False, "请先停止该实例"
            cfg = load_config(rt.config_path)
        try:
            BotState(self.root / cfg.runtime.state_file, cfg.market.coin).reset()
            p = self.root / "state" / f"equity_{name}.jsonl"
            if p.exists():
                p.unlink()
            # 引擎对象还揣着清空前的内存状态，不清掉的话面板会继续显示旧持仓/旧「已完成」，
            # 和「已清除」的提示自相矛盾。清掉后 snapshot() 会走静态分支，按新配置重新拼。
            with self._lock:
                rt.engine = None
                rt.broker = None
                rt.state = None
                rt.started_ms = 0
                rt.last_error = ""
            return True, "本地状态已清除（熔断冷却已解除）"
        except Exception as e:
            return False, f"清除失败：{e}"

    # =================================================================
    #                          配置编辑
    # =================================================================
    def update_config(self, name: str, payload: Dict[str, Any]) -> Tuple[bool, str]:
        with self._lock:
            rt = self._slot(name)
            if rt.running:
                return False, "运行中不能改配置，请先停止"
            path = rt.config_path
            cfg = load_config(path)
            cfg.root = self.root

            def apply(section: str, mapping: Dict[str, Any]) -> None:
                for k, v in (payload.get(section) or {}).items():
                    if not hasattr(getattr(cfg, section), k):
                        continue
                    setattr(getattr(cfg, section), k, v)

            for sec in ("env", "market", "grid", "martingale", "trigger", "signal", "risk", "runtime"):
                apply(sec, payload)

            # 根级字段：strategy
            st = payload.get("strategy")
            if isinstance(st, str) and st.strip():
                cfg.strategy = st.strip().lower()

            # notify 里如果没传 token 就不动原值
            nt = payload.get("notify") or {}
            for k, v in nt.items():
                if k == "telegram_bot_token" and not v:
                    continue
                if hasattr(cfg.notify, k):
                    setattr(cfg.notify, k, v)

            try:
                cfg.validate()
            except Exception as e:
                return False, f"参数不合法：{e}"

            cfg.save_to(path)
            rt.last_error = ""
            return True, "已保存（下次启动生效）"

    def bind_account(self, name: str, address: str) -> Tuple[bool, str]:
        """把网页上连接的钱包地址写进实例的 env.account_address。

        注意：这里只改「用哪个账户交易」这个地址，私钥仍然只从 .env 读取，
        网页永远不会、也无法把私钥写进配置。

        写文件走行级改写，配置文件里的注释不会被抹掉。
        """
        addr = normalize_address(address)
        if not addr:
            return False, "钱包地址格式不正确（应为 0x 开头的 40 位十六进制）"
        with self._lock:
            rt = self._slot(name)
            if rt.running:
                return False, "实例正在运行，请先停止再绑定交易账户"
            path = rt.config_path
            try:
                text = path.read_text(encoding="utf-8") if path.exists() else ""
                if not text.strip():
                    text = f"slot: {name}\n"
                path.write_text(
                    update_yaml_text(text, {"env": {"account_address": addr}}), encoding="utf-8"
                )
            except Exception as e:
                return False, f"写入配置失败：{e}"
            rt.last_error = ""
        return True, (
            f"已把 {name} 的交易账户设为 {short_address(addr)}。\n"
            "私钥仍从 .env 的 HL_PRIVATE_KEY 读取，网页不会保存任何私钥。"
        )

    @staticmethod
    def valid_address(address: str) -> bool:
        return is_address(address)

    def create_slot(self, name: str, template: Optional[Dict[str, Any]] = None) -> Tuple[bool, str]:
        name = (name or "").strip()
        if not name or not all(c.isalnum() or c in "-_" for c in name):
            return False, "名称只能包含字母、数字、- 和 _"
        with self._lock:
            path = self.configs_dir / f"{name}.yaml"
            if path.exists():
                return False, "同名配置已存在"
            data = template or DEFAULT_TEMPLATE
            data = dict(data)
            data["slot"] = name
            try:
                path.write_text(
                    yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False),
                    encoding="utf-8",
                )
            except Exception as e:
                return False, f"写入失败：{e}"
            self.refresh()
            return True, f"已创建 {path.name}"

    def delete_slot(self, name: str) -> Tuple[bool, str]:
        with self._lock:
            rt = self._slot(name)
            if rt.running:
                return False, "请先停止该实例"
            # 不真删，改名后移出扫描范围，随时可恢复
            backup = rt.config_path.with_suffix(f".yaml.deleted-{now_ms()}")
            try:
                rt.config_path.rename(backup)
            except Exception as e:
                return False, f"删除失败：{e}"
            del self._slots[name]
            return True, f"已删除（备份在 {backup.name}，可手工恢复）"

    # =================================================================
    #                            收尾
    # =================================================================
    def stop_all(self) -> None:
        with self._lock:
            names = list(self._slots.keys())
        for n in names:
            try:
                self.stop(n, close_position=False)
            except Exception:
                pass


DEFAULT_TEMPLATE: Dict[str, Any] = {
    "slot": "new-slot",
    "strategy": "grid",  # grid=网格+马丁 | trigger=触发单 | signal=信号策略
    "env": {"network": "mainnet", "dry_run": True, "account_address": ""},
    "market": {
        "coin": "ETH",
        "leverage": 5,
        "margin_mode": "isolated",
        "base_size": 0.01,
        "reduce_only_tp": True,
        "close_slippage_pct": 1.0,
    },
    "grid": {
        "mode": "long",
        "anchor_price": 0,
        "step_pct": 0.8,
        "levels": 8,
        "take_profit_pct": 0.8,
        "reanchor": False,
        "recenter_threshold_pct": 6.0,
    },
    "martingale": {"enabled": True, "multiplier": 1.4, "max_size_multiple": 8.0},
    "trigger": {
        "side": "buy",
        "price": 0,
        "size": 0,
        "order_type": "limit",
        "max_slippage_pct": 0.5,
        "once": True,
        "tp_pct": 0,
    },
    "signal": {
        "interval": "15m",
        "candles": 300,
        "candle_cache_sec": 20,
        "fast_ema": 12,
        "slow_ema": 48,
        "trend_ema": 0,
        "entry_mode": "maker",
        "entry_offset_pct": 0.05,
        "entry_ttl_sec": 90,
        "allow_short": False,
        "atr_period": 14,
        "atr_mult": 2,
        "trail_pct": 0.6,
        "take_profit_pct": 0,
        "exit_on_cross": True,
        "size": 0,
        "max_rounds": 0,
    },
    "risk": {
        "max_total_notional_usd": 500,
        "max_open_orders": 24,
        "max_unrealized_loss_usd": 60,
        "max_position_size": 0.5,
        "min_account_value_usd": 20,
        "max_daily_orders": 200,
        "cooldown_after_stop_sec": 3600,
    },
    "runtime": {
        "loop_interval_sec": 5,
        "state_file": "state/bot_state.json",
        "log_dir": "logs",
        "log_level": "INFO",
        "equity_interval_sec": 30,
    },
    "notify": {
        "enabled": False,
        "telegram_bot_token": "",
        "telegram_chat_id": "",
        "on_fill": False,
        "on_stop": True,
    },
}
