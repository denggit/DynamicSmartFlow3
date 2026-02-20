#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/16/2026
@File       : __init__.py
@Description: 服务层入口。
              统一 Solana RPC：from services import solana_client
"""

from services.blockchain import solana_client, SolanaClient
from services.helius import helius_client, HeliusClient
from services.alchemy import alchemy_client, AlchemyClient
from services.birdeye import birdeye_client, BirdeyeClient

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
