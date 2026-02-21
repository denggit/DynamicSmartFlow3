#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/16/2026
@File       : __init__.py
@Description: 服务层入口。
              统一 Solana RPC：from src import solana_client
"""

try:
    from src.blockchain import solana_client, SolanaClient
except ImportError:
    solana_client = None
    SolanaClient = None

from src.helius import helius_client, HeliusClient
from src.alchemy import alchemy_client, AlchemyClient
from src.birdeye import birdeye_client, BirdeyeClient

__all__ = [
    "solana_client",
    "SolanaClient",
    "helius_client",
    "HeliusClient",
    "alchemy_client",
    "AlchemyClient",
    "birdeye_client",
    "BirdeyeClient",
]
