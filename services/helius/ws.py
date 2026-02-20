#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : ws.py
@Description: Helius WebSocket 相关封装。
              提供 WebSocket URL 获取，供 transactionSubscribe、logsSubscribe 等订阅使用。
              实际连接与消息处理由调用方（如 hunter_monitor、hunter_agent）自行实现。
"""


class HeliusWs:
    """
    Helius WebSocket 模块。
    封装 WebSocket URL 获取。连接建立、订阅、消息循环由消费者负责。
    """

    def __init__(self, key_pool):
        """
        :param key_pool: 需实现 get_wss_url(), mark_current_failed(), size
        """
        self._pool = key_pool

    def get_wss_url(self) -> str:
        """
        获取当前 Helius WebSocket URL。
        格式：wss://mainnet.helius-rpc.com/?api-key={key}
        """
        return self._pool.get_wss_url()

    def mark_current_failed(self) -> None:
        """标记当前 Key 不可用，WebSocket 429 时由调用方调用。"""
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size
