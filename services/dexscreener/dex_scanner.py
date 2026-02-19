#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/16/2026 10:06 PM
@File       : dex_scanner.py
@Description: DexScreener 代币扫描与价格查询。
"""
import asyncio

import httpx

from utils.logger import get_logger

logger = get_logger(__name__)


class DexScanner:
    def __init__(self):
        self.base_url = "https://api.dexscreener.com"
        self.target_chain = "solana"
        self.min_liquidity = 10000  # 最低流动性 10,000 USD
        self.min_vol_1h = 50000  # 1小时最低成交额 50,000 USD

    async def fetch_latest_tokens(self):
        """获取最近有社交信息更新的代币"""
        url = f"{self.base_url}/token-profiles/latest/v1"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=10.0)
                if response.status_code == 200:
                    return response.json()
            except Exception:
                logger.exception("Error fetching profiles")
        return []

    async def get_token_pairs(self, token_address):
        """获取代币的详细交易对信息，用于过滤指标"""
        url = f"{self.base_url}/latest/dex/tokens/{token_address}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=5.0)
                if response.status_code == 200:
                    return response.json().get('pairs', [])
            except Exception:
                logger.exception("get_token_pairs 请求异常")
        return []

    # Solana 上 WSOL 地址，用于校验 pair 是否为 token/SOL
    _WSOL_ADDRESS = "So11111111111111111111111111111111111111112"

    def _parse_sol_per_token(self, pair: dict, token_address: str):
        """
        从 pair 解析「1 token = ? SOL」。
        DexScreener: priceNative = quote 币 per 1 base 币。
        若 token 为 base、quote 为 SOL → 直接用 priceNative；
        若 token 为 quote、base 为 SOL → 需用 1/priceNative。
        """
        base = pair.get("baseToken") or {}
        quote = pair.get("quoteToken") or {}
        base_addr = (base.get("address") or "").strip()
        quote_addr = (quote.get("address") or "").strip()
        token_address = (token_address or "").strip()
        price_native = pair.get("priceNative")
        if price_native is None:
            return None
        try:
            p = float(price_native)
        except (TypeError, ValueError):
            return None
        if p <= 0:
            return None
        # 仅处理 token/SOL 或 SOL/token 的 pair
        is_sol = lambda a: a == self._WSOL_ADDRESS or "11111111111111111111111111111111" in (a or "")
        if base_addr == token_address and is_sol(quote_addr):
            return p  # 1 token = p SOL
        if quote_addr == token_address and is_sol(base_addr):
            return 1.0 / p if p > 0 else None  # 1 SOL = p token → 1 token = 1/p SOL
        return None

    async def get_token_price(self, token_address: str):
        """
        获取代币当前价格 (1 token = ? SOL)。
        优先选 Solana 上 token/SOL pair，正确解析 base/quote，避免 8317% 等错误。
        成功返回 float；失败返回 None。
        """
        url = f"{self.base_url}/latest/dex/tokens/{token_address}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=5.0)
                if response.status_code == 200:
                    data = response.json()
                    pairs = data.get('pairs', []) or []
                    # 只保留包含本 token 且为 token/SOL 的 pair
                    sol_pairs = []
                    for p in pairs:
                        if p.get("chainId") != "solana":
                            continue
                        price = self._parse_sol_per_token(p, token_address)
                        if price is not None:
                            sol_pairs.append((p, price))
                    if sol_pairs:
                        best = max(sol_pairs, key=lambda x: float(x[0].get('liquidity', {}).get('usd', 0) or 0))
                        return best[1]
            except Exception:
                logger.exception("Error fetching price for %s", token_address)
        return None

    async def scan(self):
        logger.info("正在扫描 Solana 潜力币...")
        raw_tokens = await self.fetch_latest_tokens()

        qualified_tokens = []

        # 只需要处理 Solana 的币
        sol_tokens = [t for t in raw_tokens if t.get('chainId') == self.target_chain]

        for item in sol_tokens:
            addr = item.get('tokenAddress')
            # 获取该币的详细池子数据进行二次过滤
            pairs = await self.get_token_pairs(addr)

            if not pairs: continue

            # 取流动性最大的池子
            main_pair = max(pairs, key=lambda x: x.get('liquidity', {}).get('usd', 0))

            liq = main_pair.get('liquidity', {}).get('usd', 0)
            vol_1h = main_pair.get('volume', {}).get('h1', 0)

            # 过滤逻辑
            if liq >= self.min_liquidity and vol_1h >= self.min_vol_1h:
                logger.info(
                    "找到符合标准代币: %s | 地址: %s",
                    main_pair.get('baseToken', {}).get('symbol'),
                    addr,
                )
                qualified_tokens.append({
                    "address": addr,
                    "symbol": main_pair.get('baseToken', {}).get('symbol'),
                    "liquidity": liq,
                    "vol_1h": vol_1h
                })

        return qualified_tokens


async def main():
    scanner = DexScanner()
    while True:
        results = await scanner.scan()
        if results:
            logger.info("本轮结果 (%s 个)", len(results))
            for r in results:
                logger.info("代币地址: %s", r['address'])

        logger.info("等待 5 分钟后进行下一轮扫描...")
        await asyncio.sleep(300)  # 5分钟轮询一次，完全不会触发限流


if __name__ == "__main__":
    asyncio.run(main())
