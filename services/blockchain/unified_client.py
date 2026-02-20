#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : unified_client.py
@Description: Solana RPC 统一客户端。
              合并 Helius 与 Alchemy，根据配置选择主用/备用，提供单一入口。
"""

from typing import Any, Dict, List, Optional

import httpx

from config.settings import (
    SOLANA_PRIMARY_PROVIDER,
    helius_key_pool,
    alchemy_key_pool,
)

from services.helius import helius_client
from services.alchemy import alchemy_client
from services.birdeye import birdeye_client

from utils.logger import get_logger

logger = get_logger(__name__)


class SolanaClient:
    """
    Solana 统一客户端。
    
    合并 Helius、Alchemy、Birdeye：
    - RPC/WS：Helius、Alchemy
    - 行情/价格：Birdeye（get_token_price、get_token_market_data）
    """

    def __init__(self):
        self._helius = helius_client
        self._alchemy = alchemy_client
        self._birdeye = birdeye_client
        self._primary_provider = SOLANA_PRIMARY_PROVIDER

        if self._primary_provider == "helius":
            self._primary = self._helius
            self._fallback = self._alchemy
        elif self._primary_provider == "alchemy":
            self._primary = self._alchemy
            self._fallback = self._helius
        else:
            # auto: Helius 有 Key 则主用，否则 Alchemy
            if helius_key_pool.size > 0:
                self._primary = self._helius
                self._fallback = self._alchemy
            elif alchemy_key_pool.size > 0:
                self._primary = self._alchemy
                self._fallback = self._helius
            else:
                self._primary = self._helius
                self._fallback = self._alchemy

    def _current(self):
        """当前主用客户端。"""
        return self._primary

    def get_rpc_url(self) -> str:
        """获取当前主用 RPC URL。"""
        return self._primary.get_rpc_url()

    def get_wss_url(self) -> str:
        """获取当前主用 WebSocket URL。"""
        return self._primary.get_wss_url()

    def get_http_endpoint(self) -> str:
        """获取解析交易 HTTP 端点（Helius 有，Alchemy 无）。"""
        return self._primary.get_http_endpoint()

    def get_api_key(self) -> str:
        return self._primary.get_api_key()

    def mark_current_failed(self) -> None:
        """主用失败时切换 Key；若 Key 用尽可考虑切备用（此处仅切 Key）。"""
        self._primary.mark_current_failed()

    @property
    def size(self) -> int:
        return self._primary.size

    def helius(self):
        """获取 Helius 客户端（需增强解析时可直接用）。"""
        return self._helius

    def alchemy(self):
        """获取 Alchemy 客户端。"""
        return self._alchemy

    def birdeye(self):
        """获取 Birdeye 客户端（行情/价格）。"""
        return self._birdeye

    # ==================== RPC（委托给主用）====================

    async def rpc_post(
        self,
        method: str,
        params: list,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ) -> Any:
        result = await self._primary.rpc_post(method, params, http_client=http_client, timeout=timeout)
        if result is not None:
            return result
        if self._fallback.size > 0:
            logger.info("主用 RPC 失败，切换备用 (Alchemy/Helius)")
            return await self._fallback.rpc_post(method, params, http_client=http_client, timeout=timeout)
        return None

    async def get_signatures_for_address(
        self,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ) -> Optional[List[Dict]]:
        result = await self._primary.get_signatures_for_address(
            address, limit=limit, before=before, http_client=http_client, timeout=timeout
        )
        if result is not None:
            return result
        if self._fallback.size > 0:
            return await self._fallback.get_signatures_for_address(
                address, limit=limit, before=before, http_client=http_client, timeout=timeout
            )
        return None

    async def get_transaction(
        self,
        signature: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        result = await self._primary.get_transaction(
            signature, http_client=http_client, timeout=timeout
        )
        if result is not None:
            return result
        if self._fallback.size > 0:
            return await self._fallback.get_transaction(
                signature, http_client=http_client, timeout=timeout
            )
        return None

    async def get_token_accounts_by_owner(
        self,
        owner: str,
        mint: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        result = await self._primary.get_token_accounts_by_owner(
            owner, mint, http_client=http_client, timeout=timeout
        )
        if result is not None:
            return result
        if self._fallback.size > 0:
            return await self._fallback.get_token_accounts_by_owner(
                owner, mint, http_client=http_client, timeout=timeout
            )
        return None

    # ==================== HTTP API ====================

    async def fetch_parsed_transactions(
        self,
        signatures: List,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        chunk_size: int = 90,
        timeout: float = 30.0,
    ) -> List[Dict]:
        """
        批量拉取解析后的交易。
        优先 Helius（增强格式 tokenTransfers/nativeTransfers），fallback 用 Alchemy RPC 格式。
        """
        if self._helius.size > 0:
            txs = await self._helius.fetch_parsed_transactions(
                signatures, http_client=http_client, chunk_size=chunk_size, timeout=timeout
            )
            if txs:
                return txs
        if self._alchemy.size > 0:
            return await self._alchemy.fetch_parsed_transactions(
                signatures, http_client=http_client, chunk_size=min(20, chunk_size), timeout=timeout
            )
        return []

    async def get_address_transactions(
        self,
        address: str,
        limit: int = 1,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[List[Dict]]:
        result = await self._primary.get_address_transactions(
            address, limit=limit, http_client=http_client, timeout=timeout
        )
        if result is not None:
            return result
        if self._fallback.size > 0:
            return await self._fallback.get_address_transactions(
                address, limit=limit, http_client=http_client, timeout=timeout
            )
        return None

    # ==================== Birdeye 行情 ====================

    async def get_token_price(
        self,
        token_address: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[float]:
        """获取代币当前价格（USD），由 Birdeye 提供。"""
        if self._birdeye.size > 0:
            return await self._birdeye.get_token_price(
                token_address, http_client=http_client, timeout=timeout
            )
        return None

    async def get_token_market_data(
        self,
        token_address: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        """获取代币市场数据（流动性、市值、FDV 等），由 Birdeye 提供。"""
        if self._birdeye.size > 0:
            return await self._birdeye.get_token_market_data(
                token_address, http_client=http_client, timeout=timeout
            )
        return None


solana_client = SolanaClient()
