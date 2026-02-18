#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@Author  : Zijun Deng
@Date    : 2/17/2026
@File    : sm_searcher.py
@Description: Smart Money Searcher V6 - Golden Window Edition
              1. [策略调整] 放弃挖掘老币，只挖掘上市 15分钟 - 3小时 的代币
              2. [成本控制] 因为币比较新，回溯翻页次数极少 (通常<5次)，大幅节省 API
              3. [去重逻辑] 保持 scanned_tokens.json 避免重复劳动
"""

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from typing import Dict, Tuple, Set

import httpx

from config.settings import helius_key_pool
from utils.logger import get_logger

logger = get_logger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SCANNED_HISTORY_FILE = "data/scanned_tokens.json"

# === 核心策略参数 ===
MIN_TOKEN_AGE_SEC = 900  # 最少上市 15分钟 (排除纯土狗/貔貅)
MAX_TOKEN_AGE_SEC = 10800  # 最多上市 3小时 (太老的币数据太深，不挖了)
MAX_BACKTRACK_PAGES = 10  # 最多回溯10页 (1万笔交易)，对于3小时内的币通常足够

# 猎手筛选：按「每个 token 持仓时间」判断，理想区间 5分钟～1天
RECENT_TX_COUNT_FOR_FREQUENCY = 100
MIN_HOLD_SEC = 300        # 单次持仓最少 5 分钟（低于此= scalp 过于频繁）
MAX_HOLD_SEC = 86400      # 单次持仓最多 1 天（超过此= 长持不符合跟单节奏）
MAX_PCT_UNDER_5MIN = 0.5  # 超 50% 持仓 <5 分钟 → 踢出（频繁）
MAX_PCT_OVER_1DAY = 0.5   # 超 50% 持仓 >1 天 → 踢出（太慢）
MIN_HOLDING_SAMPLES = 3   # 至少 3 个完整回合（买+卖）才做判断

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


def _compute_avg_holding_per_token(txs: list, address: str) -> list:
    """
    按「单币」计算：每个 token 独立解析其所有买→卖回合的持仓时长，取该 token 的平均。
    返回各 token 的平均持仓时长列表（秒），每个 token 一个数。
    只统计非 IGNORE 代币。
    """
    parser = TransactionParser(address)
    # mint -> [(amount, buy_ts), ...] 该 token 的买入队列（单币内部按时间先后匹配卖）
    buy_queues: Dict[str, list] = defaultdict(list)
    # mint -> [hold_sec, ...] 该 token 各回合的持仓时长
    hold_per_mint: Dict[str, list] = defaultdict(list)

    sorted_txs = sorted([t for t in txs if t.get("timestamp")], key=lambda x: x.get("timestamp", 0))
    for tx in sorted_txs:
        _, token_changes, _ = parser.parse_transaction(tx)
        ts = tx.get("timestamp", 0)
        for mint, delta in token_changes.items():
            if mint in IGNORE_MINTS or abs(delta) < 1e-9:
                continue
            if delta > 0:  # 买入
                buy_queues[mint].append((delta, ts))
            else:  # 卖出：与该 token 的买入按时间先后匹配
                to_sell = abs(delta)
                while to_sell > 1e-9 and buy_queues[mint]:
                    amt, buy_ts = buy_queues[mint].pop(0)
                    close_amt = min(amt, to_sell)
                    hold_sec = ts - buy_ts
                    if hold_sec > 0:
                        hold_per_mint[mint].append(hold_sec)
                    if amt > close_amt + 1e-9:
                        buy_queues[mint].insert(0, (amt - close_amt, buy_ts))
                    to_sell -= close_amt

    # 每个 token 一个平均持仓时长
    avg_per_token = []
    for mint, holds in hold_per_mint.items():
        if holds:
            avg_per_token.append(sum(holds) / len(holds))
    return avg_per_token


def _should_kick_by_holding_times(avg_per_token: list) -> bool:
    """
    按单币平均持仓判断：>50% 的 token 平均持仓 <5min 为过于频繁，
    >50% 的 token 平均持仓 >1天 为过于缓慢。
    """
    if len(avg_per_token) < MIN_HOLDING_SAMPLES:
        return False
    total = len(avg_per_token)
    count_short = sum(1 for h in avg_per_token if h < MIN_HOLD_SEC)
    count_long = sum(1 for h in avg_per_token if h > MAX_HOLD_SEC)
    if count_short / total > MAX_PCT_UNDER_5MIN:
        return True  # 过于频繁
    if count_long / total > MAX_PCT_OVER_1DAY:
        return True  # 过于缓慢
    return False


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

        # 初筛参数
        self.min_delay_sec = 5
        self.max_delay_sec = 900  # 15分钟
        self.audit_tx_limit = 500

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
        判断该地址是否应踢出：按每个 token 持仓时间统计，
        >50% 持仓 <5 分钟（过于频繁）或 >50% 持仓 >1 天（过于缓慢）则踢出。
        """
        sigs = await self.get_signatures(client, address, limit=RECENT_TX_COUNT_FOR_FREQUENCY)
        if not sigs:
            return False
        txs = await self.fetch_parsed_transactions(client, sigs)
        avg_per_token = _compute_avg_holding_per_token(txs or [], address)
        return _should_kick_by_holding_times(avg_per_token)

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
        avg_per_token = _compute_avg_holding_per_token(txs, hunter_address)
        if _should_kick_by_holding_times(avg_per_token):
            n_short = sum(1 for h in avg_per_token if h < MIN_HOLD_SEC)
            n_long = sum(1 for h in avg_per_token if h > MAX_HOLD_SEC)
            total = len(avg_per_token)
            if n_short / total > MAX_PCT_UNDER_5MIN:
                logger.info("⏭️ 剔除地址 %s.. (%.0f%% 持仓<5分钟，过于频繁)", hunter_address, n_short / total * 100)
            else:
                logger.info("⏭️ 剔除地址 %s.. (%.0f%% 持仓>1天，过于缓慢)", hunter_address, n_long / total * 100)
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
                if 0.1 <= spend_sol <= 50.0:
                    seen_buyers.add(spender)
                    hunters_candidates.append({"address": spender, "entry_delay": delay, "cost": spend_sol})

            logger.info(f"  [初筛] 发现 {len(hunters_candidates)} 个候选人")

            # 4. 深度审计
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
                        if stats["win_rate"] >= 0.4:
                            is_qualified = True
                        elif stats["total_profit"] >= 2.0:
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
        logger.info("启动 Alpha 猎手挖掘 (V6 黄金窗口版)...")
        hot_tokens = await dex_scanner_instance.scan()
        all_hunters = []
        if hot_tokens:
            for token in hot_tokens:
                addr = token.get('address')
                sym = token.get('symbol')
                if addr in self.scanned_tokens: continue
                logger.info(f"=== 正在挖掘: {sym} ===")
                try:
                    hunters = await self.search_alpha_hunters(addr)
                    if hunters: all_hunters.extend(hunters)
                except Exception:
                    logger.exception("❌ 挖掘代币 %s 出错", sym)
                await asyncio.sleep(1)
        all_hunters.sort(key=lambda x: x.get('score', 0), reverse=True)
        return all_hunters


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
