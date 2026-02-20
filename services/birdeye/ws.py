#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : ws.py
@Description: Birdeye WebSocket 封装。
              wss://public-api.birdeye.so/socket/solana?x-api-key={key}
              支持 SUBSCRIBE_PRICE、SUBSCRIBE_TOKEN_STATS 等实时订阅。
"""


class BirdeyeWs:
    """
    Birdeye WebSocket 模块。
    提供 WebSocket URL，用于实时价格、Token Stats 等订阅。
    """

    BASE_WS_URL = "wss://public-api.birdeye.so/socket/solana"

    def __init__(self, key_pool):
        """
        :param key_pool: 需实现 get_api_key(), mark_current_failed(), size
        """
        self._pool = key_pool

    def get_wss_url(self) -> str:
        """获取 WebSocket URL。"""
        key = self._pool.get_api_key()
        if not key:
            return ""
        return f"{self.BASE_WS_URL}?x-api-key={key}"

    def mark_current_failed(self) -> None:
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size
