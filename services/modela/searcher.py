#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: MODELA 猎手挖掘器 - 热门币回溯早期买家
              DexScreener 涨幅达标代币 → 回溯开盘区间 → 初筛买家 → ROI+评分入库
"""

import asyncio
import json
import logging
import os
import threading
import time
from collections import defaultdict
from typing import Dict, List, Tuple, Set

import httpx

from config.settings import (
    BASE_DIR,
    USDC_MINT,
    WSOL_MINT,
    MIN_TOKEN_AGE_SEC,
    MAX_TOKEN_AGE_SEC,
    MAX_BACKTRACK_PAGES,
    SM_EARLY_TX_PARSE_LIMIT,
    SM_USE_ATA_FIRST,
    SM_ATA_SIG_LIMIT,
    SM_LP_CHECK_TX_LIMIT,
    SCANNED_HISTORY_FILE,
    SM_MIN_DELAY_SEC,
    SM_MAX_DELAY_SEC,
    SM_AUDIT_TX_LIMIT,
    SM_MIN_BUY_SOL,
    SM_MAX_BUY_SOL,
    SM_MIN_TOKEN_PROFIT_PCT,
    SM_ENTRY_MIN_PNL_RATIO,
    SM_ENTRY_MIN_WIN_RATE,
    SM_ENTRY_MIN_TRADE_COUNT,
    SM_ROI_MULT_ONE,
    SM_ROI_MULT_TWO,
    SM_ROI_MULT_THREE,
    DEX_MIN_24H_GAIN_PCT,
    WALLET_BLACKLIST_FILE,
    TIER_ONE_ROI,
    TIER_TWO_ROI,
    TIER_THREE_ROI,
    DEXSCREENER_TOKEN_TIMEOUT,
    SM_BACKTRACK_SIGS_PER_PAGE,
    SM_NEAR_ENTRY_THRESHOLD,
    SM_SEARCHER_WALLET_SLEEP_SEC,
    SM_SEARCHER_CANDIDATE_SKIP_SLEEP_SEC,
    SM_SEARCHER_TOKEN_SLEEP_SEC,
)
from src.alchemy import alchemy_client
from src.helius import helius_client
from utils.logger import get_logger
from utils.solana_ata import get_associated_token_address

from services.hunter_common import (
    TransactionParser,
    TokenAttributionCalculator,
    _get_tx_timestamp,
    _normalize_token_amount,
    hunter_had_any_lp_anywhere,
    hunter_had_any_lp_on_token,
    collect_lp_participants_from_txs,
    _is_frequent_trader_by_blocktimes,
)
from services.modela.scoring import compute_hunter_score

logger = get_logger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class SmartMoneySearcher:
    """
    MODELA 猎手挖掘器：热门币 → 回溯早期买家 → ROI+体检评分 → 入库。
    """

    def __init__(self):
        self.min_delay_sec = SM_MIN_DELAY_SEC
        self.max_delay_sec = SM_MAX_DELAY_SEC
        self.audit_tx_limit = SM_AUDIT_TX_LIMIT
        self.scanned_tokens: Set[str] = set()
        self.wallet_blacklist: Set[str] = set()
        self._load_scanned_history()
        self._load_wallet_blacklist()

    def _ensure_data_dir(self):
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

    def _load_scanned_history(self):
        self._ensure_data_dir()
        if os.path.exists(SCANNED_HISTORY_FILE):
            try:
                with open(SCANNED_HISTORY_FILE, 'r') as f:
                    self.scanned_tokens = set(json.load(f))
                logger.info("📂 已加载 %d 个历史扫描代币记录", len(self.scanned_tokens))
            except Exception:
                logger.exception("⚠️ 加载扫描历史失败")

    def _save_scanned_token(self, token_address: str):
        if token_address in self.scanned_tokens:
            return
        self.scanned_tokens.add(token_address)
        snapshot = list(self.scanned_tokens)

        def _write():
            try:
                with open(SCANNED_HISTORY_FILE, 'w') as f:
                    json.dump(snapshot, f)
            except Exception:
                logger.exception("保存扫描历史失败")

        threading.Thread(target=_write, daemon=True).start()

    def _load_wallet_blacklist(self):
        self._ensure_data_dir()
        if os.path.exists(WALLET_BLACKLIST_FILE):
            try:
                with open(WALLET_BLACKLIST_FILE, 'r') as f:
                    self.wallet_blacklist = set(json.load(f))
                if self.wallet_blacklist:
                    logger.info("📂 已加载 %d 个钱包黑名单", len(self.wallet_blacklist))
            except Exception:
                logger.exception("⚠️ 加载钱包黑名单失败")

    def is_blacklisted(self, address: str) -> bool:
        return address in self.wallet_blacklist

    def _add_to_wallet_blacklist(self, address: str):
        if address in self.wallet_blacklist:
            return
        self.wallet_blacklist.add(address)
        snapshot = list(self.wallet_blacklist)

        def _write():
            try:
                with open(WALLET_BLACKLIST_FILE, 'w') as f:
                    json.dump(snapshot, f)
            except Exception:
                logger.exception("保存钱包黑名单失败")

        threading.Thread(target=_write, daemon=True).start()

    async def get_signatures(self, client, address, limit=100, before=None):
        return await alchemy_client.get_signatures_for_address(
            address, limit=limit, before=before, http_client=client
        )

    async def is_frequent_trader(self, client, address: str) -> bool:
        """判断是否为高频交易（供 MODELB 等复用）。"""
        sigs = await self.get_signatures(client, address, limit=100)
        if not sigs:
            return False
        return _is_frequent_trader_by_blocktimes(sigs)

    async def fetch_parsed_transactions(self, client, signatures):
        if not signatures:
            return []
        return await helius_client.fetch_parsed_transactions(signatures, http_client=client)

    def _build_projects_from_txs(
        self, txs: List[dict], exclude_token: str, usdc_price: float, hunter_address: str
    ) -> dict:
        parser = TransactionParser(hunter_address)
        calc = TokenAttributionCalculator()
        projects = defaultdict(lambda: {"buy_sol": 0.0, "sell_sol": 0.0, "tokens": 0.0})
        txs = sorted(txs, key=lambda x: _get_tx_timestamp(x))
        for tx in txs:
            try:
                sol_change, token_changes, _ = parser.parse_transaction(tx, usdc_price_sol=usdc_price)
                if not token_changes:
                    continue
                buy_attrs, sell_attrs = calc.calculate_attribution(sol_change, token_changes)
                for mint, delta in token_changes.items():
                    if exclude_token and mint == exclude_token:
                        continue
                    if abs(delta) < 1e-9:
                        continue
                    projects[mint]["tokens"] += delta
                    if mint in buy_attrs:
                        projects[mint]["buy_sol"] += buy_attrs[mint]
                    if mint in sell_attrs:
                        projects[mint]["sell_sol"] += sell_attrs[mint]
            except Exception:
                logger.debug("解析单笔交易跳过", exc_info=True)
        return projects

    async def check_hunter_has_lp_and_blacklist(
        self, client, hunter_address: str, txs: List[dict] | None = None
    ) -> bool:
        if txs is None:
            sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
            if not sigs:
                return False
            txs = await self.fetch_parsed_transactions(client, sigs)
        if not txs:
            return False
        if hunter_had_any_lp_anywhere(txs):
            logger.warning("⚠️ 体检发现 LP 行为: %s.. 已加入黑名单并踢出", hunter_address[:12])
            self._add_to_wallet_blacklist(hunter_address)
            return True
        return False

    async def analyze_hunter_performance(
        self, client, hunter_address, exclude_token=None, pre_fetched_txs: List[dict] | None = None
    ):
        if pre_fetched_txs is not None:
            txs = pre_fetched_txs
        else:
            sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
            if not sigs:
                return None
            if _is_frequent_trader_by_blocktimes(sigs):
                logger.info("⏭️ 剔除频繁交易地址 %s.. (blockTime 预检密集)", hunter_address)
                return None
            txs = await self.fetch_parsed_transactions(client, sigs)
            if not txs:
                return None
        if not txs:
            return None

        if hunter_had_any_lp_anywhere(txs):
            logger.warning("⚠️ 体检发现 LP 行为: %s.. 已加入黑名单", hunter_address[:12])
            self._add_to_wallet_blacklist(hunter_address)
            return {"_lp_detected": True}

        usdc_price = await self._get_usdc_price_sol(client) if client else None
        projects = self._build_projects_from_txs(txs, exclude_token, usdc_price, hunter_address)

        valid_projects = []
        for mint, data in projects.items():
            if data["buy_sol"] > 0.05:
                net_profit = data["sell_sol"] - data["buy_sol"]
                roi = (net_profit / data["buy_sol"]) * 100
                valid_projects.append({"profit": net_profit, "roi": roi, "cost": data["buy_sol"]})

        if not valid_projects:
            return None

        total_profit = sum(p["profit"] for p in valid_projects)
        wins = [p for p in valid_projects if p["profit"] > 0]
        win_rate = len(wins) / len(valid_projects)
        avg_roi_pct = sum(p["roi"] for p in valid_projects) / len(valid_projects)
        total_wins = sum(p["profit"] for p in valid_projects if p["profit"] > 0)
        total_losses = sum(abs(p["profit"]) for p in valid_projects if p["profit"] < 0)
        pnl_ratio = total_wins / total_losses if total_losses > 0 else (float("inf") if total_wins > 0 else 0.0)

        now = time.time()
        max_roi_30d = 0.0
        max_roi_60d = 0.0
        for max_age_sec in (30 * 86400, 60 * 86400):
            txs_window = [tx for tx in txs if (now - _get_tx_timestamp(tx)) <= max_age_sec]
            if not txs_window:
                continue
            proj = self._build_projects_from_txs(txs_window, exclude_token, usdc_price, hunter_address)
            rois = []
            for _, data in proj.items():
                if data["buy_sol"] > 0.05:
                    net = data["sell_sol"] - data["buy_sol"]
                    rois.append((net / data["buy_sol"]) * 100)
            if rois:
                val = max(rois)
                if max_age_sec == 30 * 86400:
                    max_roi_30d = val
                else:
                    max_roi_60d = val

        return {
            "win_rate": win_rate,
            "total_profit": total_profit,
            "avg_roi_pct": avg_roi_pct,
            "pnl_ratio": pnl_ratio,
            "count": len(valid_projects),
            "max_roi_30d": max_roi_30d,
            "max_roi_60d": max_roi_60d,
        }

    async def _get_usdc_price_sol(self, client) -> float | None:
        return await self._get_token_price_sol(client, USDC_MINT)

    async def _get_token_price_sol(self, client, token_address: str) -> float | None:
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        try:
            resp = await client.get(url, timeout=DEXSCREENER_TOKEN_TIMEOUT)
            if resp.status_code != 200:
                return None
            data = resp.json()
            pairs = data.get("pairs", [])
            wsol = WSOL_MINT
            for p in pairs:
                if p.get("chainId") != "solana":
                    continue
                base = p.get("baseToken") or {}
                quote = p.get("quoteToken") or {}
                base_addr = (base.get("address") or "").strip()
                quote_addr = (quote.get("address") or "").strip()
                price_native = p.get("priceNative")
                if price_native is None:
                    continue
                try:
                    pr = float(price_native)
                except (TypeError, ValueError):
                    continue
                if pr <= 0:
                    continue
                is_sol = lambda a: a == wsol or "11111111111111111111" in (a or "")
                if base_addr == token_address and is_sol(quote_addr):
                    return pr
                if quote_addr == token_address and is_sol(base_addr):
                    return 1.0 / pr if pr > 0 else None
        except Exception:
            logger.debug("获取代币价格失败", exc_info=True)
        return None

    async def get_hunter_profit_on_token(
        self, client, hunter_address: str, token_address: str
    ) -> Tuple[float | None, List[dict] | None]:
        sigs_lp = await self.get_signatures(client, hunter_address, limit=SM_LP_CHECK_TX_LIMIT)
        txs_main_wallet = None
        if sigs_lp:
            txs_lp = await self.fetch_parsed_transactions(client, sigs_lp)
            if txs_lp:
                if hunter_had_any_lp_anywhere(txs_lp):
                    logger.warning(
                        "⚠️ LP 淘汰(主钱包/%d笔): %s.. 曾做过 LP，已拉黑",
                        SM_LP_CHECK_TX_LIMIT, hunter_address[:12],
                    )
                    self._add_to_wallet_blacklist(hunter_address)
                    return None, None
                if hunter_had_any_lp_on_token(txs_lp, hunter_address, token_address, ata_address=None):
                    logger.warning(
                        "⚠️ LP 淘汰(主钱包/该代币/%d笔): %s.. 已拉黑",
                        SM_LP_CHECK_TX_LIMIT, hunter_address[:12],
                    )
                    self._add_to_wallet_blacklist(hunter_address)
                    return None, None
                txs_main_wallet = txs_lp

        usdc_price = await self._get_usdc_price_sol(client)
        parser = TransactionParser(hunter_address)
        calc = TokenAttributionCalculator()

        if SM_USE_ATA_FIRST:
            ata_addrs = []
            for prog in ("Token2022", "Token"):
                try:
                    ata_addrs.append(get_associated_token_address(hunter_address, token_address, prog))
                except Exception:
                    pass
            ata_sigs = []
            for ata_addr in ata_addrs:
                if not ata_addr:
                    continue
                sigs = await self.get_signatures(client, ata_addr, limit=SM_ATA_SIG_LIMIT)
                if sigs:
                    ata_sigs = sigs
                    break
            if ata_sigs:
                txs_ata = await self.fetch_parsed_transactions(client, ata_sigs)
                if txs_ata:
                    buy_sol, sell_sol, tokens_held = 0.0, 0.0, 0.0
                    for tx in sorted(txs_ata, key=lambda x: _get_tx_timestamp(x)):
                        try:
                            sol_c, token_c, _ = parser.parse_transaction(tx, usdc_price_sol=usdc_price)
                            if token_address not in token_c or abs(token_c[token_address]) < 1e-9:
                                continue
                            buy_a, sell_a = calc.calculate_attribution(sol_c, token_c)
                            buy_sol += buy_a.get(token_address, 0)
                            sell_sol += sell_a.get(token_address, 0)
                            tokens_held += token_c[token_address]
                        except Exception:
                            continue
                    if buy_sol < 0.01:
                        return None, None
                    total_value = sell_sol
                    if tokens_held > 1e-9:
                        price = await self._get_token_price_sol(client, token_address)
                        if price and price > 0:
                            total_value += tokens_held * price
                    roi_ata = (total_value - buy_sol) / buy_sol * 100
                    if roi_ata < SM_MIN_TOKEN_PROFIT_PCT:
                        return None, None
                    if txs_main_wallet:
                        if _is_frequent_trader_by_blocktimes(sigs_lp if sigs_lp else []):
                            return None, None
                        txs_main = txs_main_wallet[: self.audit_tx_limit]
                        return roi_ata, txs_main if txs_main else None
                    sigs_main = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
                    if not sigs_main or _is_frequent_trader_by_blocktimes(sigs_main):
                        return None, None
                    txs_main = await self.fetch_parsed_transactions(client, sigs_main)
                    if not txs_main:
                        return None, None
                    return roi_ata, txs_main

        if txs_main_wallet:
            if _is_frequent_trader_by_blocktimes(sigs_lp if sigs_lp else []):
                return None, None
            txs = txs_main_wallet
        else:
            sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
            if not sigs:
                return None, None
            if _is_frequent_trader_by_blocktimes(sigs):
                return None, None
            txs = await self.fetch_parsed_transactions(client, sigs)
            if not txs:
                return None, None
        buy_sol, sell_sol, tokens_held = 0.0, 0.0, 0.0
        for tx in sorted(txs, key=lambda x: _get_tx_timestamp(x)):
            try:
                sol_c, token_c, _ = parser.parse_transaction(tx, usdc_price_sol=usdc_price)
                if token_address not in token_c or abs(token_c[token_address]) < 1e-9:
                    continue
                buy_a, sell_a = calc.calculate_attribution(sol_c, token_c)
                buy_sol += buy_a.get(token_address, 0)
                sell_sol += sell_a.get(token_address, 0)
                tokens_held += token_c[token_address]
            except Exception:
                continue
        if buy_sol < 0.01:
            return None, None
        total_value = sell_sol
        if tokens_held > 1e-9:
            price = await self._get_token_price_sol(client, token_address)
            if price and price > 0:
                total_value += tokens_held * price
        roi = (total_value - buy_sol) / buy_sol * 100
        return roi, txs

    async def verify_token_age_via_dexscreener(self, client, token_address):
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        try:
            resp = await client.get(url, timeout=DEXSCREENER_TOKEN_TIMEOUT)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get('pairs', [])
                if not pairs:
                    return False, 0, "No Pairs", 0.0, False

                main_pair = max(pairs, key=lambda p: float(p.get('liquidity', {}).get('usd', 0) or 0))
                gain_24h = main_pair.get('pricePercentChange24h')
                if gain_24h is None:
                    gain_24h = (main_pair.get('priceChange') or {}).get('h24')
                gain_24h = float(gain_24h or 0)
                # DexScreener 可能返回乘数(1.5=50%)或百分比(150)。>100 视为已百分比，1~20 视为乘数
                if gain_24h > 100:
                    pass  # 已是百分比
                elif 1 < gain_24h <= 20:
                    gain_24h = (gain_24h - 1) * 100

                created_at_ms = main_pair.get('pairCreatedAt', float('inf'))
                if created_at_ms == float('inf'):
                    return False, 0, "No Creation Time", gain_24h, False

                created_at_sec = created_at_ms / 1000
                age = time.time() - created_at_sec

                if age < MIN_TOKEN_AGE_SEC:
                    return False, created_at_sec, f"Too Young ({age / 60:.1f}m)", gain_24h, False
                if age > MAX_TOKEN_AGE_SEC:
                    return False, created_at_sec, f"Too Old ({age / 3600:.1f}h)", gain_24h, True

                if gain_24h < DEX_MIN_24H_GAIN_PCT:
                    return False, created_at_sec, f"GainNotYet ({gain_24h:.0f}% < {DEX_MIN_24H_GAIN_PCT}%)", gain_24h, False

                return True, created_at_sec, "OK", gain_24h, False
            return False, 0, "API Error", 0.0, False
        except Exception:
            logger.exception("verify_token_age_via_dexscreener 请求异常")
            return False, 0, "Exception", 0.0, False

    async def search_alpha_hunters(self, token_address):
        if token_address in self.scanned_tokens:
            return []

        async with httpx.AsyncClient() as client:
            is_valid, start_time, reason, gain_24h, should_save = await self.verify_token_age_via_dexscreener(client, token_address)
            if not is_valid:
                if "GainNotYet" in reason:
                    logger.info("📉 涨幅未达标，跳过挖掘: %s (不写 scanned，下次重试)", reason)
                else:
                    logger.info("⏭️ 跳过代币 %s: %s", token_address, reason)
                if should_save:
                    self._save_scanned_token(token_address)
                return []

            logger.info("🔍 涨幅达标 (%.0f%%≥%d%%) | 年龄 %.0fs，开始回溯...", gain_24h, DEX_MIN_24H_GAIN_PCT, time.time() - start_time)

            target_time_window = start_time + self.max_delay_sec
            current_before = None
            found_early_txs = []

            for page in range(MAX_BACKTRACK_PAGES):
                sigs = await self.get_signatures(client, token_address, limit=SM_BACKTRACK_SIGS_PER_PAGE, before=current_before)
                if not sigs:
                    break

                batch_oldest = sigs[-1].get('blockTime', 0)
                current_before = sigs[-1]['signature']

                if batch_oldest <= target_time_window:
                    for s in sigs:
                        t = s.get('blockTime', 0)
                        if start_time <= t <= target_time_window:
                            found_early_txs.append(s)
                    break

            if not found_early_txs:
                logger.warning("⚠️ 翻了%d页未触底，放弃", MAX_BACKTRACK_PAGES)
                self._save_scanned_token(token_address)
                return []

            found_early_txs.sort(key=lambda x: x.get('blockTime', 0))
            target_txs = found_early_txs[:SM_EARLY_TX_PARSE_LIMIT]
            txs = await self.fetch_parsed_transactions(client, target_txs)

            lp_participants = collect_lp_participants_from_txs(txs)
            if lp_participants:
                logger.info("  [LP 初筛] 代币早期交易中发现 %d 个 LP 参与者，已排除", len(lp_participants))

            hunters_candidates = []
            seen_buyers = set()
            usdc_price = await self._get_usdc_price_sol(client)

            for tx in txs:
                block_time = _get_tx_timestamp(tx)
                delay = block_time - start_time
                if delay < self.min_delay_sec:
                    continue

                spend_by_addr: Dict[str, float] = defaultdict(float)
                for nt in tx.get('nativeTransfers', []):
                    addr = nt.get('fromUserAccount')
                    if addr:
                        spend_by_addr[addr] += nt.get('amount', 0) / 1e9
                for tt in tx.get('tokenTransfers', []):
                    mint = tt.get('mint')
                    addr = tt.get('fromUserAccount')
                    if not addr:
                        continue
                    amt = _normalize_token_amount(tt.get('tokenAmount'))
                    if mint == WSOL_MINT:
                        spend_by_addr[addr] += amt
                    elif mint == USDC_MINT and usdc_price and usdc_price > 0:
                        spend_by_addr[addr] += amt * usdc_price

                if not spend_by_addr:
                    continue
                spender = max(spend_by_addr, key=spend_by_addr.get)
                spend_sol = spend_by_addr[spender]
                if spender in seen_buyers:
                    continue
                if spender in lp_participants:
                    continue
                if SM_MIN_BUY_SOL <= spend_sol <= SM_MAX_BUY_SOL:
                    seen_buyers.add(spender)
                    hunters_candidates.append({"address": spender, "entry_delay": delay, "cost": spend_sol})

            logger.info("  [初筛] 15秒后买入且金额合规: %d 个", len(hunters_candidates))

            verified_hunters = []
            pnl_passed_count = 0
            total = len(hunters_candidates)
            progress_interval = max(1, total // 10)
            for idx, candidate in enumerate(hunters_candidates, 1):
                if idx == 1 or idx % progress_interval == 0 or idx == total:
                    pct = idx * 100 // total
                    logger.info("  [进度] %d/%d (%d%%) | 符合PnL %d 个 | 已入库 %d 个", idx, total, pct, pnl_passed_count, len(verified_hunters))
                addr = candidate["address"]
                if addr in self.wallet_blacklist:
                    continue
                roi, txs_reuse = await self.get_hunter_profit_on_token(client, addr, token_address)
                if roi is None or roi < SM_MIN_TOKEN_PROFIT_PCT:
                    await asyncio.sleep(SM_SEARCHER_CANDIDATE_SKIP_SLEEP_SEC)
                    continue
                pnl_passed_count += 1

                stats = await self.analyze_hunter_performance(
                    client, addr, exclude_token=token_address, pre_fetched_txs=txs_reuse
                )

                if stats:
                    max_roi_30d = max(roi, stats.get("max_roi_30d", 0))
                    if max_roi_30d >= TIER_ONE_ROI:
                        roi_mult = SM_ROI_MULT_ONE
                    elif max_roi_30d >= TIER_TWO_ROI:
                        roi_mult = SM_ROI_MULT_TWO
                    else:
                        roi_mult = SM_ROI_MULT_THREE

                    score_result = compute_hunter_score(stats)
                    base_score = score_result["score"]
                    final_score = round(base_score * roi_mult, 1)

                    trade_count = stats.get("count", 0)
                    pnl_ok = stats.get("pnl_ratio", 0) >= SM_ENTRY_MIN_PNL_RATIO
                    wr_ok = stats["win_rate"] >= SM_ENTRY_MIN_WIN_RATE
                    count_ok = trade_count >= SM_ENTRY_MIN_TRADE_COUNT
                    profit_ok = stats["total_profit"] > 0
                    roi_ok = max_roi_30d >= TIER_THREE_ROI
                    is_qualified = pnl_ok and wr_ok and count_ok and profit_ok and roi_ok

                    if is_qualified:
                        avg_roi = stats.get("avg_roi_pct", 0.0)
                        pnl_ratio_val = stats.get("pnl_ratio", 0)
                        pnl_ratio_str = f"{pnl_ratio_val:.2f}" if pnl_ratio_val != float("inf") else "∞"
                        candidate.update({
                            "score": final_score,
                            "win_rate": f"{stats['win_rate']:.1%}",
                            "pnl_ratio": pnl_ratio_str,
                            "total_profit": f"{stats['total_profit']:.2f} SOL",
                            "avg_roi_pct": f"{avg_roi:.1f}%",
                            "scores_detail": score_result["scores_detail"],
                            "max_roi_30d": max_roi_30d,
                        })
                        candidate.pop("entry_delay", None)
                        candidate.pop("cost", None)
                        verified_hunters.append(candidate)
                        logger.info("    ✅ 锁定猎手 %s.. | 利润: %s | 评分: %s (×%s)", addr[:12], candidate["total_profit"], final_score, roi_mult)
                    else:
                        reasons = []
                        if not roi_ok:
                            reasons.append(f"单token最大收益{max_roi_30d:.0f}%%<{TIER_THREE_ROI}%%")
                        if not pnl_ok and stats.get("pnl_ratio", 0) >= SM_ENTRY_MIN_PNL_RATIO * SM_NEAR_ENTRY_THRESHOLD:
                            reasons.append(f"盈亏比{stats.get('pnl_ratio', 0):.2f}<{SM_ENTRY_MIN_PNL_RATIO}")
                        if not wr_ok and stats["win_rate"] >= SM_ENTRY_MIN_WIN_RATE * SM_NEAR_ENTRY_THRESHOLD:
                            reasons.append(f"胜率{stats['win_rate']*100:.1f}%<{SM_ENTRY_MIN_WIN_RATE*100:.0f}%")
                        if not count_ok and trade_count >= SM_ENTRY_MIN_TRADE_COUNT * SM_NEAR_ENTRY_THRESHOLD:
                            reasons.append(f"交易笔数{trade_count}<{SM_ENTRY_MIN_TRADE_COUNT}")
                        if not profit_ok and stats["total_profit"] > -0.5:
                            reasons.append("总盈利非正")
                        if reasons:
                            logger.info("[落榜钱包地址] %s | 原因: %s", addr, " | ".join(reasons))
                await asyncio.sleep(SM_SEARCHER_WALLET_SLEEP_SEC)

            logger.info("  [收益+评分] 初筛 %d → ROI≥%.0f%%: %d 个 → 入库 %d 个", total, SM_MIN_TOKEN_PROFIT_PCT, pnl_passed_count, len(verified_hunters))
            self._save_scanned_token(token_address)
            return verified_hunters

    async def run_pipeline(self, dex_scanner_instance):
        logger.info("启动 Alpha 猎手挖掘 (涨幅>%d%%才挖 | 未达标不写scanned便于重试)", DEX_MIN_24H_GAIN_PCT)
        hot_tokens = await dex_scanner_instance.scan()
        all_hunters = []
        if hot_tokens:
            hot_tokens.sort(key=lambda t: float(t.get('gain_24h_pct', 0)), reverse=True)
            for token in hot_tokens:
                addr = token.get('address')
                sym = token.get('symbol')
                if addr in self.scanned_tokens:
                    logger.info("⏭️ 跳过已扫描代币: %s (%s)", sym, addr[:16] + "..")
                    continue
                logger.info("=== 正在挖掘: %s ===", sym)
                try:
                    hunters = await self.search_alpha_hunters(addr)
                    if hunters:
                        all_hunters.extend(hunters)
                except Exception:
                    logger.exception("❌ 挖掘代币 %s 出错", sym)
                await asyncio.sleep(SM_SEARCHER_TOKEN_SLEEP_SEC)
        all_hunters.sort(key=lambda x: float(x.get('score', 0) or 0), reverse=True)
        return all_hunters
