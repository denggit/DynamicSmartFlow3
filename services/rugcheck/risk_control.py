#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 风控模块 - 早期跟单场景下主要避免：貔貅/蜜罐、不能卖、一买入就高税导致大额亏损、
              铸币权/冻结权未放弃（Mint Authority、Freeze Authority 必须为 Renounced/None）、
              老鼠仓（Top2-10 控盘过高）、撤池风险（池子 < $5k）、FDV 过高追高、流动性/市值比过低（虚胖控盘）、
              LP 未充分锁仓（< 70%）、三无盘（无官网/Twitter/Telegram 等社交绑定）。
"""
import asyncio
from typing import Optional, Tuple

import httpx

from config.settings import (
    MAX_ACCEPTABLE_BUY_TAX_PCT,
    MAX_SAFE_SCORE,
    MIN_LIQUIDITY_USD,
    MIN_LP_LOCKED_PCT,
    MAX_TOP2_10_COMBINED_PCT,
    MAX_SINGLE_HOLDER_PCT,
    MAX_ENTRY_FDV_USD,
    MIN_LIQUIDITY_TO_FDV_RATIO,
)
from utils.logger import get_logger

logger = get_logger(__name__)

WSOL_MINT = "So11111111111111111111111111111111111111112"

# HTTP 请求超时配置（秒）
RUGCHECK_TIMEOUT = 20.0
DEXSCREENER_TIMEOUT = 15.0
HTTP_MAX_RETRIES = 3
HTTP_RETRY_DELAY = 1.5


async def _fetch_with_retry(
    url: str,
    timeout: float = 15.0,
    max_retries: int = HTTP_MAX_RETRIES,
    retry_delay: float = HTTP_RETRY_DELAY,
) -> Optional[httpx.Response]:
    """
    带重试的 HTTP GET 请求，用于应对 ReadTimeout、ConnectTimeout 等瞬态网络异常。

    Args:
        url: 请求 URL
        timeout: 单次请求超时（秒）
        max_retries: 最大重试次数
        retry_delay: 重试间隔（秒）

    Returns:
        成功时返回 Response，失败时返回 None
    """
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=10.0)
            ) as client:
                return await client.get(url)
        except (httpx.ReadTimeout, httpx.ConnectTimeout) as e:
            if attempt < max_retries - 1:
                logger.warning(
                    "HTTP 请求超时 (尝试 %d/%d)，%s 秒后重试: %s",
                    attempt + 1, max_retries, retry_delay, url[:60] + ".." if len(url) > 60 else url
                )
                await asyncio.sleep(retry_delay)
            else:
                logger.warning("HTTP 请求超时，已达最大重试次数: %s", type(e).__name__)
        except Exception as e:
            logger.warning("HTTP 请求异常: %s", e)
            return None
    return None


async def check_is_safe_token(token_mint: str) -> bool:
    """
    检测代币是否可安全交易：非貔貅/蜜罐、可卖、买入税不过高、
    铸币权与冻结权已放弃（Mint/Freeze Authority 必须为 None）。
    使用 RugCheck API；WSOL 直接放行。
    """
    if token_mint == WSOL_MINT:
        return True

    url = f"https://api.rugcheck.xyz/v1/tokens/{token_mint}/report"
    resp = await _fetch_with_retry(url, timeout=RUGCHECK_TIMEOUT)
    if resp is None:
        logger.warning("RugCheck API 请求失败（超时或网络异常），保守拒绝: %s", token_mint[:16] + "..")
        return False

    try:
        if resp.status_code != 200:
            logger.warning("RugCheck 未收录该代币: %s", token_mint[:16] + "..")
            has_pool, liq_usd, fdv_usd = await check_token_liquidity(token_mint)
            if not has_pool or liq_usd < MIN_LIQUIDITY_USD:
                logger.warning("⚠️ 未收录且池子过小 ($%.0f)，拒绝: %s", liq_usd, token_mint[:16] + "..")
                return False
            if fdv_usd > MAX_ENTRY_FDV_USD:
                logger.warning("⚠️ 未收录且 FDV 过高 ($%.0f)，拒绝: %s", fdv_usd, token_mint[:16] + "..")
                return False
            if fdv_usd > 0 and liq_usd / fdv_usd < MIN_LIQUIDITY_TO_FDV_RATIO:
                logger.warning("⚠️ 未收录且流动性/市值比过低，拒绝: %s", token_mint[:16] + "..")
                return False
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

        # 3. 铸币权/冻结权：安全 Meme 币必须两者皆为 Renounced (null)
        mint_authority = data.get("mintAuthority")
        freeze_authority = data.get("freezeAuthority")
        if mint_authority not in (None, ""):
            logger.warning("⚠️ 铸币权未放弃 (Mint Authority 未 Renounced): %s", token_mint[:16] + "..")
            return False
        if freeze_authority not in (None, ""):
            logger.warning("⚠️ 冻结权未放弃 (Freeze Authority 未 Renounced): %s", token_mint[:16] + "..")
            return False

        # 4. 买入/卖出税（若 API 有返回）
        token_meta = data.get("tokenMeta") or data.get("meta") or {}
        buy_tax = _parse_tax(token_meta.get("buyTax") or token_meta.get("buy_tax"))
        if buy_tax is not None and buy_tax > MAX_ACCEPTABLE_BUY_TAX_PCT:
            logger.warning("⚠️ 买入税过高 (%.1f%%): %s", buy_tax, token_mint[:16] + "..")
            return False

        # 5. LP 锁仓检查：流动性最大的池子至少 70% 锁仓或销毁，否则拒跟
        markets = data.get("markets") or []
        if markets:
            main_market = markets[0]
            lp_locked_pct = _parse_lp_locked_pct(main_market.get("lpLockedPct"))
            if lp_locked_pct is not None and lp_locked_pct < MIN_LP_LOCKED_PCT:
                logger.warning(
                    "⚠️ 流动性未充分锁定 (仅 %.1f%% < %.0f%%): %s",
                    lp_locked_pct, MIN_LP_LOCKED_PCT, token_mint[:16] + ".."
                )
                return False

        # 6. 三无盘检查：无 Twitter/X、无 Telegram 的土狗视为春 PVP 割草盘，拒绝
        links = token_meta.get("links") or []
        has_twitter = any(
            "twitter.com" in (str(link.get("url") or "")).lower()
            or "x.com" in (str(link.get("url") or "")).lower()
            for link in links if isinstance(link, dict)
        )
        has_tg = any(
            "t.me" in (str(link.get("url") or "")).lower()
            for link in links if isinstance(link, dict)
        )
        if not (has_twitter or has_tg):
            logger.warning("⚠️ 缺乏社交媒体绑定（三无盘），拒绝入场: %s", token_mint[:16] + "..")
            return False

        # 7. 池子大小（防撤池）+ FDV + 流动性/市值比
        has_pool, liq_usd, fdv_usd = await check_token_liquidity(token_mint)
        if not has_pool or liq_usd < MIN_LIQUIDITY_USD:
            logger.warning("⚠️ 池子过小 (Liquidity $%.0f < $%.0f): %s", liq_usd, MIN_LIQUIDITY_USD, token_mint[:16] + "..")
            return False
        if fdv_usd > MAX_ENTRY_FDV_USD:
            logger.warning("⚠️ FDV 过高 ($%.0f > $%.0f)，不追高: %s", fdv_usd, MAX_ENTRY_FDV_USD, token_mint[:16] + "..")
            return False
        if fdv_usd > 0 and liq_usd / fdv_usd < MIN_LIQUIDITY_TO_FDV_RATIO:
            logger.warning(
                "⚠️ 流动性/市值比过低 (%.2f%% < %.0f%%)，虚胖控盘: %s",
                liq_usd / fdv_usd * 100, MIN_LIQUIDITY_TO_FDV_RATIO * 100, token_mint[:16] + ".."
            )
            return False

        # 9. Top 10 持仓（防老鼠仓）：排除 LP 后，第 2~10 名不得控盘过高
        if not _check_top_holders_safe(data, token_mint):
            return False

        logger.info("✅ 风控通过 (Score: %s): %s", score, token_mint)
        return True

    except Exception as e:
        logger.warning("风控检测异常（解析或逻辑错误）: %s", e)
        return False


def _check_top_holders_safe(data: dict, token_mint: str) -> bool:
    """
    防老鼠仓：排除 LP（第一大持仓）后，
    - 第 2~10 名合计 > 剩余供应的 40% → 拒绝
    - 单一地址 > 剩余供应的 10% → 拒绝
    """
    holders = data.get("topHolders") or []
    if len(holders) < 2:
        return True

    lp_addrs = set()
    for m in data.get("markets") or []:
        pubkey = (m or {}).get("pubkey")
        if pubkey:
            lp_addrs.add(str(pubkey))
    for k in (data.get("lockers") or {}).keys():
        if k:
            lp_addrs.add(str(k))

    pct_1 = float((holders[0] or {}).get("pct", 0))
    remaining_pct = 100.0 - pct_1
    if remaining_pct <= 0:
        return True

    candidates = []
    for h in holders[1:10]:
        addr = str((h or {}).get("address", ""))
        owner = str((h or {}).get("owner", ""))
        pct = float((h or {}).get("pct", 0))
        if addr in lp_addrs or owner in lp_addrs:
            continue
        candidates.append(pct)

    combined = sum(candidates)
    if combined > MAX_TOP2_10_COMBINED_PCT * remaining_pct:
        logger.warning(
            "⚠️ 老鼠仓：第2~10名合计 %.1f%% > 剩余 %.1f%% 的 %.0f%%: %s",
            combined, remaining_pct, MAX_TOP2_10_COMBINED_PCT * 100, token_mint[:16] + ".."
        )
        return False
    for p in candidates:
        if p > MAX_SINGLE_HOLDER_PCT * remaining_pct:
            logger.warning(
                "⚠️ 老鼠仓：单一地址 %.1f%% > 剩余 %.1f%% 的 %.0f%%: %s",
                p, remaining_pct, MAX_SINGLE_HOLDER_PCT * 100, token_mint[:16] + ".."
            )
            return False
    return True


def _parse_lp_locked_pct(v) -> Optional[float]:
    """
    从 API 返回的 lpLockedPct 字段解析出百分比数字。
    支持 int、float、字符串（如 "85.5"、"85.5%"）。
    无法解析返回 None（调用方将跳过 LP 锁仓检查）。
    """
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(str(v).replace("%", "").strip())
        except ValueError:
            return None
    return None


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
        resp = await _fetch_with_retry(url, timeout=DEXSCREENER_TIMEOUT)
        if resp is None or resp.status_code != 200:
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
