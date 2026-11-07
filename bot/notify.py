"""告警通知（Telegram）。

只做一件事：把重要事件推给用户。发送失败绝不影响交易主流程，
所有异常都被吞掉并降级成日志。
"""

from __future__ import annotations

import threading
from typing import Optional

import requests

from .utils import fmt_usd


class Notifier:
    def __init__(self, cfg, logger, slot: str = ""):
        self.cfg = cfg
        self.log = logger
        self.slot = slot
        n = getattr(cfg, "notify", None)
        self.enabled = bool(n and n.enabled and n.telegram_bot_token and n.telegram_chat_id)
        self.token = getattr(n, "telegram_bot_token", "") if n else ""
        self.chat_id = getattr(n, "telegram_chat_id", "") if n else ""
        self.on_fill = bool(getattr(n, "on_fill", False)) if n else False
        self.on_stop = bool(getattr(n, "on_stop", True)) if n else True

    # ------------------------------------------------------------ 发送
    def send(self, text: str) -> None:
        if not self.enabled:
            return
        # 后台线程发送，避免网络抖动拖慢交易主循环
        threading.Thread(target=self._post, args=(text,), daemon=True).start()

    def _post(self, text: str) -> None:
        head = f"【网格机器人{('·' + self.slot) if self.slot else ''}】"
        try:
            requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": head + "\n" + text, "disable_notification": False},
                timeout=8,
            )
        except Exception as e:  # 通知失败不能影响交易
            self.log.debug("Telegram 通知发送失败：%s", e)

    # ------------------------------------------------------------ 快捷事件
    def started(self, coin: str, network: str, account_value: float, dry_run: bool) -> None:
        self.send(
            f"▶️ 已启动\n标的：{coin}\n网络：{network}{'（干跑模拟）' if dry_run else '（实盘）'}\n"
            f"账户权益：${fmt_usd(account_value)}"
        )

    def stopped(self, coin: str, reason: str) -> None:
        self.send(f"⏹ 已停止\n标的：{coin}\n原因：{reason}")

    def grid_closed(self, key: str, pnl: float, total: float) -> None:
        if not self.on_fill:
            return
        self.send(f"💰 网格 {key} 止盈成交\n本笔盈亏：{pnl:+.4f}\n累计：{total:+.4f}")

    def killed(self, coin: str, reason: str) -> None:
        self.send(f"🚨 触发熔断\n标的：{coin}\n原因：{reason}\n已撤单并市价平仓，bot 已停机")

    def error(self, msg: str) -> None:
        self.send(f"⚠️ 运行异常\n{msg}")

    # ------------------------------------------------------------ 测试
    def test(self) -> bool:
        if not (self.token and self.chat_id):
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{self.token}/sendMessage",
                json={"chat_id": self.chat_id, "text": "【网格机器人】通知测试成功 ✅"},
                timeout=10,
            )
            return bool(r.ok and r.json().get("ok"))
        except Exception as e:
            self.log.warning("通知测试失败：%s", e)
            return False
