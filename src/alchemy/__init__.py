#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : __init__.py
@Description: Alchemy Solana API 模块。
              对外暴露 AlchemyClient 单例。
"""

from src.alchemy.client import AlchemyClient, alchemy_client

__all__ = ["AlchemyClient", "alchemy_client"]
