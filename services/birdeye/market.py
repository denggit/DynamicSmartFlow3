#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : market.py
@Description: Birdeye 代币市场数据 API 封装。
              GET /defi/v3/token/market-data?address={token}
"""

from typing import Dict, Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL = "https://public-api.birdeye.so"


class BirdeyeMarket:
    """
    Birdeye 市场数据模块。
    获取代币流动性、市值、FDV、持有者数等。
    """

    DEFAULT_TIMEOUT = 10.0
    MAX_RETRIES = 2

    def __init__(self, key_pool):
        """
        :param key_pool: 需实现 get_api_key(), mark_current_failed(), size
        """
        self._pool = key_pool

    def _headers(self) -> dict:
        return {
            "accept": "application/json",
            "x-chain": "solana",
            "X-API-KEY": self._pool.get_api_key() or "",
        }

    def mark_current_failed(self) -> None:
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size

    async def get_token_market_data(
        self,
        token_address: str,
        *,
        ui_amount_mode: str = "scaled",
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Optional[Dict]:
        """
        获取代币市场数据。
        :return: { address, price, liquidity, total_supply, circulating_supply, market_cap, fdv, holder }
        """
        url = f"{BASE_URL}/defi/v3/token/market-data"
        params = {"address": token_address, "ui_amount_mode": ui_amount_mode}
        own_client = None
        client = http_client
        if client is None:
            own_client = httpx.AsyncClient()
            client = own_client

        try:
            for attempt in range(self.MAX_RETRIES):
                resp = await client.get(url, params=params, headers=self._headers(), timeout=timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("success") and data.get("data"):
                        return data["data"]
                    return None
                if resp.status_code == 429 and self.size > 1:
                    self.mark_current_failed()
                    continue
                logger.warning("Birdeye market-data 请求失败: HTTP %s", resp.status_code)
                return None
        except Exception:
            logger.exception("Birdeye get_token_market_data 异常")
            return None
        finally:
            if own_client is not None:
                await own_client.aclose()

        return None
