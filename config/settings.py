#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/16/2026 10:35 PM
@File       : settings.py
@Description: 
"""
import os
from pathlib import Path

# 获取项目根目录
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"

from dotenv import load_dotenv

load_dotenv(dotenv_path=ENV_PATH)

# --- API Keys ---
HELIUS_API_KEY = os.getenv("HELIUS_API_KEY")

# --- 基础配置 ---
WSS_ENDPOINT = f"wss://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"
HTTP_ENDPOINT = f"https://api.helius.xyz/v0/transactions/?api-key={HELIUS_API_KEY}"
HELIUS_RPC_URL = f"https://mainnet.helius-rpc.com/?api-key={HELIUS_API_KEY}"

# Jupiter API (免费版)
JUPITER_QUOTE_API = "https://quote-api.jup.ag/v6/quote"
JUPITER_SWAP_API = "https://quote-api.jup.ag/v6/swap"

# 交易参数
SLIPPAGE_BPS = 200  # 滑点 2% (200 bps)
PRIORITY_FEE_SETTINGS = "auto"

# 仓位限制
TRADING_MAX_SOL_PER_TOKEN = 0.5  # 单币最大持仓 (SOL)
TRADING_MIN_BUY_SOL = 0.1  # 单次最低买入 (SOL)
TRADING_ADD_BUY_SOL = 0.1  # 加仓固定金额 (SOL)

# 入场计算
TRADING_SCORE_MULTIPLIER = 0.001  # 总分 * 倍数 = 买入SOL
# 例如: 160分 * 0.001 = 0.16 SOL

# 猎手加仓跟随阈值
HUNTER_ADD_THRESHOLD_SOL = 1.0  # 猎手加仓超过 1 SOL 我们才跟

# 止盈策略 (触发倍数, 卖出比例)
# 1.5 = 收益率 150% (即 2.5倍)
TAKE_PROFIT_LEVELS = [
    (1.5, 0.5),  # 赚 150% -> 卖 50%
    (3.0, 0.5),  # 赚 300% -> 卖 50%
    (10.0, 0.5)  # 赚 1000% -> 卖 50%
]

# 份额管理
# 剩余份额价值不足 0.05 SOL 直接清仓
MIN_SHARE_VALUE_SOL = 0.05  # 份额粉尘阈值 (低于此值该份额清仓)
MIN_SELL_RATIO = 0.3  # 跟随卖出时我方最低减仓比例 (如文档: 猎手卖10%我们也至少卖30%)
FOLLOW_SELL_THRESHOLD = 0.05  # 猎手卖出比例低于此不跟 (微调忽略，文档: 大于5%才跟)

# 系统循环配置
PNL_CHECK_INTERVAL = 5  # 每 5 秒检查一次价格止盈

# 钱包配置 (请替换为真实私钥)
SOLANA_PRIVATE_KEY_BASE58 = os.getenv("SOLANA_PRIVATE_KEY")
