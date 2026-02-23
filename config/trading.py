#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 交易参数、跟单分档、止盈止损
"""
import os

# Jupiter API
JUP_QUOTE_API = "https://api.jup.ag/swap/v1/quote"
JUP_SWAP_API = "https://api.jup.ag/swap/v1/swap"

# Jupiter 自动滑点 (dynamicSlippage)：由 Jupiter 根据市场估算最优滑点，提高成交率
# 买入：最大滑点 3%，防止追高
BUY_MAX_SLIPPAGE_BPS = 300  # 3%
# 卖出：按档位重试，Jupiter 在每档上限内自动优化
SELL_SLIPPAGE_BPS_RETRIES = [500, 1000, 2000]   # 5%, 10%, 20%
SELL_SLIPPAGE_BPS_STOP_LOSS = 2000  # 止损清仓时用 20% 滑点，确保能成交
# 兼容旧引用（加仓等复用买入逻辑）
SLIPPAGE_BPS = BUY_MAX_SLIPPAGE_BPS
PRIORITY_FEE_SETTINGS = "auto"


def get_tier_config(score: float) -> dict:
    """根据猎手分数返回 entry/add/max/stop_loss。"""
    if score >= 90:
        return {"entry_sol": 0.06, "add_sol": 0.06, "max_sol": 0.36, "stop_loss_pct": 0.65}
    if score >= 80:
        return {"entry_sol": 0.05, "add_sol": 0.05, "max_sol": 0.25, "stop_loss_pct": 0.65}
    if score >= 70:
        return {"entry_sol": 0.04, "add_sol": 0.04, "max_sol": 0.16, "stop_loss_pct": 0.65}
    if score >= 60:
        return {"entry_sol": 0.03, "add_sol": 0.03, "max_sol": 0.09, "stop_loss_pct": 0.65}
    return {"entry_sol": 0.03, "add_sol": 0.00, "max_sol": 0.03, "stop_loss_pct": 0.65}


HUNTER_ADD_THRESHOLD_SOL = 1.0
MAX_ENTRY_PUMP_MULTIPLIER = 4.0
SELL_BUFFER = 0.999

TAKE_PROFIT_LEVELS = [
    (1.0, 0.25),
    (4.0, 0.25),
    (9.0, 0.50),
]

# 卖出价值下限：低于此值认为无意义（网费约 0.000011 SOL，0.0001 以下卖出可能净亏）
MIN_SHARE_VALUE_SOL = 0.0001
# 粉尘余额阈值（UI 数量）：低于此值的持仓不再尝试卖出，Jupiter 通常无路由
DUST_TOKEN_AMOUNT_UI = 1e-5
# 持仓数量 floor 精度：存储时向下取整，避免 round 进位导致记录 > 链上，卖出超量失败
TOKEN_AMOUNT_FLOOR_DECIMALS = 6
# 卖出失败时减量重试：如 6400.40 失败则试 6400.39，应对精度导致超量失败
SELL_RETRY_DECREMENT = 0.01
MIN_SELL_RATIO = 0.3
FOLLOW_SELL_THRESHOLD = 0.05

PNL_CHECK_INTERVAL = 5
PNL_LOOP_RATE_LIMIT_SLEEP_SEC = 0.5

# 买入后结构性风险：查 DexScreener 流动性，LP 撤池/净减少 30% 则清仓
LIQUIDITY_STRUCTURAL_CHECK_INTERVAL_SEC = 60  # 检查周期（秒），60 秒或 3600 秒(60分钟) 视需求改
LIQUIDITY_COLLAPSE_THRESHOLD_USD = 100.0  # 流动性 < 100U 视为 REMOVE LIQUIDITY
LIQUIDITY_DROP_RATIO = 0.7  # 当前流动性 < 入场时的 70% 即清仓
# DexScreener 请求间隔（持仓多时避免管控）
LIQUIDITY_CHECK_DEXSCREENER_INTERVAL_SEC = 1.2  # 每次请求间隔 ≥1 秒

SOLANA_PRIVATE_KEY_BASE58 = os.getenv("SOLANA_PRIVATE_KEY")
