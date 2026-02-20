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
              4. 初筛买家: 开盘 15 秒后买入，该代币 ROI 入库门槛 ≥100%（×1/×0.9）；体检时 30d<50% 踢出
              5. 入库硬门槛: 盈亏比≥2、胜率≥20%、代币数≥10、总盈利>0
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
    MIN_TOKEN_AGE_SEC,
    MAX_TOKEN_AGE_SEC,
    MAX_BACKTRACK_PAGES,
    SM_EARLY_TX_PARSE_LIMIT,
    RECENT_TX_COUNT_FOR_FREQUENCY,
    MIN_AVG_TX_INTERVAL_SEC,
    MIN_NATIVE_LAMPORTS_FOR_REAL,
    SCANNED_HISTORY_FILE,
    SM_MIN_DELAY_SEC,
    SM_MAX_DELAY_SEC,
    SM_AUDIT_TX_LIMIT,
    SM_LP_CHECK_TX_LIMIT,
    SM_MIN_BUY_SOL,
    SM_MAX_BUY_SOL,
    SM_MIN_TOKEN_PROFIT_PCT,
    SM_ENTRY_MIN_PNL_RATIO,
    SM_ENTRY_MIN_WIN_RATE,
    SM_ENTRY_MIN_TRADE_COUNT,
    SM_ROI_MULT_200,
    SM_ROI_MULT_100_200,
    SM_ROI_MULT_50_100,
    DEX_MIN_24H_GAIN_PCT,
    WALLET_BLACKLIST_FILE,
    WALLET_BLACKLIST_MIN_SCORE,
    WALLET_BLACKLIST_LOSS_USDC,
    WALLET_BLACKLIST_WIN_RATE,
    USDC_PER_SOL,
)
from services.helius import helius_client
from utils.logger import get_logger
from utils.hunter_scoring import compute_hunter_score

logger = get_logger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# 与 SmartFlow3 一致：只把「真实买卖」算作交易，忽略 SOL/USDC/USDT 等
IGNORE_MINTS = {
    "So11111111111111111111111111111111111111112",  # WSOL
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",  # USDC
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",   # USDT
}
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
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


def _get_tx_timestamp(tx: dict) -> float:
    """
    Helius 解析交易可能返回 timestamp 或 blockTime，统一取 Unix 时间戳（秒）。
    """
    return tx.get("timestamp") or tx.get("blockTime") or 0


def tx_is_remove_liquidity(tx: dict) -> bool:
    """
    判断该笔交易是否为 REMOVE LIQUIDITY（移除流动性）。
    Helius 解析交易在 description 或 type 中标注此类操作。
    老鼠仓：LP 先加流动性，代币拉盘后移除流动性砸盘，散户接盘。
    """
    desc = (tx.get("description") or "").upper()
    tx_type = (tx.get("type") or "").upper()
    # 兼容 Helius 常见格式：description 含 "REMOVE LIQUIDITY" / "Remove Liquidity"
    if "REMOVE" in desc and "LIQUIDITY" in desc:
        return True
    if tx_type in ("REMOVE_LIQUIDITY", "REMOVE LIQUIDITY"):
        return True
    return False


def tx_is_any_lp_behavior(tx: dict) -> bool:
    """
    判断该笔交易是否包含任何 LP 行为（加池/撤池等）。
    只要涉及 ADD LIQUIDITY 或 REMOVE LIQUIDITY 即视为 LP 参与，直接淘汰该猎手。
    """
    desc = (tx.get("description") or "").upper()
    tx_type = (tx.get("type") or "").upper()
    if "LIQUIDITY" in desc:
        return True
    if tx_type in ("ADD_LIQUIDITY", "ADD LIQUIDITY", "REMOVE_LIQUIDITY", "REMOVE LIQUIDITY"):
        return True
    return False


def hunter_had_any_lp_on_token(
    txs: list, hunter_address: str, token_address: str
) -> bool:
    """
    检查该猎手在该代币上是否有任何 LP 行为（加池/撤池）。
    有则视为项目方或老鼠仓，返回 True，直接淘汰并拉黑。
    """
    for tx in (txs or []):
        if not tx_is_any_lp_behavior(tx):
            continue
        for tt in tx.get("tokenTransfers", []):
            if tt.get("mint") != token_address:
                continue
            if tt.get("fromUserAccount") == hunter_address or tt.get("toUserAccount") == hunter_address:
                return True
    return False


def hunter_had_remove_liquidity_on_token(
    txs: list, hunter_address: str, token_address: str
) -> bool:
    """
    检查该猎手在该代币上是否有 REMOVE LIQUIDITY 历史（老鼠仓）。
    若该地址参与过针对该 token 的移除流动性，返回 True。
    """
    for tx in (txs or []):
        if not tx_is_remove_liquidity(tx):
            continue
        # 确认该交易涉及目标代币且猎手参与
        for tt in tx.get("tokenTransfers", []):
            if tt.get("mint") != token_address:
                continue
            if tt.get("fromUserAccount") == hunter_address or tt.get("toUserAccount") == hunter_address:
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
        ts = _get_tx_timestamp(tx)
        if ts > 0:
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

    def parse_transaction(
        self, tx: dict, usdc_price_sol: float | None = None
    ) -> Tuple[float, Dict[str, float], int]:
        """
        解析交易，返回 (sol_change, token_changes, timestamp)。
        sol_change 含 native SOL + WSOL；若传入 usdc_price_sol，USDC 流动亦折算为 SOL 等价并入 sol_change。
        """
        timestamp = int(_get_tx_timestamp(tx))
        native_sol_change = 0.0
        wsol_change = 0.0
        usdc_change = 0.0
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
            elif mint == USDC_MINT:
                if tt.get('fromUserAccount') == self.target_wallet:
                    usdc_change -= amt
                if tt.get('toUserAccount') == self.target_wallet:
                    usdc_change += amt
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

        if usdc_price_sol is not None and usdc_price_sol > 0 and abs(usdc_change) >= 1e-9:
            sol_change += usdc_change * usdc_price_sol

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
        # Helius API 统一通过 services.helius.helius_client 调用
        # 初筛参数 (来自 config/settings.py)
        self.min_delay_sec = SM_MIN_DELAY_SEC
        self.max_delay_sec = SM_MAX_DELAY_SEC
        self.audit_tx_limit = SM_AUDIT_TX_LIMIT

        self.scanned_tokens: Set[str] = set()
        self.wallet_blacklist: Set[str] = set()
        self._load_scanned_history()
        self._load_wallet_blacklist()

    def _ensure_data_dir(self):
        """确保 data 目录存在，使用 BASE_DIR 保证路径一致性。"""
        data_dir = BASE_DIR / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

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
        """追加已扫描代币并后台写入文件，不阻塞挖掘。"""
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
        """加载钱包黑名单：劣质猎手地址，扫描时直接跳过以节省 API。"""
        self._ensure_data_dir()
        if os.path.exists(WALLET_BLACKLIST_FILE):
            try:
                with open(WALLET_BLACKLIST_FILE, 'r') as f:
                    self.wallet_blacklist = set(json.load(f))
                if self.wallet_blacklist:
                    logger.info(f"📂 已加载 {len(self.wallet_blacklist)} 个钱包黑名单")
            except Exception:
                logger.exception("⚠️ 加载钱包黑名单失败")

    def is_blacklisted(self, address: str) -> bool:
        """判断地址是否在黑名单内（供 Monitor 等调用，共振前过滤）。"""
        return address in self.wallet_blacklist

    def _add_to_wallet_blacklist(self, address: str):
        """将劣质猎手加入黑名单并后台写入，不阻塞挖掘。"""
        if address in self.wallet_blacklist:
            return
        self.wallet_blacklist.add(address)
        snapshot = list(self.wallet_blacklist)
        addr_short = address[:12]

        def _write():
            try:
                with open(WALLET_BLACKLIST_FILE, 'w') as f:
                    json.dump(snapshot, f)
                logger.debug("🖤 加入黑名单: %s..", addr_short)
            except Exception:
                logger.exception("保存钱包黑名单失败")

        threading.Thread(target=_write, daemon=True).start()

    async def get_signatures(self, client, address, limit=100, before=None):
        """通过 HeliusClient 获取地址签名列表。"""
        return await helius_client.get_signatures_for_address(
            address, limit=limit, before=before, http_client=client
        )

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
        """通过 HeliusClient 批量拉取解析后的交易。"""
        if not signatures:
            return []
        return await helius_client.fetch_parsed_transactions(signatures, http_client=client)

    def _build_projects_from_txs(
        self, txs: List[dict], exclude_token: str, usdc_price: float, hunter_address: str
    ) -> dict:
        """从交易列表构建 projects {mint: {buy_sol, sell_sol, tokens}}，供统计用。"""
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

    async def analyze_hunter_performance(
        self, client, hunter_address, exclude_token=None, pre_fetched_txs: List[dict] | None = None
    ):
        """
        体检猎手历史表现。若传入 pre_fetched_txs 则复用，避免重复拉取（节省 Helius credit）。
        返回包含 max_roi_30d, max_roi_60d（仅统计窗口内项目）。
        """
        if pre_fetched_txs is not None:
            txs = pre_fetched_txs
            # 复用数据时，频率已在 get_hunter_profit_on_token 中检测过，跳过
        else:
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
        if not txs:
            return None

        usdc_price = await self._get_usdc_price_sol(client) if client else None
        projects = self._build_projects_from_txs(txs, exclude_token, usdc_price, hunter_address)

        valid_projects = []
        for mint, data in projects.items():
            if data["buy_sol"] > 0.05:
                net_profit = data["sell_sol"] - data["buy_sol"]
                roi = (net_profit / data["buy_sol"]) * 100
                valid_projects.append({"profit": net_profit, "roi": roi, "cost": data["buy_sol"]})

        if not valid_projects: return None

        # 使用全部有效项目做评分，不限定 15 个
        total_profit = sum(p["profit"] for p in valid_projects)
        wins = [p for p in valid_projects if p["profit"] > 0]
        win_rate = len(wins) / len(valid_projects)
        avg_roi_pct = sum(p["roi"] for p in valid_projects) / len(valid_projects)
        total_wins = sum(p["profit"] for p in valid_projects if p["profit"] > 0)
        total_losses = sum(abs(p["profit"]) for p in valid_projects if p["profit"] < 0)
        pnl_ratio = total_wins / total_losses if total_losses > 0 else (float("inf") if total_wins > 0 else 0.0)

        # 最近 30/60 天最大收益：按时间过滤 tx 后重建 projects
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
        """从 DexScreener 获取 1 USDC = ? SOL，用于将 USDC 流动折算为 SOL 等价。"""
        return await self._get_token_price_sol(client, USDC_MINT)

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

    async def get_hunter_profit_on_token(
        self, client, hunter_address: str, token_address: str
    ) -> Tuple[float | None, List[dict] | None]:
        """
        计算猎手在该代币上的收益率 (ROI %)，已清仓用卖出收益算，未清仓用现价估算。
        返回 (ROI 百分比, 交易列表)，若无法计算返回 (None, None)。
        交易列表供后续 analyze_hunter_performance 复用，减少 Helius API 消耗。
        阶段顺序：先 100 笔 LP 预检 -> 120 笔频率检测 -> 拉满 500 笔。
        """
        sigs = await self.get_signatures(client, hunter_address, limit=self.audit_tx_limit)
        if not sigs:
            return None, None

        # 阶段 0+1：先拉 100 笔做 LP 预检 + 频率检测，复用以节省 API
        first_batch = sigs[:SM_LP_CHECK_TX_LIMIT]
        first_txs = await self.fetch_parsed_transactions(client, first_batch)
        if not first_txs:
            return None, None
        # LP 预检：有任何 LP 行为（加池/撤池）直接淘汰并拉黑
        if hunter_had_any_lp_on_token(first_txs, hunter_address, token_address):
            logger.warning(
                "⚠️ LP 行为淘汰: %s.. 曾对该代币有 LP 操作（加池/撤池），已加入黑名单，永不跟仓",
                hunter_address[:12],
            )
            self._add_to_wallet_blacklist(hunter_address)
            return None, None
        # 频率检测：频繁则直接淘汰
        if _is_frequent_trader_by_real_txs(first_txs, hunter_address):
            logger.debug("⏭️ 频率淘汰 %s.. (先拉 %d 笔即判定频繁)", hunter_address[:12], len(first_batch))
            return None, None

        # 阶段 2：LP 和频率均通过，拉满 500 笔算 ROI 并供评分复用
        if len(sigs) <= SM_LP_CHECK_TX_LIMIT:
            txs = first_txs
        else:
            rest_sigs = sigs[SM_LP_CHECK_TX_LIMIT:]
            rest_txs = await self.fetch_parsed_transactions(client, rest_sigs)
            txs = (first_txs or []) + (rest_txs or [])
        if not txs:
            return None, None
        usdc_price = await self._get_usdc_price_sol(client)
        parser = TransactionParser(hunter_address)
        calc = TokenAttributionCalculator()
        buy_sol, sell_sol, tokens_held = 0.0, 0.0, 0.0
        txs.sort(key=lambda x: _get_tx_timestamp(x))
        for tx in txs:
            try:
                sol_change, token_changes, _ = parser.parse_transaction(tx, usdc_price_sol=usdc_price)
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
            return None, None
        total_value = sell_sol
        if tokens_held > 1e-9:
            price = await self._get_token_price_sol(client, token_address)
            if price is not None and price > 0:
                total_value += tokens_held * price
        roi = (total_value - buy_sol) / buy_sol * 100
        return roi, txs

    async def verify_token_age_via_dexscreener(self, client, token_address):
        """
        返回: (is_valid_window, start_time, reason, gain_24h, should_save_scanned)
        should_save_scanned: 是否应写入 scanned_tokens（年龄超龄必写；年龄范围内但涨幅未达标不写，便于后续重试）
        """
        url = f"https://api.dexscreener.com/latest/dex/tokens/{token_address}"
        try:
            resp = await client.get(url, timeout=5.0)
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
                # priceChange.h24 可能为倍数 (6=6x=500%)，若在 [1,20] 视为倍数并换算
                if 1 < gain_24h <= 20:
                    gain_24h = (gain_24h - 1) * 100

                # 使用主交易对（流动性最高）的创建时间，而非 min(全部)
                # 原因：Pump.fun bonding curve 的 pair 创建最早，迁移到 Pumpswap 后才有主 DEX；
                # 主 DEX 的 pairCreatedAt 才代表代币真正上线时间。
                created_at_ms = main_pair.get('pairCreatedAt', float('inf'))
                if created_at_ms == float('inf'):
                    return False, 0, "No Creation Time", gain_24h, False

                created_at_sec = created_at_ms / 1000
                age = time.time() - created_at_sec

                if age < MIN_TOKEN_AGE_SEC:
                    return False, created_at_sec, f"Too Young ({age / 60:.1f}m)", gain_24h, False
                if age > MAX_TOKEN_AGE_SEC:
                    return False, created_at_sec, f"Too Old ({age / 3600:.1f}h)", gain_24h, True

                # 年龄在范围内：涨幅未达标时不写 scanned，便于下次发现周期重试
                if gain_24h < DEX_MIN_24H_GAIN_PCT:
                    return False, created_at_sec, f"GainNotYet ({gain_24h:.0f}% < {DEX_MIN_24H_GAIN_PCT}%)", gain_24h, False

                return True, created_at_sec, "OK", gain_24h, False
            else:
                return False, 0, "API Error", 0.0, False
        except Exception:
            logger.exception("verify_token_age_via_dexscreener 请求异常")
            return False, 0, "Exception", 0.0, False

    async def search_alpha_hunters(self, token_address):
        if token_address in self.scanned_tokens: return []

        async with httpx.AsyncClient() as client:
            # 1. 年龄 + 涨幅检查：年龄超龄写 scanned，年龄范围内涨幅未达标不写（便于下次重试）
            is_valid, start_time, reason, gain_24h, should_save = await self.verify_token_age_via_dexscreener(client, token_address)
            if not is_valid:
                if "GainNotYet" in reason:
                    logger.info(f"📉 涨幅未达标，跳过挖掘: {reason} (不写 scanned，下次重试)")
                else:
                    logger.info(f"⏭️ 跳过代币 {token_address}: {reason}")
                if should_save:
                    self._save_scanned_token(token_address)
                return []

            logger.info(f"🔍 涨幅达标 ({gain_24h:.0f}%≥{DEX_MIN_24H_GAIN_PCT}%) | 年龄 {time.time() - start_time:.0f}s，开始回溯...")

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

            # 3. 解析交易（含 native SOL + WSOL 买入）
            found_early_txs.sort(key=lambda x: x.get('blockTime', 0))
            target_txs = found_early_txs[:SM_EARLY_TX_PARSE_LIMIT]
            txs = await self.fetch_parsed_transactions(client, target_txs)

            hunters_candidates = []
            seen_buyers = set()
            usdc_price = await self._get_usdc_price_sol(client)

            wsol_mint = "So11111111111111111111111111111111111111112"
            for tx in txs:
                block_time = _get_tx_timestamp(tx)
                delay = block_time - start_time
                if delay < self.min_delay_sec: continue

                # 合并 native SOL + WSOL 转出，找出本笔交易中「付出最多 SOL」的地址（即买家）
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
                    if mint == wsol_mint:
                        spend_by_addr[addr] += amt
                    elif mint == USDC_MINT and usdc_price and usdc_price > 0:
                        spend_by_addr[addr] += amt * usdc_price

                if not spend_by_addr:
                    continue
                spender = max(spend_by_addr, key=spend_by_addr.get)
                spend_sol = spend_by_addr[spender]
                if spender in seen_buyers:
                    continue
                if SM_MIN_BUY_SOL <= spend_sol <= SM_MAX_BUY_SOL:
                    seen_buyers.add(spender)
                    hunters_candidates.append({"address": spender, "entry_delay": delay, "cost": spend_sol})

            logger.info(f"  [初筛] 15秒后买入且金额合规: {len(hunters_candidates)} 个")

            # 3.5 + 4 收益过滤 + 评分（生产者-消费者：拉取一次交易，复用于 ROI 与体检，节省 Helius credit）
            verified_hunters = []
            pnl_passed_count = 0  # 通过 ROI≥100% 的猎手数
            total = len(hunters_candidates)
            progress_interval = max(1, total // 10)  # 每 ~10% 打一次进度
            for idx, candidate in enumerate(hunters_candidates, 1):
                if idx == 1 or idx % progress_interval == 0 or idx == total:
                    pct = idx * 100 // total
                    logger.info(f"  [进度] {idx}/{total} ({pct}%) | 符合PnL {pnl_passed_count} 个 | 已入库 {len(verified_hunters)} 个")
                addr = candidate["address"]
                if addr in self.wallet_blacklist:
                    logger.debug("    跳过黑名单: %s..", addr[:12])
                    continue
                roi, txs = await self.get_hunter_profit_on_token(client, addr, token_address)
                # LP 行为（加池/撤池）已在 get_hunter_profit_on_token 前 100 笔预检中淘汰并拉黑
                if roi is None or roi < SM_MIN_TOKEN_PROFIT_PCT:
                    await asyncio.sleep(0.3)
                    continue
                pnl_passed_count += 1
                # 入库时用该代币 ROI 乘数：≥200%×1，100%~200%×0.9（入库门槛 100%+，故无 50~100%）
                if roi >= 200:
                    roi_mult = SM_ROI_MULT_200
                else:
                    roi_mult = SM_ROI_MULT_100_200
                logger.debug(f"    通过收益过滤: {addr[:12]}.. ROI={roi:.0f}%% ×{roi_mult}")

                # 复用已拉取的 txs，不再重复请求 Helius
                stats = await self.analyze_hunter_performance(
                    client, addr, exclude_token=token_address, pre_fetched_txs=txs
                )

                if stats:
                    score_result = compute_hunter_score(stats)
                    base_score = score_result["score"]
                    final_score = round(base_score * roi_mult, 1)

                    # 新入库硬门槛：pnl_ratio>=2, wr>=20%, count>=10, profit>0
                    trade_count = stats.get("count", 0)
                    pnl_ok = stats.get("pnl_ratio", 0) >= SM_ENTRY_MIN_PNL_RATIO
                    wr_ok = stats["win_rate"] >= SM_ENTRY_MIN_WIN_RATE
                    count_ok = trade_count >= SM_ENTRY_MIN_TRADE_COUNT
                    profit_ok = stats["total_profit"] > 0
                    is_qualified = pnl_ok and wr_ok and count_ok and profit_ok

                    # 劣质猎手加入黑名单
                    loss_usdc = -stats["total_profit"] * USDC_PER_SOL if stats["total_profit"] < 0 else 0
                    if (base_score < WALLET_BLACKLIST_MIN_SCORE or
                            (loss_usdc >= WALLET_BLACKLIST_LOSS_USDC and stats["win_rate"] < WALLET_BLACKLIST_WIN_RATE)):
                        self._add_to_wallet_blacklist(addr)

                    if is_qualified:
                        avg_roi = stats.get("avg_roi_pct", 0.0)
                        # 入库时该代币 ROI 作为 max_roi_30d 初始值
                        max_roi_30d = max(roi, stats.get("max_roi_30d", 0))
                        candidate.update({
                            "score": final_score,
                            "win_rate": f"{stats['win_rate']:.1%}",
                            "total_profit": f"{stats['total_profit']:.2f} SOL",
                            "avg_roi_pct": f"{avg_roi:.1f}%",
                            "scores_detail": score_result["scores_detail"],
                            "max_roi_30d": max_roi_30d,
                        })
                        verified_hunters.append(candidate)
                        logger.info(
                            f"    ✅ 锁定猎手 {addr}.. | 利润: {candidate['total_profit']} | 评分: {final_score} (×{roi_mult})")
                    else:
                        NEAR_THRESHOLD = 0.8
                        reasons = []
                        if not pnl_ok and stats.get("pnl_ratio", 0) >= SM_ENTRY_MIN_PNL_RATIO * NEAR_THRESHOLD:
                            reasons.append(f"盈亏比{stats.get('pnl_ratio', 0):.2f}<{SM_ENTRY_MIN_PNL_RATIO}")
                        if not wr_ok and stats["win_rate"] >= SM_ENTRY_MIN_WIN_RATE * NEAR_THRESHOLD:
                            reasons.append(f"胜率{stats['win_rate']*100:.1f}%<{SM_ENTRY_MIN_WIN_RATE*100:.0f}%")
                        if not count_ok and trade_count >= SM_ENTRY_MIN_TRADE_COUNT * NEAR_THRESHOLD:
                            reasons.append(f"交易笔数{trade_count}<{SM_ENTRY_MIN_TRADE_COUNT}")
                        if not profit_ok and stats["total_profit"] > -0.5:
                            reasons.append("总盈利非正")
                        if reasons:
                            logger.info("[落榜钱包地址] %s | 原因: %s", addr, " | ".join(reasons))
                await asyncio.sleep(0.5)

            logger.info(f"  [收益+评分] 初筛 {total} → ROI≥{SM_MIN_TOKEN_PROFIT_PCT:.0f}%: {pnl_passed_count} 个 → 入库 {len(verified_hunters)} 个")
            self._save_scanned_token(token_address)
            return verified_hunters

    async def run_pipeline(self, dex_scanner_instance):
        logger.info(f"启动 Alpha 猎手挖掘 (流动性+成交量筛选 | 年龄区间内且涨幅>{DEX_MIN_24H_GAIN_PCT}%才挖 | 未达标不写scanned便于重试)")
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
                logger.info(f"=== 正在挖掘: {sym} ===")
                logger.info(f"    地址: {addr}")
                try:
                    hunters = await self.search_alpha_hunters(addr)
                    if hunters: all_hunters.extend(hunters)
                except Exception:
                    logger.exception("❌ 挖掘代币 %s 出错", sym)
                await asyncio.sleep(1)
        all_hunters.sort(key=lambda x: float(x.get('score', 0) or 0), reverse=True)
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
