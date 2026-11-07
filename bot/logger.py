"""日志配置：控制台 + 文件 + 内存环形缓冲（供网页实时查看）。"""

from __future__ import annotations

import logging
import sys
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Deque, Optional


class RingBufferHandler(logging.Handler):
    """把最近若干条日志留在内存里，网页可以随时取走。"""

    def __init__(self, maxlen: int = 400):
        super().__init__()
        self.buffer: Deque[str] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass

    def tail(self, n: int = 200) -> list:
        return list(self.buffer)[-n:]


def _ensure_utf8_stdout() -> None:
    # Windows 控制台默认可能是 GBK，中文日志会炸
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def setup_logger(
    log_dir: Path,
    level: str = "INFO",
    name: str = "gridbot",
    with_console: bool = True,
) -> logging.Logger:
    """创建（或复用）一个具名 logger。"""
    _ensure_utf8_stdout()

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()
    logger.propagate = False

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    if with_console:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        logger.addHandler(console)

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        fileh = logging.FileHandler(log_dir / f"bot-{datetime.now():%Y-%m-%d}.log", encoding="utf-8")
        fileh.setFormatter(fmt)
        logger.addHandler(fileh)
    except Exception:
        pass

    return logger


def attach_ring_buffer(logger: logging.Logger, maxlen: int = 400) -> RingBufferHandler:
    """给 logger 挂一个内存缓冲并返回它。重复调用会先移除旧的。"""
    for h in list(logger.handlers):
        if isinstance(h, RingBufferHandler):
            logger.removeHandler(h)
    h = RingBufferHandler(maxlen=maxlen)
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
    logger.addHandler(h)
    return h


def detach_ring_buffers(logger: Optional[logging.Logger]) -> None:
    if logger is None:
        return
    for h in list(logger.handlers):
        if isinstance(h, RingBufferHandler):
            logger.removeHandler(h)
