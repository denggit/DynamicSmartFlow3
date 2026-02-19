#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@Author  : Zijun Deng
@Date    : 2/17/2026
@File    : sm_searcher.py
@Description: Smart Money Searcher V7 - 热门币猎手挖掘
              1. 热门币筛选: DexScreener 过去24小时涨幅 > 1000%
              2. 代币年龄: 放宽至 12 小时内
              3. 回溯: 最多 20 页
              4. 初筛买家: 开盘 15 秒后买入，且在该代币至少赚取 200%（已清仓或未清仓）
              5. 筛选后的钱包做评分入库
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import Dict, Tuple, Set

import httpx

from config.settings import (
    helius_key_pool,
    MIN_TOKEN_AGE_SEC,
    MAX_TOKEN_AGE_SEC,
    MAX_BACKTRACK_PAGES,
    RECENT_TX_COUNT_FOR_FREQUENCY,
    MIN_AVG_TX_INTERVAL_SEC,
    MIN_NATIVE_LAMPORTS_FOR_REAL,
    SCANNED_HISTORY_FILE,
    SM_MIN_DELAY_SEC,
    SM_MAX_DELAY_SEC,
    SM_AUDIT_TX_LIMIT,
    SM_MIN_BUY_SOL,
    SM_MAX_BUY_SOL,
    SM_MIN_TOKEN_PROFIT_PCT,
    SM_MIN_WIN_RATE,
    SM_MIN_TOTAL_PROFIT,
    SM_MIN_HUNTER_SCORE,
)
from utils.logger import get_logger

logger = get_logger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# 与 SmartFlow3 一致：只把「真实买卖」算作交易，忽略 SOL/USDC/USDT 等
IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
}
MIN_NATIVE_LAMPORTS_FOR_REAL = int(0.01 * 1e9)  # 至少 0.01 SOL 的 native 转账才算「真实」


def tx_has_real_trade(tx: dict) -> bool:
    """
    判断该笔链上交易是否包含「真实交易」：非纯授权/失败/粉尘。
    与 SmartFlow3 一致：存在非 IGNORE 代币的 tokenTransfer，或 meaningful 的 nativeTransfer。
    """
    for tt in tx.get("tokenTransfers", []):
        if tt.get("mint") and tt["mint"] not in IGNORE_MINTS:
            return True
    for nt in tx.get("nativeTransfers", []):
        if (nt.get("amount") or 0) >= MIN_NATIVE_LAMPORTS_FOR_REAL:
            return True
    return False


def is_real_trade_for_address(tx: dict, address: str) -> bool:
    """
    判断该笔交易对给定地址而言是否为「真实交易」：该地址参与了非 IGNORE 的 token 或 meaningful 的 native。
    """
    for tt in tx.get("tokenTransfers", []):
        if tt.get("mint") in IGNORE_MINTS:
            continue
        if tt.get("fromUserAccount") == address or tt.get("toUserAccount") == address:
            return True
    for nt in tx.get("nativeTransfers", []):
        if (nt.get("amount") or 0) < MIN_NATIVE_LAMPORTS_FOR_REAL:
            continue
        if nt.get("fromUserAccount") == address or nt.get("toUserAccount") == address:
            return True
    return False


def _is_frequent_trader_by_real_txs(txs: list, address: str) -> bool:
    """
    根据「真实交易」时间戳计算平均间隔；只统计该地址参与的真实买卖。
    txs: 已解析的交易列表（与 sigs 顺序一致，新在前），来自 fetch_parsed_transactions。
    """
    real_ts = []
    for tx in txs:
        if len(real_ts) >= RECENT_TX_COUNT_FOR_FREQUENCY:
            break
        if not is_real_trade_for_address(tx, address):
            continue
        ts = tx.get("timestamp")
        if ts is not None:
            real_ts.append(ts)
    if len(real_ts) < 2:
        return False
    real_ts.sort()
    span = real_ts[-1] - real_ts[0]
    avg_interval = span / (len(real_ts) - 1)
    return avg_interval < MIN_AVG_TX_INTERVAL_SEC


def _normalize_token_amount(raw) -> float:
    """将 Helius tokenAmount 转为浮点数。支持数字或对象 { amount: string, decimals: int }（与 SmartFlow3 一致）。"""
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    if isinstance(raw, dict):
        amount = float(raw.get("amount") or 0)
        decimals = int(raw.get("decimals") or 0)
        return amount / (10 ** decimals) if decimals else amount
    return float(raw)


class TransactionParser:
    def __init__(self, target_wallet: str):
        self.target_wallet = target_wallet
        self.wsol_mint = "So11111111111111111111111111111111111111112"

    def parse_transaction(self, tx: dict) -> Tuple[float, Dict[str, float], int]:
        timestamp = tx.get('timestamp', 0)
        native_sol_change = 0.0
        wsol_change = 0.0
        token_changes = defaultdict(float)

        for nt in tx.get('nativeTransfers', []):
            if nt.get('fromUserAccount') == self.target_wallet:
                native_sol_change -= nt.get('amount', 0) / 1e9
            if nt.get('toUserAccount') == self.target_wallet:
                native_sol_change += nt.get('amount', 0) / 1e9

        for tt in tx.get('tokenTransfers', []):
            mint = tt.get('mint', '')
            amt = _normalize_token_amount(tt.get('tokenAmount'))
            if mint == self.wsol_mint:
                if tt.get('fromUserAccount') == self.target_wallet:
                    wsol_change -= amt
                if tt.get('toUserAccount') == self.target_wallet:
                    wsol_change += amt
            else:
                if tt.get('fromUserAccount') == self.target_wallet:
                    token_changes[mint] -= amt
                if tt.get('toUserAccount') == self.target_wallet:
                    token_changes[mint] += amt

        sol_change = 0.0
        if abs(native_sol_change) < 1e-9:
            sol_change = wsol_change
        elif abs(wsol_change) < 1e-9:
            sol_change = native_sol_change
        elif native_sol_change * wsol_change > 0:
            sol_change = native_sol_change if abs(native_sol_change) > abs(wsol_change) else wsol_change
        else:
            sol_change = native_sol_change + wsol_change

        return sol_change, dict(token_changes), timestamp


class TokenAttributionCalculator:
    @staticmethod
    def calculate_attribution(sol_change: float, token_changes: Dict[str, float]):
        buy_attrs, sell_attrs = {}, {}
        if abs(sol_change) < 1e-9: return buy_attrs, sell_attrs
        buys = {m: a for m, a in token_changes.items() if a > 0}
        sells = {m: abs(a) for m, a in token_changes.items() if a < 0}

        if sol_change < 0:
            total = sum(buys.values())
            if total > 0:
                cost_per = abs(sol_change) / total
                for m, a in buys.items(): buy_attrs[m] = cost_per * a
        elif sol_change > 0:
            total = sum(sells.values())
            if total > 0:
                gain_per = sol_change / total
                for m, a in sells.items(): sell_attrs[m] = gain_per * a
        return buy_attrs, sell_attrs


class SmartMoneySearcher:
    def __init__(self):
        self._pool = helius_key_pool
        self._update_urls()

    def _update_urls(self):
        """从 Key 池更新当前 RPC / API URL。"""
        self.api_key = self._pool.get_api_key()
        self.rpc_url = self._pool.get_rpc_url()
        self.base_api_url = "https://api.helius.xyz/v0"

        # 初筛参数 (来自 config/settings.py)
        self.min_delay_sec = SM_MIN_DELAY_SEC
        self.max_delay_sec = SM_MAX_DELAY_SEC
        self.audit_tx_limit = SM_AUDIT_TX_LIMIT

        self.scanned_tokens: Set[str] = set()
        self._load_scanned_history()

    def _ensure_data_dir(self):
        if not os.path.exists("data"):
            os.makedirs("data")

    def _load_scanned_history(self):
        self._ensure_data_dir()
        if os.path.exists(SCANNED_HISTORY_FILE):
            try:
                with open(SCANNED_HISTORY_FILE, 'r') as f:
                    self.scanned_tokens = set(json.load(f))
                logger.info(f"📂 已加载 {len(self.scanned_tokens)} 个历史扫描代币记录")
            except Exception:
                logger.exception("⚠️ 加载扫描历史失败")

    def _save_scanned_token(self, token_address: str):
        if token_address in self.scanned_tokens: return
        self.scanned_tokens.add(token_address)
        try:
            with open(SCANNED_HISTORY_FILE, 'w') as f:
                json.dump(list(self.scanned_tokens), f)
        except Exception:
            logger.exception("保存扫描历史失败")

    async def _rpc_post(self, client, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}

        # === [新增] 重试机制 (最多试 3 次) ===
        max_retries = 3
        base_delay = 1.0

        for attempt in range(max_retries):
            try:
                # [优化] 增加 timeout 到 30秒，防止深翻页时超时
                resp = await client.post(self.rpc_url, json=payload, timeout=30.0)

                if resp.status_code == 200:
                    data = resp.json()
                    if "result" in data:
                        return data["result"]
                    elif "error" in data:
                        # 如果是限流错误 (429)，记录并重试
                        err_msg = data.get("error", {}).get("message", "")
                        if "Rate limit" in err_msg or "429" in str(resp.status_code):
                            logger.warning("⚠️ RPC 限流 (尝试 %s/%s)，切换 Key: %s", attempt + 1, max_retries, err_msg)
                            self._pool.mark_current_failed()
                            self._update_urls()
                        else:
                            # 其他业务错误直接返回 None
                            # logger.warning(f"RPC 业务错误: {err_msg}")
                            return None
                elif resp.status_code == 429:
                    logger.warning("⚠️ RPC HTTP 429 限流 (尝试 %s/%s)，切换 Key", attempt + 1, max_retries)
                    self._pool.mark_current_failed()
                    self._update_urls()
                else:
                    logger.warning(f"RPC 请求失败: {resp.status_code}")

            except (httpx.TimeoutException, httpx.NetworkError) as e:
                logger.warning("⚠️ RPC 网络波动 (尝试 %s/%s): %s", attempt + 1, max_retries, e)
            except Exception:
                logger.exception("❌ RPC 未知错误")
                return None

            # 指数退避：每次失败多睡一会儿 (1s -> 2s -> 4s)
            if attempt < max_retries - 1:
                sleep_time = base_delay * (2 ** attempt)
                await asyncio.sleep(sleep_time)

        logger.error(f"❌ RPC {method} 最终失败，已重试 {max_retries} 次")
        return None

    async def get_signatures(self, client, address, limit=100, before=None):
        params = [address, {"limit": limit}]
        if before:
            params[1]["before"] = before
        return await self._rpc_post(client, "getSignaturesForAddress", params)

    async def is_frequent_trader(self, client, address: str) -> bool:
        """
        判断该地址是否为「频繁交易」：最近 100 笔「真实交易」平均间隔 < 5 分钟。
        只统计该地址参与的真实买卖（非 IGNORE 代币 / meaningful native），与 SmartFlow3 一致。
        """
        sigs = await self.get_signatures(client, address, limit=RECENT_TX_COUNT_FOR_FREQUENCY)
        if not sigs:
            return False
        txs = await self.fetch_parsed_transactions(client, sigs)
        return _is_frequent_trader_by_real_txs(txs or [], address)

    async def fetch_parsed_transactions(self, client, signatures):
        if not signatures: return []
        chunk_size = 90
        all_txs = []
        for i in range(0, len(signatures), chunk_size):
            batch = signatures[i:i + chunk_size]
            payload = {"transactions": [s['signature'] for s in batch]}
            url = f"{self.base_api_url}/transactions?api-key={self.api_key}"
            try:
                resp = await client.post(url, json=payload, timeout=30.0)
                if resp.status_code == 200:
                    all_txs.extend(resp.json())
                elif resp.status_code == 429 and self._pool.size > 1:
                    self._pool.mark_current_failed()
                    self._update_urls()
            except Exception:
                logger.exception("fetch_parsed_transactions 批量请求异常")
        return all_txs

    async def analyze_hunter_performance(self, client, hunter_address, exclude_token=None):
        sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
        if not sigs:
            return None
        txs = await self.fetch_parsed_transactions(client, sigs)
        if not txs:
            return None
        # 频繁交易过滤：只统计「真实交易」，最近 100 笔真实买卖平均间隔 < 5 分钟则剔除
        if _is_frequent_trader_by_real_txs(txs, hunter_address):
            logger.info("⏭️ 剔除频繁交易地址 %s.. (真实交易平均间隔<5分钟)", hunter_address)
            return None
        if not txs: return None

        parser = TransactionParser(hunter_address)
        calc = TokenAttributionCalculator()
        projects = defaultdict(lambda: {"buy_sol": 0.0, "sell_sol": 0.0, "tokens": 0.0})

        txs.sort(key=lambda x: x.get('timestamp', 0))
        for tx in txs:
            try:
                sol_change, token_changes, _ = parser.parse_transaction(tx)
                if not token_changes: continue
                buy_attrs, sell_attrs = calc.calculate_attribution(sol_change, token_changes)
                for mint, delta in token_changes.items():
                    if exclude_token and mint == exclude_token: continue
                    if abs(delta) < 1e-9: continue
                    projects[mint]["tokens"] += delta
                    if mint in buy_attrs: projects[mint]["buy_sol"] += buy_attrs[mint]
                    if mint in sell_attrs: projects[mint]["sell_sol"] += sell_attrs[mint]
            except Exception:
                logger.debug("解析单笔交易跳过", exc_info=True)
                continue

        valid_projects = []
        for mint, data in projects.items():
            if data["buy_sol"] > 0.05:
                net_profit = data["sell_sol"] - data["buy_sol"]
                roi = (net_profit / data["buy_sol"]) * 100
                valid_projects.append({"profit": net_profit, "roi": roi, "cost": data["buy_sol"]})

        if not valid_projects: return None

        recent = valid_projects[-15:]
        total_profit = sum(p["profit"] for p in recent)
        wins = [p for p in recent if p["profit"] > 0]
        win_rate = len(wins) / len(recent)
        worst_roi = max(-100, min([p["roi"] for p in recent])) if recent else 0

        return {"win_rate": win_rate, "worst_roi": worst_roi, "total_profit": total_profit, "count": len(recent)}

    async def _get_token_price_sol(self, client, token_address: str) -> float | None:
        """
        从 DexScreener 获取代币当前价格 (1 token = ? SOL)，用于计算未实现收益。
        """
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        try:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code != 200:
                return None
            data = resp.json()
            pairs = data.get("pairs", [])
            wsol = "So11111111111111111111111111111111111111112"
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

    async def get_hunter_profit_on_token(self, client, hunter_address: str, token_address: str) -> float | None:
        """
        计算猎手在该代币上的收益率 (ROI %)，已清仓用卖出收益算，未清仓用现价估算。
        返回 ROI 百分比，若无法计算返回 None。
        """
        sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
        if not sigs:
            return None
        txs = await self.fetch_parsed_transactions(client, sigs)
        if not txs:
            return None
        parser = TransactionParser(hunter_address)
        calc = TokenAttributionCalculator()
        buy_sol, sell_sol, tokens_held = 0.0, 0.0, 0.0
        txs.sort(key=lambda x: x.get("timestamp", 0))
        for tx in txs:
            try:
                sol_change, token_changes, _ = parser.parse_transaction(tx)
                if token_address not in token_changes:
                    continue
                delta = token_changes[token_address]
                if abs(delta) < 1e-9:
                    continue
                buy_attrs, sell_attrs = calc.calculate_attribution(sol_change, token_changes)
                if token_address in buy_attrs:
                    buy_sol += buy_attrs[token_address]
                if token_address in sell_attrs:
                    sell_sol += sell_attrs[token_address]
                tokens_held += delta
            except Exception:
                continue
        if buy_sol < 0.01:
            return None
        total_value = sell_sol
        if tokens_held > 1e-9:
            price = await self._get_token_price_sol(client, token_address)
            if price is not None and price > 0:
                total_value += tokens_held * price
        roi = (total_value - buy_sol) / buy_sol * 100
        return roi

    async def verify_token_age_via_dexscreener(self, client, token_address):
        """返回: (is_valid_window, start_time, reason)"""
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        try:
            resp = await client.get(url, timeout=5.0)
            if resp.status_code == 200:
                data = resp.json()
                pairs = data.get('pairs', [])
                if not pairs: return False, 0, "No Pairs"

                created_at_ms = min([p.get('pairCreatedAt', float('inf')) for p in pairs])
                if created_at_ms == float('inf'): return False, 0, "No Creation Time"

                created_at_sec = created_at_ms / 1000
                age = time.time() - created_at_sec

                if age < MIN_TOKEN_AGE_SEC:
                    return False, created_at_sec, f"Too Young ({age / 60:.1f}m)"
                if age > MAX_TOKEN_AGE_SEC:
                    return False, created_at_sec, f"Too Old ({age / 3600:.1f}h)"

                return True, created_at_sec, "OK"
            else:
                return False, 0, "API Error"
        except Exception:
            logger.exception("verify_token_age_via_dexscreener 请求异常")
            return False, 0, "Exception"

    async def search_alpha_hunters(self, token_address):
        if token_address in self.scanned_tokens: return []

        async with httpx.AsyncClient() as client:
            # 1. 严格的年龄检查 (15m - 3h)
            is_valid, start_time, reason = await self.verify_token_age_via_dexscreener(client, token_address)
            if not is_valid:
                logger.info(f"⏭️ 跳过代币 {token_address}: {reason}")
                return []

            logger.info(f"🔍 锁定黄金窗口代币 (年龄 {time.time() - start_time:.0f}s)，开始高效回溯...")

            # 2. 回溯翻页 (因为只挖3小时内的币，翻页压力很小)
            target_time_window = start_time + self.max_delay_sec
            current_before = None
            found_early_txs = []

            for page in range(MAX_BACKTRACK_PAGES):
                sigs = await self.get_signatures(client, token_address, limit=1000, before=current_before)
                if not sigs: break

                batch_oldest = sigs[-1].get('blockTime', 0)
                current_before = sigs[-1]['signature']

                if batch_oldest <= target_time_window:
                    logger.info(f"  🎯 第{page + 1}页触达开盘区间")
                    for s in sigs:
                        t = s.get('blockTime', 0)
                        if start_time <= t <= target_time_window:
                            found_early_txs.append(s)
                    break
                else:
                    logger.info(
                        f"  📖 第{page + 1}页 (时间: {time.strftime('%H:%M', time.gmtime(batch_oldest))}) -> 继续回溯")

            if not found_early_txs:
                logger.warning(f"⚠️ 翻了{MAX_BACKTRACK_PAGES}页未触底，放弃")
                self._save_scanned_token(token_address)
                return []

            # 3. 解析交易 (同前)
            found_early_txs.sort(key=lambda x: x.get('blockTime', 0))
            target_txs = found_early_txs[:100]
            txs = await self.fetch_parsed_transactions(client, target_txs)

            hunters_candidates = []
            seen_buyers = set()

            for tx in txs:
                block_time = tx.get('timestamp', 0)
                delay = block_time - start_time
                if delay < self.min_delay_sec: continue

                spender = None
                max_spend = 0
                for nt in tx.get('nativeTransfers', []):
                    amt = nt.get('amount', 0)
                    if amt > max_spend:
                        max_spend = amt
                        spender = nt.get('fromUserAccount')

                if not spender or spender in seen_buyers: continue
                spend_sol = max_spend / 1e9
                if SM_MIN_BUY_SOL <= spend_sol <= SM_MAX_BUY_SOL:
                    seen_buyers.add(spender)
                    hunters_candidates.append({"address": spender, "entry_delay": delay, "cost": spend_sol})

            logger.info(f"  [初筛] 15秒后买入且金额合规: {len(hunters_candidates)} 个")

            # 3.5 过滤：该代币上至少赚取 200%（已清仓或未清仓）
            profit_filtered = []
            for candidate in hunters_candidates:
                roi = await self.get_hunter_profit_on_token(client, candidate["address"], token_address)
                if roi is not None and roi >= SM_MIN_TOKEN_PROFIT_PCT:
                    profit_filtered.append(candidate)
                    logger.debug(f"    通过 200%% 收益过滤: {candidate['address'][:12]}.. ROI={roi:.0f}%%")
                await asyncio.sleep(0.3)
            hunters_candidates = profit_filtered
            logger.info(f"  [初筛] 在该代币赚取≥{SM_MIN_TOKEN_PROFIT_PCT:.0f}%: {len(hunters_candidates)} 个")

            # 4. 深度审计 + 评分入库
            verified_hunters = []
            for candidate in hunters_candidates:
                addr = candidate["address"]
                stats = await self.analyze_hunter_performance(client, addr, exclude_token=token_address)

                if stats:
                    score_hit_rate = stats["win_rate"]
                    delay = candidate["entry_delay"]
                    score_entry = max(0, 1 - (delay / self.max_delay_sec))
                    score_drawdown = 1 - abs(stats["worst_roi"] / 100)
                    score_drawdown = max(0, min(1, score_drawdown))

                    final_score = (score_hit_rate * 30) + (score_entry * 40) + (score_drawdown * 30)
                    final_score = round(final_score, 1)

                    is_qualified = False
                    if stats["total_profit"] > 0.1:
                        if stats["win_rate"] >= SM_MIN_WIN_RATE:
                            is_qualified = True
                        elif stats["total_profit"] >= SM_MIN_TOTAL_PROFIT:
                            is_qualified = True

                    if is_qualified:
                        candidate.update({
                            "score": final_score,
                            "win_rate": f"{stats['win_rate']:.1%}",
                            "worst_roi": f"{stats['worst_roi']:.1f}%",
                            "total_profit": f"{stats['total_profit']:.2f} SOL",
                            "scores_detail": f"H:{score_hit_rate:.2f}/E:{score_entry:.2f}/D:{score_drawdown:.2f}"
                        })
                        verified_hunters.append(candidate)
                        logger.info(
                            f"    ✅ 锁定猎手 {addr}.. | 利润: {candidate['total_profit']} | 评分: {final_score}")
                await asyncio.sleep(0.5)

            self._save_scanned_token(token_address)
            return verified_hunters

    async def run_pipeline(self, dex_scanner_instance):
        logger.info("启动 Alpha 猎手挖掘 (V7 热门币版: 24h涨幅>1000% | 代币≤12h | 初筛15s后买入且≥200%收益)")
        hot_tokens = await dex_scanner_instance.scan()
        all_hunters = []
        if hot_tokens:
            for token in hot_tokens:
                addr = token.get('address')
                sym = token.get('symbol')
                if addr in self.scanned_tokens:
                    logger.info("⏭️ 跳过已扫描代币: %s (%s)", sym, addr[:16] + "..")
                    continue
                logger.info(f"=== 正在挖掘: {sym} ===")
                try:
                    hunters = await self.search_alpha_hunters(addr)
                    if hunters: all_hunters.extend(hunters)
                except Exception:
                    logger.exception("❌ 挖掘代币 %s 出错", sym)
                await asyncio.sleep(1)
        all_hunters.sort(key=lambda x: x.get('score', 0), reverse=True)
        # 只保留 60 分及以上猎手，与猎手池入库规则一致
        return [h for h in all_hunters if h.get('score', 0) >= SM_MIN_HUNTER_SCORE]


if __name__ == "__main__":
    from services.dexscreener.dex_scanner import DexScanner


    async def main():
        searcher = SmartMoneySearcher()
        mock_scanner = DexScanner()
        results = await searcher.run_pipeline(mock_scanner)
        logger.info("====== 最终挖掘结果 (%s) ======", len(results))
        for res in results:
            logger.info("%s", res)


    asyncio.run(main())
