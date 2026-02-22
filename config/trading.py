#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 交易参数、跟单分档、止盈止损
"""
import os

# Jupiter API
JUP_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUP_SWAP_API = "https://api.jup.ag/swap/v1/swap"

SLIPPAGE_BPS = 200
SELL_SLIPPAGE_BPS_RETRIES = [200, 500, 1000]
PRIORITY_FEE_SETTINGS = "auto"


def get_tier_config(score: float) -> dict:
    """根据猎手分数返回 entry/add/max/stop_loss。"""
    if score >= 90:
        return {"entry_sol": 0.05, "add_sol": 0.05, "max_sol": 0.15, "stop_loss_pct": 0.85}
    if score >= 80:
        return {"entry_sol": 0.04, "add_sol": 0.04, "max_sol": 0.12, "stop_loss_pct": 0.85}
    if score >= 60:
        return {"entry_sol": 0.03, "add_sol": 0.03, "max_sol": 0.09, "stop_loss_pct": 0.85}
    return {"entry_sol": 0.03, "add_sol": 0.00, "max_sol": 0.03, "stop_loss_pct": 0.85}


HUNTER_ADD_THRESHOLD_SOL = 1.0
MAX_ENTRY_PUMP_MULTIPLIER = 4.0
SELL_BUFFER = 0.999

TAKE_PROFIT_LEVELS = [
    (1.0, 0.50),
    (4.0, 0.50),
    (10.0, 0.80),
]

MIN_SHARE_VALUE_SOL = 0.01
MIN_SELL_RATIO = 0.3
FOLLOW_SELL_THRESHOLD = 0.05

PNL_CHECK_INTERVAL = 5
PNL_LOOP_RATE_LIMIT_SLEEP_SEC = 0.5

SOLANA_PRIVATE_KEY_BASE58 = os.getenv("SOLANA_PRIVATE_KEY")
