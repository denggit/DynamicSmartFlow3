#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
@Author     : Zijun Deng
@Date       : 2/20/2026
@File       : __init__.py
@Description: Birdeye 行情 API 模块。
              对外暴露 BirdeyeClient 单例。
"""

from src.birdeye.client import BirdeyeClient, birdeye_client

__all__ = ["BirdeyeClient", "birdeye_client"]
