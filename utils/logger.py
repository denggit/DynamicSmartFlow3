#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/18/2026
@File       : logger.py
@Description: 统一日志工具
              - 日志写入项目根目录 logs/ 下
              - 按日期分目录: logs/YYYY-MM-DD/
              - 每个功能模块对应独立日志文件: <module>.log
              - 每日零点后自动写入新日期目录，不依赖程序启动时间
"""

import logging
from datetime import date
from pathlib import Path

# 项目根目录 (utils 的父级)
_BASE_DIR = Path(__file__).resolve().parent.parent
LOGS_ROOT = _BASE_DIR / "logs"

# 已创建的 logger 实例，避免重复添加 handler
_logger_handlers: dict = {}


def _module_name_to_file_name(name: str) -> str:
    """
    将 logger 名称转为日志文件名（不含扩展名）。
    例如: "main" -> "main", "services.dexscreener.dex_scanner" -> "dex_scanner"
    """
    if not name or name == "root":
        return "app"
    parts = name.split(".")
    return parts[-1].lower() if parts else "app"


class DateDirFileHandler(logging.FileHandler):
    """
    按日期目录写入的 FileHandler。
    每次 emit 时检查当前日期，若日期变化则切换到新目录下的文件，
    保证跨天运行时日志自动落入当日目录。
    """

    def __init__(self, module_name: str, logs_root: Path):
        """
        :param module_name: 模块名，用于生成文件名 <module_name>.log
        :param logs_root: 日志根目录，其下按日期建子目录
        """
        self._module_name = module_name
        self._logs_root = Path(logs_root)
        self._current_date = date.today()
        self._current_path = Path(self._compute_path())
        super().__init__(str(self._current_path), encoding="utf-8")

    def _compute_path(self) -> str:
        """根据当前日期计算日志文件路径，并确保目录存在。"""
        today = date.today()
        date_dir = self._logs_root / today.isoformat()
        date_dir.mkdir(parents=True, exist_ok=True)
        path = date_dir / f"{self._module_name}.log"
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


def get_logger(name: str, level: int = logging.INFO) -> logging.Logger:
    """
    获取按日期与模块分文件的 Logger。
    同一 name 多次调用返回同一实例，且只挂一次 DateDirFileHandler。

    :param name: 通常传 __name__，或模块标识如 "Main", "Trader"
    :param level: 日志级别
    :return: 配置好的 Logger
    """
    logger = logging.getLogger(name)
    if not logger.handlers and name not in _logger_handlers:
        logger.setLevel(level)
        # 避免重复添加
        _logger_handlers[name] = True
        file_name = _module_name_to_file_name(name)
        handler = DateDirFileHandler(file_name, LOGS_ROOT)
        handler.setLevel(level)
        formatter = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        handler.setFormatter(formatter)
        logger.propagate = False
        logger.addHandler(handler)
    return logger
