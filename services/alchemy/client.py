#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : client.py
@Description: Alchemy Solana API 统一客户端。
              组合 RPC / HTTP / WebSocket 子模块，接口与 HeliusClient 对齐。
"""

from typing import Any, Dict, List, Optional

import httpx

from config.settings import alchemy_key_pool

from services.alchemy.http import AlchemyHttp
from services.alchemy.rpc import AlchemyRpc
from services.alchemy.ws import AlchemyWs


class AlchemyClient:
    """
    Alchemy Solana API 统一客户端。
    接口与 HeliusClient 对齐，便于统一入口切换。
    """

    def __init__(self):
        self._pool = alchemy_key_pool
        self._rpc = AlchemyRpc(key_pool=self._pool)
        self._http = AlchemyHttp(key_pool=self._pool, rpc_module=self._rpc)
        self._ws = AlchemyWs(key_pool=self._pool)

    def get_rpc_url(self) -> str:
        return self._rpc.get_rpc_url()

    def get_wss_url(self) -> str:
        return self._ws.get_wss_url()

    def get_http_endpoint(self) -> str:
        return self._http.get_http_endpoint()

    def get_api_key(self) -> str:
        return self._pool.get_api_key()

    def mark_current_failed(self) -> None:
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size

    async def rpc_post(
        self,
        method: str,
        params: list,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ) -> Any:
        return await self._rpc.rpc_post(method, params, http_client=http_client, timeout=timeout)

    async def get_signatures_for_address(
        self,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 30.0,
    ) -> Optional[List[Dict]]:
        return await self._rpc.get_signatures_for_address(
            address, limit=limit, before=before, http_client=http_client, timeout=timeout
        )

    async def get_transaction(
        self,
        signature: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        return await self._rpc.get_transaction(
            signature, http_client=http_client, timeout=timeout
        )

    async def get_token_accounts_by_owner(
        self,
        owner: str,
        mint: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        return await self._rpc.get_token_accounts_by_owner(
            owner, mint, http_client=http_client, timeout=timeout
        )

    async def fetch_parsed_transactions(
        self,
        signatures: List,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        chunk_size: int = 20,
        timeout: float = 30.0,
    ) -> List[Dict]:
        """通过 RPC getTransaction 批量拉取，返回 RPC 格式（非 Helius 增强格式）。"""
        return await self._http.fetch_parsed_transactions(
            signatures, http_client=http_client, chunk_size=chunk_size, timeout=timeout
        )

    async def get_address_transactions(
        self,
        address: str,
        limit: int = 1,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[List[Dict]]:
        return await self._http.get_address_transactions(
            address, limit=limit, http_client=http_client, timeout=timeout
        )


alchemy_client = AlchemyClient()
