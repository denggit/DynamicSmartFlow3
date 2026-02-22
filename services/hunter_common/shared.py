#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 猎手挖掘共用逻辑（MODELA / MODELB 复用）
              交易解析、LP 行为检测、高频交易检测。
"""

from collections import defaultdict
from typing import Dict, Tuple

from config.settings import (
    IGNORE_MINTS,
    USDC_MINT,
    WSOL_MINT,
    RECENT_TX_COUNT_FOR_FREQUENCY,
    MIN_SUCCESSFUL_TX_FOR_FREQUENCY,
    MAX_FAILURE_RATE_FOR_FREQUENCY,
    MIN_AVG_TX_INTERVAL_SEC,
)

# 至少 0.01 SOL 的 native 转账才算「真实」
MIN_NATIVE_LAMPORTS_FOR_REAL = int(0.01 * 1e9)


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


def _get_tx_timestamp(tx: dict) -> float:
    """
    Helius 解析交易可能返回 timestamp 或 blockTime，统一取 Unix 时间戳（秒）。
    """
    return tx.get("timestamp") or tx.get("blockTime") or 0


def tx_is_remove_liquidity(tx: dict) -> bool:
    """
    判断该笔交易是否为移除流动性（REMOVE/WITHDRAW LIQUIDITY）。
    Helius 官方用 WITHDRAW_LIQUIDITY，部分 DEX 可能用 REMOVE 表述。
    老鼠仓：LP 先加流动性，代币拉盘后移除流动性砸盘，散户接盘。
    """
    desc = (tx.get("description") or "").upper()
    tx_type = (tx.get("type") or "").upper()
    if "REMOVE" in desc and "LIQUIDITY" in desc:
        return True
    if "WITHDRAW" in desc and "LIQUIDITY" in desc:
        return True
    if tx_type in (
        "REMOVE_LIQUIDITY", "REMOVE LIQUIDITY",
        "WITHDRAW_LIQUIDITY", "WITHDRAW LIQUIDITY",
        "REMOVE_FROM_POOL",
    ):
        return True
    return False


def tx_is_any_lp_behavior(tx: dict) -> bool:
    """
    判断该笔交易是否包含任何 LP 行为（加池/撤池等）。
    只要涉及 ADD/REMOVE/WITHDRAW LIQUIDITY 或 POOL 操作即视为 LP 参与。
    """
    desc = (tx.get("description") or "").upper()
    tx_type = (tx.get("type") or "").upper()
    if "LIQUIDITY" in desc or ("POOL" in desc and ("ADD" in desc or "REMOVE" in desc or "WITHDRAW" in desc or "DEPOSIT" in desc)):
        return True
    if tx_type in (
        "ADD_LIQUIDITY", "ADD LIQUIDITY",
        "REMOVE_LIQUIDITY", "REMOVE LIQUIDITY",
        "WITHDRAW_LIQUIDITY", "WITHDRAW LIQUIDITY",
        "ADD_TO_POOL", "REMOVE_FROM_POOL",
    ):
        return True
    if tx_type in ("DEPOSIT", "WITHDRAW") and "POOL" in desc:
        return True
    return False


def hunter_had_any_lp_anywhere(txs: list) -> bool:
    """
    检查交易列表中是否存在任何 LP 行为（加池/撤池），不限定代币。
    做过 LP 的钱包视为做市商/项目方，一律拉黑。
    """
    for tx in (txs or []):
        if tx_is_any_lp_behavior(tx):
            return True
    return False


def hunter_had_any_lp_on_token(
    txs: list, hunter_address: str, token_address: str, ata_address: str | None = None
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
            from_a, to_a = tt.get("fromUserAccount"), tt.get("toUserAccount")
            if from_a == hunter_address or to_a == hunter_address:
                return True
            if ata_address and (from_a == ata_address or to_a == ata_address):
                return True
    return False


def hunter_had_remove_liquidity_on_token(
    txs: list, hunter_address: str, token_address: str
) -> bool:
    """
    检查该猎手在该代币上是否有 REMOVE LIQUIDITY 历史（老鼠仓）。
    """
    for tx in (txs or []):
        if not tx_is_remove_liquidity(tx):
            continue
        for tt in tx.get("tokenTransfers", []):
            if tt.get("mint") != token_address:
                continue
            if tt.get("fromUserAccount") == hunter_address or tt.get("toUserAccount") == hunter_address:
                return True
    return False


def collect_lp_participants_from_txs(txs: list) -> set:
    """
    从交易列表中收集所有 LP 行为（加池/撤池）的参与者地址。
    返回参与过 LP 操作的地址集合（主钱包，非 ATA）。
    """
    participants = set()
    for tx in (txs or []):
        if not tx_is_any_lp_behavior(tx):
            continue
        for nt in tx.get("nativeTransfers", []):
            addr = nt.get("fromUserAccount") or nt.get("toUserAccount")
            if addr:
                participants.add(addr)
        for tt in tx.get("tokenTransfers", []):
            addr = tt.get("fromUserAccount") or tt.get("toUserAccount")
            if addr:
                participants.add(addr)
    return participants


def _is_frequent_trader_by_blocktimes(
    sigs: list,
    sample_count: int = RECENT_TX_COUNT_FOR_FREQUENCY,
    max_interval_sec: float = MIN_AVG_TX_INTERVAL_SEC,
    min_successful: int = MIN_SUCCESSFUL_TX_FOR_FREQUENCY,
    max_failure_rate: float = MAX_FAILURE_RATE_FOR_FREQUENCY,
) -> bool:
    """
    免费预检：用 Signature 列表里的 blockTime + err 判断是否应否决该地址。
    - 高失败率（>= 30%）：Spam Bot 反向指标，直接否决。
    - 成功交易数 < 10：死号/新号/矩阵号，无稳定盈利历史，直接否决。
    - 若成功交易平均间隔 < 5 分钟，视为高频机器人。
    """
    sample = [s for s in (sigs or [])[:sample_count] if isinstance(s, dict)]
    if len(sample) < 2:
        return False
    failed = sum(1 for s in sample if s.get("err") is not None)
    failure_rate = failed / len(sample)
    if failure_rate >= max_failure_rate:
        return True

    successful = [s for s in sample if s.get("err") is None]
    if len(successful) < min_successful:
        return True
    times = [int(s["blockTime"]) for s in successful if s.get("blockTime") and s["blockTime"] > 0]
    if len(times) < 2:
        return True
    times.sort()
    span = times[-1] - times[0]
    if span <= 0:
        return True
    avg_interval = span / (len(times) - 1)
    return avg_interval < max_interval_sec


def _normalize_token_amount(raw) -> float:
    """将 Helius tokenAmount 转为浮点数。支持数字或对象 { amount: string, decimals: int }。"""
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
    """
    交易解析器：从 Helius 解析的交易中提取 sol 变动、代币变动、时间戳。
    MODELA 与 MODELB 共用。
    """

    def __init__(self, target_wallet: str):
        self.target_wallet = target_wallet
        self.wsol_mint = WSOL_MINT

    def parse_transaction(
        self, tx: dict, usdc_price_sol: float | None = None
    ) -> Tuple[float, Dict[str, float], int]:
        """
        解析交易，返回 (sol_change, token_changes, timestamp)。
        sol_change 含 native SOL + WSOL；若传入 usdc_price_sol，USDC 流动亦折算为 SOL 等价。
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
    """
    代币成本/收益归属计算器：根据 sol 变动与 token 变动，分配买入成本或卖出收益到各 mint。
    """

    @staticmethod
    def calculate_attribution(sol_change: float, token_changes: Dict[str, float]):
        buy_attrs, sell_attrs = {}, {}
        if abs(sol_change) < 1e-9:
            return buy_attrs, sell_attrs
        buys = {m: a for m, a in token_changes.items() if a > 0}
        sells = {m: abs(a) for m, a in token_changes.items() if a < 0}

        if sol_change < 0:
            total = sum(buys.values())
            if total > 0:
                cost_per = abs(sol_change) / total
                for m, a in buys.items():
                    buy_attrs[m] = cost_per * a
        elif sol_change > 0:
            total = sum(sells.values())
            if total > 0:
                gain_per = sol_change / total
                for m, a in sells.items():
                    sell_attrs[m] = gain_per * a
        return buy_attrs, sell_attrs
