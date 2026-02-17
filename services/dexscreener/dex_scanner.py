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

    async def get_token_price(self, token_address: str) -> float:
        """
        获取代币当前价格 (以 SOL 为单位, priceNative)
        """
        url = f"{self.base_url}/latest/dex/tokens/{token_address}"
        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(url, timeout=5.0)
                if response.status_code == 200:
                    data = response.json()
                    pairs = data.get('pairs', [])
                    if pairs:
                        # 找流动性最好的池子
                        best_pair = max(pairs, key=lambda x: float(x.get('liquidity', {}).get('usd', 0) or 0))
                        # priceNative: 1 Token = ? SOL
                        return float(best_pair.get('priceNative', 0))
            except Exception:
                logger.exception("Error fetching price for %s", token_address)
        return 0.0

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
