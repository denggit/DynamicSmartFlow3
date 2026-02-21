#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/16/2026 10:14 PM
@File       : __init__.py
@Description: Helius API 模块。
              对外暴露 HeliusClient 单例。
              统一入口请使用 src.blockchain.solana_client。
"""

from src.helius.client import HeliusClient, helius_client

__all__ = ["HeliusClient", "helius_client"]
