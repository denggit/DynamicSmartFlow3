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
    FREQUENCY_CHECK_SIG_LIMIT,
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


# 买卖活动判定：至少 0.0001 SOL 变动才算真实 swap，排除纯 transfer
MIN_SOL_CHANGE_FOR_SWAP = 0.0001
LAMPORTS_PER_SOL = 1e9


def _parse_rpc_tx_balance_changes(tx_rpc: dict, hunter_address: str) -> Tuple[float, Dict[str, float], int]:
    """
    从 Alchemy/RPC 标准格式（getTransaction 返回）解析 SOL 与 Token 变动。
    不依赖 Helius，用于频率检测等场景。
    :return: (sol_change_sol, {mint: delta_ui}, block_time)
    """
    sol_change = 0.0
    token_changes = {}
    block_time = int(tx_rpc.get("blockTime") or 0)
    meta = tx_rpc.get("meta") or {}
    if not meta:
        return sol_change, token_changes, block_time

    # 1. Account keys：版本化交易可能在 message 不同结构
    tx_body = tx_rpc.get("transaction") or {}
    msg = tx_body.get("message") or tx_body
    account_keys_raw = msg.get("accountKeys") or []
    account_keys = []
    for k in account_keys_raw:
        if isinstance(k, dict):
            pk = k.get("pubkey")
            if pk:
                account_keys.append(pk)
        elif isinstance(k, str):
            account_keys.append(k)

    # 2. 本地址的 native SOL 变动（preBalances/postBalances 与 account_keys 对齐）
    pre_bal = meta.get("preBalances") or []
    post_bal = meta.get("postBalances") or []
    for i, pk in enumerate(account_keys):
        if pk != hunter_address:
            continue
        pre = int(pre_bal[i]) if i < len(pre_bal) else 0
        post = int(post_bal[i]) if i < len(post_bal) else 0
        sol_change += (post - pre) / LAMPORTS_PER_SOL

    # 3. Token 变动（preTokenBalances/postTokenBalances 有 owner 字段）
    pre_tok = {}
    post_tok = {}
    dec_map = {}
    for bal in meta.get("preTokenBalances") or []:
        if bal.get("owner") != hunter_address:
            continue
        mint = bal.get("mint", "")
        uita = bal.get("uiTokenAmount") or {}
        raw = float(uita.get("amount", 0) or 0)
        dec = int(uita.get("decimals", 6) or 6)
        pre_tok[mint] = raw
        dec_map[mint] = dec
    for bal in meta.get("postTokenBalances") or []:
        if bal.get("owner") != hunter_address:
            continue
        mint = bal.get("mint", "")
        uita = bal.get("uiTokenAmount") or {}
        raw = float(uita.get("amount", 0) or 0)
        dec = int(uita.get("decimals", 6) or 6)
        post_tok[mint] = raw
        dec_map[mint] = dec

    for mint in set(pre_tok.keys()) | set(post_tok.keys()):
        pre = pre_tok.get(mint, 0)
        post = post_tok.get(mint, 0)
        dec = dec_map.get(mint, 6)
        delta_raw = post - pre
        if abs(delta_raw) >= 1:
            token_changes[mint] = delta_raw / (10**dec) if dec else delta_raw

    return sol_change, token_changes, block_time


def _tx_is_buy_sell_activity_rpc(tx_rpc: dict, hunter_address: str) -> bool:
    """
    从 RPC 格式判断是否为买卖活动（swap），排除纯 transfer。
    不依赖 Helius，用于 Alchemy 拉取后的频率检测。
    """
    try:
        sol_change, token_changes, _ = _parse_rpc_tx_balance_changes(tx_rpc, hunter_address)
        has_non_ignore_token = any(m not in IGNORE_MINTS and abs(a) >= 1e-9 for m, a in (token_changes or {}).items())
        has_sol_move = abs(sol_change or 0) >= MIN_SOL_CHANGE_FOR_SWAP
        return bool(has_non_ignore_token and has_sol_move)
    except Exception:
        return False


def _tx_is_buy_sell_activity(tx: dict, hunter_address: str, parser: "TransactionParser", usdc_price_sol: float) -> bool:
    """
    判断该笔交易是否为买卖活动（swap），排除纯 transfer。
    条件：有非 IGNORE 代币的 token 变动，且 SOL 有显著变动（买入花 SOL / 卖出收 SOL）。
    """
    try:
        sol_change, token_changes, _ = parser.parse_transaction(tx, usdc_price_sol=usdc_price_sol)
        has_non_ignore_token = any(m not in IGNORE_MINTS and abs(a) >= 1e-9 for m, a in (token_changes or {}).items())
        has_sol_move = abs(sol_change or 0) >= MIN_SOL_CHANGE_FOR_SWAP
        return bool(has_non_ignore_token and has_sol_move)
    except Exception:
        return False


def _is_frequent_trader_by_buy_sell_activities_rpc(
    txs_rpc: list,
    hunter_address: str,
    max_interval_sec: float = MIN_AVG_TX_INTERVAL_SEC,
    min_successful: int = MIN_SUCCESSFUL_TX_FOR_FREQUENCY,
) -> bool:
    """
    基于买卖活动判定高频（RPC 格式，不依赖 Helius）。
    只统计 buy/sell 交易，排除 transfer。
    :param txs_rpc: Alchemy getTransaction 返回的 RPC 格式交易列表
    """
    if not txs_rpc or len(txs_rpc) < 2:
        return False
    buy_sell_times = []
    for tx in txs_rpc:
        if not _tx_is_buy_sell_activity_rpc(tx, hunter_address):
            continue
        _, _, block_time = _parse_rpc_tx_balance_changes(tx, hunter_address)
        if block_time and block_time > 0:
            buy_sell_times.append(int(block_time))
    if len(buy_sell_times) < min_successful:
        return False
    if len(buy_sell_times) < 2:
        return False
    buy_sell_times.sort()
    span = buy_sell_times[-1] - buy_sell_times[0]
    if span <= 0:
        return False
    avg_interval = span / (len(buy_sell_times) - 1)
    return avg_interval < max_interval_sec


def _is_frequent_trader_by_buy_sell_activities(
    txs: list,
    hunter_address: str,
    usdc_price_sol: float = 0.01,
    max_interval_sec: float = MIN_AVG_TX_INTERVAL_SEC,
    min_successful: int = MIN_SUCCESSFUL_TX_FOR_FREQUENCY,
) -> bool:
    """
    基于买卖活动判定高频：只统计 buy/sell 交易，排除 transfer。
    拉取 300 条解析后过滤出买卖，若买卖平均间隔 < 5 分钟视为高频。
    :param txs: Helius 解析后的交易列表（或 RPC 格式，自动检测）
    :param hunter_address: 猎手地址（用于 TransactionParser）
    :param usdc_price_sol: USDC 折算 SOL 价（仅 Helius 格式需要）
    """
    if not txs or len(txs) < 2:
        return False
    # 若为首条含 RPC 特征（meta.preTokenBalances），用 RPC 解析，不依赖 Helius
    sample = txs[0] if txs else {}
    meta = sample.get("meta") if isinstance(sample, dict) else None
    if isinstance(sample, dict) and meta is not None and ("preTokenBalances" in meta or "preBalances" in meta):
        return _is_frequent_trader_by_buy_sell_activities_rpc(
            txs, hunter_address, max_interval_sec, min_successful
        )
    parser = TransactionParser(hunter_address)
    buy_sell_times = []
    for tx in txs:
        if not _tx_is_buy_sell_activity(tx, hunter_address, parser, usdc_price_sol):
            continue
        ts = _get_tx_timestamp(tx)
        if ts and ts > 0:
            buy_sell_times.append(int(ts))
    if len(buy_sell_times) < min_successful:
        return False  # 买卖笔数不足，无法判定，视为非高频
    if len(buy_sell_times) < 2:
        return False
    buy_sell_times.sort()
    span = buy_sell_times[-1] - buy_sell_times[0]
    if span <= 0:
        return False
    avg_interval = span / (len(buy_sell_times) - 1)
    return avg_interval < max_interval_sec


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
    注：此方法统计所有交易（含 transfer），精确判定请用 _is_frequent_trader_by_buy_sell_activities。
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
