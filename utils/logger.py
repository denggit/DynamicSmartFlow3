#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/18/2026
@File       : logger.py
@Description: 统一日志工具
              - 日志按模式分目录: MODELA -> logs/modelA/YYYY-MM-DD/，MODELB -> logs/modelB/YYYY-MM-DD/
              - 合并为 5 个重点模块: main、trade、hunter、risk、api
              - 每日零点后自动切到新日期目录，不依赖程序启动时间
              - 关键模块的 ERROR/exception 触发告警邮件，1 小时一封、整合该时段内所有错误
"""

import logging
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path
from typing import List, Optional

# 项目根目录 (utils 的父级)
_BASE_DIR = Path(__file__).resolve().parent.parent
# 按 HUNTER_MODE 分配日志根目录，启动时确定，与 data/modelA、data/modelB 一致
_HUNTER_MODE = (os.getenv("HUNTER_MODE", "MODELA") or "MODELA").strip().upper()
_LOGS_SUBDIR = "modelB" if _HUNTER_MODE == "MODELB" else "modelA"
LOGS_ROOT = _BASE_DIR / "logs" / _LOGS_SUBDIR

# 已创建的 logger 实例，避免重复添加 handler
_logger_handlers: dict = {}

from config.settings import CRITICAL_EMAIL_COOLDOWN_SEC as _CRITICAL_EMAIL_COOLDOWN_SEC
_critical_error_buffer: List[dict] = []
_buffer_lock = threading.Lock()
_flush_timer: Optional[threading.Timer] = None

# 触发严重错误邮件的 logger 名称（跟单/交易/风控/主流程等）
# 必须与各模块 get_logger(__name__) 传入的名称一致
_CRITICAL_LOGGER_NAMES = frozenset({
    "Main",
    "services.trader",
    "services.hunter_agent",
    "services.hunter_monitor",
    "services.modela",
    "services.modelb",
    "src.rugcheck.risk_control",
    "src.dexscreener.dex_scanner",
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
    合并为 5 个重点模块，避免一模块一文件过于分散：
    - main: 主流程、日报、通知
    - trade: 跟单、交易、监控、猎手信号
    - hunter: 猎手挖掘（modela/modelb）
    - risk: 风控
    - api: 外部 API（DexScreener、Birdeye、RPC、HTTP）
    """
    if not name or name == "root":
        return "main"
    if name == "Main":
        return "main"
    if name.startswith("services.notification"):
        return "main"
    if name.startswith("utils.trading_history"):
        return "main"
    if name == "trade":
        return "trade"
    if name.startswith("services.trader"):
        return "trade"
    if name.startswith("services.hunter_monitor"):
        return "trade"
    if name.startswith("services.hunter_agent"):
        return "trade"
    if name.startswith("services.modela"):
        return "hunter"
    if name.startswith("services.modelb"):
        return "hunter"
    if name.startswith("src.rugcheck"):
        return "risk"
    # src.dexscreener、src.birdeye、src.alchemy、src.helius 等
    if name.startswith("src."):
        return "api"
    return "main"


class DateDirFileHandler(logging.FileHandler):
    """
    按日期目录 + 文件名写入: logs/<modelA|modelB>/YYYY-MM-DD/<file_name>.log。
    每次 emit 时检查当前日期，若日期变化则切换到新日期目录下的文件。
    """

    def __init__(self, file_name: str, logs_root: Path):
        """
        :param file_name: 文件名（不含 .log），如 main、monitor、trader
        :param logs_root: 日志根目录（logs/modelA 或 logs/modelB），其下按日期建子目录 YYYY-MM-DD
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
    获取按日期目录分文件的 Logger：logs/<modelA|modelB>/YYYY-MM-DD/<file_name>.log。
    MODELA 写入 logs/modelA/，MODELB 写入 logs/modelB/，与 data 目录一致。
    5 个重点日志文件: main.log、trade.log、hunter.log、risk.log、api.log。
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

        # 文件：按日期目录写入 logs/<modelA|modelB>/YYYY-MM-DD/<file_name>.log
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
