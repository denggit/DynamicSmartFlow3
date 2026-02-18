#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 风控模块 - 早期跟单场景下主要避免：貔貅/蜜罐、不能卖、一买入就高税导致大额亏损。
              流动性不做严格限制（早期跟投可接受较低流动性）。
"""
from typing import Tuple

import httpx
from utils.logger import get_logger

logger = get_logger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"

# 买入税超过此比例则拒绝（避免一买就亏一大块）
MAX_ACCEPTABLE_BUY_TAX_PCT = 25.0
# RugCheck 风险分超过此值拒绝
MAX_SAFE_SCORE = 2000


async def check_is_safe_token(token_mint: str) -> bool:
    """
    检测代币是否可安全交易：非貔貅/蜜罐、可卖、买入税不过高。
    使用 RugCheck API；WSOL 直接放行。
    """
    if token_mint == WSOL_MINT:
        return True

    url = f"https://api.rugcheck.xyz/v1/tokens/{token_mint}/report"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                # 未收录的新币：策略性放行（早期跟单常遇新币）
                logger.warning("RugCheck 未收录该代币，策略放行: %s", token_mint[:16] + "..")
                return True

            data = resp.json()

            # 1. 风险分
            score = data.get("score", 0)
            if score > MAX_SAFE_SCORE:
                logger.warning("⚠️ 风险分过高 (Score: %s): %s", score, token_mint[:16] + "..")
                return False

            # 2. 致命风险（含蜜罐/不能卖等）
            risks = data.get("risks", [])
            if isinstance(risks, list):
                for r in risks:
                    level = (r or {}).get("level") or ""
                    name = (r or {}).get("name") or ""
                    if level == "danger":
                        logger.warning("☠️ 发现致命风险: %s", name)
                        return False
                    # 名称中含 honeypot / cannot sell / 卖 等也拦截
                    lower = name.lower()
                    if "honeypot" in lower or "cannot sell" in lower or "unable to sell" in lower:
                        logger.warning("☠️ 疑似不可卖/蜜罐: %s", name)
                        return False

            # 3. 买入/卖出税（若 API 有返回）
            token_meta = data.get("tokenMeta") or data.get("meta") or {}
            buy_tax = _parse_tax(token_meta.get("buyTax") or token_meta.get("buy_tax"))
            if buy_tax is not None and buy_tax > MAX_ACCEPTABLE_BUY_TAX_PCT:
                logger.warning("⚠️ 买入税过高 (%.1f%%): %s", buy_tax, token_mint[:16] + "..")
                return False

            # 从 risks 里再扫一遍税相关
            for r in risks or []:
                name = (r or {}).get("name") or ""
                if "buy" in name.lower() and "tax" in name.lower():
                    # 若有具体数值可解析则再判断，这里仅做名称提示
                    pass
            logger.info("✅ 风控通过 (Score: %s): %s", score, token_mint)
            return True

    except Exception as e:
        logger.exception("风控检测异常: %s", e)
        # 网络异常时保守策略：不放行
        return False


def _parse_tax(v):
    """从 API 返回的 tax 字段解析出百分比数字，无法解析返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.replace("%", "").strip())
        except ValueError:
            return None
    return None


async def check_token_liquidity(token_mint: str) -> Tuple[bool, float, float]:
    """
    查询流动性（早期跟单不强制高流动性，仅作参考）。
    返回: (是否有池子, liquidity_usd, fdv_usd)。
    """
    if token_mint == WSOL_MINT:
        return True, 999999999.0, 999999999.0

    url = f"https://api.dexscreener.com/latest/dex/tokens/{token_mint}"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                return False, 0.0, 0.0
            data = resp.json()
            pairs = data.get("pairs", [])
            if not pairs:
                return False, 0.0, 0.0
            solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
            if not solana_pairs:
                return False, 0.0, 0.0
            best = max(solana_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd") or 0))
            liq = float(best.get("liquidity", {}).get("usd") or 0)
            fdv = float(best.get("fdv", 0) or 0)
            return True, liq, fdv
    except Exception as e:
        logger.warning("流动性查询异常: %s", e)
        return False, 0.0, 0.0
