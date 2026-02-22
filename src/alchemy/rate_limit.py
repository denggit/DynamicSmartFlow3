#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/22/2026
@File       : rate_limit.py
@Description: Alchemy RPC 全局速率限制。
              串行化所有 Alchemy RPC 调用（含 alchemy_client 与 Trader 的 AsyncClient），
              每次调用间隔至少 ALCHEMY_MIN_INTERVAL_SEC，避免 429 超限。
              新增 Alchemy 调用路径时，必须经过本模块限流。
"""

import asyncio
import time
from typing import Awaitable, TypeVar

_last_call_time: float = 0.0
_lock = asyncio.Lock()

T = TypeVar("T")


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
    所有 Alchemy 调用（alchemy_client、Trader AsyncClient 等）必须经此限流。
    """
    global _last_call_time
    interval = _get_interval()
    async with _lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)
        _last_call_time = time.monotonic()


async def with_alchemy_rate_limit(coro: Awaitable[T]) -> T:
    """
    在 Alchemy 限流下执行协程。
    用于绕过 alchemy_client 的 Alchemy 调用（如 Trader 的 solana-py AsyncClient）。

    :param coro: 待执行的协程（如 self.rpc_client.get_signature_statuses([sig])）
    :return: 协程返回值
    """
    await wait_before_request()
    return await coro
