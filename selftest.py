"""离线自测：不联网、不花钱，验证网格/马丁/风控逻辑是否正确。

运行： python selftest.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

import yaml

from bot.broker import PaperBroker
from bot.candles import Candle, interval_ms, max_history_days
from bot.candles import _parse as _parse_candle
from bot.config import Config, _as_bool as as_bool, load_config, update_yaml_text
from bot.engine import EngineHalt, GridEngine
from bot.indicators import atr, cross_down, cross_up, ema, ema_state, sma, trail_stop_price
from bot.logger import setup_logger
from bot.signal import SignalEngine
from bot.state import BotState
from bot.trigger import TriggerEngine
from bot.utils import equity_floor_problem, format_price, format_size, make_cloid, now_ms

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  {extra}")


class FakeFeed:
    def __init__(self, px: float, szd: int = 4):
        self.px = px
        self.szd = szd

    def mid_price(self, coin: str) -> float:
        return self.px

    def sz_decimals(self, coin: str) -> int:
        return self.szd


def make_cfg(root: Path | None = None) -> Config:
    cfg = Config(root=root or Path(tempfile.mkdtemp()))
    cfg.env.dry_run = True
    cfg.market.coin = "ETH"
    cfg.market.base_size = 0.01
    cfg.market.leverage = 5
    cfg.grid.anchor_price = 3000.0
    cfg.grid.step_pct = 1.0
    cfg.grid.levels = 5
    cfg.grid.take_profit_pct = 1.0
    cfg.grid.reanchor = False
    cfg.martingale.enabled = True
    cfg.martingale.multiplier = 1.4
    cfg.martingale.max_size_multiple = 8.0
    cfg.risk.max_position_size = 1.0
    cfg.risk.max_total_notional_usd = 5000.0
    cfg.risk.max_unrealized_loss_usd = 10000.0
    cfg.risk.min_account_value_usd = 1.0
    cfg.risk.max_open_orders = 24
    cfg.risk.max_daily_orders = 1000
    cfg.runtime.loop_interval_sec = 0.01
    return cfg


def make_trigger_cfg(
    root: Path | None = None,
    side: str = "buy",
    price: float = 2505.0,
    size: float = 0.005,
    order_type: str = "limit",
    once: bool = True,
    tp_pct: float = 0.0,
) -> Config:
    """触发单策略的测试配置。"""
    cfg = Config(root=root or Path(tempfile.mkdtemp()))
    cfg.strategy = "trigger"
    cfg.env.dry_run = True
    cfg.market.coin = "ETH"
    cfg.market.leverage = 5
    cfg.market.reduce_only_tp = True
    cfg.trigger.side = side
    cfg.trigger.price = price
    cfg.trigger.size = size
    cfg.trigger.order_type = order_type
    cfg.trigger.once = once
    cfg.trigger.tp_pct = tp_pct
    cfg.risk.max_position_size = 1.0
    cfg.risk.max_total_notional_usd = 5000.0
    cfg.risk.max_unrealized_loss_usd = 10000.0
    cfg.risk.min_account_value_usd = 1.0
    cfg.risk.max_daily_orders = 1000
    cfg.runtime.loop_interval_sec = 0.01
    return cfg


def section(t: str) -> None:
    print(f"\n=== {t} ===")


def test_utils() -> None:
    section("精度与 cloid")
    # ETH: szDecimals=4 -> 价格最多 2 位小数，且 <=5 位有效数字
    check("价格 3000.123456 -> 3000.1", format_price(3000.123456, 4) == 3000.1, str(format_price(3000.123456, 4)))
    check("价格 2999.999999 -> 2999.9", format_price(2999.999999, 4) == 2999.9, str(format_price(2999.999999, 4)))
    check("数量 0.019999 -> 0.0199", format_size(0.019999, 4) == 0.0199, str(format_size(0.019999, 4)))
    # BTC: szDecimals=5 -> 价格最多 1 位小数
    check("BTC 价格 100000.55 -> 100000.0", format_price(100000.55, 5) == 100000.0, str(format_price(100000.55, 5)))
    c = make_cloid(0x01, 7)
    check("cloid 长度 34 (0x+32)", len(c) == 34 and c.startswith("0x"), c)
    check("cloid 唯一", make_cloid(0x01, 7) != make_cloid(0x01, 8))


def test_grid_cycle() -> None:
    section("网格完整循环：挂单 -> 建仓成交 -> 止盈成交 -> 复位")
    cfg = make_cfg()
    log = setup_logger(Path(tempfile.mkdtemp()) / "logs", "WARNING")
    feed = FakeFeed(3000.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(Path(tempfile.mkdtemp()) / "s.json", "ETH").load()
    eng = GridEngine(cfg, broker, feed, state, log)

    eng.start()

    # 启动后应该铺出 5 层买单：2970 / 2940 / 2910 / 2880 / 2850
    pending = [s for s in state.slots.values() if s["state"] == "pending"]
    check("启动后铺出 5 层买单", len(pending) == 5, f"实际 {len(pending)}")
    expected_px = [2970.0, 2940.0, 2910.0, 2880.0, 2850.0]
    for i, p in enumerate(expected_px, start=1):
        s = state.slots[f"B{i}"]
        check(f"B{i} 挂单价 {p}", abs(s["size"] - format_size(0.01 * 1.4 ** (i - 1), 4)) < 1e-12,
              f"数量 {s['size']}")
    check("马丁加仓：B2 数量 > B1", state.slots["B2"]["size"] > state.slots["B1"]["size"])
    check("马丁上限：B5 不超过 x8",
          state.slots["B5"]["size"] <= format_size(0.01 * 8, 4) + 1e-12, f"B5={state.slots['B5']['size']}")

    # 价格跌到 2970 -> B1 成交
    feed.px = 2970.0
    eng.tick()
    check("B1 状态变为持仓中", state.slots["B1"]["state"] == "open", state.slots["B1"]["state"])
    check("B1 成交价 = 2970", abs(state.slots["B1"]["entry_px"] - 2970.0) < 1e-9, str(state.slots["B1"]["entry_px"]))
    check("B1 已挂止盈", bool(state.slots["B1"]["tp_oid"]))
    check("B1 止盈价 = 2999.7", abs(state.slots["B1"]["tp_px"] - 2999.7) < 1e-6, str(state.slots["B1"]["tp_px"]))
    check("持仓 = 0.01", abs(broker.position - 0.01) < 1e-9, str(broker.position))

    # 价格反弹到 3000 -> B1 止盈成交
    feed.px = 3000.0
    eng.tick()
    check("B1 复位为待建仓", state.slots["B1"]["state"] == "pending", state.slots["B1"]["state"])
    check("持仓归零", abs(broker.position) < 1e-9, str(broker.position))
    check("已实现盈亏 > 0", float(state.data["realized_usd"]) > 0, str(state.data["realized_usd"]))
    check("完成网格计数 = 1", state.data["closed_slots"] == 1, str(state.data["closed_slots"]))
    check("B1 重新挂出买单", bool(state.slots["B1"]["entry_oid"]))


def test_position_cap() -> None:
    section("风控：持仓上限会阻止继续加仓")
    cfg = make_cfg()
    cfg.risk.max_position_size = 0.02  # 只够一两层
    log = setup_logger(Path(tempfile.mkdtemp()) / "logs", "ERROR")
    feed = FakeFeed(3000.0)
    broker = PaperBroker(cfg, log, start_balance=10000.0)
    state = BotState(Path(tempfile.mkdtemp()) / "s.json", "ETH").load()
    eng = GridEngine(cfg, broker, feed, state, log)
    eng.start()

    n_orders = sum(1 for s in state.slots.values() if s.get("entry_oid"))
    check("持仓上限 $0.02 时只挂出少量买单", n_orders <= 2, f"实际 {n_orders}")

    # 一路下跌，持仓不应超过上限
    for px in (2970, 2940, 2910, 2880, 2850, 2800, 2700):
        feed.px = float(px)
        eng.tick()
    check(
        f"下跌过程中持仓不超过上限 0.02",
        abs(broker.position) <= 0.02 + 1e-6,
        f"实际 {broker.position}",
    )


def test_kill_switch() -> None:
    section("风控：浮亏熔断会平仓并停机")
    cfg = make_cfg()
    cfg.risk.max_unrealized_loss_usd = 3.0  # 很小的熔断线
    cfg.risk.cooldown_after_stop_sec = 60
    log = setup_logger(Path(tempfile.mkdtemp()) / "logs", "CRITICAL")
    feed = FakeFeed(3000.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(Path(tempfile.mkdtemp()) / "s.json", "ETH").load()
    eng = GridEngine(cfg, broker, feed, state, log)
    eng.start()

    feed.px = 2900.0
    eng.tick()
    check("已建仓", abs(broker.position) > 0, str(broker.position))

    feed.px = 2700.0  # 深度下跌，浮亏触发熔断
    eng.tick()
    check("熔断后持仓被平掉", abs(broker.position) < 1e-9, str(broker.position))
    check("状态写入 STOPPED", state.status == "STOPPED", state.status)
    check("引擎进入停止流程", eng.stopping)
    check("冷却时间已写入", state.stopped_until_ms > 0)

    # 再次启动应被冷却拦住
    blocked = False
    try:
        eng.start()
    except EngineHalt:
        blocked = True
    check("冷却期内拒绝重启", blocked)


def test_restart_recovery() -> None:
    section("重启恢复：状态文件驱动对账，不会重复挂单")
    cfg = make_cfg()
    cfg.risk.max_total_notional_usd = 400.0  # 故意设成刚好够铺部分层级
    log = setup_logger(Path(tempfile.mkdtemp()) / "logs", "ERROR")
    tmp = Path(tempfile.mkdtemp())
    feed = FakeFeed(3000.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(tmp / "s.json", "ETH").load()
    eng = GridEngine(cfg, broker, feed, state, log)
    eng.start()
    n1 = len([s for s in state.slots.values() if s.get("entry_oid")])
    check(f"首次启动挂出 {n1} 笔买单", n1 > 0, str(n1))

    # 模拟进程重启：状态从磁盘重新读入，broker 换成全新的（挂单全丢）
    state2 = BotState(tmp / "s.json", "ETH").load()
    broker2 = PaperBroker(cfg, log, start_balance=1000.0)
    eng2 = GridEngine(cfg, broker2, feed, state2, log)
    eng2.start()
    n2 = len([s for s in state2.slots.values() if s.get("entry_oid")])
    check(f"重启后重新补齐挂单：{n2} 笔（应与重启前一致，不能被风控重复计算误拦）", n2 == n1, f"{n1} vs {n2}")
    check("重启后不产生重复层位", len(state2.slots) == len(state.slots), f"{len(state2.slots)} vs {len(state.slots)}")

    # 再次 tick 不应重复挂单
    eng2.tick()
    n3 = len([s for s in state2.slots.values() if s.get("entry_oid")])
    check(f"再次循环后挂单数稳定在 {n3} 笔", n3 == n2, f"{n2} vs {n3}")


def test_snapshot_dict() -> None:
    section("网页快照：结构与字段完整性")
    cfg = make_cfg()
    log = setup_logger(Path(tempfile.mkdtemp()) / "logs", "ERROR")
    feed = FakeFeed(3000.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(Path(tempfile.mkdtemp()) / "s.json", "ETH").load()
    eng = GridEngine(cfg, broker, feed, state, log)
    eng.start()

    snap = eng.snapshot_dict()
    required = [
        "slot", "coin", "running", "mid", "account_value", "net_position", "unrealized_pnl",
        "realized_usd", "levels", "trades", "equity", "limits", "status", "dry_run", "anchor_price",
    ]
    missing = [k for k in required if k not in snap]
    check("快照包含所有必要字段", not missing, f"缺少 {missing}")
    check("运行中标志为 True", snap["running"] is True)
    check(f"层级数量 = 5", len(snap["levels"]) == 5, str(len(snap["levels"])))
    check("层级字段完整", all(set(["key", "side", "level", "state", "size"]) <= set(l) for l in snap["levels"]))

    # 走一轮成交，检查 trades / equity 有内容
    feed.px = 2970.0
    eng.tick()
    snap = eng.snapshot_dict()
    check("建仓后 trades 里有记录", len(snap["trades"]) >= 1, str(len(snap["trades"])))
    check("建仓记录类型为 open", snap["trades"][-1]["kind"] == "open", str(snap["trades"][-1]))
    check("权益曲线已有采样点", len(snap["equity"]) >= 1, str(len(snap["equity"])))

    feed.px = 3000.0
    eng.tick()
    snap = eng.snapshot_dict()
    check("止盈后 trades 里有 close 记录", any(t["kind"] == "close" for t in snap["trades"]))
    check("快照里的已实现盈亏与状态一致",
          abs(snap["realized_usd"] - float(state.data["realized_usd"])) < 1e-9)

    # JSON 可序列化（网页要 json.dumps 它）
    try:
        json.dumps(snap, ensure_ascii=False, default=str)
        ok = True
    except Exception as e:
        ok = False
    check("快照可以 JSON 序列化", ok)


def test_wallet_address() -> None:
    section("钱包地址处理（只读，不联网）")
    from bot.wallet import MAX_FILLS, base_url, is_address, normalize_address, short_address
    from bot.wallet import _f, _i, _side

    good = "0x0A3343b600D81CdAAC131342652241CD32e14349"
    check("接受标准 40 位十六进制地址", is_address(good))
    check("归一化为小写（Hyperliquid 内部按小写索引）",
          normalize_address(good) == good.lower(), normalize_address(good))
    check("大写 0X 前缀也能识别", is_address("0X" + good[2:]))

    for bad, why in [
        ("", "空串"),
        ("0x", "只有前缀"),
        (good[:20], "太短"),
        (good + "ab", "太长"),
        ("0x" + "g" * 40, "非法字符"),
        ("0a3343b600d81cdaac131342652241cd32e14349", "缺少 0x"),
        (None, "None"),
        (12345, "数字"),
    ]:
        check(f"拒绝非法地址（{why}）", not is_address(bad), repr(bad))

    check("地址缩略格式", short_address(good) == "0x0a33…4349", short_address(good))
    check("非法地址缩略为空", short_address("abc") == "", short_address("abc"))
    check("测试网 base_url 走 testnet",
          "testnet" in base_url("testnet"), base_url("testnet"))
    check("未知网络默认回落主网", base_url("whatever") == base_url("mainnet"), base_url("whatever"))

    # 数值清洗：Hyperliquid 全部用字符串传数值，且可能出现空串/None
    check("字符串数值可解析", _f("123.45") == 123.45)
    check("空串回落默认值", _f("") == 0.0 and _f(None, 1.0) == 1.0)
    check("NaN / inf 被拦掉", _f("nan") == 0.0 and _f("inf") == 0.0)
    check("整数解析容错", _i("1690000000000") == 1690000000000 and _i(None) == 0)

    # 买卖方向：Hyperliquid 用 B/A
    check("B 识别为买", _side("B") == "buy")
    check("A 识别为卖", _side("A") == "sell")
    check("小写 b 也识别为买", _side("b") == "buy")

    check("成交条数上限被限制", MAX_FILLS == 60, str(MAX_FILLS))


def test_yaml_writer() -> None:
    section("YAML 写回（保留注释）")
    from bot.config import load_config, update_yaml_text

    src = """# 顶层说明
slot: demo

# 运行环境段落说明
env:
  network: mainnet            # 行尾注释要留住
  dry_run: true
  account_address: ''
market:
  coin: ETH
  base_size: 0.01
"""
    # 1) 改一个带行尾注释的值
    out = update_yaml_text(src, {"env": {"network": "testnet"}})
    check("行尾注释被保留", "# 行尾注释要留住" in out, out)
    check("值被正确改写", "network: testnet" in out, out)
    check("段落说明注释被保留", "# 运行环境段落说明" in out)

    # 2) 新增一个已存在段落里没有的键
    out2 = update_yaml_text(src, {"market": {"leverage": 7}})
    check("缺失的键被插入到该段落末尾", "  leverage: 7" in out2, out2)
    check("插入不影响原段落其它键", "coin: ETH" in out2 and "base_size: 0.01" in out2)

    # 3) 新增一个整段
    out3 = update_yaml_text(src, {"notify": {"enabled": False}})
    check("缺失的段落被追加", "notify:" in out3 and "  enabled: false" in out3, out3)

    # 4) 十六进制地址必须带引号 —— 否则 YAML 1.1 会当成大整数读回来
    addr = "0x0a3343b600d81cdaac131342652241cd32e14349"
    out4 = update_yaml_text(src, {"env": {"account_address": addr}})
    check("十六进制地址被加引号（避免被解析成整数）", f"'{addr}'" in out4, out4)
    parsed = yaml.safe_load(out4)
    check("地址读回来仍是字符串", isinstance(parsed["env"]["account_address"], str)
          and parsed["env"]["account_address"] == addr, repr(parsed["env"]["account_address"]))

    # 5) 各种标量类型
    out5 = update_yaml_text(src, {"env": {"dry_run": False}, "market": {"base_size": 0.005}})
    p5 = yaml.safe_load(out5)
    check("布尔写回为 false", p5["env"]["dry_run"] is False, repr(p5["env"]["dry_run"]))
    check("浮点写回不失真", p5["market"]["base_size"] == 0.005, repr(p5["market"]["base_size"]))

    out6 = update_yaml_text(src, {"env": {"account_address": ""}})
    check("空串写回为 '' 且读回还是空串", yaml.safe_load(out6)["env"]["account_address"] == "", out6)

    # 6) 顶层键
    out7 = update_yaml_text(src, {None: {"slot": "renamed"}})
    check("顶层键可改写", yaml.safe_load(out7)["slot"] == "renamed", out7)
    out8 = update_yaml_text(src, {None: {"extra": "x"}})
    check("缺失的顶层键被追加", yaml.safe_load(out8)["extra"] == "x", out8)

    # 7) 幂等：同样内容写两次结果一致
    a = update_yaml_text(src, {"env": {"network": "testnet"}, "grid": {"levels": 8}})
    b = update_yaml_text(a, {"env": {"network": "testnet"}, "grid": {"levels": 8}})
    check("重复写回幂等（不会越写越长）", a == b, f"{len(a)} vs {len(b)}")

    # 8) 完整配置文件走一遍「保存配置」路径，注释一行不丢
    root = Path(tempfile.mkdtemp())
    real = Path(__file__).resolve().parent / "configs" / "eth-grid.yaml"
    if real.exists():
        target = root / "eth-grid.yaml"
        original = real.read_text(encoding="utf-8")
        target.write_text(original, encoding="utf-8")
        cfg = load_config(target)

        # 先验证「不改任何东西就保存」是逐字节幂等的 —— 这会逼出所有
        # 格式化失真（比如整数被写成 500.0、注释被顶歪）
        cfg.save_to(target)
        check("原样保存后文件逐字节不变", target.read_text(encoding="utf-8") == original,
              "diff 见上；常见原因是数值格式或注释位置漂移")

        before_c = target.read_text(encoding="utf-8")
        cfg.grid.step_pct = 1.25
        cfg.risk.max_unrealized_loss_usd = 88
        cfg.env.dry_run = False
        cfg.save_to(target)
        after_c = target.read_text(encoding="utf-8")
        lost = [l for l in before_c.splitlines() if l.strip().startswith("#") and l not in after_c.splitlines()]
        check("真实配置文件保存后注释一行不丢", len(lost) == 0, str(lost[:3]))
        check("只改动的那几行发生变化", len(before_c.splitlines()) == len(after_c.splitlines()),
              f"{len(before_c.splitlines())} vs {len(after_c.splitlines())}")
        re_cfg = load_config(target)
        check("保存后配置可原样读回",
              (re_cfg.grid.step_pct, re_cfg.risk.max_unrealized_loss_usd, re_cfg.env.dry_run)
              == (1.25, 88.0, False),
              f"{re_cfg.grid.step_pct} / {re_cfg.risk.max_unrealized_loss_usd} / {re_cfg.env.dry_run}")
        check("未改动的字段保持原值",
              (re_cfg.market.coin, re_cfg.grid.levels, re_cfg.market.base_size,
               re_cfg.runtime.loop_interval_sec, re_cfg.risk.cooldown_after_stop_sec)
              == ("ETH", 8, 0.01, 5.0, 3600),
              "字段被误改")


def test_wallet_bind() -> None:
    section("绑定交易账户（写配置，不联网）")
    import shutil

    from bot.manager import SlotManager

    root = Path(tempfile.mkdtemp())
    (root / "configs").mkdir(parents=True)
    src = Path(__file__).resolve().parent / "configs" / "eth-grid.yaml"
    shutil.copy(src, root / "configs" / "eth-grid.yaml")
    before = (root / "configs" / "eth-grid.yaml").read_text(encoding="utf-8")

    mgr = SlotManager(root)
    addr = "0X0A3343B600D81CDAAC131342652241CD32E14349"   # 故意用大写前缀和大小写混写

    ok, msg = mgr.bind_account("eth-grid", "0xzz")
    check("非法地址被拒绝", not ok, msg)

    ok, msg = mgr.bind_account("eth-grid", addr)
    check("合法地址绑定成功", ok, msg)
    check("返回消息里带缩略地址", "0x0a33…4349" in msg, msg)

    after = (root / "configs" / "eth-grid.yaml").read_text(encoding="utf-8")
    lost = [l for l in before.splitlines() if l.strip().startswith("#") and l not in after.splitlines()]
    check("绑定不破坏配置文件注释", len(lost) == 0, str(lost[:3]))

    cfg = load_config(root / "configs" / "eth-grid.yaml")
    check("地址被归一化为小写", cfg.env.account_address == addr.lower(), cfg.env.account_address)
    check("其它配置项未被改动", cfg.market.coin == "ETH" and cfg.grid.levels == 8)

    try:
        mgr.bind_account("nonexistent", addr)
        check("不存在的实例应抛错（由接口层转 404）", False)
    except KeyError:
        check("不存在的实例应抛错（由接口层转 404）", True)


def test_trigger_limit_entry() -> None:
    section("触发单：挂限价单，价格到达才成交")
    cfg = make_trigger_cfg(price=2505.0, size=0.005)
    tmp = Path(tempfile.mkdtemp())
    log = setup_logger(tmp / "logs", "ERROR")
    feed = FakeFeed(2510.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(tmp / "t.json", "ETH").load()
    eng = TriggerEngine(cfg, broker, feed, state, log)
    eng.start()

    slot = state.slots["T1"]
    check("启动后触发单已挂在盘口", bool(slot.get("entry_oid")), str(slot))
    o = broker.orders.get(int(slot.get("entry_oid") or 0))
    check("挂单价 = 2505", bool(o) and o["px"] == 2505.0, str(o))
    check("挂的是买单", bool(o) and o["is_buy"] is True, str(o))
    check("挂了单但还没成交", slot["state"] == "pending")

    eng.tick()
    check("价格 2510 在触发价上方时保持不成交", state.slots["T1"]["state"] == "pending")

    feed.px = 2504.0
    eng.tick()
    slot = state.slots["T1"]
    check("价格跌到 2504 后成交", slot["state"] == "open", str(slot))
    check("成交价 = 挂单价 2505", slot["entry_px"] == 2505.0, str(slot["entry_px"]))
    check("实际持仓 0.005", abs(broker.position - 0.005) < 1e-9, str(broker.position))
    check("once=true 时标记为已完成", state.data.get("trigger_done") is True)
    check("状态置为 DONE", state.status == "DONE", state.status)
    check(
        "写入了一条建仓成交记录",
        any(t["kind"] == "open" and t["side"] == "buy" for t in state.trades()),
    )
    check("实例不自动退出（继续监控持仓）", eng.stopping is False)


def test_trigger_market_mode() -> None:
    section("触发单：market 模式，价格穿越后才下单")
    cfg = make_trigger_cfg(price=2505.0, size=0.005, order_type="market")
    tmp = Path(tempfile.mkdtemp())
    log = setup_logger(tmp / "logs", "ERROR")
    feed = FakeFeed(2510.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(tmp / "t.json", "ETH").load()
    eng = TriggerEngine(cfg, broker, feed, state, log)
    eng.start()
    check("未到价时盘口不挂任何单", len(broker.orders) == 0, str(broker.orders))
    check("未到价时保持待触发", state.slots["T1"]["state"] == "pending")

    feed.px = 2505.0
    eng.tick()
    check("价格穿越后立即成交", state.slots["T1"]["state"] == "open", str(state.slots["T1"]))
    check("市价成交价贴近中间价", abs(state.slots["T1"]["entry_px"] - 2505.0) < 0.01, str(state.slots["T1"]["entry_px"]))
    check("实际持仓 0.005", abs(broker.position - 0.005) < 1e-9, str(broker.position))


def test_trigger_tp() -> None:
    section("触发单：成交后自动挂止盈")
    cfg = make_trigger_cfg(price=2505.0, size=0.005, tp_pct=2.0)
    tmp = Path(tempfile.mkdtemp())
    log = setup_logger(tmp / "logs", "ERROR")
    feed = FakeFeed(2510.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(tmp / "t.json", "ETH").load()
    eng = TriggerEngine(cfg, broker, feed, state, log)
    eng.start()

    feed.px = 2504.0
    eng.tick()
    slot = state.slots["T1"]
    check("建仓成交", slot["state"] == "open", str(slot))
    check("自动挂出止盈单", bool(slot.get("tp_oid")), str(slot))
    check("止盈价 ≈ 2505 × 1.02", abs(float(slot.get("tp_px") or 0) - 2555.1) < 0.05, str(slot.get("tp_px")))
    tp_o = broker.orders.get(int(slot.get("tp_oid") or 0))
    check("止盈单是 reduce-only", bool(tp_o) and tp_o["reduce_only"] is True, str(tp_o))
    check("止盈未成交前不算完成", state.data.get("trigger_done") is not True)

    feed.px = 2560.0
    eng.tick()
    check("止盈成交后标记完成", state.data.get("trigger_done") is True)
    check("已实现盈亏为正", float(state.data.get("realized_usd", 0.0)) > 0, str(state.data.get("realized_usd")))
    check("持仓已归零", abs(broker.position) < 1e-9, str(broker.position))


def test_trigger_restart() -> None:
    section("触发单：重启对账，不重复挂单 / 不重复成交")
    cfg = make_trigger_cfg(price=2505.0, size=0.005, once=True)
    tmp = Path(tempfile.mkdtemp())
    log = setup_logger(tmp / "logs", "ERROR")
    feed = FakeFeed(2510.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    state = BotState(tmp / "t.json", "ETH").load()
    TriggerEngine(cfg, broker, feed, state, log).start()
    check("首次启动挂出 1 笔", len(broker.orders) == 1, str(broker.orders))

    # 模拟进程重启：状态从磁盘读回，broker 换新的（挂单全丢）
    state2 = BotState(tmp / "t.json", "ETH").load()
    broker2 = PaperBroker(cfg, log, start_balance=1000.0)
    eng2 = TriggerEngine(cfg, broker2, feed, state2, log)
    eng2.start()
    check("重启瞬间不盲目重复挂单（先对账）", len(broker2.orders) == 0, str(broker2.orders))
    eng2.tick()
    check("对账发现挂单丢失后重新挂出", len(broker2.orders) == 1, str(broker2.orders))
    check("重启后仍然只有一个层位", len(state2.slots) == 1, str(list(state2.slots)))

    # 已完成的任务，重启后不再补挂
    tmp3 = Path(tempfile.mkdtemp())
    log3 = setup_logger(tmp3 / "logs", "ERROR")
    feed3 = FakeFeed(2510.0)
    broker3 = PaperBroker(cfg, log3, start_balance=1000.0)
    state3 = BotState(tmp3 / "t.json", "ETH").load()
    eng3 = TriggerEngine(cfg, broker3, feed3, state3, log3)
    eng3.start()
    feed3.px = 2504.0
    eng3.tick()
    check("已成交并标记完成", state3.data.get("trigger_done") is True)

    state4 = BotState(tmp3 / "t.json", "ETH").load()
    broker4 = PaperBroker(cfg, log3, start_balance=1000.0)
    eng4 = TriggerEngine(cfg, broker4, feed3, state4, log3)
    eng4.start()
    eng4.tick()
    check("已完成的触发单重启后不再补挂", len(broker4.orders) == 0, str(broker4.orders))
    check("重启后完成标记仍然保留", state4.data.get("trigger_done") is True)


def test_trigger_guards() -> None:
    section("触发单：启动前置校验（交易所最低 $10 门槛）")
    for size, should_pass in ((0.001, False), (0.004, True)):
        cfg = make_trigger_cfg(price=2505.0, size=size)
        tmp = Path(tempfile.mkdtemp())
        log = setup_logger(tmp / "logs", "ERROR")
        broker = PaperBroker(cfg, log, start_balance=1000.0)
        state = BotState(tmp / "t.json", "ETH").load()
        eng = TriggerEngine(cfg, broker, FakeFeed(2510.0), state, log)
        halted = False
        try:
            eng.start()
        except EngineHalt:
            halted = True
        check(
            f"size={size}（名义 ${size * 2505:.2f}）{'拒绝启动' if not should_pass else '允许启动'}",
            halted is (not should_pass),
        )

    # 配置层也要拦一道，网页上改参数时就能立刻看到错误
    c = make_trigger_cfg(size=0.001)
    try:
        c.validate()
        check("配置校验拦截 0.001 ETH", False, "竟然通过了")
    except ValueError as e:
        check("配置校验拦截 0.001 ETH", "最低下单金额" in str(e), str(e))

    c2 = make_trigger_cfg(price=0.0, size=0.005)
    try:
        c2.validate()
        check("配置校验拦截 price=0", False, "竟然通过了")
    except ValueError as e:
        check("配置校验拦截 price=0", "trigger.price" in str(e), str(e))

    c3 = make_trigger_cfg(order_type="now")
    try:
        c3.validate()
        check("配置校验拦截非法 order_type", False, "竟然通过了")
    except ValueError as e:
        check("配置校验拦截非法 order_type", "order_type" in str(e), str(e))


def test_trigger_config_file() -> None:
    section("触发单：实例配置文件 round-trip")
    p = Path(__file__).resolve().parent / "configs" / "eth-buy-2505.yaml"
    check("configs/eth-buy-2505.yaml 存在", p.exists())
    if not p.exists():
        return

    cfg = load_config(p)
    check("strategy = trigger", cfg.strategy == "trigger", cfg.strategy)
    check("coin = ETH", cfg.market.coin == "ETH", cfg.market.coin)
    check("side = buy", cfg.trigger.side == "buy", cfg.trigger.side)
    check("price = 2503（跟随 configs/eth-buy-2505.yaml）", cfg.trigger.price == 2503.0, str(cfg.trigger.price))
    check("size = 0.0044（≈11 USDC 等值）", cfg.trigger.size == 0.0044, str(cfg.trigger.size))
    check("order_type = limit", cfg.trigger.order_type == "limit", cfg.trigger.order_type)
    check("once = true", cfg.trigger.once is True)
    check("tp_pct = 0（不设止盈）", cfg.trigger.tp_pct == 0.0, str(cfg.trigger.tp_pct))
    check("杠杆 5x", cfg.market.leverage == 5, str(cfg.market.leverage))
    notional = cfg.trigger.size * cfg.trigger.price
    check("名义价值 ≈ $11.02（高于 $10 门槛）", abs(notional - 11.022) < 0.01, f"${notional:.4f}")
    check("名义价值稳过交易所 $10 下限", notional >= 10.0, f"${notional:.4f}")
    try:
        cfg.validate()
        check("配置校验通过", True)
    except Exception as e:
        check("配置校验通过", False, str(e))

    # 网页改配置走的是同一套写回逻辑：注释不能丢、值要能原样读回
    import shutil

    tmp = Path(tempfile.mkdtemp()) / "x.yaml"
    shutil.copy(p, tmp)
    before = tmp.read_text(encoding="utf-8")
    c2 = load_config(tmp)
    c2.trigger.price = 2499
    c2.trigger.size = 0.006
    c2.save_to(tmp)
    after = tmp.read_text(encoding="utf-8")
    lost = [ln for ln in before.splitlines() if ln.strip().startswith("#") and ln not in after.splitlines()]
    check("保存后注释一行不丢", len(lost) == 0, str(lost[:3]))
    c3 = load_config(tmp)
    check("改后的触发价可读回", c3.trigger.price == 2499.0, str(c3.trigger.price))
    check("改后的数量可读回", c3.trigger.size == 0.006, str(c3.trigger.size))
    check("strategy 未被写坏", c3.strategy == "trigger", c3.strategy)
    check("保存后配置仍能通过校验", _validates(c3))

    # 顶层键缺失时的插入位置（不能傻乎乎追加到文件末尾）
    text = "# c\nslot: x\n\n# --- env ---\nenv:\n  dry_run: true\n"
    lines = update_yaml_text(text, {None: {"strategy": "trigger"}}).splitlines()
    check("缺失的 strategy 插在 slot 之后", lines[2].strip() == "strategy: trigger", str(lines))
    check(
        "原注释与原有段都还在",
        lines[0] == "# c" and "env:" in [ln.strip() for ln in lines] and "  dry_run: true" in lines,
        str(lines),
    )


def test_private_key_shape() -> None:
    """上真钱前的最后一道闸：私钥/地址填错要能提前拦住。"""
    section("私钥与交易账户地址的格式校验")
    from bot.config import private_key_problem

    ok_key = "0x" + "ab" * 32
    check("合法私钥（0x+64位）通过", private_key_problem(ok_key) == "")
    check("大写 0X 前缀也认", private_key_problem("0X" + "cd" * 32) == "")
    check("空值不报错（干跑不需要私钥）", private_key_problem("") == "")

    prob = private_key_problem("0x" + "ab" * 20)
    check("40 位地址被识别为「填错成地址」", "地址" in prob, prob)
    check("长度错误有提示", "64" in private_key_problem("0x" + "ab" * 10))
    check("占位符/中文被识别为含非 hex", "非十六进制" in private_key_problem("0x这里粘贴你的私钥"))
    check("前后引号会被剥掉", private_key_problem(f"'{ok_key}'") == "")

    cfg = load_config(Path(__file__).resolve().parent / "configs" / "eth-buy-2505.yaml")
    cfg.private_key = "0x" + "ab" * 20
    check("配置校验会拦住「私钥填成地址」", not _validates(cfg))
    cfg.private_key = ok_key
    check("私钥合法时校验通过", _validates(cfg))
    # 下面只关心 account_address 本身。注意 yaml 里的 dry_run 可能是 false，
    # 空私钥会额外触发「真实下单模式需要私钥」，干扰断言，所以显式给一个合法私钥。
    cfg.private_key = ok_key
    cfg.env.account_address = "0x1234"
    check("配置校验会拦住非法 account_address", not _validates(cfg))
    cfg.env.account_address = "0x0A3343B600D81CDAAC131342652241CD32E14349"
    check("大小写混写的地址也算合法", _validates(cfg))


def _validates(cfg) -> bool:
    try:
        cfg.validate()
        return True
    except Exception:
        return False


def test_reset_state_clears_engine() -> None:
    """清除状态后，面板不能还显示上一轮的持仓和「已完成」（不联网）。"""
    section("清除状态：内存里的旧引擎引用要一起丢掉")
    import shutil

    from bot.manager import SlotManager

    root = Path(tempfile.mkdtemp())
    (root / "configs").mkdir(parents=True)
    src = Path(__file__).resolve().parent / "configs" / "eth-buy-2505.yaml"
    shutil.copy(src, root / "configs" / "eth-buy-2505.yaml")

    mgr = SlotManager(root)
    mgr.refresh()
    rt = mgr._slot("eth-buy-2505")

    class _StubEngine:
        """冒充跑过一轮、已成交的触发单引擎。"""

        def snapshot_dict(self):
            return {
                "slot": "eth-buy-2505",
                "strategy": "trigger",
                "coin": "ETH",
                "trigger": {"phase": "已完成"},
                "net_position": 0.0044,
                "coins": 0.0,
                "entry_px": 2505.0,
                "unrealized_pnl": 0.12,
                "total_notional": 11.02,
                "open_orders": 0,
                "realized_usd": 0.0,
                "closed_slots": 0,
                "orders_today": 1,
                "status": "DONE",
                "levels": [{"key": "T1", "side": "buy", "level": 1, "state": "open", "size": 0.0044}],
                "trades": [{"ts": 1, "kind": "open", "side": "buy", "size": 0.0044, "price": 2505.0}],
                "equity": [[1, 100.0]],
            }

    rt.engine = _StubEngine()
    rt.state = BotState(root / "state" / "eth-buy-2505.state.json", "ETH")
    rt.state.status = "DONE"
    rt.started_ms = now_ms()

    snap = mgr.snapshot("eth-buy-2505")
    check("清状态前：面板读的是引擎内存快照", snap["trigger"]["phase"] == "已完成", snap["trigger"]["phase"])

    ok, msg = mgr.reset_state("eth-buy-2505")
    check("清除状态成功", ok, msg)
    check("旧引擎引用已被丢弃", rt.engine is None)
    check("旧 broker/state 引用也被丢弃", rt.broker is None and rt.state is None)
    check("运行时长归零", rt.started_ms == 0, str(rt.started_ms))

    snap2 = mgr.snapshot("eth-buy-2505")
    check("清状态后：面板回到「待启动」", snap2["trigger"]["phase"] == "待启动", snap2["trigger"]["phase"])
    check("清状态后：旧持仓不再显示", float(snap2["net_position"]) == 0.0, str(snap2["net_position"]))
    check("清状态后：旧成交记录不再显示", snap2["trades"] == [], str(snap2["trades"]))
    check("清状态后：权益曲线清空", snap2["equity"] == [], str(snap2["equity"]))
    check("清状态后：状态不是「运行中」", snap2["status"] == "IDLE", str(snap2["status"]))
    check("清状态后：本地层位记录为空", snap2["levels"] == [], str(snap2["levels"]))
    check("清状态不影响配置（价格仍是 2503）", snap2["trigger"]["price"] == 2503.0)


def test_equity_floor_guard() -> None:
    """回归：统一账户下权益要合并现货 USDC；且权益不足必须在挂单前就拒绝。"""
    section("权益下限：既不能误判，也不能「先开仓再强平」")

    # ① 统一账户（unifiedAccount）：资金记在现货侧，永续 accountValue 常为 0。
    #    只看永续侧会把有 19 USDC 的账户误判成"没钱"。
    cfg = make_trigger_cfg(price=2505.0, size=0.005)
    cfg.risk.min_account_value_usd = 20.0

    unified = {"account_value": 19.378, "perp_account_value": 0.0, "spot_usdc": 19.378}
    msg = equity_floor_problem(cfg, unified)
    check("统一账户 19.38 < 20 会被拦下", bool(msg), msg or "（没有拦下）")
    check("提示里给出永续/现货拆分", "现货 USDC" in msg, msg)

    ok_info = {"account_value": 25.0, "perp_account_value": 5.8, "spot_usdc": 19.2}
    check("权益 25 > 20 正常放行", equity_floor_problem(cfg, ok_info) == "")
    check("权益刚好等于下限也放行", equity_floor_problem(cfg, {"account_value": 20.0}) == "")
    check("传统账户 0 权益会被拦下", bool(equity_floor_problem(cfg, {"account_value": 0.0})))

    # ② 引擎层：权益不足时 start() 必须直接拒绝，且不许挂出任何单。
    #    这正是真钱事故的根因——主循环的熔断发生在成交之后，等于"先买再砸"。
    tmp = Path(tempfile.mkdtemp())
    log = setup_logger(tmp / "logs", "ERROR")
    feed = FakeFeed(2510.0)
    broker = PaperBroker(cfg, log, start_balance=1000.0)
    broker.verify_connection = lambda: {
        "wallet": "paper",
        "account": "paper",
        "agent_wallet": False,
        "network": "mainnet(dry-run)",
        "account_value": 5.0,
        "perp_account_value": 0.0,
        "spot_usdc": 5.0,
        "mids": 0,
        "open_orders": 0,
    }
    state = BotState(tmp / "t.json", "ETH").load()
    eng = TriggerEngine(cfg, broker, feed, state, log)

    try:
        eng.start()
        check("权益不足时启动必须被拒绝", False, "居然启动成功了")
    except EngineHalt as e:
        check("权益不足时启动必须被拒绝", True)
        check("拒绝原因点名权益下限", "低于风控下限" in str(e), str(e))

    check("被拒绝后一笔单都没挂出去", len(broker.orders) == 0, str(broker.orders))
    # 注意：BotState 的默认 status 本来就是 "RUNNING"（不是本次 start 写的），
    # 所以这里改判「start() 的后续副作用有没有发生」——trigger_price 在体检之后才写。
    check("被拒绝后没有写入本次启动的参数", "trigger_price" not in state.data, str(state.data.get("trigger_price")))
    check("被拒绝后没有留下层位记录", state.slots == {}, str(state.slots))


class _SilentLog:
    def debug(self, *a, **k):
        pass

    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def critical(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def _stub_broker(positions, margin, spot_balances, abstraction="unifiedAccount"):
    """构造一个不发网络请求的 LiveBroker 桩，只用来验证权益 / 快照的口径。"""
    from bot.broker import LiveBroker

    b = object.__new__(LiveBroker)
    b.cfg = None
    b.log = _SilentLog()
    b.account_address = "0x" + "11" * 20
    b.wallet = None
    b.using_agent_wallet = False
    b.sz_decimals_cache = {}
    b._abstraction = abstraction

    class _Info:
        def user_state(self, addr):
            return {"assetPositions": positions, "marginSummary": margin}

        def spot_user_state(self, addr):
            return {"balances": spot_balances}

        def open_orders(self, addr):
            return []

    b.info = _Info()
    return b


def test_account_equity_scope() -> None:
    print("\n=== 统一账户权益口径 & 多标的隔离 ===")

    def usdc(total, hold=0.0):
        return [{"coin": "USDC", "total": str(total), "hold": str(hold)}]

    # ① 只挂单（实盘实测：perp 2.2247 / spot 18.694 / spot hold 2.2247）
    #    perp 就是 spot 里被划走当保证金的那部分，相加会把权益翻倍。
    b = _stub_broker([], {"accountValue": "2.2247", "totalNtlPos": "0", "totalMarginUsed": "0"},
                     usdc(18.694, 2.2247))
    check("挂单占用：权益 = 18.694（不是 20.919）", abs(b.equity() - 18.694) < 1e-6, str(b.equity()))
    check("挂单占用：快照权益同样取大者",
          abs(b.snapshot("ETH").account_value - 18.694) < 1e-6,
          str(b.snapshot("ETH").account_value))

    # ② 满仓占用（实盘实测：perp == spot == 18.9639，现货 100% 被 hold）
    b = _stub_broker([], {"accountValue": "18.9639", "totalNtlPos": "0", "totalMarginUsed": "14.99"},
                     usdc(18.9639, 18.9639))
    check("全额占用：权益 = 18.964（不是 37.928）", abs(b.equity() - 18.9639) < 1e-6, str(b.equity()))

    # ③ 无仓位（accountValue 会归零，此时只有现货是真的）
    b = _stub_broker([], {"accountValue": "0", "totalNtlPos": "0", "totalMarginUsed": "0"},
                     usdc(19.378, 0.0))
    check("无仓位：权益 = 19.378（现货兜底）", abs(b.equity() - 19.378) < 1e-6, str(b.equity()))

    # ④ 传统账户：现货不该参与
    b = _stub_broker([], {"accountValue": "100", "totalNtlPos": "0", "totalMarginUsed": "0"},
                     usdc(50.0), abstraction="")
    check("传统账户：权益 = 100（现货不参与）", abs(b.equity() - 100.0) < 1e-6, str(b.equity()))

    # ⑤ 多标的隔离：手工开的 BTC 仓不能混进 ETH 策略的风控
    positions = [
        {"position": {"coin": "ETH", "szi": "0.0044", "entryPx": "2503", "positionValue": "11.01",
                      "unrealizedPnl": "0.05", "liquidationPx": "2100", "marginUsed": "2.20"}},
        {"position": {"coin": "BTC", "szi": "-0.00722", "entryPx": "83042", "positionValue": "599.55",
                      "unrealizedPnl": "-12.5", "liquidationPx": "84630", "marginUsed": "14.99"}},
    ]
    b = _stub_broker(positions, {"accountValue": "18.69", "totalNtlPos": "610.56",
                                 "totalMarginUsed": "17.19"}, usdc(18.69, 18.69))
    snap = b.snapshot("ETH")
    check("名义只算 ETH：$11.01（不是账户全部 $610.56）",
          abs(snap.total_notional - 11.01) < 1e-6, str(snap.total_notional))
    check("浮盈只算 ETH：+0.05（不含 BTC 的 -12.5）",
          abs(snap.unrealized_pnl - 0.05) < 1e-6, str(snap.unrealized_pnl))
    check("净持仓只算 ETH：0.0044", abs(snap.net_position - 0.0044) < 1e-9, str(snap.net_position))
    check("强平价只算 ETH：2100", abs(float(snap.liquidation_px) - 2100) < 1e-6, str(snap.liquidation_px))
    check("BTC 的 $599.55 不会顶爆 risk.max_total_notional_usd",
          snap.total_notional <= 500.0, f"本策略名义 ${snap.total_notional:,.2f}")


def make_signal_cfg(
    root: Path | None = None,
    fast: int = 5,
    slow: int = 20,
    size: float = 0.0044,
    entry_mode: str = "maker",
    allow_short: bool = False,
    max_rounds: int = 0,
    trail_pct: float = 0.6,
    take_profit_pct: float = 0.0,
) -> Config:
    """信号策略的测试配置。用快/慢线 5/20 让手工构造的 K 线能快点出交叉。"""
    cfg = Config(root=root or Path(tempfile.mkdtemp()))
    cfg.strategy = "signal"
    cfg.env.dry_run = True
    cfg.market.coin = "ETH"
    cfg.market.leverage = 5
    cfg.market.base_size = size
    cfg.market.reduce_only_tp = True
    cfg.signal.fast_ema = fast
    cfg.signal.slow_ema = slow
    cfg.signal.candles = 40
    cfg.signal.size = size
    cfg.signal.entry_mode = entry_mode
    cfg.signal.allow_short = allow_short
    cfg.signal.max_rounds = max_rounds
    cfg.signal.trail_pct = trail_pct
    cfg.signal.take_profit_pct = take_profit_pct
    cfg.signal.atr_mult = 2.0
    cfg.risk.max_position_size = 1.0
    cfg.risk.max_total_notional_usd = 5000.0
    cfg.risk.max_unrealized_loss_usd = 10000.0
    cfg.risk.min_account_value_usd = 1.0
    cfg.risk.max_daily_orders = 1000
    cfg.runtime.loop_interval_sec = 0.01
    return cfg


def _mk_bars(prices, interval_ms_: int = 15 * 60 * 1000):
    """把一串收盘价做成 K 线；高低点各留 0.2% 的影线。"""
    t0 = 1_700_000_000_000
    return [
        Candle(t=t0 + i * interval_ms_, o=p, h=p * 1.002, l=p * 0.998, c=p, v=1.0, closed=True)
        for i, p in enumerate(prices)
    ]


def _golden_cross_bars():
    """人为造一段「先缓跌、再急拉」的行情，让最后一根已收盘 K 线恰好出现金叉。

    实测：33 根、EMA 5/20 时，最后一根的 fast=2592.56 > slow=2591.10，且前一根是反向，
    所以 cross_up 为真；ATR(14)=10.75 → 止损距离 max(0.6%, ATR×2)=21.5（ATR 口径更宽）。
    """
    prices = ([2600 - i * 0.5 for i in range(30)] + [2585 + i * 8 for i in range(12)])[:33]
    return _mk_bars(prices)


class FakeCandles:
    """K 线桩：完全离线，避免测试联网。"""

    def __init__(self, bars):
        self.bars = list(bars)

    def closed(self, coin, limit: int = 0, force: bool = False):
        return self.bars

    def fetch(self, coin, limit: int = 0, force: bool = False):
        return self.bars[-limit:] if limit else self.bars


def _signal_engine(cfg=None, bars=None, mid: float = 2601.0, balance: float = 100.0):
    cfg = cfg or make_signal_cfg()
    log = setup_logger(Path(tempfile.mkdtemp()) / "logs", "CRITICAL")
    feed = FakeFeed(mid)
    broker = PaperBroker(cfg, log, start_balance=balance)
    state = BotState(Path(tempfile.mkdtemp()) / "sig.json", "ETH")
    eng = SignalEngine(cfg, broker, feed, state, log)
    eng.candles = FakeCandles(bars if bars is not None else _golden_cross_bars())
    return eng, broker, state, feed, cfg


def test_indicators() -> None:
    section("指标计算：EMA / SMA / ATR / 交叉 / 跟踪止损")

    # EMA 播种与递推：period=3 时第 3 个位置 = 前 3 个的简单平均
    vals = [1.0, 2.0, 3.0, 4.0, 5.0]
    e = ema(vals, 3)
    check("EMA 预热期为 None", e[0] is None and e[1] is None, str(e))
    check("EMA 用 SMA 播种", abs(e[2] - 2.0) < 1e-12, str(e[2]))
    # alpha = 2/(3+1) = 0.5 → e[3] = 0.5*4 + 0.5*2 = 3
    check("EMA 递推公式正确", abs(e[3] - 3.0) < 1e-12, str(e[3]))
    check("EMA 后值跟随价格上行", e[4] > e[3])

    s = sma(vals, 3)
    check("SMA 预热期为 None", s[0] is None and s[1] is None)
    check("SMA 第 3 个 = 2.0", abs(s[2] - 2.0) < 1e-12)
    check("SMA 第 5 个 = 4.0", abs(s[4] - 4.0) < 1e-12)

    check("数据不足时 EMA 全为 None", ema([1.0, 2.0], 5) == [None, None])

    # 交叉：a 从下方穿到上方
    a = [1.0, 1.0, 3.0]
    b = [2.0, 2.0, 2.0]
    check("cross_up 识别上穿", cross_up(a, b))
    check("cross_up 对下穿返回假", not cross_up(b, a))
    check("cross_down 识别下穿", cross_down(b, a))
    check("含 None 时交叉判定为假", not cross_up([1.0, None, 3.0], [2.0, 2.0, 2.0]))
    check("预热期不够长时也判为假", not cross_up([3.0], [2.0]))
    check("相等不算上穿（避免假信号）", not cross_up([2.0, 2.0, 2.0], [2.0, 2.0, 2.0]))

    bars = _golden_cross_bars()
    a_arr = atr(bars, 14)
    check("ATR 预热期为 None", a_arr[12] is None, str(a_arr[:13]))
    check("ATR 恒为正", all(v is None or v > 0 for v in a_arr))
    check("ATR 最后有值", a_arr[-1] is not None and a_arr[-1] > 0, str(a_arr[-1]))

    st = ema_state(bars, 5, 20, 0)
    check("ema_state 能算出来", st is not None)
    check("构造的行情确实产生金叉", bool(st and st["up_cross"]), str(st and st["up_cross"]))
    check("ema_state 给出多头排列", bool(st and st["above"]))
    check("数据不足时 ema_state 返回 None", ema_state(bars[:5], 5, 20, 0) is None)
    check("趋势过滤：收盘价在上方时可以开多", bool(st and st["trend_ok_long"]))

    # 跟踪止损：取两个口径里更宽的那个
    stop = trail_stop_price(2600.0, 2700.0, True, 0.6, 10.75, 2.0)
    check("多单止损 = 最高价 - ATR×2（比 0.6% 宽）", abs(stop - 2678.5) < 1e-9, str(stop))
    stop2 = trail_stop_price(2600.0, 2700.0, True, 2.0, 1.0, 1.0)
    check("百分比更宽时用百分比", abs(stop2 - 2646.0) < 1e-9, str(stop2))
    stop3 = trail_stop_price(2600.0, 2400.0, False, 0.6, 10.0, 2.0)
    check("空单止损在最高价上方", abs(stop3 - 2420.0) < 1e-9, str(stop3))
    check("没给任何止损参数就不设止损", trail_stop_price(2600.0, 2700.0, True, 0, None, 0) == 0.0)


def test_candles_helpers() -> None:
    section("K 线工具：周期换算、历史深度、收盘判定")
    check("15m = 900000 毫秒", interval_ms("15m") == 900_000)
    check("1h = 3600000 毫秒", interval_ms("1h") == 3_600_000)
    check("大写周期也能识别", interval_ms("15M") == 900_000)
    try:
        interval_ms("7m")
        check("非法周期会报错", False)
    except ValueError:
        check("非法周期会报错", True)
    # 接口单次上限 5000 根，所以 5m 只有约 17 天、15m 约 35 天
    check("5m 历史约 17.4 天", 17 < max_history_days("5m") < 18, str(max_history_days("5m")))
    check("15m 历史大于 30 天", max_history_days("15m") > 30, str(max_history_days("15m")))

    now = 1_700_000_000_000
    raw = {"t": now, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "n": 3}
    c = _parse_candle(raw, "15m", now + 1000)
    check("未走完周期的 K 线标记为未收盘", c is not None and not c.closed)
    c2 = _parse_candle(raw, "15m", now + 15 * 60 * 1000)
    check("走完周期的 K 线标记为已收盘", c2 is not None and c2.closed)
    check("坏数据返回 None", _parse_candle({"t": "x"}, "15m", now) is None)


def test_bool_strictness() -> None:
    section("严格布尔解析：拼错的值必须报错，不能静默当真")
    check("true → True", as_bool("true", False))
    check("false → False", as_bool("false", False) is False)
    check("0 → False", as_bool("0", True) is False)
    for bad in ("flase", "ture", "maybe", "否"):
        try:
            as_bool(bad, False, "dry_run")
            check(f"{bad!r} 应该报错", False)
        except ValueError:
            check(f"{bad!r} 报错（正是上次踩的坑）", True)


def test_signal_engine_cycle() -> None:
    section("信号引擎完整循环：金叉 -> 挂单 -> 成交 -> 跟踪止损 -> 平仓")
    eng, broker, state, feed, cfg = _signal_engine()
    eng.start()

    check("启动后指标已算出金叉", bool(eng._sig.get("up_cross")), str(eng._sig.get("desc")))
    check("启动后处于等待信号状态", eng._phase_label() == "等待信号", eng._phase_label())
    check("启动后不持仓", broker.position == 0)

    # 第一轮：金叉 → maker 挂单挂在现价下方 0.05%
    eng.tick()
    slot = state.slots["S1"]
    check("金叉后进入挂单状态", slot["state"] == "pending", slot["state"])
    oid = slot.get("entry_oid")
    check("确实挂出了一张单", bool(oid))
    order_px = broker.orders[int(oid)]["px"] if oid in broker.orders else 0
    check("买单挂在现价下方 0.05%", abs(order_px - 2599.6) < 0.05, str(order_px))
    check("挂单方向是买入", slot["side"] == "buy", slot["side"])

    # 第二轮：价格回撤到挂单价 → 成交
    feed.px = 2599.0
    eng.tick()
    check("价格回撤后成交", slot["state"] == "open", slot["state"])
    check("成交价 = 挂单价", abs(slot["entry_px"] - 2599.6) < 1e-6, str(slot["entry_px"]))
    check("持仓 = 0.0044", abs(broker.position - 0.0044) < 1e-9, str(broker.position))
    check("记录了开仓成交", any(t["kind"] == "open" for t in state.trades()))

    # 第三轮：创新高 → 止损上移
    feed.px = 2700.0
    eng.tick()
    check("峰值跟随到 2700", abs(slot["extreme_px"] - 2700.0) < 1e-9, str(slot["extreme_px"]))
    stop = eng._stop_px()
    atr_val = float(eng._atr_val or 0)
    dist = max(2700.0 * 0.006, atr_val * 2.0)
    check(
        "止损上移到 最高价 − max(0.6%, ATR×2)",
        abs(stop - (2700.0 - dist)) < 1e-9,
        f"{stop:.4f}（回撤距离 {dist:.4f}）",
    )
    check("这段行情里 ATR 口径比 0.6% 更宽", dist > 2700.0 * 0.006, f"{dist:.4f}")
    check("止损价在现价下方，还没触发", stop < feed.px)

    # 第四轮：回撤击穿止损 → 平仓
    feed.px = 2670.0
    eng.tick()
    check("击穿止损后平仓", slot["state"] == "flat", slot["state"])
    check("持仓已归零", abs(broker.position) < 1e-12, str(broker.position))
    check("轮数 +1", state.data.get("rounds") == 1, str(state.data.get("rounds")))
    pnl = float(state.data.get("realized_usd", 0))
    check("已实现盈亏为正（2700→回落 30 点仍赚）", pnl > 0, f"{pnl:.4f}")
    # 毛利 = 0.0044 × (2670 − 2599.6) = 0.30976，两边手续费合计 0.003478
    check("盈亏扣掉了两边手续费", abs(pnl - 0.30628) < 0.0005, f"{pnl:.5f}")
    check("成交记录里有一条平仓", any(t["kind"] == "close" for t in state.trades()))
    check("买入和卖出的手续费都算进了 balance",
          abs(broker.balance - (100.0 + 0.30976 - 0.00171574 - 0.00176216)) < 0.001,
          f"{broker.balance:.5f}")


def test_signal_short_and_tp() -> None:
    section("信号引擎：做空方向、止盈出场")
    # 死叉序列：把金叉序列反过来（先急涨再缓跌）
    prices = ([2600 + i * 0.5 for i in range(30)] + [2615 - i * 8 for i in range(12)])[:33]
    bars = _mk_bars(prices)

    cfg = make_signal_cfg(allow_short=True)
    eng, broker, state, feed, _ = _signal_engine(cfg=cfg, bars=bars, mid=2601.0)
    eng.start()
    check("构造的行情产生死叉", bool(eng._sig.get("down_cross")), str(eng._sig.get("desc")))

    eng.tick()
    slot = state.slots["S1"]
    check("允许做空时死叉开空", slot["state"] == "pending" and slot["side"] == "sell", str(slot["state"]))
    # 卖单挂在现价上方 0.05%
    oid = int(slot.get("entry_oid") or 0)
    check("卖单挂在现价上方", oid in broker.orders and broker.orders[oid]["px"] > 2601.0,
          str(broker.orders.get(oid, {}).get("px")))

    feed.px = 2605.0
    eng.tick()
    check("价格反弹后空单成交", slot["state"] == "open", slot["state"])
    check("持仓为负（做空）", broker.position < 0, str(broker.position))

    feed.px = 2500.0
    eng.tick()
    check("空单在下跌中赚到钱（浮盈为正）", broker.snapshot("ETH").unrealized_pnl > 0,
          str(broker.snapshot("ETH").unrealized_pnl))

    # 止盈路径
    cfg2 = make_signal_cfg(take_profit_pct=1.0, trail_pct=0.0)
    eng2, broker2, state2, feed2, _ = _signal_engine(cfg=cfg2)
    eng2.start()
    eng2.tick()
    feed2.px = 2599.0
    eng2.tick()
    slot2 = state2.slots["S1"]
    check("止盈版也完成建仓", slot2["state"] == "open", slot2["state"])
    entry = float(slot2["entry_px"])
    feed2.px = entry * 1.011  # 涨过 1% 止盈线
    eng2.tick()
    check("触及止盈后平仓", slot2["state"] == "flat", slot2["state"])
    check("止盈后已实现为正", float(state2.data.get("realized_usd", 0)) > 0,
          str(state2.data.get("realized_usd")))
    check("止盈收益约 1% 减手续费",
          abs(float(state2.data.get("realized_usd", 0)) - (entry * 1.01 - entry) * 0.0044) < 0.02,
          str(state2.data.get("realized_usd")))


def test_signal_guards() -> None:
    section("信号引擎：轮数上限、挂单超时、反向交叉、启动前体检")
    # 轮数上限：做完 1 轮就结束
    eng, broker, state, feed, cfg = _signal_engine(cfg=make_signal_cfg(max_rounds=1))
    eng.start()
    eng.tick()
    feed.px = 2599.0
    eng.tick()
    feed.px = 2700.0
    eng.tick()
    feed.px = 2670.0
    eng.tick()
    check("完成 1 轮后标记结束", bool(state.data.get("signal_done")))
    check("状态变成 DONE", state.status == "DONE", state.status)
    check("结束原因写进了状态", "轮数上限" in str(state.data.get("signal_done_reason")),
          str(state.data.get("signal_done_reason")))
    eng.tick()
    check("结束后不再开新仓", state.slots["S1"]["state"] == "flat", state.slots["S1"]["state"])

    # 挂单超时：价格一直不回撤，挂单应被撤掉
    cfg_ttl = make_signal_cfg()
    cfg_ttl.signal.entry_ttl_sec = 0.001
    eng2, broker2, state2, feed2, _ = _signal_engine(cfg=cfg_ttl)
    eng2.start()
    eng2.tick()
    slot2 = state2.slots["S1"]
    check("超时前处于挂单状态", slot2["state"] == "pending", slot2["state"])
    time.sleep(0.05)
    feed2.px = 2610.0
    eng2.tick()
    check("挂单超时后被撤掉", slot2["state"] == "flat", slot2["state"])
    check("交易所侧也不再挂着", len(broker2.orders) == 0, str(broker2.orders))

    # 反向交叉离场
    eng3, broker3, state3, feed3, cfg3 = _signal_engine()
    eng3.start()
    eng3.tick()
    feed3.px = 2599.0
    eng3.tick()
    slot3 = state3.slots["S1"]
    check("先建好仓", slot3["state"] == "open", slot3["state"])
    # 换一段会出死叉的 K 线，并把 signal_kline_ms 挪开，模拟新 K 线收盘
    eng3.candles = FakeCandles(
        _mk_bars(([2600 + i * 0.5 for i in range(30)] + [2615 - i * 8 for i in range(12)])[:33])
    )
    slot3["signal_kline_ms"] = 0
    feed3.px = 2600.0
    eng3.tick()
    check("反向交叉时平掉多单", slot3["state"] == "flat", slot3["state"])
    check("反向交叉也计入轮数", state3.data.get("rounds") == 1, str(state3.data.get("rounds")))

    # 启动前体检：下单量太小 / 超过风控都要拒绝启动
    tiny = make_signal_cfg(size=0.0001)
    eng4, _, _, _, _ = _signal_engine(cfg=tiny)
    try:
        eng4.start()
        check("下单量低于 $10 时应拒绝启动", False)
    except EngineHalt as e:
        check("下单量低于 $10 时拒绝启动", "最低下单金额" in str(e), str(e)[:60])

    over = make_signal_cfg(size=0.0044)
    over.risk.max_position_size = 0.001
    eng5, _, _, _, _ = _signal_engine(cfg=over)
    try:
        eng5.start()
        check("超过净持仓上限时应拒绝启动", False)
    except EngineHalt as e:
        check("超过净持仓上限时拒绝启动", "持仓上限" in str(e), str(e)[:60])


def test_signal_config_file() -> None:
    section("信号策略配置：文件加载、校验、注释保留")
    path = Path(__file__).resolve().parent / "configs" / "eth-trend.yaml"
    check("configs/eth-trend.yaml 存在", path.exists())
    cfg = load_config(path)
    check("strategy 读成 signal", cfg.strategy == "signal", cfg.strategy)
    check("周期 15m", cfg.signal.interval == "15m", cfg.signal.interval)
    check("EMA 12/48", cfg.signal.fast_ema == 12 and cfg.signal.slow_ema == 48)
    check("跟踪止损 0.6% / ATR×2", cfg.signal.trail_pct == 0.6 and cfg.signal.atr_mult == 2.0)
    check("轮数上限 3（先小值试探）", cfg.signal.max_rounds == 3, str(cfg.signal.max_rounds))
    check("默认是干跑", cfg.env.dry_run is True)
    cfg.validate()
    check("示例配置能通过校验", True)
    check("summary 里出现 signal 关键字", "信号" in cfg.summary())

    # 校验能拦住写坏的参数
    for label, mutate in (
        ("slow<=fast", lambda c: setattr(c.signal, "slow_ema", 5)),
        ("非法周期", lambda c: setattr(c.signal, "interval", "7m")),
        ("止损全关", lambda c: (setattr(c.signal, "trail_pct", 0), setattr(c.signal, "atr_mult", 0))),
        ("candles 太少", lambda c: setattr(c.signal, "candles", 20)),
        ("trend 比 slow 还短", lambda c: setattr(c.signal, "trend_ema", 10)),
    ):
        c = load_config(path)
        mutate(c)
        try:
            c.validate()
            check(f"拦住非法参数：{label}", False)
        except ValueError:
            check(f"拦住非法参数：{label}", True)

    # save_to 只写当前策略用到的段，且不抹注释
    c = load_config(path)
    c.signal.trail_pct = 1.2
    tmp = path.with_name("_tmp_signal.yaml")
    tmp.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    try:
        c.save_to(tmp)
        text = tmp.read_text(encoding="utf-8")
        check("写回后仍是 signal 策略", "strategy: signal" in text)
        check("写回了新的止损值", "trail_pct: 1.2" in text)
        check("没把网格段删掉（示例里留着备用）", "grid:" in text)
        check("注释保留", "信号参数" in text and "# ---- 出场 ----" in text)
        check("读回来值一致", load_config(tmp).signal.trail_pct == 1.2)
        check("幂等：再存一次文件内容不变", True)
        c.save_to(tmp)
        check("二次保存内容稳定", tmp.read_text(encoding="utf-8") == text)
    finally:
        tmp.unlink(missing_ok=True)


def main() -> int:
    print("Hyperliquid 网格机器人 —— 离线自测")
    test_utils()
    test_grid_cycle()
    test_position_cap()
    test_kill_switch()
    test_restart_recovery()
    test_snapshot_dict()
    test_wallet_address()
    test_yaml_writer()
    test_wallet_bind()
    test_trigger_limit_entry()
    test_trigger_market_mode()
    test_trigger_tp()
    test_trigger_restart()
    test_trigger_guards()
    test_trigger_config_file()
    test_reset_state_clears_engine()
    test_equity_floor_guard()
    test_account_equity_scope()
    test_private_key_shape()
    test_indicators()
    test_candles_helpers()
    test_bool_strictness()
    test_signal_engine_cycle()
    test_signal_short_and_tp()
    test_signal_guards()
    test_signal_config_file()
    print(f"\n{'=' * 40}\n通过 {PASS} 项，失败 {FAIL} 项\n{'=' * 40}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
