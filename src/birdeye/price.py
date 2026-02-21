#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : price.py
@Description: Birdeye 代币价格 API 封装。
              GET /defi/price?address={token}
"""

from typing import Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)

BASE_URL = "https://public-api.birdeye.so"


class BirdeyePrice:
    """
    Birdeye 价格模块。
    获取代币当前价格（USD）、24h 涨跌幅、流动性等。
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

    async def get_price(
        self,
        token_address: str,
        *,
        include_liquidity: bool = False,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Optional[float]:
        """
        获取代币当前价格（USD）。
        :return: 价格，失败或无数据返回 None
        """
        url = f"{BASE_URL}/defi/price"
        params = {"address": token_address, "include_liquidity": str(include_liquidity).lower()}
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
                        return float(data["data"].get("value", 0) or 0)
                    return None
                if resp.status_code == 429 and self.size > 1:
                    self.mark_current_failed()
                    continue
                logger.warning("Birdeye price 请求失败: HTTP %s", resp.status_code)
                return None
        except Exception:
            logger.exception("Birdeye get_price 异常")
            return None
        finally:
            if own_client is not None:
                await own_client.aclose()

        return None

    async def get_price_full(
        self,
        token_address: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Optional[dict]:
        """
        获取完整价格信息（含 priceChange24h、liquidity、priceInNative 等）。
        """
        url = f"{BASE_URL}/defi/price"
        params = {"address": token_address, "include_liquidity": "true"}
        own_client = None
        client = http_client
        if client is None:
            own_client = httpx.AsyncClient()
            client = own_client

        try:
            resp = await client.get(url, params=params, headers=self._headers(), timeout=timeout)
            if resp.status_code == 200:
                data = resp.json()
                if data.get("success"):
                    return data.get("data")
                return None
            if resp.status_code == 429 and self.size > 1:
                self.mark_current_failed()
            return None
        except Exception:
            logger.exception("Birdeye get_price_full 异常")
            return None
        finally:
            if own_client is not None:
                await own_client.aclose()
