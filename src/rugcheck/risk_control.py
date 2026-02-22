#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 风控模块 - 早期跟单场景下主要避免：貔貅/蜜罐、不能卖、一买入就高税导致大额亏损、
              铸币权/冻结权未放弃、买卖税可动态修改、非标准 SPL（transfer restricted/blacklist/whitelist）、
              老鼠仓（Top2-10 控盘过高）、撤池风险（池子 < $3k）、FDV 过高追高、流动性/市值比过低（虚胖控盘）、
              LP 未充分锁仓（< 70%）、三无盘（无官网/Twitter/Telegram 等社交绑定）。
"""
import asyncio
from typing import Optional, Tuple

import httpx

from config.settings import (
    MAX_ACCEPTABLE_BUY_TAX_PCT,
    MAX_SAFE_SCORE,
    MIN_LIQUIDITY_REJECT,
    MIN_LIQUIDITY_USD,
    MIN_LP_LOCKED_PCT,
    MAX_TOP2_10_COMBINED_PCT,
    MAX_SINGLE_HOLDER_PCT,
    MAX_ENTRY_FDV_USD,
    MIN_LIQUIDITY_TO_FDV_RATIO,
    WSOL_MINT,
    RUGCHECK_TIMEOUT,
    RUGCHECK_API_BASE_URL,
    DEXSCREENER_BASE_URL,
    DEXSCREENER_TIMEOUT,
    HTTP_MAX_RETRIES,
    HTTP_RETRY_DELAY,
    BIRDEYE_MARKET_DATA_TIMEOUT,
)
from utils.logger import get_logger

logger = get_logger(__name__)


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


async def check_is_safe_token(token_mint: str) -> "Tuple[bool, bool]":
    """
    检测代币是否可安全交易。
    返回 (can_buy, halve_position_no_addon)：
    - can_buy=False: 直接拒绝
    - can_buy=True, halve_position=False: 正常开仓
    - can_buy=True, halve_position=True: 减半仓买入，且不允许加仓
    """
    if token_mint == WSOL_MINT:
        return (True, False)

    url = f"{RUGCHECK_API_BASE_URL}/{token_mint}/report"
    resp = await _fetch_with_retry(url, timeout=RUGCHECK_TIMEOUT)
    if resp is None:
        logger.warning("RugCheck API 请求失败（超时或网络异常），保守拒绝: %s", token_mint[:16] + "..")
        return (False, False)

    try:
        halve_position = False

        if resp.status_code != 200:
            logger.warning("RugCheck 未收录该代币: %s", token_mint[:16] + "..")
            has_pool, liq_usd, fdv_usd = await check_token_liquidity(token_mint)
            if not has_pool or liq_usd < MIN_LIQUIDITY_REJECT:
                logger.warning("⚠️ 未收录且池子过小 ($%.0f < $%.0f)，拒绝: %s", liq_usd, MIN_LIQUIDITY_REJECT, token_mint[:16] + "..")
                return (False, False)
            if liq_usd < MIN_LIQUIDITY_USD:
                halve_position = True
                logger.warning("⚠️ 未收录且流动性 $%.0f < $%.0f，减半仓且不可加仓: %s", liq_usd, MIN_LIQUIDITY_USD, token_mint[:16] + "..")
            if fdv_usd >= MAX_ENTRY_FDV_USD:
                halve_position = True
                logger.warning("⚠️ 未收录且 FDV 过高 ($%.0f)，减半仓且不可加仓: %s", fdv_usd, token_mint[:16] + "..")
            if fdv_usd > 0 and liq_usd / fdv_usd < MIN_LIQUIDITY_TO_FDV_RATIO:
                logger.warning("⚠️ 未收录且流动性/市值比过低，拒绝: %s", token_mint[:16] + "..")
                return (False, False)
            return (True, halve_position)

        data = resp.json()

        # 1. 风险分：> MAX_SAFE_SCORE 则减半仓，不直接拒绝
        score = data.get("score", 0)
        if score > MAX_SAFE_SCORE:
            halve_position = True
            logger.warning("⚠️ 风险分过高 (Score: %s)，减半仓且不可加仓: %s", score, token_mint[:16] + "..")

        # 2. 致命风险（含蜜罐/不能卖等）+ 买卖税可动态修改
        risks = data.get("risks", [])
        if isinstance(risks, list):
            for r in risks:
                level = (r or {}).get("level") or ""
                name = (r or {}).get("name") or ""
                desc = (r or {}).get("description") or ""
                if level == "danger":
                    logger.warning("☠️ 发现致命风险: %s", name)
                    return (False, False)
                # 名称中含 honeypot / cannot sell / 卖 等也拦截
                lower = name.lower()
                if "honeypot" in lower or "cannot sell" in lower or "unable to sell" in lower:
                    logger.warning("☠️ 疑似不可卖/蜜罐: %s", name)
                    return (False, False)
                # 买卖税可动态修改：拒绝（项目方可随时调高税率割韭菜）
                lower_desc = desc.lower()
                combined = f"{lower} {lower_desc}"
                if ("transfer fee" in combined or "transfer fee authority" in combined) and (
                    "update" in combined or "mutable" in combined or "change" in combined
                    or "not revoke" in combined or "not renounce" in combined
                ):
                    logger.warning("⚠️ 买卖税可动态修改 (风险: %s)，拒绝: %s", name or desc[:50], token_mint[:16] + "..")
                    return (False, False)
                # 非标准 SPL：transfer restricted / blacklist / whitelist → 拒绝（项目方可随时拉黑/限制转出）
                if any(
                    k in combined for k in (
                        "transfer restricted", "blacklist", "whitelist",
                        "transfer restriction", "transfer hook",
                    )
                ):
                    logger.warning("⚠️ 非标准 SPL (转移限制/黑白名单): %s，拒绝: %s", name or desc[:50], token_mint[:16] + "..")
                    return (False, False)

        # # 3. 铸币权/冻结权：安全 Meme 币必须两者皆为 Renounced (null) — 暂时注释
        # mint_authority = data.get("mintAuthority")
        # freeze_authority = data.get("freezeAuthority")
        # if mint_authority not in (None, ""):
        #     logger.warning("⚠️ 铸币权未放弃 (Mint Authority 未 Renounced): %s", token_mint[:16] + "..")
        #     return False
        # if freeze_authority not in (None, ""):
        #     logger.warning("⚠️ 冻结权未放弃 (Freeze Authority 未 Renounced): %s", token_mint[:16] + "..")
        #     return False

        # 4. 买入/卖出税（若 API 有返回）
        token_meta = data.get("tokenMeta") or data.get("meta") or {}
        buy_tax = _parse_tax(token_meta.get("buyTax") or token_meta.get("buy_tax"))
        if buy_tax is not None and buy_tax > MAX_ACCEPTABLE_BUY_TAX_PCT:
            logger.warning("⚠️ 买入税过高 (%.1f%%): %s", buy_tax, token_mint[:16] + "..")
            return (False, False)

        # 4b. Token2022 转账费：若 transferFee.authority 非空且非系统程序，税率可被动态修改 → 拒绝
        transfer_fee = data.get("transferFee") or {}
        if isinstance(transfer_fee, dict):
            authority = transfer_fee.get("authority") or transfer_fee.get("transferFeeConfigAuthority")
            # 系统程序 11111111111111111111111111111111 表示无权限/不可修改
            if authority not in (None, "") and str(authority).strip().lower() not in (
                "11111111111111111111111111111111", "null"
            ):
                logger.warning(
                    "⚠️ 买卖税可动态修改 (TransferFee authority 未放弃): %s",
                    token_mint[:16] + "..",
                )
                return (False, False)

        # # 5. LP 锁仓检查：流动性最大的池子至少 70% 锁仓或销毁，否则拒跟 — 暂时注释
        # markets = data.get("markets") or []
        # if markets:
        #     main_market = markets[0]
        #     lp_locked_pct = _parse_lp_locked_pct(main_market.get("lpLockedPct"))
        #     if lp_locked_pct is not None and lp_locked_pct < MIN_LP_LOCKED_PCT:
        #         logger.warning(
        #             "⚠️ 流动性未充分锁定 (仅 %.1f%% < %.0f%%): %s",
        #             lp_locked_pct, MIN_LP_LOCKED_PCT, token_mint[:16] + ".."
        #         )
        #         return False

        # # 6. 三无盘检查：无 Twitter/X、无 Telegram 的土狗视为春 PVP 割草盘，拒绝
        # links = token_meta.get("links") or []
        # has_twitter = any(
        #     "twitter.com" in (str(link.get("url") or "")).lower()
        #     or "x.com" in (str(link.get("url") or "")).lower()
        #     for link in links if isinstance(link, dict)
        # )
        # has_tg = any(
        #     "t.me" in (str(link.get("url") or "")).lower()
        #     for link in links if isinstance(link, dict)
        # )
        # if not (has_twitter or has_tg):
        #     logger.warning("⚠️ 缺乏社交媒体绑定（三无盘），拒绝入场: %s", token_mint[:16] + "..")
        #     return False

        # 7. 池子大小（防撤池）+ FDV + 流动性/市值比
        has_pool, liq_usd, fdv_usd = await check_token_liquidity(token_mint)
        if not has_pool or liq_usd < MIN_LIQUIDITY_REJECT:
            logger.warning("⚠️ 池子过小 ($%.0f < $%.0f)，拒绝: %s", liq_usd, MIN_LIQUIDITY_REJECT, token_mint[:16] + "..")
            return (False, False)
        if liq_usd < MIN_LIQUIDITY_USD:
            halve_position = True
            logger.warning("⚠️ 流动性 $%.0f < $%.0f，减半仓且不可加仓: %s", liq_usd, MIN_LIQUIDITY_USD, token_mint[:16] + "..")
        if fdv_usd >= MAX_ENTRY_FDV_USD:
            halve_position = True
            logger.warning("⚠️ FDV 过高 ($%.0f >= $%.0f)，减半仓且不可加仓: %s", fdv_usd, MAX_ENTRY_FDV_USD, token_mint[:16] + "..")
        # # 流动性/市值比 >= 3% — 暂时注释
        # if fdv_usd > 0 and liq_usd / fdv_usd < MIN_LIQUIDITY_TO_FDV_RATIO:
        #     logger.warning(
        #         "⚠️ 流动性/市值比过低 (%.2f%% < %.0f%%)，虚胖控盘: %s",
        #         liq_usd / fdv_usd * 100, MIN_LIQUIDITY_TO_FDV_RATIO * 100, token_mint[:16] + ".."
        #     )
        #     return False

        # # 9. Top 10 持仓（防老鼠仓）：排除 LP 后，第 2~10 名不得控盘过高 — 暂时注释
        # if not _check_top_holders_safe(data, token_mint):
        #     return False

        logger.info("✅ 风控通过 (Score: %s)%s: %s", score, " [减半仓]" if halve_position else "", token_mint)
        return (True, halve_position)

    except Exception as e:
        logger.warning("风控检测异常（解析或逻辑错误）: %s", e)
        return (False, False)


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
    threshold = MAX_TOP2_10_COMBINED_PCT * remaining_pct  # 剩余供应的 30%，如 83.3%×0.3=24.99%
    if combined > threshold:
        logger.warning(
            "⚠️ 老鼠仓：第2~10名合计 %.1f%% > 阈值 %.1f%%（剩余%.1f%%的30%%）: %s",
            combined, threshold, remaining_pct, token_mint[:16] + ".."
        )
        return False
    single_threshold = MAX_SINGLE_HOLDER_PCT * remaining_pct  # 剩余供应的 10%
    for p in candidates:
        if p > single_threshold:
            logger.warning(
                "⚠️ 老鼠仓：单一地址 %.1f%% > 阈值 %.1f%%（剩余%.1f%%的10%%）: %s",
                p, single_threshold, remaining_pct, token_mint[:16] + ".."
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
    先用 DexScreener；若查不到或流动性为 0，用 Birdeye 兜底（新币收录更快）。
    """
    if token_mint == WSOL_MINT:
        return True, 999999999.0, 999999999.0

    has_pool = False
    liq_usd = 0.0
    fdv_usd = 0.0

    url = f"{DEXSCREENER_BASE_URL}/latest/dex/tokens/{token_mint}"
    try:
        resp = await _fetch_with_retry(url, timeout=DEXSCREENER_TIMEOUT)
        if resp is not None and resp.status_code == 200:
            data = resp.json()
            pairs = data.get("pairs", [])
            if pairs:
                solana_pairs = [p for p in pairs if p.get("chainId") == "solana"]
                if solana_pairs:
                    best = max(solana_pairs, key=lambda x: float(x.get("liquidity", {}).get("usd") or 0))
                    liq_usd = float(best.get("liquidity", {}).get("usd") or 0)
                    fdv_usd = float(best.get("fdv", 0) or 0)
                    if liq_usd > 0:
                        has_pool = True
    except Exception as e:
        logger.warning("DexScreener 流动性查询异常: %s", e)

    # DexScreener 未查到或流动性为 0 时，用 Birdeye 兜底（Pump.fun 新币等收录更快）
    if not has_pool or liq_usd == 0:
        try:
            from config.settings import birdeye_key_pool
            if birdeye_key_pool.size > 0:
                from src.birdeye import birdeye_client
                logger.debug("DexScreener 未查到流动性，使用 Birdeye 兜底查询...")
                market_data = await birdeye_client.get_token_market_data(token_mint, timeout=BIRDEYE_MARKET_DATA_TIMEOUT)
                if market_data:
                    liq = float(market_data.get("liquidity", 0) or 0)
                    fdv = float(market_data.get("fdv", 0) or 0)
                    if liq > 0:
                        logger.debug("Birdeye 兜底成功: liq=$%.0f fdv=$%.0f", liq, fdv)
                        return True, liq, fdv
        except Exception as e:
            logger.debug("Birdeye 兜底查询异常: %s", e)

    return has_pool, liq_usd, fdv_usd
