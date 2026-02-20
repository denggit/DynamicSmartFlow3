#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : client.py
@Description: Birdeye 行情 API 统一客户端。
              组合 price / market / ws 子模块，提供代币价格与市场数据。
"""

from typing import Dict, Optional

import httpx

from config.settings import birdeye_key_pool

from services.birdeye.market import BirdeyeMarket
from services.birdeye.price import BirdeyePrice
from services.birdeye.ws import BirdeyeWs


class BirdeyeClient:
    """
    Birdeye 行情 API 统一客户端。
    提供代币价格、市场数据、WebSocket URL。
    """

    def __init__(self):
        self._pool = birdeye_key_pool
        self._price = BirdeyePrice(key_pool=self._pool)
        self._market = BirdeyeMarket(key_pool=self._pool)
        self._ws = BirdeyeWs(key_pool=self._pool)

    def get_api_key(self) -> str:
        return self._pool.get_api_key()

    def mark_current_failed(self) -> None:
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size

    def get_wss_url(self) -> str:
        """Birdeye WebSocket URL（实时价格/Token Stats）。"""
        return self._ws.get_wss_url()

    # ==================== 价格 ====================

    async def get_token_price(
        self,
        token_address: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[float]:
        """获取代币当前价格（USD）。"""
        return await self._price.get_price(token_address, http_client=http_client, timeout=timeout)

    async def get_price_full(
        self,
        token_address: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[dict]:
        """获取完整价格信息（含 24h 涨跌幅、流动性等）。"""
        return await self._price.get_price_full(token_address, http_client=http_client, timeout=timeout)

    # ==================== 市场数据 ====================

    async def get_token_market_data(
        self,
        token_address: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        """获取代币市场数据（流动性、市值、FDV、持有者等）。"""
        return await self._market.get_token_market_data(
            token_address, http_client=http_client, timeout=timeout
        )


birdeye_client = BirdeyeClient()
