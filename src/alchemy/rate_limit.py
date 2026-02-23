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
from typing import Awaitable, Callable, TypeVar

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


async def with_alchemy_rate_limit(coro_or_factory: Awaitable[T] | Callable[[], Awaitable[T]]) -> T:
    """
    在 Alchemy 限流下执行协程。
    用于绕过 alchemy_client 的 Alchemy 调用（如 Trader 的 solana-py AsyncClient）。
    限流会先 wait 再执行，若在 wait 期间有其他协程关闭/重建了所用资源，直接传入的协程
    可能持有旧引用导致 "Cannot send a request, as the client has been closed"。

    【重要】涉及可被关闭重建的资源（如 Trader.rpc_client）时，必须传入 Callable 延迟创建：
        lambda: self.rpc_client.send_transaction(...)  # 正确：执行时再创建协程
        self.rpc_client.send_transaction(...)         # 错误：协程在 wait 前已创建，可能用已关闭的 client

    :param coro_or_factory: 协程，或返回协程的无参 Callable（如 lambda: self.rpc_client.xxx(...)）
    :return: 协程返回值
    """
    await wait_before_request()
    if callable(coro_or_factory) and not asyncio.iscoroutine(coro_or_factory):
        coro_or_factory = coro_or_factory()
    return await coro_or_factory
