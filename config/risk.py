#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: 风控参数 (RugCheck、流动性、老鼠仓等)
"""
MAX_ACCEPTABLE_BUY_TAX_PCT = 25.0
MAX_SAFE_SCORE = 2000
# 跟单买入时池子流动性（USD）：< 此值拒绝；1000~3000 减半仓且不可加仓；>= 3000 正常
MIN_LIQUIDITY_REJECT = 1000.0  # 低于此值直接拒绝
MIN_LIQUIDITY_USD = 3000.0     # 低于此值减半仓
MIN_LP_LOCKED_PCT = 70.0
MAX_TOP2_10_COMBINED_PCT = 0.30
MAX_SINGLE_HOLDER_PCT = 0.10
MAX_ENTRY_FDV_USD = 1000000.0
MIN_LIQUIDITY_TO_FDV_RATIO = 0.03
