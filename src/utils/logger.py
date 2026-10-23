"""日志配置：控制台 + logs/app.log，并确保永不打印完整 token。"""

from __future__ import annotations

import io
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

_CONFIGURED = False


def force_utf8_stdio() -> None:
    """Windows 控制台默认是 GBK，中文日志会乱码/报错。"""
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
        elif hasattr(stream, "buffer"):
            try:
                setattr(sys, stream_name,
                        io.TextIOWrapper(stream.buffer, encoding="utf-8", errors="replace"))
            except Exception:
                pass


def setup_logging(
    log_file: str | os.PathLike[str] = "logs/app.log",
    level: str | int = logging.INFO,
    console: bool = True,
    root: str | os.PathLike[str] | None = None,
) -> logging.Logger:
    """初始化根 logger（幂等）。返回根 logger。"""
    global _CONFIGURED

    force_utf8_stdio()

    logger = logging.getLogger()
    if _CONFIGURED:
        return logger

    if isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)
    logger.setLevel(level)
    logger.handlers.clear()

    formatter = logging.Formatter(DEFAULT_FORMAT, datefmt=DEFAULT_DATEFMT)

    path = Path(log_file)
    if not path.is_absolute() and root is not None:
        path = Path(root) / path
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # 常驻几个月也不能让日志无限增长：单文件 10MB，最多 5 个
        file_handler = RotatingFileHandler(
            path, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # 例如目录只读时不应让程序崩溃
        logger.warning("无法创建日志文件 %s: %s", path, exc)

    if console:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

    # httpx/httpcore 每个请求都打 INFO，太吵，压到 WARNING
    for noisy in ("httpx", "httpcore", "anyio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _CONFIGURED = True
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


#: 常驻运行日志的模块标签（Day 5 要求）
LOG_TAGS = {
    "client": "[CLIENT]",
    "lcu": "[LCU]",
    "gameflow": "[GAMEFLOW]",
    "champion": "[CHAMPION]",
    "opgg": "[OPGG]",
    "ocr": "[OCR]",
    "augment": "[AUGMENT]",
    "flow": "[FLOW]",
    "overlay": "[OVERLAY]",
}


class TaggedLogger(logging.LoggerAdapter):
    """给日志加统一标签，例如 [OPGG] / [OCR]。"""

    def __init__(self, logger: logging.Logger, tag: str) -> None:
        super().__init__(logger, {})
        self.tag = tag

    def process(self, msg, kwargs):
        return "%s %s" % (self.tag, msg), kwargs


def get_tagged(tag: str, name: str = "") -> TaggedLogger:
    """取带标签的 logger；tag 传 'opgg' / 'ocr' 等短名。"""
    label = LOG_TAGS.get(tag, "[%s]" % tag.upper())
    return TaggedLogger(logging.getLogger(name or ("tag." + tag)), label)
