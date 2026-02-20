#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : http.py
@Description: Alchemy Solana HTTP 相关封装。
              Alchemy 无 Helius 风格的增强解析交易 API，此处通过 RPC getTransaction 批量拉取。
              返回格式与 Helius 不同，需由上层做适配；或推荐使用 Helius 做 parsed 场景。
"""

import asyncio
from typing import Dict, List, Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)


class AlchemyHttp:
    """
    Alchemy Solana HTTP 模块。
    Alchemy 无 api.helius.xyz 式的增强 API，fetch_parsed_transactions 通过 RPC 批量 getTransaction 实现。
    返回 RPC 标准格式（非 Helius 增强格式），消费方需兼容。
    """

    DEFAULT_TIMEOUT = 30.0
    DEFAULT_CHUNK_SIZE = 20  # RPC 单次不宜过大

    def __init__(self, key_pool, rpc_module):
        """
        :param key_pool: 需实现 get_api_key(), mark_current_failed(), size
        :param rpc_module: AlchemyRpc 实例，用于批量 getTransaction
        """
        self._pool = key_pool
        self._rpc = rpc_module

    def get_api_key(self) -> str:
        return self._pool.get_api_key()

    def mark_current_failed(self) -> None:
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size

    def get_http_endpoint(self) -> str:
        """Alchemy 无独立 HTTP 解析端点，返回空。"""
        return ""

    async def fetch_parsed_transactions(
        self,
        signatures: List,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> List[Dict]:
        """
        通过 RPC getTransaction 批量拉取交易。
        返回 RPC 标准格式（含 transaction/meta），非 Helius 增强格式。
        需要 Helius 风格 tokenTransfers/nativeTransfers 时请使用 HeliusClient。
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
        for i in range(0, len(sigs_clean), chunk_size):
            batch = sigs_clean[i : i + chunk_size]
            tasks = [self._rpc.get_transaction(sig, http_client=http_client, timeout=timeout) for sig in batch]
            results = await asyncio.gather(*tasks)
            for tx in results:
                if tx:
                    all_txs.append(tx)

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
        通过 getSignaturesForAddress + getTransaction 实现。
        返回 RPC 格式交易列表。
        """
        sigs = await self._rpc.get_signatures_for_address(
            address, limit=limit, http_client=http_client, timeout=timeout
        )
        if not sigs:
            return []
        sig_list = [s.get("signature") if isinstance(s, dict) else s for s in sigs[:limit] if s]
        return await self.fetch_parsed_transactions(
            sig_list, http_client=http_client, chunk_size=limit, timeout=timeout
        )
