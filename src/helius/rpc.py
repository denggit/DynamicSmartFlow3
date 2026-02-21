#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : rpc.py
@Description: Helius RPC 调用封装。
              提供 JSON-RPC 通用调用及常用方法：getSignaturesForAddress、getTransaction、
              getTokenAccountsByOwner 等。429 限流时自动切换 Key 并重试。
"""

import asyncio
from typing import Any, Dict, List, Optional

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)


class HeliusRpc:
    """
    Helius RPC 模块。
    封装 JSON-RPC 调用，依赖 Key 池提供 RPC URL 与 429 切换。
    """

    DEFAULT_TIMEOUT = 30.0
    MAX_RETRIES = 3
    BASE_DELAY = 1.0

    def __init__(self, key_pool):
        """
        :param key_pool: 需实现 get_rpc_url(), mark_current_failed(), size
        """
        self._pool = key_pool

    def get_rpc_url(self) -> str:
        """获取当前 RPC URL。"""
        return self._pool.get_rpc_url()

    def mark_current_failed(self) -> None:
        """标记当前 Key 不可用。"""
        self._pool.mark_current_failed()

    @property
    def size(self) -> int:
        return self._pool.size

    def _validate_rpc_url(self, url: str) -> bool:
        """校验 RPC URL 有效，避免 unknown url type 等错误。"""
        if not url or not isinstance(url, str):
            return False
        u = url.strip()
        return u.startswith("http://") or u.startswith("https://")

    async def rpc_post(
        self,
        method: str,
        params: list,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Any:
        """
        执行 JSON-RPC 调用，429 时切换 Key 重试。

        :param method: RPC 方法名
        :param params: 参数列表
        :param http_client: 可选，复用外部 httpx 客户端
        :param timeout: 超时秒数
        :return: result 字段，失败返回 None
        """
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        own_client = None
        client = http_client
        if client is None:
            own_client = httpx.AsyncClient()
            client = own_client

        try:
            for attempt in range(self.MAX_RETRIES):
                url = self.get_rpc_url()
                if not self._validate_rpc_url(url):
                    logger.error(
                        "❌ Helius RPC URL 无效（空或缺少协议）: %r，请检查 HELIUS_API_KEY 配置",
                        url[:50] if url else "(空)",
                    )
                    return None
                try:
                    resp = await client.post(url, json=payload, timeout=timeout)
                    if resp.status_code == 200:
                        data = resp.json()
                        if "result" in data:
                            return data["result"]
                        if "error" in data:
                            err_msg = data.get("error", {}).get("message", "")
                            if "Rate limit" in err_msg or "429" in str(resp.status_code):
                                logger.warning(
                                    "⚠️ RPC 限流 (尝试 %s/%s)，切换 Key: %s",
                                    attempt + 1, self.MAX_RETRIES, err_msg,
                                )
                                self.mark_current_failed()
                            else:
                                return None
                    elif resp.status_code == 429:
                        logger.warning(
                            "⚠️ RPC HTTP 429 限流 (尝试 %s/%s)，切换 Key",
                            attempt + 1, self.MAX_RETRIES,
                        )
                        self.mark_current_failed()
                    else:
                        logger.warning("RPC 请求失败: HTTP %s", resp.status_code)

                except (httpx.TimeoutException, httpx.NetworkError) as e:
                    logger.warning("⚠️ RPC 网络波动 (尝试 %s/%s): %s", attempt + 1, self.MAX_RETRIES, e)
                except Exception:
                    logger.exception("❌ RPC 未知错误")
                    return None

                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.BASE_DELAY * (2 ** attempt))

            logger.error("❌ RPC %s 最终失败，已重试 %s 次", method, self.MAX_RETRIES)
            return None
        finally:
            if own_client is not None:
                await own_client.aclose()

    async def get_signatures_for_address(
        self,
        address: str,
        limit: int = 100,
        before: Optional[str] = None,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> Optional[List[Dict]]:
        """
        获取地址签名列表。

        :param address: 钱包地址
        :param limit: 返回数量
        :param before: 分页用，上一批最后一个 signature
        """
        params = [address, {"limit": limit}]
        if before:
            params[1]["before"] = before
        return await self.rpc_post("getSignaturesForAddress", params, http_client=http_client, timeout=timeout)

    async def get_transaction(
        self,
        signature: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        """
        获取交易详情（RPC 格式，含 transaction/meta）。

        :param signature: 交易签名
        """
        params = [
            signature,
            {"maxSupportedTransactionVersion": 0, "encoding": "jsonParsed"},
        ]
        return await self.rpc_post("getTransaction", params, http_client=http_client, timeout=timeout)

    async def get_token_accounts_by_owner(
        self,
        owner: str,
        mint: str,
        *,
        http_client: Optional[httpx.AsyncClient] = None,
        timeout: float = 10.0,
    ) -> Optional[Dict]:
        """
        获取地址在某代币上的账户信息。

        :param owner: 钱包地址
        :param mint: 代币 mint 地址
        :return: RPC result，含 value 数组
        """
        params = [
            owner,
            {"mint": mint},
            {"encoding": "jsonParsed"},
        ]
        return await self.rpc_post(
            "getTokenAccountsByOwner", params, http_client=http_client, timeout=timeout
        )
