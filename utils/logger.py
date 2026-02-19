#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/18/2026
@File       : logger.py
@Description: 统一日志工具
              - 日志按日期分目录: logs/YYYY-MM-DD/，主程序 main.log，其他按模块分文件
              - 猎手交易监控单独写入 monitor.log，便于查看什么时间、交易哪个币
              - 每日零点后自动切到新日期目录，不依赖程序启动时间
              - 关键模块的 ERROR/exception 触发告警邮件，1 小时一封、整合该时段内所有错误
"""

import logging
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import List, Optional

# 项目根目录 (utils 的父级)
_BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_ROOT = _BASE_DIR / "logs"

# 已创建的 logger 实例，避免重复添加 handler
_logger_handlers: dict = {}

from config.settings import CRITICAL_EMAIL_COOLDOWN_SEC as _CRITICAL_EMAIL_COOLDOWN_SEC
_critical_error_buffer: List[dict] = []
_buffer_lock = threading.Lock()
_flush_timer: Optional[threading.Timer] = None

# 触发严重错误邮件的 logger 名称（跟单/交易/风控/主流程等）
_CRITICAL_LOGGER_NAMES = frozenset({
    "Main",
    "services.solana.trader",
    "services.solana.hunter_agent",
    "services.solana.hunter_monitor",
    "services.helius.sm_searcher",
    "services.risk_control",
    "services.dexscreener.dex_scanner",
})


def _send_buffered_critical_errors() -> None:
    """
    将当前缓冲区的所有错误整合为一封邮件发送，并清空缓冲区。
    由定时器在 1 小时后调用，或在加锁后手动调用。
    """
    global _critical_error_buffer, _flush_timer
    with _buffer_lock:
        to_send = list(_critical_error_buffer)
        _critical_error_buffer.clear()
        _flush_timer = None
    if not to_send:
        return
    try:
        lines = [
            "本邮件汇总过去 1 小时内所有 ERROR/exception 记录（按时间排序）。",
            "",
            "=" * 60,
        ]
        for i, entry in enumerate(to_send, 1):
            lines.append("")
            lines.append(f"-------- 错误 #{i} | {entry.get('time', '')} --------")
            lines.append(f"模块: {entry.get('name', '')}")
            lines.append(f"级别: {entry.get('level', '')}")
            lines.append("消息:")
            lines.append(entry.get("message", ""))
            exc = entry.get("exc", "").strip()
            if exc:
                lines.append("堆栈:")
                lines.append(exc)
        content = "\n".join(lines)
        subject = f"过去 1 小时共 {len(to_send)} 条错误"
        from services.notification import send_critical_error_email
        send_critical_error_email(subject, content)
    except Exception:
        pass  # 避免告警逻辑自身抛错影响主流程


def _schedule_flush_if_first() -> None:
    """若当前缓冲区只有一条（刚写入），则启动 1 小时后发送的定时器。"""
    global _flush_timer
    with _buffer_lock:
        if len(_critical_error_buffer) != 1:
            return
        if _flush_timer is not None:
            _flush_timer.cancel()
        _flush_timer = threading.Timer(_CRITICAL_EMAIL_COOLDOWN_SEC, _send_buffered_critical_errors)
        _flush_timer.daemon = True
        _flush_timer.start()


def _logger_name_to_file_name(name: str) -> str:
    """
    Logger 名称 -> 日志文件名（不含扩展名）。
    主程序 Main -> main.log；猎手交易专用 trade -> monitor.log；其余取最后一段。
    """
    if not name or name == "root":
        return "app"
    if name == "Main":
        return "main"
    if name == "trade":
        return "monitor"
    parts = name.split(".")
    return parts[-1].lower() if parts else "app"


class DateDirFileHandler(logging.FileHandler):
    """
    按日期目录 + 文件名写入: logs/YYYY-MM-DD/<file_name>.log。
    每次 emit 时检查当前日期，若日期变化则切换到新日期目录下的文件。
    """

    def __init__(self, file_name: str, logs_root: Path):
        """
        :param file_name: 文件名（不含 .log），如 main、monitor、trader
        :param logs_root: 日志根目录，其下按日期建子目录 YYYY-MM-DD
        """
        self._file_name = file_name
        self._logs_root = Path(logs_root)
        self._current_date = date.today()
        self._current_path = Path(self._compute_path())
        super().__init__(str(self._current_path), encoding="utf-8")

    def _compute_path(self) -> str:
        """根据当前日期计算路径 logs/YYYY-MM-DD/<file_name>.log，并确保目录存在。"""
        today = date.today()
        date_dir = self._logs_root / today.isoformat()
        date_dir.mkdir(parents=True, exist_ok=True)
        path = date_dir / f"{self._file_name}.log"
        return str(path)

    def _ensure_stream(self):
        """若日期变化则关闭旧文件并打开新日期的文件。"""
        today = date.today()
        if self._current_date is None or self._current_date != today:
            if self.stream:
                self.stream.close()
                self.stream = None
            self._current_date = today
            self._current_path = Path(self._compute_path())
            self.baseFilename = str(self._current_path)
            self.stream = self._open()

    def emit(self, record: logging.LogRecord):
        """写入前确保写入的是当日目录下的文件。"""
        try:
            self._ensure_stream()
            super().emit(record)
        except Exception:
            self.handleError(record)


class CriticalErrorEmailHandler(logging.Handler):
    """
    当收到 ERROR/CRITICAL 时将记录写入缓冲区。
    每 1 小时最多发一封邮件，整合该时段内所有错误（时间 + 模块 + 消息 + 堆栈）。
    """

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.ERROR:
            return
        try:
            time_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record.created))
            exc_text = ""
            if record.exc_info and record.exc_info[0] is not None and self.formatter:
                exc_text = self.formatter.formatException(record.exc_info)
            entry = {
                "time": time_str,
                "name": record.name,
                "level": record.levelname,
                "message": record.getMessage(),
                "exc": exc_text,
            }
            with _buffer_lock:
                _critical_error_buffer.append(entry)
                need_schedule = len(_critical_error_buffer) == 1
            if need_schedule:
                _schedule_flush_if_first()
        except Exception:
            self.handleError(record)


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    获取按日期目录分文件的 Logger：logs/YYYY-MM-DD/<file_name>.log。
    主程序用 get_logger("Main") -> main.log；猎手交易用 get_logger("trade") -> monitor.log。
    同一 name 多次调用返回同一实例，且只挂一次 DateDirFileHandler。

    :param name: 通常传 __name__，或 "Main"、"trade"
    :param level: 日志级别
    :return: 配置好的 Logger
    """
    logger = logging.getLogger(name)
    if not logger.handlers and name not in _logger_handlers:
        logger.setLevel(level)
        _logger_handlers[name] = True
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        logger.propagate = False

        # 文件：按日期目录写入
        file_name = _logger_name_to_file_name(name)
        file_handler = DateDirFileHandler(file_name, LOGS_ROOT)
        file_handler.setLevel(level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        # 控制台：同时输出到命令行
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        # 关键业务模块：ERROR/exception 写入缓冲区，每 1 小时发一封整合邮件
        if name in _CRITICAL_LOGGER_NAMES:
            email_handler = CriticalErrorEmailHandler(level=logging.ERROR)
            email_handler.setFormatter(
                logging.Formatter("%(asctime)s\n%(message)s", datefmt="%Y-%m-%d %H:%M:%S")
            )
            logger.addHandler(email_handler)
    return logger
