#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : ws.py
@Description: Alchemy Solana WebSocket 封装。
              Endpoint: wss://solana-mainnet.g.alchemy.com/v2/{api-key}
"""


class AlchemyWs:
    """
    Alchemy Solana WebSocket 模块。
    提供 WebSocket URL，支持 transactionSubscribe、logsSubscribe 等标准订阅。
    """

    def __init__(self, key_pool):
        """
        :param key_pool: 需实现 get_wss_url(), mark_current_failed(), size
        """
        self._pool = key_pool

    def get_wss_url(self) -> str:
        """获取当前 WebSocket URL。"""
        return self._pool.get_wss_url()

    def mark_current_failed(self) -> None:
        """标记当前 Key 不可用。"""
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size
