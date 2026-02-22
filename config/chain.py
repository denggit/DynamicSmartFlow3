#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Description: Solana 主网链常量
"""
WSOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
USDT_MINT = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"
LAMPORTS_PER_SOL = 1_000_000_000

# 挖掘/跟单时忽略的 mint（SOL/USDC/USDT 不算真实买卖）
IGNORE_MINTS = frozenset({WSOL_MINT, USDC_MINT, USDT_MINT})
