#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : http.py
@Description: Helius HTTP API 调用封装。
              提供解析交易、地址交易列表等 REST 接口。
"""

from typing import Dict, List, Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)


class HeliusHttp:
    """
    Helius HTTP API 模块。
    封装 https://api.helius.xyz/v0 下的 REST 接口。
    """

    BASE_URL = "https://api.helius.xyz/v0"
    DEFAULT_TIMEOUT = 30.0
    DEFAULT_CHUNK_SIZE = 100  # Helius 单次最多 100 笔，100 credits/次，凑满更省

    def __init__(self, key_pool):
        """
        :param key_pool: 需实现 get_api_key(), get_http_endpoint(), mark_current_failed(), size
        """
        self._pool = key_pool

    def get_http_endpoint(self) -> str:
        """获取交易解析端点：POST /v0/transactions/?api-key=..."""
        return self._pool.get_http_endpoint()

    def get_api_key(self) -> str:
        """获取当前 API Key。"""
        return self._pool.get_api_key()

    def mark_current_failed(self) -> None:
        """标记当前 Key 不可用。"""
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size

    async def fetch_parsed_transactions(
        self,
        signatures: List,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> List[Dict]:
        """
        批量拉取 Helius 解析后的交易（POST /v0/transactions）。
        每批最多 100 笔，429 时切换 Key 重试。

        :param signatures: 签名列表，支持 dict 或 str（dict 取 signature 字段）
        :param chunk_size: 每批数量
        :return: 解析后的交易列表，顺序与签名对应
        """
        sigs_clean = []
        for s in signatures:
            if isinstance(s, dict):
                sig = s.get("signature")
            elif isinstance(s, str):
                sig = s
            else:
                sig = None
            if sig:
                sigs_clean.append(sig)

        if not sigs_clean:
            return []

        all_txs = []
        own_client = None
        client = http_client
        if client is None:
            own_client = httpx.AsyncClient()
            client = own_client

        try:
            for i in range(0, len(sigs_clean), chunk_size):
                batch = sigs_clean[i : i + chunk_size]
                payload = {"transactions": batch}
                url = f"{self.BASE_URL}/transactions?api-key={self.get_api_key()}"
                try:
                    resp = await client.post(url, json=payload, timeout=timeout)
                    if resp.status_code == 200:
                        all_txs.extend(resp.json() or [])
                    elif resp.status_code == 429 and self.size > 1:
                        self.mark_current_failed()
                        url = f"{self.BASE_URL}/transactions?api-key={self.get_api_key()}"
                        resp2 = await client.post(url, json=payload, timeout=timeout)
                        if resp2.status_code == 200:
                            all_txs.extend(resp2.json() or [])
                except Exception:
                    logger.exception("fetch_parsed_transactions 批量请求异常")
        finally:
            if own_client is not None:
                await own_client.aclose()

        return all_txs

    async def get_address_transactions(
        self,
        address: str,
        limit: int = 1,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[List[Dict]]:
        """
        获取地址交易列表（GET /v0/addresses/{address}/transactions）。
        用于健康检查等。

        :param address: 钱包地址
        :param limit: 返回数量
        """
        url = f"{self.BASE_URL}/addresses/{address}/transactions"
        params = {"api-key": self.get_api_key(), "limit": limit}
        own_client = None
        client = http_client
        if client is None:
            own_client = httpx.AsyncClient()
            client = own_client

        try:
            resp = await client.get(url, params=params, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429 and self.size > 1:
                self.mark_current_failed()
                resp2 = await client.get(
                    url, params={"api-key": self.get_api_key(), "limit": limit}, timeout=timeout
                )
                if resp2.status_code == 200:
                    return resp2.json()
            return None
        except Exception:
            logger.exception("get_address_transactions 异常")
            return None
        finally:
            if own_client is not None:
                await own_client.aclose()
