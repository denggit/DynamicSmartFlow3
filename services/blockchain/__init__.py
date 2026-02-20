#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : __init__.py
@Description: Solana RPC 统一入口。
              合并 Helius 与 Alchemy，对外暴露 solana_client。
"""

from services.blockchain.unified_client import SolanaClient, solana_client

__all__ = ["SolanaClient", "solana_client"]
