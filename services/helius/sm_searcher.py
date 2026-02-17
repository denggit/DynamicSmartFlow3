#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@Author  : Zijun Deng
@Date    : 2/16/2026
@File    : sm_searcher.py
@Description: Smart Money Searcher V5.1 - Hybrid Model
              1. 筛选标准：回归 V4 (盈利优先)
              2. 评分系统：应用 V5 (胜率/时机/亏损) 用于后续优胜劣汰
"""

import asyncio
import logging
from collections import defaultdict
from typing import Dict, Tuple

import httpx

# 导入配置
from config.settings import HELIUS_API_KEY

# 配置日志
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


# === 轻量级交易解析器 ===

class TransactionParser:
    def __init__(self, target_wallet: str):
        self.target_wallet = target_wallet
        self.wsol_mint = "So11111111111111111111111111111111111111112"

    def parse_transaction(self, tx: dict) -> Tuple[float, Dict[str, float], int]:
        timestamp = tx.get('timestamp', 0)
        native_sol_change = 0.0
        wsol_change = 0.0
        token_changes = defaultdict(float)

        # 1. Native SOL
        for nt in tx.get('nativeTransfers', []):
            if nt.get('fromUserAccount') == self.target_wallet:
                native_sol_change -= nt.get('amount', 0) / 1e9
            if nt.get('toUserAccount') == self.target_wallet:
                native_sol_change += nt.get('amount', 0) / 1e9

        # 2. Token & WSOL
        for tt in tx.get('tokenTransfers', []):
            mint = tt.get('mint', '')
            amt = tt.get('tokenAmount', 0)
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

        # 3. Merge
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

        if sol_change < 0:  # Buy
            total = sum(buys.values())
            if total > 0:
                cost_per = abs(sol_change) / total
                for m, a in buys.items(): buy_attrs[m] = cost_per * a
        elif sol_change > 0:  # Sell
            total = sum(sells.values())
            if total > 0:
                gain_per = sol_change / total
                for m, a in sells.items(): sell_attrs[m] = gain_per * a
        return buy_attrs, sell_attrs


# === 主逻辑 ===

class SmartMoneySearcher:
    def __init__(self):
        self.api_key = HELIUS_API_KEY
        self.rpc_url = f"https://mainnet.helius-rpc.com/?api-key={self.api_key}"
        self.base_api_url = "https://api.helius.xyz/v0"

        # --- 参数配置 ---
        self.min_delay_sec = 5  # 最小延迟
        self.max_delay_sec = 900  # 最大延迟

        self.audit_tx_limit = 500
        self.target_project_count = 10

    async def _rpc_post(self, client, method, params):
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            resp = await client.post(self.rpc_url, json=payload, timeout=10.0)
            if resp.status_code == 200:
                return resp.json().get("result")
        except Exception as e:
            logger.error(f"RPC {method} failed: {e}")
        return None

    async def get_signatures(self, client, address, limit=100):
        return await self._rpc_post(client, "getSignaturesForAddress", [address, {"limit": limit}])

    async def fetch_parsed_transactions(self, client, signatures):
        if not signatures: return []
        url = f"{self.base_api_url}/transactions?api-key={self.api_key}"
        chunk_size = 90
        all_txs = []
        for i in range(0, len(signatures), chunk_size):
            batch = signatures[i:i + chunk_size]
            payload = {"transactions": [s['signature'] for s in batch]}
            try:
                resp = await client.post(url, json=payload, timeout=30.0)
                if resp.status_code == 200:
                    all_txs.extend(resp.json())
            except Exception as e:
                logger.warning(f"Parse tx batch failed: {e}")
        return all_txs

    async def analyze_hunter_performance(self, client, hunter_address):
        """
        [深度审计]
        返回: win_rate, worst_roi, total_profit
        """
        sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
        if not sigs: return None
        txs = await self.fetch_parsed_transactions(client, sigs)
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
                    if abs(delta) < 1e-9: continue
                    projects[mint]["tokens"] += delta
                    if mint in buy_attrs: projects[mint]["buy_sol"] += buy_attrs[mint]
                    if mint in sell_attrs: projects[mint]["sell_sol"] += sell_attrs[mint]
            except:
                continue

        valid_projects = []
        for mint, data in projects.items():
            if data["buy_sol"] > 0.05:
                net_profit = data["sell_sol"] - data["buy_sol"]
                roi = (net_profit / data["buy_sol"]) * 100
                valid_projects.append({
                    "profit": net_profit,
                    "roi": roi,
                    "cost": data["buy_sol"],
                })

        if not valid_projects: return None

        recent_projects = valid_projects[-15:]

        total_profit = sum(p["profit"] for p in recent_projects)
        wins = [p for p in recent_projects if p["profit"] > 0]
        win_rate = len(wins) / len(recent_projects)

        # 计算 Worst ROI (最大亏损代理)
        worst_roi = min([p["roi"] for p in recent_projects]) if recent_projects else 0
        worst_roi = max(-100, worst_roi)  # Cap at -100%

        return {
            "win_rate": win_rate,
            "worst_roi": worst_roi,
            "total_profit": total_profit,
            "count": len(recent_projects)
        }

    async def search_alpha_hunters(self, token_address):
        hunters_candidates = []

        async with httpx.AsyncClient() as client:
            # 1. 找早期买家
            sigs = await self.get_signatures(client, token_address, limit=100)
            if not sigs: return []
            sigs.sort(key=lambda x: x.get('blockTime', 0))
            if not sigs: return []
            start_time = sigs[0].get('blockTime')
            txs = await self.fetch_parsed_transactions(client, sigs)

            # 2. 初筛
            seen_buyers = set()
            for tx in txs:
                block_time = tx.get('timestamp', 0)
                delay = block_time - start_time

                if delay < self.min_delay_sec: continue
                if delay > self.max_delay_sec: break

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
                    hunters_candidates.append({
                        "address": spender,
                        "entry_delay": delay,
                        "cost": spend_sol
                    })

            logger.info(f"  [初筛] 发现 {len(hunters_candidates)} 个候选人")

            # 3. 深度审计 & 评分
            verified_hunters = []
            for candidate in hunters_candidates:
                addr = candidate["address"]
                stats = await self.analyze_hunter_performance(client, addr)

                if stats:
                    # === 评分计算 (仅作为 Tag，不作为 Filter) ===
                    # 1. Hit Rate Score (30%)
                    score_hit_rate = stats["win_rate"]

                    # 2. Entry Score (40%)
                    delay = candidate["entry_delay"]
                    score_entry = max(0, 1 - (delay / self.max_delay_sec))

                    # 3. Drawdown Score (30%)
                    score_drawdown = 1 - abs(stats["worst_roi"] / 100)
                    score_drawdown = max(0, min(1, score_drawdown))

                    final_score = (score_hit_rate * 30) + (score_entry * 40) + (score_drawdown * 30)
                    final_score = round(final_score, 1)

                    # === 准入逻辑 (回归 V4) ===
                    is_qualified = False

                    # 基础线：总账必须赚钱
                    if stats["total_profit"] > 0.1:
                        # 路径 A: 胜率及格 (稳健)
                        if stats["win_rate"] >= 0.4:
                            is_qualified = True
                        # 路径 B: 暴击大赚 (赔率)
                        elif stats["total_profit"] >= 2.0:
                            is_qualified = True

                    if is_qualified:
                        candidate.update({
                            "score": final_score,  # 存下分数，方便以后排序替换
                            "win_rate": f"{stats['win_rate']:.1%}",
                            "worst_roi": f"{stats['worst_roi']:.1f}%",
                            "total_profit": f"{stats['total_profit']:.2f} SOL",
                            "scores_detail": f"H:{score_hit_rate:.2f}/E:{score_entry:.2f}/D:{score_drawdown:.2f}"
                        })
                        verified_hunters.append(candidate)
                        logger.info(
                            f"    ✅ 锁定猎手 {addr[:6]}.. | 利润: {candidate['total_profit']} | 评分: {final_score}")

                await asyncio.sleep(0.2)

            return verified_hunters

    async def run_pipeline(self, dex_scanner_instance):
        logger.info("启动 Alpha 猎手挖掘 (V5.1 混合版)...")
        hot_tokens = await dex_scanner_instance.scan()
        all_hunters = []

        if hot_tokens:
            for token in hot_tokens:
                addr = token.get('address')
                sym = token.get('symbol')
                logger.info(f"=== 正在挖掘: {sym} ===")
                hunters = await self.search_alpha_hunters(addr)
                if hunters:
                    all_hunters.extend(hunters)
                await asyncio.sleep(1)

        # 最终按分数排序，方便你手动或自动做替换
        all_hunters.sort(key=lambda x: x.get('score', 0), reverse=True)
        return all_hunters


if __name__ == "__main__":
    from services.dexscreener.dex_scanner import DexScanner


    async def main():
        searcher = SmartMoneySearcher()
        mock_scanner = DexScanner()
        results = await searcher.run_pipeline(mock_scanner)

        print(f"\n====== 最终挖掘结果 ({len(results)}) ======")
        for res in results:
            print(res)


    asyncio.run(main())
