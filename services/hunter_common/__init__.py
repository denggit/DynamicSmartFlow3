#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 猎手挖掘共用模块（MODELA 与 MODELB 共享）
              - 交易解析：TransactionParser、TokenAttributionCalculator
              - LP 检测：hunter_had_any_lp_anywhere、tx_is_any_lp_behavior 等
              - 高频检测：_is_frequent_trader_by_blocktimes
"""

from services.hunter_common.shared import (
    tx_has_real_trade,
    _get_tx_timestamp,
    _normalize_token_amount,
    tx_is_remove_liquidity,
    tx_is_any_lp_behavior,
    hunter_had_any_lp_anywhere,
    hunter_had_any_lp_on_token,
    hunter_had_remove_liquidity_on_token,
    collect_lp_participants_from_txs,
    _is_frequent_trader_by_blocktimes,
    TransactionParser,
    TokenAttributionCalculator,
)

__all__ = [
    "tx_has_real_trade",
    "_get_tx_timestamp",
    "_normalize_token_amount",
    "tx_is_remove_liquidity",
    "tx_is_any_lp_behavior",
    "hunter_had_any_lp_anywhere",
    "hunter_had_any_lp_on_token",
    "hunter_had_remove_liquidity_on_token",
    "collect_lp_participants_from_txs",
    "_is_frequent_trader_by_blocktimes",
    "TransactionParser",
    "TokenAttributionCalculator",
]
