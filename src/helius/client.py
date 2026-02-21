#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : client.py
@Description: Helius API 统一客户端。
              组合 RPC / HTTP / WebSocket 三个子模块，对外提供单一 HeliusClient。
"""

from typing import Any, Dict, List, Optional

import httpx

from config.settings import helius_key_pool

from src.helius.http import HeliusHttp
from src.helius.rpc import HeliusRpc
from src.helius.ws import HeliusWs


class HeliusClient:
    """
    Helius API 统一客户端。
    
    组合 RPC / HTTP / WebSocket 三个子模块，对外提供统一接口：
    - RPC: get_signatures_for_address, get_transaction, get_token_accounts_by_owner, rpc_post
    - HTTP API: fetch_parsed_transactions, get_address_transactions
    - WebSocket: get_wss_url
    - URL / Key: get_rpc_url, get_http_endpoint, get_api_key, mark_current_failed, size
    """

    def __init__(self):
        """使用 config 中的 helius_key_pool，组合三个子模块。"""
        self._pool = helius_key_pool
        self._rpc = HeliusRpc(key_pool=self._pool)
        self._http = HeliusHttp(key_pool=self._pool)
        self._ws = HeliusWs(key_pool=self._pool)

    # ==================== URL 与 Key 池（统一入口）====================

    def get_rpc_url(self) -> str:
        """获取当前 Helius RPC URL（供 solana-py AsyncClient 等使用）。"""
        return self._rpc.get_rpc_url()

    def get_wss_url(self) -> str:
        """获取当前 Helius WebSocket URL。"""
        return self._ws.get_wss_url()

    def get_http_endpoint(self) -> str:
        """获取 Helius HTTP API 交易解析端点。"""
        return self._http.get_http_endpoint()

    def get_api_key(self) -> str:
        """获取当前 API Key。"""
        return self._pool.get_api_key()

    def mark_current_failed(self) -> None:
        """标记当前 Key 不可用，切换到下一个。"""
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        """Key 池大小。"""
        return self._pool.size

    # ==================== RPC（委托给 _rpc）====================

    async def rpc_post(
        self,
        method: str,
        params: list,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = HeliusRpc.DEFAULT_TIMEOUT,
    ) -> Any:
        """执行 JSON-RPC 调用。"""
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
        """获取地址签名列表。"""
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
        """获取交易详情（RPC 格式）。"""
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
        """获取地址在某代币上的账户信息。"""
        return await self._rpc.get_token_accounts_by_owner(
            owner, mint, http_client=http_client, timeout=timeout
        )

    # ==================== HTTP API（委托给 _http）====================

    async def fetch_parsed_transactions(
        self,
        signatures: List,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        chunk_size: int = 90,
        timeout: float = 30.0,
    ) -> List[Dict]:
        """批量拉取 Helius 解析后的交易。"""
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
        """获取地址交易列表（用于健康检查等）。"""
        return await self._http.get_address_transactions(
            address, limit=limit, http_client=http_client, timeout=timeout
        )


# 单例，供全局使用
helius_client = HeliusClient()
