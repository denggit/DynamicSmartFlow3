#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/22/2026
@File       : rate_limit.py
@Description: Alchemy RPC 调用速率限制，避免 startup/并发 burst 导致 429。
              串行化所有 Alchemy RPC 调用，每次调用间隔至少 ALCHEMY_MIN_INTERVAL_SEC。
"""

import asyncio
import time

_last_call_time: float = 0.0
_lock = asyncio.Lock()


def _get_interval() -> float:
    """延迟加载配置。"""
    try:
        from config.settings import ALCHEMY_MIN_INTERVAL_SEC
        return float(ALCHEMY_MIN_INTERVAL_SEC)
    except Exception:
        return 0.5


async def wait_before_request() -> None:
    """
    Alchemy RPC 请求前调用：等待满足最小间隔，并登记本次调用开始时间。
    """
    interval = _get_interval()
    async with _lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        global _last_call_time
        _last_call_time = time.monotonic()
