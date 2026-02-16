#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@Author  : Zijun Deng
@Date    : 2/16/2026
@File    : sm_searcher.py
@Description: Smart Money Searcher V4 - 严选版
              1. 时间窗口放宽：5s - 15m
              2. 评分逻辑优化：引入总盈亏(PnL)判定，防止误杀低胜率大神
              3. 持仓处理：对小额未卖出订单更宽容
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

        # --- 宽松后的初筛参数 ---
        self.min_delay_sec = 5  # 从30s降到5s，放过手快的人类
        self.max_delay_sec = 900  # 15分钟

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
        [深度审计 - 宽松版]
        逻辑调整：
        1. 计算 Total Profit (总盈亏)
        2. 计算 Profit Factor (总赚 / 总亏)
        3. 只要 总赚 > 总亏，哪怕胜率低也通过
        """
        sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
        if not sigs: return None
        txs = await self.fetch_parsed_transactions(client, sigs)
        if not txs: return None

        parser = TransactionParser(hunter_address)
        calc = TokenAttributionCalculator()
        projects = defaultdict(lambda: {"buy_sol": 0.0, "sell_sol": 0.0, "tokens": 0.0})

        # 内存重建账本
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

        # 统计战绩
        valid_projects = []
        for mint, data in projects.items():
            # 过滤超小额 (少于 0.05 SOL 的可能只是测试或粉尘)
            if data["buy_sol"] > 0.05:
                # 宽松逻辑：如果卖出为0，且买入成本也很低(< 0.1)，可能是彩票，不计入亏损
                # 但如果买了很多没卖，就是亏损

                net_profit = data["sell_sol"] - data["buy_sol"]
                roi = (net_profit / data["buy_sol"]) * 100

                # 状态标记
                is_settled = data["sell_sol"] > 0.001

                valid_projects.append({
                    "profit": net_profit,
                    "roi": roi,
                    "cost": data["buy_sol"],
                    "revenue": data["sell_sol"],
                    "is_settled": is_settled
                })

        if not valid_projects: return None

        # 取最近 10-15 个项目
        recent_projects = valid_projects[-15:]  # 列表已经是按时间构建的，取最后即最近

        total_revenue = sum(p["revenue"] for p in recent_projects)
        total_cost = sum(p["cost"] for p in recent_projects)
        total_net_profit = total_revenue - total_cost

        # 胜率计算 (只算已结清的或者是大亏的)
        # 宽松定义：赚了就是赢
        wins = [p for p in recent_projects if p["profit"] > 0]
        win_rate = len(wins) / len(recent_projects)

        avg_roi = sum(p["roi"] for p in recent_projects) / len(recent_projects)

        return {
            "win_rate": win_rate,
            "avg_roi": avg_roi,
            "total_profit": total_net_profit,
            "total_cost": total_cost,
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

                if delay < self.min_delay_sec: continue  # 过滤 <5s 的顶级Bot
                if delay > self.max_delay_sec: break

                # 简单识别买家
                spender = None
                max_spend = 0
                for nt in tx.get('nativeTransfers', []):
                    amt = nt.get('amount', 0)
                    if amt > max_spend:
                        max_spend = amt
                        spender = nt.get('fromUserAccount')

                if not spender or spender in seen_buyers: continue
                spend_sol = max_spend / 1e9

                # 范围放宽：0.1 SOL - 50 SOL
                if 0.1 <= spend_sol <= 50.0:
                    seen_buyers.add(spender)
                    hunters_candidates.append({
                        "address": spender,
                        "entry_delay": delay,
                        "cost": spend_sol
                    })

            logger.info(f"  [初筛] 发现 {len(hunters_candidates)} 个候选人 (Delay > {self.min_delay_sec}s)")

            # 3. 深度审计
            verified_hunters = []
            for candidate in hunters_candidates:
                addr = candidate["address"]
                stats = await self.analyze_hunter_performance(client, addr)

                if stats:
                    # === 核心筛选逻辑 (V4 严选版) ===
                    is_qualified = False

                    # 硬指标：总账必须是赚钱的！(排除掉那些赢小输大的)
                    if stats["total_profit"] > 0.1:  # 至少赚了 0.1 SOL (排除粉尘干扰)

                        # 在赚钱的前提下，再看风格：

                        # 风格 A: 稳健型 (胜率尚可，且没有大亏过)
                        if stats["win_rate"] >= 0.4:
                            is_qualified = True

                        # 风格 B: 暴击型 (胜率低没关系，只要总利润够高，说明抓住了大金狗)
                        # 例如：胜率 20%，但总赚 5 SOL，说明盈亏比极高
                        elif stats["total_profit"] >= 2.0:
                            is_qualified = True

                    # 只有合格的才录入
                    if is_qualified:
                        score = stats["total_profit"] * 20 + stats["win_rate"] * 50
                        candidate.update({
                            "score": round(score, 1),
                            "win_rate": f"{stats['win_rate']:.1%}",
                            "avg_roi": f"{stats['avg_roi']:.1f}%",
                            "total_profit": f"{stats['total_profit']:.2f} SOL",
                            "history_count": stats["count"]
                        })
                        verified_hunters.append(candidate)
                        logger.info(
                            f"    ✅ 锁定猎手 {addr[:6]}.. | 利润: {candidate['total_profit']} | 胜率: {candidate['win_rate']}")

                await asyncio.sleep(0.2)  # 稍微快一点

            return verified_hunters

    async def run_pipeline(self, dex_scanner_instance):
        logger.info("启动 Alpha 猎手挖掘 (V4 严选版)...")
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
